"""Engine → shell status channel.

The Mac app needs to render what the assistant is DOING (glasses link, mic,
notification channel, last exchange) — not a log tail. The engine publishes
a small JSON snapshot; the app polls it.

A file, not a socket: it needs no second port, survives either side
restarting independently, and a stale file is self-evidently stale via its
`at` timestamp. Writes are atomic (tmp + replace) so the reader can never
observe a half-written snapshot.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(
    "~/Library/Application Support/JOAT/status.json"
).expanduser()


class StatusPublisher:
    """Mutable snapshot of engine state, flushed on every change."""

    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self._path = path
        self._state: dict[str, Any] = {
            "at": time.time(),
            "glasses": "disconnected",
            "device": "",
            "mic": "off",
            "notifications": "off",
            "stt": "off",
            "llm": "off",
            "debate": "off",
            "session": "idle",
            "last_transcript": "",
            "last_reply": "",
            "last_card": "",
            "counts": {"utterances": 0, "cards": 0, "debates": 0},
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.flush()

    def set(self, **fields: Any) -> None:
        self._state.update(fields)
        self.flush()

    def bump(self, counter: str) -> None:
        counts = dict(self._state["counts"])
        counts[counter] = counts.get(counter, 0) + 1
        self._state["counts"] = counts
        self.flush()

    def flush(self) -> None:
        self._state["at"] = time.time()
        try:
            fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
            with os.fdopen(fd, "w") as fh:
                json.dump(self._state, fh)
            os.replace(tmp, self._path)  # atomic for the reader
        except OSError:
            pass  # status is best-effort; never take the engine down for it
