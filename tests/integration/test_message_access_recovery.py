"""Synthetic chat.db -> production reader/controller; never accesses live Messages."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from joat.agents.builtin_tools import messages_tool
from joat.channels.cards import CardLog
from joat.channels.imessage import IMessageStore
from joat.core.protocol import EventType, HudList, InputEvent
from joat.services.capabilities import CapabilityRouter
from joat.services.hud import HudWriter

BODY = 'Full group message.  Keep both spaces.\n' * 30 + 'END OF MESSAGE.'


def create_db(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE message (handle_id INTEGER, text TEXT, date INTEGER, is_from_me INTEGER);
            CREATE TABLE handle (id TEXT);
            CREATE TABLE chat (display_name TEXT);
            CREATE TABLE chat_message_join (message_id INTEGER, chat_id INTEGER);
            CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
            INSERT INTO handle VALUES ('synthetic-alex'), ('synthetic-sam');
            INSERT INTO chat VALUES ('Project team');
            INSERT INTO chat_handle_join VALUES (1, 1), (1, 2);
        ''')
        for i in range(1, 11):
            db.execute('INSERT INTO message VALUES (?, ?, ?, ?)',
                       (1 if i % 2 else 2, BODY if i == 10 else f'Message {i}', i, int(i == 9)))
            db.execute('INSERT INTO chat_message_join VALUES (?, 1)', (i,))


def store_at(path: Path) -> IMessageStore:
    store = IMessageStore(path)
    # Prevent any live Contacts reads, including fallbacks in test runs.
    store._contacts = SimpleNamespace(name_for=lambda h: {
        'synthetic-alex': 'Alex', 'synthetic-sam': 'Sam',
    }.get(h, ''))
    return store


class Transport:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send(self, message: Any) -> None:
        self.sent.append(message)

    @property
    def frame(self) -> HudList:
        return [m for m in self.sent if isinstance(m, HudList)][-1]


class Replies:
    def __init__(self) -> None:
        self.selected: list[Any] = []
        self.cleared: list[str] = []

    def select(self, *args: Any) -> None:
        self.selected.append(args)

    def clear_selection(self, reason: str) -> None:
        self.cleared.append(reason)


def make_router(store: IMessageStore, replies: Replies | None) -> tuple[CapabilityRouter, Transport]:
    transport = Transport()
    router = CapabilityRouter(hud=HudWriter(transport), transport=transport,
        status=SimpleNamespace(), messages=store, replies=replies, cards=CardLog())
    return router, transport


async def test_group_history_full_reader_paging_and_back(tmp_path: Path) -> None:
    path = tmp_path / 'chat.db'
    create_db(path)
    replies = Replies()
    router, transport = make_router(store_at(path), replies)
    await router.open('Messages')
    await router._select_message(1, 'group')
    assert transport.frame.title == 'MESSAGES · CONVERSATION'
    assert transport.frame.selectable
    # Paging travels as tapped rows, not a swipe hint.
    newer, older = transport.frame.page_controls or (None, None)
    assert newer is None and older is not None
    assert transport.frame.items[older] == '▼ Older messages'
    assert 'read-only' in transport.frame.hint
    assert 'say reply' not in transport.frame.hint
    assert not any('JOAT, reply' in row or 'Mic off?' in row for row in transport.frame.items)
    assert transport.frame.items[1].startswith('Sam · ')
    assert transport.frame.items[2].startswith('You · ')
    assert transport.frame.items[3].startswith('Sam · ')
    assert replies.selected == []
    assert not router._message_selection_active

    await router._select_message_detail(1, 'read', None)
    pages = []
    while True:
        frame = transport.frame
        assert frame.title == 'MESSAGES · MESSAGE'
        assert 'From Sam' in frame.items[0]
        assert 'read-only' in frame.hint and 'say reply' not in frame.hint
        assert 'left x2: back' in frame.hint
        pages.extend(frame.to_wire()['items'][1:])
        if 'down more' not in frame.hint:
            break
        await router._page_message_detail(EventType.SCROLL_BOTTOM, 'more', None)
    assert ''.join(pages) == BODY
    router.on_input(InputEvent(container_name='capability-lifecycle', selected_name='messages-conversation'))
    assert router._message_detail is None
    await router._page_message_history(EventType.SCROLL_BOTTOM, 'older', None)
    assert any('Alex · Message 1' == row for row in transport.frame.items)
    assert transport.frame.page_controls is not None
    assert transport.frame.page_controls[1] is None
    await router._page_message_history(EventType.SCROLL_TOP, 'newer', None)
    assert transport.frame.items[1].startswith('Sam · ')
    router.on_input(InputEvent(container_name='capability-lifecycle', selected_name='messages-home'))
    assert router._message_history is None
    assert replies.selected == []


async def test_group_reading_does_not_require_a_send_controller(tmp_path: Path) -> None:
    path = tmp_path / 'chat.db'
    create_db(path)
    router, transport = make_router(store_at(path), None)
    await router.open('Messages')
    await router._select_message(1, 'group')
    assert transport.frame.title == 'MESSAGES · CONVERSATION'


def test_group_entries_resolve_each_participant_without_changing_target(tmp_path: Path) -> None:
    path = tmp_path / 'chat.db'
    create_db(path)
    store = store_at(path)
    thread = store.recent_threads()[0]
    assert thread.sender == 'Project team' and not thread.replyable
    page = store.thread_message_page(thread)
    assert [(e.row_id, e.sender) for e in page.entries[:4]] == [
        (10, 'Sam'), (9, None), (8, 'Sam'), (7, 'Alex')]
    assert thread.handle == 'synthetic-sam'


async def test_same_reader_recovers_when_database_appears(tmp_path: Path) -> None:
    path = tmp_path / 'chat.db'
    store = store_at(path)
    router, transport = make_router(store, Replies())
    await router.open('Messages')
    assert transport.frame.title == 'MESSAGES · SETUP'
    assert not path.exists(), 'reader must never create a missing Messages database'
    create_db(path)
    await router.open('Messages')
    assert transport.frame.title == 'MESSAGES'
    assert any('Project team' in row for row in transport.frame.items)


async def test_same_model_tool_recovers_when_database_appears(tmp_path: Path) -> None:
    path = tmp_path / 'chat.db'
    tool = messages_tool(store_at(path))
    result = await tool.run({})
    assert 'unavailable' in result.lower() and 'no recent' not in result.lower()
    assert str(path) not in result and not path.exists()
    create_db(path)
    assert 'Full group message.' in await tool.run({})


@pytest.mark.parametrize('error', [PermissionError('private/path'), sqlite3.OperationalError('private/path')])
async def test_model_access_failure_is_guidance_not_empty_inbox(error: Exception) -> None:
    def read(_limit: int) -> Any:
        raise error
    tool = messages_tool(SimpleNamespace(recent=read))
    result = await tool.run({})
    assert 'private/path' not in result
    assert 'no recent' not in result
    assert ('Full Disk Access' in result) == isinstance(error, PermissionError)
    assert 'retry' in result.lower()


async def test_unknown_group_sender_is_not_mislabeled_as_group_name(tmp_path: Path) -> None:
    path = tmp_path / 'chat.db'
    create_db(path)
    with sqlite3.connect(path) as db:
        db.execute('UPDATE message SET handle_id = NULL WHERE ROWID = 10')
    router, transport = make_router(store_at(path), Replies())
    await router.open('Messages')
    await router._select_message(1, 'group')
    assert transport.frame.items[1].startswith('Participant · ')
    await router._select_message_detail(1, 'read', None)
    assert 'From Participant' in transport.frame.items[0]
    assert 'From Project team' not in transport.frame.items[0]


async def test_same_reader_recovers_after_access_denial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / 'chat.db'
    create_db(path)
    store = store_at(path)
    connect = store._connect
    def denied() -> Any:
        raise PermissionError('synthetic private path')
    monkeypatch.setattr(store, '_connect', denied)
    router, transport = make_router(store, Replies())
    await router.open('Messages')
    assert transport.frame.title == 'MESSAGES · ACCESS'
    monkeypatch.setattr(store, '_connect', connect)
    await router.open('Messages')
    assert transport.frame.title == 'MESSAGES'
    await router._select_message(1, 'recovered')
    assert transport.frame.title == 'MESSAGES · CONVERSATION'


async def test_registered_notification_lane_recovers_from_missing_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    from joat.channels.notification_mux import NotificationLane, NotificationMux
    from joat.persistence.notifications import NotificationObservationStore
    from joat.persistence.store import DurableStore

    path = tmp_path / 'chat.db'
    source = store_at(path)
    failed = threading.Event()
    original_baseline = source.baseline
    def baseline() -> int:
        try:
            return original_baseline()
        except (OSError, sqlite3.Error):
            failed.set()
            raise
    monkeypatch.setattr(source, 'baseline', baseline)
    with DurableStore(tmp_path / 'joat.sqlite3') as durable:
        observations = NotificationObservationStore(durable)
        lane = NotificationLane('messages-db', source, 'messages.incoming_text',
            'authoritative', 'healthy_complete_for_scope', 'complete_for_scope', 'messages.incoming_text')
        mux = NotificationMux([lane], observations, triage=None, deliver=lambda _: None,
                              poll_interval_s=0.01)
        task = asyncio.create_task(mux.run())
        try:
            async with asyncio.timeout(10):
                while not failed.is_set():
                    await asyncio.sleep(0.01)
                assert not path.exists()
                create_db(path)
                while not durable.execute("SELECT 1 FROM notification_sources WHERE source_id='messages-db'").fetchone():
                    await asyncio.sleep(0.01)
                # The first successful baseline is 10: history is not replayed
                # as new notifications merely because access became available.
                assert durable.execute('SELECT COUNT(*) FROM notification_observations').fetchone()[0] == 0
                with sqlite3.connect(path) as writer:
                    writer.execute("INSERT INTO message VALUES (1, 'Arrived after recovery', 11, 0)")
                    writer.execute('INSERT INTO chat_message_join VALUES (11, 1)')
                while durable.execute('SELECT COUNT(*) FROM notification_observations').fetchone()[0] != 1:
                    await asyncio.sleep(0.01)
                assert json.loads(durable.execute("SELECT cursor_json FROM notification_sources WHERE source_id='messages-db'").fetchone()[0]) == 11
        finally:
            mux.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
