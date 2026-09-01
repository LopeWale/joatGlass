"""Independent source ingestion with durable cursors and a shared HUD outbox."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..core.interfaces import ReplyEngine
from ..persistence.notifications import NotificationObservationStore, PendingNotification
from .cards import Card
from .notifications import NotificationChannel, NotificationSource

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NotificationLane:
    source_id: str
    source: NotificationSource
    coverage_scope: str
    authority_level: str
    healthy_state: str
    completeness: str
    event_class: str | None = None


class NotificationMux:
    """Fan in sources independently; slow triage cannot stall ingestion."""

    def __init__(
        self,
        lanes: list[NotificationLane],
        observations: NotificationObservationStore,
        *,
        triage: ReplyEngine | None,
        deliver: Callable[[Card], None | Awaitable[None]],
        status: object | None = None,
        poll_interval_s: float = 1.0,
        authority_grace_s: float = 1.25,
    ) -> None:
        if not lanes:
            raise ValueError("notification mux requires at least one source lane")
        if authority_grace_s < 0:
            raise ValueError("authority grace must be non-negative")
        self._lanes = lanes
        self._observations = observations
        self._handler = NotificationChannel(
            lanes[0].source,
            triage,
            deliver,
            status=status,
        )
        self._status = status
        self._interval = poll_interval_s
        self._authority_grace_s = authority_grace_s
        self._running = False

    async def run(self) -> None:
        self._running = True
        starting_at_us = time.time_ns() // 1_000
        for lane in self._lanes:
            self._observations.mark_starting(
                lane.source_id,
                now_us=starting_at_us,
            )
        async with asyncio.TaskGroup() as group:
            for lane in self._lanes:
                group.create_task(self._ingest(lane))
            group.create_task(self._deliver())

    def stop(self) -> None:
        self._running = False

    async def _ingest(self, lane: NotificationLane) -> None:
        cursor = None
        while self._running and cursor is None:
            try:
                baseline = await asyncio.to_thread(lane.source.baseline)
                cursor = self._observations.initialize_source(
                    source_id=lane.source_id,
                    coverage_scope=lane.coverage_scope,
                    authority_level=lane.authority_level,
                    baseline=baseline,
                    health="stale",
                    now_us=time.time_ns() // 1_000,
                )
            except Exception as exc:
                self._observations.set_health(
                    lane.source_id,
                    "failed",
                    now_us=time.time_ns() // 1_000,
                )
                log.warning(
                    "notification source=%s startup unavailable error=%s",
                    lane.source_id,
                    type(exc).__name__,
                )
                self._publish_status()
                await asyncio.sleep(max(5.0, self._interval))
        if cursor is None:
            return
        self._publish_status()
        while self._running:
            await asyncio.sleep(self._interval)
            before = cursor
            try:
                fresh, cursor = await asyncio.to_thread(lane.source.read_new, before)
                self._observations.commit_batch(
                    source_id=lane.source_id,
                    cursor_before=before,
                    cursor_after=cursor,
                    notifications=fresh,
                    event_class=lane.event_class,
                    completeness=lane.completeness,
                    observed_at_us=time.time_ns() // 1_000,
                )
                health = lane.healthy_state
                health_after_read = getattr(lane.source, "health_after_read", None)
                if callable(health_after_read):
                    health = str(health_after_read(health))
                self._observations.set_health(
                    lane.source_id,
                    health,
                    now_us=time.time_ns() // 1_000,
                )
            except Exception as exc:
                cursor = before
                self._observations.set_health(
                    lane.source_id,
                    "failed",
                    now_us=time.time_ns() // 1_000,
                )
                log.warning("notification source=%s poll failed error=%s", lane.source_id, type(exc).__name__)
            self._publish_status()

    async def _deliver(self) -> None:
        while self._running:
            pending = self._observations.pending(limit=50)
            if not pending:
                await asyncio.sleep(self._interval)
                continue
            deferred = False
            for observation in pending:
                if observation.completeness == "metadata_only":
                    self._observations.mark_suppressed(
                        observation.observation_id,
                        reason_code="metadata_only_not_renderable",
                        now_us=time.time_ns() // 1_000,
                    )
                    continue
                if self._suppress_best_effort_duplicate(observation):
                    continue
                if self._defer_for_authority(observation):
                    deferred = True
                    continue
                try:
                    enqueued = await self._handler._handle(observation.notification)
                except Exception as exc:
                    log.warning(
                        "notification enqueue failed source=%s error=%s",
                        observation.source_id,
                        type(exc).__name__,
                    )
                    await asyncio.sleep(self._interval)
                    continue
                if enqueued:
                    self._observations.mark_enqueued(
                        observation.observation_id,
                        now_us=time.time_ns() // 1_000,
                    )
                else:
                    self._observations.mark_suppressed(
                        observation.observation_id,
                        reason_code="triage_or_policy_suppressed",
                        now_us=time.time_ns() // 1_000,
                    )
            if deferred:
                await asyncio.sleep(min(self._interval, max(0.01, self._authority_grace_s)))

    def _suppress_best_effort_duplicate(self, pending: PendingNotification) -> bool:
        return (
            pending.authority_level == "best_effort"
            and self._observations.event_class(pending.notification) == "messages.incoming_text"
            and self._observations.suppress_if_matched_authoritative(
                pending,
                "messages.incoming_text",
                now_us=time.time_ns() // 1_000,
            )
        )

    def _defer_for_authority(self, pending: PendingNotification) -> bool:
        if (
            pending.authority_level != "best_effort"
            or self._observations.event_class(pending.notification)
            != "messages.incoming_text"
        ):
            return False
        readiness = self._observations.authority_readiness(
            "messages.incoming_text"
        )
        if readiness == "catching_up":
            return True
        if readiness != "healthy" or self._authority_grace_s == 0:
            return False
        age_us = time.time_ns() // 1_000 - pending.observed_at_us
        return age_us < int(self._authority_grace_s * 1_000_000)

    def _publish_status(self) -> None:
        if self._status is not None:
            states = self._observations.source_healths(
                tuple(lane.source_id for lane in self._lanes)
            )
            healthy = sum(
                state in ("healthy_complete_for_scope", "healthy_best_effort")
                for state in states.values()
            )
            total = len(self._lanes)
            if healthy == total:
                value = "live"
            elif healthy:
                value = f"partial {healthy}/{total}"
            else:
                value = "unavailable"
            self._status.set(notifications=value)  # type: ignore[attr-defined]
