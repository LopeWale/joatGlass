"""Explicit, local Voice Note capture with durable gap and retention truth.

FEAT-002 makes this the engine of one QuickNotes feature with two ways to
talk: a continuous recording (Even's Conversate) and hold-to-talk paragraphs
(Even's QuickNote). Both journal raw audio first, exactly as before; what is
new is that the journal is transcribed in chunks WHILE the note is open, so
the lens can show the words and Save no longer waits for the whole recording.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
import wave
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..audio.router import AudioRouteConflictError, AudioRouter
from ..audio.vad import RmsVad, rms
from ..core.interfaces import AudioFrame, SttEngine, SttUnavailableError
from ..persistence.voice_notes import VoiceNote, VoiceNoteFrame, VoiceNoteStore

log = logging.getLogger(__name__)

# The wearer calls this feature both "voice note" and "quicknote"; whisper
# also splits or joins the compound unpredictably. Every control phrase
# accepts all three spellings so the recorder never looks deaf to its own
# name.
_NOTE_NOUNS = ("voice note", "quick note", "quicknote", "recording", "note")


def _phrases(*templates: str) -> frozenset[str]:
    # Speak-what-you-see: the HUD control rows say "Pause recording" and
    # "Save and finish", so the grammar accepts every noun a wearer would
    # naturally read off the lens, with or without an article.
    return frozenset(
        template.format(noun=article + noun)
        for template in templates
        for noun in _NOTE_NOUNS
        for article in ("", "the ", "this ")
    )


_START = re.compile(
    r"^(?:start|begin)(?: recording)? (?:a )?(?:voice note|quick ?note)"
    r"(?: (?:called|titled|about))?"
    r"(?: (?P<title>.+))?$",
    re.IGNORECASE,
)
# The visible control rows are also speakable verbatim ("Save and finish",
# "Confirm discard") — a wearer reads the lens and says what it says.
_STOP = _phrases("stop {noun}", "save {noun}", "finish {noun}") | frozenset(
    {"save and finish"},
)
_PAUSE = _phrases("pause {noun}")
_RESUME = _phrases("resume {noun}", "continue {noun}")
_DISCARD = _phrases("discard {noun}", "cancel {noun}")
_CONFIRM_DISCARD = _phrases("confirm discard {noun}") | frozenset({"confirm discard"})
_KEEP = _phrases("keep {noun}", "cancel discard {noun}")
_SHOW_LAST = _phrases("show last {noun}")
_EXPORT_LAST = _phrases("export last {noun}")
_MAX_TRANSCRIBE_BYTES = 20 * 16_000 * 2
#: A live chunk is cut at a pause, and never grows past this: the words of
#: someone who never pauses still reach the lens within ~15 s.
_MAX_CHUNK_BYTES = 15 * 16_000 * 2
#: A chunk whose loudest frame stays under this is near-silence: accounted
#: for, never sent to STT (whisper invents words in silence). The same
#: absolute floor the conversational VAD will not arm below.
_SILENCE_RMS = RmsVad.speech_rms
_DEFAULT_RAW_QUOTA_BYTES = 30 * 60 * 16_000 * 2
# Audio batches arrive every ~200ms while capture is healthy. A much longer
# silence with CONTIGUOUS sequence numbers is the phone suspending audio
# delivery (the Even app backgrounded, the phone locked) — the seq-gap check
# cannot see it, so it must be journaled from wall-clock discontinuity or the
# transcript would silently claim completeness across a hole.
_STALL_GAP_US = 5_000_000
#: A held touchpad registers as a long press only after a moment, and the
#: wearer is already talking by then. The router hands this much recent
#: audio to a hold so the first words are kept.
HOLD_PREROLL_US = 1_200_000
#: The clock title a tapped start gets; the summary replaces it.
DEFAULT_TITLE = re.compile(r"^Note \d{2}:\d{2}$")
_BYTES_PER_SECOND = 16_000 * 2
#: A paragraph stops growing near the store's section limit (20,000).
_MAX_PARAGRAPH_CHARS = 18_000

NoteMode = Literal["continuous", "hold"]


@dataclass(frozen=True, slots=True)
class VoiceNoteCaptureSnapshot:
    capture_id: str | None
    title: str | None
    state: str
    received_frames: int
    journaled_frames: int
    gap_count: int
    failure_code: str | None
    #: How the note was opened: a continuous recording or hold-to-talk.
    mode: NoteMode = "continuous"
    #: True while the touchpad is held and the note is listening.
    holding: bool = False
    #: Whole seconds the note has listened, on a steady clock (the REC timer).
    recorded_s: int = 0
    #: True while audio of this note is waiting for, or in, transcription.
    transcribing: bool = False


@dataclass(frozen=True, slots=True)
class LiveLine:
    """One transcript section as the open note's page shows it."""

    text: str


@dataclass(slots=True)
class _Chunk:
    first: int
    last: int
    loud: bool
    #: Cut from a held touchpad: the hold, not the chunk, is the input.
    in_hold: bool = False
    #: The paragraph its words join: one hold, or one continuous stretch.
    paragraph: int = -1


class VoiceNoteCaptureController:
    """Own one explicit Mac-local capture independently of reply generation."""

    def __init__(
        self,
        notes: VoiceNoteStore,
        audio: AudioRouter,
        stt: SttEngine,
        *,
        stt_available: bool = True,
        clock_us: Callable[[], int] | None = None,
        id_factory: Callable[[], str] | None = None,
        max_raw_bytes: int = _DEFAULT_RAW_QUOTA_BYTES,
        export_directory: Path | None = None,
        on_saved: Callable[[str], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            isinstance(max_raw_bytes, bool)
            or not isinstance(max_raw_bytes, int)
            or not 1 <= max_raw_bytes <= 2_000_000_000
        ):
            raise ValueError("max_raw_bytes must be between 1 and 2000000000")
        self._notes = notes
        self._audio = audio
        self._stt = stt
        self._stt_available = stt_available
        self._clock_us = clock_us or (lambda: time.time_ns() // 1_000)
        self._id_factory = id_factory or (lambda: secrets.token_urlsafe(18))
        self._max_raw_bytes = max_raw_bytes
        self._export_directory = export_directory or (
            Path.home() / "Library/Application Support/JOAT/Exports/Voice Notes"
        )
        # Fired with the capture_id after a retained save (saved or
        # saved_degraded). The receipt returns immediately; whatever the hook
        # starts (summarization) reports through its own surfaces.
        self._on_saved = on_saved
        # The live page repaints from these (FEAT-002). Called synchronously
        # and often (every journaled frame); listeners must be cheap and
        # schedule their own work.
        self._listeners: list[Callable[[], None]] = []
        self._monotonic = monotonic
        self._capture_id: str | None = None
        self._mode: NoteMode = "continuous"
        self._holding = False
        self._last_generation: str | None = None
        self._last_seq: int | None = None
        self._last_received_at_us: int | None = None
        # The newest journaled frame of this note; a hold's pre-roll may
        # overlap audio a continuous stretch already journaled.
        self._journal_generation: str | None = None
        self._journal_seq: int | None = None
        self._capture_error: str | None = None
        self._discard_pending: str | None = None
        self._resume_after_keep = False
        # Live transcription: the open chunk, the queue of cut chunks, the
        # worker draining it, and chunks the engine could not transcribe yet.
        self._chunk: _Chunk | None = None
        self._chunk_bytes = 0
        self._vad = RmsVad()
        self._queue: deque[_Chunk] = deque()
        self._failed: list[_Chunk] = []
        self._worker: asyncio.Task[None] | None = None
        # "Undo last input" (Even's QuickNote): the frame range of each input
        # with words in it, oldest first — a hold, or a sentence of a
        # continuous recording — and the hold now being journaled.
        self._inputs: list[tuple[int, int, int]] = []
        # Paragraphs (FEAT-002): words of one continuous stretch flow on as
        # one paragraph, as Conversate shows them; each hold is its own, as
        # QuickNote makes them. `_open_para` is the newest one with words:
        # (section ordinal, paragraph, last accounted frame, text).
        self._paragraph = 0
        self._open_para: tuple[int, int, int, str] | None = None
        self._hold_first: int | None = None
        self._hold_last: int | None = None
        self._hold_loud = False
        # Monotonic seconds: when capture last (re)started, when the newest
        # frame arrived. The no-audio guard reads these.
        self._capture_began: float | None = None
        self._last_audio: float | None = None
        # The REC timer: monotonic seconds listened in finished stretches, and
        # when the current one began. Wall time, not audio bytes — frames
        # arrive in bursts, and a timer read from them jumps.
        self._listened_before = 0.0
        self._listen_since: float | None = None
        # Bound method identity is otherwise recreated on every attribute read;
        # AudioRouter requires the exact owner that acquired the capture lane.
        self._capture_callback = self._record_frame

    @property
    def active(self) -> bool:
        return self._capture_id is not None

    @property
    def capture_id(self) -> str | None:
        return self._capture_id

    def add_listener(self, listener: Callable[[], None]) -> None:
        """Be told (synchronously) whenever the open note changes."""
        self._listeners.append(listener)

    @property
    def snapshot(self) -> VoiceNoteCaptureSnapshot:
        capture_id = self._capture_id
        if capture_id is None:
            return VoiceNoteCaptureSnapshot(None, None, "idle", 0, 0, 0, None)
        note = self._notes.get(capture_id)
        return VoiceNoteCaptureSnapshot(
            capture_id=note.capture_id,
            title=note.title,
            state=note.state,
            received_frames=note.received_frames,
            journaled_frames=note.journaled_frames,
            gap_count=note.gap_count,
            failure_code=self._capture_error,
            mode=self._mode,
            holding=self._holding,
            recorded_s=int(self.elapsed_s()),
            transcribing=self._transcribing(),
        )

    def live_lines(self, limit: int = 12) -> tuple[LiveLine, ...]:
        """The open note's newest transcript sections, oldest first."""
        capture_id = self._capture_id
        if capture_id is None or limit < 1:
            return ()
        lines = [
            LiveLine(segment.text)
            for segment in self._notes.segments(capture_id)
            if segment.kind == "transcript"
        ]
        return tuple(lines[-limit:])

    def elapsed_s(self) -> float:
        """Seconds the open note has listened (recording or held), steady."""
        since = self._listen_since
        running = self._monotonic() - since if since is not None else 0.0
        return self._listened_before + max(0.0, running)

    def silent_for_s(self) -> float:
        """Seconds the open capture has gone without a frame; 0 when not capturing."""
        if self._capture_id is None or not self._audio.capturing or self._capture_began is None:
            return 0.0
        since = self._last_audio if self._last_audio is not None else self._capture_began
        return max(0.0, self._monotonic() - since)

    async def handle_command(self, text: str) -> str | None:
        # Whisper punctuates short utterances ("Pause recording."). Terminal
        # punctuation is STT noise for the exact lifecycle grammar; the
        # _START title group still keeps its own interior punctuation.
        normalized = " ".join(text.strip().split()).rstrip(".,!?").rstrip()
        lowered = normalized.casefold()
        start = _START.fullmatch(normalized)
        if start is not None:
            return self._start(start.group("title"))
        if lowered in _PAUSE:
            return self._pause()
        if lowered in _RESUME:
            return self._resume()
        if lowered in _DISCARD:
            return self._request_discard()
        if lowered in _CONFIRM_DISCARD:
            return self._confirm_discard()
        if lowered in _KEEP:
            return self._keep()
        if lowered in _STOP:
            return await self._stop()
        if lowered in _SHOW_LAST:
            return self._show_last()
        if lowered in _EXPORT_LAST:
            return self._export_last()
        return None

    @property
    def discard_pending(self) -> bool:
        """True while a destructive discard awaits explicit confirmation."""
        return (
            self._capture_id is not None
            and self._discard_pending == self._capture_id
        )

    # Typed control surface for the on-glasses QuickNotes screen. During an
    # exclusive capture the microphone belongs to the recording, so HUD row
    # taps are the wearer's only controls; the receipts are the same visible
    # strings the voice grammar produces.
    def control_pause(self) -> str:
        return self._pause()

    def control_resume(self) -> str:
        return self._resume()

    def control_request_discard(self) -> str:
        return self._request_discard()

    def control_confirm_discard(self) -> str:
        return self._confirm_discard()

    def control_keep(self) -> str:
        return self._keep()

    def control_discard(self) -> str:
        """The exit prompt's Discard (Even's QuickNote): no second question.

        The prompt itself was the confirmation; the spoken grammar keeps its
        two steps because one misheard word must not purge a note.
        """
        note = self._active_note()
        if note is None:
            return "No Voice Note is active."
        self._request_discard()
        return self._confirm_discard()

    def control_undo(self) -> str:
        """"Undo last input" (Even's QuickNote menu): drop the newest paragraph.

        A paragraph is one hold, or one sentence of a continuous recording.
        Its audio stays accounted for; its words leave the note, including
        words still being transcribed.
        """
        note = self._active_note()
        if note is None:
            return "No Voice Note is open."
        if self._holding:
            return "Let go of the touchpad first."
        while self._inputs:
            paragraph, first, last = self._inputs.pop()
            if self._notes.is_undone(note.capture_id, first, last):
                continue
            self._notes.undo_range(
                note.capture_id,
                first_frame_ordinal=first,
                last_frame_ordinal=last,
                now_us=self._now(note),
            )
            self._close_paragraph(paragraph)
            self._notify()
            return "Removed the last paragraph."
        return "Nothing to undo."

    def _close_paragraph(self, paragraph: int) -> None:
        """Words after an undone paragraph start a new one."""
        if self._open_para is not None and self._open_para[1] == paragraph:
            self._open_para = None
        if paragraph == self._paragraph:
            self._paragraph += 1

    def control_hold_cancel(self) -> str | None:
        """The held touchpad opened the glasses' menu instead (tap, then
        long press): end the hold and keep none of its words."""
        note = self._active_note()
        if note is None or not self._holding:
            return None
        held = self._release_hold(note)
        if held is not None:
            if held in self._inputs:
                self._inputs.remove(held)
            self._notes.undo_range(
                note.capture_id,
                first_frame_ordinal=held[1],
                last_frame_ordinal=held[2],
                now_us=self._now(self._notes.get(note.capture_id)),
            )
            self._close_paragraph(held[0])
        self._notify()
        return "Menu opened; the held audio was not kept."

    async def control_stop(self) -> str:
        return await self._stop()

    def control_start(self, mode: NoteMode = "continuous") -> str:
        """Start a note from a tapped row; the title is the clock.

        The spoken grammar insists on a title because a one-word utterance
        is usually a mishearing. A tap is deliberate, and the wearer's hands
        are on the glasses, not a keyboard: the time is the title until the
        summary gives it a better one. A hold-to-talk note opens paused —
        nothing is captured until the touchpad is held.
        """
        return self._start(time.strftime("Note %H:%M"), mode=mode)

    def control_toggle(self) -> str:
        """The live page's tap: recording ⇄ paused."""
        note = self._active_note()
        if note is None:
            return "No Voice Note is open."
        if note.state == "recording" and not self._holding:
            return self._pause()
        if note.state == "paused":
            return self._resume()
        return f"Voice Note is {note.state}."

    def control_hold_start(self) -> str | None:
        """Touchpad held on the live page: listen until it is released.

        Only from paused — a continuous recording is already listening. The
        lane is taken with pre-roll, so the words spoken before the press
        registered as long are journaled too. None when the hold is ignored.
        """
        note = self._active_note()
        if note is None or self._holding or note.state != "paused":
            return None
        if self._discard_pending == note.capture_id or self._capture_error is not None:
            return None
        recording = self._notes.transition(
            note.capture_id, "recording", now_us=self._now(note),
        )
        self._holding = True
        self._hold_first = self._hold_last = None
        self._hold_loud = False
        if not self._begin_lane(preroll_us=HOLD_PREROLL_US):
            self._holding = False
            self._notes.transition(
                note.capture_id, "paused", now_us=self._now(recording),
            )
            self._notify()
            return "Voice Note could not listen because the microphone capture lane is busy."
        self._notify()
        return f"Listening for Voice Note: {note.title}."

    def control_hold_end(self) -> str | None:
        """Touchpad released: the held speech becomes one paragraph."""
        note = self._active_note()
        if note is None or not self._holding:
            return None
        self._release_hold(note)
        self._notify()
        return f"Paused Voice Note: {note.title}."

    def _release_hold(self, note: VoiceNote) -> tuple[int, int, int] | None:
        """End a hold; (paragraph, first, last), recorded as one input if loud."""
        self._holding = False
        self._end_lane()
        if note.state == "recording":
            self._notes.transition(note.capture_id, "paused", now_us=self._now(note))
        first, last, loud = self._hold_first, self._hold_last, self._hold_loud
        self._hold_first = self._hold_last = None
        self._hold_loud = False
        if first is None or last is None:
            return None
        held = (self._paragraph, first, last)
        if loud:
            self._inputs.append(held)
        return held

    def interrupt_for_shutdown(self) -> bool:
        """Close an active lane and retain raw data without claiming a save."""
        note = self._active_note()
        if note is None:
            return False
        if note.state == "recording" and self._audio.capturing:
            self._audio.end_capture(self._capture_callback)
        self._notes.transition(
            note.capture_id,
            "interrupted",
            now_us=self._now(note),
            completion_code="graceful_shutdown_raw_retained",
        )
        self._clear_active()
        return True

    def _start(self, title: str | None, *, mode: NoteMode = "continuous") -> str:
        if self._capture_id is not None:
            current = self._notes.get(self._capture_id)
            return f"Voice Note is already {current.state}: {current.title}."
        if title is None or not title.strip():
            return "Say a title after “start a voice note”."
        title = title.strip()
        if len(title) > 200 or "\x00" in title:
            return "Voice Note title must be 1 to 200 visible characters."
        try:
            title.encode("utf-8")
        except UnicodeEncodeError:
            return "Voice Note title must be valid text."
        capture_id = self._id_factory()
        now_us = self._clock_us()
        note = self._notes.create(
            capture_id=capture_id,
            idempotency_key=f"voice-note:{capture_id}",
            title=title,
            source_kind="g2.microphone",
            now_us=now_us,
        )
        self._clear_active()
        self._mode = mode
        if mode == "hold":
            self._notes.transition(capture_id, "paused", now_us=self._now(note))
            self._capture_id = note.capture_id
            self._notify()
            return f"Opened QuickNote: {title}. Hold the touchpad to talk."
        self._notes.transition(capture_id, "recording", now_us=self._now(note))
        self._capture_id = note.capture_id
        if not self._begin_lane():
            self._capture_id = None
            self._notes.transition(
                capture_id,
                "failed",
                now_us=self._now(self._notes.get(capture_id)),
                completion_code="audio_lane_unavailable",
            )
            return "Voice Note could not start because the microphone capture lane is busy."
        self._notify()
        return (
            f"Recording Voice Note: {title}. Use the Voice Note screen to pause, "
            "save, or discard."
        )

    def _begin_lane(self, *, preroll_us: int = 0) -> bool:
        """Take the capture lane for a fresh contiguous stretch of audio."""
        self._paragraph += 1
        self._last_generation = None
        self._last_seq = None
        self._last_received_at_us = None
        self._vad = RmsVad()
        self._capture_began = self._monotonic()
        self._last_audio = None
        try:
            self._audio.begin_capture(
                self._capture_callback,
                preroll_us=preroll_us,
                now_us=time.time_ns() // 1_000 if preroll_us else None,
            )
        except AudioRouteConflictError:
            return False
        self._listen_since = self._monotonic()
        return True

    def _end_lane(self) -> None:
        """Release the lane and hand the stretch's last chunk to STT."""
        if self._listen_since is not None:
            self._listened_before += max(0.0, self._monotonic() - self._listen_since)
            self._listen_since = None
        if self._audio.capturing:
            try:
                self._audio.end_capture(self._capture_callback)
            except AudioRouteConflictError:
                log.warning("voice note lane was owned by another capture at release")
        self._capture_began = None
        self._last_audio = None
        self._cut()

    def _pause(self) -> str:
        note = self._active_note()
        if note is None:
            return "No Voice Note is recording."
        if note.state == "paused":
            return f"Voice Note is already paused: {note.title}."
        if note.state != "recording":
            return f"Voice Note cannot pause while it is {note.state}."
        if self._capture_error is not None:
            return "Voice Note capture already stopped after a failure. Say stop voice note."
        paused = self._notes.transition(
            note.capture_id,
            "paused",
            now_us=self._now(note),
        )
        self._holding = False
        self._end_lane()
        self._notes.append_system(
            note.capture_id,
            code="wearer_paused",
            detail="capture paused explicitly",
            now_us=self._now(paused),
        )
        self._notify()
        return f"Paused Voice Note: {note.title}."

    def _resume(self) -> str:
        note = self._active_note()
        if note is None:
            return "No Voice Note is paused."
        if note.state == "recording":
            return f"Voice Note is already recording: {note.title}."
        if self._discard_pending == note.capture_id:
            return "Say confirm discard voice note or keep voice note first."
        if note.state != "paused":
            return f"Voice Note cannot resume while it is {note.state}."
        recording = self._notes.transition(
            note.capture_id,
            "recording",
            now_us=self._now(note),
        )
        if not self._begin_lane():
            self._notes.transition(
                note.capture_id,
                "paused",
                now_us=self._now(recording),
            )
            return "Voice Note could not resume because the microphone capture lane is busy."
        self._notes.append_system(
            note.capture_id,
            code="wearer_resumed",
            detail="capture resumed explicitly",
            now_us=self._now(recording),
        )
        self._notify()
        return f"Resumed Voice Note: {note.title}."

    def _request_discard(self) -> str:
        note = self._active_note()
        if note is None:
            return "No Voice Note is active."
        if self._discard_pending == note.capture_id:
            return (
                f"Discard Voice Note {note.title}? Say confirm discard voice note "
                "or keep voice note."
            )
        self._resume_after_keep = False
        if note.state == "recording" and self._audio.capturing:
            # Keep resumes only what discard interrupted: a continuous
            # recording, never a hold the wearer has since released.
            self._resume_after_keep = not self._holding
            paused = self._notes.transition(
                note.capture_id,
                "paused",
                now_us=self._now(note),
            )
            self._holding = False
            self._end_lane()
            self._notes.append_system(
                note.capture_id,
                code="discard_confirmation_pending",
                detail="capture paused before destructive confirmation",
                now_us=self._now(paused),
            )
        self._discard_pending = note.capture_id
        self._notify()
        return (
            f"Discard Voice Note {note.title}? Say confirm discard voice note "
            "or keep voice note."
        )

    def _confirm_discard(self) -> str:
        note = self._active_note()
        if note is None or self._discard_pending != note.capture_id:
            return "No Voice Note discard is awaiting confirmation."
        discarded = self._notes.discard(note.capture_id, now_us=self._now(note))
        self._clear_active()
        assert discarded.title is None
        self._notify()
        return "Discarded Voice Note and purged its local audio and transcript."

    def _keep(self) -> str:
        note = self._active_note()
        if note is None or self._discard_pending != note.capture_id:
            return "No Voice Note discard is awaiting confirmation."
        self._discard_pending = None
        resume = self._resume_after_keep
        self._resume_after_keep = False
        if note.state != "paused" or not resume:
            self._notify()
            return f"Kept Voice Note: {note.title}."
        recording = self._notes.transition(
            note.capture_id,
            "recording",
            now_us=self._now(note),
        )
        if not self._begin_lane():
            self._notes.transition(
                note.capture_id,
                "paused",
                now_us=self._now(recording),
            )
            self._notify()
            return (
                f"Kept Voice Note {note.title}, but it remains paused because the "
                "microphone capture lane is busy."
            )
        self._notes.append_system(
            note.capture_id,
            code="discard_cancelled",
            detail="wearer kept the note and capture resumed",
            now_us=self._now(recording),
        )
        self._notify()
        return f"Kept and resumed Voice Note: {note.title}."

    async def _stop(self) -> str:
        note = self._active_note()
        if note is None:
            return "No Voice Note is active."
        self._holding = False
        if note.state == "recording" and self._audio.capturing:
            self._audio.end_capture(self._capture_callback)
        self._cut()
        if self._capture_error is not None:
            failed = self._notes.transition(
                note.capture_id,
                "failed",
                now_us=self._now(note),
                completion_code=self._capture_error,
            )
            self._clear_active()
            self._notify()
            return (
                f"Voice Note stopped after a storage failure. Raw recovery data retained "
                f"for {failed.title}."
            )
        if note.received_frames == 0 and self._notes.frame_count(note.capture_id) == 0:
            # A note that never heard anything (a quick note nobody held) is
            # not worth a row, a summary or an Apple Note.
            self._notes.discard(note.capture_id, now_us=self._now(note))
            self._clear_active()
            self._notify()
            return "Nothing was recorded, so the note was discarded."
        finalizing = self._notes.transition(
            note.capture_id,
            "finalizing",
            now_us=self._now(note),
        )
        self._notify()
        transcript_complete = await self._finish_transcription(note.capture_id)
        complete = self._notes.frame_count(note.capture_id) > 0 and transcript_complete
        saved = self._notes.finalize(
            note.capture_id,
            complete=complete,
            completion_code=(
                "transcript_checkpointed_raw_purged"
                if complete
                else "incomplete_raw_retained"
            ),
            now_us=self._now(self._notes.get(finalizing.capture_id)),
        )
        self._clear_active()
        self._notify()
        if saved.state in ("saved", "saved_degraded") and self._on_saved is not None:
            try:
                self._on_saved(saved.capture_id)
            except Exception:
                # A post-save hook must never cost the wearer their receipt.
                pass
        return self._saved_receipt(saved)

    def _record_frame(self, frame: AudioFrame) -> None:
        capture_id = self._capture_id
        if capture_id is None:
            return
        if (
            frame.connection_generation == self._journal_generation
            and self._journal_seq is not None
            and frame.seq <= self._journal_seq
        ):
            # A hold's pre-roll reaching back over audio this note already
            # journaled: never a second copy, never a false gap.
            return
        try:
            if not frame.pcm:
                self._notes.append_gap(
                    capture_id,
                    code="empty_audio_frame",
                    detail=f"generation {frame.connection_generation} seq {frame.seq}",
                    now_us=frame.received_at_us,
                )
                return
            current = self._notes.get(capture_id)
            if current.raw_bytes + len(frame.pcm) > self._max_raw_bytes:
                self._notes.append_gap(
                    capture_id,
                    code="capture_quota_exceeded",
                    detail="recording stopped before local raw-audio quota was exceeded",
                    now_us=frame.received_at_us,
                )
                self._capture_error = "capture_quota_exceeded_raw_retained"
                if self._audio.capturing:
                    self._audio.end_capture(self._capture_callback)
                return
            if self._last_generation is not None:
                discontinuity = True
                if frame.connection_generation != self._last_generation:
                    self._notes.append_gap(
                        capture_id,
                        code="connection_replaced",
                        detail="audio connection generation changed during capture",
                        now_us=frame.received_at_us,
                    )
                elif self._last_seq is not None and frame.seq != self._last_seq + 1:
                    self._notes.append_gap(
                        capture_id,
                        code="sequence_gap",
                        detail=f"expected {self._last_seq + 1}, received {frame.seq}",
                        now_us=frame.received_at_us,
                    )
                elif (
                    self._last_received_at_us is not None
                    and frame.received_at_us - self._last_received_at_us > _STALL_GAP_US
                ):
                    # Contiguous sequence, discontinuous wall clock: the host
                    # paused audio delivery (suspension/lock) mid-recording.
                    stalled_s = (frame.received_at_us - self._last_received_at_us) // 1_000_000
                    self._notes.append_gap(
                        capture_id,
                        code="capture_stalled",
                        detail=f"~{stalled_s}s without audio while recording",
                        now_us=frame.received_at_us,
                    )
                else:
                    discontinuity = False
                if discontinuity:
                    # A chunk never spans a hole: STT must not splice the two
                    # sides of it into one invented sentence.
                    self._cut()
                    self._vad = RmsVad()
            journaled = self._notes.append_frame(
                capture_id,
                connection_generation=frame.connection_generation,
                seq=frame.seq,
                source=frame.source,
                speaker=frame.speaker,
                pcm=frame.pcm,
                received_at_us=frame.received_at_us,
            )
            self._last_generation = frame.connection_generation
            self._last_seq = frame.seq
            self._last_received_at_us = frame.received_at_us
            self._journal_generation = frame.connection_generation
            self._journal_seq = frame.seq
            self._last_audio = self._monotonic()
            if self._holding:
                if self._hold_first is None:
                    self._hold_first = journaled.ordinal
                self._hold_last = journaled.ordinal
            self._extend_chunk(journaled, frame)
        except Exception:
            # Stop receiving immediately. The active durable row and any raw
            # frames already committed remain available for explicit recovery.
            self._capture_error = "capture_journal_failed_raw_retained"
            if self._audio.capturing:
                self._audio.end_capture(self._capture_callback)
        self._notify()

    def _extend_chunk(self, journaled: VoiceNoteFrame, frame: AudioFrame) -> None:
        chunk = self._chunk
        if chunk is None:
            chunk = self._chunk = _Chunk(
                journaled.ordinal, journaled.ordinal, False,
                in_hold=self._holding, paragraph=self._paragraph,
            )
            self._chunk_bytes = 0
        chunk.last = journaled.ordinal
        self._chunk_bytes += len(frame.pcm)
        if rms(frame.pcm) >= _SILENCE_RMS:
            chunk.loud = True
            if chunk.in_hold:
                self._hold_loud = True
        utterance_closed = self._vad.feed(frame.pcm) is not None
        if utterance_closed or self._chunk_bytes >= _MAX_CHUNK_BYTES:
            self._cut()

    def _cut(self) -> None:
        """Close the open chunk and hand it to the transcription worker."""
        chunk = self._chunk
        self._chunk = None
        self._chunk_bytes = 0
        if chunk is None:
            return
        if not chunk.in_hold:
            if chunk.loud:
                # A continuous stretch is one input, grown chunk by chunk; a
                # hold is recorded whole when it is released.
                if self._inputs and self._inputs[-1][0] == chunk.paragraph:
                    paragraph, first, _last = self._inputs[-1]
                    self._inputs[-1] = (paragraph, first, chunk.last)
                else:
                    self._inputs.append((chunk.paragraph, chunk.first, chunk.last))
            elif chunk.paragraph == self._paragraph:
                # A long silence (a whole chunk of it) ends the paragraph.
                self._paragraph += 1
        self._queue.append(chunk)
        if self._worker is None or self._worker.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # No loop (a synchronous caller): Save transcribes it.
                return
            self._worker = loop.create_task(self._drain())

    def _transcribing(self) -> bool:
        return bool(self._queue) or (self._worker is not None and not self._worker.done())

    async def _drain(self) -> None:
        capture_id = self._capture_id
        while self._queue:
            chunk = self._queue.popleft()
            if capture_id is None or capture_id != self._capture_id:
                return
            try:
                await self._transcribe_chunk(capture_id, chunk, final=False)
            except Exception:
                log.exception("live note transcription failed")
                self._failed.append(chunk)
            self._notify()

    async def _transcribe_chunk(self, capture_id: str, chunk: _Chunk, *, final: bool) -> bool:
        """Account for one chunk. False when it stays untranscribed."""
        if self._notes.is_undone(capture_id, chunk.first, chunk.last):
            # The wearer took this input back before its words arrived; the
            # undone span already accounts for the audio.
            return True
        if not chunk.loud:
            self._notes.append_quiet_span(
                capture_id,
                first_frame_ordinal=chunk.first,
                last_frame_ordinal=chunk.last,
                reason="silence",
                now_us=self._now(self._notes.get(capture_id)),
            )
            return True
        if not self._stt_available:
            if not final:
                self._failed.append(chunk)
            return False
        frames = self._notes.frames_between(capture_id, chunk.first, chunk.last)
        pcm = b"".join(frame.pcm for frame in frames)
        try:
            transcript = await self._stt.transcribe_once(pcm)
        except Exception as exc:
            if not isinstance(exc, SttUnavailableError):
                log.warning("note chunk transcription raised %s", type(exc).__name__)
            if not final:
                self._failed.append(chunk)
            return False
        if transcript is None or not transcript.text.strip():
            self._notes.append_quiet_span(
                capture_id,
                first_frame_ordinal=chunk.first,
                last_frame_ordinal=chunk.last,
                reason="no_words",
                now_us=self._now(self._notes.get(capture_id)),
            )
            # No words, no paragraph: Undo must reach the one before it.
            entry = (chunk.paragraph, chunk.first, chunk.last)
            if entry in self._inputs:
                self._inputs.remove(entry)
            self._advance_paragraph(chunk)
            return True
        self._add_words(capture_id, chunk, transcript.text.strip())
        return True

    def _advance_paragraph(self, chunk: _Chunk) -> None:
        """An accounted chunk with no words keeps its paragraph contiguous."""
        para = self._open_para
        if para is not None and para[1] == chunk.paragraph and chunk.first == para[2] + 1:
            self._open_para = (para[0], para[1], chunk.last, para[3])

    def _add_words(self, capture_id: str, chunk: _Chunk, words: str) -> None:
        """The chunk's words continue its paragraph, or begin a new one.

        Only the very next audio of the same stretch or hold continues it: a
        chunk retried out of order, or one after a chunk that failed, starts
        its own paragraph rather than splice words out of order.
        """
        para = self._open_para
        now_us = self._now(self._notes.get(capture_id))
        if (
            para is not None
            and para[1] == chunk.paragraph
            and chunk.first == para[2] + 1
            and len(para[3]) + 1 + len(words) <= _MAX_PARAGRAPH_CHARS
        ):
            text = f"{para[3]} {words}"
            self._notes.extend_transcript(
                capture_id, ordinal=para[0], text=text,
                last_frame_ordinal=chunk.last, now_us=now_us,
            )
            self._open_para = (para[0], para[1], chunk.last, text)
            return
        segment = self._notes.append_transcript(
            capture_id,
            text=words,
            first_frame_ordinal=chunk.first,
            last_frame_ordinal=chunk.last,
            now_us=now_us,
        )
        self._open_para = (segment.ordinal, chunk.paragraph, chunk.last, words)

    async def _finish_transcription(self, capture_id: str) -> bool:
        """Save's half of transcription: drain, retry, then account for the rest.

        Live transcription normally leaves at most the last chunk. Chunks the
        engine could not take get one more attempt; anything still
        untranscribed — and, defensively, any frame no chunk accounted for —
        is recorded as a gap with its raw audio kept, exactly as the
        whole-recording transcription did before FEAT-002.
        """
        worker = self._worker
        if worker is not None and not worker.done():
            try:
                await worker
            except Exception:
                log.exception("live note transcription worker failed")
        while self._queue:
            await self._transcribe_chunk(capture_id, self._queue.popleft(), final=False)
        retry, self._failed = self._failed, []
        unaccounted: list[_Chunk] = []
        for chunk in retry:
            if not await self._transcribe_chunk(capture_id, chunk, final=True):
                unaccounted.append(chunk)
        tried = {
            ordinal for chunk in unaccounted for ordinal in range(chunk.first, chunk.last + 1)
        }
        leftovers = tuple(
            frame for frame in self._notes.uncovered_frames(capture_id)
            if frame.ordinal not in tried
        )
        for group in self._transcription_groups(leftovers):
            chunk = _Chunk(group[0].ordinal, group[-1].ordinal, loud=False)
            chunk.loud = any(rms(frame.pcm) >= _SILENCE_RMS for frame in group)
            if not await self._transcribe_chunk(capture_id, chunk, final=True):
                unaccounted.append(chunk)
        if not unaccounted:
            return True
        if not self._stt_available:
            # One honest marker, not one per chunk, for a Mac without STT.
            self._notes.append_gap(
                capture_id,
                code="stt_unavailable",
                detail="raw audio retained for later transcription",
                now_us=self._now(self._notes.get(capture_id)),
            )
            return False
        for chunk in unaccounted:
            self._notes.append_gap(
                capture_id,
                code="transcription_gap",
                detail=f"frames {chunk.first}-{chunk.last} retained as raw audio",
                now_us=self._now(self._notes.get(capture_id)),
            )
        return False

    @staticmethod
    def _transcription_groups(
        frames: tuple[VoiceNoteFrame, ...],
    ) -> tuple[tuple[VoiceNoteFrame, ...], ...]:
        groups: list[tuple[VoiceNoteFrame, ...]] = []
        current: list[VoiceNoteFrame] = []
        current_bytes = 0
        for frame in frames:
            prior = current[-1] if current else None
            discontinuity = prior is not None and (
                frame.connection_generation != prior.connection_generation
                or frame.seq != prior.seq + 1
                or frame.ordinal != prior.ordinal + 1
            )
            over_limit = bool(current) and current_bytes + len(frame.pcm) > _MAX_TRANSCRIBE_BYTES
            if discontinuity or over_limit:
                groups.append(tuple(current))
                current = []
                current_bytes = 0
            current.append(frame)
            current_bytes += len(frame.pcm)
        if current:
            groups.append(tuple(current))
        return tuple(groups)

    def _active_note(self) -> VoiceNote | None:
        return self._notes.get(self._capture_id) if self._capture_id is not None else None

    def _now(self, note: VoiceNote) -> int:
        """Keep lifecycle state monotonic even if the host wall clock rolls back."""
        return max(self._clock_us(), note.updated_at_us)

    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            try:
                listener()
            except Exception:
                log.exception("voice note listener failed")

    def _clear_active(self) -> None:
        self._capture_id = None
        self._mode = "continuous"
        self._holding = False
        self._last_generation = None
        self._last_seq = None
        self._last_received_at_us = None
        self._journal_generation = None
        self._journal_seq = None
        self._capture_error = None
        self._discard_pending = None
        self._resume_after_keep = False
        self._chunk = None
        self._chunk_bytes = 0
        self._queue.clear()
        self._failed = []
        self._inputs = []
        self._open_para = None
        self._hold_first = self._hold_last = None
        self._hold_loud = False
        self._capture_began = None
        self._last_audio = None
        self._listened_before = 0.0
        self._listen_since = None
        worker = self._worker
        self._worker = None
        if worker is not None and not worker.done():
            worker.cancel()

    def _show_last(self) -> str:
        note = self._notes.latest_retained()
        if note is None:
            return "There is no retained Voice Note."
        noun = "gap" if note.gap_count == 1 else "gaps"
        sections = (
            "section" if note.transcribed_segments == 1 else "sections"
        )
        return (
            f"Last Voice Note: {note.title} · {note.state} · "
            f"{note.transcribed_segments} transcript {sections} · "
            f"{note.gap_count} {noun}."
        )

    def _export_last(self) -> str:
        note = self._notes.latest_retained()
        if note is None:
            return "There is no retained Voice Note to export."
        export_root = self._export_directory
        export_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(export_root, 0o700)
        stamp = f"{self._clock_us():020d}"
        basename = f"voice-note-{stamp}-{note.capture_id[:12]}"
        destination = export_root / basename
        created_destination = False
        try:
            for suffix in range(1, 101):
                destination = export_root / (
                    basename if suffix == 1 else f"{basename}-{suffix}"
                )
                try:
                    destination.mkdir(mode=0o700)
                    created_destination = True
                    break
                except FileExistsError:
                    continue
            if not created_destination:
                return "Voice Note export failed because every safe destination was occupied."
            frames = self._notes.frames(note.capture_id)
            audio_files: list[str] = []
            for index, group in enumerate(self._transcription_groups(frames), start=1):
                filename = f"audio-{index:03d}.wav"
                self._write_wav(
                    destination / filename,
                    b"".join(frame.pcm for frame in group),
                )
                audio_files.append(filename)
            self._write_manifest(destination / "note.md", note, tuple(audio_files))
        except Exception:
            if created_destination and destination.is_dir():
                for child in destination.iterdir():
                    if child.is_file():
                        child.unlink()
                destination.rmdir()
            return "Voice Note export failed; no existing export was overwritten."
        except BaseException:
            if created_destination and destination.is_dir():
                for child in destination.iterdir():
                    if child.is_file():
                        child.unlink()
                destination.rmdir()
            raise
        return f"Exported Voice Note locally: {destination}."

    def _write_manifest(
        self,
        path: Path,
        note: VoiceNote,
        audio_files: tuple[str, ...],
    ) -> None:
        completeness = "complete" if note.state == "saved" else "incomplete"
        lines = [
            f"# {note.title}",
            "",
            f"- State: `{note.state}`",
            f"- Transcript completeness: **{completeness}**",
            f"- Received frames: {note.received_frames}",
            f"- Journaled frames: {note.journaled_frames}",
            f"- Recorded gaps: {note.gap_count}",
            f"- Raw audio retained: {'yes' if note.raw_retained else 'no'}",
            "",
            "## Transcript and capture markers",
            "",
        ]
        for segment in self._notes.segments(note.capture_id):
            if segment.kind == "transcript":
                lines.append(segment.text)
            elif segment.kind == "gap":
                lines.append(f"**AUDIO/TRANSCRIPTION GAP:** {segment.text}")
            else:
                lines.append(f"*{segment.text}*")
            lines.append("")
        if audio_files:
            lines.extend([
                "## Recoverable raw audio",
                "",
                "Discontinuous regions are separate WAV files; they were not joined "
                "into a falsely seamless recording.",
                "",
            ])
            lines.extend(f"- `{filename}`" for filename in audio_files)
            lines.append("")
        payload = "\n".join(lines).encode("utf-8")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _write_wav(path: Path, pcm: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "wb") as stream:
            with wave.open(stream, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16_000)
                wav.writeframes(pcm)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _saved_receipt(note: VoiceNote) -> str:
        if note.state == "saved":
            return (
                f"Saved Voice Note: {note.title}. "
                f"{note.transcribed_segments} transcript section"
                f"{'s' if note.transcribed_segments != 1 else ''}; raw audio purged."
            )
        return (
            f"Saved Voice Note with gaps: {note.title}. Raw audio retained locally "
            "for recovery; the transcript is not claimed complete."
        )


__all__ = [
    "DEFAULT_TITLE",
    "HOLD_PREROLL_US",
    "LiveLine",
    "NoteMode",
    "VoiceNoteCaptureController",
    "VoiceNoteCaptureSnapshot",
]
