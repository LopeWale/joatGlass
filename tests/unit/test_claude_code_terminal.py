"""Claude Code Agent Terminal lanes: JOAT owns output, approvals, stop and resume."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from joat.agents.claude_code_terminal import (
    ClaudeCodeTerminalAgent,
    ClaudeTerminalLaneConfig,
    load_lane_configs,
    summarize_tool,
)
from joat.services.agent_terminal import AgentTerminalRoster


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    name: str
    input: dict[str, Any]
    id: str = "tool-1"


@dataclass
class AssistantMessage:
    content: list[Any]
    session_id: str | None = "sess-1"


@dataclass
class ResultMessage:
    is_error: bool = False
    session_id: str = "sess-1"
    errors: list[str] | None = None
    result: str | None = None
    subtype: str = "success"


@dataclass
class PermissionResultAllow:
    updated_input: dict[str, Any] | None = None
    behavior: str = "allow"


@dataclass
class PermissionResultDeny:
    message: str = ""
    behavior: str = "deny"


@dataclass
class ClaudeAgentOptions:
    cwd: str | None = None
    cli_path: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    resume: str | None = None
    permission_mode: str | None = None
    can_use_tool: Any = None
    setting_sources: list[str] | None = None


class FakeSdk:
    """The slice of claude_agent_sdk the lane uses, with a scripted session."""

    AssistantMessage = AssistantMessage
    ResultMessage = ResultMessage
    TextBlock = TextBlock
    ToolUseBlock = ToolUseBlock
    PermissionResultAllow = PermissionResultAllow
    PermissionResultDeny = PermissionResultDeny
    ClaudeAgentOptions = ClaudeAgentOptions

    def __init__(self) -> None:
        self.script: list[Any] = []
        self.options: list[ClaudeAgentOptions] = []
        self.prompts: list[str] = []
        self.permission_results: list[Any] = []
        self.interrupted = 0
        self.hang = False
        self.sessions: list[Any] = []
        self.messages: list[Any] = []
        sdk = self

        class Client:
            def __init__(self, options: ClaudeAgentOptions) -> None:
                sdk.options.append(options)
                self.options = options

            async def connect(self) -> None:
                return None

            async def query(self, prompt: str) -> None:
                sdk.prompts.append(prompt)

            async def receive_response(self):
                for item in sdk.script:
                    if item == "ASK":
                        result = await self.options.can_use_tool(
                            "Bash", {"command": "rm -rf build"}, SimpleNamespace(),
                        )
                        sdk.permission_results.append(result)
                        continue
                    yield item
                while sdk.hang:
                    await asyncio.sleep(0.01)

            async def interrupt(self) -> None:
                sdk.interrupted += 1

            async def disconnect(self) -> None:
                return None

        self.ClaudeSDKClient = Client

    def list_sessions(self, directory: str | None, limit: int | None) -> list[Any]:
        return self.sessions[:limit]

    def get_session_messages(self, session_id: str, directory: str | None) -> list[Any]:
        return self.messages


def lane(sdk: FakeSdk, cwd: str = "/tmp", **kw: Any) -> ClaudeCodeTerminalAgent:
    return ClaudeCodeTerminalAgent(
        ClaudeTerminalLaneConfig("claude", cwd), cli_path=lambda: "/usr/local/bin/claude", sdk=sdk, **kw,
    )


async def collect(agent: ClaudeCodeTerminalAgent, prompt: str) -> str:
    return "".join([chunk async for chunk in agent.stream(prompt)])


async def test_streams_text_and_tool_lines_then_continues_the_same_session() -> None:
    sdk = FakeSdk()
    sdk.script = [
        AssistantMessage([TextBlock("Looking."), ToolUseBlock("Read", {"file_path": "/repo/app.py"})]),
        ResultMessage(),
    ]
    agent = lane(sdk)
    out = await collect(agent, "check app.py")
    assert out == "Looking.\n⏺ Read: /repo/app.py\n"
    assert agent.session_id == "sess-1"
    assert sdk.options[0].resume is None
    await collect(agent, "and now?")
    assert sdk.options[1].resume == "sess-1"
    assert sdk.options[1].setting_sources == ["user", "project", "local"]


async def test_an_error_result_fails_the_turn_with_its_reason() -> None:
    sdk = FakeSdk()
    sdk.script = [ResultMessage(is_error=True, errors=["Not logged in"])]
    with pytest.raises(RuntimeError, match="Not logged in"):
        await collect(lane(sdk), "hi")


@pytest.mark.parametrize(("decision", "expected"), [("approve", "allow"), ("deny", "deny"), ("reject", "deny")])
async def test_a_permission_prompt_waits_for_the_wearer(decision: str, expected: str) -> None:
    sdk = FakeSdk()
    sdk.script = ["ASK", ResultMessage()]
    seen: list[tuple[str, str, str]] = []
    agent = lane(sdk)
    agent.set_approval_callback(lambda *args: seen.append(args))
    turn = asyncio.create_task(collect(agent, "clean"))
    for _ in range(50):
        if seen:
            break
        await asyncio.sleep(0.01)
    approval_id, kind, summary = seen[0]
    assert (kind, summary) == ("Bash", "Bash: rm -rf build")
    assert not turn.done()
    await agent.resolve_approval(approval_id, kind, decision)
    await turn
    assert sdk.permission_results[0].behavior == expected
    with pytest.raises(RuntimeError):
        await agent.resolve_approval(approval_id, kind, decision)


async def test_cancel_interrupts_and_reports_confirmed() -> None:
    sdk = FakeSdk()
    sdk.script = [AssistantMessage([TextBlock("1")])]
    sdk.hang = True
    agent = lane(sdk)
    turn = asyncio.create_task(collect(agent, "count"))
    await asyncio.sleep(0.05)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    assert sdk.interrupted == 1
    assert agent.cancellation_status == "confirmed"


async def test_lists_sessions_everywhere_and_resumes_only_a_listed_one_in_its_folder(tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    sdk = FakeSdk()
    sdk.sessions = [
        SimpleNamespace(session_id="a", custom_title=None, summary="Fix the login", first_prompt="x", cwd=str(tmp_path)),
        SimpleNamespace(session_id="b", custom_title="Docs pass", summary=None, first_prompt=None, cwd=str(other)),
    ]
    agent = lane(sdk, cwd=str(tmp_path))
    await agent.initialize()
    rows = await agent.list_sessions()
    assert rows == [{"key": "a", "label": "Fix the login"}, {"key": "b", "label": "Docs pass (elsewhere)"}]
    with pytest.raises(RuntimeError):
        await agent.resume_session("never-listed")
    before = agent.capture_context_generation
    await agent.resume_session("b")
    assert agent.capture_context_generation != before
    sdk.script = [ResultMessage(session_id="b")]
    await collect(agent, "continue")
    assert sdk.options[-1].resume == "b" and sdk.options[-1].cwd == str(other)
    sdk.messages = [
        SimpleNamespace(type="user", message={"content": "write docs"}),
        SimpleNamespace(type="assistant", message={"content": [{"type": "text", "text": "Done."}]}),
        SimpleNamespace(type="system", message={}),
    ]
    assert await agent.history() == [("user", "write docs"), ("assistant", "Done.")]
    await agent.reset_conversation()
    sdk.script = [ResultMessage()]
    await collect(agent, "fresh")
    assert sdk.options[-1].resume is None and sdk.options[-1].cwd == str(tmp_path)


async def test_initialize_refuses_without_the_cli() -> None:
    agent = ClaudeCodeTerminalAgent(ClaudeTerminalLaneConfig("claude", "/tmp"), cli_path=lambda: None, sdk=FakeSdk())
    with pytest.raises(RuntimeError, match="Install Claude Code"):
        await agent.initialize()
    assert agent.capture_context_generation is None


async def test_the_roster_lists_resumes_and_routes_approvals_to_the_lane(tmp_path: Path) -> None:
    sdk = FakeSdk()
    sdk.sessions = [SimpleNamespace(session_id="a", custom_title="Refactor", summary=None, first_prompt=None, cwd=str(tmp_path))]
    agent = lane(sdk, cwd=str(tmp_path))
    roster = AgentTerminalRoster([("claude", agent)], visible_lanes=("claude",))
    agent.set_approval_callback(lambda i, k, s: roster.note_approval("claude", i, k, s))
    await agent.initialize()
    roster.focus("claude")
    assert "Refactor" in await roster.discover()
    assert "resumed 'Refactor'" in await roster.resume("refactor")
    sdk.script = ["ASK", ResultMessage(session_id="a")]
    await roster.send("tidy up")
    for _ in range(50):
        if roster.lane("claude").approvals:
            break
        await asyncio.sleep(0.01)
    assert "recorded approve" in await roster.decide("approve")
    task = roster.lane("claude").task
    if task is not None:
        await task
    assert sdk.permission_results[0].behavior == "allow"


def test_lane_settings_default_and_validation(tmp_path: Path) -> None:
    missing = tmp_path / "none.json"
    assert load_lane_configs(missing, home=tmp_path) == [ClaudeTerminalLaneConfig("claude", str(tmp_path))]
    project = tmp_path / "proj"
    project.mkdir()
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"lanes": [{"name": "App", "cwd": str(project), "permission_mode": "acceptEdits"}]}))
    assert load_lane_configs(good, home=tmp_path) == [ClaudeTerminalLaneConfig("app", str(project), "acceptEdits")]
    for bad in ({"lanes": []}, {"lanes": [{"name": "x", "cwd": str(tmp_path / "gone")}]},
                {"lanes": [{"name": "x", "permission_mode": "yolo"}]},
                {"lanes": [{"name": "x"}, {"name": "X"}]}):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            load_lane_configs(path, home=tmp_path)


def test_tool_summaries_are_one_short_line() -> None:
    assert summarize_tool("Bash", {"command": "ls   -la\n/tmp"}) == "Bash: ls -la /tmp"
    assert summarize_tool("Todo", {}) == "Todo"
    assert len(summarize_tool("Bash", {"command": "x" * 500})) == 120


async def test_opening_the_lane_without_claude_code_says_how_to_fix_it() -> None:
    agent = ClaudeCodeTerminalAgent(ClaudeTerminalLaneConfig("claude", "/tmp"), cli_path=lambda: None, sdk=FakeSdk())
    roster = AgentTerminalRoster([("claude", agent)], visible_lanes=("claude",))
    assert not await roster.prepare_lane("claude", is_current=lambda: True)
    state, reason = roster.lane("claude").preparation_status
    assert state == "failed" and reason.startswith("Install Claude Code on this Mac")
