"""Durability boundary for every consequential JOAT capability."""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from joat.persistence.store import (
    DurableStore,
    StorageOwnedError,
    StorageSchemaError,
)


def test_store_is_private_and_critical_pragmas_are_verified(tmp_path: Path) -> None:
    root = tmp_path / "JOAT"
    with DurableStore(root / "joat.sqlite3") as store:
        settings = store.durability_settings()

        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE((root / "joat.sqlite3").stat().st_mode) == 0o600
        assert stat.S_IMODE((root / "joat.sqlite3.lock").stat().st_mode) == 0o600
        assert settings == {
            "journal_mode": "delete",
            "synchronous": 3,
            "fullfsync": 1,
            "foreign_keys": 1,
            "trusted_schema": 0,
            "secure_delete": 1,
            "cell_size_check": 1,
            "temp_store": 2,
            "auto_vacuum": 2,
            "busy_timeout": 3000,
            "defensive": 1,
        }


def test_second_writer_is_refused_before_it_can_open_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "JOAT" / "joat.sqlite3"
    first = DurableStore(path)
    try:
        with pytest.raises(StorageOwnedError):
            DurableStore(path)
    finally:
        first.close()


def test_unknown_newer_schema_fails_closed_without_overwrite(tmp_path: Path) -> None:
    root = tmp_path / "JOAT"
    root.mkdir(mode=0o700)
    path = root / "joat.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE schema_migrations "
        "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum BLOB NOT NULL, "
        "applied_at_us INTEGER NOT NULL)"
    )
    conn.execute(
        "INSERT INTO schema_migrations VALUES (999, 'future', x'00', 1)"
    )
    conn.commit()
    conn.close()
    os.chmod(path, 0o600)

    with pytest.raises(StorageSchemaError, match="newer schema"):
        DurableStore(path)

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT name FROM schema_migrations").fetchone() == ("future",)
    finally:
        conn.close()


def test_migration_checksum_drift_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "JOAT" / "joat.sqlite3"
    with DurableStore(path) as store:
        version = store.schema_version

    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE schema_migrations SET checksum = ? WHERE version = ?",
        (b"x" * 32, version),
    )
    conn.commit()
    conn.close()

    with pytest.raises(StorageSchemaError, match="checksum"):
        DurableStore(path)


def test_integrity_checks_are_not_vacuous(tmp_path: Path) -> None:
    with DurableStore(tmp_path / "JOAT" / "joat.sqlite3") as store:
        assert store.check_integrity() == ("ok", ())


def test_clean_but_incomplete_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "JOAT" / "joat.sqlite3"
    with DurableStore(path):
        pass
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE external_identifiers")
    conn.close()

    with pytest.raises(StorageSchemaError, match="manifest"):
        DurableStore(path)


def test_refused_future_schema_keeps_its_journal_mode(tmp_path: Path) -> None:
    root = tmp_path / "JOAT"
    root.mkdir(mode=0o700)
    path = root / "joat.sqlite3"
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    conn.execute(
        "CREATE TABLE schema_migrations "
        "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum BLOB NOT NULL, "
        "applied_at_us INTEGER NOT NULL)"
    )
    conn.execute("INSERT INTO schema_migrations VALUES (999, 'future', x'00', 1)")
    conn.commit()
    conn.close()
    os.chmod(path, 0o600)

    with pytest.raises(StorageSchemaError, match="newer schema"):
        DurableStore(path)

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    finally:
        conn.close()


def test_unrelated_database_is_never_adopted(tmp_path: Path) -> None:
    root = tmp_path / "JOAT"
    root.mkdir(mode=0o700)
    path = root / "joat.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE unrelated(secret TEXT)")
    conn.execute("INSERT INTO unrelated VALUES ('keep me')")
    conn.commit()
    conn.close()

    with pytest.raises(StorageSchemaError, match="non-JOAT"):
        DurableStore(path)

    conn = sqlite3.connect(path)
    try:
        assert conn.execute("SELECT secret FROM unrelated").fetchone() == ("keep me",)
        assert conn.execute(
            "SELECT count(*) FROM sqlite_schema WHERE name='actions'"
        ).fetchone() == (0,)
    finally:
        conn.close()


def test_schema_constraints_reject_cross_action_receipt_and_partial_origin(
    tmp_path: Path,
) -> None:
    path = tmp_path / "JOAT" / "joat.sqlite3"
    with DurableStore(path) as store:
        now = 1
        blob = b"x" * 32
        with pytest.raises(sqlite3.IntegrityError):
            store.execute(
                "INSERT INTO actions("
                "action_id,kind,schema_version,canonicalizer_version,state,"
                "payload_digest,target_digest,idempotency_key,origin_kind,origin_id,"
                "created_at_us,updated_at_us"
                ") VALUES ('partial','test',1,1,'drafted',?,?,"
                "'partial-key','voice','utterance-1',?,?)",
                (blob, blob, now, now),
            )

        def insert_action(action_id: str) -> None:
            store.execute(
                "INSERT INTO actions("
                "action_id,kind,schema_version,canonicalizer_version,state,"
                "payload_digest,target_digest,idempotency_key,created_at_us,updated_at_us"
                ") VALUES (?,?,1,1,'approved',?,?,?,?,?)",
                (action_id, "test", blob, blob, f"key-{action_id}", now, now),
            )

        insert_action("a")
        insert_action("b")
        store.execute(
            "INSERT INTO action_attempts("
            "attempt_id,action_id,attempt_no,adapter,adapter_version,request_id,state,"
            "started_at_us,finished_at_us"
            ") VALUES ('attempt-b','b',1,'fake','1','request-b','succeeded',1,2)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.execute(
                "INSERT INTO action_receipts("
                "receipt_id,action_id,attempt_id,outcome,verification_level,"
                "evidence_digest,observed_at_us"
                ") VALUES ('receipt-wrong','a','attempt-b','ok','executor_accepted',?,3)",
                (blob,),
            )


def test_a_later_migration_can_retire_earlier_schema(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The manifest is the ORDERED result of the migrations, not the union of
    every CREATE. Without this a mistake could never be retired: migration 8
    created calendar_event_alerts for a poller that duplicated the platform,
    and migration 10 drops it."""
    from joat.persistence.store import DurableStore

    with DurableStore(tmp_path / "joat.sqlite3") as store:
        # The dropped table is gone from the physical schema, and the
        # manifest verifier (which runs on open) agrees rather than raising.
        names = {
            row[0] for row in store.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            ).fetchall()
        }
        assert "calendar_event_alerts" not in names
        # A table the migrations still create is untouched.
        assert "notification_observations" in names
