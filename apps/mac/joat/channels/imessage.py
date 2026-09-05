"""iMessage channel — incoming messages as HUD cards (ROADMAP Phase 1).

Why a per-source channel rather than scraping notification banners: the
banner route is undocumented, differs per macOS release, and on this
machine (macOS 27) no banner surface is reachable at all — see
docs/MAC_APP.md §Notification sources. Messages' own store is stable, is
already covered by the app's Full Disk Access grant, and carries far more
than a banner ever could (real sender, full text, thread, attachments).

Read-only and WAL-aware: committed deliveries may still be in the live
journal rather than the main database file. Never write.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .contacts import ContactResolver
from .message_body import MAX_ATTRIBUTED_BODY_BYTES, decode_message_body
from .notifications import Notification

log = logging.getLogger(__name__)

DEFAULT_DB = Path("~/Library/Messages/chat.db").expanduser()

# Apple epoch (2001-01-01) in unix seconds; `date` is nanoseconds since then
# on modern macOS, seconds on very old databases.
APPLE_EPOCH = 978_307_200
_READ_PAGE_SIZE = 20


@dataclass(frozen=True, slots=True)
class MessageThread:
    """One recent incoming conversation with its private send identity."""

    sender: str
    handle: str
    text: str
    at: float
    replyable: bool = True
    """Whether the Mac has a concrete outbound target for this conversation."""
    chat_id: int | None = None
    """Private chat identity; never sent to the Hub or accepted from it."""
    unread: bool = False
    """True only when the newest incoming row is explicitly unread."""
    attachment_count: int = 0
    """Bounded attachment count; filenames and paths never leave the Mac."""
    chat_guid: str | None = None
    """Messages.app chat identity, distinct from SQLite ROWID and sender handle."""
    is_group: bool = False

    @property
    def reply_target(self) -> tuple[Literal["participant", "chat"], str] | None:
        """Mac-owned send identity; never substitute a group participant."""
        if not self.replyable:
            return None
        if self.chat_guid and self.chat_guid.strip():
            return ("chat", self.chat_guid)
        if not self.is_group and self.handle.strip():
            return ("participant", self.handle)
        return None


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
    sender: str | None = None
    """Incoming participant display label, independent of the private reply target."""


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
            columns = self._columns(conn, "message")
            body_expr, body_size_expr, visible_clause = self._body_sql(columns)
            attachment_expr = (
                "COALESCE(m.cache_has_attachments, 0)"
                if "cache_has_attachments" in columns else "0"
            )
            rows = conn.execute(
                f"""
                SELECT m.ROWID,
                       COALESCE(m.text, ''),
                       COALESCE(h.id, ''),
                       m.date,
                       COALESCE(c.display_name, ''),
                       {body_expr},
                       {body_size_expr},
                       {attachment_expr}
                FROM message m
                LEFT JOIN handle h ON m.handle_id = h.ROWID
                LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                LEFT JOIN chat c ON c.ROWID = cmj.chat_id
                WHERE m.ROWID > ?
                  AND m.is_from_me = 0
                  AND {visible_clause}
                ORDER BY m.ROWID
                LIMIT ?
                """,
                (last, _READ_PAGE_SIZE + 1),
            ).fetchall()

        out: list[Notification] = []
        for rowid, plain, sender, date, chat_name, body, body_size, attachments in rows[:_READ_PAGE_SIZE]:
            # A blank archive still consumes its candidate ROWID. Otherwise a
            # page of blank bodies can strand later deliveries forever.
            last = max(last, int(rowid))
            text = decode_message_body(plain, body, attributed_size=int(body_size)).text
            if not text and int(attachments) > 0:
                text = "Attachment"
            if not text:
                continue
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

    # A banner usually appears within seconds of the chat.db row, so the
    # tight window resolves almost everything. But macOS re-shows and stacks
    # banners, and the watcher can read one that lingered, which leaves the
    # timestamps hours apart — the wearer keeps a cut message for a reason
    # they cannot see. The wide pass covers that, and pays for the looser
    # time bound with a much longer prefix: 24 characters of an exact opening
    # that still matches exactly one message.
    _TIGHT_WINDOW_S = 300.0
    _WIDE_WINDOW_S = 86_400.0
    _MIN_HEAD_CHARS = 12
    _WIDE_MIN_HEAD_CHARS = 24

    def full_text_near(self, preview: str, at: float) -> str | None:
        """The complete message behind a TRUNCATED banner preview, or None.

        macOS renders notification banners with an elided body ("Hey! Our
        team could use…") and the Accessibility watcher can only read what is
        rendered — so the wearer's own message arrived pre-cut, and no display
        budget could undo that (their report, 2026-09-05: "Can I not see full
        messages?"). chat.db has the whole text; this finds it.

        Matched by PREFIX within a time window, not by id: an AX banner
        carries no message identifier. A prefix that matches more than one
        candidate is ambiguous and returns None rather than risk showing the
        wrong person's message. Read-only, and nothing leaves the Mac that
        the banner did not already carry.
        """
        head = preview.strip().rstrip("…").rstrip(".").strip()
        # Too short to identify a message; a 3-character prefix would match
        # half the archive.
        if len(head) < self._MIN_HEAD_CHARS or at <= 0:
            return None
        found = self._match_within(head, preview, at, self._TIGHT_WINDOW_S)
        if found is not None or len(head) < self._WIDE_MIN_HEAD_CHARS:
            return found
        return self._match_within(head, preview, at, self._WIDE_WINDOW_S)

    def _match_within(
        self, head: str, preview: str, at: float, window_s: float,
    ) -> str | None:
        # `message.date` is nanoseconds since the Apple epoch on modern
        # macOS and SECONDS on older databases — _unix_time reads both, so
        # the window must cover both or a seconds-schema store silently
        # matches nothing (measured: candidates=0 on every lookup).
        low_ns = int((at - window_s - APPLE_EPOCH) * 1e9)
        high_ns = int((at + window_s - APPLE_EPOCH) * 1e9)
        low_s = int(at - window_s - APPLE_EPOCH)
        high_s = int(at + window_s - APPLE_EPOCH)
        try:
            with self._connect() as conn:
                columns = self._columns(conn, "message")
                body_expr, body_size_expr, visible_clause = self._body_sql(columns)
                rows = conn.execute(
                    f"""
                    SELECT COALESCE(m.text, ''), {body_expr}, {body_size_expr}
                    FROM message m
                    WHERE m.is_from_me = 0
                      AND (m.date BETWEEN ? AND ? OR m.date BETWEEN ? AND ?)
                      AND {visible_clause}
                    ORDER BY m.date DESC
                    LIMIT 50
                    """,
                    (low_ns, high_ns, low_s, high_s),
                ).fetchall()
        except sqlite3.Error as exc:
            log.info("chat.db enrichment unavailable (%s)", type(exc).__name__)
            return None

        matches = []
        for plain, body, body_size in rows:
            text = decode_message_body(plain, body, attributed_size=int(body_size)).text
            if text and text.startswith(head) and len(text) > len(preview.strip()):
                matches.append(text)
        if len(matches) != 1:
            # Counts only — never the text. A silent decline here is
            # indistinguishable from "no enrichment wired", which is the kind
            # of blind spot that costs an afternoon.
            log.info(
                "chat.db enrichment declined candidates=%d matches=%d "
                "head_chars=%d window_s=%.0f",
                len(rows), len(matches), len(head), window_s,
            )
            return None
        return matches[0]

    def recent(self, limit: int = 5) -> list[tuple[str, str, float]]:
        """Newest incoming message per sender: (sender, text, unix_time).

        This data was parsed and sitting here while "who messaged me?" was
        answered by a model that had never seen it. Tool-facing: the reply
        loop calls this when the wearer asks about their messages.
        """
        return [(row.sender, row.text, row.at) for row in self.recent_threads(limit)]

    def unread_count(self) -> int:
        """Incoming messages Messages.app itself still marks unread.

        The Overview's "unread" number counted retained notification cards
        (the audit's O1): it included battery cards and never went down when
        the wearer read a message on their phone. `message.is_read` is the
        app's own flag; a schema without it answers zero rather than a guess.
        """
        with self._connect() as conn:
            columns = self._columns(conn, "message")
            if "is_read" not in columns:
                return 0
            _body_expr, _body_size_expr, visible_clause = self._body_sql(columns)
            if "cache_has_attachments" in columns:
                visible_clause = (
                    f"(({visible_clause}) OR COALESCE(m.cache_has_attachments, 0) != 0)"
                )
            row = conn.execute(
                f"""
                SELECT COUNT(*)
                FROM message m
                WHERE COALESCE(m.is_from_me, 0) = 0
                  AND COALESCE(m.is_read, 1) = 0
                  AND {visible_clause}
                """
            ).fetchone()
        return int(row[0]) if row else 0

    def recent_threads(self, limit: int = 5) -> list[MessageThread]:
        """Newest incoming message per conversation, retaining its send target.

        Send identities remain Mac-side. The HUD receives only sender and preview,
        so a forged plugin row cannot redirect a reply to a different address.
        """
        limit = max(1, min(int(limit), 10))
        with self._connect() as conn:
            columns = self._columns(conn, "message")
            chat_columns = self._columns(conn, "chat")
            guid_expr = "COALESCE(c.guid, '')" if "guid" in chat_columns else "''"
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
            body_expr, body_size_expr, visible_clause = self._body_sql(columns)
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
                       {attachment_expr},
                       {guid_expr},
                       {body_expr},
                       {body_size_expr}
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
            handle, plain, raw_date, display, chat_id, participant_count,
            unread, attachment_count, chat_guid, body, body_size,
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
                text=decode_message_body(plain, body, attributed_size=int(body_size)).text,
                at=_unix_time(raw_date),
                replyable=bool(str(chat_guid).strip()) or int(participant_count) <= 1,
                chat_id=int(chat_id) if int(chat_id) > 0 else None,
                unread=bool(unread),
                attachment_count=1 if int(attachment_count) > 0 else 0,
                chat_guid=str(chat_guid) if str(chat_guid).strip() else None,
                is_group=int(participant_count) > 1,
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
            body_expr, body_size_expr, visible_clause = self._body_sql(columns)
            if "cache_has_attachments" in columns:
                visible_clause = (
                    f"(({visible_clause}) OR COALESCE(m.cache_has_attachments, 0) != 0)"
                )
            before_clause = " AND m.ROWID < ?" if before_row_id is not None else ""
            sender_expr = "COALESCE(h.id, '')" if "handle_id" in columns else "''"
            sender_join = (
                "LEFT JOIN handle h ON h.ROWID = m.handle_id"
                if "handle_id" in columns else ""
            )
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
                       {attachment_expr},
                       {sender_expr},
                       {body_expr},
                       {body_size_expr}
                FROM message m
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
                {sender_join}
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
            row_id, plain, raw_date, from_me, unread, sent, delivered,
            date_delivered, date_read, attachment_count, handle, body, body_size,
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
                text=decode_message_body(plain, body, attributed_size=int(body_size)).text,
                at=_unix_time(raw_date),
                from_me=bool(from_me),
                row_id=int(row_id),
                unread=bool(unread),
                delivery=delivery,
                attachment_count=1 if int(attachment_count) > 0 else 0,
                sender=(self._contacts.name_for(str(handle)) or None) if not from_me else None,
            ))
        return MessageHistoryPage(entries=tuple(entries), has_older=len(rows) > limit)

    @staticmethod
    def _body_sql(columns: set[str]) -> tuple[str, str, str]:
        """Shared archive projection and candidate predicate for all readers."""
        plain_clause = "(m.text IS NOT NULL AND m.text != '')"
        if "attributedBody" not in columns:
            return "NULL", "0", plain_clause
        size = "COALESCE(length(m.attributedBody), 0)"
        # SQLite computes BLOB length without fetching its entire contents.
        # Also avoid fetching an archive when the preferred text is present.
        body = (
            f"CASE WHEN NOT {plain_clause} AND {size} <= {MAX_ATTRIBUTED_BODY_BYTES} "
            "THEN m.attributedBody ELSE NULL END"
        )
        return body, size, f"({plain_clause} OR {size} > 0)"

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        """Feature-detect optional Apple schema fields without assuming a release."""
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}

    def _connect(self) -> sqlite3.Connection:
        # Read committed WAL rows without granting write access. immutable=1
        # ignores the live journal and can hide deliveries until checkpoint.
        return sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
