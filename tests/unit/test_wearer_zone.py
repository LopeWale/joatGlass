"""The wearer's zone, and the fallback that keeps it honest."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from joat.core.wearer_zone import WearerZone

CHICAGO = ZoneInfo("America/Chicago")
LONDON = ZoneInfo("Europe/London")


def zone(name: str = "America/Chicago") -> WearerZone:
    return WearerZone(lambda: ZoneInfo(name))


class TestAccept:
    def test_a_real_name_becomes_the_wearers_zone(self) -> None:
        wearer = zone()
        assert wearer.current() is CHICAGO
        assert wearer.accept("Europe/London") is True
        assert wearer.current() == LONDON
        assert wearer.name() == "Europe/London"

    def test_the_same_name_twice_is_not_a_change(self) -> None:
        # The caller uses the return value to decide whether anything needs
        # repainting; every position fix carries the zone, so an unchanged
        # one must not look like news.
        wearer = zone()
        assert wearer.accept("Europe/London") is True
        assert wearer.accept("Europe/London") is False

    @pytest.mark.parametrize(
        "value",
        [None, "", "   ", "Mars/Olympus_Mons", "Not A Zone", "A" * 65],
    )
    def test_an_unusable_name_is_refused_and_the_zone_stands(self, value: str | None) -> None:
        wearer = zone()
        wearer.accept("Europe/London")
        assert wearer.accept(value) is False
        assert wearer.current() == LONDON

    def test_an_unknown_name_never_falls_back_silently(self) -> None:
        # Refusing must KEEP the last good zone rather than reverting to the
        # Mac's: a wearer in London whose phone reports a name this tzdata
        # has not heard of is still in London.
        wearer = zone()
        wearer.accept("Europe/London")
        wearer.accept("Middle_Earth/Shire")
        assert wearer.current() == LONDON
        assert wearer.name() == "Europe/London"


class TestFallback:
    def test_the_macs_zone_stands_in_until_a_phone_speaks(self) -> None:
        wearer = zone()
        assert wearer.name() is None
        assert wearer.current() is CHICAGO
        assert wearer.home() is CHICAGO

    def test_home_is_unmoved_by_the_wearer(self) -> None:
        # All-day events are bucketed in home(); if accepting a zone moved
        # it, every all-day row would slide the moment the wearer travelled.
        wearer = zone()
        wearer.accept("Europe/London")
        assert wearer.home() is CHICAGO
        assert wearer.current() == LONDON


class TestTravelling:
    def test_false_before_a_phone_reports_anything(self) -> None:
        assert zone().travelling() is False

    def test_false_when_the_phone_agrees_with_the_mac(self) -> None:
        wearer = zone()
        wearer.accept("America/Chicago")
        assert wearer.travelling() is False

    def test_true_when_the_offsets_differ(self) -> None:
        wearer = zone()
        wearer.accept("Europe/London")
        assert wearer.travelling() is True

    def test_compared_by_offset_not_by_name(self) -> None:
        # Two names, one offset, all year. Reporting this as "travelling"
        # would put a pointless second clock in front of the model.
        wearer = WearerZone(lambda: ZoneInfo("Europe/London"))
        wearer.accept("Europe/Guernsey")
        assert wearer.name() == "Europe/Guernsey"
        assert wearer.travelling() is False

    def test_a_shared_offset_today_can_still_diverge_at_a_dst_boundary(self) -> None:
        # Phoenix does not observe DST; Denver does. In January they agree.
        # The check is deliberately "right now", which is when it is read.
        wearer = WearerZone(lambda: ZoneInfo("America/Phoenix"))
        wearer.accept("America/Denver")
        january = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
        july = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
        assert january.astimezone(ZoneInfo("America/Denver")).utcoffset() == \
            january.astimezone(ZoneInfo("America/Phoenix")).utcoffset()
        assert july.astimezone(ZoneInfo("America/Denver")).utcoffset() != \
            july.astimezone(ZoneInfo("America/Phoenix")).utcoffset()
