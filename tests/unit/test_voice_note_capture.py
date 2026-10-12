"""Mac-local Voice Note control remains explicit and loss-accounted."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
import stat
import struct
import time

from joat.audio.router import AudioRouter
from joat.core.interfaces import (
    AudioFrame,
    AudioFrameCallback,
    AudioFrameSource,
    SttEngine,
    SttUnavailableError,
    Transcript,
)
from joat.persistence.store import DurableStore
from joat.persistence.voice_notes import VoiceNoteStore
from joat.services.voice_note_capture import (
    LiveLine,
    VoiceNoteCaptureController,
)


#: 40 samples at ±3000 — well above the near-silence floor, so a chunk of
#: it goes to STT. (Live transcription never sends near-silence to STT.)
SPEECH = struct.pack("<40h", *([3000, -3000] * 20))
#: 40 samples of digital near-silence: accounted for, never transcribed.
SILENCE = b"\x01\x00" * 40


class FakeFrameSource(AudioFrameSource):
    def __init__(self) -> None:
        self.callback: AudioFrameCallback | None = None

    def on_audio_frame(self, callback: AudioFrameCallback) -> None:
        self.callback = callback

    def emit(
        self, seq: int, *, generation: str = "socket-a", pcm: bytes = SPEECH,
        speaker: str | None = "wearer", received_at_us: int | None = None,
    ) -> None:
        assert self.callback is not None
        self.callback(AudioFrame(
            pcm=pcm,
            connection_generation=generation,
            seq=seq,
            source="glasses",
            speaker=speaker,
            received_at_us=100 + seq if received_at_us is None else received_at_us,
        ))


class FakeStt(SttEngine):
    def __init__(self, text: str | None = "Draft launch plan") -> None:
        self.text = text
        self.fed = bytearray()
        self.resets = 0

    async def feed(self, pcm: bytes) -> Transcript | None:
        self.fed.extend(pcm)
        return None

    async def flush(self) -> Transcript | None:
        return Transcript(self.text, True) if self.text is not None else None

    def reset(self) -> None:
        self.resets += 1
        self.fed.clear()


def controller(
    path: Path,
    *,
    stt: FakeStt | None = None,
    stt_available: bool = True,
    max_raw_bytes: int = 30 * 60 * 16_000 * 2,
    export_directory: Path | None = None,
    on_saved: Callable[[str], None] | None = None,
) -> tuple[
    DurableStore,
    VoiceNoteStore,
    FakeFrameSource,
    VoiceNoteCaptureController,
]:
    durable = DurableStore(path)
    notes = VoiceNoteStore(durable)
    source = FakeFrameSource()
    capture = VoiceNoteCaptureController(
        notes,
        AudioRouter(source),
        stt or FakeStt(),
        stt_available=stt_available,
        clock_us=iter(range(1_000, 2_000)).__next__,
        id_factory=lambda: "capture-1",
        max_raw_bytes=max_raw_bytes,
        export_directory=export_directory,
        on_saved=on_saved,
    )
    return durable, notes, source, capture


async def test_complete_capture_checkpoints_transcript_then_purges_raw(tmp_path: Path) -> None:
    durable, notes, source, capture = controller(tmp_path / "joat.sqlite3")
    with durable:
        assert await capture.handle_command("start a voice note about Launch plan") == (
            "Recording Voice Note: Launch plan. "
            "Use the Voice Note screen to pause, save, or discard."
        )
        source.emit(0)
        source.emit(1)

        receipt = await capture.handle_command("stop voice note")

        assert receipt == (
            "Saved Voice Note: Launch plan. 1 transcript section; raw audio purged."
        )
        note = notes.get("capture-1")
        assert note.state == "saved"
        assert note.received_frames == note.journaled_frames == 2
        assert note.raw_retained is False
        assert notes.frames("capture-1") == ()
        assert [segment.text for segment in notes.segments("capture-1")] == [
            "Draft launch plan"
        ]


async def test_sequence_gap_retains_raw_and_refuses_complete_claim(tmp_path: Path) -> None:
    durable, notes, source, capture = controller(tmp_path / "joat.sqlite3")
    with durable:
        await capture.handle_command("start a voice note Field check")
        source.emit(0)
        source.emit(4)

        receipt = await capture.handle_command("save voice note")

        assert receipt is not None and receipt.startswith("Saved Voice Note with gaps")
        note = notes.get("capture-1")
        assert note.state == "saved_degraded"
        assert note.gap_count == 1
        assert note.raw_retained is True
        assert len(notes.frames("capture-1")) == 2


async def test_pause_resume_marks_intentional_boundary_without_journaling_pause(
    tmp_path: Path,
) -> None:
    durable, notes, source, capture = controller(tmp_path / "joat.sqlite3")
    with durable:
        await capture.handle_command("start a voice note Review")
        source.emit(0)
        assert await capture.handle_command("pause voice note") == "Paused Voice Note: Review."
        source.emit(1)
        assert len(notes.frames("capture-1")) == 1
        assert await capture.handle_command("resume voice note") == "Resumed Voice Note: Review."
        source.emit(7)

        await capture.handle_command("stop voice note")

        note = notes.get("capture-1")
        assert note.gap_count == 0
        assert [segment.kind for segment in notes.segments("capture-1")] == [
            "system",
            "system",
            "transcript",
            "transcript",
        ]


async def test_unavailable_stt_keeps_recoverable_raw_audio(tmp_path: Path) -> None:
    durable, notes, source, capture = controller(
        tmp_path / "joat.sqlite3",
        stt_available=False,
    )
    with durable:
        await capture.handle_command("start a voice note Offline")
        source.emit(0)

        receipt = await capture.handle_command("finish voice note")

        assert receipt is not None and "Raw audio retained locally" in receipt
        note = notes.get("capture-1")
        assert note.state == "saved_degraded"
        assert note.completion_code == "incomplete_raw_retained"


async def test_discard_purges_all_content(tmp_path: Path) -> None:
    durable, notes, source, capture = controller(tmp_path / "joat.sqlite3")
    with durable:
        await capture.handle_command("start a voice note Private")
        source.emit(0)

        assert await capture.handle_command("discard voice note") == (
            "Discard Voice Note Private? Say confirm discard voice note "
            "or keep voice note."
        )
        assert capture.snapshot.state == "paused"
        assert await capture.handle_command("confirm discard voice note") == (
            "Discarded Voice Note and purged its local audio and transcript."
        )
        discarded = notes.get("capture-1")
        assert discarded.state == "discarded"
        assert discarded.title is None
        assert discarded.raw_bytes == 0
        assert notes.frames("capture-1") == ()


async def test_cancelled_discard_resumes_the_same_capture(tmp_path: Path) -> None:
    durable, notes, source, capture = controller(tmp_path / "joat.sqlite3")
    with durable:
        await capture.handle_command("start a voice note Keep me")
        source.emit(0)
        await capture.handle_command("discard voice note")

        assert await capture.handle_command("keep voice note") == (
            "Kept and resumed Voice Note: Keep me."
        )
        source.emit(7)
        await capture.handle_command("stop voice note")

        kept = notes.get("capture-1")
        assert kept.state == "saved"
        assert kept.gap_count == 0
        assert [segment.kind for segment in notes.segments("capture-1")][0:2] == [
            "system",
            "system",
        ]


async def test_graceful_shutdown_marks_capture_interrupted(tmp_path: Path) -> None:
    durable, notes, source, capture = controller(tmp_path / "joat.sqlite3")
    with durable:
        await capture.handle_command("start a voice note Shutdown test")
        source.emit(0)

        assert capture.interrupt_for_shutdown() is True

        interrupted = notes.get("capture-1")
        assert interrupted.state == "interrupted"
        assert interrupted.raw_retained is True
        assert interrupted.completion_code == "graceful_shutdown_raw_retained"
        assert capture.active is False


async def test_raw_audio_quota_stops_capture_and_retains_recovery_data(
    tmp_path: Path,
) -> None:
    durable, notes, source, capture = controller(
        tmp_path / "joat.sqlite3",
        max_raw_bytes=80,
    )
    with durable:
        await capture.handle_command("start a voice note Quota")
        source.emit(0)
        source.emit(1)

        snapshot = capture.snapshot
        assert snapshot.failure_code == "capture_quota_exceeded_raw_retained"
        assert snapshot.journaled_frames == 1
        assert snapshot.gap_count == 1
        assert await capture.handle_command("stop voice note") == (
            "Voice Note stopped after a storage failure. "
            "Raw recovery data retained for Quota."
        )
        failed = notes.get("capture-1")
        assert failed.state == "failed"
        assert failed.raw_retained is True


async def test_oversized_title_is_declined_without_creating_a_capture(
    tmp_path: Path,
) -> None:
    durable, notes, _source, capture = controller(tmp_path / "joat.sqlite3")
    with durable:
        assert await capture.handle_command("start a voice note " + "x" * 201) == (
            "Voice Note title must be 1 to 200 visible characters."
        )
        assert capture.active is False
        assert notes.active() == ()


async def test_clock_rollback_cannot_strand_safe_stop(tmp_path: Path) -> None:
    durable = DurableStore(tmp_path / "joat.sqlite3")
    notes = VoiceNoteStore(durable)
    source = FakeFrameSource()
    times = iter((1_000, 1_001, 900, 800, 700, 600, 500, 400))
    capture = VoiceNoteCaptureController(
        notes,
        AudioRouter(source),
        FakeStt(),
        clock_us=times.__next__,
        id_factory=lambda: "capture-1",
    )
    with durable:
        await capture.handle_command("start a voice note Rollback")
        source.emit(0)

        receipt = await capture.handle_command("stop voice note")

        assert receipt is not None and receipt.startswith("Saved Voice Note: Rollback")
        saved = notes.get("capture-1")
        assert saved.state == "saved"
        assert saved.ended_at_us is not None
        assert saved.ended_at_us >= saved.created_at_us


async def test_degraded_export_keeps_discontinuous_audio_in_separate_private_files(
    tmp_path: Path,
) -> None:
    export_root = tmp_path / "exports"
    durable, _notes, source, capture = controller(
        tmp_path / "joat.sqlite3",
        export_directory=export_root,
    )
    with durable:
        await capture.handle_command("start a voice note Field audit")
        source.emit(0)
        source.emit(4)
        await capture.handle_command("stop voice note")

        result = await capture.handle_command("export last voice note")

        assert result is not None and result.startswith("Exported Voice Note locally: ")
        destinations = list(export_root.iterdir())
        assert len(destinations) == 1
        destination = destinations[0]
        assert stat.S_IMODE(destination.stat().st_mode) == 0o700
        manifest = destination / "note.md"
        assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
        text = manifest.read_text(encoding="utf-8")
        assert "Transcript completeness: **incomplete**" in text
        assert "AUDIO/TRANSCRIPTION GAP" in text
        wavs = sorted(destination.glob("audio-*.wav"))
        assert [path.name for path in wavs] == ["audio-001.wav", "audio-002.wav"]
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in wavs)


async def test_show_last_voice_note_reports_counts_without_exposing_transcript(
    tmp_path: Path,
) -> None:
    durable, _notes, source, capture = controller(tmp_path / "joat.sqlite3")
    with durable:
        await capture.handle_command("start a voice note Private review")
        source.emit(0)
        await capture.handle_command("stop voice note")

        shown = await capture.handle_command("show last voice note")

        assert shown == (
            "Last Voice Note: Private review · saved · "
            "1 transcript section · 0 gaps."
        )
        assert "Draft launch plan" not in shown


async def test_export_collision_preserves_existing_directory_and_uses_suffix(
    tmp_path: Path,
) -> None:
    durable = DurableStore(tmp_path / "joat.sqlite3")
    notes = VoiceNoteStore(durable)
    source = FakeFrameSource()
    export_root = tmp_path / "exports"
    capture = VoiceNoteCaptureController(
        notes,
        AudioRouter(source),
        FakeStt(),
        clock_us=lambda: 1_000,
        id_factory=lambda: "capture-1",
        export_directory=export_root,
    )
    with durable:
        await capture.handle_command("start a voice note Collision")
        source.emit(0)
        await capture.handle_command("stop voice note")
        occupied = export_root / "voice-note-00000000000000001000-capture-1"
        occupied.mkdir(parents=True)
        sentinel = occupied / "keep.txt"
        sentinel.write_text("mine", encoding="utf-8")

        result = await capture.handle_command("export last voice note")

        assert result is not None and result.endswith("-2.")
        assert sentinel.read_text(encoding="utf-8") == "mine"
        assert (export_root / (occupied.name + "-2") / "note.md").is_file()


async def test_host_audio_suspension_is_journaled_as_a_stall_gap(tmp_path: Path) -> None:
    """iOS suspends audio delivery while the WebView (and its seq counter)
    keep running. Contiguous sequence numbers across a long wall-clock hole
    must still record a gap — the transcript must never silently claim
    completeness across a suspension (device-features lifecycle contract)."""
    durable, notes, source, capture = controller(tmp_path / "joat.sqlite3")
    with durable:
        await capture.handle_command("start a voice note Suspension")
        assert source.callback is not None
        source.callback(AudioFrame(
            pcm=b"\x01\x00" * 40, connection_generation="socket-a", seq=0,
            source="glasses", speaker="wearer", received_at_us=1_000_000,
        ))
        # 12 seconds later, seq is still contiguous: the phone was suspended.
        source.callback(AudioFrame(
            pcm=b"\x01\x00" * 40, connection_generation="socket-a", seq=1,
            source="glasses", speaker="wearer", received_at_us=13_000_000,
        ))
        note = notes.get("capture-1")
        assert note.gap_count == 1
        gap = [s for s in notes.segments("capture-1") if s.kind == "gap"][0]
        assert gap.text == "[capture_stalled] ~12s without audio while recording"
        receipt = await capture.handle_command("stop voice note")
        # A stalled recording is honest: saved WITH gaps, raw retained.
        assert receipt is not None and receipt.startswith("Saved Voice Note with gaps")


async def test_quicknote_aliases_drive_the_same_recorder(tmp_path: Path) -> None:
    durable, notes, source, capture = controller(tmp_path / "joat.sqlite3")
    with durable:
        started = await capture.handle_command("start a quicknote about Standup")
        assert started is not None and started.startswith(
            "Recording Voice Note: Standup."
        )
        source.emit(0)
        assert await capture.handle_command("pause quicknote") == (
            "Paused Voice Note: Standup."
        )
        assert await capture.handle_command("resume quick note") == (
            "Resumed Voice Note: Standup."
        )
        source.emit(1, generation="socket-b")
        receipt = await capture.handle_command("save quicknote")
        assert receipt is not None and receipt.startswith("Saved Voice Note")
        shown = await capture.handle_command("show last quicknote")
        assert shown is not None and shown.startswith("Last Voice Note: Standup")


async def test_on_saved_hook_fires_only_for_retained_saves(tmp_path: Path) -> None:
    saved_ids: list[str] = []
    durable, notes, source, capture = controller(
        tmp_path / "joat.sqlite3", on_saved=saved_ids.append,
    )
    with durable:
        await capture.handle_command("start a voice note Hook check")
        source.emit(0)
        await capture.handle_command("stop voice note")
        assert saved_ids == ["capture-1"]

    # A discard must never fire the summarization hook.
    discard_ids: list[str] = []
    durable2, _, _, capture2 = controller(
        tmp_path / "joat-discard.sqlite3", on_saved=discard_ids.append,
    )
    with durable2:
        await capture2.handle_command("start a voice note Hook discard")
        await capture2.handle_command("discard voice note")
        await capture2.handle_command("confirm discard voice note")
        assert discard_ids == []


async def test_typed_controls_mirror_the_voice_grammar(tmp_path: Path) -> None:
    durable, notes, source, capture = controller(tmp_path / "joat.sqlite3")
    with durable:
        await capture.handle_command("start a voice note Controls")
        source.emit(0)
        assert capture.discard_pending is False
        assert capture.control_pause() == "Paused Voice Note: Controls."
        assert capture.control_resume() == "Resumed Voice Note: Controls."
        request = capture.control_request_discard()
        assert request.startswith("Discard Voice Note Controls?")
        assert capture.discard_pending is True
        assert capture.control_keep() == "Kept and resumed Voice Note: Controls."
        assert capture.discard_pending is False
        source.emit(1, generation="socket-b")
        receipt = await capture.control_stop()
        assert receipt.startswith("Saved Voice Note")
        assert capture.active is False


async def test_whisper_terminal_punctuation_never_deafens_the_grammar(
    tmp_path: Path,
) -> None:
    """Short utterances come back punctuated ("Pause recording."). Terminal
    punctuation is STT noise, not part of the lifecycle command."""
    durable, notes, source, capture = controller(tmp_path / "joat.sqlite3")
    with durable:
        assert await capture.handle_command("Start a quicknote called Standup.") is not None
        source.emit(0)
        assert await capture.handle_command("Pause recording.") == "Paused Voice Note: Standup."
        assert await capture.handle_command("Resume recording!") == "Resumed Voice Note: Standup."
        receipt = await capture.handle_command("Stop the quicknote.")
        assert receipt is not None and receipt.startswith("Saved Voice Note")


# ── FEAT-002: live transcription, hold-to-talk ──────────────────────────────

#: 200 ms batches, the transport's real cadence: speech and silence.
SPEECH_200MS = struct.pack("<3200h", *([3000, -3000] * 1600))
QUIET_200MS = b"\x00\x00" * 3200


class ScriptedStt(FakeStt):
    """A stateless engine (whisper serve's shape): one answer per chunk."""

    def __init__(self, answers: list[str | None | Exception]) -> None:
        super().__init__()
        self.answers = answers
        self.calls = 0

    async def transcribe_once(self, pcm: bytes) -> Transcript | None:
        self.calls += 1
        answer = self.answers.pop(0) if self.answers else "more words"
        if isinstance(answer, Exception):
            raise answer
        return Transcript(answer, True) if answer is not None else None


async def settle() -> None:
    """Let the transcription worker run."""
    for _ in range(10):
        await asyncio.sleep(0)


def build(path: Path, stt: FakeStt) -> tuple[
    DurableStore, VoiceNoteStore, FakeFrameSource, VoiceNoteCaptureController,
]:
    durable = DurableStore(path)
    notes = VoiceNoteStore(durable)
    source = FakeFrameSource()
    capture = VoiceNoteCaptureController(
        notes, AudioRouter(source), stt, id_factory=lambda: "capture-1",
    )
    return durable, notes, source, capture


async def test_a_sentence_reaches_the_live_page_while_recording(tmp_path: Path) -> None:
    stt = ScriptedStt(["We ship on Friday."])
    durable, notes, source, capture = build(tmp_path / "joat.sqlite3", stt)
    changes: list[int] = []
    capture.add_listener(lambda: changes.append(1))
    with durable:
        capture.control_start()
        seq = 0
        for _ in range(4):
            source.emit(seq, pcm=SPEECH_200MS, speaker="self")
            seq += 1
        # One second of quiet closes the utterance: the chunk is cut and
        # transcribed while the note is still recording.
        for _ in range(6):
            source.emit(seq, pcm=QUIET_200MS, speaker=None)
            seq += 1
        await settle()

        assert capture.snapshot.state == "recording"
        assert capture.live_lines() == (LiveLine("We ship on Friday."),)
        assert stt.calls == 1
        assert changes, "the live page is told about every change"

        receipt = await capture.control_stop()

        assert receipt.startswith("Saved Voice Note")
        saved = notes.get("capture-1")
        assert saved.state == "saved" and saved.raw_retained is False
        # The trailing quiet was accounted for without another STT call.
        assert stt.calls == 1


async def test_near_silence_is_accounted_for_without_stt(tmp_path: Path) -> None:
    stt = ScriptedStt([])
    durable, notes, source, capture = build(tmp_path / "joat.sqlite3", stt)
    with durable:
        capture.control_start()
        for seq in range(3):
            source.emit(seq, pcm=SILENCE)

        receipt = await capture.control_stop()

        assert stt.calls == 0
        assert receipt.startswith("Saved Voice Note: ")
        assert notes.get("capture-1").state == "saved"


async def test_no_words_is_complete_but_an_engine_failure_keeps_the_audio(
    tmp_path: Path,
) -> None:
    stt = ScriptedStt([None])
    durable, notes, source, capture = build(tmp_path / "joat.sqlite3", stt)
    with durable:
        capture.control_start()
        source.emit(0)
        await capture.control_stop()
        assert notes.get("capture-1").state == "saved"

    failing = ScriptedStt([SttUnavailableError("down"), SttUnavailableError("still down")])
    durable2, notes2, source2, capture2 = build(tmp_path / "joat-2.sqlite3", failing)
    with durable2:
        capture2.control_start()
        source2.emit(0)
        receipt = await capture2.control_stop()
        assert receipt.startswith("Saved Voice Note with gaps")
        degraded = notes2.get("capture-1")
        assert degraded.state == "saved_degraded" and degraded.raw_retained is True
        assert any(
            s.kind == "gap" and s.text.startswith("[transcription_gap]")
            for s in notes2.segments("capture-1")
        )


async def test_a_chunk_the_engine_missed_live_is_retried_at_save(tmp_path: Path) -> None:
    stt = ScriptedStt([SttUnavailableError("warming"), "Second try worked."])
    durable, notes, source, capture = build(tmp_path / "joat.sqlite3", stt)
    with durable:
        capture.control_start()
        source.emit(0)
        capture.control_pause()
        await settle()
        assert capture.live_lines() == ()

        await capture.control_stop()

        assert [s.text for s in notes.segments("capture-1") if s.kind == "transcript"] == [
            "Second try worked."
        ]
        assert notes.get("capture-1").state == "saved"


async def test_hold_to_talk_makes_one_paragraph_per_hold(tmp_path: Path) -> None:
    stt = ScriptedStt(["Buy milk.", "Call the plumber."])
    durable, notes, source, capture = build(tmp_path / "joat.sqlite3", stt)
    with durable:
        opened = capture.control_start("hold")
        assert opened.startswith("Opened QuickNote: Note ")
        assert capture.snapshot.state == "paused" and capture.snapshot.mode == "hold"
        # Paused: nothing is journaled.
        source.emit(0)
        assert notes.frame_count("capture-1") == 0

        assert capture.control_hold_start() is not None
        assert capture.snapshot.holding is True
        source.emit(10)
        source.emit(11)
        capture.control_hold_end()
        await settle()
        assert capture.snapshot.state == "paused"

        capture.control_hold_start()
        source.emit(20)
        capture.control_hold_end()
        await settle()

        assert [line.text for line in capture.live_lines()] == ["Buy milk.", "Call the plumber."]
        receipt = await capture.control_stop()
        assert receipt.startswith("Saved Voice Note")
        assert notes.get("capture-1").transcribed_segments == 2


async def test_a_hold_keeps_the_words_spoken_before_it_registered(tmp_path: Path) -> None:
    stt = ScriptedStt(["Remember the keys."])
    durable, notes, source, capture = build(tmp_path / "joat.sqlite3", stt)
    now = time.time_ns() // 1_000
    with durable:
        capture.control_start("hold")
        source.emit(0, received_at_us=now - 900_000)   # within the pre-roll
        source.emit(1, received_at_us=now - 700_000)
        capture.control_hold_start()
        source.emit(2, received_at_us=now)
        capture.control_hold_end()
        await capture.control_stop()

        frames_seen = notes.get("capture-1").journaled_frames
        assert frames_seen == 3


async def test_a_hold_never_journals_the_same_frame_twice(tmp_path: Path) -> None:
    """Pause, then hold at once: the pre-roll reaches back over audio the
    continuous stretch already journaled. No duplicate, no false gap."""
    stt = ScriptedStt(["First.", "Second."])
    durable, notes, source, capture = build(tmp_path / "joat.sqlite3", stt)
    now = time.time_ns() // 1_000
    with durable:
        capture.control_start()
        source.emit(0, received_at_us=now - 400_000)
        source.emit(1, received_at_us=now - 300_000)
        capture.control_pause()
        source.emit(2, received_at_us=now - 100_000)
        capture.control_hold_start()
        capture.control_hold_end()
        await capture.control_stop()

        note = notes.get("capture-1")
        assert note.journaled_frames == 3
        assert note.gap_count == 0


async def test_a_quick_note_nobody_held_is_not_kept(tmp_path: Path) -> None:
    saved: list[str] = []
    durable = DurableStore(tmp_path / "joat.sqlite3")
    notes = VoiceNoteStore(durable)
    capture = VoiceNoteCaptureController(
        notes, AudioRouter(FakeFrameSource()), FakeStt(),
        id_factory=lambda: "capture-1", on_saved=saved.append,
    )
    with durable:
        capture.control_start("hold")

        receipt = await capture.control_stop()

        assert receipt == "Nothing was recorded, so the note was discarded."
        assert notes.get("capture-1").state == "discarded"
        assert saved == []
        assert capture.active is False


async def test_tap_toggles_recording_and_a_hold_is_ignored_while_recording(
    tmp_path: Path,
) -> None:
    durable, _notes, source, capture = build(tmp_path / "joat.sqlite3", FakeStt())
    with durable:
        capture.control_start()
        assert capture.control_hold_start() is None
        assert capture.control_toggle().startswith("Paused Voice Note")
        assert capture.control_toggle().startswith("Resumed Voice Note")
        source.emit(0)
        assert capture.snapshot.state == "recording"


async def test_keep_after_discard_resumes_only_a_recording(tmp_path: Path) -> None:
    durable, _notes, _source, capture = build(tmp_path / "joat.sqlite3", FakeStt())
    with durable:
        capture.control_start("hold")
        capture.control_request_discard()

        assert capture.control_keep() == f"Kept Voice Note: {capture.snapshot.title}."
        assert capture.snapshot.state == "paused"


def speak_and_pause(source: FakeFrameSource, seq: int) -> int:
    """One spoken sentence then a second of quiet: the recorder cuts it."""
    for _ in range(4):
        source.emit(seq, pcm=SPEECH_200MS)
        seq += 1
    for _ in range(6):
        source.emit(seq, pcm=QUIET_200MS, speaker=None)
        seq += 1
    return seq


async def test_undo_takes_back_the_last_hold_and_the_note_stays_complete(
    tmp_path: Path,
) -> None:
    stt = ScriptedStt(["Buy milk.", "Call the plumber."])
    durable, notes, source, capture = build(tmp_path / "joat.sqlite3", stt)
    with durable:
        capture.control_start("hold")
        capture.control_hold_start()
        source.emit(10)
        source.emit(11)
        capture.control_hold_end()
        capture.control_hold_start()
        source.emit(20)
        capture.control_hold_end()
        await settle()

        assert capture.control_undo() == "Removed the last paragraph."
        assert [line.text for line in capture.live_lines()] == ["Buy milk."]
        assert capture.control_undo() == "Removed the last paragraph."
        assert capture.live_lines() == ()
        assert capture.control_undo() == "Nothing to undo."

        await capture.control_stop()
        saved = notes.get("capture-1")
        # Undone audio is still accounted for: complete, raw purged.
        assert saved.state == "saved" and saved.raw_retained is False
        assert saved.transcribed_segments == 0


async def test_undo_reaches_words_still_being_transcribed(tmp_path: Path) -> None:
    stt = ScriptedStt(["Never shown."])
    durable, notes, source, capture = build(tmp_path / "joat.sqlite3", stt)
    with durable:
        capture.control_start("hold")
        capture.control_hold_start()
        source.emit(10)
        capture.control_hold_end()

        assert capture.control_undo() == "Removed the last paragraph."
        await settle()

        assert capture.live_lines() == ()
        assert stt.calls == 0


async def test_a_continuous_stretch_flows_as_one_paragraph(tmp_path: Path) -> None:
    """Conversate shows flowing words: the pause detector's cuts are not new
    lines. Pausing ends the paragraph; Undo takes back the newest one."""
    stt = ScriptedStt(["We ship on Friday.", "Then the review.", None, "After the pause."])
    durable, notes, source, capture = build(tmp_path / "joat.sqlite3", stt)
    with durable:
        capture.control_start()
        seq = speak_and_pause(source, 0)
        seq = speak_and_pause(source, seq)
        seq = speak_and_pause(source, seq)    # heard, but no words in it
        await settle()
        assert [line.text for line in capture.live_lines()] == [
            "We ship on Friday. Then the review.",
        ]

        capture.control_pause()
        capture.control_resume()
        speak_and_pause(source, seq + 5)
        await settle()
        assert [line.text for line in capture.live_lines()] == [
            "We ship on Friday. Then the review.", "After the pause.",
        ]

        assert capture.control_undo() == "Removed the last paragraph."
        assert [line.text for line in capture.live_lines()] == [
            "We ship on Friday. Then the review.",
        ]
        await capture.control_stop()
        saved = notes.get("capture-1")
        assert saved.state == "saved" and saved.transcribed_segments == 1


async def test_a_hold_that_opened_the_menu_keeps_nothing(tmp_path: Path) -> None:
    stt = ScriptedStt(["Buy milk.", "Never shown."])
    durable, _notes, source, capture = build(tmp_path / "joat.sqlite3", stt)
    with durable:
        capture.control_start("hold")
        capture.control_hold_start()
        source.emit(10)
        capture.control_hold_end()
        await settle()
        capture.control_hold_start()
        source.emit(20)

        assert capture.control_hold_cancel() is not None
        await settle()

        assert capture.snapshot.holding is False
        assert [line.text for line in capture.live_lines()] == ["Buy milk."]
        assert stt.calls == 1
        # The earlier paragraph is still the one Undo reaches.
        assert capture.control_undo() == "Removed the last paragraph."
        assert capture.live_lines() == ()


async def test_the_exit_prompt_discards_without_a_second_question(tmp_path: Path) -> None:
    durable, notes, source, capture = build(tmp_path / "joat.sqlite3", ScriptedStt(["Words."]))
    with durable:
        capture.control_start("hold")
        capture.control_hold_start()
        source.emit(10)
        capture.control_hold_end()

        assert capture.control_discard().startswith("Discarded Voice Note")

        assert capture.active is False
        assert notes.get("capture-1").state == "discarded"


async def test_silence_watch_reports_a_capture_with_no_audio(tmp_path: Path) -> None:
    clock = [100.0]
    durable = DurableStore(tmp_path / "joat.sqlite3")
    notes = VoiceNoteStore(durable)
    source = FakeFrameSource()
    capture = VoiceNoteCaptureController(
        notes, AudioRouter(source), FakeStt(), id_factory=lambda: "capture-1",
        monotonic=lambda: clock[0],
    )
    with durable:
        assert capture.silent_for_s() == 0.0
        capture.control_start()
        clock[0] = 103.5
        assert capture.silent_for_s() == 3.5
        source.emit(0)
        clock[0] = 104.0
        assert capture.silent_for_s() == 0.5
        capture.control_pause()
        assert capture.silent_for_s() == 0.0


async def test_the_rec_timer_counts_listening_time_not_audio_bytes(tmp_path: Path) -> None:
    """Frames arrive in bursts; a timer read from them jumps. The REC timer
    is wall time while listening, paused while paused."""
    clock = [100.0]
    durable = DurableStore(tmp_path / "joat.sqlite3")
    notes = VoiceNoteStore(durable)
    capture = VoiceNoteCaptureController(
        notes, AudioRouter(FakeFrameSource()), FakeStt(), id_factory=lambda: "capture-1",
        monotonic=lambda: clock[0],
    )
    with durable:
        capture.control_start()
        clock[0] = 103.4
        assert capture.snapshot.recorded_s == 3          # no frame arrived at all
        capture.control_pause()
        clock[0] = 150.0
        assert round(capture.elapsed_s(), 1) == 3.4      # paused: the timer stops
        capture.control_resume()
        clock[0] = 152.0
        assert capture.snapshot.recorded_s == 5
