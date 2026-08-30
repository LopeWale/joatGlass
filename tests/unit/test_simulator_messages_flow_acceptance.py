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


def test_messages_compose_flow_requires_new_message_selection_and_confirmation() -> None:
    driver = _driver()
    assert driver is not None
    receipts = [
        driver.ScreenReceipt("messages-sidebar", "test-hook-open", "0" * 64),
        driver.ScreenReceipt("messages-conversations", "click", "1" * 64),
        driver.ScreenReceipt("messages-compose", "list-select", "2" * 64),
        driver.ScreenReceipt("messages-confirmation", None, "3" * 64),
        driver.ScreenReceipt("messages-result", "click", "4" * 64),
    ]

    driver.validate_compose_flow(receipts)


def test_messages_history_flow_requires_real_scrolls_between_distinct_pages() -> None:
    driver = _driver()
    assert driver is not None
    receipts = [
        driver.ScreenReceipt("messages-sidebar", "test-hook-open", "0" * 64),
        driver.ScreenReceipt("messages-conversations", "click", "1" * 64),
        driver.ScreenReceipt("messages-latest", "list-select", "2" * 64),
        driver.ScreenReceipt("messages-older", "scroll-down", "3" * 64),
        driver.ScreenReceipt("messages-latest-returned", "scroll-up", "4" * 64),
    ]

    driver.validate_history_flow(receipts)


def test_fixture_store_contains_distinct_conversations_and_history(tmp_path) -> None:  # type: ignore[no-untyped-def]
    driver = _driver()
    assert driver is not None
    store = driver.build_fixture_store(tmp_path / "chat.db")

    rows = store.recent_threads()

    assert [row.sender for row in rows] == ["Adaeze", "Dele"]
    page = store.thread_message_page(rows[1], limit=8)
    assert page.has_older is True
    assert len(page.entries) == 8
    assert page.entries[0].attachment_count == 1
    assert page.entries[0].unread is True
    assert page.entries[1].delivery == "read"
