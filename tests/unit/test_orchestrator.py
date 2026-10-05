"""VoiceOrchestrator flow tests with fake transport and engine."""

from __future__ import annotations

import asyncio
import logging
import struct
from contextlib import asynccontextmanager
from typing import Any

import pytest

from joat.audio.vad import RmsVad
from joat.core.interfaces import (
    AudioFrame,
    AudioFrameCallback,
    AudioFrameSource,
    ReplyEngine,
    AudioCallback,
    DeviceCallback,
    GlassesTransport,
    InputCallback,
    SttEngine,
    Transcript,
)
from joat.core.protocol import (
    AssistantConversationTurn,
    AssistantConversationView,
    AssistantOverlay,
    AudioControl,
    AudioSource,
    BrainstormAction,
    BrainstormHudView,
    ContainerId,
    DownlinkMessage,
    HudText,
)
from joat.services.orchestrator import (
    BYTES_PER_SECOND, ConversationWindow, VoiceOrchestrator,
    _QueuedUtterance, _Transcribed, _Turn, _joined,
)
from joat.services.greetings import greeting_response
from joat.services.hud import HudWriter
from joat.channels.cards import CardLog
from joat.services.capabilities import CapabilityOpenController, CapabilityRouter
from joat.services.commands import CommandReply
from joat.services.prompted_input import PromptedInputDispatcher, UtteranceProvenance
from joat.services.roundtable import Participant, RoundTable
from joat.services.session import DebateSession, Session, SessionManager
from joat.services.sinks import HudSink

BATCH_SAMPLES = 3200


def batch(amplitude: int) -> bytes:
    samples = [amplitude if i % 2 == 0 else -amplitude for i in range(BATCH_SAMPLES)]
    return struct.pack(f"<{BATCH_SAMPLES}h", *samples)


LOUD = batch(8000)
QUIET = batch(50)


class FakeTransport(GlassesTransport):
    def __init__(self) -> None:
        self.sent: list[DownlinkMessage] = []
        self.audio_cb: AudioCallback | None = None

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send(self, message: DownlinkMessage) -> None:
        self.sent.append(message)

    @property
    def connected(self) -> bool:
        return True

    def on_audio(self, callback: AudioCallback) -> None:
        self.audio_cb = callback

    def on_input(self, callback: InputCallback) -> None: ...
    def on_device(self, callback: DeviceCallback) -> None: ...


class FrameTransport(FakeTransport, AudioFrameSource):
    def __init__(self) -> None:
        super().__init__()
        self.frame_cb: AudioFrameCallback | None = None

    def on_audio_frame(self, callback: AudioFrameCallback) -> None:
        self.frame_cb = callback

    def emit_frame(
        self,
        pcm: bytes,
        seq: int,
        source: AudioSource,
        speaker: str | None = None,
        generation: str = "current",
    ) -> None:
        assert self.frame_cb is not None
        self.frame_cb(AudioFrame(
            pcm=pcm,
            connection_generation=generation,
            seq=seq,
            source=source,
            speaker=speaker,
            received_at_us=0,
        ))


class FakeEngine(SttEngine):
    def __init__(self, text: str = "hello glasses") -> None:
        self.text = text
        self.fed = bytearray()
        self.resets = 0

    async def feed(self, pcm: bytes) -> Transcript | None:
        self.fed.extend(pcm)
        return None

    async def flush(self) -> Transcript | None:
        if not self.fed:
            return None
        self.fed.clear()
        return Transcript(text=self.text, is_final=True)

    def reset(self) -> None:
        self.resets += 1
        self.fed.clear()


def TEST_VAD() -> RmsVad:
    """Fixed, short-hangover VAD so routing tests stay deterministic and
    independent of production acoustic tuning."""
    return RmsVad(hangover_batches=3, adaptive=False, speech_rms=0.015,
                  start_batches=1)


async def drain() -> None:
    for _ in range(4):
        await asyncio.sleep(0)


class TestVoiceLoop:
    async def test_connect_clears_stale_content_before_announcing_online(self) -> None:
        transport, engine = FakeTransport(), FakeEngine()

        await VoiceOrchestrator(transport, engine).on_connect()

        cleared = [
            message.container_id
            for message in transport.sent
            if isinstance(message, HudText) and message.text == ""
        ]
        assert cleared == [ContainerId.MAIN, ContainerId.CONTEXT, ContainerId.TRAY]
        status = region(transport, ContainerId.STATUS_BAR)
        assert status == ["JOAT · online"]
        assert isinstance(transport.sent[-1], AudioControl)
        assert transport.sent[-1].open is True

    async def test_connect_restores_active_session_status_after_opening_mic(self) -> None:
        class ActiveSessions:
            active = True
            active_status = "DEBATE · battery policy"

            def handle_transcript(self, _text: str) -> bool:
                return True

        transport, engine = FakeTransport(), FakeEngine()
        orch = VoiceOrchestrator(
            transport,
            engine,
            sessions=ActiveSessions(),  # type: ignore[arg-type]
        )

        await orch.on_connect()

        assert not [
            message
            for message in transport.sent
            if isinstance(message, HudText) and message.text == ""
        ]
        assert region(transport, ContainerId.STATUS_BAR)[-1] == "DEBATE · battery policy"
        assert isinstance(transport.sent[-2], AudioControl)
        assert transport.sent[-2].open is True

    async def test_connect_replays_the_active_brainstorm_frame_after_authentication(self) -> None:
        class ActiveSessions:
            active = True
            active_status = "BRAINSTORM · reconnect"

            def __init__(self) -> None:
                self.replayed = 0

            async def replay_active_hud(self) -> bool:
                self.replayed += 1
                return True

            def handle_transcript(self, _text: str) -> bool:
                return True

        sessions = ActiveSessions()
        transport, engine = FakeTransport(), FakeEngine()
        await VoiceOrchestrator(transport, engine, sessions=sessions).on_connect()  # type: ignore[arg-type]

        assert sessions.replayed == 1

    async def test_utterance_becomes_hud_text(self) -> None:
        transport, engine = FakeTransport(), FakeEngine("turn on the lights")
        orch = VoiceOrchestrator(transport, engine, vad=TEST_VAD())
        orch.attach()
        assert transport.audio_cb is not None

        seq = 0
        for pcm in [LOUD, LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]:
            transport.audio_cb(pcm, seq)
            seq += 1
        await drain()

        assert conversation_role_text(transport, "user") == ["turn on the lights"]

    async def test_other_speaker_audio_skips_stt_but_unknown_remains_eligible(self) -> None:
        transport, engine = FrameTransport(), FakeEngine("hello from an unknown speaker")
        orch = VoiceOrchestrator(transport, engine, vad=TEST_VAD())
        orch.attach()
        assert transport.frame_cb is not None

        sequence = 0
        for pcm in [LOUD, LOUD, QUIET, QUIET, QUIET]:
            transport.emit_frame(pcm, sequence, "glasses", "other")
            sequence += 1
        await drain()
        # SDK speaker role is only a classifier, so unknown must not share
        # the other-speaker rejection path.
        for pcm in [LOUD, LOUD, QUIET, QUIET, QUIET]:
            transport.emit_frame(pcm, sequence, "glasses", "unknown")
            sequence += 1
        await drain()

        assert conversation_role_text(transport, "user") == ["hello from an unknown speaker"]

    async def test_seq_gap_resets_vad_and_engine(self) -> None:
        transport, engine = FakeTransport(), FakeEngine()
        orch = VoiceOrchestrator(transport, engine, vad=TEST_VAD())
        orch.attach()
        assert transport.audio_cb is not None

        transport.audio_cb(LOUD, 0)
        transport.audio_cb(LOUD, 1)
        transport.audio_cb(LOUD, 7)  # gap: 2-6 dropped upstream
        await drain()
        assert engine.resets == 1

        # utterance restarted at the gap; closing it still transcribes
        for i, pcm in enumerate([QUIET, QUIET, QUIET, QUIET]):
            transport.audio_cb(pcm, 8 + i)
        await drain()
        assert conversation_role_text(transport, "user") == [engine.text]

    @pytest.mark.parametrize("next_seq", [0, 2])
    async def test_new_connection_never_joins_partial_speech(
        self, next_seq: int, caplog: pytest.LogCaptureFixture,
    ) -> None:
        class RecordingEngine(FakeEngine):
            async def feed(self, pcm: bytes) -> Transcript | None:
                self.received = pcm
                return await super().feed(pcm)

        transport, engine = FrameTransport(), RecordingEngine()
        VoiceOrchestrator(transport, engine, vad=TEST_VAD()).attach()
        transport.emit_frame(batch(4000), 0, "glasses", generation="old")
        transport.emit_frame(batch(4000), 1, "glasses", generation="old")
        for seq, pcm in enumerate([LOUD, LOUD, QUIET, QUIET, QUIET], next_seq):
            transport.emit_frame(pcm, seq, "glasses", generation="replacement")
        await drain()

        assert engine.received == LOUD * 2 + QUIET * 3
        assert "audio gap" not in caplog.text  # counter restart is a stream boundary

    @pytest.mark.parametrize("boundary", ["gap", "replacement"])
    async def test_gap_during_stt_preserves_complete_turn_and_recovers_without_splicing(
        self, boundary: str,
    ) -> None:
        class HeldEngine(FakeEngine):
            def __init__(self) -> None:
                super().__init__("JOAT, recovery proof")
                self.entered = asyncio.Event()
                self.release = asyncio.Event()
                self.flushed: list[bytes] = []

            async def feed(self, pcm: bytes) -> Transcript | None:
                await super().feed(pcm)
                self.entered.set()
                await self.release.wait()
                return None

            async def flush(self) -> Transcript | None:
                self.flushed.append(bytes(self.fed))
                return await super().flush()

        transport, engine = FrameTransport(), HeldEngine()
        orch = VoiceOrchestrator(
            transport, engine, vad=TEST_VAD(), wake_words=("joat",),
        )
        orch.attach()
        complete = [LOUD, LOUD, QUIET, QUIET, QUIET]
        for seq, pcm in enumerate(complete):
            transport.emit_frame(pcm, seq, "glasses", "self")
        await asyncio.wait_for(engine.entered.wait(), 1)
        try:
            transport.emit_frame(batch(4000), 5, "glasses", "self")
            # seq 6 is genuinely missing. Neither its prefix nor its speaker
            # attribution may leak into the contiguous recovery segment.
            recovered = [batch(9000), batch(9000), QUIET, QUIET, QUIET]
            for seq, pcm in enumerate(recovered, 7):
                transport.emit_frame(
                    pcm, seq, "glasses", "unknown",
                    generation="replacement" if boundary == "replacement" else "current",
                )
            engine.release.set()
            assert orch._utterance_worker is not None
            await asyncio.wait_for(orch._utterance_worker, 1)
        finally:
            engine.release.set()

        assert engine.flushed == [b"".join(complete), b"".join(recovered)]
        assert conversation_role_text(transport, "user") == [
            "recovery proof", "recovery proof",
        ]

    async def test_gap_clears_speaker_provenance_and_keeps_unaddressed_gate(self) -> None:
        transport, engine = FrameTransport(), FakeEngine("ambient private fixture")
        orch = VoiceOrchestrator(
            transport, engine, vad=TEST_VAD(), wake_words=("joat",),
        )
        orch.attach()
        transport.emit_frame(LOUD, 0, "glasses", "self")
        for seq, pcm in enumerate([LOUD, LOUD, QUIET, QUIET, QUIET], 2):
            transport.emit_frame(pcm, seq, "glasses", "other")
        await drain()
        assert orch._voice_turn == 0  # pre-gap self must not admit other-only speech
        for seq, pcm in enumerate([LOUD, LOUD, QUIET, QUIET, QUIET], 7):
            transport.emit_frame(pcm, seq, "glasses", "unknown")
        await drain()
        assert orch._voice_turn == 1
        assert region(transport, ContainerId.CONTEXT) == []

    async def test_no_stt_stays_silent_but_alive(self) -> None:
        transport, engine = FakeTransport(), FakeEngine()
        orch = VoiceOrchestrator(transport, engine, stt_available=False)
        orch.attach()
        assert transport.audio_cb is not None

        for i, pcm in enumerate([LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]):
            transport.audio_cb(pcm, i)
        await drain()
        assert transport.sent == []  # no transcript, no crash

    async def test_greeting_reflects_stt_state_and_requests_ears(self) -> None:
        transport, engine = FakeTransport(), FakeEngine()
        await VoiceOrchestrator(transport, engine, stt_available=False).on_connect()
        assert "no STT" in region(transport, ContainerId.STATUS_BAR)[0]
        assert "Install whisperkit-cli" in region(transport, ContainerId.MAIN)[-1]
        assert isinstance(transport.sent[-1], AudioControl)
        assert transport.sent[-1].open is True
        assert len(transport.sent) == 6  # 3 clears, status, install hint, mic on


class FakeReplier(ReplyEngine):
    def __init__(self, text: str | None = "It is 72 degrees.") -> None:
        self.text = text
        self.asked: list[str] = []

    async def reply(self, text: str) -> str | None:
        self.asked.append(text)
        return self.text


@asynccontextmanager
async def held_brainstorm():
    """Keep real collaborators pending while the wearer changes features."""
    transport = FakeTransport()
    hud = HudWriter(transport)
    entered, release = asyncio.Event(), asyncio.Event()
    selected = ["Brainstorm"]

    async def collaborator(_prompt):
        entered.set()
        await release.wait()
        yield "Finished."

    seats = RoundTable([
        Participant(name=name, role=role, stream=collaborator)
        for name, role in (("codex", "proposer"), ("claude", "critic"))
    ])
    session = DebateSession("Desk organization", seats, HudSink(hud), None, max_rounds=1)
    manager = SessionManager(
        hud, lambda _topic: session,
        brainstorm_view_current=lambda: selected[0] == "Brainstorm",
    )
    try:
        assert manager.try_start_session(session)
        await asyncio.wait_for(entered.wait(), 1)
        yield transport, manager, session, selected
    finally:
        release.set()
        await manager.close(timeout_s=.25)


class TestSpeechWhileBrainstormRunsElsewhere:
    @pytest.mark.parametrize("spoken,navigated,contribution", [
        ("JOAT, open Messages", True, ()),
        ("open Messages", False, ()),
        ("JOAT, consider a standing desk", False, (("YOU", "consider a standing desk"),)),
    ])
    async def test_only_addressed_feature_open_leaves_selected_brainstorm(self, spoken, navigated, contribution):
        async with held_brainstorm() as (transport, manager, session, _selected):
            capabilities = CapabilityRouter(
                HudWriter(transport), transport, None, None, CardLog(),
            )
            capabilities.claim_brainstorm_view()
            opener = CapabilityOpenController(lambda: capabilities)
            before = capabilities.view_revision
            orchestrator = VoiceOrchestrator(
                transport, FakeEngine(spoken), sessions=manager,
                wake_words=("joat",), feature_navigation=opener.handle_command,
                surface_revision=lambda: capabilities.view_revision,
            )
            await orchestrator._handle_utterance(LOUD)

            assert capabilities.brainstorm_view_current is not navigated
            assert (capabilities.view_revision > before) is navigated
            assert session.transcript() == contribution
            assert manager.active_session is session
            assert not any(isinstance(frame, AssistantOverlay) for frame in transport.sent)

    @pytest.mark.parametrize("surface,query", [
        ("Messages", "open QuickNotes"),
        ("QuickNotes", "open Messages"),
        ("Agent Terminal", "terminal cancel"),
    ])
    async def test_addressed_feature_request_reaches_its_controller(self, surface, query):
        async with held_brainstorm() as (transport, manager, session, selected):
            selected[0] = surface
            dispatched = []

            async def command(text):
                dispatched.append(text)
                return "Feature request handled."

            orchestrator = VoiceOrchestrator(
                transport, FakeEngine("JOAT, " + query), sessions=manager,
                wake_words=("joat",), commands=command,
            )
            await orchestrator._handle_utterance(LOUD)

            assert dispatched == [query]
            assert session.transcript() == ()
            assert manager.active_session is session

    @pytest.mark.parametrize("spoken,expected", [
        ("JOAT, what is a standing desk?", ["what is a standing desk?"]),
        ("what is a standing desk?", []),
    ])
    async def test_assistant_address_gate_survives_background_work(self, spoken, expected):
        async with held_brainstorm() as (transport, manager, session, selected):
            selected[0] = "QuickNotes"
            replier = FakeReplier()
            orchestrator = VoiceOrchestrator(
                transport, FakeEngine(spoken), sessions=manager,
                wake_words=("joat",), replier=replier,
            )
            await orchestrator._handle_utterance(LOUD)

            assert replier.asked == expected
            assert session.transcript() == ()
            assert manager.active_session is session

    async def test_selected_bare_reply_reaches_message_owner(self):
        async with held_brainstorm() as (transport, manager, session, selected):
            selected[0] = "Messages"
            received = []
            provenance = UtteranceProvenance("glasses", "g1", None, 1, 2, frozenset({"self"}))

            async def reply(text, receipt):
                received.append((text, receipt))
                return True

            orchestrator = VoiceOrchestrator(
                transport, FakeEngine("reply"), sessions=manager,
                wake_words=("joat",), selected_message_reply=reply,
            )
            await orchestrator._handle_utterance(LOUD, provenance=provenance)

            assert received == [("reply", provenance)]
            assert session.transcript() == ()
            assert manager.active_session is session

    @pytest.mark.parametrize("spoken", ["cancel", "JOAT, open QuickNotes"])
    async def test_published_message_body_precedes_cancel_and_feature_routing(self, spoken):
        async with held_brainstorm() as (transport, manager, session, selected):
            selected[0] = "Messages"
            received = []
            navigated = []
            dispatcher = PromptedInputDispatcher()

            async def consume(text):
                received.append(text)

            async def navigate(text):
                navigated.append(text)
                return "Opening QuickNotes."

            ticket = dispatcher.begin_publication(
                owner_kind="message", owner_id="selected", field="body",
                source="glasses", connection_generation="g1",
                presentation_binding="body-prompt", ttl_s=30,
                is_valid=lambda: True, consume=consume,
            )
            assert dispatcher.acknowledge(
                ticket.prompt_id, success=True, presentation_binding="body-prompt",
                connection_generation="g1", audio_epoch=ticket.prompt_id, ingress_fence=0,
            )
            provenance = UtteranceProvenance(
                "glasses", "g1", ticket.prompt_id, 1, 2, frozenset({"self"}),
            )
            replier = FakeReplier()
            orchestrator = VoiceOrchestrator(
                transport, FakeEngine(spoken), sessions=manager,
                wake_words=("joat",), prompted_input=dispatcher, replier=replier,
                feature_navigation=navigate,
            )
            await orchestrator._handle_utterance(LOUD, provenance=provenance)

            assert received == [spoken]
            assert navigated == []
            assert replier.asked == []
            assert session.transcript() == ()
            assert manager.active_session is session


class HeldTopicSession(Session):
    def __init__(self, entered: asyncio.Event) -> None:
        self._entered = entered

    async def run(self) -> None:
        self._entered.set()
        await asyncio.Event().wait()

    def cancel(self) -> None:
        return None


class TestGlassesFirstTopicCapture:
    async def test_addressed_glasses_voice_starts_brainstorm_without_opening_a_menu(self) -> None:
        """The everyday path is voice on the glasses, not Mac-side setup."""
        transport = FrameTransport()
        engine = FakeEngine("JOAT, brainstorm compare home Mini options")
        entered = asyncio.Event()
        topics: list[str] = []
        sessions = SessionManager(
            HudWriter(transport),
            lambda topic: topics.append(topic) or HeldTopicSession(entered),
            brainstorm_available=lambda: True,
        )
        replier = FakeReplier()
        orchestrator = VoiceOrchestrator(
            transport,
            engine,
            vad=TEST_VAD(),
            sessions=sessions,
            replier=replier,
            wake_words=("joat",),
        )
        orchestrator.attach()
        assert transport.frame_cb is not None

        for sequence, pcm in enumerate([LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]):
            transport.emit_frame(pcm, sequence, "glasses")
        await asyncio.wait_for(entered.wait(), timeout=1)

        assert topics == ["compare home Mini options"]
        assert replier.asked == []
        await sessions.close(timeout_s=0.1)

    async def test_tapped_capture_consumes_glasses_speech_before_the_wake_word_gate(self) -> None:
        transport = FrameTransport()
        engine = FakeEngine("compare a home mini with this laptop")
        entered = asyncio.Event()
        topics: list[str] = []
        capture = BrainstormHudView(state="topic_capture", topic="Speak your question", collaborators=["Codex", "Claude", "Hermes"], activity="Your next spoken phrase starts this Brainstorm.")
        sessions = SessionManager(
            HudWriter(transport),
            lambda topic: topics.append(topic) or HeldTopicSession(entered),
            topic_capture_view=lambda _armed: capture,
        )
        replier = FakeReplier()
        sessions.handle_brainstorm_action(BrainstormAction("begin_topic_capture"))
        await asyncio.sleep(0)
        orchestrator = VoiceOrchestrator(
            transport, engine, vad=TEST_VAD(), sessions=sessions,
            replier=replier, wake_words=("joat",),
        )
        orchestrator.attach()
        assert transport.frame_cb is not None

        for sequence, pcm in enumerate([LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]):
            transport.emit_frame(pcm, sequence, "glasses")
        await asyncio.wait_for(entered.wait(), timeout=1)

        assert topics == ["compare a home mini with this laptop"]
        assert replier.asked == []
        await sessions.close(timeout_s=0.1)

    async def test_phone_audio_cannot_satisfy_a_glasses_armed_capture(self) -> None:
        transport = FrameTransport()
        engine = FakeEngine("a phone-originated topic")
        entered = asyncio.Event()
        topics: list[str] = []
        capture = BrainstormHudView(state="topic_capture", topic="Speak your question", collaborators=["Codex", "Claude", "Hermes"], activity="Your next spoken phrase starts this Brainstorm.")
        sessions = SessionManager(
            HudWriter(transport),
            lambda topic: topics.append(topic) or HeldTopicSession(entered),
            topic_capture_view=lambda _armed: capture,
        )
        replier = FakeReplier()
        sessions.handle_brainstorm_action(BrainstormAction("begin_topic_capture"))
        await asyncio.sleep(0)
        orchestrator = VoiceOrchestrator(
            transport, engine, vad=TEST_VAD(), sessions=sessions,
            replier=replier, wake_words=("joat",),
        )
        orchestrator.attach()

        for sequence, pcm in enumerate([LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]):
            transport.emit_frame(pcm, sequence, "phone")
        await drain()

        assert topics == []
        assert not entered.is_set()
        assert sessions.topic_capture_armed
        assert replier.asked == []


class TestReplyFlow:
    async def test_addressed_greeting_uses_command_path_not_reply_engine(self) -> None:
        transport, engine = FakeTransport(), FakeEngine("Good morning, JOATs.")
        replier = FakeReplier("irrelevant grounded fallback")
        commands_seen: list[str] = []

        async def commands(text: str) -> str | None:
            commands_seen.append(text)
            return greeting_response(text)

        orch = VoiceOrchestrator(
            transport,
            engine,
            vad=TEST_VAD(),
            replier=replier,
            wake_words=("joat",),
            commands=commands,
        )
        orch.attach()
        assert transport.audio_cb is not None
        for i, pcm in enumerate([LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]):
            transport.audio_cb(pcm, i)
        await drain()

        assert commands_seen == ["Good morning."]
        assert assistant_text(transport) == ["Good morning. How can I help?"]
        assert replier.asked == []

    async def test_greeting_plus_request_reaches_reply_engine_intact(self) -> None:
        transcript = "Good morning, JOATs, what is on my calendar?"
        transport, engine = FakeTransport(), FakeEngine(transcript)
        replier = FakeReplier("You have one event at noon.")
        commands_seen: list[str] = []

        async def commands(text: str) -> str | None:
            commands_seen.append(text)
            return greeting_response(text)

        orch = VoiceOrchestrator(
            transport,
            engine,
            vad=TEST_VAD(),
            replier=replier,
            wake_words=("joat",),
            commands=commands,
        )
        orch.attach()
        assert transport.audio_cb is not None
        for i, pcm in enumerate([LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]):
            transport.audio_cb(pcm, i)
        await drain()

        expected = "Good morning, what is on my calendar?"
        assert commands_seen == [expected]
        assert replier.asked == [expected]
        assert assistant_text(transport) == ["You have one event at noon."]

    async def test_unaddressed_greeting_stays_silent_in_wake_word_mode(self) -> None:
        transport, engine = FakeTransport(), FakeEngine("Good morning.")
        replier = FakeReplier("should not be used")
        orch = VoiceOrchestrator(
            transport,
            engine,
            vad=TEST_VAD(),
            replier=replier,
            wake_words=("joat",),
        )
        orch.attach()
        assert transport.audio_cb is not None
        for i, pcm in enumerate([LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]):
            transport.audio_cb(pcm, i)
        await drain()

        assert replier.asked == []
        assert region(transport, ContainerId.MAIN) == []
        assert region(transport, ContainerId.STATUS_BAR)[-1] == "JOAT · listening"

    async def test_active_session_keeps_its_status_after_consuming_speech(self) -> None:
        class ActiveSessions:
            active = True
            active_status = "DEBATE · battery policy"

            def handle_transcript(self, _text: str) -> bool:
                return True

        transport, engine = FakeTransport(), FakeEngine("Claude, respond.")
        orch = VoiceOrchestrator(
            transport,
            engine,
            vad=TEST_VAD(),
            sessions=ActiveSessions(),  # type: ignore[arg-type]
        )
        orch.attach()
        assert transport.audio_cb is not None
        for i, pcm in enumerate([LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]):
            transport.audio_cb(pcm, i)
        await drain()

        assert region(transport, ContainerId.STATUS_BAR)[-1] == "DEBATE · battery policy"

    async def test_transcript_then_reply_both_reach_the_hud(self) -> None:
        transport, engine = FakeTransport(), FakeEngine("what is the weather")
        replier = FakeReplier("It is 72 degrees.")
        orch = VoiceOrchestrator(transport, engine, vad=TEST_VAD(), replier=replier)
        orch.attach()
        assert transport.audio_cb is not None
        for i, pcm in enumerate([LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]):
            transport.audio_cb(pcm, i)
        await drain()
        assert assistant_text(transport) == ["It is 72 degrees."]
        assert conversation_role_text(transport, "user") == ["what is the weather"]
        assert replier.asked == ["what is the weather"]

    async def test_assistant_reply_is_one_persistent_transcript_not_a_timed_overlay(self) -> None:
        transport, engine = FakeTransport(), FakeEngine("JOAT, what is next?")
        orch = VoiceOrchestrator(
            transport, engine, vad=TEST_VAD(), replier=FakeReplier("Design review at 4:30."),
            wake_words=("joat",),
        )
        orch.attach()
        assert transport.audio_cb is not None
        for i, pcm in enumerate([LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]):
            transport.audio_cb(pcm, i)
        await drain()

        views = conversation_views(transport)
        assert [view.state for view in views] == ["thinking", "answering", "ready"]
        assert views[-1].turns == (
            AssistantConversationTurn("user", "what is next?"),
            AssistantConversationTurn("assistant", "Design review at 4:30."),
        )
        assert not any(isinstance(message, AssistantOverlay) for message in transport.sent)
        assert region(transport, ContainerId.MAIN) == []

    async def test_replier_declining_leaves_transcript_standing(self) -> None:
        transport, engine = FakeTransport(), FakeEngine("hm")
        orch = VoiceOrchestrator(transport, engine, vad=TEST_VAD(), replier=FakeReplier(None))
        orch.attach()
        assert transport.audio_cb is not None
        for i, pcm in enumerate([LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]):
            transport.audio_cb(pcm, i)
        await drain()
        # No reply: MAIN stays clear, but the wearer still sees they were heard.
        assert region(transport, ContainerId.MAIN) == []
        assert conversation_role_text(transport, "user") == ["hm"]

    async def test_busy_voice_lane_keeps_latest_completed_glasses_utterance(self) -> None:
        transport = FrameTransport()

        class UtteranceEngine(FakeEngine):
            async def flush(self) -> Transcript | None:
                if not self.fed:
                    return None
                amplitude = max(abs(sample[0]) for sample in struct.iter_unpack("<h", self.fed))
                self.fed.clear()
                return Transcript(
                    text={8000: "first question", 7000: "middle question", 6000: "latest question"}[amplitude],
                    is_final=True,
                )

        engine = UtteranceEngine()

        class ArmedCapture:
            active = False
            active_status = ""

            def __init__(self) -> None:
                self.topic_capture_armed = False
                self.captured: list[tuple[str, AudioSource | None]] = []

            def handle_transcript(
                self,
                text: str,
                capture_source: AudioSource | None = None,
            ) -> bool:
                if not self.topic_capture_armed or capture_source != "glasses":
                    return False
                self.captured.append((text, capture_source))
                return True

        sessions = ArmedCapture()

        class SlowReplier(ReplyEngine):
            def __init__(self) -> None:
                self.gate = asyncio.Event()
                self.entered = asyncio.Event()
                self.asked: list[str] = []

            async def reply(self, text: str) -> str | None:
                self.asked.append(text)
                self.entered.set()
                await self.gate.wait()
                return "slow answer"

        replier = SlowReplier()
        orch = VoiceOrchestrator(
            transport, engine, vad=TEST_VAD(), replier=replier, sessions=sessions  # type: ignore[arg-type]
        )
        orch.attach()
        assert transport.frame_cb is not None

        seq = 0
        def emit_utterance(amplitude: int, source: AudioSource) -> None:
            nonlocal seq
            for pcm in [batch(amplitude), batch(amplitude), QUIET, QUIET, QUIET]:
                transport.emit_frame(pcm, seq, source)
                seq += 1

        emit_utterance(8000, "phone")
        await asyncio.wait_for(replier.entered.wait(), timeout=1)
        # Two finished utterances arrive while the reply engine is occupied.
        # They queue in order (the audit's A3: the old one-slot handoff
        # dropped the middle one). Without wake words every utterance is
        # addressed, and speech that ends while an answer is under way
        # replaces that answer — so the newest glasses utterance wins the
        # turn, retaining its source for the G2 topic capture security gate.
        sessions.topic_capture_armed = True
        emit_utterance(7000, "phone")
        emit_utterance(6000, "glasses")
        replier.gate.set()
        assert orch._utterance_worker is not None
        await asyncio.wait_for(orch._utterance_worker, 2)

        assert replier.asked[0] == "first question"
        assert "latest question" not in replier.asked  # captured, never a reply
        assert sessions.captured == [("latest question", "glasses")]
        assert conversation_role_text(transport, "user")[0] == "first question"


class StreamingReplier(ReplyEngine):
    """True chunked streaming, like MlxReplyEngine.stream."""

    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks

    async def reply(self, text: str) -> str | None:
        return "".join(self.chunks)

    async def stream(self, text: str):
        for c in self.chunks:
            yield c


class GatedStreamingReplier(ReplyEngine):
    """Pause after a complete first sentence so visibility can be inspected."""

    def __init__(self) -> None:
        self.waiting_after_preview = asyncio.Event()
        self.release = asyncio.Event()

    async def reply(self, text: str) -> str | None:
        raise AssertionError("stream owns this test")

    async def stream(self, text: str):
        yield "Why did the developer bring glasses? "
        self.waiting_after_preview.set()
        await self.release.wait()
        yield "Because every bug deserved a closer look."


class TestStreamedReply:
    async def test_one_native_transcript_streams_cumulative_text_before_the_model_finishes(self) -> None:
        transport, engine = FakeTransport(), FakeEngine("JOAT, tell me a joke")
        replier = GatedStreamingReplier()
        latency_events: list[tuple[int, str, str]] = []
        orch = VoiceOrchestrator(
            transport, engine, vad=TEST_VAD(), replier=replier, wake_words=WAKE,
            latency_observer=lambda turn, stage, bucket: latency_events.append((turn, stage, bucket)),
            stream_publish_interval_s=0.0,
        )

        turn = asyncio.create_task(orch._handle_utterance(LOUD))
        await asyncio.wait_for(replier.waiting_after_preview.wait(), timeout=1)
        visible = conversation_views(transport)
        assert [message.state for message in visible] == ["thinking", "answering"]
        assert visible[-1].turns == (
            AssistantConversationTurn("user", "tell me a joke"),
            AssistantConversationTurn("assistant", "Why did the developer bring glasses?"),
        )
        assert [stage for _turn, stage, _bucket in latency_events][-2:] == [
            "first_model_chunk", "first_reply_sent",
        ]
        assert not any(isinstance(message, AssistantOverlay) for message in transport.sent)

        replier.release.set()
        await asyncio.wait_for(turn, timeout=1)
        visible = conversation_views(transport)
        assert [message.state for message in visible] == [
            "thinking", "answering", "answering", "ready",
        ]
        assert visible[-1].turns[-1].text == (
            "Why did the developer bring glasses? Because every bug deserved a closer look."
        )
        assert len({message.conversation_id for message in visible}) == 1

    async def test_model_opened_terminal_keeps_its_view_and_finishes_the_reply(self) -> None:
        transport = FakeTransport()
        revision = 0
        completed = asyncio.Event()

        class TerminalToolReplier(ReplyEngine):
            async def reply(self, _text: str) -> str | None:
                raise AssertionError("stream owns this test")

            async def stream(self, _text: str):
                nonlocal revision
                revision += 1  # The typed tool acquires its explicit Terminal visit.
                yield "Your instruction was accepted by Hermes."
                completed.set()

        orch = VoiceOrchestrator(
            transport, FakeEngine("JOAT, ask Hermes for two desk organization ideas"),
            vad=TEST_VAD(), replier=TerminalToolReplier(), wake_words=WAKE,
            surface_revision=lambda: revision,
        )
        await orch._handle_utterance(LOUD)
        assert completed.is_set()
        assert not any(isinstance(message, AssistantOverlay) for message in transport.sent)

    @pytest.mark.parametrize("settles_before_receipt", [False, True])
    async def test_model_started_session_keeps_hud_after_tool_receipt(
        self, settles_before_receipt: bool,
    ) -> None:
        transport = FakeTransport()
        opened, release, settled = (asyncio.Event() for _ in range(3))
        model_completed = asyncio.Event()
        hud = HudWriter(transport)

        class StartedBrainstorm(Session):
            async def run(self) -> None:
                await hud.show_brainstorm(BrainstormHudView(
                    state="running", topic="Organize a desk",
                    collaborators=["codex", "claude"], activity="Collaborators are working",
                ))
                opened.set()
                await release.wait()

            def cancel(self) -> None:
                release.set()

        sessions = SessionManager(
            hud, lambda _topic: StartedBrainstorm(),
            on_state=lambda state: settled.set() if state == "settled" else None,
        )

        class ToolCallingReplier(ReplyEngine):
            async def reply(self, _text: str) -> str | None:
                raise AssertionError("stream owns this test")

            async def stream(self, _text: str):
                assert sessions.start_brainstorm("Organize a desk") == "started"
                await opened.wait()
                if settles_before_receipt:
                    release.set()
                    await settled.wait()
                yield "I've started your Brainstorm."
                model_completed.set()

        orch = VoiceOrchestrator(
            transport, FakeEngine("JOAT, have the collaborators consider desk organization"),
            vad=TEST_VAD(), replier=ToolCallingReplier(), sessions=sessions, wake_words=WAKE,
        )
        try:
            await orch._handle_utterance(LOUD)
            assert any(isinstance(message, BrainstormHudView) for message in transport.sent)
            assert not any(isinstance(message, AssistantOverlay) for message in transport.sent), (
                "The obsolete assistant receipt must not cover the newly owned session, even after it settles"
            )
            assert model_completed.is_set(), "a successful tool handoff must finish the assistant turn"
        finally:
            release.set()
            await sessions.close(timeout_s=.1)

    async def test_opt_in_latency_observer_is_bounded_content_free_and_ordered(
        self,
    ) -> None:
        secret_query = "private calendar question"
        secret_reply = "Private answer that must never enter the latency receipt."
        latency_events: list[tuple[int, str, str]] = []
        transport, engine = FakeTransport(), FakeEngine(f"JOAT, {secret_query}")
        orch = VoiceOrchestrator(
            transport,
            engine,
            vad=TEST_VAD(),
            replier=StreamingReplier([secret_reply[:8], secret_reply[8:]]),
            wake_words=WAKE,
            latency_observer=lambda turn, stage, bucket: latency_events.append((turn, stage, bucket)),
            stream_publish_interval_s=0.0,
        )

        await orch._handle_utterance(LOUD)

        assert [stage for _turn, stage, _bucket in latency_events] == [
            "utterance_ready",
            "ack_sent",
            "stt_complete",
            "first_model_chunk",
            "first_reply_sent",
            "reply_complete",
        ]
        assert {turn for turn, _stage, _bucket in latency_events} == {1}
        allowed_buckets = {
            "lte250ms", "250_500ms", "500ms_1s", "1_2s",
            "2_5s", "5_10s", "gt10s",
        }
        assert all(bucket in allowed_buckets for _turn, _stage, bucket in latency_events)
        assert secret_query not in repr(latency_events)
        assert secret_reply not in repr(latency_events)

    async def test_native_conversation_does_not_claim_an_unrelated_image_tile_as_its_paint(
        self,
    ) -> None:
        latency_events: list[tuple[int, str, str]] = []
        transport, engine = FakeTransport(), FakeEngine("JOAT, hello")
        orch = VoiceOrchestrator(
            transport,
            engine,
            vad=TEST_VAD(),
            replier=StreamingReplier(["Hello there."]),
            wake_words=WAKE,
            latency_observer=lambda turn, stage, bucket: latency_events.append((turn, stage, bucket)),
        )

        await orch._handle_utterance(LOUD)
        orch.on_paint_trace({
            "type": "tile_upload", "outcome": "success", "containerId": 4,
        })

        assert "painted" not in [stage for _turn, stage, _bucket in latency_events]

    async def test_raster_receipt_still_correlates_its_next_successful_tile(self) -> None:
        latency_events: list[tuple[int, str, str]] = []
        surface_revision = [0]

        async def receipt(_query: str) -> str:
            # A successful feature/tool transition owns the page, so its
            # compact acknowledgement is intentionally a raster receipt.
            surface_revision[0] += 1
            return CommandReply("Saved QuickNote.", presentation="receipt")

        transport, engine = FakeTransport(), FakeEngine("JOAT, save that note")
        orch = VoiceOrchestrator(
            transport,
            engine,
            vad=TEST_VAD(),
            wake_words=WAKE,
            commands=receipt,
            surface_revision=lambda: surface_revision[0],
            latency_observer=lambda turn, stage, bucket: latency_events.append((turn, stage, bucket)),
        )

        await orch._handle_utterance(LOUD)
        orch.on_paint_trace({
            "type": "tile_upload", "outcome": "success", "containerId": 4,
        })

        assert [stage for _turn, stage, _bucket in latency_events][-1] == "painted"

    async def test_receipt_without_a_surface_handoff_remains_in_the_native_conversation(self) -> None:
        async def receipt(_query: str) -> str:
            return CommandReply("Saved QuickNote.", presentation="receipt")

        transport, engine = FakeTransport(), FakeEngine("JOAT, save that note")
        orch = VoiceOrchestrator(
            transport,
            engine,
            vad=TEST_VAD(),
            wake_words=WAKE,
            commands=receipt,
        )

        await orch._handle_utterance(LOUD)

        assert conversation_views(transport)[-1].turns == (
            AssistantConversationTurn("user", "save that note"),
            AssistantConversationTurn("assistant", "Saved QuickNote."),
        )
        assert not any(isinstance(message, AssistantOverlay) for message in transport.sent)

    async def test_latency_observer_is_disabled_by_default(self, caplog) -> None:  # type: ignore[no-untyped-def]
        caplog.set_level(logging.INFO)
        transport, engine = FakeTransport(), FakeEngine("JOAT, hello")
        orch = VoiceOrchestrator(
            transport,
            engine,
            vad=TEST_VAD(),
            replier=StreamingReplier(["hello"]),
            wake_words=WAKE,
        )

        await orch._handle_utterance(LOUD)

        assert "voice latency" not in caplog.text
        assert "utterance ended:" not in caplog.text

    async def test_latency_observer_failure_cannot_break_the_voice_path(self) -> None:
        def fail_observer(_turn: int, _stage: str, _bucket: str) -> None:
            raise RuntimeError("acceptance sink unavailable")

        transport, engine = FakeTransport(), FakeEngine("JOAT, hello")
        orch = VoiceOrchestrator(
            transport,
            engine,
            vad=TEST_VAD(),
            replier=StreamingReplier(["hello"]),
            wake_words=WAKE,
            latency_observer=fail_observer,
        )

        await orch._handle_utterance(LOUD)

        assert assistant_text(transport) == ["hello"]

    async def test_streamed_reply_frames_are_cumulative_and_finish_ready_without_a_settle_timer(self) -> None:
        transport, engine = FakeTransport(), FakeEngine("tell me a story")
        orch = VoiceOrchestrator(
            transport, engine, vad=TEST_VAD(),
            replier=StreamingReplier(["Once ", "upon ", "a time. \n"]),
            stream_publish_interval_s=0.0,
        )
        orch.attach()
        assert transport.audio_cb is not None
        for i, pcm in enumerate([LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]):
            transport.audio_cb(pcm, i)
        await drain()

        views = conversation_views(transport)
        assert [view.state for view in views] == [
            "thinking", "answering", "answering", "answering", "ready",
        ]
        assert [view.turns[-1].text for view in views[1:]] == [
            "Once", "Once upon", "Once upon a time.", "Once upon a time.",
        ]
        assert not any(isinstance(message, AssistantOverlay) for message in transport.sent)
        assert conversation_role_text(transport, "user") == ["tell me a story"]

    async def test_streamed_frames_are_coalesced_to_the_publish_interval(self) -> None:
        """A token-at-a-time CLI must not re-send the whole transcript per token."""
        transport, engine = FakeTransport(), FakeEngine("tell me a story")
        clock = [100.0]
        orch = VoiceOrchestrator(
            transport, engine, vad=TEST_VAD(),
            replier=StreamingReplier(["Once ", "upon ", "a ", "time."]),
            stream_publish_interval_s=0.2, monotonic=lambda: clock[0],
        )
        orch.attach()
        assert transport.audio_cb is not None
        for i, pcm in enumerate([LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]):
            transport.audio_cb(pcm, i)
        await drain()

        views = conversation_views(transport)
        # The first chunk goes out at once; the rest ride the final frame.
        assert [view.state for view in views] == ["thinking", "answering", "ready"]
        assert [view.turns[-1].text for view in views[1:]] == ["Once", "Once upon a time."]

    async def test_streamed_frames_resume_once_the_publish_interval_has_passed(self) -> None:
        transport, engine = FakeTransport(), FakeEngine("tell me a story")
        clock = [100.0]

        class PacedReplier(ReplyEngine):
            async def reply(self, text: str) -> str | None:
                return None

            async def stream(self, text: str):
                yield "Once "
                clock[0] += 0.05
                yield "upon "       # 50 ms later: held
                clock[0] += 0.3
                yield "a time."     # 350 ms after the first frame: sent

        orch = VoiceOrchestrator(
            transport, engine, vad=TEST_VAD(), replier=PacedReplier(),
            stream_publish_interval_s=0.2, monotonic=lambda: clock[0],
        )
        orch.attach()
        assert transport.audio_cb is not None
        for i, pcm in enumerate([LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]):
            transport.audio_cb(pcm, i)
        await drain()

        views = conversation_views(transport)
        assert [view.state for view in views] == ["thinking", "answering", "answering", "ready"]
        assert [view.turns[-1].text for view in views[1:]] == [
            "Once", "Once upon a time.", "Once upon a time.",
        ]

    async def test_single_chunk_stream_has_thinking_answering_and_ready_states(self) -> None:
        transport, engine = FakeTransport(), FakeEngine("hi")
        orch = VoiceOrchestrator(transport, engine, vad=TEST_VAD(), replier=FakeReplier("Hello!"))
        orch.attach()
        assert transport.audio_cb is not None
        for i, pcm in enumerate([LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]):
            transport.audio_cb(pcm, i)
        await drain()
        views = conversation_views(transport)
        assert [view.state for view in views] == ["thinking", "answering", "ready"]
        assert views[-1].turns[-1] == AssistantConversationTurn("assistant", "Hello!")
        assert not any(isinstance(message, AssistantOverlay) for message in transport.sent)
        assert conversation_role_text(transport, "user") == ["hi"]

    async def test_long_answer_and_follow_up_remain_in_one_ordered_conversation_without_512_loss(self) -> None:
        transport = FakeTransport()
        engine = FakeEngine("JOAT, explain the sky")
        long_answer = "Blue light scatters through the atmosphere. " * 20

        class Replies(ReplyEngine):
            async def reply(self, text: str) -> str | None:
                return None

            async def stream(self, text: str):
                yield long_answer if "sky" in text else "A photon checked into a hotel and had no luggage."

        orch = VoiceOrchestrator(
            transport, engine, vad=TEST_VAD(), replier=Replies(), wake_words=WAKE,
        )
        await orch._handle_utterance(LOUD)
        current = conversation_views(transport)[-1]
        assert orch.handle_conversation_action(
            "continue", current.conversation_id, current.revision,
        ) is True
        engine.text = "and tell me a joke"
        await orch._handle_utterance(LOUD)

        views = conversation_views(transport)
        assert len({view.conversation_id for view in views}) == 1
        assert views[-1].turns == (
            AssistantConversationTurn("user", "explain the sky"),
            AssistantConversationTurn("assistant", long_answer.strip()),
            AssistantConversationTurn("user", "and tell me a joke"),
            AssistantConversationTurn(
                "assistant", "A photon checked into a hotel and had no luggage.",
            ),
        )
        assert len(views[-1].turns[1].text) > 512
        assert not any(isinstance(message, AssistantOverlay) for message in transport.sent)

    async def test_delayed_action_from_an_old_conversation_cannot_close_the_current_one(self) -> None:
        transport = FakeTransport()
        engine = FakeEngine("JOAT, first question")
        orch = VoiceOrchestrator(
            transport, engine, vad=TEST_VAD(), replier=FakeReplier("First answer"), wake_words=WAKE,
        )
        await orch._handle_utterance(LOUD)
        old = conversation_views(transport)[-1]
        assert orch.handle_conversation_action(
            "exit", old.conversation_id, old.revision,
        ) is True

        engine.text = "JOAT, second question"
        await orch._handle_utterance(LOUD)
        current = conversation_views(transport)[-1]
        assert current.conversation_id != old.conversation_id

        assert orch.handle_conversation_action(
            "exit", old.conversation_id, old.revision,
        ) is False
        assert orch.handle_conversation_action(
            "continue", current.conversation_id, current.revision,
        ) is True


def region(transport, container: ContainerId) -> list[str]:
    """Text written to one dashboard region (docs/HUD_DASHBOARD.md)."""
    return [m.text for m in transport.sent
            if isinstance(m, HudText) and m.container_id is container]


def conversation_views(transport) -> list[AssistantConversationView]:
    return [m for m in transport.sent if isinstance(m, AssistantConversationView)]


def conversation_role_text(transport, role: str) -> list[str]:
    views = conversation_views(transport)
    if not views:
        return []
    return [turn.text for turn in views[-1].turns if turn.role == role]


def assistant_text(transport) -> list[str]:
    """Assistant rows retained in the latest one-page conversation."""
    return conversation_role_text(transport, "assistant")


WAKE = ("hey joat", "joat")


class TestAcknowledgement:
    """The wearer must know they were heard BEFORE the answer exists.

    On real hardware transcription took 24s, and an unchanged HUD for that
    long is indistinguishable from a dead assistant — the wearer reported
    exactly that ("nothing happens").
    """

    async def test_status_bar_acknowledges_before_the_transcript(self) -> None:
        transport, engine = FakeTransport(), FakeEngine("what is the weather")
        orch = VoiceOrchestrator(transport, engine, vad=TEST_VAD(),
                                 replier=FakeReplier("72 degrees"))
        orch.attach()
        assert transport.audio_cb is not None
        for i, pcm in enumerate([LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]):
            transport.audio_cb(pcm, i)
        await drain()
        hud = transport.sent
        status = [m for m in hud if isinstance(m, HudText) and m.container_id is ContainerId.STATUS_BAR]
        assert status, "no acknowledgement — the wearer sees a dead HUD while waiting"
        # And it lands before the native transcript claims the page.
        assert hud.index(status[0]) < next(
            i for i, m in enumerate(hud)
            if isinstance(m, AssistantConversationView))


class TestWakeWordGate:
    async def _run(self, text: str, wake: tuple[str, ...] | None = WAKE):
        transport, engine = FakeTransport(), FakeEngine(text)
        orch = VoiceOrchestrator(
            transport, engine, vad=TEST_VAD(), replier=FakeReplier("answer"), wake_words=wake
        )
        orch.attach()
        assert transport.audio_cb is not None
        for i, pcm in enumerate([LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]):
            transport.audio_cb(pcm, i)
        await drain()
        view = conversation_views(transport)
        return [turn.text for turn in view[-1].turns] if view else []

    async def test_unaddressed_room_speech_is_fully_ignored(self) -> None:
        texts = await self._run("I'm so tired of repeating this to the team.")
        assert texts == []  # no HUD write, no reply, nothing

    async def test_ambient_rhymes_do_not_cross_the_wake_boundary(self) -> None:
        for speech in (
            "Coat, note payroll cutoff",
            "Goat, export notes",
            "Boat, complete reminder taxes",
        ):
            assert await self._run(speech) == []

    async def test_addressed_speech_answers_with_wake_word_stripped(self) -> None:
        texts = await self._run("JOAT, what time is sunset?")
        assert texts == ["what time is sunset?", "answer"]

    async def test_multiword_wake_word_strips_whole_prefix(self) -> None:
        texts = await self._run("Hey JOAT what is the weather")
        assert texts == ["what is the weather", "answer"]

    async def test_wake_word_alone_prompts(self) -> None:
        texts = await self._run("JOAT.")
        assert texts == ["JOAT", "Yes?"]

    async def test_none_gate_answers_everything(self) -> None:
        texts = await self._run("any old speech", wake=None)
        assert texts == ["any old speech", "answer"]

    async def test_spoken_message_draft_never_reaches_logs(self, caplog) -> None:  # type: ignore[no-untyped-def]
        caplog.set_level(logging.INFO)
        secret = "reply saying the locker code is indigo"
        transport, engine = FakeTransport(), FakeEngine(f"JOAT, {secret}")

        async def commands(query: str) -> str | None:
            assert query == secret
            return "Reply cancelled."

        orch = VoiceOrchestrator(
            transport,
            engine,
            wake_words=WAKE,
            commands=commands,
        )
        await orch._handle_utterance(LOUD)

        assert secret not in caplog.text
        assert "transcript received chars=" in caplog.text

    def _session_orch(self, heard: str):  # type: ignore[no-untyped-def]
        transport, engine = FakeTransport(), FakeEngine(heard)
        started: list[str] = []

        class S(Session):
            async def run(self) -> None:
                started.append("ran")

            def cancel(self) -> None: ...

        from joat.services.hud import HudWriter as HW
        from joat.services.session import SessionManager as SM
        orch = VoiceOrchestrator(
            transport, engine, vad=TEST_VAD(), replier=FakeReplier("x"),
            sessions=SM(HW(transport), lambda t: S()), wake_words=WAKE,
        )
        orch.attach()
        assert transport.audio_cb is not None
        for i, pcm in enumerate([LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]):
            transport.audio_cb(pcm, i)
        return started

    async def test_bare_debate_in_room_speech_does_NOT_start_a_session(self) -> None:
        """The audit's two passes both confirmed this bypass: session routing
        ran before the wake gate, so overheard speech beginning with
        'debate ' shipped the rest of the utterance to the cloud CLIs
        without the wearer ever addressing JOAT. Explicit invocation means
        triggers see only ADDRESSED text."""
        started = self._session_orch("debate tabs or spaces")
        await drain()
        assert started == []

    async def test_addressed_debate_still_starts_a_session(self) -> None:
        started = self._session_orch("JOAT, debate tabs or spaces")
        await drain()
        assert started == ["ran"]


class TestInterposedFrameSource:
    async def test_attach_prefers_the_injected_frame_source(self) -> None:
        """With an AudioRouter interposed, the orchestrator must subscribe to
        the router — never directly to the transport — or an exclusive
        QuickNote capture could not silence conversation."""
        transport = FrameTransport()

        class Frames(AudioFrameSource):
            def __init__(self) -> None:
                self.callback: AudioFrameCallback | None = None

            def on_audio_frame(self, callback: AudioFrameCallback) -> None:
                self.callback = callback

        frames = Frames()
        orch = VoiceOrchestrator(
            transport, stt=FakeEngine(), stt_available=False, frames=frames,
        )
        orch.attach()

        assert frames.callback is not None
        assert transport.frame_cb is None
        assert transport.audio_cb is None


class TestQuickNoteVoicePath:
    async def test_glasses_quicknote_command_arrives_wake_stripped(self) -> None:
        """The glasses chain's orchestrator half: "JOAT, start a quicknote…"
        from the G2 mic reaches the command router with the wake word
        stripped, and the router's receipt is what lands on the HUD."""
        transport = FakeTransport()
        engine = FakeEngine("JOAT, start a quicknote called Standup.")
        commands_seen: list[str] = []

        async def commands(text: str) -> str | None:
            commands_seen.append(text)
            return "Recording Voice Note: Standup."

        orch = VoiceOrchestrator(
            transport,
            engine,
            vad=TEST_VAD(),
            wake_words=("joat",),
            commands=commands,
        )
        orch.attach()
        assert transport.audio_cb is not None
        for i, pcm in enumerate([LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]):
            transport.audio_cb(pcm, i)
        await drain()

        assert commands_seen == ["start a quicknote called Standup."]
        assert assistant_text(transport) == ["Recording Voice Note: Standup."]


class TestTheWakeGateCatchesItsOwnName:
    """`whisper_serve` documents what Whisper does to this name — renders
    it "Jode"/"Jodh" — and the fuzzy fallback scored both at 0.50 against
    "joat", so it could not catch either failure the project had already
    written down. The wearer says JOAT and is silently ignored.

    The 0.86 threshold is deliberate and unchanged: 0.72 would admit
    coat/goat/boat and turn room speech into local writes, which is the
    ambient-capture hazard the wake gate exists for. The known renderings
    are matched directly instead.
    """

    @staticmethod
    def _wakes(word: str) -> bool:
        import difflib
        import inspect

        from joat.services.orchestrator import VoiceOrchestrator

        sig = inspect.signature(VoiceOrchestrator.__init__)
        cores = sig.parameters["fuzzy_wake"].default
        threshold = sig.parameters["fuzzy_threshold"].default
        if len(word) < 4:
            return False  # the orchestrator skips short words outright
        return any(
            difflib.SequenceMatcher(None, word, core).ratio() >= threshold
            for core in cores
        )

    def test_the_documented_mis_renderings_wake_it(self) -> None:
        for heard in ("joat", "jode", "jodh"):
            assert self._wakes(heard), heard

    def test_ordinary_room_speech_still_does_not(self) -> None:
        # The reason the threshold is 0.86 rather than 0.72, and adding
        # cores must not quietly undo it.
        for ambient in ("coat", "goat", "boat", "code", "node", "mode",
                        "rode", "road", "joke", "jade", "note", "vote",
                        "load", "toad", "jodhpur"):
            assert not self._wakes(ambient), ambient


class TestOrdinaryEnglishWakeWordsMustOpenTheUtterance:
    """Three wake words are also ordinary English.

    They are in the list because Whisper renders JOAT as them, which is
    real. But matched ANYWHERE, "I really liked your jokes last night",
    "give the door a jolt" and "we should jot that down" were speech
    addressed to the assistant — stripped of that word and answered.

    The fuzzy path is held to 0.86 precisely so coat/goat/boat cannot do
    this. The exact path was admitting three such words outright, which
    made the strictness next to it decorative.
    """

    @staticmethod
    async def _addressed(text: str) -> str | None:
        from joat.services.orchestrator import VoiceOrchestrator

        class Transport:
            async def send(self, *a: object, **k: object) -> None: ...

        o = VoiceOrchestrator(
            Transport(),  # type: ignore[arg-type]
            stt=None, stt_available=False,
            wake_words=("hey joat", "joat", "jote", "jolt", "jokes", "jot"),
        )
        return o._strip_wake_word(text)

    async def test_conversation_containing_them_is_not_addressed(self) -> None:
        for ambient in (
            "I really liked your jokes last night",
            "he tells terrible jokes",
            "give the door a jolt",
            "the comedian told jokes about goats",
            "we should jot that in the notes app later",
            "that was a jolt of caffeine honestly",
        ):
            assert await self._addressed(ambient) is None, ambient

    async def test_a_mis_rendering_that_opens_the_utterance_still_wakes(self) -> None:
        """What the words are in the list FOR."""
        for heard in ("jokes what is the weather",
                      "jot remind me at six",
                      "so jokes tell me the time"):
            assert await self._addressed(heard) is not None, heard

    async def test_the_assistants_own_name_is_never_anchored(self) -> None:
        """No way of addressing JOAT by name may be lost — it is not an
        ordinary word, so it carries no ambient risk wherever it lands."""
        for heard in ("JOAT what is the weather",
                      "hey JOAT open the terminal",
                      "so JOAT tell me the time",
                      "I was wondering JOAT what is next",
                      "okay so anyway JOAT what is on my calendar"):
            assert await self._addressed(heard) is not None, heard


class TestTheRestartGreetingGivesFOLLOWABLEAdvice:
    """After an engine restart mid-Brainstorm the greeting names the
    interrupted session and tells the wearer what to SAY to get back to
    it. That sentence is a promise, and nothing checked the code could
    keep it: the title is interpolated verbatim, so a title format or a
    `continue` grammar change turns the greeting into instructions that
    do nothing — the exact "advice that cannot be followed" this codebase
    keeps having to remove.

    So the test does not hard-code the phrasing. It reads the command out
    of the greeting the orchestrator actually paints and feeds it to the
    session manager the way the orchestrator feeds real speech.
    """

    class Past:
        status = "active"
        session_id = "aaaa"

        def __init__(self, title: str) -> None:
            self.title = title
            self.topic = title
            self.turns = ()
            self.parent_id = ""
            self.root_id = "aaaa"

    class Stt:
        async def transcribe(self, pcm: bytes) -> None:
            return None

    @staticmethod
    def _spoken_command(painted: str) -> str:
        """The words between the curly quotes after "Say"."""
        import re

        found = re.search(r"Say\s+[“\"']([^”\"']+)[”\"']", painted)
        assert found is not None, f"the greeting stopped naming a command: {painted}"
        return found.group(1)

    @pytest.mark.parametrize(
        "title",
        [
            "cold-start latency",          # a hyphen
            "tabs or spaces",              # plain words
            "should we cache the synthesis?",   # punctuation
            "GPT-5 vs Claude: which critic",    # colon and digits
        ],
    )
    async def test_the_named_command_actually_resumes_that_session(
        self, title: str,
    ) -> None:
        from joat.core.protocol import HudText
        from joat.services.hud import HudWriter
        from joat.services.session import Session, SessionManager

        past = self.Past(title)

        class History:
            def sessions(self, limit: int = 5):
                return [past]

            def find_all(self, spoken: str):
                said = " ".join(spoken.lower().split())
                topic = past.topic.lower()
                return [past] if (said in topic or topic in said) else []

            def load(self, session_id: str):
                return past if session_id == past.session_id else None

        resumed: list[str] = []

        class Live(Session):
            session_id = parent_id = root_id = ""

            async def run(self) -> None:
                await asyncio.sleep(0.01)

            def cancel(self) -> None: ...

            def consume_transcript(self, text: str) -> bool:
                return True

            def direct(self, text: str) -> str | None:
                return None

        def continuation(topic: str, prior):
            resumed.append(topic)
            return Live()

        transport = FakeTransport()
        manager = SessionManager(
            HudWriter(transport), lambda t: None,
            history=History(), continuation_factory=continuation,
            brainstorm_available=lambda: True,
        )
        orchestrator = VoiceOrchestrator(
            transport=transport, stt=self.Stt(), sessions=manager,
            stt_available=True,
        )
        await orchestrator.on_connect()

        painted = " ".join(
            m.text for m in transport.sent
            if isinstance(m, HudText) and "interrupted" in m.text
        )
        assert painted, "no interrupted-session greeting was painted"

        # Exactly what the orchestrator strips before calling: the wake
        # word is already gone by the time a trigger phrase is matched.
        command = self._spoken_command(painted)
        assert manager.handle_transcript(command) is True, (
            f"the greeting said {command!r} and the manager ignored it"
        )
        await asyncio.sleep(0.05)
        assert resumed == [title]
        await manager.close()


class TestAVoiceTurnThatRaisesDoesNotVanish:
    """The same bare-task silence as the capability dispatch and the
    terminal retry — but on the MAIN VOICE PATH, where it costs most.

    `_drain_utterances` was `try`/`finally` with no `except`, so any
    handler that raised ended the turn as an unhandled task exception. The
    last thing painted is the "listening" status from the start of the
    turn, so the wearer is left watching a screen that says JOAT is
    listening, forever, with no way to tell a dead turn from a long think.
    The loop itself always survived; only the wearer was left out.
    """

    class Stt:
        async def feed(self, pcm: bytes) -> None:
            return None

        async def flush(self):
            return Transcript(
                text="JOAT, brainstorm should we cache",
                confidence=0.9, is_final=True,
            )

    class Exploding:
        active = False
        active_status = None
        topic_capture_armed = False

        def handle_transcript(self, text: str, **kw: object) -> bool:
            raise RuntimeError("codex binary vanished")

        def interrupted_session(self) -> None:
            return None

        async def replay_active_hud(self) -> bool:
            return False

    async def test_the_wearer_is_told_and_the_worker_survives(self) -> None:
        from joat.core.protocol import AssistantOverlay

        transport = FakeTransport()
        orchestrator = VoiceOrchestrator(
            transport=transport, stt=self.Stt(), sessions=self.Exploding(),
            stt_available=True, wake_words=("joat",),
        )

        # The real worker, the way the audio path creates it.
        await orchestrator._drain_utterances(b"\x00" * 3200, None)

        spoken = [
            m.text for m in transport.sent
            if isinstance(m, AssistantOverlay) and m.text
        ]
        assert spoken, "the turn died silently"
        assert "say it again" in spoken[-1].lower()
        assert orchestrator._utterance_worker is None, (
            "the worker did not release for the next utterance"
        )

    async def test_the_notice_carries_no_exception_text(self) -> None:
        """The exception can carry a prompt, a transcript fragment or a
        provider payload. None of that belongs on the lenses."""
        from joat.core.protocol import AssistantOverlay

        transport = FakeTransport()
        orchestrator = VoiceOrchestrator(
            transport=transport, stt=self.Stt(), sessions=self.Exploding(),
            stt_available=True, wake_words=("joat",),
        )
        await orchestrator._drain_utterances(b"\x00" * 3200, None)

        spoken = " ".join(
            m.text for m in transport.sent
            if isinstance(m, AssistantOverlay) and m.text
        )
        assert "codex binary vanished" not in spoken

    async def test_cancellation_still_cancels(self) -> None:
        """Shutdown must not be swallowed as a failed turn."""
        class Cancelling(self.Exploding):
            def handle_transcript(self, text: str, **kw: object) -> bool:
                raise asyncio.CancelledError

        transport = FakeTransport()
        orchestrator = VoiceOrchestrator(
            transport=transport, stt=self.Stt(), sessions=Cancelling(),
            stt_available=True, wake_words=("joat",),
        )
        with pytest.raises(asyncio.CancelledError):
            await orchestrator._drain_utterances(b"\x00" * 3200, None)


class TestTheConversationWindow:
    """After JOAT answers, the wearer keeps talking — not repeating its name.

    The wearer's own log, 2026-09-08: an addressed request at 17:31:07 was
    answered, then a 157-character follow-up at 17:31:23 and a 51-character
    one at 17:31:43 were both discarded as unaddressed. "If I don't say it a
    certain way, it's useless."
    """

    def gate(self, seconds: float = 30.0) -> tuple[VoiceOrchestrator, object]:
        class Clock:
            t = 1_000.0
            def __call__(self) -> float:
                return self.t
        clock = Clock()
        o = VoiceOrchestrator.__new__(VoiceOrchestrator)
        o._wake_words = ("hey joat", "joat")
        o._anchored_wake = ("jokes", "jot", "jolt")
        o._anchor_window = 2
        o._fuzzy_wake = ("joat", "jode", "jodh")
        o._fuzzy_threshold = 0.86
        o._conversation = ConversationWindow(seconds, clock)
        return o, clock

    def test_a_follow_up_after_a_reply_is_heard_without_the_name(self) -> None:
        o, clock = self.gate()
        assert o._addressed("JOAT what's on my calendar") == "what's on my calendar"
        o._conversation.open()          # JOAT replied
        clock.t += 16                   # the wearer's real pause
        assert o._addressed("and what about tomorrow") == "and what about tomorrow"

    def test_each_reply_re_opens_it_so_a_real_exchange_flows(self) -> None:
        o, clock = self.gate()
        o._conversation.open()
        for pause, said in ((16, "and what about tomorrow"),
                            (20, "move the second one to friday"),
                            (25, "no the other one")):
            clock.t += pause
            assert o._addressed(said) == said, said
            o._conversation.open()      # JOAT answered again

    def test_silence_closes_it_and_room_speech_is_not_a_prompt(self) -> None:
        o, clock = self.gate()
        o._conversation.open()
        clock.t += 31
        # Past the window the wearer is no longer in a conversation, so the
        # room is the room again. This is the boundary that keeps explicit
        # invocation true (RULES.md) rather than a standing invitation.
        assert o._addressed("so anyway I told him it was fine") is None

    def test_it_never_opens_on_unaddressed_speech(self) -> None:
        o, _ = self.gate()
        # Nothing has addressed JOAT, so nothing may be heard — the window
        # is a continuation of an invitation, never a substitute for one.
        assert o._addressed("tokens was wasted on and you didn't get it done") is None
        assert not o._conversation.is_open

    def test_a_zero_window_restores_the_old_wake_every_time_rule(self) -> None:
        o, _ = self.gate(seconds=0.0)
        o._conversation.open()
        assert not o._conversation.is_open
        assert o._addressed("and what about tomorrow") is None

    def test_an_open_window_lets_the_wearer_interrupt_a_streaming_reply(self) -> None:
        o, _ = self.gate()
        o._conversation.open()
        # Cutting a long answer short is the most conversational act there
        # is; it must not require the wake word either.
        assert o._interrupts("actually never mind that, what about friday") == "addressed"


class TestASentenceSplitByABreath:
    """One thought, paused mid-sentence, is one request.

    The wearer, 2026-09-08: "I won't get done talking before it responds...
    it is still dropping context." Their log at 17:40:44-17:40:50 shows why
    — one spoken thought arrived as transcripts of 262, 17 and 25
    characters, each answered on its own, because the VAD closes an
    utterance after 1.0s of quiet and an ordinary pause is longer than that.
    """

    def frag(
        self, text: str, seconds: float, endpointed_at: float,
        first: int, last: int, generation: str = "g1", epoch: str = "e1",
    ) -> tuple[Any, Any]:
        provenance = UtteranceProvenance(
            "glasses", generation, epoch, first, last, frozenset({"self"}),
        )
        return (
            _QueuedUtterance(
                b"\0" * int(seconds * BYTES_PER_SECOND), "glasses",
                provenance, endpointed_at,
            ),
            _Transcribed(
                _Turn(1, endpointed_at, 0, None), Transcript(text, True),
                "glasses", provenance,
            ),
        )

    def orchestrator(self) -> VoiceOrchestrator:
        return VoiceOrchestrator.__new__(VoiceOrchestrator)

    def test_a_pause_mid_sentence_does_not_end_the_request(self) -> None:
        o = self.orchestrator()
        _, head = self.frag("JOAT check my calendar for tomorrow and also", 3.4, 100.0, 0, 20)
        tail_q, _ = self.frag("tell me if I have anything", 1.2, 102.0, 21, 28)
        assert o._continues(head, 100.0, tail_q)

    def test_a_thought_may_be_joined_more_than_once(self) -> None:
        o = self.orchestrator()
        _, head = self.frag("JOAT check my calendar for tomorrow and also", 3.4, 100.0, 0, 20)
        q2, t2 = self.frag("tell me if I have anything", 1.2, 102.0, 21, 28)
        q3, t3 = self.frag("in the afternoon", 0.9, 103.4, 29, 34)
        assert o._continues(head, 100.0, q2)
        joined = _joined(head, t2)
        # The merged provenance has to carry the ingress span forward, or a
        # third fragment looks like it skipped everything the second one
        # carried and the sentence can only ever be joined twice.
        assert o._continues(joined, 102.0, q3)
        joined = _joined(joined, t3)
        assert joined.transcript.text == (
            "JOAT check my calendar for tomorrow and also "
            "tell me if I have anything in the afternoon"
        )

    def test_the_joined_request_keeps_the_opening_turn(self) -> None:
        _, head = self.frag("JOAT check my calendar", 3.4, 100.0, 0, 20)
        _, tail = self.frag("for tomorrow", 1.2, 102.0, 21, 28)
        # The wearer's wait began when they started speaking; restarting the
        # latency clock at every breath would report a fast reply to someone
        # who had been waiting since the first word.
        assert _joined(head, tail).turn is head.turn

    def test_a_genuinely_new_request_is_not_glued_to_the_last_one(self) -> None:
        o = self.orchestrator()
        _, head = self.frag("JOAT check my calendar", 3.4, 100.0, 0, 20)
        later, _ = self.frag("what's the weather", 1.0, 130.0, 60, 66)
        assert not o._continues(head, 100.0, later)

    def test_dropped_frames_are_never_spliced_over(self) -> None:
        o = self.orchestrator()
        _, head = self.frag("JOAT check my calendar", 3.4, 100.0, 0, 20)
        # Ordinals jumped: audio between the fragments never arrived, so a
        # join would invent the continuity it claims to have observed.
        gapped, _ = self.frag("for tomorrow", 1.2, 102.0, 40, 46)
        assert not o._continues(head, 100.0, gapped)

    def test_a_reconnect_boundary_is_never_spliced_over(self) -> None:
        o = self.orchestrator()
        _, head = self.frag("JOAT check my calendar", 3.4, 100.0, 0, 20)
        reconnected, _ = self.frag("for tomorrow", 1.2, 102.0, 21, 28, generation="g2")
        assert not o._continues(head, 100.0, reconnected)
        fresh_epoch, _ = self.frag("for tomorrow", 1.2, 102.0, 21, 28, epoch="e2")
        assert not o._continues(head, 100.0, fresh_epoch)

    def test_the_gap_is_measured_from_audio_not_from_transcription(self) -> None:
        o = self.orchestrator()
        _, head = self.frag("JOAT check my calendar", 3.4, 100.0, 0, 20)
        # A long fragment endpointed far later is still continuous speech:
        # nine seconds of talking, not nine seconds of silence. Comparing
        # endpoint timestamps alone would split a sentence nobody paused in.
        long_tail, _ = self.frag("for tomorrow and the day after", 9.0, 109.5, 21, 28)
        assert o._continues(head, 100.0, long_tail)
