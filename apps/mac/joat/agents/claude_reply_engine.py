"""The mind on a frontier model: Claude, with the brain's tools in hand.

The split this file exists to keep honest (grounding.py: "the deterministic
runtime supplies what it knows, the mind reasons"): the BRAIN — the Mac app
around this class — owns the senses, the hands and the access: the glasses
gateway, iMessage, calendar, notifications, search, navigation, persistence
and the confirmation gates. The MIND is only the model plus the loop that
decides what to say and which tool to ask the brain to run. Until now that
mind was a 3B local model told to decline anything outside its fact block,
so it declined jokes and Hamlet (measured 2026-09-02: "I do not have a
joke.", in 0.34 s). This is the same seat, filled by Claude.

Only the official Anthropic SDK, on a metered API key or an `ant auth login`
profile — RULES.md #9 keeps Claude Code's own subscription tokens out of
every other client, and the key never touches the `claude` binary either
way. Streaming, one tool round-trip at a time, and a reply sized for a
576x288 display. When the API cannot be reached the resident MLX engine
answers instead, so an outage degrades the mind rather than silencing it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from ..core.interfaces import ReplyEngine
from .grounding import Grounding
from .tools import ToolBelt, ToolCall

log = logging.getLogger(__name__)

DEFAULT_AGENT_MODEL = "claude-opus-5"
FIRST_CHUNK_CHARS = 8
CHUNK_CHARS = 64
MAX_TOOL_ROUNDS = 6
DEFAULT_MAX_TOKENS = 600
DEFAULT_EFFORT = "low"
API_KEY_PATH = Path.home() / "Library/Application Support/JOAT/anthropic-api-key"

PERSONA = (
    "You are JOAT, the wearer's personal assistant, speaking through the "
    "display of their Even Realities G2 smart glasses.\n"
    "\n"
    "How to answer:\n"
    "- Answer the actual question, fully, from your own knowledge. Jokes, "
    "facts, explanations, advice, arithmetic: just answer.\n"
    "- Use a tool when the answer depends on something live or personal: "
    "the weather, the wearer's location and nearby places, their "
    "notifications and messages, the news, a web search for recent events, "
    "their focus timer. Call it without narrating, then answer.\n"
    "- If a tool fails or returns nothing, say in one short clause what you "
    "could not get, and still give the best answer you can.\n"
    "- Never invent the wearer's personal data: their messages, calendar, "
    "location, or device state. The facts block says what you have.\n"
    "\n"
    "How to speak:\n"
    "- Plain text only: no markdown, no lists, no headings, no emoji.\n"
    "- Short. The display shows about four lines. Default to one or two "
    "sentences, at most about 60 words. Give more only when the wearer "
    "asks for detail.\n"
    "- Direct and warm. No preamble, no disclaimers about being an AI, no "
    "restating the question."
)

UNAVAILABLE_NOTICE = (
    "JOAT's model is not signed in on the Mac — add the Anthropic API key."
)
FAILED_NOTICE = "The model call failed — say it again in a moment."
REFUSAL_NOTICE = "I can't help with that one."
EXHAUSTED_NOTICE = "I couldn't finish that — it needed too many lookups."

ModelProvider = Callable[[], str | None]


def _text_delta(event: Any) -> str | None:
    """The text carried by a streaming event, or None for every other event."""
    if getattr(event, "type", None) != "content_block_delta":
        return None
    delta = getattr(event, "delta", None)
    if getattr(delta, "type", None) != "text_delta":
        return None
    text = getattr(delta, "text", "")
    return str(text) if text else None


def _short(exc: BaseException) -> str:
    """An SDK error message, single-line and bounded. Never wearer content."""
    return " ".join(str(exc).split())[:160]


def _is_missing_credentials(exc: BaseException) -> bool:
    """The SDK raises a bare TypeError, per request, when it finds no key,
    token or profile — observed live 2026-09-03: "Could not resolve
    authentication method." That is a configuration state, not a fault."""
    return isinstance(exc, TypeError) and "authentication method" in str(exc)


def _is_auth_error(exc: BaseException) -> bool:
    try:
        import anthropic  # lazy: optional at import time
    except ImportError:
        return False
    return isinstance(exc, anthropic.AuthenticationError)


def load_api_key(path: Path = API_KEY_PATH, env: str = "ANTHROPIC_API_KEY") -> str | None:
    """The metered key, from the environment or a 0600 file under the app's
    support directory (the installed JOAT.app has no shell profile to read)."""
    configured = os.environ.get(env, "").strip()
    if configured:
        return configured
    try:
        stored = path.read_text().strip()
    except OSError:
        return None
    return stored or None


class ClaudeReplyEngine(ReplyEngine):
    """Claude as the fast-path mind: transcript in, lens-sized reply out.

    The model is read PER TURN through ``model`` when a provider is given,
    so "use sonnet for the assistant" lands on the next question, the same
    way the Brainstorm seats switch (services/models.py).
    """

    HISTORY_TTL_S = 1800.0
    HISTORY_EXCHANGES = 10

    def __init__(
        self,
        model: str | ModelProvider = DEFAULT_AGENT_MODEL,
        *,
        grounding: Grounding | None = None,
        tools: ToolBelt | None = None,
        client: Any | None = None,
        fallback: ReplyEngine | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        effort: str = DEFAULT_EFFORT,
        chunk_chars: int = CHUNK_CHARS,
        first_chunk_chars: int = FIRST_CHUNK_CHARS,
        api_key: str | None = None,
        on_ready: Callable[[], None] | None = None,
    ) -> None:
        if chunk_chars <= 0 or first_chunk_chars <= 0:
            raise ValueError("reply chunk sizes must be positive")
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        self._model_source = model
        self._grounding = grounding
        self._tools = tools
        self._client = client
        self._fallback = fallback
        self._max_tokens = max_tokens
        self._effort = effort
        self._chunk_chars = chunk_chars
        self._first_chunk_chars = first_chunk_chars
        self._api_key = api_key
        self._on_ready = on_ready
        self._client_failed = False
        self._key_tried: str | None = None
        self._ready = False
        # One turn at a time: the voice path and the Even AI door share this
        # mind, and its working memory is not safe to interleave.
        self._lock = asyncio.Lock()
        self._history: list[tuple[float, str, str]] = []

    # ── model and client ────────────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        """Whether a boot-time probe reached the API with these credentials."""
        return self._ready

    def model(self) -> str:
        source = self._model_source
        chosen = source() if callable(source) else source
        return chosen or DEFAULT_AGENT_MODEL

    def _client_or_none(self) -> Any | None:
        if self._client is not None:
            return self._client
        key = self._api_key or load_api_key()
        if self._client_failed and key == self._key_tried:
            # Credentials were missing or refused, and nothing has changed.
            # A key written to the support file after boot is picked up
            # here without a restart; the same wrong key is not retried.
            return None
        self._key_tried = key
        try:
            from anthropic import AsyncAnthropic  # lazy: keeps import cost off boot

            self._client = AsyncAnthropic(api_key=key) if key else AsyncAnthropic()
            self._client_failed = False
        except Exception as exc:
            self._client_failed = True
            log.warning(
                "Claude client unavailable (%s) — set ANTHROPIC_API_KEY, write it to "
                "%s, or run `ant auth login`", type(exc).__name__, API_KEY_PATH,
            )
            return None
        return self._client

    async def warm(self) -> bool:
        """Probe the API once at boot so the dashboard can say 'ready'.

        Opportunistic: a failure here is logged and the first real turn
        retries, exactly like the MLX engine's warm-up.
        """
        client = self._client_or_none()
        if client is None:
            return False
        try:
            await client.with_options(max_retries=0, timeout=15.0).models.retrieve(
                self.model()
            )
        except Exception as exc:
            self._note_failure(exc)
            return False
        self._ready = True
        if self._on_ready is not None:
            self._on_ready()
        return True

    def _note_failure(self, exc: BaseException) -> None:
        if _is_missing_credentials(exc) or _is_auth_error(exc):
            # Not a transient fault: stop dialling until the credentials
            # change. The fallback engine answers meanwhile, and the notice
            # on the lens says what to fix.
            self._client_failed = True
            self._client = None
            self._ready = False
            log.error(
                "Claude cannot be used (%s) — set ANTHROPIC_API_KEY, write the "
                "key to %s, or run `ant auth login`", _short(exc), API_KEY_PATH,
            )
            return
        log.warning("assistant model call failed (%s: %s)", type(exc).__name__, _short(exc))

    # ── the reply contract ─────────────────────────────────────────────────

    async def reply(self, text: str) -> str | None:
        parts = [chunk async for chunk in self.stream(text)]
        return "".join(parts) or None

    async def stream(self, text: str) -> AsyncIterator[str]:
        """True token streaming across the whole tool loop.

        Text arrives in a short first chunk and then ~chunk_chars per HUD
        append. Tool calls run between model turns; whatever the model said
        before calling a tool is shown too, so a slow lookup never reads as
        a dead assistant.
        """
        if not text.strip():
            return
        async with self._lock:
            client = self._client_or_none()
            if client is None:
                async for chunk in self._degraded(text, UNAVAILABLE_NOTICE):
                    yield chunk
                return
            messages: list[dict[str, Any]] = [
                *self._recent_turns(),
                {"role": "user", "content": text},
            ]
            spoken: list[str] = []
            rounds_left = MAX_TOOL_ROUNDS
            while True:
                buf = ""
                threshold = (
                    self._chunk_chars if spoken
                    else min(self._first_chunk_chars, self._chunk_chars)
                )
                try:
                    async with client.messages.stream(**self._request(messages)) as stream:
                        async for event in stream:
                            piece = _text_delta(event)
                            if piece is None:
                                continue
                            buf += piece
                            while len(buf) >= threshold:
                                chunk, buf = buf[:threshold], buf[threshold:]
                                spoken.append(chunk)
                                yield chunk
                                threshold = self._chunk_chars
                        final = await stream.get_final_message()
                except Exception as exc:
                    self._note_failure(exc)
                    if buf:
                        spoken.append(buf)
                        yield buf
                    if not spoken:
                        async for chunk in self._degraded(text, FAILED_NOTICE):
                            yield chunk
                    return
                if buf:
                    spoken.append(buf)
                    yield buf
                stop = getattr(final, "stop_reason", None)
                content = list(getattr(final, "content", None) or [])
                tool_uses = [b for b in content if getattr(b, "type", "") == "tool_use"]
                if stop == "tool_use" and tool_uses:
                    if rounds_left == 0:
                        if not spoken:
                            spoken.append(EXHAUSTED_NOTICE)
                            yield EXHAUSTED_NOTICE
                        break
                    rounds_left -= 1
                    messages.append({"role": "assistant", "content": content})
                    results = await asyncio.gather(*(self._run_tool(b) for b in tool_uses))
                    messages.append({"role": "user", "content": list(results)})
                    continue
                if stop == "pause_turn":
                    messages.append({"role": "assistant", "content": content})
                    continue
                if stop == "refusal" and not spoken:
                    spoken.append(REFUSAL_NOTICE)
                    yield REFUSAL_NOTICE
                break
            if spoken:
                self.remember(text, "".join(spoken))

    async def complete(
        self, *, system: str, text: str, max_tokens: int = 700
    ) -> str | None:
        """One task completion: no persona, no history, no tools.

        The shape ``services/note_insights.py`` expects, so a summary can be
        routed here when the wearer explicitly asks for Claude.
        """
        if not text.strip() or not system.strip():
            return None
        async with self._lock:
            client = self._client_or_none()
            if client is None:
                return None
            request: dict[str, Any] = {
                "model": self.model(),
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": text}],
            }
            try:
                async with client.messages.stream(**request) as stream:
                    final = await stream.get_final_message()
            except Exception as exc:
                self._note_failure(exc)
                return None
        parts = [
            str(getattr(b, "text", ""))
            for b in list(getattr(final, "content", None) or [])
            if getattr(b, "type", "") == "text"
        ]
        result = "".join(parts).strip()
        return result or None

    # ── working memory ──────────────────────────────────────────────────────

    def remember(self, asked: str, answered: str) -> None:
        self._history.append((time.monotonic(), asked, answered))

    def _recent_turns(self) -> list[dict[str, Any]]:
        cutoff = time.monotonic() - self.HISTORY_TTL_S
        self._history = [h for h in self._history if h[0] >= cutoff]
        turns: list[dict[str, Any]] = []
        for _, asked, answered in self._history[-self.HISTORY_EXCHANGES:]:
            turns.append({"role": "user", "content": asked})
            turns.append({"role": "assistant", "content": answered})
        return turns

    # ── request assembly ────────────────────────────────────────────────────

    def _live_context(self) -> str:
        if self._grounding is None:
            return ""
        parts: list[str] = []
        facts = self._grounding.facts()
        if facts:
            parts.append(f"Facts you can rely on right now:\n{facts}")
        gaps = self._grounding.cannot_know()
        if gaps:
            parts.append(
                "You have no source for these and must not invent them — say "
                f"you do not have them: {gaps}."
            )
        return "\n\n".join(parts)

    def _system_blocks(self) -> list[dict[str, Any]]:
        # The persona is byte-stable so the prefix caches; the clock and the
        # device facts change every turn and ride in a second, uncached block.
        blocks: list[dict[str, Any]] = [
            {"type": "text", "text": PERSONA, "cache_control": {"type": "ephemeral"}},
        ]
        live = self._live_context()
        if live:
            blocks.append({"type": "text", "text": live})
        return blocks

    def _request(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        model = self.model()
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": self._max_tokens,
            "system": self._system_blocks(),
            "messages": messages,
        }
        # Effort is a Claude 4.6+ control; the Haiku 4.5 generation rejects it.
        if not model.startswith("claude-haiku"):
            request["output_config"] = {"effort": self._effort}
        if self._tools is not None and self._tools:
            request["tools"] = self._tools.anthropic_tools()
        return request

    async def _run_tool(self, block: Any) -> dict[str, Any]:
        name = str(getattr(block, "name", "") or "")
        raw = getattr(block, "input", None)
        arguments = dict(raw) if isinstance(raw, dict) else {}
        if self._tools is None:
            result = f"error: no tool named {name!r}"
        else:
            result = await self._tools.execute(ToolCall(name=name, arguments=arguments))
        out: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": str(getattr(block, "id", "") or ""),
            "content": result,
        }
        if result.startswith("error:"):
            out["is_error"] = True
        return out

    async def _degraded(self, text: str, notice: str) -> AsyncIterator[str]:
        """The fallback engine's answer, or an honest notice when there is none."""
        if self._fallback is not None:
            spoke = False
            try:
                async for chunk in self._fallback.stream(text):
                    spoke = True
                    yield chunk
            except Exception:
                log.exception("fallback reply engine failed")
            if spoke:
                return
        yield notice


__all__ = [
    "API_KEY_PATH",
    "DEFAULT_AGENT_MODEL",
    "EXHAUSTED_NOTICE",
    "FAILED_NOTICE",
    "PERSONA",
    "REFUSAL_NOTICE",
    "UNAVAILABLE_NOTICE",
    "ClaudeReplyEngine",
    "load_api_key",
]
