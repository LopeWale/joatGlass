"""Explicit text-only QuickNote commands.

This is not a meeting recorder. It stores only the visible final transcript
the wearer explicitly asks to save; there is no raw audio or completeness
claim.
"""

from __future__ import annotations

import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

from ..persistence.notes import NoteNotFoundError, NoteSource, NoteStore


class NoteCommandController:
    def __init__(
        self,
        notes: NoteStore,
        *,
        export_directory: Path | None = None,
    ) -> None:
        self._notes = notes
        self._export_directory = export_directory or (
            Path.home() / "Library/Application Support/JOAT/Exports"
        )
        self._last_deleted_id: str | None = None

    async def handle_command(self, text: str) -> str | None:
        stripped = text.strip()
        lowered = stripped.casefold()
        # Whisper punctuates short utterances ("Export notes."). Terminal
        # punctuation is STT noise for the exact-match commands — but note
        # BODIES keep the wearer's punctuation untouched.
        command = lowered.rstrip(".,!?").rstrip()
        for prefix in ("take a note ", "note "):
            if lowered.startswith(prefix):
                body = stripped[len(prefix):]
                return self._save(body)
        if command in ("take a note", "note"):
            return "Say the text after “take a note”."
        if lowered.startswith("find notes "):
            return self._search(stripped[len("find notes "):])
        if command == "find notes":
            return "Say what to search for after “find notes”."
        if command == "undo last note":
            return self._delete_latest()
        if command == "restore last note":
            return self._restore_latest()
        if command == "export notes":
            return self._export()
        return None

    def save_text(self, visible_text: str) -> str:
        """Save arbitrary text as a QuickNote — the cross-surface entry.

        "Save Claude's last reply as a note", spoken into a live
        Brainstorm, lands here: the wearer thinks with the collaborators
        and keeps the parts worth keeping. Public because other surfaces
        route text in; the spoken "take a note" grammar shares it.
        """
        return self._save(visible_text)

    def _save(self, visible_text: str) -> str:
        if not visible_text:
            return "Say the text after “take a note”."
        identifier = secrets.token_urlsafe(18)
        self._notes.create(
            note_id=identifier,
            idempotency_key=f"quick-note:{identifier}",
            text=visible_text,
            source=NoteSource(kind="hud.quick-note", identifier=identifier),
            now_us=time.time_ns() // 1_000,
        )
        preview = visible_text if len(visible_text) <= 72 else visible_text[:69] + "…"
        return f"Saved QuickNote: {preview}"

    def _search(self, query: str) -> str:
        if not query:
            return "Say what to search for after “find notes”."
        matches = self._notes.search(query, limit=5)
        if not matches:
            return "No matching QuickNotes."
        snippets = [
            note.text.replace("\n", " ")[:72]
            for note in matches
        ]
        noun = "note" if len(snippets) == 1 else "notes"
        return f"{len(snippets)} {noun}: " + " · ".join(snippets)

    def _delete_latest(self) -> str:
        matches = self._notes.search("", limit=1)
        if not matches:
            return "There is no active QuickNote to remove."
        latest = matches[0]
        self._notes.delete(latest.note_id, now_us=time.time_ns() // 1_000)
        self._last_deleted_id = latest.note_id
        return "Removed the last QuickNote. Say restore last note to undo."

    def _restore_latest(self) -> str:
        note_id = self._last_deleted_id
        if note_id is None:
            return "There is no QuickNote deletion to restore in this session."
        try:
            self._notes.undo_delete(note_id, now_us=time.time_ns() // 1_000)
        except NoteNotFoundError:
            return "The deleted QuickNote is no longer available to restore."
        self._last_deleted_id = None
        return "Restored the last QuickNote."

    def _export(self) -> str:
        notes = self._notes.active()
        if not notes:
            return "There are no active QuickNotes to export."
        self._export_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._export_directory, 0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        destination = self._export_directory / f"quick-notes-{stamp}.md"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        fd = os.open(destination, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write("# JOAT QuickNotes\n\n")
                stream.write(
                    "Text-only export of explicit QuickNotes. "
                    "No audio or generated summaries are included.\n"
                )
                for note in notes:
                    created = datetime.fromtimestamp(
                        note.created_at_us / 1_000_000, tz=timezone.utc
                    ).isoformat()
                    stream.write(f"\n## {created}\n\n{note.text}\n")
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            try:
                destination.unlink()
            except OSError:
                pass
            raise
        return f"Exported {len(notes)} QuickNote{'s' if len(notes) != 1 else ''} to {destination}."
