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
from joat.services.hud import HudWriter
from joat.services.roundtable import Participant, RoundTable
from joat.services.session import DebateSession, Session, SessionManager
from joat.services.sinks import HudSink


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

        assert phases == ["opening", "turn", "closing"]

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

    async def test_each_speaker_sees_every_prior_turn(self) -> None:
        """Sequencing is structural: turn N+1's prompt CONTAINS turn N, so a
        parallel debate could not produce this transcript at all."""
        transport = SinkTransport()
        seen: list[str] = []

        def recorder(reply: str):
            async def stream(prompt: str) -> AsyncIterator[str]:
                seen.append(prompt)
                yield reply
            return stream

        seats = table(
            ("codex", "proposer", recorder("gas is cheaper")),
            ("claude", "critic", recorder("only near a pipeline")),
            ("openclaw", "third opinion", recorder("both ignore permitting")),
        )
        await debate(transport, seats, None, topic="heat pumps").run()
        assert "gas is cheaper" not in seen[0]          # opener sees no room
        assert "gas is cheaper" in seen[1]              # critic sees the proposal
        assert "gas is cheaper" in seen[2] and "only near a pipeline" in seen[2]

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
        await debate(transport, seats, None).run()
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        assert "out of usage credits" in joined      # the wearer SEES it
        assert "Cache the model in RAM." in seen[2]  # openclaw got the real turn
        assert "usage credits" not in seen[2]        # and not the failure

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
    async def test_glasses_begin_action_keeps_a_typed_brainstorm_capture_frame_visible(self) -> None:
        transport = SinkTransport()
        capture = BrainstormHudView(
            screen="brainstorm",
            state="ready",
            topic="Speak your question",
            collaborators=["Codex", "Claude", "Hermes"],
            activity='Say “JOAT, brainstorm …” to begin.',
            terminal_state="ready",
            endpoint_host="home-mac.example-tailnet.ts.net",
            no_tools_checked=True,
        )
        manager = SessionManager(
            HudWriter(transport),
            lambda _topic: None,
            topic_capture_view=lambda: capture,
        )

        manager.handle_brainstorm_action(BrainstormAction("begin_topic_capture"))
        await asyncio.sleep(0)

        assert not manager.active
        assert brainstorm_frames(transport) == [capture]
        assert not any(isinstance(message, HudText) for message in transport.sent)

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

        manager.notify("Dele · Messages\nOn my way")
        await asyncio.sleep(0)

        cards = [m for m in transport.sent if isinstance(m, HudText)]
        assert len(cards) == 1
        assert cards[0].content_kind == "notification"

    async def test_notify_and_wait_completes_only_after_hud_write(self) -> None:
        gate = asyncio.Event()

        class GatedTransport(SinkTransport):
            async def send(self, message: DownlinkMessage) -> None:
                await gate.wait()
                await super().send(message)

        transport = GatedTransport()
        manager = SessionManager(HudWriter(transport), lambda _topic: None)
        delivery = asyncio.create_task(manager.notify_and_wait("Ada · Messages\nHi"))
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
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        assert joined.count("no sources cited") == 2


class ScriptedJudge(ReplyEngine):
    """Emit scripted judgments per round."""

    def __init__(self, judgments: list[str]) -> None:
        self._judgments = iter(judgments)

    async def reply(self, text: str) -> str | None:
        return next(self._judgments)


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
