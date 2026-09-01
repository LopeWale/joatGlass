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
                "endpoint_host": "home-mac.example-tailnet.ts.net",
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
        assert frame.activity == "OPENCLAW > Use tabs with a formatter."

    async def test_the_answer_replaces_the_marker_and_later_chunks_join_exactly(self) -> None:
        """The marker says "nothing yet"; the first token makes that false.

        The word "first" is not incidental. An earlier implementation
        re-scanned the row on every chunk to strip the marker, and this
        turn — whose own first word is "first" — is the one it silently
        ate. Only the opening chunk may rewrite the row.
        """
        transport = HudTransport()
        sink = HudSink(HudWriter(transport))  # type: ignore[arg-type]
        await sink.open("topic", ["hermes"])
        await sink.turn("hermes")

        await sink.chunk("first")
        first = transport.sent[-1]
        assert isinstance(first, BrainstormHudView)
        assert first.activity == "HERMES > first"

        await sink.chunk("-chunk")
        second = transport.sent[-1]
        assert isinstance(second, BrainstormHudView)
        assert second.activity == "HERMES > first-chunk"

        # A model that opens with whitespace must not push the answer off
        # the "NAME > " prefix by a stray column.
        await sink.turn("hermes")
        await sink.chunk(" already spaced")
        naturally_spaced = transport.sent[-1]
        assert isinstance(naturally_spaced, BrainstormHudView)
        assert naturally_spaced.activity == (
            "HERMES > first-chunk\nHERMES > already spaced"
        )

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
            "CODEX > Keep the decision reversible.\n"
            "CLAUDE > Check the calendar first.\n"
            "HERMES > I can stage a safe next action."
        )

    async def test_terminal_overlay_and_replay_keep_the_real_brainstorm_state(self) -> None:
        transport = HudTransport()
        sink = HudSink(
            HudWriter(transport),  # type: ignore[arg-type]
            lambda: {
                "state": "ready",
                "reason": "",
                "endpoint_host": "home-mac.example-tailnet.ts.net",
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


class TestTheTranscriptFillsTheGlass:
    """The activity bound is display capacity, and it is bound ONCE.

    The producer trims and the wire validates. While those were two
    independent literals, raising one and forgetting the other was a
    silent failure rather than a loud one: to_wire() raises ProtocolError,
    Broadcast swallows every sink exception by design so a dead surface
    cannot stop a debate, and the HUD just stops updating.
    """

    def test_the_producer_trims_to_exactly_what_the_wire_admits(self) -> None:
        from joat.core.protocol import BRAINSTORM_ACTIVITY_MAX
        from joat.services.sinks import HudSink

        full = "x" * BRAINSTORM_ACTIVITY_MAX
        assert HudSink._bounded_transcript(full) == full

        # One byte over: trimmed, and still a legal frame.
        trimmed = HudSink._bounded_transcript("y" + full)
        assert len(trimmed) <= BRAINSTORM_ACTIVITY_MAX
        BrainstormHudView(
            screen="brainstorm", state="running", topic="t",
            collaborators=["codex"], activity=trimmed, terminal_state="ready",
        ).to_wire()

    def test_a_full_transcript_survives_the_wire(self) -> None:
        """What the producer considers in-bounds must actually serialise."""
        from joat.core.protocol import BRAINSTORM_ACTIVITY_MAX

        wire = BrainstormHudView(
            screen="agent_terminal", state="running", topic="t",
            collaborators=["hermes"],
            activity="z" * BRAINSTORM_ACTIVITY_MAX,
            terminal_state="running",
        ).to_wire()
        assert len(wire["brainstorm"]["activity"]) == BRAINSTORM_ACTIVITY_MAX

    def test_the_bound_holds_scrollback_beyond_the_renderer_window(self) -> None:
        """The wire now carries SCROLLBACK; the renderer windows it. The
        window must fit inside the bound with real room left to scroll —
        a bound equal to the window is what made scrolling pointless."""
        import re
        from pathlib import Path
        from joat.core.protocol import (
            BRAINSTORM_ACTIVITY_MAX, BRAINSTORM_SCROLLBACK_ROWS,
        )

        renderer = (Path(__file__).resolve().parents[2]
                    / "apps/hub-plugin/src/HudCanvasRenderer.ts").read_text()
        capacity = re.search(r"transcriptLineCapacity:\s*(\d+)", renderer)
        assert capacity is not None, "the transcript layout has moved"
        window = int(capacity.group(1))
        assert BRAINSTORM_ACTIVITY_MAX == (
            BRAINSTORM_SCROLLBACK_ROWS * 74 + (BRAINSTORM_SCROLLBACK_ROWS - 1)
        )
        assert BRAINSTORM_SCROLLBACK_ROWS >= window * 3, (
            "scrollback should hold several windows of history"
        )


class TestFinishedTurnsCollapseToTheirGist:
    """The wearer must be able to FOLLOW the debate, not just witness it.

    A turn is capped at 120 words, which wraps to about nine 74-column
    lines; the transcript region draws at most seven. Streamed raw, the
    glass showed roughly one turn and the wearer could not tell who had
    said what. Collaborators are instructed to open with
    `GIST: <a dozen words>`, so a finished turn has a one-line summary of
    the speaker's OWN making to fall back to.
    """

    LONG = (
        "GIST: Buffer to clause boundaries; never stream raw tokens.\n\n"
        "Token-level streaming is wrong for this display. Every partial "
        "update is a BLE page write, and re-sending lines as words shift "
        "wrap position produces jitter in a monochrome overlay."
    )

    async def _sink(self):
        transport = HudTransport()
        sink = HudSink(HudWriter(transport))  # type: ignore[arg-type]
        await sink.open("topic", ["codex", "claude"])
        return sink, transport

    async def test_the_live_turn_keeps_its_detail(self) -> None:
        sink, transport = await self._sink()
        await sink.turn("codex (proposer)")
        await sink.chunk(self.LONG)
        assert "monochrome overlay" in transport.sent[-1].activity

    async def test_a_finished_turn_keeps_only_its_gist(self) -> None:
        sink, transport = await self._sink()
        await sink.turn("codex (proposer)")
        await sink.chunk(self.LONG)
        await sink.turn("claude (critic)")
        await sink.chunk("GIST: Stream it.\n\nDetail nobody has read yet.")

        activity = transport.sent[-1].activity
        assert "CODEX > Buffer to clause boundaries; never stream raw tokens." \
            in activity
        assert "monochrome overlay" not in activity, "a finished turn kept its prose"
        assert "Detail nobody has read yet." in activity, "the live turn lost detail"

    async def test_the_synthesis_is_never_collapsed(self) -> None:
        """close() retires the last turn WHOLE. It is the answer the wearer
        sat through the session for; a one-line summary of it is a loss."""
        sink, transport = await self._sink()
        await sink.turn("joat (synthesis)")
        await sink.chunk("AGREED\nMeasure before caching.\n\nOPEN\nEviction order.")
        await sink.close("converged (agreement 0.93, round 2)")

        activity = transport.sent[-1].activity
        assert "OPEN" in activity and "Eviction order." in activity
        assert "JOAT > converged (agreement 0.93, round 2)" in activity

    async def test_a_failed_seat_is_not_quoted_as_an_argument(self) -> None:
        """Its stdout carried the CLI's complaint. Summarised verbatim it
        reads exactly like a position the collaborator never took."""
        sink, transport = await self._sink()
        await sink.turn("codex (proposer)")
        await sink.chunk("[!] You are out of usage credits until August 28.")
        await sink.turn("claude (critic)")
        await sink.chunk("GIST: I will take it from here.")

        activity = transport.sent[-1].activity
        assert "CODEX > could not answer" in activity
        assert "usage credits" not in activity

    async def test_a_turn_without_a_gist_falls_back_to_its_opening_words(self) -> None:
        sink, transport = await self._sink()
        await sink.turn("codex (proposer)")
        await sink.chunk("No gist line here, just " + "prose " * 40)
        await sink.turn("claude (critic)")
        await sink.chunk("GIST: noted.")

        row = [line for line in transport.sent[-1].activity.splitlines()
               if line.startswith("CODEX > ")][0]
        assert row.startswith("CODEX > No gist line here, just prose")
        assert row.endswith("…"), "an unbounded fallback would eat the screen"
        assert len(row) <= HudSink.ROW_COLUMNS, (
            f"a collapsed row must fit one line: {len(row)}"
        )

    async def test_a_long_speaker_prefix_does_not_push_the_row_over(self) -> None:
        """The row budget is the WHOLE row, prefix included.

        A fixed body cap read as correct until a live two-round session
        produced the prefix "CLAUDE — ROUND 2 > " — nineteen columns — and
        finished rows reached 83, so the renderer truncated a second time
        on top of this one.
        """
        sink, transport = await self._sink()
        await sink.turn("claude (critic) — round 2")
        await sink.chunk("GIST: " + "keep the judge warm during active work " * 5)
        await sink.turn("codex (proposer)")
        await sink.chunk("GIST: noted.")

        collapsed = [line for line in transport.sent[-1].activity.splitlines()
                     if line.startswith("CLAUDE")][0]
        assert collapsed.startswith("CLAUDE — ROUND 2 > keep the judge warm")
        assert len(collapsed) == HudSink.ROW_COLUMNS, (
            f"row is {len(collapsed)} columns, glass holds {HudSink.ROW_COLUMNS}"
        )
        assert collapsed.endswith("…")


class TestTheSilentWaitIsCovered:
    """A turn is almost entirely silent, so the clock is load-bearing.

    Measured against the real CLIs: codex delivered a 380-character answer
    in ONE chunk with the first byte at 9.7s of a 10.0s turn, and claude
    845 characters in two chunks at 10.4s of 10.7s. The wearer spends
    96-98% of every turn with nothing on the glass but this marker. Both
    tools buffer; the streaming loop in subprocess_agent already yields
    each line the moment it arrives.

    Every other test here overrides WAIT_TICK_S to run fast, which leaves
    the shipped value itself unguarded — and the shipped value is the one
    the wearer actually experiences.
    """

    def test_the_tick_reassures_well_inside_one_turn(self) -> None:
        from joat.services.sinks import HudSink

        assert HudSink.WAIT_TICK_S <= 5.0, (
            "turns run 10-18s and are silent for nearly all of it; a slower "
            "tick leaves the wearer unable to tell working from hung, and "
            "the only remedy they know is cancelling a healthy session"
        )
        assert HudSink.WAIT_TICK_S >= 1.0, (
            "every tick is a BLE page write for a counter nobody reads that "
            "closely"
        )

    async def test_a_turn_that_produced_nothing_says_so(self) -> None:
        """A silent turn must not be filed as "Thinking…" forever.

        Reachable whenever a CLI exits cleanly with nothing usable — all
        of its output noise-filtered, say. The marker is a claim about a
        turn IN PROGRESS; once the turn is over it is simply false, which
        is the same defect this sink already had to fix in front of live
        answers.
        """
        transport = HudTransport()
        sink = HudSink(HudWriter(transport))  # type: ignore[arg-type]
        await sink.open("topic", ["codex", "claude"])
        await sink.turn("codex (proposer)")          # no chunk ever arrives
        await sink.turn("claude (critic)")
        await sink.chunk("GIST: I will answer instead.")
        await sink.close("converged")

        activity = transport.sent[-1].activity
        assert "CODEX > said nothing" in activity
        assert "Thinking…" not in activity


class TestBlankRowsAreNotAffordable:
    """Seven rows, and a paragraph break spends one of them on nothing.

    Collaborators are told to open with a GIST line and then argue, so
    almost every live turn arrives as "GIST: …\\n\\n<argument>" — 14% of
    the glass showing a blank row, on the surface whose complaint was that
    it did not show enough.

    The depth is not lost: the stored transcript, the saved history and
    the Markdown export all keep the wearer's paragraphs. This is a
    rendering decision about a 576x288 display.
    """

    async def _sink(self):
        transport = HudTransport()
        sink = HudSink(HudWriter(transport))  # type: ignore[arg-type]
        await sink.open("topic", ["codex"])
        return sink, transport

    async def test_a_paragraph_break_does_not_cost_a_row(self) -> None:
        sink, transport = await self._sink()
        await sink.turn("codex (proposer)")
        await sink.chunk("GIST: cache them.\n\nCold start costs 2.1s per query.")

        rows = transport.sent[-1].activity.splitlines()
        assert "" not in rows, rows
        assert rows == ["CODEX > GIST: cache them.",
                        "Cold start costs 2.1s per query."]

    async def test_it_survives_a_break_split_across_chunks(self) -> None:
        """Streaming delivers "\\n" and "\\n" separately, so a per-chunk
        check would never see the pair. This collapses at render time."""
        sink, transport = await self._sink()
        await sink.turn("codex (proposer)")
        for piece in ("GIST: cache them.", "\n", "\n", "Cold start costs 2.1s."):
            await sink.chunk(piece)

        rows = transport.sent[-1].activity.splitlines()
        assert "" not in rows, rows
        assert len(rows) == 2, rows

    async def test_whitespace_only_rows_go_too(self) -> None:
        sink, transport = await self._sink()
        await sink.turn("codex (proposer)")
        await sink.chunk("GIST: cache them.\n   \n\t\nCold start costs 2.1s.")

        rows = transport.sent[-1].activity.splitlines()
        assert [r for r in rows if not r.strip()] == [], rows

    async def test_single_line_breaks_are_kept(self) -> None:
        """Structure the model chose is still structure — only EMPTY rows
        are unaffordable, not line breaks."""
        sink, transport = await self._sink()
        await sink.turn("joat (synthesis)")
        await sink.chunk("AGREED\nMeasure first.\nOPEN\nEviction order.")

        rows = transport.sent[-1].activity.splitlines()
        assert len(rows) == 4, rows

    async def test_the_synthesis_keeps_no_blank_row_either(self) -> None:
        """The row that matters most was the one still wasting a row.

        close() retires the last turn UNCOLLAPSED on purpose — it is the
        answer, not a summary — so an earlier version of this fix, which
        only cleaned the LIVE row, left a blank line inside the synthesis.
        AGREED and OPEN are separated by one, so it was there every time.
        """
        sink, transport = await self._sink()
        await sink.turn("joat (synthesis)")
        await sink.chunk("AGREED\nMeasure before caching.\n\nOPEN\nEviction order.")
        await sink.close("converged (agreement 0.92, round 2)")

        rows = transport.sent[-1].activity.splitlines()
        assert [r for r in rows if not r.strip()] == [], rows
        assert "AGREED" in rows[0] and "OPEN" in rows[2], rows
        assert rows[-1].startswith("JOAT > converged")

    async def test_a_turn_that_streams_only_whitespace_says_so_too(self) -> None:
        """The sibling of the silent turn, and the one I missed.

        Keying on "did a chunk arrive" caught a turn that never streamed
        and left the turn whose only chunk was a stray newline: it retired
        as a bare "CODEX >", an empty speaker row spending a line of seven
        to say nothing.
        """
        transport = HudTransport()
        sink = HudSink(HudWriter(transport))  # type: ignore[arg-type]
        await sink.open("topic", ["codex", "claude"])
        await sink.turn("codex (proposer)")
        await sink.chunk("\n")
        await sink.chunk("   ")
        await sink.turn("claude (critic)")
        await sink.chunk("GIST: I will answer instead.")
        await sink.close("converged")

        rows = transport.sent[-1].activity.splitlines()
        assert "CODEX > said nothing" in rows, rows
        assert not any(r.rstrip().endswith(">") for r in rows), rows


class TestTheTerminalHandoffSurvivesStreaming:
    """Watched live: tapping to the terminal painted it for ONE frame,
    then the next streamed token repainted the Brainstorm over it. While
    the wearer is on the terminal view, running repaints are withheld;
    the closing verdict still lands, and replay() returns to the room."""

    async def test_a_streamed_chunk_does_not_snatch_the_lenses_back(self) -> None:
        transport = HudTransport()
        sink = HudSink(HudWriter(transport))  # type: ignore[arg-type]
        await sink.open("topic", ["codex"])
        await sink.turn("codex (proposer)")
        await sink.chunk("GIST: first.")
        await sink.show_terminal()
        assert transport.sent[-1].screen == "agent_terminal"

        await sink.chunk(" more words arrive")
        assert transport.sent[-1].screen == "agent_terminal", (
            "a streaming token stole the surface back from the terminal"
        )

        await sink.replay()
        assert transport.sent[-1].screen == "brainstorm"
        assert "more words arrive" in transport.sent[-1].activity

    async def test_the_verdict_still_lands_while_handed_off(self) -> None:
        transport = HudTransport()
        sink = HudSink(HudWriter(transport))  # type: ignore[arg-type]
        await sink.open("topic", ["codex"])
        await sink.turn("joat (synthesis)")
        await sink.chunk("AGREED\nShip it.")
        await sink.show_terminal()
        await sink.close("converged (agreement 0.90, round 1)")

        assert transport.sent[-1].screen == "brainstorm"
        assert "converged" in transport.sent[-1].activity
