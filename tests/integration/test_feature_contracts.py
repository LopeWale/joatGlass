"""The CLI MCP door executes the same note/timer controllers as the UI.

Only network peers and glasses output are fixtures. SQLite, feature routing,
tool execution and timer state are production components. This is not a
model-decision, simulator or physical-glasses acceptance test.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

from joat.agents.feature_tools import feature_tools
from joat.agents.message_tools import message_tools
from joat.agents.tools import ToolBelt
from joat.channels.cards import CardLog
from joat.core.interfaces import GlassesTransport, ReplyEngine
from joat.core.protocol import AgentTerminalHudView, BrainstormHudView, HudList, HudPanel, HudRebuild
from joat.persistence.notes import NoteStore
from joat.persistence.store import DurableStore
from joat.services.agent_endpoint import AgentEndpoint
from joat.services.agent_terminal import AgentTerminalRoster, TerminalFrame
from joat.services.capabilities import CapabilityRouter
from joat.services.hud import HudWriter
from joat.services.message_reply import MessageReplyController
from joat.services.note_commands import NoteCommandController
from joat.services.panels import PanelPublisher
from joat.services.pomodoro import PomodoroService
from joat.services.roundtable import Participant, RoundTable
from joat.services.session import DebateSession, SessionManager
from joat.services.sinks import HudSink
from joat.services.status import StatusPublisher
from joat.services.terminal_opening import TerminalOpeningController


class UnusedEngine(ReplyEngine):
    async def reply(self, text: str) -> str | None:
        raise AssertionError("A tool call must not start a second model")

    async def stream(self, text: str) -> AsyncIterator[str]:
        raise AssertionError("A tool call must not start a second model")
        yield ""  # pragma: no cover


class RecordingTransport:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: object) -> None:
        self.sent.append(message)


async def rpc(port: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
    }).encode()
    writer.write((
        "POST /agent/mcp HTTP/1.1\r\nHost: localhost\r\n"
        "Authorization: Bearer fixture-token\r\n"
        f"Content-Length: {len(payload)}\r\n\r\n"
    ).encode() + payload)
    await writer.drain()
    try:
        raw = await asyncio.wait_for(reader.read(), timeout=2)
    finally:
        writer.close()
        await writer.wait_closed()
    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200")
    return cast(dict[str, Any], json.loads(body))["result"]


async def test_mcp_feature_calls_reach_real_persistence_router_and_timer(tmp_path: Path) -> None:
    output = RecordingTransport()
    transport = cast(GlassesTransport, output)
    timer = PomodoroService(clock=lambda: 1000.0)
    panels = PanelPublisher(transport, {"pomodoro": timer})
    belt = ToolBelt()
    # Composition gives the reply engine and endpoint the registry before
    # controllers exist. Late registration must reach this same instance.
    endpoint = AgentEndpoint(UnusedEngine(), "fixture-token", port=0, tools=belt)
    with DurableStore(tmp_path / "notes.sqlite3") as durable:
        notes = NoteStore(durable)
        capabilities = CapabilityRouter(
            hud=HudWriter(transport), transport=transport,
            status=StatusPublisher(tmp_path / "status.json"), messages=None,
            cards=CardLog(tmp_path / "cards.jsonl"), notes=notes, panels=panels,
        )
        for tool in feature_tools(
            capabilities=capabilities, notes=NoteCommandController(notes),
            pomodoro=timer, panels=panels,
        ):
            belt.register(tool)
        await endpoint.start()
        try:
            listed = await rpc(endpoint.port, "tools/list")
            assert {tool["name"] for tool in listed["tools"]} == {
                "open_feature", "create_quicknote", "control_timer",
            }
            text = "  Spare key with Sam.\nKeep punctuation, café and  two spaces.  "
            params = {"name": "create_quicknote", "arguments": {
                "text": text, "operation_id": "fixture-note-1",
            }}
            first = await rpc(endpoint.port, "tools/call", params)
            retry = await rpc(endpoint.port, "tools/call", params)
            assert first == retry and first["isError"] is False
            assert [note.text for note in notes.active()] == [text]
            lists = [message for message in output.sent if isinstance(message, HudList)]
            assert lists[-1].title == "QUICKNOTES"
            assert lists[-1].request_id and lists[-1].request_id.startswith("voice-")
            assert lists[-1].to_wire()["items"], "saved note must reach the UI transport"

            for action, running in (("start", True), ("pause", False)):
                result = await rpc(endpoint.port, "tools/call", {
                    "name": "control_timer", "arguments": {"action": action},
                })
                assert result["isError"] is False
                assert timer.phase == "focus" and timer.running is running
                pushed = [message for message in output.sent if isinstance(message, HudPanel)][-1]
                assert pushed.activate is True and pushed.tab == "pomodoro"
                assert pushed.pomodoro and pushed.pomodoro.running is running

            opened = await rpc(endpoint.port, "tools/call", {
                "name": "open_feature", "arguments": {"feature": "quicknotes"},
            })
            assert opened["isError"] is False
            assert isinstance(output.sent[-1], HudList)
            assert output.sent[-1].title == "QUICKNOTES"
            rejected = await rpc(endpoint.port, "tools/call", {
                "name": "create_quicknote", "arguments": {
                    "text": "Do not overwrite", "operation_id": "fixture-note-1",
                },
            })
            assert rejected["isError"] is True
            assert [note.text for note in notes.active()] == [text]
        finally:
            timer.reset()
            await endpoint.stop()
    with DurableStore(tmp_path / "notes.sqlite3") as reopened:
        assert [note.text for note in NoteStore(reopened).active()] == [text]


async def test_mcp_terminal_instruction_reaches_captured_lane_and_bound_result(tmp_path: Path) -> None:
    output = RecordingTransport()
    transport = cast(GlassesTransport, output)
    hud = HudWriter(transport)
    calls: list[str] = []
    returned = asyncio.Event()

    class Terminal:
        capture_context_generation = 7

        async def stream(self, text: str) -> AsyncIterator[str]:
            raise AssertionError("The model tool must preserve the selected context")
            yield ""

        async def stream_captured(self, text: str, *, context_generation: int) -> AsyncIterator[str]:
            assert context_generation == 7
            calls.append(text)
            yield "Keep one clear work surface."
            returned.set()

    async def paint(frame: TerminalFrame, request_id: str | None, selection_id: str | None) -> None:
        await hud.show_agent_terminal(AgentTerminalHudView(
            state=frame.state, focus=frame.topic, lanes=frame.lanes,
            activity="\n".join(frame.lines), opening_request_id=request_id,
            opening_selection_id=selection_id,
        ))

    roster = AgentTerminalRoster([("hermes", Terminal())], paint=lambda frame: opening.paint(frame))
    opening = TerminalOpeningController(roster, paint=paint, connection_generation=lambda: "fixture-connection")
    timer = PomodoroService()
    panels = PanelPublisher(transport, {"pomodoro": timer})
    belt = ToolBelt()
    endpoint = AgentEndpoint(UnusedEngine(), "fixture-token", port=0, tools=belt)
    with DurableStore(tmp_path / "notes.sqlite3") as durable:
        notes = NoteStore(durable)
        router = CapabilityRouter(
            hud=hud, transport=transport, status=StatusPublisher(tmp_path / "status.json"),
            messages=None, cards=CardLog(tmp_path / "cards.jsonl"), notes=notes,
            terminal_rows=roster.picker_rows, terminal_open=opening.open,
            terminal_current=lambda: roster.focused, terminal_busy=lambda: roster.busy,
        )
        for tool in feature_tools(
            capabilities=router, notes=NoteCommandController(notes), pomodoro=timer, panels=panels,
            terminal=lambda lane, instruction, operation_id: opening.submit_instruction(
                lane, instruction, operation_id, router.prepare_terminal_instruction,
            ), terminal_lanes=roster.names(),
        ):
            belt.register(tool)
        await endpoint.start()
        try:
            exact = "  Give one idea: café tools, 漢字, and  space.  "
            params = {"name": "send_terminal_instruction", "arguments": {
                "lane": "hermes", "instruction": exact, "operation_id": "once",
            }}
            result = await rpc(endpoint.port, "tools/call", params)
            assert result["isError"] is False
            await asyncio.wait_for(returned.wait(), timeout=1)
            async with asyncio.timeout(1):
                while roster.busy:
                    await asyncio.sleep(0)
            assert (await rpc(endpoint.port, "tools/call", params))["isError"] is False
            assert calls == [exact]
            entry = next(message for message in output.sent if isinstance(message, HudList))
            frames = [message for message in output.sent if isinstance(message, AgentTerminalHudView)]
            assert entry.request_id and entry.request_id.startswith("voice-")
            assert all((frame.opening_request_id, frame.opening_selection_id)
                       == (entry.request_id, entry.request_id) for frame in frames)
            assert any("Keep one clear work surface." in frame.activity for frame in frames)
        finally:
            await endpoint.stop()
            await roster.cancel("hermes")
            timer.reset()


async def test_mcp_message_clarification_returns_before_review_and_submits_once() -> None:
    confirmation_started, confirmation_release, submitted = (asyncio.Event() for _ in range(3))
    prompts: list[str] = []
    sends: list[tuple[str, str]] = []
    lookups: list[str] = []

    class Sender:
        async def send(self, recipient: str, text: str) -> None:
            sends.append((recipient, text))
            submitted.set()

    async def confirm(_action_id: str, prompt: str, _timeout: float, **_kwargs: Any) -> bool:
        prompts.append(prompt)
        confirmation_started.set()
        await confirmation_release.wait()
        return True

    def contacts(name: str) -> list[tuple[str, str]]:
        lookups.append(name)
        return [("Dele", "fixture-recipient")] if name == "Dele" else []

    replies = MessageReplyController(Sender(), confirm, contact_search=contacts)
    belt = ToolBelt(message_tools(replies), timeout_s=0.03)
    endpoint = AgentEndpoint(UnusedEngine(), "fixture-token", port=0, tools=belt)
    await endpoint.start()

    async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await rpc(endpoint.port, "tools/call", {"name": name, "arguments": arguments})
        assert result["isError"] is False
        return cast(dict[str, Any], json.loads(result["content"][0]["text"]))

    try:
        request = {"mode": "new", "action_id": "fixture-message", "recipient_name": "my wife"}
        first = await call("prepare_message", request)
        assert first["state"] == "awaiting_input" and first["needs_input"] == "recipient"
        assert lookups == [] and sends == []
        recipient = await call("provide_message_input", {"action_id": first["action_id"], "recipient_name": "Dele"})
        assert recipient["needs_input"] == "body" and lookups == ["Dele"]
        body = "  I'll be home at six.  "
        pending = await call("provide_message_input", {"action_id": first["action_id"], "body": body})
        await asyncio.wait_for(confirmation_started.wait(), timeout=0.3)
        # The RPC and ToolBelt have returned; the review is still pending.
        assert pending["state"] == "awaiting_confirmation" and sends == []
        assert prompts == [f"Send to Dele? “{body}”"]
        assert (await call("message_action_status", {"action_id": first["action_id"]}))["state"] == "awaiting_confirmation"
        confirmation_release.set()
        await asyncio.wait_for(submitted.wait(), timeout=0.3)
        final = await call("message_action_status", {"action_id": first["action_id"]})
        assert final["state"] == "sent" and final["verification_level"] == "delivery_unverified"
        assert (await call("prepare_message", request))["state"] == "sent"
        assert len(prompts) == 1 and sends == [("fixture-recipient", body)]
    finally:
        confirmation_release.set()
        await endpoint.stop()
        await replies.aclose()


async def test_mcp_brainstorm_topic_reaches_both_collaborators_and_live_hud(tmp_path: Path) -> None:
    """Real HTTP, tool registry, session arbitration, debate and HUD producer.

    Only collaborator responses and glasses transport are controlled. This
    proves the topic path, not real provider availability or lit pixels.
    """
    output = RecordingTransport()
    transport = cast(GlassesTransport, output)
    hud = HudWriter(transport)
    timer = PomodoroService(clock=lambda: 1000.0)
    panels = PanelPublisher(transport, {"pomodoro": timer})
    topics: list[str] = []
    prompts: dict[str, list[str]] = {"codex": [], "claude": []}
    release = asyncio.Event()
    settled = asyncio.Event()

    def collaborator(name: str, answer: str):
        async def stream(prompt: str) -> AsyncIterator[str]:
            prompts[name].append(prompt)
            await release.wait()
            yield answer
        return stream

    def factory(topic: str) -> DebateSession:
        topics.append(topic)
        return DebateSession(topic, RoundTable([
            Participant("codex", "collaborator", collaborator("codex", "GIST: Sort by use.\nKeep daily tools close.")),
            Participant("claude", "collaborator", collaborator("claude", "GIST: Leave a clear zone.\nKeep one work surface free.")),
        ]), HudSink(hud), None, max_rounds=1)

    sessions = SessionManager(
        hud, factory, brainstorm_available=lambda: True,
        on_state=lambda state: settled.set() if state == "settled" else None,
    )
    belt = ToolBelt()
    endpoint = AgentEndpoint(UnusedEngine(), "fixture-token", port=0, tools=belt)
    with DurableStore(tmp_path / "notes.sqlite3") as durable:
        notes = NoteStore(durable)
        capabilities = CapabilityRouter(
            hud=hud, transport=transport,
            status=StatusPublisher(tmp_path / "status.json"), messages=None,
            cards=CardLog(tmp_path / "cards.jsonl"), notes=notes, panels=panels,
        )
        for tool in feature_tools(
            capabilities=capabilities, notes=NoteCommandController(notes),
            pomodoro=timer, panels=panels, brainstorm=sessions,
        ):
            belt.register(tool)
        await endpoint.start()
        try:
            listed = await rpc(endpoint.port, "tools/list")
            assert "start_brainstorm" in {tool["name"] for tool in listed["tools"]}
            topic = "  Organize a desk: café notes, tools, and  clear space.\nTwo options.  "
            call = {"name": "start_brainstorm", "arguments": {
                "topic": topic, "operation_id": "topic-once",
            }}
            first = await rpc(endpoint.port, "tools/call", call)
            assert first["isError"] is False
            assert topics == [topic] and sessions.active
            assert (await rpc(endpoint.port, "tools/call", call))["isError"] is False
            assert topics == [topic]
            busy = await rpc(endpoint.port, "tools/call", {
                "name": "start_brainstorm", "arguments": {
                    "topic": "A different topic", "operation_id": "do-not-replace",
                },
            })
            assert busy["isError"] is True and topics == [topic]
            release.set()
            await asyncio.wait_for(settled.wait(), timeout=1)
            assert all(len(value) == 1 and topic in value[0] for value in prompts.values())
            frames = [message for message in output.sent if isinstance(message, BrainstormHudView)]
            entries = [message for message in output.sent if isinstance(message, HudRebuild)]
            assert len(entries) == 1 and entries[0].reason == "brainstorm"
            assert output.sent.index(entries[0]) < output.sent.index(frames[0]), (
                "the initial entry must release menu focus before streaming frames"
            )
            assert frames[0].topic == topic and frames[0].state == "running"
            assert frames[0].collaborators == ["codex", "claude"]
            assert any("Sort by use" in frame.activity for frame in frames)
            assert any("Leave a clear zone" in frame.activity for frame in frames)
            assert frames[-1].state == "settled" and not sessions.active
            assert (await rpc(endpoint.port, "tools/call", call))["isError"] is False
            assert topics == [topic], "a late retry cannot launch the collaborators again"
        finally:
            release.set()
            await endpoint.stop()
            await sessions.close(timeout_s=.1)
