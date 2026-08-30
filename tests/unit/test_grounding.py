"""Grounding: the facts a model cannot know, supplied per reply.

The bug this exists for: asked the time, a 3B model with no clock answered
"Current time is 2:47 PM" at 4:44 PM. It did not decline — it invented one,
and on a HUD the wearer has no cue that it was guessing.
"""

from __future__ import annotations

import time

from joat.agents.grounding import (
    Grounding,
    device_facts,
    local_clock,
    location_facts,
    static_fact,
)


class TestGrounding:
    def test_empty_when_no_sources(self) -> None:
        assert Grounding().facts() == ""

    def test_facts_are_bulleted_lines(self) -> None:
        g = Grounding([static_fact("Wearer: Emmanuel"), static_fact("Battery: 87%")])
        assert g.facts() == "- Wearer: Emmanuel\n- Battery: 87%"

    def test_a_broken_source_never_costs_the_answer(self) -> None:
        def boom() -> str:
            raise RuntimeError("sensor offline")

        g = Grounding([boom, static_fact("Battery: 87%")])
        assert g.facts() == "- Battery: 87%"

    def test_empty_values_are_omitted(self) -> None:
        g = Grounding([lambda: None, lambda: "", static_fact("here")])
        assert g.facts() == "- here"

    def test_clock_reports_the_supplied_instant(self) -> None:
        fixed = time.mktime((2026, 8, 26, 16, 44, 0, 0, 0, -1))
        text = local_clock(now=lambda: fixed)()
        assert "4:44 PM" in text
        assert "Wednesday" in text
        assert "August 2026" in text

    def test_clock_is_recomputed_per_call(self) -> None:
        # A clock captured once is wrong by however long the engine has been
        # up — the same bug wearing a different mask.
        ticks = iter([time.mktime((2026, 8, 26, 9, 0, 0, 0, 0, -1)),
                      time.mktime((2026, 8, 26, 17, 30, 0, 0, 0, -1))])
        source = local_clock(now=lambda: next(ticks))
        assert "9:00 AM" in source()
        assert "5:30 PM" in source()


class TestDeviceFacts:
    """Battery has been arriving and sitting on the transport the whole
    time, while 'what is my battery' was answered by a model that had never
    seen it — the clock bug in a different costume."""

    class _T:
        def __init__(self, device: object | None) -> None:
            self.device = device

    def test_reports_battery_and_wear(self) -> None:
        from joat.core.protocol import Device
        t = self._T(Device(connect="connected", battery=62, wearing=True, charging=False))
        assert device_facts(t)() == "Glasses: battery 62%, being worn"

    def test_marks_charging(self) -> None:
        from joat.core.protocol import Device
        t = self._T(Device(connect="connected", battery=99, wearing=False, charging=True))
        assert "99% (charging)" in device_facts(t)()
        assert "not being worn" in device_facts(t)()

    def test_absent_device_states_the_gap_rather_than_going_quiet(self) -> None:
        # Silence would let the model fill it in; a stated unknown will not.
        assert "unknown" in device_facts(self._T(None))()


class TestLocationFacts:
    class _T:
        def __init__(self, location: object | None) -> None:
            self._location = location

        def fresh_location(self) -> object | None:
            return self._location

    def test_only_calls_a_phone_fix_known_when_it_is_fresh(self) -> None:
        assert "fresh" in location_facts(self._T(object()))()

    def test_never_calls_a_missing_or_expired_fix_known(self) -> None:
        assert "unknown" in location_facts(self._T(None))()


class TestUnknowns:
    def test_unknowns_render_as_a_named_list(self) -> None:
        g = Grounding(unknowns=["the weather", "their calendar"])
        assert g.cannot_know() == "the weather, their calendar"

    def test_no_unknowns_is_empty(self) -> None:
        assert Grounding().cannot_know() == ""


class TestTurnMemory:
    """'Repeat that' must not be answered by a model that invents a
    plausible prior turn. History is bounded and expires on a long idle."""

    def _engine(self):  # type: ignore[no-untyped-def]
        from joat.agents.reply_engine import MlxReplyEngine
        return MlxReplyEngine()

    def test_recent_turns_replay_in_order(self) -> None:
        e = self._engine()
        e.remember("what time is it", "5:09 PM")
        e.remember("and the weather", "92F, clear")
        turns = e._recent_turns()
        assert [t["content"] for t in turns] == [
            "what time is it", "5:09 PM", "and the weather", "92F, clear"]

    def test_history_is_bounded_to_the_last_exchanges(self) -> None:
        e = self._engine()
        for i in range(6):
            e.remember(f"q{i}", f"a{i}")
        turns = e._recent_turns()
        assert len(turns) == e.HISTORY_TURNS * 2
        assert turns[0]["content"] == "q3"

    def test_history_expires_after_the_ttl(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        import time as time_mod
        e = self._engine()
        e.remember("old question", "old answer")
        real = time_mod.monotonic
        monkeypatch.setattr(time_mod, "monotonic", lambda: real() + e.HISTORY_TTL_S + 1)
        assert e._recent_turns() == []
