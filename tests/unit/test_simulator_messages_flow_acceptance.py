"""Contract for the official-simulator Messages wearer flow."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
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
        driver.ScreenReceipt("messages-conversation", "down + click", "2" * 64),
        driver.ScreenReceipt("messages-confirmation", None, "3" * 64),
        driver.ScreenReceipt("messages-result", "click", "4" * 64),
        driver.ScreenReceipt("messages-returned", "click", "2" * 64),
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
            driver.ScreenReceipt("messages-returned", "click", "2" * 64),
        ])


@pytest.mark.parametrize(
    ("outcome", "result_input"),
    [
        ("accepted", "click"),
        ("cancelled", "down"),
        ("timed_out", None),
        ("uncertain", "click"),
        ("failed", "click"),
    ],
)
def test_messages_result_receipt_names_the_control_that_caused_that_outcome(
    outcome: str,
    result_input: str | None,
) -> None:
    driver = _driver()
    assert driver is not None

    receipts = [
        driver.ScreenReceipt("messages-sidebar", "test-hook-open", "0" * 64),
        driver.ScreenReceipt("messages-conversations", "click", "1" * 64),
        driver.ScreenReceipt("messages-conversation", "down + click", "2" * 64),
        driver.ScreenReceipt("messages-confirmation", None, "3" * 64),
        driver.ScreenReceipt("messages-result", result_input, "4" * 64),
        driver.ScreenReceipt("messages-returned", "click", "2" * 64),
    ]

    driver.validate_flow(receipts, outcome)


def test_messages_compose_flow_requires_new_message_selection_and_confirmation() -> None:
    driver = _driver()
    assert driver is not None
    receipts = [
        driver.ScreenReceipt("messages-sidebar", "test-hook-open", "0" * 64),
        driver.ScreenReceipt("messages-conversations", "click", "1" * 64),
        driver.ScreenReceipt("messages-compose", "click", "2" * 64),
        driver.ScreenReceipt("messages-confirmation", None, "3" * 64),
        driver.ScreenReceipt("messages-result", "click", "4" * 64),
        driver.ScreenReceipt("messages-returned", "click", "2" * 64),
    ]

    driver.validate_compose_flow(receipts)


def test_messages_history_flow_requires_real_scrolls_between_distinct_pages() -> None:
    driver = _driver()
    assert driver is not None
    receipts = [
        driver.ScreenReceipt("messages-sidebar", "test-hook-open", "0" * 64),
        driver.ScreenReceipt("messages-conversations", "click", "1" * 64),
        driver.ScreenReceipt("messages-latest", "down + down + click", "2" * 64),
        driver.ScreenReceipt("messages-older", "down × 11", "3" * 64),
        driver.ScreenReceipt("messages-latest-returned", "scroll-up", "2" * 64),
    ]

    driver.validate_history_flow(receipts)


def test_messages_recovery_flow_requires_a_visible_retry_then_a_real_list() -> None:
    driver = _driver()
    assert driver is not None

    receipts = [
        driver.ScreenReceipt("messages-sidebar", "test-hook-open", "0" * 64),
        driver.ScreenReceipt("messages-retry", "click", "1" * 64),
        driver.ScreenReceipt("messages-conversations", "click", "2" * 64),
    ]

    driver.validate_recovery_flow(receipts)


def test_recovery_store_fails_once_then_delegates_to_the_real_fixture(tmp_path: Path) -> None:
    driver = _driver()
    assert driver is not None
    recovering = driver.RecoverOnceMessagesStore(driver.build_fixture_store(tmp_path / "chat.db"))

    with pytest.raises(sqlite3.DatabaseError):
        recovering.recent_threads()

    assert recovering.retry_visible is True
    rows = recovering.recent_threads()

    assert recovering.read_attempts == 2
    assert recovering.retry_visible is False
    assert len(rows) == 2


@pytest.mark.parametrize(
    ("flow", "expected"),
    [
        (
            "reply",
            (
                "messages-sidebar",
                "messages-conversations",
                "messages-conversation",
                "messages-confirmation",
                "messages-result",
                "messages-returned",
            ),
        ),
        (
            "compose",
            (
                "messages-sidebar",
                "messages-conversations",
                "messages-compose",
                "messages-confirmation",
                "messages-result",
                "messages-returned",
            ),
        ),
        (
            "history",
            (
                "messages-sidebar",
                "messages-conversations",
                "messages-latest",
                "messages-older",
                "messages-latest-returned",
            ),
        ),
        (
            "recovery",
            (
                "messages-sidebar",
                "messages-retry",
                "messages-conversations",
            ),
        ),
    ],
)
def test_official_simulator_evidence_retains_every_required_rendered_frame(
    flow: str,
    expected: tuple[str, ...],
) -> None:
    driver = _driver()
    assert driver is not None

    assert driver.required_evidence_frames(flow) == expected


@pytest.mark.parametrize(
    ("flow", "controls", "selection_input"),
    [
        ("reply", ("click", "down", "click"), "down + click"),
        ("compose", ("click", "click"), "click"),
        ("history", ("click", "down", "down", "click"), "down + down + click"),
        ("recovery", ("click", "click"), "click"),
    ],
)
def test_official_simulator_drives_each_visible_messages_selection_control(
    flow: str,
    controls: tuple[str, ...],
    selection_input: str,
) -> None:
    driver = _driver()
    assert driver is not None

    assert driver.menu_selection_controls(flow) == controls
    assert driver.initial_selection_receipt_input(flow) == selection_input


def test_history_paging_reaches_the_real_boundary_before_requesting_older_rows() -> None:
    driver = _driver()
    assert driver is not None

    controls = driver.history_page_boundary_controls(8)

    assert controls == ("down",) * 11
    assert driver.history_page_receipt_input(8) == "down × 11"


def test_evidence_receipt_retains_the_causal_frame_order_without_message_content(
    tmp_path: Path,
) -> None:
    driver = _driver()
    assert driver is not None
    frames = [
        driver.ScreenReceipt(
            name,
            action,
            f"{(2 if name == 'messages-returned' else index):x}" * 64,
        )
        for index, (name, action) in enumerate(
            (
                ("messages-sidebar", "test-hook-open"),
                ("messages-conversations", "click"),
                ("messages-conversation", "down + click"),
                ("messages-confirmation", None),
                ("messages-result", "click"),
                ("messages-returned", "click"),
            )
        )
    ]

    driver._write_evidence_receipt(
        tmp_path,
        flow="reply",
        outcome="accepted",
        expectation=driver.reply_outcome_expectation("accepted"),
        sender_called=True,
        frames=frames,
    )

    receipt = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
    assert [frame["name"] for frame in receipt["frames"]] == [frame.name for frame in frames]
    assert receipt["messages_app_send"] is False
    assert receipt["physical_g2_proof"] is False
    assert "Simulator trace only" not in json.dumps(receipt)


def test_official_simulator_frame_has_a_reviewable_alpha_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _driver()
    assert driver is not None
    raw = tmp_path / "messages-sidebar.png"
    raw.write_bytes(b"rgba")

    def extract_alpha(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        Path(arguments[-1]).write_bytes(b"alpha")
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(driver.shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)
    monkeypatch.setattr(driver.subprocess, "run", extract_alpha)

    preview = driver._write_alpha_preview(raw)

    assert preview.name == "messages-sidebar.alpha.png"
    assert preview.read_bytes() == b"alpha"


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
