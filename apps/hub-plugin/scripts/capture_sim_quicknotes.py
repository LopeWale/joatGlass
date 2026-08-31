#!/usr/bin/env python3
"""Capture QuickNote HUD states through the OFFICIAL EvenHub simulator.

Unlike the browser preview (production renderer, bridge bypassed), this runs
the plugin against the simulator's injected bridge: createStartUpPageContainer,
the four-tile updateImageRawData lane, and its LVGL framebuffer. Screenshots
are the simulator's RGBA output composited onto black (alpha 0 = unlit lens).

Usage: python3 capture_sim_quicknotes.py OUT_DIR
Requires: vite dev server already running on :5190; Pillow importable.
"""

from __future__ import annotations

import io
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from PIL import Image

SIM = str(Path.home() / ".local/bin/evenhub-simulator")
BASE = "http://127.0.0.1:9898"
DEV = "http://localhost:5190/"
STATES = [
    ("1-quicknotes-list", "quicknotes"),
    ("2-quicknote-detail", "quicknote-detail"),
    ("3-quicknote-recording", "quicknote-recording"),
    ("4-quicknote-discard", "quicknote-discard"),
    ("5-quicknote-saving", "quicknote-saving"),
    ("m1-messages-list", "messages"),
    ("m2-messages-conversation", "messages-conversation"),
    ("m3-message-reader", "message-reader"),
    ("m4-messages-compose", "messages-compose"),
]


def get(path: str, timeout: float = 12.0) -> bytes:
    with urllib.request.urlopen(BASE + path, timeout=timeout) as response:
        return response.read()


def post_input(action: str) -> None:
    request = urllib.request.Request(
        BASE + "/api/input",
        data=('{"action": "%s"}' % action).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5.0) as response:
        response.read()


def lit_pixels(png: bytes) -> int:
    image = Image.open(io.BytesIO(png))  # keep RGBA — alpha IS the signal
    return sum(1 for _, _, _, alpha in image.getdata() if alpha > 0)


def composite(png: bytes, destination: Path) -> None:
    image = Image.open(io.BytesIO(png))
    black = Image.new("RGBA", image.size, (0, 0, 0, 255))
    black.alpha_composite(image)
    black.convert("RGB").save(destination)


def kill_simulator() -> None:
    """Electron survives SIGTERM and keeps the automation port — a polite
    terminate makes every 'relaunch' silently reuse the first instance's
    frame. Kill hard and wait until the port actually stops answering."""
    subprocess.run(["pkill", "-9", "-f", "evenhub-simulator"], check=False)
    deadline = time.time() + 10.0
    while time.time() < deadline:
        try:
            get("/api/ping", timeout=1.0)
            time.sleep(0.3)
        except Exception:
            return
    raise SystemExit("a previous simulator is still holding the automation port")


def wait_for_frame(minimum_lit: int = 500, deadline_s: float = 40.0) -> bytes:
    start = time.time()
    last = b""
    while time.time() - start < deadline_s:
        try:
            get("/api/ping", timeout=2.0)
            last = get("/api/screenshot/glasses")
            if lit_pixels(last) >= minimum_lit:
                # One extra beat so late tiles land before we freeze the frame.
                time.sleep(1.2)
                return get("/api/screenshot/glasses")
        except Exception:
            pass
        time.sleep(0.5)
    raise SystemExit(f"no lit frame within {deadline_s}s (last={lit_pixels(last) if last else 'none'})")


def launch(url: str) -> subprocess.Popen[bytes]:
    kill_simulator()
    return subprocess.Popen(
        [SIM, url, "--automation-port", "9898"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def run_state(url: str, out: Path, seen: set[bytes]) -> None:
    launch(url)
    frame = wait_for_frame()
    if frame in seen:
        raise SystemExit(f"{out.name}: framebuffer identical to a previous state — stale instance?")
    seen.add(frame)
    composite(frame, out)
    print(f"captured {out.name}")


def run_gesture_smoke(out_dir: Path) -> None:
    """Real input injection: sidebar → scroll to QuickNotes → tap → focus page."""
    launch(DEV + "?joatVisual=sidebar")
    composite(wait_for_frame(), out_dir / "6-smoke-sidebar.png")
    for _ in range(5):  # Messages → ... → QuickNotes (row 6)
        post_input("down")
        time.sleep(0.4)
    time.sleep(1.2)
    composite(get("/api/screenshot/glasses"), out_dir / "7-smoke-quicknotes-row.png")
    post_input("click")
    time.sleep(2.5)
    composite(get("/api/screenshot/glasses"), out_dir / "8-smoke-after-tap.png")
    print("gesture smoke captured")
    kill_simulator()


def main() -> None:
    out_dir = Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    seen: set[bytes] = set()
    for name, state in STATES:
        run_state(f"{DEV}?joatVisual={state}", out_dir / f"sim-{name}.png", seen)
    run_gesture_smoke(out_dir)


if __name__ == "__main__":
    main()
