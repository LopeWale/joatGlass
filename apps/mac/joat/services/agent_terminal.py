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
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from ..core.protocol import BRAINSTORM_ROW_COLUMNS, BRAINSTORM_SCROLLBACK_ROWS

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
"""The wire's bound on the header line."""

HUD_LANE_COLUMNS = 48
"""The wire's bound on one lane chip."""


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
    approvals: list[tuple[str, str, str]] = field(default_factory=list)
    """Pending (id, kind, summary) — tool calls waiting on the wearer."""

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    @property
    def state(self) -> str:
        if self.busy:
            return "working"
        if self.last_error:
            return "needs recovery"
        return "idle"


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
        self._discovered: list[dict[str, str]] = []
        # Last chip text painted per lane, so a background lane
        # repaints on a STATE change and not on every line.
        self._marks: dict[str, str] = {}
        """Rows from the last discover(); what 'resume <label>' can name."""

    # ── roster ───────────────────────────────────────────────────────────

    @property
    def focused(self) -> str | None:
        return self._focus

    def names(self) -> list[str]:
        return list(self._lanes)

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
        state = (
            "running" if lane.busy
            else "approval_required" if lane.approvals
            else "blocked" if lane.last_error
            else "ready"
        )
        return TerminalFrame(
            topic=header, state=state,
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

    def focus(self, name: str) -> bool:
        """Switch focus. Never touches any lane's running work."""
        said = name.strip().lower()
        for lane_name in self._lanes:
            if lane_name == said or lane_name in said:
                self._focus = lane_name
                return True
        return False

    def tail(self, name: str | None = None, lines: int = 6) -> str:
        lane = self._lanes.get(name or self._focus or "")
        if lane is None:
            return "No such terminal session."
        if not lane.tail:
            return f"{lane.name}: no output yet."
        return "\n".join(list(lane.tail)[-lines:])

    # ── input, work, cancel ──────────────────────────────────────────────

    async def send(self, text: str, name: str | None = None) -> str:
        """Voice becomes terminal input to the (focused) session — T2.

        Returns the immediate receipt line; the turn itself runs as a task
        so the wearer can switch away. One in-flight turn per lane: the
        runtime serializes anyway, so a queued second utterance would only
        fire much later with stale intent — refusing is more honest.
        """
        lane = self._lanes.get(name or self._focus or "")
        if lane is None:
            return "No terminal session is focused."
        if lane.busy:
            return (f"{lane.name} is still working — say 'terminal cancel' "
                    "or wait for it to finish.")
        lane.last_error = ""
        lane.tail.append(f"> {text}")
        lane.task = asyncio.get_running_loop().create_task(
            self._run_send(lane, text), name=f"terminal-{lane.name}"
        )
        return f"{lane.name} ▸ sent."

    async def _run_send(self, lane: TerminalLane, text: str) -> None:
        pending = ""
        try:
            async for chunk in lane.agent.stream(text):
                pending += chunk
                *lines, pending = pending.split("\n")
                for line in lines:
                    if line.strip():
                        lane.tail.append(line)
                        await self._narrate(lane, line)
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
            (lane for lane in self._lanes.values()
             if hasattr(lane.agent, "list_sessions")),
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
            label = row.get("label") or row.get("key") or "?"
            lines.append(f"• {label}")
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
        match = None
        for row in self._discovered:
            label = (row.get("label") or "").lower()
            if label and (label == said or said in label or label in said):
                match = row
                break
        if match is None:
            return f"No listed session matches '{spoken}'."
        lane = next(
            (lane for lane in self._lanes.values()
             if hasattr(lane.agent, "resume_session")),
            None,
        )
        if lane is None:
            return "No terminal lane on this Mac can resume a session."
        if lane.busy:
            return f"{lane.name} is still working — cancel it before resuming."
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
        lane = self._lanes.get(self._focus or "")
        if lane is None:
            return "No terminal session is focused."
        if not lane.approvals:
            return f"{lane.name}: nothing waiting for approval."
        if index is None and len(lane.approvals) > 1:
            return (f"{lane.name} has {len(lane.approvals)} waiting — say "
                    f"'terminal {decision} 1' through "
                    f"'terminal {decision} {len(lane.approvals)}'.")
        position = (index or 1) - 1
        if not 0 <= position < len(lane.approvals):
            return f"{lane.name}: no approval number {index}."
        resolver = getattr(lane.agent, "resolve_approval", None)
        if resolver is None:
            return f"{lane.name} cannot answer approvals."
        approval_id, kind, summary = lane.approvals[position]
        try:
            recorded = await resolver(approval_id, kind, decision)
        except Exception as exc:
            lane.last_error = str(exc) or exc.__class__.__name__
            return f"{lane.name}: approval failed — {lane.last_error}"
        lane.approvals.pop(position)
        label = summary or approval_id
        if recorded is None:
            return (f"{lane.name}: sent {decision} for '{label}' but the "
                    "gateway gave no legible receipt.")
        if recorded != decision:
            return (f"{lane.name}: another controller answered first — "
                    f"recorded {recorded} for '{label}'.")
        lane.tail.append(f"[approval] {recorded}: {label}")
        return f"{lane.name}: recorded {recorded} for '{label}'."

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
        if not lane.busy or lane.task is None:
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
    r"^(?:terminal switch to|switch terminal to|switch the terminal to)"
    r" (?P<name>[a-z0-9 ]+)$"
)

# Cross-surface routing: JOAT is the glue between Brainstorm and this
# terminal. "Send Claude's last reply to Hermes" takes a collaborator's
# whole answer out of the discussion and hands it to a work session —
# the wearer's own words for what they are building: "the JOAT assistant
# should be able to take anything and do something i ask it to do with
# it". Matched against the normalized transcript, where "Claude's"
# arrives as "claude s".
_ROUTE_REPLY = re.compile(
    r"^send (?:the )?(?:(?P<who>codex|claude)(?: ?s)? )?"
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
                 ) -> None:
        self._roster = roster
        self._armed_until = 0.0
        self._window_s = self.ARM_WINDOW_S if window_s is None else window_s
        # Answers "what did <seat> last say?" from the Brainstorm snapshot.
        # Injected, because the discussion belongs to another feature and
        # this grammar must not import its internals to quote it.
        self._last_reply = last_reply

    def arm(self) -> None:
        """A tap on the terminal promises the NEXT utterance as its input.

        The promise is one utterance and it is explicit, so the phrase does
        not need the 'terminal' prefix — the gesture already established
        that this is terminal speech, which is exactly the consent the
        prefix exists to obtain. It also expires: see ARM_WINDOW_S.
        """
        self._armed_until = time.monotonic() + self._window_s

    @property
    def armed(self) -> bool:
        return time.monotonic() < self._armed_until

    def _disarm(self) -> None:
        self._armed_until = 0.0

    async def handle_command(self, text: str) -> str | None:
        if self.armed:
            self._disarm()
            said = text.strip()
            if not said:
                return None
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
            return self._roster.overview()
        m = _SWITCH.match(said)
        if m is not None:
            name = m.group("name").strip()
            if self._roster.focus(name):
                return f"Focused on {self._roster.focused}."
            return f"No terminal session called {name}. " + self._roster.overview()
        if said in ("terminal cancel", "terminal stop", "cancel the terminal"):
            return await self._roster.cancel()
        if said in ("terminal approvals", "terminal pending"):
            return self._roster.approvals_view()
        if said in ("terminal discover", "terminal refresh",
                    "terminal all sessions"):
            return await self._roster.discover()
        resume_match = _RESUME.match(said)
        if resume_match is not None:
            return await self._roster.resume(resume_match.group("name"))
        decision_match = _DECIDE.match(said)
        if decision_match is not None:
            spoken = decision_match.group("decision")
            index = decision_match.group("index")
            return await self._roster.decide(
                "approve" if spoken == "approve" else "deny",  # 'reject' → deny
                int(index) if index else None,
            )
        if said in ("terminal tail", "terminal output", "terminal history"):
            return self._roster.tail()
        if said.startswith("terminal "):
            route = parse_reply_route(said[len("terminal "):])
            if route is not None:
                return await self.route_reply(*route)
            # Everything after the surface name is the input, from the RAW
            # text so punctuation and casing survive into the session.
            raw = text.strip()
            body = raw[raw.lower().index("terminal") + len("terminal"):].strip(" ,:")
            if body:
                return await self._roster.send(body)
        return None
