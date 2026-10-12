"""Journal-first fan-out for conversational and durable audio consumers."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable

from ..core.interfaces import (
    AudioCallback,
    AudioFrame,
    AudioFrameCallback,
    AudioFrameSource,
)


class AudioRouteConflictError(RuntimeError):
    """A second capture tried to take ownership of the microphone stream."""


class AudioRouter(AudioFrameSource):
    """Give one capture owner first access while preserving conversation.

    Capture is deliberately single-owner: two recorders would make consent,
    retention, and gap accounting ambiguous. Conversational subscribers still
    receive the same frame afterwards, but their busy/drop policy cannot affect
    the already-journaled capture lane.

    The router is itself an ``AudioFrameSource``: conversation consumers (the
    voice orchestrator) subscribe to it exactly as they would to the raw
    transport, and an exclusive capture silences them for the recording's
    duration — a live meeting recording must not also drive assistant replies.
    """

    #: How much recent audio the router remembers for a capture that asks
    #: for pre-roll. A held-touchpad note (FEAT-002) begins only once the
    #: press registers as long, after the wearer has already started talking.
    RECENT_AUDIO_US = 2_000_000

    def __init__(
        self, source: AudioFrameSource, *,
        on_capture_started: Callable[[], None] | None = None,
    ) -> None:
        self._capture: AudioFrameCallback | None = None
        self._capture_exclusive = False
        self._on_capture_started = on_capture_started
        self._conversation: list[AudioCallback] = []
        self._conversation_frames: list[AudioFrameCallback] = []
        self._recent: deque[AudioFrame] = deque()
        source.on_audio_frame(self._route)

    @property
    def capturing(self) -> bool:
        return self._capture is not None

    def on_conversation(self, callback: AudioCallback) -> None:
        self._conversation.append(callback)

    def on_audio_frame(self, callback: AudioFrameCallback) -> None:
        """Subscribe a conversation consumer that needs full frame provenance."""
        self._conversation_frames.append(callback)

    def begin_capture(
        self,
        callback: AudioFrameCallback,
        *,
        exclusive: bool = True,
        preroll_us: int = 0,
        now_us: int | None = None,
    ) -> None:
        """Take the capture lane.

        ``preroll_us`` > 0 first hands the new owner the most recent frames
        of the CURRENT connection that arrived within that window before
        ``now_us`` (or before the newest frame when no clock is given),
        oldest first — audio the conversation consumers already saw, now
        also journaled by the capture.
        """
        if self._capture is not None:
            raise AudioRouteConflictError("an audio capture is already active")
        if isinstance(preroll_us, bool) or not isinstance(preroll_us, int) or preroll_us < 0:
            raise ValueError("preroll_us must be a non-negative integer")
        self._capture = callback
        self._capture_exclusive = exclusive
        try:
            # Recording changes the wearer's input context even if it begins
            # and ends before another conversation frame or state read.
            # Retire old questions synchronously; completed STT keeps running.
            if self._on_capture_started is not None:
                self._on_capture_started()
        except Exception:
            # A failed acquisition must not leave an unreported recorder.
            self._capture = None
            self._capture_exclusive = False
            raise
        if preroll_us:
            for frame in self._preroll(preroll_us, now_us):
                callback(frame)

    def end_capture(self, callback: AudioFrameCallback) -> None:
        if self._capture is not callback:
            raise AudioRouteConflictError("capture owner does not match")
        self._capture = None
        self._capture_exclusive = False

    def _preroll(self, window_us: int, now_us: int | None) -> list[AudioFrame]:
        """Recent frames of the newest connection, within the window, in order.

        A pre-roll must never splice two connections: frames from an older
        generation, or older than a sequence gap, are left out.
        """
        if not self._recent:
            return []
        newest = self._recent[-1]
        reference = now_us if now_us is not None else newest.received_at_us
        picked: list[AudioFrame] = []
        for frame in reversed(self._recent):
            if (
                frame.connection_generation != newest.connection_generation
                or reference - frame.received_at_us > window_us
            ):
                break
            if picked and frame.seq + 1 != picked[-1].seq:
                break
            picked.append(frame)
        picked.reverse()
        return picked

    def _remember(self, frame: AudioFrame) -> None:
        recent = self._recent
        recent.append(frame)
        while recent and frame.received_at_us - recent[0].received_at_us > self.RECENT_AUDIO_US:
            recent.popleft()
        while len(recent) > 256:
            recent.popleft()

    def _route(self, frame: AudioFrame) -> None:
        self._remember(frame)
        capture = self._capture
        if capture is not None:
            capture(frame)
            if self._capture_exclusive:
                return
        for frame_callback in tuple(self._conversation_frames):
            frame_callback(frame)
        for callback in tuple(self._conversation):
            callback(frame.pcm, frame.seq)


__all__ = ["AudioRouteConflictError", "AudioRouter"]
