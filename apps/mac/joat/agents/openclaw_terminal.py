"""Dedicated, no-tool OpenClaw Brainstorm Agent Terminal session.

This Brainstorm boundary never adopts OpenClaw's main/Telegram session and
never provides a delivery route. It refuses to send unless the dedicated
agent's effective tool inventory is empty at the moment of the turn.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, cast
from uuid import uuid4

from .openclaw_gateway import (
    AgentTerminalPolicy,
    BRAINSTORM_TERMINAL_POLICY,
    OpenClawConnectionClosed,
    OpenClawEventSequenceGap,
    OpenClawGatewayError,
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
REQUIRED_EVENTS = frozenset({"agent", "chat", "session.message"})
AGENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SESSION_ROUTE_FIELDS = frozenset(
    {
        "channel",
        "deliveryContext",
        "groupChannel",
        "groupId",
        "lastAccountId",
        "lastChannel",
        "lastThreadId",
        "lastTo",
        "origin",
        "space",
        "subject",
    }
)
INTERNAL_WEBCHAT_PROVENANCE_FIELDS = frozenset(
    {"deliveryContext", "lastChannel", "origin"}
)
ABORT_CONFIRMATION_TIMEOUT_S = 1.0


class OpenClawToolInventoryUnsettled(OpenClawGatewayError):
    """The remote no-tool proof is incomplete and the session stays blocked."""


class OpenClawToolsEnabled(OpenClawGatewayError):
    """The dedicated remote agent exposes at least one effective tool."""


class AgentTerminalConnection(Protocol):
    @property
    def advertised_methods(self) -> frozenset[str]: ...

    @property
    def advertised_events(self) -> frozenset[str]: ...

    @property
    def granted_scopes(self) -> tuple[str, ...]: ...

    async def request(self, method: str, params: dict[str, object]) -> object: ...

    async def next_event(self) -> dict[str, object]: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RecoveredRun:
    connection: AgentTerminalConnection
    text: str
    active: bool
    complete: bool


class AgentTerminalRecovery(Protocol):
    async def recover(
        self,
        *,
        connection: AgentTerminalConnection,
        agent_id: str,
        session_key: str,
        run_id: str,
        prompt: str,
    ) -> RecoveredRun: ...


@dataclass(slots=True)
class OpenClawAgentTerminal:
    connection: AgentTerminalConnection
    agent_id: str
    session_key: str
    session_id: str
    policy: AgentTerminalPolicy = BRAINSTORM_TERMINAL_POLICY
    recovery: AgentTerminalRecovery | None = None

    @classmethod
    async def create(
        cls,
        *,
        connection: AgentTerminalConnection,
        agent_id: str,
        policy: AgentTerminalPolicy = BRAINSTORM_TERMINAL_POLICY,
        recovery: AgentTerminalRecovery | None = None,
    ) -> OpenClawAgentTerminal:
        if not AGENT_ID_PATTERN.fullmatch(agent_id):
            raise ValueError("OpenClaw Agent Terminal requires a valid dedicated agent ID")
        if agent_id != policy.agent_id:
            raise ValueError("OpenClaw Agent Terminal agent does not match its policy")
        _assert_connection_contract(connection, require_write=True)
        listed = _record(await connection.request(
            "sessions.list",
            {
                "label": policy.session_label,
                "agentId": agent_id,
                "limit": 2,
            },
        ))
        raw_sessions = listed.get("sessions") if listed else None
        if not isinstance(raw_sessions, list) or len(raw_sessions) > 1:
            raise OpenClawGatewayError(
                "OpenClaw did not identify one dedicated Agent Terminal session"
            )
        if raw_sessions:
            existing = _record(raw_sessions[0])
            session_key = existing.get("key") if existing else None
            session_id = existing.get("sessionId") if existing else None
            # OpenClaw 2026.7.1 derives the agent from the session key when it
            # filters this request, but its list/describe row omits agentId.
            # The owned key below and tools.effective's returned agentId form
            # the observable binding available in that exact protocol.
            if (
                existing is None
                or not isinstance(session_key, str)
                or not policy.owns_session_key(session_key)
                or not isinstance(session_id, str)
                or not session_id
                or existing.get("label") != policy.session_label
            ):
                raise OpenClawGatewayError(
                    "OpenClaw returned an invalid Agent Terminal session"
                )
        else:
            requested_key = policy.new_session_key(uuid4().hex)
            created = _record(await connection.request(
                "sessions.create",
                {
                    "key": requested_key,
                    "agentId": agent_id,
                    "label": policy.session_label,
                },
            ))
            session_key = created.get("key") if created else None
            session_id = created.get("sessionId") if created else None
            entry = _record(created.get("entry")) if created else None
            if (
                created is None
                or created.get("ok") is not True
                or not isinstance(session_key, str)
                or session_key != requested_key
                or not isinstance(session_id, str)
                or not session_id
                or created.get("runStarted") is not False
                or entry is None
                or entry.get("sessionId") != session_id
                or entry.get("label") != policy.session_label
                or not _session_has_no_route(
                    entry,
                    provenance_label=policy.client_display_name,
                )
            ):
                raise OpenClawGatewayError(
                    "OpenClaw did not create an isolated Agent Terminal session"
                )
        terminal = cls(
            connection=connection,
            agent_id=agent_id,
            session_key=session_key,
            session_id=session_id,
            policy=policy,
            recovery=recovery,
        )
        await terminal._assert_isolated_session()
        await terminal._assert_tools_disabled()
        subscribed = _record(await connection.request(
            "sessions.messages.subscribe",
            {"key": session_key, "agentId": agent_id},
        ))
        if (
            subscribed is None
            or subscribed.get("subscribed") is not True
            or subscribed.get("key") != session_key
        ):
            raise OpenClawGatewayError(
                "OpenClaw did not confirm the Agent Terminal subscription"
            )
        return terminal

    @classmethod
    async def attest_existing(
        cls,
        *,
        connection: AgentTerminalConnection,
        agent_id: str,
        policy: AgentTerminalPolicy = BRAINSTORM_TERMINAL_POLICY,
    ) -> None:
        """Freshly prove one existing terminal without reading or changing it.

        This deliberately stops before subscription: a wearer-triggered retry
        must establish that the already-created Hermes seat is still isolated
        and tool-free, but it must never create a session, subscribe to its
        messages, read history, or send a prompt.
        """
        if not AGENT_ID_PATTERN.fullmatch(agent_id):
            raise ValueError("OpenClaw Agent Terminal requires a valid dedicated agent ID")
        if agent_id != policy.agent_id:
            raise ValueError("OpenClaw Agent Terminal agent does not match its policy")
        _assert_connection_contract(connection, require_write=False)
        listed = _record(await connection.request(
            "sessions.list",
            {
                "label": policy.session_label,
                "agentId": agent_id,
                "limit": 2,
            },
        ))
        raw_sessions = listed.get("sessions") if listed else None
        if not isinstance(raw_sessions, list) or len(raw_sessions) != 1:
            raise OpenClawGatewayError(
                "OpenClaw did not identify one dedicated Agent Terminal session"
            )
        existing = _record(raw_sessions[0])
        session_key = existing.get("key") if existing else None
        session_id = existing.get("sessionId") if existing else None
        if (
            existing is None
            or not isinstance(session_key, str)
            or not policy.owns_session_key(session_key)
            or not isinstance(session_id, str)
            or not session_id
            or existing.get("label") != policy.session_label
        ):
            raise OpenClawGatewayError(
                "OpenClaw returned an invalid Agent Terminal session"
            )
        terminal = cls(
            connection=connection,
            agent_id=agent_id,
            session_key=session_key,
            session_id=session_id,
            policy=policy,
        )
        await terminal._assert_isolated_session()
        await terminal._assert_tools_disabled()

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        if not prompt or len(prompt) > 100_000:
            raise ValueError("OpenClaw Agent Terminal prompt is invalid")
        await self._assert_isolated_session()
        await self._assert_tools_disabled()
        sent = _record(await self.connection.request(
            "sessions.send",
            {
                "key": self.session_key,
                "agentId": self.agent_id,
                "message": prompt,
                "idempotencyKey": uuid4().hex,
            },
        ))
        run_id = sent.get("runId") if sent else None
        if not isinstance(run_id, str) or not run_id:
            raise OpenClawGatewayError("OpenClaw did not acknowledge the Agent Terminal run")

        accumulated = ""
        last_seq: int | None = None
        terminal_received = False
        raised: BaseException | None = None
        try:
            while True:
                try:
                    frame = await self.connection.next_event()
                except (
                    OpenClawConnectionClosed,
                    OpenClawEventSequenceGap,
                ):
                    if self.recovery is None:
                        raise
                    suffix, accumulated, complete = await self._recover_run(
                        run_id=run_id,
                        prompt=prompt,
                        accumulated=accumulated,
                    )
                    last_seq = None
                    if suffix:
                        yield suffix
                    if complete:
                        terminal_received = True
                        return
                    continue
                event_name = frame.get("event")
                payload = _record(frame.get("payload"))
                owned_event = (
                    payload is not None
                    and payload.get("runId") == run_id
                    and payload.get("sessionKey") == self.session_key
                )
                if payload is not None and owned_event and (
                    event_name == "agent" and payload.get("stream") == "tool"
                    or isinstance(event_name, str)
                    and (
                        "approval" in event_name.lower()
                        or "tool" in event_name.lower()
                    )
                ):
                    raise OpenClawGatewayError(
                        "OpenClaw attempted a blocked Agent Terminal capability"
                    )
                if event_name != "chat":
                    continue
                if (
                    payload is None
                    or payload.get("runId") != run_id
                    or payload.get("sessionKey") != self.session_key
                ):
                    continue
                seq = payload.get("seq")
                if (
                    not isinstance(seq, int)
                    or isinstance(seq, bool)
                    or seq < 0
                ):
                    raise OpenClawGatewayError(
                        "OpenClaw sent an invalid Agent Terminal event sequence"
                    )
                state = payload.get("state")
                if last_seq is not None and (
                    seq < last_seq
                    or seq == last_seq and state == "delta"
                ):
                    continue
                last_seq = seq
                if state == "delta":
                    delta = payload.get("deltaText")
                    if not isinstance(delta, str):
                        raise OpenClawGatewayError(
                            "OpenClaw sent an invalid Agent Terminal delta"
                        )
                    if payload.get("replace") is True:
                        if not delta.startswith(accumulated):
                            raise OpenClawGatewayError(
                                "OpenClaw Agent Terminal replacement diverged"
                            )
                        suffix = delta[len(accumulated):]
                        accumulated = delta
                        if suffix:
                            yield suffix
                    else:
                        accumulated += delta
                        if delta:
                            yield delta
                    continue
                if state == "final":
                    message = payload.get("message")
                    if message is not None:
                        final_text = _assistant_text(message)
                        if final_text is None:
                            raise OpenClawGatewayError(
                                "OpenClaw sent an invalid final Agent Terminal message"
                            )
                        if not final_text.startswith(accumulated):
                            raise OpenClawGatewayError(
                                "OpenClaw final Agent Terminal message diverged"
                            )
                        suffix = final_text[len(accumulated):]
                        if suffix:
                            yield suffix
                    terminal_received = True
                    return
                if state in {"aborted", "error"}:
                    terminal_received = True
                    raise OpenClawGatewayError(
                        "OpenClaw Agent Terminal run did not complete"
                    )
        except BaseException as exc:
            raised = exc
            raise
        finally:
            if not terminal_received:
                try:
                    async with asyncio.timeout(ABORT_CONFIRMATION_TIMEOUT_S):
                        await self.abort(run_id=run_id)
                except TimeoutError as exc:
                    if raised is None or isinstance(
                        raised, (asyncio.CancelledError, GeneratorExit),
                    ):
                        raise OpenClawGatewayError(
                            "OpenClaw did not confirm the Agent Terminal abort"
                        ) from exc
                except OpenClawGatewayError:
                    if raised is None or isinstance(
                        raised, (asyncio.CancelledError, GeneratorExit),
                    ):
                        raise

    async def _recover_run(
        self,
        *,
        run_id: str,
        prompt: str,
        accumulated: str,
    ) -> tuple[str, str, bool]:
        recovery = self.recovery
        if recovery is None:
            raise OpenClawGatewayError(
                "OpenClaw Agent Terminal recovery is unavailable"
            )
        recovered = await recovery.recover(
            connection=self.connection,
            agent_id=self.agent_id,
            session_key=self.session_key,
            run_id=run_id,
            prompt=prompt,
        )
        _assert_connection_contract(recovered.connection, require_write=True)
        if recovered.active == recovered.complete:
            raise OpenClawGatewayError(
                "OpenClaw returned an invalid recovered run state"
            )
        if not recovered.text.startswith(accumulated):
            raise OpenClawGatewayError(
                "OpenClaw recovered Agent Terminal text diverged"
            )
        self.connection = recovered.connection
        await self._assert_isolated_session()
        await self._assert_tools_disabled()
        suffix = recovered.text[len(accumulated):]
        return suffix, recovered.text, recovered.complete

    async def abort(self, *, run_id: str) -> None:
        result = _record(await self.connection.request(
            "sessions.abort",
            {
                "key": self.session_key,
                "runId": run_id,
                "agentId": self.agent_id,
            },
        ))
        status = result.get("status") if result else None
        aborted_run_id = result.get("abortedRunId") if result else None
        confirmed = (
            result is not None
            and result.get("ok") is True
            and (
                (status == "aborted" and aborted_run_id == run_id)
                or (status == "no-active-run" and aborted_run_id is None)
            )
        )
        if not confirmed:
            raise OpenClawGatewayError(
                "OpenClaw did not confirm the Agent Terminal abort"
            )

    async def _assert_isolated_session(self) -> None:
        described = _record(await self.connection.request(
            "sessions.describe",
            {"key": self.session_key},
        ))
        session = _record(described.get("session")) if described else None
        if (
            session is None
            or session.get("key") != self.session_key
            or session.get("sessionId") != self.session_id
            or session.get("label") != self.policy.session_label
            or not _is_empty_route_value(session.get("parentSessionKey"))
            or not _is_empty_route_value(session.get("spawnedBy"))
            or not _session_has_no_route(
                session,
                provenance_label=self.policy.client_display_name,
            )
        ):
            raise OpenClawGatewayError(
                "OpenClaw Agent Terminal session is not isolated from delivery routes"
            )

    async def _assert_tools_disabled(self) -> None:
        inventory = _record(await self.connection.request(
            "tools.effective",
            {"sessionKey": self.session_key, "agentId": self.agent_id},
        ))
        groups = inventory.get("groups") if inventory else None
        notices = inventory.get("notices", []) if inventory else None
        if (
            inventory is None
            or inventory.get("agentId") != self.agent_id
            or not isinstance(groups, list)
            or not isinstance(notices, list)
        ):
            raise OpenClawGatewayError(
                "OpenClaw returned an invalid effective tool inventory"
            )
        if notices:
            raise OpenClawToolInventoryUnsettled(
                "OpenClaw effective tool inventory is unsettled"
            )
        for group in groups:
            record = _record(group)
            tools = record.get("tools") if record else None
            if not isinstance(tools, list):
                raise OpenClawGatewayError(
                    "OpenClaw returned an invalid effective tool inventory"
                )
            if tools:
                raise OpenClawToolsEnabled(
                    "OpenClaw Agent Terminal tools are enabled"
                )


def _record(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return None
    return cast(dict[str, object], value)


def _session_has_no_route(
    session: dict[str, object],
    *,
    provenance_label: str,
) -> bool:
    if all(_is_empty_route_value(session.get(field)) for field in SESSION_ROUTE_FIELDS):
        return True
    if any(
        not _is_empty_route_value(session.get(field))
        for field in SESSION_ROUTE_FIELDS - INTERNAL_WEBCHAT_PROVENANCE_FIELDS
    ):
        return False
    return (
        session.get("deliveryContext") == {"channel": "webchat"}
        and session.get("lastChannel") == "webchat"
        and session.get("origin") == {
            "provider": "webchat",
            "surface": "webchat",
            "chatType": "direct",
            "label": provenance_label,
        }
    )


def _is_empty_route_value(value: object) -> bool:
    return value is None or value == ""


def _assert_connection_contract(
    connection: AgentTerminalConnection,
    *,
    require_write: bool,
) -> None:
    """Require only the protocol surface and authority this operation uses."""
    required_methods = (
        REQUIRED_METHODS
        if require_write
        else frozenset({"sessions.list", "sessions.describe", "tools.effective"})
    )
    if not required_methods.issubset(connection.advertised_methods):
        raise OpenClawGatewayError(
            "OpenClaw is missing required Agent Terminal methods"
        )
    if require_write and not REQUIRED_EVENTS.issubset(connection.advertised_events):
        raise OpenClawGatewayError(
            "OpenClaw is missing required Agent Terminal events"
        )
    expected_scopes = (
        {"operator.read", "operator.write"}
        if require_write
        else {"operator.read"}
    )
    if set(connection.granted_scopes) != expected_scopes:
        raise OpenClawGatewayError(
            "OpenClaw Agent Terminal requires exact "
            f"{'read and write' if require_write else 'read-only'} scopes"
        )


def _assistant_text(value: object) -> str | None:
    message = _record(value)
    if message is None or message.get("role") != "assistant":
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for item in content:
        block = _record(item)
        if block is None:
            return None
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            return None
        parts.append(text)
    return "".join(parts)
