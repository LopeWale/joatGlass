"""Contract for the isolated official-simulator productivity HUD flow."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


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
        driver.ScreenReceipt("calendar-uncertain", None, None, "check", "f" * 64),
        driver.ScreenReceipt("reminders-review", None, None, "review", "0" * 64),
        driver.ScreenReceipt("reminders-verifying", "click", True, "verifying", "1" * 64),
        driver.ScreenReceipt("reminders-saved", None, None, "saved", "2" * 64),
        driver.ScreenReceipt("reminders-due", None, None, "due", "3" * 64),
        driver.ScreenReceipt("reminders-complete-review", None, None, "review", "4" * 64),
        driver.ScreenReceipt("reminders-completing", "click", True, "verifying", "5" * 64),
        driver.ScreenReceipt("reminders-completed", None, None, "completed", "6" * 64),
        driver.ScreenReceipt("reminders-failed", None, None, "failed", "7" * 64),
        driver.ScreenReceipt("reminders-uncertain", None, None, "check", "8" * 64),
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
