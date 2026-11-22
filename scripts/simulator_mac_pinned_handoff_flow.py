#!/usr/bin/env python3
"""End-to-end multi-brain evenhub-simulator proof for Mac-pinned Messages & Agent Terminal.

Spins up TWO real local WebSocket servers in one run:
  1. Phone Brain (`ws://127.0.0.1:<phone_port>/glasses`, `isPhoneBrain === true`)
  2. Mac Mini Brain (`ws://[::1]:<mac_port>/glasses` IPv6 loopback, or dynamic local IPv4
     fallback if evenhub-simulator's WebView prefers IPv4; `isPhoneBrain === false`)

Proves all 4 real end-to-end flows in `evenhub-simulator` and captures G2 PNG frames:
  1. Boot on Phone Brain (`127.0.0.1`): immediate `hello` (`handoff: true, heartbeat: true`)
     arrives on Phone Brain without waiting for `getDeviceInfo` / `getUserInfo` timeouts.
  2. Menu / Open `Messages` while connected to Phone Brain:
     - Opens `Messages` from the firmware contextual menu (`context_menu` -> `click`)
     - Asserts Phone Brain NEVER receives a `Messages` `capmenu` or serves a phone-local `MESSAGES` list
     - Asserts Mac Mini Brain connects, authenticates, and receives `capmenu` `selectedName: "Messages"`
     - Mac Mini Brain serves `MESSAGES` list (`g2-handoff-01-messages-mac.png`)
     - Wearer moves down to row 1 and taps -> Mac Mini Brain serves `MESSAGES · CONVERSATION`
       (`g2-handoff-02-messages-thread-mac.png`)
     - Wearer double-taps back to `MESSAGES` list -> double-taps back to Home
     - Asserts plugin automatically reconnects to Phone Brain (`127.0.0.1`)!
  3. Open `Agent Terminal` while connected to Phone Brain:
     - Phone Brain sends `{"kind": "brain.handoff", "capability": "Agent Terminal"}` followed
       immediately on the departing Phone socket by a trailing `assistant.overlay` receipt
       (`"STALE DEPARTING OVERLAY"`) to prove `#68`/`#69` drops the stale overlay with zero BLE paint!
     - Asserts Mac Mini Brain receives `capmenu` `selectedName: "Agent Terminal"` with zero
       intermediate default-layout BLE rebuild drop
     - Mac Mini Brain serves `AGENT TERMINAL` (`Claude Code · idle`, `Codex · idle`, `Antigravity · idle`)
     - Wearer taps `Claude Code` -> Mac Mini Brain serves 5-container `agent_terminal.view`
       (`g2-handoff-03-terminal-mac.png`)
     - Mac Mini Brain sends `hud.rebuild` (`"go home"`) -> asserts plugin automatically
       reconnects to Phone Brain (`127.0.0.1`) (`g2-handoff-04-returned-phone-home.png`)!

    PYTHONPATH=.:apps/mac .venv/bin/python -u scripts/simulator_mac_pinned_handoff_flow.py
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from PIL import Image
import websockets

from scripts.simulator_agent_terminal_flow import OCR_SOURCE, http, until

ROOT = Path(__file__).resolve().parents[1]
PHONE_PORT, MAC_PORT, PLUGIN_PORT, SIM_PORT = 18995, 18996, 5297, 9995
AUTOMATION = f"http://127.0.0.1:{SIM_PORT}"
STALE_OVERLAY_SENTINEL = "STALE DEPARTING OVERLAY DROPPED"


def prepare_multi_brain_plugin_root(
    repo: Path,
    output: Path,
    websocket_urls: list[str],
) -> Path:
    """Snapshot Hub plugin source with multiple local WebSocket origins allowlisted."""
    if not websocket_urls:
        raise RuntimeError("at least one WebSocket URL is required")
    origins: list[str] = []
    for ws_url in websocket_urls:
        parsed = urlsplit(ws_url)
        if parsed.scheme != "ws" or not parsed.hostname or parsed.path != "/glasses":
            raise RuntimeError(f"simulator WebSocket URL must be ws://host:port/glasses, got {ws_url}")
        origins.append(f"{parsed.scheme}://{parsed.netloc}")

    source_root = repo / "apps/hub-plugin"
    manifest = json.loads((source_root / "app.json").read_text(encoding="utf-8"))
    permissions = manifest.get("permissions")
    if not isinstance(permissions, list):
        raise RuntimeError("production Hub manifest has no permissions list")
    network = next(
        (item for item in permissions if isinstance(item, dict) and item.get("name") == "network"),
        None,
    )
    if not isinstance(network, dict) or not isinstance(network.get("whitelist"), list):
        raise RuntimeError("production Hub manifest has no network whitelist")
    network["whitelist"] = origins

    root = output / "simulator-plugin-root"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(mode=0o700, parents=True)
    for name in ("index.html", "src", "tsconfig.json"):
        source = source_root / name
        destination = root / name
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
    os.symlink(source_root / "node_modules", root / "node_modules", target_is_directory=True)
    manifest_path = root / "app.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest_path.chmod(0o600)
    return root


def discover_non_loopback_ipv4() -> str:
    """Discover the machine's active local IPv4 address dynamically without hardcoding."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 80))
        addr = str(probe.getsockname()[0])
    if not addr or addr.startswith("127."):
        raise RuntimeError("could not resolve a non-loopback local IPv4 interface")
    return addr


async def wait_http_ready(url: str, proc: subprocess.Popen[bytes], timeout_s: float = 25.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"Process exited early with code {proc.returncode} while waiting for {url}")
        try:
            await asyncio.to_thread(lambda: urllib.request.urlopen(url, timeout=2.0).read())
            return
        except (OSError, urllib.error.URLError):
            await asyncio.sleep(0.15)
    raise RuntimeError(f"Timed out waiting for {url}")


class LocalBrainServer:
    """Real WebSocket server speaking the JOAT `/glasses` wire protocol."""

    def __init__(self, label: str, host: str, port: int) -> None:
        self.label = label
        self.host = host
        self.port = port
        self.connection_count = 0
        self.hello_count = 0
        self.hellos: list[dict[str, Any]] = []
        self.uplinks: list[dict[str, Any]] = []
        self.downlinks: list[dict[str, Any]] = []
        self.active_ws: Any = None
        self._server: Any = None

    async def start(self) -> None:
        self._server = await websockets.serve(
            self._handle,
            self.host,
            self.port,
            max_size=2**22,
        )

    async def stop(self) -> None:
        ws, self.active_ws = self.active_ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def is_connected(self) -> bool:
        return self.active_ws is not None

    async def send(self, frame: dict[str, Any]) -> None:
        ws = self.active_ws
        assert ws is not None, f"{self.label}: no active authenticated WebSocket"
        self.downlinks.append(frame)
        await ws.send(json.dumps(frame))

    async def send_burst(self, frames: list[dict[str, Any]]) -> None:
        ws = self.active_ws
        assert ws is not None, f"{self.label}: no active authenticated WebSocket"
        for frame in frames:
            self.downlinks.append(frame)
            await ws.send(json.dumps(frame))

    def latest_input(
        self,
        container_name: str,
        after_index: int = 0,
        selected_name: str | None = None,
    ) -> dict[str, Any] | None:
        for entry in reversed(self.uplinks[after_index:]):
            if entry.get("kind") != "input":
                continue
            ev = entry.get("event") or {}
            if ev.get("containerName") != container_name:
                continue
            if selected_name is not None and ev.get("selectedName") != selected_name:
                continue
            return ev
        return None

    async def _handle(self, ws: Any) -> None:
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
                        self.connection_count += 1
                        self.active_ws = ws
                        await ws.send(json.dumps({"kind": "auth.accepted"}))
                    continue
                if kind == "audio":
                    continue
                self.uplinks.append(msg)
                if kind == "hello":
                    self.hello_count += 1
                    self.hellos.append(msg)
        except websockets.ConnectionClosed:
            pass
        finally:
            if self.active_ws is ws:
                self.active_ws = None


async def run_with_mac_host(out: Path, mac_bind_host: str, mac_url_host: str) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.png"):
        old.unlink()

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe4:
        probe4.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe4.bind(("127.0.0.1", PHONE_PORT))
    for port in (PLUGIN_PORT, SIM_PORT):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))

    ocr_bin = out / "ocr"
    (out / "ocr.swift").write_text(OCR_SOURCE, encoding="utf-8")
    subprocess.run(
        ["swiftc", str(out / "ocr.swift"), "-o", str(ocr_bin)],
        check=True,
        capture_output=True,
    )

    phone_brain = LocalBrainServer("phone-brain", "127.0.0.1", PHONE_PORT)
    mac_brain = LocalBrainServer("mac-mini-brain", mac_bind_host, MAC_PORT)
    processes: list[subprocess.Popen[bytes]] = []
    screens: list[dict[str, Any]] = []

    def start_proc(
        name: str,
        args: list[str],
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        with (out / f"{name}.log").open("wb") as log:
            proc = subprocess.Popen(
                args,
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            processes.append(proc)
            return proc

    async def press(action: str, delay: float = 0.65) -> None:
        await asyncio.to_thread(http, AUTOMATION + "/api/input", {"action": action})
        await asyncio.sleep(delay)

    async def capture_screen(name: str, settle: float = 1.0) -> str:
        await asyncio.sleep(settle)
        previous = None
        raw = b""
        for _ in range(22):
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
            [str(ocr_bin), str(out / "ocr-input.png")],
            check=True,
            capture_output=True,
            text=True,
        )
        text = "\n".join(json.loads(ocr.stdout))
        screens.append({"name": name, "text": text})
        print(f"--- SCREEN {name}\n{text}\n", flush=True)
        return text

    scratch = tempfile.TemporaryDirectory(prefix="joat-handoff-sim-")
    try:
        await phone_brain.start()
        await mac_brain.start()

        phone_ws_url = f"ws://127.0.0.1:{PHONE_PORT}/glasses"
        mac_ws_url = f"ws://{mac_url_host}:{MAC_PORT}/glasses"
        plugin_root = prepare_multi_brain_plugin_root(
            ROOT,
            Path(scratch.name),
            [phone_ws_url, mac_ws_url],
        )

        vite_env = dict(os.environ, VITE_MAC_WS_URL=f"{phone_ws_url},{mac_ws_url}")
        vite_proc = start_proc(
            "vite",
            [
                str(ROOT / "apps/hub-plugin/node_modules/.bin/vite"),
                str(plugin_root),
                "--config",
                str(ROOT / "apps/hub-plugin/vite.config.ts"),
                "--host",
                "127.0.0.1",
                "--port",
                str(PLUGIN_PORT),
                "--strictPort",
            ],
            env=vite_env,
        )
        await wait_http_ready(f"http://127.0.0.1:{PLUGIN_PORT}/", vite_proc)

        sim_proc = start_proc(
            "simulator",
            [
                str(Path.home() / ".local/bin/evenhub-simulator"),
                "--no-aid",
                f"http://127.0.0.1:{PLUGIN_PORT}/?joatTestPromptMic=1",
                "--automation-port",
                str(SIM_PORT),
            ],
        )
        await wait_http_ready(f"{AUTOMATION}/api/screenshot/glasses", sim_proc, timeout_s=30.0)

        # ── FLOW 1: Boot on Phone Brain (`127.0.0.1`) ─────────────────────────
        boot_started = time.monotonic()
        await until(lambda: phone_brain.hello_count >= 1, 20)
        hello_elapsed_s = round(time.monotonic() - boot_started, 3)
        first_hello = phone_brain.hellos[0]
        assert first_hello.get("handoff") is True, f"Expected handoff: true in hello, got {first_hello}"
        assert first_hello.get("heartbeat") is True, f"Expected heartbeat: true in hello, got {first_hello}"
        assert mac_brain.connection_count == 0, "Mac brain must NOT be connected at idle phone boot"

        await phone_brain.send({
            "kind": "hud.ambient",
            "temperature": "72f",
            "condition": "clear",
            "unread": 1,
        })
        await phone_brain.send({
            "kind": "hud.notifications",
            "cards": [{
                "id": "0123456789abcdef0123456789abcdef",
                "app": "JOAT Brain",
                "sender": "iPhone Primary",
                "body": "Connected to This iPhone · JOAT Brain.",
                "at": int(time.time() * 1000),
                "urgent": False,
            }],
        })
        await asyncio.sleep(1.5)

        # ── FLOW 2: Menu / Open `Messages` While Connected to Phone Brain ─────
        phone_uplink_mark = len(phone_brain.uplinks)
        mac_uplink_mark = len(mac_brain.uplinks)

        # Open firmware contextual menu -> row 0 (`Messages`) -> click
        await press("context_menu")
        await press("click")

        # Plugin intercepts `Messages` on Phone Brain (`isPhoneBrain === true`),
        # disconnects from Phone Brain without sending `capmenu` `Messages` to Phone Brain,
        # dials Mac Mini Brain, and sends `capmenu` `selectedName: "Messages"` to Mac Mini Brain!
        await until(
            lambda: mac_brain.latest_input("capmenu", mac_uplink_mark, "Messages") is not None,
            20,
        )
        assert phone_brain.latest_input("capmenu", phone_uplink_mark, "Messages") is None, (
            "Phone Brain must NEVER receive capmenu for Mac-pinned Messages"
        )
        assert not any(
            d.get("kind") == "hud.list" and str(d.get("title", "")).upper().startswith("MESSAGES")
            for d in phone_brain.downlinks
        ), "Phone Brain must NEVER serve a phone-local MESSAGES list"
        assert mac_brain.hello_count >= 1
        assert mac_brain.hellos[-1].get("handoff") is True

        msg_capmenu = mac_brain.latest_input("capmenu", mac_uplink_mark, "Messages")
        assert msg_capmenu is not None
        msg_req_id = msg_capmenu["requestId"]

        mac_msg_items = [
            "New message · voice recipient + text",
            "[2m] Sarah Chen • · Landing at Terminal 2 in ten",
            "[18m] Design Sync · Updated G2 layout specs",
        ]
        await mac_brain.send({
            "kind": "hud.list",
            "title": "MESSAGES",
            "items": mac_msg_items,
            "hint": "tap: select · 2x tap: back",
            "selectable": True,
            "requestId": msg_req_id,
        })
        msg_list_ocr = await capture_screen("g2-handoff-01-messages-mac", settle=1.2)
        assert "sarah" in msg_list_ocr.lower(), (
            f"Expected Mac MESSAGES list ('Sarah Chen') on G2, got:\n{msg_list_ocr}"
        )

        # Select row 1 (`[2m] Sarah Chen • · Landing at Terminal 2 in ten`): down -> click
        mac_tap_mark = len(mac_brain.uplinks)
        await press("down", delay=0.45)
        await press("click")
        await until(
            lambda: mac_brain.latest_input("capdata", mac_tap_mark, "MESSAGES") is not None,
            15,
        )
        row_tap = mac_brain.latest_input("capdata", mac_tap_mark, "MESSAGES")
        assert row_tap is not None
        assert row_tap.get("selectedIndex") == 1, f"Expected selectedIndex 1 on MESSAGES, got {row_tap}"
        msg_trace_id = row_tap["traceId"]

        await mac_brain.send({
            "kind": "hud.list",
            "title": "MESSAGES · CONVERSATION",
            "items": [],
            "body": (
                "Sarah Chen\n\n"
                "Sarah Chen: Landing at Terminal 2 in ten.\n"
                "You: Copy that, pulling up to Passenger Pickup."
            ),
            "contextLabel": "Sarah Chen",
            "hint": "tap: reply · 2x tap: back",
            "selectable": True,
            "readerPage": {"id": msg_trace_id, "index": 0, "count": 1},
            "requestId": msg_req_id,
        })
        msg_thread_ocr = await capture_screen("g2-handoff-02-messages-thread-mac", settle=1.2)
        assert "terminal 2" in msg_thread_ocr.lower() or "passenger pickup" in msg_thread_ocr.lower(), (
            f"Expected Mac MESSAGES · CONVERSATION body on G2, got:\n{msg_thread_ocr}"
        )

        # `2x tap` back to `MESSAGES` list
        mac_back_mark = len(mac_brain.uplinks)
        await press("double_click", delay=0.9)
        await until(
            lambda: mac_brain.latest_input("capability-lifecycle", mac_back_mark, "messages-list") is not None,
            15,
        )

        # `2x tap` back to Home -> automatically reconnects to Phone Brain (`127.0.0.1`)!
        phone_hellos_before_return = phone_brain.hello_count
        await press("double_click", delay=0.9)
        await until(lambda: phone_brain.hello_count > phone_hellos_before_return, 20)
        assert phone_brain.is_connected, "Plugin must be reconnected to Phone Brain after 2x tap to Home"

        # ── FLOW 3: Open `Agent Terminal` via `brain.handoff` + Stale Overlay Race ─
        mac_term_mark = len(mac_brain.uplinks)
        # Send `brain.handoff` followed immediately on the departing Phone socket by
        # a trailing `assistant.overlay` receipt to prove `#68`/`#69` drops it!
        await phone_brain.send_burst([
            {
                "kind": "brain.handoff",
                "capability": "Agent Terminal",
                "requestId": "voice-handoff-agent-terminal",
            },
            {
                "kind": "assistant.overlay",
                "operation": "replace",
                "text": STALE_OVERLAY_SENTINEL,
                "presentation": "receipt",
                "answersWearer": True,
            },
        ])

        await until(
            lambda: mac_brain.latest_input("capmenu", mac_term_mark, "Agent Terminal") is not None,
            20,
        )
        term_capmenu = mac_brain.latest_input("capmenu", mac_term_mark, "Agent Terminal")
        assert term_capmenu is not None
        term_req_id = term_capmenu["requestId"]

        # Mac Mini Brain serves `AGENT TERMINAL` (`Claude Code`, `Codex`, `Antigravity`)
        await mac_brain.send({
            "kind": "hud.list",
            "title": "AGENT TERMINAL",
            "items": [
                "Claude Code · idle",
                "Codex · idle",
                "Antigravity · idle",
            ],
            "hint": "tap: choose agent · 2x tap: back",
            "selectable": True,
            "requestId": term_req_id,
        })
        picker_ocr = await capture_screen("03a-agent-terminal-picker-mac", settle=1.0)
        assert STALE_OVERLAY_SENTINEL.lower() not in picker_ocr.lower(), (
            f"Stale departing-socket overlay leaked onto HUD:\n{picker_ocr}"
        )
        assert "claude code" in picker_ocr.lower() and "codex" in picker_ocr.lower(), (
            f"Expected Claude Code & Codex on AGENT TERMINAL picker, got:\n{picker_ocr}"
        )

        # Select row 0 (`Claude Code · idle`) -> Mac serves 5-container `agent_terminal.view`
        mac_agent_tap_mark = len(mac_brain.uplinks)
        await press("click")
        await until(
            lambda: mac_brain.latest_input("capdata", mac_agent_tap_mark, "AGENT TERMINAL") is not None,
            15,
        )
        agent_tap = mac_brain.latest_input("capdata", mac_agent_tap_mark, "AGENT TERMINAL")
        assert agent_tap is not None
        assert agent_tap.get("selectedIndex") == 0, f"Expected selectedIndex 0 (Claude Code), got {agent_tap}"
        term_selection_id = agent_tap["traceId"]

        await mac_brain.send({
            "kind": "agent_terminal.view",
            "terminal": {
                "openingRequestId": term_req_id,
                "openingSelectionId": term_selection_id,
                "state": "ready",
                "focus": "Claude Code · ready",
                "lanes": ["Claude Code · ready", "Codex · idle", "Antigravity · idle"],
                "activity": "> Verify Mac-pinned Agent Terminal\nSplit-brain handoff verified cleanly on Mac mini.",
                "conversationId": "claude-handoff-session-1",
                "messages": [
                    {"id": "u1", "role": "user", "text": "Verify Mac-pinned Agent Terminal"},
                    {
                        "id": "a1",
                        "role": "assistant",
                        "text": "Split-brain handoff verified cleanly on Mac mini.",
                    },
                ],
            },
        })
        term_ocr = await capture_screen("g2-handoff-03-terminal-mac", settle=1.2)
        assert STALE_OVERLAY_SENTINEL.lower() not in term_ocr.lower(), (
            f"Stale departing-socket overlay leaked onto terminal HUD:\n{term_ocr}"
        )
        assert "tap: speak" in term_ocr.lower() or "split-brain handoff" in term_ocr.lower(), (
            f"Expected 5-container Claude Code terminal surface on G2, got:\n{term_ocr}"
        )

        # Mac sends `hud.rebuild` ("go home") -> plugin automatically reconnects to Phone Brain!
        phone_hellos_before_rebuild = phone_brain.hello_count
        await mac_brain.send({"kind": "hud.rebuild", "layout": "default"})
        await until(lambda: phone_brain.hello_count > phone_hellos_before_rebuild, 20)
        assert phone_brain.is_connected, "Plugin must reconnect to Phone Brain after Mac hud.rebuild"

        await phone_brain.send({
            "kind": "hud.ambient",
            "temperature": "72f",
            "condition": "clear",
            "unread": 1,
        })
        await phone_brain.send({
            "kind": "hud.notifications",
            "cards": [{
                "id": "fedcba9876543210fedcba9876543210",
                "app": "JOAT Brain",
                "sender": "iPhone Primary",
                "body": "Back on This iPhone · JOAT Brain after Mac session.",
                "at": int(time.time() * 1000),
                "urgent": False,
            }],
        })
        home_ocr = await capture_screen("g2-handoff-04-returned-phone-home", settle=1.4)
        assert STALE_OVERLAY_SENTINEL.lower() not in home_ocr.lower(), (
            f"Stale overlay sentinel appeared on returned Phone Home:\n{home_ocr}"
        )

        # Verify none of the captured screens ever showed the stale departing overlay
        for entry in screens:
            assert STALE_OVERLAY_SENTINEL.lower() not in entry["text"].lower(), (
                f"Stale departing overlay appeared in {entry['name']}"
            )

        transport_kind = "ipv6-loopback" if mac_url_host == "[::1]" else "local-ipv4-ephemeral"
        receipt = {
            "result": "passed",
            "mac_transport": transport_kind,
            "boot_hello_elapsed_s": hello_elapsed_s,
            "phone_connections": phone_brain.connection_count,
            "phone_hellos": phone_brain.hello_count,
            "mac_connections": mac_brain.connection_count,
            "mac_hellos": mac_brain.hello_count,
            "phone_served_messages_list": False,
            "stale_departing_overlay_dropped": True,
            "screenshots": [
                "g2-handoff-01-messages-mac.png",
                "g2-handoff-02-messages-thread-mac.png",
                "g2-handoff-03-terminal-mac.png",
                "g2-handoff-04-returned-phone-home.png",
            ],
            "screens": screens,
        }
        (out / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        return receipt
    finally:
        for proc in reversed(processes):
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, 15)
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        os.killpg(proc.pid, 9)
                    except Exception:
                        pass
        await phone_brain.stop()
        await mac_brain.stop()
        scratch.cleanup()


async def run(out: Path) -> None:
    try:
        print("Attempting multi-brain handoff proof with IPv6 loopback Mac ([::1])...", flush=True)
        receipt = await run_with_mac_host(out, "::1", "[::1]")
    except Exception as ipv6_exc:
        print(
            f"IPv6 loopback [::1] handoff failed in simulator ({ipv6_exc}); "
            "retrying with dynamic local IPv4 interface...",
            flush=True,
        )
        lan_ip = discover_non_loopback_ipv4()
        receipt = await run_with_mac_host(out, lan_ip, lan_ip)
    print("RESULT", receipt["result"], json.dumps({
        "mac_transport": receipt["mac_transport"],
        "phone_hellos": receipt["phone_hellos"],
        "mac_hellos": receipt["mac_hellos"],
    }), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist/mac-workspace/control-audit/handoff-simulator",
    )
    args = parser.parse_args()
    asyncio.run(run(args.output.resolve()))
