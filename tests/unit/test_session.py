"""SessionManager + DebateSession tests with fake agents and HUD."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from jarvis.core.interfaces import GlassesTransport, ReplyEngine
from jarvis.core.protocol import ContainerId, DownlinkMessage, HudText
from jarvis.services.hud import HudWriter
from jarvis.services.session import DebateSession, Session, SessionManager


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


def hud_texts(transport: SinkTransport) -> list[tuple[str, bool, int]]:
    return [(m.text, m.append, int(m.container_id))
            for m in transport.sent if isinstance(m, HudText)]


class TestDebateSession:
    async def test_turns_stream_in_order_visibly(self) -> None:
        transport = SinkTransport()
        session = DebateSession(
            "tabs or spaces", HudWriter(transport),
            agent(["Use spaces. ", "Simple."]), agent(["Weak claim: simplicity."]),
            OneShotJudge(),
        )
        await session.run()
        texts = [t for t, _a, c in hud_texts(transport) if c == int(ContainerId.MAIN)]
        joined = "".join(texts)
        # order: proposer header+chunks, critic header+chunks, judge header+verdict
        assert joined.index("PROPOSER") < joined.index("Use spaces.")
        assert joined.index("Use spaces.") < joined.index("CRITIC")
        assert joined.index("CRITIC") < joined.index("Weak claim")
        assert joined.index("Weak claim") < joined.index("JUDGE")
        assert "Verdict: proposal stands." in joined
        # streaming means appends, not one repaint
        appends = [t for t, a, c in hud_texts(transport) if a]
        assert len(appends) >= 5

    async def test_cancel_stops_midstream(self) -> None:
        transport = SinkTransport()

        class Sess:
            pass

        session = DebateSession(
            "x", HudWriter(transport), agent(["a", "b", "c"]), agent(["never"]), None,
        )

        async def cancelling_stream(prompt: str) -> AsyncIterator[str]:
            yield "first"
            session.cancel()
            yield "second"

        session._propose = cancelling_stream  # type: ignore[method-assign]
        await session.run()
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        assert "never" not in joined
        assert "cancelled" in joined


class TestSessionManager:
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

    async def test_factory_declining_falls_through(self) -> None:
        mgr = SessionManager(HudWriter(SinkTransport()), lambda topic: None)
        assert mgr.handle_transcript("debate anything") is False


class ScriptedGate:
    """Deny first verify() call, pass afterwards."""

    def __init__(self) -> None:
        self.calls = 0

    async def verify(self, citations):  # duck-typed as VerifierGate
        from jarvis.verifier.gate import CitationStatus, CitationVerdict, Verdict
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

        session = DebateSession(
            "topic", HudWriter(transport),
            proposer([CITED, CITED]),          # original then repair
            agent(["no factual claims here"]),  # critic cites nothing
            None,
            gate=gate,  # type: ignore[arg-type]
        )
        await session.run()
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        # visible order: turn, verifier deny, repair turn, verifier pass
        assert joined.index("» PROPOSER") < joined.index("FAIL — quote not found")
        assert joined.index("DENIED") < joined.index("repair 1")
        assert joined.index("repair 1") < joined.index("[1] OK")
        assert "PASS" in joined
        # the repair prompt carried the verifier's complaint
        assert any("DENIED — fix [1]" in p for p in prompts[1:])
        # critic with no citations: verifier says so, does not deny
        assert "no sources cited" in joined
        assert gate.calls == 2

    async def test_uncited_turns_flow_straight_through(self) -> None:
        transport = SinkTransport()
        gate = ScriptedGate()
        session = DebateSession(
            "topic", HudWriter(transport),
            agent(["pure opinion"]), agent(["also opinion"]), None,
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

        return DebateSession(
            "topic", HudWriter(transport), propose, agent(["critique text"]),
            judge, max_rounds=2,
        )

    async def test_low_agreement_runs_second_round_then_converges(self) -> None:
        transport = SinkTransport()
        judge = ScriptedJudge([
            "Not aligned yet.\nAGREEMENT: 0.40\nBLOCKING: yes",
            "Aligned now.\nAGREEMENT: 0.95\nBLOCKING: no",
        ])
        await self._session(judge, transport, ["v1 proposal", "v2 proposal"]).run()
        joined = "".join(t for t, _a, _c in hud_texts(transport))
        assert "PROPOSER — round 2" in joined
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
        from jarvis.services.session import parse_judgment
        assert parse_judgment("x\nAGREEMENT: 0.85\nBLOCKING: no") == (0.85, False)
        assert parse_judgment("agreement: 0.4\nblocking: YES") == (0.4, True)

    def test_malformed_never_green_lights(self) -> None:
        from jarvis.services.session import parse_judgment
        agreement, blocking = parse_judgment("great job everyone")
        assert agreement is None and blocking is True
