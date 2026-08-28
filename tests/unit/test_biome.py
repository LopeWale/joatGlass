from __future__ import annotations

import struct
import time
from pathlib import Path

from joat.channels.biome import BiomeNotificationStore


def record(text: str, bundle: str, timestamp: float) -> bytes:
    text_bytes = text.encode("utf-8")
    bundle_bytes = bundle.encode("ascii")
    assert len(text_bytes) < 128 and len(bundle_bytes) < 128
    return (
        b"\x0a"
        + bytes([len(text_bytes)])
        + text_bytes
        + b"\x12"
        + bytes([len(bundle_bytes)])
        + bundle_bytes
        + b"\x19"
        + struct.pack("<d", timestamp)
    )


def test_equal_timestamp_record_in_later_segment_is_not_lost(tmp_path: Path) -> None:
    timestamp = time.time() - 1
    (tmp_path / "segment-a").write_bytes(
        record("first", "com.example.first", timestamp)
    )
    source = BiomeNotificationStore(tmp_path)

    first, cursor = source.read_new(
        {"timestamp": timestamp - 1, "seen_at_timestamp": []}
    )
    (tmp_path / "segment-b").write_bytes(
        record("second", "com.example.second", timestamp)
    )
    second, cursor = source.read_new(cursor)
    repeated, _ = source.read_new(cursor)

    assert [item.title for item in first] == ["first"]
    assert [item.title for item in second] == ["second"]
    assert first[0].native_id != second[0].native_id
    assert repeated == []
