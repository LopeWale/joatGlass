"""QuickNotes on the overview's rail (the wearer, 2026-10-11).

"Replace the search on the overview tab with the quicknote … so now the
overview has a way to quickly get to the quicknote." Like Even's QuickNote
widget on its Dashboard: the tab shows the newest notes, and a tap on it
opens QuickNotes itself (the plugin does that; this tab has no rows of its
own to open). Search moved into the menu.

The age column ("5m") is text the Mac writes into each row, and the plugin
paints whatever it last received. Until 2026-10-12 a row was only rewritten
when a note was saved, so the overview kept saying "5m" for an hour while
the QuickNotes page itself said "1h" (the wearer: "the quick notes time does
not update on the overview when it is the current active tab"). `run` now
pushes the tab again whenever an age it shows would read differently.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
import time
from typing import Any

from ..persistence.notes import NoteStore
from ..persistence.voice_notes import VoiceNoteStore
from .panels import PanelSnapshot, Reader

log = logging.getLogger(__name__)

#: Rows the resting pane can show under the tab.
_ROWS = 4

#: A note saved since the last look starts at "now" and turns "1m" a minute
#: later, so the ages are never checked less often than this.
_MAX_WAIT_S = 60.0


def _age(unix: float, now: float | None = None) -> str:
    """"now", "5m", "3h", "2d" — the QuickNotes list's own age column."""
    mins = max(0, int(((time.time() if now is None else now) - unix) / 60))
    if mins < 1:
        return "now"
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    return f"{hours}h" if hours < 24 else f"{hours // 24}d"


def _next_change_s(unix: float, now: float) -> float:
    """Seconds until `_age` of this stamp reads differently."""
    elapsed = max(0.0, now - unix)
    unit = 60.0 if elapsed < 3600 else 3600.0 if elapsed < 86400 else 86400.0
    return unit - (elapsed % unit)


class QuickNotesPanel:
    """The rail's QuickNotes tab: the newest notes, recordings and text."""

    tab = "quicknotes"

    def __init__(
        self,
        voice_notes: VoiceNoteStore,
        notes: NoteStore,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._voice_notes = voice_notes
        self._notes = notes
        self._clock = clock
        self._sleep = sleep
        #: The rows last handed to the publisher; None until the first push.
        self._shown: tuple[str, ...] | None = None
        #: When those rows' notes were last updated (unix seconds).
        self._shown_at: tuple[float, ...] = ()

    def _newest(self, now: float) -> list[tuple[float, str]]:
        rows: list[tuple[int, str]] = []
        for voice in self._voice_notes.retained():
            rows.append((voice.updated_at_us, f"[{_age(voice.updated_at_us / 1e6, now)}] {voice.title or 'Untitled'}"))
        for note in self._notes.active():
            first = " ".join(note.text.split())
            rows.append((note.updated_at_us, f"[{_age(note.updated_at_us / 1e6, now)}] {first}"))
        rows.sort(key=lambda row: -row[0])
        return [(at / 1e6, label[:120]) for at, label in rows[:_ROWS]]

    def snapshot(self) -> PanelSnapshot:
        newest = self._newest(self._clock())
        self._shown = tuple(label for _at, label in newest)
        self._shown_at = tuple(at for at, _label in newest)
        return PanelSnapshot(
            rows=self._shown,
            empty="No notes yet. Tap to create one.",
        )

    def _wait_s(self) -> float:
        """Until the first shown age turns over, never more than a minute."""
        now = self._clock()
        changes = [_next_change_s(at, now) for at in self._shown_at]
        return max(1.0, min([_MAX_WAIT_S, *changes]))

    async def run(self, on_changed: Callable[[], Any]) -> None:
        """Push the tab again whenever what it shows would read differently.

        Only a change is pushed: the plugin repaints the overview for every
        push to the tab on the glass, the way the pomodoro minute does.
        """
        while True:
            await self._sleep(self._wait_s())
            if self._shown is None:
                # Nothing published yet: the plugin's connect fills the tab.
                continue
            try:
                current = tuple(label for _at, label in self._newest(self._clock()))
            except Exception:
                log.warning("quicknotes tab check failed", exc_info=True)
                continue
            if current == self._shown:
                continue
            try:
                result = on_changed()
                if asyncio.iscoroutine(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("quicknotes tab publish failed", exc_info=True)

    async def refresh(self) -> None:
        """Nothing to fetch: the notes live on this Mac."""

    async def open(self, index: int) -> Reader | None:
        """A tap on the tab opens QuickNotes on the plugin; rows open nothing here."""
        return None


__all__ = ["QuickNotesPanel"]
