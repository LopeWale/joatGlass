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
import difflib
import logging
from collections.abc import Awaitable, Callable
import re

from ..audio.vad import RmsVad
from ..core.interfaces import GlassesTransport, ReplyEngine, SttEngine
from ..core.protocol import AudioControl, ContainerId, HudText
from .hud import HudWriter
from .session import SessionManager

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
        sessions: SessionManager | None = None,
        wake_words: tuple[str, ...] | None = None,
        status: object | None = None,
        commands: Callable[[str], Awaitable[str | None]] | None = None,
        fuzzy_wake: tuple[str, ...] = ("joat",),
        fuzzy_threshold: float = 0.72,
    ) -> None:
        self._transport = transport
        self._commands = commands
        self._hud = HudWriter(transport)
        self._sessions = sessions
        self._status = status
        # STT mangles a one-word wake utterance differently every time
        # (observed: Jeves, Javes, Javits, Jokes). A curated alias list can
        # never be complete, so near-misses are matched by similarity too.
        # 0.72 was chosen against live mishearings: it accepts javits/javes/
        # jote and rejects service/driving/job/just/jazz/javascript.
        self._fuzzy_wake = fuzzy_wake
        self._fuzzy_threshold = fuzzy_threshold
        # Longest first so "hey joat" strips before "joat" matches.
        self._wake_words = (
            tuple(sorted(wake_words, key=len, reverse=True)) if wake_words else None
        )
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
        wake_hint = " — say 'JOAT ...'" if self._wake_words else ""
        status = (f"JOAT · online{wake_hint}" if self._stt_available
                  else "JOAT · online (no STT)")
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
            # Acknowledge FIRST. Transcription takes seconds, and an
            # unchanged HUD for that long reads as "the assistant is dead" —
            # the wearer has no other signal that they were heard.
            await self._hud.status("JOAT · hearing you…")
            await self._stt.feed(utterance)
            transcript = await self._stt.flush()
            if transcript is None or not transcript.text:
                log.info("STT produced no text")
                return
            log.info("transcript: %s", transcript.text)
            if self._status is not None:
                self._status.set(last_transcript=transcript.text)  # type: ignore[attr-defined]
                self._status.bump("utterances")  # type: ignore[attr-defined]
            # An ACTIVE session owns the HUD and consumes transcripts
            # ("stop", turn directions) without needing the wake word — the
            # wearer already invoked it.
            if (self._sessions is not None and self._sessions.active
                    and self._sessions.handle_transcript(transcript.text)):
                return
            # Address gate BEFORE any trigger phrase: session routing used to
            # run first, so room speech merely beginning with "debate " (or a
            # notification read aloud) started a cloud debate and shipped the
            # rest of the utterance to the codex/claude subprocesses without
            # the wearer ever addressing JOAT — observed live, and
            # a direct violation of explicit invocation. Room conversation is
            # not a prompt.
            addressed = self._strip_wake_word(transcript.text)
            if addressed is None:
                # Include the text: a wake word misheard by STT is
                # indistinguishable from room noise without it.
                log.info("unaddressed speech ignored: %s", transcript.text)
                return
            if not addressed:
                await self._hud.show("Yes?")
                return
            query = addressed
            # Trigger phrases ("debate <topic>") now see only ADDRESSED text.
            if self._sessions is not None and self._sessions.handle_transcript(query):
                return
            # Settings the wearer can change by speaking ("use sonnet for the
            # critic"). Handled BEFORE the reply engine because a config
            # change is a deterministic action, not something to ask a 3B
            # model to interpret — and because the LLM would otherwise
            # cheerfully claim to have switched a model it cannot touch.
            if self._commands is not None:
                answer = await self._commands(query)
                if answer is not None:
                    await self._hud.context(f"you  {query}")
                    await self._hud.show(answer)
                    return
            # The wearer's words go to their OWN region, so the reply no
            # longer erases the proof they were heard (HUD_DASHBOARD.md).
            await self._hud.context(f"you  {query}")
            if self._replier is None:
                return
            parts: list[str] = []
            async for chunk in self._replier.stream(query):
                if parts:
                    await self._hud.append(chunk)
                else:
                    await self._hud.show(chunk)
                parts.append(chunk)
            if parts:
                reply_text = "".join(parts)
                log.info("reply: %s", reply_text)
                if self._status is not None:
                    self._status.set(last_reply=reply_text)  # type: ignore[attr-defined]
        finally:
            self._busy = False

    async def watch_loop_lag(self, interval_s: float = 0.5,
                             warn_s: float = 0.75) -> None:
        """Log when the event loop is blocked longer than a wearer would wait.

        A blocked loop delays every await — the STT round trip included — so
        a slow reply looks like a slow model when it is really CPU
        contention. This measures the difference directly.
        """
        loop = asyncio.get_running_loop()
        while True:
            before = loop.time()
            await asyncio.sleep(interval_s)
            lag = loop.time() - before - interval_s
            if lag > warn_s:
                log.warning("event loop stalled %.2fs — replies delayed by at least this", lag)

    def _strip_wake_word(self, text: str) -> str | None:
        """None = not addressed to us; '' = wake word alone; else the query.

        The wake word may appear ANYWHERE, because people don't speak in
        command syntax: "JOAT, what's the weather", "Good morning, JOAT",
        and "hey JOAT can you..." are all addressed. Requiring a prefix
        silently ignored the wearer's real speech (observed live).

        Text-level gate on the transcript (v1). The audio-level VadWakeWord
        from MAC_APP.md replaces this later and stops even the transcription
        of unaddressed speech; until then, unaddressed audio is transcribed
        for this check but never answered, shown, or sent to an LLM.
        """
        if self._wake_words is None:
            return text
        words = re.findall(r"[a-z0-9]+", text.lower())
        if not words:
            return None
        for wake in self._wake_words:  # longest first
            wake_words = wake.split()
            n = len(wake_words)
            for i in range(len(words) - n + 1):
                if words[i:i + n] != wake_words:
                    continue
                # Addressed. Rebuild the query from the ORIGINAL text minus
                # the wake phrase, so casing and punctuation survive.
                pattern = r"\b" + r"[^a-z0-9]+".join(
                    re.escape(w) for w in wake_words) + r"\b"
                query = re.sub(pattern, " ", text, count=1, flags=re.IGNORECASE)
                query = re.sub(r"\s+", " ", query)
                # Removing the wake phrase can orphan its punctuation
                # ("Good morning, JOAT." -> "Good morning, ."). Reattach
                # and drop the orphan, but NEVER strip terminal ?/! — they
                # carry meaning the model should see.
                query = re.sub(r"\s+([,.!?])", r"\1", query)
                query = re.sub(r",+([.!?])", r"\1", query)
                query = re.sub(r"^[\s,.\-—]+", "", query)
                return query.rstrip(" ,-—")

        # No exact hit — try similarity against the core names.
        for i, word in enumerate(words):
            if len(word) < 4:
                continue  # short words collide with everything
            for core in self._fuzzy_wake:
                ratio = difflib.SequenceMatcher(None, word, core).ratio()
                if ratio < self._fuzzy_threshold:
                    continue
                log.info("wake word matched by similarity: %r ~ %r (%.2f)",
                         word, core, ratio)
                query = re.sub(r"\b" + re.escape(word) + r"\b", " ", text,
                               count=1, flags=re.IGNORECASE)
                query = re.sub(r"\s+", " ", query)
                query = re.sub(r"\s+([,.!?])", r"\1", query)
                query = re.sub(r",+([.!?])", r"\1", query)
                query = re.sub(r"^[\s,.\-—]+", "", query)
                return query.rstrip(" ,-—")
        return None
