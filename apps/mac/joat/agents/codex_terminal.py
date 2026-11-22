"""Codex sessions in the Agent Terminal, run by JOAT on this Mac.

JOAT starts `codex app-server` (the JSON-RPC interface the Codex IDE
extension uses, over stdio JSONL) under the wearer's own Codex login and
drives the thread itself: streamed items reach the lenses, every command or
file-change approval becomes a terminal approval, `turn/interrupt` stops a
turn, and `thread/list` + `thread/resume` reopen saved Codex threads,
including ones started in a Terminal window. JOAT does not control a Codex
process it did not start.

Protocol per https://learn.chatgpt.com/docs/app-server. The lane name,
folder and approval mode come from the wearer's settings; nothing here names
an account or a project.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .claude_code_terminal import ClaudeTerminalUnavailable, summarize_tool

log = logging.getLogger(__name__)

CONFIG_PATH = Path.home() / "Library" / "Application Support" / "JOAT" / "codex-terminal.json"
REQUEST_TIMEOUT_S = 30.0
INTERRUPT_TIMEOUT_S = 10.0
#: The JSONL reader's per-line ceiling; an item can carry a whole diff.
LINE_LIMIT = 8 * 1024 * 1024
SANDBOX_MODES = ("read-only", "workspace-write", "danger-full-access")
_NOT_INSTALLED = "Install Codex on this Mac and sign in, then retry."


class CodexTerminalLaneConfig:
    __slots__ = ("name", "cwd", "sandbox", "model")

    def __init__(self, name: str, cwd: str, sandbox: str = "workspace-write", model: str | None = None) -> None:
        self.name, self.cwd, self.sandbox, self.model = name, cwd, sandbox, model

    def __eq__(self, other: object) -> bool:
        return isinstance(other, CodexTerminalLaneConfig) and (
            (self.name, self.cwd, self.sandbox, self.model) == (other.name, other.cwd, other.sandbox, other.model))

    def __repr__(self) -> str:
        return f"CodexTerminalLaneConfig({self.name!r}, {self.cwd!r}, {self.sandbox!r}, {self.model!r})"


def load_codex_lane_configs(path: Path = CONFIG_PATH, *, home: Path | None = None) -> list[CodexTerminalLaneConfig]:
    """One "codex" lane in the home folder unless the wearer configured others.

    File shape: ``{"lanes": [{"name": "codex", "cwd": "~/Developer/app",
    "sandbox": "workspace-write"}]}``.
    """
    base = home if home is not None else Path.home()
    if not path.exists():
        return [CodexTerminalLaneConfig("codex", str(base))]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path.name} could not be read: {exc}") from exc
    lanes = raw.get("lanes") if isinstance(raw, dict) else None
    if not isinstance(lanes, list) or not lanes:
        raise ValueError(f"{path.name} needs a non-empty \"lanes\" list")
    configs: list[CodexTerminalLaneConfig] = []
    for entry in lanes:
        if not isinstance(entry, dict):
            raise ValueError(f"{path.name}: every lane must be an object")
        name = " ".join(str(entry.get("name", "")).casefold().split())
        if not name or not all(c.isalnum() or c == " " for c in name):
            raise ValueError(f"{path.name}: lane names are letters, digits and spaces")
        if any(c.name == name for c in configs):
            raise ValueError(f"{path.name}: lane '{name}' is listed twice")
        cwd = Path(os.path.expanduser(str(entry["cwd"]))) if entry.get("cwd") else base
        if not cwd.is_dir():
            raise ValueError(f"{path.name}: lane '{name}' folder does not exist: {cwd}")
        sandbox = str(entry.get("sandbox", "workspace-write"))
        if sandbox not in SANDBOX_MODES:
            raise ValueError(f"{path.name}: lane '{name}' sandbox must be one of {', '.join(SANDBOX_MODES)}")
        model_raw = entry.get("model")
        model = model_raw.strip() if isinstance(model_raw, str) and model_raw.strip() else None
        configs.append(CodexTerminalLaneConfig(name, str(cwd), sandbox, model))
    return configs


class _RosterPolicy:
    roster = True
    allows_approvals = True


def _clean_env(overlay: Mapping[str, str | None]) -> dict[str, str]:
    env = dict(os.environ)
    for key, value in overlay.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


class CodexTerminalAgent:
    """One Agent Terminal lane backed by a JOAT-hosted `codex app-server`."""

    policy = _RosterPolicy()
    provider = "codex"

    def __init__(
        self,
        config: CodexTerminalLaneConfig,
        *,
        binary: Callable[[], str | None],
        env_provider: Callable[[], Mapping[str, str | None]] = dict,
        on_approval: Callable[[str, str, str], None] | None = None,
        spawn: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self.display_name = config.name
        self._binary = binary
        self._env_provider = env_provider
        self._on_approval = on_approval
        self._spawn = spawn or asyncio.create_subprocess_exec
        self._proc: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._next_id = 1
        self._waiting: dict[int, asyncio.Future[Any]] = {}
        self._events: asyncio.Queue[dict[str, Any]] | None = None
        self._approvals: dict[str, tuple[Any, asyncio.Future[str]]] = {}
        self._thread_id: str | None = None
        self._turn_id: str | None = None
        self._cwd = config.cwd
        self._model: str | None = config.model
        self._listed: dict[str, str] = {}
        self._generation: int | None = None
        self._lock = asyncio.Lock()
        self.cancellation_status = "none"

    def set_approval_callback(self, callback: Callable[[str, str, str], None]) -> None:
        self._on_approval = callback

    @property
    def capture_context_generation(self) -> int | None:
        return self._generation

    @property
    def thread_id(self) -> str | None:
        return self._thread_id

    @property
    def cwd(self) -> str:
        return self._cwd

    @property
    def model(self) -> str | None:
        return self._model

    async def set_cwd(self, cwd: str) -> None:
        if self._lock.locked():
            raise RuntimeError("Finish or cancel the running turn first")
        resolved = Path(os.path.expanduser(cwd))
        if not resolved.is_dir():
            raise ValueError(f"Folder does not exist: {resolved}")
        self.config = CodexTerminalLaneConfig(self.config.name, str(resolved), self.config.sandbox, self._model)
        self._cwd = str(resolved)
        self._thread_id = None
        self._generation = (self._generation or 0) + 1

    async def set_model(self, model: str | None) -> None:
        if self._lock.locked():
            raise RuntimeError("Finish or cancel the running turn first")
        cleaned = model.strip() if isinstance(model, str) and model.strip() else None
        if cleaned == self._model:
            return
        self._model = cleaned
        self.config = CodexTerminalLaneConfig(self.config.name, self.config.cwd, self.config.sandbox, cleaned)
        self._thread_id = None
        self._generation = (self._generation or 0) + 1

    # ── process and JSON-RPC ─────────────────────────────────────────────

    async def initialize(self) -> None:
        await self._ensure_server()
        if self._generation is None:
            self._generation = 0

    async def _ensure_server(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        binary = self._binary()
        if binary is None:
            raise ClaudeTerminalUnavailable(_NOT_INSTALLED)
        self._proc = await self._spawn(
            binary, "app-server",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=_clean_env(self._env_provider()),
            limit=LINE_LIMIT,
        )
        self._waiting = {}
        self._reader = asyncio.create_task(self._read_loop(self._proc))
        await self._request("initialize", {"clientInfo": {"name": "joat", "title": "JOAT", "version": "1.0"}})
        await self._send({"method": "initialized", "params": {}})

    async def _send(self, message: dict[str, Any]) -> None:
        self._proc.stdin.write((json.dumps(message) + "\n").encode())
        await self._proc.stdin.drain()

    async def _request(self, method: str, params: dict[str, Any], timeout: float = REQUEST_TIMEOUT_S) -> Any:
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = future
        await self._send({"method": method, "id": request_id, "params": params})
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self._waiting.pop(request_id, None)

    async def _read_loop(self, proc: Any) -> None:
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                if "method" in message and "id" in message:
                    self._server_request(message)
                elif "id" in message:
                    future = self._waiting.get(message["id"])
                    if future is not None and not future.done():
                        if "error" in message:
                            detail = message["error"].get("message") if isinstance(message["error"], dict) else message["error"]
                            future.set_exception(RuntimeError(f"Codex: {detail}"))
                        else:
                            future.set_result(message.get("result"))
                elif "method" in message and self._events is not None:
                    self._events.put_nowait(message)
        finally:
            for future in self._waiting.values():
                if not future.done():
                    future.set_exception(RuntimeError("Codex stopped"))
            if self._events is not None:
                self._events.put_nowait({"method": "joat/serverExited", "params": {}})
            self._deny_pending()

    def _server_request(self, message: dict[str, Any]) -> None:
        method = str(message.get("method"))
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method not in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            # Anything JOAT does not answer is declined, never left hanging.
            asyncio.get_running_loop().create_task(
                self._send({"id": message["id"], "error": {"code": -32601, "message": "not supported by JOAT"}}))
            return
        approval_id = "cx-" + secrets.token_hex(4)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._approvals[approval_id] = (message["id"], future)
        if method.startswith("item/commandExecution"):
            kind = "command"
            summary = summarize_tool("Run", {"command": params.get("command") or params.get("reason") or ""})
        else:
            kind = "file change"
            summary = summarize_tool("Edit", {"description": params.get("reason") or params.get("grantRoot") or "files"})
        asyncio.get_running_loop().create_task(self._answer_when_decided(approval_id))
        if self._on_approval is not None:
            self._on_approval(approval_id, kind, summary)

    async def _answer_when_decided(self, approval_id: str) -> None:
        request_id, future = self._approvals[approval_id]
        try:
            decision = await future
        finally:
            self._approvals.pop(approval_id, None)
        try:
            await self._send({"id": request_id, "result": {"decision": "accept" if decision == "approve" else "decline"}})
        except Exception:
            log.warning("codex approval answer failed", exc_info=True)

    def _deny_pending(self) -> None:
        for _request_id, future in list(self._approvals.values()):
            if not future.done():
                future.set_result("deny")

    async def resolve_approval(self, approval_id: str, kind: str, decision: str) -> str | None:
        entry = self._approvals.get(approval_id)
        if entry is None or entry[1].done():
            raise RuntimeError("That request is no longer waiting")
        entry[1].set_result("approve" if decision == "approve" else "deny")
        return decision if decision in ("approve", "deny") else "deny"

    # ── turns ────────────────────────────────────────────────────────────

    def stream(self, prompt: str) -> AsyncIterator[str]:
        return self._turn(prompt)

    def stream_captured(self, prompt: str, *, context_generation: int) -> AsyncIterator[str]:
        if context_generation != self._generation:
            raise RuntimeError("The Codex thread changed; open it again")
        return self._turn(prompt)

    async def _turn(self, prompt: str) -> AsyncIterator[str]:
        if self._lock.locked():
            raise RuntimeError("A Codex turn is already running in this lane")
        async with self._lock:
            await self._ensure_server()
            if self._generation is None:
                self._generation = 0
            self._events = asyncio.Queue()
            self.cancellation_status = "none"
            if self._thread_id is None:
                start_params: dict[str, Any] = {
                    "cwd": self._cwd, "approvalPolicy": "on-request", "sandbox": self.config.sandbox,
                }
                if self._model:
                    start_params["model"] = self._model
                result = await self._request("thread/start", start_params)
                self._thread_id = str(result["thread"]["id"])
            started = await self._request("turn/start", {
                "threadId": self._thread_id, "input": [{"type": "text", "text": prompt}],
            })
            turn = started.get("turn") if isinstance(started, dict) else None
            self._turn_id = str(turn["id"]) if isinstance(turn, dict) and turn.get("id") else None
            try:
                while True:
                    event = await self._events.get()
                    method = event.get("method")
                    params = event.get("params") or {}
                    if method == "joat/serverExited":
                        self._proc = None
                        raise RuntimeError("Codex stopped during the turn")
                    if method == "turn/started" and self._turn_id is None:
                        self._turn_id = str((params.get("turn") or {}).get("id") or "") or None
                    for line in self._render(method, params):
                        yield line
                    if method == "turn/completed":
                        finished = params.get("turn") or {}
                        if finished.get("status") == "failed":
                            error = finished.get("error") or {}
                            raise RuntimeError(f"Codex failed: {error.get('message') or 'unknown error'}")
                        return
            except asyncio.CancelledError:
                await self._interrupt()
                raise
            finally:
                self._turn_id = None
                self._events = None
                self._deny_pending()

    @staticmethod
    def _render(method: str | None, params: dict[str, Any]) -> list[str]:
        item = params.get("item") if isinstance(params.get("item"), dict) else None
        if item is None:
            return []
        kind = item.get("type")
        if method == "item/started" and kind == "commandExecution":
            return ["⏺ " + summarize_tool("Run", {"command": str(item.get("command") or "")}) + "\n"]
        if method == "item/started" and kind == "fileChange":
            paths = [str(change.get("path")) for change in item.get("changes") or [] if isinstance(change, dict)]
            return ["⏺ " + summarize_tool("Edit", {"file_path": ", ".join(paths) or "files"}) + "\n"]
        if method == "item/completed" and kind == "agentMessage":
            text = str(item.get("text") or "").rstrip()
            return [text + "\n"] if text else []
        return []

    async def _interrupt(self) -> None:
        if self._thread_id is None or self._turn_id is None:
            self.cancellation_status = "uncertain"
            return
        try:
            await asyncio.wait_for(asyncio.shield(self._request(
                "turn/interrupt", {"threadId": self._thread_id, "turnId": self._turn_id})), INTERRUPT_TIMEOUT_S)
            self.cancellation_status = "confirmed"
        except Exception:
            log.warning("codex interrupt unconfirmed", exc_info=True)
            self.cancellation_status = "uncertain"

    # ── threads ──────────────────────────────────────────────────────────

    async def list_sessions(self, limit: int = 20) -> list[dict[str, str]]:
        await self._ensure_server()
        result = await self._request("thread/list", {"limit": limit, "sortKey": "updated_at"})
        rows: list[dict[str, str]] = []
        self._listed = {}
        for thread in (result or {}).get("data") or []:
            if not isinstance(thread, dict) or not thread.get("id"):
                continue
            folder = str(thread.get("cwd") or self.config.cwd)
            title = thread.get("name") or thread.get("preview") or thread["id"]
            label = " ".join(str(title).split())[:60]
            if folder != self.config.cwd:
                label = f"{label} ({Path(folder).name})"
            rows.append({"key": str(thread["id"]), "label": label})
            self._listed[str(thread["id"])] = folder
        return rows

    async def resume_session(self, session_key: str) -> None:
        if self._lock.locked():
            raise RuntimeError("Finish or cancel the running turn first")
        if session_key not in self._listed:
            raise RuntimeError("Only a thread JOAT just listed can be resumed")
        await self._request("thread/resume", {"threadId": session_key, "excludeTurns": True})
        self._thread_id = session_key
        self._cwd = self._listed[session_key]
        self._generation = (self._generation or 0) + 1

    async def reset_conversation(self) -> None:
        if self._lock.locked():
            raise RuntimeError("Finish or cancel the running turn first")
        self._thread_id = None
        self._cwd = self.config.cwd
        self._generation = (self._generation or 0) + 1

    async def close(self) -> None:
        self._deny_pending()
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except Exception:
                proc.kill()
        if self._reader is not None:
            self._reader.cancel()


def build_codex_lanes(
    configs: Sequence[CodexTerminalLaneConfig],
    *,
    binary: Callable[[], str | None],
    env_provider: Callable[[], Mapping[str, str | None]],
) -> dict[str, CodexTerminalAgent]:
    return {c.name: CodexTerminalAgent(c, binary=binary, env_provider=env_provider) for c in configs}
