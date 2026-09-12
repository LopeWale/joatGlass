"""Durable raw notification observations, cursors, and enqueue outbox."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, cast

from ..channels.event_taxonomy import classify_event_class
from ..channels.notifications import Notification
from .store import DurableStore


class NotificationPersistenceConflict(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PendingNotification:
    observation_id: str
    source_id: str
    coverage_scope: str
    authority_level: str
    completeness: str
    observed_at_us: int
    notification: Notification


class NotificationObservationStore:
    """Atomically retain every source observation and advance its cursor."""

    def __init__(self, store: DurableStore) -> None:
        self._store = store

    def initialize_source(
        self,
        *,
        source_id: str,
        coverage_scope: str,
        authority_level: str,
        baseline: Any,
        health: str,
        now_us: int,
    ) -> Any:
        cursor = self._json(baseline)
        with self._store.transaction() as db:
            row = db.execute(
                "SELECT coverage_scope,authority_level,cursor_json FROM notification_sources "
                "WHERE source_id=?",
                (source_id,),
            ).fetchone()
            if row is not None:
                if str(row[0]) != coverage_scope or str(row[1]) != authority_level:
                    raise NotificationPersistenceConflict("source identity changed meaning")
                db.execute(
                    "UPDATE notification_sources SET health=?,updated_at_us=? WHERE source_id=?",
                    (health, now_us, source_id),
                )
                return self._decode(bytes(row[2]))
            db.execute(
                "INSERT INTO notification_sources(source_id,coverage_scope,authority_level,"
                "cursor_json,health,initialized_at_us,updated_at_us) VALUES (?,?,?,?,?,?,?)",
                (source_id, coverage_scope, authority_level, cursor, health, now_us, now_us),
            )
        return baseline

    def commit_batch(
        self,
        *,
        source_id: str,
        cursor_before: Any,
        cursor_after: Any,
        notifications: list[Notification],
        event_class: str | None,
        completeness: str,
        observed_at_us: int,
    ) -> int:
        before = self._json(cursor_before)
        after = self._json(cursor_after)
        inserted = 0
        with self._store.transaction() as db:
            row = db.execute(
                "SELECT cursor_json FROM notification_sources WHERE source_id=?",
                (source_id,),
            ).fetchone()
            if row is None or bytes(row[0]) != before:
                raise NotificationPersistenceConflict("notification source cursor changed")
            for notification in notifications:
                payload = self._payload(notification)
                digest = hashlib.sha256(payload).digest()
                native_id = notification.native_id or f"{notification.rec_id}:{digest.hex()}"
                observation_id = str(uuid.uuid4())
                classification = event_class or self.event_class(notification)
                changed = db.execute(
                    "INSERT OR IGNORE INTO notification_observations("
                    "observation_id,source_id,native_id,event_class,occurred_at_us,"
                    "observed_at_us,payload,payload_digest,completeness) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        observation_id,
                        source_id,
                        native_id,
                        classification,
                        int(notification.delivered_unix * 1_000_000)
                        if notification.delivered_unix > 0 else None,
                        observed_at_us,
                        payload,
                        digest,
                        completeness,
                    ),
                ).rowcount
                if changed:
                    db.execute(
                        "INSERT INTO notification_outbox(observation_id,state,updated_at_us) "
                        "VALUES (?,'pending',?)",
                        (observation_id, observed_at_us),
                    )
                    inserted += 1
            db.execute(
                "UPDATE notification_sources SET cursor_json=?,updated_at_us=? WHERE source_id=?",
                (after, observed_at_us, source_id),
            )
        return inserted

    def set_health(self, source_id: str, health: str, *, now_us: int) -> None:
        self._store.execute(
            "UPDATE notification_sources SET health=?,updated_at_us=? WHERE source_id=?",
            (health, now_us, source_id),
        )

    def mark_starting(self, source_id: str, *, now_us: int) -> None:
        """Withdraw stale readiness before restart catch-up begins."""

        self.set_health(source_id, "stale", now_us=now_us)

    def prune(self, *, before_us: int, limit: int = 500) -> int:
        """Drop one bounded batch of settled observations older than ``before_us``.

        Nothing ever removed rows from this table, and the cross-source
        duplicate-suppression queries filter it on every poll. Left to grow it
        makes each incoming notification a little more expensive than the
        last — the wearer experiences that as JOAT getting slower the longer
        it runs.

        Only SETTLED rows go: anything still `pending` in the outbox has not
        reached the wearer yet. The outbox row follows by cascade, and the
        retained window must stay wider than the suppression lookup's own
        window or a duplicate could slip through behind the deletion.
        """
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("prune limit must be between 1 and 500")
        cursor = self._store.execute(
            "DELETE FROM notification_observations WHERE observation_id IN ("
            "  SELECT o.observation_id FROM notification_observations o "
            "  JOIN notification_outbox x ON x.observation_id=o.observation_id "
            "  WHERE x.state != 'pending' "
            "  AND COALESCE(o.occurred_at_us,o.observed_at_us) < ? LIMIT ?"
            ")",
            (before_us, limit),
        )
        return cursor.rowcount if cursor.rowcount > 0 else 0

    def pending(self, *, limit: int = 50) -> tuple[PendingNotification, ...]:
        rows = self._store.execute(
            "SELECT o.observation_id,o.source_id,s.coverage_scope,s.authority_level,"
            "o.completeness,o.observed_at_us,o.payload,o.payload_digest "
            "FROM notification_outbox x "
            "JOIN notification_observations o ON o.observation_id=x.observation_id "
            "JOIN notification_sources s ON s.source_id=o.source_id "
            "WHERE x.state='pending' "
            "ORDER BY COALESCE(o.occurred_at_us,o.observed_at_us),o.observation_id LIMIT ?",
            (limit,),
        ).fetchall()
        return tuple(
            PendingNotification(
                observation_id=cast(str, row[0]),
                source_id=cast(str, row[1]),
                coverage_scope=cast(str, row[2]),
                authority_level=cast(str, row[3]),
                completeness=cast(str, row[4]),
                observed_at_us=cast(int, row[5]),
                notification=self._notification(bytes(row[6]), bytes(row[7])),
            )
            for row in rows
        )

    def mark_enqueued(self, observation_id: str, *, now_us: int) -> None:
        self._mark(observation_id, "enqueued", "hud_enqueue_accepted", now_us)

    def mark_suppressed(
        self,
        observation_id: str,
        *,
        reason_code: str,
        now_us: int,
    ) -> None:
        self._mark(observation_id, "suppressed", reason_code, now_us)

    def source_healths(self, source_ids: tuple[str, ...]) -> dict[str, str]:
        if not source_ids:
            return {}
        placeholders = ",".join("?" for _ in source_ids)
        return {
            str(source_id): str(health)
            for source_id, health in self._store.execute(
                f"SELECT source_id,health FROM notification_sources "
                f"WHERE source_id IN ({placeholders})",
                source_ids,
            ).fetchall()
        }

    def has_healthy_authority(self, coverage_scope: str) -> bool:
        return self._store.execute(
            "SELECT 1 FROM notification_sources WHERE coverage_scope=? "
            "AND authority_level='authoritative' "
            "AND health='healthy_complete_for_scope' LIMIT 1",
            (coverage_scope,),
        ).fetchone() is not None

    def authority_readiness(self, coverage_scope: str) -> str:
        rows = self._store.execute(
            "SELECT health FROM notification_sources WHERE coverage_scope=? "
            "AND authority_level='authoritative'",
            (coverage_scope,),
        ).fetchall()
        states = {str(row[0]) for row in rows}
        if not states:
            return "absent"
        if "stale" in states:
            return "catching_up"
        if "healthy_complete_for_scope" in states:
            return "healthy"
        return "unavailable"

    def suppress_if_matched_authoritative(
        self,
        pending: PendingNotification,
        coverage_scope: str,
        *,
        now_us: int,
        window_seconds: float = 30.0,
    ) -> bool:
        """Consume one exact authoritative match for one fallback observation.

        Cardinality matters: one authoritative row can suppress only one
        fallback appearance. The claim and state change share one transaction.
        """
        notification = pending.notification
        if notification.delivered_unix <= 0 or window_seconds <= 0:
            return False
        center = int(notification.delivered_unix * 1_000_000)
        radius = int(window_seconds * 1_000_000)
        event_class = self.event_class(notification)
        with self._store.transaction() as db:
            authoritative = db.execute(
                "SELECT o.payload,o.payload_digest FROM notification_observations o "
                "JOIN notification_sources s ON s.source_id=o.source_id "
                "WHERE s.coverage_scope=? AND s.authority_level='authoritative' "
                "AND s.health='healthy_complete_for_scope' "
                "AND o.event_class=? AND o.occurred_at_us BETWEEN ? AND ?",
                (coverage_scope, event_class, center - radius, center + radius),
            ).fetchall()
            authoritative_count = self._matching_count(authoritative, notification)
            if authoritative_count == 0:
                return False
            consumed = db.execute(
                "SELECT o.payload,o.payload_digest FROM notification_observations o "
                "JOIN notification_sources s ON s.source_id=o.source_id "
                "JOIN notification_outbox x ON x.observation_id=o.observation_id "
                "WHERE s.authority_level='best_effort' AND o.event_class=? "
                "AND o.occurred_at_us BETWEEN ? AND ? "
                "AND x.state='suppressed' "
                "AND x.reason_code='matched_authoritative_observation'",
                (event_class, center - radius, center + radius),
            ).fetchall()
            if self._matching_count(consumed, notification) >= authoritative_count:
                return False
            changed = db.execute(
                "UPDATE notification_outbox SET state='suppressed',reason_code=?,"
                "updated_at_us=? WHERE observation_id=? AND state='pending'",
                ("matched_authoritative_observation", now_us, pending.observation_id),
            ).rowcount
            if changed != 1:
                raise NotificationPersistenceConflict(
                    "notification outbox item is not pending"
                )
            return True

    @classmethod
    def _matching_count(
        cls,
        rows: list[tuple[bytes, bytes]],
        notification: Notification,
    ) -> int:
        matches = 0
        for payload, digest in rows:
            candidate = cls._notification(bytes(payload), bytes(digest))
            if (
                candidate.title == notification.title
                and candidate.subtitle == notification.subtitle
                and candidate.body == notification.body
            ):
                matches += 1
        return matches

    def _mark(self, observation_id: str, state: str, reason: str, now_us: int) -> None:
        changed = self._store.execute(
            "UPDATE notification_outbox SET state=?,reason_code=?,updated_at_us=? "
            "WHERE observation_id=? AND state='pending'",
            (state, reason, now_us, observation_id),
        ).rowcount
        if changed != 1:
            raise NotificationPersistenceConflict("notification outbox item is not pending")

    @staticmethod
    def event_class(notification: Notification) -> str:
        """One taxonomy, shared with the card producer — see event_taxonomy."""
        return classify_event_class(notification.app, notification.title)

    @staticmethod
    def _payload(notification: Notification) -> bytes:
        return NotificationObservationStore._json({
            "rec_id": notification.rec_id,
            "app": notification.app,
            "title": notification.title,
            "subtitle": notification.subtitle,
            "body": notification.body,
            "delivered_unix": notification.delivered_unix,
            "native_id": notification.native_id,
        })

    @staticmethod
    def _notification(payload: bytes, expected_digest: bytes) -> Notification:
        if hashlib.sha256(payload).digest() != expected_digest:
            raise NotificationPersistenceConflict(
                "stored notification payload failed its digest check"
            )
        value = NotificationObservationStore._decode(payload)
        if not isinstance(value, dict):
            raise NotificationPersistenceConflict("stored notification payload is malformed")
        return Notification(
            rec_id=int(value["rec_id"]),
            app=str(value["app"]),
            title=str(value["title"]),
            subtitle=str(value["subtitle"]),
            body=str(value["body"]),
            delivered_unix=float(value["delivered_unix"]),
            native_id=(str(value["native_id"]) if value.get("native_id") is not None else None),
        )

    @staticmethod
    def _json(value: Any) -> bytes:
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise NotificationPersistenceConflict("source cursor is not JSON serializable") from exc

    @staticmethod
    def _decode(value: bytes) -> Any:
        try:
            return json.loads(value)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise NotificationPersistenceConflict("stored cursor or payload is malformed") from exc
