from __future__ import annotations

import json

import pytest

from joat.services.brainstorm_acceptance import RUN_SCHEMA
from scripts.simulator_brainstorm_action_flow_acceptance import (
    BrainstormActionFlowError,
    ScreenReceipt,
    _prepare_simulator_plugin_root,
    default_ws_url,
    validate_visual_artifacts,
    validate_flow,
)


def test_simulator_plugin_root_snapshots_application_source_but_reuses_dependencies(
    tmp_path,
) -> None:
    repo = tmp_path / "repo"
    plugin = repo / "apps" / "hub-plugin"
    source = plugin / "src"
    source.mkdir(parents=True)
    (source / "main.ts").write_text("export const version = 'before';\n", encoding="utf-8")
    (plugin / "node_modules").mkdir()
    (plugin / "index.html").write_text("<main></main>\n", encoding="utf-8")
    (plugin / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    (plugin / "app.json").write_text(json.dumps({
        "permissions": [{"name": "network", "whitelist": ["wss://mac.local:8787"]}],
    }), encoding="utf-8")

    output = tmp_path / "out"
    output.mkdir()
    root = _prepare_simulator_plugin_root(
        repo,
        output,
        "ws://127.0.0.1:8794/glasses",
    )

    assert not (root / "src").is_symlink()
    assert (root / "src" / "main.ts").read_text(encoding="utf-8") == "export const version = 'before';\n"
    assert (root / "node_modules").is_symlink()
    (source / "main.ts").write_text("export const version = 'after';\n", encoding="utf-8")
    assert (root / "src" / "main.ts").read_text(encoding="utf-8") == "export const version = 'before';\n"


def test_default_websocket_origin_tracks_the_isolated_gateway_port() -> None:
    assert default_ws_url(8797) == "ws://emmanuels-macbook-pro.local:8797/glasses"
    with pytest.raises(ValueError, match="port"):
        default_ws_url(0)


def _frames() -> list[ScreenReceipt]:
    rows = [
        ("terminal-approval-required", None, None, "agent_terminal", "approval_required"),
        ("terminal-retry-ready", "click", "retry_terminal", "agent_terminal", "ready"),
        ("terminal-topic-capture", "click", "begin_topic_capture", "brainstorm", "topic_capture"),
        ("terminal-topic-capture-cancelled", "down", "cancel", "brainstorm", "ready"),
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


def _artifacts() -> dict[str, dict[str, str]]:
    return {
        frame.name: {
            "raw": f"{index:02d}-{frame.name}.rgba.png",
            "optical": f"{index:02d}-{frame.name}.optical.png",
        }
        for index, frame in enumerate(_frames(), start=1)
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


def test_visual_artifacts_require_an_opaque_optical_preview_for_every_hud_state() -> None:
    validate_visual_artifacts(_artifacts())

    missing_preview = _artifacts()
    del missing_preview["terminal"]["optical"]
    with pytest.raises(BrainstormActionFlowError, match="terminal.*optical"):
        validate_visual_artifacts(missing_preview)

    alpha_only = _artifacts()
    alpha_only["returned"]["optical"] = "05-returned.rgba.alpha.png"
    with pytest.raises(BrainstormActionFlowError, match="opaque green-on-black"):
        validate_visual_artifacts(alpha_only)
