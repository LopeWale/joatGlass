"""Antigravity sessions in the Agent Terminal, run by JOAT on this Mac.

Google's Antigravity SDK (`google-antigravity`, Python) runs the same agent
runtime as the Antigravity app and `agy` CLI. JOAT hosts the agent itself:
streamed text and tool calls reach the lenses, every shell command waits for
"terminal approve / deny", a running turn can be cancelled, and the
conversation continues across turns until "new conversation".

The conversation survives a JOAT restart. The SDK saves each conversation to
a folder JOAT owns, `~/Library/Application Support/JOAT/antigravity/<lane>/`,
and the lane remembers its conversation id there, so the next turn after a
restart resumes the same session. `terminal history` reads that saved
session, not JOAT's screen tail.

The agent's app data (skills, rules, plugins, MCP servers) lives in that
same folder, NOT in `~/.gemini/antigravity`: the Antigravity app's own
plugins and hooks do not load into the glasses agent, and nothing this lane
does touches the app's data.

The SDK authenticates with a Gemini API key (metered by Google), not the
Antigravity app's sign-in, so this lane runs only when the wearer has put a
key on the Mac: `GEMINI_API_KEY` in JOAT's environment, or the file
`~/Library/Application Support/JOAT/gemini-api-key` (mode 0600). Store it
with `python -m joat.agents.antigravity_terminal set-key`, which reads the
key without echoing it. Nothing is hard-coded. The SDK has no session
listing, so sessions running in the Antigravity app are not discoverable or
resumable from here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import stat
import time
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any

from .claude_code_terminal import ClaudeTerminalUnavailable, _RosterPolicy, summarize_tool

log = logging.getLogger(__name__)

JOAT_DIR = Path.home() / "Library" / "Application Support" / "JOAT"
CONFIG_PATH = JOAT_DIR / "antigravity-terminal.json"
KEY_PATH = JOAT_DIR / "gemini-api-key"
STATE_ROOT = JOAT_DIR / "antigravity"
_NO_KEY = "Add a Gemini API key on the Mac for Antigravity, then retry."
_NO_SDK = "Update JOAT on this Mac: the Antigravity SDK is missing."
_REOPEN_FAILED = "(The last Antigravity conversation could not be reopened; this is a new one.)"
_BUSY = "(Gemini is busy; retrying…)"
#: Backoff from 2 s doubling: 2+4+8+16+32 ≈ 1 minute before the turn fails.
API_RETRIES = 5
#: How long a turn may sit on a busy model with no new output before it stops.
BUSY_GIVE_UP_S = 90.0
_RETRYABLE = re.compile(r"\b(429|503|500|502|504)\b|UNAVAILABLE|RESOURCE_EXHAUSTED|high demand|retryable", re.I)

#: What the agent is told about where its words end up. The lenses are a
#: 576x288 monochrome panel read while walking; a markdown table or a
#: 60-line code block is unreadable there.
SYSTEM_INSTRUCTIONS = (
    "You are driven by voice from smart glasses. Every reply is read on a "
    "small monochrome display, a few short lines at a time. Answer in brief "
    "plain text: no markdown tables, no headings, and no long code blocks "
    "unless the wearer asks for code. After you change files or run "
    "commands, say in one or two sentences what you did and what is left."
)

# A wearer-readable line for SDK failures, whose raw text is a multi-line
# gRPC dump. Matched against the error text; first match wins.
_FAILURES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"API_KEY_INVALID|API key not valid|API key expired", re.I),
     "Antigravity refused the Gemini API key. Replace it on the Mac, then retry."),
    (re.compile(r"RESOURCE_EXHAUSTED|quota|\b429\b", re.I),
     "The Gemini API quota is used up. Try again later."),
    (re.compile(r"PERMISSION_DENIED|\b403\b", re.I),
     "The Gemini API key is not allowed to use this model."),
    (re.compile(r"UNAVAILABLE|\b503\b|DEADLINE_EXCEEDED|timed out", re.I),
     "The Gemini API did not answer. Try again in a moment."),
)


def wearer_error(exc: BaseException) -> str:
    """One short line for the lenses; the full text goes to the log."""
    text = str(exc) or exc.__class__.__name__
    for pattern, line in _FAILURES:
        if pattern.search(text):
            return line
    first = " ".join(text.split())
    return "Antigravity failed: " + (first if len(first) <= 120 else first[:119] + "…")


def gemini_api_key(path: Path = KEY_PATH, env: Mapping[str, str] | None = None) -> str | None:
    """The wearer's key from the environment, else a private file on the Mac."""
    source = os.environ if env is None else env
    value = source.get("GEMINI_API_KEY", "").strip()
    if value:
        return value
    try:
        info = path.stat()
    except OSError:
        return None
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        log.warning("%s is readable by others; chmod 600 it before JOAT uses it", path.name)
        return None
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def store_gemini_api_key(key: str, path: Path = KEY_PATH) -> None:
    """Write the key owner-only (0600 file in a 0700 folder), atomically."""
    key = key.strip()
    if not key or any(c.isspace() or ord(c) < 32 for c in key):
        raise ValueError("That does not look like an API key")
    _write_private(path, key + "\n")


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def load_antigravity_lane(path: Path = CONFIG_PATH, *, home: Path | None = None) -> tuple[str, str]:
    """(lane name, folder): "antigravity" in the home folder unless configured.

    File shape: ``{"name": "antigravity", "cwd": "~/Developer/app"}``. The
    folder is the agent's workspace: its file tools are confined to it.
    """
    base = home if home is not None else Path.home()
    if not path.exists():
        return "antigravity", str(base)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path.name} could not be read: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name} must be an object")
    name = " ".join(str(raw.get("name", "antigravity")).casefold().split())
    if not name or not all(c.isalnum() or c == " " for c in name):
        raise ValueError(f"{path.name}: the lane name is letters, digits and spaces")
    cwd = Path(os.path.expanduser(str(raw["cwd"]))) if raw.get("cwd") else base
    if not cwd.is_dir():
        raise ValueError(f"{path.name}: folder does not exist: {cwd}")
    return name, str(cwd)


def load_antigravity_model(path: Path = CONFIG_PATH) -> str | None:
    """The optional ``"model"`` from the lane settings; None = the SDK default."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    model = raw.get("model") if isinstance(raw, dict) else None
    if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,80}", model.strip()):
        return model.strip()
    return None


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


#: Antigravity's tools name their main argument differently from Claude
#: Code's (`CommandLine`, `AbsolutePath`, …); the first one present is the
#: detail the wearer needs. Seen in the simulator: an approval that read
#: `run_command: run_command: {"CommandLine": "ls -d */", "Cwd": "/Users/…`.
_DETAIL_KEYS = (
    "CommandLine", "command_line", "command", "TargetFile", "target_file",
    "AbsolutePath", "absolute_path", "file_path", "path",
    "DirectoryPath", "directory_path", "Url", "url", "Query", "query", "Pattern",
    "pattern", "SearchPath", "search_path", "SearchDirectory", "search_directory",
    "Prompt", "prompt", "description",
)
_DETAIL_CHARS = 160


def tool_detail(args: Mapping[str, Any]) -> str:
    """The one argument that says what a tool call does, flattened."""
    for key in _DETAIL_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            detail = " ".join(value.split())
            return detail if len(detail) <= _DETAIL_CHARS else detail[: _DETAIL_CHARS - 1] + "…"
    return ""


_THOUGHT_BLOCK = re.compile(r"<thought>.*?</thought>\s*", re.S | re.I)
_THOUGHT_TAG = re.compile(r"</?thought>\s*", re.I)
_FILE_LINK = re.compile(r"\[([^\[\]]+)\]\(file://[^)\s]+\)")


def clean_glasses_text(text: str) -> str:
    """Strip model thought tags and file:// markdown links that clutter 576x288 lenses."""
    cleaned = _THOUGHT_BLOCK.sub("", text)
    cleaned = _THOUGHT_TAG.sub("", cleaned)
    return _FILE_LINK.sub(r"\1", cleaned)


def tool_line(name: str, args: Mapping[str, Any]) -> str:
    detail = tool_detail(args)
    return f"{name}: {detail}" if detail else summarize_tool(name, args)


MAX_SAVED_SESSIONS = 50


class AntigravityTerminalAgent:
    """One Agent Terminal lane backed by a JOAT-hosted Antigravity agent."""

    policy = _RosterPolicy(roster=True, allows_approvals=True)
    provider = "antigravity"
    #: The roster labels `terminal history` with this; the rows come from
    #: the saved Antigravity session, which outlives JOAT restarts.
    history_scope = "this Antigravity conversation"

    def __init__(
        self,
        name: str,
        cwd: str,
        *,
        api_key: Callable[[], str | None] = gemini_api_key,
        on_approval: Callable[[str, str, str], None] | None = None,
        sdk: Any = None,
        state_dir: Path | None = None,
        model: str | None = None,
    ) -> None:
        self.display_name = name
        self._default_cwd = cwd
        self._cwd = cwd
        self._model = model
        self._api_key = api_key
        self._on_approval = on_approval
        self._sdk = sdk
        self._state_dir = state_dir if state_dir is not None else STATE_ROOT / "-".join(name.split())
        self._agent: Any = None
        self._stack: contextlib.AsyncExitStack | None = None
        self._response: Any = None
        self._pending: dict[str, asyncio.Future[str]] = {}
        self._listed: dict[str, str] = {}
        self._generation: int | None = None
        self._lock = asyncio.Lock()
        self._conversation_id: str | None = self._load_conversation_id()
        self._reopen_failed = False
        self.cancellation_status = "none"

    def set_approval_callback(self, callback: Callable[[str, str, str], None]) -> None:
        self._on_approval = callback

    @property
    def capture_context_generation(self) -> int | None:
        return self._generation

    @property
    def conversation_id(self) -> str | None:
        return self._conversation_id

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
        await self.close()
        self._default_cwd = str(resolved)
        self._cwd = str(resolved)
        self._generation = (self._generation or 0) + 1

    async def set_model(self, model: str | None) -> None:
        if self._lock.locked():
            raise RuntimeError("Finish or cancel the running turn first")
        cleaned = model.strip() if isinstance(model, str) and model.strip() else None
        if cleaned == self._model:
            return
        await self.close()
        self._model = cleaned
        self._generation = (self._generation or 0) + 1

    # ── saved conversation & sessions index ──────────────────────────────

    @property
    def _state_path(self) -> Path:
        return self._state_dir / "state.json"

    @property
    def _sessions_path(self) -> Path:
        return self._state_dir / "sessions.json"

    def _load_conversation_id(self) -> str | None:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        value = raw.get("conversation_id") if isinstance(raw, dict) else None
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            return value
        return None

    def _save_conversation_id(self, value: str | None) -> None:
        if value == self._conversation_id:
            return
        self._conversation_id = value
        try:
            _write_private(self._state_path, json.dumps({"conversation_id": value}) + "\n")
        except OSError:
            # The turn already happened; only resume-after-restart is lost.
            log.warning("antigravity lane %s could not save its conversation id", self.display_name)

    def _load_sessions_index(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self._sessions_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(raw, list):
            return []
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key) or key in seen:
                continue
            seen.add(key)
            label = str(item.get("label") or key).strip() or key
            cwd = str(item.get("cwd") or self._default_cwd).strip() or self._default_cwd
            updated_raw = item.get("updated_at", 0)
            updated_at = int(updated_raw) if isinstance(updated_raw, int | float) else 0
            entries.append({"key": key, "label": label, "cwd": cwd, "updated_at": updated_at})
        return entries

    def _record_session(self, conv_id: str, prompt: str = "") -> None:
        existing = self._load_sessions_index()
        prompt_title = " ".join(prompt.strip().split())[:60]
        prior = next((entry for entry in existing if entry["key"] == conv_id), None)
        if prior is not None and prior["label"] != conv_id:
            label = str(prior["label"])
        else:
            label = prompt_title or conv_id
        entry = {
            "key": conv_id,
            "label": label,
            "cwd": self._cwd,
            "updated_at": int(time.time()),
        }
        updated = [entry, *(item for item in existing if item["key"] != conv_id)][:MAX_SAVED_SESSIONS]
        try:
            _write_private(self._sessions_path, json.dumps(updated, indent=2) + "\n")
        except OSError:
            log.warning("antigravity lane %s could not save sessions index", self.display_name)

    # ── SDK ──────────────────────────────────────────────────────────────

    def _module(self) -> Any:
        if self._sdk is None:
            try:
                # The SDK ships no type information (no py.typed).
                import google.antigravity as antigravity  # type: ignore[import-untyped]
                from google.antigravity.hooks import policy  # type: ignore[import-untyped]
            except ImportError as exc:
                raise ClaudeTerminalUnavailable(_NO_SDK) from exc
            self._sdk = type("Sdk", (), {"ag": antigravity, "policy": policy})
        return self._sdk

    async def initialize(self) -> None:
        if not self._api_key():
            raise ClaudeTerminalUnavailable(_NO_KEY)
        self._module()
        if self._generation is None:
            self._generation = 0

    def _config(self, sdk: Any, key: str, conversation_id: str | None) -> Any:
        self._state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self._state_dir / "conversations").mkdir(exist_ok=True, mode=0o700)
        (self._state_dir / "app-data").mkdir(exist_ok=True, mode=0o700)
        options: dict[str, Any] = {
            "workspaces": [self._cwd],
            "policies": [sdk.policy.confirm_run_command(handler=self._ask)],
            "api_key": key,
            "system_instructions": SYSTEM_INSTRUCTIONS,
            "save_dir": str(self._state_dir / "conversations"),
            "app_data_dir": str(self._state_dir / "app-data"),
        }
        if self._model:
            options["model"] = self._model
        retry = getattr(sdk.ag.types, "RetryConfig", None)
        api_retry = getattr(sdk.ag.types, "ModelAPIRetryConfig", None)
        if retry is not None and api_retry is not None:
            # A busy model (503) or a rate limit (429) is retried with
            # backoff, about a minute in all, then the turn says why it
            # failed. The SDK default kept a simulator turn on WORKING for
            # over four minutes with nothing on the lenses.
            options["retry_config"] = retry(api_retry=api_retry(
                max_retries=API_RETRIES, initial_sleep_duration_ms=2000, exponential_multiplier=2.0,
            ))
        if conversation_id:
            options["conversation_id"] = conversation_id
            options["session_continuation_mode"] = sdk.ag.types.SessionContinuationMode.RESUME
        return sdk.ag.LocalAgentConfig(**options)

    async def _ensure_agent(self) -> Any:
        if self._agent is not None:
            return self._agent
        key = self._api_key()
        if not key:
            raise ClaudeTerminalUnavailable(_NO_KEY)
        sdk = self._module()
        try:
            await self._start(sdk, key, self._conversation_id)
        except Exception as exc:
            if not self._conversation_id:
                raise
            # The saved session is gone or unreadable: say so once, and
            # start a new conversation rather than leaving a dead lane.
            log.warning("antigravity lane %s could not resume %s: %s",
                        self.display_name, self._conversation_id, exc)
            self._save_conversation_id(None)
            self._reopen_failed = True
            await self._start(sdk, key, None)
        return self._agent

    async def _start(self, sdk: Any, key: str, conversation_id: str | None) -> None:
        stack = contextlib.AsyncExitStack()
        try:
            self._agent = await stack.enter_async_context(
                sdk.ag.Agent(self._config(sdk, key, conversation_id))
            )
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack

    # ── approvals ────────────────────────────────────────────────────────

    async def _ask(self, tool_call: Any, reason: str = "") -> bool:
        approval_id = "ag-" + secrets.token_hex(4)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending[approval_id] = future
        name = _enum_value(getattr(tool_call, "name", "tool"))
        args = getattr(tool_call, "args", None)
        args = args if isinstance(args, dict) else {}
        # The roster prints "<kind>: <summary>", so the summary is the
        # detail alone ("ls -d */"), not the tool name a second time.
        summary = tool_detail(args) or reason or summarize_tool(name, args)
        if self._on_approval is not None:
            self._on_approval(approval_id, name, summary)
        try:
            return (await future) == "approve"
        finally:
            self._pending.pop(approval_id, None)

    async def resolve_approval(self, approval_id: str, kind: str, decision: str) -> str | None:
        future = self._pending.get(approval_id)
        if future is None or future.done():
            raise RuntimeError("That request is no longer waiting")
        future.set_result("approve" if decision == "approve" else "deny")
        return decision if decision in ("approve", "deny") else "deny"

    def _deny_pending(self) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_result("deny")

    # ── turns ────────────────────────────────────────────────────────────

    def stream(self, prompt: str) -> AsyncIterator[str]:
        return self._turn(prompt)

    def stream_captured(self, prompt: str, *, context_generation: int) -> AsyncIterator[str]:
        if context_generation != self._generation:
            raise RuntimeError("The Antigravity conversation changed; open it again")
        return self._turn(prompt)

    async def _turn(self, prompt: str) -> AsyncIterator[str]:
        if self._lock.locked():
            raise RuntimeError("An Antigravity turn is already running in this lane")
        async with self._lock:
            try:
                agent = await self._ensure_agent()
                if self._generation is None:
                    self._generation = 0
            except ClaudeTerminalUnavailable:
                raise
            except Exception as exc:
                log.warning("antigravity lane %s did not start: %s", self.display_name, exc)
                raise RuntimeError(wearer_error(exc)) from None
            if self._reopen_failed:
                self._reopen_failed = False
                yield _REOPEN_FAILED + "\n"
            sdk = self._module()
            self.cancellation_status = "none"
            text_open = False
            produced = False
            system_errors: list[str] = []
            busy_since: float | None = None
            known = {str(getattr(s, "id", "")) for s in self._steps(agent)}
            queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
            workers: list[asyncio.Task[None]] = []
            try:
                response = await agent.chat(prompt)
                self._response = response
                workers = [asyncio.create_task(self._pump(response, queue)),
                           asyncio.create_task(self._watch(agent, known, queue))]
                while True:
                    try:
                        kind, item = await asyncio.wait_for(queue.get(), 1.0)
                    except TimeoutError:
                        if (busy_since is not None
                                and asyncio.get_running_loop().time() - busy_since > BUSY_GIVE_UP_S):
                            # The harness retries a busy model at its own
                            # pace, past our API retry bound (seen: three
                            # 503s in four minutes). Stop the turn here.
                            with contextlib.suppress(Exception):
                                await asyncio.wait_for(response.cancel(), 10)
                            raise RuntimeError(system_errors[-1]) from None
                        continue
                    if kind == "end":
                        break
                    if kind == "error":
                        raise item
                    if kind == "system":
                        # The SDK retries these itself and only logs them;
                        # the lenses would show WORKING with no reason.
                        first = not system_errors
                        system_errors.append(item)
                        if _RETRYABLE.search(item) and busy_since is None:
                            busy_since = asyncio.get_running_loop().time()
                        if first and _RETRYABLE.search(item):
                            prefix = "\n" if text_open else ""
                            text_open = False
                            yield prefix + _BUSY + "\n"
                        continue
                    busy_since = None  # the model answered; the clock restarts
                    chunk = item
                    if isinstance(chunk, sdk.ag.types.Text):
                        cleaned = clean_glasses_text(chunk.text or "")
                        if cleaned:
                            text_open = produced = True
                            yield cleaned
                    elif isinstance(chunk, sdk.ag.types.ToolCall):
                        produced = True
                        prefix = "\n" if text_open else ""
                        text_open = False
                        args = chunk.args if isinstance(chunk.args, dict) else {}
                        yield prefix + "⏺ " + tool_line(_enum_value(chunk.name), args) + "\n"
                if text_open:
                    yield "\n"
                # A step that landed after the watcher's last look.
                system_errors.extend(self._new_system_errors(agent, known))
                if system_errors and not produced:
                    # Retries ran out and the turn ended with nothing said.
                    raise RuntimeError(system_errors[-1])
            except asyncio.CancelledError:
                response = self._response
                try:
                    if response is None:
                        raise RuntimeError("no turn to cancel")
                    await asyncio.wait_for(asyncio.shield(response.cancel()), 10)
                    self.cancellation_status = "confirmed"
                except Exception:
                    self.cancellation_status = "uncertain"
                raise
            except Exception as exc:
                log.warning("antigravity lane %s turn failed: %s", self.display_name, exc)
                # Restart the runtime on the next turn; the saved
                # conversation id brings the same session back.
                self._remember_conversation(agent, prompt)
                await self.close()
                raise RuntimeError(wearer_error(exc)) from None
            finally:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                self._response = None
                self._deny_pending()
            self._remember_conversation(agent, prompt)

    @staticmethod
    def _steps(agent: Any) -> list[Any]:
        try:
            return list(agent.conversation.history)
        except Exception:
            return []

    @staticmethod
    async def _pump(response: Any, queue: asyncio.Queue[tuple[str, Any]]) -> None:
        try:
            async for chunk in response.chunks:
                await queue.put(("chunk", chunk))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await queue.put(("error", exc))
            return
        await queue.put(("end", None))

    async def _watch(self, agent: Any, known: set[str], queue: asyncio.Queue[tuple[str, Any]]) -> None:
        """Report this turn's platform error steps (busy model, rate limit)."""
        while True:
            await asyncio.sleep(0.5)
            for error in self._new_system_errors(agent, known):
                await queue.put(("system", error))

    def _new_system_errors(self, agent: Any, known: set[str]) -> list[str]:
        errors: list[str] = []
        for step in self._steps(agent):
            key = str(getattr(step, "id", ""))
            if key in known:
                continue
            if (_enum_value(getattr(step, "source", "")) == "SYSTEM"
                    and _enum_value(getattr(step, "status", "")) == "ERROR"):
                known.add(key)
                detail = str(getattr(step, "error", "") or getattr(step, "content", "") or "system error")
                code = getattr(step, "http_code", 0)
                errors.append(f"HTTP {code}: {detail}" if code else detail)
        return errors

    def _remember_conversation(self, agent: Any, prompt: str = "") -> None:
        value = getattr(agent, "conversation_id", None)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            self._save_conversation_id(value)
            self._record_session(value, prompt)

    # ── sessions & history ───────────────────────────────────────────────

    async def list_sessions(self, limit: int = 20) -> list[dict[str, str]]:
        """Saved Antigravity sessions recorded by JOAT on this Mac, newest first."""
        entries = self._load_sessions_index()[:max(1, min(limit, MAX_SAVED_SESSIONS))]
        listed: list[dict[str, str]] = []
        self._listed = {}
        default_real = os.path.realpath(self._default_cwd)
        for item in entries:
            key = str(item["key"])
            folder = str(item["cwd"] or self._default_cwd)
            base_label = " ".join(str(item["label"]).split())[:60] or key
            label = base_label
            if os.path.realpath(folder) != default_real:
                label = f"{base_label} ({Path(folder).name})"
            listed.append({"key": key, "label": label, "cwd": folder})
            self._listed[key] = folder
        return listed

    async def resume_session(self, session_key: str) -> None:
        if self._lock.locked():
            raise RuntimeError("Finish or cancel the running turn first")
        folder = self._listed.get(session_key)
        if folder is None:
            for item in self._load_sessions_index():
                if item["key"] == session_key:
                    folder = str(item["cwd"])
                    break
        if folder is None:
            raise RuntimeError("Only a saved Antigravity conversation can be resumed")
        if not Path(folder).is_dir():
            raise RuntimeError(f"That session's folder is gone: {folder}")
        await self.close()
        self._cwd = folder
        self._save_conversation_id(session_key)
        self._reopen_failed = False
        self._generation = (self._generation or 0) + 1

    async def history(self, limit: int = 12) -> list[tuple[str, str]]:
        """The saved session's own turns: what was asked and what it said."""
        if self._agent is None:
            if not self._conversation_id:
                return []
            if self._lock.locked():
                return []
            async with self._lock:
                await self._ensure_agent()
        steps = getattr(getattr(self._agent, "conversation", None), "history", None) or []
        latest: dict[str, Any] = {}
        for index, step in enumerate(steps):
            # A step arrives as several updates under one id; the last one
            # holds the full content.
            latest[str(getattr(step, "id", "") or index)] = step
        rows: list[tuple[str, str]] = []
        for step in latest.values():
            if getattr(step, "depth", 0):
                continue  # a subagent's inner steps, not this conversation
            content = str(getattr(step, "content", "") or "").strip()
            if not content:
                continue
            source = _enum_value(getattr(step, "source", ""))
            if source == "USER":
                rows.append(("user", content))
            elif (source == "MODEL"
                  and _enum_value(getattr(step, "target", "")) == "TARGET_USER"
                  and _enum_value(getattr(step, "status", "")) != "ERROR"):
                cleaned = clean_glasses_text(content).strip()
                if cleaned:
                    rows.append(("agent", cleaned))
        return rows[-max(1, min(limit, 64)):]

    # ── lifecycle ────────────────────────────────────────────────────────

    async def reset_conversation(self) -> None:
        if self._lock.locked():
            raise RuntimeError("Finish or cancel the running turn first")
        await self.close()
        self._cwd = self._default_cwd
        self._save_conversation_id(None)
        self._reopen_failed = False
        self._generation = (self._generation or 0) + 1

    async def close(self) -> None:
        self._deny_pending()
        stack, self._stack, self._agent = self._stack, None, None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                log.debug("antigravity close failed", exc_info=True)


# ── setup from Terminal ─────────────────────────────────────────────────


def _status_lines() -> list[str]:
    import importlib.util

    try:
        installed = importlib.util.find_spec("google.antigravity") is not None
    except (ImportError, ValueError):
        installed = False
    try:
        name, cwd = load_antigravity_lane()
        lane = f"lane '{name}' works in {cwd}"
    except ValueError as exc:
        lane = f"lane settings invalid: {exc}"
    has_key = gemini_api_key() is not None
    return [
        f"Antigravity SDK: {'installed' if installed else 'MISSING (pip install google-antigravity)'}",
        f"Gemini API key: {'present' if has_key else 'missing (run: set-key)'}",
        lane,
    ]


async def _check() -> int:
    name, cwd = load_antigravity_lane()
    agent = AntigravityTerminalAgent(name, cwd, state_dir=STATE_ROOT / "_check")
    try:
        await agent.initialize()
        reply = "".join([c async for c in agent.stream("Reply with exactly: OK")]).strip()
    except Exception as exc:  # the CLI's whole job is to say why
        print(f"check failed: {getattr(exc, 'wearer_reason', None) or exc}")
        return 1
    finally:
        await agent.close()
    print(f"check passed: {reply[:80]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    import getpass
    import sys

    parser = argparse.ArgumentParser(prog="python -m joat.agents.antigravity_terminal",
                                     description="Set up the Agent Terminal's Antigravity lane.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("set-key", help="store a Gemini API key on this Mac (input hidden)")
    sub.add_parser("status", help="show whether the lane can run (never prints the key)")
    sub.add_parser("check", help="send one tiny request with the stored key (metered)")
    args = parser.parse_args(argv)
    if args.command == "set-key":
        key = getpass.getpass("Gemini API key (hidden): ") if sys.stdin.isatty() else sys.stdin.readline()
        try:
            store_gemini_api_key(key)
        except ValueError as exc:
            print(exc)
            return 1
        print(f"Saved to {KEY_PATH} (owner-only). Restart JOAT to use it.")
        return 0
    if args.command == "status":
        print("\n".join(_status_lines()))
        return 0
    return asyncio.run(_check())


if __name__ == "__main__":
    raise SystemExit(main())
