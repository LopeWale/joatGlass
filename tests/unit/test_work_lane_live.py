"""The Agent Terminal work lane against a REAL WebSocket gateway.

Everything else about this lane is checked at the policy layer — which
params are admitted, which are refused. That proves the trust boundary but
never proves the lane can hold a conversation: the roster read, the
approval arriving as an event, and the answer going back with the recorded
decision have only ever been exercised against hand-built fakes.

This stands up an actual websockets server speaking the protocol frames
OpenClaw uses and drives the shipping client through it. It is still not a
real OpenClaw — the deployment's own literals could differ — but every
byte between JOAT's code and the socket is production code.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

from joat.agents import openclaw_gateway as gateway
from joat.agents.openclaw_gateway import WORK_TERMINAL_POLICY
from joat.agents.openclaw_terminal import OpenClawAgentTerminal, PendingApproval

SESSION_KEY = WORK_TERMINAL_POLICY.new_session_key("ab12")
OTHER_KEY = "agent:hermes:someone-elses-lane"

METHODS = sorted({
    "tools.effective", "sessions.subscribe", "sessions.list", "sessions.create",
    "sessions.describe", "sessions.messages.subscribe", "sessions.send",
    "sessions.abort", "chat.history", "approval.resolve",
})
EVENTS = ["agent", "chat", "session.message", "session.approval"]


class RecordingIdentity:
    """Matches the shape the shipping client actually calls: sign() is
    awaited and public_key is an attribute, not a method."""

    device_id = "b" * 64
    public_key = "public-key-base64url"

    async def sign(self, payload: str) -> str:
        return "signature-base64url"


def hello_ok(frame_id: str) -> str:
    return json.dumps({
        "type": "res", "id": frame_id, "ok": True,
        "payload": {
            "type": "hello-ok", "protocol": 4,
            "server": {"version": "2026.8.28", "connId": "conn-live"},
            "features": {
                "methods": METHODS, "events": EVENTS, "capabilities": ["tool-events"],
            },
            "auth": {
                "role": "operator",
                "scopes": ["operator.read", "operator.write", "operator.approvals"],
            },
            # The client validates the server's declared limits, so a hello
            # without them is rejected before any method is sent.
            "policy": {
                "maxPayload": 1_048_576,
                "maxBufferedBytes": 4_194_304,
                "tickIntervalMs": 15_000,
            },
            "snapshot": {},
        },
    })


async def handshake(connection: ServerConnection) -> None:
    await connection.send(json.dumps({
        "type": "event", "event": "connect.challenge",
        "payload": {"nonce": "n", "ts": 1_777_777_777_010},
    }))
    frame = json.loads(await connection.recv())
    await connection.send(hello_ok(frame["id"]))


async def connect(port: int) -> Any:
    client = gateway.OpenClawGatewayClient(
        profile=gateway.GatewayProfile(f"ws://127.0.0.1:{port}"),
        identity=RecordingIdentity(),
        scopes=("operator.read", "operator.write", "operator.approvals"),
        capabilities=("tool-events",),
        policy=WORK_TERMINAL_POLICY,
    )
    return await client.connect()


class TestWorkLaneOverARealSocket:
    async def test_the_roster_read_crosses_the_wire(self) -> None:
        seen: list[dict[str, Any]] = []

        async def handler(connection: ServerConnection) -> None:
            await handshake(connection)
            frame = json.loads(await connection.recv())
            seen.append(frame)
            await connection.send(json.dumps({
                "type": "res", "id": frame["id"], "ok": True,
                "payload": {"sessions": [
                    {"key": OTHER_KEY, "label": "nightly build", "sessionId": "s1"},
                    # A non-string KEY disqualifies the row: the key is what
                    # identifies a session, so a row without one is
                    # unaddressable and dropped.
                    {"key": 42, "label": "unusable"},
                    # A bad LABEL is only a missing name — the session is
                    # still addressable, so it is kept and coerced.
                    {"key": "agent:hermes:unlabelled", "label": 42},
                    "not even a record",
                ]},
            }))

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            connection = await connect(port)
            terminal = OpenClawAgentTerminal(
                connection=connection, agent_id="hermes",
                session_key=SESSION_KEY, session_id="s",
                policy=WORK_TERMINAL_POLICY,
            )
            rows = await terminal.list_sessions(limit=20)
            await connection.close()

        assert seen[0]["method"] == "sessions.list"
        assert seen[0]["params"] == {
            "agentId": "hermes", "limit": 20, "involvingMe": True,
        }
        assert rows == [
            {"key": OTHER_KEY, "label": "nightly build", "sessionId": "s1"},
            {"key": "agent:hermes:unlabelled", "label": "", "sessionId": ""},
        ]

    async def test_an_approval_is_answered_with_the_recorded_decision(self) -> None:
        seen: list[dict[str, Any]] = []

        async def handler(connection: ServerConnection) -> None:
            await handshake(connection)
            frame = json.loads(await connection.recv())
            seen.append(frame)
            # First-answer-wins: the gateway records DENY even though this
            # caller asked to approve.
            await connection.send(json.dumps({
                "type": "res", "id": frame["id"], "ok": True,
                "payload": {"id": "appr-1", "decision": "deny"},
            }))

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            connection = await connect(port)
            terminal = OpenClawAgentTerminal(
                connection=connection, agent_id="hermes",
                session_key=SESSION_KEY, session_id="s",
                policy=WORK_TERMINAL_POLICY,
            )
            recorded = await terminal.resolve_approval("appr-1", "exec", "approve")
            await connection.close()

        assert seen[0]["method"] == "approval.resolve"
        assert seen[0]["params"] == {
            "id": "appr-1", "kind": "exec", "decision": "approve",
        }
        # The receipt is what the GATEWAY recorded, never what we sent.
        assert recorded == "deny"

    async def test_attaching_to_a_listed_session_subscribes_with_approvals(
        self,
    ) -> None:
        seen: list[dict[str, Any]] = []

        async def handler(connection: ServerConnection) -> None:
            await handshake(connection)
            while True:
                try:
                    frame = json.loads(await connection.recv())
                except Exception:
                    return
                seen.append(frame)
                if frame["method"] == "sessions.list":
                    payload: dict[str, Any] = {"sessions": [
                        {"key": OTHER_KEY, "label": "nightly", "sessionId": "s1"},
                    ]}
                else:
                    payload = {"subscribed": True, "key": OTHER_KEY}
                await connection.send(json.dumps({
                    "type": "res", "id": frame["id"], "ok": True, "payload": payload,
                }))

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            connection = await connect(port)
            probe = OpenClawAgentTerminal(
                connection=connection, agent_id="hermes",
                session_key=SESSION_KEY, session_id="s",
                policy=WORK_TERMINAL_POLICY,
            )
            # The gateway must NAME the key before it can be addressed.
            await probe.list_sessions()
            terminal = await OpenClawAgentTerminal.attach(
                connection=connection, agent_id="hermes",
                session_key=OTHER_KEY, policy=WORK_TERMINAL_POLICY,
            )
            await connection.close()

        assert terminal.session_key == OTHER_KEY
        subscribe = [f for f in seen if f["method"] == "sessions.messages.subscribe"]
        assert subscribe[0]["params"] == {
            "key": OTHER_KEY, "agentId": "hermes", "includeApprovals": True,
        }

    async def test_an_unlisted_key_is_refused_before_it_reaches_the_socket(
        self,
    ) -> None:
        """The provenance rule, proven over a live connection."""
        seen: list[dict[str, Any]] = []

        async def handler(connection: ServerConnection) -> None:
            await handshake(connection)
            try:
                seen.append(json.loads(await connection.recv()))
            except Exception:
                return

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            connection = await connect(port)
            with pytest.raises(gateway.OpenClawGatewayError):
                # Nothing has listed this key on THIS connection.
                await OpenClawAgentTerminal.attach(
                    connection=connection, agent_id="hermes",
                    session_key=OTHER_KEY, policy=WORK_TERMINAL_POLICY,
                )
            await connection.close()

        assert seen == [], "an unlisted key reached the gateway"


class TestApprovalEventParsing:
    def test_a_sanitized_event_becomes_a_wearer_facing_record(self) -> None:
        from joat.agents.openclaw_terminal import _parse_approval

        approval = _parse_approval({
            "id": "appr-9", "kind": "exec", "command": "rm -rf build/",
            "runId": "run-1", "sessionKey": SESSION_KEY,
        })
        assert approval == PendingApproval("appr-9", "exec", "rm -rf build/")

    def test_an_unusable_event_is_dropped_rather_than_guessed(self) -> None:
        from joat.agents.openclaw_terminal import _parse_approval

        assert _parse_approval({"kind": "exec"}) is None          # no id
        assert _parse_approval({"id": "a"}) is None               # no kind
        assert _parse_approval({"id": "", "kind": "exec"}) is None
