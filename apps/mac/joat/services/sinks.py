"""Where a debate is watched.

The wearer's requirement was visibility, twice over: streamed to the glasses
("I wanna see as they're debating ... otherwise I cannot prove that was
debated") and streamed to the terminal, where a long discussion is actually
readable. Those are different surfaces with opposite constraints — 46
characters and a glance versus a scrollback — so the round table writes to a
list of sinks and neither surface knows about the other.

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
from ..core.protocol import BrainstormHudView
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
        self._activity = ""
        self._wait_task: asyncio.Task[None] | None = None
        self._state = "ready"

    @staticmethod
    def _bounded_transcript(value: str) -> str:
        """Keep recent complete terminal rows within the wire contract."""

        if len(value) <= 180:
            return value
        tail = value[-180:]
        boundary = tail.find("\n")
        return tail[boundary + 1:] if boundary >= 0 else tail

    def _replace_last_transcript(self, row: str) -> None:
        """Update the newest row in place — the wait clock ticks, it does
        not append a new line every three seconds."""
        head, _, _ = self._activity.rpartition("\n")
        self._activity = self._bounded_transcript(f"{head}\n{row}" if head else row)

    _WAIT_CLOCK = re.compile(r"(Thinking…) \d+:\d{2}$")

    def _strip_wait_clock(self) -> None:
        """Drop the elapsed suffix before the answer lands on the same row.

        Matched as a CLOCK, not as 'anything after Thinking…'. A looser
        strip ate real answer text: a turn already reading 'Thinking…
        first' lost its word when the next chunk arrived.
        """
        head, sep, last = self._activity.rpartition("\n")
        trimmed = self._WAIT_CLOCK.sub(r"\1", last)
        if trimmed != last:
            self._activity = f"{head}{sep}{trimmed}" if sep else trimmed

    def _append_transcript(self, row: str) -> None:
        self._activity = self._bounded_transcript(
            f"{self._activity}\n{row}" if self._activity else row
        )

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
            active_speaker=self._display_speaker(self._speaker),
            activity=self._activity,
            terminal_state=str(terminal["state"]),
            endpoint_host=str(terminal["endpoint_host"]),
            terminal_reason=str(terminal["reason"]),
            no_tools_checked=bool(terminal["no_tools_checked"]),
        ))

    async def open(self, topic: str, speakers: list[str]) -> None:
        self._topic = topic[:96]
        self._speakers = list(speakers)
        self._speaker = ""
        self._activity = "JOAT > Preparing collaborators…"
        await self._paint("running")

    WAIT_TICK_S = 3.0
    """How often the silent-wait counter repaints.

    Only while a collaborator has produced NOTHING. Once tokens arrive the
    stream is its own progress signal, so the ticker stops and costs the
    link nothing."""

    async def turn(self, label: str) -> None:
        self._speaker = label
        if self._activity == "JOAT > Preparing collaborators…":
            self._activity = ""
        self._append_transcript(f"{self._display_speaker(label).upper()} > Thinking…")
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
                    speaker = self._display_speaker(self._speaker).upper()
                    self._replace_last_transcript(
                        f"{speaker} > Thinking… {waited // 60}:{waited % 60:02d}"
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
        # `turn()` paints a fixed progress marker before any model bytes
        # arrive. The first token is usually lexical rather than whitespace,
        # so concatenate it as readable prose once; later chunks must retain
        # their exact boundaries because a streaming model may split a word.
        # Tokens are arriving: the stream itself is the progress signal now.
        # Strip the elapsed suffix FIRST — the separator test looks for a
        # bare "Thinking…", and a ticked clock ends in "Thinking… 0:12",
        # so checking before stripping silently skipped both.
        self._stop_wait_clock()
        self._strip_wait_clock()
        needs_prefix_separator = (
            self._activity.endswith("Thinking…") and text and not text[0].isspace()
        )
        separator = " " if needs_prefix_separator else ""
        self._activity = self._bounded_transcript(self._activity + separator + text)
        await self._paint("running")

    async def close(self, verdict: str) -> None:
        self._stop_wait_clock()
        self._speaker = ""
        self._activity = f"JOAT > {verdict}"[:180]
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
