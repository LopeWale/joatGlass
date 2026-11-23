"""Loopback /terminal HTTP endpoint for iPhone companion agent configuration.

Exposes the Mac's Agent Terminal state and configuration over loopback HTTP
(`127.0.0.1:8790` by default) so Tailscale Serve can mount it at `/terminal`:

    tailscale serve --bg --set-path /terminal http://127.0.0.1:8790

Routes (with or without `/terminal` prefix):
- `GET /health` (or `/v1/health`) — unauthenticated liveness check (`{"ok": true}`).
- `GET /v1/state` — bearer-authenticated snapshot of Claude, Codex, Antigravity,
  the voice assistant seat, saved CLI accounts, and in-progress CLI sign-in.
  NEVER exposes raw Gemini API keys, OAuth codes, or bearer tokens.
- `POST /v1/config` — bearer-authenticated mutations (`set_model`, `set_cwd`,
  write-only `set_gemini_key`, `new_session`, `resume_session`,
  `send_instruction`, `cancel_run`, `decide_approval`, `set_assistant_seat`,
  `account.select`/`account.activate`, `account.add`, `account.remove`,
  `login.start`, `login.code`, `login.cancel`, `login.dismiss`, `refresh`).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import importlib.util
import json
import logging
import os
import re
import secrets
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..agents.antigravity_terminal import (
    JOAT_DIR,
    KEY_PATH as DEFAULT_GEMINI_KEY_PATH,
    _write_private,
    gemini_api_key,
    load_antigravity_model,
    store_gemini_api_key,
)
from ..agents.cli_accounts import (
    PROVIDERS,
    PROVIDER_TITLES,
    AccountError,
    AccountProfile,
    AccountRegistry,
    Provider,
)
from ..agents.cli_signin import AccountStatus, CliSignIn, LoginSnapshot
from .agent_endpoint import BadRequest, HttpRequest, read_http_request
from .agent_terminal import MAX_TERMINAL_INSTRUCTION_CHARS, AgentTerminalRoster
from .models import ModelRegistry

log = logging.getLogger(__name__)

DEFAULT_PORT = 8790
TOKEN_PATH = JOAT_DIR / "terminal-token"
MAX_CONFIG_BODY_BYTES = 32 * 1024
MAX_LANE_SESSIONS = 12
MAX_SESSION_KEY_CHARS = 256
MAX_RECENT_MESSAGES = 6
MAX_RECENT_MESSAGE_CHARS = 600
MAX_APPROVAL_SUMMARY_CHARS = 240

_MODEL_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_CLAUDE_MODEL_OPTIONS = ("default", "sonnet", "opus", "fable")
_CODEX_MODEL_OPTIONS = ("default", "gpt-5.6-terra", "gpt-5.3-codex-spark")
_ANTIGRAVITY_MODEL_OPTIONS = (
    "default",
    "gemini-3.8-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
)

_REASONS = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    411: "Length Required",
    413: "Payload Too Large",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


def terminal_token(
    path: Path = TOKEN_PATH,
    env: Mapping[str, str] | None = None,
) -> str:
    """Read `JOAT_TERMINAL_TOKEN` or load/mint an owner-only `0600` bearer token."""
    source = os.environ if env is None else env
    override = source.get("JOAT_TERMINAL_TOKEN", "").strip()
    if override:
        return override
    try:
        info = path.stat()
        if not (info.st_mode & (stat.S_IRWXG | stat.S_IRWXO)):
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
    except OSError:
        pass
    minted = secrets.token_urlsafe(24)
    _write_private(path, minted + "\n")
    return minted


def _default_antigravity_installed() -> bool:
    try:
        return importlib.util.find_spec("google.antigravity") is not None
    except (ImportError, ValueError):
        return False


def _normalize_provider(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if value in ("claude", "claude-code", "claude code"):
        return "claude"
    if value == "codex":
        return "codex"
    if value == "antigravity":
        return "antigravity"
    return value


def _cli_provider(raw: Any) -> Provider:
    normalized = _normalize_provider(raw)
    if normalized == "claude":
        return "claude"
    if normalized == "codex":
        return "codex"
    raise BadRequest(400, "provider must be 'claude' or 'codex'")


class TerminalEndpoint:
    """Loopback HTTP server exposing `/health`, `/v1/state`, and `/v1/config`."""

    def __init__(
        self,
        *,
        roster: AgentTerminalRoster,
        models: ModelRegistry,
        registry: AccountRegistry,
        signin: CliSignIn | None = None,
        token: str | None = None,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        config_dir: Path = JOAT_DIR,
        gemini_key_path: Path = DEFAULT_GEMINI_KEY_PATH,
        antigravity_installed: Callable[[], bool] = _default_antigravity_installed,
    ) -> None:
        self._roster = roster
        self._models = models
        self._registry = registry
        self._signin = signin if signin is not None else CliSignIn(registry, on_change=self._on_signin_change)
        self._token = token if token is not None else terminal_token(config_dir / "terminal-token")
        self._host = host
        self._requested_port = port
        self._config_dir = config_dir
        self._gemini_key_path = gemini_key_path
        self._antigravity_installed = antigravity_installed
        self._server: asyncio.Server | None = None
        self._statuses: dict[tuple[Provider, str], AccountStatus] = {}
        self._probe_tasks: set[asyncio.Task[Any]] = set()
        self._notice: str | None = None

    @property
    def port(self) -> int:
        if self._server is not None and self._server.sockets:
            sockname = self._server.sockets[0].getsockname()
            return int(sockname[1])
        return self._requested_port

    def _on_signin_change(self, snapshot: LoginSnapshot | None) -> None:
        if snapshot is not None and snapshot.stage == "done":
            with contextlib.suppress(RuntimeError):
                loop = asyncio.get_running_loop()
                profile = self._registry.get(snapshot.provider, snapshot.profile_id)
                task = loop.create_task(self.probe(profile))
                self._probe_tasks.add(task)
                task.add_done_callback(self._probe_tasks.discard)

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._handle_client, host=self._host, port=self._requested_port,
        )
        log.info("terminal endpoint listening on http://%s:%d", self._host, self.port)

    async def stop(self) -> None:
        for task in tuple(self._probe_tasks):
            task.cancel()
        if self._probe_tasks:
            await asyncio.gather(*self._probe_tasks, return_exceptions=True)
            self._probe_tasks.clear()
        self._signin.cancel(publish=False)
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()

    async def probe(self, profile: AccountProfile) -> AccountStatus:
        status = await self._signin.status(profile)
        self._statuses[(profile.provider, profile.profile_id)] = status
        return status

    async def probe_all(self) -> None:
        profiles = [p for provider in PROVIDERS for p in self._registry.profiles(provider)]
        results = await asyncio.gather(
            *(self._signin.status(p) for p in profiles), return_exceptions=True,
        )
        for profile, item in zip(profiles, results, strict=False):
            if isinstance(item, AccountStatus):
                self._statuses[(profile.provider, profile.profile_id)] = item
            else:
                self._statuses[(profile.provider, profile.profile_id)] = AccountStatus("unknown")

    # ── HTTP transport ───────────────────────────────────────────────────

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        try:
            try:
                request = await asyncio.wait_for(
                    read_http_request(reader, max_body=MAX_CONFIG_BODY_BYTES),
                    timeout=15.0,
                )
            except TimeoutError:
                await self._write_json(writer, 400, {"ok": False, "error": "request timed out"})
                return
            except BadRequest as exc:
                await self._write_json(writer, exc.status, {"ok": False, "error": exc.message})
                return

            status, payload = await self._dispatch(request)
            await self._write_json(writer, status, payload)
        except Exception:
            log.exception("terminal endpoint unhandled error")
            with contextlib.suppress(Exception):
                await self._write_json(writer, 500, {"ok": False, "error": "internal error"})
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    @staticmethod
    async def _write_json(
        writer: asyncio.StreamWriter, status: int, body: Mapping[str, Any],
    ) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        reason = _REASONS.get(status, "OK")
        header = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            "Cache-Control: no-store\r\n"
            f"Content-Length: {len(encoded)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(header + encoded)
        await writer.drain()

    @staticmethod
    def _route_path(raw_path: str) -> str:
        path = raw_path or "/"
        if path == "/terminal":
            return "/"
        if path.startswith("/terminal/"):
            return path[len("/terminal"):]
        return path

    def _authorized(self, headers: Mapping[str, str]) -> bool:
        auth = headers.get("authorization", "")
        scheme, _, candidate = auth.partition(" ")
        if scheme.lower() != "bearer" or not candidate:
            return False
        return hmac.compare_digest(candidate.strip(), self._token)

    async def _dispatch(self, request: HttpRequest) -> tuple[int, dict[str, Any]]:
        path = self._route_path(request.path)
        if path in ("/health", "/v1/health"):
            if request.method != "GET":
                return 405, {"ok": False, "error": "method not allowed"}
            return 200, {"ok": True}

        if not self._authorized(request.headers):
            return 401, {"ok": False, "error": "unauthorized"}

        if path == "/v1/state":
            if request.method != "GET":
                return 405, {"ok": False, "error": "method not allowed"}
            return 200, await self._state_snapshot()

        if path == "/v1/config":
            if request.method != "POST":
                return 405, {"ok": False, "error": "method not allowed"}
            try:
                body = json.loads(request.body.decode("utf-8") or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                return 400, {"ok": False, "error": "invalid JSON body"}
            if not isinstance(body, dict):
                return 400, {"ok": False, "error": "JSON body must be an object"}
            try:
                notice = await self._apply_config(body)
            except BadRequest as exc:
                return exc.status, {"ok": False, "error": exc.message}
            except AccountError as exc:
                return 400, {"ok": False, "error": str(exc)}
            snapshot = await self._state_snapshot(notice=notice)
            return 200, snapshot

        return 404, {"ok": False, "error": "not found"}

    # ── State projection (secret-free) ───────────────────────────────────

    async def _state_snapshot(self, *, notice: str | None = None) -> dict[str, Any]:
        assistant_slot = self._models.slot("assistant")
        assistant_seat = (
            (assistant_slot.current.id or assistant_slot.current.label)
            if assistant_slot is not None
            else "claude"
        )
        assistant_options = (
            [opt.id or opt.label for opt in assistant_slot.options]
            if assistant_slot is not None
            else ["claude", "codex", "local"]
        )

        lane_entries: list[tuple[str, Any, str]] = []
        for name in self._roster.names():
            lane = self._roster.lane(name)
            if lane is None:
                continue
            raw_provider = getattr(lane.agent, "provider", None) or lane.name
            provider = _normalize_provider(raw_provider)
            if provider not in ("claude", "codex", "antigravity"):
                continue
            lane_entries.append((lane.key, lane, provider))

        sessions_per_lane = await asyncio.gather(
            *(self._lane_sessions(lane_key) for lane_key, _, _ in lane_entries),
        )
        agents: list[dict[str, Any]] = [
            self._agent_item(lane_key, lane, provider, sessions=sessions)
            for (lane_key, lane, provider), sessions in zip(
                lane_entries, sessions_per_lane, strict=False,
            )
        ]

        accounts: list[dict[str, Any]] = []
        for cli_prov in PROVIDERS:
            installed = self._signin.binary(cli_prov) is not None
            active_profile = self._registry.active(cli_prov)
            profiles_out: list[dict[str, Any]] = []
            for profile in self._registry.profiles(cli_prov):
                status = self._statuses.get((cli_prov, profile.profile_id), AccountStatus("unknown"))
                profiles_out.append({
                    "profileId": profile.profile_id,
                    "label": profile.label,
                    "isDefault": profile.is_default,
                    "active": profile.profile_id == active_profile.profile_id,
                    "state": status.state,
                    "email": status.email,
                    "plan": status.plan,
                })
            accounts.append({
                "provider": cli_prov,
                "title": PROVIDER_TITLES[cli_prov],
                "installed": installed,
                "activeProfileId": active_profile.profile_id,
                "profiles": profiles_out,
            })

        login_snapshot = self._signin.current
        login_payload: dict[str, Any] | None = None
        if login_snapshot is not None:
            login_payload = {
                "provider": login_snapshot.provider,
                "profileId": login_snapshot.profile_id,
                "stage": login_snapshot.stage,
                "url": login_snapshot.url,
                "code": login_snapshot.code,
                "needsCode": login_snapshot.needs_code,
                "message": login_snapshot.message,
            }

        effective_notice = notice if notice is not None else self._notice
        return {
            "ok": True,
            "agents": agents,
            "assistantSeat": assistant_seat,
            "assistantOptions": assistant_options,
            "accounts": accounts,
            "login": login_payload,
            "notice": effective_notice,
        }

    async def _lane_sessions(self, lane_key: str) -> list[dict[str, str]]:
        raw_sessions: Any = []
        with contextlib.suppress(Exception):
            raw_sessions = await asyncio.wait_for(
                self._roster.list_lane_sessions(lane_key, limit=MAX_LANE_SESSIONS),
                timeout=3.0,
            )
        if not isinstance(raw_sessions, list):
            return []
        sessions: list[dict[str, str]] = []
        for item in raw_sessions:
            if not isinstance(item, Mapping):
                continue
            raw_key = item.get("key")
            if raw_key is None or isinstance(raw_key, bool):
                continue
            key = str(raw_key).strip()
            if not key or "\x00" in key or len(key) > MAX_SESSION_KEY_CHARS:
                continue
            raw_label = item.get("label")
            label = (
                str(raw_label).strip()
                if raw_label is not None and not isinstance(raw_label, bool)
                else ""
            )
            sessions.append({
                "key": key,
                "label": label or key,
            })
            if len(sessions) >= MAX_LANE_SESSIONS:
                break
        return sessions

    def _agent_item(
        self,
        lane_key: str,
        lane: Any,
        provider: str,
        *,
        sessions: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        cwd = str(getattr(lane.agent, "cwd", None) or Path.home())
        model_val = getattr(lane.agent, "model", None)
        if provider == "antigravity":
            installed = bool(self._antigravity_installed())
            has_key = gemini_api_key(self._gemini_key_path, env=os.environ) is not None
            if not has_key:
                key_probe = getattr(lane.agent, "_api_key", None)
                if callable(key_probe):
                    with contextlib.suppress(Exception):
                        has_key = bool(key_probe())
            logged_in = installed and has_key
            if not installed:
                detail = "Antigravity SDK missing"
            elif has_key:
                detail = "Gemini API key configured"
            else:
                detail = "Gemini API key missing"
            saved_model = load_antigravity_model(self._config_dir / "antigravity-terminal.json")
            current_model = str(model_val or saved_model or "default")
            options = list(_ANTIGRAVITY_MODEL_OPTIONS)
            title = "Antigravity" if lane.name == "antigravity" else lane.name.title()
        else:
            cli_prov: Provider = "claude" if provider == "claude" else "codex"
            installed = self._signin.binary(cli_prov) is not None
            active_profile = self._registry.active(cli_prov)
            acct_status = self._statuses.get((cli_prov, active_profile.profile_id), AccountStatus("unknown"))
            logged_in = installed and acct_status.state == "logged_in"
            if not installed:
                detail = f"Install {PROVIDER_TITLES[cli_prov]} on this Mac"
            elif acct_status.state == "logged_in":
                parts = ["Signed in"]
                if acct_status.email:
                    parts.append(acct_status.email)
                if acct_status.plan:
                    parts.append(acct_status.plan)
                detail = " · ".join(parts)
            elif acct_status.state == "logged_out":
                detail = f"Signed out ({active_profile.label})"
            else:
                detail = active_profile.label
            slot = self._models.slot(cli_prov)
            slot_model = (slot.current.id or "default") if slot is not None else "default"
            current_model = str(model_val or slot_model or "default")
            base_opts = _CLAUDE_MODEL_OPTIONS if cli_prov == "claude" else _CODEX_MODEL_OPTIONS
            options = list(base_opts)
            if slot is not None:
                for opt in slot.options:
                    candidate = opt.id or opt.label
                    if candidate and candidate not in options:
                        options.append(candidate)
            title = PROVIDER_TITLES[cli_prov] if lane.name == cli_prov else lane.name.title()

        if current_model and current_model not in options:
            options.append(current_model)

        return {
            "name": lane_key,
            "title": title,
            "provider": provider,
            "installed": installed,
            "loggedIn": logged_in,
            "statusDetail": detail,
            "cwd": cwd,
            "currentModel": current_model,
            "modelOptions": options,
            "busy": bool(lane.busy),
            "activeSessionTitle": self._roster.lane_active_summary(lane_key),
            "pendingApprovals": len(lane.approvals),
            "approvals": len(lane.approvals),
            "sessions": list(sessions) if sessions is not None else [],
            "recentMessages": self._recent_messages(lane),
            "pendingApprovalItems": self._pending_approval_items(lane),
        }

    @staticmethod
    def _lane_title(lane: Any, provider: str) -> str:
        lane_name = str(getattr(lane, "name", "") or "")
        if provider == "antigravity":
            return "Antigravity" if lane_name == "antigravity" else lane_name.title()
        if provider == "claude":
            return PROVIDER_TITLES["claude"] if lane_name == "claude" else lane_name.title()
        if provider == "codex":
            return PROVIDER_TITLES["codex"] if lane_name == "codex" else lane_name.title()
        return lane_name.title() or provider.title()

    def _recent_messages(self, lane: Any) -> list[dict[str, str]]:
        conv_msgs = list(getattr(getattr(lane, "conversation", None), "messages", ()) or ())
        tail_lines = [str(line) for line in (getattr(lane, "tail", ()) or ()) if str(line).strip()]
        has_non_user_conv = any(getattr(m, "role", "") != "user" for m in conv_msgs)
        use_conv = bool(conv_msgs) and (
            has_non_user_conv
            or getattr(self._roster, "_visible_lanes", None) is not None
            or not any(not line.strip().startswith("> ") for line in tail_lines)
        )
        out: list[dict[str, str]] = []
        if use_conv:
            for msg in conv_msgs:
                text = str(getattr(msg, "text", "") or "").strip()
                if not text:
                    continue
                role = str(getattr(msg, "role", "assistant") or "assistant")
                out.append({"role": role, "text": text[:MAX_RECENT_MESSAGE_CHARS]})
            return out[-MAX_RECENT_MESSAGES:]

        for line in tail_lines:
            stripped = line.strip()
            if stripped.startswith("> "):
                role = "user"
                content = stripped[2:].strip()
            elif stripped.startswith("["):
                role = "status"
                content = stripped
            else:
                role = "assistant"
                content = stripped
            if content:
                out.append({"role": role, "text": content[:MAX_RECENT_MESSAGE_CHARS]})
        return out[-MAX_RECENT_MESSAGES:]

    @staticmethod
    def _pending_approval_items(lane: Any) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        for entry in getattr(lane, "approvals", ()) or ():
            if isinstance(entry, tuple) and len(entry) >= 3:
                approval_id, kind, summary = entry[0], entry[1], entry[2]
            else:
                approval_id = getattr(entry, "approval_id", None)
                if approval_id is None:
                    approval_id = getattr(entry, "id", "")
                kind = getattr(entry, "kind", "")
                summary = getattr(entry, "summary", "")
            items.append({
                "id": str(approval_id),
                "kind": str(kind),
                "summary": str(summary)[:MAX_APPROVAL_SUMMARY_CHARS],
            })
        return items

    def _ensure_lane_available(self, lane: Any, provider: str, title: str) -> None:
        if getattr(lane, "detaching", False):
            raise BadRequest(409, f"{title} is shutting down")
        if getattr(lane, "persistence_error", ""):
            raise BadRequest(409, str(lane.persistence_error))
        prep = getattr(lane, "preparation_status", ("ready", ""))
        if isinstance(prep, tuple) and len(prep) == 2 and prep[0] in ("failed", "approval_required"):
            raise BadRequest(409, str(prep[1] or f"{title} is unavailable"))
        if provider == "antigravity":
            if not self._antigravity_installed():
                raise BadRequest(409, "Antigravity SDK missing on this Mac")
            has_key = gemini_api_key(self._gemini_key_path, env=os.environ) is not None
            if not has_key:
                key_probe = getattr(lane.agent, "_api_key", None)
                if callable(key_probe):
                    with contextlib.suppress(Exception):
                        has_key = bool(key_probe())
            if not has_key:
                raise BadRequest(409, "Gemini API key missing on this Mac")
        elif provider in ("claude", "codex"):
            cli_prov: Provider = "claude" if provider == "claude" else "codex"
            if self._signin.binary(cli_prov) is None:
                raise BadRequest(409, f"Install {PROVIDER_TITLES[cli_prov]} on this Mac")
            cli_probe = getattr(lane.agent, "_cli_path", None) or getattr(lane.agent, "_binary", None)
            if callable(cli_probe) and cli_probe() is None:
                raise BadRequest(409, f"Install {PROVIDER_TITLES[cli_prov]} on this Mac")

    # ── Config mutations ─────────────────────────────────────────────────

    async def _apply_config(self, body: dict[str, Any]) -> str | None:
        action = str(body.get("action") or "").strip()
        if not action:
            raise BadRequest(400, "missing action")

        if action == "refresh":
            if self._signin.current is not None and self._signin.current.stage in ("done", "cancelled"):
                self._signin.dismiss()
            await self.probe_all()
            return "Refreshed."

        if action == "set_model":
            provider = _normalize_provider(body.get("provider") or body.get("lane"))
            if provider not in ("claude", "codex", "antigravity"):
                raise BadRequest(400, "unknown provider")
            raw_model = str(body.get("model") or "").strip()
            if not raw_model:
                raise BadRequest(400, "model is required")
            effective: str | None = None if raw_model.lower() == "default" else raw_model
            if effective is not None and not _MODEL_ID.fullmatch(effective):
                raise BadRequest(400, "invalid model identifier")
            await self._update_lanes_model(provider, effective)
            self._persist_provider_config(provider, model=effective, update_model=True)
            if provider in ("claude", "codex"):
                slot = self._models.slot(provider)
                if slot is not None:
                    choice = "default" if effective is None else effective
                    if slot.find(choice) is not None:
                        await self._models.select(provider, choice)
            shown = effective or "default"
            return f"{provider.title()} model set to {shown}."

        if action == "set_cwd":
            provider = _normalize_provider(body.get("provider") or body.get("lane"))
            if provider not in ("claude", "codex", "antigravity"):
                raise BadRequest(400, "unknown provider")
            raw_cwd = str(body.get("cwd") or "").strip()
            if not raw_cwd or "\x00" in raw_cwd:
                raise BadRequest(400, "cwd is required")
            resolved = Path(os.path.expanduser(raw_cwd))
            if not resolved.is_dir():
                raise BadRequest(400, f"folder does not exist: {resolved}")
            await self._update_lanes_cwd(provider, str(resolved))
            self._persist_provider_config(provider, cwd=str(resolved))
            return f"{provider.title()} working folder updated."

        if action == "set_gemini_key":
            raw_key = body.get("key")
            if not isinstance(raw_key, str):
                raise BadRequest(400, "key is required")
            try:
                store_gemini_api_key(raw_key, self._gemini_key_path)
            except ValueError as exc:
                raise BadRequest(400, str(exc)) from exc
            return "Gemini API key saved."

        if action == "new_session":
            lane_key = self._resolve_lane_key(body.get("lane") or body.get("provider"))
            if lane_key is None:
                raise BadRequest(400, "unknown lane")
            started = await self._roster.start_new_lane_session(lane_key)
            if not started:
                raise BadRequest(400, "finish or cancel the running turn first")
            return f"Started a new {lane_key} session."

        if action == "resume_session":
            raw_target = body.get("provider") or body.get("lane")
            if not isinstance(raw_target, str):
                raise BadRequest(400, "unknown provider or lane")
            lane_key = self._resolve_lane_key(raw_target)
            if lane_key is None:
                raise BadRequest(400, "unknown provider or lane")
            lane = self._roster.lane(lane_key)
            if lane is None:
                raise BadRequest(400, "unknown provider or lane")
            raw_session_key = body.get("sessionKey") or body.get("session_key")
            if raw_session_key is not None and not isinstance(raw_session_key, str):
                raise BadRequest(400, "sessionKey must be a string")
            session_key = str(raw_session_key or "").strip()
            if (
                not session_key
                or len(session_key) > MAX_SESSION_KEY_CHARS
                or "\x00" in session_key
            ):
                raise BadRequest(
                    400, f"sessionKey must be 1-{MAX_SESSION_KEY_CHARS} characters",
                )
            try:
                session_key.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise BadRequest(400, "sessionKey must be valid UTF-8") from exc
            if lane.busy or lane.queued:
                raise BadRequest(409, "finish or cancel the running turn first")
            listed_cache = getattr(lane.agent, "_listed", None)
            if isinstance(listed_cache, dict) and not listed_cache:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        self._roster.list_lane_sessions(lane_key, limit=MAX_LANE_SESSIONS),
                        timeout=3.0,
                    )
            resumed = await self._roster.resume_lane_session(lane_key, session_key)
            if not resumed:
                raise BadRequest(404, "session not found or could not be resumed")
            await self._roster.repaint()
            return f"Resumed {lane_key} session."

        if action == "send_instruction":
            raw_target = body.get("provider") if body.get("provider") is not None else body.get("lane")
            lane_key = self._resolve_lane_key(raw_target)
            if lane_key is None:
                raise BadRequest(400, "unknown provider or lane")
            lane = self._roster.lane(lane_key)
            if lane is None:
                raise BadRequest(400, "unknown provider or lane")
            raw_instruction = body.get("instruction")
            if not isinstance(raw_instruction, str):
                raise BadRequest(400, "instruction is required")
            instruction = raw_instruction.strip()
            if (
                not instruction
                or "\x00" in instruction
                or len(instruction) > MAX_TERMINAL_INSTRUCTION_CHARS
            ):
                raise BadRequest(
                    400, f"instruction must be 1-{MAX_TERMINAL_INSTRUCTION_CHARS} characters",
                )
            try:
                instruction.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise BadRequest(400, "instruction must be valid UTF-8") from exc
            raw_new_session = body.get("newSession", body.get("new_session", False))
            if not isinstance(raw_new_session, bool):
                raise BadRequest(400, "newSession must be a boolean")

            provider = _normalize_provider(getattr(lane.agent, "provider", None) or lane.name)
            title = self._lane_title(lane, provider)
            if lane.approvals:
                raise BadRequest(409, f"{title} is waiting for approval")
            if lane.busy or lane.queued:
                raise BadRequest(409, f"{title} is busy")
            self._ensure_lane_available(lane, provider, title)

            if raw_new_session:
                started = await self._roster.start_new_lane_session(lane_key)
                if not started:
                    raise BadRequest(409, "finish or cancel the running turn first")
                if lane.approvals:
                    raise BadRequest(409, f"{title} is waiting for approval")
                if lane.busy or lane.queued:
                    raise BadRequest(409, f"{title} is busy")

            self._roster.focus(lane_key)
            await self._roster.send(instruction, lane_key)
            await self._roster.repaint()
            return f"Sent instruction to {title}."

        if action == "cancel_run":
            raw_target = body.get("provider") if body.get("provider") is not None else body.get("lane")
            if raw_target is not None and str(raw_target).strip():
                lane_key = self._resolve_lane_key(raw_target)
                if lane_key is None:
                    raise BadRequest(400, "unknown provider or lane")
            else:
                busy_keys = [
                    name for name in self._roster.names()
                    if (ln := self._roster.lane(name)) is not None and (ln.busy or ln.queued)
                ]
                focused = self._roster.focused
                if focused and focused in busy_keys:
                    lane_key = focused
                elif len(busy_keys) == 1:
                    lane_key = busy_keys[0]
                elif len(busy_keys) > 1:
                    raise BadRequest(409, "multiple agents are running; specify provider")
                else:
                    lane_key = focused or (self._roster.names()[0] if self._roster.names() else None)
                if lane_key is None:
                    raise BadRequest(400, "no terminal session configured")

            lane = self._roster.lane(lane_key)
            provider = _normalize_provider(getattr(lane.agent, "provider", None) or lane.name) if lane else ""
            title = self._lane_title(lane, provider) if lane else lane_key
            was_active = bool(lane and (lane.busy or lane.queued))
            receipt = await self._roster.cancel(lane_key)
            await self._roster.repaint()
            if was_active:
                detail = receipt.split("▸", 1)[1].strip() if "▸" in receipt else receipt
                return f"Cancelled {title}: {detail}"
            return f"{title} has nothing running."

        if action == "decide_approval":
            raw_decision = str(body.get("decision") or "").strip().lower()
            if raw_decision not in ("approve", "deny"):
                raise BadRequest(400, "decision must be 'approve' or 'deny'")
            raw_target = body.get("provider") if body.get("provider") is not None else body.get("lane")
            if raw_target is not None and str(raw_target).strip():
                lane_key = self._resolve_lane_key(raw_target)
                if lane_key is None:
                    raise BadRequest(400, "unknown provider or lane")
            else:
                waiting_keys = [
                    name for name in self._roster.names()
                    if (ln := self._roster.lane(name)) is not None and ln.approvals
                ]
                focused = self._roster.focused
                if focused and focused in waiting_keys:
                    lane_key = focused
                elif len(waiting_keys) == 1:
                    lane_key = waiting_keys[0]
                elif len(waiting_keys) > 1:
                    raise BadRequest(409, "multiple agents are waiting for approval; specify provider")
                else:
                    lane_key = focused or (self._roster.names()[0] if self._roster.names() else None)
                if lane_key is None:
                    raise BadRequest(400, "no terminal session configured")

            lane = self._roster.lane(lane_key)
            if lane is None or not lane.approvals:
                raise BadRequest(409, f"{lane_key}: nothing waiting for approval")

            index = 1
            raw_approval_id = str(body.get("approvalId") or body.get("id") or "").strip()
            if raw_approval_id:
                matched_idx = next(
                    (
                        idx + 1
                        for idx, entry in enumerate(lane.approvals)
                        if (
                            entry[0]
                            if isinstance(entry, tuple)
                            else getattr(entry, "approval_id", getattr(entry, "id", ""))
                        ) == raw_approval_id
                    ),
                    None,
                )
                if matched_idx is None:
                    raise BadRequest(404, "approval request is no longer pending")
                index = matched_idx
            elif "index" in body and body["index"] is not None:
                if not isinstance(body["index"], int) or isinstance(body["index"], bool) or body["index"] < 1:
                    raise BadRequest(400, "index must be a positive integer")
                index = body["index"]

            self._roster.focus(lane_key)
            approval_receipt = await self._roster.decide_receipt(raw_decision, index)
            if approval_receipt.outcome in ("nothing_pending", "ambiguous", "failed", "unsupported", "no_session"):
                raise BadRequest(409, approval_receipt.sentence)
            await self._roster.repaint()
            return approval_receipt.sentence

        if action == "set_assistant_seat":
            seat = str(body.get("seat") or "").strip().lower()
            slot = self._models.slot("assistant")
            if not seat or slot is None or slot.find(seat) is None:
                raise BadRequest(400, "unknown assistant seat")
            receipt = await self._models.select("assistant", seat)
            return receipt

        if action in ("account.select", "account.activate"):
            cli_prov = _cli_provider(body.get("provider"))
            profile_id = str(body.get("profileId") or body.get("profile_id") or "").strip()
            if not profile_id:
                raise BadRequest(400, "profileId is required")
            profile = self._registry.activate(cli_prov, profile_id)
            await self.probe(profile)
            return f"Switched {PROVIDER_TITLES[cli_prov]} to {profile.label}."

        if action == "account.add":
            cli_prov = _cli_provider(body.get("provider"))
            label = str(body.get("label") or "").strip()
            profile = self._registry.add(cli_prov, label)
            self._statuses[(cli_prov, profile.profile_id)] = AccountStatus("logged_out")
            self._signin.start(profile)
            return f"Added {profile.label}."

        if action == "account.remove":
            cli_prov = _cli_provider(body.get("provider"))
            profile_id = str(body.get("profileId") or body.get("profile_id") or "").strip()
            if not profile_id:
                raise BadRequest(400, "profileId is required")
            profile = self._registry.get(cli_prov, profile_id)
            current_login = self._signin.current
            if (
                current_login is not None
                and current_login.provider == cli_prov
                and current_login.profile_id == profile_id
            ):
                self._signin.cancel(publish=False)
                self._signin.dismiss()
            with contextlib.suppress(Exception):
                await self._signin.sign_out(profile)
            self._registry.remove(cli_prov, profile_id)
            self._statuses.pop((cli_prov, profile_id), None)
            return f"Removed {profile.label}."

        if action == "login.start":
            cli_prov = _cli_provider(body.get("provider"))
            profile_id = str(body.get("profileId") or body.get("profile_id") or "").strip()
            profile = (
                self._registry.get(cli_prov, profile_id)
                if profile_id
                else self._registry.active(cli_prov)
            )
            self._signin.start(profile)
            return f"Started {PROVIDER_TITLES[cli_prov]} sign-in."

        if action == "login.code":
            code = body.get("code")
            if not isinstance(code, str) or not code.strip():
                raise BadRequest(400, "code is required")
            if not self._signin.submit_code(code):
                raise BadRequest(400, "no sign-in is waiting for a code")
            if self._signin.current is not None and self._signin.current.stage == "done":
                await self.probe_all()
            return "Submitted sign-in code."

        if action == "login.cancel":
            if (
                self._signin.current is not None
                and self._signin.current.stage in ("done", "failed", "cancelled")
            ):
                self._signin.dismiss()
                return None
            self._signin.cancel()
            return "Sign-in cancelled."

        if action == "login.dismiss":
            self._signin.dismiss()
            return None

        raise BadRequest(400, f"unknown action: {action}")

    def _resolve_lane_key(self, raw: Any) -> str | None:
        wanted = str(raw or "").strip().lower()
        if not wanted:
            return None
        if self._roster.lane(wanted) is not None:
            return wanted
        norm_provider = _normalize_provider(wanted)
        for name in self._roster.names():
            lane = self._roster.lane(name)
            if lane is None:
                continue
            lane_prov = _normalize_provider(getattr(lane.agent, "provider", None) or lane.name)
            if lane_prov == norm_provider:
                return name
        return None

    async def _update_lanes_model(self, provider: str, model: str | None) -> None:
        matched = False
        for name in self._roster.names():
            lane = self._roster.lane(name)
            if lane is None or _normalize_provider(getattr(lane.agent, "provider", None)) != provider:
                continue
            matched = True
            setter = getattr(lane.agent, "set_model", None)
            if setter is not None:
                try:
                    await setter(model)
                except RuntimeError as exc:
                    raise BadRequest(400, str(exc)) from exc
        if not matched:
            log.debug("no live %s lane on roster during set_model", provider)

    async def _update_lanes_cwd(self, provider: str, cwd: str) -> None:
        for name in self._roster.names():
            lane = self._roster.lane(name)
            if lane is None or _normalize_provider(getattr(lane.agent, "provider", None)) != provider:
                continue
            setter = getattr(lane.agent, "set_cwd", None)
            if setter is not None:
                try:
                    await setter(cwd)
                except (RuntimeError, ValueError) as exc:
                    raise BadRequest(400, str(exc)) from exc

    def _persist_provider_config(
        self,
        provider: str,
        *,
        cwd: str | None = None,
        model: str | None = None,
        update_model: bool = False,
    ) -> None:
        if provider == "antigravity":
            path = self._config_dir / "antigravity-terminal.json"
            existing = self._read_json_dict(path)
            existing.setdefault("name", "antigravity")
            if cwd is not None:
                existing["cwd"] = cwd
            elif "cwd" not in existing:
                lane = self._roster.lane("antigravity")
                existing["cwd"] = str(getattr(lane.agent, "cwd", Path.home())) if lane else str(Path.home())
            if update_model:
                if model is None:
                    existing.pop("model", None)
                else:
                    existing["model"] = model
            _write_private(path, json.dumps(existing, indent=2) + "\n")
            return

        filename = "claude-terminal.json" if provider == "claude" else "codex-terminal.json"
        path = self._config_dir / filename
        existing = self._read_json_dict(path)
        lanes = existing.get("lanes")
        if not isinstance(lanes, list) or not lanes:
            lane = self._roster.lane(provider)
            default_cwd = cwd or (str(getattr(lane.agent, "cwd", Path.home())) if lane else str(Path.home()))
            entry: dict[str, Any] = {"name": provider, "cwd": default_cwd}
            if provider == "claude":
                entry["permission_mode"] = "default"
            else:
                entry["sandbox"] = "workspace-write"
            lanes = [entry]
        for item in lanes:
            if not isinstance(item, dict):
                continue
            if cwd is not None:
                item["cwd"] = cwd
            if update_model:
                if model is None:
                    item.pop("model", None)
                else:
                    item["model"] = model
        _write_private(path, json.dumps({"lanes": lanes}, indent=2) + "\n")

    @staticmethod
    def _read_json_dict(path: Path) -> dict[str, Any]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}
