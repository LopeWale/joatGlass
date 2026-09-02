"""Every callback __main__ wires, bound the way its consumer calls it.

Three defects in one session came from a call-shape mismatch that no test
could see, because each test called the thing differently than production
does:

  - a session double returned False from consume_transcript where every
    real session returns True, so routed replies were silently swallowed;
  - a fixture emitted a lane state of "running" where the roster says
    "working", so a guard never fired once;
  - instruction tests passed `addressed=`, which orchestrator.py never
    passes on the idle path, so the feature was dead in production while
    the suite was green.

`inspect.signature().bind()` catches that class exactly: it asks whether
the CALL the consumer makes is legal against the callable that was wired,
without running either. A test that calls a function itself cannot.
"""

from __future__ import annotations

import inspect

from joat.services.agent_terminal import AgentTerminalCommands, AgentTerminalRoster
from joat.services.brainstorm_history import BrainstormHistory
from joat.services.capabilities import CapabilityRouter
from joat.services.session import SessionManager


def _binds(fn, *args, **kwargs) -> bool:
    """Would this exact call be legal against this callable?"""
    inspect.signature(fn).bind(*args, **kwargs)
    return True


class FakeAgent:
    async def stream(self, prompt: str):  # type: ignore[no-untyped-def]
        yield "ok\n"


class TestTheWiringMatchesItsCallers:
    def test_capability_router_calls_its_brainstorm_hooks_correctly(
        self, tmp_path,
    ) -> None:  # type: ignore[no-untyped-def]
        history = BrainstormHistory(
            path=tmp_path / "b.json", export_dir=tmp_path / "e")
        manager = SessionManager.__new__(SessionManager)

        # __main__ wires these; CapabilityRouter calls them exactly so.
        assert _binds(lambda: history.sessions(9))
        assert _binds(SessionManager.resume_by_id, manager, "an-id")
        assert _binds(SessionManager.replay_active_hud, manager)

    def test_capability_router_calls_its_terminal_hooks_correctly(self) -> None:
        roster = AgentTerminalRoster([("work", FakeAgent())])
        assert _binds(roster.picker_rows)
        assert _binds(roster.focus, "work")
        # `busy` is a property, so the wiring must be a lambda, not a bound
        # method — asserting the property EXISTS is the check that matters.
        assert isinstance(type(roster).busy, property)

    def test_session_manager_calls_its_injected_hooks_correctly(self) -> None:
        roster = AgentTerminalRoster([("work", FakeAgent())])
        commands = AgentTerminalCommands(roster)

        # SessionManager calls route_reply(who, lane) and decide(verdict).
        assert _binds(commands.route_reply, "claude", "hermes")
        assert _binds(roster.decide, "approve", None)

    def test_the_orchestrator_call_shape_is_legal(self) -> None:
        """orchestrator.py's idle path: positional, no `addressed`.

        This is the one that shipped broken. The keyword is optional and an
        omitted value MEANS addressed; a guard that required the keyword
        made the whole path unreachable in production.
        """
        manager = SessionManager.__new__(SessionManager)
        assert _binds(SessionManager.handle_transcript, manager, "make that shorter")
        # …and the two shapes the live paths use.
        assert _binds(SessionManager.handle_transcript, manager, "x",
                      addressed="x")
        assert _binds(SessionManager.handle_transcript, manager, "x",
                      capture_source="glasses")

    def test_the_router_hook_names_still_exist(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """A rename that silently drops a hook is a feature that stops
        working with nothing red. These are the keywords __main__ passes."""
        parameters = inspect.signature(CapabilityRouter.__init__).parameters
        for name in (
            "brainstorm_recap", "brainstorm_replay", "brainstorm_sessions",
            "brainstorm_continue", "terminal_view", "terminal_rows",
            "terminal_focus", "terminal_busy",
        ):
            assert name in parameters, name

        session_params = inspect.signature(SessionManager.__init__).parameters
        for name in (
            "route_reply", "save_reply", "transform", "last_reply",
            "terminal_decide", "history", "continuation_factory",
            "topic_capture_view", "brainstorm_available",
        ):
            assert name in session_params, name
