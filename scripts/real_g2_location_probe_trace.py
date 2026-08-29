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
FIELD_RUN_SCHEMA = "joat.g2-navigation-field-run.v1"
MODES = frozenset({"walking", "cycling"})
NAVIGATION_CUES = frozenset({
    "destination", "repair", "mode", "preview", "guidance", "uncertain",
    "wrong_way", "off_route", "rerouting", "arrived", "error", "stopped",
})
QUALITY_BUCKETS = {
    "accuracy": frozenset({"unknown", "lte5", "lte10", "lte25", "gt25"}),
    "timestampAge": frozenset({"missing", "future", "fresh", "stale"}),
    "cadence": frozenset({"first", "lte1s", "1_5s", "5_30s", "gt30s"}),
    "displacement": frozenset({"first", "lt5", "5_20", "20_100", "gt100"}),
    "headingDelta": frozenset({"unavailable", "insufficient_motion", "0_30", "31_90", "91_180"}),
    "lifecycle": frozenset({"foreground", "background"}),
}
# The packaged G2 confirmation controller owns the wearer-visible clock.  It
# deliberately fails closed after eight seconds, irrespective of a longer Mac
# request.  Keep the harness truthful about that independently enforced bound.
PACKAGED_CONFIRMATION_MAX_SECONDS = 8.0


def marker_timeout_seconds(requested: float) -> float:
    """Return the marker window the currently packaged G2 can actually honor."""

    return min(requested, PACKAGED_CONFIRMATION_MAX_SECONDS)


async def await_stationary_marker(
    gateway: HubGatewayTransport,
    *,
    mode: str,
    marker_timeout_s: float,
    marker_attempts: int,
) -> tuple[bool, int]:
    """Renew the harmless stationary marker within G2's fixed fail-closed window."""

    if marker_attempts < 1:
        raise ValueError("marker_attempts must be positive")
    prompt = f"Gate 0 {mode} capture. Tap while stopped to mark lens visible."
    for attempt in range(1, marker_attempts + 1):
        marked = await gateway.request_confirm(
            f"gate0-{mode}-marker-{attempt}",
            prompt,
            timeout_s=marker_timeout_s,
        )
        if marked:
            return True, attempt
        if attempt < marker_attempts:
            log.info("stationary marker was not confirmed; renewing capture marker")
    return False, marker_attempts


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


def _positive_recorded_at(event: dict[str, object]) -> int | None:
    value = event.get("recordedAtMs")
    return value if type(value) is int and value > 0 else None


def _field_presence_bucket(value: object) -> str | None:
    if type(value) is not int or not 0 <= value <= 7:
        return None
    return (
        "none", "timestamp", "speed", "timestamp_speed", "heading",
        "timestamp_heading", "speed_heading", "all",
    )[value]


def _bucket_counts(
    samples: list[dict[str, object]], *, field: str,
    allowed: frozenset[str] | None = None,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for sample in samples:
        value = _field_presence_bucket(sample.get(field)) if field == "fields" else sample.get(field)
        if not isinstance(value, str) or (allowed is not None and value not in allowed):
            continue
        counts[value] = counts.get(value, 0) + 1
    return counts


def summarize_navigation_field_run(mode: str, receipt: dict[str, object]) -> dict[str, object]:
    """Reduce a normal navigation trace into a privacy-bounded field-run receipt.

    `receiptResult` is deliberately only a transport/diagnostic result. A
    continuous lens video that includes the named start and end markers remains
    the separate proof that either cue was visible to the wearer.
    """

    if mode not in MODES:
        raise ValueError("mode must be walking or cycling")
    if receipt.get("schema") != TRACE_SCHEMA:
        raise ValueError("receipt is not a Gate 0 v2 diagnostic trace")
    events = receipt.get("events")
    if not isinstance(events, list):
        raise ValueError("receipt events must be a list")

    typed_events = [event for event in events if isinstance(event, dict)]
    samples = [
        event for event in typed_events if event.get("type") == "navigation_location"
    ]
    cues = []
    for event in typed_events:
        if event.get("type") != "navigation_cue":
            continue
        cue = event.get("cue")
        outcome = event.get("outcome")
        recorded_at = _positive_recorded_at(event)
        if (
            not isinstance(cue, str)
            or cue not in NAVIGATION_CUES
            or outcome not in ("success", "failed")
            or recorded_at is None
        ):
            continue
        cues.append({"cue": cue, "outcome": outcome, "recordedAtMs": recorded_at})
    cues = cues[-64:]

    start = next(
        (
            cue for cue in cues
            if cue["cue"] == "destination" and cue["outcome"] == "success"
        ),
        None,
    )
    end = next(
        (
            cue for cue in reversed(cues)
            if cue["cue"] in ("stopped", "arrived") and cue["outcome"] == "success"
        ),
        None,
    )
    markers = []
    if start is not None:
        markers.append({"name": "start", "recordedAtMs": start["recordedAtMs"]})
    if end is not None:
        markers.append({"name": "end", "recordedAtMs": end["recordedAtMs"]})

    if any(cue["cue"] == "error" and cue["outcome"] == "success" for cue in cues):
        receipt_result = "fail"
    elif start is not None and end is not None and samples and any(
        cue["cue"] == "guidance" and cue["outcome"] == "success" for cue in cues
    ):
        receipt_result = "pass"
    else:
        receipt_result = "inconclusive"

    return {
        "schema": FIELD_RUN_SCHEMA,
        "mode": mode,
        "receiptResult": receipt_result,
        "lensVideoResult": "required",
        "eventCount": len(typed_events),
        "navigationSampleCount": len(samples),
        "locationQuality": {
            "accuracy": _bucket_counts(samples, field="accuracy", allowed=QUALITY_BUCKETS["accuracy"]),
            "timestampAge": _bucket_counts(samples, field="timestampAge", allowed=QUALITY_BUCKETS["timestampAge"]),
            "cadence": _bucket_counts(samples, field="cadence", allowed=QUALITY_BUCKETS["cadence"]),
            "displacement": _bucket_counts(samples, field="displacement", allowed=QUALITY_BUCKETS["displacement"]),
            "headingDelta": _bucket_counts(samples, field="headingDelta", allowed=QUALITY_BUCKETS["headingDelta"]),
            "lifecycle": _bucket_counts(samples, field="lifecycle", allowed=QUALITY_BUCKETS["lifecycle"]),
            "fieldPresence": _bucket_counts(samples, field="fields"),
        },
        "hudCues": cues,
        "lensVideoMarkers": markers,
    }


def write_navigation_field_run_summary(
    *, mode: str, receipt_path: Path, out: Path,
) -> dict[str, object]:
    """Write a separately named owner-only summary of a normal JOAT run."""

    if out.exists():
        raise FileExistsError("refusing to overwrite an existing evidence file")
    raw: Any = json.loads(receipt_path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("diagnostic receipt must be an object")
    summary = summarize_navigation_field_run(mode, raw)
    _write_owner_only_json(out, summary)
    return summary


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
    marker_timeout_s: float = 30.0,
    marker_attempts: int = 5,
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

        effective_marker_timeout_s = marker_timeout_seconds(marker_timeout_s)
        if effective_marker_timeout_s != marker_timeout_s:
            log.info(
                "packed confirmation window is %.0fs; marker request was capped",
                effective_marker_timeout_s,
            )
        marked, _marker_attempt_count = await await_stationary_marker(
            gateway,
            mode=mode,
            marker_timeout_s=effective_marker_timeout_s,
            marker_attempts=marker_attempts,
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


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def configure_probe_logging() -> None:
    """Keep the operator console as redacted as the persisted receipt.

    The gateway's normal INFO diagnostics may include the host-provided device
    identifier.  Gate 0 intentionally records neither that identifier nor
    precise location data, so a physical probe must not leak it to the console
    either.  The runner emits its own bounded progress messages instead.
    """

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    for logger_name in ("joat.ble.hub_gateway", "websockets"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=sorted(MODES), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--summarize-navigation-field-run", action="store_true",
        help="reduce an existing normal-JOAT diagnostic receipt; does not start a gateway",
    )
    parser.add_argument(
        "--source-receipt", type=Path,
        help="normal JOAT's owner-only g2-diagnostics.json for --summarize-navigation-field-run",
    )
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--connect-timeout", type=_positive_float, default=120.0)
    parser.add_argument("--duration", type=_positive_float, default=120.0)
    parser.add_argument("--pre-marker-delay", type=float, default=5.0)
    parser.add_argument(
        "--marker-timeout",
        type=_positive_float,
        default=PACKAGED_CONFIRMATION_MAX_SECONDS,
        help="seconds to wait for the stationary lens-marker tap (packed G2 maximum: 8)",
    )
    parser.add_argument(
        "--marker-attempts",
        type=_positive_int,
        default=5,
        help="number of renewed no-op stationary-marker prompts",
    )
    args = parser.parse_args()
    configure_probe_logging()
    if args.summarize_navigation_field_run:
        if args.source_receipt is None:
            parser.error("--source-receipt is required with --summarize-navigation-field-run")
        summary = write_navigation_field_run_summary(
            mode=args.mode,
            receipt_path=args.source_receipt,
            out=args.out,
        )
        print(json.dumps(summary, sort_keys=True))
        return
    if args.source_receipt is not None:
        parser.error("--source-receipt requires --summarize-navigation-field-run")
    summary = asyncio.run(
        capture(
            mode=args.mode,
            out=args.out,
            port=args.port,
            connect_timeout_s=args.connect_timeout,
            duration_s=args.duration,
            pre_marker_delay_s=max(0.0, args.pre_marker_delay),
            marker_timeout_s=args.marker_timeout,
            marker_attempts=args.marker_attempts,
        )
    )
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
