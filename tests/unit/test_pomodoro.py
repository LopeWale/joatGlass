"""The pomodoro: a state machine the Mac keeps, on an injectable clock."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from joat.channels.cards import Card
from joat.persistence.pomodoro import PomodoroCheckpoint, PomodoroStore
from joat.persistence.store import DurableStore
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


def enter_phase(service: PomodoroService, phase: str) -> None:
    service.start()
    for _ in range(8):
        if service.phase == phase:
            return
        service.skip()
    raise AssertionError("requested cycle phase was not reached")


class TestDurableRecovery:
    @pytest.mark.parametrize("phase", ["focus", "break", "long_break"])
    def test_running_restart_keeps_the_original_deadline_and_progress(self, tmp_path: Path, phase: str) -> None:
        path = tmp_path / "timer.sqlite3"
        clock = Clock()
        with DurableStore(path) as durable:
            original = PomodoroService(store=PomodoroStore(durable), clock=clock)
            enter_phase(original, phase)
            before = original.state()
        clock.now += 61
        with DurableStore(path) as durable:
            recovered = PomodoroService(store=PomodoroStore(durable), clock=clock)
            recovered.activate_restored()
            state = recovered.state()
            assert (state.phase, state.running, state.completed) == (phase, True, before.completed)
            assert state.ends_at_unix_ms == before.ends_at_unix_ms
            assert state.remaining_ms == before.remaining_ms - 61_000

    @pytest.mark.parametrize("phase", ["focus", "break", "long_break"])
    def test_paused_restart_does_not_spend_offline_time(self, tmp_path: Path, phase: str) -> None:
        path = tmp_path / "timer.sqlite3"
        clock = Clock()
        with DurableStore(path) as durable:
            original = PomodoroService(store=PomodoroStore(durable), clock=clock)
            enter_phase(original, phase)
            clock.now += 13.25
            original.pause()
            before = original.state()
        clock.now += 200_000
        with DurableStore(path) as durable:
            recovered = PomodoroService(store=PomodoroStore(durable), clock=clock)
            recovered.activate_restored()
            assert recovered.state() == before
            assert recovered.rows() == ("▶ Resume", "Reset")
            recovered.resume()
            assert recovered.state().ends_at_unix_ms == int(clock.now * 1000) + before.remaining_ms

    @pytest.mark.parametrize(("phase", "next_phase", "completed"), [
        ("focus", "break", 1), ("break", "focus", 1), ("long_break", "focus", 0),
    ])
    def test_overdue_restart_reconciles_only_one_phase_once(
        self, tmp_path: Path, phase: str, next_phase: str, completed: int,
    ) -> None:
        path = tmp_path / "timer.sqlite3"
        clock = Clock()
        with DurableStore(path) as durable:
            original = PomodoroService(store=PomodoroStore(durable), clock=clock)
            enter_phase(original, phase)
        clock.now += 7 * 86_400
        cards: list[Card] = []
        with DurableStore(path) as durable:
            recovered = PomodoroService(store=PomodoroStore(durable), clock=clock, notify=cards.append)
            assert recovered.phase == phase and cards == []
            recovered.activate_restored()
            recovered.activate_restored()
            state = recovered.state()
            assert (state.phase, state.running, state.completed) == (next_phase, False, completed)
            assert state.remaining_ms == state.length_ms and state.ends_at_unix_ms is None
            assert len(cards) == 1
            assert "while JOAT was away" in cards[0].summary
            assert "paused" in cards[0].summary.lower()
            assert "paused" in recovered.status_text().lower()
        with DurableStore(path) as durable:
            second = PomodoroService(store=PomodoroStore(durable), clock=clock, notify=cards.append)
            second.activate_restored()
            assert second.state() == state
            assert len(cards) == 1, "the same offline interval must not manufacture further phases"

    def test_reset_is_durable_and_active_phase_length_survives_new_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "timer.sqlite3"
        clock = Clock()
        with DurableStore(path) as durable:
            original = PomodoroService(store=PomodoroStore(durable), clock=clock,
                lengths=PomodoroLengths(focus_ms=120_000))
            original.start()
        clock.now += 12
        with DurableStore(path) as durable:
            recovered = PomodoroService(store=PomodoroStore(durable), clock=clock)
            recovered.activate_restored()
            assert recovered.state().length_ms == 120_000
            assert recovered.remaining_ms() == 108_000
            recovered.reset()
        with DurableStore(path) as durable:
            recovered = PomodoroService(store=PomodoroStore(durable), clock=clock)
            recovered.activate_restored()
            assert recovered.phase == "idle" and recovered.completed == 0
            assert recovered.state().length_ms == 25 * 60_000

    async def test_constructor_waits_for_explicit_activation_before_callbacks(self, tmp_path: Path) -> None:
        clock = Clock()
        cards: list[Card] = []
        with DurableStore(tmp_path / "timer.sqlite3") as durable:
            store = PomodoroStore(durable)
            store.save(PomodoroCheckpoint("focus", True, 50, 50, 0, 999_000, 998_950))
            recovered = PomodoroService(store=store, clock=clock, notify=cards.append)
            try:
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert recovered.phase == "focus" and cards == []
                recovered.activate_restored()
                assert recovered.phase == "break" and not recovered.running
                assert len(cards) == 1
            finally:
                await recovered.aclose()


class RejectingPomodoroStore(PomodoroStore):
    reject = False

    def save(self, checkpoint: PomodoroCheckpoint) -> None:
        if self.reject:
            raise OSError("fixture storage unavailable")
        super().save(checkpoint)


class TestDurableMutationBoundary:
    @pytest.mark.parametrize("operation", ["start", "pause", "resume", "skip", "reset", "tick"])
    def test_failed_save_preserves_memory_disk_and_phase_notification(
        self, tmp_path: Path, operation: str,
    ) -> None:
        clock = Clock()
        cards: list[Card] = []
        with DurableStore(tmp_path / "timer.sqlite3") as durable:
            store = RejectingPomodoroStore(durable)
            service = PomodoroService(store=store, clock=clock, notify=cards.append)
            if operation != "start":
                service.start()
            if operation == "resume":
                service.pause()
            if operation == "tick":
                clock.now += 1500
            before, checkpoint = service.state(), store.load()
            store.reject = True
            reply = getattr(service, operation)()
            if operation != "tick":
                assert "failed" in reply.lower()
            assert service.state() == before
            assert store.load() == checkpoint
            assert "failed" in service.status_text().lower()
            assert service.snapshot().headline == "Timer change failed"
            assert len(cards) == 1 and cards[0].event_class == "pomodoro.error"

    def test_failed_recovery_keeps_exact_checkpoint_for_explicit_retry(self, tmp_path: Path) -> None:
        clock = Clock()
        cards: list[Card] = []
        with DurableStore(tmp_path / "timer.sqlite3") as durable:
            store = RejectingPomodoroStore(durable)
            store.save(PomodoroCheckpoint("focus", True, 50, 50, 0, 999_000, 998_950))
            recovered = PomodoroService(store=store, clock=clock, notify=cards.append)
            before = store.load()
            store.reject = True
            recovered.activate_restored()
            recovered.activate_restored()
            assert recovered.phase == "focus" and recovered.completed == 0
            assert store.load() == before
            assert len(cards) == 1 and cards[0].event_class == "pomodoro.error"
            store.reject = False
            recovered.activate_restored()
            recovered.activate_restored()
            assert recovered.phase == "break" and recovered.completed == 1 and not recovered.running
            assert len(cards) == 2 and cards[-1].source == "Timer recovered"
            assert recovered.reset() == "Pomodoro reset."
            assert recovered.phase == "idle" and recovered.completed == 0 and len(cards) == 2

    def test_unreadable_checkpoint_is_not_overwritten_until_explicit_reset(self, tmp_path: Path) -> None:
        class UnreadableStore(PomodoroStore):
            def load(self) -> PomodoroCheckpoint | None:
                raise RuntimeError("fixture unreadable checkpoint")

        cards: list[Card] = []
        clock = Clock()
        with DurableStore(tmp_path / "timer.sqlite3") as durable:
            real_store = PomodoroStore(durable)
            saved = PomodoroCheckpoint("focus", False, 7000, 9000, 2, None, 100_000)
            real_store.save(saved)
            service = PomodoroService(store=UnreadableStore(durable), clock=clock, notify=cards.append)
            assert cards == [], "constructor must not call the not-yet-composed notifier"
            service.activate_restored()
            assert service.recovery_unavailable
            assert service.snapshot().headline == "Timer unavailable"
            assert service.snapshot().empty == "Timer recovery unavailable. Reset to start a new timer."
            assert service.rows() == ("Reset",)
            for operation in (service.start, service.pause, service.resume, service.skip):
                assert "unavailable" in operation()
                assert real_store.load() == saved
            assert len(cards) == 1 and cards[0].event_class == "pomodoro.error"
            assert service.control(0) == "Pomodoro reset."
            reset_state = real_store.load()
            assert reset_state is not None and reset_state.phase == "idle"
            assert not service.recovery_unavailable
            assert service.snapshot().headline is None and "unavailable" not in service.status_text()

    def test_failed_explicit_reset_keeps_recovery_unavailable(self, tmp_path: Path) -> None:
        class UnreadableStore(RejectingPomodoroStore):
            def load(self) -> PomodoroCheckpoint | None:
                raise RuntimeError("fixture unreadable checkpoint")

        with DurableStore(tmp_path / "timer.sqlite3") as durable:
            store = UnreadableStore(durable)
            store.reject = True
            service = PomodoroService(store=store)
            service.activate_restored()
            assert "unavailable" in service.reset()
            assert service.rows() == ("Reset",)
            assert service.snapshot().headline == "Timer unavailable"

    async def test_timer_rechecks_wall_deadline_and_close_retires_owned_work(self, tmp_path: Path) -> None:
        clock = Clock()
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def publish() -> None:
            entered.set()
            try:
                await release.wait()
            finally:
                finished.set()

        with DurableStore(tmp_path / "timer.sqlite3") as durable:
            store = PomodoroStore(durable)
            cards: list[Card] = []
            service = PomodoroService(store=store, clock=clock, on_change=publish, notify=cards.append)
            service.start()
            before = store.load()
            try:
                await asyncio.wait_for(entered.wait(), 0.2)
                clock.now -= 30
                service._on_timer()  # an elapsed monotonic callback is not proof of the wall deadline
                assert service.phase == "focus" and cards == []
                await asyncio.wait_for(service.aclose(), 0.2)
                assert finished.is_set()
                assert store.load() == before, "shutdown must preserve the original running deadline"
                clock.now += 3000
                service._on_timer()
                service.tick()
                service.activate_restored()
                assert service.phase == "focus" and cards == []
                with pytest.raises(RuntimeError, match="closed"):
                    service.reset()
            finally:
                release.set()
                await service.aclose()
