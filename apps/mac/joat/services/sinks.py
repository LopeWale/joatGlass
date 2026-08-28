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

import sys
from dataclasses import dataclass
from typing import Protocol, TextIO

from .hud import HudWriter
from .roundtable import is_failure


class TranscriptSink(Protocol):
    """One place a debate is rendered as it happens."""

    async def open(self, topic: str, speakers: list[str]) -> None: ...
    async def turn(self, label: str) -> None: ...
    async def chunk(self, text: str) -> None: ...
    async def close(self, verdict: str) -> None: ...


class HudSink:
    """The glasses: two lines at a glance, streamed token by token."""

    def __init__(self, hud: HudWriter) -> None:
        self._hud = hud

    async def open(self, topic: str, speakers: list[str]) -> None:
        await self._hud.begin_brainstorm()
        await self._hud.status(f"BRAINSTORM · {topic[:30]}")
        await self._hud.show(f"{' · '.join(speakers)}\ndiscussing…")

    async def turn(self, label: str) -> None:
        # A fresh paint per turn: the HUD shows WHO is speaking now, not a
        # scrolling wall the wearer has to re-read while walking.
        await self._hud.show(f"» {label}\n")

    async def chunk(self, text: str) -> None:
        await self._hud.append(text)

    async def close(self, verdict: str) -> None:
        await self._hud.status(f"BRAINSTORM · {verdict}")


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

    def __init__(self) -> None:
        self.snapshot = DebateSnapshot()
        self._current: list[str] = []

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

    def _flush(self) -> None:
        said = "".join(self._current).strip()
        self._current = []
        if said and self.snapshot.speaking:
            self.snapshot.turns = self.snapshot.turns + ((self.snapshot.speaking, said),)


__all__ = ["Broadcast", "DebateSnapshot", "DebateStateSink", "HudSink",
           "TerminalSink", "TranscriptSink"]
