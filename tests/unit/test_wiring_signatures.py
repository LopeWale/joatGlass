"""Every callback __main__ wires, bound the way its consumer calls it."""

from __future__ import annotations

import inspect

from joat.core.protocol import TerminalAction
from joat.services.agent_terminal import AgentTerminalCommands, AgentTerminalRoster
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
    def test_capability_router_calls_its_terminal_hooks_correctly(self) -> None:
        roster = AgentTerminalRoster([("work", FakeAgent())])
        assert _binds(roster.picker_rows)
        assert _binds(roster.focus, "work")
        assert _binds(roster.list_lane_sessions, "work")
        assert _binds(roster.resume_lane_session, "work", "sess-1")
        assert _binds(roster.start_new_lane_session, "work")
        assert _binds(roster.lane_active_summary, "work")
        assert isinstance(type(roster).busy, property)

    def test_session_manager_calls_its_injected_hooks_correctly(self) -> None:
        roster = AgentTerminalRoster([("work", FakeAgent())])
        commands = AgentTerminalCommands(roster)
        manager = SessionManager.__new__(SessionManager)

        assert _binds(commands.route_reply, "claude", "work")
        assert _binds(roster.decide, "approve", None)
        assert _binds(
            SessionManager.handle_terminal_action,
            manager,
            TerminalAction(action="begin_terminal_input"),
        )

    def test_the_orchestrator_call_shape_is_legal(self) -> None:
        manager = SessionManager.__new__(SessionManager)
        assert _binds(SessionManager.handle_transcript, manager, "make that shorter")
        assert _binds(SessionManager.handle_transcript, manager, "x", addressed="x")
        assert _binds(SessionManager.handle_transcript, manager, "x", capture_source="glasses")

    def test_the_router_hook_names_still_exist(self) -> None:
        parameters = inspect.signature(CapabilityRouter.__init__).parameters
        for name in (
            "terminal_view", "terminal_rows", "terminal_focus", "terminal_busy",
            "terminal_open", "terminal_current", "terminal_sessions",
            "terminal_resume", "terminal_new_session", "terminal_active_summary",
            "terminal_roster",
        ):
            assert name in parameters, name

        session_params = inspect.signature(SessionManager.__init__).parameters
        for name in (
            "route_reply", "save_reply", "transform", "last_reply",
            "terminal_decide", "terminal_capture", "terminal_retry", "terminal_cancel",
        ):
            assert name in session_params, name
