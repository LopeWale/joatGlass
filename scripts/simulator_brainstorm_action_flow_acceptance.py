#!/usr/bin/env python3
"""Exercise the glasses-originating Brainstorm acceptance in the official simulator.

This is a causal product-path check, not a visual-fixture gallery. It runs the
current Hub source in the official Even simulator, connects it to a real
``HubGatewayTransport``, opens Agent Terminal through ``CapabilityRouter``,
and drives the production ``EventRouter`` with the simulator input API.

The fixed acceptance controller retains only allowlisted gesture metadata and
completed HUD-frame SHA-256 digests. A deterministic, content-free Hermes
stream replaces private Gateway I/O in this simulator lane. Physical G2 is a
separate gate because the simulator always reports the right temple and cannot
emit the left-temple double press required by the physical acceptance result.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import time
from typing import Any, TypeVar, cast
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from joat.ble.hub_gateway import HubGatewayTransport
from joat.channels.cards import CardLog
from joat.core.protocol import BrainstormAction, BrainstormHudView, DownlinkMessage
from joat.services.brainstorm_acceptance import (
    MAX_RETAINED_EVENTS,
    RUN_SCHEMA,
    file_sha256,
    try_start_g2_gesture_acceptance,
)
from joat.services.capabilities import CapabilityRouter
from joat.services.hud import HudWriter
from joat.services.session import SessionManager
from joat.services.status import StatusPublisher


class BrainstormActionFlowError(RuntimeError):
    """The observed official-simulator flow diverged from the product contract."""


@dataclass(frozen=True, slots=True)
class ScreenReceipt:
    """Content-free visible-state receipt from one settled simulator frame."""

    name: str
    simulator_input: str | None
    semantic_action: str | None
    screen: str
    state: str
    diagnostic_armed: bool
    framebuffer_sha256: str


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


class RecordingGateway(HubGatewayTransport):
    """Production gateway plus a bounded observer used only by this runner."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.sent: list[DownlinkMessage] = []

    async def send(self, message: DownlinkMessage) -> None:
        self.sent.append(message)
        await super().send(message)


class DeterministicHermes:
    """No-network stream that keeps cancellation observable without content."""

    def __init__(self) -> None:
        self._hold = asyncio.Event()

    async def stream(self, _prompt: str) -> AsyncIterator[str]:
        yield "status marker one "
        yield "status marker two"
        await self._hold.wait()

    def release(self) -> None:
        self._hold.set()


T = TypeVar("T")

_EXPECTED_FLOW = (
    ("terminal-approval-required", None, None, "agent_terminal", "approval_required", False),
    ("terminal-retry-ready", "click", "retry_terminal", "agent_terminal", "ready", False),
    ("daily-topic-capture", "click", "begin_topic_capture", "brainstorm", "topic_capture", False),
    ("daily-topic-capture-cancelled", "down", "cancel", "brainstorm", "ready", False),
    ("diagnostic-ready", None, None, "brainstorm", "ready", True),
    # The arm exists only on the safe ready surface.  The accepted action
    # moves the controller into a distinct state, so it must no longer expose
    # the diagnostic gesture while the fixed acceptance run is in progress.
    ("acceptance-ready", "double_click", "begin_acceptance", "brainstorm", "acceptance_ready", False),
    ("running", "click", "begin_topic_capture", "brainstorm", "running", False),
    ("terminal", "click", "show_terminal", "agent_terminal", "running", False),
    ("returned", "click", "show_brainstorm", "brainstorm", "running", False),
    ("reconnected", None, "authenticated_replay", "brainstorm", "running", False),
    ("cancelled", "down", "cancel", "brainstorm", "cancelled", False),
)

_SAFE_ARTIFACT_NAME = re.compile(r"^[0-9]{2}-[a-z-]+\.(?:rgba|optical)\.png$")

_BANNED_EVIDENCE_KEYS = frozenset({
    "audio", "content", "endpoint", "message", "payload", "prompt", "topic",
    "transcript", "url",
})
_DEFAULT_WS_HOST = "emmanuels-macbook-pro.local"


def default_ws_url(port: int) -> str:
    """Derive the disposable simulator origin from its isolated gateway port."""

    if not 1 <= port <= 65_535:
        raise ValueError("simulator WebSocket port is invalid")
    return f"ws://{_DEFAULT_WS_HOST}:{port}/glasses"


def _is_sha256(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _walk_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key).casefold())
            keys.update(_walk_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_walk_keys(child))
    return keys


def validate_flow(
    receipts: list[ScreenReceipt],
    acceptance: dict[str, object],
) -> None:
    """Require the full simulator sequence while refusing a physical claim."""

    observed = [
        (
            item.name,
            item.simulator_input,
            item.semantic_action,
            item.screen,
            item.state,
            item.diagnostic_armed,
        )
        for item in receipts
    ]
    if observed != list(_EXPECTED_FLOW):
        raise BrainstormActionFlowError(f"unexpected rendered action flow: {observed!r}")
    digests = [item.framebuffer_sha256 for item in receipts]
    if any(not _is_sha256(digest) for digest in digests):
        raise BrainstormActionFlowError("simulator receipt contains an invalid frame digest")
    if len(set(digests)) != len(digests):
        raise BrainstormActionFlowError("simulator flow did not produce distinct visible frames")

    if acceptance.get("schema") != RUN_SCHEMA:
        raise BrainstormActionFlowError("acceptance receipt has the wrong schema")
    if acceptance.get("mode") != "simulator_app_engine":
        raise BrainstormActionFlowError("simulator receipt was mislabeled as physical G2")
    if acceptance.get("result") == "pass":
        raise BrainstormActionFlowError("simulator receipt must never claim physical pass")
    if acceptance.get("g2Reconnect") != "restored":
        raise BrainstormActionFlowError("replacement simulator connection was not restored")
    if acceptance.get("hermesRun") != "cancelled":
        raise BrainstormActionFlowError("the deterministic Hermes stream was not cancelled")
    hermes_stream = acceptance.get("hermesStream")
    if not isinstance(hermes_stream, dict) or hermes_stream.get("hud") != "verified":
        raise BrainstormActionFlowError("the Hermes HUD stream was not verified")
    stream_digests = hermes_stream.get("frameSha256")
    if (
        not isinstance(stream_digests, list)
        or len(stream_digests) != 2
        or any(not _is_sha256(digest) for digest in stream_digests)
        or len(set(stream_digests)) != 2
    ):
        raise BrainstormActionFlowError(
            "the Hermes HUD stream has no distinct ordered frame pair"
        )
    events = acceptance.get("events")
    if not isinstance(events, list):
        raise BrainstormActionFlowError("the acceptance trace is missing")
    running_brainstorm_digests = [
        event.get("sha256")
        for event in events
        if isinstance(event, dict)
        and event.get("type") == "visual_frame"
        and event.get("kind") == "brainstorm"
        and event.get("surface") == "brainstorm"
        and event.get("state") == "running"
        and _is_sha256(event.get("sha256"))
    ]
    if not any(
        running_brainstorm_digests[index:index + 2] == stream_digests
        for index in range(max(0, len(running_brainstorm_digests) - 1))
    ):
        raise BrainstormActionFlowError(
            "the Hermes HUD frames are absent or reversed in Brainstorm trace order"
        )
    started_by = acceptance.get("startedBy")
    if started_by != {
        "interface": "g2",
        "modality": "gesture",
        "surface": "brainstorm",
        "source": "right",
        "gesture": "double_press",
        "delivery": "sys",
    }:
        raise BrainstormActionFlowError("acceptance did not preserve the simulator gesture source")
    retained_count = acceptance.get("retainedEventCount")
    if not isinstance(retained_count, int) or not 0 <= retained_count <= MAX_RETAINED_EVENTS:
        raise BrainstormActionFlowError("acceptance retained too many events")
    if _walk_keys(acceptance) & _BANNED_EVIDENCE_KEYS:
        raise BrainstormActionFlowError("acceptance retained a forbidden content field")


def validate_visual_artifacts(artifacts: dict[str, dict[str, str]]) -> None:
    """Require reviewable, content-free optical evidence for every driven HUD state.

    The official simulator's raw RGBA framebuffer uses alpha as optical
    brightness. A normal image viewer can render that carrier incorrectly, so
    every raw capture is accompanied by a deterministic, opaque green-on-black
    interpretation. This validator deliberately checks filenames only: the
    screenshot pixels stay out of the JSON receipt.
    """

    expected = [item[0] for item in _EXPECTED_FLOW]
    if list(artifacts) != expected:
        raise BrainstormActionFlowError("simulator optical artifacts do not match the driven HUD flow")
    for index, state in enumerate(expected, start=1):
        artifact = artifacts.get(state)
        if not isinstance(artifact, dict):
            raise BrainstormActionFlowError(f"{state}: missing visual artifact record")
        raw = artifact.get("raw")
        optical = artifact.get("optical")
        if (
            not isinstance(raw, str)
            or not _SAFE_ARTIFACT_NAME.fullmatch(raw)
            or not raw.startswith(f"{index:02d}-{state}.")
            or not raw.endswith(".rgba.png")
        ):
            raise BrainstormActionFlowError(f"{state}: invalid raw framebuffer artifact")
        if (
            not isinstance(optical, str)
            or not _SAFE_ARTIFACT_NAME.fullmatch(optical)
            or not optical.startswith(f"{index:02d}-{state}.")
            or not optical.endswith(".optical.png")
        ):
            raise BrainstormActionFlowError(
                f"{state}: opaque green-on-black optical preview is required",
            )


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


def _prepare_simulator_plugin_root(repo: Path, output: Path, websocket_url: str) -> Path:
    """Use exact current Hub source with one disposable local allowlist entry."""

    parsed = urlsplit(websocket_url)
    if parsed.scheme != "ws" or not parsed.hostname or parsed.path != "/glasses":
        raise BrainstormActionFlowError("simulator WebSocket URL must be ws://host:port/glasses")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    source_root = repo / "apps/hub-plugin"
    try:
        manifest = json.loads((source_root / "app.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BrainstormActionFlowError("could not read the production Hub manifest") from exc
    permissions = manifest.get("permissions")
    if not isinstance(permissions, list):
        raise BrainstormActionFlowError("production Hub manifest has no permissions list")
    network = next(
        (item for item in permissions if isinstance(item, dict) and item.get("name") == "network"),
        None,
    )
    if not isinstance(network, dict) or not isinstance(network.get("whitelist"), list):
        raise BrainstormActionFlowError("production Hub manifest has no network whitelist")
    whitelist = list(network["whitelist"])
    if origin not in whitelist:
        whitelist.append(origin)
    network["whitelist"] = whitelist

    root = output / "simulator-plugin-root"
    root.mkdir(mode=0o700)
    # The simulator must exercise a coherent source revision. Symlinking the
    # live `src` tree lets another developer's HMR edit replace the WebView or
    # its socket mid-gesture, contaminating causal evidence. Snapshot app code
    # and static entrypoints, while reusing the immutable local dependency tree
    # so this stays fast and does not re-resolve packages.
    for name in ("index.html", "src", "tsconfig.json"):
        source = source_root / name
        destination = root / name
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
    dependencies = source_root / "node_modules"
    os.symlink(dependencies, root / "node_modules", target_is_directory=True)
    manifest_path = root / "app.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest_path.chmod(0o600)
    return root


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


async def _wait_for(
    predicate: Callable[[], T | None],
    *,
    label: str,
    timeout_s: float = 20.0,
) -> T:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = predicate()
        if result is not None:
            return result
        await asyncio.sleep(0.05)
    raise BrainstormActionFlowError(f"timed out waiting for {label}")


async def _wait_http(url: str, process: ManagedProcess) -> None:
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if process.process.poll() is not None:
            raise BrainstormActionFlowError(f"{process.name} exited before {url} was ready")
        try:
            await asyncio.to_thread(_http_bytes, url)
            return
        except (OSError, urllib.error.URLError):
            await asyncio.sleep(0.1)
    raise BrainstormActionFlowError(f"timed out waiting for {url}")


def _optical_preview(raw_path: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise BrainstormActionFlowError("ffmpeg is required to inspect simulator alpha framebuffers")
    optical = raw_path.with_name(raw_path.name.replace(".rgba.png", ".optical.png"))
    completed = subprocess.run(
        [
            ffmpeg, "-y", "-loglevel", "error", "-i", str(raw_path),
            "-vf", "alphaextract,format=rgb24,lutrgb=r=0:g=val:b=0",
            "-pix_fmt", "rgb24", str(optical),
        ],
        capture_output=True,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0 or not optical.is_file():
        raise BrainstormActionFlowError("could not build opaque simulator optical preview")
    optical.chmod(0o600)
    return optical


async def _capture(
    automation_base: str,
    output: Path,
    name: str,
    prior: str | None,
) -> tuple[str, str, str]:
    previous: str | None = None
    stable_since = 0.0
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        try:
            raw = await asyncio.to_thread(
                _http_bytes,
                f"{automation_base}/api/screenshot/glasses",
            )
        except (OSError, urllib.error.URLError):
            await asyncio.sleep(0.1)
            continue
        digest = hashlib.sha256(raw).hexdigest()
        if prior is not None and digest == prior:
            await asyncio.sleep(0.1)
            continue
        now = time.monotonic()
        if digest != previous:
            previous, stable_since = digest, now
        elif now - stable_since >= 0.8:
            raw_path = output / f"{name}.rgba.png"
            raw_path.write_bytes(raw)
            raw_path.chmod(0o600)
            optical = _optical_preview(raw_path)
            return digest, raw_path.name, optical.name
        await asyncio.sleep(0.1)
    raise BrainstormActionFlowError(f"{name}: framebuffer did not visibly settle")


async def _input(automation_base: str, action: str) -> None:
    await asyncio.to_thread(
        _http_bytes,
        f"{automation_base}/api/input",
        method="POST",
        body=json.dumps({"action": action}).encode("utf-8"),
    )


async def _start_official_simulator(
    *,
    args: argparse.Namespace,
    plugin_url: str,
    env: dict[str, str],
    output: Path,
    log_name: str,
) -> ManagedProcess:
    simulator = _start_process(
        "official-evenhub-simulator",
        [str(args.simulator), plugin_url, "--automation-port", str(args.automation_port)],
        cwd=args.repo,
        env=env,
        log_path=output / log_name,
    )
    await _wait_http(f"http://127.0.0.1:{args.automation_port}/api/ping", simulator)
    return simulator


def _last_brainstorm(
    gateway: RecordingGateway,
    *,
    after: int = 0,
    screen: str | None = None,
    state: str | None = None,
) -> BrainstormHudView | None:
    for message in reversed(gateway.sent[after:]):
        if not isinstance(message, BrainstormHudView):
            continue
        if screen is not None and message.screen != screen:
            continue
        if state is not None and message.state != state:
            continue
        return message
    return None


async def _run(args: argparse.Namespace) -> dict[str, object]:
    output = args.out
    if output.exists() and any(output.iterdir()):
        raise BrainstormActionFlowError("output directory must be empty")
    output.mkdir(parents=True, mode=0o700, exist_ok=True)
    if _port_busy(args.ws_port) or _port_busy(args.vite_port) or _port_busy(args.automation_port):
        raise BrainstormActionFlowError("a requested isolated acceptance port is already in use")
    if not args.simulator.is_file():
        raise BrainstormActionFlowError("official evenhub-simulator is not installed")

    gateway = RecordingGateway(host=args.ws_listen_host, port=args.ws_port)
    connected = asyncio.Event()
    hello_count = 0
    actions: list[BrainstormAction] = []
    terminal = DeterministicHermes()
    hud = HudWriter(gateway)
    diagnostic_armed = False
    status = StatusPublisher(path=output / "status.json")
    status.set_openclaw(
        state="approval_required",
        endpoint_host="simulator-only",
        scopes=("operator.read", "operator.write"),
        reason="device_approval_required",
    )
    status.set_agent_terminal(state="approval_required", reason="write_scope_required")
    def topic_capture_view(capturing: bool) -> BrainstormHudView:
        return BrainstormHudView(
            screen="brainstorm",
            state="topic_capture" if capturing else "ready",
            topic="Speak your question" if capturing else "Ready for a question",
            collaborators=["Codex", "Claude", "Hermes"],
            activity=(
                "Your next spoken phrase starts this Brainstorm."
                if capturing else "Tap to choose a question, then speak."
            ),
            terminal_state="ready",
            no_tools_checked=True,
            acceptance_armed=diagnostic_armed,
        )

    async def retry_terminal() -> None:
        """Model a completed out-of-band approval without retaining its payload.

        The G2 action proves that the wearable can ask the normal Mac retry
        route to re-check a terminal that was unavailable at first paint. The
        harness changes only fixed local status state; it does not contact
        OpenClaw or create a pairing request.
        """

        status.set_openclaw(
            state="connected_operator",
            endpoint_host="simulator-only",
            scopes=("operator.read", "operator.write"),
            reason="",
        )
        status.set_agent_terminal(state="ready", reason="")
        await capabilities.open("Agent Terminal")

    sessions = SessionManager(
        hud,
        lambda _topic: None,
        topic_capture_view=topic_capture_view,
        terminal_retry=retry_terminal,
    )
    capabilities = CapabilityRouter(
        hud=hud,
        transport=gateway,
        status=status,
        messages=None,
        cards=CardLog(),
        acceptance_armed=False,
    )
    receipt_directory = output / "receipts"

    def on_hello(_hello: object) -> None:
        nonlocal hello_count
        hello_count += 1
        connected.set()

    gateway.on_hello(on_hello)

    def on_action(action: BrainstormAction) -> None:
        actions.append(action)
        if action.action == "begin_acceptance":
            rejection = try_start_g2_gesture_acceptance(
                transport=gateway,
                terminal=lambda: terminal,
                trigger=action,
                start_session=sessions.try_start_session,
                # The harness is its own explicit simulator-only test mode;
                # production actions reach this same function unarmed.
                acceptance_armed=True,
                output_directory=receipt_directory,
                action_timeout_s=15.0,
                connect_timeout_s=30.0,
                simulator_override=True,
            )
            if rejection is not None:
                hello = gateway.hello
                raise BrainstormActionFlowError(
                    "explicit acceptance route rejected simulator classification "
                    f"state={rejection.state} activity={rejection.activity!r} "
                    f"model={hello.model if hello else 'missing'} "
                    f"launch={hello.launch_source if hello else 'missing'} "
                    f"physical={gateway.physical_g2_connected}"
                )
            return
        sessions.handle_brainstorm_action(action)

    gateway.on_brainstorm_action(on_action)

    managed: list[ManagedProcess] = []
    receipts: list[ScreenReceipt] = []
    artifacts: dict[str, dict[str, str]] = {}
    try:
        await gateway.start()
        simulator_root = _prepare_simulator_plugin_root(args.repo, output, args.ws_url)
        env = dict(os.environ)
        env["VITE_MAC_WS_URL"] = args.ws_url
        vite = _start_process(
            "vite",
            [
                "npx", "vite", str(simulator_root),
                "--config", str(args.repo / "apps/hub-plugin/vite.config.ts"),
                "--host", "127.0.0.1",
                "--port", str(args.vite_port),
                "--strictPort",
            ],
            cwd=args.repo / "apps/hub-plugin",
            env=env,
            log_path=output / "vite.log",
        )
        managed.append(vite)
        plugin_url = f"http://127.0.0.1:{args.vite_port}"
        await _wait_http(plugin_url, vite)
        simulator = await _start_official_simulator(
            args=args,
            plugin_url=plugin_url,
            env=env,
            output=output,
            log_name="simulator.log",
        )
        managed.append(simulator)
        automation_base = f"http://127.0.0.1:{args.automation_port}"
        await _wait_for(
            lambda: True if gateway.connected else None,
            label="authenticated simulator WebSocket",
        )
        try:
            await asyncio.wait_for(connected.wait(), timeout=15)
        except asyncio.TimeoutError as exc:
            raise BrainstormActionFlowError("simulator authenticated but never sent Hello") from exc

        # Open the real Agent Terminal capability producer. The simulator
        # cannot inject the left-temple sidebar gesture that selects this row,
        # so sidebar ingress remains a physical G2 gate. It can drive the
        # terminal's own retry gesture, which must recover from a truthful
        # approval-required frame without displaying a raw internal code.
        await capabilities.open("Agent Terminal")
        terminal_approval = await _wait_for(
            lambda: _last_brainstorm(
                gateway,
                screen="agent_terminal",
                state="approval_required",
            ),
            label="approval-required Agent Terminal capability",
        )
        digest, raw, optical = await _capture(
            automation_base,
            output,
            "01-terminal-approval-required",
            None,
        )
        receipts.append(ScreenReceipt(
            "terminal-approval-required", None, None,
            terminal_approval.screen, terminal_approval.state,
            terminal_approval.acceptance_armed, digest,
        ))
        artifacts["terminal-approval-required"] = {"raw": raw, "optical": optical}

        action_count, sent_before = len(actions), len(gateway.sent)
        await _input(automation_base, "click")
        retry_action = await _wait_for(
            lambda: next(
                (item for item in actions[action_count:] if item.action == "retry_terminal"),
                None,
            ),
            label="Agent Terminal retry tap",
        )
        terminal_ready = await _wait_for(
            lambda: _last_brainstorm(
                gateway,
                after=sent_before,
                screen="agent_terminal",
                state="ready",
            ),
            label="ready Agent Terminal after retry",
        )
        digest, raw, optical = await _capture(
            automation_base,
            output,
            "02-terminal-retry-ready",
            digest,
        )
        receipts.append(ScreenReceipt(
            "terminal-retry-ready", "click", retry_action.action,
            terminal_ready.screen, terminal_ready.state,
            terminal_ready.acceptance_armed, digest,
        ))
        artifacts["terminal-retry-ready"] = {"raw": raw, "optical": optical}

        # Agent Terminal deliberately starts an ordinary Brainstorm rather
        # than a free-form remote console. Drive its production tap through
        # EventRouter and prove the one-utterance capture plus explicit
        # cancellation before beginning the content-free diagnostic run.
        action_count, sent_before = len(actions), len(gateway.sent)
        await _input(automation_base, "click")
        topic_capture = await _wait_for(
            lambda: next(
                (item for item in actions[action_count:]
                 if item.action == "begin_topic_capture"),
                None,
            ),
            label="Agent Terminal topic-capture tap",
        )
        capture_view = await _wait_for(
            lambda: _last_brainstorm(
                gateway,
                after=sent_before,
                screen="brainstorm",
                state="topic_capture",
            ),
            label="Agent Terminal topic-capture Brainstorm frame",
        )
        digest, raw, optical = await _capture(
            automation_base, output, "03-daily-topic-capture", digest,
        )
        receipts.append(ScreenReceipt(
            "daily-topic-capture", "click", topic_capture.action,
            capture_view.screen, capture_view.state,
            capture_view.acceptance_armed, digest,
        ))
        artifacts["daily-topic-capture"] = {"raw": raw, "optical": optical}

        action_count, sent_before = len(actions), len(gateway.sent)
        await _input(automation_base, "down")
        clear_capture = await _wait_for(
            lambda: next(
                (item for item in actions[action_count:] if item.action == "cancel"),
                None,
            ),
            label="Agent Terminal topic-capture cancellation",
        )
        cleared_view = await _wait_for(
            lambda: _last_brainstorm(
                gateway,
                after=sent_before,
                screen="brainstorm",
                state="ready",
            ),
            label="cleared Agent Terminal topic-capture Brainstorm frame",
        )
        digest, raw, optical = await _capture(
            automation_base, output, "04-daily-topic-capture-cancelled", digest,
        )
        receipts.append(ScreenReceipt(
            "daily-topic-capture-cancelled", "down", clear_capture.action,
            cleared_view.screen, cleared_view.state,
            cleared_view.acceptance_armed, digest,
        ))
        artifacts["daily-topic-capture-cancelled"] = {"raw": raw, "optical": optical}

        # The wearer-controlled daily path finishes above. Diagnostic mode is
        # an explicit source-side arm: repaint the capability through that
        # distinct route before accepting the simulator's fixed double press.
        diagnostic_armed = True
        capabilities = CapabilityRouter(
            hud=hud,
            transport=gateway,
            status=status,
            messages=None,
            cards=CardLog(),
            acceptance_armed=True,
        )
        sent_before = len(gateway.sent)
        await capabilities.open("Brainstorm")
        diagnostic_ready = await _wait_for(
            lambda: _last_brainstorm(
                gateway,
                after=sent_before,
                screen="brainstorm",
                state="ready",
            ),
            label="explicitly armed diagnostic Brainstorm frame",
        )
        digest, raw, optical = await _capture(
            automation_base, output, "05-diagnostic-ready", digest,
        )
        receipts.append(ScreenReceipt(
            "diagnostic-ready", None, None,
            diagnostic_ready.screen, diagnostic_ready.state,
            diagnostic_ready.acceptance_armed, digest,
        ))
        artifacts["diagnostic-ready"] = {"raw": raw, "optical": optical}

        action_count, sent_before = len(actions), len(gateway.sent)
        await _input(automation_base, "double_click")
        trigger = await _wait_for(
            lambda: next(
                (item for item in actions[action_count:] if item.action == "begin_acceptance"),
                None,
            ),
            label="fixed begin-acceptance action",
        )
        acceptance_ready = await _wait_for(
            lambda: _last_brainstorm(
                gateway,
                after=sent_before,
                screen="brainstorm",
                state="acceptance_ready",
            ),
            label="acceptance-ready Brainstorm frame",
        )
        digest, raw, optical = await _capture(
            automation_base,
            output,
            "06-acceptance-ready",
            digest,
        )
        receipts.append(ScreenReceipt(
            "acceptance-ready",
            "double_click",
            trigger.action,
            acceptance_ready.screen,
            acceptance_ready.state,
            acceptance_ready.acceptance_armed,
            digest,
        ))
        artifacts["acceptance-ready"] = {"raw": raw, "optical": optical}

        action_count, sent_before = len(actions), len(gateway.sent)
        await _input(automation_base, "click")
        begin = await _wait_for(
            lambda: next(
                (item for item in actions[action_count:] if item.action == "begin_topic_capture"),
                None,
            ),
            label="acceptance start tap",
        )
        running = await _wait_for(
            lambda: _last_brainstorm(
                gateway,
                after=sent_before,
                screen="brainstorm",
                state="running",
            ),
            label="streaming Brainstorm frame",
        )
        await asyncio.sleep(1.0)
        digest, raw, optical = await _capture(automation_base, output, "07-running", digest)
        receipts.append(ScreenReceipt(
            "running", "click", begin.action, running.screen, running.state,
            running.acceptance_armed, digest,
        ))
        artifacts["running"] = {"raw": raw, "optical": optical}

        action_count, sent_before = len(actions), len(gateway.sent)
        await _input(automation_base, "click")
        show_terminal = await _wait_for(
            lambda: next(
                (item for item in actions[action_count:] if item.action == "show_terminal"),
                None,
            ),
            label="show-terminal tap",
        )
        terminal_view = await _wait_for(
            lambda: _last_brainstorm(
                gateway,
                after=sent_before,
                screen="agent_terminal",
                state="running",
            ),
            label="Agent Terminal frame",
        )
        digest, raw, optical = await _capture(automation_base, output, "08-terminal", digest)
        receipts.append(ScreenReceipt(
            "terminal",
            "click",
            show_terminal.action,
            terminal_view.screen,
            terminal_view.state,
            terminal_view.acceptance_armed,
            digest,
        ))
        artifacts["terminal"] = {"raw": raw, "optical": optical}

        action_count, sent_before = len(actions), len(gateway.sent)
        await _input(automation_base, "click")
        show_brainstorm = await _wait_for(
            lambda: next(
                (item for item in actions[action_count:] if item.action == "show_brainstorm"),
                None,
            ),
            label="return-to-brainstorm tap",
        )
        returned = await _wait_for(
            lambda: _last_brainstorm(
                gateway,
                after=sent_before,
                screen="brainstorm",
                state="running",
            ),
            label="returned Brainstorm frame",
        )
        digest, raw, optical = await _capture(automation_base, output, "09-returned", digest)
        receipts.append(ScreenReceipt(
            "returned",
            "click",
            show_brainstorm.action,
            returned.screen,
            returned.state,
            returned.acceptance_armed,
            digest,
        ))
        artifacts["returned"] = {"raw": raw, "optical": optical}

        # Replace the official simulator connection. A duplicate Hello on the
        # same socket is not accepted as reconnect evidence.
        sent_before = len(gateway.sent)
        generation_before = gateway.connection_generation
        simulator.stop()
        await _wait_for(
            lambda: True if not _port_busy(args.automation_port) else None,
            label="stopped simulator automation server",
        )
        reconnected_simulator = await _start_official_simulator(
            args=args,
            plugin_url=plugin_url,
            env=env,
            output=output,
            log_name="simulator-reconnect.log",
        )
        managed.append(reconnected_simulator)
        await _wait_for(
            lambda: True
            if hello_count >= 2 and gateway.connection_generation > generation_before
            else None,
            label="authenticated replacement simulator generation",
        )
        replayed = await _wait_for(
            lambda: _last_brainstorm(
                gateway,
                after=sent_before,
                screen="brainstorm",
                state="running",
            ),
            label="restored Brainstorm frame after reconnect",
        )
        await asyncio.sleep(1.0)
        digest, raw, optical = await _capture(automation_base, output, "10-reconnected", digest)
        receipts.append(ScreenReceipt(
            "reconnected",
            None,
            "authenticated_replay",
            replayed.screen,
            replayed.state,
            replayed.acceptance_armed,
            digest,
        ))
        artifacts["reconnected"] = {"raw": raw, "optical": optical}

        # Simulator swipe-down has text delivery and no physical source. The
        # visible cancel path runs, while the receipt stays non-physical.
        action_count, sent_before = len(actions), len(gateway.sent)
        await _input(automation_base, "down")
        cancel = await _wait_for(
            lambda: next(
                (item for item in actions[action_count:] if item.action == "cancel"),
                None,
            ),
            label="simulator cancellation fallback",
        )
        cancelled = await _wait_for(
            lambda: _last_brainstorm(
                gateway,
                after=sent_before,
                screen="brainstorm",
                state="cancelled",
            ),
            label="cancelled Brainstorm frame",
        )
        digest, raw, optical = await _capture(automation_base, output, "11-cancelled", digest)
        receipts.append(ScreenReceipt(
            "cancelled",
            "down",
            cancel.action,
            cancelled.screen,
            cancelled.state,
            cancelled.acceptance_armed,
            digest,
        ))
        artifacts["cancelled"] = {"raw": raw, "optical": optical}

        await _wait_for(
            lambda: True if not sessions.active else None,
            label="completed simulator acceptance session",
        )
        receipt_paths = list(receipt_directory.glob("*-simulator.json"))
        if len(receipt_paths) != 1:
            raise BrainstormActionFlowError("acceptance did not write exactly one simulator receipt")
        receipt_path = receipt_paths[0]
        try:
            acceptance = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BrainstormActionFlowError("acceptance receipt is not readable JSON") from exc
        if not isinstance(acceptance, dict):
            raise BrainstormActionFlowError("acceptance receipt must be an object")
        validate_flow(receipts, acceptance)
        validate_visual_artifacts(artifacts)

        return {
            "schemaVersion": 2,
            "result": "simulator-brainstorm-action-flow-pass",
            "evidenceBoundary": {
                "hud": "official Even simulator raw framebuffer plus opaque green-on-black optical preview",
                "input": "official simulator input endpoint through production EventRouter",
                "mac": "production gateway, capability router, session manager, and fixed acceptance controller",
                "hermes": "deterministic no-network stream; no OpenClaw payload retained",
                "gestureSource": "simulator-fixed right temple only",
                "leftTempleDoublePress": "not available; physical G2 remains required",
                "physicalLens": "not run",
                "manifest": "disposable simulator root only; production manifest unchanged",
            },
            "receipts": [asdict(item) for item in receipts],
            "artifacts": artifacts,
            "acceptanceReceipt": {
                "file": str(receipt_path.relative_to(output)),
                "sha256": file_sha256(receipt_path),
                "mode": acceptance["mode"],
                "result": acceptance["result"],
                "retainedEventCount": acceptance["retainedEventCount"],
            },
        }
    finally:
        terminal.release()
        await sessions.close(timeout_s=2.0)
        for process in reversed(managed):
            process.stop()
        await gateway.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--vite-port", type=int, default=5297)
    parser.add_argument("--automation-port", type=int, default=9993)
    parser.add_argument("--ws-port", type=int, default=8793)
    parser.add_argument("--ws-listen-host", default="0.0.0.0")
    parser.add_argument(
        "--ws-url",
        default=None,
        help="Isolated local WebSocket gateway for the disposable simulator manifest. Defaults to --ws-port.",
    )
    parser.add_argument(
        "--simulator",
        type=Path,
        default=Path.home() / ".local/bin/evenhub-simulator",
    )
    args = parser.parse_args()
    args.repo = args.repo.resolve()
    args.out = args.out.resolve()
    if args.ws_url is None:
        args.ws_url = default_ws_url(args.ws_port)
    try:
        summary = asyncio.run(_run(args))
    except BrainstormActionFlowError as exc:
        print(f"simulator-brainstorm-action-flow: failed: {exc}")
        raise SystemExit(1) from exc
    summary_path = args.out / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    summary_path.chmod(0o600)
    print(summary_path)


if __name__ == "__main__":
    main()
