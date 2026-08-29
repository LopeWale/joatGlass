"""Bounded reconnect and authoritative history recovery for Agent Terminal."""

from __future__ import annotations

import asyncio
from collections import deque

import pytest

from joat.agents.openclaw_gateway import (
    OpenClawConnectionClosed,
    OpenClawGatewayError,
)
from joat.agents.openclaw_runtime import (
    OpenClawHistoryRecovery,
    OpenClawRuntimeConfig,
    OpenClawTerminalAgent,
)
from joat.agents.openclaw_terminal import OpenClawToolsEnabled


METHODS = frozenset(
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
EVENTS = frozenset({"agent", "chat", "session.message"})


@pytest.fixture(autouse=True)
def fixed_session_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    class FixedUuid:
        hex = "123"

    monkeypatch.setattr(
        "joat.agents.openclaw_terminal.uuid4",
        lambda: FixedUuid(),
    )


def test_runtime_config_requires_exact_owner_only_non_secret_schema(tmp_path) -> None:
    path = tmp_path / "openclaw.json"
    path.write_text(
        '{"gateway_url":"wss://admins-mac-mini.tail3a3363.ts.net",'
        '"agent_id":"hermes"}'
    )
    path.chmod(0o600)

    config = OpenClawRuntimeConfig.load(path)

    assert config is not None
    assert config.profile.url == "wss://admins-mac-mini.tail3a3363.ts.net"
    assert config.agent_id == "hermes"


@pytest.mark.parametrize(
    "payload",
    [
        '{"gateway_url":"wss://example.ts.net","agent_id":"hermes",'
        '"token":"secret"}',
        '{"gateway_url":"https://example.ts.net","agent_id":"hermes"}',
        '{"gateway_url":"wss://admins-mac-mini.tail3a3363.ts.net:9443",'
        '"agent_id":"hermes"}',
        '{"gateway_url":"wss://example.ts.net","agent_id":"bad agent"}',
        '{"gateway_url":"wss://example.ts.net","agent_id":"main"}',
    ],
)
def test_runtime_config_rejects_secrets_unsafe_urls_and_bad_agents(
    tmp_path,
    payload: str,
) -> None:
    path = tmp_path / "openclaw.json"
    path.write_text(payload)
    path.chmod(0o600)

    with pytest.raises(ValueError):
        OpenClawRuntimeConfig.load(path)


def test_runtime_config_rejects_group_readable_file(tmp_path) -> None:
    path = tmp_path / "openclaw.json"
    path.write_text(
        '{"gateway_url":"wss://example.ts.net","agent_id":"hermes"}'
    )
    path.chmod(0o640)

    with pytest.raises(ValueError, match="owner-only"):
        OpenClawRuntimeConfig.load(path)


class RuntimeConnection:
    def __init__(self, responses: dict[str, object]) -> None:
        self.advertised_methods = METHODS
        self.advertised_events = EVENTS
        self.granted_scopes = ("operator.read", "operator.write")
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    async def request(self, method: str, params: dict[str, object]) -> object:
        self.calls.append((method, params))
        response = self.responses[method]
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, deque):
            return response.popleft()
        return response

    async def next_event(self) -> dict[str, object]:
        raise AssertionError("history recovery must not consume a live event")

    async def close(self) -> None:
        self.closed = True


class RuntimeFactory:
    def __init__(self, results: list[RuntimeConnection | BaseException]) -> None:
        self.results = deque(results)
        self.calls = 0

    async def connect(self) -> RuntimeConnection:
        self.calls += 1
        result = self.results.popleft()
        if isinstance(result, BaseException):
            raise result
        return result


def recovery_connection(history: object) -> RuntimeConnection:
    return RuntimeConnection(
        {
            "sessions.subscribe": {"subscribed": True, "key": "catalog"},
            "sessions.messages.subscribe": {
                "subscribed": True,
                "key": "agent:hermes:joat-123",
            },
            "chat.history": history,
        }
    )


@pytest.mark.asyncio
async def test_recovery_adopts_exact_active_run_snapshot() -> None:
    previous = RuntimeConnection({})
    reconnected = recovery_connection(
        {
            "messages": [],
            "sessionInfo": {
                "hasActiveRun": True,
                "activeRunIds": ["run-1"],
            },
            "inFlightRun": {"runId": "run-1", "text": "partial"},
        }
    )
    recovery = OpenClawHistoryRecovery(
        client=RuntimeFactory([reconnected]),
        attempts=1,
        backoff_s=(0.0,),
    )

    result = await recovery.recover(
        connection=previous,
        agent_id="hermes",
        session_key="agent:hermes:joat-123",
        run_id="run-1",
        prompt="question",
    )

    assert previous.closed
    assert result.connection is reconnected
    assert result.text == "partial"
    assert result.active and not result.complete
    assert [method for method, _ in reconnected.calls] == [
        "sessions.subscribe",
        "sessions.messages.subscribe",
        "chat.history",
    ]


@pytest.mark.asyncio
async def test_recovery_refuses_unproven_completed_reply_after_disconnect() -> None:
    reconnected = recovery_connection(
        {
            "messages": [
                {"role": "user", "content": "question"},
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "answer"}],
                },
            ],
            "sessionInfo": {"hasActiveRun": False, "activeRunIds": []},
            "inFlightRun": None,
        }
    )
    recovery = OpenClawHistoryRecovery(
        client=RuntimeFactory([reconnected]),
        attempts=1,
        backoff_s=(0.0,),
    )

    with pytest.raises(OpenClawGatewayError, match="outcome is uncertain"):
        await recovery.recover(
            connection=RuntimeConnection({}),
            agent_id="hermes",
            session_key="agent:hermes:joat-123",
            run_id="run-1",
            prompt="question",
        )

    assert reconnected.closed


@pytest.mark.asyncio
async def test_recovery_rejects_different_in_flight_run_and_closes_socket() -> None:
    reconnected = recovery_connection(
        {
            "messages": [],
            "sessionInfo": {
                "hasActiveRun": True,
                "activeRunIds": ["run-other"],
            },
            "inFlightRun": {"runId": "run-other", "text": "not ours"},
        }
    )
    recovery = OpenClawHistoryRecovery(
        client=RuntimeFactory([reconnected]),
        attempts=1,
        backoff_s=(0.0,),
    )

    with pytest.raises(OpenClawGatewayError, match="different in-flight run"):
        await recovery.recover(
            connection=RuntimeConnection({}),
            agent_id="hermes",
            session_key="agent:hermes:joat-123",
            run_id="run-1",
            prompt="question",
        )

    assert reconnected.closed


@pytest.mark.asyncio
async def test_recovery_closes_reconnected_socket_when_rpc_raises() -> None:
    reconnected = RuntimeConnection(
        {"sessions.subscribe": OpenClawConnectionClosed("lost again")}
    )
    recovery = OpenClawHistoryRecovery(
        client=RuntimeFactory([reconnected]),
        attempts=1,
        backoff_s=(0.0,),
    )

    with pytest.raises(OpenClawConnectionClosed):
        await recovery.recover(
            connection=RuntimeConnection({}),
            agent_id="hermes",
            session_key="agent:hermes:joat-123",
            run_id="run-1",
            prompt="question",
        )

    assert reconnected.closed


@pytest.mark.asyncio
async def test_recovery_retries_connection_only_with_bounded_backoff() -> None:
    reconnected = recovery_connection(
        {
            "messages": [],
            "sessionInfo": {"hasActiveRun": True, "activeRunIds": ["run-1"]},
            "inFlightRun": {"runId": "run-1", "text": "partial"},
        }
    )
    factory = RuntimeFactory(
        [
            OpenClawConnectionClosed("first"),
            OpenClawConnectionClosed("second"),
            reconnected,
        ]
    )
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    recovery = OpenClawHistoryRecovery(
        client=factory,
        attempts=3,
        backoff_s=(0.0, 0.25, 1.0),
        sleep=record_sleep,
    )

    result = await recovery.recover(
        connection=RuntimeConnection({}),
        agent_id="hermes",
        session_key="agent:hermes:joat-123",
        run_id="run-1",
        prompt="question",
    )

    assert result.active and not result.complete
    assert result.text == "partial"
    assert factory.calls == 3
    assert sleeps == [0.25, 1.0]


@pytest.mark.asyncio
async def test_terminal_agent_initializes_one_dedicated_session_and_closes_it() -> None:
    connection = RuntimeConnection(
        {
            "sessions.list": {"sessions": []},
            "sessions.create": {
                "ok": True,
                "key": "agent:hermes:joat-123",
                "sessionId": "session-1",
                "entry": {
                    "sessionId": "session-1",
                    "label": "JOAT Agent Terminal",
                },
                "runStarted": False,
            },
            "sessions.describe": {
                "session": {
                    "key": "agent:hermes:joat-123",
                    "sessionId": "session-1",
                    "label": "JOAT Agent Terminal",
                    "agentId": "hermes",
                }
            },
            "tools.effective": {
                "agentId": "hermes",
                "groups": [{"id": "core", "tools": []}],
                "notices": [],
            },
            "sessions.messages.subscribe": {
                "subscribed": True,
                "key": "agent:hermes:joat-123",
            },
        }
    )
    factory = RuntimeFactory([connection])
    agent = OpenClawTerminalAgent(client=factory, agent_id="hermes")

    await agent.initialize()
    await agent.initialize()
    await agent.close()

    assert factory.calls == 1
    assert connection.closed


@pytest.mark.asyncio
async def test_terminal_agent_cannot_reinitialize_after_close() -> None:
    connection = RuntimeConnection({})
    factory = RuntimeFactory([connection])
    agent = OpenClawTerminalAgent(client=factory, agent_id="hermes")

    await agent.close()

    with pytest.raises(OpenClawGatewayError, match="closed"):
        await agent.initialize()
    assert factory.calls == 0


@pytest.mark.asyncio
async def test_terminal_agent_close_is_bounded_when_socket_close_stalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_started = asyncio.Event()

    class HangingCloseConnection(RuntimeConnection):
        async def close(self) -> None:
            close_started.set()
            await asyncio.Event().wait()

    class Terminal:
        connection = HangingCloseConnection({})
        recovery = None

    monkeypatch.setattr(
        "joat.agents.openclaw_runtime.CONNECTION_CLOSE_TIMEOUT_S",
        0.01,
    )
    agent = OpenClawTerminalAgent(client=RuntimeFactory([]), agent_id="hermes")
    agent._terminal = Terminal()  # type: ignore[assignment]

    async with asyncio.timeout(0.1):
        await agent.close()

    assert close_started.is_set()


@pytest.mark.asyncio
async def test_terminal_agent_close_cancels_active_stream_before_returning() -> None:
    entered = asyncio.Event()

    class Terminal:
        connection = RuntimeConnection({})

        async def stream(self, prompt: str):
            entered.set()
            await asyncio.Event().wait()
            if False:
                yield ""

    agent = OpenClawTerminalAgent(client=RuntimeFactory([]), agent_id="hermes")
    agent._terminal = Terminal()  # type: ignore[assignment]

    async def consume() -> list[str]:
        return [chunk async for chunk in agent.stream("question")]

    task = asyncio.create_task(consume())
    await entered.wait()
    try:
        await agent.close()
        assert task.done()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert Terminal.connection.closed
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


@pytest.mark.asyncio
async def test_terminal_agent_can_close_from_its_stream_consumer_task() -> None:
    class Terminal:
        connection = RuntimeConnection({})

        async def stream(self, prompt: str):
            yield "answer"
            await asyncio.Event().wait()

    agent = OpenClawTerminalAgent(client=RuntimeFactory([]), agent_id="hermes")
    agent._terminal = Terminal()  # type: ignore[assignment]

    async def consume_and_close() -> str:
        async for chunk in agent.stream("question"):
            await agent.close()
            return chunk
        raise AssertionError("stream ended before yielding")

    async with asyncio.timeout(0.2):
        result = await consume_and_close()

    assert result == "answer"
    assert Terminal.connection.closed


@pytest.mark.asyncio
async def test_terminal_agent_publishes_live_running_and_ready_states() -> None:
    states: list[tuple[str, str]] = []

    class Terminal:
        connection = RuntimeConnection({})

        async def stream(self, prompt: str):
            assert prompt == "question"
            yield "answer"

    agent = OpenClawTerminalAgent(
        client=RuntimeFactory([]),
        agent_id="hermes",
        state_changed=lambda state, reason: states.append((state, reason)),
    )
    agent._terminal = Terminal()  # type: ignore[assignment]

    assert [chunk async for chunk in agent.stream("question")] == ["answer"]
    assert states == [("running", ""), ("ready", "")]


@pytest.mark.asyncio
async def test_terminal_agent_publishes_safety_block_reason() -> None:
    states: list[tuple[str, str]] = []

    class Terminal:
        connection = RuntimeConnection({})

        async def stream(self, prompt: str):
            if False:
                yield ""
            raise OpenClawToolsEnabled("tools appeared")

    agent = OpenClawTerminalAgent(
        client=RuntimeFactory([]),
        agent_id="hermes",
        state_changed=lambda state, reason: states.append((state, reason)),
    )
    agent._terminal = Terminal()  # type: ignore[assignment]

    with pytest.raises(OpenClawToolsEnabled):
        _ = [chunk async for chunk in agent.stream("question")]
    assert states == [("running", ""), ("blocked", "tools_enabled")]
    assert agent._terminal is None
    assert Terminal.connection.closed
