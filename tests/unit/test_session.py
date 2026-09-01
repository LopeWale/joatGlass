"""SessionManager + DebateSession tests with fake agents and HUD.

The debate is driven by a RoundTable of N participants, so these run it
through the real HudSink — what the assertions read is what the glasses
would have been sent.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from joat.core.interfaces import GlassesTransport, ReplyEngine
from joat.core.protocol import BrainstormAction, BrainstormHudView, ContainerId, DownlinkMessage, HudText
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
        assert frames[0].screen == "brainstorm"
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
        capture = BrainstormHudView(
            screen="brainstorm",
            state="topic_capture",
            topic="Speak your question",
            collaborators=["Codex", "Claude", "Hermes"],
            activity="Your next spoken phrase starts this Brainstorm.",
            terminal_state="ready",
            endpoint_host="home-mac.example-tailnet.ts.net",
            no_tools_checked=True,
        )
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
            topic_capture_view=lambda armed: capture if armed else BrainstormHudView(
                screen="brainstorm",
                state="ready",
                topic="Ready for a question",
                collaborators=capture.collaborators,
                activity="Tap to choose a question, then speak.",
                terminal_state="ready",
                no_tools_checked=True,
            ),
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

    async def test_brainstorm_requires_hermes_and_recovers_on_the_terminal_surface(self) -> None:
        """Neither a spoken command nor a tap may silently use local seats."""
        transport = SinkTransport()
        terminal_recovery = BrainstormHudView(
            screen="agent_terminal",
            state="approval_required",
            topic="Hermes Agent Terminal",
            collaborators=[],
            activity="Hermes is required for Brainstorm. Tap to retry.",
            terminal_state="approval_required",
            terminal_reason="Approve the JOAT device in OpenClaw, then tap retry.",
            no_tools_checked=False,
        )
        factory_topics: list[str] = []
        manager = SessionManager(
            HudWriter(transport),
            lambda topic: factory_topics.append(topic) or None,
            topic_capture_view=lambda _armed: terminal_recovery,
            brainstorm_available=lambda: False,
        )

        assert manager.handle_transcript(
            "brainstorm compare home brain options",
            capture_source="glasses",
        )
        await asyncio.sleep(0)
        assert factory_topics == []
        assert not manager.topic_capture_armed
        assert brainstorm_frames(transport) == [terminal_recovery]

        manager.handle_brainstorm_action(BrainstormAction("begin_topic_capture"))
        await asyncio.sleep(0)
        assert factory_topics == []
        assert not manager.topic_capture_armed
        assert brainstorm_frames(transport) == [terminal_recovery, terminal_recovery]

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
        assert frame.screen == "brainstorm"          # not a Hermes recovery card
        assert "Hermes" not in frame.activity
        assert frame.collaborators == ["Codex", "Claude"]

        # A terminal mid-response is likewise irrelevant to a Brainstorm.
        status.set_agent_terminal(state="running", reason="")
        await router.open("Brainstorm")

        running = brainstorm_frames(transport)[-1]
        assert running.screen == "brainstorm"
        assert "Hermes" not in running.activity

    async def test_topic_capture_deduplicates_taps_and_clears_on_cancel_or_reconnect(self) -> None:
        transport = SinkTransport()
        capture = BrainstormHudView(
            screen="brainstorm", state="topic_capture", topic="Speak your question",
            collaborators=["Codex", "Claude", "Hermes"], activity="Your next spoken phrase starts this Brainstorm.",
            terminal_state="ready", no_tools_checked=True,
        )
        ready = BrainstormHudView(
            screen="brainstorm", state="ready", topic="Ready for a question",
            collaborators=capture.collaborators, activity="Tap to choose a question, then speak.",
            terminal_state="ready", no_tools_checked=True,
        )
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
        ready = BrainstormHudView(
            screen="brainstorm", state="ready", topic="Ready for a question",
            collaborators=["Codex", "Claude", "Hermes"],
            activity="Tap to choose a question, then speak.", terminal_state="unavailable",
            no_tools_checked=True,
        )
        manager = SessionManager(
            HudWriter(transport),
            lambda _topic: None,
            topic_capture_view=lambda armed: BrainstormHudView(
                screen="brainstorm", state="topic_capture", topic="Speak your question",
                collaborators=ready.collaborators,
                activity="Your next spoken phrase starts this Brainstorm.",
                terminal_state="unavailable", no_tools_checked=True,
            ) if armed else ready,
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
        capture = BrainstormHudView(
            screen="brainstorm", state="topic_capture", topic="Speak your question",
            collaborators=["Codex", "Claude", "Hermes"],
            activity="Your next spoken phrase starts this Brainstorm.",
            terminal_state="ready", no_tools_checked=True,
        )
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
        assert joined.index("DENIED") < joined.index("CODEX — repair 1")
        assert joined.index("CODEX — repair 1") < joined.index("[1] OK")
        assert "PASS" in joined
        # the repair prompt carried the verifier's complaint
        assert any("DENIED — fix [1]" in p for p in prompts[1:])
        # critic with no citations: verifier says so, does not deny
        assert "no sources cited" in joined
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
        # Every G2 paint is a complete rolling terminal tail, so combining
        # all historical frames would count an earlier verifier row again on
        # later paints. One live tail must still retain the two distinct
        # verifier outcomes.
        visible_frames = [text for text, _a, _c in hud_texts(transport)]
        assert max(frame.count("no sources cited") for frame in visible_frames) == 2


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

    async def test_illegible_judge_stops_without_converging(self) -> None:
        transport = SinkTransport()
        judge = ScriptedJudge(["I simply vibe with this proposal."])
        await self._session(judge, transport, ["p1"]).run()
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        assert "judge gave no score" in joined


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
