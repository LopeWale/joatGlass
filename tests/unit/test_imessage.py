"""Messages store keeps reply targeting truthful and read-only."""

from __future__ import annotations

import sqlite3
import json
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from joat.channels.imessage import IMessageStore, MessageThread
from joat.channels.message_body import MAX_ATTRIBUTED_BODY_BYTES, MESSAGE_TEXT_UNAVAILABLE

BODY_FIXTURES = Path(__file__).parents[1] / "fixtures/messages/typedstream"


def _attributed_message_db(path: Path, rows: list[tuple[str | None, bytes, int]]) -> Path:
    db = _incoming_message_db(path, 0)
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE message ADD COLUMN attributedBody BLOB")
        conn.execute("ALTER TABLE message ADD COLUMN cache_has_attachments INTEGER DEFAULT 0")
        for ordinal, (plain, blob, attachments) in enumerate(rows, 1):
            conn.execute(
                "INSERT INTO message VALUES (1, ?, ?, 0, ?, ?)",
                (plain, ordinal, blob, attachments),
            )
            conn.execute("INSERT INTO chat_message_join VALUES (?, 1)", (ordinal,))
    return db


@pytest.mark.parametrize("name", ["AttributedBodyTextOnly", "LongMessage", "EmojiBoldUnderline", "MultiPart"])
def test_every_messages_reader_recovers_the_same_complete_attributed_body(
    tmp_path: Path, name: str,
) -> None:
    blob = (BODY_FIXTURES / name).read_bytes()
    expected = json.loads((BODY_FIXTURES / "expected.json").read_text())[name]
    path = _attributed_message_db(tmp_path / "chat.db", [(None, blob, 0)])
    store = IMessageStore(path)

    thread = store.recent_threads()[0]
    assert thread.text == expected
    assert store.recent()[0][1] == expected
    page = store.thread_message_page(thread)
    assert page.entries[0].text == expected
    assert store.thread_messages(thread)[0].text == expected
    fresh, watermark = store.read_new(0)
    assert [(item.rec_id, item.body) for item in fresh] == [(1, expected)]
    assert watermark == 1


def test_bad_archive_is_visible_without_aborting_later_messages(tmp_path: Path) -> None:
    valid = (BODY_FIXTURES / "AttributedBodyTextOnly").read_bytes()
    path = _attributed_message_db(tmp_path / "chat.db", [
        (None, valid, 0), (None, b"malformed", 1), ("  exact\n plain  ", b"bad", 0),
    ])
    store = IMessageStore(path)
    thread = store.recent_threads()[0]
    assert thread.text == "  exact\n plain  "
    entries = store.thread_message_page(thread).entries
    assert [row.text for row in entries] == ["  exact\n plain  ", MESSAGE_TEXT_UNAVAILABLE, "Noter test"]
    assert entries[1].attachment_count == 1
    fresh, watermark = store.read_new(0)
    assert [row.body for row in fresh] == ["Noter test", MESSAGE_TEXT_UNAVAILABLE, "  exact\n plain  "]
    assert watermark == 3


def test_marker_only_archives_use_attachment_metadata_or_unavailable_text(tmp_path: Path) -> None:
    from joat.services.capabilities import CapabilityRouter

    blank = (BODY_FIXTURES / "Blank").read_bytes()
    marker_archives = [
        blank.replace(b"\x84\x01+\x00", b"\x84\x01+\x03" + marker.encode(), 1)
        for marker in ("\ufffc", "\ufffd")
    ]
    path = _attributed_message_db(tmp_path / "chat.db", [
        (None, marker_archives[0], 1), (None, marker_archives[1], 0),
    ])
    store = IMessageStore(path)
    thread = store.recent_threads()[0]
    assert thread.text == MESSAGE_TEXT_UNAVAILABLE
    assert "\ufffd" not in CapabilityRouter._message_list_row(thread)
    replacement, attachment = store.thread_message_page(thread).entries
    assert replacement.text == MESSAGE_TEXT_UNAVAILABLE
    assert attachment.text == ""
    assert attachment.attachment_count == 1
    assert CapabilityRouter._message_history_row(thread, attachment).endswith(" · Attachment")
    fresh, watermark = store.read_new(0)
    assert [row.body for row in fresh] == ["Attachment", MESSAGE_TEXT_UNAVAILABLE]
    assert watermark == 2


def test_blank_archives_advance_notification_and_history_cursors(tmp_path: Path) -> None:
    blank = (BODY_FIXTURES / "Blank").read_bytes()
    valid = (BODY_FIXTURES / "AttributedBodyTextOnly").read_bytes()
    path = _attributed_message_db(tmp_path / "chat.db", [(None, blank, 0)] * 20 + [(None, valid, 0)])
    store = IMessageStore(path)

    first, watermark = store.read_new(0)
    assert first == []
    assert watermark == 20
    second, watermark = store.read_new(watermark)
    assert [row.body for row in second] == ["Noter test"]
    assert watermark == 21
    assert store.read_new(watermark) == ([], 21)
    thread = store.recent_threads()[0]
    page = store.thread_message_page(thread, limit=8, before_row_id=21)
    assert page.has_older
    assert [row.row_id for row in page.entries] == list(range(20, 12, -1))
    assert all(row.text == "" for row in page.entries)
    older = store.thread_message_page(thread, limit=8, before_row_id=page.entries[-1].row_id)
    assert [row.row_id for row in older.entries] == list(range(12, 4, -1))


def test_oversized_archives_are_not_materialized_by_messages_queries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from joat.channels import imessage

    size = MAX_ATTRIBUTED_BODY_BYTES + 1
    path = _attributed_message_db(tmp_path / "chat.db", [(None, b"x" * size, 0)])
    seen: list[tuple[object, int | None]] = []
    original = imessage.decode_message_body

    def inspect(plain: object, attributed: object, *, attributed_size: int | None = None):  # type: ignore[no-untyped-def]
        seen.append((attributed, attributed_size))
        return original(plain, attributed, attributed_size=attributed_size)

    monkeypatch.setattr(imessage, "decode_message_body", inspect)
    store = IMessageStore(path)
    thread = store.recent_threads()[0]
    assert thread.text == MESSAGE_TEXT_UNAVAILABLE
    assert store.thread_message_page(thread).entries[0].text == MESSAGE_TEXT_UNAVAILABLE
    assert store.read_new(0)[0][0].body == MESSAGE_TEXT_UNAVAILABLE
    assert seen == [(None, size)] * 3


@pytest.fixture(autouse=True)
def synthetic_contacts_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("joat.channels.contacts.ContactResolver._load", lambda _self: {})


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


def _commit_incoming_message(writer: sqlite3.Connection, number: int) -> None:
    writer.execute(
        "INSERT INTO message VALUES (1, ?, ?, 0)", (f"Note {number}", number),
    )
    row_id = writer.execute("SELECT last_insert_rowid()").fetchone()[0]
    writer.execute("INSERT INTO chat_message_join VALUES (?, 1)", (row_id,))
    writer.commit()


@pytest.fixture
def live_wal_messages(tmp_path: Path) -> Iterator[tuple[Path, sqlite3.Connection]]:
    path = _incoming_message_db(tmp_path / "chat.db", 1)
    with closing(sqlite3.connect(path)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        checkpointed = path.read_bytes()
        _commit_incoming_message(writer, 2)
        # Keep the writer open: closing the last connection can checkpoint
        # the row, concealing the live-store failure this fixture exercises.
        assert path.with_name("chat.db-wal").stat().st_size > 0
        assert path.read_bytes() == checkpointed
        yield path, writer


@pytest.mark.parametrize("reader", ["baseline", "read_new", "recent", "history"])
def test_message_readers_include_committed_live_wal_rows(
    live_wal_messages: tuple[Path, sqlite3.Connection], reader: str,
) -> None:
    path, _writer = live_wal_messages
    store = IMessageStore(path)
    wal_path = path.with_name("chat.db-wal")
    before = (path.read_bytes(), wal_path.read_bytes())

    if reader == "baseline":
        assert store.baseline() == 2
    elif reader == "read_new":
        rows, cursor = store.read_new(1)
        assert [(row.rec_id, row.body) for row in rows] == [(2, "Note 2")]
        assert cursor == 2
    elif reader == "recent":
        assert [thread.text for thread in store.recent_threads()] == ["Note 2"]
    else:
        thread = MessageThread("Dele", "+14055550101", "Note 2", 2.0, chat_id=1)
        page = store.thread_message_page(thread)
        assert [(row.row_id, row.text) for row in page.entries] == [
            (2, "Note 2"), (1, "Note 1"),
        ]

    assert (path.read_bytes(), wal_path.read_bytes()) == before


def test_message_reader_observes_later_commits_to_live_wal(
    live_wal_messages: tuple[Path, sqlite3.Connection],
) -> None:
    path, writer = live_wal_messages
    store = IMessageStore(path)
    assert [thread.text for thread in store.recent_threads()] == ["Note 2"]

    _commit_incoming_message(writer, 3)

    thread = store.recent_threads()[0]
    assert thread.text == "Note 3"
    assert store.baseline() == 3
    assert [row.text for row in store.thread_message_page(thread).entries] == [
        "Note 3", "Note 2", "Note 1",
    ]


def test_message_connection_remains_read_only_with_live_wal(
    live_wal_messages: tuple[Path, sqlite3.Connection],
) -> None:
    path, writer = live_wal_messages
    wal_path = path.with_name("chat.db-wal")
    before = (path.read_bytes(), wal_path.read_bytes())

    with closing(IMessageStore(path)._connect()) as reader:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("DELETE FROM message")
        assert reader.total_changes == 0

    assert writer.execute("SELECT COUNT(*) FROM message").fetchone()[0] == 2
    assert (path.read_bytes(), wal_path.read_bytes()) == before


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


def test_group_without_chat_guid_never_uses_its_latest_participant(tmp_path) -> None:  # type: ignore[no-untyped-def]
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
    assert rows[0].is_group is True
    assert rows[0].reply_target is None


def test_recent_threads_target_existing_groups_by_chat_guid(tmp_path: Path) -> None:
    db = _incoming_message_db(tmp_path / "chat.db", 1)
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE chat ADD COLUMN guid TEXT")
        conn.execute("UPDATE chat SET guid = 'iMessage;-;direct-thread'")
        conn.executescript("""
            INSERT INTO handle VALUES ('+14055550102');
            INSERT INTO chat VALUES ('Project team', 'iMessage;+;group-thread');
            INSERT INTO chat_handle_join VALUES (2, 1);
            INSERT INTO chat_handle_join VALUES (2, 2);
            INSERT INTO message VALUES (2, 'Newest from another participant', 2, 0);
            INSERT INTO chat_message_join VALUES (2, 2);
        """)

    group, direct = IMessageStore(db).recent_threads()

    assert group.replyable is True
    assert group.is_group is True
    assert group.chat_id == 2
    assert group.handle == "+14055550102"
    assert group.reply_target == ("chat", "iMessage;+;group-thread")
    assert direct.reply_target == ("chat", "iMessage;-;direct-thread")
    assert direct.is_group is False


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
