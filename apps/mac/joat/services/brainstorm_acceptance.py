"""Opt-in, in-engine physical G2 acceptance for Brainstorm / Agent Terminal.

The acceptance controller deliberately runs *inside* JOAT's app-launched
Python engine.  That is important: the native bridge rightly admits only the
exact child PID launched by the signed macOS shell, so an external script must
never be granted its OpenClaw identity just to run a field test.

The controller sends one fixed no-tool Hermes prompt after a physical G2
gesture and routes a bounded pair of response deltas through the production
HUD sink.  Its owner-only receipt contains only allowlisted G2 diagnostic
metadata and visual-frame hashes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from ..ble.hub_gateway import HubGatewayTransport
from ..core.protocol import BrainstormAction, BrainstormHudView, Hello
from ..services.g2_diagnostics import G2DiagnosticStore, TRACE_SCHEMA
from ..services.hud import HudWriter
from ..services.session import Session
from ..services.sinks import HudSink


RUN_SCHEMA = "joat.g2-brainstorm-acceptance.v2"
MAX_RETAINED_EVENTS = 16
_HEX_SHA256_LENGTH = 64
_STREAM_FRAME_TARGET = 2
_MAX_STREAM_FRAME_ATTEMPTS = 4
_PHYSICAL_SOURCES = frozenset({"left", "right", "ring"})
_TERMINAL_TOGGLE_SOURCES = frozenset({"right", "ring"})
_BRAINSTORM_ACTIONS = frozenset({
    "begin_acceptance", "begin_topic_capture", "show_terminal", "show_brainstorm",
    "retry_terminal", "cancel",
})
_HUD_STATES = frozenset({
    "unavailable", "approval_required", "ready", "acceptance_ready", "running",
    "settled", "cancelled", "failed", "blocked",
})
_HUD_SURFACES = frozenset({"brainstorm", "agent_terminal"})
_HERMES_RUN_STATES = frozenset({"not_exercised", "cancelled", "completed", "failed"})
_G2_RECONNECT_STATES = frozenset({"not_exercised", "restored"})
EvidenceMode = Literal["physical_g2", "simulator"]

# It is intentionally never copied into the G2 view, receipt, status file,
# console, or log.  The response is long only so the wearer has time to
# exercise the cancellation gesture while a genuine no-tool run is active.
_FIXED_HERMES_PROMPT = (
    "JOAT physical acceptance run. Emit a long sequence of neutral status "
    "markers only. Do not use tools or call any external service."
)


def default_output_directory() -> Path:
    return Path.home() / "Library/Application Support/JOAT/acceptance/brainstorm"


def _next_output_path(directory: Path, mode: EvidenceMode) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    suffix = "physical-g2" if mode == "physical_g2" else "simulator"
    return directory / f"{stamp}-{uuid.uuid4().hex[:8]}-{suffix}.json"


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
    hermes_run: str, g2_reconnect: str, stream_frame_sha256: tuple[str, ...] = (),
    stream_verified: bool = False,
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
        "hermesStream": {
            "hud": "verified" if stream_verified else "not_verified",
            "frameSha256": list(stream_frame_sha256),
        },
        "g2Reconnect": g2_reconnect,
    }


def _ordered_stream_frames(
    retained: list[dict[str, str]], frame_sha256: tuple[str, ...],
) -> bool:
    if (
        len(frame_sha256) != _STREAM_FRAME_TARGET
        or len(set(frame_sha256)) != len(frame_sha256)
    ):
        return False
    cursor = -1
    for digest in frame_sha256:
        found = _find_after(retained, cursor, lambda event: (
            event.get("type") == "visual_frame"
            and event.get("surface") == "brainstorm"
            and event.get("state") == "running"
            and event.get("sha256") == digest
        ))
        if found is None:
            return False
        cursor = found[0]
    return True


def summarize_receipt(
    receipt: dict[str, object], *, lens_video_sha256: str | None = None,
    hermes_run: str = "not_exercised", g2_reconnect: str = "not_exercised",
    require_cancel: bool = False, require_reconnect: bool = False,
    stream_frame_sha256: tuple[str, ...] = (), require_stream: bool = False,
) -> dict[str, object]:
    """Fail closed unless the full physical G2 flow is represented."""

    if lens_video_sha256 is not None:
        _sha256(lens_video_sha256, "lensVideoSha256")
    if hermes_run not in _HERMES_RUN_STATES:
        raise ValueError("hermes run state is invalid")
    if g2_reconnect not in _G2_RECONNECT_STATES:
        raise ValueError("G2 reconnect state is invalid")
    stream_frame_sha256 = tuple(
        _sha256(digest, "stream frame SHA-256") for digest in stream_frame_sha256
    )
    if len(stream_frame_sha256) > _STREAM_FRAME_TARGET:
        raise ValueError("stream frame evidence exceeds bounded pair")
    retained = retained_events(receipt)
    stream_verified = False

    def finish(result: str) -> dict[str, object]:
        if result == "pass" and require_stream and not stream_verified:
            result = "inconclusive"
        return _summary(
            result, retained, lens_video_sha256, hermes_run, g2_reconnect,
            stream_frame_sha256, stream_verified,
        )

    initial = next(((index, event) for index, event in enumerate(retained) if (
        event.get("type") == "visual_frame" and event["kind"] == "brainstorm"
        and event["surface"] == "brainstorm" and event["state"] == "running"
    )), None)
    if initial is None:
        return finish("inconclusive")
    terminal_action = _find_after(retained, initial[0], lambda event: (
        event.get("type") == "brainstorm_action" and event.get("action") == "show_terminal"
        and event.get("surface") == "brainstorm" and event.get("gesture") == "tap"
        and event.get("delivery") == "sys" and event.get("source") in _TERMINAL_TOGGLE_SOURCES
    ))
    if terminal_action is None:
        return finish("inconclusive")
    stream_verified = _ordered_stream_frames(
        retained[:terminal_action[0]], stream_frame_sha256,
    )
    terminal_frame = _find_after(retained, terminal_action[0], lambda event: (
        event.get("type") == "visual_frame" and event.get("kind") == "brainstorm"
        and event.get("surface") == "agent_terminal" and event.get("state") == "running"
    ))
    if terminal_frame is None or terminal_frame[1]["sha256"] == initial[1]["sha256"]:
        return finish("fail")
    return_action = _find_after(retained, terminal_frame[0], lambda event: (
        event.get("type") == "brainstorm_action" and event.get("action") == "show_brainstorm"
        and event.get("surface") == "agent_terminal" and event.get("gesture") == "tap"
        and event.get("delivery") == "sys" and event.get("source") in _TERMINAL_TOGGLE_SOURCES
    ))
    if return_action is None:
        return finish("inconclusive")
    returned = _find_after(retained, return_action[0], lambda event: (
        event.get("type") == "visual_frame" and event.get("kind") == "brainstorm"
        and event.get("surface") == "brainstorm" and event.get("state") == "running"
    ))
    if returned is None or returned[1]["sha256"] == terminal_frame[1]["sha256"]:
        return finish("fail")
    if require_reconnect and g2_reconnect != "restored":
        return finish("inconclusive")
    if not require_cancel:
        return finish("pass")
    cancelled = _find_after(retained, returned[0], lambda event: (
        event.get("type") == "brainstorm_action" and event.get("action") == "cancel"
        and event.get("surface") == "brainstorm" and event.get("gesture") == "double_press"
        and event.get("delivery") == "sys" and event.get("source") == "left"
    ))
    if cancelled is None:
        return finish("inconclusive")
    cancelled_frame = _find_after(retained, cancelled[0], lambda event: (
        event.get("type") == "visual_frame" and event.get("kind") == "brainstorm"
        and event.get("surface") == "brainstorm" and event.get("state") == "cancelled"
    ))
    if cancelled_frame is None:
        return finish("fail")
    if hermes_run != "cancelled":
        return finish("inconclusive")
    return finish("pass")


def write_owner_only_json(path: Path, payload: dict[str, object]) -> None:
    """Atomic owner-only output.  It never copies a lens recording."""

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    encoded = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, 0o600)
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


def acceptance_rejection_view(
    reason: Literal["not_armed", "unavailable", "busy"],
) -> BrainstormHudView:
    """Fixed wearer-visible response when a glasses-originating arm is refused."""

    if reason == "not_armed":
        return _view(
            "brainstorm",
            state="blocked",
            activity="Acceptance support trace is off",
        )
    if reason == "busy":
        return _view(
            "brainstorm",
            state="blocked",
            activity="Finish the active session, then retry acceptance",
        )
    return _view(
        "brainstorm",
        state="failed",
        activity="Acceptance unavailable; reconnect G2 and retry",
    )


class InAppBrainstormAcceptance:
    """One-shot physical G2 workflow driven by the production engine objects."""

    def __init__(
        self, *, transport: HubGatewayTransport, terminal: HermesStream | None,
        out: Path, action_timeout_s: float = 45.0, connect_timeout_s: float = 120.0,
        diagnostic_events: list[dict[str, object]] | None = None,
        is_isolated: Callable[[], bool] | None = None,
        trigger: BrainstormAction,
        connection_generation: int,
        evidence_mode: EvidenceMode,
        on_complete: Callable[[], None] | None = None,
    ) -> None:
        if (
            trigger.action != "begin_acceptance"
            or trigger.surface != "brainstorm"
            or trigger.source not in ("right", "ring")
            or trigger.gesture != "double_press"
        ):
            raise ValueError("Brainstorm acceptance requires the fixed G2 gesture")
        if connection_generation < 1:
            raise ValueError("Brainstorm acceptance requires an authenticated connection")
        self._transport = transport
        self._terminal = terminal
        self._out = out
        self._action_timeout_s = action_timeout_s
        self._connect_timeout_s = connect_timeout_s
        self._trigger_event: dict[str, object] = {
            "sequence": 1,
            "recordedAtMs": int(time.time() * 1_000),
            "type": "brainstorm_action",
            "action": "begin_acceptance",
            "surface": "brainstorm",
            "source": trigger.source,
            "gesture": "double_press",
            "delivery": "sys",
        }
        self._events = diagnostic_events if diagnostic_events is not None else []
        self._events.append(dict(self._trigger_event))
        self._is_isolated = is_isolated or (lambda: True)
        self._connection_generation = connection_generation
        self._action_generation = connection_generation
        self._evidence_mode = evidence_mode
        self._on_complete = on_complete
        self._actions: asyncio.Queue[BrainstormAction] = asyncio.Queue()
        self._accepted_hello = asyncio.Event()
        self._replacement_hello = asyncio.Event()
        self._reconnect_generation_floor: int | None = None
        self._task: asyncio.Task[None] | None = None
        self._stream_task: asyncio.Task[None] | None = None
        self._stream_proven = asyncio.Event()
        self._stream_frame_sha256: list[str] = []

    @property
    def output_path(self) -> Path:
        return self._out

    def observe_diagnostics(self, _path: Path, events: list[dict[str, Any]]) -> None:
        """G2DiagnosticStore callback; receives only protocol-sanitized events."""

        self.observe_diagnostic_events(events)

    def observe_diagnostic_events(self, events: list[dict[str, Any]]) -> None:
        """Admit a direct, already-sanitized diagnostic snapshot.

        This updates acceptance causality before the best-effort owner-only
        receipt write.  It deliberately receives the same bounded vocabulary
        as the persistence writer—never HUD text, transcript, or payload.
        """

        retained = [dict(self._trigger_event)]
        for sequence, event in enumerate(events, start=2):
            copy = dict(event)
            copy["sequence"] = sequence
            retained.append(copy)
        self._events[:] = retained

    def attach(self, *, subscribe_actions: bool = True) -> None:
        """Attach once to the live engine without taking over normal routing.

        Dynamic arming registers the production action router first and calls
        :meth:`handle_action` only while this controller is active.  Static
        tests can still subscribe directly.
        """

        if self._out.exists():
            raise FileExistsError("refusing to overwrite an acceptance receipt")
        self._transport.on_hello(self._on_hello)
        if subscribe_actions:
            self._transport.on_brainstorm_action(self._on_action)
        self._task = asyncio.get_running_loop().create_task(self._run())
        existing = self._transport.hello
        if self._transport.connected and existing is not None:
            self._on_hello(existing)

    async def wait(self) -> None:
        """Wait until this one-shot flow writes its final bounded receipt."""

        task = self._task
        if task is None:
            raise RuntimeError("acceptance controller was not attached")
        await asyncio.shield(task)

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

    def handle_action(self, action: BrainstormAction) -> None:
        """Receive a production-routed Brainstorm action while armed."""

        self._on_action(action)

    def _on_hello(self, hello: Hello) -> None:
        accepted = (
            hello.model == "g2"
            and (
                self._transport.physical_g2_connected
                if self._evidence_mode == "physical_g2"
                # Entry into simulator mode was already fail-closed by the
                # factory: either the Hello explicitly said simulator or the
                # isolated official-simulator runner supplied its one-way
                # evidence downgrade. The controller must not re-impose the
                # unreliable launchSource field after that decision.
                else True
            )
        )
        if not accepted:
            return
        generation = self._transport.connection_generation
        if generation == self._connection_generation:
            self._accepted_hello.set()
            return
        if generation > self._action_generation:
            # A real replacement before the explicit reconnect prompt is not
            # acceptance evidence, but it *is* now the current G2 surface.
            # Subsequent controls must bind to it rather than to the closed
            # predecessor.  The separate floor below still prevents it from
            # satisfying the later reconnect gate.
            self._action_generation = generation
        if (
            self._reconnect_generation_floor is not None
            and generation > self._reconnect_generation_floor
        ):
            self._replacement_hello.set()

    def _on_action(self, action: BrainstormAction) -> None:
        # A replay from the replaced authenticated socket is a valid-looking
        # control with the wrong wearer generation.  It cannot advance this
        # run or be retained as evidence for its successor.
        if action.connection_generation != self._action_generation:
            return
        self._actions.put_nowait(action)

    async def _wait_for(self, predicate: Callable[[], bool], timeout_s: float, message: str) -> None:
        deadline = time.monotonic() + timeout_s
        while not predicate():
            if time.monotonic() >= deadline:
                raise TimeoutError(message)
            await asyncio.sleep(0.05)

    def _require_isolated(self) -> None:
        if not self._is_isolated():
            raise RuntimeError("another wearer session is active")

    async def _send(self, surface: str, *, state: str = "running", activity: str) -> None:
        self._require_isolated()
        before = len(self._events)
        await self._transport.send(_view(surface, state=state, activity=activity))
        self._require_isolated()
        await self._wait_for(
            lambda: any(
                event.get("type") == "visual_frame" and event.get("kind") == "brainstorm"
                and event.get("surface") == surface and event.get("state") == state
                for event in self._events[before:]
            ),
            self._action_timeout_s,
            f"{surface} {state} visual frame was not acknowledged",
        )
        self._require_isolated()

    async def _expect(self, action: str) -> None:
        before = len(self._events)
        observed = await asyncio.wait_for(self._actions.get(), self._action_timeout_s)
        if observed.action != action:
            raise RuntimeError(f"expected {action} action")
        if observed.connection_generation != self._action_generation:
            raise RuntimeError("Brainstorm action generation is stale")
        # The official simulator exposes swipe-down only as a text-delivery
        # event and supplies no physical source. Exercise the visible cancel
        # transition, but do not invent left/right/ring provenance: the final
        # simulator receipt remains inconclusive because summarize_receipt()
        # still requires the real left-temple diagnostic event for a pass.
        if self._evidence_mode == "simulator" and action == "cancel":
            return

        expected_surface = "agent_terminal" if action == "show_brainstorm" else "brainstorm"
        expected_gesture = "double_press" if action == "cancel" else "tap"
        expected_sources = {"left"} if action == "cancel" else _TERMINAL_TOGGLE_SOURCES
        await self._wait_for(
            lambda: any(
                event.get("type") == "brainstorm_action"
                and event.get("action") == action
                and event.get("surface") == expected_surface
                and event.get("gesture") == expected_gesture
                and event.get("delivery") == "sys"
                and event.get("source") in expected_sources
                for event in self._events[before:]
            ),
            self._action_timeout_s,
            f"{action} action did not retain the matching G2 gesture receipt",
        )

    async def _next_running_frame_sha256(self, after: int) -> str:
        def next_frame() -> dict[str, object] | None:
            return next((event for event in self._events[after:] if (
                event.get("type") == "visual_frame"
                and event.get("kind") == "brainstorm"
                and event.get("surface") == "brainstorm"
                and event.get("state") == "running"
            )), None)

        self._require_isolated()
        await self._wait_for(
            lambda: next_frame() is not None,
            self._action_timeout_s,
            "streamed Brainstorm visual frame was not acknowledged",
        )
        self._require_isolated()
        frame = next_frame()
        assert frame is not None
        return _sha256(frame.get("sha256"), "stream frame SHA-256")

    async def _consume_hermes(self, terminal: HermesStream) -> None:
        def terminal_projection() -> dict[str, str | bool]:
            return {
                "state": "running", "reason": "", "endpoint_host": "",
                "no_tools_checked": True,
            }

        sink = HudSink(HudWriter(self._transport), terminal_projection)
        self._require_isolated()
        before = len(self._events)
        await sink.open("Acceptance run", ["Hermes"])
        await self._next_running_frame_sha256(before)
        before = len(self._events)
        await sink.turn("Hermes")
        await self._next_running_frame_sha256(before)

        attempts = 0
        async for chunk in terminal.stream(_FIXED_HERMES_PROMPT):
            if not chunk or len(self._stream_frame_sha256) >= _STREAM_FRAME_TARGET:
                continue
            if attempts >= _MAX_STREAM_FRAME_ATTEMPTS:
                continue
            attempts += 1
            self._require_isolated()
            before = len(self._events)
            await sink.chunk(chunk)
            digest = await self._next_running_frame_sha256(before)
            if digest not in self._stream_frame_sha256:
                self._stream_frame_sha256.append(digest)
            if len(self._stream_frame_sha256) >= _STREAM_FRAME_TARGET:
                self._stream_proven.set()

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
            self._require_isolated()
            await asyncio.wait_for(self._accepted_hello.wait(), self._connect_timeout_s)
            self._require_isolated()
            if self._terminal is None:
                await self._send("agent_terminal", state="blocked", activity="Hermes is not ready; acceptance stopped")
                return
            await self._send(
                "brainstorm",
                state="acceptance_ready",
                activity="Tap to start the Hermes acceptance run",
            )
            await self._expect("begin_topic_capture")
            self._stream_task = asyncio.create_task(self._consume_hermes(self._terminal))
            await asyncio.wait_for(self._stream_proven.wait(), self._action_timeout_s)
            await self._expect("show_terminal")
            await self._send("agent_terminal", activity="Dedicated no-tool Hermes session active")
            await self._expect("show_brainstorm")
            await self._send("brainstorm", activity="Hermes acceptance run active")
            # Only a connection established after the controller enters the
            # reconnect-instruction phase can satisfy this transition. An
            # earlier replacement is ignored and requires another one.
            self._replacement_hello.clear()
            self._reconnect_generation_floor = self._transport.connection_generation
            await self._send("brainstorm", activity="Reconnect G2 to restore this Hermes run")
            await asyncio.wait_for(self._replacement_hello.wait(), self._connect_timeout_s)
            await self._send("brainstorm", activity="Hermes run restored after G2 reconnect")
            g2_reconnect = "restored"
            await self._expect("cancel")
            hermes_run = await self._cancel_hermes()
            if hermes_run == "cancelled":
                await self._send(
                    "brainstorm",
                    state="cancelled",
                    activity="Hermes acceptance cancelled",
                )
            else:
                await self._send(
                    "brainstorm",
                    state="failed",
                    activity="Hermes abort was not confirmed; acceptance failed",
                )
        finally:
            if self._stream_task is not None and not self._stream_task.done():
                self._stream_task.cancel()
                await asyncio.gather(self._stream_task, return_exceptions=True)
            receipt: dict[str, object] = {"schema": TRACE_SCHEMA, "events": self._events}
            try:
                summary = summarize_receipt(
                    receipt, hermes_run=hermes_run, g2_reconnect=g2_reconnect,
                    require_cancel=True, require_reconnect=True,
                    stream_frame_sha256=tuple(self._stream_frame_sha256),
                    require_stream=True,
                )
            except ValueError:
                # A malformed diagnostic trace cannot be made into a receipt.
                summary = _summary("fail", [], None, hermes_run, g2_reconnect)
            summary["startedBy"] = {
                "interface": "g2",
                "modality": "gesture",
                "surface": "brainstorm",
                "source": self._trigger_event["source"],
                "gesture": "double_press",
                "delivery": "sys",
            }
            if self._evidence_mode == "simulator":
                summary["mode"] = "simulator_app_engine"
                if summary.get("result") == "pass":
                    summary["result"] = "simulator_pass"
            write_owner_only_json(self._out, summary)
            if self._on_complete is not None:
                self._on_complete()


class G2GestureBrainstormAcceptanceSession(Session):
    """Session-manager adapter for a glasses-originating acceptance run."""

    def __init__(
        self,
        *,
        transport: HubGatewayTransport,
        terminal: HermesStream | None,
        out: Path,
        action_timeout_s: float,
        connect_timeout_s: float,
        trigger: BrainstormAction,
        connection_generation: int,
        evidence_mode: EvidenceMode,
    ) -> None:
        events: list[dict[str, object]] = []
        self._transport = transport
        self._controller = InAppBrainstormAcceptance(
            transport=transport,
            terminal=terminal,
            out=out,
            action_timeout_s=action_timeout_s,
            connect_timeout_s=connect_timeout_s,
            diagnostic_events=events,
            trigger=trigger,
            connection_generation=connection_generation,
            evidence_mode=evidence_mode,
        )
        self._diagnostics = G2DiagnosticStore(
            writer=self._controller.observe_diagnostics,
            observer=self._controller.observe_diagnostic_events,
        )

    async def run(self) -> None:
        previous = self._transport.g2_diagnostics
        await self._transport.set_g2_diagnostics(self._diagnostics)
        try:
            self._controller.attach(subscribe_actions=False)
            await self._controller.wait()
        except asyncio.CancelledError:
            # The wearer's spoken escape ("JOAT, stop") lands here: the
            # manager cancels the task, and this session used to write only
            # its receipt — leaving the lenses showing a RUNNING Brainstorm
            # forever after the wearer had already ended it. Paint the
            # cancelled frame as a detached task: the transport outlives
            # this session, and teardown must not await a diagnostics ack
            # on a channel that is being torn down beneath it.
            paint = asyncio.get_running_loop().create_task(
                self._transport.send(_view(
                    "brainstorm", state="cancelled",
                    activity="Acceptance run cancelled",
                )),
            )
            paint.add_done_callback(lambda t: t.exception())
            await self._controller.close()
            raise
        finally:
            await self._transport.set_g2_diagnostics(previous)

    def cancel(self) -> None:
        # SessionManager cancels run() immediately after this synchronous hook;
        # the CancelledError branch above closes the controller and writes its
        # bounded inconclusive receipt.
        return None

    async def handle_brainstorm_action(self, action: BrainstormAction) -> bool:
        if action.action not in _BRAINSTORM_ACTIONS:
            return False
        self._controller.handle_action(action)
        return True

    @property
    def status(self) -> str:
        return "BRAINSTORM · connection test"

    @property
    def completion_state(self) -> str:
        return "settled"


def make_g2_gesture_acceptance_session(
    *,
    transport: HubGatewayTransport,
    terminal: Callable[[], HermesStream | None],
    trigger: BrainstormAction,
    output_directory: Path | None = None,
    action_timeout_s: float = 45.0,
    connect_timeout_s: float = 120.0,
    simulator_override: bool = False,
) -> Session | None:
    """Create the fixed gesture session for physical G2 or simulator proof.

    The official simulator currently reports ``launchSource=unknown``. Its
    isolated acceptance runner may opt into simulator evidence explicitly;
    the normal app engine leaves this false, so an unknown launch never gains
    physical-G2 authority and the override can only produce a downgraded
    ``simulator_app_engine`` receipt.
    """

    directory = output_directory or default_output_directory()
    hello = transport.hello
    if transport.physical_g2_connected:
        mode: EvidenceMode = "physical_g2"
    elif transport.connected and hello is not None \
            and hello.model == "g2" \
            and (hello.launch_source == "simulator" or simulator_override):
        mode = "simulator"
    else:
        return None
    if (
        trigger.action != "begin_acceptance"
        or trigger.surface != "brainstorm"
        or trigger.source not in ("right", "ring")
        or trigger.gesture != "double_press"
    ):
        raise ValueError("Brainstorm acceptance requires the fixed G2 gesture")
    if trigger.connection_generation != transport.connection_generation:
        return None
    return G2GestureBrainstormAcceptanceSession(
        transport=transport,
        terminal=terminal(),
        out=_next_output_path(directory, mode),
        action_timeout_s=action_timeout_s,
        connect_timeout_s=connect_timeout_s,
        trigger=trigger,
        connection_generation=transport.connection_generation,
        evidence_mode=mode,
    )


def try_start_g2_gesture_acceptance(
    *,
    transport: HubGatewayTransport,
    terminal: Callable[[], HermesStream | None],
    trigger: BrainstormAction,
    start_session: Callable[[Session], bool],
    acceptance_armed: bool,
    output_directory: Path | None = None,
    action_timeout_s: float = 45.0,
    connect_timeout_s: float = 120.0,
    simulator_override: bool = False,
) -> BrainstormHudView | None:
    """Start an explicitly armed diagnostic run, or return a fixed G2 frame.

    The ordinary wearable Brainstorm path must never enter an acceptance run.
    Keeping the gate here, rather than only in the app callback, protects a
    future caller from accidentally making a production gesture diagnostic.
    """

    if not acceptance_armed:
        return acceptance_rejection_view("not_armed")

    candidate = make_g2_gesture_acceptance_session(
        transport=transport,
        terminal=terminal,
        trigger=trigger,
        output_directory=output_directory,
        action_timeout_s=action_timeout_s,
        connect_timeout_s=connect_timeout_s,
        simulator_override=simulator_override,
    )
    if candidate is None:
        return acceptance_rejection_view("unavailable")
    if not start_session(candidate):
        return acceptance_rejection_view("busy")
    return None
