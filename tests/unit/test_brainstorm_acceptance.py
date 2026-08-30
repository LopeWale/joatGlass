"""Focused in-engine acceptance controller tests; no Gateway is contacted."""

from __future__ import annotations

import asyncio
import hashlib
import json
import stat
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from joat.core.protocol import BrainstormAction, BrainstormHudView, Hello
from joat.services.brainstorm_acceptance import (
    G2GestureBrainstormAcceptanceSession,
    InAppBrainstormAcceptance,
    acceptance_rejection_view,
    make_g2_gesture_acceptance_session,
    summarize_receipt,
    try_start_g2_gesture_acceptance,
)
from joat.services.g2_diagnostics import TRACE_SCHEMA


class FakeTransport:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self.events = events
        self.connected = True
        self.physical_g2_connected = True
        self.connection_generation = 1
        self.hello: Hello | None = None
        self.sent: list[BrainstormHudView] = []
        self.hello_callbacks: list[Callable[[Hello], None]] = []
        self.action_callbacks: list[Callable[[BrainstormAction], None]] = []
        self._g2_diagnostics: object | None = None

    def on_hello(self, callback: Callable[[Hello], None]) -> None:
        self.hello_callbacks.append(callback)

    def on_brainstorm_action(self, callback: Callable[[BrainstormAction], None]) -> None:
        self.action_callbacks.append(callback)

    @property
    def g2_diagnostics(self) -> object | None:
        return self._g2_diagnostics

    async def set_g2_diagnostics(self, store: object | None) -> None:
        self._g2_diagnostics = store

    async def send(self, view: BrainstormHudView) -> None:
        self.sent.append(view)
        digest = hashlib.sha256(json.dumps(
            view.to_wire(), sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        event: dict[str, str] = {
            "type": "visual_frame",
            "kind": "brainstorm",
            "surface": view.screen,
            "state": view.state,
            "sha256": digest,
        }
        self.events.append({
            "sequence": len(self.events) + 1,
            "recordedAtMs": len(self.events) + 1,
            **event,
        })
        record = getattr(self._g2_diagnostics, "record", None)
        if record is not None:
            record(event)

    def emit_hello(self, *, new_connection: bool = False, launch_source: str = "physical") -> None:
        if new_connection:
            self.connection_generation += 1
        message = Hello(sn="g2-not-retained", model="g2", launch_source=launch_source)
        self.hello = message
        for callback in self.hello_callbacks:
            callback(message)

    def action(
        self,
        action: str,
        surface: str,
        source: str,
        gesture: str = "tap",
        *,
        record_diagnostic: bool = True,
    ) -> None:
        event = {
            "type": "brainstorm_action",
            "action": action,
            "surface": surface,
            "gesture": gesture,
            "delivery": "sys",
            "source": source,
        }
        if record_diagnostic:
            self.events.append({
                "sequence": len(self.events) + 1,
                "recordedAtMs": len(self.events) + 1,
                **event,
            })
            record = getattr(self._g2_diagnostics, "record", None)
            if record is not None:
                record(event)
        message = BrainstormAction(
            action=action,
            connection_generation=self.connection_generation,
        )
        for callback in self.action_callbacks:
            callback(message)


class FakeHermes:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.cancelled = asyncio.Event()

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        self.prompts.append(prompt)
        try:
            yield "private remote output one "
            yield "private remote output two"
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class UnconfirmedAbortHermes(FakeHermes):
    async def stream(self, prompt: str) -> AsyncIterator[str]:
        self.prompts.append(prompt)
        try:
            yield "private remote output one "
            yield "private remote output two"
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            raise RuntimeError("abort not confirmed") from exc


def acceptance_trigger(source: str = "right", generation: int = 1) -> BrainstormAction:
    return BrainstormAction(
        action="begin_acceptance",
        surface="brainstorm",
        source=source,  # type: ignore[arg-type]
        gesture="double_press",
        connection_generation=generation,
    )


async def _wait_for(predicate: Callable[[], bool]) -> None:
    for _ in range(1_000):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("acceptance controller did not advance")


@pytest.mark.asyncio
async def test_in_app_controller_streams_hermes_to_hud_but_retains_only_frame_hashes(
    tmp_path: Path,
) -> None:
    events: list[dict[str, object]] = []
    transport = FakeTransport(events)
    hermes = FakeHermes()
    out = tmp_path / "receipt.json"
    completed = asyncio.Event()
    controller = InAppBrainstormAcceptance(
        transport=transport, terminal=hermes, out=out,
        diagnostic_events=events, action_timeout_s=1, connect_timeout_s=1,
        trigger=acceptance_trigger(), connection_generation=1,
        evidence_mode="physical_g2", on_complete=completed.set,
    )
    controller.attach()

    transport.emit_hello()
    await _wait_for(lambda: len(transport.sent) == 1)
    assert transport.sent[-1].state == "acceptance_ready"
    transport.action("begin_topic_capture", "brainstorm", "right")
    await _wait_for(lambda: len(transport.sent) == 5)
    streamed = transport.sent[1:5]
    assert [view.activity for view in streamed] == [
        "Preparing collaborators…",
        "Thinking…",
        "Thinking… private remote output one ",
        "Thinking… private remote output one private remote output two",
    ]
    assert all(view.state == "running" for view in streamed)
    transport.action("show_terminal", "brainstorm", "right")
    await _wait_for(lambda: len(transport.sent) == 6)
    assert transport.sent[-1].screen == "agent_terminal"
    transport.action("show_brainstorm", "agent_terminal", "ring")
    await _wait_for(lambda: len(transport.sent) == 8)
    transport.emit_hello(new_connection=True)
    await _wait_for(lambda: len(transport.sent) == 9)
    transport.action("cancel", "brainstorm", "left", "double_press")
    await asyncio.wait_for(completed.wait(), timeout=1)

    receipt = json.loads(out.read_text())
    assert receipt["result"] == "pass"
    assert receipt["startedBy"] == {
        "interface": "g2",
        "modality": "gesture",
        "surface": "brainstorm",
        "source": "right",
        "gesture": "double_press",
        "delivery": "sys",
    }
    assert receipt["hermesRun"] == "cancelled"
    assert receipt["g2Reconnect"] == "restored"
    streamed_hashes = receipt["hermesStream"]["frameSha256"]
    assert len(streamed_hashes) == 2
    assert streamed_hashes[0] != streamed_hashes[1]
    assert receipt["hermesStream"]["hud"] == "verified"
    assert "private remote output" not in out.read_text()
    assert "JOAT physical acceptance run" not in out.read_text()
    assert all("prompt" not in event for event in receipt["events"])
    raw_receipt: dict[str, object] = {"schema": TRACE_SCHEMA, "events": events}
    assert summarize_receipt(
        raw_receipt,
        hermes_run="cancelled",
        g2_reconnect="restored",
        require_cancel=True,
        require_reconnect=True,
        stream_frame_sha256=(streamed_hashes[0], streamed_hashes[0]),
        require_stream=True,
    )["result"] == "inconclusive"
    assert summarize_receipt(
        raw_receipt,
        hermes_run="cancelled",
        g2_reconnect="restored",
        require_cancel=True,
        require_reconnect=True,
        stream_frame_sha256=tuple(reversed(streamed_hashes)),
        require_stream=True,
    )["result"] == "inconclusive"
    terminal_action_index = next(index for index, event in enumerate(events) if (
        event.get("type") == "brainstorm_action"
        and event.get("action") == "show_terminal"
    ))
    later_running_hashes = tuple(
        str(event["sha256"])
        for event in events[terminal_action_index + 1:]
        if event.get("type") == "visual_frame"
        and event.get("surface") == "brainstorm"
        and event.get("state") == "running"
    )[:2]
    assert len(later_running_hashes) == 2
    assert summarize_receipt(
        raw_receipt,
        hermes_run="cancelled",
        g2_reconnect="restored",
        require_cancel=True,
        require_reconnect=True,
        stream_frame_sha256=later_running_hashes,
        require_stream=True,
    )["result"] == "inconclusive"
    with pytest.raises(ValueError, match="bounded pair"):
        summarize_receipt(
            raw_receipt,
            hermes_run="cancelled",
            g2_reconnect="restored",
            require_cancel=True,
            require_reconnect=True,
            stream_frame_sha256=(
                streamed_hashes[0], streamed_hashes[1], streamed_hashes[0],
            ),
            require_stream=True,
        )
    assert hermes.prompts and await hermes.cancelled.wait()
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_unconfirmed_hermes_abort_never_renders_cancelled_success(
    tmp_path: Path,
) -> None:
    events: list[dict[str, object]] = []
    transport = FakeTransport(events)
    completed = asyncio.Event()
    controller = InAppBrainstormAcceptance(
        transport=transport,
        terminal=UnconfirmedAbortHermes(),
        out=tmp_path / "receipt.json",
        diagnostic_events=events,
        action_timeout_s=1,
        connect_timeout_s=1,
        trigger=acceptance_trigger(),
        connection_generation=1,
        evidence_mode="physical_g2",
        on_complete=completed.set,
    )
    controller.attach()
    transport.emit_hello()
    await _wait_for(lambda: len(transport.sent) == 1)
    transport.action("begin_topic_capture", "brainstorm", "right")
    await _wait_for(lambda: len(transport.sent) == 5)
    transport.action("show_terminal", "brainstorm", "right")
    await _wait_for(lambda: len(transport.sent) == 6)
    transport.action("show_brainstorm", "agent_terminal", "right")
    await _wait_for(lambda: len(transport.sent) == 8)
    transport.emit_hello(new_connection=True)
    await _wait_for(lambda: len(transport.sent) == 9)
    transport.action("cancel", "brainstorm", "left", "double_press")
    await asyncio.wait_for(completed.wait(), timeout=1)

    assert transport.sent[-1].state == "failed"
    assert "not confirmed" in transport.sent[-1].activity
    receipt = json.loads(controller.output_path.read_text())
    assert receipt["hermesRun"] == "failed"
    assert receipt["result"] != "pass"


def test_rejected_acceptance_has_a_fixed_wearer_visible_recovery_frame() -> None:
    not_armed = acceptance_rejection_view("not_armed")
    unavailable = acceptance_rejection_view("unavailable")
    busy = acceptance_rejection_view("busy")

    assert not_armed.state == "blocked"
    assert not_armed.activity == "Acceptance support trace is off"
    assert unavailable.state == "failed"
    assert unavailable.activity == "Acceptance unavailable; reconnect G2 and retry"
    assert busy.state == "blocked"
    assert busy.activity == "Finish the active session, then retry acceptance"
    assert unavailable.topic == busy.topic == "Acceptance run"


def test_glasses_start_returns_an_explicit_rejection_for_unavailable_or_busy(
    tmp_path: Path,
) -> None:
    events: list[dict[str, object]] = []
    transport = FakeTransport(events)
    transport.connected = False
    transport.physical_g2_connected = False
    starts: list[object] = []

    def reject_start(session: object) -> bool:
        starts.append(session)
        return False

    unavailable = try_start_g2_gesture_acceptance(
        transport=transport,
        terminal=lambda: FakeHermes(),
        trigger=acceptance_trigger(),
        start_session=reject_start,
        acceptance_armed=True,
        output_directory=tmp_path,
    )
    assert unavailable is not None and unavailable.state == "failed"
    assert starts == []

    transport.connected = True
    transport.physical_g2_connected = True
    transport.emit_hello()
    busy = try_start_g2_gesture_acceptance(
        transport=transport,
        terminal=lambda: FakeHermes(),
        trigger=acceptance_trigger(),
        start_session=reject_start,
        acceptance_armed=True,
        output_directory=tmp_path,
    )
    assert busy is not None and busy.state == "blocked"
    assert len(starts) == 1

    accepted = try_start_g2_gesture_acceptance(
        transport=transport,
        terminal=lambda: FakeHermes(),
        trigger=acceptance_trigger(),
        start_session=lambda _session: True,
        acceptance_armed=True,
        output_directory=tmp_path,
    )
    assert accepted is None


def test_glasses_start_refuses_an_unarmed_production_gesture(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    transport = FakeTransport(events)
    transport.emit_hello()
    starts: list[object] = []
    terminal_called = False

    def terminal() -> FakeHermes:
        nonlocal terminal_called
        terminal_called = True
        return FakeHermes()

    rejection = try_start_g2_gesture_acceptance(
        transport=transport,
        terminal=terminal,
        trigger=acceptance_trigger(),
        start_session=lambda session: starts.append(session) or True,
        acceptance_armed=False,
        output_directory=tmp_path,
    )

    assert rejection is not None
    assert rejection.state == "blocked"
    assert rejection.activity == "Acceptance support trace is off"
    assert starts == []
    assert terminal_called is False
    assert list(tmp_path.iterdir()) == []


def test_glasses_start_rejects_a_stale_authenticated_generation(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    transport = FakeTransport(events)
    transport.emit_hello()
    transport.connection_generation = 2

    rejection = try_start_g2_gesture_acceptance(
        transport=transport,
        terminal=lambda: FakeHermes(),
        trigger=acceptance_trigger(generation=1),
        start_session=lambda _session: True,
        acceptance_armed=True,
        output_directory=tmp_path,
    )

    assert rejection is not None
    assert rejection.state == "failed"


@pytest.mark.asyncio
async def test_g2_gesture_session_ignores_stale_actions_after_reconnect(
    tmp_path: Path,
) -> None:
    events: list[dict[str, object]] = []
    transport = FakeTransport(events)
    session = InAppBrainstormAcceptance(
        transport=transport,
        terminal=FakeHermes(),
        out=tmp_path / "receipt.json",
        diagnostic_events=events,
        trigger=acceptance_trigger(),
        action_timeout_s=1,
        connect_timeout_s=1,
        connection_generation=1,
        evidence_mode="physical_g2",
    )
    session.attach(subscribe_actions=False)
    transport.emit_hello()
    await _wait_for(lambda: len(transport.sent) == 1)

    # A replayed control from a prior authenticated connection is not a
    # wearer action on this post-reconnect Brainstorm surface.
    session.handle_action(BrainstormAction(
        action="begin_topic_capture", connection_generation=0,
    ))
    await asyncio.sleep(0.01)
    assert len(transport.sent) == 1

    transport.action("begin_topic_capture", "brainstorm", "right")
    session.handle_action(BrainstormAction(
        action="begin_topic_capture", connection_generation=1,
    ))
    await _wait_for(lambda: len(transport.sent) == 5)
    await session.close()


@pytest.mark.asyncio
async def test_in_app_controller_refuses_when_a_normal_session_is_active(
    tmp_path: Path,
) -> None:
    events: list[dict[str, object]] = []
    transport = FakeTransport(events)
    completed = asyncio.Event()
    controller = InAppBrainstormAcceptance(
        transport=transport, terminal=FakeHermes(), out=tmp_path / "receipt.json",
        diagnostic_events=events, action_timeout_s=1, connect_timeout_s=1,
        is_isolated=lambda: False, trigger=acceptance_trigger(), connection_generation=1,
        evidence_mode="physical_g2", on_complete=completed.set,
    )
    controller.attach()

    transport.emit_hello()
    await asyncio.wait_for(completed.wait(), timeout=1)
    with pytest.raises(RuntimeError, match="another wearer session is active"):
        await controller.wait()

    assert transport.sent == []
    receipt = json.loads(controller.output_path.read_text())
    assert receipt["result"] == "inconclusive"
    assert receipt["hermesStream"] == {
        "hud": "not_verified", "frameSha256": [],
    }


@pytest.mark.asyncio
async def test_in_app_controller_refuses_to_start_from_non_g2_hello(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    transport = FakeTransport(events)
    controller = InAppBrainstormAcceptance(
        transport=transport, terminal=FakeHermes(), out=tmp_path / "receipt.json",
        diagnostic_events=events, action_timeout_s=0.01, connect_timeout_s=0.01,
        trigger=acceptance_trigger(), connection_generation=1,
        evidence_mode="physical_g2",
    )
    controller.attach()
    for callback in transport.hello_callbacks:
        callback(Hello(sn="sim-not-retained", model="simulator", launch_source="test"))
    await asyncio.sleep(0)
    assert transport.sent == []
    await controller.close()
    assert json.loads(controller.output_path.read_text())["result"] == "inconclusive"


@pytest.mark.asyncio
async def test_in_app_controller_accepts_actions_from_the_live_router_without_a_second_subscription(
    tmp_path: Path,
) -> None:
    events: list[dict[str, object]] = []
    transport = FakeTransport(events)
    controller = InAppBrainstormAcceptance(
        transport=transport, terminal=FakeHermes(), out=tmp_path / "receipt.json",
        diagnostic_events=events, action_timeout_s=1, connect_timeout_s=1,
        trigger=acceptance_trigger(), connection_generation=1,
        evidence_mode="physical_g2",
    )
    controller.attach(subscribe_actions=False)
    assert transport.action_callbacks == []
    transport.emit_hello()
    await _wait_for(lambda: len(transport.sent) == 1)

    transport.events.append({
        "sequence": 2, "recordedAtMs": 2, "type": "brainstorm_action",
        "action": "begin_topic_capture", "surface": "brainstorm", "gesture": "tap",
        "delivery": "sys", "source": "right",
    })
    controller.handle_action(BrainstormAction(
        action="begin_topic_capture", connection_generation=1,
    ))
    await _wait_for(lambda: len(transport.sent) == 5)
    await controller.close()


@pytest.mark.asyncio
async def test_in_app_controller_rejects_loopback_g2_even_when_its_model_is_g2(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    transport = FakeTransport(events)
    transport.physical_g2_connected = False
    controller = InAppBrainstormAcceptance(
        transport=transport, terminal=FakeHermes(), out=tmp_path / "receipt.json",
        diagnostic_events=events, action_timeout_s=0.01, connect_timeout_s=0.01,
        trigger=acceptance_trigger(), connection_generation=1,
        evidence_mode="physical_g2",
    )
    controller.attach()
    transport.emit_hello()
    await asyncio.sleep(0)
    assert transport.sent == []
    await controller.close()
    assert json.loads(controller.output_path.read_text())["result"] == "inconclusive"


def test_g2_gesture_factory_requires_fixed_metadata_and_a_current_g2(
    tmp_path: Path,
) -> None:
    events: list[dict[str, object]] = []
    transport = FakeTransport(events)
    transport.connected = False
    transport.physical_g2_connected = False
    assert make_g2_gesture_acceptance_session(
        transport=transport,
        terminal=lambda: FakeHermes(),
        trigger=acceptance_trigger(),
        output_directory=tmp_path,
        action_timeout_s=1,
        connect_timeout_s=1,
    ) is None

    transport.connected = True
    transport.physical_g2_connected = True
    transport.emit_hello()
    session = make_g2_gesture_acceptance_session(
        transport=transport,
        terminal=lambda: FakeHermes(),
        trigger=acceptance_trigger("ring"),
        output_directory=tmp_path,
        action_timeout_s=1,
        connect_timeout_s=1,
    )
    assert isinstance(session, G2GestureBrainstormAcceptanceSession)

    with pytest.raises(ValueError, match="fixed G2 gesture"):
        make_g2_gesture_acceptance_session(
            transport=transport,
            terminal=lambda: FakeHermes(),
            trigger=BrainstormAction(action="begin_acceptance"),
            output_directory=tmp_path,
        )


def test_g2_gesture_factory_requires_explicit_override_for_unknown_simulator_launch(
    tmp_path: Path,
) -> None:
    events: list[dict[str, object]] = []
    transport = FakeTransport(events)
    transport.physical_g2_connected = False
    transport.emit_hello(launch_source="unknown")

    assert make_g2_gesture_acceptance_session(
        transport=transport,
        terminal=lambda: FakeHermes(),
        trigger=acceptance_trigger(),
        output_directory=tmp_path,
    ) is None
    session = make_g2_gesture_acceptance_session(
        transport=transport,
        terminal=lambda: FakeHermes(),
        trigger=acceptance_trigger(),
        output_directory=tmp_path,
        simulator_override=True,
    )
    assert isinstance(session, G2GestureBrainstormAcceptanceSession)


@pytest.mark.asyncio
async def test_g2_gesture_session_proves_source_reconnect_and_restores_diagnostics(
    tmp_path: Path,
) -> None:
    events: list[dict[str, object]] = []
    transport = FakeTransport(events)
    original_diagnostics = object()
    transport._g2_diagnostics = original_diagnostics
    hermes = FakeHermes()
    transport.emit_hello()
    session = make_g2_gesture_acceptance_session(
        transport=transport,
        terminal=lambda: hermes,
        trigger=acceptance_trigger("ring"),
        output_directory=tmp_path,
        action_timeout_s=1,
        connect_timeout_s=1,
    )
    assert isinstance(session, G2GestureBrainstormAcceptanceSession)

    run_task = asyncio.create_task(session.run())
    await _wait_for(lambda: len(transport.sent) == 1)
    assert transport.sent[-1].activity == "Tap to start the Hermes acceptance run"

    async def gesture(
        action: str, surface: str, source: str, gesture_name: str = "tap",
    ) -> None:
        assert await session.handle_brainstorm_action(BrainstormAction(
            action=action,
            connection_generation=transport.connection_generation,
        ))
        # The production EventRouter sends the bounded action first and then
        # queues its content-free diagnostic on that same WebSocket.  Yield so
        # the controller records its evidence boundary before the trace arrives.
        await asyncio.sleep(0)
        transport.action(action, surface, source, gesture_name)

    await gesture("begin_topic_capture", "brainstorm", "right")
    await _wait_for(lambda: len(transport.sent) == 5)
    await gesture("show_terminal", "brainstorm", "ring")
    await _wait_for(lambda: len(transport.sent) == 6)
    # A replacement before the wearer-visible reconnect instruction cannot
    # satisfy the later reconnect gate.
    transport.emit_hello(new_connection=True)
    await gesture("show_brainstorm", "agent_terminal", "right")
    await _wait_for(lambda: len(transport.sent) == 8)
    # A duplicate hello on the same authenticated socket is not a reconnect.
    transport.emit_hello()
    await asyncio.sleep(0.01)
    assert len(transport.sent) == 8
    transport.emit_hello(new_connection=True)
    await _wait_for(lambda: len(transport.sent) == 9)
    await gesture("cancel", "brainstorm", "left", "double_press")
    await asyncio.wait_for(run_task, timeout=1)

    assert transport.g2_diagnostics is original_diagnostics
    receipts = list(tmp_path.glob("*-physical-g2.json"))
    assert len(receipts) == 1
    retained = receipts[0].read_text()
    receipt = json.loads(retained)
    assert receipt["result"] == "pass"
    assert receipt["startedBy"] == {
        "interface": "g2",
        "modality": "gesture",
        "surface": "brainstorm",
        "source": "ring",
        "gesture": "double_press",
        "delivery": "sys",
    }
    assert "private remote output" not in retained


@pytest.mark.asyncio
async def test_simulator_receipt_is_never_labeled_physical_g2(tmp_path: Path) -> None:
    events: list[dict[str, object]] = []
    transport = FakeTransport(events)
    transport.physical_g2_connected = False
    transport.emit_hello(launch_source="simulator")
    session = make_g2_gesture_acceptance_session(
        transport=transport,
        terminal=lambda: None,
        trigger=acceptance_trigger(),
        output_directory=tmp_path,
        action_timeout_s=1,
        connect_timeout_s=1,
    )
    assert isinstance(session, G2GestureBrainstormAcceptanceSession)

    await session.run()

    receipt_path = next(tmp_path.glob("*-simulator.json"))
    receipt = json.loads(receipt_path.read_text())
    assert receipt["mode"] == "simulator_app_engine"
    assert receipt["result"] != "pass"


@pytest.mark.asyncio
async def test_simulator_can_render_cancel_without_inventing_physical_source(
    tmp_path: Path,
) -> None:
    events: list[dict[str, object]] = []
    transport = FakeTransport(events)
    transport.physical_g2_connected = False
    hermes = FakeHermes()
    transport.emit_hello(launch_source="simulator")
    session = make_g2_gesture_acceptance_session(
        transport=transport,
        terminal=lambda: hermes,
        trigger=acceptance_trigger(),
        output_directory=tmp_path,
        action_timeout_s=1,
        connect_timeout_s=1,
    )
    assert isinstance(session, G2GestureBrainstormAcceptanceSession)

    run_task = asyncio.create_task(session.run())
    await _wait_for(lambda: len(transport.sent) == 1)

    async def gesture(
        action: str,
        surface: str,
        *,
        record_diagnostic: bool = True,
    ) -> None:
        assert await session.handle_brainstorm_action(BrainstormAction(
            action=action,
            connection_generation=transport.connection_generation,
        ))
        await asyncio.sleep(0)
        transport.action(
            action,
            surface,
            "right",
            record_diagnostic=record_diagnostic,
        )

    await gesture("begin_topic_capture", "brainstorm")
    await _wait_for(lambda: len(transport.sent) == 5)
    await gesture("show_terminal", "brainstorm")
    await _wait_for(lambda: len(transport.sent) == 6)
    await gesture("show_brainstorm", "agent_terminal")
    await _wait_for(lambda: len(transport.sent) == 8)
    transport.emit_hello(new_connection=True, launch_source="simulator")
    await _wait_for(lambda: len(transport.sent) == 9)
    await gesture("cancel", "brainstorm", record_diagnostic=False)
    await asyncio.wait_for(run_task, timeout=1)

    assert transport.sent[-1].state == "cancelled"
    receipt = json.loads(next(tmp_path.glob("*-simulator.json")).read_text())
    assert receipt["mode"] == "simulator_app_engine"
    assert receipt["result"] != "pass"
    assert receipt["hermesRun"] == "cancelled"
    assert receipt["g2Reconnect"] == "restored"
    assert not any(
        event.get("action") == "cancel"
        for event in receipt["events"]
        if event.get("type") == "brainstorm_action"
    )
