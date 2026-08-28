"""Sanitized OpenClaw / Agent Terminal dashboard projections."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from joat.services.status import StatusPublisher


def _snapshot(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text())
    assert isinstance(value, dict)
    return value


def test_status_defaults_do_not_infer_openclaw_readiness(tmp_path: Path) -> None:
    path = tmp_path / "status.json"

    StatusPublisher(path=path)

    snapshot = _snapshot(path)
    assert snapshot["openclaw"] == {
        "state": "not_configured",
        "endpoint_host": "",
        "scopes": [],
        "reason": "endpoint_missing",
    }
    assert snapshot["agent_terminal"] == {
        "state": "unavailable",
        "reason": "gateway_not_ready",
    }
    assert snapshot["brainstorm"] == {
        "state": "unavailable",
        "seat_count": 0,
        "phase": "idle",
    }


def test_openclaw_status_accepts_only_sanitized_fields(tmp_path: Path) -> None:
    path = tmp_path / "status.json"
    status = StatusPublisher(path=path)

    status.set_openclaw(
        state="connected_read_only",
        endpoint_host="ADMINS-MAC-MINI.TAIL3A3363.TS.NET",
        scopes=("operator.read",),
        reason="write_scope_required",
    )
    status.set_agent_terminal(
        state="approval_required",
        reason="write_scope_required",
    )
    status.set_brainstorm(state="ready", seat_count=2, phase="idle")

    snapshot = _snapshot(path)
    assert snapshot["openclaw"] == {
        "state": "connected_read_only",
        "endpoint_host": "admins-mac-mini.tail3a3363.ts.net",
        "scopes": ["operator.read"],
        "reason": "write_scope_required",
    }
    serialized = path.read_text()
    assert "token" not in serialized.lower()
    assert "telegram" not in serialized.lower()
    assert "prompt" not in serialized.lower()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("openclaw", {"token": "secret"}),
        ("agent_terminal", {"prompt": "private"}),
        ("brainstorm", {"transcript": "private"}),
    ],
)
def test_generic_status_set_cannot_bypass_sanitized_projection(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    status = StatusPublisher(path=tmp_path / "status.json")

    with pytest.raises(ValueError, match="dedicated status methods"):
        status.set(**{field: value})


@pytest.mark.parametrize(
    "bad_reason",
    ["paired-device-token", "Telegram is running", "arbitrary remote error"],
)
def test_openclaw_status_rejects_arbitrary_reason_text(
    tmp_path: Path,
    bad_reason: str,
) -> None:
    status = StatusPublisher(path=tmp_path / "status.json")

    with pytest.raises(ValueError, match="reason"):
        status.set_openclaw(
            state="failed",
            endpoint_host="admins-mac-mini.tail3a3363.ts.net",
            scopes=(),
            reason=bad_reason,
        )
