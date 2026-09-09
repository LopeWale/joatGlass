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
from collections import deque
from dataclasses import dataclass
import difflib
import logging
from collections.abc import Awaitable, Callable
import re
import time

from ..audio.vad import RmsVad
from ..core.interfaces import (
    AudioFrame, AudioFrameSource, GlassesTransport, ReplyEngine, SttEngine, Transcript,
)
from ..core.protocol import AudioControl, AudioSource, ContainerId, HudText
from .hud import HudWriter
from .prompted_input import PromptedInputDispatcher, PromptTicket, UtteranceProvenance
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


# How many completed utterances may wait behind the active turn. Three
# covers "wait — no — the other one" without letting a noisy room queue
# minutes of speech; past it the oldest is dropped, and logged.
PENDING_UTTERANCE_LIMIT = 3
# Whole-utterance stop words heard WITHOUT the wake word while a reply is
# streaming. Failing toward "stopped" is safe, and only the entire
# utterance matches, so dictation that merely contains "wait" is unaffected.
INTERRUPT_WORDS = frozenset({
    "stop", "stop it", "cancel", "cancel that", "wait", "hold on",
    "never mind", "nevermind", "forget it", "that's enough", "thats enough",
})


CONTINUATION_GAP_S = 1.6
"""Resume within this long of the last utterance's audio ending and you are
still saying the same thing.

The VAD closes an utterance after 1.0s of quiet (vad.RmsVad.hangover_batches)
because a longer hangover never closed one in a noisy room. That is a sound
rule for cutting AUDIO and a terrible one for deciding a person has finished
a THOUGHT: an ordinary mid-sentence pause ends the utterance, the engine
answers the fragment, and the rest of the sentence arrives as a separate
request. Observed live, 2026-09-08 17:40:44-17:40:50 — one spoken thought
became transcripts of 262, 17 and 25 characters, each handled alone.

Bytes are the clock: 16 kHz mono 16-bit, so a fragment's audio length gives
its true start and the real silence between two fragments.
"""

BYTES_PER_SECOND = 16_000 * 2

CONVERSATION_WINDOW_S = 30.0
"""How long JOAT keeps listening after it has finished answering.

Measured against the wearer's own speech: the follow-ups this window
exists to catch arrived 16s and 20s after the request JOAT had just
answered, and were both discarded. Thirty seconds covers a normal pause
for thought with margin, and still closes long before it could become a
standing invitation to the room.
"""


class ConversationWindow:
    """Whether JOAT is still in a conversation it was invited into.

    Explicit invocation is the rule (RULES.md), and this does not weaken
    it: the window opens ONLY after the wearer addressed JOAT by name and
    JOAT answered. It never opens on room speech, it closes on its own,
    and every reply re-opens it, so a real back-and-forth flows for as
    long as the wearer keeps talking and lapses the moment they stop.

    Its own object because "am I still being spoken to?" is a rule with a
    lifetime, not a timestamp comparison to repeat at each call site — and
    because the surface that TELLS the wearer it is open has to read the
    same answer the gate acts on.
    """

    def __init__(
        self,
        seconds: float = CONVERSATION_WINDOW_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._seconds = seconds
        self._monotonic = monotonic
        self._open_until = 0.0

    def open(self) -> None:
        """JOAT just answered; keep listening."""
        if self._seconds > 0:
            self._open_until = self._monotonic() + self._seconds

    def close(self) -> None:
        """The exchange is over — the next words need the wake word again."""
        self._open_until = 0.0

    @property
    def is_open(self) -> bool:
        return self._monotonic() < self._open_until


@dataclass(frozen=True, slots=True)
class _QueuedUtterance:
    pcm: bytes
    source: AudioSource | None
    provenance: UtteranceProvenance | None
    #: When VAD closed the utterance — the wearer's end of speech, which is
    #: where their wait begins. Every latency stage counts from here.
    endpointed_at: float


@dataclass(frozen=True, slots=True)
class _Turn:
    ordinal: int
    started_at: float
    prompt_revision: int
    prompt_ticket: PromptTicket | None


@dataclass(frozen=True, slots=True)
class _Transcribed:
    turn: _Turn
    transcript: Transcript
    source: AudioSource | None
    provenance: UtteranceProvenance | None


def _joined(head: _Transcribed, tail: _Transcribed) -> _Transcribed:
    """One request from two fragments of the same sentence.

    The FIRST fragment's turn is kept, so the wearer's wait is still
    measured from when they started speaking rather than restarting the
    clock every time they take a breath.

    The provenance is MERGED rather than inherited. It keeps the opening
    fragment's identity — same source, same connection, same epoch, which
    _continues has already proven — but its ingress span now ends where the
    latest fragment ended. Inheriting the head's span unchanged made a third
    fragment look like it had skipped every frame the second one carried, so
    a sentence could be joined exactly twice and never more.
    """
    text = f"{head.transcript.text.rstrip()} {tail.transcript.text.lstrip()}".strip()
    confidences = [c for c in (head.transcript.confidence, tail.transcript.confidence)
                   if c is not None]
    before, after = head.provenance, tail.provenance
    provenance = before
    if before is not None and after is not None:
        provenance = UtteranceProvenance(
            source=before.source,
            connection_generation=before.connection_generation,
            audio_epoch=before.audio_epoch,
            first_ingress_ordinal=before.first_ingress_ordinal,
            last_ingress_ordinal=after.last_ingress_ordinal,
            speaker_roles=before.speaker_roles | after.speaker_roles,
            mixed=before.mixed or after.mixed,
        )
    return _Transcribed(
        turn=head.turn,
        transcript=Transcript(
            text=text,
            is_final=tail.transcript.is_final,
            # The joined request is only as trustworthy as its weakest part.
            confidence=min(confidences) if confidences else None,
        ),
        source=head.source,
        provenance=provenance,
    )


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
        conversation_window_s: float = CONVERSATION_WINDOW_S,
        latency_observer: LatencyObserver | None = None,
        frames: AudioFrameSource | None = None,
        prompted_input: PromptedInputDispatcher | None = None,
        selected_message_reply: Callable[[str, UtteranceProvenance], Awaitable[bool]] | None = None,
        surface_revision: Callable[[], int] | None = None,
        feature_navigation: Callable[[str], Awaitable[str | None]] | None = None,
    ) -> None:
        self._transport = transport
        # An interposed source (the AudioRouter) lets an exclusive Voice Note
        # capture own the microphone; the orchestrator hears nothing while a
        # meeting is being recorded and resumes when the capture lane closes.
        self._frames = frames
        self._prompted_input = prompted_input
        self._selected_message_reply = selected_message_reply
        self._surface_revision = surface_revision or (lambda: 0)
        self._commands = commands
        self._feature_navigation = feature_navigation
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
        self._conversation = ConversationWindow(conversation_window_s)
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
        self._connection_generation: str | None = None
        self._ingress_ordinal = 0
        self._prompt_revision = 0
        # Mirror VAD's actual pre-roll, rather than retaining every idle
        # microphone/source label since the preceding utterance. These
        # immutable receipts follow exactly the chunks admitted into VAD.
        self._audio_preroll: deque[UtteranceProvenance] = deque(
            maxlen=self._vad.preroll_capacity,
        )
        self._utterance_frames: list[UtteranceProvenance] = []
        # STT and painting stay single-flight, but the voice path must not
        # appear deaf while a reply streams (the audit's A3): a correction
        # spoken over an answer used to wait behind that answer, and a third
        # utterance replaced the second. Completed utterances now queue in
        # order, bounded so a noisy room cannot build an unbounded latency
        # tail; the worker transcribes the next one WHILE the current reply
        # streams, and an addressed request or a bare stop word interrupts
        # that reply instead of waiting for it.
        self._pending_utterances: deque[_QueuedUtterance] = deque(
            maxlen=PENDING_UTTERANCE_LIMIT,
        )
        self._pending_arrived = asyncio.Event()
        self._stt_busy = 0
        self._utterance_worker: asyncio.Task[None] | None = None
        # The reply whose first words were sent but not yet seen painted:
        # (turn ordinal, end of speech). The next successful tile upload
        # the plugin reports closes it with the `painted` stage.
        self._awaiting_paint: tuple[int, float] | None = None

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

    def fence_prompt_audio(self) -> int:
        """Fence partial ingress after the matching prompt presentation.

        Completed STT and the pending turn keep their original receipts.
        A maximum-duration cut does not turn ongoing speech into an answer.
        """
        self._vad.fence_prompt()
        self._audio_preroll.clear()
        self._utterance_frames.clear()
        self._prompt_revision += 1
        return self._ingress_ordinal

    def on_audio(self, pcm: bytes, seq: int) -> None:
        self._ingest_audio(pcm, seq, source=None, speaker=None)

    def on_audio_frame(self, frame: AudioFrame) -> None:
        self._ingest_audio(
            frame.pcm,
            frame.seq,
            source=frame.source,
            speaker=frame.speaker,
            connection_generation=frame.connection_generation,
            audio_epoch=frame.audio_epoch,
        )

    def _ingest_audio(
        self,
        pcm: bytes,
        seq: int,
        *,
        source: AudioSource | None,
        speaker: str | None,
        connection_generation: str | None = None,
        audio_epoch: str | None = None,
    ) -> None:
        self._ingress_ordinal += 1
        if connection_generation != self._connection_generation:
            # Sequence comparisons are scoped to a plugin connection. A restart is
            # not evidence of a missing numbered batch; even a coincidentally
            # consecutive number must not join speech across connections.
            if self._last_seq is not None:
                log.info("audio stream changed: resetting partial utterance")
                self._reset_partial_utterance()
            self._connection_generation = connection_generation
            self._last_seq = None
        if self._last_seq is not None and seq != self._last_seq + 1:
            # Discontinuity within one stream (including a mic pause).
            # Never splice across the hole, regardless of its size.
            log.warning("audio gap (seq %d → %d): resetting utterance", self._last_seq, seq)
            self._reset_partial_utterance()
        self._last_seq = seq

        receipt = UtteranceProvenance(
            source=source, connection_generation=connection_generation,
            audio_epoch=audio_epoch,
            first_ingress_ordinal=self._ingress_ordinal,
            last_ingress_ordinal=self._ingress_ordinal,
            speaker_roles=frozenset({speaker}) if speaker in _SPEAKER_ROLES else frozenset(),
        )
        was_in_speech = self._vad.in_speech
        was_discarding = self._vad.prompt_discarding
        if was_in_speech:
            self._utterance_frames.append(receipt)
        else:
            self._audio_preroll.append(receipt)
        utterance = self._vad.feed(pcm)
        if was_discarding or self._vad.prompt_discarding:
            # The final quiet chunk of a fence is discarded too. Do not let
            # its old source/epoch contaminate the next fresh utterance.
            self._audio_preroll.clear()
            self._utterance_frames.clear()
            return
        if not was_in_speech and (self._vad.in_speech or utterance is not None):
            self._utterance_frames = list(self._audio_preroll)
            self._audio_preroll.clear()
        if utterance is None:
            return
        provenance = self._completed_provenance(self._utterance_frames)
        utterance_source: AudioSource | None = (
            "glasses" if provenance.source == "glasses"
            else "phone" if provenance.source == "phone" else None
        )
        only_other_speaker = provenance.speaker_roles == {"other"}
        self._utterance_frames.clear()
        if only_other_speaker:
            # Do not transcribe a completed utterance the host classified as
            # someone else.  This is a capacity hint, never identity proof:
            # unknown/mixed/legacy speech remains available to the normal
            # wake-word gate rather than making the wearer appear muted.
            log.info("ignored other-speaker utterance bytes=%d", len(utterance))
            return
        queued = _QueuedUtterance(utterance, utterance_source, provenance, time.monotonic())
        if self._utterance_worker is not None and not self._utterance_worker.done():
            dropped = len(self._pending_utterances) == self._pending_utterances.maxlen
            self._pending_utterances.append(queued)
            # Content-free: audio is transient, and diagnostics must not
            # expose transcripts or raw voice.
            log.info(
                "queued utterance (%d bytes, %d waiting%s) while voice work is in flight",
                len(utterance), len(self._pending_utterances),
                ", oldest dropped" if dropped else "",
            )
            self._pending_arrived.set()
            return
        self._utterance_worker = asyncio.get_running_loop().create_task(
            self._drain_utterances(queued)
        )

    @staticmethod
    def _completed_provenance(frames: list[UtteranceProvenance]) -> UtteranceProvenance:
        sources = {frame.source for frame in frames}
        generations = {frame.connection_generation for frame in frames}
        epochs = {frame.audio_epoch for frame in frames}
        return UtteranceProvenance(
            source=next(iter(sources)) if len(sources) == 1 else None,
            connection_generation=next(iter(generations)) if len(generations) == 1 else None,
            audio_epoch=next(iter(epochs)) if len(epochs) == 1 else None,
            first_ingress_ordinal=frames[0].first_ingress_ordinal,
            last_ingress_ordinal=frames[-1].last_ingress_ordinal,
            speaker_roles=frozenset(role for frame in frames for role in frame.speaker_roles),
            mixed=any(len(values) > 1 for values in (sources, generations, epochs)),
        )

    def _reset_partial_utterance(self) -> None:
        if self._prompted_input is not None and self._prompted_input.current_ticket is not None:
            self._vad.fence_prompt()
        else:
            self._vad.reset()
        self._audio_preroll.clear()
        self._utterance_frames.clear()
        # STT receives only completed, contiguous utterances, owned by the
        # single-flight worker. A gap in NEW ingress must not erase an older
        # complete turn while feed()/flush() is suspended. The pending slot
        # likewise contains a complete utterance and needs no reset.
        if self._utterance_worker is None or self._utterance_worker.done():
            self._stt.reset()

    async def _drain_utterances(
        self,
        first: _QueuedUtterance | bytes,
        source: AudioSource | None = None,
        provenance: UtteranceProvenance | None = None,
    ) -> None:
        """Run voice turns in order; transcribe ahead; let the wearer interrupt."""
        if isinstance(first, bytes):
            # The pre-queue calling shape (pcm, source[, provenance]).
            first = _QueuedUtterance(first, source, provenance, time.monotonic())
        current: _QueuedUtterance | None = first
        try:
            while current is not None:
                try:
                    await self._run_turn(current)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # A voice turn must not die into a bare task. This was
                    # `try/finally` with no `except`, so ANY handler that
                    # raised — a session factory whose binary vanished, a
                    # command, the reply engine — ended the turn as an
                    # unhandled task exception. The last thing painted is
                    # the "listening" status from the start of the turn, so
                    # the wearer is left looking at a screen that says JOAT
                    # is listening, forever, unable to tell a failure from
                    # a long think. The loop already survives (the queue
                    # drains, the next utterance is served); only the
                    # wearer was left out.
                    # Owner/provider exceptions can include the raw prompted
                    # answer. Keep the class for diagnostics, not its message
                    # or a traceback containing private arguments.
                    log.error("voice turn failed error=%s", type(exc).__name__)
                    await self._say_turn_failed()
                current = (
                    self._pending_utterances.popleft() if self._pending_utterances else None
                )
        finally:
            self._utterance_worker = None

    async def _say_turn_failed(self) -> None:
        """Tell the wearer the turn died, on the surface they are watching.

        Deliberately vague about the cause and specific about the remedy:
        the exception text can carry a prompt, a transcript fragment or a
        provider payload, none of which belongs on the lenses or in a log
        the wearer reads aloud.
        """
        if self._prompted_input is not None and self._prompted_input.current_ticket is not None:
            return
        try:
            await self._hud.assistant(
                "Something went wrong with that — say it again.",
                answers_wearer=True,
            )
            await self._hud.settle_assistant(4_000)
        except Exception:
            log.warning("turn-failure notice not painted", exc_info=True)

    async def _handle_utterance(
        self,
        utterance: bytes,
        source: AudioSource | None = None,
        provenance: UtteranceProvenance | None = None,
    ) -> None:
        """One complete turn, serially — the entry the existing fixtures use."""
        await self._run_turn(_QueuedUtterance(utterance, source, provenance, time.monotonic()))

    def _begin_turn(self, endpointed_at: float) -> _Turn:
        self._voice_turn += 1
        turn = _Turn(
            ordinal=self._voice_turn, started_at=endpointed_at,
            prompt_revision=self._prompt_revision,
            prompt_ticket=self._prompted_input.current_ticket if self._prompted_input else None,
        )
        # Measured from the END of speech (the audit's A6): this first bucket
        # is the queue wait, which the old clock — started when the worker
        # picked the utterance up — could never see.
        self._observe_latency(turn.ordinal, "utterance_ready", turn.started_at)
        return turn

    async def _transcribe(self, turn: _Turn, utterance: bytes) -> Transcript | None:
        if not self._stt_available:
            log.info("no STT engine — utterance dropped")
            return None
        self._stt_busy += 1
        try:
            # Acknowledge FIRST. Transcription takes seconds, and an
            # unchanged HUD for that long reads as "the assistant is dead" —
            # the wearer has no other signal that they were heard.
            await self._hud.status("JOAT · hearing you…")
            self._observe_latency(turn.ordinal, "ack_sent", turn.started_at)
            await self._stt.feed(utterance)
            transcript = await self._stt.flush()
            self._observe_latency(turn.ordinal, "stt_complete", turn.started_at)
        finally:
            self._stt_busy -= 1
        if transcript is None or not transcript.is_final or not transcript.text:
            log.info("STT produced no text")
            await self._restore_status()
            return None
        log.info("transcript received chars=%d", len(transcript.text))
        if self._status is not None:
            self._status.set(last_transcript=transcript.text)  # type: ignore[attr-defined]
            self._status.bump("utterances")  # type: ignore[attr-defined]
        return transcript

    def _interrupts(self, text: str) -> str | None:
        """Why speech heard over a streaming reply should cut it short, if at all.

        "addressed": the wearer said JOAT again — a new or corrected request,
        which replaces the answer in progress. "stop": a bare stop word,
        heard without the wake word. Anything else is room talk or an answer
        to a question on the glass, and waits its turn.
        """
        if self._addressed(text) is not None:
            return "addressed"
        lowered = re.sub(r"[^a-z' ]+", " ", text.lower().replace("’", "'"))
        return "stop" if " ".join(lowered.split()) in INTERRUPT_WORDS else None

    async def _restore_status(self) -> None:
        # "hearing you" is transient. Empty STT, unaddressed room speech,
        # a deterministic command, and a normal model reply must all
        # return the chrome to its truthful idle state.
        if self._stt_busy:
            # A queued utterance is being transcribed over this turn's end;
            # its own "hearing you…" is the truthful line right now.
            return
        active_status = (
            self._sessions.active_status
            if self._sessions is not None and self._sessions.active
            else None
        )
        # Say which of the two states the wearer is actually in. "listening"
        # was shown whether the next sentence would be heard or silently
        # dropped, so the only safe habit it taught was to say the name
        # every time — the habit that made this feel like a command line.
        idle = ("JOAT · go on" if self._conversation.is_open
                else "JOAT · listening")
        await self._hud.status(active_status or idle)

    async def _run_turn(self, queued: _QueuedUtterance) -> None:
        """One utterance: transcribe, then act — while listening for the next.

        The reply runs as its own task. While it streams, the next queued
        utterance is transcribed; an addressed request or a bare stop word
        cancels the reply (which kills the CLI child) and takes its place.
        Unaddressed speech is acted on after the reply, in arrival order, so
        an answer to a question on the glass is never lost either.
        """
        turn = self._begin_turn(queued.endpointed_at)
        transcript = await self._transcribe(turn, queued.pcm)
        if transcript is None:
            return
        head = _Transcribed(turn, transcript, queued.source, queued.provenance)
        head_endpointed_at = queued.endpointed_at
        backlog: list[_Transcribed] = []
        loop = asyncio.get_running_loop()
        act = loop.create_task(self._act(head))
        act_started = time.monotonic()
        try:
            while not act.done():
                if not self._pending_utterances:
                    self._pending_arrived.clear()
                    arrival = loop.create_task(self._pending_arrived.wait())
                    try:
                        await asyncio.wait({act, arrival}, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        if not arrival.done():
                            arrival.cancel()
                    if act.done() or not self._pending_utterances:
                        continue
                nxt = self._pending_utterances.popleft()
                next_turn = self._begin_turn(nxt.endpointed_at)
                next_transcript = await self._transcribe(next_turn, nxt.pcm)
                if next_transcript is None:
                    continue
                item = _Transcribed(next_turn, next_transcript, nxt.source, nxt.provenance)
                if not act.done() and self._continues(head, head_endpointed_at, nxt):
                    # The wearer never stopped talking; the VAD stopped
                    # listening. Answering the fragment and then answering
                    # the remainder separately is what "it responds before
                    # I'm done" and "it drops context" both look like from
                    # the outside. Take back the premature answer and ask
                    # the whole sentence instead.
                    act.cancel()
                    await asyncio.gather(act, return_exceptions=True)
                    log.info("joined a split utterance (%d + %d chars)",
                             len(head.transcript.text), len(next_transcript.text))
                    head = _joined(head, item)
                    head_endpointed_at = nxt.endpointed_at
                    act = loop.create_task(self._act(head))
                    act_started = time.monotonic()
                    continue
                reason = None if act.done() else self._interrupts(next_transcript.text)
                if reason == "addressed" and nxt.endpointed_at < act_started:
                    # Finished speaking before any answer existed: a second
                    # request in a row, not a correction of the first. Both
                    # are answered, in order. Only speech that ENDS while the
                    # answer is under way replaces it.
                    reason = None
                if reason is None:
                    backlog.append(item)
                    continue
                act.cancel()
                await asyncio.gather(act, return_exceptions=True)
                self._observe_latency(head.turn.ordinal, "interrupted", head.turn.started_at)
                log.info("voice turn interrupted (%s) by a newer utterance", reason)
                if reason == "stop" and not (self._sessions is not None and self._sessions.active):
                    # No session to hand the stop word to: acknowledge it
                    # where the cut-off answer was, then let it settle.
                    await self._hud.assistant("Stopped.", answers_wearer=True)
                    await self._hud.settle_assistant(2_000)
                head = item
                head_endpointed_at = nxt.endpointed_at
                act = loop.create_task(self._act(head))
                act_started = time.monotonic()
            await act
        finally:
            if not act.done():
                act.cancel()
                await asyncio.gather(act, return_exceptions=True)
        for item in backlog:
            await self._act(item)

    async def _act(self, item: _Transcribed) -> None:
        turn, transcript, source, provenance = item.turn, item.transcript, item.source, item.provenance
        started_at = turn.started_at
        latency_turn = turn.ordinal
        prompt_revision = turn.prompt_revision
        prompt_ticket = turn.prompt_ticket
        try:
            if self._prompted_input is not None and await self._prompted_input.dispatch(
                transcript, provenance,
            ):
                # The typed owner received the exact transcript. Do not let
                # greetings, command parsing or a second model reinterpret it.
                self._observe_latency(latency_turn, "reply_complete", started_at)
                return
            # A tap has explicitly armed the next glasses phrase as a
            # Brainstorm topic. This path intentionally runs before the normal
            # wake-word gate: requiring the wearer to say JOAT again would
            # contradict the HUD instruction. SessionManager rejects phone,
            # mixed, and legacy-source utterances while the capture is armed.
            if (
                self._prompted_input is None
                and self._sessions is not None
                and self._sessions.topic_capture_armed
                and self._sessions.handle_transcript(
                    transcript.text,
                    capture_source=source,
                )
            ):
                return
            addressed = self._addressed(transcript.text)
            if addressed and self._feature_navigation is not None:
                navigation = await self._feature_navigation(addressed)
                if navigation is not None:
                    # The narrow capability opener already owns its new
                    # surface and revision. Do not cover it with an assistant
                    # receipt or turn the request into a Brainstorm remark.
                    self._observe_latency(latency_turn, "reply_complete", started_at)
                    return
            # SessionManager checks whether the current feature owns speech;
            # a background Brainstorm must not swallow another feature's input.
            # Only the cancel words are heard unaddressed — failing
            # toward "stopped" is safe, and a wearer who wants it to stop
            # should not have to negotiate with the wake gate. Everything
            # that STEERS the session (turn direction, and any future
            # follow-up) requires the wearer to address JOAT: an overheard
            # "ask claude about that" must never reorder the wearer's round
            # table, and once follow-ups reach the collaborators the same
            # sentence would be room conversation shipped to a subprocess.
            if self._sessions is not None and self._sessions.active:
                if self._sessions.handle_transcript(
                    transcript.text,
                    addressed=addressed,
                ):
                    return
            # Address gate BEFORE any trigger phrase: session routing used to
            # run first, so room speech merely beginning with "debate " (or a
            # notification read aloud) started a cloud debate and shipped the
            # rest of the utterance to the codex/claude subprocesses without
            # the wearer ever addressing JOAT — observed live, and
            # a direct violation of explicit invocation. Room conversation is
            # not a prompt.
            if addressed is None:
                if (
                    self._selected_message_reply is not None
                    and provenance is not None
                    and await self._selected_message_reply(transcript.text, provenance)
                ):
                    return
                log.info("unaddressed speech ignored chars=%d", len(transcript.text))
                return
            if not addressed:
                if not self._reply_surface_current(prompt_revision, prompt_ticket):
                    return
                # "JOAT" alone is the wearer opening a conversation and
                # waiting to be asked. Answering "Yes?" and then demanding
                # the name again in the very next breath is the behaviour
                # this window exists to end.
                self._conversation.open()
                await self._hud.assistant("Yes?")
                if not self._reply_surface_current(prompt_revision, prompt_ticket):
                    return
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
                    if answer == "":
                        # The command handled its own surface or question.
                        return
                    if not self._reply_surface_current(prompt_revision, prompt_ticket):
                        return
                    await self._hud.context(f"you  {query}")
                    if not self._reply_surface_current(prompt_revision, prompt_ticket):
                        return
                    await self._hud.assistant(answer)
                    # A deterministic answer is a reply too; it was missing
                    # from the receipt (the audit's A6), and it continues a
                    # conversation for the same reason a model reply does.
                    self._conversation.open()
                    self._observe_latency(latency_turn, "first_reply_sent", started_at)
                    if not self._reply_surface_current(prompt_revision, prompt_ticket):
                        return
                    await self._hud.settle_assistant()
                    self._observe_latency(latency_turn, "reply_complete", started_at)
                    return
            # The wearer's words go to their OWN region, so the reply no
            # longer erases the proof they were heard (HUD_DASHBOARD.md).
            session_revision = getattr(self._sessions, "session_revision", 0)
            surface_revision = self._surface_revision()

            def reply_is_current() -> bool:
                # A model tool can start a session during this very stream.
                # Its receipt no longer owns the HUD, even if that session
                # finishes before the model returns its first chunk. An
                # unchanged existing session may still allow ordinary chat.
                return (
                    self._reply_surface_current(prompt_revision, prompt_ticket)
                    and getattr(self._sessions, "session_revision", 0) == session_revision
                    and self._surface_revision() == surface_revision
                )

            if not reply_is_current():
                return
            await self._hud.context(f"you  {query}")
            if self._replier is None or not reply_is_current():
                return
            parts: list[str] = []
            async for chunk in self._replier.stream(query):
                if not self._reply_surface_current(prompt_revision, prompt_ticket):
                    return
                if not reply_is_current():
                    # The tool handed off the display successfully. Drain
                    # the assistant turn so its CLI can finish and retain
                    # conversation continuity; only painting is obsolete.
                    continue
                if parts:
                    await self._hud.assistant(chunk, append=True)
                else:
                    await self._hud.assistant(chunk)
                    self._observe_latency(latency_turn, "first_reply_sent", started_at)
                parts.append(chunk)
            if parts:
                if not reply_is_current():
                    return
                reply_text = "".join(parts)
                # JOAT just spoke to the wearer; a reply invites an answer.
                self._conversation.open()
                await self._hud.settle_assistant()
                self._observe_latency(latency_turn, "reply_complete", started_at)
                log.info("reply emitted chars=%d", len(reply_text))
                if self._status is not None:
                    self._status.set(last_reply=reply_text)  # type: ignore[attr-defined]
        finally:
            await self._restore_status()

    def _reply_surface_current(self, revision: int, ticket: PromptTicket | None) -> bool:
        """Ordinary replies cannot paint while an input question owns the HUD.

        Queued audio may start STT after publication, so an unchanged snapshot
        of a live ticket is not permission to cover that question. Accepted
        prompted answers already return through their typed owner above.
        """
        return (
            revision == self._prompt_revision
            and ticket is None
            and (self._prompted_input is None or self._prompted_input.current_ticket is None)
        )

    def on_paint_trace(self, event: dict[str, str | int]) -> None:
        """A plugin trace event: the first successful tile upload after a
        reply's first words is the closest thing to "the wearer can see it"
        the host has (the audit's A6). Diagnostics-only, like the receipt."""
        if self._awaiting_paint is None:
            return
        if event.get("type") != "tile_upload" or event.get("outcome") != "success":
            return
        turn, started_at = self._awaiting_paint
        self._awaiting_paint = None
        self._observe_latency(turn, "painted", started_at)

    def _observe_latency(self, turn: int, stage: str, started_at: float) -> None:
        """Emit one opt-in, fixed-vocabulary, coarsely bucketed stage."""
        if stage == "first_reply_sent":
            self._awaiting_paint = (turn, started_at)
        # Say how long the wearer waited, in the log, always. The engine has
        # always MEASURED every stage of that wait and then discarded all of
        # it unless an acceptance harness happened to be attached — so "the
        # response is very slow" could only ever be answered by inference.
        # Elapsed seconds and a fixed stage name carry no transcript and no
        # audio, which is the whole reason the observer was opt-in.
        log.info(
            "turn %d %s +%.2fs", turn, stage, time.monotonic() - started_at,
        )
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

    def _continues(
        self, head: _Transcribed, previous_endpointed_at: float, nxt: _QueuedUtterance,
    ) -> bool:
        """Is this the rest of the sentence, or a new one?

        Two things must both hold. The audio has to be continuous — measured
        from the AUDIO, not from when transcription happened, because the
        fragment's byte length gives when the wearer resumed speaking, and a
        wall-clock comparison would read a slow transcription as a long
        silence and split a sentence that was never split.

        And it has to be the SAME capture. Audio either side of a reconnect
        or a source change is not one sentence however close the timestamps
        are: splicing across that boundary would attribute words to a
        capture that never carried them, which is the thing the recovery
        path exists to prevent.
        """
        before, after = head.provenance, nxt.provenance
        if head.source != nxt.source:
            return False
        if before is not None and after is not None:
            if (
                before.source != after.source
                or before.connection_generation != after.connection_generation
                or before.audio_epoch != after.audio_epoch
            ):
                return False
            # Contiguous ingress, or the silence between the fragments is
            # not silence at all but audio that never arrived. Dropped
            # frames are exactly the case where joining would invent the
            # continuity it claims to have observed; the ordinals are the
            # only place that loss is visible after the fact.
            last, first = before.last_ingress_ordinal, after.first_ingress_ordinal
            if last is not None and first is not None and first > last + 1:
                return False
        resumed_at = nxt.endpointed_at - (len(nxt.pcm) / BYTES_PER_SECOND)
        return resumed_at - previous_endpointed_at < CONTINUATION_GAP_S

    def _addressed(self, text: str) -> str | None:
        """The wearer's request, or None if this speech was not for JOAT.

        The wake word is how a conversation STARTS, not a toll charged on
        every sentence of it. Once JOAT has answered, the wearer's next
        words continue that exchange — which is what "conversation" means
        and what the wake gate alone could never be.
        """
        spoken = self._strip_wake_word(text)
        if spoken is not None:
            return spoken
        if self._conversation.is_open:
            log.info("follow-up heard inside the conversation window")
            return text
        return None

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
