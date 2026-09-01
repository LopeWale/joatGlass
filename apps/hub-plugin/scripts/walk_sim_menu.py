#!/usr/bin/env python3
"""Gesture-walk the capability menu in the OFFICIAL simulator and capture each step.

Injects touchpad input via POST /api/input and screenshots the LVGL framebuffer
after each action: sidebar scroll, capability select, the loading frame, and the
12s no-Mac retry frame. Proves the interactive state machine, not just statics.

Caveat (verified against the SDK enums): /api/input's "double_click" arrives as
eventSource 1 = RIGHT temple — the platform exit gesture (shutDownPageContainer),
NOT the plugin's left-x2 back. The left temple cannot be injected via this API.

Usage: python3 walk_sim_menu.py OUT_DIR   (vite must be serving :5190)
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from PIL import Image

BASE = "http://127.0.0.1:9898"
SIM = str(Path.home() / ".local/bin/evenhub-simulator")


def get(path: str, timeout: float = 12.0) -> bytes:
    with urllib.request.urlopen(BASE + path, timeout=timeout) as response:
        return response.read()


def post_input(action: str) -> None:
    request = urllib.request.Request(
        BASE + "/api/input",
        data=json.dumps({"action": action}).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(request, timeout=8.0).read()


def lit_pixels(png: bytes) -> int:
    image = Image.open(io.BytesIO(png)).convert("RGBA")
    return sum(1 for value in image.split()[3].tobytes() if value > 0)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)

    def snap(name: str) -> int:
        png = get("/api/screenshot/glasses")
        image = Image.open(io.BytesIO(png))
        black = Image.new("RGBA", image.size, (0, 0, 0, 255))
        black.alpha_composite(image)
        black.convert("RGB").save(out / f"{name}.png")
        count = lit_pixels(png)
        print(name, "lit", count)
        return count

    subprocess.run(["pkill", "-9", "-f", "evenhub-simulator"], check=False)
    time.sleep(2)
    subprocess.Popen(
        [SIM, "http://localhost:5190/?joatVisual=sidebar", "--automation-port", "9898"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            get("/api/ping", timeout=1.0)
            if lit_pixels(get("/api/screenshot/glasses")) > 500:
                break
        except Exception:
            pass
        time.sleep(0.5)
    time.sleep(1.5)

    snap("g0-sidebar-open")
    post_input("down"); time.sleep(0.8)
    snap("g1-scrolled")
    post_input("click"); time.sleep(1.2)
    loading = snap("g2-loading")
    time.sleep(12.5)
    retry = snap("g3-response-timeout")
    if retry <= loading:
        raise SystemExit("retry frame did not appear after the 12s response timeout")
    subprocess.run(["pkill", "-9", "-f", "evenhub-simulator"], check=False)


if __name__ == "__main__":
    main()
