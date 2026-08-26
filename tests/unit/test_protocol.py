"""Protocol mirror tests.

These use the exact wire shapes apps/hub-plugin/src/types.ts emits. If the TS
side changes and this file isn't updated in the same commit, these fail — which
is the point: silent protocol drift between the two ends is the failure mode
this whole module exists to prevent.
"""

from __future__ import annotations

import pytest

from jarvis.ble.hub_gateway import pack_pcm
from jarvis.core.protocol import (
    Audio,
    AudioControl,
    Confirm,
    ConfirmRequest,
    ContainerId,
    Device,
    EventSource,
    EventType,
    Hello,
    HudList,
    HudText,
    Imu,
    ImuTrace,
    PairingAck,
    PairingSet,
    Input,
    Policy,
    ProtocolError,
    Shutdown,
    parse_uplink,
)


class TestParseUplink:
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
        msg = parse_uplink({"kind": "confirm", "actionId": "act-1", "accepted": True})
        assert msg == Confirm(action_id="act-1", accepted=True)

    def test_policy(self) -> None:
        assert parse_uplink({"kind": "policy", "resume": "confirm"}) == Policy(resume="confirm")

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

    def test_hud_list_enforces_sdk_caps(self) -> None:
        wire = HudList(items=[f"item {i}" * 20 for i in range(30)]).to_wire()
        assert len(wire["items"]) == 20
        assert all(len(s) <= 64 for s in wire["items"])

    def test_audio_control_omits_source_when_unset(self) -> None:
        assert AudioControl(open=True).to_wire() == {"kind": "audio.control", "open": True}

    def test_imu_trace(self) -> None:
        assert ImuTrace(enabled=True).to_wire() == {"kind": "imu.trace", "enabled": True}

    def test_confirm_request_uses_camel_case_action_id(self) -> None:
        wire = ConfirmRequest(action_id="a1", prompt="Delete?").to_wire()
        assert wire == {"kind": "confirm.request", "actionId": "a1", "prompt": "Delete?"}

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

    def test_pairing_ack_requires_a_token(self) -> None:
        with pytest.raises(ProtocolError):
            parse_uplink({"kind": "pairing"})

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
