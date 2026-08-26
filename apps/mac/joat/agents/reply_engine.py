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
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..core.interfaces import ReplyEngine
from .grounding import Grounding

log = logging.getLogger(__name__)

DEFAULT_MODEL = "mlx-community/Llama-3.2-3B-Instruct-4bit"

# The HUD is 576x288 monochrome with a ~2000-char upgrade cap. A reply that
# needs scrolling is a reply that failed.
SYSTEM_PROMPT = (
    "You are JOAT, a heads-up-display assistant on smart glasses. "
    "Answer in under 50 words of plain text. No markdown, no lists, "
    "no preamble — just the answer. "
    # Without this the model invents facts it has no source for, and a
    # confident wrong answer on a HUD is worse than none — the wearer
    # cannot tell it was guessing.
    "Use the facts below when they answer the question. If something is "
    "not in them and you cannot know it, say you do not have it rather "
    "than guessing."
)


def mlx_available() -> bool:
    return importlib.util.find_spec("mlx_lm") is not None


class MlxReplyEngine(ReplyEngine):
    """Resident mlx-lm model. First reply() triggers load (and, first time
    ever, the model download) — subsequent replies reuse the loaded weights."""

    def __init__(
        self, model_id: str = DEFAULT_MODEL, max_tokens: int = 150, chunk_chars: int = 32,
        grounding: Grounding | None = None,
    ) -> None:
        self._model_id = model_id
        self._max_tokens = max_tokens
        self._chunk_chars = chunk_chars
        self._grounding = grounding
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

    def _load(self) -> tuple[Any, Any]:
        from mlx_lm import load  # lazy: optional dependency

        loaded = load(self._model_id)  # 2-tuple, or 3-tuple with config
        return loaded[0], loaded[1]

    async def reply(self, text: str) -> str | None:
        if not text.strip():
            return None
        await self._ensure_loaded()
        return await asyncio.get_running_loop().run_in_executor(
            self._mlx_thread, self._generate, text
        )

    async def stream(self, text: str) -> AsyncIterator[str]:
        """True token streaming: chunks arrive as the model produces them.

        Generation runs on the dedicated MLX thread (streams are
        thread-bound); chunks cross to the event loop via a queue. Tokens are
        batched to ~chunk_chars per HUD append — every append is a BLE
        textContainerUpgrade, so per-token updates would flood the link.
        """
        if not text.strip():
            return
        await self._ensure_loaded()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def worker() -> None:
            try:
                from mlx_lm import stream_generate  # lazy: optional dependency

                buf = ""
                for response in stream_generate(
                    self._model, self._tokenizer,
                    prompt=self._chat_prompt(text), max_tokens=self._max_tokens,
                ):
                    buf += str(response.text)
                    if len(buf) >= self._chunk_chars:
                        loop.call_soon_threadsafe(queue.put_nowait, buf)
                        buf = ""
                if buf:
                    loop.call_soon_threadsafe(queue.put_nowait, buf)
            except Exception:
                log.exception("stream_generate failed")
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        done = loop.run_in_executor(self._mlx_thread, worker)
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            yield chunk
        await done

    def _system_prompt(self) -> str:
        """Prompt plus the facts as of NOW — recomputed every reply."""
        if self._grounding is None:
            return SYSTEM_PROMPT
        parts = [SYSTEM_PROMPT]
        facts = self._grounding.facts()
        if facts:
            parts.append(f"Facts you can rely on right now:\n{facts}")
        gaps = self._grounding.cannot_know()
        if gaps:
            parts.append(
                "You have NO source for these and must never invent them — "
                f"say you do not have them: {gaps}."
            )
        return "\n\n".join(parts)

    def _chat_prompt(self, text: str) -> Any:
        return self._tokenizer.apply_chat_template(
            [
                {"role": "system", "content": self._system_prompt()},
                {"role": "user", "content": text},
            ],
            add_generation_prompt=True,
        )

    def _generate(self, text: str) -> str:
        from mlx_lm import generate  # lazy: optional dependency

        out: str = generate(
            self._model, self._tokenizer,
            prompt=self._chat_prompt(text), max_tokens=self._max_tokens,
        )
        return out.strip()
