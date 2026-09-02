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
import time

from ..audio.vad import RmsVad
from ..core.interfaces import AudioFrame, AudioFrameSource, GlassesTransport, ReplyEngine, SttEngine
from ..core.protocol import AudioControl, AudioSource, ContainerId, HudText
from .hud import HudWriter
from .session import SessionManager

log = logging.getLogger(__name__)

LatencyObserver = Callable[[int, str, str], None]
_LATENCY_BUCKETS = (
    (0.25, "lte250ms"),
    (0.5, "250_500ms"),
    (1.0, "500ms_1s"),
    (2.0, "1_2s"),
    (5.0, "2_5s"),
    (10.0, "5_10s"),
)
_SPEAKER_ROLES = frozenset({"self", "other", "unknown"})


def _latency_bucket(elapsed_s: float) -> str:
    for upper_bound, label in _LATENCY_BUCKETS:
        if elapsed_s <= upper_bound:
            return label
    return "gt10s"


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
        fuzzy_wake: tuple[str, ...] = ("joat", "jode", "jodh"),
        anchored_wake: tuple[str, ...] = ("jokes", "jot", "jolt"),
        anchor_window: int = 2,
        fuzzy_threshold: float = 0.86,
        latency_observer: LatencyObserver | None = None,
        frames: AudioFrameSource | None = None,
    ) -> None:
        self._transport = transport
        # An interposed source (the AudioRouter) lets an exclusive Voice Note
        # capture own the microphone; the orchestrator hears nothing while a
        # meeting is being recorded and resumes when the capture lane closes.
        self._frames = frames
        self._commands = commands
        self._hud = HudWriter(transport)
        self._sessions = sessions
        self._status = status
        # Curated live aliases are supplied by __main__. Fuzzy fallback is
        # intentionally strict: 0.72 also accepts ordinary ambient words
        # coat/goat/boat, which can turn room speech into local note writes.
        #
        # The threshold stays where it is; the CORES carry the known
        # failures instead. whisper_serve documents what actually happens
        # to this name — "Whisper renders unknown proper nouns
        # phonetically ('JOAT' -> 'Jode'/'Jodh')" — and both score 0.50
        # against "joat", so the fallback could not catch either of the two
        # renderings this project had already written down. Matching them
        # directly costs nothing: at 0.86 the nearest ordinary words to
        # "jode" are code/node/mode/rode/joke/jade, all 0.75.
        #
        # The STT prompt bias remains the primary defence — this only
        # matters on the utterances where it does not take.
        # Wake words that are also ordinary English. They are in the list
        # because Whisper renders JOAT as them, which is real — but matched
        # ANYWHERE they turned "I liked your jokes", "give the door a jolt"
        # and "jot that down" into speech addressed to the assistant, which
        # was then stripped of that word and answered.
        #
        # The fuzzy path is strict to 0.86 precisely so coat/goat/boat
        # cannot do this; the exact path was admitting three such words
        # outright. Requiring them to OPEN the utterance keeps the
        # mis-rendering they exist for ("jokes, what is the weather") while
        # dropping the conversational use.
        #
        # Two words rather than one, because the mis-rendering can follow a
        # filler ("so jokes, tell me the time"). Measured over thirteen
        # cases: a window of 1 loses that, 3 re-admits "we should jot that
        # in the notes app later", 2 gets all thirteen right. The
        # assistant's own name is never anchored — it may appear anywhere,
        # so no way of addressing JOAT by name is lost.
        self._anchored_wake = anchored_wake
        self._anchor_window = anchor_window
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
        # Disabled by default. Acceptance may inject a local observer, which
        # receives only fixed stage names and coarse duration buckets.
        self._latency_observer = latency_observer
        # One monotonically increasing in-process ordinal correlates a voice
        # turn's fixed diagnostic stages without exposing an utterance,
        # timestamp, wearer, device, or cross-run identifier.
        self._voice_turn = 0
        self._last_seq: int | None = None
        # A G2-armed topic capture is allowed to consume exactly one glasses
        # utterance. Keep its provenance through VAD/STT rather than inferring
        # it later from the currently connected device.
        self._utterance_sources: set[AudioSource] = set()
        # The SDK labels speaker role with an app-side classifier, not a
        # firmware identity assertion.  Keep the entire observed set so only
        # an utterance classified *exclusively* as ``other`` is removed from
        # the scarce STT lane; self, unknown, legacy, and mixed frames remain
        # eligible for the normal explicit-invocation gate.
        self._utterance_speaker_roles: set[str] = set()
        # The voice path must stay single-flight: overlapping STT/reply work
        # competes for the resident engines.  It must not, however, appear
        # deaf while that work is in flight.  Keep exactly one *completed*
        # utterance as a latest-wins handoff.  The bounded slot prevents a
        # noisy room from creating an unbounded latency tail, while keeping
        # the wearer's newest question (and its source provenance) available
        # as soon as the active turn settles.
        self._pending_utterance: tuple[bytes, AudioSource | None] | None = None
        self._utterance_worker: asyncio.Task[None] | None = None

    def attach(self) -> None:
        if self._frames is not None:
            self._frames.on_audio_frame(self.on_audio_frame)
        elif isinstance(self._transport, AudioFrameSource):
            self._transport.on_audio_frame(self.on_audio_frame)
        else:
            # Older/direct transports retain ordinary voice support, but cannot
            # satisfy a glasses-armed capture because they provide no source.
            self._transport.on_audio(self.on_audio)

    async def on_connect(self) -> None:
        """Greet the wearer so a live brain is visibly different from none."""
        # A plugin can outlive the Mac brain. Its image containers retain the
        # previous process's reply across that reconnect, so painting only a
        # fresh status makes stale copy look like a new answer. With no live
        # session owning the surface, clear those regions and let the plugin
        # render its intentional connected-empty (Ready) surface.
        active_status = (
            self._sessions.active_status
            if self._sessions is not None and self._sessions.active
            else None
        )
        if active_status is None:
            for container_id in (
                ContainerId.MAIN,
                ContainerId.CONTEXT,
                ContainerId.TRAY,
            ):
                await self._transport.send(
                    HudText(text="", container_id=container_id)
                )
        if active_status is None:
            wake_hint = " — say 'JOAT ...'" if self._wake_words else ""
            status = (f"JOAT · online{wake_hint}" if self._stt_available
                      else "JOAT · online (no STT)")
            await self._transport.send(
                HudText(text=status, container_id=ContainerId.STATUS_BAR)
            )
        if not self._stt_available and active_status is None:
            await self._transport.send(
                HudText(text="Install whisperkit-cli on the Mac to enable transcription.")
            )
        if active_status is None and self._sessions is not None:
            # An engine restart mid-Brainstorm used to paint the empty
            # Ready surface as if no conversation had ever been happening.
            # The durable record knows better: a row still marked `active`
            # is crash evidence, and the wearer deserves the way back.
            probe = getattr(self._sessions, "interrupted_session", None)
            interrupted = probe() if callable(probe) else None
            if interrupted is not None:
                title = getattr(interrupted, "title", "") or "untitled"
                await self._transport.send(HudText(
                    text=(f"A Brainstorm was interrupted — “{title[:40]}”. "
                          f"Say “continue {title[:40]}” to pick it up."),
                ))
        # Request ears: an earlier session (or doff) may have left the mic off,
        # and a brain that cannot hear is indistinguishable from none.
        await self._transport.send(AudioControl(open=True))
        if active_status is not None:
            # The replacement Hub process starts with no retained visual
            # surface. Restore the current Brainstorm snapshot only after the
            # new authenticated link is able to receive it.
            replay = getattr(self._sessions, "replay_active_hud", None)
            if replay is not None:
                await replay()
            # The plugin truthfully marks AudioControl(open=True) as
            # listening. A focused session owns stronger status copy, so
            # restore it after opening the microphone.
            await self._transport.send(
                HudText(text=active_status, container_id=ContainerId.STATUS_BAR)
            )

    def on_audio(self, pcm: bytes, seq: int) -> None:
        self._ingest_audio(pcm, seq, source=None, speaker=None)

    def on_audio_frame(self, frame: AudioFrame) -> None:
        self._ingest_audio(
            frame.pcm,
            frame.seq,
            source=frame.source,
            speaker=frame.speaker,
        )

    def _ingest_audio(
        self,
        pcm: bytes,
        seq: int,
        *,
        source: AudioSource | None,
        speaker: str | None,
    ) -> None:
        if self._last_seq is not None and seq != self._last_seq + 1:
            # Frames were shed upstream; never splice across the hole.
            log.warning("audio gap (seq %d → %d): resetting utterance", self._last_seq, seq)
            self._vad.reset()
            self._stt.reset()
            self._utterance_sources.clear()
            self._utterance_speaker_roles.clear()
        self._last_seq = seq

        if source is not None:
            self._utterance_sources.add(source)
        if speaker in _SPEAKER_ROLES:
            self._utterance_speaker_roles.add(speaker)

        utterance = self._vad.feed(pcm)
        if utterance is None:
            return
        utterance_source = (
            next(iter(self._utterance_sources))
            if len(self._utterance_sources) == 1
            else None
        )
        only_other_speaker = self._utterance_speaker_roles == {"other"}
        self._utterance_sources.clear()
        self._utterance_speaker_roles.clear()
        if only_other_speaker:
            # Do not transcribe a completed utterance the host classified as
            # someone else.  This is a capacity hint, never identity proof:
            # unknown/mixed/legacy speech remains available to the normal
            # wake-word gate rather than making the wearer appear muted.
            log.info("ignored other-speaker utterance bytes=%d", len(utterance))
            return
        if self._utterance_worker is not None and not self._utterance_worker.done():
            replaced = self._pending_utterance is not None
            self._pending_utterance = (utterance, utterance_source)
            # Content-free: audio is transient, and diagnostics must not
            # expose transcripts or raw voice.  A replacement means the
            # newest completed utterance wins over an older queued one.
            log.info(
                "%s pending utterance (%d bytes) while voice work is in flight",
                "replaced" if replaced else "retained",
                len(utterance),
            )
            return
        self._utterance_worker = asyncio.get_running_loop().create_task(
            self._drain_utterances(utterance, utterance_source)
        )

    async def _drain_utterances(
        self,
        utterance: bytes,
        source: AudioSource | None,
    ) -> None:
        """Run one voice turn at a time, then hand off the newest queued turn."""
        current: tuple[bytes, AudioSource | None] | None = (utterance, source)
        try:
            while current is not None:
                await self._handle_utterance(*current)
                current = self._pending_utterance
                self._pending_utterance = None
        finally:
            self._utterance_worker = None

    async def _handle_utterance(
        self,
        utterance: bytes,
        source: AudioSource | None = None,
    ) -> None:
        started_at = time.monotonic()
        self._voice_turn += 1
        latency_turn = self._voice_turn
        self._observe_latency(latency_turn, "utterance_ready", started_at)
        if not self._stt_available:
            log.info("no STT engine — utterance dropped")
            return
        try:
            # Acknowledge FIRST. Transcription takes seconds, and an
            # unchanged HUD for that long reads as "the assistant is dead" —
            # the wearer has no other signal that they were heard.
            await self._hud.status("JOAT · hearing you…")
            self._observe_latency(latency_turn, "ack_sent", started_at)
            await self._stt.feed(utterance)
            transcript = await self._stt.flush()
            self._observe_latency(latency_turn, "stt_complete", started_at)
            if transcript is None or not transcript.text:
                log.info("STT produced no text")
                return
            log.info("transcript received chars=%d", len(transcript.text))
            if self._status is not None:
                self._status.set(last_transcript=transcript.text)  # type: ignore[attr-defined]
                self._status.bump("utterances")  # type: ignore[attr-defined]
            # A tap has explicitly armed the next glasses phrase as a
            # Brainstorm topic. This path intentionally runs before the normal
            # wake-word gate: requiring the wearer to say JOAT again would
            # contradict the HUD instruction. SessionManager rejects phone,
            # mixed, and legacy-source utterances while the capture is armed.
            if (
                self._sessions is not None
                and self._sessions.topic_capture_armed
                and self._sessions.handle_transcript(
                    transcript.text,
                    capture_source=source,
                )
            ):
                return
            # An ACTIVE session owns the HUD, but it does NOT get unaddressed
            # speech. Only the cancel words are heard unaddressed — failing
            # toward "stopped" is safe, and a wearer who wants it to stop
            # should not have to negotiate with the wake gate. Everything
            # that STEERS the session (turn direction, and any future
            # follow-up) requires the wearer to address JOAT: an overheard
            # "ask claude about that" must never reorder the wearer's round
            # table, and once follow-ups reach the collaborators the same
            # sentence would be room conversation shipped to a subprocess.
            if self._sessions is not None and self._sessions.active:
                session_addressed = self._strip_wake_word(transcript.text)
                if self._sessions.handle_transcript(
                    transcript.text,
                    addressed=session_addressed,
                ):
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
                log.info("unaddressed speech ignored chars=%d", len(transcript.text))
                return
            if not addressed:
                await self._hud.assistant("Yes?")
                await self._hud.settle_assistant(4_000)
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
                    await self._hud.assistant(answer)
                    await self._hud.settle_assistant()
                    return
            # The wearer's words go to their OWN region, so the reply no
            # longer erases the proof they were heard (HUD_DASHBOARD.md).
            await self._hud.context(f"you  {query}")
            if self._replier is None:
                return
            parts: list[str] = []
            async for chunk in self._replier.stream(query):
                if parts:
                    await self._hud.assistant(chunk, append=True)
                else:
                    await self._hud.assistant(chunk)
                    self._observe_latency(latency_turn, "first_reply_sent", started_at)
                parts.append(chunk)
            if parts:
                reply_text = "".join(parts)
                await self._hud.settle_assistant()
                self._observe_latency(latency_turn, "reply_complete", started_at)
                log.info("reply emitted chars=%d", len(reply_text))
                if self._status is not None:
                    self._status.set(last_reply=reply_text)  # type: ignore[attr-defined]
        finally:
            # "hearing you" is transient. Empty STT, unaddressed room speech,
            # a deterministic command, and a normal model reply must all
            # return the chrome to its truthful idle state.
            active_status = (
                self._sessions.active_status
                if self._sessions is not None and self._sessions.active
                else None
            )
            await self._hud.status(active_status or "JOAT · listening")

    def _observe_latency(self, turn: int, stage: str, started_at: float) -> None:
        """Emit one opt-in, fixed-vocabulary, coarsely bucketed stage."""
        if self._latency_observer is None:
            return
        try:
            self._latency_observer(
                turn, stage, _latency_bucket(time.monotonic() - started_at),
            )
        except Exception:
            # Instrumentation is strictly observational and must not affect
            # the voice path even when an acceptance sink fails.
            pass

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
            # Wake words that are also ORDINARY ENGLISH must open the
            # utterance; the assistant's own name may appear anywhere.
            limit = (
                self._anchor_window if wake in self._anchored_wake
                else len(words) - n + 1
            )
            for i in range(max(0, min(limit, len(words) - n + 1))):
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
                query = re.sub(r",{2,}", ",", query)
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
                query = re.sub(r",{2,}", ",", query)
                query = re.sub(r",+([.!?])", r"\1", query)
                query = re.sub(r"^[\s,.\-—]+", "", query)
                return query.rstrip(" ,-—")
        return None
