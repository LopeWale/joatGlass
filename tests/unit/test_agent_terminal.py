"""Agent Terminal roster: choose, watch, steer, cancel — never Brainstorm.

The transport underneath is the already-verified OpenClaw adapter; these
tests fake it at the seam the roster actually uses (`stream`), and pin the
wearer-facing contract: focus switching never stops work, two lanes run at
once, output lands in a bounded tail, cancel is reported honestly, and the
spoken grammar is prefix-gated so overheard speech can never become
terminal input.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from joat.services.agent_terminal import AgentTerminalCommands, AgentTerminalRoster


class FakeAgent:
    def __init__(self, chunks: list[str] | None = None,
                 gate: asyncio.Event | None = None,
                 fail: str | None = None) -> None:
        self.prompts: list[str] = []
        self._chunks = chunks or ["line one\n", "line two\n"]
        self._gate = gate
        self._fail = fail

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        self.prompts.append(prompt)
        if self._fail:
            raise RuntimeError(self._fail)
        for c in self._chunks:
            yield c
        if self._gate is not None:
            await self._gate.wait()


def roster(**lanes: FakeAgent) -> AgentTerminalRoster:
    return AgentTerminalRoster(list(lanes.items()))


class TestRoster:
    async def test_lists_lanes_and_marks_focus(self) -> None:
        r = roster(hermes=FakeAgent(), calendar=FakeAgent())
        view = r.overview()
        assert "▸ hermes" in view and "  calendar" in view.replace("calendar", " calendar")
        assert r.focus("calendar") is True
        assert "▸ calendar" in r.overview()

    async def test_no_lanes_says_so_plainly(self) -> None:
        r = AgentTerminalRoster([])
        assert "No terminal sessions" in r.overview()
        assert "No terminal session is focused." == await r.send("hi")

    async def test_send_streams_into_the_tail(self) -> None:
        agent = FakeAgent(["build ok\n", "3 warnings\n"])
        r = roster(hermes=agent)
        receipt = await r.send("rebuild the index")
        assert receipt == "hermes ▸ sent."
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert agent.prompts == ["rebuild the index"]
        tail = r.tail()
        assert "> rebuild the index" in tail       # input is part of history
        assert "build ok" in tail and "3 warnings" in tail

    async def test_one_in_flight_turn_per_lane_is_refused_not_queued(self) -> None:
        gate = asyncio.Event()
        r = roster(hermes=FakeAgent(gate=gate))
        await r.send("first")
        await asyncio.sleep(0)
        refusal = await r.send("second")
        assert "still working" in refusal
        gate.set()

    async def test_two_lanes_work_concurrently_and_focus_never_stops_work(self) -> None:
        gate = asyncio.Event()
        slow, fast = FakeAgent(gate=gate), FakeAgent(["done\n"])
        r = roster(hermes=slow, calendar=fast)
        await r.send("long job")            # hermes, focused
        assert r.focus("calendar") is True  # switch away — T3
        await r.send("quick job")
        await asyncio.sleep(0); await asyncio.sleep(0)
        assert "done" in r.tail("calendar")
        assert slow.prompts == ["long job"]  # still running, untouched
        gate.set()

    async def test_cancel_is_honest_about_recovery(self) -> None:
        gate = asyncio.Event()
        r = roster(hermes=FakeAgent(gate=gate))
        await r.send("never finishes")
        await asyncio.sleep(0)
        report = await r.cancel()
        assert "re-verify" in report          # never 'stopped cleanly'
        assert "[cancelled]" in r.tail()

    async def test_a_failed_lane_keeps_the_reason_in_its_tail(self) -> None:
        r = roster(hermes=FakeAgent(fail="gateway said no"))
        await r.send("doomed")
        await asyncio.sleep(0); await asyncio.sleep(0)
        assert "[!] gateway said no" in r.tail()
        assert "needs recovery" in r.overview()


class TestSpokenGrammar:
    async def test_the_roster_and_switching(self) -> None:
        c = AgentTerminalCommands(roster(hermes=FakeAgent(), calendar=FakeAgent()))
        assert "hermes" in (await c.handle_command("terminal sessions") or "")
        assert "Focused on calendar." == await c.handle_command("switch terminal to calendar")

    async def test_input_requires_the_surface_name_first(self) -> None:
        agent = FakeAgent()
        c = AgentTerminalCommands(roster(hermes=agent))
        assert await c.handle_command("terminal run the tests again") is not None
        await asyncio.sleep(0)
        assert agent.prompts == ["run the tests again"]

    async def test_ordinary_speech_is_left_alone(self) -> None:
        # The ambient-capture hazard: free speech must never become
        # terminal input. Everything without the surface name declines.
        agent = FakeAgent()
        c = AgentTerminalCommands(roster(hermes=agent))
        for said in ("run the tests again", "what is the weather",
                     "cancel my meeting", "switch to decaf"):
            assert await c.handle_command(said) is None
        assert agent.prompts == []

    async def test_cancel_and_tail_phrasings(self) -> None:
        gate = asyncio.Event()
        c = AgentTerminalCommands(roster(hermes=FakeAgent(gate=gate)))
        await c.handle_command("terminal do something slow")
        await asyncio.sleep(0)
        assert "cancelled" in (await c.handle_command("terminal cancel") or "")
        tail = await c.handle_command("terminal tail")
        assert tail is not None and "> do something slow" in tail


class ApprovingAgent(FakeAgent):
    """A work-lane fake: carries resolve_approval with a scripted receipt."""

    def __init__(self, recorded: str | None = "deny") -> None:
        super().__init__()
        self.resolved: list[tuple[str, str, str]] = []
        self._recorded = recorded

    async def resolve_approval(self, approval_id: str, kind: str,
                               decision: str) -> str | None:
        self.resolved.append((approval_id, kind, decision))
        return self._recorded if self._recorded is not None else None


class TestApprovals:
    async def test_an_approval_lands_in_the_roster_and_the_tail(self) -> None:
        r = roster(work=ApprovingAgent())
        r.note_approval("work", "appr-1", "exec", "rm -rf build/")
        assert "1 approval(s) waiting" in r.overview()
        assert "rm -rf build/" in r.tail()
        assert "terminal approve" in r.tail()   # the tail tells you how to act

    async def test_deny_reports_the_recorded_decision(self) -> None:
        agent = ApprovingAgent(recorded="deny")
        r = roster(work=agent)
        r.note_approval("work", "appr-1", "exec", "rm -rf build/")
        report = await r.decide("deny")
        assert agent.resolved == [("appr-1", "exec", "deny")]
        assert "recorded deny" in report
        assert "0" not in r.overview() and "waiting" not in r.overview()

    async def test_first_answer_wins_is_reported_honestly(self) -> None:
        # The gateway records the FIRST answer; if another controller beat
        # us, the wearer hears what was recorded, not what we sent.
        r = roster(work=ApprovingAgent(recorded="approve"))
        r.note_approval("work", "appr-1", "exec", "push to main")
        report = await r.decide("deny")
        assert "another controller answered first" in report
        assert "recorded approve" in report

    async def test_two_pending_approvals_require_a_number(self) -> None:
        agent = ApprovingAgent()
        r = roster(work=agent)
        r.note_approval("work", "a1", "exec", "first")
        r.note_approval("work", "a2", "web", "second")
        report = await r.decide("approve")
        assert "terminal approve 1" in report and agent.resolved == []
        numbered = await r.decide("approve", 2)
        assert agent.resolved == [("a2", "web", "approve")]
        assert "second" in numbered

    async def test_a_lane_without_the_approvals_surface_says_so(self) -> None:
        r = roster(hermes=FakeAgent())   # brainstorm-style lane: no resolver
        r.note_approval("hermes", "a1", "exec", "anything")
        assert "cannot answer approvals" in await r.decide("approve")

    async def test_grammar_routes_decisions_and_the_pending_list(self) -> None:
        agent = ApprovingAgent(recorded="approve")
        r = roster(work=agent)
        c = AgentTerminalCommands(r)
        r.note_approval("work", "a1", "exec", "compile the kernel")
        pending = await c.handle_command("terminal approvals")
        assert pending is not None and "compile the kernel" in pending
        report = await c.handle_command("terminal approve")
        assert report is not None and "recorded approve" in report
        # 'reject' is spoken language for deny
        r.note_approval("work", "a2", "exec", "second thing")
        agent2_report = await c.handle_command("terminal reject")
        assert agent.resolved[-1][2] == "deny"
        assert agent2_report is not None


class ListingAgent(FakeAgent):
    def __init__(self, rows: list[dict[str, str]] | None = None,
                 fail: str | None = None) -> None:
        super().__init__()
        self._rows = rows if rows is not None else []
        self._list_fail = fail

    async def list_sessions(self, limit: int = 20) -> list[dict[str, str]]:
        if self._list_fail:
            raise RuntimeError(self._list_fail)
        return self._rows


class TestDiscovery:
    async def test_lists_the_wearers_gateway_sessions(self) -> None:
        r = roster(work=ListingAgent([
            {"key": "agent:hermes:x1", "label": "deploy pipeline", "sessionId": "s1"},
            {"key": "agent:hermes:x2", "label": "", "sessionId": "s2"},
        ]), hermes=FakeAgent())
        view = await r.discover()
        assert "deploy pipeline" in view
        assert "agent:hermes:x2" in view   # unlabeled rows show their key

    async def test_no_enumeration_lane_is_a_plain_answer(self) -> None:
        r = roster(hermes=FakeAgent())     # brainstorm-style lane only
        assert "No terminal lane" in await r.discover()

    async def test_listing_failure_names_the_reason(self) -> None:
        r = roster(work=ListingAgent(fail="scope missing"))
        assert "scope missing" in await r.discover()

    async def test_grammar_routes_discovery(self) -> None:
        c = AgentTerminalCommands(roster(work=ListingAgent(
            [{"key": "agent:hermes:x1", "label": "nightly build", "sessionId": "s"}])))
        view = await c.handle_command("terminal discover")
        assert view is not None and "nightly build" in view


class ResumingAgent(ListingAgent):
    def __init__(self, rows: list[dict[str, str]], fail: str | None = None) -> None:
        super().__init__(rows)
        self.resumed: list[str] = []
        self._resume_fail = fail

    async def resume_session(self, session_key: str) -> None:
        if self._resume_fail:
            raise RuntimeError(self._resume_fail)
        self.resumed.append(session_key)


ROWS = [
    {"key": "agent:hermes:k1", "label": "nightly build", "sessionId": "s1"},
    {"key": "agent:hermes:k2", "label": "deploy pipeline", "sessionId": "s2"},
]


class TestResume:
    async def test_resume_names_a_discovered_row(self) -> None:
        agent = ResumingAgent(ROWS)
        r = roster(work=agent)
        await r.discover()
        report = await r.resume("deploy pipeline")
        assert agent.resumed == ["agent:hermes:k2"]
        assert "resumed 'deploy pipeline'" in report
        assert "[resumed] deploy pipeline" in r.tail()

    async def test_resume_before_discover_says_so(self) -> None:
        r = roster(work=ResumingAgent(ROWS))
        assert "terminal discover" in await r.resume("anything")

    async def test_an_unlisted_name_is_refused(self) -> None:
        agent = ResumingAgent(ROWS)
        r = roster(work=agent)
        await r.discover()
        assert "No listed session matches" in await r.resume("some other job")
        assert agent.resumed == []      # never a key spoken into existence

    async def test_a_failed_resume_keeps_the_reason(self) -> None:
        agent = ResumingAgent(ROWS, fail="scope missing")
        r = roster(work=agent)
        await r.discover()
        assert "scope missing" in await r.resume("nightly build")

    async def test_resume_clears_the_previous_sessions_state(self) -> None:
        agent = ResumingAgent(ROWS)
        r = roster(work=agent)
        await r.discover()
        r.note_approval("work", "a1", "exec", "stale request")
        await r.resume("nightly build")
        # Approvals and output belonged to the OLD session; carrying them
        # over would let the wearer answer the wrong session's question.
        assert "approval(s) waiting" not in r.overview()
        assert "stale request" not in r.tail()

    async def test_grammar_routes_resume(self) -> None:
        agent = ResumingAgent(ROWS)
        c = AgentTerminalCommands(roster(work=agent))
        await c.handle_command("terminal discover")
        report = await c.handle_command("terminal resume nightly build")
        assert report is not None and agent.resumed == ["agent:hermes:k1"]
