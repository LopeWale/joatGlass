"""Safety and evidence contracts for the live productivity acceptance harness."""

from __future__ import annotations

import json
import stat
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import scripts.simulator_productivity_native_acceptance as acceptance
from PIL import Image

from scripts.simulator_productivity_native_acceptance import (
    AcceptanceSafetyError,
    DefaultsPreference,
    alarm_delivery_after,
    alpha_preview_has_content,
    alpha_preview_command,
    create_engine_stub_repo,
    plan_acceptance_schedule,
    redact_native_reference,
    repo_preference_restore_command,
    repo_preference_from_defaults_result,
    resolve_simulator_ws_url,
    secure_notification_feed,
    validate_acceptance_title,
    write_private_receipt,
)


def test_schedule_keeps_a_full_two_minute_alarm_observation_window() -> None:
    now = datetime(2026, 8, 28, 10, 52, 45, tzinfo=ZoneInfo("America/Chicago"))

    schedule = plan_acceptance_schedule(now, minimum_lead_seconds=120)

    assert schedule.event_start.isoformat() == "2026-08-28T10:55:00-05:00"
    assert schedule.event_end.isoformat() == "2026-08-28T10:56:00-05:00"
    assert schedule.reminder_due.isoformat() == "2026-08-28T10:55:00-05:00"
    assert (schedule.reminder_due - now).total_seconds() == 135


def test_cleanup_title_must_be_an_exact_randomized_acceptance_identity() -> None:
    validate_acceptance_title("JOAT ACCEPTANCE a1b2c3d4", "a1b2c3d4")

    with pytest.raises(AcceptanceSafetyError, match="exact randomized"):
        validate_acceptance_title("Design review", "a1b2c3d4")
    with pytest.raises(AcceptanceSafetyError, match="exact randomized"):
        validate_acceptance_title("JOAT ACCEPTANCE a1b2c3d4 extra", "a1b2c3d4")


def test_alarm_observation_uses_only_new_lines_and_returns_no_content(tmp_path: Path) -> None:
    title = "JOAT ACCEPTANCE a1b2c3d4"
    feed = tmp_path / "notifications.jsonl"
    feed.write_text(
        json.dumps({"at": 10, "app": "Reminders", "title": title, "body": "old"})
        + "\n",
        encoding="utf-8",
    )
    offset = feed.stat().st_size
    with feed.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"at": 20, "app": "Mail", "title": title, "body": "wrong app"}) + "\n")
        stream.write(json.dumps({"at": 30, "app": "Reminders", "title": title, "body": "due now"}) + "\n")

    evidence = alarm_delivery_after(
        feed,
        offset=offset,
        exact_title=title,
        not_before_unix_ms=25_000,
        now_unix_ms=35_000,
    )

    assert evidence is not None
    assert evidence.observed_at_unix_ms == 30_000
    serialized = json.dumps(asdict(evidence), sort_keys=True)
    assert title not in serialized
    assert "due now" not in serialized
    assert "Reminders" not in serialized
    assert len(evidence.content_digest) == 64
    assert len(evidence.source_digest) == 64


def test_alarm_probe_rejects_matching_reminders_banner_before_due(tmp_path: Path) -> None:
    title = "JOAT ACCEPTANCE a1b2c3d4"
    feed = tmp_path / "notifications.jsonl"
    feed.write_text(
        json.dumps({"at": 20, "app": "Reminders", "title": title, "body": "created"})
        + "\n",
        encoding="utf-8",
    )

    assert alarm_delivery_after(
        feed,
        offset=0,
        exact_title=title,
        not_before_unix_ms=25_000,
        now_unix_ms=35_000,
    ) is None


def test_alarm_probe_rejects_a_future_timestamp_and_feed_rotation(tmp_path: Path) -> None:
    title = "JOAT ACCEPTANCE a1b2c3d4"
    feed = tmp_path / "notifications.jsonl"
    feed.write_text(
        json.dumps({"at": 50, "app": "Reminders", "title": title, "body": "future"})
        + "\n",
        encoding="utf-8",
    )

    assert alarm_delivery_after(
        feed,
        offset=0,
        exact_title=title,
        not_before_unix_ms=25_000,
        now_unix_ms=35_000,
    ) is None
    with pytest.raises(acceptance.AcceptanceBlocked, match="rotated or truncated"):
        alarm_delivery_after(
            feed,
            offset=feed.stat().st_size + 1,
            exact_title=title,
            not_before_unix_ms=25_000,
            now_unix_ms=35_000,
        )


def test_alarm_probe_rejects_inode_replacement_even_when_size_does_not_shrink(
    tmp_path: Path,
) -> None:
    title = "JOAT ACCEPTANCE a1b2c3d4"
    feed = tmp_path / "notifications.jsonl"
    feed.write_text("{}\n", encoding="utf-8")
    original = feed.stat()
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text("{}\n", encoding="utf-8")
    replacement.replace(feed)

    with pytest.raises(acceptance.AcceptanceBlocked, match="rotated or replaced"):
        alarm_delivery_after(
            feed,
            offset=0,
            exact_title=title,
            not_before_unix_ms=25_000,
            now_unix_ms=35_000,
            expected_feed_identity=(original.st_dev, original.st_ino),
        )


def test_native_identifiers_are_hashed_before_the_retained_receipt() -> None:
    reference = redact_native_reference(
        action_kind="calendar.create",
        action_id="action-secret-id",
        native_identifier="eventkit-secret-id",
        evidence_digest="a" * 64,
    )

    serialized = json.dumps(reference, sort_keys=True)
    assert "action-secret-id" not in serialized
    assert "eventkit-secret-id" not in serialized
    assert reference["action_ref"] == "b864351ae54d"
    assert len(reference["native_ref_digest"]) == 64
    assert reference["evidence_digest"] == "a" * 64


def test_retained_receipt_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"

    write_private_receipt(path, {"schema": "joat.native-productivity-acceptance.v1"})

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8"))["schema"].endswith(".v1")


def test_alarm_probe_hardens_an_existing_banner_feed_before_reading(tmp_path: Path) -> None:
    feed = tmp_path / "notifications.jsonl"
    feed.write_text("", encoding="utf-8")
    feed.chmod(0o644)

    secure_notification_feed(feed)

    assert stat.S_IMODE(feed.stat().st_mode) == 0o600


def test_simulator_url_must_come_from_the_packaged_local_allowlist(tmp_path: Path) -> None:
    manifest = tmp_path / "app.json"
    manifest.write_text(
        json.dumps(
            {
                "permissions": [
                    {
                        "name": "network",
                        "whitelist": [
                            "ws://example.tailnet.ts.net:8787",
                            "ws://wearers-mac.local:8787",
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert resolve_simulator_ws_url(manifest, gateway_port=8787) == (
        "ws://wearers-mac.local:8787/glasses"
    )

    with pytest.raises(AcceptanceSafetyError, match="allowlisted local WebSocket"):
        resolve_simulator_ws_url(manifest, gateway_port=9999)


def test_repo_restore_deletes_a_previously_absent_preference() -> None:
    snapshot = DefaultsPreference(present=False, value=None)

    assert repo_preference_restore_command("com.joat.shell", snapshot) == [
        "/usr/bin/defaults",
        "delete",
        "com.joat.shell",
        "RepoPath",
    ]


def test_repo_restore_preserves_an_existing_path_preference() -> None:
    snapshot = DefaultsPreference(present=True, value="/Users/wearer/joatGlass")

    assert repo_preference_restore_command("com.joat.shell", snapshot) == [
        "/usr/bin/defaults",
        "write",
        "com.joat.shell",
        "RepoPath",
        "/Users/wearer/joatGlass",
    ]


def test_repo_preference_read_distinguishes_missing_key_from_defaults_failure() -> None:
    assert repo_preference_from_defaults_result(
        returncode=1,
        stdout="",
        stderr="Error: Could not find key 'RepoPath' in domain 'com.joat.shell'.",
    ) == DefaultsPreference(present=False, value=None)

    with pytest.raises(AcceptanceSafetyError, match="could not read RepoPath"):
        repo_preference_from_defaults_result(
            returncode=1,
            stdout="",
            stderr="defaults: permission denied",
        )


def test_engine_stub_is_private_executable_and_cannot_open_the_native_bridge(
    tmp_path: Path,
) -> None:
    repo = create_engine_stub_repo(tmp_path)
    executable = repo / ".venv/bin/python"

    assert executable.is_file()
    assert stat.S_IMODE(executable.stat().st_mode) == 0o700
    source = executable.read_text(encoding="utf-8")
    assert "signal.pause()" in source
    assert "JOAT_NATIVE_BRIDGE" not in source


def test_cleanup_launches_native_apps_before_exact_title_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        acceptance,
        "_ensure_native_app_running",
        lambda name: calls.append(f"launch:{name}") or name == "Calendar",
    )
    monkeypatch.setattr(
        acceptance,
        "_quit_native_app",
        lambda name: calls.append(f"quit:{name}"),
    )
    monkeypatch.setattr(
        acceptance,
        "_cleanup_calendar_items",
        lambda _script, _title, _calendar_id: calls.append("delete-calendar") or (1, 1),
    )
    monkeypatch.setattr(
        acceptance,
        "_osascript",
        lambda _script, _title: calls.append("delete-reminder") or 1,
    )

    removed = acceptance.cleanup_test_items(
        "JOAT ACCEPTANCE a1b2c3d4",
        "a1b2c3d4",
    )

    assert calls == [
        "launch:Calendar",
        "launch:Reminders",
        "delete-calendar",
        "delete-reminder",
        "quit:Calendar",
    ]
    assert removed.calendar_removed_count == 2
    assert removed.calendar_target_removed_count == 1
    assert removed.calendar_other_removed_count == 1
    assert removed.reminder_removed_count == 1
    assert removed.errors == ()


def test_cleanup_restores_calendar_if_reminders_launch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def ensure(name: str) -> bool:
        calls.append(f"launch:{name}")
        if name == "Reminders":
            raise acceptance.AcceptanceBlocked("unavailable", "Reminders unavailable")
        return True

    monkeypatch.setattr(acceptance, "_ensure_native_app_running", ensure)
    monkeypatch.setattr(
        acceptance,
        "_quit_native_app",
        lambda name: calls.append(f"quit:{name}"),
    )

    with pytest.raises(acceptance.AcceptanceBlocked, match="Reminders unavailable"):
        acceptance.cleanup_test_items("JOAT ACCEPTANCE a1b2c3d4", "a1b2c3d4")

    assert calls == ["launch:Calendar", "launch:Reminders", "quit:Calendar"]


def test_cleanup_preserves_calendar_result_when_reminder_delete_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(acceptance, "_ensure_native_app_running", lambda _name: False)
    monkeypatch.setattr(
        acceptance,
        "_cleanup_calendar_items",
        lambda _script, _title, _calendar_id: (1, 1),
    )

    def delete_reminder(_script: str, _title: str) -> int:
        raise RuntimeError("reminders unavailable")

    monkeypatch.setattr(acceptance, "_osascript", delete_reminder)

    removed = acceptance.cleanup_test_items(
        "JOAT ACCEPTANCE a1b2c3d4",
        "a1b2c3d4",
    )

    assert removed.calendar_removed_count == 2
    assert removed.calendar_target_removed_count == 1
    assert removed.calendar_other_removed_count == 1
    assert removed.reminder_removed_count == 0
    assert removed.errors == ("reminder_delete_RuntimeError",)


def test_calendar_only_cleanup_never_launches_or_deletes_reminders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        acceptance,
        "_ensure_native_app_running",
        lambda name: calls.append(f"launch:{name}") or False,
    )
    monkeypatch.setattr(
        acceptance,
        "_cleanup_calendar_items",
        lambda _script, _title, calendar_id: (
            calls.append(f"calendar:{calendar_id}") or (1, 0)
        ),
    )
    monkeypatch.setattr(
        acceptance,
        "_osascript",
        lambda _script, _title: pytest.fail("Reminders cleanup must not run"),
    )

    removed = acceptance.cleanup_test_items(
        "JOAT ACCEPTANCE a1b2c3d4",
        "a1b2c3d4",
        event_calendar_identifier="calendar-target",
        include_reminder=False,
    )

    assert calls == ["launch:Calendar", "calendar:calendar-target"]
    assert removed.calendar_target_removed_count == 1
    assert removed.calendar_other_removed_count == 0
    assert removed.reminder_removed_count == 0
    assert removed.errors == ()


def test_alpha_preview_uses_the_simulator_framebuffers_authoritative_alpha_channel(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "confirmation.png"
    preview = tmp_path / "confirmation.alpha.png"

    assert alpha_preview_command(Path("/opt/homebrew/bin/ffmpeg"), raw, preview) == [
        "/opt/homebrew/bin/ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(raw),
        "-vf",
        "alphaextract",
        str(preview),
    ]


def test_alpha_preview_must_contain_visible_hud_pixels(tmp_path: Path) -> None:
    blank = tmp_path / "blank.png"
    visible = tmp_path / "visible.png"
    Image.new("L", (2, 1), color=0).save(blank)
    image = Image.new("L", (2, 1), color=0)
    image.putpixel((1, 0), 255)
    image.save(visible)

    assert alpha_preview_has_content(blank) is False
    assert alpha_preview_has_content(visible) is True
