from __future__ import annotations

import asyncio
import time
from pathlib import Path

from joat.channels.cards import Card
from joat.channels.notification_mux import NotificationLane, NotificationMux
from joat.channels.notifications import Notification
from joat.persistence.notifications import (
    NotificationObservationStore,
    NotificationPersistenceConflict,
)
from joat.persistence.store import DurableStore


class ScriptedSource:
    def __init__(self, baseline, batches):  # type: ignore[no-untyped-def]
        self._baseline = baseline
        self._batches = list(batches)

    def baseline(self):  # type: ignore[no-untyped-def]
        return self._baseline

    def read_new(self, watermark):  # type: ignore[no-untyped-def]
        if not self._batches:
            return [], watermark
        return self._batches.pop(0)


def notification(rec_id: int, *, app: str, title: str, body: str) -> Notification:
    # Stamped "now": the channel refuses to interrupt the lens with a card
    # older than NotificationChannel.STALE_CARD_S (120 s, since 2026-09-08),
    # and a fixed 2023 epoch made every fan-in test below assert on a card
    # the guard had correctly withheld. These tests pin ingestion and
    # authority suppression, not staleness; the guard has its own test in
    # test_notifications.py.
    return Notification(rec_id, app, title, "", body, time.time(), str(rec_id))


def test_prune_drops_settled_history_but_never_an_undelivered_notification(
    tmp_path: Path,
) -> None:
    """Nothing pruned this table, and every poll queries it — so it made each
    incoming notification a little slower than the last. Pruning must still
    never drop something the wearer has not seen."""
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        observations = NotificationObservationStore(store)
        observations.initialize_source(
            source_id="messages-db",
            coverage_scope="messages.incoming_text",
            authority_level="authoritative",
            baseline=0,
            health="healthy_complete_for_scope",
            now_us=1,
        )
        assert observations.commit_batch(
            source_id="messages-db",
            cursor_before=0,
            cursor_after=3,
            notifications=[
                notification(1, app="Messages", title="Ada", body="settled"),
                notification(2, app="Messages", title="Ada", body="still pending"),
            ],
            event_class="messages.incoming_text",
            completeness="complete_for_scope",
            observed_at_us=1_000,
        ) == 2

        settled, still_pending = observations.pending(limit=10)
        observations.mark_enqueued(settled.observation_id, now_us=1_000)

        # Horizon far in the future: age is not what protects the pending row.
        assert observations.prune(before_us=10**18) == 1
        remaining = observations.pending(limit=10)
        assert [row.observation_id for row in remaining] == [
            still_pending.observation_id
        ]


def test_batch_commit_retains_raw_observations_and_cursor_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "joat.sqlite3"
    with DurableStore(path) as store:
        observations = NotificationObservationStore(store)
        cursor = observations.initialize_source(
            source_id="messages-db",
            coverage_scope="messages.incoming_text",
            authority_level="authoritative",
            baseline=10,
            health="healthy_complete_for_scope",
            now_us=1,
        )
        assert cursor == 10
        assert observations.commit_batch(
            source_id="messages-db",
            cursor_before=10,
            cursor_after=12,
            notifications=[
                notification(11, app="Messages", title="Ada", body="one"),
                notification(12, app="Messages", title="Ada", body="two"),
            ],
            event_class="messages.incoming_text",
            completeness="complete_for_scope",
            observed_at_us=2,
        ) == 2

    with DurableStore(path) as reopened:
        observations = NotificationObservationStore(reopened)
        cursor = observations.initialize_source(
            source_id="messages-db",
            coverage_scope="messages.incoming_text",
            authority_level="authoritative",
            baseline=999,
            health="healthy_complete_for_scope",
            now_us=3,
        )
        assert cursor == 12
        assert [item.notification.body for item in observations.pending()] == ["one", "two"]


async def test_messages_and_banner_ingest_together_but_authority_suppression_keeps_raw(
    tmp_path: Path,
) -> None:
    message = notification(1, app="Messages", title="Ada", body="Call me")
    banner = notification(2, app="Messages", title="Ada", body="Call me")
    messages = ScriptedSource(0, [([message], 1)])
    banners = ScriptedSource(0, [([banner], 1)])
    cards: list[Card] = []
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        observations = NotificationObservationStore(store)
        mux = NotificationMux(
            [
                NotificationLane(
                    "messages-db", messages, "messages.incoming_text", "authoritative",
                    "healthy_complete_for_scope", "complete_for_scope", "messages.incoming_text",
                ),
                NotificationLane(
                    "ax-banners", banners, "notifications.banner", "best_effort",
                    "healthy_best_effort", "best_effort",
                ),
            ],
            observations,
            triage=None,
            deliver=cards.append,
            poll_interval_s=0.01,
            authority_grace_s=0.02,
        )
        task = asyncio.create_task(mux.run())
        for _ in range(50):
            states = dict(store.execute(
                "SELECT state,count(*) FROM notification_outbox GROUP BY state"
            ).fetchall())
            if states == {"enqueued": 1, "suppressed": 1}:
                break
            await asyncio.sleep(0.01)
        mux.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert [c.render() for c in cards] == ["Ada · Messages\nCall me"]
        assert store.execute("SELECT count(*) FROM notification_observations").fetchone()[0] == 2
        assert dict(store.execute(
            "SELECT state,count(*) FROM notification_outbox GROUP BY state"
        ).fetchall()) == {"enqueued": 1, "suppressed": 1}


async def test_lane_failure_does_not_stop_other_source_ingestion(tmp_path: Path) -> None:
    class FailingSource(ScriptedSource):
        def read_new(self, watermark):  # type: ignore[no-untyped-def]
            raise PermissionError("revoked")

    good = ScriptedSource(0, [([notification(1, app="Mail", title="Boss", body="Review")], 1)])
    bad = FailingSource(0, [])
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        observations = NotificationObservationStore(store)
        mux = NotificationMux(
            [
                NotificationLane("bad", bad, "messages.incoming_text", "authoritative",
                                 "healthy_complete_for_scope", "complete_for_scope"),
                NotificationLane("good", good, "notifications.banner", "best_effort",
                                 "healthy_best_effort", "best_effort"),
            ],
            observations,
            triage=None,
            deliver=lambda _card: None,
            poll_interval_s=0.01,
            authority_grace_s=0.02,
        )
        task = asyncio.create_task(mux.run())
        # Wait for the CONDITION, not a duration. A fixed 60ms budgeted six
        # poll cycles and measured scheduler luck: it flaked whenever the
        # machine was busy — a full suite, live model calls, the running
        # engine — and passed 12/12 the moment it was watched in isolation.
        # A generous deadline only makes a healthy run slower on a slow
        # machine; it can no longer make it red.
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            states = dict(store.execute(
                "SELECT source_id,health FROM notification_sources").fetchall())
            ingested = store.execute(
                "SELECT count(*) FROM notification_observations WHERE source_id='good'"
            ).fetchone()[0]
            if states.get("bad") == "failed" and ingested == 1:
                break
            await asyncio.sleep(0.01)
        mux.stop(); task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert states["bad"] == "failed"
        assert ingested == 1


async def test_healthy_authority_without_exact_observation_does_not_hide_banner(
    tmp_path: Path,
) -> None:
    messages = ScriptedSource(0, [])
    banners = ScriptedSource(
        0,
        [([notification(2, app="Messages", title="Ada", body="Attachment")], 1)],
    )
    cards: list[Card] = []
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        mux = NotificationMux(
            [
                NotificationLane(
                    "messages-db", messages, "messages.incoming_text", "authoritative",
                    "healthy_complete_for_scope", "complete_for_scope",
                    "messages.incoming_text",
                ),
                NotificationLane(
                    "ax-banners", banners, "notifications.banner", "best_effort",
                    "healthy_best_effort", "best_effort",
                ),
            ],
            NotificationObservationStore(store),
            triage=None,
            deliver=cards.append,
            poll_interval_s=0.01,
            authority_grace_s=0.02,
        )
        task = asyncio.create_task(mux.run())
        for _ in range(30):
            if cards:
                break
            await asyncio.sleep(0.01)
        mux.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert [c.render() for c in cards] == ["Ada · Messages\nAttachment"]
        assert store.execute(
            "SELECT state FROM notification_outbox"
        ).fetchone()[0] == "enqueued"


def test_overlapping_reordered_batches_use_stable_native_identity(tmp_path: Path) -> None:
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        observations = NotificationObservationStore(store)
        observations.initialize_source(
            source_id="source",
            coverage_scope="notifications.banner",
            authority_level="best_effort",
            baseline=0,
            health="healthy_best_effort",
            now_us=1,
        )
        one = notification(1, app="Mail", title="A", body="one")
        two = notification(2, app="Mail", title="B", body="two")
        three = notification(3, app="Mail", title="C", body="three")
        assert observations.commit_batch(
            source_id="source", cursor_before=0, cursor_after=2,
            notifications=[one, two], event_class=None,
            completeness="best_effort", observed_at_us=2,
        ) == 2
        assert observations.commit_batch(
            source_id="source", cursor_before=2, cursor_after=3,
            notifications=[two, three], event_class=None,
            completeness="best_effort", observed_at_us=3,
        ) == 1
        assert observations.commit_batch(
            source_id="source", cursor_before=3, cursor_after=4,
            notifications=[two, one], event_class=None,
            completeness="best_effort", observed_at_us=4,
        ) == 0
        assert store.execute(
            "SELECT count(*) FROM notification_observations"
        ).fetchone()[0] == 3


def test_payload_digest_is_verified_before_delivery(tmp_path: Path) -> None:
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        observations = NotificationObservationStore(store)
        observations.initialize_source(
            source_id="source",
            coverage_scope="notifications.banner",
            authority_level="best_effort",
            baseline=0,
            health="healthy_best_effort",
            now_us=1,
        )
        observations.commit_batch(
            source_id="source", cursor_before=0, cursor_after=1,
            notifications=[notification(1, app="Mail", title="A", body="one")],
            event_class=None, completeness="best_effort", observed_at_us=2,
        )
        store.execute(
            "UPDATE notification_observations SET payload=?",
            (b'{"body":"tampered"}',),
        )

        try:
            observations.pending()
        except NotificationPersistenceConflict as exc:
            assert "digest" in str(exc)
        else:
            raise AssertionError("corrupted notification payload must fail closed")


def test_one_authoritative_observation_suppresses_only_one_identical_banner(
    tmp_path: Path,
) -> None:
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        observations = NotificationObservationStore(store)
        for source_id, scope, authority, health in (
            (
                "messages-db", "messages.incoming_text", "authoritative",
                "healthy_complete_for_scope",
            ),
            ("ax-banners", "notifications.banner", "best_effort", "healthy_best_effort"),
        ):
            observations.initialize_source(
                source_id=source_id,
                coverage_scope=scope,
                authority_level=authority,
                baseline=0,
                health=health,
                now_us=1,
            )
        observations.commit_batch(
            source_id="messages-db", cursor_before=0, cursor_after=1,
            notifications=[notification(1, app="Messages", title="Ada", body="Hi")],
            event_class="messages.incoming_text", completeness="complete_for_scope",
            observed_at_us=2,
        )
        observations.commit_batch(
            source_id="ax-banners", cursor_before=0, cursor_after=2,
            notifications=[
                notification(2, app="Messages", title="Ada", body="Hi"),
                notification(3, app="Messages", title="Ada", body="Hi"),
            ],
            event_class=None, completeness="best_effort", observed_at_us=3,
        )
        banners = [item for item in observations.pending() if item.source_id == "ax-banners"]

        assert observations.suppress_if_matched_authoritative(
            banners[0], "messages.incoming_text", now_us=4,
        ) is True
        assert observations.suppress_if_matched_authoritative(
            banners[1], "messages.incoming_text", now_us=5,
        ) is False


async def test_authority_grace_closes_one_poll_ingestion_race(tmp_path: Path) -> None:
    authoritative = notification(1, app="Messages", title="Ada", body="Hi")
    banner = notification(2, app="Messages", title="Ada", body="Hi")
    messages = ScriptedSource(0, [([], 0), ([authoritative], 1)])
    banners = ScriptedSource(0, [([banner], 1)])
    cards: list[Card] = []
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        mux = NotificationMux(
            [
                NotificationLane(
                    "messages-db", messages, "messages.incoming_text", "authoritative",
                    "healthy_complete_for_scope", "complete_for_scope",
                    "messages.incoming_text",
                ),
                NotificationLane(
                    "ax-banners", banners, "notifications.banner", "best_effort",
                    "healthy_best_effort", "best_effort",
                ),
            ],
            NotificationObservationStore(store),
            triage=None,
            deliver=cards.append,
            poll_interval_s=0.01,
            authority_grace_s=0.06,
        )
        task = asyncio.create_task(mux.run())
        for _ in range(60):
            states = dict(store.execute(
                "SELECT state,count(*) FROM notification_outbox GROUP BY state"
            ).fetchall())
            if states == {"enqueued": 1, "suppressed": 1}:
                break
            await asyncio.sleep(0.01)
        mux.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert [c.render() for c in cards] == ["Ada · Messages\nHi"]


async def test_restart_waits_for_authoritative_catch_up_before_old_fallback(
    tmp_path: Path,
) -> None:
    authoritative = notification(1, app="Messages", title="Ada", body="Hi")
    banner = notification(2, app="Messages", title="Ada", body="Hi")
    cards: list[Card] = []
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        observations = NotificationObservationStore(store)
        observations.initialize_source(
            source_id="messages-db",
            coverage_scope="messages.incoming_text",
            authority_level="authoritative",
            baseline=0,
            health="healthy_complete_for_scope",
            now_us=1,
        )
        observations.initialize_source(
            source_id="ax-banners",
            coverage_scope="notifications.banner",
            authority_level="best_effort",
            baseline=0,
            health="healthy_best_effort",
            now_us=1,
        )
        observations.commit_batch(
            source_id="ax-banners",
            cursor_before=0,
            cursor_after=1,
            notifications=[banner],
            event_class=None,
            completeness="best_effort",
            observed_at_us=2,
        )
        mux = NotificationMux(
            [
                NotificationLane(
                    "messages-db",
                    ScriptedSource(999, [([authoritative], 1)]),
                    "messages.incoming_text",
                    "authoritative",
                    "healthy_complete_for_scope",
                    "complete_for_scope",
                    "messages.incoming_text",
                ),
                NotificationLane(
                    "ax-banners",
                    ScriptedSource(999, [([], 1)]),
                    "notifications.banner",
                    "best_effort",
                    "healthy_best_effort",
                    "best_effort",
                ),
            ],
            observations,
            triage=None,
            deliver=cards.append,
            poll_interval_s=0.01,
            authority_grace_s=0.02,
        )
        task = asyncio.create_task(mux.run())
        for _ in range(60):
            states = dict(store.execute(
                "SELECT state,count(*) FROM notification_outbox GROUP BY state"
            ).fetchall())
            if states == {"enqueued": 1, "suppressed": 1}:
                break
            await asyncio.sleep(0.01)
        mux.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert [c.render() for c in cards] == ["Ada · Messages\nHi"]


async def test_metadata_only_observation_is_retained_but_never_rendered(
    tmp_path: Path,
) -> None:
    metadata = ScriptedSource(
        0,
        [([notification(1, app="com.apple.Calendar", title="opaque", body="")], 1)],
    )
    cards: list[Card] = []
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        mux = NotificationMux(
            [NotificationLane(
                "biome", metadata, "notifications.delivery_metadata", "metadata_only",
                "healthy_best_effort", "metadata_only",
            )],
            NotificationObservationStore(store),
            triage=None,
            deliver=cards.append,
            poll_interval_s=0.01,
            authority_grace_s=0.02,
        )
        task = asyncio.create_task(mux.run())
        for _ in range(30):
            row = store.execute(
                "SELECT state FROM notification_outbox"
            ).fetchone()
            if row is not None and row[0] == "suppressed":
                break
            await asyncio.sleep(0.01)
        mux.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert cards == []
        assert store.execute(
            "SELECT state,reason_code FROM notification_outbox"
        ).fetchone() == ("suppressed", "metadata_only_not_renderable")


async def test_a_backlog_drain_never_holds_the_event_loop(tmp_path: Path) -> None:
    """A stale observation awaits nothing, so a backlog used to run its whole
    length without yielding: 1,130 old banners froze JOAT for 9.7 s at
    startup (2026-10-11), and glasses reconnects drained 5-12 s backlogs.
    Another task must get the loop between every two observations."""
    stale_at = time.time() - 3600
    backlog = [
        Notification(index, "Mail", f"old {index}", "", "body", stale_at, str(index))
        for index in range(200)
    ]
    source = ScriptedSource(0, [(backlog, 200)])
    cards: list[Card] = []
    ticks = 0
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        mux = NotificationMux(
            [NotificationLane(
                "ax-banners", source, "notifications.banner", "best_effort",
                "healthy_best_effort", "best_effort",
            )],
            NotificationObservationStore(store),
            triage=None,
            deliver=cards.append,
            poll_interval_s=0.01,
            authority_grace_s=0.0,
        )

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0)
                ticks += 1

        # The tick count each observation was handled at: it must move
        # between every two, i.e. the other task ran in between.
        handled_at: list[int] = []
        handle = mux._handler._handle

        async def recording(item: Notification) -> bool:
            handled_at.append(ticks)
            return await handle(item)

        mux._handler._handle = recording  # type: ignore[method-assign]
        task = asyncio.create_task(mux.run())
        other = asyncio.create_task(ticker())
        settled = 0
        for _ in range(500):
            settled = store.execute(
                "SELECT COUNT(*) FROM notification_outbox WHERE state='suppressed'"
            ).fetchone()[0]
            if settled == 200:
                break
            await asyncio.sleep(0.01)
        mux.stop()
        for running in (task, other):
            running.cancel()
            try:
                await running
            except asyncio.CancelledError:
                pass

        assert settled == 200
        assert cards == []
        assert len(handled_at) == 200
        assert all(later > earlier for earlier, later in zip(handled_at, handled_at[1:]))


async def test_a_failing_source_warns_once_per_spell_and_logs_recovery(
    tmp_path: Path, caplog,  # type: ignore[no-untyped-def]
) -> None:
    """The banner watcher pauses while the Mac sleeps; its stale heartbeat
    used to log a warning on every poll (once a second) until it woke."""

    class Flaky:
        def __init__(self) -> None:
            self.reads = 0

        def baseline(self):  # type: ignore[no-untyped-def]
            return 0

        def read_new(self, watermark):  # type: ignore[no-untyped-def]
            self.reads += 1
            if self.reads <= 5:
                raise RuntimeError("stale Accessibility watcher heartbeat")
            return [], watermark

    source = Flaky()
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        mux = NotificationMux(
            [NotificationLane(
                "ax-banners", source, "notifications.banner", "best_effort",
                "healthy_best_effort", "best_effort",
            )],
            NotificationObservationStore(store),
            triage=None,
            deliver=lambda card: None,
            poll_interval_s=0.005,
            authority_grace_s=0.0,
        )
        with caplog.at_level("INFO", logger="joat.channels.notification_mux"):
            task = asyncio.create_task(mux.run())
            for _ in range(200):
                if source.reads >= 8:
                    break
                await asyncio.sleep(0.005)
            mux.stop()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    messages = [record.getMessage() for record in caplog.records]
    assert sum("poll failed" in message for message in messages) == 1
    assert any("recovered after 5 failed polls" in message for message in messages)
