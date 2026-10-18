"""A reminder is only ever something the wearer said they would do."""

from __future__ import annotations

import pytest

from joat.services import commitments as module
from joat.services.commitments import (
    COMMITMENT_SYSTEM,
    MAX_COMMITMENTS,
    Commitment,
    extract_commitments,
    keep_backed_commitments,
    parse_blocks,
    spoken_day,
)

PLUMBER = "I need to call the plumber tomorrow morning about the leak."
TRANSCRIPT = (
    "So today was long and the kitchen is still a mess. "
    f"{PLUMBER} "
    "The invoice from the tile shop arrived on Monday. "
    "I'll send Dele the signed contract on Friday. "
    "Anyway, that is where things stand."
)


class ScriptedCompleter:
    """Answers each pass from a function of the text it was handed."""

    def __init__(self, reply) -> None:  # type: ignore[no-untyped-def]
        self._reply = reply
        self.texts: list[str] = []
        self.systems: list[str] = []

    async def complete(self, *, system: str, text: str, max_tokens: int = 700) -> str | None:
        self.systems.append(system)
        self.texts.append(text)
        return self._reply(text)  # type: ignore[no-any-return]


class TestParseBlocks:
    def test_reads_plain_task_and_quote_blocks_in_order(self) -> None:
        raw = (
            "TASK: Call the plumber about the leak\n"
            f"QUOTE: {PLUMBER}\n"
            "\n"
            "TASK: Send Dele the contract\n"
            "QUOTE: I'll send Dele the signed contract on Friday.\n"
        )

        assert parse_blocks(raw) == [
            ("Call the plumber about the leak", PLUMBER),
            ("Send Dele the contract", "I'll send Dele the signed contract on Friday."),
        ]

    def test_tolerates_numbering_bold_and_curly_quotes(self) -> None:
        raw = (
            "1. **TASK:** Call the plumber\n"
            "   **QUOTE:** “I need to call the plumber.”\n"
            "- task: Email the landlord\n"
            "- quote: \"I will email the landlord.\"\n"
        )

        assert parse_blocks(raw) == [
            ("Call the plumber", "I need to call the plumber."),
            ("Email the landlord", "I will email the landlord."),
        ]

    def test_joins_a_quote_the_model_wrapped_onto_a_second_line(self) -> None:
        raw = "TASK: Call the plumber\nQUOTE: I need to call\nthe plumber tomorrow.\n"

        assert parse_blocks(raw) == [("Call the plumber", "I need to call the plumber tomorrow.")]

    def test_drops_a_block_that_has_no_quote_or_no_task(self) -> None:
        raw = "TASK: Call the plumber\nTASK: Email the landlord\nQUOTE: I will email the landlord.\n"

        assert parse_blocks(raw) == [("Email the landlord", "I will email the landlord.")]
        assert parse_blocks("QUOTE: I will email the landlord.") == []

    def test_prose_and_none_yield_no_blocks(self) -> None:
        assert parse_blocks("NONE") == []
        assert parse_blocks("You said you would call the plumber.") == []
        assert parse_blocks("") == []


class TestSpokenDay:
    @pytest.mark.parametrize(
        ("quote", "day"),
        [
            ("I need to call the plumber tomorrow morning.", "tomorrow"),
            ("I'll send it on Friday.", "friday"),
            ("I have to pay the bill today.", "today"),
            ("I will finish this tonight.", "today"),
            ("I need to call the plumber.", None),
            ("I'll do it on Mondays and Fridays.", None),
        ],
    )
    def test_names_the_first_day_word_the_wearer_said(self, quote: str, day: str | None) -> None:
        assert spoken_day(quote) == day


class TestKeepBackedCommitments:
    def test_keeps_a_task_the_wearer_said_with_the_sentence_it_came_from(self) -> None:
        kept = keep_backed_commitments(
            [("Call the plumber about the leak", f"Earlier I was tired. {PLUMBER}")],
            TRANSCRIPT,
        )

        assert kept == (
            Commitment(
                task="Call the plumber about the leak", quote=PLUMBER, day="tomorrow",
            ),
        )

    def test_a_quote_the_transcript_does_not_contain_is_dropped(self) -> None:
        kept = keep_backed_commitments(
            [("Schedule a meeting with IT", "I need to schedule a meeting with the IT department.")],
            TRANSCRIPT,
        )

        assert kept == ()

    def test_a_sentence_with_no_commitment_cue_is_dropped(self) -> None:
        kept = keep_backed_commitments(
            [("Pay the tile shop invoice", "The invoice from the tile shop arrived on Monday.")],
            TRANSCRIPT,
        )

        assert kept == ()

    def test_a_task_the_quote_does_not_say_is_dropped(self) -> None:
        kept = keep_backed_commitments(
            [("Book a flight to Lagos", PLUMBER)],
            TRANSCRIPT,
        )

        assert kept == ()

    def test_a_quote_too_short_to_be_a_sentence_is_dropped(self) -> None:
        kept = keep_backed_commitments(
            [("Remind me", "Remind me.")], "Okay. Remind me. Thanks.",
        )

        assert kept == ()

    def test_two_proposals_from_one_sentence_are_one_task(self) -> None:
        kept = keep_backed_commitments(
            [
                ("Call the plumber about the leak", PLUMBER),
                ("Ask the plumber about the leak", PLUMBER),
            ],
            TRANSCRIPT,
        )

        assert [item.task for item in kept] == ["Call the plumber about the leak"]

    def test_the_same_task_said_in_two_sentences_is_one_task(self) -> None:
        transcript = "I need to call the plumber. Later on I will call the plumber now."
        kept = keep_backed_commitments(
            [
                ("Call the plumber", "I need to call the plumber."),
                ("Call the plumber now", "I will call the plumber now."),
            ],
            transcript,
        )

        assert [item.task for item in kept] == ["Call the plumber"]

    def test_only_the_sentence_with_the_cue_is_kept_from_a_longer_quote(self) -> None:
        kept = keep_backed_commitments(
            [(
                "Send Dele the signed contract",
                "The tile shop wrote. I'll send Dele the signed contract on Friday.",
            )],
            TRANSCRIPT,
        )

        assert [item.quote for item in kept] == ["I'll send Dele the signed contract on Friday."]
        assert kept[0].day == "friday"

    def test_case_punctuation_and_curly_apostrophes_do_not_hide_a_match(self) -> None:
        transcript = "okay so I’ll send DELE the signed contract on friday!"
        kept = keep_backed_commitments(
            [("Send Dele the signed contract", "I'll send Dele the signed contract on Friday.")],
            transcript,
        )

        assert [item.task for item in kept] == ["Send Dele the signed contract"]

    def test_titles_are_cleaned_into_short_instructions(self) -> None:
        kept = keep_backed_commitments(
            [("  to call the plumber about the leak.  ", PLUMBER)], TRANSCRIPT,
        )

        assert kept[0].task == "Call the plumber about the leak"

    def test_a_very_long_task_is_cut_at_a_word(self) -> None:
        long_task = "call the plumber about the leak " * 12
        kept = keep_backed_commitments([(long_task, PLUMBER)], TRANSCRIPT)

        assert len(kept) == 1
        assert len(kept[0].task) <= 160 and not kept[0].task.endswith(" ")

    def test_at_most_eight_tasks_are_kept(self) -> None:
        nouns = ["apples", "bread", "candles", "drums", "eggs", "flour", "grapes",
                 "honey", "icing", "jam"]
        transcript = " ".join(f"I need to buy {noun}." for noun in nouns)
        proposed = [(f"Buy {noun}", f"I need to buy {noun}.") for noun in nouns]

        kept = keep_backed_commitments(proposed, transcript)

        assert MAX_COMMITMENTS == 8
        assert [item.task for item in kept] == [f"Buy {noun}" for noun in nouns[:8]]

    def test_the_key_names_the_sentence_not_its_spelling(self) -> None:
        a = Commitment(task="Call", quote="I need to call the plumber.")
        same = Commitment(task="Phone", quote="i need to  call the plumber")
        other = Commitment(task="Call", quote="I need to call the electrician.")

        assert a.key == same.key
        assert a.key != other.key
        assert len(a.key) == 20


class TestExtractCommitments:
    @staticmethod
    def good_reply(_: str) -> str:
        return (
            "TASK: Call the plumber about the leak\n"
            f"QUOTE: {PLUMBER}\n"
            "TASK: Send Dele the signed contract\n"
            "QUOTE: I'll send Dele the signed contract on Friday.\n"
            "TASK: Book a flight to Lagos\n"
            "QUOTE: I will book a flight to Lagos.\n"
        )

    async def test_keeps_only_what_the_transcript_backs_up(self) -> None:
        completer = ScriptedCompleter(self.good_reply)

        found = await extract_commitments(completer, TRANSCRIPT)

        assert found is not None
        assert [item.task for item in found] == [
            "Call the plumber about the leak",
            "Send Dele the signed contract",
        ]
        assert [item.day for item in found] == ["tomorrow", "friday"]
        assert completer.systems == [COMMITMENT_SYSTEM]
        assert completer.texts == [TRANSCRIPT]

    async def test_a_model_that_names_no_task_gives_an_empty_answer_not_a_failure(self) -> None:
        assert await extract_commitments(ScriptedCompleter(lambda _: "NONE"), TRANSCRIPT) == ()
        assert await extract_commitments(ScriptedCompleter(lambda _: "None."), TRANSCRIPT) == ()

    @pytest.mark.parametrize("silence", [None, "", "   \n"])
    async def test_a_model_that_never_answered_is_none_so_a_retry_can_work(
        self, silence: str | None
    ) -> None:
        assert await extract_commitments(ScriptedCompleter(lambda _: silence), TRANSCRIPT) is None

    async def test_a_long_transcript_is_read_in_pieces_and_merged(self) -> None:
        filler = "The kitchen work goes on and on. " * 700
        transcript = f"I need to call the plumber. {filler}{filler}I will email the landlord."

        def reply(text: str) -> str:
            blocks = []
            if "plumber" in text:
                blocks.append("TASK: Call the plumber\nQUOTE: I need to call the plumber.")
            if "landlord" in text:
                blocks.append("TASK: Email the landlord\nQUOTE: I will email the landlord.")
            return "\n".join(blocks) or "NONE"

        completer = ScriptedCompleter(reply)
        found = await extract_commitments(completer, transcript)

        assert len(completer.texts) > 1
        assert all(len(text) <= 20_000 for text in completer.texts)
        assert found is not None
        assert [item.task for item in found] == ["Call the plumber", "Email the landlord"]

    async def test_one_silent_piece_does_not_lose_what_another_piece_answered(self) -> None:
        filler = "The kitchen work goes on and on. " * 700
        transcript = f"I need to call the plumber. {filler}{filler}"

        def reply(text: str) -> str | None:
            if "plumber" in text:
                return "TASK: Call the plumber\nQUOTE: I need to call the plumber."
            return None

        found = await extract_commitments(ScriptedCompleter(reply), transcript)

        assert found is not None and [item.task for item in found] == ["Call the plumber"]

    def test_a_huge_transcript_is_read_in_at_most_eight_pieces(self) -> None:
        pieces = module._chunks("The kitchen work goes on and on. " * 12_000)

        assert len(pieces) == 8
        assert all(len(piece) <= 20_000 for piece in pieces)
