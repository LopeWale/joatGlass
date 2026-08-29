"""Classification tests for the physical Gate 0 capture harness."""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from scripts.real_g2_location_probe_trace import capture, summarize_receipt


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
