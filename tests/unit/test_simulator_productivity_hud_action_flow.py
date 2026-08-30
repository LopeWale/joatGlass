"""Contract for the isolated official-simulator productivity HUD flow."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from joat.core.confirmation import ConfirmationResult
from joat.core.protocol import ProductivityHudView
from joat.persistence.actions import ActionJournal
from joat.persistence.store import DurableStore
from joat.persistence.tasks import TaskOccurrenceStore
from joat.services.productivity_actions import ProductivityActionController


def _driver():
    path = Path(__file__).resolve().parents[2] / "scripts" / "simulator_productivity_hud_action_flow.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("productivity_hud_flow", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_productivity_flow_requires_real_confirmation_before_each_safe_write() -> None:
    driver = _driver()

    assert driver is not None, "productivity HUD action-flow driver is missing"
    receipts = [
        driver.ScreenReceipt("planning-month", "click", None, "month", "0" * 64),
        driver.ScreenReceipt("planning-week", "click", None, "week", "1" * 64),
        driver.ScreenReceipt("planning-day", "click", None, "day", "2" * 64),
        driver.ScreenReceipt("planning-item-detail", "click", None, "detail", "3" * 64),
        driver.ScreenReceipt("calendar-review", None, None, "review", "a" * 64),
        driver.ScreenReceipt("calendar-verifying", "click", True, "verifying", "b" * 64),
        driver.ScreenReceipt("calendar-saved", None, None, "saved", "c" * 64),
        driver.ScreenReceipt("calendar-return-planning", None, None, "day", "d" * 64),
        driver.ScreenReceipt("calendar-failed", None, None, "failed", "e" * 64),
        driver.ScreenReceipt("calendar-failed-return-planning", None, None, "day", "f" * 64),
        driver.ScreenReceipt("calendar-uncertain", None, None, "check", "7" * 64),
        driver.ScreenReceipt("calendar-uncertain-return-planning", None, None, "day", "8" * 64),
        driver.ScreenReceipt("calendar-cancelled", None, None, "cancelled", "9" * 64),
        driver.ScreenReceipt("calendar-cancelled-return-planning", None, None, "day", "a" * 64),
        driver.ScreenReceipt("calendar-not-saved", None, None, "not_saved", "b" * 64),
        driver.ScreenReceipt("calendar-not-saved-return-planning", None, None, "day", "c" * 64),
        driver.ScreenReceipt("reminders-review", None, None, "review", "0" * 64),
        driver.ScreenReceipt("reminders-verifying", "click", True, "verifying", "1" * 64),
        driver.ScreenReceipt("reminders-saved", None, None, "saved", "2" * 64),
        driver.ScreenReceipt("reminders-monitoring", None, None, "monitoring", "3" * 64),
        driver.ScreenReceipt("reminders-monitoring-return-planning", None, None, "day", "4" * 64),
        driver.ScreenReceipt("reminders-due", None, None, "due", "5" * 64),
        driver.ScreenReceipt("reminders-complete-review", None, None, "review", "4" * 64),
        driver.ScreenReceipt("reminders-completing", "click", True, "verifying", "5" * 64),
        driver.ScreenReceipt("reminders-completed", None, None, "completed", "6" * 64),
        driver.ScreenReceipt("reminders-completed-return-planning", None, None, "day", "7" * 64),
        driver.ScreenReceipt("reminders-failed", None, None, "failed", "8" * 64),
        driver.ScreenReceipt("reminders-failed-return-planning", None, None, "day", "9" * 64),
        driver.ScreenReceipt("reminders-uncertain", None, None, "check", "a" * 64),
        driver.ScreenReceipt("reminders-uncertain-return-planning", None, None, "day", "b" * 64),
        driver.ScreenReceipt("reminders-cancelled", None, None, "cancelled", "c" * 64),
        driver.ScreenReceipt("reminders-cancelled-return-planning", None, None, "day", "d" * 64),
        driver.ScreenReceipt("reminders-not-saved", None, None, "not_saved", "e" * 64),
        driver.ScreenReceipt("reminders-not-saved-return-planning", None, None, "day", "f" * 64),
    ]

    driver.validate_flow(receipts)


def test_planning_day_receipt_requires_both_native_kinds() -> None:
    driver = _driver()

    assert driver is not None
    driver.validate_planning_screen("day", SimpleNamespace(
        title="PLANNING · PLAN · TUE SEP 1",
        items=[
            "10:30 · EVENT · Design review",
            "19:00 · REMINDER · Buy filters",
        ],
        planning_grid=None,
    ))
    with pytest.raises(driver.ProductivityHudFlowError, match="mixed Event and Reminder"):
        driver.validate_planning_screen("day", SimpleNamespace(
            title="PLANNING · PLAN · TUE SEP 1",
            items=["10:30 · EVENT · Design review"],
            planning_grid=None,
        ))


async def test_screenshot_completion_never_reads_console_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _driver()

    assert driver is not None
    calls: list[str] = []

    def screenshot_only(url: str, **_kwargs: object) -> bytes:
        calls.append(url)
        assert "/api/console" not in url
        assert url.endswith("/api/screenshot/glasses")
        return b"stable-frame"

    def alpha_preview(raw_path: Path) -> Path:
        preview = raw_path.with_name(f"{raw_path.stem}.alpha.png")
        preview.write_bytes(b"preview")
        return preview

    monkeypatch.setattr(driver, "_http_bytes", screenshot_only)
    monkeypatch.setattr(driver, "_alpha_preview", alpha_preview)

    observation = driver.CaptureObservation()
    digest, _, _ = await driver._capture(
        "http://simulator.invalid",
        tmp_path,
        "frame",
        "0" * 64,
        observation=observation,
    )

    assert digest == hashlib.sha256(b"stable-frame").hexdigest()
    assert len(calls) >= 2
    assert all(url.endswith("/api/screenshot/glasses") for url in calls)
    assert observation.first_changed_at_ns is not None
    assert observation.settled_at_ns is not None
    assert observation.settled_at_ns >= observation.first_changed_at_ns
    assert not hasattr(driver, "_wait_for_visual_frame")
    assert not hasattr(driver, "visual_frame_ack_count")


def test_visual_latency_separates_first_changed_framebuffer_from_settled_framebuffer() -> None:
    driver = _driver()

    assert driver is not None
    sample = driver.visual_latency(
        screen="calendar-verifying",
        trigger="confirmed_tap",
        started_at_ns=1_000_000_000,
        observation=driver.CaptureObservation(
            first_changed_at_ns=1_180_000_000,
            settled_at_ns=1_820_000_000,
        ),
    )

    assert sample == driver.VisualLatency(
        screen="calendar-verifying",
        trigger="confirmed_tap",
        first_changed_framebuffer_ms=180,
        settled_framebuffer_ms=820,
    )
    assert driver.visual_latency(
        screen="calendar-verifying",
        trigger="confirmed_tap",
        started_at_ns=1_000_000_000,
        observation=driver.CaptureObservation(),
    ) is None
    with pytest.raises(driver.ProductivityHudFlowError, match="out of order"):
        driver.visual_latency(
            screen="calendar-verifying",
            trigger="confirmed_tap",
            started_at_ns=1_000_000_000,
            observation=driver.CaptureObservation(
                first_changed_at_ns=900_000_000,
                settled_at_ns=1_820_000_000,
            ),
        )
    assert driver.visual_latency_summary([]) == {"sample_count": 0}
    assert driver.visual_latency_summary([
        driver.VisualLatency("planning-month", "menu_click", 120, 720),
        driver.VisualLatency("calendar-verifying", "confirmed_tap", 240, 840),
    ]) == {
        "sample_count": 2,
        "max_first_changed_framebuffer_ms": 240,
        "max_settled_framebuffer_ms": 840,
    }


async def test_fixture_bridge_holds_a_controller_write_until_verifying_can_render() -> None:
    driver = _driver()

    assert driver is not None
    bridge = driver.FixtureEventKitBridge()
    bridge.hold_next_write()
    due = {
        "year": 2026,
        "month": 9,
        "day": 1,
        "hour": 18,
        "minute": 0,
        "second": 0,
        "time_zone_id": "America/Chicago",
        "repeated_time_choice": None,
    }
    pending = asyncio.create_task(bridge.call("reminders.create", {
        "calendar_identifier": "personal",
        "title": "Prepare report",
        "due": due,
        "resolved_due_unix_ms": 1_788_303_600_000,
        "alarm_offsets_seconds": [0],
    }))

    await bridge.wait_for_write()
    assert not pending.done(), "the fake bridge must not hide the verifying HUD frame"
    bridge.release_next_write()
    response = await pending

    assert response.commit_phase == "committed_readback"
    assert response.result["calendar_item_identifier"] == "fixture-reminder-prepare-report"
    assert response.result["alarms"] == [{
        "relative_offset_seconds": None,
        "absolute_unix_ms": 1_788_303_600_000,
    }]
    assert bridge.write_methods == ["reminders.create"]


async def test_driver_retains_verifying_frame_before_releasing_controller_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise controller, confirmation, driver, and visual-order boundary.

    The fixture holds ``reminders.create``. The driver must request the
    actual screenshot capture after the real controller published ``verifying``
    and before it releases that held native-shaped write. Console output cannot
    satisfy this boundary.
    """

    driver = _driver()
    assert driver is not None
    bridge = driver.FixtureEventKitBridge()
    published: list[ProductivityHudView] = []
    confirmation_requests: list[str] = []
    accepted = asyncio.Event()
    store = DurableStore(tmp_path / "controller-order.sqlite3")

    async def confirm(
        action_id: str,
        prompt: str,
        timeout_s: float,
        *,
        payload_digest: str,
    ) -> ConfirmationResult:
        del timeout_s
        confirmation_requests.append(action_id)
        await accepted.wait()
        return ConfirmationResult(
            action_id=action_id,
            accepted=True,
            challenge_id="simulator-order-check",
            session_id="simulator-session",
            payload_digest=bytes.fromhex(payload_digest),
            preview_digest=hashlib.sha256(prompt.encode()).digest(),
            pairing_identity_digest=b"p" * 32,
            authorization_digest=b"a" * 32,
            issued_at_us=1_000_000,
            expires_at_us=2_000_000,
            preview_completed_at_us=1_100_000,
            decided_at_us=1_200_000,
        )

    async def publish(view: ProductivityHudView) -> None:
        published.append(view)

    controller = ProductivityActionController(
        bridge,
        confirm,
        ActionJournal(store),
        TaskOccurrenceStore(store),
        publish_status=publish,
    )
    gateway = SimpleNamespace(sent=published, confirmation_requests=confirmation_requests)
    captures = iter([
        ("a" * 64, "review.rgba.png", "review.alpha.png"),
        ("b" * 64, "verifying.rgba.png", "verifying.alpha.png"),
        ("c" * 64, "saved.rgba.png", "saved.alpha.png"),
    ])
    async def input_action(_automation_base: str, action: str) -> None:
        assert action == "click"
        accepted.set()

    captured_names: list[str] = []

    async def capture(
        _automation_base: str,
        _output: Path,
        name: str,
        _prior_digest: str | None,
        *,
        observation,
    ):
        captured_names.append(name)
        if name.endswith("-verifying"):
            assert [view.phase for view in published] == ["verifying"]
            assert bridge.write_methods == ["reminders.create"]
        assert observation.first_changed_at_ns is None
        assert observation.settled_at_ns is None
        return next(captures)

    monkeypatch.setattr(driver, "_input", input_action)
    monkeypatch.setattr(driver, "_capture", capture)

    receipts: list[object] = []
    artifacts: dict[str, dict[str, str]] = {}
    latencies: list[object] = []
    try:
        _, before = await driver._drive_controller_write(
            controller,
            bridge,
            gateway,
            command="remind me to Prepare report on 2026-09-01 at 18:00 in America/Chicago",
            domain="reminders",
            title="Prepare report",
            terminal_phase="saved",
            terminal_result="Created Reminder: Prepare report.",
            review_name="review",
            verifying_name="verifying",
            terminal_name="saved",
            automation_base="http://simulator.invalid",
            output=tmp_path,
            prior_digest="0" * 64,
            receipts=receipts,
            artifacts=artifacts,
            latencies=latencies,
        )
    finally:
        store.close()

    assert before == 0
    assert captured_names == ["01-review", "02-verifying", "03-saved"]
    assert [view.phase for view in published] == ["verifying", "saved"]
    assert bridge.write_methods == ["reminders.create"]
    assert latencies == []
