"""RoundTable: N collaborators, one at a time, in an order the wearer sets.

The wearer's requirements this pins: "the debate is going to be one at a
time", "I can pick which between codex or Claude to pass the questions to
and which should respond next", and OpenClaw seated as a third collaborator
rather than a rewrite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from joat.services.roundtable import (
    Participant,
    RoundTable,
    build_prompt,
    parse_direction,
)


def seat(name: str, role: str = "peer") -> Participant:
    async def stream(prompt: str) -> AsyncIterator[str]:
        yield f"{name} says something"

    return Participant(name=name, role=role, stream=stream)


def table(*names: str) -> RoundTable:
    return RoundTable([seat(n) for n in names])


class TestTurnOrder:
    def test_round_robins_when_undirected(self) -> None:
        t = table("codex", "claude", "openclaw")
        assert [t.next_speaker().name for _ in range(4)] == [
            "codex", "claude", "openclaw", "codex"]

    def test_direction_sets_who_speaks_next(self) -> None:
        t = table("codex", "claude", "openclaw")
        t.next_speaker()  # codex
        assert t.direct("openclaw") is not None
        assert t.next_speaker().name == "openclaw"

    def test_rotation_continues_from_the_directed_seat(self) -> None:
        t = table("codex", "claude", "openclaw")
        t.direct("claude")
        assert [t.next_speaker().name for _ in range(3)] == [
            "claude", "openclaw", "codex"]

    def test_unknown_name_changes_nothing(self) -> None:
        t = table("codex", "claude")
        assert t.direct("gemini") is None
        assert t.next_speaker().name == "codex"

    def test_a_table_needs_a_seat(self) -> None:
        try:
            RoundTable([])
        except ValueError:
            return
        raise AssertionError("an empty round table must be refused")


class TestNameResolution:
    def test_matches_a_name_inside_a_phrase(self) -> None:
        t = table("codex", "claude")
        assert t.find("ask codex about it").name == "codex"

    def test_is_case_insensitive(self) -> None:
        assert table("codex").find("CODEX").name == "codex"


class TestSpokenDirection:
    def test_recognises_nominations(self) -> None:
        t = table("codex", "claude", "openclaw")
        for said in ("claude respond", "codex first", "openclaw what do you think",
                     "ask claude", "over to openclaw", "codex, your turn"):
            assert parse_direction(said, t) is not None, said

    def test_ignores_ordinary_speech_that_mentions_a_name(self) -> None:
        # The trap: discussion ABOUT a participant must not re-order the room.
        t = table("codex", "claude")
        for said in ("what did codex mean by that",
                     "I disagree with claude on the second point",
                     "stop"):
            assert parse_direction(said, t) is None, said


class TestPromptConstruction:
    def test_the_opener_has_no_transcript(self) -> None:
        t = table("codex")
        prompt = build_prompt("tabs or spaces", t.peek(), [])
        assert "tabs or spaces" in prompt
        assert "Open the discussion" in prompt

    def test_later_speakers_see_every_prior_turn_in_order(self) -> None:
        # This IS the sequencing guarantee: turn N+1 cannot be built before
        # turn N exists, so "one at a time" is structural, not a policy.
        t = table("codex", "claude")
        transcript = [("CODEX (peer)", "use tabs"), ("CLAUDE (peer)", "spaces read better")]
        prompt = build_prompt("tabs or spaces", t.peek(), transcript)
        assert prompt.index("use tabs") < prompt.index("spaces read better")
        assert "do not restate" in prompt


class TestFailureNotices:
    """A seat that could not speak must be visible and unquotable."""

    def test_recognised_wherever_it_lands_in_the_turn(self) -> None:
        from joat.services.roundtable import failure_notice, is_failure

        note = failure_notice("claude", "exited 1 — see the log")
        assert is_failure(note)
        # The live shape: the CLI streams what it has, THEN dies.
        assert is_failure(f"You're out of usage credits.\n{note}")
        assert is_failure(f"A partial thought…\n\n{note}\n")

    def test_an_ordinary_turn_is_not_a_failure(self) -> None:
        from joat.services.roundtable import is_failure

        assert not is_failure("Cache the model in RAM. [1] is the weak claim.")
        assert not is_failure("")


class TestLensFirstAnswerContract:
    def test_prompts_keep_the_live_glasses_stream_concise(self) -> None:
        """Brainstorm's production image surface has no scrollback, so its
        prompt cannot claim that the wearer can read arbitrarily deep output
        by swiping. The terminal remains the long-form companion surface."""
        from joat.services.roundtable import Participant, RoundTable, build_prompt

        async def noop(prompt: str):  # pragma: no cover - never driven
            yield ""

        seat = Participant(name="codex", role="proposer", stream=noop)
        prompt = build_prompt("heat pumps", seat, [])
        assert "GIST:" in prompt
        assert "Under 120 words" in prompt
        assert "as much depth" not in prompt


class TestRoundInvariant:
    """'A round contains one turn from every seated collaborator.'

    Before the round snapshot existed this was only a default: direct()
    moved the rotation cursor, so a mid-round redirect made one seat speak
    twice and another zero times in that round.
    """

    def _table(self):
        from joat.services.roundtable import Participant, RoundTable

        async def noop(prompt):  # pragma: no cover - never driven
            yield ""

        return RoundTable([
            Participant(name=n, role="seat", stream=noop)
            for n in ("codex", "claude", "joat")
        ])

    def test_mid_round_redirect_reorders_but_everyone_speaks_once(self) -> None:
        t = self._table()
        t.start_round()
        spoke = [t.next_speaker().name]          # codex spoke
        assert t.direct("joat") is not None      # wearer: "joat, your turn"
        spoke += [t.next_speaker().name, t.next_speaker().name]
        assert spoke == ["codex", "joat", "claude"]   # reordered…
        assert sorted(spoke) == ["claude", "codex", "joat"]  # …never recounted

    def test_redirect_to_a_seat_that_already_spoke_opens_the_next_round(self) -> None:
        t = self._table()
        t.start_round()
        assert t.next_speaker().name == "codex"
        assert t.direct("codex") is not None     # a beat too late
        # codex does NOT get a second turn this round…
        rest = [t.next_speaker().name, t.next_speaker().name]
        assert "codex" not in rest
        # …but opens the next one, which is what the wearer meant.
        t.start_round()
        assert t.next_speaker().name == "codex"

    def test_between_rounds_a_redirect_picks_the_next_opener(self) -> None:
        t = self._table()
        t.start_round()
        for _ in range(3):
            t.next_speaker()
        assert t.direct("claude") is not None
        t.start_round()
        assert t.next_speaker().name == "claude"


class TestRedirectingTheSpeakingOrder:
    """"You can redirect the speaking order by voice" — the wearer's spec.

    Audited against punctuated speech, the way Whisper writes it. The gap
    found: "Claude, your turn" was recognised and "Codex, you next" was
    not. That asymmetry is what makes a voice feature feel BROKEN rather
    than limited — the wearer says a natural variant, nothing happens, and
    nothing explains why.
    """

    def _table(self):
        from joat.services.roundtable import Participant, RoundTable

        async def stream(_prompt: str):
            yield "x"

        return RoundTable([Participant("codex", "proposer", stream),
                           Participant("claude", "critic", stream)])

    def test_natural_phrasings_all_nominate(self) -> None:
        from joat.services.roundtable import parse_direction

        table = self._table()
        for said, expected in [
            ("Claude, respond.", "claude"),
            ("Claude, your turn.", "claude"),
            ("codex, you next", "codex"),
            ("Codex, you're next.", "codex"),
            ("Claude, take it.", "claude"),
            ("Codex, answer.", "codex"),
            ("Claude, weigh in.", "claude"),
            ("Back to Codex.", "codex"),
            ("Now Claude.", "claude"),
            ("Claude, thoughts?", "claude"),
            ("Let Claude go next.", "claude"),
            ("Over to Claude.", "claude"),
            ("Claude.", "claude"),
        ]:
            seat = parse_direction(said, table)
            assert seat is not None and seat.name == expected, said

    def test_talking_about_a_seat_never_reorders_the_room(self) -> None:
        """The narrowness is the point: an overheard sentence mentioning a
        collaborator must not hand them the floor."""
        from joat.services.roundtable import parse_direction

        table = self._table()
        for said in (
            "I was talking to Claude yesterday.",
            "the claude cli is slow",
            "what did codex mean by that",
            "codex and claude disagree",
            "I think claude is right about the cache",
        ):
            assert parse_direction(said, table) is None, said


class TestRoutingTheNextTurn:
    """The two instructions that make identical text a debate or a
    brainstorm — the wearer's own distinction.

    An unrecognised routing instruction does not fail loudly. It falls
    through to an ordinary cross-review turn, so the wearer sees a
    plausible answer to a question they did not ask. "Disagree with that",
    "add to that" and "extend that" were all silently unrecognised.
    """

    def test_critique_phrasings(self) -> None:
        from joat.services.roundtable import parse_relay_mode

        for said in ("Tear that apart.", "Push back on that.",
                     "Poke holes in that.", "Challenge that.",
                     "Disagree with that.", "Argue against that.",
                     "Stress test it.", "Where does that break?",
                     "What's wrong with that?", "Find the weak claim."):
            assert parse_relay_mode(said) == "critique", said

    def test_build_phrasings(self) -> None:
        from joat.services.roundtable import parse_relay_mode

        for said in ("Build on that.", "Expand on that.", "Extend that.",
                     "Add to that.", "Take that further.", "Go deeper on that.",
                     "Flesh that out.", "Keep going with that.",
                     "Run with it.", "Develop that."):
            assert parse_relay_mode(said) == "build", said

    def test_ordinary_remarks_route_nothing(self) -> None:
        """These are things said IN a discussion, not instructions about
        it. Routing them would answer a question nobody asked."""
        from joat.services.roundtable import parse_relay_mode

        for said in ("I disagree.", "that adds up",
                     "we should extend the deadline", "the build is broken",
                     "I want to build a cache"):
            assert parse_relay_mode(said) is None, said


class TestStageDirectionsAreAnchored:
    """Two defects from a blind-pair audit, one root: `parse_relay_mode`
    ran an UNANCHORED search over closed phrase lists.

    First (BSI-14): any remark merely CONTAINING a phrase mid-sentence was
    claimed as a stage direction and the wearer's actual thought discarded
    without a trace — no YOU turn, no paint, nothing for the next speaker
    to answer. Second (BSI-15): "claude, tear that apart" set the mode but
    never handed over the floor — parse_direction's phrasing list did not
    know it, so the OTHER seat spoke with the new instruction.
    """

    @staticmethod
    def _table():
        async def _s(prompt: str):
            yield "x"
        from joat.services.roundtable import Participant, RoundTable
        return RoundTable([
            Participant("codex", "proposer", _s),
            Participant("claude", "critic", _s),
        ])

    def test_a_remark_containing_a_phrase_is_not_a_direction(self) -> None:
        from joat.services.roundtable import parse_relay_mode
        for said in (
            "I think we should add to that list of requirements",
            "someone needs to push back on the vendor timeline",
            "the way you tear that apart matters less than the data",
        ):
            assert parse_relay_mode(said) is None, said

    def test_a_direction_still_opens_the_utterance(self) -> None:
        from joat.services.roundtable import parse_relay_mode
        for said, mode in (
            ("Tear that apart.", "critique"),
            ("please push back on that", "critique"),
            ("Now build on that.", "build"),
            ("okay add to that", "build"),
        ):
            assert parse_relay_mode(said) == mode, said

    def test_a_nomination_carrying_its_instruction_hands_over_the_floor(self) -> None:
        from joat.services.roundtable import parse_direction, parse_relay_mode
        table = self._table()
        for said, seat, mode in (
            ("claude, tear that apart", "claude", "critique"),
            ("Codex, build on that.", "codex", "build"),
        ):
            direction = parse_direction(said, table)
            assert direction is not None and direction.name == seat, said
            assert parse_relay_mode(said, table) == mode, said

    def test_a_bare_seat_remark_is_still_not_a_nomination(self) -> None:
        from joat.services.roundtable import parse_direction
        table = self._table()
        assert parse_direction("claude was wrong about the cache", table) is None


class TestARedirectToASpokenSeatSurvivesTheRound:
    """The audit's dead store: direct() for an already-spoken seat wrote the
    rotation cursor, and next_speaker() unconditionally rewrites that after
    every turn — so the redirect was destroyed by the very next speaker. At
    exactly two seats the +1 wrap coincidentally landed on the named seat,
    which is the only reason production never showed it.
    """

    @staticmethod
    def _three():
        async def _s(prompt: str):
            yield "x"
        return RoundTable([
            Participant("codex", "proposer", _s),
            Participant("claude", "critic", _s),
            Participant("hermes", "scribe", _s),
        ])

    def test_the_named_seat_opens_the_next_round_at_three_seats(self) -> None:
        table = self._three()
        table.start_round()
        first = table.next_speaker()          # codex speaks
        assert first.name == "codex"
        assert table.direct("codex") is not None   # "codex, go again"
        table.next_speaker()                  # claude — this used to clobber it
        table.next_speaker()                  # hermes
        table.start_round()
        assert table.next_speaker().name == "codex", (
            "the redirect was destroyed by the next turn"
        )

    def test_two_seats_still_behave_as_before(self) -> None:
        async def _s(prompt: str):
            yield "x"
        table = RoundTable([
            Participant("codex", "proposer", _s),
            Participant("claude", "critic", _s),
        ])
        table.start_round()
        table.next_speaker()                  # codex
        assert table.direct("codex") is not None
        table.next_speaker()                  # claude
        table.start_round()
        assert table.next_speaker().name == "codex"


class TestANominationCanCarryAQuestion:
    """The plan's P3 acceptance list included "ask Codex if that's true",
    and it did not work: the nomination was recognised, the seat took the
    floor, and the QUESTION was discarded — so the collaborator was
    prompted with the prior turns plus "add what the others missed" and
    answered something nobody had asked. The wearer's words for this
    feature are "take anything and do something I ask it to do with it".
    """

    @staticmethod
    def _table():
        async def _s(prompt: str):
            yield "x"
        from joat.services.roundtable import Participant, RoundTable
        return RoundTable([
            Participant("codex", "proposer", _s),
            Participant("claude", "critic", _s),
        ])

    def test_the_question_is_returned_as_the_tail(self) -> None:
        from joat.services.roundtable import direction_tail
        table = self._table()
        assert direction_tail("ask Codex if that's true", table) == "if that's true"
        assert direction_tail(
            "codex what do you think about the latency claim", table,
        ) == "about the latency claim"

    def test_a_bare_nomination_carries_nothing(self) -> None:
        from joat.services.roundtable import direction_tail
        table = self._table()
        for said in ("codex, go", "claude your turn", "over to codex", "codex"):
            assert direction_tail(said, table) == "", said

    def test_a_stage_direction_is_not_a_question(self) -> None:
        """parse_relay_mode already routes "tear that apart"; recording it
        as a YOU turn too would make the room discuss the wearer's stage
        direction."""
        from joat.services.roundtable import direction_tail
        table = self._table()
        for said in ("claude, tear that apart", "codex build on that"):
            assert direction_tail(said, table) == "", said

    def test_a_remark_that_merely_names_a_seat_carries_nothing(self) -> None:
        from joat.services.roundtable import direction_tail
        table = self._table()
        assert direction_tail("claude was wrong about the cache", table) == ""
