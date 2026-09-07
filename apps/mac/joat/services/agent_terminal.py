"""Agent Terminal: the glasses-side control surface for Hermes/OpenClaw work.

NOT Brainstorm. Per docs/AGENT_TERMINAL_ROADMAP.md and the wearer's ruling,
this surface exists only to choose, watch, steer and cancel EXISTING
Hermes/OpenClaw sessions — Codex and Claude never appear here, and nothing
in this module gates or joins a Brainstorm.

The roster composes the already-verified transport (`OpenClawTerminalAgent`
serializes turns, demands runtime receipts, invalidates itself rather than
guessing after a cancel) instead of loosening it: each lane keeps its own
frozen policy, and what this adds is purely wearer-facing — a spoken way to
pick a lane, send a line of terminal input, keep a bounded output tail per
lane, and let two lanes work at once while only the focused one narrates.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import secrets
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from ..core.protocol import BRAINSTORM_ROW_COLUMNS, BRAINSTORM_SCROLLBACK_ROWS
from ..agents.openclaw_gateway import (
    OpenClawPairingRequired, OpenClawScopeMismatch, classify_gateway_dial_failure,
)

log = logging.getLogger(__name__)

Announce = Callable[[str], Awaitable[None]]
Paint = Callable[["TerminalFrame"], Awaitable[None]]

HUD_LINES = BRAINSTORM_SCROLLBACK_ROWS
"""Rows of terminal transcript that ride the wire.

Scrollback, not one screenful. This was 7 — fewer rows than the renderer's
running-terminal window even shows (12) — so the terminal could not fill
its own screen, let alone be scrolled. Brainstorm was given scrollback
when the wearer asked for it; the terminal is the same surface with the
same swipe, and was simply missed."""

HUD_COLUMNS = BRAINSTORM_ROW_COLUMNS
"""Characters one of those rows holds.

Derived once, in protocol.py, from the face HudCanvasRenderer actually
draws with — read that docstring before changing this. It lived here as a
literal 74 (the old monospace derivation) for one release after the
transcript became proportional, which is exactly the truncation the
wearer reported. HUD_LINES rows of HUD_COLUMNS, plus the newlines between
them, are what BRAINSTORM_ACTIVITY_MAX is: the wire bound and the shipped
scrollback cannot drift apart because they are now the same arithmetic."""

HUD_TOPIC_COLUMNS = 96
"""The wire's PAYLOAD bound on the header line — not a layout budget.

Layout is decided at draw time by measured pixels (the renderer's fitText),
because the header is set in a proportional face where a character count
cannot predict a width. This number only stops an unbounded string reaching
the phone. Same for HUD_LANE_COLUMNS below. Contrast HUD_COLUMNS, which IS
a layout budget and therefore derives from the measured advance."""

HUD_LANE_COLUMNS = 48
"""The wire's PAYLOAD bound on one lane chip; see HUD_TOPIC_COLUMNS."""

SETUP_LANE_COLUMNS = 20
"""Leave room for complete retry guidance in the 64-character reason."""

MAX_TERMINAL_INSTRUCTION_CHARS = 4096


class TerminalInstructionResult(StrEnum):
    """Local admission, not proof the remote session received or finished work."""

    ACCEPTED = "accepted"
    INVALID = "invalid"
    WRONG_FOCUS = "wrong_focus"
    UNAVAILABLE = "unavailable"
    BUSY = "busy"
    APPROVAL_REQUIRED = "approval_required"
    CONTEXT_CHANGED = "context_changed"


@dataclass(frozen=True)
class TerminalFrame:
    """What the lenses should show for the terminal, right now.

    Deliberately just strings and a state word: the host decides how to
    put it on the wire, so this module never learns the HUD protocol.
    """

    topic: str
    state: str
    lanes: list[str]
    lines: list[str]
    #: Tool requests the FOCUSED lane is blocked on. Not the same thing as
    #: state == "approval_required", which ALSO describes a connection
    #: whose device is unapproved in OpenClaw — opposite remedies (approve
    #: the tool vs retry the daemon), so a gesture keys on this count
    #: rather than on the overloaded state word.
    approvals: int = 0
    #: Safe lane-specific setup guidance; never an adapter exception.
    reason: str = ""


def _dial_failure_sentence(label: str, exc: BaseException) -> str:
    """The lane frame's one recovery sentence for a failed connection.

    Fixed copy keyed on the classified reason — never the exception text,
    which can carry the endpoint host or a remote payload. An unclassified
    failure keeps the generic sentence so nothing invents a cause.
    """
    cause: BaseException | None = exc
    while cause is not None and not isinstance(cause, OSError | TimeoutError):
        cause = cause.__cause__ or cause.__context__
    reason = classify_gateway_dial_failure(cause if cause is not None else exc).reason_code
    if reason == "endpoint_refused":
        return f"{label}: the home brain refused — restore its Tailscale Serve, then tap retry."
    if reason == "host_unreachable":
        return f"{label}: home brain unreachable — check Tailscale on both Macs, then tap retry."
    return f"Could not connect to {label}. Tap retry."


def _one_row(text: str, columns: int) -> str:
    """One tail entry, guaranteed to render as exactly one row.

    Agent output is split on newlines before it reaches the tail, so
    entries are normally single lines. Errors are not: `lane.last_error`
    is `str(exc)`, and an exception message can carry newlines of its own
    — a traceback, a multi-line protocol complaint. Recorded as
    `[!] <that>` it became one tail entry spanning several rows, so seven
    entries could render as fourteen and the renderer's tail trim would
    quietly drop half the terminal.

    Only line breaks are folded. Other whitespace is CLI output the wearer
    may be reading for its shape, and is left alone.
    """
    return text.replace("\r", " ").replace("\n", " ")[:columns]


class TerminalAgentLike(Protocol):
    """What a lane needs from its runtime — the adapter already satisfies it."""

    def stream(self, prompt: str) -> AsyncIterator[str]: ...


@runtime_checkable
class CapturedTerminalAgentLike(TerminalAgentLike, Protocol):
    """A subscribed target that can reject a changed context under its lock."""

    @property
    def capture_context_generation(self) -> int | None: ...

    def stream_captured(
        self, prompt: str, *, context_generation: int,
    ) -> AsyncIterator[str]: ...


@runtime_checkable
class InitializableTerminalAgentLike(CapturedTerminalAgentLike, Protocol):
    async def initialize(self) -> None: ...


@dataclass(frozen=True)
class TerminalCapture:
    """One question about the exact terminal target selected at the tap."""

    capture_id: str
    lane_name: str
    question: str
    expires_at: float


@dataclass(frozen=True)
class _CapturedContext:
    agent: CapturedTerminalAgentLike
    generation: int


def _capture_context(agent: TerminalAgentLike) -> _CapturedContext | None:
    try:
        if not isinstance(agent, CapturedTerminalAgentLike):
            return None
        generation = agent.capture_context_generation
    except Exception:
        return None
    if type(generation) is not int or generation < 0:
        return None
    return _CapturedContext(agent, generation)


@dataclass
class _LanePreparation:
    """One owned initialize attempt, shared by independently bounded visits."""

    agent: InitializableTerminalAgentLike
    requires_authorization: bool = False
    task: asyncio.Task[bool] | None = None
    waiters: set[object] = field(default_factory=set)
    abandoned: bool = False
    reason: str = ""


class SessionListerLike(Protocol):
    """The optional enumeration surface (roster lanes only)."""

    async def list_sessions(self, limit: int = 20) -> list[dict[str, str]]: ...


class SessionResumerLike(Protocol):
    """The optional resume surface (roster lanes only)."""

    async def resume_session(self, session_key: str) -> None: ...


class ApprovalResolverLike(Protocol):
    """The optional approvals surface (work lane only)."""

    async def resolve_approval(
        self, approval_id: str, kind: str, decision: str
    ) -> str | None: ...


def _session_host(key: object) -> str:
    """The agent segment of a gateway session key, or "" if unreadable.

    Keys are "agent:<agentId>:<namespace><suffix>", so the agentId is the
    field that tells two same-labelled sessions apart. Read defensively:
    a malformed key disambiguates nothing rather than raising.
    """
    if not isinstance(key, str):
        return ""
    parts = key.split(":")
    return parts[1] if len(parts) >= 3 and parts[1] else ""


def _listed_name(row: dict[str, str], rows: Sequence[dict[str, str]]) -> str:
    """How one discovered session is NAMED to the wearer.

    Bare label when it is unique — the roadmap is explicit that host names
    appear ONLY when two rows otherwise cannot be distinguished, and a host
    on every row is chrome on a surface that cannot afford it. When a label
    repeats, every row carrying it gains its host, because showing it on
    one and not the other would be worse than showing neither.
    """
    label = row.get("label") or row.get("key") or "?"
    if sum(1 for other in rows if (other.get("label") or "") == row.get("label")) < 2:
        return label
    host = _session_host(row.get("key"))
    return f"{label} ({host})" if host else label


def _is_roster_lane(lane: "TerminalLane") -> bool:
    """Can this lane enumerate and adopt the wearer's other sessions?

    Selecting on `hasattr(agent, "list_sessions")` looked like a capability
    check and was not one: every lane is the same class, so every lane
    answered True and the winner was decided by dict insertion order. It
    worked only because "work" happens to be registered first
    (__main__.py). Drop the work lane and `discover()` would silently pick
    the BRAINSTORM lane and fail at the policy layer — with a message about
    policy, for a bug that was really about ordering.

    The gateway's own policy is the authority: `roster` is exactly the flag
    it checks before allowing `sessions.list` in the involvingMe shape or a
    resume. A lane with no policy is not a roster lane.
    """
    policy = getattr(lane.agent, "policy", None)
    return bool(getattr(policy, "roster", False))


@dataclass
class TerminalLane:
    """One Hermes/OpenClaw session the wearer can address by name."""

    name: str
    """Spoken handle: 'hermes', 'calendar'."""
    agent: TerminalAgentLike
    tail: deque[str] = field(default_factory=lambda: deque(maxlen=200))
    """Bounded output history — enough for 'terminal tail' and the future
    dense HUD surface, small enough to never become an unbounded transcript
    store (the runtime, not JOAT, owns full history)."""
    task: asyncio.Task[None] | None = None
    last_error: str = ""
    #: One queued instruction, newest wins — a terminal's type-ahead.
    #: Landed automatically when the in-flight turn settles; dropped by
    #: 'terminal cancel', because a line landing after a cancel is a
    #: surprise, not a service.
    queued: str = ""
    queued_context: _CapturedContext | None = None
    capture_revision: int = 0
    approvals: list[tuple[str, str, str]] = field(default_factory=list)
    """Pending (id, kind, summary) — tool calls waiting on the wearer."""
    preparation: _LanePreparation | None = None

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    @property
    def state(self) -> str:
        if self.busy:
            return "working"
        setup, _ = self.preparation_status
        if setup != "ready":
            return setup
        if self.last_error:
            return "needs recovery"
        return "idle"

    @property
    def preparation_status(self) -> tuple[str, str]:
        # Legacy stream-only embeddings have no typed readiness contract.
        # Retain their behavior; actual captured lanes must prove a context.
        try:
            scoped = isinstance(self.agent, CapturedTerminalAgentLike)
        except Exception:
            scoped = True
        if not scoped:
            return "ready", ""
        attempt = self.preparation
        if attempt is not None and attempt.agent is not self.agent:
            attempt = None
        label = _one_row(self.name, SETUP_LANE_COLUMNS)
        if attempt is not None and attempt.task is not None and not attempt.task.done():
            if attempt.abandoned:
                return "failed", attempt.reason
            return "preparing", f"Connecting to {label}…"
        if _capture_context(self.agent) is not None:
            return "ready", ""
        if attempt is not None and attempt.reason:
            if attempt.requires_authorization:
                return "approval_required", attempt.reason
            return "failed", attempt.reason
        return "unprepared", f"Open {label} to connect this session."


@dataclass(frozen=True)
class ApprovalReceipt:
    """What an approval answer came back as, separated from how it is said.

    `recorded` is the verdict the gateway kept ("approve"/"deny") or None
    when nothing was recorded; `outcome` names why, so a consumer never has
    to parse the sentence to know whether the wearer's answer stood.
    """

    recorded: str | None
    sentence: str
    outcome: Literal[
        "recorded", "conflict", "no_receipt", "failed", "nothing_pending",
        "ambiguous", "unsupported", "no_session",
    ] = "recorded"


@dataclass(frozen=True)
class _TerminalCaptureTarget:
    lane: TerminalLane
    context: _CapturedContext
    focus_revision: int
    lane_revision: int
    task: asyncio.Task[None] | None
    busy: bool


class AgentTerminalRoster:
    """The wearer's sessions: focus one, keep the others running.

    Concurrency is the point (T3): each lane's send runs as its own task,
    so switching focus never stops anyone's work. Output always lands in
    that lane's tail; only the focused lane narrates via `announce`, so an
    unfocused lane finishing is a quiet fact you can ask about, not an
    interruption.
    """

    def __init__(
        self,
        lanes: Sequence[tuple[str, TerminalAgentLike]],
        announce: Announce | None = None,
        paint: Paint | None = None,
    ) -> None:
        self._lanes: dict[str, TerminalLane] = {
            name: TerminalLane(name=name, agent=agent) for name, agent in lanes
        }
        self._announce = announce
        self._paint = paint
        self._focus: str | None = next(iter(self._lanes), None)
        self._focus_revision = 0
        self._discovered: list[dict[str, str]] = []
        # Last chip text painted per lane, so a background lane
        # repaints on a STATE change and not on every line.
        self._marks: dict[str, str] = {}
        """Rows from the last discover(); what 'resume <label>' can name."""

    # ── roster ───────────────────────────────────────────────────────────

    @property
    def focused(self) -> str | None:
        return self._focus

    @property
    def focus_generation(self) -> int:
        """Changes even when focus leaves a lane and later returns to it."""
        return self._focus_revision

    def _capture_target(self) -> _TerminalCaptureTarget | None:
        lane = self._lanes.get(self._focus or "")
        if lane is None or lane.preparation_status[0] != "ready":
            return None
        context = _capture_context(lane.agent)
        if context is None:
            return None
        return _TerminalCaptureTarget(
            lane=lane, context=context,
            focus_revision=self._focus_revision, lane_revision=lane.capture_revision,
            task=lane.task, busy=lane.busy,
        )

    def _context_is_current(self, lane: TerminalLane, context: _CapturedContext) -> bool:
        if self._lanes.get(lane.name) is not lane or lane.agent is not context.agent:
            return False
        try:
            generation = context.agent.capture_context_generation
        except Exception:
            return False
        return type(generation) is int and generation == context.generation

    def _capture_target_is_current(self, target: _TerminalCaptureTarget) -> bool:
        lane = target.lane
        return (
            self._focus == lane.name and self._focus_revision == target.focus_revision
            and lane.capture_revision == target.lane_revision
            and lane.task is target.task and lane.busy == target.busy
            and self._context_is_current(lane, target.context)
        )

    async def _send_captured(self, target: _TerminalCaptureTarget, text: str) -> str | None:
        if not self._capture_target_is_current(target):
            return None
        return await self._send_to_lane(target.lane, text, target.context)

    def names(self) -> list[str]:
        return list(self._lanes)

    @property
    def busy(self) -> bool:
        """Is any lane doing work right now?

        Asked directly rather than inferred from the rendered row text: a
        caller that string-matched " · running" found nothing, because the
        word this roster emits is "working". A picker keyed on that guard
        would have drawn over live output — the one thing it must not do.
        """
        return any(lane.busy for lane in self._lanes.values())

    def picker_rows(self) -> list[tuple[str, str]]:
        """(lane name, row text) for every session, newest state first.

        The idle terminal used to say: Say "terminal sessions" to choose a
        work session. That is not a chooser, it is homework — the wearer
        asked what it even meant. A list of the sessions that exist, that
        you tap, is the chooser; the spoken phrase stays for hands-busy.
        """
        rows: list[tuple[str, str]] = []
        for lane in self._lanes.values():
            mark = "▸ " if lane.name == self._focus else "  "
            last = (lane.tail[-1] if lane.tail else "").strip()
            waiting = (f" · {len(lane.approvals)} waiting"
                       if lane.approvals else "")
            detail = f" · {last}" if last else ""
            rows.append((
                lane.name,
                _one_row(f"{mark}{lane.name} · {lane.state}{waiting}{detail}",
                         HUD_COLUMNS),
            ))
        return rows

    def overview(self) -> str:
        """One line per session — the spoken (and later HUD) roster."""
        if not self._lanes:
            return "No terminal sessions are configured on this Mac."
        rows = []
        for lane in self._lanes.values():
            mark = "▸" if lane.name == self._focus else " "
            last = lane.tail[-1] if lane.tail else "—"
            waiting = f" · {len(lane.approvals)} approval(s) waiting" \
                if lane.approvals else ""
            rows.append(f"{mark} {lane.name}: {lane.state}{waiting} · {last}")
        return "\n".join(rows)

    def frame(self) -> TerminalFrame:
        """The dense terminal view: what is running, and its newest output.

        Tail-anchored — the roadmap asks for a dense newest-first terminal
        tail, and on a 7-row area the honest way to show "newest" is to
        keep the bottom pinned to now.
        """
        lane = self._lanes.get(self._focus or "")
        if lane is None:
            return TerminalFrame(
                topic="Agent Terminal"[:HUD_TOPIC_COLUMNS], state="unavailable",
                lanes=[], lines=["No terminal sessions on this Mac."],
            )
        header = f"{lane.name} · {lane.state}"
        if lane.approvals:
            header += f" · {len(lane.approvals)} waiting"
        # Bounded HERE, where the frame is built, so an over-long frame
        # cannot be constructed at all.
        #
        # Agent output is arbitrary: the lane tail is bounded by LINE COUNT
        # (deque maxlen), never by line width, so six verbose lines from a
        # working Hermes ran past the wire's activity bound. That failure
        # was silent by design — repaint() swallows sink exceptions so a
        # dead surface cannot kill the work — so the terminal simply froze
        # on a stale frame with nothing logged, which is precisely the
        # "watch concurrent work" the roster exists to provide.
        header = _one_row(header, HUD_TOPIC_COLUMNS)
        # All HUD_LINES rows, not one fewer: the header is a separate
        # field drawn above the divider, so it never cost a transcript row.
        # Each line is bounded to exactly one row, so the row count is the
        # line count.
        body = [_one_row(line, HUD_COLUMNS)
                for line in list(lane.tail)[-HUD_LINES:]] or ["no output yet"]
        setup, reason = lane.preparation_status
        state = (
            "running" if lane.busy
            else "approval_required" if lane.approvals or setup == "approval_required"
            else "unavailable" if setup != "ready"
            else "blocked" if lane.last_error
            else "ready"
        )
        return TerminalFrame(
            approvals=len(lane.approvals),
            topic=header, state=state, reason=reason,
            # Names alone made 'watch concurrent work' impossible: a
            # background lane could finish, block, or start waiting on an
            # approval and the frame looked identical. Each lane now
            # carries its state, so a glance covers the whole roster.
            lanes=[_one_row(f"{name}·{self._mark(other)}", HUD_LANE_COLUMNS)
                   for name, other in list(self._lanes.items())[:3]],
            lines=body,
        )

    @staticmethod
    def _mark(lane: TerminalLane) -> str:
        if lane.approvals:
            return f"ask{len(lane.approvals)}"
        if lane.busy:
            return "run"
        setup, _ = lane.preparation_status
        if setup == "preparing":
            return "connect"
        if setup == "unprepared":
            return "setup"
        if setup == "failed":
            return "err"
        if setup == "approval_required":
            return "access"
        if lane.last_error:
            return "err"
        return "idle"

    def _visible_lane(self, fallback: TerminalLane) -> TerminalLane:
        """Where output must land to actually be SEEN.

        The frame draws the FOCUSED lane. Recording into the lane that
        happened to serve the request leaves the line invisible whenever
        the two differ — which is most of the time, since only one lane
        can enumerate.
        """
        return self._lanes.get(self._focus or "", fallback)

    def _record(self, lane: TerminalLane, line: str) -> None:
        lane.tail.append(line)

    async def repaint(self) -> None:
        if self._paint is not None:
            try:
                await self._paint(self.frame())
            except Exception:  # a dead surface must not kill the work
                pass

    async def prepare_lane(
        self, name: str, *, is_current: Callable[[], bool],
        timeout_s: float = 8.0, paint: Paint | None = None,
    ) -> bool:
        """Connect an explicitly opened lane without transferring its visit.

        Initialize is shared, but each visit owns its deadline and paint
        rights. The final departing waiter cancels setup and allows 100 ms
        for cleanup. A non-cooperative task remains owned and blocks a
        parallel attempt until it finishes. The adapter owns connection
        cleanup and also joins its initialization during shutdown.
        """
        if type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("Terminal preparation timeout must be finite and positive")
        try:
            if not is_current() or name not in self._lanes:
                return False
        except Exception:
            return False
        self.focus(name)
        lane = self._lanes[name]
        agent = lane.agent
        focus_generation = self._focus_revision
        attempt = lane.preparation
        if attempt is not None and attempt.agent is not agent:
            attempt = None

        def current() -> bool:
            try:
                return (
                    self._lanes.get(name) is lane and lane.agent is agent
                    and self._focus == name and self._focus_revision == focus_generation
                    and (attempt is None or lane.preparation is attempt)
                    and is_current()
                )
            except Exception:
                return False

        async def show() -> None:
            sink = paint if paint is not None else self._paint
            if sink is not None and current():
                try:
                    await sink(self.frame())
                except Exception:  # Presentation failure cannot break a live lane.
                    pass

        async def show_bounded() -> bool:
            try:
                async with asyncio.timeout(timeout_s):
                    await show()
            except TimeoutError:
                return False
            return current()

        pending = attempt is not None and attempt.task is not None and not attempt.task.done()
        if not pending and _capture_context(agent) is not None:
            return await show_bounded() and _capture_context(agent) is not None
        # Opening a running legacy lane must not wait on its turn lock or
        # secretly reconnect the target of an instruction already in flight.
        if lane.busy or (pending and attempt is not None and attempt.abandoned):
            await show_bounded()
            return False
        if not pending:
            if not isinstance(agent, InitializableTerminalAgentLike):
                await show_bounded()
                return False
            attempt = _LanePreparation(agent)
            lane.preparation = attempt
            lane.capture_revision += 1
            attempt.task = asyncio.create_task(self._initialize_lane(lane, attempt))
        assert attempt is not None and attempt.task is not None
        task = attempt.task
        waiter = object()
        attempt.waiters.add(waiter)
        timed_out = False
        try:
            async with asyncio.timeout(timeout_s):
                await show()
                if not current():
                    return False
                ready = await asyncio.shield(task)
                await show()
                return ready and current() and _capture_context(agent) is not None
        except TimeoutError:
            timed_out = True
            return False
        finally:
            attempt.waiters.discard(waiter)
            if not attempt.waiters and not task.done():
                attempt.abandoned = True
                label = _one_row(name, SETUP_LANE_COLUMNS)
                attempt.reason = (
                    f"Connection to {label} timed out. Tap retry." if timed_out
                    else f"Connection to {label} was interrupted. Tap retry."
                )
                task.cancel()
                # asyncio.wait does not cancel a task again or wait forever
                # for an initializer that suppresses cancellation. Keep its
                # attempt identity on the lane even if this grace expires.
                await asyncio.wait({task}, timeout=.1)
            if timed_out and current():
                try:
                    async with asyncio.timeout(.1):
                        await show()
                except TimeoutError:
                    pass

    async def _initialize_lane(self, lane: TerminalLane, attempt: _LanePreparation) -> bool:
        label = _one_row(lane.name, SETUP_LANE_COLUMNS)
        try:
            await attempt.agent.initialize()
            ready = _capture_context(attempt.agent) is not None
        except Exception as exc:
            # Connection errors can contain private remote payloads. Only a
            # fixed lane-specific recovery sentence reaches TerminalFrame.
            # Retain the failing code boundary without logging its payload:
            # otherwise authentication, protocol and session failures all
            # disappear behind the same retry sentence, even in local logs.
            origin = exc.__traceback__
            while origin is not None and origin.tb_next is not None:
                origin = origin.tb_next
            log.warning(
                "terminal initialization failed lane=%s error=%s function=%s line=%s",
                label, type(exc).__name__,
                origin.tb_frame.f_code.co_name if origin is not None else "unknown",
                origin.tb_lineno if origin is not None else 0,
            )
            if isinstance(exc, (OpenClawScopeMismatch, OpenClawPairingRequired)):
                attempt.requires_authorization = True
                attempt.reason = (
                    "Approve Work permissions in OpenClaw, then retry."
                    if isinstance(exc, OpenClawScopeMismatch)
                    else "Approve this device in OpenClaw, then retry."
                )
            else:
                # Say which way the dial died (2026-09-06): the lane frame
                # read "Could not connect to work. Tap retry." whether the
                # tailnet was gone or the home brain answered and refused —
                # opposite remedies. The classifier reads the exception's
                # own distinction; a wrapped cause counts too.
                attempt.reason = _dial_failure_sentence(label, exc)
            ready = False
        if (
            self._lanes.get(lane.name) is not lane or lane.agent is not attempt.agent
            or lane.preparation is not attempt or attempt.abandoned
        ):
            return False
        if ready:
            attempt.reason = ""
            lane.last_error = ""
        elif not attempt.requires_authorization and not attempt.reason:
            attempt.reason = f"Could not connect to {label}. Tap retry."
        return ready

    def focus(self, name: str) -> bool:
        """Switch focus. Never touches any lane's running work."""
        said = name.strip().lower()
        if said in self._lanes:
            if self._focus != said:
                self._focus_revision += 1
            self._focus = said
            return True
        for lane_name in self._lanes:
            if lane_name in said:
                if self._focus != lane_name:
                    self._focus_revision += 1
                self._focus = lane_name
                return True
        return False

    async def focus_next(self) -> str:
        """Move focus to the next lane and repaint — the gesture switch.

        AGENT_TERMINAL_ROADMAP.md:36 wants other sessions left running
        while another is focused, and the wearer asked to "switch among
        sessions". Both had voice ("switch terminal to <lane>") and the
        idle picker, and NEITHER is reachable while work is running: the
        picker deliberately yields to live output, which is exactly when
        switching matters. Cycling is the terminal-multiplexer gesture and
        the LANES strip already shows where it landed.
        """
        names = list(self._lanes)
        if not names:
            return "No terminal sessions are configured on this Mac."
        if len(names) < 2:
            return f"{names[0]} is the only session."
        current = self._focus if self._focus in names else names[0]
        self._focus = names[(names.index(current) + 1) % len(names)]
        self._focus_revision += 1
        await self.repaint()
        lane = self._lanes[self._focus]
        return f"{self._focus} · {lane.state}"

    async def history(self, name: str | None = None, lines: int = 10) -> str:
        """What this SESSION contains, from the runtime's own record.

        Distinct from `tail`, which is what scrolled past on JOAT's screen.
        For any session JOAT did not personally run — a resumed one, or
        anything from before this engine started — the local deque is empty
        or misleading, and "inspect a session through its runtime's direct
        session API" (AGENT_TERMINAL_ROADMAP.md:37-38, :113) is the whole
        point of the operation. Falls back to the local tail rather than
        failing: an adapter without the surface still answers something
        true, and says which it gave.
        """
        lane = self._lanes.get(name or self._focus or "")
        if lane is None:
            return (
                "No terminal sessions are configured on this Mac."
                if not self._lanes else "No such terminal session."
            )
        reader = getattr(lane.agent, "history", None)
        if reader is None:
            return f"{lane.name} (local tail — this runtime has no history API):\n{self.tail(name, lines)}"
        try:
            rows = await reader(lines)
        except Exception as exc:
            reason = str(exc) or exc.__class__.__name__
            return f"{lane.name}: history unavailable — {reason}"
        if not rows:
            return f"{lane.name}: the runtime reports no history yet."
        out = []
        for role, text in rows:
            prefix = "> " if role == "user" else ""
            # The whole entry, not its first line: multi-line answers used
            # to lose everything after line one (the audit's T2). The
            # renderer wraps; this cap only stops one entry eating the page.
            flat = " ".join(text.split())
            if len(flat) > 400:
                flat = flat[:399].rstrip() + "…"
            out.append(f"{prefix}{flat}")
        return "\n".join(out)

    def tail(self, name: str | None = None, lines: int = 6) -> str:
        lane = self._lanes.get(name or self._focus or "")
        if lane is None:
            # "No such terminal session" reads as "you named one wrong"
            # when the truth is that none exist — two different problems
            # with two different next steps, and this surface already has
            # the right words for the second one.
            return (
                "No terminal sessions are configured on this Mac."
                if not self._lanes else "No such terminal session."
            )
        if not lane.tail:
            return f"{lane.name}: no output yet."
        return "\n".join(list(lane.tail)[-lines:])

    # ── input, work, cancel ──────────────────────────────────────────────

    def admit_instruction(self, name: str, text: str) -> TerminalInstructionResult:
        """Atomically admit exact input into the prepared, focused context.

        The caller owns explicit view preparation and retry identity. Unlike
        gesture type-ahead, this entry never queues or replaces input and
        never answers a pending Work approval.
        """
        if (
            not isinstance(name, str) or not isinstance(text, str)
            or not text.strip() or "\x00" in text
            or len(text) > MAX_TERMINAL_INSTRUCTION_CHARS
        ):
            return TerminalInstructionResult.INVALID
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            return TerminalInstructionResult.INVALID
        lane = self._lanes.get(name)
        if lane is None or self._focus != name:
            return TerminalInstructionResult.WRONG_FOCUS
        if lane.approvals:
            return TerminalInstructionResult.APPROVAL_REQUIRED
        if lane.busy or lane.queued:
            return TerminalInstructionResult.BUSY
        target = self._capture_target()
        if target is None:
            return TerminalInstructionResult.UNAVAILABLE
        if target.lane is not lane or not self._capture_target_is_current(target):
            return TerminalInstructionResult.CONTEXT_CHANGED
        # Accessing an adapter's captured generation may invoke its own
        # state code. Recheck admission after that boundary, without yielding.
        if lane.approvals:
            return TerminalInstructionResult.APPROVAL_REQUIRED
        if lane.busy or lane.queued:
            return TerminalInstructionResult.BUSY
        lane.capture_revision += 1
        self._launch_idle_send(lane, text, target.context)
        return TerminalInstructionResult.ACCEPTED

    async def send(self, text: str, name: str | None = None) -> str:
        """Voice becomes terminal input to the (focused) session — T2.

        Returns the immediate receipt line; the turn itself runs as a task
        so the wearer can switch away. Busy lanes keep one newest instruction;
        captured instructions additionally retain their exact adapter context.
        """
        lane = self._lanes.get(name or self._focus or "")
        if lane is None:
            return "No terminal session is focused."
        return await self._send_to_lane(lane, text)

    async def _send_to_lane(
        self, lane: TerminalLane, text: str, context: _CapturedContext | None = None,
    ) -> str:
        if context is not None and not self._context_is_current(lane, context):
            return f"{lane.name} ▸ input expired; select the session and speak again."
        lane.capture_revision += 1
        if lane.busy:
            # AGENT_TERMINAL_ROADMAP.md:32 — "speak ordinary instructions
            # to steer the focused session". Refusing while a turn was in
            # flight meant a running session could not be steered at all;
            # the wearer's only verb was cancel. The runtime serializes
            # turns, so the honest shape is a terminal's type-ahead: ONE
            # queued line, newest wins, landed the moment the turn
            # settles — and the receipt says which of those happened.
            replaced = bool(lane.queued)
            lane.queued = text
            lane.queued_context = context
            lane.tail.append(f"> {text} (queued)")
            await self.repaint()
            return (
                f"{lane.name} ▸ queued — replaces the earlier queued line."
                if replaced
                else f"{lane.name} ▸ queued — lands when this turn finishes."
            )
        return self._launch_idle_send(lane, text, context)

    def _launch_idle_send(
        self, lane: TerminalLane, text: str, context: _CapturedContext | None = None,
    ) -> str:
        """Schedule an admitted idle turn with the existing settlement hook."""
        lane.last_error = ""
        lane.tail.append(f"> {text}")
        task = asyncio.get_running_loop().create_task(
            self._run_send(lane, text, context), name=f"terminal-{lane.name}"
        )
        lane.task = task

        def _schedule_settled(_task: asyncio.Task[None]) -> None:
            asyncio.get_running_loop().create_task(self._lane_settled(lane, _task))

        task.add_done_callback(_schedule_settled)
        return f"{lane.name} ▸ sent."

    async def _lane_settled(self, lane: TerminalLane, completed: asyncio.Task[None]) -> None:
        """The run→idle / run→err transition, which nothing else can see.

        The last _narrate of a turn executes INSIDE the still-running task,
        where lane.busy is still True — so the strip's chip said "run"
        through the narration and then nothing fired when the task actually
        completed. A background lane therefore never repainted its own
        finish, and "watch concurrent work" showed stale state until the
        focused lane happened to speak. This runs after the task is done.
        """
        # An old completion cannot take a newer turn's queued instruction.
        if lane.task is not completed or self._lanes.get(lane.name) is not lane:
            return
        queued, context = lane.queued, lane.queued_context
        lane.queued, lane.queued_context = "", None
        if queued:
            lane.capture_revision += 1
            receipt = await self._send_to_lane(lane, queued, context)
            await self._narrate(lane, receipt)
        mark = self._mark(lane)
        if self._marks.get(lane.name) != mark:
            self._marks[lane.name] = mark
            await self.repaint()

    async def _run_send(
        self, lane: TerminalLane, text: str, context: _CapturedContext | None = None,
    ) -> None:
        pending = ""
        try:
            if context is not None:
                if not self._context_is_current(lane, context):
                    raise RuntimeError("Captured terminal context changed")
                stream = context.agent.stream_captured(text, context_generation=context.generation)
            else:
                stream = lane.agent.stream(text)
            try:
                async for chunk in stream:
                    pending += chunk
                    *lines, pending = pending.split("\n")
                    for line in lines:
                        if line.strip():
                            lane.tail.append(line)
                            await self._narrate(lane, line)
            finally:
                close = getattr(stream, "aclose", None)
                if close is not None:
                    await close()
            if pending.strip():
                lane.tail.append(pending)
                await self._narrate(lane, pending)
            await self._narrate(lane, f"{lane.name} ▸ done.")
        except asyncio.CancelledError:
            lane.tail.append("[cancelled]")
            raise
        except Exception as exc:  # runtime already classified it; keep the line
            # The adapter invalidates itself on any protocol doubt — that is
            # its receipt discipline, not ours to soften. The wearer gets
            # the reason and the retry path, never a silent dead lane.
            lane.last_error = str(exc) or exc.__class__.__name__
            lane.tail.append(f"[!] {lane.last_error}")
            log.warning("terminal %s failed: %s", lane.name, lane.last_error)
            await self._narrate(lane, f"{lane.name} ▸ failed — {lane.last_error}")

    async def _alert(self, line: str) -> None:
        """Say something about a lane the wearer is NOT watching."""
        await self.repaint()
        if self._announce is not None:
            try:
                await self._announce(line)
            except Exception:  # a dead surface must not kill the work
                pass

    async def _narrate(self, lane: TerminalLane, line: str) -> None:
        # Only the focused lane's CONTENT changes what the lenses show. A
        # background lane streaming its own output must not repaint once
        # per line — that is pure BLE traffic on a link the wearer needs
        # for the session they are actually watching.
        #
        # Its STATE is different, and this used to return before reaching
        # it. The LANES strip exists so "one glance covers the roster", and
        # the wearer asked for exactly this — "watch concurrent work" — but
        # a background lane going idle → run → ask1 → err never repainted
        # anything, so the strip showed whatever it happened to say when
        # the focused lane last spoke. A state change is ONE chip, not a
        # stream: repaint on the transition, never on the line.
        if lane.name != self._focus:
            mark = self._mark(lane)
            if self._marks.get(lane.name) != mark:
                self._marks[lane.name] = mark
                await self.repaint()
            return
        self._marks[lane.name] = self._mark(lane)
        # Every narrated line IS a frame change: the glasses show the tail,
        # not one sentence that scrolls away.
        await self.repaint()
        if self._announce is not None:
            try:
                await self._announce(line)
            except Exception:  # a dead surface must not kill the work
                pass

    # ── discovery (T1) ───────────────────────────────────────────────────

    async def discover(self) -> str:
        """List the wearer's existing gateway sessions — 'choose' made real.

        Uses the first lane that carries the enumeration surface (the work
        lane's policy is the only one whose sessions.list is allowed in
        the involvingMe shape). Rows that are also addressable lanes are
        marked; the rest are visible but watch-only until the send policy
        learns resumable keys — shown honestly rather than pretended at.
        """
        lister = next(
            (lane for lane in self._lanes.values() if _is_roster_lane(lane)),
            None,
        )
        if lister is None:
            return "No terminal lane on this Mac can enumerate sessions."
        try:
            lister_agent = lister.agent
            rows = await lister_agent.list_sessions()  # type: ignore[attr-defined]
        except Exception as exc:
            reason = str(exc) or exc.__class__.__name__
            return f"Session listing failed — {reason}"
        self._discovered = rows
        if not rows:
            self._record(self._visible_lane(lister),
                         "[discover] no sessions involving you")
            return "The gateway lists no sessions involving you."
        lines = []
        for row in rows[:12]:
            lines.append(f"• {_listed_name(row, rows)}")
        # A listing IS terminal output: putting it in the tail is what lets
        # the wearer READ their sessions on the lenses instead of having to
        # remember what was just spoken.
        self._record(self._visible_lane(lister), "[discover] " + ", ".join(
            (row.get("label") or row.get("key") or "?") for row in rows[:6]
        ))
        suffix = f"\n(+{len(rows) - 12} more)" if len(rows) > 12 else ""
        return "Your sessions on the gateway:\n" + "\n".join(lines) + suffix

    async def resume(self, spoken: str) -> str:
        """Point the enumerating lane at a discovered session, by name.

        Only rows the gateway actually listed are namable — resuming is
        choosing from what was seen, never typing a key by voice.
        """
        if not self._discovered:
            return "Say 'terminal discover' first — nothing has been listed yet."
        said = " ".join(spoken.lower().split())
        matches = [
            row for row in self._discovered
            if (name := _listed_name(row, self._discovered).lower())
            and (name == said or said in name or name in said)
        ]
        if not matches:
            return f"No listed session matches '{spoken}'."
        if len(matches) > 1:
            # AGENT_TERMINAL_ROADMAP.md:37-38 — "host names appear only
            # when two rows otherwise cannot be distinguished." Two real
            # sessions can share a label; this took the FIRST silently, so
            # the second was unreachable by voice and the wearer was never
            # told a choice existed. Same rule as `continue <title>`: name
            # the candidates, do not guess.
            named = " · ".join(
                _listed_name(row, self._discovered) for row in matches[:3]
            )
            return f"{len(matches)} sessions match — say one exactly: {named}"
        match = matches[0]
        lane = next(
            (lane for lane in self._lanes.values() if _is_roster_lane(lane)),
            None,
        )
        if lane is None:
            return "No terminal lane on this Mac can resume a session."
        if lane.busy:
            return f"{lane.name} is still working — cancel it before resuming."
        # Invalidate before the adapter awaits a lock or connection close.
        # Even a failed resume has displaced the prior spoken question.
        lane.capture_revision += 1
        try:
            await lane.agent.resume_session(match["key"])  # type: ignore[attr-defined]
        except Exception as exc:
            reason = str(exc) or exc.__class__.__name__
            lane.last_error = reason
            return f"Could not resume — {reason}"
        lane.last_error = ""
        lane.approvals.clear()
        lane.tail.clear()
        label = match.get("label") or match["key"]
        lane.tail.append(f"[resumed] {label}")
        # The session's OWN recent history, from the runtime's direct API
        # (AGENT_TERMINAL_ROADMAP.md:113) — not JOAT's in-process echo,
        # which for a freshly resumed session is empty. Best-effort: a
        # session with unreadable history is still resumed and steerable.
        reader = getattr(lane.agent, "history", None)
        if reader is not None:
            try:
                for role, text in await reader(10):
                    prefix = "> " if role == "user" else ""
                    first_line = text.split("\n", 1)[0][:200]
                    lane.tail.append(f"{prefix}{first_line}")
            except Exception as exc:
                lane.tail.append(
                    f"[history unavailable — {str(exc) or exc.__class__.__name__}]"
                )
        if self._focus != lane.name:
            self._focus_revision += 1
        self._focus = lane.name
        return f"{lane.name} ▸ resumed '{label}'. Speak to send into it."

    # ── approvals (T5) ───────────────────────────────────────────────────

    def note_approval(self, lane_name: str, approval_id: str,
                      kind: str, summary: str) -> None:
        """A work session wants to run a tool; the wearer decides.

        Called from the lane's on_approval callback (sync, event-loop
        thread). Lands in the lane's list AND its tail, so 'terminal tail'
        shows the request in context.
        """
        lane = self._lanes.get(lane_name)
        if lane is None:
            return
        lane.capture_revision += 1
        lane.approvals.append((approval_id, kind, summary))
        line = f"[approval] {kind}: {summary or approval_id} — say 'terminal approve' or 'terminal deny'"
        lane.tail.append(line)
        loop = asyncio.get_running_loop()
        if lane.name == self._focus:
            loop.create_task(self._narrate(lane, line))
        else:
            # A BACKGROUND approval is the one thing that must interrupt.
            # Ordinary output from an unwatched lane stays quiet on
            # purpose, but an approval halts that session until it is
            # answered — staying silent means the wearer discovers a
            # stalled job much later, wondering why nothing happened.
            loop.create_task(self._alert(
                f"{lane.name} is waiting on approval: {summary or kind}. "
                f"Say 'switch terminal to {lane.name}'."
            ))

    def approvals_view(self) -> str:
        lane = self._lanes.get(self._focus or "")
        if lane is None:
            return "No terminal session is focused."
        if not lane.approvals:
            return f"{lane.name}: nothing waiting for approval."
        return "\n".join(
            f"{i + 1}. [{kind}] {summary or approval_id}"
            for i, (approval_id, kind, summary) in enumerate(lane.approvals)
        )

    async def decide(self, decision: str, index: int | None = None) -> str:
        """Approve or deny a pending tool call — with the RECORDED receipt.

        The gateway is first-answer-wins, so the report is what was
        recorded, never merely what we sent. Denying and then speaking a
        'terminal <instruction>' IS the documented alternative flow: deny
        the original, steer the same session with the replacement.
        """
        return (await self.decide_receipt(decision, index)).sentence

    async def decide_receipt(self, decision: str, index: int | None = None) -> ApprovalReceipt:
        """`decide`, with the recorded verdict separate from the sentence.

        The audit's T1: the glasses handler compared this method's whole
        sentence ("work: recorded approve for '…'") with the bare verdict
        and told the wearer another controller had answered first — on
        every successful approval. The verdict now travels as data.
        """
        lane = self._lanes.get(self._focus or "")
        if lane is None:
            return ApprovalReceipt(None, "No terminal session is focused.", outcome="no_session")
        if not lane.approvals:
            return ApprovalReceipt(None, f"{lane.name}: nothing waiting for approval.", outcome="nothing_pending")
        if index is None and len(lane.approvals) > 1:
            return ApprovalReceipt(None, (
                f"{lane.name} has {len(lane.approvals)} waiting — say "
                f"'terminal {decision} 1' through "
                f"'terminal {decision} {len(lane.approvals)}'."
            ), outcome="ambiguous")
        position = (index or 1) - 1
        if not 0 <= position < len(lane.approvals):
            return ApprovalReceipt(None, f"{lane.name}: no approval number {index}.", outcome="ambiguous")
        resolver = getattr(lane.agent, "resolve_approval", None)
        if resolver is None:
            return ApprovalReceipt(None, f"{lane.name} cannot answer approvals.", outcome="unsupported")
        approval_id, kind, summary = lane.approvals[position]
        lane.capture_revision += 1
        try:
            recorded = await resolver(approval_id, kind, decision)
        except Exception as exc:
            lane.last_error = str(exc) or exc.__class__.__name__
            return ApprovalReceipt(
                None, f"{lane.name}: approval failed — {lane.last_error}", outcome="failed",
            )
        lane.approvals.pop(position)
        label = summary or approval_id
        if recorded is None:
            return ApprovalReceipt(None, (
                f"{lane.name}: sent {decision} for '{label}' but the "
                "gateway gave no legible receipt."
            ), outcome="no_receipt")
        if recorded != decision:
            return ApprovalReceipt(recorded, (
                f"{lane.name}: another controller answered first — "
                f"recorded {recorded} for '{label}'."
            ), outcome="conflict")
        lane.tail.append(f"[approval] {recorded}: {label}")
        return ApprovalReceipt(
            recorded, f"{lane.name}: recorded {recorded} for '{label}'.", outcome="recorded",
        )

    async def cancel(self, name: str | None = None) -> str:
        """Cancel the (focused) lane's in-flight turn — T4's honest half.

        The runtime treats a cancelled stream as grounds to invalidate the
        connection rather than guess what the remote did (its abort path
        confirms by runId inside the adapter). So the truthful report is
        'cancelled — needs recovery before the next send', never 'stopped
        cleanly' — a receipt we did not get is a receipt we do not claim.
        """
        lane = self._lanes.get(name or self._focus or "")
        if lane is None:
            return "No terminal session is focused."
        lane.capture_revision += 1
        discarded_queue = bool(lane.queued)
        # A completed turn can still have a scheduled settlement callback.
        # Clear its queue before checking busy so that callback cannot send
        # an instruction after the wearer has cancelled it.
        lane.queued, lane.queued_context = "", None
        if not lane.busy or lane.task is None:
            if discarded_queue:
                return f"{lane.name} ▸ queued instruction discarded; no turn is running."
            return f"{lane.name} has nothing running."
        lane.task.cancel()
        try:
            await lane.task
        except (asyncio.CancelledError, Exception):
            pass
        lane.last_error = lane.last_error or "cancelled — needs recovery"
        return f"{lane.name} ▸ cancelled; the session will re-verify on next send."


# The word "terminal" is REQUIRED in every phrasing: an optional prefix
# here once let plain "switch to decaf" get claimed by this grammar —
# hijacked from the assistant and answered with a terminal error.
_RESUME = re.compile(r"^terminal resume (?P<name>[a-z0-9 ]+)$")
_DECIDE = re.compile(r"^terminal (?P<decision>approve|deny|reject)(?: (?P<index>\d{1,2}))?$")
_SWITCH = re.compile(
    # The word "terminal" stays REQUIRED in every phrasing. An optional
    # prefix here once let a plain "switch to decaf" get claimed by this
    # grammar — hijacked from the assistant and answered with a terminal
    # error. "go to the work terminal" is safe because it names the
    # surface; "switch to work" deliberately is not.
    r"^(?:terminal switch to|switch terminal to|switch the terminal to)"
    r" (?P<name>[a-z0-9 ]+)$"
    r"|^(?:go to|open|show) (?:the )?(?P<named>[a-z0-9 ]+?) terminal$"
)

# Cross-surface routing: JOAT is the glue between Brainstorm and this
# terminal. "Send Claude's last reply to Hermes" takes a collaborator's
# whole answer out of the discussion and hands it to a work session —
# the wearer's own words for what they are building: "the JOAT assistant
# should be able to take anything and do something i ask it to do with
# it". Matched against the normalized transcript, where "Claude's"
# arrives as "claude s".
_ROUTE_REPLY = re.compile(
    # "give/pass/hand/push that to hermes" are how people say this out
    # loud; only "send" was recognised.
    r"^(?:send|give|pass|hand|push|forward|route) (?:the )?(?:(?P<who>codex|claude)(?: ?s)? )?"
    r"(?:last |latest )?(?:reply|response|answer|message|that)"
    r"(?: to (?:the )?(?P<lane>[a-z0-9 ]+?))?$"
)


def parse_reply_route(said: str) -> tuple[str | None, str | None] | None:
    """(who, lane) from a normalized routing phrase, or None.

    Shared between the 'terminal …'-prefixed grammar and the mid-session
    addressed path so the two surfaces cannot drift apart on what counts
    as a routing request.
    """
    m = _ROUTE_REPLY.match(said)
    if m is None:
        return None
    lane = (m.group("lane") or "").strip() or None
    if lane in ("terminal", "agent terminal"):
        lane = None  # the focused lane, same as every other terminal verb
    return m.group("who"), lane


class AgentTerminalCommands:
    """The spoken grammar — deliberately prefix-gated on 'terminal'.

    Free speech becoming terminal input is exactly the ambient-capture
    hazard the wake gate exists for, so every send requires the wearer to
    SAY the surface's name first. 'terminal run the tests' is one breath;
    an overheard sentence can never be one.
    """

    ARM_WINDOW_S = 45.0
    """How long a tap's promise lasts.

    An arm that never expires is an open microphone into a session that
    can run tools: tap, get distracted, and the next thing said — minutes
    later, to a colleague — arrives at Hermes as an instruction. The
    gesture buys ONE utterance, soon."""

    def __init__(self, roster: AgentTerminalRoster,
                 window_s: float | None = None,
                 last_reply: Callable[
                     [str | None], tuple[str, str] | None] | None = None,
                 *, monotonic: Callable[[], float] = time.monotonic,
                 on_explicit_view: Callable[[], None] | None = None,
                 ) -> None:
        self._roster = roster
        self._armed_until = 0.0
        self._window_s = self.ARM_WINDOW_S if window_s is None else window_s
        self._monotonic = monotonic
        self._capture: TerminalCapture | None = None
        self._capture_target: _TerminalCaptureTarget | None = None
        # Answers "what did <seat> last say?" from the Brainstorm snapshot.
        # Injected, because the discussion belongs to another feature and
        # this grammar must not import its internals to quote it.
        self._last_reply = last_reply
        self._on_explicit_view = on_explicit_view

    def begin_capture(self) -> TerminalCapture | None:
        """Snapshot one ready target without arming the legacy command parser."""
        current = self.current_capture
        if current is not None:
            return current
        self.clear_capture()
        target = self._roster._capture_target()
        ttl_s = min(self.ARM_WINDOW_S, self._window_s)
        if target is None or not math.isfinite(ttl_s) or ttl_s <= 0:
            return None
        capture = TerminalCapture(
            capture_id=secrets.token_urlsafe(18), lane_name=target.lane.name,
            question=f"What should {target.lane.name} do?",
            expires_at=self._monotonic() + ttl_s,
        )
        self._capture, self._capture_target = capture, target
        return capture

    @property
    def current_capture(self) -> TerminalCapture | None:
        capture = self._capture
        return capture if capture is not None and self.capture_is_current(capture) else None

    def capture_is_current(self, capture: TerminalCapture) -> bool:
        if self._capture is not capture:
            return False
        target = self._capture_target
        if (
            target is None or self._monotonic() >= capture.expires_at
            or not self._roster._capture_target_is_current(target)
        ):
            self.clear_capture()
            return False
        return True

    def clear_capture(self) -> None:
        """End both forms of pending input on Home, reconnect or replacement."""
        self._capture, self._capture_target = None, None
        self._disarm()

    async def consume_capture(self, capture: TerminalCapture, text: str) -> str | None:
        """Consume literal admitted speech once, retaining its adapter context."""
        if not self.capture_is_current(capture):
            return None
        target = self._capture_target
        self.clear_capture()
        if target is None or not isinstance(text, str) or not text.strip():
            return None
        # No await precedes owner removal or the roster's second target check.
        return await self._roster._send_captured(target, text)

    def arm(self) -> None:
        """A tap on the terminal promises the NEXT utterance as its input.

        The promise is one utterance and it is explicit, so the phrase does
        not need the 'terminal' prefix — the gesture already established
        that this is terminal speech, which is exactly the consent the
        prefix exists to obtain. It also expires: see ARM_WINDOW_S.
        """
        self.clear_capture()
        self._armed_until = self._monotonic() + self._window_s

    @property
    def armed(self) -> bool:
        return self._monotonic() < self._armed_until

    def _disarm(self) -> None:
        self._armed_until = 0.0

    def _acquire_explicit_view(self) -> None:
        """Recognition owns this boundary; ambient and scoped input skip it."""
        if self._on_explicit_view is not None:
            self._on_explicit_view()

    async def handle_command(self, text: str) -> str | None:
        if self.armed:
            said = text.strip()
            if not said:
                self._disarm()
                return None
            self._acquire_explicit_view()
            self._disarm()
            # Cancelling the promise must not become a command sent INTO
            # the session — "never mind" is spoken to JOAT, not to Hermes.
            if said.lower().rstrip(".!?") in ("stop", "cancel", "never mind",
                                              "nevermind"):
                await self._roster.repaint()
                return "Terminal input cancelled."
            sent = await self._roster.send(said)
            await self._roster.repaint()
            return sent
        answer = await self._dispatch(text)
        if answer is not None:
            # Any claimed command can change what the lenses should show —
            # focus, resume, cancel, a decision, a new send. Repainting once
            # here beats sprinkling it through every branch and forgetting
            # one, which is exactly what happened before.
            await self._roster.repaint()
        return answer

    async def route_reply(self, who: str | None, lane: str | None) -> str:
        """Hand a Brainstorm collaborator's last reply to a work session.

        The receipt names the seat that was quoted, so the wearer can tell
        "sent Claude's answer" from "sent the newest answer" without
        opening the terminal to check.
        """
        self._acquire_explicit_view()
        if self._last_reply is None:
            return "No Brainstorm has run yet — nothing to send."
        found = self._last_reply(who)
        if found is None:
            seat = who.capitalize() if who else "any collaborator"
            return f"No reply from {seat} to send yet."
        label, text = found
        if lane is not None and not self._roster.focus(lane):
            return (f"No terminal session called {lane}. "
                    + self._roster.overview())
        sent = await self._roster.send(text)
        await self._roster.repaint()
        return f"{label.split(' (')[0]}'s reply → {sent}"

    async def _dispatch(self, text: str) -> str | None:
        said = " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())
        if not said:
            return None
        if said in ("terminal", "terminal sessions", "terminal list",
                    "list terminal sessions", "agent terminal"):
            self._acquire_explicit_view()
            return self._roster.overview()
        m = _SWITCH.match(said)
        if m is not None:
            # Two alternations, two groups: "switch terminal to work" and
            # "go to the work terminal" name the lane in different places.
            name = (m.group("name") or m.group("named") or "").strip()
            self._acquire_explicit_view()
            if self._roster.focus(name):
                return f"Focused on {self._roster.focused}."
            return f"No terminal session called {name}. " + self._roster.overview()
        if said in ("terminal cancel", "terminal stop", "cancel the terminal"):
            self._acquire_explicit_view()
            return await self._roster.cancel()
        if said in ("terminal approvals", "terminal pending"):
            self._acquire_explicit_view()
            return self._roster.approvals_view()
        if said in ("terminal discover", "terminal refresh",
                    "terminal all sessions"):
            self._acquire_explicit_view()
            return await self._roster.discover()
        resume_match = _RESUME.match(said)
        if resume_match is not None:
            self._acquire_explicit_view()
            return await self._roster.resume(resume_match.group("name"))
        decision_match = _DECIDE.match(said)
        if decision_match is not None:
            spoken = decision_match.group("decision")
            index = decision_match.group("index")
            self._acquire_explicit_view()
            return await self._roster.decide(
                "approve" if spoken == "approve" else "deny",  # 'reject' → deny
                int(index) if index else None,
            )
        if said in ("terminal tail", "terminal output"):
            self._acquire_explicit_view()
            return self._roster.tail()
        if said in ("terminal history", "terminal inspect", "terminal transcript"):
            # The RUNTIME's record, not JOAT's echo. These were the same
            # command, so "inspect" showed only what this engine happened
            # to relay — nothing at all for a session it did not run.
            self._acquire_explicit_view()
            return await self._roster.history()
        if said.startswith("terminal "):
            route = parse_reply_route(said[len("terminal "):])
            if route is not None:
                return await self.route_reply(*route)
            # Everything after the surface name is the input, from the RAW
            # text so punctuation and casing survive into the session.
            raw = text.strip()
            body = raw[raw.lower().index("terminal") + len("terminal"):].strip(" ,:")
            if body:
                self._acquire_explicit_view()
                return await self._roster.send(body)
        return None
