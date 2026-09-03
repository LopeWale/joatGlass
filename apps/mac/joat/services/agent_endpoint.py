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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.interfaces import ReplyEngine

log = logging.getLogger(__name__)

DEFAULT_PORT = 8788
MAX_BODY_BYTES = 64 * 1024
MAX_LENS_CHARS = 600
REQUEST_TIMEOUT_S = 90.0
TOKEN_PATH = Path.home() / "Library/Application Support/JOAT/agent-token"
EMPTY_REPLY = "I didn't get an answer for that — say it again."

_REASONS = {
    200: "OK", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found",
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


def last_user_text(body: dict[str, Any]) -> str | None:
    """The newest user turn of an OpenAI-shaped request, as plain text."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
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
    return None


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
        try:
            answer = await asyncio.wait_for(
                self._collect(query), timeout=self._request_timeout_s,
            )
        except asyncio.TimeoutError:
            log.warning("agent request timed out after %.0fs", self._request_timeout_s)
            await self._write_json(writer, 504, _error("the assistant took too long", "timeout"))
            return
        text = lens_text(answer, self._max_lens_chars) or EMPTY_REPLY
        log.info(
            "agent request chars=%d reply_chars=%d ms=%d stream=%s",
            len(query), len(text), int((time.monotonic() - started) * 1000), wants_stream,
        )
        if wants_stream:
            await self._write_sse(writer, text)
        else:
            await self._write_json(writer, 200, self._completion(text))

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

    async def _write_sse(self, writer: asyncio.StreamWriter, text: str) -> None:
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())

        def chunk(delta: dict[str, Any], finish: str | None) -> bytes:
            payload = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": self._model_label(),
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return f"data: {json.dumps(payload)}\n\n".encode()

        body = (
            chunk({"role": "assistant", "content": text}, None)
            + chunk({}, "stop")
            + b"data: [DONE]\n\n"
        )
        await self._write_raw(writer, 200, "text/event-stream", body)

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
