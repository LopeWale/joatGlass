"""Derived QuickNote insights: local-first distillation and bounded actions."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from joat.persistence.store import DurableStore
from joat.persistence.voice_notes import VoiceNoteStore
from joat.services.commitments import Commitment
from joat.services.note_insights import (
    NoteInsightCommandController,
    NoteInsightsService,
    _due_date,
    parse_insights,
)
from joat.services.productivity_actions import (
    ActionOrigin,
    ReminderBatchResult,
    ReminderIntent,
)

_SHAPED = (
    "SUMMARY:\n"
    "The team reviewed the launch plan and agreed to ship on Friday. QA owns "
    "the final pass.\n"
    "KEY POINTS:\n"
    "- Ship on Friday\n"
    "- QA signs off Thursday\n"
    "TODOS:\n"
    "- Email the release notes\n"
    "- Book the retro room\n"
)


class FakeCompleter:
    def __init__(self, replies: list[str | None]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, str]] = []

    async def complete(
        self, *, system: str, text: str, max_tokens: int = 700
    ) -> str | None:
        del max_tokens
        self.calls.append((system, text))
        if not self.replies:
            return None
        return self.replies.pop(0)


def retained_note(
    notes: VoiceNoteStore,
    capture_id: str = "capture-1",
    *,
    transcript: str = "We agreed to ship Friday. QA signs off Thursday.",
    base_us: int = 10,
) -> None:
    notes.create(
        capture_id=capture_id,
        idempotency_key=f"voice-note:{capture_id}",
        title="Launch sync",
        source_kind="g2.microphone",
        now_us=base_us,
    )
    notes.transition(capture_id, "recording", now_us=base_us + 1)
    notes.append_frame(
        capture_id,
        connection_generation="socket-a",
        seq=1,
        source="glasses",
        speaker="wearer",
        pcm=b"\x01\x00",
        received_at_us=base_us + 2,
    )
    notes.transition(capture_id, "finalizing", now_us=base_us + 3)
    notes.append_transcript(
        capture_id,
        text=transcript,
        first_frame_ordinal=0,
        last_frame_ordinal=0,
        now_us=base_us + 4,
    )
    notes.finalize(
        capture_id,
        complete=True,
        completion_code="transcript_checkpointed_raw_purged",
        now_us=base_us + 5,
    )


def service(
    path: Path,
    completer: FakeCompleter | None,
    *,
    claude: FakeCompleter | None = None,
) -> tuple[DurableStore, VoiceNoteStore, NoteInsightsService]:
    durable = DurableStore(path)
    notes = VoiceNoteStore(durable)
    insights = NoteInsightsService(
        notes, completer, claude=claude, clock_us=lambda: 999,
    )
    return durable, notes, insights


def test_parse_insights_accepts_decorated_headers_and_strips_bullets() -> None:
    parsed = parse_insights(
        "## Summary:\nShip Friday.\n**KEY POINTS**\n1. Ship it\n2) QA pass\n"
        "Todos:\n- none\n"
    )
    assert parsed is not None
    assert parsed.summary == "Ship Friday."
    assert parsed.key_points == ("Ship it", "QA pass")
    assert parsed.todos == ()


def test_parse_insights_refuses_output_without_a_summary() -> None:
    assert parse_insights("KEY POINTS:\n- something\n") is None


async def test_summarize_stores_engine_attributed_insights(tmp_path: Path) -> None:
    completer = FakeCompleter([_SHAPED])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    with durable:
        retained_note(notes)

        receipt = await insights.summarize_last()

        assert receipt == (
            "Summarized “Launch sync”: 2 key points, 2 todos. "
            "Open QuickNotes to read it."
        )
        stored = notes.insights("capture-1")
        assert stored is not None
        assert stored.engine == "mlx-local"
        assert stored.todos == ("Email the release notes", "Book the retro room")
        # The private transcript went to the local engine, never elsewhere.
        assert "ship Friday" in completer.calls[0][1]


async def test_summarize_with_claude_requires_the_optional_engine(tmp_path: Path) -> None:
    durable, notes, insights = service(tmp_path / "joat.sqlite3", FakeCompleter([]))
    with durable:
        retained_note(notes)
        assert await insights.summarize_last(engine="claude") == (
            "Claude is not available on this Mac."
        )

    claude = FakeCompleter([_SHAPED])
    durable2, notes2, insights2 = service(
        tmp_path / "joat-claude.sqlite3", FakeCompleter([]), claude=claude,
    )
    with durable2:
        retained_note(notes2)
        receipt = await insights2.summarize_last(engine="claude")
        assert receipt.startswith("Summarized “Launch sync”")
        stored = notes2.insights("capture-1")
        assert stored is not None
        assert stored.engine == "claude-cli"


async def test_summarize_refuses_transcriptless_notes_and_unusable_output(
    tmp_path: Path,
) -> None:
    completer = FakeCompleter(["no sections here at all"])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    with durable:
        retained_note(notes)
        receipt = await insights.summarize_last()
        assert receipt.startswith("The summarizer returned an unusable answer.")
        assert notes.insights("capture-1") is None


async def test_long_transcripts_fold_before_distillation(tmp_path: Path) -> None:
    # Real captures transcribe in bounded groups, so a long recording is many
    # segments; their joined text is what must exceed the direct-distill cap.
    section = ("The team discussed shipping. " * 600).strip()
    completer = FakeCompleter(["Condensed part one.", "Condensed part two.", _SHAPED])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    with durable:
        notes.create(
            capture_id="capture-1",
            idempotency_key="voice-note:capture-1",
            title="Launch sync",
            source_kind="g2.microphone",
            now_us=10,
        )
        notes.transition("capture-1", "recording", now_us=11)
        for seq in (1, 2):
            notes.append_frame(
                "capture-1",
                connection_generation="socket-a",
                seq=seq,
                source="glasses",
                speaker="wearer",
                pcm=b"\x01\x00",
                received_at_us=11 + seq,
            )
        notes.transition("capture-1", "finalizing", now_us=20)
        notes.append_transcript(
            "capture-1", text=section,
            first_frame_ordinal=0, last_frame_ordinal=0, now_us=21,
        )
        notes.append_transcript(
            "capture-1", text=section,
            first_frame_ordinal=1, last_frame_ordinal=1, now_us=22,
        )
        notes.finalize(
            "capture-1", complete=True,
            completion_code="transcript_checkpointed_raw_purged", now_us=23,
        )
        assert len(insights.transcript_text("capture-1")) > 20_000

        receipt = await insights.summarize_last()

        assert receipt.startswith("Summarized “Launch sync”")
        fold_calls = [call for call in completer.calls if "Condense" in call[0]]
        final_calls = [call for call in completer.calls if "distill" in call[0]]
        assert len(fold_calls) >= 2
        assert len(final_calls) == 1
        assert "Condensed part one." in final_calls[0][1]


async def test_ask_grounds_the_answer_in_the_note(tmp_path: Path) -> None:
    completer = FakeCompleter(["QA signs off Thursday."])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    with durable:
        retained_note(notes)

        answer = await insights.ask_last("who signs off?")

        assert answer == "QA signs off Thursday."
        system, text = completer.calls[0]
        assert "ONLY the note" in system
        assert "QUESTION: who signs off?" in text
        assert "ship Friday" in text


async def test_command_grammar_routes_summarize_ask_and_unknown(tmp_path: Path) -> None:
    completer = FakeCompleter([_SHAPED, "Friday."])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    commands = NoteInsightCommandController(insights)
    with durable:
        retained_note(notes)
        summarized = await commands.handle_command("Summarize the last quicknote")
        assert summarized is not None and summarized.startswith("Summarized")
        answered = await commands.handle_command("ask the last note when do we ship")
        assert answered == "Friday."
        assert await commands.handle_command("what time is it") is None


#: A Monday, 09:00 in Chicago. The note below was recorded then.
NOTE_US = int(datetime(2026, 8, 31, 14, 0, tzinfo=timezone.utc).timestamp() * 1_000_000)
#: Monday afternoon in Chicago: that morning's 09:00 has already gone by.
NOW = datetime(2026, 8, 31, 20, 0, tzinfo=timezone.utc)

SPOKEN = (
    "We agreed to ship Friday. I need to email the release notes to the team tomorrow. "
    "QA signs off Thursday. I'll book the retro room on Friday."
)
EMAIL_QUOTE = "I need to email the release notes to the team tomorrow."
COMMITMENT_REPLY = (
    "TASK: Email the release notes\n"
    f"QUOTE: {EMAIL_QUOTE}\n"
    "TASK: Book the retro room\n"
    "QUOTE: I'll book the retro room on Friday.\n"
    # The model's own idea: nothing in the note says it, so it is never offered.
    "TASK: Schedule a meeting with the IT department\n"
    "QUOTE: We should schedule a meeting with the IT department.\n"
)
EMAIL_KEY = Commitment(task="Email the release notes", quote=EMAIL_QUOTE).key
BOOK_KEY = Commitment(task="Book the retro room", quote="I'll book the retro room on Friday.").key


class FakeProductivity:
    """What the controller needs of the confirmed-reminder writer."""

    def __init__(
        self,
        result: ReminderBatchResult | None = None,
        *,
        created: set[str] | None = None,
    ) -> None:
        self.batches: list[tuple[list[tuple[ReminderIntent, ActionOrigin]], str]] = []
        self.created = created or set()
        self._result = result

    def is_created(self, origin: ActionOrigin) -> bool:
        return origin.identifier in self.created

    async def create_reminder_batch(
        self,
        items: Sequence[tuple[ReminderIntent, ActionOrigin]],
        *,
        label: str,
    ) -> ReminderBatchResult:
        self.batches.append((list(items), label))
        if self._result is not None:
            return self._result
        return ReminderBatchResult(created=len(items))


class FakeReview:
    """The glasses list page: shows every line, then the wearer decides."""

    def __init__(self, decision: bool | None = True) -> None:
        self.decision = decision
        self.shown: list[tuple[str, tuple[str, ...]]] = []

    async def __call__(self, title: str, lines: Sequence[str]) -> bool | None:
        self.shown.append((title, tuple(lines)))
        return self.decision


def reminder_commands(
    insights: NoteInsightsService,
    productivity: FakeProductivity | None,
    review: FakeReview | None,
    *,
    zone: str | None = "America/Chicago",
) -> NoteInsightCommandController:
    return NoteInsightCommandController(
        insights,
        productivity=productivity,  # type: ignore[arg-type]
        review_reminders=review,
        local_time_zone=lambda: zone,
        now=lambda: NOW,
    )


async def test_spoken_commitments_are_read_then_confirmed_once_with_provenance(
    tmp_path: Path,
) -> None:
    completer = FakeCompleter([COMMITMENT_REPLY])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    productivity, review = FakeProductivity(), FakeReview()
    commands = reminder_commands(insights, productivity, review)
    with durable:
        # No summary has been made: the transcript alone is the source.
        retained_note(notes, transcript=SPOKEN, base_us=NOTE_US)

        receipt = await commands.create_reminders_for("capture-1")

        assert receipt == "Added 2 reminders from “Launch sync”. Adjust times in Planning."
        # Every reminder is shown in full, with the day it will fire.
        assert review.shown == [(
            "Launch sync",
            (
                "□ Email the release notes — Sep 1 at 09:00",
                "□ Book the retro room — Sep 4 at 09:00",
            ),
        )]
        ((items, label),) = productivity.batches
        assert label == "Launch sync"
        assert [intent.title for intent, _ in items] == [
            "Email the release notes", "Book the retro room",
        ]
        # "tomorrow" is the day after the note; "Friday" the first Friday after it.
        assert [intent.date for intent, _ in items] == ["2026-09-01", "2026-09-04"]
        assert {(intent.time, intent.time_zone) for intent, _ in items} == {
            ("09:00", "America/Chicago"),
        }
        assert [(origin.kind, origin.identifier, origin.ordinal) for _, origin in items] == [
            ("voice_note.commitment", f"capture-1:{EMAIL_KEY}", 0),
            ("voice_note.commitment", f"capture-1:{BOOK_KEY}", 0),
        ]
        # The private transcript went to the local engine only, asked once.
        assert len(completer.calls) == 1
        assert "retro room" in completer.calls[0][1]


async def test_a_task_the_wearer_never_said_is_never_offered(tmp_path: Path) -> None:
    completer = FakeCompleter([COMMITMENT_REPLY])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    productivity, review = FakeProductivity(), FakeReview()
    commands = reminder_commands(insights, productivity, review)
    with durable:
        retained_note(notes, transcript=SPOKEN, base_us=NOTE_US)

        await commands.create_reminders_for("capture-1")

        assert all("IT department" not in line for line in review.shown[0][1])
        assert all(
            "IT department" not in intent.title
            for intent, _ in productivity.batches[0][0]
        )


async def test_nothing_is_written_unless_the_list_is_confirmed(tmp_path: Path) -> None:
    for decision, expected in [
        (False, "Reminders cancelled."),
        (None, "Reminders were not added: the list was not answered in time."),
    ]:
        completer = FakeCompleter([COMMITMENT_REPLY])
        durable, notes, insights = service(tmp_path / f"{decision}.sqlite3", completer)
        productivity, review = FakeProductivity(), FakeReview(decision)
        commands = reminder_commands(insights, productivity, review)
        with durable:
            retained_note(notes, transcript=SPOKEN, base_us=NOTE_US)

            receipt = await commands.create_reminders_for("capture-1")

            assert receipt == expected
            assert len(review.shown) == 1
            assert productivity.batches == []


async def test_a_declined_final_confirmation_cancels_without_a_count(tmp_path: Path) -> None:
    completer = FakeCompleter([COMMITMENT_REPLY])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    productivity = FakeProductivity(ReminderBatchResult(declined=True))
    commands = reminder_commands(insights, productivity, FakeReview())
    with durable:
        retained_note(notes, transcript=SPOKEN, base_us=NOTE_US)

        assert await commands.create_reminders_for("capture-1") == "Reminders cancelled."
        assert len(productivity.batches) == 1


async def test_a_note_where_nothing_was_promised_offers_nothing(tmp_path: Path) -> None:
    completer = FakeCompleter(["NONE"])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    productivity, review = FakeProductivity(), FakeReview()
    commands = reminder_commands(insights, productivity, review)
    with durable:
        retained_note(notes, base_us=NOTE_US)

        receipt = await commands.create_reminders_for("capture-1")

        assert receipt == (
            "“Launch sync” has no reminders: I add only what you said you will do."
        )
        assert review.shown == [] and productivity.batches == []


async def test_a_silent_model_is_reported_and_asked_again_next_time(tmp_path: Path) -> None:
    completer = FakeCompleter([None, COMMITMENT_REPLY])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    productivity, review = FakeProductivity(), FakeReview()
    commands = reminder_commands(insights, productivity, review)
    with durable:
        retained_note(notes, transcript=SPOKEN, base_us=NOTE_US)

        first = await commands.create_reminders_for("capture-1")
        second = await commands.create_reminders_for("capture-1")

        assert first == "The local model did not answer. Try again in a moment."
        assert review.shown and second.startswith("Added 2 reminders")
        assert len(completer.calls) == 2


async def test_the_model_reads_a_note_once_however_often_it_is_opened(tmp_path: Path) -> None:
    completer = FakeCompleter([COMMITMENT_REPLY])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    review = FakeReview(False)
    commands = reminder_commands(insights, FakeProductivity(), review)
    with durable:
        retained_note(notes, transcript=SPOKEN, base_us=NOTE_US)

        for _ in range(3):
            assert await commands.create_reminders_for("capture-1") == "Reminders cancelled."

        assert len(completer.calls) == 1
        assert len(review.shown) == 3
        assert review.shown[0] == review.shown[2]


async def test_reminders_already_made_are_not_offered_again(tmp_path: Path) -> None:
    completer = FakeCompleter([COMMITMENT_REPLY])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    productivity = FakeProductivity(created={f"capture-1:{EMAIL_KEY}"})
    review = FakeReview()
    commands = reminder_commands(insights, productivity, review)
    with durable:
        retained_note(notes, transcript=SPOKEN, base_us=NOTE_US)

        receipt = await commands.create_reminders_for("capture-1")

        assert receipt == (
            "Added 1 reminder from “Launch sync”. 1 already in Reminders. "
            "Adjust times in Planning."
        )
        assert review.shown[0][1] == ("□ Book the retro room — Sep 4 at 09:00",)
        assert [intent.title for intent, _ in productivity.batches[0][0]] == ["Book the retro room"]


@pytest.mark.parametrize(
    ("created", "expected"),
    [
        ({EMAIL_KEY, BOOK_KEY}, "The reminders from “Launch sync” are already in Reminders."),
        ({BOOK_KEY}, None),
    ],
)
async def test_a_second_tap_after_everything_was_made_asks_nothing(
    tmp_path: Path, created: set[str], expected: str | None,
) -> None:
    completer = FakeCompleter([COMMITMENT_REPLY])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    productivity = FakeProductivity(created={f"capture-1:{key}" for key in created})
    review = FakeReview()
    commands = reminder_commands(insights, productivity, review)
    with durable:
        retained_note(notes, transcript=SPOKEN, base_us=NOTE_US)

        receipt = await commands.create_reminders_for("capture-1")

        if expected is not None:
            assert receipt == expected
            assert review.shown == [] and productivity.batches == []
        else:
            assert len(review.shown) == 1


async def test_one_reminder_already_made_reads_in_the_singular(tmp_path: Path) -> None:
    completer = FakeCompleter(["TASK: Email the release notes\nQUOTE: " + EMAIL_QUOTE])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    commands = reminder_commands(
        insights, FakeProductivity(created={f"capture-1:{EMAIL_KEY}"}), FakeReview(),
    )
    with durable:
        retained_note(notes, transcript=SPOKEN, base_us=NOTE_US)

        assert await commands.create_reminders_for("capture-1") == (
            "The reminder from “Launch sync” is already in Reminders."
        )


async def test_a_reminder_that_could_not_be_written_says_why_and_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="joat.services.note_insights")
    completer = FakeCompleter([COMMITMENT_REPLY])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    problem = "Reminders did not create the reminder. Check JOAT on the Mac."
    commands = reminder_commands(
        insights,
        FakeProductivity(ReminderBatchResult(created=1, problems=(problem,))),
        FakeReview(),
    )
    with durable:
        retained_note(notes, transcript=SPOKEN, base_us=NOTE_US)

        receipt = await commands.create_reminders_for("capture-1")

        assert receipt == f"Added 1 of 2 reminders from “Launch sync”. {problem}"
        assert f"not created: {problem}" in caplog.text


async def test_a_reminder_the_journal_found_made_meanwhile_is_counted_apart(
    tmp_path: Path,
) -> None:
    completer = FakeCompleter([COMMITMENT_REPLY])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    commands = reminder_commands(
        insights, FakeProductivity(ReminderBatchResult(created=1, already=1)), FakeReview(),
    )
    with durable:
        retained_note(notes, transcript=SPOKEN, base_us=NOTE_US)

        assert await commands.create_reminders_for("capture-1") == (
            "Added 1 of 2 reminders from “Launch sync”. 1 already in Reminders."
            " Adjust times in Planning."
        )


async def test_a_refusal_is_the_receipt(tmp_path: Path) -> None:
    completer = FakeCompleter([COMMITMENT_REPLY])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    refusal = "Open JOAT on the Mac and connect Reminders first."
    commands = reminder_commands(
        insights, FakeProductivity(ReminderBatchResult(refused=refusal)), FakeReview(),
    )
    with durable:
        retained_note(notes, transcript=SPOKEN, base_us=NOTE_US)

        assert await commands.create_reminders_for("capture-1") == refusal


async def test_reminders_say_what_is_missing_before_asking_anything(tmp_path: Path) -> None:
    completer = FakeCompleter([COMMITMENT_REPLY])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    with durable:
        retained_note(notes, transcript=SPOKEN, base_us=NOTE_US)

        no_productivity = reminder_commands(insights, None, FakeReview())
        no_review = reminder_commands(insights, FakeProductivity(), None)
        no_zone = reminder_commands(insights, FakeProductivity(), FakeReview(), zone=None)
        bad_zone = reminder_commands(insights, FakeProductivity(), FakeReview(), zone="Mars/Olympus")
        missing = reminder_commands(insights, FakeProductivity(), FakeReview())

        assert await no_productivity.create_reminders_for("capture-1") == (
            "Reminders are unavailable on this Mac."
        )
        assert await no_review.create_reminders_for("capture-1") == (
            "Reminders cannot be shown on the glasses right now."
        )
        for commands in (no_zone, bad_zone):
            assert await commands.create_reminders_for("capture-1") == (
                "The Mac's time zone is unavailable, so reminders cannot be scheduled."
            )
        assert await missing.create_reminders_for("nope") == "That note no longer exists."
        # None of that spent a model pass.
        assert completer.calls == []


async def test_the_spoken_command_runs_the_same_flow_as_the_tap(tmp_path: Path) -> None:
    completer = FakeCompleter([COMMITMENT_REPLY])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    productivity, review = FakeProductivity(), FakeReview()
    commands = reminder_commands(insights, productivity, review)
    with durable:
        retained_note(notes, "older", transcript="Nothing to do here.", base_us=NOTE_US)
        retained_note(notes, "newer", transcript=SPOKEN, base_us=NOTE_US + 1_000_000)

        receipt = await commands.handle_command("Create reminders from the last note.")

        # "The last note": the list is read first, then one confirmation.
        assert receipt == "Added 2 reminders from “Launch sync”. Adjust times in Planning."
        assert len(review.shown) == 1 and len(productivity.batches) == 1
        assert {origin.identifier.split(":")[0] for _, origin in productivity.batches[0][0]} == {
            "newer",
        }


async def test_the_spoken_command_adds_nothing_unless_the_list_is_confirmed(
    tmp_path: Path,
) -> None:
    completer = FakeCompleter([COMMITMENT_REPLY])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    productivity, review = FakeProductivity(), FakeReview(None)
    commands = reminder_commands(insights, productivity, review)
    with durable:
        retained_note(notes, transcript=SPOKEN, base_us=NOTE_US)

        receipt = await commands.handle_command("create reminders from the last note")

        assert receipt == "Reminders were not added: the list was not answered in time."
        assert productivity.batches == []


async def test_the_command_needs_a_retained_note(tmp_path: Path) -> None:
    durable, notes, insights = service(tmp_path / "joat.sqlite3", FakeCompleter([]))
    commands = reminder_commands(insights, FakeProductivity(), FakeReview())
    with durable:
        assert await commands.handle_command("create reminders from the last note") == (
            "There is no retained note to create reminders from."
        )


async def test_reminders_can_come_from_any_note_not_only_the_last(tmp_path: Path) -> None:
    completer = FakeCompleter([COMMITMENT_REPLY])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    productivity = FakeProductivity()
    commands = reminder_commands(insights, productivity, FakeReview())
    with durable:
        retained_note(notes, "older", transcript=SPOKEN, base_us=NOTE_US)
        retained_note(notes, "newer", transcript="Nothing to do here.", base_us=NOTE_US + 1_000_000)

        receipt = await commands.create_reminders_for("older")

        assert receipt.startswith("Added 2 reminders from “Launch sync”")
        assert {origin.identifier.split(":")[0] for _, origin in productivity.batches[0][0]} == {
            "older",
        }


async def test_a_day_that_has_already_passed_moves_to_the_next_nine(tmp_path: Path) -> None:
    # Recorded two weeks ago: "tomorrow" was long ago, so the first 09:00 ahead is used.
    completer = FakeCompleter([COMMITMENT_REPLY])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    review = FakeReview()
    commands = reminder_commands(insights, FakeProductivity(), review)
    with durable:
        retained_note(
            notes, transcript=SPOKEN, base_us=NOTE_US - 14 * 86_400 * 1_000_000,
        )

        await commands.create_reminders_for("capture-1")

        assert review.shown[0][1] == (
            "□ Email the release notes — Sep 1 at 09:00",
            "□ Book the retro room — Sep 1 at 09:00",
        )


async def test_commitments_are_unavailable_without_the_local_model(tmp_path: Path) -> None:
    durable, notes, insights = service(tmp_path / "joat.sqlite3", None)
    with durable:
        retained_note(notes, transcript=SPOKEN, base_us=NOTE_US)

        assert await insights.commitments("capture-1") is None
        assert await insights.commitments("nope") is None


class TestDueDate:
    MONDAY = date(2026, 8, 31)
    #: Chicago is five hours behind UTC in August.
    AFTERNOON = datetime(2026, 8, 31, 15, 0, tzinfo=ZoneInfo("America/Chicago"))
    EARLY = datetime(2026, 8, 31, 7, 0, tzinfo=ZoneInfo("America/Chicago"))

    @pytest.mark.parametrize(
        ("day", "now", "expected"),
        [
            # Nothing named: the note's own day, unless its 09:00 is behind us.
            (None, "EARLY", date(2026, 8, 31)),
            (None, "AFTERNOON", date(2026, 9, 1)),
            ("today", "EARLY", date(2026, 8, 31)),
            ("today", "AFTERNOON", date(2026, 9, 1)),
            ("tomorrow", "AFTERNOON", date(2026, 9, 1)),
            # A weekday is the first one AFTER the note's day, never the same day.
            ("tuesday", "AFTERNOON", date(2026, 9, 1)),
            ("friday", "AFTERNOON", date(2026, 9, 4)),
            ("monday", "AFTERNOON", date(2026, 9, 7)),
            ("sunday", "AFTERNOON", date(2026, 9, 6)),
        ],
    )
    def test_the_named_day_wins_and_the_past_cannot_alert(
        self, day: str | None, now: str, expected: date,
    ) -> None:
        assert _due_date(day, self.MONDAY, getattr(self, now)) == expected

    def test_an_old_note_moves_to_the_next_nine_still_ahead(self) -> None:
        old = date(2026, 8, 10)

        assert _due_date("friday", old, self.AFTERNOON) == date(2026, 9, 1)
        assert _due_date("friday", old, self.EARLY) == date(2026, 8, 31)


class FakeExporter:
    def __init__(self) -> None:
        self.exports: list[tuple[str, tuple[str, ...]]] = []

    async def export(self, *, title: str, body_lines: tuple[str, ...]) -> str:
        self.exports.append((title, body_lines))
        return f"Sent “{title}” to Apple Notes (JOAT folder)."


async def test_send_to_notes_exports_summary_and_transcript(tmp_path: Path) -> None:
    completer = FakeCompleter([_SHAPED])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    exporter = FakeExporter()
    commands = NoteInsightCommandController(insights, exporter=exporter)
    with durable:
        retained_note(notes)
        await insights.summarize_last()

        receipt = await commands.handle_command("send the last note to my notes")

        assert receipt is not None and receipt.startswith("Sent “Launch sync”")
        title, body = exporter.exports[0]
        assert title == "Launch sync"
        assert "Overview" in body
        assert "Next steps" in body
        assert any("ship on Friday" in line for line in body)
        assert "Transcript" in body
        assert any("QA signs off Thursday" in line for line in body)


async def test_schedule_summarize_runs_in_the_background(tmp_path: Path) -> None:
    completer = FakeCompleter([_SHAPED])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    with durable:
        retained_note(notes)
        insights.schedule_summarize("capture-1")
        for _ in range(20):
            await asyncio.sleep(0)
            if notes.insights("capture-1") is not None:
                break
        stored = notes.insights("capture-1")
        assert stored is not None
        assert stored.engine == "mlx-local"


# ── FEAT-002: title, Conversate-style sections, follow-on steps ─────────────

_CONVERSATE_SHAPED = (
    "TITLE: \"Launch plan review.\"\n"
    "**OVERVIEW:**\n"
    "The team agreed to ship on Friday.\n"
    "KEY POINTS:\n"
    "- Ship on Friday\n"
    "NEXT STEPS:\n"
    "- Email the release notes\n"
)


def test_parse_insights_reads_title_overview_and_next_steps() -> None:
    parsed = parse_insights(_CONVERSATE_SHAPED)
    assert parsed is not None
    assert parsed.title == "Launch plan review"
    assert parsed.summary == "The team agreed to ship on Friday."
    assert parsed.key_points == ("Ship on Friday",)
    assert parsed.todos == ("Email the release notes",)


def test_a_key_point_that_merely_starts_with_a_header_word_stays_a_point() -> None:
    parsed = parse_insights(
        "OVERVIEW:\nShip it.\nKEY POINTS:\nSummary of costs was approved\n"
    )
    assert parsed is not None
    assert parsed.key_points == ("Summary of costs was approved",)


async def test_a_clock_title_is_replaced_by_the_generated_one(tmp_path: Path) -> None:
    completer = FakeCompleter([_CONVERSATE_SHAPED])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    with durable:
        retained_note(notes)
        notes.rename("capture-1", "Note 09:30", now_us=50)

        receipt = await insights.summarize("capture-1")

        assert receipt.startswith("Summarized “Launch plan review”")
        assert notes.get("capture-1").title == "Launch plan review"


async def test_a_spoken_title_is_never_replaced(tmp_path: Path) -> None:
    completer = FakeCompleter([_CONVERSATE_SHAPED])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    with durable:
        retained_note(notes)

        await insights.summarize("capture-1")

        assert notes.get("capture-1").title == "Launch sync"


async def test_the_post_summary_step_runs_even_when_the_summary_fails(
    tmp_path: Path,
) -> None:
    followed: list[str] = []

    async def after(capture_id: str) -> None:
        followed.append(capture_id)

    durable = DurableStore(tmp_path / "joat.sqlite3")
    notes = VoiceNoteStore(durable)
    insights = NoteInsightsService(
        notes, FakeCompleter(["unusable"]), clock_us=lambda: 999, after_summary=after,
    )
    with durable:
        retained_note(notes)
        insights.schedule_summarize("capture-1")
        for _ in range(50):
            await asyncio.sleep(0)
            if followed:
                break

        assert followed == ["capture-1"]
        assert notes.insights("capture-1") is None


class FakeSync:
    def __init__(self) -> None:
        self.synced: list[tuple[str, bool]] = []
        self.toggles: list[str] = []

    async def sync_voice(self, capture_id: str, *, force: bool = False) -> str | None:
        self.synced.append((capture_id, force))
        return "Sent “Launch sync” to Apple Notes (JOAT folder)."

    def handle_toggle(self, text: str) -> str | None:
        self.toggles.append(text)
        return "toggled" if "apple notes" in text.casefold() else None


async def test_send_to_notes_and_resummarize_go_through_the_sync(tmp_path: Path) -> None:
    completer = FakeCompleter([_SHAPED])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    sync = FakeSync()
    commands = NoteInsightCommandController(insights, sync=sync)
    with durable:
        retained_note(notes)

        assert (await commands.handle_command("send the last note to my notes") or "").startswith(
            "Sent “Launch sync”"
        )
        await commands.handle_command("summarize the last note")
        assert sync.synced == [("capture-1", True), ("capture-1", False)]
        assert await commands.handle_command("stop saving notes to Apple Notes") == "toggled"
        assert await commands.handle_command("what time is it") is None
