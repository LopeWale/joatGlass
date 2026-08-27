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
