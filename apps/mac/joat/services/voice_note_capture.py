"""Explicit, local Voice Note capture with durable gap and retention truth."""

from __future__ import annotations

import os
import re
import secrets
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..audio.router import AudioRouteConflictError, AudioRouter
from ..core.interfaces import AudioFrame, SttEngine
from ..persistence.voice_notes import VoiceNote, VoiceNoteFrame, VoiceNoteStore


# The wearer calls this feature both "voice note" and "quicknote"; whisper
# also splits or joins the compound unpredictably. Every control phrase
# accepts all three spellings so the recorder never looks deaf to its own
# name.
_NOTE_NOUNS = ("voice note", "quick note", "quicknote")


def _phrases(*templates: str) -> frozenset[str]:
    return frozenset(
        template.format(noun=noun) for template in templates for noun in _NOTE_NOUNS
    )


_START = re.compile(
    r"^(?:start|begin)(?: recording)? (?:a )?(?:voice note|quick ?note)"
    r"(?: (?:called|titled|about))?"
    r"(?: (?P<title>.+))?$",
    re.IGNORECASE,
)
_STOP = _phrases("stop {noun}", "save {noun}", "finish {noun}")
_PAUSE = _phrases("pause {noun}")
_RESUME = _phrases("resume {noun}", "continue {noun}")
_DISCARD = _phrases("discard {noun}", "cancel {noun}")
_CONFIRM_DISCARD = _phrases("confirm discard {noun}")
_KEEP = _phrases("keep {noun}", "cancel discard {noun}")
_SHOW_LAST = _phrases("show last {noun}")
_EXPORT_LAST = _phrases("export last {noun}")
_MAX_TRANSCRIBE_BYTES = 20 * 16_000 * 2
_DEFAULT_RAW_QUOTA_BYTES = 30 * 60 * 16_000 * 2
# Audio batches arrive every ~200ms while capture is healthy. A much longer
# silence with CONTIGUOUS sequence numbers is the phone suspending audio
# delivery (the Even app backgrounded, the phone locked) — the seq-gap check
# cannot see it, so it must be journaled from wall-clock discontinuity or the
# transcript would silently claim completeness across a hole.
_STALL_GAP_US = 5_000_000


@dataclass(frozen=True, slots=True)
class VoiceNoteCaptureSnapshot:
    capture_id: str | None
    title: str | None
    state: str
    received_frames: int
    journaled_frames: int
    gap_count: int
    failure_code: str | None


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
        self._capture_id: str | None = None
        self._last_generation: str | None = None
        self._last_seq: int | None = None
        self._last_received_at_us: int | None = None
        self._capture_error: str | None = None
        self._discard_pending: str | None = None
        # Bound method identity is otherwise recreated on every attribute read;
        # AudioRouter requires the exact owner that acquired the capture lane.
        self._capture_callback = self._record_frame

    @property
    def active(self) -> bool:
        return self._capture_id is not None

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
        )

    async def handle_command(self, text: str) -> str | None:
        normalized = " ".join(text.strip().split())
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

    async def control_stop(self) -> str:
        return await self._stop()

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

    def _start(self, title: str | None) -> str:
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
        self._notes.transition(capture_id, "recording", now_us=self._now(note))
        try:
            self._audio.begin_capture(self._capture_callback)
        except AudioRouteConflictError:
            self._notes.transition(
                capture_id,
                "failed",
                now_us=self._now(self._notes.get(capture_id)),
                completion_code="audio_lane_unavailable",
            )
            return "Voice Note could not start because the microphone capture lane is busy."
        self._capture_id = note.capture_id
        self._last_generation = None
        self._last_seq = None
        self._last_received_at_us = None
        self._capture_error = None
        return (
            f"Recording Voice Note: {title}. Use the Voice Note screen to pause, "
            "save, or discard."
        )

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
        self._audio.end_capture(self._capture_callback)
        self._notes.append_system(
            note.capture_id,
            code="wearer_paused",
            detail="capture paused explicitly",
            now_us=self._now(paused),
        )
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
        try:
            self._audio.begin_capture(self._capture_callback)
        except AudioRouteConflictError:
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
        self._last_generation = None
        self._last_seq = None
        self._last_received_at_us = None
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
        if note.state == "recording" and self._audio.capturing:
            paused = self._notes.transition(
                note.capture_id,
                "paused",
                now_us=self._now(note),
            )
            self._audio.end_capture(self._capture_callback)
            self._notes.append_system(
                note.capture_id,
                code="discard_confirmation_pending",
                detail="capture paused before destructive confirmation",
                now_us=self._now(paused),
            )
        self._discard_pending = note.capture_id
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
        return "Discarded Voice Note and purged its local audio and transcript."

    def _keep(self) -> str:
        note = self._active_note()
        if note is None or self._discard_pending != note.capture_id:
            return "No Voice Note discard is awaiting confirmation."
        self._discard_pending = None
        if note.state != "paused":
            return f"Kept Voice Note: {note.title}."
        recording = self._notes.transition(
            note.capture_id,
            "recording",
            now_us=self._now(note),
        )
        try:
            self._audio.begin_capture(self._capture_callback)
        except AudioRouteConflictError:
            self._notes.transition(
                note.capture_id,
                "paused",
                now_us=self._now(recording),
            )
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
        self._last_generation = None
        self._last_seq = None
        self._last_received_at_us = None
        return f"Kept and resumed Voice Note: {note.title}."

    async def _stop(self) -> str:
        note = self._active_note()
        if note is None:
            return "No Voice Note is active."
        if note.state == "recording" and self._audio.capturing:
            self._audio.end_capture(self._capture_callback)
        if self._capture_error is not None:
            failed = self._notes.transition(
                note.capture_id,
                "failed",
                now_us=self._now(note),
                completion_code=self._capture_error,
            )
            self._clear_active()
            return (
                f"Voice Note stopped after a storage failure. Raw recovery data retained "
                f"for {failed.title}."
            )
        finalizing = self._notes.transition(
            note.capture_id,
            "finalizing",
            now_us=self._now(note),
        )
        frames = self._notes.frames(note.capture_id)
        transcript_complete = await self._transcribe(note.capture_id, frames)
        complete = bool(frames) and transcript_complete
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
            self._notes.append_frame(
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
        except Exception:
            # Stop receiving immediately. The active durable row and any raw
            # frames already committed remain available for explicit recovery.
            self._capture_error = "capture_journal_failed_raw_retained"
            if self._audio.capturing:
                self._audio.end_capture(self._capture_callback)

    async def _transcribe(
        self,
        capture_id: str,
        frames: tuple[VoiceNoteFrame, ...],
    ) -> bool:
        if not frames or not self._stt_available:
            if frames:
                self._notes.append_gap(
                    capture_id,
                    code="stt_unavailable",
                    detail="raw audio retained for later transcription",
                    now_us=self._now(self._notes.get(capture_id)),
                )
            return False
        complete = True
        for group in self._transcription_groups(frames):
            first, last = group[0].ordinal, group[-1].ordinal
            try:
                self._stt.reset()
                await self._stt.feed(b"".join(frame.pcm for frame in group))
                transcript = await self._stt.flush()
                if transcript is None or not transcript.text.strip():
                    raise RuntimeError("STT produced no transcript")
                self._notes.append_transcript(
                    capture_id,
                    text=transcript.text.strip(),
                    first_frame_ordinal=first,
                    last_frame_ordinal=last,
                    now_us=self._now(self._notes.get(capture_id)),
                )
            except Exception:
                complete = False
                self._notes.append_gap(
                    capture_id,
                    code="transcription_gap",
                    detail=f"frames {first}-{last} retained as raw audio",
                    now_us=self._now(self._notes.get(capture_id)),
                )
            finally:
                self._stt.reset()
        return complete

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

    def _clear_active(self) -> None:
        self._capture_id = None
        self._last_generation = None
        self._last_seq = None
        self._last_received_at_us = None
        self._capture_error = None
        self._discard_pending = None

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


__all__ = ["VoiceNoteCaptureController", "VoiceNoteCaptureSnapshot"]
