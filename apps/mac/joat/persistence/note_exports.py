"""Where a JOAT note was written outside JOAT (Apple Notes).

A note is exported automatically after it is saved and may be sent again
later (re-summarized with Claude, or by the spoken "send … to my notes").
Remembering the Apple Notes id lets that second write update the same note
instead of leaving the wearer a duplicate.
"""

from __future__ import annotations

from typing import Literal, cast

from .store import DurableStore

NoteKind = Literal["voice", "text"]
ExportTarget = Literal["apple_notes"]

_KINDS = frozenset({"voice", "text"})
_TARGETS = frozenset({"apple_notes"})


class NoteExportStore:
    """One external id per (note kind, note id, target)."""

    def __init__(self, store: DurableStore) -> None:
        self._store = store

    def external_id(
        self, kind: NoteKind, note_id: str, target: ExportTarget = "apple_notes",
    ) -> str | None:
        self._validate(kind, note_id, target)
        row = self._store.execute(
            "SELECT external_id FROM note_exports "
            "WHERE note_kind=? AND note_id=? AND target=?",
            (kind, note_id, target),
        ).fetchone()
        return cast(str, row[0]) if row is not None else None

    def record(
        self,
        kind: NoteKind,
        note_id: str,
        external_id: str,
        *,
        now_us: int,
        target: ExportTarget = "apple_notes",
    ) -> None:
        self._validate(kind, note_id, target)
        if (
            not isinstance(external_id, str)
            or not external_id.strip()
            or len(external_id) > 512
            or "\x00" in external_id
        ):
            raise ValueError("external id must be 1 to 512 visible characters")
        if isinstance(now_us, bool) or not isinstance(now_us, int) or now_us < 0:
            raise ValueError("timestamp must be a non-negative integer")
        with self._store.transaction() as db:
            db.execute(
                "INSERT INTO note_exports(note_kind,note_id,target,external_id,"
                "exported_at_us) VALUES (?,?,?,?,?) "
                "ON CONFLICT(note_kind,note_id,target) DO UPDATE SET "
                "external_id=excluded.external_id,"
                "exported_at_us=excluded.exported_at_us",
                (kind, note_id, target, external_id.strip(), now_us),
            )

    @staticmethod
    def _validate(kind: str, note_id: str, target: str) -> None:
        if kind not in _KINDS:
            raise ValueError("note kind must be 'voice' or 'text'")
        if target not in _TARGETS:
            raise ValueError("unknown export target")
        if not isinstance(note_id, str) or not note_id or len(note_id) > 256:
            raise ValueError("note id must be 1 to 256 characters")


__all__ = ["ExportTarget", "NoteExportStore", "NoteKind"]
