"""Selected-message speech entry at the ordinary wake gate.

AudioRouter, VAD, VoiceOrchestrator and prompted dispatch are real. STT and
the selected-conversation admission callback are controlled; these tests do
not establish contact selection, UI freshness or microphone recognition.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import struct

import pytest

from joat.audio.router import AudioRouter
from joat.audio.vad import RmsVad
from joat.core.interfaces import (
    AudioCallback, AudioFrame, AudioFrameCallback, AudioFrameSource,
    DeviceCallback, GlassesTransport, InputCallback, ReplyEngine, SttEngine, Transcript,
)
from joat.core.protocol import AudioSource, ContainerId, DownlinkMessage, HudText
from joat.services.orchestrator import VoiceOrchestrator
from joat.services.prompted_input import PromptedInputDispatcher, PromptTicket, UtteranceProvenance


LOUD = struct.pack("<3200h", *([8000, -8000] * 1600))
QUIET = bytes(6400)


async def until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


class Transport(GlassesTransport, AudioFrameSource):
    def __init__(self) -> None:
        self.sent: list[DownlinkMessage] = []
        self.callback: AudioFrameCallback | None = None
        self.seq = 0

    @property
    def connected(self) -> bool:
        return True

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send(self, message: DownlinkMessage) -> None:
        self.sent.append(message)

    def on_audio(self, callback: AudioCallback) -> None:
        raise AssertionError("The fixture must preserve capture provenance")

    def on_audio_frame(self, callback: AudioFrameCallback) -> None:
        self.callback = callback

    def on_input(self, callback: InputCallback) -> None: ...
    def on_device(self, callback: DeviceCallback) -> None: ...

    def emit(
        self, pcm: bytes, *, source: AudioSource = "glasses", generation: str = "connection-a",
        epoch: str | None = "captured-epoch", speaker: str | None = "self",
    ) -> None:
        assert self.callback is not None
        self.callback(AudioFrame(
            pcm=pcm, seq=self.seq, source=source, speaker=speaker,
            connection_generation=generation, audio_epoch=epoch, received_at_us=0,
        ))
        self.seq += 1

    def utterance(self, *, epoch: str | None = "captured-epoch") -> None:
        for pcm in (LOUD, LOUD, QUIET, QUIET):
            self.emit(pcm, epoch=epoch)


class Stt(SttEngine):
    def __init__(self, text: str, *, is_final: bool = True) -> None:
        self.result = Transcript(text, is_final)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.flushes = 0

    async def feed(self, pcm: bytes) -> Transcript | None:
        self.started.set()
        await self.release.wait()
        return None

    async def flush(self) -> Transcript:
        self.flushes += 1
        return self.result

    def reset(self) -> None: ...


class Replier(ReplyEngine):
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def reply(self, text: str) -> str:
        self.queries.append(text)
        return "ordinary model answer"


class Journey:
    def __init__(
        self, text: str, *, accept: bool = True, is_final: bool = True, install_entry: bool = True,
        command_result: str | None = None,
    ) -> None:
        self.transport = Transport()
        self.audio = AudioRouter(self.transport)
        self.stt = Stt(text, is_final=is_final)
        self.replier = Replier()
        self.dispatcher = PromptedInputDispatcher()
        self.entry_calls: list[tuple[str, UtteranceProvenance]] = []
        self.commands: list[str] = []
        self.prompt_answers: list[str] = []

        async def selected_message_reply(literal: str, provenance: UtteranceProvenance) -> bool:
            self.entry_calls.append((literal, provenance))
            return accept

        async def command(text: str) -> str | None:
            self.commands.append(text)
            return command_result

        options = {"selected_message_reply": selected_message_reply} if install_entry else {}
        self.voice = VoiceOrchestrator(
            self.transport, self.stt, frames=self.audio,
            vad=RmsVad(adaptive=False, start_batches=2, hangover_batches=2),
            wake_words=("joat",), commands=command, replier=self.replier,
            prompted_input=self.dispatcher, **options,
        )
        self.voice.attach()

    def arm_prompt(self) -> PromptTicket:
        async def consume(text: str) -> None:
            self.prompt_answers.append(text)

        ticket = self.dispatcher.begin_publication(
            owner_kind="messages", owner_id="pending-message", field="body",
            source="glasses", connection_generation="connection-a",
            presentation_binding="exact-body-question", ttl_s=45,
            is_valid=lambda: True, consume=consume,
        )
        assert self.dispatcher.acknowledge(
            ticket.prompt_id, success=True, presentation_binding=ticket.presentation_binding,
            connection_generation=ticket.connection_generation, audio_epoch=ticket.prompt_id,
            ingress_fence=self.voice.fence_prompt_audio(),
        )
        return ticket

    async def idle(self) -> None:
        await until(lambda: self.voice._utterance_worker is None)

    def assert_no_ordinary_response(self) -> None:
        assert self.commands == self.replier.queries == []
        assert all(
            isinstance(message, HudText) and message.container_id == ContainerId.STATUS_BAR
            for message in self.transport.sent
        ), "An owned or ignored utterance reached ordinary narration instead"

    async def aclose(self) -> None:
        self.stt.release.set()
        self.dispatcher.revoke()
        worker = self.voice._utterance_worker
        if worker is not None:
            worker.cancel()
            await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 1)


@pytest.mark.parametrize("literal", ["reply", "  Respond?!  "])
async def test_accepted_selected_reply_gets_original_literal_and_capture_provenance_once(literal: str) -> None:
    journey = Journey(literal)
    try:
        journey.transport.utterance()
        await journey.idle()
        assert len(journey.entry_calls) == 1
        text, receipt = journey.entry_calls[0]
        assert text == literal
        assert receipt == UtteranceProvenance(
            source="glasses", connection_generation="connection-a", audio_epoch="captured-epoch",
            first_ingress_ordinal=1, last_ingress_ordinal=4, speaker_roles=frozenset({"self"}),
        )
        assert journey.prompt_answers == []
        journey.assert_no_ordinary_response()
    finally:
        await journey.aclose()


@pytest.mark.parametrize("literal", ["reply", "Tell me a joke", "Open News"])
async def test_declined_unaddressed_entry_does_not_open_the_generic_wake_gate(literal: str) -> None:
    journey = Journey(literal, accept=False)
    try:
        journey.transport.utterance()
        await journey.idle()
        assert len(journey.entry_calls) == 1 and journey.entry_calls[0][0] == literal
        journey.assert_no_ordinary_response()
    finally:
        await journey.aclose()


async def test_addressed_conversation_bypasses_selected_reply_callback() -> None:
    journey = Journey("JOAT tell me a joke")
    try:
        journey.transport.utterance()
        await journey.idle()
        assert journey.entry_calls == []
        assert journey.commands == journey.replier.queries == ["tell me a joke"]
        assert journey.prompt_answers == []
    finally:
        await journey.aclose()


async def test_addressed_command_with_empty_handled_result_does_not_narrate() -> None:
    journey = Journey("JOAT reply", command_result="")
    try:
        journey.transport.utterance()
        await journey.idle()
        assert journey.entry_calls == []
        assert journey.commands == ["reply"] and journey.replier.queries == []
        assert all(
            isinstance(message, HudText) and message.container_id == ContainerId.STATUS_BAR
            for message in journey.transport.sent
        ), "A command that already handled its surface produced blank ordinary narration"
    finally:
        await journey.aclose()


async def test_prompted_body_consumes_reply_word_before_selected_entry() -> None:
    journey = Journey("  reply  ")
    try:
        ticket = journey.arm_prompt()
        journey.transport.utterance(epoch=ticket.prompt_id)
        await journey.idle()
        assert journey.prompt_answers == ["  reply  "]
        assert journey.entry_calls == []
        journey.assert_no_ordinary_response()
    finally:
        await journey.aclose()


async def test_completed_stt_keeps_old_receipt_after_a_new_selection_fence_and_connection() -> None:
    journey = Journey("reply", accept=False)
    journey.stt.release.clear()
    try:
        journey.transport.utterance()
        await until(journey.stt.started.is_set)
        selection_fence = journey.voice.fence_prompt_audio()
        assert selection_fence == 4
        journey.transport.emit(QUIET, generation="connection-b", epoch="new-selection-epoch")
        journey.stt.release.set()
        await journey.idle()
        assert len(journey.entry_calls) == 1
        literal, receipt = journey.entry_calls[0]
        assert literal == "reply"
        assert receipt.connection_generation == "connection-a"
        assert receipt.audio_epoch == "captured-epoch"
        assert receipt.first_ingress_ordinal == 1 and receipt.last_ingress_ordinal == selection_fence
        journey.assert_no_ordinary_response()
    finally:
        await journey.aclose()


@pytest.mark.parametrize("variation", ["phone", "mixed-source", "mixed-speaker", "missing-epoch"])
async def test_entry_owner_receives_existing_provenance_without_orchestrator_relabeling(variation: str) -> None:
    journey = Journey("reply", accept=False)
    try:
        for index, pcm in enumerate((LOUD, LOUD, QUIET, QUIET)):
            journey.transport.emit(
                pcm,
                source="phone" if variation == "phone" or (variation == "mixed-source" and index == 0) else "glasses",
                speaker="other" if variation == "mixed-speaker" and index == 0 else "self",
                epoch=None if variation == "missing-epoch" else "captured-epoch",
            )
        await journey.idle()
        assert len(journey.entry_calls) == 1
        receipt = journey.entry_calls[0][1]
        assert receipt.source == ("phone" if variation == "phone" else None if variation == "mixed-source" else "glasses")
        assert receipt.mixed is (variation == "mixed-source")
        assert receipt.speaker_roles == (frozenset({"self", "other"}) if variation == "mixed-speaker" else frozenset({"self"}))
        assert receipt.audio_epoch == (None if variation == "missing-epoch" else "captured-epoch")
        journey.assert_no_ordinary_response()
    finally:
        await journey.aclose()


@pytest.mark.parametrize("text,is_final", [("", True), ("reply", False)])
async def test_incomplete_or_empty_stt_never_reaches_selected_entry(text: str, is_final: bool) -> None:
    journey = Journey(text, is_final=is_final)
    try:
        journey.transport.utterance()
        await journey.idle()
        assert journey.entry_calls == []
        journey.assert_no_ordinary_response()
    finally:
        await journey.aclose()


async def test_missing_optional_callback_keeps_unaddressed_speech_ignored() -> None:
    journey = Journey("reply", install_entry=False)
    try:
        journey.transport.utterance()
        await journey.idle()
        assert journey.entry_calls == []
        journey.assert_no_ordinary_response()
    finally:
        await journey.aclose()
