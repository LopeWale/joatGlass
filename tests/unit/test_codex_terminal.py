"""Codex Agent Terminal lanes over `codex app-server` (JSONL JSON-RPC)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from joat.agents.codex_terminal import CodexTerminalAgent, CodexTerminalLaneConfig, load_codex_lane_configs
from joat.services.agent_terminal import AgentTerminalRoster


class FakeAppServer:
    """A scripted app-server: answers requests, emits a turn's notifications."""

    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.requests: list[dict[str, Any]] = []
        self.answers: list[dict[str, Any]] = []
        self.returncode: int | None = None
        self.turn_script: list[dict[str, Any]] = []
        self.hang = False
        self.threads = [{"id": "thr_a", "preview": "Fix the build", "cwd": "/tmp"}]
        server = self

        class Stdin:
            def write(self, data: bytes) -> None:
                for line in data.decode().splitlines():
                    server._receive(json.loads(line))

            async def drain(self) -> None:
                return None

        self.stdin = Stdin()

    def emit(self, message: dict[str, Any]) -> None:
        self.stdout.feed_data((json.dumps(message) + "\n").encode())

    def _receive(self, message: dict[str, Any]) -> None:
        if "method" not in message:
            self.answers.append(message)
            if self.hang and message.get("result", {}).get("decision"):
                self.emit({"method": "turn/completed", "params": {"turn": {"id": "turn_1", "status": "completed"}}})
            return
        self.requests.append(message)
        method, rid = message["method"], message.get("id")
        if rid is None:
            return
        if method == "initialize":
            self.emit({"id": rid, "result": {}})
        elif method == "thread/start":
            self.emit({"id": rid, "result": {"thread": {"id": "thr_new"}}})
        elif method == "turn/start":
            self.emit({"id": rid, "result": {"turn": {"id": "turn_1"}}})
            for event in self.turn_script:
                self.emit(event)
        elif method == "turn/interrupt":
            self.emit({"id": rid, "result": {}})
            self.emit({"method": "turn/completed", "params": {"turn": {"id": "turn_1", "status": "interrupted"}}})
        elif method == "thread/list":
            self.emit({"id": rid, "result": {"data": self.threads, "nextCursor": None}})
        elif method == "thread/resume":
            self.emit({"id": rid, "result": {"thread": {"id": message["params"]["threadId"]}}})

    async def wait(self) -> int:
        return 0

    def terminate(self) -> None:
        self.returncode = 0


def lane(server: FakeAppServer, cwd: str = "/tmp") -> CodexTerminalAgent:
    async def spawn(*args: Any, **kwargs: Any) -> FakeAppServer:
        server.spawned = (args, kwargs)  # type: ignore[attr-defined]
        return server
    return CodexTerminalAgent(CodexTerminalLaneConfig("codex", cwd), binary=lambda: "/bin/codex", spawn=spawn)


def done(status: str = "completed", **extra: Any) -> dict[str, Any]:
    return {"method": "turn/completed", "params": {"turn": {"id": "turn_1", "status": status, **extra}}}


async def collect(agent: CodexTerminalAgent, prompt: str) -> str:
    return "".join([chunk async for chunk in agent.stream(prompt)])


async def test_handshake_thread_and_streamed_items() -> None:
    server = FakeAppServer()
    server.turn_script = [
        {"method": "item/started", "params": {"item": {"type": "commandExecution", "id": "i1", "command": "ls"}}},
        {"method": "item/completed", "params": {"item": {"type": "agentMessage", "id": "i2", "text": "Two files."}}},
        done(),
    ]
    agent = lane(server)
    await agent.initialize()
    out = await collect(agent, "list files")
    assert out == "⏺ Run: ls\nTwo files.\n"
    methods = [r["method"] for r in server.requests]
    assert methods == ["initialize", "initialized", "thread/start", "turn/start"]
    start = server.requests[2]["params"]
    assert start == {"cwd": "/tmp", "approvalPolicy": "on-request", "sandbox": "workspace-write"}
    assert server.spawned[0][1] == "app-server"  # type: ignore[attr-defined]
    server.turn_script = [done()]
    await collect(agent, "again")
    assert [r["method"] for r in server.requests].count("thread/start") == 1


async def test_a_failed_turn_reports_its_error() -> None:
    server = FakeAppServer()
    server.turn_script = [done("failed", error={"message": "usage limit reached"})]
    with pytest.raises(RuntimeError, match="usage limit reached"):
        await collect(lane(server), "go")


@pytest.mark.parametrize(("decision", "sent"), [("approve", "accept"), ("deny", "decline")])
async def test_command_approval_waits_for_the_wearer(decision: str, sent: str) -> None:
    server = FakeAppServer()
    server.hang = True
    server.turn_script = [{"method": "item/commandExecution/requestApproval", "id": 900,
                           "params": {"threadId": "thr_new", "turnId": "turn_1", "itemId": "i1", "command": "rm -rf dist"}}]
    seen: list[tuple[str, str, str]] = []
    agent = lane(server)
    agent.set_approval_callback(lambda *a: seen.append(a))
    turn = asyncio.create_task(collect(agent, "clean"))
    for _ in range(100):
        if seen:
            break
        await asyncio.sleep(0.01)
    approval_id, kind, summary = seen[0]
    assert (kind, summary) == ("command", "Run: rm -rf dist")
    await agent.resolve_approval(approval_id, kind, decision)
    await asyncio.wait_for(turn, 2)
    assert server.answers[-1] == {"id": 900, "result": {"decision": sent}}


async def test_cancel_sends_turn_interrupt_and_confirms() -> None:
    server = FakeAppServer()
    server.turn_script = [{"method": "turn/started", "params": {"turn": {"id": "turn_1"}}}]
    agent = lane(server)
    turn = asyncio.create_task(collect(agent, "long job"))
    await asyncio.sleep(0.05)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    interrupt = next(r for r in server.requests if r["method"] == "turn/interrupt")
    assert interrupt["params"] == {"threadId": "thr_new", "turnId": "turn_1"}
    assert agent.cancellation_status == "confirmed"


async def test_lists_and_resumes_a_saved_thread_through_the_roster(tmp_path: Path) -> None:
    server = FakeAppServer()
    server.threads = [{"id": "thr_a", "preview": "Fix the build", "cwd": str(tmp_path)}]
    agent = lane(server, cwd=str(tmp_path))
    roster = AgentTerminalRoster([("codex", agent)], visible_lanes=("codex",))
    await agent.initialize()
    roster.focus("codex")
    assert "Fix the build" in await roster.discover()
    assert "resumed 'Fix the build'" in await roster.resume("fix the build")
    server.turn_script = [done()]
    await collect(agent, "continue")
    turn = next(r for r in server.requests if r["method"] == "turn/start")
    assert turn["params"]["threadId"] == "thr_a"
    assert "thread/start" not in [r["method"] for r in server.requests]


async def test_missing_codex_says_how_to_fix_it() -> None:
    agent = CodexTerminalAgent(CodexTerminalLaneConfig("codex", "/tmp"), binary=lambda: None)
    roster = AgentTerminalRoster([("codex", agent)], visible_lanes=("codex",))
    assert not await roster.prepare_lane("codex", is_current=lambda: True)
    assert roster.lane("codex").preparation_status[1].startswith("Install Codex on this Mac")


def test_settings_default_and_validation(tmp_path: Path) -> None:
    assert load_codex_lane_configs(tmp_path / "none.json", home=tmp_path) == [CodexTerminalLaneConfig("codex", str(tmp_path))]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"lanes": [{"name": "codex", "sandbox": "everything"}]}))
    with pytest.raises(ValueError):
        load_codex_lane_configs(bad, home=tmp_path)
