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
from collections.abc import AsyncIterator, Callable
from dataclasses import replace

import pytest

from joat.core.protocol import AgentTerminalHudView
from joat.services.agent_terminal import (
    AgentTerminalCommands, AgentTerminalRoster, LaneAddress, TerminalFrame, TerminalLane,
)


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


def roster(paint=None, **lanes: FakeAgent) -> AgentTerminalRoster:
    return AgentTerminalRoster(list(lanes.items()), paint=paint)


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

    async def test_a_second_utterance_queues_and_lands_when_the_turn_settles(self) -> None:
        """AGENT_TERMINAL_ROADMAP.md:32 — the wearer steers a RUNNING
        session. Refusing while busy left cancel as the only verb; the
        honest shape is a terminal's type-ahead: one queued line, newest
        wins, landed the moment the turn settles, receipt saying so."""
        gate = asyncio.Event()
        agent = FakeAgent(gate=gate)
        r = roster(hermes=agent)
        await r.send("first")
        await asyncio.sleep(0)

        receipt = await r.send("second")
        assert "queued" in receipt
        assert agent.prompts == ["first"], "it interleaved instead of queueing"

        replaced = await r.send("third")
        assert "replaces" in replaced

        gate.set()
        for _ in range(40):
            await asyncio.sleep(0.01)
            if "third" in agent.prompts:
                break
        assert agent.prompts == ["first", "third"], "newest wins, once"

    async def test_cancel_drops_the_queued_line(self) -> None:
        """A line landing after a cancel is a surprise, not a service."""
        gate = asyncio.Event()
        agent = FakeAgent(gate=gate)
        r = roster(hermes=agent)
        await r.send("first")
        await asyncio.sleep(0)
        await r.send("second")
        await r.cancel()
        for _ in range(20):
            await asyncio.sleep(0.01)
        assert agent.prompts == ["first"], "the queued line landed after cancel"
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

    async def test_a_background_lane_repaints_when_its_run_settles(self) -> None:
        """The run→idle transition happens AFTER the last narrated line —
        inside the task, lane.busy is still True, so the chip said "run"
        through the narration and nothing fired at completion. The strip
        showed stale state until the focused lane happened to speak."""
        frames: list[object] = []

        async def paint(frame: object) -> None:
            frames.append(frame)

        gate = asyncio.Event()
        slow = FakeAgent(gate=gate)
        r = roster(hermes=FakeAgent(["focused\n"]), calendar=slow, paint=paint)
        await r.send("background job", "calendar")   # calendar, unfocused
        await asyncio.sleep(0)
        gate.set()
        for _ in range(40):
            await asyncio.sleep(0.01)
            if not r._lanes["calendar"].busy:
                break
        await asyncio.sleep(0.05)                    # the settle callback
        assert frames, "the finish never repainted the strip"
        last = frames[-1]
        lanes = getattr(last, "lanes", [])
        assert any("calendar" in row and "idle" in row for row in lanes), lanes

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


class _RosterPolicy:
    """What the gateway actually consults before allowing a roster read.

    The production selection is `agent.policy.roster`, so a double that
    enumerates sessions must carry it — a double that only *looks* capable
    is how the real selection came to be `hasattr(agent, "list_sessions")`,
    which every lane satisfies.
    """

    roster = True


class ListingAgent(FakeAgent):
    #: Only a roster lane may enumerate; see _is_roster_lane.
    policy = _RosterPolicy()

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


class TestHudFrame:
    """The roster is visible, not merely audible.

    Before this the agent_terminal screen carried a status/retry line only,
    so the wearer could hear their sessions but never see them — on a
    surface whose whole job is watching remote work.
    """

    async def test_the_frame_carries_the_tail_not_one_line(self) -> None:
        agent = FakeAgent(["compiling\n", "linking\n", "done\n"])
        r = roster(work=agent)
        await r.send("build it")
        await asyncio.sleep(0); await asyncio.sleep(0)
        frame = r.frame()
        assert frame.lines[0] == "> build it"      # your input opens the tail
        assert "done" in frame.lines[-1]
        assert len(frame.lines) <= 7               # fits the reserved rows

    async def test_the_header_reports_lane_state_and_waiting_work(self) -> None:
        r = roster(work=ApprovingAgent())
        r.note_approval("work", "a1", "exec", "rm -rf build/")
        frame = r.frame()
        assert "1 waiting" in frame.topic
        assert frame.state == "approval_required"

    async def test_states_map_to_what_the_wearer_must_do(self) -> None:
        gate = asyncio.Event()
        r = roster(work=FakeAgent(gate=gate))
        assert r.frame().state == "ready"
        await r.send("slow")
        await asyncio.sleep(0)
        assert r.frame().state == "running"
        gate.set()

    async def test_a_failed_lane_reads_as_blocked(self) -> None:
        r = roster(work=FakeAgent(fail="gateway said no"))
        await r.send("doomed")
        await asyncio.sleep(0); await asyncio.sleep(0)
        assert r.frame().state == "blocked"

    async def test_an_empty_roster_says_so_rather_than_drawing_nothing(self) -> None:
        frame = AgentTerminalRoster([]).frame()
        assert frame.state == "unavailable"
        assert "No terminal sessions" in frame.lines[0]

    async def test_painting_happens_as_output_arrives(self) -> None:
        painted: list[int] = []

        async def paint(frame) -> None:
            painted.append(len(frame.lines))

        r = AgentTerminalRoster([("work", FakeAgent(["a\n", "b\n"]))], paint=paint)
        await r.send("go")
        await asyncio.sleep(0); await asyncio.sleep(0)
        assert painted, "the lenses were never updated"

    async def test_a_dead_surface_never_kills_the_work(self) -> None:
        async def broken(frame) -> None:
            raise RuntimeError("HUD gone")

        agent = FakeAgent(["still works\n"])
        r = AgentTerminalRoster([("work", agent)], paint=broken)
        await r.send("go")
        await asyncio.sleep(0); await asyncio.sleep(0)
        assert "still works" in r.tail()


class TestRepaintCoverage:
    """Every command that changes what the lenses should show, repaints.

    An earlier build repainted only on streamed output and approvals, so
    switching focus or resuming left the glasses showing the OLD lane —
    the worst kind of stale, because it looks live.
    """

    def _counting(self, **lanes):
        painted: list[str] = []

        async def paint(frame) -> None:
            painted.append(frame.topic)

        return AgentTerminalRoster(list(lanes.items()), paint=paint), painted

    async def test_focus_switch_repaints(self) -> None:
        r, painted = self._counting(hermes=FakeAgent(), calendar=FakeAgent())
        c = AgentTerminalCommands(r)
        await c.handle_command("switch terminal to calendar")
        assert painted and painted[-1].startswith("calendar")

    async def test_resume_repaints(self) -> None:
        r, painted = self._counting(work=ResumingAgent(ROWS))
        c = AgentTerminalCommands(r)
        await c.handle_command("terminal discover")
        await c.handle_command("terminal resume nightly build")
        assert painted, "resume never reached the lenses"

    async def test_discovery_is_readable_on_the_lenses(self) -> None:
        r = roster(work=ListingAgent(ROWS))
        await r.discover()
        # The listing lands in the tail, so the wearer can READ their
        # sessions rather than having to remember what was spoken.
        assert any("[discover]" in line for line in r.frame().lines)
        assert "nightly build" in " ".join(r.frame().lines)

    async def test_an_unclaimed_utterance_does_not_repaint(self) -> None:
        r, painted = self._counting(hermes=FakeAgent())
        c = AgentTerminalCommands(r)
        assert await c.handle_command("what is the weather") is None
        assert painted == []


class TestFrameEconomy:
    """Only what the wearer is looking at costs HUD traffic."""

    def _counting(self, **lanes):
        painted: list[str] = []

        async def paint(frame) -> None:
            painted.append(frame.topic)

        return AgentTerminalRoster(list(lanes.items()), paint=paint), painted

    async def test_a_background_lane_does_not_repaint_per_line(self) -> None:
        # An unfocused lane streaming its own output was repainting the
        # focused lane's UNCHANGED frame once per line — pure BLE traffic
        # on a link the wearer needs for what they are watching.
        #
        # It is bounded by STATE now, not silenced: the lane's chip may
        # change (idle → run → ask → err) and the wearer asked to watch
        # concurrent work, so those transitions must reach the strip. What
        # must never happen is a frame per LINE.
        r, painted = self._counting(
            work=FakeAgent(gate=asyncio.Event()),
            other=FakeAgent(["one\n", "two\n", "three\n"]),
        )
        await r.send("background job", name="other")
        await asyncio.sleep(0); await asyncio.sleep(0)
        assert "one" in r.tail("other")   # the work still happened…
        assert len(painted) <= 2, (       # …at the cost of transitions only
            "a background lane may repaint on a state change, never per line"
        )

    async def test_the_focused_lane_still_repaints_per_line(self) -> None:
        r, painted = self._counting(work=FakeAgent(["one\n", "two\n"]))
        await r.send("watched job")
        await asyncio.sleep(0); await asyncio.sleep(0)
        assert len(painted) >= 2

    async def test_discovery_lands_where_the_wearer_is_looking(self) -> None:
        # Only one lane can enumerate, so the listing used to be recorded
        # into THAT lane while the frame drew the focused one — invisible
        # whenever they differ, which is most of the time.
        r = roster(work=ListingAgent(ROWS), hermes=FakeAgent())
        assert r.focus("hermes") is True
        await r.discover()
        assert any("[discover]" in line for line in r.frame().lines)


class TestTapToSpeak:
    """A tap on the terminal arms ONE utterance as terminal input.

    It used to send begin_topic_capture — the Agent Terminal's primary
    gesture literally started a Brainstorm, a different feature with
    different collaborators.
    """

    async def test_an_armed_utterance_needs_no_prefix(self) -> None:
        agent = FakeAgent()
        c = AgentTerminalCommands(roster(work=agent))
        c.arm()
        # The gesture already established this is terminal speech, which is
        # the consent the 'terminal' prefix exists to obtain.
        assert await c.handle_command("rerun the failing test") is not None
        await asyncio.sleep(0)
        assert agent.prompts == ["rerun the failing test"]

    async def test_the_promise_is_exactly_one_utterance(self) -> None:
        agent = FakeAgent()
        c = AgentTerminalCommands(roster(work=agent))
        c.arm()
        await c.handle_command("first thing")
        await asyncio.sleep(0)
        assert c.armed is False
        # The next sentence is ordinary speech again.
        assert await c.handle_command("what is the weather") is None
        assert agent.prompts == ["first thing"]

    async def test_cancelling_the_promise_is_spoken_to_JOAT_not_hermes(self) -> None:
        agent = FakeAgent()
        c = AgentTerminalCommands(roster(work=agent))
        c.arm()
        answer = await c.handle_command("never mind")
        assert answer == "Terminal input cancelled."
        assert agent.prompts == []      # never sent INTO the session
        assert c.armed is False

    async def test_unarmed_speech_still_needs_the_prefix(self) -> None:
        agent = FakeAgent()
        c = AgentTerminalCommands(roster(work=agent))
        assert await c.handle_command("rerun the failing test") is None
        assert agent.prompts == []


class TestArmExpiry:
    """A tap buys ONE utterance, soon — not an open microphone.

    An arm that never expires is exactly the ambient-capture hazard the
    'terminal' prefix exists to prevent, reintroduced by a gesture: tap,
    get distracted, and the next thing said — minutes later, to a
    colleague — arrives at a session that can run tools.
    """

    async def test_a_stale_arm_does_not_capture_later_speech(self) -> None:
        agent = FakeAgent()
        c = AgentTerminalCommands(roster(work=agent), window_s=0.0)
        c.arm()
        assert c.armed is False                     # already lapsed
        assert await c.handle_command("tell Bob about the meeting") is None
        assert agent.prompts == []                  # nothing reached Hermes

    async def test_a_fresh_arm_still_captures(self) -> None:
        agent = FakeAgent()
        c = AgentTerminalCommands(roster(work=agent), window_s=60.0)
        c.arm()
        assert c.armed is True
        assert await c.handle_command("rerun the tests") is not None
        await asyncio.sleep(0)
        assert agent.prompts == ["rerun the tests"]

    async def test_the_window_is_bounded_by_default(self) -> None:
        c = AgentTerminalCommands(roster(work=FakeAgent()))
        assert 0 < AgentTerminalCommands.ARM_WINDOW_S <= 120
        c.arm()
        assert c.armed is True


class TestSidebarOpensTheRoster:
    """Picking 'Agent Terminal' shows the wearer's sessions.

    It used to draw a readiness card whose body read 'Hermes participates
    as a Brainstorm collaborator' — false after the boundary change, and
    useless before it: no roster, no sessions, no output. The feature was
    unreachable from the menu it was listed in.
    """

    async def test_the_roster_paints_when_the_row_is_opened(self) -> None:
        painted: list[list[str]] = []

        async def paint(frame) -> None:
            painted.append(frame.lines)

        agent = FakeAgent(["build ok\n"])
        r = AgentTerminalRoster([("work", agent)], paint=paint)
        await r.send("build it")
        await asyncio.sleep(0); await asyncio.sleep(0)
        painted.clear()
        # This is exactly what the capability row invokes.
        await r.repaint()
        assert painted, "opening the row painted nothing"
        assert any("build ok" in line for line in painted[-1])

    async def test_an_unconfigured_mac_still_answers(self) -> None:
        # With no lanes the router keeps its own fallback copy, so the row
        # must never open onto an empty frame.
        frame = AgentTerminalRoster([]).frame()
        assert frame.lines and "No terminal sessions" in frame.lines[0]


class TestSpokenCapabilityOpeners:
    """The glasses are voice-first: both features open by asking.

    The capability-open grammar covered Messages, Planning, Notifications
    and friends but neither Brainstorm nor Agent Terminal — the two
    surfaces this work owns were reachable only by tapping a sidebar row.
    """

    def _resolve(self, said: str) -> str | None:
        from joat.services.capabilities import (
            _OPEN_CAPABILITY, _SPOKEN_CAPABILITY,
        )
        match = _OPEN_CAPABILITY.fullmatch(" ".join(said.split()).rstrip(".,!?"))
        if match is None:
            return None
        return _SPOKEN_CAPABILITY[" ".join(match.group("name").lower().split())]

    def test_both_features_have_spoken_openers(self) -> None:
        assert self._resolve("open brainstorm") == "Brainstorm"
        assert self._resolve("show my brainstorm") == "Brainstorm"
        assert self._resolve("open agent terminal") == "Agent Terminal"
        assert self._resolve("show the terminal") == "Agent Terminal"

    def test_they_remain_different_features(self) -> None:
        # Nothing in the opener maps one onto the other.
        assert self._resolve("open debate") == "Brainstorm"
        assert self._resolve("open terminal") == "Agent Terminal"

    def test_the_terminal_command_grammar_is_not_shadowed(self) -> None:
        # 'terminal sessions' must still reach AgentTerminalCommands rather
        # than being swallowed as a surface-open request.
        assert self._resolve("terminal sessions") is None
        assert self._resolve("terminal cancel") is None

    def test_ordinary_speech_is_left_alone(self) -> None:
        for said in ("open the door", "show me the weather",
                     "navigate to the park", "open a new tab"):
            assert self._resolve(said) is None


class TestBrainstormRowIsNotGatedOnHermes:
    """Opening Brainstorm must not send the wearer to fix a daemon.

    _show_brainstorm refused unless the Hermes terminal was ready and
    seated Hermes beside Codex and Claude — both contradict the boundary.
    Hermes/OpenClaw are Agent Terminal workers; they neither participate
    in a Brainstorm nor gate one.
    """

    def _router(self, recap=None):
        from joat.services.capabilities import CapabilityRouter
        from joat.services.hud import HudWriter
        from joat.channels.cards import CardLog
        from tests.unit.test_session import SinkTransport

        transport = SinkTransport()

        class Status:
            def agent_terminal_projection(self):
                # The worst case: Hermes entirely unavailable.
                return {"state": "unavailable", "reason": "endpoint_missing",
                        "endpoint_host": "", "no_tools_checked": False}

        return CapabilityRouter(
            hud=HudWriter(transport), transport=transport, status=Status(),
            messages=None, cards=CardLog(),
            brainstorm_recap=recap,
        ), transport

    def _frames(self, transport):
        from joat.core.protocol import AgentTerminalHudView, BrainstormHudView  # noqa: F401
        return [m for m in transport.sent
                if type(m).__name__ == "BrainstormHudView"]

    async def test_it_opens_even_with_hermes_unavailable(self) -> None:
        router, transport = self._router()
        await router.open("Brainstorm")
        frames = self._frames(transport)
        assert frames, "Brainstorm did not open at all"
        frame = frames[-1]
        assert "Hermes" not in frame.activity
        assert "Hermes" not in frame.collaborators

    async def test_the_seats_are_codex_and_claude(self) -> None:
        router, transport = self._router()
        await router.open("Brainstorm")
        assert self._frames(transport)[-1].collaborators == ["Codex", "Claude"]

    async def test_a_finished_session_is_re_readable(self) -> None:
        from joat.services.sinks import DebateStateSink

        sink = DebateStateSink()
        await sink.open("cold-start latency", ["codex", "claude"])
        await sink.turn("CODEX (proposer)")
        await sink.chunk("keep the weights resident")
        await sink.close("converged 0.91")

        router, transport = self._router(recap=lambda: sink.snapshot)
        await router.open("Brainstorm")
        frame = self._frames(transport)[-1]
        # The synthesis the wearer walked away with, still there.
        assert "cold-start latency" in frame.topic
        assert "keep the weights resident" in frame.activity
        assert frame.state == "settled"


class TestWatchingConcurrentWork:
    """'Watch concurrent work' is in the roadmap; it needs to be visible.

    The frame listed lane NAMES only, so a background lane could finish,
    fail, or start waiting on an approval and the frame looked identical.
    """

    async def test_every_lane_carries_its_state(self) -> None:
        gate = asyncio.Event()
        r = roster(work=FakeAgent(gate=gate), calendar=FakeAgent(["done\n"]))
        await r.send("long job")                 # work: running
        await asyncio.sleep(0)
        frame = r.frame()
        assert "work·run" in frame.lanes
        assert "calendar·idle" in frame.lanes
        gate.set()

    async def test_a_waiting_lane_shows_its_count(self) -> None:
        r = roster(work=FakeAgent(), calendar=ApprovingAgent())
        r.note_approval("calendar", "a1", "exec", "deploy")
        assert "calendar·ask1" in r.frame().lanes

    async def test_a_failed_background_lane_is_visible(self) -> None:
        r = roster(work=FakeAgent(), other=FakeAgent(fail="gateway said no"))
        await r.send("doomed", name="other")
        await asyncio.sleep(0); await asyncio.sleep(0)
        assert "other·err" in r.frame().lanes


class TestBackgroundApprovalsInterrupt:
    """An approval halts its session until answered — silence hides a stall.

    Ordinary output from an unwatched lane stays quiet on purpose (BLE
    economy), but an approval is the one thing that must reach the wearer
    wherever they are looking.
    """

    async def test_a_background_approval_is_announced(self) -> None:
        said: list[str] = []

        async def announce(line: str) -> None:
            said.append(line)

        r = AgentTerminalRoster(
            [("work", FakeAgent()), ("calendar", ApprovingAgent())],
            announce=announce,
        )
        assert r.focused == "work"
        r.note_approval("calendar", "a1", "exec", "deploy to prod")
        await asyncio.sleep(0); await asyncio.sleep(0)
        assert said, "a blocking approval was never surfaced"
        assert "calendar" in said[-1] and "deploy to prod" in said[-1]
        # And it tells the wearer how to get there.
        assert "switch terminal to calendar" in said[-1]

    async def test_background_OUTPUT_stays_quiet(self) -> None:
        said: list[str] = []

        async def announce(line: str) -> None:
            said.append(line)

        r = AgentTerminalRoster(
            [("work", FakeAgent(gate=asyncio.Event())),
             ("other", FakeAgent(["chatter\n", "more chatter\n"]))],
            announce=announce,
        )
        await r.send("background", name="other")
        await asyncio.sleep(0); await asyncio.sleep(0)
        assert said == []          # not an interruption
        assert "chatter" in r.tail("other")   # but still recorded


class TestTheFrameAlwaysFitsTheWire:
    """A frame the wire rejects is a terminal that silently freezes.

    repaint() swallows sink exceptions on purpose — a dead surface must
    not kill the work — so an over-long frame does not raise anywhere the
    wearer or the log can see it. The lenses just stop updating. The frame
    must therefore be impossible to build too large, not merely unlikely.
    """

    VERBOSE = [
        "> audit every outbound HTTP call for missing timeouts and retries",
        "Scanning 1,284 files across src/ and services/ for requests/httpx usage",
        "Found 37 call sites. 12 pass no timeout at all, 9 pass a connect "
        "timeout but no read timeout, and 4 retry inside a bare except.",
        "The bare-except retries swallow asyncio.CancelledError, so a "
        "cancelled request keeps retrying forever.",
        "Writing report to docs/http-audit.md and staging fixes for the worst.",
        "Proposed diff touches 6 files (+204 -71). Approve to write?",
    ]

    def test_verbose_agent_output_still_makes_a_legal_frame(self) -> None:
        from joat.core.protocol import BrainstormHudView
        from joat.services.agent_terminal import (
            AgentTerminalRoster, HUD_COLUMNS,
        )

        r = AgentTerminalRoster([("hermes", FakeAgent())])
        lane = r._lanes["hermes"]
        for line in self.VERBOSE:
            lane.tail.append(line)
        frame = r.frame()

        assert all(len(line) <= HUD_COLUMNS for line in frame.lines)
        # The exact construction __main__.terminal_paint performs.
        AgentTerminalHudView(
             state=frame.state, focus=frame.topic,
            lanes=frame.lanes, activity="\n".join(frame.lines),
        ).to_wire()

    def test_a_gateway_session_key_cannot_overflow_a_lane_chip(self) -> None:
        """Roster lanes are named by GATEWAY keys, which we do not choose."""
        from joat.core.protocol import BrainstormHudView
        from joat.services.agent_terminal import (
            AgentTerminalRoster, HUD_LANE_COLUMNS,
        )

        key = "agent:hermes:" + "a-very-long-branch-name" * 5
        r = AgentTerminalRoster([(key, FakeAgent())])
        frame = r.frame()

        assert all(len(chip) <= HUD_LANE_COLUMNS for chip in frame.lanes)
        assert len(frame.topic) <= 96
        AgentTerminalHudView(
             state=frame.state, focus=frame.topic,
            lanes=frame.lanes, activity="\n".join(frame.lines),
        ).to_wire()

    def test_the_shipped_scrollback_fits_the_wire_bound(self) -> None:
        from joat.core.protocol import BRAINSTORM_ACTIVITY_MAX
        from joat.services.agent_terminal import HUD_COLUMNS, HUD_LINES

        # Rows, their columns, AND the newlines that separate them. The
        # bound is scrollback depth now — the wearer swipes a 12-row
        # window over it — so shipped rows must FIT, not equal.
        assert (HUD_LINES * HUD_COLUMNS + (HUD_LINES - 1)
                <= BRAINSTORM_ACTIVITY_MAX), (
            "the terminal ships more scrollback than the wire admits"
        )

    def test_a_full_frame_uses_every_row_and_still_fits(self) -> None:
        """The worst case the geometry allows must be a legal frame."""
        from joat.core.protocol import BrainstormHudView
        from joat.services.agent_terminal import (
            AgentTerminalRoster, HUD_COLUMNS, HUD_LINES,
        )

        r = AgentTerminalRoster([("hermes", FakeAgent())])
        for index in range(HUD_LINES + 5):
            r._lanes["hermes"].tail.append(f"{index}" * (HUD_COLUMNS * 2))
        frame = r.frame()

        assert len(frame.lines) == HUD_LINES, "a transcript row went unused"
        AgentTerminalHudView(
             state="running", focus=frame.topic,
            lanes=frame.lanes, activity="\n".join(frame.lines),
        ).to_wire()

    def test_a_multi_line_error_stays_one_row(self) -> None:
        """`lane.last_error` is `str(exc)`, and exception messages carry
        newlines — a traceback, a multi-line protocol complaint. Recorded
        as `[!] <that>`, one tail entry rendered as several rows, so seven
        entries could become fourteen and the renderer's tail trim would
        quietly drop half the terminal."""
        from joat.services.agent_terminal import AgentTerminalRoster, HUD_LINES

        r = AgentTerminalRoster([("hermes", FakeAgent())])
        lane = r._lanes["hermes"]
        lane.last_error = "\n".join(
            f"traceback frame {i} with explanatory detail" for i in range(14)
        )
        lane.tail.append(f"[!] {lane.last_error}")
        for i in range(HUD_LINES):
            lane.tail.append(f"line {i}\nsmuggled continuation {i}")
        frame = r.frame()

        rendered = "\n".join(frame.lines)
        assert rendered.count("\n") + 1 == len(frame.lines), (
            "a tail entry rendered as more than one row"
        )
        assert len(frame.lines) <= HUD_LINES

    def test_a_lane_chip_is_one_row_too(self) -> None:
        """The neighbour of the transcript fix, and it survived it.

        Lane names come from GATEWAY session keys, which we do not choose
        — the same provenance that made bounding their width necessary.
        A newline in one reached `collaborators`, a field the renderer
        draws on a single line as the LANES row.
        """
        from joat.core.protocol import BrainstormHudView
        from joat.services.agent_terminal import (
            AgentTerminalRoster, HUD_LANE_COLUMNS,
        )

        r = AgentTerminalRoster([("agent:hermes:build\nsecond line", FakeAgent())])
        frame = r.frame()

        assert all("\n" not in chip for chip in frame.lanes), frame.lanes
        assert all(len(chip) <= HUD_LANE_COLUMNS for chip in frame.lanes)
        AgentTerminalHudView(
             state="running", focus=frame.topic,
            lanes=frame.lanes, activity="\n".join(frame.lines),
        ).to_wire()


class TestReplyRouting:
    """JOAT is the glue: a Brainstorm reply becomes terminal input.

    The wearer's design, verbatim: "asking to take the last response from
    claude or codex and send it to the hermes in the Agent Terminal; you
    need to understand what i am trying to build here."
    """

    def _commands(self, replies=None):
        from joat.services.agent_terminal import (
            AgentTerminalCommands, AgentTerminalRoster,
        )

        roster = AgentTerminalRoster([("hermes", FakeAgent())])
        commands = AgentTerminalCommands(
            roster,
            last_reply=lambda who: replies(who) if replies else None,
        )
        return commands, roster

    def test_the_grammar_reads_natural_phrasings(self) -> None:
        from joat.services.agent_terminal import parse_reply_route

        # "Claude's" arrives from the normalizer as "claude s".
        assert parse_reply_route("send claude s last reply to hermes") == (
            "claude", "hermes")
        assert parse_reply_route("send codex s answer") == ("codex", None)
        assert parse_reply_route("send the last response to hermes") == (
            None, "hermes")
        assert parse_reply_route("send that to hermes") == (None, "hermes")
        # "to the terminal" means the focused lane, like every other verb.
        assert parse_reply_route("send claude s reply to the terminal") == (
            "claude", None)
        # Ordinary speech must not be claimed as routing.
        assert parse_reply_route("send an email to my mother") is None
        assert parse_reply_route("what should i reply") is None

    async def test_a_reply_is_sent_into_the_focused_lane(self) -> None:
        commands, roster = self._commands(
            replies=lambda who: ("CLAUDE (critic)", "Verify the cache key.")
            if who in (None, "claude") else None,
        )
        answer = await commands.handle_command("terminal send claude's last reply")
        assert answer is not None
        assert "CLAUDE" in answer
        # The reply text itself — not the spoken command — became input.
        lane = roster._lanes["hermes"]
        assert any("Verify the cache key." in line for line in lane.tail)

    async def test_no_session_yet_is_an_honest_refusal(self) -> None:
        from joat.services.agent_terminal import (
            AgentTerminalCommands, AgentTerminalRoster,
        )

        commands = AgentTerminalCommands(
            AgentTerminalRoster([("hermes", FakeAgent())]),
        )
        answer = await commands.handle_command("terminal send claude's reply")
        assert answer == "No Brainstorm has run yet — nothing to send."

    async def test_a_missing_reply_names_the_seat(self) -> None:
        commands, _ = self._commands(replies=lambda who: None)
        answer = await commands.handle_command(
            "terminal send codex's last answer")
        assert answer == "No reply from Codex to send yet."


class TestWatchingConcurrentWork:
    """The wearer's requirement, in their words: "watch concurrent work".

    `_narrate` returned before `repaint()` for any unfocused lane, so a
    background lane going idle → run → ask → err never repainted anything.
    The LANES strip — which exists so one glance covers the roster — showed
    whatever it happened to say when the FOCUSED lane last spoke.
    """

    def _roster(self):
        from joat.services.agent_terminal import AgentTerminalRoster

        painted: list[int] = []

        async def paint(frame) -> None:
            painted.append(len(frame.lanes))

        roster = AgentTerminalRoster(
            [("work", FakeAgent()), ("calendar", FakeAgent())], paint=paint,
        )
        return roster, painted

    async def test_a_background_lane_repaints_when_its_state_changes(self) -> None:
        roster, painted = self._roster()
        background = roster._lanes["calendar"]
        assert roster.focused == "work"

        before = len(painted)
        await roster._narrate(background, "starting up")
        assert len(painted) > before, (
            "a background lane's first output changes its chip from idle to "
            "run, and the strip must show it"
        )

    async def test_a_background_lane_does_not_repaint_per_line(self) -> None:
        """The reason the early return existed is still valid: streaming
        output must not spend a frame per line on a link the wearer needs."""
        roster, painted = self._roster()
        background = roster._lanes["calendar"]

        await roster._narrate(background, "first")     # idle -> its state
        settled = len(painted)
        for line in ("second", "third", "fourth", "fifth"):
            await roster._narrate(background, line)
        assert len(painted) == settled, "no repaint while the chip is unchanged"

    async def test_an_approval_on_a_background_lane_reaches_the_strip(self) -> None:
        roster, painted = self._roster()
        background = roster._lanes["calendar"]
        await roster._narrate(background, "working")
        settled = len(painted)

        background.approvals.append(("id", "tool", "write a file"))
        await roster._narrate(background, "waiting")
        assert len(painted) > settled, (
            "run -> ask1 is exactly the transition the wearer must not miss"
        )


class TestRosterLaneSelection:
    """Which lane may enumerate and adopt the wearer's other sessions.

    The selection was `hasattr(agent, "list_sessions")`, which looks like a
    capability check and is not one: every lane is the same class, so every
    lane answered True and the winner was whichever came first in dict
    order. It worked only because "work" is registered first.
    """

    def test_a_non_roster_lane_is_never_chosen_to_enumerate(self) -> None:
        from joat.services.agent_terminal import AgentTerminalRoster

        # Brainstorm's lane FIRST, exactly the ordering that used to decide
        # this — and it is not allowed to read the roster.
        roster = AgentTerminalRoster(
            [("hermes", FakeAgent()), ("work", ListingAgent(ROWS))],
        )
        chosen = [name for name, lane in roster._lanes.items()
                  if getattr(getattr(lane.agent, "policy", None), "roster", False)]
        assert chosen == ["work"]

    async def test_without_a_roster_lane_it_says_so(self) -> None:
        """Rather than picking a lane that will fail at the policy layer and
        report a policy problem for what is really an ordering bug."""
        from joat.services.agent_terminal import AgentTerminalRoster

        roster = AgentTerminalRoster([("hermes", FakeAgent())])
        answer = await roster.discover()
        assert "enumerate" in answer

    async def test_discovery_works_when_the_roster_lane_is_not_first(self) -> None:
        from joat.services.agent_terminal import AgentTerminalRoster

        roster = AgentTerminalRoster(
            [("hermes", FakeAgent()), ("work", ListingAgent(ROWS))],
        )
        answer = await roster.discover()
        assert "enumerate" not in answer
        assert any("[discover]" in line for line in roster.frame().lines)


class TestTheTerminalReportsItsOwnLane:
    """The terminal's state must come from the terminal's lanes.

    `model.terminalState` — which gates whether a tap arms terminal input —
    was `status.agent_terminal_projection()["state"]`, and that projection
    is published by the BRAINSTORM lane (__main__: the work lane is built
    with state_changed=None). So a blocked Brainstorm lane made terminal
    input unreachable no matter what the work lane was doing.
    """

    async def test_frame_state_tracks_the_lanes_not_a_foreign_projection(
        self,
    ) -> None:
        from joat.services.agent_terminal import AgentTerminalRoster

        roster = AgentTerminalRoster([("work", FakeAgent(gate=asyncio.Event()))])
        assert roster.frame().state == "ready"

        await roster.send("do the thing")
        assert roster.frame().state == "running", (
            "a working lane reports running, whatever any other lane is doing"
        )

    async def test_an_approval_shows_on_the_lane_that_has_it(self) -> None:
        from joat.services.agent_terminal import AgentTerminalRoster

        roster = AgentTerminalRoster([("work", FakeAgent())])
        roster.note_approval("work", "id-1", "tool", "write a file")
        assert roster.frame().state == "approval_required"


class TestAReplyReachesHermesForReal:
    """The wearer's headline: "take the last response from claude or codex
    and send it to the hermes in the Agent Terminal".

    Covered until now with a fake snapshot on one end and a fake agent on
    the other. Fakes on both ends is how the routing shipped broken once
    already — consume_transcript returning False where every real session
    returns True.
    """

    async def test_the_whole_reply_arrives_in_the_lane(self) -> None:
        from joat.services.agent_terminal import (
            AgentTerminalCommands, AgentTerminalRoster,
        )
        from joat.services.sinks import DebateStateSink

        reply = ("Key the cache on the session id AND the generation, or a "
                 "lens handoff replays a round the wearer already read.")

        # A real settled session through the real sink.
        snapshot = DebateStateSink()
        await snapshot.open("caching", ["codex (proposer)", "claude (critic)"])
        await snapshot.turn("CODEX (proposer)")
        await snapshot.chunk("cache on session id")
        await snapshot.turn("CLAUDE (critic)")
        await snapshot.chunk(reply)
        await snapshot.close("done — converged")

        seen: list[str] = []

        class Recording:
            async def stream(self, prompt: str):
                seen.append(prompt)
                yield "acknowledged\n"

        roster = AgentTerminalRoster([("hermes", Recording())])
        commands = AgentTerminalCommands(
            roster, last_reply=lambda who: snapshot.snapshot.last_reply(who))

        receipt = await commands.route_reply("claude", None)
        for _ in range(40):
            await asyncio.sleep(0.01)
            if seen:
                break

        assert "CLAUDE" in receipt
        assert seen, "the reply must actually reach the lane"
        # The WHOLE answer, not the collapsed row the panel draws.
        assert reply in seen[0]

    async def test_a_seat_that_never_spoke_routes_nothing(self) -> None:
        from joat.services.agent_terminal import (
            AgentTerminalCommands, AgentTerminalRoster,
        )
        from joat.services.sinks import DebateStateSink

        snapshot = DebateStateSink()
        await snapshot.open("caching", ["codex (proposer)"])
        await snapshot.turn("CODEX (proposer)")
        await snapshot.chunk("only codex spoke")
        await snapshot.close("done")

        roster = AgentTerminalRoster([("hermes", FakeAgent())])
        commands = AgentTerminalCommands(
            roster, last_reply=lambda who: snapshot.snapshot.last_reply(who))
        answer = await commands.route_reply("claude", None)
        assert "No reply from Claude" in answer


class TestTheTerminalVocabularies:
    """Routing and switching, phrased the way people say them out loud."""

    @staticmethod
    def _norm(text: str) -> str:
        import re
        return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())

    def test_routing_hears_more_than_send(self) -> None:
        from joat.services.agent_terminal import parse_reply_route

        for said in ("send that to hermes", "give that to hermes",
                     "pass that to hermes", "hand that to hermes",
                     "push that to hermes", "forward that to hermes",
                     "send Claude's last reply to hermes"):
            assert parse_reply_route(self._norm(said)), said

    def test_switching_hears_the_surface_named_either_way(self) -> None:
        from joat.services.agent_terminal import _SWITCH

        for said, lane in (("switch terminal to work", "work"),
                           ("go to the work terminal", "work"),
                           ("open the calendar terminal", "calendar")):
            m = _SWITCH.match(self._norm(said))
            assert m is not None, said
            assert (m.group("name") or m.group("named")).strip() == lane

    def test_the_word_terminal_is_still_required(self) -> None:
        """An optional prefix once let a plain "switch to decaf" get claimed
        by this grammar — hijacked from the assistant and answered with a
        terminal error."""
        from joat.services.agent_terminal import _SWITCH

        for said in ("switch to decaf", "switch to work", "go to bed",
                     "open the door"):
            assert _SWITCH.match(self._norm(said)) is None, said


class TestSwitchingSessionsByGesture:
    """The wearer's ask, verbatim: "switch among sessions" — and their
    complaint about the alternative: "why do I have to say something to
    choose a work session?"

    Both existing paths fail while work is RUNNING, which is exactly when
    switching matters: the idle picker deliberately yields to live output
    (a chooser drawn over the thing you came to watch is worse), and voice
    means speaking. Meanwhile the tap on a busy terminal sent
    show_brainstorm, hit `if active is None: return` in the manager, and
    did nothing at all — a dead gesture, unnamed by the footer.
    """

    async def test_focus_cycles_and_wraps(self) -> None:
        r = roster(work=FakeAgent(), hermes=FakeAgent(), calendar=FakeAgent())
        assert r.focused == "work"
        assert "hermes" in await r.focus_next()
        assert "calendar" in await r.focus_next()
        assert "work" in await r.focus_next()

    async def test_cycling_never_touches_running_work(self) -> None:
        gate = asyncio.Event()
        busy = FakeAgent(gate=gate)
        r = roster(work=busy, hermes=FakeAgent())
        await r.send("long job")
        await asyncio.sleep(0)
        assert r._lanes["work"].busy

        await r.focus_next()

        assert r.focused == "hermes"
        assert r._lanes["work"].busy, "switching cancelled the work"
        assert busy.prompts == ["long job"]
        gate.set()

    async def test_a_single_lane_says_so_instead_of_pretending(self) -> None:
        r = roster(work=FakeAgent())
        assert "only session" in await r.focus_next()
        assert r.focused == "work"

    async def test_the_switch_repaints_so_the_strip_is_truthful(self) -> None:
        frames: list[object] = []

        async def paint(frame: object) -> None:
            frames.append(frame)

        r = roster(work=FakeAgent(), hermes=FakeAgent(), paint=paint)
        await r.focus_next()
        assert frames, "the wearer cannot see where focus landed"
        assert getattr(frames[-1], "topic", "").startswith("hermes")


class TestTwoSessionsTheWearerCannotOtherwiseTellApart:
    """AGENT_TERMINAL_ROADMAP.md:37-38 — "Host names appear only when two
    rows otherwise cannot be distinguished."

    That was satisfied vacuously: the host was discarded from every row, so
    it never appeared and the rule was trivially true — while the thing it
    protects failed completely. Two real sessions sharing a label rendered
    as two IDENTICAL bullets, and resuming by name silently took the first,
    so the second was unreachable by voice with no sign a choice existed.
    """

    class Lister:
        from joat.agents.openclaw_gateway import WORK_TERMINAL_POLICY as _p
        policy = _p

        def __init__(self, rows):
            self._rows = rows
            self.resumed: str | None = None

        async def list_sessions(self, limit: int = 20):
            return self._rows

        async def stream(self, prompt: str):
            yield "x"

        async def resume_session(self, key: str) -> None:
            self.resumed = key

    _DUP = [
        {"key": "agent:alpha:work-1", "label": "build the parser", "sessionId": "s1"},
        {"key": "agent:beta:work-2", "label": "build the parser", "sessionId": "s2"},
    ]
    _UNIQUE = [
        {"key": "agent:alpha:work-1", "label": "build the parser", "sessionId": "s1"},
        {"key": "agent:beta:work-2", "label": "fix the tests", "sessionId": "s2"},
    ]

    async def test_colliding_labels_gain_their_host(self) -> None:
        r = AgentTerminalRoster([("work", self.Lister(self._DUP))])
        listing = await r.discover()
        assert "build the parser (alpha)" in listing
        assert "build the parser (beta)" in listing

    async def test_unique_labels_carry_no_host_chrome(self) -> None:
        """The other half of the rule: a host on every row is chrome on a
        surface that cannot afford it."""
        r = AgentTerminalRoster([("work", self.Lister(self._UNIQUE))])
        listing = await r.discover()
        assert "(alpha)" not in listing and "(beta)" not in listing

    async def test_an_ambiguous_name_asks_instead_of_guessing(self) -> None:
        agent = self.Lister(self._DUP)
        r = AgentTerminalRoster([("work", agent)])
        await r.discover()
        answer = await r.resume("build the parser")
        assert "2 sessions match" in answer
        assert agent.resumed is None, "it resumed one anyway"

    async def test_the_disambiguated_name_reaches_the_second_session(self) -> None:
        agent = self.Lister(self._DUP)
        r = AgentTerminalRoster([("work", agent)])
        await r.discover()
        assert "resumed" in await r.resume("build the parser (beta)")
        assert agent.resumed == "agent:beta:work-2"

    async def test_a_malformed_key_disambiguates_nothing_rather_than_raising(self) -> None:
        rows = [
            {"key": "nonsense", "label": "same", "sessionId": "s1"},
            {"key": "", "label": "same", "sessionId": "s2"},
        ]
        r = AgentTerminalRoster([("work", self.Lister(rows))])
        assert "same" in await r.discover()


class TestInspectAsksTheRuntimeNotOurselves:
    """AGENT_TERMINAL_ROADMAP.md:37-38 and :113 — inspect a session
    "through its runtime's direct session API". "terminal history" was an
    alias for "terminal tail", so it returned JOAT's own in-process deque:
    for any session this engine did not personally run — a resumed one, or
    anything from before a restart — that is empty or misleading, and the
    operation answered a different question than the wearer asked.

    tail and history are genuinely different: what scrolled past on my
    screen, versus what this session contains.
    """

    class WithHistory:
        def __init__(self) -> None:
            self.asked: list[int] = []

        async def stream(self, prompt: str):
            yield "x"

        async def history(self, limit: int = 10):
            self.asked.append(limit)
            return [("user", "run the audit"), ("assistant", "found 3 issues")]

    class NoHistory:
        async def stream(self, prompt: str):
            yield "x"

    class Broken:
        async def stream(self, prompt: str):
            yield "x"

        async def history(self, limit: int = 10):
            raise RuntimeError("gateway said no")

    async def test_history_reads_the_runtime_and_tail_stays_local(self) -> None:
        agent = self.WithHistory()
        r = roster(work=agent)
        r._lanes["work"].tail.append("only what JOAT relayed")

        assert "only what JOAT relayed" in r.tail()
        inspected = await r.history()
        assert "run the audit" in inspected
        assert "found 3 issues" in inspected
        assert "only what JOAT relayed" not in inspected
        assert agent.asked, "the runtime was never asked"

    async def test_a_runtime_without_the_api_falls_back_and_says_which(self) -> None:
        r = roster(work=self.NoHistory())
        r._lanes["work"].tail.append("local only")
        answer = await r.history()
        assert "local only" in answer
        assert "no history API" in answer, "it passed off a local tail as the record"

    async def test_a_failing_runtime_reports_the_reason(self) -> None:
        r = roster(work=self.Broken())
        answer = await r.history()
        assert "history unavailable" in answer
        assert "gateway said no" in answer

    async def test_the_spoken_commands_are_wired_to_the_right_source(self) -> None:
        agent = self.WithHistory()
        r = roster(work=agent)
        c = AgentTerminalCommands(r)
        r._lanes["work"].tail.append("relayed line")

        assert "relayed line" in (await c.handle_command("terminal tail") or "")
        for phrase in ("terminal history", "terminal inspect", "terminal transcript"):
            answer = await c.handle_command(phrase) or ""
            assert "run the audit" in answer, phrase


class TestBrainstormMaterialReachesABusyLane:
    """The wearer's headline ask, across BOTH features: "take the last
    response from claud or codex and send it to the hermes in the Agent
    Terminal" — into a lane that is already working.

    Two things changed today meet here: routing resolves the material per
    speaker, and a busy lane queues one line instead of refusing. Each is
    tested alone; the combination is where material could be resolved
    correctly and then dropped, or queued and then sent as the WRONG
    speaker's text. Neither suite would notice.
    """

    async def test_the_right_speakers_text_survives_the_queue(self) -> None:
        from joat.services.hud import HudWriter
        from joat.services.session import SessionManager
        from tests.unit.test_session import SinkTransport

        gate = asyncio.Event()
        received: list[str] = []

        class Hermes:
            async def stream(self, prompt: str):
                received.append(prompt)
                if len(received) == 1:
                    await gate.wait()      # the first turn holds the lane
                yield "ok"

        roster = AgentTerminalRoster([("hermes", Hermes())])
        replies = {
            "claude": ("CLAUDE (critic)", "CLAUDE-TEXT key on session id"),
            "codex": ("CODEX (proposer)", "CODEX-TEXT cache the synthesis"),
            None: ("CLAUDE (critic)", "CLAUDE-TEXT key on session id"),
        }
        commands = AgentTerminalCommands(
            roster, last_reply=lambda who: replies.get(who),
        )
        manager = SessionManager(
            HudWriter(SinkTransport()), lambda t: None,
            route_reply=lambda who, lane: commands.route_reply(who, lane),
            last_reply=lambda who: replies.get(who),
        )

        manager.handle_transcript(
            "send that to hermes", addressed="send that to hermes",
        )
        await asyncio.sleep(0.05)
        assert received == ["CLAUDE-TEXT key on session id"]

        # Hermes is busy now. This must queue, not interleave, not drop.
        manager.handle_transcript(
            "send codex's answer to hermes",
            addressed="send codex's answer to hermes",
        )
        await asyncio.sleep(0.05)
        assert len(received) == 1, "it interleaved into a busy lane"
        assert roster._lanes["hermes"].queued.startswith("CODEX-TEXT"), (
            "the queue holds the wrong speaker's text"
        )

        gate.set()
        for _ in range(60):
            await asyncio.sleep(0.02)
            if len(received) > 1:
                break
        assert received == [
            "CLAUDE-TEXT key on session id",
            "CODEX-TEXT cache the synthesis",
        ]
        await manager.close()


class _CaptureContextAgent(ResumingAgent):
    """Controlled public adapter seam; no OpenClaw connection or process."""

    def __init__(self, gate: asyncio.Event | None = None) -> None:
        super().__init__(ROWS)
        self._gate = gate
        self.capture_context_generation: int | None = 1
        self.scoped_calls: list[tuple[str, int]] = []
        self.dispatch_gate = asyncio.Event()
        self.dispatch_gate.set()
        self.resume_started = asyncio.Event()
        self.resume_release = asyncio.Event()
        self.resume_release.set()

    async def stream_captured(self, prompt: str, *, context_generation: int) -> AsyncIterator[str]:
        self.scoped_calls.append((prompt, context_generation))
        await self.dispatch_gate.wait()
        if self.capture_context_generation != context_generation:
            raise RuntimeError("Captured terminal context changed")
        async for chunk in self.stream(prompt):
            yield chunk

    async def resume_session(self, session_key: str) -> None:
        self.resume_started.set()
        await self.resume_release.wait()
        await super().resume_session(session_key)
        assert self.capture_context_generation is not None
        self.capture_context_generation += 1


async def _terminal_until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


async def _terminal_idle(roster: AgentTerminalRoster) -> None:
    await _terminal_until(lambda: not roster.busy and not any(lane.queued for lane in roster._lanes.values()))
    # Let owned run completion callbacks perform their final repaint.
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def _close_capture_fixture(roster: AgentTerminalRoster, *agents: _CaptureContextAgent) -> None:
    for agent in agents:
        agent.dispatch_gate.set()
        agent.resume_release.set()
        if agent._gate is not None:
            agent._gate.set()
    for lane in roster._lanes.values():
        lane.queued = ""
        if lane.task is not None and not lane.task.done():
            lane.task.cancel()
    tasks = [lane.task for lane in roster._lanes.values() if lane.task is not None]
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 1)
    await asyncio.sleep(0)
    await asyncio.sleep(0)


class TestTypedTerminalInstruction:
    async def test_exact_text_enters_only_the_captured_adapter(self) -> None:
        agent, other = _CaptureContextAgent(), _CaptureContextAgent()
        r = roster(work=agent, other=other)
        literal = "  Café: keep BOTH spaces?!\n漢字 👋 Do not normalize this.  "
        try:
            result = r.admit_instruction("work", literal)
            assert result.value == "accepted"
            assert agent.scoped_calls == [], "admission must not wait for remote execution"
            await _terminal_idle(r)
            assert agent.scoped_calls == [(literal, 1)]
            assert agent.prompts == [literal] and other.prompts == []
            assert not r.busy, "existing task-settled handling must remain attached"
        finally:
            await _close_capture_fixture(r, agent, other)

    @pytest.mark.parametrize("text", ["", " \n", "bad\x00input", "bad\ud800input", "a" * 4097])
    async def test_invalid_input_never_starts_a_turn(self, text: str) -> None:
        agent = _CaptureContextAgent()
        r = roster(work=agent)
        try:
            assert r.admit_instruction("work", text).value == "invalid"
            assert not r.busy and agent.scoped_calls == []
        finally:
            await _close_capture_fixture(r, agent)

    @pytest.mark.parametrize("name", ["other", "unknown", "WORK", " work", ""])
    async def test_only_the_exact_focused_lane_is_admissible(self, name: str) -> None:
        agent, other = _CaptureContextAgent(), _CaptureContextAgent()
        r = roster(work=agent, other=other)
        try:
            assert r.admit_instruction(name, "Keep target").value == "wrong_focus"
            assert r.focused == "work" and not r.busy
            assert agent.scoped_calls == other.scoped_calls == []
        finally:
            await _close_capture_fixture(r, agent, other)

    @pytest.mark.parametrize("state", ["busy", "queued", "approval"])
    async def test_admission_never_replaces_input_or_resolves_work_approval(self, state: str) -> None:
        gate = asyncio.Event()
        agent = _CaptureContextAgent(gate)
        r = roster(work=agent)
        try:
            if state == "busy":
                await r.send("Existing turn")
                await _terminal_until(lambda: bool(agent.prompts))
            elif state == "queued":
                # A finished turn's settlement callback may still own this line.
                r._lanes["work"].queued = "Existing queued instruction"
            else:
                r.note_approval("work", "existing-approval", "exec", "Existing request")
            lane = r._lanes["work"]
            previous = (lane.task, lane.queued, list(lane.approvals), list(lane.tail))

            result = r.admit_instruction("work", "New instruction")

            assert result.value == ("approval_required" if state == "approval" else "busy")
            assert (lane.task, lane.queued, lane.approvals, list(lane.tail)) == previous
            assert agent.scoped_calls == []
        finally:
            await _close_capture_fixture(r, agent)

    async def test_unready_or_stream_only_agent_cannot_receive_typed_input(self) -> None:
        captured, legacy = _CaptureContextAgent(), FakeAgent()
        captured.capture_context_generation = None
        r = roster(work=captured, legacy=legacy)
        try:
            assert r.admit_instruction("work", "Question").value == "unavailable"
            assert r.focus("legacy")
            assert r.admit_instruction("legacy", "Question").value == "unavailable"
            assert captured.prompts == legacy.prompts == [] and not r.busy
        finally:
            await _close_capture_fixture(r, captured)

    async def test_changed_focus_between_capture_and_admission_is_rejected(self, monkeypatch) -> None:
        agent, other = _CaptureContextAgent(), _CaptureContextAgent()
        r = roster(work=agent, other=other)
        capture = r._capture_target

        def changed_target():
            target = capture()
            r.focus("other")
            return target

        monkeypatch.setattr(r, "_capture_target", changed_target)
        try:
            assert r.admit_instruction("work", "Old target only").value == "context_changed"
            assert not r.busy and agent.scoped_calls == other.scoped_calls == []
        finally:
            await _close_capture_fixture(r, agent, other)

    @pytest.mark.parametrize("change", ["generation", "adapter"])
    async def test_replaced_context_before_scheduled_task_never_receives_input(self, change: str) -> None:
        agent, replacement = _CaptureContextAgent(), _CaptureContextAgent()
        r = roster(work=agent)
        try:
            assert r.admit_instruction("work", "Original context only").value == "accepted"
            if change == "generation":
                agent.capture_context_generation = 2
            else:
                r._lanes["work"].agent = replacement
            await _terminal_idle(r)
            assert agent.scoped_calls == replacement.scoped_calls == []
            assert agent.prompts == replacement.prompts == []
            assert r._lanes["work"].last_error == "Captured terminal context changed"
        finally:
            await _close_capture_fixture(r, agent, replacement)


class TestScopedTerminalCapture:
    async def test_exact_instruction_reaches_only_captured_lane_once(self) -> None:
        agent, other = _CaptureContextAgent(), _CaptureContextAgent()
        r = roster(work=agent, other=other)
        c = AgentTerminalCommands(r, monotonic=lambda: 100.0)
        literal = "  Keep BOTH spaces?!\nDo not normalize this.  "
        try:
            capture = c.begin_capture()
            assert capture is not None and capture.lane_name == "work"
            assert "work" in capture.question
            assert await c.consume_capture(capture, literal) == "work ▸ sent."
            assert c.current_capture is None
            assert await c.consume_capture(capture, literal) is None
            await _terminal_idle(r)
            assert agent.prompts == [literal] and other.prompts == []
            assert agent.scoped_calls == [(literal, 1)]
        finally:
            await _close_capture_fixture(r, agent, other)

    async def test_typed_owner_never_arms_the_legacy_command_parser(self) -> None:
        agent = _CaptureContextAgent()
        r = roster(work=agent)
        c = AgentTerminalCommands(r, monotonic=lambda: 100.0)
        try:
            capture = c.begin_capture()
            assert await c.handle_command("explain the weather") is None
            assert agent.prompts == [] and c.current_capture is capture
            assert await c.consume_capture(capture, "Run the chosen task.") == "work ▸ sent."
            await _terminal_idle(r)
            assert agent.prompts == ["Run the chosen task."]
        finally:
            await _close_capture_fixture(r, agent)

    @pytest.mark.parametrize("lifecycle", ["home", "reconnect"])
    async def test_cleared_capture_cannot_claim_later_ordinary_speech(self, lifecycle: str) -> None:
        """Host lifecycle wiring calls this same explicit owner exit."""
        agent = _CaptureContextAgent()
        r = roster(work=agent)
        c = AgentTerminalCommands(r, monotonic=lambda: 100.0)
        try:
            capture = c.begin_capture()
            c.clear_capture()
            assert await c.handle_command("explain this question") is None, lifecycle
            assert await c.consume_capture(capture, "stale terminal instruction") is None
            await _terminal_idle(r)
            assert agent.prompts == [] and c.current_capture is None
            # Addressed terminal commands remain available after leaving capture.
            assert await c.handle_command("terminal deliberate new instruction") == "work ▸ sent."
            await _terminal_idle(r)
            assert agent.prompts == ["deliberate new instruction"]
        finally:
            await _close_capture_fixture(r, agent)

    @pytest.mark.parametrize("change", ["focus_other", "focus_away_and_back", "cycle_away_and_back"])
    async def test_focus_change_never_retargets_or_revives_old_instruction(self, change: str) -> None:
        agent, other = _CaptureContextAgent(), _CaptureContextAgent()
        r = roster(work=agent, other=other)
        c = AgentTerminalCommands(r, monotonic=lambda: 100.0)
        try:
            capture = c.begin_capture()
            if change == "cycle_away_and_back":
                await r.focus_next()
                await r.focus_next()
            else:
                assert r.focus("other")
                if change == "focus_away_and_back":
                    assert r.focus("work")
            assert await c.consume_capture(capture, "old focused instruction") is None
            await _terminal_idle(r)
            assert agent.prompts == other.prompts == []
            assert c.current_capture is None
        finally:
            await _close_capture_fixture(r, agent, other)

    @pytest.mark.parametrize("change", ["agent", "lane"])
    async def test_same_named_replacement_cannot_inherit_a_capture(self, change: str) -> None:
        original, replacement = _CaptureContextAgent(), _CaptureContextAgent()
        r = roster(work=original)
        c = AgentTerminalCommands(r, monotonic=lambda: 100.0)
        try:
            capture = c.begin_capture()
            if change == "agent":
                r._lanes["work"].agent = replacement
            else:
                r._lanes["work"] = type(r._lanes["work"])(name="work", agent=replacement)
            assert await c.consume_capture(capture, "for the old session only") is None
            await _terminal_idle(r)
            assert original.prompts == replacement.prompts == []
        finally:
            await _close_capture_fixture(r, original, replacement)

    @pytest.mark.parametrize("phase", ["pending", "succeeded", "failed"])
    async def test_resume_invalidates_before_its_first_await_even_if_it_fails(self, phase: str) -> None:
        agent = _CaptureContextAgent()
        r = roster(work=agent)
        c = AgentTerminalCommands(r, monotonic=lambda: 100.0)
        pending: asyncio.Task[str] | None = None
        try:
            await r.discover()
            capture = c.begin_capture()
            if phase == "failed":
                agent._resume_fail = "controlled refusal"
            agent.resume_release.clear()
            pending = asyncio.create_task(r.resume("nightly build"))
            await asyncio.wait_for(agent.resume_started.wait(), 1)
            if phase != "pending":
                agent.resume_release.set()
                await asyncio.wait_for(pending, 1)
            assert await c.consume_capture(capture, "do not cross the resume boundary") is None
            await _terminal_idle(r)
            assert agent.prompts == []
        finally:
            agent.resume_release.set()
            if pending is not None:
                await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 1)
            await _close_capture_fixture(r, agent)

    @pytest.mark.parametrize("settled", [False, True])
    async def test_intervening_turn_never_revives_an_idle_capture(self, settled: bool) -> None:
        gate = asyncio.Event()
        agent = _CaptureContextAgent(gate)
        r = roster(work=agent)
        c = AgentTerminalCommands(r, monotonic=lambda: 100.0)
        try:
            capture = c.begin_capture()
            await r.send("new explicit turn")
            await _terminal_until(lambda: agent.prompts == ["new explicit turn"])
            if settled:
                gate.set()
                await _terminal_idle(r)
            assert await c.consume_capture(capture, "stale idle instruction") is None
            assert r._lanes["work"].queued == ""
            gate.set()
            await _terminal_idle(r)
            assert agent.prompts == ["new explicit turn"]
        finally:
            await _close_capture_fixture(r, agent)

    async def test_busy_capture_preserves_exact_one_line_steering(self) -> None:
        gate = asyncio.Event()
        agent = _CaptureContextAgent(gate)
        r = roster(work=agent)
        c = AgentTerminalCommands(r, monotonic=lambda: 100.0)
        literal = "  Keep the current run; then CHECK this?!  "
        try:
            await r.send("running turn")
            await _terminal_until(lambda: bool(agent.prompts))
            capture = c.begin_capture()
            result = await c.consume_capture(capture, literal)
            assert result is not None and "queued" in result
            assert agent.prompts == ["running turn"]
            assert r._lanes["work"].queued == literal
            assert await c.consume_capture(capture, literal) is None
            gate.set()
            await _terminal_idle(r)
            assert agent.prompts == ["running turn", literal]
            assert agent.scoped_calls == [(literal, 1)]
        finally:
            await _close_capture_fixture(r, agent)

    @pytest.mark.parametrize("change", ["settled", "new_queue"])
    async def test_busy_capture_cannot_overwrite_a_newer_turn_or_queue(self, change: str) -> None:
        gate = asyncio.Event()
        agent = _CaptureContextAgent(gate)
        r = roster(work=agent)
        c = AgentTerminalCommands(r, monotonic=lambda: 100.0)
        try:
            await r.send("running turn")
            await _terminal_until(lambda: bool(agent.prompts))
            capture = c.begin_capture()
            if change == "settled":
                gate.set()
                await _terminal_idle(r)
            else:
                await r.send("newer explicit queue")
            assert await c.consume_capture(capture, "stale queued instruction") is None
            gate.set()
            await _terminal_idle(r)
            expected = ["running turn"] + (["newer explicit queue"] if change == "new_queue" else [])
            assert agent.prompts == expected
        finally:
            await _close_capture_fixture(r, agent)

    @pytest.mark.parametrize("phase", ["before_consume", "scheduled", "queued"])
    async def test_changed_adapter_context_never_receives_old_capture(self, phase: str) -> None:
        gate = asyncio.Event() if phase == "queued" else None
        agent = _CaptureContextAgent(gate)
        r = roster(work=agent)
        c = AgentTerminalCommands(r, monotonic=lambda: 100.0)
        try:
            if phase == "queued":
                await r.send("running turn")
                await _terminal_until(lambda: bool(agent.prompts))
            capture = c.begin_capture()
            if phase == "before_consume":
                agent.capture_context_generation = 2
                assert await c.consume_capture(capture, "old context only") is None
            else:
                agent.dispatch_gate.clear()
                assert await c.consume_capture(capture, "old context only") is not None
                # The adapter boundary can suspend after capture admission,
                # or wait in the busy lane's queue. Both must retain its token.
                await asyncio.sleep(0)
                agent.capture_context_generation = 2
                agent.dispatch_gate.set()
                if gate is not None:
                    gate.set()
            await _terminal_idle(r)
            assert agent.prompts == (["running turn"] if phase == "queued" else [])
            assert c.current_capture is None
        finally:
            await _close_capture_fixture(r, agent)

    async def test_duplicate_tap_preserves_identity_and_original_deadline(self) -> None:
        agent = _CaptureContextAgent()
        r = roster(work=agent)
        now = [100.0]
        c = AgentTerminalCommands(r, monotonic=lambda: now[0])
        try:
            capture = c.begin_capture()
            assert capture is not None and capture.expires_at == 145.0
            now[0] += 20
            assert c.begin_capture() is capture
            now[0] = capture.expires_at
            assert await c.consume_capture(capture, "expired answer") is None
            await _terminal_idle(r)
            assert agent.prompts == [] and c.current_capture is None
            fresh = c.begin_capture()
            assert fresh is not None and fresh.capture_id != capture.capture_id
        finally:
            await _close_capture_fixture(r, agent)

    async def test_reconstructed_capture_cannot_borrow_the_live_owner(self) -> None:
        agent = _CaptureContextAgent()
        r = roster(work=agent)
        c = AgentTerminalCommands(r, monotonic=lambda: 100.0)
        try:
            capture = c.begin_capture()
            assert await c.consume_capture(replace(capture), "forged owner") is None
            assert c.current_capture is capture
            assert await c.consume_capture(capture, "real owner") == "work ▸ sent."
            await _terminal_idle(r)
            assert agent.prompts == ["real owner"]
        finally:
            await _close_capture_fixture(r, agent)

    async def test_uninitialized_context_never_offers_a_spoken_target(self) -> None:
        agent = _CaptureContextAgent()
        agent.capture_context_generation = None
        r = roster(work=agent)
        c = AgentTerminalCommands(r, monotonic=lambda: 100.0)
        try:
            assert c.begin_capture() is None
            assert await c.handle_command("ordinary speech") is None
            assert agent.prompts == []
        finally:
            await _close_capture_fixture(r, agent)

    async def test_cancel_discards_captured_queue_after_turn_finishes_before_settlement(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        gate = asyncio.Event()
        agent = _CaptureContextAgent(gate)
        r = roster(work=agent)
        c = AgentTerminalCommands(r, monotonic=lambda: 100.0)
        settlement_started = asyncio.Event()
        release_settlement = asyncio.Event()
        settle = r._lane_settled

        async def held_settlement(lane: TerminalLane, completed: asyncio.Task[None]) -> None:
            settlement_started.set()
            await release_settlement.wait()
            await settle(lane, completed)

        monkeypatch.setattr(r, "_lane_settled", held_settlement)
        try:
            await r.send("running turn")
            await _terminal_until(lambda: agent.prompts == ["running turn"])
            capture = c.begin_capture()
            assert capture is not None
            receipt = await c.consume_capture(capture, "captured queued instruction")
            assert receipt is not None and "queued" in receipt
            gate.set()
            await asyncio.wait_for(settlement_started.wait(), 1)
            assert not r.busy and r._lanes["work"].queued == "captured queued instruction"

            cancellation = await r.cancel()
            release_settlement.set()
            await _terminal_idle(r)

            assert agent.prompts == ["running turn"], "Cancelled queued capture streamed after settlement"
            assert agent.scoped_calls == []
            assert r._lanes["work"].queued == "" and r._lanes["work"].queued_context is None
            assert "queue" in cancellation and "discard" in cancellation
        finally:
            release_settlement.set()
            await _close_capture_fixture(r, agent)


class _PreparingAgent(_CaptureContextAgent):
    def __init__(self) -> None:
        super().__init__()
        self.capture_context_generation = None
        self.initialize_calls = 0
        self.initialize_started = asyncio.Event()
        self.initialize_release = asyncio.Event()
        self.initialize_release.set()
        self.initialize_finished = asyncio.Event()
        self.initialize_cancelled = asyncio.Event()
        self.initialize_tasks: list[asyncio.Task[object]] = []
        self.fail = False
        self.provide_context = True
        self.ignore_cancel = False

    async def initialize(self) -> None:
        self.initialize_calls += 1
        current = asyncio.current_task()
        assert current is not None
        self.initialize_tasks.append(current)
        self.initialize_started.set()
        try:
            while not self.initialize_release.is_set():
                try:
                    await self.initialize_release.wait()
                except asyncio.CancelledError:
                    self.initialize_cancelled.set()
                    if not self.ignore_cancel:
                        raise
            if self.fail:
                raise RuntimeError("PRIVATE-INITIALIZATION-DETAIL must never reach the lenses")
            if self.provide_context:
                self.capture_context_generation = 2
        finally:
            self.initialize_finished.set()


async def _initialization_entered(task: asyncio.Task[bool], agent: _PreparingAgent) -> None:
    await _terminal_until(lambda: task.done() or agent.initialize_started.is_set())
    assert agent.initialize_calls == 1, "Opening an unprepared lane never initialized its adapter"


async def _close_preparing_fixture(
    r: AgentTerminalRoster, agents: list[_PreparingAgent], tasks: list[asyncio.Task[bool]],
) -> None:
    for agent in agents:
        agent.initialize_release.set()
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 1)
    initializations = {task for agent in agents for task in agent.initialize_tasks}
    if initializations:
        await asyncio.wait_for(asyncio.gather(*initializations, return_exceptions=True), 1)
    await _close_capture_fixture(r, *agents)


class TestTerminalLanePreparation:
    @pytest.mark.parametrize("scope_upgrade", [True, False])
    async def test_recognized_pairing_scope_refusal_requests_access_instead_of_offline(self, scope_upgrade: bool) -> None:
        from joat.agents.openclaw_gateway import OpenClawPairingRequired, OpenClawScopeMismatch

        class ScopeLimitedAgent(_PreparingAgent):
            async def initialize(self) -> None:
                if scope_upgrade:
                    raise OpenClawScopeMismatch("private remote diagnostic")
                raise OpenClawPairingRequired(request_id="private request", reason="private remote diagnostic")

        agent = ScopeLimitedAgent()
        r = roster(work=agent)
        try:
            assert not await r.prepare_lane("work", is_current=lambda: True)
            frame = r.frame()
            assert frame.state == "approval_required"
            assert frame.approvals == 0  # Connection grant, not a tool approval.
            assert frame.reason == (
                "Approve Work permissions in OpenClaw, then retry." if scope_upgrade
                else "Approve this device in OpenClaw, then retry."
            )
            assert AgentTerminalCommands(r).begin_capture() is None
            assert "private remote diagnostic" not in str(frame)
        finally:
            await _close_preparing_fixture(r, [agent], [])

    async def test_setup_failure_reason_fits_the_actual_terminal_wire_with_a_long_lane_name(self, caplog: pytest.LogCaptureFixture) -> None:
        from joat.core.protocol import AgentTerminalHudView

        agent = _PreparingAgent()
        agent.fail = True
        name = "work-" + "x" * 70
        r = AgentTerminalRoster([(name, agent)])
        try:
            assert await r.prepare_lane(name, is_current=lambda: True) is False
            frame = r.frame()
            wire = AgentTerminalHudView(
                state=frame.state, focus=frame.topic, lanes=frame.lanes,
                activity="\n".join(frame.lines), reason=frame.reason,
            ).to_wire()
            assert wire["terminal"]["terminalReason"].endswith("Tap retry.")
            assert "PRIVATE-INITIALIZATION-DETAIL" not in str(wire)
            assert "error=RuntimeError function=initialize" in caplog.text
            assert "PRIVATE-INITIALIZATION-DETAIL" not in caplog.text
        finally:
            await _close_preparing_fixture(r, [agent], [])

    def test_exact_lane_focus_wins_over_an_earlier_substring_match(self) -> None:
        r = roster(work=_PreparingAgent(), **{"work-calendar": _PreparingAgent()})
        assert r.focus("work-calendar")
        assert r.focused == "work-calendar"
        assert r.focus("switch to work")
        assert r.focused == "work"

    async def test_unprepared_lane_does_not_claim_readiness_or_reconnect_during_capture(self) -> None:
        agent = _PreparingAgent()
        r = roster(work=agent)
        commands = AgentTerminalCommands(r)
        try:
            assert r.frame().state == "unavailable", "A never-initialized work lane was presented as ready"
            assert "work" in r.frame().reason.lower()
            assert "unprepared" in r.picker_rows()[0][1].lower()
            assert commands.begin_capture() is None
            assert agent.initialize_calls == 0
        finally:
            await _close_preparing_fixture(r, [agent], [])

    async def test_first_open_prepares_selected_work_then_exact_capture_can_send(self) -> None:
        work, hermes = _PreparingAgent(), _PreparingAgent()
        r = roster(work=work, hermes=hermes)
        frames: list[TerminalFrame] = []

        async def paint(frame: TerminalFrame) -> None:
            frames.append(frame)

        try:
            assert await r.prepare_lane("work", is_current=lambda: True, paint=paint)
            assert work.initialize_calls == 1 and hermes.initialize_calls == 0
            assert r.focused == "work" and r.frame().state == "ready"
            assert frames[-1].state == "ready" and frames[-1].reason == ""
            commands = AgentTerminalCommands(r)
            capture = commands.begin_capture()
            assert capture is not None
            literal = "  First instruction; keep these words?!  "
            assert await commands.consume_capture(capture, literal) == "work ▸ sent."
            await _terminal_idle(r)
            assert work.prompts == [literal] and work.scoped_calls == [(literal, 2)]
            assert hermes.prompts == []
        finally:
            await _close_preparing_fixture(r, [work, hermes], [])

    async def test_preparing_frame_has_no_ready_capture_until_context_exists(self) -> None:
        agent = _PreparingAgent()
        agent.initialize_release.clear()
        r = roster(work=agent)
        frames: list[TerminalFrame] = []

        async def paint(frame: TerminalFrame) -> None:
            frames.append(frame)

        task = asyncio.create_task(r.prepare_lane("work", is_current=lambda: True, paint=paint))
        try:
            await _initialization_entered(task, agent)
            assert r.frame().state == "unavailable"
            assert "connecting" in r.frame().reason.lower()
            assert "preparing" in r.picker_rows()[0][1].lower()
            assert AgentTerminalCommands(r).begin_capture() is None
            agent.initialize_release.set()
            assert await asyncio.wait_for(task, 1)
            assert frames[0].state == "unavailable" and frames[-1].state == "ready"
        finally:
            await _close_preparing_fixture(r, [agent], [task])

    @pytest.mark.parametrize("failure", ["exception", "timeout", "missing_context"])
    async def test_failed_preparation_stays_unavailable_and_retry_prepares_the_same_lane(self, failure: str) -> None:
        work, hermes = _PreparingAgent(), _PreparingAgent()
        work.fail = failure == "exception"
        work.provide_context = failure != "missing_context"
        if failure == "timeout":
            work.initialize_release.clear()
        r = roster(work=work, hermes=hermes)
        frames: list[TerminalFrame] = []

        async def paint(frame: TerminalFrame) -> None:
            frames.append(frame)

        try:
            result = await asyncio.wait_for(r.prepare_lane(
                "work", is_current=lambda: True, timeout_s=.02, paint=paint,
            ), .5)
            assert result is False
            assert work.initialize_calls == 1 and hermes.initialize_calls == 0
            assert r.frame().state == "unavailable" and AgentTerminalCommands(r).begin_capture() is None
            reason = r.frame().reason.lower()
            assert "work" in reason and "retry" in reason
            if failure == "timeout":
                assert "timed out" in reason and work.initialize_cancelled.is_set()
            for frame in frames:
                assert "PRIVATE-INITIALIZATION-DETAIL" not in str(frame)
            work.fail = False
            work.provide_context = True
            work.initialize_release.set()
            assert await r.prepare_lane("work", is_current=lambda: True, paint=paint)
            assert work.initialize_calls == 2 and hermes.initialize_calls == 0
            assert r.frame().state == "ready" and r.frame().reason == ""
        finally:
            await _close_preparing_fixture(r, [work, hermes], [])

    @pytest.mark.parametrize("departure", ["cancel", "timeout"])
    async def test_duplicate_open_shares_setup_and_one_departing_waiter_does_not_cancel_the_other(self, departure: str) -> None:
        agent = _PreparingAgent()
        agent.initialize_release.clear()
        r = roster(work=agent)
        first = asyncio.create_task(r.prepare_lane(
            "work", is_current=lambda: True, timeout_s=.05 if departure == "timeout" else 8,
        ))
        tasks = [first]
        try:
            await _initialization_entered(first, agent)
            second = asyncio.create_task(r.prepare_lane("work", is_current=lambda: True))
            tasks.append(second)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert agent.initialize_calls == 1
            if departure == "cancel":
                first.cancel()
                await asyncio.wait_for(asyncio.gather(first, return_exceptions=True), .5)
            else:
                assert await asyncio.wait_for(first, .5) is False
            assert not agent.initialize_cancelled.is_set(), "One waiter cancelled shared setup still in use"
            agent.initialize_release.set()
            assert await asyncio.wait_for(second, 1)
            assert agent.initialize_calls == 1 and r.frame().state == "ready"
        finally:
            await _close_preparing_fixture(r, [agent], tasks)


    @pytest.mark.parametrize("exit_kind", ["home", "reconnect", "focus_other", "focus_round_trip", "agent_replaced"])
    async def test_late_preparation_never_refocuses_or_repaints_a_superseded_opening(self, exit_kind: str) -> None:
        work, other = _PreparingAgent(), _PreparingAgent()
        work.initialize_release.clear()
        r = roster(work=work, other=other)
        current = [True]
        frames: list[TerminalFrame] = []

        async def paint(frame: TerminalFrame) -> None:
            frames.append(frame)

        task = asyncio.create_task(r.prepare_lane("work", is_current=lambda: current[0], paint=paint))
        try:
            await _initialization_entered(task, work)
            painted_before_exit = len(frames)
            if exit_kind in {"home", "reconnect"}:
                current[0] = False
            elif exit_kind == "agent_replaced":
                r._lanes["work"].agent = other
            else:
                assert r.focus("other")
                if exit_kind == "focus_round_trip":
                    assert r.focus("work")
            expected_focus = r.focused
            work.initialize_release.set()
            assert await asyncio.wait_for(task, 1) is False
            assert r.focused == expected_focus
            assert len(frames) == painted_before_exit, "Old setup painted a ready/error frame after its owner left"
            assert work.prompts == other.prompts == []
        finally:
            await _close_preparing_fixture(r, [work, other], [task])

    async def test_stale_open_is_inert_before_focus_or_setup(self) -> None:
        work, other = _PreparingAgent(), _PreparingAgent()
        r = roster(work=work, other=other)
        painted: list[TerminalFrame] = []

        async def paint(frame: TerminalFrame) -> None:
            painted.append(frame)

        try:
            assert await r.prepare_lane("other", is_current=lambda: False, paint=paint) is False
            assert r.focused == "work" and painted == []
            assert work.initialize_calls == other.initialize_calls == 0
        finally:
            await _close_preparing_fixture(r, [work, other], [])

    def test_public_focus_generation_preserves_round_trip_invalidation(self) -> None:
        r = roster(work=_PreparingAgent(), other=_PreparingAgent())
        generation = r.focus_generation
        assert type(generation) is int, "Opening guards need the roster's actual focus revision"
        assert r.focus("work") and r.focus_generation == generation
        assert r.focus("other") and r.focus_generation > generation
        changed = r.focus_generation
        assert r.focus("work") and r.focus_generation > changed
        with pytest.raises(AttributeError):
            r.focus_generation = generation

    async def test_opening_prepared_busy_lane_never_waits_for_or_reinitializes_its_turn(self) -> None:
        agent = _PreparingAgent()
        agent.capture_context_generation = 7
        agent._gate = asyncio.Event()
        r = roster(work=agent)
        try:
            await r.send("ongoing work")
            await _terminal_until(lambda: agent.prompts == ["ongoing work"])
            assert await asyncio.wait_for(r.prepare_lane("work", is_current=lambda: True), .1)
            assert agent.initialize_calls == 0 and r.busy and r.frame().state == "running"
            assert AgentTerminalCommands(r).begin_capture() is not None
            assert agent.prompts == ["ongoing work"]
        finally:
            await _close_preparing_fixture(r, [agent], [])

    async def test_last_waiter_timeout_bounds_cleanup_and_does_not_launch_parallel_setup(self) -> None:
        agent = _PreparingAgent()
        agent.initialize_release.clear()
        agent.ignore_cancel = True
        r = roster(work=agent)
        current = [True]
        first = asyncio.create_task(r.prepare_lane("work", is_current=lambda: current[0], timeout_s=.02))
        tasks = [first]
        try:
            await _initialization_entered(first, agent)
            assert await asyncio.wait_for(first, .5) is False
            assert agent.initialize_cancelled.is_set() and not agent.initialize_finished.is_set()
            second = asyncio.create_task(r.prepare_lane("work", is_current=lambda: current[0], timeout_s=.02))
            tasks.append(second)
            assert await asyncio.wait_for(second, .5) is False
            assert agent.initialize_calls == 1, "Abandoned initialization lost ownership and a second one started"
            assert AgentTerminalCommands(r).begin_capture() is None
            current[0] = False
        finally:
            await _close_preparing_fixture(r, [agent], tasks)


class TestExplicitTerminalViewOwnership:
    @pytest.mark.parametrize(("phrase", "operation"), [
        ("terminal sessions", "overview"),
        # focus_resolved, not focus: switching resolves first so an
        # ambiguous name can list its candidates, and the roster effect
        # this invariant guards is the one that MOVES focus.
        ("switch terminal to other", "focus_resolved"),
        ("terminal cancel", "cancel"),
        ("terminal approvals", "approvals_view"),
        ("terminal discover", "discover"),
        ("terminal resume nightly build", "resume"),
        ("terminal approve 1", "decide"),
        ("terminal tail", "tail"),
        ("terminal history", "history"),
        ("terminal send claude's reply to other", "focus"),
        ("terminal Keep this punctuation?!", "send"),
    ])
    async def test_each_recognized_branch_acquires_once_before_its_first_roster_effect(
        self, phrase: str, operation: str, monkeypatch,
    ) -> None:
        agent, other = _CaptureContextAgent(), _CaptureContextAgent()
        r = roster(work=agent, other=other)
        await r.discover()
        events: list[str] = []

        def acquire() -> None:
            assert r.focused == "work", "The command changed focus before acquiring its view"
            events.append("view")

        def last_reply(who: str | None) -> tuple[str, str]:
            assert events == ["view"], "Reply lookup preceded the explicit view boundary"
            return "CLAUDE (critic)", "Exact collaborator reply."

        commands = AgentTerminalCommands(r, last_reply=last_reply, on_explicit_view=acquire)
        original = getattr(r, operation)

        def record_effect() -> None:
            assert events == ["view"], "The command performed a roster effect before acquiring its view"
            events.append(operation)

        if operation in {"cancel", "discover", "resume", "decide", "history", "send"}:
            async def tracked(*args, **kwargs):
                record_effect()
                return await original(*args, **kwargs)
        else:
            def tracked(*args, **kwargs):
                record_effect()
                return original(*args, **kwargs)

        monkeypatch.setattr(r, operation, tracked)
        try:
            answer = await commands.handle_command(phrase)
            await _terminal_idle(r)
            assert answer is not None
            assert events == ["view", operation]
        finally:
            await _close_capture_fixture(r, agent, other)

    @pytest.mark.parametrize("phrase", ["keep this instruction", "cancel", "never mind"])
    async def test_legacy_armed_answer_acquires_before_disarm_or_send(self, phrase: str) -> None:
        agent = _CaptureContextAgent()
        r = roster(work=agent)
        observed_armed: list[bool] = []
        commands = AgentTerminalCommands(
            r, on_explicit_view=lambda: observed_armed.append(commands.armed),
        )
        try:
            commands.arm()
            assert observed_armed == []
            assert await commands.handle_command(phrase) is not None
            await _terminal_idle(r)
            assert observed_armed == [True]
            assert not commands.armed
            assert agent.prompts == ([] if phrase in {"cancel", "never mind"} else [phrase])
        finally:
            await _close_capture_fixture(r, agent)

    @pytest.mark.parametrize("phrase", [
        "", "   ", "what is the weather", "switch to decaf",
        "hello terminal", "I use the terminal", "send a joke to my wife",
    ])
    async def test_unrecognized_speech_does_not_acquire_display_rights(self, phrase: str) -> None:
        agent = _CaptureContextAgent()
        r = roster(work=agent)
        acquired: list[bool] = []
        commands = AgentTerminalCommands(r, on_explicit_view=lambda: acquired.append(True))
        try:
            assert await commands.handle_command(phrase) is None
            assert acquired == [] and agent.prompts == []
        finally:
            await _close_capture_fixture(r, agent)

    @pytest.mark.parametrize("exit_kind", ["blank", "expired", "cleared"])
    async def test_an_unaccepted_legacy_answer_does_not_reopen_terminal(self, exit_kind: str) -> None:
        agent = _CaptureContextAgent()
        r = roster(work=agent)
        acquired: list[bool] = []
        now = [100.0]
        commands = AgentTerminalCommands(
            r, monotonic=lambda: now[0], on_explicit_view=lambda: acquired.append(True),
        )
        try:
            commands.arm()
            if exit_kind == "expired":
                now[0] += 46
            elif exit_kind == "cleared":
                commands.clear_capture()
            phrase = " " if exit_kind == "blank" else "an ordinary answer"
            assert await commands.handle_command(phrase) is None
            assert acquired == [] and agent.prompts == []
        finally:
            await _close_capture_fixture(r, agent)

    async def test_scoped_capture_keeps_its_existing_owner_without_a_legacy_view_lease(self) -> None:
        agent = _CaptureContextAgent()
        r = roster(work=agent)
        acquired: list[bool] = []
        commands = AgentTerminalCommands(r, on_explicit_view=lambda: acquired.append(True))
        try:
            capture = commands.begin_capture()
            assert capture is not None
            literal = "  This exact scoped answer?!  "
            assert await commands.consume_capture(capture, literal) == "work ▸ sent."
            assert await commands.consume_capture(capture, literal) is None
            await _terminal_idle(r)
            assert acquired == [] and agent.prompts == [literal]
        finally:
            await _close_capture_fixture(r, agent)

    @pytest.mark.parametrize("reply_available", [False, True])
    async def test_direct_explicit_reply_handoff_acquires_once_even_when_no_reply_exists(
        self, reply_available: bool,
    ) -> None:
        agent = _CaptureContextAgent()
        r = roster(work=agent)
        acquired: list[bool] = []

        def last_reply(who: str | None) -> tuple[str, str] | None:
            assert acquired == [True]
            return ("CLAUDE (critic)", "The chosen reply.") if reply_available else None

        commands = AgentTerminalCommands(
            r, last_reply=last_reply, on_explicit_view=lambda: acquired.append(True),
        )
        try:
            assert await commands.route_reply("claude", None)
            await _terminal_idle(r)
            assert acquired == [True]
            assert agent.prompts == (["The chosen reply."] if reply_available else [])
        finally:
            await _close_capture_fixture(r, agent)


class TestMultiHostRoster:
    """A roster spanning several Macs.

    AGENT_TERMINAL_ROADMAP.md wants one unified roster across configured
    Tailscale Macs, with host labels shown ONLY on ambiguity, and a spoken
    name matching two sessions naming the candidates instead of guessing.
    """

    def test_a_single_host_roster_shows_no_host_labels(self) -> None:
        r = AgentTerminalRoster([
            (LaneAddress("work", "studio"), FakeAgent()),
            (LaneAddress("calendar", "studio"), FakeAgent()),
        ])
        # Every name is unique, so the host is chrome the 576x288 canvas
        # cannot afford — even though both lanes carry one.
        assert r.labels() == ["work", "calendar"]
        assert "studio" not in r.overview()

    def test_a_repeated_name_gains_its_host_on_every_row(self) -> None:
        r = AgentTerminalRoster([
            (LaneAddress("work", "mini"), FakeAgent()),
            (LaneAddress("work", "studio"), FakeAgent()),
            (LaneAddress("calendar", "mini"), FakeAgent()),
        ])
        # Both "work" rows qualify; showing the host on one and not the
        # other would be worse than showing neither. "calendar" is still
        # unique, so it stays bare.
        assert r.labels() == ["work (mini)", "work (studio)", "calendar"]

    def test_two_hosts_offering_one_name_are_two_lanes(self) -> None:
        first, second = FakeAgent(), FakeAgent()
        r = AgentTerminalRoster([
            (LaneAddress("work", "mini"), first),
            (LaneAddress("work", "studio"), second),
        ])
        # The pair is the identity: a name collision must not collapse two
        # real sessions on two real Macs into one row.
        assert len(r.names()) == 2
        assert r.lane("work@mini") is not r.lane("work@studio")

    def test_an_ambiguous_spoken_name_names_the_candidates(self) -> None:
        r = AgentTerminalRoster([
            (LaneAddress("work", "mini"), FakeAgent()),
            (LaneAddress("work", "studio"), FakeAgent()),
        ])
        resolution = r.resolve("work")
        assert not resolution.found
        assert resolution.ambiguous
        assert set(resolution.candidates) == {"work (mini)", "work (studio)"}
        # And it must not silently take the first, which is exactly what
        # the roadmap says a repeated label used to do.
        assert r.focus("work") is False

    def test_the_qualified_name_resolves_to_exactly_one_lane(self) -> None:
        r = AgentTerminalRoster([
            (LaneAddress("work", "mini"), FakeAgent()),
            (LaneAddress("work", "studio"), FakeAgent()),
        ])
        assert r.focus("work (studio)") is True
        assert r.focused == "work@studio"

    def test_an_unambiguous_name_still_resolves_bare(self) -> None:
        r = AgentTerminalRoster([
            (LaneAddress("work", "mini"), FakeAgent()),
            (LaneAddress("calendar", "studio"), FakeAgent()),
        ])
        assert r.focus("calendar") is True
        assert r.focused == "calendar@studio"

    def test_hostless_lanes_keep_their_exact_previous_identity(self) -> None:
        # Every single-host caller passes plain strings; their keys must
        # stay the bare names so nothing downstream has to learn a new one.
        r = AgentTerminalRoster([("work", FakeAgent()), ("hermes", FakeAgent())])
        assert r.names() == ["work", "hermes"]
        assert r.labels() == ["work", "hermes"]
        assert r.focus("hermes") is True and r.focused == "hermes"

    async def test_an_ambiguous_switch_asks_which_mac(self) -> None:
        r = AgentTerminalRoster([
            (LaneAddress("work", "mini"), FakeAgent()),
            (LaneAddress("work", "studio"), FakeAgent()),
        ])
        answer = await AgentTerminalCommands(r).handle_command(
            "switch terminal to work",
        )
        assert answer is not None
        assert "mini" in answer and "studio" in answer
        # Focus did not move to an arbitrary Mac.
        assert r.focused == "work@mini"


def test_agent_choices_exclude_internal_sessions_without_deleting_them():
    names = ["openclaw", "hermes", "work", "hermes session", "calendar"]
    r = AgentTerminalRoster([(name, FakeAgent()) for name in names],
                            visible_lanes=("openclaw", "hermes"))
    assert [name for name, _ in r.picker_rows()] == ["openclaw", "hermes"]
    assert r.lane("calendar") is not None
    assert r.lane("work") is not None
    assert len(r.frame().lanes) == 2
    empty = AgentTerminalRoster([("work", FakeAgent())], visible_lanes=())
    assert empty.picker_rows() == []
