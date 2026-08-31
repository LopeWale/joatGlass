"""Paired OpenClaw Gateway transport for Brainstorm / Agent Terminal.

The compatibility adapter in :mod:`joat.agents.openclaw` intentionally stays
separate: it uses a shared HTTP bearer credential.  This module owns the
device-paired WebSocket trust boundary instead.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Protocol, cast
from urllib.parse import urlsplit
from uuid import uuid4

from websockets.asyncio.client import ClientConnection, connect as open_websocket

from ..core.log_redaction import install_websocket_secret_redaction


PROTOCOL_VERSION = 4
# ``gateway-client`` / ``backend`` is OpenClaw's reserved direct-loopback
# helper identity.  JOAT crosses a Tailnet boundary with a signed paired-device
# identity, so it must advertise the ordinary canonical operator identity
# instead of looking like the Gateway's own internal control-plane client.
CLIENT_ID = "cli"
CLIENT_MODE = "operator"
CLIENT_VERSION = "0.1.0"
CLIENT_PLATFORM = "darwin"
CLIENT_DEVICE_FAMILY = "mac"
CLIENT_ROLE = "operator"
# operator.approvals is requested ONLY by the work lane (allows_approvals);
# the Brainstorm/calendar lanes keep their exact read+write equality check,
# so listing it here grants them nothing.
ALLOWED_OPERATOR_SCOPES = frozenset(
    {"operator.read", "operator.write", "operator.approvals"}
)
ALLOWED_CLIENT_CAPABILITIES = frozenset({"tool-events"})
PAIRING_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
AGENT_TERMINAL_AGENT_ID = "hermes"
AGENT_TERMINAL_SESSION_LABEL = "JOAT Agent Terminal"
CALENDAR_PROPOSAL_SESSION_LABEL = "JOAT Calendar Proposals"
WORK_SESSION_LABEL = "JOAT Work Sessions"
AGENT_TERMINAL_METHODS = frozenset(
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
        # Work lane only (allows_approvals) — docs.openclaw.ai/gateway/protocol:
        # answer a session.approval event; first answer wins.
        "approval.resolve",
    }
)
AGENT_TERMINAL_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class AgentTerminalPolicy:
    """Closed local RPC/session namespace for one Hermes use case."""

    agent_id: str
    session_label: str
    session_key_namespace: str
    client_display_name: str
    allows_approvals: bool = False
    """Only the work lane may receive and answer tool approvals. The
    Brainstorm lane treats any approval/tool event as a violation — that
    tripwire is load-bearing safety, so approvals arrive via a NEW lane
    with its own scopes, never by loosening an existing one."""
    roster: bool = False
    """Only the work lane may enumerate the wearer's sessions
    (sessions.list with involvingMe); the other lanes stay pinned to their
    single labelled session."""

    def __post_init__(self) -> None:
        identity = (
            self.agent_id,
            self.session_label,
            self.session_key_namespace,
            self.client_display_name,
            self.allows_approvals,
            self.roster,
        )
        if identity not in {
            (
                AGENT_TERMINAL_AGENT_ID,
                AGENT_TERMINAL_SESSION_LABEL,
                "joat-",
                "JOAT Brainstorm",
                False,
                False,
            ),
            (
                AGENT_TERMINAL_AGENT_ID,
                CALENDAR_PROPOSAL_SESSION_LABEL,
                "joat-calendar-",
                "JOAT Calendar Proposer",
                False,
                False,
            ),
            (
                AGENT_TERMINAL_AGENT_ID,
                WORK_SESSION_LABEL,
                "joat-work-",
                "JOAT Agent Terminal",
                True,
                True,
            ),
        }:
            raise ValueError("JOAT Agent Terminal policy is not a known complete lane")

    def owns_session_key(self, value: object) -> bool:
        if not isinstance(value, str):
            return False
        prefix = f"agent:{self.agent_id}:{self.session_key_namespace}"
        suffix = value.removeprefix(prefix)
        return (
            value.startswith(prefix)
            and 1 <= len(suffix) <= 64
            and all(character in "0123456789abcdef" for character in suffix)
        )

    def new_session_key(self, suffix: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{1,64}", suffix):
            raise ValueError("JOAT Agent Terminal session suffix is invalid")
        return f"agent:{self.agent_id}:{self.session_key_namespace}{suffix}"


BRAINSTORM_TERMINAL_POLICY = AgentTerminalPolicy(
    agent_id=AGENT_TERMINAL_AGENT_ID,
    session_label=AGENT_TERMINAL_SESSION_LABEL,
    session_key_namespace="joat-",
    client_display_name="JOAT Brainstorm",
)
CALENDAR_PROPOSAL_TERMINAL_POLICY = AgentTerminalPolicy(
    agent_id=AGENT_TERMINAL_AGENT_ID,
    session_label=CALENDAR_PROPOSAL_SESSION_LABEL,
    session_key_namespace="joat-calendar-",
    client_display_name="JOAT Calendar Proposer",
)
WORK_TERMINAL_POLICY = AgentTerminalPolicy(
    agent_id=AGENT_TERMINAL_AGENT_ID,
    session_label=WORK_SESSION_LABEL,
    session_key_namespace="joat-work-",
    client_display_name="JOAT Agent Terminal",
    allows_approvals=True,
    roster=True,
)
APPROVAL_DECISIONS = frozenset({"approve", "deny"})
_APPROVAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class DeviceIdentity(Protocol):
    """Non-exporting signer for a stable, paired JOAT device identity."""

    device_id: str
    public_key: str

    async def sign(self, payload: str) -> str: ...


@dataclass(frozen=True, slots=True)
class DeviceTokenRecord:
    """Paired token material loaded from a platform-owned secret store."""

    token: str = field(repr=False)
    scopes: tuple[str, ...]


class DeviceTokenStore(Protocol):
    """Persistence boundary implemented by the macOS Keychain bridge."""

    async def load(self, *, device_id: str, role: str) -> DeviceTokenRecord | None: ...

    async def store(
        self,
        *,
        device_id: str,
        role: str,
        token: str,
        scopes: tuple[str, ...],
    ) -> None: ...


class OpenClawGatewayError(RuntimeError):
    """Sanitized Gateway failure safe to surface in local UI."""


class OpenClawEventSequenceGap(OpenClawGatewayError):
    """A sequenced server event was lost or arrived out of order."""

    def __init__(self, *, expected: int, actual: int) -> None:
        super().__init__("OpenClaw Gateway event sequence has a gap")
        self.expected = expected
        self.actual = actual


class OpenClawConnectionClosed(OpenClawGatewayError):
    """The authenticated socket ended and may be re-established safely."""


class OpenClawPairingRequired(OpenClawGatewayError):
    """The stable JOAT identity has a pending Gateway approval request."""

    def __init__(self, *, request_id: str | None, reason: str | None) -> None:
        super().__init__("OpenClaw device approval is required")
        self.request_id = request_id
        self.reason = reason


def _harvest_listed_keys(result: object) -> set[str]:
    """Keys from a sessions.list payload — strings only, nothing inferred."""
    record = result if isinstance(result, dict) else None
    raw_sessions = record.get("sessions") if record else None
    keys: set[str] = set()
    if isinstance(raw_sessions, list):
        for raw in raw_sessions:
            if isinstance(raw, dict):
                key = raw.get("key")
                if isinstance(key, str) and 0 < len(key) <= 256:
                    keys.add(key)
    return keys


def _enforce_agent_terminal_request_policy(
    method: str,
    params: dict[str, object],
    policy: AgentTerminalPolicy = BRAINSTORM_TERMINAL_POLICY,
    *,
    listed_keys: frozenset[str] = frozenset(),
) -> None:
    """Reject any post-auth RPC outside JOAT's isolated Hermes terminal."""

    permitted = False
    if method == "sessions.subscribe":
        permitted = (
            set(params) == {"label", "agentId", "limit"}
            and params.get("label") == policy.session_label
            and params.get("agentId") == policy.agent_id
            and params.get("limit") == 2
            and not isinstance(params.get("limit"), bool)
        )
    elif method == "sessions.list":
        pinned = (
            set(params) == {"label", "agentId", "limit"}
            and params.get("label") == policy.session_label
            and params.get("agentId") == policy.agent_id
            and params.get("limit") == 2
            and not isinstance(params.get("limit"), bool)
        )
        # Roster lanes may enumerate the wearer's OWN sessions — bounded,
        # involvingMe only (docs.openclaw.ai/gateway/protocol), never an
        # arbitrary filter surface.
        limit = params.get("limit")
        roster = (
            policy.roster
            and set(params) == {"agentId", "limit", "involvingMe"}
            and params.get("agentId") == policy.agent_id
            and params.get("involvingMe") is True
            and isinstance(limit, int)
            and not isinstance(limit, bool)
            and 1 <= limit <= 20
        )
        permitted = pinned or roster
    elif method == "sessions.create":
        permitted = (
            set(params) == {"key", "agentId", "label"}
            and policy.owns_session_key(params.get("key"))
            and params.get("agentId") == policy.agent_id
            and params.get("label") == policy.session_label
        )
    elif method == "sessions.describe":
        permitted = (
            set(params) == {"key"}
            and policy.owns_session_key(params.get("key"))
        )
    elif method == "sessions.messages.subscribe":
        plain = (
            set(params) == {"key", "agentId"}
            and policy.owns_session_key(params.get("key"))
            and params.get("agentId") == policy.agent_id
        )
        # includeApprovals is the documented opt-in for session.approval
        # events; only an approvals lane may ask for it.
        with_approvals = (
            policy.allows_approvals
            and set(params) == {"key", "agentId", "includeApprovals"}
            and policy.owns_session_key(params.get("key"))
            and params.get("agentId") == policy.agent_id
            and params.get("includeApprovals") is True
        )
        permitted = plain or with_approvals
    elif method == "sessions.send":
        message = params.get("message")
        key = params.get("key")
        # A roster lane may also address a key the GATEWAY listed for this
        # wearer on this connection — resume, with the gateway itself as
        # the authority. Every other pin (agent, bounds, idempotency) holds
        # identically; only the key's provenance widens.
        addressable = policy.owns_session_key(key) or (
            policy.roster and isinstance(key, str) and key in listed_keys
        )
        permitted = (
            set(params) == {"key", "agentId", "message", "idempotencyKey"}
            and addressable
            and params.get("agentId") == policy.agent_id
            and isinstance(message, str)
            and 0 < len(message) <= 100_000
            and _matches(
                AGENT_TERMINAL_IDEMPOTENCY_KEY_PATTERN,
                params.get("idempotencyKey"),
            )
        )
    elif method == "sessions.abort":
        run_id = params.get("runId")
        abort_key = params.get("key")
        abort_addressable = policy.owns_session_key(abort_key) or (
            policy.roster and isinstance(abort_key, str) and abort_key in listed_keys
        )
        permitted = (
            set(params) == {"key", "runId", "agentId"}
            and abort_addressable
            and isinstance(run_id, str)
            and 0 < len(run_id) <= 1_048_576
            and params.get("agentId") == policy.agent_id
        )
    elif method == "tools.effective":
        permitted = (
            set(params) == {"sessionKey", "agentId"}
            and policy.owns_session_key(params.get("sessionKey"))
            and params.get("agentId") == policy.agent_id
        )
    elif method == "chat.history":
        permitted = (
            set(params) == {"sessionKey"}
            and policy.owns_session_key(params.get("sessionKey"))
        )
    elif method == "approval.resolve":
        decision = params.get("decision")
        kind = params.get("kind")
        permitted = (
            policy.allows_approvals
            and set(params) == {"id", "kind", "decision"}
            and _matches(_APPROVAL_ID_PATTERN, params.get("id"))
            and isinstance(kind, str)
            and 0 < len(kind) <= 64
            and decision in APPROVAL_DECISIONS
        )

    if method not in AGENT_TERMINAL_METHODS or not permitted:
        raise OpenClawGatewayError(
            "OpenClaw Agent Terminal request violates local policy"
        )

def _matches(pattern: re.Pattern[str], value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


@dataclass(slots=True)
class OpenClawGatewayConnection:
    """One authenticated WebSocket plus its protocol-v4 hello payload."""

    websocket: ClientConnection
    hello: dict[str, object]
    timeout_s: float = 10.0
    policy: AgentTerminalPolicy = BRAINSTORM_TERMINAL_POLICY
    _pending: dict[
        str, tuple[str, asyncio.Future[object]]
    ] = field(default_factory=dict, init=False, repr=False)
    _events: asyncio.Queue[dict[str, object] | OpenClawGatewayError] = field(
        default_factory=asyncio.Queue, init=False, repr=False
    )
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _reader_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _methods: frozenset[str] = field(default_factory=frozenset, init=False, repr=False)
    _advertised_events: frozenset[str] = field(default_factory=frozenset, init=False, repr=False)
    _granted_scopes: tuple[str, ...] = field(default=(), init=False, repr=False)
    _last_event_seq: int | None = field(default=None, init=False, repr=False)
    _listed_keys: set[str] = field(default_factory=set, init=False, repr=False)
    """Session keys THIS connection saw the gateway list for this wearer.

    The authority basis for resumable sends: a roster lane may address a
    key only after the gateway itself named it in an involvingMe listing on
    this same authenticated connection — never a key the Mac merely
    invented. Bounded; reconnecting starts empty by construction, which is
    exactly the reconnect-without-replay discipline (roadmap T6)."""

    def __post_init__(self) -> None:
        features = _record(self.hello.get("features"))
        raw_methods = features.get("methods") if features else None
        raw_events = features.get("events") if features else None
        auth = _record(self.hello.get("auth"))
        raw_scopes = auth.get("scopes") if auth else None
        if not isinstance(raw_methods, list) or not all(
            isinstance(method, str) and method for method in raw_methods
        ) or not isinstance(raw_events, list) or not all(
            isinstance(event, str) and event for event in raw_events
        ) or not isinstance(raw_scopes, list) or not all(
            isinstance(scope, str) and scope for scope in raw_scopes
        ):
            raise OpenClawGatewayError("OpenClaw advertised invalid Gateway methods")
        self._methods = frozenset(cast(list[str], raw_methods))
        self._advertised_events = frozenset(cast(list[str], raw_events))
        self._granted_scopes = tuple(cast(list[str], raw_scopes))

    def start(self) -> None:
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._read_loop())

    @property
    def advertised_methods(self) -> frozenset[str]:
        return self._methods

    @property
    def advertised_events(self) -> frozenset[str]:
        return self._advertised_events

    @property
    def granted_scopes(self) -> tuple[str, ...]:
        return self._granted_scopes

    async def request(self, method: str, params: dict[str, object]) -> object:
        _enforce_agent_terminal_request_policy(
            method, params, self.policy,
            listed_keys=frozenset(self._listed_keys),
        )
        if method not in self._methods:
            raise OpenClawGatewayError(
                "OpenClaw does not advertise a required Agent Terminal method"
            )
        if self._reader_task is None:
            raise OpenClawGatewayError("OpenClaw connection is not active")
        request_id = uuid4().hex
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = (method, future)
        try:
            async with asyncio.timeout(self.timeout_s):
                async with self._send_lock:
                    await self.websocket.send(
                        json.dumps(
                            {
                                "type": "req",
                                "id": request_id,
                                "method": method,
                                "params": params,
                            },
                            separators=(",", ":"),
                        )
                    )
                result = await future
                if (
                    method == "sessions.list"
                    and self.policy.roster
                    and params.get("involvingMe") is True
                ):
                    self._listed_keys |= _harvest_listed_keys(result)
                    while len(self._listed_keys) > 64:
                        self._listed_keys.pop()
                return result
        except TimeoutError as exc:
            raise OpenClawGatewayError("OpenClaw request timed out") from exc
        finally:
            self._pending.pop(request_id, None)

    async def next_event(self) -> dict[str, object]:
        item = await self._events.get()
        if isinstance(item, OpenClawGatewayError):
            raise item
        return item

    async def close(self) -> None:
        reader, self._reader_task = self._reader_task, None
        if reader is not None:
            reader.cancel()
            with suppress(asyncio.CancelledError):
                await reader
        try:
            async with asyncio.timeout(self.timeout_s):
                await self.websocket.close()
        except TimeoutError as exc:
            raise OpenClawGatewayError("OpenClaw connection close timed out") from exc

    async def _read_loop(self) -> None:
        try:
            async for raw in self.websocket:
                frame = _decode_frame(raw)
                frame_type = frame.get("type")
                if frame_type == "event":
                    event = frame.get("event")
                    if not isinstance(event, str) or not event:
                        raise OpenClawGatewayError("OpenClaw sent an invalid event")
                    seq = frame.get("seq")
                    if seq is not None:
                        if (
                            not isinstance(seq, int)
                            or isinstance(seq, bool)
                            or seq < 0
                        ):
                            raise OpenClawGatewayError(
                                "OpenClaw sent an invalid event sequence"
                            )
                        if self._last_event_seq is not None:
                            expected = self._last_event_seq + 1
                            if seq != expected:
                                raise OpenClawEventSequenceGap(
                                    expected=expected,
                                    actual=seq,
                                )
                        self._last_event_seq = seq
                    await self._events.put(frame)
                    continue
                if frame_type != "res":
                    raise OpenClawGatewayError("OpenClaw sent an unexpected protocol frame")
                request_id = frame.get("id")
                pending = self._pending.get(request_id) if isinstance(request_id, str) else None
                if pending is None:
                    raise OpenClawGatewayError("OpenClaw sent an unmatched response")
                method, future = pending
                if frame.get("ok") is True:
                    if not future.done():
                        future.set_result(frame.get("payload"))
                    continue
                error = _record(frame.get("error"))
                code = error.get("code") if error else None
                if not future.done():
                    future.set_exception(OpenClawRequestError(
                        method=method,
                        code=code if isinstance(code, str) else None,
                    ))
            raise OpenClawConnectionClosed("OpenClaw connection closed unexpectedly")
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            safe_error = (
                exc
                if isinstance(exc, OpenClawGatewayError)
                else OpenClawConnectionClosed(
                    "OpenClaw connection closed unexpectedly"
                )
            )
            for _, future in self._pending.values():
                if not future.done():
                    future.set_exception(safe_error)
            await self._events.put(safe_error)


class OpenClawRequestError(OpenClawGatewayError):
    """Sanitized failure for one authenticated Gateway request."""

    def __init__(self, *, method: str, code: str | None) -> None:
        super().__init__(f"OpenClaw rejected {method}")
        self.method = method
        self.code = code


@dataclass(frozen=True, slots=True)
class GatewayProfile:
    """Validated, non-secret location of one OpenClaw Gateway."""

    url: str

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError(
                "OpenClaw endpoint must be a valid ws or wss Gateway origin"
            ) from exc
        is_loopback = hostname in {"127.0.0.1", "::1", "localhost"}
        if parsed.scheme == "ws" and not is_loopback:
            raise ValueError("remote OpenClaw gateways require wss")
        safe_origin = (
            parsed.scheme in {"ws", "wss"}
            and bool(hostname)
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
        )
        if not safe_origin:
            raise ValueError("OpenClaw endpoint must be a valid ws or wss Gateway origin")
        host = f"[{hostname}]" if ":" in hostname else hostname
        default_port = 80 if parsed.scheme == "ws" else 443
        authority = host if port in {None, default_port} else f"{host}:{port}"
        object.__setattr__(self, "url", f"{parsed.scheme}://{authority}")


def _validated_operator_scopes(scopes: Sequence[str]) -> list[str]:
    requested = list(scopes)
    if (
        not requested
        or len(set(requested)) != len(requested)
        or any(scope not in ALLOWED_OPERATOR_SCOPES for scope in requested)
        # Approvals authority is only coherent riding on a full read+write
        # session — a bare operator.approvals request is a config typo (or
        # something worse), and it was rejected before the work lane
        # existed. Keep rejecting it.
        or (
            "operator.approvals" in requested
            and not {"operator.read", "operator.write"}.issubset(requested)
        )
    ):
        raise ValueError(
            "OpenClaw operator scopes must be unique operator.read/operator.write "
            "values, with operator.approvals only alongside both"
        )
    return requested


def _validated_client_capabilities(capabilities: Sequence[str]) -> list[str]:
    requested = list(capabilities)
    if (
        len(set(requested)) != len(requested)
        or any(capability not in ALLOWED_CLIENT_CAPABILITIES for capability in requested)
    ):
        raise ValueError(
            "OpenClaw client capabilities must contain only one optional tool-events value"
        )
    return requested


def build_device_auth_payload_v3(
    *,
    device_id: str,
    client_id: str,
    client_mode: str,
    role: str,
    scopes: Sequence[str],
    signed_at_ms: int,
    token: str | None,
    nonce: str,
    platform: str | None,
    device_family: str | None,
) -> str:
    """Build the byte-exact OpenClaw v3 challenge-signature payload."""

    return "|".join(
        (
            "v3",
            device_id,
            client_id,
            client_mode,
            role,
            ",".join(scopes),
            str(signed_at_ms),
            token or "",
            nonce,
            (platform or "").strip().lower(),
            (device_family or "").strip().lower(),
        )
    )


async def build_connect_params(
    *,
    nonce: str,
    challenge_ts: int,
    identity: DeviceIdentity,
    scopes: Sequence[str],
    device_token: str | None,
    capabilities: Sequence[str] = (),
    client_display_name: str = BRAINSTORM_TERMINAL_POLICY.client_display_name,
) -> dict[str, object]:
    """Build one signed protocol-v4 ``connect`` request payload."""

    requested_scopes = _validated_operator_scopes(scopes)
    requested_capabilities = _validated_client_capabilities(capabilities)
    signature_payload = build_device_auth_payload_v3(
        device_id=identity.device_id,
        client_id=CLIENT_ID,
        client_mode=CLIENT_MODE,
        role=CLIENT_ROLE,
        scopes=requested_scopes,
        signed_at_ms=challenge_ts,
        token=device_token,
        nonce=nonce,
        platform=CLIENT_PLATFORM,
        device_family=CLIENT_DEVICE_FAMILY,
    )
    params: dict[str, object] = {
        "minProtocol": PROTOCOL_VERSION,
        "maxProtocol": PROTOCOL_VERSION,
        "client": {
            "id": CLIENT_ID,
            "displayName": client_display_name,
            "version": CLIENT_VERSION,
            "platform": CLIENT_PLATFORM,
            "deviceFamily": CLIENT_DEVICE_FAMILY,
            "mode": CLIENT_MODE,
        },
        "caps": requested_capabilities,
        "role": CLIENT_ROLE,
        "scopes": requested_scopes,
        "device": {
            "id": identity.device_id,
            "publicKey": identity.public_key,
            "signature": await identity.sign(signature_payload),
            "signedAt": challenge_ts,
            "nonce": nonce,
        },
        "locale": "en-US",
        "userAgent": f"joat-openclaw-gateway/{CLIENT_VERSION}",
    }
    if device_token:
        params["auth"] = {"token": device_token, "deviceToken": device_token}
    return params


class OpenClawGatewayClient:
    """Minimal paired client for the OpenClaw Gateway protocol."""

    def __init__(
        self,
        *,
        profile: GatewayProfile,
        identity: DeviceIdentity,
        scopes: Sequence[str],
        token_store: DeviceTokenStore | None = None,
        capabilities: Sequence[str] = (),
        timeout_s: float = 10.0,
        policy: AgentTerminalPolicy = BRAINSTORM_TERMINAL_POLICY,
    ) -> None:
        self._profile = profile
        self._identity = identity
        self._scopes = tuple(_validated_operator_scopes(scopes))
        self._token_store = token_store
        self._capabilities = tuple(_validated_client_capabilities(capabilities))
        self._timeout_s = timeout_s
        self._policy = policy

    async def connect(self) -> OpenClawGatewayConnection:
        """Authenticate once, preserving the socket only after ``hello-ok``."""

        install_websocket_secret_redaction()
        stored = (
            await self._token_store.load(
                device_id=self._identity.device_id,
                role=CLIENT_ROLE,
            )
            if self._token_store
            else None
        )
        if stored is not None and (not isinstance(stored.token, str) or not stored.token):
            raise OpenClawGatewayError("The stored OpenClaw pairing is invalid")
        device_token = stored.token if stored else None
        try:
            async with asyncio.timeout(self._timeout_s):
                websocket = await open_websocket(
                    self._profile.url,
                    open_timeout=self._timeout_s,
                    close_timeout=self._timeout_s,
                )
        except TimeoutError as exc:
            raise OpenClawGatewayError("OpenClaw connection timed out") from exc
        try:
            async with asyncio.timeout(self._timeout_s):
                challenge = _decode_frame(await websocket.recv())
                nonce, challenge_ts = _read_challenge(challenge)
                request_id = uuid4().hex
                params = await build_connect_params(
                    nonce=nonce,
                    challenge_ts=challenge_ts,
                    identity=self._identity,
                    scopes=self._scopes,
                    device_token=device_token,
                    capabilities=self._capabilities,
                    client_display_name=self._policy.client_display_name,
                )
                await websocket.send(
                    json.dumps(
                        {
                            "type": "req",
                            "id": request_id,
                            "method": "connect",
                            "params": params,
                        },
                        separators=(",", ":"),
                    )
                )
                response = _decode_frame(await websocket.recv())
                hello, issued_token = _read_connect_response(
                    response,
                    request_id=request_id,
                    requested_scopes=self._scopes,
                )
                if issued_token and self._token_store:
                    await self._token_store.store(
                        device_id=self._identity.device_id,
                        role=CLIENT_ROLE,
                        token=issued_token,
                        scopes=self._scopes,
                    )
        except BaseException:
            with suppress(Exception, asyncio.CancelledError):
                async with asyncio.timeout(self._timeout_s):
                    await websocket.close()
            raise
        connection = OpenClawGatewayConnection(
            websocket=websocket,
            hello=hello,
            timeout_s=self._timeout_s,
            policy=self._policy,
        )
        connection.start()
        return connection


def _decode_frame(raw: str | bytes) -> dict[str, object]:
    try:
        decoded: object = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OpenClawGatewayError("OpenClaw sent an invalid protocol frame") from exc
    if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
        raise OpenClawGatewayError("OpenClaw sent an invalid protocol frame")
    return cast(dict[str, object], decoded)


def _record(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return None
    return cast(dict[str, object], value)


def _read_challenge(frame: dict[str, object]) -> tuple[str, int]:
    payload = _record(frame.get("payload"))
    nonce = payload.get("nonce") if payload else None
    challenge_ts = payload.get("ts") if payload else None
    if (
        frame.get("type") != "event"
        or frame.get("event") != "connect.challenge"
        or not isinstance(nonce, str)
        or not nonce
        or not isinstance(challenge_ts, int)
        or isinstance(challenge_ts, bool)
        or challenge_ts < 0
    ):
        raise OpenClawGatewayError("OpenClaw sent an invalid connection challenge")
    return nonce, challenge_ts


def _read_connect_response(
    frame: dict[str, object], *, request_id: str, requested_scopes: tuple[str, ...]
) -> tuple[dict[str, object], str | None]:
    if frame.get("type") != "res" or frame.get("id") != request_id:
        raise OpenClawGatewayError("OpenClaw sent an unexpected connection response")
    if frame.get("ok") is True:
        payload = _record(frame.get("payload"))
        auth = _record(payload.get("auth")) if payload else None
        server = _record(payload.get("server")) if payload else None
        features = _record(payload.get("features")) if payload else None
        snapshot = _record(payload.get("snapshot")) if payload else None
        policy = _record(payload.get("policy")) if payload else None
        raw_scopes = auth.get("scopes") if auth else None
        hello_scopes = (
            tuple(raw_scopes)
            if isinstance(raw_scopes, list)
            and all(isinstance(scope, str) for scope in raw_scopes)
            else None
        )
        if (
            payload is None
            or payload.get("type") != "hello-ok"
            or payload.get("protocol") != PROTOCOL_VERSION
            or server is None
            or not isinstance(server.get("version"), str)
            or not server.get("version")
            or not isinstance(server.get("connId"), str)
            or not server.get("connId")
            or features is None
            or not _is_string_list(features.get("methods"))
            or not _is_string_list(features.get("events"))
            or (
                features.get("capabilities") is not None
                and not _is_string_list(features.get("capabilities"))
            )
            or snapshot is None
            or auth is None
            or auth.get("role") != CLIENT_ROLE
            or hello_scopes != requested_scopes
            or policy is None
            or not _is_positive_int(policy.get("maxPayload"))
            or not _is_positive_int(policy.get("maxBufferedBytes"))
            or not _is_positive_int(policy.get("tickIntervalMs"))
        ):
            raise OpenClawGatewayError("OpenClaw sent an invalid authenticated response")
        raw_token = auth.get("deviceToken")
        issued_token = raw_token if isinstance(raw_token, str) and raw_token else None
        sanitized_auth = {
            key: value
            for key, value in auth.items()
            if key not in {"deviceToken", "deviceTokens"}
        }
        sanitized_payload = dict(payload)
        sanitized_payload["auth"] = sanitized_auth
        return sanitized_payload, issued_token
    error = _record(frame.get("error"))
    details = _record(error.get("details")) if error else None
    detail_code = details.get("code") if details else None
    if detail_code == "PAIRING_REQUIRED":
        raw_request_id = details.get("requestId") if details else None
        pairing_request_id = (
            raw_request_id
            if isinstance(raw_request_id, str)
            and PAIRING_REQUEST_ID_PATTERN.fullmatch(raw_request_id)
            else None
        )
        raw_reason = details.get("reason") if details else None
        reason = raw_reason if isinstance(raw_reason, str) else None
        raise OpenClawPairingRequired(request_id=pairing_request_id, reason=reason)
    raise OpenClawGatewayError("OpenClaw rejected the connection")


def _is_string_list(value: object) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, str) and bool(item) for item in value
    )


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
