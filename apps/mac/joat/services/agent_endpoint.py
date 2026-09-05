"""The door Even AI knocks on: an OpenAI-shaped chat endpoint on the brain.

Even's own assistant can be pointed at a custom agent (Even app → Settings →
Even AI → Agent Configure: a name, a URL and a token). From then on "Hey
Even" sends the wearer's transcribed words here as an OpenAI chat-completions
POST with a bearer token, and paints the plain-text answer on the lenses
itself. Even owns the wake word, speech-to-text and rendering; the brain
owns the mind and the tools. This module is only the HTTP shape between
them, so nothing about the brain's capabilities lives here.

Loopback only. Tailscale Serve, which already fronts the glasses gateway,
carries it to the phone over HTTPS:

    tailscale serve --bg --set-path /agent http://127.0.0.1:8788

The phone then dials https://<this-mac>.<tailnet>.ts.net/agent/v1/chat/completions.
Paths are matched by suffix so a mounted prefix changes nothing.

Content-free logging, as everywhere in the brain: character counts and
latency, never the words.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from ..agents.tools import ToolBelt, ToolCall
from ..core.interfaces import ReplyEngine

log = logging.getLogger(__name__)

DEFAULT_PORT = 8788
MAX_BODY_BYTES = 64 * 1024
MAX_LENS_CHARS = 600
REQUEST_TIMEOUT_S = 90.0
TOKEN_PATH = Path.home() / "Library/Application Support/JOAT/agent-token"
EMPTY_REPLY = "I didn't get an answer for that — say it again."

_REASONS = {
    200: "OK", 202: "Accepted", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
    405: "Method Not Allowed", 411: "Length Required", 413: "Payload Too Large",
    431: "Request Header Fields Too Large", 500: "Internal Server Error",
    504: "Gateway Timeout",
}


@dataclass(frozen=True)
class HttpRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


class BadRequest(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


async def read_http_request(
    reader: asyncio.StreamReader, max_body: int = MAX_BODY_BYTES,
) -> HttpRequest:
    """One HTTP/1.1 request with a Content-Length body. Nothing else is needed:
    the Even app's Dart client sends exactly that shape."""
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except asyncio.LimitOverrunError as exc:
        raise BadRequest(431, "headers too large") from exc
    except asyncio.IncompleteReadError as exc:
        raise BadRequest(400, "truncated request") from exc
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3:
        raise BadRequest(400, "bad request line")
    method, target, _version = parts
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep:
            raise BadRequest(400, "bad header")
        headers[name.strip().lower()] = value.strip()
    if "chunked" in headers.get("transfer-encoding", "").lower():
        raise BadRequest(411, "length required")
    try:
        length = int(headers.get("content-length", "0"))
    except ValueError as exc:
        raise BadRequest(400, "bad content-length") from exc
    if length < 0:
        raise BadRequest(400, "bad content-length")
    if length > max_body:
        raise BadRequest(413, "body too large")
    try:
        body = await reader.readexactly(length) if length else b""
    except asyncio.IncompleteReadError as exc:
        raise BadRequest(400, "truncated body") from exc
    return HttpRequest(method.upper(), target.split("?", 1)[0], headers, body)


def lens_text(text: str, max_chars: int = MAX_LENS_CHARS) -> str:
    """Plain text sized for the lenses: markdown gone, whitespace collapsed,
    cut at a sentence or word boundary. The G2 renders no markup, and a wall
    of text past ~500 characters is unreadable on it."""
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = t.replace("`", "")
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", t, flags=re.M)
    t = re.sub(r"(\*\*|__)(.*?)\1", r"\2", t, flags=re.S)
    t = re.sub(r"(?<!\w)[*_](\S[^*_]*?)[*_](?!\w)", r"\1", t)
    t = re.sub(r"\s+", " ", t).strip()
    if max_chars <= 0 or len(t) <= max_chars:
        return t
    cut = t[:max_chars]
    boundary = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if boundary >= max_chars // 2:
        return cut[:boundary + 1]
    space = cut.rfind(" ")
    if space >= max_chars // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:") + "…"


def _message_text(message: Any) -> str | None:
    """Plain text of one OpenAI-shaped message, or None when it has none."""
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        texts = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        joined = " ".join(t for t in texts if t).strip()
        return joined or None
    return None


def last_user_text(body: dict[str, Any]) -> str | None:
    """The newest user turn of an OpenAI-shaped request, as plain text."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        return _message_text(message)
    return None


# How much of the caller's conversation rides into the seat. Even AI sends
# the whole chat every time; the seat itself resumes nothing (the audit's
# A8), so this block IS its memory. Bounded so a long chat cannot push the
# actual question out of a small model's window.
HISTORY_TURNS = 8
HISTORY_CHARS = 2_400


def conversation_context(body: dict[str, Any]) -> str:
    """The caller's earlier turns, newest last, as a block the seat can read.

    Everything before the newest user message is history; system messages
    are the caller's own instructions and are not replayed as speech.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ""
    latest = None
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], dict) and messages[index].get("role") == "user":
            latest = index
            break
    if latest is None:
        return ""
    turns: list[str] = []
    for message in messages[:latest]:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        text = _message_text(message)
        if not text or role not in ("user", "assistant"):
            continue
        turns.append(f"{'Wearer' if role == 'user' else 'JOAT'}: {text}")
    turns = turns[-HISTORY_TURNS:]
    block = "\n".join(turns)
    while len(block) > HISTORY_CHARS and len(turns) > 1:
        turns.pop(0)
        block = "\n".join(turns)
    if len(block) > HISTORY_CHARS:
        block = "…" + block[-HISTORY_CHARS:]
    return block


def seat_prompt(body: dict[str, Any], query: str) -> str:
    """The text the seat receives: the caller's history, then the question."""
    context = conversation_context(body)
    if not context:
        return query
    return (
        "Earlier in this conversation (the wearer's own app keeps it; treat "
        f"it as what was already said):\n{context}\n\nWearer: {query}"
    )


_STREAM_MARKUP = re.compile(r"`+|\*\*|__|^\s{0,3}#{1,6}\s*|^\s*(?:[-*•]|\d+[.)])\s+", re.M)


def stream_text(piece: str) -> str:
    """Lens-safe text for ONE streamed chunk: markup gone, newlines spaced.

    The whole-answer cleaner (`lens_text`) needs the whole answer. While
    streaming, each chunk gets the same treatment it can get on its own;
    the character cap is enforced by the caller across chunks.
    """
    t = _STREAM_MARKUP.sub("", piece)
    return re.sub(r"\s+", " ", t)


def agent_token(path: Path = TOKEN_PATH, env: str = "JOAT_AGENT_TOKEN") -> str:
    """The shared secret the Even app must present.

    The environment wins; otherwise a token file, created once with 0600
    permissions so the wearer can read it into the Even app and never type
    it into a chat.
    """
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
    log.info(
        "agent token created at %s — enter it in Even → Settings → Even AI → "
        "Agent Configure", path,
    )
    return token


def _is_chat_path(path: str) -> bool:
    trimmed = path.rstrip("/")
    return trimmed == "" or trimmed.endswith("/chat/completions") or trimmed.endswith("/agent")


def _is_health_path(path: str) -> bool:
    return path.rstrip("/").endswith("/health")


def _is_mcp_path(path: str) -> bool:
    return path.rstrip("/").endswith("/mcp")


def _rpc_error(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


class AgentEndpoint:
    """Serve the brain's mind to Even AI in the shape Even AI speaks."""

    def __init__(
        self,
        replier: ReplyEngine,
        token: str,
        *,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        model_label: Callable[[], str] | None = None,
        tools: ToolBelt | None = None,
        max_body: int = MAX_BODY_BYTES,
        max_lens_chars: int = MAX_LENS_CHARS,
        request_timeout_s: float = REQUEST_TIMEOUT_S,
    ) -> None:
        if not token:
            raise ValueError("agent token must be non-empty")
        self._replier = replier
        self._token = token
        self._host = host
        self._port = port
        self._model_label = model_label or (lambda: "joat")
        self._tools = tools
        self._max_body = max_body
        self._max_lens_chars = max_lens_chars
        self._request_timeout_s = request_timeout_s
        self._server: asyncio.AbstractServer | None = None

    @property
    def port(self) -> int:
        return self._port

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self._host, self._port)
        sockets = self._server.sockets
        if sockets:
            self._port = int(sockets[0].getsockname()[1])
        log.info(
            "agent endpoint listening on http://%s:%d/v1/chat/completions",
            self._host, self._port,
        )

    async def stop(self) -> None:
        server = self._server
        if server is None:
            return
        self._server = None
        server.close()
        await server.wait_closed()

    # ── one connection ──────────────────────────────────────────────────────

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        started = time.monotonic()
        try:
            try:
                request = await asyncio.wait_for(
                    read_http_request(reader, self._max_body), timeout=15.0,
                )
            except BadRequest as exc:
                await self._write_json(writer, exc.status, _error(exc.message))
                return
            except (asyncio.TimeoutError, ConnectionError):
                return
            await self._dispatch(request, writer, started)
        except Exception:
            log.exception("agent endpoint request failed")
            try:
                await self._write_json(writer, 500, _error("internal error", "server_error"))
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch(
        self, request: HttpRequest, writer: asyncio.StreamWriter, started: float,
    ) -> None:
        if request.method == "GET" and _is_health_path(request.path):
            await self._write_json(writer, 200, {"ok": True, "model": self._model_label()})
            return
        if _is_mcp_path(request.path):
            await self._mcp(request, writer)
            return
        if not _is_chat_path(request.path):
            await self._write_json(writer, 404, _error("not found"))
            return
        if request.method != "POST":
            await self._write_json(writer, 405, _error("use POST"))
            return
        if not self._authorized(request.headers.get("authorization", "")):
            await self._write_json(
                writer, 401, _error("unauthorized", "authentication_error"),
            )
            return
        try:
            body = json.loads(request.body or b"{}")
        except ValueError:
            await self._write_json(writer, 400, _error("body is not JSON"))
            return
        if not isinstance(body, dict):
            await self._write_json(writer, 400, _error("body must be an object"))
            return
        query = last_user_text(body)
        if query is None:
            await self._write_json(writer, 400, _error("no user message"))
            return
        wants_stream = bool(body.get("stream"))
        prompt = seat_prompt(body, query)
        if wants_stream:
            # Words reach the phone as the mind produces them (the audit's
            # A8): the answer used to be collected whole, then replayed as
            # one SSE chunk, so streaming bought the wearer nothing.
            sent = await self._stream_sse(writer, self._replier.stream(prompt))
            log.info(
                "agent request chars=%d reply_chars=%d ms=%d stream=True",
                len(query), sent, int((time.monotonic() - started) * 1000),
            )
            return
        try:
            answer = await asyncio.wait_for(
                self._collect(prompt), timeout=self._request_timeout_s,
            )
        except asyncio.TimeoutError:
            log.warning("agent request timed out after %.0fs", self._request_timeout_s)
            await self._write_json(writer, 504, _error("the assistant took too long", "timeout"))
            return
        text = lens_text(answer, self._max_lens_chars) or EMPTY_REPLY
        log.info(
            "agent request chars=%d reply_chars=%d ms=%d stream=False",
            len(query), len(text), int((time.monotonic() - started) * 1000),
        )
        await self._write_json(writer, 200, self._completion(text))

    # ── the MCP route: the brain's tools, offered to the CLI seats ──────────

    async def _mcp(self, request: HttpRequest, writer: asyncio.StreamWriter) -> None:
        """MCP over streamable HTTP, JSON responses only.

        `claude --mcp-config` and `codex -c mcp_servers.*` both speak this:
        one JSON-RPC message per POST. There is no server-initiated stream,
        so GET answers 405 as the transport allows, and a DELETE that ends a
        session is acknowledged and forgotten — the route is stateless.
        """
        if request.method == "GET":
            await self._write_json(writer, 405, _error("no event stream"))
            return
        if request.method == "DELETE":
            await self._write_json(writer, 200, {})
            return
        if request.method != "POST":
            await self._write_json(writer, 405, _error("use POST"))
            return
        if not self._authorized(request.headers.get("authorization", "")):
            await self._write_json(writer, 401, _error("unauthorized", "authentication_error"))
            return
        try:
            payload = json.loads(request.body or b"")
        except ValueError:
            await self._write_json(writer, 400, _error("body is not JSON"))
            return
        messages = payload if isinstance(payload, list) else [payload]
        replies: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict):
                replies.append(_rpc_error(None, -32600, "invalid request"))
                continue
            reply = await self._rpc(message)
            if reply is not None:
                replies.append(reply)
        if not replies:
            await self._write_raw(writer, 202, "application/json", b"")
            return
        body = json.dumps(replies if isinstance(payload, list) else replies[0]).encode("utf-8")
        await self._write_raw(writer, 200, "application/json", body)

    async def _rpc(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        id_ = message.get("id")
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        if not isinstance(method, str):
            return _rpc_error(id_, -32600, "method required")
        if method.startswith("notifications/"):
            return None
        result: dict[str, Any]
        if method == "initialize":
            requested = params.get("protocolVersion")
            result = {
                "protocolVersion": requested if isinstance(requested, str) else "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "joat", "version": "1"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            definitions = self._tools.tool_definitions() if self._tools is not None else []
            result = {"tools": [
                {"name": d["name"], "description": d["description"],
                 "inputSchema": d["input_schema"]}
                for d in definitions
            ]}
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments")
            if self._tools is None or not isinstance(name, str):
                text = "error: no such tool"
            else:
                text = await self._tools.execute(ToolCall(
                    name=name,
                    arguments=dict(arguments) if isinstance(arguments, dict) else {},
                ))
            log.info("mcp tool call chars=%d error=%s", len(text), text.startswith("error:"))
            result = {"content": [{"type": "text", "text": text}],
                      "isError": text.startswith("error:")}
        else:
            return _rpc_error(id_, -32601, f"unknown method {method}")
        return {"jsonrpc": "2.0", "id": id_, "result": result}

    async def _collect(self, query: str) -> str:
        parts: list[str] = []
        async for chunk in self._replier.stream(query):
            parts.append(chunk)
        return "".join(parts)

    def _authorized(self, header: str) -> bool:
        scheme, _, value = header.partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(
            value.strip().encode(), self._token.encode(),
        )

    # ── responses ───────────────────────────────────────────────────────────

    def _completion(self, text: str) -> dict[str, Any]:
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self._model_label(),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    def _sse_chunk(self, completion_id: str, created: int,
                   delta: dict[str, Any], finish: str | None) -> bytes:
        payload = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": self._model_label(),
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(payload)}\n\n".encode()

    async def _write_sse(self, writer: asyncio.StreamWriter, text: str) -> None:
        """One complete answer as a single SSE event (non-streamed fallback)."""
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        body = (
            self._sse_chunk(completion_id, created, {"role": "assistant", "content": text}, None)
            + self._sse_chunk(completion_id, created, {}, "stop")
            + b"data: [DONE]\n\n"
        )
        await self._write_raw(writer, 200, "text/event-stream", body)

    async def _stream_sse(
        self, writer: asyncio.StreamWriter, chunks: AsyncIterator[str],
    ) -> int:
        """Relay the mind's chunks as they arrive. Returns characters sent.

        Close-delimited body: no Content-Length, `Connection: close`, and
        the socket ends the stream — the form HTTP/1.1 (RFC 9112 §6.3)
        allows and every chat-completions client already handles. The
        request deadline bounds the whole stream; on expiry the wearer
        gets what was said so far, ended cleanly, not a dropped socket.
        """
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        head = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(head)
        await writer.drain()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._request_timeout_s
        sent = 0
        opened = False
        async with aclosing(cast(AsyncGenerator[str, None], chunks)) as source:
            while sent < self._max_lens_chars:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    log.warning("agent stream cut at %.0fs", self._request_timeout_s)
                    break
                try:
                    piece = await asyncio.wait_for(anext(source), remaining)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    log.warning("agent stream cut at %.0fs", self._request_timeout_s)
                    break
                text = stream_text(piece)
                if not text:
                    continue
                text = text[: self._max_lens_chars - sent]
                delta: dict[str, Any] = {"content": text}
                if not opened:
                    delta["role"] = "assistant"
                    opened = True
                writer.write(self._sse_chunk(completion_id, created, delta, None))
                await writer.drain()
                sent += len(text)
        if not opened:
            writer.write(self._sse_chunk(
                completion_id, created, {"role": "assistant", "content": EMPTY_REPLY}, None,
            ))
        writer.write(self._sse_chunk(completion_id, created, {}, "stop") + b"data: [DONE]\n\n")
        await writer.drain()
        return sent

    async def _write_json(
        self, writer: asyncio.StreamWriter, status: int, payload: dict[str, Any],
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        await self._write_raw(writer, status, "application/json; charset=utf-8", body)

    async def _write_raw(
        self, writer: asyncio.StreamWriter, status: int, content_type: str, body: bytes,
    ) -> None:
        reason = _REASONS.get(status, "OK")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(head + body)
        await writer.drain()


def _error(message: str, kind: str = "invalid_request_error") -> dict[str, Any]:
    return {"error": {"message": message, "type": kind}}


__all__ = [
    "AgentEndpoint",
    "BadRequest",
    "DEFAULT_PORT",
    "EMPTY_REPLY",
    "HttpRequest",
    "MAX_LENS_CHARS",
    "TOKEN_PATH",
    "agent_token",
    "last_user_text",
    "lens_text",
    "read_http_request",
]
