"""Deterministic catch-up loop for JOAT-created Reminder occurrences."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import Enum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..core.protocol import ProductivityHudView
from ..native.bridge import (
    NativeBridgeClient,
    NativeBridgeError,
    NativeBridgeProtocolError,
    NativeBridgeUnavailable,
)
from ..persistence.tasks import TaskOccurrence, TaskOccurrenceStore

log = logging.getLogger(__name__)

_PLANNING_ANCHOR_MAX_UNIX_MS = 32_503_680_000_000


class ReminderNativeState(str, Enum):
    """Result of reconciling one local occurrence with its native Reminder."""

    INCOMPLETE = "incomplete"
    COMPLETED = "completed"
    RESCHEDULED = "rescheduled"


def native_reminder_matches_persisted_due(
    occurrence: TaskOccurrence,
    reminder: dict[str, object],
) -> bool:
    """Whether EventKit still reports the civil time JOAT originally stored.

    The durable receipt retains the exact instant selected at creation.  A
    Reminder query intentionally returns civil components, because EventKit
    does not carry a DST-fold choice in that DTO.  Reusing the original instant
    is therefore sound only while every native civil component and the IANA
    zone match the durable creation receipt.  A user reschedule (including a
    change to an undated/all-day reminder) makes EventKit authoritative and
    retires the old local alarm rather than guessing a new instant.
    """

    due = reminder.get("due")
    if not isinstance(due, dict):
        return False
    try:
        zone = ZoneInfo(occurrence.time_zone_id)
        expected = datetime.fromtimestamp(occurrence.due_unix_ms / 1_000, tz=zone)
    except (OSError, OverflowError, ValueError, ZoneInfoNotFoundError):
        return False
    expected_fields = {
        "year": expected.year,
        "month": expected.month,
        "day": expected.day,
        "hour": expected.hour,
        "minute": expected.minute,
        "second": expected.second,
        "time_zone_id": occurrence.time_zone_id,
    }
    return all(
        isinstance(due.get(field), type(value))
        and not isinstance(due.get(field), bool)
        and due.get(field) == value
        for field, value in expected_fields.items()
    )


def reminder_due_hud_view(
    card: str,
    *,
    planning_anchor_unix_ms: int | None = None,
    reminder_due_id: str | None = None,
) -> ProductivityHudView:
    """Turn a durable due card into the one read-only G2 reminder surface.

    The task monitor owns *when* an alert is eligible.  This helper owns the
    small, typed projection shared by the running Mac app and acceptance
    harnesses, so evidence cannot quietly validate a different alarm screen.
    """

    title = card.removeprefix("!! ").split(" · Reminder\n", 1)[0].strip() or "Reminder"
    anchor = (
        planning_anchor_unix_ms
        if isinstance(planning_anchor_unix_ms, int)
        and not isinstance(planning_anchor_unix_ms, bool)
        and 0 <= planning_anchor_unix_ms <= _PLANNING_ANCHOR_MAX_UNIX_MS
        else None
    )
    return ProductivityHudView(
        domain="reminders",
        phase="due",
        title=title[:72],
        primary="Due now",
        secondary="This reminder is still incomplete.",
        # Every terminal productivity surface returns through the same
        # native-backed Calendar + Reminders schedule.  Calling this a
        # separate Reminders page would send the wearer toward a destination
        # that the HUD router intentionally no longer exposes.
        # A due card is deliberately read-only: completing the Reminder still
        # requires the existing spoken intent and exact confirmation preview.
        # Tell the wearer how to enter that safe path instead of presenting a
        # dead-end alarm that only offers navigation away.
        hint="Say complete reminder title · Back: left x2 Planning",
        planning_anchor_unix_ms=anchor,
        reminder_due_id=reminder_due_id,
    )


async def evaluate_reminder_occurrence(
    occurrence: TaskOccurrence,
    bridge: NativeBridgeClient | None,
) -> ReminderNativeState | None:
    """Return whether the exact native Reminder is still incomplete.

    ``None`` is deliberately fail-quiet: an unavailable EventKit bridge,
    changed store generation, moved list, or ambiguous match must never turn
    into a stale due card.  EventKit's own alarm remains the authoritative
    fallback while the next monitor tick retries reconciliation.
    """

    if bridge is None:
        return None
    try:
        listed = await bridge.call("reminders.listContainers")
        containers = listed.result if isinstance(listed.result, list) else []
        identifiers = [
            item["calendar_identifier"]
            for item in containers
            if isinstance(item, dict)
            and isinstance(item.get("calendar_identifier"), str)
        ]
        if occurrence.calendar_identifier not in identifiers:
            return None
        queried = await bridge.call(
            "reminders.query",
            {
                "calendar_identifiers": [occurrence.calendar_identifier],
                "include_completed": True,
                "limit": 500,
            },
        )
    except (NativeBridgeError, NativeBridgeProtocolError, NativeBridgeUnavailable):
        return None
    if (
        listed.store_instance_id != queried.store_instance_id
        or listed.store_generation != queried.store_generation
    ):
        return None
    reminders = queried.result if isinstance(queried.result, list) else []
    matches = [
        item
        for item in reminders
        if isinstance(item, dict)
        and item.get("calendar_item_identifier") == occurrence.external_item_id
    ]
    if len(matches) != 1:
        return None
    reminder = matches[0]
    completed = reminder.get("is_completed")
    if completed is True:
        return ReminderNativeState.COMPLETED
    if completed is False:
        return (
            ReminderNativeState.INCOMPLETE
            if native_reminder_matches_persisted_due(occurrence, reminder)
            else ReminderNativeState.RESCHEDULED
        )
    return None


class TaskMonitor:
    """Enqueue each persisted due occurrence at least once.

    The durable receipt is deliberately named `alert_enqueued`: it records a
    generation-bound, current admitted Hub-client acceptance, not a macOS
    banner, G2 pixels, or wearer attention. EventKit's own alarm remains the
    primary system reminder.
    """

    def __init__(
        self,
        occurrences: TaskOccurrenceStore,
        deliver: Callable[[ProductivityHudView], bool | Awaitable[bool]],
        *,
        poll_interval_s: float = 15.0,
        evaluate: Callable[
            [TaskOccurrence], Awaitable[ReminderNativeState | None]
        ] | None = None,
    ) -> None:
        if poll_interval_s <= 0:
            raise ValueError("poll interval must be positive")
        self._occurrences = occurrences
        self._deliver = deliver
        self._interval = poll_interval_s
        self._evaluate = evaluate
        self._running = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            await self.run_due_once()
            await asyncio.sleep(self._interval)

    def stop(self) -> None:
        self._running = False

    async def run_due_once(
        self,
        *,
        now_unix_ms: int | None = None,
        now_us: int | None = None,
    ) -> int:
        due_ms = now_unix_ms if now_unix_ms is not None else time.time_ns() // 1_000_000
        receipt_us = now_us if now_us is not None else time.time_ns() // 1_000
        delivered = 0
        for occurrence in self._occurrences.due(now_unix_ms=due_ms):
            if self._evaluate is not None:
                try:
                    native_state = await self._evaluate(occurrence)
                except Exception:
                    log.exception(
                        "task occurrence evaluation failed occurrence=%s",
                        occurrence.occurrence_id,
                    )
                    continue
                if native_state is ReminderNativeState.COMPLETED:
                    self._occurrences.mark_completed(
                        occurrence.occurrence_id,
                        now_us=receipt_us,
                    )
                    continue
                if native_state is ReminderNativeState.RESCHEDULED:
                    self._occurrences.retire_due_tracking(
                        occurrence.occurrence_id,
                        now_us=receipt_us,
                    )
                    continue
                if native_state is not ReminderNativeState.INCOMPLETE:
                    continue
            card = f"!! {occurrence.title} · Reminder\nDue now"
            try:
                # Never expose a raw card delivery seam: due state is a
                # typed, read-only productivity HUD and always returns to the
                # source-backed Planning day for this occurrence.
                payload = reminder_due_hud_view(
                    card,
                    planning_anchor_unix_ms=occurrence.due_unix_ms,
                    reminder_due_id=occurrence.occurrence_id,
                )
                delivery = self._deliver(payload)
                accepted = await delivery if inspect.isawaitable(delivery) else delivery
                if accepted is not True:
                    # The plugin did not send a generation-bound receipt for
                    # this typed due view. Keep it pending for a retry rather
                    # than promoting a local write to an alert claim.
                    log.info(
                        "task alert receipt absent occurrence=%s",
                        occurrence.occurrence_id,
                    )
                    continue
                # The native Reminder may have been completed or rescheduled
                # while the current client painted the due HUD. EventKit stays
                # authoritative at the durable state boundary.
                if self._evaluate is not None:
                    native_state = await self._evaluate(occurrence)
                    if native_state is ReminderNativeState.COMPLETED:
                        self._occurrences.mark_completed(
                            occurrence.occurrence_id,
                            now_us=receipt_us,
                        )
                        continue
                    if native_state is ReminderNativeState.RESCHEDULED:
                        self._occurrences.retire_due_tracking(
                            occurrence.occurrence_id,
                            now_us=receipt_us,
                        )
                        continue
                    if native_state is not ReminderNativeState.INCOMPLETE:
                        continue
            except Exception:
                log.exception(
                    "task alert enqueue failed occurrence=%s",
                    occurrence.occurrence_id,
                )
                continue
            self._occurrences.mark_alert_enqueued(
                occurrence.occurrence_id,
                now_us=receipt_us,
            )
            delivered += 1
        return delivered
