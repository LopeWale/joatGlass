"""Messages store keeps reply targeting truthful and read-only."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from joat.channels.imessage import IMessageStore


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
