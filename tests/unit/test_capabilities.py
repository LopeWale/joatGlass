"""CapabilityRouter: the on-glasses menu's data views.

The wearer's hardware report that motivated this: double-tap opened 'a menu
modal, but not the one we designed'. The plugin now shows the designed rows;
this router is the half that draws each view.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

from joat.channels.cards import CardLog
from joat.channels.imessage import MessageThread
from joat.core.protocol import BrainstormHudView, ContainerId, HudList, HudText, InputEvent
from joat.persistence.actions import ActionDraft, ActionJournal
from joat.persistence.notes import NoteSource, NoteStore
from joat.persistence.store import DurableStore
from joat.services.capabilities import CapabilityRouter
from joat.services.hud import HudWriter
from joat.services.status import StatusPublisher


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, message: object) -> None:
        self.sent.append(message)


class FakeMessages:
    available = True

    def __init__(self, rows: list[tuple[str, str, float]]) -> None:
        self._rows = rows

    def recent(self, limit: int = 5) -> list[tuple[str, str, float]]:
        return self._rows[:limit]

    def recent_threads(self, limit: int = 5) -> list[MessageThread]:
        return [
            MessageThread(
                row[0],
                f"handle-{index}",
                row[1],
                row[2],
                row[3] if len(row) > 3 else True,
            )
            for index, row in enumerate(self._rows[:limit])
        ]


class FakeReplies:
    def __init__(self) -> None:
        self.selected: list[tuple[MessageThread, str]] = []

    def select(self, thread: MessageThread, trace_id: str) -> None:
        self.selected.append((thread, trace_id))


class BlockingMessages:
    available = True

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def recent_threads(self, limit: int = 5) -> list[MessageThread]:
        del limit
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            self.release.wait(timeout=2)
            return [MessageThread("Old", "old", "stale", time.time())]
        return [MessageThread("New", "new", "current", time.time())]


class FakePersonalData:
    def __init__(self, responses):  # type: ignore[no-untyped-def]
        self._responses = responses
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    async def call(self, method: str, body=None):  # type: ignore[no-untyped-def]
        self.calls.append((method, body))
        return self._responses[method]


def bridge_response(*, calendar="full_access", reminders="full_access", result=None):  # type: ignore[no-untyped-def]
    return SimpleNamespace(
        authorization=SimpleNamespace(calendar=calendar, reminders=reminders),
        result=result,
    )


def make_router(
    tmp_path,
    rows=None,
    cards=None,
    replies=None,
    personal_data=None,
    notes=None,
    actions=None,
):  # type: ignore[no-untyped-def]
    transport = FakeTransport()
    status = StatusPublisher(path=tmp_path / "status.json")
    log = CardLog()
    for c in cards or []:
        log.add(c, at=1.0)
    router = CapabilityRouter(
        hud=HudWriter(transport),  # type: ignore[arg-type]
        transport=transport,  # type: ignore[arg-type]
        status=status,
        messages=FakeMessages(rows) if rows is not None else None,  # type: ignore[arg-type]
        cards=log,
        replies=replies,  # type: ignore[arg-type]
        personal_data=personal_data,  # type: ignore[arg-type]
        notes=notes,
        actions=actions,
    )
    return router, transport, status


class TestCapabilityViews:
    async def test_brainstorm_and_agent_terminal_open_real_hud_views(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, transport, status = make_router(tmp_path)
        status.set_openclaw(
            state="connected_read_only",
            endpoint_host="home-mac.example-tailnet.ts.net",
            scopes=["operator.read"],
            reason="",
        )
        status.set_agent_terminal(state="ready", reason="")
        await router.open("Brainstorm")
        await router.open("Agent Terminal")
        frames = [item for item in transport.sent if isinstance(item, BrainstormHudView)]
        assert [frame.screen for frame in frames] == ["brainstorm", "agent_terminal"]
        assert frames[-1].endpoint_host == "home-mac.example-tailnet.ts.net"
        assert frames[-1].no_tools_checked is True

    async def test_messages_view_lists_recent_conversations(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        now = time.time()
        router, transport, _ = make_router(
            tmp_path, rows=[("Dele", "On my way", now), ("Adaeze", "Call me", now)])
        await router.open("Messages")
        lists = [m for m in transport.sent if isinstance(m, HudList)]
        assert lists and lists[0].to_wire()["items"] == [
            "[now] Dele · On my way", "[now] Adaeze · Call me"]
        assert lists[0].title == "MESSAGES"
        assert lists[0].hint == "tap select · left x2 overview"

    async def test_newest_same_capability_generation_owns_render_and_selection(
        self, tmp_path
    ) -> None:  # type: ignore[no-untyped-def]
        messages = BlockingMessages()
        replies = FakeReplies()
        router, transport, _ = make_router(tmp_path, replies=replies)
        router._messages = messages  # type: ignore[assignment]

        stale = asyncio.create_task(router.open("Messages", request_id="request-old"))
        assert await asyncio.to_thread(messages.started.wait, 1)
        await router.open("Messages", request_id="request-new")
        messages.release.set()
        await stale

        lists = [m for m in transport.sent if isinstance(m, HudList)]
        assert len(lists) == 1
        assert lists[0].request_id == "request-new"
        assert lists[0].selectable is True
        assert "New" in lists[0].items[0]

        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="MESSAGES",
            selected_index=0,
            trace_id="selection-old",
            request_id="request-old",
        ))
        await asyncio.sleep(0)
        assert replies.selected == []

        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="MESSAGES",
            selected_index=0,
            trace_id="selection-new",
            request_id="request-new",
        ))
        await asyncio.sleep(0)
        assert replies.selected[0][0].sender == "New"
        assert replies.selected[0][1] == "selection-new"

    async def test_selecting_a_message_arms_voice_reply_without_sending(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        now = time.time()
        replies = FakeReplies()
        router, transport, _ = make_router(
            tmp_path,
            rows=[("Dele", "On my way", now), ("Adaeze", "Call me", now)],
            replies=replies,
        )
        await router.open("Messages")

        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="MESSAGES",
            selected_index=1,
            trace_id="hud-12",
        ))
        await asyncio.sleep(0)

        assert replies.selected == [(
            MessageThread("Adaeze", "handle-1", "Call me", now),
            "hud-12",
        )]
        detail = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert detail.title == "MESSAGES · REPLY"
        assert any("JOAT, reply" in item for item in detail.items)

    async def test_group_message_cannot_be_misrouted_to_one_participant(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        now = time.time()
        replies = FakeReplies()
        router, transport, _ = make_router(
            tmp_path,
            rows=[("Project team", "Ship it", now, False)],
            replies=replies,
        )
        await router.open("Messages")

        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="MESSAGES",
            selected_index=0,
            trace_id="hud-group",
        ))
        await asyncio.sleep(0)

        assert replies.selected == []
        detail = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert detail.title == "MESSAGES · READ ONLY"
        assert any("Group replies" in item for item in detail.items)

    async def test_messages_without_access_states_the_gap(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(tmp_path, rows=None)
        await router.open("Messages")
        lists = [m for m in transport.sent if isinstance(m, HudList)]
        assert any("Full Disk Access" in item for item in lists[0].items)
        assert lists[0].title == "MESSAGES · SETUP"
        # The focus title owns capability state. A separate status message can
        # arrive after Back and stale Home for twelve seconds even when the
        # plugin correctly rejects the delayed list response.
        statuses = [m for m in transport.sent
                    if isinstance(m, HudText)
                    and m.container_id is ContainerId.STATUS_BAR]
        assert statuses == []

    async def test_notifications_view_lists_recent_cards(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(tmp_path, cards=["!! Adaeze · Messages\nCall me now"])
        await router.open("Notifications")
        lists = [m for m in transport.sent if isinstance(m, HudList)]
        assert lists and "!! Adaeze · Messages" in lists[0].to_wire()["items"][0]
        assert lists[0].title == "NOTIFICATIONS"

    async def test_notification_selection_opens_the_complete_two_line_card(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(
            tmp_path,
            cards=["!! Adaeze · Messages\nCall me now"],
        )
        await router.open("Notifications")
        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="NOTIFICATIONS",
            selected_index=0,
        ))
        await asyncio.sleep(0)

        detail = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert detail.title == "NOTIFICATIONS · DETAIL"
        assert detail.items == ["!! Adaeze · Messages", "Call me now"]

    async def test_calendar_directs_setup_to_the_explicit_mac_control(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(tmp_path)
        await router.open("Calendar")
        lists = [m for m in transport.sent if isinstance(m, HudList)]
        assert any("connect Calendar" in item for item in lists[0].items)
        assert lists[0].title == "CALENDAR · SETUP"

    async def test_calendar_queries_authorized_native_store_by_identifier(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        now_ms = int(time.time() * 1000)
        personal = FakePersonalData({
            "authorization.status": bridge_response(),
            "calendar.listContainers": bridge_response(result=[
                {"calendar_identifier": "cal-work", "title": "Work"},
            ]),
            "calendar.query": bridge_response(result=[
                {"title": "Design review", "start_unix_ms": now_ms},
            ]),
        })
        router, transport, _ = make_router(tmp_path, personal_data=personal)

        await router.open("Calendar")

        query = personal.calls[-1]
        assert query[0] == "calendar.query"
        assert query[1]["calendar_identifiers"] == ["cal-work"]
        assert query[1]["limit"] == 5
        rendered = [m for m in transport.sent if isinstance(m, HudList)][0]
        assert rendered.title == "CALENDAR · 7 DAYS"
        assert "Design review" in rendered.items[0]

        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="CALENDAR",
            selected_index=0,
        ))
        await asyncio.sleep(0)
        detail = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert detail.title == "CALENDAR · DETAIL"
        assert any("Design review" in row for row in detail.items)

    async def test_tasks_queries_incomplete_reminders(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        personal = FakePersonalData({
            "authorization.status": bridge_response(),
            "reminders.listContainers": bridge_response(result=[
                {"calendar_identifier": "list-home", "title": "Home"},
            ]),
            "reminders.query": bridge_response(result=[
                {"title": "Buy filters"},
            ]),
        })
        router, transport, _ = make_router(tmp_path, personal_data=personal)

        await router.open("Tasks")

        query = personal.calls[-1]
        assert query == (
            "reminders.query",
            {
                "calendar_identifiers": ["list-home"],
                "include_completed": False,
                "limit": 5,
            },
        )
        rendered = [m for m in transport.sent if isinstance(m, HudList)][0]
        assert rendered.title == "TASKS"
        assert rendered.items == ["Buy filters"]
        assert rendered.hint == "tap detail · left x2 overview"

        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="TASKS",
            selected_index=0,
        ))
        await asyncio.sleep(0)
        detail = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert detail.title == "TASKS · DETAIL"
        assert detail.items[0] == "Buy filters"

    async def test_quicknotes_and_activity_are_durable_reachable_views(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        store = DurableStore(tmp_path / "durable.sqlite3")
        notes = NoteStore(store)
        notes.create(
            note_id="note-1",
            idempotency_key="voice-1",
            text="Ask Adaeze about the design review",
            source=NoteSource(kind="voice.command"),
            now_us=10,
        )
        actions = ActionJournal(store)
        actions.create_draft(
            ActionDraft(
                kind="calendar.create",
                canonical_payload=b'{"title":"Design review"}',
                target_digest=b"t" * 32,
                idempotency_key="calendar-1",
            ),
            now_us=20,
        )
        router, transport, _ = make_router(
            tmp_path,
            notes=notes,
            actions=actions,
        )

        await router.open("QuickNotes")
        note_list = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert note_list.title == "QUICKNOTES"
        assert "Ask Adaeze" in note_list.items[0]
        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="QUICKNOTES",
            selected_index=0,
        ))
        await asyncio.sleep(0)
        note_detail = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert note_detail.title == "QUICKNOTES · DETAIL"
        assert note_detail.items == ["Ask Adaeze about the design review"]

        await router.open("Activity")
        action_list = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert action_list.title == "ACTIVITY"
        assert "Calendar create" in action_list.items[0]
        assert "Drafted" in action_list.items[0]
        router.on_input(InputEvent(
            container_name="capdata",
            selected_name="ACTIVITY",
            selected_index=0,
        ))
        await asyncio.sleep(0)
        action_detail = [m for m in transport.sent if isinstance(m, HudList)][-1]
        assert action_detail.title == "ACTIVITY · DETAIL"
        assert action_detail.items[0] == "Calendar create"
        assert action_detail.items[1] == "State · Drafted"
        assert action_detail.items[2].startswith("Action ref · ")
        store.close()


class TestGestureReflexes:
    async def test_menu_closed_repaints_the_last_exchange(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, transport, status = make_router(tmp_path)
        status.set(last_reply="It is 72 degrees.", last_transcript="what is the weather")
        router.on_input(InputEvent(container_name="menu", selected_name="closed"))
        await asyncio.sleep(0.05)
        main = [m for m in transport.sent
                if isinstance(m, HudText) and m.container_id is ContainerId.MAIN]
        ctx = [m for m in transport.sent
               if isinstance(m, HudText) and m.container_id is ContainerId.CONTEXT]
        assert [m.text for m in main] == ["It is 72 degrees."]
        assert [m.text for m in ctx] == ["you  what is the weather"]

    async def test_repeat_with_no_history_says_so(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        router, transport, _ = make_router(tmp_path)
        router.on_input(InputEvent(container_name="sysmenu", selected_index=1))
        await asyncio.sleep(0.05)
        texts = [m for m in transport.sent if isinstance(m, HudText)]
        assert any("Nothing to repeat" in m.text for m in texts)
