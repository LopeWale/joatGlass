"""Native Calendar + Reminders projection for the glasses Planning surface.

EventKit keeps events and reminders in separate stores.  The wearer should not
have to understand that storage boundary: this module produces one read-only
calendar-shaped projection while retaining the source and native identifier for
each entry.  It deliberately does *not* create, complete, or delete anything.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from enum import StrEnum
from typing import Any, Literal


class PlanningScope(StrEnum):
    """The three deliberately bounded ways to inspect a schedule."""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"


PlanningEntryKind = Literal["event", "reminder"]


@dataclass(frozen=True, slots=True)
class PlanningEntry:
    """One native item rendered inside a local calendar day."""

    kind: PlanningEntryKind
    native_identifier: str
    title: str
    time_label: str
    sort_at: datetime


@dataclass(frozen=True, slots=True)
class PlanningDay:
    """The schedule for one calendar day, including empty days in a range."""

    day: date
    entries: tuple[PlanningEntry, ...]
    is_selected: bool

    @property
    def event_count(self) -> int:
        return sum(entry.kind == "event" for entry in self.entries)

    @property
    def reminder_count(self) -> int:
        return sum(entry.kind == "reminder" for entry in self.entries)


@dataclass(frozen=True, slots=True)
class PlanningProjection:
    """A local-time, read-only range that the HUD can navigate safely."""

    scope: PlanningScope
    selected_day: date
    title: str
    days: tuple[PlanningDay, ...]


def month_grid(selected_day: date) -> tuple[date, ...]:
    """Return the complete Monday-first six-week grid around ``selected_day``.

    A fixed six rows prevents the physical HUD layout from jumping for months
    that happen to span four, five, or six rendered weeks.
    """

    first = selected_day.replace(day=1)
    grid_start = first - timedelta(days=first.weekday())
    return tuple(grid_start + timedelta(days=offset) for offset in range(42))


def advance_selected_day(selected_day: date, scope: PlanningScope, direction: int) -> date:
    """Move a day, week, or month without an invalid end-of-month date."""

    if direction not in {-1, 1}:
        raise ValueError("planning navigation direction must be -1 or 1")
    if scope is PlanningScope.DAY:
        return selected_day + timedelta(days=direction)
    if scope is PlanningScope.WEEK:
        return selected_day + timedelta(days=7 * direction)

    target_month = selected_day.month + direction
    target_year = selected_day.year
    if target_month == 0:
        target_year -= 1
        target_month = 12
    elif target_month == 13:
        target_year += 1
        target_month = 1
    return date(target_year, target_month, min(selected_day.day, monthrange(target_year, target_month)[1]))


def project_planning(
    *,
    selected_day: date,
    scope: PlanningScope,
    zone: tzinfo,
    events: list[dict[str, Any]],
    reminders: list[dict[str, Any]],
) -> PlanningProjection:
    """Merge native EventKit DTOs into a bounded local-time schedule.

    Inputs are intentionally plain bridge DTO dictionaries.  Invalid or
    undated records are omitted rather than guessed into a day, avoiding a
    misleading plan.  Completed reminders are excluded defensively even when
    the native query asked EventKit to exclude them.
    """

    range_days = _days_for_scope(selected_day, scope)
    lower, upper = range_days[0], range_days[-1]
    entries_by_day: dict[date, list[PlanningEntry]] = {day: [] for day in range_days}

    for item in events:
        entry = _event_entry(item, zone)
        if entry is None:
            continue
        entry_day = entry.sort_at.date()
        if lower <= entry_day <= upper:
            entries_by_day[entry_day].append(entry)

    for item in reminders:
        entry = _reminder_entry(item, zone)
        if entry is None:
            continue
        entry_day = entry.sort_at.date()
        if lower <= entry_day <= upper:
            entries_by_day[entry_day].append(entry)

    days = tuple(
        PlanningDay(
            day=day,
            entries=tuple(sorted(entries_by_day[day], key=_entry_sort_key)),
            is_selected=day == selected_day,
        )
        for day in range_days
    )
    return PlanningProjection(
        scope=scope,
        selected_day=selected_day,
        title=_title_for_scope(selected_day, scope, range_days),
        days=days,
    )


def _days_for_scope(selected_day: date, scope: PlanningScope) -> tuple[date, ...]:
    if scope is PlanningScope.DAY:
        return (selected_day,)
    if scope is PlanningScope.WEEK:
        start = selected_day - timedelta(days=selected_day.weekday())
        return tuple(start + timedelta(days=offset) for offset in range(7))
    return month_grid(selected_day)


def _title_for_scope(
    selected_day: date,
    scope: PlanningScope,
    range_days: tuple[date, ...],
) -> str:
    if scope is PlanningScope.DAY:
        return f"PLAN · {selected_day.strftime('%a %b').upper()} {selected_day.day}"
    if scope is PlanningScope.MONTH:
        return f"PLAN · {selected_day.strftime('%B').upper()} {selected_day.year}"
    start, end = range_days[0], range_days[-1]
    if start.month == end.month:
        return f"PLAN · {start.strftime('%b').upper()} {start.day}–{end.day}"
    return f"PLAN · {start.strftime('%b').upper()} {start.day}–{end.strftime('%b').upper()} {end.day}"


def _event_entry(item: dict[str, Any], zone: tzinfo) -> PlanningEntry | None:
    start = item.get("start_unix_ms")
    native_identifier = item.get("calendar_item_identifier")
    if not isinstance(start, (int, float)) or not isinstance(native_identifier, str) or not native_identifier:
        return None
    local = datetime.fromtimestamp(start / 1_000, zone)
    is_all_day = item.get("is_all_day") is True
    return PlanningEntry(
        kind="event",
        native_identifier=native_identifier,
        title=_title(item),
        time_label="ALL DAY" if is_all_day else local.strftime("%H:%M"),
        sort_at=datetime.combine(local.date(), time.min, zone) if is_all_day else local,
    )


def _reminder_entry(item: dict[str, Any], zone: tzinfo) -> PlanningEntry | None:
    if item.get("is_completed") is True:
        return None
    native_identifier = item.get("calendar_item_identifier")
    due = item.get("due")
    if not isinstance(native_identifier, str) or not native_identifier or not isinstance(due, dict):
        return None
    year = due.get("year")
    month = due.get("month")
    day = due.get("day")
    if not isinstance(year, int) or not isinstance(month, int) or not isinstance(day, int):
        return None
    hour = due.get("hour")
    minute = due.get("minute")
    if isinstance(hour, int) and isinstance(minute, int):
        is_timed = True
        resolved_hour, resolved_minute = hour, minute
    else:
        is_timed = False
        resolved_hour, resolved_minute = 0, 0
    try:
        sort_at = datetime(year, month, day, resolved_hour, resolved_minute, tzinfo=zone)
    except ValueError:
        return None
    return PlanningEntry(
        kind="reminder",
        native_identifier=native_identifier,
        title=_title(item),
        time_label=sort_at.strftime("%H:%M") if is_timed else "ALL DAY",
        sort_at=sort_at,
    )


def _title(item: dict[str, Any]) -> str:
    value = item.get("title")
    return value.strip() if isinstance(value, str) and value.strip() else "Untitled"


def _entry_sort_key(entry: PlanningEntry) -> tuple[datetime, int, str, str]:
    # Untimed reminders are commitments for the day, not false midnight
    # appointments.  Place them after timed agenda entries. Events precede
    # reminders at an identical instant, and title/ID keep selection stable.
    scheduled_at = entry.sort_at
    if entry.kind == "reminder" and entry.time_label == "ALL DAY":
        scheduled_at = scheduled_at.replace(hour=23, minute=59, second=59)
    return (scheduled_at, 0 if entry.kind == "event" else 1, entry.title.casefold(), entry.native_identifier)
