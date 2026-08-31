#!/usr/bin/env python3
"""Drive the real sidebar-to-Navigation entry through the official simulator.

This is intentionally not a ``joatVisual`` fixture: the DEV-only query flag
opens the ordinary production sidebar, then the official simulator sends two
scrolls and a tap.  The production Hub turns that into a ``capmenu`` message,
and the production Mac ``CapabilityRouter`` returns the Navigation entry.

It proves the glasses-side interaction and visual frame only.  It neither
injects a location nor starts a route, so it cannot stand in for a phone or
physical-G2 navigation run.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time
from typing import Any, TypeVar
import urllib.error

from joat.ble.hub_gateway import HubGatewayTransport
from joat.channels.cards import CardLog
from joat.core.protocol import DownlinkMessage, HudList, InputEvent
from joat.services.capabilities import CapabilityRouter
from joat.services.hud import HudWriter
from joat.services.status import StatusPublisher

from simulator_navigation_action_flow_acceptance import (
    _decode_rgba,
    _http_bytes,
    _port_busy,
    _start_process,
    _wait_http,
    _write_alpha_preview,
    visible_content_alpha_pixels,
)


class NavigationEntryFlowError(RuntimeError):
    """The official simulator did not reach the real Navigation entry."""


@dataclass(frozen=True, slots=True)
class ScreenReceipt:
    name: str
    simulator_input: str | None
    framebuffer_sha256: str
    visible_content_alpha_pixels: int


class RecordingGateway(HubGatewayTransport):
    """Keep the actual downlinks only long enough to validate the causal flow."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.sent: list[DownlinkMessage] = []

    async def send(self, message: DownlinkMessage) -> None:
        self.sent.append(message)
        await super().send(message)


T = TypeVar("T")


async def _wait_for(
    predicate: Callable[[], T | None],
    *,
    label: str,
    timeout_s: float = 15.0,
) -> T:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = predicate()
        if result is not None:
            return result
        await asyncio.sleep(0.05)
    raise NavigationEntryFlowError(f"timed out waiting for {label}")


def _last_navigation_entry(gateway: RecordingGateway, *, after: int = 0) -> HudList | None:
    for message in reversed(gateway.sent[after:]):
        if isinstance(message, HudList) and message.title == "NAVIGATION":
            return message
    return None


async def _capture_visible_frame(
    automation_base: str,
    output: Path,
    *,
    name: str,
    prior_digest: str | None,
) -> ScreenReceipt:
    """Capture a settled raster and reject an empty optical content region."""

    deadline = time.monotonic() + 15.0
    previous: str | None = None
    stable_since = 0.0
    while time.monotonic() < deadline:
        try:
            raw = await asyncio.to_thread(
                _http_bytes, f"{automation_base}/api/screenshot/glasses",
            )
        except (OSError, urllib.error.URLError):
            await asyncio.sleep(0.1)
            continue
        digest = hashlib.sha256(raw).hexdigest()
        if prior_digest == digest:
            await asyncio.sleep(0.1)
            continue
        now = time.monotonic()
        if digest != previous:
            previous, stable_since = digest, now
            await asyncio.sleep(0.1)
            continue
        if now - stable_since < 0.6:
            await asyncio.sleep(0.1)
            continue
        alpha_pixels = visible_content_alpha_pixels(_decode_rgba(raw))
        if alpha_pixels < 120:
            raise NavigationEntryFlowError(
                f"{name}: official simulator frame had only {alpha_pixels} visible content pixels"
            )
        raw_path = output / f"{name}.rgba.png"
        raw_path.write_bytes(raw)
        raw_path.chmod(0o600)
        _write_alpha_preview(raw_path)
        return ScreenReceipt(name, None, digest, alpha_pixels)
    raise NavigationEntryFlowError(f"{name}: simulator framebuffer did not settle")


async def _send_input(automation_base: str, action: str) -> None:
    try:
        await asyncio.to_thread(
            _http_bytes,
            f"{automation_base}/api/input",
            method="POST",
            body=json.dumps({"action": action}).encode("utf-8"),
        )
    except (OSError, urllib.error.URLError) as exc:
        raise NavigationEntryFlowError(
            f"official simulator did not accept its {action!r} control"
        ) from exc


async def _wait_for_sidebar(
    automation_base: str,
    output: Path,
) -> ScreenReceipt:
    """Require the live production menu before driving its selector."""

    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        try:
            console = await asyncio.to_thread(_http_bytes, f"{automation_base}/api/console")
        except (OSError, urllib.error.URLError):
            await asyncio.sleep(0.1)
            continue
        if b"live menu acceptance opened=true" in console:
            return await _capture_visible_frame(
                automation_base, output, name="01-sidebar", prior_digest=None,
            )
        await asyncio.sleep(0.1)
    raise NavigationEntryFlowError("production capability sidebar never became visible")


def _validate_flow(
    receipts: list[ScreenReceipt],
    input_events: list[InputEvent],
    entry: HudList,
) -> None:
    if [receipt.name for receipt in receipts] != ["01-sidebar", "02-navigation-entry"]:
        raise NavigationEntryFlowError("the simulator did not retain the sidebar then Navigation sequence")
    if any(len(receipt.framebuffer_sha256) != 64 for receipt in receipts):
        raise NavigationEntryFlowError("simulator did not return a valid framebuffer digest")
    if not any(
        event.container_name == "capmenu" and event.selected_name == "Navigation"
        for event in input_events
    ):
        raise NavigationEntryFlowError("clicking the displayed Navigation row never reached the Mac router")
    if entry.selectable:
        raise NavigationEntryFlowError("Navigation instruction entry must not render as selectable card rows")
    if entry.items != [
        'Say “JOAT, navigate to …”',
        "I’ll confirm the place. Then choose Walk or Cycle.",
    ]:
        raise NavigationEntryFlowError("Mac did not produce the compact Navigation voice handoff")


async def _run(args: argparse.Namespace) -> dict[str, object]:
    if args.out.exists() and any(args.out.iterdir()):
        raise NavigationEntryFlowError("output directory must be empty")
    args.out.mkdir(parents=True, mode=0o700, exist_ok=True)
    if _port_busy(args.ws_port):
        raise NavigationEntryFlowError(
            f"WebSocket port {args.ws_port} is occupied; refusing to interrupt another JOAT session"
        )
    if not args.simulator.is_file():
        raise NavigationEntryFlowError("official evenhub-simulator is not installed")

    connected = asyncio.Event()
    input_events: list[InputEvent] = []
    gateway = RecordingGateway(host="127.0.0.1", port=args.ws_port)
    gateway.on_hello(lambda _hello: connected.set())
    gateway.on_input(input_events.append)
    receipts: list[ScreenReceipt] = []
    managed: list[Any] = []
    with tempfile.TemporaryDirectory(prefix="joat-navigation-entry-") as state:
        router = CapabilityRouter(
            hud=HudWriter(gateway),
            transport=gateway,
            status=StatusPublisher(Path(state) / "status.json"),
            messages=None,
            cards=CardLog(),
        )
        gateway.on_input(router.on_input)
        try:
            await gateway.start()
            env = dict(os.environ)
            env["VITE_MAC_WS_URL"] = args.ws_url
            vite = _start_process(
                "vite",
                ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(args.vite_port), "--strictPort"],
                cwd=args.repo / "apps/hub-plugin",
                env=env,
                log_path=args.out / "vite.log",
            )
            managed.append(vite)
            plugin_url = f"http://127.0.0.1:{args.vite_port}?joatTestOpenMenu=1"
            await _wait_http(f"http://127.0.0.1:{args.vite_port}", vite)
            simulator = _start_process(
                "official-evenhub-simulator",
                [str(args.simulator), plugin_url, "--automation-port", str(args.automation_port)],
                cwd=args.repo,
                env=env,
                log_path=args.out / "simulator.log",
            )
            managed.append(simulator)
            automation_base = f"http://127.0.0.1:{args.automation_port}"
            await _wait_http(f"{automation_base}/api/ping", simulator)
            await asyncio.wait_for(connected.wait(), timeout=20)

            sidebar = await _wait_for_sidebar(automation_base, args.out)
            receipts.append(sidebar)
            for action in ("down", "down", "click"):
                await _send_input(automation_base, action)
                await asyncio.sleep(0.2)

            entry = await _wait_for(
                lambda: _last_navigation_entry(gateway),
                label="Mac Navigation capability response",
            )
            if router._active_capability != "NAVIGATION":
                raise NavigationEntryFlowError("Mac router did not retain Navigation as its active capability")
            navigation = await _capture_visible_frame(
                automation_base,
                args.out,
                name="02-navigation-entry",
                prior_digest=sidebar.framebuffer_sha256,
            )
            receipts.append(ScreenReceipt(
                navigation.name,
                "down + down + click",
                navigation.framebuffer_sha256,
                navigation.visible_content_alpha_pixels,
            ))
            _validate_flow(receipts, input_events, entry)
            return {
                "result": "simulator-navigation-entry-pass",
                "proof": "official_simulator",
                "flow": "sidebar -> Navigation row -> Mac CapabilityRouter -> compact Navigation entry",
                "receipts": [asdict(receipt) for receipt in receipts],
                "input": "down + down + click",
                "physicalG2": "not run",
                "locationOrRoute": "not run; no phone fix or route is injected",
            }
        finally:
            for process in reversed(managed):
                if process.process.poll() is None:
                    try:
                        os.killpg(process.process.pid, signal.SIGTERM)
                        process.process.wait(timeout=5)
                    except (ProcessLookupError, subprocess.TimeoutExpired):
                        if process.process.poll() is None:
                            os.killpg(process.process.pid, signal.SIGKILL)
            await gateway.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--ws-port", type=int, default=17888)
    parser.add_argument("--vite-port", type=int, default=5205)
    parser.add_argument("--automation-port", type=int, default=9975)
    parser.add_argument("--ws-url", default="ws://127.0.0.1:17888/glasses")
    parser.add_argument(
        "--simulator",
        type=Path,
        default=Path.home() / ".local/bin/evenhub-simulator",
    )
    args = parser.parse_args()
    args.repo = args.repo.resolve()
    args.out = args.out.resolve()
    try:
        summary = asyncio.run(_run(args))
    except NavigationEntryFlowError as exc:
        print(f"simulator-navigation-entry: failed: {exc}")
        raise SystemExit(1) from exc
    summary_path = args.out / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    summary_path.chmod(0o600)
    print(summary_path)


if __name__ == "__main__":
    main()
