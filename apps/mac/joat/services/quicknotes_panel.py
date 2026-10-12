"""QuickNotes on the overview's rail (the wearer, 2026-10-11).

"Replace the search on the overview tab with the quicknote … so now the
overview has a way to quickly get to the quicknote." Like Even's QuickNote
widget on its Dashboard: the tab shows the newest notes, and a tap on it
opens QuickNotes itself (the plugin does that; this tab has no rows of its
own to open). Search moved into the menu.
"""

from __future__ import annotations

import time

from ..persistence.notes import NoteStore
from ..persistence.voice_notes import VoiceNoteStore
from .panels import PanelSnapshot, Reader

#: Rows the resting pane can show under the tab.
_ROWS = 4


def _age(unix: float) -> str:
    """"now", "5m", "3h", "2d" — the QuickNotes list's own age column."""
    mins = max(0, int((time.time() - unix) / 60))
    if mins < 1:
        return "now"
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    return f"{hours}h" if hours < 24 else f"{hours // 24}d"


class QuickNotesPanel:
    """The rail's QuickNotes tab: the newest notes, recordings and text."""

    tab = "quicknotes"

    def __init__(self, voice_notes: VoiceNoteStore, notes: NoteStore) -> None:
        self._voice_notes = voice_notes
        self._notes = notes

    def snapshot(self) -> PanelSnapshot:
        rows: list[tuple[int, str]] = []
        for voice in self._voice_notes.retained():
            rows.append((voice.updated_at_us, f"[{_age(voice.updated_at_us / 1e6)}] {voice.title or 'Untitled'}"))
        for note in self._notes.active():
            first = " ".join(note.text.split())
            rows.append((note.updated_at_us, f"[{_age(note.updated_at_us / 1e6)}] {first}"))
        rows.sort(key=lambda row: -row[0])
        return PanelSnapshot(
            rows=tuple(label[:120] for _at, label in rows[:_ROWS]),
            empty="No notes yet. Tap to create one.",
        )

    async def refresh(self) -> None:
        """Nothing to fetch: the notes live on this Mac."""

    async def open(self, index: int) -> Reader | None:
        """A tap on the tab opens QuickNotes on the plugin; rows open nothing here."""
        return None


__all__ = ["QuickNotesPanel"]
