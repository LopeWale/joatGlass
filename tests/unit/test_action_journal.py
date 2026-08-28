"""Consequential actions survive crashes without becoming replayable authority."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from joat.persistence.actions import (
    ActionConflictError,
    ActionDraft,
    ActionJournal,
    ConfirmationEvidence,
)
from joat.persistence.store import DurableStore


def digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def draft(key: str = "message:trace-1") -> ActionDraft:
    ordinal = 91 if key == "message:trace-1" else 92
    return ActionDraft(
        kind="message.reply",
        canonical_payload=b'{"body":"On my way","recipient":"+14055550123"}',
        target_digest=digest("+14055550123"),
        idempotency_key=key,
        origin_kind="messages.thread",
        origin_id="chat-7",
        origin_ordinal=ordinal,
    )


def approved_action(journal: ActionJournal, *, now_us: int = 1_000_000) -> str:
    action = journal.create_draft(draft(), now_us=now_us)
    journal.mark_ready(action.action_id, reason_code="schema_valid", now_us=now_us + 1)
    journal.record_confirmation(
        action.action_id,
        ConfirmationEvidence(
            challenge_id="challenge-1",
            challenge_token="wearer-proof-1",
            session_id="session-1",
            pairing_identity_digest=digest("paired-glasses"),
            authorization_digest=digest("bound-action-preview"),
            payload_digest=digest('{"body":"On my way","recipient":"+14055550123"}'),
            preview_digest=digest("Send to Dele? On my way"),
            issued_at_us=now_us + 2,
            expires_at_us=now_us + 10_000_000,
            preview_completed_at_us=now_us + 3,
            decided_at_us=now_us + 4,
            accepted=True,
        ),
    )
    return action.action_id


def test_idempotency_replays_only_the_identical_draft(tmp_path: Path) -> None:
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        journal = ActionJournal(store)
        first = journal.create_draft(draft(), now_us=1)
        replay = journal.create_draft(draft(), now_us=2)

        assert replay == first
        assert journal.event_states(first.action_id) == ("drafted",)

        changed = ActionDraft(
            kind="message.reply",
            canonical_payload=b'{"body":"Different","recipient":"+14055550123"}',
            target_digest=digest("+14055550123"),
            idempotency_key=first.idempotency_key,
        )
        with pytest.raises(ActionConflictError, match="idempotency"):
            journal.create_draft(changed, now_us=3)


def test_recent_activity_is_newest_first_and_never_exposes_payload(tmp_path: Path) -> None:
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        journal = ActionJournal(store)
        first = journal.create_draft(draft(), now_us=1)
        second = journal.create_draft(draft("message:trace-2"), now_us=2)
        journal.mark_ready(second.action_id, reason_code="schema_valid", now_us=3)

        activity = journal.recent(limit=2)

        assert [item.action_id for item in activity] == [second.action_id, first.action_id]
        assert [(item.kind, item.state) for item in activity] == [
            ("message.reply", "ready"),
            ("message.reply", "drafted"),
        ]
        assert not hasattr(activity[0], "canonical_payload")
        assert not hasattr(activity[0], "payload_digest")


def test_recent_activity_rejects_unbounded_limits(tmp_path: Path) -> None:
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        journal = ActionJournal(store)
        with pytest.raises(ValueError, match="between 1 and 20"):
            journal.recent(limit=21)


def test_confirmation_is_exact_bound_single_use_and_expiring(tmp_path: Path) -> None:
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        journal = ActionJournal(store)
        action_id = approved_action(journal)

        assert journal.get(action_id).state == "approved"
        assert journal.event_states(action_id) == (
            "drafted",
            "ready",
            "awaiting_confirmation",
            "approved",
        )

        with pytest.raises(ActionConflictError, match="confirmation"):
            journal.record_confirmation(
                action_id,
                ConfirmationEvidence(
                    challenge_id="challenge-2",
                    challenge_token="wearer-proof-2",
                    session_id="session-1",
                    pairing_identity_digest=digest("paired-glasses"),
                    authorization_digest=digest("bound-action-preview"),
                    payload_digest=digest('{"body":"On my way","recipient":"+14055550123"}'),
                    preview_digest=digest("Send to Dele? On my way"),
                    issued_at_us=2,
                    expires_at_us=3,
                    preview_completed_at_us=2,
                    decided_at_us=3,
                    accepted=True,
                ),
            )

        # Only a salted digest is durable; the bearer-like proof is never stored.
        stored = store.execute(
            "SELECT token_hash FROM confirmation_challenges WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        assert stored is not None
        assert stored[0] != b"wearer-proof-1"
        assert b"wearer-proof-1" not in store.path.read_bytes()


def test_expired_confirmation_cannot_authorize_dispatch(tmp_path: Path) -> None:
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        journal = ActionJournal(store)
        action = journal.create_draft(draft(), now_us=1)
        journal.mark_ready(action.action_id, reason_code="schema_valid", now_us=2)
        journal.record_confirmation(
            action.action_id,
            ConfirmationEvidence(
                challenge_id="late",
                challenge_token="late-proof",
                session_id="session-1",
                pairing_identity_digest=digest("paired-glasses"),
                authorization_digest=digest("bound-action-preview"),
                payload_digest=digest('{"body":"On my way","recipient":"+14055550123"}'),
                preview_digest=digest("preview"),
                issued_at_us=3,
                expires_at_us=4,
                preview_completed_at_us=3,
                decided_at_us=5,
                accepted=True,
            ),
        )

        assert journal.get(action.action_id).state == "expired"
        with pytest.raises(ActionConflictError, match="approved"):
            journal.begin_attempt(
                action.action_id,
                adapter="messages.applescript",
                adapter_version="1",
                request_id="request-late",
                now_us=6,
            )


def test_attempt_intent_commits_before_external_call_and_receipt_is_honest(
    tmp_path: Path,
) -> None:
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        journal = ActionJournal(store)
        action_id = approved_action(journal)

        attempt = journal.begin_attempt(
            action_id,
            adapter="messages.applescript",
            adapter_version="1",
            request_id="request-1",
            now_us=2_000_000,
        )
        assert journal.get(action_id).state == "dispatching"
        assert attempt.state == "dispatching"

        journal.record_success(
            action_id,
            attempt.attempt_id,
            outcome="submitted_to_messages",
            verification_level="delivery_unverified",
            evidence_digest=digest("osascript-exit-0"),
            now_us=2_000_001,
        )

        assert journal.get(action_id).state == "succeeded"
        receipt = store.execute(
            "SELECT outcome, verification_level FROM action_receipts "
            "WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        assert receipt == ("submitted_to_messages", "delivery_unverified")


def test_restart_invalidates_unspent_authority_and_marks_ambiguous_dispatch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "joat.sqlite3"
    with DurableStore(path) as store:
        journal = ActionJournal(store)
        approved = approved_action(journal)

        second = journal.create_draft(draft("message:trace-2"), now_us=2_000_000)
        journal.mark_ready(second.action_id, reason_code="schema_valid", now_us=2_000_001)
        journal.record_confirmation(
            second.action_id,
            ConfirmationEvidence(
                challenge_id="challenge-2",
                challenge_token="wearer-proof-2",
                session_id="session-2",
                pairing_identity_digest=digest("paired-glasses"),
                authorization_digest=digest("bound-action-preview-2"),
                payload_digest=digest('{"body":"On my way","recipient":"+14055550123"}'),
                preview_digest=digest("preview-2"),
                issued_at_us=2_000_002,
                expires_at_us=9_000_000,
                preview_completed_at_us=2_000_003,
                decided_at_us=2_000_004,
                accepted=True,
            ),
        )
        attempt = journal.begin_attempt(
            second.action_id,
            adapter="messages.applescript",
            adapter_version="1",
            request_id="request-crash",
            now_us=2_000_005,
        )
        ready = journal.create_draft(
            ActionDraft(
                kind="calendar_event",
                canonical_payload=b'{"title":"unconfirmed"}',
                target_digest=digest("calendar"),
                idempotency_key="calendar:ready-before-restart",
            ),
            now_us=2_000_006,
        )
        journal.mark_ready(
            ready.action_id,
            reason_code="schema_valid",
            now_us=2_000_007,
        )

    with DurableStore(path) as reopened:
        recovered = ActionJournal(reopened).recover_after_restart(now_us=3_000_000)
        assert recovered == {
            approved: "invalidated",
            second.action_id: "uncertain",
            ready.action_id: "invalidated",
        }

        journal = ActionJournal(reopened)
        assert journal.get(approved).state == "invalidated"
        assert journal.get(second.action_id).state == "uncertain"
        assert journal.get(ready.action_id).state == "invalidated"
        assert reopened.execute(
            "SELECT count(*) FROM action_payloads WHERE action_id IN (?,?)",
            (approved, ready.action_id),
        ).fetchone()[0] == 0
        assert journal.attempt_state(attempt.attempt_id) == "uncertain"
        with pytest.raises(ActionConflictError, match="approved"):
            journal.begin_attempt(
                second.action_id,
                adapter="messages.applescript",
                adapter_version="1",
                request_id="request-retry",
                now_us=3_000_001,
            )
