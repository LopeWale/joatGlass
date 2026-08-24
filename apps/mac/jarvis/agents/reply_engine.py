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
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..core.interfaces import ReplyEngine

log = logging.getLogger(__name__)

DEFAULT_MODEL = "mlx-community/Llama-3.2-3B-Instruct-4bit"

# The HUD is 576x288 monochrome with a ~2000-char upgrade cap. A reply that
# needs scrolling is a reply that failed.
SYSTEM_PROMPT = (
    "You are JOAT, a heads-up-display assistant on smart glasses. "
    "Answer in under 50 words of plain text. No markdown, no lists, "
    "no preamble — just the answer."
)


def mlx_available() -> bool:
    return importlib.util.find_spec("mlx_lm") is not None


class MlxReplyEngine(ReplyEngine):
    """Resident mlx-lm model. First reply() triggers load (and, first time
    ever, the model download) — subsequent replies reuse the loaded weights."""

    def __init__(self, model_id: str = DEFAULT_MODEL, max_tokens: int = 150) -> None:
        self._model_id = model_id
        self._max_tokens = max_tokens
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

    def _generate(self, text: str) -> str:
        from mlx_lm import generate  # lazy: optional dependency

        prompt = self._tokenizer.apply_chat_template(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            add_generation_prompt=True,
        )
        out: str = generate(
            self._model, self._tokenizer, prompt=prompt, max_tokens=self._max_tokens
        )
        return out.strip()
