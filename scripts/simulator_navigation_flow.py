#!/usr/bin/env python3
"""Drive the Complete Navigation flow through evenhub-simulator.

Exercises and visually captures on `evenhub-simulator`:
  1. `01-navigation-list.png`               — Selectable `NAVIGATION` firmware list showing
                                              `➤ Navigate to a place…`, `➤ Home · ...`, `➤ Work · ...`
  2. `02-where-to-prompt.png`               — `prompt.present` (`ownerKind: "capability"`, `"Where to?"`)
                                              + live `prompt.partial` (`"drive to Millennium Park"`) with
                                              `'tap: search · 2x tap: cancel'`, plus `2x tap` cancel
                                              leaving row `1` (`➤ Home`) tappable (`selectedIndex: 1`)
  3. `03-travel-mode-picker.png`            — 3-option `TRAVEL MODE` (`▶ Walk`, `Cycle`, `Drive`) + selecting `Drive`
  4. `04-drive-route-preview.png`           — `DRIVE · ROUTE PREVIEW` card
  5. `05-active-guidance-with-street-map.png` — Active `DRIVING · ACTIVE` guidance (`phoneFixes: true`) with a real
                                              4-bit G2 Web Mercator street tile (`284×220`) via `navigation.map`,
                                              single-source `navigation.fix`, privacy-clean enriched `navigation.state`
                                              validated by `joat.core.protocol.parse_uplink`, and `navigation.resume`
                                              reclaiming the G2 screen after opening `NAVIGATION` mid-route!

Isolation: binds only dedicated loopback ports (`18993`, `5298`, `9996`).

    PYTHONPATH=.:apps/mac .venv/bin/python -u scripts/simulator_navigation_flow.py
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import json
import math
import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw
import websockets

from joat.core.protocol import (
    NavigationCoordinate,
    NavigationFix,
    NavigationMapPanel,
    NavigationResume,
    NavigationRoute,
    NavigationRoutePlan,
    NavigationRouteStep,
    NavigationState,
    parse_uplink,
)
from joat.navigation.map_image import _tone_curve, map_move_meters
from joat.navigation.policies import candidate_navigation_policies
from scripts.simulator_agent_terminal_flow import OCR_SOURCE, http, until
from scripts.simulator_harness import _prepare_simulator_plugin_root

ROOT = Path(__file__).resolve().parents[1]
ENGINE_PORT, PLUGIN_PORT, SIM_PORT = 18993, 5298, 9996
AUTOMATION = f"http://127.0.0.1:{SIM_PORT}"
MAX_MERCATOR_LATITUDE = 85.05112878


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def mercator_world_pixel(latitude: float, longitude: float, zoom: int) -> tuple[float, float]:
    """Exact 512*2^zoom Web Mercator pixel matching NavigationMapTiles & HudCanvasRenderer."""
    world_pixels = 512.0 * (2.0 ** zoom)
    clamped = max(-MAX_MERCATOR_LATITUDE, min(MAX_MERCATOR_LATITUDE, latitude))
    sin_lat = math.sin(math.radians(clamped))
    x = ((longitude + 180.0) / 360.0) * world_pixels
    y = (0.5 - math.log((1.0 + sin_lat) / (1.0 - sin_lat)) / (4.0 * math.pi)) * world_pixels
    return x, y


def render_real_g2_street_tile(
    latitude: float,
    longitude: float,
    zoom: int = 15,
    width: int = 284,
    height: int = 220,
) -> bytes:
    """Stitch real 256x256 Web Mercator dark-gray street tiles at z = min(16, zoom + 1)
    (exact 512 * 2^zoom Web Mercator scale) and apply JOAT's 4-bit G2 tone curve
    (levels 1..11 * 17), matching NavigationMapTiles on iOS.
    """
    z = min(16, zoom + 1)
    tile_draw_px = 256 * (1 << max(0, (zoom + 1) - z))
    num_tiles = 1 << z
    cx, cy = mercator_world_pixel(latitude, longitude, zoom)
    top_left_x = cx - width / 2.0
    top_left_y = cy - height / 2.0
    bottom_right_x = top_left_x + width
    bottom_right_y = top_left_y + height

    min_tx = int(math.floor(top_left_x / tile_draw_px))
    max_tx = int(math.floor((bottom_right_x - 1e-9) / tile_draw_px))
    min_ty = max(0, int(math.floor(top_left_y / tile_draw_px)))
    max_ty = min(num_tiles - 1, int(math.floor((bottom_right_y - 1e-9) / tile_draw_px)))

    canvas = Image.new("L", (width, height), 0)
    fetched_all = True
    for ty in range(min_ty, max_ty + 1):
        for raw_x in range(min_tx, max_tx + 1):
            wrapped_x = ((raw_x % num_tiles) + num_tiles) % num_tiles
            url = (
                "https://services.arcgisonline.com/ArcGIS/rest/services/"
                f"Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{ty}/{wrapped_x}"
            )
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "JOATBrain/1.0", "Accept": "image/png"},
            )
            try:
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    tile_bytes = resp.read()
                tile_img = Image.open(io.BytesIO(tile_bytes)).convert("L")
                if tile_draw_px != 256:
                    tile_img = tile_img.resize(
                        (tile_draw_px, tile_draw_px), Image.Resampling.BILINEAR
                    )
                sx = int(round(raw_x * tile_draw_px - top_left_x))
                sy = int(round(ty * tile_draw_px - top_left_y))
                canvas.paste(tile_img, (sx, sy))
            except Exception:
                fetched_all = False
                break
        if not fetched_all:
            break

    if not fetched_all:
        # Offline deterministic Chicago Loop grid fallback aligned to the same Mercator projection
        draw = ImageDraw.Draw(canvas)
        for bx in range(16, width - 16, 54):
            for by in range(14, height - 14, 42):
                draw.rectangle((bx, by, bx + 38, by + 28), fill=28)
        for gx in range(10, width, 54):
            draw.line((gx, 0, gx, height), fill=110, width=3)
        for gy in range(8, height, 42):
            draw.line((0, gy, width, gy), fill=110, width=3)

    toned = _tone_curve(canvas)
    buf = io.BytesIO()
    toned.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


async def wait_http_ready(url: str, proc: subprocess.Popen[bytes], timeout_s: float = 25.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Vite exited early with code {proc.returncode}")
        try:
            await asyncio.to_thread(lambda: urllib.request.urlopen(url, timeout=2.0).read())
            return
        except (OSError, urllib.error.URLError):
            await asyncio.sleep(0.15)
    raise RuntimeError(f"Timed out waiting for {url}")


class MockPhoneBrainServer:
    """Loopback `/glasses` server speaking the exact JOAT Brain wire protocol."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.connected = asyncio.Event()
        self.uplinks: list[dict[str, Any]] = []
        self.downlinks: list[dict[str, Any]] = []
        self.mic_source = "glasses"
        self.generation = "phone-sim01153"
        self._ws: Any = None
        self._server: Any = None

    async def start(self) -> None:
        self._server = await websockets.serve(self._handle, "127.0.0.1", self.port, max_size=2**22)

    async def stop(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def send(self, frame: dict[str, Any]) -> None:
        assert self._ws is not None, "No authenticated plugin WebSocket connected"
        self.downlinks.append(frame)
        await self._ws.send(json.dumps(frame))

    def latest_input(self, container_name: str, after_index: int = 0) -> dict[str, Any] | None:
        for msg in reversed(self.uplinks[after_index:]):
            if msg.get("kind") == "input":
                ev = msg.get("event") or {}
                if ev.get("containerName") == container_name:
                    return ev
        return None

    def latest_kind(self, kind: str, after_index: int = 0) -> dict[str, Any] | None:
        for msg in reversed(self.uplinks[after_index:]):
            if msg.get("kind") == kind:
                return msg
        return None

    async def _handle(self, ws: Any) -> None:
        self._ws = ws
        authed = False
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                msg = json.loads(raw)
                kind = msg.get("kind")
                if not authed:
                    if kind == "auth":
                        authed = True
                        await ws.send(json.dumps({"kind": "auth.accepted"}))
                    continue
                if kind == "audio":
                    continue
                self.uplinks.append(msg)
                if kind == "device":
                    src = msg.get("micSource")
                    if src in ("glasses", "phone"):
                        self.mic_source = src
                elif kind == "hello":
                    self.connected.set()
        except websockets.ConnectionClosed:
            pass


async def run(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.png"):
        old.unlink()
    for port in (ENGINE_PORT, PLUGIN_PORT, SIM_PORT):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))

    (out / "ocr.swift").write_text(OCR_SOURCE)
    subprocess.run(
        ["swiftc", str(out / "ocr.swift"), "-o", str(out / "ocr")],
        check=True,
        capture_output=True,
    )

    brain = MockPhoneBrainServer(ENGINE_PORT)
    processes: list[subprocess.Popen[bytes]] = []
    screens: list[dict[str, Any]] = []

    def start_proc(name: str, args: list[str], env: dict[str, str] | None = None) -> subprocess.Popen[bytes]:
        with (out / f"{name}.log").open("wb") as log:
            proc = subprocess.Popen(
                args, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
            )
            processes.append(proc)
            return proc

    async def press(action: str, delay: float = 0.65) -> None:
        await asyncio.to_thread(http, AUTOMATION + "/api/input", {"action": action})
        await asyncio.sleep(delay)

    async def screen(name: str, settle: float = 1.0) -> tuple[str, Image.Image]:
        await asyncio.sleep(settle)
        previous = None
        raw = b""
        for _ in range(20):
            raw = await asyncio.to_thread(http, AUTOMATION + "/api/screenshot/glasses")
            visible = Image.open(io.BytesIO(raw)).crop((0, 30, 576, 288)).tobytes()
            if visible == previous:
                break
            previous = visible
            await asyncio.sleep(0.3)
        source = Image.open(io.BytesIO(raw)).convert("RGBA")
        optical = Image.new("RGBA", source.size, "black")
        optical.alpha_composite(source)
        rgb = optical.convert("RGB")
        rgb.save(out / f"{name}.png")
        ocr_channel = source.getchannel("A").point(lambda p: 255 if p > 8 else 0)
        ocr_channel.resize((2304, 1152)).save(out / "ocr-input.png")
        ocr = await asyncio.to_thread(
            subprocess.run,
            [str(out / "ocr"), str(out / "ocr-input.png")],
            check=True,
            capture_output=True,
            text=True,
        )
        text = "\n".join(json.loads(ocr.stdout))
        screens.append({"name": name, "text": text})
        print(f"--- SCREEN {name}\n{text}\n", flush=True)
        return text, rgb

    scratch = tempfile.TemporaryDirectory(prefix="joat-nav-sim-")
    receipt: dict[str, Any] = {"result": "failed"}
    try:
        await brain.start()
        ws_url = f"ws://127.0.0.1:{ENGINE_PORT}/glasses"
        plugin_root = _prepare_simulator_plugin_root(ROOT, Path(scratch.name), ws_url)
        vite_env = dict(os.environ, VITE_MAC_WS_URL=ws_url)
        vite_proc = start_proc("vite", [
            str(ROOT / "apps/hub-plugin/node_modules/.bin/vite"),
            str(plugin_root),
            "--config", str(ROOT / "apps/hub-plugin/vite.config.ts"),
            "--host", "127.0.0.1",
            "--port", str(PLUGIN_PORT),
            "--strictPort",
        ], env=vite_env)
        await wait_http_ready(f"http://127.0.0.1:{PLUGIN_PORT}/", vite_proc)

        start_proc("simulator", [
            str(Path.home() / ".local/bin/evenhub-simulator"),
            "--no-aid",
            f"http://127.0.0.1:{PLUGIN_PORT}/?joatTestPromptMic=1",
            "--automation-port", str(SIM_PORT),
        ])
        await asyncio.wait_for(brain.connected.wait(), 45)
        await asyncio.sleep(2.0)

        # Verify empty Places store did NOT emit a `places` uplink on announce (Review Focus #2)
        assert brain.latest_kind("places") is None, "Empty plugin places store must NOT emit a `places` uplink"

        # 1. Contextual Menu -> `Navigation` (index 3: Messages, Mail, Reminders, Navigation)
        await press("context_menu")
        for _ in range(3):
            await press("down", delay=0.4)
        mark = len(brain.uplinks)
        await press("click")
        await until(lambda: brain.latest_input("capmenu", mark) is not None, 10)
        capmenu_ev = brain.latest_input("capmenu", mark)
        assert capmenu_ev is not None
        assert capmenu_ev.get("selectedName") == "Navigation", (
            f"Expected capmenu selectedName=='Navigation', got {capmenu_ev}"
        )
        req_id = capmenu_ev["requestId"]

        nav_items = [
            "➤ Navigate to a place…",
            "➤ Home · 333 W Wolf Point Plaza, Chicago",
            "➤ Work · 233 S Wacker Dr, Chicago",
        ]
        await brain.send({
            "kind": "hud.list",
            "title": "NAVIGATION",
            "items": nav_items,
            "hint": "tap: choose · left x2: back",
            "selectable": True,
            "requestId": req_id,
        })
        nav_list_text, _ = await screen("01-navigation-list", settle=1.0)
        lower_list = nav_list_text.lower()
        assert "navigate to a place" in lower_list and "home" in lower_list and "work" in lower_list, (
            f"Expected Navigate to a place, Home, and Work on 01-navigation-list, got:\n{nav_list_text}"
        )

        # 2. Tap row 0 (`➤ Navigate to a place…`) -> `prompt.present` (`ownerKind: "capability"`, `"Where to?"`)
        #    + `prompt.partial` (`"drive to Millennium Park"`) -> `02-where-to-prompt.png`
        mark = len(brain.uplinks)
        await press("click")
        await until(lambda: brain.latest_input("capdata", mark) is not None, 10)
        row0_ev = brain.latest_input("capdata", mark)
        assert row0_ev is not None
        assert row0_ev.get("selectedName") == "NAVIGATION"
        assert row0_ev.get("selectedIndex") == 0, f"Expected selectedIndex==0, got {row0_ev}"

        prompt_id = "prompt01153nav"
        question = "Where to?"
        mark_prompt = len(brain.uplinks)
        await brain.send({
            "kind": "prompt.present",
            "promptId": prompt_id,
            "question": question,
            "presentationBinding": sha256_hex(f"{prompt_id}|{question}|{brain.generation}"),
            "connectionGeneration": brain.generation,
            "source": brain.mic_source,
            "expiresInMs": 30000,
            "ownerKind": "capability",
            "ownerId": "nav-01153aabb",
            "field": "destination",
        })
        await until(lambda: brain.latest_kind("prompt.ack", mark_prompt) is not None, 10)
        prompt_ack = brain.latest_kind("prompt.ack", mark_prompt)
        assert prompt_ack is not None and prompt_ack.get("success") is True, f"Expected prompt.ack success, got {prompt_ack}"

        await brain.send({
            "kind": "prompt.partial",
            "promptId": prompt_id,
            "connectionGeneration": brain.generation,
            "text": "drive to",
        })
        await asyncio.sleep(0.25)
        await brain.send({
            "kind": "prompt.partial",
            "promptId": prompt_id,
            "connectionGeneration": brain.generation,
            "text": "drive to Millennium Park",
        })
        prompt_text, _ = await screen("02-where-to-prompt", settle=0.9)
        lower_prompt = prompt_text.lower()
        assert "where to?" in lower_prompt and "millennium park" in lower_prompt, (
            f"Expected 'Where to?' and 'drive to Millennium Park' on 02-where-to-prompt, got:\n{prompt_text}"
        )
        assert "tap: search" in lower_prompt and "cancel" in lower_prompt, (
            f"Expected 'tap: search · 2x tap: cancel' footer on 02-where-to-prompt, got:\n{prompt_text}"
        )

        # Cancel `"Where to?"` via `2x tap` and prove `NAVIGATION` list returns AND row 1 (`➤ Home`) emits selectedIndex: 1!
        mark_cancel = len(brain.uplinks)
        await press("double_click", delay=0.9)
        await until(lambda: brain.latest_kind("prompt.cancel", mark_cancel) is not None, 10)

        mark_home = len(brain.uplinks)
        await press("down", delay=0.5)
        await press("click")
        await until(lambda: brain.latest_input("capdata", mark_home) is not None, 10)
        home_ev = brain.latest_input("capdata", mark_home)
        assert home_ev is not None
        assert home_ev.get("selectedName") == "NAVIGATION"
        assert home_ev.get("selectedIndex") == 1, (
            f"Expected tapping row 1 (`➤ Home`) to emit selectedIndex==1, got {home_ev}"
        )

        # 3. 3-option `TRAVEL MODE` (`▶ Walk`, `Cycle`, `Drive`) -> `03-travel-mode-picker.png`
        session_id = "nav-sim01153"
        mode_req_id = f"{session_id}:mode:1"
        mark_mode_ack = len(brain.uplinks)
        await brain.send({
            "kind": "navigation.view",
            "sessionId": session_id,
            "requestId": mode_req_id,
            "generation": 1,
            "navigation": {
                "view": "mode",
                "primary": "Choose travel mode",
                "secondary": "Millennium Park",
                "hint": "tap choose · swipe select · say walk/cycle/drive",
                "interaction": "stationary",
                "options": ["Walk", "Cycle", "Drive"],
                "choiceIds": ["walking", "cycling", "driving"],
                "selectedIndex": 0,
            },
        })
        await until(lambda: brain.latest_kind("navigation.view.ack", mark_mode_ack) is not None, 10)
        mode_text, _ = await screen("03-travel-mode-picker", settle=0.9)
        lower_mode = mode_text.lower()
        assert "travel mode" in lower_mode and "walk" in lower_mode and "cycle" in lower_mode and "drive" in lower_mode, (
            f"Expected TRAVEL MODE with Walk, Cycle, Drive on 03-travel-mode-picker, got:\n{mode_text}"
        )

        # Select `Drive` (index 2) via two downward swipes + tap -> verify `choiceId: "driving"`
        mark_select_drive = len(brain.uplinks)
        await press("down", delay=0.45)
        await press("down", delay=0.45)
        await press("click")
        await until(lambda: brain.latest_kind("navigation.action", mark_select_drive) is not None, 10)
        drive_act = brain.latest_kind("navigation.action", mark_select_drive)
        assert drive_act is not None
        assert drive_act.get("action") == "select_mode" and drive_act.get("choiceId") == "driving", (
            f"Expected select_mode choiceId=='driving', got {drive_act}"
        )

        # 4. `DRIVE · ROUTE PREVIEW` card -> `04-drive-route-preview.png`
        preview_req_id = f"{session_id}:preview:2"
        mark_preview_ack = len(brain.uplinks)
        await brain.send({
            "kind": "navigation.view",
            "sessionId": session_id,
            "requestId": preview_req_id,
            "generation": 2,
            "navigation": {
                "view": "preview",
                "mode": "driving",
                "primary": "Millennium Park",
                "secondary": "Driving · 4 min",
                "hint": "tap/say start · swipe up/down to edit",
                "interaction": "stationary",
                "options": [],
                "selectedIndex": 0,
                "maneuver": "depart",
                "remainingDistanceM": 640.0,
                "etaMinutes": 4.0,
                "progress": 0.0,
            },
        })
        await until(lambda: brain.latest_kind("navigation.view.ack", mark_preview_ack) is not None, 10)
        preview_text, _ = await screen("04-drive-route-preview", settle=0.9)
        lower_preview = preview_text.lower()
        assert "drive" in lower_preview and "route preview" in lower_preview and "millennium park" in lower_preview, (
            f"Expected 'DRIVE · ROUTE PREVIEW' and 'Millennium Park' on 04-drive-route-preview, got:\n{preview_text}"
        )

        mark_start_act = len(brain.uplinks)
        await press("click")
        await until(lambda: brain.latest_kind("navigation.action", mark_start_act) is not None, 10)
        start_act = brain.latest_kind("navigation.action", mark_start_act)
        assert start_act is not None and start_act.get("action") == "start", f"Expected start action, got {start_act}"

        # 5. Install `driving` route (`phoneFixes: true`) + real 4-bit G2 street tile (`284x220` zoom 15)
        #    along downtown Chicago E Randolph St -> turning right (south) onto N Michigan Ave!
        geometry_pts = [
            (41.88435, -87.62955),  # W Randolph St & N Dearborn St
            (41.88435, -87.62782),  # N State St & E Randolph St
            (41.88435, -87.62614),  # N Wabash Ave & E Randolph St
            (41.88435, -87.62436),  # N Michigan Ave & E Randolph St (Turn right!)
            (41.88322, -87.62436),  # N Michigan Ave & E Washington St
            (41.88195, -87.62436),  # S Michigan Ave & E Madison St (Millennium Park)
        ]
        along = [0.0]
        for (la1, lo1), (la2, lo2) in zip(geometry_pts, geometry_pts[1:]):
            along.append(along[-1] + map_move_meters(la1, lo1, la2, lo2))
        turn_m = round(along[3], 1)
        total_m = round(along[-1], 1)

        tile_centre_lat, tile_centre_lon = 41.88360, -87.62600
        tile_png = await asyncio.to_thread(
            render_real_g2_street_tile, tile_centre_lat, tile_centre_lon, 15, 284, 220
        )
        (out / "rendered-street-tile-284x220.png").write_bytes(tile_png)

        # Verify 4-bit G2 tone curve contract on the tile itself (levels <= 11*17 == 187)
        tile_check = Image.open(io.BytesIO(tile_png)).convert("L")
        assert tile_check.size == (284, 220), f"Unexpected tile size {tile_check.size}"
        tile_extrema = tile_check.getextrema()
        assert tile_extrema[1] <= 187 and tile_extrema[1] > 0, (
            f"Expected street tile levels in 1..187 (reserving 204..255 for route rails), got extrema {tile_extrema}"
        )

        await brain.send(
            NavigationMapPanel(
                session_id=session_id,
                latitude=tile_centre_lat,
                longitude=tile_centre_lon,
                zoom=15,
                width=284,
                height=220,
                png_base64=base64.b64encode(tile_png).decode("ascii"),
            ).to_wire()
        )
        await asyncio.sleep(0.25)

        driving_policy = candidate_navigation_policies().for_mode("driving")
        route_id = "mk-millennium-drive-1"
        mark_route = len(brain.uplinks)
        install_t0 = time.monotonic()
        await brain.send(
            NavigationRoute(
                session_id=session_id,
                request_id=preview_req_id,
                generation=2,
                route_id=route_id,
                revision=1,
                destination_label="Millennium Park",
                eta_minutes=4.0,
                phone_fixes=True,
                plan=NavigationRoutePlan(
                    mode="driving",
                    geometry=[NavigationCoordinate(latitude=la, longitude=lo) for la, lo in geometry_pts],
                    steps=[
                        NavigationRouteStep(
                            id="step-1",
                            maneuver="depart",
                            instruction="Head east on E Randolph St",
                            distance_along_route_m=0.0,
                        ),
                        NavigationRouteStep(
                            id="step-2",
                            maneuver="turn_right",
                            instruction="Turn right onto N Michigan Ave",
                            distance_along_route_m=turn_m,
                        ),
                        NavigationRouteStep(
                            id="step-3",
                            maneuver="arrive",
                            instruction="Arrive at Millennium Park",
                            distance_along_route_m=total_m,
                        ),
                    ],
                    policy=driving_policy,
                ),
            ).to_wire()
        )
        await until(lambda: brain.latest_kind("navigation.route.ack", mark_route) is not None, 5)
        install_elapsed_ms = round((time.monotonic() - install_t0) * 1000, 1)
        route_ack = brain.latest_kind("navigation.route.ack", mark_route)
        assert route_ack is not None and route_ack.get("outcome") == "installed", (
            f"Expected navigation.route.ack installed, got {route_ack}"
        )
        assert install_elapsed_ms < 1000.0, (
            f"phoneFixes: true route install should not wait on LocationSessionManager, took {install_elapsed_ms} ms"
        )

        # Send single-source `navigation.fix` along E Randolph St ~185 m before N Michigan Ave turn
        mark_state = len(brain.uplinks)
        await brain.send(
            NavigationFix(
                session_id=session_id,
                latitude=41.88435,
                longitude=-87.62660,
                accuracy=8.0,
                heading=90.0,
                speed=11.2,
                timestamp=float(int(time.time() * 1000)),
            ).to_wire()
        )
        await until(lambda: brain.latest_kind("navigation.state", mark_state) is not None, 10)
        nav_state = brain.latest_kind("navigation.state", mark_state)
        assert nav_state is not None
        assert "latitude" not in nav_state and "longitude" not in nav_state, (
            f"Privacy violation: coordinates leaked in navigation.state: {nav_state}"
        )
        for required_field in (
            "maneuver",
            "maneuverDistanceM",
            "instruction",
            "stepId",
            "remainingDistanceM",
            "etaMinutes",
        ):
            assert required_field in nav_state, f"Missing {required_field} in navigation.state: {nav_state}"
        assert nav_state["maneuver"] == "turn_right"
        assert nav_state["stepId"] == "step-2"
        assert nav_state["instruction"] == "Turn right onto N Michigan Ave"

        parsed_state = parse_uplink(nav_state)
        assert isinstance(parsed_state, NavigationState), f"Expected NavigationState from parse_uplink, got {parsed_state}"

        guidance_text, guidance_rgb = await screen("05-active-guidance-with-street-map", settle=1.1)
        lower_guidance = guidance_text.lower()
        assert "driving" in lower_guidance and "active" in lower_guidance, (
            f"Expected 'DRIVING · ACTIVE' on 05-active-guidance-with-street-map, got:\n{guidance_text}"
        )
        assert ("turn right" in lower_guidance or "tum right" in lower_guidance) and "michigan" in lower_guidance, (
            f"Expected maneuver copy on 05-active-guidance-with-street-map, got:\n{guidance_text}"
        )
        assert "api key" not in lower_guidance and "not yet available" not in lower_guidance, (
            f"Unexpected tile watermark/placeholder on 05-active-guidance-with-street-map:\n{guidance_text}"
        )

        # Inspect right-hand map pane (x=284..568, y=36..256) to verify BOTH mid-tone streets AND bright route rails!
        map_pane = guidance_rgb.crop((284, 36, 568, 256))
        green_vals = list(map_pane.getchannel("G").getdata())
        street_pixels = sum(1 for v in green_vals if 12 <= v <= 195)
        rail_pixels = sum(1 for v in green_vals if v >= 200)
        assert street_pixels >= 800, f"Expected visible street blocks/roads on right pane, got {street_pixels} mid-tone px"
        assert rail_pixels >= 80, f"Expected bright route rails + position dot on right pane, got {rail_pixels} bright px"

        # 6. Open `NAVIGATION` mid-route -> tap row 0 (`➤ Show active guidance`) -> send `navigation.resume` ->
        #    verify active guidance + street map reclaims the G2 screen!
        await press("context_menu")
        for _ in range(3):
            await press("down", delay=0.4)
        mark_mid_menu = len(brain.uplinks)
        await press("click")
        await until(lambda: brain.latest_input("capmenu", mark_mid_menu) is not None, 10)
        mid_menu_ev = brain.latest_input("capmenu", mark_mid_menu)
        assert mid_menu_ev is not None
        mid_req_id = mid_menu_ev["requestId"]

        await brain.send({
            "kind": "hud.list",
            "title": "NAVIGATION",
            "items": [
                "➤ Show active guidance (Millennium Park)",
                "⏹ Stop navigation (Millennium Park)",
                "➤ Navigate to a place…",
                "➤ Home · 333 W Wolf Point Plaza, Chicago",
            ],
            "hint": "tap: choose · left x2: back",
            "selectable": True,
            "requestId": mid_req_id,
        })
        mid_list_text, _ = await screen("05b-active-navigation-menu", settle=0.9)
        assert "show active guidance" in mid_list_text.lower() and "stop navigation" in mid_list_text.lower(), (
            f"Expected active guidance & stop rows on mid-route NAVIGATION menu, got:\n{mid_list_text}"
        )

        mark_resume_tap = len(brain.uplinks)
        await press("click")
        await until(lambda: brain.latest_input("capdata", mark_resume_tap) is not None, 10)
        resume_ev = brain.latest_input("capdata", mark_resume_tap)
        assert resume_ev is not None and resume_ev.get("selectedIndex") == 0

        await brain.send(NavigationResume(session_id=session_id).to_wire())
        resumed_text, _ = await screen("06-navigation-resumed", settle=1.0)
        assert "driving" in resumed_text.lower() and "michigan" in resumed_text.lower(), (
            f"Expected navigation.resume to reclaim G2 screen with DRIVING · ACTIVE, got:\n{resumed_text}"
        )

        receipt = {
            "result": "passed",
            "install_route_ack_ms": install_elapsed_ms,
            "map_pane_street_pixels": street_pixels,
            "map_pane_rail_pixels": rail_pixels,
            "navigation_state_uplink": nav_state,
            "screens": screens,
            "verified_lifecycle": [
                "empty places store suppressed on announce",
                "capmenu -> Navigation selectable firmware list",
                "capdata row 0 -> prompt.present (ownerKind=capability, Where to?) + prompt.partial",
                "2x tap prompt cancel -> NAVIGATION restored -> capdata row 1 (selectedIndex=1)",
                "navigation.view mode (Walk, Cycle, Drive) -> select_mode choiceId=driving",
                "navigation.view preview (DRIVE · ROUTE PREVIEW) -> start",
                "navigation.map (284x220 4-bit G2 street tile) + navigation.route (phoneFixes=true) + navigation.fix -> navigation.state",
                "mid-route NAVIGATION menu -> navigation.resume reclaims G2 guidance screen",
            ],
        }
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
    print("RESULT", receipt["result"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist/mac-workspace/control-audit/navigation-simulator",
    )
    args = parser.parse_args()
    asyncio.run(run(args.output.resolve()))
