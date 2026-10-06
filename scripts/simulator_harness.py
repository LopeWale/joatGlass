#!/usr/bin/env python3
"""Shared plumbing for the official-simulator scripts.

Moved here from the Brainstorm self-test (deleted 2026-10-06, its route had
gone dead), which other simulator scripts imported for these helpers: start
and stop the dev server and simulator, wait on them, snapshot the plugin
source, capture settled frames and drive the automation API.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import time
from typing import TypeVar, cast
import urllib.error
import urllib.request
from urllib.parse import urlsplit

T = TypeVar("T")


class SimulatorFlowError(RuntimeError):
    """The observed official-simulator flow diverged from the product contract."""


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
        raise SimulatorFlowError("simulator WebSocket URL must be ws://host:port/glasses")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    source_root = repo / "apps/hub-plugin"
    try:
        manifest = json.loads((source_root / "app.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SimulatorFlowError("could not read the production Hub manifest") from exc
    permissions = manifest.get("permissions")
    if not isinstance(permissions, list):
        raise SimulatorFlowError("production Hub manifest has no permissions list")
    network = next(
        (item for item in permissions if isinstance(item, dict) and item.get("name") == "network"),
        None,
    )
    if not isinstance(network, dict) or not isinstance(network.get("whitelist"), list):
        raise SimulatorFlowError("production Hub manifest has no network whitelist")
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
    raise SimulatorFlowError(f"timed out waiting for {label}")


async def _wait_http(url: str, process: ManagedProcess) -> None:
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if process.process.poll() is not None:
            raise SimulatorFlowError(f"{process.name} exited before {url} was ready")
        try:
            await asyncio.to_thread(_http_bytes, url)
            return
        except (OSError, urllib.error.URLError):
            await asyncio.sleep(0.1)
    raise SimulatorFlowError(f"timed out waiting for {url}")


def _optical_preview(raw_path: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SimulatorFlowError("ffmpeg is required to inspect simulator alpha framebuffers")
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
        raise SimulatorFlowError("could not build opaque simulator optical preview")
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
    # Why the wait ended, for the failure message. "did not visibly settle"
    # has two OPPOSITE causes — a frame that never changed from the last
    # phase (the action did nothing) and one that never stopped changing
    # (something is repainting) — and the bare wording named neither. This
    # gate is what a release is verified against, so a run that fails
    # intermittently must say which it hit: read as a flake, a real
    # regression gets waved through; read as a regression, a slow simulator
    # blocks a good build.
    unchanged_polls = 0
    changes = 0
    reachable = False
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
        reachable = True
        digest = hashlib.sha256(raw).hexdigest()
        if prior is not None and digest == prior:
            unchanged_polls += 1
            await asyncio.sleep(0.1)
            continue
        now = time.monotonic()
        if digest != previous:
            previous, stable_since = digest, now
            changes += 1
        elif now - stable_since >= 0.8:
            raw_path = output / f"{name}.rgba.png"
            raw_path.write_bytes(raw)
            raw_path.chmod(0o600)
            optical = _optical_preview(raw_path)
            return digest, raw_path.name, optical.name
        await asyncio.sleep(0.1)
    if not reachable:
        why = "the simulator never answered /api/screenshot/glasses"
    elif changes == 0:
        why = (
            f"the frame never changed from the previous phase "
            f"({unchanged_polls} identical polls) — the action produced no "
            f"visible effect"
        )
    else:
        why = (
            f"the frame never held still for 0.8s ({changes} distinct frames) "
            f"— something is still repainting"
        )
    raise SimulatorFlowError(f"{name}: framebuffer did not visibly settle: {why}")


async def _input(automation_base: str, action: str) -> None:
    await asyncio.to_thread(
        _http_bytes,
        f"{automation_base}/api/input",
        method="POST",
        body=json.dumps({"action": action}).encode("utf-8"),
    )
