"""Messages store keeps reply targeting truthful and read-only."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from joat.channels.imessage import IMessageStore, MessageThread


def _incoming_message_db(path: Path, count: int) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE message (handle_id INTEGER, text TEXT, date INTEGER, is_from_me INTEGER);
        CREATE TABLE handle (id TEXT);
        CREATE TABLE chat (display_name TEXT);
        CREATE TABLE chat_message_join (message_id INTEGER, chat_id INTEGER);
        CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
        INSERT INTO handle VALUES ('+14055550101');
        INSERT INTO chat VALUES ('Dele');
    """)
    for index in range(1, count + 1):
        conn.execute("INSERT INTO message VALUES (1, ?, ?, 0)", (f"Note {index}", index))
        message_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO chat_message_join VALUES (?, 1)", (message_id,))
    conn.commit()
    conn.close()
    return path


def test_read_new_drains_unchanged_45_row_backlog_in_20_20_5_pages(
    tmp_path: Path,
) -> None:
    store = IMessageStore(_incoming_message_db(tmp_path / "chat.db", 45))

    first, cursor = store.read_new(0)
    second, cursor = store.read_new(cursor)
    third, cursor = store.read_new(cursor)

    assert [len(first), len(second), len(third)] == [20, 20, 5]
    assert cursor == 45


def test_read_new_does_not_repeat_rows_after_unchanged_backlog_is_drained(
    tmp_path: Path,
) -> None:
    store = IMessageStore(_incoming_message_db(tmp_path / "chat.db", 45))
    cursor = 0
    observed_ids: list[int] = []

    for _ in range(3):
        rows, cursor = store.read_new(cursor)
        observed_ids.extend(row.rec_id for row in rows)
    after_drain, final_cursor = store.read_new(cursor)

    assert observed_ids == list(range(1, 46))
    assert len(observed_ids) == len(set(observed_ids))
    assert after_drain == []
    assert final_cursor == 45


def test_source_disappearance_is_a_health_failure_not_an_empty_poll(tmp_path: Path) -> None:
    path = _incoming_message_db(tmp_path / "chat.db", 1)
    store = IMessageStore(path)
    _, cursor = store.read_new(0)
    path.unlink()

    with pytest.raises(FileNotFoundError):
        store.read_new(cursor)


def test_recent_threads_marks_group_chats_non_replyable(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = tmp_path / "chat.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE message (handle_id INTEGER, text TEXT, date INTEGER, is_from_me INTEGER);
        CREATE TABLE handle (id TEXT);
        CREATE TABLE chat (display_name TEXT);
        CREATE TABLE chat_message_join (message_id INTEGER, chat_id INTEGER);
        CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
    """)
    conn.execute("INSERT INTO handle VALUES ('+14055550101')")
    conn.execute("INSERT INTO handle VALUES ('+14055550102')")
    conn.execute("INSERT INTO chat VALUES ('Dele')")
    conn.execute("INSERT INTO chat VALUES ('Project team')")
    conn.execute("INSERT INTO message VALUES (1, 'Direct note', 1, 0)")
    direct_message_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO chat_message_join VALUES (?, 1)", (direct_message_id,))
    conn.execute("INSERT INTO chat_handle_join VALUES (1, 1)")
    conn.execute("INSERT INTO message VALUES (1, 'Group note', 2, 0)")
    group_message_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO chat_message_join VALUES (?, 2)", (group_message_id,))
    conn.execute("INSERT INTO chat_handle_join VALUES (2, 1)")
    conn.execute("INSERT INTO chat_handle_join VALUES (2, 2)")
    conn.commit()
    conn.close()

    rows = IMessageStore(db).recent_threads()

    assert [(row.sender, row.replyable) for row in rows] == [
        ("Project team", False),
        ("Dele", True),
    ]


def test_recent_threads_preserves_distinct_chats_with_the_same_display_name(
    tmp_path: Path,
) -> None:
    db = tmp_path / "chat.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE message (handle_id INTEGER, text TEXT, date INTEGER, is_from_me INTEGER);
        CREATE TABLE handle (id TEXT);
        CREATE TABLE chat (display_name TEXT);
        CREATE TABLE chat_message_join (message_id INTEGER, chat_id INTEGER);
        CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
        INSERT INTO handle VALUES ('+14055550101');
        INSERT INTO handle VALUES ('+14055550102');
        INSERT INTO chat VALUES ('Alex');
        INSERT INTO chat VALUES ('Alex');
        INSERT INTO message VALUES (1, 'First chat', 1, 0);
        INSERT INTO chat_message_join VALUES (1, 1);
        INSERT INTO chat_handle_join VALUES (1, 1);
        INSERT INTO message VALUES (2, 'Second chat', 2, 0);
        INSERT INTO chat_message_join VALUES (2, 2);
        INSERT INTO chat_handle_join VALUES (2, 2);
    """)
    conn.commit()
    conn.close()

    rows = IMessageStore(db).recent_threads()

    assert [(row.chat_id, row.handle, row.text) for row in rows] == [
        (2, "+14055550102", "Second chat"),
        (1, "+14055550101", "First chat"),
    ]


def test_thread_messages_returns_incoming_and_outgoing_context_oldest_first(
    tmp_path: Path,
) -> None:
    db = tmp_path / "chat.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE message (handle_id INTEGER, text TEXT, date INTEGER, is_from_me INTEGER);
        CREATE TABLE handle (id TEXT);
        CREATE TABLE chat (display_name TEXT);
        CREATE TABLE chat_message_join (message_id INTEGER, chat_id INTEGER);
        CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
        INSERT INTO handle VALUES ('+14055550101');
        INSERT INTO chat VALUES ('Dele');
        INSERT INTO chat_handle_join VALUES (1, 1);
        INSERT INTO message VALUES (1, 'Are you coming?', 1, 0);
        INSERT INTO chat_message_join VALUES (1, 1);
        INSERT INTO message VALUES (1, 'On my way', 2, 1);
        INSERT INTO chat_message_join VALUES (2, 1);
        INSERT INTO message VALUES (1, 'Great', 3, 0);
        INSERT INTO chat_message_join VALUES (3, 1);
    """)
    conn.commit()
    conn.close()
    store = IMessageStore(db)
    thread = MessageThread(
        sender="Dele",
        handle="+14055550101",
        text="Great",
        at=3.0,
        chat_id=1,
    )

    messages = store.thread_messages(thread, limit=5)

    assert [(item.from_me, item.text) for item in messages] == [
        (False, "Are you coming?"),
        (True, "On my way"),
        (False, "Great"),
    ]


def test_thread_message_pages_include_metadata_and_attachment_only_rows(
    tmp_path: Path,
) -> None:
    db = tmp_path / "chat.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE message (
            handle_id INTEGER,
            text TEXT,
            date INTEGER,
            is_from_me INTEGER,
            is_read INTEGER,
            is_sent INTEGER,
            is_delivered INTEGER,
            date_delivered INTEGER,
            date_read INTEGER,
            cache_has_attachments INTEGER
        );
        CREATE TABLE handle (id TEXT);
        CREATE TABLE chat (display_name TEXT);
        CREATE TABLE chat_message_join (message_id INTEGER, chat_id INTEGER);
        CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
        INSERT INTO handle VALUES ('+14055550101');
        INSERT INTO chat VALUES ('Dele');
        INSERT INTO chat_handle_join VALUES (1, 1);
        INSERT INTO message VALUES (1, 'First', 1, 0, 0, 0, 0, 0, 0, 0);
        INSERT INTO chat_message_join VALUES (1, 1);
        INSERT INTO message VALUES (1, 'Sent', 2, 1, 1, 1, 0, 0, 0, 0);
        INSERT INTO chat_message_join VALUES (2, 1);
        INSERT INTO message VALUES (1, 'Delivered', 3, 1, 1, 1, 1, 3, 0, 0);
        INSERT INTO chat_message_join VALUES (3, 1);
        INSERT INTO message VALUES (1, 'Read', 4, 1, 1, 1, 1, 3, 4, 0);
        INSERT INTO chat_message_join VALUES (4, 1);
        INSERT INTO message VALUES (1, NULL, 5, 0, 0, 0, 0, 0, 0, 1);
        INSERT INTO chat_message_join VALUES (5, 1);
    """)
    conn.commit()
    conn.close()
    store = IMessageStore(db)
    thread = MessageThread(
        sender="Dele",
        handle="+14055550101",
        text="Attachment",
        at=5.0,
        chat_id=1,
    )

    recent = store.recent_threads()
    latest = store.thread_message_page(thread, limit=3)
    older = store.thread_message_page(
        thread,
        limit=3,
        before_row_id=min(item.row_id for item in latest.entries),
    )

    assert recent[0].text == ""
    assert recent[0].attachment_count == 1
    assert recent[0].unread is True
    assert latest.has_older is True
    assert [item.text for item in latest.entries] == ["", "Read", "Delivered"]
    assert latest.entries[0].attachment_count == 1
    assert latest.entries[0].unread is True
    assert latest.entries[1].delivery == "read"
    assert latest.entries[2].delivery == "delivered"
    assert older.has_older is False
    assert [(item.text, item.delivery, item.unread) for item in older.entries] == [
        ("Sent", "sent", False),
        ("First", None, True),
    ]
