"""Contracts for the content-free app-owned OpenClaw readiness proof."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType


def _probe_module() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "openclaw_pair_probe.py"
    spec = importlib.util.spec_from_file_location("openclaw_pair_probe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _status(*, at: float = 100.0) -> dict[str, object]:
    return {
        "at": at,
        "openclaw": {
            "state": "connected_operator",
            "endpoint_host": "admins-mac-mini.tail3a3363.ts.net",
            "scopes": ["operator.read", "operator.write"],
            "reason": "",
        },
        "agent_terminal": {"state": "ready", "reason": ""},
    }


def _write_status(path: Path, status: dict[str, object]) -> None:
    path.write_text(json.dumps(status), encoding="utf-8")


def test_probe_reports_only_content_free_app_owned_readiness(tmp_path: Path) -> None:
    probe = _probe_module()
    status_path = tmp_path / "status.json"
    _write_status(status_path, _status())

    result = probe._probe(
        status_path=status_path,
        expected_host="ADMINS-MAC-MINI.TAIL3A3363.TS.NET.",
        now=110.0,
    )

    assert result == {
        "status": "ready",
        "gateway_host": "admins-mac-mini.tail3a3363.ts.net",
        "operator_scopes": "verified",
        "terminal_session": "verified",
        "tool_preflight": "verified",
    }


def test_probe_refuses_stale_or_wrong_home_brain_status(tmp_path: Path) -> None:
    probe = _probe_module()
    status_path = tmp_path / "status.json"
    _write_status(status_path, _status(at=84.0))

    assert probe._probe(
        status_path=status_path,
        expected_host="admins-mac-mini.tail3a3363.ts.net",
        now=100.0,
    ) == {"status": "not_ready", "reason": "engine_stale"}

    _write_status(status_path, _status())
    assert probe._probe(
        status_path=status_path,
        expected_host="other-mini.tail3a3363.ts.net",
        now=110.0,
    ) == {"status": "not_ready", "reason": "gateway_host_mismatch"}


def test_probe_does_not_echo_secret_bearing_or_untrusted_status_fields(tmp_path: Path) -> None:
    probe = _probe_module()
    status_path = tmp_path / "status.json"
    status = _status()
    gateway = status["openclaw"]
    assert isinstance(gateway, dict)
    gateway.update({"state": "failed", "reason": "token=super-secret", "token": "super-secret"})
    status["last_transcript"] = "private Brainstorm topic"
    status["last_reply"] = "private Hermes response"
    _write_status(status_path, status)

    result = probe._probe(
        status_path=status_path,
        expected_host="admins-mac-mini.tail3a3363.ts.net",
        now=110.0,
    )

    assert result == {"status": "not_ready", "reason": "gateway_not_ready"}
    assert "secret" not in json.dumps(result)
    assert "private" not in json.dumps(result)


def test_probe_requires_operator_scope_and_ready_terminal(tmp_path: Path) -> None:
    probe = _probe_module()
    status_path = tmp_path / "status.json"
    status = _status()
    gateway = status["openclaw"]
    assert isinstance(gateway, dict)
    gateway["scopes"] = ["operator.read"]
    _write_status(status_path, status)

    assert probe._probe(
        status_path=status_path,
        expected_host="admins-mac-mini.tail3a3363.ts.net",
        now=110.0,
    ) == {"status": "not_ready", "reason": "operator_scope_missing"}

    gateway["scopes"] = ["operator.read", "operator.write"]
    terminal = status["agent_terminal"]
    assert isinstance(terminal, dict)
    terminal.update({"state": "blocked", "reason": "tools_enabled"})
    _write_status(status_path, status)

    assert probe._probe(
        status_path=status_path,
        expected_host="admins-mac-mini.tail3a3363.ts.net",
        now=110.0,
    ) == {"status": "not_ready", "reason": "terminal_not_ready"}
