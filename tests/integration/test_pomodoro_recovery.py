"""Real SQLite restart to the existing PanelPublisher wire contract.

The recording transport is a host fixture, not a simulator or glasses receipt.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from joat.agents.feature_tools import feature_tools
from joat.channels.cards import Card
from joat.core.interfaces import GlassesTransport
from joat.core.protocol import HudPanel
from joat.persistence.notes import NoteStore
from joat.persistence.pomodoro import PomodoroCheckpoint, PomodoroStore
from joat.persistence.store import DurableStore
from joat.services.note_commands import NoteCommandController
from joat.services.panel_commands import PomodoroCommands
from joat.services.panels import PanelPublisher
from joat.services.pomodoro import PomodoroService


class RecordingTransport:
    def __init__(self) -> None:
        self.panels: list[HudPanel] = []

    async def send(self, message: object) -> None:
        assert isinstance(message, HudPanel)
        self.panels.append(message)


async def test_restart_and_reconnect_publish_remaining_time_then_accept_current_pause(tmp_path: Path) -> None:
    path = tmp_path / "joat.sqlite3"
    now = [1000.0]
    with DurableStore(path) as durable:
        original = PomodoroService(store=PomodoroStore(durable), clock=lambda: now[0])
        original.start()
        deadline = original.state().ends_at_unix_ms
        await original.aclose()

    now[0] += 125
    with DurableStore(path) as durable:
        recovered = PomodoroService(store=PomodoroStore(durable), clock=lambda: now[0])
        output = RecordingTransport()
        panels = PanelPublisher(cast(GlassesTransport, output), {"pomodoro": recovered})
        try:
            recovered.activate_restored()
            await panels.publish_all()
            wire = output.panels[-1].to_wire()["pomodoro"]
            assert wire == {"phase": "focus", "running": True, "remainingMs": 1_375_000,
                "lengthMs": 1_500_000, "completed": 0, "endsAtUnixMs": deadline}
            now[0] += 5
            await panels.publish_all()  # the ordinary greet/reconnect callback
            assert output.panels[-1].to_wire()["pomodoro"]["remainingMs"] == 1_370_000
            await panels.select("POMODORO", 0, panels.request_id("pomodoro"))
            paused = output.panels[-1].to_wire()["pomodoro"]
            assert paused["running"] is False and paused["remainingMs"] == 1_370_000
            assert "endsAtUnixMs" not in paused
        finally:
            await recovered.aclose()
    now[0] += 100_000
    with DurableStore(path) as durable:
        again = PomodoroService(store=PomodoroStore(durable), clock=lambda: now[0])
        try:
            again.activate_restored()
            assert again.state().to_wire() == paused
        finally:
            await again.aclose()


async def test_failed_durable_changes_reach_voice_tool_and_panel_without_success(tmp_path: Path) -> None:
    class RejectingStore(PomodoroStore):
        reject = True

        def save(self, checkpoint: PomodoroCheckpoint) -> None:
            if self.reject:
                raise OSError("fixture storage unavailable")
            super().save(checkpoint)

    class UnusedOpener:
        async def open(self, name: str, *, request_id: str | None = None) -> None:
            raise AssertionError("timer tools do not dispatch capability command strings")

    with DurableStore(tmp_path / "joat.sqlite3") as durable:
        store = RejectingStore(durable)
        cards: list[Card] = []
        timer = PomodoroService(store=store, clock=lambda: 1000.0, notify=cards.append)
        output = RecordingTransport()
        panels = PanelPublisher(cast(GlassesTransport, output), {"pomodoro": timer})
        commands = PomodoroCommands(timer, panels)
        tools = {tool.name: tool for tool in feature_tools(capabilities=UnusedOpener(),
            notes=NoteCommandController(NoteStore(durable)), pomodoro=timer, panels=panels)}
        try:
            timer.activate_restored()
            reply = await commands.handle_command("start a pomodoro")
            assert reply is not None and "failed" in reply.lower()
            assert timer.phase == "idle" and store.load() is None
            assert output.panels[-1].to_wire()["headline"] == "Timer change failed"
            assert cards[-1].event_class == "pomodoro.error"

            store.reject = False
            started = await commands.handle_command("start a pomodoro")
            assert started is not None and started.startswith("Focus block")
            original = timer.state()
            store.reject = True
            reply = await tools["control_timer"].run({"action": "pause"})
            assert "failed" in reply.lower() and "Resumed" not in reply
            assert timer.state() == original
            await panels.select("POMODORO", 0, panels.request_id("pomodoro"))
            wire = output.panels[-1].to_wire()
            assert wire["headline"] == "Timer change failed"
            assert wire["empty"] == "Timer change failed. Previous state kept. Retry or reset."
            assert wire["pomodoro"] == original.to_wire()
            assert all(card.event_class == "pomodoro.error" for card in cards)
        finally:
            await timer.aclose()
