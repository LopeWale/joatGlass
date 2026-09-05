"""Explicit Terminal opening across the real capability and capture owners.

This is a composition contract, not a recording of the original incident.
Only the adapter's initializer and transport are synthetic. No Gateway,
microphone, model, contact lookup, or Messages send is used.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from joat.channels.cards import CardLog
from joat.core.protocol import AgentTerminalHudView, BrainstormHudView, HudList, InputEvent, PromptAck, PromptPresent
from joat.services.agent_terminal import AgentTerminalCommands, AgentTerminalRoster, TerminalFrame
from joat.services.capabilities import CapabilityRouter
from joat.services.hud import HudWriter
from joat.services.message_reply import MessageReplyController
from joat.services.prompted_speech import PromptedSpeechRuntime
from joat.services.session import SessionManager
from joat.services.sinks import HudSink
from joat.services.status import StatusPublisher
from joat.services.terminal_opening import TerminalOpeningController


async def until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


class InitializingAgent:
    """Expose a capture context only after this exact initialization finishes."""

    def __init__(self) -> None:
        self.capture_context_generation: int | None = None
        self.initialize_calls = 0
        self.initialize_started = asyncio.Event()
        self.initialize_release = asyncio.Event()
        self.initialize_release.set()
        self.initialize_tasks: list[asyncio.Task[Any]] = []
        self.fail = False
        self.scoped: list[tuple[str, int]] = []
        self.unscoped: list[str] = []

    async def initialize(self) -> None:
        self.initialize_calls += 1
        task = asyncio.current_task()
        assert task is not None
        self.initialize_tasks.append(task)
        self.initialize_started.set()
        await self.initialize_release.wait()
        if self.fail:
            raise RuntimeError("PRIVATE setup detail must not reach the glasses")
        self.capture_context_generation = self.initialize_calls

    async def stream(self, text: str) -> AsyncIterator[str]:
        self.unscoped.append(text)
        yield "unexpected ordinary instruction"

    async def stream_captured(self, text: str, *, context_generation: int) -> AsyncIterator[str]:
        assert context_generation == self.capture_context_generation
        self.scoped.append((text, context_generation))
        yield "unexpected captured instruction"


class Transport:
    def __init__(self) -> None:
        self.sent: list[object] = []
        self.questions: list[PromptPresent] = []
        self.cancelled: list[str | None] = []

    async def send(self, message: Any) -> None:
        message.to_wire()  # Exercise the actual outbound schema as well.
        self.sent.append(message)

    async def request_prompt(
        self, request: PromptPresent, *, on_presented: Callable[[PromptAck], bool],
        on_revoked: Callable[[], None] | None = None, timeout_s: float = 5,
    ) -> bool:
        del on_revoked, timeout_s
        request.to_wire()
        self.questions.append(request)
        return on_presented(PromptAck(
            prompt_id=request.prompt_id,
            presentation_binding=request.presentation_binding,
            connection_generation=request.connection_generation,
            source=request.source, success=True, audio_epoch=request.prompt_id,
        ))

    async def cancel_prompt(self, prompt_id: str | None = None) -> bool:
        self.cancelled.append(prompt_id)
        return True


class NeverSender:
    async def send(self, recipient: str, body: str) -> None:
        raise AssertionError("This fixture must never send a message")


class OpeningRig:
    def __init__(self, tmp_path: Path) -> None:
        self.generation: str | None = "authenticated-connection-1"
        self.transport = Transport()
        self.hud = HudWriter(self.transport)  # type: ignore[arg-type]
        self.agents = {name: InitializingAgent() for name in ("work", "calendar", "hermes")}
        self.frames: list[tuple[TerminalFrame, str | None, str | None]] = []
        self.opens: list[tuple[str, str | None, str, Callable[[], bool]]] = []
        self.open_tasks: list[asyncio.Task[Any]] = []
        self.finished_opens = 0
        self.fences = 0

        async def relay(frame: TerminalFrame) -> None:
            await self.opening.paint(frame)

        async def paint(frame: TerminalFrame, request_id: str | None, selection_id: str | None) -> None:
            self.frames.append((frame, request_id, selection_id))
            await self.hud.show_agent_terminal(AgentTerminalHudView(
                state=frame.state, focus=frame.topic, lanes=frame.lanes,
                activity="\n".join(frame.lines), approvals=frame.approvals,
                reason=getattr(frame, "reason", ""),
                opening_request_id=request_id, opening_selection_id=selection_id,
            ))

        self.roster = AgentTerminalRoster(list(self.agents.items()), paint=relay)
        self.commands = AgentTerminalCommands(
            self.roster,
            on_explicit_view=lambda: self.opening.claim_legacy(self.router.claim_terminal_voice),
        )
        self.opening = TerminalOpeningController(
            self.roster, paint=paint, connection_generation=lambda: self.generation,
        )

        async def open_lane(
            name: str, request_id: str | None, selection_id: str, is_current: Callable[[], bool],
        ) -> None:
            task = asyncio.current_task()
            assert task is not None
            self.open_tasks.append(task)
            self.opens.append((name, request_id, selection_id, is_current))
            try:
                await self.opening.open(name, request_id, selection_id, is_current)
            finally:
                self.finished_opens += 1

        self.router = CapabilityRouter(
            self.hud, self.transport, StatusPublisher(tmp_path / "status.json"),
            None, CardLog(), terminal_rows=self.roster.picker_rows,
            terminal_busy=lambda: self.roster.busy, terminal_open=open_lane,
            terminal_current=lambda: self.roster.focused,
        )  # type: ignore[arg-type]

        async def never_confirm(*args: Any, **kwargs: Any) -> bool:
            raise AssertionError("Opening Terminal must never request a message send")

        def never_start(topic: str) -> Any:
            raise AssertionError("Opening Terminal must never start Brainstorm")

        self.replies = MessageReplyController(NeverSender(), never_confirm, contact_search=lambda _: [])
        self.sessions = SessionManager(self.hud, never_start)
        self.runtime = PromptedSpeechRuntime(
            self.hud, self.replies, sessions=lambda: self.sessions,
            connection_generation=lambda: self.generation,
            fence_audio=self.fence, capturing=lambda: False, terminal=lambda: self.commands,
        )

    def fence(self) -> int:
        self.fences += 1
        return 10

    async def picker(self, request_id: str = "visit-1") -> None:
        await self.router.open("Agent Terminal", request_id=request_id)
        lists = [item for item in self.transport.sent if isinstance(item, HudList)]
        assert lists[-1].title == "AGENT TERMINAL"

    def select(self, name: str = "work", *, request_id: str = "visit-1", selection_id: str = "tap-1") -> None:
        self.router.on_input(InputEvent(
            container_name="capdata", selected_name="AGENT TERMINAL",
            selected_index=list(self.agents).index(name), request_id=request_id, trace_id=selection_id,
        ))

    async def opened(self, count: int = 1) -> None:
        await until(lambda: self.finished_opens >= count)

    async def entered(self, name: str = "work") -> None:
        agent = self.agents[name]
        await until(lambda: agent.initialize_started.is_set() or self.finished_opens > 0)
        assert agent.initialize_started.is_set(), "The real opening chain did not initialize its selected lane"

    def home(self) -> None:
        self.router.on_input(InputEvent(container_name="menu", selected_name="closed"))

    def assert_no_input(self) -> None:
        assert all(not agent.scoped and not agent.unscoped for agent in self.agents.values())

    async def aclose(self) -> None:
        for agent in self.agents.values():
            agent.initialize_release.set()
        self.router.on_disconnect()
        self.generation = None
        await asyncio.wait_for(self.runtime.aclose(), 1)
        await asyncio.wait_for(self.sessions.close(), 1)
        await asyncio.wait_for(self.replies.aclose(), 1)
        tasks = set(self.open_tasks)
        tasks.update(task for agent in self.agents.values() for task in agent.initialize_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 1)
        for name in self.agents:
            await asyncio.wait_for(self.roster.cancel(name), 1)
        await asyncio.sleep(0)  # Finish bounded router/Home and roster callbacks.


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[OpeningRig]:
    fixture = OpeningRig(tmp_path)
    try:
        yield fixture
    finally:
        await fixture.aclose()


async def test_first_use_picker_selects_and_prepares_work_before_scoped_question(rig: OpeningRig) -> None:
    await rig.picker()
    assert all(agent.initialize_calls == 0 for agent in rig.agents.values())
    assert rig.commands.begin_capture() is None
    rig.select()
    await rig.opened()
    assert rig.agents["work"].initialize_calls == 1
    assert rig.agents["calendar"].initialize_calls == rig.agents["hermes"].initialize_calls == 0
    assert rig.opening.current and rig.roster.focused == "work"
    frame, request_id, selection_id = rig.frames[-1]
    assert (frame.state, request_id, selection_id) == ("ready", "visit-1", "tap-1")
    capture = rig.commands.begin_capture()
    assert capture is not None and capture.lane_name == "work"
    assert await rig.runtime.publish_terminal_capture(capture)
    assert rig.transport.questions[-1].question == capture.question
    assert rig.runtime.dispatcher.is_armed and rig.fences == 1
    assert not rig.commands.armed, "A question must not arm legacy command parsing"
    rig.assert_no_input()


async def test_held_first_use_cannot_offer_capture_before_adapter_is_ready(rig: OpeningRig) -> None:
    rig.agents["work"].initialize_release.clear()
    await rig.picker()
    rig.select()
    await rig.entered()
    assert rig.opening.current
    assert rig.frames[-1][0].state == "unavailable"
    assert rig.commands.begin_capture() is None
    assert rig.transport.questions == []
    rig.agents["work"].initialize_release.set()
    await rig.opened()
    assert rig.frames[-1][0].state == "ready"
    rig.assert_no_input()


@pytest.mark.parametrize("leave", ["home", "new-capability", "disconnect", "reconnect", "focus-away-back"])
async def test_held_open_loses_ownership_before_late_ready_and_roster_updates(rig: OpeningRig, leave: str) -> None:
    rig.agents["work"].initialize_release.clear()
    await rig.picker()
    rig.select()
    await rig.entered()
    before = len(rig.frames)
    if leave == "home":
        rig.home()
    elif leave == "new-capability":
        rig.router.on_input(InputEvent(container_name="capmenu", selected_name="Messages", request_id="visit-2"))
    elif leave == "disconnect":
        rig.router.on_disconnect()
        rig.generation = None
    elif leave == "reconnect":
        rig.generation = "authenticated-connection-2"
    else:
        assert rig.roster.focus("calendar")
        assert rig.roster.focus("work")
    assert not rig.opening.current, "Navigation invalidation must be synchronous, including focus ABA"
    rig.agents["work"].initialize_release.set()
    await rig.opened()
    await rig.roster.repaint()
    assert len(rig.frames) == before, "Late readiness or background repaint restored an abandoned opening"
    assert rig.transport.questions == []
    rig.assert_no_input()


async def test_new_selection_owns_all_frames_after_synchronously_displacing_first(rig: OpeningRig) -> None:
    rig.agents["work"].initialize_release.clear()
    await rig.picker()
    rig.select()
    await rig.entered()
    before = len(rig.frames)
    rig.select("calendar", selection_id="tap-2")
    assert not rig.opening.current
    await until(lambda: rig.agents["calendar"].initialize_calls == 1)
    rig.agents["work"].initialize_release.set()
    await rig.opened(2)
    await rig.roster.repaint()
    assert rig.roster.focused == "calendar" and rig.opening.current
    assert rig.frames[before:]
    assert all(request == "visit-1" and selection == "tap-2" for _, request, selection in rig.frames[before:])
    assert rig.frames[-1][0].state == "ready"
    rig.assert_no_input()


async def test_same_lane_reselection_cannot_relabel_the_old_opening_receipt(rig: OpeningRig) -> None:
    rig.agents["work"].initialize_release.clear()
    await rig.picker()
    rig.select()
    await rig.entered()
    before = len(rig.frames)
    rig.select(selection_id="tap-2")
    assert not rig.opening.current
    await until(lambda: len(rig.opens) == 2)
    rig.agents["work"].initialize_release.set()
    await rig.opened(2)
    assert rig.opening.current and rig.frames[-1][0].state == "ready"
    assert all(selection == "tap-2" for _, _, selection in rig.frames[before:])
    assert rig.agents["work"].initialize_calls == 1, "Concurrent openings should share this lane's setup"
    rig.assert_no_input()


async def test_home_before_queued_tap_never_starts_initialization(rig: OpeningRig) -> None:
    await rig.picker()
    rig.select()
    rig.home()
    for _ in range(3):
        await asyncio.sleep(0)
    assert rig.opens == []
    assert all(agent.initialize_calls == 0 for agent in rig.agents.values())
    assert rig.transport.questions == []
    rig.assert_no_input()


async def test_retry_prepares_only_failed_selected_work_and_keeps_opening_binding(rig: OpeningRig) -> None:
    rig.agents["work"].fail = True
    await rig.picker()
    rig.select()
    await rig.opened()
    assert rig.frames[-1][0].state in ("blocked", "unavailable")
    assert rig.commands.begin_capture() is None
    assert "PRIVATE" not in repr(rig.transport.sent)
    rig.agents["work"].fail = False
    assert await asyncio.wait_for(rig.opening.retry(), 1)
    assert rig.frames[-1][0].state == "ready"
    assert rig.frames[-1][1:] == ("visit-1", "tap-1")
    assert rig.agents["work"].initialize_calls == 2
    assert rig.agents["hermes"].initialize_calls == rig.agents["calendar"].initialize_calls == 0
    assert rig.transport.questions == [], "Retry prepares the lane; a fresh tap owns instruction capture"
    rig.assert_no_input()


async def test_departed_owned_retry_is_handled_without_wrong_hermes_fallback(rig: OpeningRig) -> None:
    assert not await rig.opening.retry(), "No opening existed to own this retry"
    rig.agents["work"].fail = True
    await rig.picker()
    rig.select()
    await rig.opened()
    rig.home()
    assert not rig.opening.current
    before = len(rig.frames)
    fallback_calls = 0
    if not await rig.opening.retry():
        fallback_calls += 1
    assert fallback_calls == 0, "A stale work retry must not fall through to Hermes recovery"
    assert len(rig.frames) == before
    assert rig.agents["work"].initialize_calls == 1
    assert rig.agents["hermes"].initialize_calls == 0
    rig.assert_no_input()


async def test_held_retry_cannot_publish_after_home(rig: OpeningRig) -> None:
    work = rig.agents["work"]
    work.fail = True
    await rig.picker()
    rig.select()
    await rig.opened()
    work.fail = False
    work.initialize_release.clear()
    retry = asyncio.create_task(rig.opening.retry())
    try:
        await until(lambda: work.initialize_calls == 2 or retry.done())
        assert work.initialize_calls == 2
        before = len(rig.frames)
        rig.home()
        assert not rig.opening.current
        work.initialize_release.set()
        assert await asyncio.wait_for(retry, 1), "The departed owned retry remains handled"
        assert len(rig.frames) == before
        assert rig.agents["hermes"].initialize_calls == 0
        rig.assert_no_input()
    finally:
        work.initialize_release.set()
        if not retry.done():
            retry.cancel()
        await asyncio.wait_for(asyncio.gather(retry, return_exceptions=True), 1)


async def test_cycle_prepares_next_lane_under_visit_and_invalidates_old_capture(rig: OpeningRig) -> None:
    await rig.picker()
    rig.select()
    await rig.opened()
    old_capture = rig.commands.begin_capture()
    assert old_capture is not None and old_capture.lane_name == "work"
    calendar = rig.agents["calendar"]
    calendar.initialize_release.clear()
    cycle = asyncio.create_task(rig.opening.cycle())
    try:
        await until(lambda: calendar.initialize_started.is_set() or cycle.done())
        assert calendar.initialize_started.is_set(), "Cycling to an unprepared lane must initialize it"
        assert rig.roster.focused == "calendar" and rig.opening.current
        assert not rig.commands.capture_is_current(old_capture)
        assert await rig.commands.consume_capture(old_capture, "must not reach calendar") is None
        assert rig.commands.begin_capture() is None
        calendar.initialize_release.set()
        result = await asyncio.wait_for(cycle, 1)
        assert isinstance(result, str) and result
        assert rig.opening.current and rig.frames[-1][0].state == "ready"
        assert rig.frames[-1][1:] == ("visit-1", "tap-1")
        new_capture = rig.commands.begin_capture()
        assert new_capture is not None and new_capture.lane_name == "calendar"
        await rig.roster.repaint()
        assert rig.frames[-1][1:] == ("visit-1", "tap-1")
        assert rig.agents["work"].initialize_calls == calendar.initialize_calls == 1
        assert rig.agents["hermes"].initialize_calls == 0
        rig.assert_no_input()
    finally:
        calendar.initialize_release.set()
        if not cycle.done():
            cycle.cancel()
        await asyncio.wait_for(asyncio.gather(cycle, return_exceptions=True), 1)


async def test_departed_owned_cycle_does_not_focus_or_prepare_another_lane(rig: OpeningRig) -> None:
    await rig.picker()
    rig.select()
    await rig.opened()
    rig.home()
    before = len(rig.frames)
    result = await asyncio.wait_for(rig.opening.cycle(), 1)
    assert result == "", "A departed gesture must not request a SessionManager narration"
    assert rig.roster.focused == "work" and not rig.opening.current
    assert len(rig.frames) == before
    assert rig.agents["calendar"].initialize_calls == rig.agents["hermes"].initialize_calls == 0
    rig.assert_no_input()


@pytest.mark.parametrize("action", ["retry", "cycle"])
async def test_queued_action_keeps_its_original_opening_when_another_lane_is_selected(
    rig: OpeningRig, action: str,
) -> None:
    await rig.picker()
    rig.select("work")
    await rig.opened()
    # Capture the accepted action synchronously, as SessionManager must do
    # before scheduling its worker. Do not begin awaiting it until B owns UI.
    operation = rig.opening.retry() if action == "retry" else rig.opening.cycle()
    awaited = False
    try:
        rig.select("calendar", selection_id="tap-2")
        await rig.opened(2)
        assert rig.roster.focused == "calendar" and rig.opening.current
        before = len(rig.frames)
        calls = {name: agent.initialize_calls for name, agent in rig.agents.items()}
        awaited = True
        result = await asyncio.wait_for(operation, 1)
        if action == "retry":
            assert result is True, "A stale work retry remains handled without fallback"
        else:
            assert result == "", "A stale work cycle must not narrate or move calendar"
        assert rig.roster.focused == "calendar" and rig.opening.current
        assert len(rig.frames) == before, "A queued action operated on its successor opening"
        assert {name: agent.initialize_calls for name, agent in rig.agents.items()} == calls
        assert rig.frames[-1][1:] == ("visit-1", "tap-2")
        rig.assert_no_input()
    finally:
        if not awaited and inspect.iscoroutine(operation):
            operation.close()


async def test_explicit_spoken_terminal_reopens_after_home_without_reviving_background_paint(
    rig: OpeningRig,
) -> None:
    await rig.picker()
    rig.select("work")
    await rig.opened()
    rig.home()
    before_voice = len(rig.frames)
    result = await rig.commands.handle_command("go to the calendar terminal")
    assert result == "Focused on calendar."
    assert rig.roster.focused == "calendar"
    assert len(rig.frames) > before_voice, "An explicit spoken Terminal entry stayed hidden after Home"
    frame, request_id, selection_id = rig.frames[-1]
    assert frame.topic.split(" · ", 1)[0] == "calendar"
    assert (request_id, selection_id) == (None, None), "Legacy voice entry retains its existing unbound wire path"
    rig.home()
    after_home = len(rig.frames)
    await rig.roster.repaint()
    assert len(rig.frames) == after_home, "The spoken entry revived background repaint rights after Home"
    rig.assert_no_input()


async def test_spoken_switch_inside_bound_terminal_retains_visit_until_matching_home(rig: OpeningRig) -> None:
    await rig.picker()
    rig.select("work")
    await rig.opened()
    before_voice = len(rig.frames)
    result = await rig.commands.handle_command("go to the calendar terminal")
    assert result == "Focused on calendar."
    assert rig.roster.focused == "calendar"
    assert len(rig.frames) > before_voice
    frame, request_id, selection_id = rig.frames[-1]
    assert frame.topic.split(" · ", 1)[0] == "calendar"
    assert (request_id, selection_id) == ("visit-1", "tap-1"), "A spoken switch lost the Hub's accepted opening binding"
    await rig.roster.repaint()
    assert rig.frames[-1][1:] == ("visit-1", "tap-1")
    # This is the actual Hub departure receipt for the still-bound view,
    # rather than the older unbound menu/closed compatibility event.
    rig.router.on_input(InputEvent(
        container_name="capability-lifecycle", selected_name="terminal-home",
        request_id="visit-1", trace_id="tap-1",
    ))
    after_home = len(rig.frames)
    await rig.roster.repaint()
    assert len(rig.frames) == after_home, "Matching Terminal Home failed to retire the spoken successor"
    rig.assert_no_input()


class InstructionVisit:
    """Real visit ownership; the router's separate wire-entry test owns its list."""

    def __init__(self, rig: OpeningRig) -> None:
        self.rig = rig
        self.calls: list[tuple[str, str]] = []
        self.after_open: Callable[[], Awaitable[None]] | None = None
        self.accept = True

    async def __call__(self, name: str, request_id: str) -> bool:
        self.calls.append((name, request_id))
        current = self.rig.router.claim_terminal_voice(request_id, request_id)
        await self.rig.opening.open(name, request_id, request_id, current)
        if self.after_open is not None:
            await self.after_open()
        return self.accept and self.rig.opening.current


async def test_terminal_instruction_router_emits_voice_entry_before_bound_selected_lane(rig: OpeningRig) -> None:
    assert await rig.router.prepare_terminal_instruction("hermes", "voice-instruction-entry")

    entry_index = next(index for index, item in enumerate(rig.transport.sent) if isinstance(item, HudList))
    entry = rig.transport.sent[entry_index]
    assert isinstance(entry, HudList)
    assert entry.title == "AGENT TERMINAL" and entry.request_id == "voice-instruction-entry"
    terminal_frames = [(index, item) for index, item in enumerate(rig.transport.sent)
                       if isinstance(item, AgentTerminalHudView)]
    assert terminal_frames and all(index > entry_index for index, _ in terminal_frames)
    assert all((frame.opening_request_id, frame.opening_selection_id)
               == ("voice-instruction-entry", "voice-instruction-entry")
               for _, frame in terminal_frames)
    assert rig.roster.focused == "hermes" and rig.opening.current
    assert rig.agents["hermes"].initialize_calls == 1
    rig.assert_no_input()


async def test_terminal_instruction_router_rejects_unknown_before_mutating_existing_visit(rig: OpeningRig) -> None:
    await rig.picker()
    rig.select("work")
    await rig.opened()
    before = len(rig.transport.sent)

    assert not await rig.router.prepare_terminal_instruction("hermes extra", "voice-invalid")

    assert len(rig.transport.sent) == before
    assert rig.opening.current and rig.roster.focused == "work"
    await rig.roster.repaint()
    assert rig.frames[-1][1:] == ("visit-1", "tap-1")
    rig.assert_no_input()


async def test_terminal_instruction_router_home_during_prepare_cannot_return_success(rig: OpeningRig) -> None:
    rig.agents["hermes"].initialize_release.clear()
    preparation = asyncio.create_task(rig.router.prepare_terminal_instruction("hermes", "voice-held-entry"))
    try:
        await until(lambda: rig.agents["hermes"].initialize_started.is_set())
        rig.home()
        before = len(rig.frames)
        rig.agents["hermes"].initialize_release.set()
        assert await preparation is False
        assert len(rig.frames) == before and not rig.opening.current
        rig.assert_no_input()
    finally:
        preparation.cancel()
        await asyncio.gather(preparation, return_exceptions=True)


async def test_terminal_instruction_opens_exact_lane_and_uses_captured_literal(rig: OpeningRig) -> None:
    visit = InstructionVisit(rig)
    literal = "  Café: keep  spacing.\r\nDo not reinterpret ‘terminal cancel’.  "

    receipt = await rig.opening.submit_instruction("hermes", literal, "request-1", visit)

    assert not receipt.startswith("error:") and "accepted" in receipt.lower()
    assert "completed" not in receipt.lower() and literal not in receipt
    assert len(visit.calls) == 1 and visit.calls[0][0] == "hermes"
    assert visit.calls[0][1].startswith("voice-")
    assert rig.frames[-1][1:] == (visit.calls[0][1], visit.calls[0][1])
    await until(lambda: bool(rig.agents["hermes"].scoped))
    assert rig.agents["hermes"].scoped == [(literal, 1)]
    assert all(not agent.unscoped for agent in rig.agents.values())
    assert not rig.agents["work"].scoped and not rig.agents["calendar"].scoped
    assert not rig.transport.questions, "Already supplied text must not arm a microphone question"


async def test_terminal_instruction_pending_retry_never_reopens_or_duplicates(rig: OpeningRig) -> None:
    visit = InstructionVisit(rig)
    rig.agents["hermes"].initialize_release.clear()
    first = asyncio.create_task(rig.opening.submit_instruction("hermes", "Question", "once", visit))
    try:
        await until(lambda: rig.agents["hermes"].initialize_started.is_set())
        pending = await rig.opening.submit_instruction("hermes", "Question", "once", visit)
        assert "progress" in pending.lower() or "pending" in pending.lower()
        assert "accepted" not in pending.lower()
        assert len(visit.calls) == 1
        rig.assert_no_input()
        conflict = await rig.opening.submit_instruction("hermes", "Changed", "once", visit)
        assert conflict.startswith("error:") and len(visit.calls) == 1
        rig.agents["hermes"].initialize_release.set()
        assert "accepted" in (await first).lower()
        await until(lambda: bool(rig.agents["hermes"].scoped))
        assert rig.agents["hermes"].scoped == [("Question", 1)]
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.parametrize("change", ["lane", "body"])
async def test_terminal_instruction_accepted_identity_binds_exact_lane_and_body(
    rig: OpeningRig, change: str,
) -> None:
    visit = InstructionVisit(rig)
    first = await rig.opening.submit_instruction("hermes", "  Question  ", "once", visit)
    await until(lambda: bool(rig.agents["hermes"].scoped))
    rig.home()
    before = len(rig.frames)

    replay = await rig.opening.submit_instruction("hermes", "  Question  ", "once", visit)
    conflict = await rig.opening.submit_instruction(
        "work" if change == "lane" else "hermes",
        "Question" if change == "body" else "  Question  ", "once", visit,
    )

    assert "accepted" in first.lower() and "already" in replay.lower()
    assert conflict.startswith("error:")
    assert len(visit.calls) == 1 and len(rig.frames) == before
    assert rig.agents["hermes"].scoped == [("  Question  ", 1)]
    assert not rig.agents["work"].scoped


@pytest.mark.parametrize("name", ["missing", "HERmes", "hermes extra", "", "work-calendar"])
async def test_terminal_instruction_unknown_lane_is_rejected_before_display(
    rig: OpeningRig, name: str,
) -> None:
    visit = InstructionVisit(rig)
    result = await rig.opening.submit_instruction(name, "Question", "once", visit)
    assert result.startswith("error:")
    assert visit.calls == [] and rig.frames == []
    assert rig.roster.focused == "work"
    rig.assert_no_input()


@pytest.mark.parametrize("departure", ["home", "reconnect", "focus-round-trip", "same-lane-successor"])
async def test_terminal_instruction_held_open_cannot_submit_into_departed_or_successor_visit(
    rig: OpeningRig, departure: str,
) -> None:
    visit = InstructionVisit(rig)
    opened, release = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        opened.set()
        await release.wait()

    visit.after_open = hold
    pending = asyncio.create_task(rig.opening.submit_instruction("hermes", "Question", "once", visit))
    try:
        await until(opened.is_set)
        if departure == "home":
            rig.home()
        elif departure == "reconnect":
            rig.generation = "authenticated-connection-2"
        elif departure == "focus-round-trip":
            assert rig.roster.focus("work") and rig.roster.focus("hermes")
        else:
            current = rig.router.claim_terminal_voice("voice-successor", "voice-successor")
            await rig.opening.open("hermes", "voice-successor", "voice-successor", current)
        before = len(rig.frames)
        release.set()
        assert (await pending).startswith("error:")
        assert len(rig.frames) == before
        rig.assert_no_input()
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_terminal_instruction_cancelled_preparation_can_retry_without_late_send(rig: OpeningRig) -> None:
    visit = InstructionVisit(rig)
    rig.agents["hermes"].initialize_release.clear()
    first = asyncio.create_task(rig.opening.submit_instruction("hermes", "Question", "once", visit))
    try:
        await until(lambda: rig.agents["hermes"].initialize_started.is_set())
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        rig.assert_no_input()
        rig.agents["hermes"].initialize_release.set()
        recovered = await rig.opening.submit_instruction("hermes", "Question", "once", visit)
        assert "accepted" in recovered.lower()
        await until(lambda: bool(rig.agents["hermes"].scoped))
        assert rig.agents["hermes"].scoped == [("Question", 2)]
        assert len(visit.calls) == 2
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.parametrize("failure", ["declined", "exception", "unavailable"])
async def test_terminal_instruction_failed_open_does_not_poison_recovery_retry(
    rig: OpeningRig, failure: str,
) -> None:
    visit = InstructionVisit(rig)
    visit.accept = failure != "declined"
    rig.agents["hermes"].fail = failure == "unavailable"

    async def fail() -> None:
        raise RuntimeError("PRIVATE callback detail")

    if failure == "exception":
        visit.after_open = fail
    refused = await rig.opening.submit_instruction("hermes", "Question", "once", visit)
    assert refused.startswith("error:") and "PRIVATE" not in refused
    rig.assert_no_input()
    visit.accept = True
    visit.after_open = None
    rig.agents["hermes"].fail = False
    accepted = await rig.opening.submit_instruction("hermes", "Question", "once", visit)
    assert "accepted" in accepted.lower()
    await until(lambda: bool(rig.agents["hermes"].scoped))
    assert len(rig.agents["hermes"].scoped) == 1


async def test_terminal_instruction_retry_after_admission_does_not_wait_for_or_repeat_result(rig: OpeningRig) -> None:
    visit = InstructionVisit(rig)
    release = asyncio.Event()

    async def held_result(text: str, *, context_generation: int) -> AsyncIterator[str]:
        rig.agents["hermes"].scoped.append((text, context_generation))
        await release.wait()
        yield "Synthetic terminal result"

    rig.agents["hermes"].stream_captured = held_result  # type: ignore[method-assign]
    try:
        request = asyncio.create_task(rig.opening.submit_instruction("hermes", "Question", "once", visit))
        receipt = await asyncio.wait_for(request, 1)
        assert "accepted" in receipt.lower()
        await until(lambda: bool(rig.agents["hermes"].scoped))
        assert request.cancel() is False, "Admission receipt must be committed before caller cancellation"
        assert "already" in (await rig.opening.submit_instruction("hermes", "Question", "once", visit)).lower()
        assert len(visit.calls) == 1 and rig.agents["hermes"].scoped == [("Question", 1)]
        release.set()
        await until(lambda: not rig.roster.busy)
        assert "Synthetic terminal result" in rig.roster.tail("hermes")
    finally:
        release.set()


async def test_terminal_instruction_retry_ledger_caps_at_256_without_evicting_accepted(rig: OpeningRig) -> None:
    visit = InstructionVisit(rig)
    for index in range(256):
        receipt = await rig.opening.submit_instruction("hermes", f"Question {index}", f"once-{index}", visit)
        assert "accepted" in receipt.lower()
        await until(lambda: len(rig.agents["hermes"].scoped) == index + 1 and not rig.roster.busy)

    capacity = await rig.opening.submit_instruction("hermes", "Overflow", "overflow", visit)
    replay = await rig.opening.submit_instruction("hermes", "Question 0", "once-0", visit)

    assert capacity.startswith("error:") and "capacity" in capacity.lower()
    assert "already" in replay.lower()
    assert len(visit.calls) == 256 and len(rig.agents["hermes"].scoped) == 256


async def test_brainstorm_handoff_uses_picker_and_real_selected_lane(rig: OpeningRig) -> None:
    rig.router.claim_brainstorm_view()
    sink = HudSink(
        rig.hud, open_terminal=rig.router.open_terminal_from_brainstorm,
        brainstorm_view_current=lambda: rig.router.brainstorm_view_current,
    )
    await sink.open("One work surface", ["Codex", "Claude"])
    await sink.show_terminal()
    picker = [m for m in rig.transport.sent if isinstance(m, HudList)][-1]
    assert picker.title == "AGENT TERMINAL"
    assert not rig.frames, "A picker is not an invented running Hermes session"
    assert picker.request_id is not None
    rig.select("work", request_id=picker.request_id)
    await rig.opened()
    assert rig.frames[-1][0].state == "ready"
    assert rig.frames[-1][0].topic.startswith("work")
    before = len(rig.transport.sent)
    await sink.chunk("Preserved while reading Terminal")
    await sink.close("settled")
    assert len(rig.transport.sent) == before
    rig.router.claim_brainstorm_view()
    await sink.replay()
    restored = rig.transport.sent[-1]
    assert isinstance(restored, BrainstormHudView)
    assert restored.state == "settled"
    assert "Preserved while reading Terminal" in restored.activity
    rig.assert_no_input()


@pytest.mark.parametrize("destination", ["home", "settings", "news", "search", "pomodoro", "feature"])
async def test_local_departure_stops_brainstorm_frames_without_mac_repaint(
    rig: OpeningRig, destination: str,
) -> None:
    rig.router.claim_brainstorm_view()
    sink = HudSink(rig.hud, brainstorm_view_current=lambda: rig.router.brainstorm_view_current)
    await sink.open("Keep selected feature", ["Codex", "Claude"])
    before = len(rig.transport.sent)
    rig.router.on_input(InputEvent(
        container_name="capability-lifecycle", selected_name=f"surface-{destination}",
    ))
    await sink.chunk("Background result")
    await sink.close("settled")
    await asyncio.sleep(0)
    assert len(rig.transport.sent) == before
    assert not rig.router.brainstorm_view_current


async def test_contextual_overlay_close_cannot_restore_departed_brainstorm(rig: OpeningRig) -> None:
    def lifecycle(name: str) -> None:
        rig.router.on_input(InputEvent(container_name="capability-lifecycle", selected_name=name))

    rig.router.claim_brainstorm_view()
    lifecycle("surface-context-menu-open")
    assert not rig.router.brainstorm_view_current
    lifecycle("surface-context-menu-closed")
    assert rig.router.brainstorm_view_current
    lifecycle("surface-context-menu-open")
    lifecycle("surface-home")
    lifecycle("surface-context-menu-closed")
    assert not rig.router.brainstorm_view_current


async def test_feature_tap_retires_brainstorm_before_async_open(rig: OpeningRig) -> None:
    rig.router.claim_brainstorm_view()
    rig.router.on_input(InputEvent(container_name="capmenu", selected_name="Messages", request_id="messages"))
    assert not rig.router.brainstorm_view_current
    await asyncio.sleep(0)
