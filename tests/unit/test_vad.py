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


class TestPromptFence:
    def test_preroll_capacity_reports_the_actual_pcm_ring(self) -> None:
        vad = RmsVad(preroll_batches=1)
        # The current PCM deque remains three callbacks regardless of this
        # legacy configuration field. Provenance must follow actual capacity.
        assert vad.preroll_capacity == 3

    def test_idle_fence_removes_old_preroll_from_the_next_utterance(self) -> None:
        vad = RmsVad(adaptive=False, start_batches=2, hangover_batches=2)
        old_quiet = batch(70)
        first, second, closing = batch(9_000), batch(10_000), batch(25)
        vad.feed(old_quiet)
        vad.feed(old_quiet)

        vad.fence_prompt()

        assert vad.prompt_discarding is False
        assert vad.feed(first) is None
        assert vad.feed(second) is None
        assert vad.feed(closing) is None
        assert vad.feed(closing) == first + second + closing + closing

    def test_debounce_started_before_fence_discards_its_entire_tail(self) -> None:
        vad = RmsVad(adaptive=False, start_batches=2, hangover_batches=2)
        assert vad.feed(LOUD) is None
        assert vad.in_speech is False

        vad.fence_prompt()

        assert vad.prompt_discarding is True
        for _ in range(4):
            assert vad.feed(LOUD) is None
            assert vad.prompt_discarding is True
        assert vad.feed(QUIET) is None
        assert vad.prompt_discarding is True
        assert vad.feed(QUIET) is None
        assert vad.prompt_discarding is False
        fresh, closing = batch(12_000), batch(20)
        assert vad.feed(fresh) is None
        assert vad.feed(fresh) is None
        assert vad.feed(closing) is None
        assert vad.feed(closing) == fresh + fresh + closing + closing

    def test_active_speech_is_discarded_without_emitting_an_old_fragment(self) -> None:
        vad = RmsVad(adaptive=False, start_batches=1, hangover_batches=2)
        vad.feed(LOUD)
        vad.feed(LOUD)
        assert vad.in_speech is True

        vad.fence_prompt()

        assert vad.prompt_discarding is True
        assert vad.in_speech is False
        assert vad.feed(LOUD) is None
        assert vad.feed(QUIET) is None
        assert vad.feed(QUIET) is None
        assert vad.prompt_discarding is False

    def test_discard_requires_a_full_quiet_boundary_after_resumed_speech(self) -> None:
        vad = RmsVad(adaptive=False, start_batches=1, hangover_batches=2)
        vad.feed(LOUD)
        vad.fence_prompt()

        assert vad.feed(QUIET) is None
        assert vad.feed(LOUD) is None
        assert vad.feed(QUIET) is None
        assert vad.prompt_discarding is True
        assert vad.feed(QUIET) is None
        assert vad.prompt_discarding is False

    def test_discard_hangover_uses_sample_duration_not_callback_count(self) -> None:
        vad = RmsVad(adaptive=False, start_batches=1, hangover_batches=2)
        vad.feed(LOUD)
        vad.fence_prompt()
        assert vad.prompt_discarding is True

        for _ in range(3):
            assert vad.feed(batch(25, 1_600)) is None
            assert vad.prompt_discarding is True
        assert vad.feed(b"") is None
        assert vad.prompt_discarding is True
        assert vad.feed(batch(25, 1_600)) is None
        assert vad.prompt_discarding is False

    def test_maximum_duration_cut_is_not_a_quiet_boundary_for_new_prompt(self) -> None:
        vad = RmsVad(adaptive=False, start_batches=1, hangover_batches=2, max_utterance_s=0.6)
        assert vad.feed(LOUD) is None
        assert vad.feed(LOUD) is None
        completed = vad.feed(LOUD)
        assert completed == LOUD * 3
        assert vad.in_speech is False

        vad.fence_prompt()

        assert vad.prompt_discarding is True
        for _ in range(12):
            assert vad.feed(LOUD) is None
            assert vad.prompt_discarding is True
        assert vad.feed(QUIET) is None
        assert vad.feed(QUIET) is None
        assert vad.prompt_discarding is False
        assert completed == LOUD * 3  # earlier completed work stays intact

    def test_repeated_fences_do_not_turn_a_discarded_tail_into_a_new_start(self) -> None:
        vad = RmsVad(adaptive=False, start_batches=2, hangover_batches=2)
        vad.feed(LOUD)
        vad.fence_prompt()
        vad.feed(LOUD)

        vad.fence_prompt()

        assert vad.prompt_discarding is True
        assert vad.feed(LOUD) is None
        assert vad.feed(QUIET) is None
        assert vad.feed(QUIET) is None
        assert vad.prompt_discarding is False

    def test_actual_quiet_after_a_cap_allows_an_idle_fence(self) -> None:
        vad = RmsVad(adaptive=False, start_batches=1, hangover_batches=2, max_utterance_s=0.6)
        vad.feed(LOUD)
        vad.feed(LOUD)
        assert vad.feed(LOUD) is not None
        assert vad.feed(QUIET) is None
        assert vad.feed(QUIET) is None

        vad.fence_prompt()

        assert vad.prompt_discarding is False
        fresh, closing = batch(12_000), batch(20)
        assert vad.feed(fresh) is None
        assert vad.feed(closing) is None
        assert vad.feed(closing) == fresh + closing + closing

    def test_fence_and_reset_preserve_noise_floor_but_reset_clears_partial_discard(self) -> None:
        vad = RmsVad(start_batches=1, hangover_batches=2)
        for _ in range(40):
            vad.feed(batch(120))
        before = vad.floor
        vad.feed(LOUD)

        vad.fence_prompt()

        assert vad.prompt_discarding is True
        assert vad.floor == before
        vad.reset()
        assert vad.prompt_discarding is False
        assert vad.in_speech is False
        assert vad.floor == before


def test_finish_now_closes_only_an_utterance_in_progress() -> None:
    """A tap on the listening box ends speech without a quiet boundary."""
    vad = RmsVad(adaptive=False, start_batches=2, hangover_batches=2)
    loud = struct.pack("<" + "h" * 320, *([8000] * 320))
    assert vad.finish_now() is None  # nothing being said
    assert vad.feed(loud) is None
    assert vad.feed(loud) is None
    assert vad.in_speech
    taken = vad.finish_now()
    assert taken is not None and len(taken) >= 2 * len(loud)
    assert not vad.in_speech
    assert vad.finish_now() is None
    # The quiet-boundary bookkeeping survives, as after a maximum-duration cut:
    # a tap is never mistaken for silence by the next prompt's fence.
    vad.fence_prompt()
    assert vad.prompt_discarding


class TestRoomBeforeSpeech:
    """2026-10-12: after every brain start (7 of 7) the first utterance opened
    within a second of the mic at the bare 0.004 trigger — room tone, sent to
    Whisper, which once returned "JOAT." and woke JOAT."""

    ROOM = batch(300)    # G2 room tone sits around 0.01-0.02; this is ~0.009
    SPEECH = batch(4000)

    def test_without_the_guard_room_tone_opens_an_utterance_at_once(self) -> None:
        vad = RmsVad()
        assert rms(self.ROOM) > vad.speech_rms
        for _ in range(3):
            vad.feed(self.ROOM)
        assert vad.in_speech  # the old behaviour, still the QuickNote default

    def test_with_the_guard_room_tone_never_opens_one(self) -> None:
        vad = RmsVad(arm_after_floor=True)
        for _ in range(60):
            assert vad.feed(self.ROOM) is None
            assert not vad.in_speech

    def test_with_the_guard_speech_after_the_room_is_known_still_opens(self) -> None:
        vad = RmsVad(arm_after_floor=True)
        for _ in range(10):
            vad.feed(self.ROOM)
        for _ in range(3):
            vad.feed(self.SPEECH)
        assert vad.in_speech
