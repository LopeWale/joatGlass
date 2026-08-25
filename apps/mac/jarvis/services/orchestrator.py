"""Phase-0 voice loop: glasses mic → VAD → STT → transcript on the HUD.

No LLM yet — the transcript itself is echoed to the HUD, which is exactly
the ROADMAP.md Phase-0 proof ("round-trip voice → HUD"). When the MLX
reply lands, it slots between flush() and send() here and nothing else
moves.

Threading model: transport callbacks fire synchronously inside the running
event loop (HubGatewayTransport dispatches from its receive coroutine), so
on_audio() may not block — it segments synchronously and schedules the
async transcribe/send work as a task.
"""

from __future__ import annotations

import asyncio
import logging

from ..audio.vad import RmsVad
from ..core.interfaces import GlassesTransport, ReplyEngine, SttEngine
from ..core.protocol import AudioControl, ContainerId, HudText
from .hud import HudWriter

log = logging.getLogger(__name__)


class VoiceOrchestrator:
    """Wires one transport to one STT engine.

    stt_available should reflect reality (WhisperKitEngine.available); when
    False the loop still runs — utterances are detected and logged, and the
    wearer sees an honest status line instead of silence.
    """

    def __init__(
        self,
        transport: GlassesTransport,
        stt: SttEngine,
        vad: RmsVad | None = None,
        stt_available: bool = True,
        replier: ReplyEngine | None = None,
    ) -> None:
        self._transport = transport
        self._hud = HudWriter(transport)
        self._stt = stt
        self._vad = vad or RmsVad()
        self._stt_available = stt_available
        self._replier = replier
        self._last_seq: int | None = None
        self._busy = False

    def attach(self) -> None:
        self._transport.on_audio(self.on_audio)

    async def on_connect(self) -> None:
        """Greet the wearer so a live brain is visibly different from none."""
        status = "JOAT · brain online" if self._stt_available else "JOAT · brain online (no STT)"
        await self._transport.send(HudText(text=status, container_id=ContainerId.STATUS_BAR))
        if not self._stt_available:
            await self._transport.send(
                HudText(text="Install whisperkit-cli on the Mac to enable transcription.")
            )
        # Request ears: an earlier session (or doff) may have left the mic off,
        # and a brain that cannot hear is indistinguishable from none.
        await self._transport.send(AudioControl(open=True))

    def on_audio(self, pcm: bytes, seq: int) -> None:
        if self._last_seq is not None and seq != self._last_seq + 1:
            # Frames were shed upstream; never splice across the hole.
            log.warning("audio gap (seq %d → %d): resetting utterance", self._last_seq, seq)
            self._vad.reset()
            self._stt.reset()
        self._last_seq = seq

        utterance = self._vad.feed(pcm)
        if utterance is None:
            return
        if self._busy:
            # One transcription at a time; a new utterance while Whisper is
            # still chewing gets dropped with a log rather than queueing
            # unboundedly. Revisit when a streaming engine lands.
            log.warning("dropping utterance (%d bytes) — transcription in flight", len(utterance))
            return
        asyncio.get_running_loop().create_task(self._handle_utterance(utterance))

    async def _handle_utterance(self, utterance: bytes) -> None:
        seconds = len(utterance) / (16_000 * 2)
        log.info("utterance ended: %.1fs of audio", seconds)
        if not self._stt_available:
            log.info("no STT engine — utterance dropped")
            return
        # Busy covers STT AND the reply: overlapping generations raced the
        # MLX engine and produced garbage before this held to the end.
        self._busy = True
        try:
            await self._stt.feed(utterance)
            transcript = await self._stt.flush()
            if transcript is None or not transcript.text:
                log.info("STT produced no text")
                return
            log.info("transcript: %s", transcript.text)
            # Show the wearer their words immediately — proof they were heard —
            # then STREAM the reply over them chunk by chunk as it generates
            # (the same visible-streaming path Phase 2's debate turns use).
            await self._hud.show(transcript.text)
            if self._replier is None:
                return
            parts: list[str] = []
            async for chunk in self._replier.stream(transcript.text):
                if parts:
                    await self._hud.append(chunk)
                else:
                    await self._hud.show(chunk)
                parts.append(chunk)
            if parts:
                log.info("reply: %s", "".join(parts))
        finally:
            self._busy = False
