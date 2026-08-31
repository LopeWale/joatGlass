"""Lifecycle owner for one isolated OpenClaw Agent Terminal.

It adopts only one exact, JOAT-labelled session for the dedicated agent after
re-validating its identity, routing, and effective tools.  Reconnect never
re-sends an acknowledged prompt: it re-subscribes and reloads authoritative
history for that exact key.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlsplit

from .openclaw_gateway import (
    AGENT_TERMINAL_AGENT_ID,
    AgentTerminalPolicy,
    BRAINSTORM_TERMINAL_POLICY,
    DeviceIdentity,
    DeviceTokenStore,
    GatewayProfile,
    OpenClawConnectionClosed,
    OpenClawGatewayClient,
    OpenClawGatewayError,
    WORK_TERMINAL_POLICY,
)
from .openclaw_terminal import (
    AgentTerminalConnection,
    OpenClawAgentTerminal,
    OpenClawToolInventoryUnsettled,
    OpenClawToolsEnabled,
    PendingApproval,
    RecoveredRun,
)


DEFAULT_OPENCLAW_CONFIG_PATH = (
    Path.home() / "Library/Application Support/JOAT/openclaw.json"
)
CONNECTION_CLOSE_TIMEOUT_S = 1.0
# Hermes exposes its broad browser control dashboard on this port.  JOAT must
# connect only to OpenClaw's authenticated Gateway, never that dashboard.
HERMES_CONTROL_DASHBOARD_PORT = 9443


def terminal_ready_activity(*, reverified: bool) -> str:
    """Return the short wearable confirmation for terminal readiness."""
    if reverified:
        return "Dedicated no-tool session re-verified. Tap to begin a Brainstorm."
    return "Dedicated no-tool session ready. Tap to begin a Brainstorm."


class OpenClawHistoryRecoveryRequired(OpenClawGatewayError):
    """The exact run outcome cannot be attributed after transport loss."""


@dataclass(frozen=True, slots=True)
class OpenClawRuntimeConfig:
    profile: GatewayProfile
    agent_id: str

    @classmethod
    def load(
        cls,
        path: Path = DEFAULT_OPENCLAW_CONFIG_PATH,
    ) -> OpenClawRuntimeConfig | None:
        """Load a small, secret-free, owner-only Gateway binding."""
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size < 2
            or info.st_size > 65_536
        ):
            raise ValueError("OpenClaw runtime config is not owner-only")
        try:
            raw: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("OpenClaw runtime config is invalid") from exc
        if not isinstance(raw, dict) or set(raw) != {"gateway_url", "agent_id"}:
            raise ValueError("OpenClaw runtime config is invalid")
        gateway_url = raw.get("gateway_url")
        agent_id = raw.get("agent_id")
        if (
            not isinstance(gateway_url, str)
            or not isinstance(agent_id, str)
            or agent_id != AGENT_TERMINAL_AGENT_ID
        ):
            raise ValueError("OpenClaw runtime config is invalid")
        profile = GatewayProfile(gateway_url)
        if urlsplit(profile.url).port == HERMES_CONTROL_DASHBOARD_PORT:
            raise ValueError(
                "OpenClaw runtime config must point to the Gateway, not the Hermes dashboard"
            )
        return cls(profile=profile, agent_id=agent_id)


class OpenClawConnectionFactory(Protocol):
    async def connect(self) -> AgentTerminalConnection: ...


class OpenClawCredentials(DeviceIdentity, DeviceTokenStore, Protocol):
    """The paired device identity and its Keychain-backed token boundary."""


def make_brainstorm_terminal_agent(
    *,
    profile: GatewayProfile,
    credentials: OpenClawCredentials,
    agent_id: str,
    state_changed: Callable[[str, str], None] | None = None,
) -> OpenClawTerminalAgent:
    """Build the write terminal plus its separately scoped proof client."""
    terminal_client = OpenClawGatewayClient(
        profile=profile,
        identity=credentials,
        token_store=credentials,
        scopes=("operator.read", "operator.write"),
        capabilities=("tool-events",),
        policy=BRAINSTORM_TERMINAL_POLICY,
    )
    attestation_client = OpenClawGatewayClient(
        profile=profile,
        identity=credentials,
        token_store=credentials,
        scopes=("operator.read",),
        policy=BRAINSTORM_TERMINAL_POLICY,
    )
    return OpenClawTerminalAgent(
        client=terminal_client,
        attestation_client=attestation_client,
        agent_id=agent_id,
        policy=BRAINSTORM_TERMINAL_POLICY,
        state_changed=state_changed,
    )


def make_work_terminal_agent(
    *,
    profile: GatewayProfile,
    credentials: OpenClawCredentials,
    agent_id: str,
    state_changed: Callable[[str, str], None] | None = None,
    on_approval: Callable[[PendingApproval], None] | None = None,
) -> OpenClawTerminalAgent:
    """The Agent Terminal work lane: approvals in, roster reads allowed.

    Same shape as the Brainstorm factory — dedicated write client plus a
    read-only proof client — with two differences that are the whole
    point: WORK_TERMINAL_POLICY (allows_approvals, roster) and the
    operator.approvals scope, which the validator only accepts riding on
    read+write.
    """
    terminal_client = OpenClawGatewayClient(
        profile=profile,
        identity=credentials,
        token_store=credentials,
        scopes=("operator.read", "operator.write", "operator.approvals"),
        capabilities=("tool-events",),
        policy=WORK_TERMINAL_POLICY,
    )
    attestation_client = OpenClawGatewayClient(
        profile=profile,
        identity=credentials,
        token_store=credentials,
        scopes=("operator.read",),
        policy=WORK_TERMINAL_POLICY,
    )
    return OpenClawTerminalAgent(
        client=terminal_client,
        attestation_client=attestation_client,
        agent_id=agent_id,
        policy=WORK_TERMINAL_POLICY,
        state_changed=state_changed,
        on_approval=on_approval,
    )


@dataclass(slots=True)
class OpenClawHistoryRecovery:
    client: OpenClawConnectionFactory
    policy: AgentTerminalPolicy = BRAINSTORM_TERMINAL_POLICY
    attempts: int = 3
    backoff_s: tuple[float, ...] = (0.0, 0.25, 1.0)
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    def __post_init__(self) -> None:
        if self.attempts < 1 or len(self.backoff_s) < self.attempts:
            raise ValueError("OpenClaw reconnect policy is invalid")

    async def recover(
        self,
        *,
        connection: AgentTerminalConnection,
        agent_id: str,
        session_key: str,
        run_id: str,
        prompt: str,
    ) -> RecoveredRun:
        """Reconnect once logically; retry transport establishment only."""

        await _safe_close(connection)
        recovered = await self._connect_with_backoff()
        try:
            if agent_id != self.policy.agent_id or not self.policy.owns_session_key(session_key):
                raise OpenClawGatewayError("OpenClaw recovery does not own the Agent Terminal session")
            subscribed = _record(await recovered.request(
                "sessions.subscribe",
                {
                    "label": self.policy.session_label,
                    "agentId": agent_id,
                    "limit": 2,
                },
            ))
            if subscribed is None or subscribed.get("subscribed") is not True:
                raise OpenClawGatewayError(
                    "OpenClaw did not restore the session catalog subscription"
                )
            snapshot = _record(subscribed.get("list"))
            sessions = snapshot.get("sessions") if snapshot else None
            if not isinstance(sessions, list) or len(sessions) != 1:
                raise OpenClawGatewayError(
                    "OpenClaw did not restore an exact Agent Terminal catalog snapshot"
                )
            catalog_session = _record(sessions[0])
            if (
                catalog_session is None
                or catalog_session.get("key") != session_key
                or catalog_session.get("label") != self.policy.session_label
            ):
                raise OpenClawGatewayError(
                    "OpenClaw catalog snapshot did not preserve the Agent Terminal session"
                )
            messages_subscribed = _record(await recovered.request(
                "sessions.messages.subscribe",
                {"key": session_key, "agentId": agent_id},
            ))
            if (
                messages_subscribed is None
                or messages_subscribed.get("subscribed") is not True
                or messages_subscribed.get("key") != session_key
            ):
                raise OpenClawGatewayError(
                    "OpenClaw did not restore the Agent Terminal subscription"
                )
            history = _record(await recovered.request(
                "chat.history",
                {"sessionKey": session_key},
            ))
            text, active, complete = _reconcile_history(
                history,
                run_id=run_id,
                prompt=prompt,
            )
        except BaseException:
            await _safe_close(recovered)
            raise
        return RecoveredRun(
            connection=recovered,
            text=text,
            active=active,
            complete=complete,
        )

    async def _connect_with_backoff(self) -> AgentTerminalConnection:
        last_error: BaseException | None = None
        for index in range(self.attempts):
            delay = self.backoff_s[index]
            if delay:
                await self.sleep(delay)
            try:
                return await self.client.connect()
            except (OpenClawConnectionClosed, OSError) as exc:
                last_error = exc
        raise OpenClawConnectionClosed(
            "OpenClaw reconnect attempts were exhausted"
        ) from last_error


@dataclass(slots=True)
class OpenClawTerminalAgent:
    """Reply-engine adapter with one serialized dedicated session."""

    client: OpenClawConnectionFactory
    agent_id: str
    # The persistent terminal needs write authority to send a Brainstorm turn.
    # A wearer-selected re-attestation must never reuse that client: it proves
    # the existing terminal through a fresh operator.read-only connection.
    attestation_client: OpenClawConnectionFactory | None = None
    policy: AgentTerminalPolicy = BRAINSTORM_TERMINAL_POLICY
    state_changed: Callable[[str, str], None] | None = None
    on_approval: Callable[[PendingApproval], None] | None = None
    _terminal: OpenClawAgentTerminal | None = field(default=None, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _closed: bool = field(default=False, init=False)
    _stream_task: asyncio.Task[object] | None = field(default=None, init=False)

    @property
    def initialized(self) -> bool:
        """Whether normal setup already owns a subscribed terminal connection."""
        return self._terminal is not None

    async def initialize(self) -> None:
        if self._closed:
            raise OpenClawGatewayError("OpenClaw Agent Terminal is closed")
        if self._terminal is not None:
            return
        connection = await self.client.connect()
        if self._closed:
            await _safe_close(connection)
            raise OpenClawGatewayError("OpenClaw Agent Terminal is closed")
        recovery = OpenClawHistoryRecovery(client=self.client, policy=self.policy)
        try:
            self._terminal = await OpenClawAgentTerminal.create(
                connection=connection,
                agent_id=self.agent_id,
                policy=self.policy,
                recovery=recovery,
                on_approval=self.on_approval,
            )
        except BaseException:
            await _safe_close(connection)
            raise

    async def attest_existing(self) -> None:
        """Use a fresh, read-only Gateway connection to re-prove the terminal.

        The persistent connection remains untouched.  This operation is used
        only after the wearer asks to retry an already-ready Agent Terminal.
        """
        async with self._lock:
            if self._closed:
                raise OpenClawGatewayError("OpenClaw Agent Terminal is closed")
            if self._terminal is None:
                raise OpenClawGatewayError(
                    "OpenClaw Agent Terminal must be initialized before attestation"
                )
            attestation_client = self.attestation_client
            if attestation_client is None:
                raise OpenClawGatewayError(
                    "OpenClaw Agent Terminal has no read-only attestation client"
                )
            connection = await attestation_client.connect()
            if self._closed:
                await _safe_close(connection)
                raise OpenClawGatewayError("OpenClaw Agent Terminal is closed")
            try:
                await OpenClawAgentTerminal.attest_existing(
                    connection=connection,
                    agent_id=self.agent_id,
                    policy=self.policy,
                )
            finally:
                await _safe_close(connection)

    async def stream(self, prompt: str) -> AsyncGenerator[str, None]:
        async with self._lock:
            if self._closed:
                raise OpenClawGatewayError("OpenClaw Agent Terminal is closed")
            current_task = asyncio.current_task()
            self._stream_task = current_task
            try:
                await self.initialize()
                terminal = self._terminal
                if terminal is None:  # defensive: initialize either sets or raises
                    raise OpenClawGatewayError(
                        "OpenClaw Agent Terminal is unavailable"
                    )
                self._set_state("running", "")
                async for chunk in terminal.stream(prompt):
                    yield chunk
            except OpenClawToolsEnabled:
                await self._invalidate_terminal()
                self._set_state("blocked", "tools_enabled")
                raise
            except OpenClawToolInventoryUnsettled:
                await self._invalidate_terminal()
                self._set_state("blocked", "dedicated_session_not_proven")
                raise
            except OpenClawHistoryRecoveryRequired:
                await self._invalidate_terminal()
                self._set_state("blocked", "history_recovery_required")
                raise
            except (OpenClawConnectionClosed, OSError):
                await self._invalidate_terminal()
                self._set_state("blocked", "connection_closed")
                raise
            except OpenClawGatewayError:
                await self._invalidate_terminal()
                self._set_state("blocked", "protocol_rejected")
                raise
            except (asyncio.CancelledError, GeneratorExit):
                await self._invalidate_terminal()
                self._set_state("blocked", "history_recovery_required")
                raise
            else:
                self._set_state("ready", "")
            finally:
                if self._stream_task is current_task:
                    self._stream_task = None

    async def resolve_approval(
        self, approval_id: str, kind: str, decision: str
    ) -> str | None:
        """Answer a pending approval; returns the RECORDED decision.

        First answer wins on the gateway, so the receipt may differ from
        what we sent — the caller reports what was recorded, never what it
        merely asked for.
        """
        async with self._lock:
            if self._closed:
                raise OpenClawGatewayError("OpenClaw Agent Terminal is closed")
            await self.initialize()
            terminal = self._terminal
            if terminal is None:
                raise OpenClawGatewayError("OpenClaw Agent Terminal is unavailable")
            return await terminal.resolve_approval(approval_id, kind, decision)

    async def close(self) -> None:
        self._closed = True
        stream_task = self._stream_task
        current_task = asyncio.current_task()
        if stream_task is not None and stream_task is not current_task:
            stream_task.cancel()
            with suppress(asyncio.CancelledError):
                await stream_task
        if stream_task is current_task:
            await self._invalidate_terminal()
            return
        async with self._lock:
            await self._invalidate_terminal()

    async def _invalidate_terminal(self) -> None:
        terminal, self._terminal = self._terminal, None
        if terminal is not None:
            terminal.recovery = None
            await _safe_close(terminal.connection)

    def _set_state(self, state: str, reason: str) -> None:
        if self.state_changed is not None:
            self.state_changed(state, reason)


def _reconcile_history(
    history: dict[str, object] | None,
    *,
    run_id: str,
    prompt: str,
) -> tuple[str, bool, bool]:
    if history is None:
        raise OpenClawGatewayError("OpenClaw returned invalid Agent Terminal history")
    messages = history.get("messages")
    session_info = _record(history.get("sessionInfo"))
    in_flight = _record(history.get("inFlightRun"))
    if not isinstance(messages, list) or session_info is None:
        raise OpenClawGatewayError("OpenClaw returned invalid Agent Terminal history")
    active_ids_raw = session_info.get("activeRunIds")
    if active_ids_raw is not None and (
        not isinstance(active_ids_raw, list)
        or not all(isinstance(item, str) and item for item in active_ids_raw)
    ):
        raise OpenClawGatewayError("OpenClaw returned invalid active run history")
    active_ids = cast(list[str] | None, active_ids_raw)
    has_active = session_info.get("hasActiveRun")
    if not isinstance(has_active, bool):
        raise OpenClawGatewayError("OpenClaw returned invalid active run history")

    if in_flight is not None:
        recovered_id = in_flight.get("runId")
        text = in_flight.get("text")
        if (
            recovered_id != run_id
            or not isinstance(text, str)
            or not has_active
        ):
            raise OpenClawGatewayError(
                "OpenClaw history belongs to a different in-flight run"
            )
        if active_ids is not None and run_id not in active_ids:
            raise OpenClawGatewayError(
                "OpenClaw in-flight run is absent from the active run set"
            )
        return text, True, False

    if has_active:
        raise OpenClawHistoryRecoveryRequired(
            "OpenClaw active run is missing its recoverable text snapshot"
        )
    raise OpenClawHistoryRecoveryRequired(
        "OpenClaw completed the Agent Terminal run while disconnected; outcome is uncertain"
    )


async def _safe_close(connection: AgentTerminalConnection) -> None:
    with suppress(OpenClawGatewayError, OSError, TimeoutError):
        async with asyncio.timeout(CONNECTION_CLOSE_TIMEOUT_S):
            await connection.close()


def _record(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return None
    return cast(dict[str, object], value)
