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


class _Stop(Exception):
    pass


def _ticking(panel_clock: list[float], waits: list[float], rounds: int):
    """A sleep that moves the panel's clock instead of waiting, `rounds` times."""

    async def sleep(seconds: float) -> None:
        if len(waits) == rounds:
            raise _Stop
        waits.append(seconds)
        panel_clock[0] += seconds

    return sleep


def test_the_overview_age_is_pushed_again_when_it_turns_over(tmp_path: Path) -> None:
    # The wearer, 2026-10-12: "the quick notes time does not update on the
    # overview when it is the current active tab".
    import asyncio

    with DurableStore(tmp_path / "joat.sqlite3") as durable:
        voice, text = VoiceNoteStore(durable), NoteStore(durable)
        start = 1_800_000_000.0
        _saved_recording(voice, int((start - 90) * 1e6) - 3)
        clock, waits, pushed = [start], [], []
        panel = QuickNotesPanel(voice, text, clock=lambda: clock[0],
                                sleep=_ticking(clock, waits, rounds=3))
        assert panel.snapshot().rows == ("[1m] Delivery and van booking",)

        def publish() -> None:
            pushed.append(panel.snapshot().rows)

        try:
            asyncio.run(panel.run(on_changed=publish))
        except _Stop:
            pass

        # Each wake lands on the next minute boundary, and each one changed
        # the label, so each one was pushed.
        assert waits == [30.0, 60.0, 60.0]
        assert pushed == [
            ("[2m] Delivery and van booking",),
            ("[3m] Delivery and van booking",),
            ("[4m] Delivery and van booking",),
        ]


def test_an_unchanged_tab_and_an_unpublished_tab_are_not_pushed(tmp_path: Path) -> None:
    import asyncio

    with DurableStore(tmp_path / "joat.sqlite3") as durable:
        voice, text = VoiceNoteStore(durable), NoteStore(durable)
        start = 1_800_000_000.0
        # Three hours old: the label only changes on the hour.
        _saved_recording(voice, int((start - 3 * 3600 - 600) * 1e6) - 3)
        clock, waits, pushed = [start], [], []
        panel = QuickNotesPanel(voice, text, clock=lambda: clock[0],
                                sleep=_ticking(clock, waits, rounds=2))

        try:
            asyncio.run(panel.run(on_changed=lambda: pushed.append("before first push")))
        except _Stop:
            pass
        # Nothing was ever published: the plugin's connect fills the tab.
        assert pushed == []

        waits.clear()
        clock[0] = start
        panel.snapshot()
        try:
            asyncio.run(panel.run(on_changed=lambda: pushed.append(panel.snapshot().rows)))
        except _Stop:
            pass
        # Never more than a minute apart (a new note starts at "now"), and
        # "[3h]" still reads "[3h]" two minutes later: nothing to push.
        assert waits == [60.0, 60.0]
        assert pushed == []
