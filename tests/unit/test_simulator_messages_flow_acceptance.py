"""Contract for the official-simulator Messages wearer flow."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


def _driver():
    path = Path(__file__).resolve().parents[2] / "scripts" / "simulator_messages_flow_acceptance.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("simulator_messages_flow", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_messages_flow_requires_causal_menu_selection_and_confirmation() -> None:
    driver = _driver()

    assert driver is not None, "Messages simulator acceptance driver is missing"
    receipts = [
        driver.ScreenReceipt("messages-sidebar", "test-hook-open", "0" * 64),
        driver.ScreenReceipt("messages-conversations", "click", "1" * 64),
        driver.ScreenReceipt("messages-conversation", "list-select", "2" * 64),
        driver.ScreenReceipt("messages-confirmation", None, "3" * 64),
        driver.ScreenReceipt("messages-result", "click", "4" * 64),
    ]

    driver.validate_flow(receipts)


def test_messages_flow_rejects_a_static_conversation_screenshot() -> None:
    driver = _driver()
    assert driver is not None

    with pytest.raises(driver.MessagesFlowError, match="messages-conversation"):
        driver.validate_flow([
            driver.ScreenReceipt("messages-sidebar", "test-hook-open", "0" * 64),
            driver.ScreenReceipt("messages-conversations", "click", "1" * 64),
            driver.ScreenReceipt("messages-conversation", None, "2" * 64),
            driver.ScreenReceipt("messages-confirmation", None, "3" * 64),
            driver.ScreenReceipt("messages-result", "click", "4" * 64),
        ])


def test_fixture_store_contains_distinct_conversations_and_history(tmp_path) -> None:  # type: ignore[no-untyped-def]
    driver = _driver()
    assert driver is not None
    store = driver.build_fixture_store(tmp_path / "chat.db")

    rows = store.recent_threads()

    assert [row.sender for row in rows] == ["Adaeze", "Dele"]
    assert [(entry.from_me, entry.text) for entry in store.thread_messages(rows[1])] == [
        (False, "Are you coming?"),
        (True, "On my way"),
        (False, "Great, see you soon."),
    ]
