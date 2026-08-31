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
