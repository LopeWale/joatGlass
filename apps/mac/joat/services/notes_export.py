"""Apple Notes export for retained QuickNotes.

The Even plugin has no phone-side Notes access (SDK 0.0.16 exposes mic,
location, album, camera, storage, and the whitelisted socket — nothing else),
so "send to my notes" lands the note in Apple Notes on the MAC via
AppleScript, and iCloud carries it to the phone's Notes app. The wearer's
content rides argv into ``on run argv`` — never spliced into script source —
and the script touches only a dedicated folder in the default account.

FEAT-002: the script returns the note's id, and given that id back it
updates the same note instead of making another, so an automatic save
followed by a re-send never leaves the wearer a duplicate.
"""

from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

_TIMEOUT_S = 30.0
DEFAULT_FOLDER = "JOAT"

# argv 1 = note title, argv 2 = HTML body, argv 3 = folder, argv 4 = the id
# of the note this one was written to before ("" for none). Notes bodies are
# HTML; plain newlines are dropped by the app, so the exporter pre-renders
# div lines. A previous note that was deleted (or sits in Recently Deleted)
# is replaced by a new one rather than resurrected.
_APPLESCRIPT = """
on run argv
    set noteTitle to item 1 of argv
    set noteBody to item 2 of argv
    set folderName to item 3 of argv
    set existingId to item 4 of argv
    tell application "Notes"
        if existingId is not "" then
            try
                set theNote to note id existingId
                if name of container of theNote is not "Recently Deleted" then
                    set body of theNote to noteBody
                    return id of theNote
                end if
            end try
        end if
        tell default account
            if not (exists folder folderName) then
                make new folder with properties {name:folderName}
            end if
            tell folder folderName
                set theNote to make new note with properties {name:noteTitle, body:noteBody}
            end tell
        end tell
        return id of theNote
    end tell
end run
"""


def render_notes_body(title: str, body_lines: tuple[str, ...]) -> str:
    """Escape wearer content and render one HTML paragraph per line."""
    rendered = [f"<div><h1>{html.escape(title)}</h1></div>"]
    for line in body_lines:
        text = line.rstrip()
        if text:
            rendered.append(f"<div>{html.escape(text)}</div>")
        else:
            rendered.append("<div><br></div>")
    return "".join(rendered)


@dataclass(frozen=True, slots=True)
class NotesWrite:
    """The outcome of one write: the wearer-visible receipt, and on success
    the Apple Notes id to send back next time."""

    receipt: str
    ok: bool
    note_id: str | None = None


class AppleNotesExporter:
    """Create (or update) one Apple Note per export."""

    def __init__(
        self,
        *,
        osascript: str = "/usr/bin/osascript",
        timeout_s: float = _TIMEOUT_S,
    ) -> None:
        if not 1.0 <= timeout_s <= 600.0:
            raise ValueError("timeout must be between 1 and 600 seconds")
        self._osascript = osascript
        self._timeout_s = timeout_s

    async def export(self, *, title: str, body_lines: tuple[str, ...]) -> str:
        """Create a new note; the receipt only (the original contract)."""
        return (await self.write(title=title, body_lines=body_lines)).receipt

    async def write(
        self,
        *,
        title: str,
        body_lines: tuple[str, ...],
        folder: str = DEFAULT_FOLDER,
        existing_id: str | None = None,
    ) -> NotesWrite:
        cleaned_title = " ".join(title.split())[:120] or "Untitled"
        cleaned_folder = " ".join(folder.split())[:60] or DEFAULT_FOLDER
        body = render_notes_body(cleaned_title, body_lines)
        try:
            proc = await asyncio.create_subprocess_exec(
                self._osascript, "-", cleaned_title, body, cleaned_folder,
                existing_id or "",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            log.exception("osascript could not be started")
            return NotesWrite(
                "Notes export failed: AppleScript is unavailable on this Mac.", False,
            )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(_APPLESCRIPT.encode("utf-8")),
                timeout=self._timeout_s,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return NotesWrite(
                "Notes export timed out. If macOS is asking for permission, "
                "allow JOAT to control Notes and retry.",
                False,
            )
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            log.warning("Notes export failed rc=%s detail=%r", proc.returncode, detail[:200])
            if "-1743" in detail or "not allowed" in detail.casefold():
                return NotesWrite(
                    "Notes export was blocked. Allow JOAT to control Notes in "
                    "System Settings → Privacy & Security → Automation, then retry.",
                    False,
                )
            return NotesWrite(
                "Notes export failed. Check the Notes app on this Mac and retry.", False,
            )
        returned = stdout.decode("utf-8", errors="replace").strip()
        note_id = returned if returned and len(returned) <= 512 and "\n" not in returned else None
        verb = "Updated" if existing_id and note_id == existing_id else "Sent"
        return NotesWrite(
            f"{verb} “{cleaned_title}” {'in' if verb == 'Updated' else 'to'} Apple Notes "
            f"({cleaned_folder} folder). iCloud will sync it to your phone.",
            True,
            note_id,
        )


__all__ = ["DEFAULT_FOLDER", "AppleNotesExporter", "NotesWrite", "render_notes_body"]
