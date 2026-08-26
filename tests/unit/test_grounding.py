"""Grounding: the facts a model cannot know, supplied per reply.

The bug this exists for: asked the time, a 3B model with no clock answered
"Current time is 2:47 PM" at 4:44 PM. It did not decline — it invented one,
and on a HUD the wearer has no cue that it was guessing.
"""

from __future__ import annotations

import time

from joat.agents.grounding import Grounding, local_clock, static_fact


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
