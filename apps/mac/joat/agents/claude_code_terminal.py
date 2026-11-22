"""Claude Code sessions in the Agent Terminal, run by JOAT on this Mac.

JOAT hosts the session through the Claude Agent SDK, which drives the
installed `claude` CLI under the wearer's own login. JOAT therefore owns
every turn: it sees the output as it streams, answers each permission prompt
from the glasses, interrupts a running turn, and resumes earlier sessions.

Sessions Claude Code saved on this Mac (including ones started in a Terminal
window) are listed from its transcript store and can be resumed into this
lane. Resuming continues the conversation in a JOAT-hosted process; JOAT
cannot type into, or approve prompts in, a terminal window it does not own.

Nothing here names an account, a folder or a project: the working folder and
lanes come from the wearer's own settings (`ClaudeTerminalLaneConfig`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Where the wearer's lane settings live (optional; one default lane without it).
CONFIG_PATH = Path.home() / "Library" / "Application Support" / "JOAT" / "claude-terminal.json"
#: How long one interrupt may take before the stop is called unconfirmed.
INTERRUPT_TIMEOUT_S = 10.0
#: Per-line cap for tool summaries on the lenses.
SUMMARY_CHARS = 120
#: Permission modes Claude Code accepts (`--permission-mode`).
PERMISSION_MODES = ("default", "acceptEdits", "plan", "bypassPermissions")


class ClaudeTerminalUnavailable(RuntimeError):
    """Claude Code cannot run here; `wearer_reason` is the fix, for the lenses."""

    def __init__(self, wearer_reason: str) -> None:
        super().__init__(wearer_reason)
        self.wearer_reason = wearer_reason


_NOT_INSTALLED = "Install Claude Code on this Mac and sign in, then retry."


@dataclass(frozen=True)
class ClaudeTerminalLaneConfig:
    """One spoken lane: its name, working folder and permission mode."""

    name: str
    cwd: str
    permission_mode: str = "default"
    model: str | None = None


def load_lane_configs(path: Path = CONFIG_PATH, *, home: Path | None = None) -> list[ClaudeTerminalLaneConfig]:
    """The wearer's Claude Code lanes; one lane in the home folder by default.

    File shape: ``{"lanes": [{"name": "claude", "cwd": "~/Developer/app",
    "permission_mode": "default"}]}``. A malformed file raises ValueError so
    the Mac can say what to fix rather than silently running somewhere else.
    """
    base = home if home is not None else Path.home()
    if not path.exists():
        return [ClaudeTerminalLaneConfig(name="claude", cwd=str(base))]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path.name} could not be read: {exc}") from exc
    lanes = raw.get("lanes") if isinstance(raw, dict) else None
    if not isinstance(lanes, list) or not lanes:
        raise ValueError(f"{path.name} needs a non-empty \"lanes\" list")
    configs: list[ClaudeTerminalLaneConfig] = []
    seen: set[str] = set()
    for entry in lanes:
        if not isinstance(entry, dict):
            raise ValueError(f"{path.name}: every lane must be an object")
        name = " ".join(str(entry.get("name", "")).casefold().split())
        if not name or not all(c.isalnum() or c == " " for c in name):
            raise ValueError(f"{path.name}: lane names are letters, digits and spaces")
        if name in seen:
            raise ValueError(f"{path.name}: lane '{name}' is listed twice")
        seen.add(name)
        cwd_raw = entry.get("cwd")
        cwd = Path(os.path.expanduser(str(cwd_raw))) if cwd_raw else base
        if not cwd.is_dir():
            raise ValueError(f"{path.name}: lane '{name}' folder does not exist: {cwd}")
        mode = str(entry.get("permission_mode", "default"))
        if mode not in PERMISSION_MODES:
            raise ValueError(f"{path.name}: lane '{name}' permission_mode must be one of {', '.join(PERMISSION_MODES)}")
        model_raw = entry.get("model")
        model = model_raw.strip() if isinstance(model_raw, str) and model_raw.strip() else None
        configs.append(ClaudeTerminalLaneConfig(name=name, cwd=str(cwd), permission_mode=mode, model=model))
    return configs


@dataclass(frozen=True)
class _RosterPolicy:
    """The roster asks a lane's policy whether it may list and resume sessions."""

    roster: bool = True
    allows_approvals: bool = True


def summarize_tool(name: str, tool_input: Mapping[str, Any]) -> str:
    """One readable line for a tool call or a permission request."""
    for key in ("command", "file_path", "path", "url", "pattern", "query", "description"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            detail = " ".join(value.split())
            break
    else:
        detail = " ".join(json.dumps(dict(tool_input), ensure_ascii=False, default=str).split())
    text = f"{name}: {detail}" if detail and detail != "{}" else name
    return text if len(text) <= SUMMARY_CHARS else text[: SUMMARY_CHARS - 1] + "…"


def _message_text(message: Any) -> str:
    """The readable text of a stored transcript message (user or assistant)."""
    content = message.get("content") if isinstance(message, Mapping) else None
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [
        block.get("text", "") for block in content
        if isinstance(block, Mapping) and block.get("type") == "text"
    ]
    return "\n".join(part for part in parts if part)


def _clean_env(overlay: Mapping[str, str | None]) -> dict[str, str]:
    """The SDK merges `env` onto the engine's own; None means 'not this login'.

    The SDK cannot delete an inherited variable, so a variable the account
    overlay removes is blanked instead when the engine happens to carry it.
    """
    env: dict[str, str] = {}
    for key, value in overlay.items():
        if value is not None:
            env[key] = value
        elif key in os.environ:
            env[key] = ""
    return env


class ClaudeCodeTerminalAgent:
    """One Agent Terminal lane backed by a JOAT-hosted Claude Code session."""

    policy = _RosterPolicy()
    provider = "claude-code"

    def __init__(
        self,
        config: ClaudeTerminalLaneConfig,
        *,
        cli_path: Callable[[], str | None],
        env_provider: Callable[[], Mapping[str, str | None]] = dict,
        on_approval: Callable[[str, str, str], None] | None = None,
        sdk: Any = None,
    ) -> None:
        self.config = config
        self.display_name = config.name
        self._cli_path = cli_path
        self._env_provider = env_provider
        self._on_approval = on_approval
        self._sdk = sdk
        self._session_id: str | None = None
        self._generation: int | None = None
        self._client: Any = None
        self._pending: dict[str, asyncio.Future[str]] = {}
        #: Sessions JOAT listed: id -> the folder that session ran in.
        self._listed: dict[str, str] = {}
        #: The folder of the session being continued (a resumed one may differ).
        self._cwd = config.cwd
        self._model: str | None = config.model
        #: What the roster reads after cancelling a turn.
        self.cancellation_status = "none"

    # ── wiring ───────────────────────────────────────────────────────────

    def set_approval_callback(self, callback: Callable[[str, str, str], None]) -> None:
        self._on_approval = callback

    def _module(self) -> Any:
        if self._sdk is None:
            import claude_agent_sdk  # imported late: the engine runs without it

            self._sdk = claude_agent_sdk
        return self._sdk

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def cwd(self) -> str:
        return self._cwd

    @property
    def model(self) -> str | None:
        return self._model

    async def set_cwd(self, cwd: str) -> None:
        if self._client is not None:
            raise RuntimeError("Finish or cancel the running turn first")
        resolved = Path(os.path.expanduser(cwd))
        if not resolved.is_dir():
            raise ValueError(f"Folder does not exist: {resolved}")
        self.config = ClaudeTerminalLaneConfig(
            name=self.config.name,
            cwd=str(resolved),
            permission_mode=self.config.permission_mode,
            model=self._model,
        )
        self._cwd = str(resolved)
        self._session_id = None
        self._generation = (self._generation or 0) + 1

    async def set_model(self, model: str | None) -> None:
        if self._client is not None:
            raise RuntimeError("Finish or cancel the running turn first")
        cleaned = model.strip() if isinstance(model, str) and model.strip() else None
        self._model = cleaned
        self.config = ClaudeTerminalLaneConfig(
            name=self.config.name,
            cwd=self.config.cwd,
            permission_mode=self.config.permission_mode,
            model=cleaned,
        )

    @property
    def capture_context_generation(self) -> int | None:
        return self._generation

    async def initialize(self) -> None:
        """Ready when the CLI and the SDK are both present on this Mac."""
        if self._cli_path() is None:
            raise ClaudeTerminalUnavailable(_NOT_INSTALLED)
        try:
            self._module()
        except ImportError as exc:
            raise ClaudeTerminalUnavailable("Update JOAT on this Mac: the Claude Agent SDK is missing.") from exc
        if self._generation is None:
            self._generation = 0

    # ── turns ────────────────────────────────────────────────────────────

    def stream(self, prompt: str) -> AsyncIterator[str]:
        return self._turn(prompt)

    def stream_captured(self, prompt: str, *, context_generation: int) -> AsyncIterator[str]:
        if context_generation != self._generation:
            raise RuntimeError("The Claude Code session changed; open it again")
        return self._turn(prompt)

    def _options(self) -> Any:
        sdk = self._module()
        cli = self._cli_path()
        if cli is None:
            raise ClaudeTerminalUnavailable(_NOT_INSTALLED)
        kwargs: dict[str, Any] = {
            "cwd": self._cwd,
            "cli_path": cli,
            "env": _clean_env(self._env_provider()),
            "resume": self._session_id,
            "permission_mode": self.config.permission_mode,
            "can_use_tool": self._can_use_tool,
            # The wearer's own CLAUDE.md, settings and skills, as in a terminal.
            "setting_sources": ["user", "project", "local"],
        }
        if self._model:
            kwargs["model"] = self._model
        return sdk.ClaudeAgentOptions(**kwargs)

    async def _turn(self, prompt: str) -> AsyncIterator[str]:
        if self._client is not None:
            raise RuntimeError("A Claude Code turn is already running in this lane")
        sdk = self._module()
        client = sdk.ClaudeSDKClient(self._options())
        self._client = client
        self.cancellation_status = "none"
        try:
            await client.connect()
            if self._generation is None:
                self._generation = 0
            await client.query(prompt)
            async for message in client.receive_response():
                session = getattr(message, "session_id", None)
                if isinstance(session, str) and session:
                    self._session_id = session
                for line in self._render(sdk, message):
                    yield line
        except asyncio.CancelledError:
            await self._interrupt(client)
            raise
        finally:
            self._deny_pending("The turn ended before the wearer answered.")
            self._client = None
            try:
                await asyncio.shield(client.disconnect())
            except Exception:
                log.debug("claude code disconnect failed", exc_info=True)

    def _render(self, sdk: Any, message: Any) -> list[str]:
        if isinstance(message, sdk.AssistantMessage):
            lines: list[str] = []
            for block in message.content:
                if isinstance(block, sdk.TextBlock) and block.text.strip():
                    lines.append(block.text.rstrip() + "\n")
                elif isinstance(block, sdk.ToolUseBlock):
                    lines.append("⏺ " + summarize_tool(block.name, block.input or {}) + "\n")
            return lines
        if isinstance(message, sdk.ResultMessage) and message.is_error:
            detail = "; ".join(message.errors or []) or message.result or message.subtype
            raise RuntimeError(f"Claude Code stopped: {detail}")
        return []

    async def _interrupt(self, client: Any) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(client.interrupt()), INTERRUPT_TIMEOUT_S)
            self.cancellation_status = "confirmed"
        except Exception:
            log.warning("claude code interrupt unconfirmed", exc_info=True)
            self.cancellation_status = "uncertain"

    # ── approvals ────────────────────────────────────────────────────────

    async def _can_use_tool(self, tool_name: str, tool_input: dict[str, Any], _context: Any) -> Any:
        sdk = self._module()
        approval_id = "cc-" + secrets.token_hex(4)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending[approval_id] = future
        if self._on_approval is not None:
            self._on_approval(approval_id, tool_name, summarize_tool(tool_name, tool_input))
        try:
            decision = await future
        finally:
            self._pending.pop(approval_id, None)
        if decision == "approve":
            return sdk.PermissionResultAllow(updated_input=tool_input)
        return sdk.PermissionResultDeny(message="The wearer denied this from the glasses.")

    async def resolve_approval(self, approval_id: str, kind: str, decision: str) -> str | None:
        """Answer a waiting permission prompt; returns the recorded verdict."""
        verdict = "approve" if decision == "approve" else "deny"
        future = self._pending.get(approval_id)
        if future is None or future.done():
            raise RuntimeError("That request is no longer waiting")
        future.set_result(verdict)
        return decision if decision in ("approve", "deny") else verdict

    def _deny_pending(self, _reason: str) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_result("deny")
        self._pending.clear()

    # ── sessions ─────────────────────────────────────────────────────────

    async def list_sessions(self, limit: int = 20) -> list[dict[str, str]]:
        """Claude Code's saved sessions on this Mac, newest first.

        Every folder, not only this lane's: a session started in a Terminal
        window elsewhere is resumable here, in the folder it ran in.
        """
        sdk = self._module()
        rows = await asyncio.to_thread(sdk.list_sessions, None, limit)
        listed: list[dict[str, str]] = []
        self._listed = {}
        for row in rows:
            title = row.custom_title or row.summary or row.first_prompt or row.session_id
            folder = row.cwd if isinstance(row.cwd, str) and row.cwd else self.config.cwd
            label = " ".join(str(title).split())[:60]
            # Claude Code records the resolved path (/private/tmp, not /tmp).
            if os.path.realpath(folder) != os.path.realpath(self.config.cwd):
                label = f"{label} ({Path(folder).name})"
            listed.append({"key": row.session_id, "label": label})
            self._listed[row.session_id] = folder
        return listed

    async def resume_session(self, session_key: str) -> None:
        if self._client is not None:
            raise RuntimeError("Finish or cancel the running turn first")
        if session_key not in self._listed:
            raise RuntimeError("Only a session JOAT just listed can be resumed")
        folder = self._listed[session_key]
        if not Path(folder).is_dir():
            raise RuntimeError(f"That session's folder is gone: {folder}")
        self._session_id = session_key
        self._cwd = folder
        self._generation = (self._generation or 0) + 1

    async def history(self, limit: int = 10) -> list[tuple[str, str]]:
        """The session's own recent messages, from Claude Code's transcript."""
        if self._session_id is None:
            return []
        sdk = self._module()
        messages = await asyncio.to_thread(sdk.get_session_messages, self._session_id, self._cwd)
        rows: list[tuple[str, str]] = []
        for item in messages:
            if item.type not in ("user", "assistant"):
                continue
            text = _message_text(item.message).strip()
            if text:
                rows.append((item.type, text))
        return rows[-limit:]

    async def reset_conversation(self) -> None:
        if self._client is not None:
            raise RuntimeError("Finish or cancel the running turn first")
        self._session_id = None
        self._cwd = self.config.cwd
        self._generation = (self._generation or 0) + 1

    async def close(self) -> None:
        self._deny_pending("JOAT is shutting down.")
        client = self._client
        if client is not None:
            await self._interrupt(client)


def build_claude_lanes(
    configs: Sequence[ClaudeTerminalLaneConfig],
    *,
    cli_path: Callable[[], str | None],
    env_provider: Callable[[], Mapping[str, str | None]],
) -> dict[str, ClaudeCodeTerminalAgent]:
    return {
        config.name: ClaudeCodeTerminalAgent(config, cli_path=cli_path, env_provider=env_provider)
        for config in configs
    }
