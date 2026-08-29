"""Opt-in, in-engine physical G2 acceptance for Brainstorm / Agent Terminal.

The acceptance controller deliberately runs *inside* JOAT's app-launched
Python engine.  That is important: the native bridge rightly admits only the
exact child PID launched by the signed macOS shell, so an external script must
never be granted its OpenClaw identity just to run a field test.

The controller sends one fixed no-tool Hermes prompt after a physical G2
gesture and discards every response chunk.  Its owner-only receipt contains
only allowlisted G2 diagnostic metadata and visual-frame hashes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..ble.hub_gateway import HubGatewayTransport
from ..core.protocol import BrainstormAction, BrainstormHudView, Hello
from ..services.g2_diagnostics import TRACE_SCHEMA


RUN_SCHEMA = "joat.g2-brainstorm-acceptance.v2"
MAX_RETAINED_EVENTS = 12
_HEX_SHA256_LENGTH = 64
_PHYSICAL_SOURCES = frozenset({"left", "right", "ring"})
_TERMINAL_TOGGLE_SOURCES = frozenset({"right", "ring"})
_BRAINSTORM_ACTIONS = frozenset({
    "begin_topic_capture", "show_terminal", "show_brainstorm", "retry_terminal", "cancel",
})
_HUD_STATES = frozenset({
    "unavailable", "approval_required", "ready", "running", "settled", "cancelled", "failed", "blocked",
})
_HUD_SURFACES = frozenset({"brainstorm", "agent_terminal"})
_HERMES_RUN_STATES = frozenset({"not_exercised", "cancelled", "completed", "failed"})
_G2_RECONNECT_STATES = frozenset({"not_exercised", "restored"})

# It is intentionally never copied into the G2 view, receipt, status file,
# console, or log.  The response is long only so the wearer has time to
# exercise the cancellation gesture while a genuine no-tool run is active.
_FIXED_HERMES_PROMPT = (
    "JOAT physical acceptance run. Emit a long sequence of neutral status "
    "markers only. Do not use tools or call any external service."
)


class HermesStream(Protocol):
    def stream(self, prompt: str) -> AsyncIterator[str]: ...


@dataclass(frozen=True, slots=True)
class AcceptanceResult:
    """Bounded completion projection suitable for the owner-only receipt."""

    result: str
    hermes_run: str
    g2_reconnect: str


def _require_object(value: object, message: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(message)
    return value


def _require_events(receipt: dict[str, object]) -> list[dict[str, object]]:
    if receipt.get("schema") != TRACE_SCHEMA:
        raise ValueError("receipt is not an opt-in JOAT G2 diagnostic trace")
    raw_events = receipt.get("events")
    if not isinstance(raw_events, list):
        raise ValueError("receipt events must be a list")
    return [_require_object(event, "receipt event must be an object") for event in raw_events]


def _sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _HEX_SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256")
    return value


def _frame_event(event: dict[str, object]) -> dict[str, str] | None:
    if event.get("type") != "visual_frame":
        return None
    expected = {"sequence", "recordedAtMs", "type", "kind", "surface", "state", "sha256"}
    if set(event) != expected:
        raise ValueError("visual_frame contains unsupported fields")
    kind, surface, state = event.get("kind"), event.get("surface"), event.get("state")
    if kind != "brainstorm" or surface not in _HUD_SURFACES or state not in _HUD_STATES:
        raise ValueError("visual_frame contains an invalid fixed value")
    return {
        "type": "visual_frame", "kind": "brainstorm", "surface": str(surface),
        "state": str(state), "sha256": _sha256(event.get("sha256"), "sha256"),
    }


def _action_event(event: dict[str, object]) -> dict[str, str] | None:
    if event.get("type") != "brainstorm_action":
        return None
    expected = {"sequence", "recordedAtMs", "type", "action", "surface", "gesture", "delivery", "source"}
    if set(event) != expected:
        raise ValueError("brainstorm_action contains unsupported fields")
    action, surface = event.get("action"), event.get("surface")
    gesture, delivery, source = event.get("gesture"), event.get("delivery"), event.get("source")
    if action not in _BRAINSTORM_ACTIONS:
        raise ValueError("brainstorm_action is invalid")
    if surface not in _HUD_SURFACES:
        raise ValueError("brainstorm_action surface is invalid")
    if gesture not in {"tap", "double_press"} or delivery != "sys" or source not in _PHYSICAL_SOURCES:
        raise ValueError("brainstorm_action is not a physical G2 input")
    return {
        "type": "brainstorm_action", "action": str(action), "surface": str(surface),
        "gesture": str(gesture), "delivery": "sys", "source": str(source),
    }


def retained_events(receipt: dict[str, object]) -> list[dict[str, str]]:
    """Remove timestamps, tile events, and every non-acceptance field."""

    retained: list[dict[str, str]] = []
    for event in _require_events(receipt):
        simplified = _frame_event(event) or _action_event(event)
        if simplified is not None:
            retained.append(simplified)
    if len(retained) > MAX_RETAINED_EVENTS:
        raise ValueError("acceptance run retained too many Brainstorm trace events")
    return retained


def _find_after(
    events: list[dict[str, str]], start: int, predicate: Callable[[dict[str, str]], bool],
) -> tuple[int, dict[str, str]] | None:
    for index in range(start + 1, len(events)):
        if predicate(events[index]):
            return index, events[index]
    return None


def _summary(
    result: str, retained: list[dict[str, str]], lens_video_sha256: str | None,
    hermes_run: str, g2_reconnect: str,
) -> dict[str, object]:
    return {
        "schema": RUN_SCHEMA,
        "mode": "physical_g2_app_engine",
        "result": result,
        "retainedEventCount": len(retained),
        "events": retained,
        "wearerVisibleHud": {
            "bridge": "verified" if result == "pass" else "not_verified",
            "lens": "video_digest_recorded" if lens_video_sha256 else "not_recorded",
        },
        "lensVideoSha256": lens_video_sha256,
        "hermesRun": hermes_run,
        "g2Reconnect": g2_reconnect,
    }


def summarize_receipt(
    receipt: dict[str, object], *, lens_video_sha256: str | None = None,
    hermes_run: str = "not_exercised", g2_reconnect: str = "not_exercised",
    require_cancel: bool = False, require_reconnect: bool = False,
) -> dict[str, object]:
    """Fail closed unless the full physical G2 flow is represented."""

    if lens_video_sha256 is not None:
        _sha256(lens_video_sha256, "lensVideoSha256")
    if hermes_run not in _HERMES_RUN_STATES:
        raise ValueError("hermes run state is invalid")
    if g2_reconnect not in _G2_RECONNECT_STATES:
        raise ValueError("G2 reconnect state is invalid")
    retained = retained_events(receipt)
    initial = next(((index, event) for index, event in enumerate(retained) if (
        event.get("type") == "visual_frame" and event["kind"] == "brainstorm"
        and event["surface"] == "brainstorm" and event["state"] == "running"
    )), None)
    if initial is None:
        return _summary("inconclusive", retained, lens_video_sha256, hermes_run, g2_reconnect)
    terminal_action = _find_after(retained, initial[0], lambda event: (
        event.get("type") == "brainstorm_action" and event.get("action") == "show_terminal"
        and event.get("surface") == "brainstorm" and event.get("gesture") == "tap"
        and event.get("delivery") == "sys" and event.get("source") in _TERMINAL_TOGGLE_SOURCES
    ))
    if terminal_action is None:
        return _summary("inconclusive", retained, lens_video_sha256, hermes_run, g2_reconnect)
    terminal_frame = _find_after(retained, terminal_action[0], lambda event: (
        event.get("type") == "visual_frame" and event.get("kind") == "brainstorm"
        and event.get("surface") == "agent_terminal" and event.get("state") == "running"
    ))
    if terminal_frame is None or terminal_frame[1]["sha256"] == initial[1]["sha256"]:
        return _summary("fail", retained, lens_video_sha256, hermes_run, g2_reconnect)
    return_action = _find_after(retained, terminal_frame[0], lambda event: (
        event.get("type") == "brainstorm_action" and event.get("action") == "show_brainstorm"
        and event.get("surface") == "agent_terminal" and event.get("gesture") == "tap"
        and event.get("delivery") == "sys" and event.get("source") in _TERMINAL_TOGGLE_SOURCES
    ))
    if return_action is None:
        return _summary("inconclusive", retained, lens_video_sha256, hermes_run, g2_reconnect)
    returned = _find_after(retained, return_action[0], lambda event: (
        event.get("type") == "visual_frame" and event.get("kind") == "brainstorm"
        and event.get("surface") == "brainstorm" and event.get("state") == "running"
    ))
    if returned is None or returned[1]["sha256"] == terminal_frame[1]["sha256"]:
        return _summary("fail", retained, lens_video_sha256, hermes_run, g2_reconnect)
    if require_reconnect and g2_reconnect != "restored":
        return _summary("inconclusive", retained, lens_video_sha256, hermes_run, g2_reconnect)
    if not require_cancel:
        return _summary("pass", retained, lens_video_sha256, hermes_run, g2_reconnect)
    cancelled = _find_after(retained, returned[0], lambda event: (
        event.get("type") == "brainstorm_action" and event.get("action") == "cancel"
        and event.get("surface") == "brainstorm" and event.get("gesture") == "double_press"
        and event.get("delivery") == "sys" and event.get("source") == "left"
    ))
    if cancelled is None:
        return _summary("inconclusive", retained, lens_video_sha256, hermes_run, g2_reconnect)
    cancelled_frame = _find_after(retained, cancelled[0], lambda event: (
        event.get("type") == "visual_frame" and event.get("kind") == "brainstorm"
        and event.get("surface") == "brainstorm" and event.get("state") == "cancelled"
    ))
    if cancelled_frame is None:
        return _summary("fail", retained, lens_video_sha256, hermes_run, g2_reconnect)
    if hermes_run != "cancelled":
        return _summary("inconclusive", retained, lens_video_sha256, hermes_run, g2_reconnect)
    return _summary("pass", retained, lens_video_sha256, hermes_run, g2_reconnect)


def write_owner_only_json(path: Path, payload: dict[str, object]) -> None:
    """Atomic owner-only output.  It never copies a lens recording."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _view(surface: str, *, state: str = "running", activity: str) -> BrainstormHudView:
    if surface == "brainstorm":
        return BrainstormHudView(
            screen="brainstorm", state=state, topic="Acceptance run", collaborators=["Hermes"],
            activity=activity, terminal_state="ready" if state != "cancelled" else "cancelled",
            no_tools_checked=True,
        )
    return BrainstormHudView(
        screen="agent_terminal", state=state, topic="Hermes Agent Terminal", collaborators=[],
        activity=activity, terminal_state="running", no_tools_checked=True,
    )


class InAppBrainstormAcceptance:
    """One-shot physical G2 workflow driven by the production engine objects."""

    def __init__(
        self, *, transport: HubGatewayTransport, terminal: HermesStream | None,
        out: Path, action_timeout_s: float = 45.0, connect_timeout_s: float = 120.0,
        diagnostic_events: list[dict[str, object]] | None = None,
        on_complete: Callable[[], None] | None = None,
    ) -> None:
        self._transport = transport
        self._terminal = terminal
        self._out = out
        self._action_timeout_s = action_timeout_s
        self._connect_timeout_s = connect_timeout_s
        self._events = diagnostic_events if diagnostic_events is not None else []
        self._on_complete = on_complete
        self._actions: asyncio.Queue[str] = asyncio.Queue()
        self._hello_count = 0
        self._physical_hello = asyncio.Event()
        self._replacement_hello = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stream_task: asyncio.Task[None] | None = None
        self._first_chunk = asyncio.Event()

    @property
    def output_path(self) -> Path:
        return self._out

    def observe_diagnostics(self, _path: Path, events: list[dict[str, Any]]) -> None:
        """G2DiagnosticStore callback; receives only protocol-sanitized events."""

        self._events[:] = [dict(event) for event in events]

    def attach(self) -> None:
        if self._out.exists():
            raise FileExistsError("refusing to overwrite an acceptance receipt")
        self._transport.on_hello(self._on_hello)
        self._transport.on_brainstorm_action(self._on_action)

    async def close(self) -> None:
        """Finish a stopped app run without leaving an ambiguous active task."""

        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        elif not self._out.exists():
            write_owner_only_json(
                self._out,
                summarize_receipt(
                    {"schema": TRACE_SCHEMA, "events": self._events},
                    require_cancel=True,
                    require_reconnect=True,
                ),
            )

    def _on_hello(self, hello: Hello) -> None:
        if hello.model != "g2":
            return
        self._hello_count += 1
        self._physical_hello.set()
        if self._hello_count > 1:
            self._replacement_hello.set()
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    def _on_action(self, action: BrainstormAction) -> None:
        self._actions.put_nowait(action.action)

    async def _wait_for(self, predicate: Callable[[], bool], timeout_s: float, message: str) -> None:
        deadline = time.monotonic() + timeout_s
        while not predicate():
            if time.monotonic() >= deadline:
                raise TimeoutError(message)
            await asyncio.sleep(0.05)

    async def _send(self, surface: str, *, state: str = "running", activity: str) -> None:
        await self._transport.send(_view(surface, state=state, activity=activity))
        await self._wait_for(
            lambda: any(
                event.get("type") == "visual_frame" and event.get("kind") == "brainstorm"
                and event.get("surface") == surface and event.get("state") == state
                for event in self._events
            ),
            self._action_timeout_s,
            f"{surface} {state} visual frame was not acknowledged",
        )

    async def _expect(self, action: str) -> None:
        observed = await asyncio.wait_for(self._actions.get(), self._action_timeout_s)
        if observed != action:
            raise RuntimeError(f"expected {action} action")
        await self._wait_for(
            lambda: any(event.get("type") == "brainstorm_action" and event.get("action") == action for event in self._events),
            self._action_timeout_s,
            f"{action} action did not retain a G2 receipt",
        )

    async def _consume_hermes(self, terminal: HermesStream) -> None:
        async for _ignored in terminal.stream(_FIXED_HERMES_PROMPT):
            self._first_chunk.set()

    async def _cancel_hermes(self) -> str:
        task = self._stream_task
        if task is None:
            return "failed"
        if task.done():
            try:
                task.result()
            except asyncio.CancelledError:
                return "cancelled"
            except Exception:
                return "failed"
            return "completed"
        task.cancel()
        results = await asyncio.gather(task, return_exceptions=True)
        return "cancelled" if isinstance(results[0], asyncio.CancelledError) else "failed"

    async def _run(self) -> None:
        hermes_run = "not_exercised"
        g2_reconnect = "not_exercised"
        try:
            await asyncio.wait_for(self._physical_hello.wait(), self._connect_timeout_s)
            if self._terminal is None:
                await self._send("agent_terminal", state="blocked", activity="Hermes is not ready; acceptance stopped")
                return
            await self._send("brainstorm", state="ready", activity="Tap to start the Hermes acceptance run")
            await self._expect("begin_topic_capture")
            self._stream_task = asyncio.create_task(self._consume_hermes(self._terminal))
            await asyncio.wait_for(self._first_chunk.wait(), self._action_timeout_s)
            await self._send("brainstorm", activity="Hermes acceptance run active")
            await self._expect("show_terminal")
            await self._send("agent_terminal", activity="Dedicated no-tool Hermes session active")
            await self._expect("show_brainstorm")
            await self._send("brainstorm", activity="Hermes acceptance run active")
            await self._send("brainstorm", activity="Reconnect G2 to restore this Hermes run")
            await asyncio.wait_for(self._replacement_hello.wait(), self._connect_timeout_s)
            await self._send("brainstorm", activity="Hermes run restored after G2 reconnect")
            g2_reconnect = "restored"
            await self._expect("cancel")
            hermes_run = await self._cancel_hermes()
            await self._send("brainstorm", state="cancelled", activity="Hermes acceptance cancelled")
        finally:
            if self._stream_task is not None and not self._stream_task.done():
                self._stream_task.cancel()
                await asyncio.gather(self._stream_task, return_exceptions=True)
            receipt: dict[str, object] = {"schema": TRACE_SCHEMA, "events": self._events}
            try:
                summary = summarize_receipt(
                    receipt, hermes_run=hermes_run, g2_reconnect=g2_reconnect,
                    require_cancel=True, require_reconnect=True,
                )
            except ValueError:
                # A malformed diagnostic trace cannot be made into a receipt.
                summary = _summary("fail", [], None, hermes_run, g2_reconnect)
            write_owner_only_json(self._out, summary)
            if self._on_complete is not None:
                self._on_complete()
