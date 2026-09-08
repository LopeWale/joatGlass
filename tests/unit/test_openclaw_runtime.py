"""Bounded reconnect and authoritative history recovery for Agent Terminal."""

from __future__ import annotations

import json
import asyncio
from collections import deque

import pytest

from joat.agents.openclaw_gateway import (
    OpenClawConnectionClosed,
    OpenClawGatewayError,
)
from joat.agents.openclaw_runtime import (
    make_brainstorm_terminal_agent,
    OpenClawHistoryRecovery,
    OpenClawRuntimeConfig,
    OpenClawTerminalAgent,
    terminal_ready_activity,
)
from joat.agents.openclaw_terminal import OpenClawToolsEnabled


@pytest.fixture
def capture_agent(monkeypatch: pytest.MonkeyPatch):
    """Real adapter lifecycle; the owned terminal only records exact input."""
    terminals = []

    class Terminal:
        def __init__(self, connection):
            self.connection = connection
            self.recovery = None
            self.received = []
            self.cleanup_before_close = False

        async def stream(self, prompt):
            self.received.append(prompt)
            try:
                yield "captured answer"
            finally:
                await asyncio.sleep(0)
                self.cleanup_before_close = not self.connection.closed

    async def create(**kwargs):
        terminal = Terminal(kwargs["connection"])
        terminals.append(terminal)
        return terminal

    monkeypatch.setattr("joat.agents.openclaw_runtime.OpenClawAgentTerminal.create", create)
    factory = RuntimeFactory([RuntimeConnection({}), RuntimeConnection({})])
    return OpenClawTerminalAgent(client=factory, agent_id="hermes"), factory, terminals


def capture_generation(agent):
    return agent.capture_context_generation


def captured_stream(agent, prompt, generation):
    return agent.stream_captured(prompt, context_generation=generation)


@pytest.mark.asyncio
async def test_captured_terminal_context_changes_after_reinitialization(capture_agent) -> None:
    agent, factory, terminals = capture_agent
    assert capture_generation(agent) is None
    await agent.initialize()
    original = capture_generation(agent)
    assert isinstance(original, int)
    assert [chunk async for chunk in agent.stream("explicit command")] == ["captured answer"]
    assert capture_generation(agent) == original
    await agent._invalidate_terminal()
    assert capture_generation(agent) is None
    await agent.initialize()
    try:
        assert capture_generation(agent) != original
        assert factory.calls == 2
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_captured_terminal_cannot_retarget_while_waiting_for_adapter_lock(capture_agent) -> None:
    agent, _factory, terminals = capture_agent
    await agent.initialize()
    original = capture_generation(agent)
    await agent._lock.acquire()

    async def consume():
        return [chunk async for chunk in captured_stream(agent, "  keep this?!\n  ", original)]

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # The real adapter is waiting for its lock.
    try:
        await agent._invalidate_terminal()
        await agent._initialize_locked()  # This actor already holds the adapter lock.
        agent._lock.release()
        with pytest.raises(OpenClawGatewayError, match="changed|unavailable"):
            await asyncio.wait_for(task, 1)
        assert all(terminal.received == [] for terminal in terminals)
        assert agent.initialized  # A stale request must not destroy its successor.
    finally:
        if agent._lock.locked():
            agent._lock.release()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await agent.close()


@pytest.mark.asyncio
async def test_captured_terminal_does_not_initialize_a_missing_context(capture_agent) -> None:
    agent, factory, _terminals = capture_agent
    try:
        with pytest.raises(OpenClawGatewayError, match="changed|unavailable"):
            _ = [chunk async for chunk in captured_stream(agent, "old captured instruction", 1)]
        assert factory.calls == 0
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_captured_terminal_preserves_literal_input_in_the_current_context(capture_agent) -> None:
    agent, factory, terminals = capture_agent
    await agent.initialize()
    generation = capture_generation(agent)
    text = "  Stop, open News?!\nKeep these exact words.  "
    try:
        assert [chunk async for chunk in captured_stream(agent, text, generation)] == ["captured answer"]
        assert terminals[0].received == [text]
        assert capture_generation(agent) == generation
        assert factory.calls == 1
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_captured_terminal_generator_close_invalidates_its_connection(capture_agent) -> None:
    agent, _factory, terminals = capture_agent
    await agent.initialize()
    chunks = captured_stream(agent, "literal input", capture_generation(agent))
    try:
        assert await anext(chunks) == "captured answer"
        await chunks.aclose()
        assert terminals[0].cleanup_before_close
        assert capture_generation(agent) is None
        assert terminals[0].connection.closed
    finally:
        await chunks.aclose()
        await agent.close()


@pytest.fixture
def held_initialization(monkeypatch: pytest.MonkeyPatch):
    started, release = asyncio.Event(), asyncio.Event()
    connection = RuntimeConnection({})
    factory = RuntimeFactory([connection, RuntimeConnection({})])

    class Terminal:
        recovery = None

        def __init__(self, owned_connection):
            self.connection = owned_connection

    async def create(**kwargs):
        started.set()
        await release.wait()
        return Terminal(kwargs["connection"])

    monkeypatch.setattr("joat.agents.openclaw_runtime.OpenClawAgentTerminal.create", create)
    return OpenClawTerminalAgent(client=factory, agent_id="hermes"), factory, connection, started, release


@pytest.mark.asyncio
async def test_captured_terminal_initialization_cannot_publish_after_close(held_initialization) -> None:
    agent, _factory, connection, started, release = held_initialization
    setup = asyncio.create_task(agent.initialize())
    await asyncio.wait_for(started.wait(), 1)
    closing = asyncio.create_task(agent.close())
    await asyncio.sleep(0)
    release.set()
    try:
        await asyncio.wait_for(closing, 1)
        await asyncio.gather(setup, return_exceptions=True)
        assert not agent.initialized
        assert capture_generation(agent) is None
        assert connection.closed
    finally:
        release.set()
        await asyncio.gather(setup, closing, return_exceptions=True)
        await agent.close()


@pytest.mark.asyncio
async def test_captured_terminal_close_cancels_held_initialization(held_initialization) -> None:
    agent, _factory, connection, started, release = held_initialization
    setup = asyncio.create_task(agent.initialize())
    await asyncio.wait_for(started.wait(), 1)
    try:
        await asyncio.wait_for(agent.close(), .2)
        assert setup.done()
        assert connection.closed
        assert capture_generation(agent) is None
    finally:
        release.set()
        await asyncio.gather(setup, return_exceptions=True)
        await agent.close()


@pytest.mark.asyncio
async def test_captured_terminal_concurrent_initialization_owns_one_connection(held_initialization) -> None:
    agent, factory, _connection, started, release = held_initialization
    first = asyncio.create_task(agent.initialize())
    await asyncio.wait_for(started.wait(), 1)
    second = asyncio.create_task(agent.initialize())
    await asyncio.sleep(0)
    release.set()
    try:
        await asyncio.wait_for(asyncio.gather(first, second), 1)
        assert factory.calls == 1
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
        await agent.close()


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


def test_terminal_ready_activity_distinguishes_first_setup_from_re_attestation() -> None:
    assert terminal_ready_activity(reverified=False) == (
        "Dedicated no-tool session ready. Tap to begin a Brainstorm."
    )
    assert terminal_ready_activity(reverified=True) == (
        "Dedicated no-tool session re-verified. Tap to begin a Brainstorm."
    )


def test_brainstorm_terminal_builds_distinct_write_and_read_only_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import joat.agents.openclaw_runtime as runtime
    from joat.agents.openclaw_gateway import GatewayProfile

    captured: list[dict[str, object]] = []

    class RecordingGatewayClient:
        def __init__(self, **kwargs: object) -> None:
            captured.append(kwargs)

    class Credentials:
        device_id = "device-1"
        public_key = "public-key"

        async def sign(self, _payload: str) -> str:
            return "signature"

        async def load(self, *, device_id: str, role: str) -> object:
            return None

        async def store(
            self,
            *,
            device_id: str,
            role: str,
            token: str,
            scopes: tuple[str, ...],
        ) -> None:
            return None

    monkeypatch.setattr(runtime, "OpenClawGatewayClient", RecordingGatewayClient)

    agent = make_brainstorm_terminal_agent(
        profile=GatewayProfile("wss://example.ts.net"),
        credentials=Credentials(),
        agent_id="hermes",
    )

    assert len(captured) == 2
    assert captured[0]["scopes"] == ("operator.read", "operator.write")
    assert captured[0]["capabilities"] == ("tool-events",)
    assert captured[1]["scopes"] == ("operator.read",)
    assert "capabilities" not in captured[1]
    assert agent.attestation_client is not None


class RuntimeConnection:
    def __init__(
        self,
        responses: dict[str, object],
        *,
        granted_scopes: tuple[str, ...] = ("operator.read", "operator.write"),
    ) -> None:
        self.advertised_methods = METHODS
        self.advertised_events = EVENTS
        self.granted_scopes = granted_scopes
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
            "sessions.subscribe": {
                "subscribed": True,
                "list": {
                    "sessions": [
                        {
                            "key": "agent:hermes:joat-123",
                            "sessionId": "session-1",
                            "label": "JOAT Agent Terminal",
                        },
                    ],
                },
            },
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
    assert reconnected.calls[0] == (
        "sessions.subscribe",
        {"label": "JOAT Agent Terminal", "agentId": "hermes", "limit": 2},
    )


@pytest.mark.asyncio
async def test_recovery_requires_a_filtered_catalog_snapshot_after_reconnect() -> None:
    reconnected = recovery_connection(
        {
            "messages": [],
            "sessionInfo": {"hasActiveRun": True, "activeRunIds": ["run-1"]},
            "inFlightRun": {"runId": "run-1", "text": "partial"},
        }
    )
    reconnected.responses["sessions.subscribe"] = {"subscribed": True}
    recovery = OpenClawHistoryRecovery(
        client=RuntimeFactory([reconnected]),
        attempts=1,
        backoff_s=(0.0,),
    )

    with pytest.raises(OpenClawGatewayError, match="catalog snapshot"):
        await recovery.recover(
            connection=RuntimeConnection({}),
            agent_id="hermes",
            session_key="agent:hermes:joat-123",
            run_id="run-1",
            prompt="question",
        )

    assert reconnected.closed
    assert reconnected.calls == [
        (
            "sessions.subscribe",
            {"label": "JOAT Agent Terminal", "agentId": "hermes", "limit": 2},
        ),
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
async def test_terminal_agent_re_attests_over_a_fresh_read_only_connection() -> None:
    initialized = RuntimeConnection(
        {
            "sessions.list": {
                "sessions": [{
                    "key": "agent:hermes:joat-123",
                    "sessionId": "session-1",
                    "label": "JOAT Agent Terminal",
                }],
            },
            "sessions.describe": {
                "session": {
                    "key": "agent:hermes:joat-123",
                    "sessionId": "session-1",
                    "label": "JOAT Agent Terminal",
                    "agentId": "hermes",
                },
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
    attestation = RuntimeConnection(
        {
            "sessions.list": {
                "sessions": [{
                    "key": "agent:hermes:joat-123",
                    "sessionId": "session-1",
                    "label": "JOAT Agent Terminal",
                }],
            },
            "sessions.describe": {
                "session": {
                    "key": "agent:hermes:joat-123",
                    "sessionId": "session-1",
                    "label": "JOAT Agent Terminal",
                    "agentId": "hermes",
                },
            },
            "tools.effective": {
                "agentId": "hermes",
                "groups": [{"id": "core", "tools": []}],
                "notices": [],
            },
        },
        granted_scopes=("operator.read",),
    )
    terminal_factory = RuntimeFactory([initialized])
    attestation_factory = RuntimeFactory([attestation])
    agent = OpenClawTerminalAgent(
        client=terminal_factory,
        agent_id="hermes",
        attestation_client=attestation_factory,
    )

    await agent.initialize()
    await agent.attest_existing()

    assert terminal_factory.calls == 1
    assert attestation_factory.calls == 1
    assert attestation.closed
    assert [method for method, _ in attestation.calls] == [
        "sessions.list",
        "sessions.describe",
        "tools.effective",
    ]


@pytest.mark.asyncio
async def test_terminal_agent_never_falls_back_to_write_client_for_re_attestation() -> None:
    initialized = RuntimeConnection(
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
                },
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
    terminal_factory = RuntimeFactory([initialized])
    agent = OpenClawTerminalAgent(client=terminal_factory, agent_id="hermes")

    await agent.initialize()
    with pytest.raises(OpenClawGatewayError, match="read-only attestation client"):
        await agent.attest_existing()

    assert terminal_factory.calls == 1


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
@pytest.mark.parametrize("captured", [False, True])
async def test_terminal_agent_close_cancels_active_stream_before_returning(captured: bool) -> None:
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
        chunks = (captured_stream(agent, "question", capture_generation(agent))
                  if captured else agent.stream("question"))
        return [chunk async for chunk in chunks]

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


class TestAnsweringAnApprovalDuringItsOwnRun:
    """The audit's AT-10 blocker, reproduced at the lock.

    Approvals are raised from inside a run's event loop, the gateway holds
    that run open until the approval is answered, and stream() holds the
    agent lock across the entire run — so resolve_approval, which took the
    same lock, waited on a holder that was waiting on this very answer.
    "terminal approve" parked the voice pipeline forever. The connection
    multiplexes by request id, so answering beside the live stream is the
    transport's intended use.
    """

    class _LiveTerminal:
        """A run in flight: first chunk arrives, then the run waits —
        exactly the state a pending approval puts a session in."""

        def __init__(self) -> None:
            self.release = asyncio.Event()
            self.answered: list[tuple[str, str, str]] = []

        async def stream(self, prompt: str):
            yield "working…"
            await self.release.wait()
            yield "done"

        async def resolve_approval(
            self, approval_id: str, kind: str, decision: str
        ) -> str:
            self.answered.append((approval_id, kind, decision))
            return decision

        async def close(self) -> None:
            return None

    @pytest.mark.asyncio
    async def test_the_answer_lands_while_the_run_holds_the_lock(self) -> None:
        agent = OpenClawTerminalAgent(client=RuntimeFactory([]), agent_id="hermes")
        terminal = self._LiveTerminal()
        agent._terminal = terminal  # a live, subscribed terminal mid-run

        first_chunk = asyncio.Event()
        run_done = asyncio.Event()

        async def run() -> None:
            async for chunk in agent.stream("do the work"):
                if chunk == "working…":
                    first_chunk.set()
            run_done.set()

        task = asyncio.get_running_loop().create_task(run())
        await asyncio.wait_for(first_chunk.wait(), 2.0)

        # The run is in flight and the agent lock is held. Before the fix
        # this wait_for timed out — the deadlock, surfaced as a hang.
        recorded = await asyncio.wait_for(
            agent.resolve_approval("appr-1", "exec", "approve"), timeout=1.0
        )

        assert recorded == "approve"
        assert terminal.answered == [("appr-1", "exec", "approve")]
        terminal.release.set()
        await asyncio.wait_for(run_done.wait(), 2.0)
        task.cancel()


class RunOutcomeConnection(RuntimeConnection):
    """Real adapter/terminal lifecycle; only gateway RPC and events are synthetic."""

    def __init__(self, outcome: dict[str, object]) -> None:
        key = "agent:hermes:joat-123"
        super().__init__({
            "sessions.list": {"sessions": []},
            "sessions.create": {
                "ok": True, "key": key, "sessionId": "session-1",
                "entry": {"sessionId": "session-1", "label": "JOAT Agent Terminal"},
                "runStarted": False,
            },
            "sessions.describe": {"session": {
                "key": key, "sessionId": "session-1", "label": "JOAT Agent Terminal",
                "agentId": "hermes",
            }},
            "tools.effective": {"agentId": "hermes", "groups": [{"id": "core", "tools": []}], "notices": []},
            "sessions.messages.subscribe": {"subscribed": True, "key": key},
            "sessions.send": deque([{"runId": "run-1"}, {"runId": "run-2"}]),
            "sessions.abort": {"ok": True, "abortedRunId": "run-1", "status": "aborted"},
        })
        self.events: deque[dict[str, object]] = deque([
            {"event": "chat", "payload": {
                "runId": "run-1", "sessionKey": key, "seq": 1, **outcome,
            }},
            {"event": "chat", "payload": {
                "runId": "run-2", "sessionKey": key, "seq": 1, "state": "final",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "second run completed"}]},
            }},
        ])

    async def next_event(self) -> dict[str, object]:
        assert not self.closed, "runtime reused a closed connection"
        return self.events.popleft()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [
    {"state": "error", "errorKind": "rate_limit", "errorMessage": "PRIVATE_PROVIDER_PAYLOAD"},
    {"state": "error"},
    {"state": "aborted", "stopReason": "restart"},
])
async def test_run_outcome_keeps_ready_context_for_a_followup_captured_run(outcome: dict[str, object]) -> None:
    connection = RunOutcomeConnection(outcome)
    factory = RuntimeFactory([connection])
    states: list[tuple[str, str]] = []
    agent = OpenClawTerminalAgent(client=factory, agent_id="hermes",
                                 state_changed=lambda state, reason: states.append((state, reason)))
    try:
        await agent.initialize()
        terminal = agent._terminal
        generation = agent.capture_context_generation
        assert generation is not None
        states.clear()
        with pytest.raises(OpenClawGatewayError) as failed:
            _ = [chunk async for chunk in agent.stream_captured("synthetic first", context_generation=generation)]
        assert agent.initialized
        assert agent._terminal is terminal
        assert agent.capture_context_generation == generation
        assert not connection.closed
        assert states == [("running", ""), ("ready", "")]
        assert "PRIVATE_" not in str(failed.value)

        assert [chunk async for chunk in agent.stream_captured("synthetic second", context_generation=generation)] == ["second run completed"]
        assert factory.calls == 1
        assert [method for method, _ in connection.calls].count("sessions.create") == 1
        assert not any(method == "sessions.abort" for method, _ in connection.calls)
    finally:
        await agent.close()
    assert connection.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    {"state": "error", "errorKind": "PRIVATE_NEW_KIND"},
    {"state": "error", "errorMessage": {"private": "synthetic secret"}},
    {"state": "error", "stopReason": False},
    # An unknown state token is no longer a protocol failure: the live
    # gateway sends informational states ("status", captured 2026-09-05)
    # mid-run, so the adapter skips them — see the status-event tests in
    # test_openclaw_terminal.py.
])
async def test_run_outcome_malformed_event_still_invalidates_runtime(invalid: dict[str, object]) -> None:
    connection = RunOutcomeConnection(invalid)
    states: list[tuple[str, str]] = []
    agent = OpenClawTerminalAgent(client=RuntimeFactory([connection]), agent_id="hermes",
                                 state_changed=lambda state, reason: states.append((state, reason)))
    try:
        await agent.initialize()
        with pytest.raises(OpenClawGatewayError) as failed:
            _ = [chunk async for chunk in agent.stream("synthetic instruction")]
        assert agent.capture_context_generation is None
        assert not agent.initialized
        assert connection.closed
        assert states[-1] == ("blocked", "protocol_rejected")
        assert "PRIVATE_" not in str(failed.value)
    finally:
        await agent.close()


def _write(path, payload: str):
    path.write_text(payload)
    path.chmod(0o600)
    return path


def test_multi_host_config_labels_each_mac_from_its_own_gateway(tmp_path) -> None:
    path = _write(tmp_path / "openclaw.json", json.dumps({"hosts": [
        {"gateway_url": "wss://admins-mac-mini.tail3a3363.ts.net", "agent_id": "hermes"},
        {"gateway_url": "wss://emmanuels-macbook-pro.tail3a3363.ts.net", "agent_id": "hermes"},
    ]}))

    configs = OpenClawRuntimeConfig.load_all(path)

    # Tailscale already named these machines; the first DNS label is what
    # the wearer calls them, and the tailnet suffix would eat the HUD row.
    assert [config.host for config in configs] == [
        "admins-mac-mini", "emmanuels-macbook-pro",
    ]


def test_a_single_configured_host_carries_no_label_in_either_shape(tmp_path) -> None:
    listed = OpenClawRuntimeConfig.load_all(_write(
        tmp_path / "listed.json",
        json.dumps({"hosts": [
            {"gateway_url": "wss://only.ts.net", "agent_id": "hermes"},
        ]}),
    ))
    plain = OpenClawRuntimeConfig.load_all(_write(
        tmp_path / "plain.json",
        '{"gateway_url":"wss://only.ts.net","agent_id":"hermes"}',
    ))

    # One Mac is a single-host install however it was spelled: the list
    # form must not start printing a host on every row.
    assert [config.host for config in listed] == [""]
    assert [config.host for config in plain] == [""]


def test_the_dashboard_port_stays_refused_on_every_host(tmp_path) -> None:
    path = _write(tmp_path / "openclaw.json", json.dumps({"hosts": [
        {"gateway_url": "wss://good.ts.net", "agent_id": "hermes"},
        {"gateway_url": "wss://admins-mac-mini.tail3a3363.ts.net:9443", "agent_id": "hermes"},
    ]}))

    # The Hermes control dashboard is off-limits to JOAT. A second host
    # entry is not a way around that.
    with pytest.raises(ValueError, match="Gateway"):
        OpenClawRuntimeConfig.load_all(path)


@pytest.mark.parametrize("payload", [
    '{"hosts":[]}',
    '{"hosts":{}}',
    '{"hosts":[{"gateway_url":"wss://a.ts.net","agent_id":"hermes","token":"s"}]}',
    '{"hosts":[{"gateway_url":"wss://a.ts.net","agent_id":"main"}]}',
    '{"hosts":[{"gateway_url":"wss://a.ts.net","agent_id":"hermes"}],"agent_id":"hermes"}',
])
def test_multi_host_config_rejects_malformed_shapes(tmp_path, payload: str) -> None:
    with pytest.raises(ValueError):
        OpenClawRuntimeConfig.load_all(_write(tmp_path / "openclaw.json", payload))


def test_a_repeated_gateway_is_refused_rather_than_shown_twice(tmp_path) -> None:
    path = _write(tmp_path / "openclaw.json", json.dumps({"hosts": [
        {"gateway_url": "wss://same.ts.net", "agent_id": "hermes"},
        {"gateway_url": "wss://same.ts.net", "agent_id": "hermes"},
    ]}))

    # Two entries for one Mac would paint two identical rows the wearer
    # cannot tell apart, and the host label could not separate them.
    with pytest.raises(ValueError, match="repeats a gateway"):
        OpenClawRuntimeConfig.load_all(path)


def test_load_still_answers_single_host_callers_from_a_list(tmp_path) -> None:
    path = _write(tmp_path / "openclaw.json", json.dumps({"hosts": [
        {"gateway_url": "wss://first.ts.net", "agent_id": "hermes"},
        {"gateway_url": "wss://second.ts.net", "agent_id": "hermes"},
    ]}))

    config = OpenClawRuntimeConfig.load(path)

    assert config is not None and config.profile.url == "wss://first.ts.net"


def test_a_missing_config_is_no_hosts_not_an_error(tmp_path) -> None:
    assert OpenClawRuntimeConfig.load_all(tmp_path / "absent.json") == []
