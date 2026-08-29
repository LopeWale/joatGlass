"""Mac-local Voice Note control remains explicit and loss-accounted."""

from __future__ import annotations

from pathlib import Path
import stat

from joat.audio.router import AudioRouter
from joat.core.interfaces import (
    AudioFrame,
    AudioFrameCallback,
    AudioFrameSource,
    SttEngine,
    Transcript,
)
from joat.persistence.store import DurableStore
from joat.persistence.voice_notes import VoiceNoteStore
from joat.services.voice_note_capture import VoiceNoteCaptureController


class FakeFrameSource(AudioFrameSource):
    def __init__(self) -> None:
        self.callback: AudioFrameCallback | None = None

    def on_audio_frame(self, callback: AudioFrameCallback) -> None:
        self.callback = callback

    def emit(self, seq: int, *, generation: str = "socket-a") -> None:
        assert self.callback is not None
        self.callback(AudioFrame(
            pcm=b"\x01\x00" * 40,
            connection_generation=generation,
            seq=seq,
            source="glasses",
            speaker="wearer",
            received_at_us=100 + seq,
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
