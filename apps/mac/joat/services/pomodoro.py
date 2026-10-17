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

The page opened by hand is a control surface: while idle it shows Start
and the three lengths, and a tap on a length steps it to the next preset.
The lengths persist with the checkpoint, so a restart keeps them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from functools import partial
import logging
import time

from ..channels.cards import Card, Urgency
from ..core.protocol import HudPanelPomodoro
from ..persistence.pomodoro import (
    DEFAULT_BLOCKS_PER_SET,
    DEFAULT_FOCUS_MS,
    DEFAULT_LONG_BREAK_MS,
    DEFAULT_SHORT_BREAK_MS,
    MAX_BLOCKS_PER_SET,
    PomodoroCheckpoint,
    PomodoroStore,
)
from .panels import PanelSnapshot, Reader

log = logging.getLogger(__name__)

FOCUS_MS = DEFAULT_FOCUS_MS
SHORT_BREAK_MS = DEFAULT_SHORT_BREAK_MS
LONG_BREAK_MS = DEFAULT_LONG_BREAK_MS
BLOCKS_PER_SET = DEFAULT_BLOCKS_PER_SET
#: The longest a spoken length may be: the wire and the store both stop at a day.
MAX_LENGTH_MIN = 24 * 60

Phase = str  # "idle" | "focus" | "break" | "long_break"
LengthKind = str  # "focus" | "short_break" | "long_break"

# A tap on an idle length row steps to the next preset and wraps.
LENGTH_PRESETS_MIN: dict[LengthKind, tuple[int, ...]] = {
    "focus": (15, 20, 25, 30, 45, 50, 60),
    "short_break": (3, 5, 10, 15),
    "long_break": (10, 15, 20, 30),
}
SESSION_PRESETS = (2, 3, 4, 5, 6, 8)
#: The countdown the idle page starts with one tap; any other length is spoken.
QUICK_TIMER_MIN = 10
_LENGTH_ROW_LABELS: dict[LengthKind, str] = {
    "focus": "Focus", "short_break": "Short break", "long_break": "Long break",
}
_LENGTH_RECEIPT_NOUNS: dict[LengthKind, str] = {
    "focus": "Focus blocks", "short_break": "Short breaks", "long_break": "Long breaks",
}


def _minutes(ms: int) -> int:
    return ms // 60_000


def _minutes_phrase(ms: int) -> str:
    minutes = _minutes(ms)
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


@dataclass(frozen=True, slots=True)
class PomodoroLengths:
    focus_ms: int = FOCUS_MS
    short_break_ms: int = SHORT_BREAK_MS
    long_break_ms: int = LONG_BREAK_MS
    blocks_per_set: int = BLOCKS_PER_SET

    def for_phase(self, phase: Phase) -> int:
        if phase == "focus":
            return self.focus_ms
        if phase == "long_break":
            return self.long_break_ms
        if phase == "break":
            return self.short_break_ms
        return self.focus_ms

    def of(self, kind: LengthKind) -> int:
        if kind == "focus":
            return self.focus_ms
        if kind == "short_break":
            return self.short_break_ms
        if kind == "long_break":
            return self.long_break_ms
        raise ValueError(f"unknown pomodoro length {kind!r}")

    def with_next_preset(self, kind: LengthKind) -> PomodoroLengths:
        """The next preset above the current length, wrapping to the first."""
        presets = LENGTH_PRESETS_MIN.get(kind)
        if presets is None:
            raise ValueError(f"unknown pomodoro length {kind!r}")
        current = self.of(kind)
        minutes = next((preset for preset in presets if preset * 60_000 > current), presets[0])
        return replace(self, **{f"{kind}_ms": minutes * 60_000})

    def with_next_sessions(self) -> PomodoroLengths:
        """The next session-count preset above the current one, wrapping."""
        count = next((n for n in SESSION_PRESETS if n > self.blocks_per_set), SESSION_PRESETS[0])
        return replace(self, blocks_per_set=count)

    def with_minutes(self, kind: LengthKind, minutes: int) -> PomodoroLengths:
        """Any whole-minute length, 1 minute to a day."""
        if kind not in LENGTH_PRESETS_MIN:
            raise ValueError(f"unknown pomodoro length {kind!r}")
        if not 1 <= minutes <= MAX_LENGTH_MIN:
            raise ValueError(f"a length is 1 to {MAX_LENGTH_MIN} minutes")
        return replace(self, **{f"{kind}_ms": minutes * 60_000})

    @classmethod
    def from_checkpoint(cls, checkpoint: PomodoroCheckpoint) -> PomodoroLengths:
        return cls(checkpoint.focus_ms, checkpoint.short_break_ms, checkpoint.long_break_ms,
            checkpoint.blocks_per_set)


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
        if finished == "timer":
            self._recovery_notice = "Your timer finished while JOAT was away."
        else:
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
            blocks_per_set=self._lengths.blocks_per_set,
        )

    @property
    def lengths(self) -> PomodoroLengths:
        return self._lengths

    def _controls(self) -> tuple[tuple[str, Callable[[], str]], ...]:
        """The expanded tab's rows, each paired with what its tap does.

        The page is a control surface: every row is something to tap, and
        the idle page carries the cycle's lengths so they can be set by hand.
        """
        if self._load_failed:
            return (("Reset", self.reset),)
        if self._phase == "idle":
            return (
                ("▶ Start a focus block", self.start),
                *(
                    (f"{_LENGTH_ROW_LABELS[kind]}: {_minutes(self._lengths.of(kind))} min",
                     partial(self.cycle_length, kind))
                    for kind in ("focus", "short_break", "long_break")
                ),
                (f"Sessions: {self._lengths.blocks_per_set}", self.cycle_sessions),
                (f"▶ {QUICK_TIMER_MIN} min timer", partial(self.start_timer, QUICK_TIMER_MIN)),
            )
        if not self._running:
            return (("▶ Resume", self.resume), ("Reset", self.reset))
        if self._phase == "timer":
            return (("Pause", self.pause), ("Cancel timer", self.reset))
        skip = "Skip to break" if self._phase == "focus" else "Skip break"
        return (("Pause", self.pause), (skip, self.skip), ("Reset", self.reset))

    def rows(self) -> tuple[str, ...]:
        """The control rows the expanded tab shows; a tap acts, by index."""
        return tuple(label for label, _action in self._controls())

    def status_text(self) -> str:
        """What JOAT says for "how long is left"."""
        if self._error is not None:
            return self._error
        prefix = f"{self._recovery_notice} " if self._recovery_notice else ""
        blocks = self._lengths.blocks_per_set
        if self._phase == "idle":
            return (f"No pomodoro running. A focus block is {_minutes_phrase(self._lengths.focus_ms)}, "
                f"{blocks} to a set.")
        minutes = max(0, -(-self.remaining_ms() // 60_000))
        what = {
            "focus": f"focus block {min(blocks, self._completed + 1)} of {blocks}",
            "break": "short break",
            "long_break": "long break",
            "timer": "timer",
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
            return (f"Focus block {self._completed + 1} of {self._lengths.blocks_per_set}. "
                f"{_minutes_phrase(self._lengths.focus_ms)}.")
        if not self._running:
            return self.resume()
        return "The timer is already running. " + self.status_text()

    def start_timer(self, minutes: int) -> str:
        """A plain one-shot countdown: runs once, says so, and goes idle.

        One timer at a time: it shares the Pomodoro's page, chip and
        checkpoint, so it will not start over a pomodoro or another timer.
        """
        if not self._can_control():
            return self.status_text()
        if not 1 <= minutes <= MAX_LENGTH_MIN:
            return f"A timer is 1 to {MAX_LENGTH_MIN} minutes."
        if self._phase != "idle":
            what = "A timer" if self._phase == "timer" else "A pomodoro"
            return f"{what} is already under way. Say reset first. " + self.status_text()
        length = minutes * 60_000
        if not self._commit(self._phase_checkpoint("timer", completed=0, running=True, length_ms=length)):
            return self.status_text()
        return f"Timer set for {_minutes_phrase(length)}."

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

    def cycle_length(self, kind: LengthKind) -> str:
        """Step one length to its next preset, wrapping, and persist it.

        Idle, the new focus length is also the page's countdown. During a
        phase the block under way keeps the length it started with; the new
        one applies from the next phase of its kind.
        """
        if not self._can_control():
            return self.status_text()
        lengths = self._lengths.with_next_preset(kind)
        if not self._apply_lengths(lengths):
            return self.status_text()
        return f"{_LENGTH_RECEIPT_NOUNS[kind]}: {_minutes_phrase(lengths.of(kind))}."

    def set_length(self, kind: LengthKind, minutes: int) -> str:
        """Set one length to any whole number of minutes (a spoken value)."""
        if not self._can_control():
            return self.status_text()
        if not 1 <= minutes <= MAX_LENGTH_MIN:
            return f"A length is 1 to {MAX_LENGTH_MIN} minutes."
        lengths = self._lengths.with_minutes(kind, minutes)
        if not self._apply_lengths(lengths):
            return self.status_text()
        return f"{_LENGTH_RECEIPT_NOUNS[kind]}: {_minutes_phrase(lengths.of(kind))}."

    def set_sessions(self, count: int) -> str:
        """Focus blocks before the long break. Applies to the next set."""
        if not self._can_control():
            return self.status_text()
        if not 1 <= count <= MAX_BLOCKS_PER_SET:
            return f"A set is 1 to {MAX_BLOCKS_PER_SET} sessions."
        if not self._sessions_fit(count):
            return (f"You are {self._completed} blocks into this set. "
                f"Say reset, then set {count} sessions.")
        if not self._apply_lengths(replace(self._lengths, blocks_per_set=count)):
            return self.status_text()
        return f"{count} session{'s' if count != 1 else ''} to a set."

    def cycle_sessions(self) -> str:
        """A tap on the Sessions row: the next preset count, wrapping."""
        if not self._can_control():
            return self.status_text()
        return self.set_sessions(self._lengths.with_next_sessions().blocks_per_set)

    def control(self, index: int) -> str:
        """A tap on one of rows(): the action paired with that row."""
        controls = self._controls()
        if not 0 <= index < len(controls):
            return ""
        return controls[index][1]()

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

    def _checkpoint(self, *, running: bool, remaining_ms: int, ends_at: int | None,
                    lengths: PomodoroLengths | None = None) -> PomodoroCheckpoint:
        lengths = lengths or self._lengths
        return PomodoroCheckpoint(self._phase, running, remaining_ms, self._length_ms,
            self._completed, ends_at, self._now_ms(),
            lengths.focus_ms, lengths.short_break_ms, lengths.long_break_ms, lengths.blocks_per_set)

    def _phase_checkpoint(self, phase: Phase, *, completed: int, running: bool,
                          lengths: PomodoroLengths | None = None,
                          length_ms: int | None = None) -> PomodoroCheckpoint:
        lengths = lengths or self._lengths
        length = length_ms if length_ms is not None else lengths.for_phase(phase)
        now = self._now_ms()
        return PomodoroCheckpoint(phase, running, length, length, completed,
            now + length if running else None, now,
            lengths.focus_ms, lengths.short_break_ms, lengths.long_break_ms, lengths.blocks_per_set)

    def _apply_lengths(self, lengths: PomodoroLengths) -> bool:
        """Persist new lengths. Idle, the page's countdown follows the focus
        length; during a phase the block under way keeps the length it
        started with and the new one applies from the next of its kind."""
        if self._phase == "idle":
            checkpoint = self._phase_checkpoint("idle", completed=0, running=False, lengths=lengths)
        else:
            checkpoint = self._checkpoint(running=self._running, remaining_ms=self.remaining_ms(),
                ends_at=self._ends_at_unix_ms if self._running else None, lengths=lengths)
        return self._commit(checkpoint)

    def _sessions_fit(self, count: int) -> bool:
        """Can a set of `count` take the phase under way without a rewrite?"""
        if self._phase in ("idle", "timer"):
            return True
        if self._phase == "focus":
            return self._completed < count
        if self._phase == "break":
            return 1 <= self._completed < count
        return self._completed == count

    def _adopt(self, checkpoint: PomodoroCheckpoint) -> None:
        # The checkpoint is the one truth, lengths included: a restart keeps
        # what the wearer set, not this build's defaults.
        self._lengths = PomodoroLengths.from_checkpoint(checkpoint)
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
        if self._phase == "timer":
            return "idle", 0
        if self._phase == "focus":
            completed = self._completed + 1
            return ("long_break" if completed >= self._lengths.blocks_per_set else "break"), completed
        return "focus", 0 if self._phase == "long_break" else self._completed

    def _advance(self, *, skipped: bool = False) -> None:
        """The current phase is over: count it, start what follows."""
        finished = self._phase
        finished_ms = self._length_ms
        next_phase, completed = self._next_phase()
        if self._commit(self._phase_checkpoint(next_phase, completed=completed,
                running=next_phase != "idle")):
            if finished == "timer":
                self._announce_timer(finished_ms, skipped)
            else:
                self._announce(finished, next_phase, skipped)

    def _announce_timer(self, finished_ms: int, skipped: bool) -> None:
        if self._notify is None:
            return
        self._notify(Card(
            source="Timer ended" if skipped else "Timer done",
            channel="Pomodoro",
            summary=f"{_minutes_phrase(finished_ms)} timer " + ("ended early" if skipped else "finished"),
            urgency=Urgency.NOTIFY,
            event_class="pomodoro.phase",
        ))

    def _announce(self, finished: Phase, next_phase: Phase, skipped: bool) -> None:
        if self._notify is None:
            return
        minutes = self._length_ms // 60_000
        if next_phase == "focus":
            summary = f"Focus block {self._completed + 1} of {self._lengths.blocks_per_set} — {minutes} min"
            headline = "Break over" if not skipped else "Break skipped"
        elif next_phase == "long_break":
            blocks = self._lengths.blocks_per_set
            summary = f"{blocks} block{'s' if blocks != 1 else ''} done — long break, {minutes} min"
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
