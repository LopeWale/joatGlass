"""Saved QuickNotes land in Apple Notes, once, and update in place (FEAT-002)."""

from __future__ import annotations

import json
from pathlib import Path

from joat.persistence.note_exports import NoteExportStore
from joat.persistence.notes import NoteSource, NoteStore
from joat.persistence.store import DurableStore
from joat.persistence.voice_notes import VoiceNoteStore
from joat.services.notes_export import NotesWrite
from joat.services.notes_sync import (
    NotesSettings,
    NotesSync,
    text_title,
    voice_document,
)


class FakeWriter:
    def __init__(self) -> None:
        self.writes: list[dict[str, object]] = []

    async def write(
        self,
        *,
        title: str,
        body_lines: tuple[str, ...],
        folder: str = "JOAT",
        existing_id: str | None = None,
    ) -> NotesWrite:
        self.writes.append({
            "title": title, "lines": body_lines, "folder": folder, "existing": existing_id,
        })
        note_id = existing_id or f"x-coredata://N/ICNote/p{len(self.writes)}"
        return NotesWrite(f"Sent “{title}”.", True, note_id)


def saved_recording(notes: VoiceNoteStore, *, speaker: str | None = "other") -> None:
    notes.create(
        capture_id="capture-1", idempotency_key="voice-note:capture-1",
        title="Supplier call", source_kind="g2.microphone", now_us=1_000_000,
    )
    notes.transition("capture-1", "recording", now_us=1_000_001)
    notes.append_frame(
        "capture-1", connection_generation="g", seq=0, source="glasses",
        speaker=speaker, pcm=b"\x01\x00", received_at_us=1_000_002,
    )
    notes.transition("capture-1", "finalizing", now_us=1_000_003)
    notes.append_transcript(
        "capture-1", text="We can deliver on the 14th.", first_frame_ordinal=0,
        last_frame_ordinal=0, now_us=1_000_004, speaker=speaker,
    )
    notes.finalize(
        "capture-1", complete=True,
        completion_code="transcript_checkpointed_raw_purged", now_us=1_000_005,
    )


def build(tmp_path: Path) -> tuple[DurableStore, VoiceNoteStore, NoteStore, FakeWriter, NotesSync]:
    durable = DurableStore(tmp_path / "joat.sqlite3")
    voice = VoiceNoteStore(durable)
    text = NoteStore(durable)
    writer = FakeWriter()
    sync = NotesSync(
        writer, NoteExportStore(durable), voice_notes=voice, text_notes=text,
        settings=NotesSettings(tmp_path / "notes.json"), clock_us=lambda: 5,
    )
    return durable, voice, text, writer, sync


def test_a_recording_reads_as_overview_next_steps_then_who_said_what(tmp_path: Path) -> None:
    durable, voice, _text, _writer, _sync = build(tmp_path)
    with durable:
        saved_recording(voice)
        voice.replace_insights(
            "capture-1", engine="mlx-local", summary="Delivery is on the 14th.",
            key_points=("Supplier confirmed",), todos=("Book the van",), now_us=2_000_000,
        )

        document = voice_document(voice, "capture-1")

        assert document.title == "Supplier call"
        lines = list(document.lines)
        assert lines[lines.index("Overview") + 1] == "Delivery is on the 14th."
        assert "☐ Book the van" in lines
        assert lines[-1] == "Them: We can deliver on the 14th."


async def test_the_first_write_creates_and_the_second_updates_the_same_note(
    tmp_path: Path,
) -> None:
    durable, voice, _text, writer, sync = build(tmp_path)
    with durable:
        saved_recording(voice)

        assert await sync.sync_voice("capture-1") == "Sent “Supplier call”."
        await sync.sync_voice("capture-1")

        assert writer.writes[0]["existing"] is None
        assert writer.writes[1]["existing"] == "x-coredata://N/ICNote/p1"


async def test_the_switch_off_keeps_notes_on_the_mac_unless_asked(tmp_path: Path) -> None:
    durable, voice, _text, writer, sync = build(tmp_path)
    with durable:
        saved_recording(voice)
        assert sync.handle_toggle("Stop saving notes to Apple Notes.") is not None
        assert json.loads((tmp_path / "notes.json").read_text())["saveToAppleNotes"] is False

        assert await sync.sync_voice("capture-1") is None
        assert writer.writes == []
        # An explicit "send the last note to my notes" still goes.
        assert await sync.sync_voice("capture-1", force=True) is not None

        assert sync.handle_toggle("save notes to apple notes") is not None
        assert sync.enabled is True
        assert sync.handle_toggle("save the milk") is None


async def test_a_recording_with_no_words_makes_no_apple_note(tmp_path: Path) -> None:
    durable, voice, _text, writer, sync = build(tmp_path)
    with durable:
        voice.create(
            capture_id="quiet", idempotency_key="voice-note:quiet", title="Note 10:00",
            source_kind="g2.microphone", now_us=1,
        )
        voice.transition("quiet", "recording", now_us=2)
        voice.append_frame(
            "quiet", connection_generation="g", seq=0, source="glasses",
            speaker=None, pcm=b"\x00\x00", received_at_us=3,
        )
        voice.append_quiet_span(
            "quiet", first_frame_ordinal=0, last_frame_ordinal=0, reason="silence", now_us=4,
        )
        voice.transition("quiet", "finalizing", now_us=5)
        voice.finalize("quiet", complete=True, completion_code="x", now_us=6)

        assert await sync.sync_voice("quiet") is None
        assert writer.writes == []


async def test_a_dictated_note_goes_too_titled_by_its_first_words(tmp_path: Path) -> None:
    durable, _voice, text, writer, sync = build(tmp_path)
    with durable:
        text.create(
            note_id="t1", idempotency_key="quick-note:t1",
            text="Buy milk and eggs on the way home from the station tonight please",
            source=NoteSource(kind="hud.quick-note", identifier="t1"), now_us=1_000_000,
        )

        await sync.sync_text("t1")

        assert writer.writes[0]["title"] == "Buy milk and eggs on the way home from the station tonight…"
        assert "Buy milk and eggs" in " ".join(writer.writes[0]["lines"])  # type: ignore[arg-type]


def test_text_title_keeps_short_notes_whole() -> None:
    assert text_title("Call Mum") == "Call Mum"
    assert text_title("   ") == "Note"


def test_settings_default_on_and_survive_a_bad_file(tmp_path: Path) -> None:
    settings = NotesSettings(tmp_path / "notes.json")
    assert settings.enabled is True and settings.folder == "JOAT"
    (tmp_path / "notes.json").write_text("{not json", encoding="utf-8")
    assert settings.enabled is True
    (tmp_path / "notes.json").write_text('{"saveToAppleNotes": false, "folder": "Glasses"}')
    assert settings.enabled is False and settings.folder == "Glasses"
