"""Hermes may propose one action; JOAT remains the action authority."""

from __future__ import annotations

from collections.abc import AsyncIterator
import asyncio
from datetime import datetime
import re

import pytest

from joat.services.hermes_productivity import HermesCalendarProposalController
from joat.services.productivity_actions import ActionOrigin, CalendarEventIntent


class ScriptedAgent:
    def __init__(self, chunks: list[str] | BaseException) -> None:
        self._chunks = chunks
        self.prompts: list[str] = []
        self.closed = False

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        self.prompts.append(prompt)
        try:
            if isinstance(self._chunks, BaseException):
                raise self._chunks
            for chunk in self._chunks:
                yield chunk
        finally:
            self.closed = True


class RecordingCalendarActions:
    def __init__(self, result: str = "Created Calendar event: Design review.") -> None:
        self.result = result
        self.calls: list[tuple[CalendarEventIntent, ActionOrigin]] = []

    async def create_calendar_event(
        self,
        intent: CalendarEventIntent,
        *,
        origin: ActionOrigin,
    ) -> str:
        self.calls.append((intent, origin))
        return self.result


def controller(
    response: list[str] | BaseException,
) -> tuple[HermesCalendarProposalController, ScriptedAgent, RecordingCalendarActions]:
    agent = ScriptedAgent(response)
    actions = RecordingCalendarActions()
    command = HermesCalendarProposalController(
        agent,
        actions,  # type: ignore[arg-type]
        now=lambda: datetime.fromisoformat("2026-08-28T13:00:00-05:00"),
        local_time_zone="America/Chicago",
        proposal_id=lambda: "0123456789abcdef0123456789abcdef",
    )
    return command, agent, actions


async def test_valid_hermes_candidate_enters_the_typed_local_calendar_boundary() -> None:
    command, agent, actions = controller([
        "JOAT_CALENDAR_V1\n",
        '{"title":"Design review","date":"2026-09-01","time":"09:30",'
        '"duration_minutes":45,"time_zone":"America/Chicago","calendar":"Work"}',
    ])

    result = await command.handle_command(
        "ask Hermes to draft a calendar event for Design review in Work on "
        "2026-09-01 at 09:30 for 45 minutes in America/Chicago"
    )

    assert result == "Created Calendar event: Design review."
    assert actions.calls == [(
        CalendarEventIntent(
            title="Design review",
            date="2026-09-01",
            time="09:30",
            duration_minutes=45,
            time_zone="America/Chicago",
            calendar_title="Work",
        ),
        ActionOrigin(
            kind="openclaw.hermes.proposal",
            identifier="0123456789abcdef0123456789abcdef",
            ordinal=0,
        ),
    )]
    assert "Current local datetime: 2026-08-28T13:00:00-05:00" in agent.prompts[0]
    assert "Default IANA time zone: America/Chicago" in agent.prompts[0]
    assert "Do not execute, schedule, send, or call a tool" in agent.prompts[0]
    assert agent.closed is True


async def test_common_whisper_asks_hermes_variant_is_explicitly_claimed() -> None:
    command, agent, actions = controller([
        "JOAT_CALENDAR_V1\n",
        '{"title":"Design review","date":"2026-09-01","time":"09:30",'
        '"duration_minutes":45,"time_zone":"America/Chicago","calendar":null}',
    ])

    result = await command.handle_command(
        "asks Hermes to draft a calendar event for Design review"
    )

    assert result == "Created Calendar event: Design review."
    assert len(agent.prompts) == 1
    assert len(actions.calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        'Here you go:\nJOAT_CALENDAR_V1\n{"title":"Review"}',
        (
            'JOAT_CALENDAR_V1\n{"title":"Review","date":"2026-09-01",'
            '"time":"09:30","duration_minutes":30,"time_zone":"America/Chicago",'
            '"calendar":null,"attendees":["a@example.com"]}'
        ),
        (
            'JOAT_CALENDAR_V1\n{"title":"Review","title":"Changed",'
            '"date":"2026-09-01","time":"09:30","duration_minutes":30,'
            '"time_zone":"America/Chicago","calendar":null}'
        ),
        (
            'JOAT_CALENDAR_V1\n{"title":"Review","date":"2026-09-01",'
            '"time":"09:30","duration_minutes":true,"time_zone":"America/Chicago",'
            '"calendar":null}'
        ),
        (
            'JOAT_CALENDAR_V1\n{"title":"Review","date":"tomorrow",'
            '"time":"9:30","duration_minutes":30,"time_zone":"CDT",'
            '"calendar":null}'
        ),
        (
            'JOAT_CALENDAR_V1\n{"title":"Review\\u000aInjected",'
            '"date":"2026-09-01","time":"09:30","duration_minutes":30,'
            '"time_zone":"America/Chicago","calendar":null}'
        ),
        (
            'JOAT_CALENDAR_V1\n{"title":"Review\\u202eHidden",'
            '"date":"2026-09-01","time":"09:30","duration_minutes":30,'
            '"time_zone":"America/Chicago","calendar":null}'
        ),
    ],
)
async def test_malformed_or_widened_candidate_never_reaches_calendar(
    response: str,
) -> None:
    command, _, actions = controller([response])

    result = await command.handle_command(
        "ask Hermes to draft a calendar event for a review"
    )

    assert result == "Hermes returned no valid Calendar proposal. Nothing was created."
    assert actions.calls == []


async def test_no_proposal_and_agent_failure_are_fail_closed() -> None:
    declined, _, declined_actions = controller(["NO_PROPOSAL"])
    assert await declined.handle_command(
        "ask Hermes to draft a calendar event for sometime next week"
    ) == "Hermes needs more exact Calendar details. Nothing was created."
    assert declined_actions.calls == []

    failed, _, failed_actions = controller(RuntimeError("remote detail must stay private"))
    assert await failed.handle_command(
        "ask Hermes to draft a calendar event for a review"
    ) == "Hermes could not prepare a Calendar proposal. Nothing was created."
    assert failed_actions.calls == []


async def test_oversized_candidate_stops_collection_and_never_creates_a_draft() -> None:
    command, agent, actions = controller(["x" * 2_049, "never consumed"])

    result = await command.handle_command(
        "ask Hermes to draft a calendar event for a review"
    )

    assert result == "Hermes returned no valid Calendar proposal. Nothing was created."
    assert actions.calls == []
    assert agent.closed is True


async def test_timeout_closes_the_remote_stream_and_never_creates_a_draft() -> None:
    closed = False

    class HangingAgent:
        async def stream(self, _prompt: str) -> AsyncIterator[str]:
            nonlocal closed
            try:
                await asyncio.Event().wait()
                yield "unreachable"
            finally:
                closed = True

    actions = RecordingCalendarActions()
    command = HermesCalendarProposalController(
        HangingAgent(),
        actions,  # type: ignore[arg-type]
        local_time_zone="America/Chicago",
        timeout_s=0.01,
    )

    assert await command.handle_command(
        "ask Hermes to draft a calendar event for review"
    ) == "Hermes could not prepare a Calendar proposal. Nothing was created."
    assert actions.calls == []
    assert closed is True


async def test_timeout_remains_bounded_when_remote_cleanup_stalls() -> None:
    close_started = asyncio.Event()

    class SlowClosingAgent:
        async def stream(self, _prompt: str) -> AsyncIterator[str]:
            try:
                await asyncio.Event().wait()
                yield "unreachable"
            finally:
                close_started.set()
                await asyncio.sleep(10)

    actions = RecordingCalendarActions()
    command = HermesCalendarProposalController(
        SlowClosingAgent(),
        actions,  # type: ignore[arg-type]
        local_time_zone="America/Chicago",
        timeout_s=0.04,
        cleanup_timeout_s=0.01,
    )
    started = asyncio.get_running_loop().time()

    result = await asyncio.wait_for(
        command.handle_command(
            "ask Hermes to draft a calendar event for review tomorrow"
        ),
        timeout=0.15,
    )

    assert result == "Hermes timed out and remote stop was not confirmed. Nothing was created."
    assert asyncio.get_running_loop().time() - started < 0.15
    assert close_started.is_set()
    assert actions.calls == []


async def test_parent_cancellation_closes_collector_and_releases_calendar_lane() -> None:
    entered = asyncio.Event()
    closed = asyncio.Event()

    class SerialAgent:
        def __init__(self) -> None:
            self._lock = asyncio.Lock()
            self.calls = 0

        async def stream(self, _prompt: str) -> AsyncIterator[str]:
            async with self._lock:
                self.calls += 1
                if self.calls == 1:
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        closed.set()
                    return
                yield "NO_PROPOSAL"

    agent = SerialAgent()
    actions = RecordingCalendarActions()
    command = HermesCalendarProposalController(
        agent,
        actions,  # type: ignore[arg-type]
        local_time_zone="America/Chicago",
        timeout_s=1.0,
        cleanup_timeout_s=0.1,
    )
    first = asyncio.create_task(command.handle_command(
        "ask Hermes to draft a calendar event for review tomorrow"
    ))
    await entered.wait()

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    assert closed.is_set()
    async with asyncio.timeout(0.1):
        assert await command.handle_command(
            "ask Hermes to draft a calendar event for review next Friday"
        ) == "Hermes needs more exact Calendar details. Nothing was created."
    assert agent.calls == 2
    assert actions.calls == []


async def test_long_request_is_rejected_before_contacting_hermes() -> None:
    command, agent, actions = controller(["NO_PROPOSAL"])

    assert await command.handle_command(
        "ask Hermes to draft a calendar event for " + "x" * 513
    ) == "Calendar request is too long for Hermes. Nothing was created."
    assert agent.prompts == []
    assert actions.calls == []


@pytest.mark.parametrize("control", ["\u202e", "\u2066", "\u200b", "\ud800"])
async def test_request_unicode_controls_are_rejected_before_contacting_hermes(
    control: str,
) -> None:
    command, agent, actions = controller(["NO_PROPOSAL"])

    assert await command.handle_command(
        f"ask Hermes to draft a calendar event for review {control} tomorrow"
    ) == "Calendar request contains unsafe text controls. Nothing was created."
    assert agent.prompts == []
    assert actions.calls == []


@pytest.mark.parametrize(
    "detail",
    [
        "review and invite alice@example.com",
        "a recurring review every Tuesday",
        "an all-day review",
        "update the existing review",
        "review with a 15 minute alarm",
    ],
)
async def test_unsupported_calendar_scope_is_rejected_before_contacting_hermes(
    detail: str,
) -> None:
    command, agent, actions = controller(["NO_PROPOSAL"])

    assert await command.handle_command(
        f"ask Hermes to draft a calendar event for {detail}"
    ) == "That Calendar request includes unsupported actions. Nothing was created."
    assert agent.prompts == []
    assert actions.calls == []


async def test_non_command_does_not_contact_hermes() -> None:
    command, agent, actions = controller(["NO_PROPOSAL"])

    assert await command.handle_command("what is on my calendar?") is None
    assert agent.prompts == []
    assert actions.calls == []


async def test_explicit_command_is_claimed_when_agent_terminal_is_absent() -> None:
    actions = RecordingCalendarActions()
    command = HermesCalendarProposalController(
        None,
        actions,  # type: ignore[arg-type]
        local_time_zone="America/Chicago",
    )

    assert await command.handle_command(
        "ask Hermes to draft a calendar event for review"
    ) == "Hermes Agent Terminal is not ready. Nothing was created."
    assert actions.calls == []


async def test_generated_proposal_identity_is_local_and_opaque() -> None:
    command, _, actions = controller([
        'JOAT_CALENDAR_V1\n{"title":"Review","date":"2026-09-01",'
        '"time":"09:30","duration_minutes":30,"time_zone":"America/Chicago",'
        '"calendar":null}'
    ])

    await command.handle_command("Hermes, draft a calendar event for review")

    assert re.fullmatch(r"[0-9a-f]{32}", actions.calls[0][1].identifier)


async def test_local_timezone_is_resolved_when_each_request_runs() -> None:
    agent = ScriptedAgent(["NO_PROPOSAL"])
    actions = RecordingCalendarActions()
    zone = "America/Chicago"
    command = HermesCalendarProposalController(
        agent,
        actions,  # type: ignore[arg-type]
        local_time_zone=lambda: zone,
    )
    zone = "America/New_York"

    await command.handle_command(
        "ask Hermes to draft a calendar event for review tomorrow"
    )

    assert "Default IANA time zone: America/New_York" in agent.prompts[0]
