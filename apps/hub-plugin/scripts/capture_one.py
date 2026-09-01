#!/usr/bin/env python3
"""Capture ONE simulator state, freshly loaded.

Why this exists: `curl /api/screenshot/glasses` returns whatever the canvas
last painted. Vite HMR swaps the module but nothing repaints, so polling the
screenshot after an edit reads a pre-edit frame and looks like the edit did
nothing. This relaunches the simulator so the current source actually runs.

Usage: python3 capture_one.py STATE OUT.png
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from capture_sim_quicknotes import DEV, composite, launch, wait_for_frame  # noqa: E402


def main() -> None:
    state, out = sys.argv[1], Path(sys.argv[2])
    out.parent.mkdir(parents=True, exist_ok=True)
    launch(f"{DEV}?joatVisual={state}")
    composite(wait_for_frame(), out)
    print(f"captured {out}")


if __name__ == "__main__":
    main()
