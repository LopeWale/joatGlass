"""Every saved QuickNote lands in Apple Notes (FEAT-002).

The wearer asked for it in as many words — "saves it to my notes app" — so
this is the explicit cloud copy RULES.md #8 allows: after a recording is
saved and summarized, and after a text note is saved, the note is written
to Apple Notes on this Mac (folder "JOAT", default account) and iCloud
carries it to the phone. One switch turns it off: ``notes.json`` in JOAT's
Application Support folder, toggled by voice ("stop saving notes to Apple
Notes"). The glasses carry no settings row for it, as Even's QuickNote
list carries none.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from ..persistence.note_exports import NoteExportStore, NoteKind
from ..persistence.notes import NoteNotFoundError, NoteStore
from ..persistence.voice_notes import VoiceNote, VoiceNoteError, VoiceNoteStore
from .notes_export import DEFAULT_FOLDER, NotesWrite

log = logging.getLogger(__name__)

_SETTINGS_PATH = Path.home() / "Library/Application Support/JOAT/notes.json"
_TOGGLE_OFF = re.compile(
    r"^(?:stop|don'?t|do not) (?:saving|save|sending|send) (?:my )?(?:quick ?)?notes "
    r"to (?:apple )?notes$",
    re.IGNORECASE,
)
_TOGGLE_ON = re.compile(
    r"^(?:start )?(?:saving|save|send|sending) (?:my )?(?:quick ?)?notes "
    r"to (?:apple )?notes$",
    re.IGNORECASE,
)


class NotesWriter(Protocol):
    async def write(
        self,
        *,
        title: str,
        body_lines: tuple[str, ...],
        folder: str = DEFAULT_FOLDER,
        existing_id: str | None = None,
    ) -> NotesWrite: ...


class NotesSettings:
    """``{"saveToAppleNotes": true, "folder": "JOAT"}``, read on every use."""

    def __init__(self, path: Path = _SETTINGS_PATH) -> None:
        self._path = path

    def _read(self) -> dict[str, object]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.warning("notes settings unreadable; using defaults")
            return {}
        return raw if isinstance(raw, dict) else {}

    @property
    def enabled(self) -> bool:
        value = self._read().get("saveToAppleNotes", True)
        return value if isinstance(value, bool) else True

    @property
    def folder(self) -> str:
        value = self._read().get("folder", DEFAULT_FOLDER)
        if isinstance(value, str) and value.strip() and len(value) <= 60:
            return value.strip()
        return DEFAULT_FOLDER

    def set_enabled(self, enabled: bool) -> None:
        data = self._read()
        data["saveToAppleNotes"] = bool(enabled)
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp = tempfile.mkstemp(dir=self._path.parent, prefix=".notes-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, indent=2)
                stream.write("\n")
            os.chmod(temp, 0o600)
            os.replace(temp, self._path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise


@dataclass(frozen=True, slots=True)
class NoteDocument:
    title: str
    lines: tuple[str, ...]


def _when(at_us: int | None) -> str:
    if at_us is None:
        return ""
    return datetime.fromtimestamp(at_us / 1_000_000).astimezone().strftime("%a %d %b %Y, %H:%M")


def _duration(note: VoiceNote) -> str:
    if note.started_at_us is None or note.ended_at_us is None:
        return ""
    minutes = max(0, (note.ended_at_us - note.started_at_us) // 60_000_000)
    return f"{minutes} min" if minutes else "under a minute"


def voice_document(notes: VoiceNoteStore, capture_id: str) -> NoteDocument:
    """A recording as a readable document: what, when, gist, then every word."""
    note = notes.get(capture_id)
    title = note.title or "Untitled"
    insights = notes.insights(capture_id)
    lines: list[str] = []
    stamp = " · ".join(part for part in (_when(note.started_at_us), _duration(note)) if part)
    if stamp:
        lines.extend([stamp, ""])
    if insights is not None:
        lines.extend(["Overview", insights.summary])
        if insights.key_points:
            lines.extend(["", "Key points"])
            lines.extend(f"• {point}" for point in insights.key_points)
        if insights.todos:
            lines.extend(["", "Next steps"])
            lines.extend(f"☐ {todo}" for todo in insights.todos)
        lines.append("")
    lines.append(
        "Transcript" if note.state == "saved"
        else "Transcript (incomplete — the recording had gaps)"
    )
    body = [
        segment.text if segment.kind == "transcript" else "[recording gap]"
        for segment in notes.segments(capture_id)
        if segment.kind in ("transcript", "gap")
    ]
    lines.extend(body or ["(no transcript)"])
    return NoteDocument(title, tuple(lines))


def text_title(text: str, limit: int = 60) -> str:
    """A dictated note's title: its first line, cut at a word."""
    first = " ".join(text.strip().splitlines()[0].split()) if text.strip() else ""
    if len(first) <= limit:
        return first or "Note"
    cut = first[:limit].rsplit(" ", 1)[0]
    return (cut or first[:limit]) + "…"


class NotesSync:
    """Write QuickNotes to Apple Notes once, and update them in place after."""

    def __init__(
        self,
        writer: NotesWriter,
        exports: NoteExportStore,
        *,
        voice_notes: VoiceNoteStore | None = None,
        text_notes: NoteStore | None = None,
        settings: NotesSettings | None = None,
        clock_us: Callable[[], int] | None = None,
    ) -> None:
        self._writer = writer
        self._exports = exports
        self._voice_notes = voice_notes
        self._text_notes = text_notes
        self._settings = settings or NotesSettings()
        self._clock_us = clock_us or (lambda: time.time_ns() // 1_000)
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def settings(self) -> NotesSettings:
        return self._settings

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    def set_enabled(self, enabled: bool) -> None:
        """The QuickNotes page's switch."""
        self._settings.set_enabled(enabled)

    async def sync_voice(self, capture_id: str, *, force: bool = False) -> str | None:
        """Write one recording. None when the switch is off (and not forced)."""
        if self._voice_notes is None or (not force and not self._settings.enabled):
            return None
        try:
            if not force and self._voice_notes.get(capture_id).transcribed_segments == 0:
                # Nothing was said: no Apple Note full of "(no transcript)".
                return None
            document = voice_document(self._voice_notes, capture_id)
        except VoiceNoteError:
            return "That note no longer exists."
        return await self._write("voice", capture_id, document)

    async def sync_text(self, note_id: str, *, force: bool = False) -> str | None:
        if self._text_notes is None or (not force and not self._settings.enabled):
            return None
        try:
            note = self._text_notes.get(note_id)
        except NoteNotFoundError:
            return "That note no longer exists."
        stamp = _when(note.created_at_us)
        lines = ((stamp, "") if stamp else ()) + tuple(note.text.splitlines() or [note.text])
        return await self._write("text", note_id, NoteDocument(text_title(note.text), lines))

    def schedule_text(self, note_id: str) -> None:
        """Fire-and-forget after a dictated note is saved."""
        self._schedule(self.sync_text(note_id), f"text note {note_id[:8]}")

    def schedule_voice(self, capture_id: str) -> None:
        self._schedule(self.sync_voice(capture_id), f"recording {capture_id[:8]}")

    def _schedule(self, work: Coroutine[object, object, str | None], label: str) -> None:
        async def run() -> None:
            try:
                receipt = await work
                if receipt:
                    log.info("apple notes %s: %s", label, receipt)
            except Exception:
                log.exception("apple notes write failed for %s", label)

        try:
            task = asyncio.get_running_loop().create_task(run())
        except RuntimeError:
            work.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _write(self, kind: NoteKind, note_id: str, document: NoteDocument) -> str:
        existing = self._exports.external_id(kind, note_id)
        result = await self._writer.write(
            title=document.title,
            body_lines=document.lines,
            folder=self._settings.folder,
            existing_id=existing,
        )
        if result.ok and result.note_id:
            try:
                self._exports.record(kind, note_id, result.note_id, now_us=self._clock_us())
            except ValueError:
                log.warning("apple notes returned an unusable id; not remembered")
        return result.receipt

    def handle_toggle(self, text: str) -> str | None:
        """'stop saving notes to Apple Notes' / 'save notes to Apple Notes'."""
        normalized = " ".join(text.strip().split()).rstrip(".,!?").rstrip()
        if _TOGGLE_OFF.fullmatch(normalized):
            self._settings.set_enabled(False)
            return "New notes stay on this Mac; they are no longer saved to Apple Notes."
        if _TOGGLE_ON.fullmatch(normalized):
            self._settings.set_enabled(True)
            return "Every saved note now goes to Apple Notes too."
        return None


__all__ = [
    "NoteDocument",
    "NotesSettings",
    "NotesSync",
    "NotesWriter",
    "text_title",
    "voice_document",
]
