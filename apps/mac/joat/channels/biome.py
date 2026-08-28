"""Biome-backed notification source — macOS 26+/27 (Darwin 27 verified).

Modern macOS retired the usernoted sqlite store; `usernoted` now writes
delivery events to Biome SEGB segment files under
~/Library/Biome/streams/restricted/Notification.Delivery/local (verified by
lsof on the live daemon, then byte-level inspection).

Observed record payload (protobuf), per notification:
    field 1 (len-delimited): display text (title, or the app's identifier)
    field 2 (len-delimited): bundle id, e.g. com.apple.BTUserNotifications
    field 3 (fixed64):       delivery time, unix seconds as double

Rather than depend on Apple's undocumented segment framing, the reader
scans for that three-field shape anywhere in the file and validates each
candidate (printable text, reverse-DNS-ish bundle, sane timestamp). Junk
between records is skipped by construction; framing changes degrade to
missed records, never crashes. Files are only ever read.
"""

from __future__ import annotations

import logging
import re
import struct
import time
from pathlib import Path
from typing import Any

from .notifications import Notification

log = logging.getLogger(__name__)

DEFAULT_STREAM_DIR = Path(
    "~/Library/Biome/streams/restricted/Notification.Delivery/local"
).expanduser()

_BUNDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{2,120}$")


def _read_varint(data: bytes, i: int) -> tuple[int, int] | None:
    value = shift = 0
    while i < len(data) and shift <= 28:
        b = data[i]
        value |= (b & 0x7F) << shift
        i += 1
        if not b & 0x80:
            return value, i
        shift += 7
    return None


def _parse_record_entries(data: bytes) -> list[tuple[str, str, float, int]]:
    out: list[tuple[str, str, float, int]] = []
    i = 0
    now = time.time()
    while (i := data.find(b"\x0a", i)) != -1:
        candidate = _try_record(data, i, now)
        if candidate is not None:
            record, end = candidate
            out.append((*record, i))
            i = end
        else:
            i += 1
    return out


def parse_records(data: bytes) -> list[tuple[str, str, float]]:
    """(text, bundle_id, unix_seconds) for every record shape in the blob."""
    return [(text, bundle, timestamp) for text, bundle, timestamp, _ in _parse_record_entries(data)]


def _try_record(data: bytes, i: int, now: float) -> tuple[tuple[str, str, float], int] | None:
    v = _read_varint(data, i + 1)
    if v is None:
        return None
    tlen, j = v
    if tlen == 0 or tlen > 1024 or j + tlen >= len(data):
        return None
    try:
        text = data[j:j + tlen].decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not text.isprintable():
        return None
    j += tlen
    if j >= len(data) or data[j] != 0x12:
        return None
    v = _read_varint(data, j + 1)
    if v is None:
        return None
    blen, j = v
    if blen == 0 or blen > 160 or j + blen >= len(data):
        return None
    try:
        bundle = data[j:j + blen].decode("ascii")
    except UnicodeDecodeError:
        return None
    if not _BUNDLE_RE.match(bundle) or "." not in bundle:
        return None
    j += blen
    if j + 9 > len(data) or data[j] != 0x19:
        return None
    ts = struct.unpack("<d", data[j + 1:j + 9])[0]
    # Sanity: a delivery time within (2015, now + 1 day) in unix seconds.
    if not (1.4e9 < ts < now + 86_400):
        return None
    return (text, bundle, ts), j + 9


class BiomeNotificationStore:
    """Read Biome records with a timestamp plus exact-record cursor."""

    def __init__(self, stream_dir: Path = DEFAULT_STREAM_DIR) -> None:
        self._dir = stream_dir

    def baseline(self) -> dict[str, object]:
        """Start delivering only notifications newer than 'now'."""
        if not self._dir.is_dir():
            raise FileNotFoundError(f"no Biome notification stream at {self._dir}")
        next(self._dir.iterdir(), None)  # surfaces TCC denial as PermissionError
        return {"timestamp": time.time(), "seen_at_timestamp": []}

    def read_new(self, watermark: Any) -> tuple[list[Notification], Any]:
        if isinstance(watermark, dict):
            timestamp = float(watermark.get("timestamp", 0.0))
            seen_at_timestamp = {
                str(value) for value in watermark.get("seen_at_timestamp", [])
            }
        else:
            # Backward-compatible upgrade from the former scalar cursor.
            timestamp = float(watermark)
            seen_at_timestamp = set()
        fresh: list[Notification] = []
        newest = timestamp
        seen_at_newest = set(seen_at_timestamp)
        for segment in sorted(self._dir.iterdir()):
            if not segment.is_file():
                continue
            if segment.stat().st_mtime < timestamp - 60:
                continue  # untouched since before the watermark
            for text, bundle, ts, byte_offset in _parse_record_entries(segment.read_bytes()):
                native_id = f"{segment.name}:{byte_offset}"
                if ts > timestamp or (ts == timestamp and native_id not in seen_at_timestamp):
                    fresh.append(Notification(
                        rec_id=int(ts * 1000), app=bundle, title=text,
                        subtitle="", body="", delivered_unix=ts,
                        native_id=native_id,
                    ))
                    if ts > newest:
                        newest = ts
                        seen_at_newest = {native_id}
                    elif ts == newest:
                        seen_at_newest.add(native_id)
        fresh.sort(key=lambda n: n.delivered_unix)
        return fresh, {
            "timestamp": newest,
            "seen_at_timestamp": sorted(seen_at_newest),
        }
