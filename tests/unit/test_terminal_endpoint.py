"""Unit tests for Mac /terminal HTTP endpoint (services/terminal_endpoint.py)."""

from __future__ import annotations

import asyncio
import enum
import json
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from joat.agents.antigravity_terminal import AntigravityTerminalAgent, gemini_api_key, store_gemini_api_key
from joat.agents.claude_code_terminal import (
    ClaudeCodeTerminalAgent,
    ClaudeTerminalLaneConfig,
    _RosterPolicy,
)
from joat.agents.cli_accounts import AccountProfile, AccountRegistry
from joat.agents.cli_signin import AccountStatus, LoginSnapshot
from joat.agents.codex_terminal import CodexTerminalAgent, CodexTerminalLaneConfig
from joat.services.agent_terminal import AgentTerminalRoster
from joat.services.models import ModelRegistry, assistant_slot, default_slots
from joat.services.terminal_endpoint import TerminalEndpoint, terminal_token

TOKEN = "test-terminal-bearer-token"
RAW_SECRET_KEY = "test-gemini-secret-never-leak-999"


class FakeSignIn:
    def __init__(self, registry: AccountRegistry) -> None:
        self._registry = registry
        self.current: LoginSnapshot | None = None
        self.started: list[str] = []
        self.submitted_codes: list[str] = []
        self.cancelled = 0

    def binary(self, provider: str) -> str | None:
        return f"/usr/local/bin/{provider}"

    async def status(self, profile: AccountProfile) -> AccountStatus:
        return AccountStatus("logged_in", email=f"{profile.profile_id}@example.com", plan="Max")

    async def sign_out(self, profile: AccountProfile) -> bool:
        return True

    def start(self, profile: AccountProfile) -> LoginSnapshot:
        self.started.append(f"{profile.provider}:{profile.profile_id}")
        self.current = LoginSnapshot(
            provider=profile.provider,
            profile_id=profile.profile_id,
            stage="enter_code" if profile.provider == "claude" else "open_link",
            url="https://example.com/login",
            code=None if profile.provider == "claude" else "ABCD-1234",
            needs_code=profile.provider == "claude",
            message="Open the link to sign in.",
        )
        return self.current

    def submit_code(self, code: str) -> bool:
        if self.current is None or not self.current.needs_code:
            return False
        self.submitted_codes.append(code)
        self.current = LoginSnapshot(
            provider=self.current.provider,
            profile_id=self.current.profile_id,
            stage="done",
            message="Signed in.",
        )
        return True

    def cancel(self, *, publish: bool = True) -> None:
        self.cancelled += 1
        if self.current is not None and self.current.stage not in ("done", "failed", "cancelled"):
            self.current = LoginSnapshot(
                provider=self.current.provider,
                profile_id=self.current.profile_id,
                stage="cancelled",
                message="Sign-in cancelled.",
            )

    def dismiss(self) -> None:
        self.current = None


async def _call(
    endpoint: TerminalEndpoint,
    method: str,
    target: str,
    body: dict[str, Any] | None = None,
    token: str | None = TOKEN,
) -> tuple[int, dict[str, Any]]:
    reader, writer = await asyncio.open_connection("127.0.0.1", endpoint.port)
    payload = json.dumps(body).encode("utf-8") if body is not None else b""
    lines = [f"{method} {target} HTTP/1.1", "Host: 127.0.0.1", f"Content-Length: {len(payload)}"]
    if token is not None:
        lines.append(f"Authorization: Bearer {token}")
    if body is not None:
        lines.append("Content-Type: application/json")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + payload)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    await writer.wait_closed()
    head, _, rest = raw.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(rest or b"{}")


class _FakeClaudeSdk:
    def __init__(self) -> None:
        self.sessions: list[Any] = []
        self.messages: dict[str, list[Any]] = {}

    def list_sessions(self, _directory: str | None = None, limit: int = 20) -> list[Any]:
        return self.sessions[:limit]

    def get_session_messages(self, session_id: str, _cwd: str | None = None) -> list[Any]:
        return list(self.messages.get(session_id, []))


class _FakeAntigravityMode(str, enum.Enum):
    RESUME = "resume"


class _FakeAntigravitySdk:
    def __init__(self) -> None:
        sdk = self
        self.history: list[Any] = []
        self.conversation_id = "ag-default-conv"

        class _Agent:
            def __init__(self, config: dict[str, Any]) -> None:
                conv_id = config.get("conversation_id")
                if isinstance(conv_id, str) and conv_id:
                    sdk.conversation_id = conv_id
                self.conversation = SimpleNamespace(history=sdk.history)

            async def __aenter__(self) -> "_Agent":
                return self

            async def __aexit__(self, *_exc: Any) -> None:
                return None

            @property
            def conversation_id(self) -> str:
                return sdk.conversation_id

        def _plain(**kwargs: Any) -> dict[str, Any]:
            return kwargs

        self.ag = SimpleNamespace(
            LocalAgentConfig=_plain,
            Agent=_Agent,
            types=SimpleNamespace(
                Text=type("Text", (), {}),
                ToolCall=type("ToolCall", (), {}),
                SessionContinuationMode=_FakeAntigravityMode,
                RetryConfig=_plain,
                ModelAPIRetryConfig=_plain,
            ),
        )
        self.policy = SimpleNamespace(confirm_run_command=lambda handler: ["confirm"])


async def _offline_codex_spawn(*_args: Any, **_kwargs: Any) -> Any:
    raise FileNotFoundError("codex binary not used in unit tests")


@pytest.fixture
async def terminal_service(tmp_path: Path) -> AsyncIterator[tuple[TerminalEndpoint, Path, Path, Path, FakeSignIn]]:
    work_a = tmp_path / "work_a"
    work_b = tmp_path / "work_b"
    work_a.mkdir()
    work_b.mkdir()
    joat_dir = tmp_path / "JOAT"
    joat_dir.mkdir(mode=0o700)
    key_path = joat_dir / "gemini-api-key"
    store_gemini_api_key(RAW_SECRET_KEY, key_path)

    claude_sdk = _FakeClaudeSdk()
    claude = ClaudeCodeTerminalAgent(
        ClaudeTerminalLaneConfig("claude", str(work_a)),
        cli_path=lambda: "/usr/local/bin/claude",
        sdk=claude_sdk,
    )
    codex = CodexTerminalAgent(
        CodexTerminalLaneConfig("codex", str(work_a)),
        binary=lambda: "/usr/local/bin/codex",
        spawn=_offline_codex_spawn,
    )
    ag_sdk = _FakeAntigravitySdk()
    antigravity = AntigravityTerminalAgent(
        "antigravity",
        str(work_a),
        api_key=lambda: gemini_api_key(key_path, env={}),
        sdk=ag_sdk,
        state_dir=joat_dir / "antigravity" / "antigravity",
    )
    roster = AgentTerminalRoster(
        [("claude", claude), ("codex", codex), ("antigravity", antigravity)],
        visible_lanes=("claude", "codex", "antigravity"),
    )
    registry = AccountRegistry(root=joat_dir / "accounts")
    signin = FakeSignIn(registry)
    models = ModelRegistry([*default_slots(), assistant_slot("claude")])

    endpoint = TerminalEndpoint(
        roster=roster,
        models=models,
        registry=registry,
        signin=signin,  # type: ignore[arg-type]
        token=TOKEN,
        port=0,
        config_dir=joat_dir,
        gemini_key_path=key_path,
        antigravity_installed=lambda: True,
    )
    await endpoint.probe_all()
    await endpoint.start()
    try:
        yield endpoint, joat_dir, work_a, work_b, signin
    finally:
        await antigravity.close()
        await endpoint.stop()


def test_terminal_token_mints_private_file_and_respects_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JOAT_TERMINAL_TOKEN", raising=False)
    path = tmp_path / "JOAT" / "terminal-token"
    minted = terminal_token(path)
    assert minted
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert terminal_token(path) == minted
    monkeypatch.setenv("JOAT_TERMINAL_TOKEN", "override-token")
    assert terminal_token(path) == "override-token"


async def test_health_is_public_and_state_requires_bearer_auth(terminal_service: Any) -> None:
    endpoint, *_ = terminal_service
    status, body = await _call(endpoint, "GET", "/health", token=None)
    assert status == 200 and body["ok"] is True

    # Also works behind Tailscale Serve /terminal prefix
    status, body = await _call(endpoint, "GET", "/terminal/health", token=None)
    assert status == 200 and body["ok"] is True

    assert (await _call(endpoint, "GET", "/v1/state", token=None))[0] == 401
    assert (await _call(endpoint, "GET", "/v1/state", token="wrong-token"))[0] == 401
    assert (await _call(endpoint, "POST", "/v1/config", {"action": "refresh"}, token=None))[0] == 401


async def test_state_never_exposes_raw_gemini_api_key(terminal_service: Any) -> None:
    endpoint, *_ = terminal_service
    status, body = await _call(endpoint, "GET", "/terminal/v1/state")
    assert status == 200
    serialized = json.dumps(body)
    assert RAW_SECRET_KEY not in serialized

    agents = {a["provider"]: a for a in body["agents"]}
    assert set(agents) == {"claude", "codex", "antigravity"}
    assert agents["antigravity"]["loggedIn"] is True
    assert "API key" in agents["antigravity"]["statusDetail"]
    assert body["assistantSeat"] == "claude"
    assert "claude" in body["assistantOptions"]


async def test_config_updates_model_cwd_gemini_key_session_and_accounts(terminal_service: Any) -> None:
    endpoint, joat_dir, _work_a, work_b, signin = terminal_service

    # 1. set_model persists and updates state
    status, body = await _call(
        endpoint, "POST", "/v1/config",
        {"action": "set_model", "provider": "antigravity", "model": "gemini-2.5-pro"},
    )
    assert status == 200
    ag_item = next(a for a in body["agents"] if a["provider"] == "antigravity")
    assert ag_item["currentModel"] == "gemini-2.5-pro"
    saved_ag = json.loads((joat_dir / "antigravity-terminal.json").read_text())
    assert saved_ag["model"] == "gemini-2.5-pro"

    # 2. set_cwd validates directory and updates config + live lane
    bad_status, _ = await _call(
        endpoint, "POST", "/v1/config",
        {"action": "set_cwd", "provider": "claude", "cwd": str(joat_dir / "does-not-exist")},
    )
    assert bad_status == 400

    status, body = await _call(
        endpoint, "POST", "/v1/config",
        {"action": "set_cwd", "provider": "claude", "cwd": str(work_b)},
    )
    assert status == 200
    claude_item = next(a for a in body["agents"] if a["provider"] == "claude")
    assert claude_item["cwd"] == str(work_b)

    # 3. set_gemini_key writes 0600 key without echoing it back
    new_secret = "test-gemini-rotated-never-echoed-12345"
    status, body = await _call(
        endpoint, "POST", "/v1/config",
        {"action": "set_gemini_key", "key": new_secret},
    )
    assert status == 200
    assert new_secret not in json.dumps(body)
    assert gemini_api_key(joat_dir / "gemini-api-key", env={}) == new_secret

    # 4. set_assistant_seat switches voice assistant seat
    status, body = await _call(
        endpoint, "POST", "/v1/config",
        {"action": "set_assistant_seat", "seat": "codex"},
    )
    assert status == 200
    assert body["assistantSeat"] == "codex"

    # 5. new_session resets lane conversation
    status, body = await _call(
        endpoint, "POST", "/v1/config",
        {"action": "new_session", "lane": "claude"},
    )
    assert status == 200

    # 6. account.add + login.code + account.select + account.remove
    status, body = await _call(
        endpoint, "POST", "/v1/config",
        {"action": "account.add", "provider": "claude", "label": "Work Max"},
    )
    assert status == 200
    assert body["login"]["stage"] == "enter_code"
    assert body["login"]["needsCode"] is True

    oauth_code = "oauth-one-time-code-xyz"
    status, body = await _call(
        endpoint, "POST", "/v1/config",
        {"action": "login.code", "code": oauth_code},
    )
    assert status == 200
    assert oauth_code not in json.dumps(body)
    assert signin.submitted_codes == [oauth_code]
    assert body["login"]["stage"] == "done"

    claude_group = next(g for g in body["accounts"] if g["provider"] == "claude")
    added = next(p for p in claude_group["profiles"] if p["label"] == "Work Max")
    status, body = await _call(
        endpoint, "POST", "/v1/config",
        {"action": "account.select", "provider": "claude", "profileId": added["profileId"]},
    )
    assert status == 200
    claude_group = next(g for g in body["accounts"] if g["provider"] == "claude")
    assert claude_group["activeProfileId"] == added["profileId"]

    status, body = await _call(
        endpoint, "POST", "/v1/config",
        {"action": "account.remove", "provider": "claude", "profileId": added["profileId"]},
    )
    assert status == 200
    claude_group = next(g for g in body["accounts"] if g["provider"] == "claude")
    assert all(p["profileId"] != added["profileId"] for p in claude_group["profiles"])


class _ScriptedTerminalAgent:
    """Controllable terminal agent double for live instruction, cancel, approval, and session tests."""

    policy = _RosterPolicy()

    def __init__(self, provider: str, cwd: str) -> None:
        self.provider = provider
        self.cwd = cwd
        self.model: str | None = None
        self.prompts: list[str] = []
        self.resets = 0
        self.chunks: list[str] = ["All unit tests passed.\n"]
        self.gate: asyncio.Event | None = None
        self.cancellation_status = "none"
        self.resolved_approvals: list[tuple[str, str, str]] = []
        self._approval_cb: Any = None
        self._pending: dict[str, asyncio.Future[str]] = {}
        self.trigger_approval: tuple[str, str, str] | None = None
        self.sessions: list[dict[str, Any]] = []
        self.session_histories: dict[str, list[tuple[str, str]]] = {}
        self.session_cwds: dict[str, str] = {}
        self.resumed_sessions: list[str] = []
        self.list_sessions_error: Exception | None = None

    def set_approval_callback(self, callback: Any) -> None:
        self._approval_cb = callback

    async def reset_conversation(self) -> None:
        self.resets += 1

    async def list_sessions(self, limit: int = 20) -> list[dict[str, str]]:
        if self.list_sessions_error is not None:
            raise self.list_sessions_error
        return list(self.sessions[:limit])

    async def resume_session(self, session_key: str) -> None:
        valid_keys = {
            str(item.get("key") or "").strip()
            for item in self.sessions
            if isinstance(item, dict) and str(item.get("key") or "").strip()
        }
        if session_key not in valid_keys:
            raise RuntimeError("session not found")
        self.resumed_sessions.append(session_key)
        if session_key in self.session_cwds:
            self.cwd = self.session_cwds[session_key]

    async def history(self, limit: int = 10) -> list[tuple[str, str]]:
        if not self.resumed_sessions:
            return []
        latest = self.resumed_sessions[-1]
        return list(self.session_histories.get(latest, []))[-limit:]

    async def resolve_approval(self, approval_id: str, kind: str, decision: str) -> str | None:
        self.resolved_approvals.append((approval_id, kind, decision))
        fut = self._pending.pop(approval_id, None)
        if fut is not None and not fut.done():
            fut.set_result(decision)
        return decision

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        self.prompts.append(prompt)
        self.cancellation_status = "none"
        try:
            if self.trigger_approval is not None:
                appr_id, kind, summary = self.trigger_approval
                fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
                self._pending[appr_id] = fut
                if self._approval_cb is not None:
                    self._approval_cb(appr_id, kind, summary)
                verdict = await fut
                yield f"Approval verdict: {verdict}\n"
            for chunk in self.chunks:
                yield chunk
            if self.gate is not None:
                await self.gate.wait()
                yield "Finished long job.\n"
        except asyncio.CancelledError:
            self.cancellation_status = "confirmed"
            raise


@pytest.fixture
async def controllable_terminal(
    tmp_path: Path,
) -> AsyncIterator[tuple[TerminalEndpoint, AgentTerminalRoster, dict[str, _ScriptedTerminalAgent], Path]]:
    work = tmp_path / "work"
    work.mkdir()
    joat_dir = tmp_path / "JOAT"
    joat_dir.mkdir(mode=0o700)
    key_path = joat_dir / "gemini-api-key"
    store_gemini_api_key(RAW_SECRET_KEY, key_path)

    claude = _ScriptedTerminalAgent("claude-code", str(work))
    codex = _ScriptedTerminalAgent("codex", str(work))
    antigravity = _ScriptedTerminalAgent("antigravity", str(work))
    roster = AgentTerminalRoster(
        [("claude", claude), ("codex", codex), ("antigravity", antigravity)],
        visible_lanes=("claude", "codex", "antigravity"),
    )
    for lane_name, agent in (("claude", claude), ("codex", codex), ("antigravity", antigravity)):
        agent.set_approval_callback(
            lambda appr_id, kind, summary, ln=lane_name: roster.note_approval(ln, appr_id, kind, summary)
        )
    registry = AccountRegistry(root=joat_dir / "accounts")
    signin = FakeSignIn(registry)
    models = ModelRegistry([*default_slots(), assistant_slot("claude")])
    endpoint = TerminalEndpoint(
        roster=roster,
        models=models,
        registry=registry,
        signin=signin,  # type: ignore[arg-type]
        token=TOKEN,
        port=0,
        config_dir=joat_dir,
        gemini_key_path=key_path,
        antigravity_installed=lambda: True,
    )
    await endpoint.probe_all()
    await endpoint.start()
    try:
        yield endpoint, roster, {"claude": claude, "codex": codex, "antigravity": antigravity}, key_path
    finally:
        for name in roster.names():
            lane = roster.lane(name)
            if lane is not None and lane.task is not None and not lane.task.done():
                lane.task.cancel()
                await asyncio.gather(lane.task, return_exceptions=True)
        await endpoint.stop()


async def test_state_exposes_recent_messages_and_pending_approval_items(
    controllable_terminal: Any,
) -> None:
    endpoint, roster, _agents, _key_path = controllable_terminal
    claude_lane = roster.lane("claude")
    assert claude_lane is not None
    for idx in range(8):
        role = "user" if idx % 2 == 0 else "assistant"
        text = f"turn-{idx}-" + ("x" * 700)
        claude_lane.conversation.append(role, text)  # type: ignore[arg-type]

    # Codex has empty conversation.messages -> falls back to tail lines
    codex_lane = roster.lane("codex")
    assert codex_lane is not None
    codex_lane.tail.extend(["> run linter", "[status] checking", "0 warnings found"])

    roster.note_approval("claude", "cc-42", "Bash", "Bash: " + ("rm -rf dist/" * 30))

    status, body = await _call(endpoint, "GET", "/v1/state")
    assert status == 200
    by_prov = {a["provider"]: a for a in body["agents"]}

    claude_msgs = by_prov["claude"]["recentMessages"]
    assert len(claude_msgs) == 6  # capped at 6 newest (turn-3..turn-7 + approval status)
    assert claude_msgs[0]["text"].startswith("turn-3-")
    assert claude_msgs[-2]["role"] == "assistant"
    assert len(claude_msgs[-2]["text"]) == 600  # truncated at 600 chars
    assert claude_msgs[-1]["role"] == "status"

    approvals = by_prov["claude"]["pendingApprovalItems"]
    assert len(approvals) == 1
    assert approvals[0]["id"] == "cc-42"
    assert approvals[0]["kind"] == "Bash"
    assert len(approvals[0]["summary"]) == 240

    codex_msgs = by_prov["codex"]["recentMessages"]
    assert codex_msgs == [
        {"role": "user", "text": "run linter"},
        {"role": "status", "text": "[status] checking"},
        {"role": "assistant", "text": "0 warnings found"},
    ]


async def test_send_instruction_returns_promptly_supports_new_session_and_rejects_invalid_or_busy(
    controllable_terminal: Any,
) -> None:
    endpoint, roster, agents, key_path = controllable_terminal

    # Invalid inputs -> 400
    for bad_payload in (
        {"action": "send_instruction", "provider": "claude", "instruction": "   "},
        {"action": "send_instruction", "provider": "claude"},
        {"action": "send_instruction", "provider": "claude", "instruction": "x" * 4097},
        {"action": "send_instruction", "provider": "unknown-agent", "instruction": "hi"},
        {"action": "send_instruction", "provider": "claude", "instruction": "hi", "newSession": "yes"},
    ):
        bad_status, _ = await _call(endpoint, "POST", "/v1/config", bad_payload)
        assert bad_status == 400

    # Gated turn on Claude returns 200 immediately without waiting for completion
    gate = asyncio.Event()
    agents["claude"].gate = gate
    agents["claude"].chunks = ["Inspecting repository...\n"]

    status, body = await asyncio.wait_for(
        _call(
            endpoint, "POST", "/v1/config",
            {"action": "send_instruction", "provider": "claude-code", "instruction": "  check git status  "},
        ),
        timeout=1.0,
    )
    assert status == 200
    assert body["notice"] == "Sent instruction to Claude."
    assert roster.focused == "claude"
    claude_item = next(a for a in body["agents"] if a["provider"] == "claude")
    assert claude_item["busy"] is True
    assert claude_item["recentMessages"][0] == {"role": "user", "text": "check git status"}

    # Sending again while busy -> 409 Conflict (does not overwrite or queue)
    busy_status, busy_body = await _call(
        endpoint, "POST", "/v1/config",
        {"action": "send_instruction", "provider": "claude", "instruction": "second instruction"},
    )
    assert busy_status == 409
    assert busy_body["ok"] is False

    # Let the turn finish and confirm assistant reply appears in recentMessages
    gate.set()
    task = roster.lane("claude").task  # type: ignore[union-attr]
    assert task is not None
    await task
    await asyncio.sleep(0)

    _, settled = await _call(endpoint, "GET", "/v1/state")
    claude_settled = next(a for a in settled["agents"] if a["provider"] == "claude")
    assert claude_settled["busy"] is False
    assert any(
        m["role"] == "assistant" and "Finished long job." in m["text"]
        for m in claude_settled["recentMessages"]
    )

    # newSession=True resets the lane conversation first before submitting
    agents["claude"].gate = None
    agents["claude"].chunks = ["Fresh session answer.\n"]
    status, reset_body = await _call(
        endpoint, "POST", "/v1/config",
        {
            "action": "send_instruction",
            "provider": "claude",
            "instruction": "start clean",
            "newSession": True,
        },
    )
    assert status == 200
    assert agents["claude"].resets == 1
    new_task = roster.lane("claude").task  # type: ignore[union-attr]
    if new_task is not None:
        await new_task
    await asyncio.sleep(0)
    _, after_new = await _call(endpoint, "GET", "/v1/state")
    claude_after = next(a for a in after_new["agents"] if a["provider"] == "claude")
    texts = [m["text"] for m in claude_after["recentMessages"]]
    assert "check git status" not in " ".join(texts)
    assert any("Fresh session answer." in t for t in texts)

    # Unavailable Antigravity (missing Gemini key) -> 409
    key_path.unlink()
    unavail_status, _ = await _call(
        endpoint, "POST", "/v1/config",
        {"action": "send_instruction", "provider": "antigravity", "instruction": "hello"},
    )
    assert unavail_status == 409


async def test_cancel_run_and_decide_approval_approve_and_deny(controllable_terminal: Any) -> None:
    endpoint, roster, agents, _key_path = controllable_terminal

    # 1. Start a long-running Codex job, while focusing Claude, then cancel_run without provider
    gate = asyncio.Event()
    agents["codex"].gate = gate
    await _call(
        endpoint, "POST", "/v1/config",
        {"action": "send_instruction", "provider": "codex", "instruction": "long refactor"},
    )
    roster.focus("claude")

    status, cancelled = await _call(endpoint, "POST", "/v1/config", {"action": "cancel_run"})
    assert status == 200
    assert "Codex" in (cancelled["notice"] or "")
    codex_item = next(a for a in cancelled["agents"] if a["provider"] == "codex")
    assert codex_item["busy"] is False
    assert agents["codex"].cancellation_status == "confirmed"

    # Cancelling again when idle returns 200 with clear idle notice
    idle_status, idle_body = await _call(
        endpoint, "POST", "/v1/config", {"action": "cancel_run", "provider": "codex"},
    )
    assert idle_status == 200
    assert "nothing running" in (idle_body["notice"] or "")

    # 2. decide_approval: invalid decision -> 400; nothing pending -> 409
    assert (await _call(
        endpoint, "POST", "/v1/config", {"action": "decide_approval", "decision": "maybe"},
    ))[0] == 400
    assert (await _call(
        endpoint, "POST", "/v1/config", {"action": "decide_approval", "decision": "approve", "provider": "claude"},
    ))[0] == 409

    # 3. decide_approval: approve & deny live tool requests
    for decision in ("approve", "deny"):
        agents["antigravity"].trigger_approval = (f"ag-{decision}", "run_command", "pytest -q")
        agents["antigravity"].chunks = []
        send_status, _ = await _call(
            endpoint, "POST", "/v1/config",
            {"action": "send_instruction", "provider": "antigravity", "instruction": f"test {decision}"},
        )
        assert send_status == 200
        for _ in range(50):
            if roster.lane("antigravity").approvals:  # type: ignore[union-attr]
                break
            await asyncio.sleep(0.01)

        _, state_waiting = await _call(endpoint, "GET", "/v1/state")
        ag_waiting = next(a for a in state_waiting["agents"] if a["provider"] == "antigravity")
        assert ag_waiting["pendingApprovalItems"] == [
            {"id": f"ag-{decision}", "kind": "run_command", "summary": "pytest -q"},
        ]

        # Blocked on approval -> send_instruction returns 409
        blocked_status, _ = await _call(
            endpoint, "POST", "/v1/config",
            {"action": "send_instruction", "provider": "antigravity", "instruction": "interrupt"},
        )
        assert blocked_status == 409

        # Resolve approval (omit provider on 'deny' to test automatic waiting-lane selection!)
        roster.focus("claude")
        payload: dict[str, Any] = {"action": "decide_approval", "decision": decision}
        if decision == "approve":
            payload["provider"] = "antigravity"
        dec_status, dec_body = await _call(endpoint, "POST", "/v1/config", payload)
        assert dec_status == 200
        assert f"recorded {decision}" in (dec_body["notice"] or "")
        ag_after = next(a for a in dec_body["agents"] if a["provider"] == "antigravity")
        assert ag_after["pendingApprovalItems"] == []

        ag_task = roster.lane("antigravity").task  # type: ignore[union-attr]
        if ag_task is not None:
            await ag_task
        await asyncio.sleep(0)
        assert agents["antigravity"].resolved_approvals[-1] == (f"ag-{decision}", "run_command", decision)


async def test_state_exposes_lane_sessions_capped_at_twelve_and_falls_back_on_error(
    controllable_terminal: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint, roster, agents, _key_path = controllable_terminal

    # Populate Claude with invalid/blank entries, missing-label entry, and >12 valid sessions
    agents["claude"].sessions = [
        {"key": "   ", "label": "Blank key ignored"},
        {"key": "sess-fallback-label", "label": ""},
        *[
            {"key": f"sess-{idx:02d}", "label": f"Session {idx:02d}"}
            for idx in range(1, 16)
        ],
    ]
    # Codex listing raises an error -> gracefully falls back to []
    agents["codex"].list_sessions_error = RuntimeError("codex daemon unavailable")

    status, body = await _call(endpoint, "GET", "/terminal/v1/state")
    assert status == 200
    by_prov = {a["provider"]: a for a in body["agents"]}

    # Note: AgentTerminalRoster.list_lane_sessions slices rows[:12] before filtering blank keys,
    # and TerminalEndpoint._lane_sessions also caps valid non-empty items at 12 and falls back label->key.
    claude_sessions = by_prov["claude"]["sessions"]
    assert 1 <= len(claude_sessions) <= 12
    assert claude_sessions[0] == {"key": "sess-fallback-label", "label": "sess-fallback-label"}
    assert claude_sessions[1] == {"key": "sess-01", "label": "Session 01"}
    assert all(item["key"].strip() and item["label"].strip() for item in claude_sessions)
    assert by_prov["codex"]["sessions"] == []
    assert by_prov["antigravity"]["sessions"] == []

    # Direct roster.list_lane_sessions returning >12 mixed items + raising on one lane is capped at 12 valid items
    async def fake_list_lane_sessions(lane_name: str, limit: int = 16) -> list[dict[str, str]]:
        assert limit == 12
        if lane_name == "codex":
            raise OSError("unexpected I/O error")
        if lane_name == "antigravity":
            return []
        return [
            {"key": "", "label": "skip-empty"},
            {"key": "bad\x00null", "label": "skip-null"},
            {"key": "no-label-key", "label": ""},
            *[{"key": f"k-{i}", "label": f"Label {i}"} for i in range(1, 16)],
        ]

    monkeypatch.setattr(roster, "list_lane_sessions", fake_list_lane_sessions)
    status2, body2 = await _call(endpoint, "GET", "/v1/state")
    assert status2 == 200
    by_prov2 = {a["provider"]: a for a in body2["agents"]}
    assert len(by_prov2["claude"]["sessions"]) == 12
    assert by_prov2["claude"]["sessions"][0] == {"key": "no-label-key", "label": "no-label-key"}
    assert by_prov2["claude"]["sessions"][-1] == {"key": "k-11", "label": "Label 11"}
    assert by_prov2["codex"]["sessions"] == []
    assert by_prov2["antigravity"]["sessions"] == []


async def test_resume_session_success_hydrates_messages_and_supports_camel_and_snake_case(
    controllable_terminal: Any, tmp_path: Path,
) -> None:
    endpoint, roster, agents, _key_path = controllable_terminal
    alt_cwd = tmp_path / "alt_project"
    alt_cwd.mkdir()

    agents["claude"].sessions = [
        {"key": "sess-alpha", "label": "Fix cursor pagination"},
        {"key": "sess-beta", "label": "Add telemetry counters"},
    ]
    agents["claude"].session_cwds["sess-alpha"] = str(alt_cwd)
    agents["claude"].session_histories["sess-alpha"] = [
        ("user", "Fix cursor pagination"),
        ("assistant", "Updated page token handling."),
    ]
    agents["claude"].session_histories["sess-beta"] = [
        ("user", "Add telemetry counters"),
        ("assistant", "Counters wired to metrics sink."),
    ]

    # 1. GET /v1/state primes labels and exposes both sessions
    state_status, state_body = await _call(endpoint, "GET", "/v1/state")
    assert state_status == 200
    claude_before = next(a for a in state_body["agents"] if a["provider"] == "claude")
    assert claude_before["sessions"] == [
        {"key": "sess-alpha", "label": "Fix cursor pagination"},
        {"key": "sess-beta", "label": "Add telemetry counters"},
    ]

    # 2. Resume with provider + sessionKey (camelCase, trimmed)
    roster.focus("codex")
    status, resumed_body = await _call(
        endpoint,
        "POST",
        "/terminal/v1/config",
        {"action": "resume_session", "provider": "claude-code", "sessionKey": "  sess-alpha  "},
    )
    assert status == 200
    assert resumed_body["ok"] is True
    assert resumed_body["notice"] == "Resumed claude session."
    assert roster.focused == "claude"
    assert agents["claude"].resumed_sessions == ["sess-alpha"]

    claude_after = next(a for a in resumed_body["agents"] if a["provider"] == "claude")
    assert claude_after["cwd"] == str(alt_cwd)
    assert claude_after["activeSessionTitle"] == "Fix cursor pagination"
    assert claude_after["recentMessages"] == [
        {"role": "status", "text": "Resumed Fix cursor pagination"},
        {"role": "user", "text": "Fix cursor pagination"},
        {"role": "assistant", "text": "Updated page token handling."},
    ]

    # 3. Resume with lane + session_key (snake_case)
    status2, resumed_body2 = await _call(
        endpoint,
        "POST",
        "/v1/config",
        {"action": "resume_session", "lane": "claude", "session_key": "sess-beta"},
    )
    assert status2 == 200
    assert resumed_body2["notice"] == "Resumed claude session."
    claude_after2 = next(a for a in resumed_body2["agents"] if a["provider"] == "claude")
    assert claude_after2["recentMessages"] == [
        {"role": "status", "text": "Resumed Add telemetry counters"},
        {"role": "user", "text": "Add telemetry counters"},
        {"role": "assistant", "text": "Counters wired to metrics sink."},
    ]

    # 4. Follow-up instruction on the resumed session succeeds seamlessly
    send_status, _ = await _call(
        endpoint,
        "POST",
        "/v1/config",
        {"action": "send_instruction", "provider": "claude", "instruction": "run unit suite"},
    )
    assert send_status == 200
    task = roster.lane("claude").task  # type: ignore[union-attr]
    if task is not None:
        await task


async def test_resume_session_returns_400_404_and_409_appropriately(
    controllable_terminal: Any,
) -> None:
    endpoint, roster, agents, _key_path = controllable_terminal
    agents["claude"].sessions = [{"key": "sess-valid", "label": "Valid session"}]

    # 1. Invalid payloads -> 400 Bad Request
    for bad_payload in (
        {"action": "resume_session", "sessionKey": "sess-valid"},
        {"action": "resume_session", "provider": "", "sessionKey": "sess-valid"},
        {"action": "resume_session", "provider": "unknown-lane", "sessionKey": "sess-valid"},
        {"action": "resume_session", "provider": 42, "sessionKey": "sess-valid"},
        {"action": "resume_session", "provider": "claude"},
        {"action": "resume_session", "provider": "claude", "sessionKey": "   "},
        {"action": "resume_session", "provider": "claude", "sessionKey": "x" * 257},
        {"action": "resume_session", "provider": "claude", "sessionKey": "sess\x00bad"},
        {"action": "resume_session", "provider": "claude", "sessionKey": True},
    ):
        bad_status, bad_body = await _call(endpoint, "POST", "/v1/config", bad_payload)
        assert bad_status == 400, f"expected 400 for {bad_payload}, got {bad_status}"
        assert bad_body["ok"] is False

    # 2. Exactly 256-char key passes validation and reaches lookup -> 404 Not Found
    not_found_256, nf_body_256 = await _call(
        endpoint,
        "POST",
        "/v1/config",
        {"action": "resume_session", "provider": "claude", "sessionKey": "k" * 256},
    )
    assert not_found_256 == 404
    assert nf_body_256 == {"ok": False, "error": "session not found or could not be resumed"}

    # Missing session key -> 404 Not Found
    missing_status, missing_body = await _call(
        endpoint,
        "POST",
        "/v1/config",
        {"action": "resume_session", "provider": "claude", "sessionKey": "sess-missing"},
    )
    assert missing_status == 404
    assert missing_body == {"ok": False, "error": "session not found or could not be resumed"}

    # 3. Busy lane -> 409 Conflict ("finish or cancel the running turn first")
    gate = asyncio.Event()
    agents["claude"].gate = gate
    send_status, _ = await _call(
        endpoint,
        "POST",
        "/v1/config",
        {"action": "send_instruction", "provider": "claude", "instruction": "hold turn open"},
    )
    assert send_status == 200

    busy_status, busy_body = await _call(
        endpoint,
        "POST",
        "/v1/config",
        {"action": "resume_session", "provider": "claude", "sessionKey": "sess-valid"},
    )
    assert busy_status == 409
    assert busy_body == {"ok": False, "error": "finish or cancel the running turn first"}

    # Let running turn complete, then resume succeeds with 200
    gate.set()
    task = roster.lane("claude").task  # type: ignore[union-attr]
    if task is not None:
        await task
    await asyncio.sleep(0)

    ok_status, ok_body = await _call(
        endpoint,
        "POST",
        "/v1/config",
        {"action": "resume_session", "provider": "claude", "sessionKey": "sess-valid"},
    )
    assert ok_status == 200
    assert ok_body["notice"] == "Resumed claude session."


async def test_real_claude_and_antigravity_adapters_list_and_resume_over_http(
    terminal_service: Any,
) -> None:
    endpoint, _joat_dir, work_a, work_b, _signin = terminal_service
    claude_lane = endpoint._roster.lane("claude")
    ag_lane = endpoint._roster.lane("antigravity")
    assert claude_lane is not None and ag_lane is not None
    claude_agent: Any = claude_lane.agent
    ag_agent: Any = ag_lane.agent

    # Seed real ClaudeCodeTerminalAgent's fake SDK and real AntigravityTerminalAgent's sessions.json
    claude_agent._sdk.sessions = [
        SimpleNamespace(
            session_id="cc-session-1",
            custom_title="Audit loopback HTTP routes",
            summary=None,
            first_prompt=None,
            cwd=str(work_b),
        ),
        SimpleNamespace(
            session_id="cc-session-2",
            custom_title=None,
            summary="Clean up Swift models",
            first_prompt=None,
            cwd=str(work_a),
        ),
    ]
    claude_agent._sdk.messages["cc-session-1"] = [
        SimpleNamespace(type="user", message={"content": "Audit loopback HTTP routes"}),
        SimpleNamespace(type="assistant", message={"content": "All endpoints require bearer auth."}),
    ]
    ag_agent._record_session("ag-conv-1", "Implement session picker")
    ag_agent._record_session("ag-conv-2", "Verify simulator flow")

    # Cold resume on Claude BEFORE any GET /v1/state call primes _listed automatically
    cold_status, cold_body = await _call(
        endpoint,
        "POST",
        "/v1/config",
        {"action": "resume_session", "provider": "claude", "sessionKey": "cc-session-1"},
    )
    assert cold_status == 200
    assert cold_body["notice"] == "Resumed claude session."
    claude_item = next(a for a in cold_body["agents"] if a["provider"] == "claude")
    assert claude_item["cwd"] == str(work_b)
    assert [s["key"] for s in claude_item["sessions"]] == ["cc-session-1", "cc-session-2"]
    assert any(
        m["role"] == "assistant" and "bearer auth" in m["text"]
        for m in claude_item["recentMessages"]
    )

    ag_item = next(a for a in cold_body["agents"] if a["provider"] == "antigravity")
    assert ag_item["sessions"] == [
        {"key": "ag-conv-2", "label": "Verify simulator flow"},
        {"key": "ag-conv-1", "label": "Implement session picker"},
    ]

    # Resume saved Antigravity conversation
    ag_status, ag_body = await _call(
        endpoint,
        "POST",
        "/v1/config",
        {"action": "resume_session", "provider": "antigravity", "sessionKey": "ag-conv-1"},
    )
    assert ag_status == 200
    assert ag_body["notice"] == "Resumed antigravity session."
    assert ag_agent.conversation_id == "ag-conv-1"
