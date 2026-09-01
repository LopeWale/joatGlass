"""Announce a calendar event that is about to start.

Scope, deliberately narrow: this watches CALENDAR EVENTS only. It is not a
"Planning monitor". Planning is the merged read-only view of two different
native stores, and the other half — reminders/tasks — already has its own
due-alert path with its own acknowledged delivery contract
(`services/task_monitor.py`). A monitor spanning both would announce a
reminder twice and blur two things the wearer keeps separate: a schedule that
starts, and a task that is due.

It also stays on the ordinary notification seam, so an event heads-up obeys the
same coalescing, rate cap, and never-interrupt-a-session policy as any other
card, rather than inventing a second way to seize the lenses. That means going
through a `DeliveryPolicy` before delivery — reaching `SessionManager` directly
would make calendar the one card kind exempt from the rate cap, and a poll that
returned six qualifying events would paint six frames in one tick.

Ordering matters here: the durable "already announced" cursor is committed only
AFTER an awaited delivery reports the write completed. Committing it after a
fire-and-forget hand-off would make a dropped card unrepeatable, because a
marked event is never re-offered. Failing the other way costs at most a
repeated announcement, and even that is bounded — an event leaves the lead
window as soon as it starts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, tzinfo
from typing import Any, Protocol

from ..channels.cards import Card, DeliveryPolicy, Urgency
from ..persistence.calendar_alerts import CalendarAlertStore

log = logging.getLogger(__name__)

# How far ahead to look. Long enough to stand up and walk somewhere, short
# enough that the HUD is not previewing the whole afternoon.
DEFAULT_LEAD_S = 600.0
# The window moves slowly, so the poll can too — a minute of latency on a
# ten-minute warning is invisible, and this shares the native bridge with
# everything else.
DEFAULT_POLL_INTERVAL_S = 60.0
# A card queued behind a live Brainstorm resolves only when that session ends.
# Bound the wait so one long session cannot stall the poll loop indefinitely;
# an undelivered event stays unmarked and is simply re-offered.
DEFAULT_DELIVERY_TIMEOUT_S = 20.0

_EVENT_QUERY_LIMIT = 100


class CalendarQueryClient(Protocol):
    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any: ...


def starting_soon_card(
    title: str,
    starts_at: datetime,
    minutes_away: int,
    *,
    origin_id: str | None = None,
) -> Card:
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
        # The producing store's own identity for this occurrence, so history
        # can tell two announcements apart the way every other producer can.
        origin_id=origin_id,
    )


class CalendarMonitor:
    """Poll a short forward window and announce each event once."""

    def __init__(
        self,
        client: CalendarQueryClient,
        alerts: CalendarAlertStore,
        notify: Callable[[Card], Awaitable[None]],
        *,
        connected: Callable[[], bool],
        zone: tzinfo | None,
        lead_s: float = DEFAULT_LEAD_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        policy: DeliveryPolicy | None = None,
        delivery_timeout_s: float = DEFAULT_DELIVERY_TIMEOUT_S,
    ) -> None:
        if lead_s <= 0:
            raise ValueError("lead must be positive")
        if poll_interval_s <= 0:
            raise ValueError("poll interval must be positive")
        if delivery_timeout_s <= 0:
            raise ValueError("delivery timeout must be positive")
        self._client = client
        self._alerts = alerts
        self._notify = notify
        # Awaiting the HUD write is NOT proof the wearer saw anything: the
        # transport logs and returns normally when no plugin is connected, so
        # a normal return would otherwise commit the cursor for a frame that
        # was discarded. This is guaranteed at every launch, because the poll
        # loop starts before the gateway accepts its first client.
        self._connected = connected
        self._zone = zone
        self._lead_s = lead_s
        self._interval = poll_interval_s
        # The same admission RULES every other card passes — coalescing plus
        # the per-minute cap — via this monitor's own policy instance. The
        # budget is per-producer, not global: calendar and notifications can
        # together exceed six frames/minute in a pathological burst. A truly
        # global budget would mean sharing one DeliveryPolicy across
        # subsystems; accepted as-is because each producer alone stays
        # bounded, and admit() runs before delivery, so a failed delivery has
        # already consumed budget and coalescing state (cost: a retried
        # announcement may render with an inflated "(2)" count — cosmetic).
        self._policy = policy or DeliveryPolicy()
        self._delivery_timeout_s = delivery_timeout_s
        self._running = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            # The durable-store reads and writes inside run_once are not
            # guarded there. An unguarded raise here would end the task for
            # the rest of the process — silently, since nothing awaits it —
            # so one sqlite hiccup would cost every future schedule alert.
            try:
                await self.run_once()
            except Exception:
                log.exception("calendar starting-soon poll failed; continuing")
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
        if not self._connected():
            # Nothing can be announced, so nothing may be recorded as
            # announced. Staying quiet here is what keeps the wearer's first
            # meeting of the day from being consumed by the startup poll.
            log.info("calendar starting-soon skipped: no glasses client connected")
            return 0
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
            admitted = self._policy.admit(payload)
            if admitted is None:
                # Rate-capped (admit never returns None for coalescing — a
                # coalesced repeat comes back as a replacement card).
                # Deliberately NOT marked: the wearer never saw it, so it
                # stays eligible for the next tick.
                log.info("calendar starting-soon suppressed by delivery policy")
                continue
            try:
                async with asyncio.timeout(self._delivery_timeout_s):
                    await self._notify(admitted)
            # CancelledError is a BaseException and deliberately passes
            # through, so shutdown still cancels the poll promptly.
            except Exception as exc:
                # An un-awaited hand-off would let a dropped card be marked and
                # therefore never re-offered. Leave it unmarked instead: the
                # cost is a possible repeat, bounded by the lead window.
                log.info(
                    "calendar starting-soon not delivered (%s); leaving it unannounced",
                    type(exc).__name__,
                )
                continue
            if not self._connected():
                # The client dropped somewhere between the tick's gate and
                # this write completing, so the "successful" paint may have
                # been silently discarded. Prefer a possible duplicate next
                # tick over recording an announcement nobody saw.
                log.info("calendar starting-soon delivery raced a disconnect; not marking")
                continue
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
        # already on the day's agenda. The bridge DTO always sends a real
        # bool, so anything else means the payload cannot be trusted to say
        # whether this is a timed event — and announcing "starts in 8 min"
        # for an all-day item is worse than staying quiet.
        all_day = item.get("is_all_day")
        if not isinstance(all_day, bool):
            log.info("calendar item skipped: is_all_day missing or not a bool")
            return None
        if all_day:
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
        # A None zone means system local time, resolved now rather than at
        # construction, so the rendered clock time survives a DST change.
        starts_at = datetime.fromtimestamp(starts_ms / 1_000, self._zone)
        minutes_away = max(0, int((starts_ms - now_ms) // 60_000))
        return identifier, starts_ms, starting_soon_card(
            readable, starts_at, minutes_away, origin_id=f"{identifier}:{starts_ms}",
        )
