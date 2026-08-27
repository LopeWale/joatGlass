"""OpenClaw as a debate participant — the third collaborator.

The wearer's requirement, verbatim: the debate "can integrate with an
existing open claw ... as the third collaborator". OpenClaw runs as a local
gateway daemon and exposes an OpenAI-compatible streaming endpoint, so it
satisfies the same contract the codex and claude agents do:

    stream(prompt) -> AsyncIterator[str]

which is the whole reason a third voice costs a file rather than a rewrite.

Transport choice (verified against openclaw's own docs): the gateway
multiplexes several APIs on one port, and `POST /v1/chat/completions` with
`stream: true` is the simplest with real token streaming — Server-Sent
Events, `data: {json}` lines terminated by `data: [DONE]`. The WebSocket
protocol (`chat.send` + `session.message` deltas) is richer, offering
sessions and mid-run steering, and is the natural upgrade if the round
table ever needs to interrupt a speaker.

Auth is the gateway's own token, never a reused model credential — RULES.md
#9 is about not laundering OAuth tokens through third-party clients, and
this talks to the wearer's own daemon on loopback.

Absence is normal: OpenClaw is optional. `available()` says whether the
daemon is reachable, and the round table simply seats fewer participants
when it is not.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterator

from ..services.roundtable import failure_notice

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:18789"
DEFAULT_MODEL = "openclaw/default"


class OpenClawAgent:
    """A debate seat backed by a local OpenClaw gateway."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str = DEFAULT_MODEL,
        token: str | None = None,
        timeout_s: float = 240.0,
        system: str | None = None,
    ) -> None:
        self._base_url = (base_url or os.environ.get("OPENCLAW_BASE_URL")
                          or DEFAULT_BASE_URL).rstrip("/")
        self._model = model
        self._token = token or os.environ.get("OPENCLAW_GATEWAY_TOKEN") or ""
        self._timeout_s = timeout_s
        self._system = system

    # ── availability ─────────────────────────────────────────────────────

    def available(self, timeout_s: float = 1.5) -> bool:
        """True when the gateway answers its liveness probe.

        Checked at wiring time so a missing daemon costs one fast probe, not
        a 240s timeout in the middle of a debate the wearer is watching.
        """
        req = urllib.request.Request(f"{self._base_url}/healthz", method="GET")
        self._authorize(req)
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return bool(200 <= resp.status < 300)
        except Exception:
            return False

    # ── participant contract ─────────────────────────────────────────────

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        """Yield the reply in chunks as the gateway produces them."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def worker() -> None:
            try:
                for chunk in self._sse_chunks(prompt):
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
            except urllib.error.HTTPError as exc:
                # Surface the reason as TEXT: a debate turn that fails should
                # say why on the transcript, not vanish or kill the session.
                detail = "unauthorized — check OPENCLAW_GATEWAY_TOKEN" \
                    if exc.code in (401, 403) else f"HTTP {exc.code}"
                loop.call_soon_threadsafe(
                    queue.put_nowait, failure_notice("openclaw", detail))
            except Exception as exc:
                log.warning("openclaw stream failed: %s", exc)
                loop.call_soon_threadsafe(
                    queue.put_nowait, failure_notice("openclaw", str(exc)))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        done = loop.run_in_executor(None, worker)
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            yield chunk
        await done

    # ── internals ────────────────────────────────────────────────────────

    def _authorize(self, req: urllib.request.Request) -> None:
        if self._token:
            req.add_header("Authorization", f"Bearer {self._token}")

    def _sse_chunks(self, prompt: str) -> Iterator[str]:
        messages: list[dict[str, str]] = []
        if self._system:
            messages.append({"role": "system", "content": self._system})
        messages.append({"role": "user", "content": prompt})
        body = json.dumps({
            "model": self._model,
            "stream": True,
            "messages": messages,
        }).encode()
        req = urllib.request.Request(
            f"{self._base_url}/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        self._authorize(req)
        with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
            for raw in resp:
                text = raw.decode("utf-8", "replace").strip()
                if not text.startswith("data:"):
                    continue  # SSE comments and blank separators
                payload = text[5:].strip()
                if payload == "[DONE]":
                    return
                piece = _delta_text(payload)
                if piece:
                    yield piece


def _delta_text(payload: str) -> str:
    """The text of one SSE frame, or '' for frames that carry none.

    Tolerant by design: a frame we cannot parse is a dropped token, never a
    failed debate turn.
    """
    try:
        frame = json.loads(payload)
    except json.JSONDecodeError:
        return ""
    choices = frame.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    delta = first.get("delta")
    if isinstance(delta, dict):
        content = delta.get("content")
        if isinstance(content, str):
            return content
    # Non-streaming servers answer with `message` instead of `delta`.
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
    return ""
