"""The overview's Planner tab (it replaced Notifications on the rail, 2026-10-15)."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, tzinfo
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from joat.core.protocol import PANEL_TABS, HudPanel
from joat.native.bridge import NativeBridgeUnavailable
from joat.services.panels import PanelPublisher
from joat.services.planner_panel import PlannerPanel, planner_rows
from joat.services.planning import (
    PlanningAuthorizationError,
    PlanningEntry,
    load_native_reminders,
)

CHICAGO = ZoneInfo("America/Chicago")
#: A Thursday, so "Tmrw" is Friday and the weekday words are unambiguous.
TODAY = date(2026, 10, 15)


def _clock(_zone: tzinfo) -> datetime:
    return datetime(2026, 10, 15, 14, 0, tzinfo=CHICAGO)


class FakeNativeData:
    """The reminders half of the EventKit bridge, with scripted responses."""

    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def call(self, method: str, params: dict[str, Any] | None = None) -> object:
        self.calls.append((method, params))
        response = self.responses[method]
        if isinstance(response, tuple):
            response, *remaining = response
            self.responses[method] = tuple(remaining)
        if isinstance(response, Exception):
            raise response
        return response


def bridge_response(
    *, calendar: str = "full_access", reminders: str = "full_access",
    result: object = None, generation: int = 1,
) -> object:
    return SimpleNamespace(
        authorization=SimpleNamespace(calendar=calendar, reminders=reminders),
        result=result,
        store_instance_id="eventkit-store",
        store_generation=generation,
    )


def reminder(
    title: str, *, month: int = 10, day: int, hour: int | None = None,
    minute: int | None = None, completed: bool = False,
) -> dict[str, object]:
    due: dict[str, object] = {"year": 2026, "month": month, "day": day, "time_zone_id": "America/Chicago"}
    if hour is not None:
        due["hour"] = hour
        due["minute"] = minute or 0
    return {
        "title": title, "calendar_item_identifier": f"r-{title}",
        "due": due, "is_completed": completed,
    }


def bridge(items: list[dict[str, object]], **status: str) -> FakeNativeData:
    return FakeNativeData({
        "authorization.status": bridge_response(**status),
        "reminders.listContainers": bridge_response(result=[{"calendar_identifier": "list-1"}]),
        "reminders.query": bridge_response(result=items),
    })


WEEK = [
    reminder("Send the Q3 invoice", day=12),
    reminder("Call the bank", day=15, hour=9, minute=0),
    reminder("Back up the staging database", day=15),
    reminder("Standup notes", day=16, hour=9, minute=0),
    reminder("Renew the domain", day=17),
    reminder("Review the dashboard", day=18, hour=10, minute=30),
]


# ── the loader ───────────────────────────────────────────────────────────


async def test_loads_open_reminders_over_a_window_earliest_first() -> None:
    client = bridge(WEEK)
    entries = await load_native_reminders(
        client, first_day=date(2026, 10, 8), last_day=date(2026, 10, 21), zone=CHICAGO,
    )

    assert [entry.title for entry in entries] == [
        "Send the Q3 invoice", "Call the bank", "Back up the staging database",
        "Standup notes", "Renew the domain", "Review the dashboard",
    ]
    query = next(params for method, params in client.calls if method == "reminders.query")
    assert query is not None
    assert query["include_completed"] is False
    assert query["calendar_identifiers"] == ["list-1"]
    assert query["due_start_unix_ms"] == int(datetime(2026, 10, 8, tzinfo=CHICAGO).timestamp() * 1000)
    # The window is half-open: it ends at the start of the day AFTER last_day.
    assert query["due_end_unix_ms"] == int(datetime(2026, 10, 22, tzinfo=CHICAGO).timestamp() * 1000)


async def test_skips_completed_and_undated_reminders() -> None:
    client = bridge([
        reminder("Done", day=15, completed=True),
        {"title": "No date", "calendar_item_identifier": "r-nodate"},
        reminder("Open", day=15),
    ])
    entries = await load_native_reminders(
        client, first_day=date(2026, 10, 8), last_day=date(2026, 10, 21), zone=CHICAGO,
    )
    assert [entry.title for entry in entries] == ["Open"]


async def test_needs_reminders_access_but_not_calendar() -> None:
    # A Mac that never granted Calendar still gets its tasks.
    entries = await load_native_reminders(
        bridge(WEEK, calendar="denied"),
        first_day=date(2026, 10, 8), last_day=date(2026, 10, 21), zone=CHICAGO,
    )
    assert len(entries) == 6
    with pytest.raises(PlanningAuthorizationError):
        await load_native_reminders(
            bridge(WEEK, reminders="denied"),
            first_day=date(2026, 10, 8), last_day=date(2026, 10, 21), zone=CHICAGO,
        )


async def test_rereads_when_the_store_changes_mid_load() -> None:
    client = FakeNativeData({
        "authorization.status": bridge_response(),
        "reminders.listContainers": (
            bridge_response(result=[{"calendar_identifier": "list-1"}], generation=2),
            bridge_response(result=[{"calendar_identifier": "list-1"}]),
        ),
        "reminders.query": bridge_response(result=[reminder("Open", day=15)]),
    })
    # The first container read carries another generation than the status
    # read did, so the whole load is repeated rather than merged.
    client.responses["authorization.status"] = (bridge_response(), bridge_response())
    entries = await load_native_reminders(
        client, first_day=date(2026, 10, 8), last_day=date(2026, 10, 21), zone=CHICAGO,
    )
    assert [entry.title for entry in entries] == ["Open"]
    assert [method for method, _ in client.calls].count("authorization.status") == 2


# ── the rows ─────────────────────────────────────────────────────────────


async def _entries() -> tuple[PlanningEntry, ...]:
    return await load_native_reminders(
        bridge(WEEK), first_day=date(2026, 10, 8), last_day=date(2026, 10, 21), zone=CHICAGO,
    )


async def test_rows_lead_with_the_word_the_glass_puts_in_its_label_column() -> None:
    assert planner_rows(await _entries(), TODAY) == (
        "[Late] Send the Q3 invoice",
        "[09:00] Call the bank",
        "[Today] Back up the staging database",
        "[Tmrw] 09:00 Standup notes",
        "[Sat] Renew the domain",
        "[Sun] 10:30 Review the dashboard",
    )


async def test_more_than_the_pane_holds_ends_in_an_honest_count() -> None:
    entry = (await _entries())[0]
    rows = planner_rows((entry,) * 12, TODAY)
    assert len(rows) == 8
    assert rows[:7] == ("[Late] Send the Q3 invoice",) * 7
    assert rows[7] == "+5 more"
    # Exactly eight fit without a count.
    assert planner_rows((entry,) * 8, TODAY)[-1] == "[Late] Send the Q3 invoice"


def test_a_row_label_fits_the_glass_label_column() -> None:
    # "Wed 09:00" measures 95 px against a 92 px column on the glass, which is
    # why a timed reminder on another day puts its time in front of the title.
    rows = planner_rows((
        PlanningEntry("reminder", "id", "Later", "09:00", datetime(2026, 10, 21, 9, tzinfo=CHICAGO)),
    ), TODAY)
    assert rows == ("[Wed] 09:00 Later",)


# ── the tab ──────────────────────────────────────────────────────────────


async def test_the_tab_reads_loading_then_the_reminders() -> None:
    panel = PlannerPanel(bridge(WEEK), lambda: CHICAGO, clock=_clock)
    assert panel.snapshot().rows == ()
    assert panel.snapshot().empty == "Reading your reminders…"

    await panel.refresh()
    snapshot = panel.snapshot()

    assert snapshot.rows[0] == "[Late] Send the Q3 invoice"
    assert len(snapshot.rows) == 6
    assert panel.tab == "planner"
    assert await panel.open(0) is None  # a tap opens Planning on the plugin


async def test_an_empty_week_says_so() -> None:
    panel = PlannerPanel(bridge([]), lambda: CHICAGO, clock=_clock)
    await panel.refresh()
    assert panel.snapshot().rows == ()
    assert panel.snapshot().empty == "Nothing due this week."


async def test_reminders_access_off_is_said_not_shown_as_an_empty_week() -> None:
    panel = PlannerPanel(bridge(WEEK, reminders="denied"), lambda: CHICAGO, clock=_clock)
    await panel.refresh()
    assert panel.snapshot().rows == ()
    assert panel.snapshot().empty == "Allow JOAT to use Reminders on your Mac."


async def test_without_the_bridge_the_tab_says_how_to_start() -> None:
    panel = PlannerPanel(None, lambda: CHICAGO, clock=_clock)
    await panel.refresh()
    assert panel.snapshot().empty == "Start JOAT from the Mac app to see reminders."


async def test_a_failing_bridge_keeps_the_rows_for_a_few_minutes_then_stops_vouching() -> None:
    client = bridge(WEEK)
    panel = PlannerPanel(client, lambda: CHICAGO, clock=_clock)
    await panel.refresh()
    assert len(panel.snapshot().rows) == 6

    client.responses["authorization.status"] = NativeBridgeUnavailable("down")
    await panel.refresh()
    await panel.refresh()
    assert len(panel.snapshot().rows) == 6  # stale beats blank, briefly
    await panel.refresh()
    assert panel.snapshot().rows == ()
    assert panel.snapshot().empty == "Reminders are unavailable on this Mac."

    # And it recovers on its own.
    client.responses["authorization.status"] = bridge_response()
    await panel.refresh()
    assert len(panel.snapshot().rows) == 6


async def test_run_pushes_the_first_read_then_only_changes() -> None:
    client = bridge(WEEK)
    panel = PlannerPanel(client, lambda: CHICAGO, clock=_clock, poll_s=0.01)
    pushes: list[tuple[str, ...]] = []

    def publish() -> None:
        pushes.append(panel.snapshot().rows)

    task = asyncio.create_task(panel.run(publish))
    try:
        await asyncio.sleep(0.15)
        # Many polls, one push: nothing changed after the first read.
        assert len(pushes) == 1 and len(pushes[0]) == 6

        client.responses["reminders.query"] = bridge_response(result=WEEK[:2])
        await asyncio.sleep(0.15)
        assert len(pushes) == 2 and len(pushes[1]) == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_a_nudge_reads_now_not_at_the_next_poll() -> None:
    client = bridge(WEEK)
    panel = PlannerPanel(client, lambda: CHICAGO, clock=_clock, poll_s=60.0)
    pushes: list[int] = []
    task = asyncio.create_task(panel.run(lambda: pushes.append(len(panel.snapshot().rows))))
    try:
        await asyncio.sleep(0.1)
        assert pushes == [6]
        client.responses["reminders.query"] = bridge_response(result=WEEK[:3])
        panel.nudge()  # QuickNotes just made reminders
        await asyncio.sleep(0.1)
        assert pushes == [6, 3]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


# ── the wire ─────────────────────────────────────────────────────────────


def test_planner_is_a_pushed_tab_and_its_rows_reach_the_wire() -> None:
    assert "planner" in PANEL_TABS
    wire = HudPanel(tab="planner", revision=3, rows=("[Late] Send the Q3 invoice",), empty="Nothing due this week.").to_wire()
    assert wire["tab"] == "planner"
    assert wire["rows"] == ["[Late] Send the Q3 invoice"]


async def test_the_publisher_pushes_the_planner_and_ignores_a_tap_on_it() -> None:
    sent: list[object] = []

    class Transport:
        async def send(self, message: object) -> None:
            sent.append(message)

    panel = PlannerPanel(bridge(WEEK), lambda: CHICAGO, clock=_clock)
    await panel.refresh()
    publisher = PanelPublisher(Transport(), {"planner": panel})  # type: ignore[arg-type]

    await publisher.publish("planner")
    pushed = sent[-1]
    assert isinstance(pushed, HudPanel) and pushed.tab == "planner"
    assert pushed.rows[0] == "[Late] Send the Q3 invoice"
    # The tab has no page of its own: nothing here "owns" a PLANNER title.
    assert publisher.owns("PLANNER") is False
