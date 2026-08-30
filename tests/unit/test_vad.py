"""RmsVad segmentation tests with synthetic PCM."""

from __future__ import annotations

import struct

from joat.audio.vad import RmsVad, rms

BATCH_SAMPLES = 3200  # 200 ms at 16 kHz


def batch(amplitude: int, sample_count: int = BATCH_SAMPLES) -> bytes:
    """A square-ish PCM batch at the given peak amplitude and sample count."""
    samples = [amplitude if i % 2 == 0 else -amplitude for i in range(sample_count)]
    return struct.pack(f"<{sample_count}h", *samples)


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
        vad = RmsVad(hangover_batches=3, adaptive=False, start_batches=1)
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
        vad = RmsVad(hangover_batches=4, adaptive=False, start_batches=1)
        for _ in range(3):
            vad.feed(LOUD)
        for _ in range(3):  # pause, but < hangover
            assert vad.feed(QUIET) is None
        assert vad.feed(LOUD) is None  # same utterance continues
        assert vad.in_speech

    def test_one_noise_spike_during_closing_silence_does_not_hold_open(self) -> None:
        """One room-noise batch must not reset an otherwise complete pause."""
        vad = RmsVad(hangover_batches=5, adaptive=False, start_batches=1)
        assert vad.feed(LOUD) is None
        for _ in range(4):
            assert vad.feed(QUIET) is None
        assert vad.feed(LOUD) is None  # isolated chair/room spike

        utterance = vad.feed(QUIET)

        assert utterance is not None
        assert len(utterance) == 7 * len(LOUD)
        assert not vad.in_speech

    def test_short_callbacks_do_not_shorten_the_hangover(self) -> None:
        """Five 100 ms callbacks are only 500 ms of silence, not one second."""
        vad = RmsVad(hangover_batches=5, adaptive=False, start_batches=1)
        assert vad.feed(LOUD) is None

        for _ in range(9):
            assert vad.feed(batch(50, 1_600)) is None
        utterance = vad.feed(batch(50, 1_600))

        assert utterance is not None
        assert len(utterance) == 38_400  # 200 ms speech + 1 s silence

    def test_variable_chunks_close_after_one_second_of_quiet_pcm(self) -> None:
        """Chunk boundaries must not delay an otherwise complete hangover."""
        vad = RmsVad(hangover_batches=5, adaptive=False, start_batches=1)
        assert vad.feed(LOUD) is None
        for sample_count in (1_600, 5_600, 8_784):  # 999 ms total
            assert vad.feed(batch(50, sample_count)) is None

        utterance = vad.feed(batch(50, 16))

        assert utterance is not None
        assert len(utterance) == 38_400  # 200 ms speech + 1 s silence

    def test_max_length_forces_cut(self) -> None:
        vad = RmsVad(max_utterance_s=0.6, adaptive=False, start_batches=1)
        assert vad.feed(LOUD) is None
        assert vad.feed(LOUD) is None
        assert vad.feed(LOUD) is not None  # hard cut at the cap

    def test_quiet_chunk_is_trimmed_at_the_max_sample_duration(self) -> None:
        """A large closing chunk cannot overshoot the configured hard cap."""
        vad = RmsVad(max_utterance_s=0.6, adaptive=False, start_batches=1)
        assert vad.feed(LOUD) is None

        utterance = vad.feed(batch(50, 8_000))  # crosses 0.6 s by 100 ms

        assert utterance is not None
        assert len(utterance) == 19_200

    def test_trigger_chunk_is_trimmed_at_the_max_sample_duration(self) -> None:
        """The first loud chunk is capped even when it arms and ends at once."""
        vad = RmsVad(max_utterance_s=0.6, adaptive=False, start_batches=1)

        utterance = vad.feed(batch(8_000, 12_800))

        assert utterance is not None
        assert len(utterance) == 19_200

    def test_reset_drops_partial_utterance(self) -> None:
        vad = RmsVad(hangover_batches=2, adaptive=False, start_batches=1)
        vad.feed(LOUD)
        vad.reset()
        assert not vad.in_speech
        assert vad.feed(QUIET) is None
        assert vad.feed(QUIET) is None  # no stale speech to close


class TestDebounceAndAdaptiveFloor:
    def test_isolated_spike_does_not_start_an_utterance(self) -> None:
        """A keystroke or door bump is one loud batch, not speech."""
        vad = RmsVad(adaptive=False, start_batches=2, hangover_batches=2)
        assert vad.feed(LOUD) is None
        assert not vad.in_speech          # armed only after 2 consecutive
        assert vad.feed(QUIET) is None
        assert not vad.in_speech
        assert vad.feed(LOUD) is None
        assert vad.feed(LOUD) is None
        assert vad.in_speech              # two in a row: real speech

    def test_floor_tracks_room_tone_and_cannot_lock(self) -> None:
        """Percentile floor must follow the room UP as well as down — the
        earlier EWMA locked low and made room tone read as speech forever."""
        vad = RmsVad()
        quiet_room = batch(120)
        for _ in range(40):
            vad.feed(quiet_room)
        low_floor = vad.floor
        noisy_room = batch(900)
        for _ in range(120):            # window is 100, so this fully replaces
            vad.feed(noisy_room)
        assert vad.floor > low_floor * 3
        # and the noisy room itself must NOT count as speech
        assert rms(noisy_room) < vad.trigger
