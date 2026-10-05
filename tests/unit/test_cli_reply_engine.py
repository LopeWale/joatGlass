"""The subscription seats: real spawns of scripted `claude` and `codex` doubles.

Each double records its argv and the environment it received, and speaks
the exact JSON the real binaries emitted on this Mac (2026-09-03), so the
assertions are about the bytes on the wire and the child's real
environment, not about a dict the test built for itself.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from joat.agents.cli_reply_engine import (
    FAILED_NOTICE,
    NO_SEAT_NOTICE,
    LIMIT_NOTICE,
    MISSING_NOTICE,
    SIGNED_OUT_NOTICE,
    ClaudeCliReplyEngine,
    CodexCliReplyEngine,
    McpDoor,
    SeatSwitch,
)
from joat.agents.grounding import Grounding, static_fact
from joat.core.interfaces import ReplyEngine
from joat.core.protocol import AssistantOverlay
from joat.services.hud import HudWriter

JOKE = "Why did the scarecrow win an award? Because he was outstanding in his field."

CLAUDE_OK = r"""#!/bin/sh
printf '%s\0' "$@" > "$RECORD_DIR/argv"
printf '%s|%s|%s' "${ANTHROPIC_API_KEY:-ABSENT}" "${OPENAI_API_KEY:-ABSENT}" "${ANTHROPIC_BASE_URL:-ABSENT}" > "$RECORD_DIR/env"
pwd > "$RECORD_DIR/cwd"
if [ "$1" = "auth" ]; then echo '{"loggedIn": true, "authMethod": "oauth"}'; exit 0; fi
echo '{"type":"system","subtype":"init","model":"claude-opus-5"}'
echo '{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Why did the "}}}'
echo '{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"scarecrow win an award? Because he was outstanding in his field."}}}'
echo '{"type":"result","subtype":"success","result":"Why did the scarecrow win an award? Because he was outstanding in his field.","is_error":false}'
"""

CLAUDE_SIGNED_OUT = r"""#!/bin/sh
if [ "$1" = "auth" ]; then echo '{"loggedIn": false, "authMethod": "none"}'; exit 0; fi
echo 'Not logged in · Please run /login'
exit 1
"""

CLAUDE_LIMIT = r"""#!/bin/sh
echo '{"type":"result","subtype":"error","result":"You have hit your usage limit","is_error":true}'
exit 1
"""

CODEX_OK = r"""#!/bin/sh
printf '%s\0' "$@" > "$RECORD_DIR/argv"
printf '%s|%s' "${OPENAI_API_KEY:-ABSENT}" "${JOAT_AGENT_TOKEN:-ABSENT}" > "$RECORD_DIR/env"
if [ "$1" = "login" ]; then echo 'Logged in using ChatGPT'; exit 0; fi
echo '{"type": "thread.started", "thread_id": "01a06984-cdcb-7321-a21b-c6b2242ce193"}'
echo '{"type": "item.completed", "item": {"id": "item_0", "type": "error", "message": "Under-development features enabled: chronicle."}}'
echo '{"type": "turn.started"}'
echo '{"type": "item.completed", "item": {"id": "item_2", "type": "agent_message", "text": "Why did the developer go broke? He used up all his cache."}}'
echo '{"type": "turn.completed", "usage": {"input_tokens": 20942, "output_tokens": 167}}'
"""

CODEX_SIGNED_OUT = r"""#!/bin/sh
if [ "$1" = "login" ]; then echo 'Not logged in'; exit 1; fi
echo 'Error: not logged in. Run codex login.' >&2
exit 1
"""


def script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture
def record_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "record"
    directory.mkdir()
    monkeypatch.setenv("RECORD_DIR", str(directory))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-LEAKED")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-LEAKED")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://proxy.invalid")
    return directory


async def drain(engine: ReplyEngine, text: str) -> list[str]:
    return [chunk async for chunk in engine.stream(text)]


def argv_of(record_dir: Path) -> list[str]:
    return (record_dir / "argv").read_text().split("\0")[:-1]


class LocalEngine(ReplyEngine):
    def __init__(self, text: str, available: bool = True) -> None:
        self.text = text
        self.available = available
        self.asked: list[str] = []

    async def reply(self, text: str) -> str | None:
        return self.text

    async def stream(self, text: str) -> AsyncIterator[str]:
        self.asked.append(text)
        yield self.text


# ── Claude Code seat ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_claude_streams_deltas_in_hud_sized_chunks(tmp_path: Path, record_dir: Path) -> None:
    engine = ClaudeCliReplyEngine(script(tmp_path, "claude", CLAUDE_OK), workdir=tmp_path / "mind")
    chunks = await drain(engine, "tell me a joke")
    assert "".join(chunks) == JOKE
    assert len(chunks[0]) == 8 and all(len(c) <= 64 for c in chunks)
    assert (record_dir / "cwd").read_text().strip().endswith("mind"), "spawned in the mind's own dir, not the repo"


@pytest.mark.asyncio
async def test_claude_argv_carries_persona_no_builtin_tools_and_the_mcp_door(
    tmp_path: Path, record_dir: Path,
) -> None:
    grounding = Grounding(sources=[static_fact("The wearer's name is Sam")], unknowns=["their calendar"])
    engine = ClaudeCliReplyEngine(
        script(tmp_path, "claude", CLAUDE_OK), grounding=grounding,
        model=lambda: "opus", mcp=McpDoor("http://127.0.0.1:8788/mcp", "tok"), workdir=tmp_path,
    )
    await drain(engine, "hi")
    argv = argv_of(record_dir)
    assert argv[0] == "-p" and argv[-1] == "hi", "the prompt is the last argument"
    assert "--bare" not in argv, "bare mode ignores the subscription login"
    assert "--include-partial-messages" in argv
    tools_at = argv.index("--tools")
    assert argv[tools_at + 1] == "" and argv[tools_at + 2].startswith("--"), "no shell on the wearer's Mac"
    system = argv[argv.index("--system-prompt") + 1]
    assert system.startswith("You are JOAT") and "The wearer's name is Sam" in system
    assert "their calendar" in system
    assert argv[argv.index("--model") + 1] == "opus"
    config = json.loads(argv[argv.index("--mcp-config") + 1])
    assert config["mcpServers"]["joat"]["url"] == "http://127.0.0.1:8788/mcp"
    assert config["mcpServers"]["joat"]["headers"]["Authorization"] == "Bearer tok"
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--allowedTools") + 1] == "mcp__joat__*"


@pytest.mark.asyncio
async def test_claude_is_always_strict_about_mcp_even_without_a_door(
    tmp_path: Path, record_dir: Path,
) -> None:
    engine = ClaudeCliReplyEngine(script(tmp_path, "claude", CLAUDE_OK), workdir=tmp_path)
    await drain(engine, "hi")
    argv = argv_of(record_dir)
    assert "--strict-mcp-config" in argv and "--allowedTools" not in argv
    assert json.loads(argv[argv.index("--mcp-config") + 1]) == {"mcpServers": {}}


@pytest.mark.asyncio
async def test_claude_first_turn_opens_a_session_and_the_next_resumes_it(
    tmp_path: Path, record_dir: Path,
) -> None:
    engine = ClaudeCliReplyEngine(script(tmp_path, "claude", CLAUDE_OK), workdir=tmp_path)
    await drain(engine, "one")
    first = argv_of(record_dir)
    session = first[first.index("--session-id") + 1]
    await drain(engine, "two")
    second = argv_of(record_dir)
    assert "--session-id" not in second
    assert second[second.index("--resume") + 1] == session


@pytest.mark.asyncio
async def test_claude_keys_and_proxy_never_reach_the_binary(tmp_path: Path, record_dir: Path) -> None:
    engine = ClaudeCliReplyEngine(script(tmp_path, "claude", CLAUDE_OK), workdir=tmp_path)
    await drain(engine, "hi")
    assert (record_dir / "env").read_text() == "ABSENT|ABSENT|ABSENT"


@pytest.mark.asyncio
async def test_claude_signed_out_is_reported_once_and_not_retried(tmp_path: Path, record_dir: Path) -> None:
    states: list[tuple[str, str]] = []
    engine = ClaudeCliReplyEngine(
        script(tmp_path, "claude", CLAUDE_SIGNED_OUT), workdir=tmp_path,
        on_account=lambda seat, state: states.append((seat, state)),
    )
    assert await engine.warm() is False
    assert states == [("claude", "logged_out")]
    assert engine.available is False
    notice = SIGNED_OUT_NOTICE.format(name="Claude", command="claude auth login")
    assert await drain(engine, "hi") == [notice]


@pytest.mark.asyncio
async def test_claude_usage_limit_becomes_a_lens_notice(tmp_path: Path, record_dir: Path) -> None:
    engine = ClaudeCliReplyEngine(script(tmp_path, "claude", CLAUDE_LIMIT), workdir=tmp_path)
    assert await drain(engine, "hi") == [LIMIT_NOTICE.format(name="Claude")]


@pytest.mark.asyncio
async def test_claude_warm_reads_the_auth_status_json(tmp_path: Path, record_dir: Path) -> None:
    states: list[tuple[str, str]] = []
    engine = ClaudeCliReplyEngine(
        script(tmp_path, "claude", CLAUDE_OK), workdir=tmp_path,
        on_account=lambda seat, state: states.append((seat, state)),
    )
    assert await engine.warm() is True
    assert states == [("claude", "logged_in")] and engine.available
    assert argv_of(record_dir) == ["auth", "status"]


@pytest.mark.asyncio
async def test_a_missing_binary_says_so_or_uses_the_fallback(tmp_path: Path) -> None:
    engine = ClaudeCliReplyEngine(None, workdir=tmp_path)
    assert await drain(engine, "hi") == [MISSING_NOTICE.format(name="Claude")]
    local = LocalEngine("offline")
    engine = ClaudeCliReplyEngine(None, workdir=tmp_path, fallback=local)
    assert await drain(engine, "hi") == ["offline"] and local.asked == ["hi"]


# ── Codex seat ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_codex_answers_from_the_agent_message_and_resumes_its_thread(
    tmp_path: Path, record_dir: Path,
) -> None:
    grounding = Grounding(sources=[static_fact("Battery 71%")])
    engine = CodexCliReplyEngine(
        script(tmp_path, "codex", CODEX_OK), grounding=grounding, model=lambda: "gpt-5.6-terra",
        mcp=McpDoor("http://127.0.0.1:8788/mcp", "tok"), workdir=tmp_path,
    )
    chunks = await drain(engine, "tell me a joke")
    assert chunks == ["Why did the developer go broke? He used up all his cache."]
    argv = argv_of(record_dir)
    assert argv[:2] == ["exec", "--json"] and "--ignore-user-config" in argv
    # Measured, not assumed: exec refuses an MCP tool call without this, and
    # plugin-sourced servers keep dialling OAuth without the features flag.
    assert 'mcp_servers.joat.default_tools_approval_mode="approve"' in argv
    assert "features.plugins=false" in argv
    assert argv[argv.index("-m") + 1] == "gpt-5.6-terra"
    assert argv.index("-m") > argv.index("--skip-git-repo-check")
    assert 'mcp_servers.joat.url="http://127.0.0.1:8788/mcp"' in argv
    assert 'mcp_servers.joat.bearer_token_env_var="JOAT_AGENT_TOKEN"' in argv
    prompt = argv[-1]
    assert prompt.startswith("SYSTEM INSTRUCTIONS") and "Battery 71%" in prompt
    assert prompt.endswith("WEARER: tell me a joke")
    assert (record_dir / "env").read_text() == "ABSENT|tok", "key stripped, MCP token delivered"

    await drain(engine, "another")
    argv = argv_of(record_dir)
    at = argv.index("resume")
    assert argv[at + 1] == "01a06984-cdcb-7321-a21b-c6b2242ce193"
    assert at > argv.index("--skip-git-repo-check"), "codex exec resume rejects -s/--json after the subcommand"
    assert argv.index("-m") > at, "resume accepts -m and the prompt after it"
    assert not argv[-1].startswith("SYSTEM INSTRUCTIONS"), "the persona rides only the first turn"


@pytest.mark.asyncio
async def test_codex_signed_out_is_a_notice(tmp_path: Path, record_dir: Path) -> None:
    engine = CodexCliReplyEngine(script(tmp_path, "codex", CODEX_SIGNED_OUT), workdir=tmp_path)
    assert await engine.warm() is False
    assert await drain(engine, "hi") == [SIGNED_OUT_NOTICE.format(name="Codex", command="codex login")]


@pytest.mark.asyncio
async def test_codex_warm_reads_login_status(tmp_path: Path, record_dir: Path) -> None:
    engine = CodexCliReplyEngine(script(tmp_path, "codex", CODEX_OK), workdir=tmp_path)
    assert await engine.warm() is True and engine.account == "logged_in"
    assert argv_of(record_dir) == ["login", "status"]


# ── the seat switch ──────────────────────────────────────────────────────────

FINAL_CODEX_REPLY = "The complete answer is ready."

CODEX_COMPLETION_GATES = r'''
import json
import os
from pathlib import Path
import sys
import time

directory = Path(os.environ["RECORD_DIR"])
mode = (directory / "mode").read_text()
(directory / "pid").write_text(str(os.getpid()))
(directory / "argv").write_text("\0".join(sys.argv[1:]) + "\0")

def emit(event):
    print(json.dumps(event, ensure_ascii=True), flush=True)

def wait_for_gate(name):
    deadline = time.monotonic() + 8
    while not (directory / name).exists():
        if time.monotonic() >= deadline:
            sys.exit(9)
        time.sleep(0.005)

emit({"type": "thread.started", "thread_id": "01a06984-cdcb-7321-a21b-c6b2242ce193"})
emit({"type": "turn.started"})
emit({"type": "item.completed", "item": {"id": "reason", "type": "reasoning", "text": "Hidden reasoning"}})
emit({"type": "item.completed", "item": {"id": "warning", "type": "error", "message": "Nonfatal warning"}})
emit({"type": "item.completed", "item": {"id": "interim", "type": "agent_message", "text": "Interim commentary must not be the final reply."}})
emit({"type": "item.updated", "item": {"id": "final", "type": "agent_message", "text": "Partial answer"}})
answer = {"type": "item.completed", "item": {"id": "final", "type": "agent_message", "text": "The complete answer is ready."}}

if mode == "unterminated-item":
    sys.stdout.write(json.dumps(answer))
    sys.stdout.flush()
    sys.exit(0)
if mode == "fragmented":
    encoded = json.dumps(answer)
    split = len(encoded) // 2
    sys.stdout.write(encoded[:split])
    sys.stdout.flush()
    (directory / "fragment.ready").write_text("ready")
    wait_for_gate("release-fragment")
    sys.stdout.write(encoded[split:] + "\n")
    sys.stdout.flush()
else:
    emit(answer)
(directory / "item.ready").write_text("ready")

if mode == "wait-turn":
    wait_for_gate("release-turn")
if mode in {"turn-failed", "stream-error"}:
    if mode == "turn-failed":
        emit({"type": "turn.failed", "error": {"message": "Synthetic fatal failure"}})
    else:
        emit({"type": "error", "message": "Synthetic fatal failure"})
    sys.exit(0)

completed = {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}
(directory / "completed.at").write_text(str(time.monotonic()))
if mode == "unterminated-completion":
    sys.stdout.write(json.dumps(completed))
    sys.stdout.flush()
    sys.exit(0)
emit(completed)
if mode == "duplicate-completion":
    emit(answer)
    emit(completed)
wait_for_gate("release-eof")
(directory / "exiting.at").write_text(str(time.monotonic()))
sys.exit(7 if mode == "late-nonzero" else 0)
'''


class CompletionTransport:
    """Record the real HudWriter send boundary, not a physical paint receipt."""

    def __init__(self) -> None:
        self.sent: list[AssistantOverlay] = []
        self.first_reply_at: float | None = None
        self.first_reply = asyncio.Event()

    async def send(self, message: object) -> None:
        if isinstance(message, AssistantOverlay) and message.text:
            self.sent.append(message)
            if self.first_reply_at is None:
                self.first_reply_at = time.monotonic()
                self.first_reply.set()


def completion_engine(
    tmp_path: Path, record_dir: Path, *, mode: str = "held-eof",
    fallback: ReplyEngine | None = None, timeout_s: float = 5,
) -> CodexCliReplyEngine:
    (record_dir / "mode").write_text(mode)
    binary = script(tmp_path, "controlled-codex", f"#!{sys.executable}\n{CODEX_COMPLETION_GATES}")
    return CodexCliReplyEngine(binary, workdir=tmp_path, fallback=fallback, timeout_s=timeout_s)


async def wait_fixture_path(path: Path, timeout_s: float = 2) -> None:
    async def wait() -> None:
        while not path.exists():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait(), timeout_s)


def fixture_child_alive(record_dir: Path) -> bool:
    pid_file = record_dir / "pid"
    if not pid_file.exists():
        return False
    try:
        os.kill(int(pid_file.read_text()), 0)
    except ProcessLookupError:
        return False
    return True


async def wait_fixture_child_stopped(record_dir: Path) -> None:
    async def wait() -> None:
        while fixture_child_alive(record_dir):
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait(), 3)


async def pump_completion_reply(
    engine: CodexCliReplyEngine, transport: CompletionTransport, chunks: list[str],
) -> None:
    hud = HudWriter(transport)  # type: ignore[arg-type]
    async for chunk in engine.stream("A synthetic question"):
        await hud.assistant(chunk, append=bool(chunks))
        chunks.append(chunk)


async def release_completion_fixture(record_dir: Path, task: asyncio.Task[None]) -> None:
    for name in ("release-turn", "release-fragment", "release-eof"):
        (record_dir / name).write_text("released")
    if not task.done():
        task.cancel()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
    await wait_fixture_child_stopped(record_dir)


@pytest.mark.asyncio
async def test_codex_completed_turn_reaches_hud_before_held_eof(
    tmp_path: Path, record_dir: Path, record_property: Any,
) -> None:
    engine = completion_engine(tmp_path, record_dir)
    transport, chunks = CompletionTransport(), []
    task = asyncio.create_task(pump_completion_reply(engine, transport, chunks))
    try:
        await wait_fixture_path(record_dir / "completed.at")
        await asyncio.wait_for(transport.first_reply.wait(), 0.5)
        assert transport.sent[0].text == FINAL_CODEX_REPLY
        assert fixture_child_alive(record_dir)
        assert not task.done(), "reply delivery must not abandon child ownership"
        assert not (record_dir / "exiting.at").exists()
        assert transport.first_reply_at is not None
        completed_at = float((record_dir / "completed.at").read_text())
        latency_ms = (transport.first_reply_at - completed_at) * 1000
        assert latency_ms >= 0
        record_property("completed_event_to_first_hud_send_ms", latency_ms)
        record_property("first_hud_send_before_eof_release", True)
        (record_dir / "release-eof").write_text("released")
        await asyncio.wait_for(task, 3)
        assert chunks == [FINAL_CODEX_REPLY]
        assert engine.account == "logged_in"
        assert transport.first_reply_at < float((record_dir / "exiting.at").read_text())
        # Successful cleanup preserves the CLI-owned conversation on the next turn.
        assert await drain(engine, "Continue the synthetic conversation") == [FINAL_CODEX_REPLY]
        argv = argv_of(record_dir)
        assert argv[argv.index("resume") + 1] == "01a06984-cdcb-7321-a21b-c6b2242ce193"
    finally:
        await release_completion_fixture(record_dir, task)


@pytest.mark.asyncio
@pytest.mark.parametrize(("mode", "ready", "release"), [
    ("wait-turn", "item.ready", "release-turn"),
    ("fragmented", "fragment.ready", "release-fragment"),
])
async def test_codex_completed_item_waits_for_complete_turn_boundary(
    tmp_path: Path, record_dir: Path, mode: str, ready: str, release: str,
) -> None:
    engine = completion_engine(tmp_path, record_dir, mode=mode)
    transport, chunks = CompletionTransport(), []
    task = asyncio.create_task(pump_completion_reply(engine, transport, chunks))
    try:
        await wait_fixture_path(record_dir / ready)
        await asyncio.sleep(0.03)
        assert transport.sent == [], "neither interim items nor incomplete JSON are a final answer"
        (record_dir / release).write_text("released")
        await wait_fixture_path(record_dir / "completed.at")
        await asyncio.wait_for(transport.first_reply.wait(), 0.5)
        assert transport.sent[0].text == FINAL_CODEX_REPLY
        (record_dir / "release-eof").write_text("released")
        await asyncio.wait_for(task, 3)
        assert chunks == [FINAL_CODEX_REPLY]
    finally:
        await release_completion_fixture(record_dir, task)


@pytest.mark.asyncio
async def test_codex_duplicate_completion_does_not_repeat_delivered_reply(
    tmp_path: Path, record_dir: Path,
) -> None:
    engine = completion_engine(tmp_path, record_dir, mode="duplicate-completion")
    transport, chunks = CompletionTransport(), []
    task = asyncio.create_task(pump_completion_reply(engine, transport, chunks))
    try:
        await wait_fixture_path(record_dir / "completed.at")
        await asyncio.wait_for(transport.first_reply.wait(), 0.5)
        (record_dir / "release-eof").write_text("released")
        await asyncio.wait_for(task, 3)
        assert chunks == [FINAL_CODEX_REPLY]
    finally:
        await release_completion_fixture(record_dir, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["unterminated-item", "unterminated-completion"])
async def test_codex_clean_eof_preserves_last_unterminated_event(
    tmp_path: Path, record_dir: Path, mode: str,
) -> None:
    engine = completion_engine(tmp_path, record_dir, mode=mode)
    assert await asyncio.wait_for(drain(engine, "Synthetic question"), 3) == [FINAL_CODEX_REPLY]
    assert engine.account == "logged_in"
    await wait_fixture_child_stopped(record_dir)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["turn-failed", "stream-error"])
async def test_codex_fatal_stream_event_cannot_publish_candidate_answer(
    tmp_path: Path, record_dir: Path, mode: str,
) -> None:
    fallback = LocalEngine("Fallback answer")
    engine = completion_engine(tmp_path, record_dir, mode=mode, fallback=fallback)
    assert await asyncio.wait_for(drain(engine, "Synthetic question"), 3) == ["Fallback answer"]
    assert fallback.asked == ["Synthetic question"]
    assert engine.account != "logged_in"
    await wait_fixture_child_stopped(record_dir)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["late-nonzero", "held-eof"])
async def test_codex_failure_after_delivery_never_runs_fallback_or_resumes_failed_turn(
    tmp_path: Path, record_dir: Path, mode: str,
) -> None:
    fallback = LocalEngine("Fallback would repeat the turn")
    engine = completion_engine(tmp_path, record_dir, mode=mode, fallback=fallback,
                               timeout_s=0.8 if mode == "held-eof" else 5)
    transport, chunks = CompletionTransport(), []
    task = asyncio.create_task(pump_completion_reply(engine, transport, chunks))
    try:
        await wait_fixture_path(record_dir / "completed.at")
        await asyncio.wait_for(transport.first_reply.wait(), 0.4)
        assert transport.sent[0].text == FINAL_CODEX_REPLY
        if mode == "late-nonzero":
            (record_dir / "release-eof").write_text("released")
        await asyncio.wait_for(task, 3)
        assert chunks.count(FINAL_CODEX_REPLY) == 1
        assert fallback.asked == []
        assert engine.account != "logged_in"
        await wait_fixture_child_stopped(record_dir)
        # A late process failure must not make a new question resume an
        # incompletely finalized turn, even though its answer was visible.
        (record_dir / "mode").write_text("held-eof")
        (record_dir / "release-eof").write_text("released")
        assert await asyncio.wait_for(drain(engine, "Next question"), 3) == [FINAL_CODEX_REPLY]
        assert "resume" not in argv_of(record_dir)
    finally:
        await release_completion_fixture(record_dir, task)


@pytest.mark.asyncio
async def test_codex_cancelling_after_first_hud_send_reaps_owned_child(
    tmp_path: Path, record_dir: Path,
) -> None:
    fallback = LocalEngine("Fallback must not run")
    engine = completion_engine(tmp_path, record_dir, fallback=fallback)
    transport, chunks = CompletionTransport(), []
    task = asyncio.create_task(pump_completion_reply(engine, transport, chunks))
    try:
        await wait_fixture_path(record_dir / "completed.at")
        await asyncio.wait_for(transport.first_reply.wait(), 0.5)
        assert fixture_child_alive(record_dir)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        await wait_fixture_child_stopped(record_dir)
        assert fallback.asked == []
        assert chunks == [FINAL_CODEX_REPLY]
    finally:
        await release_completion_fixture(record_dir, task)


@pytest.mark.asyncio
async def test_codex_closing_stream_after_first_yield_reaps_owned_child(
    tmp_path: Path, record_dir: Path,
) -> None:
    fallback = LocalEngine("Fallback must not run")
    engine = completion_engine(tmp_path, record_dir, fallback=fallback)
    stream = engine.stream("Synthetic question")
    try:
        assert await asyncio.wait_for(anext(stream), 1) == FINAL_CODEX_REPLY
        assert fixture_child_alive(record_dir)
        await asyncio.wait_for(stream.aclose(), 3)
        await wait_fixture_child_stopped(record_dir)
        assert fallback.asked == []
    finally:
        (record_dir / "release-eof").write_text("released")
        await asyncio.wait_for(stream.aclose(), 3)
        await wait_fixture_child_stopped(record_dir)


@pytest.mark.asyncio
async def test_the_switch_honours_the_spoken_seat_and_skips_a_signed_out_one() -> None:
    claude = LocalEngine("from claude", available=False)
    codex = LocalEngine("from codex")
    local = LocalEngine("from mlx")
    chosen = ["claude"]
    picks: list[str] = []
    switch = SeatSwitch({"claude": claude, "codex": codex, "local": local},
                        chosen=lambda: chosen[0], on_pick=picks.append)
    assert await drain(switch, "hi") == ["from codex"]
    chosen[0] = "local"
    assert await drain(switch, "hi") == ["from mlx"]
    claude.available = True
    chosen[0] = "claude"
    assert await drain(switch, "hi") == ["from claude"]
    assert picks == ["codex", "local", "claude"]


@pytest.mark.asyncio
async def test_the_switch_says_so_when_no_seat_can_speak() -> None:
    switch = SeatSwitch({"claude": LocalEngine("x", available=False)}, chosen=lambda: "claude")
    assert await drain(switch, "hi") == [NO_SEAT_NOTICE]
    with pytest.raises(ValueError):
        SeatSwitch({}, chosen=lambda: None)


def test_failed_notice_is_generic_text() -> None:
    assert "again" in FAILED_NOTICE


class TestCodexDiscovery:
    """A brain started by launchd has a minimal PATH; `codex` ships inside the
    ChatGPT app bundle with a user-level symlink. PATH-only discovery read a
    working CLI as "codex CLI missing" on the glass."""

    def test_falls_back_to_the_user_symlink_when_not_on_path(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from joat.agents import cli_reply_engine

        monkeypatch.setattr(cli_reply_engine.shutil, "which", lambda _name: None)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert cli_reply_engine.find_codex_binary() is None
        binary = tmp_path / ".local" / "bin" / "codex"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        assert cli_reply_engine.find_codex_binary() == str(binary)

    def test_path_still_wins(self, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from joat.agents import cli_reply_engine

        monkeypatch.setattr(cli_reply_engine.shutil, "which", lambda _name: "/usr/bin/codex")
        assert cli_reply_engine.find_codex_binary() == "/usr/bin/codex"
