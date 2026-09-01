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

from joat.core.protocol import AgentTerminalHudView
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
