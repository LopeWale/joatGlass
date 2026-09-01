"""Finished Brainstorms survive a restart.

The artifact's blocking gap: 'Nothing survives a restart' — the snapshot
died with the process, so the synthesis the wearer walked away with was
gone and Recent had no source.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from joat.services.brainstorm_history import BrainstormHistory, PastBrainstorm
from joat.services.sinks import DebateStateSink

TURNS = (
    ("CODEX (proposer)", "keep the weights resident"),
    ("CLAUDE (critic)", "the OS evicts it anyway"),
    ("JOAT (synthesis)", "AGREED: measure the gate.\nOPEN: memory pressure."),
)


def store(tmp_path: Path, **kw) -> BrainstormHistory:
    return BrainstormHistory(path=tmp_path / "brainstorms.json", **kw)


class TestPersistence:
    def test_a_session_survives_a_new_process(self, tmp_path: Path) -> None:
        store(tmp_path).record("cold-start latency", ("codex", "claude"),
                               TURNS, "converged 0.91")
        # A brand-new object, as after a restart.
        recovered = store(tmp_path).recent()
        assert len(recovered) == 1
        assert recovered[0].topic == "cold-start latency"
        assert recovered[0].verdict == "converged 0.91"

    def test_the_synthesis_is_what_comes_back(self, tmp_path: Path) -> None:
        store(tmp_path).record("t", ("codex",), TURNS, "done")
        assert "AGREED: measure the gate." in store(tmp_path).recent()[0].synthesis

    def test_a_session_without_a_synthesis_falls_back_to_its_last_turn(
        self, tmp_path: Path
    ) -> None:
        store(tmp_path).record("t", ("codex",),
                               (("CODEX (proposer)", "only view"),), "cancelled")
        assert store(tmp_path).recent()[0].synthesis == "only view"

    def test_an_empty_session_is_not_kept(self, tmp_path: Path) -> None:
        store(tmp_path).record("nothing happened", ("codex",), (), "cancelled")
        assert store(tmp_path).recent() == []

    def test_newest_first(self, tmp_path: Path) -> None:
        s = store(tmp_path)
        for topic in ("first", "second", "third"):
            s.record(topic, ("codex",), TURNS, "done")
        assert [p.topic for p in s.recent()] == ["third", "second", "first"]

    def test_history_is_bounded(self, tmp_path: Path) -> None:
        # An unbounded file of wearer conversation is a liability.
        s = store(tmp_path, keep=3)
        for i in range(6):
            s.record(f"topic {i}", ("codex",), TURNS, "done")
        kept = s.recent(limit=99)
        assert len(kept) == 3
        assert [p.topic for p in kept] == ["topic 5", "topic 4", "topic 3"]


class TestResilience:
    def test_a_corrupt_file_reads_as_empty_rather_than_crashing(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "brainstorms.json"
        path.write_text("{ not json at all")
        assert BrainstormHistory(path=path).recent() == []

    def test_a_partially_malformed_entry_is_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "brainstorms.json"
        path.write_text(json.dumps([
            {"topic": "good", "speakers": ["codex"],
             "turns": [["CODEX", "said"]], "verdict": "ok", "at": time.time()},
            {"topic": "bad", "turns": "not a list"},
            "not even a record",
        ]))
        recovered = BrainstormHistory(path=path).recent()
        assert [p.topic for p in recovered] == ["good"]

    def test_an_unwritable_path_never_raises(self, tmp_path: Path) -> None:
        # Losing history must not take down the session that produced it.
        blocked = tmp_path / "file-not-dir"
        blocked.write_text("x")
        BrainstormHistory(path=blocked / "nested.json").record(
            "t", ("codex",), TURNS, "done")


class TestRows:
    def test_rows_are_glance_sized(self, tmp_path: Path) -> None:
        s = store(tmp_path)
        s.record("cold-start latency", ("codex", "claude"), TURNS, "converged")
        [row] = s.rows()
        assert row == "cold-start latency · 3 turns · just now"
        assert len(row) <= 46

    def test_a_long_question_loses_its_tail_not_its_age(self, tmp_path: Path) -> None:
        # Trimming the whole row cut the age off the end, and the age is
        # what makes a list of past questions scannable at all.
        s = store(tmp_path)
        s.record("how should we cut cold-start latency on the wearable",
                 ("codex",), TURNS, "converged")
        [row] = s.rows()
        assert len(row) <= 46
        assert row.endswith("· 3 turns · just now")
        assert "…" in row                      # the QUESTION gave way

    def test_age_reads_the_way_a_person_would_say_it(self) -> None:
        now = time.time()
        def past(delta: float) -> PastBrainstorm:
            return PastBrainstorm("t", (), (("A", "b"),), "v", now - delta)
        assert past(10).age(now) == "just now"
        assert past(600).age(now) == "10m ago"
        assert past(7200).age(now) == "2h ago"
        assert past(172_800).age(now) == "2d ago"


class TestSinkIntegration:
    async def test_closing_a_session_records_it(self, tmp_path: Path) -> None:
        history = store(tmp_path)
        sink = DebateStateSink(on_finished=history.record)
        await sink.open("tabs or spaces", ["codex", "claude"])
        await sink.turn("CODEX (proposer)")
        await sink.chunk("tabs indent faster")
        await sink.close("converged 0.90")

        [kept] = history.recent()
        assert kept.topic == "tabs or spaces"
        assert kept.turns == (("CODEX (proposer)", "tabs indent faster"),)
        assert kept.verdict == "converged 0.90"

    async def test_a_failing_store_never_breaks_the_session(self) -> None:
        def explode(*_args: object) -> None:
            raise RuntimeError("disk full")

        sink = DebateStateSink(on_finished=explode)
        await sink.open("t", ["codex"])
        await sink.turn("CODEX")
        await sink.chunk("said something")
        await sink.close("done")          # must not raise
        assert sink.snapshot.verdict == "done"
