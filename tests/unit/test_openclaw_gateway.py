"""First-class OpenClaw Gateway transport contracts.

These checks exercise JOAT's own trust boundary.  OpenClaw's server validates
its protocol separately; JOAT must still refuse configurations that would
send a paired device credential over plaintext to another machine.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.frames import Frame, TEXT


@dataclass
class RecordingIdentity:
    device_id: str = "b" * 64
    public_key: str = "public-key-base64url"
    signed_payloads: list[str] = field(default_factory=list)

    async def sign(self, payload: str) -> str:
        self.signed_payloads.append(payload)
        return "signature-base64url"


@dataclass(frozen=True)
class StoredToken:
    token: str
    scopes: tuple[str, ...]


@dataclass
class RecordingTokenStore:
    record: StoredToken | None = None

    async def load(self, *, device_id: str, role: str) -> StoredToken | None:
        assert device_id == "b" * 64
        assert role == "operator"
        return self.record

    async def store(
        self,
        *,
        device_id: str,
        role: str,
        token: str,
        scopes: tuple[str, ...],
    ) -> None:
        assert device_id == "b" * 64
        assert role == "operator"
        self.record = StoredToken(token=token, scopes=scopes)


class EchoRecordingWebSocket:
    """External transport double that records every frame and answers it."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._incoming: asyncio.Queue[str | None] = asyncio.Queue()

    def __aiter__(self) -> EchoRecordingWebSocket:
        return self

    async def __anext__(self) -> str:
        incoming = await self._incoming.get()
        if incoming is None:
            raise StopAsyncIteration
        return incoming

    async def send(self, raw: str) -> None:
        self.sent.append(raw)
        request = json.loads(raw)
        await self._incoming.put(json.dumps({
            "type": "res",
            "id": request["id"],
            "ok": True,
            "payload": {"ok": True},
        }))

    async def close(self) -> None:
        await self._incoming.put(None)


class HangingSendWebSocket(EchoRecordingWebSocket):
    async def send(self, _raw: str) -> None:
        await asyncio.Event().wait()


class HangingCloseWebSocket(EchoRecordingWebSocket):
    async def close(self) -> None:
        await asyncio.Event().wait()


def started_recording_connection(
    gateway: Any,
    *,
    methods: list[str],
    policy: Any = None,
) -> tuple[Any, EchoRecordingWebSocket]:
    websocket = EchoRecordingWebSocket()
    connection_kwargs = {}
    if policy is not None:
        connection_kwargs["policy"] = policy
    connection = gateway.OpenClawGatewayConnection(
        websocket=websocket,
        hello={
            "type": "hello-ok",
            "protocol": 4,
            "server": {"version": "2026.7.1", "connId": "conn-policy"},
            "features": {
                "methods": methods,
                "events": ["agent", "chat", "session.message"],
                "capabilities": ["tool-events"],
            },
            "snapshot": {},
            "auth": {
                "role": "operator",
                "scopes": ["operator.read", "operator.write"],
            },
            "policy": {
                "maxPayload": 1_048_576,
                "maxBufferedBytes": 1_048_576,
                "tickIntervalMs": 30_000,
            },
        },
        **connection_kwargs,
    )
    connection.start()
    return connection, websocket


@pytest.mark.asyncio
async def test_request_timeout_bounds_websocket_send_as_well_as_response() -> None:
    gateway = importlib.import_module("joat.agents.openclaw_gateway")
    websocket = HangingSendWebSocket()
    connection = gateway.OpenClawGatewayConnection(
        websocket=websocket,
        timeout_s=0.01,
        hello={
            "features": {
                "methods": ["sessions.list"],
                "events": ["agent", "chat", "session.message"],
            },
            "auth": {"scopes": ["operator.read", "operator.write"]},
        },
    )
    connection.start()
    try:
        async with asyncio.timeout(0.1):
            with pytest.raises(gateway.OpenClawGatewayError, match="request timed out"):
                await connection.request(
                    "sessions.list",
                    {"label": "JOAT Agent Terminal", "agentId": "hermes", "limit": 2},
                )
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_connection_close_uses_joat_owned_deadline() -> None:
    gateway = importlib.import_module("joat.agents.openclaw_gateway")
    connection = gateway.OpenClawGatewayConnection(
        websocket=HangingCloseWebSocket(),
        timeout_s=0.01,
        hello={
            "features": {
                "methods": ["sessions.list"],
                "events": ["agent", "chat", "session.message"],
            },
            "auth": {"scopes": ["operator.read", "operator.write"]},
        },
    )
    connection.start()

    async with asyncio.timeout(0.1):
        with pytest.raises(gateway.OpenClawGatewayError, match="close timed out"):
            await connection.close()


@pytest.mark.asyncio
async def test_gateway_dial_uses_configured_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = importlib.import_module("joat.agents.openclaw_gateway")

    async def hanging_dial(*_args: Any, **_kwargs: Any) -> Any:
        await asyncio.Event().wait()

    monkeypatch.setattr(gateway, "open_websocket", hanging_dial)
    client = gateway.OpenClawGatewayClient(
        profile=gateway.GatewayProfile("wss://example.ts.net"),
        identity=RecordingIdentity(),
        scopes=("operator.read", "operator.write"),
        timeout_s=0.01,
    )

    async with asyncio.timeout(0.1):
        with pytest.raises(gateway.OpenClawGatewayError, match="connection timed out"):
            await client.connect()


def test_remote_gateway_rejects_plaintext_websocket() -> None:
    """Regression caught: accepting ``ws://`` for a non-loopback host."""
    gateway = importlib.import_module("joat.agents.openclaw_gateway")

    with pytest.raises(ValueError, match="remote OpenClaw gateways require wss"):
        gateway.GatewayProfile("ws://home-mac.example-tailnet.ts.net")


@pytest.mark.parametrize(
    ("label", "namespace", "display_name"),
    [
        ("JOAT Calendar Proposals", "joat-", "JOAT Calendar Proposer"),
        ("JOAT Agent Terminal", "joat-calendar-", "JOAT Brainstorm"),
        ("JOAT Agent Terminal", "joat-", "JOAT Calendar Proposer"),
    ],
)
def test_terminal_policy_rejects_mixed_lane_identity(
    label: str,
    namespace: str,
    display_name: str,
) -> None:
    gateway = importlib.import_module("joat.agents.openclaw_gateway")

    with pytest.raises(ValueError, match="complete lane"):
        gateway.AgentTerminalPolicy(
            agent_id="hermes",
            session_label=label,
            session_key_namespace=namespace,
            client_display_name=display_name,
        )


def test_gateway_profile_canonicalizes_stable_keychain_identity_origin() -> None:
    gateway = importlib.import_module("joat.agents.openclaw_gateway")

    profile = gateway.GatewayProfile(
        "WSS://home-mac.example-tailnet.TS.NET:443/"
    )

    assert profile.url == "wss://home-mac.example-tailnet.ts.net"


@pytest.mark.parametrize(
    "url",
    [
        "https://home-mac.example-tailnet.ts.net",
        "wss://token@home-mac.example-tailnet.ts.net",
        "wss://home-mac.example-tailnet.ts.net?token=secret",
        "wss://home-mac.example-tailnet.ts.net#secret",
        "wss://home-mac.example-tailnet.ts.net/not-the-gateway",
    ],
)
def test_gateway_profile_rejects_unsafe_endpoint_shapes(url: str) -> None:
    """Regression caught: credentials or an ambiguous endpoint enter config."""
    gateway = importlib.import_module("joat.agents.openclaw_gateway")

    with pytest.raises(ValueError, match="valid ws or wss Gateway origin"):
        gateway.GatewayProfile(url)


@pytest.mark.parametrize(
    ("token", "secret_fragment"),
    [
        ("paired-secret", "paired-secret"),
        ('prefix"SECRET-SUFFIX', "SECRET-SUFFIX"),
        (r"prefix\BACKSLASH-SUFFIX", "BACKSLASH-SUFFIX"),
        ("π-🔐-UNICODE-SUFFIX", "UNICODE-SUFFIX"),
        ("prefix]ARRAY-SUFFIX", "ARRAY-SUFFIX"),
    ],
)
def test_openclaw_redaction_covers_device_token_in_real_frame_log(
    token: str,
    secret_fragment: str,
) -> None:
    """The paired secret must be redacted before the client socket opens."""
    gateway = importlib.import_module("joat.agents.openclaw_gateway")
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    root = logging.getLogger()
    client_log = logging.getLogger("websockets.client")
    old_root_level = root.level
    old_client_level = client_log.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    client_log.setLevel(logging.DEBUG)
    try:
        gateway.install_websocket_secret_redaction()
        frame = Frame(
            TEXT,
            json.dumps(
                {
                    "auth": {
                        "token": token,
                        "deviceToken": token,
                        "deviceTokens": [token],
                    }
                },
                ensure_ascii=False,
            ).encode(),
        )
        client_log.debug("> %s", frame)
        rendered = output.getvalue()
        assert secret_fragment not in rendered
        assert rendered.count("<redacted>") == 3
    finally:
        root.removeHandler(handler)
        root.setLevel(old_root_level)
        client_log.setLevel(old_client_level)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    [
        "channels.status",
        "channels.stop",
        "channels.logout",
        "config.get",
        "config.set",
        "config.patch",
        "config.apply",
        "agents.create",
        "agents.update",
        "agents.delete",
        "agents.files.set",
        "tools.invoke",
        "send",
        "health",
        "future.plugin.root",
    ],
)
async def test_authenticated_connection_never_transmits_non_terminal_rpc(
    method: str,
) -> None:
    """Server advertisement must not widen JOAT beyond Agent Terminal RPCs."""
    gateway = importlib.import_module("joat.agents.openclaw_gateway")
    connection, websocket = started_recording_connection(
        gateway,
        methods=[method],
    )

    try:
        with pytest.raises(gateway.OpenClawGatewayError, match="local policy"):
            await connection.request(method, {})
    finally:
        await connection.close()

    assert websocket.sent == []
    assert connection._pending == {}


@pytest.mark.asyncio
async def test_calendar_connection_can_only_use_its_own_session_namespace() -> None:
    gateway = importlib.import_module("joat.agents.openclaw_gateway")
    connection, websocket = started_recording_connection(
        gateway,
        methods=["sessions.list", "sessions.create"],
        policy=gateway.CALENDAR_PROPOSAL_TERMINAL_POLICY,
    )

    try:
        await connection.request(
            "sessions.list",
            {
                "label": "JOAT Calendar Proposals",
                "agentId": "hermes",
                "limit": 2,
            },
        )
        await connection.request(
            "sessions.create",
            {
                "key": "agent:hermes:joat-calendar-deadbeef",
                "agentId": "hermes",
                "label": "JOAT Calendar Proposals",
            },
        )
        with pytest.raises(gateway.OpenClawGatewayError, match="local policy"):
            await connection.request(
                "sessions.send",
                {
                    "key": "agent:hermes:joat-deadbeef",
                    "agentId": "hermes",
                    "message": "Brainstorm history must stay separate",
                    "idempotencyKey": "a" * 32,
                },
            )
    finally:
        await connection.close()

    sent = [json.loads(raw) for raw in websocket.sent]
    assert [frame["method"] for frame in sent] == [
        "sessions.list",
        "sessions.create",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params"),
    [
        (
            "sessions.list",
            {"label": "JOAT Agent Terminal", "agentId": "main", "limit": 2},
        ),
        (
            "sessions.create",
            {
                "key": "agent:main:telegram:direct:123",
                "agentId": "main",
                "label": "JOAT Agent Terminal",
            },
        ),
        ("sessions.describe", {"key": "agent:main:main"}),
        (
            "sessions.messages.subscribe",
            {"key": "agent:main:telegram:direct:123", "agentId": "main"},
        ),
        (
            "sessions.send",
            {
                "key": "agent:main:telegram:direct:123",
                "agentId": "main",
                "message": "must not leave this Mac",
                "idempotencyKey": "a" * 32,
            },
        ),
        (
            "sessions.abort",
            {"key": "agent:main:main", "agentId": "main", "runId": "run-1"},
        ),
        (
            "tools.effective",
            {"sessionKey": "agent:main:main", "agentId": "main"},
        ),
        ("chat.history", {"sessionKey": "agent:main:telegram:direct:123"}),
        (
            "sessions.send",
            {
                "key": "agent:other:joat-deadbeef",
                "agentId": "hermes",
                "message": "must not leave this Mac",
                "idempotencyKey": "a" * 32,
            },
        ),
        (
            "sessions.send",
            {
                "key": "agent:hermes:joat-deadbeef",
                "agentId": "hermes",
                "message": "must not acquire a delivery route",
                "idempotencyKey": "a" * 32,
                "to": "telegram:123",
            },
        ),
        ("sessions.subscribe", {"includeTelegram": True}),
    ],
)
async def test_authenticated_connection_never_transmits_outside_hermes_session(
    method: str,
    params: dict[str, object],
) -> None:
    """An allowed method cannot be retargeted to main or Telegram sessions."""
    gateway = importlib.import_module("joat.agents.openclaw_gateway")
    connection, websocket = started_recording_connection(
        gateway,
        methods=[method],
    )

    try:
        with pytest.raises(gateway.OpenClawGatewayError, match="local policy"):
            await connection.request(method, params)
    finally:
        await connection.close()

    assert websocket.sent == []
    assert connection._pending == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("sessions.subscribe", {}),
        (
            "sessions.list",
            {"label": "JOAT Agent Terminal", "agentId": "hermes", "limit": 2},
        ),
        (
            "sessions.create",
            {
                "key": "agent:hermes:joat-deadbeef",
                "agentId": "hermes",
                "label": "JOAT Agent Terminal",
            },
        ),
        ("sessions.describe", {"key": "agent:hermes:joat-deadbeef"}),
        (
            "sessions.messages.subscribe",
            {"key": "agent:hermes:joat-deadbeef", "agentId": "hermes"},
        ),
        (
            "sessions.send",
            {
                "key": "agent:hermes:joat-deadbeef",
                "agentId": "hermes",
                "message": "isolated Brainstorm turn",
                "idempotencyKey": "a" * 32,
            },
        ),
        (
            "sessions.abort",
            {
                "key": "agent:hermes:joat-deadbeef",
                "runId": "opaque/run=id α",
                "agentId": "hermes",
            },
        ),
        (
            "tools.effective",
            {"sessionKey": "agent:hermes:joat-deadbeef", "agentId": "hermes"},
        ),
        ("chat.history", {"sessionKey": "agent:hermes:joat-deadbeef"}),
    ],
)
async def test_authenticated_connection_transmits_only_exact_terminal_rpc_shapes(
    method: str,
    params: dict[str, object],
) -> None:
    """The local firewall must preserve every RPC used by the safe terminal."""
    gateway = importlib.import_module("joat.agents.openclaw_gateway")
    connection, websocket = started_recording_connection(
        gateway,
        methods=[method],
    )

    try:
        response = await connection.request(method, params)
    finally:
        await connection.close()

    assert response == {"ok": True}
    assert len(websocket.sent) == 1
    emitted = json.loads(websocket.sent[0])
    assert emitted["method"] == method
    assert emitted["params"] == params


def test_device_auth_payload_matches_openclaw_v3_contract() -> None:
    """Regression caught: a differently ordered signature payload is emitted."""
    gateway = importlib.import_module("joat.agents.openclaw_gateway")

    payload = gateway.build_device_auth_payload_v3(
        device_id="a" * 64,
        client_id="gateway-client",
        client_mode="backend",
        role="operator",
        scopes=("operator.read",),
        signed_at_ms=1_777_777_777_000,
        token=None,
        nonce="nonce-123",
        platform=" Darwin ",
        device_family=" MacBookPro ",
    )

    assert payload == (
        "v3|"
        + "a" * 64
        + "|gateway-client|backend|operator|operator.read|1777777777000||"
        + "nonce-123|darwin|macbookpro"
    )


@pytest.mark.asyncio
async def test_connect_params_bind_identity_to_challenge_and_read_scope() -> None:
    """Regression caught: connect is unsigned or signs a different challenge."""
    gateway = importlib.import_module("joat.agents.openclaw_gateway")
    identity = RecordingIdentity()

    params = await gateway.build_connect_params(
        nonce="server-nonce",
        challenge_ts=1_777_777_777_001,
        identity=identity,
        scopes=("operator.read",),
        device_token=None,
    )

    assert identity.signed_payloads == [
        "v3|"
        + "b" * 64
        + "|gateway-client|backend|operator|operator.read|1777777777001||"
        + "server-nonce|darwin|mac"
    ]
    assert params == {
        "minProtocol": 4,
        "maxProtocol": 4,
        "client": {
            "id": "gateway-client",
            "displayName": "JOAT Brainstorm",
            "version": "0.1.0",
            "platform": "darwin",
            "deviceFamily": "mac",
            "mode": "backend",
        },
        "caps": [],
        "role": "operator",
        "scopes": ["operator.read"],
        "device": {
            "id": "b" * 64,
            "publicKey": "public-key-base64url",
            "signature": "signature-base64url",
            "signedAt": 1_777_777_777_001,
            "nonce": "server-nonce",
        },
        "locale": "en-US",
        "userAgent": "joat-openclaw-gateway/0.1.0",
    }


@pytest.mark.asyncio
async def test_connect_params_advertise_only_the_tool_event_capability() -> None:
    """A no-tool terminal must still observe any attempted tool execution."""
    gateway = importlib.import_module("joat.agents.openclaw_gateway")

    params = await gateway.build_connect_params(
        nonce="server-nonce",
        challenge_ts=1_777_777_777_001,
        identity=RecordingIdentity(),
        scopes=("operator.read", "operator.write"),
        device_token=None,
        capabilities=("tool-events",),
    )

    assert params["caps"] == ["tool-events"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capabilities",
    [
        ("approvals",),
        ("tool-events", "tool-events"),
    ],
)
async def test_connect_params_reject_unknown_or_duplicate_capabilities(
    capabilities: tuple[str, ...],
) -> None:
    gateway = importlib.import_module("joat.agents.openclaw_gateway")

    with pytest.raises(ValueError, match="OpenClaw client capabilities"):
        await gateway.build_connect_params(
            nonce="server-nonce",
            challenge_ts=1_777_777_777_001,
            identity=RecordingIdentity(),
            scopes=("operator.read", "operator.write"),
            device_token=None,
            capabilities=capabilities,
        )
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scopes",
    [
        (),
        ("operator.admin",),
        ("operator.approvals",),
        ("operator.read", "operator.admin"),
        ("operator.read", "operator.read"),
    ],
)
async def test_connect_params_reject_empty_privileged_or_duplicate_scopes(
    scopes: tuple[str, ...],
) -> None:
    """Regression caught: a config typo silently widens or muddies authority."""
    gateway = importlib.import_module("joat.agents.openclaw_gateway")
    identity = RecordingIdentity()

    with pytest.raises(ValueError, match="OpenClaw operator scopes"):
        await gateway.build_connect_params(
            nonce="server-nonce",
            challenge_ts=1_777_777_777_001,
            identity=identity,
            scopes=scopes,
            device_token=None,
        )

    assert identity.signed_payloads == []


@pytest.mark.asyncio
async def test_connect_params_bind_device_token_without_exposing_it_elsewhere() -> None:
    """Regression caught: the pairing token is omitted from the signed proof."""
    gateway = importlib.import_module("joat.agents.openclaw_gateway")
    identity = RecordingIdentity()

    params = await gateway.build_connect_params(
        nonce="server-nonce",
        challenge_ts=1_777_777_777_002,
        identity=identity,
        scopes=("operator.read",),
        device_token="paired-device-token",
    )

    assert identity.signed_payloads == [
        "v3|"
        + "b" * 64
        + "|gateway-client|backend|operator|operator.read|1777777777002|"
        + "paired-device-token|server-nonce|darwin|mac"
    ]
    assert params["auth"] == {
        "token": "paired-device-token",
        "deviceToken": "paired-device-token",
    }
    assert "paired-device-token" not in repr(params["client"])


@pytest.mark.asyncio
async def test_client_surfaces_pairing_request_from_real_websocket_exchange() -> None:
    """Regression caught: a pending approval is flattened into an offline error."""
    gateway = importlib.import_module("joat.agents.openclaw_gateway")
    received: list[dict[str, Any]] = []

    async def handler(connection: ServerConnection) -> None:
        await connection.send(
            json.dumps(
                {
                    "type": "event",
                    "event": "connect.challenge",
                    "payload": {"nonce": "gateway-nonce", "ts": 1_777_777_777_003},
                }
            )
        )
        frame = json.loads(await connection.recv())
        received.append(frame)
        await connection.send(
            json.dumps(
                {
                    "type": "res",
                    "id": frame["id"],
                    "ok": False,
                    "error": {
                        "code": "NOT_PAIRED",
                        "message": "device pairing required",
                        "details": {
                            "code": "PAIRING_REQUIRED",
                            "reason": "not-paired",
                            "requestId": "pairing-request-123",
                            "requestedRole": "operator",
                            "requestedScopes": ["operator.read"],
                        },
                    },
                }
            )
        )

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = gateway.OpenClawGatewayClient(
            profile=gateway.GatewayProfile(f"ws://127.0.0.1:{port}"),
            identity=RecordingIdentity(),
            scopes=("operator.read",),
        )

        with pytest.raises(gateway.OpenClawPairingRequired) as error:
            await client.connect()

    assert error.value.request_id == "pairing-request-123"
    assert error.value.reason == "not-paired"
    assert str(error.value) == "OpenClaw device approval is required"
    assert len(received) == 1
    assert received[0]["type"] == "req"
    assert received[0]["method"] == "connect"
    assert received[0]["params"]["scopes"] == ["operator.read"]


@pytest.mark.asyncio
async def test_client_persists_issued_device_token_and_uses_it_on_reconnect() -> None:
    """Regression caught: approval succeeds once, then JOAT forgets its pairing."""
    gateway = importlib.import_module("joat.agents.openclaw_gateway")
    identity = RecordingIdentity()
    token_store = RecordingTokenStore()
    received: list[dict[str, Any]] = []

    async def handler(connection: ServerConnection) -> None:
        await connection.send(
            json.dumps(
                {
                    "type": "event",
                    "event": "connect.challenge",
                    "payload": {"nonce": "gateway-nonce", "ts": 1_777_777_777_004},
                }
            )
        )
        frame = json.loads(await connection.recv())
        received.append(frame)
        await connection.send(
            json.dumps(
                {
                    "type": "res",
                    "id": frame["id"],
                    "ok": True,
                    "payload": {
                        "type": "hello-ok",
                        "protocol": 4,
                        "server": {"version": "2026.8.28", "connId": "conn-123"},
                        "features": {"methods": [], "events": [], "capabilities": []},
                        "snapshot": {},
                        "auth": {
                            "role": "operator",
                            "scopes": ["operator.read"],
                            "deviceToken": "issued-device-token",
                        },
                        "policy": {
                            "maxPayload": 1_048_576,
                            "maxBufferedBytes": 1_048_576,
                            "tickIntervalMs": 30_000,
                        },
                    },
                }
            )
        )

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        profile = gateway.GatewayProfile(f"ws://127.0.0.1:{port}")
        client = gateway.OpenClawGatewayClient(
            profile=profile,
            identity=identity,
            scopes=("operator.read",),
            token_store=token_store,
        )
        first = await client.connect()
        await first.close()
        second = await client.connect()
        await second.close()

    assert token_store.record == StoredToken(
        token="issued-device-token", scopes=("operator.read",)
    )
    assert "auth" not in received[0]["params"]
    assert received[1]["params"]["auth"] == {
        "token": "issued-device-token",
        "deviceToken": "issued-device-token",
    }
    assert "|issued-device-token|gateway-nonce|" in identity.signed_payloads[1]


@pytest.mark.asyncio
async def test_authenticated_connection_correlates_response_with_interleaved_event() -> None:
    """Regression caught: a live event is mistaken for the RPC response."""
    gateway = importlib.import_module("joat.agents.openclaw_gateway")

    async def handler(connection: ServerConnection) -> None:
        await connection.send(
            json.dumps(
                {
                    "type": "event",
                    "event": "connect.challenge",
                    "payload": {"nonce": "gateway-nonce", "ts": 1_777_777_777_005},
                }
            )
        )
        connect_frame = json.loads(await connection.recv())
        await connection.send(
            json.dumps(
                {
                    "type": "res",
                    "id": connect_frame["id"],
                    "ok": True,
                    "payload": {
                        "type": "hello-ok",
                        "protocol": 4,
                        "server": {"version": "2026.8.28", "connId": "conn-456"},
                        "features": {
                            "methods": ["sessions.subscribe"],
                            "events": ["tick"],
                            "capabilities": [],
                        },
                        "snapshot": {},
                        "auth": {"role": "operator", "scopes": ["operator.read"]},
                        "policy": {
                            "maxPayload": 1_048_576,
                            "maxBufferedBytes": 1_048_576,
                            "tickIntervalMs": 30_000,
                        },
                    },
                }
            )
        )
        request = json.loads(await connection.recv())
        await connection.send(
            json.dumps(
                {
                    "type": "event",
                    "event": "tick",
                    "payload": {"ts": 1_777_777_777_006},
                    "seq": 1,
                }
            )
        )
        await connection.send(
            json.dumps(
                {
                    "type": "res",
                    "id": request["id"],
                    "ok": True,
                    "payload": {"ok": True},
                }
            )
        )

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = gateway.OpenClawGatewayClient(
            profile=gateway.GatewayProfile(f"ws://127.0.0.1:{port}"),
            identity=RecordingIdentity(),
            scopes=("operator.read",),
        )
        connection = await client.connect()
        response = await connection.request("sessions.subscribe", {})
        event = await connection.next_event()
        await connection.close()

    assert response == {"ok": True}
    assert event["event"] == "tick"
    assert event["seq"] == 1


@pytest.mark.asyncio
async def test_authenticated_connection_surfaces_outer_event_sequence_gap() -> None:
    """A dropped socket event must trigger history recovery, not silent drift."""
    gateway = importlib.import_module("joat.agents.openclaw_gateway")

    async def handler(connection: ServerConnection) -> None:
        await _accept_gateway(connection, events=["tick"])
        await connection.send(json.dumps({
            "type": "event",
            "event": "tick",
            "payload": {"ts": 1},
            "seq": 7,
        }))
        await connection.send(json.dumps({
            "type": "event",
            "event": "tick",
            "payload": {"ts": 2},
            "seq": 9,
        }))

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = gateway.OpenClawGatewayClient(
            profile=gateway.GatewayProfile(f"ws://127.0.0.1:{port}"),
            identity=RecordingIdentity(),
            scopes=("operator.read",),
        )
        connection = await client.connect()
        first = await connection.next_event()
        with pytest.raises(gateway.OpenClawEventSequenceGap) as error:
            await connection.next_event()
        await connection.close()

    assert first["seq"] == 7
    assert error.value.expected == 8
    assert error.value.actual == 9


@pytest.mark.asyncio
async def test_authenticated_connection_wakes_event_waiter_when_socket_closes() -> None:
    """A silent close must not leave a model run waiting for an arbitrary timeout."""
    gateway = importlib.import_module("joat.agents.openclaw_gateway")

    async def handler(connection: ServerConnection) -> None:
        await _accept_gateway(connection, events=["tick"])
        await connection.close()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = gateway.OpenClawGatewayClient(
            profile=gateway.GatewayProfile(f"ws://127.0.0.1:{port}"),
            identity=RecordingIdentity(),
            scopes=("operator.read",),
            timeout_s=60,
        )
        connection = await client.connect()
        async with asyncio.timeout(1):
            with pytest.raises(gateway.OpenClawGatewayError, match="closed unexpectedly"):
                await connection.next_event()
        await connection.close()


async def _accept_gateway(
    connection: ServerConnection,
    *,
    events: list[str],
) -> None:
    await connection.send(json.dumps({
        "type": "event",
        "event": "connect.challenge",
        "payload": {"nonce": "gateway-nonce", "ts": 1_777_777_777_007},
    }))
    request = json.loads(await connection.recv())
    await connection.send(json.dumps({
        "type": "res",
        "id": request["id"],
        "ok": True,
        "payload": {
            "type": "hello-ok",
            "protocol": 4,
            "server": {"version": "2026.8.28", "connId": "conn-recovery"},
            "features": {"methods": [], "events": events, "capabilities": []},
            "snapshot": {},
            "auth": {"role": "operator", "scopes": ["operator.read"]},
            "policy": {
                "maxPayload": 1_048_576,
                "maxBufferedBytes": 1_048_576,
                "tickIntervalMs": 30_000,
            },
        },
    }))
