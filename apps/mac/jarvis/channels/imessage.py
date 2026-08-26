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
from pathlib import Path
from typing import Any

from .notifications import Notification

log = logging.getLogger(__name__)

DEFAULT_DB = Path("~/Library/Messages/chat.db").expanduser()

# Apple epoch (2001-01-01) in unix seconds; `date` is nanoseconds since then
# on modern macOS, seconds on very old databases.
APPLE_EPOCH = 978_307_200


def _unix_time(raw: int | None) -> float:
    if not raw:
        return 0.0
    seconds = raw / 1e9 if raw > 1e11 else float(raw)
    return seconds + APPLE_EPOCH


class IMessageStore:
    """Newest-first tail of incoming messages. Watermark is a ROWID."""

    def __init__(self, db_path: Path = DEFAULT_DB) -> None:
        self._path = db_path

    @property
    def available(self) -> bool:
        return self._path.is_file()

    def baseline(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()
            return int(row[0])

    def read_new(self, watermark: Any) -> tuple[list[Notification], Any]:
        last = int(watermark)
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
                LIMIT 20
                """,
                (last,),
            ).fetchall()

        out: list[Notification] = []
        for rowid, text, sender, date, chat_name in rows:
            last = max(last, int(rowid))
            who = chat_name or sender or "unknown"
            out.append(Notification(
                rec_id=int(rowid),
                app="Messages",
                title=who,
                subtitle="",
                body=text,
                delivered_unix=_unix_time(date),
            ))
        return out, last

    def _connect(self) -> sqlite3.Connection:
        # immutable=1: the store is live and WAL-journaled; a normal open can
        # take locks that stall Messages itself.
        return sqlite3.connect(f"file:{self._path}?immutable=1", uri=True)
