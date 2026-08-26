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

import asyncio
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
            "last_transcript": "",
            "last_reply": "",
            "last_card": "",
            # "session" and counts.debates were dead schema — written into
            # every snapshot, decoded by the shell, displayed by nothing,
            # and updated by nothing (audit finding 26).
            "counts": {"utterances": 0, "cards": 0},
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

    async def heartbeat(self, interval_s: float = 5.0) -> None:
        """Re-stamp the snapshot on a timer.

        set()/bump() fire only on EVENTS, but the shell declares a snapshot
        stale after 15s and blanks every field — so a healthy idle engine
        looked identical to a dead one within 15 seconds of the last
        utterance, while the (ungated) counters kept ticking. Interval must
        stay well under that window.
        """
        while True:
            await asyncio.sleep(interval_s)
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
