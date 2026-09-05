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


def test_timer_migration_preserves_all_ten_released_statement_checksums() -> None:
    from joat.persistence import store as store_module

    # Frozen from the pre-Timer migration statements with the ledger's exact
    # checksum canonicalization. Appending migration 11 must not rewrite them.
    expected = [
        "b74c950879bc6f7f86db08371a5a089d664bdf9797569377ef988dcfe313fff5",
        "e940bcbcb0012a50466e410444c743859b1d7febad3fa25a62dc1f3b580e7566",
        "52bedaef3d63afb258e20176eff734e5daecf305f337c5e59b3e0ddade95be0f",
        "036de989f396a8a603251e486ba13d34fd80924018c92c917f6a4ffb56cf8b53",
        "e4652dfb37870cd53b0acb6cd320e2a01f9778ad37c5e773cdc407d8d14d070f",
        "a7c95f4737e2fd06112313890ed5e399cedf1c9291714dac6aa40e19952379c8",
        "767eb2127a60b51909551fab9cf16cff6873095c89fedb9f59ef931afa18e601",
        "46bf6fa9aaab2b9451e31c8faa48457577fe792abdae359f5ccb94a81a6f66e4",
        "fbfc7fd2706c04a834918cf49f1878703e52142b4e73054483780dfb235924d2",
        "db3740442e244b6c7e95dc09fbf4f03db4b565aff9cba23ddd3b1f9a63d5c764",
    ]
    assert [version for version, _, _ in store_module._MIGRATIONS[:10]] == list(range(1, 11))
    assert [store_module._migration_checksum(sql).hex()
            for _, _, sql in store_module._MIGRATIONS[:10]] == expected


def test_timer_migration_upgrades_v10_without_changing_existing_data_or_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from joat.persistence import store as store_module
    from joat.persistence.notes import NoteSource, NoteStore

    path = tmp_path / "timer-upgrade.sqlite3"
    current = store_module._MIGRATIONS
    with monkeypatch.context() as baseline:
        baseline.setattr(store_module, "_MIGRATIONS", current[:10])
        with DurableStore(path) as db:
            assert db.schema_version == 10
            note = NoteStore(db).create(note_id="retained", idempotency_key="retained-key",
                text="  Retained note\r\nwith exact spacing.  ", source=NoteSource("test"), now_us=10)
            db.execute(
                "INSERT INTO actions(action_id,kind,schema_version,canonicalizer_version,state,"
                "payload_digest,target_digest,idempotency_key,created_at_us,updated_at_us) "
                "VALUES ('retained-action','test',1,1,'drafted',?,?,'retained-action-key',10,10)",
                (b"p" * 32, b"t" * 32),
            )
            ledger = db.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
            action = db.execute("SELECT * FROM actions WHERE action_id='retained-action'").fetchone()
            tables = db.execute("SELECT name,sql FROM sqlite_schema WHERE type='table' ORDER BY name").fetchall()
    with DurableStore(path) as db:
        assert db.schema_version == 11, "Timer checkpoints require an append-only version-11 migration"
        assert db.execute("SELECT * FROM schema_migrations WHERE version<=10 ORDER BY version").fetchall() == ledger
        assert NoteStore(db).get("retained") == note
        assert db.execute("SELECT * FROM actions WHERE action_id='retained-action'").fetchone() == action
        assert db.execute("SELECT name,sql FROM sqlite_schema WHERE type='table' AND name!='pomodoro_checkpoint' ORDER BY name").fetchall() == tables
        assert db.execute("SELECT count(*) FROM pomodoro_checkpoint").fetchone() == (0,)
        assert db.check_integrity() == ("ok", ())
