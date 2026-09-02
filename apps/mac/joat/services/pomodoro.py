"""The Pomodoro tab: a focus timer the Mac keeps and the glasses count.

Classic cycle — 25 minutes of focus, a 5 minute break, and a 15 minute
break after four focus blocks. The Mac owns the state machine and the
moment a phase ends; the plugin counts the seconds down from the phase's
end instant on its own clock, so a running timer costs the link nothing.
Every transition pushes a fresh snapshot and drops a card on the HUD, the
same way any other notification arrives — there is no speaker to ring.

Phases run on: a finished focus block starts its break, a finished break
starts the next block. The wearer's hands are busy; the timer should not
wait on them. Pause, skip and reset are one tap or one word away.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
import time

from ..channels.cards import Card, Urgency
from ..core.protocol import HudPanelPomodoro
from .panels import PanelSnapshot, Reader

log = logging.getLogger(__name__)

FOCUS_MS = 25 * 60_000
SHORT_BREAK_MS = 5 * 60_000
LONG_BREAK_MS = 15 * 60_000
BLOCKS_PER_SET = 4

Phase = str  # "idle" | "focus" | "break" | "long_break"


@dataclass(frozen=True, slots=True)
class PomodoroLengths:
    focus_ms: int = FOCUS_MS
    short_break_ms: int = SHORT_BREAK_MS
    long_break_ms: int = LONG_BREAK_MS

    def for_phase(self, phase: Phase) -> int:
        if phase == "focus":
            return self.focus_ms
        if phase == "long_break":
            return self.long_break_ms
        if phase == "break":
            return self.short_break_ms
        return self.focus_ms


OnChange = Callable[[], Awaitable[None]]
Notify = Callable[[Card], None]


class PomodoroService:
    """Owns the timer. Every mutation is one method; every method reports."""

    tab = "pomodoro"

    def __init__(
        self,
        *,
        lengths: PomodoroLengths = PomodoroLengths(),
        clock: Callable[[], float] = time.time,
        on_change: OnChange | None = None,
        notify: Notify | None = None,
    ) -> None:
        self._lengths = lengths
        self._clock = clock
        self._on_change = on_change
        self._notify = notify
        self._phase: Phase = "idle"
        self._running = False
        self._remaining_ms = lengths.focus_ms
        self._length_ms = lengths.focus_ms
        self._completed = 0
        self._ends_at: float | None = None
        self._timer: asyncio.TimerHandle | None = None

    # ── Observation ──────────────────────────────────────────────────────

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def running(self) -> bool:
        return self._running

    @property
    def completed(self) -> int:
        return self._completed

    def remaining_ms(self) -> int:
        if self._running and self._ends_at is not None:
            return max(0, int(round((self._ends_at - self._clock()) * 1000)))
        return self._remaining_ms

    def state(self) -> HudPanelPomodoro:
        return HudPanelPomodoro(
            phase=self._phase,
            running=self._running,
            remaining_ms=min(self.remaining_ms(), self._length_ms),
            length_ms=self._length_ms,
            completed=self._completed,
            ends_at_unix_ms=(
                int(self._ends_at * 1000)
                if self._running and self._ends_at is not None
                else None
            ),
        )

    def rows(self) -> tuple[str, ...]:
        """The control rows the expanded tab shows; a tap acts, by index."""
        if self._phase == "idle":
            return ("▶ Start a focus block",)
        if not self._running:
            return ("▶ Resume", "Reset")
        if self._phase == "focus":
            return ("Pause", "Skip to break", "Reset")
        return ("Pause", "Skip break", "Reset")

    def status_text(self) -> str:
        """What JOAT says for "how long is left"."""
        if self._phase == "idle":
            minutes = self._lengths.focus_ms // 60_000
            return f"No pomodoro running. Say start to begin a {minutes} minute focus block."
        minutes = max(1, -(-self.remaining_ms() // 60_000))
        what = {
            "focus": f"focus block {min(BLOCKS_PER_SET, self._completed + 1)} of {BLOCKS_PER_SET}",
            "break": "short break",
            "long_break": "long break",
        }.get(self._phase, self._phase)
        state = "" if self._running else "Paused. "
        return f"{state}{minutes} minute{'s' if minutes != 1 else ''} left in your {what}."

    # ── Mutation ─────────────────────────────────────────────────────────

    def start(self) -> str:
        """Idle → focus; paused → resume; running → a truthful no-op."""
        if self._phase == "idle":
            self._begin("focus")
            return f"Focus block {self._completed + 1} of {BLOCKS_PER_SET}. {self._lengths.focus_ms // 60_000} minutes."
        if not self._running:
            return self.resume()
        return "The timer is already running. " + self.status_text()

    def pause(self) -> str:
        if self._phase == "idle" or not self._running:
            return "Nothing is running."
        self._remaining_ms = self.remaining_ms()
        self._running = False
        self._ends_at = None
        self._cancel_timer()
        self._changed()
        return self.status_text()

    def resume(self) -> str:
        if self._phase == "idle":
            return self.start()
        if self._running:
            return "It is already running."
        self._running = True
        self._ends_at = self._clock() + self._remaining_ms / 1000
        self._arm_timer()
        self._changed()
        return "Resumed. " + self.status_text()

    def skip(self) -> str:
        """End the current phase now and move on."""
        if self._phase == "idle":
            return self.start()
        self._advance(skipped=True)
        return self.status_text()

    def reset(self) -> str:
        self._cancel_timer()
        self._phase = "idle"
        self._running = False
        self._completed = 0
        self._length_ms = self._lengths.focus_ms
        self._remaining_ms = self._lengths.focus_ms
        self._ends_at = None
        self._changed()
        return "Pomodoro reset."

    def control(self, index: int) -> str:
        """A tap on one of rows()."""
        rows = self.rows()
        if not 0 <= index < len(rows):
            return ""
        label = rows[index].lstrip("▶ ").lower()
        if label.startswith("start") or label.startswith("resume"):
            return self.start()
        if label == "pause":
            return self.pause()
        if label.startswith("skip"):
            return self.skip()
        if label == "reset":
            return self.reset()
        return ""

    # ── PanelProvider ────────────────────────────────────────────────────

    def snapshot(self) -> PanelSnapshot:
        return PanelSnapshot(rows=self.rows(), pomodoro=self.state())

    async def refresh(self) -> None:
        # A hold on the timer page asks for nothing new; the next push is
        # the truth already.
        return None

    async def open(self, index: int) -> Reader | None:
        self.control(index)
        return None

    # ── Internals ────────────────────────────────────────────────────────

    def _begin(self, phase: Phase) -> None:
        self._phase = phase
        self._length_ms = self._lengths.for_phase(phase)
        self._remaining_ms = self._length_ms
        self._running = True
        self._ends_at = self._clock() + self._length_ms / 1000
        self._arm_timer()
        self._changed()

    def _advance(self, *, skipped: bool = False) -> None:
        """The current phase is over: count it, start what follows."""
        self._cancel_timer()
        finished = self._phase
        if finished == "focus":
            self._completed += 1
            if self._completed >= BLOCKS_PER_SET:
                next_phase: Phase = "long_break"
            else:
                next_phase = "break"
        else:
            if finished == "long_break":
                self._completed = 0
            next_phase = "focus"
        self._begin(next_phase)
        self._announce(finished, next_phase, skipped)

    def _announce(self, finished: Phase, next_phase: Phase, skipped: bool) -> None:
        if self._notify is None:
            return
        minutes = self._length_ms // 60_000
        if next_phase == "focus":
            summary = f"Focus block {self._completed + 1} of {BLOCKS_PER_SET} — {minutes} min"
            headline = "Break over" if not skipped else "Break skipped"
        elif next_phase == "long_break":
            summary = f"Four blocks done — long break, {minutes} min"
            headline = "Focus done"
        else:
            summary = f"Break for {minutes} min"
            headline = "Focus done" if not skipped else "Focus skipped"
        self._notify(Card(
            source=headline,
            channel="Pomodoro",
            summary=summary,
            urgency=Urgency.NOTIFY,
            event_class="pomodoro.phase",
        ))

    def _arm_timer(self) -> None:
        self._cancel_timer()
        if not self._running or self._ends_at is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop (a unit test driving the state machine by hand):
            # the next phase waits for skip() or an explicit tick.
            return
        delay = max(0.0, self._ends_at - self._clock())
        self._timer = loop.call_later(delay, self._on_timer)

    def _on_timer(self) -> None:
        self._timer = None
        if not self._running:
            return
        self._advance()

    def tick(self) -> None:
        """Test seam: fire the phase end if the clock says it has passed."""
        if self._running and self._ends_at is not None and self._clock() >= self._ends_at:
            self._advance()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _changed(self) -> None:
        if self._on_change is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._publish())

    async def _publish(self) -> None:
        if self._on_change is None:
            return
        try:
            await self._on_change()
        except Exception:
            log.warning("pomodoro publish failed", exc_info=True)
