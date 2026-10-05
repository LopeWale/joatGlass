"""One exact Timer checkpoint on the process-owned durable database.

The checkpoint carries the wearer's configured focus and break lengths
alongside the phase. The lengths live in their own single-row table
(`pomodoro_lengths`, migration 13): migration 11's checkpoint table is
checksummed history, so a database from before the lengths were
configurable simply has no lengths row and loads the classic 25/5/15 cycle.
"""

from __future__ import annotations

from dataclasses import dataclass

from .store import DurableStore, StorageError

DEFAULT_FOCUS_MS = 25 * 60_000
DEFAULT_SHORT_BREAK_MS = 5 * 60_000
DEFAULT_LONG_BREAK_MS = 15 * 60_000
_MAX_LENGTH_MS = 24 * 3_600_000
_LENGTH_FIELDS = ("focus_ms", "short_break_ms", "long_break_ms")


class PomodoroStateError(StorageError):
    """Persisted Timer state cannot be safely restored."""


@dataclass(frozen=True, slots=True)
class PomodoroCheckpoint:
    phase: str
    running: bool
    remaining_ms: int
    length_ms: int
    completed: int
    ends_at_unix_ms: int | None
    saved_at_unix_ms: int
    # The configured cycle rides with the checkpoint. Defaults keep a row
    # saved before the lengths were configurable loading unchanged.
    focus_ms: int = DEFAULT_FOCUS_MS
    short_break_ms: int = DEFAULT_SHORT_BREAK_MS
    long_break_ms: int = DEFAULT_LONG_BREAK_MS


def _require_integer(name: str, value: int) -> None:
    if type(value) is not int or not 0 <= value < 2**63:
        raise ValueError(f"{name} must be a non-negative SQLite integer")


def _require_length(name: str, value: int) -> None:
    _require_integer(name, value)
    # Restored state must fit HudPanelPomodoro's existing wire contract.
    if not 1 <= value <= _MAX_LENGTH_MS:
        raise ValueError(f"{name} must be positive and at most 24 hours")


def _validate(checkpoint: PomodoroCheckpoint) -> None:
    if not isinstance(checkpoint, PomodoroCheckpoint):
        raise ValueError("checkpoint must be a PomodoroCheckpoint")
    if type(checkpoint.phase) is not str or checkpoint.phase not in (
        "idle", "focus", "break", "long_break",
    ):
        raise ValueError("invalid Timer phase")
    if type(checkpoint.running) is not bool:
        raise ValueError("running must be a boolean")
    for name in ("remaining_ms", "length_ms", "completed", "saved_at_unix_ms"):
        _require_integer(name, getattr(checkpoint, name))
    _require_length("length_ms", checkpoint.length_ms)
    for name in _LENGTH_FIELDS:
        _require_length(name, getattr(checkpoint, name))
    if checkpoint.remaining_ms > checkpoint.length_ms:
        raise ValueError("remaining_ms must not exceed length_ms")
    completed_range = {
        "idle": range(1),
        "focus": range(4),
        "break": range(1, 4),
        "long_break": range(4, 5),
    }[checkpoint.phase]
    if checkpoint.completed not in completed_range:
        raise ValueError("completed count does not match the Timer phase")
    if checkpoint.ends_at_unix_ms is not None:
        _require_integer("ends_at_unix_ms", checkpoint.ends_at_unix_ms)
    if checkpoint.running:
        if checkpoint.phase == "idle" or checkpoint.ends_at_unix_ms is None:
            raise ValueError("a running Timer requires an active phase and deadline")
    elif checkpoint.ends_at_unix_ms is not None:
        raise ValueError("a paused or idle Timer must not have a deadline")


class PomodoroStore:
    """Read and atomically replace the single checkpoint without owning a writer."""

    def __init__(self, store: DurableStore) -> None:
        self._store = store

    def load(self) -> PomodoroCheckpoint | None:
        rows = self._store.execute(
            "SELECT id,phase,running,remaining_ms,length_ms,completed,"
            "ends_at_unix_ms,saved_at_unix_ms FROM pomodoro_checkpoint LIMIT 2"
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1 or type(rows[0][0]) is not int or rows[0][0] != 1:
            raise PomodoroStateError("invalid Timer checkpoint slot")
        _, phase, running, remaining, length, completed, ends_at, saved_at = rows[0]
        if type(running) is not int or running not in (0, 1):
            raise PomodoroStateError("invalid Timer checkpoint running state")
        focus, short_break, long_break = self._load_lengths()
        checkpoint = PomodoroCheckpoint(
            phase=phase,
            running=bool(running),
            remaining_ms=remaining,
            length_ms=length,
            completed=completed,
            ends_at_unix_ms=ends_at,
            saved_at_unix_ms=saved_at,
            focus_ms=focus,
            short_break_ms=short_break,
            long_break_ms=long_break,
        )
        try:
            _validate(checkpoint)
        except ValueError as exc:
            raise PomodoroStateError("invalid Timer checkpoint fields") from exc
        return checkpoint

    def _load_lengths(self) -> tuple[int, int, int]:
        rows = self._store.execute(
            "SELECT id,focus_ms,short_break_ms,long_break_ms FROM pomodoro_lengths LIMIT 2"
        ).fetchall()
        if not rows:
            # Saved before the lengths were configurable: the classic cycle.
            return DEFAULT_FOCUS_MS, DEFAULT_SHORT_BREAK_MS, DEFAULT_LONG_BREAK_MS
        if len(rows) != 1 or type(rows[0][0]) is not int or rows[0][0] != 1:
            raise PomodoroStateError("invalid Timer lengths slot")
        _, focus, short_break, long_break = rows[0]
        return focus, short_break, long_break

    def save(self, checkpoint: PomodoroCheckpoint) -> None:
        _validate(checkpoint)
        with self._store.transaction() as db:
            db.execute(
                "INSERT INTO pomodoro_checkpoint("
                "id,phase,running,remaining_ms,length_ms,completed,"
                "ends_at_unix_ms,saved_at_unix_ms) VALUES (1,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "phase=excluded.phase,running=excluded.running,"
                "remaining_ms=excluded.remaining_ms,length_ms=excluded.length_ms,"
                "completed=excluded.completed,ends_at_unix_ms=excluded.ends_at_unix_ms,"
                "saved_at_unix_ms=excluded.saved_at_unix_ms",
                (
                    checkpoint.phase,
                    int(checkpoint.running),
                    checkpoint.remaining_ms,
                    checkpoint.length_ms,
                    checkpoint.completed,
                    checkpoint.ends_at_unix_ms,
                    checkpoint.saved_at_unix_ms,
                ),
            )
            db.execute(
                "INSERT INTO pomodoro_lengths(id,focus_ms,short_break_ms,long_break_ms) "
                "VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "focus_ms=excluded.focus_ms,short_break_ms=excluded.short_break_ms,"
                "long_break_ms=excluded.long_break_ms",
                (checkpoint.focus_ms, checkpoint.short_break_ms, checkpoint.long_break_ms),
            )
