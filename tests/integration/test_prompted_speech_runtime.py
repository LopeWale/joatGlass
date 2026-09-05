"""Production prompt composition with real owners and controlled I/O.

Socket cases exercise real authentication, HUD acknowledgment and PCM ingress.
They do not claim optical timing, recognition accuracy or Messages delivery.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
import json
from pathlib import Path
import struct
import time
from typing import Any

import pytest
import websockets

from joat.audio.router import AudioRouter
from joat.audio.vad import RmsVad
from joat.ble.hub_gateway import HubGatewayTransport
from joat.core.interfaces import AudioFrame, AudioFrameCallback, AudioFrameSource, GlassesTransport, Transcript
from joat.core.pairing import PairingStore
from joat.core.protocol import AudioSource, BrainstormAction, Device, DownlinkMessage
from joat.services.agent_terminal import AgentTerminalCommands, AgentTerminalRoster, TerminalCapture
from joat.services.hud import HudWriter
from joat.services.message_prompts import ActivatePrompt, PromptPresentationAck
from joat.services.message_reply import MessageActionStatus, MessageReplyController
from joat.services.orchestrator import VoiceOrchestrator
from joat.services.prompted_input import PromptTicket, UtteranceProvenance
from joat.services.prompted_speech import PromptedSpeechRuntime
from joat.services.session import Session, SessionManager, TopicCapture


async def until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


class Transport(GlassesTransport):
    def __init__(self) -> None:
        self.sent: list[DownlinkMessage] = []

    @property
    def connected(self) -> bool:
        return True

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, message: DownlinkMessage) -> None:
        self.sent.append(message)
    def on_audio(self, callback: Any) -> None: ...
    def on_input(self, callback: Any) -> None: ...
    def on_device(self, callback: Any) -> None: ...


@dataclass
class Publication:
    ticket: PromptTicket
    question: str
    activate: ActivatePrompt
    revoke: Callable[[str], object]
    finished: asyncio.Event
    activated: bool = False


class ControlledHud(HudWriter):
    def __init__(self) -> None:
        super().__init__(Transport())
        self.publications: list[Publication] = []
        self.cancellations: list[str] = []
        self.hold: asyncio.Event | None = None
        self.ignore_cancel = False
        self.cancelled = asyncio.Event()

    async def publish_prompt(
        self, ticket: PromptTicket, question: str, activate: ActivatePrompt, *,
        fence_audio: Callable[[], int], revoke: Callable[[str], object],
    ) -> bool:
        publication = Publication(ticket, question, activate, revoke, asyncio.Event())
        self.publications.append(publication)
        hold, ignore_cancel = self.hold, self.ignore_cancel
        try:
            while hold is not None and not hold.is_set():
                try:
                    await hold.wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
                    if not ignore_cancel:
                        raise
                    # This host deliberately finishes late even if both its
                    # owner and runtime request cancellation. Only the test's
                    # explicit release lets it attempt the stale receipt.
            publication.activated = activate(PromptPresentationAck(
                prompt_id=ticket.prompt_id, success=True,
                presentation_binding=ticket.presentation_binding,
                connection_generation=ticket.connection_generation,
                audio_epoch=ticket.prompt_id, ingress_fence=fence_audio(),
            ))
            return publication.activated
        finally:
            publication.finished.set()

    async def cancel_prompt(self, prompt_id: str) -> bool:
        self.cancellations.append(prompt_id)
        for publication in self.publications:
            if publication.ticket.prompt_id == prompt_id:
                publication.revoke(prompt_id)
                return True
        return False


class Sender:
    async def send(self, _recipient: str, _body: str) -> None:
        raise AssertionError("The fixture must never send a real or simulated message")


class Rig:
    def __init__(
        self, *, hud: HudWriter | None = None,
        generation: Callable[[], str | None] | None = None,
        capturing: Callable[[], bool] | None = None,
        terminal: Callable[[], AgentTerminalCommands] | None = None,
        audio_source: Callable[[], AudioSource] | None = None,
    ) -> None:
        self.hud = hud if hud is not None else ControlledHud()
        self.now = time.monotonic()
        self.generation: str | None = "authenticated-session"
        self.capturing = False
        self.fences = 0
        self.fence: Callable[[], int] = self._fence
        self.reviewed: list[str] = []
        self.review_release = asyncio.Event()
        self.session_release = asyncio.Event()
        self.topics: list[str] = []
        self.factory_owners: list[TopicCapture | None] = []
        self.calls: list[asyncio.Task[Any]] = []

        async def confirm(_action: str, text: str, _timeout: float, **_binding: Any) -> bool:
            self.reviewed.append(text)
            await self.review_release.wait()
            return False

        self.replies = MessageReplyController(
            Sender(), confirm,
            contact_search=lambda name: [("Sam", "fixture-contact")] if name == "Sam" else [],
        )
        self.runtime = PromptedSpeechRuntime(
            self.hud, self.replies, sessions=lambda: self.sessions,
            connection_generation=generation or (lambda: self.generation),
            fence_audio=lambda: self.fence(), capturing=capturing or (lambda: self.capturing),
            monotonic=lambda: self.now, terminal=terminal,
            **({"audio_source": audio_source} if audio_source is not None else {}),
        )
        rig = self

        class HeldSession(Session):
            async def run(self) -> None:
                await rig.session_release.wait()

            def cancel(self) -> None:
                rig.session_release.set()

        self.session = HeldSession()

        def factory(topic: str) -> Session:
            self.factory_owners.append(self.sessions.current_topic_capture)
            self.topics.append(topic)
            return self.session

        self.sessions = SessionManager(
            self.hud, factory, brainstorm_available=lambda: True,
            publish_topic_capture=self.runtime.publish_topic_capture,
            monotonic=lambda: self.now,
        )

    def _fence(self) -> int:
        self.fences += 1
        return 10

    def launch(self, coroutine: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coroutine)
        self.calls.append(task)
        return task

    def begin_topic(self) -> TopicCapture:
        self.sessions.handle_brainstorm_action(BrainstormAction("begin_topic_capture"))
        capture = self.sessions.current_topic_capture
        assert capture is not None
        return capture

    async def message(self, *, action_id: str = "runtime-message", recipient: str | None = "Sam") -> MessageActionStatus:
        status = await self.replies.prepare_new_message(
            action_id=action_id, trace_id=f"trace-{action_id}", recipient_name=recipient,
        )
        await self.runtime.after_message_input(status)
        return status

    async def aclose(self) -> None:
        self.review_release.set()
        self.session_release.set()
        if isinstance(self.hud, ControlledHud) and self.hud.hold is not None:
            self.hud.hold.set()
        self.runtime.begin_shutdown()
        await asyncio.wait_for(self.runtime.aclose(), 1)
        await self.sessions.close()
        await self.replies.aclose()
        for task in self.calls:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.calls, return_exceptions=True)


class ControlledTerminal:
    """No Gateway or model: record which production adapter seam is used."""

    def __init__(self) -> None:
        self.capture_context_generation: int | None = 1
        self.scoped: list[tuple[str, int]] = []
        self.unscoped: list[str] = []

    async def stream(self, text: str) -> AsyncIterator[str]:
        self.unscoped.append(text)
        yield "synthetic terminal result"

    async def stream_captured(self, text: str, *, context_generation: int) -> AsyncIterator[str]:
        if self.capture_context_generation != context_generation:
            raise RuntimeError("Captured terminal context changed")
        self.scoped.append((text, context_generation))
        yield "synthetic terminal result"


class TerminalRig(Rig):
    def __init__(self, **options: Any) -> None:
        self.work = ControlledTerminal()
        self.other = ControlledTerminal()
        self.roster = AgentTerminalRoster([("work", self.work), ("other", self.other)])
        self.terminal = AgentTerminalCommands(self.roster, monotonic=lambda: self.now)
        super().__init__(terminal=lambda: self.terminal, **options)

    def begin_terminal(self) -> TerminalCapture:
        capture = self.terminal.begin_capture()
        assert capture is not None
        return capture

    async def publish_terminal(self, capture: TerminalCapture) -> bool:
        return await self.runtime.publish_terminal_capture(capture)

    async def aclose(self) -> None:
        try:
            await super().aclose()
        finally:
            for name in self.roster.names():
                await asyncio.wait_for(self.roster.cancel(name), 1)
            # Let the roster's owned completion callbacks finish their repaint.
            await asyncio.sleep(0)


@pytest.fixture
async def terminal_rig() -> AsyncIterator[TerminalRig]:
    fixture = TerminalRig()
    try:
        yield fixture
    finally:
        await fixture.aclose()


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    fixture = Rig()
    try:
        yield fixture
    finally:
        await fixture.aclose()


def receipt(ticket: PromptTicket) -> UtteranceProvenance:
    return UtteranceProvenance(
        source="glasses", connection_generation=ticket.connection_generation,
        audio_epoch=ticket.prompt_id, first_ingress_ordinal=11,
        last_ingress_ordinal=14, speaker_roles=frozenset(),
    )


@pytest.mark.parametrize("owner", ["brainstorm", "terminal", "messages"])
@pytest.mark.parametrize("selected", [None, "phone"])
async def test_prompt_audio_source_is_frozen_for_each_owned_question(
    owner: str, selected: AudioSource | None,
) -> None:
    source: list[AudioSource] = [selected or "glasses"]
    fixture = TerminalRig(audio_source=(lambda: source[0]) if selected is not None else None)
    hud = fixture.hud
    assert isinstance(hud, ControlledHud)
    release = hud.hold = asyncio.Event()
    try:
        if owner == "brainstorm":
            fixture.begin_topic()
        elif owner == "terminal":
            capture = fixture.begin_terminal()
            fixture.launch(fixture.publish_terminal(capture))
        else:
            fixture.launch(fixture.message())
        await until(lambda: bool(hud.publications))
        ticket = hud.publications[0].ticket
        expected = selected or "glasses"
        assert ticket.owner_kind == owner and ticket.source == expected
        # Later device preferences cannot relabel an already presented request.
        source[0] = "glasses" if expected == "phone" else "phone"
        release.set()
        await until(lambda: fixture.runtime.dispatcher.is_armed)
        assert fixture.runtime.dispatcher.current_ticket is ticket
        assert ticket.source == expected
        wrong_source = "glasses" if expected == "phone" else "phone"
        literal = "  Keep this exact question?!  "
        assert not await fixture.runtime.dispatcher.dispatch(
            Transcript(literal, True), replace(receipt(ticket), source=wrong_source),
        )
        assert fixture.runtime.dispatcher.current_ticket is ticket
        assert await fixture.runtime.dispatcher.dispatch(
            Transcript(literal, True), replace(receipt(ticket), source=expected),
        )
        if owner == "brainstorm":
            assert fixture.topics == [literal]
        elif owner == "terminal":
            await until(lambda: not fixture.roster.busy)
            assert fixture.work.scoped == [(literal, 1)] and fixture.work.unscoped == []
        else:
            await until(lambda: bool(fixture.reviewed))
            assert fixture.reviewed == [f"Send to Sam? “{literal}”"]
    finally:
        release.set()
        await fixture.aclose()


async def test_message_questions_share_runtime_and_keep_exact_body(rig: Rig) -> None:
    hud = rig.hud
    assert isinstance(hud, ControlledHud)
    await rig.message(recipient="my wife")
    first = rig.runtime.dispatcher.current_ticket
    assert first is not None and first.field == "recipient"
    assert await rig.runtime.dispatcher.dispatch(Transcript("Sam", True), receipt(first))
    second = rig.runtime.dispatcher.current_ticket
    assert second is not None and second.field == "body" and second.prompt_id != first.prompt_id
    body = "  Stop, open News?!\nKeep this exact.  "
    assert await rig.runtime.dispatcher.dispatch(Transcript(body, True), receipt(second))
    await until(lambda: bool(rig.reviewed))
    assert rig.reviewed == [f"Send to Sam? “{body}”"]
    assert rig.runtime.dispatcher.current_ticket is None
    await until(lambda: second.prompt_id in hud.cancellations)
    assert first.prompt_id in hud.cancellations and rig.fences == 2


async def test_consumed_brainstorm_survives_retirement_until_owner_consumes(rig: Rig) -> None:
    hud = rig.hud
    assert isinstance(hud, ControlledHud)
    capture = rig.begin_topic()
    await until(lambda: rig.runtime.dispatcher.is_armed)
    ticket = rig.runtime.dispatcher.current_ticket
    assert ticket is not None and ticket.owner_id == capture.capture_id and ticket.field == "topic"
    topic = "  Stop?!\nShould JOAT open News or keep brainstorming?  "
    assert await rig.runtime.dispatcher.dispatch(Transcript(topic, True), receipt(ticket))
    assert rig.topics == [topic] and rig.factory_owners == [None]
    assert rig.sessions.active_session is rig.session
    assert not rig.sessions.topic_capture_armed
    await until(lambda: ticket.prompt_id in hud.cancellations)
    assert not await rig.runtime.dispatcher.dispatch(Transcript(topic, True), receipt(ticket))


async def test_replacement_clears_only_displaced_brainstorm_and_allows_a_fresh_tap(rig: Rig) -> None:
    hud = rig.hud
    assert isinstance(hud, ControlledHud)
    old_capture = rig.begin_topic()
    await until(lambda: rig.runtime.dispatcher.is_armed)
    old = rig.runtime.dispatcher.current_ticket
    assert old is not None
    await rig.message()
    message_ticket = rig.runtime.dispatcher.current_ticket
    assert message_ticket is not None and message_ticket.owner_kind == "messages"
    assert rig.sessions.current_topic_capture is None
    new_capture = rig.begin_topic()
    assert new_capture.capture_id != old_capture.capture_id
    await until(lambda: (
        rig.runtime.dispatcher.current_ticket is not None
        and rig.runtime.dispatcher.current_ticket.owner_id == new_capture.capture_id
        and rig.runtime.dispatcher.is_armed
    ))
    current = rig.runtime.dispatcher.current_ticket
    for publication in hud.publications[:-1]:
        publication.revoke(publication.ticket.prompt_id)
    await asyncio.sleep(0)
    assert rig.runtime.dispatcher.current_ticket is current
    assert rig.sessions.current_topic_capture is new_capture
    assert rig.topics == []


@pytest.mark.parametrize("owner", ["messages", "brainstorm"])
async def test_matching_transport_revocation_disarms_without_inventing_send_failure(rig: Rig, owner: str) -> None:
    hud = rig.hud
    assert isinstance(hud, ControlledHud)
    if owner == "messages":
        status = await rig.message()
    else:
        rig.begin_topic()
        await until(lambda: rig.runtime.dispatcher.is_armed)
    ticket = rig.runtime.dispatcher.current_ticket
    assert ticket is not None
    hud.publications[-1].revoke(ticket.prompt_id)
    assert rig.runtime.dispatcher.current_ticket is None
    assert rig.sessions.current_topic_capture is None
    assert not await rig.runtime.dispatcher.dispatch(Transcript("late input", True), receipt(ticket))
    if owner == "messages":
        assert rig.replies.message_action_status(status.action_id).state == "awaiting_input"


@pytest.mark.parametrize("reason", ["explicit", "expired", "owner_invalid"])
async def test_brainstorm_retirement_clears_capture_without_starting_session(rig: Rig, reason: str) -> None:
    capture = rig.begin_topic()
    await until(lambda: rig.runtime.dispatcher.is_armed)
    if reason == "explicit":
        rig.runtime.revoke_current()
    elif reason == "expired":
        rig.now = capture.expires_at
        assert rig.runtime.dispatcher.current_ticket is None
    else:
        rig.sessions.clear_topic_capture()
        assert rig.runtime.dispatcher.current_ticket is None
    assert rig.sessions.current_topic_capture is None
    assert rig.runtime.dispatcher.current_ticket is None and rig.topics == []


@pytest.mark.parametrize("owner", ["messages", "brainstorm"])
@pytest.mark.parametrize("blocked", ["closed", "recording", "no_generation"])
async def test_unavailable_runtime_never_publishes_or_arms(rig: Rig, owner: str, blocked: str) -> None:
    hud = rig.hud
    assert isinstance(hud, ControlledHud)
    if blocked == "closed":
        rig.runtime.begin_shutdown()
    elif blocked == "recording":
        rig.capturing = True
    else:
        rig.generation = None
    if owner == "messages":
        status = await rig.message()
        assert status.state == "awaiting_input"
    else:
        rig.begin_topic()
        await until(lambda: rig.sessions.current_topic_capture is None)
    assert hud.publications == [] and rig.runtime.dispatcher.current_ticket is None


@pytest.mark.parametrize("change", ["generation", "recording", "shutdown"])
async def test_held_publication_rechecks_ownership_before_activation(rig: Rig, change: str) -> None:
    hud = rig.hud
    assert isinstance(hud, ControlledHud)
    release = hud.hold = asyncio.Event()
    pending = rig.launch(rig.message())
    await until(lambda: len(hud.publications) == 1)
    if change == "generation":
        rig.generation = "replacement-session"
    elif change == "recording":
        rig.capturing = True
    else:
        rig.runtime.begin_shutdown()
    release.set()
    await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 1)
    assert hud.publications[0].activated is False
    assert rig.runtime.dispatcher.current_ticket is None and rig.reviewed == []


async def test_duplicate_message_step_never_renews_or_repaints(rig: Rig) -> None:
    hud = rig.hud
    assert isinstance(hud, ControlledHud)
    status = await rig.message()
    ticket = rig.runtime.dispatcher.current_ticket
    assert ticket is not None
    rig.now += 5
    await rig.runtime.after_message_input(status)
    assert rig.runtime.dispatcher.current_ticket is ticket and len(hud.publications) == 1


async def test_late_cancelled_brainstorm_paint_cannot_revoke_message_successor(rig: Rig) -> None:
    hud = rig.hud
    assert isinstance(hud, ControlledHud)
    release = hud.hold = asyncio.Event()
    hud.ignore_cancel = True
    rig.begin_topic()
    await until(lambda: len(hud.publications) == 1)
    old = hud.publications[0]
    hud.hold = None
    hud.ignore_cancel = False
    try:
        await rig.message()
        current = rig.runtime.dispatcher.current_ticket
        assert current is not None and current.owner_kind == "messages"
        release.set()
        await asyncio.wait_for(old.finished.wait(), 1)
        assert old.activated is False
        assert rig.runtime.dispatcher.current_ticket is current and rig.runtime.dispatcher.is_armed
        assert rig.sessions.current_topic_capture is None
    finally:
        release.set()


async def test_shutdown_joins_owned_publication_without_cancelling_foreign_caller(rig: Rig) -> None:
    hud = rig.hud
    assert isinstance(hud, ControlledHud)
    release = hud.hold = asyncio.Event()
    pending = rig.launch(rig.message())
    foreign_release = asyncio.Event()
    foreign = rig.launch(foreign_release.wait())
    try:
        await until(lambda: len(hud.publications) == 1)
        rig.runtime.begin_shutdown()
        assert rig.runtime.dispatcher.current_ticket is None
        await asyncio.wait_for(rig.runtime.aclose(), 1)
        assert hud.publications[0].finished.is_set() and hud.cancelled.is_set()
        assert not foreign.done()
        result = await asyncio.wait_for(pending, 1)
        assert result.state == "awaiting_input"
        assert rig.reviewed == []
    finally:
        release.set()
        foreign_release.set()


@pytest.mark.parametrize("literal", ["stop", "  Stop, open News?!\nKeep BOTH spaces and these exact words.  "])
async def test_terminal_runtime_consumes_the_matching_owner_literally_once(
    terminal_rig: TerminalRig, literal: str,
) -> None:
    fixture = terminal_rig
    hud = fixture.hud
    assert isinstance(hud, ControlledHud)
    capture = fixture.begin_terminal()
    assert await fixture.publish_terminal(capture), "Terminal capture did not publish its question"
    ticket = fixture.runtime.dispatcher.current_ticket
    assert ticket is not None and fixture.runtime.dispatcher.is_armed
    assert (ticket.owner_kind, ticket.owner_id, ticket.field) == (
        "terminal", capture.capture_id, "instruction",
    )
    assert hud.publications[0].question == capture.question
    assert hud.publications[0].activated and fixture.fences == 1
    assert not fixture.terminal.armed, "Typed capture must not arm the command parser"
    assert await fixture.runtime.dispatcher.dispatch(Transcript(literal, True), receipt(ticket))
    assert fixture.terminal.current_capture is None
    assert not await fixture.runtime.dispatcher.dispatch(Transcript(literal, True), receipt(ticket))
    await until(lambda: not fixture.roster.busy)
    assert fixture.work.scoped == [(literal, 1)] and fixture.work.unscoped == []
    assert fixture.other.scoped == [] and fixture.other.unscoped == []
    assert fixture.topics == [] and fixture.reviewed == []
    assert fixture.runtime.dispatcher.current_ticket is None
    await until(lambda: ticket.prompt_id in hud.cancellations)


async def held_terminal_publication(
    fixture: TerminalRig, capture: TerminalCapture,
) -> tuple[asyncio.Task[Any], Publication]:
    hud = fixture.hud
    assert isinstance(hud, ControlledHud) and hud.hold is not None
    count = len(hud.publications)
    pending = fixture.launch(fixture.publish_terminal(capture))
    await until(lambda: len(hud.publications) > count or pending.done())
    assert len(hud.publications) == count + 1, "Terminal capture did not publish its question"
    return pending, hud.publications[-1]


async def test_terminal_runtime_waits_for_the_exact_question_ack(terminal_rig: TerminalRig) -> None:
    fixture = terminal_rig
    hud = fixture.hud
    assert isinstance(hud, ControlledHud)
    release = hud.hold = asyncio.Event()
    capture = fixture.begin_terminal()
    pending, publication = await held_terminal_publication(fixture, capture)
    ticket = publication.ticket
    assert publication.question == capture.question
    assert not fixture.runtime.dispatcher.is_armed
    assert not await fixture.runtime.dispatcher.dispatch(Transcript("too early", True), receipt(ticket))
    matching = PromptPresentationAck(
        prompt_id=ticket.prompt_id, success=True,
        presentation_binding=ticket.presentation_binding,
        connection_generation=ticket.connection_generation,
        audio_epoch=ticket.prompt_id, ingress_fence=10,
    )
    for wrong in (
        replace(matching, prompt_id="wrong-prompt"),
        replace(matching, presentation_binding="wrong-question-binding"),
        replace(matching, connection_generation="wrong-connection"),
        replace(matching, audio_epoch="wrong-audio-epoch"),
    ):
        assert publication.activate(wrong) is False
        assert not fixture.runtime.dispatcher.is_armed
        assert fixture.terminal.current_capture is capture
    assert fixture.work.scoped == [] and fixture.work.unscoped == []
    release.set()
    assert await asyncio.wait_for(pending, 1) is True
    assert publication.activated and fixture.runtime.dispatcher.is_armed


async def test_terminal_runtime_failed_matching_paint_clears_capture(terminal_rig: TerminalRig) -> None:
    fixture = terminal_rig
    hud = fixture.hud
    assert isinstance(hud, ControlledHud)
    release = hud.hold = asyncio.Event()
    capture = fixture.begin_terminal()
    pending, publication = await held_terminal_publication(fixture, capture)
    ticket = publication.ticket
    assert publication.activate(PromptPresentationAck(
        prompt_id=ticket.prompt_id, success=False,
        presentation_binding=ticket.presentation_binding,
        connection_generation=ticket.connection_generation,
        audio_epoch=ticket.prompt_id, ingress_fence=10,
    )) is False
    release.set()
    assert await asyncio.wait_for(pending, 1) is False
    assert fixture.terminal.current_capture is None
    assert fixture.runtime.dispatcher.current_ticket is None
    assert fixture.work.scoped == [] and fixture.work.unscoped == []


@pytest.mark.parametrize("reason", ["transport", "home", "expired", "generation", "owner_invalid"])
async def test_terminal_runtime_retirement_clears_the_exact_owner(
    terminal_rig: TerminalRig, reason: str,
) -> None:
    fixture = terminal_rig
    hud = fixture.hud
    assert isinstance(hud, ControlledHud)
    capture = fixture.begin_terminal()
    assert await fixture.publish_terminal(capture)
    ticket = fixture.runtime.dispatcher.current_ticket
    assert ticket is not None and fixture.runtime.dispatcher.is_armed
    if reason == "transport":
        hud.publications[-1].revoke(ticket.prompt_id)
    elif reason == "home":
        fixture.runtime.revoke_current()
    elif reason == "expired":
        fixture.now = capture.expires_at
    elif reason == "generation":
        fixture.generation = "replacement-transport"
    else:
        fixture.work.capture_context_generation = 2
    assert fixture.runtime.dispatcher.current_ticket is None
    assert fixture.terminal.current_capture is None and not fixture.terminal.armed
    assert not await fixture.runtime.dispatcher.dispatch(Transcript("late answer", True), receipt(ticket))
    assert await fixture.terminal.handle_command("ordinary later conversation") is None
    assert fixture.work.scoped == [] and fixture.work.unscoped == []
    await until(lambda: ticket.prompt_id in hud.cancellations)


async def test_terminal_runtime_replacement_and_late_revoke_preserve_the_successor(
    terminal_rig: TerminalRig,
) -> None:
    fixture = terminal_rig
    hud = fixture.hud
    assert isinstance(hud, ControlledHud)
    old_capture = fixture.begin_terminal()
    assert await fixture.publish_terminal(old_capture)
    old = hud.publications[-1]
    await fixture.message()
    message_ticket = fixture.runtime.dispatcher.current_ticket
    assert message_ticket is not None and message_ticket.owner_kind == "messages"
    assert fixture.terminal.current_capture is None
    fresh_capture = fixture.begin_terminal()
    assert fresh_capture.capture_id != old_capture.capture_id
    assert await fixture.publish_terminal(fresh_capture)
    fresh = fixture.runtime.dispatcher.current_ticket
    assert fresh is not None and fresh.owner_id == fresh_capture.capture_id
    assert fresh.presentation_binding != old.ticket.presentation_binding
    old.revoke(old.ticket.prompt_id)
    assert fixture.runtime.dispatcher.current_ticket is fresh and fixture.runtime.dispatcher.is_armed
    assert fixture.terminal.current_capture is fresh_capture
    assert fixture.work.scoped == [] and fixture.work.unscoped == []
    assert fixture.replies.message_action_status("runtime-message").state == "awaiting_input"


@pytest.mark.parametrize("return_to_work", [False, True])
async def test_terminal_runtime_held_question_cannot_arm_after_focus_changes(
    terminal_rig: TerminalRig, return_to_work: bool,
) -> None:
    fixture = terminal_rig
    hud = fixture.hud
    assert isinstance(hud, ControlledHud)
    release = hud.hold = asyncio.Event()
    capture = fixture.begin_terminal()
    pending, publication = await held_terminal_publication(fixture, capture)
    assert fixture.roster.focus("other")
    if return_to_work:
        assert fixture.roster.focus("work")
    release.set()
    assert await asyncio.wait_for(pending, 1) is False
    assert publication.activated is False
    assert fixture.runtime.dispatcher.current_ticket is None
    assert fixture.terminal.current_capture is None
    assert fixture.work.scoped == [] and fixture.other.scoped == []


async def test_terminal_runtime_late_cancelled_paint_cannot_clear_a_new_capture(
    terminal_rig: TerminalRig,
) -> None:
    fixture = terminal_rig
    hud = fixture.hud
    assert isinstance(hud, ControlledHud)
    release = hud.hold = asyncio.Event()
    hud.ignore_cancel = True
    old_capture = fixture.begin_terminal()
    pending, old = await held_terminal_publication(fixture, old_capture)
    fixture.terminal.clear_capture()
    fresh_capture = fixture.begin_terminal()
    hud.hold = None
    hud.ignore_cancel = False
    try:
        assert await fixture.publish_terminal(fresh_capture)
        current = fixture.runtime.dispatcher.current_ticket
        assert current is not None and current.owner_id == fresh_capture.capture_id
        release.set()
        assert await asyncio.wait_for(pending, 1) is False
        assert old.finished.is_set() and old.activated is False
        assert fixture.runtime.dispatcher.current_ticket is current
        assert fixture.runtime.dispatcher.is_armed and fixture.terminal.current_capture is fresh_capture
        assert fixture.work.scoped == [] and fixture.work.unscoped == []
    finally:
        release.set()


async def test_terminal_runtime_duplicate_publication_keeps_original_deadline(
    terminal_rig: TerminalRig,
) -> None:
    fixture = terminal_rig
    hud = fixture.hud
    assert isinstance(hud, ControlledHud)
    capture = fixture.begin_terminal()
    assert await fixture.publish_terminal(capture)
    ticket = fixture.runtime.dispatcher.current_ticket
    assert ticket is not None and ticket.expires_at == capture.expires_at
    fixture.now += 5
    assert fixture.begin_terminal() is capture
    assert await fixture.publish_terminal(capture) is False
    assert fixture.runtime.dispatcher.current_ticket is ticket
    assert len(hud.publications) == 1 and fixture.fences == 1
    fixture.now = capture.expires_at
    assert fixture.runtime.dispatcher.current_ticket is None and fixture.terminal.current_capture is None


@pytest.mark.parametrize("blocked", ["closed", "recording", "no_generation"])
async def test_terminal_runtime_unavailable_never_publishes_a_question(
    terminal_rig: TerminalRig, blocked: str,
) -> None:
    fixture = terminal_rig
    hud = fixture.hud
    assert isinstance(hud, ControlledHud)
    capture = fixture.begin_terminal()
    if blocked == "closed":
        fixture.runtime.begin_shutdown()
    elif blocked == "recording":
        fixture.capturing = True
    else:
        fixture.generation = None
    assert await fixture.publish_terminal(capture) is False
    assert hud.publications == [] and fixture.fences == 0
    assert fixture.runtime.dispatcher.current_ticket is None
    assert fixture.terminal.current_capture is None and not fixture.terminal.armed
    assert fixture.work.scoped == [] and fixture.work.unscoped == []


@pytest.mark.parametrize("event", ["home", "shutdown"])
async def test_terminal_runtime_revokes_capture_before_publication_starts(
    terminal_rig: TerminalRig, event: str,
) -> None:
    """The tap owns capture synchronously, before its publication coroutine runs."""
    fixture = terminal_rig
    hud = fixture.hud
    assert isinstance(hud, ControlledHud)
    capture = fixture.begin_terminal()
    assert fixture.runtime.dispatcher.current_ticket is None
    if event == "home":
        fixture.runtime.revoke_current()
    else:
        fixture.runtime.begin_shutdown()
    assert fixture.terminal.current_capture is None
    assert await fixture.publish_terminal(capture) is False
    assert hud.publications == [] and fixture.fences == 0
    assert fixture.work.scoped == [] and fixture.work.unscoped == []


@pytest.mark.parametrize("event", ["recording", "shutdown"])
async def test_prepublication_brainstorm_capture_is_retired_before_publication(event: str) -> None:
    """A real SessionManager tap creates ownership before its task can run."""
    class FrameSource(AudioFrameSource):
        def on_audio_frame(self, callback: AudioFrameCallback) -> None:
            self.callback = callback

    audio = AudioRouter(
        FrameSource(), on_capture_started=lambda: fixture.runtime.revoke_current(),
    )
    fixture = Rig(capturing=lambda: audio.capturing)
    hud = fixture.hud
    assert isinstance(hud, ControlledHud)
    try:
        capture = fixture.begin_topic()
        assert fixture.sessions.current_topic_capture is capture
        assert fixture.runtime.dispatcher.current_ticket is None
        assert hud.publications == []
        # No await between the tap and lifecycle change: its publication task
        # has not run, so there is no dispatcher ticket to retire yet.
        if event == "recording":
            recorded: list[AudioFrame] = []
            receive = recorded.append
            audio.begin_capture(receive, exclusive=True)
            audio.end_capture(receive)
            assert audio.capturing is False
        else:
            fixture.runtime.begin_shutdown()
        assert fixture.sessions.current_topic_capture is None
        # The old caller can still finish; it must not recreate its question
        # after recording ends or publish while shutdown is in progress.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert hud.publications == [] and fixture.fences == 0
        assert fixture.runtime.dispatcher.current_ticket is None
        assert fixture.topics == [] and fixture.reviewed == []
    finally:
        await fixture.aclose()


class Stt:
    def __init__(self, texts: list[str]) -> None:
        self.texts = deque(texts)
        self.calls = 0
        self.reset_calls = 0

    async def feed(self, pcm: bytes) -> None: ...
    async def flush(self) -> Transcript:
        self.calls += 1
        return Transcript(self.texts.popleft(), True)
    def reset(self) -> None:
        self.reset_calls += 1


@pytest.mark.parametrize("owner", ["messages", "brainstorm", "terminal"])
async def test_recording_start_retires_prompt_before_held_completed_stt_returns(owner: str) -> None:
    """A short recording cannot restore an earlier answer lease after it ends.

    The old PCM was already a complete utterance and must finish STT normally.
    Capture start retires its owner admission, even when no one polls prompt
    validity while the exclusive route is held. No transcript is sent anywhere.
    """
    class FrameTransport(Transport, AudioFrameSource):
        callback: AudioFrameCallback | None = None

        def on_audio_frame(self, callback: AudioFrameCallback) -> None:
            self.callback = callback

        def utterance(self, ticket: PromptTicket) -> None:
            assert self.callback is not None
            for seq, amplitude in enumerate((8000, 8000, 0, 0)):
                self.callback(AudioFrame(
                    pcm=struct.pack("<3200h", *([amplitude, -amplitude] * 1600)),
                    connection_generation=ticket.connection_generation,
                    seq=seq, source="glasses", speaker=None, received_at_us=0,
                    audio_epoch=ticket.prompt_id,
                ))

    class HeldStt(Stt):
        def __init__(self) -> None:
            super().__init__(["Keep this answer with its original question."])
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def feed(self, pcm: bytes) -> None:
            self.started.set()
            await self.release.wait()

    transport = FrameTransport()
    audio = AudioRouter(
        transport, on_capture_started=lambda: fixture.runtime.revoke_current(),
    )
    fixture = (TerminalRig if owner == "terminal" else Rig)(capturing=lambda: audio.capturing)
    hud = fixture.hud
    assert isinstance(hud, ControlledHud)
    stt = HeldStt()
    voice = VoiceOrchestrator(
        transport, stt, frames=audio, sessions=fixture.sessions,
        vad=RmsVad(adaptive=False, start_batches=2, hangover_batches=2),
        wake_words=("joat",), prompted_input=fixture.runtime.dispatcher,
    )
    fixture.fence = voice.fence_prompt_audio
    voice.attach()
    try:
        if owner == "messages":
            await fixture.message()
        elif owner == "terminal":
            assert isinstance(fixture, TerminalRig)
            assert await fixture.publish_terminal(fixture.begin_terminal())
        else:
            fixture.begin_topic()
            await until(lambda: fixture.runtime.dispatcher.is_armed)
        ticket = fixture.runtime.dispatcher.current_ticket
        assert ticket is not None and fixture.runtime.dispatcher.is_armed
        transport.utterance(ticket)
        await asyncio.wait_for(stt.started.wait(), 1)
        worker = voice._utterance_worker
        assert worker is not None and not worker.done()

        captured: list[AudioFrame] = []
        capture = captured.append
        audio.begin_capture(capture, exclusive=True)
        audio.end_capture(capture)
        # Deliberately do not access dispatcher state until after recording
        # ends and the completed utterance returns from the real voice worker.
        assert audio.capturing is False
        stt.release.set()
        await asyncio.wait_for(asyncio.shield(worker), 1)

        assert stt.calls == 1, "Recording must not cancel completed STT"
        assert fixture.reviewed == [] and fixture.topics == [], (
            "A retired question must not consume the completed pre-recording answer"
        )
        assert fixture.runtime.dispatcher.current_ticket is None
        assert fixture.sessions.current_topic_capture is None
        assert len(hud.publications) == 1, "Recording end must not republish the old question"
        await until(lambda: ticket.prompt_id in hud.cancellations)
        if owner == "messages":
            assert fixture.replies.message_action_status("runtime-message").state == "awaiting_input"
        elif owner == "terminal":
            assert isinstance(fixture, TerminalRig)
            assert fixture.terminal.current_capture is None and not fixture.terminal.armed
            assert fixture.work.scoped == [] and fixture.work.unscoped == []
    finally:
        stt.release.set()
        worker = voice._utterance_worker
        if worker is not None:
            worker.cancel()
            await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 1)
        await fixture.aclose()


async def read_question(socket: Any) -> dict[str, Any]:
    async with asyncio.timeout(1):
        while True:
            message = json.loads(await socket.recv())
            if message["kind"] == "prompt.present":
                return message


async def acknowledge(socket: Any, question: dict[str, Any]) -> None:
    await socket.send(json.dumps({
        "kind": "prompt.ack", "promptId": question["promptId"],
        "presentationBinding": question["presentationBinding"],
        "connectionGeneration": question["connectionGeneration"],
        "source": question["source"], "success": True, "audioEpoch": question["promptId"],
    }))


@pytest.mark.parametrize("stop", ["manual", "matching", "not_worn"])
async def test_scoped_mic_stop_preserves_the_successor_at_gateway_and_runtime(
    unused_tcp_port: int, stop: str,
) -> None:
    gateway = HubGatewayTransport(host="127.0.0.1", port=unused_tcp_port)
    fixture = Rig(hud=HudWriter(gateway), generation=lambda: gateway.audio_connection_generation)
    devices: list[Device] = []
    gateway.on_device(fixture.runtime.handle_device)
    gateway.on_device(devices.append)
    await gateway.start()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{unused_tcp_port}") as socket:
            await socket.send(json.dumps({"kind": "auth", "token": None}))
            assert json.loads(await socket.recv())["kind"] == "auth.accepted"
            preparation = fixture.launch(fixture.message())
            old = await read_question(socket)
            await acknowledge(socket, old)
            await asyncio.wait_for(preparation, 1)
            fixture.begin_topic()
            successor = await read_question(socket)
            assert successor["promptId"] != old["promptId"]
            # The Hub releases A after observing B. Its real mic is off, but
            # that old release must not revoke B's pending paint/owner.
            await socket.send(json.dumps({"kind": "device", "connect": "connected",
                                          "mic": False, "micSource": old["source"],
                                          "micPromptId": old["promptId"]}))
            await until(lambda: len(devices) == 1)
            ticket = fixture.runtime.dispatcher.current_ticket
            assert ticket is not None and ticket.prompt_id == successor["promptId"]
            assert not fixture.runtime.dispatcher.is_armed
            assert gateway.device is not None and gateway.device.mic is False
            await acknowledge(socket, successor)
            await until(lambda: fixture.runtime.dispatcher.is_armed)
            assert fixture.runtime.dispatcher.current_ticket is ticket
            ending: dict[str, Any] = {"kind": "device", "connect": "connected", "mic": False}
            if stop == "matching":
                ending["micPromptId"] = successor["promptId"]
            elif stop == "not_worn":
                ending.update(wearing=False, micPromptId=old["promptId"])
            await socket.send(json.dumps(ending))
            await until(lambda: len(devices) == 2)
            assert fixture.runtime.dispatcher.current_ticket is None
            assert fixture.sessions.current_topic_capture is None
            assert not fixture.runtime.dispatcher.is_armed
    finally:
        await fixture.aclose()
        await gateway.stop()


@pytest.mark.parametrize("owner", ["brainstorm", "terminal"])
async def test_scoped_mic_stop_cannot_clear_an_unpublished_successor(owner: str) -> None:
    fixture = TerminalRig()
    try:
        fixture.begin_topic()
        await until(lambda: fixture.runtime.dispatcher.is_armed)
        previous = fixture.runtime.dispatcher.current_ticket
        assert previous is not None
        fixture.runtime.revoke_current()
        capture = fixture.begin_topic() if owner == "brainstorm" else fixture.begin_terminal()
        # No await: the wearer already bound this owner, but it has no ticket.
        assert fixture.runtime.dispatcher.current_ticket is None
        fixture.runtime.handle_device(Device(
            connect="connected", mic=False, mic_prompt_id=previous.prompt_id,
        ))
        current = (fixture.sessions.current_topic_capture if owner == "brainstorm"
                   else fixture.terminal.current_capture)
        assert current is capture
        fixture.runtime.handle_device(Device(connect="connected", mic=False))
        assert fixture.sessions.current_topic_capture is None
        assert fixture.terminal.current_capture is None
    finally:
        await fixture.aclose()


@pytest.mark.parametrize("owner", ["messages", "brainstorm"])
async def test_real_runtime_activates_inside_ack_before_immediately_following_pcm(
    tmp_path: Path, unused_tcp_port: int, owner: str,
) -> None:
    pairing = PairingStore(tmp_path / "pairing.json")
    token = PairingStore.mint()
    pairing.confirm(token)
    gateway = HubGatewayTransport(
        host="127.0.0.1", port=unused_tcp_port, pairing=pairing, trust_loopback=False,
    )
    fixture = Rig(hud=HudWriter(gateway), generation=lambda: gateway.audio_connection_generation)
    literal = "  Stop?!\nOpen News, but keep these words literal.  "
    stt = Stt(["Sam", literal] if owner == "messages" else [literal])
    commands: list[str] = []
    voice = VoiceOrchestrator(
        gateway, stt, frames=AudioRouter(gateway), sessions=fixture.sessions,
        vad=RmsVad(adaptive=False, start_batches=2, hangover_batches=2),
        wake_words=("joat",), prompted_input=fixture.runtime.dispatcher,
        commands=lambda text: commands.append(text),
    )
    fixture.fence = voice.fence_prompt_audio
    voice.attach()
    await gateway.start()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{unused_tcp_port}") as socket:
            await socket.send(json.dumps({"kind": "auth", "token": token}))
            assert json.loads(await socket.recv())["kind"] == "auth.accepted"
            if owner == "messages":
                preparation = fixture.launch(fixture.message(recipient="my wife"))
                fields = ("recipient", "body")
            else:
                fixture.begin_topic()
                preparation = None
                fields = ("topic",)
            seq = 0
            for field in fields:
                question = await read_question(socket)
                assert question["ownerKind"] == owner and question["field"] == field
                assert not fixture.runtime.dispatcher.is_armed
                await acknowledge(socket, question)
                # No yield/barrier after receipt beyond ordered socket sends.
                for amplitude in (8000, 8000, 0, 0):
                    await socket.send(json.dumps({
                        "kind": "audio", "source": "glasses", "seq": seq,
                        "pcm": [amplitude, -amplitude] * 1600,
                        "audioEpoch": question["promptId"],
                    }))
                    seq += 1
            if preparation is not None:
                await asyncio.wait_for(preparation, 1)
                await until(lambda: bool(fixture.reviewed))
                assert fixture.reviewed == [f"Send to Sam? “{literal}”"]
            else:
                await until(lambda: bool(fixture.topics))
                assert fixture.topics == [literal] and fixture.factory_owners == [None]
            assert commands == [] and stt.calls == len(fields)
            assert fixture.runtime.dispatcher.current_ticket is None
    finally:
        worker = voice._utterance_worker
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        await fixture.aclose()
        await gateway.stop()


@pytest.mark.parametrize("event", ["back", "mute", "expiry", "disconnect"])
async def test_authenticated_lifecycle_revokes_brainstorm_and_allows_a_new_capture(
    tmp_path: Path, unused_tcp_port: int, event: str,
) -> None:
    pairing = PairingStore(tmp_path / "pairing.json")
    token = PairingStore.mint()
    pairing.confirm(token)
    gateway = HubGatewayTransport(
        host="127.0.0.1", port=unused_tcp_port, pairing=pairing, trust_loopback=False,
    )
    fixture = Rig(hud=HudWriter(gateway), generation=lambda: gateway.audio_connection_generation)
    await gateway.start()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{unused_tcp_port}") as socket:
            await socket.send(json.dumps({"kind": "auth", "token": token}))
            assert json.loads(await socket.recv())["kind"] == "auth.accepted"
            capture = fixture.begin_topic()
            question = await read_question(socket)
            await acknowledge(socket, question)
            await until(lambda: fixture.runtime.dispatcher.is_armed)
            if event == "back":
                await socket.send(json.dumps({
                    "kind": "prompt.cancel", "promptId": question["promptId"],
                    "connectionGeneration": question["connectionGeneration"],
                }))
            elif event == "mute":
                await socket.send(json.dumps({"kind": "device", "mic": False}))
            elif event == "expiry":
                fixture.now = capture.expires_at
            else:
                await socket.close()
            await until(lambda: fixture.runtime.dispatcher.current_ticket is None)
            assert fixture.sessions.current_topic_capture is None and fixture.topics == []
            if event in ("back", "mute"):
                # The new matching presentation receipt is authoritative even
                # if gateway.device retains an earlier mic=False observation.
                fresh = fixture.begin_topic()
                assert fresh.capture_id != capture.capture_id
                next_question = await read_question(socket)
                await acknowledge(socket, next_question)
                await until(lambda: fixture.runtime.dispatcher.is_armed)
                assert fixture.runtime.dispatcher.current_ticket.owner_id == fresh.capture_id
    finally:
        await fixture.aclose()
        await gateway.stop()
