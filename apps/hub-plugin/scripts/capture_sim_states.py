#!/usr/bin/env python3
"""Capture ANY joatVisual states through the OFFICIAL EvenHub simulator.

Not the browser preview: this drives the injected bridge —
createStartUpPageContainer, the four-tile updateImageRawData lane, and the
simulator's LVGL framebuffer — so what it saves is what the firmware path
draws, real font metrics included. Screenshots composite the RGBA output onto
black (alpha 0 = unlit lens).

Usage:  python3 capture_sim_states.py OUT_DIR state [state ...]
        python3 capture_sim_states.py OUT_DIR --default
Requires: vite dev server on :5190; Pillow importable (repo .venv has it).
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
DEFAULT_STATES = [
    "calendar",            # the split Planning dashboard
    "calendar-saved",      # outcome pill, calendar domain
    "reminders-saved",     # outcome pill, reminders domain
    "quicknotes",
    "nav-repair",
    "nav-mode",
    "nav-guidance",
]


def get(path: str, timeout: float = 12.0) -> bytes:
    with urllib.request.urlopen(BASE + path, timeout=timeout) as response:
        return response.read()


def lit_pixels(png: bytes) -> int:
    image = Image.open(io.BytesIO(png)).convert("RGBA")
    alpha = image.split()[3]
    return sum(1 for value in alpha.tobytes() if value > 0)


def composite(png: bytes, destination: Path) -> None:
    image = Image.open(io.BytesIO(png))
    black = Image.new("RGBA", image.size, (0, 0, 0, 255))
    black.alpha_composite(image)
    black.convert("RGB").save(destination)


def kill_simulator() -> None:
    """Electron survives SIGTERM and keeps the automation port; kill hard."""
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
                time.sleep(1.2)  # let late tiles land before freezing
                return get("/api/screenshot/glasses")
        except Exception:
            pass
        time.sleep(0.5)
    raise SystemExit(f"no lit frame within {deadline_s}s")


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    out_dir = Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    states = DEFAULT_STATES if sys.argv[2] == "--default" else sys.argv[2:]
    seen: set[bytes] = set()
    for state in states:
        kill_simulator()
        subprocess.Popen(
            [SIM, f"{DEV}?joatVisual={state}", "--automation-port", "9898"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        frame = wait_for_frame()
        if frame in seen:
            raise SystemExit(f"{state}: framebuffer identical to a previous state")
        seen.add(frame)
        composite(frame, out_dir / f"sim-{state}.png")
        print(f"captured {state}")
    kill_simulator()


if __name__ == "__main__":
    main()
