"""VoiceOrchestrator flow tests with fake transport and engine."""

from __future__ import annotations

import asyncio
import logging
import struct

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
    AssistantOverlay,
    AudioControl,
    AudioSource,
    BrainstormAction,
    BrainstormHudView,
    ContainerId,
    DownlinkMessage,
    HudText,
)
from joat.services.orchestrator import VoiceOrchestrator
from joat.services.greetings import greeting_response
from joat.services.hud import HudWriter
from joat.services.session import Session, SessionManager

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
    ) -> None:
        assert self.frame_cb is not None
        self.frame_cb(AudioFrame(
            pcm=pcm,
            connection_generation="current",
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

        said = region(transport, ContainerId.CONTEXT)
        assert said == ["you  turn on the lights"]

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

        assert region(transport, ContainerId.CONTEXT) == ["you  hello from an unknown speaker"]

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
        assert len(region(transport, ContainerId.CONTEXT)) == 1

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
        capture = BrainstormHudView(
            screen="brainstorm", state="topic_capture", topic="Speak your question",
            collaborators=["Codex", "Claude", "Hermes"],
            activity="Your next spoken phrase starts this Brainstorm.",
            terminal_state="ready", no_tools_checked=True,
        )
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
        capture = BrainstormHudView(
            screen="brainstorm", state="topic_capture", topic="Speak your question",
            collaborators=["Codex", "Claude", "Hermes"],
            activity="Your next spoken phrase starts this Brainstorm.",
            terminal_state="ready", no_tools_checked=True,
        )
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
        assert region(transport, ContainerId.CONTEXT) == ["you  what is the weather"]
        assert replier.asked == ["what is the weather"]

    async def test_assistant_reply_is_an_overlay_not_the_messages_main_region(self) -> None:
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

        overlays = [message for message in transport.sent if isinstance(message, AssistantOverlay)]
        assert [(message.text, message.operation, message.dismiss_after_ms) for message in overlays] == [
            ("Design review at 4:30.", "replace", None),
            ("", "settle", 6_000),
        ]
        assert region(transport, ContainerId.MAIN) == []
        assert region(transport, ContainerId.CONTEXT) == ["you  what is next?"]

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
        assert region(transport, ContainerId.CONTEXT) == ["you  hm"]

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
        # Two finished utterances arrive while the resident reply engine is
        # occupied.  The one-slot handoff replaces the older phone utterance
        # with the newest glasses one, retaining its source for the G2 topic
        # capture security gate.
        sessions.topic_capture_armed = True
        emit_utterance(7000, "phone")
        emit_utterance(6000, "glasses")
        replier.gate.set()
        await drain()

        assert replier.asked == ["first question"]  # no overlapping reply work
        assert sessions.captured == [("latest question", "glasses")]
        assert assistant_text(transport) == ["slow answer"]
        assert region(transport, ContainerId.CONTEXT) == ["you  first question"]


class StreamingReplier(ReplyEngine):
    """True chunked streaming, like MlxReplyEngine.stream."""

    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks

    async def reply(self, text: str) -> str | None:
        return "".join(self.chunks)

    async def stream(self, text: str):
        for c in self.chunks:
            yield c


class TestStreamedReply:
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
        )

        await orch._handle_utterance(LOUD)

        assert [stage for _turn, stage, _bucket in latency_events] == [
            "utterance_ready",
            "ack_sent",
            "stt_complete",
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

        assert "hello" in [message.text for message in transport.sent]

    async def test_chunks_stream_as_show_then_appends(self) -> None:
        transport, engine = FakeTransport(), FakeEngine("tell me a story")
        orch = VoiceOrchestrator(
            transport, engine, vad=TEST_VAD(), replier=StreamingReplier(["Once ", "upon ", "a time."])
        )
        orch.attach()
        assert transport.audio_cb is not None
        for i, pcm in enumerate([LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]):
            transport.audio_cb(pcm, i)
        await drain()

        hud = [m for m in transport.sent if isinstance(m, AssistantOverlay)]
        # Assistant output is its own overlay: it streams above the feature,
        # then settles without rewriting that feature's main region.
        assert [(m.text, m.operation) for m in hud] == [
            ("Once ", "replace"),
            ("upon ", "append"),
            ("a time.", "append"),
            ("", "settle"),
        ]
        assert region(transport, ContainerId.CONTEXT) == ["you  tell me a story"]

    async def test_single_chunk_default_stream_still_replaces(self) -> None:
        transport, engine = FakeTransport(), FakeEngine("hi")
        orch = VoiceOrchestrator(transport, engine, vad=TEST_VAD(), replier=FakeReplier("Hello!"))
        orch.attach()
        assert transport.audio_cb is not None
        for i, pcm in enumerate([LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]):
            transport.audio_cb(pcm, i)
        await drain()
        hud = [m for m in transport.sent if isinstance(m, AssistantOverlay)]
        assert [(m.text, m.operation) for m in hud] == [
            ("Hello!", "replace"),
            ("", "settle"),
        ]
        assert region(transport, ContainerId.CONTEXT) == ["you  hi"]


def region(transport, container: ContainerId) -> list[str]:
    """Text written to one dashboard region (docs/HUD_DASHBOARD.md)."""
    return [m.text for m in transport.sent
            if isinstance(m, HudText) and m.container_id is container]


def assistant_text(transport) -> list[str]:
    """Visible reply copy, deliberately outside any feature-page region."""
    return [
        m.text for m in transport.sent
        if isinstance(m, AssistantOverlay) and m.operation in {"replace", "append"}
    ]


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
        # And it lands BEFORE the response reaches the assistant overlay.
        assert hud.index(status[0]) < next(
            i for i, m in enumerate(hud)
            if isinstance(m, AssistantOverlay) and m.operation == "replace")


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
        # The wearer's words land in CONTEXT prefixed "you  "; the reply is
        # in a separate overlay. Strip the prefix so these stay about wake.
        said = [s.removeprefix("you  ") for s in region(transport, ContainerId.CONTEXT)]
        return said + assistant_text(transport)

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
        assert texts == ["Yes?"]

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
