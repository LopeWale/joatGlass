"""The pomodoro: a state machine the Mac keeps, on an injectable clock."""

from __future__ import annotations

import asyncio

from joat.channels.cards import Card
from joat.services.pomodoro import PomodoroLengths, PomodoroService


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def make(lengths: PomodoroLengths = PomodoroLengths()) -> tuple[PomodoroService, Clock, list[Card], list[int]]:
    clock = Clock()
    cards: list[Card] = []
    changes: list[int] = []

    async def on_change() -> None:
        changes.append(1)

    service = PomodoroService(
        lengths=lengths, clock=clock, on_change=on_change, notify=cards.append,
    )
    return service, clock, cards, changes


class TestStateMachine:
    def test_starts_idle_with_one_start_row(self) -> None:
        service, _, _, _ = make()
        assert service.phase == "idle"
        assert service.rows() == ("▶ Start a focus block",)
        state = service.state()
        assert state.phase == "idle" and state.running is False
        assert state.remaining_ms == state.length_ms == 25 * 60_000
        assert "No pomodoro running" in service.status_text()

    def test_start_runs_a_focus_block_with_an_end_instant(self) -> None:
        service, clock, _, _ = make()
        reply = service.start()
        assert reply == "Focus block 1 of 4. 25 minutes."
        state = service.state()
        assert state.phase == "focus" and state.running is True
        assert state.ends_at_unix_ms == int((clock.now + 1500) * 1000)
        clock.now += 60
        assert service.remaining_ms() == 24 * 60_000
        assert service.status_text() == "24 minutes left in your focus block 1 of 4."
        assert service.rows() == ("Pause", "Skip to break", "Reset")

    def test_pause_freezes_the_remaining_time_and_resume_continues(self) -> None:
        service, clock, _, _ = make()
        service.start()
        clock.now += 300
        assert service.pause().startswith("Paused. 20 minutes")
        clock.now += 1_000
        assert service.remaining_ms() == 20 * 60_000
        assert service.state().ends_at_unix_ms is None
        assert service.rows() == ("▶ Resume", "Reset")
        assert service.resume().startswith("Resumed.")
        assert service.state().ends_at_unix_ms == int((clock.now + 1200) * 1000)
        assert service.start() == "The timer is already running. 20 minutes left in your focus block 1 of 4."

    def test_a_finished_block_starts_its_break_and_drops_a_card(self) -> None:
        service, clock, cards, _ = make()
        service.start()
        clock.now += 1500
        service.tick()
        assert service.phase == "break" and service.running
        assert service.completed == 1
        assert service.state().length_ms == 5 * 60_000
        assert cards[-1].channel == "Pomodoro"
        assert cards[-1].source == "Focus done"
        assert cards[-1].summary == "Break for 5 min"
        clock.now += 300
        service.tick()
        assert service.phase == "focus" and service.completed == 1
        assert cards[-1].source == "Break over"
        assert cards[-1].summary == "Focus block 2 of 4 — 25 min"

    def test_four_blocks_earn_a_long_break_then_the_count_resets(self) -> None:
        service, clock, cards, _ = make()
        service.start()
        for _ in range(3):
            clock.now += 1500
            service.tick()  # focus → break
            clock.now += 300
            service.tick()  # break → focus
        clock.now += 1500
        service.tick()
        assert service.phase == "long_break"
        assert service.completed == 4
        assert service.state().length_ms == 15 * 60_000
        assert cards[-1].summary == "Four blocks done — long break, 15 min"
        clock.now += 900
        service.tick()
        assert service.phase == "focus" and service.completed == 0

    def test_skip_ends_the_phase_now_and_says_so(self) -> None:
        service, _, cards, _ = make()
        service.start()
        assert service.skip().endswith("left in your short break.")
        assert cards[-1].source == "Focus skipped"
        assert service.rows() == ("Pause", "Skip break", "Reset")

    def test_reset_returns_to_idle_and_forgets_progress(self) -> None:
        service, clock, _, _ = make()
        service.start()
        clock.now += 1500
        service.tick()
        assert service.reset() == "Pomodoro reset."
        assert service.phase == "idle" and service.completed == 0
        assert service.pause() == "Nothing is running."

    def test_a_row_tap_acts_by_label(self) -> None:
        service, _, _, _ = make()
        assert service.control(0).startswith("Focus block 1")
        assert service.control(0).startswith("Paused")  # "Pause"
        assert service.control(0).startswith("Resumed")  # "▶ Resume"
        assert service.control(2) == "Pomodoro reset."  # "Reset"
        assert service.control(9) == ""


class TestAsyncIntegration:
    async def test_the_timer_fires_on_the_loop_and_publishes_each_change(self) -> None:
        clock = Clock()
        cards: list[Card] = []
        published: list[str] = []

        async def on_change() -> None:
            published.append(clock_service.phase)

        clock_service = PomodoroService(
            lengths=PomodoroLengths(focus_ms=50, short_break_ms=50, long_break_ms=50),
            on_change=on_change, notify=cards.append,
        )
        clock_service.start()
        await asyncio.sleep(0.08)
        assert clock_service.phase == "break"
        assert cards[-1].source == "Focus done"
        await asyncio.sleep(0)
        assert published[:2] == ["focus", "break"]
        clock_service.reset()
        await asyncio.sleep(0.08)
        assert clock_service.phase == "idle"  # a cancelled timer never fires

    async def test_open_applies_the_control_and_returns_no_reader(self) -> None:
        service, _, _, _ = make()
        assert await service.open(0) is None
        assert service.phase == "focus"
        assert service.snapshot().pomodoro is not None
        assert service.snapshot().rows == ("Pause", "Skip to break", "Reset")
        await service.refresh()
