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


@pytest.mark.parametrize("exclusive", [True, False])
def test_capture_entry_retires_context_before_any_recorded_frame(exclusive: bool) -> None:
    source = FakeFrameSource()
    order: list[str] = []
    router = AudioRouter(source, on_capture_started=lambda: order.append("retire-prompt"))

    def capture(_frame: AudioFrame) -> None:
        order.append("capture")

    router.begin_capture(capture, exclusive=exclusive)
    with pytest.raises(AudioRouteConflictError):
        router.begin_capture(capture)
    source.emit(frame())
    router.end_capture(capture)
    assert order == ["retire-prompt", "capture"]


def test_failed_capture_entry_cannot_leave_an_unreported_recorder_active() -> None:
    def unavailable() -> None:
        raise RuntimeError("capture lifecycle failed")

    source = FakeFrameSource()
    router = AudioRouter(source, on_capture_started=unavailable)
    captured: list[AudioFrame] = []
    with pytest.raises(RuntimeError, match="capture lifecycle failed"):
        router.begin_capture(captured.append)
    source.emit(frame())
    assert not router.capturing and captured == []


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


def timed(seq: int, at_us: int, generation: str = "socket-a") -> AudioFrame:
    return AudioFrame(
        pcm=b"\x01\x00", connection_generation=generation, seq=seq,
        source="glasses", speaker="self", received_at_us=at_us,
    )


def test_preroll_hands_a_hold_the_recent_audio_first() -> None:
    """A held-touchpad note (FEAT-002) starts after the wearer began talking:
    the capture is handed the last moments of audio, oldest first."""
    source = FakeFrameSource()
    router = AudioRouter(source)
    for seq in range(10):
        source.emit(timed(seq, 1_000_000 + seq * 200_000))
    captured: list[int] = []

    router.begin_capture(lambda f: captured.append(f.seq), preroll_us=600_000, now_us=2_900_000)
    source.emit(timed(10, 3_000_000))

    # Frames received at or after 2.3 s (2.9 s − 0.6 s), then the live frame.
    assert captured == [7, 8, 9, 10]


def test_preroll_never_splices_connections_or_crosses_a_gap() -> None:
    source = FakeFrameSource()
    router = AudioRouter(source)
    source.emit(timed(0, 1_000_000, generation="socket-a"))
    source.emit(timed(0, 1_100_000, generation="socket-b"))
    source.emit(timed(1, 1_200_000, generation="socket-b"))
    source.emit(timed(5, 1_300_000, generation="socket-b"))
    source.emit(timed(6, 1_400_000, generation="socket-b"))
    captured: list[tuple[str, int]] = []

    router.begin_capture(
        lambda f: captured.append((f.connection_generation, f.seq)),
        preroll_us=2_000_000, now_us=1_400_000,
    )

    assert captured == [("socket-b", 5), ("socket-b", 6)]


def test_stale_audio_is_not_preroll() -> None:
    """Audio from before a long silence (a muted mic) is not "just now"."""
    source = FakeFrameSource()
    router = AudioRouter(source)
    source.emit(timed(0, 1_000_000))
    captured: list[AudioFrame] = []

    router.begin_capture(captured.append, preroll_us=1_200_000, now_us=60_000_000)

    assert captured == []


def test_no_preroll_unless_asked() -> None:
    source = FakeFrameSource()
    router = AudioRouter(source)
    source.emit(timed(0, 1_000_000))
    captured: list[AudioFrame] = []

    router.begin_capture(captured.append)

    assert captured == []
