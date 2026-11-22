"""Unit tests for streaming transcript sinks (TerminalSink and Broadcast)."""

from __future__ import annotations

import io

from joat.services.sinks import Broadcast, TerminalSink


class Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.events: list[tuple[str, object]] = []
        self._fail = fail

    async def open(self, topic: str, speakers: list[str]) -> None:
        self._record("open", topic)

    async def turn(self, label: str) -> None:
        self._record("turn", label)

    async def chunk(self, text: str) -> None:
        self._record("chunk", text)

    async def close(self, verdict: str) -> None:
        self._record("close", verdict)

    def _record(self, kind: str, value: object) -> None:
        if self._fail:
            raise RuntimeError("sink is down")
        self.events.append((kind, value))


class TestTerminalSink:
    async def test_streams_turns_and_chunks_as_they_arrive(self) -> None:
        buf = io.StringIO()
        sink = TerminalSink(out=buf, use_color=False)
        await sink.open("tabs or spaces", ["codex", "claude", "antigravity"])
        await sink.turn("CODEX")
        await sink.chunk("Tabs ")
        await sink.chunk("indent faster.")
        await sink.close("done")
        out = buf.getvalue()
        assert "tabs or spaces" in out
        assert "codex → claude → antigravity" in out
        assert "» CODEX" in out
        assert "Tabs indent faster." in out
        assert "done" in out

    async def test_writes_without_ansi_when_not_a_tty(self) -> None:
        buf = io.StringIO()
        await TerminalSink(out=buf, use_color=False).turn("CODEX")
        assert "\033[" not in buf.getvalue()


class TestBroadcast:
    async def test_every_sink_sees_every_event(self) -> None:
        a, b = Recorder(), Recorder()
        cast = Broadcast([a, b])
        await cast.open("t", ["codex"])
        await cast.turn("CODEX")
        await cast.chunk("hi")
        await cast.close("done")
        assert [k for k, _ in a.events] == ["open", "turn", "chunk", "close"]
        assert a.events == b.events

    async def test_a_dead_sink_never_silences_the_others(self) -> None:
        dead, alive = Recorder(fail=True), Recorder()
        cast = Broadcast([dead, alive])
        await cast.open("t", ["codex"])
        await cast.chunk("still streaming")
        assert ("chunk", "still streaming") in alive.events
