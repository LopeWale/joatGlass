"""MiniCheckJudge — NLI claim-support via the MiniCheck fact-checker.

Installed from git (Liyan06/MiniCheck) as the optional `nli` extra. The
UNRELATED PyPI package is also named 'minicheck' (a formal-methods model
checker) — availability is therefore checked by the fact-checker's
distinctive submodule, never by the top-level name alone.

flan-t5-large (770M, ~3.1 GB weights, downloads on first load) is the
verified-best sub-1B variant. Model loads once on a dedicated thread and
stays resident; scoring is single-flight.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ..core.interfaces import ClaimJudge

log = logging.getLogger(__name__)

MAX_DOC_CHARS = 12_000  # MiniCheck chunks internally, but cap pathological pages


def minicheck_available() -> bool:
    """True only for the FACT-CHECKING MiniCheck (has minicheck.minicheck)."""
    return (
        importlib.util.find_spec("minicheck") is not None
        and importlib.util.find_spec("minicheck.minicheck") is not None
    )


class MiniCheckJudge(ClaimJudge):
    def __init__(self, model_name: str = "flan-t5-large") -> None:
        self._model_name = model_name
        self._scorer: Any = None
        self._lock = asyncio.Lock()
        self._thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="minicheck")

    async def support(self, doc: str, claim: str) -> float | None:
        if not doc.strip() or not claim.strip():
            return None
        try:
            async with self._lock:  # load-once + single-flight scoring
                loop = asyncio.get_running_loop()
                if self._scorer is None:
                    log.info("loading MiniCheck %s (first time downloads ~3.1 GB)",
                             self._model_name)
                    self._scorer = await loop.run_in_executor(self._thread, self._load)
                    log.info("MiniCheck resident")
                return await loop.run_in_executor(
                    self._thread, self._score, doc[:MAX_DOC_CHARS], claim
                )
        except Exception:
            log.exception("MiniCheck scoring failed")
            return None

    def _load(self) -> Any:
        # MiniCheck's chunking needs NLTK's punkt_tab data but its installer
        # fetches only the legacy 'punkt' — self-heal the gap here (no-op
        # when already present).
        import nltk

        nltk.download("punkt_tab", quiet=True)
        from minicheck.minicheck import MiniCheck  # lazy: optional dependency

        return MiniCheck(model_name=self._model_name)

    def _score(self, doc: str, claim: str) -> float:
        _labels, probs, _, _ = self._scorer.score(docs=[doc], claims=[claim])
        return float(probs[0])
