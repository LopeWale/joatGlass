"""Calendar "starting soon" alerts.

The load-bearing property under test is SCOPE: this watches calendar events
only. Reminders/tasks keep their own due path, and Planning is the merged
read-only view of both — so a reminder must never be announced here.
"""

from __future__ import annotations

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
    bridge = FakeBridge(events)
    monitor = CalendarMonitor(
        bridge, CalendarAlertStore(store), cards.append, zone=ZONE, **kwargs,
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
        first = CalendarMonitor(
            FakeBridge([event(minutes_ahead=5)]), alerts, cards.append, zone=ZONE,
        )
        assert await first.run_once(now_unix_ms=NOW_MS, now_us=NOW_US) == 1
        moved = CalendarMonitor(
            FakeBridge([event(minutes_ahead=9)]), alerts, cards.append, zone=ZONE,
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


async def test_untitled_and_malformed_events_never_crash_the_poll(tmp_path: Path) -> None:
    monitor, cards, _, store = make(tmp_path, [
        {"calendar_item_identifier": "no-start", "title": "Missing start"},
        {"start_unix_ms": NOW_MS + 60_000, "title": "Missing identifier"},
        event(identifier="untitled", title="   "),
    ])
    with store:
        assert await monitor.run_once(now_unix_ms=NOW_MS, now_us=NOW_US) == 1
        assert cards[0].source == "Untitled event"
