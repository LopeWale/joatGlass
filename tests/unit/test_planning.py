from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from joat.native.bridge import NativeBridgeProtocolError, NativeBridgeUnavailable
from joat.services.planning import (
    PlanningAuthorizationError,
    PlanningScope,
    advance_selected_day,
    load_native_planning,
    month_grid,
    personal_data_bridge_state,
    project_planning,
)


CHICAGO = ZoneInfo("America/Chicago")


class FakeNativeData:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    async def call(self, method: str, params: dict[str, object] | None = None) -> object:
        self.calls.append((method, params))
        response = self.responses[method]
        if isinstance(response, tuple):
            if not response:
                raise AssertionError(f"exhausted sequential response for {method}")
            response, *remaining = response
            self.responses[method] = tuple(remaining)
        if isinstance(response, Exception):
            raise response
        return response


def bridge_response(
    *,
    calendar: str = "full_access",
    reminders: str = "full_access",
    result: object = None,
    store_instance_id: str = "eventkit-store",
    store_generation: int = 1,
) -> object:
    return SimpleNamespace(
        authorization=SimpleNamespace(calendar=calendar, reminders=reminders),
        result=result,
        store_instance_id=store_instance_id,
        store_generation=store_generation,
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
    time_zone_id: str | None = None,
) -> dict[str, object]:
    due: dict[str, object] = {"year": year, "month": month, "day": day}
    if hour is not None:
        due["hour"] = hour
    if minute is not None:
        due["minute"] = minute
    if time_zone_id is not None:
        due["time_zone_id"] = time_zone_id
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


def test_reminder_uses_its_native_civil_zone_then_projects_into_the_wearer_zone() -> None:
    projection = project_planning(
        selected_day=date(2026, 9, 1),
        scope=PlanningScope.DAY,
        zone=CHICAGO,
        events=[],
        reminders=[
            reminder(
                "Call Berlin",
                year=2026,
                month=9,
                day=2,
                hour=0,
                minute=30,
                time_zone_id="Europe/Berlin",
            ),
        ],
    )

    assert [(entry.title, entry.time_label) for entry in projection.days[0].entries] == [
        ("Call Berlin", "17:30"),
    ]


def test_planning_drops_a_mislabeled_reminder_from_the_event_feed() -> None:
    projection = project_planning(
        selected_day=date(2026, 9, 1),
        scope=PlanningScope.DAY,
        zone=CHICAGO,
        events=[{
            **event("Scheduled reminder projection", datetime(2026, 9, 1, 9, 30, tzinfo=CHICAGO)),
            "entity": "reminder",
        }],
        reminders=[],
    )

    assert projection.days[0].entries == ()


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


@pytest.mark.asyncio
async def test_native_loader_accepts_the_real_swift_full_access_enum_spelling() -> None:
    native = FakeNativeData({
        "authorization.status": bridge_response(
            calendar="fullAccess",
            reminders="fullAccess",
        ),
        "calendar.listContainers": bridge_response(result=[]),
        "reminders.listContainers": bridge_response(result=[]),
        "calendar.query": bridge_response(result=[]),
        "reminders.query": bridge_response(result=[]),
    })

    projection = await load_native_planning(
        native,
        selected_day=date(2026, 9, 1),
        scope=PlanningScope.DAY,
        zone=CHICAGO,
    )

    assert projection.days[0].entries == ()


@pytest.mark.asyncio
async def test_native_loader_retries_instead_of_merging_eventkit_generations() -> None:
    native = FakeNativeData({
        "authorization.status": (
            bridge_response(store_generation=1),
            bridge_response(store_generation=2),
        ),
        # The first attempt observes a write after authorization. The retry
        # then obtains its entire agenda from generation 2.
        "calendar.listContainers": (
            bridge_response(result=[], store_generation=2),
            bridge_response(result=[], store_generation=2),
        ),
        "reminders.listContainers": bridge_response(result=[], store_generation=2),
        "calendar.query": bridge_response(result=[], store_generation=2),
        "reminders.query": bridge_response(result=[], store_generation=2),
    })

    projection = await load_native_planning(
        native,
        selected_day=date(2026, 9, 1),
        scope=PlanningScope.DAY,
        zone=CHICAGO,
    )

    assert projection.days[0].entries == ()
    assert [method for method, _ in native.calls].count("authorization.status") == 2


@pytest.mark.asyncio
async def test_personal_data_bridge_state_never_retains_eventkit_error_detail() -> None:
    ready = FakeNativeData({
        "authorization.status": bridge_response(
            calendar="fullAccess",
            reminders="fullAccess",
        ),
    })
    access_missing = FakeNativeData({
        "authorization.status": bridge_response(reminders="denied"),
    })
    unavailable = FakeNativeData({
        "authorization.status": NativeBridgeUnavailable("private socket detail"),
    })
    protocol_error = FakeNativeData({
        "authorization.status": NativeBridgeProtocolError("private response detail"),
    })

    assert await personal_data_bridge_state(None) == "not_configured"
    assert await personal_data_bridge_state(ready) == "ready"
    assert await personal_data_bridge_state(access_missing) == "authorization_required"
    assert await personal_data_bridge_state(unavailable) == "unavailable"
    assert await personal_data_bridge_state(protocol_error) == "protocol_error"


LONDON = ZoneInfo("Europe/London")


def all_day_event(title: str, day: date, authored_in: ZoneInfo) -> dict[str, object]:
    """EventKit hands an all-day event as midnight in the CALENDAR's zone."""
    start = datetime(day.year, day.month, day.day, tzinfo=authored_in)
    end = datetime(day.year, day.month, day.day, tzinfo=authored_in)
    return {
        "title": title,
        "calendar_item_identifier": f"all-day-{title}",
        "start_unix_ms": int(start.timestamp() * 1_000),
        "end_unix_ms": int(end.timestamp() * 1_000) + 86_400_000,
        "is_all_day": True,
    }


def _titles(projection: object, day: date) -> list[str]:
    for planning_day in projection.days:  # type: ignore[attr-defined]
        if planning_day.day == day:
            return [entry.title for entry in planning_day.entries]
    return []


class TestTheWearersZone:
    """What travelling must and must not change.

    A timed event is an INSTANT: render it where it will be read. An all-day
    event is a DATE: converting its authored midnight into another zone
    slides it across midnight and moves it a whole day.
    """

    def test_a_timed_event_is_shown_in_the_wearers_zone(self) -> None:
        # 09:30 in Chicago is 15:30 in London, and a wearer in London
        # checking "how long until" must read it against a London clock.
        projection = project_planning(
            selected_day=date(2026, 9, 1),
            scope=PlanningScope.DAY,
            zone=LONDON,
            events=[event("Design review", datetime(2026, 9, 1, 9, 30, tzinfo=CHICAGO))],
            reminders=[],
            all_day_zone=CHICAGO,
        )
        labels = [
            entry.time_label
            for planning_day in projection.days
            if planning_day.day == date(2026, 9, 1)
            for entry in planning_day.entries
        ]
        assert labels == ["15:30"]

    def test_an_all_day_event_does_not_move_when_the_wearer_does(self) -> None:
        # Chicago midnight on Sep 1 is 06:00 London — same day. But midnight
        # viewed from Tokyo (UTC+9) is 14:00 on Sep 1, and from Honolulu
        # (UTC-10) it is 19:00 on AUG 31. Bucketing in the authoring zone is
        # what keeps "Conference, Sep 1" on Sep 1 wherever the wearer stands.
        conference = all_day_event("Conference", date(2026, 9, 1), CHICAGO)
        for elsewhere in ("Pacific/Honolulu", "Asia/Tokyo", "Europe/London"):
            projection = project_planning(
                selected_day=date(2026, 9, 1),
                scope=PlanningScope.WEEK,
                zone=ZoneInfo(elsewhere),
                events=[conference],
                reminders=[],
                all_day_zone=CHICAGO,
            )
            assert "Conference" in _titles(projection, date(2026, 9, 1)), elsewhere
            assert "Conference" not in _titles(projection, date(2026, 8, 31)), elsewhere

    def test_the_all_day_guard_is_what_holds_it(self) -> None:
        # Without the authoring zone the same event lands on Aug 31 from
        # Honolulu — the exact silent day-shift the guard exists to stop.
        projection = project_planning(
            selected_day=date(2026, 9, 1),
            scope=PlanningScope.WEEK,
            zone=ZoneInfo("Pacific/Honolulu"),
            events=[all_day_event("Conference", date(2026, 9, 1), CHICAGO)],
            reminders=[],
        )
        assert "Conference" in _titles(projection, date(2026, 8, 31))

    def test_a_date_only_reminder_stays_on_its_date(self) -> None:
        # Same trap, different store: a due date with no time is a date.
        projection = project_planning(
            selected_day=date(2026, 9, 1),
            scope=PlanningScope.WEEK,
            zone=ZoneInfo("Pacific/Honolulu"),
            events=[],
            reminders=[
                reminder("Book dentist", year=2026, month=9, day=1,
                         time_zone_id="America/Chicago"),
            ],
        )
        assert "Book dentist" in _titles(projection, date(2026, 9, 1))
        assert "Book dentist" not in _titles(projection, date(2026, 8, 31))

    def test_a_timed_reminder_still_converts(self) -> None:
        # 18:00 Chicago is 00:00 the next day in London, and that IS when it
        # is due where the wearer is standing.
        projection = project_planning(
            selected_day=date(2026, 9, 1),
            scope=PlanningScope.WEEK,
            zone=LONDON,
            events=[],
            reminders=[
                reminder("Buy filters", year=2026, month=9, day=1, hour=18, minute=0,
                         time_zone_id="America/Chicago"),
            ],
        )
        assert "Buy filters" in _titles(projection, date(2026, 9, 2))
