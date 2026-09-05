"""A1 probe: drive the REAL subscription seats against the REAL tool belt.

Measures the audit's own seven-day indicator: for a fixed set of ordinary
requests, which feature controller (if any) does the mind actually reach?
Controllers are recording fakes; the CLI, MCP endpoint, tool schemas,
persona and tool loop are production code. No glasses, no real sends.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "mac"))

from joat.agents.builtin_tools import conversation_tool, messages_tool, notifications_tool
from joat.agents.cli_reply_engine import (
    ClaudeCliReplyEngine, CodexCliReplyEngine, McpDoor, find_codex_binary,
)
from joat.agents.feature_tools import feature_tools, quicknote_reader_tool, terminal_sessions_tool
from joat.agents.grounding import Grounding, static_fact
from joat.agents.location_tools import wearer_location_tools
from joat.agents.message_tools import message_tools
from joat.agents.panel_tools import (
    news_article_tool, news_headlines_tool, pomodoro_status_tool, web_search_tool,
)
from joat.agents.subprocess_agent import find_claude_binary
from joat.agents.tools import ToolBelt, ToolCall
from joat.channels.cards import Card, CardLog, Urgency
from joat.channels.imessage import MessageEntry, MessageHistoryPage, MessageThread
from joat.persistence.notes import NoteStore
from joat.persistence.store import DurableStore
from joat.services.agent_endpoint import AgentEndpoint
from joat.services.message_reply import MessageReplyController
from joat.services.news import Headline
from joat.services.note_commands import NoteCommandController
from joat.services.panels import Reader, paginate
from joat.services.pomodoro import PomodoroService
from joat.services.session import BrainstormStartResult

OUT = Path(__file__).resolve().parents[2] / "output" / "a1_probe_results.json"
OUT.parent.mkdir(parents=True, exist_ok=True)
LONG_TEXT = ("Hey, about Saturday: the venue moved to the Riverside hall, doors at 6, "
             "bring the projector cable and the second HDMI adapter because theirs is "
             "broken. Also Priya asked if you could pick her up on the way, she is at "
             "the corner of 5th and Main. Let me know either way by tonight.")


class Capabilities:
    def __init__(self) -> None:
        self.opened: list[str] = []

    async def open(self, name: str, *, request_id: str | None = None) -> None:
        self.opened.append(name)


class Panels:
    async def activate(self, tab: str) -> None:
        pass


class Sessions:
    def __init__(self) -> None:
        self.topics: list[str] = []
        self.destinations: list[str] = []

    def start_brainstorm(self, topic: str) -> BrainstormStartResult:
        self.topics.append(topic)
        return BrainstormStartResult.STARTED

    def start_navigation(self, destination: str) -> BrainstormStartResult:
        self.destinations.append(destination)
        return BrainstormStartResult.STARTED


class Sender:
    async def send(self, recipient: str, text: str) -> None:
        raise AssertionError("probe must never send")


async def confirm(action_id: str, prompt: str, timeout_s: float, **_: Any) -> bool:
    await asyncio.sleep(0.2)
    return False  # the wearer declines on the glasses; nothing is sent


CONTACTS = {
    "alex": [("Alex Rivera", "+14155550101"), ("Alex Chen", "alex.chen@example.com")],
    "sam": [("Sam Okafor", "+14155550111")],
}


def contact_search(query: str) -> list[tuple[str, str]]:
    return list(CONTACTS.get(query.strip().lower(), []))


class Store:
    def _threads(self) -> list[MessageThread]:
        now = time.time()
        return [
            MessageThread(sender="Dele", handle="+14155550102", text=LONG_TEXT, at=now - 600, chat_id=1,
                          chat_guid="iMessage;-;+14155550102"),
            MessageThread(sender="Priya", handle="+14155550103", text="Running 10 late", at=now - 3000,
                          chat_id=2, chat_guid="iMessage;-;+14155550103"),
        ]

    def recent(self, limit: int = 3) -> list[tuple[str, str, float]]:
        return [(t.sender, t.text, t.at) for t in self._threads()][:limit]

    def recent_threads(self, limit: int = 5) -> list[MessageThread]:
        return self._threads()[:limit]

    def thread_message_page(self, thread: MessageThread, *, limit: int = 8,
                            before_row_id: int | None = None) -> MessageHistoryPage:
        now = time.time()
        if thread.sender == "Dele":
            entries = (
                MessageEntry(text=LONG_TEXT, at=now - 600, from_me=False, row_id=3, sender="Dele"),
                MessageEntry(text="Sure, what time?", at=now - 900, from_me=True, row_id=2),
                MessageEntry(text="Are you still coming Saturday?", at=now - 1000, from_me=False, row_id=1, sender="Dele"),
            )
        else:
            entries = (MessageEntry(text="Running 10 late", at=now - 3000, from_me=False, row_id=5, sender="Priya"),)
        return MessageHistoryPage(entries=entries[:limit], has_older=False)


class News:
    def __init__(self) -> None:
        now = time.time()
        self.headlines = (
            Headline(title="City council approves new bike lanes on Main Street", link="https://example.org/a",
                     source="Local Times", category="local", published_at=now - 3600, guid="a"),
            Headline(title="Apple unveils thinner glasses display", link="https://example.org/b",
                     source="Tech Daily", category="tech", published_at=now - 7200, guid="b"),
        )

    async def open(self, index: int) -> Reader | None:
        if not 0 <= index < len(self.headlines):
            return None
        body = ("The council voted 7 to 2 on Tuesday to add protected bike lanes along Main Street "
                "between 1st and 9th, with construction starting in October and finishing by spring. "
                "Opponents cited parking loss; supporters pointed to a 40 percent rise in cycling.")
        return Reader(tab="news", title="NEWS · ARTICLE", metadata="Local Times · 1h", pages=paginate(body))


class Search:
    results: list[Any] = []

    async def lookup(self, query: str) -> str:
        return ""


class Roster:
    def picker_rows(self) -> list[tuple[str, str]]:
        return [("hermes", "▸ hermes · ready"), ("work", "  work · working · 1 waiting · running tests")]

    def tail(self, name: str | None = None, lines: int = 6) -> str:
        return f"{name}: $ pytest -q\n..F..\n1 failed, 4 passed\n[approval] run: git push?"


class NoReplier:
    async def stream(self, text: str):
        yield ""


async def submit_terminal(lane: str, instruction: str, operation_id: str) -> str:
    return f"{lane}: accepted."


JOURNEYS: list[tuple[str, list[str], str]] = [
    ("J1 open messages", ["could you bring up my messages?"], "open_feature:messages"),
    ("J2 read full message", ["read the latest message from Dele in full"], "read_conversation(Dele) with the whole text"),
    ("J3 message my wife", ["send a message to my wife", "her name is Sam", "I'll be home at six"],
     "prepare_message -> provide_message_input(Sam) -> provide_message_input(body verbatim)"),
    ("J4 tell Alex", ["tell Alex I'm running late"], "prepare_message(new, Alex, 'I'm running late') -> candidate clarification"),
    ("J5 tell her (no selection)", ["could you tell her I'm on my way?"], "prepare_message(reply) -> 'select a conversation first'"),
    ("J6 quick note", ["keep this as a quick note: the spare key is with Sam"], "create_quicknote('the spare key is with Sam')"),
    ("J7 focus + pause", ["give me a focus session", "could you pause that timer?"], "control_timer(start) then control_timer(pause)"),
    ("J8 walk to library", ["help me walk to the library"], "start_navigation('the library')"),
    ("J9 open terminal", ["open agent terminal"], "open_feature:agent_terminal"),
    ("J10 brainstorm", ["start a brainstorm about whether we should move to Austin"], "start_brainstorm('whether we should move to Austin')"),
    ("J11 news", ["what happened in the news today?"], "news_headlines"),
    ("J12 article", ["what does the bike lane story say?"], "read_news_article"),
    ("J13 read note", ["what did I note about the spare key?"], "read_quicknotes(query)"),
    ("J14 terminal status", ["what is the work terminal doing?"], "terminal_sessions(lane=work)"),
]


async def main(seat_name: str) -> None:
    tmp = Path(__file__).with_name(f"a1_probe_store_{seat_name}.sqlite3")
    if tmp.exists():
        tmp.unlink()
    store = DurableStore(tmp)
    note_store = NoteStore(store)
    NoteCommandController(note_store).save_text("the spare key is with Sam")
    timer = PomodoroService()
    caps = Capabilities()
    sessions = Sessions()
    cards = CardLog()
    cards.add(Card(source="Priya", channel="Messages", summary="Running 10 late", urgency=Urgency.NOTIFY))
    replies = MessageReplyController(Sender(), confirm, contact_search=contact_search)
    news = News()
    message_store = Store()
    belt = ToolBelt([
        *wearer_location_tools(lambda: None, lambda: None, None),  # type: ignore[arg-type]
        notifications_tool(cards),
        news_headlines_tool(news),  # type: ignore[arg-type]
        news_article_tool(news),  # type: ignore[arg-type]
        web_search_tool(Search(), None),  # type: ignore[arg-type]
        pomodoro_status_tool(timer),
        messages_tool(message_store),  # type: ignore[arg-type]
        conversation_tool(message_store),  # type: ignore[arg-type]
    ])
    for tool in feature_tools(capabilities=caps, notes=NoteCommandController(note_store),
                              pomodoro=timer, panels=Panels(), brainstorm=sessions,
                              terminal=submit_terminal, terminal_lanes=("hermes", "work"),
                              navigation=sessions):
        belt.register(tool)
    for tool in message_tools(replies):
        belt.register(tool)
    belt.register(quicknote_reader_tool(note_store, None))
    belt.register(terminal_sessions_tool(Roster()))

    calls: list[dict[str, Any]] = []
    real_execute = belt.execute

    async def recording_execute(call: ToolCall) -> str:
        started = time.monotonic()
        result = await real_execute(call)
        calls.append({"tool": call.name, "arguments": call.arguments,
                      "result": result[:300], "ms": round((time.monotonic() - started) * 1000)})
        return result

    belt.execute = recording_execute  # type: ignore[method-assign]

    endpoint = AgentEndpoint(NoReplier(), "probe-token", port=0, tools=belt)  # type: ignore[arg-type]
    await endpoint.start()
    door = McpDoor(url=f"http://127.0.0.1:{endpoint.port}/mcp", token="probe-token")
    grounding = Grounding(sources=[static_fact("Local time: 1:30 PM Saturday, Los Angeles. Glasses battery: 87%.")],
                          unknowns=["their calendar or schedule"])

    def seat() -> Any:
        if seat_name == "claude":
            return ClaudeCliReplyEngine(find_claude_binary(), grounding=grounding, mcp=door)
        return CodexCliReplyEngine(find_codex_binary(), grounding=grounding, mcp=door)

    results: list[dict[str, Any]] = []
    try:
        for label, turns, expected in JOURNEYS:
            engine = seat()
            record: dict[str, Any] = {"journey": label, "expected": expected, "turns": []}
            for text in turns:
                before = len(calls)
                started = time.monotonic()
                try:
                    reply = await engine.reply(text)
                except Exception as exc:  # noqa: BLE001
                    reply = f"<<exception {type(exc).__name__}: {exc}>>"
                record["turns"].append({
                    "said": text, "reply": (reply or "")[:400],
                    "seconds": round(time.monotonic() - started, 1),
                    "tool_calls": calls[before:],
                })
                print(f"[{seat_name}] {label} | {text!r} -> {len(calls) - before} tool call(s) in "
                      f"{time.monotonic() - started:.1f}s: {(reply or '')[:160]!r}", flush=True)
                for c in calls[before:]:
                    print(f"        tool {c['tool']} {json.dumps(c['arguments'])[:160]} -> {c['result'][:120]!r}", flush=True)
            results.append(record)
    finally:
        await endpoint.stop()
        store.close()
    summary = {"seat": seat_name, "opened": caps.opened, "brainstorm_topics": sessions.topics,
               "destinations": sessions.destinations, "journeys": results}
    out = OUT.with_name(f"a1_probe_results_{seat_name}.json")
    out.write_text(json.dumps(summary, indent=2))
    print(f"wrote {out}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "claude"))
