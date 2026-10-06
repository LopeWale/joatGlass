"""CliSignIn against a scripted `claude` in a real pseudo-terminal.

The double prints what Claude Code 2.1.287 printed on this Mac (the
authorize link, then the paste prompt), reads the pasted code from its
terminal, and — like any login prompt — asks again when the code is wrong.
"""

from __future__ import annotations

import asyncio
import stat
import sys
from pathlib import Path

import pytest

from joat.agents.cli_accounts import AccountProfile, AccountRegistry
from joat.agents.cli_signin import CLAUDE_STEPS, CliSignIn, LoginSnapshot

FAKE_CLAUDE = f"""#!{sys.executable}
import sys
print("If the browser didn't open, visit: https://claude.ai/oauth/authorize?code=true&state=abc", flush=True)
while True:
    print("Paste code here if prompted > ", end="", flush=True)
    code = sys.stdin.readline().strip()
    if code == "GOOD-CODE":
        print("Login successful.", flush=True)
        sys.exit(0)
    print("Invalid code. Please try again.", flush=True)
"""

SILENT_AFTER_CODE = f"""#!{sys.executable}
import sys, time
print("If the browser didn't open, visit: https://claude.ai/oauth/authorize?code=true&state=abc", flush=True)
print("Paste code here if prompted > ", end="", flush=True)
sys.stdin.readline()
time.sleep(60)
"""


def _exe(path: Path, body: str) -> str:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


async def _until(signin: CliSignIn, stage: str, timeout: float = 10.0) -> LoginSnapshot:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        current = signin.current
        if current is not None and current.stage == stage:
            return current
        await asyncio.sleep(0.05)
    raise AssertionError(f"never reached {stage}: {signin.current}")


@pytest.mark.asyncio
async def test_a_wrong_code_asks_again_instead_of_checking_forever(tmp_path: Path) -> None:
    """2026-10-06: after a paste the phone stayed on "Checking the code…"
    until the 15-minute deadline, whatever the CLI said back."""
    registry = AccountRegistry(tmp_path / "accounts")
    signed_in: list[AccountProfile] = []
    signin = CliSignIn(
        registry, on_signed_in=signed_in.append,
        claude_binary=lambda: _exe(tmp_path / "claude", FAKE_CLAUDE),
        codex_binary=lambda: None,
    )
    signin.start(registry.active("claude"))
    first = await _until(signin, "enter_code")
    assert first.needs_code and first.url is not None
    assert first.message == CLAUDE_STEPS and "Authorize" in first.message

    assert signin.submit_code("WRONG")
    again = await _until(signin, "enter_code")
    assert again.needs_code and "didn't work" in again.message
    assert again.url == first.url, "the link stays on the phone"

    assert signin.submit_code("GOOD-CODE")
    done = await _until(signin, "done")
    assert done.url is None and done.code is None
    assert [p.profile_id for p in signed_in] == ["default"]


@pytest.mark.asyncio
async def test_a_code_the_cli_never_answers_fails_in_bounded_time(tmp_path: Path) -> None:
    registry = AccountRegistry(tmp_path / "accounts")
    signin = CliSignIn(
        registry, claude_binary=lambda: _exe(tmp_path / "claude", SILENT_AFTER_CODE),
        codex_binary=lambda: None,
    )
    signin._code_check_timeout_s = 1.0
    signin.start(registry.active("claude"))
    await _until(signin, "enter_code")
    assert signin.submit_code("SOME-CODE")
    failed = await _until(signin, "failed", timeout=8.0)
    assert "wasn't accepted" in failed.message


@pytest.mark.asyncio
async def test_a_failing_sign_in_observer_does_not_lose_the_result(tmp_path: Path) -> None:
    registry = AccountRegistry(tmp_path / "accounts")

    def boom(_profile: AccountProfile) -> None:
        raise RuntimeError("observer broke")

    signin = CliSignIn(
        registry, on_signed_in=boom,
        claude_binary=lambda: _exe(tmp_path / "claude", FAKE_CLAUDE),
        codex_binary=lambda: None,
    )
    signin.start(registry.active("claude"))
    await _until(signin, "enter_code")
    assert signin.submit_code("GOOD-CODE")
    assert (await _until(signin, "done")).message == "Signed in."
