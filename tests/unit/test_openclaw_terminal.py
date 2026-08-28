"""Fail-closed Agent Terminal session contracts."""

from __future__ import annotations

import asyncio
from collections import deque

import pytest

from joat.agents.openclaw_gateway import (
    BRAINSTORM_TERMINAL_POLICY,
    CALENDAR_PROPOSAL_TERMINAL_POLICY,
    AgentTerminalPolicy,
    OpenClawEventSequenceGap,
    OpenClawGatewayError,
)
from joat.agents.openclaw_terminal import OpenClawAgentTerminal, RecoveredRun


REQUIRED_METHODS = frozenset(
    {
        "tools.effective",
        "sessions.subscribe",
        "sessions.list",
        "sessions.create",
        "sessions.describe",
        "sessions.messages.subscribe",
        "sessions.send",
        "sessions.abort",
        "chat.history",
    }
)


class FakeConnection:
    def __init__(
        self,
        *,
        tools: list[dict[str, object]] | None = None,
        events: list[dict[str, object] | BaseException] | None = None,
        notices: list[dict[str, object]] | None = None,
        existing_session: bool = False,
        subscribed: bool = True,
        subscription_key: str | None = None,
        session_route: dict[str, object] | None = None,
        listed_session_key: str | None = None,
        created_session_key: str | None = None,
        run_id: str = "run-1",
        policy: AgentTerminalPolicy = BRAINSTORM_TERMINAL_POLICY,
        hang_abort: bool = False,
        inventory_agent_id: str | None = None,
    ) -> None:
        self.advertised_methods = REQUIRED_METHODS
        self.advertised_events = frozenset({"agent", "chat", "session.message"})
        self.granted_scopes: tuple[str, ...] = ("operator.read", "operator.write")
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.events: deque[dict[str, object] | BaseException] = deque(events or [])
        self.tools = tools or []
        self.notices = notices
        self.existing_session = existing_session
        self.subscribed = subscribed
        default_key = policy.new_session_key("123")
        self.policy = policy
        self.subscription_key = (
            default_key if subscription_key is None else subscription_key
        )
        self.listed_session_key = (
            default_key if listed_session_key is None else listed_session_key
        )
        self.created_session_key = (
            default_key if created_session_key is None else created_session_key
        )
        self.run_id = run_id
        self.hang_abort = hang_abort
        self.inventory_agent_id = inventory_agent_id or policy.agent_id
        self.session_route = dict(session_route or {})
        self.session: dict[str, object] = {
            "key": default_key,
            "sessionId": "session-1",
            "label": policy.session_label,
            **self.session_route,
        }

    async def request(self, method: str, params: dict[str, object]) -> object:
        self.calls.append((method, params))
        if method == "sessions.list":
            return {
                "sessions": [
                    {
                        "key": self.listed_session_key,
                        "sessionId": "session-1",
                        "label": self.policy.session_label,
                    }
                ] if self.existing_session else [],
            }
        if method == "sessions.create":
            return {
                "ok": True,
                "key": self.created_session_key,
                "sessionId": "session-1",
                "entry": {
                    "sessionId": "session-1",
                    "label": self.policy.session_label,
                    **self.session_route,
                },
                "runStarted": False,
            }
        if method == "sessions.describe":
            return {"session": self.session}
        if method == "tools.effective":
            inventory: dict[str, object] = {
                "agentId": self.inventory_agent_id,
                "profile": "default",
                "groups": [{"id": "core", "label": "Core", "tools": self.tools}],
            }
            if self.notices is not None:
                inventory["notices"] = self.notices
            return inventory
        if method == "sessions.messages.subscribe":
            return {
                "subscribed": self.subscribed,
                "key": self.subscription_key,
            }
        if method == "sessions.send":
            return {"runId": self.run_id, "messageSeq": 1}
        if method == "sessions.abort":
            if self.hang_abort:
                await asyncio.Event().wait()
            return {"ok": True, "abortedRunId": self.run_id, "status": "aborted"}
        raise AssertionError(f"unexpected request: {method}")

    async def next_event(self) -> dict[str, object]:
        if not self.events:
            raise AssertionError("terminal requested an unexpected event")
        event = self.events.popleft()
        if isinstance(event, BaseException):
            raise event
        return event

    async def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def fixed_session_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    class FixedUuid:
        hex = "123"

    monkeypatch.setattr(
        "joat.agents.openclaw_terminal.uuid4",
        lambda: FixedUuid(),
    )


@pytest.mark.asyncio
async def test_terminal_creates_dedicated_no_tool_session_without_delivery_route() -> None:
    connection = FakeConnection()

    terminal = await OpenClawAgentTerminal.create(
        connection=connection,
        agent_id="hermes",
    )

    assert terminal.session_key == "agent:hermes:joat-123"
    assert terminal.session_id == "session-1"
    assert [method for method, _ in connection.calls] == [
        "sessions.list",
        "sessions.create",
        "sessions.describe",
        "tools.effective",
        "sessions.messages.subscribe",
    ]
    create = connection.calls[1][1]
    assert create == {
        "key": "agent:hermes:joat-123",
        "agentId": "hermes",
        "label": "JOAT Agent Terminal",
    }
    assert connection.calls[2] == (
        "sessions.describe",
        {"key": "agent:hermes:joat-123"},
    )
    assert connection.calls[4][1] == {
        "key": "agent:hermes:joat-123",
        "agentId": "hermes",
    }


@pytest.mark.asyncio
async def test_calendar_terminal_uses_a_distinct_session_namespace_and_label() -> None:
    connection = FakeConnection(policy=CALENDAR_PROPOSAL_TERMINAL_POLICY)

    terminal = await OpenClawAgentTerminal.create(
        connection=connection,
        agent_id="hermes",
        policy=CALENDAR_PROPOSAL_TERMINAL_POLICY,
    )

    assert terminal.session_key == "agent:hermes:joat-calendar-123"
    assert connection.calls[0] == (
        "sessions.list",
        {
            "label": "JOAT Calendar Proposals",
            "agentId": "hermes",
            "limit": 2,
        },
    )
    assert connection.calls[1] == (
        "sessions.create",
        {
            "key": "agent:hermes:joat-calendar-123",
            "agentId": "hermes",
            "label": "JOAT Calendar Proposals",
        },
    )


@pytest.mark.asyncio
async def test_terminal_abort_cleanup_is_bounded_when_gateway_never_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "joat.agents.openclaw_terminal.ABORT_CONFIRMATION_TIMEOUT_S",
        0.01,
    )
    connection = FakeConnection(hang_abort=True)
    terminal = await OpenClawAgentTerminal.create(
        connection=connection,
        agent_id="hermes",
    )

    async def consume() -> list[str]:
        return [chunk async for chunk in terminal.stream("question")]

    async with asyncio.timeout(0.1):
        with pytest.raises(AssertionError, match="unexpected event"):
            await consume()

    assert any(method == "sessions.abort" for method, _ in connection.calls)


@pytest.mark.asyncio
async def test_terminal_rejects_created_session_with_telegram_delivery_route() -> None:
    connection = FakeConnection(
        session_route={
            "channel": "telegram",
            "deliveryContext": {"channel": "telegram", "to": "8245150068"},
        }
    )

    with pytest.raises(OpenClawGatewayError, match="isolated Agent Terminal session"):
        await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")

    assert [method for method, _ in connection.calls] == [
        "sessions.list",
        "sessions.create",
    ]


@pytest.mark.asyncio
async def test_terminal_adopts_one_existing_owned_session_after_restart() -> None:
    connection = FakeConnection(existing_session=True)

    terminal = await OpenClawAgentTerminal.create(
        connection=connection,
        agent_id="hermes",
    )

    assert terminal.session_key == "agent:hermes:joat-123"
    assert terminal.session_id == "session-1"
    assert [method for method, _ in connection.calls] == [
        "sessions.list",
        "sessions.describe",
        "tools.effective",
        "sessions.messages.subscribe",
    ]


@pytest.mark.asyncio
async def test_terminal_rejects_existing_session_when_tools_bind_another_agent() -> None:
    connection = FakeConnection(
        existing_session=True,
        inventory_agent_id="main",
    )

    with pytest.raises(OpenClawGatewayError, match="invalid effective tool inventory"):
        await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")

    assert [method for method, _ in connection.calls] == [
        "sessions.list",
        "sessions.describe",
        "tools.effective",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "poisoned_key",
    [
        "agent:main:main",
        "agent:main:telegram:direct:8245150068",
        "agent:hermes:telegram:group:-1001234567890:topic:42",
    ],
)
async def test_terminal_never_adopts_main_or_telegram_existing_session(
    poisoned_key: str,
) -> None:
    connection = FakeConnection(
        existing_session=True,
        listed_session_key=poisoned_key,
    )

    with pytest.raises(OpenClawGatewayError, match="invalid Agent Terminal session"):
        await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")

    assert [method for method, _ in connection.calls] == ["sessions.list"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "poisoned_key",
    [
        "agent:main:main",
        "agent:main:telegram:direct:8245150068",
        "agent:hermes:telegram:direct:8245150068",
    ],
)
async def test_terminal_never_follows_poisoned_created_session(
    poisoned_key: str,
) -> None:
    connection = FakeConnection(created_session_key=poisoned_key)

    with pytest.raises(OpenClawGatewayError, match="isolated Agent Terminal session"):
        await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")

    assert [method for method, _ in connection.calls] == [
        "sessions.list",
        "sessions.create",
    ]
    assert connection.calls[1][1]["key"] == "agent:hermes:joat-123"


@pytest.mark.asyncio
async def test_terminal_accepts_omitted_optional_tool_notices() -> None:
    connection = FakeConnection(notices=None)

    terminal = await OpenClawAgentTerminal.create(
        connection=connection,
        agent_id="hermes",
    )

    assert terminal.session_id == "session-1"


@pytest.mark.asyncio
async def test_terminal_accepts_only_targetless_internal_webchat_provenance() -> None:
    connection = FakeConnection(
        existing_session=True,
        session_route={
            "deliveryContext": {"channel": "webchat"},
            "lastChannel": "webchat",
            "origin": {
                "provider": "webchat",
                "surface": "webchat",
                "chatType": "direct",
                "label": "JOAT Brainstorm",
            },
        },
    )

    terminal = await OpenClawAgentTerminal.create(
        connection=connection,
        agent_id="hermes",
    )

    assert terminal.session_id == "session-1"


@pytest.mark.asyncio
async def test_terminal_rejects_webchat_provenance_with_delivery_target() -> None:
    connection = FakeConnection(
        existing_session=True,
        session_route={
            "deliveryContext": {"channel": "webchat", "to": "other-client"},
            "lastChannel": "webchat",
            "origin": {
                "provider": "webchat",
                "surface": "webchat",
                "chatType": "direct",
                "label": "JOAT Brainstorm",
            },
        },
    )

    with pytest.raises(OpenClawGatewayError, match="not isolated"):
        await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")


@pytest.mark.asyncio
async def test_terminal_rechecks_delivery_isolation_before_every_send() -> None:
    connection = FakeConnection()
    terminal = await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")
    connection.session["deliveryContext"] = {
        "channel": "telegram",
        "to": "8245150068",
    }

    with pytest.raises(OpenClawGatewayError, match="not isolated from delivery routes"):
        _ = [chunk async for chunk in terminal.stream("Do not deliver this")]

    assert not any(method == "sessions.send" for method, _ in connection.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("subscribed", "subscription_key"),
    [
        (False, "agent:hermes:joat-123"),
        (True, "agent:hermes:other"),
    ],
)
async def test_terminal_requires_confirmed_subscription_to_its_exact_session(
    subscribed: bool,
    subscription_key: str,
) -> None:
    connection = FakeConnection(
        subscribed=subscribed,
        subscription_key=subscription_key,
    )

    with pytest.raises(OpenClawGatewayError, match="subscription"):
        await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")


@pytest.mark.asyncio
async def test_terminal_refuses_to_send_when_agent_has_any_effective_tool() -> None:
    connection = FakeConnection(
        tools=[{"id": "exec", "label": "Execute", "source": "core"}]
    )

    with pytest.raises(OpenClawGatewayError, match="tools are enabled"):
        await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")

    assert [method for method, _ in connection.calls] == [
        "sessions.list",
        "sessions.create",
        "sessions.describe",
        "tools.effective",
    ]


@pytest.mark.asyncio
async def test_terminal_refuses_unsettled_effective_tool_inventory() -> None:
    connection = FakeConnection(
        notices=[{"source": "mcp", "status": "not-yet-listed"}],
    )

    with pytest.raises(OpenClawGatewayError, match="inventory is unsettled"):
        await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")


@pytest.mark.asyncio
async def test_terminal_streams_only_owned_run_and_aborts_on_divergent_replace() -> None:
    acknowledged_run_id = "opaque/run=id α"
    connection = FakeConnection(
        run_id=acknowledged_run_id,
        events=[
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "runId": "other-run",
                    "sessionKey": "agent:hermes:joat-123",
                    "state": "delta",
                    "seq": 0,
                    "deltaText": "ignore",
                },
            },
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "runId": acknowledged_run_id,
                    "sessionKey": "agent:hermes:joat-123",
                    "state": "delta",
                    "seq": 0,
                    "deltaText": "Hello",
                },
            },
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "runId": acknowledged_run_id,
                    "sessionKey": "agent:hermes:joat-123",
                    "state": "delta",
                    "seq": 1,
                    "deltaText": "Different answer",
                    "replace": True,
                },
            },
        ]
    )
    terminal = await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")

    chunks: list[str] = []
    with pytest.raises(OpenClawGatewayError, match="replacement diverged"):
        async for chunk in terminal.stream("Say hello"):
            chunks.append(chunk)

    assert chunks == ["Hello"]
    abort_method, abort = connection.calls[-1]
    assert abort_method == "sessions.abort"
    assert abort == {
        "key": "agent:hermes:joat-123",
        "runId": acknowledged_run_id,
        "agentId": "hermes",
    }


@pytest.mark.asyncio
async def test_terminal_stream_completes_on_matching_final_event() -> None:
    connection = FakeConnection(
        events=[
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "runId": "run-1",
                    "sessionKey": "agent:hermes:joat-123",
                    "state": "delta",
                    "seq": 0,
                    "deltaText": "Hello",
                },
            },
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "runId": "run-1",
                    "sessionKey": "agent:hermes:joat-123",
                    "state": "final",
                    "seq": 1,
                },
            },
        ]
    )
    terminal = await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")

    chunks = [chunk async for chunk in terminal.stream("Say hello")]

    assert chunks == ["Hello"]
    assert connection.calls[-1][0] == "sessions.send"
    assert not any(method == "sessions.abort" for method, _ in connection.calls)


@pytest.mark.asyncio
async def test_terminal_ignores_duplicate_sequence_and_uses_final_message_suffix() -> None:
    connection = FakeConnection(
        events=[
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "runId": "run-1",
                    "sessionKey": "agent:hermes:joat-123",
                    "state": "delta",
                    "seq": 4,
                    "deltaText": "Hello",
                },
            },
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "runId": "run-1",
                    "sessionKey": "agent:hermes:joat-123",
                    "state": "delta",
                    "seq": 4,
                    "deltaText": "Hello",
                },
            },
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "runId": "run-1",
                    "sessionKey": "agent:hermes:joat-123",
                    "state": "final",
                    "seq": 4,
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Hello world"}],
                    },
                },
            },
        ]
    )
    terminal = await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")

    chunks = [chunk async for chunk in terminal.stream("Say hello")]

    assert chunks == ["Hello", " world"]


@pytest.mark.asyncio
async def test_terminal_accepts_throttled_forward_run_sequence_gap() -> None:
    connection = FakeConnection(
        events=[
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "runId": "run-1",
                    "sessionKey": "agent:hermes:joat-123",
                    "state": "delta",
                    "seq": 1,
                    "deltaText": "Hello",
                },
            },
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "runId": "run-1",
                    "sessionKey": "agent:hermes:joat-123",
                    "state": "final",
                    "seq": 3,
                },
            },
        ]
    )
    terminal = await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")

    chunks = [chunk async for chunk in terminal.stream("Say hello")]

    assert chunks == ["Hello"]


@pytest.mark.asyncio
async def test_terminal_recovers_gateway_event_sequence_gap_from_exact_history() -> None:
    connection = FakeConnection(
        events=[
            {
                "type": "event",
                "event": "chat",
                "payload": {
                    "runId": "run-1",
                    "sessionKey": "agent:hermes:joat-123",
                    "state": "delta",
                    "seq": 1,
                    "deltaText": "Hello",
                },
            },
            OpenClawEventSequenceGap(expected=8, actual=10),
        ]
    )
    recovered_connection = FakeConnection()

    class Recovery:
        calls = 0

        async def recover(self, **_: object) -> RecoveredRun:
            self.calls += 1
            return RecoveredRun(
                connection=recovered_connection,
                text="Hello world",
                active=False,
                complete=True,
            )

    recovery = Recovery()
    terminal = await OpenClawAgentTerminal.create(
        connection=connection,
        agent_id="hermes",
        recovery=recovery,
    )

    chunks = [chunk async for chunk in terminal.stream("Say hello")]

    assert chunks == ["Hello", " world"]
    assert recovery.calls == 1
    assert terminal.connection is recovered_connection
    assert recovered_connection.calls == [
        (
            "sessions.describe",
            {"key": "agent:hermes:joat-123"},
        ),
        (
            "tools.effective",
            {"sessionKey": "agent:hermes:joat-123", "agentId": "hermes"},
        )
    ]


@pytest.mark.asyncio
async def test_terminal_aborts_an_owned_tool_event() -> None:
    connection = FakeConnection(
        events=[
            {
                "type": "event",
                "event": "agent",
                "payload": {
                    "runId": "run-1",
                    "sessionKey": "agent:hermes:joat-123",
                    "stream": "tool",
                    "seq": 1,
                    "data": {"phase": "start", "name": "read"},
                },
            }
        ]
    )
    terminal = await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")

    with pytest.raises(OpenClawGatewayError, match="blocked Agent Terminal capability"):
        _ = [chunk async for chunk in terminal.stream("Do not use tools")]

    assert connection.calls[-1] == (
        "sessions.abort",
        {
            "key": "agent:hermes:joat-123",
            "runId": "run-1",
            "agentId": "hermes",
        },
    )
