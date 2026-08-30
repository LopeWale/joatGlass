"""iMessage channel — incoming messages as HUD cards (ROADMAP Phase 1).

Why a per-source channel rather than scraping notification banners: the
banner route is undocumented, differs per macOS release, and on this
machine (macOS 27) no banner surface is reachable at all — see
docs/MAC_APP.md §Notification sources. Messages' own store is stable, is
already covered by the app's Full Disk Access grant, and carries far more
than a banner ever could (real sender, full text, thread, attachments).

Read-only and immutable: the database is live and WAL-journaled, so a
writer-ish open could stall the user's Messages app. Never write.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .contacts import ContactResolver
from .notifications import Notification

log = logging.getLogger(__name__)

DEFAULT_DB = Path("~/Library/Messages/chat.db").expanduser()

# Apple epoch (2001-01-01) in unix seconds; `date` is nanoseconds since then
# on modern macOS, seconds on very old databases.
APPLE_EPOCH = 978_307_200
_READ_PAGE_SIZE = 20


@dataclass(frozen=True, slots=True)
class MessageThread:
    """One recent incoming conversation with its private send handle."""

    sender: str
    handle: str
    text: str
    at: float
    replyable: bool = True
    """False for group chats until Messages.app group targeting is proven."""
    chat_id: int | None = None
    """Private chat identity; never sent to the Hub or accepted from it."""
    unread: bool = False
    """True only when the newest incoming row is explicitly unread."""
    attachment_count: int = 0
    """Bounded attachment count; filenames and paths never leave the Mac."""


@dataclass(frozen=True, slots=True)
class MessageEntry:
    """One bounded conversation row for wearer-visible context."""

    text: str
    at: float
    from_me: bool
    row_id: int = 0
    unread: bool = False
    delivery: Literal["sent", "delivered", "read"] | None = None
    attachment_count: int = 0


@dataclass(frozen=True, slots=True)
class MessageHistoryPage:
    """One newest-first, cursor-addressed conversation page."""

    entries: tuple[MessageEntry, ...]
    has_older: bool


def _unix_time(raw: int | None) -> float:
    if not raw:
        return 0.0
    seconds = raw / 1e9 if raw > 1e11 else float(raw)
    return seconds + APPLE_EPOCH


class IMessageStore:
    """Newest-first tail of incoming messages. Watermark is a ROWID."""

    def __init__(self, db_path: Path = DEFAULT_DB) -> None:
        self._path = db_path
        self._last_change = 0.0
        # An mtime is only an edge signal. Once an edge reveals more than one
        # page, the returned ROWID becomes the durable in-process signal that
        # the caller must keep draining even if the filesystem is unchanged.
        self._drain_after: int | None = None
        self._last_health_probe = 0.0
        # Messages stores only the raw handle; the wearer needs a name.
        self._contacts = ContactResolver()

    @property
    def available(self) -> bool:
        return self._path.is_file()

    def baseline(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()
            return int(row[0])

    def _changed(self) -> bool:
        """Cheap gate: has the store been written since we last looked?

        SQLite in WAL mode lands new rows in chat.db-wal, so that file's
        mtime moves on every delivery. Comparing an mtime costs one stat;
        opening and querying 350k rows costs far more. Polling stays at a
        1s cadence for latency, but the expensive half only runs when there
        is genuinely something new — subscribe-like behaviour without
        depending on FSEvents in the engine process.
        """
        newest = 0.0
        observed = False
        for suffix in ("", "-wal"):
            try:
                newest = max(newest, (self._path.parent /
                                      (self._path.name + suffix)).stat().st_mtime)
                observed = True
            except OSError:
                continue
        if not observed:
            raise FileNotFoundError(f"Messages store is unavailable at {self._path}")
        if newest > self._last_change:
            self._last_change = newest
            return True
        return False

    def read_new(self, watermark: Any) -> tuple[list[Notification], Any]:
        last = int(watermark)
        if self._drain_after != last and not self._changed():
            # An mtime is an edge signal, not a health signal. Periodically
            # prove the read permission still exists so the mux can promote
            # best-effort banners if Full Disk Access is revoked while idle.
            now = time.monotonic()
            if now - self._last_health_probe >= 30:
                with self._connect() as conn:
                    conn.execute("SELECT 1 FROM message LIMIT 1").fetchone()
                self._last_health_probe = now
            return [], last

        # Preserve this continuation before opening the live store. If a
        # transient read fails after _changed() consumes the mtime edge, a
        # retry with the same watermark can still recover the page.
        self._drain_after = last
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT m.ROWID,
                       COALESCE(m.text, ''),
                       COALESCE(h.id, ''),
                       m.date,
                       COALESCE(c.display_name, '')
                FROM message m
                LEFT JOIN handle h ON m.handle_id = h.ROWID
                LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                LEFT JOIN chat c ON c.ROWID = cmj.chat_id
                WHERE m.ROWID > ?
                  AND m.is_from_me = 0
                  AND m.text IS NOT NULL AND m.text != ''
                ORDER BY m.ROWID
                LIMIT ?
                """,
                (last, _READ_PAGE_SIZE + 1),
            ).fetchall()

        out: list[Notification] = []
        for rowid, text, sender, date, chat_name in rows[:_READ_PAGE_SIZE]:
            last = max(last, int(rowid))
            who = chat_name or self._contacts.name_for(sender) or "unknown"
            out.append(Notification(
                rec_id=int(rowid),
                app="Messages",
                title=who,
                subtitle="",
                body=text,
                delivered_unix=_unix_time(date),
                native_id=str(rowid),
            ))
        self._drain_after = last if len(rows) > _READ_PAGE_SIZE else None
        return out, last

    def recent(self, limit: int = 5) -> list[tuple[str, str, float]]:
        """Newest incoming message per sender: (sender, text, unix_time).

        This data was parsed and sitting here while "who messaged me?" was
        answered by a model that had never seen it. Tool-facing: the reply
        loop calls this when the wearer asks about their messages.
        """
        return [(row.sender, row.text, row.at) for row in self.recent_threads(limit)]

    def recent_threads(self, limit: int = 5) -> list[MessageThread]:
        """Newest incoming message per sender, retaining the send handle.

        The handle remains Mac-side. The HUD receives only sender and preview,
        so a forged plugin row cannot redirect a reply to a different address.
        """
        limit = max(1, min(int(limit), 10))
        with self._connect() as conn:
            columns = self._columns(conn, "message")
            attachment_expr = (
                "COALESCE(m.cache_has_attachments, 0)"
                if "cache_has_attachments" in columns else "0"
            )
            unread_expr = (
                "CASE WHEN COALESCE(m.is_from_me, 0) = 0 "
                "THEN CASE WHEN COALESCE(m.is_read, 1) = 0 THEN 1 ELSE 0 END "
                "ELSE 0 END"
                if "is_read" in columns else "0"
            )
            visible_clause = "m.text IS NOT NULL AND m.text != ''"
            if "cache_has_attachments" in columns:
                visible_clause = (
                    f"(({visible_clause}) OR COALESCE(m.cache_has_attachments, 0) != 0)"
                )
            rows = conn.execute(
                f"""
                SELECT COALESCE(h.id, ''),
                       COALESCE(m.text, ''),
                       m.date,
                       COALESCE(c.display_name, ''),
                       COALESCE(c.ROWID, 0),
                       COALESCE((
                           SELECT COUNT(*)
                           FROM chat_handle_join chj
                           WHERE chj.chat_id = c.ROWID
                       ), 1),
                       {unread_expr},
                       {attachment_expr}
                FROM message m
                LEFT JOIN handle h ON m.handle_id = h.ROWID
                LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                LEFT JOIN chat c ON c.ROWID = cmj.chat_id
                WHERE m.is_from_me = 0
                  AND {visible_clause}
                ORDER BY m.ROWID DESC
                LIMIT 60
                """
            ).fetchall()
        out: list[MessageThread] = []
        seen: set[tuple[str, str | int]] = set()
        for (
            handle, text, raw_date, display, chat_id, participant_count,
            unread, attachment_count,
        ) in rows:
            sender = display or self._contacts.name_for(handle)
            identity: tuple[str, str | int] = (
                ("chat", int(chat_id)) if int(chat_id) > 0 else ("handle", str(handle))
            )
            if identity in seen:
                continue  # newest preview per stable conversation
            seen.add(identity)
            out.append(MessageThread(
                sender=sender,
                handle=str(handle),
                text=str(text),
                at=_unix_time(raw_date),
                replyable=int(participant_count) <= 1,
                chat_id=int(chat_id) if int(chat_id) > 0 else None,
                unread=bool(unread),
                attachment_count=1 if int(attachment_count) > 0 else 0,
            ))
            if len(out) >= limit:
                break
        return out

    def thread_messages(
        self,
        thread: MessageThread,
        limit: int = 5,
    ) -> list[MessageEntry]:
        """Compatibility context for one Mac-owned chat, oldest row first."""
        page = self.thread_message_page(thread, limit=limit)
        return list(reversed(page.entries))

    def thread_message_page(
        self,
        thread: MessageThread,
        *,
        limit: int = 8,
        before_row_id: int | None = None,
    ) -> MessageHistoryPage:
        """Newest-first text/attachment page for one Mac-owned conversation."""
        limit = max(1, min(int(limit), 16))
        if thread.chat_id is None:
            return MessageHistoryPage(entries=(), has_older=False)
        with self._connect() as conn:
            columns = self._columns(conn, "message")
            unread_expr = (
                "CASE WHEN COALESCE(m.is_from_me, 0) = 0 "
                "THEN CASE WHEN COALESCE(m.is_read, 1) = 0 THEN 1 ELSE 0 END "
                "ELSE 0 END"
                if "is_read" in columns else "0"
            )
            sent_expr = "COALESCE(m.is_sent, 0)" if "is_sent" in columns else "0"
            delivered_expr = (
                "COALESCE(m.is_delivered, 0)" if "is_delivered" in columns else "0"
            )
            date_delivered_expr = (
                "COALESCE(m.date_delivered, 0)" if "date_delivered" in columns else "0"
            )
            date_read_expr = (
                "COALESCE(m.date_read, 0)" if "date_read" in columns else "0"
            )
            attachment_expr = (
                "COALESCE(m.cache_has_attachments, 0)"
                if "cache_has_attachments" in columns else "0"
            )
            visible_clause = "m.text IS NOT NULL AND m.text != ''"
            if "cache_has_attachments" in columns:
                visible_clause = (
                    f"(({visible_clause}) OR COALESCE(m.cache_has_attachments, 0) != 0)"
                )
            before_clause = " AND m.ROWID < ?" if before_row_id is not None else ""
            params: tuple[int, ...] = (
                (thread.chat_id, int(before_row_id), limit + 1)
                if before_row_id is not None
                else (thread.chat_id, limit + 1)
            )
            rows = conn.execute(
                f"""
                SELECT m.ROWID,
                       COALESCE(m.text, ''),
                       m.date,
                       COALESCE(m.is_from_me, 0),
                       {unread_expr},
                       {sent_expr},
                       {delivered_expr},
                       {date_delivered_expr},
                       {date_read_expr},
                       {attachment_expr}
                FROM message m
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                WHERE cmj.chat_id = ?
                  AND {visible_clause}
                  {before_clause}
                ORDER BY m.ROWID DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        entries: list[MessageEntry] = []
        for (
            row_id, text, raw_date, from_me, unread, sent, delivered,
            date_delivered, date_read, attachment_count,
        ) in rows[:limit]:
            delivery: Literal["sent", "delivered", "read"] | None = None
            if bool(from_me):
                if int(date_read) > 0:
                    delivery = "read"
                elif bool(delivered) or int(date_delivered) > 0:
                    delivery = "delivered"
                elif bool(sent):
                    delivery = "sent"
            entries.append(MessageEntry(
                text=str(text),
                at=_unix_time(raw_date),
                from_me=bool(from_me),
                row_id=int(row_id),
                unread=bool(unread),
                delivery=delivery,
                attachment_count=1 if int(attachment_count) > 0 else 0,
            ))
        return MessageHistoryPage(entries=tuple(entries), has_older=len(rows) > limit)

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        """Feature-detect optional Apple schema fields without assuming a release."""
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}

    def _connect(self) -> sqlite3.Connection:
        # immutable=1: the store is live and WAL-journaled; a normal open can
        # take locks that stall Messages itself.
        return sqlite3.connect(f"file:{self._path}?immutable=1", uri=True)
