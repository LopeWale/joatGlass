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
from ..persistence.pomodoro import PomodoroCheckpoint, PomodoroStore
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
_LOAD_FAILURE = "Timer recovery unavailable. Reset to start a new timer."
_SAVE_FAILURE = "Timer change failed. Previous state kept. Retry or reset."


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
        store: PomodoroStore | None = None,
    ) -> None:
        self._lengths = lengths
        self._clock = clock
        self._on_change = on_change
        self._notify = notify
        self._store = store
        self._phase: Phase = "idle"
        self._running = False
        self._remaining_ms = lengths.focus_ms
        self._length_ms = lengths.focus_ms
        self._completed = 0
        self._ends_at: float | None = None
        self._ends_at_unix_ms: int | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._publications: set[asyncio.Task[None]] = set()
        self._closed = False
        self._restored: PomodoroCheckpoint | None = None
        self._activated = False
        self._load_failed = False
        self._error: str | None = None
        self._error_announced = False
        self._recovery_notice: str | None = None
        if store is not None:
            try:
                self._restored = store.load()
            except Exception:
                # Do not reset an unreadable checkpoint or stop unrelated
                # engine features. Only an explicit, durable Reset replaces it.
                self._load_failed = True
                self._error = _LOAD_FAILURE
            if self._restored is not None:
                self._adopt(self._restored)

    def activate_restored(self) -> None:
        """Activate after callbacks are composed; construction never schedules.

        Only the recorded deadline can expire while the engine was away.
        Its successor is persisted paused, so another restart cannot invent
        an offline sequence of completed focus blocks.
        """
        if self._closed or self._activated:
            return
        self._activated = True
        if self._load_failed:
            self._report_failure(_LOAD_FAILURE)
            return
        restored = self._restored
        if restored is None or not restored.running:
            return
        if self._ends_at is not None and self._clock() < self._ends_at:
            self._arm_timer()
            return
        finished = self._phase
        next_phase, completed = self._next_phase()
        checkpoint = self._phase_checkpoint(next_phase, completed=completed, running=False)
        if not self._commit(checkpoint):
            self._activated = False
            return
        finished_label = {"focus": "Focus", "break": "Break", "long_break": "Long break"}[finished]
        next_label = {"focus": "Focus", "break": "Short break", "long_break": "Long break"}[next_phase]
        self._recovery_notice = (
            f"{finished_label} deadline passed while JOAT was away. {next_label} is paused."
        )
        if self._notify is not None:
            self._notify(Card(source="Timer recovered", channel="Pomodoro",
                summary=self._recovery_notice, urgency=Urgency.NOTIFY, event_class="pomodoro.phase"))

    async def aclose(self) -> None:
        """Stop this process's callbacks without pausing or resetting storage."""
        self._closed = True
        self._cancel_timer()
        current = asyncio.current_task()
        pending = [task for task in self._publications if task is not current]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

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

    @property
    def recovery_unavailable(self) -> bool:
        return self._load_failed

    def remaining_ms(self) -> int:
        if self._running and self._ends_at is not None:
            return min(self._length_ms, max(0, int(round((self._ends_at - self._clock()) * 1000))))
        return self._remaining_ms

    def state(self) -> HudPanelPomodoro:
        return HudPanelPomodoro(
            phase=self._phase,
            running=self._running,
            remaining_ms=min(self.remaining_ms(), self._length_ms),
            length_ms=self._length_ms,
            completed=self._completed,
            ends_at_unix_ms=self._ends_at_unix_ms if self._running else None,
        )

    def rows(self) -> tuple[str, ...]:
        """The control rows the expanded tab shows; a tap acts, by index."""
        if self._load_failed:
            return ("Reset",)
        if self._phase == "idle":
            return ("▶ Start a focus block",)
        if not self._running:
            return ("▶ Resume", "Reset")
        if self._phase == "focus":
            return ("Pause", "Skip to break", "Reset")
        return ("Pause", "Skip break", "Reset")

    def status_text(self) -> str:
        """What JOAT says for "how long is left"."""
        if self._error is not None:
            return self._error
        prefix = f"{self._recovery_notice} " if self._recovery_notice else ""
        if self._phase == "idle":
            minutes = self._lengths.focus_ms // 60_000
            return f"No pomodoro running. Say start to begin a {minutes} minute focus block."
        minutes = max(0, -(-self.remaining_ms() // 60_000))
        what = {
            "focus": f"focus block {min(BLOCKS_PER_SET, self._completed + 1)} of {BLOCKS_PER_SET}",
            "break": "short break",
            "long_break": "long break",
        }.get(self._phase, self._phase)
        state = "" if self._running else "Paused. "
        return f"{prefix}{state}{minutes} minute{'s' if minutes != 1 else ''} left in your {what}."

    # ── Mutation ─────────────────────────────────────────────────────────

    def start(self) -> str:
        """Idle → focus; paused → resume; running → a truthful no-op."""
        if not self._can_control():
            return self.status_text()
        if self._phase == "idle":
            if not self._begin("focus"):
                return self.status_text()
            return f"Focus block {self._completed + 1} of {BLOCKS_PER_SET}. {self._lengths.focus_ms // 60_000} minutes."
        if not self._running:
            return self.resume()
        return "The timer is already running. " + self.status_text()

    def pause(self) -> str:
        if not self._can_control():
            return self.status_text()
        if self._phase == "idle" or not self._running:
            return self._error or "Nothing is running."
        self._commit(self._checkpoint(running=False, remaining_ms=self.remaining_ms(), ends_at=None))
        return self.status_text()

    def resume(self) -> str:
        if not self._can_control():
            return self.status_text()
        if self._phase == "idle":
            return self.start()
        if self._running:
            return self._error or "It is already running."
        if not self._commit(self._checkpoint(running=True, remaining_ms=self._remaining_ms,
                ends_at=self._now_ms() + self._remaining_ms)):
            return self.status_text()
        return "Resumed. " + self.status_text()

    def skip(self) -> str:
        """End the current phase now and move on."""
        if not self._can_control():
            return self.status_text()
        if self._phase == "idle":
            return self.start()
        self._advance(skipped=True)
        return self.status_text()

    def reset(self) -> str:
        self._ensure_open()
        if not self._commit(self._phase_checkpoint("idle", completed=0, running=False)):
            return self.status_text()
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
        headline = ("Timer unavailable" if self._load_failed else "Timer change failed") if self._error else None
        return PanelSnapshot(rows=self.rows(), headline=headline, empty=self._error, pomodoro=self.state())

    async def refresh(self) -> None:
        # A hold on the timer page asks for nothing new; the next push is
        # the truth already.
        return None

    async def open(self, index: int) -> Reader | None:
        self.control(index)
        return None

    # ── Internals ────────────────────────────────────────────────────────

    def _now_ms(self) -> int:
        return int(round(self._clock() * 1000))

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Pomodoro service is closed")

    def _can_control(self) -> bool:
        self._ensure_open()
        if self._load_failed:
            self._report_failure(_LOAD_FAILURE)
            return False
        return True

    def _checkpoint(self, *, running: bool, remaining_ms: int, ends_at: int | None) -> PomodoroCheckpoint:
        return PomodoroCheckpoint(self._phase, running, remaining_ms, self._length_ms,
            self._completed, ends_at, self._now_ms())

    def _phase_checkpoint(self, phase: Phase, *, completed: int, running: bool) -> PomodoroCheckpoint:
        length = self._lengths.for_phase(phase)
        now = self._now_ms()
        return PomodoroCheckpoint(phase, running, length, length, completed,
            now + length if running else None, now)

    def _adopt(self, checkpoint: PomodoroCheckpoint) -> None:
        self._phase = checkpoint.phase
        self._running = checkpoint.running
        self._remaining_ms = checkpoint.remaining_ms
        self._length_ms = checkpoint.length_ms
        self._completed = checkpoint.completed
        self._ends_at_unix_ms = checkpoint.ends_at_unix_ms
        self._ends_at = checkpoint.ends_at_unix_ms / 1000 if checkpoint.ends_at_unix_ms is not None else None

    def _commit(self, checkpoint: PomodoroCheckpoint) -> bool:
        self._ensure_open()
        if self._store is not None:
            try:
                self._store.save(checkpoint)
            except Exception:
                self._report_failure(_LOAD_FAILURE if self._load_failed else _SAVE_FAILURE)
                return False
        # No timer, logical state, success receipt or phase card precedes the
        # transaction. Failed writes leave the previous checkpoint untouched.
        self._cancel_timer()
        self._adopt(checkpoint)
        self._restored = None
        self._activated = True
        self._load_failed = False
        self._error = None
        self._error_announced = False
        self._recovery_notice = None
        self._arm_timer()
        self._changed()
        return True

    def _report_failure(self, message: str) -> None:
        self._error = message
        log.warning("pomodoro durable state unavailable")
        if not self._error_announced and self._notify is not None:
            self._error_announced = True
            self._notify(Card(source="Timer unavailable" if self._load_failed else "Timer change failed",
                channel="Pomodoro", summary=message, urgency=Urgency.NOTIFY, event_class="pomodoro.error"))
        self._changed()

    def _begin(self, phase: Phase) -> bool:
        return self._commit(self._phase_checkpoint(phase, completed=self._completed, running=True))

    def _next_phase(self) -> tuple[Phase, int]:
        if self._phase == "focus":
            completed = self._completed + 1
            return ("long_break" if completed >= BLOCKS_PER_SET else "break"), completed
        return "focus", 0 if self._phase == "long_break" else self._completed

    def _advance(self, *, skipped: bool = False) -> None:
        """The current phase is over: count it, start what follows."""
        finished = self._phase
        next_phase, completed = self._next_phase()
        if self._commit(self._phase_checkpoint(next_phase, completed=completed, running=True)):
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
        if self._closed or not self._running or self._ends_at is None:
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
        self._cancel_timer()
        if self._closed or not self._running or self._ends_at is None:
            return
        if self._clock() < self._ends_at:
            self._arm_timer()
            return
        self._advance()

    def tick(self) -> None:
        """Test seam: fire the phase end if the clock says it has passed."""
        if not self._closed and self._running and self._ends_at is not None and self._clock() >= self._ends_at:
            self._advance()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _changed(self) -> None:
        if self._closed or self._on_change is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._publish())
        self._publications.add(task)
        task.add_done_callback(self._publications.discard)

    async def _publish(self) -> None:
        if self._on_change is None:
            return
        try:
            await self._on_change()
        except Exception:
            log.warning("pomodoro publish failed", exc_info=True)
