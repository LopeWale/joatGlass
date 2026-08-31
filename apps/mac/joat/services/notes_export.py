"""Apple Notes export for retained QuickNotes.

The Even plugin has no phone-side Notes access (SDK 0.0.14 exposes mic,
location, album, camera, storage, and the whitelisted socket — nothing else),
so "send to my notes" lands the note in Apple Notes on the MAC via
AppleScript, and iCloud carries it to the phone's Notes app. The wearer's
content rides argv into ``on run argv`` — never spliced into script source —
and the script touches only a dedicated "JOAT" folder in the default account.
"""

from __future__ import annotations

import asyncio
import html
import logging

log = logging.getLogger(__name__)

_TIMEOUT_S = 30.0

# argv 1 = note title, argv 2 = HTML body. Notes bodies are HTML; plain
# newlines are dropped by the app, so the exporter pre-renders div lines.
_APPLESCRIPT = """
on run argv
    set noteTitle to item 1 of argv
    set noteBody to item 2 of argv
    tell application "Notes"
        tell default account
            if not (exists folder "JOAT") then
                make new folder with properties {name:"JOAT"}
            end if
            tell folder "JOAT"
                make new note with properties {name:noteTitle, body:noteBody}
            end tell
        end tell
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


class AppleNotesExporter:
    """Create one Apple Note per export; returns a wearer-visible receipt."""

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
        cleaned_title = " ".join(title.split())[:120] or "Untitled"
        body = render_notes_body(cleaned_title, body_lines)
        try:
            proc = await asyncio.create_subprocess_exec(
                self._osascript, "-", cleaned_title, body,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            log.exception("osascript could not be started")
            return "Notes export failed: AppleScript is unavailable on this Mac."
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(_APPLESCRIPT.encode("utf-8")),
                timeout=self._timeout_s,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return (
                "Notes export timed out. If macOS is asking for permission, "
                "allow JOAT to control Notes and retry."
            )
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            log.warning("Notes export failed rc=%s detail=%r", proc.returncode, detail[:200])
            if "-1743" in detail or "not allowed" in detail.casefold():
                return (
                    "Notes export was blocked. Allow JOAT to control Notes in "
                    "System Settings → Privacy & Security → Automation, then retry."
                )
            return "Notes export failed. Check the Notes app on this Mac and retry."
        return (
            f"Sent “{cleaned_title}” to Apple Notes (JOAT folder). "
            "iCloud will sync it to your phone."
        )


__all__ = ["AppleNotesExporter", "render_notes_body"]
