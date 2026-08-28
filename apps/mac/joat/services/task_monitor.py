"""Deterministic catch-up loop for JOAT-created Reminder occurrences."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable

from ..persistence.tasks import TaskOccurrence, TaskOccurrenceStore

log = logging.getLogger(__name__)


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
