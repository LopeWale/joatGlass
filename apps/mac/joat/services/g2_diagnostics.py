"""Opt-in, redacted real-G2 diagnostic receipt storage.

The plugin sends only a fixed diagnostic vocabulary and protocol.py rejects
anything outside it. This store adds receipt time/order and atomically writes
at most 512 records with owner-only permissions. It is intentionally separate
from application logs: brain.log can contain operational context, while this
file is safe for the Mac shell's support export.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


TRACE_SCHEMA = "joat.g2-diagnostic.v1"
MAX_EVENTS = 512


def default_g2_diagnostics_path() -> Path:
    return Path.home() / "Library/Application Support/JOAT/g2-diagnostics.json"


@dataclass(slots=True)
class G2DiagnosticStore:
    """Append in memory and atomically replace one content-free support trace."""

    path: Path = field(default_factory=default_g2_diagnostics_path)
    _events: list[dict[str, Any]] = field(default_factory=list, init=False)

    def record(self, event: dict[str, str | int]) -> None:
        """Persist a protocol-sanitized event. Callers cannot inject raw data."""

        # protocol.py has already whitelisted every event field. Copy it so a
        # caller cannot mutate a stored record after this method returns.
        record = {"sequence": len(self._events) + 1, "recordedAtMs": int(time.time() * 1000)}
        record.update(event)
        self._events.append(record)
        if len(self._events) > MAX_EVENTS:
            self._events = self._events[-MAX_EVENTS:]
            for index, retained in enumerate(self._events, start=1):
                retained["sequence"] = index
        self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = json.dumps(
            {"schema": TRACE_SCHEMA, "events": self._events},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
