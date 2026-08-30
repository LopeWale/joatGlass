#!/usr/bin/env python3
"""Exercise Calendar/Reminders HUD actions in the official Even simulator.

The runner uses the current production Hub source, a real local
``HubGatewayTransport``, the official simulator's documented input endpoint,
and the ordinary confirmation protocol.  It deliberately binds an isolated
loopback port and creates a disposable simulator-only manifest, so it cannot
replace the user's app-launched engine or a connected G2 on port 8787.

This is renderer-and-gesture evidence only.  Its test producer sends typed
``ProductivityHudView`` outcomes after a real confirmation response; it never
writes EventKit and must not be used as native write or physical-G2 proof.  A
due view additionally checks the live Hub-plugin acceptance receipt, which is
still distinct from pixels on a physical G2 or OS notification delivery.
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
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any, TypeVar, cast
from zoneinfo import ZoneInfo
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from joat.ble.hub_gateway import HubGatewayTransport
from joat.channels.cards import CardLog
from joat.core.protocol import DownlinkMessage, HudList, ProductivityHudView
from joat.services.capabilities import CapabilityRouter
from joat.services.hud import HudWriter
from joat.services.status import StatusPublisher


class ProductivityHudFlowError(RuntimeError):
    """The official-simulator flow did not satisfy its bounded contract."""


@dataclass(frozen=True, slots=True)
class ScreenReceipt:
    """Content-free proof that one named state produced a distinct framebuffer."""

    name: str
    simulator_input: str | None
    confirmation_accepted: bool | None
    phase: str
    framebuffer_sha256: str


_EXPECTED_FLOW = (
    ("planning-month", "click", None, "month"),
    ("planning-week", "click", None, "week"),
    ("planning-day", "click", None, "day"),
    ("planning-item-detail", "click", None, "detail"),
    ("calendar-review", None, None, "review"),
    ("calendar-verifying", "click", True, "verifying"),
    ("calendar-saved", None, None, "saved"),
    ("calendar-return-planning", None, None, "day"),
    ("calendar-failed", None, None, "failed"),
    ("calendar-failed-return-planning", None, None, "day"),
    ("calendar-uncertain", None, None, "check"),
    ("calendar-uncertain-return-planning", None, None, "day"),
    ("calendar-cancelled", None, None, "cancelled"),
    ("calendar-cancelled-return-planning", None, None, "day"),
    ("calendar-not-saved", None, None, "not_saved"),
    ("calendar-not-saved-return-planning", None, None, "day"),
    ("reminders-review", None, None, "review"),
    ("reminders-verifying", "click", True, "verifying"),
    ("reminders-saved", None, None, "saved"),
    ("reminders-monitoring", None, None, "monitoring"),
    ("reminders-monitoring-return-planning", None, None, "day"),
    ("reminders-due", None, None, "due"),
    ("reminders-complete-review", None, None, "review"),
    ("reminders-completing", "click", True, "verifying"),
    ("reminders-completed", None, None, "completed"),
    ("reminders-completed-return-planning", None, None, "day"),
    ("reminders-failed", None, None, "failed"),
    ("reminders-failed-return-planning", None, None, "day"),
    ("reminders-uncertain", None, None, "check"),
    ("reminders-uncertain-return-planning", None, None, "day"),
    ("reminders-cancelled", None, None, "cancelled"),
    ("reminders-cancelled-return-planning", None, None, "day"),
    ("reminders-not-saved", None, None, "not_saved"),
    ("reminders-not-saved-return-planning", None, None, "day"),
)


def validate_flow(receipts: list[ScreenReceipt]) -> None:
    """Reject a pretty screenshot sequence without causal confirm boundaries."""

    if len(receipts) != len(_EXPECTED_FLOW):
        raise ProductivityHudFlowError(
            f"expected {len(_EXPECTED_FLOW)} screen receipts, got {len(receipts)}"
        )
    for index, (receipt, expected) in enumerate(zip(receipts, _EXPECTED_FLOW), start=1):
        name, input_action, accepted, phase = expected
        if receipt.name != name:
            raise ProductivityHudFlowError(f"step {index}: expected {name}, got {receipt.name}")
        if receipt.simulator_input != input_action:
            raise ProductivityHudFlowError(
                f"step {index} {name}: expected simulator input {input_action!r}, "
                f"got {receipt.simulator_input!r}"
            )
        if receipt.confirmation_accepted is not accepted:
            raise ProductivityHudFlowError(
                f"step {index} {name}: expected confirmation {accepted!r}, "
                f"got {receipt.confirmation_accepted!r}"
            )
        if receipt.phase != phase:
            raise ProductivityHudFlowError(
                f"step {index} {name}: expected phase {phase!r}, got {receipt.phase!r}"
            )
        if len(receipt.framebuffer_sha256) != 64:
            raise ProductivityHudFlowError(f"step {index} {name}: invalid framebuffer digest")


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
                    return
                self.process.wait(timeout=2)


class RecordingGateway(HubGatewayTransport):
    """The actual gateway, with content-free local confirmation bookkeeping."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.sent: list[DownlinkMessage] = []
        self.confirmation_requests: list[str] = []

    async def send(self, message: DownlinkMessage) -> None:
        self.sent.append(message)
        await super().send(message)

    async def request_confirmation(self, action_id: str, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        self.confirmation_requests.append(action_id)
        return await super().request_confirmation(action_id, *args, **kwargs)


class FixturePlanningBridge:
    """A fixed native-shaped snapshot for visualizing the real Planning router.

    It deliberately implements only the read-only EventKit calls used by
    ``CapabilityRouter``.  The production month/week/day projection, selection
    routing, Hub gateway, and HUD renderer remain in play; live EventKit data
    is not touched while a wearer may have a G2 connected to the normal app.
    """

    def __init__(self) -> None:
        scheduled = datetime(2026, 9, 1, 9, 30, tzinfo=ZoneInfo("America/Chicago"))
        self._event_start_unix_ms = int(scheduled.timestamp() * 1_000)

    @staticmethod
    def _response(*, result: object = None) -> SimpleNamespace:
        return SimpleNamespace(
            authorization=SimpleNamespace(calendar="fullAccess", reminders="fullAccess"),
            result=result,
            store_instance_id="simulator-eventkit-snapshot",
            store_generation=1,
        )

    async def call(
        self,
        method: str,
        params: dict[str, object] | None = None,
    ) -> SimpleNamespace:
        del params
        if method == "authorization.status":
            return self._response()
        if method == "calendar.listContainers":
            return self._response(result=[{
                "calendar_identifier": "work", "title": "Work",
            }])
        if method == "reminders.listContainers":
            return self._response(result=[{
                "calendar_identifier": "personal", "title": "Personal",
            }])
        if method == "calendar.query":
            return self._response(result=[{
                "calendar_item_identifier": "event-design-review",
                "title": "Design review",
                "start_unix_ms": self._event_start_unix_ms,
                "end_unix_ms": self._event_start_unix_ms + 45 * 60_000,
            }])
        if method == "reminders.query":
            return self._response(result=[{
                "calendar_item_identifier": "reminder-buy-filters",
                "calendar_identifier": "personal",
                "title": "Buy filters",
                "due": {
                    "year": 2026, "month": 9, "day": 1,
                    "hour": 18, "minute": 0, "second": 0,
                    "time_zone_id": "America/Chicago",
                },
                "is_completed": False,
            }])
        raise ProductivityHudFlowError(f"unexpected fixture Planning call: {method}")


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
    predicate: Callable[[], T | None], *, label: str, timeout_s: float = 15.0,
) -> T:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = predicate()
        if result is not None:
            return result
        await asyncio.sleep(0.05)
    raise ProductivityHudFlowError(f"timed out waiting for {label}")


async def _wait_http(url: str, process: ManagedProcess) -> None:
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if process.process.poll() is not None:
            raise ProductivityHudFlowError(f"{process.name} exited before {url} was ready")
        try:
            await asyncio.to_thread(_http_bytes, url)
            return
        except (OSError, urllib.error.URLError):
            await asyncio.sleep(0.1)
    raise ProductivityHudFlowError(f"timed out waiting for {url}")


async def _wait_console_marker(
    automation_base: str,
    marker: str,
    *,
    timeout_s: float = 15.0,
) -> None:
    """Do not inject a menu gesture before the product says it owns a page."""

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            console = await asyncio.to_thread(_http_bytes, f"{automation_base}/api/console")
        except (OSError, urllib.error.URLError):
            await asyncio.sleep(0.1)
            continue
        if marker.encode("utf-8") in console:
            return
        await asyncio.sleep(0.1)
    raise ProductivityHudFlowError(f"timed out waiting for simulator console marker {marker!r}")


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


def _prepare_simulator_plugin_root(repo: Path, output: Path, websocket_url: str) -> Path:
    """Add exactly one local acceptance origin without editing app.json."""

    parsed = urlsplit(websocket_url)
    if parsed.scheme != "ws" or parsed.hostname != "127.0.0.1" or parsed.path != "/glasses":
        raise ProductivityHudFlowError("simulator gateway must be ws://127.0.0.1:<port>/glasses")
    source_root = repo / "apps/hub-plugin"
    try:
        manifest = json.loads((source_root / "app.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductivityHudFlowError("could not read production Hub manifest") from exc
    permissions = manifest.get("permissions")
    network = next(
        (item for item in permissions if isinstance(item, dict) and item.get("name") == "network"),
        None,
    ) if isinstance(permissions, list) else None
    if not isinstance(network, dict) or not isinstance(network.get("whitelist"), list):
        raise ProductivityHudFlowError("production Hub manifest has no network allowlist")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in network["whitelist"]:
        network["whitelist"].append(origin)

    root = output / "simulator-plugin-root"
    root.mkdir(mode=0o700)
    # Snapshot actual production source into this disposable root.  Vite's
    # normal HMR follows symlinks, which means an unrelated shared-checkout
    # edit can silently reload the simulator halfway through an acceptance
    # gesture.  Dependencies are immutable for this run, so only they remain
    # symlinked to avoid an expensive copy.
    shutil.copy2(source_root / "index.html", root / "index.html")
    shutil.copy2(source_root / "tsconfig.json", root / "tsconfig.json")
    shutil.copytree(source_root / "src", root / "src", symlinks=True)
    os.symlink(source_root / "node_modules", root / "node_modules", target_is_directory=True)
    manifest_path = root / "app.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest_path.chmod(0o600)
    return root


def _alpha_preview(raw_path: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ProductivityHudFlowError("ffmpeg is required to inspect simulator framebuffers")
    preview = raw_path.with_name(f"{raw_path.stem}.alpha.png")
    completed = subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(raw_path), "-vf", "alphaextract", str(preview)],
        capture_output=True,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0 or not preview.is_file():
        raise ProductivityHudFlowError("could not extract simulator framebuffer alpha")
    preview.chmod(0o600)
    return preview


async def _capture(
    automation_base: str,
    output: Path,
    name: str,
    prior_digest: str | None,
) -> tuple[str, str, str]:
    """Wait for a distinct, settled framebuffer and retain reviewable pixels."""

    previous: str | None = None
    stable_since = 0.0
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        try:
            raw = await asyncio.to_thread(_http_bytes, f"{automation_base}/api/screenshot/glasses")
        except (OSError, urllib.error.URLError):
            await asyncio.sleep(0.1)
            continue
        digest = hashlib.sha256(raw).hexdigest()
        if prior_digest is not None and digest == prior_digest:
            await asyncio.sleep(0.1)
            continue
        now = time.monotonic()
        if digest != previous:
            previous, stable_since = digest, now
        elif now - stable_since >= 0.6:
            raw_path = output / f"{name}.rgba.png"
            raw_path.write_bytes(raw)
            raw_path.chmod(0o600)
            preview = _alpha_preview(raw_path)
            return digest, raw_path.name, preview.name
        await asyncio.sleep(0.1)
    raise ProductivityHudFlowError(f"{name}: framebuffer did not settle")


async def _input(automation_base: str, action: str) -> None:
    try:
        await asyncio.to_thread(
            _http_bytes,
            f"{automation_base}/api/input",
            method="POST",
            body=json.dumps({"action": action}).encode("utf-8"),
        )
    except urllib.error.HTTPError as exc:
        raise ProductivityHudFlowError(f"official simulator rejected input {action!r}") from exc


def _view(
    domain: str,
    phase: str,
    *,
    title: str,
    primary: str,
    secondary: str,
    hint: str,
    monitoring_armed: bool = False,
    reminder_due_id: str | None = None,
) -> ProductivityHudView:
    return ProductivityHudView(
        domain=domain,
        phase=phase,
        title=title,
        primary=primary,
        secondary=secondary,
        hint=hint,
        planning_anchor_unix_ms=1_788_274_200_000,
        monitoring_armed=monitoring_armed,
        reminder_due_id=reminder_due_id,
    )


async def _confirm(
    gateway: RecordingGateway,
    *,
    action_id: str,
    prompt: str,
    automation_base: str,
    output: Path,
    receipt_name: str,
    prior_digest: str | None,
) -> tuple[str, dict[str, str]]:
    request = asyncio.create_task(gateway.request_confirmation(action_id, prompt, timeout_s=8))
    await _wait_for(
        lambda: action_id if action_id in gateway.confirmation_requests else None,
        label=f"{action_id} confirmation downlink",
    )
    digest, raw, alpha = await _capture(automation_base, output, receipt_name, prior_digest)
    await _input(automation_base, "click")
    decision = await asyncio.wait_for(request, timeout=5)
    if decision is None or not decision.accepted:
        raise ProductivityHudFlowError(f"{action_id}: simulator click did not bind an accepted confirmation")
    return digest, {"raw": raw, "alpha": alpha}


def _planning_list_after(
    gateway: RecordingGateway,
    *,
    after: int,
) -> HudList | None:
    for message in reversed(gateway.sent[after:]):
        if isinstance(message, HudList) and message.title.startswith("PLANNING"):
            return message
    return None


def validate_planning_screen(phase: str, rendered: HudList) -> None:
    """Reject a rendered Planning page that lost its time or source semantics."""

    expected = {
        "month": "SEPTEMBER 2026",
        "week": "AUG 31–SEP 6",
        "day": "TUE SEP 1",
        "detail": "PLANNING · EVENT",
    }.get(phase)
    if expected is None:
        raise ProductivityHudFlowError(f"unknown Planning phase {phase!r}")
    if expected not in rendered.title:
        raise ProductivityHudFlowError(
            f"Planning {phase}: expected title containing {expected!r}, got {rendered.title!r}"
        )
    if phase == "month":
        if rendered.planning_grid is None or len(rendered.planning_grid) != 42:
            raise ProductivityHudFlowError("planning month did not include the required 42-cell grid")
        return
    if phase == "week":
        if "TUE SEP 1 · 1 event · 1 reminder" not in rendered.items:
            raise ProductivityHudFlowError("Planning week omitted its source-backed mixed-day summary")
        return
    if phase == "day":
        has_event = any("EVENT · Design review" in item for item in rendered.items)
        has_reminder = any("REMINDER · Buy filters" in item for item in rendered.items)
        if not (has_event and has_reminder):
            raise ProductivityHudFlowError("Planning day omitted the mixed Event and Reminder agenda")
        return
    if "Design review" not in rendered.items:
        raise ProductivityHudFlowError("Planning detail omitted the selected native item title")


async def _planning_click(
    gateway: RecordingGateway,
    *,
    automation_base: str,
    output: Path,
    name: str,
    phase: str,
    prior_digest: str | None,
) -> tuple[str, dict[str, str]]:
    """Drive one actual menu selection and wait for the authoritative list."""

    before = len(gateway.sent)
    await _input(automation_base, "click")
    rendered = await _wait_for(
        lambda: _planning_list_after(gateway, after=before),
        label=f"{name} Mac Planning response",
    )
    digest, raw, alpha = await _capture(automation_base, output, name, prior_digest)
    validate_planning_screen(phase, rendered)
    return digest, {"raw": raw, "alpha": alpha}


async def _wait_for_productivity_return(
    gateway: RecordingGateway,
    *,
    after: int,
    automation_base: str,
    output: Path,
    name: str,
    prior_digest: str,
) -> tuple[str, dict[str, str]]:
    """Prove a terminal receipt actually re-enters the anchored live agenda.

    No synthetic gesture is sent here. The Hub timer must emit its ordinary
    ``capmenu Planning`` request, and the production CapabilityRouter must
    answer with the native-shaped day projection before a framebuffer is kept.
    """

    rendered = await _wait_for(
        lambda: _planning_list_after(gateway, after=after),
        label=f"{name} automatic Planning response",
        timeout_s=8.0,
    )
    digest, raw, alpha = await _capture(automation_base, output, name, prior_digest)
    validate_planning_screen("day", rendered)
    return digest, {"raw": raw, "alpha": alpha}


async def _drive_planning_hierarchy(
    gateway: RecordingGateway,
    *,
    automation_base: str,
    output: Path,
) -> tuple[list[ScreenReceipt], dict[str, dict[str, str]], str]:
    """Prove month → week → mixed day → item through real input callbacks.

    The official simulator exposes click and scroll injection, but no
    left-temple double press.  It can therefore prove the forward hierarchy
    here; left-double return remains a separately unit-tested physical G2
    contract.
    """

    # The live-menu test hook starts with Messages selected.  Move exactly
    # once to Planning before selecting it.  If the simulator does not accept
    # its documented scroll action, the following title assertion fails.
    await _input(automation_base, "down")
    digest, artifact = await _planning_click(
        gateway,
        automation_base=automation_base,
        output=output,
        name="01-planning-month",
        phase="month",
        prior_digest=None,
    )
    receipts = [ScreenReceipt("planning-month", "click", None, "month", digest)]
    artifacts = {"planning-month": artifact}

    digest, artifact = await _planning_click(
        gateway,
        automation_base=automation_base,
        output=output,
        name="02-planning-week",
        phase="week",
        prior_digest=digest,
    )
    receipts.append(ScreenReceipt("planning-week", "click", None, "week", digest))
    artifacts["planning-week"] = artifact

    # In the week, Monday is empty; select Tuesday so the visual proof must
    # contain the fixture's merged Event and Reminder agenda, not merely a
    # free-day state.
    await _input(automation_base, "down")
    digest, artifact = await _planning_click(
        gateway,
        automation_base=automation_base,
        output=output,
        name="03-planning-day",
        phase="day",
        prior_digest=digest,
    )
    receipts.append(ScreenReceipt("planning-day", "click", None, "day", digest))
    artifacts["planning-day"] = artifact

    digest, artifact = await _planning_click(
        gateway,
        automation_base=automation_base,
        output=output,
        name="04-planning-item-detail",
        phase="detail",
        prior_digest=digest,
    )
    receipts.append(ScreenReceipt("planning-item-detail", "click", None, "detail", digest))
    artifacts["planning-item-detail"] = artifact
    return receipts, artifacts, digest


async def _run(args: argparse.Namespace) -> dict[str, object]:
    if args.out.exists() and any(args.out.iterdir()):
        raise ProductivityHudFlowError("output directory must be empty")
    args.out.mkdir(parents=True, mode=0o700, exist_ok=True)
    if any(_port_busy(port) for port in (args.ws_port, args.vite_port, args.automation_port)):
        raise ProductivityHudFlowError("an isolated acceptance port is already in use")
    if not args.simulator.is_file():
        raise ProductivityHudFlowError("official evenhub-simulator is not installed")

    websocket_url = f"ws://127.0.0.1:{args.ws_port}/glasses"
    gateway = RecordingGateway(host="127.0.0.1", port=args.ws_port)
    connected = asyncio.Event()
    gateway.on_hello(lambda _hello: connected.set())
    planning_router = CapabilityRouter(
        hud=HudWriter(gateway),
        transport=gateway,
        status=StatusPublisher(path=args.out / "planning-status.json"),
        messages=None,
        cards=CardLog(),
        personal_data=FixturePlanningBridge(),  # type: ignore[arg-type]
    )
    # Fix the synthetic read model to a month containing both fixture items.
    # The normal Mac's selected day remains its own live local state.
    planning_router._planning_selected_day = date(2026, 9, 1)
    gateway.on_input(planning_router.on_input)
    managed: list[ManagedProcess] = []
    receipts: list[ScreenReceipt] = []
    artifacts: dict[str, dict[str, str]] = {}
    try:
        await gateway.start()
        root = _prepare_simulator_plugin_root(args.repo, args.out, websocket_url)
        environment = dict(os.environ)
        environment["VITE_MAC_WS_URL"] = websocket_url
        vite = _start_process(
            "vite",
            [
                "npx", "vite", str(root), "--config", str(args.repo / "apps/hub-plugin/vite.config.ts"),
                "--host", "127.0.0.1", "--port", str(args.vite_port), "--strictPort",
            ],
            cwd=args.repo / "apps/hub-plugin",
            env=environment,
            log_path=args.out / "vite.log",
        )
        managed.append(vite)
        plugin_url = f"http://127.0.0.1:{args.vite_port}"
        await _wait_http(plugin_url, vite)
        simulator = _start_process(
            "official-evenhub-simulator",
            [
                str(args.simulator),
                f"{plugin_url}/?joatTestOpenMenu=1",
                "--automation-port", str(args.automation_port),
            ],
            cwd=args.repo,
            env=environment,
            log_path=args.out / "simulator.log",
        )
        managed.append(simulator)
        automation_base = f"http://127.0.0.1:{args.automation_port}"
        await _wait_http(f"{automation_base}/api/ping", simulator)
        try:
            await asyncio.wait_for(connected.wait(), timeout=20)
        except TimeoutError as exc:
            raise ProductivityHudFlowError(
                "official simulator did not connect to the isolated Hub gateway"
            ) from exc
        await _wait_console_marker(
            automation_base,
            "[joat] live menu acceptance opened=true",
        )

        planning_receipts, planning_artifacts, digest = await _drive_planning_hierarchy(
            gateway,
            automation_base=automation_base,
            output=args.out,
        )
        receipts.extend(planning_receipts)
        artifacts.update(planning_artifacts)

        digest, artifact = await _confirm(
            gateway,
            action_id="calendar-create",
            prompt="Create event “Design review”? Work · Sep 1, 2026 at 09:30 · 45m · America/Chicago",
            automation_base=automation_base,
            output=args.out,
            receipt_name=f"{len(receipts) + 1:02d}-calendar-review",
            prior_digest=digest,
        )
        receipts.append(ScreenReceipt("calendar-review", None, None, "review", digest))
        artifacts["calendar-review"] = artifact

        calendar_states = (
            ("calendar-verifying", "verifying", "Saving calendar event", "Checking native read-back", "Please wait", "click", True),
            ("calendar-saved", "saved", "Saved and verified", "Calendar read-back matched", "Planning in 5s · left x2 now", None, None),
            ("calendar-failed", "failed", "Not saved", "No calendar item was changed.", "Planning in 5s · left x2 now", None, None),
            ("calendar-uncertain", "check", "Check Calendar", "It may have changed. Do not retry.", "Planning in 5s · left x2 now", None, None),
            ("calendar-cancelled", "cancelled", "Cancelled", "No calendar item was changed.", "Planning in 5s · left x2 now", None, None),
            ("calendar-not-saved", "not_saved", "Not saved", "No calendar item was changed.", "Planning in 5s · left x2 now", None, None),
        )
        for name, phase, primary, secondary, hint, input_action, accepted in calendar_states:
            before = len(gateway.sent)
            await gateway.send(_view("calendar", phase, title="Design review", primary=primary, secondary=secondary, hint=hint))
            digest, raw, alpha = await _capture(automation_base, args.out, f"{len(receipts) + 2:02d}-{name}", digest)
            receipts.append(ScreenReceipt(name, input_action, accepted, phase, digest))
            artifacts[name] = {"raw": raw, "alpha": alpha}
            if phase in {"saved", "failed", "check", "cancelled", "not_saved"}:
                return_name = (
                    "calendar-return-planning"
                    if name == "calendar-saved"
                    else f"{name}-return-planning"
                )
                digest, artifact = await _wait_for_productivity_return(
                    gateway,
                    after=before,
                    automation_base=automation_base,
                    output=args.out,
                    name=f"{len(receipts) + 2:02d}-{return_name}",
                    prior_digest=digest,
                )
                receipts.append(ScreenReceipt(return_name, None, None, "day", digest))
                artifacts[return_name] = artifact

        digest, artifact = await _confirm(
            gateway,
            action_id="reminder-create",
            prompt="Create reminder + JOAT alert “Buy filters”? Personal · Sep 1, 2026 at 18:00 · America/Chicago",
            automation_base=automation_base,
            output=args.out,
            receipt_name=f"{len(receipts) + 2:02d}-reminders-review",
            prior_digest=digest,
        )
        receipts.append(ScreenReceipt("reminders-review", None, None, "review", digest))
        artifacts["reminders-review"] = artifact

        reminder_states = (
            ("reminders-verifying", "verifying", "Saving reminder", "Checking native read-back", "Please wait", "click", True),
        )
        for name, phase, primary, secondary, hint, input_action, accepted in reminder_states:
            before = len(gateway.sent)
            await gateway.send(_view("reminders", phase, title="Buy filters", primary=primary, secondary=secondary, hint=hint))
            digest, raw, alpha = await _capture(automation_base, args.out, f"{len(receipts) + 2:02d}-{name}", digest)
            receipts.append(ScreenReceipt(name, input_action, accepted, phase, digest))
            artifacts[name] = {"raw": raw, "alpha": alpha}

        before = len(gateway.sent)
        await gateway.send(_view(
            "reminders",
            "saved",
            title="Buy filters",
            primary="Saved and verified",
            secondary="Native alarm set · JOAT monitoring armed.",
            hint="Monitoring next · left x2 Planning",
            monitoring_armed=True,
        ))
        digest, raw, alpha = await _capture(
            automation_base, args.out, f"{len(receipts) + 2:02d}-reminders-saved", digest,
        )
        receipts.append(ScreenReceipt("reminders-saved", None, None, "saved", digest))
        artifacts["reminders-saved"] = {"raw": raw, "alpha": alpha}
        digest, raw, alpha = await _capture(
            automation_base, args.out, f"{len(receipts) + 2:02d}-reminders-monitoring", digest,
        )
        receipts.append(ScreenReceipt("reminders-monitoring", None, None, "monitoring", digest))
        artifacts["reminders-monitoring"] = {"raw": raw, "alpha": alpha}
        digest, artifact = await _wait_for_productivity_return(
            gateway,
            after=before,
            automation_base=automation_base,
            output=args.out,
            name=f"{len(receipts) + 2:02d}-reminders-monitoring-return-planning",
            prior_digest=digest,
        )
        receipts.append(ScreenReceipt("reminders-monitoring-return-planning", None, None, "day", digest))
        artifacts["reminders-monitoring-return-planning"] = artifact

        due_delivery = asyncio.create_task(gateway.request_reminder_due_ack(_view(
            "reminders",
            "due",
            title="Buy filters",
            primary="Due now",
            secondary="This reminder is still incomplete.",
            hint="say complete reminder title · left x2 Planning",
            # Simulator-only occurrence ID; no personal native identifier or
            # reminder content participates in the receipt protocol.
            reminder_due_id="67ec97c5-a0fd-48ec-89cb-1b756ddaf87a",
        )))
        digest, raw, alpha = await _capture(
            automation_base, args.out, f"{len(receipts) + 2:02d}-reminders-due", digest,
        )
        if not await asyncio.wait_for(due_delivery, timeout=5):
            raise ProductivityHudFlowError(
                "current Hub plugin did not accept the typed reminder due view"
            )
        receipts.append(ScreenReceipt("reminders-due", None, None, "due", digest))
        artifacts["reminders-due"] = {"raw": raw, "alpha": alpha}

        digest, artifact = await _confirm(
            gateway,
            action_id="reminder-complete",
            prompt="Complete reminder “Buy filters”? Personal",
            automation_base=automation_base,
            output=args.out,
            receipt_name=f"{len(receipts) + 2:02d}-reminders-complete-review",
            prior_digest=digest,
        )
        receipts.append(ScreenReceipt("reminders-complete-review", None, None, "review", digest))
        artifacts["reminders-complete-review"] = artifact

        completion_states = (
            ("reminders-completing", "verifying", "Saving reminder", "Checking native read-back", "Please wait", "click", True),
            ("reminders-completed", "completed", "Completed and verified", "Reminders read-back matched", "Planning in 5s · left x2 now", None, None),
            ("reminders-failed", "failed", "Not saved", "No reminders item was changed.", "Planning in 5s · left x2 now", None, None),
            ("reminders-uncertain", "check", "Check Reminders", "It may have changed. Do not retry.", "Planning in 5s · left x2 now", None, None),
            ("reminders-cancelled", "cancelled", "Cancelled", "No reminders item was changed.", "Planning in 5s · left x2 now", None, None),
            ("reminders-not-saved", "not_saved", "Not saved", "No reminders item was changed.", "Planning in 5s · left x2 now", None, None),
        )
        for name, phase, primary, secondary, hint, input_action, accepted in completion_states:
            before = len(gateway.sent)
            await gateway.send(_view("reminders", phase, title="Buy filters", primary=primary, secondary=secondary, hint=hint))
            digest, raw, alpha = await _capture(automation_base, args.out, f"{len(receipts) + 2:02d}-{name}", digest)
            receipts.append(ScreenReceipt(name, input_action, accepted, phase, digest))
            artifacts[name] = {"raw": raw, "alpha": alpha}
            if phase in {"completed", "failed", "check", "cancelled", "not_saved"}:
                return_name = f"{name}-return-planning"
                digest, artifact = await _wait_for_productivity_return(
                    gateway,
                    after=before,
                    automation_base=automation_base,
                    output=args.out,
                    name=f"{len(receipts) + 2:02d}-{return_name}",
                    prior_digest=digest,
                )
                receipts.append(ScreenReceipt(return_name, None, None, "day", digest))
                artifacts[return_name] = artifact

        validate_flow(receipts)
        return {
            "schemaVersion": 1,
            "result": "simulator-productivity-hud-action-flow-pass",
            "receipts": [asdict(receipt) for receipt in receipts],
            "artifacts": artifacts,
            "evidenceBoundary": {
                "hud": "official Even Hub simulator framebuffer",
                "input": "official simulator click through production menu, Planning, and confirmation routers",
                "mac": "real local HubGatewayTransport; CapabilityRouter drives the native-shaped Planning fixture",
                "eventkit": "not run; fixture native bridge prevents access to live personal data",
                "nativeAlarm": "not run",
                "hubClientAcceptance": "generation-bound reminder_due acknowledgement from the current production Hub client; not a macOS banner or physical-G2 visual proof",
                "terminalOutcomeReturn": "every terminal Calendar and Reminders outcome waits for the real Hub timeout, then the production CapabilityRouter returns the anchored mixed Planning day",
                "planningReturnGesture": "not run; simulator cannot inject left-temple double press",
                "physicalG2": "not run",
            },
        }
    finally:
        for process in reversed(managed):
            process.stop()
        await gateway.stop()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--simulator", type=Path, default=Path.home() / ".local/bin/evenhub-simulator")
    parser.add_argument("--ws-port", type=int, default=18860)
    parser.add_argument("--vite-port", type=int, default=5198)
    parser.add_argument("--automation-port", type=int, default=9908)
    return parser


async def _main_async(args: argparse.Namespace) -> int:
    try:
        receipt = await _run(args)
    except ProductivityHudFlowError as exc:
        print(f"productivity_hud_action_flow=failed reason={exc}")
        return 1
    receipt_path = args.out / "receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    receipt_path.chmod(0o600)
    print(f"productivity_hud_action_flow=passed receipt={receipt_path}")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_main_async(_parser().parse_args())))


if __name__ == "__main__":
    main()
