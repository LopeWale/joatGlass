#!/usr/bin/env python3
"""Capture one packaged, physical-G2 Gate 0 location-probe run.

The harness uses the production authenticated Hub gateway, the existing pairing
store, and the same opt-in ``g2.diagnostic`` control as JOAT. It never stores
coordinates, raw motion values, HUD text, device identifiers, or input payloads.
Walking and cycling are separate explicit runs; this script adds no routing or
guidance behavior.

Before starting, stop the ordinary JOAT engine, keep the phone foreground and
unlocked, and begin filming through the G2 lens beside a synchronized clock.
Tap the fixed capture marker only while stationary. Movement begins after that
marker is dismissed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from joat.ble.hub_gateway import HubGatewayTransport
from joat.core.pairing import PairingStore
from joat.services.g2_diagnostics import G2DiagnosticStore, TRACE_SCHEMA

log = logging.getLogger("joat.real_g2_location_probe_trace")

RUN_SCHEMA = "joat.g2-location-probe-run.v2"
MODES = frozenset({"walking", "cycling"})


def _last_hud_surface_value(
    events: list[dict[str, object]], *, stage: str, field: str
) -> str:
    """Return a bounded first-light value, or explicit absence for old builds."""

    for event in reversed(events):
        if event.get("type") != "hud_surface" or event.get("stage") != stage:
            continue
        value = event.get(field)
        if isinstance(value, str):
            return value
    return "missing"


def summarize_receipt(mode: str, receipt: dict[str, object]) -> dict[str, object]:
    """Return a bounded, content-free classification of one retained receipt."""

    if mode not in MODES:
        raise ValueError("mode must be walking or cycling")
    if receipt.get("schema") != TRACE_SCHEMA:
        raise ValueError("receipt is not a Gate 0 v2 diagnostic trace")
    events = receipt.get("events")
    if not isinstance(events, list):
        raise ValueError("receipt events must be a list")

    typed_events = [event for event in events if isinstance(event, dict)]
    high_states = [
        event
        for event in typed_events
        if event.get("type") == "location_probe_state"
        and event.get("requested") == "high"
    ]
    samples = [
        event for event in typed_events if event.get("type") == "location_probe"
    ]

    if high_states and high_states[-1].get("outcome") == "failed":
        result = "fail"
    elif (
        high_states
        and high_states[-1].get("outcome") == "success"
        and samples
    ):
        result = "pass"
    else:
        # Missing state, an unknown outcome, or high/success without samples
        # cannot establish host delivery.
        result = "inconclusive"

    return {
        "schema": RUN_SCHEMA,
        "mode": mode,
        "result": result,
        "eventCount": len(typed_events),
        "stateCount": len(high_states),
        "sampleCount": len(samples),
        "pageTransitionCount": sum(
            event.get("type") == "page_transition" for event in typed_events
        ),
        "gestureCount": sum(
            event.get("type") == "gesture" for event in typed_events
        ),
        "tileUploadCount": sum(
            event.get("type") == "tile_upload" for event in typed_events
        ),
        # These are bridge outcomes, deliberately not lens-pixel claims. The
        # synchronized G2 video remains the independent visible-HUD receipt.
        "hudSurfaceEventCount": sum(
            event.get("type") == "hud_surface" for event in typed_events
        ),
        "visualRebuildOutcome": _last_hud_surface_value(
            typed_events, stage="visual_rebuild", field="outcome"
        ),
        "firstFrameOutcome": _last_hud_surface_value(
            typed_events, stage="first_frame", field="outcome"
        ),
        "selectedHudSurface": _last_hud_surface_value(
            typed_events, stage="selected", field="surface"
        ),
    }


def _write_owner_only_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


async def capture(
    *,
    mode: str,
    out: Path,
    port: int,
    connect_timeout_s: float,
    duration_s: float,
    pre_marker_delay_s: float,
) -> dict[str, object]:
    """Run one stationary-marker then foreground movement capture."""

    if mode not in MODES:
        raise ValueError("mode must be walking or cycling")
    summary_path = out.with_name(f"{out.stem}-summary.json")
    collisions = [path for path in (out, summary_path) if path.exists()]
    if collisions:
        raise FileExistsError("refusing to overwrite an existing evidence file")

    connected = asyncio.Event()
    disconnected = asyncio.Event()
    gateway = HubGatewayTransport(
        host="0.0.0.0",
        port=port,
        pairing=PairingStore(),
        g2_diagnostics=G2DiagnosticStore(path=out),
        location_probe=True,
    )
    gateway.on_hello(lambda _hello: connected.set())
    gateway.on_disconnect(disconnected.set)

    await gateway.start()
    try:
        log.info("waiting for packaged JOAT on port=%d", port)
        await asyncio.wait_for(connected.wait(), connect_timeout_s)
        log.info(
            "packaged plugin authenticated; keep camera and phone ready for %s marker",
            mode,
        )
        if pre_marker_delay_s > 0:
            await asyncio.sleep(pre_marker_delay_s)

        marked = await gateway.request_confirm(
            f"gate0-{mode}-marker",
            f"Gate 0 {mode} capture. Tap while stopped to mark lens visible.",
            timeout_s=8.0,
        )
        if not marked:
            raise RuntimeError("stationary lens marker was not confirmed")

        log.info(
            "%s marker confirmed; begin movement for %.0f seconds with phone foreground",
            mode,
            duration_s,
        )
        duration = asyncio.create_task(asyncio.sleep(duration_s))
        lost = asyncio.create_task(disconnected.wait())
        done, pending = await asyncio.wait(
            {duration, lost}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if lost in done and lost.result():
            log.warning("packaged plugin disconnected before the capture duration elapsed")
    finally:
        await gateway.stop()

    raw: Any = json.loads(out.read_text())
    if not isinstance(raw, dict):
        raise ValueError("diagnostic receipt must be an object")
    summary = summarize_receipt(mode, raw)
    _write_owner_only_json(summary_path, summary)
    log.info(
        "capture result=%s samples=%d receipt=%s summary=%s",
        summary["result"],
        summary["sampleCount"],
        out,
        summary_path,
    )
    return summary


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=sorted(MODES), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--connect-timeout", type=_positive_float, default=120.0)
    parser.add_argument("--duration", type=_positive_float, default=120.0)
    parser.add_argument("--pre-marker-delay", type=float, default=5.0)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    summary = asyncio.run(
        capture(
            mode=args.mode,
            out=args.out,
            port=args.port,
            connect_timeout_s=args.connect_timeout,
            duration_s=args.duration,
            pre_marker_delay_s=max(0.0, args.pre_marker_delay),
        )
    )
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
