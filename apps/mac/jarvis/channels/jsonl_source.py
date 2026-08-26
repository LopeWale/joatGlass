"""Notification source fed by JOAT.app's Accessibility watcher.

The app reads rendered banners (the only place title/subtitle/body actually
exist — see channels/biome.py for why the system store cannot supply them)
and appends one JSON object per notification. This tails that file.

Tolerates: the file not existing yet (app not running / no Accessibility
grant), partial final lines (writer mid-append), and malformed lines.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from .notifications import Notification

log = logging.getLogger(__name__)

DEFAULT_PATH = Path(
    "~/Library/Application Support/JOAT/notifications.jsonl"
).expanduser()


class JsonlNotificationStore:
    """Watermark is a byte offset — cheap, and immune to clock weirdness."""

    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self._path = path

    @property
    def available(self) -> bool:
        return self._path.exists()

    def baseline(self) -> int:
        """Start at EOF: never replay a backlog as if it were fresh."""
        try:
            return self._path.stat().st_size
        except OSError:
            return 0

    def read_new(self, watermark: Any) -> tuple[list[Notification], Any]:
        offset = int(watermark)
        try:
            size = self._path.stat().st_size
        except OSError:
            return [], offset
        if size < offset:  # file was truncated/rotated — resync
            offset = 0
        if size == offset:
            return [], offset
        try:
            with self._path.open("rb") as fh:
                fh.seek(offset)
                raw = fh.read(size - offset)
        except OSError:
            return [], offset

        text = raw.decode("utf-8", errors="replace")
        lines = text.split("\n")
        # A trailing fragment means the writer is mid-append; leave it for
        # the next poll by rewinding the watermark past only whole lines.
        complete = lines[:-1]
        consumed = len(text) - len(lines[-1])

        out: list[Notification] = []
        for line in complete:
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            at = float(d.get("at") or time.time())
            out.append(Notification(
                rec_id=int(at * 1000),
                app=str(d.get("app") or ""),
                title=str(d.get("title") or ""),
                subtitle="",
                body=str(d.get("body") or ""),
                delivered_unix=at,
            ))
        # Advance by the BYTE length of whole lines only.
        return out, offset + len(text[:consumed].encode("utf-8"))
