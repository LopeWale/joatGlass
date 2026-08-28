"""Text QuickNotes remain exact, local, durable, and reversibly deletable."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from joat.persistence.notes import (
    NoteConflictError,
    NoteNotFoundError,
    NoteSource,
    NoteStore,
)
from joat.persistence.store import DurableStore, StorageSchemaError


def create_note(
    notes: NoteStore,
    *,
    note_id: str = "note-1",
    idempotency_key: str = "capture-1",
    text: str = "Exact visible words",
    now_us: int = 100,
):
    return notes.create(
        note_id=note_id,
        idempotency_key=idempotency_key,
        text=text,
        source=NoteSource(kind="hud.quick-note", identifier="voice-session-7"),
        now_us=now_us,
    )


def test_exact_text_and_source_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "joat.sqlite3"
    visible_text = "  First line\nSecond line with Café 👩🏽‍💻  "

    with DurableStore(path) as store:
        created = create_note(NoteStore(store), text=visible_text)
        assert store.schema_version == 2
        assert created.text == visible_text
        assert created.source == NoteSource(
            kind="hud.quick-note", identifier="voice-session-7"
        )
        assert created.created_at_us == created.updated_at_us == 100
        assert created.deleted_at_us is None

    with DurableStore(path) as store:
        restored = NoteStore(store).get("note-1")
        assert restored == created
        assert restored.text.encode("utf-8") == visible_text.encode("utf-8")


def test_soft_delete_is_immediate_search_exclusion_and_undo_is_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "joat.sqlite3"
    with DurableStore(path) as store:
        notes = NoteStore(store)
        create_note(notes)

        deleted = notes.delete("note-1", now_us=200)
        assert deleted.deleted_at_us == deleted.updated_at_us == 200
        assert notes.delete("note-1", now_us=201) == deleted
        assert notes.search("visible") == ()
        with pytest.raises(NoteNotFoundError):
            notes.get("note-1")
        assert notes.get("note-1", include_deleted=True) == deleted

    with DurableStore(path) as store:
        notes = NoteStore(store)
        restored = notes.undo_delete("note-1", now_us=300)
        assert restored.deleted_at_us is None
        assert restored.updated_at_us == 300
        assert notes.undo_delete("note-1", now_us=301) == restored
        assert notes.search("visible") == (restored,)


def test_create_replay_requires_exact_idempotent_payload(tmp_path: Path) -> None:
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        notes = NoteStore(store)
        first = create_note(notes)

        assert create_note(notes, now_us=999) == first
        with pytest.raises(NoteConflictError, match="idempotency"):
            create_note(notes, text="changed")
        with pytest.raises(NoteConflictError, match="idempotency"):
            create_note(notes, note_id="note-2")
        with pytest.raises(NoteConflictError, match="identity"):
            create_note(notes, idempotency_key="capture-2")

        notes.delete("note-1", now_us=200)
        replay = create_note(notes, now_us=999)
        assert replay.deleted_at_us == 200


def test_search_is_unicode_aware_exact_and_stably_ordered(tmp_path: Path) -> None:
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        notes = NoteStore(store)
        second = create_note(
            notes,
            note_id="note-b",
            idempotency_key="capture-b",
            text="Straße — 東京 👩🏽‍💻",
            now_us=500,
        )
        first = create_note(
            notes,
            note_id="note-a",
            idempotency_key="capture-a",
            text="STRASSE planning",
            now_us=500,
        )
        create_note(
            notes,
            note_id="note-c",
            idempotency_key="capture-c",
            text="unrelated",
            now_us=900,
        )

        assert notes.search("strasse") == (first, second)
        assert notes.search("東京 👩🏽‍💻") == (second,)
        assert notes.search("", limit=2)[0].note_id == "note-c"


def test_invalid_text_and_time_regression_fail_before_mutation(tmp_path: Path) -> None:
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        notes = NoteStore(store)
        with pytest.raises(ValueError, match="text"):
            create_note(notes, text="")
        with pytest.raises(ValueError, match="text"):
            create_note(notes, text="hidden\x00suffix")

        original = create_note(notes, now_us=100)
        with pytest.raises(NoteConflictError, match="timestamp"):
            notes.delete("note-1", now_us=99)
        assert notes.get("note-1") == original


def test_v1_store_is_upgraded_without_rewriting_its_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import joat.persistence.store as store_module

    path = tmp_path / "joat.sqlite3"
    all_migrations = store_module._MIGRATIONS
    migration_1 = all_migrations[0]
    monkeypatch.setattr(store_module, "_MIGRATIONS", (migration_1,))
    with DurableStore(path) as store:
        assert store.schema_version == 1
        v1_ledger = store.execute(
            "SELECT name,checksum FROM schema_migrations WHERE version=1"
        ).fetchone()

    monkeypatch.setattr(store_module, "_MIGRATIONS", all_migrations)
    with DurableStore(path) as upgraded:
        assert upgraded.schema_version == 2
        assert upgraded.execute(
            "SELECT name,checksum FROM schema_migrations WHERE version=1"
        ).fetchone() == v1_ledger
        create_note(NoteStore(upgraded))


def test_corrupted_note_schema_is_rejected_by_manifest(tmp_path: Path) -> None:
    path = tmp_path / "joat.sqlite3"
    with DurableStore(path):
        pass

    conn = sqlite3.connect(path)
    conn.execute("DROP INDEX quick_notes_active_order")
    conn.close()

    with pytest.raises(StorageSchemaError, match="manifest"):
        DurableStore(path)
