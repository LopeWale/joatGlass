"""RMS-gated utterance segmentation for the Phase-0 voice loop.

The transport delivers ~200 ms PCM batches. This turns that stream into
discrete utterances: speech starts when batch RMS crosses the threshold,
and ends after a hangover of consecutive quiet batches (so natural pauses
between words don't split an utterance).

Deliberately crude (RULES.md phase discipline): energy gating is enough to
prove the voice → HUD loop. Swap for a real VAD (Silero, WebRTC) behind the
same feed() shape when false triggers start to matter.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2  # signed 16-bit LE


def rms(pcm: bytes) -> float:
    """Root-mean-square amplitude, 0.0-1.0."""
    if not pcm:
        return 0.0
    count = len(pcm) // SAMPLE_WIDTH
    if count == 0:
        return 0.0
    samples = struct.unpack(f"<{count}h", pcm[: count * SAMPLE_WIDTH])
    return float((sum(s * s for s in samples) / count) ** 0.5) / 32768.0


@dataclass
class RmsVad:
    """Feed PCM batches in; get a completed utterance back when one ends.

    speech_rms: batch RMS at/above this counts as speech. The right value
    depends on mic gain — calibrate against a real quiet room; 0.015 is a
    starting point, not a tuned number.
    hangover_batches: consecutive sub-threshold batches that close an
    utterance (4 x 200 ms = 0.8 s of silence).
    max_utterance_s: hard cut so a noisy room cannot buffer unboundedly.
    """

    speech_rms: float = 0.015
    hangover_batches: int = 4
    max_utterance_s: float = 30.0

    _buffer: bytearray = field(default_factory=bytearray, repr=False)
    _in_speech: bool = False
    _quiet_run: int = 0

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    def feed(self, pcm: bytes) -> bytes | None:
        """Returns the finished utterance's PCM when this batch closes one."""
        loud = rms(pcm) >= self.speech_rms

        if not self._in_speech:
            if not loud:
                return None
            self._in_speech = True
            self._quiet_run = 0
            self._buffer = bytearray(pcm)
            return None

        self._buffer.extend(pcm)
        max_bytes = int(self.max_utterance_s * SAMPLE_RATE * SAMPLE_WIDTH)
        if loud:
            self._quiet_run = 0
            if len(self._buffer) >= max_bytes:
                return self._finish()
            return None

        self._quiet_run += 1
        if self._quiet_run >= self.hangover_batches:
            return self._finish()
        return None

    def reset(self) -> None:
        """Drop any partial utterance — call on an audio seq gap, so a
        dropped-frame discontinuity never splices into one bogus utterance."""
        self._buffer = bytearray()
        self._in_speech = False
        self._quiet_run = 0

    def _finish(self) -> bytes:
        utterance = bytes(self._buffer)
        self.reset()
        return utterance
