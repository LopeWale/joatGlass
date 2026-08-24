#!/usr/bin/env python3
"""Capture IMU traces from the Hub plugin for nod-threshold calibration.

Stands in for the real Mac brain: accepts the plugin's WebSocket connection,
turns on trace mode, and writes every sample to CSV with a gesture label.

    python3 scripts/imu_capture.py --out traces/nod.csv --label nod

Then perform the labelled gesture repeatedly. Ctrl-C to stop.

Run one file per gesture: nod, shake, walk, still, chew. The threshold you
want is the one that separates `nod` from the loudest non-nod — which is
usually `walk`, not `still`. Calibrating against `still` alone is how you end
up with a confirmer that fires while you cross the street.

Deliberately dependency-light (websockets only) so calibration doesn't wait on
the real brain being built.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import signal
import sys
from pathlib import Path

try:
    import websockets
except ImportError:
    sys.exit("pip install websockets")


class ImuCapture:
    """One capture session: one gesture label, one CSV file."""

    def __init__(self, out: Path, label: str) -> None:
        self.out = out
        self.label = label
        self.count = 0
        self._file = None
        self._writer: csv.writer | None = None

    def __enter__(self) -> "ImuCapture":
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.out.open("w", newline="")
        self._writer = csv.writer(self._file)
        self._writer.writerow(["at", "x", "y", "z", "label"])
        return self

    def __exit__(self, *exc: object) -> None:
        if self._file:
            self._file.close()
        print(f"\n{self.count} samples → {self.out}")

    def write(self, msg: dict) -> None:
        assert self._writer is not None
        self._writer.writerow(
            [msg.get("at"), msg.get("x"), msg.get("y"), msg.get("z"), self.label]
        )
        self.count += 1
        # Flush per sample: a Ctrl-C mid-gesture should not lose the trace.
        self._file.flush()
        print(f"\r{self.count} samples", end="", flush=True)


async def handle(websocket, capture: ImuCapture) -> None:
    print("plugin connected — enabling trace mode")
    await websocket.send(json.dumps({"kind": "imu.trace", "enabled": True}))
    # The plugin auto-starts its mic at boot; a calibration session doesn't
    # want ~16 kB/s of PCM JSON drowning the 10 Hz trace. Mic off, IMU on.
    await websocket.send(json.dumps({"kind": "audio.control", "open": False}))
    await websocket.send(
        json.dumps(
            {
                "kind": "hud.text",
                "containerId": 2,
                "text": f"CALIBRATING\n\nGesture: {capture.label}\nRepeat until told to stop.",
            }
        )
    )

    async for raw in websocket:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if msg.get("kind") == "imu":
            capture.write(msg)
        elif msg.get("kind") == "hello":
            print(f"  device {msg.get('model')} / {msg.get('sn')}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="CSV output path")
    parser.add_argument(
        "--label",
        required=True,
        help="gesture being performed: nod | shake | walk | still | chew",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    with ImuCapture(args.out, args.label) as capture:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        async with websockets.serve(
            lambda ws: handle(ws, capture), args.host, args.port
        ):
            print(f"listening on ws://{args.host}:{args.port} — waiting for the plugin")
            await stop.wait()


if __name__ == "__main__":
    asyncio.run(main())
