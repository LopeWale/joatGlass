#!/usr/bin/env python3
"""Create an opaque, reviewable optical preview from a simulator HUD frame.

The official simulator returns an RGBA PNG whose green phosphor brightness is
held in alpha.  A normal image viewer may composite that against an arbitrary
background, which is not the image the wearer sees.  This tool composites the
frame onto black, preserves only the HUD's green intensity, rejects empty or
flat output, and writes an opaque green-on-black PNG for visual review.

It deliberately does not decide whether a layout is good.  A person must still
inspect the emitted preview before treating a simulator frame as visual proof.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import struct
import zlib


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class HudVisualEvidenceError(RuntimeError):
    """The capture cannot be shown as an honest optical HUD preview."""


@dataclass(frozen=True)
class OpticalPreviewStats:
    width: int
    height: int
    green_pixels: int
    black_pixels: int


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
        raise HudVisualEvidenceError("simulator screenshot is not a PNG")
    return struct.unpack(">II", data[16:24])


def decode_rgba8_png(data: bytes) -> tuple[int, int, bytes]:
    """Decode a non-interlaced 8-bit RGBA screenshot without image libraries."""

    width, height = png_dimensions(data)
    offset = 8
    idat = bytearray()
    bit_depth = color_type = compression = filter_method = interlace = -1
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        payload_start = offset + 8
        payload_end = payload_start + length
        if payload_end + 4 > len(data):
            raise HudVisualEvidenceError("simulator PNG chunk is truncated")
        payload = data[payload_start:payload_end]
        if kind == b"IHDR":
            if len(payload) != 13:
                raise HudVisualEvidenceError("simulator PNG IHDR is invalid")
            decoded = struct.unpack(">IIBBBBB", payload)
            if decoded[:2] != (width, height):
                raise HudVisualEvidenceError("simulator PNG dimensions disagree")
            bit_depth, color_type, compression, filter_method, interlace = decoded[2:]
        elif kind == b"IDAT":
            idat.extend(payload)
        elif kind == b"IEND":
            break
        offset = payload_end + 4

    if (bit_depth, color_type, compression, filter_method, interlace) != (8, 6, 0, 0, 0):
        raise HudVisualEvidenceError("simulator screenshot is not non-interlaced RGBA8")
    try:
        filtered = zlib.decompress(idat)
    except zlib.error as exc:
        raise HudVisualEvidenceError("simulator PNG pixels are invalid") from exc

    bytes_per_pixel = 4
    stride = width * bytes_per_pixel
    if len(filtered) != height * (stride + 1):
        raise HudVisualEvidenceError("simulator PNG scanline size is invalid")
    decoded_rgba = bytearray(width * height * bytes_per_pixel)

    def paeth(left: int, above: int, upper_left: int) -> int:
        prediction = left + above - upper_left
        left_distance = abs(prediction - left)
        above_distance = abs(prediction - above)
        upper_left_distance = abs(prediction - upper_left)
        if left_distance <= above_distance and left_distance <= upper_left_distance:
            return left
        if above_distance <= upper_left_distance:
            return above
        return upper_left

    for row_index in range(height):
        source = row_index * (stride + 1)
        filter_type = filtered[source]
        if filter_type > 4:
            raise HudVisualEvidenceError("simulator PNG uses an invalid row filter")
        row = bytearray(filtered[source + 1:source + 1 + stride])
        prior_start = (row_index - 1) * stride
        for column in range(stride):
            left = row[column - bytes_per_pixel] if column >= bytes_per_pixel else 0
            above = decoded_rgba[prior_start + column] if row_index > 0 else 0
            upper_left = (
                decoded_rgba[prior_start + column - bytes_per_pixel]
                if row_index > 0 and column >= bytes_per_pixel else 0
            )
            if filter_type == 1:
                row[column] = (row[column] + left) & 0xFF
            elif filter_type == 2:
                row[column] = (row[column] + above) & 0xFF
            elif filter_type == 3:
                row[column] = (row[column] + ((left + above) // 2)) & 0xFF
            elif filter_type == 4:
                row[column] = (row[column] + paeth(left, above, upper_left)) & 0xFF
        target = row_index * stride
        decoded_rgba[target:target + stride] = row
    return width, height, bytes(decoded_rgba)


def encode_rgba8_png(width: int, height: int, rgba: bytes) -> bytes:
    if width <= 0 or height <= 0 or len(rgba) != width * height * 4:
        raise HudVisualEvidenceError("opaque preview pixels do not match dimensions")
    stride = width * 4
    rows = b"".join(b"\x00" + rgba[row * stride:(row + 1) * stride] for row in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return PNG_SIGNATURE + _png_chunk(b"IHDR", header) + _png_chunk(b"IDAT", zlib.compress(rows)) + _png_chunk(b"IEND", b"")


def flatten_simulator_rgba(rgba: bytes) -> bytes:
    """Composite alpha-encoded green simulator pixels over a black lens field."""

    if len(rgba) % 4:
        raise HudVisualEvidenceError("simulator RGBA buffer is incomplete")
    preview = bytearray(len(rgba))
    for source in range(0, len(rgba), 4):
        red, green, blue, alpha = rgba[source:source + 4]
        intensity = max(red, green, blue)
        # The simulator's RGBA screenshot is an additive green layer.  Its
        # alpha is therefore brightness over the black optical background.
        composited_green = (intensity * alpha + 127) // 255
        preview[source:source + 4] = bytes((0, composited_green, 0, 255))
    return bytes(preview)


def validate_optical_preview(rgba: bytes, *, width: int, height: int) -> OpticalPreviewStats:
    if len(rgba) != width * height * 4:
        raise HudVisualEvidenceError("optical preview pixels do not match dimensions")
    green_pixels = 0
    black_pixels = 0
    for source in range(0, len(rgba), 4):
        red, green, blue, alpha = rgba[source:source + 4]
        if alpha != 255:
            raise HudVisualEvidenceError("optical preview is not opaque")
        if red != 0 or blue != 0:
            raise HudVisualEvidenceError("optical preview is not green-on-black")
        if green:
            green_pixels += 1
        else:
            black_pixels += 1
    if green_pixels == 0:
        raise HudVisualEvidenceError("optical preview has no visible green HUD pixels")
    if black_pixels == 0:
        raise HudVisualEvidenceError("optical preview has no black HUD background")
    return OpticalPreviewStats(width, height, green_pixels, black_pixels)


def write_optical_preview(input_path: Path, output_path: Path) -> OpticalPreviewStats:
    width, height, simulator_rgba = decode_rgba8_png(input_path.read_bytes())
    preview = flatten_simulator_rgba(simulator_rgba)
    stats = validate_optical_preview(preview, width=width, height=height)
    output_path.write_bytes(encode_rgba8_png(width, height, preview))
    output_path.chmod(0o600)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="official simulator RGBA PNG")
    parser.add_argument("--output", type=Path, required=True, help="opaque green-on-black review PNG")
    args = parser.parse_args()
    stats = write_optical_preview(args.input, args.output)
    print(json.dumps({
        "width": stats.width,
        "height": stats.height,
        "greenPixels": stats.green_pixels,
        "blackPixels": stats.black_pixels,
        "result": "review-required",
    }))


if __name__ == "__main__":
    main()
