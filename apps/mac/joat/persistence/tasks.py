"""Persistent one-off reminder occurrences for deterministic due catch-up."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import cast

from .store import DurableStore


class TaskOccurrenceError(RuntimeError):
    pass


class TaskOccurrenceConflict(TaskOccurrenceError):
    pass


@dataclass(frozen=True, slots=True)
class TaskOccurrence:
    occurrence_id: str
    action_id: str
    external_item_id: str
    calendar_identifier: str
    title: str
    due_unix_ms: int
    time_zone_id: str
    state: str
    created_at_us: int
    updated_at_us: int
    alerted_at_us: int | None
    completed_at_us: int | None


class TaskOccurrenceStore:
    """One durable occurrence for each JOAT-created nonrecurring Reminder."""

    def __init__(self, store: DurableStore) -> None:
        self._store = store

    def register(
        self,
        *,
        action_id: str,
        external_item_id: str,
        calendar_identifier: str,
        title: str,
        due_unix_ms: int,
        time_zone_id: str,
        now_us: int,
    ) -> TaskOccurrence:
        values = (action_id, external_item_id, calendar_identifier, title, time_zone_id)
        if any(not value or value != value.strip() for value in values):
            raise ValueError("task identifiers, title, and time zone must be non-empty")
        if isinstance(due_unix_ms, bool) or due_unix_ms < 0:
            raise ValueError("due_unix_ms must be non-negative")
        if isinstance(now_us, bool) or now_us < 0:
            raise ValueError("now_us must be non-negative")
        with self._store.transaction() as db:
            existing = db.execute(
                self._select_columns() +
                " FROM task_occurrences t LEFT JOIN task_alert_receipts r "
                "ON r.occurrence_id=t.occurrence_id "
                "WHERE action_id=? OR external_item_id=?",
                (action_id, external_item_id),
            ).fetchall()
            if existing:
                if len(existing) != 1:
                    raise TaskOccurrenceConflict("action and external item name different occurrences")
                occurrence = self._from_row(existing[0])
                if (
                    occurrence.action_id != action_id
                    or occurrence.external_item_id != external_item_id
                    or occurrence.calendar_identifier != calendar_identifier
                    or occurrence.title != title
                    or occurrence.due_unix_ms != due_unix_ms
                    or occurrence.time_zone_id != time_zone_id
                ):
                    raise TaskOccurrenceConflict("occurrence identity names different task data")
                return occurrence
            occurrence_id = str(uuid.uuid4())
            try:
                db.execute(
                    "INSERT INTO task_occurrences("
                    "occurrence_id,action_id,external_system,external_item_id,"
                    "calendar_identifier,title,due_unix_ms,time_zone_id,state,"
                    "created_at_us,updated_at_us) "
                    "VALUES (?,?,'eventkit.reminders',?,?,?,?,?,'pending',?,?)",
                    (
                        occurrence_id,
                        action_id,
                        external_item_id,
                        calendar_identifier,
                        title,
                        due_unix_ms,
                        time_zone_id,
                        now_us,
                        now_us,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise TaskOccurrenceConflict("task occurrence conflicts with durable state") from exc
            return self._get_with(db, occurrence_id)

    def due(self, *, now_unix_ms: int, limit: int = 50) -> tuple[TaskOccurrence, ...]:
        if isinstance(now_unix_ms, bool) or now_unix_ms < 0:
            raise ValueError("now_unix_ms must be non-negative")
        if isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be 1 through 500")
        rows = self._store.execute(
            self._select_columns() +
            " FROM task_occurrences t LEFT JOIN task_alert_receipts r "
            "ON r.occurrence_id=t.occurrence_id "
            "WHERE t.state='pending' AND t.due_unix_ms<=? "
            "ORDER BY t.due_unix_ms,t.occurrence_id LIMIT ?",
            (now_unix_ms, limit),
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def find_by_external_item_id(self, external_item_id: str) -> TaskOccurrence | None:
        """Read the exact durable due instant for one JOAT-created reminder."""

        if not isinstance(external_item_id, str) or not external_item_id.strip():
            raise ValueError("external_item_id must be a non-empty string")
        row = self._store.execute(
            self._select_columns() +
            " FROM task_occurrences t LEFT JOIN task_alert_receipts r "
            "ON r.occurrence_id=t.occurrence_id WHERE t.external_item_id=?",
            (external_item_id,),
        ).fetchone()
        return self._from_row(row) if row is not None else None

    def mark_alert_enqueued(self, occurrence_id: str, *, now_us: int) -> TaskOccurrence:
        with self._store.transaction() as db:
            current = self._get_with(db, occurrence_id)
            if current.state == "alerted":
                return current
            if current.state != "pending":
                raise TaskOccurrenceConflict(f"cannot alert occurrence in {current.state}")
            db.execute(
                "INSERT OR IGNORE INTO task_alert_receipts(occurrence_id,alerted_at_us) "
                "VALUES (?,?)",
                (occurrence_id, now_us),
            )
            db.execute(
                "UPDATE task_occurrences SET state='alerted',updated_at_us=?,alerted_at_us=? "
                "WHERE occurrence_id=? AND state='pending'",
                (now_us, now_us, occurrence_id),
            )
            return self._get_with(db, occurrence_id)

    def mark_completed(self, occurrence_id: str, *, now_us: int) -> TaskOccurrence:
        with self._store.transaction() as db:
            current = self._get_with(db, occurrence_id)
            if current.state == "completed":
                return current
            if current.state in ("dismissed", "cancelled"):
                raise TaskOccurrenceConflict(f"cannot complete occurrence in {current.state}")
            if current.alerted_at_us is not None:
                db.execute(
                    "INSERT OR IGNORE INTO task_alert_receipts(occurrence_id,alerted_at_us) "
                    "VALUES (?,?)",
                    (occurrence_id, current.alerted_at_us),
                )
            db.execute(
                "UPDATE task_occurrences SET state='completed',updated_at_us=?,"
                "completed_at_us=?,alerted_at_us=NULL WHERE occurrence_id=?",
                (now_us, now_us, occurrence_id),
            )
            return self._get_with(db, occurrence_id)

    def retire_due_tracking(self, occurrence_id: str, *, now_us: int) -> TaskOccurrence:
        """Stop JOAT's old local alarm after EventKit reschedules the item.

        This changes only the local occurrence ledger; it never modifies the
        native Reminder.  ``cancelled`` means the stored due occurrence is no
        longer current, not that the wearer cancelled the Reminder itself.
        """

        with self._store.transaction() as db:
            current = self._get_with(db, occurrence_id)
            if current.state in ("completed", "dismissed", "cancelled"):
                return current
            db.execute(
                "UPDATE task_occurrences SET state='cancelled',updated_at_us=?,"
                "alerted_at_us=NULL WHERE occurrence_id=?",
                (now_us, occurrence_id),
            )
            return self._get_with(db, occurrence_id)

    def mark_completed_external_if_present(
        self,
        external_item_id: str,
        *,
        now_us: int,
    ) -> TaskOccurrence | None:
        if not isinstance(external_item_id, str) or not external_item_id.strip():
            return None
        occurrence = self.find_by_external_item_id(external_item_id)
        if occurrence is None:
            return None
        # A native reschedule retires only JOAT's old local alarm.  If the
        # wearer later completes that same native Reminder, bookkeeping must
        # not turn the verified native completion into a false uncertainty.
        if occurrence.state == "cancelled":
            return occurrence
        return self.mark_completed(occurrence.occurrence_id, now_us=now_us)

    @classmethod
    def _get_with(cls, db: sqlite3.Connection, occurrence_id: str) -> TaskOccurrence:
        row = db.execute(
            cls._select_columns() +
            " FROM task_occurrences t LEFT JOIN task_alert_receipts r "
            "ON r.occurrence_id=t.occurrence_id WHERE t.occurrence_id=?",
            (occurrence_id,),
        ).fetchone()
        if row is None:
            raise TaskOccurrenceConflict("unknown task occurrence")
        return cls._from_row(row)

    @staticmethod
    def _select_columns() -> str:
        return (
            "SELECT t.occurrence_id,t.action_id,t.external_item_id,"
            "t.calendar_identifier,t.title,t.due_unix_ms,t.time_zone_id,t.state,"
            "t.created_at_us,t.updated_at_us,"
            "COALESCE(t.alerted_at_us,r.alerted_at_us),t.completed_at_us"
        )

    @staticmethod
    def _from_row(row: tuple[object, ...] | sqlite3.Row) -> TaskOccurrence:
        return TaskOccurrence(
            occurrence_id=cast(str, row[0]),
            action_id=cast(str, row[1]),
            external_item_id=cast(str, row[2]),
            calendar_identifier=cast(str, row[3]),
            title=cast(str, row[4]),
            due_unix_ms=cast(int, row[5]),
            time_zone_id=cast(str, row[6]),
            state=cast(str, row[7]),
            created_at_us=cast(int, row[8]),
            updated_at_us=cast(int, row[9]),
            alerted_at_us=cast(int, row[10]) if row[10] is not None else None,
            completed_at_us=cast(int, row[11]) if row[11] is not None else None,
        )
