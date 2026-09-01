"""Calendar "starting soon" alerts.

The load-bearing property under test is SCOPE: this watches calendar events
only. Reminders/tasks keep their own due path, and Planning is the merged
read-only view of both — so a reminder must never be announced here.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from joat.channels.cards import Card, Urgency
from joat.persistence.calendar_alerts import CalendarAlertStore
from joat.persistence.store import DurableStore
from joat.services.calendar_monitor import CalendarMonitor

ZONE = ZoneInfo("UTC")
NOW_MS = 1_700_000_000_000
NOW_US = NOW_MS * 1_000


class FakeBridge:
    """Records the native calls made, so scope violations are visible."""

    def __init__(self, events: list[dict[str, Any]] | Exception) -> None:
        self._events = events
        self.calls: list[str] = []

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(method)
        if isinstance(self._events, Exception):
            raise self._events
        return type("Response", (), {"result": list(self._events)})()


def event(
    *,
    identifier: str = "evt-1",
    title: str = "Design review",
    minutes_ahead: float = 8,
    all_day: bool = False,
    entity: str | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "calendar_item_identifier": identifier,
        "title": title,
        "start_unix_ms": NOW_MS + int(minutes_ahead * 60_000),
        "end_unix_ms": NOW_MS + int((minutes_ahead + 30) * 60_000),
        "is_all_day": all_day,
    }
    if entity is not None:
        item["entity"] = entity
    return item


def make(tmp_path: Path, events: list[dict[str, Any]] | Exception, **kwargs: Any):  # type: ignore[no-untyped-def]
    store = DurableStore(tmp_path / "joat.sqlite3")
    store.__enter__()
    cards: list[Card] = []

    async def notify(card: Card) -> None:
        cards.append(card)

    bridge = FakeBridge(events)
    monitor = CalendarMonitor(
        bridge, CalendarAlertStore(store), notify,
        connected=lambda: True, zone=ZONE, **kwargs,
    )
    return monitor, cards, bridge, store


async def test_announces_an_event_inside_the_lead_window(tmp_path: Path) -> None:
    monitor, cards, _, store = make(tmp_path, [event()])
    with store:
        assert await monitor.run_once(now_unix_ms=NOW_MS, now_us=NOW_US) == 1
        assert [(c.source, c.channel, c.event_class, c.urgency) for c in cards] == [
            ("Design review", "Calendar", "calendar.starting_soon", Urgency.NOTIFY),
        ]
        # Two lines, who then what — and NOTIFY, so it never claims the
        # brightest state reserved for corroborated urgency.
        assert cards[0].render().splitlines()[0] == "Design review · Calendar"
        assert "in 8 min" in cards[0].summary


async def test_never_announces_a_reminder_projected_into_the_calendar(
    tmp_path: Path,
) -> None:
    """Calendar.app can project Scheduled Reminders alongside events. Those
    belong to the reminder due path; announcing them here would tell the
    wearer the same thing twice."""
    monitor, cards, bridge, store = make(
        tmp_path, [event(identifier="rem-1", entity="reminder")],
    )
    with store:
        assert await monitor.run_once(now_unix_ms=NOW_MS, now_us=NOW_US) == 0
        assert cards == []
    # Scope proof: the reminder store is never even consulted.
    assert bridge.calls == ["calendar.query"]
    assert not any("reminder" in call for call in bridge.calls)


async def test_announces_each_event_once_across_repeated_polls(tmp_path: Path) -> None:
    monitor, cards, _, store = make(tmp_path, [event()])
    with store:
        assert await monitor.run_once(now_unix_ms=NOW_MS, now_us=NOW_US) == 1
        assert await monitor.run_once(now_unix_ms=NOW_MS + 60_000, now_us=NOW_US) == 0
        assert await monitor.run_once(now_unix_ms=NOW_MS + 120_000, now_us=NOW_US) == 0
        assert len(cards) == 1


async def test_a_rescheduled_event_announces_its_new_start(tmp_path: Path) -> None:
    """The cursor is (event, start time): moving a meeting is news again."""
    store = DurableStore(tmp_path / "joat.sqlite3")
    with store:
        alerts = CalendarAlertStore(store)
        cards: list[Card] = []
        async def notify(card: Card) -> None:
            cards.append(card)

        first = CalendarMonitor(
            FakeBridge([event(minutes_ahead=5)]), alerts, notify,
            connected=lambda: True, zone=ZONE,
        )
        assert await first.run_once(now_unix_ms=NOW_MS, now_us=NOW_US) == 1
        moved = CalendarMonitor(
            FakeBridge([event(minutes_ahead=9)]), alerts, notify,
            connected=lambda: True, zone=ZONE,
        )
        assert await moved.run_once(now_unix_ms=NOW_MS, now_us=NOW_US) == 1
        assert len(cards) == 2


async def test_ignores_all_day_and_out_of_window_events(tmp_path: Path) -> None:
    monitor, cards, _, store = make(tmp_path, [
        event(identifier="all-day", all_day=True),
        event(identifier="far", minutes_ahead=45),
        # EventKit matches anything OVERLAPPING the window, so a meeting that
        # began this morning arrives too — it is not "starting soon".
        event(identifier="already-running", minutes_ahead=-30),
    ])
    with store:
        assert await monitor.run_once(now_unix_ms=NOW_MS, now_us=NOW_US) == 0
        assert cards == []


async def test_unavailable_calendar_access_is_quiet_not_fatal(tmp_path: Path) -> None:
    monitor, cards, _, store = make(tmp_path, RuntimeError("calendar access denied"))
    with store:
        assert await monitor.run_once(now_unix_ms=NOW_MS, now_us=NOW_US) == 0
        assert cards == []


async def test_nothing_is_announced_or_marked_while_no_client_is_connected(
    tmp_path: Path,
) -> None:
    """Awaiting the HUD write is not proof the wearer saw anything.

    The transport logs and returns normally when no plugin is connected, so a
    normal return would commit the cursor for a discarded frame. The poll loop
    starts before the gateway accepts its first client, so without this gate
    the first meeting of every launch is consumed by the startup poll.
    """
    store = DurableStore(tmp_path / "joat.sqlite3")
    with store:
        alerts = CalendarAlertStore(store)
        cards: list[Card] = []
        online = False

        async def notify(card: Card) -> None:
            cards.append(card)

        monitor = CalendarMonitor(
            FakeBridge([event()]), alerts, notify,
            connected=lambda: online, zone=ZONE,
        )
        assert await monitor.run_once(now_unix_ms=NOW_MS, now_us=NOW_US) == 0
        assert cards == []
        assert not alerts.already_alerted("evt-1", NOW_MS + 8 * 60_000)

        # The event survives to be announced once the phone dials in.
        online = True
        assert await monitor.run_once(now_unix_ms=NOW_MS, now_us=NOW_US) == 1
        assert len(cards) == 1


async def test_a_store_failure_does_not_end_calendar_polling(tmp_path: Path) -> None:
    """Nothing awaits the poll task, so an escaping exception would stop
    schedule alerts for the rest of the process without a word."""
    store = DurableStore(tmp_path / "joat.sqlite3")
    with store:
        monitor = CalendarMonitor(
            FakeBridge([event()]), CalendarAlertStore(store),
            lambda card: _unused(card), connected=lambda: True, zone=ZONE,
            poll_interval_s=0.01,
        )
        calls = {"n": 0}

        async def exploding_run_once(**_: Any) -> int:
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("database is locked")
            monitor.stop()
            return 0

        monitor.run_once = exploding_run_once  # type: ignore[method-assign]
        await monitor.run()
        # It kept polling after the failure instead of dying on the first one.
        assert calls["n"] == 2


async def _unused(card: Card) -> None:
    raise AssertionError("delivery is not exercised by this test")


async def test_an_item_without_a_usable_all_day_flag_stays_quiet(
    tmp_path: Path,
) -> None:
    """The bridge always sends a real bool. Anything else means the payload
    cannot be trusted to say whether this is timed, and announcing
    "starts in 8 min" for an all-day item is worse than silence."""
    malformed = event(identifier="no-flag")
    del malformed["is_all_day"]
    null_flag = event(identifier="null-flag")
    null_flag["is_all_day"] = None
    monitor, cards, _, store = make(tmp_path, [malformed, null_flag])
    with store:
        assert await monitor.run_once(now_unix_ms=NOW_MS, now_us=NOW_US) == 0
        assert cards == []


async def test_a_failed_delivery_is_not_marked_and_is_re_offered(tmp_path: Path) -> None:
    """The cursor must be committed only AFTER a delivery that completed.

    Marking first (or after a fire-and-forget hand-off) makes a dropped card
    unrepeatable, because a marked event is never re-offered. Failing the
    other way costs at most a repeat, bounded by the lead window.
    """
    store = DurableStore(tmp_path / "joat.sqlite3")
    with store:
        alerts = CalendarAlertStore(store)
        attempts: list[Card] = []
        fail = True

        async def notify(card: Card) -> None:
            attempts.append(card)
            if fail:
                raise RuntimeError("HUD write failed")

        monitor = CalendarMonitor(
            FakeBridge([event()]), alerts, notify,
            connected=lambda: True, zone=ZONE,
        )
        assert await monitor.run_once(now_unix_ms=NOW_MS, now_us=NOW_US) == 0
        assert len(attempts) == 1
        assert not alerts.already_alerted("evt-1", NOW_MS + 8 * 60_000)

        fail = False
        recovered = CalendarMonitor(
            FakeBridge([event()]), alerts, notify,
            connected=lambda: True, zone=ZONE,
        )
        assert await recovered.run_once(now_unix_ms=NOW_MS, now_us=NOW_US) == 1
        assert len(attempts) == 2
        assert alerts.already_alerted("evt-1", NOW_MS + 8 * 60_000)


async def test_a_stalled_delivery_times_out_without_marking(tmp_path: Path) -> None:
    """A card queued behind a long session must not stall the poll loop, and
    must not be recorded as announced when it never landed."""
    store = DurableStore(tmp_path / "joat.sqlite3")
    with store:
        alerts = CalendarAlertStore(store)

        async def never_returns(card: Card) -> None:
            await asyncio.Event().wait()

        monitor = CalendarMonitor(
            FakeBridge([event()]), alerts, never_returns,
            connected=lambda: True, zone=ZONE, delivery_timeout_s=0.01,
        )
        assert await monitor.run_once(now_unix_ms=NOW_MS, now_us=NOW_US) == 0
        assert not alerts.already_alerted("evt-1", NOW_MS + 8 * 60_000)


async def test_a_burst_of_events_obeys_the_delivery_policy_rate_cap(tmp_path: Path) -> None:
    """Calendar is not exempt from delivery-policy admission: a poll
    returning many events must not paint a frame per event. The cap is this
    monitor's own budget — per-producer, not shared across subsystems."""
    store = DurableStore(tmp_path / "joat.sqlite3")
    with store:
        events = [event(identifier=f"evt-{n}", title=f"Meeting {n}", minutes_ahead=n + 1)
                  for n in range(9)]
        cards: list[Card] = []

        async def notify(card: Card) -> None:
            cards.append(card)

        monitor = CalendarMonitor(
            FakeBridge(events), CalendarAlertStore(store), notify,
            connected=lambda: True, zone=ZONE,
        )
        announced = await monitor.run_once(now_unix_ms=NOW_MS, now_us=NOW_US)
        # DeliveryPolicy caps routine traffic at six per minute.
        assert announced == 6
        assert len(cards) == 6


async def test_a_moved_event_corrects_a_card_that_is_now_wrong(tmp_path: Path) -> None:
    """10:00 -> 10:30 -> 10:00 must announce all three times.

    After the second move the wearer's most recent card says 10:30, which is
    wrong. Remembering every historical start would silently suppress the
    correction, so only the latest announced start is retained.
    """
    store = DurableStore(tmp_path / "joat.sqlite3")
    with store:
        alerts = CalendarAlertStore(store)
        cards: list[Card] = []

        async def notify(card: Card) -> None:
            cards.append(card)

        for ahead in (5, 9, 5):
            monitor = CalendarMonitor(
                FakeBridge([event(minutes_ahead=ahead)]), alerts, notify,
                connected=lambda: True, zone=ZONE,
            )
            assert await monitor.run_once(now_unix_ms=NOW_MS, now_us=NOW_US) == 1
        assert len(cards) == 3


async def test_an_unmoved_event_is_still_announced_only_once(tmp_path: Path) -> None:
    """The correction above must not become a re-announcement loop."""
    monitor, cards, _, store = make(tmp_path, [event()])
    with store:
        assert await monitor.run_once(now_unix_ms=NOW_MS, now_us=NOW_US) == 1
        assert await monitor.run_once(now_unix_ms=NOW_MS + 30_000, now_us=NOW_US) == 0
        assert len(cards) == 1


async def test_the_card_carries_its_occurrence_identity(tmp_path: Path) -> None:
    monitor, cards, _, store = make(tmp_path, [event()])
    with store:
        await monitor.run_once(now_unix_ms=NOW_MS, now_us=NOW_US)
        # origin_id distinguishes two announcements of the same series.
        assert cards[0].origin_id == f"evt-1:{NOW_MS + 8 * 60_000}"


async def test_untitled_and_malformed_events_never_crash_the_poll(tmp_path: Path) -> None:
    monitor, cards, _, store = make(tmp_path, [
        {"calendar_item_identifier": "no-start", "title": "Missing start"},
        {"start_unix_ms": NOW_MS + 60_000, "title": "Missing identifier"},
        event(identifier="untitled", title="   "),
    ])
    with store:
        assert await monitor.run_once(now_unix_ms=NOW_MS, now_us=NOW_US) == 1
        assert cards[0].source == "Untitled event"
