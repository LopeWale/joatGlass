"""The Planner on the overview's rail (the wearer, 2026-10-15).

"Remove the notification from the overview … let's put the planner's
reminder … so I can see the reminders/tasks." The first tab of the rail used
to be Notifications; it is now the Planner, and Notifications is the first
row of the menu while the overview is up.

What the tab shows is the wearer's open Reminders: what is overdue, what is
due today, and what is due in the next six days, earliest first. That is the
list QuickNotes feeds ("create reminders from the last note" makes one
Reminder per to-do), and the same Reminders Planning merges with Calendar
events. It is not Planning's own day page: Planning shows a reminder only on
the day it is due, so a missed one drops out of sight there. A to-do glance
should keep it until it is done, so this tab asks EventKit for a window of
days and keeps the overdue ones in view (`load_native_reminders`).

A tap on the tab opens Planning itself (the plugin does that; the tab has no
rows of its own to open), the way a tap on QuickNotes opens QuickNotes.

Row text is written here and the plugin paints it as it arrives, so the word
in front of a row ("Late", "Today", "Tmrw", "Fri", or the time) is computed
on this Mac in the wearer's zone, and the tab is read again every minute and
pushed when a row would read differently, the way the QuickNotes tab keeps
its ages honest.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date, datetime, timedelta, tzinfo
import logging
from typing import Any

from ..native.bridge import (
    NativeBridgeError,
    NativeBridgeProtocolError,
    NativeBridgeUnavailable,
)
from .panels import PanelSnapshot, Reader
from .planning import (
    NativePlanningClient,
    PlanningAuthorizationError,
    PlanningEntry,
    load_native_reminders,
)

log = logging.getLogger(__name__)

#: Rows the resting pane can show under the tab (the glass pane is 8 lines).
_ROWS = 8

#: How far back an unfinished reminder stays in view, and how far ahead the
#: tab looks. Six days ahead makes the weekday word unambiguous: seven days
#: out would read "Thu" on a Thursday and mean next week.
_LOOKBACK_DAYS = 7
_AHEAD_DAYS = 6

#: How often EventKit is asked again.
_POLL_S = 60.0

#: A bridge that keeps failing stops vouching for rows it can no longer
#: check: after this many misses in a row the stale rows are dropped.
_MAX_MISSES = 3

_LOADING = "Reading your reminders…"
_NOTHING = "Nothing due this week."


def _label_and_value(entry: PlanningEntry, today: date) -> tuple[str, str]:
    """The word in the label column, and the row's text.

    A label is at most ~92 px on the glass: "Wed 09:00" does not fit, so a
    timed reminder on another day puts its time in front of the title.
    """
    day = entry.sort_at.date()
    delta = (day - today).days
    timed = entry.time_label != "ALL DAY"
    if delta < 0:
        return "Late", entry.title
    if delta == 0:
        return (entry.time_label, entry.title) if timed else ("Today", entry.title)
    label = "Tmrw" if delta == 1 else day.strftime("%a")
    return label, (f"{entry.time_label} {entry.title}" if timed else entry.title)


def planner_rows(entries: tuple[PlanningEntry, ...], today: date) -> tuple[str, ...]:
    """The tab's rows: "[Today] Call the bank", "[09:00] Standup notes".

    More than the pane holds ends in an honest "+N more" row instead of
    silently dropping the rest.
    """
    shown = entries if len(entries) <= _ROWS else entries[: _ROWS - 1]
    rows = [
        f"[{label}] {value}"
        for label, value in (_label_and_value(entry, today) for entry in shown)
    ]
    if len(entries) > len(shown):
        rows.append(f"+{len(entries) - len(shown)} more")
    return tuple(rows)


class PlannerPanel:
    """The rail's Planner tab: open reminders, overdue first."""

    tab = "planner"

    def __init__(
        self,
        client: NativePlanningClient | None,
        zone: Callable[[], tzinfo],
        *,
        clock: Callable[[tzinfo], datetime] = lambda zone: datetime.now(zone),
        poll_s: float = _POLL_S,
    ) -> None:
        self._client = client
        self._zone = zone
        self._clock = clock
        self._poll_s = poll_s
        self._rows: tuple[str, ...] = ()
        self._empty: str | None = (
            _LOADING if client is not None
            else "Start JOAT from the Mac app to see reminders."
        )
        #: What was last handed to the publisher; None until the first load.
        self._shown: tuple[tuple[str, ...], str | None] | None = None
        self._misses = 0
        self._wake = asyncio.Event()

    def snapshot(self) -> PanelSnapshot:
        self._shown = (self._rows, self._empty)
        return PanelSnapshot(rows=self._rows, empty=self._empty)

    def nudge(self) -> None:
        """Read again now: a reminder was just made or completed."""
        self._wake.set()

    async def _reload(self) -> None:
        client = self._client
        if client is None:
            return
        zone = self._zone()
        today = self._clock(zone).date()
        try:
            entries = await load_native_reminders(
                client,
                first_day=today - timedelta(days=_LOOKBACK_DAYS),
                last_day=today + timedelta(days=_AHEAD_DAYS),
                zone=zone,
            )
        except PlanningAuthorizationError:
            self._misses = 0
            self._rows = ()
            self._empty = "Allow JOAT to use Reminders on your Mac."
            return
        except (NativeBridgeError, NativeBridgeProtocolError, NativeBridgeUnavailable):
            # The three bridge failures are siblings, not one family. Stale
            # rows beat a blank tab for a minute or two; not for good.
            self._misses += 1
            if self._misses >= _MAX_MISSES or not self._rows:
                self._rows = ()
                self._empty = "Reminders are unavailable on this Mac."
            return
        self._misses = 0
        self._rows = planner_rows(entries, today)
        self._empty = _NOTHING

    def _differs(self) -> bool:
        return self._shown is None or self._shown != (self._rows, self._empty)

    async def run(self, on_changed: Callable[[], Any]) -> None:
        """Read the reminders, push the tab when it would read differently.

        Only a change is pushed: the plugin repaints the overview for every
        push to the tab on the glass.
        """
        while True:
            try:
                await self._reload()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("planner tab check failed", exc_info=True)
            if self._differs():
                try:
                    result = on_changed()
                    if asyncio.iscoroutine(result):
                        await result
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.warning("planner tab publish failed", exc_info=True)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_s)
            except TimeoutError:
                pass
            self._wake.clear()

    async def refresh(self) -> None:
        await self._reload()

    async def open(self, index: int) -> Reader | None:
        """A tap on the tab opens Planning on the plugin; rows open nothing here."""
        return None


__all__ = ["PlannerPanel", "planner_rows"]
