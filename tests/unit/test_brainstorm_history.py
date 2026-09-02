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


class TestExport:
    """'The result has to leave the glasses.'

    The transcript streamed to the HUD and to stdout — which under JOAT.app
    is redirected to a log file, so there was no attached terminal to read
    and nothing a wearer could open later.
    """

    def _export(self, tmp_path: Path, topic: str = "cold-start latency"):
        history = BrainstormHistory(path=tmp_path / "b.json",
                                    export_dir=tmp_path / "exports")
        history.record(topic, ("codex", "claude"), TURNS, "converged 0.91")
        files = sorted((tmp_path / "exports").iterdir())
        return files[0].read_text(), files[0].name

    def test_a_finished_session_becomes_a_readable_file(self, tmp_path: Path) -> None:
        text, name = self._export(tmp_path)
        assert name.endswith("-cold-start-latency.md")
        assert text.startswith("# cold-start latency")
        assert "codex, claude" in text and "converged 0.91" in text

    def test_every_turn_is_attributed(self, tmp_path: Path) -> None:
        text, _ = self._export(tmp_path)
        for label, said in TURNS:
            assert f"## {label}" in text
            assert said.strip().splitlines()[0] in text

    def test_the_filename_survives_an_awkward_question(self, tmp_path: Path) -> None:
        _, name = self._export(tmp_path, "Tabs vs. spaces?! (again)")
        assert name.endswith(".md")
        assert "/" not in name and "?" not in name

    def test_an_unwritable_export_dir_never_breaks_recording(
        self, tmp_path: Path
    ) -> None:
        # The history and the export are separate on purpose: losing one
        # must not lose the other, and neither may break the session.
        blocked = tmp_path / "not-a-dir"
        blocked.write_text("x")
        history = BrainstormHistory(path=tmp_path / "b.json",
                                    export_dir=blocked / "nested")
        history.record("t", ("codex",), TURNS, "done")
        assert history.recent()[0].topic == "t"   # history still landed


class TestSpokenLookup:
    """An export nobody can find is not an export."""

    def _commands(self, tmp_path: Path):
        from joat.services.brainstorm_history import BrainstormHistoryCommands

        history = BrainstormHistory(path=tmp_path / "b.json",
                                    export_dir=tmp_path / "JOAT Brainstorms")
        return BrainstormHistoryCommands(history), history

    async def test_it_names_the_folder_and_the_newest(self, tmp_path: Path) -> None:
        commands, history = self._commands(tmp_path)
        history.record("cold-start latency", ("codex",), TURNS, "converged")
        answer = await commands.handle_command("where are my brainstorms")
        assert answer is not None
        assert "cold-start latency" in answer
        assert "JOAT Brainstorms" in answer

    async def test_it_answers_before_there_is_anything(self, tmp_path: Path) -> None:
        commands, _ = self._commands(tmp_path)
        answer = await commands.handle_command("where are my brainstorms saved")
        assert answer is not None and "No brainstorms yet" in answer

    async def test_ordinary_speech_is_left_alone(self, tmp_path: Path) -> None:
        commands, _ = self._commands(tmp_path)
        for said in ("where is the nearest coffee shop",
                     "what is a brainstorm", "brainstorm heat pumps"):
            assert await commands.handle_command(said) is None


class TestRecall:
    """Recent was a list you could not act on.

    The point of keeping sessions is coming back to the answer — which
    means asking for it out loud, since the glasses are voice-first.
    """

    def _commands(self, tmp_path: Path):
        from joat.services.brainstorm_history import BrainstormHistoryCommands

        history = BrainstormHistory(path=tmp_path / "b.json",
                                    export_dir=tmp_path / "exports")
        history.record("cold-start latency", ("codex", "claude"), TURNS, "converged")
        history.record("tabs or spaces", ("codex",),
                       (("JOAT (synthesis)", "AGREED: tabs."),), "converged")
        return BrainstormHistoryCommands(history), history

    async def test_read_the_last_brainstorm(self, tmp_path: Path) -> None:
        commands, _ = self._commands(tmp_path)
        answer = await commands.handle_command("read the last brainstorm")
        assert answer is not None
        assert "tabs or spaces" in answer          # newest
        assert "AGREED: tabs." in answer           # its synthesis

    async def test_recall_by_topic(self, tmp_path: Path) -> None:
        commands, _ = self._commands(tmp_path)
        answer = await commands.handle_command(
            "what did we decide about cold start latency")
        assert answer is not None
        assert "measure the gate" in answer

    async def test_an_unknown_topic_says_so(self, tmp_path: Path) -> None:
        commands, _ = self._commands(tmp_path)
        answer = await commands.handle_command("what did we decide about pizza")
        assert answer is not None and "No brainstorm about" in answer

    async def test_find_matches_only_the_question_not_the_turns(
        self, tmp_path: Path
    ) -> None:
        # Searching the collaborators' words for a half-remembered phrase
        # reads far more of the wearer's conversation than answering needs.
        _, history = self._commands(tmp_path)
        assert history.find("cold-start latency") is not None
        assert history.find("keep the weights resident") is None

    async def test_ordinary_speech_is_left_alone(self, tmp_path: Path) -> None:
        commands, _ = self._commands(tmp_path)
        for said in ("read the last message", "what did we decide",
                     "show me the weather"):
            assert await commands.handle_command(said) is None


class TestTheDefaultConstructionIsContained:
    """`__main__` builds `BrainstormHistory()` with no arguments.

    That is the construction whose defaults point at
    `~/Documents/JOAT Brainstorms`, and the one that wrote 312 real files
    into the wearer's Documents before tests/conftest.py redirected it.
    Every other test here passes an explicit path, so the no-argument
    form — the production form — went unexercised.
    """

    def test_no_argument_construction_writes_nowhere_near_home(self) -> None:
        from pathlib import Path

        from joat.services.brainstorm_history import BrainstormHistory

        history = BrainstormHistory()
        history.record("contained", ("codex",),
                       (("CODEX (proposer)", "x"),), "converged")

        home_documents = Path.home() / "Documents"
        assert not history.export_dir.is_relative_to(home_documents), (
            f"exports would land in {history.export_dir}"
        )
        assert list(history.export_dir.iterdir()), "nothing was exported at all"
        assert history.recent(), "nothing was recorded at all"


class TestTheDurableRecord:
    """docs/SESSION_MODEL.md:30-35 specifies what a stored session IS.

    None of the identity fields existed, so twenty sessions with full
    turns sat on disk reachable only as four lines of dead text — the
    wearer could see that past sessions existed and could not open one.
    """

    def _history(self, tmp_path):
        from joat.services.brainstorm_history import BrainstormHistory

        return BrainstormHistory(
            path=tmp_path / "brainstorms.json",
            export_dir=tmp_path / "exports",
        )

    def test_a_recorded_session_can_be_loaded_back_by_id(self, tmp_path) -> None:
        history = self._history(tmp_path)
        history.record("cold-start latency", ("Codex", "Claude"),
                       (("CODEX", "Cache it."), ("JOAT", "AGREED: cache it.")),
                       "done — converged")
        [row] = history.sessions()
        assert row.session_id
        loaded = history.load(row.session_id)
        assert loaded is not None
        assert loaded.topic == "cold-start latency"
        # The whole turn survives, not the collapsed row the panel draws.
        assert loaded.turns[0] == ("CODEX", "Cache it.")

    def test_the_id_never_encodes_the_question(self, tmp_path) -> None:
        """An id that carries wearer content is the content in another
        shape — and this one is logged and spoken back."""
        history = self._history(tmp_path)
        history.record("my private medical question", ("Codex",),
                       (("CODEX", "x"),), "done")
        [row] = history.sessions()
        for word in ("private", "medical", "question"):
            assert word not in row.session_id.lower()

    def test_an_unbranched_session_is_its_own_root(self, tmp_path) -> None:
        """So lineage is always answerable without a "no parent" case."""
        history = self._history(tmp_path)
        history.record("t", ("Codex",), (("CODEX", "x"),), "done")
        [row] = history.sessions()
        assert row.root_id == row.session_id
        assert row.parent_id == ""

    def test_status_is_machine_readable_not_prose(self, tmp_path) -> None:
        history = self._history(tmp_path)
        history.record("a", ("Codex",), (("CODEX", "x"),), "cancelled")
        history.record("b", ("Codex",), (("CODEX", "x"),), "done — converged")
        assert [r.status for r in history.sessions()] == ["settled", "cancelled"]

    def test_sessions_written_before_this_record_stay_selectable(
        self, tmp_path,
    ) -> None:
        """The wearer already has twenty of these on disk. A migration that
        drops them, or leaves them without ids, makes the picker lie about
        what exists."""
        import json

        path = tmp_path / "brainstorms.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([
            {"topic": "tabs or spaces", "speakers": ["Codex"],
             "turns": [["CODEX", "tabs"]], "verdict": "done", "at": 1000.0},
            {"topic": "BLE retries", "speakers": ["Claude"],
             "turns": [["CLAUDE", "back off"]], "verdict": "cancelled",
             "at": 2000.0},
        ]))
        history = self._history(tmp_path)
        rows = history.sessions()
        assert [r.topic for r in rows] == ["BLE retries", "tabs or spaces"]
        assert all(r.session_id for r in rows), "a legacy row must be openable"
        assert history.load(rows[0].session_id) is not None
        # Status is read out of the prose verdict, since it was not stored.
        assert rows[0].status == "cancelled"
        # Ids are stable across reloads, or the picker's rows change under
        # the wearer between one open and the next.
        assert [r.session_id for r in self._history(tmp_path).sessions()] == \
            [r.session_id for r in rows]


class TestTheExportIsAsPrivateAsTheStore:
    """BSD-07, from the audit's counter-pass: the export is a SECOND
    complete verbatim copy of the conversation, and it landed at the umask
    default (0644) while the private store it duplicates is 0600.
    SESSION_MODEL.md:37 calls that store private; a world-readable twin in
    ~/Documents makes the word do no work. The export stays — it is the
    point — but at the store's own mode.
    """

    def test_both_copies_carry_the_same_private_mode(self, tmp_path) -> None:
        import stat

        history = BrainstormHistory(
            path=tmp_path / "store.json", export_dir=tmp_path / "exports"
        )
        history.record(
            "a private question", ("codex",),
            (("CODEX (proposer)", "a private thought"),), "done",
        )
        store_mode = stat.S_IMODE((tmp_path / "store.json").stat().st_mode)
        exports = sorted((tmp_path / "exports").iterdir())
        assert exports, "the export is the product; it must still be written"
        export_mode = stat.S_IMODE(exports[0].stat().st_mode)
        assert store_mode == 0o600
        assert export_mode == store_mode, "the export is world-readable"

    def test_the_export_directory_is_private_too(self, tmp_path) -> None:
        """Its NAMES are the session topics, so the directory listing is
        itself wearer content."""
        import stat

        history = BrainstormHistory(
            path=tmp_path / "store.json", export_dir=tmp_path / "exports"
        )
        history.record(
            "a private question", ("codex",), (("CODEX", "x"),), "done",
        )
        mode = stat.S_IMODE((tmp_path / "exports").stat().st_mode)
        assert mode == 0o700
