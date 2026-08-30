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
from typing import Any
from dataclasses import dataclass, field

FactSource = Callable[[], str | None]


@dataclass
class Grounding:
    """Composes the fact block prepended to the system prompt."""

    sources: list[FactSource] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    """Things the assistant has NO source for, named explicitly.

    Omitting a fact is a weak signal: a 3B model must infer "battery is
    absent from this list, therefore I should decline" — exactly the
    inference small models fail, and exactly how the clock got invented.
    Naming the gap is a far stronger instruction than leaving a hole.
    """

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

    def cannot_know(self) -> str:
        """The 'do not invent these' block, or empty when nothing is listed."""
        return ", ".join(self.unknowns)


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


def device_facts(transport: Any) -> FactSource:
    """Glasses battery / worn / charging — already parsed and cached.

    This data has been arriving over the wire and sitting on the transport
    the whole time, while "what is my battery" was answered by a model that
    had never seen it. Same failure as the clock, different costume.
    """

    def source() -> str:
        device = getattr(transport, "device", None)
        if device is None:
            return "Glasses battery: unknown (no device report received yet)"
        bits: list[str] = []
        if device.battery is not None:
            charging = " (charging)" if device.charging else ""
            bits.append(f"battery {device.battery}%{charging}")
        if device.wearing is not None:
            bits.append("being worn" if device.wearing else "not being worn")
        hello = getattr(transport, "hello", None)
        if hello is not None:
            bits.append(f"model {hello.model}, serial {hello.sn}")
        return f"Glasses: {', '.join(bits)}" if bits else ""

    return source


def location_facts(transport: Any) -> FactSource:
    """Whether the wearer's position is known — stated either way.

    The coordinates themselves stay OUT of the prompt (a model reciting raw
    lat/lon at the wearer is noise).  A cached fix must be explicitly fresh;
    a disconnected or expired phone fix is not usable assistant context.
    """

    def source() -> str:
        fresh_location = getattr(transport, "fresh_location", None)
        loc = fresh_location() if callable(fresh_location) else None
        if loc is None:
            return ("Wearer location: unknown or stale. Local tools may "
                    "request a fresh phone fix; otherwise ask for a city.")
        return ("Wearer location: fresh. get_weather with no location and "
                "find_nearby_places use it privately.")

    return source


def static_fact(text: str) -> FactSource:
    """A fact that does not change during a session (e.g. the wearer's name)."""
    return lambda: text
