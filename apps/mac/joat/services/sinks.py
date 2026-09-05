"""Where a debate is watched.

The wearer's requirement was visibility, twice over: streamed to the glasses
("I wanna see as they're debating ... otherwise I cannot prove that was
debated") and streamed to the terminal, where a long discussion is actually
readable. Those are different surfaces with opposite constraints — seven
74-column lines and a glance versus a scrollback — so the round table writes
to a list of sinks and neither surface knows about the other.

Adding a sink (a file, a web view, a transcript archive) is a class, not a
change to the debate.
"""

from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass
from collections.abc import Callable
from typing import Protocol, TextIO

from .hud import HudWriter
from ..core.protocol import (
    BRAINSTORM_ACTIVITY_MAX,
    AgentTerminalHudView,
    BRAINSTORM_ROW_COLUMNS,
    BrainstormHudView,
)
from .roundtable import is_failure

FinishedSession = Callable[
    [str, tuple[str, ...], tuple[tuple[str, str], ...], str], None
]


class BrainstormHistoryLike(Protocol):
    """What the durable store offers this sink — the begin/finish pair."""

    def begin(self, topic: str, speakers: tuple[str, ...], *, session_id: str,
              parent_id: str = "", root_id: str = "") -> None: ...
    def finish(self, session_id: str, turns: tuple[tuple[str, str], ...],
               verdict: str, *, verified: bool | None = None) -> None: ...


class TranscriptSink(Protocol):
    """One place a debate is rendered as it happens."""

    async def open(self, topic: str, speakers: list[str]) -> None: ...
    async def turn(self, label: str) -> None: ...
    async def chunk(self, text: str) -> None: ...
    async def close(self, verdict: str) -> None: ...


_BLANK_LINES = re.compile(r"\n[ \t]*(?:\n[ \t]*)+")
"""Runs of blank rows, which the glasses cannot afford."""


class HudSink:
    """The glasses: a live production Brainstorm page, never a text dump."""

    def __init__(
        self,
        hud: HudWriter,
        terminal_projection: Callable[[], dict[str, str | bool]] | None = None,
    ) -> None:
        self._hud = hud
        self._terminal_projection = terminal_projection
        self._topic = ""
        self._speakers: list[str] = []
        self._speaker = ""
        # A finished turn keeps its GIST; only the LIVE turn keeps its
        # detail. Holding them apart is what makes that possible — a flat
        # string cannot say where one turn ended and the next began.
        self._done: list[str] = []
        self._head = ""
        self._body = ""
        self._placeholder = ""
        self._streaming = False
        self._handed_to_terminal = False
        self._progress = ""
        self._state = "ready"

    @staticmethod
    def _bounded_transcript(value: str) -> str:
        """Keep recent complete terminal rows within the wire contract.

        Bounded by the SAME constant the wire validates against. These were
        two independent literals, and the drift was a silent-failure trap:
        a producer one byte over the wire's bound raises ProtocolError
        inside to_wire(), Broadcast swallows every sink exception so one
        dead surface cannot stop a debate, and the HUD simply stops
        updating with nothing logged.
        """

        if len(value) <= BRAINSTORM_ACTIVITY_MAX:
            return value
        tail = value[-BRAINSTORM_ACTIVITY_MAX:]
        boundary = tail.find("\n")
        return tail[boundary + 1:] if boundary >= 0 else tail

    _GIST = re.compile(r"^\s*GIST:\s*(\S.*)$", re.MULTILINE)

    ROW_COLUMNS = BRAINSTORM_ROW_COLUMNS
    """Character budget for a COLLAPSED turn's single row.

    Was 74, derived for the old monospace face. The surface now sets
    light proportional type (Helvetica Neue at 12px, ~5.2px average
    advance over a 504px column ≈ 96 characters), and the wearer caught
    the stale budget on screen within minutes: collapsed rows ended in
    "…" with a quarter of the width still empty — "why are some words
    being cut off when there is obviously room on the right?"

    An estimate, deliberately: the engine cannot measure pixels. A
    glyph-wide row that overruns simply wraps at paint time — the
    renderer wraps by measured pixels — which costs one extra row rarely
    instead of wasting 100px always.
    """

    @classmethod
    def _summarise(cls, body: str, budget: int) -> str:
        """One line standing for a whole finished turn.

        Every collaborator is instructed to open with `GIST: <a dozen
        words>` (build_prompt), so the one-line summary is the speaker's
        own, not something inferred here. Turns without one fall back to
        their opening words.

        `budget` is what is left of the row AFTER the speaker prefix. A
        fixed body cap looked right until a live two-round session made
        the prefix "CLAUDE — ROUND 2 > " — nineteen columns — and pushed
        finished rows to 83, so the renderer truncated a second time on
        top of this one.
        """
        if is_failure(body):
            # The seat's stdout carried the CLI's complaint ("out of usage
            # credits"). Quoted as a summary it reads exactly like an
            # argument the collaborator never made.
            return "could not answer"
        found = cls._GIST.search(body)
        line = " ".join((found.group(1) if found else body).split())
        limit = max(1, budget)
        return f"{line[:limit - 1]}…" if len(line) > limit else line

    @property
    def _activity(self) -> str:
        """What the glasses show: every finished turn as one line, plus the
        live turn in full.

        A 120-word turn wraps to about nine 74-column lines, and the
        transcript region draws at most seven — so a raw stream showed
        roughly ONE turn and the wearer could not follow who had said what.
        The wearer's requirement was watching the debate happen; a session
        they cannot follow does not meet it however faithfully it streams.
        """
        rows = list(self._done)
        if self._head or self._body:
            rows.append(f"{self._head}{self._body}")
        elif not rows and self._placeholder:
            rows.append(self._placeholder)
        # A blank line costs a whole row out of seven. Models separate their
        # GIST from their argument with one, and the SYNTHESIS separates
        # AGREED from OPEN the same way — and the synthesis is retired
        # uncollapsed on purpose, so an earlier version of this that only
        # cleaned the live row left a blank row in the one piece of content
        # the wearer sat through the session for.
        #
        # Applied to the whole rendering, at RENDER time: chunks arrive
        # mid-sequence while streaming, so "\n" then "\n" would slip past
        # any per-chunk check. The stored transcript, the saved history and
        # the Markdown export keep the wearer's paragraphs.
        return self._bounded_transcript(_BLANK_LINES.sub(chr(10), "\n".join(rows)))

    def _retire_live_turn(self, *, collapse: bool = True) -> None:
        """Move the live turn into the finished rows."""
        if self._head or self._body:
            if not self._streaming or not self._body.strip():
                # The turn is over and produced nothing usable — a CLI whose
                # output was entirely noise-filtered, one that exited cleanly
                # and silently, or one whose only chunk was a stray newline.
                #
                # Both cases matter. Keying this on "did a chunk arrive"
                # alone caught the silent turn and left its sibling: a turn
                # that streamed WHITESPACE retired as a bare "CODEX >", an
                # empty speaker row spending a line to say nothing — while
                # keeping the marker would have recorded "CODEX > Thinking…"
                # as that seat's contribution for the rest of the session.
                body = "said nothing"
            elif collapse:
                body = self._summarise(
                    self._body, self.ROW_COLUMNS - len(self._head)
                )
            else:
                body = self._body
            self._done.append(f"{self._head}{body}")
        self._head = ""
        self._body = ""

    def _terminal_status(self) -> dict[str, str | bool]:
        if self._terminal_projection is not None:
            return self._terminal_projection()
        return {
            "state": "unavailable",
            "reason": "gateway_not_ready",
            "endpoint_host": "",
            "no_tools_checked": False,
        }

    @staticmethod
    def _display_speaker(label: str) -> str:
        """Keep a compact participant name while retaining live turn context."""
        name, separator, remainder = label.partition(" (")
        if not separator:
            return label[:64]
        closing = remainder.find(")")
        if closing < 0:
            return name[:64]
        suffix = remainder[closing + 1:].strip()
        return f"{name} {suffix}"[:64] if suffix else name[:64]

    async def _paint(self, state: str) -> None:
        self._state = state
        if self._handed_to_terminal and state == "running":
            # The wearer tapped away to the terminal mid-session. The
            # debate keeps streaming into the transcript, but it must not
            # snatch the lenses back token by token; closing verdicts
            # still land so a finished session is never hidden.
            return
        await self._hud.show_brainstorm(BrainstormHudView(
            state=state,
            topic=self._topic,
            collaborators=[speaker.split(" (")[0][:48] for speaker in self._speakers[:3]],
            # The live line carries the COST as well as the speaker: two
            # subscription CLIs bill per turn, and a wearer who cannot see
            # how many rounds remain cannot decide whether to let it run.
            active_speaker=(
                f"{self._display_speaker(self._speaker)} · {self._progress}"[:64]
                if self._speaker and self._progress
                else self._progress or self._display_speaker(self._speaker)
            ),
            activity=self._activity,
        ))

    async def progress(self, round_no: int, max_rounds: int, calls: int) -> None:
        """Where the session is, and what it has spent so far.

        Optional by design: discovered with getattr, so a terminal or a
        snapshot sink never has to implement a HUD concern.
        """
        self._progress = f"round {round_no}/{max_rounds} · {calls} calls"

    async def open(self, topic: str, speakers: list[str]) -> None:
        self._topic = topic[:96]
        self._speakers = list(speakers)
        self._speaker = ""
        self._done = []
        self._head = ""
        self._body = ""
        self._placeholder = "JOAT > Preparing collaborators…"
        # Claim the initial entry once. Ordinary streamed frames still yield
        # to a menu the wearer opens later; they cannot supply this handoff.
        await self._hud.begin_brainstorm()
        await self._paint("running")

    THINKING_MARKER = "Thinking…"
    """What a seat shows while it has produced nothing yet.

    HudCanvasRenderer keys its local elapsed counter off this exact
    string (`endsWith('Thinking…')`), so it is a contract across two
    repositories, not a label. test_sinks pins both spellings together;
    without that, renaming it here would stop the clock on the glasses
    with every test in both repositories still green."""

    # The silent-wait counter is animated ON DEVICE now. It used to be
    # computed here and re-shipped as a whole frame over BLE every three
    # seconds — the wearer noticed: "it's only changing every three
    # seconds … we're doing it the difficult way." The engine paints the
    # "Thinking…" marker once; the plugin appends live elapsed seconds
    # locally — smooth at one second, free on the link.

    async def turn(self, label: str) -> None:
        self._retire_live_turn()
        self._placeholder = ""
        self._speaker = label
        self._head = f"{self._display_speaker(label).upper()} > "
        self._body = self.THINKING_MARKER
        self._streaming = False
        await self._paint("running")

    async def chunk(self, text: str) -> None:
        # Tokens are arriving, so the stream is its own progress signal and
        # the silence marker has done its job: the FIRST chunk retires it
        # and takes its place on the row.
        #
        # Only the first chunk may rewrite the row. Every later chunk is
        # appended byte for byte, because a streaming model splits words
        # across chunks — an earlier version re-examined the whole row on
        # each chunk and ate real text from a turn that opened with the
        # word "first". Tracking whether the turn has begun makes that
        # class of mistake unreachable rather than merely unlikely.
        if not self._streaming:
            self._streaming = True
            self._body = text.lstrip()
        else:
            self._body += text
        await self._paint("running")

    async def close(self, verdict: str) -> None:
        """End the session WITHOUT erasing what it produced.

        This used to replace the whole transcript with 'JOAT > <verdict>',
        so the verified synthesis — the thing the wearer sat through the
        session for — vanished at the exact moment it finished, leaving a
        one-line status where the answer had been. The verdict is a
        footer, not a replacement.
        """
        self._handed_to_terminal = False
        # The last live turn is the SYNTHESIS — the thing the wearer sat
        # through the session for. Every other turn collapses; this one
        # stays whole.
        self._retire_live_turn(collapse=False)
        self._placeholder = ""
        self._speaker = ""
        self._progress = ""
        self._done.append(f"JOAT > {verdict}")
        if verdict == "cancelled":
            await self._paint("cancelled")
        elif verdict.startswith("blocked"):
            await self._paint("blocked")
        else:
            await self._paint("settled")

    async def show_terminal(self) -> None:
        """Hand the lenses to the terminal without stopping the roundtable.

        Watched live in the simulator: the tap painted this view for ONE
        frame, then the next streamed token repainted the Brainstorm over
        it — a working handoff stomped by its own session. While the
        wearer is looking at the terminal, streaming continues silently
        and repaints are withheld; replay() is the way back.
        """
        self._handed_to_terminal = True
        terminal = self._terminal_status()
        await self._hud.show_agent_terminal(AgentTerminalHudView(
            state="running",
            focus="hermes",
            lanes=[],
            activity=self._activity or "JOAT > Brainstorm remains active. Tap to return to the room.",
            endpoint_host=str(terminal["endpoint_host"]),
            reason=str(terminal["reason"]),
            no_tools_checked=bool(terminal["no_tools_checked"]),
        ))

    def terminal_owns_hud(self) -> bool:
        """True while the wearer has tapped away to the terminal."""
        return self._handed_to_terminal

    async def replay(self) -> None:
        """Repaint the last semantic frame (reconnect, or back from the
        terminal handoff)."""
        self._handed_to_terminal = False
        if self._topic:
            await self._paint(self._state)


class TerminalSink:
    """The terminal: the surface where a long debate is actually readable.

    Writes directly to a stream rather than through logging, because the
    point is to watch it arrive — log formatting would line-buffer the
    turn into a single block after the fact, which is exactly the
    "background debate that only reports a verdict" the wearer rejected.
    """

    def __init__(self, out: TextIO | None = None, use_color: bool | None = None) -> None:
        self._out = out if out is not None else sys.stdout
        self._color = (self._out.isatty() if use_color is None else use_color)

    def _dim(self, text: str) -> str:
        return f"\033[2m{text}\033[0m" if self._color else text

    def _bold(self, text: str) -> str:
        return f"\033[1m{text}\033[0m" if self._color else text

    async def open(self, topic: str, speakers: list[str]) -> None:
        self._write(f"\n{self._bold('BRAINSTORM')} {topic}\n")
        self._write(self._dim(f"  round table: {' → '.join(speakers)}\n"))

    async def turn(self, label: str) -> None:
        self._write(f"\n{self._bold('» ' + label)}\n")

    async def chunk(self, text: str) -> None:
        self._write(text)

    async def close(self, verdict: str) -> None:
        self._write(f"\n\n{self._dim('— ' + verdict)}\n\n")

    def _write(self, text: str) -> None:
        self._out.write(text)
        self._out.flush()  # unbuffered: the point is watching it arrive


class Broadcast:
    """Every sink sees every turn; one failing sink never stops the debate."""

    def __init__(self, sinks: list[TranscriptSink]) -> None:
        self._sinks = sinks

    async def open(self, topic: str, speakers: list[str]) -> None:
        await self._each("open", topic, speakers)

    async def turn(self, label: str) -> None:
        await self._each("turn", label)

    async def chunk(self, text: str) -> None:
        await self._each("chunk", text)

    async def close(self, verdict: str) -> None:
        await self._each("close", verdict)

    async def _each(self, method: str, *args: object) -> None:
        for sink in self._sinks:
            try:
                await getattr(sink, method)(*args)
            except Exception:
                # A dead HUD must not silence the terminal, and vice versa.
                # The debate is the product; a surface is not.
                pass

    def adopt_identity(
        self, session_id: str, parent_id: str, root_id: str
    ) -> None:
        for sink in self._sinks:
            callback = getattr(sink, "adopt_identity", None)
            if callback is not None:
                callback(session_id, parent_id, root_id)

    def adopt_verification(self, verified: bool | None) -> None:
        for sink in self._sinks:
            callback = getattr(sink, "adopt_verification", None)
            if callback is not None:
                callback(verified)

    async def show_terminal(self) -> None:
        for sink in self._sinks:
            callback = getattr(sink, "show_terminal", None)
            if callback is not None:
                await callback()

    async def replay(self) -> None:
        for sink in self._sinks:
            callback = getattr(sink, "replay", None)
            if callback is not None:
                await callback()

    def terminal_owns_hud(self) -> bool:
        for sink in self._sinks:
            callback = getattr(sink, "terminal_owns_hud", None)
            if callback is not None and callback():
                return True
        return False


@dataclass
class DebateSnapshot:
    """What a Debate panel needs to draw, without replaying a token stream."""

    topic: str = ""
    speakers: tuple[str, ...] = ()
    speaking: str = ""
    turns: tuple[tuple[str, str], ...] = ()
    verdict: str = ""
    running: bool = False

    def headline(self) -> str:
        """The panel header, in the dashboard's "Messages · 3" shape."""
        if not self.topic:
            return "Brainstorm"
        if self.running:
            return f"Brainstorm · {len(self.speakers)} seats"
        return f"Brainstorm · {self.verdict}" if self.verdict else "Brainstorm"

    def rows(self, width: int = 60, limit: int = 8) -> list[str]:
        """Newest-last list rows for the HUD's fixed 40px list layout.

        The dashboard renders each capability as ROWS, not prose — the
        Messages panel shows "who / when / first line", so the Debate panel
        shows "who said what", one turn per row. Prose is what the wearer
        gets by opening a turn, not what the panel is made of.
        """
        out = []
        for label, text in self.turns[-limit:]:
            who = label.split(" (")[0]
            # A seat that failed reads as failed. Its stdout carried the
            # CLI's complaint ("You're out of usage credits"), and a row
            # quoting that looks exactly like an argument it never made.
            body = "could not answer" if is_failure(text) else " ".join(text.split())
            out.append(f"{who}: {body}"[:width])
        if self.running and self.speaking:
            out.append(f"{self.speaking.split(' (')[0]}: …"[:width])
        elif self.verdict:
            out.append(f"— {self.verdict}"[:width])
        return out

    def last_reply(self, who: str | None = None) -> tuple[str, str] | None:
        """The newest full turn — the thing JOAT routes onward.

        The wearer's design, in their words: "asking to take the last
        response from claude or codex and send it to the hermes in the
        Agent Terminal" — JOAT is the glue between the surfaces, so the
        snapshot must be able to answer "what did Claude just say?" with
        the WHOLE reply, not the collapsed row the panel draws.

        ``who`` filters by seat name ("claude", "codex"), matched against
        the label's name half so "CLAUDE (critic)" answers to "claude".
        Failed turns are skipped: routing a CLI's usage-limit complaint
        into a work session would execute an error message.
        """
        wanted = who.strip().lower() if who else None
        for label, text in reversed(self.turns):
            if is_failure(text):
                continue
            name = label.split(" (")[0].strip().lower()
            if wanted is not None and name != wanted:
                continue
            return label, text
        return None


class DebateStateSink:
    """Keeps the current debate as STATE, so a panel can be reopened.

    The other surfaces are write-once streams: a chunk paints and is gone.
    A menu-driven dashboard needs the opposite — the wearer opens Debate
    minutes later and must see what was said, so someone has to hold it.
    Deliberately a plain snapshot with no HUD imports: the panel that draws
    it and the debate that fills it stay independent.
    """

    def __init__(
        self,
        on_finished: FinishedSession | None = None,
        history: "BrainstormHistoryLike | None" = None,
    ) -> None:
        self.snapshot = DebateSnapshot()
        self._current: list[str] = []
        # Called once when a session closes, so the answer outlives the
        # process that produced it.
        self._on_finished = on_finished
        # The durable store, when the richer contract is wanted: an
        # ``active`` record at open, updated in place at close, with the
        # identity and verification state SESSION_MODEL.md:30-35 lists.
        # ``on_finished`` alone silently dropped all of that — the record
        # API accepted session_id/parent_id/root_id/verified and the one
        # production caller passed four positional arguments, so every
        # field the picker and lineage were built on was minted fresh at
        # close and the real identity thrown away.
        self._history = history
        self._identity: tuple[str, str, str] = ("", "", "")
        self._verified: bool | None = None

    def adopt_identity(
        self, session_id: str, parent_id: str, root_id: str
    ) -> None:
        """Identity arrives from the session that owns it, before open()."""
        self._identity = (session_id, parent_id, root_id)

    def adopt_verification(self, verified: bool | None) -> None:
        """The verifier's overall outcome, before close()."""
        self._verified = verified

    async def open(self, topic: str, speakers: list[str]) -> None:
        self.snapshot = DebateSnapshot(topic=topic, speakers=tuple(speakers),
                                       running=True)
        self._current = []
        self._verified = None
        session_id, parent_id, root_id = self._identity
        if self._history is not None and session_id:
            try:
                self._history.begin(
                    topic, tuple(speakers), session_id=session_id,
                    parent_id=parent_id, root_id=root_id,
                )
            except Exception:
                pass

    async def turn(self, label: str) -> None:
        self._flush()
        self.snapshot.speaking = label

    async def chunk(self, text: str) -> None:
        self._current.append(text)

    async def close(self, verdict: str) -> None:
        self._flush()
        self.snapshot.speaking = ""
        self.snapshot.verdict = verdict
        self.snapshot.running = False
        session_id = self._identity[0]
        self._identity = ("", "", "")
        # Never let persistence failure take down the session that
        # just produced the answer — the wearer already has it.
        if self._history is not None and session_id:
            try:
                self._history.finish(
                    session_id, self.snapshot.turns, verdict,
                    verified=self._verified,
                )
            except Exception:
                pass
            return
        if self._on_finished is not None:
            try:
                self._on_finished(
                    self.snapshot.topic, self.snapshot.speakers,
                    self.snapshot.turns, verdict,
                )
            except Exception:
                pass

    def _flush(self) -> None:
        said = "".join(self._current).strip()
        self._current = []
        if said and self.snapshot.speaking:
            self.snapshot.turns = self.snapshot.turns + ((self.snapshot.speaking, said),)


__all__ = ["Broadcast", "DebateSnapshot", "DebateStateSink", "HudSink",
           "TerminalSink", "TranscriptSink"]
