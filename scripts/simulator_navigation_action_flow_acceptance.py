#!/usr/bin/env python3
"""Drive a navigation recovery flow through the official Even simulator.

This is deliberately not the ``?joatVisual=`` matrix.  Each recorded state is
reached through the production Hub event router, the production WebSocket
gateway, and a real ``NavigationSession``.  The runner uses a deterministic
candidate provider only to avoid an external routing-provider call; it never
injects HUD frames or a GPS fix.  The simulator's own location result decides
whether the flow reaches the recoverable location error.

The simulator cannot select a left-temple input source or supply verified G2
location updates.  Consequently this runner proves the destination/mode/
location-error/retry/swipe-cancel interaction path, not walking/cycling route
guidance or physical-glasses behavior.
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
import shutil
import signal
import socket
import subprocess
import time
from typing import Any, TypeVar, cast
import urllib.error
import urllib.request

from joat.ble.hub_gateway import HubGatewayTransport
from joat.core.interfaces import NavigationProvider
from joat.core.navigation import NavigationCandidate, NavigationMode, NavigationPoint, ProviderRoute
from joat.core.protocol import (
    DownlinkMessage,
    NavigationAction,
    NavigationOriginFailure,
    NavigationRouteAck,
    NavigationStop,
    NavigationView,
    NavigationViewAck,
)
from joat.services.navigation import NavigationSession


class ActionFlowError(RuntimeError):
    """The observable simulator route diverged from the safe action contract."""


@dataclass(frozen=True, slots=True)
class FlowReceipt:
    name: str
    input_action: str | None
    semantic_action: str | None
    visible_view: str
    bridge_acknowledged: bool
    framebuffer_sha256: str
    visible_content_alpha_pixels: int


# Exclude the fixed chrome and border. A changed framebuffer without enough
# alpha-bearing pixels in this central region is a failed paint, not evidence
# that a downlink became wearer-visible.
_MIN_VISIBLE_CONTENT_ALPHA_PIXELS = 120
_VISIBLE_CONTENT_X = range(52, 524)
_VISIBLE_CONTENT_Y = range(70, 212)


_EXPECTED_FLOW = (
    ("destination", None, None, "destination"),
    ("mode-walk", "click", "confirm_destination", "mode"),
    ("mode-cycle", "down", None, "mode"),
    ("locating", "click", "select_mode", "uncertain"),
    ("location-unavailable", None, None, "error"),
    ("retrying-location", "click", "retry", "uncertain"),
    ("location-unavailable-again", None, None, "error"),
    ("cancelled", "down", "cancel", "overview"),
)


def is_actionable_location_recovery(
    primary: str,
    secondary: str,
    *,
    has_origin_failure: bool,
) -> bool:
    """Require a real phone-origin failure plus a concrete wearer recovery action."""

    return (
        has_origin_failure
        and primary == "Phone location unavailable"
        and secondary == "Allow Location for Even Hub, then retry"
    )


def validate_action_flow(receipts: list[FlowReceipt]) -> None:
    """Reject a pretty frame sequence that lacks its causal input/action path."""

    for index, (receipt, expected) in enumerate(zip(receipts, _EXPECTED_FLOW), start=1):
        expected_name, expected_input, expected_semantic, expected_view = expected
        if receipt.name != expected_name:
            raise ActionFlowError(f"step {index}: expected {expected_name}, got {receipt.name}")
        if receipt.input_action != expected_input:
            raise ActionFlowError(
                f"step {index} {expected_name}: expected simulator input {expected_input!r}, "
                f"got {receipt.input_action!r}"
            )
        if receipt.semantic_action != expected_semantic:
            raise ActionFlowError(
                f"step {index} {expected_name}: expected semantic action "
                f"{expected_semantic!r}, got {receipt.semantic_action!r}"
            )
        if receipt.visible_view != expected_view:
            raise ActionFlowError(
                f"step {index} {expected_name}: expected visible view {expected_view!r}, "
                f"got {receipt.visible_view!r}"
            )
        if not receipt.bridge_acknowledged:
            raise ActionFlowError(f"step {index} {expected_name}: Hub did not acknowledge the view")
        if len(receipt.framebuffer_sha256) != 64:
            raise ActionFlowError(f"step {index} {expected_name}: framebuffer digest is invalid")
        if receipt.visible_content_alpha_pixels < _MIN_VISIBLE_CONTENT_ALPHA_PIXELS:
            raise ActionFlowError(
                f"step {index} {expected_name}: framebuffer changed but is visually empty"
            )
    if len(receipts) != len(_EXPECTED_FLOW):
        raise ActionFlowError(
            f"expected {len(_EXPECTED_FLOW)} action-flow receipts, got {len(receipts)}"
        )


class CandidateOnlyProvider(NavigationProvider):
    """Deterministic search seam; the runner never emits or accepts a route."""

    async def search(
        self,
        query: str,
        proximity: NavigationPoint | None,
    ) -> list[NavigationCandidate]:
        del query, proximity
        return [NavigationCandidate(
            choice_id="cajun-corner",
            label="Cajun Corner",
            address="2301 N Lincoln Ave, Chicago",
            point=NavigationPoint(latitude=41.92, longitude=-87.65),
        )]

    async def route(
        self,
        origin: NavigationPoint,
        destination: NavigationCandidate,
        mode: NavigationMode,
    ) -> ProviderRoute:
        del origin, destination, mode
        raise ActionFlowError(
            "the simulator unexpectedly supplied an origin; this runner intentionally "
            "does not substitute GPS or route guidance for a real G2 trace"
        )


class RecordingGateway(HubGatewayTransport):
    """The live gateway, with a content-minimized record of outbound messages."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.sent: list[DownlinkMessage] = []

    async def send(self, message: DownlinkMessage) -> None:
        self.sent.append(message)
        await super().send(message)


@dataclass(slots=True)
class ManagedProcess:
    name: str
    process: subprocess.Popen[bytes]

    def stop(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait(timeout=2)


T = TypeVar("T")


def _http_bytes(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    timeout_s: float = 2.0,
) -> bytes:
    headers = {"Content-Type": "application/json"} if body is not None else {}
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return cast(bytes, response.read())


def _port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


async def _wait_for(
    predicate: Callable[[], T | None],
    *,
    label: str,
    timeout_s: float = 12.0,
) -> T:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = predicate()
        if result is not None:
            return result
        await asyncio.sleep(0.05)
    raise ActionFlowError(f"timed out waiting for {label}")


async def _wait_http(
    url: str,
    process: ManagedProcess,
    *,
    timeout_s: float = 20.0,
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.process.poll() is not None:
            raise ActionFlowError(f"{process.name} exited before {url} was ready")
        try:
            await asyncio.to_thread(_http_bytes, url)
            return
        except (OSError, urllib.error.URLError):
            await asyncio.sleep(0.1)
    raise ActionFlowError(f"timed out waiting for {url}")


def _start_process(
    name: str,
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
) -> ManagedProcess:
    with log_path.open("xb") as log_file:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return ManagedProcess(name=name, process=process)


def _write_alpha_preview(raw_path: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ActionFlowError("ffmpeg is required to review the simulator alpha framebuffer")
    preview = raw_path.with_name(f"{raw_path.stem}.alpha.png")
    completed = subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(raw_path), "-vf", "alphaextract", str(preview)],
        capture_output=True,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0 or not preview.is_file():
        raise ActionFlowError("could not alpha-extract the official simulator framebuffer")
    preview.chmod(0o600)
    return preview


def _decode_rgba(png: bytes) -> bytes:
    """Decode the official screenshot once for an optical-content check."""

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ActionFlowError("ffmpeg is required to inspect the simulator framebuffer")
    completed = subprocess.run(
        [
            ffmpeg, "-loglevel", "error", "-i", "pipe:0",
            "-f", "rawvideo", "-pix_fmt", "rgba", "pipe:1",
        ],
        input=png,
        capture_output=True,
        timeout=15,
        check=False,
    )
    expected_bytes = 576 * 288 * 4
    if completed.returncode != 0 or len(completed.stdout) != expected_bytes:
        raise ActionFlowError("could not decode the official simulator framebuffer")
    return completed.stdout


def visible_content_alpha_pixels(rgba: bytes) -> int:
    """Count central, wearer-facing alpha pixels while excluding static chrome."""

    expected_bytes = 576 * 288 * 4
    if len(rgba) != expected_bytes:
        raise ValueError("expected a 576x288 RGBA framebuffer")
    return sum(
        1
        for y in _VISIBLE_CONTENT_Y
        for x in _VISIBLE_CONTENT_X
        if rgba[(y * 576 + x) * 4 + 3] >= 16
    )


async def _capture_visible_frame(
    *,
    automation_base: str,
    output: Path,
    name: str,
    prior_digest: str | None,
    require_changed: bool,
) -> tuple[str, str, str, int]:
    """Wait until the device framebuffer settles, then retain raw and reviewable pixels."""

    deadline = time.monotonic() + 12.0
    previous: str | None = None
    stable_since = 0.0
    raw = b""
    digest = ""
    last_content_alpha_pixels = 0
    while time.monotonic() < deadline:
        try:
            raw = await asyncio.to_thread(_http_bytes, f"{automation_base}/api/screenshot/glasses")
        except (OSError, urllib.error.URLError):
            await asyncio.sleep(0.1)
            continue
        digest = hashlib.sha256(raw).hexdigest()
        if require_changed and prior_digest == digest:
            await asyncio.sleep(0.1)
            continue
        now = time.monotonic()
        if digest != previous:
            previous, stable_since = digest, now
        elif now - stable_since >= 0.6:
            last_content_alpha_pixels = visible_content_alpha_pixels(_decode_rgba(raw))
            if last_content_alpha_pixels < _MIN_VISIBLE_CONTENT_ALPHA_PIXELS:
                await asyncio.sleep(0.1)
                continue
            raw_path = output / f"{name}.rgba.png"
            raw_path.write_bytes(raw)
            raw_path.chmod(0o600)
            preview = _write_alpha_preview(raw_path)
            return digest, raw_path.name, preview.name, last_content_alpha_pixels
        await asyncio.sleep(0.1)
    raise ActionFlowError(
        f"{name}: simulator framebuffer did not settle with readable wearer content "
        f"({last_content_alpha_pixels} central alpha pixels)"
    )


async def _send_input(automation_base: str, action: str) -> None:
    await asyncio.to_thread(
        _http_bytes,
        f"{automation_base}/api/input",
        method="POST",
        body=json.dumps({"action": action}).encode("utf-8"),
    )


async def _request_input(
    automation_base: str,
    action: str,
    *,
    manual: bool,
) -> None:
    """Either inject the documented simulator control or await its real UI button."""

    if manual:
        print(f"manual simulator input required: {action}", flush=True)
        return
    await _send_input(automation_base, action)


def _last_view(
    gateway: RecordingGateway,
    view: str,
    *,
    after: int = 0,
) -> NavigationView | None:
    for message in reversed(gateway.sent[after:]):
        if isinstance(message, NavigationView) and message.navigation.view == view:
            return message
    return None


async def _run(args: argparse.Namespace) -> dict[str, object]:
    output = args.out
    if output.exists() and any(output.iterdir()):
        raise ActionFlowError("output directory must be empty")
    output.mkdir(parents=True, mode=0o700, exist_ok=True)
    if _port_busy(args.ws_port):
        raise ActionFlowError(
            f"WebSocket port {args.ws_port} is already in use; this runner will not interrupt "
            "another JOAT session"
        )
    if not args.simulator.is_file():
        raise ActionFlowError("official evenhub-simulator is not installed")

    gateway = RecordingGateway(host="127.0.0.1", port=args.ws_port)
    connected = asyncio.Event()
    actions: list[NavigationAction] = []
    view_acks: list[NavigationViewAck] = []
    origin_failures: list[NavigationOriginFailure] = []
    route_acks: list[NavigationRouteAck] = []
    session = NavigationSession(
        "Cajun Corner",
        CandidateOnlyProvider(),
        gateway,
        session_id="nav-simulator-action-flow",
    )
    gateway.on_hello(lambda _hello: connected.set())
    gateway.on_navigation_action(lambda action: (actions.append(action), session.accept_action(action)))
    gateway.on_navigation_view_ack(lambda ack: (view_acks.append(ack), session.accept_view_ack(ack)))
    gateway.on_navigation_origin_failure(
        lambda failure: (origin_failures.append(failure), session.accept_origin_failure(failure))
    )
    gateway.on_navigation_route_ack(lambda ack: (route_acks.append(ack), session.accept_route_ack(ack)))

    managed: list[ManagedProcess] = []
    task: asyncio.Task[None] | None = None
    receipts: list[FlowReceipt] = []
    try:
        await gateway.start()
        env = dict(os.environ)
        env["VITE_MAC_WS_URL"] = args.ws_url
        vite = _start_process(
            "vite",
            ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(args.vite_port), "--strictPort"],
            cwd=args.repo / "apps/hub-plugin",
            env=env,
            log_path=output / "vite.log",
        )
        managed.append(vite)
        plugin_url = f"http://127.0.0.1:{args.vite_port}"
        await _wait_http(plugin_url, vite)
        simulator = _start_process(
            "official-evenhub-simulator",
            [str(args.simulator), plugin_url, "--automation-port", str(args.automation_port)],
            cwd=args.repo,
            env=env,
            log_path=output / "simulator.log",
        )
        managed.append(simulator)
        automation_base = f"http://127.0.0.1:{args.automation_port}"
        await _wait_http(f"{automation_base}/api/ping", simulator)
        try:
            await asyncio.wait_for(connected.wait(), timeout=20)
        except TimeoutError as exc:
            raise ActionFlowError(
                "official simulator did not connect to the isolated Hub gateway; "
                "the WebSocket origin must be in the active plugin allowlist"
            ) from exc

        task = asyncio.create_task(session.run())
        destination = await _wait_for(
            lambda: _last_view(gateway, "destination"), label="destination downlink"
        )
        await _wait_for(
            lambda: next((ack for ack in view_acks if ack.request_id == destination.request_id), None),
            label="destination view acknowledgement",
        )
        digest, raw, alpha, content_pixels = await _capture_visible_frame(
            automation_base=automation_base, output=output, name="01-destination",
            prior_digest=None, require_changed=False,
        )
        receipts.append(FlowReceipt("destination", None, None, "destination", True, digest, content_pixels))
        artifacts: dict[str, dict[str, str]] = {"destination": {"raw": raw, "alpha": alpha}}

        action_count = len(actions)
        view_count = len(gateway.sent)
        await _request_input(automation_base, "click", manual=args.manual_input)
        confirmation = await _wait_for(
            lambda: next((item for item in actions[action_count:] if item.action == "confirm_destination"), None),
            label="semantic confirm_destination action",
        )
        mode_walk = await _wait_for(
            lambda: _last_view(gateway, "mode", after=view_count), label="mode downlink"
        )
        await _wait_for(
            lambda: next((ack for ack in view_acks if ack.request_id == mode_walk.request_id), None),
            label="mode view acknowledgement",
        )
        digest, raw, alpha, content_pixels = await _capture_visible_frame(
            automation_base=automation_base, output=output, name="02-mode-walk",
            prior_digest=digest, require_changed=True,
        )
        receipts.append(FlowReceipt("mode-walk", "click", confirmation.action, "mode", True, digest, content_pixels))
        artifacts["mode-walk"] = {"raw": raw, "alpha": alpha}

        await _request_input(automation_base, "down", manual=args.manual_input)
        digest, raw, alpha, content_pixels = await _capture_visible_frame(
            automation_base=automation_base, output=output, name="03-mode-cycle",
            prior_digest=digest, require_changed=True,
        )
        receipts.append(FlowReceipt("mode-cycle", "down", None, "mode", True, digest, content_pixels))
        artifacts["mode-cycle"] = {"raw": raw, "alpha": alpha}

        action_count = len(actions)
        view_count = len(gateway.sent)
        await _request_input(automation_base, "click", manual=args.manual_input)
        select_mode = await _wait_for(
            lambda: next((item for item in actions[action_count:] if item.action == "select_mode"), None),
            label="semantic select_mode action",
        )
        if select_mode.choice_id != "cycling":
            raise ActionFlowError(f"expected cycling choice after down, got {select_mode.choice_id!r}")
        locating = await _wait_for(
            lambda: _last_view(gateway, "uncertain", after=view_count), label="locating downlink"
        )
        await _wait_for(
            lambda: next((ack for ack in view_acks if ack.request_id == locating.request_id), None),
            label="locating view acknowledgement",
        )
        digest, raw, alpha, content_pixels = await _capture_visible_frame(
            automation_base=automation_base, output=output, name="04-locating",
            prior_digest=digest, require_changed=True,
        )
        receipts.append(FlowReceipt("locating", "click", select_mode.action, "uncertain", True, digest, content_pixels))
        artifacts["locating"] = {"raw": raw, "alpha": alpha}

        error = await _wait_for(
            lambda: _last_view(gateway, "error", after=view_count), label="location unavailable error"
        )
        if not is_actionable_location_recovery(
            error.navigation.primary,
            error.navigation.secondary,
            has_origin_failure=bool(origin_failures),
        ):
            raise ActionFlowError(
                "simulator did not prove the actionable high-accuracy phone-location recovery path"
            )
        await _wait_for(
            lambda: next((ack for ack in view_acks if ack.request_id == error.request_id), None),
            label="location error view acknowledgement",
        )
        digest, raw, alpha, content_pixels = await _capture_visible_frame(
            automation_base=automation_base, output=output, name="05-location-unavailable",
            prior_digest=digest, require_changed=True,
        )
        receipts.append(FlowReceipt("location-unavailable", None, None, "error", True, digest, content_pixels))
        artifacts["location-unavailable"] = {"raw": raw, "alpha": alpha}

        action_count = len(actions)
        view_count = len(gateway.sent)
        await _request_input(automation_base, "click", manual=args.manual_input)
        retry = await _wait_for(
            lambda: next((item for item in actions[action_count:] if item.action == "retry"), None),
            label="semantic retry action",
        )
        retrying = await _wait_for(
            lambda: _last_view(gateway, "uncertain", after=view_count), label="retry locating downlink"
        )
        await _wait_for(
            lambda: next((ack for ack in view_acks if ack.request_id == retrying.request_id), None),
            label="retry locating view acknowledgement",
        )
        digest, raw, alpha, content_pixels = await _capture_visible_frame(
            automation_base=automation_base, output=output, name="06-retrying-location",
            prior_digest=digest, require_changed=True,
        )
        receipts.append(FlowReceipt("retrying-location", "click", retry.action, "uncertain", True, digest, content_pixels))
        artifacts["retrying-location"] = {"raw": raw, "alpha": alpha}

        error_again = await _wait_for(
            lambda: _last_view(gateway, "error", after=view_count), label="second location unavailable error"
        )
        await _wait_for(
            lambda: next((ack for ack in view_acks if ack.request_id == error_again.request_id), None),
            label="second location error view acknowledgement",
        )
        digest, raw, alpha, content_pixels = await _capture_visible_frame(
            automation_base=automation_base, output=output, name="07-location-unavailable-again",
            prior_digest=digest, require_changed=True,
        )
        receipts.append(FlowReceipt("location-unavailable-again", None, None, "error", True, digest, content_pixels))
        artifacts["location-unavailable-again"] = {"raw": raw, "alpha": alpha}

        action_count = len(actions)
        stop_count = len([message for message in gateway.sent if isinstance(message, NavigationStop)])
        await _request_input(automation_base, "down", manual=args.manual_input)
        cancelled = await _wait_for(
            lambda: next((item for item in actions[action_count:] if item.action == "cancel"), None),
            label="semantic swipe cancel action",
        )
        await _wait_for(
            lambda: next(
                (
                    message
                    for message in gateway.sent
                    if isinstance(message, NavigationStop)
                ),
                None,
            ) if len([message for message in gateway.sent if isinstance(message, NavigationStop)]) > stop_count else None,
            label="navigation.stop downlink",
        )
        digest, raw, alpha, content_pixels = await _capture_visible_frame(
            automation_base=automation_base, output=output, name="08-overview-after-cancel",
            prior_digest=digest, require_changed=True,
        )
        receipts.append(FlowReceipt("cancelled", "down", cancelled.action, "overview", True, digest, content_pixels))
        artifacts["cancelled"] = {"raw": raw, "alpha": alpha}

        validate_action_flow(receipts)
        return {
            "schemaVersion": 1,
            "result": "simulator-action-flow-pass",
            "evidenceBoundary": {
                "hud": "official Even simulator visible framebuffer",
                "input": "official simulator input endpoint into production EventRouter",
                "mac": "real HubGatewayTransport and NavigationSession",
                "provider": "deterministic candidate only; no route or GPS was injected",
                "physicalG2": "not run",
                "guidance": "not run; simulator supplied no verified G2 location stream",
                "leftTempleDoublePress": "not run; official simulator only injects right-temple double click",
            },
            "receipts": [asdict(receipt) for receipt in receipts],
            "artifacts": artifacts,
            "semanticActions": [
                {"action": action.action, "choiceId": action.choice_id}
                for action in actions
            ],
            "originFailureCount": len(origin_failures),
            "routeAckCount": len(route_acks),
        }
    finally:
        if task is not None and not task.done():
            session.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for process in reversed(managed):
            process.stop()
        await gateway.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--vite-port", type=int, default=5198)
    parser.add_argument("--automation-port", type=int, default=9972)
    parser.add_argument(
        "--manual-input",
        action="store_true",
        help="Wait for clicks on the official simulator's Glasses Display controls.",
    )
    parser.add_argument("--ws-port", type=int, default=8787)
    parser.add_argument(
        "--ws-url",
        default="ws://laptop-mac.local:8787/glasses",
    )
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
    except ActionFlowError as exc:
        print(f"simulator-action-flow: failed: {exc}")
        raise SystemExit(1) from exc
    path = args.out / "summary.json"
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    print(path)


if __name__ == "__main__":
    main()
