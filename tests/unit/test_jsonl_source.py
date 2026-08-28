from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from joat.channels.jsonl_source import JsonlNotificationStore


def append(path: Path, value: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, separators=(",", ":")) + "\n")


def test_identical_lines_have_distinct_stable_byte_identities(tmp_path: Path) -> None:
    path = tmp_path / "notifications.jsonl"
    path.touch()
    store = JsonlNotificationStore(path, require_heartbeat=False)
    cursor = store.baseline()
    event = {"at": 1_700_000_000.0, "app": "Messages", "title": "Ada", "body": "Hi"}
    append(path, event)
    append(path, event)

    fresh, cursor = store.read_new(cursor)
    repeated, repeated_cursor = store.read_new(cursor)

    assert len(fresh) == 2
    assert fresh[0].native_id != fresh[1].native_id
    assert repeated == []
    assert repeated_cursor["offset"] == cursor["offset"] == path.stat().st_size


def test_missing_feed_is_a_health_failure_not_an_empty_success(tmp_path: Path) -> None:
    path = tmp_path / "notifications.jsonl"
    path.write_text("")
    store = JsonlNotificationStore(path, require_heartbeat=False)
    cursor = store.baseline()
    path.unlink()

    with pytest.raises(FileNotFoundError):
        store.read_new(cursor)


def test_same_inode_truncation_creates_a_new_generation(tmp_path: Path) -> None:
    path = tmp_path / "notifications.jsonl"
    path.touch()
    store = JsonlNotificationStore(path, require_heartbeat=False)
    cursor = store.baseline()
    append(path, {"at": 1_700_000_000.0, "app": "Mail", "title": "A", "body": "one"})
    first, cursor = store.read_new(cursor)

    path.write_text(
        json.dumps(
            {"at": 1_700_000_001.0, "app": "Mail", "title": "B", "body": "two"},
            separators=(",", ":"),
        ) + "\n"
    )
    second, cursor = store.read_new(cursor)

    assert len(first) == len(second) == 1
    assert first[0].native_id != second[0].native_id
    assert second[0].title == "B"
    assert cursor["offset"] == path.stat().st_size


def test_metadata_touch_does_not_replay_unchanged_feed(tmp_path: Path) -> None:
    path = tmp_path / "notifications.jsonl"
    path.touch()
    store = JsonlNotificationStore(path, require_heartbeat=False)
    cursor = store.baseline()
    append(path, {"at": time.time(), "app": "Mail", "title": "A", "body": "one"})
    first, cursor = store.read_new(cursor)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

    repeated, repeated_cursor = store.read_new(cursor)

    assert [item.title for item in first] == ["A"]
    assert repeated == []
    assert repeated_cursor["offset"] == cursor["offset"]


def test_initial_tail_includes_recent_line_but_skips_old_backlog(tmp_path: Path) -> None:
    path = tmp_path / "notifications.jsonl"
    append(path, {"at": 1_700_000_000.0, "app": "Mail", "title": "old", "body": "x"})
    append(path, {"at": time.time(), "app": "Mail", "title": "fresh", "body": "y"})
    store = JsonlNotificationStore(path, require_heartbeat=False)

    notifications, _ = store.read_new(store.baseline())

    assert [item.title for item in notifications] == ["fresh"]


def test_stale_watcher_heartbeat_is_a_health_failure(tmp_path: Path) -> None:
    path = tmp_path / "notifications.jsonl"
    path.touch()
    heartbeat = tmp_path / "notifications.heartbeat"
    heartbeat.touch()
    stale = time.time() - 30
    os.utime(heartbeat, (stale, stale))

    with pytest.raises(RuntimeError, match="stale"):
        JsonlNotificationStore(path, heartbeat_max_age_s=5).baseline()


def test_malformed_complete_line_is_reported_as_parse_degraded(tmp_path: Path) -> None:
    path = tmp_path / "notifications.jsonl"
    path.touch()
    store = JsonlNotificationStore(path, require_heartbeat=False)
    cursor = store.baseline()
    with path.open("ab") as stream:
        stream.write(b"not json\n")
    append(path, {"at": time.time(), "app": "Mail", "title": "valid", "body": "y"})

    notifications, cursor = store.read_new(cursor)

    assert [item.title for item in notifications] == ["valid"]
    assert store.health_after_read("healthy_best_effort") == "parse_degraded"
    assert cursor["offset"] == path.stat().st_size
