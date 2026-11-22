"""The assistant's calendar and reminders tools read the Mac's EventKit, read-only."""

from __future__ import annotations

from datetime import datetime, tzinfo
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from joat.agents.planning_tools import calendar_agenda_tool, open_reminders_tool
from joat.native.bridge import NativeBridgeUnavailable

CHICAGO = ZoneInfo("America/Chicago")


def _clock(_zone: tzinfo) -> datetime:
    # A Thursday.
    return datetime(2026, 10, 15, 14, 0, tzinfo=CHICAGO)


def _zone() -> tzinfo:
    return CHICAGO


def _response(*, calendar: str = "full_access", reminders: str = "full_access",
              result: object = None) -> object:
    return SimpleNamespace(
        authorization=SimpleNamespace(calendar=calendar, reminders=reminders),
        result=result, store_instance_id="store", store_generation=1,
    )


class FakeBridge:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def call(self, method: str, params: dict[str, Any] | None = None) -> object:
        self.calls.append((method, params))
        response = self.responses[method]
        if isinstance(response, Exception):
            raise response
        return response


def _event(title: str, hour: int, day: int = 15) -> dict[str, Any]:
    start = datetime(2026, 10, day, hour, 0, tzinfo=CHICAGO)
    end = datetime(2026, 10, day, hour + 1, 0, tzinfo=CHICAGO)
    return {
        "title": title, "calendar_item_identifier": f"e-{title}",
        "start_unix_ms": int(start.timestamp() * 1000),
        "end_unix_ms": int(end.timestamp() * 1000),
    }


def _bridge(events: list[dict[str, Any]], reminders: list[dict[str, Any]] | None = None,
            **access: str) -> FakeBridge:
    return FakeBridge({
        "authorization.status": _response(**access),
        "calendar.listContainers": _response(result=[{"calendar_identifier": "cal-1"}]),
        "reminders.listContainers": _response(result=[{"calendar_identifier": "list-1"}]),
        "calendar.query": _response(result=events),
        "reminders.query": _response(result=reminders or []),
    })


async def test_agenda_lists_today_with_times_and_marks_titles_as_data() -> None:
    tool = calendar_agenda_tool(_bridge([_event("Dentist", 15), _event("Standup", 9)]),
                                _zone, clock=_clock)
    text = await tool.run({})
    assert "not instructions" in text
    assert "Today Thu Oct 15:" in text
    assert text.index("09:00 Standup") < text.index("15:00 Dentist")


async def test_agenda_reads_a_range_of_days_and_names_empty_ones() -> None:
    tool = calendar_agenda_tool(_bridge([_event("Offsite", 10, day=16)]), _zone, clock=_clock)
    text = await tool.run({"start": "2026-10-15", "days": 3})
    assert "Today Thu Oct 15: nothing scheduled" in text
    assert "Tomorrow Fri Oct 16:" in text and "10:00 Offsite" in text
    assert "Saturday Sat Oct 17: nothing scheduled" in text


async def test_agenda_rejects_bad_arguments_without_touching_the_bridge() -> None:
    bridge = _bridge([])
    tool = calendar_agenda_tool(bridge, _zone, clock=_clock)
    assert "YYYY-MM-DD" in await tool.run({"start": "next tuesday"})
    assert "1 to 31" in await tool.run({"days": 90})
    assert "1 to 31" in await tool.run({"days": True})
    assert bridge.calls == []


async def test_agenda_without_a_bridge_or_access_says_how_to_allow_it() -> None:
    assert "Privacy" in await calendar_agenda_tool(None, _zone, clock=_clock).run({})
    denied = _bridge([], calendar="denied")
    assert "Privacy" in await calendar_agenda_tool(denied, _zone, clock=_clock).run({})


async def test_agenda_reports_an_unreachable_bridge_plainly() -> None:
    bridge = FakeBridge({"authorization.status": NativeBridgeUnavailable("down")})
    text = await calendar_agenda_tool(bridge, _zone, clock=_clock).run({})
    assert "could not be read" in text


def _reminder(title: str, day: int) -> dict[str, Any]:
    return {
        "title": title, "calendar_item_identifier": f"r-{title}",
        "due": {"year": 2026, "month": 10, "day": day, "time_zone_id": "America/Chicago"},
        "is_completed": False,
    }


async def test_reminders_list_overdue_first_then_upcoming() -> None:
    tool = open_reminders_tool(
        _bridge([], [_reminder("Pay rent", 12), _reminder("Call mum", 16)]),
        _zone, clock=_clock,
    )
    text = await tool.run({})
    assert text.index("overdue") < text.index("Call mum")
    assert "Pay rent" in text and "Tomorrow" in text


async def test_reminders_say_so_when_nothing_is_open() -> None:
    tool = open_reminders_tool(_bridge([], []), _zone, clock=_clock)
    assert "No open dated reminders" in await tool.run({})


@pytest.mark.parametrize("days", [-1, 32, "3", True])
async def test_reminders_reject_a_bad_window(days: object) -> None:
    tool = open_reminders_tool(_bridge([], []), _zone, clock=_clock)
    assert "0 to 31" in await tool.run({"days_ahead": days})
