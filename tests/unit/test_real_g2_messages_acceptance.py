"""Contract for the content-free physical-G2 Messages receipt."""

from __future__ import annotations

from scripts.real_g2_messages_acceptance import summarize_receipt
from joat.services.g2_diagnostics import TRACE_SCHEMA


def _trace(*events: dict[str, object]) -> dict[str, object]:
    return {
        "schema": TRACE_SCHEMA,
        "events": [
            {"sequence": index, "recordedAtMs": index, **event}
            for index, event in enumerate(events, start=1)
        ],
    }


def _no_send_reply_trace(*, outcome: str = "declined", physical: bool = True) -> dict[str, object]:
    return _trace(
        {"type": "g2_session", "source": "physical_g2" if physical else "nonphysical"},
        {"type": "gesture", "gesture": "menu_item", "delivery": "menu"},
        {"type": "visual_frame", "kind": "messages", "surface": "picker", "sha256": "1" * 64},
        {"type": "gesture", "gesture": "tap", "delivery": "list"},
        {"type": "visual_frame", "kind": "messages", "surface": "conversation", "sha256": "2" * 64},
        {"type": "gesture", "gesture": "tap", "delivery": "list"},
        {"type": "visual_frame", "kind": "messages", "surface": "compose", "sha256": "3" * 64},
        {"type": "visual_frame", "kind": "messages", "surface": "confirmation", "sha256": "4" * 64},
        {"type": "messages_confirmation", "outcome": outcome},
        {"type": "visual_frame", "kind": "messages", "surface": "compose", "sha256": "3" * 64},
    )


def test_summarize_receipt_requires_the_physical_menu_to_conversation_compose_cancel_path() -> None:
    summary = summarize_receipt(_no_send_reply_trace())

    assert summary["result"] == "pass"
    assert summary["mode"] == "physical_g2"
    assert summary["messagesAppSend"] == "not_requested"
    assert summary["confirmationOutcome"] == "declined"
    assert summary["recovery"] == "not_exercised"
    assert all("recordedAtMs" not in event for event in summary["events"])


def test_summarize_receipt_never_converts_a_loopback_trace_into_physical_proof() -> None:
    summary = summarize_receipt(_no_send_reply_trace(physical=False))

    assert summary["result"] == "inconclusive"
    assert summary["physicalG2Evidence"] is False
    assert summary["messagesAppSend"] == "not_proven"


def test_summarize_receipt_does_not_call_an_accepted_confirmation_a_no_send_run() -> None:
    summary = summarize_receipt(_no_send_reply_trace(outcome="accepted"))

    assert summary["result"] == "inconclusive"
    assert summary["physicalG2Evidence"] is True
    assert summary["messagesAppSend"] == "not_proven"


def test_summarize_receipt_reports_missing_recovery_without_erasing_a_core_pass() -> None:
    trace = _no_send_reply_trace()

    summary = summarize_receipt(trace, require_recovery=True)

    assert summary["result"] == "inconclusive"
    assert summary["coreResult"] == "pass"
    assert summary["recovery"] == "not_exercised"
