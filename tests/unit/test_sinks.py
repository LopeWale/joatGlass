"""Where a debate is watched.

"I wanna see as they're debating ... because then I cannot prove that that
was debated." Two surfaces with opposite constraints, and neither may be
able to silence the other.
"""

from __future__ import annotations

import asyncio
import re

import io
from collections.abc import AsyncIterator

import pytest

from joat.core.protocol import AgentTerminalHudView, BrainstormHudView, HudRebuild
from joat.services.hud import HudWriter
from joat.services.sinks import Broadcast, HudSink, TerminalSink
from joat.services.agent_terminal import AgentTerminalRoster, TerminalFrame
from joat.services.terminal_opening import TerminalOpeningController


class HudTransport:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, message: object) -> None:
        self.sent.append(message)


class TerminalHandoffRig:
    """The real opening/roster path; no remote model or device calls."""

    class Agent:
        def __init__(self, *, fail: bool = False) -> None:
            self.capture_context_generation: int | None = None
            self.fail = fail
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.release.set()

        async def initialize(self) -> None:
            self.started.set()
            await self.release.wait()
            if self.fail:
                raise RuntimeError("synthetic setup refusal")
            self.capture_context_generation = 1

        async def stream(self, text: str) -> AsyncIterator[str]:
            raise AssertionError("Handoff must not send an instruction")
            yield ""

        async def stream_captured(self, text: str, *, context_generation: int) -> AsyncIterator[str]:
            raise AssertionError("Handoff must not send a captured instruction")
            yield ""

    def __init__(self, *, fail: bool = False) -> None:
        self.transport = HudTransport()
        self.hud = HudWriter(self.transport)  # type: ignore[arg-type]
        self.agent = self.Agent(fail=fail)
        self.roster = AgentTerminalRoster([("work", self.agent), ("hermes", self.Agent())])
        self.opening = TerminalOpeningController(
            self.roster, paint=self.paint, connection_generation=lambda: "connection",
        )
        self.sink = HudSink(self.hud, open_terminal=self.open)

    async def paint(self, frame: TerminalFrame, request_id: str | None, selection_id: str | None) -> None:
        await self.hud.show_agent_terminal(AgentTerminalHudView(
            state=frame.state, focus=frame.topic, lanes=frame.lanes,
            activity="\n".join(frame.lines), reason=frame.reason,
            opening_request_id=request_id, opening_selection_id=selection_id,
        ))

    async def open(self) -> bool:
        await self.opening.open("work", "visit", "selection", lambda: True)
        return self.opening.current


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
        sink = HudSink(HudWriter(transport))  # type: ignore[arg-type]
        await sink.open("tabs or spaces", ["codex (proposer)", "openclaw (agent terminal)"])
        await sink.turn("openclaw (agent terminal)")
        await sink.chunk("Use tabs with a formatter.")
        frame = transport.sent[-1]
        assert isinstance(frame, BrainstormHudView)
        assert frame.state == "running"
        assert frame.active_speaker == "openclaw"
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
        rig = TerminalHandoffRig()
        transport, sink = rig.transport, rig.sink
        await sink.open("reconnect safely", ["codex", "claude", "hermes"])
        await sink.turn("hermes (agent terminal)")
        await sink.show_terminal()

        terminal = transport.sent[-1]
        assert isinstance(terminal, AgentTerminalHudView)
        assert terminal.state == "ready"
        assert terminal.focus == "work · idle"
        assert terminal.lanes == ["work·idle", "hermes·setup"]
        assert "Thinking…" not in terminal.activity

        await sink.replay()
        restored = transport.sent[-1]
        assert isinstance(restored, BrainstormHudView)
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

    The number still moves — it is animated ON DEVICE now. The engine used
    to recompute it and re-ship a whole frame every three seconds, and the
    wearer saw straight through that: "it's only changing every three
    seconds … we're doing it the difficult way." What the engine owes the
    plugin is a marker to animate and silence afterwards; the tests that
    used to pin the ticker pin those two things instead.
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

    async def test_a_silent_turn_shows_the_marker_the_plugin_animates(
        self,
    ) -> None:
        sink, transport = self._sink()
        await sink.open("t", ["codex"])
        await sink.turn("CODEX (proposer)")
        assert self._frames(transport)[-1].activity.endswith("Thinking…")

    async def test_the_engine_does_not_reship_frames_to_move_a_counter(
        self,
    ) -> None:
        """The whole point of the change. A silent turn costs the link one
        frame, not one every three seconds — however long it stays silent.
        """
        sink, transport = self._sink()
        await sink.open("t", ["codex"])
        await sink.turn("CODEX (proposer)")
        painted = len(self._frames(transport))
        await asyncio.sleep(0.05)
        assert len(self._frames(transport)) == painted

    async def test_the_marker_gives_way_to_the_answer(self) -> None:
        sink, transport = self._sink()
        await sink.open("t", ["codex"])
        await sink.turn("CODEX (proposer)")
        await sink.chunk("resident weights")
        activity = self._frames(transport)[-1].activity
        assert "Thinking…" not in activity
        assert "resident weights" in activity

    def test_the_marker_reaches_the_glass_as_written(self) -> None:
        """The marker is the engine's word, shown verbatim.

        The raster transcript once matched the tail row against its own
        spelling (`endsWith('Thinking…')`) to animate an elapsed counter,
        a string shared across two repositories that this test pinned
        together. That page is gone (2026-10-05): the Agent Terminal and
        Brainstorm are the firmware's own text page
        (`NativeTerminalSurface`), which shows the Mac's rows as they
        arrive and keeps no spelling of its own — so there is nothing on
        the plugin side to drift. Pin that it stays that way: a plugin that
        starts matching the word again must bring this test back with it.
        """
        from pathlib import Path

        from joat.services.sinks import HudSink

        surface = (Path(__file__).resolve().parents[2]
                   / "apps/hub-plugin/src/NativeTerminalSurface.ts").read_text()
        # The native page matches the tail row again to run its local
        # elapsed counter (one in-place status-row upgrade a second), so the
        # spelling is pinned on both sides once more.
        assert HudSink.THINKING_MARKER == "Thinking…"
        assert f"const THINKING_MARKER = '{HudSink.THINKING_MARKER}';" in surface


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
        await sink.progress(2, 5)
        await sink.turn("CLAUDE (critic)")
        live = self._frames(transport)[-1].active_speaker
        assert "CLAUDE" in live
        assert "round 2" in live and "5 calls" in live
        # A round has no maximum to count to: it goes on while the wearer does.
        assert "/" not in live

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
        await sink.turn("CODEX (proposer)")
        await sink.chunk("Measure the gate first.")
        await sink.close("cancelled")
        activity = self._last_activity(transport)
        assert "Measure the gate first." in activity     # the answer
        assert "cancelled" in activity                   # and the verdict

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
        await sink.progress(1, 3)
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
        from joat.core.protocol import BRAINSTORM_TRANSCRIPT_MAX
        from joat.services.sinks import HudSink

        full = "x" * BRAINSTORM_TRANSCRIPT_MAX
        assert HudSink._bounded_transcript(full) == full

        # One byte over: trimmed, and still a legal frame.
        trimmed = HudSink._bounded_transcript("y" + full)
        assert len(trimmed) <= BRAINSTORM_TRANSCRIPT_MAX
        BrainstormHudView(state="running", topic="t", collaborators=["codex"], activity=trimmed).to_wire()

    def test_a_full_transcript_survives_the_wire(self) -> None:
        """What the producer considers in-bounds must actually serialise."""
        from joat.core.protocol import BRAINSTORM_ACTIVITY_MAX

        wire = AgentTerminalHudView(
             state="running", focus="t",
            lanes=["hermes"],
            activity="z" * BRAINSTORM_ACTIVITY_MAX,
        ).to_wire()
        assert len(wire["terminal"]["activity"]) == BRAINSTORM_ACTIVITY_MAX

    def test_the_bound_holds_scrollback_beyond_the_renderer_window(self) -> None:
        """The wire now carries SCROLLBACK; the renderer windows it. The
        window must fit inside the bound with real room left to scroll —
        a bound equal to the window is what made scrolling pointless."""
        import re
        from pathlib import Path
        from joat.core.protocol import (
    AgentTerminalHudView,
            BRAINSTORM_ACTIVITY_MAX, BRAINSTORM_ROW_COLUMNS,
            BRAINSTORM_SCROLLBACK_ROWS,
        )

        # The transcript is the firmware's own text page (2026-10-05):
        # `bodyLineCapacity` lines, less the status row and the footer.
        surface = (Path(__file__).resolve().parents[2]
                   / "apps/hub-plugin/src/NativeTerminalSurface.ts").read_text()
        capacity = re.search(r"bodyLineCapacity:\s*(\d+)", surface)
        fixed = re.search(r"const BODY_FIXED_ROWS\s*=\s*(\d+)", surface)
        assert capacity is not None and fixed is not None, (
            "the native terminal layout has moved"
        )
        window = int(capacity.group(1)) - int(fixed.group(1))
        assert window >= 5, "a transcript window under five rows is not a transcript"
        assert BRAINSTORM_ACTIVITY_MAX == (
            BRAINSTORM_SCROLLBACK_ROWS * BRAINSTORM_ROW_COLUMNS
            + (BRAINSTORM_SCROLLBACK_ROWS - 1)
        )
        # The page wraps every row by measured glyph width
        # (`wrapWithMetrics`), so the Mac's column budget is a size bound,
        # not a layout: it must stay inside the plugin's per-row unit cap
        # or a full-width Mac row would be split in two on the glass.
        units = re.search(r"const MAX_LINE_UNITS\s*=\s*(\d+)", surface)
        assert units is not None, "the native terminal's row cap has moved"
        assert BRAINSTORM_ROW_COLUMNS <= int(units.group(1)), (
            "protocol.py's column budget exceeds the plugin's row cap"
        )
        assert BRAINSTORM_SCROLLBACK_ROWS >= window * 3, (
            "scrollback should hold several windows of history"
        )


class TestFinishedTurnsStayWhole:
    """The wearer must be able to READ the conversation, not just witness it.

    Finished turns used to collapse to the one `GIST:` line their speaker
    opened with (and every turn was capped at 120 words), so the wearer read
    a single line of each answer and could not read the rest. A turn is
    kept whole; the glasses scroll it.
    """

    LONG = (
        "Buffer to clause boundaries; never stream raw tokens.\n\n"
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

    async def test_a_finished_turn_keeps_all_of_its_prose(self) -> None:
        sink, transport = await self._sink()
        await sink.turn("codex (proposer)")
        await sink.chunk(self.LONG)
        await sink.turn("claude (critic)")
        await sink.chunk("Stream it.\n\nDetail nobody has read yet.")

        activity = transport.sent[-1].activity
        assert "monochrome overlay" in activity, "a finished turn lost its prose"
        assert "Token-level streaming is wrong" in activity
        assert "Detail nobody has read yet." in activity

    async def test_a_gist_line_is_just_text(self) -> None:
        """Nothing asks for a GIST any more, and nothing keys on one."""
        sink, transport = await self._sink()
        await sink.turn("codex (proposer)")
        await sink.chunk("GIST: not special.\n\nThe body that follows it.")
        await sink.turn("claude (critic)")
        await sink.chunk("Reply.")

        activity = transport.sent[-1].activity
        assert "GIST: not special." in activity
        assert "The body that follows it." in activity

    async def test_a_very_long_answer_is_kept_to_the_last_word(self) -> None:
        sink, transport = await self._sink()
        answer = " ".join(f"point{i} is explained here." for i in range(600))
        await sink.turn("codex (proposer)")
        for i in range(0, len(answer), 500):
            await sink.chunk(answer[i:i + 500])
        await sink.turn("claude (critic)")
        await sink.chunk("Reply.")

        activity = transport.sent[-1].activity
        assert "point0 is explained here." in activity
        assert "point599 is explained here." in activity

    async def test_the_last_turn_is_never_collapsed(self) -> None:
        """close() retires the last turn WHOLE. It is the answer the wearer
        sat through the session for; a one-line summary of it is a loss."""
        sink, transport = await self._sink()
        await sink.turn("codex (proposer)")
        await sink.chunk("First the cache.\n\nThen eviction order.")
        await sink.close("cancelled")

        activity = transport.sent[-1].activity
        assert "Then eviction order." in activity
        assert "JOAT > cancelled" in activity

    async def test_a_failed_seat_is_not_quoted_as_an_argument(self) -> None:
        """Its stdout carried the CLI's complaint. Shown verbatim it reads
        exactly like a position the collaborator never took."""
        sink, transport = await self._sink()
        await sink.turn("codex (proposer)")
        await sink.chunk("[!] You are out of usage credits until August 28.")
        await sink.turn("claude (critic)")
        await sink.chunk("I will take it from here.")

        activity = transport.sent[-1].activity
        assert "CODEX > could not answer" in activity
        assert "usage credits" not in activity


class TestTheWaitingRoomIsTheReader:
    """After a round the room stops and the conversation goes up to read, in
    the same swipe-scroll as a News article. Nothing in it is collapsed."""

    def _sink(self, *, shows: bool = True, current=lambda: True):
        shown: list[str] = []
        hidden: list[bool] = []
        transport = HudTransport()

        async def show_room(text: str) -> bool:
            shown.append(text)
            return shows

        sink = HudSink(
            HudWriter(transport),  # type: ignore[arg-type]
            brainstorm_view_current=lambda: current(),
            show_room=show_room,
            hide_room=lambda: hidden.append(True),
        )
        return sink, transport, shown, hidden

    async def _one_round(self, sink):
        await sink.open("heat pumps", ["codex", "claude"])
        await sink.turn("codex (proposer)")
        await sink.chunk("First paragraph.\n\nSecond paragraph of a long answer.")
        await sink.turn("claude (critic)")
        await sink.chunk("Claude agrees, then adds a plan:\n1. Survey the house\n2. Size the unit")

    async def test_the_whole_conversation_is_put_up_to_read(self) -> None:
        sink, _transport, shown, _hidden = self._sink()
        await self._one_round(sink)
        await sink.waiting()
        assert len(shown) == 1
        text = shown[0]
        assert text.index("CODEX > First paragraph.") < text.index("CLAUDE > Claude agrees")
        assert "Second paragraph of a long answer." in text      # not collapsed
        assert "2. Size the unit" in text                         # whole, to the end
        assert "\n\nCLAUDE > " in text                           # a blank line between turns

    async def test_the_next_turn_takes_the_page_back(self) -> None:
        sink, transport, _shown, hidden = self._sink()
        await self._one_round(sink)
        await sink.waiting()
        before = len(transport.sent)
        await sink.turn("codex (proposer) — round 2")
        assert hidden == [True]
        assert len(transport.sent) > before                        # the live page is painted again
        assert transport.sent[-1].state == "running"

    async def test_a_reader_that_could_not_open_falls_back_to_the_live_page(self) -> None:
        sink, transport, shown, hidden = self._sink(shows=False)
        await self._one_round(sink)
        await sink.waiting()
        assert len(shown) == 1
        assert transport.sent[-1].state == "running"
        assert "2. Size the unit" in transport.sent[-1].activity
        assert hidden == []

    async def test_nothing_opens_when_brainstorm_is_not_the_current_view(self) -> None:
        sink, transport, shown, _hidden = self._sink(current=lambda: False)
        await sink.open("t", ["codex", "claude"])
        await sink.waiting()
        assert shown == []

    async def test_ending_the_room_takes_the_page_back_and_keeps_the_conversation(self) -> None:
        sink, transport, _shown, hidden = self._sink()
        await self._one_round(sink)
        await sink.waiting()
        await sink.close("cancelled")
        assert hidden == [True]
        final = transport.sent[-1]
        assert final.state == "cancelled"
        assert "2. Size the unit" in final.activity

    async def test_coming_back_to_a_waiting_room_shows_the_reader_again(self) -> None:
        sink, transport, shown, _hidden = self._sink()
        await self._one_round(sink)
        await sink.waiting()
        await sink.replay()
        assert len(shown) == 2
        assert shown[1] == shown[0]

    async def test_the_room_that_is_not_waiting_does_not_open_the_reader(self) -> None:
        sink, _transport, shown, _hidden = self._sink()
        await self._one_round(sink)
        await sink.replay()
        assert shown == []


class TestTheSilentWaitIsCovered:
    """A turn is almost entirely silent, so the clock is load-bearing.

    Measured against the real CLIs: codex delivered a 380-character answer
    in ONE chunk with the first byte at 9.7s of a 10.0s turn, and claude
    845 characters in two chunks at 10.4s of 10.7s. The wearer spends
    96-98% of every turn with nothing on the glass but this marker. Both
    tools buffer; the streaming loop in subprocess_agent already yields
    each line the moment it arrives.

    The tick lives in the plugin now, so this asserts across the seam.
    The engine no longer has a rate to guard — it paints the marker once —
    but the wearer's experience still depends on one, and a rate nobody
    tests is how it reached three seconds in the first place.
    """

    def test_the_silent_wait_shows_the_marker_once(self) -> None:
        """The engine paints the marker ONCE; a silent turn costs one frame.

        The elapsed counter that this test used to time lived on the raster
        transcript, which the firmware's text page replaced before the
        raster page was deleted (2026-10-05): the glass shows the marker
        as written and no clock beside it. That is a gap, recorded in the
        Changelog, not a rate to guard — an in-place upgrade of the
        terminal's status row once a second would be the native version.
        Until it exists, pin the engine's side: the marker goes out once
        per turn, as its own row, and never again until words arrive.
        """
        from pathlib import Path

        from joat.services.sinks import HudSink

        # The native version now exists: NativeTerminalSurface matches the
        # marker and HudPresenter upgrades the status row in place once a
        # second. The engine's side is unchanged: one marker, one frame.
        assert HudSink.THINKING_MARKER == "Thinking…"
        assert f"const THINKING_MARKER = '{HudSink.THINKING_MARKER}';" in (
            Path(__file__).resolve().parents[2]
            / "apps/hub-plugin/src/NativeTerminalSurface.ts").read_text()

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
    the closing verdict is retained, and replay() returns to the room."""

    async def test_a_streamed_chunk_does_not_snatch_the_lenses_back(self) -> None:
        rig = TerminalHandoffRig()
        transport, sink = rig.transport, rig.sink
        await sink.open("topic", ["codex"])
        await sink.turn("codex (proposer)")
        await sink.chunk("GIST: first.")
        await sink.show_terminal()
        assert isinstance(transport.sent[-1], AgentTerminalHudView)

        await sink.chunk(" more words arrive")
        assert isinstance(transport.sent[-1], AgentTerminalHudView), (
            "a streaming token stole the surface back from the terminal"
        )

        await sink.replay()
        assert isinstance(transport.sent[-1], BrainstormHudView)
        assert "more words arrive" in transport.sent[-1].activity

    @pytest.mark.parametrize("verdict,state", [
        ("converged (agreement 0.90, round 1)", "settled"),
        ("cancelled", "cancelled"),
        ("blocked · collaborator disconnected", "blocked"),
    ])
    async def test_final_state_waits_for_explicit_return(self, verdict: str, state: str) -> None:
        rig = TerminalHandoffRig()
        transport, sink = rig.transport, rig.sink
        await sink.open("topic", ["codex"])
        await sink.turn("joat (synthesis)")
        await sink.chunk("AGREED\nShip it.")
        await sink.show_terminal()
        await sink.close(verdict)

        assert isinstance(transport.sent[-1], AgentTerminalHudView)
        assert sink.terminal_owns_hud()
        await sink.replay()

        assert isinstance(transport.sent[-1], BrainstormHudView)
        assert transport.sent[-1].state == state
        assert verdict in transport.sent[-1].activity
        assert "Ship it." in transport.sent[-1].activity


class TestOwnedTerminalHandoff:
    async def test_departed_queued_open_does_not_issue_an_entry_rebuild(self) -> None:
        transport = HudTransport()
        sink = HudSink(HudWriter(transport), brainstorm_view_current=lambda: False)  # type: ignore[arg-type]
        await sink.open("queued topic", ["codex"])
        assert not transport.sent

    @pytest.mark.parametrize("operation", ["open", "replay"])
    @pytest.mark.parametrize("departure", ["view", "handoff"])
    async def test_delayed_entry_does_not_paint_after_departure(self, operation: str, departure: str) -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        class HeldEntryTransport(HudTransport):
            hold = False

            async def send(self, message: object) -> None:
                await super().send(message)
                if self.hold and isinstance(message, HudRebuild):
                    entered.set()
                    await release.wait()

        async def open_terminal() -> bool:
            return True

        current = True
        transport = HeldEntryTransport()
        sink = HudSink(HudWriter(transport), open_terminal=open_terminal,  # type: ignore[arg-type]
                       brainstorm_view_current=lambda: current)
        if operation == "replay":
            await sink.open("topic", ["codex"])
        transport.hold = True
        pending = asyncio.create_task(sink.open("topic", ["codex"]) if operation == "open" else sink.replay())
        await asyncio.wait_for(entered.wait(), 1)
        before = len(transport.sent)
        if departure == "view":
            current = False
        else:
            await sink.show_terminal()
        release.set()
        await pending
        assert len(transport.sent) == before
        assert isinstance(transport.sent[-1], HudRebuild)
        if departure == "handoff":
            assert sink.terminal_owns_hud()

    async def test_explicit_replay_rebuilds_before_returning_current_frame(self) -> None:
        transport = HudTransport()
        sink = HudSink(HudWriter(transport))  # type: ignore[arg-type]
        await sink.open("topic", ["codex"])
        await sink.chunk("current transcript")
        before = len(transport.sent)
        await sink.replay()
        returned = transport.sent[before:]
        assert len(returned) == 2
        assert isinstance(returned[0], HudRebuild)
        assert returned[0].reason == "brainstorm"
        assert isinstance(returned[1], BrainstormHudView)
        assert "current transcript" in returned[1].activity

    async def test_handoff_matches_normal_opening_including_blocked_setup(self) -> None:
        normal = TerminalHandoffRig(fail=True)
        assert await normal.open()
        expected = normal.transport.sent[-1]
        rig = TerminalHandoffRig(fail=True)
        await rig.sink.open("topic", ["codex"])
        await rig.sink.chunk("Brainstorm output is not a terminal transcript.")
        await rig.sink.show_terminal()
        actual = rig.transport.sent[-1]
        assert isinstance(actual, AgentTerminalHudView)
        assert isinstance(expected, AgentTerminalHudView)
        assert actual == expected
        assert actual.state != "running"
        assert rig.sink.terminal_owns_hud()

    async def test_preparation_yields_display_before_initializer_finishes(self) -> None:
        rig = TerminalHandoffRig()
        rig.agent.release.clear()
        await rig.sink.open("topic", ["codex"])
        opening = asyncio.create_task(rig.sink.show_terminal())
        try:
            await asyncio.wait_for(rig.agent.started.wait(), 1)
            preparing = rig.transport.sent[-1]
            assert isinstance(preparing, AgentTerminalHudView)
            assert preparing.state == "unavailable"
            assert preparing.focus == "work · preparing"
            assert preparing.reason == "Connecting to work…"
            assert rig.sink.terminal_owns_hud()
            await rig.sink.chunk("background answer")
            assert rig.transport.sent[-1] is preparing
        finally:
            rig.agent.release.set()
            await opening
        assert rig.transport.sent[-1].state == "ready"

    async def test_missing_opener_never_fabricates_terminal(self) -> None:
        transport = HudTransport()
        sink = HudSink(HudWriter(transport))  # type: ignore[arg-type]
        await sink.open("topic", ["codex"])
        before = len(transport.sent)
        await sink.show_terminal()
        assert len(transport.sent) == before
        assert not sink.terminal_owns_hud()

    async def test_departed_brainstorm_cannot_start_an_old_handoff(self) -> None:
        rig = TerminalHandoffRig()
        sink = HudSink(rig.hud, open_terminal=rig.open, brainstorm_view_current=lambda: False)
        await sink.show_terminal()
        assert not rig.agent.started.is_set()
        assert not rig.opening.current
        assert not sink.terminal_owns_hud()

    async def test_cancelled_opening_releases_its_own_handoff(self) -> None:
        entered = asyncio.Event()

        async def cancelled() -> bool:
            entered.set()
            await asyncio.Event().wait()
            return True

        transport = HudTransport()
        sink = HudSink(HudWriter(transport), open_terminal=cancelled)  # type: ignore[arg-type]
        await sink.open("topic", ["codex"])
        opening = asyncio.create_task(sink.show_terminal())
        await asyncio.wait_for(entered.wait(), 1)
        opening.cancel()
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert not sink.terminal_owns_hud()
        await sink.chunk("still running")
        assert isinstance(transport.sent[-1], BrainstormHudView)
        assert "still running" in transport.sent[-1].activity

    async def test_rejected_handoff_restores_latest_brainstorm_state(self) -> None:
        transport = HudTransport()

        async def reject() -> bool:
            await sink.chunk("arrived while opening")
            return False

        sink = HudSink(HudWriter(transport), open_terminal=reject)  # type: ignore[arg-type]
        await sink.open("topic", ["codex"])
        await sink.show_terminal()
        assert not sink.terminal_owns_hud()
        assert isinstance(transport.sent[-1], BrainstormHudView)
        assert "arrived while opening" in transport.sent[-1].activity

    @pytest.mark.parametrize("fail", [False, True])
    async def test_late_opening_result_cannot_rollback_a_new_handoff(self, fail: bool) -> None:
        transport = HudTransport()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def open_terminal() -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
                if fail:
                    raise RuntimeError("old opening failed")
                return False
            return True

        sink = HudSink(HudWriter(transport), open_terminal=open_terminal)  # type: ignore[arg-type]
        await sink.open("topic", ["codex"])
        old = asyncio.create_task(sink.show_terminal())
        await asyncio.wait_for(entered.wait(), 1)
        await sink.replay()
        await sink.show_terminal()
        release.set()
        if fail:
            with pytest.raises(RuntimeError, match="old opening failed"):
                await old
        else:
            await old
        assert sink.terminal_owns_hud()

    async def test_off_view_stream_and_completion_wait_for_explicit_return(self) -> None:
        transport = HudTransport()
        current = True
        sink = HudSink(HudWriter(transport), brainstorm_view_current=lambda: current)  # type: ignore[arg-type]
        await sink.open("topic", ["codex"])
        current = False
        before = len(transport.sent)
        await sink.chunk("background final answer")
        await sink.close("settled")
        await sink.replay()  # Even an obsolete return must not claim the view.
        assert len(transport.sent) == before
        current = True
        await sink.replay()
        assert isinstance(transport.sent[-1], BrainstormHudView)
        assert transport.sent[-1].state == "settled"
        assert "background final answer" in transport.sent[-1].activity


class TestTheSnapshotQuotesWholeReplies:
    """last_reply feeds cross-surface routing, so it must return the FULL
    turn — the collapsed panel row would send a summary to Hermes."""

    def _snapshot(self):
        from joat.services.sinks import DebateSnapshot

        return DebateSnapshot(
            topic="caching",
            speakers=("CODEX (proposer)", "CLAUDE (critic)"),
            turns=(
                ("CODEX (proposer)", "Cache on session id."),
                ("CLAUDE (critic)", "Key it on the id AND the generation, "
                                    "or a handoff replays stale rounds."),
            ),
        )

    def test_names_resolve_case_insensitively_without_roles(self) -> None:
        snapshot = self._snapshot()
        found = snapshot.last_reply("claude")
        assert found is not None
        assert found[0] == "CLAUDE (critic)"
        assert found[1].startswith("Key it on the id")

    def test_no_name_means_the_newest_turn(self) -> None:
        snapshot = self._snapshot()
        found = snapshot.last_reply(None)
        assert found is not None and found[0] == "CLAUDE (critic)"

    def test_a_failed_turn_is_never_routed(self) -> None:
        """Routing a CLI usage-limit complaint into a work session would
        execute an error message as an instruction."""
        from joat.services.roundtable import FAILURE_MARK
        from joat.services.sinks import DebateSnapshot

        snapshot = DebateSnapshot(turns=(
            ("CODEX (proposer)", "Real answer."),
            ("CLAUDE (critic)",
             f"{FAILURE_MARK} You've hit your usage limit."),
        ))
        found = snapshot.last_reply(None)
        assert found is not None and found[0] == "CODEX (proposer)"
        assert snapshot.last_reply("claude") is None


class TestTheRecordKeepsTheVerificationStory:
    """SESSION_MODEL.md:33 lists VERIFY among the five turn kinds the
    durable record contains. A checked turn gets its VERIFY rows beside it,
    and the turn itself is never rewritten: the check shows, it does not
    re-ask, so a long answer cannot come back shorter.
    """

    ANSWER = 'Cache it.\nSOURCES:\n[1] https://example.invalid :: "q"'

    def _seats(self):
        from joat.services.roundtable import Participant, RoundTable

        asked: list[str] = []

        def make(text):
            async def seat(prompt: str):
                asked.append(prompt)
                yield text

            return seat

        seats = RoundTable([
            Participant("codex", "proposer", make(self.ANSWER)),
            Participant("claude", "critic", make("Disagree.")),
        ])
        return seats, asked

    async def _run(self, sink):
        from joat.verifier.gate import VerifierGate
        from tests.unit.test_session import OneRoundSession

        seats, asked = self._seats()

        async def unfetchable(url: str) -> str:
            return ""

        session = OneRoundSession(
            "caching", seats, sink, gate=VerifierGate(unfetchable, unfetchable),
        )
        await session.run()
        return session, asked

    async def _recorded(self):
        from joat.services.sinks import Broadcast, DebateStateSink
        from tests.unit.test_session import SinkTransport

        state = DebateStateSink()
        session, asked = await self._run(
            Broadcast([HudSink(HudWriter(SinkTransport())), state])
        )
        return session, asked, state.snapshot

    async def test_the_verifier_turn_is_in_the_record(self) -> None:
        _session, _asked, snapshot = await self._recorded()
        labels = [label for label, _text in snapshot.turns]
        assert any("VERIFY" in label.upper() for label in labels), labels

    async def test_a_denied_answer_is_not_re_asked_or_rewritten(self) -> None:
        session, asked, snapshot = await self._recorded()
        labels = [label for label, _text in snapshot.turns]
        assert not any("repair" in label.lower() for label in labels), labels
        assert len(asked) == 2                       # one turn per seat, no repair turn
        joined = " ".join(text for _label, text in snapshot.turns)
        assert "Cache it." in joined, "the original answer was overwritten"

    async def test_the_prompt_context_carries_what_the_seat_said(self) -> None:
        session, _asked, _snapshot = await self._recorded()
        joined = " ".join(text for _label, text in session.transcript())
        assert "Cache it." in joined
