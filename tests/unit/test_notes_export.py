"""Apple Notes export stays injection-safe and honest about permissions."""

from __future__ import annotations

from pathlib import Path

from joat.services.notes_export import AppleNotesExporter, render_notes_body


def test_render_notes_body_escapes_html_and_keeps_blank_lines() -> None:
    body = render_notes_body(
        "Launch <sync>",
        ("Summary", "", "Ship & QA <b>now</b>"),
    )
    assert "<h1>Launch &lt;sync&gt;</h1>" in body
    assert "<div>Summary</div>" in body
    assert "<div><br></div>" in body
    assert "Ship &amp; QA &lt;b&gt;now&lt;/b&gt;" in body
    assert "<b>now</b>" not in body


def _stub(tmp_path: Path, script: str) -> str:
    stub = tmp_path / "osascript-stub"
    stub.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    stub.chmod(0o755)
    return str(stub)


async def test_export_success_names_the_folder_and_sync_path(tmp_path: Path) -> None:
    # The stub records argv so the test proves content rides arguments,
    # never spliced AppleScript source.
    recorded = tmp_path / "argv.txt"
    exporter = AppleNotesExporter(
        osascript=_stub(tmp_path, f'printf "%s\\n" "$@" > "{recorded}"; exit 0'),
    )
    receipt = await exporter.export(
        title="Launch sync",
        body_lines=("Summary", 'Ship "Friday"'),
    )
    assert receipt == (
        "Sent “Launch sync” to Apple Notes (JOAT folder). "
        "iCloud will sync it to your phone."
    )
    argv = recorded.read_text(encoding="utf-8").splitlines()
    assert argv[0] == "-"
    assert argv[1] == "Launch sync"
    assert "Ship &quot;Friday&quot;" in argv[2]
    assert argv[3] == "JOAT"
    assert argv[4] == ""  # no earlier note to update


async def test_write_returns_the_note_id_and_updates_by_it(tmp_path: Path) -> None:
    """FEAT-002: the id the script prints comes back, and sending it again
    asks the script to update that note rather than make a duplicate."""
    recorded = tmp_path / "argv.txt"
    exporter = AppleNotesExporter(
        osascript=_stub(
            tmp_path,
            f'printf "%s\\n" "$@" > "{recorded}"; echo "x-coredata://ABC/ICNote/p42"',
        ),
    )
    first = await exporter.write(title="Standup", body_lines=("x",), folder="Meetings")
    assert first.ok and first.note_id == "x-coredata://ABC/ICNote/p42"
    assert first.receipt.startswith("Sent “Standup” to Apple Notes (Meetings folder)")

    again = await exporter.write(
        title="Standup", body_lines=("y",), existing_id="x-coredata://ABC/ICNote/p42",
    )
    argv = recorded.read_text(encoding="utf-8").splitlines()
    assert argv[4] == "x-coredata://ABC/ICNote/p42"
    assert again.receipt.startswith("Updated “Standup” in Apple Notes")


async def test_a_failed_write_carries_no_id(tmp_path: Path) -> None:
    exporter = AppleNotesExporter(osascript=_stub(tmp_path, "exit 3"))
    result = await exporter.write(title="Broken", body_lines=("x",))
    assert result.ok is False and result.note_id is None


async def test_export_translates_automation_denial_into_guidance(tmp_path: Path) -> None:
    exporter = AppleNotesExporter(
        osascript=_stub(
            tmp_path,
            'echo "execution error: Not authorized to send Apple events (-1743)" 1>&2; exit 1',
        ),
    )
    receipt = await exporter.export(title="Blocked", body_lines=("x",))
    assert "Automation" in receipt
    assert "System Settings" in receipt


async def test_export_failure_is_generic_but_honest(tmp_path: Path) -> None:
    exporter = AppleNotesExporter(osascript=_stub(tmp_path, "exit 3"))
    receipt = await exporter.export(title="Broken", body_lines=("x",))
    assert receipt == "Notes export failed. Check the Notes app on this Mac and retry."
