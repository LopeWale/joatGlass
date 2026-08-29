from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from joat.services.planning import (
    PlanningAuthorizationError,
    PlanningScope,
    advance_selected_day,
    load_native_planning,
    month_grid,
    project_planning,
)


CHICAGO = ZoneInfo("America/Chicago")


class FakeNativeData:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    async def call(self, method: str, params: dict[str, object] | None = None) -> object:
        self.calls.append((method, params))
        return self.responses[method]


def bridge_response(
    *,
    calendar: str = "full_access",
    reminders: str = "full_access",
    result: object = None,
) -> object:
    return SimpleNamespace(
        authorization=SimpleNamespace(calendar=calendar, reminders=reminders),
        result=result,
    )


def event(title: str, when: datetime) -> dict[str, object]:
    return {
        "title": title,
        "calendar_item_identifier": f"event-{title}",
        "start_unix_ms": int(when.timestamp() * 1_000),
        "end_unix_ms": int(when.timestamp() * 1_000) + 3_600_000,
    }


def reminder(
    title: str,
    *,
    year: int,
    month: int,
    day: int,
    hour: int | None = None,
    minute: int | None = None,
) -> dict[str, object]:
    due: dict[str, object] = {"year": year, "month": month, "day": day}
    if hour is not None:
        due["hour"] = hour
    if minute is not None:
        due["minute"] = minute
    return {
        "title": title,
        "calendar_item_identifier": f"reminder-{title}",
        "due": due,
        "is_completed": False,
    }


def test_month_grid_is_a_complete_monday_first_six_week_calendar() -> None:
    grid = month_grid(date(2026, 9, 17))

    assert len(grid) == 42
    assert grid[0] == date(2026, 8, 31)
    assert grid[-1] == date(2026, 10, 11)
    assert grid[grid.index(date(2026, 9, 1))].weekday() == 1


def test_day_projection_nests_events_and_reminders_in_one_timed_agenda() -> None:
    selected = date(2026, 9, 1)
    projection = project_planning(
        selected_day=selected,
        scope=PlanningScope.DAY,
        zone=CHICAGO,
        events=[
            event("Design review", datetime(2026, 9, 1, 9, 30, tzinfo=CHICAGO)),
            event("Lunch", datetime(2026, 9, 1, 12, 0, tzinfo=CHICAGO)),
        ],
        reminders=[
            reminder("Buy filters", year=2026, month=9, day=1, hour=18, minute=0),
            reminder("Book dentist", year=2026, month=9, day=1),
        ],
    )

    assert projection.title == "PLAN · TUE SEP 1"
    assert [entry.title for entry in projection.days[0].entries] == [
        "Design review",
        "Lunch",
        "Buy filters",
        "Book dentist",
    ]
    assert [entry.kind for entry in projection.days[0].entries] == [
        "event",
        "event",
        "reminder",
        "reminder",
    ]
    assert projection.days[0].entries[2].time_label == "18:00"
    assert projection.days[0].entries[3].time_label == "ALL DAY"


def test_week_projection_keeps_tasks_on_their_calendar_day_and_omits_undated() -> None:
    projection = project_planning(
        selected_day=date(2026, 9, 3),
        scope=PlanningScope.WEEK,
        zone=CHICAGO,
        events=[event("Standup", datetime(2026, 9, 1, 9, 0, tzinfo=CHICAGO))],
        reminders=[
            reminder("Ship report", year=2026, month=9, day=4, hour=16, minute=0),
            {"title": "Someday", "calendar_item_identifier": "undated", "is_completed": False},
        ],
    )

    assert projection.title == "PLAN · AUG 31–SEP 6"
    assert [(day.day, [entry.title for entry in day.entries]) for day in projection.days] == [
        (date(2026, 8, 31), []),
        (date(2026, 9, 1), ["Standup"]),
        (date(2026, 9, 2), []),
        (date(2026, 9, 3), []),
        (date(2026, 9, 4), ["Ship report"]),
        (date(2026, 9, 5), []),
        (date(2026, 9, 6), []),
    ]


def test_month_projection_marks_days_with_both_native_sources() -> None:
    projection = project_planning(
        selected_day=date(2026, 9, 17),
        scope=PlanningScope.MONTH,
        zone=CHICAGO,
        events=[event("Demo", datetime(2026, 9, 17, 10, 0, tzinfo=CHICAGO))],
        reminders=[reminder("Send notes", year=2026, month=9, day=17, hour=15, minute=0)],
    )

    selected = next(day for day in projection.days if day.day == date(2026, 9, 17))
    assert projection.title == "PLAN · SEPTEMBER 2026"
    assert selected.event_count == 1
    assert selected.reminder_count == 1
    assert selected.is_selected is True


def test_event_crossing_midnight_is_visible_on_every_overlapping_calendar_day() -> None:
    overnight = event("Red-eye review", datetime(2026, 9, 1, 23, 30, tzinfo=CHICAGO))
    overnight["end_unix_ms"] = int(datetime(2026, 9, 2, 1, 0, tzinfo=CHICAGO).timestamp() * 1_000)

    projection = project_planning(
        selected_day=date(2026, 9, 2),
        scope=PlanningScope.DAY,
        zone=CHICAGO,
        events=[overnight],
        reminders=[],
    )

    assert [(entry.title, entry.time_label) for entry in projection.days[0].entries] == [
        ("Red-eye review", "CONTINUES"),
    ]


def test_scope_navigation_keeps_the_wearer_on_human_calendar_boundaries() -> None:
    selected = date(2026, 1, 31)

    assert advance_selected_day(selected, PlanningScope.DAY, 1) == date(2026, 2, 1)
    assert advance_selected_day(selected, PlanningScope.WEEK, 1) == date(2026, 2, 7)
    assert advance_selected_day(selected, PlanningScope.MONTH, 1) == date(2026, 2, 28)
    assert advance_selected_day(date(2026, 3, 31), PlanningScope.MONTH, -1) == date(2026, 2, 28)


@pytest.mark.asyncio
async def test_native_loader_queries_one_calendar_window_and_incomplete_reminders() -> None:
    native = FakeNativeData({
        "authorization.status": bridge_response(),
        "calendar.listContainers": bridge_response(result=[{"calendar_identifier": "work"}]),
        "reminders.listContainers": bridge_response(result=[{"calendar_identifier": "personal"}]),
        "calendar.query": bridge_response(result=[
            event("Design review", datetime(2026, 9, 1, 9, 30, tzinfo=CHICAGO)),
        ]),
        "reminders.query": bridge_response(result=[
            reminder("Buy filters", year=2026, month=9, day=1, hour=18, minute=0),
        ]),
    })

    projection = await load_native_planning(
        native,
        selected_day=date(2026, 9, 1),
        scope=PlanningScope.DAY,
        zone=CHICAGO,
    )

    assert [entry.title for entry in projection.days[0].entries] == ["Design review", "Buy filters"]
    assert native.calls == [
        ("authorization.status", None),
        ("calendar.listContainers", None),
        ("reminders.listContainers", None),
        ("calendar.query", {
            "start_unix_ms": int(datetime(2026, 9, 1, tzinfo=CHICAGO).timestamp() * 1_000),
            "end_unix_ms": int(datetime(2026, 9, 2, tzinfo=CHICAGO).timestamp() * 1_000),
            "calendar_identifiers": ["work"],
            "limit": 500,
        }),
        ("reminders.query", {
            "calendar_identifiers": ["personal"],
            "include_completed": False,
            "limit": 500,
            "due_start_unix_ms": int(datetime(2026, 9, 1, tzinfo=CHICAGO).timestamp() * 1_000),
            "due_end_unix_ms": int(datetime(2026, 9, 2, tzinfo=CHICAGO).timestamp() * 1_000),
        }),
    ]


@pytest.mark.asyncio
async def test_native_loader_requires_full_access_to_both_authoritative_stores() -> None:
    native = FakeNativeData({
        "authorization.status": bridge_response(reminders="denied"),
    })

    with pytest.raises(PlanningAuthorizationError, match="Reminders"):
        await load_native_planning(
            native,
            selected_day=date(2026, 9, 1),
            scope=PlanningScope.DAY,
            zone=CHICAGO,
        )

    assert native.calls == [("authorization.status", None)]
