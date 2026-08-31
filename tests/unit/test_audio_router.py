"""AudioRouter journals capture frames before conversational processing."""

from __future__ import annotations

import pytest

from joat.audio.router import AudioRouteConflictError, AudioRouter
from joat.core.interfaces import AudioFrame, AudioFrameCallback, AudioFrameSource


class FakeFrameSource(AudioFrameSource):
    def __init__(self) -> None:
        self.callback: AudioFrameCallback | None = None

    def on_audio_frame(self, callback: AudioFrameCallback) -> None:
        self.callback = callback

    def emit(self, frame: AudioFrame) -> None:
        assert self.callback is not None
        self.callback(frame)


def frame(seq: int = 1) -> AudioFrame:
    return AudioFrame(
        pcm=b"\x01\x00",
        connection_generation="socket-a",
        seq=seq,
        source="glasses",
        speaker="wearer",
        received_at_us=10,
    )


def test_exclusive_capture_prevents_conversational_processing() -> None:
    source = FakeFrameSource()
    router = AudioRouter(source)
    order: list[str] = []

    def capture(_frame: AudioFrame) -> None:
        order.append("capture")

    router.begin_capture(capture)
    router.on_conversation(lambda _pcm, _seq: order.append("conversation"))

    source.emit(frame())

    assert order == ["capture"]


def test_nonexclusive_capture_still_runs_before_conversation() -> None:
    source = FakeFrameSource()
    router = AudioRouter(source)
    order: list[str] = []

    router.begin_capture(
        lambda _frame: order.append("capture"),
        exclusive=False,
    )
    router.on_conversation(lambda _pcm, _seq: order.append("conversation"))

    source.emit(frame())

    assert order == ["capture", "conversation"]


def test_capture_has_one_explicit_owner() -> None:
    router = AudioRouter(FakeFrameSource())

    def first(_frame: AudioFrame) -> None:
        pass

    def second(_frame: AudioFrame) -> None:
        pass

    router.begin_capture(first)
    with pytest.raises(AudioRouteConflictError, match="already active"):
        router.begin_capture(second)
    with pytest.raises(AudioRouteConflictError, match="does not match"):
        router.end_capture(second)

    router.end_capture(first)
    assert router.capturing is False


def test_router_is_a_frame_source_for_conversation_consumers() -> None:
    """The orchestrator subscribes to the router exactly like a transport."""
    source = FakeFrameSource()
    router = AudioRouter(source)
    frames: list[AudioFrame] = []
    router.on_audio_frame(frames.append)

    source.emit(frame(1))
    assert [f.seq for f in frames] == [1]

    # An exclusive capture owns the microphone: conversation frame consumers
    # hear nothing until the capture lane closes.
    def capture(_frame: AudioFrame) -> None:
        pass

    router.begin_capture(capture)
    source.emit(frame(2))
    assert [f.seq for f in frames] == [1]

    router.end_capture(capture)
    source.emit(frame(3))
    assert [f.seq for f in frames] == [1, 3]
