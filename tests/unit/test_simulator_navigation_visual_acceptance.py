from __future__ import annotations

import struct
import zlib

import pytest

from scripts.simulator_navigation_visual_acceptance import (
    VisualAcceptanceError,
    advance_frame_stability,
    console_messages,
    png_dimensions,
    png_rgba_alpha,
)


def test_png_dimensions_reads_the_authoritative_ihdr() -> None:
    value = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 576, 288)
    assert png_dimensions(value) == (576, 288)


def test_png_dimensions_rejects_non_png_evidence() -> None:
    with pytest.raises(VisualAcceptanceError):
        png_dimensions(b"not a screenshot")


def test_console_messages_ignores_untrusted_non_message_shapes() -> None:
    payload = {"entries": [
        {"message": "[HudVisualSurface] frame 1 bridge-ack"},
        {"message": 7},
        "not an entry",
    ]}
    assert console_messages(payload) == ["[HudVisualSurface] frame 1 bridge-ack"]


def test_framebuffer_must_remain_unchanged_for_the_settle_window() -> None:
    first = advance_frame_stability(None, 0.0, b"partial", 10.0, stable_for_s=1.0)
    assert first[2] is False

    still_partial = advance_frame_stability(
        first[0], first[1], b"partial", 10.9, stable_for_s=1.0,
    )
    assert still_partial[2] is False

    replaced = advance_frame_stability(
        still_partial[0], still_partial[1], b"complete", 11.0, stable_for_s=1.0,
    )
    assert replaced[1] == 11.0
    assert replaced[2] is False

    settled = advance_frame_stability(
        replaced[0], replaced[1], b"complete", 12.0, stable_for_s=1.0,
    )
    assert settled[2] is True


def test_png_rgba_alpha_decodes_the_authoritative_pixel_channel() -> None:
    def chunk(name: bytes, payload: bytes) -> bytes:
        body = name + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"\x00\x00\xff\x00\x00\x00\xff\x00\x09" \
        b"\x00\x00\xff\x00\x80\x00\xff\x00\xff"
    png = b"\x89PNG\r\n\x1a\n" + chunk(
        b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0),
    ) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")

    assert png_rgba_alpha(png) == (2, 2, bytes([0, 9, 128, 255]))
