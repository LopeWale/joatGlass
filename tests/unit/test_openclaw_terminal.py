"""Fail-closed Agent Terminal session contracts."""

from __future__ import annotations

import asyncio
import logging
from collections import deque

import pytest

from joat.agents.openclaw_gateway import (
    BRAINSTORM_TERMINAL_POLICY,
    CALENDAR_PROPOSAL_TERMINAL_POLICY,
    AgentTerminalPolicy,
    OpenClawEventSequenceGap,
    OpenClawGatewayError,
)
from joat.agents.openclaw_terminal import (
    OpenClawAgentTerminal,
    OpenClawRunFailed,
    OpenClawToolInventoryUnsettled,
    OpenClawToolsEnabled,
    RecoveredRun,
)


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
        granted_scopes: tuple[str, ...] = ("operator.read", "operator.write"),
    ) -> None:
        self.advertised_methods = REQUIRED_METHODS
        self.advertised_events = frozenset({"agent", "chat", "session.message"})
        self.granted_scopes = granted_scopes
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
async def test_cancel_is_not_reported_until_gateway_confirms_the_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "joat.agents.openclaw_terminal.ABORT_CONFIRMATION_TIMEOUT_S",
        0.01,
    )

    class BlockingConnection(FakeConnection):
        async def next_event(self) -> dict[str, object]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    connection = BlockingConnection(hang_abort=True)
    terminal = await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")

    async def consume() -> list[str]:
        return [chunk async for chunk in terminal.stream("question")]

    task = asyncio.create_task(consume())
    while not any(method == "sessions.send" for method, _ in connection.calls):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(OpenClawGatewayError, match="did not confirm"):
        await task

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
async def test_terminal_read_only_attestation_rechecks_existing_dedicated_session() -> None:
    connection = FakeConnection(
        existing_session=True,
        granted_scopes=("operator.read",),
    )

    await OpenClawAgentTerminal.attest_existing(
        connection=connection,
        agent_id="hermes",
    )

    assert [method for method, _ in connection.calls] == [
        "sessions.list",
        "sessions.describe",
        "tools.effective",
    ]
    assert not {
        "sessions.create",
        "sessions.messages.subscribe",
        "sessions.send",
        "chat.history",
    } & {method for method, _ in connection.calls}


@pytest.mark.asyncio
async def test_terminal_read_only_attestation_rejects_write_capable_connection() -> None:
    connection = FakeConnection(existing_session=True)

    with pytest.raises(OpenClawGatewayError, match="exact read-only scopes"):
        await OpenClawAgentTerminal.attest_existing(
            connection=connection,
            agent_id="hermes",
        )

    assert connection.calls == []


@pytest.mark.asyncio
async def test_terminal_read_only_attestation_requires_one_existing_session() -> None:
    connection = FakeConnection(granted_scopes=("operator.read",))

    with pytest.raises(OpenClawGatewayError, match="one dedicated Agent Terminal session"):
        await OpenClawAgentTerminal.attest_existing(
            connection=connection,
            agent_id="hermes",
        )

    assert [method for method, _ in connection.calls] == ["sessions.list"]


@pytest.mark.asyncio
async def test_terminal_read_only_attestation_rejects_delivery_route() -> None:
    connection = FakeConnection(
        existing_session=True,
        granted_scopes=("operator.read",),
        session_route={
            "channel": "telegram",
            "deliveryContext": {"channel": "telegram", "to": "8245150068"},
        },
    )

    with pytest.raises(OpenClawGatewayError, match="isolated from delivery routes"):
        await OpenClawAgentTerminal.attest_existing(
            connection=connection,
            agent_id="hermes",
        )

    assert [method for method, _ in connection.calls] == [
        "sessions.list",
        "sessions.describe",
    ]


@pytest.mark.asyncio
async def test_terminal_read_only_attestation_rejects_enabled_tools_without_subscribing() -> None:
    connection = FakeConnection(
        existing_session=True,
        granted_scopes=("operator.read",),
        tools=[{"name": "calendar.write"}],
    )

    with pytest.raises(OpenClawToolsEnabled, match="tools are enabled"):
        await OpenClawAgentTerminal.attest_existing(
            connection=connection,
            agent_id="hermes",
        )

    assert [method for method, _ in connection.calls] == [
        "sessions.list",
        "sessions.describe",
        "tools.effective",
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
@pytest.mark.parametrize("notice_id,severity", [
    ("browser-filtered-by-profile", "info"),
    ("browser-denied-by-policy", "info"),
    ("browser-plugin-not-allowed", "warning"),
])
async def test_terminal_settled_browser_exclusion_allows_empty_inventory(
    notice_id: str, severity: str,
) -> None:
    class EmptyInventoryConnection(FakeConnection):
        async def request(self, method: str, params: dict[str, object]) -> object:
            result = await super().request(method, params)
            if method == "tools.effective":
                return {
                    "agentId": "hermes", "profile": "minimal", "groups": [],
                    "notices": [{
                        "id": notice_id, "severity": severity,
                        "message": "Browser is configured but excluded from this session.",
                    }],
                }
            return result

    connection = EmptyInventoryConnection(existing_session=True)
    terminal = await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")

    assert terminal.session_id == "session-1"
    assert [method for method, _ in connection.calls] == [
        "sessions.list", "sessions.describe", "tools.effective", "sessions.messages.subscribe",
    ]


@pytest.mark.asyncio
async def test_terminal_settled_browser_exclusion_does_not_hide_enabled_tools() -> None:
    connection = FakeConnection(
        existing_session=True,
        tools=[{"id": "exec", "source": "core"}],
        notices=[{
            "id": "browser-filtered-by-profile", "severity": "info",
            "message": "Browser is excluded, but other tools may still exist.",
        }],
    )
    with pytest.raises(OpenClawToolsEnabled, match="tools are enabled"):
        await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")
    assert connection.calls[-1][0] == "tools.effective"


@pytest.mark.asyncio
async def test_terminal_settled_browser_exclusion_mixed_with_mcp_remains_blocked() -> None:
    connection = FakeConnection(
        existing_session=True,
        notices=[
            {"id": "browser-filtered-by-profile", "severity": "info", "message": "Excluded."},
            {"id": "mcp-not-yet-connected", "severity": "info", "message": "Not discovered."},
        ],
    )
    with pytest.raises(OpenClawToolInventoryUnsettled):
        await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")
    assert connection.calls[-1][0] == "tools.effective"


@pytest.mark.asyncio
@pytest.mark.parametrize("notice", [
    {"id": "browser-filtered-by-profile", "severity": "warning", "message": "Excluded."},
    {"id": "browser-plugin-not-allowed", "severity": "info", "message": "Excluded."},
    {"id": "browser-filtered-by-profile", "message": "Excluded."},
    {"id": "browser-filtered-by-profile", "severity": "info"},
    {"id": "browser-filtered-by-profile", "severity": "info", "message": None},
    {"id": "browser-filtered-by-profile", "severity": "info", "message": " \n"},
    {"id": "unknown-notice", "severity": "info", "message": "Excluded."},
    {"id": "unsupported-tool-schema:exec", "severity": "warning", "message": "Quarantined."},
])
async def test_terminal_settled_browser_exclusion_rejects_unproven_notice(
    notice: dict[str, object],
) -> None:
    connection = FakeConnection(existing_session=True, notices=[notice])
    with pytest.raises(OpenClawToolInventoryUnsettled):
        await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")
    assert connection.calls[-1][0] == "tools.effective"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_group", [{"tools": None}, {"tools": "private"}, {}])
async def test_terminal_settled_browser_exclusion_still_validates_every_group(
    invalid_group: dict[str, object],
) -> None:
    class MalformedInventoryConnection(FakeConnection):
        async def request(self, method: str, params: dict[str, object]) -> object:
            result = await super().request(method, params)
            if method == "tools.effective":
                return {
                    "agentId": "hermes", "groups": [{"tools": []}, invalid_group],
                    "notices": [{
                        "id": "browser-filtered-by-profile", "severity": "info", "message": "Excluded.",
                    }],
                }
            return result

    connection = MalformedInventoryConnection(existing_session=True)
    with pytest.raises(OpenClawGatewayError, match="invalid effective tool inventory"):
        await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")
    assert connection.calls[-1][0] == "tools.effective"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [
    "mcp-not-yet-connected", "mcp-not-yet-listed", "mcp-stale-catalog",
    "browser-denied-by-policy", "browser-filtered-by-profile", "browser-plugin-not-allowed",
])
async def test_tool_inventory_diagnostic_identifies_known_notice_on_blocked_inventory(
    code: str, caplog: pytest.LogCaptureFixture,
) -> None:
    # A valid settled exclusion alone is admissible; an unknown companion
    # remains blocked and diagnostics must identify every known notice.
    connection = FakeConnection(notices=[
        {
            "id": code,
            "severity": "warning" if code == "browser-plugin-not-allowed" else "info",
            "message": "Synthetic inventory notice",
        },
        {"id": "future-notice", "severity": "info", "message": "Unknown inventory state."},
    ])

    with caplog.at_level(logging.WARNING, logger="joat.agents.openclaw_terminal"):
        with pytest.raises(OpenClawToolInventoryUnsettled, match="inventory is unsettled"):
            await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")

    assert caplog.messages == [
        f"OpenClaw tool inventory unsettled notice_count=2 codes={code} "
        "unknown_notices=1 malformed_notices=0 groups=1 tool_entries=0 malformed_groups=0"
    ]
    assert [method for method, _ in connection.calls] == [
        "sessions.list", "sessions.create", "sessions.describe", "tools.effective",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("notice_id,expected,unknown", [
    ("unsupported-tool-schema:private-tool bearer-secret\nFAKE LOG", "unsupported-tool-schema", 0),
    ("unsupported-tool-schema:", "none", 1),
    ("unsupported-tool-schema-private-tool", "none", 1),
])
async def test_tool_inventory_diagnostic_schema_family_never_logs_tool_name(
    notice_id: str, expected: str, unknown: int, caplog: pytest.LogCaptureFixture,
) -> None:
    # Both core/plugin and MCP inventory builders use this exact ID prefix,
    # followed by the private tool name. It is not a fixed literal ID.
    connection = FakeConnection(notices=[{
        "id": notice_id, "severity": "warning", "message": "private-tool bearer-secret",
    }])
    with caplog.at_level(logging.WARNING, logger="joat.agents.openclaw_terminal"):
        with pytest.raises(OpenClawToolInventoryUnsettled, match="inventory is unsettled"):
            await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")
    assert caplog.messages == [
        f"OpenClaw tool inventory unsettled notice_count=1 codes={expected} "
        f"unknown_notices={unknown} malformed_notices=0 groups=1 tool_entries=0 malformed_groups=0"
    ]
    for marker in ("private-tool", "bearer-secret", "FAKE LOG"):
        assert marker not in repr(caplog.record_tuples)
        assert marker not in repr([record.args for record in caplog.records])
    assert connection.calls[-1][0] == "tools.effective"


@pytest.mark.asyncio
async def test_tool_inventory_diagnostic_redacts_hostile_and_private_payloads(
    caplog: pytest.LogCaptureFixture,
) -> None:
    private = "private-server bearer-secret\nFAKE LOG: ready"

    class PrivateInventoryConnection(FakeConnection):
        async def request(self, method: str, params: dict[str, object]) -> object:
            result = await super().request(method, params)
            if method == "tools.effective":
                return {
                    "agentId": "hermes",
                    "sessionKey": private,
                    "notices": [
                        {"id": "mcp-not-yet-listed", "severity": "info", "message": private,
                         "servers": [private]},
                        {"id": "mcp-not-yet-listed", "details": {"token": private}},
                        {"id": private}, {"id": {"token": private}},
                        {"source": private}, private, None,
                    ],
                    "groups": [
                        {"label": private, "tools": [{"name": private}, {"token": private}]},
                        {"id": private, "tools": []},
                        {"id": private, "tools": private},
                    ],
                }
            return result

    connection = PrivateInventoryConnection()
    with caplog.at_level(logging.WARNING, logger="joat.agents.openclaw_terminal"):
        with pytest.raises(OpenClawToolInventoryUnsettled, match="inventory is unsettled"):
            await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")

    assert caplog.messages == [
        "OpenClaw tool inventory unsettled notice_count=7 codes=mcp-not-yet-listed "
        "unknown_notices=1 malformed_notices=4 groups=3 tool_entries=2 malformed_groups=1"
    ]
    # Check logging arguments as well: formatting/redaction must not merely
    # hide secret-bearing values that a different handler could serialize.
    for marker in ("private-server", "bearer-secret", "FAKE LOG"):
        assert marker not in repr(caplog.record_tuples)
        assert marker not in repr([record.args for record in caplog.records])
    assert [method for method, _ in connection.calls] == [
        "sessions.list", "sessions.create", "sessions.describe", "tools.effective",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("notices", [None, []])
async def test_tool_inventory_diagnostic_is_silent_for_settled_inventory(
    notices: list[dict[str, object]] | None, caplog: pytest.LogCaptureFixture,
) -> None:
    connection = FakeConnection(notices=notices)
    with caplog.at_level(logging.WARNING, logger="joat.agents.openclaw_terminal"):
        await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")
    assert caplog.messages == []


@pytest.mark.asyncio
async def test_tool_inventory_diagnostic_does_not_infer_notice_id_from_other_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    connection = FakeConnection(notices=[{
        "code": "mcp-not-yet-connected", "kind": "mcp-not-yet-connected",
        "message": "mcp-not-yet-connected",
    }])
    with caplog.at_level(logging.WARNING, logger="joat.agents.openclaw_terminal"):
        with pytest.raises(OpenClawToolInventoryUnsettled, match="inventory is unsettled"):
            await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")
    assert caplog.messages == [
        "OpenClaw tool inventory unsettled notice_count=1 codes=none "
        "unknown_notices=0 malformed_notices=1 groups=1 tool_entries=0 malformed_groups=0"
    ]


@pytest.mark.asyncio
async def test_tool_inventory_diagnostic_failure_preserves_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from joat.agents import openclaw_terminal

    def broken_handler(*args: object) -> None:
        raise RuntimeError("diagnostic handler unavailable")

    monkeypatch.setattr(openclaw_terminal.log, "warning", broken_handler)
    connection = FakeConnection(notices=[{"id": "mcp-stale-catalog"}])
    with pytest.raises(OpenClawToolInventoryUnsettled, match="inventory is unsettled"):
        await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")
    assert connection.calls[-1][0] == "tools.effective"


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


class TestAResumedSessionCanActuallyBeUsed:
    """The audit's AT-03/AT-04 blocker: stream() ran the Brainstorm-grade
    isolation preflight on EVERY session — including one bound to an
    existing listed session via attach(). An attached session is someone's
    real work: its own label, its own lineage, legitimately has tools. The
    describe never matched, so every send into a resumed session was
    refused by JOAT's own policy layer and "resume then steer" was dead.

    What protects the wearer on the attach path is the gateway's
    per-connection earned-key enforcement plus the approval gate — not an
    isolation assert that can only ever describe the lane's own session.
    """

    @staticmethod
    def _work_connection() -> FakeConnection:
        from joat.agents.openclaw_gateway import WORK_TERMINAL_POLICY

        connection = FakeConnection(policy=WORK_TERMINAL_POLICY)
        connection.granted_scopes = (
            "operator.read", "operator.write", "operator.approvals",
        )
        connection.advertised_methods = REQUIRED_METHODS | {"approval.resolve"}
        connection.advertised_events = (
            connection.advertised_events | {"session.approval"}
        )
        # The fake echoes back whatever key it was configured with; an
        # attach subscribes to the FOREIGN key, so echo that one.
        connection.subscription_key = "agent:other:their-work-1"
        return connection

    @pytest.mark.asyncio
    async def test_attach_streams_without_the_isolation_preflight(self) -> None:
        from joat.agents.openclaw_gateway import WORK_TERMINAL_POLICY

        connection = self._work_connection()
        connection.events = deque([
            {"event": "chat", "payload": {
                "runId": "run-1", "sessionKey": "agent:other:their-work-1",
                "seq": 1, "state": "final",
                "message": {"role": "assistant",
                            "content": [{"type": "text", "text": "done"}]}}},
        ])
        terminal = await OpenClawAgentTerminal.attach(
            connection=connection,
            agent_id="hermes",
            session_key="agent:other:their-work-1",
            policy=WORK_TERMINAL_POLICY,
        )
        assert terminal.attached is True

        chunks = [chunk async for chunk in terminal.stream("carry on")]

        assert chunks == ["done"]
        methods = [method for method, _ in connection.calls]
        # The send went straight out; no describe, no tools inventory —
        # the preflights that refused every resumed-session send.
        assert "sessions.send" in methods
        assert "sessions.describe" not in methods
        assert "tools.effective" not in methods

    @pytest.mark.asyncio
    async def test_a_created_terminal_still_runs_both_preflights(self) -> None:
        """The gate is scoped to attach, not removed: the lane's own
        dedicated session keeps its Brainstorm-grade tripwire."""
        connection = FakeConnection()
        terminal = await OpenClawAgentTerminal.create(
            connection=connection, agent_id="hermes",
        )
        assert terminal.attached is False
        connection.calls.clear()
        connection.events = deque([
            {"event": "chat", "payload": {
                "runId": "run-1", "sessionKey": terminal.session_key,
                "seq": 1, "state": "final",
                "message": {"role": "assistant",
                            "content": [{"type": "text", "text": "ok"}]}}},
        ])

        _ = [chunk async for chunk in terminal.stream("hello")]

        methods = [method for method, _ in connection.calls]
        assert "sessions.describe" in methods
        assert "tools.effective" in methods


@pytest.mark.asyncio
@pytest.mark.parametrize(("state", "kind", "stop", "expected"), [
    ("error", "refusal", None, "declined"),
    ("error", "timeout", None, "timed out"),
    ("error", "rate_limit", None, "rate limit"),
    ("error", "context_length", None, "context"),
    ("error", "unknown", None, "run failed"),
    ("error", None, "timeout", "timed out"),
    ("error", None, None, "run failed"),
    ("aborted", None, None, "stopped"),
    ("aborted", None, "restart", "restart"),
    ("aborted", None, "rpc", "cancelled"),
])
async def test_run_outcome_reports_safe_reason_and_allows_next_run(
    state: str, kind: str | None, stop: str | None, expected: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private = "PRIVATE_PROVIDER_TOKEN=synthetic-secret PRIVATE_INSTRUCTION=synthetic-body"
    payload: dict[str, object] = {
        "runId": "run-1", "sessionKey": "agent:hermes:joat-123", "seq": 1,
        "state": state, "errorMessage": private,
        "message": {"role": "assistant", "content": [{"type": "text", "text": private}]},
    }
    if kind is not None:
        payload["errorKind"] = kind
    if stop is not None:
        payload["stopReason"] = stop
    connection = FakeConnection(events=[{"event": "chat", "payload": payload}])
    terminal = await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")

    with caplog.at_level(logging.WARNING), pytest.raises(OpenClawGatewayError) as failed:
        _ = [chunk async for chunk in terminal.stream("synthetic first instruction")]

    assert expected in str(failed.value)
    assert isinstance(failed.value, OpenClawRunFailed)
    assert private not in str(failed.value)
    assert "PRIVATE_" not in caplog.text
    assert f"run ended state={state} kind={kind or 'unknown'} stop={stop or 'unknown'}" in caplog.text
    assert not any(method == "sessions.abort" for method, _ in connection.calls)
    connection.run_id = "run-2"
    connection.events.append({"event": "chat", "payload": {
        "runId": "run-2", "sessionKey": terminal.session_key,
        "seq": 1, "state": "final", "message": {
            "role": "assistant", "content": [{"type": "text", "text": "next run completed"}],
        },
    }})
    assert [chunk async for chunk in terminal.stream("synthetic second instruction")] == ["next run completed"]
    assert terminal.connection is connection
    assert [params["message"] for method, params in connection.calls if method == "sessions.send"] == [
        "synthetic first instruction", "synthetic second instruction",
    ]


@pytest.mark.asyncio
async def test_run_outcome_unknown_freeform_stop_reason_is_never_echoed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    private = "PRIVATE_STOP_REASON with a synthetic secret"
    connection = FakeConnection(events=[{"event": "chat", "payload": {
        "runId": "run-1", "sessionKey": "agent:hermes:joat-123", "seq": 1,
        "state": "aborted", "stopReason": private, "errorMessage": private,
    }}])
    terminal = await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")
    with caplog.at_level(logging.WARNING), pytest.raises(OpenClawGatewayError) as failed:
        _ = [chunk async for chunk in terminal.stream("synthetic instruction")]
    assert "stopped" in str(failed.value)
    assert private not in str(failed.value)
    assert "PRIVATE_" not in caplog.text
    assert "run ended state=aborted kind=unknown stop=unknown" in caplog.text
    assert not any(method == "sessions.abort" for method, _ in connection.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    {"errorKind": "PRIVATE_UNRECOGNIZED_KIND"},
    {"errorKind": None}, {"errorKind": 3}, {"errorKind": []},
    {"errorMessage": {"private": "synthetic secret"}},
    {"stopReason": {"private": "synthetic secret"}},
    {"state": "PRIVATE_UNRECOGNIZED_STATE"},
    {"seq": True},
])
async def test_run_outcome_malformed_fields_remain_protocol_failure(invalid: dict[str, object]) -> None:
    payload: dict[str, object] = {
        "runId": "run-1", "sessionKey": "agent:hermes:joat-123", "seq": 1,
        "state": "error", **invalid,
    }
    connection = FakeConnection(events=[{"event": "chat", "payload": payload}])
    terminal = await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")
    with pytest.raises(OpenClawGatewayError, match="invalid") as failed:
        _ = [chunk async for chunk in terminal.stream("synthetic instruction")]
    assert not isinstance(failed.value, OpenClawRunFailed)
    assert "PRIVATE_" not in str(failed.value)
    assert [method for method, _ in connection.calls].count("sessions.abort") == 1


@pytest.mark.asyncio
async def test_run_outcome_ignores_foreign_failures_and_preserves_own_partial_output() -> None:
    key = "agent:hermes:joat-123"
    connection = FakeConnection(events=[
        {"event": "chat", "payload": {"runId": "other-run", "sessionKey": key,
                                       "seq": 1, "state": "error", "errorKind": "timeout"}},
        {"event": "chat", "payload": {"runId": "run-1", "sessionKey": "other-session",
                                       "seq": 1, "state": "aborted"}},
        {"event": "chat", "payload": {"runId": "run-1", "sessionKey": key,
                                       "seq": 1, "state": "delta", "deltaText": "partial output"}},
        {"event": "chat", "payload": {"runId": "run-1", "sessionKey": key,
                                       "seq": 2, "state": "error", "errorKind": "timeout"}},
    ])
    terminal = await OpenClawAgentTerminal.create(connection=connection, agent_id="hermes")
    chunks: list[str] = []
    with pytest.raises(OpenClawGatewayError, match="timed out"):
        async for chunk in terminal.stream("synthetic instruction"):
            chunks.append(chunk)
    assert chunks == ["partial output"]
    assert not any(method == "sessions.abort" for method, _ in connection.calls)
