"""The overview's QuickNotes tab (it replaced Search on the rail, 2026-10-11)."""

from __future__ import annotations

from pathlib import Path
import time

from joat.persistence.notes import NoteSource, NoteStore
from joat.persistence.store import DurableStore
from joat.persistence.voice_notes import VoiceNoteStore
from joat.services.quicknotes_panel import QuickNotesPanel


def _saved_recording(notes: VoiceNoteStore, at_us: int) -> None:
    notes.create(capture_id="rec-1", idempotency_key="rec-1", title="Delivery and van booking",
                 source_kind="g2.microphone", now_us=at_us)
    notes.transition("rec-1", "recording", now_us=at_us + 1)
    notes.transition("rec-1", "finalizing", now_us=at_us + 2)
    notes.finalize("rec-1", complete=True, completion_code="no_audio", now_us=at_us + 3)


def test_the_tab_shows_the_newest_notes_first(tmp_path: Path) -> None:
    with DurableStore(tmp_path / "joat.sqlite3") as durable:
        voice, text = VoiceNoteStore(durable), NoteStore(durable)
        now = time.time_ns() // 1000
        text.create(note_id="t1", idempotency_key="t1", text="Buy milk\nand eggs",
                    source=NoteSource(kind="voice"), now_us=now - 600_000_000)
        _saved_recording(voice, now - 60_000_000)

        snapshot = QuickNotesPanel(voice, text).snapshot()

        assert snapshot.rows == ("[1m] Delivery and van booking", "[10m] Buy milk and eggs")
        assert snapshot.empty


def test_an_empty_tab_says_so_and_rows_open_nothing(tmp_path: Path) -> None:
    import asyncio

    with DurableStore(tmp_path / "joat.sqlite3") as durable:
        panel = QuickNotesPanel(VoiceNoteStore(durable), NoteStore(durable))
        assert panel.snapshot().rows == ()
        assert panel.snapshot().empty == "No notes yet. Tap to create one."
        assert asyncio.run(panel.open(0)) is None
