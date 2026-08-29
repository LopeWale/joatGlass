"""Classification tests for the physical Gate 0 capture harness."""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock

import pytest
import websockets

from scripts.real_g2_location_probe_trace import (
    capture,
    configure_probe_logging,
    await_stationary_marker,
    marker_timeout_seconds,
    summarize_navigation_field_run,
    summarize_receipt,
    write_navigation_field_run_summary,
)


def _confirm_reply(request: dict[str, object]) -> str:
    return json.dumps(
        {
            "kind": "confirm",
            "actionId": request["actionId"],
            "accepted": True,
            "challengeId": request["challengeId"],
            "sessionId": request["sessionId"],
            "payloadDigest": request["payloadDigest"],
            "previewDigest": request["previewDigest"],
        }
    )


def test_probe_logging_suppresses_gateway_and_transport_metadata() -> None:
    """A physical run must not emit host/device metadata through child loggers."""

    logger_names = ("joat.ble.hub_gateway", "websockets")
    prior_levels = {name: logging.getLogger(name).level for name in logger_names}
    try:
        configure_probe_logging()
        assert all(logging.getLogger(name).level == logging.WARNING for name in logger_names)
    finally:
        for name, level in prior_levels.items():
            logging.getLogger(name).setLevel(level)


def test_marker_timeout_never_overstates_the_packed_g2_confirmation_window() -> None:
    assert marker_timeout_seconds(4.0) == 4.0
    assert marker_timeout_seconds(30.0) == 8.0


@pytest.mark.asyncio
async def test_stationary_marker_renews_only_the_noop_capture_prompt() -> None:
    gateway = type("Gateway", (), {})()
    gateway.request_confirm = AsyncMock(side_effect=[False, False, True])

    marked, attempts = await await_stationary_marker(
        gateway,
        mode="walking",
        marker_timeout_s=8.0,
        marker_attempts=5,
    )

    assert (marked, attempts) == (True, 3)
    assert [call.args[0] for call in gateway.request_confirm.await_args_list] == [
        "gate0-walking-marker-1",
        "gate0-walking-marker-2",
        "gate0-walking-marker-3",
    ]
    assert all(call.args[1] == "Gate 0 walking capture. Tap while stopped to mark lens visible."
               for call in gateway.request_confirm.await_args_list)


async def _connect_when_ready(url: str):  # type: ignore[no-untyped-def]
    for _ in range(100):
        try:
            return await websockets.connect(url)
        except OSError:
            await asyncio.sleep(0.01)
    raise AssertionError("capture gateway did not start")


def test_success_requires_high_profile_and_a_following_sample() -> None:
    summary = summarize_receipt(
        "walking",
        {
            "schema": "joat.g2-diagnostic.v2",
            "events": [
                {
                    "type": "location_probe_state",
                    "requested": "high",
                    "outcome": "success",
                },
                {"type": "location_probe", "sampleSequence": 1},
                {"type": "page_transition"},
                {"type": "gesture"},
                {"type": "tile_upload"},
            ],
        },
    )

    assert summary == {
        "schema": "joat.g2-location-probe-run.v2",
        "mode": "walking",
        "result": "pass",
        "eventCount": 5,
        "stateCount": 1,
        "sampleCount": 1,
        "pageTransitionCount": 1,
        "gestureCount": 1,
        "tileUploadCount": 1,
        "hudSurfaceEventCount": 0,
        "visualRebuildOutcome": "missing",
        "firstFrameOutcome": "missing",
        "selectedHudSurface": "missing",
    }


def test_summary_records_bridge_outcomes_without_claiming_lens_visibility() -> None:
    summary = summarize_receipt(
        "walking",
        {
            "schema": "joat.g2-diagnostic.v2",
            "events": [
                {
                    "type": "hud_surface",
                    "stage": "visual_rebuild",
                    "surface": "visual",
                    "outcome": "success",
                },
                {
                    "type": "hud_surface",
                    "stage": "first_frame",
                    "surface": "visual",
                    "outcome": "failed",
                },
                {
                    "type": "hud_surface",
                    "stage": "selected",
                    "surface": "text",
                    "outcome": "success",
                },
            ],
        },
    )

    assert summary["hudSurfaceEventCount"] == 3
    assert summary["visualRebuildOutcome"] == "success"
    assert summary["firstFrameOutcome"] == "failed"
    assert summary["selectedHudSurface"] == "text"


def test_success_without_samples_is_inconclusive() -> None:
    summary = summarize_receipt(
        "cycling",
        {
            "schema": "joat.g2-diagnostic.v2",
            "events": [
                {
                    "type": "location_probe_state",
                    "requested": "high",
                    "outcome": "success",
                }
            ],
        },
    )

    assert summary["result"] == "inconclusive"
    assert summary["sampleCount"] == 0


def test_navigation_field_run_summary_keeps_only_quality_buckets_and_video_markers() -> None:
    summary = summarize_navigation_field_run(
        "walking",
        {
            "schema": "joat.g2-diagnostic.v2",
            "events": [
                {
                    "sequence": 1, "recordedAtMs": 100,
                    "type": "navigation_cue", "cue": "destination", "outcome": "success",
                },
                {
                    "sequence": 2, "recordedAtMs": 200,
                    "type": "navigation_location", "sampleSequence": 1,
                    "lifecycle": "foreground", "fields": 5, "cadence": "first",
                    "accuracy": "lte10", "timestampAge": "fresh",
                    "displacement": "first", "headingDelta": "unavailable",
                },
                {
                    "sequence": 3, "recordedAtMs": 300,
                    "type": "navigation_cue", "cue": "guidance", "outcome": "success",
                },
                {
                    "sequence": 4, "recordedAtMs": 400,
                    "type": "navigation_cue", "cue": "stopped", "outcome": "success",
                },
            ],
        },
    )

    assert summary["schema"] == "joat.g2-navigation-field-run.v1"
    assert summary["receiptResult"] == "pass"
    assert summary["locationQuality"] == {
        "accuracy": {"lte10": 1},
        "timestampAge": {"fresh": 1},
        "cadence": {"first": 1},
        "displacement": {"first": 1},
        "headingDelta": {"unavailable": 1},
        "lifecycle": {"foreground": 1},
        "fieldPresence": {"timestamp_heading": 1},
    }
    assert summary["hudCues"] == [
        {"cue": "destination", "outcome": "success", "recordedAtMs": 100},
        {"cue": "guidance", "outcome": "success", "recordedAtMs": 300},
        {"cue": "stopped", "outcome": "success", "recordedAtMs": 400},
    ]
    assert summary["lensVideoMarkers"] == [
        {"name": "start", "recordedAtMs": 100},
        {"name": "end", "recordedAtMs": 400},
    ]
    serialized = json.dumps(summary).lower()
    assert "latitude" not in serialized
    assert "longitude" not in serialized
    assert "cajun" not in serialized
    assert "address" not in serialized
    assert "route" not in serialized


def test_navigation_field_run_receipt_stays_inconclusive_without_both_video_markers() -> None:
    summary = summarize_navigation_field_run(
        "cycling",
        {
            "schema": "joat.g2-diagnostic.v2",
            "events": [
                {
                    "sequence": 1, "recordedAtMs": 100,
                    "type": "navigation_cue", "cue": "destination", "outcome": "success",
                },
                {
                    "sequence": 2, "recordedAtMs": 200,
                    "type": "navigation_location", "sampleSequence": 1,
                    "lifecycle": "foreground", "fields": 0, "cadence": "first",
                    "accuracy": "lte10", "timestampAge": "fresh",
                    "displacement": "first", "headingDelta": "unavailable",
                },
            ],
        },
    )

    assert summary["receiptResult"] == "inconclusive"
    assert summary["lensVideoMarkers"] == [{"name": "start", "recordedAtMs": 100}]


def test_navigation_field_run_summary_writes_separate_owner_only_output(tmp_path) -> None:  # type: ignore[no-untyped-def]
    receipt = tmp_path / "normal-joat-receipt.json"
    receipt.write_text(json.dumps({
        "schema": "joat.g2-diagnostic.v2",
        "events": [{
            "sequence": 1, "recordedAtMs": 100,
            "type": "navigation_cue", "cue": "destination", "outcome": "success",
        }],
    }))
    summary_path = tmp_path / "walking-field-run.json"

    summary = write_navigation_field_run_summary(
        mode="walking", receipt_path=receipt, out=summary_path,
    )

    assert json.loads(summary_path.read_text()) == summary
    assert summary_path.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        write_navigation_field_run_summary(
            mode="walking", receipt_path=receipt, out=summary_path,
        )


def test_failed_high_profile_is_a_failure_even_if_no_sample_arrives() -> None:
    summary = summarize_receipt(
        "walking",
        {
            "schema": "joat.g2-diagnostic.v2",
            "events": [
                {
                    "type": "location_probe_state",
                    "requested": "high",
                    "outcome": "failed",
                }
            ],
        },
    )

    assert summary["result"] == "fail"


@pytest.mark.parametrize(
    ("mode", "receipt"),
    [
        ("driving", {"schema": "joat.g2-diagnostic.v2", "events": []}),
        ("walking", {"schema": "wrong", "events": []}),
        ("walking", {"schema": "joat.g2-diagnostic.v2", "events": {}}),
    ],
)
def test_summary_rejects_non_gate0_inputs(
    mode: str, receipt: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        summarize_receipt(mode, receipt)


@pytest.mark.asyncio
async def test_capture_uses_real_gateway_controls_and_writes_separate_evidence(
    tmp_path, unused_tcp_port: int,  # type: ignore[no-untyped-def]
) -> None:
    receipt_path = tmp_path / "walking-receipt.json"
    task = asyncio.create_task(
        capture(
            mode="walking",
            out=receipt_path,
            port=unused_tcp_port,
            connect_timeout_s=1.0,
            duration_s=0.05,
            pre_marker_delay_s=0,
        )
    )

    socket = await _connect_when_ready(f"ws://127.0.0.1:{unused_tcp_port}")
    try:
        assert json.loads(await socket.recv()) == {"kind": "auth.accepted"}
        assert json.loads(await socket.recv()) == {
            "kind": "g2.diagnostic.control",
            "enabled": True,
        }
        assert json.loads(await socket.recv()) == {
            "kind": "location.probe.control",
            "enabled": True,
        }
        await socket.send(
            json.dumps(
                {
                    "kind": "g2.diagnostic",
                    "event": {
                        "type": "location_probe_state",
                        "requested": "high",
                        "outcome": "success",
                    },
                }
            )
        )
        await socket.send(
            json.dumps(
                {
                    "kind": "g2.diagnostic",
                    "event": {
                        "type": "location_probe",
                        "sampleSequence": 1,
                        "profile": "high",
                        "lifecycle": "foreground",
                        "fields": 7,
                        "cadenceMs": 1000,
                        "accuracy": "lte10",
                        "timestampAge": "fresh",
                        "displacement": "5_20",
                        "headingDelta": "0_30",
                    },
                }
            )
        )
        await socket.send(
            json.dumps(
                {
                    "kind": "hello",
                    "sn": "S",
                    "model": "g2",
                    "launchSource": "appMenu",
                }
            )
        )
        request = json.loads(await socket.recv())
        assert request["kind"] == "confirm.request"
        assert request["prompt"] == (
            "Gate 0 walking capture. Tap while stopped to mark lens visible."
        )
        await socket.send(_confirm_reply(request))

        summary = await task
    finally:
        await socket.close()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert summary["result"] == "pass"
    assert summary["sampleCount"] == 1
    assert receipt_path.exists()
    summary_path = tmp_path / "walking-receipt-summary.json"
    assert json.loads(summary_path.read_text()) == summary
    assert receipt_path.stat().st_mode & 0o077 == 0
    assert summary_path.stat().st_mode & 0o077 == 0
