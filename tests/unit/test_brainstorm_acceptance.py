"""Focused in-engine acceptance controller tests; no Gateway is contacted."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from joat.core.protocol import BrainstormAction, BrainstormHudView, Hello
from joat.services.brainstorm_acceptance import (
    ARM_SCHEMA,
    InAppBrainstormAcceptance,
    consume_arm_request,
    summarize_receipt,
)
from joat.services.g2_diagnostics import TRACE_SCHEMA


class FakeTransport:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self.events = events
        self.connected = True
        self.physical_g2_connected = True
        self.hello: Hello | None = None
        self.sent: list[BrainstormHudView] = []
        self.hello_callbacks: list[Callable[[Hello], None]] = []
        self.action_callbacks: list[Callable[[BrainstormAction], None]] = []

    def on_hello(self, callback: Callable[[Hello], None]) -> None:
        self.hello_callbacks.append(callback)

    def on_brainstorm_action(self, callback: Callable[[BrainstormAction], None]) -> None:
        self.action_callbacks.append(callback)

    async def send(self, view: BrainstormHudView) -> None:
        self.sent.append(view)
        digest = hashlib.sha256(json.dumps(
            view.to_wire(), sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        self.events.append({
            "sequence": len(self.events) + 1,
            "recordedAtMs": len(self.events) + 1,
            "type": "visual_frame",
            "kind": "brainstorm",
            "surface": view.screen,
            "state": view.state,
            "sha256": digest,
        })

    def emit_hello(self) -> None:
        message = Hello(sn="g2-not-retained", model="g2", launch_source="physical")
        self.hello = message
        for callback in self.hello_callbacks:
            callback(message)

    def action(self, action: str, surface: str, source: str, gesture: str = "tap") -> None:
        self.events.append({
            "sequence": len(self.events) + 1,
            "recordedAtMs": len(self.events) + 1,
            "type": "brainstorm_action",
            "action": action,
            "surface": surface,
            "gesture": gesture,
            "delivery": "sys",
            "source": source,
        })
        message = BrainstormAction(action=action)
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


async def _wait_for(predicate: Callable[[], bool]) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
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
        on_complete=completed.set,
    )
    controller.attach()

    transport.emit_hello()
    await _wait_for(lambda: len(transport.sent) == 1)
    assert transport.sent[-1].state == "ready"
    transport.action("begin_topic_capture", "brainstorm", "right")
    await _wait_for(lambda: len(transport.sent) == 5)
    streamed = transport.sent[1:5]
    assert [view.activity for view in streamed] == [
        "Preparing collaborators…",
        "Thinking…",
        "Thinking…private remote output one ",
        "Thinking…private remote output one private remote output two",
    ]
    assert all(view.state == "running" for view in streamed)
    transport.action("show_terminal", "brainstorm", "right")
    await _wait_for(lambda: len(transport.sent) == 6)
    assert transport.sent[-1].screen == "agent_terminal"
    transport.action("show_brainstorm", "agent_terminal", "ring")
    await _wait_for(lambda: len(transport.sent) == 8)
    transport.emit_hello()
    await _wait_for(lambda: len(transport.sent) == 9)
    transport.action("cancel", "brainstorm", "left", "double_press")
    await asyncio.wait_for(completed.wait(), timeout=1)

    receipt = json.loads(out.read_text())
    assert receipt["result"] == "pass"
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
async def test_in_app_controller_refuses_when_a_normal_session_is_active(
    tmp_path: Path,
) -> None:
    events: list[dict[str, object]] = []
    transport = FakeTransport(events)
    completed = asyncio.Event()
    controller = InAppBrainstormAcceptance(
        transport=transport, terminal=FakeHermes(), out=tmp_path / "receipt.json",
        diagnostic_events=events, action_timeout_s=1, connect_timeout_s=1,
        is_isolated=lambda: False, on_complete=completed.set,
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
    controller.handle_action(BrainstormAction(action="begin_topic_capture"))
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
    )
    controller.attach()
    transport.emit_hello()
    await asyncio.sleep(0)
    assert transport.sent == []
    await controller.close()
    assert json.loads(controller.output_path.read_text())["result"] == "inconclusive"


def test_arm_request_is_fixed_owner_only_and_single_use(tmp_path: Path) -> None:
    arm = tmp_path / "arm.json"
    arm.write_text(ARM_SCHEMA + "\n")
    os.chmod(arm, 0o600)
    assert consume_arm_request(arm) is True
    assert not arm.exists()

    arm.write_text("topic must never be accepted\n")
    os.chmod(arm, 0o600)
    assert consume_arm_request(arm) is False
    assert not arm.exists()
