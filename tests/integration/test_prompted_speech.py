"""Fresh prompted PCM reaches its typed owner through the real voice pipeline.

The transport and final STT output are controlled. AudioRouter, RmsVad,
VoiceOrchestrator and message preparation are real. These cases establish
host routing and rejection, not microphone quality or optical timing.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
import struct
import json
from pathlib import Path
from typing import Any

import pytest
import websockets

from joat.audio.router import AudioRouter
from joat.agents.message_tools import message_tools
from joat.audio.vad import RmsVad
from joat.ble.hub_gateway import HubGatewayTransport
from joat.core.pairing import PairingStore
from joat.core.interfaces import (
    AudioCallback, AudioFrame, AudioFrameCallback, AudioFrameSource,
    DeviceCallback, GlassesTransport, InputCallback, ReplyEngine, SttEngine,
    Transcript,
)
from joat.core.protocol import AudioSource, DownlinkMessage
from joat.services.message_reply import MessageReplyController
from joat.services.hud import HudWriter
from joat.services.message_prompts import ActivatePrompt, MessagePromptBridge, PromptPresentationAck
from joat.services.orchestrator import VoiceOrchestrator
from joat.services.prompted_input import PromptedInputDispatcher, PromptTicket


def pcm(amplitude: int) -> bytes:
    return struct.pack("<3200h", *([amplitude, -amplitude] * 1600))


LOUD = pcm(8000)
QUIET = pcm(0)


class Transport(GlassesTransport, AudioFrameSource):
    def __init__(self) -> None:
        self.callback: AudioFrameCallback | None = None
        self.sent: list[DownlinkMessage] = []
        self.seq = 0

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(self, message: DownlinkMessage) -> None:
        self.sent.append(message)

    @property
    def connected(self) -> bool:
        return True

    def on_audio(self, callback: AudioCallback) -> None:
        raise AssertionError("Conversation must use metadata-preserving audio")

    def on_audio_frame(self, callback: AudioFrameCallback) -> None:
        self.callback = callback

    def on_input(self, callback: InputCallback) -> None:
        pass

    def on_device(self, callback: DeviceCallback) -> None:
        pass

    def emit(
        self, data: bytes, *, epoch: str | None,
        source: AudioSource = "glasses",
        generation: str = "authenticated-session", speaker: str | None = None,
    ) -> None:
        assert self.callback is not None
        self.callback(AudioFrame(
            pcm=data, connection_generation=generation, seq=self.seq,
            source=source, speaker=speaker, received_at_us=0, audio_epoch=epoch,
        ))
        self.seq += 1

    def utterance(self, **provenance: Any) -> None:
        for data in (LOUD, LOUD, QUIET, QUIET):
            self.emit(data, **provenance)


class Stt(SttEngine):
    def __init__(self, *texts: str) -> None:
        self.texts = deque(texts)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.calls = 0
        self.reset_calls = 0

    async def feed(self, data: bytes) -> Transcript | None:
        self.started.set()
        await self.release.wait()
        return None

    async def flush(self) -> Transcript | None:
        self.calls += 1
        return Transcript(text=self.texts.popleft(), is_final=True)

    def reset(self) -> None:
        self.reset_calls += 1


class Replier(ReplyEngine):
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def reply(self, text: str) -> str | None:
        self.queries.append(text)
        self.started.set()
        await self.release.wait()
        return "ordinary answer"


async def until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


class Journey:
    def __init__(self, *texts: str) -> None:
        self.transport = Transport()
        self.audio = AudioRouter(self.transport)
        self.stt = Stt(*texts)
        self.replier = Replier()
        self.dispatcher = PromptedInputDispatcher()
        self.command_calls: list[str] = []
        self.received: list[str] = []
        self.valid = True

        async def commands(text: str) -> str | None:
            self.command_calls.append(text)
            return None

        self.voice = VoiceOrchestrator(
            self.transport, self.stt,
            vad=RmsVad(adaptive=False, start_batches=2, hangover_batches=2),
            frames=self.audio, wake_words=("joat",), commands=commands,
            replier=self.replier, prompted_input=self.dispatcher,
        )
        self.voice.attach()

    def arm(self, consumer: Any = None) -> PromptTicket:
        async def receive(text: str) -> None:
            self.received.append(text)
            if consumer is not None:
                await consumer(text)

        ticket = self.dispatcher.begin_publication(
            owner_kind="messages", owner_id="message-1", field="body",
            source="glasses", connection_generation="authenticated-session",
            presentation_binding="fixture-body-question", ttl_s=45,
            is_valid=lambda: self.valid, consume=receive,
        )
        # The controlled presenter explicitly succeeds. Production must obtain
        # this acknowledgment from the plugin before calling the same fence.
        assert self.dispatcher.acknowledge(
            ticket.prompt_id, success=True,
            presentation_binding=ticket.presentation_binding,
            connection_generation=ticket.connection_generation,
            audio_epoch=ticket.prompt_id,
            ingress_fence=self.voice.fence_prompt_audio(),
        )
        return ticket

    async def idle(self) -> None:
        await until(lambda: self.voice._utterance_worker is None)

    async def aclose(self) -> None:
        self.stt.release.set()
        self.replier.release.set()
        worker = self.voice._utterance_worker
        if worker is not None:
            worker.cancel()
            await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 1)


@pytest.mark.parametrize("text", ["Good morning", "stop", "open Messages", "  Keep both spaces.  "])
async def test_fresh_spoken_body_reaches_exact_message_review(text: str) -> None:
    journey = Journey(text)
    reviewed: list[str] = []
    sends: list[tuple[str, str]] = []
    hold_confirmation = asyncio.Event()

    class Sender:
        async def send(self, recipient: str, body: str) -> None:
            sends.append((recipient, body))

    async def confirm(_action: str, prompt: str, _timeout: float, **_kw: Any) -> bool:
        reviewed.append(prompt)
        await hold_confirmation.wait()
        return False

    replies = MessageReplyController(
        Sender(), confirm, contact_search=lambda _name: [("Dele", "fixture-address")],
    )
    try:
        status = await replies.prepare_new_message(
            action_id="message-1", trace_id="fixture", recipient_name="Dele",
        )
        assert status.needs_input == "body"

        async def consume(body: str) -> None:
            await replies.provide_message_input("message-1", body=body)

        ticket = journey.arm(consume)
        journey.transport.utterance(epoch=ticket.prompt_id)
        await journey.idle()
        await until(lambda: bool(reviewed))
        assert journey.received == [text]
        assert reviewed == [f"Send to Dele? “{text}”"]
        assert replies.message_action_status("message-1").state == "awaiting_confirmation"
        assert sends == []
        assert journey.command_calls == journey.replier.queries == []
    finally:
        hold_confirmation.set()
        await replies.aclose()
        await journey.aclose()


async def test_old_inflight_and_pending_transcripts_cannot_fill_a_new_prompt() -> None:
    journey = Journey("old active", "old pending", "fresh body")
    try:
        journey.stt.release.clear()
        journey.transport.utterance(epoch=None)
        await asyncio.wait_for(journey.stt.started.wait(), 1)
        journey.transport.utterance(epoch=None)
        resets_before = journey.stt.reset_calls
        ticket = journey.arm()
        assert journey.stt.reset_calls == resets_before
        journey.stt.release.set()
        await journey.idle()
        assert journey.stt.calls == 2 and journey.received == []
        assert journey.dispatcher.is_armed
        journey.transport.utterance(epoch=ticket.prompt_id)
        await journey.idle()
        assert journey.received == ["fresh body"]
    finally:
        await journey.aclose()


async def test_old_addressed_pending_audio_cannot_overlay_a_new_input_question() -> None:
    from joat.core.protocol import AssistantOverlay

    journey = Journey("old active", "JOAT tell me a joke")
    try:
        journey.stt.release.clear()
        journey.transport.utterance(epoch=None)
        await asyncio.wait_for(journey.stt.started.wait(), 1)
        journey.transport.utterance(epoch=None)
        ticket = journey.arm()
        marker = len(journey.transport.sent)

        journey.stt.release.set()
        await journey.idle()

        assert journey.stt.calls == 2
        assert journey.received == []
        assert journey.dispatcher.current_ticket is ticket
        assert journey.dispatcher.is_armed
        assert not any(
            isinstance(message, AssistantOverlay)
            for message in journey.transport.sent[marker:]
        )
    finally:
        await journey.aclose()


@pytest.mark.parametrize("started_batches", [1, 2])
async def test_speech_started_before_prompt_is_discarded_through_quiet(started_batches: int) -> None:
    journey = Journey("fresh body")
    for _ in range(started_batches):
        journey.transport.emit(LOUD, epoch=None)
    ticket = journey.arm()
    for data in (LOUD, LOUD, QUIET, QUIET):
        journey.transport.emit(data, epoch=ticket.prompt_id)
    await journey.idle()
    assert journey.stt.calls == 0 and journey.received == []
    journey.transport.utterance(epoch=ticket.prompt_id)
    await journey.idle()
    assert journey.received == ["fresh body"]


async def test_maximum_duration_cut_is_not_a_fresh_speech_boundary() -> None:
    journey = Journey("old completed segment", "fresh body")
    try:
        journey.voice._vad.max_utterance_s = .4
        journey.stt.release.clear()
        journey.transport.emit(LOUD, epoch=None)
        journey.transport.emit(LOUD, epoch=None)
        await asyncio.wait_for(journey.stt.started.wait(), 1)
        ticket = journey.arm()
        # The same ongoing speech must not become an answer merely because
        # VAD already emitted one maximum-duration segment before the prompt.
        for _ in range(6):
            journey.transport.emit(LOUD, epoch=ticket.prompt_id)
        journey.transport.emit(QUIET, epoch=ticket.prompt_id)
        journey.transport.emit(QUIET, epoch=ticket.prompt_id)
        journey.stt.release.set()
        await journey.idle()
        assert journey.stt.calls == 1 and journey.received == []
        journey.voice._vad.max_utterance_s = 30
        journey.transport.utterance(epoch=ticket.prompt_id)
        await journey.idle()
        assert journey.received == ["fresh body"]
    finally:
        await journey.aclose()


@pytest.mark.parametrize("wrong", [
    {"source": "phone"},
    {"epoch": None},
    {"epoch": "old-prompt"},
    {"speaker": "other"},
])
async def test_wrong_provenance_does_not_spend_a_live_prompt(wrong: dict[str, Any]) -> None:
    # Explicit other-speaker frames are removed before STT by existing policy.
    texts = ("fresh body",) if wrong.get("speaker") == "other" else ("wrong body", "fresh body")
    journey = Journey(*texts)
    ticket = journey.arm()
    journey.transport.utterance(**({"epoch": ticket.prompt_id} | wrong))
    await journey.idle()
    assert journey.received == [] and journey.dispatcher.is_armed
    journey.transport.utterance(epoch=ticket.prompt_id)
    await journey.idle()
    assert journey.received == ["fresh body"]


async def test_prompt_invalidated_while_stt_runs_does_not_use_late_text() -> None:
    journey = Journey("late body")
    try:
        ticket = journey.arm()
        journey.stt.release.clear()
        journey.transport.utterance(epoch=ticket.prompt_id)
        await asyncio.wait_for(journey.stt.started.wait(), 1)
        journey.valid = False
        journey.stt.release.set()
        await journey.idle()
        assert journey.received == []
        assert journey.command_calls == journey.replier.queries == []
    finally:
        await journey.aclose()


async def test_prompt_owner_exception_does_not_log_literal_input_or_retry_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    literal = "private-prompted-message-body"
    journey = Journey(literal, "JOAT tell me a joke")

    async def failing_owner(text: str) -> None:
        raise RuntimeError(f"Sensitive owner failure: {text}")

    try:
        ticket = journey.arm(failing_owner)
        journey.transport.utterance(epoch=ticket.prompt_id)
        await journey.idle()

        assert journey.received == [literal]
        assert journey.dispatcher.current_ticket is None
        assert journey.command_calls == journey.replier.queries == []
        assert literal not in caplog.text
        assert "Sensitive owner failure" not in caplog.text

        journey.transport.utterance(epoch=None)
        await journey.idle()
        assert journey.stt.calls == 2
        assert journey.received == [literal]
        assert journey.command_calls == journey.replier.queries == ["tell me a joke"]
    finally:
        await journey.aclose()


async def test_older_model_answer_cannot_overwrite_a_new_input_question() -> None:
    from joat.core.protocol import AssistantOverlay

    journey = Journey("JOAT tell me a joke")
    try:
        journey.replier.release.clear()
        journey.transport.utterance(epoch=None)
        await asyncio.wait_for(journey.replier.started.wait(), 1)
        journey.arm()
        marker = len(journey.transport.sent)
        journey.replier.release.set()
        await journey.idle()
        assert not any(isinstance(message, AssistantOverlay)
                       for message in journey.transport.sent[marker:])
        assert journey.dispatcher.is_armed
    finally:
        await journey.aclose()


async def test_prompt_published_during_command_context_send_blocks_the_old_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from joat.core.protocol import AssistantOverlay, ContainerId, HudText

    journey = Journey("JOAT open News")
    entered, release = asyncio.Event(), asyncio.Event()
    original_send = journey.transport.send

    async def command(_query: str) -> str:
        return "News opened."

    async def send(message: DownlinkMessage) -> None:
        await original_send(message)
        if isinstance(message, HudText) and message.container_id == ContainerId.CONTEXT:
            entered.set()
            await release.wait()

    monkeypatch.setattr(journey.voice, "_commands", command)
    monkeypatch.setattr(journey.transport, "send", send)
    try:
        journey.transport.utterance(epoch=None)
        await asyncio.wait_for(entered.wait(), 1)
        ticket = journey.arm()
        marker = len(journey.transport.sent)
        release.set()
        await journey.idle()

        assert journey.dispatcher.current_ticket is ticket and journey.dispatcher.is_armed
        assert not any(
            isinstance(message, AssistantOverlay)
            for message in journey.transport.sent[marker:]
        )
    finally:
        release.set()
        await journey.aclose()


async def test_prompt_published_during_wake_answer_send_blocks_the_old_settle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from joat.core.protocol import AssistantOverlay

    journey = Journey("JOAT")
    entered, release = asyncio.Event(), asyncio.Event()
    original_send = journey.transport.send

    async def send(message: DownlinkMessage) -> None:
        await original_send(message)
        if isinstance(message, AssistantOverlay) and message.operation == "replace":
            entered.set()
            await release.wait()

    monkeypatch.setattr(journey.transport, "send", send)
    try:
        journey.transport.utterance(epoch=None)
        await asyncio.wait_for(entered.wait(), 1)
        ticket = journey.arm()
        marker = len(journey.transport.sent)
        release.set()
        await journey.idle()

        assert journey.dispatcher.current_ticket is ticket and journey.dispatcher.is_armed
        assert not any(
            isinstance(message, AssistantOverlay) and message.operation == "settle"
            for message in journey.transport.sent[marker:]
        )
    finally:
        release.set()
        await journey.aclose()


async def test_without_a_prompt_addressed_speech_still_uses_the_assistant() -> None:
    journey = Journey("JOAT tell me a joke")
    journey.transport.utterance(epoch=None)
    await journey.idle()
    assert journey.command_calls == journey.replier.queries == ["tell me a joke"]


async def test_exclusive_recording_never_feeds_prompted_conversation() -> None:
    journey = Journey("fresh body")
    ticket = journey.arm()
    captured: list[AudioFrame] = []
    capture = captured.append
    journey.audio.begin_capture(capture, exclusive=True)
    journey.transport.utterance(epoch=ticket.prompt_id)
    await journey.idle()
    assert len(captured) == 4 and journey.stt.calls == 0
    assert journey.received == []
    journey.audio.end_capture(capture)
    journey.transport.utterance(epoch=ticket.prompt_id)
    await journey.idle()
    assert journey.received == ["fresh body"]


async def test_message_tool_and_bridge_accept_two_fresh_spoken_answers() -> None:
    """Typed tool to visible-question receipt to PCM to exact controller review."""
    body = "  I'll be home at six.  "
    journey = Journey("Dele", body)
    questions: list[tuple[PromptTicket, str]] = []
    reviewed: list[str] = []
    confirmation_release = asyncio.Event()

    class Sender:
        async def send(self, recipient: str, text: str) -> None:
            raise AssertionError("No message may be sent by this refused fixture")

    async def confirm(_action: str, prompt: str, _timeout: float, **_kw: Any) -> bool:
        reviewed.append(prompt)
        await confirmation_release.wait()
        return False

    replies = MessageReplyController(
        Sender(), confirm,
        contact_search=lambda name: [("Dele", "fixture-address")] if name == "Dele" else [],
    )

    async def publish(ticket: PromptTicket, question: str, activate: ActivatePrompt) -> bool:
        questions.append((ticket, question))
        # This controlled presenter receipt is a host integration fixture.
        # The production plugin must supply its own matching successful ack.
        return activate(PromptPresentationAck(
            prompt_id=ticket.prompt_id, success=True,
            presentation_binding=ticket.presentation_binding,
            connection_generation="authenticated-session", audio_epoch=ticket.prompt_id,
            ingress_fence=journey.voice.fence_prompt_audio(),
        ))

    bridge = MessagePromptBridge(
        replies, journey.dispatcher, publish=publish,
        connection_generation=lambda: "authenticated-session",
    )
    tools = {tool.name: tool for tool in message_tools(replies, after_input=bridge.after_input)}
    try:
        initial = json.loads(await tools["prepare_message"].run({
            "mode": "new", "action_id": "spoken-message", "recipient_name": "my wife",
        }))
        assert initial["needs_input"] == "recipient" and len(questions) == 1
        recipient_question = questions[0][0]
        assert journey.dispatcher.is_armed
        journey.transport.utterance(epoch=recipient_question.prompt_id)
        await journey.idle()
        assert len(questions) == 2
        body_question = questions[1][0]
        assert body_question.prompt_id != recipient_question.prompt_id
        assert body_question.field == "body" and journey.dispatcher.is_armed
        journey.transport.utterance(epoch=body_question.prompt_id)
        await journey.idle()
        await until(lambda: bool(reviewed))
        assert reviewed == [f"Send to Dele? “{body}”"]
        assert journey.command_calls == journey.replier.queries == []
        assert replies.message_action_status("spoken-message").state == "awaiting_confirmation"
        assert len(questions) == 2 and not journey.dispatcher.is_armed
    finally:
        confirmation_release.set()
        await replies.aclose()
        await journey.aclose()


async def test_authenticated_gateway_questions_accept_immediately_following_pcm(
    tmp_path: Path, unused_tcp_port: int,
) -> None:
    """The production HUD publisher activates before next real socket PCM."""
    pairing = PairingStore(tmp_path / "isolated-pairing.json")
    token = PairingStore.mint()
    pairing.confirm(token)
    gateway = HubGatewayTransport(
        host="127.0.0.1", port=unused_tcp_port, pairing=pairing, trust_loopback=False,
    )
    await gateway.start()
    dispatcher = PromptedInputDispatcher()
    stt = Stt("Dele", "  stop, open Messages, keep these words.  ")
    replier = Replier()
    command_calls: list[str] = []
    reviewed: list[str] = []
    release_review = asyncio.Event()
    voice = VoiceOrchestrator(
        gateway, stt, frames=AudioRouter(gateway),
        vad=RmsVad(adaptive=False, start_batches=2, hangover_batches=2),
        wake_words=("joat",), replier=replier, prompted_input=dispatcher,
        commands=lambda text: command_calls.append(text),
    )
    voice.attach()
    hud = HudWriter(gateway)

    class Sender:
        async def send(self, _recipient: str, _body: str) -> None:
            raise AssertionError("Refused fixture must never send")

    async def confirm(_action: str, prompt: str, _timeout: float, **_kw: Any) -> bool:
        reviewed.append(prompt)
        await release_review.wait()
        return False

    replies = MessageReplyController(
        Sender(), confirm,
        contact_search=lambda name: [("Dele", "fixture-address")] if name == "Dele" else [],
    )

    async def publish(ticket: PromptTicket, question: str, activate: ActivatePrompt) -> bool:
        return await hud.publish_prompt(
            ticket, question, activate,
            fence_audio=voice.fence_prompt_audio, revoke=dispatcher.revoke,
        )

    bridge = MessagePromptBridge(
        replies, dispatcher, publish=publish,
        connection_generation=lambda: gateway.audio_connection_generation,
    )
    tool_map = {tool.name: tool for tool in message_tools(replies, after_input=bridge.after_input)}
    preparation: asyncio.Task[str] | None = None
    try:
        async with websockets.connect(f"ws://127.0.0.1:{unused_tcp_port}") as socket:
            await socket.send(json.dumps({"kind": "auth", "token": token}))
            assert json.loads(await socket.recv())["kind"] == "auth.accepted"
            preparation = asyncio.create_task(tool_map["prepare_message"].run({
                "mode": "new", "action_id": "gateway-spoken-message", "recipient_name": "my wife",
            }))
            seq = 0
            ids: list[str] = []
            for expected_field in ("recipient", "body"):
                async with asyncio.timeout(1):
                    while True:
                        question = json.loads(await socket.recv())
                        if question["kind"] == "prompt.present":
                            break
                assert question["field"] == expected_field
                assert question["ownerId"] == "gateway-spoken-message"
                assert question["question"].strip()
                ids.append(question["promptId"])
                assert not dispatcher.is_armed
                await socket.send(json.dumps({
                    "kind": "prompt.ack", "promptId": question["promptId"],
                    "presentationBinding": question["presentationBinding"],
                    "connectionGeneration": question["connectionGeneration"],
                    "source": question["source"], "success": True,
                    "audioEpoch": question["promptId"],
                }))
                # Deliberately no delay/barrier after acknowledgment: the
                # socket ordering alone must admit these first fresh samples.
                for data in (LOUD, LOUD, QUIET, QUIET):
                    await socket.send(json.dumps({
                        "kind": "audio", "source": "glasses", "seq": seq,
                        "pcm": list(struct.unpack("<3200h", data)),
                        "audioEpoch": question["promptId"],
                    }))
                    seq += 1
            await asyncio.wait_for(preparation, 1)
            await until(lambda: bool(reviewed))
            assert ids[0] != ids[1]
            assert reviewed == ["Send to Dele? “  stop, open Messages, keep these words.  ”"]
            assert command_calls == replier.queries == []
            assert not dispatcher.is_armed
            assert stt.calls == 2
            assert not gateway.physical_g2_connected
    finally:
        release_review.set()
        if preparation is not None and not preparation.done():
            preparation.cancel()
            await asyncio.gather(preparation, return_exceptions=True)
        worker = voice._utterance_worker
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        await replies.aclose()
        await gateway.stop()
