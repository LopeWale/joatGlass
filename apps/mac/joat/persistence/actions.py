"""Durable, replay-resistant state machine for consequential actions."""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass

from .store import DurableStore


class ActionError(RuntimeError):
    """Base class for action-journal failures."""


class ActionConflictError(ActionError):
    """The requested mutation conflicts with durable state."""


@dataclass(frozen=True)
class ActionDraft:
    kind: str
    canonical_payload: bytes
    target_digest: bytes
    idempotency_key: str
    schema_version: int = 1
    canonicalizer_version: int = 1
    origin_kind: str | None = None
    origin_id: str | None = None
    origin_ordinal: int | None = None
    expires_at_us: int | None = None


@dataclass(frozen=True)
class ActionRecord:
    action_id: str
    kind: str
    state: str
    payload_digest: bytes
    target_digest: bytes
    preview_digest: bytes | None
    idempotency_key: str
    state_version: int


@dataclass(frozen=True)
class ActionActivity:
    """Payload-free action metadata safe for a glanceable activity view."""

    action_id: str
    kind: str
    state: str
    created_at_us: int
    updated_at_us: int


@dataclass(frozen=True)
class ConfirmationEvidence:
    """Evidence returned by the authenticated, exact-preview channel."""

    challenge_id: str
    challenge_token: str
    session_id: str
    pairing_identity_digest: bytes
    authorization_digest: bytes
    payload_digest: bytes
    preview_digest: bytes
    issued_at_us: int
    expires_at_us: int
    preview_completed_at_us: int
    decided_at_us: int
    accepted: bool


@dataclass(frozen=True)
class ExternalReference:
    """One read-back identity bound atomically to a successful receipt."""

    system: str
    kind: str
    identifier_kind: str
    identifier_value: str
    scope_digest: bytes


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    action_id: str
    request_id: str
    state: str


def _sha256(value: bytes) -> bytes:
    return hashlib.sha256(value).digest()


def _confirmation_token_hash(token: str) -> bytes:
    return _sha256(b"joat.confirmation-token.v1\0" + token.encode("utf-8"))


def _require_digest(name: str, value: bytes) -> None:
    if len(value) != 32:
        raise ValueError(f"{name} must be a SHA-256 digest")


class ActionJournal:
    """Append transitions and receipts on the process-owned SQLite writer."""

    def __init__(self, store: DurableStore) -> None:
        self._store = store

    def create_draft(self, draft: ActionDraft, *, now_us: int) -> ActionRecord:
        if not draft.kind or not draft.idempotency_key or not draft.canonical_payload:
            raise ValueError("kind, idempotency key, and canonical payload are required")
        _require_digest("target_digest", draft.target_digest)
        payload_digest = _sha256(draft.canonical_payload)

        with self._store.transaction() as db:
            existing = db.execute(
                "SELECT action_id FROM actions WHERE idempotency_key = ?",
                (draft.idempotency_key,),
            ).fetchone()
            if existing is not None:
                record = self._get_with(db, str(existing[0]))
                payload_row = db.execute(
                    "SELECT canonical_payload FROM action_payloads WHERE action_id = ?",
                    (record.action_id,),
                ).fetchone()
                if (
                    record.kind != draft.kind
                    or record.payload_digest != payload_digest
                    or record.target_digest != draft.target_digest
                    or payload_row is None
                    or bytes(payload_row[0]) != draft.canonical_payload
                ):
                    raise ActionConflictError(
                        "idempotency key already names a different action"
                    )
                return record

            action_id = str(uuid.uuid4())
            try:
                db.execute(
                    "INSERT INTO actions("
                    "action_id,kind,schema_version,canonicalizer_version,state,"
                    "payload_digest,target_digest,idempotency_key,origin_kind,origin_id,"
                    "origin_ordinal,created_at_us,updated_at_us,expires_at_us,state_version"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                    (
                        action_id,
                        draft.kind,
                        draft.schema_version,
                        draft.canonicalizer_version,
                        "drafted",
                        payload_digest,
                        draft.target_digest,
                        draft.idempotency_key,
                        draft.origin_kind,
                        draft.origin_id,
                        draft.origin_ordinal,
                        now_us,
                        now_us,
                        draft.expires_at_us,
                    ),
                )
                db.execute(
                    "INSERT INTO action_payloads(action_id,canonical_payload) VALUES (?,?)",
                    (action_id, draft.canonical_payload),
                )
                db.execute(
                    "INSERT INTO action_events("
                    "action_id,seq,from_state,to_state,reason_code,at_us"
                    ") VALUES (?,0,NULL,'drafted','created',?)",
                    (action_id, now_us),
                )
            except sqlite3.IntegrityError as exc:
                raise ActionConflictError("draft identity conflicts with durable state") from exc
            return self._get_with(db, action_id)

    def get(self, action_id: str) -> ActionRecord:
        return self._get_with(self._store._db, action_id)

    def recent(self, *, limit: int = 5) -> tuple[ActionActivity, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 20
        ):
            raise ValueError("limit must be between 1 and 20")
        rows = self._store.execute(
            "SELECT action_id,kind,state,created_at_us,updated_at_us "
            "FROM actions ORDER BY updated_at_us DESC,action_id ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return tuple(
            ActionActivity(
                action_id=str(row[0]),
                kind=str(row[1]),
                state=str(row[2]),
                created_at_us=int(row[3]),
                updated_at_us=int(row[4]),
            )
            for row in rows
        )

    @staticmethod
    def _get_with(db: sqlite3.Connection, action_id: str) -> ActionRecord:
        row = db.execute(
            "SELECT action_id,kind,state,payload_digest,target_digest,preview_digest,"
            "idempotency_key,state_version FROM actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if row is None:
            raise ActionConflictError("unknown action")
        return ActionRecord(
            action_id=str(row[0]),
            kind=str(row[1]),
            state=str(row[2]),
            payload_digest=bytes(row[3]),
            target_digest=bytes(row[4]),
            preview_digest=bytes(row[5]) if row[5] is not None else None,
            idempotency_key=str(row[6]),
            state_version=int(row[7]),
        )

    @staticmethod
    def _transition(
        db: sqlite3.Connection,
        action_id: str,
        *,
        expected: tuple[str, ...],
        to_state: str,
        reason_code: str,
        now_us: int,
    ) -> None:
        row = db.execute(
            "SELECT state,state_version FROM actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if row is None:
            raise ActionConflictError("unknown action")
        state, version = str(row[0]), int(row[1])
        if state not in expected:
            wanted = " or ".join(expected)
            raise ActionConflictError(
                f"action must be {wanted}; durable state is {state}"
            )
        changed = db.execute(
            "UPDATE actions SET state=?,updated_at_us=?,state_version=state_version+1 "
            "WHERE action_id=? AND state_version=?",
            (to_state, now_us, action_id, version),
        ).rowcount
        if changed != 1:
            raise ActionConflictError("action state changed concurrently")
        db.execute(
            "INSERT INTO action_events("
            "action_id,seq,from_state,to_state,reason_code,at_us"
            ") VALUES (?,?,?,?,?,?)",
            (action_id, version + 1, state, to_state, reason_code, now_us),
        )

    def mark_ready(self, action_id: str, *, reason_code: str, now_us: int) -> None:
        with self._store.transaction() as db:
            self._transition(
                db,
                action_id,
                expected=("drafted",),
                to_state="ready",
                reason_code=reason_code,
                now_us=now_us,
            )

    def record_confirmation(
        self,
        action_id: str,
        evidence: ConfirmationEvidence,
    ) -> None:
        for name, value in (
            ("pairing_identity_digest", evidence.pairing_identity_digest),
            ("authorization_digest", evidence.authorization_digest),
            ("payload_digest", evidence.payload_digest),
            ("preview_digest", evidence.preview_digest),
        ):
            _require_digest(name, value)
        if not evidence.challenge_id or not evidence.challenge_token or not evidence.session_id:
            raise ValueError("confirmation identity is required")
        if not (
            evidence.issued_at_us <= evidence.preview_completed_at_us
            <= evidence.decided_at_us
        ):
            raise ValueError("confirmation timestamps are out of order")
        expired = evidence.decided_at_us > evidence.expires_at_us
        decision = "approved" if evidence.accepted else "rejected"
        final_state = "expired" if expired else decision

        with self._store.transaction() as db:
            record = self._get_with(db, action_id)
            if record.state != "ready":
                raise ActionConflictError(
                    f"confirmation requires ready action; durable state is {record.state}"
                )
            if record.payload_digest != evidence.payload_digest:
                raise ActionConflictError("confirmation payload does not match durable draft")
            db.execute(
                "UPDATE actions SET preview_digest=? WHERE action_id=?",
                (evidence.preview_digest, action_id),
            )
            try:
                db.execute(
                    "INSERT INTO confirmation_challenges("
                    "challenge_id,action_id,token_hash,session_id,"
                    "pairing_identity_digest,authorization_digest,issued_at_us,"
                    "expires_at_us,preview_completed_at_us,consumed_at_us,decision"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        evidence.challenge_id,
                        action_id,
                        _confirmation_token_hash(evidence.challenge_token),
                        evidence.session_id,
                        evidence.pairing_identity_digest,
                        evidence.authorization_digest,
                        evidence.issued_at_us,
                        evidence.expires_at_us,
                        evidence.preview_completed_at_us,
                        evidence.decided_at_us,
                        decision,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ActionConflictError("confirmation was already consumed") from exc
            self._transition(
                db,
                action_id,
                expected=("ready",),
                to_state="awaiting_confirmation",
                reason_code="preview_completed",
                now_us=evidence.preview_completed_at_us,
            )
            self._transition(
                db,
                action_id,
                expected=("awaiting_confirmation",),
                to_state=final_state,
                reason_code="confirmation_expired" if expired else f"wearer_{decision}",
                now_us=evidence.decided_at_us,
            )

    def begin_attempt(
        self,
        action_id: str,
        *,
        adapter: str,
        adapter_version: str,
        request_id: str,
        now_us: int,
    ) -> AttemptRecord:
        if not adapter or not adapter_version or not request_id:
            raise ValueError("adapter identity and request ID are required")
        with self._store.transaction() as db:
            record = self._get_with(db, action_id)
            if record.state != "approved":
                raise ActionConflictError(
                    f"dispatch requires approved action; durable state is {record.state}"
                )
            row = db.execute(
                "SELECT coalesce(max(attempt_no),0) FROM action_attempts WHERE action_id=?",
                (action_id,),
            ).fetchone()
            attempt_no = int(row[0]) + 1 if row is not None else 1
            attempt_id = str(uuid.uuid4())
            try:
                db.execute(
                    "INSERT INTO action_attempts("
                    "attempt_id,action_id,attempt_no,adapter,adapter_version,request_id,"
                    "state,started_at_us,dispatch_started_at_us"
                    ") VALUES (?,?,?,?,?,?,'dispatching',?,?)",
                    (
                        attempt_id,
                        action_id,
                        attempt_no,
                        adapter,
                        adapter_version,
                        request_id,
                        now_us,
                        now_us,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ActionConflictError("attempt conflicts with durable state") from exc
            self._transition(
                db,
                action_id,
                expected=("approved",),
                to_state="dispatching",
                reason_code="attempt_committed",
                now_us=now_us,
            )
            return AttemptRecord(attempt_id, action_id, request_id, "dispatching")

    def record_success(
        self,
        action_id: str,
        attempt_id: str,
        *,
        outcome: str,
        verification_level: str,
        evidence_digest: bytes,
        now_us: int,
        external_item_id: str | None = None,
        external_reference: ExternalReference | None = None,
    ) -> None:
        _require_digest("evidence_digest", evidence_digest)
        if external_item_id is not None and external_reference is not None:
            raise ValueError("provide an external item id or reference, not both")
        if external_reference is not None:
            _require_digest("external scope_digest", external_reference.scope_digest)
            if any(
                not value or value != value.strip()
                for value in (
                    external_reference.system,
                    external_reference.kind,
                    external_reference.identifier_kind,
                    external_reference.identifier_value,
                )
            ):
                raise ValueError("external reference fields must be non-empty and unpadded")
        with self._store.transaction() as db:
            attempt = db.execute(
                "SELECT state FROM action_attempts WHERE attempt_id=? AND action_id=?",
                (attempt_id, action_id),
            ).fetchone()
            if attempt is None or str(attempt[0]) not in ("dispatching", "verifying"):
                raise ActionConflictError("attempt is not awaiting a result")
            db.execute(
                "UPDATE action_attempts SET state='succeeded',finished_at_us=?,"
                "result_digest=? WHERE attempt_id=?",
                (now_us, evidence_digest, attempt_id),
            )
            if external_reference is not None:
                external_item_id = self._upsert_external_reference(
                    db,
                    external_reference,
                    fingerprint=evidence_digest,
                    now_us=now_us,
                )
            receipt_id = str(uuid.uuid4())
            try:
                db.execute(
                    "INSERT INTO action_receipts("
                    "receipt_id,action_id,attempt_id,outcome,verification_level,"
                    "evidence_digest,observed_at_us,external_item_id"
                    ") VALUES (?,?,?,?,?,?,?,?)",
                    (
                        receipt_id,
                        action_id,
                        attempt_id,
                        outcome,
                        verification_level,
                        evidence_digest,
                        now_us,
                        external_item_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ActionConflictError("receipt conflicts with durable state") from exc
            self._transition(
                db,
                action_id,
                expected=("dispatching", "verifying"),
                to_state="succeeded",
                reason_code=f"receipt_{verification_level}",
                now_us=now_us,
            )

    @staticmethod
    def _upsert_external_reference(
        db: sqlite3.Connection,
        reference: ExternalReference,
        *,
        fingerprint: bytes,
        now_us: int,
    ) -> str:
        row = db.execute(
            "SELECT x.external_item_id,e.system,e.kind,e.scope_digest "
            "FROM external_identifiers x JOIN external_items e "
            "ON e.external_item_id=x.external_item_id "
            "WHERE x.identifier_kind=? AND x.identifier_value=? "
            "AND x.valid_to_us IS NULL",
            (reference.identifier_kind, reference.identifier_value),
        ).fetchone()
        if row is not None:
            if (
                str(row[1]) != reference.system
                or str(row[2]) != reference.kind
            ):
                raise ActionConflictError(
                    "external identifier already names a different object kind"
                )
            external_item_id = str(row[0])
            db.execute(
                "UPDATE external_items SET scope_digest=?,fingerprint=?,last_seen_us=? "
                "WHERE external_item_id=?",
                (reference.scope_digest, fingerprint, now_us, external_item_id),
            )
            return external_item_id

        external_item_id = str(uuid.uuid4())
        try:
            db.execute(
                "INSERT INTO external_items(external_item_id,system,kind,scope_digest,"
                "fingerprint,first_seen_us,last_seen_us) VALUES (?,?,?,?,?,?,?)",
                (
                    external_item_id,
                    reference.system,
                    reference.kind,
                    reference.scope_digest,
                    fingerprint,
                    now_us,
                    now_us,
                ),
            )
            db.execute(
                "INSERT INTO external_identifiers(external_item_id,identifier_kind,"
                "identifier_value,valid_from_us) VALUES (?,?,?,?)",
                (
                    external_item_id,
                    reference.identifier_kind,
                    reference.identifier_value,
                    now_us,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ActionConflictError("external reference conflicts with durable state") from exc
        return external_item_id

    def record_failure(
        self,
        action_id: str,
        attempt_id: str,
        *,
        error_code: str,
        possible_external_commit: bool,
        now_us: int,
    ) -> None:
        """Finish an attempt without converting ambiguity into a safe retry."""

        attempt_state = "uncertain" if possible_external_commit else "failed"
        action_state = "uncertain" if possible_external_commit else "failed"
        with self._store.transaction() as db:
            attempt = db.execute(
                "SELECT state FROM action_attempts WHERE attempt_id=? AND action_id=?",
                (attempt_id, action_id),
            ).fetchone()
            if attempt is None or str(attempt[0]) not in ("dispatching", "verifying"):
                raise ActionConflictError("attempt is not awaiting a result")
            db.execute(
                "UPDATE action_attempts SET state=?,finished_at_us=?,error_code=? "
                "WHERE attempt_id=?",
                (attempt_state, now_us, error_code, attempt_id),
            )
            self._transition(
                db,
                action_id,
                expected=("dispatching", "verifying"),
                to_state=action_state,
                reason_code=(
                    "external_commit_ambiguous"
                    if possible_external_commit
                    else "executor_failed_before_commit"
                ),
                now_us=now_us,
            )

    def cancel_unconfirmed(
        self,
        action_id: str,
        *,
        reason_code: str,
        now_us: int,
    ) -> None:
        with self._store.transaction() as db:
            self._transition(
                db,
                action_id,
                expected=("ready", "awaiting_confirmation"),
                to_state="cancelled",
                reason_code=reason_code,
                now_us=now_us,
            )

    def purge_payload(self, action_id: str) -> None:
        """Remove retained cleartext after reconciliation no longer needs it."""

        record = self.get(action_id)
        if record.state not in (
            "rejected",
            "expired",
            "succeeded",
            "failed",
            "cancelled",
            "superseded",
            "invalidated",
            "reconciled_succeeded",
            "reconciled_absent",
        ):
            raise ActionConflictError(
                f"payload is still required while action is {record.state}"
            )
        self._store.execute(
            "DELETE FROM action_payloads WHERE action_id=?",
            (action_id,),
        )

    def recover_after_restart(self, *, now_us: int) -> dict[str, str]:
        """Invalidate approvals and quarantine calls that may have committed."""

        recovered: dict[str, str] = {}
        with self._store.transaction() as db:
            rows = db.execute(
                "SELECT action_id,state FROM actions WHERE state IN ("
                "'ready','awaiting_confirmation','approved','dispatching','verifying') "
                "ORDER BY created_at_us,action_id"
            ).fetchall()
            for raw_action_id, raw_state in rows:
                action_id, state = str(raw_action_id), str(raw_state)
                if state in ("dispatching", "verifying"):
                    db.execute(
                        "UPDATE action_attempts SET state='uncertain',finished_at_us=? "
                        "WHERE action_id=? AND state IN ('dispatching','verifying')",
                        (now_us, action_id),
                    )
                    target, reason = "uncertain", "restart_after_possible_dispatch"
                else:
                    target, reason = "invalidated", "restart_invalidated_authority"
                self._transition(
                    db,
                    action_id,
                    expected=(state,),
                    to_state=target,
                    reason_code=reason,
                    now_us=now_us,
                )
                if target == "invalidated":
                    db.execute(
                        "DELETE FROM action_payloads WHERE action_id=?",
                        (action_id,),
                    )
                recovered[action_id] = target
        return recovered

    def event_states(self, action_id: str) -> tuple[str, ...]:
        rows = self._store.execute(
            "SELECT to_state FROM action_events WHERE action_id=? ORDER BY seq",
            (action_id,),
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def attempt_state(self, attempt_id: str) -> str:
        row = self._store.execute(
            "SELECT state FROM action_attempts WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise ActionConflictError("unknown attempt")
        return str(row[0])
