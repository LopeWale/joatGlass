from __future__ import annotations

import json

import pytest

from joat.services.brainstorm_acceptance import RUN_SCHEMA
from scripts.simulator_brainstorm_action_flow_acceptance import (
    BrainstormActionFlowError,
    ScreenReceipt,
    VOICE_FIXTURE_PCM,
    _ARTIFACT_STATES,
    _VISUAL_ONLY_ARTIFACTS,
    _prepare_simulator_plugin_root,
    _write_voice_fixture,
    default_ws_url,
    validate_voice_direct,
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
    assert default_ws_url(8797) == "ws://laptop-mac.local:8797/glasses"
    with pytest.raises(ValueError, match="port"):
        default_ws_url(0)


def _frames() -> list[ScreenReceipt]:
    rows = [
        # Mirrors _EXPECTED_FLOW. Brainstorm opens READY while the terminal
        # still needs approval — the Hermes gate is gone, and this fixture
        # is where the removed gate would otherwise stay alive as a spec.
        ("brainstorm-ungated", None, None, "brainstorm", "ready", False),
        ("terminal-hermes-required", None, None, "agent_terminal", "approval_required", False),
        ("terminal-retry-ready", "click", "retry_terminal", "agent_terminal", "ready", False),
        ("brainstorm-reopened", None, None, "brainstorm", "ready", False),
        ("daily-topic-capture", "click", "begin_topic_capture", "brainstorm", "topic_capture", False),
        ("daily-topic-capture-cancelled", "down", "cancel", "brainstorm", "ready", False),
        ("diagnostic-ready", None, None, "brainstorm", "ready", True),
        ("acceptance-ready", "double_click", "begin_acceptance", "brainstorm", "acceptance_ready", False),
        ("running", "click", "begin_topic_capture", "brainstorm", "running", False),
        ("terminal", "click", "show_terminal", "agent_terminal", "running", False),
        ("returned", "click", "show_brainstorm", "brainstorm", "running", False),
        ("reconnected", None, "authenticated_replay", "brainstorm", "running", False),
        ("cancelled", None, "cancel", "brainstorm", "cancelled", False),
        ("voice-direct-running", "synthetic_mic", "start_brainstorm", "brainstorm", "running", False),
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
        name: {
            "raw": f"{index:02d}-{name}.rgba.png",
            "optical": f"{index:02d}-{name}.optical.png",
        }
        for index, name in enumerate(_ARTIFACT_STATES, start=1)
    }


def _voice_direct() -> dict[str, object]:
    return {
        "source": "glasses",
        "route": "addressed_brainstorm",
        "hud": "verified",
        "stt": "deterministic_no_network",
        "openclaw": "not_contacted",
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
        frames[-1].diagnostic_armed,
        frames[-2].framebuffer_sha256,
    )
    with pytest.raises(BrainstormActionFlowError, match="distinct visible"):
        validate_flow(frames, _acceptance())

    leaked = _acceptance()
    leaked["payload"] = "forbidden"
    with pytest.raises(BrainstormActionFlowError, match="forbidden content"):
        validate_flow(_frames(), leaked)


def test_voice_direct_receipt_requires_glasses_source_and_remains_content_free() -> None:
    validate_voice_direct(_voice_direct())

    wrong_source = _voice_direct()
    wrong_source["source"] = "phone"
    with pytest.raises(BrainstormActionFlowError, match="glasses source"):
        validate_voice_direct(wrong_source)

    leaked = _voice_direct()
    leaked["transcript"] = "forbidden"
    with pytest.raises(BrainstormActionFlowError, match="forbidden content"):
        validate_voice_direct(leaked)


def test_voice_fixture_is_fixed_synthetic_pcm_under_the_disposable_plugin_root(tmp_path) -> None:
    root = tmp_path / "simulator-plugin-root"
    fixture = _write_voice_fixture(root)

    assert fixture == root / "public" / "__joat-audio-fixture" / "glasses-direct.pcm"
    assert fixture.read_bytes() == VOICE_FIXTURE_PCM
    assert fixture.stat().st_mode & 0o777 == 0o600


def test_flow_rejects_a_diagnostic_arm_on_the_daily_glasses_path() -> None:
    frames = _frames()
    daily_capture = frames[2]
    frames[2] = ScreenReceipt(
        daily_capture.name,
        daily_capture.simulator_input,
        daily_capture.semantic_action,
        daily_capture.screen,
        daily_capture.state,
        True,
        daily_capture.framebuffer_sha256,
    )

    with pytest.raises(BrainstormActionFlowError, match="unexpected rendered"):
        validate_flow(frames, _acceptance())


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

    missing_busy = _artifacts()
    del missing_busy["brainstorm-while-terminal-busy"]
    with pytest.raises(BrainstormActionFlowError, match="HUD flow"):
        validate_visual_artifacts(missing_busy)


def test_visual_previews_include_the_running_agent_terminal_transcript() -> None:
    """The terminal density change needs an optical proof, not just geometry tests."""
assert _VISUAL_ONLY_ARTIFACTS == (
        "dense-transcript-preview",
        "terminal-transcript-preview",
        "brainstorm-while-terminal-busy",
    )
