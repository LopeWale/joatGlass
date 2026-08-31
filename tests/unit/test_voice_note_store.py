"""Durable, loss-accounted local Voice Note persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from joat.persistence.store import DurableStore
from joat.persistence.voice_notes import (
    VoiceNoteConflictError,
    VoiceNoteStore,
)


def open_store(path: Path) -> tuple[DurableStore, VoiceNoteStore]:
    durable = DurableStore(path)
    return durable, VoiceNoteStore(durable)


def test_complete_capture_checkpoints_transcript_before_raw_audio_is_purged(
    tmp_path: Path,
) -> None:
    durable, notes = open_store(tmp_path / "JOAT" / "joat.sqlite3")
    with durable:
        created = notes.create(
            capture_id="capture-1",
            idempotency_key="voice-note:capture-1",
            title="Design review",
            source_kind="g2.microphone",
            now_us=10,
        )
        assert created.state == "preparing"
        notes.transition("capture-1", "recording", now_us=11)

        first = notes.append_frame(
            "capture-1",
            connection_generation="socket-a",
            seq=7,
            source="glasses",
            speaker="wearer",
            pcm=b"\x01\x00\x02\x00",
            received_at_us=12,
        )
        assert first.ordinal == 0
        assert notes.append_frame(
            "capture-1",
            connection_generation="socket-a",
            seq=7,
            source="glasses",
            speaker="wearer",
            pcm=b"\x01\x00\x02\x00",
            received_at_us=12,
        ) == first
        notes.append_frame(
            "capture-1",
            connection_generation="socket-a",
            seq=8,
            source="glasses",
            speaker=None,
            pcm=b"\x03\x00",
            received_at_us=13,
        )
        notes.append_transcript(
            "capture-1",
            text="Discuss the launch plan.",
            first_frame_ordinal=0,
            last_frame_ordinal=1,
            now_us=14,
        )
        notes.transition("capture-1", "finalizing", now_us=15)
        saved = notes.finalize(
            "capture-1",
            complete=True,
            completion_code="transcript_checkpointed",
            now_us=16,
        )

        assert saved.state == "saved"
        assert saved.received_frames == 2
        assert saved.journaled_frames == 2
        assert saved.transcribed_segments == 1
        assert saved.gap_count == 0
        assert saved.raw_bytes == 0
        assert saved.raw_retained is False
        assert notes.frame_count("capture-1") == 0
        assert [segment.kind for segment in notes.segments("capture-1")] == [
            "transcript",
        ]


def test_conflicting_replay_of_one_wire_frame_fails_closed(tmp_path: Path) -> None:
    durable, notes = open_store(tmp_path / "JOAT" / "joat.sqlite3")
    with durable:
        notes.create(
            capture_id="capture-1",
            idempotency_key="voice-note:capture-1",
            title="Review",
            source_kind="g2.microphone",
            now_us=1,
        )
        notes.transition("capture-1", "recording", now_us=2)
        notes.append_frame(
            "capture-1",
            connection_generation="socket-a",
            seq=1,
            source="glasses",
            speaker=None,
            pcm=b"\x00\x00",
            received_at_us=3,
        )

        with pytest.raises(VoiceNoteConflictError, match="different audio"):
            notes.append_frame(
                "capture-1",
                connection_generation="socket-a",
                seq=1,
                source="glasses",
                speaker=None,
                pcm=b"\x01\x00",
                received_at_us=3,
            )


def test_degraded_save_retains_raw_audio_for_explicit_recovery(tmp_path: Path) -> None:
    durable, notes = open_store(tmp_path / "JOAT" / "joat.sqlite3")
    with durable:
        notes.create(
            capture_id="capture-1",
            idempotency_key="voice-note:capture-1",
            title="Field note",
            source_kind="g2.microphone",
            now_us=1,
        )
        notes.transition("capture-1", "recording", now_us=2)
        notes.append_frame(
            "capture-1",
            connection_generation="socket-a",
            seq=1,
            source="glasses",
            speaker=None,
            pcm=b"\x00\x00",
            received_at_us=3,
        )
        notes.append_gap(
            "capture-1",
            code="sequence_gap",
            detail="expected 2, received 4",
            now_us=4,
        )
        notes.transition("capture-1", "finalizing", now_us=5)

        saved = notes.finalize(
            "capture-1",
            complete=False,
            completion_code="stt_failed_raw_retained",
            now_us=6,
        )

        assert saved.state == "saved_degraded"
        assert saved.raw_retained is True
        assert saved.raw_bytes == 2
        assert saved.gap_count == 1
        assert notes.frame_count("capture-1") == 1


def test_complete_claim_without_transcript_checkpoint_stays_degraded(
    tmp_path: Path,
) -> None:
    durable, notes = open_store(tmp_path / "JOAT" / "joat.sqlite3")
    with durable:
        notes.create(
            capture_id="capture-1",
            idempotency_key="voice-note:capture-1",
            title="Untranscribed",
            source_kind="g2.microphone",
            now_us=1,
        )
        notes.transition("capture-1", "recording", now_us=2)
        notes.append_frame(
            "capture-1",
            connection_generation="socket-a",
            seq=1,
            source="glasses",
            speaker=None,
            pcm=b"\x00\x00",
            received_at_us=3,
        )
        notes.transition("capture-1", "finalizing", now_us=4)

        saved = notes.finalize(
            "capture-1",
            complete=True,
            completion_code="caller_claimed_complete",
            now_us=5,
        )

        assert saved.state == "saved_degraded"
        assert saved.raw_retained is True


def test_partial_transcript_coverage_cannot_purge_untranscribed_audio(
    tmp_path: Path,
) -> None:
    durable, notes = open_store(tmp_path / "JOAT" / "joat.sqlite3")
    with durable:
        notes.create(
            capture_id="capture-1",
            idempotency_key="voice-note:capture-1",
            title="Partial",
            source_kind="g2.microphone",
            now_us=1,
        )
        notes.transition("capture-1", "recording", now_us=2)
        for seq in (1, 2):
            notes.append_frame(
                "capture-1",
                connection_generation="socket-a",
                seq=seq,
                source="glasses",
                speaker=None,
                pcm=b"\x00\x00",
                received_at_us=2 + seq,
            )
        notes.append_transcript(
            "capture-1",
            text="only the first frame",
            first_frame_ordinal=0,
            last_frame_ordinal=0,
            now_us=5,
        )
        notes.transition("capture-1", "finalizing", now_us=6)

        saved = notes.finalize(
            "capture-1",
            complete=True,
            completion_code="caller_claimed_complete",
            now_us=7,
        )

        assert saved.state == "saved_degraded"
        assert saved.raw_retained is True
        assert notes.frame_count("capture-1") == 2


def test_latest_retained_excludes_discarded_content(tmp_path: Path) -> None:
    durable, notes = open_store(tmp_path / "JOAT" / "joat.sqlite3")
    with durable:
        notes.create(
            capture_id="saved",
            idempotency_key="voice-note:saved",
            title="Saved",
            source_kind="g2.microphone",
            now_us=1,
        )
        notes.transition("saved", "recording", now_us=2)
        notes.transition("saved", "finalizing", now_us=3)
        notes.finalize(
            "saved",
            complete=False,
            completion_code="empty",
            now_us=4,
        )
        notes.create(
            capture_id="discarded",
            idempotency_key="voice-note:discarded",
            title="Discarded",
            source_kind="g2.microphone",
            now_us=5,
        )
        notes.transition("discarded", "recording", now_us=6)
        notes.discard("discarded", now_us=7)

        latest = notes.latest_retained()

        assert latest is not None
        assert latest.capture_id == "saved"


def test_transcript_must_reference_journaled_frame_range(tmp_path: Path) -> None:
    durable, notes = open_store(tmp_path / "JOAT" / "joat.sqlite3")
    with durable:
        notes.create(
            capture_id="capture-1",
            idempotency_key="voice-note:capture-1",
            title="Invalid range",
            source_kind="g2.microphone",
            now_us=1,
        )
        notes.transition("capture-1", "recording", now_us=2)

        with pytest.raises(VoiceNoteConflictError, match="not backed"):
            notes.append_transcript(
                "capture-1",
                text="invented",
                first_frame_ordinal=0,
                last_frame_ordinal=1,
                now_us=3,
            )


def test_restart_marks_active_capture_interrupted_without_deleting_raw(
    tmp_path: Path,
) -> None:
    path = tmp_path / "JOAT" / "joat.sqlite3"
    durable, notes = open_store(path)
    with durable:
        notes.create(
            capture_id="capture-1",
            idempotency_key="voice-note:capture-1",
            title="Crash recovery",
            source_kind="g2.microphone",
            now_us=1,
        )
        notes.transition("capture-1", "recording", now_us=2)
        notes.append_frame(
            "capture-1",
            connection_generation="socket-a",
            seq=1,
            source="glasses",
            speaker=None,
            pcm=b"\x00\x00",
            received_at_us=3,
        )

    durable, notes = open_store(path)
    with durable:
        recovered = notes.recover_after_restart(now_us=10)
        assert recovered == ("capture-1",)
        record = notes.get("capture-1")
        assert record.state == "interrupted"
        assert record.completion_code == "process_restart_raw_retained"
        assert record.raw_retained is True
        assert notes.frame_count("capture-1") == 1


def test_discard_hard_purges_title_audio_and_transcript(tmp_path: Path) -> None:
    durable, notes = open_store(tmp_path / "JOAT" / "joat.sqlite3")
    with durable:
        notes.create(
            capture_id="capture-1",
            idempotency_key="voice-note:capture-1",
            title="Private thought",
            source_kind="g2.microphone",
            now_us=1,
        )
        notes.transition("capture-1", "recording", now_us=2)
        notes.append_frame(
            "capture-1",
            connection_generation="socket-a",
            seq=1,
            source="glasses",
            speaker=None,
            pcm=b"\x00\x00",
            received_at_us=3,
        )
        notes.append_transcript(
            "capture-1",
            text="secret",
            first_frame_ordinal=0,
            last_frame_ordinal=0,
            now_us=4,
        )

        discarded = notes.discard("capture-1", now_us=5)

        assert discarded.state == "discarded"
        assert discarded.title is None
        assert discarded.raw_bytes == 0
        assert discarded.raw_retained is False
        assert notes.frame_count("capture-1") == 0
        assert notes.segments("capture-1") == ()


def _retained_note(notes: VoiceNoteStore, capture_id: str, *, base_us: int) -> None:
    notes.create(
        capture_id=capture_id,
        idempotency_key=f"voice-note:{capture_id}",
        title=f"Note {capture_id}",
        source_kind="g2.microphone",
        now_us=base_us,
    )
    notes.transition(capture_id, "recording", now_us=base_us + 1)
    notes.append_frame(
        capture_id,
        connection_generation="socket-a",
        seq=1,
        source="glasses",
        speaker="wearer",
        pcm=b"\x01\x00",
        received_at_us=base_us + 2,
    )
    notes.transition(capture_id, "finalizing", now_us=base_us + 3)
    notes.append_transcript(
        capture_id,
        text="Discussed the launch and assigned owners.",
        first_frame_ordinal=0,
        last_frame_ordinal=0,
        now_us=base_us + 4,
    )
    notes.finalize(
        capture_id,
        complete=True,
        completion_code="transcript_checkpointed_raw_purged",
        now_us=base_us + 5,
    )


def test_insights_replace_and_read_back(tmp_path: Path) -> None:
    durable, notes = open_store(tmp_path / "joat.sqlite3")
    with durable:
        _retained_note(notes, "capture-1", base_us=10)
        assert notes.insights("capture-1") is None

        stored = notes.replace_insights(
            "capture-1",
            engine="mlx-local",
            summary="  The team agreed to ship Friday.  ",
            key_points=("Ship on Friday", "", "QA signs off Thursday"),
            todos=("Email the release notes",),
            now_us=100,
        )
        assert stored.summary == "The team agreed to ship Friday."
        assert stored.key_points == ("Ship on Friday", "QA signs off Thursday")
        assert stored.todos == ("Email the release notes",)

        # Re-summarizing with another engine replaces, never appends.
        notes.replace_insights(
            "capture-1",
            engine="claude-cli",
            summary="Ship Friday; QA Thursday.",
            key_points=("Ship Friday",),
            todos=(),
            now_us=200,
        )
        read = notes.insights("capture-1")
        assert read is not None
        assert read.engine == "claude-cli"
        assert read.generated_at_us == 200
        assert read.key_points == ("Ship Friday",)
        assert read.todos == ()


def test_insights_require_a_retained_transcribed_note(tmp_path: Path) -> None:
    durable, notes = open_store(tmp_path / "joat.sqlite3")
    with durable:
        notes.create(
            capture_id="capture-1",
            idempotency_key="voice-note:capture-1",
            title="Still recording",
            source_kind="g2.microphone",
            now_us=10,
        )
        notes.transition("capture-1", "recording", now_us=11)
        with pytest.raises(VoiceNoteConflictError, match="retained"):
            notes.replace_insights(
                "capture-1",
                engine="mlx-local",
                summary="Too early",
                key_points=(),
                todos=(),
                now_us=12,
            )


def test_recent_retained_orders_newest_first_and_skips_discards(tmp_path: Path) -> None:
    durable, notes = open_store(tmp_path / "joat.sqlite3")
    with durable:
        _retained_note(notes, "capture-1", base_us=10)
        _retained_note(notes, "capture-2", base_us=100)
        notes.create(
            capture_id="capture-3",
            idempotency_key="voice-note:capture-3",
            title="Dropped",
            source_kind="g2.microphone",
            now_us=200,
        )
        notes.transition("capture-3", "recording", now_us=201)
        notes.discard("capture-3", now_us=202)

        recent = notes.recent_retained(5)
        assert [note.capture_id for note in recent] == ["capture-2", "capture-1"]
