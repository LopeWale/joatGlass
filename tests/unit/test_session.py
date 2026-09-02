"""SessionManager + DebateSession tests with fake agents and HUD.

The debate is driven by a RoundTable of N participants, so these run it
through the real HudSink — what the assertions read is what the glasses
would have been sent.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from joat.core.interfaces import GlassesTransport, ReplyEngine
from joat.core.protocol import AgentTerminalHudView, AssistantOverlay, BrainstormAction, BrainstormHudView, ContainerId, DownlinkMessage, HudText
from joat.channels.cards import Card, CardLog, Urgency
from joat.services.capabilities import CapabilityRouter
from joat.services.hud import HudWriter
from joat.services.roundtable import Participant, RoundTable
from joat.services.session import DebateSession, Session, SessionManager
from joat.services.sinks import HudSink
from joat.services.status import StatusPublisher


class SinkTransport(GlassesTransport):
    def __init__(self) -> None:
        self.sent: list[DownlinkMessage] = []

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send(self, message: DownlinkMessage) -> None:
        self.sent.append(message)

    @property
    def connected(self) -> bool:
        return True

    def on_audio(self, callback: object) -> None: ...  # type: ignore[override]
    def on_input(self, callback: object) -> None: ...  # type: ignore[override]
    def on_device(self, callback: object) -> None: ...  # type: ignore[override]


def agent(chunks: list[str]):
    async def stream(prompt: str) -> AsyncIterator[str]:
        for c in chunks:
            yield c
    return stream


class OneShotJudge(ReplyEngine):
    async def reply(self, text: str) -> str | None:
        return "Verdict: proposal stands."


def table(*seats) -> RoundTable:
    """(name, role, stream) triples → a seated round table."""
    return RoundTable([Participant(name=n, role=r, stream=st) for n, r, st in seats])


def pair(propose, critique) -> RoundTable:
    return table(("codex", "proposer", propose), ("claude", "critic", critique))


def debate(transport: SinkTransport, seats: RoundTable, judge, **kw) -> DebateSession:
    return DebateSession(
        kw.pop("topic", "topic"), seats, HudSink(HudWriter(transport)), judge, **kw
    )


def hud_texts(transport: SinkTransport) -> list[tuple[str, bool, int]]:
    """Project the real Brainstorm frames into an ordered visible transcript.

    The production glasses surface is no longer a rolling HudText pane.  Each
    paint is a complete, typed Brainstorm frame, so expose its speaker and
    activity in test order rather than reintroducing a text-only fallback.
    """
    return [
        (f"{message.active_speaker}\n{message.activity}", False, int(ContainerId.MAIN))
        for message in transport.sent
        if isinstance(message, BrainstormHudView)
    ]


def brainstorm_frames(transport: SinkTransport) -> list[BrainstormHudView]:
    return [
        message for message in transport.sent if isinstance(message, BrainstormHudView)
    ]


class TestDebateSession:
    async def test_brainstorm_starts_with_a_production_frame(self) -> None:
        transport = SinkTransport()

        await debate(
            transport,
            pair(agent(["one"]), agent(["two"])),
            None,
            topic="plan the week",
        ).run()

        frames = brainstorm_frames(transport)
        assert isinstance(frames[0], BrainstormHudView)
        assert frames[0].state == "running"
        assert frames[0].topic == "plan the week"
        assert frames[0].collaborators == ["codex", "claude"]
        assert frames[-1].state == "settled"
        assert not any(isinstance(message, HudText) for message in transport.sent)

    async def test_publishes_opening_turn_and_closing_phases(self) -> None:
        transport = SinkTransport()
        phases: list[str] = []

        await debate(
            transport,
            pair(agent(["one"]), agent(["two"])),
            None,
            on_phase=phases.append,
        ).run()

        assert phases == ["opening", "independent", "closing"]

    async def test_remote_collaborator_exception_becomes_a_visible_blocked_frame(self) -> None:
        transport = SinkTransport()

        async def broken(_prompt: str) -> AsyncIterator[str]:
            raise ConnectionError("remote closed")
            yield "unreachable"

        await debate(transport, pair(broken, agent(["never"])), None).run()

        frames = brainstorm_frames(transport)
        assert frames[-1].state == "blocked"
        assert "collaborator disconnected" in frames[-1].activity

    async def test_turns_stream_in_order_visibly(self) -> None:
        transport = SinkTransport()
        session = debate(
            transport,
            pair(agent(["Use spaces. ", "Simple."]), agent(["Weak claim: simplicity."])),
            OneShotJudge(),
            topic="tabs or spaces",
        )
        await session.run()
        texts = [t for t, _a, c in hud_texts(transport) if c == int(ContainerId.MAIN)]
        joined = "".join(texts)
        # order: each speaker's header, then their words, then the judge
        assert joined.index("CODEX") < joined.index("Use spaces.")
        assert joined.index("Use spaces.") < joined.index("CLAUDE")
        assert joined.index("CLAUDE") < joined.index("Weak claim")
        assert joined.index("Weak claim") < joined.index("JUDGE")
        assert "Verdict: proposal stands." in joined
        # streaming means the two proposer chunks each reached the production
        # frame sequence before the next collaborator received the floor.
        activities = [frame.activity for frame in brainstorm_frames(transport)]
        assert any("Use spaces. " in activity for activity in activities)
        assert any("Simple." in activity for activity in activities)

    async def test_a_third_seat_speaks_after_the_other_two(self) -> None:
        """OpenClaw's whole integration requirement, as a test: a third voice
        is a seat at the table, not a second debate implementation."""
        transport = SinkTransport()
        seats = table(
            ("codex", "proposer", agent(["one"])),
            ("claude", "critic", agent(["two"])),
            ("openclaw", "third opinion", agent(["three"])),
        )
        await debate(transport, seats, None).run()
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        assert joined.index("one") < joined.index("two") < joined.index("three")
        assert "OPENCLAW" in joined

    async def test_round_one_is_blind_and_round_two_sees_everything(self) -> None:
        """The starter design's first property: independent reviews. Round 1
        withholds the transcript, so two seats give uncontaminated views —
        an earlier build let seat 2 read seat 1's answer in round 1, which
        is one view plus an echo. From round 2, cross-review: everyone sees
        everything."""
        transport = SinkTransport()
        seen: list[str] = []

        def recorder(reply: str):
            async def stream(prompt: str) -> AsyncIterator[str]:
                seen.append(prompt)
                yield reply
            return stream

        seats = pair(recorder("gas is cheaper"), recorder("only near a pipeline"))
        judge = ScriptedJudge([
            "Not aligned.\nAGREEMENT: 0.20\nBLOCKING: yes",
            "Aligned now.\nAGREEMENT: 0.95\nBLOCKING: no",
        ])
        await debate(transport, seats, judge, topic="heat pumps",
                     max_rounds=2).run()
        # Round 1 (seen[0], seen[1]): both blind.
        assert "gas is cheaper" not in seen[0]
        assert "gas is cheaper" not in seen[1]          # THE independence claim
        assert "only near a pipeline" not in seen[1]
        # Round 2 (seen[2], seen[3]): cross-review sees both round-1 turns.
        assert "gas is cheaper" in seen[2] and "only near a pipeline" in seen[2]
        assert "gas is cheaper" in seen[3] and "only near a pipeline" in seen[3]

    async def test_synthesis_streams_after_convergence_and_lands_in_transcript(self) -> None:
        """B8: JOAT produces a VISIBLE verified synthesis — the product the
        wearer walks away with, not just a one-line outcome status."""
        transport = SinkTransport()
        judge = ScriptedJudge(["Fine.\nAGREEMENT: 0.95\nBLOCKING: no"])
        session = debate(transport, pair(agent(["view A"]), agent(["view B"])),
                         judge)
        await session.run()
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        assert "AGREED: scripted accord." in joined
        assert "OPEN: nothing." in joined
        # The synthesis is a transcript turn, so the reopenable panel holds
        # the product, not only the path to it.
        assert ("JOAT (synthesis)", "AGREED: scripted accord.\nOPEN: nothing.") \
            in session._transcript

    async def test_no_judge_means_no_synthesis_is_faked(self) -> None:
        transport = SinkTransport()
        await debate(transport, pair(agent(["a"]), agent(["b"])), None).run()
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        assert "synthesis" not in joined.lower()

    async def test_phases_follow_the_starter_design(self) -> None:
        """independent → meta-review (per round, judge) → cross-review →
        … → synthesis. The status projection is how the HUD will later show
        which stage the room is in."""
        transport = SinkTransport()
        phases: list[str] = []
        judge = ScriptedJudge([
            "No.\nAGREEMENT: 0.10\nBLOCKING: yes",
            "Yes.\nAGREEMENT: 0.95\nBLOCKING: no",
        ])
        await debate(transport, pair(agent(["a1"]), agent(["b1"])), judge,
                     max_rounds=2, on_phase=phases.append).run()
        assert phases == ["opening", "independent", "meta_review",
                          "cross_review", "meta_review", "synthesis", "closing"]

    async def test_the_wearer_can_hand_the_floor_to_a_named_seat(self) -> None:
        transport = SinkTransport()
        seats = table(
            ("codex", "proposer", agent(["FIRSTWORD"])),
            ("claude", "critic", agent(["SECONDWORD"])),
            ("openclaw", "third opinion", agent(["THIRDWORD"])),
        )
        session = debate(transport, seats, None)
        assert session.direct("openclaw, your turn") == "openclaw"
        assert session.direct("what did codex mean by that") is None  # not a command
        await session.run()
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        # The directed seat OPENED, and the rotation carried on from there —
        # a debate must not stall waiting for the next instruction.
        assert joined.index("THIRDWORD") < joined.index("FIRSTWORD")
        assert joined.index("FIRSTWORD") < joined.index("SECONDWORD")

    async def test_a_failed_seat_is_shown_but_never_quoted(self) -> None:
        """Live on this machine: `claude -p` exits 1 and prints "You're out of
        usage credits" on stdout. Recorded as a position, the next speaker
        argues with a billing notice and the judge scores it."""
        transport = SinkTransport()
        seen: list[str] = []

        def recorder(reply: str):
            async def stream(prompt: str) -> AsyncIterator[str]:
                seen.append(prompt)
                yield reply
            return stream

        seats = table(
            ("codex", "proposer", recorder("Cache the model in RAM.")),
            ("claude", "critic",
             recorder("You're out of usage credits.\n"
                      "[!] claude could not answer: exited 1 — see the log")),
            ("openclaw", "third opinion", recorder("Thermals decide it.")),
        )
        judge = ScriptedJudge(["No.\nAGREEMENT: 0.10\nBLOCKING: yes"])
        await debate(transport, seats, judge, max_rounds=2).run()
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        assert "out of usage credits" in joined       # the wearer SEES it
        # Round 2 is cross-review (round 1 is blind for everyone), so the
        # exclusion is visible there: real turns circulate, failures don't.
        round_two = seen[3:]
        assert round_two, "expected a second round"
        assert all("Cache the model in RAM." in p for p in round_two)
        assert all("usage credits" not in p for p in round_two)

    async def test_one_voice_and_a_failure_is_not_a_debate(self) -> None:
        transport = SinkTransport()
        seats = pair(
            agent(["A position."]),
            agent(["[!] claude could not answer: no answer within 240s"]),
        )
        await debate(transport, seats, OneShotJudge()).run()
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        assert "insufficient collaborators — 1 of 2 spoke (claude silent)" in joined
        assert "JUDGE" not in joined  # nothing to score, so no verdict is faked

    async def test_cancel_stops_midstream(self) -> None:
        transport = SinkTransport()
        holder: dict[str, DebateSession] = {}

        async def cancelling(prompt: str) -> AsyncIterator[str]:
            yield "first"
            holder["s"].cancel()
            yield "second"

        session = debate(transport, pair(cancelling, agent(["never"])), None)
        holder["s"] = session
        await session.run()
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        assert "never" not in joined
        assert "cancelled" in joined


class TestSessionManager:
    async def test_typed_external_session_cannot_replace_the_active_wearer_session(self) -> None:
        entered = asyncio.Event()

        class HeldSession(Session):
            async def run(self) -> None:
                entered.set()
                await asyncio.Event().wait()

            def cancel(self) -> None:
                return None

        manager = SessionManager(HudWriter(SinkTransport()), lambda _topic: None)
        first = HeldSession()
        assert manager.try_start_session(first)
        await entered.wait()
        assert not manager.try_start_session(HeldSession())
        assert manager.active_session is first
        await manager.close(timeout_s=0.1)

    async def test_glasses_begin_action_arms_and_consumes_the_next_topic(self) -> None:
        transport = SinkTransport()
        capture = BrainstormHudView(state="topic_capture", topic="Speak your question", collaborators=["Codex", "Claude", "Hermes"], activity="Your next spoken phrase starts this Brainstorm.")
        entered = asyncio.Event()
        started: list[str] = []

        class HeldSession(Session):
            async def run(self) -> None:
                entered.set()
                await asyncio.Event().wait()

            def cancel(self) -> None:
                return None

        manager = SessionManager(
            HudWriter(transport),
            lambda topic: started.append(topic) or HeldSession(),
            topic_capture_view=lambda armed: capture if armed else BrainstormHudView(state="ready", topic="Ready for a question", collaborators=capture.collaborators, activity="Tap to choose a question, then speak."),
        )

        manager.handle_brainstorm_action(BrainstormAction("begin_topic_capture"))
        await asyncio.sleep(0)

        assert not manager.active
        assert brainstorm_frames(transport) == [capture]
        assert not any(isinstance(message, HudText) for message in transport.sent)
        assert manager.handle_transcript(
            "compare a hosted Mac brain with a local one",
            capture_source="glasses",
        )
        await entered.wait()
        assert started == ["compare a hosted Mac brain with a local one"]
        await manager.close(timeout_s=0.1)

    async def test_an_unavailable_brainstorm_starts_nothing_by_voice_or_tap(self) -> None:
        """Neither a spoken command nor a tap may silently use local seats."""
        transport = SinkTransport()
        # A BRAINSTORM frame, naming the real blocker. This fixture used to
        # be a terminal recovery card reading "Hermes is required for
        # Brainstorm" — copy the next test exists to disprove. The subject
        # here is brainstorm_available=False (a missing CLI seat), which has
        # nothing to do with Hermes and must never route the wearer there.
        unavailable = BrainstormHudView(
            state="unavailable",
            topic="Brainstorm",
            collaborators=[],
            activity="codex CLI missing on the Mac — install to enable.",
        )
        factory_topics: list[str] = []
        manager = SessionManager(
            HudWriter(transport),
            lambda topic: factory_topics.append(topic) or None,
            topic_capture_view=lambda _armed: unavailable,
            brainstorm_available=lambda: False,
        )

        assert manager.handle_transcript(
            "brainstorm compare home brain options",
            capture_source="glasses",
        )
        await asyncio.sleep(0)
        assert factory_topics == []
        assert not manager.topic_capture_armed
        assert brainstorm_frames(transport) == [unavailable]

        manager.handle_brainstorm_action(BrainstormAction("begin_topic_capture"))
        await asyncio.sleep(0)
        assert factory_topics == []
        assert not manager.topic_capture_armed
        assert brainstorm_frames(transport) == [unavailable, unavailable]

    async def test_brainstorm_menu_opens_regardless_of_hermes(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """Brainstorm's seats are the Codex and Claude CLIs.

        This test previously pinned the opposite: opening the row with the
        Hermes terminal unready projected a recovery card reading 'Hermes
        is required for Brainstorm. Tap to retry.' Per docs/BRAINSTORM.md
        and the wearer's ruling, Hermes/OpenClaw are Agent Terminal workers
        that neither participate in a Brainstorm nor gate one — so the row
        must open, and must not name a third party as a collaborator.
        """
        transport = SinkTransport()
        status = StatusPublisher(path=tmp_path / "status.json")
        status.set_agent_terminal(state="approval_required", reason="write_scope_required")
        router = CapabilityRouter(
            hud=HudWriter(transport),
            transport=transport,
            status=status,
            messages=None,
            cards=CardLog(),
        )

        await router.open("Brainstorm")

        [frame] = brainstorm_frames(transport)
        assert "Hermes" not in frame.activity
        assert frame.collaborators == ["Codex", "Claude"]

        # A terminal mid-response is likewise irrelevant to a Brainstorm.
        status.set_agent_terminal(state="running", reason="")
        await router.open("Brainstorm")

        running = brainstorm_frames(transport)[-1]
        assert isinstance(running, BrainstormHudView)
        assert "Hermes" not in running.activity

    async def test_topic_capture_deduplicates_taps_and_clears_on_cancel_or_reconnect(self) -> None:
        transport = SinkTransport()
        capture = BrainstormHudView(state="topic_capture", topic="Speak your question", collaborators=["Codex", "Claude", "Hermes"], activity="Your next spoken phrase starts this Brainstorm.")
        ready = BrainstormHudView(state="ready", topic="Ready for a question", collaborators=capture.collaborators, activity="Tap to choose a question, then speak.")
        manager = SessionManager(
            HudWriter(transport),
            lambda _topic: None,
            topic_capture_view=lambda armed: capture if armed else ready,
        )

        manager.handle_brainstorm_action(BrainstormAction("begin_topic_capture"))
        manager.handle_brainstorm_action(BrainstormAction("begin_topic_capture"))
        await asyncio.sleep(0)
        assert brainstorm_frames(transport) == [capture]

        manager.handle_brainstorm_action(BrainstormAction("cancel"))
        await asyncio.sleep(0)
        assert brainstorm_frames(transport)[-1] == ready
        assert manager.handle_transcript("ordinary assistant question") is False

        manager.handle_brainstorm_action(BrainstormAction("begin_topic_capture"))
        manager.clear_topic_capture()
        assert manager.handle_transcript("ordinary assistant question") is False

    async def test_topic_capture_consumes_a_phrase_when_collaborators_cannot_start(self) -> None:
        transport = SinkTransport()
        ready = BrainstormHudView(state="ready", topic="Ready for a question", collaborators=["Codex", "Claude", "Hermes"], activity="Tap to choose a question, then speak.")
        manager = SessionManager(
            HudWriter(transport),
            lambda _topic: None,
            topic_capture_view=lambda armed: BrainstormHudView(state="topic_capture", topic="Speak your question", collaborators=ready.collaborators, activity="Your next spoken phrase starts this Brainstorm.") if armed else ready,
        )

        manager.handle_brainstorm_action(BrainstormAction("begin_topic_capture"))
        await asyncio.sleep(0)
        assert manager.handle_transcript(
            "should the home brain use a mini?", capture_source="glasses",
        ) is True
        await asyncio.sleep(0)
        assert brainstorm_frames(transport)[-1] == ready

    async def test_topic_capture_keeps_its_promise_for_glasses_audio_only(self) -> None:
        transport = SinkTransport()
        capture = BrainstormHudView(state="topic_capture", topic="Speak your question", collaborators=["Codex", "Claude", "Hermes"], activity="Your next spoken phrase starts this Brainstorm.")
        started: list[str] = []
        manager = SessionManager(
            HudWriter(transport),
            lambda topic: started.append(topic) or None,
            topic_capture_view=lambda _armed: capture,
        )

        manager.handle_brainstorm_action(BrainstormAction("begin_topic_capture"))
        await asyncio.sleep(0)
        assert manager.handle_transcript("phone fallback must not start it", capture_source="phone")
        await asyncio.sleep(0)
        assert started == []
        assert manager.topic_capture_armed
        assert brainstorm_frames(transport)[-1] == capture

    async def test_glasses_cancel_action_interrupts_the_active_session(self) -> None:
        entered = asyncio.Event()
        exited = asyncio.Event()

        class BlockedSession(Session):
            async def run(self) -> None:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    exited.set()

            def cancel(self) -> None:
                return None

        manager = SessionManager(HudWriter(SinkTransport()), lambda _topic: BlockedSession())
        assert manager.handle_transcript("brainstorm a launch")
        await entered.wait()

        manager.handle_brainstorm_action(BrainstormAction("cancel"))
        await exited.wait()
        await asyncio.sleep(0)

        assert not manager.active

    async def test_glasses_cancel_is_offered_to_the_typed_session_before_fallback(
        self,
    ) -> None:
        entered = asyncio.Event()
        handled = asyncio.Event()
        release = asyncio.Event()
        fallback_cancel_calls = 0

        class OwnedCancelSession(Session):
            async def run(self) -> None:
                entered.set()
                await release.wait()

            def cancel(self) -> None:
                nonlocal fallback_cancel_calls
                fallback_cancel_calls += 1

            async def handle_brainstorm_action(self, action: BrainstormAction) -> bool:
                if action.action != "cancel":
                    return False
                handled.set()
                release.set()
                return True

        manager = SessionManager(
            HudWriter(SinkTransport()),
            lambda _topic: OwnedCancelSession(),
        )
        assert manager.handle_transcript("brainstorm a launch")
        await entered.wait()

        manager.handle_brainstorm_action(BrainstormAction("cancel"))
        await asyncio.wait_for(handled.wait(), timeout=0.1)
        while manager.active:
            await asyncio.sleep(0)

        assert fallback_cancel_calls == 0

    async def test_glasses_show_terminal_action_is_owned_by_the_active_session(self) -> None:
        handled: list[str] = []
        active = asyncio.Event()

        class ActionSession(Session):
            async def run(self) -> None:
                active.set()
                await asyncio.Event().wait()

            def cancel(self) -> None:
                return None

            async def handle_brainstorm_action(self, action: BrainstormAction) -> bool:
                handled.append(action.action)
                return True

        manager = SessionManager(HudWriter(SinkTransport()), lambda _topic: ActionSession())
        assert manager.handle_transcript("brainstorm a launch")
        await active.wait()

        manager.handle_brainstorm_action(BrainstormAction("show_terminal"))
        await asyncio.sleep(0)

        assert handled == ["show_terminal"]
        await manager.close(timeout_s=0.01)

    async def test_glasses_retry_action_uses_only_the_registered_terminal_recovery(self) -> None:
        retried = asyncio.Event()

        async def retry() -> None:
            retried.set()

        manager = SessionManager(
            HudWriter(SinkTransport()),
            lambda _topic: None,
            terminal_retry=retry,
        )
        manager.handle_brainstorm_action(BrainstormAction("retry_terminal"))
        await retried.wait()

    async def test_registered_session_starter_and_active_correction_share_one_owner(self) -> None:
        received: list[str] = []
        states: list[str] = []
        running = asyncio.Event()

        class NavigationSession(Session):
            async def run(self) -> None:
                running.set()
                await asyncio.Event().wait()

            def cancel(self) -> None:
                return None

            def consume_transcript(self, text: str) -> bool:
                received.append(text)
                return True

        manager = SessionManager(
            HudWriter(SinkTransport()),
            lambda _topic: None,
            starters=[
                lambda text: NavigationSession()
                if text.lower().startswith("navigate to ") else None,
            ],
            on_state=states.append,
        )
        assert manager.handle_transcript("navigate to Cajun Corner") is True
        await running.wait()
        assert manager.handle_transcript("the street number is 2301") is True
        assert received == ["the street number is 2301"]
        assert states == []
        await manager.close(timeout_s=0.01)

    async def test_publishes_running_and_settled_lifecycle(self) -> None:
        states: list[str] = []

        class FastSession(Session):
            async def run(self) -> None: ...

            def cancel(self) -> None: ...

        manager = SessionManager(
            HudWriter(SinkTransport()),
            lambda _topic: FastSession(),
            on_state=states.append,
        )

        assert manager.handle_transcript("brainstorm a launch")
        await asyncio.sleep(0)
        assert states == ["running", "settled"]

    async def test_crashed_session_publishes_failed_not_settled(self) -> None:
        states: list[str] = []

        class CrashedSession(Session):
            async def run(self) -> None:
                raise RuntimeError("boom")

            def cancel(self) -> None: ...

        manager = SessionManager(
            HudWriter(SinkTransport()),
            lambda _topic: CrashedSession(),
            on_state=states.append,
        )

        assert manager.handle_transcript("brainstorm a launch")
        await asyncio.sleep(0)
        assert states == ["running", "failed"]

    async def test_close_cancels_and_awaits_active_session(self) -> None:
        states: list[str] = []

        class ActiveSession(Session):
            def __init__(self) -> None:
                self.cancelled = asyncio.Event()

            async def run(self) -> None:
                await self.cancelled.wait()

            def cancel(self) -> None:
                self.cancelled.set()

            @property
            def completion_state(self) -> str:
                return "cancelled"

        manager = SessionManager(
            HudWriter(SinkTransport()),
            lambda _topic: ActiveSession(),
            on_state=states.append,
        )

        assert manager.handle_transcript("brainstorm a launch")
        await asyncio.sleep(0)
        await manager.close()

        assert not manager.active
        assert states == ["running", "cancelled"]

    async def test_spoken_stop_interrupts_a_session_blocked_in_remote_io(self) -> None:
        entered = asyncio.Event()
        exited = asyncio.Event()

        class BlockedSession(Session):
            async def run(self) -> None:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    exited.set()

            def cancel(self) -> None:
                return None

        manager = SessionManager(
            HudWriter(SinkTransport()),
            lambda _topic: BlockedSession(),
        )

        assert manager.handle_transcript("brainstorm a launch")
        await entered.wait()
        assert manager.handle_transcript("stop")
        async with asyncio.timeout(0.1):
            await exited.wait()
            while manager.active:
                await asyncio.sleep(0)

        assert not manager.active

    async def test_incoming_card_is_typed_as_notification_not_assistant_reply(self) -> None:
        transport = SinkTransport()
        manager = SessionManager(HudWriter(transport), lambda _topic: None)

        manager.notify(Card(source="Dele", channel="Messages", summary="On my way"))
        await asyncio.sleep(0)

        cards = [m for m in transport.sent if isinstance(m, HudText)]
        assert len(cards) == 1
        assert cards[0].content_kind == "notification"

    async def test_brightness_comes_from_urgency_not_a_rendered_prefix(self) -> None:
        """Triage already decided urgency; re-deriving it by sniffing '!!'
        in the rendered text threw that decision away."""
        transport = SinkTransport()
        manager = SessionManager(HudWriter(transport), lambda _topic: None)

        manager.notify(Card(source="Dele", channel="Messages", summary="Call me",
                            urgency=Urgency.URGENT))
        await asyncio.sleep(0)
        manager.notify(Card(source="News", channel="News", summary="Digest ready",
                            urgency=Urgency.NOTIFY))
        await asyncio.sleep(0)

        cards = [m for m in transport.sent if isinstance(m, HudText)]
        assert [c.text_color for c in cards] == [4, 3]

    async def test_notify_and_wait_completes_only_after_hud_write(self) -> None:
        gate = asyncio.Event()

        class GatedTransport(SinkTransport):
            async def send(self, message: DownlinkMessage) -> None:
                await gate.wait()
                await super().send(message)

        transport = GatedTransport()
        manager = SessionManager(HudWriter(transport), lambda _topic: None)
        delivery = asyncio.create_task(manager.notify_and_wait(
            Card(source="Ada", channel="Messages", summary="Hi"),
        ))
        await asyncio.sleep(0)

        assert not delivery.done()
        assert transport.sent == []
        gate.set()
        await delivery
        assert len(transport.sent) == 1

    async def test_history_records_each_card_including_a_drained_batch(self) -> None:
        """The card_log write runs in production but was never exercised.

        A drained batch paints ONE frame, so a log keyed off paints would
        collapse several events into one unreadable entry; history keeps them
        separate.
        """
        transport = SinkTransport()
        card_log = CardLog()

        class Held(Session):
            def __init__(self) -> None:
                self.gate = asyncio.Event()

            async def run(self) -> None:
                await self.gate.wait()

            def cancel(self) -> None:
                self.gate.set()

        held = Held()
        manager = SessionManager(
            HudWriter(transport), lambda _topic: held, card_log=card_log,
        )

        manager.notify(Card(source="Ada", channel="Messages", summary="first"))
        await asyncio.sleep(0)
        assert [c.source for c in card_log.recent(5)] == ["Ada"]

        assert manager.handle_transcript("debate something") is True
        await asyncio.sleep(0)
        manager.notify(Card(source="Dele", channel="Messages", summary="queued one"))
        manager.notify(Card(source="Marcus", channel="Messages", summary="queued two"))
        held.gate.set()
        for _ in range(6):
            await asyncio.sleep(0)

        # One painted frame for the batch, but three distinct history entries.
        assert [c.source for c in card_log.recent(5)] == ["Ada", "Dele", "Marcus"]

    async def test_an_abandoned_queued_card_is_withdrawn_not_painted_later(self) -> None:
        """A caller that gives up (bounded timeout, shutdown) must not have
        its card surface minutes later when the session drains.

        Leaving the entry queued paints an announcement the caller already
        recorded as undelivered — so it is shown AND retried, i.e. twice.
        """
        transport = SinkTransport()

        class Held(Session):
            def __init__(self) -> None:
                self.gate = asyncio.Event()

            async def run(self) -> None:
                await self.gate.wait()

            def cancel(self) -> None:
                self.gate.set()

        held = Held()
        manager = SessionManager(HudWriter(transport), lambda _topic: held)
        assert manager.handle_transcript("debate something") is True
        await asyncio.sleep(0)

        card = Card(source="Design review", channel="Calendar", summary="Starts 16:30")
        delivery = asyncio.create_task(manager.notify_and_wait(card))
        await asyncio.sleep(0)
        assert not delivery.done()

        delivery.cancel()
        try:
            await delivery
        except asyncio.CancelledError:
            pass

        held.gate.set()
        for _ in range(5):
            await asyncio.sleep(0)

        painted = [m for m in transport.sent if isinstance(m, HudText)]
        assert not any("Design review" in m.text for m in painted)

    async def test_trigger_starts_and_consumes(self) -> None:
        transport = SinkTransport()
        started: list[str] = []

        class FastSession(Session):
            def __init__(self, topic: str) -> None:
                self.topic = topic
                self.gate = asyncio.Event()

            async def run(self) -> None:
                started.append(self.topic)
                await self.gate.wait()

            def cancel(self) -> None:
                self.gate.set()

        made: list[FastSession] = []

        def factory(topic: str) -> Session:
            s = FastSession(topic)
            made.append(s)
            return s

        mgr = SessionManager(HudWriter(transport), factory)
        assert mgr.handle_transcript("hello there") is False
        assert mgr.handle_transcript("Debate tabs versus spaces.") is True
        await asyncio.sleep(0)
        assert started == ["tabs versus spaces"]
        assert mgr.active
        # transcripts consumed while active; 'stop' cancels
        assert mgr.handle_transcript("what is the weather") is True
        assert mgr.handle_transcript("stop") is True
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not mgr.active
        assert mgr.handle_transcript("hello again") is False

    async def test_a_wedged_session_releases_the_wearer_instead_of_trapping_them(
        self,
    ) -> None:
        """While a session is active EVERY transcript is consumed, so a task
        that ignores cancellation left the wearer with no working commands at
        all. Refuse to stay hostage to it."""
        from joat.services import session as session_module

        transport = SinkTransport()

        class Wedged(Session):
            """Ignores the first cancellations, like a subprocess stuck in
            uninterruptible I/O. Releasable so the test can clean up."""

            def __init__(self) -> None:
                self.release = asyncio.Event()

            async def run(self) -> None:
                while not self.release.is_set():
                    try:
                        await asyncio.wait_for(self.release.wait(), 0.005)
                    except (TimeoutError, asyncio.CancelledError):
                        continue

            def cancel(self) -> None:
                return None

        wedged = Wedged()
        mgr = SessionManager(HudWriter(transport), lambda _topic: wedged)
        assert mgr.handle_transcript("brainstorm something") is True
        await asyncio.sleep(0)
        assert mgr.active

        original = session_module.CANCEL_GRACE_S
        session_module.CANCEL_GRACE_S = 0.01
        try:
            assert mgr.handle_transcript("stop") is True
            async with asyncio.timeout(1.0):
                while mgr.active:
                    await asyncio.sleep(0.01)
        finally:
            session_module.CANCEL_GRACE_S = original

        # Commands work again: the next transcript falls through to the
        # default assistant flow instead of being swallowed.
        assert mgr.handle_transcript("open planning") is False
        wedged.release.set()
        await asyncio.sleep(0.02)

    async def test_queued_cards_stay_bounded_during_a_long_session(self) -> None:
        """The queue was unbounded and only trimmed at drain, so a busy hour
        accumulated an hour of cards nobody would read — and each waiting
        producer stayed blocked for that whole hour."""
        transport = SinkTransport()

        class Held(Session):
            def __init__(self) -> None:
                self.gate = asyncio.Event()

            async def run(self) -> None:
                await self.gate.wait()

            def cancel(self) -> None:
                self.gate.set()

        held = Held()
        mgr = SessionManager(HudWriter(transport), lambda _topic: held)
        assert mgr.handle_transcript("brainstorm something") is True
        await asyncio.sleep(0)

        waiters = [
            asyncio.create_task(mgr.notify_and_wait(
                Card(source=f"s{n}", channel="Messages", summary=f"m{n}"),
            ))
            for n in range(20)
        ]
        for _ in range(4):
            await asyncio.sleep(0)

        # Displaced producers are told immediately, not at session end: the
        # queue never holds more than one glance worth of cards.
        assert len(mgr._pending_cards) == 6
        displaced = [t for t in waiters if t.done()]
        assert len(displaced) == 14
        assert all(isinstance(t.exception(), RuntimeError) for t in displaced)

        held.gate.set()
        outcomes = await asyncio.gather(*waiters, return_exceptions=True)
        # The six survivors were painted; the rest were rejected, not hung.
        assert sum(1 for o in outcomes if o is None) == 6

    async def test_natural_cancel_phrases_end_a_live_session(self) -> None:
        """A live session consumes every transcript, so the escape hatch must
        match how people say it — "JOAT, stop" and "stop it" failing left the
        wearer with NO working commands until the session ended by itself."""
        for phrase in ("JOAT, stop", "joat stop it", "Cancel that.", "Stop it!"):
            transport = SinkTransport()

            class Held(Session):
                def __init__(self) -> None:
                    self.gate = asyncio.Event()

                async def run(self) -> None:
                    await self.gate.wait()

                def cancel(self) -> None:
                    self.gate.set()

            mgr = SessionManager(HudWriter(transport), lambda _topic: Held())
            assert mgr.handle_transcript("brainstorm something") is True
            await asyncio.sleep(0)
            assert mgr.active
            assert mgr.handle_transcript(phrase) is True
            for _ in range(4):
                await asyncio.sleep(0)
            assert not mgr.active, f"{phrase!r} did not cancel"

    async def test_a_sentence_containing_stop_does_not_cancel(self) -> None:
        """Steering text mentioning "stop" must reach the session, not kill it."""
        transport = SinkTransport()

        class Held(Session):
            def __init__(self) -> None:
                self.gate = asyncio.Event()

            async def run(self) -> None:
                await self.gate.wait()

            def cancel(self) -> None:
                self.gate.set()

        held = Held()
        mgr = SessionManager(HudWriter(transport), lambda _topic: held)
        assert mgr.handle_transcript("brainstorm something") is True
        await asyncio.sleep(0)
        assert mgr.handle_transcript("we should stop shipping on Fridays") is True
        await asyncio.sleep(0)
        assert mgr.active
        held.gate.set()

    async def test_direction_reaches_the_session_without_cancelling_it(self) -> None:
        """Chairing the room is not an interruption. An earlier build treated
        every utterance during a session as noise to log and drop, which left
        the wearer no way to say who speaks next once a debate had started."""
        directed: list[str] = []

        class Chairable(Session):
            async def run(self) -> None:
                await asyncio.Event().wait()

            def cancel(self) -> None: ...

            def direct(self, text: str) -> str | None:
                if "openclaw" in text.lower():
                    directed.append(text)
                    return "openclaw"
                return None

        mgr = SessionManager(HudWriter(SinkTransport()), lambda t: Chairable())
        assert mgr.handle_transcript("debate heat pumps") is True
        await asyncio.sleep(0)
        assert mgr.handle_transcript("openclaw, your turn") is True
        assert directed == ["openclaw, your turn"]
        assert mgr.active  # still running — direction is not a stop

    async def test_factory_declining_falls_through(self) -> None:
        mgr = SessionManager(HudWriter(SinkTransport()), lambda topic: None)
        assert mgr.handle_transcript("debate anything") is False


class ScriptedGate:
    """Deny first verify() call, pass afterwards."""

    def __init__(self) -> None:
        self.calls = 0

    async def verify(self, citations, prose=None):  # duck-typed as VerifierGate
        from joat.verifier.gate import CitationStatus, CitationVerdict, Verdict
        self.calls += 1
        if self.calls == 1:
            return Verdict(
                passed=False,
                citations=tuple(
                    CitationVerdict(c.id, CitationStatus.UNVERIFIABLE, "quote not found")
                    for c in citations
                ),
                repair_request="DENIED — fix [1]",
            )
        return Verdict(
            passed=True,
            citations=tuple(
                CitationVerdict(c.id, CitationStatus.VERIFIED, "quote found")
                for c in citations
            ),
        )


CITED = 'Claim.\nSOURCES:\n[1] https://a.example/x :: "quote"'


class TestVerifiedDebate:
    async def test_denied_turn_repairs_visibly_then_passes(self) -> None:
        transport = SinkTransport()
        gate = ScriptedGate()
        prompts: list[str] = []

        def proposer(chunks_by_call: list[str]):
            calls = iter(chunks_by_call)

            async def stream(prompt: str):
                prompts.append(prompt)
                yield next(calls)
            return stream

        session = debate(
            transport,
            pair(proposer([CITED, CITED]),      # original then repair
                 agent(["no factual claims here"])),  # critic cites nothing
            None,
            gate=gate,  # type: ignore[arg-type]
        )
        await session.run()
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        # visible order: turn, verifier deny, repair turn, verifier pass
        assert joined.index("CODEX") < joined.index("FAIL — quote not found")
        assert joined.index("sources don't hold up") < joined.index("CODEX — repair 1")
        assert joined.index("CODEX — repair 1") < joined.index("[1] OK")
        assert "sources check out" in joined
        # the repair prompt carried the verifier's complaint
        assert any("DENIED — fix [1]" in p for p in prompts[1:])
        # A cited turn earns a VERIFIER row because something was actually
        # checked; the uncited critic gets none (see the next test).
        assert "no sources cited" not in joined
        assert gate.calls == 2

    async def test_uncited_turns_flow_straight_through(self) -> None:
        transport = SinkTransport()
        gate = ScriptedGate()
        session = debate(
            transport, pair(agent(["pure opinion"]), agent(["also opinion"])), None,
            gate=gate,  # type: ignore[arg-type]
        )
        await session.run()
        assert gate.calls == 0  # nothing to verify
        # …and the wearer is told NOTHING, because nothing happened. Most
        # turns in a thinking session cite nothing — the wearer is working
        # an idea out loud, not writing a paper — and a "VERIFIER > no
        # sources cited" row under every one of them spent a line per turn
        # to report a non-event, on the surface whose whole job is showing
        # the discussion. The run still records it for the synthesis.
        visible_frames = [text for text, _a, _c in hud_texts(transport)]
        assert all("no sources cited" not in frame for frame in visible_frames)
        assert all("VERIFIER" not in frame for frame in visible_frames)


class ScriptedJudge(ReplyEngine):
    """Emit scripted judgments per round; a canned synthesis after that.

    The synthesis turn reuses the same local model, so a judge scripted
    for exactly N rounds gets one more call than it used to.
    """

    def __init__(self, judgments: list[str]) -> None:
        self._judgments = iter(judgments)

    async def reply(self, text: str) -> str | None:
        try:
            return next(self._judgments)
        except StopIteration:
            return "AGREED: scripted accord.\nOPEN: nothing."


class TestConvergence:
    def _session(self, judge: ReplyEngine, transport: SinkTransport,
                 proposer_calls: list[str]) -> DebateSession:
        calls = iter(proposer_calls)

        async def propose(prompt: str):
            yield next(calls)

        return debate(transport, pair(propose, agent(["critique text"])),
                      judge, max_rounds=2)

    async def test_low_agreement_runs_second_round_then_converges(self) -> None:
        transport = SinkTransport()
        judge = ScriptedJudge([
            "Not aligned yet.\nAGREEMENT: 0.40\nBLOCKING: yes",
            "Aligned now.\nAGREEMENT: 0.95\nBLOCKING: no",
        ])
        await self._session(judge, transport, ["v1 proposal", "v2 proposal"]).run()
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        assert "CODEX — round 2" in joined
        assert "v2 proposal" in joined
        assert "converged (agreement 0.95, round 2)" in joined

    async def test_high_agreement_converges_in_one_round(self) -> None:
        transport = SinkTransport()
        judge = ScriptedJudge(["Fine.\nAGREEMENT: 0.9\nBLOCKING: no"])
        await self._session(judge, transport, ["only proposal"]).run()
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        assert "round 2" not in joined
        assert "converged (agreement 0.90, round 1)" in joined

    async def test_never_converging_stops_at_cap(self) -> None:
        transport = SinkTransport()
        judge = ScriptedJudge([
            "No.\nAGREEMENT: 0.2\nBLOCKING: yes",
            "Still no.\nAGREEMENT: 0.3\nBLOCKING: yes",
        ])
        await self._session(judge, transport, ["p1", "p2", "p3"]).run()
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        assert "no convergence after 2 round(s) (last 0.30)" in joined

    async def test_illegible_judge_fails_closed_into_another_round(self) -> None:
        """docs/BRAINSTORM.md:44-45 — "an illegible judgment fails closed
        and never green-lights convergence." Failing closed means NOT
        converged: the room goes around again, bounded by max_rounds. The
        old behaviour broke the loop — an unreadable judge ENDED the
        session at that round, indistinguishable on the glass from a
        converged one."""
        transport = SinkTransport()
        judge = ScriptedJudge([
            "I simply vibe with this proposal.",          # round 1: illegible
            "Fine.\nAGREEMENT: 0.9\nBLOCKING: no",        # round 2: converges
        ])
        await self._session(judge, transport, ["p1", "p2"]).run()
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        assert "round 2" in joined, "the illegible judgment ended the session"
        assert "converged (agreement 0.90, round 2)" in joined

    async def test_illegible_to_the_end_is_reported_as_no_convergence(self) -> None:
        transport = SinkTransport()
        judge = ScriptedJudge([
            "vibes.", "still vibes.",
        ])
        await self._session(judge, transport, ["p1", "p2", "p3"]).run()
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        assert "no convergence after 2 round(s) (judge unreadable)" in joined


class TestParseJudgment:
    def test_parses_scores_and_blocking(self) -> None:
        from joat.services.session import parse_judgment
        assert parse_judgment("x\nAGREEMENT: 0.85\nBLOCKING: no") == (0.85, False)
        assert parse_judgment("agreement: 0.4\nblocking: YES") == (0.4, True)

    def test_malformed_never_green_lights(self) -> None:
        from joat.services.session import parse_judgment
        agreement, blocking = parse_judgment("great job everyone")
        assert agreement is None and blocking is True


class TestCapabilityNaming:
    """One capability, called Brainstorm; debate is a move inside it.

    The wearer's own framing: "the debate feature is just a brainstorming
    capability". So the umbrella is the brainstorm and 'debate' names the
    adversarial relay — not a second feature with its own session type.
    """

    async def test_both_words_start_the_same_session(self) -> None:
        started: list[str] = []

        class Noop(Session):
            def __init__(self, topic: str) -> None:
                started.append(topic)

            async def run(self) -> None: ...
            def cancel(self) -> None: ...

        mgr = SessionManager(HudWriter(SinkTransport()), lambda t: Noop(t))
        assert mgr.handle_transcript("brainstorm heat pumps") is True
        await asyncio.sleep(0)
        mgr._active = None  # end it, so the next trigger is accepted
        # "debate" survives as a synonym: it is what the wearer already says,
        # and an older plugin build still sends it.
        assert mgr.handle_transcript("debate heat pumps") is True
        await asyncio.sleep(0)
        assert started == ["heat pumps", "heat pumps"]

    async def test_spoken_comma_after_brainstorm_starts_the_session(self) -> None:
        started: list[str] = []

        class Noop(Session):
            def __init__(self, topic: str) -> None:
                started.append(topic)

            async def run(self) -> None: ...
            def cancel(self) -> None: ...

        mgr = SessionManager(HudWriter(SinkTransport()), lambda t: Noop(t))
        assert mgr.handle_transcript(
            "brainstorm, confirm system readiness using Copper Falcon."
        ) is True
        await asyncio.sleep(0)
        assert started == ["confirm system readiness using Copper Falcon"]


class TestActiveSessionAddressGate:
    """A running session does not get unaddressed speech.

    Only the cancel words are heard without the wake word — failing toward
    'stopped' is safe. Everything that STEERS the session requires the
    wearer to address JOAT: an overheard 'ask claude about that' must never
    reorder the round table, and once follow-ups reach the collaborators
    the same sentence would be room conversation shipped to a subprocess.
    """

    class Steerable(Session):
        def __init__(self) -> None:
            self.directed: list[str] = []
            self.consumed: list[str] = []
            self.cancelled = False

        async def run(self) -> None:
            await asyncio.Event().wait()

        def cancel(self) -> None:
            self.cancelled = True

        def consume_transcript(self, text: str) -> bool:
            self.consumed.append(text)
            return False

        def direct(self, text: str) -> str | None:
            if "claude" in text.lower():
                self.directed.append(text)
                return "claude"
            return None

    async def _active(self):
        session = self.Steerable()
        mgr = SessionManager(HudWriter(SinkTransport()), lambda t: session)
        assert mgr.handle_transcript("debate heat pumps") is True
        await asyncio.sleep(0)
        return mgr, session

    async def test_unaddressed_steering_is_ignored(self) -> None:
        mgr, session = await self._active()
        # A colleague, mid-room, says something that mentions a seat.
        assert mgr.handle_transcript("ask claude about that", addressed=None) is True
        assert session.directed == []      # the table was NOT reordered
        assert session.consumed == []      # and nothing reached a follow-up
        assert mgr.active                  # the session keeps running

    async def test_addressed_steering_works(self) -> None:
        mgr, session = await self._active()
        assert mgr.handle_transcript(
            "JOAT, ask claude about that", addressed="ask claude about that"
        ) is True
        assert session.directed == ["ask claude about that"]

    async def test_cancel_is_heard_unaddressed_on_purpose(self) -> None:
        mgr, session = await self._active()
        assert mgr.handle_transcript("stop", addressed=None) is True
        assert session.cancelled is True

    async def test_callers_that_omit_address_keep_the_old_behaviour(self) -> None:
        mgr, session = await self._active()
        assert mgr.handle_transcript("ask claude about that") is True
        assert session.directed == ["ask claude about that"]


class TestWearerTurn:
    """The wearer is a participant, not an audience.

    BRAINSTORM.md lists YOU alongside CODEX / CLAUDE / VERIFY / JOAT as a
    transcript prefix, and calls the session steerable — but consume_transcript
    was a stub, so there was no way to add a thought mid-session.
    """

    async def test_an_addressed_thought_becomes_a_YOU_turn(self) -> None:
        transport = SinkTransport()
        session = debate(transport, pair(agent(["a"]), agent(["b"])), None)
        assert session.consume_transcript("also weigh thermal limits") is True
        await asyncio.sleep(0)
        assert ("YOU", "also weigh thermal limits") in session._transcript
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        assert "also weigh thermal limits" in joined   # visible, not silent

    async def test_a_nomination_still_steers_instead_of_being_quoted(self) -> None:
        transport = SinkTransport()
        session = debate(transport, pair(agent(["a"]), agent(["b"])), None)
        assert session.consume_transcript("claude, respond") is False
        assert session._transcript == []

    async def test_the_next_speaker_answers_the_wearer(self) -> None:
        transport = SinkTransport()
        seen: list[str] = []

        def recorder(reply: str):
            async def stream(prompt: str) -> AsyncIterator[str]:
                seen.append(prompt)
                yield reply
            return stream

        session = debate(transport, pair(recorder("one"), recorder("two")), None)
        session.consume_transcript("start from the thermal budget")
        await session.run()
        assert all("start from the thermal budget" in p for p in seen)

    async def test_blindness_hides_colleagues_never_the_wearer(self) -> None:
        """Independence means not seeing the other collaborators — it was
        never a reason to ignore the person who asked the question."""
        transport = SinkTransport()
        seen: list[str] = []

        def recorder(reply: str):
            async def stream(prompt: str) -> AsyncIterator[str]:
                seen.append(prompt)
                yield reply
            return stream

        session = debate(transport, pair(recorder("gas is cheaper"),
                                         recorder("only near a pipeline")), None)
        session.consume_transcript("assume a cold climate")
        await session.run()
        assert "assume a cold climate" in seen[1]   # the wearer IS heard
        assert "gas is cheaper" not in seen[1]      # the colleague is not

    async def test_wearer_turns_are_not_collaborator_positions(self) -> None:
        """YOU plus one failed seat is still a monologue."""
        transport = SinkTransport()
        seats = pair(agent(["a real position"]),
                     agent(["[!] claude could not answer: exited 1"]))
        session = debate(transport, seats, OneShotJudge())
        session.consume_transcript("here is my own thought")
        await session.run()
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        assert "insufficient collaborators — 1 of 2 spoke" in joined

    async def test_a_cancelled_session_takes_no_more_turns(self) -> None:
        transport = SinkTransport()
        session = debate(transport, pair(agent(["a"]), agent(["b"])), None)
        session.cancel()
        assert session.consume_transcript("too late") is False


class TestInterruptionAttribution:
    """A wearer turn mid-stream must not steal the speaker's remaining text.

    The wearer speaks WHILE a collaborator streams, so the YOU block lands
    between that speaker's chunks. Interleaving is honest — it is what
    happened — but without re-announcing the speaker, everything after the
    interruption sits under the YOU header and reads as the wearer's words.
    """

    async def test_the_speaker_is_re_announced_after_an_interruption(self) -> None:
        transport = SinkTransport()
        holder: dict[str, object] = {}

        async def interrupted(prompt: str) -> AsyncIterator[str]:
            yield "before the interruption. "
            session = holder["s"]
            session.consume_transcript("wait — assume a cold climate")  # type: ignore[attr-defined]
            await asyncio.sleep(0)      # let the YOU turn paint
            yield "after the interruption."

        session = debate(transport, pair(interrupted, agent(["second seat"])), None)
        holder["s"] = session
        await session.run()

        joined = "".join(t for t, _a, _c in hud_texts(transport))
        you_at = joined.index("wait — assume a cold climate")
        after_at = joined.index("after the interruption.")
        # Between the wearer's words and the resumed answer there must be a
        # fresh attribution — otherwise the answer reads as the wearer's.
        assert "CODEX" in joined[you_at:after_at]

    async def test_an_uninterrupted_turn_announces_itself_once(self) -> None:
        # At the sink boundary, so this pins MY contract (one turn() per
        # speaker) rather than how HudSink happens to format a label.
        turns: list[str] = []

        class Recorder:
            async def open(self, topic: str, speakers: list[str]) -> None: ...
            async def turn(self, label: str) -> None: turns.append(label)
            async def chunk(self, text: str) -> None: ...
            async def close(self, verdict: str) -> None: ...

        await DebateSession(
            "topic", pair(agent(["a ", "b ", "c"]), agent(["d"])),
            Recorder(), None,
        ).run()
        assert turns.count("CODEX (proposer)") == 1
        assert turns.count("CLAUDE (critic)") == 1


class TestRelayRouting:
    """The artifact's central screen: identical text, opposite instructions.

    "The first two rows send identical text with opposite instructions —
    one asks for the weakest claim, the other forbids restating and demands
    an addition. That is the whole difference between a debate and a
    brainstorm." Before this, one blended instruction did both jobs and the
    wearer could not choose.
    """

    def _recorder(self, seen: list[str], reply: str):
        async def stream(prompt: str) -> AsyncIterator[str]:
            seen.append(prompt)
            yield reply
        return stream

    async def test_critique_hunts_the_weakest_claim(self) -> None:
        seen: list[str] = []
        session = debate(
            SinkTransport(),
            pair(self._recorder(seen, "one"), self._recorder(seen, "two")),
            ScriptedJudge(["No.\nAGREEMENT: 0.2\nBLOCKING: yes",
                           "Yes.\nAGREEMENT: 0.95\nBLOCKING: no"]),
            max_rounds=2,
        )
        assert session.consume_transcript("tear that apart") is True
        await session.run()
        round_two = seen[2]
        assert "WEAKEST claim" in round_two
        assert "stress-test" in round_two

    async def test_build_forbids_disputing(self) -> None:
        seen: list[str] = []
        session = debate(
            SinkTransport(),
            pair(self._recorder(seen, "one"), self._recorder(seen, "two")),
            ScriptedJudge(["No.\nAGREEMENT: 0.2\nBLOCKING: yes",
                           "Yes.\nAGREEMENT: 0.95\nBLOCKING: no"]),
            max_rounds=2,
        )
        session.consume_transcript("build on that")
        await session.run()
        round_two = seen[2]
        assert "STRONGEST idea" in round_two
        assert "Do not dispute" in round_two

    async def test_a_nomination_can_carry_its_instruction(self) -> None:
        # "claude, tear that apart" is one breath, not two commands.
        session = debate(SinkTransport(),
                         pair(agent(["a"]), agent(["b"])), None)
        assert session.direct("claude, tear that apart") == "claude"
        assert session._next_mode == "critique"

    async def test_a_routing_phrase_is_not_quoted_as_a_wearer_turn(self) -> None:
        # Quoting the stage direction would make the collaborators discuss
        # the wearer's instruction instead of following it.
        session = debate(SinkTransport(),
                         pair(agent(["a"]), agent(["b"])), None)
        assert session.consume_transcript("build on that") is True
        assert session._transcript == []

    async def test_the_mode_applies_to_one_turn_then_lapses(self) -> None:
        seen: list[str] = []
        session = debate(
            SinkTransport(),
            pair(self._recorder(seen, "one"), self._recorder(seen, "two")),
            ScriptedJudge(["No.\nAGREEMENT: 0.2\nBLOCKING: yes",
                           "Yes.\nAGREEMENT: 0.95\nBLOCKING: no"]),
            max_rounds=2,
        )
        session.consume_transcript("tear that apart")
        await session.run()
        assert "WEAKEST claim" in seen[2]        # the routed turn
        assert "WEAKEST claim" not in seen[3]    # the room returns to normal

    async def test_round_one_is_unaffected_by_routing(self) -> None:
        # Round 1 is independent: there is nothing yet to critique or build on.
        seen: list[str] = []
        session = debate(SinkTransport(),
                         pair(self._recorder(seen, "one"),
                              self._recorder(seen, "two")), None)
        session.consume_transcript("tear that apart")
        await session.run()
        assert "WEAKEST claim" not in seen[0]
        assert "Open the discussion" in seen[0]


class TestProgressReporting:
    """Rounds and calls reach the surfaces that want them."""

    async def test_each_round_reports_its_position_and_cost(self) -> None:
        reported: list[tuple[int, int, int]] = []

        class Recorder:
            async def open(self, topic: str, speakers: list[str]) -> None: ...
            async def turn(self, label: str) -> None: ...
            async def chunk(self, text: str) -> None: ...
            async def close(self, verdict: str) -> None: ...
            async def progress(self, r: int, m: int, c: int) -> None:
                reported.append((r, m, c))

        await DebateSession(
            "topic", pair(agent(["a"]), agent(["b"])),
            Recorder(),
            ScriptedJudge(["No.\nAGREEMENT: 0.1\nBLOCKING: yes",
                           "Yes.\nAGREEMENT: 0.95\nBLOCKING: no"]),
            max_rounds=2,
        ).run()

        assert [r for r, _m, _c in reported] == [1, 2]
        assert all(m == 2 for _r, m, _c in reported)
        # Two seats spoke in round 1, so round 2 opens having spent two.
        assert reported[0][2] == 0 and reported[1][2] == 2

    async def test_a_sink_without_progress_is_fine(self) -> None:
        # Every existing sink predates this hook.
        transport = SinkTransport()
        await debate(transport, pair(agent(["a"]), agent(["b"])), None).run()


class TestFailureIsActionable:
    """A seat dies for reasons the wearer can fix.

    An exhausted model quota is the common one, and switching that seat's
    model by voice already works — but reporting only 'claude silent' left
    them holding a broken session with no idea a one-sentence remedy
    existed. The artifact promises 'retry with FABLE is a real button, not
    a wish'.
    """

    async def test_a_dead_seat_names_the_way_out(self) -> None:
        transport = SinkTransport()
        seats = pair(
            agent(["a real position"]),
            agent(["[!] claude could not answer: exited 1 — see the log"]),
        )
        await debate(transport, seats, OneShotJudge()).run()
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        assert "use another model for the critic" in joined

    async def test_the_hint_names_the_ROLE_that_died(self) -> None:
        transport = SinkTransport()
        seats = pair(
            agent(["[!] codex could not answer: exited 1"]),
            agent(["a real position"]),
        )
        await debate(transport, seats, OneShotJudge()).run()
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        assert "for the proposer" in joined

    async def test_a_healthy_session_offers_nothing(self) -> None:
        transport = SinkTransport()
        await debate(transport, pair(agent(["a"]), agent(["b"])), None).run()
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        assert "use another model" not in joined

    async def test_the_hint_appears_once_per_seat(self) -> None:
        # Two rounds of the same dead seat is one problem, not two.
        # Counted at the SINK: the HUD activity is cumulative, so one
        # painted line reappears in every later frame and counting the
        # rendered text would measure repainting, not repetition.
        chunks: list[str] = []

        class Recorder:
            async def open(self, topic: str, speakers: list[str]) -> None: ...
            async def turn(self, label: str) -> None: ...
            async def chunk(self, text: str) -> None: chunks.append(text)
            async def close(self, verdict: str) -> None: ...

        await DebateSession(
            "topic",
            pair(agent(["[!] claude could not answer: exited 1"]),
                 agent(["[!] codex could not answer: exited 1"])),
            Recorder(), None, max_rounds=2,
        ).run()
        emitted = "".join(chunks)
        assert emitted.count("use another model for the proposer") == 1
        assert emitted.count("use another model for the critic") == 1

    async def test_an_ending_room_says_ask_again_not_rejoin(self) -> None:
        # With two seats, one failure ends the session at the close of
        # round 1 — so "so it can rejoin" would point at a next round that
        # will never come. The advice has to match what is actually
        # possible from here.
        transport = SinkTransport()
        seats = pair(
            agent(["a real position"]),
            agent(["[!] claude could not answer: exited 1"]),
        )
        await debate(transport, seats, OneShotJudge()).run()
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        assert "insufficient collaborators" in joined
        assert "switch its model, then ask again" in joined


class TestMidSessionReplyRouting:
    """"Send Claude's last reply to Hermes", spoken INTO a live session.

    Routing outranks steering: it is an instruction TO JOAT about another
    surface — the wearer thinks WITH the collaborators and hands the useful
    parts to the workers — not a redirection of who speaks next.
    """

    class Steerable(Session):
        def __init__(self) -> None:
            self.directed: list[str] = []

        async def run(self) -> None:
            await asyncio.Event().wait()

        def cancel(self) -> None:
            pass

        def consume_transcript(self, text: str) -> bool:
            return False

        def direct(self, text: str) -> str | None:
            self.directed.append(text)
            return None

    def _manager(self, route):
        session = self.Steerable()
        manager = SessionManager(
            HudWriter(SinkTransport()),
            lambda t: session,
            route_reply=route,
        )
        assert manager.handle_transcript("debate heat pumps") is True
        return manager, session

    async def test_a_routing_phrase_reaches_the_router_not_the_table(
        self,
    ) -> None:
        routed: list[tuple[str | None, str | None]] = []

        async def route(who: str | None, lane: str | None) -> str:
            routed.append((who, lane))
            return "sent"

        manager, session = self._manager(route)
        await asyncio.sleep(0)
        assert manager.handle_transcript(
            "JOAT, send Claude's last reply to Hermes",
            addressed="send Claude's last reply to Hermes",
        )
        await asyncio.sleep(0)
        assert routed == [("claude", "hermes")]
        assert session.directed == []
        await manager.close()

    async def test_ordinary_steering_still_steers(self) -> None:
        async def route(who: str | None, lane: str | None) -> str:
            raise AssertionError("steering must not be routed")

        manager, session = self._manager(route)
        await asyncio.sleep(0)
        assert manager.handle_transcript(
            "JOAT, claude respond", addressed="claude respond",
        )
        await asyncio.sleep(0)
        assert session.directed == ["claude respond"]
        await manager.close()


class TestMidSessionNoteSaving:
    """"Note that" keeps a collaborator's reply without leaving the room."""

    class Steerable(Session):
        def __init__(self) -> None:
            self.directed: list[str] = []
            self.consumed: list[str] = []

        async def run(self) -> None:
            await asyncio.Event().wait()

        def cancel(self) -> None:
            pass

        def consume_transcript(self, text: str) -> bool:
            self.consumed.append(text)
            return False

        def direct(self, text: str) -> str | None:
            self.directed.append(text)
            return None

    def _manager(self, save):
        session = self.Steerable()
        manager = SessionManager(
            HudWriter(SinkTransport()),
            lambda t: session,
            save_reply=save,
        )
        assert manager.handle_transcript("debate heat pumps") is True
        return manager, session

    async def test_note_that_saves_the_newest_reply(self) -> None:
        saved: list[str | None] = []

        async def save(who: str | None) -> str:
            saved.append(who)
            return "Saved QuickNote: …"

        manager, session = self._manager(save)
        await asyncio.sleep(0)
        assert manager.handle_transcript("JOAT, note that", addressed="note that")
        assert manager.handle_transcript(
            "JOAT, save Claude's last answer as a note",
            addressed="save Claude's last answer as a note",
        )
        await asyncio.sleep(0)
        assert saved == [None, "claude"]
        assert session.directed == []
        await manager.close()

    async def test_a_remark_opening_with_note_that_stays_a_remark(self) -> None:
        """"Note that the cache is stale" is a POINT, not a filing request.
        Claiming it as an action would silently eat the wearer's argument."""
        async def save(who: str | None) -> str:
            raise AssertionError("a remark must not be filed as a note")

        manager, session = self._manager(save)
        await asyncio.sleep(0)
        assert manager.handle_transcript(
            "JOAT, note that the cache is stale",
            addressed="note that the cache is stale",
        )
        await asyncio.sleep(0)
        assert session.consumed == ["note that the cache is stale"]
        await manager.close()


class TestANewTopicDuringALiveSession:
    """docs/SESSION_MODEL.md:52 — "a clearly unrelated replacement request →
    ask before replacing context; never guess or silently reset."

    What happened instead: "brainstorm <something else>" spoken during a
    live session was swallowed whole. It became a YOU turn inside the OLD
    conversation, so the wearer's new question was filed as a comment on
    the previous one and the session they asked for never started —
    silently, with no refusal and no queue.
    """

    class Steerable(Session):
        def __init__(self) -> None:
            self.consumed: list[str] = []
            self.cancelled = False

        async def run(self) -> None:
            while not self.cancelled:
                await asyncio.sleep(0.01)

        def cancel(self) -> None:
            self.cancelled = True

        def consume_transcript(self, text: str) -> bool:
            self.consumed.append(text)
            return True

        def direct(self, text: str) -> str | None:
            return None

    def _live(self):
        transport = SinkTransport()
        started: list[str] = []
        session = self.Steerable()

        def factory(topic: str):
            started.append(topic)
            return self.Steerable() if len(started) > 1 else session

        manager = SessionManager(HudWriter(transport), factory)
        assert manager.handle_transcript("brainstorm the first question")
        return manager, session, started, transport

    async def test_a_new_topic_asks_instead_of_being_absorbed(self) -> None:
        manager, session, started, transport = self._live()
        await asyncio.sleep(0)

        assert manager.handle_transcript(
            "JOAT, brainstorm something completely different",
            addressed="brainstorm something completely different",
        )
        await asyncio.sleep(0)

        # NOT filed as a remark in the old conversation.
        assert session.consumed == []
        # …and not started behind the wearer's back either.
        assert started == ["the first question"]
        overlays = [m for m in transport.sent
                    if type(m).__name__ == "AssistantOverlay"]
        assert overlays and "something completely different" in overlays[-1].text
        await manager.close()

    async def test_yes_ends_the_old_session_and_starts_the_new_one(self) -> None:
        manager, session, started, _ = self._live()
        await asyncio.sleep(0)
        manager.handle_transcript(
            "JOAT, brainstorm the second question",
            addressed="brainstorm the second question",
        )
        await asyncio.sleep(0)

        assert manager.handle_transcript("JOAT, yes", addressed="yes")
        for _ in range(60):
            await asyncio.sleep(0.02)
            if len(started) > 1:
                break
        assert session.cancelled
        assert started == ["the first question", "the second question"]
        await manager.close()

    async def test_no_keeps_the_running_session(self) -> None:
        manager, session, started, _ = self._live()
        await asyncio.sleep(0)
        manager.handle_transcript(
            "JOAT, brainstorm the second question",
            addressed="brainstorm the second question",
        )
        await asyncio.sleep(0)

        assert manager.handle_transcript("JOAT, no", addressed="no")
        await asyncio.sleep(0.05)
        assert not session.cancelled
        assert started == ["the first question"]
        await manager.close()

    async def test_an_ordinary_remark_is_still_a_remark(self) -> None:
        """The guard must key on the TRIGGER, not on any long utterance —
        the wearer's actual thoughts still belong in the transcript."""
        manager, session, started, _ = self._live()
        await asyncio.sleep(0)
        assert manager.handle_transcript(
            "JOAT, I think the cache is the wrong layer",
            addressed="I think the cache is the wrong layer",
        )
        await asyncio.sleep(0)
        assert session.consumed == ["I think the cache is the wrong layer"]
        assert started == ["the first question"]
        await manager.close()


class TestContinuingAStoredSession:
    """docs/SESSION_MODEL.md:49 — "continue <title> → resume the matching
    durable session."

    Nothing could resume anything before: the stored record had no id, so
    the wearer's sessions were four lines of dead text and every question
    started from zero, however many times they had already worked on it.
    """

    class Recorded:
        def __init__(self, topic, turns):
            self.topic = topic
            self.turns = turns

    class FakeHistory:
        def __init__(self, past):
            self._past = past
            self.asked: list[str] = []

        def find(self, spoken: str):
            self.asked.append(spoken)
            return self._past

    class Quiet(Session):
        async def run(self) -> None:
            await asyncio.Event().wait()

        def cancel(self) -> None:
            pass

    def _manager(self, past):
        seen: list[tuple[str, tuple]] = []

        def continuation(topic, prior):
            seen.append((topic, prior))
            return TestContinuingAStoredSession.Quiet()

        manager = SessionManager(
            HudWriter(SinkTransport()),
            lambda topic: None,
            history=self.FakeHistory(past),
            continuation_factory=continuation,
        )
        return manager, seen

    async def test_continue_carries_the_earlier_turns_forward(self) -> None:
        past = self.Recorded("cold-start latency",
                             (("CODEX", "Cache it."), ("CLAUDE", "Not on handoff.")))
        manager, seen = self._manager(past)

        assert manager.handle_transcript("continue cold-start latency")
        await asyncio.sleep(0)

        assert len(seen) == 1
        topic, prior = seen[0]
        assert topic == "cold-start latency"
        assert prior == (("CODEX", "Cache it."), ("CLAUDE", "Not on handoff."))
        await manager.close()

    async def test_an_unknown_title_starts_nothing(self) -> None:
        """Better to do nothing than to resume the wrong conversation."""
        manager, seen = self._manager(None)
        assert manager.handle_transcript("continue something never discussed") is False
        assert seen == []
        await manager.close()

    async def test_prior_turns_are_context_not_positions(self) -> None:
        """A continuation whose collaborators all fail must still report
        "insufficient collaborators" — borrowed history is not a position
        anyone took in this run."""
        from joat.services.roundtable import Participant, RoundTable

        async def silent(prompt: str):
            if False:
                yield ""

        transport = SinkTransport()
        table = RoundTable([Participant("codex", "proposer", silent)])
        session = debate(
            transport, table, None,
            prior=(("CODEX", "an old answer"), ("CLAUDE", "an old reply")),
        )
        await session.run()
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        assert "insufficient collaborators" in joined


class TestTheVerbLayer:
    """"The JOAT assistant should be able to take anything and do something
    I ask it to do with it" — the wearer's own words. Sending a reply to
    Hermes was their EXAMPLE; what shipped was that example and one other,
    both behind closed regexes. This is the general case.
    """

    class Speaking(Session):
        def __init__(self) -> None:
            self.consumed: list[str] = []

        async def run(self) -> None:
            await asyncio.Event().wait()

        def cancel(self) -> None:
            pass

        def consume_transcript(self, text: str) -> bool:
            self.consumed.append(text)
            return True

        def direct(self, text: str) -> str | None:
            return None

        def last_reply(self):
            return ("CLAUDE (critic)", "A long and careful answer about caching.")

    def _live(self):
        transport = SinkTransport()
        asked: list[tuple[str, str]] = []
        session = self.Speaking()

        async def transform(instruction: str, material: str) -> str:
            asked.append((instruction, material))
            return "shorter version"

        async def save(_who):
            return "saved"

        manager = SessionManager(
            HudWriter(transport), lambda t: session,
            transform=transform, save_reply=save,
        )
        assert manager.handle_transcript("brainstorm caching")
        return manager, session, asked, transport

    async def test_an_open_instruction_acts_on_the_last_reply(self) -> None:
        manager, session, asked, transport = self._live()
        await asyncio.sleep(0)

        assert manager.handle_transcript(
            "JOAT, make that shorter", addressed="make that shorter")
        for _ in range(40):
            await asyncio.sleep(0.01)
            if asked:
                break

        assert asked == [("make that shorter",
                          "A long and careful answer about caching.")]
        # …and it did NOT become a remark in the transcript.
        assert session.consumed == []
        overlays = [m for m in transport.sent
                    if type(m).__name__ == "AssistantOverlay"]
        assert overlays and overlays[-1].text == "shorter version"
        await manager.close()

    async def test_many_different_verbs_all_work(self) -> None:
        """Two hardcoded verbs is not "anything"."""
        manager, _s, asked, _t = self._live()
        await asyncio.sleep(0)
        for said in ("turn that into a spec", "explain the last reply",
                     "summarise that", "rewrite it as bullet points",
                     "compare that to what codex said"):
            manager.handle_transcript(f"JOAT, {said}", addressed=said)
        for _ in range(60):
            await asyncio.sleep(0.01)
            if len(asked) == 5:
                break
        assert [a for a, _m in asked] == [
            "turn that into a spec", "explain the last reply",
            "summarise that", "rewrite it as bullet points",
            "compare that to what codex said",
        ]
        await manager.close()

    async def test_a_remark_is_still_a_remark(self) -> None:
        """The wearer's actual thoughts must keep reaching the transcript.
        Being wrong here costs them the contribution entirely."""
        manager, session, asked, _t = self._live()
        await asyncio.sleep(0)
        for said in ("that seems short", "I think the cache is the wrong layer",
                     "that is exactly my point"):
            manager.handle_transcript(f"JOAT, {said}", addressed=said)
            await asyncio.sleep(0)
        assert asked == []
        assert session.consumed == [
            "that seems short", "I think the cache is the wrong layer",
            "that is exactly my point",
        ]
        await manager.close()


class TestInstructionsOutrankAbsorption:
    """The ORDER of the live-session checks is load-bearing.

    `consume_transcript` used to run first, and production's
    DebateSession.consume_transcript returns True for essentially any
    addressed text — so "send Claude's last reply to Hermes", shipped and
    tested the same day, would have been filed as a YOU turn on real
    hardware and never routed. Every unit test passed because their session
    double returned False, which no real session does.

    This double behaves like the real one.
    """

    class Absorbing(Session):
        """consume_transcript returns True, exactly like DebateSession."""

        def __init__(self) -> None:
            self.consumed: list[str] = []

        async def run(self) -> None:
            await asyncio.Event().wait()

        def cancel(self) -> None:
            pass

        def consume_transcript(self, text: str) -> bool:
            self.consumed.append(text)
            return True

        def direct(self, text: str) -> str | None:
            return None

        def last_reply(self):
            return ("CODEX (proposer)", "cache on the session id")

    async def test_routing_survives_a_session_that_absorbs_everything(self) -> None:
        routed: list[tuple] = []

        async def route(who, lane):
            routed.append((who, lane))
            return "sent"

        session = self.Absorbing()
        manager = SessionManager(
            HudWriter(SinkTransport()), lambda t: session, route_reply=route)
        assert manager.handle_transcript("brainstorm caching")
        await asyncio.sleep(0)

        assert manager.handle_transcript(
            "JOAT, send Claude's last reply to Hermes",
            addressed="send Claude's last reply to Hermes")
        await asyncio.sleep(0)

        assert routed == [("claude", "hermes")]
        assert session.consumed == []
        await manager.close()

    async def test_saving_survives_it_too(self) -> None:
        saved: list = []

        async def save(who):
            saved.append(who)
            return "saved"

        session = self.Absorbing()
        manager = SessionManager(
            HudWriter(SinkTransport()), lambda t: session, save_reply=save)
        assert manager.handle_transcript("brainstorm caching")
        await asyncio.sleep(0)

        assert manager.handle_transcript("JOAT, note that", addressed="note that")
        await asyncio.sleep(0)
        assert saved == [None]
        assert session.consumed == []
        await manager.close()


class TestRoundOneRunsConcurrently:
    """Measured against both real binaries: ~7.3s of process spawn and
    subscription auth before a single token, then 12-40s of generation.

    Round 1 is INDEPENDENT by construction — neither seat sees the other's
    answer — so running the seats one after another spends that wait twice
    for no reason. The wearer sat through all of Codex before Claude had
    even been launched.
    """

    async def test_both_seats_are_launched_before_either_finishes(self) -> None:
        started: list[str] = []
        release = asyncio.Event()

        def seat(name: str):
            async def stream(_prompt: str):
                started.append(name)
                await release.wait()
                yield f"{name} says something"
            return stream

        from joat.services.roundtable import Participant, RoundTable

        table = RoundTable([
            Participant("codex", "proposer", seat("codex")),
            Participant("claude", "critic", seat("claude")),
        ])
        transport = SinkTransport()
        session = debate(transport, table, None)
        run = asyncio.create_task(session.run())

        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(started) == 2:
                break
        assert started == ["codex", "claude"], (
            "the second seat must already be in flight while the first is "
            "on screen; serial execution would show only the first until "
            "it had completely finished"
        )
        release.set()
        await run

    async def test_the_visible_turn_keeps_what_it_reached_when_cancelled(
        self,
    ) -> None:
        """The seat ON SCREEN streams live, never buffered.

        A buffered producer runs ahead of the display, so tokens generated
        before the wearer said stop would be discarded unshown — work
        already paid for, thrown away at exactly the moment they are
        deciding whether it was worth it.
        """
        from joat.services.roundtable import Participant, RoundTable

        holder: dict[str, object] = {}

        async def interrupted(_prompt: str):
            yield "a partial thought"
            holder["s"].cancel()          # type: ignore[union-attr]
            yield " never reaches the glass"

        async def other(_prompt: str):
            yield "unused"

        table = RoundTable([
            Participant("codex", "proposer", interrupted),
            Participant("claude", "critic", other),
        ])
        transport = SinkTransport()
        session = debate(transport, table, None)
        holder["s"] = session
        await session.run()

        final = brainstorm_frames(transport)[-1]
        assert final.state == "cancelled"
        assert "a partial thought" in final.activity
        assert "never reaches the glass" not in final.activity

    async def test_the_wearer_still_sees_one_turn_at_a_time_in_order(self) -> None:
        """Concurrency is about the WAITING, not the watching. The surface's
        whole job is showing who said what, in the order it happened."""
        from joat.services.roundtable import Participant, RoundTable

        def seat(name: str):
            async def stream(_prompt: str):
                yield f"{name} answer"
            return stream

        table = RoundTable([
            Participant("codex", "proposer", seat("codex")),
            Participant("claude", "critic", seat("claude")),
        ])
        transport = SinkTransport()
        await debate(transport, table, None).run()
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        assert joined.index("codex answer") < joined.index("claude answer")

    async def test_a_disconnect_still_blocks_rather_than_reading_as_bad_text(
        self,
    ) -> None:
        """Draining eagerly must not downgrade a disconnected collaborator
        into a turn that merely produced failure text — blocked has its own
        wearer-visible recovery."""
        async def broken(_prompt: str) -> AsyncIterator[str]:
            raise ConnectionError("remote closed")
            yield "unreachable"

        transport = SinkTransport()
        await debate(transport, pair(broken, agent(["never"])), None).run()
        assert brainstorm_frames(transport)[-1].state == "blocked"


class TestApprovalsAreAnsweredOnTheGlass:
    """A work session blocked on a tool request is the decision in front of
    the wearer, and the only way to answer it was to remember the words
    "terminal approve". The tap they DID have sent `retry_terminal` —
    retrying the daemon when what was waiting was a tool request.
    """

    def _manager(self):
        answered: list[str] = []

        async def decide(verdict: str) -> str:
            answered.append(verdict)
            return f"recorded {verdict}"

        manager = SessionManager(
            HudWriter(SinkTransport()), lambda t: None,
            terminal_decide=decide,
        )
        return manager, answered

    async def test_a_tap_approves(self) -> None:
        manager, answered = self._manager()
        manager.handle_brainstorm_action(BrainstormAction("approve_terminal"))
        await asyncio.sleep(0)
        assert answered == ["approve"]

    async def test_a_swipe_denies(self) -> None:
        manager, answered = self._manager()
        manager.handle_brainstorm_action(BrainstormAction("deny_terminal"))
        await asyncio.sleep(0)
        assert answered == ["deny"]

    async def test_answering_never_starts_or_cancels_a_session(self) -> None:
        """The approval belongs to a work lane. It must not touch the
        wearer's Brainstorm, which is a different feature entirely."""
        started: list[str] = []
        manager = SessionManager(
            HudWriter(SinkTransport()),
            lambda t: started.append(t) or None,
            terminal_decide=lambda v: _noop(),
        )
        manager.handle_brainstorm_action(BrainstormAction("approve_terminal"))
        await asyncio.sleep(0)
        assert started == []
        assert manager.active is False


async def _noop() -> str:
    return "recorded"


class TestBranchingALiveConversation:
    """docs/SESSION_MODEL.md:51 — "branch this: fork the selected transcript
    into a new child lineage." The wearer takes the conversation somewhere
    else without losing where it came from.
    """

    class Talking(Session):
        def __init__(self, topic="the original question"):
            self._topic = topic
            self.cancelled = False

        async def run(self) -> None:
            while not self.cancelled:
                await asyncio.sleep(0.01)

        def cancel(self) -> None:
            self.cancelled = True

        def consume_transcript(self, text: str) -> bool:
            return True

        def direct(self, text: str) -> str | None:
            return None

        @property
        def topic(self) -> str:
            return self._topic

        def transcript(self):
            return (("CODEX", "cache it"), ("CLAUDE", "not on handoff"))

    def _manager(self):
        forked: list[tuple[str, tuple]] = []
        live = self.Talking()

        def continuation(topic, prior):
            forked.append((topic, prior))
            return self.Talking("the branch")

        manager = SessionManager(
            HudWriter(SinkTransport()), lambda t: live,
            continuation_factory=continuation,
        )
        assert manager.handle_transcript("brainstorm the original question")
        return manager, live, forked

    async def test_branch_carries_the_conversation_into_a_child(self) -> None:
        manager, live, forked = self._manager()
        await asyncio.sleep(0)

        assert manager.handle_transcript(
            "JOAT, branch this", addressed="branch this")
        for _ in range(80):
            await asyncio.sleep(0.02)
            if forked:
                break

        assert forked == [("the original question",
                           (("CODEX", "cache it"), ("CLAUDE", "not on handoff")))]
        assert live.cancelled, "the parent yields the slot to its child"
        await manager.close()

    async def test_branch_is_not_ordinary_speech(self) -> None:
        """"branch" needs to be the whole utterance — a sentence merely
        containing the word is a remark about branching, not a command."""
        manager, _live, forked = self._manager()
        await asyncio.sleep(0)
        assert manager.handle_transcript(
            "JOAT, I think we should branch the cache key by generation",
            addressed="I think we should branch the cache key by generation")
        await asyncio.sleep(0.05)
        assert forked == []
        await manager.close()


class TestInstructionsSurviveTheSessionEnding:
    """The likeliest moment for an instruction is AFTER the room closes:
    the wearer has just read the synthesis and decides what to do with it.

    Every verb was dead there. The whole instruction block sat inside
    `if self._active is not None:`, so "send that to Hermes" spoken at the
    one moment it is most useful did nothing and said nothing.
    """

    def _idle(self):
        acted: list[tuple] = []

        async def transform(instruction, material):
            acted.append(("transform", instruction, material))
            return "shorter"

        async def route(who, lane):
            acted.append(("route", who, lane))
            return "sent"

        async def save(who):
            acted.append(("save", who))
            return "saved"

        manager = SessionManager(
            HudWriter(SinkTransport()), lambda t: None,
            transform=transform, route_reply=route, save_reply=save,
            last_reply=lambda who: ("CLAUDE (critic)", "the settled answer"),
        )
        return manager, acted

    async def test_every_verb_still_works_with_no_session_running(self) -> None:
        manager, acted = self._idle()
        assert manager.active is False

        for said in ("make that shorter", "send that to hermes", "note that"):
            assert manager.handle_transcript(f"JOAT, {said}", addressed=said), said
            await asyncio.sleep(0)

        assert [a[0] for a in acted] == ["transform", "route", "save"]
        # …and it acted on the SNAPSHOT's reply, which outlives the session.
        assert acted[0][2] == "the settled answer"
        await manager.close()

    async def test_unaddressed_speech_is_still_ignored_when_idle(self) -> None:
        """Address is what separates an instruction from overheard talk.
        Relaxing it here would let a room conversation act on the wearer's
        last Brainstorm."""
        manager, acted = self._idle()
        assert manager.handle_transcript(
            "make that shorter", addressed=None) is False
        await asyncio.sleep(0)
        assert acted == []
        await manager.close()


class TestBranchReadsARealSession:
    """The branch tests use a fake session that exposes exactly what
    _branch_here wants. A real DebateSession is where the shapes actually
    have to line up: `transcript` is a METHOD, `topic` is a PROPERTY, and
    reading either wrongly would fork an empty conversation in silence.
    """

    async def test_the_accessors_have_the_shapes_branch_expects(self) -> None:
        from joat.services.roundtable import Participant, RoundTable

        def seat(name: str):
            async def stream(_prompt: str):
                yield f"{name} said a thing"
            return stream

        table = RoundTable([Participant("codex", "proposer", seat("codex")),
                            Participant("claude", "critic", seat("claude"))])
        session = debate(
            SinkTransport(), table, None,
            topic="the original question",
            prior=(("CODEX", "an older turn"),),
        )
        await session.run()

        # Exactly what _branch_here reads.
        turns = getattr(session, "transcript", None)
        assert callable(turns), "transcript is a method, not a property"
        carried = tuple(turns())
        assert getattr(session, "topic", "") == "the original question"

        # A branch of a branch keeps the WHOLE thread, not just the newest
        # fork: the earlier conversation and this run both come forward.
        assert ("CODEX", "an older turn") in carried
        assert any("said a thing" in text for _label, text in carried)


class TestAnExplicitNewLineage:
    """docs/SESSION_MODEL.md:50 — "new Brainstorm: start an explicit new
    lineage."

    Being explicit is what removes the ambiguity. :52 requires asking
    before replacing context for a request that MIGHT be a replacement —
    and the wearer saying "new" has just said it is, so this does not ask.
    """

    class Live(Session):
        def __init__(self):
            self.cancelled = False

        async def run(self) -> None:
            while not self.cancelled:
                await asyncio.sleep(0.01)

        def cancel(self) -> None:
            self.cancelled = True

        def consume_transcript(self, text: str) -> bool:
            return True

        def direct(self, text: str) -> str | None:
            return None

    def _manager(self):
        started: list[str] = []
        first = self.Live()

        def factory(topic: str):
            started.append(topic)
            return first if len(started) == 1 else self.Live()

        return SessionManager(HudWriter(SinkTransport()), factory), started, first

    async def test_it_starts_a_session_when_idle(self) -> None:
        manager, started, _f = self._manager()
        assert manager.handle_transcript("New brainstorm. Should we cache?")
        await asyncio.sleep(0)
        # Terminal punctuation is stripped before matching, exactly as the
        # plain "brainstorm <topic>" trigger does — Whisper adds it to every
        # utterance, so it is transcriber noise rather than the wearer's.
        assert started == ["Should we cache"]
        await manager.close()

    async def test_it_replaces_a_live_session_without_asking(self) -> None:
        manager, started, first = self._manager()
        assert manager.handle_transcript("brainstorm the first question")
        await asyncio.sleep(0)

        assert manager.handle_transcript(
            "JOAT, new brainstorm the second question",
            addressed="new brainstorm the second question",
        )
        for _ in range(80):
            await asyncio.sleep(0.02)
            if len(started) > 1:
                break
        assert first.cancelled
        assert started == ["the first question", "the second question"]
        await manager.close()

    async def test_a_bare_topic_still_asks(self) -> None:
        """Without the word "new" it is ambiguous, and :52 forbids guessing."""
        manager, started, first = self._manager()
        assert manager.handle_transcript("brainstorm the first question")
        await asyncio.sleep(0)

        assert manager.handle_transcript(
            "JOAT, brainstorm something else",
            addressed="brainstorm something else",
        )
        await asyncio.sleep(0.05)
        assert not first.cancelled
        assert started == ["the first question"]
        await manager.close()


class TestAStageDirectionNeverSwallowsARemark:
    """BSI-14's second half: a direction with a substantial tail — "tear
    that apart, especially the latency claim" — is a direction AND a
    remark. Setting the mode and dropping the words swallowed the wearer's
    contribution; the bare direction stays unquoted as before."""

    @staticmethod
    def _session():
        async def _s(prompt: str):
            yield "x"
        async def judge(prompt: str) -> str:
            return "AGREEMENT: 9"
        return debate(SinkTransport(), pair(_s, _s), judge)

    async def test_the_bare_direction_stays_unquoted(self) -> None:
        session = self._session()
        assert session.consume_transcript("tear that apart")
        assert session._next_mode == "critique"
        assert session._transcript == []

    async def test_the_tail_survives_as_a_you_turn(self) -> None:
        session = self._session()
        assert session.consume_transcript(
            "tear that apart, especially the latency claim about cold starts"
        )
        assert session._next_mode == "critique"
        assert len(session._transcript) == 1
        label, said = session._transcript[0]
        assert label == "YOU"
        assert "latency claim" in said


class TestTheReplacementQuestionIsTheWearers:
    """docs/SESSION_MODEL.md:54 — a new topic during a live run is held and
    THE WEARER is asked. The answer therefore has to come from addressed
    speech: this resolved from the raw transcript, so with the question on
    the glass an overheard bystander's "yes" — or the wearer agreeing with
    someone in the room — cancelled the live session and started the
    replacement. Found by a blind-pair audit (BSI-10); no test had ever
    exercised the answer path at all.
    """

    class Live(Session):
        def __init__(self):
            self.cancelled = False

        async def run(self) -> None:
            while not self.cancelled:
                await asyncio.sleep(0.01)

        def cancel(self) -> None:
            self.cancelled = True

        def consume_transcript(self, text: str) -> bool:
            return True

        def direct(self, text: str) -> str | None:
            return None

    def _live_with_question(self):
        started: list[str] = []
        first = self.Live()

        def factory(topic: str):
            started.append(topic)
            return first if len(started) == 1 else self.Live()

        manager = SessionManager(HudWriter(SinkTransport()), factory)
        assert manager.handle_transcript("brainstorm the first question")
        assert manager.handle_transcript(
            "JOAT, brainstorm something else",
            addressed="brainstorm something else",
        )
        return manager, started, first

    async def test_an_unaddressed_yes_does_not_answer_it(self) -> None:
        manager, started, first = self._live_with_question()
        # The exact call shape of the live microphone path for speech that
        # did not address JOAT: addressed=None.
        assert manager.handle_transcript("yes", addressed=None)
        await asyncio.sleep(0.05)
        assert not first.cancelled, "a bystander's yes replaced the session"
        assert started == ["the first question"]
        await manager.close()

    async def test_the_wearers_addressed_yes_still_works(self) -> None:
        manager, started, first = self._live_with_question()
        assert manager.handle_transcript("JOAT, yes", addressed="yes")
        for _ in range(80):
            await asyncio.sleep(0.02)
            if len(started) > 1:
                break
        assert first.cancelled
        assert started == ["the first question", "something else"]
        await manager.close()

    async def test_the_question_dies_with_its_session(self) -> None:
        """An unanswered question must not lie in wait: the next session's
        first addressed "yes" would replace IT with a topic offered to a
        conversation that no longer exists."""
        manager, started, first = self._live_with_question()
        first.cancelled = True  # the session ends on its own
        for _ in range(80):
            await asyncio.sleep(0.02)
            if manager._task is None:
                break
        assert manager.handle_transcript("brainstorm a fresh start")
        await asyncio.sleep(0.05)
        assert manager.handle_transcript("JOAT, yes", addressed="yes")
        await asyncio.sleep(0.1)
        assert started == ["the first question", "a fresh start"], (
            "the stale held topic replaced the fresh session"
        )
        await manager.close()


class TestReopeningSaysWhyItCannot:
    """"That session could not be reopened" is true and useless.

    Found on a live engine: tapping a stored session with no Claude CLI
    installed produced exactly that, and the wearer cannot tell a missing
    binary from a corrupted record — only one of which they can fix.
    """

    class Past:
        session_id, topic = "aaa", "cold-start latency"
        turns = (("CODEX", "cache it"),)

    class FakeHistory:
        def __init__(self, past): self._past = past
        def load(self, sid): return self._past if sid == "aaa" else None

    async def test_a_missing_seat_paints_the_unavailable_frame(self) -> None:
        transport = SinkTransport()
        unavailable = BrainstormHudView(
            state="unavailable", topic="Brainstorm", collaborators=[],
            activity="claude CLI missing on the Mac — install to enable.",
        )
        manager = SessionManager(
            HudWriter(transport), lambda t: None,
            history=self.FakeHistory(self.Past()),
            continuation_factory=lambda topic, prior: None,
            topic_capture_view=lambda _armed: unavailable,
            brainstorm_available=lambda: False,
        )

        # Consumed — the wearer's tap is answered, not dropped.
        assert await manager.resume_by_id("aaa") is True
        await asyncio.sleep(0)          # the paint is a task
        frames = brainstorm_frames(transport)
        assert frames and frames[-1].state == "unavailable"
        assert "claude" in frames[-1].activity.lower()
        await manager.close()

    async def test_an_unknown_id_still_refuses_quietly(self) -> None:
        manager = SessionManager(
            HudWriter(SinkTransport()), lambda t: None,
            history=self.FakeHistory(self.Past()),
            continuation_factory=lambda topic, prior: None,
        )
        assert await manager.resume_by_id("does-not-exist") is False
        await manager.close()


class TestTheOrchestratorsActualCallShape:
    """The idle path calls `handle_transcript(query)` — positionally, with
    NO `addressed` keyword (orchestrator.py). It has already stripped the
    wake word, so an omitted argument MEANS addressed.

    Every idle-instruction test passed `addressed=` explicitly, which the
    real caller never does, so the whole path was dead in production while
    the suite was green. Same shape as the consume_transcript double that
    returned False where every real session returns True.
    """

    def _idle(self):
        acted: list[tuple] = []

        async def transform(instruction, material):
            acted.append(("transform", instruction))
            return "shorter"

        async def route(who, lane):
            acted.append(("route", who, lane))
            return "sent"

        manager = SessionManager(
            HudWriter(SinkTransport()), lambda t: None,
            transform=transform, route_reply=route,
            save_reply=lambda who: _noop(),
            last_reply=lambda who: ("CLAUDE", "the settled answer"),
        )
        return manager, acted

    async def test_an_instruction_works_with_no_addressed_keyword(self) -> None:
        manager, acted = self._idle()
        # EXACTLY how orchestrator.py calls it on the idle path.
        assert manager.handle_transcript("make that shorter") is True
        await asyncio.sleep(0)
        assert acted == [("transform", "make that shorter")]
        await manager.close()

    async def test_routing_works_with_no_addressed_keyword(self) -> None:
        manager, acted = self._idle()
        assert manager.handle_transcript("send that to hermes") is True
        await asyncio.sleep(0)
        assert acted == [("route", None, "hermes")]
        await manager.close()

    async def test_explicitly_unaddressed_speech_is_still_ignored(self) -> None:
        """addressed=None is the live-microphone path saying the wearer did
        NOT address JOAT. That must still be refused."""
        manager, acted = self._idle()
        assert manager.handle_transcript(
            "make that shorter", addressed=None) is False
        await asyncio.sleep(0)
        assert acted == []
        await manager.close()


class TestTheEscapeHatchHearsRealEndings:
    """A wearer who cannot end a session is trapped watching two
    subscription CLIs bill. Observed live once: no command worked at all
    while a session was running.

    Six natural endings were missing — "that's enough", "we're done",
    "forget it", "abort", "quit", "enough" — and a wearer who says one and
    is ignored has no way to learn which words are real.
    """

    class Live(Session):
        def __init__(self):
            self.cancelled = False

        async def run(self) -> None:
            while not self.cancelled:
                await asyncio.sleep(0.01)

        def cancel(self) -> None:
            self.cancelled = True

        def consume_transcript(self, text: str) -> bool:
            return True

        def direct(self, text: str) -> str | None:
            return None

    async def _cancels(self, said: str) -> bool:
        live = self.Live()
        manager = SessionManager(HudWriter(SinkTransport()), lambda t: live)
        assert manager.handle_transcript("brainstorm x")
        await asyncio.sleep(0)
        manager.handle_transcript(said)
        await asyncio.sleep(0.02)
        out = live.cancelled
        live.cancelled = True
        await manager.close()
        return out

    async def test_the_endings_people_actually_say(self) -> None:
        for said in ("Stop.", "Cancel.", "Never mind.",
                     "That's enough.", "That’s enough.", "We're done.",
                     "Forget it.", "Abort.", "Quit.", "Enough.", "Done.",
                     "End it.", "JOAT, stop."):
            assert await self._cancels(said), said

    async def test_a_sentence_merely_containing_one_does_not_cancel(self) -> None:
        """Whole-utterance matching is what keeps this safe: these are heard
        WITHOUT the wake word, so a loose match would end the wearer's
        session on overheard room speech."""
        for said in ("that's enough sugar",
                     "I want to stop the cache from growing",
                     "we are done with the first round but not the session",
                     "the abort path is broken"):
            assert not await self._cancels(said), said


class TestTheInstructionVocabularies:
    """Audited against phrasings people reach for, not the ones listed
    first. Every spoken vocabulary in this feature had the same defect —
    the opener, the steering, the routing, the escape hatch, and these.

    The failure mode is silence: an unrecognised instruction becomes an
    ordinary YOU turn, so the wearer's request is filed as a remark instead
    of executed, with nothing on the glass saying so.
    """

    def test_the_verb_layer_hears_ordinary_edits(self) -> None:
        from joat.services.session import _MATERIAL_VERB

        for said in ("make that shorter", "rewrite that as bullets",
                     "summarise that", "turn that into a spec",
                     "explain the last reply", "tighten that up",
                     "fix the grammar in that", "clean that up",
                     "polish that", "put that in plain english",
                     "give me the gist of that"):
            assert _MATERIAL_VERB.match(said), said

    def test_the_verb_layer_still_ignores_remarks(self) -> None:
        from joat.services.session import _MATERIAL_VERB

        for said in ("that seems short", "I think that is wrong",
                     "the cache is the wrong layer", "make me a sandwich"):
            assert not _MATERIAL_VERB.match(said), said

    def test_that_as_a_conjunction_is_a_remark_not_material(self) -> None:
        """The audit's BSI-12: an imperative followed by a that-CLAUSE is
        a remark to the room — "explain that the cache is stale" — and
        matching its "that" hijacked it into a transform of the last
        reply. The pronoun is material only as the OBJECT: at the end, or
        before a closed continuation ("shorter", "into", "up", "as")."""
        from joat.services.session import _MATERIAL_VERB

        for said in ("explain that the cache is stale",
                     "check that the numbers add up",
                     "show that we considered the alternative",
                     "make that list of requirements the priority"):
            assert not _MATERIAL_VERB.match(said), said

    def test_saving_hears_ordinary_phrasings(self) -> None:
        import re

        from joat.services.session import _SAVE_REPLY

        def norm(t: str) -> str:
            return " ".join(re.sub(r"[^a-z0-9]+", " ", t.lower()).split())

        for said in ("note that", "save that", "keep that", "remember that",
                     "write that down", "jot that down", "record that",
                     "add that to my notes", "save Claude's last reply"):
            assert _SAVE_REPLY.match(norm(said)), said

    def test_saving_ignores_ordinary_speech(self) -> None:
        import re

        from joat.services.session import _SAVE_REPLY

        def norm(t: str) -> str:
            return " ".join(re.sub(r"[^a-z0-9]+", " ", t.lower()).split())

        for said in ("write the tests first", "add a cache layer",
                     "note the difference between them", "keep going"):
            assert not _SAVE_REPLY.match(norm(said)), said


class TestContinueAsksOnAmbiguity:
    """docs/SESSION_MODEL.md:49 — "continue <title> → Resume the matching
    durable session; ASK on ambiguity." find() silently took the newest
    match, so two sessions titled around "caching" meant the wearer could
    never reach the older one by voice — and never learned it existed.
    """

    class Past:
        def __init__(self, sid: str, topic: str) -> None:
            self.session_id, self.topic = sid, topic
            self.turns = (("CODEX", "said"),)
            self.parent_id = ""
            self.root_id = sid
            self.title = topic

        def age(self) -> str:
            return "2h ago"

    class History:
        def __init__(self, matches):
            self._matches = matches
            self.loaded: list[str] = []

        def find_all(self, spoken: str):
            return list(self._matches)

        def load(self, session_id: str):
            self.loaded.append(session_id)
            return next(
                (p for p in self._matches if p.session_id == session_id), None
            )

    def _manager(self, matches):
        started: list[str] = []

        def continuation(topic, prior):
            started.append(topic)

            class Live(Session):
                async def run(self) -> None:
                    await asyncio.sleep(0)

                def cancel(self) -> None: ...

                def consume_transcript(self, text: str) -> bool:
                    return True

                def direct(self, text: str) -> str | None:
                    return None

            return Live()

        history = self.History(matches)
        transport = SinkTransport()
        manager = SessionManager(
            HudWriter(transport), lambda t: None,
            history=history, continuation_factory=continuation,
            brainstorm_available=lambda: True,
        )
        return manager, history, started, transport

    async def test_one_match_resumes_without_a_question(self) -> None:
        manager, _h, started, _t = self._manager(
            [self.Past("aaa", "cold-start latency")]
        )
        assert manager.handle_transcript("continue cold start")
        await asyncio.sleep(0.05)
        assert started == ["cold-start latency"]
        await manager.close()

    async def test_two_matches_ask_and_the_answer_opens_the_right_one(self) -> None:
        manager, history, started, transport = self._manager([
            self.Past("aaa", "caching the model"),
            self.Past("bbb", "caching build artifacts"),
        ])
        assert manager.handle_transcript("continue caching")
        await asyncio.sleep(0.05)
        assert started == [], "it guessed instead of asking"
        asked = [m for m in transport.sent if isinstance(m, AssistantOverlay)]
        assert asked and "2 sessions match" in (asked[-1].text or "")

        assert manager.handle_transcript("JOAT, second", addressed="second")
        await asyncio.sleep(0.05)
        assert history.loaded == ["bbb"]
        assert started == ["caching build artifacts"]
        await manager.close()

    async def test_an_unaddressed_answer_does_not_choose(self) -> None:
        manager, history, started, _t = self._manager([
            self.Past("aaa", "caching the model"),
            self.Past("bbb", "caching build artifacts"),
        ])
        assert manager.handle_transcript("continue caching")
        await asyncio.sleep(0.05)
        assert manager.handle_transcript("second", addressed=None) is False
        await asyncio.sleep(0.05)
        assert history.loaded == []
        assert started == []
        await manager.close()


class TestACrashLeavesAWayBack:
    """BSD-08, the audit's one unresolved dispute, settled by building the
    missing half: replay was process memory, so an engine restart painted
    the empty Ready surface as if no conversation had ever been happening.
    The durable record now opens `active` and settles at close — so a row
    still `active` with no live session is crash evidence."""

    class History:
        def __init__(self, rows):
            self._rows = rows

        def sessions(self, limit: int = 5):
            return self._rows[:limit]

    class Past:
        def __init__(self, status: str, title: str) -> None:
            self.status, self.title = status, title
            self.session_id = "aaaa"

    def test_an_active_record_with_no_live_session_is_reported(self) -> None:
        manager = SessionManager(
            HudWriter(SinkTransport()), lambda t: None,
            history=self.History([self.Past("active", "cold starts")]),
        )
        found = manager.interrupted_session()
        assert found is not None and found.title == "cold starts"

    def test_settled_records_are_not_crash_evidence(self) -> None:
        manager = SessionManager(
            HudWriter(SinkTransport()), lambda t: None,
            history=self.History([self.Past("settled", "done one")]),
        )
        assert manager.interrupted_session() is None


class TestCancelWordsMeetThePendingQuestion:
    """S1 from the audit's counter-pass: three cancel words — "cancel
    that", "never mind", "nevermind" — are also replacement-question NOs,
    and the question is resolved first. That order is a decision, pinned
    here: while the question is up those words decline the NEW topic and
    keep the session; the bare escape words are in neither list, so "stop"
    still ends everything even mid-question. Failing toward the session
    surviving is the cheap direction — a kept session costs a repeat of
    "stop"; a killed one costs the conversation.
    """

    class Live(Session):
        def __init__(self):
            self.cancelled = False

        async def run(self) -> None:
            while not self.cancelled:
                await asyncio.sleep(0.01)

        def cancel(self) -> None:
            self.cancelled = True

        def consume_transcript(self, text: str) -> bool:
            return True

        def direct(self, text: str) -> str | None:
            return None

    def _with_question(self):
        first = self.Live()
        manager = SessionManager(
            HudWriter(SinkTransport()),
            lambda t: first if t == "the first question" else self.Live(),
        )
        assert manager.handle_transcript("brainstorm the first question")
        assert manager.handle_transcript(
            "JOAT, brainstorm something else",
            addressed="brainstorm something else",
        )
        return manager, first

    async def test_never_mind_declines_the_new_topic_and_keeps_the_session(self) -> None:
        manager, first = self._with_question()
        assert manager.handle_transcript("JOAT, never mind", addressed="never mind")
        await asyncio.sleep(0.05)
        assert not first.cancelled
        assert manager._pending_topic == ""
        await manager.close()

    async def test_bare_stop_still_cancels_even_mid_question(self) -> None:
        manager, first = self._with_question()
        assert manager.handle_transcript("stop")
        await asyncio.sleep(0.05)
        assert first.cancelled
        await manager.close()


class TestFailuresNeverRideBackInAsPositions:
    """S3: the in-run exclusion kept a dead seat out of _transcript, but a
    resumed or branched session inherits `prior` from the durable record —
    the sink's UNFILTERED stream — so a provider's own error text rode
    back in as a quoted position (BRAINSTORM.md:53-54 forbids exactly
    that). History keeps the failure; prompts never see it."""

    async def test_a_failure_turn_in_prior_is_dropped_from_context(self) -> None:
        from joat.services.roundtable import is_failure

        from joat.services.roundtable import FAILURE_MARK
        failure = f"{FAILURE_MARK} claude — You're out of usage credits"
        assert is_failure(failure), "fixture must be a real failure shape"

        async def seat(prompt: str):
            yield "x"

        async def judge(prompt: str) -> str:
            return "AGREEMENT: 9"

        session = debate(
            SinkTransport(), pair(seat, seat), judge,
            prior=(("CODEX (proposer)", "a real position"),
                   ("CLAUDE (critic)", failure)),
        )
        carried = session.transcript()
        assert ("CODEX (proposer)", "a real position") in carried
        assert all(not is_failure(text) for _l, text in carried)


class TestTheTerminalPaintsDuringTheHandoff:
    """S5: the terminal's paint gate keyed on `sessions.active` alone, so
    tapping away to the terminal MID-Brainstorm froze the very view the
    wearer switched to — new lane output and approvals never painted until
    the session ended."""

    async def test_ownership_follows_the_handoff(self) -> None:
        from joat.services.sinks import Broadcast, HudSink

        sink = HudSink(HudWriter(SinkTransport()))
        broadcast = Broadcast([sink])
        await broadcast.open("topic", ["codex", "claude"])
        assert broadcast.terminal_owns_hud() is False
        await broadcast.show_terminal()
        assert broadcast.terminal_owns_hud() is True
        await broadcast.replay()
        assert broadcast.terminal_owns_hud() is False


class TestTheDurableRecordCarriesRealIdentity:
    """The audit's BSI-08/BSD-03/BSD-06 cluster, one root cause: the record
    API accepted session_id/parent_id/root_id/verified and the ONE
    production caller (DebateStateSink.close) passed four positional
    arguments — so every stored id was minted AFTER the fact, "branch this"
    produced children no record could trace, no record was ever `active`,
    and verified was null on every row. These run the REAL sink against the
    REAL history in a temp store and read the file back.
    """

    @staticmethod
    def _rig(tmp_path):
        from joat.services.brainstorm_history import BrainstormHistory
        from joat.services.sinks import DebateStateSink

        history = BrainstormHistory(
            path=tmp_path / "store.json", export_dir=tmp_path / "exports"
        )
        return history, DebateStateSink(history=history)

    async def test_the_record_is_active_from_open_and_settles_at_close(
        self, tmp_path
    ) -> None:
        history, sink = self._rig(tmp_path)
        sink.adopt_identity("aaaa", "", "")
        await sink.open("cold starts", ["codex", "claude"])

        live = history.load("aaaa")
        assert live is not None and live.status == "active"

        await sink.turn("CODEX (proposer)")
        await sink.chunk("cache it")
        sink.adopt_verification(True)
        await sink.close("done — converged")

        settled = history.load("aaaa")
        assert settled is not None
        assert settled.status == "settled"
        assert settled.verified is True
        assert settled.turns == (("CODEX (proposer)", "cache it"),)
        assert settled.root_id == "aaaa"

    async def test_a_cancelled_empty_session_leaves_no_husk(self, tmp_path) -> None:
        """An active record with no turns cannot be reopened into anything;
        settling it would put an untitled ghost in the picker."""
        history, sink = self._rig(tmp_path)
        sink.adopt_identity("bbbb", "", "")
        await sink.open("never spoke", ["codex", "claude"])
        await sink.close("cancelled")
        assert history.load("bbbb") is None

    async def test_a_branch_traces_to_its_parent(self, tmp_path) -> None:
        history, sink = self._rig(tmp_path)
        sink.adopt_identity("cccc", "aaaa", "aaaa")
        await sink.open("branched question", ["codex", "claude"])
        await sink.turn("CLAUDE (critic)")
        await sink.chunk("but why")
        await sink.close("done — converged")

        child = history.load("cccc")
        assert child is not None
        assert child.parent_id == "aaaa"
        assert child.root_id == "aaaa"

    async def test_the_session_hands_the_sink_its_own_identity(self) -> None:
        """End to end through DebateSession: the id the session was born
        with is the id the sink is told, not a stranger minted at close."""
        adopted: list[tuple[str, str, str]] = []

        class Spy:
            def adopt_identity(self, sid: str, pid: str, rid: str) -> None:
                adopted.append((sid, pid, rid))

            def adopt_verification(self, verified: bool | None) -> None:
                self.verified = verified

            async def open(self, topic, speakers): ...
            async def turn(self, label): ...
            async def chunk(self, text): ...
            async def close(self, verdict): ...

        async def seat(prompt: str):
            yield "a position"

        async def judge(prompt: str) -> str:
            return "AGREEMENT: 9"

        session = DebateSession(
            "t", pair(seat, seat), Spy(), judge,
            session_id="dddd", parent_id="pppp", root_id="rrrr",
        )
        await session.run()
        assert adopted == [("dddd", "pppp", "rrrr")]

    async def test_resume_continues_the_same_record(self, tmp_path) -> None:
        """docs/SESSION_MODEL.md:49 — resume, not replay-into-a-stranger.
        The continuation adopts the STORED id, so its close updates the
        original record instead of appending a duplicate."""
        from joat.services.brainstorm_history import BrainstormHistory

        history = BrainstormHistory(
            path=tmp_path / "store.json", export_dir=tmp_path / "exports"
        )
        history.record(
            "cold starts", ("codex",), (("CODEX (proposer)", "cache"),),
            "done", session_id="eeee",
        )

        captured: list[DebateSession] = []

        def continuation(topic, prior):
            async def seat(prompt: str):
                yield "x"
            async def judge(prompt: str) -> str:
                return "AGREEMENT: 9"
            session = debate(SinkTransport(), pair(seat, seat), judge, topic=topic)
            captured.append(session)
            return session

        manager = SessionManager(
            HudWriter(SinkTransport()), lambda t: None,
            history=history, continuation_factory=continuation,
            brainstorm_available=lambda: True,
        )
        assert await manager.resume_by_id("eeee")
        assert captured and captured[0].session_id == "eeee"
        assert captured[0].root_id == "eeee"
        await manager.close()


class TestTheTapOnABusyTerminalDoesSomething:
    """The dead-gesture half of the switch. handle_brainstorm_action
    reached `if active is None: return` for show_brainstorm, so a tap on a
    busy Agent Terminal with no Brainstorm running produced NOTHING — on
    the one surface whose whole job is control. Verified through the
    production manager, not the roster in isolation.
    """

    async def test_it_cycles_the_terminal_when_no_brainstorm_is_live(self) -> None:
        cycled: list[str] = []

        async def cycle() -> str:
            cycled.append("next")
            return "hermes · idle"

        manager = SessionManager(
            HudWriter(SinkTransport()), lambda t: None,
            terminal_cycle=cycle,
        )
        assert manager.active is False
        manager.handle_brainstorm_action(
            BrainstormAction(action="show_brainstorm", surface="agent_terminal")
        )
        for _ in range(20):
            await asyncio.sleep(0.01)
            if cycled:
                break
        assert cycled == ["next"], "the tap did nothing, as before"
        await manager.close()

    async def test_a_live_brainstorm_still_wins_the_tap(self) -> None:
        """With a session to go back to, the tap means what it always
        meant — returning to the room, not switching lanes."""
        cycled: list[str] = []

        async def cycle() -> str:
            cycled.append("next")
            return "hermes"

        replayed: list[bool] = []

        class Live(Session):
            async def run(self) -> None:
                await asyncio.sleep(0.5)

            def cancel(self) -> None: ...

            def consume_transcript(self, text: str) -> bool:
                return True

            def direct(self, text: str) -> str | None:
                return None

            async def replay_hud(self) -> bool:
                replayed.append(True)
                return True

            async def handle_brainstorm_action(self, action) -> bool:
                if action.action == "show_brainstorm":
                    return await self.replay_hud()
                return False

        manager = SessionManager(
            HudWriter(SinkTransport()), lambda t: Live(),
            terminal_cycle=cycle,
        )
        assert manager.handle_transcript("brainstorm a live one")
        await asyncio.sleep(0.02)
        manager.handle_brainstorm_action(
            BrainstormAction(action="show_brainstorm", surface="agent_terminal")
        )
        for _ in range(20):
            await asyncio.sleep(0.01)
            if replayed:
                break
        assert replayed == [True]
        assert cycled == [], "it switched lanes instead of returning"
        await manager.close()


class TestTheSeatSeesWhatWasAsked:
    """P3's "ask Codex if that's true", end to end: the nomination routes
    the floor AND the question lands in the transcript the next prompt is
    built from. Verified through the real prompt builder, because the
    defect was invisible anywhere else — the floor moved correctly and the
    collaborator simply answered the wrong thing."""

    async def test_the_question_reaches_the_next_prompt(self) -> None:
        from joat.services.roundtable import build_prompt

        async def seat(prompt: str):
            yield "x"

        async def judge(prompt: str) -> str:
            return "AGREEMENT: 9"

        table = pair(seat, seat)
        session = debate(SinkTransport(), table, judge, topic="caching")
        session._transcript.append(("CODEX (proposer)", "cache the synthesis"))

        claimed = session.consume_transcript("ask Codex if that's true")
        assert claimed is False, "the direction must still route to direct()"

        turns = session.transcript()
        assert ("YOU", "if that's true") in turns
        prompt = build_prompt("caching", table._participants[0], turns)
        assert "if that's true" in prompt

    async def test_a_bare_nomination_adds_no_turn(self) -> None:
        async def seat(prompt: str):
            yield "x"

        async def judge(prompt: str) -> str:
            return "AGREEMENT: 9"

        session = debate(SinkTransport(), pair(seat, seat), judge)
        assert session.consume_transcript("codex, go") is False
        assert session.transcript() == ()


class TestWhereTheSecondsActuallyGo:
    """P5: "JOAT is fucking slow" was about a Brainstorm TURN — two
    subscription CLIs, a verifier and a judge per round — and nothing
    measured it. voice_latency.py covers endpointing/STT/first-reply; the
    Brainstorm phases were timed once, by hand, in a session now past. An
    unmeasured cost regresses silently.

    Content-free by construction (docs/BRAINSTORM.md:125-128): fixed phase
    names the class already paints, plus elapsed floats.
    """

    async def test_a_finished_session_reports_where_its_time_went(self) -> None:
        async def seat(prompt: str):
            yield "a position"

        async def judge(prompt: str) -> str:
            return "AGREEMENT: 0.95\nBLOCKING: no"

        session = debate(SinkTransport(), pair(seat, seat), judge)
        await session.run()

        timings = session.phase_timings()
        assert timings, "no phase was timed"
        # The phases this session actually runs, all fixed names.
        assert {"opening", "independent"} <= set(timings)
        assert all(isinstance(v, float) and v >= 0 for v in timings.values())

    async def test_the_timings_carry_no_wearer_content(self) -> None:
        """The keys are a closed set of phase names; a topic or a turn
        appearing here would put conversation into a diagnostic."""
        async def seat(prompt: str):
            yield "a very distinctive position about caching"

        async def judge(prompt: str) -> str:
            return "AGREEMENT: 0.95\nBLOCKING: no"

        session = debate(
            SinkTransport(), pair(seat, seat), judge,
            topic="a very distinctive topic",
        )
        await session.run()

        # Read from the class, not re-listed from memory: this assertion
        # first shipped missing "meta_review", so it would have passed a
        # session that leaked a key it did not expect.
        assert set(session.phase_timings()) <= set(DebateSession.PHASES), (
            session.phase_timings()
        )


def test_every_emitted_phase_name_is_declared() -> None:
    """The timings are a diagnostic, so their KEYS are a privacy boundary:
    what may appear must be a closed, declared set. Parsed from the AST
    rather than grepped — a first attempt with a regex missed
    `_set_phase("independent" if round_no == 1 else "cross_review")`
    entirely, reporting a disagreement that did not exist. Searching text
    is not reading it.
    """
    import ast
    from pathlib import Path

    from joat.services.session import DebateSession

    source = (Path(__file__).resolve().parents[2]
              / "apps/mac/joat/services/session.py").read_text()
    emitted: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if not (isinstance(target, ast.Attribute) and target.attr == "_set_phase"):
            continue
        for argument in node.args:
            for inner in ast.walk(argument):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    emitted.add(inner.value)

    assert emitted, "no _set_phase call found — has it been renamed?"
    assert emitted == set(DebateSession.PHASES), (
        f"emitted-only: {sorted(emitted - set(DebateSession.PHASES))}, "
        f"declared-only: {sorted(set(DebateSession.PHASES) - emitted)}"
    )


class TestTheEscapeHatchBeatsAHungCollaborator:
    """The wearer's safety valve, against the case that actually traps
    them. A cooperative seat notices `_cancelled` between chunks; a HUNG
    one never yields, so nothing in the session loop can observe the flag
    — the release depends on the manager cancelling the task itself.

    Worth its own test because the failure is indistinguishable from the
    feature working until it happens: every cooperative-seat test passes
    either way, and the wearer discovers it while trapped watching two
    subscription CLIs bill.
    """

    @staticmethod
    def _manager_with_hung_seats():
        async def hung(prompt: str):
            await asyncio.sleep(600)   # never answers
            yield "never"

        async def judge(prompt: str) -> str:
            return "AGREEMENT: 0.9\nBLOCKING: no"

        def factory(topic: str):
            return debate(SinkTransport(), pair(hung, hung), judge, topic=topic)

        return SessionManager(HudWriter(SinkTransport()), factory)

    async def test_stop_releases_a_session_whose_seats_never_answer(self) -> None:
        manager = self._manager_with_hung_seats()
        assert manager.handle_transcript("brainstorm something")
        await asyncio.sleep(0.05)
        assert manager.active is True

        assert manager.handle_transcript("stop")
        for _ in range(60):
            await asyncio.sleep(0.05)
            if not manager.active:
                break
        assert manager.active is False, "the wearer is trapped"
        await manager.close()

    async def test_close_releases_a_hung_session_too(self) -> None:
        """Shutdown has the same problem: a hung seat must not hold the
        engine open while transports are torn down."""
        manager = self._manager_with_hung_seats()
        assert manager.handle_transcript("brainstorm something")
        await asyncio.sleep(0.05)
        await asyncio.wait_for(manager.close(), timeout=10)
        assert manager.active is False
