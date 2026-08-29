"""Deterministic catch-up loop for JOAT-created Reminder occurrences."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable

from ..core.protocol import ProductivityHudView
from ..native.bridge import (
    NativeBridgeClient,
    NativeBridgeError,
    NativeBridgeProtocolError,
    NativeBridgeUnavailable,
)
from ..persistence.tasks import TaskOccurrence, TaskOccurrenceStore

log = logging.getLogger(__name__)


def reminder_due_hud_view(card: str) -> ProductivityHudView:
    """Turn a durable due card into the one read-only G2 reminder surface.

    The task monitor owns *when* an alert is eligible.  This helper owns the
    small, typed projection shared by the running Mac app and acceptance
    harnesses, so evidence cannot quietly validate a different alarm screen.
    """

    title = card.removeprefix("!! ").split(" · Reminder\n", 1)[0].strip() or "Reminder"
    return ProductivityHudView(
        domain="reminders",
        phase="due",
        title=title[:72],
        primary="Due now",
        secondary="This reminder is still incomplete.",
        hint="left x2 Reminders",
    )


async def evaluate_reminder_occurrence(
    occurrence: TaskOccurrence,
    bridge: NativeBridgeClient | None,
) -> bool | None:
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
    completed = matches[0].get("is_completed")
    if completed is True:
        return False
    if completed is False:
        return True
    return None


class TaskMonitor:
    """Enqueue each persisted due occurrence at least once.

    The durable receipt is deliberately named `alert_enqueued`: until the Hub
    protocol has a render acknowledgement it is not evidence that the wearer
    saw the card. EventKit's own alarm remains the primary system reminder.
    """

    def __init__(
        self,
        occurrences: TaskOccurrenceStore,
        deliver: Callable[[str], None | Awaitable[None]],
        *,
        poll_interval_s: float = 15.0,
        evaluate: Callable[[TaskOccurrence], Awaitable[bool | None]] | None = None,
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
                    still_incomplete = await self._evaluate(occurrence)
                except Exception:
                    log.exception(
                        "task occurrence evaluation failed occurrence=%s",
                        occurrence.occurrence_id,
                    )
                    continue
                if still_incomplete is False:
                    self._occurrences.mark_completed(
                        occurrence.occurrence_id,
                        now_us=receipt_us,
                    )
                    continue
                if still_incomplete is None:
                    continue
            card = f"!! {occurrence.title} · Reminder\nDue now"
            try:
                delivery = self._deliver(card)
                if inspect.isawaitable(delivery):
                    await delivery
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
