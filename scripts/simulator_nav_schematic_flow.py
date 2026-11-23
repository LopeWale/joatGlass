#!/usr/bin/env python3
"""Prove the route-schematic navigation HUD on the real evenhub-simulator.

Installs a walking route through downtown Chicago (east on E Randolph St,
right onto N Michigan Ave, left onto E Washington St), drives live
`navigation.fix` positions along it, and captures the glasses at:

  nav-schematic-01-approach-right.png  straight segment approaching a right turn
  nav-schematic-01-after-right.png     just after the right turn
  nav-schematic-01-approach-left.png   approaching a left turn
  nav-schematic-01-after-left.png      just after the left turn

A stray raster `navigation.map` is sent first: the hub must ignore it.

Isolation: binds only loopback ports 18971 / 5271 / 9971.

    PYTHONPATH=.:apps/mac .venv/bin/python -u scripts/simulator_nav_schematic_flow.py --output DIR
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from PIL import Image

from joat.core.protocol import (
    NavigationCoordinate,
    NavigationFix,
    NavigationMapPanel,
    NavigationRoute,
    NavigationRoutePlan,
    NavigationRouteStep,
)
from joat.navigation.map_image import map_move_meters
from joat.navigation.policies import candidate_navigation_policies
from scripts.simulator_agent_terminal_flow import http, until
from scripts.simulator_harness import _prepare_simulator_plugin_root
from scripts.simulator_navigation_flow import MockPhoneBrainServer, wait_http_ready

ROOT = Path(__file__).resolve().parents[1]
ENGINE_PORT, PLUGIN_PORT, SIM_PORT = 18971, 5271, 9971
AUTOMATION = f"http://127.0.0.1:{SIM_PORT}"

GEOMETRY = [
    (41.88435, -87.62955),  # W Randolph St & N Dearborn St
    (41.88435, -87.62782),  # N State St
    (41.88435, -87.62614),  # N Wabash Ave
    (41.88435, -87.62436),  # N Michigan Ave (turn right)
    (41.88322, -87.62436),  # E Washington St (turn left)
    (41.88322, -87.62250),  # Millennium Park entrance
]
M_LAT = 1 / 111_195.0
M_LON = 1 / (111_195.0 * 0.7447)  # cos(41.88°)


async def run(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for port in (ENGINE_PORT, PLUGIN_PORT, SIM_PORT):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))

    brain = MockPhoneBrainServer(ENGINE_PORT)
    processes: list[subprocess.Popen[bytes]] = []

    def start_proc(name: str, args: list[str], env: dict[str, str] | None = None) -> None:
        with (out / f"{name}.log").open("wb") as log:
            processes.append(subprocess.Popen(
                args, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            ))

    async def screen(name: str, settle: float = 2.5) -> Image.Image:
        await asyncio.sleep(settle)
        previous = None
        raw = b""
        for _ in range(20):
            raw = await asyncio.to_thread(http, AUTOMATION + "/api/screenshot/glasses")
            visible = Image.open(io.BytesIO(raw)).tobytes()
            if visible == previous:
                break
            previous = visible
            await asyncio.sleep(0.4)
        source = Image.open(io.BytesIO(raw)).convert("RGBA")
        optical = Image.new("RGBA", source.size, "black")
        optical.alpha_composite(source)
        rgb = optical.convert("RGB")
        rgb.save(out / f"{name}.png")
        print(f"captured {name}", flush=True)
        return rgb

    scratch = tempfile.TemporaryDirectory(prefix="joat-nav-schematic-")
    receipt: dict[str, Any] = {"result": "failed"}
    try:
        await brain.start()
        ws_url = f"ws://127.0.0.1:{ENGINE_PORT}/glasses"
        plugin_root = _prepare_simulator_plugin_root(ROOT, Path(scratch.name), ws_url)
        vite_env = dict(os.environ, VITE_MAC_WS_URL=ws_url)
        start_proc("vite", [
            str(ROOT / "apps/hub-plugin/node_modules/.bin/vite"), str(plugin_root),
            "--config", str(ROOT / "apps/hub-plugin/vite.config.ts"),
            "--host", "127.0.0.1", "--port", str(PLUGIN_PORT), "--strictPort",
        ], env=vite_env)
        await wait_http_ready(f"http://127.0.0.1:{PLUGIN_PORT}/", processes[-1])
        start_proc("simulator", [
            str(Path.home() / ".local/bin/evenhub-simulator"), "--no-aid",
            f"http://127.0.0.1:{PLUGIN_PORT}/", "--automation-port", str(SIM_PORT),
        ])
        await asyncio.wait_for(brain.connected.wait(), 45)
        await asyncio.sleep(2.0)

        session_id = "nav-schematic-1"
        # A stray raster tile from an old producer: must be ignored.
        stray = Image.new("L", (284, 220), 120)
        buf = io.BytesIO()
        stray.save(buf, format="PNG")
        await brain.send(NavigationMapPanel(
            session_id=session_id, latitude=41.8836, longitude=-87.6260, zoom=16,
            width=284, height=220, png_base64=base64.b64encode(buf.getvalue()).decode("ascii"),
        ).to_wire())

        preview_req = f"{session_id}:preview:1"
        mark = len(brain.uplinks)
        await brain.send({
            "kind": "navigation.view", "sessionId": session_id, "requestId": preview_req,
            "generation": 1,
            "navigation": {
                "view": "preview", "mode": "walking", "primary": "Millennium Park",
                "secondary": "Walking · 9 min", "hint": "tap: start", "interaction": "stationary",
                "options": [], "selectedIndex": 0, "maneuver": "depart",
                "remainingDistanceM": 700.0, "etaMinutes": 9.0, "progress": 0.0,
            },
        })
        await until(lambda: brain.latest_kind("navigation.view.ack", mark) is not None, 10)

        along = [0.0]
        for (la1, lo1), (la2, lo2) in zip(GEOMETRY, GEOMETRY[1:]):
            along.append(along[-1] + map_move_meters(la1, lo1, la2, lo2))
        mark = len(brain.uplinks)
        await brain.send(NavigationRoute(
            session_id=session_id, request_id=preview_req, generation=1,
            route_id="schematic-route", revision=1, destination_label="Millennium Park",
            eta_minutes=9.0, phone_fixes=True,
            plan=NavigationRoutePlan(
                mode="walking",
                geometry=[NavigationCoordinate(latitude=la, longitude=lo) for la, lo in GEOMETRY],
                steps=[
                    NavigationRouteStep(id="s1", maneuver="depart", instruction="Head east on E Randolph St",
                                        distance_along_route_m=0.0),
                    NavigationRouteStep(id="s2", maneuver="turn_right", instruction="Turn right onto N Michigan Ave",
                                        distance_along_route_m=round(along[3], 1)),
                    NavigationRouteStep(id="s3", maneuver="turn_left", instruction="Turn left onto E Washington St",
                                        distance_along_route_m=round(along[4], 1)),
                    NavigationRouteStep(id="s4", maneuver="arrive", instruction="Arrive at Millennium Park",
                                        distance_along_route_m=round(along[-1], 1)),
                ],
                policy=candidate_navigation_policies().for_mode("walking"),
            ),
        ).to_wire())
        await until(lambda: brain.latest_kind("navigation.route.ack", mark) is not None, 5)
        ack = brain.latest_kind("navigation.route.ack", mark)
        assert ack and ack.get("outcome") == "installed", ack

        async def fix(lat: float, lon: float, heading: float) -> None:
            await brain.send(NavigationFix(
                session_id=session_id, latitude=lat, longitude=lon, accuracy=6.0,
                heading=heading, speed=1.4, timestamp=float(int(time.time() * 1000)),
            ).to_wire())

        turn_lat, turn_lon = GEOMETRY[3]
        left_lat = GEOMETRY[4][0]
        captures = []
        # Walk east along Randolph in steps (tracker search window), then
        # capture 110 m before the right turn.
        for metres in (300, 220, 160, 110):
            await fix(turn_lat, turn_lon - metres * M_LON, 90.0)
            await asyncio.sleep(1.2)
        captures.append(await screen("nav-schematic-01-approach-right"))
        await fix(turn_lat, turn_lon - 20 * M_LON, 90.0)
        await asyncio.sleep(1.2)
        await fix(turn_lat - 25 * M_LAT, turn_lon, 180.0)
        captures.append(await screen("nav-schematic-01-after-right"))
        await fix(left_lat + 70 * M_LAT, turn_lon, 180.0)
        captures.append(await screen("nav-schematic-01-approach-left"))
        await fix(left_lat + 10 * M_LAT, turn_lon, 180.0)
        await asyncio.sleep(1.2)
        await fix(left_lat, turn_lon + 30 * M_LON, 90.0)
        captures.append(await screen("nav-schematic-01-after-left"))

        # The right pane must carry a bright route and no mid-tone raster fill.
        panes = []
        for image in captures:
            pane = image.crop((292, 40, 576, 248)).getchannel("G")
            values = list(pane.getdata())
            panes.append({
                "bright_px": sum(1 for v in values if v >= 200),
                "midtone_px": sum(1 for v in values if 12 <= v < 200),
            })
        receipt = {"result": "passed", "panes": panes,
                   "states": [m for m in brain.uplinks if m.get("kind") == "navigation.state"]}
    finally:
        (out / "receipt.json").write_text(json.dumps(receipt, indent=2, default=str))
        for proc in reversed(processes):
            if proc.poll() is None:
                os.killpg(proc.pid, 15)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, 9)
        await brain.stop()
        scratch.cleanup()
    print("RESULT", receipt["result"], json.dumps(receipt.get("panes")), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/nav-schematic-simulator")
    args = parser.parse_args()
    asyncio.run(run(args.output.resolve()))
