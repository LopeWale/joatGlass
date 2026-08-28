#!/usr/bin/env python3
"""Capture every navigation HUD state through the official Even simulator.

The retained evidence deliberately separates SDK bridge acknowledgement from
the simulator framebuffer. Neither is physical-G2 lens proof.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import time
from typing import cast
import urllib.error
import urllib.request
import zlib

STATES = (
    "nav-destination",
    "nav-repair",
    "nav-mode",
    "nav-preview",
    "nav-guidance",
    "nav-uncertain",
    "nav-wrong-way",
    "nav-off-route",
    "nav-rerouting",
    "nav-arrived",
    "nav-error",
)
FORBIDDEN_CONSOLE_MARKERS = (
    "boot failed:",
    "[HudVisualSurface] frame paint failed",
)
FRAMEBUFFER_STABLE_FOR_S = 1.0


class VisualAcceptanceError(RuntimeError):
    pass


def png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise VisualAcceptanceError("simulator screenshot is not a PNG")
    return struct.unpack(">II", data[16:24])


def png_rgba_alpha(data: bytes) -> tuple[int, int, bytes]:
    """Decode alpha from the simulator's non-interlaced 8-bit RGBA PNG."""

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
            raise VisualAcceptanceError("simulator PNG chunk is truncated")
        payload = data[payload_start:payload_end]
        if kind == b"IHDR":
            if len(payload) != 13:
                raise VisualAcceptanceError("simulator PNG IHDR is invalid")
            (
                decoded_width,
                decoded_height,
                bit_depth,
                color_type,
                compression,
                filter_method,
                interlace,
            ) = struct.unpack(">IIBBBBB", payload)
            if (decoded_width, decoded_height) != (width, height):
                raise VisualAcceptanceError("simulator PNG dimensions disagree")
        elif kind == b"IDAT":
            idat.extend(payload)
        elif kind == b"IEND":
            break
        offset = payload_end + 4
    if (bit_depth, color_type, compression, filter_method, interlace) != (8, 6, 0, 0, 0):
        raise VisualAcceptanceError("simulator screenshot is not non-interlaced RGBA8")
    try:
        filtered = zlib.decompress(idat)
    except zlib.error as exc:
        raise VisualAcceptanceError("simulator PNG pixels are invalid") from exc
    bytes_per_pixel = 4
    stride = width * bytes_per_pixel
    if len(filtered) != height * (stride + 1):
        raise VisualAcceptanceError("simulator PNG scanline size is invalid")

    decoded = bytearray(width * height * bytes_per_pixel)

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
            raise VisualAcceptanceError("simulator PNG uses an invalid row filter")
        row = bytearray(filtered[source + 1:source + 1 + stride])
        prior_start = (row_index - 1) * stride
        for column in range(stride):
            left = row[column - bytes_per_pixel] if column >= bytes_per_pixel else 0
            above = decoded[prior_start + column] if row_index > 0 else 0
            upper_left = (
                decoded[prior_start + column - bytes_per_pixel]
                if row_index > 0 and column >= bytes_per_pixel else 0
            )
            if filter_type == 1:
                row[column] = (row[column] + left) & 0xff
            elif filter_type == 2:
                row[column] = (row[column] + above) & 0xff
            elif filter_type == 3:
                row[column] = (row[column] + ((left + above) // 2)) & 0xff
            elif filter_type == 4:
                row[column] = (row[column] + paeth(left, above, upper_left)) & 0xff
        target = row_index * stride
        decoded[target:target + stride] = row
    return width, height, bytes(decoded[3::4])


def quadrant_lit_pixels(data: bytes) -> tuple[int, int, int, int]:
    width, height, alpha = png_rgba_alpha(data)
    if (width, height) != (576, 288):
        raise VisualAcceptanceError("simulator framebuffer is not 576x288")
    counts = [0, 0, 0, 0]
    for y in range(height):
        for x in range(width):
            if alpha[y * width + x] == 0:
                continue
            quadrant = (2 if y >= 144 else 0) + (1 if x >= 288 else 0)
            counts[quadrant] += 1
    return cast(tuple[int, int, int, int], tuple(counts))


def console_messages(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return []
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return []
    messages: list[str] = []
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("message"), str):
            messages.append(cast(str, entry["message"]))
    return messages


def http_bytes(url: str, *, timeout_s: float = 2.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout_s) as response:
        return cast(bytes, response.read())


def wait_for_ping(base: str, process: subprocess.Popen[bytes], timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise VisualAcceptanceError("Even simulator exited before ping")
        try:
            if http_bytes(f"{base}/api/ping", timeout_s=0.5).strip() == b"pong":
                return
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(0.1)
    raise VisualAcceptanceError("Even simulator did not become ready")


def wait_for_bridge_ack(base: str, timeout_s: float) -> dict[str, object]:
    deadline = time.monotonic() + timeout_s
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        try:
            decoded: object = json.loads(http_bytes(f"{base}/api/console"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            time.sleep(0.1)
            continue
        if isinstance(decoded, dict):
            latest = cast(dict[str, object], decoded)
        if any("[HudVisualSurface]" in message and "bridge-ack" in message
               for message in console_messages(decoded)):
            return latest
        time.sleep(0.1)
    raise VisualAcceptanceError("HUD bridge acknowledgement was not observed")


def advance_frame_stability(
    previous_digest: str | None,
    stable_since: float,
    raw: bytes,
    now: float,
    *,
    stable_for_s: float,
) -> tuple[str, float, bool]:
    """Track when the official simulator framebuffer stops changing.

    The Hub SDK callback acknowledges a bridge request, not simulator pixels.
    A new digest therefore resets the visible-settle window even when the
    callback has already fired.
    """

    digest = hashlib.sha256(raw).hexdigest()
    if digest != previous_digest:
        return digest, now, False
    return digest, stable_since, now - stable_since >= stable_for_s


def wait_for_stable_framebuffer(
    base: str,
    timeout_s: float,
    *,
    stable_for_s: float = FRAMEBUFFER_STABLE_FOR_S,
) -> tuple[bytes, int, float]:
    deadline = time.monotonic() + timeout_s
    previous_digest: str | None = None
    stable_since = 0.0
    sample_count = 0
    latest = b""
    while time.monotonic() < deadline:
        try:
            latest = http_bytes(f"{base}/api/screenshot/glasses")
            png_dimensions(latest)
        except (urllib.error.URLError, TimeoutError, VisualAcceptanceError):
            time.sleep(0.1)
            continue
        sample_count += 1
        now = time.monotonic()
        previous_digest, stable_since, settled = advance_frame_stability(
            previous_digest,
            stable_since,
            latest,
            now,
            stable_for_s=stable_for_s,
        )
        if settled:
            return latest, sample_count, now - stable_since
        time.sleep(0.1)
    raise VisualAcceptanceError("simulator framebuffer did not stabilize after bridge ack")


def stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def capture_state(
    *,
    state: str,
    base_url: str,
    simulator: Path,
    automation_port: int,
    output: Path,
    timeout_s: float,
) -> dict[str, object]:
    safe_url = f"{base_url.rstrip('/')}?joatVisual={state}"
    automation_base = f"http://127.0.0.1:{automation_port}"
    log_path = output / f"{state}.simulator.log"
    with log_path.open("xb") as simulator_log:
        process = subprocess.Popen(
            [str(simulator), safe_url, "--automation-port", str(automation_port)],
            stdout=simulator_log,
            stderr=subprocess.STDOUT,
        )
        try:
            wait_for_ping(automation_base, process, timeout_s)
            console = wait_for_bridge_ack(automation_base, timeout_s)
            bridge_ack_at = time.monotonic()
            raw, framebuffer_samples, stable_duration_s = wait_for_stable_framebuffer(
                automation_base,
                timeout_s,
            )
            bridge_to_stable_s = time.monotonic() - bridge_ack_at
        finally:
            stop_process(process)

    if png_dimensions(raw) != (576, 288):
        raise VisualAcceptanceError(f"{state}: framebuffer is not 576x288")
    quadrant_counts = quadrant_lit_pixels(raw)
    if any(count == 0 for count in quadrant_counts):
        raise VisualAcceptanceError(f"{state}: framebuffer has an empty image quadrant")
    messages = console_messages(console)
    forbidden = [
        marker for marker in FORBIDDEN_CONSOLE_MARKERS
        if any(marker in message for message in messages)
    ]
    if forbidden:
        raise VisualAcceptanceError(f"{state}: console contains {forbidden}")
    bridge_acks = sum(
        "[HudVisualSurface]" in message and "bridge-ack" in message
        for message in messages
    )
    raw_path = output / f"{state}.rgba.png"
    raw_path.write_bytes(raw)
    os.chmod(raw_path, 0o600)
    console_path = output / f"{state}.console.json"
    console_path.write_text(json.dumps(console, indent=2) + "\n")
    os.chmod(console_path, 0o600)

    alpha_path: Path | None = None
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is not None:
        alpha_path = output / f"{state}.png"
        subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(raw_path),
             "-vf", "alphaextract", str(alpha_path)],
            check=True,
        )
        os.chmod(alpha_path, 0o600)

    return {
        "state": state,
        "bridgeAckCount": bridge_acks,
        "bridgeEvidence": "simulator SDK callback only",
        "framebufferEvidence": "official simulator pixels only",
        "framebufferSampleCount": framebuffer_samples,
        "framebufferStableDurationMs": round(stable_duration_s * 1000),
        "bridgeAckToStableCaptureMs": round(bridge_to_stable_s * 1000),
        "quadrantLitPixels": list(quadrant_counts),
        "physicalLensEvidence": "not captured",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "raw": raw_path.name,
        "alpha": alpha_path.name if alpha_path is not None else None,
        "console": console_path.name,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5194/")
    parser.add_argument(
        "--simulator",
        type=Path,
        default=Path.home() / ".local/bin/evenhub-simulator",
    )
    parser.add_argument("--automation-port", type=int, default=9960)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    if not args.simulator.is_file():
        raise VisualAcceptanceError("evenhub-simulator is not installed")
    if args.out.exists() and any(args.out.iterdir()):
        raise VisualAcceptanceError("output directory is not empty")
    args.out.mkdir(parents=True, exist_ok=True, mode=0o700)

    records = [
        capture_state(
            state=state,
            base_url=args.base_url,
            simulator=args.simulator,
            automation_port=args.automation_port + index,
            output=args.out,
            timeout_s=args.timeout,
        )
        for index, state in enumerate(STATES)
    ]
    hashes = {cast(str, record["sha256"]) for record in records}
    if len(hashes) != len(STATES):
        raise VisualAcceptanceError("navigation states did not produce distinct framebuffers")
    summary = {
        "schemaVersion": 1,
        "result": "simulator-pass",
        "stateCount": len(records),
        "distinctFramebufferCount": len(hashes),
        "realG2Result": "not-run",
        "records": records,
    }
    summary_path = args.out / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")
    os.chmod(summary_path, 0o600)
    print(summary_path)


if __name__ == "__main__":
    main()
