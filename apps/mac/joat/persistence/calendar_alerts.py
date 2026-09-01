"""Durable record of which calendar events already announced themselves.

Scope note: this is CALENDAR ONLY. Reminders/tasks have their own due-alert
path (`services/task_monitor.py` over `task_occurrences`), and Planning is the
merged read-only view of both. Keeping the two alert cursors separate is what
stops one wearer-facing "Planning monitor" from announcing a reminder twice —
once as a task alert and once as a schedule alert.

The key is (event identifier, start time), not the identifier alone: moving a
meeting is a new thing to announce, while a poll that sees the same unmoved
event ten times in a row is not.
"""

from __future__ import annotations

from .store import DurableStore


class CalendarAlertStore:
    """Which (event, start time) pairs have already reached the wearer."""

    def __init__(self, store: DurableStore) -> None:
        self._store = store

    def already_alerted(self, event_identifier: str, starts_at_unix_ms: int) -> bool:
        row = self._store.execute(
            "SELECT 1 FROM calendar_event_alerts "
            "WHERE event_identifier = ? AND starts_at_unix_ms = ?",
            (event_identifier, starts_at_unix_ms),
        ).fetchone()
        return row is not None

    def mark_alerted(
        self,
        event_identifier: str,
        starts_at_unix_ms: int,
        *,
        now_us: int,
    ) -> None:
        """Idempotent: a repeated mark is the same fact, not a conflict."""
        self._store.execute(
            "INSERT INTO calendar_event_alerts "
            "(event_identifier, starts_at_unix_ms, alerted_at_us) VALUES (?, ?, ?) "
            "ON CONFLICT(event_identifier, starts_at_unix_ms) DO NOTHING",
            (event_identifier, starts_at_unix_ms, now_us),
        )

    def prune(self, *, before_unix_ms: int) -> int:
        """Drop rows for events that already started; the wearer cannot be
        warned about them again, so retaining them only grows the table."""
        cursor = self._store.execute(
            "DELETE FROM calendar_event_alerts WHERE starts_at_unix_ms < ?",
            (before_unix_ms,),
        )
        return cursor.rowcount if cursor.rowcount > 0 else 0
