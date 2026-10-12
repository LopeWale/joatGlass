"""Protocol mirror tests.

These use the exact wire shapes apps/hub-plugin/src/types.ts emits. If the TS
side changes and this file isn't updated in the same commit, these fail — which
is the point: silent protocol drift between the two ends is the failure mode
this whole module exists to prevent.
"""

from __future__ import annotations

from datetime import date, timedelta

from pathlib import Path

import pytest

from joat.ble.hub_gateway import pack_pcm
from joat.core.protocol import (
    AgentTerminalHudView,
    AgentTerminalHudView,
    Auth,
    AssistantOverlay,
    AssistantConversationDismiss,
    AssistantConversationTurn,
    AssistantConversationView,
    AuthAccepted,
    Audio,
    AudioControl,
    BrainstormAction,
    BrainstormHudView,
    Confirm,
    ConfirmRequest,
    ContainerId,
    Device,
    EventSource,
    EventType,
    Hello,
    G2Diagnostic,
    G2DiagnosticControl,
    HudList,
    HudText,
    Imu,
    ImuTrace,
    Location,
    LocationRefresh,
    LocationProbeControl,
    NavigationAction,
    NavigationCoordinate,
    NavigationHudView,
    NavigationOriginFailure,
    NavigationProgressPolicy,
    NavigationOriginRequest,
    NavigationRoute,
    NavigationRoutePlan,
    NavigationRouteStep,
    NavigationViewAck,
    NavigationView,
    PlanningGridCell,
    PlanningGridEntry,
    PairingAck,
    PairingSet,
    ProductivityHudView,
    ProductivityViewAck,
    ReminderDueAck,
    ReminderDueComplete,
    Input,
    Policy,
    ProtocolError,
    Shutdown,
    parse_uplink,
)


class TestParseUplink:
    def test_productivity_view_ack_is_content_free_and_verifying_only(self) -> None:
        acknowledgement = {
            "kind": "productivity.view.ack",
            "presentationReceiptId": "a76b12f1-45e4-41fd-aed8-086f6b6b1954",
            "generation": 7,
            "outcome": "accepted",
        }
        assert "Private calendar title" not in acknowledgement.values()
        assert parse_uplink(acknowledgement) == ProductivityViewAck(
            presentation_receipt_id="a76b12f1-45e4-41fd-aed8-086f6b6b1954",
            generation=7,
            outcome="accepted",
        )

    def test_navigation_action_is_bound_to_exact_visible_generation(self) -> None:
        assert parse_uplink({
            "kind": "navigation.action",
            "sessionId": "nav-1",
            "requestId": "mode-2",
            "generation": 8,
            "action": "select_mode",
            "choiceId": "walking",
        }) == NavigationAction(
            session_id="nav-1",
            request_id="mode-2",
            generation=8,
            action="select_mode",
            choice_id="walking",
        )

    def test_navigation_route_ack_and_semantic_state_are_content_free(self) -> None:
        ack = parse_uplink({
            "kind": "navigation.route.ack", "sessionId": "nav-1",
            "routeId": "route-1", "revision": 2, "outcome": "installed",
        })
        assert ack.outcome == "installed"  # type: ignore[attr-defined]
        state = parse_uplink({
            "kind": "navigation.state", "sessionId": "nav-1", "routeId": "route-1",
            "revision": 2, "sequence": 7, "state": "wrong_way",
        })
        assert state.state == "wrong_way"  # type: ignore[attr-defined]

    def test_reroute_request_is_the_only_navigation_uplink_with_a_coordinate(self) -> None:
        message = parse_uplink({
            "kind": "navigation.reroute.request",
            "sessionId": "nav-1", "routeId": "route-1", "baseRevision": 2,
            "requestId": "route-1:2:reroute", "latitude": 41.9, "longitude": -87.65,
        })
        assert message.latitude == 41.9  # type: ignore[attr-defined]
        assert message.longitude == -87.65  # type: ignore[attr-defined]

    def test_navigation_origin_is_bound_to_the_explicit_request(self) -> None:
        message = parse_uplink({
            "kind": "navigation.origin", "sessionId": "nav-1",
            "requestId": "origin-1", "generation": 6,
            "latitude": 41.9, "longitude": -87.65, "accuracy": 6,
        })
        assert message.request_id == "origin-1"  # type: ignore[attr-defined]

    def test_navigation_view_and_origin_failure_receipts_are_generation_bound(self) -> None:
        assert parse_uplink({
            "kind": "navigation.view.ack", "sessionId": "nav-1",
            "requestId": "mode-2", "generation": 8,
            "outcome": "bridge_acknowledged",
        }) == NavigationViewAck(
            session_id="nav-1", request_id="mode-2", generation=8,
            outcome="bridge_acknowledged",
        )
        assert parse_uplink({
            "kind": "navigation.origin.failed", "sessionId": "nav-1",
            "requestId": "origin-1", "generation": 9, "reason": "unavailable",
        }) == NavigationOriginFailure(
            session_id="nav-1", request_id="origin-1", generation=9,
            reason="unavailable",
        )

    @pytest.mark.parametrize(
        "payload",
        [
            {
                "kind": "navigation.view.ack", "sessionId": "nav-1",
                "requestId": "mode-2", "generation": 8, "outcome": "visible",
            },
            {
                "kind": "navigation.origin.failed", "sessionId": "nav-1",
                "requestId": "origin-1", "generation": 9, "reason": "denied",
            },
        ],
    )
    def test_navigation_failure_receipts_reject_unallowlisted_outcomes(
        self, payload: dict[str, object],
    ) -> None:
        with pytest.raises(ProtocolError):
            parse_uplink(payload)

    @pytest.mark.parametrize(
        ("mutated", "match"),
        [
            ({"generation": True}, "generation"),
            ({"action": "teleport"}, "action"),
            ({"choiceId": ""}, "choiceId"),
            ({"privateAddress": "2301 N Lincoln"}, "unsupported fields"),
        ],
    )
    def test_navigation_action_rejects_unbound_or_content_fields(
        self, mutated: dict[str, object], match: str,
    ) -> None:
        payload: dict[str, object] = {
            "kind": "navigation.action",
            "sessionId": "nav-1",
            "requestId": "mode-2",
            "generation": 8,
            "action": "select_mode",
            "choiceId": "walking",
        }
        payload.update(mutated)
        with pytest.raises(ProtocolError, match=match):
            parse_uplink(payload)  # type: ignore[arg-type]

    def test_auth_carries_token_in_a_frame_not_the_url(self) -> None:
        assert parse_uplink({"kind": "auth", "token": "secret"}) == Auth(token="secret")

    def test_auth_allows_an_explicit_unpaired_client(self) -> None:
        assert parse_uplink({"kind": "auth", "token": None}) == Auth(token=None)

    def test_hello(self) -> None:
        msg = parse_uplink(
            {"kind": "hello", "sn": "ABC123", "model": "g2", "launchSource": "appMenu"}
        )
        assert msg == Hello(sn="ABC123", model="g2", launch_source="appMenu")

    def test_audio(self) -> None:
        msg = parse_uplink({"kind": "audio", "source": "glasses", "seq": 7, "pcm": [1, -1]})
        assert isinstance(msg, Audio)
        assert msg.seq == 7 and msg.pcm == [1, -1]

    @pytest.mark.parametrize("source", [None, "unknown", 3])
    def test_audio_rejects_missing_or_unknown_source(self, source: object) -> None:
        payload: dict[str, object] = {"kind": "audio", "seq": 7, "pcm": [1, -1]}
        if source is not None:
            payload["source"] = source
        with pytest.raises(ProtocolError, match="audio source is invalid"):
            parse_uplink(payload)

    def test_imu_defaults_missing_axes_to_zero(self) -> None:
        msg = parse_uplink({"kind": "imu", "y": 0.5, "at": 100})
        assert msg == Imu(x=0.0, y=0.5, z=0.0, at=100)

    def test_input_camel_case_maps_to_snake_case(self) -> None:
        msg = parse_uplink(
            {
                "kind": "input",
                "event": {
                    "containerId": 2,
                    "eventType": EventType.CLICK,
                    "eventSource": EventSource.GLASSES_RIGHT,
                },
            }
        )
        assert isinstance(msg, Input)
        assert msg.event.container_id == 2
        assert msg.event.is_right_temple_click

    def test_input_with_no_event_object_is_tolerated(self) -> None:
        # A sysEvent relay can carry only eventType/eventSource; an absent
        # event object should not crash the link.
        msg = parse_uplink({"kind": "input"})
        assert isinstance(msg, Input)
        assert msg.event.container_id is None

    def test_conversation_input_is_bound_to_the_exact_visible_revision(self) -> None:
        msg = parse_uplink({
            "kind": "input",
            "event": {
                "containerName": "assistant-conversation",
                "selectedName": "exit",
                "conversationId": "voice-chat-7",
                "conversationRevision": 12,
            },
        })
        assert isinstance(msg, Input)
        assert msg.event.conversation_id == "voice-chat-7"
        assert msg.event.conversation_revision == 12

        for event in (
            {"conversationId": "voice-chat-7"},
            {"conversationRevision": 12},
            {"conversationId": "bad id", "conversationRevision": 12},
            {"conversationId": "voice-chat-7", "conversationRevision": -1},
        ):
            with pytest.raises(ProtocolError, match="conversation"):
                parse_uplink({"kind": "input", "event": event})

    def test_brainstorm_action_is_bounded_and_typed(self) -> None:
        message = parse_uplink({"kind": "brainstorm.action", "action": "cancel"})
        assert message == BrainstormAction(action="cancel")
        # The long-press on a running work session (2026-10-06).
        assert parse_uplink({"kind": "brainstorm.action", "action": "cancel_terminal"}) == \
            BrainstormAction(action="cancel_terminal")

    @pytest.mark.parametrize("payload", [
        {"kind": "brainstorm.action", "action": "invent"},
        {"kind": "brainstorm.action", "action": "cancel", "topic": "leak"},
        # The deleted diagnostic self-test's action (2026-10-06) is gone.
        {"kind": "brainstorm.action", "action": "begin_acceptance"},
        {"kind": "brainstorm.action", "action": "begin_acceptance", "surface": "brainstorm", "source": "right", "gesture": "double_press"},
        {"kind": "brainstorm.action", "action": "cancel", "surface": "brainstorm"},
        {"kind": "brainstorm.action"},
    ])
    def test_brainstorm_action_rejects_freeform_or_unknown_fields(self, payload: object) -> None:
        with pytest.raises(ProtocolError, match="brainstorm action"):
            parse_uplink(payload)  # type: ignore[arg-type]

    def test_left_temple_click_is_not_a_confirm(self) -> None:
        msg = parse_uplink(
            {
                "kind": "input",
                "event": {
                    "eventType": EventType.CLICK,
                    "eventSource": EventSource.GLASSES_LEFT,
                },
            }
        )
        assert isinstance(msg, Input)
        assert not msg.event.is_right_temple_click
        assert not msg.event.is_ring_click

    def test_device_optional_fields(self) -> None:
        msg = parse_uplink({"kind": "device", "connect": "connected", "wearing": True})
        assert msg == Device(connect="connected", battery=None, wearing=True, charging=None)

    def test_confirm(self) -> None:
        msg = parse_uplink({
            "kind": "confirm",
            "actionId": "act-1",
            "accepted": True,
            "challengeId": "challenge-1",
            "sessionId": "session-1",
            "payloadDigest": "payload-digest",
            "previewDigest": "preview-digest",
        })
        assert msg == Confirm(
            action_id="act-1",
            accepted=True,
            challenge_id="challenge-1",
            session_id="session-1",
            payload_digest="payload-digest",
            preview_digest="preview-digest",
        )

    def test_confirm_requires_a_real_boolean(self) -> None:
        with pytest.raises(ProtocolError, match="accepted must be a boolean"):
            parse_uplink({"kind": "confirm", "actionId": "act-1", "accepted": "false"})

    def test_policy(self) -> None:
        assert parse_uplink({"kind": "policy", "resume": "confirm"}) == Policy(resume="confirm")

    def test_g2_diagnostic_is_a_fixed_content_free_schema(self) -> None:
        message = parse_uplink({
            "kind": "g2.diagnostic",
            "event": {
                "type": "tile_upload",
                "frame": 4,
                "order": 2,
                "containerId": 9,
                "outcome": "success",
            },
        })
        assert message == G2Diagnostic(event={
            "type": "tile_upload", "frame": 4, "order": 2,
            "containerId": 9, "outcome": "success",
        })

    def test_g2_diagnostic_tile_upload_carries_its_bridge_timing(self) -> None:
        # The plugin times every tile's bridge round trip and reports the
        # encoded size; both are optional so an older plugin still parses.
        message = parse_uplink({
            "kind": "g2.diagnostic",
            "event": {
                "type": "tile_upload", "frame": 4, "order": 2, "containerId": 9,
                "outcome": "success", "durationMs": 1375, "bytes": 6120,
            },
        })
        assert message == G2Diagnostic(event={
            "type": "tile_upload", "frame": 4, "order": 2, "containerId": 9,
            "outcome": "success", "durationMs": 1375, "bytes": 6120,
        })
        with pytest.raises(ProtocolError, match="durationMs is invalid"):
            parse_uplink({
                "kind": "g2.diagnostic",
                "event": {
                    "type": "tile_upload", "frame": 4, "order": 2, "containerId": 9,
                    "outcome": "success", "durationMs": -1,
                },
            })
        with pytest.raises(ProtocolError, match="unsupported fields"):
            parse_uplink({
                "kind": "g2.diagnostic",
                "event": {
                    "type": "tile_upload", "frame": 4, "order": 2, "containerId": 9,
                    "outcome": "success", "pixels": [0, 1, 2],
                },
            })

    @pytest.mark.parametrize("container_id", [16, 17, 18, 19])
    def test_g2_diagnostic_tile_upload_names_a_page_part(self, container_id: int) -> None:
        # The header halves and Home's column are image page parts. On the
        # glasses (2026-10-08/09) one of their writes hung and the link
        # dropped every 15 s; every report of it was refused here.
        message = parse_uplink({
            "kind": "g2.diagnostic",
            "event": {
                "type": "tile_upload", "frame": 4, "order": 0, "containerId": container_id,
                "outcome": "error", "durationMs": 6000, "bytes": 812,
            },
        })
        assert message == G2Diagnostic(event={
            "type": "tile_upload", "frame": 4, "order": 0, "containerId": container_id,
            "outcome": "error", "durationMs": 6000, "bytes": 812,
        })

    @pytest.mark.parametrize("container_id", [1, 2, 5, 7, 11, 15, 20])
    def test_g2_diagnostic_tile_upload_refuses_a_text_container(self, container_id: int) -> None:
        with pytest.raises(ProtocolError, match="containerId is invalid"):
            parse_uplink({
                "kind": "g2.diagnostic",
                "event": {
                    "type": "tile_upload", "frame": 4, "order": 0, "containerId": container_id,
                    "outcome": "error",
                },
            })

    def test_g2_diagnostic_frame_timing_splits_one_frame_by_stage(self) -> None:
        # The whole-frame split the DEV-only console line cannot give us:
        # `[hud-timing]` never runs in a QR sideload or a packed .ehpk, and
        # those are the only places the BLE hop exists. Stage durations are
        # optional so an older plugin still parses.
        message = parse_uplink({
            "kind": "g2.diagnostic",
            "event": {
                "type": "frame_timing", "frame": 7, "kind": "overview",
                "changedTiles": 4, "queueMs": 0, "renderMs": 31,
                "encodeMs": 12, "pacingMs": 330, "bridgeMs": 1840, "totalMs": 2213,
            },
        })
        assert message == G2Diagnostic(event={
            "type": "frame_timing", "frame": 7, "kind": "overview",
            "changedTiles": 4, "queueMs": 0, "renderMs": 31,
            "encodeMs": 12, "pacingMs": 330, "bridgeMs": 1840, "totalMs": 2213,
        })
        # An older plugin sends the shape without the stage breakdown.
        assert parse_uplink({
            "kind": "g2.diagnostic",
            "event": {
                "type": "frame_timing", "frame": 1, "kind": "list", "changedTiles": 0,
            },
        }) == G2Diagnostic(event={
            "type": "frame_timing", "frame": 1, "kind": "list", "changedTiles": 0,
        })
        # A screen name outside the closed set is a schema change, not data.
        with pytest.raises(ProtocolError, match="kind is invalid"):
            parse_uplink({
                "kind": "g2.diagnostic",
                "event": {
                    "type": "frame_timing", "frame": 1, "kind": "sidebar",
                    "changedTiles": 1,
                },
            })
        # Five tiles cannot exist: the page declares four image containers.
        with pytest.raises(ProtocolError, match="changedTiles is invalid"):
            parse_uplink({
                "kind": "g2.diagnostic",
                "event": {
                    "type": "frame_timing", "frame": 1, "kind": "overview",
                    "changedTiles": 5,
                },
            })
        # This record is durations and counts. Nothing renderable rides it.
        with pytest.raises(ProtocolError, match="unsupported fields"):
            parse_uplink({
                "kind": "g2.diagnostic",
                "event": {
                    "type": "frame_timing", "frame": 1, "kind": "overview",
                    "changedTiles": 1, "main": "Submit project report draft",
                },
            })

    def test_g2_diagnostic_accepts_content_free_navigation_page_transition(self) -> None:
        assert parse_uplink({
            "kind": "g2.diagnostic",
            "event": {
                "type": "page_transition",
                "from": "overview",
                "to": "navigation",
                "reason": "show_navigation",
                "outcome": "success",
            },
        }) == G2Diagnostic(event={
            "type": "page_transition",
            "from": "overview",
            "to": "navigation",
            "reason": "show_navigation",
            "outcome": "success",
        })

    def test_g2_diagnostic_accepts_only_bounded_first_light_outcome(self) -> None:
        assert parse_uplink({
            "kind": "g2.diagnostic",
            "event": {
                "type": "hud_surface",
                "stage": "first_frame",
                "surface": "visual",
                "outcome": "failed",
            },
        }) == G2Diagnostic(event={
            "type": "hud_surface",
            "stage": "first_frame",
            "surface": "visual",
            "outcome": "failed",
        })

    def test_g2_diagnostic_accepts_a_prompt_stage_and_nothing_else_about_it(self) -> None:
        event = {"type": "prompt_stage", "owner": "brainstorm", "stage": "mic_refused"}
        assert parse_uplink({"kind": "g2.diagnostic", "event": event}) == G2Diagnostic(event=event)
        for bad in (
            {**event, "stage": "spoken"},
            {**event, "owner": "notes"},
            {**event, "question": "What would you like to brainstorm?"},
        ):
            with pytest.raises(ProtocolError):
                parse_uplink({"kind": "g2.diagnostic", "event": bad})

    def test_g2_diagnostic_accepts_only_redacted_brainstorm_action_receipts(self) -> None:
        assert parse_uplink({
            "kind": "g2.diagnostic",
            "event": {
                "type": "brainstorm_action", "action": "show_terminal", "surface": "brainstorm",
                "source": "right", "gesture": "tap", "delivery": "sys",
            },
        }) == G2Diagnostic(event={
            "type": "brainstorm_action", "action": "show_terminal", "surface": "brainstorm",
            "source": "right", "gesture": "tap", "delivery": "sys",
        })
        # The Brainstorm frame receipt hashed the four-tile raster, which no
        # longer exists, and fed only the self-test deleted 2026-10-06.
        with pytest.raises(ProtocolError, match="visual frame kind"):
            parse_uplink({
                "kind": "g2.diagnostic",
                "event": {
                    "type": "visual_frame", "kind": "brainstorm", "surface": "agent_terminal",
                    "state": "running", "sha256": "a" * 64,
                },
            })

    @pytest.mark.parametrize("event", [
        {
            "type": "brainstorm_action", "action": "show_terminal", "surface": "brainstorm",
            "source": "right", "gesture": "tap", "delivery": "sys", "topic": "private",
        },
        {
            "type": "visual_frame", "kind": "brainstorm", "surface": "brainstorm",
            "state": "running", "sha256": "A" * 64,
        },
        {
            "type": "visual_frame", "kind": "brainstorm", "surface": "brainstorm",
            "state": "running", "sha256": "a" * 63,
        },
    ])
    def test_g2_brainstorm_diagnostic_rejects_content_and_noncanonical_digest(self, event) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(ProtocolError):
            parse_uplink({"kind": "g2.diagnostic", "event": event})

    def test_g2_diagnostic_rejects_content_and_unknown_fields(self) -> None:
        with pytest.raises(ProtocolError, match="unsupported fields"):
            parse_uplink({
                "kind": "g2.diagnostic",
                "event": {
                    "type": "gesture", "gesture": "tap", "delivery": "sys",
                    "text": "private message",
                },
            })

        with pytest.raises(ProtocolError, match="unsupported fields"):
            parse_uplink({
                "kind": "g2.diagnostic",
                "event": {
                    "type": "hud_surface",
                    "stage": "selected",
                    "surface": "text",
                    "outcome": "success",
                    "content": "JOAT is ready",
                },
            })

        with pytest.raises(ProtocolError, match="unsupported fields"):
            parse_uplink({
                "kind": "g2.diagnostic",
                "event": {
                    "type": "gesture", "gesture": "tap", "delivery": "sys",
                },
                "text": "private message",
            })

    def test_location_probe_diagnostic_accepts_only_bounded_derived_metadata(self) -> None:
        message = parse_uplink({
            "kind": "g2.diagnostic",
            "event": {
                "type": "location_probe",
                "sampleSequence": 7,
                "profile": "high",
                "lifecycle": "foreground",
                "fields": 7,
                "cadence": "lte1s",
                "accuracy": "lte10",
                "timestampAge": "fresh",
                "displacement": "5_20",
                "headingDelta": "0_30",
            },
        })
        assert message == G2Diagnostic(event={
            "type": "location_probe",
            "sampleSequence": 7,
            "profile": "high",
            "lifecycle": "foreground",
            "fields": 7,
            "cadence": "lte1s",
            "accuracy": "lte10",
            "timestampAge": "fresh",
            "displacement": "5_20",
            "headingDelta": "0_30",
        })

    def test_location_probe_state_distinguishes_start_failure_from_missing_fixes(self) -> None:
        assert parse_uplink({
            "kind": "g2.diagnostic",
            "event": {
                "type": "location_probe_state",
                "requested": "high",
                "outcome": "failed",
            },
        }) == G2Diagnostic(event={
            "type": "location_probe_state",
            "requested": "high",
            "outcome": "failed",
        })

    def test_location_probe_diagnostic_rejects_raw_coordinates(self) -> None:
        with pytest.raises(ProtocolError, match="unsupported fields"):
            parse_uplink({
                "kind": "g2.diagnostic",
                "event": {
                    "type": "location_probe",
                    "sampleSequence": 1,
                    "profile": "high",
                    "lifecycle": "foreground",
                    "fields": 0,
                    "cadence": "first",
                    "accuracy": "unknown",
                    "timestampAge": "missing",
                    "displacement": "first",
                    "headingDelta": "unavailable",
                    "latitude": 41.88,
                },
            })

    def test_navigation_field_run_diagnostics_accept_only_fixed_semantic_values(self) -> None:
        assert parse_uplink({
            "kind": "g2.diagnostic",
            "event": {
                "type": "navigation_cue", "cue": "guidance", "outcome": "success",
            },
        }) == G2Diagnostic(event={
            "type": "navigation_cue", "cue": "guidance", "outcome": "success",
        })
        assert parse_uplink({
            "kind": "g2.diagnostic",
            "event": {
                "type": "navigation_location", "sampleSequence": 3,
                "lifecycle": "foreground", "fields": 5, "cadence": "1_5s",
                "accuracy": "lte25", "timestampAge": "fresh",
                "displacement": "20_100", "headingDelta": "31_90",
            },
        }) == G2Diagnostic(event={
            "type": "navigation_location", "sampleSequence": 3,
            "lifecycle": "foreground", "fields": 5, "cadence": "1_5s",
            "accuracy": "lte25", "timestampAge": "fresh",
            "displacement": "20_100", "headingDelta": "31_90",
        })

    def test_navigation_field_run_diagnostics_reject_raw_coordinates_and_text(self) -> None:
        with pytest.raises(ProtocolError, match="unsupported fields"):
            parse_uplink({
                "kind": "g2.diagnostic",
                "event": {
                    "type": "navigation_cue", "cue": "guidance", "outcome": "success",
                    "destination": "private place",
                },
            })

    def test_unknown_policy_rejected(self) -> None:
        with pytest.raises(ProtocolError, match="unknown resume policy"):
            parse_uplink({"kind": "policy", "resume": "yolo"})

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ProtocolError, match="unknown uplink kind"):
            parse_uplink({"kind": "telepathy"})

    def test_missing_kind_rejected(self) -> None:
        with pytest.raises(ProtocolError, match="no string 'kind'"):
            parse_uplink({"sn": "ABC"})

    def test_missing_required_field_rejected(self) -> None:
        with pytest.raises(ProtocolError, match="missing required field"):
            parse_uplink({"kind": "confirm", "actionId": "act-1"})


class TestDownlinkWire:
    """Field names must be camelCase — the TS side reads them literally."""

    def test_hud_text_omits_unset_optionals(self) -> None:
        assert HudText(text="hi").to_wire() == {
            "kind": "hud.text",
            "containerId": ContainerId.MAIN,
            "text": "hi",
        }

    def test_hud_text_includes_offset_and_length(self) -> None:
        wire = HudText(text="hi", offset=3, length=2).to_wire()
        assert wire["offset"] == 3 and wire["length"] == 2

    def test_hud_text_carries_content_provenance_and_trace_id(self) -> None:
        wire = HudText(
            text="Dele · Messages\nOn my way",
            content_kind="notification",
            trace_id="msg-7",
        ).to_wire()
        assert wire["contentKind"] == "notification"
        assert wire["traceId"] == "msg-7"

    def test_assistant_overlay_is_a_distinct_non_destructive_surface(self) -> None:
        assert AssistantOverlay(
            text="On it.",
            operation="replace",
            dismiss_after_ms=6_000,
        ).to_wire() == {
            "kind": "assistant.overlay",
            "text": "On it.",
            "operation": "replace",
            "presentation": "answer",
            "dismissAfterMs": 6_000,
        }

        assert AssistantOverlay(
            text="Reply sent.", presentation="receipt",
        ).to_wire()["presentation"] == "receipt"

    def test_assistant_conversation_keeps_prompt_and_answer_in_one_bounded_transcript(self) -> None:
        answer = "The sky appears blue because shorter blue wavelengths scatter more strongly. " * 12
        wire = AssistantConversationView(
            conversation_id="voice-chat-7",
            revision=3,
            state="ready",
            turns=(
                AssistantConversationTurn("user", "Why is the sky blue?"),
                AssistantConversationTurn("assistant", answer),
            ),
            continue_after_ms=30_000,
        ).to_wire()

        assert wire == {
            "kind": "assistant.conversation",
            "operation": "replace",
            "conversationId": "voice-chat-7",
            "revision": 3,
            "state": "ready",
            "turns": [
                {"role": "user", "text": "Why is the sky blue?"},
                {"role": "assistant", "text": answer},
            ],
            "continueAfterMs": 30_000,
            "earlierTurnsOmitted": False,
        }
        assert len(wire["turns"][1]["text"]) > 512
        assert AssistantConversationDismiss("voice-chat-7", 3).to_wire() == {
            "kind": "assistant.conversation",
            "operation": "dismiss",
            "conversationId": "voice-chat-7",
            "revision": 3,
        }

    def test_assistant_conversation_rejects_unmarked_loss_instead_of_silently_truncating(self) -> None:
        with pytest.raises(ProtocolError, match="turn text"):
            AssistantConversationView(
                conversation_id="voice-chat-7",
                revision=1,
                state="ready",
                turns=(AssistantConversationTurn("assistant", "x" * 4_001),),
                continue_after_ms=30_000,
            ).to_wire()

    def test_hud_list_enforces_sdk_caps(self) -> None:
        wire = HudList(items=[f"item {i}" * 20 for i in range(30)]).to_wire()
        assert len(wire["items"]) == 20
        assert all(len(s) <= 64 for s in wire["items"])

    def test_hud_list_row_cap_counts_utf16_units_like_the_sdk(self) -> None:
        # The SDK's 64-per-row cap is JavaScript String.length. An astral
        # glyph is TWO units there, so a code-point slice would smuggle up
        # to 128 units past the cap and truncate on the glasses instead.
        from joat.core.protocol import utf16_units

        wire = HudList(items=["\U0001F4C5" * 64]).to_wire()
        assert utf16_units(wire["items"][0]) <= 64
        # A surrogate pair is never split at the boundary.
        assert not wire["items"][0].endswith("\ud83d")

    def test_hud_list_carries_a_bounded_focus_page_header_and_hint(self) -> None:
        wire = HudList(
            items=["Messages"],
            title="T" * 60,
            hint="H" * 120,
            request_id="hud-request-7",
            selectable=True,
        ).to_wire()
        assert wire["title"] == "T" * 46
        assert wire["hint"] == "H" * 96
        assert wire["requestId"] == "hud-request-7"
        assert wire["selectable"] is True

    def test_hud_list_page_controls_name_row_indices_and_drop_out_of_range(self) -> None:
        # Mirror: `pageControls` on the hud.list downlink in types.ts. Present
        # (even empty) means the paging rows exist, so the plugin may draw the
        # thread as a firmware list; absent means the old swipe-paging thread.
        items = ["Latest messages", "Dele · Hi", "▼ Older messages"]
        wire = HudList(items=items, page_controls=(None, 2)).to_wire()
        assert wire["pageControls"] == {"olderIndex": 2}
        wire = HudList(items=items, page_controls=(0, 9)).to_wire()
        assert wire["pageControls"] == {"newerIndex": 0}
        wire = HudList(items=items, page_controls=(None, None)).to_wire()
        assert wire["pageControls"] == {}
        assert "pageControls" not in HudList(items=items).to_wire()

    def test_message_footer_preserves_paging_reply_and_complete_back_hint(self) -> None:
        hint = "up newer · down older · tap read · say reply · left x2: back"
        wire = HudList(items=["Latest messages"], title="MESSAGES · CONVERSATION", hint=hint).to_wire()
        assert wire["hint"] == hint
        assert wire["hint"].endswith("left x2: back")

    def test_planning_grid_entries_are_bounded_and_carry_no_identifiers(self) -> None:
        """The dashboard's right pane repaints from these on hover, so titles
        now travel with the month (wearer's ask, 2026-09-01) — but still no
        calendar or reminder identifiers, and at most six rows per day."""
        cell = PlanningGridCell(
            day=date(2026, 9, 1),
            event_count=8,
            reminder_count=0,
            in_month=True,
            entries=tuple(
                PlanningGridEntry(time="16:30", kind="event", title=f"Meeting {i}" + "x" * 90)
                for i in range(8)
            ),
        )
        wire = cell.to_wire()
        assert len(wire["entries"]) == 6
        for entry in wire["entries"]:
            assert set(entry) == {"time", "kind", "title"}
            assert len(entry["title"]) <= 64
        bad_kind = PlanningGridEntry(time="09:00", kind="mystery", title="x").to_wire()
        assert bad_kind["kind"] == "event"
        # A cell without entries stays exactly the pre-dashboard shape.
        bare = PlanningGridCell(
            day=date(2026, 9, 1), event_count=1, reminder_count=0, in_month=True,
        ).to_wire()
        assert "entries" not in bare

    def test_hud_list_planning_grid_is_redacted_and_bounded_to_six_weeks(self) -> None:
        cells = tuple(
            PlanningGridCell(
                day=date(2026, 8, 31) + timedelta(days=index),
                event_count=index,
                reminder_count=-1,
                in_month=index > 0,
            )
            for index in range(45)
        )

        wire = HudList(items=["week"], planning_grid=cells).to_wire()

        assert wire["planningGrid"] == [
            {"day": "2026-08-31", "eventCount": 0, "reminderCount": 0, "inMonth": False},
            {"day": "2026-09-01", "eventCount": 1, "reminderCount": 0, "inMonth": True},
            *[
                {
                    "day": (date(2026, 8, 31) + timedelta(days=index)).isoformat(),
                    "eventCount": min(99, index),
                    "reminderCount": 0,
                    "inMonth": True,
                }
                for index in range(2, 42)
            ],
        ]
        assert len(wire["planningGrid"]) == 42
        assert all("title" not in cell and "identifier" not in cell for cell in wire["planningGrid"])

    def test_brainstorm_view_is_bounded_and_has_no_terminal_payload(self) -> None:
        """The name of this test is now enforceable.

        docs/SESSION_MODEL.md:74-75 requires the Brainstorm model to carry
        no Agent Terminal, Hermes, OpenClaw, endpoint, approval or
        tool-policy field. It carried all of them, discriminated by a
        `screen` key, and this test asserted that payload as correct. The
        two surfaces are two frames now, so the absence is structural
        rather than a promise.
        """
        wire = BrainstormHudView(
            state="running",
            topic="cold-start latency",
            collaborators=["Codex", "Claude"],
            active_speaker="Claude · critic",
            activity="CLAUDE > Key it on the generation too.",
        ).to_wire()
        assert wire["kind"] == "brainstorm.view"
        assert wire["brainstorm"] == {
            "state": "running",
            "topic": "cold-start latency",
            "collaborators": ["Codex", "Claude"],
            "activeSpeaker": "Claude · critic",
            "activity": "CLAUDE > Key it on the generation too.",
        }
        # Structural, not aspirational: every forbidden field is simply
        # absent from the dataclass, so no producer can reintroduce one.
        for forbidden in ("screen", "terminalState", "endpointHost",
                          "terminalReason", "noToolsChecked"):
            assert forbidden not in wire["brainstorm"]

    def test_agent_terminal_view_owns_the_connection_facts(self) -> None:
        """Its own frame, its own kind — and the fields that describe only
        this surface live here instead of riding every Brainstorm frame."""
        wire = AgentTerminalHudView(
            state="ready",
            focus="work · run",
            lanes=["work·run", "calendar·ask1"],
            activity="> run the http audit",
            endpoint_host="home-mac.example-tailnet.ts.net",
            reason="",
            no_tools_checked=True,
        ).to_wire()
        assert wire["kind"] == "agent_terminal.view"
        assert wire["terminal"] == {
            "state": "ready",
            "focus": "work · run",
            "lanes": ["work·run", "calendar·ask1"],
            "activity": "> run the http audit",
            "endpointHost": "home-mac.example-tailnet.ts.net",
            "terminalReason": "",
            "noToolsChecked": True,
            # Distinct from state="approval_required", which ALSO describes
            # a connection whose device is unapproved. Opposite remedies,
            # so a gesture keys on the count, not the state word.
            "approvals": 0,
        }

        with pytest.raises(ProtocolError):
            BrainstormHudView(state="acceptance_ready", topic="t", collaborators=[], activity="x").to_wire()

        topic_capture = BrainstormHudView(state="topic_capture", topic="Speak your question", collaborators=["Codex", "Claude", "Hermes"], activity="Your next spoken phrase starts this Brainstorm.").to_wire()
        assert topic_capture["brainstorm"]["state"] == "topic_capture"

    def test_productivity_view_is_typed_and_bounded(self) -> None:
        wire = ProductivityHudView(
            domain="reminders",
            phase="completed",
            title="Buy filters",
            primary="Saved and verified",
            secondary="Reminders read-back matched",
            hint="left x2 overview",
            planning_anchor_unix_ms=1_788_274_200_000,
        ).to_wire()
        assert wire == {
            "kind": "productivity.view",
            "productivity": {
                "domain": "reminders",
                "phase": "completed",
                "title": "Buy filters",
                "primary": "Saved and verified",
                "secondary": "Reminders read-back matched",
                "hint": "left x2 overview",
                "planningAnchorUnixMs": 1_788_274_200_000,
            },
        }

    def test_due_ack_is_content_free_and_generation_bound(self) -> None:
        reminder_due_id = "67ec97c5-a0fd-48ec-89cb-1b756ddaf87a"
        attempt_id = "a76b12f1-45e4-41fd-aed8-086f6b6b1954"
        wire = ProductivityHudView(
            domain="reminders",
            phase="due",
            title="Private reminder",
            primary="Due now",
            secondary="Still incomplete",
            hint="say complete reminder title",
            reminder_due_id=reminder_due_id,
            reminder_due_attempt_id=attempt_id,
            reminder_due_generation=7,
        ).to_wire()
        assert wire["productivity"] == {
            "domain": "reminders",
            "phase": "due",
            "title": "Private reminder",
            "primary": "Due now",
            "secondary": "Still incomplete",
            "hint": "say complete reminder title",
            "reminderDueId": reminder_due_id,
            "reminderDueAttemptId": attempt_id,
            "reminderDueGeneration": 7,
        }
        acknowledgement = {
            "kind": "reminder_due.ack",
            "reminderDueId": reminder_due_id,
            "reminderDueAttemptId": attempt_id,
            "generation": 7,
            "outcome": "accepted",
        }
        assert "Private reminder" not in acknowledgement.values()
        assert parse_uplink(acknowledgement) == ReminderDueAck(
            reminder_due_id=reminder_due_id,
            reminder_due_attempt_id=attempt_id,
            generation=7,
            outcome="accepted",
        )
        # The tap on the due page carries the same opaque binding, nothing else.
        completion = {
            "kind": "reminder_due.complete",
            "reminderDueId": reminder_due_id,
            "reminderDueAttemptId": attempt_id,
            "generation": 7,
        }
        assert parse_uplink(completion) == ReminderDueComplete(
            reminder_due_id=reminder_due_id,
            reminder_due_attempt_id=attempt_id,
            generation=7,
        )
        for broken in (
            {**completion, "reminderDueId": "private title"},
            {**completion, "generation": -1},
            {**completion, "title": "Private reminder"},
        ):
            with pytest.raises(ProtocolError):
                parse_uplink(broken)

    def test_saved_places_arrive_whole_and_bounded(self) -> None:
        from joat.core.protocol import Places, SavedPlace

        wire = {
            "kind": "places",
            "places": [
                {"id": "p1", "name": "  Home ", "address": " 12  Oak St "},
                {"id": "p2", "name": "Work", "latitude": 35.2, "longitude": -97.4},
                {"id": "p3", "name": "Gym", "address": "Campus", "latitude": 35.21, "longitude": -97.41},
            ],
        }
        assert parse_uplink(wire) == Places(places=(
            SavedPlace(place_id="p1", name="Home", address="12 Oak St"),
            SavedPlace(place_id="p2", name="Work", latitude=35.2, longitude=-97.4),
            SavedPlace(place_id="p3", name="Gym", address="Campus", latitude=35.21, longitude=-97.41),
        ))
        assert parse_uplink({"kind": "places", "places": []}) == Places(places=())
        for broken in (
            {"kind": "places"},
            {"kind": "places", "places": "home"},
            {"kind": "places", "places": [{"id": "p1", "name": "Nowhere"}]},
            {"kind": "places", "places": [{"id": "p1", "name": "", "address": "x"}]},
            {"kind": "places", "places": [{"id": "p1", "name": "Half", "latitude": 35.2}]},
            {"kind": "places", "places": [{"id": "p1", "name": "Off", "latitude": 95, "longitude": 0}]},
            {"kind": "places", "places": [{"id": "p1", "name": "x" * 41, "address": "a"}]},
            {"kind": "places", "places": [{"id": "p1", "name": "A", "address": "a", "extra": 1}]},
            {"kind": "places", "places": [{"id": "p1", "name": "A", "address": "a"}] * 2},
            {"kind": "places", "places": [{"id": f"p{i}", "name": "A", "address": "a"} for i in range(51)]},
            {"kind": "places", "places": [], "more": True},
        ):
            with pytest.raises(ProtocolError):
                parse_uplink(broken)

    def test_verifying_view_receipt_is_content_free_and_phase_bound(self) -> None:
        receipt_id = "a76b12f1-45e4-41fd-aed8-086f6b6b1954"
        wire = ProductivityHudView(
            domain="calendar",
            phase="verifying",
            title="Private calendar title",
            primary="Saving calendar event",
            secondary="Checking native read-back",
            hint="Please wait",
            presentation_receipt_id=receipt_id,
            presentation_receipt_generation=7,
        ).to_wire()
        assert wire["productivity"]["presentationReceiptId"] == receipt_id
        assert wire["productivity"]["presentationReceiptGeneration"] == 7
        with pytest.raises(ProtocolError, match="presentation receipt requires a verifying view"):
            ProductivityHudView(
                domain="calendar",
                phase="saved",
                title="Private calendar title",
                primary="Saved and verified",
                secondary="Calendar read-back matched",
                hint="Back to Planning",
                presentation_receipt_id=receipt_id,
                presentation_receipt_generation=7,
            ).to_wire()

    @pytest.mark.parametrize("payload", [
        {"kind": "reminder_due.ack", "reminderDueId": "private title", "reminderDueAttemptId": "a76b12f1-45e4-41fd-aed8-086f6b6b1954", "generation": 7, "outcome": "accepted"},
        {"kind": "reminder_due.ack", "reminderDueId": "67ec97c5-a0fd-48ec-89cb-1b756ddaf87a", "reminderDueAttemptId": "prior attempt", "generation": 7, "outcome": "accepted"},
        {"kind": "reminder_due.ack", "reminderDueId": "67ec97c5-a0fd-48ec-89cb-1b756ddaf87a", "reminderDueAttemptId": "a76b12f1-45e4-41fd-aed8-086f6b6b1954", "generation": 0, "outcome": "accepted"},
        {"kind": "reminder_due.ack", "reminderDueId": "67ec97c5-a0fd-48ec-89cb-1b756ddaf87a", "reminderDueAttemptId": "a76b12f1-45e4-41fd-aed8-086f6b6b1954", "generation": 7, "outcome": "rendered"},
    ])
    def test_due_ack_rejects_content_or_unbound_generation(self, payload: object) -> None:
        with pytest.raises(ProtocolError):
            parse_uplink(payload)  # type: ignore[arg-type]

    def test_productivity_view_and_planning_input_reject_invalid_anchors(self) -> None:
        with pytest.raises(ProtocolError, match="planning anchor"):
            ProductivityHudView(
                domain="calendar",
                phase="saved",
                title="Review",
                primary="Saved",
                secondary="Calendar read-back matched",
                hint="left x2 Planning",
                planning_anchor_unix_ms=True,  # type: ignore[arg-type]
            ).to_wire()

        parsed = parse_uplink({
            "kind": "input",
            "event": {
                "containerName": "capmenu",
                "selectedName": "Planning",
                "planningAnchorUnixMs": 1_788_274_200_000,
            },
        })
        assert isinstance(parsed, Input)
        assert parsed.event.planning_anchor_unix_ms == 1_788_274_200_000

        # A stale outcome screen must not make the Back gesture inert. The
        # malformed optional context is discarded and generic Planning opens.
        fallback = parse_uplink({
            "kind": "input",
            "event": {
                "containerName": "capmenu",
                "selectedName": "Planning",
                "planningAnchorUnixMs": -1,
            },
        })
        assert isinstance(fallback, Input)
        assert fallback.event.planning_anchor_unix_ms is None

        with pytest.raises(ProtocolError, match="planningAnchorUnixMs"):
            parse_uplink({
                "kind": "input",
                "event": {
                    "containerName": "capdata",
                    "planningAnchorUnixMs": -1,
                },
            })

    def test_productivity_view_rejects_invalid_phase_and_oversized_copy(self) -> None:
        with pytest.raises(ProtocolError, match="phase"):
            ProductivityHudView(  # type: ignore[arg-type]
                domain="calendar",
                phase="invented",
                title="Review",
                primary="Saved",
                secondary="Read-back matched",
                hint="left x2 overview",
            ).to_wire()

    def test_productivity_monitoring_armed_is_reminder_saved_only(self) -> None:
        assert ProductivityHudView(
            domain="reminders",
            phase="saved",
            title="Buy filters",
            primary="Saved and verified",
            secondary="Reminders read-back matched",
            hint="Monitoring next · left x2 Planning",
            monitoring_armed=True,
        ).to_wire()["productivity"]["monitoringArmed"] is True

        with pytest.raises(ProtocolError, match="monitoring"):
            ProductivityHudView(
                domain="calendar",
                phase="saved",
                title="Design review",
                primary="Saved and verified",
                secondary="Calendar read-back matched",
                hint="Planning in 5s · left x2 now",
                monitoring_armed=True,
            ).to_wire()
        with pytest.raises(ProtocolError, match="title"):
            ProductivityHudView(
                domain="calendar",
                phase="saved",
                title="x" * 73,
                primary="Saved",
                secondary="Calendar read-back matched",
                hint="left x2 overview",
            ).to_wire()

    def test_audio_control_omits_source_when_unset(self) -> None:
        assert AudioControl(open=True).to_wire() == {"kind": "audio.control", "open": True}

    def test_imu_trace(self) -> None:
        assert ImuTrace(enabled=True).to_wire() == {"kind": "imu.trace", "enabled": True}

    def test_g2_diagnostic_control(self) -> None:
        assert G2DiagnosticControl(enabled=True).to_wire() == {
            "kind": "g2.diagnostic.control", "enabled": True,
        }

    def test_location_probe_control(self) -> None:
        assert LocationProbeControl(enabled=True).to_wire() == {
            "kind": "location.probe.control", "enabled": True,
        }

    def test_location_refresh_control_is_request_bound(self) -> None:
        assert LocationRefresh(request_id="location-refresh-1").to_wire() == {
            "kind": "location.refresh", "requestId": "location-refresh-1",
        }

    def test_navigation_view_is_structured_bounded_and_generation_bound(self) -> None:
        wire = NavigationView(
            session_id="nav-1",
            request_id="mode-2",
            generation=8,
            navigation=NavigationHudView(
                view="mode",
                primary="Choose travel mode",
                secondary="Cajun Corner · 1.2 km",
                hint="swipe choose · tap continue",
                interaction="stationary",
                options=["Walk · 18 min", "Cycle · 7 min"],
                choice_ids=["walking", "cycling"],
                selected_index=0,
            ),
        ).to_wire()
        assert wire == {
            "kind": "navigation.view",
            "sessionId": "nav-1",
            "requestId": "mode-2",
            "generation": 8,
            "navigation": {
                "view": "mode",
                "primary": "Choose travel mode",
                "secondary": "Cajun Corner · 1.2 km",
                "hint": "swipe choose · tap continue",
                "interaction": "stationary",
                "options": ["Walk · 18 min", "Cycle · 7 min"],
                "choiceIds": ["walking", "cycling"],
                "selectedIndex": 0,
            },
        }

    def test_navigation_route_prefetches_geometry_steps_and_explicit_policy(self) -> None:
        policy = NavigationProgressPolicy(
            max_accuracy_m=20, stale_after_ms=5000, corridor_radius_m=30,
            ambiguity_margin_m=5, ambiguity_progress_delta_m=100,
            search_back_m=120, search_forward_m=500, min_course_displacement_m=5,
            wrong_way_angle_deg=120, wrong_way_min_backtrack_m=15,
            wrong_way_samples=2, off_route_samples=3, arrival_radius_m=15,
            arrival_samples=2, u_turn_suppression_m=50,
        )
        wire = NavigationRoute(
            session_id="nav-1", request_id="route-request-1", generation=10,
            route_id="route-1", revision=1, destination_label="Cajun Corner",
            eta_minutes=18,
            plan=NavigationRoutePlan(
                mode="walking",
                geometry=[
                    NavigationCoordinate(latitude=41.9, longitude=-87.65),
                    NavigationCoordinate(latitude=41.901, longitude=-87.64),
                ],
                steps=[NavigationRouteStep(
                    id="step-1", maneuver="turn_right",
                    instruction="onto Test Ave", distance_along_route_m=500,
                )],
                policy=policy,
            ),
        ).to_wire()
        assert wire["kind"] == "navigation.route"
        assert wire["plan"]["mode"] == "walking"  # type: ignore[index]
        assert wire["plan"]["steps"][0]["maneuver"] == "turn_right"  # type: ignore[index]
        assert wire["plan"]["policy"]["offRouteSamples"] == 3  # type: ignore[index]

    def test_navigation_origin_request_is_generation_bound(self) -> None:
        assert NavigationOriginRequest(
            session_id="nav-1", request_id="origin-1", generation=6,
        ).to_wire() == {
            "kind": "navigation.origin.request", "sessionId": "nav-1",
            "requestId": "origin-1", "generation": 6,
        }

    def test_confirm_request_uses_camel_case_action_id(self) -> None:
        wire = ConfirmRequest(
            action_id="a1",
            prompt="Delete?",
            challenge_id="c1",
            session_id="s1",
            payload_digest="pd",
            preview_digest="vd",
            expires_at_ms=1234,
            diagnostic_kind="messages",
        ).to_wire()
        assert wire == {
            "kind": "confirm.request",
            "actionId": "a1",
            "prompt": "Delete?",
            "challengeId": "c1",
            "sessionId": "s1",
            "payloadDigest": "pd",
            "previewDigest": "vd",
            "expiresAtMs": 1234,
            "diagnosticKind": "messages",
        }

    def test_confirm_request_carries_a_terminal_input_purpose_only_when_set(self) -> None:
        """2026-10-06: a spoken terminal reply is checked on the same gate,
        whose labels then read send / say it again."""
        base = dict(action_id="a1", prompt="Send to work? “fix it”", challenge_id="c1",
                    session_id="s1", payload_digest="pd", preview_digest="vd", expires_at_ms=1)
        assert "purpose" not in ConfirmRequest(**base).to_wire()  # type: ignore[arg-type]
        assert ConfirmRequest(**base, purpose="terminal_input").to_wire()["purpose"] == "terminal_input"  # type: ignore[arg-type]
        with pytest.raises(ProtocolError):
            ConfirmRequest(**base, purpose="anything").to_wire()  # type: ignore[arg-type]
        source = (Path(__file__).resolve().parents[2] / "apps/hub-plugin/src/types.ts").read_text()
        assert "export type ConfirmationPurpose = 'terminal_input';" in source
        assert "purpose?: ConfirmationPurpose" in source

    def test_shutdown_defaults_to_confirmation_dialog(self) -> None:
        # exitMode 1 = show a dialog. Defaulting to 0 would close the app out
        # from under the wearer with no warning.
        assert Shutdown().to_wire()["exitMode"] == 1

    def test_pairing_ack_uplink_parses(self) -> None:
        # The Mac persists the token ONLY on this ack; a drifted key would
        # silently reopen the pairing window forever.
        msg = parse_uplink({"kind": "pairing", "token": "tok"})
        assert isinstance(msg, PairingAck)
        assert msg.token == "tok"

    def test_input_trace_id_survives_the_gateway_parser(self) -> None:
        message = parse_uplink({
            "kind": "input",
            "event": {
                "containerName": "capdata",
                "selectedIndex": 2,
                "selectedName": "MESSAGES",
                "traceId": "hud-11",
                "requestId": "hud-request-7",
            },
        })
        assert message.event.trace_id == "hud-11"  # type: ignore[attr-defined]
        assert message.event.request_id == "hud-request-7"  # type: ignore[attr-defined]

    @pytest.mark.parametrize("request_id", ["", 7, "x" * 129])
    def test_input_request_id_rejects_invalid_generation_identity(
        self, request_id: object
    ) -> None:
        with pytest.raises(ProtocolError, match="requestId"):
            parse_uplink({
                "kind": "input",
                "event": {
                    "containerName": "capmenu",
                    "selectedName": "Messages",
                    "requestId": request_id,
                },
            })

    def test_pairing_ack_requires_a_token(self) -> None:
        with pytest.raises(ProtocolError):
            parse_uplink({"kind": "pairing"})

    def test_dashboard_container_ids_are_pinned(self) -> None:
        # Mirrored in types.ts — a drifted value silently paints the wrong
        # region on the glasses.
        assert ContainerId.CONTEXT == 5
        assert ContainerId.TRAY == 6
        assert ContainerId.HINT == 7

    def test_pairing_set_wire_shape(self) -> None:
        # The plugin's dispatch switches on this exact shape; a drifted key
        # silently strands the phone unpaired.
        assert PairingSet(token="tok").to_wire() == {"kind": "pairing.set", "token": "tok"}


class TestPackPcm:
    def test_round_trips_signed_16_bit_le(self) -> None:
        assert pack_pcm([0, 1, -1]) == b"\x00\x00\x01\x00\xff\xff"

    def test_clamps_rather_than_raising(self) -> None:
        # One bad sample must not discard a whole 200 ms batch.
        assert pack_pcm([40000, -40000]) == b"\xff\x7f\x00\x80"

    def test_empty_is_empty(self) -> None:
        assert pack_pcm([]) == b""


class TestHudTextAppend:
    def test_append_flag_serializes_only_when_true(self) -> None:
        assert "append" not in HudText(text="x").to_wire()
        wire = HudText(text="x", append=True).to_wire()
        assert wire["append"] is True and wire["kind"] == "hud.text"


class TestLocationAndIdentity:
    """New wire shapes for audit findings 12/15/16/21 — mirrored in types.ts."""

    def test_location_parses(self) -> None:
        msg = parse_uplink({"kind": "location", "latitude": 35.2, "longitude": -97.4,
                            "accuracy": 12.5})
        assert isinstance(msg, Location)
        assert (msg.latitude, msg.longitude, msg.accuracy) == (35.2, -97.4, 12.5)

    def test_location_accuracy_is_optional(self) -> None:
        msg = parse_uplink({"kind": "location", "latitude": 1.0, "longitude": 2.0})
        assert isinstance(msg, Location)
        assert msg.accuracy is None

    def test_location_carries_optional_source_time_and_refresh_binding(self) -> None:
        msg = parse_uplink({
            "kind": "location", "latitude": 41.9, "longitude": -87.65,
            "accuracy": 11, "timestamp": 1_725_000_000_123,
            "refreshId": "location-refresh-1",
        })
        assert msg == Location(
            latitude=41.9,
            longitude=-87.65,
            accuracy=11,
            timestamp_ms=1_725_000_000_123,
            refresh_id="location-refresh-1",
        )

    def test_location_rejects_unsafe_extra_fields(self) -> None:
        with pytest.raises(ProtocolError, match="unsupported fields"):
            parse_uplink({
                "kind": "location", "latitude": 41.9, "longitude": -87.65,
                "debug": "must not cross the location boundary",
            })

    def test_hello_user_name_is_optional(self) -> None:
        msg = parse_uplink({"kind": "hello", "sn": "S", "model": "g2",
                            "launchSource": "x", "userName": "Emmanuel"})
        assert isinstance(msg, Hello)
        assert msg.user_name == "Emmanuel"
        bare = parse_uplink({"kind": "hello", "sn": "S", "model": "g2",
                             "launchSource": "x"})
        assert isinstance(bare, Hello)
        assert bare.user_name == ""

    def test_hello_carries_the_phones_time_zone(self) -> None:
        msg = parse_uplink({"kind": "hello", "sn": "S", "model": "g2",
                            "launchSource": "x", "timeZone": "America/Chicago"})
        assert isinstance(msg, Hello)
        assert msg.time_zone == "America/Chicago"
        bare = parse_uplink({"kind": "hello", "sn": "S", "model": "g2",
                             "launchSource": "x"})
        assert isinstance(bare, Hello)
        assert bare.time_zone == ""

    def test_location_carries_the_phones_time_zone(self) -> None:
        # The self-healing path: a wearer who lands mid-session moves zone
        # without reconnecting, so the fix that reports the new position
        # carries the new zone with it.
        msg = parse_uplink({"kind": "location", "latitude": 51.5, "longitude": -0.12,
                            "timeZone": "Europe/London"})
        assert isinstance(msg, Location)
        assert msg.time_zone == "Europe/London"
        bare = parse_uplink({"kind": "location", "latitude": 51.5, "longitude": -0.12})
        assert isinstance(bare, Location)
        assert bare.time_zone is None

    @pytest.mark.parametrize(
        "value",
        ["", "   ", "Not A Zone", "../../etc/passwd", "A" * 65, 42, None, ["Europe/London"]],
    )
    def test_a_malformed_time_zone_is_dropped_not_fatal(self, value: object) -> None:
        # Deliberately not a ProtocolError: this frame also carries the fix
        # that powers weather and grounding, and losing all of that over a
        # cosmetic string would trade a small problem for a real one.
        msg = parse_uplink({"kind": "location", "latitude": 1.0, "longitude": 2.0,
                            "timeZone": value})
        assert isinstance(msg, Location)
        assert msg.time_zone is None
        assert msg.latitude == 1.0

    def test_audio_speaker_is_optional(self) -> None:
        msg = parse_uplink({"kind": "audio", "source": "glasses", "seq": 1,
                            "pcm": [], "speaker": "other"})
        assert isinstance(msg, Audio)
        assert msg.speaker == "other"

    def test_hud_text_color_rides_the_wire_only_when_set(self) -> None:
        assert "textColor" not in HudText(text="x").to_wire()
        assert HudText(text="x", text_color=4).to_wire()["textColor"] == 4

    def test_device_mic_truth_is_optional(self) -> None:
        msg = parse_uplink({"kind": "device", "connect": "connected", "mic": False})
        assert isinstance(msg, Device)
        assert msg.mic is False
        bare = parse_uplink({"kind": "device", "connect": "connected"})
        assert isinstance(bare, Device)
        assert bare.mic is None

    @pytest.mark.parametrize("source", ["glasses", "phone"])
    @pytest.mark.parametrize("mic", [True, False])
    def test_device_mic_source_selection_survives_muting(self, source: str, mic: bool) -> None:
        msg = parse_uplink({"kind": "device", "connect": "connected", "mic": mic,
                            "micSource": source})
        assert isinstance(msg, Device)
        assert msg.mic is mic
        assert msg.mic_source == source

    def test_device_mic_source_is_optional_for_older_clients(self) -> None:
        msg = parse_uplink({"kind": "device", "connect": "connected", "mic": False})
        assert isinstance(msg, Device)
        assert msg.mic_source is None
        assert Device(connect="connected").mic_source is None

    @pytest.mark.parametrize("source", [None, "", "Phone", "microphone", 0, True, [], {}])
    def test_device_mic_source_rejects_invalid_wire_values(self, source: object) -> None:
        with pytest.raises(ProtocolError):
            parse_uplink({"kind": "device", "connect": "connected", "micSource": source})

    def test_device_mic_prompt_id_names_only_an_automatic_stop(self) -> None:
        msg = parse_uplink({"kind": "device", "connect": "connected", "mic": False,
                            "micSource": "phone", "micPromptId": "old_prompt-1"})
        assert isinstance(msg, Device)
        assert msg.mic_prompt_id == "old_prompt-1" and msg.mic_source == "phone"
        manual = parse_uplink({"kind": "device", "connect": "connected", "mic": False})
        assert isinstance(manual, Device) and manual.mic_prompt_id is None

    @pytest.mark.parametrize("prompt_id", [None, "", "private prompt", "x" * 129, 1, True, [], {}])
    def test_device_mic_prompt_id_rejects_invalid_tokens(self, prompt_id: object) -> None:
        with pytest.raises(ProtocolError):
            parse_uplink({"kind": "device", "connect": "connected", "mic": False,
                          "micPromptId": prompt_id})

    @pytest.mark.parametrize("mic", [None, True, 0, "false"])
    def test_device_mic_prompt_id_requires_explicit_false(self, mic: object) -> None:
        with pytest.raises(ProtocolError):
            parse_uplink({"kind": "device", "connect": "connected", "mic": mic,
                          "micPromptId": "old_prompt"})

    def test_auth_accepted_wire_shape(self) -> None:
        assert AuthAccepted().to_wire() == {"kind": "auth.accepted"}


class TestVoiceGenerationConvention:
    def test_voice_request_prefix_matches_the_plugin_constant(self) -> None:
        # apps/hub-plugin/src/types.ts VOICE_REQUEST_PREFIX must equal this;
        # the plugin adopts Mac-initiated lists strictly by this prefix.
        from joat.core.protocol import VOICE_REQUEST_PREFIX

        assert VOICE_REQUEST_PREFIX == "voice-"


class TestHudPanelWire:
    """The overview's tabbed panel. apps/hub-plugin/src/types.ts `hud.panel`
    reads these keys literally, and `panelRequestId` there must mint the same
    string `panel_request_id` mints here — a row tap echoes it back."""

    def test_panel_tabs_match_the_plugin_content_tabs(self) -> None:
        from joat.core.protocol import PANEL_TABS

        # PANEL_CONTENT_TABS in types.ts: the rail's news, pomodoro and
        # quicknotes (QuickNotes replaced Search on the rail, 2026-10-11), and
        # Search, whose page the menu opens. Notifications is never pushed.
        assert set(PANEL_TABS) == {"news", "pomodoro", "search", "quicknotes"}

    def test_hud_panel_wire_shape_and_caps(self) -> None:
        from joat.core.protocol import HudPanel

        wire = HudPanel(
            tab="news",
            revision=7,
            rows=tuple(f"[{i}m] headline {i} " * 10 for i in range(30)),
            headline="H" * 80,
            empty="E" * 80,
        ).to_wire()
        assert wire["kind"] == "hud.panel"
        assert wire["tab"] == "news"
        assert wire["revision"] == 7
        assert len(wire["rows"]) == 20
        assert all(len(row) <= 64 for row in wire["rows"])
        assert wire["headline"] == "H" * 64
        assert wire["empty"] == "E" * 64
        assert "pomodoro" not in wire
        assert "activate" not in wire

    def test_hud_panel_carries_the_timer_in_camel_case(self) -> None:
        from joat.core.protocol import HudPanel, HudPanelPomodoro

        wire = HudPanel(
            tab="pomodoro",
            revision=1,
            rows=("Pause", "Reset"),
            pomodoro=HudPanelPomodoro(
                phase="focus",
                running=True,
                remaining_ms=1_499_000,
                length_ms=1_500_000,
                completed=2,
                ends_at_unix_ms=1_760_000_000_000,
            ),
            activate=True,
        ).to_wire()
        assert wire["pomodoro"] == {
            "phase": "focus",
            "running": True,
            "remainingMs": 1_499_000,
            "lengthMs": 1_500_000,
            "completed": 2,
            "endsAtUnixMs": 1_760_000_000_000,
        }
        assert wire["activate"] is True

    def test_hud_panel_refuses_unknown_tabs_and_phases(self) -> None:
        from joat.core.protocol import HudPanel, HudPanelPomodoro, ProtocolError

        with pytest.raises(ProtocolError):
            HudPanel(tab="notifications", revision=1).to_wire()
        with pytest.raises(ProtocolError):
            HudPanelPomodoro(
                phase="lunch", running=False, remaining_ms=0, length_ms=1, completed=0,
            ).to_wire()
        with pytest.raises(ProtocolError):
            HudPanelPomodoro(
                phase="focus", running=False, remaining_ms=5, length_ms=1, completed=0,
            ).to_wire()

    def test_panel_request_id_round_trips_and_rejects_foreign_ids(self) -> None:
        from joat.core.protocol import panel_request_id, parse_panel_request_id

        assert panel_request_id("news", 12) == "panel-news-12"
        assert parse_panel_request_id("panel-news-12") == ("news", 12)
        assert parse_panel_request_id("panel-search-0") == ("search", 0)
        assert parse_panel_request_id("hud-request-abc") is None
        assert parse_panel_request_id("panel-notifications-1") is None
        assert parse_panel_request_id(None) is None


class TestTheTwoLanguagesDescribeTheSameWire:
    """CLAUDE.md: the wire lives twice by design and both files must change
    in the same commit. Nothing enforced that. An audit found types.ts
    still declaring the pre-split fat Brainstorm frame — endpointHost,
    terminalState, noToolsChecked, approvals on a payload the engine has
    never sent — so the two halves disagreed about the frame they both
    describe and a cross-surface read type-checked.

    Verified by hand once; a hand check protects one commit. This compares
    the ACTUAL serialized payload against the ACTUAL TypeScript interface,
    in both directions, so the next one-sided edit fails here.
    """

    @staticmethod
    def _ts_fields(interface: str) -> set[str]:
        import re
        from pathlib import Path

        source = (Path(__file__).resolve().parents[2]
                  / "apps/hub-plugin/src/types.ts").read_text()
        def fields(name: str) -> set[str]:
            match = re.search(rf"export interface {re.escape(name)}(?: extends (\w+))? \{{(.*?)\n\}}", source, re.S)
            assert match is not None, f"{name} has been renamed or removed"
            # Two-space indent only: nested object literals sit deeper.
            own = set(re.findall(r"^\s{2}(\w+)\??:", match[2], re.M))
            return own | (fields(match[1]) if match[1] else set())
        return fields(interface)

    def test_the_brainstorm_frame_matches_its_typescript_payload(self) -> None:
        view = BrainstormHudView(
            state="ready", topic="t", collaborators=["a"],
            active_speaker="s", activity="x",
        )
        emitted = set(view.to_wire()["brainstorm"].keys())
        declared = self._ts_fields("BrainstormViewPayload")
        assert emitted == declared, (
            f"engine-only: {sorted(emitted - declared)}, "
            f"plugin-only: {sorted(declared - emitted)}"
        )

    def test_messages_ui_receipt_matches_its_typescript_payload(self) -> None:
        from joat.core.protocol import MessageUIView
        wire = MessageUIView('request', 'selection', 'messages-ui-action',
                             'sent', 'Submitted; delivery unverified.').to_wire()
        assert set(wire) == self._ts_fields('MessageUIMessage')
        assert wire['requestId'] == 'request'
        assert wire['selectionId'] == 'selection'
        assert wire['actionId'] == 'messages-ui-action'

    def test_the_terminal_frame_matches_its_typescript_model(self) -> None:
        view = AgentTerminalHudView(
            state="ready", focus="hermes", lanes=["l"], activity="x",
            endpoint_host="h", reason="r", no_tools_checked=True, approvals=1,
            opening_request_id="visit", opening_selection_id="selection",
            conversation_id="conversation", history_truncated=False, input_required=True,
        )
        wire = view.to_wire()
        payload_key = next(k for k in wire if k != "kind")
        emitted = set(wire[payload_key].keys())
        declared = self._ts_fields("AgentTerminalHudModel")
        assert emitted == declared, (
            f"engine-only: {sorted(emitted - declared)}, "
            f"plugin-only: {sorted(declared - emitted)}"
        )

    def test_the_brainstorm_frame_carries_no_terminal_field(self) -> None:
        """docs/SESSION_MODEL.md:84-88, checked against the emitted keys
        rather than the class definition — a producer could add one."""
        view = BrainstormHudView(
            state="ready", topic="t", collaborators=[], activity="x",
        )
        emitted = set(view.to_wire()["brainstorm"].keys())
        forbidden = {
            "endpointHost", "terminalState", "terminalReason",
            "noToolsChecked", "approvals", "focus", "lanes", "screen",
        }
        assert not (emitted & forbidden), sorted(emitted & forbidden)


@pytest.mark.parametrize("request_id,selection_id", [("visit", None), (None, "selection"), ("", "selection"), ("visit", "")])
def test_terminal_opening_requires_a_complete_nonempty_binding(request_id, selection_id):
    view = AgentTerminalHudView(state="ready", focus="work", lanes=[], activity="", opening_request_id=request_id, opening_selection_id=selection_id)
    with pytest.raises(ProtocolError):
        view.to_wire()


class TestHudNotifications:
    """The unread list the glasses' notification card reads. Plugin
    counterpart: HudNotificationsMessage in apps/hub-plugin/src/types.ts."""

    def test_wire_shape(self) -> None:
        from joat.core.protocol import HudNotificationCard, HudNotifications

        card = HudNotificationCard("a" * 32, "WhatsApp", "Elizabeth", "Mom didn't\n pick up.", 1_791_410_291_592, True)
        assert HudNotifications((card,)).to_wire() == {
            "kind": "hud.notifications",
            "cards": [{
                "id": "a" * 32, "app": "WhatsApp", "sender": "Elizabeth",
                "body": "Mom didn't pick up.", "at": 1_791_410_291_592, "urgent": True,
            }],
        }

    def test_bounds(self) -> None:
        from joat.core.protocol import (
            NOTIFICATION_BODY_CHARACTERS, NOTIFICATION_STACK_LIMIT, HudNotificationCard, HudNotifications,
            ProtocolError,
        )

        long = HudNotificationCard("b" * 32, "Mail", "Ann", "word " * 400, 0)
        body = long.to_wire()["body"]
        assert len(body) <= NOTIFICATION_BODY_CHARACTERS and body.endswith("…")
        many = tuple(HudNotificationCard(f"{i:032x}", "Mail", "Ann", "hi", i) for i in range(15))
        assert len(HudNotifications(many).to_wire()["cards"]) == NOTIFICATION_STACK_LIMIT
        with pytest.raises(ProtocolError):
            HudNotificationCard("not-an-id", "Mail", "Ann", "hi", 0).to_wire()
