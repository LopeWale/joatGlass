"""Timer checkpoints on temporary, process-owned databases only."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from joat.persistence import pomodoro
from joat.persistence.store import DurableStore, StorageError


@pytest.fixture
def api() -> Any:
    return pomodoro


def checkpoint(api: Any, **changes: Any) -> Any:
    fields = dict(phase="focus", running=True, remaining_ms=50_000,
                  length_ms=60_000, completed=0, ends_at_unix_ms=150_000,
                  saved_at_unix_ms=100_000)
    fields.update(changes)
    return api.PomodoroCheckpoint(**fields)


def test_empty_load_has_no_write_and_checkpoint_is_immutable(api: Any, tmp_path: Path) -> None:
    with DurableStore(tmp_path / "timer.sqlite3") as db:
        repository = api.PomodoroStore(db)
        before = db.execute("SELECT total_changes()").fetchone()
        assert repository.load() is None
        assert db.execute("SELECT total_changes()").fetchone() == before
        value = checkpoint(api)
        with pytest.raises(FrozenInstanceError):
            value.phase = "idle"
        assert not hasattr(value, "__dict__")


@pytest.mark.parametrize("changes", [
    {},
    {"phase": "focus", "completed": 3},
    {"running": False, "ends_at_unix_ms": None, "remaining_ms": 23_456},
    {"phase": "break", "completed": 1},
    {"phase": "break", "completed": 3, "running": False, "ends_at_unix_ms": None},
    {"phase": "long_break", "completed": 4},
    {"phase": "idle", "completed": 0, "running": False, "ends_at_unix_ms": None},
    {"remaining_ms": 0, "ends_at_unix_ms": 99_000},
    {"remaining_ms": 86_400_000, "length_ms": 86_400_000},
])
def test_checkpoint_survives_store_reconstruction_exactly(
    api: Any, tmp_path: Path, changes: dict[str, Any],
) -> None:
    path = tmp_path / "timer.sqlite3"
    expected = checkpoint(api, **changes)
    with DurableStore(path) as db:
        api.PomodoroStore(db).save(expected)
    with DurableStore(path) as db:
        restored = api.PomodoroStore(db).load()
        assert restored == expected
        assert type(restored.running) is bool


def test_latest_checkpoint_replaces_the_single_slot(api: Any, tmp_path: Path) -> None:
    with DurableStore(tmp_path / "timer.sqlite3") as db:
        repository = api.PomodoroStore(db)
        repository.save(checkpoint(api))
        last = checkpoint(api, phase="break", completed=2, running=False,
                          ends_at_unix_ms=None, remaining_ms=12_345, saved_at_unix_ms=120_000)
        repository.save(last)
        assert repository.load() == last
        assert db.execute("SELECT count(*) FROM pomodoro_checkpoint").fetchone() == (1,)


def test_save_uses_existing_owned_transaction_and_rolls_back_failure(
    api: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with DurableStore(tmp_path / "timer.sqlite3") as db:
        repository = api.PomodoroStore(db)
        prior = checkpoint(api)
        repository.save(prior)
        transaction = db.transaction

        @contextmanager
        def rejected_commit() -> Any:
            with transaction() as owned:
                yield owned
                raise sqlite3.OperationalError("synthetic commit failure")

        monkeypatch.setattr(db, "transaction", rejected_commit)
        with pytest.raises(sqlite3.OperationalError, match="synthetic commit failure"):
            repository.save(checkpoint(api, running=False, ends_at_unix_ms=None))
        assert repository.load() == prior


def test_repository_never_opens_an_additional_connection(
    api: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with DurableStore(tmp_path / "timer.sqlite3") as db:
        def forbidden_connect(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("Timer repositories must share the process-owned connection")

        monkeypatch.setattr(sqlite3, "connect", forbidden_connect)
        repository = api.PomodoroStore(db)
        value = checkpoint(api)
        repository.save(value)
        assert repository.load() == value


@pytest.mark.parametrize("changes", [
    {"phase": "unknown"}, {"phase": 1}, {"running": 1}, {"running": "true"},
    {"length_ms": 0}, {"length_ms": -1}, {"length_ms": True}, {"length_ms": 60_000.0},
    {"remaining_ms": -1}, {"remaining_ms": 60_001}, {"remaining_ms": False},
    {"remaining_ms": 1.5}, {"completed": True}, {"completed": -1},
    {"phase": "focus", "completed": 4}, {"phase": "break", "completed": 0},
    {"phase": "break", "completed": 4}, {"phase": "long_break", "completed": 3},
    {"phase": "idle", "completed": 1, "running": False, "ends_at_unix_ms": None},
    {"phase": "idle"}, {"ends_at_unix_ms": None},
    {"running": False}, {"ends_at_unix_ms": -1}, {"ends_at_unix_ms": True},
    {"ends_at_unix_ms": float("inf")}, {"ends_at_unix_ms": float("nan")},
    {"saved_at_unix_ms": -1}, {"saved_at_unix_ms": False},
    {"saved_at_unix_ms": 100_000.0}, {"saved_at_unix_ms": float("inf")},
    {"saved_at_unix_ms": float("nan")}, {"saved_at_unix_ms": 2**63},
    {"ends_at_unix_ms": 2**63}, {"length_ms": 2**63}, {"length_ms": 86_400_001},
])
def test_invalid_save_preserves_previous_checkpoint(
    api: Any, tmp_path: Path, changes: dict[str, Any],
) -> None:
    with DurableStore(tmp_path / "timer.sqlite3") as db:
        repository = api.PomodoroStore(db)
        prior = checkpoint(api)
        repository.save(prior)
        before = db.execute("SELECT total_changes()").fetchone()
        with pytest.raises(ValueError):
            repository.save(replace(prior, **changes))
        assert repository.load() == prior
        assert db.execute("SELECT total_changes()").fetchone() == before


@pytest.mark.parametrize(("column", "value"), [
    ("phase", "unknown"), ("running", 2), ("remaining_ms", -1),
    ("completed", 4), ("ends_at_unix_ms", None), ("saved_at_unix_ms", -1), ("id", 2),
    ("length_ms", 86_400_001),
])
def test_invalid_stored_state_raises_without_resetting_or_rewriting(
    api: Any, tmp_path: Path, column: str, value: Any,
) -> None:
    with DurableStore(tmp_path / "timer.sqlite3") as db:
        repository = api.PomodoroStore(db)
        repository.save(checkpoint(api))
        db.execute("PRAGMA ignore_check_constraints=ON")
        try:
            # Column names are the fixed test cases above, never external data.
            db.execute(f"UPDATE pomodoro_checkpoint SET {column}=?", (value,))
        finally:
            db.execute("PRAGMA ignore_check_constraints=OFF")
        before = db.execute("SELECT * FROM pomodoro_checkpoint").fetchone()
        assert issubclass(api.PomodoroStateError, StorageError)
        with pytest.raises(api.PomodoroStateError):
            repository.load()
        assert db.execute("SELECT * FROM pomodoro_checkpoint").fetchone() == before


def test_database_constraints_reject_invalid_direct_writes(api: Any, tmp_path: Path) -> None:
    with DurableStore(tmp_path / "timer.sqlite3") as db:
        value = checkpoint(api)
        repository = api.PomodoroStore(db)
        repository.save(value)
        for assignment in (
            "running=2", "completed=4", "remaining_ms=length_ms+1", "id=2",
            "length_ms=86400001",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(f"UPDATE pomodoro_checkpoint SET {assignment}")
        assert repository.load() == value
