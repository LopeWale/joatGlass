"""Opt-in, redacted real-G2 diagnostic receipt storage.

The plugin sends only a fixed diagnostic vocabulary and protocol.py rejects
anything outside it. This store adds receipt time/order and atomically writes
at most 512 records with owner-only permissions. It is intentionally separate
from application logs: brain.log can contain operational context, while this
file is safe for the Mac shell's support export.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


TRACE_SCHEMA = "joat.g2-diagnostic.v2"
MAX_EVENTS = 512
log = logging.getLogger(__name__)

WriteReceipt = Callable[[Path, list[dict[str, Any]]], None]
ObserveReceipt = Callable[[list[dict[str, Any]]], None]


def default_g2_diagnostics_path() -> Path:
    return Path.home() / "Library/Application Support/JOAT/g2-diagnostics.json"


def write_receipt(path: Path, events: list[dict[str, Any]]) -> None:
    """Atomically publish one already-redacted support receipt."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps(
        {"schema": TRACE_SCHEMA, "events": events},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@dataclass(slots=True)
class G2DiagnosticStore:
    """Bounded, content-free support storage that cannot block the gateway."""

    path: Path = field(default_factory=default_g2_diagnostics_path)
    writer: WriteReceipt = field(default=write_receipt, repr=False)
    # A live owner may need an already-sanitized frame before the background
    # receipt write finishes.  It receives a copied bounded snapshot and has
    # no authority to modify persistence or the incoming event.
    observer: ObserveReceipt | None = field(default=None, repr=False)
    _events: list[dict[str, Any]] = field(default_factory=list, init=False)
    _persist_task: asyncio.Task[None] | None = field(default=None, init=False)
    _dirty: bool = field(default=False, init=False)
    _disabled: bool = field(default=False, init=False)

    def record(self, event: dict[str, str | int]) -> None:
        """Queue one protocol-sanitized event without blocking the gateway."""

        # protocol.py has already whitelisted every event field. Copy it so a
        # caller cannot mutate a stored record after this method returns.
        if self._disabled:
            return
        record: dict[str, Any] = {
            "sequence": len(self._events) + 1,
            "recordedAtMs": int(time.time() * 1000),
        }
        record.update(event)
        self._events.append(record)
        if len(self._events) > MAX_EVENTS:
            self._events = self._events[-MAX_EVENTS:]
            for index, retained in enumerate(self._events, start=1):
                retained["sequence"] = index
        self._dirty = True
        self._notify_observer()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Unit callers have no event loop. Production dispatch always does,
            # so this synchronous branch cannot delay a glasses session.
            self._persist_synchronously()
            return
        if self._persist_task is None or self._persist_task.done():
            self._persist_task = loop.create_task(self._persist_pending())

    def record_session(self, *, physical_g2: bool) -> None:
        """Record the gateway's own source classification, never a device ID.

        This fact cannot come from the plugin: only the trusted Mac knows
        whether the authenticated peer is a non-loopback G2.  A simulator is
        useful for regression work, but it must never become physical proof.
        """

        self.record({
            "type": "g2_session",
            "source": "physical_g2" if physical_g2 else "nonphysical",
        })

    async def flush(self) -> None:
        """Finish a pending receipt write during normal gateway shutdown."""

        if self._persist_task is not None:
            await self._persist_task

    async def _persist_pending(self) -> None:
        while self._dirty and not self._disabled:
            self._dirty = False
            try:
                await asyncio.to_thread(self.writer, self.path, self._snapshot())
            except Exception as exc:
                self._disable(exc)
                return

    def _persist_synchronously(self) -> None:
        try:
            self.writer(self.path, self._snapshot())
        except Exception as exc:
            self._disable(exc)
            return
        self._dirty = False

    def _snapshot(self) -> list[dict[str, Any]]:
        return [dict(event) for event in self._events]

    def _notify_observer(self) -> None:
        observer = self.observer
        if observer is None:
            return
        try:
            observer(self._snapshot())
        except Exception as exc:
            # Observers are optional live consumers.  A degraded acceptance
            # receipt must not make the glasses transport lose a real gesture
            # or disable durable diagnostic persistence.
            log.warning("G2 diagnostic observer failed after %s", type(exc).__name__)

    def _disable(self, exc: Exception) -> None:
        self._disabled = True
        self._dirty = False
        # The class is enough for a support operator; paths and exception text
        # can carry user- or machine-specific data and do not belong in logs.
        log.warning("G2 diagnostic persistence disabled after %s", type(exc).__name__)
