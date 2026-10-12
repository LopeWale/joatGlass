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
from collections import deque
from dataclasses import dataclass, field

SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2  # signed 16-bit LE
NOMINAL_BATCH_SAMPLES = SAMPLE_RATE // 5  # 200 ms, retained for API compatibility


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
    depends on mic gain, so the trigger is adaptive by default (see below).
    hangover_batches: 200 ms compatibility units of sub-threshold PCM that
    close an utterance. The actual limit is measured from sample duration and
    may span one isolated noisy chunk.
    max_utterance_s: hard cut so a noisy room cannot buffer unboundedly.
    """

    speech_rms: float = 0.004
    """Absolute minimum the trigger may fall to — guards against arming on
    pure digital silence. NOT the working threshold when adaptive=True."""

    hangover_batches: int = 5
    """5 x 200ms = 1.0s. 4 (0.8s) split sentences at a clause pause; 8 (1.6s)
    never closed them, because a noisy room produces a spike more often than
    once per 1.6s. 1.0s is the measured middle."""

    start_batches: int = 2
    """Consecutive loud batches required to ARM. A single spike (keystroke,
    door, chair) is not speech, and without this debounce one stray packet
    started an utterance that the hangover then kept alive."""

    preroll_batches: int = 3
    """Batches of audio kept BEFORE the trigger fires and prepended to the
    utterance. Without it the debounce silently ate the first 200ms+ of
    every utterance — which is exactly where the wake word is. Measured:
    'JOAT, are you there?' transcribed as 'Are you there?', and 'JOAT'
    as 'Jode'. Speech also ramps up, so its first syllable often sits just
    under the trigger; pre-roll recovers that too."""

    max_utterance_s: float = 30.0
    adaptive: bool = True
    """Track the room's noise floor and trigger relative to it. A fixed
    threshold assumes a mic gain we do not control: measured live, speech
    peaked at 0.019 against a 0.015 trigger, so only the loudest syllables
    registered and everything between them ended the utterance."""

    noise_margin: float = 1.8
    """Trigger at floor x margin. Measured live on the built-in mic at full
    gain (120 packets): p20 0.0111, median 0.0144, p80 0.0211, max 0.065.
    At 1.6x the trigger sat at 0.0177 and passed 30% of ALL packets, so the
    hangover never elapsed; 1.8x puts it at ~0.020, just above the median
    room level and below the speech band."""

    floor_window: int = 100
    """Batches of history (100 x 200ms = 20s) used to estimate the floor."""

    _buffer: bytearray = field(default_factory=bytearray, repr=False)
    _in_speech: bool = False
    _closing_quiet_samples: int = 0
    _closing_noise_chunks: int = 0
    _loud_run: int = 0
    _speech_since_quiet: bool = False
    _prompt_quiet_samples: int = 0
    _prompt_discarding: bool = False
    _preroll: deque[bytes] = field(
        default_factory=lambda: deque(maxlen=3), repr=False)
    _recent: deque[float] = field(
        default_factory=lambda: deque(maxlen=100), repr=False)
    """Rolling RMS history. The floor is its 20th percentile, which tracks
    room tone whether or not anyone is talking. An earlier EWMA that only
    sampled 'quiet' batches could LOCK: one too-low estimate made room tone
    read as speech forever, and utterances ran to the 30s cap."""

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    def speech_so_far(self) -> bytes | None:
        """The utterance still being spoken, for a partial transcript; None
        between utterances. A copy: the buffer keeps growing."""
        return bytes(self._buffer) if self._in_speech else None

    @property
    def preroll_capacity(self) -> int:
        """Actual callback capacity, for a matching ingress-provenance ring."""
        capacity = self._preroll.maxlen
        assert capacity is not None
        return capacity

    @property
    def prompt_discarding(self) -> bool:
        """Whether ingress is currently dropping pre-prompt speech."""
        return self._prompt_discarding

    def fence_prompt(self) -> None:
        """Drop old pre-roll and discard begun speech through a quiet boundary.

        A debounce or maximum-duration cut can leave speech underway even
        when ``in_speech`` is false. Neither may become a new prompt's input.
        Completed utterance bytes and the room-noise history are untouched.
        """
        discard = (
            self._prompt_discarding or self._speech_since_quiet
            or self._in_speech or self._loud_run > 0
        )
        self.reset()
        self._prompt_discarding = discard
        self._speech_since_quiet = discard

    @property
    def floor(self) -> float:
        """Room tone estimate: 20th percentile of recent levels. Speech is
        bursty and occupies the upper percentiles, so it cannot drag this up
        the way a mean would."""
        if len(self._recent) < 10:
            return 0.0
        ordered = sorted(self._recent)
        return ordered[len(ordered) // 5]

    @property
    def trigger(self) -> float:
        """The RMS a batch must reach to count as speech, right now."""
        floor = self.floor
        if not self.adaptive or floor <= 0.0:
            return self.speech_rms
        return max(self.speech_rms, floor * self.noise_margin)

    def feed(self, pcm: bytes) -> bytes | None:
        """Returns the finished utterance's PCM when this batch closes one."""
        sample_count = len(pcm) // SAMPLE_WIDTH
        level = rms(pcm)
        # History is fed unconditionally — a percentile floor needs to see
        # speech to know what is NOT speech.
        self._recent.append(level)
        loud = level >= self.trigger

        # Prompt ownership needs an actual quiet boundary, independent of
        # ordinary segmentation's hard cap and isolated-noise tolerance.
        if loud:
            self._speech_since_quiet = True
            self._prompt_quiet_samples = 0
        elif self._speech_since_quiet:
            self._prompt_quiet_samples += sample_count
            if self._prompt_quiet_samples >= self.hangover_batches * NOMINAL_BATCH_SAMPLES:
                self._speech_since_quiet = False
                self._prompt_quiet_samples = 0
        if self._prompt_discarding:
            self._prompt_discarding = self._speech_since_quiet
            # The closing quiet frame is discarded too, never pre-roll.
            return None

        if not self._in_speech:
            self._preroll.append(pcm)
            if not loud:
                self._loud_run = 0
                return None
            self._loud_run += 1
            if self._loud_run < self.start_batches:
                return None  # debounce: not enough consecutive speech yet
            self._in_speech = True
            self._closing_quiet_samples = 0
            self._closing_noise_chunks = 0
            # Seed with the pre-roll (which already includes this batch and
            # the debounced ones) so the utterance keeps its own beginning.
            self._buffer = bytearray(b"".join(self._preroll))
            self._preroll.clear()
            if self._cap_buffer():
                return self._finish()
            return None

        self._buffer.extend(pcm)
        if self._cap_buffer():
            return self._finish()
        if loud:
            if self._closing_quiet_samples > 0:
                if self._closing_noise_chunks == 0:
                    self._closing_noise_chunks = 1
                else:
                    self._closing_quiet_samples = 0
                    self._closing_noise_chunks = 0
            return None

        self._closing_quiet_samples += sample_count
        hangover_samples = self.hangover_batches * NOMINAL_BATCH_SAMPLES
        if self._closing_quiet_samples >= hangover_samples:
            return self._finish()
        return None

    def finish_now(self) -> bytes | None:
        """Close the utterance being spoken, without waiting for quiet.

        The wearer tapped the listening box: what was heard so far is the
        answer. None between utterances, so a tap before speech changes
        nothing. The prompt's quiet-boundary bookkeeping is kept, as for a
        maximum-duration cut, so the tap is never mistaken for silence.
        """
        if not self._in_speech:
            return None
        return self._finish()

    def reset(self) -> None:
        """Drop any partial utterance — call on an audio seq gap, so a
        dropped-frame discontinuity never splices into one bogus utterance."""
        self._buffer = bytearray()
        self._in_speech = False
        self._closing_quiet_samples = 0
        self._closing_noise_chunks = 0
        self._loud_run = 0
        self._preroll.clear()
        self._speech_since_quiet = False
        self._prompt_quiet_samples = 0
        self._prompt_discarding = False

    def _cap_buffer(self) -> bool:
        """Trim to the exact configured sample limit and report completion."""
        max_samples = max(0, int(self.max_utterance_s * SAMPLE_RATE))
        max_bytes = max_samples * SAMPLE_WIDTH
        if len(self._buffer) < max_bytes:
            return False
        del self._buffer[max_bytes:]
        return True

    def _finish(self) -> bytes:
        utterance = bytes(self._buffer)
        # Emitting a chunk is not proof of quiet: a cap can cut live speech,
        # and ordinary hangover permits one isolated noisy callback.
        speech_since_quiet = self._speech_since_quiet
        prompt_quiet_samples = self._prompt_quiet_samples
        self.reset()
        self._speech_since_quiet = speech_since_quiet
        self._prompt_quiet_samples = prompt_quiet_samples
        return utterance
