"""Messages store keeps reply targeting truthful and read-only."""

from __future__ import annotations

import sqlite3

from joat.channels.imessage import IMessageStore


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
