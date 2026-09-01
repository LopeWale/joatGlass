"""Where a debate is watched.

"I wanna see as they're debating ... because then I cannot prove that that
was debated." Two surfaces with opposite constraints, and neither may be
able to silence the other.
"""

from __future__ import annotations

import asyncio

import io

from joat.core.protocol import BrainstormHudView
from joat.services.hud import HudWriter
from joat.services.sinks import Broadcast, HudSink, TerminalSink


class HudTransport:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, message: object) -> None:
        self.sent.append(message)


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
        await sink.open("tabs or spaces", ["codex", "claude", "openclaw"])
        await sink.turn("CODEX (proposer)")
        await sink.chunk("Tabs ")
        await sink.chunk("indent faster.")
        await sink.close("converged 0.91")
        out = buf.getvalue()
        assert "tabs or spaces" in out
        assert "codex → claude → openclaw" in out  # the room is named up front
        assert "» CODEX (proposer)" in out
        assert "Tabs indent faster." in out
        assert "converged 0.91" in out

    async def test_writes_without_ansi_when_not_a_tty(self) -> None:
        buf = io.StringIO()
        await TerminalSink(out=buf, use_color=False).turn("CODEX (proposer)")
        assert "\033[" not in buf.getvalue()


class TestHudSink:
    async def test_paints_the_structured_brainstorm_view_not_streamed_overview_text(self) -> None:
        transport = HudTransport()
        sink = HudSink(
            HudWriter(transport),  # type: ignore[arg-type]
            lambda: {
                "state": "ready",
                "reason": "",
                "endpoint_host": "admins-mac-mini.tail3a3363.ts.net",
                "no_tools_checked": True,
            },
        )
        await sink.open("tabs or spaces", ["codex (proposer)", "openclaw (agent terminal)"])
        await sink.turn("openclaw (agent terminal)")
        await sink.chunk("Use tabs with a formatter.")
        frame = transport.sent[-1]
        assert isinstance(frame, BrainstormHudView)
        assert frame.screen == "brainstorm"
        assert frame.state == "running"
        assert frame.active_speaker == "openclaw"
        assert frame.terminal_state == "ready"
        assert frame.no_tools_checked is True
        assert frame.activity == "OPENCLAW > Thinking… Use tabs with a formatter."

    async def test_keeps_the_thinking_marker_readable_without_changing_later_chunk_joining(self) -> None:
        transport = HudTransport()
        sink = HudSink(HudWriter(transport))  # type: ignore[arg-type]
        await sink.open("topic", ["hermes"])
        await sink.turn("hermes")

        await sink.chunk("first")
        first = transport.sent[-1]
        assert isinstance(first, BrainstormHudView)
        assert first.activity == "HERMES > Thinking… first"

        await sink.chunk("-chunk")
        second = transport.sent[-1]
        assert isinstance(second, BrainstormHudView)
        assert second.activity == "HERMES > Thinking… first-chunk"

        await sink.turn("hermes")
        await sink.chunk(" already spaced")
        naturally_spaced = transport.sent[-1]
        assert isinstance(naturally_spaced, BrainstormHudView)
        assert naturally_spaced.activity == "HERMES > Thinking… first-chunk\nHERMES > Thinking… already spaced"

    async def test_retains_recent_speaker_rows_for_the_wearable_terminal_tail(self) -> None:
        transport = HudTransport()
        sink = HudSink(HudWriter(transport))  # type: ignore[arg-type]
        await sink.open("topic", ["codex", "claude", "hermes"])
        await sink.turn("codex (proposer)")
        await sink.chunk("Keep the decision reversible.")
        await sink.turn("claude (critic)")
        await sink.chunk("Check the calendar first.")
        await sink.turn("hermes (agent terminal)")
        await sink.chunk("I can stage a safe next action.")

        frame = transport.sent[-1]
        assert isinstance(frame, BrainstormHudView)
        assert frame.activity == (
            "CODEX > Thinking… Keep the decision reversible.\n"
            "CLAUDE > Thinking… Check the calendar first.\n"
            "HERMES > Thinking… I can stage a safe next action."
        )

    async def test_terminal_overlay_and_replay_keep_the_real_brainstorm_state(self) -> None:
        transport = HudTransport()
        sink = HudSink(
            HudWriter(transport),  # type: ignore[arg-type]
            lambda: {
                "state": "ready",
                "reason": "",
                "endpoint_host": "admins-mac-mini.tail3a3363.ts.net",
                "no_tools_checked": True,
            },
        )
        await sink.open("reconnect safely", ["codex", "claude", "hermes"])
        await sink.turn("hermes (agent terminal)")
        await sink.show_terminal()

        terminal = transport.sent[-1]
        assert isinstance(terminal, BrainstormHudView)
        assert terminal.screen == "agent_terminal"
        assert terminal.state == "running"
        assert terminal.activity == "HERMES > Thinking…"

        await sink.replay()
        restored = transport.sent[-1]
        assert isinstance(restored, BrainstormHudView)
        assert restored.screen == "brainstorm"
        assert restored.state == "running"
        assert restored.active_speaker == "hermes"

    async def test_cancellation_and_blocked_failure_have_distinct_hud_states(self) -> None:
        transport = HudTransport()
        sink = HudSink(HudWriter(transport))  # type: ignore[arg-type]
        await sink.open("t", ["codex", "claude"])
        await sink.close("cancelled")
        cancelled = transport.sent[-1]
        assert isinstance(cancelled, BrainstormHudView)
        assert cancelled.state == "cancelled"

        await sink.close("blocked · collaborator disconnected")
        blocked = transport.sent[-1]
        assert isinstance(blocked, BrainstormHudView)
        assert blocked.state == "blocked"


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
        # A HUD that has gone away must not stop the terminal transcript —
        # the debate is the product, a surface is not.
        dead, alive = Recorder(fail=True), Recorder()
        cast = Broadcast([dead, alive])
        await cast.open("t", ["codex"])
        await cast.chunk("still streaming")
        assert ("chunk", "still streaming") in alive.events


class TestDebateStateSink:
    """The dashboard makes Debate a menu panel the wearer opens later, so
    someone must hold the debate as STATE — a token stream that has already
    painted cannot answer "what did they decide?" ten minutes on."""

    async def test_keeps_finished_turns_as_rows(self) -> None:
        from joat.services.sinks import DebateStateSink

        sink = DebateStateSink()
        await sink.open("tabs or spaces", ["codex", "claude", "openclaw"])
        await sink.turn("CODEX (proposer)")
        await sink.chunk("Tabs ")
        await sink.chunk("indent faster.")
        await sink.turn("CLAUDE (critic)")
        await sink.chunk("Only with consistent width.")
        await sink.close("converged 0.91")

        snap = sink.snapshot
        assert snap.topic == "tabs or spaces"
        assert snap.speakers == ("codex", "claude", "openclaw")
        assert snap.turns == (
            ("CODEX (proposer)", "Tabs indent faster."),
            ("CLAUDE (critic)", "Only with consistent width."),
        )
        assert snap.running is False and snap.verdict == "converged 0.91"
        rows = snap.rows()
        assert rows[0].startswith("CODEX: Tabs indent faster.")
        assert rows[-1] == "— converged 0.91"

    async def test_a_live_debate_shows_who_is_speaking_now(self) -> None:
        from joat.services.sinks import DebateStateSink

        sink = DebateStateSink()
        await sink.open("t", ["codex", "claude"])
        await sink.turn("CODEX (proposer)")
        await sink.chunk("thinking out loud")
        await sink.turn("CLAUDE (critic)")
        rows = sink.snapshot.rows()
        assert rows == ["CODEX: thinking out loud", "CLAUDE: …"]

    async def test_rows_fit_the_list_row_budget(self) -> None:
        # RULES: list rows are a fixed 40px and the font is not monospaced,
        # so a row that overflows is a row that silently truncates on glass.
        from joat.services.sinks import DebateStateSink

        sink = DebateStateSink()
        await sink.open("t", ["codex"])
        await sink.turn("CODEX (proposer)")
        await sink.chunk("word " * 200)
        await sink.close("done")
        assert all(len(r) <= 60 for r in sink.snapshot.rows())


    async def test_a_failed_seat_reads_as_failed_not_as_an_argument(self) -> None:
        from joat.services.sinks import DebateStateSink

        sink = DebateStateSink()
        await sink.open("t", ["codex", "claude"])
        await sink.turn("CLAUDE (critic)")
        await sink.chunk("You're out of usage credits. Switch to another model")
        await sink.chunk("\n[!] claude could not answer: exited 1 — see the log")
        await sink.close("insufficient collaborators — 1 of 2 spoke")
        assert sink.snapshot.rows()[0] == "CLAUDE: could not answer"

    async def test_headline_matches_the_dashboard_panel_shape(self) -> None:
        from joat.services.sinks import DebateStateSink

        sink = DebateStateSink()
        assert sink.snapshot.headline() == "Brainstorm"          # never opened
        await sink.open("tabs or spaces", ["codex", "claude", "openclaw"])
        assert sink.snapshot.headline() == "Brainstorm · 3 seats"
        await sink.close("converged 0.91")
        assert sink.snapshot.headline() == "Brainstorm · converged 0.91"


class TestSilentWaitClock:
    """A turn says nothing for 12-40s. Without a moving number the wearer
    cannot tell working from hung, and the only remedy they know is to
    cancel a healthy session.
    """

    def _sink(self):
        from joat.services.hud import HudWriter
        from joat.services.sinks import HudSink
        from tests.unit.test_session import SinkTransport

        transport = SinkTransport()
        return HudSink(HudWriter(transport)), transport

    def _frames(self, transport):
        return [m for m in transport.sent
                if type(m).__name__ == "BrainstormHudView"]

    async def test_the_wait_is_counted_while_the_model_is_silent(self) -> None:
        from joat.services.sinks import HudSink

        sink, transport = self._sink()
        original = HudSink.WAIT_TICK_S
        HudSink.WAIT_TICK_S = 0.01          # keep the test honest and quick
        try:
            await sink.open("t", ["codex"])
            await sink.turn("CODEX (proposer)")
            await asyncio.sleep(0.05)
            activity = self._frames(transport)[-1].activity
            assert "Thinking… 0:0" in activity
        finally:
            HudSink.WAIT_TICK_S = original
            sink._stop_wait_clock()

    async def test_the_clock_stops_once_tokens_arrive(self) -> None:
        from joat.services.sinks import HudSink

        sink, transport = self._sink()
        original = HudSink.WAIT_TICK_S
        HudSink.WAIT_TICK_S = 0.01
        try:
            await sink.open("t", ["codex"])
            await sink.turn("CODEX (proposer)")
            await asyncio.sleep(0.03)
            await sink.chunk("the answer")
            painted = len(self._frames(transport))
            await asyncio.sleep(0.05)
            # Streaming is its own progress signal; the ticker must cost
            # the link nothing once it is under way.
            assert len(self._frames(transport)) == painted
            assert "the answer" in self._frames(transport)[-1].activity
        finally:
            HudSink.WAIT_TICK_S = original
            sink._stop_wait_clock()

    async def test_the_elapsed_suffix_does_not_survive_into_the_answer(self) -> None:
        from joat.services.sinks import HudSink

        sink, transport = self._sink()
        original = HudSink.WAIT_TICK_S
        HudSink.WAIT_TICK_S = 0.01
        try:
            await sink.open("t", ["codex"])
            await sink.turn("CODEX (proposer)")
            await asyncio.sleep(0.03)
            await sink.chunk("resident weights")
            activity = self._frames(transport)[-1].activity
            assert "Thinking… 0:" not in activity   # the clock is gone
            assert "resident weights" in activity
        finally:
            HudSink.WAIT_TICK_S = original
            sink._stop_wait_clock()

    async def test_closing_stops_the_clock(self) -> None:
        sink, _ = self._sink()
        await sink.open("t", ["codex"])
        await sink.turn("CODEX (proposer)")
        await sink.close("done")
        assert sink._wait_task is None or sink._wait_task.cancelled() \
            or sink._wait_task.done()


class TestCostVisibility:
    """Two subscription CLIs bill per turn.

    A wearer who cannot see how many rounds remain, or how many calls this
    session has already spent, cannot decide whether to let it run — and
    this project has hit provider quota limits repeatedly.
    """

    def _sink(self):
        from joat.services.hud import HudWriter
        from joat.services.sinks import HudSink
        from tests.unit.test_session import SinkTransport

        transport = SinkTransport()
        return HudSink(HudWriter(transport)), transport

    def _frames(self, transport):
        return [m for m in transport.sent
                if type(m).__name__ == "BrainstormHudView"]

    async def test_the_live_line_carries_round_and_calls(self) -> None:
        sink, transport = self._sink()
        await sink.open("t", ["codex", "claude"])
        await sink.progress(2, 2, 5)
        await sink.turn("CLAUDE (critic)")
        live = self._frames(transport)[-1].active_speaker
        assert "CLAUDE" in live
        assert "round 2/2" in live and "5 calls" in live

    async def test_it_is_optional_for_other_sinks(self) -> None:
        # A terminal or a snapshot has no use for a HUD concern and must
        # not have to implement it — the session discovers it by getattr.
        from joat.services.sinks import DebateStateSink, TerminalSink
        import io

        assert not hasattr(TerminalSink(out=io.StringIO()), "progress")
        assert not hasattr(DebateStateSink(), "progress")


class TestTheAnswerSurvivesTheEnding:
    """close() used to replace the transcript with a status line.

    The verified synthesis is what the wearer sat through the session for,
    and it vanished at the exact moment it finished.
    """

    def _sink(self):
        from joat.services.hud import HudWriter
        from joat.services.sinks import HudSink
        from tests.unit.test_session import SinkTransport

        transport = SinkTransport()
        return HudSink(HudWriter(transport)), transport

    def _last_activity(self, transport) -> str:
        frames = [m for m in transport.sent
                  if type(m).__name__ == "BrainstormHudView"]
        return frames[-1].activity

    async def test_the_synthesis_is_still_there_after_the_verdict(self) -> None:
        sink, transport = self._sink()
        await sink.open("cold-start latency", ["codex", "claude"])
        await sink.turn("JOAT (synthesis)")
        await sink.chunk("AGREED: measure the gate.")
        await sink.close("done — converged (agreement 0.95, round 1)")
        activity = self._last_activity(transport)
        assert "AGREED: measure the gate." in activity   # the answer
        assert "converged" in activity                   # and the verdict

    async def test_a_cancelled_session_keeps_what_was_said(self) -> None:
        sink, transport = self._sink()
        await sink.open("t", ["codex"])
        await sink.turn("CODEX (proposer)")
        await sink.chunk("a partial thought")
        await sink.close("cancelled")
        activity = self._last_activity(transport)
        assert "a partial thought" in activity
        assert "cancelled" in activity

    async def test_the_cost_meter_clears_when_the_session_ends(self) -> None:
        sink, transport = self._sink()
        await sink.open("t", ["codex"])
        await sink.progress(1, 2, 3)
        await sink.turn("CODEX (proposer)")
        await sink.close("done")
        frames = [m for m in transport.sent
                  if type(m).__name__ == "BrainstormHudView"]
        assert "calls" not in (frames[-1].active_speaker or "")
