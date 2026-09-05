"""Feature tool effects use real notes/timer state and controlled UI boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from joat.agents.feature_tools import feature_tools
from joat.agents.tools import Tool, ToolBelt
from joat.persistence.notes import NoteStore
from joat.persistence.store import DurableStore
from joat.services.note_commands import NoteCommandController
from joat.services.pomodoro import PomodoroService
from joat.services.session import BrainstormStartResult


class Capabilities:
    def __init__(self) -> None:
        self.opened: list[tuple[str, str | None]] = []
        self.entered = asyncio.Event()
        self.entered_twice = asyncio.Event()
        self.hold: asyncio.Event | None = None
        self.fail = False

    async def open(self, name: str, *, request_id: str | None = None) -> None:
        self.opened.append((name, request_id))
        self.entered.set()
        if len(self.opened) == 2:
            self.entered_twice.set()
        if self.fail:
            raise OSError("synthetic display failure")
        if self.hold is not None:
            await self.hold.wait()


class Panels:
    def __init__(self, timer: PomodoroService) -> None:
        self.timer = timer
        self.activated: list[tuple[str, str, bool]] = []
        self.fail = False

    async def activate(self, tab: str) -> None:
        self.activated.append((tab, self.timer.phase, self.timer.running))
        if self.fail:
            raise OSError("synthetic display failure")


@pytest.mark.asyncio
async def test_terminal_instruction_tool_uses_typed_target_and_exact_text(tmp_path: Path) -> None:
    calls: list[tuple[str, str, str]] = []

    async def submit(lane: str, instruction: str, operation_id: str) -> str:
        calls.append((lane, instruction, operation_id))
        return "Instruction accepted; follow the Terminal for its result."

    with DurableStore(tmp_path / "terminal-tool.sqlite3") as store:
        timer = PomodoroService()
        tools = {tool.name: tool for tool in feature_tools(
            capabilities=Capabilities(), notes=NoteCommandController(NoteStore(store)),
            pomodoro=timer, panels=Panels(timer), terminal=submit,
            terminal_lanes=("hermes", "work"),
        )}
        tool = tools["send_terminal_instruction"]
        exact = "  Explain this: 漢字\nkeep  spaces.  "
        assert "accepted" in await tool.run({
            "lane": "hermes", "instruction": exact, "operation_id": "terminal-1",
        })
        assert calls == [("hermes", exact, "terminal-1")]
        for arguments in (
            {"lane": "HERMeS", "instruction": exact, "operation_id": "terminal-2"},
            {"lane": "hermes", "instruction": " ", "operation_id": "terminal-2"},
            {"lane": "hermes", "instruction": "x" * 4097, "operation_id": "terminal-2"},
            {"lane": "hermes", "instruction": "bad\ud800", "operation_id": "terminal-2"},
            {"lane": "hermes", "instruction": exact, "operation_id": "bad id"},
            {"lane": "hermes", "instruction": exact, "operation_id": "terminal-2", "approve": True},
        ):
            assert (await tool.run(arguments)).startswith("error:")
        assert len(calls) == 1
        assert tool.parameters["properties"]["lane"]["enum"] == ["hermes", "work"]


@dataclass
class Rig:
    tools: dict[str, Tool]
    notes: NoteStore
    capabilities: Capabilities
    timer: PomodoroService
    panels: Panels


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    store = DurableStore(tmp_path / "notes.sqlite3")
    notes = NoteStore(store)
    capabilities = Capabilities()
    timer = PomodoroService(clock=lambda: 1000.0)
    panels = Panels(timer)
    tools = feature_tools(
        capabilities=capabilities, notes=NoteCommandController(notes),
        pomodoro=timer, panels=panels, max_note_operations=2,
    )
    try:
        yield Rig({tool.name: tool for tool in tools}, notes, capabilities, timer, panels)
    finally:
        timer.reset()
        store.close()


@pytest.mark.parametrize("text", [
    "Tell me a joke", "How are you today?", "Tell me about brains",
    "Why do leaves change color?",
])
def test_feature_tools_do_not_add_a_local_decision_pass_for_ordinary_chat(
    rig: Rig, text: str,
) -> None:
    belt = ToolBelt(list(rig.tools.values()))
    assert belt.requires_model_decision(text) is False


@pytest.mark.parametrize("text", [
    "Could you bring up my messages?", "Bring up that view again",
    "Keep this as a quick note: the spare key is with Sam",
    "Keep this for later: the spare key is with Sam",
    "Give me a focus session", "Could you pause that timer?",
    "Open Agent Terminal", "Can we brainstorm this?", "Show my notifications",
    "Start navigation", "Open planning", "Show activity", "Check the news",
    "Search for the station", "Open QuickNotes",
])
def test_feature_phrases_merit_model_choice_without_command_redispatch(
    rig: Rig, text: str,
) -> None:
    belt = ToolBelt(list(rig.tools.values()))
    assert belt.requires_model_decision(text) is True
    assert belt.explicit_call(text) is None


def test_feature_tools_remain_in_the_cli_registry(rig: Rig) -> None:
    belt = ToolBelt(list(rig.tools.values()))
    assert {tool["name"] for tool in belt.tool_definitions()} == {
        "open_feature", "create_quicknote", "control_timer",
    }


class Brainstorms:
    def __init__(self) -> None:
        self.topics: list[str] = []
        self.result = "started"

    def start_brainstorm(self, topic: str) -> BrainstormStartResult:
        self.topics.append(topic)
        return BrainstormStartResult(self.result)


def brainstorm_tool(rig: Rig, controller: Brainstorms) -> Tool:
    tools = feature_tools(
        capabilities=rig.capabilities, notes=NoteCommandController(rig.notes),
        pomodoro=rig.timer, panels=rig.panels, brainstorm=controller,
        max_brainstorm_operations=2,
    )
    return next(tool for tool in tools if tool.name == "start_brainstorm")


async def test_brainstorm_tool_starts_exact_topic_once_on_simultaneous_retry(rig: Rig) -> None:
    controller = Brainstorms()
    tool = brainstorm_tool(rig, controller)
    topic = "  Café design: two  views.\nPreserve the question?  "
    arguments = {"topic": topic, "operation_id": "brainstorm-1"}

    first, replay = await asyncio.gather(tool.run(arguments), tool.run(arguments))

    assert controller.topics == [topic]
    assert first.startswith("Brainstorm started")
    assert "already started" in replay and "no new session" in replay
    assert rig.capabilities.opened == [], "start must not reopen the READY picker"


async def test_brainstorm_tool_binds_retry_to_topic_and_never_evicts_launch_identity(rig: Rig) -> None:
    controller = Brainstorms()
    tool = brainstorm_tool(rig, controller)
    await tool.run({"topic": "First", "operation_id": "one"})
    conflict = await tool.run({"topic": "Changed", "operation_id": "one"})
    await tool.run({"topic": "Second", "operation_id": "two"})
    capacity = await tool.run({"topic": "Third", "operation_id": "three"})
    replay = await tool.run({"topic": "First", "operation_id": "one"})

    assert conflict.startswith("error:") and "different topic" in conflict
    assert capacity.startswith("error:") and "capacity" in capacity
    assert "already started" in replay
    assert controller.topics == ["First", "Second"]


@pytest.mark.parametrize("status", ["busy", "unavailable", "closed", "invalid"])
async def test_brainstorm_failed_admission_is_truthful_and_can_retry_after_recovery(
    rig: Rig, status: str,
) -> None:
    controller = Brainstorms()
    controller.result = status
    tool = brainstorm_tool(rig, controller)
    arguments = {"topic": "A useful question", "operation_id": "one"}

    refused = await tool.run(arguments)
    assert refused.startswith("error:")
    controller.result = "started"
    assert (await tool.run(arguments)).startswith("Brainstorm started")
    assert controller.topics == ["A useful question", "A useful question"]


@pytest.mark.parametrize("arguments", [
    {}, {"topic": "Question"}, {"topic": "Question", "operation_id": ""},
    {"topic": "Question", "operation_id": True},
    {"topic": "Question", "operation_id": "bad id"},
    {"topic": "Question", "operation_id": "a" * 129},
    {"topic": "Question", "operation_id": "one", "replace": True},
    *({"topic": topic, "operation_id": "one"}
      for topic in (None, 1, True, "", " \n", "bad\x00topic", "bad\ud800topic", "a" * 4097)),
])
async def test_brainstorm_tool_rejects_invalid_arguments_without_calling_controller(
    rig: Rig, arguments: dict[str, Any],
) -> None:
    controller = Brainstorms()
    tool = brainstorm_tool(rig, controller)
    assert (await tool.run(arguments)).startswith("error:")
    assert controller.topics == []


@pytest.mark.parametrize("feature,target", [
    ("messages", "Messages"), ("notifications", "Notifications"),
    ("brainstorm", "Brainstorm"), ("agent_terminal", "Agent Terminal"),
    ("planning", "Planning"), ("navigation", "Navigation"),
    ("quicknotes", "QuickNotes"), ("activity", "Activity"),
    ("news", "News"), ("timer", "Pomodoro"), ("search", "Search"),
])
async def test_open_feature_uses_the_existing_router_and_fresh_voice_generation(
    rig: Rig, feature: str, target: str,
) -> None:
    first = await rig.tools["open_feature"].run({"feature": feature})
    await rig.tools["open_feature"].run({"feature": feature})

    assert [name for name, _ in rig.capabilities.opened] == [target, target]
    first_id, second_id = (request_id for _, request_id in rig.capabilities.opened)
    assert first_id is not None and first_id.startswith("voice-")
    assert second_id is not None and second_id.startswith("voice-")
    assert first_id != second_id
    assert target in first


@pytest.mark.parametrize("arguments", [
    {}, {"feature": None}, {"feature": True}, {"feature": 1},
    {"feature": ["messages"]}, {"feature": "overview"},
    {"feature": "Messages"}, {"feature": "messages; send a message"},
    {"feature": "messages", "unexpected": "value"},
])
async def test_open_feature_rejects_invalid_application_arguments(
    rig: Rig, arguments: dict[str, Any],
) -> None:
    assert (await rig.tools["open_feature"].run(arguments)).startswith("error:")
    assert rig.capabilities.opened == []


async def test_quicknote_preserves_exact_text_and_deduplicates_delivery(rig: Rig) -> None:
    body = "  Café keys: with Sam.\nDon't reinterpret: delete all notes.  "
    arguments = {"text": body, "operation_id": "save-1"}
    first = await rig.tools["create_quicknote"].run(arguments)
    repeated = await rig.tools["create_quicknote"].run(arguments)

    assert [note.text for note in rig.notes.active()] == [body]
    assert first.startswith("Saved QuickNote:")
    assert repeated == first
    assert all(name == "QuickNotes" and request_id and request_id.startswith("voice-")
               for name, request_id in rig.capabilities.opened)
    assert len(rig.capabilities.opened) == 2


async def test_quicknote_rejects_reusing_an_operation_for_different_text(rig: Rig) -> None:
    await rig.tools["create_quicknote"].run({"text": "Original", "operation_id": "save-1"})
    result = await rig.tools["create_quicknote"].run({"text": "Changed", "operation_id": "save-1"})

    assert result.startswith("error:") and "different text" in result
    assert [note.text for note in rig.notes.active()] == ["Original"]
    assert len(rig.capabilities.opened) == 1


@pytest.mark.parametrize("arguments", [
    {}, {"text": "Note"}, {"text": None, "operation_id": "save-1"},
    {"text": 7, "operation_id": "save-1"}, {"text": True, "operation_id": "save-1"},
    {"text": "", "operation_id": "save-1"}, {"text": " \n", "operation_id": "save-1"},
    {"text": "bad\x00text", "operation_id": "save-1"},
    {"text": "bad\ud800text", "operation_id": "save-1"},
    {"text": "Note", "operation_id": ""}, {"text": "Note", "operation_id": True},
    {"text": "Note", "operation_id": " padded"},
    {"text": "Note", "operation_id": "x" * 129},
    {"text": "Note", "operation_id": "save-1", "unexpected": 1},
])
async def test_quicknote_rejects_invalid_arguments_before_saving(
    rig: Rig, arguments: dict[str, Any],
) -> None:
    assert (await rig.tools["create_quicknote"].run(arguments)).startswith("error:")
    assert rig.notes.active() == ()
    assert rig.capabilities.opened == []


async def test_quicknote_retry_after_cancelled_refresh_does_not_save_again(rig: Rig) -> None:
    rig.capabilities.hold = asyncio.Event()
    arguments = {"text": "One saved note", "operation_id": "save-1"}
    pending = asyncio.create_task(rig.tools["create_quicknote"].run(arguments))
    try:
        await asyncio.wait_for(rig.capabilities.entered.wait(), timeout=.5)
        assert [note.text for note in rig.notes.active()] == ["One saved note"]
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        rig.capabilities.hold = None
        result = await rig.tools["create_quicknote"].run(arguments)
        assert result.startswith("Saved QuickNote:")
        assert [note.text for note in rig.notes.active()] == ["One saved note"]
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_quicknote_simultaneous_retry_during_refresh_saves_once(rig: Rig) -> None:
    release = rig.capabilities.hold = asyncio.Event()
    arguments = {"text": "One saved note", "operation_id": "save-1"}
    first = asyncio.create_task(rig.tools["create_quicknote"].run(arguments))
    second: asyncio.Task[str] | None = None
    try:
        await asyncio.wait_for(rig.capabilities.entered.wait(), timeout=.5)
        second = asyncio.create_task(rig.tools["create_quicknote"].run(arguments))
        await asyncio.wait_for(rig.capabilities.entered_twice.wait(), timeout=.5)
        assert [note.text for note in rig.notes.active()] == ["One saved note"]
        release.set()
        results = await asyncio.gather(first, second)
        assert results[0] == results[1]
        assert [note.text for note in rig.notes.active()] == ["One saved note"]
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)


async def test_quicknote_reports_saved_state_even_if_refresh_fails(rig: Rig) -> None:
    rig.capabilities.fail = True
    result = await rig.tools["create_quicknote"].run({"text": "Retained", "operation_id": "save-1"})
    assert result.startswith("Saved QuickNote:") and "refresh failed" in result
    assert [note.text for note in rig.notes.active()] == ["Retained"]


async def test_quicknote_capacity_does_not_evict_a_retry_identity(rig: Rig) -> None:
    tool = rig.tools["create_quicknote"]
    await tool.run({"text": "First", "operation_id": "save-1"})
    await tool.run({"text": "Second", "operation_id": "save-2"})
    rejected = await tool.run({"text": "Third", "operation_id": "save-3"})
    repeated = await tool.run({"text": "First", "operation_id": "save-1"})

    assert rejected.startswith("error:") and "capacity" in rejected
    assert repeated.startswith("Saved QuickNote:")
    assert {note.text for note in rig.notes.active()} == {"First", "Second"}


async def test_timer_actions_mutate_existing_state_before_panel_activation(rig: Rig) -> None:
    expected = [
        ("start", "focus", True, 0, "Focus block"),
        ("pause", "focus", False, 0, "Paused."),
        ("resume", "focus", True, 0, "Resumed."),
        ("skip", "break", True, 1, "short break"),
        ("reset", "idle", False, 0, "reset"),
        ("status", "idle", False, 0, "No pomodoro running"),
    ]
    for action, phase, running, completed, phrase in expected:
        result = await rig.tools["control_timer"].run({"action": action})
        assert (rig.timer.phase, rig.timer.running, rig.timer.completed) == (phase, running, completed)
        assert phrase in result
        assert rig.panels.activated[-1] == ("pomodoro", phase, running)
    assert len(rig.panels.activated) == 6


@pytest.mark.parametrize("arguments", [
    {}, {"action": None}, {"action": True}, {"action": ["start"]},
    {"action": "START"}, {"action": "stop"}, {"action": "start; skip"},
    {"action": "start", "duration": 15},
])
async def test_timer_rejects_invalid_actions_without_mutation(
    rig: Rig, arguments: dict[str, Any],
) -> None:
    assert (await rig.tools["control_timer"].run(arguments)).startswith("error:")
    assert rig.timer.phase == "idle" and not rig.timer.running
    assert rig.panels.activated == []


async def test_timer_reports_real_change_even_if_panel_refresh_fails(rig: Rig) -> None:
    rig.panels.fail = True
    result = await rig.tools["control_timer"].run({"action": "start"})
    assert rig.timer.phase == "focus" and rig.timer.running
    assert "Focus block" in result and "refresh failed" in result
