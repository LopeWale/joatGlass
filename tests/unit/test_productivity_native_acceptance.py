"""Safety and evidence contracts for the live productivity acceptance harness."""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import scripts.simulator_productivity_native_acceptance as acceptance
from PIL import Image

from scripts.simulator_productivity_native_acceptance import (
    AcceptanceSafetyError,
    NativeItemIdentity,
    DefaultsPreference,
    alarm_delivery_after,
    alpha_preview_has_content,
    alpha_preview_command,
    build_acceptance_titles,
    classify_alarm_delivery_failure,
    create_engine_stub_repo,
    evaluate_notification_preflight,
    has_due_surface_receipt,
    mark_interrupted,
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


def test_event_and_reminder_use_distinct_randomized_acceptance_identities() -> None:
    titles = build_acceptance_titles("a1b2c3d4")

    assert titles.event == "JOAT ACCEPTANCE a1b2c3d4 EVENT"
    assert titles.reminder == "JOAT ACCEPTANCE a1b2c3d4 REMINDER"
    assert titles.event != titles.reminder
    validate_acceptance_title(titles.event, "a1b2c3d4", kind="event")
    validate_acceptance_title(titles.reminder, "a1b2c3d4", kind="reminder")

    with pytest.raises(AcceptanceSafetyError, match="exact randomized"):
        validate_acceptance_title("Design review", "a1b2c3d4", kind="event")
    with pytest.raises(AcceptanceSafetyError, match="exact randomized"):
        validate_acceptance_title(
            "JOAT ACCEPTANCE a1b2c3d4 REMINDER",
            "a1b2c3d4",
            kind="event",
        )


def test_due_surface_receipt_is_fixed_and_carries_no_reminder_content() -> None:
    assert has_due_surface_receipt(
        b"[HudVisualSurface] receipt reminder_due bridge-ack\n"
    )
    assert not has_due_surface_receipt(
        b"[HudVisualSurface] receipt saved bridge-ack\n"
    )
    assert not has_due_surface_receipt(
        b"reminder_due private title bridge-ack\n"
    )


def test_interruption_receipt_never_promotes_a_partial_run_to_delivery() -> None:
    receipt: dict[str, object] = {"result": "running", "blockers": []}

    mark_interrupted(receipt)  # type: ignore[arg-type]

    assert receipt["result"] == "interrupted"
    assert receipt["blockers"] == [{
        "code": "acceptance_interrupted",
        "message": "acceptance interrupted before delivery conclusion",
    }]


def test_alarm_observation_uses_only_new_lines_and_returns_no_content(tmp_path: Path) -> None:
    title = "JOAT ACCEPTANCE a1b2c3d4 REMINDER"
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
    title = "JOAT ACCEPTANCE a1b2c3d4 REMINDER"
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
    title = "JOAT ACCEPTANCE a1b2c3d4 REMINDER"
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
    title = "JOAT ACCEPTANCE a1b2c3d4 REMINDER"
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


def test_completed_reminder_readback_requires_the_original_native_identity() -> None:
    identity = NativeItemIdentity(
        calendar_item_identifier="reminder-native-id",
        calendar_item_external_identifier="reminder-external-id",
        calendar_identifier="reminder-list-id",
    )
    completed = {
        "calendar_item_identifier": "reminder-native-id",
        "calendar_identifier": "reminder-list-id",
        "is_completed": True,
    }

    proof = acceptance._verify_completed_reminder_readback([completed], identity)

    assert proof == {
        "bounded_query_match_count": 1,
        "native_identity_matched": True,
        "is_completed": True,
    }
    with pytest.raises(acceptance.AcceptanceFailed, match="completion read-back differed"):
        acceptance._verify_completed_reminder_readback(
            [{**completed, "calendar_item_identifier": "other-reminder"}],
            identity,
        )


def test_calendar_projection_timeout_does_not_override_native_cleanup_proof() -> None:
    assert acceptance._blocking_cleanup_errors((
        "calendar_projection_before_TimeoutExpired",
        "calendar_projection_after_TimeoutExpired",
    )) == ()
    assert acceptance._blocking_cleanup_errors((
        "calendar_projection_before_TimeoutExpired",
        "reminder_delete_RuntimeError",
    )) == ("reminder_delete_RuntimeError",)


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


def test_engine_stub_is_private_pid_bound_bridge_proxy(
    tmp_path: Path,
) -> None:
    stub = create_engine_stub_repo(tmp_path, authorized_client_pid=4242)
    executable = stub.repo / ".venv/bin/python"

    assert executable.is_file()
    assert stat.S_IMODE(executable.stat().st_mode) == 0o700
    source = executable.read_text(encoding="utf-8")
    assert "JOAT_NATIVE_BRIDGE" in source
    assert "AUTHORIZED_CLIENT_PID = 4242" in source
    assert "LOCAL_PEERPID = 0x002" in source
    assert stub.bridge_path == stub.repo / "acceptance-native-bridge.sock"


@pytest.mark.asyncio
async def test_engine_stub_relays_only_the_authorized_harness_pid(tmp_path: Path) -> None:
    socket_context = tempfile.TemporaryDirectory(prefix="joat-proxy-test-", dir="/tmp")
    socket_root = Path(socket_context.name)
    upstream_path = socket_root / "native.sock"

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.read(32)
        writer.write(data)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(echo, path=upstream_path)
    stub = create_engine_stub_repo(
        tmp_path,
        authorized_client_pid=os.getpid(),
        proxy_root=socket_root,
    )
    env = dict(os.environ)
    env["JOAT_NATIVE_BRIDGE"] = str(upstream_path)
    process = subprocess.Popen(  # noqa: S603 - private generated executable
        [str(stub.repo / ".venv/bin/python"), "-u", "-m", "joat"],
        cwd=stub.repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        while not stub.bridge_path.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert stub.bridge_path.exists()
        assert stat.S_IMODE(stub.bridge_path.stat().st_mode) == 0o600

        reader, writer = await asyncio.open_unix_connection(stub.bridge_path)
        writer.write(b"authorized")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(10), timeout=2) == b"authorized"
        writer.close()
        await writer.wait_closed()

        probe = (
            "import socket,sys; s=socket.socket(socket.AF_UNIX); "
            "s.connect(sys.argv[1]); s.sendall(b'unauthorized'); "
            "data=s.recv(32); raise SystemExit(0 if data == b'' else 7)"
        )
        unauthorized = await asyncio.to_thread(
            subprocess.run,
            [str(Path(sys.executable)), "-c", probe, str(stub.bridge_path)],
            capture_output=True,
            timeout=5,
            check=False,
        )
        assert unauthorized.returncode == 0
    finally:
        process.terminate()
        await asyncio.to_thread(process.wait, 5)
        server.close()
        await server.wait_closed()
        socket_context.cleanup()


class _CleanupBridge:
    def __init__(self, *, fail_method: str | None = None) -> None:
        self.fail_method = fail_method
        self.calls: list[tuple[str, dict[str, object], str, str]] = []

    async def call(
        self,
        method: str,
        body: dict[str, object],
        *,
        attempt_id: str,
        payload_digest: str,
    ) -> SimpleNamespace:
        self.calls.append((method, body, attempt_id, payload_digest))
        if method == self.fail_method:
            raise RuntimeError("native cleanup failed")
        return SimpleNamespace(result={"removed": True})


def _cleanup_schedule() -> acceptance.AcceptanceSchedule:
    return plan_acceptance_schedule(
        datetime(2026, 8, 28, 10, 52, 45, tzinfo=ZoneInfo("America/Chicago")),
        minimum_lead_seconds=120,
    )


@pytest.mark.asyncio
async def test_cleanup_deletes_only_the_read_back_event_and_reminder_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    bridge = _CleanupBridge()
    titles = build_acceptance_titles("a1b2c3d4")
    event = NativeItemIdentity(
        calendar_item_identifier="event-internal",
        calendar_item_external_identifier="event-external",
        calendar_identifier="calendar-target",
    )
    reminder = NativeItemIdentity(
        calendar_item_identifier="reminder-internal",
        calendar_item_external_identifier="reminder-external",
        calendar_identifier="reminder-list",
    )
    projection_counts = iter((1, 0))
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
        "_count_calendar_projection",
        lambda _script, title: (
            calls.append(f"count-projection:{title}") or next(projection_counts)
        ),
    )

    removed = await acceptance.cleanup_test_items(
        bridge,  # type: ignore[arg-type]
        titles,
        "a1b2c3d4",
        schedule=_cleanup_schedule(),
        event_identity=event,
        reminder_identity=reminder,
    )

    assert calls == [
        "launch:Calendar",
        f"count-projection:{titles.reminder}",
        f"count-projection:{titles.reminder}",
        "quit:Calendar",
    ]
    assert [call[0] for call in bridge.calls] == [
        "acceptance.calendar.delete",
        "acceptance.reminder.delete",
    ]
    assert bridge.calls[0][1] == {
        "calendar_identifier": "calendar-target",
        "calendar_item_identifier": "event-internal",
        "expected_title": titles.event,
        "expected_start_unix_ms": 1_787_932_500_000,
        "expected_end_unix_ms": 1_787_932_560_000,
    }
    assert bridge.calls[1][1] == {
        "calendar_identifier": "reminder-list",
        "calendar_item_identifier": "reminder-internal",
        "expected_title": titles.reminder,
        "expected_due_unix_ms": 1_787_932_500_000,
    }
    assert all(call[2].startswith("acceptance-cleanup-") for call in bridge.calls)
    assert all(len(call[3]) == 64 for call in bridge.calls)
    assert removed.calendar_removed_count == 1
    assert removed.reminder_removed_count == 1
    assert removed.scheduled_reminder_projection_count_before == 1
    assert removed.scheduled_reminder_projection_count_after == 0
    assert removed.errors == ()


@pytest.mark.asyncio
async def test_cleanup_continues_to_projection_verification_if_reminder_delete_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _CleanupBridge(fail_method="acceptance.reminder.delete")
    monkeypatch.setattr(acceptance, "_ensure_native_app_running", lambda _name: False)
    monkeypatch.setattr(acceptance, "_count_calendar_projection", lambda *_args: 0)

    removed = await acceptance.cleanup_test_items(
        bridge,  # type: ignore[arg-type]
        build_acceptance_titles("a1b2c3d4"),
        "a1b2c3d4",
        schedule=_cleanup_schedule(),
        event_identity=NativeItemIdentity("event", "uid", "calendar"),
        reminder_identity=NativeItemIdentity("reminder", "external", "list"),
    )

    assert removed.calendar_removed_count == 1
    assert removed.reminder_removed_count == 0
    assert removed.errors == ("reminder_delete_RuntimeError",)
    assert not hasattr(acceptance, "_cleanup_calendar_item")
    assert not hasattr(acceptance, "_cleanup_reminder_item")


@pytest.mark.asyncio
async def test_cleanup_waits_for_scheduled_reminder_projection_to_converge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge = _CleanupBridge()
    counts = iter((1, 1, 0))
    sleeps: list[float] = []
    monkeypatch.setattr(acceptance, "_ensure_native_app_running", lambda _name: False)
    monkeypatch.setattr(
        acceptance,
        "_count_calendar_projection",
        lambda *_args: next(counts),
    )
    monkeypatch.setattr(
        "scripts.simulator_productivity_native_acceptance.time.sleep",
        sleeps.append,
    )

    removed = await acceptance.cleanup_test_items(
        bridge,  # type: ignore[arg-type]
        build_acceptance_titles("a1b2c3d4"),
        "a1b2c3d4",
        schedule=_cleanup_schedule(),
        event_identity=NativeItemIdentity("event", "uid", "calendar"),
        reminder_identity=NativeItemIdentity("reminder", "external", "list"),
    )

    assert removed.scheduled_reminder_projection_count_before == 1
    assert removed.scheduled_reminder_projection_count_after == 0
    assert sleeps == [0.25]


@pytest.mark.asyncio
async def test_cleanup_requires_native_identities_before_launching_apps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        acceptance,
        "_ensure_native_app_running",
        lambda _name: pytest.fail("unsafe cleanup must fail before launching apps"),
    )

    with pytest.raises(AcceptanceSafetyError, match="native event identity"):
        await acceptance.cleanup_test_items(
            _CleanupBridge(),  # type: ignore[arg-type]
            build_acceptance_titles("a1b2c3d4"),
            "a1b2c3d4",
            schedule=_cleanup_schedule(),
            event_identity=None,
            reminder_identity=None,
        )


@pytest.mark.asyncio
async def test_calendar_only_cleanup_never_launches_or_deletes_reminders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    bridge = _CleanupBridge()
    monkeypatch.setattr(
        acceptance,
        "_ensure_native_app_running",
        lambda name: calls.append(f"launch:{name}") or False,
    )
    monkeypatch.setattr(acceptance, "_count_calendar_projection", lambda *_args: 0)

    removed = await acceptance.cleanup_test_items(
        bridge,  # type: ignore[arg-type]
        build_acceptance_titles("a1b2c3d4"),
        "a1b2c3d4",
        schedule=_cleanup_schedule(),
        event_identity=NativeItemIdentity("event", "uid", "calendar-target"),
        reminder_identity=None,
        include_reminder=False,
    )

    assert calls == []
    assert [call[0] for call in bridge.calls] == ["acceptance.calendar.delete"]
    assert removed.calendar_removed_count == 1
    assert removed.reminder_removed_count == 0
    assert removed.errors == ()


def test_focus_preflight_is_read_only_and_requires_a_matching_operator_condition() -> None:
    focus_off = evaluate_notification_preflight(
        "focus-off",
        diagnostic_focus_active=False,
    )
    assert focus_off.expect_banner is True
    assert focus_off.expect_focus_suppression is False
    assert focus_off.diagnostic_consistent is True
    assert focus_off.mutated_settings is False

    suppressed = evaluate_notification_preflight(
        "focus-suppressed",
        diagnostic_focus_active=True,
    )
    assert suppressed.expect_banner is False
    assert suppressed.expect_focus_suppression is True
    assert suppressed.diagnostic_consistent is True
    assert suppressed.mutated_settings is False

    with pytest.raises(acceptance.AcceptanceBlocked, match="does not match"):
        evaluate_notification_preflight(
            "focus-off",
            diagnostic_focus_active=True,
        )
    with pytest.raises(acceptance.AcceptanceBlocked, match="must be explicitly confirmed"):
        evaluate_notification_preflight(None, diagnostic_focus_active=False)


def test_non_authoritative_legacy_focus_diagnostic_cannot_disprove_operator_suppression() -> None:
    suppressed = evaluate_notification_preflight(
        "focus-suppressed",
        diagnostic_focus_active=False,
    )

    assert suppressed.expect_focus_suppression is True
    assert suppressed.diagnostic_consistent is False
    assert suppressed.focus_diagnostic_authoritative is False


def test_reminders_allowed_preflight_records_operator_assertion_without_claiming_detection() -> None:
    preflight = evaluate_notification_preflight(
        "reminders-allowed",
        diagnostic_focus_active=True,
    )

    assert preflight.expect_banner is True
    assert preflight.focus_diagnostic_authoritative is False
    assert preflight.operator_assertion == "reminders-allowed"


def test_alarm_failure_classification_distinguishes_native_delivery_boundaries() -> None:
    due = datetime(2026, 8, 28, 16, 20, tzinfo=timezone.utc)
    fired = "RDTimeAlarmProducer expiredAlarm {fireDate: 2026-08-28 16:20:00 +0000}"
    submitted = "Added notification request: [ hasError: 0 hasCompletionHandler: 1 ]"

    not_fired = classify_alarm_delivery_failure("unrelated", due=due)
    assert not_fired.category == "alarm_not_fired"

    submission_failed = classify_alarm_delivery_failure(fired, due=due)
    assert submission_failed.category == "notification_submission_failed"

    focus_suppressed = classify_alarm_delivery_failure(
        "\n".join(
            (
                fired,
                submitted,
                "interruptionSuppression: delay delivery",
                "muted by DND suppression: delay",
            )
        ),
        due=due,
    )
    assert focus_suppressed.category == "focus_suppressed"
    assert focus_suppressed.alarm_fired is True
    assert focus_suppressed.notification_submitted is True
    assert focus_suppressed.focus_suppressed is True

    banner_unobserved = classify_alarm_delivery_failure(
        "\n".join((fired, submitted)),
        due=due,
    )
    assert banner_unobserved.category == "banner_accessibility_unobserved"
    assert banner_unobserved.alarm_fired is True
    assert banner_unobserved.notification_submitted is True
    assert banner_unobserved.focus_suppressed is False
    assert banner_unobserved.retained_raw_log is False


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
