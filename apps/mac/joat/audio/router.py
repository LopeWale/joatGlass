"""Journal-first fan-out for conversational and durable audio consumers."""

from __future__ import annotations

from ..core.interfaces import (
    AudioCallback,
    AudioFrame,
    AudioFrameCallback,
    AudioFrameSource,
)


class AudioRouteConflictError(RuntimeError):
    """A second capture tried to take ownership of the microphone stream."""


class AudioRouter:
    """Give one capture owner first access while preserving conversation.

    Capture is deliberately single-owner: two recorders would make consent,
    retention, and gap accounting ambiguous. Conversational subscribers still
    receive the same frame afterwards, but their busy/drop policy cannot affect
    the already-journaled capture lane.
    """

    def __init__(self, source: AudioFrameSource) -> None:
        self._capture: AudioFrameCallback | None = None
        self._capture_exclusive = False
        self._conversation: list[AudioCallback] = []
        source.on_audio_frame(self._route)

    @property
    def capturing(self) -> bool:
        return self._capture is not None

    def on_conversation(self, callback: AudioCallback) -> None:
        self._conversation.append(callback)

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
        for callback in tuple(self._conversation):
            callback(frame.pcm, frame.seq)


__all__ = ["AudioRouteConflictError", "AudioRouter"]
