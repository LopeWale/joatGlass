"""The mind on the wearer's subscriptions: `claude -p` and `codex exec`.

The BRAIN — the Mac app around these classes — owns the senses, the hands
and the access. The MIND is only the model plus the loop that decides what
to say and which tool to ask the brain to run. These seats fill it with the
two CLIs the wearer already pays for, through the official binaries only
(RULES.md #9 and #10): Claude Code on the Max subscription, Codex CLI on
the ChatGPT subscription. No API keys, ever — every spawn strips the
ambient key and base-URL variables so a stray key cannot move the billing
boundary, the same guard agents/claude_completer.py holds.

The spawning, streaming and timeout mechanics are ``SubprocessAgent`` —
the same class that runs the Brainstorm seats — so a seat here is only an
argv builder plus the two things a conversation needs that a debate turn
does not: a session that resumes between turns, and an honest account
state the seat switch can read.

The brain's tools reach the CLIs over MCP: services/agent_endpoint.py
serves the belt at ``/mcp`` on loopback and each spawn is told about it.
The CLI runs its own tool loop; the brain executes the tools.

Measured on this Mac, 2026-09-03: `codex exec` answers a one-word prompt in
6-8 s cold (it ships ~16k tokens of its own context per turn); `claude -p`
on the Max login answers "pong" in about 3 s. `--bare` is NOT used: in bare
mode the CLI ignores the OAuth login and wants an API key — observed live
as an instant error with the login present.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

from ..core.interfaces import ReplyEngine
from .grounding import Grounding
from .subprocess_agent import SubprocessAgent, find_claude_binary

log = logging.getLogger(__name__)

FIRST_CHUNK_CHARS = 8
CHUNK_CHARS = 64
SESSION_TTL_S = 1800.0
TURN_TIMEOUT_S = 90.0
AUTH_PROBE_TIMEOUT_S = 20.0
STRIP_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY",
)
DEFAULT_WORKDIR = Path.home() / "Library/Application Support/JOAT/mind"

PERSONA = (
    "You are JOAT, the wearer's personal assistant, speaking through the "
    "display of their Even Realities G2 smart glasses.\n"
    "\n"
    "How to answer:\n"
    "- Answer the actual question, fully, from your own knowledge. Jokes, "
    "facts, explanations, advice, arithmetic: just answer.\n"
    "- Use a tool when the answer depends on something live or personal: "
    "the weather, the wearer's location and nearby places, their "
    "notifications and messages, the news, a web search for recent events, "
    "their focus timer. Call it without narrating, then answer.\n"
    "- If a tool fails or returns nothing, say in one short clause what you "
    "could not get, and still give the best answer you can.\n"
    "- Never invent the wearer's personal data: their messages, calendar, "
    "location, or device state. The facts block says what you have.\n"
    "\n"
    "How to speak:\n"
    "- Plain text only: no markdown, no lists, no headings, no emoji, no "
    "code blocks.\n"
    "- Short. The display shows about four lines. Default to one or two "
    "sentences, at most about 60 words. Give more only when the wearer "
    "asks for detail.\n"
    "- Direct and warm. No preamble, no disclaimers about being an AI, no "
    "restating the question."
)

FAILED_NOTICE = "That turn failed — say it again in a moment."
MISSING_NOTICE = "{name} isn't installed on the Mac."
SIGNED_OUT_NOTICE = "{name} isn't signed in on the Mac — run: {command}"
LIMIT_NOTICE = "{name} has hit its usage limit for now."
NO_SEAT_NOTICE = "No assistant seat is available on the Mac — sign in to Claude or Codex."

ModelProvider = Callable[[], str | None]
AccountObserver = Callable[[str, str], None]
"""(seat name, account state) — 'logged_in', 'logged_out', 'missing', 'unknown'."""

@dataclass(frozen=True)
class McpDoor:
    """Where a CLI finds the brain's tools: the loopback MCP route and its token."""

    url: str
    token: str


def find_codex_binary() -> str | None:
    return shutil.which("codex")


def _classify_failure(name: str, login_command: str, output: str) -> str:
    """Turn a CLI's own complaint into the one line the wearer can act on."""
    lowered = output.lower()
    if "not logged in" in lowered or "please run /login" in lowered or "sign in" in lowered:
        return SIGNED_OUT_NOTICE.format(name=name, command=login_command)
    if "usage limit" in lowered or "out of usage" in lowered or "rate limit" in lowered:
        return LIMIT_NOTICE.format(name=name)
    return FAILED_NOTICE


class _CliSeat(ReplyEngine):
    """What the two seats share: one turn at a time, a session that forgets
    after a quiet half hour, the account state, and the fallback."""

    name = "cli"
    login_command = ""

    def __init__(
        self,
        binary: str | None,
        *,
        grounding: Grounding | None = None,
        model: ModelProvider | None = None,
        mcp: McpDoor | None = None,
        fallback: ReplyEngine | None = None,
        timeout_s: float = TURN_TIMEOUT_S,
        workdir: Path = DEFAULT_WORKDIR,
        on_account: AccountObserver | None = None,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("timeout must be positive")
        self._binary = binary
        self._grounding = grounding
        self._model = model
        self._mcp = mcp
        self._fallback = fallback
        self._timeout_s = timeout_s
        self._workdir = workdir
        self._on_account = on_account
        self._account = "missing" if binary is None else "unknown"
        self._session_id: str | None = None
        self._session_used_at = 0.0
        self._lock = asyncio.Lock()

    # ── state the seat switch reads ─────────────────────────────────────────

    @property
    def title(self) -> str:
        return self.name.capitalize()

    @property
    def account(self) -> str:
        return self._account

    @property
    def available(self) -> bool:
        return self._binary is not None and self._account != "logged_out"

    def _set_account(self, state: str) -> None:
        if state != self._account:
            self._account = state
            if self._on_account is not None:
                self._on_account(self.name, state)

    # ── the reply contract ─────────────────────────────────────────────────

    async def reply(self, text: str) -> str | None:
        parts = [chunk async for chunk in self.stream(text)]
        return "".join(parts) or None

    async def stream(self, text: str) -> AsyncIterator[str]:
        if not text.strip():
            return
        async with self._lock:
            if self._binary is None:
                async for chunk in self._degraded(text, MISSING_NOTICE.format(name=self.title)):
                    yield chunk
                return
            if self._account == "logged_out":
                async for chunk in self._degraded(
                    text, SIGNED_OUT_NOTICE.format(name=self.title, command=self.login_command),
                ):
                    yield chunk
                return
            self._workdir.mkdir(parents=True, exist_ok=True)
            resuming = self._session_alive()
            if not resuming:
                self._session_id = str(uuid.uuid4())
            spoke = False
            async for chunk in self._turn(text, resuming):
                spoke = True
                yield chunk
            if spoke:
                self._session_used_at = time.monotonic()

    def _turn(self, text: str, resuming: bool) -> AsyncIterator[str]:
        """One spawned turn; subclasses are async generators."""
        raise NotImplementedError

    def _session_alive(self) -> bool:
        return (
            self._session_id is not None
            and time.monotonic() - self._session_used_at < SESSION_TTL_S
        )

    async def _degraded(self, text: str, notice: str) -> AsyncIterator[str]:
        if self._fallback is not None:
            spoke = False
            try:
                async for chunk in self._fallback.stream(text):
                    spoke = True
                    yield chunk
            except Exception:
                log.exception("fallback reply engine failed")
            if spoke:
                return
        yield notice

    # ── spawning, through the Brainstorm seats' own runner ──────────────────

    def _agent(self, command: list[str], *, stream_json: bool,
               timeout_s: float | None = None,
               env_extra: dict[str, str] | None = None) -> SubprocessAgent:
        return SubprocessAgent(
            command, env_strip=STRIP_ENV, timeout_s=timeout_s or self._timeout_s,
            name=self.name, stream_json=stream_json, notices=False, capture=True,
            cwd=str(self._workdir), env_extra=env_extra,
        )

    async def _probe(self, argv: list[str]) -> tuple[int | None, str]:
        """Run a short CLI command; (exit code, everything it printed)."""
        agent = self._agent(argv, stream_json=False, timeout_s=AUTH_PROBE_TIMEOUT_S)
        try:
            async for _ in agent.stream(""):
                pass  # the capture below holds stdout and stderr, bounded
        except OSError as exc:
            return 127, str(exc)
        return agent.last_returncode, agent.last_output

    def _live_context(self) -> str:
        if self._grounding is None:
            return ""
        parts: list[str] = []
        facts = self._grounding.facts()
        if facts:
            parts.append(f"Facts you can rely on right now:\n{facts}")
        gaps = self._grounding.cannot_know()
        if gaps:
            parts.append(
                "You have no source for these and must not invent them — say "
                f"you do not have them: {gaps}."
            )
        return "\n\n".join(parts)

    def _failure(self, agent: SubprocessAgent, resuming: bool) -> str:
        notice = _classify_failure(self.title, self.login_command, agent.last_output)
        if "signed in" in notice:
            self._set_account("logged_out")
        elif resuming and "no conversation found" in agent.last_output.lower():
            self._session_id = None
        log.warning("%s turn failed rc=%s (%s)", self.name, agent.last_returncode, notice)
        return notice


class ClaudeCliReplyEngine(_CliSeat):
    """Claude Code in print mode, streamed, on the Max subscription.

    One process per turn. The session id carries the conversation between
    turns (`--session-id` first, `--resume` after), so working memory lives
    in the CLI's own transcript on this Mac. Built-in tools are switched off
    — the mind must never hold a shell on the wearer's Mac unattended — and
    the brain's belt is offered through `--mcp-config`. The prompt is the
    LAST argument, after an option that closes the variadic `--tools`.
    """

    name = "claude"
    login_command = "claude auth login"

    async def warm(self) -> bool:
        if self._binary is None:
            self._set_account("missing")
            return False
        code, output = await self._probe([self._binary, "auth", "status"])
        logged_in = False
        start = output.find("{")
        if start != -1:
            try:
                payload, _ = json.JSONDecoder().raw_decode(output[start:])
                logged_in = isinstance(payload, dict) and bool(payload.get("loggedIn"))
            except ValueError:
                logged_in = False
        if code == 127:
            self._set_account("missing")
            return False
        self._set_account("logged_in" if logged_in else "logged_out")
        return logged_in

    def _command(self, resuming: bool) -> list[str]:
        assert self._binary is not None and self._session_id is not None
        system = PERSONA
        live = self._live_context()
        if live:
            system = f"{PERSONA}\n\n{live}"
        command = [
            self._binary, "-p",
            "--tools", "",
            "--output-format", "stream-json", "--include-partial-messages", "--verbose",
            "--system-prompt", system,
        ]
        # ALWAYS strict: without it the CLI loads every MCP server in the
        # wearer's global config on each turn — measured 2026-09-03 as +4 s
        # of API time with `--tools ""` (5.6 s vs 1.4 s for "pong").
        servers: dict[str, object] = {}
        if self._mcp is not None:
            servers["joat"] = {
                "type": "http", "url": self._mcp.url,
                "headers": {"Authorization": f"Bearer {self._mcp.token}"},
            }
        command += ["--mcp-config", json.dumps({"mcpServers": servers}), "--strict-mcp-config"]
        if self._mcp is not None:
            # Print mode has nobody to answer a permission prompt. Without this
            # the model saw the tool and asked for approval (live 2026-09-03);
            # with it, it called get_weather and answered in 4.3 s.
            command += ["--allowedTools", "mcp__joat__*"]
        command += ["--resume", self._session_id] if resuming else ["--session-id", self._session_id]
        return command

    async def _turn(self, text: str, resuming: bool) -> AsyncIterator[str]:
        agent = self._agent(self._command(resuming), stream_json=True)
        agent._model = self._model  # noqa: SLF001 - the seats' late-bound model reader
        agent._model_flag = "--model"  # noqa: SLF001
        buf = ""
        threshold = FIRST_CHUNK_CHARS
        spoke = False
        try:
            async for piece in agent.stream(text):
                buf += piece
                while len(buf) >= threshold:
                    chunk, buf = buf[:threshold], buf[threshold:]
                    spoke = True
                    yield chunk
                    threshold = CHUNK_CHARS
        except OSError as exc:
            log.error("claude could not be started (%s)", type(exc).__name__)
            async for chunk in self._degraded(text, FAILED_NOTICE):
                yield chunk
            return
        if agent.last_failed:
            notice = self._failure(agent, resuming)
            if buf:
                yield buf
            elif not spoke:
                async for chunk in self._degraded(text, notice):
                    yield chunk
            return
        if buf:
            yield buf
        self._set_account("logged_in")


class CodexCliReplyEngine(_CliSeat):
    """Codex CLI in exec mode on the ChatGPT subscription.

    `codex exec --json` reports one `agent_message` item at the end of the
    turn, so the answer lands as a single chunk; `thread.started` carries
    the id that `codex exec resume <id>` continues. Codex has no
    system-prompt flag in exec mode, so the persona rides at the top of the
    first prompt and the live facts at the top of every prompt.
    """

    name = "codex"
    login_command = "codex login"

    def __init__(self, binary: str | None, **kwargs: object) -> None:
        super().__init__(binary, **kwargs)  # type: ignore[arg-type]
        self._thread_id: str | None = None

    async def warm(self) -> bool:
        if self._binary is None:
            self._set_account("missing")
            return False
        code, output = await self._probe([self._binary, "login", "status"])
        if code == 127:
            self._set_account("missing")
            return False
        lowered = output.lower()
        logged_in = "logged in" in lowered and "not logged in" not in lowered
        self._set_account("logged_in" if logged_in else "logged_out")
        return logged_in

    def _prompt(self, text: str, resuming: bool) -> str:
        live = self._live_context()
        head = "" if resuming else f"SYSTEM INSTRUCTIONS\n{PERSONA}\n\n"
        facts = f"{live}\n\n" if live else ""
        return f"{head}{facts}WEARER: {text}"

    def _command(self, resuming: bool) -> list[str]:
        assert self._binary is not None
        # Verified 2026-09-03: `codex exec resume` rejects -s/--json AFTER the
        # subcommand (exit 2, "unexpected argument"), and accepts them before
        # it. -m and the prompt, which the runner appends, are fine after.
        # --ignore-user-config: the wearer's own codex config carries a dozen
        # MCP servers (OAuth ones fail, slowly) and the model reached for one
        # of those instead of the brain's belt — observed live 2026-09-03.
        # Auth lives in CODEX_HOME/auth.json, which this flag leaves alone.
        #
        # It does NOT, however, suppress servers that arrive through plugins:
        # a turn still opened OAuth connections to the wearer's Cloudflare and
        # RunPod servers and watched them fail. Measured 2026-09-04, same
        # prompt: two such failures per turn with plugins on, zero with them
        # off. A voice turn has no business dialling either.
        command = [self._binary, "exec", "--json", "-s", "read-only", "--skip-git-repo-check",
                   "--ignore-user-config", "-c", "features.plugins=false"]
        if self._mcp is not None:
            command += [
                "-c", f'mcp_servers.joat.url="{self._mcp.url}"',
                "-c", 'mcp_servers.joat.bearer_token_env_var="JOAT_AGENT_TOKEN"',
                # Without this the model calls the tool and the CLI refuses it
                # — "MCP tool call requires approval, but approval policy is
                # never" — because exec has nobody to ask. Scoped to this one
                # server, and it does not touch the sandbox: the turn stays
                # read-only with approval_policy never. A/B measured
                # 2026-09-04: without it, no tools/call and a refusal; with
                # it, the tool ran and its text reached the answer.
                "-c", 'mcp_servers.joat.default_tools_approval_mode="approve"',
            ]
        if resuming and self._thread_id:
            command += ["resume", self._thread_id]
        return command

    async def _turn(self, text: str, resuming: bool) -> AsyncIterator[str]:
        resuming = resuming and self._thread_id is not None
        token_env = {"JOAT_AGENT_TOKEN": self._mcp.token} if self._mcp is not None else None
        agent = self._agent(self._command(resuming), stream_json=False, env_extra=token_env)
        agent._model = self._model  # noqa: SLF001
        agent._model_flag = "-m"  # noqa: SLF001
        lines: list[str] = []
        try:
            async for piece in agent.stream(self._prompt(text, resuming)):
                lines.append(piece)
        except OSError as exc:
            log.error("codex could not be started (%s)", type(exc).__name__)
            async for chunk in self._degraded(text, FAILED_NOTICE):
                yield chunk
            return
        answer = ""
        for line in "".join(lines).splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "thread.started" and isinstance(event.get("thread_id"), str):
                self._thread_id = event["thread_id"]
            elif kind == "item.completed":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    text_out = item.get("text")
                    if isinstance(text_out, str):
                        answer = text_out
        if agent.last_failed or not answer:
            notice = self._failure(agent, resuming)
            if resuming and "signed in" not in notice:
                self._thread_id = None
            async for chunk in self._degraded(text, notice):
                yield chunk
            return
        self._set_account("logged_in")
        yield answer.strip()


class SeatSwitch(ReplyEngine):
    """One mind, several seats: whichever the wearer picked, if it can speak.

    The assistant slot (services/models.py) names the seat per turn —
    'claude', 'codex', 'local' — so "use codex for the assistant" lands on
    the next question. A seat that is missing or signed out is skipped for
    the next one in order, and the choice is logged once per change.
    """

    def __init__(
        self,
        seats: dict[str, ReplyEngine],
        chosen: ModelProvider,
        order: tuple[str, ...] = ("claude", "codex", "local"),
        on_pick: Callable[[str], None] | None = None,
    ) -> None:
        if not seats:
            raise ValueError("at least one seat is required")
        self._seats = seats
        self._chosen = chosen
        self._order = order
        self._on_pick = on_pick
        self._last_pick: str | None = None

    @property
    def seats(self) -> dict[str, ReplyEngine]:
        return dict(self._seats)

    def pick(self) -> tuple[str, ReplyEngine] | None:
        wanted = self._chosen() or ""
        candidates = [wanted, *[s for s in self._order if s != wanted]]
        for name in candidates:
            engine = self._seats.get(name)
            if engine is None:
                continue
            if getattr(engine, "available", True):
                if name != self._last_pick:
                    if wanted and name != wanted:
                        log.warning("assistant seat %r unavailable — using %r", wanted, name)
                    else:
                        log.info("assistant seat: %s", name)
                    self._last_pick = name
                    if self._on_pick is not None:
                        self._on_pick(name)
                return name, engine
        return None

    async def warm(self) -> bool:
        results: list[bool] = []
        for engine in self._seats.values():
            warm = getattr(engine, "warm", None)
            if warm is None:
                continue
            try:
                results.append(bool(await warm()))
            except Exception:
                log.exception("seat warm-up failed")
                results.append(False)
        return self.pick() is not None and any(results)

    async def reply(self, text: str) -> str | None:
        parts = [chunk async for chunk in self.stream(text)]
        return "".join(parts) or None

    async def stream(self, text: str) -> AsyncIterator[str]:
        picked = self.pick()
        if picked is None:
            yield NO_SEAT_NOTICE
            return
        _, engine = picked
        async for chunk in engine.stream(text):
            yield chunk


__all__ = [
    "CHUNK_CHARS",
    "FAILED_NOTICE",
    "FIRST_CHUNK_CHARS",
    "LIMIT_NOTICE",
    "MISSING_NOTICE",
    "NO_SEAT_NOTICE",
    "PERSONA",
    "SESSION_TTL_S",
    "SIGNED_OUT_NOTICE",
    "STRIP_ENV",
    "ClaudeCliReplyEngine",
    "CodexCliReplyEngine",
    "McpDoor",
    "SeatSwitch",
    "find_codex_binary",
]
