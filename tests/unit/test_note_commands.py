"""Voice QuickNotes preserve the spoken text and keep destructive scope local."""

from __future__ import annotations

import os
from pathlib import Path

from joat.persistence.notes import NoteStore
from joat.persistence.store import DurableStore
from joat.services.note_commands import NoteCommandController


def rig(tmp_path: Path):
    store = DurableStore(tmp_path / "joat.sqlite3")
    controller = NoteCommandController(NoteStore(store), export_directory=tmp_path / "exports")
    return controller, store


async def test_take_note_saves_exact_visible_text_and_searches_locally(tmp_path: Path) -> None:
    controller, store = rig(tmp_path)

    assert await controller.handle_command("take a note Café planning at 4") == (
        "Saved QuickNote: Café planning at 4"
    )
    assert await controller.handle_command("find notes café") == (
        "1 note: Café planning at 4"
    )
    note = NoteStore(store).search("planning")[0]
    assert note.text == "Café planning at 4"
    assert note.source.kind == "hud.quick-note"
    store.close()


async def test_whisper_terminal_punctuation_does_not_kill_exact_commands(
    tmp_path: Path,
) -> None:
    """Whisper emits "Export notes." — on hardware, the trailing period made
    these commands do nothing at all. Note BODIES keep their punctuation."""
    controller, store = rig(tmp_path)

    assert await controller.handle_command("take a note Ship it today.") == (
        "Saved QuickNote: Ship it today."
    )
    assert (await controller.handle_command("Export notes.")) is not None
    assert (await controller.handle_command("Undo last note.")) is not None
    assert (await controller.handle_command("Restore last note!")) is not None
    saved = NoteStore(store).search("Ship")[0]
    assert saved.text == "Ship it today."  # body punctuation untouched
    store.close()


async def test_undo_hides_last_note_and_restore_is_explicit(tmp_path: Path) -> None:
    controller, store = rig(tmp_path)
    await controller.handle_command("note first")
    await controller.handle_command("note second")

    assert await controller.handle_command("undo last note") == "Removed the last QuickNote. Say restore last note to undo."
    assert [note.text for note in NoteStore(store).search("")] == ["first"]
    assert await controller.handle_command("restore last note") == "Restored the last QuickNote."
    assert [note.text for note in NoteStore(store).search("")] == ["second", "first"]
    store.close()


async def test_export_is_explicit_owner_only_and_contains_active_notes(tmp_path: Path) -> None:
    controller, store = rig(tmp_path)
    await controller.handle_command("note Private exact words")
    result = await controller.handle_command("export notes")

    assert result is not None and result.startswith("Exported 1 QuickNote to ")
    files = list((tmp_path / "exports").glob("quick-notes-*.md"))
    assert len(files) == 1
    assert "Private exact words" in files[0].read_text()
    assert os.stat(files[0]).st_mode & 0o777 == 0o600
    assert os.stat(files[0].parent).st_mode & 0o777 == 0o700
    store.close()


async def test_note_like_questions_do_not_get_captured(tmp_path: Path) -> None:
    controller, store = rig(tmp_path)

    assert await controller.handle_command("what did I note yesterday?") is None
    assert await controller.handle_command("take a note") == "Say the text after “take a note”."
    assert NoteStore(store).search("") == ()
    store.close()


async def test_export_never_silently_truncates_after_one_hundred_notes(
    tmp_path: Path,
) -> None:
    controller, store = rig(tmp_path)
    for index in range(101):
        await controller.handle_command(f"note entry {index:03d}")

    result = await controller.handle_command("export notes")

    assert result is not None and result.startswith("Exported 101 QuickNotes to ")
    exported = next((tmp_path / "exports").glob("quick-notes-*.md")).read_text()
    assert exported.count("\n## ") == 101
    assert "entry 000" in exported
    assert "entry 100" in exported
    store.close()
