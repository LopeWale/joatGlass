"""Record raw IMU samples from the glasses for motion calibration.

docs/NEXT_STEPS.md §5 asks for labelled traces (nod / head-shake / walking /
nothing) before any motion gesture may decide a confirmation again. The
plugin has relayed ``imu`` samples since the ``imu.trace`` downlink existed,
but nothing on the Mac ever sent that downlink, so the path was documented
as wired and was not. This is the Mac's half: opt in with ``--imu-trace
PATH``, and every authenticated connection asks the plugin for samples at
``pace_ms`` and appends them to PATH as JSON lines.

The file carries only ``at`` (the plugin's millisecond clock), ``x``, ``y``,
``z`` and a monotonic receive time: no transcript, no content.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from ..core.protocol import Hello, Imu, ImuControl, ImuTrace

log = logging.getLogger(__name__)

#: The SDK's closed set of report paces, in milliseconds.
IMU_PACES_MS = (100, 200, 500, 1000)


class _TraceTransport(Protocol):
    def on_hello(self, callback: Callable[[Hello], None]) -> None: ...
    def on_imu(self, callback: Callable[[Imu], None]) -> None: ...
    async def send(self, message: Any) -> None: ...


class ImuTraceRecorder:
    """Ask for samples on every hello and append each one to a JSONL file."""

    def __init__(self, path: Path, *, pace_ms: int = 100) -> None:
        if pace_ms not in IMU_PACES_MS:
            raise ValueError(f"pace_ms must be one of {IMU_PACES_MS}")
        self._path = path
        self._pace_ms = pace_ms
        self._samples = 0

    @property
    def samples(self) -> int:
        return self._samples

    def attach(self, transport: _TraceTransport) -> None:
        transport.on_hello(lambda _hello: self._arm(transport))
        transport.on_imu(self._record)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        log.info("IMU trace recording to %s at %d ms", self._path, self._pace_ms)

    def _arm(self, transport: _TraceTransport) -> None:
        async def arm() -> None:
            try:
                await transport.send(ImuControl(open=True, pace_ms=self._pace_ms))
                await transport.send(ImuTrace(enabled=True))
            except Exception as exc:  # noqa: BLE001 — link loss is the lifecycle's business
                log.warning("IMU trace arm failed error=%s", type(exc).__name__)

        asyncio.get_running_loop().create_task(arm())

    def _record(self, sample: Imu) -> None:
        line = json.dumps({
            "at": sample.at, "x": sample.x, "y": sample.y, "z": sample.z,
            "received_monotonic": time.monotonic(),
        })
        try:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            log.warning("IMU trace write failed error=%s", type(exc).__name__)
            return
        self._samples += 1
