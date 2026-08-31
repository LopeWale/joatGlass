from __future__ import annotations

from joat.services.brainstorm import BrainstormStore


def test_brainstorm_store_persists_a_private_session_across_restart(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "brainstorm-sessions.json"

    created = BrainstormStore(path).create("How should JOAT handle follow-ups?")
    reopened = BrainstormStore(path).get(created.session_id)

    assert reopened is not None
    assert reopened.session_id == created.session_id
    assert reopened.title == "How should JOAT handle follow-ups?"
    assert reopened.status == "active"
    assert path.stat().st_mode & 0o777 == 0o600


def test_brainstorm_store_retains_turns_verification_and_synthesis(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = BrainstormStore(tmp_path / "brainstorm-sessions.json")
    session = store.create("Should JOAT retain context?")

    store.append_turn(session.session_id, "YOU", "Compare the trade-offs.")
    store.append_turn(session.session_id, "CODEX", "Persistence supports follow-ups.")
    store.settle(
        session.session_id,
        verification_state="verified",
        synthesis="JOAT should retain explicit session context.",
    )

    reopened = BrainstormStore(store.path).get(session.session_id)
    assert reopened is not None
    assert [(turn.speaker, turn.text) for turn in reopened.turns] == [
        ("YOU", "Compare the trade-offs."),
        ("CODEX", "Persistence supports follow-ups."),
    ]
    assert reopened.verification_state == "verified"
    assert reopened.synthesis == "JOAT should retain explicit session context."
    assert reopened.status == "settled"


def test_branch_copies_context_and_records_lineage(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = BrainstormStore(tmp_path / "brainstorm-sessions.json")
    root = store.create("Plan a private home brain")
    store.append_turn(root.session_id, "YOU", "Start with one Mac mini.")

    branch = store.branch(root.session_id, "Compare two Mac minis")

    assert branch.branch_parent_id == root.session_id
    assert branch.branch_root_id == root.session_id
    assert branch.title == "Compare two Mac minis"
    assert [(turn.speaker, turn.text) for turn in branch.turns] == [
        ("YOU", "Start with one Mac mini."),
    ]
    assert branch.status == "active"


def test_recent_sessions_are_newest_first_and_cancel_is_durable(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = BrainstormStore(tmp_path / "brainstorm-sessions.json")
    first = store.create("First topic")
    second = store.create("Second topic")
    store.cancel(first.session_id)

    recent = store.recent(limit=2)

    assert [session.session_id for session in recent] == [first.session_id, second.session_id]
    assert recent[0].status == "cancelled"
