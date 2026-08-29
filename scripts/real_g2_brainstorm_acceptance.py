#!/usr/bin/env python3
"""Run a content-free physical-G2 Brainstorm / Agent Terminal acceptance loop.

The public command exercises JOAT's production paired no-tool Hermes session
through a fixed acceptance turn only after a physical G2 gesture starts it.
It uses the production authenticated Hub gateway and the already-paired
phone/G2. No spoken topic, Brainstorm transcript, Agent Terminal output,
endpoint, device identifier, session key, or OpenClaw payload is stored.

The recorded receipt contains only these bounded facts:

* the accepted visual-frame SHA-256 for the Brainstorm and Agent Terminal
  states;
* the exact fixed HUD state names;
* the accepted Brainstorm control action; and
* the gesture delivery and physical input source which produced that action;
* whether the no-tool Hermes run was cancelled while active; and
* whether a second physical-G2 hello restored the running Brainstorm HUD.

The trace is opt-in: this runner requires ``--confirm-physical``,
``--confirm-hermes``, and ``--confirm-g2-reconnect`` and refuses to overwrite
evidence. Stop the ordinary JOAT engine before running it so the paired G2 can
connect to this gateway. A physical lens recording is a separate proof
artifact; ``--lens-video`` stores its SHA-256 only, never a copy of the video.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any, Protocol

# Permit the checked-in runner to be launched directly from the repository;
# production imports remain the same package used by the JOAT application.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_MAC_PACKAGE_ROOT = _REPOSITORY_ROOT / "apps" / "mac"
if str(_MAC_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAC_PACKAGE_ROOT))

from joat.ble.hub_gateway import HubGatewayTransport
from joat.core.pairing import PairingStore
from joat.core.protocol import BrainstormHudView
from joat.agents.openclaw_gateway import (
    BRAINSTORM_TERMINAL_POLICY,
    OpenClawGatewayClient,
)
from joat.agents.openclaw_native import NativeOpenClawCredentials
from joat.agents.openclaw_runtime import OpenClawRuntimeConfig, OpenClawTerminalAgent
from joat.native.bridge import NativeBridgeClient
from joat.services.g2_diagnostics import G2DiagnosticStore, TRACE_SCHEMA


RUN_SCHEMA = "joat.g2-brainstorm-acceptance.v1"
MAX_RETAINED_EVENTS = 12
HEX_SHA256_LENGTH = 64
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
# This deliberately uninteresting fixed text is sent only after the wearer
# starts the opt-in physical acceptance.  It is never written to the receipt,
# console, trace, or HUD.  The longer response gives the wearer time to invoke
# the left-double cancel gesture while a real no-tool Gateway run is active.
_HERMES_ACCEPTANCE_PROMPT = (
    "JOAT physical acceptance run. Emit a long sequence of neutral status "
    "markers only. Do not use tools or call any external service."
)
log = logging.getLogger("joat.real_g2_brainstorm_acceptance")


class HermesStream(Protocol):
    """Only the no-tool stream/close surface the acceptance runner needs."""

    def stream(self, prompt: str) -> AsyncIterator[str]: ...

    async def close(self) -> None: ...


HermesContext = Callable[[], AbstractAsyncContextManager[HermesStream]]


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
        or len(value) != HEX_SHA256_LENGTH
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
    kind = event.get("kind")
    surface = event.get("surface")
    state = event.get("state")
    if kind != "brainstorm" or surface not in _HUD_SURFACES or state not in _HUD_STATES:
        raise ValueError("visual_frame contains an invalid fixed value")
    return {
        "type": "visual_frame",
        "kind": "brainstorm",
        "surface": surface,
        "state": state,
        "sha256": _sha256(event.get("sha256"), "sha256"),
    }


def _action_event(event: dict[str, object]) -> dict[str, str] | None:
    if event.get("type") != "brainstorm_action":
        return None
    expected = {"sequence", "recordedAtMs", "type", "action", "surface", "gesture", "delivery", "source"}
    if set(event) != expected:
        raise ValueError("brainstorm_action contains unsupported fields")
    action = event.get("action")
    surface = event.get("surface")
    gesture = event.get("gesture")
    delivery = event.get("delivery")
    source = event.get("source")
    if action not in _BRAINSTORM_ACTIONS:
        raise ValueError("brainstorm_action is invalid")
    if surface not in _HUD_SURFACES:
        raise ValueError("brainstorm_action surface is invalid")
    if gesture not in {"tap", "double_press"}:
        raise ValueError("brainstorm_action gesture is invalid")
    if delivery != "sys":
        raise ValueError("brainstorm_action delivery is invalid")
    if source not in _PHYSICAL_SOURCES:
        raise ValueError("brainstorm_action source is not a physical G2 input")
    return {
        "type": "brainstorm_action",
        "action": action,
        "surface": surface,
        "gesture": gesture,
        "delivery": delivery,
        "source": source,
    }


def _retained_events(events: list[dict[str, object]]) -> list[dict[str, str]]:
    """Strip receipt stamps and every event irrelevant to this acceptance run."""

    retained: list[dict[str, str]] = []
    for event in events:
        simplified = _frame_event(event) or _action_event(event)
        if simplified is not None:
            retained.append(simplified)
    if len(retained) > MAX_RETAINED_EVENTS:
        raise ValueError("acceptance run retained too many Brainstorm trace events")
    return retained


def _find_after(
    events: list[dict[str, str]],
    start: int,
    predicate: Callable[[dict[str, str]], bool],
) -> tuple[int, dict[str, str]] | None:
    for index in range(start + 1, len(events)):
        event = events[index]
        if predicate(event):
            return index, event
    return None


def summarize_receipt(
    receipt: dict[str, object],
    *,
    lens_video_sha256: str | None = None,
    hermes_run: str = "not_exercised",
    g2_reconnect: str = "not_exercised",
    require_cancel: bool = False,
    require_reconnect: bool = False,
) -> dict[str, object]:
    """Verify the physical Brainstorm → Terminal → Brainstorm contract.

    A passed receipt proves authenticated G2 control flow and completed visual
    frame upload.  It intentionally does *not* claim lens visibility unless a
    separately recorded lens-video digest accompanies the run.  A live-Hermes
    pass additionally requires a source-attested left-double cancellation while
    its no-tool stream is still active; no remote response is retained.
    """

    if lens_video_sha256 is not None:
        _sha256(lens_video_sha256, "lensVideoSha256")
    if hermes_run not in _HERMES_RUN_STATES:
        raise ValueError("hermes run state is invalid")
    if g2_reconnect not in _G2_RECONNECT_STATES:
        raise ValueError("G2 reconnect state is invalid")
    retained = _retained_events(_require_events(receipt))
    initial = next(
        (
            (index, event)
            for index, event in enumerate(retained)
            if event.get("type") == "visual_frame"
            and event["kind"] == "brainstorm"
            and event["surface"] == "brainstorm"
            and event["state"] == "running"
        ),
        None,
    )
    if initial is None:
        return _summary("inconclusive", retained, lens_video_sha256, hermes_run, g2_reconnect)

    terminal_action = _find_after(
        retained,
        initial[0],
        lambda event: event.get("type") == "brainstorm_action"
        and event.get("action") == "show_terminal"
        and event.get("surface") == "brainstorm"
        and event.get("gesture") == "tap"
        and event.get("delivery") == "sys"
        and event.get("source") in _TERMINAL_TOGGLE_SOURCES,
    )
    if terminal_action is None:
        return _summary("inconclusive", retained, lens_video_sha256, hermes_run, g2_reconnect)
    terminal_frame = _find_after(
        retained,
        terminal_action[0],
        lambda event: event.get("type") == "visual_frame"
        and event.get("kind") == "brainstorm"
        and event.get("surface") == "agent_terminal"
        and event.get("state") == "running"
    )
    if terminal_frame is None:
        return _summary("fail", retained, lens_video_sha256, hermes_run, g2_reconnect)
    if terminal_frame[1]["sha256"] == initial[1]["sha256"]:
        return _summary("fail", retained, lens_video_sha256, hermes_run, g2_reconnect)

    return_action = _find_after(
        retained,
        terminal_frame[0],
        lambda event: event.get("type") == "brainstorm_action"
        and event.get("action") == "show_brainstorm"
        and event.get("surface") == "agent_terminal"
        and event.get("gesture") == "tap"
        and event.get("delivery") == "sys"
        and event.get("source") in _TERMINAL_TOGGLE_SOURCES,
    )
    if return_action is None:
        return _summary("inconclusive", retained, lens_video_sha256, hermes_run, g2_reconnect)
    returned_frame = _find_after(
        retained,
        return_action[0],
        lambda event: event.get("type") == "visual_frame"
        and event.get("kind") == "brainstorm"
        and event.get("surface") == "brainstorm"
        and event.get("state") == "running"
    )
    if returned_frame is None:
        return _summary("fail", retained, lens_video_sha256, hermes_run, g2_reconnect)
    if returned_frame[1]["sha256"] == terminal_frame[1]["sha256"]:
        return _summary("fail", retained, lens_video_sha256, hermes_run, g2_reconnect)

    if require_reconnect and g2_reconnect != "restored":
        return _summary("inconclusive", retained, lens_video_sha256, hermes_run, g2_reconnect)
    if not require_cancel:
        return _summary("pass", retained, lens_video_sha256, hermes_run, g2_reconnect)
    cancel_action = _find_after(
        retained,
        returned_frame[0],
        lambda event: event.get("type") == "brainstorm_action"
        and event.get("action") == "cancel"
        and event.get("surface") == "brainstorm"
        and event.get("gesture") == "double_press"
        and event.get("delivery") == "sys"
        and event.get("source") == "left",
    )
    if cancel_action is None:
        return _summary("inconclusive", retained, lens_video_sha256, hermes_run, g2_reconnect)
    cancelled_frame = _find_after(
        retained,
        cancel_action[0],
        lambda event: event.get("type") == "visual_frame"
        and event.get("kind") == "brainstorm"
        and event.get("surface") == "brainstorm"
        and event.get("state") == "cancelled",
    )
    if cancelled_frame is None:
        return _summary("fail", retained, lens_video_sha256, hermes_run, g2_reconnect)
    if hermes_run != "cancelled":
        return _summary("inconclusive", retained, lens_video_sha256, hermes_run, g2_reconnect)
    return _summary("pass", retained, lens_video_sha256, hermes_run, g2_reconnect)


def _summary(
    result: str,
    retained: list[dict[str, str]],
    lens_video_sha256: str | None,
    hermes_run: str,
    g2_reconnect: str,
) -> dict[str, object]:
    return {
        "schema": RUN_SCHEMA,
        "mode": "physical_g2",
        "result": result,
        "retainedEventCount": len(retained),
        "events": retained,
        "wearerVisibleHud": {
            # Raw image frames and successful BLE calls establish the exact
            # G2 HUD transition.  A lens video remains the only pixel-level
            # evidence and is intentionally kept outside this receipt.
            "bridge": "verified" if result == "pass" else "not_verified",
            "lens": "video_digest_recorded" if lens_video_sha256 else "not_recorded",
        },
        "lensVideoSha256": lens_video_sha256,
        "hermesRun": hermes_run,
        "g2Reconnect": g2_reconnect,
    }


def _write_owner_only_json(path: Path, payload: dict[str, object]) -> None:
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


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@asynccontextmanager
async def _live_hermes(bridge_path: Path) -> AsyncIterator[HermesStream]:
    """Open JOAT's production paired, isolated, no-tool Hermes session.

    The native bridge remains open for the entire run so reconnect recovery
    retains the same Keychain-backed device identity.  No endpoint, token,
    device identity, session key, prompt, or response crosses this seam into
    the acceptance receipt.
    """

    config = OpenClawRuntimeConfig.load()
    if config is None:
        raise RuntimeError("OpenClaw Agent Terminal is not configured")
    async with NativeBridgeClient(bridge_path) as bridge:
        credentials = await NativeOpenClawCredentials.open(
            bridge=bridge,
            profile=config.profile,
        )
        client = OpenClawGatewayClient(
            profile=config.profile,
            identity=credentials,
            token_store=credentials,
            scopes=("operator.read", "operator.write"),
            capabilities=("tool-events",),
            policy=BRAINSTORM_TERMINAL_POLICY,
        )
        terminal = OpenClawTerminalAgent(
            client=client,
            agent_id=config.agent_id,
            policy=BRAINSTORM_TERMINAL_POLICY,
        )
        try:
            # `initialize` verifies the exact no-route session and an empty
            # effective tool inventory before any acceptance prompt can leave
            # JOAT.  It raises instead of falling back to main/Telegram.
            await terminal.initialize()
            yield terminal
        finally:
            await terminal.close()


@asynccontextmanager
async def _without_hermes() -> AsyncIterator[HermesStream | None]:
    """Keep the original, HUD-only runner usable in isolated contract tests."""

    yield None


async def _consume_hermes(agent: HermesStream, first_chunk: asyncio.Event) -> None:
    """Exercise a real stream while deliberately retaining none of its text."""

    async for _ignored_chunk in agent.stream(_HERMES_ACCEPTANCE_PROMPT):
        # Do not aggregate, display, log, hash, or otherwise retain remote
        # content.  Cancellation propagates into OpenClawTerminalAgent, which
        # performs its bounded `sessions.abort` confirmation before closing.
        first_chunk.set()


def _view(
    surface: str,
    *,
    state: str = "running",
    activity: str = "HUD transition check",
) -> BrainstormHudView:
    if surface == "brainstorm":
        return BrainstormHudView(
            screen="brainstorm",
            state=state,
            topic="Acceptance run",
            collaborators=[],
            activity=activity,
            terminal_state="ready" if state != "cancelled" else "cancelled",
            no_tools_checked=True,
        )
    return BrainstormHudView(
        screen="agent_terminal",
        state=state,
        topic="Agent Terminal",
        collaborators=[],
        activity=activity,
        terminal_state="ready",
        no_tools_checked=True,
    )


async def _wait_for(
    predicate: Callable[[], bool],
    timeout_s: float,
    message: str,
) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError(message)
        await asyncio.sleep(0.05)


def _frame_count(
    events: list[dict[str, object]],
    surface: str,
    state: str = "running",
) -> int:
    return sum(
        event.get("type") == "visual_frame"
        and event.get("kind") == "brainstorm"
        and event.get("surface") == surface
        and event.get("state") == state
        for event in events
    )


def _action_count(events: list[dict[str, object]], action: str) -> int:
    return sum(
        event.get("type") == "brainstorm_action"
        and event.get("action") == action
        for event in events
    )


async def capture(
    *,
    out: Path,
    port: int,
    connect_timeout_s: float,
    action_timeout_s: float,
    lens_video: Path | None = None,
    _gateway_factory: Callable[[G2DiagnosticStore], HubGatewayTransport] | None = None,
    _hermes_context: HermesContext | None = None,
    _require_cancel: bool = False,
    _require_reconnect: bool = False,
) -> dict[str, object]:
    """Run the physical G2 loop, optionally with a real no-tool Hermes run.

    The public CLI always supplies a live Hermes context and requires the
    cancellation phase. The omitted form is solely for the original isolated
    HUD contract test and records ``not_exercised`` for Hermes.
    """

    if out.exists():
        raise FileExistsError("refusing to overwrite an existing acceptance receipt")
    if lens_video is not None and not lens_video.is_file():
        raise FileNotFoundError("lens video does not exist")

    with tempfile.TemporaryDirectory(prefix="joat-g2-brainstorm-") as temporary_directory:
        trace_path = Path(temporary_directory) / "trace.json"
        observed_events: list[dict[str, object]] = []

        def observe(_path: Path, events: list[dict[str, Any]]) -> None:
            observed_events[:] = [dict(event) for event in events]

        store = G2DiagnosticStore(path=trace_path, writer=observe)
        gateway = _gateway_factory(store) if _gateway_factory else HubGatewayTransport(
            host="0.0.0.0",
            port=port,
            pairing=PairingStore(),
            # This is a physical run.  Do not grant a loopback socket the
            # simulator exemption while we are asserting G2 input provenance.
            trust_loopback=False,
            g2_diagnostics=store,
        )
        hello: list[str] = []
        actions: asyncio.Queue[str] = asyncio.Queue()
        gateway.on_hello(lambda message: hello.append(message.model))
        gateway.on_brainstorm_action(lambda message: actions.put_nowait(message.action))

        hermes_run = "not_exercised"
        g2_reconnect = "not_exercised"
        hermes_task: asyncio.Task[None] | None = None
        hermes_first_chunk = asyncio.Event()
        await gateway.start()
        try:
            await _wait_for(
                lambda: gateway.connected and bool(hello) and hello[-1] == "g2",
                connect_timeout_s,
                "waiting for the paired physical G2 connection",
            )
            # Do not even initialize the remote Agent Terminal until a
            # physical G2 is attached to this opt-in acceptance gateway.
            hermes_context = _hermes_context() if _hermes_context is not None else _without_hermes()
            async with hermes_context as hermes:
                if hermes is None:
                    # Keep the existing local HUD-only contract runnable for
                    # isolated test coverage, but make its evidence boundary
                    # explicit in the receipt.
                    await gateway.send(_view("brainstorm"))
                    await _wait_for(
                        lambda: _frame_count(observed_events, "brainstorm") >= 1,
                        action_timeout_s,
                        "Brainstorm visual frame was not acknowledged",
                    )
                else:
                    # Connecting the glasses never starts a remote run. The
                    # wearer must first accept the visible ready state.
                    await gateway.send(_view(
                        "brainstorm",
                        state="ready",
                        activity="Tap to begin the Hermes acceptance run",
                    ))
                    await _wait_for(
                        lambda: _frame_count(observed_events, "brainstorm", "ready") >= 1,
                        action_timeout_s,
                        "ready Brainstorm visual frame was not acknowledged",
                    )
                    begin = await asyncio.wait_for(actions.get(), action_timeout_s)
                    if begin != "begin_topic_capture":
                        raise RuntimeError("expected a physical tap to begin the Hermes run")
                    await _wait_for(
                        lambda: _action_count(observed_events, "begin_topic_capture") >= 1,
                        action_timeout_s,
                        "begin action did not retain its physical G2 receipt",
                    )
                    hermes_task = asyncio.create_task(_consume_hermes(hermes, hermes_first_chunk))
                    await _wait_for(
                        hermes_first_chunk.is_set,
                        action_timeout_s,
                        "Hermes did not produce a bounded acceptance stream",
                    )
                    await gateway.send(_view(
                        "brainstorm",
                        activity="Hermes acceptance run active",
                    ))
                    await _wait_for(
                        lambda: _frame_count(observed_events, "brainstorm") >= 1,
                        action_timeout_s,
                        "running Brainstorm visual frame was not acknowledged",
                    )

                first_action = await asyncio.wait_for(actions.get(), action_timeout_s)
                if first_action != "show_terminal":
                    raise RuntimeError("expected a live-Brainstorm tap to open Agent Terminal")
                await _wait_for(
                    lambda: _action_count(observed_events, "show_terminal") >= 1,
                    action_timeout_s,
                    "terminal action did not retain its physical G2 receipt",
                )

                await gateway.send(_view(
                    "agent_terminal",
                    activity="Dedicated Hermes session active",
                ))
                await _wait_for(
                    lambda: _frame_count(observed_events, "agent_terminal") >= 1,
                    action_timeout_s,
                    "Agent Terminal visual frame was not acknowledged",
                )
                second_action = await asyncio.wait_for(actions.get(), action_timeout_s)
                if second_action != "show_brainstorm":
                    raise RuntimeError("expected an Agent Terminal tap to return to Brainstorm")
                await _wait_for(
                    lambda: _action_count(observed_events, "show_brainstorm") >= 1,
                    action_timeout_s,
                    "return action did not retain its physical G2 receipt",
                )

                await gateway.send(_view(
                    "brainstorm",
                    activity="Hermes acceptance run active" if hermes is not None else "HUD transition check",
                ))
                await _wait_for(
                    lambda: _frame_count(observed_events, "brainstorm") >= 2,
                    action_timeout_s,
                    "returned Brainstorm visual frame was not acknowledged",
                )

                if _require_reconnect:
                    # The initial phone-side page is intentionally replaced.
                    # A second G2 hello must restore the current Brainstorm
                    # state and acknowledge a fresh visual frame; a stale
                    # cached image cannot satisfy this count.
                    frames_before_reconnect = _frame_count(observed_events, "brainstorm")
                    hellos_before_reconnect = len(hello)
                    await gateway.send(_view(
                        "brainstorm",
                        activity="Reconnect G2 to restore this Hermes run",
                    ))
                    await _wait_for(
                        lambda: _frame_count(observed_events, "brainstorm") > frames_before_reconnect,
                        action_timeout_s,
                        "reconnect instruction frame was not acknowledged",
                    )
                    await _wait_for(
                        lambda: len(hello) > hellos_before_reconnect and gateway.connected,
                        connect_timeout_s,
                        "waiting for the replacement physical G2 connection",
                    )
                    frames_before_restore = _frame_count(observed_events, "brainstorm")
                    await gateway.send(_view(
                        "brainstorm",
                        activity="Hermes run restored after G2 reconnect",
                    ))
                    await _wait_for(
                        lambda: _frame_count(observed_events, "brainstorm") > frames_before_restore,
                        action_timeout_s,
                        "restored Brainstorm visual frame was not acknowledged",
                    )
                    g2_reconnect = "restored"

                if _require_cancel:
                    cancellation = await asyncio.wait_for(actions.get(), action_timeout_s)
                    if cancellation != "cancel":
                        raise RuntimeError("expected a left-double physical G2 cancellation")
                    await _wait_for(
                        lambda: _action_count(observed_events, "cancel") >= 1,
                        action_timeout_s,
                        "cancel action did not retain its physical G2 receipt",
                    )
                    if hermes_task is None:
                        hermes_run = "failed"
                    elif hermes_task.done():
                        # Completion before the physical cancel is explicitly
                        # inconclusive; it must never be relabelled an abort.
                        # Retrieve an already-failed task as well: a Gateway
                        # error is a safe bounded state, not a completion.
                        try:
                            hermes_task.result()
                        except asyncio.CancelledError:
                            hermes_run = "cancelled"
                        except Exception:
                            hermes_run = "failed"
                        else:
                            hermes_run = "completed"
                    else:
                        hermes_task.cancel()
                        cancelled = await asyncio.gather(hermes_task, return_exceptions=True)
                        if not isinstance(cancelled[0], asyncio.CancelledError):
                            raise RuntimeError("Hermes run did not acknowledge cancellation")
                        hermes_run = "cancelled"
                    await gateway.send(_view(
                        "brainstorm",
                        state="cancelled",
                        activity="Hermes acceptance cancelled",
                    ))
                    await _wait_for(
                        lambda: _frame_count(observed_events, "brainstorm", "cancelled") >= 1,
                        action_timeout_s,
                        "cancelled Brainstorm visual frame was not acknowledged",
                    )
        finally:
            if hermes_task is not None and not hermes_task.done():
                hermes_task.cancel()
                await asyncio.gather(hermes_task, return_exceptions=True)
            await gateway.stop()

        receipt: dict[str, object] = {"schema": TRACE_SCHEMA, "events": observed_events}
        summary = summarize_receipt(
            receipt,
            lens_video_sha256=_file_sha256(lens_video) if lens_video else None,
            hermes_run=hermes_run,
            g2_reconnect=g2_reconnect,
            require_cancel=_require_cancel,
            require_reconnect=_require_reconnect,
        )

    _write_owner_only_json(out, summary)
    return summary


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def configure_logging() -> None:
    """The console is as content-free as the exported receipt."""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    for logger_name in ("joat.ble.hub_gateway", "websockets"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--connect-timeout", type=_positive_float, default=120.0)
    parser.add_argument("--action-timeout", type=_positive_float, default=45.0)
    parser.add_argument(
        "--lens-video",
        type=Path,
        help="optional physical lens recording; only its SHA-256 enters the receipt",
    )
    parser.add_argument(
        "--confirm-physical",
        action="store_true",
        help="required acknowledgement that this takes over the paired G2 gateway",
    )
    parser.add_argument(
        "--confirm-hermes",
        action="store_true",
        help="required acknowledgement that this runs one fixed no-tool Hermes acceptance turn",
    )
    parser.add_argument(
        "--confirm-g2-reconnect",
        action="store_true",
        help="required acknowledgement that this run asks you to reconnect the installed G2 once",
    )
    parser.add_argument(
        "--bridge",
        type=Path,
        default=Path(
            os.environ.get(
                "JOAT_NATIVE_BRIDGE",
                str(Path.home() / "Library" / "Application Support" / "JOAT" / "native-bridge.sock"),
            )
        ),
        help="owner-only JOAT native bridge socket",
    )
    args = parser.parse_args()
    if not args.confirm_physical:
        parser.error("--confirm-physical is required; stop the ordinary JOAT engine first")
    if not args.confirm_hermes:
        parser.error("--confirm-hermes is required; this run sends one fixed no-tool Hermes turn")
    if not args.confirm_g2_reconnect:
        parser.error("--confirm-g2-reconnect is required; this run verifies HUD recovery after a new G2 connection")
    configure_logging()
    summary = asyncio.run(
        capture(
            out=args.out,
            port=args.port,
            connect_timeout_s=args.connect_timeout,
            action_timeout_s=args.action_timeout,
            lens_video=args.lens_video,
            _hermes_context=lambda: _live_hermes(args.bridge),
            _require_cancel=True,
            _require_reconnect=True,
        )
    )
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
