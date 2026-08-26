"""Facts the model cannot know, supplied fresh at every reply.

A language model has no clock, no location, and no view of the wearer's
device. Asked "what time is it" it does not decline — it CONFABULATES, and
on a heads-up display a confident wrong answer is worse than no answer: the
wearer has no cue that the assistant was guessing. (Observed live: JOAT
answered "Current time is 2:47 PM" at 4:44 PM.)

So grounding is injected, not inferred. This is a TOOLS/context seam, not a
keyword router: the model still decides what to say, it just gets the facts
first — the deterministic runtime supplies what it knows, the mind reasons
over it (see the architecture ruling in docs/ARCHITECTURE.md).

Sources are pluggable so device and location facts can join later without
touching the reply engine.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

FactSource = Callable[[], str | None]


@dataclass
class Grounding:
    """Composes the fact block prepended to the system prompt."""

    sources: list[FactSource] = field(default_factory=list)

    def add(self, source: FactSource) -> None:
        self.sources.append(source)

    def facts(self) -> str:
        """Current facts, one per line. Empty string when nothing is known."""
        lines: list[str] = []
        for source in self.sources:
            try:
                value = source()
            except Exception:
                # A broken source must never cost the wearer their answer.
                continue
            if value:
                lines.append(f"- {value}")
        return "\n".join(lines)


def local_clock(now: Callable[[], float] = time.time) -> FactSource:
    """Date and time in the Mac's local zone — recomputed per reply.

    Per reply, not per launch: a clock captured at startup is wrong by
    however long the engine has been up, which is the same failure wearing
    a different mask.
    """

    def source() -> str:
        stamp = time.localtime(now())
        zone = time.strftime("%Z", stamp)
        return (
            f"Current date and time: {time.strftime('%A, %-d %B %Y, %-I:%M %p', stamp)}"
            f"{f' {zone}' if zone else ''}"
        )

    return source


def static_fact(text: str) -> FactSource:
    """A fact that does not change during a session (e.g. the wearer's name)."""
    return lambda: text
