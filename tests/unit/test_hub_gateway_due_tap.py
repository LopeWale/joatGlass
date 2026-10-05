"""A tap on the due Reminder page completes only the occurrence this client was shown."""

from __future__ import annotations

import asyncio
import json

import pytest

from joat.ble.hub_gateway import HubGatewayTransport, _PendingReminderDueAck

OCCURRENCE = "67ec97c5-a0fd-48ec-89cb-1b756ddaf87a"
ATTEMPT = "a76b12f1-45e4-41fd-aed8-086f6b6b1954"


def gateway_with_accepted_due() -> tuple[HubGatewayTransport, list[str]]:
    transport = HubGatewayTransport(host="127.0.0.1", port=0)
    transport._session_id = "session-1"
    transport._connection_generation = 7
    completed: list[str] = []
    transport.on_reminder_due_complete(completed.append)
    future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    transport._pending_reminder_due_acks[OCCURRENCE] = _PendingReminderDueAck(
        future=future, session_id="session-1", generation=7, attempt_id=ATTEMPT,
    )
    transport._dispatch(json.dumps({
        "kind": "reminder_due.ack", "reminderDueId": OCCURRENCE,
        "reminderDueAttemptId": ATTEMPT, "generation": 7, "outcome": "accepted",
    }), "session-1")
    assert future.result() is True
    return transport, completed


def tap(generation: int = 7, attempt: str = ATTEMPT, occurrence: str = OCCURRENCE) -> str:
    return json.dumps({
        "kind": "reminder_due.complete", "reminderDueId": occurrence,
        "reminderDueAttemptId": attempt, "generation": generation,
    })


@pytest.mark.asyncio
async def test_the_tap_completes_the_accepted_occurrence_exactly_once() -> None:
    transport, completed = gateway_with_accepted_due()
    transport._dispatch(tap(), "session-1")
    assert completed == [OCCURRENCE]
    transport._dispatch(tap(), "session-1")
    assert completed == [OCCURRENCE]


@pytest.mark.asyncio
async def test_a_tap_that_does_not_match_the_accepted_page_is_dropped() -> None:
    transport, completed = gateway_with_accepted_due()
    transport._dispatch(tap(generation=6), "session-1")
    transport._dispatch(tap(attempt="9b38eafb-6fda-4d4c-bb6e-80b9365854b5"), "session-1")
    transport._dispatch(tap(occurrence="9b38eafb-6fda-4d4c-bb6e-80b9365854b5"), "session-1")
    transport._dispatch(tap(), "session-2")
    assert completed == []
    # A new socket forgets what the old one showed.
    transport._clear_reminder_due_acks("session-1")
    transport._dispatch(tap(), "session-1")
    assert completed == []
