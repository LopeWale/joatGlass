#!/usr/bin/env python3
"""Run one real Calendar + Reminders safe-write acceptance scenario.

The driver deliberately reuses the production controller, native bridge,
durable action journal, localhost Hub gateway, and official Even simulator.
Only randomized synthetic titles are written.  Retained receipts contain
hashes and state, never native identifiers, notification text, or action
payloads.

The signed Mac app remains the TCC owner throughout the run.  The harness
temporarily points its supervised Python engine at a private inert stub so no
second bridge client or gateway can race the acceptance driver, then restores
the exact prior repo preference and relaunches normal operation.
EventKit permission prompts remain UI only: if Calendar or Reminders is not at
full access, this exits before any native write and tells the operator to use
the app's Connect buttons.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PIL import Image

from joat.ble.hub_gateway import HubGatewayTransport
from joat.core.pairing import PairingStore
from joat.core.protocol import AudioControl, HudList
from joat.native.bridge import NativeBridgeClient
from joat.persistence.actions import ActionJournal
from joat.persistence.store import DurableStore
from joat.persistence.tasks import TaskOccurrenceStore
from joat.services.g2_diagnostics import G2DiagnosticStore
from joat.services.productivity_actions import ProductivityActionController

log = logging.getLogger("joat.simulator_productivity_native_acceptance")

RECEIPT_SCHEMA = "joat.native-productivity-acceptance.v1"
TITLE_PREFIX = "JOAT ACCEPTANCE"
DEFAULT_BRIDGE = Path.home() / "Library/Application Support/JOAT/native-bridge.sock"
DEFAULT_NOTIFICATION_FEED = (
    Path.home() / "Library/Application Support/JOAT/notifications.jsonl"
)
DEFAULT_NOTIFICATION_HEARTBEAT = (
    Path.home() / "Library/Application Support/JOAT/notifications.heartbeat"
)
DEFAULT_EVIDENCE_ROOT = (
    Path.home() / "Library/Application Support/JOAT/acceptance/productivity"
)
DEFAULT_APP = Path("/Applications/JOAT.app")
DEFAULT_APP_BUNDLE_ID = "com.joat.shell"
DEFAULT_RECOVERY_GUARD = DEFAULT_EVIDENCE_ROOT / ".mac-app-recovery.json"


class AcceptanceSafetyError(RuntimeError):
    """The harness refused an unsafe input or cleanup target."""


class AcceptanceBlocked(RuntimeError):
    """A required live environment boundary was unavailable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AcceptanceFailed(RuntimeError):
    """The live path ran but did not satisfy an acceptance invariant."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class AcceptanceSchedule:
    event_start: datetime
    event_end: datetime
    reminder_due: datetime


@dataclass(frozen=True, slots=True)
class AlarmDeliveryEvidence:
    observed_at_unix_ms: int
    source_digest: str
    content_digest: str


@dataclass(frozen=True, slots=True)
class CleanupOutcome:
    calendar_removed_count: int
    calendar_target_removed_count: int
    calendar_other_removed_count: int
    reminder_removed_count: int
    errors: tuple[str, ...]


@dataclass(slots=True)
class ManagedProcess:
    name: str
    process: subprocess.Popen[bytes]

    def stop(self) -> None:
        if self.process.poll() is not None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=4)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if self.process.poll() is None:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass


@dataclass(frozen=True, slots=True)
class DefaultsPreference:
    present: bool
    value: str | None


def repo_preference_restore_command(
    bundle_id: str,
    preference: DefaultsPreference,
) -> list[str]:
    if preference.present:
        if preference.value is None:
            raise AcceptanceSafetyError("present RepoPath preference had no value")
        return [
            "/usr/bin/defaults",
            "write",
            bundle_id,
            "RepoPath",
            preference.value,
        ]
    return ["/usr/bin/defaults", "delete", bundle_id, "RepoPath"]


def repo_preference_from_defaults_result(
    *,
    returncode: int,
    stdout: str,
    stderr: str,
) -> DefaultsPreference:
    if returncode != 0:
        if "Could not find key 'RepoPath'" not in stderr and "does not exist" not in stderr:
            raise AcceptanceSafetyError("could not read RepoPath preference safely")
        return DefaultsPreference(present=False, value=None)
    value = stdout.strip()
    if not value or "\n" in value or "\r" in value:
        raise AcceptanceSafetyError("RepoPath preference was not a single nonempty path")
    return DefaultsPreference(present=True, value=value)


def _read_repo_preference(bundle_id: str) -> DefaultsPreference:
    completed = subprocess.run(
        ["/usr/bin/defaults", "read", bundle_id, "RepoPath"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return repo_preference_from_defaults_result(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def create_engine_stub_repo(parent: Path) -> Path:
    """Create an inert child executable accepted by EngineController.isRepo."""

    repo = parent / "native-bridge-only-repo"
    executable = repo / ".venv/bin/python"
    executable.parent.mkdir(parents=True, mode=0o700)
    executable.write_text(
        "#!/usr/bin/python3\n"
        "import signal\n"
        "import sys\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "signal.signal(signal.SIGINT, lambda *_: sys.exit(0))\n"
        "signal.pause()\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return repo


def _network_whitelist(manifest_path: Path) -> list[str]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceSafetyError("could not read the Hub plugin manifest") from exc
    permissions = manifest.get("permissions") if isinstance(manifest, dict) else None
    if not isinstance(permissions, list):
        raise AcceptanceSafetyError("Hub plugin manifest has no permissions list")
    for permission in permissions:
        if not isinstance(permission, dict) or permission.get("name") != "network":
            continue
        whitelist = permission.get("whitelist")
        if isinstance(whitelist, list) and all(isinstance(item, str) for item in whitelist):
            return cast(list[str], whitelist)
    raise AcceptanceSafetyError("Hub plugin manifest has no network whitelist")


def resolve_simulator_ws_url(manifest_path: Path, *, gateway_port: int) -> str:
    suffix = f".local:{gateway_port}"
    candidates = [
        item
        for item in _network_whitelist(manifest_path)
        if item.startswith("ws://") and item.casefold().endswith(suffix.casefold())
    ]
    if len(candidates) != 1:
        raise AcceptanceSafetyError(
            "expected one allowlisted local WebSocket origin for the acceptance gateway"
        )
    return f"{candidates[0]}/glasses"


def _port_is_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _app_process_is_running(app_path: Path) -> bool:
    return _app_process_id(app_path) is not None


def _app_process_id(app_path: Path) -> int | None:
    executable = str(app_path / "Contents/MacOS/JOAT")
    completed = subprocess.run(
        ["/usr/bin/pgrep", "-f", f"^{re.escape(executable)}$"],
        check=False,
        capture_output=True,
        timeout=2,
    )
    if completed.returncode != 0:
        return None
    lines = completed.stdout.decode("utf-8", errors="replace").splitlines()
    try:
        pids = [int(line) for line in lines if line.strip()]
    except ValueError:
        return None
    return pids[0] if len(pids) == 1 else None


def _port_is_owned_by_app_child(app_path: Path, port: int) -> bool:
    app_pid = _app_process_id(app_path)
    if app_pid is None:
        return False
    listeners = subprocess.run(
        ["/usr/sbin/lsof", "-tiTCP:" + str(port), "-sTCP:LISTEN"],
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )
    for value in listeners.stdout.splitlines():
        if not value.strip().isdigit():
            continue
        parent = subprocess.run(
            ["/bin/ps", "-o", "ppid=", "-p", value.strip()],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
        if parent.returncode == 0 and parent.stdout.strip() == str(app_pid):
            return True
    return False


def _fsync_directory(path: Path) -> None:
    """Persist creation or removal of a safety-critical directory entry."""

    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


async def _wait_until(predicate: Any, *, timeout_s: float, description: str) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.2)
    raise AcceptanceBlocked("mac_app_transition_timeout", f"timed out waiting for {description}")


class MacAppNativeBridgeLease:
    """Keep JOAT.app's bridge live without starting the production engine."""

    def __init__(
        self,
        *,
        app_path: Path,
        bundle_id: str,
        bridge_path: Path,
        gateway_port: int,
        stub_repo: Path,
        recovery_guard: Path,
    ) -> None:
        self.app_path = app_path
        self.bundle_id = bundle_id
        self.bridge_path = bridge_path
        self.gateway_port = gateway_port
        self.stub_repo = stub_repo
        self.recovery_guard = recovery_guard
        self.preference: DefaultsPreference | None = None
        self.preference_changed = False
        self.transition_started = False
        self.restored = False

    @staticmethod
    def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            check=check,
            capture_output=True,
            text=True,
            timeout=90,
        )

    async def _quit_app(self) -> None:
        try:
            self._run(
                [
                    "/usr/bin/osascript",
                    "-e",
                    f'tell application id "{self.bundle_id}" to quit',
                ],
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AcceptanceBlocked(
                "mac_app_quit_timeout",
                "JOAT.app did not finish its graceful engine shutdown within 90 seconds",
            ) from exc
        await _wait_until(
            lambda: not self.bridge_path.exists() and not _app_process_is_running(self.app_path),
            timeout_s=15,
            description="JOAT.app to stop its native bridge and exit",
        )

    async def _open_app(self, *, expected_port: int | None) -> None:
        launch_deadline = time.monotonic() + 15
        opened: subprocess.CompletedProcess[str] | None = None
        while time.monotonic() < launch_deadline:
            opened = self._run(["/usr/bin/open", str(self.app_path)], check=False)
            if opened.returncode == 0:
                break
            await asyncio.sleep(0.5)
        if opened is None or opened.returncode != 0:
            raise AcceptanceBlocked(
                "mac_app_launch_failed",
                "Launch Services did not reopen JOAT.app after its prior process exited",
            )
        await _wait_until(
            lambda: self.bridge_path.exists()
            and _app_process_is_running(self.app_path)
            and (
                expected_port is None
                or _port_is_owned_by_app_child(self.app_path, expected_port)
            ),
            timeout_s=300 if expected_port is not None else 20,
            description=(
                f"JOAT.app bridge and engine port {expected_port}"
                if expected_port is not None
                else "JOAT.app native bridge"
            ),
        )

    def _write_recovery_guard(self, preference: DefaultsPreference) -> None:
        self.recovery_guard.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.recovery_guard.parent, 0o700)
        payload = json.dumps(
            {
                "schema": "joat.native-productivity-app-recovery.v1",
                "bundle_id": self.bundle_id,
                "repo_preference": asdict(preference),
                "stub_repo": str(self.stub_repo),
                "owner_pid": os.getpid(),
                "created_at_unix_ms": int(time.time() * 1_000),
            },
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        try:
            fd = os.open(
                self.recovery_guard,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError as exc:
            raise AcceptanceBlocked(
                "acceptance_lease_exists",
                "another native acceptance recovery guard already exists",
            ) from exc
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _fsync_directory(self.recovery_guard.parent)

    def _read_recovery_guard(self) -> tuple[DefaultsPreference, int, Path]:
        info = self.recovery_guard.lstat()
        if (
            self.recovery_guard.is_symlink()
            or not self.recovery_guard.is_file()
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise AcceptanceSafetyError("Mac app recovery guard is not owner-only")
        try:
            value = json.loads(self.recovery_guard.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AcceptanceSafetyError("Mac app recovery guard is unreadable") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema") != "joat.native-productivity-app-recovery.v1"
            or value.get("bundle_id") != self.bundle_id
        ):
            raise AcceptanceSafetyError("Mac app recovery guard identity did not match")
        raw = value.get("repo_preference")
        owner_pid = value.get("owner_pid")
        stub_repo = value.get("stub_repo")
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("present"), bool)
            or isinstance(owner_pid, bool)
            or not isinstance(owner_pid, int)
            or owner_pid <= 0
            or not isinstance(stub_repo, str)
        ):
            raise AcceptanceSafetyError("Mac app recovery guard preference was invalid")
        present = raw["present"]
        preference_value = raw.get("value")
        if present and not isinstance(preference_value, str):
            raise AcceptanceSafetyError("Mac app recovery guard path was invalid")
        if not present and preference_value is not None:
            raise AcceptanceSafetyError("Mac app recovery guard absence was invalid")
        return DefaultsPreference(present=present, value=preference_value), owner_pid, Path(stub_repo)

    async def _recover_stale_lease(self) -> None:
        if not self.recovery_guard.exists():
            return
        self.preference, owner_pid, stale_stub = self._read_recovery_guard()
        if owner_pid != os.getpid():
            try:
                os.kill(owner_pid, 0)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                raise AcceptanceBlocked(
                    "acceptance_lease_owner_unknown",
                    "the existing acceptance lease owner could not be verified",
                ) from exc
            else:
                raise AcceptanceBlocked(
                    "acceptance_already_running",
                    "another native productivity acceptance process is still active",
                )
        if (
            _read_repo_preference(self.bundle_id) == self.preference
            and self.bridge_path.exists()
            and _port_is_owned_by_app_child(self.app_path, self.gateway_port)
        ):
            self.recovery_guard.unlink()
            _fsync_directory(self.recovery_guard.parent)
            guard_root = self.recovery_guard.parent.resolve()
            try:
                stale_resolved = stale_stub.resolve()
                if (
                    stale_stub.name == "native-bridge-only-repo"
                    and stale_resolved.is_relative_to(guard_root)
                ):
                    shutil.rmtree(stale_resolved, ignore_errors=True)
            except OSError:
                pass
            self.restored = True
            return
        self.transition_started = True
        self.preference_changed = True
        await self.restore()
        guard_root = self.recovery_guard.parent.resolve()
        try:
            stale_resolved = stale_stub.resolve()
            if (
                stale_stub.name == "native-bridge-only-repo"
                and stale_resolved.is_relative_to(guard_root)
            ):
                shutil.rmtree(stale_resolved, ignore_errors=True)
        except OSError:
            pass
        self.preference = None
        self.transition_started = False
        self.preference_changed = False
        self.restored = False

    async def enter(self) -> None:
        if not self.app_path.is_dir():
            raise AcceptanceBlocked("mac_app_missing", f"signed Mac app not found at {self.app_path}")
        stub_executable = self.stub_repo / ".venv/bin/python"
        if not stub_executable.is_file() or not os.access(stub_executable, os.X_OK):
            raise AcceptanceSafetyError("native-bridge-only engine stub is unavailable")
        await self._recover_stale_lease()
        self.preference = _read_repo_preference(self.bundle_id)
        self._write_recovery_guard(self.preference)
        self.transition_started = True
        await self._quit_app()
        self._run(
            [
                "/usr/bin/defaults",
                "write",
                self.bundle_id,
                "RepoPath",
                str(self.stub_repo),
            ]
        )
        self.preference_changed = True
        await self._open_app(expected_port=None)
        if _port_is_listening(self.gateway_port):
            raise AcceptanceBlocked(
                "production_gateway_port_busy",
                f"gateway port {self.gateway_port} was not released by JOAT.app",
            )

    async def restore(self) -> None:
        if not self.transition_started or self.preference is None:
            return
        await self._quit_app()
        if self.preference_changed:
            restore_command = repo_preference_restore_command(self.bundle_id, self.preference)
            self._run(restore_command, check=self.preference.present)
            restored_preference = _read_repo_preference(self.bundle_id)
            if restored_preference != self.preference:
                raise AcceptanceBlocked(
                    "mac_app_preference_restore_mismatch",
                    "RepoPath did not read back as the exact pre-run preference",
                )
        await self._open_app(expected_port=self.gateway_port)
        try:
            self.recovery_guard.unlink()
        except FileNotFoundError:
            pass
        else:
            _fsync_directory(self.recovery_guard.parent)
        self.restored = True


def plan_acceptance_schedule(
    now: datetime,
    *,
    minimum_lead_seconds: int = 120,
) -> AcceptanceSchedule:
    """Choose a whole-minute alarm with the requested observation lead."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("acceptance time must be timezone-aware")
    if minimum_lead_seconds < 60:
        raise ValueError("minimum lead must be at least 60 seconds")
    candidate = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    while (candidate - now).total_seconds() < minimum_lead_seconds:
        candidate += timedelta(minutes=1)
    return AcceptanceSchedule(
        event_start=candidate,
        event_end=candidate + timedelta(minutes=1),
        reminder_due=candidate,
    )


def validate_acceptance_title(title: str, run_id: str) -> None:
    expected = f"{TITLE_PREFIX} {run_id}"
    if title != expected or len(run_id) != 8 or any(c not in "0123456789abcdef" for c in run_id):
        raise AcceptanceSafetyError("cleanup requires the exact randomized acceptance title")


def _digest(value: str | bytes) -> str:
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def redact_native_reference(
    *,
    action_kind: str,
    action_id: str,
    native_identifier: str,
    evidence_digest: str,
) -> dict[str, str]:
    return {
        "action_kind": action_kind,
        "action_ref": _digest(action_id)[:12],
        "native_ref_digest": _digest(native_identifier),
        "evidence_digest": evidence_digest.lower(),
    }


def write_private_receipt(path: Path, receipt: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    payload = json.dumps(receipt, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    temporary = path.with_name(f".{path.name}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def secure_notification_feed(path: Path) -> None:
    """Refuse foreign/symlink feeds and harden a legacy readable file."""

    info = path.lstat()
    if path.is_symlink() or info.st_uid != os.geteuid() or not path.is_file():
        raise AcceptanceSafetyError("notification feed is not an owned regular file")
    os.chmod(path, 0o600)


def alarm_delivery_after(
    path: Path,
    *,
    offset: int,
    exact_title: str,
    not_before_unix_ms: int,
    now_unix_ms: int,
    expected_feed_identity: tuple[int, int] | None = None,
) -> AlarmDeliveryEvidence | None:
    """Return redacted evidence for a new exact synthetic Reminders banner."""

    current = path.stat()
    if (
        expected_feed_identity is not None
        and (current.st_dev, current.st_ino) != expected_feed_identity
    ):
        raise AcceptanceBlocked(
            "notification_feed_replaced",
            "the notification feed rotated or replaced during alarm observation",
        )
    if current.st_size < offset:
        raise AcceptanceBlocked(
            "notification_feed_rotated",
            "the notification feed rotated or truncated during alarm observation",
        )
    with path.open("rb") as stream:
        stream.seek(offset)
        raw = stream.read()
    complete = raw[: raw.rfind(b"\n") + 1]
    for line in complete.splitlines():
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        app = str(value.get("app") or "")
        title = str(value.get("title") or "")
        body = str(value.get("body") or "")
        if "reminders" not in app.casefold():
            continue
        if exact_title != title and exact_title not in body:
            continue
        raw_at = value.get("at")
        try:
            if isinstance(raw_at, bool) or not isinstance(raw_at, (int, float, str)):
                continue
            observed_at_ms = int(float(raw_at) * 1_000)
        except (TypeError, ValueError):
            continue
        if observed_at_ms < not_before_unix_ms or observed_at_ms > now_unix_ms + 2_000:
            continue
        content = json.dumps(
            {"app": app, "title": title, "body": body},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return AlarmDeliveryEvidence(
            observed_at_unix_ms=observed_at_ms,
            source_digest=_digest(app),
            content_digest=_digest(content),
        )
    return None


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


def alpha_preview_command(ffmpeg: Path, source: Path, destination: Path) -> list[str]:
    return [
        str(ffmpeg),
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-vf",
        "alphaextract",
        str(destination),
    ]


def alpha_preview_has_content(path: Path) -> bool:
    with Image.open(path) as image:
        low, high = image.convert("L").getextrema()
    return low != high


def _write_alpha_preview(source: Path) -> tuple[Path, str]:
    resolved = shutil.which("ffmpeg")
    if resolved is None:
        raise AcceptanceBlocked(
            "ffmpeg_missing",
            "ffmpeg is required to expose the simulator framebuffer alpha channel",
        )
    destination = source.with_name(f"{source.stem}.alpha.png")
    completed = subprocess.run(
        alpha_preview_command(Path(resolved), source, destination),
        check=False,
        capture_output=True,
        timeout=15,
    )
    if (
        completed.returncode != 0
        or not destination.is_file()
        or not alpha_preview_has_content(destination)
    ):
        raise AcceptanceBlocked(
            "hud_alpha_extract_failed",
            "the simulator framebuffer could not be converted into a reviewable HUD preview",
        )
    destination.chmod(0o600)
    return destination, _digest(destination.read_bytes())


async def _wait_http(url: str, *, timeout_s: float, process: ManagedProcess | None = None) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process is not None and process.process.poll() is not None:
            raise AcceptanceBlocked(
                f"{process.name}_exited",
                f"{process.name} exited before its health endpoint became ready",
            )
        try:
            await asyncio.to_thread(_http_bytes, url)
            return
        except (OSError, urllib.error.URLError):
            await asyncio.sleep(0.2)
    raise AcceptanceBlocked("endpoint_timeout", f"timed out waiting for {url}")


async def _wait_for_confirmation_and_click(
    automation_base: str,
    screenshot_path: Path,
    *,
    timeout_s: float = 10.0,
) -> tuple[str, str]:
    try:
        await asyncio.to_thread(
            _http_bytes,
            f"{automation_base}/api/console",
            method="DELETE",
        )
    except (OSError, urllib.error.URLError) as exc:
        raise AcceptanceBlocked(
            "simulator_console_reset_failed",
            "the simulator console could not be cleared before the confirmation action",
        ) from exc
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        raw = await asyncio.to_thread(_http_bytes, f"{automation_base}/api/console")
        if "confirmation-visible" in raw.decode("utf-8", errors="replace"):
            png = await asyncio.to_thread(
                _http_bytes,
                f"{automation_base}/api/screenshot/glasses",
            )
            screenshot_path.write_bytes(png)
            screenshot_path.chmod(0o600)
            _, preview_digest = await asyncio.to_thread(_write_alpha_preview, screenshot_path)
            await asyncio.to_thread(
                _http_bytes,
                f"{automation_base}/api/input",
                method="POST",
                body=b'{"action":"click"}',
            )
            return _digest(png), preview_digest
        await asyncio.sleep(0.1)
    raise AcceptanceBlocked(
        "confirmation_not_visible",
        "the official simulator did not report a visible confirmation before timeout",
    )


async def _capture_screenshot(automation_base: str, path: Path) -> tuple[str, str]:
    await asyncio.sleep(0.4)
    png = await asyncio.to_thread(_http_bytes, f"{automation_base}/api/screenshot/glasses")
    path.write_bytes(png)
    path.chmod(0o600)
    _, preview_digest = await asyncio.to_thread(_write_alpha_preview, path)
    return _digest(png), preview_digest


def _start_process(name: str, command: list[str], *, cwd: Path, env: dict[str, str]) -> ManagedProcess:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return ManagedProcess(name=name, process=process)


def _osascript(script: str, title: str) -> int:
    completed = subprocess.run(
        ["/usr/bin/osascript", "-e", script, title],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    value = completed.stdout.strip()
    return int(value) if value else 0


def _cleanup_calendar_items(
    script: str,
    title: str,
    event_calendar_identifier: str | None,
) -> tuple[int, int]:
    completed = subprocess.run(
        [
            "/usr/bin/osascript",
            "-e",
            script,
            title,
            event_calendar_identifier or "",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    fields = [field.strip() for field in completed.stdout.strip().split(",")]
    if len(fields) != 2 or any(not field.isdecimal() for field in fields):
        raise AcceptanceSafetyError("Calendar cleanup returned invalid cardinality evidence")
    return int(fields[0]), int(fields[1])


def _native_app_is_running(name: str) -> bool:
    status = subprocess.run(
        ["/usr/bin/osascript", "-e", f'application "{name}" is running'],
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )
    return status.returncode == 0 and status.stdout.strip() == "true"


def _ensure_native_app_running(name: str) -> bool:
    if _native_app_is_running(name):
        return False
    opened = subprocess.run(
        ["/usr/bin/open", "-gj", "-a", name],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if opened.returncode != 0:
        raise AcceptanceBlocked(
            "native_cleanup_app_unavailable",
            f"macOS did not launch {name} for exact-title cleanup",
        )
    for _ in range(50):
        if _native_app_is_running(name):
            return True
        time.sleep(0.2)
    raise AcceptanceBlocked(
        "native_cleanup_app_unavailable",
        f"{name} did not become ready for exact-title cleanup",
    )


def _quit_native_app(name: str) -> None:
    subprocess.run(
        ["/usr/bin/osascript", "-e", f'tell application "{name}" to quit'],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )


def cleanup_test_items(
    title: str,
    run_id: str,
    *,
    event_calendar_identifier: str | None = None,
    include_reminder: bool = True,
) -> CleanupOutcome:
    """Delete exact synthetic items through their native macOS applications."""

    validate_acceptance_title(title, run_id)
    started_apps: list[str] = []
    calendar_script = """
on run argv
  set wanted to item 1 of argv
  set wantedCalendarIdentifier to item 2 of argv
  set targetRemoved to 0
  set otherRemoved to 0
  tell application "Calendar"
    repeat with targetCalendar in calendars
      set matches to (every event of targetCalendar whose summary is wanted)
      repeat with targetEvent in matches
        delete targetEvent
        if wantedCalendarIdentifier is not "" and calendarIdentifier of targetCalendar is wantedCalendarIdentifier then
          set targetRemoved to targetRemoved + 1
        else
          set otherRemoved to otherRemoved + 1
        end if
      end repeat
    end repeat
  end tell
  return (targetRemoved as text) & "," & (otherRemoved as text)
end run
"""
    reminders_script = """
on run argv
  set wanted to item 1 of argv
  set removed to 0
  tell application "Reminders"
    repeat with targetList in lists
      set matches to (every reminder of targetList whose name is wanted)
      repeat with targetReminder in matches
        delete targetReminder
        set removed to removed + 1
      end repeat
    end repeat
  end tell
  return removed
end run
"""
    try:
        app_names = ("Calendar", "Reminders") if include_reminder else ("Calendar",)
        for name in app_names:
            if _ensure_native_app_running(name):
                started_apps.append(name)
        errors: list[str] = []
        try:
            calendar_target_removed_count, calendar_other_removed_count = (
                _cleanup_calendar_items(
                    calendar_script,
                    title,
                    event_calendar_identifier,
                )
            )
            calendar_removed_count = (
                calendar_target_removed_count + calendar_other_removed_count
            )
        except Exception as exc:
            calendar_removed_count = 0
            calendar_target_removed_count = 0
            calendar_other_removed_count = 0
            errors.append(f"calendar_delete_{type(exc).__name__}")
        reminder_removed_count = 0
        if include_reminder:
            try:
                reminder_removed_count = _osascript(reminders_script, title)
            except Exception as exc:
                errors.append(f"reminder_delete_{type(exc).__name__}")
        return CleanupOutcome(
            calendar_removed_count=calendar_removed_count,
            calendar_target_removed_count=calendar_target_removed_count,
            calendar_other_removed_count=calendar_other_removed_count,
            reminder_removed_count=reminder_removed_count,
            errors=tuple(errors),
        )
    finally:
        for name in reversed(started_apps):
            _quit_native_app(name)


async def _query_exact_items(
    bridge: NativeBridgeClient,
    *,
    title: str,
    schedule: AcceptanceSchedule,
    include_reminder: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    start_ms = int((schedule.event_start - timedelta(minutes=2)).timestamp() * 1_000)
    end_ms = int((schedule.event_end + timedelta(minutes=2)).timestamp() * 1_000)
    events_response = await bridge.call(
        "calendar.query",
        {
            "start_unix_ms": start_ms,
            "end_unix_ms": end_ms,
            "calendar_identifiers": [],
            "limit": 500,
        },
    )
    events = [
        item for item in (events_response.result if isinstance(events_response.result, list) else [])
        if isinstance(item, dict) and item.get("title") == title
    ]
    reminders: list[dict[str, Any]] = []
    if include_reminder:
        reminders_response = await bridge.call(
            "reminders.query",
            {
                "calendar_identifiers": [],
                "include_completed": True,
                "limit": 500,
            },
        )
        reminders = [
            item
            for item in (
                reminders_response.result if isinstance(reminders_response.result, list) else []
            )
            if isinstance(item, dict) and item.get("title") == title
        ]
    return events, reminders


def _verify_native_readback(
    events: list[dict[str, Any]],
    reminders: list[dict[str, Any]],
    schedule: AcceptanceSchedule,
    zone: str,
    *,
    include_reminder: bool = True,
) -> dict[str, Any]:
    if len(events) != 1:
        raise AcceptanceFailed("calendar_query_mismatch", "native event query was not uniquely matched")
    if include_reminder and len(reminders) != 1:
        raise AcceptanceFailed("reminder_query_mismatch", "native reminder query was not uniquely matched")
    if not include_reminder and reminders:
        raise AcceptanceFailed("reminder_query_mismatch", "Calendar-only run found a reminder")
    event = events[0]
    event_start_ms = int(schedule.event_start.timestamp() * 1_000)
    event_end_ms = int(schedule.event_end.timestamp() * 1_000)
    if (
        event.get("start_unix_ms") != event_start_ms
        or event.get("end_unix_ms") != event_end_ms
        or event.get("time_zone_id") != zone
        or event.get("is_all_day") is not False
        or event.get("has_recurrence_rules") is not False
    ):
        raise AcceptanceFailed("calendar_readback_mismatch", "native event read-back differed")
    result: dict[str, Any] = {
        "calendar_event": {
            "expected_window_match_count": 1,
            "global_uniqueness_verified": False,
            "time_and_zone_matched": True,
            "nonrecurring": True,
        }
    }
    if not include_reminder:
        return result
    reminder = reminders[0]
    due_ms = int(schedule.reminder_due.timestamp() * 1_000)
    alarms = reminder.get("alarms")
    if not isinstance(alarms, list) or len(alarms) != 1:
        raise AcceptanceFailed("reminder_alarm_missing", "native reminder did not have one alarm")
    alarm = alarms[0]
    if (
        not isinstance(alarm, dict)
        or alarm.get("absolute_unix_ms") != due_ms
        or alarm.get("relative_offset_seconds") is not None
        or reminder.get("is_completed") is not False
        or reminder.get("has_recurrence_rules") is not False
    ):
        raise AcceptanceFailed("reminder_alarm_mismatch", "native absolute alarm read-back differed")
    result["reminder"] = {
        "bounded_query_match_count": 1,
        "global_uniqueness_verified": False,
        "nonrecurring": True,
        "absolute_alarm_matched": True,
        "alarm_due_unix_ms": due_ms,
    }
    return result


def _redacted_action_receipts(store: DurableStore) -> list[dict[str, Any]]:
    rows = store.execute(
        "SELECT a.action_id,a.kind,a.state,t.state,r.outcome,r.verification_level,"
        "lower(hex(r.evidence_digest)),x.identifier_value "
        "FROM actions a JOIN action_attempts t ON t.action_id=a.action_id "
        "JOIN action_receipts r ON r.attempt_id=t.attempt_id "
        "LEFT JOIN external_items e ON e.external_item_id=r.external_item_id "
        "LEFT JOIN external_identifiers x ON x.external_item_id=e.external_item_id "
        "ORDER BY a.created_at_us"
    ).fetchall()
    receipts: list[dict[str, Any]] = []
    for row in rows:
        native_identifier = str(row[7] or "")
        receipt: dict[str, Any] = redact_native_reference(
            action_kind=str(row[1]),
            action_id=str(row[0]),
            native_identifier=native_identifier,
            evidence_digest=str(row[6]),
        )
        receipt.update(
            action_state=str(row[2]),
            attempt_state=str(row[3]),
            outcome=str(row[4]),
            verification_level=str(row[5]),
        )
        receipts.append(receipt)
    return receipts


def _assert_durable_acceptance(
    store: DurableStore,
    *,
    include_reminder: bool = True,
) -> list[dict[str, Any]]:
    receipts = _redacted_action_receipts(store)
    expected = (
        {"calendar.create", "reminders.create"}
        if include_reminder
        else {"calendar.create"}
    )
    if len(receipts) != len(expected):
        raise AcceptanceFailed(
            "durable_receipt_count",
            f"expected exactly {len(expected)} durable receipt(s)",
        )
    if {item["action_kind"] for item in receipts} != expected:
        raise AcceptanceFailed("durable_receipt_kinds", "durable action kinds did not match")
    if any(
        item["action_state"] != "succeeded"
        or item["attempt_state"] != "succeeded"
        or item["outcome"] != "eventkit_saved_and_read_back"
        or item["verification_level"] != "read_back_matched"
        for item in receipts
    ):
        raise AcceptanceFailed("durable_receipt_state", "a durable receipt was not successful")
    payload_count = int(store.execute("SELECT count(*) FROM action_payloads").fetchone()[0])
    approved_count = int(
        store.execute(
            "SELECT count(*) FROM confirmation_challenges WHERE decision='approved'"
        ).fetchone()[0]
    )
    occurrence_count = int(store.execute("SELECT count(*) FROM task_occurrences").fetchone()[0])
    expected_occurrences = 1 if include_reminder else 0
    if (
        payload_count != 0
        or approved_count != len(expected)
        or occurrence_count != expected_occurrences
    ):
        raise AcceptanceFailed(
            "durable_evidence_incomplete",
            "payload purge, confirmation, or task occurrence evidence was incomplete",
        )
    return receipts


async def _wait_for_alarm(
    path: Path,
    *,
    offset: int,
    feed_identity: tuple[int, int],
    title: str,
    due: datetime,
    timeout_s: float,
) -> AlarmDeliveryEvidence:
    deadline = max(time.monotonic(), time.monotonic() + (due.timestamp() - time.time())) + timeout_s
    while time.monotonic() < deadline:
        evidence = await asyncio.to_thread(
            alarm_delivery_after,
            path,
            offset=offset,
            exact_title=title,
            not_before_unix_ms=int(due.timestamp() * 1_000) - 2_000,
            now_unix_ms=int(time.time() * 1_000),
            expected_feed_identity=feed_identity,
        )
        if evidence is not None:
            return evidence
        await asyncio.sleep(0.5)
    raise AcceptanceBlocked(
        "reminder_alarm_not_observed",
        "the native alarm was present but no matching Reminders banner was observed",
    )


async def _wait_for_cleanup_absence(
    bridge: NativeBridgeClient,
    *,
    title: str,
    schedule: AcceptanceSchedule,
    include_reminder: bool = True,
    timeout_s: float = 12.0,
) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        events, reminders = await _query_exact_items(
            bridge,
            title=title,
            schedule=schedule,
            include_reminder=include_reminder,
        )
        if not events and not reminders:
            return True
        await asyncio.sleep(0.5)
    return False


async def _drive_confirmed_command(
    controller: ProductivityActionController,
    command: str,
    *,
    automation_base: str,
    screenshot_path: Path,
    expected_result_prefix: str,
) -> dict[str, str]:
    task = asyncio.create_task(controller.handle_command(command))
    screenshot_digest, preview_digest = await _wait_for_confirmation_and_click(
        automation_base,
        screenshot_path,
    )
    result = await asyncio.wait_for(task, timeout=20)
    if not isinstance(result, str) or not result.startswith(expected_result_prefix):
        raise AcceptanceFailed("action_result", "production controller did not report success")
    return {
        "confirmation_screenshot_digest": screenshot_digest,
        "confirmation_preview_digest": preview_digest,
        "decision": "accepted",
    }


async def run(args: argparse.Namespace) -> tuple[int, Path]:
    try:
        zone = ZoneInfo(args.zone)
    except ZoneInfoNotFoundError as exc:
        raise AcceptanceSafetyError("unknown IANA time zone") from exc
    run_id = uuid.uuid4().hex[:8]
    title = f"{TITLE_PREFIX} {run_id}"
    validate_acceptance_title(title, run_id)
    include_reminder = not args.calendar_only
    schedule: AcceptanceSchedule | None = None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    evidence_dir = args.evidence_root / f"{stamp}-{run_id}"
    evidence_dir.mkdir(parents=True, mode=0o700)
    os.chmod(evidence_dir, 0o700)
    receipt_path = evidence_dir / "receipt.json"
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "run_ref": _digest(run_id)[:12],
        "started_at_unix_ms": int(time.time() * 1_000),
        "result": "running",
        "scenario": "calendar_and_reminder" if include_reminder else "calendar_only",
        "permissions": {},
        "mac_app_isolation": {
            "gateway_port": args.ws_port,
            "inert_engine_stub": True,
            "repo_preference_restored": False,
        },
        "simulator": {},
        "confirmations": [],
        "native_readback": {},
        "alarm_delivery": {"observed": False},
        "durable_actions": [],
        "cleanup": {
            "calendar_removed_count": 0,
            "calendar_target_removed_count": 0,
            "calendar_other_removed_count": 0,
            "reminder_removed_count": 0,
            "native_absence_verified": False,
        },
        "blockers": [],
    }
    write_private_receipt(receipt_path, receipt)

    bridge = NativeBridgeClient(args.bridge)
    store: DurableStore | None = None
    gateway: HubGatewayTransport | None = None
    managed: list[ManagedProcess] = []
    temp_context = tempfile.TemporaryDirectory(prefix="joat-productivity-acceptance-")
    temp_dir = Path(temp_context.name)
    diagnostics_path = evidence_dir / "g2-diagnostic.json"
    app_lease = MacAppNativeBridgeLease(
        app_path=args.app,
        bundle_id=args.app_bundle_id,
        bridge_path=args.bridge,
        gateway_port=args.ws_port,
        stub_repo=create_engine_stub_repo(evidence_dir),
        recovery_guard=args.recovery_guard,
    )
    wrote_native = False
    native_readback_verified = False
    event_calendar_identifier: str | None = None
    exit_code = 1
    try:
        simulator_ws_url = resolve_simulator_ws_url(
            args.repo / "apps/hub-plugin/app.json",
            gateway_port=args.ws_port,
        )
        await app_lease.enter()
        # App shutdown time must never consume the alarm lead window.
        schedule = plan_acceptance_schedule(
            datetime.now(zone),
            minimum_lead_seconds=args.minimum_lead_seconds,
        )
        async with bridge:
            authorization = await bridge.call("authorization.status")
            receipt["permissions"] = {
                "calendar": authorization.authorization.calendar,
                "reminders": authorization.authorization.reminders,
                "request_surface": "JOAT.app UI only",
                "request_performed_this_run": False,
                "full_access_verified_this_run": True,
            }
            if authorization.authorization.calendar != "fullAccess" or (
                include_reminder
                and authorization.authorization.reminders != "fullAccess"
            ):
                raise AcceptanceBlocked(
                    "tcc_full_access_required",
                    (
                        "use the Calendar and Reminders Connect buttons in JOAT.app, then rerun"
                        if include_reminder
                        else "use the Calendar Connect button in JOAT.app, then rerun"
                    ),
                )
            notification_offset = 0
            notification_feed_identity = (0, 0)
            if include_reminder:
                secure_notification_feed(args.notification_feed)
                try:
                    heartbeat_age = time.time() - args.notification_heartbeat.stat().st_mtime
                except OSError as exc:
                    raise AcceptanceBlocked(
                        "notification_watcher_unavailable",
                        "the JOAT notification watcher heartbeat is missing",
                    ) from exc
                if heartbeat_age > 5:
                    raise AcceptanceBlocked(
                        "notification_watcher_stale",
                        "the JOAT notification watcher heartbeat is stale",
                    )
                notification_stat = args.notification_feed.stat()
                notification_offset = notification_stat.st_size
                notification_feed_identity = (
                    notification_stat.st_dev,
                    notification_stat.st_ino,
                )

            store = DurableStore(temp_dir / "actions.sqlite3")
            journal = ActionJournal(store)
            gateway = HubGatewayTransport(
                host="127.0.0.1",
                port=args.ws_port,
                pairing=PairingStore(path=temp_dir / "pairing.json"),
                g2_diagnostics=G2DiagnosticStore(path=diagnostics_path),
            )
            connected = asyncio.Event()
            gateway.on_hello(lambda _hello: connected.set())

            async def publish_status(status_title: str, message: str) -> None:
                await gateway.send(
                    HudList(
                        items=[message],
                        title=status_title,
                        hint="acceptance · synthetic data",
                    )
                )

            controller = ProductivityActionController(
                bridge,
                gateway.request_confirmation,
                journal,
                TaskOccurrenceStore(store),
                publish_status=publish_status,
            )
            await gateway.start()

            env = dict(os.environ)
            env["VITE_MAC_WS_URL"] = simulator_ws_url
            vite = _start_process(
                "vite",
                [
                    "npm",
                    "run",
                    "dev",
                    "--",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(args.vite_port),
                    "--strictPort",
                ],
                cwd=args.repo / "apps/hub-plugin",
                env=env,
            )
            managed.append(vite)
            plugin_url = f"http://127.0.0.1:{args.vite_port}"
            await _wait_http(plugin_url, timeout_s=20, process=vite)

            simulator_binary = Path.home() / ".local/bin/evenhub-simulator"
            if not simulator_binary.is_file():
                raise AcceptanceBlocked("simulator_missing", "evenhub-simulator is not installed")
            simulator = _start_process(
                "evenhub_simulator",
                [
                    str(simulator_binary),
                    plugin_url,
                    "--automation-port",
                    str(args.automation_port),
                ],
                cwd=args.repo,
                env=env,
            )
            managed.append(simulator)
            automation_base = f"http://127.0.0.1:{args.automation_port}"
            await _wait_http(
                f"{automation_base}/api/ping",
                timeout_s=20,
                process=simulator,
            )
            try:
                await asyncio.wait_for(connected.wait(), timeout=20)
            except TimeoutError as exc:
                raise AcceptanceBlocked(
                    "simulator_plugin_not_connected",
                    "the official simulator did not connect through the packaged WebSocket allowlist",
                ) from exc
            receipt["simulator"] = {
                "official_host": True,
                "allowlisted_local_websocket": True,
                "raw_framebuffer_retained": True,
                "reviewable_alpha_previews_retained": True,
            }
            await gateway.send(AudioControl(open=False))
            await asyncio.sleep(2)

            event_command = (
                f"schedule {title} on {schedule.event_start:%Y-%m-%d} "
                f"at {schedule.event_start:%H:%M} for 1 minute in {args.zone}"
            )
            # Arm exact-title cleanup before the controller can cross the
            # EventKit boundary. An uncertain post-commit result must not
            # strand a synthetic item merely because success was not reported.
            wrote_native = True
            event_confirmation = await _drive_confirmed_command(
                controller,
                event_command,
                automation_base=automation_base,
                screenshot_path=evidence_dir / "event-confirmation.png",
                expected_result_prefix="Created Calendar event:",
            )
            event_confirmation["action_kind"] = "calendar.create"
            event_receipt_digest, event_receipt_preview_digest = await _capture_screenshot(
                automation_base,
                evidence_dir / "event-receipt.png",
            )
            event_confirmation["receipt_screenshot_digest"] = event_receipt_digest
            event_confirmation["receipt_preview_digest"] = event_receipt_preview_digest
            receipt["confirmations"].append(event_confirmation)

            if include_reminder:
                reminder_command = (
                    f"remind me to {title} on {schedule.reminder_due:%Y-%m-%d} "
                    f"at {schedule.reminder_due:%H:%M} in {args.zone}"
                )
                reminder_confirmation = await _drive_confirmed_command(
                    controller,
                    reminder_command,
                    automation_base=automation_base,
                    screenshot_path=evidence_dir / "reminder-confirmation.png",
                    expected_result_prefix="Created Reminder:",
                )
                wrote_native = True
                reminder_confirmation["action_kind"] = "reminders.create"
                reminder_receipt_digest, reminder_receipt_preview_digest = (
                    await _capture_screenshot(
                        automation_base,
                        evidence_dir / "reminder-receipt.png",
                    )
                )
                reminder_confirmation["receipt_screenshot_digest"] = (
                    reminder_receipt_digest
                )
                reminder_confirmation["receipt_preview_digest"] = (
                    reminder_receipt_preview_digest
                )
                receipt["confirmations"].append(reminder_confirmation)

            events, reminders = await _query_exact_items(
                bridge,
                title=title,
                schedule=schedule,
                include_reminder=include_reminder,
            )
            candidate_calendar_identifier = events[0].get("calendar_identifier") if events else None
            if not isinstance(candidate_calendar_identifier, str) or not candidate_calendar_identifier:
                raise AcceptanceFailed(
                    "calendar_identifier_missing",
                    "native event read-back omitted its target calendar identity",
                )
            event_calendar_identifier = candidate_calendar_identifier
            receipt["native_readback"] = _verify_native_readback(
                events,
                reminders,
                schedule,
                args.zone,
                include_reminder=include_reminder,
            )
            native_readback_verified = True
            receipt["durable_actions"] = _assert_durable_acceptance(
                store,
                include_reminder=include_reminder,
            )

            if include_reminder:
                alarm = await _wait_for_alarm(
                    args.notification_feed,
                    offset=notification_offset,
                    feed_identity=notification_feed_identity,
                    title=title,
                    due=schedule.reminder_due,
                    timeout_s=args.alarm_timeout_seconds,
                )
                receipt["alarm_delivery"] = {"observed": True, **asdict(alarm)}
            else:
                receipt["alarm_delivery"] = {
                    "observed": False,
                    "not_applicable": True,
                }
            receipt["result"] = "pending_cleanup"
            write_private_receipt(receipt_path, receipt)
            exit_code = 0
    except AcceptanceBlocked as exc:
        receipt["result"] = "blocked"
        receipt["blockers"].append({"code": exc.code, "message": str(exc)})
        exit_code = 2
    except (AcceptanceFailed, AcceptanceSafetyError) as exc:
        receipt["result"] = "failed"
        receipt["blockers"].append(
            {"code": getattr(exc, "code", "safety_failure"), "message": str(exc)}
        )
        exit_code = 1
    except Exception as exc:
        receipt["result"] = "failed"
        receipt["blockers"].append(
            {"code": "unexpected_failure", "message": type(exc).__name__}
        )
        log.exception("acceptance failed unexpectedly")
        exit_code = 1
    finally:
        cleanup_error: str | None = None
        if wrote_native:
            if schedule is None:
                cleanup_error = "native_cleanup_schedule_missing"
            else:
                try:
                    cleanup = await asyncio.to_thread(
                        cleanup_test_items,
                        title,
                        run_id,
                        event_calendar_identifier=event_calendar_identifier,
                        include_reminder=include_reminder,
                    )
                    receipt["cleanup"].update(
                        calendar_removed_count=cleanup.calendar_removed_count,
                        calendar_target_removed_count=(
                            cleanup.calendar_target_removed_count
                        ),
                        calendar_other_removed_count=cleanup.calendar_other_removed_count,
                        reminder_removed_count=cleanup.reminder_removed_count,
                    )
                    if cleanup.errors:
                        cleanup_error = "_and_".join(cleanup.errors)
                    elif native_readback_verified and (
                        cleanup.calendar_target_removed_count != 1
                        or cleanup.calendar_other_removed_count
                        not in ({0, 1} if include_reminder else {0})
                        or cleanup.reminder_removed_count != (1 if include_reminder else 0)
                    ):
                        cleanup_error = "cleanup_cardinality_mismatch"
                except Exception as exc:
                    cleanup_error = f"cleanup_{type(exc).__name__}"
                try:
                    async with NativeBridgeClient(args.bridge) as cleanup_bridge:
                        receipt["cleanup"]["native_absence_verified"] = (
                            await _wait_for_cleanup_absence(
                                cleanup_bridge,
                                title=title,
                                schedule=schedule,
                                include_reminder=include_reminder,
                            )
                        )
                except Exception as exc:
                    absence_error = f"cleanup_absence_{type(exc).__name__}"
                    cleanup_error = (
                        f"{cleanup_error}_and_{absence_error}"
                        if cleanup_error is not None
                        else absence_error
                    )
                if not receipt["cleanup"]["native_absence_verified"]:
                    cleanup_error = cleanup_error or "native_cleanup_absence_not_verified"
        for process in reversed(managed):
            process.stop()
        if gateway is not None:
            await gateway.stop()
        if store is not None:
            store.close()

        if diagnostics_path.exists():
            receipt["simulator"]["g2_diagnostic_receipt_digest"] = _digest(
                diagnostics_path.read_bytes()
            )

        restore_error: str | None = None
        try:
            await app_lease.restore()
            receipt["mac_app_isolation"]["repo_preference_restored"] = app_lease.restored
        except Exception as exc:
            restore_error = f"mac_app_restore_{type(exc).__name__}"
            log.exception("failed to restore normal JOAT.app engine operation")
        if app_lease.restored or not app_lease.preference_changed:
            if (
                app_lease.stub_repo.name == "native-bridge-only-repo"
                and app_lease.stub_repo.parent == evidence_dir
            ):
                shutil.rmtree(app_lease.stub_repo, ignore_errors=True)
            else:
                restore_error = "mac_app_stub_cleanup_scope_invalid"
        temp_context.cleanup()

        if cleanup_error is not None:
            receipt["blockers"].append(
                {"code": cleanup_error, "message": "synthetic EventKit cleanup was incomplete"}
            )
            receipt["result"] = "blocked"
            exit_code = 2
        if restore_error is not None:
            receipt["blockers"].append(
                {"code": restore_error, "message": "normal JOAT.app operation was not restored"}
            )
            receipt["result"] = "blocked"
            exit_code = 2
        elif exit_code == 0:
            receipt["result"] = "passed"
        receipt["completed_at_unix_ms"] = int(time.time() * 1_000)
        write_private_receipt(receipt_path, receipt)
    return exit_code, receipt_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--notification-feed", type=Path, default=DEFAULT_NOTIFICATION_FEED)
    parser.add_argument(
        "--notification-heartbeat",
        type=Path,
        default=DEFAULT_NOTIFICATION_HEARTBEAT,
    )
    parser.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    parser.add_argument("--app", type=Path, default=DEFAULT_APP)
    parser.add_argument("--app-bundle-id", default=DEFAULT_APP_BUNDLE_ID)
    parser.add_argument("--recovery-guard", type=Path, default=DEFAULT_RECOVERY_GUARD)
    parser.add_argument("--zone", default="America/Chicago")
    parser.add_argument("--ws-port", type=int, default=8787)
    parser.add_argument("--vite-port", type=int, default=5194)
    parser.add_argument("--automation-port", type=int, default=9902)
    parser.add_argument("--minimum-lead-seconds", type=int, default=120)
    parser.add_argument("--alarm-timeout-seconds", type=float, default=120)
    parser.add_argument(
        "--calendar-only",
        action="store_true",
        help="exercise Calendar create/read-back/cleanup without creating a Reminder",
    )
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = _parser().parse_args()
    exit_code, receipt_path = asyncio.run(run(args))
    print(f"acceptance_result={exit_code} receipt={receipt_path}")
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
