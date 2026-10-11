"""Notification source fed by JOAT.app's Accessibility watcher.

The app reads rendered banners (the only place title/subtitle/body actually
exist — see channels/biome.py for why the system store cannot supply them)
and appends one JSON object per notification. This tails that file.

The source reports a missing/unreadable file as unhealthy so the mux can expose
the coverage loss and retry independently. It tolerates partial final lines
(writer mid-append) and malformed individual lines.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from .notifications import Notification

log = logging.getLogger(__name__)

DEFAULT_PATH = Path(
    "~/Library/Application Support/JOAT/notifications.jsonl"
).expanduser()
INITIAL_LOOKBACK_BYTES = 256 * 1024
INITIAL_RECENCY_SECONDS = 5.0


class JsonlNotificationStore:
    """Cursor binds a byte offset to one observed file generation."""

    def __init__(
        self,
        path: Path = DEFAULT_PATH,
        *,
        require_heartbeat: bool = True,
        heartbeat_max_age_s: float = 5.0,
    ) -> None:
        self._path = path
        self._heartbeat_path = path.with_name("notifications.heartbeat")
        self._require_heartbeat = require_heartbeat
        self._heartbeat_max_age_s = heartbeat_max_age_s
        self._last_parse_failures = 0

    @property
    def available(self) -> bool:
        return self._path.exists()

    def baseline(self) -> dict[str, object]:
        """Scan only a small recent tail so startup cannot lose a fresh line."""
        self._require_live_heartbeat()
        try:
            info = self._path.stat()
        except OSError as exc:
            raise FileNotFoundError(f"no Accessibility banner feed at {self._path}") from exc
        cursor = self._cursor(
            info,
            offset=max(0, info.st_size - INITIAL_LOOKBACK_BYTES),
            generation=self._generation(info),
        )
        cursor["initial_scan_end"] = info.st_size
        cursor["initial_cutoff"] = time.time() - INITIAL_RECENCY_SECONDS
        return cursor

    def read_new(self, watermark: Any) -> tuple[list[Notification], Any]:
        self._require_live_heartbeat()
        try:
            info = self._path.stat()
            size = info.st_size
        except OSError as exc:
            raise FileNotFoundError(
                f"Accessibility banner feed disappeared at {self._path}"
            ) from exc

        if isinstance(watermark, dict):
            offset = int(watermark.get("offset", 0))
            generation = str(watermark.get("generation") or "")
            prior_device = int(watermark.get("device", -1))
            prior_inode = int(watermark.get("inode", -1))
            prior_anchor = str(watermark.get("anchor") or "")
            initial_scan_end = int(watermark.get("initial_scan_end", -1))
            initial_cutoff = float(watermark.get("initial_cutoff", 0.0))
        else:
            # Backward-compatible upgrade from the former integer cursor.
            offset = int(watermark)
            generation = self._generation(info)
            prior_device, prior_inode = info.st_dev, info.st_ino
            prior_anchor = self._anchor(offset) if offset <= size else ""
            initial_scan_end = -1
            initial_cutoff = 0.0

        identity_changed = prior_device != info.st_dev or prior_inode != info.st_ino
        prefix_changed = offset > 0 and offset <= size and prior_anchor != self._anchor(offset)
        if size < offset or identity_changed or prefix_changed:
            reason = ("shrank" if size < offset
                      else "replaced" if identity_changed else "rewritten")
            offset = 0
            generation = self._generation(info)
            # Everything already in the file at our last read was read then,
            # or is history: re-reading from the start must never deliver it
            # again. On 2026-10-11 a reset re-read the whole feed, 1,130
            # banners from 46 days back, at startup. They were held back as
            # too stale, but the drain froze JOAT for 9.7 s.
            seen_ns = watermark.get("mtime_ns") if isinstance(watermark, dict) else None
            if isinstance(seen_ns, int) and seen_ns > 0:
                initial_scan_end = size
                initial_cutoff = max(initial_cutoff, seen_ns / 1e9 - INITIAL_RECENCY_SECONDS)
            log.info(
                "Accessibility banner feed %s under the cursor; re-reading it "
                "without the lines already seen",
                reason,
            )
        if size == offset:
            return [], self._cursor(info, offset=offset, generation=generation)
        with self._path.open("rb") as fh:
            fh.seek(offset)
            raw = fh.read(size - offset)

        # A trailing fragment means the writer is mid-append; leave it for
        # the next poll by advancing past only newline-terminated bytes.
        consumed = raw.rfind(b"\n") + 1
        complete = raw[:consumed].splitlines(keepends=True)

        out: list[Notification] = []
        parse_failures = 0
        line_offset = 0
        for raw_line in complete:
            native_offset = offset + line_offset
            line_offset += len(raw_line)
            if not raw_line.strip():
                continue
            try:
                d = json.loads(raw_line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                parse_failures += 1
                continue
            if not isinstance(d, dict):
                parse_failures += 1
                continue
            try:
                at = float(d.get("at") or time.time())
            except (TypeError, ValueError):
                parse_failures += 1
                continue
            if native_offset < initial_scan_end and at < initial_cutoff:
                continue
            out.append(Notification(
                rec_id=int(at * 1000),
                app=str(d.get("app") or ""),
                title=str(d.get("title") or ""),
                subtitle="",
                body=str(d.get("body") or ""),
                delivered_unix=at,
                native_id=f"{generation}:{native_offset}",
            ))
        self._last_parse_failures = parse_failures
        if parse_failures:
            log.warning(
                "Accessibility banner feed skipped malformed_lines=%d",
                parse_failures,
            )
        return out, self._cursor(
            info,
            offset=offset + consumed,
            generation=generation,
        )

    def health_after_read(self, healthy_state: str) -> str:
        return "parse_degraded" if self._last_parse_failures else healthy_state

    def _cursor(
        self,
        info: os.stat_result,
        *,
        offset: int,
        generation: str,
    ) -> dict[str, object]:
        return {
            "version": 1,
            "device": info.st_dev,
            "inode": info.st_ino,
            "mtime_ns": info.st_mtime_ns,
            "offset": offset,
            "anchor": self._anchor(offset),
            "generation": generation,
        }

    def _anchor(self, offset: int) -> str:
        start = max(0, offset - 64)
        with self._path.open("rb") as stream:
            stream.seek(start)
            value = stream.read(offset - start)
        return hashlib.sha256(value).hexdigest()

    def _generation(self, info: os.stat_result) -> str:
        size = info.st_size
        with self._path.open("rb") as stream:
            head = stream.read(64)
            stream.seek(max(0, size - 64))
            tail = stream.read(64)
        identity = (
            f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}:{size}"
        ).encode("ascii")
        return hashlib.sha256(identity + b"\0" + head + b"\0" + tail).hexdigest()

    def _require_live_heartbeat(self) -> None:
        if not self._require_heartbeat:
            return
        try:
            age = time.time() - self._heartbeat_path.stat().st_mtime
        except OSError as exc:
            raise FileNotFoundError(
                f"no live Accessibility watcher heartbeat at {self._heartbeat_path}"
            ) from exc
        if age > self._heartbeat_max_age_s:
            raise RuntimeError(
                f"stale Accessibility watcher heartbeat at {self._heartbeat_path}"
            )
