"""Privacy contract tests for the opt-in voice-latency receipt."""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

from joat.services.voice_latency import MAX_EVENTS, VoiceLatencyStore, write_voice_latency_receipt


async def test_receipt_is_bounded_content_free_and_keeps_a_turn_correlation(
    tmp_path: Path,
) -> None:
    output = tmp_path / "voice-latency.json"
    store = VoiceLatencyStore(path=output)

    store.record(7, "utterance_ready", "lte250ms")
    store.record(7, "stt_complete", "5_10s")
    store.record(7, "first_reply_sent", "gt10s")
    await store.flush()

    payload = json.loads(output.read_text())
    assert payload == {
        "schema": "joat.voice-latency.v1",
        "events": [
            {"sequence": 1, "turn": 7, "stage": "utterance_ready", "bucket": "lte250ms"},
            {"sequence": 2, "turn": 7, "stage": "stt_complete", "bucket": "5_10s"},
            {"sequence": 3, "turn": 7, "stage": "first_reply_sent", "bucket": "gt10s"},
        ],
    }
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(output.parent.stat().st_mode) == 0o700
    rendered = output.read_text()
    assert "transcript" not in rendered
    assert "private answer" not in rendered
    assert "recordedAt" not in rendered


async def test_unknown_stage_or_bucket_is_never_retained(tmp_path: Path) -> None:
    writes: list[list[dict[str, object]]] = []
    store = VoiceLatencyStore(path=tmp_path / "receipt.json", writer=lambda _path, events: writes.append(events))

    store.record(1, "utterance_ready", "lte250ms")
    store.record(1, "private stage", "exact 17.2 seconds")
    await store.flush()

    assert writes == [[
        {"sequence": 1, "turn": 1, "stage": "utterance_ready", "bucket": "lte250ms"},
    ]]


async def test_receipt_keeps_only_the_most_recent_bounded_events(tmp_path: Path) -> None:
    output = tmp_path / "receipt.json"
    store = VoiceLatencyStore(path=output)

    for turn in range(1, MAX_EVENTS + 3):
        store.record(turn, "utterance_ready", "lte250ms")
    await store.flush()

    events = json.loads(output.read_text())["events"]
    assert len(events) == MAX_EVENTS
    assert events[0] == {
        "sequence": 1,
        "turn": 3,
        "stage": "utterance_ready",
        "bucket": "lte250ms",
    }
    assert events[-1]["sequence"] == MAX_EVENTS


async def test_writer_failure_disables_future_persistence(tmp_path: Path) -> None:
    writes = 0

    def fail(_path: Path, _events: list[dict[str, object]]) -> None:
        nonlocal writes
        writes += 1
        raise OSError("private path")

    store = VoiceLatencyStore(path=tmp_path / "receipt.json", writer=fail)
    store.record(1, "utterance_ready", "lte250ms")
    await asyncio.sleep(0)
    store.record(1, "stt_complete", "1_2s")
    await store.flush()

    assert writes == 1


def test_writer_emits_exact_schema_without_creating_a_general_log(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"

    write_voice_latency_receipt(path, [
        {"sequence": 1, "turn": 4, "stage": "ack_sent", "bucket": "250_500ms"},
    ])

    assert json.loads(path.read_text())["schema"] == "joat.voice-latency.v1"
