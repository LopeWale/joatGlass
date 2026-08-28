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
from collections.abc import AsyncIterator, Callable

from ..services.roundtable import failure_notice

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
                 timeout_s: float = 240.0, noise: re.Pattern[str] | None = None,
                 name: str | None = None,
                 model: Callable[[], str | None] | None = None,
                 model_flag: str = "-m") -> None:
        self._command = command
        self._name = name or os.path.basename(command[0])
        self._env_strip = env_strip
        self._timeout_s = timeout_s
        self._noise = noise
        # Read PER CALL, not at construction. Boot-time capture is what made
        # the model a launch flag: the wearer could not change a seat without
        # restarting the engine. A provider returning None passes no flag and
        # leaves the tool on its own default.
        self._model = model
        self._model_flag = model_flag

    def _argv(self, prompt: str) -> list[str]:
        chosen = self._model() if self._model is not None else None
        flag = [self._model_flag, chosen] if chosen else []
        return [*self._command, *flag, prompt]

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        env = {k: v for k, v in os.environ.items() if k not in self._env_strip}
        env["OTEL_SDK_DISABLED"] = "true"
        proc = await asyncio.create_subprocess_exec(
            *self._argv(prompt),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
            env=env,
        )
        assert proc.stdout is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout_s
        pending = ""
        timed_out = False
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    raw = await asyncio.wait_for(proc.stdout.read(512), remaining)
                except asyncio.TimeoutError:
                    timed_out = True
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
            # How the turn ended, in the wearer's view. The exit code is the
            # ONLY trustworthy success signal: a failing CLI prints its
            # complaint on stdout, indistinguishable from an answer
            # (`claude -p` exits 1 and says "You're out of usage credits").
            # A hung child is NEVER waited on here — the finally clause kills
            # it — because a stalled collaborator must cost the debate one
            # seat, not the whole round.
            if timed_out:
                log.warning("agent %s timed out after %.0fs", self._name, self._timeout_s)
                yield "\n" + failure_notice(
                    self._name, f"no answer within {self._timeout_s:.0f}s"
                ) + "\n"
            else:
                # stdout is at EOF, so reaping is normally instant — but a
                # child that closes stdout and lingers must not become an
                # unbounded wait, so it shares the turn's remaining budget.
                try:
                    await asyncio.wait_for(
                        proc.wait(), max(0.5, deadline - loop.time())
                    )
                except asyncio.TimeoutError:
                    pass
                if proc.returncode:
                    log.warning("agent %s exited %s", self._name, proc.returncode)
                    yield "\n" + failure_notice(
                        self._name, f"exited {proc.returncode} — see the log"
                    ) + "\n"
        finally:
            if proc.returncode is None:
                proc.kill()
            # Bounded, because asyncio completes wait() only once the stdout
            # PIPE closes — and a killed agent's own children inherit that
            # pipe and keep it open. `codex exec` spawns children, so an
            # unbounded wait here stalls the debate for the lifetime of a
            # grandchild we already gave up on. Measured: 30s for a killed
            # `sh -c "sleep 30"`. Reaping is the OS's problem; the round
            # table's problem is the next speaker.
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()), 2.0)
            except asyncio.TimeoutError:
                log.warning("agent %s killed but not reaped within 2s "
                            "(a child is holding its stdout)", self._name)

    def _is_noise(self, line: str) -> bool:
        return bool(self._noise and self._noise.match(line.strip()))


def proposer(model: Callable[[], str | None] | None = None) -> SubprocessAgent | None:
    """Codex proposes, in a read-only sandbox.

    `model` is a late-bound reader (normally ModelRegistry.provider("codex"))
    so a switch takes effect on the next turn. With none supplied it falls
    back to the boot-time env pin, preserving the previous behaviour.
    """
    binary = shutil.which("codex")
    if binary is None:
        return None
    if model is None:
        pinned = os.environ.get("CODEX_MODEL", "gpt-5.3-codex-spark")
        model = lambda: pinned  # noqa: E731 - a constant provider reads clearer here
    return SubprocessAgent(
        [binary, "exec", "-s", "read-only"],
        env_strip=("OPENAI_API_KEY",),
        noise=_CODEX_NOISE,
        name="codex",
        model=model,
        model_flag="-m",
    )


# MCP plumbing chatter claude prints on stdout; not part of any critique.
_CLAUDE_NOISE = re.compile(r"^Client\.\w+\(\) called but server does not advertise")


def critic(model: Callable[[], str | None] | None = None) -> SubprocessAgent | None:
    """Claude critiques, via the official binary only (RULES.md #9).

    ANTHROPIC_API_KEY is stripped so the critic always runs on the
    subscription login — RULES #9 makes metered API an explicit config
    choice (AuthStrategy, with --max-budget-usd), never an accident of
    whatever the shell happened to export."""
    binary = find_claude_binary()
    if binary is None:
        return None
    # `claude --model <alias|full-name>` (verified against --help). No
    # provider, or one returning None, leaves the CLI on its own default.
    return SubprocessAgent([binary, "-p"], env_strip=("ANTHROPIC_API_KEY",),
                           noise=_CLAUDE_NOISE, name="claude",
                           model=model, model_flag="--model")
