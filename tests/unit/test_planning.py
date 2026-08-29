from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from joat.services.planning import (
    PlanningScope,
    advance_selected_day,
    month_grid,
    project_planning,
)


CHICAGO = ZoneInfo("America/Chicago")


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


def test_scope_navigation_keeps_the_wearer_on_human_calendar_boundaries() -> None:
    selected = date(2026, 1, 31)

    assert advance_selected_day(selected, PlanningScope.DAY, 1) == date(2026, 2, 1)
    assert advance_selected_day(selected, PlanningScope.WEEK, 1) == date(2026, 2, 7)
    assert advance_selected_day(selected, PlanningScope.MONTH, 1) == date(2026, 2, 28)
    assert advance_selected_day(date(2026, 3, 31), PlanningScope.MONTH, -1) == date(2026, 2, 28)
