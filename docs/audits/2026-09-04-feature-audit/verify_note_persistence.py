"""Synthetic Q1 persistence acceptance using the production Mac ownership path.

Run from any directory with the repository Python environment. Only temporary
SQLite files are written; stdout is a content-free JSON receipt suitable for
retention by the caller. AudioFrameSource, STT, and the outbound transport are
deterministic fixtures. AudioRouter, CapabilityRouter, HudWriter,
VoiceNoteCaptureController, DurableStore, and VoiceNoteStore are production.

The input journey is recording -> Discard row -> quicknotes-back -> Save row.
Audio must survive the discard prompt and Back. A healthy save checkpoints
the transcript and deliberately purges raw audio; an incomplete transcription
retains both its completed transcript sections and recoverable PCM. Each
outcome is read again after closing and reopening the SQLite store. A separate
Confirm discard journey checks the destructive path.

This proves synthetic Mac persistence, not recognition, microphone capture,
Hub rendering, a physical G2 gesture, power-loss durability, or installation.
There are no live microphone, network, model, or external application calls.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
from pathlib import Path
import sys
import tempfile
from typing import cast

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "apps/mac"))

from joat.audio.router import AudioRouter
from joat.channels.cards import CardLog
from joat.core.interfaces import (
    AudioFrame,
    AudioFrameCallback,
    AudioFrameSource,
    GlassesTransport,
    SttEngine,
    Transcript,
)
from joat.core.protocol import DownlinkMessage, HudList, InputEvent
from joat.persistence.store import DurableStore
from joat.persistence.voice_notes import VoiceNoteStore
from joat.services.capabilities import CapabilityRouter
from joat.services.hud import HudWriter
from joat.services.status import StatusPublisher
from joat.services.voice_note_capture import VoiceNoteCaptureController


PCM = b"\x13\x00" * 160  # Synthetic 16-bit mono samples, not recorded speech.
CAPTURE_ID = "synthetic-q1-capture"
TITLE = "Synthetic persistence fixture"
REQUEST_ID = "synthetic-q1-request"


class SyntheticFrames(AudioFrameSource):
    def __init__(self) -> None:
        self.callback: AudioFrameCallback | None = None

    def on_audio_frame(self, callback: AudioFrameCallback) -> None:
        self.callback = callback

    def emit(self, seq: int) -> None:
        assert self.callback is not None
        self.callback(AudioFrame(
            pcm=PCM,
            connection_generation="synthetic-q1-connection",
            seq=seq,
            source="glasses",
            speaker="wearer",
            received_at_us=2_000_000 + seq * 20_000,
        ))


class DeterministicStt(SttEngine):
    def __init__(self, *, fail_second_section: bool = False) -> None:
        self.fail_second_section = fail_second_section
        self.buffer = bytearray()
        self.batches: list[bytes] = []
        self.transcripts: list[str] = []

    async def feed(self, pcm: bytes) -> Transcript | None:
        self.buffer.extend(pcm)
        return None

    async def flush(self) -> Transcript | None:
        assert self.buffer
        self.batches.append(bytes(self.buffer))
        if self.fail_second_section and len(self.batches) == 2:
            raise RuntimeError("synthetic transcription failure")
        text = f"Synthetic transcript section {len(self.batches)}."
        self.transcripts.append(text)
        return Transcript(text, True)

    def reset(self) -> None:
        self.buffer.clear()


class RecordingSink:
    """Only capture outbound lists; no socket or hardware transport exists."""

    def __init__(self) -> None:
        self.lists: asyncio.Queue[HudList] = asyncio.Queue()

    async def send(self, message: DownlinkMessage) -> None:
        if isinstance(message, HudList):
            self.lists.put_nowait(message)

    async def next_list(self, title: str) -> HudList:
        async with asyncio.timeout(2):
            frame = await self.lists.get()
        assert frame.title == title, "unexpected capability surface"
        assert frame.request_id == REQUEST_ID, "response lost its request context"
        return frame


async def select_row(
    router: CapabilityRouter, frame: HudList, label: str,
) -> None:
    assert frame.selectable, "expected selectable production controls"
    router.on_input(InputEvent(
        container_name="capdata",
        selected_name="QUICKNOTES",
        selected_index=frame.items.index(label),
        request_id=REQUEST_ID,
    ))


async def exercise(path: Path, outcome: str) -> dict[str, object]:
    sink = RecordingSink()
    transport = cast(GlassesTransport, sink)
    frames = SyntheticFrames()
    audio = AudioRouter(frames)
    stt = DeterministicStt(fail_second_section=outcome == "degraded_keep")
    saved_ids: list[str] = []
    with DurableStore(path) as durable:
        notes = VoiceNoteStore(durable)
        capture = VoiceNoteCaptureController(
            notes,
            audio,
            stt,
            clock_us=itertools.count(1_000_000, 1_000).__next__,
            id_factory=lambda: CAPTURE_ID,
            export_directory=path.parent / "unused-exports",
            on_saved=saved_ids.append,
        )
        router = CapabilityRouter(
            hud=HudWriter(transport), transport=transport,
            status=StatusPublisher(path=path.parent / "unused-status.json"),
            messages=None, cards=CardLog(), voice_notes=notes,
            voice_capture=capture,
        )
        await capture.handle_command(f"start a quicknote called {TITLE}")
        assert capture.active and audio.capturing
        frames.emit(0)
        assert b"".join(frame.pcm for frame in notes.frames(CAPTURE_ID)) == PCM
        await router.open("QuickNotes", request_id=REQUEST_ID)
        recording = await sink.next_list("QUICKNOTES · RECORDING")

        await select_row(router, recording, "Discard recording")
        discard = await sink.next_list("QUICKNOTES · DISCARD?")
        assert capture.discard_pending and capture.snapshot.state == "paused"
        assert not audio.capturing
        frames.emit(1)  # Audio while the discard prompt owns a pause is not journaled.
        assert notes.get(CAPTURE_ID).title == TITLE
        assert b"".join(frame.pcm for frame in notes.frames(CAPTURE_ID)) == PCM

        if outcome == "confirmed_discard":
            await select_row(router, discard, "Confirm discard")
            await sink.next_list("QUICKNOTES · EMPTY")
            assert not stt.batches and not saved_ids
        else:
            router.on_input(InputEvent(
                container_name="capability-lifecycle",
                selected_name="quicknotes-back",
                request_id=REQUEST_ID,
            ))
            resumed = await sink.next_list("QUICKNOTES · RECORDING")
            assert capture.active and not capture.discard_pending and audio.capturing
            assert capture.snapshot.capture_id == CAPTURE_ID
            assert b"".join(frame.pcm for frame in notes.frames(CAPTURE_ID)) == PCM
            frames.emit(2)
            assert b"".join(frame.pcm for frame in notes.frames(CAPTURE_ID)) == PCM * 2
            assert notes.get(CAPTURE_ID).gap_count == 0
            await select_row(router, resumed, "Save and finish")
            await sink.next_list("QUICKNOTES")
            assert b"".join(stt.batches) == PCM * 2
            assert saved_ids == [CAPTURE_ID]

        assert not capture.active and not audio.capturing
        before_close = notes.get(CAPTURE_ID)
        segments_before_close = notes.segments(CAPTURE_ID)
        frames_before_close = notes.frames(CAPTURE_ID)

    # A fresh connection and store instance must recover the same persisted data.
    with DurableStore(path) as reopened:
        notes = VoiceNoteStore(reopened)
        note = notes.get(CAPTURE_ID)
        segments = notes.segments(CAPTURE_ID)
        retained_frames = notes.frames(CAPTURE_ID)
        assert note == before_close
        assert segments == segments_before_close
        assert retained_frames == frames_before_close
        transcripts = [segment for segment in segments if segment.kind == "transcript"]
        assert [segment.text for segment in transcripts] == stt.transcripts
        if outcome == "confirmed_discard":
            assert note.state == "discarded" and note.title is None
            assert not segments and not retained_frames and not note.raw_retained
            assert note.raw_bytes == 0 and notes.latest_retained() is None
        else:
            assert note.title == TITLE and notes.latest_retained() == note
            assert note.received_frames == note.journaled_frames == 2
            assert len(transcripts) == note.transcribed_segments
            if outcome == "healthy_keep":
                assert note.state == "saved" and note.gap_count == 0
                assert len(transcripts) == 2
                assert not retained_frames and not note.raw_retained and note.raw_bytes == 0
                assert note.completion_code == "transcript_checkpointed_raw_purged"
            else:
                assert note.state == "saved_degraded" and note.gap_count == 1
                assert len(transcripts) == 1
                assert b"".join(frame.pcm for frame in retained_frames) == PCM * 2
                assert note.raw_retained and note.raw_bytes == len(PCM) * 2
                assert note.completion_code == "incomplete_raw_retained"
        return {
            "case": outcome,
            "passed": True,
            "state_after_reopen": note.state,
            "pcm_preserved_at_discard_prompt": True,
            "pcm_preserved_after_back": True if outcome != "confirmed_discard" else None,
            "transcript_sections": len(transcripts),
            "raw_frames_after_reopen": len(retained_frames),
            "raw_bytes_after_reopen": note.raw_bytes,
            "completion_code": note.completion_code,
            "save_hook_calls": len(saved_ids),
            "store_reopen_equal": True,
        }


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="joat-q1-persistence-") as scratch:
        outcomes = []
        for case in ("healthy_keep", "degraded_keep", "confirmed_discard"):
            outcomes.append(await exercise(Path(scratch) / f"{case}.sqlite3", case))
    sources = (
        "apps/mac/joat/services/capabilities.py",
        "apps/mac/joat/services/voice_note_capture.py",
        "apps/mac/joat/persistence/voice_notes.py",
    )
    print(json.dumps({
        "acceptance": "Q1 production Mac note persistence",
        "evidence": "synthetic PCM and deterministic STT; no recognition or device proof",
        "passed": True,
        "temporary_files_removed": True,
        "source_sha256": {
            source: hashlib.sha256((REPO / source).read_bytes()).hexdigest()
            for source in sources
        },
        "cases": outcomes,
    }, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
