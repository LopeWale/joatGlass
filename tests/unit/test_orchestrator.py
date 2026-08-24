"""VoiceOrchestrator flow tests with fake transport and engine."""

from __future__ import annotations

import asyncio
import struct

from jarvis.core.interfaces import (
    AudioCallback,
    DeviceCallback,
    GlassesTransport,
    InputCallback,
    SttEngine,
    Transcript,
)
from jarvis.core.protocol import AudioControl, DownlinkMessage, HudText
from jarvis.services.orchestrator import VoiceOrchestrator

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


async def drain() -> None:
    for _ in range(4):
        await asyncio.sleep(0)


class TestVoiceLoop:
    async def test_utterance_becomes_hud_text(self) -> None:
        transport, engine = FakeTransport(), FakeEngine("turn on the lights")
        orch = VoiceOrchestrator(transport, engine)
        orch.attach()
        assert transport.audio_cb is not None

        seq = 0
        for pcm in [LOUD, LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]:
            transport.audio_cb(pcm, seq)
            seq += 1
        await drain()

        assert len(transport.sent) == 1
        msg = transport.sent[0]
        assert isinstance(msg, HudText)
        assert msg.text == "turn on the lights"

    async def test_seq_gap_resets_vad_and_engine(self) -> None:
        transport, engine = FakeTransport(), FakeEngine()
        orch = VoiceOrchestrator(transport, engine)
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
        assert len(transport.sent) == 1

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
        assert isinstance(transport.sent[0], HudText)
        assert "no STT" in transport.sent[0].text
        assert transport.sent[1:] and isinstance(transport.sent[-1], AudioControl)
        assert transport.sent[-1].open is True
        assert len(transport.sent) == 3  # status, install hint, mic on


class FakeReplier:
    def __init__(self, text: str | None = "It is 72 degrees.") -> None:
        self.text = text
        self.asked: list[str] = []

    async def reply(self, text: str) -> str | None:
        self.asked.append(text)
        return self.text


class TestReplyFlow:
    async def test_transcript_then_reply_both_reach_the_hud(self) -> None:
        transport, engine = FakeTransport(), FakeEngine("what is the weather")
        replier = FakeReplier("It is 72 degrees.")
        orch = VoiceOrchestrator(transport, engine, replier=replier)  # type: ignore[arg-type]
        orch.attach()
        assert transport.audio_cb is not None
        for i, pcm in enumerate([LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]):
            transport.audio_cb(pcm, i)
        await drain()
        texts = [m.text for m in transport.sent if isinstance(m, HudText)]
        assert texts == ["what is the weather", "It is 72 degrees."]
        assert replier.asked == ["what is the weather"]

    async def test_replier_declining_leaves_transcript_standing(self) -> None:
        transport, engine = FakeTransport(), FakeEngine("hm")
        orch = VoiceOrchestrator(transport, engine, replier=FakeReplier(None))  # type: ignore[arg-type]
        orch.attach()
        assert transport.audio_cb is not None
        for i, pcm in enumerate([LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]):
            transport.audio_cb(pcm, i)
        await drain()
        texts = [m.text for m in transport.sent if isinstance(m, HudText)]
        assert texts == ["hm"]

    async def test_utterance_during_reply_is_dropped_single_flight(self) -> None:
        transport, engine = FakeTransport(), FakeEngine("first question")

        class SlowReplier:
            def __init__(self) -> None:
                self.gate = asyncio.Event()
                self.asked: list[str] = []

            async def reply(self, text: str) -> str | None:
                self.asked.append(text)
                await self.gate.wait()
                return "slow answer"

        replier = SlowReplier()
        orch = VoiceOrchestrator(transport, engine, replier=replier)  # type: ignore[arg-type]
        orch.attach()
        assert transport.audio_cb is not None

        seq = 0
        for pcm in [LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]:
            transport.audio_cb(pcm, seq); seq += 1
        await drain()
        # second utterance arrives while the reply is still generating
        for pcm in [LOUD, LOUD, QUIET, QUIET, QUIET, QUIET]:
            transport.audio_cb(pcm, seq); seq += 1
        await drain()
        replier.gate.set()
        await drain()

        assert replier.asked == ["first question"]  # second was dropped, not raced
        texts = [m.text for m in transport.sent if isinstance(m, HudText)]
        assert texts == ["first question", "slow answer"]
