"""Protocol mirror tests.

These use the exact wire shapes apps/hub-plugin/src/types.ts emits. If the TS
side changes and this file isn't updated in the same commit, these fail — which
is the point: silent protocol drift between the two ends is the failure mode
this whole module exists to prevent.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from joat.ble.hub_gateway import pack_pcm
from joat.core.protocol import (
    Auth,
    AssistantOverlay,
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
    PairingAck,
    PairingSet,
    ProductivityHudView,
    ReminderDueAck,
    Input,
    Policy,
    ProtocolError,
    Shutdown,
    parse_uplink,
)


class TestParseUplink:
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

    def test_brainstorm_action_is_bounded_and_typed(self) -> None:
        message = parse_uplink({"kind": "brainstorm.action", "action": "cancel"})
        assert message == BrainstormAction(action="cancel")

        acceptance = parse_uplink({
            "kind": "brainstorm.action",
            "action": "begin_acceptance",
            "surface": "brainstorm",
            "source": "ring",
            "gesture": "double_press",
        })
        assert acceptance == BrainstormAction(
            action="begin_acceptance",
            surface="brainstorm",
            source="ring",
            gesture="double_press",
        )

    @pytest.mark.parametrize("payload", [
        {"kind": "brainstorm.action", "action": "invent"},
        {"kind": "brainstorm.action", "action": "cancel", "topic": "leak"},
        {"kind": "brainstorm.action", "action": "begin_acceptance"},
        {"kind": "brainstorm.action", "action": "begin_acceptance", "surface": "brainstorm", "source": "left", "gesture": "double_press"},
        {"kind": "brainstorm.action", "action": "begin_acceptance", "surface": "brainstorm", "source": "right", "gesture": "double_press", "topic": "leak"},
        {"kind": "brainstorm.action", "action": "begin_acceptance", "surface": "brainstorm", "source": "right", "gesture": "double_press", "connectionGeneration": 9},
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

    def test_g2_diagnostic_accepts_only_redacted_brainstorm_action_and_frame_receipts(self) -> None:
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
        assert parse_uplink({
            "kind": "g2.diagnostic",
            "event": {
                "type": "visual_frame", "kind": "brainstorm", "surface": "agent_terminal",
                "state": "running", "sha256": "a" * 64,
            },
        }) == G2Diagnostic(event={
            "type": "visual_frame", "kind": "brainstorm", "surface": "agent_terminal",
            "state": "running", "sha256": "a" * 64,
        })
        assert parse_uplink({
            "kind": "g2.diagnostic",
            "event": {
                "type": "visual_frame", "kind": "brainstorm", "surface": "brainstorm",
                "state": "acceptance_ready", "sha256": "b" * 64,
            },
        }) == G2Diagnostic(event={
            "type": "visual_frame", "kind": "brainstorm", "surface": "brainstorm",
            "state": "acceptance_ready", "sha256": "b" * 64,
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
            "dismissAfterMs": 6_000,
        }

    def test_hud_list_enforces_sdk_caps(self) -> None:
        wire = HudList(items=[f"item {i}" * 20 for i in range(30)]).to_wire()
        assert len(wire["items"]) == 20
        assert all(len(s) <= 64 for s in wire["items"])

    def test_hud_list_carries_a_bounded_focus_page_header_and_hint(self) -> None:
        wire = HudList(
            items=["Messages"],
            title="T" * 60,
            hint="H" * 60,
            request_id="hud-request-7",
            selectable=True,
        ).to_wire()
        assert wire["title"] == "T" * 46
        assert wire["hint"] == "H" * 46
        assert wire["requestId"] == "hud-request-7"
        assert wire["selectable"] is True

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
        wire = BrainstormHudView(
            screen="agent_terminal",
            state="ready",
            topic="Hermes Agent Terminal",
            collaborators=[],
            activity="Hermes joins Brainstorm.",
            terminal_state="ready",
            endpoint_host="home-mac.example-tailnet.ts.net",
            terminal_reason="",
            no_tools_checked=True,
        ).to_wire()
        assert wire["kind"] == "brainstorm.view"
        assert wire["brainstorm"] == {
            "screen": "agent_terminal",
            "state": "ready",
            "topic": "Hermes Agent Terminal",
            "collaborators": [],
            "activeSpeaker": "",
            "activity": "Hermes joins Brainstorm.",
            "endpointHost": "home-mac.example-tailnet.ts.net",
            "terminalState": "ready",
            "terminalReason": "",
            "noToolsChecked": True,
        }

        acceptance_ready = BrainstormHudView(
            screen="brainstorm",
            state="acceptance_ready",
            topic="Acceptance run",
            collaborators=["Hermes"],
            activity="Tap to start the Hermes acceptance run",
            terminal_state="ready",
            no_tools_checked=True,
        ).to_wire()
        assert acceptance_ready["brainstorm"]["state"] == "acceptance_ready"

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
        }

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
    def test_auth_accepted_wire_shape(self) -> None:
        assert AuthAccepted().to_wire() == {"kind": "auth.accepted"}
