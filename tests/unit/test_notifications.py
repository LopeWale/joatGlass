"""Notification channel tests against a fixture usernoted-style db."""

from __future__ import annotations

import asyncio
import plistlib
import sqlite3
from pathlib import Path

from jarvis.channels.cards import Urgency
from jarvis.channels.notifications import (
    COCOA_EPOCH_UNIX,
    Notification,
    NotificationChannel,
    NotificationStore,
    parse_triage,
)
from jarvis.core.interfaces import ReplyEngine


def make_db(path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE app (app_id INTEGER PRIMARY KEY, identifier TEXT)")
    conn.execute(
        "CREATE TABLE record (rec_id INTEGER PRIMARY KEY, app_id INTEGER, "
        "delivered_date REAL, data BLOB)"
    )
    for i, (app, title, subt, body) in enumerate(rows, start=1):
        conn.execute("INSERT OR IGNORE INTO app VALUES (?, ?)", (hash(app) % 1000, app))
        blob = plistlib.dumps(
            {"req": {"titl": title, "subt": subt, "body": body}},
            fmt=plistlib.FMT_BINARY,
        )
        conn.execute(
            "INSERT INTO record VALUES (?, ?, ?, ?)",
            (i, hash(app) % 1000, 800000000.0 + i, blob),
        )
    conn.commit()
    conn.close()


class TestStore:
    def test_reads_new_records_with_app_identity(self, tmp_path: Path) -> None:
        db = tmp_path / "db"
        make_db(db, [
            ("com.apple.MobileSMS", "Dele", "", "are you coming?"),
            ("com.spam.deals", "50% OFF", "", "limited time!"),
        ])
        store = NotificationStore(db)
        assert store.latest_rec_id() == 2
        fresh = store.read_since(0)
        assert [n.app for n in fresh] == ["com.apple.MobileSMS", "com.spam.deals"]
        assert fresh[0].title == "Dele" and fresh[0].body == "are you coming?"
        assert fresh[0].delivered_unix > COCOA_EPOCH_UNIX
        assert store.read_since(2) == []

    def test_malformed_blob_rows_are_skipped(self, tmp_path: Path) -> None:
        db = tmp_path / "db"
        make_db(db, [("com.a.b", "ok", "", "fine")])
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO record VALUES (99, 1, 0, ?)", (b"not a plist",))
        conn.commit(); conn.close()
        fresh = NotificationStore(db).read_since(0)
        assert [n.rec_id for n in fresh] == [1]


class TestTriageParse:
    def test_levels_and_summary(self) -> None:
        assert parse_triage("URGENT — Dele is waiting for a reply") == (
            Urgency.URGENT, "Dele is waiting for a reply")
        assert parse_triage("ignore - promo blast") == (Urgency.IGNORE, "promo blast")
        assert parse_triage("total nonsense") is None


class ScriptedTriage(ReplyEngine):
    def __init__(self, replies: dict[str, str]) -> None:
        self.replies = replies

    async def reply(self, text: str) -> str | None:
        for key, out in self.replies.items():
            if key in text:
                return out
        return "IGNORE — unmatched"


class TestChannel:
    async def test_polls_triages_and_delivers_with_urgency_prefix(self, tmp_path: Path) -> None:
        db = tmp_path / "db"
        make_db(db, [("com.seed.app", "old", "", "pre-existing")])
        delivered: list[str] = []
        channel = NotificationChannel(
            NotificationStore(db),
            ScriptedTriage({
                "are you coming": "URGENT — Dele asks if you are coming",
                "50% OFF": "IGNORE — promotional",
            }),
            delivered.append,
            poll_interval_s=0.01,
        )
        task = asyncio.get_running_loop().create_task(channel.run())
        await asyncio.sleep(0.05)  # channel snapshots rec_id=1 as baseline
        conn = sqlite3.connect(db)
        for rid, (app, title, body) in enumerate([
            ("com.apple.MobileSMS", "Dele", "are you coming?"),
            ("com.spam.deals", "50% OFF", "act now"),
        ], start=10):
            blob = plistlib.dumps({"req": {"titl": title, "body": body}}, fmt=plistlib.FMT_BINARY)
            conn.execute("INSERT INTO app VALUES (?, ?)", (rid, app))
            conn.execute("INSERT INTO record VALUES (?, ?, 800000001.0, ?)", (rid, rid, blob))
        conn.commit(); conn.close()
        await asyncio.sleep(0.1)
        channel.stop()
        task.cancel()
        # New contract: humanized channel, sender headline, two lines, and a
        # SHORT message shown verbatim rather than model-paraphrased. The
        # promo is still classified IGNORE by the model and never delivered.
        assert delivered == ["Dele · Messages\nare you coming?"]

    async def test_unreadable_store_goes_dormant_not_fatal(self, tmp_path: Path) -> None:
        channel = NotificationChannel(
            NotificationStore(tmp_path / "missing" / "db"), None, lambda c: None,
        )
        await channel.run()  # returns promptly instead of crashing


class TestManagerCards:
    async def test_idle_shows_now_active_queues_and_drains(self) -> None:
        from jarvis.core.protocol import HudText
        from jarvis.services.hud import HudWriter
        from jarvis.services.session import Session, SessionManager
        from tests.unit.test_session import SinkTransport

        transport = SinkTransport()

        class Gated(Session):
            def __init__(self) -> None:
                self.gate = asyncio.Event()
            async def run(self) -> None:
                await self.gate.wait()
            def cancel(self) -> None:
                self.gate.set()

        sess = Gated()
        mgr = SessionManager(HudWriter(transport), lambda t: sess)
        mgr.notify("[Mail] first card")
        await asyncio.sleep(0)
        assert any(isinstance(m, HudText) and "first card" in m.text for m in transport.sent)

        assert mgr.handle_transcript("debate something") is True
        await asyncio.sleep(0)
        mgr.notify("[SMS] queued while debating")
        assert not any("queued while debating" in getattr(m, "text", "") for m in transport.sent)
        sess.gate.set()
        await asyncio.sleep(0); await asyncio.sleep(0); await asyncio.sleep(0)
        assert any("queued while debating" in getattr(m, "text", "") for m in transport.sent)
