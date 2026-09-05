"""Journal-first fan-out for conversational and durable audio consumers."""

from __future__ import annotations

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

    def __init__(
        self, source: AudioFrameSource, *,
        on_capture_started: Callable[[], None] | None = None,
    ) -> None:
        self._capture: AudioFrameCallback | None = None
        self._capture_exclusive = False
        self._on_capture_started = on_capture_started
        self._conversation: list[AudioCallback] = []
        self._conversation_frames: list[AudioFrameCallback] = []
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
    ) -> None:
        if self._capture is not None:
            raise AudioRouteConflictError("an audio capture is already active")
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

    def end_capture(self, callback: AudioFrameCallback) -> None:
        if self._capture is not callback:
            raise AudioRouteConflictError("capture owner does not match")
        self._capture = None
        self._capture_exclusive = False

    def _route(self, frame: AudioFrame) -> None:
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
