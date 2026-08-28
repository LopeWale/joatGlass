"""Exact-text, owner-local QuickNotes on the process-owned durable store."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import cast

from .store import DurableStore


class NoteError(RuntimeError):
    """Base class for durable QuickNote failures."""


class NoteConflictError(NoteError):
    """The requested mutation conflicts with an existing durable note."""


class NoteNotFoundError(NoteError):
    """No visible note has the requested identity."""


@dataclass(frozen=True, slots=True)
class NoteSource:
    """Local provenance supplied by the capture surface."""

    kind: str
    identifier: str | None = None


@dataclass(frozen=True, slots=True)
class QuickNote:
    """One exact visible text capture; no audio or generated summary is stored."""

    note_id: str
    idempotency_key: str
    text: str
    source: NoteSource
    created_at_us: int
    updated_at_us: int
    deleted_at_us: int | None


def _require_identifier(name: str, value: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty, unpadded string")
    _require_utf8(name, value)


def _require_utf8(name: str, value: str) -> None:
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8 text") from exc


def _require_timestamp(now_us: int) -> None:
    if isinstance(now_us, bool) or not isinstance(now_us, int) or now_us < 0:
        raise ValueError("now_us must be a non-negative integer")


class NoteStore:
    """Persist and search reversible text QuickNotes.

    Soft deletion immediately hides a note from normal reads and search, but
    deliberately retains its text so an explicit undo can restore it. It is not
    a privacy-erasure operation.
    """

    def __init__(self, store: DurableStore) -> None:
        self._store = store

    def create(
        self,
        *,
        note_id: str,
        idempotency_key: str,
        text: str,
        source: NoteSource,
        now_us: int,
    ) -> QuickNote:
        _require_identifier("note_id", note_id)
        _require_identifier("idempotency_key", idempotency_key)
        if not isinstance(text, str) or not text:
            raise ValueError("text must be non-empty")
        _require_utf8("text", text)
        if not isinstance(source, NoteSource):
            raise ValueError("source must be NoteSource metadata")
        _require_identifier("source.kind", source.kind)
        if source.identifier is not None:
            _require_identifier("source.identifier", source.identifier)
        _require_timestamp(now_us)

        with self._store.transaction() as db:
            existing = db.execute(
                "SELECT note_id,idempotency_key,visible_text,source_kind,"
                "source_identifier,created_at_us,updated_at_us,deleted_at_us "
                "FROM quick_notes WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                record = self._from_row(existing)
                if (
                    record.note_id != note_id
                    or record.text != text
                    or record.source != source
                ):
                    raise NoteConflictError(
                        "idempotency key already names a different QuickNote"
                    )
                return record

            try:
                db.execute(
                    "INSERT INTO quick_notes("
                    "note_id,idempotency_key,visible_text,source_kind,"
                    "source_identifier,created_at_us,updated_at_us"
                    ") VALUES (?,?,?,?,?,?,?)",
                    (
                        note_id,
                        idempotency_key,
                        text,
                        source.kind,
                        source.identifier,
                        now_us,
                        now_us,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise NoteConflictError(
                    "QuickNote identity conflicts with durable state"
                ) from exc
            return self._get_with(db, note_id, include_deleted=True)

    def get(self, note_id: str, *, include_deleted: bool = False) -> QuickNote:
        _require_identifier("note_id", note_id)
        return self._get_with(
            self._store._db,
            note_id,
            include_deleted=include_deleted,
        )

    def search(self, query: str, *, limit: int = 20) -> tuple[QuickNote, ...]:
        if not isinstance(query, str):
            raise ValueError("query must be text")
        _require_utf8("query", query)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

        needle = query.casefold()
        rows = self._store.execute(
            "SELECT note_id,idempotency_key,visible_text,source_kind,"
            "source_identifier,created_at_us,updated_at_us,deleted_at_us "
            "FROM quick_notes WHERE deleted_at_us IS NULL "
            "ORDER BY updated_at_us DESC,note_id ASC"
        )
        matches: list[QuickNote] = []
        for row in rows:
            note = self._from_row(row)
            if needle in note.text.casefold():
                matches.append(note)
                if len(matches) == limit:
                    break
        return tuple(matches)

    def delete(self, note_id: str, *, now_us: int) -> QuickNote:
        """Hide a note immediately while retaining its text for undo."""

        return self._set_deleted(note_id, deleted=True, now_us=now_us)

    def undo_delete(self, note_id: str, *, now_us: int) -> QuickNote:
        return self._set_deleted(note_id, deleted=False, now_us=now_us)

    def _set_deleted(
        self,
        note_id: str,
        *,
        deleted: bool,
        now_us: int,
    ) -> QuickNote:
        _require_identifier("note_id", note_id)
        _require_timestamp(now_us)
        with self._store.transaction() as db:
            current = self._get_with(db, note_id, include_deleted=True)
            is_deleted = current.deleted_at_us is not None
            if is_deleted == deleted:
                return current
            if now_us < current.updated_at_us:
                raise NoteConflictError("mutation timestamp precedes durable note timestamp")
            db.execute(
                "UPDATE quick_notes SET updated_at_us=?,deleted_at_us=? WHERE note_id=?",
                (now_us, now_us if deleted else None, note_id),
            )
            return self._get_with(db, note_id, include_deleted=True)

    @classmethod
    def _get_with(
        cls,
        db: sqlite3.Connection,
        note_id: str,
        *,
        include_deleted: bool,
    ) -> QuickNote:
        sql = (
            "SELECT note_id,idempotency_key,visible_text,source_kind,"
            "source_identifier,created_at_us,updated_at_us,deleted_at_us "
            "FROM quick_notes WHERE note_id=?"
        )
        if not include_deleted:
            sql += " AND deleted_at_us IS NULL"
        row = db.execute(sql, (note_id,)).fetchone()
        if row is None:
            raise NoteNotFoundError("unknown or deleted QuickNote")
        return cls._from_row(row)

    @staticmethod
    def _from_row(row: tuple[object, ...] | sqlite3.Row) -> QuickNote:
        return QuickNote(
            note_id=cast(str, row[0]),
            idempotency_key=cast(str, row[1]),
            text=cast(str, row[2]),
            source=NoteSource(
                kind=cast(str, row[3]),
                identifier=cast(str, row[4]) if row[4] is not None else None,
            ),
            created_at_us=cast(int, row[5]),
            updated_at_us=cast(int, row[6]),
            deleted_at_us=cast(int, row[7]) if row[7] is not None else None,
        )
