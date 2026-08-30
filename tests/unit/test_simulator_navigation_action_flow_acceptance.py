from __future__ import annotations

from dataclasses import replace

import pytest

from scripts.simulator_navigation_action_flow_acceptance import (
    ActionFlowError,
    FlowReceipt,
    is_actionable_location_recovery,
    validate_action_flow,
)


def receipt(
    name: str,
    *,
    input_action: str | None,
    semantic_action: str | None,
    visible_view: str,
) -> FlowReceipt:
    return FlowReceipt(
        name=name,
        input_action=input_action,
        semantic_action=semantic_action,
        visible_view=visible_view,
        bridge_acknowledged=True,
        framebuffer_sha256="a" * 64,
        visible_content_alpha_pixels=2_000,
    )


def test_action_flow_requires_each_simulator_gesture_to_reach_its_expected_visible_state() -> None:
    receipts = [
        receipt(
            "destination",
            input_action=None,
            semantic_action=None,
            visible_view="destination",
        ),
        receipt(
            "mode-walk",
            input_action="click",
            semantic_action="confirm_destination",
            visible_view="mode",
        ),
        receipt(
            "mode-cycle",
            input_action="down",
            semantic_action=None,
            visible_view="mode",
        ),
        receipt(
            "locating",
            input_action="click",
            semantic_action="select_mode",
            visible_view="uncertain",
        ),
        receipt(
            "location-unavailable",
            input_action=None,
            semantic_action=None,
            visible_view="error",
        ),
        receipt(
            "retrying-location",
            input_action="click",
            semantic_action="retry",
            visible_view="uncertain",
        ),
        receipt(
            "location-unavailable-again",
            input_action=None,
            semantic_action=None,
            visible_view="error",
        ),
        receipt(
            "cancelled",
            input_action="down",
            semantic_action="cancel",
            visible_view="overview",
        ),
    ]

    validate_action_flow(receipts)


def test_action_flow_rejects_a_frame_that_changed_without_the_expected_semantic_action() -> None:
    receipts = [
        receipt(
            "destination",
            input_action=None,
            semantic_action=None,
            visible_view="destination",
        ),
        receipt(
            "mode-walk",
            input_action="click",
            semantic_action="wrong_action",
            visible_view="mode",
        ),
    ]

    with pytest.raises(ActionFlowError, match="confirm_destination"):
        validate_action_flow(receipts)


def test_action_flow_rejects_a_changed_but_visually_empty_frame() -> None:
    receipts = [
        receipt(
            "destination",
            input_action=None,
            semantic_action=None,
            visible_view="destination",
        ),
    ]
    receipts[0] = replace(receipts[0], visible_content_alpha_pixels=0)

    with pytest.raises(ActionFlowError, match="visually empty"):
        validate_action_flow(receipts)


def test_location_failure_requires_actionable_even_hub_permission_recovery() -> None:
    assert is_actionable_location_recovery(
        "Phone location unavailable",
        "Allow Location for Even Hub, then retry",
        has_origin_failure=True,
    )
    assert not is_actionable_location_recovery(
        "Location unavailable",
        "Keep the phone foreground and unlocked",
        has_origin_failure=True,
    )
