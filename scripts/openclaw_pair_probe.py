#!/usr/bin/env python3
"""Read a content-free OpenClaw readiness proof from the app-owned engine.

This is a one-time Mini setup verification, never a normal glasses control.
The native bridge admits only JOAT.app's exact Python child PID, so an
independent CLI must not try to open it or duplicate the Gateway handshake.
Instead, the app-owned engine writes this small, allowlisted status projection
only after it has authenticated the paired device, opened the dedicated Hermes
session, and checked that its effective tool inventory is empty.
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from typing import Final

DEFAULT_STATUS_PATH = Path(
    "~/Library/Application Support/JOAT/status.json"
).expanduser()
MAX_STATUS_AGE_S: Final = 15.0
REQUIRED_SCOPES: Final = frozenset({"operator.read", "operator.write"})


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read JOAT.app's content-free Agent Terminal readiness proof"
    )
    parser.add_argument(
        "--expected-host",
        required=True,
        help="expected private OpenClaw Gateway hostname on this Mini",
    )
    parser.add_argument(
        "--status",
        type=Path,
        default=DEFAULT_STATUS_PATH,
        help="app-owned JOAT status projection (default: %(default)s)",
    )
    return parser.parse_args()


def _not_ready(reason: str) -> dict[str, str]:
    """Return only fixed reason codes; never echo status-file content."""

    return {"status": "not_ready", "reason": reason}


def _mapping(value: object) -> dict[str, object] | None:
    return value if isinstance(value, dict) else None


def _normalize_host(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.lower().rstrip(".")
    if not normalized or len(normalized) > 253:
        return None
    labels = normalized.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not all(character.isascii() and (character.isalnum() or character == "-")
                   for character in label)
        for label in labels
    ):
        return None
    return normalized


def _probe(*, status_path: Path, expected_host: str, now: float | None = None) -> dict[str, str]:
    """Validate the fixed status projection without touching credentials or payloads."""

    expected = _normalize_host(expected_host)
    if expected is None:
        raise ValueError("expected host is invalid")
    try:
        raw = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return _not_ready("status_unavailable")
    snapshot = _mapping(raw)
    if snapshot is None:
        return _not_ready("status_invalid")

    observed_at = snapshot.get("at")
    current = time.time() if now is None else now
    if (
        isinstance(observed_at, bool)
        or not isinstance(observed_at, (int, float))
        or not math.isfinite(observed_at)
        or observed_at > current
        or current - observed_at >= MAX_STATUS_AGE_S
    ):
        return _not_ready("engine_stale")

    gateway = _mapping(snapshot.get("openclaw"))
    terminal = _mapping(snapshot.get("agent_terminal"))
    if gateway is None or terminal is None:
        return _not_ready("status_invalid")
    if _normalize_host(gateway.get("endpoint_host")) != expected:
        return _not_ready("gateway_host_mismatch")
    if gateway.get("state") != "connected_operator":
        return _not_ready("gateway_not_ready")
    scopes = gateway.get("scopes")
    if not isinstance(scopes, list) or any(not isinstance(scope, str) for scope in scopes):
        return _not_ready("operator_scope_missing")
    if set(scopes) != REQUIRED_SCOPES or len(scopes) != len(REQUIRED_SCOPES):
        return _not_ready("operator_scope_missing")
    if terminal.get("state") != "ready" or terminal.get("reason") != "":
        return _not_ready("terminal_not_ready")

    return {
        "status": "ready",
        "gateway_host": expected,
        "operator_scopes": "verified",
        "terminal_session": "verified",
        "tool_preflight": "verified",
    }


def main() -> int:
    args = _arguments()
    result = _probe(status_path=args.status, expected_host=args.expected_host)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
