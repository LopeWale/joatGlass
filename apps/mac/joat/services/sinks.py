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
import time
from dataclasses import dataclass
from collections.abc import Callable
from typing import Protocol, TextIO

from .hud import HudWriter

FinishedSession = Callable[
    [str, tuple[str, ...], tuple[tuple[str, str], ...], str], None
]
from ..core.protocol import BRAINSTORM_ACTIVITY_MAX, BrainstormHudView
from .roundtable import is_failure


class TranscriptSink(Protocol):
    """One place a debate is rendered as it happens."""

    async def open(self, topic: str, speakers: list[str]) -> None: ...
    async def turn(self, label: str) -> None: ...
    async def chunk(self, text: str) -> None: ...
    async def close(self, verdict: str) -> None: ...


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
        self._wait_task: asyncio.Task[None] | None = None
        self._streaming = False
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

    ROW_COLUMNS = 74
    """What one transcript row holds on the glasses.

    HudCanvasRenderer draws the transcript at 11px in the `"SF Mono", …,
    Menlo, monospace` stack across a 504px column; SF Mono's advance is
    1266/2048 em, so 504 / 6.80 = 74 characters. See
    BRAINSTORM_ACTIVITY_MAX, which is this times the tallest surface's
    line count.
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
        return self._bounded_transcript("\n".join(rows))

    def _retire_live_turn(self, *, collapse: bool = True) -> None:
        """Move the live turn into the finished rows."""
        if self._head or self._body:
            if not self._streaming:
                # The turn is over and produced nothing — a CLI whose output
                # was entirely noise-filtered, or which exited cleanly and
                # silently. Keeping the marker would record "CODEX >
                # Thinking…" as that seat's contribution for the rest of the
                # session: the same false status this sink already had to
                # stop printing in front of live answers.
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
        terminal = self._terminal_status()
        await self._hud.show_brainstorm(BrainstormHudView(
            screen="brainstorm",
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
            terminal_state=str(terminal["state"]),
            endpoint_host=str(terminal["endpoint_host"]),
            terminal_reason=str(terminal["reason"]),
            no_tools_checked=bool(terminal["no_tools_checked"]),
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
        await self._paint("running")

    WAIT_TICK_S = 3.0
    """How often the silent-wait counter repaints.

    Only while a collaborator has produced NOTHING. Once tokens arrive the
    stream is its own progress signal, so the ticker stops and costs the
    link nothing."""

    async def turn(self, label: str) -> None:
        self._retire_live_turn()
        self._placeholder = ""
        self._speaker = label
        self._head = f"{self._display_speaker(label).upper()} > "
        self._body = "Thinking…"
        self._streaming = False
        await self._paint("running")
        self._start_wait_clock()

    def _start_wait_clock(self) -> None:
        """Count the silence. A turn takes 12-40s and says nothing while it
        thinks; without a moving number the wearer cannot tell working from
        hung, and the only remedy they know is to cancel a healthy session.
        """
        self._stop_wait_clock()
        started = time.monotonic()

        async def tick() -> None:
            try:
                while True:
                    await asyncio.sleep(self.WAIT_TICK_S)
                    waited = int(time.monotonic() - started)
                    self._body = (
                        f"Thinking… {waited // 60}:{waited % 60:02d}"
                    )
                    await self._paint("running")
            except asyncio.CancelledError:
                raise

        self._wait_task = asyncio.get_running_loop().create_task(tick())

    def _stop_wait_clock(self) -> None:
        task, self._wait_task = self._wait_task, None
        if task is not None and not task.done():
            task.cancel()

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
        self._stop_wait_clock()
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
        self._stop_wait_clock()
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
        """Show Hermes connection truth without stopping the live roundtable."""
        terminal = self._terminal_status()
        await self._hud.show_brainstorm(BrainstormHudView(
            screen="agent_terminal",
            state="running",
            topic="Hermes Agent Terminal",
            collaborators=[],
            activity=self._activity or "JOAT > Brainstorm remains active. Tap to return to the room.",
            terminal_state=str(terminal["state"]),
            endpoint_host=str(terminal["endpoint_host"]),
            terminal_reason=str(terminal["reason"]),
            no_tools_checked=bool(terminal["no_tools_checked"]),
        ))

    async def replay(self) -> None:
        """Repaint the exact last semantic frame after an authenticated reconnect."""
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


class DebateStateSink:
    """Keeps the current debate as STATE, so a panel can be reopened.

    The other surfaces are write-once streams: a chunk paints and is gone.
    A menu-driven dashboard needs the opposite — the wearer opens Debate
    minutes later and must see what was said, so someone has to hold it.
    Deliberately a plain snapshot with no HUD imports: the panel that draws
    it and the debate that fills it stay independent.
    """

    def __init__(self, on_finished: FinishedSession | None = None) -> None:
        self.snapshot = DebateSnapshot()
        self._current: list[str] = []
        # Called once when a session closes, so the answer outlives the
        # process that produced it.
        self._on_finished = on_finished

    async def open(self, topic: str, speakers: list[str]) -> None:
        self.snapshot = DebateSnapshot(topic=topic, speakers=tuple(speakers),
                                       running=True)
        self._current = []

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
        if self._on_finished is not None:
            # Never let persistence failure take down the session that
            # just produced the answer — the wearer already has it.
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
