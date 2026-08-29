#!/usr/bin/env python3
"""Prove the G2 Brainstorm action flow in the official Even simulator.

This runner deliberately avoids development visual fixtures. It starts the
production Hub plugin, connects it to a real ``HubGatewayTransport``, opens
the production Mac capability view, and drives the simulator input API through
the production EventRouter. A deterministic local roundtable prevents an
external model call from becoming the test oracle.

The simulator emits only the right-temple input source. It therefore proves
tap actions and the supported downward-swipe cancellation path; physical G2
is still required for the left-temple-double shortcut.
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
from joat.core.interfaces import SttEngine, Transcript
from joat.core.protocol import BrainstormAction, BrainstormHudView, DownlinkMessage
from joat.services.capabilities import CapabilityRouter
from joat.services.hud import HudWriter
from joat.services.orchestrator import VoiceOrchestrator
from joat.services.roundtable import Participant, RoundTable
from joat.services.session import DebateSession, SessionManager
from joat.services.sinks import HudSink
from joat.services.status import StatusPublisher


class BrainstormActionFlowError(RuntimeError):
    """The observed official-simulator flow diverged from the product contract."""


@dataclass(frozen=True, slots=True)
class Receipt:
    name: str
    simulator_input: str | None
    semantic_action: str | None
    screen: str
    state: str
    framebuffer_sha256: str


class RecordingGateway(HubGatewayTransport):
    """Real gateway with a bounded in-memory downlink receipt for this run."""

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


def _prepare_simulator_plugin_root(repo: Path, output: Path, websocket_url: str) -> Path:
    """Create a disposable official-simulator root with one isolated WS origin.

    The real manifest deliberately limits production JOAT to port 8787, where
    a user-owned engine may already be running. This acceptance runner must
    not stop or replace that engine. Instead, its Vite root symlinks the exact
    current source and adds one local-only WebSocket origin for the isolated
    gateway; the production manifest is never changed.
    """
    parsed = urlsplit(websocket_url)
    if parsed.scheme != "ws" or not parsed.hostname or parsed.path != "/glasses":
        raise BrainstormActionFlowError("simulator WebSocket URL must be ws://host:port/glasses")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    source_root = repo / "apps/hub-plugin"
    manifest_path = source_root / "app.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
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
    for name in ("index.html", "src", "node_modules", "tsconfig.json"):
        os.symlink(source_root / name, root / name, target_is_directory=(source_root / name).is_dir())
    (root / "app.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.chmod(root / "app.json", 0o600)
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
    predicate: Callable[[], T | None], *, label: str, timeout_s: float = 15.0,
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


def _alpha_preview(raw_path: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise BrainstormActionFlowError("ffmpeg is required to inspect simulator alpha framebuffers")
    alpha = raw_path.with_name(f"{raw_path.stem}.alpha.png")
    completed = subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(raw_path), "-vf", "alphaextract", str(alpha)],
        capture_output=True,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0 or not alpha.is_file():
        raise BrainstormActionFlowError("could not alpha-extract simulator framebuffer")
    alpha.chmod(0o600)
    return alpha


async def _capture(
    automation_base: str,
    output: Path,
    name: str,
    prior: str | None,
) -> tuple[str, str, str]:
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
        if prior is not None and digest == prior:
            await asyncio.sleep(0.1)
            continue
        now = time.monotonic()
        if digest != previous:
            previous, stable_since = digest, now
        elif now - stable_since >= 0.6:
            raw_path = output / f"{name}.rgba.png"
            raw_path.write_bytes(raw)
            raw_path.chmod(0o600)
            alpha = _alpha_preview(raw_path)
            return digest, raw_path.name, alpha.name
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
    """Start one official glasses runtime and wait for its automation API."""
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
    gateway: RecordingGateway, *, after: int = 0, screen: str | None = None, state: str | None = None,
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

    # The official Hub simulator enforces app.json's network allowlist. Bind
    # the test gateway on every local interface and dial the existing
    # whitelisted Bonjour hostname instead of inventing a loopback exception.
    gateway = RecordingGateway(host=args.ws_listen_host, port=args.ws_port)
    connected = asyncio.Event()
    hello_count = 0
    actions: list[BrainstormAction] = []
    retry_actions: list[None] = []
    hold = asyncio.Event()

    async def codex_stream(_prompt: str) -> AsyncIterator[str]:
        yield "Exploring the constraints."
        await hold.wait()

    async def claude_stream(_prompt: str) -> AsyncIterator[str]:
        yield "Looking for the trade-offs."

    async def hermes_stream(_prompt: str) -> AsyncIterator[str]:
        yield "Checking the operational constraints."

    def make_session(topic: str) -> DebateSession:
        table = RoundTable([
            Participant("codex", "proposer", codex_stream),
            Participant("claude", "critic", claude_stream),
            # The isolated acceptance preserves the actual participant shape
            # after the app has admitted Hermes, while replacing only remote
            # Gateway I/O with a deterministic stream.
            Participant("hermes", "operator", hermes_stream),
        ])
        return DebateSession(
            topic,
            table,
            HudSink(HudWriter(gateway), lambda: {
                "state": "ready",
                "reason": "",
                "endpoint_host": "home-mac.example-tailnet.ts.net",
                "no_tools_checked": True,
            }),
            judge=None,
        )

    def topic_capture_view() -> BrainstormHudView:
        return BrainstormHudView(
            screen="brainstorm",
            state="ready",
            topic="Speak your question",
            collaborators=["Codex", "Claude", "Hermes"],
            activity='Say “JOAT, brainstorm …” to begin.',
            terminal_state="ready",
            endpoint_host="home-mac.example-tailnet.ts.net",
            terminal_reason="",
            no_tools_checked=True,
        )

    status = StatusPublisher(path=output / "status.json")
    status.set_openclaw(
        state="connected_operator",
        endpoint_host="home-mac.example-tailnet.ts.net",
        scopes=("operator.read", "operator.write"),
        reason="",
    )
    status.set_agent_terminal(state="ready", reason="")
    hud = HudWriter(gateway)

    async def retry_terminal() -> None:
        """Deterministic stand-in for the production signed retry boundary.

        This visual acceptance deliberately does not dial the private Gateway,
        but it drives the same SessionManager retry callback that production
        invokes after the wearer taps an approval/unavailable terminal frame.
        """
        retry_actions.append(None)
        status.set_openclaw(
            state="connected_operator",
            endpoint_host="home-mac.example-tailnet.ts.net",
            scopes=("operator.read", "operator.write"),
            reason="",
        )
        status.set_agent_terminal(state="ready", reason="")
        await hud.show_brainstorm(BrainstormHudView(
            screen="agent_terminal",
            state="ready",
            topic="Hermes Agent Terminal",
            collaborators=[],
            activity="Dedicated no-tool session ready. Tap to begin a Brainstorm.",
            terminal_state="ready",
            endpoint_host="home-mac.example-tailnet.ts.net",
            terminal_reason="",
            no_tools_checked=True,
        ))

    manager = SessionManager(
        hud,
        make_session,
        terminal_retry=retry_terminal,
        topic_capture_view=topic_capture_view,
    )

    class InertStt(SttEngine):
        """The reconnect path never transcribes, but requires an STT shape."""

        async def feed(self, _pcm: bytes) -> Transcript | None:
            return None

        async def flush(self) -> Transcript | None:
            return None

        def reset(self) -> None:
            return None

    orchestrator = VoiceOrchestrator(gateway, InertStt(), sessions=manager)
    capabilities = CapabilityRouter(
        hud=hud,
        transport=gateway,
        status=status,
        messages=None,
        cards=CardLog(),
    )
    def on_hello(_hello: object) -> None:
        nonlocal hello_count
        hello_count += 1
        connected.set()
        asyncio.get_running_loop().create_task(orchestrator.on_connect())

    gateway.on_hello(on_hello)

    def record_brainstorm_action(action: BrainstormAction) -> None:
        actions.append(action)
        manager.handle_brainstorm_action(action)

    gateway.on_brainstorm_action(record_brainstorm_action)

    managed: list[ManagedProcess] = []
    receipts: list[Receipt] = []
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
                "--port", str(args.vite_port), "--strictPort",
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
        try:
            await _wait_for(
                lambda: True if gateway.connected else None,
                label="authenticated simulator WebSocket",
                timeout_s=20,
            )
        except BrainstormActionFlowError as exc:
            raise BrainstormActionFlowError(
                "Hub plugin never reached the isolated Mac gateway; the simulator WebSocket dial is blocked"
            ) from exc
        try:
            # Boot may consume the bounded 3s device lookup plus the bounded
            # 3s optional-account lookup before it can announce itself.
            await asyncio.wait_for(connected.wait(), timeout=15)
        except asyncio.TimeoutError as exc:
            # Preserve the actual optical boot state when an app-side boot
            # failure prevents the required hello, rather than leaving only a
            # terminal timeout to diagnose a wearer-visible failure.
            try:
                await _capture(automation_base, output, "00-authenticated-no-hello", None)
            except BrainstormActionFlowError:
                pass
            raise BrainstormActionFlowError(
                "Hub plugin authenticated but never sent its required hello frame"
            ) from exc

        # This is the real Mac menu target. The simulator cannot send a
        # left-temple event, so this selects the already-supported row rather
        # than claiming a menu-source proof it cannot supply.
        await capabilities.open("Brainstorm")
        ready = await _wait_for(lambda: _last_brainstorm(gateway, screen="brainstorm", state="ready"), label="ready Brainstorm view")
        digest, raw, alpha = await _capture(automation_base, output, "01-ready", None)
        receipts.append(Receipt("ready", None, None, ready.screen, ready.state, digest))
        artifacts["ready"] = {"raw": raw, "alpha": alpha}

        # Recovery is visible and wearer-controlled. Start with the exact
        # approval-required terminal state, then let the real Hub controller
        # emit retry_terminal on a simulator click. The callback above is the
        # deterministic seam replacing only the signed Tailscale retry.
        status.set_openclaw(
            state="approval_required",
            endpoint_host="home-mac.example-tailnet.ts.net",
            scopes=("operator.read", "operator.write"),
            reason="device_approval_required",
        )
        status.set_agent_terminal(state="approval_required", reason="write_scope_required")
        sent_before = len(gateway.sent)
        await capabilities.open("Agent Terminal")
        approval = await _wait_for(
            lambda: _last_brainstorm(
                gateway,
                after=sent_before,
                screen="agent_terminal",
                state="approval_required",
            ),
            label="approval-required Hermes terminal frame",
        )
        digest, raw, alpha = await _capture(automation_base, output, "02-approval-required", digest)
        receipts.append(Receipt("approval-required", None, None, approval.screen, approval.state, digest))
        artifacts["approval-required"] = {"raw": raw, "alpha": alpha}

        action_count, sent_before = len(actions), len(gateway.sent)
        await _input(automation_base, "click")
        retry = await _wait_for(
            lambda: next((item for item in actions[action_count:] if item.action == "retry_terminal"), None),
            label="retry-terminal action",
        )
        await _wait_for(
            lambda: True if retry_actions else None,
            label="terminal retry callback",
        )
        retried = await _wait_for(
            lambda: _last_brainstorm(
                gateway,
                after=sent_before,
                screen="agent_terminal",
                state="ready",
            ),
            label="recovered Hermes terminal frame",
        )
        digest, raw, alpha = await _capture(automation_base, output, "03-terminal-retried", digest)
        receipts.append(Receipt("terminal-retried", "click", retry.action, retried.screen, retried.state, digest))
        artifacts["terminal-retried"] = {"raw": raw, "alpha": alpha}

        # Return to the primary Brainstorm surface before starting a topic.
        sent_before = len(gateway.sent)
        await capabilities.open("Brainstorm")
        ready_after_retry = await _wait_for(
            lambda: _last_brainstorm(gateway, after=sent_before, screen="brainstorm", state="ready"),
            label="recovered ready Brainstorm view",
        )

        action_count, sent_before = len(actions), len(gateway.sent)
        await _input(automation_base, "click")
        begin = await _wait_for(
            lambda: next((item for item in actions[action_count:] if item.action == "begin_topic_capture"), None),
            label="begin-topic-capture action",
        )
        capture = await _wait_for(
            lambda: _last_brainstorm(
                gateway,
                after=sent_before,
                screen="brainstorm",
                state="ready",
            ),
            label="visible topic-capture Brainstorm frame",
        )
        digest, raw, alpha = await _capture(automation_base, output, "04-topic-capture", digest)
        receipts.append(Receipt("topic-capture", "click", begin.action, capture.screen, capture.state, digest))
        artifacts["topic-capture"] = {"raw": raw, "alpha": alpha}

        # STT is outside this deterministic simulator lane. This call enters
        # the real SessionManager trigger grammar that production STT invokes.
        sent_before = len(gateway.sent)
        if not manager.handle_transcript("brainstorm compare two safe plans"):
            raise BrainstormActionFlowError("SessionManager rejected the Brainstorm trigger")
        running = await _wait_for(
            lambda: _last_brainstorm(gateway, after=sent_before, screen="brainstorm", state="running"),
            label="live Brainstorm frame",
        )
        digest, raw, alpha = await _capture(automation_base, output, "05-running", digest)
        receipts.append(Receipt("running", None, "voice_trigger", running.screen, running.state, digest))
        artifacts["running"] = {"raw": raw, "alpha": alpha}

        action_count, sent_before = len(actions), len(gateway.sent)
        await _input(automation_base, "click")
        terminal_action = await _wait_for(
            lambda: next((item for item in actions[action_count:] if item.action == "show_terminal"), None),
            label="show-terminal action",
        )
        terminal = await _wait_for(
            lambda: _last_brainstorm(gateway, after=sent_before, screen="agent_terminal", state="running"),
            label="live Hermes terminal frame",
        )
        digest, raw, alpha = await _capture(automation_base, output, "06-terminal", digest)
        receipts.append(Receipt("terminal", "click", terminal_action.action, terminal.screen, terminal.state, digest))
        artifacts["terminal"] = {"raw": raw, "alpha": alpha}

        action_count, sent_before = len(actions), len(gateway.sent)
        await _input(automation_base, "click")
        back = await _wait_for(
            lambda: next((item for item in actions[action_count:] if item.action == "show_brainstorm"), None),
            label="return-to-brainstorm action",
        )
        restored = await _wait_for(
            lambda: _last_brainstorm(gateway, after=sent_before, screen="brainstorm", state="running"),
            label="restored Brainstorm frame",
        )
        digest, raw, alpha = await _capture(automation_base, output, "07-returned", digest)
        receipts.append(Receipt("returned", "click", back.action, restored.screen, restored.state, digest))
        artifacts["returned"] = {"raw": raw, "alpha": alpha}

        # A real Hub replacement loses all in-memory visual objects. Restart
        # the official simulator against the same production Vite build and
        # require the production on_hello → VoiceOrchestrator.on_connect
        # path to replay the active typed Brainstorm frame on the new socket.
        sent_before = len(gateway.sent)
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
            lambda: True if hello_count >= 2 else None,
            label="authenticated replacement simulator hello",
        )
        replayed = await _wait_for(
            lambda: _last_brainstorm(
                gateway,
                after=sent_before,
                screen="brainstorm",
                state="running",
            ),
            label="replayed active Brainstorm frame after reconnect",
        )
        # The gateway receipt is not the same as a finished four-tile draw.
        # Let the replacement Hub finish the currently accepted downlink
        # before sampling the optical framebuffer; the frame itself remains
        # the acceptance criterion below.
        await asyncio.sleep(2.0)
        digest, raw, alpha = await _capture(automation_base, output, "08-reconnected", digest)
        receipts.append(Receipt("reconnected", None, "authenticated_replay", replayed.screen, replayed.state, digest))
        artifacts["reconnected"] = {"raw": raw, "alpha": alpha}

        action_count, sent_before = len(actions), len(gateway.sent)
        await _input(automation_base, "down")
        cancel = await _wait_for(
            lambda: next((item for item in actions[action_count:] if item.action == "cancel"), None),
            label="swipe cancellation action",
        )
        cancelled = await _wait_for(
            lambda: _last_brainstorm(gateway, after=sent_before, screen="brainstorm", state="cancelled"),
            label="cancelled Brainstorm frame",
        )
        digest, raw, alpha = await _capture(automation_base, output, "09-cancelled", digest)
        receipts.append(Receipt("cancelled", "down", cancel.action, cancelled.screen, cancelled.state, digest))
        artifacts["cancelled"] = {"raw": raw, "alpha": alpha}

        expected = [
            ("ready", None, "brainstorm", "ready"),
            ("approval-required", None, "agent_terminal", "approval_required"),
            ("terminal-retried", "retry_terminal", "agent_terminal", "ready"),
            ("topic-capture", "begin_topic_capture", "brainstorm", "ready"),
            ("running", "voice_trigger", "brainstorm", "running"),
            ("terminal", "show_terminal", "agent_terminal", "running"),
            ("returned", "show_brainstorm", "brainstorm", "running"),
            ("reconnected", "authenticated_replay", "brainstorm", "running"),
            ("cancelled", "cancel", "brainstorm", "cancelled"),
        ]
        observed = [(item.name, item.semantic_action, item.screen, item.state) for item in receipts]
        if observed != expected:
            raise BrainstormActionFlowError(f"unexpected rendered action flow: {observed!r}")
        return {
            "schemaVersion": 1,
            "result": "simulator-brainstorm-recovery-flow-pass",
            "evidenceBoundary": {
                "hud": "official Even simulator alpha-extracted framebuffer",
                "input": "official simulator input endpoint through production EventRouter",
                "mac": "real HubGatewayTransport, CapabilityRouter, SessionManager, DebateSession, and HudSink",
                "topicStart": "real SessionManager trigger grammar; deterministic test seam replaces STT/model I/O",
                "approvalRetry": "production BrainstormController action and SessionManager retry callback; deterministic seam replaces the signed Gateway retry",
                "reconnect": "replacement official simulator socket triggers the production on_hello → VoiceOrchestrator replay path",
                "leftTempleDoublePress": "not available from official simulator; physical G2 remains required",
                "hermesGateway": "not contacted by this isolated visual acceptance",
                "physicalG2": "not run",
                "manifest": "disposable simulator-only root adds the isolated local gateway origin; production app.json is unchanged",
            },
            "receipts": [asdict(item) for item in receipts],
            "artifacts": artifacts,
            "semanticActions": [item.action for item in actions],
        }
    finally:
        hold.set()
        await manager.close(timeout_s=1)
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
        default="ws://laptop-mac.local:8793/glasses",
        help="Local-only isolated WebSocket gateway for the disposable simulator manifest.",
    )
    parser.add_argument("--simulator", type=Path, default=Path.home() / ".local/bin/evenhub-simulator")
    args = parser.parse_args()
    args.repo = args.repo.resolve()
    args.out = args.out.resolve()
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
