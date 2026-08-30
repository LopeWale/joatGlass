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


@pytest.mark.parametrize(
    ("outcome", "gesture", "sender_called", "result_fragment"),
    [
        ("accepted", "click", True, "Submitted to Messages"),
        ("cancelled", "swipe", False, "Reply cancelled."),
        ("timed_out", None, False, "Reply cancelled."),
        ("uncertain", "click", True, "may have submitted"),
        ("failed", "click", True, "Could not send"),
    ],
)
def test_messages_reply_outcome_contract_is_explicit_and_truthful(
    outcome: str,
    gesture: str | None,
    sender_called: bool,
    result_fragment: str,
) -> None:
    driver = _driver()
    assert driver is not None

    expectation = driver.reply_outcome_expectation(outcome)

    assert expectation.simulator_gesture == gesture
    assert expectation.sender_called is sender_called
    assert result_fragment in expectation.result_text


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("accepted", "Submitted to Messages for Adaeze; delivery unverified."),
        (
            "uncertain",
            "Messages may have submitted the reply to Adaeze. "
            "Check Messages on your Mac before trying again.",
        ),
        ("failed", "Could not send to Adaeze. Check Messages on your Mac."),
    ],
)
def test_messages_reply_outcome_uses_the_actual_selected_recipient(
    outcome: str,
    expected: str,
) -> None:
    driver = _driver()
    assert driver is not None

    assert driver.reply_outcome_expectation(outcome).result_for("Adaeze") == expected


@pytest.mark.parametrize(
    ("outcome", "simulator_control"),
    [
        ("accepted", "click"),
        ("cancelled", "down"),
        ("timed_out", None),
        ("uncertain", "click"),
        ("failed", "click"),
    ],
)
def test_messages_reply_outcomes_map_labels_to_official_simulator_controls(
    outcome: str,
    simulator_control: str | None,
) -> None:
    driver = _driver()
    assert driver is not None

    expectation = driver.reply_outcome_expectation(outcome)

    assert expectation.simulator_control == simulator_control


@pytest.mark.parametrize(
    ("outcome", "wearer_label"),
    [
        ("accepted", "TAP · Confirm"),
        ("cancelled", "SWIPE · Cancel"),
        ("timed_out", None),
        ("uncertain", "TAP · Confirm"),
        ("failed", "TAP · Confirm"),
    ],
)
def test_messages_reply_outcomes_name_the_visible_g2_action(
    outcome: str,
    wearer_label: str | None,
) -> None:
    driver = _driver()
    assert driver is not None

    expectation = driver.reply_outcome_expectation(outcome)

    assert expectation.wearer_label == wearer_label


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
