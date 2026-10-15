"""A Brainstorm the wearer leads (2026-10-15).

The wearer: "what is the point of a brainstorm if I cannot lead and see the
full responses so I can participate". Before this, a Brainstorm was a fixed
loop that ran to its own verdict in about 80 seconds, showed each finished
turn as one gist line, and dropped anything said without "JOAT". These pin the
other design: the room stops after every seat, shows the whole turn, and goes
on only when the wearer does.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest

from joat.core.interfaces import ReplyEngine
from joat.core.protocol import BrainstormHudView, HudList, HudRebuild
from joat.services.hud import HudWriter
from joat.services.roundtable import Participant, RoundTable
from joat.services.session import (
    DebateSession,
    SessionManager,
    parse_floor_command,
    turn_reader_text,
)
from joat.services.sinks import Broadcast, HudSink
from tests.unit.test_capabilities import make_router
from tests.unit.test_session import SinkTransport, brainstorm_frames


async def until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


class FloorSink:
    """A sink that records what a led room hands the wearer."""

    def __init__(self) -> None:
        self.floors: list[tuple[str, str, Callable[[], object]]] = []
        self.turns: list[str] = []
        self.progress_calls: list[tuple[int, int, int]] = []
        self.closed: str | None = None

    async def open(self, topic: str, speakers: list[str]) -> None: ...

    async def turn(self, label: str) -> None:
        self.turns.append(label)

    async def chunk(self, text: str) -> None: ...

    async def close(self, verdict: str) -> None:
        self.closed = verdict

    async def floor(self, label: str, text: str, release: Callable[[], object]) -> None:
        self.floors.append((label, text, release))

    async def progress(self, round_no: int, max_rounds: int, calls: int) -> None:
        self.progress_calls.append((round_no, max_rounds, calls))


def seat(name: str, role: str, reply: str, prompts: list[tuple[str, str]]) -> Participant:
    async def stream(prompt: str) -> AsyncIterator[str]:
        prompts.append((name, prompt))
        yield reply

    return Participant(name=name, role=role, stream=stream)


def room(prompts: list[tuple[str, str]]) -> RoundTable:
    return RoundTable([
        seat("codex", "proposer", "GIST: Heat pumps win.\n\nThey move heat instead of making it.", prompts),
        seat("claude", "critic", "GIST: Only above freezing.\n\nCold climates change the math.", prompts),
    ])


class RecordingJudge(ReplyEngine):
    name = "judge"

    def __init__(self, scripted: str = "AGREED: fine.\nOPEN: none.") -> None:
        self.prompts: list[str] = []
        self._scripted = scripted

    async def reply(self, text: str) -> str | None:
        return self._scripted

    async def stream(self, text: str) -> AsyncIterator[str]:
        self.prompts.append(text)
        yield self._scripted if "You are the judge" not in text else "Fine.\nAGREEMENT: 0.90\nBLOCKING: no"


class TestTheRoomStopsOnTheWearer:
    async def test_it_stops_after_every_turn_and_hands_over_the_whole_turn(self) -> None:
        prompts: list[tuple[str, str]] = []
        sink = FloorSink()
        session = DebateSession("heat pumps", room(prompts), sink, None, led=True)
        task = asyncio.create_task(session.run())
        await until(lambda: len(sink.floors) == 1)

        assert session.awaiting_wearer
        assert [name for name, _p in prompts] == ["codex"], "claude must not start yet"
        label, text, _release = sink.floors[0]
        assert label.startswith("CODEX")
        assert "Heat pumps win." in text and "They move heat" in text
        assert "GIST:" not in text

        session.release_floor()
        await until(lambda: len(sink.floors) == 2)
        assert [name for name, _p in prompts] == ["codex", "claude"]
        assert sink.floors[1][0].startswith("CLAUDE")

        session.floor_command("wrap")
        await asyncio.wait_for(task, 2)
        assert sink.closed == "done — wrapped up on your call"
        # An open-ended room never promises "round 1/2".
        assert sink.progress_calls[0][1] == 0

    async def test_nothing_is_scored_or_concluded_for_the_wearer(self) -> None:
        prompts: list[tuple[str, str]] = []
        sink = FloorSink()
        judge = RecordingJudge()
        session = DebateSession("heat pumps", room(prompts), sink, judge, led=True)
        task = asyncio.create_task(session.run())
        for expected in (1, 2, 3):
            await until(lambda n=expected: len(sink.floors) == n)
            session.release_floor()
        await until(lambda: len(sink.floors) == 4)
        assert judge.prompts == [], "no meta-review, no agreement score, no synthesis yet"
        session.floor_command("wrap")
        await asyncio.wait_for(task, 2)
        # The summary comes only because the wearer asked for it.
        assert len(judge.prompts) == 1
        assert judge.prompts[0].startswith("Write the verified synthesis")
        assert not any("You are the judge" in p for p in judge.prompts)

    async def test_a_question_is_the_wearers_move_and_the_next_seat_hears_it(self) -> None:
        prompts: list[tuple[str, str]] = []
        sink = FloorSink()
        session = DebateSession("heat pumps", room(prompts), sink, None, led=True)
        task = asyncio.create_task(session.run())
        await until(lambda: len(sink.floors) == 1)

        assert session.consume_transcript("what about the running cost?") is True
        assert not session.awaiting_wearer
        await until(lambda: len(sink.floors) == 2)

        claude_prompt = next(p for name, p in prompts if name == "claude")
        assert "what about the running cost?" in claude_prompt
        # A led room has no blinders: claude heard what codex just said.
        assert "Heat pumps win." in claude_prompt
        session.cancel()
        await asyncio.wait_for(task, 2)

    async def test_naming_a_seat_is_the_wearers_move_too(self) -> None:
        prompts: list[tuple[str, str]] = []
        sink = FloorSink()
        session = DebateSession("heat pumps", room(prompts), sink, None, led=True)
        task = asyncio.create_task(session.run())
        await until(lambda: len(sink.floors) == 1)
        assert session.direct("claude, respond") == "claude"
        assert not session.awaiting_wearer
        await until(lambda: len(sink.floors) == 2)
        session.cancel()
        await asyncio.wait_for(task, 2)

    async def test_let_them_run_hands_the_room_back_to_the_judge(self) -> None:
        prompts: list[tuple[str, str]] = []
        sink = FloorSink()
        judge = RecordingJudge()
        session = DebateSession("heat pumps", room(prompts), sink, judge, led=True)
        task = asyncio.create_task(session.run())
        await until(lambda: len(sink.floors) == 1)
        session.floor_command("auto")
        await asyncio.wait_for(task, 2)

        assert len(sink.floors) == 1, "no more stops once they let it run"
        assert any("You are the judge" in p for p in judge.prompts)
        assert sink.closed is not None and sink.closed.startswith("done — converged")

    async def test_a_wearer_who_walks_away_leaves_a_saved_room(self) -> None:
        prompts: list[tuple[str, str]] = []
        sink = FloorSink()
        judge = RecordingJudge()
        session = DebateSession("heat pumps", room(prompts), sink, judge, led=True)
        session.FLOOR_IDLE_SECONDS = 0.05
        await asyncio.wait_for(session.run(), 2)
        assert sink.closed == "done — you stepped away; the room is saved"
        assert judge.prompts == [], "nobody asked for a summary"

    async def test_cancel_ends_a_waiting_room_without_another_turn(self) -> None:
        prompts: list[tuple[str, str]] = []
        sink = FloorSink()
        session = DebateSession("heat pumps", room(prompts), sink, None, led=True)
        task = asyncio.create_task(session.run())
        await until(lambda: len(sink.floors) == 1)
        session.cancel()
        await asyncio.wait_for(task, 2)
        assert sink.closed == "cancelled"
        assert [name for name, _p in prompts] == ["codex"]

    async def test_the_autonomous_room_is_unchanged_when_not_led(self) -> None:
        prompts: list[tuple[str, str]] = []
        sink = FloorSink()
        session = DebateSession("heat pumps", room(prompts), sink, RecordingJudge())
        await asyncio.wait_for(session.run(), 2)
        assert sink.floors == [], "a room that is not led never stops"
        assert sink.closed is not None and sink.closed.startswith("done — converged")
        assert sink.progress_calls[0][1] == 2


class TestWhatTheWearerSaysToAWaitingRoom:
    async def _waiting(self) -> tuple[SessionManager, DebateSession, FloorSink, asyncio.Event]:
        transport = SinkTransport()
        prompts: list[tuple[str, str]] = []
        sink = FloorSink()
        hold = asyncio.Event()

        async def held(_prompt: str) -> AsyncIterator[str]:
            await hold.wait()
            yield "GIST: later.\n\nLater."

        table = RoundTable([
            seat("codex", "proposer", "GIST: One.\n\nFirst view.", prompts),
            Participant(name="claude", role="critic", stream=held),
        ])
        session = DebateSession("heat pumps", table, sink, None, led=True)
        manager = SessionManager(HudWriter(transport), lambda _topic: session)
        assert manager.try_start_session(session)
        await until(lambda: len(sink.floors) == 1)
        return manager, session, sink, hold

    async def test_unaddressed_speech_is_theirs_while_the_room_waits(self) -> None:
        manager, session, _sink, hold = await self._waiting()
        try:
            assert manager.handle_transcript("how does that hold up in winter", addressed=None) is True
            assert ("YOU", "how does that hold up in winter") in session._transcript
            assert not session.awaiting_wearer
        finally:
            hold.set()
            await manager.close(timeout_s=0.25)

    async def test_one_stray_word_is_not_a_move(self) -> None:
        manager, session, _sink, hold = await self._waiting()
        try:
            assert manager.handle_transcript("you", addressed=None) is True
            assert session.awaiting_wearer, "a hallucinated word must not spend a turn"
            assert session._transcript[-1][0] != "YOU"
        finally:
            hold.set()
            await manager.close(timeout_s=0.25)

    @pytest.mark.parametrize("spoken", ["continue", "go on", "Okay", "keep going"])
    async def test_continue_needs_no_wake_word(self, spoken: str) -> None:
        manager, session, _sink, hold = await self._waiting()
        try:
            assert manager.handle_transcript(spoken, addressed=None) is True
            assert not session.awaiting_wearer
            assert not any(label == "YOU" for label, _t in session._transcript)
        finally:
            hold.set()
            await manager.close(timeout_s=0.25)

    async def test_wrap_up_is_heard_unaddressed(self) -> None:
        manager, session, sink, hold = await self._waiting()
        try:
            assert manager.handle_transcript("wrap it up", addressed=None) is True
            hold.set()
            await until(lambda: sink.closed is not None)
            assert sink.closed == "done — wrapped up on your call"
        finally:
            hold.set()
            await manager.close(timeout_s=0.25)

    async def test_stop_still_ends_the_room(self) -> None:
        manager, _session, sink, hold = await self._waiting()
        try:
            assert manager.handle_transcript("stop", addressed=None) is True
            await until(lambda: sink.closed == "cancelled")
        finally:
            hold.set()
            await manager.close(timeout_s=0.25)

    async def test_a_seat_that_is_still_talking_keeps_the_old_address_rule(self) -> None:
        # While a seat streams, the room is NOT waiting: overheard speech is
        # still ignored, exactly as before.
        transport = SinkTransport()
        sink = FloorSink()
        hold = asyncio.Event()
        entered = asyncio.Event()

        async def held(_prompt: str) -> AsyncIterator[str]:
            entered.set()
            await hold.wait()
            yield "GIST: x.\n\nX."

        table = RoundTable([Participant(name="codex", role="proposer", stream=held),
                            Participant(name="claude", role="critic", stream=held)])
        session = DebateSession("t", table, sink, None, led=True)
        manager = SessionManager(HudWriter(transport), lambda _topic: session)
        try:
            assert manager.try_start_session(session)
            await asyncio.wait_for(entered.wait(), 1)
            assert manager.handle_transcript("something overheard in the room", addressed=None) is True
            assert session._transcript == []
        finally:
            hold.set()
            await manager.close(timeout_s=0.25)


class TestWhatTheGlassesShow:
    async def test_the_sink_reads_the_turn_in_full_then_takes_the_page_back(self) -> None:
        transport = SinkTransport()
        shown: list[tuple[str, str]] = []
        hidden: list[int] = []

        async def show(label: str, text: str, _release: Callable[[], object]) -> bool:
            shown.append((label, text))
            return True

        sink = HudSink(HudWriter(transport), show_turn=show, hide_turn=lambda: hidden.append(1))
        await sink.open("heat pumps", ["Codex", "Claude"])
        await sink.turn("CODEX (proposer)")
        await sink.chunk("GIST: Heat pumps win.\n\nThey move heat.")
        await sink.floor("CODEX (proposer)", "Heat pumps win.\n\nThey move heat.", lambda: None)
        assert shown == [("CODEX (proposer)", "Heat pumps win.\n\nThey move heat.")]
        assert hidden == []

        sent_before = len(transport.sent)
        await sink.turn("CLAUDE (critic)")
        assert hidden == [1]
        resumed = transport.sent[sent_before:]
        # The room page is claimed again BEFORE the next frame is painted.
        assert isinstance(resumed[0], HudRebuild)
        assert any(isinstance(m, BrainstormHudView) for m in resumed[1:])

    async def test_coming_back_to_a_waiting_room_shows_the_turn_not_the_scoreboard(self) -> None:
        transport = SinkTransport()
        shown: list[str] = []

        async def show(label: str, _text: str, _release: Callable[[], object]) -> bool:
            shown.append(label)
            return True

        sink = HudSink(HudWriter(transport), show_turn=show)
        await sink.open("heat pumps", ["Codex", "Claude"])
        await sink.turn("CODEX (proposer)")
        await sink.chunk("Heat pumps win.")
        await sink.floor("CODEX (proposer)", "Heat pumps win.", lambda: None)
        frames = len(brainstorm_frames(transport))
        await sink.replay()
        assert shown == ["CODEX (proposer)", "CODEX (proposer)"]
        assert len(brainstorm_frames(transport)) == frames

    async def test_an_open_ended_room_does_not_promise_a_round_count(self) -> None:
        transport = SinkTransport()
        sink = HudSink(HudWriter(transport))
        await sink.open("t", ["Codex", "Claude"])
        await sink.progress(2, 0, 5)
        await sink.turn("CODEX (proposer)")
        assert brainstorm_frames(transport)[-1].active_speaker.endswith("round 2 · 5 calls")
        await sink.progress(1, 2, 3)
        await sink.turn("CLAUDE (critic)")
        assert brainstorm_frames(transport)[-1].active_speaker.endswith("round 1/2 · 3 calls")

    async def test_every_sink_hears_the_floor_and_a_dead_one_changes_nothing(self) -> None:
        good = FloorSink()

        class Dead:
            async def floor(self, *_args: object) -> None:
                raise RuntimeError("surface is gone")

        class NoFloor: ...

        await Broadcast([Dead(), NoFloor(), good]).floor("CODEX", "text", lambda: None)  # type: ignore[list-item]
        assert [label for label, _t, _r in good.floors] == ["CODEX"]

    async def test_the_router_reads_a_turn_whole_and_a_tap_continues_the_room(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, transport, _status = make_router(tmp_path)
        router._active_capability = "BRAINSTORM"
        released: list[int] = []

        assert await router.show_brainstorm_turn(
            "CODEX (proposer)", "Heat pumps win.\n\nThey move heat.", lambda: released.append(1),
        ) is True
        shown = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert shown.title == "BRAINSTORM · TURN"
        assert shown.body == "CODEX (proposer)\n\nHeat pumps win.\n\nThey move heat."
        assert shown.selectable is True
        assert "tap: continue" in shown.hint and "speak: ask" in shown.hint

        reader = router._note_reader
        assert reader is not None
        await router._tap_reader(reader)
        assert released == [1]
        assert router._note_reader is None, "a later tap on the room page must not 'continue' again"

    async def test_nothing_is_put_up_when_the_wearer_is_somewhere_else(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, transport, _status = make_router(tmp_path)
        router._active_capability = "MESSAGES"
        assert await router.show_brainstorm_turn("CODEX", "text", lambda: None) is False
        assert not [m for m in transport.sent if isinstance(m, HudList)]


class TestTheWordsOnTheLens:
    def test_the_gist_leads_without_its_label_and_sources_become_a_line(self) -> None:
        said = (
            "GIST: Heat pumps win.\n\nThey move heat instead of making it.\n\n"
            'SOURCES:\n[1] https://example.org/a :: "exact quote"'
        )
        assert turn_reader_text(said, "sources check out (1 citation(s))") == (
            "Heat pumps win.\n\nThey move heat instead of making it.\n\n"
            "Sources: 1 cited, and they check out."
        )
        assert turn_reader_text(said, "sources don't hold up (1 citation(s))").endswith(
            "Sources: 1 cited, and they don't hold up."
        )
        assert turn_reader_text(said, "").endswith("Sources: 1 cited.")

    def test_a_turn_without_sources_is_just_its_words(self) -> None:
        assert turn_reader_text("GIST: Short.\n\nBody.", "") == "Short.\n\nBody."
        assert turn_reader_text("No gist at all.", "") == "No gist at all."

    @pytest.mark.parametrize(("spoken", "command"), [
        ("continue", "continue"), ("Go on.", "continue"), ("JOAT, keep going", "continue"),
        ("okay", "continue"), ("what's next", None), ("next", "continue"),
        ("wrap up", "wrap"), ("Wrap it up!", "wrap"), ("hey joat summarize", "wrap"),
        ("let them run", "auto"), ("Let them go", "auto"),
        ("that next sentence is wrong", None), ("what about cost", None), ("", None),
    ])
    def test_the_words_a_waiting_room_listens_for(self, spoken: str, command: str | None) -> None:
        assert parse_floor_command(spoken) == command
