#!/usr/bin/env python3
"""Exercise Calendar/Reminders HUD actions in the official Even simulator.

The runner uses the current production Hub source, a real local
``HubGatewayTransport``, the official simulator's documented input endpoint,
and the ordinary confirmation protocol.  It deliberately binds an isolated
loopback port and creates a disposable simulator-only manifest, so it cannot
replace the user's app-launched engine or a connected G2 on port 8787.

This is renderer-and-gesture evidence only.  Successful Calendar creation,
Reminder creation, monitoring, and Reminder completion are produced by the
production ``ProductivityActionController`` against a disposable native-shaped
fixture; it never writes EventKit and must not be used as native write or
physical-G2 proof.  Fallback outcome frames remain typed downlink rendering
checks.  A due view additionally checks the live Hub-plugin acceptance
receipt, which is still distinct from pixels on a physical G2 or OS
notification delivery.
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
from typing import Any, Literal, TypeVar, cast
from zoneinfo import ZoneInfo
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from joat.ble.hub_gateway import HubGatewayTransport
from joat.channels.cards import CardLog
from joat.core.protocol import DownlinkMessage, HudList, ProductivityHudView
from joat.persistence.actions import ActionJournal
from joat.persistence.store import DurableStore
from joat.persistence.tasks import TaskOccurrenceStore
from joat.services.capabilities import CapabilityRouter
from joat.services.hud import HudWriter
from joat.services.productivity_actions import ProductivityActionController
from joat.services.status import StatusPublisher
from joat.services.task_monitor import TaskMonitor, evaluate_reminder_occurrence


class ProductivityHudFlowError(RuntimeError):
    """The official-simulator flow did not satisfy its bounded contract."""


VisualTrigger = Literal[
    "menu_click",
    "command_dispatch",
    "confirmed_tap",
    "native_readback",
    "typed_downlink",
    "due_dispatch",
]


@dataclass(frozen=True, slots=True)
class ScreenReceipt:
    """Content-free proof that one named state produced a distinct framebuffer."""

    name: str
    simulator_input: str | None
    confirmation_accepted: bool | None
    phase: str
    framebuffer_sha256: str


@dataclass(frozen=True, slots=True)
class VisualLatency:
    """Content-free timing of one causal simulator-to-HUD transition.

    Both timestamps come only from the acceptance driver's official-simulator
    screenshots: first changed framebuffer and the same framebuffer after the
    deliberate 600 ms stability window.  They are evidence-quality timing,
    not a claim about physical G2 lens latency or a renderer acknowledgement.
    """

    screen: str
    trigger: VisualTrigger
    first_changed_framebuffer_ms: int
    settled_framebuffer_ms: int


@dataclass(slots=True)
class CaptureObservation:
    """Local, ephemeral timestamps for one screenshot-only observation."""

    first_changed_at_ns: int | None = None
    settled_at_ns: int | None = None


_EXPECTED_FLOW = (
    ("planning-context-menu", "context_menu", None, "context_menu"),
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
        self.reminder_due_requests: list[ProductivityHudView] = []

    async def send(self, message: DownlinkMessage) -> None:
        self.sent.append(message)
        await super().send(message)

    async def request_confirmation(self, action_id: str, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        self.confirmation_requests.append(action_id)
        return await super().request_confirmation(action_id, *args, **kwargs)

    async def request_reminder_due_ack(
        self,
        view: ProductivityHudView,
        *,
        timeout_s: float = 5.0,
    ) -> bool:
        self.reminder_due_requests.append(view)
        return await super().request_reminder_due_ack(view, timeout_s=timeout_s)


class FixtureEventKitBridge:
    """Disposable EventKit-shaped data behind the real productivity controller.

    The fixture starts with one Event and one Reminder so the Planning flow can
    prove the unified agenda.  Confirmed controller writes update only this
    in-memory snapshot.  Each write can be paused after the controller emits
    its genuine ``verifying`` phase; that makes a simulator capture able to
    falsify a missing verification state instead of letting an immediate fake
    read-back hide it.
    """

    def __init__(self) -> None:
        event_start = datetime(2026, 9, 1, 10, 30, tzinfo=ZoneInfo("America/Chicago"))
        reminder_due = datetime(2026, 9, 1, 19, 0, tzinfo=ZoneInfo("America/Chicago"))
        self._events: list[dict[str, object]] = [{
            "calendar_item_identifier": "fixture-event-design-review",
            "calendar_identifier": "work",
            "title": "Design review",
            "is_all_day": False,
            "start_unix_ms": int(event_start.timestamp() * 1_000),
            "end_unix_ms": int((event_start.timestamp() + 45 * 60) * 1_000),
        }]
        self._reminders: list[dict[str, object]] = [{
            "calendar_item_identifier": "fixture-reminder-buy-filters",
            "calendar_identifier": "personal",
            "title": "Buy filters",
            "due": self._civil_wire(reminder_due),
            "is_completed": False,
            "has_recurrence_rules": False,
        }]
        self._write_entered = asyncio.Event()
        self._release_write = asyncio.Event()
        self._release_write.set()
        self.write_methods: list[str] = []

    @staticmethod
    def _civil_wire(value: datetime) -> dict[str, object]:
        return {
            "year": value.year,
            "month": value.month,
            "day": value.day,
            "hour": value.hour,
            "minute": value.minute,
            "second": value.second,
            "time_zone_id": value.tzinfo.key if isinstance(value.tzinfo, ZoneInfo) else "UTC",
        }

    @staticmethod
    def _response(*, result: object = None) -> SimpleNamespace:
        return SimpleNamespace(
            authorization=SimpleNamespace(calendar="fullAccess", reminders="fullAccess"),
            result=result,
            store_instance_id="simulator-eventkit-snapshot",
            store_generation=1,
        )

    def hold_next_write(self) -> None:
        """Stop a synthetic native write after the real verifying publication."""

        self._write_entered.clear()
        self._release_write.clear()

    async def wait_for_write(self) -> None:
        try:
            await asyncio.wait_for(self._write_entered.wait(), timeout=5)
        except TimeoutError as exc:
            raise ProductivityHudFlowError("controller did not reach native write") from exc

    def release_next_write(self) -> None:
        self._release_write.set()

    async def _write_boundary(self, method: str) -> None:
        self.write_methods.append(method)
        self._write_entered.set()
        await self._release_write.wait()

    @staticmethod
    def _resolved_time(civil: object) -> tuple[int, int]:
        if not isinstance(civil, dict):
            raise ProductivityHudFlowError("fixture local time request was malformed")
        try:
            value = datetime(
                int(civil["year"]), int(civil["month"]), int(civil["day"]),
                int(civil["hour"]), int(civil["minute"]), int(civil.get("second", 0)),
                tzinfo=ZoneInfo(str(civil["time_zone_id"])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProductivityHudFlowError("fixture local time request was invalid") from exc
        offset = value.utcoffset()
        if offset is None:
            raise ProductivityHudFlowError("fixture local time had no UTC offset")
        return int(value.timestamp() * 1_000), int(offset.total_seconds())

    async def call(
        self,
        method: str,
        params: dict[str, object] | None = None,
        *,
        attempt_id: str | None = None,
        payload_digest: str | None = None,
        timeout_s: float | None = None,
    ) -> SimpleNamespace:
        del attempt_id, payload_digest, timeout_s
        params = params or {}
        if method == "authorization.status":
            return self._response()
        if method == "calendar.listContainers":
            return self._response(result=[{
                "calendar_identifier": "work", "title": "Work",
                "allows_content_modifications": True, "is_default": True,
            }])
        if method == "reminders.listContainers":
            return self._response(result=[{
                "calendar_identifier": "personal", "title": "Personal",
                "allows_content_modifications": True, "is_default": True,
            }])
        if method == "localTime.resolve":
            chosen, offset = self._resolved_time(params.get("civil_time"))
            return self._response(result={
                "status": "unique",
                "chosen_unix_ms": chosen,
                "chosen_utc_offset_seconds": offset,
            })
        if method == "calendar.query":
            return self._response(result=[dict(item) for item in self._events])
        if method == "reminders.query":
            include_completed = params.get("include_completed") is True
            return self._response(result=[
                dict(item) for item in self._reminders
                if include_completed or item.get("is_completed") is False
            ])
        if method == "calendar.create":
            await self._write_boundary(method)
            result = {
                "calendar_item_identifier": "fixture-event-submit-proposal",
                "calendar_identifier": params.get("calendar_identifier"),
                "title": params.get("title"),
                "is_all_day": params.get("is_all_day"),
                "start_unix_ms": params.get("start_unix_ms"),
                "end_unix_ms": params.get("end_unix_ms"),
                "time_zone_id": params.get("time_zone_id"),
                "alarms": [
                    {"relative_offset_seconds": -int(offset), "absolute_unix_ms": None}
                    for offset in params.get("alarm_offsets_seconds", [])
                ],
            }
            self._events.append({key: value for key, value in result.items() if key != "alarms"})
            response = self._response(result=result)
            response.commit_phase = "committed_readback"
            return response
        if method == "reminders.create":
            await self._write_boundary(method)
            due = params.get("due")
            due_unix_ms = params.get("resolved_due_unix_ms")
            if not isinstance(due, dict) or isinstance(due_unix_ms, bool) or not isinstance(due_unix_ms, int):
                raise ProductivityHudFlowError("fixture reminder create request was malformed")
            result = {
                "calendar_item_identifier": "fixture-reminder-prepare-report",
                "calendar_identifier": params.get("calendar_identifier"),
                "title": params.get("title"),
                "due": due,
                "is_completed": False,
                "alarms": [
                    {"relative_offset_seconds": None, "absolute_unix_ms": due_unix_ms - int(offset) * 1_000}
                    for offset in params.get("alarm_offsets_seconds", [])
                ],
            }
            self._reminders.append({
                **{key: value for key, value in result.items() if key != "alarms"},
                "has_recurrence_rules": False,
            })
            response = self._response(result=result)
            response.commit_phase = "committed_readback"
            return response
        if method == "reminders.complete":
            await self._write_boundary(method)
            item_id = params.get("calendar_item_identifier")
            calendar_id = params.get("calendar_identifier")
            matches = [
                item for item in self._reminders
                if item.get("calendar_item_identifier") == item_id
                and item.get("calendar_identifier") == calendar_id
            ]
            if len(matches) != 1:
                raise ProductivityHudFlowError("fixture completion did not identify exactly one reminder")
            matches[0]["is_completed"] = True
            response = self._response(result={
                "calendar_item_identifier": item_id,
                "calendar_identifier": calendar_id,
                "is_completed": True,
            })
            response.commit_phase = "committed_readback"
            return response
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


def visual_latency(
    *,
    screen: str,
    trigger: VisualTrigger,
    started_at_ns: int,
    observation: CaptureObservation,
) -> VisualLatency | None:
    """Build a screenshot-only simulator latency receipt, or omit fake data."""

    first_changed_at_ns = observation.first_changed_at_ns
    settled_at_ns = observation.settled_at_ns
    if first_changed_at_ns is None or settled_at_ns is None:
        return None
    if (
        started_at_ns <= 0
        or first_changed_at_ns < started_at_ns
        or settled_at_ns < first_changed_at_ns
    ):
        raise ProductivityHudFlowError("visual latency timestamps were out of order")
    return VisualLatency(
        screen=screen,
        trigger=trigger,
        first_changed_framebuffer_ms=(first_changed_at_ns - started_at_ns) // 1_000_000,
        settled_framebuffer_ms=(settled_at_ns - started_at_ns) // 1_000_000,
    )


def record_visual_latency(
    latencies: list[VisualLatency],
    *,
    screen: str,
    trigger: VisualTrigger,
    started_at_ns: int,
    observation: CaptureObservation,
) -> None:
    """Record the bounded screenshot observation for one causal action."""

    sample = visual_latency(
        screen=screen,
        trigger=trigger,
        started_at_ns=started_at_ns,
        observation=observation,
    )
    if sample is not None:
        latencies.append(sample)


def visual_latency_summary(latencies: list[VisualLatency]) -> dict[str, int]:
    """Summarize bounded simulator observations without pretending to time G2."""

    if not latencies:
        return {"sample_count": 0}
    return {
        "sample_count": len(latencies),
        "max_first_changed_framebuffer_ms": max(
            sample.first_changed_framebuffer_ms for sample in latencies
        ),
        "max_settled_framebuffer_ms": max(
            sample.settled_framebuffer_ms for sample in latencies
        ),
    }


async def _send_downlink(
    gateway: RecordingGateway,
    message: DownlinkMessage,
) -> int:
    """Deliver a production downlink and return the local trigger timestamp."""

    started_at_ns = time.monotonic_ns()
    await gateway.send(message)
    return started_at_ns


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
    *,
    observation: CaptureObservation | None = None,
) -> tuple[str, str, str]:
    """Wait for a distinct, settled framebuffer and retain reviewable pixels.

    Completion is intentionally screenshot-only. The runner never consumes
    app-console text, bridge-transfer logs, or renderer acknowledgements.
    """

    raw_path = output / f"{name}.rgba.png"
    if raw_path.exists():
        raise ProductivityHudFlowError(
            f"{name}: refusing to overwrite an earlier simulator framebuffer"
        )

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
            if observation is not None and observation.first_changed_at_ns is None:
                observation.first_changed_at_ns = time.monotonic_ns()
        elif now - stable_since >= 0.6:
            raw_path.write_bytes(raw)
            raw_path.chmod(0o600)
            preview = _alpha_preview(raw_path)
            if observation is not None:
                observation.settled_at_ns = time.monotonic_ns()
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


def _productivity_view_after(
    gateway: RecordingGateway,
    *,
    after: int,
    domain: str,
    phase: str,
) -> ProductivityHudView | None:
    for message in reversed(gateway.sent[after:]):
        if (
            isinstance(message, ProductivityHudView)
            and message.domain == domain
            and message.phase == phase
        ):
            return message
    return None


async def _drive_controller_write(
    controller: ProductivityActionController,
    bridge: FixtureEventKitBridge,
    gateway: RecordingGateway,
    *,
    command: str,
    domain: str,
    title: str,
    terminal_phase: str,
    terminal_result: str,
    review_name: str,
    verifying_name: str,
    terminal_name: str,
    automation_base: str,
    output: Path,
    prior_digest: str,
    receipts: list[ScreenReceipt],
    artifacts: dict[str, dict[str, str]],
    latencies: list[VisualLatency],
) -> tuple[str, int]:
    """Drive one confirmed controller write through the official simulator.

    The bridge is deliberately paused at its write boundary.  Consequently the
    verifying frame has to be published by the actual controller before the
    fixture can yield a matching committed read-back.  This rejects the former
    shortcut where the acceptance driver manufactured both states itself.
    """

    before = len(gateway.sent)
    confirmation_after = len(gateway.confirmation_requests)
    bridge.hold_next_write()
    task: asyncio.Task[str | None] | None = None
    try:
        review_started_at_ns = time.monotonic_ns()
        task = asyncio.create_task(controller.handle_command(command))
        action_id = await _wait_for(
            lambda: (
                gateway.confirmation_requests[confirmation_after]
                if len(gateway.confirmation_requests) > confirmation_after
                else None
            ),
            label=f"{review_name} controller confirmation downlink",
        )
        review_observation = CaptureObservation()
        digest, raw, alpha = await _capture(
            automation_base, output, f"{len(receipts) + 1:02d}-{review_name}", prior_digest,
            observation=review_observation,
        )
        record_visual_latency(
            latencies,
            screen=review_name,
            trigger="command_dispatch",
            started_at_ns=review_started_at_ns,
            observation=review_observation,
        )
        receipts.append(ScreenReceipt(review_name, None, None, "review", digest))
        artifacts[review_name] = {"raw": raw, "alpha": alpha}

        verifying_started_at_ns = time.monotonic_ns()
        await _input(automation_base, "click")
        verifying = await _wait_for(
            lambda: _productivity_view_after(
                gateway, after=before, domain=domain, phase="verifying",
            ),
            label=f"{action_id} controller verifying status",
        )
        if verifying.title != title:
            raise ProductivityHudFlowError(
                f"{verifying_name}: controller emitted unexpected title {verifying.title!r}"
            )
        verifying_observation = CaptureObservation()
        digest, raw, alpha = await _capture(
            automation_base, output, f"{len(receipts) + 1:02d}-{verifying_name}", digest,
            observation=verifying_observation,
        )
        record_visual_latency(
            latencies,
            screen=verifying_name,
            trigger="confirmed_tap",
            started_at_ns=verifying_started_at_ns,
            observation=verifying_observation,
        )
        receipts.append(ScreenReceipt(verifying_name, "click", True, "verifying", digest))
        artifacts[verifying_name] = {"raw": raw, "alpha": alpha}

        await bridge.wait_for_write()
        terminal_started_at_ns = time.monotonic_ns()
        bridge.release_next_write()
        terminal = await _wait_for(
            lambda: _productivity_view_after(
                gateway, after=before, domain=domain, phase=terminal_phase,
            ),
            label=f"{action_id} controller {terminal_phase} status",
        )
        if terminal.title != title:
            raise ProductivityHudFlowError(
                f"{terminal_name}: controller emitted unexpected title {terminal.title!r}"
            )
        terminal_observation = CaptureObservation()
        digest, raw, alpha = await _capture(
            automation_base, output, f"{len(receipts) + 1:02d}-{terminal_name}", digest,
            observation=terminal_observation,
        )
        record_visual_latency(
            latencies,
            screen=terminal_name,
            trigger="native_readback",
            started_at_ns=terminal_started_at_ns,
            observation=terminal_observation,
        )
        receipts.append(ScreenReceipt(terminal_name, None, None, terminal_phase, digest))
        artifacts[terminal_name] = {"raw": raw, "alpha": alpha}
        result = await asyncio.wait_for(task, timeout=5)
        if result != terminal_result:
            raise ProductivityHudFlowError(
                f"{terminal_name}: controller returned {result!r}, expected {terminal_result!r}"
            )
        return digest, before
    except BaseException:
        bridge.release_next_write()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        raise


def _planning_list_after(
    gateway: RecordingGateway,
    *,
    after: int,
) -> HudList | None:
    for message in reversed(gateway.sent[after:]):
        if isinstance(message, HudList) and message.title.startswith("PLANNING"):
            return message
    return None


def validate_planning_screen(
    phase: str,
    rendered: HudList,
    *,
    required_item: str | None = None,
    absent_item: str | None = None,
) -> None:
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
        if required_item is not None and not any(required_item in item for item in rendered.items):
            raise ProductivityHudFlowError(
                f"Planning day omitted controller-created item {required_item!r}"
            )
        if absent_item is not None and any(absent_item in item for item in rendered.items):
            raise ProductivityHudFlowError(
                f"Planning day retained completed item {absent_item!r}"
            )
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
    latencies: list[VisualLatency],
) -> tuple[str, dict[str, str]]:
    """Drive one actual menu selection and wait for the authoritative list."""

    before = len(gateway.sent)
    started_at_ns = time.monotonic_ns()
    await _input(automation_base, "click")
    rendered = await _wait_for(
        lambda: _planning_list_after(gateway, after=before),
        label=f"{name} Mac Planning response",
    )
    observation = CaptureObservation()
    digest, raw, alpha = await _capture(
        automation_base, output, name, prior_digest, observation=observation,
    )
    record_visual_latency(
        latencies,
        screen=name,
        trigger="menu_click",
        started_at_ns=started_at_ns,
        observation=observation,
    )
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
    required_item: str | None = None,
    absent_item: str | None = None,
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
    validate_planning_screen(
        "day",
        rendered,
        required_item=required_item,
        absent_item=absent_item,
    )
    return digest, {"raw": raw, "alpha": alpha}


async def _drive_planning_hierarchy(
    gateway: RecordingGateway,
    *,
    automation_base: str,
    output: Path,
    latencies: list[VisualLatency],
) -> tuple[list[ScreenReceipt], dict[str, dict[str, str]], str]:
    """Prove month → week → mixed day → item through real input callbacks.

    The official simulator exposes the documented OS contextual-menu action,
    but no left-temple double press. It therefore proves the image-HUD menu
    escape and the forward hierarchy here; left-double return remains a
    separately unit-tested physical G2 contract.
    """

    # The visual HUD's first app-owned contextual action is Open Planning.
    # Retain the overlay itself before selecting it: this proves the SDK did
    # not silently clear `menuObject` when the image page was rebuilt.
    context_started_at_ns = time.monotonic_ns()
    await _input(automation_base, "context_menu")
    context_observation = CaptureObservation()
    digest, raw, alpha = await _capture(
        automation_base, output, "01-planning-context-menu", None,
        observation=context_observation,
    )
    record_visual_latency(
        latencies,
        screen="planning-context-menu",
        trigger="menu_click",
        started_at_ns=context_started_at_ns,
        observation=context_observation,
    )
    receipts = [ScreenReceipt("planning-context-menu", "context_menu", None, "context_menu", digest)]
    artifacts = {"planning-context-menu": {"raw": raw, "alpha": alpha}}

    digest, artifact = await _planning_click(
        gateway,
        automation_base=automation_base,
        output=output,
        name="02-planning-month",
        phase="month",
        prior_digest=digest,
        latencies=latencies,
    )
    receipts.append(ScreenReceipt("planning-month", "click", None, "month", digest))
    artifacts["planning-month"] = artifact

    digest, artifact = await _planning_click(
        gateway,
        automation_base=automation_base,
        output=output,
        name="03-planning-week",
        phase="week",
        prior_digest=digest,
        latencies=latencies,
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
        name="04-planning-day",
        phase="day",
        prior_digest=digest,
        latencies=latencies,
    )
    receipts.append(ScreenReceipt("planning-day", "click", None, "day", digest))
    artifacts["planning-day"] = artifact

    digest, artifact = await _planning_click(
        gateway,
        automation_base=automation_base,
        output=output,
        name="05-planning-item-detail",
        phase="detail",
        prior_digest=digest,
        latencies=latencies,
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
    fixture_bridge = FixtureEventKitBridge()
    durable_store = DurableStore(args.out / "simulator-productivity.sqlite3")
    occurrences = TaskOccurrenceStore(durable_store)

    async def confirm_from_simulator(
        action_id: str,
        prompt: str,
        timeout_s: float,
        *,
        payload_digest: str,
    ) -> object:
        return await gateway.request_confirmation(
            action_id,
            prompt,
            timeout_s,
            payload_digest=payload_digest,
        )

    async def publish_controller_status(view: ProductivityHudView) -> None:
        await gateway.send(view)

    controller = ProductivityActionController(
        fixture_bridge,  # type: ignore[arg-type]
        confirm_from_simulator,
        ActionJournal(durable_store),
        occurrences,
        publish_status=publish_controller_status,
    )
    task_monitor = TaskMonitor(
        occurrences,
        gateway.request_reminder_due_ack,
        evaluate=lambda occurrence: evaluate_reminder_occurrence(
            occurrence,
            fixture_bridge,  # type: ignore[arg-type]
        ),
        poll_interval_s=60.0,
    )
    planning_router = CapabilityRouter(
        hud=HudWriter(gateway),
        transport=gateway,
        status=StatusPublisher(path=args.out / "planning-status.json"),
        messages=None,
        cards=CardLog(),
        personal_data=fixture_bridge,  # type: ignore[arg-type]
    )
    # Fix the synthetic read model to a month containing both fixture items.
    # The normal Mac's selected day remains its own live local state.
    planning_router._planning_selected_day = date(2026, 9, 1)
    gateway.on_input(planning_router.on_input)
    managed: list[ManagedProcess] = []
    receipts: list[ScreenReceipt] = []
    artifacts: dict[str, dict[str, str]] = {}
    latencies: list[VisualLatency] = []
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
                f"{plugin_url}/",
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
        # The gateway handshake intentionally completes before all local HUD
        # startup paints. This bounded launch grace is not a visual receipt;
        # each following action must still cause a changed, settled screenshot
        # and an authoritative Mac-side state transition.
        await asyncio.sleep(1.0)
        planning_receipts, planning_artifacts, digest = await _drive_planning_hierarchy(
            gateway,
            automation_base=automation_base,
            output=args.out,
            latencies=latencies,
        )
        receipts.extend(planning_receipts)
        artifacts.update(planning_artifacts)

        digest, calendar_before = await _drive_controller_write(
            controller,
            fixture_bridge,
            gateway,
            command=(
                "schedule Submit proposal on 2026-09-01 at 09:30 for 45 minutes "
                "in America/Chicago"
            ),
            domain="calendar",
            title="Submit proposal",
            terminal_phase="saved",
            terminal_result="Created Calendar event: Submit proposal.",
            review_name="calendar-review",
            verifying_name="calendar-verifying",
            terminal_name="calendar-saved",
            automation_base=automation_base,
            output=args.out,
            prior_digest=digest,
            receipts=receipts,
            artifacts=artifacts,
            latencies=latencies,
        )
        digest, artifact = await _wait_for_productivity_return(
            gateway,
            after=calendar_before,
            automation_base=automation_base,
            output=args.out,
            name=f"{len(receipts) + 1:02d}-calendar-return-planning",
            prior_digest=digest,
            required_item="EVENT · Submit proposal",
        )
        receipts.append(ScreenReceipt("calendar-return-planning", None, None, "day", digest))
        artifacts["calendar-return-planning"] = artifact

        # These adverse terminal models deliberately remain typed downlink
        # render checks. The controller-backed success above proves the
        # consequence path; the failure screens are isolated so a fixture
        # cannot claim it changed EventKit while exercising a no-write state.
        calendar_states = (
            ("calendar-failed", "failed", "Failed Calendar draft", "Not saved", "No calendar item was changed.", "Planning in 5s · left x2 now", None, None),
            ("calendar-uncertain", "check", "Uncertain Calendar draft", "Check Calendar", "It may have changed. Do not retry.", "Planning in 5s · left x2 now", None, None),
            ("calendar-cancelled", "cancelled", "Cancelled Calendar draft", "Cancelled", "No calendar item was changed.", "Planning in 5s · left x2 now", None, None),
            ("calendar-not-saved", "not_saved", "Unsaved Calendar draft", "Not saved", "No calendar item was changed.", "Planning in 5s · left x2 now", None, None),
        )
        for name, phase, draft_title, primary, secondary, hint, input_action, accepted in calendar_states:
            before = len(gateway.sent)
            started_at_ns = await _send_downlink(
                gateway,
                _view(
                    "calendar", phase, title=draft_title, primary=primary,
                    secondary=secondary, hint=hint,
                ),
            )
            observation = CaptureObservation()
            digest, raw, alpha = await _capture(
                automation_base, args.out, f"{len(receipts) + 1:02d}-{name}", digest,
                observation=observation,
            )
            record_visual_latency(
                latencies,
                screen=name,
                trigger="typed_downlink",
                started_at_ns=started_at_ns,
                observation=observation,
            )
            receipts.append(ScreenReceipt(name, input_action, accepted, phase, digest))
            artifacts[name] = {"raw": raw, "alpha": alpha}
            if phase in {"saved", "failed", "check", "cancelled", "not_saved"}:
                return_name = f"{name}-return-planning"
                digest, artifact = await _wait_for_productivity_return(
                    gateway,
                    after=before,
                    automation_base=automation_base,
                    output=args.out,
                    name=f"{len(receipts) + 1:02d}-{return_name}",
                    prior_digest=digest,
                )
                receipts.append(ScreenReceipt(return_name, None, None, "day", digest))
                artifacts[return_name] = artifact

        digest, reminder_before = await _drive_controller_write(
            controller,
            fixture_bridge,
            gateway,
            command=(
                "remind me to Prepare report on 2026-09-01 at 18:00 "
                "in America/Chicago"
            ),
            domain="reminders",
            title="Prepare report",
            terminal_phase="saved",
            terminal_result="Created Reminder: Prepare report.",
            review_name="reminders-review",
            verifying_name="reminders-verifying",
            terminal_name="reminders-saved",
            automation_base=automation_base,
            output=args.out,
            prior_digest=digest,
            receipts=receipts,
            artifacts=artifacts,
            latencies=latencies,
        )
        # The monitoring state is an intentional local Hub transition five
        # seconds after the Mac's saved+armed downlink. It has no Mac uplink,
        # so only a changed, settled simulator screenshot may prove it here.
        digest, raw, alpha = await _capture(
            automation_base, args.out, f"{len(receipts) + 1:02d}-reminders-monitoring", digest,
        )
        receipts.append(ScreenReceipt("reminders-monitoring", None, None, "monitoring", digest))
        artifacts["reminders-monitoring"] = {"raw": raw, "alpha": alpha}
        digest, artifact = await _wait_for_productivity_return(
            gateway,
            after=reminder_before,
            automation_base=automation_base,
            output=args.out,
            name=f"{len(receipts) + 1:02d}-reminders-monitoring-return-planning",
            prior_digest=digest,
            required_item="REMINDER · Prepare report",
        )
        receipts.append(ScreenReceipt("reminders-monitoring-return-planning", None, None, "day", digest))
        artifacts["reminders-monitoring-return-planning"] = artifact

        due_occurrences = occurrences.due(now_unix_ms=32_503_680_000_000)
        if len(due_occurrences) != 1:
            raise ProductivityHudFlowError("controller did not persist exactly one monitoring occurrence")
        due_started_at_ns = time.monotonic_ns()
        due_delivery = asyncio.create_task(task_monitor.run_due_once(
            now_unix_ms=due_occurrences[0].due_unix_ms,
            now_us=1_800_000_000_000_000,
        ))
        await _wait_for(
            lambda: bool(gateway.reminder_due_requests),
            label="reminders-due Hub delivery request",
        )
        due_observation = CaptureObservation()
        digest, raw, alpha = await _capture(
            automation_base, args.out, f"{len(receipts) + 1:02d}-reminders-due", digest,
            observation=due_observation,
        )
        record_visual_latency(
            latencies,
            screen="reminders-due",
            trigger="due_dispatch",
            started_at_ns=due_started_at_ns,
            observation=due_observation,
        )
        if await asyncio.wait_for(due_delivery, timeout=5) != 1:
            raise ProductivityHudFlowError(
                "TaskMonitor did not receive a current Hub acknowledgement for the due reminder"
            )
        if not gateway.reminder_due_requests or gateway.reminder_due_requests[-1].title != "Prepare report":
            raise ProductivityHudFlowError("TaskMonitor lost the due reminder title before HUD delivery")
        receipts.append(ScreenReceipt("reminders-due", None, None, "due", digest))
        artifacts["reminders-due"] = {"raw": raw, "alpha": alpha}

        digest, completion_before = await _drive_controller_write(
            controller,
            fixture_bridge,
            gateway,
            command="complete reminder Prepare report",
            domain="reminders",
            title="Prepare report",
            terminal_phase="completed",
            terminal_result="Completed Reminder: Prepare report.",
            review_name="reminders-complete-review",
            verifying_name="reminders-completing",
            terminal_name="reminders-completed",
            automation_base=automation_base,
            output=args.out,
            prior_digest=digest,
            receipts=receipts,
            artifacts=artifacts,
            latencies=latencies,
        )
        digest, artifact = await _wait_for_productivity_return(
            gateway,
            after=completion_before,
            automation_base=automation_base,
            output=args.out,
            name=f"{len(receipts) + 1:02d}-reminders-completed-return-planning",
            prior_digest=digest,
            absent_item="REMINDER · Prepare report",
        )
        receipts.append(ScreenReceipt("reminders-completed-return-planning", None, None, "day", digest))
        artifacts["reminders-completed-return-planning"] = artifact

        completion_states = (
            ("reminders-failed", "failed", "Failed Reminder draft", "Not saved", "No reminders item was changed.", "Planning in 5s · left x2 now", None, None),
            ("reminders-uncertain", "check", "Uncertain Reminder draft", "Check Reminders", "It may have changed. Do not retry.", "Planning in 5s · left x2 now", None, None),
            ("reminders-cancelled", "cancelled", "Cancelled Reminder draft", "Cancelled", "No reminders item was changed.", "Planning in 5s · left x2 now", None, None),
            ("reminders-not-saved", "not_saved", "Unsaved Reminder draft", "Not saved", "No reminders item was changed.", "Planning in 5s · left x2 now", None, None),
        )
        for name, phase, draft_title, primary, secondary, hint, input_action, accepted in completion_states:
            before = len(gateway.sent)
            started_at_ns = await _send_downlink(
                gateway,
                _view(
                    "reminders", phase, title=draft_title, primary=primary,
                    secondary=secondary, hint=hint,
                ),
            )
            observation = CaptureObservation()
            digest, raw, alpha = await _capture(
                automation_base, args.out, f"{len(receipts) + 1:02d}-{name}", digest,
                observation=observation,
            )
            record_visual_latency(
                latencies,
                screen=name,
                trigger="typed_downlink",
                started_at_ns=started_at_ns,
                observation=observation,
            )
            receipts.append(ScreenReceipt(name, input_action, accepted, phase, digest))
            artifacts[name] = {"raw": raw, "alpha": alpha}
            if phase in {"completed", "failed", "check", "cancelled", "not_saved"}:
                return_name = f"{name}-return-planning"
                digest, artifact = await _wait_for_productivity_return(
                    gateway,
                    after=before,
                    automation_base=automation_base,
                    output=args.out,
                    name=f"{len(receipts) + 1:02d}-{return_name}",
                    prior_digest=digest,
                )
                receipts.append(ScreenReceipt(return_name, None, None, "day", digest))
                artifacts[return_name] = artifact

        expected_writes = ["calendar.create", "reminders.create", "reminders.complete"]
        if fixture_bridge.write_methods != expected_writes:
            raise ProductivityHudFlowError(
                f"controller did not exercise the expected native-shaped writes: "
                f"{fixture_bridge.write_methods!r}"
            )
        validate_flow(receipts)
        return {
            "schemaVersion": 1,
            "result": "simulator-productivity-hud-action-flow-pass",
            "receipts": [asdict(receipt) for receipt in receipts],
            "latency": {
                "scope": "official simulator only; driver-observed first-changed and 600ms-settled framebuffer timing, not physical G2 lens latency",
                "samples": [asdict(sample) for sample in latencies],
                "summary": visual_latency_summary(latencies),
            },
            "artifacts": artifacts,
            "evidenceBoundary": {
                "hud": "official Even Hub simulator framebuffer",
                "input": "official simulator click through production menu, Planning, and confirmation routers",
                "mac": "real local HubGatewayTransport; CapabilityRouter drives the native-shaped Planning fixture",
                "controllerWrites": "calendar.create, reminders.create, and reminders.complete are emitted by the production ProductivityActionController after a real bound confirmation; a fixture blocks each native-shaped write until its verifying HUD frame is retained",
                "reminderMonitoring": "production TaskMonitor reconciles the fixture Reminder and obtains a generation-bound current Hub-client due acknowledgement before alert_enqueued",
                "fallbackStates": "failed, uncertain, cancelled, and not-saved frames are typed production-downlink rendering and timed-return checks only; they do not claim an EventKit write",
                "eventkit": "not run; fixture native bridge prevents access to live personal data",
                "nativeAlarm": "not run",
                "hubClientAcceptance": "generation-bound reminder_due acknowledgement from the current production Hub client; not a macOS banner or physical-G2 visual proof",
                "frameCompleteness": "each retained framebuffer must differ from its causal predecessor and remain unchanged for 600ms in the official simulator; completion never consumes console output, renderer telemetry, or bridge-transfer logs",
                "terminalOutcomeReturn": "every terminal Calendar and Reminders outcome waits for the real Hub timeout, then the production CapabilityRouter returns the anchored mixed Planning day",
                "planningReturnGesture": "not run; simulator cannot inject left-temple double press",
                "physicalG2": "not run",
            },
        }
    finally:
        for process in reversed(managed):
            process.stop()
        await gateway.stop()
        durable_store.close()


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
