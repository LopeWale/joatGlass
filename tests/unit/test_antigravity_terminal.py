"""Antigravity Agent Terminal lane: JOAT-hosted agent, shell approvals, cancel,
a conversation that survives restarts, and history from the saved session."""

from __future__ import annotations

import asyncio
import enum
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from joat.agents.antigravity_terminal import (
    AntigravityTerminalAgent,
    gemini_api_key,
    load_antigravity_lane,
    load_antigravity_model,
    store_gemini_api_key,
    tool_detail,
    tool_line,
    wearer_error,
)
from joat.services.agent_terminal import AgentTerminalRoster


class Text:
    def __init__(self, text: str) -> None:
        self.text = text


class ToolCall:
    def __init__(self, name: Any, args: dict[str, Any]) -> None:
        self.name, self.args = name, args


class Mode(str, enum.Enum):
    RESUME = "resume"


class Tool(str, enum.Enum):
    VIEW_FILE = "view_file"


def step(id_: str, source: str, content: str, *, target: str = "UNKNOWN",
         status: str = "DONE", depth: int = 0) -> SimpleNamespace:
    return SimpleNamespace(id=id_, source=source, target=target, status=status,
                           content=content, depth=depth)


class FakeSdk:
    def __init__(self) -> None:
        sdk = self
        self.configs: list[dict[str, Any]] = []
        self.script: list[Any] = []
        self.hang = False
        self.cancelled = 0
        self.asked: list[bool] = []
        self.entered = 0
        self.exited = 0
        self.conversation_id = "conv1"
        self.fail_resume = False
        self.fail_chat: Exception | None = None
        self.history: list[Any] = []

        class Response:
            @property
            def chunks(self):
                async def gen():
                    if sdk.fail_chat is not None:
                        raise sdk.fail_chat
                    for item in sdk.script:
                        if item == "ASK":
                            sdk.asked.append(await sdk.handler(ToolCall("run_command", {"command": "make"})))
                            continue
                        if item == "BUSY":
                            sdk.history.append(busy_step(f"busy{len(sdk.history)}"))
                            continue
                        if item == "WAIT":
                            await asyncio.sleep(0.7)  # let the watcher see it
                            continue
                        yield item
                    while sdk.hang:
                        await asyncio.sleep(0.01)
                return gen()

            async def cancel(self) -> None:
                sdk.cancelled += 1
                sdk.hang = False

        class Agent:
            def __init__(self, config: dict[str, Any]) -> None:
                sdk.configs.append(config)
                self.conversation = SimpleNamespace(history=sdk.history)

            async def __aenter__(self) -> "Agent":
                if sdk.fail_resume and sdk.configs[-1].get("conversation_id"):
                    raise RuntimeError("conversation not found (cannot resume)")
                sdk.entered += 1
                return self

            async def __aexit__(self, *exc: Any) -> None:
                sdk.exited += 1

            @property
            def conversation_id(self) -> str:
                return sdk.conversation_id

            async def chat(self, prompt: str) -> Response:
                return Response()

        def local_config(**kwargs: Any) -> dict[str, Any]:
            return kwargs

        def plain(**kwargs: Any) -> dict[str, Any]:
            return kwargs

        def confirm_run_command(handler: Any) -> list[str]:
            sdk.handler = handler
            return ["confirm"]

        self.ag = SimpleNamespace(LocalAgentConfig=local_config, Agent=Agent,
                                  types=SimpleNamespace(Text=Text, ToolCall=ToolCall,
                                                        SessionContinuationMode=Mode,
                                                        RetryConfig=plain, ModelAPIRetryConfig=plain))
        self.policy = SimpleNamespace(confirm_run_command=confirm_run_command)


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


def lane(sdk: FakeSdk, state: Path, key: str | None = "k") -> AntigravityTerminalAgent:
    return AntigravityTerminalAgent("antigravity", "/tmp", api_key=lambda: key, sdk=sdk, state_dir=state)


async def collect(agent: AntigravityTerminalAgent, prompt: str) -> str:
    return "".join([c async for c in agent.stream(prompt)])


async def test_streams_text_and_tools_and_keeps_one_agent_per_conversation(state: Path) -> None:
    sdk = FakeSdk()
    sdk.script = [Text("Building"), Text("."), ToolCall(Tool.VIEW_FILE, {"path": "a.py"}), Text("Done.")]
    agent = lane(sdk, state)
    assert await collect(agent, "build") == "Building.\n⏺ view_file: a.py\nDone.\n"
    config = sdk.configs[0]
    assert config["workspaces"] == ["/tmp"] and config["api_key"] == "k"
    # JOAT owns the saved session and the agent's app data, away from the
    # Antigravity app's ~/.gemini/antigravity.
    assert config["save_dir"] == str(state / "conversations")
    assert config["app_data_dir"] == str(state / "app-data")
    assert "conversation_id" not in config and "glasses" in config["system_instructions"]
    await collect(agent, "again")
    assert sdk.entered == 1
    await agent.reset_conversation()
    assert sdk.exited == 1 and agent.conversation_id is None
    await collect(agent, "fresh")
    assert sdk.entered == 2 and "conversation_id" not in sdk.configs[1]


async def test_the_conversation_survives_a_restart(state: Path) -> None:
    sdk = FakeSdk()
    sdk.script = [Text("hi")]
    await collect(lane(sdk, state), "remember 7")
    saved = json.loads((state / "state.json").read_text())
    assert saved == {"conversation_id": "conv1"}
    assert stat.S_IMODE((state / "state.json").stat().st_mode) == 0o600
    restarted = lane(sdk, state)
    await collect(restarted, "what number?")
    assert sdk.configs[-1]["conversation_id"] == "conv1"
    assert sdk.configs[-1]["session_continuation_mode"] is Mode.RESUME


async def test_a_lost_session_starts_fresh_and_says_so(state: Path) -> None:
    sdk = FakeSdk()
    sdk.script = [Text("ok")]
    await collect(lane(sdk, state), "one")
    sdk.fail_resume = True
    sdk.conversation_id = "conv2"
    out = await collect(lane(sdk, state), "two")
    assert out.startswith("(The last Antigravity conversation could not be reopened")
    assert "conversation_id" not in sdk.configs[-1]
    assert json.loads((state / "state.json").read_text()) == {"conversation_id": "conv2"}


@pytest.mark.parametrize(("decision", "allowed"), [("approve", True), ("deny", False)])
async def test_shell_commands_wait_for_the_wearer(decision: str, allowed: bool, state: Path) -> None:
    sdk = FakeSdk()
    sdk.script = ["ASK", Text("ok")]
    seen: list[tuple[str, str, str]] = []
    agent = lane(sdk, state)
    agent.set_approval_callback(lambda *a: seen.append(a))
    turn = asyncio.create_task(collect(agent, "make it"))
    for _ in range(100):
        if seen:
            break
        await asyncio.sleep(0.01)
    assert seen[0][1:] == ("run_command", "make")
    await agent.resolve_approval(seen[0][0], seen[0][1], decision)
    await turn
    assert sdk.asked == [allowed]


async def test_cancel_stops_the_turn(state: Path) -> None:
    sdk = FakeSdk()
    sdk.script = [Text("1")]
    sdk.hang = True
    agent = lane(sdk, state)
    turn = asyncio.create_task(collect(agent, "count"))
    await asyncio.sleep(0.05)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    assert sdk.cancelled == 1 and agent.cancellation_status == "confirmed"


async def test_a_refused_key_reads_as_one_line_and_the_next_turn_restarts(state: Path) -> None:
    sdk = FakeSdk()
    sdk.fail_chat = RuntimeError(
        'Agent execution terminated due to error. ("agent executor error: Error 400, '
        "Message: API key not valid. Please pass a valid API key., Status: INVALID_ARGUMENT, "
        "Details: [map[reason:API_KEY_INVALID]]\")"
    )
    agent = lane(sdk, state)
    with pytest.raises(RuntimeError, match="^Antigravity refused the Gemini API key"):
        await collect(agent, "hi")
    assert sdk.exited == 1  # the runtime is restarted on the next turn
    sdk.fail_chat = None
    sdk.script = [Text("ok")]
    assert await collect(agent, "hi") == "ok\n"
    assert sdk.configs[-1]["conversation_id"] == "conv1"


def test_wearer_error_is_short() -> None:
    assert wearer_error(RuntimeError("Status: RESOURCE_EXHAUSTED")).startswith("The Gemini API quota")
    line = wearer_error(RuntimeError("x" * 500))
    assert line.startswith("Antigravity failed: ") and len(line) < 150


def test_tool_lines_name_antigravitys_own_arguments() -> None:
    # Seen in the simulator: the approval showed the raw JSON arguments.
    from joat.agents.antigravity_terminal import clean_glasses_text

    assert tool_line("run_command", {"CommandLine": "ls -d */", "Cwd": "/repo"}) == "run_command: ls -d */"
    assert tool_line("run_command", {"command_line": "git status", "working_dir": "/repo"}) == "run_command: git status"
    assert tool_line("view_file", {"AbsolutePath": "/repo/a.py"}) == "view_file: /repo/a.py"
    assert tool_detail({"Cwd": "/repo"}) == ""
    assert len(tool_detail({"CommandLine": "x" * 400})) == 160
    assert clean_glasses_text("<thought>\nName is [joat-g2](file:///repo/pyproject.toml#L2).") == "Name is joat-g2."


async def test_history_reads_the_saved_session(state: Path) -> None:
    sdk = FakeSdk()
    sdk.script = [Text("ok")]
    await collect(lane(sdk, state), "first")
    sdk.history[:] = [
        step("c:0", "USER", "what is 2+2"),
        step("c:1", "MODEL", "It is", target="TARGET_USER"),
        step("c:1", "MODEL", "It is 4.", target="TARGET_USER"),  # same step, later update
        step("c:2", "MODEL", "inner", target="TARGET_USER", depth=1),  # a subagent
        step("c:3", "SYSTEM", "Agent execution terminated", status="ERROR", target="TARGET_USER"),
    ]
    restarted = lane(sdk, state)  # no turn yet: history reopens the saved session
    assert await restarted.history() == [("user", "what is 2+2"), ("agent", "It is 4.")]
    assert sdk.configs[-1]["conversation_id"] == "conv1"


async def test_history_without_a_conversation_is_empty(state: Path) -> None:
    assert await lane(FakeSdk(), state).history() == []


def busy_step(id_: str) -> SimpleNamespace:
    s = step(id_, "SYSTEM", "", status="ERROR", target="TARGET_USER")
    s.error, s.http_code = "Error 503, This model is currently experiencing high demand. UNAVAILABLE", 503
    return s


async def test_a_busy_model_is_said_while_it_retries(state: Path) -> None:
    # Seen in the simulator: four minutes of WORKING with no reason given.
    sdk = FakeSdk()
    sdk.script = ["BUSY", "WAIT", Text("Answer.")]
    out = await collect(lane(sdk, state), "q")
    assert out == "(Gemini is busy; retrying…)\nAnswer.\n"


async def test_retries_that_run_out_fail_with_one_line(state: Path) -> None:
    sdk = FakeSdk()
    sdk.script = ["BUSY"]  # the turn ends with nothing said
    agent = lane(sdk, state)
    with pytest.raises(RuntimeError, match="^The Gemini API did not answer"):
        await collect(agent, "q")


async def test_a_model_busy_for_too_long_stops_the_turn(
    state: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("joat.agents.antigravity_terminal.BUSY_GIVE_UP_S", 0.05)
    sdk = FakeSdk()
    sdk.script = ["BUSY"]
    sdk.hang = True
    agent = lane(sdk, state)
    with pytest.raises(RuntimeError, match="^The Gemini API did not answer"):
        await collect(agent, "q")
    assert sdk.cancelled >= 1


def test_model_and_retry_settings(tmp_path: Path, state: Path) -> None:
    settings = tmp_path / "antigravity-terminal.json"
    assert load_antigravity_model(settings) is None
    settings.write_text('{"model": "gemini-4-hydrogen"}')
    assert load_antigravity_model(settings) == "gemini-4-hydrogen"
    settings.write_text('{"model": "bad model; rm"}')
    assert load_antigravity_model(settings) is None
    sdk = FakeSdk()
    agent = AntigravityTerminalAgent("antigravity", "/tmp", api_key=lambda: "k", sdk=sdk,
                                     state_dir=state, model="gemini-4-hydrogen")
    config = agent._config(sdk, "k", None)
    assert config["model"] == "gemini-4-hydrogen"
    assert config["retry_config"] == {"api_retry": {"max_retries": 5, "initial_sleep_duration_ms": 2000,
                                                    "exponential_multiplier": 2.0}}


async def test_without_a_key_the_lane_says_how_to_fix_it(state: Path) -> None:
    agent = lane(FakeSdk(), state, key=None)
    roster = AgentTerminalRoster([("antigravity", agent)], visible_lanes=("antigravity",))
    assert not await roster.prepare_lane("antigravity", is_current=lambda: True)
    assert roster.lane("antigravity").preparation_status[1].startswith("Add a Gemini API key")


def test_key_from_env_or_a_private_file(tmp_path: Path) -> None:
    key_file = tmp_path / "gemini-api-key"
    assert gemini_api_key(key_file, env={"GEMINI_API_KEY": " abc "}) == "abc"
    assert gemini_api_key(key_file, env={}) is None
    key_file.write_text("filekey\n")
    os.chmod(key_file, 0o644)
    assert gemini_api_key(key_file, env={}) is None
    os.chmod(key_file, 0o600)
    assert gemini_api_key(key_file, env={}) == "filekey"


def test_stored_key_is_owner_only(tmp_path: Path) -> None:
    key_file = tmp_path / "JOAT" / "gemini-api-key"
    store_gemini_api_key("  AIzaSecret \n", key_file)
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(key_file.parent.stat().st_mode) == 0o700
    assert gemini_api_key(key_file, env={}) == "AIzaSecret"
    with pytest.raises(ValueError):
        store_gemini_api_key("two words", key_file)


def test_lane_settings(tmp_path: Path) -> None:
    assert load_antigravity_lane(tmp_path / "none.json", home=tmp_path) == ("antigravity", str(tmp_path))
    bad = tmp_path / "bad.json"
    bad.write_text('{"cwd": "/definitely/not/here"}')
    with pytest.raises(ValueError):
        load_antigravity_lane(bad, home=tmp_path)


async def test_sessions_index_persists_across_reset_and_supports_list_and_resume(
    tmp_path: Path, state: Path,
) -> None:
    first_dir = tmp_path / "first_repo"
    second_dir = tmp_path / "second_repo"
    first_dir.mkdir()
    second_dir.mkdir()

    sdk = FakeSdk()
    sdk.script = [Text("Read pyproject.")]
    sdk.conversation_id = "conv-alpha"
    agent = AntigravityTerminalAgent(
        "antigravity", str(first_dir), api_key=lambda: "k", sdk=sdk, state_dir=state,
    )
    assert agent.policy.roster is True and agent.policy.allows_approvals is True

    await collect(agent, "Read pyproject.toml and summarize")
    sessions_file = state / "sessions.json"
    assert sessions_file.exists()
    assert stat.S_IMODE(sessions_file.stat().st_mode) == 0o600

    # Starting "+ New session" clears active conversation_id WITHOUT erasing sessions.json!
    await agent.reset_conversation()
    assert agent.conversation_id is None

    await agent.set_cwd(str(second_dir))
    sdk.conversation_id = "conv-beta"
    sdk.script = [Text("Clean working tree.")]
    await collect(agent, "Check git status")

    listed = await agent.list_sessions()
    assert [row["key"] for row in listed] == ["conv-beta", "conv-alpha"]
    assert listed[0]["label"] == "Check git status"
    assert listed[0]["cwd"] == str(second_dir)
    assert "Read pyproject.toml and summarize" in listed[1]["label"]
    assert "(first_repo)" in listed[1]["label"]
    assert listed[1]["cwd"] == str(first_dir)

    # Reset again and verify both prior sessions are still listed and resumable.
    await agent.reset_conversation()
    assert agent.conversation_id is None
    listed_after_reset = await agent.list_sessions()
    assert [row["key"] for row in listed_after_reset] == ["conv-beta", "conv-alpha"]

    with pytest.raises(RuntimeError):
        await agent.resume_session("conv-unknown")

    before_gen = agent.capture_context_generation
    await agent.resume_session("conv-alpha")
    assert agent.conversation_id == "conv-alpha"
    assert agent.cwd == str(first_dir)
    assert agent.capture_context_generation != before_gen

    sdk.script = [Text("Resumed alpha.")]
    await collect(agent, "Continue")
    assert sdk.configs[-1]["conversation_id"] == "conv-alpha"
    assert sdk.configs[-1]["session_continuation_mode"] is Mode.RESUME
    assert sdk.configs[-1]["workspaces"] == [str(first_dir)]

