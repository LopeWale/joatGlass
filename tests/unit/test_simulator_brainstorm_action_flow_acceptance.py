from __future__ import annotations

import pytest

from joat.services.brainstorm_acceptance import RUN_SCHEMA
from scripts.simulator_brainstorm_action_flow_acceptance import (
    BrainstormActionFlowError,
    ScreenReceipt,
    validate_flow,
)


def _frames() -> list[ScreenReceipt]:
    rows = [
        ("ready", None, None, "brainstorm", "ready"),
        ("acceptance-ready", "double_click", "begin_acceptance", "brainstorm", "acceptance_ready"),
        ("running", "click", "begin_topic_capture", "brainstorm", "running"),
        ("terminal", "click", "show_terminal", "agent_terminal", "running"),
        ("returned", "click", "show_brainstorm", "brainstorm", "running"),
        ("reconnected", None, "authenticated_replay", "brainstorm", "running"),
        ("cancelled", "down", "cancel", "brainstorm", "cancelled"),
    ]
    return [
        ScreenReceipt(*row, framebuffer_sha256=f"{index:064x}")
        for index, row in enumerate(rows, start=1)
    ]


def _acceptance() -> dict[str, object]:
    return {
        "schema": RUN_SCHEMA,
        "mode": "simulator_app_engine",
        "result": "inconclusive",
        "retainedEventCount": 12,
        "events": [
            {
                "type": "visual_frame",
                "kind": "brainstorm",
                "surface": "brainstorm",
                "state": "running",
                "sha256": "a" * 64,
            },
            {
                "type": "visual_frame",
                "kind": "brainstorm",
                "surface": "brainstorm",
                "state": "running",
                "sha256": "b" * 64,
            },
        ],
        "wearerVisibleHud": {"bridge": "not_verified", "lens": "not_recorded"},
        "lensVideoSha256": None,
        "hermesRun": "cancelled",
        "hermesStream": {"hud": "verified", "frameSha256": ["a" * 64, "b" * 64]},
        "g2Reconnect": "restored",
        "startedBy": {
            "interface": "g2",
            "modality": "gesture",
            "surface": "brainstorm",
            "source": "right",
            "gesture": "double_press",
            "delivery": "sys",
        },
    }


def test_flow_accepts_only_the_causal_simulator_sequence_and_never_physical_pass() -> None:
    validate_flow(_frames(), _acceptance())

    physical = _acceptance()
    physical["mode"] = "physical_g2_app_engine"
    physical["result"] = "pass"
    with pytest.raises(BrainstormActionFlowError, match="mislabeled"):
        validate_flow(_frames(), physical)


def test_flow_rejects_duplicate_visible_frames_and_content_fields() -> None:
    frames = _frames()
    frames[-1] = ScreenReceipt(
        frames[-1].name,
        frames[-1].simulator_input,
        frames[-1].semantic_action,
        frames[-1].screen,
        frames[-1].state,
        frames[-2].framebuffer_sha256,
    )
    with pytest.raises(BrainstormActionFlowError, match="distinct visible"):
        validate_flow(frames, _acceptance())

    leaked = _acceptance()
    leaked["payload"] = "forbidden"
    with pytest.raises(BrainstormActionFlowError, match="forbidden content"):
        validate_flow(_frames(), leaked)


def test_flow_requires_the_fixed_simulator_gesture_source() -> None:
    acceptance = _acceptance()
    started = acceptance["startedBy"]
    assert isinstance(started, dict)
    started["source"] = "ring"
    with pytest.raises(BrainstormActionFlowError, match="gesture source"):
        validate_flow(_frames(), acceptance)


@pytest.mark.parametrize(
    ("hermes_stream", "message"),
    [
        ({"hud": "not_verified", "frameSha256": ["a" * 64, "b" * 64]}, "HUD stream"),
        ({"hud": "verified", "frameSha256": ["a" * 64]}, "ordered frame pair"),
        (
            {"hud": "verified", "frameSha256": ["a" * 64, "a" * 64]},
            "ordered frame pair",
        ),
    ],
)
def test_flow_requires_a_verified_distinct_hermes_frame_pair(
    hermes_stream: dict[str, object],
    message: str,
) -> None:
    acceptance = _acceptance()
    acceptance["hermesStream"] = hermes_stream

    with pytest.raises(BrainstormActionFlowError, match=message):
        validate_flow(_frames(), acceptance)


def test_flow_requires_hermes_frames_in_brainstorm_running_trace_order() -> None:
    acceptance = _acceptance()
    acceptance["events"] = [
        {
            "type": "visual_frame",
            "kind": "brainstorm",
            "surface": "brainstorm",
            "state": "running",
            "sha256": "b" * 64,
        },
        {
            "type": "visual_frame",
            "kind": "brainstorm",
            "surface": "brainstorm",
            "state": "running",
            "sha256": "a" * 64,
        },
    ]

    with pytest.raises(BrainstormActionFlowError, match="trace order"):
        validate_flow(_frames(), acceptance)
