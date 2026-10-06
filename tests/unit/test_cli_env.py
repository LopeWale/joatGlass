"""Every subscription CLI launch drops the same credential list.

Until 2026-10-06 each launch path kept a list of its own: the assistant
seats and the sign-in dropped a bearer token and a base URL, while the
Brainstorm critic dropped only ANTHROPIC_API_KEY and the QuickNote completer
two names. An inherited token, endpoint or provider switch therefore moved
those turns off the wearer's subscription. Nothing dropped CODEX_API_KEY,
which `codex exec` takes over the ChatGPT login.

The proofs are real spawns: the "binary" is a script that writes the
environment it actually received, so each assertion is about the child.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from joat.agents import subprocess_agent
from joat.agents.claude_completer import ClaudeCompleter
from joat.agents.cli_accounts import AccountRegistry
from joat.agents.cli_env import STRIP_ENV, cli_env
from joat.agents.cli_signin import CliSignIn
from joat.agents.subprocess_agent import SubprocessAgent, critic, proposer

_PROBE = '#!/bin/sh\nenv > "$RECORD_FILE"\necho ok\n'
_LOGINS = ("CLAUDE_CODE_OAUTH_TOKEN", "CODEX_ACCESS_TOKEN")


@pytest.fixture
def probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A CLI stand-in, and every listed credential set in the brain's env."""
    binary = tmp_path / "cli"
    binary.write_text(_PROBE)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("RECORD_FILE", str(tmp_path / "env"))
    monkeypatch.setenv("JOAT_CANARY", "arrived")
    for name in (*STRIP_ENV, *_LOGINS):
        monkeypatch.setenv(name, f"LEAKED-{name}")
    return binary


def received(probe: Path) -> dict[str, str]:
    lines = (probe.parent / "env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def assert_dropped(env: dict[str, str]) -> None:
    leaked = sorted(name for name in STRIP_ENV if name in env)
    assert leaked == [], f"reached the CLI: {leaked}"
    assert env.get("JOAT_CANARY") == "arrived", "the rest of the environment must arrive"
    assert env.get("OTEL_SDK_DISABLED") == "true"


async def drain(agent: SubprocessAgent) -> None:
    async for _ in agent.stream("p"):
        pass


def test_the_list_covers_every_way_off_the_subscription() -> None:
    # Claude Code's documented precedence ranks these above a /login.
    for name in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                 "CLAUDE_CODE_USE_ANTHROPIC_AWS", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY",
                 "ANTHROPIC_PROFILE", "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID",
                 "ANTHROPIC_BASE_URL", "CODEX_API_KEY", "OPENAI_API_KEY"):
        assert name in STRIP_ENV
    # The Mac's own subscription login stays: the default account runs on it.
    for name in _LOGINS:
        assert name not in STRIP_ENV


def test_cli_env_drops_the_list_and_any_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*STRIP_ENV, "EXTRA_SECRET"):
        monkeypatch.setenv(name, "LEAKED")
    monkeypatch.setenv("JOAT_CANARY", "arrived")
    env = cli_env(["EXTRA_SECRET"])
    assert not set(STRIP_ENV) & set(env)
    assert "EXTRA_SECRET" not in env
    assert env["JOAT_CANARY"] == "arrived"
    assert env["OTEL_SDK_DISABLED"] == "true"
    assert os.environ["ANTHROPIC_API_KEY"] == "LEAKED", "the brain's own environment is untouched"


async def test_the_brainstorm_critic_drops_the_whole_list(
    probe: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subprocess_agent, "find_claude_binary", lambda: str(probe))
    agent = critic()
    assert agent is not None
    await drain(agent)
    assert_dropped(received(probe))


async def test_the_brainstorm_proposer_drops_the_whole_list(
    probe: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subprocess_agent, "find_codex_binary", lambda: str(probe))
    agent = proposer(model=lambda: None)
    assert agent is not None
    await drain(agent)
    assert_dropped(received(probe))


async def test_a_seat_can_add_names_but_never_let_one_through(probe: Path) -> None:
    # The assistant seats build their runner without a list of their own;
    # one that passes its own list still drops the shared one.
    await drain(SubprocessAgent([str(probe)], env_strip=("JOAT_EXTRA",)))
    assert_dropped(received(probe))
    await drain(SubprocessAgent([str(probe)]))
    assert_dropped(received(probe))


async def test_the_quicknote_completer_drops_the_whole_list(probe: Path) -> None:
    result = await ClaudeCompleter(str(probe), timeout_s=30.0).complete(system="s", text="t")
    assert result == "ok"
    assert_dropped(received(probe))


async def test_the_sign_in_drops_the_list_and_any_inherited_login(
    probe: Path, tmp_path: Path
) -> None:
    registry = AccountRegistry(tmp_path / "accounts")
    signin = CliSignIn(registry, claude_binary=lambda: str(probe), codex_binary=lambda: str(probe))
    for provider in ("claude", "codex"):
        await signin.status(registry.get(provider, "default"))
        env = received(probe)
        assert_dropped(env)
        for name in _LOGINS:
            assert name not in env, f"a sign-in must not be short-circuited by {name}"
        # The CLI opens nothing on the Mac: the phone's link is the only one.
        assert env.get("BROWSER") == "true"
