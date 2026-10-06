"""Debate agents as subprocess wrappers (MAC_APP.md §DebateLoop).

ProposerAgent shells out to the official `codex` binary, CriticAgent to the
official `claude` binary (RULES.md #9: never third-party clients, never
reused OAuth tokens). Both stream stdout incrementally so the wearer SEES
the debate happen — the Phase-2 requirement is visibility, not verdicts.

Every launch drops the shared credential list (`cli_env.STRIP_ENV`) so both
CLIs always run on the subscription login, and Codex's sandbox is read-only.
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
import shutil
from collections.abc import AsyncIterator, Callable, Mapping

from ..services.models import DEFAULT_CODEX_MODEL
from ..services.roundtable import LIMIT_DETAIL, SIGNED_OUT_DETAIL, failure_notice
from .cli_env import cli_env
from .cli_failures import classify_cli_failure
from .cli_locate import find_codex_binary

EnvProvider = Callable[[], Mapping[str, str | None]]
"""Late-bound environment overlay for one spawn. A value of None REMOVES
the variable, so the default account really is the CLI's default even when
the brain itself was launched with CLAUDE_CONFIG_DIR or CODEX_HOME set."""

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
    # A launchd-started brain has a minimal PATH; the native installer and
    # the package managers put the binary in these fixed places.
    for candidate in ("~/.local/bin/claude", "~/.claude/local/claude",
                      "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        path = os.path.expanduser(candidate)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    bundles = sorted(
        glob.glob(
            os.path.expanduser(
                "~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude"
            )
        )
    )
    return bundles[-1] if bundles else None


def _is_json_line(line: str) -> bool:
    try:
        json.loads(line)
    except (ValueError, UnicodeError):
        return False
    return True


def _complaint(line: str) -> str | None:
    """A stdout line as the CLI's own complaint, or None for answer text.

    Prose is a complaint (an answer in plain mode is prose too, which is why
    only the tail is read). A JSON line is the CLI's structured stream:
    only its error events count — `codex exec --json` reports a failed
    turn as `{"type": "error"|"turn.failed", ...}` or an `error` item,
    while its `agent_message` items carry the answer.
    """
    try:
        event = json.loads(line)
    except (ValueError, UnicodeError):
        return line
    if not isinstance(event, dict):
        return None
    kind = event.get("type")
    item = event.get("item")
    if kind == "item.completed" and isinstance(item, dict) and item.get("type") == "error":
        return str(item.get("message", ""))
    if kind in ("error", "turn.failed"):
        error = event.get("error")
        message = error.get("message") if isinstance(error, dict) else event.get("message")
        return str(message or "")
    return None


def _diagnostic_text(stderr: list[str], stdout_errors: list[str], plain_stdout: list[str],
                     *, timed_out: bool) -> str:
    """What a failed turn said about ITSELF, without the model's answer.

    The failure classifier looks for "sign in" and "usage limit", and a
    model's answer is free to contain both: a Brainstorm about login flows
    that timed out used to mark a working seat signed out, and every later
    turn was refused until the wearer tapped Refresh. So the answer never
    reaches it. stderr is the CLI's own; in stream-json mode the CLI's
    complaints are the lines that are not JSON and a result flagged
    `is_error`; in plain mode an answer and a complaint share stdout, and
    a CLI that refuses a turn says so last and exits, so only the last
    lines count, and none at all after a timeout (a hung turn was talking,
    not refusing).
    """
    parts = [*stderr, *stdout_errors]
    if plain_stdout and not timed_out:
        complaints = (_complaint(ln) for ln in "".join(plain_stdout).splitlines() if ln.strip())
        parts.append("\n".join([c for c in complaints if c][-3:]))
    return "\n".join(part for part in parts if part)[-4000:]


def _stream_json_event(line: str) -> tuple[str, str | None, bool]:
    """One `--output-format stream-json` line, read for what it carries.

    Returns (incremental text, final answer or None, the CLI called it an
    error). Shapes taken from a real run, not from documentation:

        {"type":"stream_event","event":{"type":"content_block_delta",
         "index":0,"delta":{"type":"text_delta","text":"Ca"}}}
        {"type":"result","result":"<the whole answer>","is_error":false}

    Anything unrecognised yields nothing, so an added event type is inert
    rather than corrupting.
    """
    try:
        event = json.loads(line)
    except (ValueError, UnicodeError):
        return "", None, False
    if not isinstance(event, dict):
        return "", None, False
    kind = event.get("type")
    if kind == "stream_event":
        inner = event.get("event")
        if isinstance(inner, dict) and inner.get("type") == "content_block_delta":
            delta = inner.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                text = delta.get("text")
                if isinstance(text, str):
                    return text, None, False
        return "", None, False
    if kind == "result":
        final = event.get("result")
        return "", final if isinstance(final, str) else "", bool(event.get("is_error"))
    return "", None, False


class SubprocessAgent:
    """Run a CLI agent; yield its stdout in chunks as they arrive."""

    def __init__(self, command: list[str], env_strip: tuple[str, ...] = (),
                 timeout_s: float = 240.0, noise: re.Pattern[str] | None = None,
                 name: str | None = None,
                 model: Callable[[], str | None] | None = None,
                 model_flag: str = "-m",
                 stream_json: bool = False,
                 notices: bool = True,
                 capture: bool = False,
                 cwd: str | None = None,
                 env_extra: dict[str, str] | None = None,
                 env_provider: EnvProvider | None = None) -> None:
        # Read the CLI's incremental protocol instead of its final stdout.
        # Measured on the real binaries: plain `-p` delivers a whole answer
        # in one chunk with the first byte at 10.4s of a 10.7s turn, so the
        # wearer watches "Thinking…" for 98% of every turn. With deltas the
        # first words land at 6.7s. Opt-in per seat because `codex exec
        # --json` has no equivalent — it emits one item.completed at the end.
        self._stream_json = stream_json
        self._command = command
        self._name = name or os.path.basename(command[0])
        # Dropped on top of the shared list (`cli_env.STRIP_ENV`), which
        # every launch drops: a seat can add names, never let one through.
        self._env_strip = env_strip
        self._timeout_s = timeout_s
        self._noise = noise
        # Read PER CALL, not at construction. Boot-time capture is what made
        # the model a launch flag: the wearer could not change a seat without
        # restarting the engine. A provider returning None passes no flag and
        # leaves the tool on its own default.
        self._model = model
        self._model_flag = model_flag
        # The assistant seats (agents/cli_reply_engine.py) render their own
        # failure copy from the CLI's exit state, so they turn the debate
        # notices off and read these after the stream ends.
        self._notices = notices
        self._capture = capture
        # The assistant seats spawn in their own directory so the CLI does
        # not read this repository's CLAUDE.md/AGENTS.md as project context,
        # and hand the child the MCP token without touching os.environ.
        self._cwd = cwd
        self._env_extra = dict(env_extra or {})
        # Which subscription ACCOUNT this spawn runs as: read per call, like
        # the model, so switching accounts on the phone takes effect on the
        # very next turn without rebuilding any seat.
        self._env_provider = env_provider
        self.last_returncode: int | None = None
        self.last_failed = False
        self.last_output = ""
        #: The CLI's own complaints from the last turn, never the answer:
        #: what the failure classifier reads (see `_diagnostic_text`).
        self.last_diagnostic = ""

    def _argv(self, prompt: str) -> list[str]:
        chosen = self._model() if self._model is not None else None
        flag = [self._model_flag, chosen] if chosen else []
        # An empty prompt is "no prompt" — the assistant seats probe the
        # CLIs' login state with argv alone, and a trailing "" would be
        # parsed as a positional argument by both binaries.
        return [*self._command, *flag, *([prompt] if prompt else [])]

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        env = cli_env(self._env_strip)
        env.update(self._env_extra)
        if self._env_provider is not None:
            for key, value in self._env_provider().items():
                if value is None:
                    env.pop(key, None)
                else:
                    env[key] = value
        self.last_returncode = None
        self.last_failed = False
        self.last_output = ""
        self.last_diagnostic = ""
        proc = await asyncio.create_subprocess_exec(
            *self._argv(prompt),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE if self._capture else asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
            env=env,
            cwd=self._cwd,
        )
        assert proc.stdout is not None
        captured: list[str] = []
        stderr_text: list[str] = []
        stdout_errors: list[str] = []
        plain_stdout: list[str] = []

        async def drain_stderr() -> None:
            # Bounded: a chatty child must neither deadlock on a full pipe
            # nor grow this without limit. The tail is what carries the
            # login or usage-limit complaint.
            assert proc.stderr is not None
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    return
                captured.append(chunk.decode(errors="replace"))
                stderr_text.append(captured[-1])
                if sum(len(c) for c in captured) > 16_000:
                    del captured[:-2]
                if sum(len(c) for c in stderr_text) > 8_000:
                    del stderr_text[:-2]

        stderr_task = asyncio.ensure_future(drain_stderr()) if self._capture else None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout_s
        pending = ""
        timed_out = False
        streamed = False
        final_answer = ""
        result_failed = False
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
                decoded = raw.decode(errors="replace")
                pending += decoded
                if self._capture:
                    captured.append(decoded)
                    if sum(len(c) for c in captured) > 16_000:
                        del captured[:-2]
                    if not self._stream_json:
                        plain_stdout.append(decoded)
                        if sum(len(c) for c in plain_stdout) > 8_000:
                            del plain_stdout[:-2]
                # Emit complete lines (noise-filtered); keep the tail fragment.
                *lines, pending = pending.split("\n")
                if self._stream_json:
                    # Coalesce every delta in THIS read into one yield.
                    # Each yield becomes a HUD repaint and a BLE frame, and
                    # the deltas inside a single 512-byte read are already
                    # simultaneous as far as the wearer is concerned —
                    # emitting them separately buys no responsiveness and
                    # spends link budget the glasses have to carry.
                    batch = []
                    for line in lines:
                        text, final, failed = _stream_json_event(line)
                        if final is not None:
                            final_answer, result_failed = final, failed
                        elif self._capture and line.strip() and not _is_json_line(line):
                            stdout_errors.append(line[-1_000:])
                            del stdout_errors[:-8]
                        if text:
                            batch.append(text)
                    if batch:
                        streamed = True
                        yield "".join(batch)
                    continue
                out = "\n".join(ln for ln in lines if not self._is_noise(ln))
                if out.strip():
                    yield out + "\n"
            if self._stream_json:
                # The last line need not arrive with a trailing newline, and
                # the last line is the one carrying `result` — the whole
                # answer. Leaving it in `pending` put the safety net below
                # out of reach in exactly the case it exists for.
                if pending.strip():
                    text, final, failed = _stream_json_event(pending)
                    if final is not None:
                        final_answer, result_failed = final, failed
                    elif self._capture and not _is_json_line(pending):
                        stdout_errors.append(pending[-1_000:])
                    if text:
                        streamed = True
                        yield text
                # Never lose an answer to a parse gap: if no delta arrived
                # but the CLI reported one, emit what it reported. A silent
                # truncation would be worse than no streaming at all.
                if not streamed and final_answer and not result_failed:
                    yield final_answer
            elif pending.strip() and not self._is_noise(pending):
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
                if self._notices:
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
                if proc.returncode or result_failed:
                    log.warning("agent %s exited %s", self._name, proc.returncode)
                    if self._notices:
                        yield "\n" + failure_notice(
                            self._name,
                            self._failure_detail(
                                _diagnostic_text(
                                    stderr_text,
                                    [*stdout_errors, *([final_answer] if result_failed else [])],
                                    plain_stdout, timed_out=False,
                                ),
                                proc.returncode,
                            ),
                        ) + "\n"
        finally:
            if proc.returncode is None:
                proc.kill()
            if stderr_task is not None:
                try:
                    await asyncio.wait_for(stderr_task, 2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    stderr_task.cancel()
            self.last_returncode = proc.returncode
            self.last_failed = timed_out or bool(proc.returncode) or result_failed
            self.last_output = "".join(captured)[-4000:]
            self.last_diagnostic = _diagnostic_text(
                stderr_text,
                [*stdout_errors, *([final_answer] if result_failed else [])],
                plain_stdout, timed_out=timed_out,
            )
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

    @staticmethod
    def _failure_detail(output: str, returncode: int | None) -> str:
        """Name the two causes the wearer can fix from the phone; keep the
        exit code for everything else (the log has the rest)."""
        kind = classify_cli_failure(output)
        if kind == "signed_out":
            return SIGNED_OUT_DETAIL
        if kind == "limit":
            return LIMIT_DETAIL
        return f"exited {returncode} — see the log"

    def _is_noise(self, line: str) -> bool:
        return bool(self._noise and self._noise.match(line.strip()))


def proposer(
    model: Callable[[], str | None] | None = None,
    env_provider: EnvProvider | None = None,
) -> SubprocessAgent | None:
    """Codex proposes, in a read-only sandbox.

    `model` is a late-bound reader (normally ModelRegistry.provider("codex"))
    so a switch takes effect on the next turn. With none supplied it falls
    back to the boot-time env pin, preserving the previous behaviour.
    """
    binary = find_codex_binary()
    if binary is None:
        return None
    if model is None:
        pinned = os.environ.get("CODEX_MODEL", DEFAULT_CODEX_MODEL)
        model = lambda: pinned  # noqa: E731 - a constant provider reads clearer here
    return SubprocessAgent(
        [binary, "exec", "-s", "read-only"],
        noise=_CODEX_NOISE,
        name="codex",
        model=model,
        model_flag="-m",
        # Captured so a failed turn can say "signed out" or "usage limit"
        # instead of an exit code (agents/cli_failures.py).
        capture=True,
        env_provider=env_provider,
    )


# MCP plumbing chatter claude prints on stdout; not part of any critique.
_CLAUDE_NOISE = re.compile(r"^Client\.\w+\(\) called but server does not advertise")


def critic(
    model: Callable[[], str | None] | None = None,
    env_provider: EnvProvider | None = None,
) -> SubprocessAgent | None:
    """Claude critiques, via the official binary only (RULES.md #9).

    The shared credential list is dropped, as on every launch, so the critic
    always runs on the subscription login — RULES #9 makes metered API an
    explicit config choice (AuthStrategy, with --max-budget-usd), never an
    accident of whatever the shell happened to export. Until 2026-10-06 the
    critic dropped only ANTHROPIC_API_KEY, so an inherited bearer token or
    base URL still moved its turns off the subscription."""
    binary = find_claude_binary()
    if binary is None:
        return None
    # `claude --model <alias|full-name>` (verified against --help). No
    # provider, or one returning None, leaves the CLI on its own default.
    # stream-json so the wearer sees the first words at ~6.7s instead of
    # ~10.4s of a ~10.7s turn. `--verbose` is required alongside it in
    # print mode; both were verified against the installed binary.
    return SubprocessAgent([binary, "-p", "--output-format", "stream-json",
                            "--include-partial-messages", "--verbose"],
                           noise=_CLAUDE_NOISE, name="claude",
                           model=model, model_flag="--model",
                           stream_json=True, capture=True,
                           env_provider=env_provider)
