from __future__ import annotations

import pytest

from scripts.hud_visual_evidence import (
    HudVisualEvidenceError,
    flatten_simulator_rgba,
    validate_optical_preview,
)


def test_alpha_encoded_simulator_green_becomes_an_opaque_optical_preview() -> None:
    # The official simulator encodes its green phosphor intensity in alpha.
    # A normal image viewer otherwise composites it against its own background
    # and can make a real HUD look like a flat green rectangle.
    simulator_rgba = bytes([
        0, 255, 0, 0,
        0, 255, 0, 128,
        0, 255, 0, 255,
        0, 0, 0, 255,
    ])

    assert flatten_simulator_rgba(simulator_rgba) == bytes([
        0, 0, 0, 255,
        0, 128, 0, 255,
        0, 255, 0, 255,
        0, 0, 0, 255,
    ])


def test_visual_evidence_rejects_a_blank_or_flat_optical_preview() -> None:
    with pytest.raises(HudVisualEvidenceError, match="no visible green HUD pixels"):
        validate_optical_preview(bytes([0, 0, 0, 255]) * 4, width=2, height=2)

    with pytest.raises(HudVisualEvidenceError, match="no black HUD background"):
        validate_optical_preview(bytes([0, 255, 0, 255]) * 4, width=2, height=2)


def test_visual_evidence_rejects_transparent_or_non_green_output() -> None:
    with pytest.raises(HudVisualEvidenceError, match="not opaque"):
        validate_optical_preview(bytes([0, 128, 0, 64]) * 4, width=2, height=2)

    with pytest.raises(HudVisualEvidenceError, match="green-on-black"):
        validate_optical_preview(
            bytes([
                0, 0, 0, 255,
                20, 128, 0, 255,
                0, 255, 0, 255,
                0, 0, 0, 255,
            ]),
            width=2,
            height=2,
        )
