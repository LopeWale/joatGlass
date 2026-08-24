"""RmsVad segmentation tests with synthetic PCM."""

from __future__ import annotations

import struct

from jarvis.audio.vad import RmsVad, rms

BATCH_SAMPLES = 3200  # 200 ms at 16 kHz


def batch(amplitude: int) -> bytes:
    """A 200 ms square-ish batch at the given peak amplitude."""
    samples = [amplitude if i % 2 == 0 else -amplitude for i in range(BATCH_SAMPLES)]
    return struct.pack(f"<{BATCH_SAMPLES}h", *samples)


LOUD = batch(8000)
QUIET = batch(50)


class TestRms:
    def test_silence_is_zero(self) -> None:
        assert rms(b"") == 0.0
        assert rms(batch(0)) == 0.0

    def test_loud_beats_threshold_and_quiet_does_not(self) -> None:
        vad = RmsVad()
        assert rms(LOUD) >= vad.speech_rms
        assert rms(QUIET) < vad.speech_rms


class TestSegmentation:
    def test_pure_silence_never_yields(self) -> None:
        vad = RmsVad()
        assert all(vad.feed(QUIET) is None for _ in range(20))

    def test_speech_then_hangover_silence_yields_one_utterance(self) -> None:
        vad = RmsVad(hangover_batches=3)
        for _ in range(5):
            assert vad.feed(LOUD) is None
        assert vad.feed(QUIET) is None
        assert vad.feed(QUIET) is None
        utterance = vad.feed(QUIET)  # third quiet batch closes it
        assert utterance is not None
        # 5 loud + 3 quiet batches captured
        assert len(utterance) == 8 * len(LOUD)
        assert not vad.in_speech

    def test_pause_shorter_than_hangover_does_not_split(self) -> None:
        vad = RmsVad(hangover_batches=4)
        for _ in range(3):
            vad.feed(LOUD)
        for _ in range(3):  # pause, but < hangover
            assert vad.feed(QUIET) is None
        assert vad.feed(LOUD) is None  # same utterance continues
        assert vad.in_speech

    def test_max_length_forces_cut(self) -> None:
        vad = RmsVad(max_utterance_s=0.6)  # 3 batches
        assert vad.feed(LOUD) is None
        assert vad.feed(LOUD) is None
        assert vad.feed(LOUD) is not None  # hard cut at the cap

    def test_reset_drops_partial_utterance(self) -> None:
        vad = RmsVad(hangover_batches=2)
        vad.feed(LOUD)
        vad.reset()
        assert not vad.in_speech
        assert vad.feed(QUIET) is None
        assert vad.feed(QUIET) is None  # no stale speech to close
