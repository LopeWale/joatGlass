"""Notification channel tests against a fixture usernoted-style db."""

from __future__ import annotations

import asyncio
import logging
import plistlib
import sqlite3
import time
from pathlib import Path

from joat.channels.cards import Card, Urgency
from joat.channels.notifications import (
    COCOA_EPOCH_UNIX,
    Notification,
    NotificationChannel,
    NotificationStore,
    parse_triage,
)
from joat.core.interfaces import ReplyEngine


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
    def test_card_taxonomy_agrees_with_the_observation_store(self) -> None:
        """Two names for one fact is worse than one imprecise name.

        The card's event_class and the observation store's row describe the
        SAME notification, and used to be derived independently — one from the
        humanized channel, one from the raw bundle id — so they disagreed
        (com.apple.iCal is not the substring "calendar", and an email was being
        called an incoming text message). They now share one classifier; this
        pins that they cannot drift apart again.
        """
        from joat.channels.event_taxonomy import classify_event_class
        from joat.persistence.notifications import NotificationObservationStore

        cases = [
            ("com.apple.MobileSMS", "Dele", "messages.incoming_text"),
            ("com.apple.mail", "Bank statement", "mail.banner"),
            ("com.apple.iCal", "Standup", "calendar.banner"),
            ("com.apple.reminders", "Milk", "reminders.banner"),
            ("com.spam.deals", "50% off", "notifications.banner"),
            # Deliberate: chat apps arrive as banners; incoming_text is
            # reserved for the authoritative iMessage lane. Display still
            # shows the sender via PERSON_CHANNELS — taxonomy and display
            # are separate decisions.
            ("com.tinyspeck.slackmacgap", "Adaeze", "notifications.banner"),
            ("com.hnc.Discord", "Marcus", "notifications.banner"),
        ]
        for app, title, expected in cases:
            notification = Notification(
                rec_id=1, app=app, title=title, subtitle="", body="body",
                delivered_unix=0.0,
            )
            assert classify_event_class(app, title) == expected
            assert NotificationObservationStore.event_class(notification) == expected

        # An email is not an incoming text message.
        assert classify_event_class("com.apple.mail", "Bank") != "messages.incoming_text"

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
        delivered: list[Card] = []
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
        # Delivered "now" in Cocoa seconds: a card older than STALE_CARD_S
        # is logged and kept for the tray but never interrupts the lens, so
        # a fixed 2026-05 stamp would (correctly) deliver nothing here.
        delivered_cocoa = time.time() - COCOA_EPOCH_UNIX
        for rid, (app, title, body) in enumerate([
            ("com.apple.MobileSMS", "Dele", "are you coming?"),
            ("com.spam.deals", "50% OFF", "act now"),
        ], start=10):
            blob = plistlib.dumps({"req": {"titl": title, "body": body}}, fmt=plistlib.FMT_BINARY)
            conn.execute("INSERT INTO app VALUES (?, ?)", (rid, app))
            conn.execute(
                "INSERT INTO record VALUES (?, ?, ?, ?)", (rid, rid, delivered_cocoa, blob)
            )
        conn.commit(); conn.close()
        await asyncio.sleep(0.1)
        channel.stop()
        task.cancel()
        # New contract: humanized channel, sender headline, two lines, and a
        # SHORT message shown verbatim rather than model-paraphrased. The
        # promo is still classified IGNORE by the model and never delivered.
        assert [c.render() for c in delivered] == ["Dele · Messages\nare you coming?"]

    async def test_stale_card_is_withheld_from_the_lens(self) -> None:
        """A backlog draining after a reconnect must not seize the screen for
        something that happened minutes or days ago (measured 2026-09-08:
        15 of 109 delivered cards were over a minute old). The channel still
        delivers a card that just arrived."""
        delivered: list[Card] = []
        channel = NotificationChannel(
            None,  # type: ignore[arg-type] -- _handle is the unit under test
            None,
            delivered.append,
        )
        stale = Notification(
            rec_id=1, app="com.apple.MobileSMS", title="Dele", subtitle="",
            body="old news", delivered_unix=time.time() - NotificationChannel.STALE_CARD_S - 60,
        )
        fresh = Notification(
            rec_id=2, app="com.apple.MobileSMS", title="Dele", subtitle="",
            body="just now", delivered_unix=time.time(),
        )

        assert await channel._handle(stale) is False
        assert await channel._handle(fresh) is True
        assert [c.render() for c in delivered] == ["Dele · Messages\njust now"]

    async def test_unreadable_store_goes_dormant_not_fatal(self, tmp_path: Path) -> None:
        channel = NotificationChannel(
            NotificationStore(tmp_path / "missing" / "db"), None, lambda c: None,
        )
        await channel.run()  # returns promptly instead of crashing

    async def test_delivery_log_never_contains_sender_or_message(self, caplog) -> None:  # type: ignore[no-untyped-def]
        caplog.set_level(logging.INFO)
        delivered: list[Card] = []
        channel = NotificationChannel(
            None,  # type: ignore[arg-type] -- _handle is the unit under test
            None,
            delivered.append,
        )
        notification = Notification(
            rec_id=1,
            app="com.apple.MobileSMS",
            title="Private Sender",
            subtitle="",
            body="The private message body",
            delivered_unix=0.0,
        )

        await channel._handle(notification)

        assert [c.render() for c in delivered] == ["Private Sender · Messages\nThe private message body"]
        assert "Private Sender" not in caplog.text
        assert "The private message body" not in caplog.text
        assert "channel=Messages" in caplog.text

    async def test_slow_triage_times_out_and_raw_notification_still_delivers(self) -> None:
        class NeverReplies(ReplyEngine):
            async def reply(self, text: str) -> str | None:
                del text
                await asyncio.Event().wait()
                return None

        delivered: list[Card] = []
        channel = NotificationChannel(
            None,  # type: ignore[arg-type] -- _handle is the unit under test
            NeverReplies(),
            delivered.append,
            triage_timeout_s=0.01,
        )
        await channel._handle(Notification(
            rec_id=1,
            app="com.apple.MobileSMS",
            title="Ada",
            subtitle="",
            body="Call me",
            delivered_unix=0.0,
        ))

        assert [c.render() for c in delivered] == ["Ada · Messages\nCall me"]

    async def test_failed_triage_does_not_block_raw_delivery(self) -> None:
        class BrokenTriage(ReplyEngine):
            async def reply(self, text: str) -> str | None:
                del text
                raise RuntimeError("model unavailable")

        delivered: list[Card] = []
        channel = NotificationChannel(
            None,  # type: ignore[arg-type] -- _handle is the unit under test
            BrokenTriage(),
            delivered.append,
        )
        await channel._handle(Notification(
            rec_id=1,
            app="com.apple.MobileSMS",
            title="Ada",
            subtitle="",
            body="Call me",
            delivered_unix=0.0,
        ))

        assert [c.render() for c in delivered] == ["Ada · Messages\nCall me"]


class TestManagerCards:
    async def test_idle_shows_now_active_queues_and_drains(self, caplog) -> None:  # type: ignore[no-untyped-def]
        from joat.core.protocol import HudText
        from joat.services.hud import HudWriter
        from joat.services.session import Session, SessionManager
        from test_session import SinkTransport

        caplog.set_level(logging.INFO)
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
        mgr.notify(Card(source="Mail", channel="Mail", summary="first card"))
        await asyncio.sleep(0)
        assert any(isinstance(m, HudText) and "first card" in m.text for m in transport.sent)

        assert mgr.handle_transcript("debate something") is True
        await asyncio.sleep(0)
        mgr.notify(Card(source="SMS", channel="Messages",
                        summary="queued while debating"))
        assert not any("queued while debating" in getattr(m, "text", "") for m in transport.sent)
        assert "queued while debating" not in caplog.text
        assert "card queued behind active session urgency=" in caplog.text
        sess.gate.set()
        await asyncio.sleep(0); await asyncio.sleep(0); await asyncio.sleep(0)
        assert any("queued while debating" in getattr(m, "text", "") for m in transport.sent)
