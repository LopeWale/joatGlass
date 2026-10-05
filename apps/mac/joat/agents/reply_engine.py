"""Fast-path reply engine on Apple MLX (MAC_APP.md: question → local MLX → HUD).

mlx-lm loads the model ONCE and keeps it resident, so replies have no
per-utterance cold start (unlike the whisperkit-cli shell-out). The import is
lazy: machines without the `mlx` extra installed still run the brain — the
orchestrator just echoes transcripts.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..core.interfaces import ReplyEngine
from .grounding import Grounding
from .tools import ToolBelt

log = logging.getLogger(__name__)

DEFAULT_MODEL = "mlx-community/Llama-3.2-3B-Instruct-4bit"
FIRST_CHUNK_CHARS = 8

# The HUD is 576x288 monochrome with a ~2000-char upgrade cap. A reply that
# needs scrolling is a reply that failed.
SYSTEM_PROMPT = (
    "You are JOAT, a heads-up-display assistant on smart glasses. "
    "Answer in under 50 words of plain text. No markdown, no lists, "
    "no preamble — just the answer."
)

# Attached ONLY alongside an actual fact block — a standing reference to
# "the facts below" with no facts below is a dangling pointer the model
# resolves by inventing the block.
FACTS_PREAMBLE = (
    "Use these facts when they answer the question. If something is not "
    "in them and no tool can get it, say you do not have it rather than "
    "guessing."
)


def mlx_available() -> bool:
    return importlib.util.find_spec("mlx_lm") is not None


class MlxReplyEngine(ReplyEngine):
    """Resident mlx-lm model. First reply() triggers load (and, first time
    ever, the model download) — subsequent replies reuse the loaded weights."""

    def __init__(
        self, model_id: str = DEFAULT_MODEL, max_tokens: int = 150,
        chunk_chars: int = 32, first_chunk_chars: int = FIRST_CHUNK_CHARS,
        grounding: Grounding | None = None, tools: ToolBelt | None = None,
        on_loaded: Callable[[], None] | None = None,
    ) -> None:
        if chunk_chars <= 0 or first_chunk_chars <= 0:
            raise ValueError("reply chunk sizes must be positive")
        self._model_id = model_id
        self._max_tokens = max_tokens
        self._chunk_chars = chunk_chars
        self._first_chunk_chars = first_chunk_chars
        self._grounding = grounding
        self._tools = tools
        self._on_loaded = on_loaded
        # 5-minute, 3-exchange working memory. Without it "repeat that" and
        # "and tomorrow?" are answered by a model that invents a plausible
        # prior turn — an assistant with amnesia that hides its amnesia.
        self._history: list[tuple[float, str, str]] = []
        self._model: Any = None
        self._tokenizer: Any = None
        self._load_lock = asyncio.Lock()
        # MLX Metal streams are THREAD-BOUND: loading on one thread and
        # generating on another raises "There is no Stream(gpu, 1) in current
        # thread". One dedicated worker thread runs every MLX op, forever.
        self._mlx_thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mlx")

    async def _ensure_loaded(self) -> None:
        async with self._load_lock:
            if self._model is not None:
                return
            log.info("loading MLX model %s (first time may download)", self._model_id)
            loop = asyncio.get_running_loop()
            self._model, self._tokenizer = await loop.run_in_executor(
                self._mlx_thread, self._load
            )
            log.info("MLX model resident")
            if self._on_loaded is not None:
                self._on_loaded()

    async def warm(self) -> bool:
        """Load model weights ahead of the first wearer request.

        Startup warm-up is opportunistic: a temporary model/download failure
        must not stop the bridge, and the normal reply path may retry later.
        """
        try:
            await self._ensure_loaded()
        except Exception:
            log.exception("MLX model warm-up failed")
            return False
        return True

    def _load(self) -> tuple[Any, Any]:
        # Lazy: mlx_lm is an optional macOS-only dependency with no stubs;
        # availability is gated at runtime by mlx_available().
        from mlx_lm import load  # type: ignore[import-not-found]

        loaded = load(self._model_id)  # 2-tuple, or 3-tuple with config
        return loaded[0], loaded[1]

    async def reply(self, text: str) -> str | None:
        if not text.strip():
            return None
        await self._ensure_loaded()
        tool_note = await self._maybe_use_tools(text)
        answer = await asyncio.get_running_loop().run_in_executor(
            self._mlx_thread, self._generate, text, tool_note
        )
        if answer:
            self.remember(text, answer)
        return answer

    async def complete(
        self, *, system: str, text: str, max_tokens: int = 700
    ) -> str | None:
        """One task completion on the resident model — no persona, no history.

        The conversational ``reply`` path caps answers at HUD size, remembers
        turns, and may call tools; none of that belongs to summarization-class
        work over a private transcript. This shares the loaded weights and the
        one MLX thread, and touches neither grounding nor working memory.
        """
        if not text.strip() or not system.strip():
            return None
        await self._ensure_loaded()

        def run() -> str:
            from mlx_lm import generate  # lazy: optional dependency

            out: str = generate(
                self._model, self._tokenizer,
                prompt=self._tokenizer.apply_chat_template(
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": text},
                    ],
                    add_generation_prompt=True,
                ),
                max_tokens=max_tokens, verbose=False,
            )
            return out.strip()

        return await asyncio.get_running_loop().run_in_executor(self._mlx_thread, run)

    async def _maybe_use_tools(self, text: str) -> str | None:
        """DECIDE pass: offer the tools; execute whatever the model calls.

        The wearer's ruling: an assistant that stops at "I don't know" is as
        fired as one that makes the answer up — it goes and figures out what
        is needed. The model picks the tool (policy); the belt executes it
        with timeouts and error containment (mechanism).

        Decide-then-answer, empirically: the 3B emits clean calls but
        over-calls (asked the time — in its facts — it still called the
        weather tool). Splitting the passes makes a needless call a harmless
        extra fetch instead of a wrong answer: the ANSWER pass gets facts
        AND results, with no tools offered, so it must produce text.

        Returns the tool-results note for the answer pass, or None.
        """
        if self._tools is None or not self._tools:
            return None
        call = self._tools.explicit_call(text)
        if call is None:
            if not self._tools.requires_model_decision(text):
                return None
            decision = await asyncio.get_running_loop().run_in_executor(
                self._mlx_thread, self._decide, text
            )
            call = self._tools.parse_call(decision)
        if call is None:
            return None
        result = await self._tools.execute(call)
        return f"You looked this up with {call.name}: {result}"

    def _decide(self, text: str) -> str:
        from mlx_lm import generate  # lazy: optional dependency

        out: str = generate(
            self._model, self._tokenizer,
            prompt=self._tokenizer.apply_chat_template(
                [
                    {"role": "system", "content": self._system_prompt()},
                    {"role": "user", "content": text},
                ],
                tools=self._tools.schemas() if self._tools else None,
                add_generation_prompt=True,
            ),
            max_tokens=80, verbose=False,
        )
        return out

    async def stream(self, text: str) -> AsyncIterator[str]:
        """True token streaming: chunks arrive as the model produces them.

        Generation runs on the dedicated MLX thread (streams are
        thread-bound); chunks cross to the event loop via a queue. Tokens are
        batched to a short first chunk and then ~chunk_chars per HUD append —
        per-token updates would flood the visual transport.
        """
        if not text.strip():
            return
        await self._ensure_loaded()
        tool_note = await self._maybe_use_tools(text)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def worker() -> None:
            try:
                from mlx_lm import stream_generate  # lazy: optional dependency

                buf = ""
                first_chunk = True
                first_threshold = min(self._first_chunk_chars, self._chunk_chars)
                for response in stream_generate(
                    self._model, self._tokenizer,
                    prompt=self._chat_prompt(text, tool_note),
                    max_tokens=self._max_tokens,
                ):
                    buf += str(response.text)
                    if first_chunk and len(buf) >= first_threshold:
                        loop.call_soon_threadsafe(
                            queue.put_nowait, buf[:first_threshold]
                        )
                        buf = buf[first_threshold:]
                        first_chunk = False
                    while not first_chunk and len(buf) >= self._chunk_chars:
                        loop.call_soon_threadsafe(
                            queue.put_nowait, buf[:self._chunk_chars]
                        )
                        buf = buf[self._chunk_chars:]
                if buf:
                    loop.call_soon_threadsafe(queue.put_nowait, buf)
            except Exception:
                log.exception("stream_generate failed")
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        done = loop.run_in_executor(self._mlx_thread, worker)
        spoken: list[str] = []
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            spoken.append(chunk)
            yield chunk
        await done
        if spoken:
            self.remember(text, "".join(spoken))

    def _system_prompt(self) -> str:
        """Prompt plus the facts as of NOW — recomputed every reply."""
        if self._grounding is None:
            return SYSTEM_PROMPT
        parts = [SYSTEM_PROMPT]
        facts = self._grounding.facts()
        if facts:
            parts.append(f"{FACTS_PREAMBLE}\nFacts you can rely on right now:\n{facts}")
        gaps = self._grounding.cannot_know()
        if gaps:
            parts.append(
                "You have NO source for these and must never invent them — "
                f"say you do not have them: {gaps}."
            )
        return "\n\n".join(parts)

    HISTORY_TTL_S = 300.0
    HISTORY_TURNS = 3

    def _recent_turns(self) -> list[dict[str, str]]:
        cutoff = time.monotonic() - self.HISTORY_TTL_S
        self._history = [h for h in self._history if h[0] >= cutoff]
        turns: list[dict[str, str]] = []
        for _, asked, answered in self._history[-self.HISTORY_TURNS:]:
            turns.append({"role": "user", "content": asked})
            turns.append({"role": "assistant", "content": answered})
        return turns

    def remember(self, asked: str, answered: str) -> None:
        self._history.append((time.monotonic(), asked, answered))

    def _chat_prompt(self, text: str, tool_note: str | None = None) -> Any:
        system = self._system_prompt()
        if tool_note is not None:
            # Results ride the system prompt for the ANSWER pass — no tools
            # offered here, so the model must produce text, not another call.
            system = f"{system}\n\n{tool_note}"
        return self._tokenizer.apply_chat_template(
            [
                {"role": "system", "content": system},
                *self._recent_turns(),
                {"role": "user", "content": text},
            ],
            add_generation_prompt=True,
        )

    def _generate(self, text: str, tool_note: str | None = None) -> str:
        from mlx_lm import generate  # lazy: optional dependency

        out: str = generate(
            self._model, self._tokenizer,
            prompt=self._chat_prompt(text, tool_note), max_tokens=self._max_tokens,
        )
        return out.strip()
