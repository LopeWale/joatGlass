"""Debate agents as subprocess wrappers (MAC_APP.md §DebateLoop).

ProposerAgent shells out to the official `codex` binary, CriticAgent to the
official `claude` binary (RULES.md #9: never third-party clients, never
reused OAuth tokens). Both stream stdout incrementally so the wearer SEES
the debate happen — the Phase-2 requirement is visibility, not verdicts.

Safety parity with .claude/bin/codex-bridge.sh: OPENAI_API_KEY is stripped
from the child env so Codex always uses subscription auth, and the sandbox
is read-only.
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import re
import shutil
from collections.abc import AsyncIterator

log = logging.getLogger(__name__)

# codex exec banner/noise lines that mean nothing on a 576x288 HUD.
_CODEX_NOISE = re.compile(
    r"^(workdir|model|provider|approval|sandbox|reasoning|session id|tokens used)\b"
    r"|^-{5,}$|^codex$|^user$|^Reading additional input"
)


def find_claude_binary() -> str | None:
    """PATH first; else the newest VS Code extension bundle (version-pinned
    paths churn on extension updates, so resolve at runtime, never hardcode)."""
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    bundles = sorted(
        glob.glob(
            os.path.expanduser(
                "~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude"
            )
        )
    )
    return bundles[-1] if bundles else None


class SubprocessAgent:
    """Run a CLI agent; yield its stdout in chunks as they arrive."""

    def __init__(self, command: list[str], env_strip: tuple[str, ...] = (),
                 timeout_s: float = 240.0, noise: re.Pattern[str] | None = None) -> None:
        self._command = command
        self._env_strip = env_strip
        self._timeout_s = timeout_s
        self._noise = noise

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        env = {k: v for k, v in os.environ.items() if k not in self._env_strip}
        env["OTEL_SDK_DISABLED"] = "true"
        proc = await asyncio.create_subprocess_exec(
            *self._command, prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
            env=env,
        )
        assert proc.stdout is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout_s
        pending = ""
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    log.warning("agent %s timed out", self._command[0])
                    break
                try:
                    raw = await asyncio.wait_for(proc.stdout.read(512), remaining)
                except asyncio.TimeoutError:
                    log.warning("agent %s timed out", self._command[0])
                    break
                if not raw:
                    break
                pending += raw.decode(errors="replace")
                # Emit complete lines (noise-filtered); keep the tail fragment.
                *lines, pending = pending.split("\n")
                out = "\n".join(ln for ln in lines if not self._is_noise(ln))
                if out.strip():
                    yield out + "\n"
            if pending.strip() and not self._is_noise(pending):
                yield pending
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()

    def _is_noise(self, line: str) -> bool:
        return bool(self._noise and self._noise.match(line.strip()))


def proposer(model: str | None = None) -> SubprocessAgent | None:
    """Codex proposes. Read-only sandbox; model pinned the same way the
    collab bridge pins it (spark until terra credits reset)."""
    binary = shutil.which("codex")
    if binary is None:
        return None
    codex_model = model or os.environ.get("CODEX_MODEL", "gpt-5.3-codex-spark")
    return SubprocessAgent(
        [binary, "exec", "-m", codex_model, "-s", "read-only"],
        env_strip=("OPENAI_API_KEY",),
        noise=_CODEX_NOISE,
    )


def critic() -> SubprocessAgent | None:
    """Claude critiques, via the official binary only (RULES.md #9).

    ANTHROPIC_API_KEY is stripped so the critic always runs on the
    subscription login — RULES #9 makes metered API an explicit config
    choice (AuthStrategy, with --max-budget-usd), never an accident of
    whatever the shell happened to export."""
    binary = find_claude_binary()
    if binary is None:
        return None
    return SubprocessAgent([binary, "-p"], env_strip=("ANTHROPIC_API_KEY",))
