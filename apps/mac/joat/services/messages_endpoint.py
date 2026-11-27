"""Messages relay: this Mac's texts, for JOAT Brain on the wearer's iPhone.

The glasses talk to ONE brain, the iPhone, with no manual switching between
a phone path and a Mac path. When a home Mac holds Messages, the iPhone asks
it over the tailnet for the few things Messaging needs, and the Mac keeps
doing what it already does well: read chat.db, send through Messages.app.

    GET  /v1/events?after=<row id>&wait=<s>       new incoming texts (long poll)
    GET  /v1/threads?limit=<n>&q=<search>         recent conversations (with search)
    GET  /v1/threads/<id>/messages?limit&before   one conversation's history
    POST /v1/send  {"thread": <id> | "to": <number or email>, "text", "client_id"}
    GET  /health                                  unauthenticated liveness

Rules this module holds:

- Bearer token on everything except /health, compared in constant time. The
  token lives in a 0600 file beside the agent token, or in the environment.
- Loopback only. Tailscale Serve (tailnet-only, never Funnel) puts it on the
  tailnet:  tailscale serve --bg --set-path /messages http://127.0.0.1:8789
- Chat GUIDs and handles never leave the Mac. The phone receives an opaque
  thread id (an HMAC of the private identity under the token) and sends
  that id back; the Mac resolves it to a target. A forged id cannot redirect
  a reply to a different conversation, and it reveals nothing if logged.
- A text to a NEW person (`to`) must look like a phone number or email; the
  sender passes it to Messages as an argument, never into a script.
- Sends are idempotent per `client_id`: a retry after a dropped response
  answers with the first outcome instead of texting twice.
- Message bodies are never logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from ..channels.imessage import IMessageStore, IncomingText, MessageEntry, MessageThread
from ..channels.message_sender import MessageSendError
from ..core.interfaces import MessageSender
from .agent_endpoint import BadRequest, HttpRequest, read_http_request

log = logging.getLogger(__name__)

DEFAULT_PORT = 8789
TOKEN_PATH = Path.home() / "Library/Application Support/JOAT/messages-token"
READ_STATE_PATH = Path.home() / "Library/Application Support/JOAT/messages-read.json"
SHORTCUT_MARK_READ_NAME = "Mark Message Read from JOAT"
MAX_BODY_BYTES = 16 * 1024
MAX_TEXT_CHARS = 2_000
MAX_WAIT_S = 25.0
DEFAULT_THREADS = 10
MAX_THREADS = 50
DEFAULT_PAGE = 16
MAX_PAGE = 50
MAX_QUERY_CHARS = 120
_PREVIEW_CHARS = 140
_REGISTRY_SIZE = 256
_READ_KEEP = 256
_SEEN_SENDS = 128

_REASONS = {
    200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
    405: "Method Not Allowed", 411: "Length Required", 413: "Payload Too Large",
    431: "Request Header Fields Too Large", 500: "Internal Server Error",
    502: "Bad Gateway",
}
_THREAD_ID = re.compile(r"[0-9a-f]{20}")
_THREAD_MESSAGES = re.compile(r"^/v1/threads/([0-9a-f]{20})/messages$")
_PHONE = re.compile(r"^\+?[0-9][0-9 ().-]{3,19}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class _BadParameter(ValueError):
    pass


def messages_token(path: Path = TOKEN_PATH, env: str = "JOAT_MESSAGES_TOKEN") -> str:
    """The shared secret the iPhone presents. The environment wins; otherwise
    a file created once with 0600 permissions, so it is read from the Mac
    into the phone's build and never typed into a chat."""
    configured = os.environ.get(env, "").strip()
    if configured:
        return configured
    try:
        existing = path.read_text().strip()
    except OSError:
        existing = ""
    if existing:
        return existing
    token = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n")
    path.chmod(0o600)
    log.info("messages token created at %s", path)
    return token


def _error(kind: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"error": {"type": kind, "message": message, **extra}}


def _looks_like_handle(value: str) -> bool:
    value = value.strip()
    return bool(_PHONE.match(value) or _EMAIL.match(value))


def _int(
    query: dict[str, list[str]], name: str, *, default: int | None = None,
    minimum: int | None = None, maximum: int | None = None,
) -> int:
    values = query.get(name)
    if not values:
        if default is None:
            raise _BadParameter(f"{name} is required")
        return default
    try:
        value = int(values[0])
    except ValueError as exc:
        raise _BadParameter(f"{name} must be a whole number") from exc
    if minimum is not None and value < minimum:
        raise _BadParameter(f"{name} must be at least {minimum}")
    if maximum is not None:
        value = min(value, maximum)
    return value


def _entry_json(entry: MessageEntry) -> dict[str, Any]:
    return {
        "id": entry.row_id,
        "text": entry.text,
        "at": entry.at,
        "from_me": entry.from_me,
        "person": entry.sender,
        "delivery": entry.delivery,
        "attachment": entry.attachment_count > 0,
        "unread": entry.unread,
    }


class ShortcutsMessagesReadMarker:
    """Best-effort native Messages.app read sync via Apple's Shortcuts CLI.

    When the user installs ``Mark Message Read from JOAT.shortcut`` in macOS
    Shortcuts, running ``shortcuts run`` invokes Apple's native ChatKit
    ``MarkConversationAsUnreadIntent`` (``OpenWhenRun=false``,
    ``unreadState=false``) in the background—clearing the unread badge in
    Messages.app on this Mac and syncing read state to iPhone via iCloud
    Messages without opening windows or stealing focus.
    """

    def __init__(
        self,
        *,
        shortcut_name: str = SHORTCUT_MARK_READ_NAME,
        shortcuts_bin: str = "/usr/bin/shortcuts",
        timeout_s: float = 10.0,
        cache_ttl_s: float = 60.0,
    ) -> None:
        self._shortcut_name = shortcut_name
        self._bin = shortcuts_bin
        self._timeout_s = timeout_s
        self._cache_ttl_s = cache_ttl_s
        self._installed: bool | None = None
        self._checked_at = 0.0

    async def _is_installed(self) -> bool:
        now = time.monotonic()
        if self._installed is not None and (now - self._checked_at) < self._cache_ttl_s:
            return self._installed
        self._checked_at = now
        if not Path(self._bin).is_file():
            self._installed = False
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                self._bin, "list",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            names = {line.strip() for line in stdout.decode("utf-8", errors="replace").splitlines()}
            self._installed = self._shortcut_name in names
        except Exception:
            self._installed = False
        return bool(self._installed)

    async def mark_read(self, thread: MessageThread) -> bool:
        display_name = (thread.sender or "").strip()
        if not display_name or "\0" in display_name:
            return False
        if not await self._is_installed():
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                self._bin,
                "run",
                self._shortcut_name,
                "--input-path",
                "-",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception:
            return False
        comm = asyncio.create_task(proc.communicate(display_name.encode("utf-8")))
        try:
            await asyncio.wait_for(asyncio.shield(comm), timeout=self._timeout_s)
            return proc.returncode == 0
        except Exception:
            if proc.returncode is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except Exception:
                    try:
                        proc.kill()
                    except OSError:
                        pass
            if not comm.done():
                comm.cancel()
            await asyncio.gather(comm, return_exceptions=True)
            return False


class MessagesEndpoint:
    """Serve the Mac's Messages to the wearer's own phone."""

    def __init__(
        self,
        store: IMessageStore,
        sender: MessageSender,
        token: str,
        *,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        poll_s: float = 1.0,
        max_wait_s: float = MAX_WAIT_S,
        clock: Callable[[], float] = time.monotonic,
        read_state_path: Path | None = None,
        read_marker: Callable[[MessageThread], Awaitable[bool]] | None = None,
    ) -> None:
        if not token:
            raise ValueError("messages token must be non-empty")
        self._store = store
        self._sender = sender
        self._token = token
        self._key = token.encode()
        self._host = host
        self._port = port
        self._poll_s = poll_s
        self._max_wait_s = max_wait_s
        self._clock = clock
        self._read_state_path = read_state_path
        self._read_marker = read_marker
        self._server: asyncio.AbstractServer | None = None
        self._threads: OrderedDict[str, MessageThread] = OrderedDict()
        self._read_state: OrderedDict[str, dict[str, float | int]] = OrderedDict()
        self._marker_tasks: set[asyncio.Task[Any]] = set()
        self._sends: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._send_lock = asyncio.Lock()
        self._load_read_state()

    @property
    def port(self) -> int:
        return self._port

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self._host, self._port)
        sockets = self._server.sockets
        if sockets:
            self._port = int(sockets[0].getsockname()[1])
        log.info(
            "messages endpoint listening on http://%s:%d/v1 (expose with: "
            "tailscale serve --bg --set-path /messages http://127.0.0.1:%d)",
            self._host, self._port, self._port,
        )

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        if self._marker_tasks:
            pending = list(self._marker_tasks)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            self._marker_tasks.clear()

    def _load_read_state(self) -> None:
        path = self._read_state_path
        if path is None or not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if not isinstance(raw, dict):
            return
        for key, val in raw.items():
            if not isinstance(key, str) or not _THREAD_ID.fullmatch(key) or not isinstance(val, dict):
                continue
            at_val = val.get("at", 0.0)
            row_val = val.get("row_id", 0)
            if isinstance(at_val, (int, float)) and isinstance(row_val, int):
                self._read_state[key] = {"at": float(at_val), "row_id": int(row_val)}
        while len(self._read_state) > _READ_KEEP:
            self._read_state.popitem(last=False)

    def _persist_read_state(self) -> None:
        path = self._read_state_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._read_state) + "\n", encoding="utf-8")
            tmp.chmod(0o600)
            tmp.replace(path)
            path.chmod(0o600)
        except OSError:
            log.warning("messages read state could not be persisted")

    def _is_thread_unread(self, thread_id: str, thread: MessageThread) -> bool:
        if not thread.unread:
            return False
        mark = self._read_state.get(thread_id)
        if mark is None:
            return True
        return float(thread.at) > float(mark.get("at", 0.0)) + 0.5

    def _is_entry_unread(self, thread_id: str, entry: MessageEntry) -> bool:
        if not entry.unread:
            return False
        mark = self._read_state.get(thread_id)
        if mark is None:
            return True
        mark_row = int(mark.get("row_id", 0))
        if entry.row_id > 0 and mark_row > 0:
            return entry.row_id > mark_row
        return float(entry.at) > float(mark.get("at", 0.0)) + 0.5

    # -- identity ------------------------------------------------------------

    def _thread_id(self, thread: MessageThread) -> str:
        """Opaque and stable: an HMAC of the private identity, so the same
        conversation keeps one id across restarts and nothing is revealed."""
        private = thread.chat_guid or thread.handle or f"chat#{thread.chat_id or 0}"
        return hmac.new(self._key, private.encode(), hashlib.sha256).hexdigest()[:20]

    def _register(self, thread: MessageThread) -> str:
        thread_id = self._thread_id(thread)
        self._threads[thread_id] = thread
        self._threads.move_to_end(thread_id)
        while len(self._threads) > _REGISTRY_SIZE:
            self._threads.popitem(last=False)
        return thread_id

    @staticmethod
    def _thread_of(text: IncomingText) -> MessageThread:
        return MessageThread(
            sender=text.label,
            handle=text.handle,
            text=text.text,
            at=text.at,
            replyable=bool(text.chat_guid) or not text.is_group,
            chat_id=text.chat_id,
            attachment_count=text.attachment_count,
            chat_guid=text.chat_guid,
            is_group=text.is_group,
        )

    # -- one connection ------------------------------------------------------

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        try:
            try:
                request = await asyncio.wait_for(
                    read_http_request(reader, MAX_BODY_BYTES), timeout=15.0,
                )
            except BadRequest as exc:
                await self._write(writer, exc.status, _error("bad_request", exc.message))
                return
            except (asyncio.TimeoutError, ConnectionError):
                return
            status, payload = await self._dispatch(request)
            await self._write(writer, status, payload)
        except ConnectionError:
            pass
        except Exception:
            log.exception("messages endpoint request failed")
            try:
                await self._write(writer, 500, _error("server_error", "internal error"))
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def _authorized(self, header: str) -> bool:
        scheme, _, value = header.partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(
            value.strip().encode(), self._key,
        )

    async def _dispatch(self, request: HttpRequest) -> tuple[int, dict[str, Any]]:
        path = request.path
        # Tailscale Serve may or may not strip its mount path; accept both.
        if path == "/messages" or path.startswith("/messages/"):
            path = path[len("/messages"):] or "/"
        if request.method == "GET" and path in ("/health", "/v1/health"):
            return 200, {"ok": True, "messages": self._store.available}
        if not self._authorized(request.headers.get("authorization", "")):
            return 401, _error("unauthorized", "unauthorized")
        if not self._store.available:
            return 502, _error("store_unavailable", "Messages is not readable on this Mac")
        query = parse_qs(request.query, keep_blank_values=False)
        try:
            if path == "/v1/events" and request.method == "GET":
                return await self._events(query)
            if path == "/v1/threads" and request.method == "GET":
                return await self._list_threads(query)
            match = _THREAD_MESSAGES.match(path)
            if match and request.method == "GET":
                return await self._history(match.group(1), query)
            if path == "/v1/send" and request.method == "POST":
                return await self._send(request.body)
            if path == "/v1/read" and request.method == "POST":
                return await self._read_thread(request.body)
        except _BadParameter as exc:
            return 400, _error("bad_request", str(exc))
        if path in {"/v1/events", "/v1/threads", "/v1/send", "/v1/read"} or _THREAD_MESSAGES.match(path):
            return 405, _error("method_not_allowed", "wrong method for this route")
        return 404, _error("not_found", "not found")

    # -- routes --------------------------------------------------------------

    async def _events(self, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
        """Long poll. Without `after` the answer is only the current
        watermark: a phone that has never connected starts from now, so a
        mailbox full of years of history (iCloud syncs it onto a new Mac)
        is never replayed onto the glasses."""
        if "after" not in query:
            baseline = await asyncio.to_thread(self._store.baseline)
            return 200, {"texts": [], "after": baseline, "more": False}
        after = _int(query, "after", minimum=0)
        wait = min(float(_int(query, "wait", default=0, minimum=0)), self._max_wait_s)
        deadline = self._clock() + wait
        while True:
            texts, watermark, more = await asyncio.to_thread(
                self._store.read_new_texts, after,
            )
            now = self._clock()
            if texts or more or watermark != after or now >= deadline:
                return 200, {
                    "texts": [self._text_json(text) for text in texts],
                    "after": watermark,
                    "more": more,
                }
            await asyncio.sleep(max(0.0, min(self._poll_s, deadline - now)))

    def _text_json(self, text: IncomingText) -> dict[str, Any]:
        return {
            "id": text.row_id,
            "thread": self._register(self._thread_of(text)),
            "label": text.label,
            "person": text.person,
            "group": text.is_group,
            "replyable": bool(text.chat_guid) or not text.is_group,
            "text": text.text,
            "at": text.at,
            "attachment": text.attachment_count > 0,
        }

    async def _list_threads(self, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
        limit = _int(query, "limit", default=DEFAULT_THREADS, minimum=1, maximum=MAX_THREADS)
        raw_q = (query.get("q") or [""])[0].strip()[:MAX_QUERY_CHARS]
        if raw_q:
            candidates = await asyncio.to_thread(
                self._store.recent_threads, max(limit * 5, 100),
            )
            needle = raw_q.casefold()
            threads = []
            for thread in candidates:
                raw_label = getattr(thread, "sender", None)
                if raw_label is None:
                    raw_label = getattr(thread, "label", "")
                raw_text = getattr(thread, "text", None)
                if raw_text is None:
                    raw_text = getattr(thread, "preview", "")
                if (
                    needle in str(raw_label or "").casefold()
                    or needle in str(raw_text or "").casefold()
                ):
                    threads.append(thread)
                    if len(threads) >= limit:
                        break
        else:
            threads = await asyncio.to_thread(self._store.recent_threads, limit)
        out_threads = []
        for thread in threads:
            tid = self._register(thread)
            out_threads.append({
                "id": tid,
                "label": thread.sender,
                "group": thread.is_group,
                "replyable": thread.reply_target is not None,
                "preview": thread.text[:_PREVIEW_CHARS],
                "at": thread.at,
                "unread": self._is_thread_unread(tid, thread),
            })
        return 200, {"threads": out_threads}

    async def _history(
        self, thread_id: str, query: dict[str, list[str]],
    ) -> tuple[int, dict[str, Any]]:
        thread = self._threads.get(thread_id)
        if thread is None:
            return 404, _error("unknown_thread", "list the threads again")
        limit = _int(query, "limit", default=DEFAULT_PAGE, minimum=1, maximum=MAX_PAGE)
        before = _int(query, "before", default=0, minimum=0) or None
        page = await asyncio.to_thread(
            lambda: self._store.thread_message_page(
                thread, limit=limit, before_row_id=before,
            ),
        )
        return 200, {
            "messages": [
                {**_entry_json(entry), "unread": self._is_entry_unread(thread_id, entry)}
                for entry in page.entries
            ],
            "older": page.has_older,
        }

    async def _read_thread(self, raw: bytes) -> tuple[int, dict[str, Any]]:
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return 400, _error("bad_request", "body is not JSON")
        if not isinstance(body, dict):
            return 400, _error("bad_request", "body must be an object")
        thread_id = body.get("thread")
        if not isinstance(thread_id, str) or not _THREAD_ID.fullmatch(thread_id):
            return 400, _error("bad_request", "thread must be a 20-char hex id")
        thread = self._threads.get(thread_id)
        if thread is None:
            return 404, _error("unknown_thread", "list the threads again")

        latest_at = float(thread.at)
        latest_row = 0
        try:
            latest_threads = await asyncio.to_thread(self._store.recent_threads, 30)
            for candidate in latest_threads:
                cid = self._register(candidate)
                if cid == thread_id:
                    thread = candidate
                    latest_at = max(latest_at, float(candidate.at))
                    break
            page = await asyncio.to_thread(
                lambda: self._store.thread_message_page(thread, limit=1),
            )
            if page.entries:
                latest_at = max(latest_at, float(page.entries[0].at))
                latest_row = max(0, int(page.entries[0].row_id))
        except Exception:
            pass

        prev = self._read_state.get(thread_id, {})
        self._read_state[thread_id] = {
            "at": max(latest_at, float(prev.get("at", 0.0))),
            "row_id": max(latest_row, int(prev.get("row_id", 0))),
        }
        self._read_state.move_to_end(thread_id)
        while len(self._read_state) > _READ_KEEP:
            self._read_state.popitem(last=False)
        self._persist_read_state()

        if self._read_marker is not None:
            task = asyncio.create_task(self._run_read_marker(thread))
            self._marker_tasks.add(task)
            task.add_done_callback(self._marker_tasks.discard)

        return 200, {"ok": True}

    async def _run_read_marker(self, thread: MessageThread) -> None:
        if self._read_marker is None:
            return
        try:
            await self._read_marker(thread)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("messages upstream read marker failed", exc_info=True)

    async def _send(self, raw: bytes) -> tuple[int, dict[str, Any]]:
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return 400, _error("bad_request", "body is not JSON")
        if not isinstance(body, dict):
            return 400, _error("bad_request", "body must be an object")
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return 400, _error("bad_request", "text is required")
        if len(text) > MAX_TEXT_CHARS:
            return 400, _error("bad_request", "text is too long")
        client_id = body.get("client_id")
        if client_id is not None and (
            not isinstance(client_id, str) or not 0 < len(client_id) <= 64
        ):
            return 400, _error("bad_request", "client_id must be 1-64 characters")

        thread_id, to = body.get("thread"), body.get("to")
        target: tuple[str, str]
        if isinstance(thread_id, str) and to is None:
            thread = self._threads.get(thread_id) if _THREAD_ID.fullmatch(thread_id) else None
            if thread is None:
                return 404, _error("unknown_thread", "list the threads again")
            resolved = thread.reply_target
            if resolved is None:
                return 400, _error("not_replyable", "this conversation cannot be replied to")
            target = resolved
        elif isinstance(to, str) and thread_id is None:
            if not _looks_like_handle(to):
                return 400, _error("bad_request", "to must be a phone number or email")
            target = ("participant", to.strip())
        else:
            return 400, _error("bad_request", "send to exactly one of thread or to")

        async with self._send_lock:
            if client_id is not None and client_id in self._sends:
                return 200, {**self._sends[client_id], "repeat": True}
            kind, destination = target
            try:
                if kind == "chat":
                    await self._sender.send_chat(destination, text)
                else:
                    await self._sender.send(destination, text)
            except MessageSendError as exc:
                log.warning("relay send failed code=%s", getattr(exc, "code", "send"))
                return 502, _error(
                    "send_failed", "Messages did not accept the text",
                    code=getattr(exc, "code", "send"),
                    possibly_sent=exc.possible_external_commit,
                )
            result = {"ok": True}
            if client_id is not None:
                self._sends[client_id] = result
                while len(self._sends) > _SEEN_SENDS:
                    self._sends.popitem(last=False)
            log.info("relay send ok kind=%s chars=%d", kind, len(text))
            return 200, result

    # -- responses -----------------------------------------------------------

    async def _write(
        self, writer: asyncio.StreamWriter, status: int, payload: dict[str, Any],
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        head = (
            f"HTTP/1.1 {status} {_REASONS.get(status, 'OK')}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(head + body)
        await writer.drain()


__all__ = [
    "DEFAULT_PAGE",
    "DEFAULT_PORT",
    "DEFAULT_THREADS",
    "MAX_PAGE",
    "MAX_THREADS",
    "MessagesEndpoint",
    "READ_STATE_PATH",
    "SHORTCUT_MARK_READ_NAME",
    "ShortcutsMessagesReadMarker",
    "TOKEN_PATH",
    "messages_token",
]
