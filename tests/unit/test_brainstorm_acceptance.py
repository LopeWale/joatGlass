"""Focused in-engine acceptance controller tests; no Gateway is contacted."""

from __future__ import annotations

import asyncio
import json
import stat
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from joat.core.protocol import BrainstormAction, BrainstormHudView, Hello
from joat.services.brainstorm_acceptance import InAppBrainstormAcceptance


class FakeTransport:
    def __init__(self, events: list[dict[str, object]]) -> None:
        self.events = events
        self.sent: list[BrainstormHudView] = []
        self.hello_callbacks: list[Callable[[Hello], None]] = []
        self.action_callbacks: list[Callable[[BrainstormAction], None]] = []

    def on_hello(self, callback: Callable[[Hello], None]) -> None:
        self.hello_callbacks.append(callback)

    def on_brainstorm_action(self, callback: Callable[[BrainstormAction], None]) -> None:
        self.action_callbacks.append(callback)

    async def send(self, view: BrainstormHudView) -> None:
        self.sent.append(view)
        self.events.append({
            "sequence": len(self.events) + 1,
            "recordedAtMs": len(self.events) + 1,
            "type": "visual_frame",
            "kind": "brainstorm",
            "surface": view.screen,
            "state": view.state,
            "sha256": f"{len(self.events) + 10:064x}",
        })

    def hello(self) -> None:
        message = Hello(sn="g2-not-retained", model="g2", launch_source="physical")
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
            yield "private remote output must not enter evidence"
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
async def test_in_app_controller_drives_real_g2_contract_and_discards_hermes_text(tmp_path: Path) -> None:
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

    transport.hello()
    await _wait_for(lambda: len(transport.sent) == 1)
    assert transport.sent[-1].state == "ready"
    transport.action("begin_topic_capture", "brainstorm", "right")
    await _wait_for(lambda: len(transport.sent) == 2)
    assert transport.sent[-1].state == "running"
    transport.action("show_terminal", "brainstorm", "right")
    await _wait_for(lambda: len(transport.sent) == 3)
    assert transport.sent[-1].screen == "agent_terminal"
    transport.action("show_brainstorm", "agent_terminal", "ring")
    await _wait_for(lambda: len(transport.sent) == 5)
    transport.hello()
    await _wait_for(lambda: len(transport.sent) == 6)
    transport.action("cancel", "brainstorm", "left", "double_press")
    await asyncio.wait_for(completed.wait(), timeout=1)

    receipt = json.loads(out.read_text())
    assert receipt["result"] == "pass"
    assert receipt["hermesRun"] == "cancelled"
    assert receipt["g2Reconnect"] == "restored"
    assert "private remote output" not in out.read_text()
    assert all("prompt" not in event for event in receipt["events"])
    assert hermes.prompts and await hermes.cancelled.wait()
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


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
