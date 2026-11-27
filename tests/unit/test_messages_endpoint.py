"""The Messages relay the iPhone talks to: auth, opaque ids, long poll, sends."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from joat.channels.imessage import (
    IMessageStore,
    IncomingText,
    MessageEntry,
    MessageHistoryPage,
    MessageThread,
)
from joat.channels.message_sender import MessageSendError
from joat.core.interfaces import MessageSender
from joat.services.messages_endpoint import MAX_PAGE, MAX_THREADS, MessagesEndpoint, messages_token


TOKEN = "test-token-not-a-real-secret"
GUID = "iMessage;-;+13125550101"
HANDLE = "+13125550101"


def _incoming_message_db(path: Path, count: int) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE message (handle_id INTEGER, text TEXT, date INTEGER, is_from_me INTEGER);
        CREATE TABLE handle (id TEXT);
        CREATE TABLE chat (display_name TEXT);
        CREATE TABLE chat_message_join (message_id INTEGER, chat_id INTEGER);
        CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
        INSERT INTO handle VALUES ('+13125550101');
        INSERT INTO chat VALUES ('Evan');
    """)
    for index in range(1, count + 1):
        conn.execute("INSERT INTO message VALUES (1, ?, ?, 0)", (f"Note {index}", index))
        conn.execute("INSERT INTO chat_message_join VALUES (?, 1)", (index,))
    conn.commit()
    conn.close()
    return path


class FakeStore:
    available = True

    def __init__(self) -> None:
        self.texts: list[IncomingText] = []
        self.thread = MessageThread(
            sender="Evan", handle=HANDLE, text="hello", at=1.0, chat_id=7,
            chat_guid=GUID, unread=True,
        )
        self.threads: list[MessageThread] | None = None
        self.entries: tuple[MessageEntry, ...] = (
            MessageEntry(text="hi", at=2.0, from_me=True, row_id=9),
        )
        self.recent_limits: list[int] = []
        self.history_limits: list[int] = []

    def baseline(self) -> int:
        return 40

    def read_new_texts(self, after: int, limit: int = 20):
        fresh = [t for t in self.texts if t.row_id > after]
        return fresh, (fresh[-1].row_id if fresh else after), False

    def recent_threads(self, limit: int = 5) -> list[MessageThread]:
        self.recent_limits.append(limit)
        rows = self.threads if self.threads is not None else [self.thread]
        return rows[:limit]

    def thread_message_page(self, thread, *, limit=8, before_row_id=None):
        self.history_limits.append(limit)
        sliced = self.entries[:limit]
        return MessageHistoryPage(
            entries=sliced,
            has_older=len(self.entries) > limit,
        )


class FakeSender(MessageSender):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.fail: MessageSendError | None = None

    async def send(self, recipient: str, text: str) -> None:
        if self.fail:
            raise self.fail
        self.sent.append(("participant", recipient, text))

    async def send_chat(self, chat_guid: str, text: str) -> None:
        if self.fail:
            raise self.fail
        self.sent.append(("chat", chat_guid, text))


def _incoming(row_id: int, text: str = "yo") -> IncomingText:
    return IncomingText(
        row_id=row_id, label="Evan", person="Evan", text=text, at=3.0,
        is_group=False, handle=HANDLE, chat_id=7, chat_guid=GUID,
    )


async def _call(
    endpoint: MessagesEndpoint, method: str, target: str,
    body: dict | None = None, token: str | None = TOKEN,
) -> tuple[int, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", endpoint.port)
    payload = json.dumps(body).encode() if body is not None else b""
    lines = [f"{method} {target} HTTP/1.1", "Host: x", f"Content-Length: {len(payload)}"]
    if token is not None:
        lines.append(f"Authorization: Bearer {token}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode() + payload)
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, rest = raw.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(rest or b"{}")


@pytest.fixture
async def relay():
    store, sender = FakeStore(), FakeSender()
    endpoint = MessagesEndpoint(store, sender, TOKEN, port=0, poll_s=0.01)
    await endpoint.start()
    try:
        yield endpoint, store, sender
    finally:
        await endpoint.stop()


async def test_health_needs_no_token_but_everything_else_does(relay) -> None:
    endpoint, *_ = relay
    assert (await _call(endpoint, "GET", "/health", token=None))[0] == 200
    assert (await _call(endpoint, "GET", "/v1/threads", token=None))[0] == 401
    assert (await _call(endpoint, "GET", "/v1/threads", token="wrong"))[0] == 401


async def test_threads_are_opaque_and_never_carry_the_private_identity(relay) -> None:
    endpoint, *_ = relay
    status, body = await _call(endpoint, "GET", "/v1/threads")
    assert status == 200
    raw = json.dumps(body)
    assert GUID not in raw and HANDLE not in raw
    (thread,) = body["threads"]
    assert len(thread["id"]) == 20 and thread["label"] == "Evan" and thread["replyable"]


async def test_first_events_call_returns_only_a_watermark(relay) -> None:
    endpoint, store, _ = relay
    store.texts = [_incoming(5)]
    status, body = await _call(endpoint, "GET", "/v1/events")
    assert (status, body["texts"], body["after"]) == (200, [], 40)


async def test_events_deliver_new_texts_with_a_thread_id_the_phone_can_reply_to(relay) -> None:
    endpoint, store, sender = relay
    store.texts = [_incoming(41, "on my way")]
    status, body = await _call(endpoint, "GET", "/v1/events?after=40")
    assert status == 200 and body["after"] == 41
    (text,) = body["texts"]
    assert text["text"] == "on my way" and GUID not in json.dumps(body)

    status, _ = await _call(
        endpoint, "POST", "/v1/send", {"thread": text["thread"], "text": "ok", "client_id": "a"},
    )
    assert status == 200 and sender.sent == [("chat", GUID, "ok")]


async def test_events_long_poll_waits_then_returns_empty(relay) -> None:
    endpoint, *_ = relay
    status, body = await _call(endpoint, "GET", "/v1/events?after=40&wait=1")
    assert (status, body["texts"], body["after"]) == (200, [], 40)


async def test_history_requires_a_known_thread(relay) -> None:
    endpoint, *_ = relay
    assert (await _call(endpoint, "GET", f"/v1/threads/{'a' * 20}/messages"))[0] == 404
    _, listing = await _call(endpoint, "GET", "/v1/threads")
    thread_id = listing["threads"][0]["id"]
    status, body = await _call(endpoint, "GET", f"/v1/threads/{thread_id}/messages?limit=4")
    assert status == 200 and body["messages"][0]["from_me"] is True and body["older"] is False


async def test_send_is_idempotent_per_client_id(relay) -> None:
    endpoint, _, sender = relay
    for _ in range(2):
        status, _ = await _call(
            endpoint, "POST", "/v1/send", {"to": "+15555550123", "text": "hi", "client_id": "same"},
        )
        assert status == 200
    assert sender.sent == [("participant", "+15555550123", "hi")]


async def test_send_rejects_a_forged_thread_and_a_malformed_recipient(relay) -> None:
    endpoint, _, sender = relay
    forged = await _call(endpoint, "POST", "/v1/send", {"thread": "f" * 20, "text": "x"})
    assert forged[0] == 404
    bad = await _call(endpoint, "POST", "/v1/send", {"to": 'x"; do shell script', "text": "x"})
    assert bad[0] == 400
    both = await _call(endpoint, "POST", "/v1/send", {"to": HANDLE, "thread": "f" * 20, "text": "x"})
    assert both[0] == 400
    assert sender.sent == []


async def test_send_failure_is_reported_with_the_possible_commit(relay) -> None:
    endpoint, _, sender = relay
    sender.fail = MessageSendError("boom", possible_external_commit=False, code="timeout")
    status, body = await _call(endpoint, "POST", "/v1/send", {"to": HANDLE, "text": "x"})
    assert status == 502
    assert body["error"]["code"] == "timeout" and body["error"]["possibly_sent"] is False


def test_token_comes_from_the_environment_or_a_private_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("JOAT_MESSAGES_TOKEN", raising=False)
    path = tmp_path / "messages-token"
    first = messages_token(path)
    assert first and path.stat().st_mode & 0o777 == 0o600
    assert messages_token(path) == first
    monkeypatch.setenv("JOAT_MESSAGES_TOKEN", "from-env")
    assert messages_token(path) == "from-env"


def test_read_new_texts_pages_by_row_id_without_stealing_the_edge(tmp_path: Path) -> None:
    store = IMessageStore(_incoming_message_db(tmp_path / "chat.db", 3))
    texts, watermark, more = store.read_new_texts(0, limit=2)
    assert [t.text for t in texts] == ["Note 1", "Note 2"] and watermark == 2 and more
    texts, watermark, more = store.read_new_texts(watermark, limit=2)
    assert [t.text for t in texts] == ["Note 3"] and watermark == 3 and not more
    assert store.read_new_texts(3)[0] == []


async def test_threads_and_history_support_expanded_limits_up_to_fifty(relay) -> None:
    endpoint, store, _ = relay
    assert MAX_THREADS == 50 and MAX_PAGE == 50
    store.threads = [
        MessageThread(
            sender=f"Contact {idx}",
            handle=f"+1555000{idx:04d}",
            text=f"Preview {idx}",
            at=float(idx),
            chat_id=idx,
            chat_guid=f"iMessage;-;+1555000{idx:04d}",
        )
        for idx in range(1, 61)
    ]
    store.entries = tuple(
        MessageEntry(text=f"Message {idx}", at=float(idx), from_me=(idx % 2 == 0), row_id=100 - idx)
        for idx in range(60)
    )

    # Default limit stays 10 when omitted
    status, default_body = await _call(endpoint, "GET", "/v1/threads")
    assert status == 200 and len(default_body["threads"]) == 10

    # Supports limit=30 and limit=50, clamping >50 to 50
    status, thirty = await _call(endpoint, "GET", "/v1/threads?limit=30")
    assert status == 200 and len(thirty["threads"]) == 30

    status, fifty = await _call(endpoint, "GET", "/v1/threads?limit=50")
    assert status == 200 and len(fifty["threads"]) == 50

    status, clamped = await _call(endpoint, "GET", "/v1/threads?limit=99")
    assert status == 200 and len(clamped["threads"]) == 50

    # Default history page is 16 when omitted, and allows up to 50
    thread_id = fifty["threads"][0]["id"]
    status, default_hist = await _call(endpoint, "GET", f"/v1/threads/{thread_id}/messages")
    assert status == 200 and len(default_hist["messages"]) == 16 and default_hist["older"] is True

    status, full_hist = await _call(endpoint, "GET", f"/v1/threads/{thread_id}/messages?limit=50")
    assert status == 200 and len(full_hist["messages"]) == 50 and full_hist["older"] is True


async def test_threads_query_searches_deep_slice_and_registers_hmac_ids(relay) -> None:
    endpoint, store, sender = relay
    store.threads = [
        MessageThread(
            sender=f"Person {idx}",
            handle=f"+1555111{idx:04d}",
            text=f"Routine update {idx}",
            at=float(idx),
            chat_id=idx,
            chat_guid=f"iMessage;-;+1555111{idx:04d}",
        )
        for idx in range(1, 95)
    ]
    # Place targets deep in the list (past top 50)
    store.threads[72] = MessageThread(
        sender="Sarah Chen",
        handle="+15559990073",
        text="Can you review the schematic?",
        at=200.0,
        chat_id=73,
        chat_guid="iMessage;-;+15559990073",
    )
    store.threads[88] = MessageThread(
        sender="Ops Group",
        handle="+15559990089",
        text="Deployment window moved to Friday",
        at=201.0,
        chat_id=89,
        chat_guid="iMessage;+;chat89",
        is_group=True,
    )

    # Match by sender label (case-insensitive)
    store.recent_limits.clear()
    status, by_name = await _call(endpoint, "GET", "/v1/threads?q=sarah&limit=5")
    assert status == 200
    assert store.recent_limits[-1] == 100  # max(5 * 5, 100)
    assert [t["label"] for t in by_name["threads"]] == ["Sarah Chen"]
    sarah_id = by_name["threads"][0]["id"]
    assert len(sarah_id) == 20
    assert "+15559990073" not in json.dumps(by_name)

    # Registered HMAC ID works immediately for history and send
    hist_status, _ = await _call(endpoint, "GET", f"/v1/threads/{sarah_id}/messages")
    assert hist_status == 200
    send_status, _ = await _call(
        endpoint, "POST", "/v1/send", {"thread": sarah_id, "text": "Looks good!"},
    )
    assert send_status == 200
    assert sender.sent[-1] == ("chat", "iMessage;-;+15559990073", "Looks good!")

    # Match by preview text (case-insensitive) with larger limit -> max(30 * 5, 100) == 150
    status, by_preview = await _call(endpoint, "GET", "/v1/threads?q=DEPLOYMENT%20WINDOW&limit=30")
    assert status == 200
    assert store.recent_limits[-1] == 150
    assert [t["label"] for t in by_preview["threads"]] == ["Ops Group"]


async def test_sqlite_imessage_store_supports_fifty_threads_query_and_fifty_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("joat.channels.contacts.ContactResolver._load", lambda _self: {})
    db_path = tmp_path / "chat.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE message (handle_id INTEGER, text TEXT, date INTEGER, is_from_me INTEGER);
        CREATE TABLE handle (id TEXT);
        CREATE TABLE chat (display_name TEXT, guid TEXT);
        CREATE TABLE chat_message_join (message_id INTEGER, chat_id INTEGER);
        CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
    """)
    msg_id = 0
    for chat_idx in range(1, 56):
        phone = f"+1555222{chat_idx:04d}"
        label = "Amina Bello" if chat_idx == 1 else f"Thread {chat_idx}"
        body = "Flight landed safely at gate B12" if chat_idx == 1 else f"Latest from {chat_idx}"
        conn.execute("INSERT INTO handle VALUES (?)", (phone,))
        conn.execute("INSERT INTO chat VALUES (?, ?)", (label, f"iMessage;-;{phone}"))
        conn.execute("INSERT INTO chat_handle_join VALUES (?, ?)", (chat_idx, chat_idx))
        turns = 35 if chat_idx == 1 else 1
        for turn in range(turns):
            msg_id += 1
            text = body if turn == turns - 1 else f"Earlier {turn}"
            conn.execute("INSERT INTO message VALUES (?, ?, ?, 0)", (chat_idx, text, msg_id))
            conn.execute("INSERT INTO chat_message_join VALUES (?, ?)", (msg_id, chat_idx))
    conn.commit()
    conn.close()

    store = IMessageStore(db_path)
    endpoint = MessagesEndpoint(store, FakeSender(), TOKEN, port=0, poll_s=0.01)
    await endpoint.start()
    try:
        status, thirty = await _call(endpoint, "GET", "/v1/threads?limit=30")
        assert status == 200 and len(thirty["threads"]) == 30

        status, fifty = await _call(endpoint, "GET", "/v1/threads?limit=50")
        assert status == 200 and len(fifty["threads"]) == 50

        # Chat 1 is oldest (55th newest), so unfiltered top 50 omits it, but ?q= finds it!
        assert all(t["label"] != "Amina Bello" for t in fifty["threads"])
        status, searched = await _call(endpoint, "GET", "/v1/threads?q=gate%20b12&limit=10")
        assert status == 200
        assert [t["label"] for t in searched["threads"]] == ["Amina Bello"]

        amina_id = searched["threads"][0]["id"]
        status, history = await _call(endpoint, "GET", f"/v1/threads/{amina_id}/messages?limit=35")
        assert status == 200
        assert len(history["messages"]) == 35
        assert history["messages"][0]["text"] == "Flight landed safely at gate B12"
    finally:
        await endpoint.stop()


async def test_mark_thread_read_clears_unread_across_restarts_until_newer_message_and_runs_marker(
    tmp_path: Path,
) -> None:
    store, sender = FakeStore(), FakeSender()
    store.entries = (
        MessageEntry(text="new incoming", at=10.0, from_me=False, row_id=12, unread=True, sender="Evan"),
        MessageEntry(text="older", at=5.0, from_me=False, row_id=11, unread=True, sender="Evan"),
    )
    store.thread = MessageThread(
        sender="Evan", handle=HANDLE, text="new incoming", at=10.0, chat_id=7,
        chat_guid=GUID, unread=True,
    )
    read_state_path = tmp_path / "messages-read.json"
    marked_upstream: list[tuple[str, str | None]] = []

    async def fake_read_marker(thread: MessageThread) -> bool:
        marked_upstream.append((thread.sender, thread.chat_guid))
        return True

    endpoint = MessagesEndpoint(
        store,
        sender,
        TOKEN,
        port=0,
        poll_s=0.01,
        read_state_path=read_state_path,
        read_marker=fake_read_marker,
    )
    await endpoint.start()
    try:
        # Auth & parameter validation on /v1/read
        assert (await _call(endpoint, "POST", "/v1/read", {"thread": "a" * 20}, token=None))[0] == 401
        assert (await _call(endpoint, "POST", "/v1/read", {"thread": "bad"}))[0] == 400
        assert (await _call(endpoint, "POST", "/v1/read", {"thread": "f" * 20}))[0] == 404

        status, initial = await _call(endpoint, "GET", "/v1/threads")
        assert status == 200 and initial["threads"][0]["unread"] is True
        thread_id = initial["threads"][0]["id"]

        status, read_res = await _call(endpoint, "POST", "/v1/read", {"thread": thread_id})
        assert status == 200 and read_res["ok"] is True

        # Wait briefly for background upstream marker task to settle
        await asyncio.sleep(0.05)
        assert marked_upstream == [("Evan", GUID)]

        # Persisted file is 0600 and contains zero private handles or chat GUIDs
        assert read_state_path.is_file()
        assert (read_state_path.stat().st_mode & 0o777) == 0o600
        raw_state = read_state_path.read_text()
        assert GUID not in raw_state and HANDLE not in raw_state and "new incoming" not in raw_state

        # Both /v1/threads and /v1/threads/<id>/messages immediately report unread=False!
        _, after_threads = await _call(endpoint, "GET", "/v1/threads")
        assert after_threads["threads"][0]["unread"] is False
        _, after_hist = await _call(endpoint, "GET", f"/v1/threads/{thread_id}/messages")
        assert [m["unread"] for m in after_hist["messages"]] == [False, False]
    finally:
        await endpoint.stop()

    # Restart endpoint reading the same read_state_path -> still read!
    restarted = MessagesEndpoint(
        store, sender, TOKEN, port=0, poll_s=0.01,
        read_state_path=read_state_path, read_marker=fake_read_marker,
    )
    await restarted.start()
    try:
        _, persisted_threads = await _call(restarted, "GET", "/v1/threads")
        assert persisted_threads["threads"][0]["unread"] is False

        # A newer incoming message arrives (at=25.0 > 10.0, row_id=15 > 12) -> unread becomes True again!
        store.thread = MessageThread(
            sender="Evan", handle=HANDLE, text="follow up", at=25.0, chat_id=7,
            chat_guid=GUID, unread=True,
        )
        store.entries = (
            MessageEntry(text="follow up", at=25.0, from_me=False, row_id=15, unread=True, sender="Evan"),
            *store.entries,
        )
        _, newer_threads = await _call(restarted, "GET", "/v1/threads")
        assert newer_threads["threads"][0]["unread"] is True
        _, newer_hist = await _call(restarted, "GET", f"/v1/threads/{thread_id}/messages")
        assert [m["unread"] for m in newer_hist["messages"]] == [True, False, False]
    finally:
        await restarted.stop()
