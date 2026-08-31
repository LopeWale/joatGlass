"""Single-writer, fail-closed SQLite foundation for consequential actions."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType


class StorageError(RuntimeError):
    """Base class for failures that must disable durable capabilities."""


class StorageOwnedError(StorageError):
    """Another engine already owns the only permitted writer lease."""


class StorageSchemaError(StorageError):
    """The on-disk schema cannot be safely interpreted by this build."""


class StorageDurabilityError(StorageError):
    """SQLite refused a required durability or privacy setting."""


# Migration inputs are immutable once released. Adding a state requires a new
# table-rebuild migration; changing this tuple would invalidate every v1 file.
_ACTION_STATES_V1 = (
    "drafted",
    "ready",
    "previewing",
    "awaiting_confirmation",
    "rejected",
    "expired",
    "approved",
    "dispatching",
    "verifying",
    "succeeded",
    "failed",
    "uncertain",
    "reconciling",
    "reconciled_succeeded",
    "reconciled_absent",
    "manual_review",
    "cancelled",
    "superseded",
    "invalidated",
)
_STATE_SQL_V1 = ",".join(f"'{state}'" for state in _ACTION_STATES_V1)

_SCHEMA_MIGRATIONS_SQL = (
    "CREATE TABLE schema_migrations ("
    "version INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
    "checksum BLOB NOT NULL CHECK(typeof(checksum)='blob' AND length(checksum)=32), "
    "applied_at_us INTEGER NOT NULL) STRICT"
)
_APPLICATION_META_SQL = (
    "CREATE TABLE application_meta ("
    "application_id TEXT PRIMARY KEY CHECK(application_id='com.joat.durable-store'), "
    "created_at_us INTEGER NOT NULL) STRICT"
)

_MIGRATION_1 = (
    f"""
    CREATE TABLE actions (
        action_id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        canonicalizer_version INTEGER NOT NULL,
        state TEXT NOT NULL CHECK (state IN ({_STATE_SQL_V1})),
        payload_digest BLOB NOT NULL CHECK (
            typeof(payload_digest) = 'blob' AND length(payload_digest) = 32
        ),
        target_digest BLOB NOT NULL CHECK (
            typeof(target_digest) = 'blob' AND length(target_digest) = 32
        ),
        preview_digest BLOB CHECK (
            preview_digest IS NULL OR (
                typeof(preview_digest) = 'blob' AND length(preview_digest) = 32
            )
        ),
        idempotency_key TEXT NOT NULL UNIQUE,
        origin_kind TEXT,
        origin_id TEXT,
        origin_ordinal INTEGER,
        created_at_us INTEGER NOT NULL,
        updated_at_us INTEGER NOT NULL,
        expires_at_us INTEGER,
        validation_code TEXT,
        last_error_code TEXT,
        state_version INTEGER NOT NULL DEFAULT 0 CHECK (state_version >= 0),
        CHECK (
            (origin_kind IS NULL AND origin_id IS NULL AND origin_ordinal IS NULL)
            OR
            (origin_kind IS NOT NULL AND origin_id IS NOT NULL
             AND origin_ordinal IS NOT NULL AND origin_ordinal >= 0)
        )
    ) STRICT
    """,
    """
    CREATE UNIQUE INDEX one_action_per_origin
    ON actions(origin_kind, origin_id, origin_ordinal)
    WHERE origin_kind IS NOT NULL
    """,
    """
    CREATE TABLE action_payloads (
        action_id TEXT PRIMARY KEY REFERENCES actions(action_id) ON DELETE CASCADE,
        canonical_payload BLOB NOT NULL,
        retention_until_us INTEGER
    ) STRICT
    """,
    """
    CREATE TABLE action_events (
        event_pk INTEGER PRIMARY KEY,
        action_id TEXT NOT NULL REFERENCES actions(action_id) ON DELETE CASCADE,
        seq INTEGER NOT NULL,
        from_state TEXT,
        to_state TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        at_us INTEGER NOT NULL,
        detail_digest BLOB CHECK (
            detail_digest IS NULL OR (
                typeof(detail_digest) = 'blob' AND length(detail_digest) = 32
            )
        ),
        CHECK (from_state IS NULL OR from_state IN (""" + _STATE_SQL_V1 + """)),
        CHECK (to_state IN (""" + _STATE_SQL_V1 + """)),
        UNIQUE (action_id, seq)
    ) STRICT
    """,
    """
    CREATE TABLE confirmation_challenges (
        challenge_id TEXT PRIMARY KEY,
        action_id TEXT NOT NULL REFERENCES actions(action_id) ON DELETE CASCADE,
        token_hash BLOB NOT NULL UNIQUE CHECK (
            typeof(token_hash) = 'blob' AND length(token_hash) = 32
        ),
        session_id TEXT NOT NULL,
        pairing_identity_digest BLOB NOT NULL CHECK (
            typeof(pairing_identity_digest) = 'blob'
            AND length(pairing_identity_digest) = 32
        ),
        authorization_digest BLOB NOT NULL CHECK (
            typeof(authorization_digest) = 'blob'
            AND length(authorization_digest) = 32
        ),
        issued_at_us INTEGER NOT NULL,
        expires_at_us INTEGER NOT NULL,
        preview_completed_at_us INTEGER NOT NULL,
        consumed_at_us INTEGER,
        decision TEXT CHECK (decision IS NULL OR decision IN ('approved','rejected')),
        CHECK (expires_at_us > issued_at_us),
        CHECK (preview_completed_at_us >= issued_at_us
               AND preview_completed_at_us <= expires_at_us),
        CHECK ((consumed_at_us IS NULL AND decision IS NULL)
               OR (consumed_at_us IS NOT NULL AND decision IS NOT NULL))
    ) STRICT
    """,
    """
    CREATE UNIQUE INDEX one_pending_confirmation_per_action
    ON confirmation_challenges(action_id)
    WHERE consumed_at_us IS NULL
    """,
    """
    CREATE TABLE action_attempts (
        attempt_id TEXT PRIMARY KEY,
        action_id TEXT NOT NULL REFERENCES actions(action_id) ON DELETE CASCADE,
        attempt_no INTEGER NOT NULL,
        adapter TEXT NOT NULL,
        adapter_version TEXT NOT NULL,
        request_id TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL CHECK (state IN ('dispatching','verifying','succeeded','failed','uncertain')),
        started_at_us INTEGER NOT NULL,
        dispatch_started_at_us INTEGER,
        finished_at_us INTEGER,
        result_digest BLOB CHECK (
            result_digest IS NULL OR (
                typeof(result_digest) = 'blob' AND length(result_digest) = 32
            )
        ),
        error_code TEXT,
        UNIQUE (action_id, attempt_no),
        UNIQUE (action_id, attempt_id),
        CHECK (dispatch_started_at_us IS NULL OR dispatch_started_at_us >= started_at_us),
        CHECK ((state IN ('dispatching','verifying') AND finished_at_us IS NULL)
               OR (state IN ('succeeded','failed','uncertain')
                   AND finished_at_us IS NOT NULL)),
        CHECK (finished_at_us IS NULL OR finished_at_us >= started_at_us)
    ) STRICT
    """,
    """
    CREATE UNIQUE INDEX one_unfinished_attempt_per_action
    ON action_attempts(action_id)
    WHERE state IN ('dispatching','verifying','uncertain')
    """,
    """
    CREATE TABLE action_receipts (
        receipt_id TEXT PRIMARY KEY,
        action_id TEXT NOT NULL REFERENCES actions(action_id) ON DELETE CASCADE,
        attempt_id TEXT NOT NULL REFERENCES action_attempts(attempt_id) ON DELETE CASCADE,
        outcome TEXT NOT NULL,
        verification_level TEXT NOT NULL CHECK (verification_level IN (
            'executor_accepted','read_back_matched','delivery_observed',
            'delivery_unverified','reconciled'
        )),
        evidence_digest BLOB NOT NULL CHECK (
            typeof(evidence_digest) = 'blob' AND length(evidence_digest) = 32
        ),
        observed_at_us INTEGER NOT NULL,
        external_item_id TEXT REFERENCES external_items(external_item_id),
        UNIQUE (attempt_id, verification_level),
        FOREIGN KEY (action_id, attempt_id)
            REFERENCES action_attempts(action_id, attempt_id)
    ) STRICT
    """,
    """
    CREATE TABLE external_items (
        external_item_id TEXT PRIMARY KEY,
        system TEXT NOT NULL,
        kind TEXT NOT NULL,
        scope_digest BLOB NOT NULL CHECK (
            typeof(scope_digest) = 'blob' AND length(scope_digest) = 32
        ),
        fingerprint BLOB NOT NULL CHECK (
            typeof(fingerprint) = 'blob' AND length(fingerprint) = 32
        ),
        first_seen_us INTEGER NOT NULL,
        last_seen_us INTEGER NOT NULL,
        tombstoned_at_us INTEGER
    ) STRICT
    """,
    """
    CREATE TABLE external_identifiers (
        external_item_id TEXT NOT NULL REFERENCES external_items(external_item_id) ON DELETE CASCADE,
        identifier_kind TEXT NOT NULL,
        identifier_value TEXT NOT NULL,
        valid_from_us INTEGER NOT NULL,
        valid_to_us INTEGER,
        PRIMARY KEY (external_item_id, identifier_kind, identifier_value)
    ) STRICT
    """,
    """
    CREATE UNIQUE INDEX one_external_identifier_owner
    ON external_identifiers(identifier_kind, identifier_value)
    WHERE valid_to_us IS NULL
    """,
)

_MIGRATION_2 = (
    """
    CREATE TABLE quick_notes (
        note_id TEXT PRIMARY KEY CHECK (
            typeof(note_id) = 'text' AND length(note_id) > 0
        ),
        idempotency_key TEXT NOT NULL UNIQUE CHECK (
            typeof(idempotency_key) = 'text' AND length(idempotency_key) > 0
        ),
        visible_text TEXT NOT NULL CHECK (
            typeof(visible_text) = 'text' AND length(visible_text) > 0
        ),
        source_kind TEXT NOT NULL CHECK (
            typeof(source_kind) = 'text' AND length(source_kind) > 0
        ),
        source_identifier TEXT CHECK (
            source_identifier IS NULL OR (
                typeof(source_identifier) = 'text' AND length(source_identifier) > 0
            )
        ),
        created_at_us INTEGER NOT NULL CHECK (created_at_us >= 0),
        updated_at_us INTEGER NOT NULL CHECK (updated_at_us >= created_at_us),
        deleted_at_us INTEGER CHECK (
            deleted_at_us IS NULL OR deleted_at_us = updated_at_us
        )
    ) STRICT
    """,
    """
    CREATE INDEX quick_notes_active_order
    ON quick_notes(updated_at_us DESC, note_id)
    WHERE deleted_at_us IS NULL
    """,
)

_MIGRATION_3 = (
    """
    CREATE TABLE task_occurrences (
        occurrence_id TEXT PRIMARY KEY CHECK (
            typeof(occurrence_id) = 'text' AND length(occurrence_id) > 0
        ),
        action_id TEXT NOT NULL UNIQUE REFERENCES actions(action_id) ON DELETE RESTRICT,
        external_system TEXT NOT NULL CHECK (external_system = 'eventkit.reminders'),
        external_item_id TEXT NOT NULL UNIQUE CHECK (
            typeof(external_item_id) = 'text' AND length(external_item_id) > 0
        ),
        calendar_identifier TEXT NOT NULL CHECK (
            typeof(calendar_identifier) = 'text' AND length(calendar_identifier) > 0
        ),
        title TEXT NOT NULL CHECK (
            typeof(title) = 'text' AND length(title) > 0
        ),
        due_unix_ms INTEGER NOT NULL CHECK (due_unix_ms >= 0),
        time_zone_id TEXT NOT NULL CHECK (
            typeof(time_zone_id) = 'text' AND length(time_zone_id) > 0
        ),
        state TEXT NOT NULL CHECK (
            state IN ('pending','alerted','completed','dismissed','cancelled')
        ),
        created_at_us INTEGER NOT NULL CHECK (created_at_us >= 0),
        updated_at_us INTEGER NOT NULL CHECK (updated_at_us >= created_at_us),
        alerted_at_us INTEGER,
        completed_at_us INTEGER,
        CHECK ((state = 'alerted') = (alerted_at_us IS NOT NULL)),
        CHECK ((state = 'completed') = (completed_at_us IS NOT NULL)),
        CHECK (alerted_at_us IS NULL OR alerted_at_us >= created_at_us),
        CHECK (completed_at_us IS NULL OR completed_at_us >= created_at_us)
    ) STRICT
    """,
    """
    CREATE INDEX pending_task_occurrences_due
    ON task_occurrences(due_unix_ms, occurrence_id)
    WHERE state = 'pending'
    """,
)

_MIGRATION_4 = (
    """
    CREATE TABLE notification_sources (
        source_id TEXT PRIMARY KEY,
        coverage_scope TEXT NOT NULL,
        authority_level TEXT NOT NULL CHECK (
            authority_level IN ('authoritative','best_effort','metadata_only')
        ),
        cursor_json BLOB NOT NULL,
        health TEXT NOT NULL CHECK (health IN (
            'healthy_complete_for_scope','healthy_best_effort','permission_denied',
            'unsupported','stale','cursor_reset_required','parse_degraded','failed'
        )),
        initialized_at_us INTEGER NOT NULL,
        updated_at_us INTEGER NOT NULL CHECK (updated_at_us >= initialized_at_us)
    ) STRICT
    """,
    """
    CREATE TABLE notification_observations (
        observation_id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL REFERENCES notification_sources(source_id) ON DELETE RESTRICT,
        native_id TEXT NOT NULL,
        event_class TEXT NOT NULL,
        occurred_at_us INTEGER,
        observed_at_us INTEGER NOT NULL,
        payload BLOB NOT NULL,
        payload_digest BLOB NOT NULL CHECK (
            typeof(payload_digest) = 'blob' AND length(payload_digest) = 32
        ),
        completeness TEXT NOT NULL CHECK (
            completeness IN ('complete_for_scope','best_effort','metadata_only')
        ),
        UNIQUE (source_id, native_id)
    ) STRICT
    """,
    """
    CREATE TABLE notification_outbox (
        observation_id TEXT PRIMARY KEY REFERENCES notification_observations(observation_id) ON DELETE CASCADE,
        state TEXT NOT NULL CHECK (state IN ('pending','enqueued','suppressed')),
        reason_code TEXT,
        updated_at_us INTEGER NOT NULL,
        CHECK ((state = 'pending' AND reason_code IS NULL)
               OR (state != 'pending' AND reason_code IS NOT NULL))
    ) STRICT
    """,
    """
    CREATE INDEX pending_notification_outbox
    ON notification_outbox(updated_at_us, observation_id)
    WHERE state = 'pending'
    """,
)

_MIGRATION_5 = (
    """
    CREATE TABLE task_alert_receipts (
        occurrence_id TEXT PRIMARY KEY
            REFERENCES task_occurrences(occurrence_id) ON DELETE CASCADE,
        alerted_at_us INTEGER NOT NULL CHECK (alerted_at_us >= 0)
    ) STRICT
    """,
)

_MIGRATION_6 = (
    """
    CREATE TABLE voice_notes (
        capture_id TEXT PRIMARY KEY CHECK (
            typeof(capture_id) = 'text' AND length(capture_id) > 0
        ),
        idempotency_key TEXT NOT NULL UNIQUE CHECK (
            typeof(idempotency_key) = 'text' AND length(idempotency_key) > 0
        ),
        title TEXT CHECK (
            title IS NULL OR (typeof(title) = 'text' AND length(title) > 0)
        ),
        source_kind TEXT NOT NULL CHECK (
            typeof(source_kind) = 'text' AND length(source_kind) > 0
        ),
        state TEXT NOT NULL CHECK (state IN (
            'preparing','recording','paused','finalizing','saved',
            'saved_degraded','interrupted','discarded','failed'
        )),
        created_at_us INTEGER NOT NULL CHECK (created_at_us >= 0),
        updated_at_us INTEGER NOT NULL CHECK (updated_at_us >= created_at_us),
        started_at_us INTEGER CHECK (
            started_at_us IS NULL OR started_at_us >= created_at_us
        ),
        ended_at_us INTEGER CHECK (
            ended_at_us IS NULL OR ended_at_us >= created_at_us
        ),
        received_frames INTEGER NOT NULL DEFAULT 0 CHECK (received_frames >= 0),
        journaled_frames INTEGER NOT NULL DEFAULT 0 CHECK (
            journaled_frames >= 0 AND journaled_frames <= received_frames
        ),
        transcribed_segments INTEGER NOT NULL DEFAULT 0 CHECK (
            transcribed_segments >= 0
        ),
        gap_count INTEGER NOT NULL DEFAULT 0 CHECK (gap_count >= 0),
        raw_bytes INTEGER NOT NULL DEFAULT 0 CHECK (raw_bytes >= 0),
        raw_retained INTEGER NOT NULL DEFAULT 1 CHECK (raw_retained IN (0,1)),
        completion_code TEXT,
        CHECK ((state IN ('saved','saved_degraded','interrupted','discarded','failed'))
               = (ended_at_us IS NOT NULL)),
        CHECK ((state = 'discarded') = (title IS NULL)),
        CHECK (state != 'saved' OR raw_retained = 0)
    ) STRICT
    """,
    """
    CREATE TABLE voice_note_frames (
        capture_id TEXT NOT NULL REFERENCES voice_notes(capture_id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        connection_generation TEXT NOT NULL CHECK (
            typeof(connection_generation) = 'text'
            AND length(connection_generation) > 0
        ),
        seq INTEGER NOT NULL CHECK (seq >= 0),
        source TEXT NOT NULL CHECK (
            typeof(source) = 'text' AND length(source) > 0
        ),
        speaker TEXT CHECK (
            speaker IS NULL OR (typeof(speaker) = 'text' AND length(speaker) > 0)
        ),
        received_at_us INTEGER NOT NULL CHECK (received_at_us >= 0),
        pcm_digest BLOB NOT NULL CHECK (
            typeof(pcm_digest) = 'blob' AND length(pcm_digest) = 32
        ),
        pcm BLOB NOT NULL CHECK (typeof(pcm) = 'blob' AND length(pcm) > 0),
        PRIMARY KEY (capture_id, ordinal),
        UNIQUE (capture_id, connection_generation, seq)
    ) STRICT
    """,
    """
    CREATE TABLE voice_note_segments (
        capture_id TEXT NOT NULL REFERENCES voice_notes(capture_id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        kind TEXT NOT NULL CHECK (kind IN ('transcript','gap','system')),
        text TEXT NOT NULL CHECK (
            typeof(text) = 'text' AND length(text) > 0
        ),
        first_frame_ordinal INTEGER CHECK (
            first_frame_ordinal IS NULL OR first_frame_ordinal >= 0
        ),
        last_frame_ordinal INTEGER CHECK (
            last_frame_ordinal IS NULL OR last_frame_ordinal >= first_frame_ordinal
        ),
        created_at_us INTEGER NOT NULL CHECK (created_at_us >= 0),
        PRIMARY KEY (capture_id, ordinal),
        CHECK ((kind = 'transcript') = (first_frame_ordinal IS NOT NULL)),
        CHECK ((kind = 'transcript') = (last_frame_ordinal IS NOT NULL))
    ) STRICT
    """,
    """
    CREATE INDEX voice_notes_active_state
    ON voice_notes(state, updated_at_us, capture_id)
    WHERE state IN ('preparing','recording','paused','finalizing')
    """,
)

_MIGRATION_7 = (
    # Generated distillations of a retained Voice Note. Kept OUT of
    # voice_note_segments deliberately: segments are capture-time truth and
    # freeze at completion, while insights are derived, engine-attributed,
    # and replaceable (re-summarize with a different engine later).
    """
    CREATE TABLE voice_note_insights (
        capture_id TEXT NOT NULL REFERENCES voice_notes(capture_id) ON DELETE CASCADE,
        kind TEXT NOT NULL CHECK (kind IN ('summary','key_point','todo')),
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        text TEXT NOT NULL CHECK (
            typeof(text) = 'text' AND length(text) > 0
        ),
        engine TEXT NOT NULL CHECK (
            typeof(engine) = 'text' AND length(engine) > 0
        ),
        generated_at_us INTEGER NOT NULL CHECK (generated_at_us >= 0),
        PRIMARY KEY (capture_id, kind, ordinal)
    ) STRICT
    """,
)

_MIGRATIONS: tuple[tuple[int, str, tuple[str, ...]], ...] = (
    (1, "durable_actions", _MIGRATION_1),
    (2, "text_quick_notes", _MIGRATION_2),
    (3, "eventkit_task_occurrences", _MIGRATION_3),
    (4, "durable_notification_observations", _MIGRATION_4),
    (5, "retain_task_alert_history", _MIGRATION_5),
    (6, "durable_local_voice_notes", _MIGRATION_6),
    (7, "voice_note_insights", _MIGRATION_7),
)


def _migration_checksum(statements: tuple[str, ...]) -> bytes:
    canonical = "\n-- statement --\n".join(s.strip() for s in statements)
    return hashlib.sha256(canonical.encode("utf-8")).digest()


class DurableStore:
    """Own one SQLite writer connection and a process-lifetime file lease."""

    def __init__(self, path: Path, *, busy_timeout_ms: int = 3000) -> None:
        if busy_timeout_ms < 1 or busy_timeout_ms > 60_000:
            raise ValueError("busy_timeout_ms must be between 1 and 60000")
        self._path = path
        self._busy_timeout_ms = busy_timeout_ms
        self._lock_fd: int | None = None
        self._conn: sqlite3.Connection | None = None
        self._new_database = not path.exists() or path.stat().st_size == 0
        try:
            self._prepare_private_files()
            self._acquire_writer_lease()
            self._open_sqlite()
            self._preflight_identity()
            self._configure()
            self._migrate()
            self._verify_schema_manifest()
            self._verify_settings()
            quick, foreign_keys = self.check_integrity()
            if quick != "ok" or foreign_keys:
                raise StorageSchemaError("SQLite integrity check failed")
        except BaseException:
            self.close()
            raise

    def _prepare_private_files(self) -> None:
        if self._path.is_symlink():
            raise StorageSchemaError("database path must not be a symbolic link")
        lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        if lock_path.is_symlink():
            raise StorageSchemaError("writer lock must not be a symbolic link")
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._path.parent, 0o700)
        self._lock_fd = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        os.fchmod(self._lock_fd, 0o600)

    def _acquire_writer_lease(self) -> None:
        assert self._lock_fd is not None
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StorageOwnedError("storage already owned by another engine") from exc

    def _open_sqlite(self) -> None:
        fd = os.open(
            self._path,
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        os.fchmod(fd, 0o600)
        os.close(fd)
        self._conn = sqlite3.connect(
            self._path,
            timeout=self._busy_timeout_ms / 1000,
            autocommit=True,
        )

    def _preflight_identity(self) -> None:
        """Refuse unsupported or unrelated files before persistent PRAGMAs."""

        db = self._db
        try:
            objects = {
                str(row[0])
                for row in db.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }
        except sqlite3.DatabaseError as exc:
            raise StorageSchemaError("database header or schema is malformed") from exc
        if not objects:
            if self._new_database:
                return
            raise StorageSchemaError("existing empty database has no JOAT identity marker")
        if "schema_migrations" in objects:
            try:
                newest = db.execute(
                    "SELECT max(version) FROM schema_migrations"
                ).fetchone()
            except sqlite3.DatabaseError as exc:
                raise StorageSchemaError("migration ledger is malformed") from exc
            supported = _MIGRATIONS[-1][0]
            if newest and newest[0] is not None and int(newest[0]) > supported:
                raise StorageSchemaError(
                    f"database has newer schema {newest[0]}; this build supports {supported}"
                )
        if not {"application_meta", "schema_migrations"}.issubset(objects):
            raise StorageSchemaError("refusing non-JOAT or unmarked SQLite database")
        try:
            marker = db.execute(
                "SELECT application_id FROM application_meta"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise StorageSchemaError("application identity marker is malformed") from exc
        if marker != [("com.joat.durable-store",)]:
            raise StorageSchemaError("database application identity does not match JOAT")
        self._validate_migration_ledger()

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StorageError("storage is closed")
        return self._conn

    @property
    def path(self) -> Path:
        """The resolved database path; useful for permissions and backup tooling."""

        return self._path

    def execute(
        self,
        sql: str,
        parameters: tuple[object, ...] = (),
    ) -> sqlite3.Cursor:
        """Execute on the leased connection.

        Persistence repositories share this one process-owned connection. They
        must use :meth:`transaction` for multi-statement state changes.
        """

        return self._db.execute(sql, parameters)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run one immediate, rollback-on-error transaction."""

        db = self._db
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

    def _configure(self) -> None:
        db = self._db
        db.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA synchronous=EXTRA")
        db.execute("PRAGMA fullfsync=ON")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA secure_delete=ON")
        db.execute("PRAGMA cell_size_check=ON")
        db.execute("PRAGMA temp_store=MEMORY")
        db.execute("PRAGMA auto_vacuum=INCREMENTAL")
        if not hasattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE"):
            raise StorageDurabilityError("SQLite defensive mode is unavailable")
        db.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)

    def _validate_migration_ledger(
        self,
    ) -> list[tuple[object, object, object]]:
        db = self._db
        try:
            rows = db.execute(
                "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise StorageSchemaError("migration ledger is malformed") from exc
        current = _MIGRATIONS[-1][0]
        if rows and int(rows[-1][0]) > current:
            raise StorageSchemaError(
                f"database has newer schema {rows[-1][0]}; this build supports {current}"
            )
        known = {version: (name, _migration_checksum(sql)) for version, name, sql in _MIGRATIONS}
        for version, name, checksum in rows:
            expected = known.get(int(version))
            if expected is None:
                raise StorageSchemaError(f"unknown migration {version}")
            if str(name) != expected[0] or bytes(checksum) != expected[1]:
                raise StorageSchemaError(f"migration {version} checksum mismatch")
        return rows

    def _migrate(self) -> None:
        db = self._db
        if self._new_database:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(_APPLICATION_META_SQL)
                db.execute(
                    "INSERT INTO application_meta(application_id,created_at_us) "
                    "VALUES ('com.joat.durable-store',?)",
                    (time.time_ns() // 1000,),
                )
                db.execute(_SCHEMA_MIGRATIONS_SQL)
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
        rows = self._validate_migration_ledger()
        applied = {int(str(row[0])) for row in rows}
        for version, name, statements in _MIGRATIONS:
            if version in applied:
                continue
            db.execute("BEGIN IMMEDIATE")
            try:
                for statement in statements:
                    db.execute(statement)
                db.execute(
                    "INSERT INTO schema_migrations(version,name,checksum,applied_at_us) "
                    "VALUES (?,?,?,?)",
                    (
                        version,
                        name,
                        _migration_checksum(statements),
                        time.time_ns() // 1000,
                    ),
                )
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise

    @staticmethod
    def _normalize_ddl(sql: str) -> str:
        return re.sub(r"\s+", " ", sql.strip()).lower()

    def _verify_schema_manifest(self) -> None:
        """Prove the physical schema still matches the signed migration set."""

        expected_sql = (_APPLICATION_META_SQL, _SCHEMA_MIGRATIONS_SQL) + tuple(
            statement
            for _version, _name, statements in _MIGRATIONS
            for statement in statements
        )
        expected: dict[tuple[str, str], str] = {}
        pattern = re.compile(
            r"^\s*CREATE\s+(?:UNIQUE\s+)?(TABLE|INDEX)\s+([A-Za-z_][A-Za-z0-9_]*)",
            re.IGNORECASE,
        )
        for sql in expected_sql:
            match = pattern.match(sql)
            if match is None:
                raise StorageSchemaError("internal migration has unrecognized DDL")
            expected[(match.group(1).lower(), match.group(2))] = self._normalize_ddl(sql)
        actual = {
            (str(kind), str(name)): self._normalize_ddl(str(sql))
            for kind, name, sql in self._db.execute(
                "SELECT type,name,sql FROM sqlite_schema "
                "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
            )
        }
        if actual != expected:
            missing = sorted(set(expected) - set(actual))
            extra = sorted(set(actual) - set(expected))
            changed = sorted(
                key for key in set(expected) & set(actual) if expected[key] != actual[key]
            )
            raise StorageSchemaError(
                f"physical schema manifest mismatch: missing={missing}, "
                f"extra={extra}, changed={changed}"
            )

    def durability_settings(self) -> dict[str, str | int]:
        db = self._db

        def scalar(pragma: str) -> str | int:
            row = db.execute(f"PRAGMA {pragma}").fetchone()
            if row is None:
                raise StorageDurabilityError(f"SQLite ignored PRAGMA {pragma}")
            return row[0]  # type: ignore[no-any-return]

        return {
            "journal_mode": str(scalar("journal_mode")).lower(),
            "synchronous": int(scalar("synchronous")),
            "fullfsync": int(scalar("fullfsync")),
            "foreign_keys": int(scalar("foreign_keys")),
            "trusted_schema": int(scalar("trusted_schema")),
            "secure_delete": int(scalar("secure_delete")),
            "cell_size_check": int(scalar("cell_size_check")),
            "temp_store": int(scalar("temp_store")),
            "auto_vacuum": int(scalar("auto_vacuum")),
            "busy_timeout": int(scalar("busy_timeout")),
            "defensive": int(db.getconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE)),
        }

    def _verify_settings(self) -> None:
        expected: dict[str, str | int] = {
            "journal_mode": "delete",
            "synchronous": 3,
            "fullfsync": 1,
            "foreign_keys": 1,
            "trusted_schema": 0,
            "secure_delete": 1,
            "cell_size_check": 1,
            "temp_store": 2,
            "auto_vacuum": 2,
            "busy_timeout": self._busy_timeout_ms,
            "defensive": 1,
        }
        actual = self.durability_settings()
        if actual != expected:
            raise StorageDurabilityError(
                f"SQLite durability settings mismatch: expected {expected}, got {actual}"
            )

    @property
    def schema_version(self) -> int:
        row = self._db.execute("SELECT max(version) FROM schema_migrations").fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def check_integrity(self) -> tuple[str, tuple[tuple[object, ...], ...]]:
        quick_rows = self._db.execute("PRAGMA quick_check").fetchall()
        quick = str(quick_rows[0][0]) if len(quick_rows) == 1 else repr(quick_rows)
        foreign_rows = tuple(self._db.execute("PRAGMA foreign_key_check").fetchall())
        return quick, foreign_rows

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_fd)
                self._lock_fd = None

    def __enter__(self) -> DurableStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
