"""The subscription seats: real spawns of scripted `claude` and `codex` doubles.

Each double records its argv and the environment it received, and speaks
the exact JSON the real binaries emitted on this Mac (2026-09-03), so the
assertions are about the bytes on the wire and the child's real
environment, not about a dict the test built for itself.
"""

from __future__ import annotations

import json
import stat
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
