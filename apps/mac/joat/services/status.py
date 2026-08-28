"""Engine → shell status channel.

The Mac app needs to render what the assistant is DOING (glasses link, mic,
notification channel, last exchange) — not a log tail. The engine publishes
a small JSON snapshot; the app polls it.

A file, not a socket: it needs no second port, survives either side
restarting independently, and a stale file is self-evidently stale via its
`at` timestamp. Writes are atomic (tmp + replace) so the reader can never
observe a half-written snapshot.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path(
    "~/Library/Application Support/JOAT/status.json"
).expanduser()

_RESERVED_SAFE_PROJECTIONS = frozenset({"openclaw", "agent_terminal", "brainstorm"})
_OPENCLAW_STATES = frozenset({
    "not_configured",
    "checking",
    "approval_required",
    "connected_read_only",
    "connected_operator",
    "disconnected",
    "failed",
})
_AGENT_TERMINAL_STATES = frozenset({
    "unavailable",
    "approval_required",
    "ready",
    "running",
    "blocked",
})
_BRAINSTORM_STATES = frozenset({
    "unavailable", "ready", "running", "settled", "cancelled", "failed",
})
_BRAINSTORM_PHASES = frozenset({"idle", "opening", "turn", "closing"})
_SAFE_REASONS = frozenset({
    "",
    "endpoint_missing",
    "checking_pairing",
    "device_approval_required",
    "write_scope_required",
    "gateway_not_ready",
    "dedicated_session_not_proven",
    "tools_enabled",
    "history_recovery_required",
    "connection_closed",
    "protocol_rejected",
    "native_credentials_unavailable",
})
_HOST_PATTERN = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$"
)


class StatusPublisher:
    """Mutable snapshot of engine state, flushed on every change."""

    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self._path = path
        self._state: dict[str, Any] = {
            "at": time.time(),
            "glasses": "disconnected",
            "device": "",
            "mic": "off",
            "notifications": "off",
            "stt": "off",
            "llm": "off",
            "debate": "off",
            "openclaw": {
                "state": "not_configured",
                "endpoint_host": "",
                "scopes": [],
                "reason": "endpoint_missing",
            },
            "agent_terminal": {
                "state": "unavailable",
                "reason": "gateway_not_ready",
            },
            "brainstorm": {
                "state": "unavailable",
                "seat_count": 0,
                "phase": "idle",
            },
            "last_transcript": "",
            "last_reply": "",
            "last_card": "",
            # "session" and counts.debates were dead schema — written into
            # every snapshot, decoded by the shell, displayed by nothing,
            # and updated by nothing.
            "counts": {"utterances": 0, "cards": 0},
        }
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._path.parent, 0o700)
        self.flush()

    def get(self, key: str, default: str = "") -> str:
        """Read one snapshot field (the sysmenu 'repeat' action needs
        last_reply back)."""
        value = self._state.get(key, default)
        return value if isinstance(value, str) else default

    def set(self, **fields: Any) -> None:
        if _RESERVED_SAFE_PROJECTIONS.intersection(fields):
            raise ValueError("OpenClaw fields require dedicated status methods")
        self._state.update(fields)
        self.flush()

    def set_openclaw(
        self,
        *,
        state: str,
        endpoint_host: str,
        scopes: Sequence[str],
        reason: str,
    ) -> None:
        """Publish connection proof without accepting secret-bearing text."""

        normalized_host = endpoint_host.lower().rstrip(".")
        safe_scopes = list(scopes)
        if state not in _OPENCLAW_STATES:
            raise ValueError("invalid OpenClaw state")
        if normalized_host and not _HOST_PATTERN.fullmatch(normalized_host):
            raise ValueError("invalid OpenClaw endpoint host")
        if (
            len(set(safe_scopes)) != len(safe_scopes)
            or any(scope not in {"operator.read", "operator.write"} for scope in safe_scopes)
        ):
            raise ValueError("invalid OpenClaw scopes")
        if reason not in _SAFE_REASONS:
            raise ValueError("invalid OpenClaw reason")
        self._state["openclaw"] = {
            "state": state,
            "endpoint_host": normalized_host,
            "scopes": safe_scopes,
            "reason": reason,
        }
        self.flush()

    def set_agent_terminal(self, *, state: str, reason: str) -> None:
        """Publish capability state using fixed, non-sensitive reason codes."""

        if state not in _AGENT_TERMINAL_STATES:
            raise ValueError("invalid Agent Terminal state")
        if reason not in _SAFE_REASONS:
            raise ValueError("invalid Agent Terminal reason")
        self._state["agent_terminal"] = {"state": state, "reason": reason}
        self.flush()

    def set_brainstorm(self, *, state: str, seat_count: int, phase: str) -> None:
        """Publish lifecycle metadata, never prompt or transcript content."""

        if state not in _BRAINSTORM_STATES:
            raise ValueError("invalid Brainstorm state")
        if isinstance(seat_count, bool) or not 0 <= seat_count <= 16:
            raise ValueError("invalid Brainstorm seat count")
        if phase not in _BRAINSTORM_PHASES:
            raise ValueError("invalid Brainstorm phase")
        self._state["brainstorm"] = {
            "state": state,
            "seat_count": seat_count,
            "phase": phase,
        }
        self.flush()

    def bump(self, counter: str) -> None:
        counts = dict(self._state["counts"])
        counts[counter] = counts.get(counter, 0) + 1
        self._state["counts"] = counts
        self.flush()

    async def heartbeat(self, interval_s: float = 5.0) -> None:
        """Re-stamp the snapshot on a timer.

        set()/bump() fire only on EVENTS, but the shell declares a snapshot
        stale after 15s and blanks every field — so a healthy idle engine
        looked identical to a dead one within 15 seconds of the last
        utterance, while the (ungated) counters kept ticking. Interval must
        stay well under that window.
        """
        while True:
            await asyncio.sleep(interval_s)
            self.flush()

    def flush(self) -> None:
        self._state["at"] = time.time()
        try:
            fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(self._state, fh)
            os.replace(tmp, self._path)  # atomic for the reader
            os.chmod(self._path, 0o600)
        except OSError:
            pass  # status is best-effort; never take the engine down for it
