"""Announce a calendar event that is about to start.

Scope, deliberately narrow: this watches CALENDAR EVENTS only. It is not a
"Planning monitor". Planning is the merged read-only view of two different
native stores, and the other half — reminders/tasks — already has its own
due-alert path with its own acknowledged delivery contract
(`services/task_monitor.py`). A monitor spanning both would announce a
reminder twice and blur two things the wearer keeps separate: a schedule that
starts, and a task that is due.

It also stays on the ordinary notification seam (`SessionManager.notify`), so
an event heads-up obeys the same coalescing, rate cap, and never-interrupt-a-
session policy as any other card, rather than inventing a second way to seize
the lenses.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, tzinfo
from typing import Any, Protocol

from ..channels.cards import Card, Urgency
from ..persistence.calendar_alerts import CalendarAlertStore

log = logging.getLogger(__name__)

# How far ahead to look. Long enough to stand up and walk somewhere, short
# enough that the HUD is not previewing the whole afternoon.
DEFAULT_LEAD_S = 600.0
# The window moves slowly, so the poll can too — a minute of latency on a
# ten-minute warning is invisible, and this shares the native bridge with
# everything else.
DEFAULT_POLL_INTERVAL_S = 60.0

_EVENT_QUERY_LIMIT = 100


class CalendarQueryClient(Protocol):
    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any: ...


def starting_soon_card(title: str, starts_at: datetime, minutes_away: int) -> Card:
    """Two lines, who then what — the card contract in `channels/cards.py`.

    NOTIFY, never URGENT: urgency in this codebase means corroborated
    time-critical wording, and "a meeting begins shortly" is a heads-up. A
    calendar that shouted would train the wearer to ignore the bright state.
    """
    when = starts_at.strftime("%H:%M")
    summary = f"Starts {when}" if minutes_away <= 0 else f"Starts {when} · in {minutes_away} min"
    return Card(
        source=title,
        channel="Calendar",
        summary=summary,
        urgency=Urgency.NOTIFY,
        event_class="calendar.starting_soon",
        origin_id=None,
    )


class CalendarMonitor:
    """Poll a short forward window and announce each event once."""

    def __init__(
        self,
        client: CalendarQueryClient,
        alerts: CalendarAlertStore,
        notify: Callable[[Card], None | Awaitable[None]],
        *,
        zone: tzinfo,
        lead_s: float = DEFAULT_LEAD_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        if lead_s <= 0:
            raise ValueError("lead must be positive")
        if poll_interval_s <= 0:
            raise ValueError("poll interval must be positive")
        self._client = client
        self._alerts = alerts
        self._notify = notify
        self._zone = zone
        self._lead_s = lead_s
        self._interval = poll_interval_s
        self._running = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            await self.run_once()
            await asyncio.sleep(self._interval)

    def stop(self) -> None:
        self._running = False

    async def run_once(
        self,
        *,
        now_unix_ms: int | None = None,
        now_us: int | None = None,
    ) -> int:
        """Announce every not-yet-announced event inside the lead window.

        Fail quiet, like the notification channel going dormant: without
        Calendar access the native call raises and the wearer simply gets no
        schedule alerts, rather than a crash or a nagging error card.
        """
        now_ms = int(time.time() * 1_000) if now_unix_ms is None else now_unix_ms
        stamp_us = int(time.time() * 1_000_000) if now_us is None else now_us
        window_end_ms = now_ms + int(self._lead_s * 1_000)
        try:
            response = await self._client.call("calendar.query", {
                "start_unix_ms": now_ms,
                "end_unix_ms": window_end_ms,
                # Empty means every calendar: the bridge only narrows when
                # given explicit identifiers, so this needs no extra
                # listContainers round-trip per poll.
                "calendar_identifiers": [],
                "limit": _EVENT_QUERY_LIMIT,
            })
        except Exception as exc:
            log.info("calendar starting-soon poll unavailable: %s", type(exc).__name__)
            return 0

        raw = getattr(response, "result", None)
        items = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
        announced = 0
        for item in items:
            card = self._card_for(item, now_ms=now_ms, window_end_ms=window_end_ms)
            if card is None:
                continue
            identifier, starts_ms, payload = card
            if self._alerts.already_alerted(identifier, starts_ms):
                continue
            delivery = self._notify(payload)
            if inspect.isawaitable(delivery):
                await delivery
            self._alerts.mark_alerted(identifier, starts_ms, now_us=stamp_us)
            announced += 1
        if announced:
            # Rows for events already underway can never fire again.
            self._alerts.prune(before_unix_ms=now_ms)
        return announced

    def _card_for(
        self,
        item: dict[str, Any],
        *,
        now_ms: int,
        window_end_ms: int,
    ) -> tuple[str, int, Card] | None:
        # Calendar.app can project Scheduled Reminders alongside events. Those
        # belong to the reminder path, and counting them here is exactly the
        # double-announcement this monitor exists to avoid.
        if item.get("entity") == "reminder":
            return None
        # An all-day event has no meaningful "starts in N minutes"; it is
        # already on the day's agenda.
        if item.get("is_all_day") is True:
            return None
        start = item.get("start_unix_ms")
        identifier = item.get("calendar_item_identifier")
        if not isinstance(start, (int, float)) or isinstance(start, bool):
            return None
        if not isinstance(identifier, str) or not identifier:
            return None
        starts_ms = int(start)
        # EventKit's predicate matches anything OVERLAPPING the window, so a
        # long meeting that began this morning would otherwise announce itself
        # as "starting soon" every poll until it ended.
        if starts_ms < now_ms or starts_ms > window_end_ms:
            return None
        title = item.get("title")
        readable = title.strip() if isinstance(title, str) and title.strip() else "Untitled event"
        starts_at = datetime.fromtimestamp(starts_ms / 1_000, self._zone)
        minutes_away = max(0, int((starts_ms - now_ms) // 60_000))
        return identifier, starts_ms, starting_soon_card(readable, starts_at, minutes_away)
