"""WhisperKit STT adapter.

WhisperKit ships as a Swift CLI, so this shells out rather than binding a
library. Buffering happens here: Whisper wants whole utterances, but the
transport hands us 200 ms batches, so we accumulate until an utterance
boundary and only then transcribe.

Chosen for Phase 0 because it runs on the Neural Engine and covers 99
languages. ParakeetMlxEngine is the low-WER alternative behind the same
interface — swapping is a config change, not a code change.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import struct
import tempfile
import wave
from pathlib import Path

from ..core.interfaces import SttEngine, Transcript

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2  # signed 16-bit
CHANNELS = 1


class WhisperKitEngine(SttEngine):
    """Buffer PCM, transcribe on flush.

    No partial transcripts yet: the CLI is batch-only, so feed() always
    returns None and the real result comes from flush(). A streaming engine
    can return partials from feed() without any caller changing.
    """

    def __init__(
        self,
        binary: str = "whisperkit-cli",
        model: str = "large-v3_turbo",
        max_buffer_s: float = 30.0,
    ) -> None:
        self._binary = binary
        self._model = model
        self._max_bytes = int(max_buffer_s * SAMPLE_RATE * SAMPLE_WIDTH)
        self._buffer = bytearray()

    @property
    def available(self) -> bool:
        return shutil.which(self._binary) is not None

    async def feed(self, pcm: bytes) -> Transcript | None:
        self._buffer.extend(pcm)
        if len(self._buffer) > self._max_bytes:
            # Runaway buffer means VAD never fired. Drop the oldest audio
            # rather than grow without bound — the recent words are the ones
            # the wearer expects a reply to.
            overflow = len(self._buffer) - self._max_bytes
            del self._buffer[:overflow]
            log.warning("stt buffer full, dropped %d bytes of old audio", overflow)
        return None

    async def flush(self) -> Transcript | None:
        if not self._buffer:
            return None
        pcm = bytes(self._buffer)
        self._buffer.clear()

        if len(pcm) < SAMPLE_RATE * SAMPLE_WIDTH // 4:  # < 250 ms
            return None  # too short to be speech; usually a VAD false trigger

        text = await self._transcribe(pcm)
        return Transcript(text=text, is_final=True) if text else None

    def reset(self) -> None:
        self._buffer.clear()

    async def _transcribe(self, pcm: bytes) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "utterance.wav"
            write_wav(path, pcm)
            proc = await asyncio.create_subprocess_exec(
                self._binary,
                "transcribe",
                "--audio-path",
                str(path),
                "--model",
                self._model,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            log.error("whisperkit failed (%s): %s", proc.returncode, stderr.decode()[:400])
            return ""
        return stdout.decode().strip()


def write_wav(path: Path, pcm: bytes) -> None:
    """16 kHz mono 16-bit LE WAV — the format the transport already gives us."""
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)


def rms(pcm: bytes) -> float:
    """Root-mean-square amplitude, 0.0–1.0. Crude VAD input for Phase 0."""
    if not pcm:
        return 0.0
    count = len(pcm) // SAMPLE_WIDTH
    samples = struct.unpack(f"<{count}h", pcm[: count * SAMPLE_WIDTH])
    return float((sum(s * s for s in samples) / count) ** 0.5) / 32768.0
