"""Dedicated, no-tool OpenClaw Brainstorm Agent Terminal session.

This Brainstorm boundary never adopts OpenClaw's main/Telegram session and
never provides a delivery route. It refuses to send unless the dedicated
agent's effective tool inventory is empty at the moment of the turn.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Literal, Protocol, cast
from uuid import uuid4

from .openclaw_gateway import (
    AgentTerminalPolicy,
    BRAINSTORM_TERMINAL_POLICY,
    OpenClawConnectionClosed,
    OpenClawEventSequenceGap,
    OpenClawGatewayError,
)


log = logging.getLogger(__name__)
# Official tools.effective notices. Unknown provider text is never diagnostic
# output: it may contain private MCP names, connection details, or credentials.
# https://docs.openclaw.ai/gateway/protocol#operator-helper-methods
# Wire field: EffectiveToolInventoryNotice.id (OpenClaw v2026.7.1 and main),
# src/agents/tools-effective-inventory.types.ts; message/server text stays private.
# IDs: src/agents/tools-effective-inventory.ts and
# src/gateway/server-methods/tools-effective.ts at those same revisions.
TOOL_INVENTORY_NOTICE_CODES = frozenset({
    "mcp-not-yet-connected", "mcp-not-yet-listed", "mcp-stale-catalog",
    "browser-denied-by-policy", "browser-filtered-by-profile", "browser-plugin-not-allowed",
})
# Both tools-effective-inventory-build.ts and tools-effective-mcp-inventory.ts
# emit unsupported-tool-schema:<toolName>. Only the fixed family is loggable.
TOOL_SCHEMA_NOTICE_PREFIX = "unsupported-tool-schema:"
# These notices explain a completed exclusion, not missing inventory. Their
# exact severities come from buildToolInventoryNotices in OpenClaw v2026.7.1
# src/agents/tools-effective-inventory.ts. MCP notices also use "info", so
# accepting by severity alone would incorrectly admit undiscovered tools.
SETTLED_TOOL_EXCLUSIONS = {
    "browser-filtered-by-profile": "info",
    "browser-denied-by-policy": "info",
    "browser-plugin-not-allowed": "warning",
}
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
# The work lane additionally requires the documented approval surface
# (docs.openclaw.ai/gateway/protocol): the session.approval event and the
# approval.resolve RPC. Only a policy with allows_approvals asks for them.
APPROVAL_EVENT = "session.approval"
APPROVAL_METHODS = frozenset({"approval.resolve"})


@dataclass(frozen=True, slots=True)
class PendingApproval:
    """One tool call a work session is waiting on the wearer to answer."""

    approval_id: str
    kind: str
    summary: str
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


# OpenClaw v2026.7.1 packages/gateway-protocol/src/schema/logs-chat.ts
# defines these coarse error kinds. Free-form provider errorMessage/message
# fields are never exception text or log data in JOAT.
_RUN_ERROR_MESSAGES = {
    "refusal": "OpenClaw declined this instruction.",
    "timeout": "OpenClaw run timed out. Try again.",
    "rate_limit": "OpenClaw provider rate limit reached. Try again later.",
    "context_length": "OpenClaw session context is too long. Shorten the instruction or start a new session.",
    "unknown": "OpenClaw run failed. Check OpenClaw for details, then try again.",
}
# src/agents/run-termination.ts and agent-run-terminal-outcome.ts document
# these stop literals. The wire permits other strings; keep those private.
_RUN_STOP_REASONS = frozenset({"aborted", "restart", "timeout", "rpc", "stop"})


class OpenClawRunFailed(OpenClawGatewayError):
    """An owned run ended unsuccessfully without revoking its connection."""

    def __init__(
        self, state: Literal["error", "aborted"], *, kind: str, stop_reason: str,
    ) -> None:
        self.state = state
        self.kind = kind if kind in _RUN_ERROR_MESSAGES else "unknown"
        self.stop_reason = stop_reason if stop_reason in _RUN_STOP_REASONS else "unknown"
        if state == "aborted":
            if self.stop_reason == "restart":
                message = "OpenClaw interrupted this run for restart. Try again when ready."
            elif self.stop_reason in {"rpc", "stop"}:
                message = "OpenClaw run was cancelled."
            else:
                message = "OpenClaw stopped this run before completion."
        else:
            kind = "timeout" if self.kind == "unknown" and self.stop_reason == "timeout" else self.kind
            message = _RUN_ERROR_MESSAGES[kind]
        super().__init__(message)


def _run_failure(
    payload: dict[str, object], state: Literal["error", "aborted"],
) -> OpenClawRunFailed:
    for name in ("errorMessage", "stopReason"):
        if name in payload and not isinstance(payload[name], str):
            raise OpenClawGatewayError("OpenClaw sent an invalid Agent Terminal run outcome")
    kind = payload.get("errorKind", "unknown")
    if not isinstance(kind, str) or kind not in _RUN_ERROR_MESSAGES:
        raise OpenClawGatewayError("OpenClaw sent an invalid Agent Terminal error kind")
    stop_reason = payload.get("stopReason", "unknown")
    assert isinstance(stop_reason, str)  # Validated above, including explicit null.
    return OpenClawRunFailed(state, kind=kind, stop_reason=stop_reason)


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
    on_approval: Callable[[PendingApproval], None] | None = None
    #: True when this terminal was bound to an EXISTING listed session via
    #: attach() rather than created in the lane's own namespace. An
    #: attached session is someone's real work: it has its own label, its
    #: own lineage, and legitimately has tools — so the Brainstorm-grade
    #: isolation preflight is a category error there, and running it
    #: anyway refused EVERY send into a resumed session ("resume then
    #: steer" was dead). What protects the wearer on this path is the
    #: gateway's per-connection earned-key enforcement (a roster lane may
    #: only address keys the gateway itself listed on this connection) and
    #: the approval gate.
    attached: bool = False

    @classmethod
    async def create(
        cls,
        *,
        connection: AgentTerminalConnection,
        agent_id: str,
        policy: AgentTerminalPolicy = BRAINSTORM_TERMINAL_POLICY,
        recovery: AgentTerminalRecovery | None = None,
        on_approval: Callable[[PendingApproval], None] | None = None,
    ) -> OpenClawAgentTerminal:
        if not AGENT_ID_PATTERN.fullmatch(agent_id):
            raise ValueError("OpenClaw Agent Terminal requires a valid dedicated agent ID")
        if agent_id != policy.agent_id:
            raise ValueError("OpenClaw Agent Terminal agent does not match its policy")
        _assert_connection_contract(
            connection, require_write=True, approvals=policy.allows_approvals
        )
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
            on_approval=on_approval,
        )
        await terminal._assert_isolated_session()
        await terminal._assert_tools_disabled()
        subscribe_params: dict[str, object] = {"key": session_key, "agentId": agent_id}
        if policy.allows_approvals:
            # The documented per-call opt-in; re-subscribing without the
            # flag would silently drop approval delivery.
            subscribe_params["includeApprovals"] = True
        subscribed = _record(await connection.request(
            "sessions.messages.subscribe",
            subscribe_params,
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
    async def attach(
        cls,
        *,
        connection: AgentTerminalConnection,
        agent_id: str,
        session_key: str,
        policy: AgentTerminalPolicy,
        recovery: AgentTerminalRecovery | None = None,
        on_approval: Callable[[PendingApproval], None] | None = None,
    ) -> OpenClawAgentTerminal:
        """Bind to an EXISTING session the gateway listed for this wearer.

        Resume, as distinct from create: no adopt-or-create dance and no
        namespace claim — the key's authority is that the gateway named it,
        which the connection re-checks on every request. Roster lanes only;
        any other policy is refused before a byte leaves the Mac.
        """
        if not policy.roster:
            raise OpenClawGatewayError(
                "Only an Agent Terminal roster lane may attach to a listed session"
            )
        if agent_id != policy.agent_id:
            raise ValueError("OpenClaw Agent Terminal agent does not match its policy")
        _assert_connection_contract(
            connection, require_write=True, approvals=policy.allows_approvals
        )
        terminal = cls(
            connection=connection,
            agent_id=agent_id,
            session_key=session_key,
            session_id="",
            policy=policy,
            recovery=recovery,
            on_approval=on_approval,
            attached=True,
        )
        subscribe_params: dict[str, object] = {"key": session_key, "agentId": agent_id}
        if policy.allows_approvals:
            subscribe_params["includeApprovals"] = True
        subscribed = _record(await connection.request(
            "sessions.messages.subscribe",
            subscribe_params,
        ))
        if (
            subscribed is None
            or subscribed.get("subscribed") is not True
            or subscribed.get("key") != session_key
        ):
            raise OpenClawGatewayError(
                "OpenClaw did not confirm the resumed session subscription"
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

    async def stream(self, prompt: str) -> AsyncGenerator[str, None]:
        if not prompt or len(prompt) > 100_000:
            raise ValueError("OpenClaw Agent Terminal prompt is invalid")
        if not self.attached:
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
                    if not self.policy.allows_approvals:
                        raise OpenClawGatewayError(
                            "OpenClaw attempted a blocked Agent Terminal capability"
                        )
                    # Work lane: tool traffic is the session doing its job.
                    # An approval request is routed to the wearer; other
                    # tool chatter is tolerated, never a violation.
                    if event_name == APPROVAL_EVENT:
                        approval = _parse_approval(payload)
                        if approval is not None and self.on_approval is not None:
                            self.on_approval(approval)
                    continue
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
                if state == "aborted" or state == "error":
                    failure = _run_failure(payload, state)
                    terminal_received = True
                    log.warning(
                        "OpenClaw Agent Terminal run ended state=%s kind=%s stop=%s",
                        failure.state, failure.kind, failure.stop_reason,
                    )
                    raise failure
                # A chat event that is neither text nor an ending. The live
                # gateway sends `state: "status"` mid-run (captured 2026-09-05,
                # seq=2), and the protocol reference describes the chat family
                # as carrying "transcript-only chat events" beyond deltas and
                # finals. Treating those as protocol violations killed every
                # real Hermes run at its first status line. Skipping is safe:
                # text enters ONLY through delta/final, both still guarded by
                # the divergence checks, and a stream that ends without a
                # terminal event still raises (connection closed, or the
                # recovery contract) with the run aborted in the finally —
                # so an unknown TERMINAL state degrades to a truthful
                # failure rather than a hang or a false success. The token
                # is protocol vocabulary, not
                # wearer content; bounded anyway in case a broken peer sends
                # prose.
                log.info(
                    "OpenClaw chat event skipped (informational state=%.40r seq=%s)",
                    state, seq,
                )
                continue
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
        if not self.attached:
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

    async def list_sessions(self, limit: int = 20) -> list[dict[str, str]]:
        """Enumerate the wearer's OWN gateway sessions — roster lanes only.

        The policy layer admits sessions.list in this shape only for a
        roster lane (agentId pinned, limit 1..20, involvingMe required), so
        on any other lane this raises before a byte leaves the Mac. Rows
        are sanitized to the three fields the roster shows; anything
        malformed is skipped rather than guessed at.
        """
        listed = _record(await self.connection.request(
            "sessions.list",
            {"agentId": self.agent_id, "limit": limit, "involvingMe": True},
        ))
        raw_sessions = listed.get("sessions") if listed else None
        if not isinstance(raw_sessions, list):
            raise OpenClawGatewayError(
                "OpenClaw returned an invalid session listing"
            )
        rows: list[dict[str, str]] = []
        for raw in raw_sessions:
            record = _record(raw)
            if record is None:
                continue
            key = record.get("key")
            label = record.get("label")
            session_id = record.get("sessionId")
            if not isinstance(key, str) or not key:
                continue
            rows.append({
                "key": key,
                "label": label if isinstance(label, str) else "",
                "sessionId": session_id if isinstance(session_id, str) else "",
            })
        return rows

    async def history(self, limit: int = 12) -> list[tuple[str, str]]:
        """The session's own recent (role, text) rows, oldest first.

        The runtime's direct session API, not JOAT's in-process echo —
        AGENT_TERMINAL_ROADMAP.md:113 requires "read bounded history", and
        until this existed 'terminal tail' could only show what JOAT
        itself had relayed, which for a freshly resumed session is
        nothing. Malformed rows are skipped, never guessed at.
        """
        if not 1 <= limit <= 50:
            raise ValueError("OpenClaw Agent Terminal history limit is invalid")
        payload = _record(await self.connection.request(
            "chat.history",
            {"sessionKey": self.session_key},
        ))
        messages = payload.get("messages") if payload else None
        if not isinstance(messages, list):
            raise OpenClawGatewayError(
                "OpenClaw returned invalid Agent Terminal history"
            )
        rows: list[tuple[str, str]] = []
        for raw in messages:
            message = _record(raw)
            if message is None:
                continue
            role = message.get("role")
            if not isinstance(role, str) or not role:
                continue
            content = message.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts: list[str] = []
                for item in content:
                    block = _record(item)
                    if block is not None and block.get("type") == "text":
                        piece = block.get("text")
                        if isinstance(piece, str):
                            parts.append(piece)
                text = "".join(parts)
            else:
                continue
            if not text.strip():
                continue
            rows.append((role, text.strip()))
        return rows[-limit:]

    async def resolve_approval(
        self, approval_id: str, kind: str, decision: str
    ) -> str | None:
        """Answer a pending approval; returns the RECORDED decision.

        The gateway is first-answer-wins, so the canonical result may not
        be ours — the caller reports what was recorded, never what it
        merely sent. None means the gateway gave no legible receipt.
        """
        result = _record(await self.connection.request(
            "approval.resolve",
            {"id": approval_id, "kind": kind, "decision": decision},
        ))
        recorded = result.get("decision") if result else None
        return recorded if isinstance(recorded, str) else None

    async def _assert_tools_disabled(self) -> None:
        if self.policy.allows_approvals:
            # A work session exists to run tools; what protects the wearer
            # there is the approval gate, not tool absence. The Brainstorm
            # and calendar lanes keep this preflight untouched.
            return
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
        if any(not _is_settled_tool_exclusion(notice) for notice in notices):
            _log_unsettled_tool_inventory(notices, groups)
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


def _is_settled_tool_exclusion(notice: object) -> bool:
    record = _record(notice)
    notice_id = record.get("id") if record else None
    if record is None or not isinstance(notice_id, str):
        return False
    severity = SETTLED_TOOL_EXCLUSIONS.get(notice_id)
    message = record.get("message")
    return (
        severity is not None
        and record.get("severity") == severity
        and isinstance(message, str)
        and bool(message.strip())
    )


def _log_unsettled_tool_inventory(notices: list[object], groups: list[object]) -> None:
    """Describe the refusal without retaining any remote payload text."""
    codes: set[str] = set()
    unknown = malformed = 0
    for notice in notices:
        record = _record(notice)
        code = record.get("id") if record else None
        if not isinstance(code, str) or not code:
            malformed += 1
        elif code in TOOL_INVENTORY_NOTICE_CODES:
            codes.add(code)
        elif (code.startswith(TOOL_SCHEMA_NOTICE_PREFIX)
              and len(code) > len(TOOL_SCHEMA_NOTICE_PREFIX)):
            codes.add("unsupported-tool-schema")
        else:
            unknown += 1
    tool_entries = malformed_groups = 0
    for group in groups:
        record = _record(group)
        tools = record.get("tools") if record else None
        if isinstance(tools, list):
            tool_entries += len(tools)
        else:
            malformed_groups += 1
    try:
        log.warning(
            "OpenClaw tool inventory unsettled notice_count=%d codes=%s "
            "unknown_notices=%d malformed_notices=%d groups=%d "
            "tool_entries=%d malformed_groups=%d",
            len(notices), ",".join(sorted(codes)) or "none", unknown, malformed,
            len(groups), tool_entries, malformed_groups,
        )
    except Exception:
        # Diagnostics must not replace the original fail-closed exception.
        pass


def _parse_approval(payload: dict[str, object]) -> PendingApproval | None:
    """A sanitized session.approval event → the wearer-facing record.

    Tolerant on the summary (naming fields vary by tool kind); strict on
    the id and kind, because those two are what approval.resolve replays.
    """
    approval_id = payload.get("id") or payload.get("approvalId")
    kind = payload.get("kind")
    if not isinstance(approval_id, str) or not approval_id:
        return None
    if not isinstance(kind, str) or not kind:
        return None
    summary = ""
    for field_name in ("summary", "title", "description", "command"):
        candidate = payload.get(field_name)
        if isinstance(candidate, str) and candidate.strip():
            summary = candidate.strip()[:200]
            break
    return PendingApproval(approval_id=approval_id, kind=kind, summary=summary)


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
    approvals: bool = False,
) -> None:
    """Require only the protocol surface and authority this operation uses."""
    required_methods = (
        REQUIRED_METHODS
        if require_write
        else frozenset({"sessions.list", "sessions.describe", "tools.effective"})
    )
    if approvals and require_write:
        required_methods = required_methods | APPROVAL_METHODS
    if not required_methods.issubset(connection.advertised_methods):
        raise OpenClawGatewayError(
            "OpenClaw is missing required Agent Terminal methods"
        )
    required_events = REQUIRED_EVENTS
    if approvals and require_write:
        required_events = required_events | {APPROVAL_EVENT}
    if require_write and not required_events.issubset(connection.advertised_events):
        raise OpenClawGatewayError(
            "OpenClaw is missing required Agent Terminal events"
        )
    expected_scopes = (
        {"operator.read", "operator.write", "operator.approvals"}
        if require_write and approvals
        else {"operator.read", "operator.write"}
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
