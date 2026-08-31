"""Derived QuickNote insights: local-first distillation and bounded actions."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from joat.persistence.store import DurableStore
from joat.persistence.voice_notes import VoiceNoteStore
from joat.services.note_insights import (
    NoteInsightCommandController,
    NoteInsightsService,
    parse_insights,
)
from joat.services.productivity_actions import ActionOrigin, ReminderIntent

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


class FakeProductivity:
    def __init__(self) -> None:
        self.intents: list[tuple[ReminderIntent, ActionOrigin | None]] = []

    async def create_reminder(
        self,
        intent: ReminderIntent,
        *,
        origin: ActionOrigin | None = None,
    ) -> str:
        self.intents.append((intent, origin))
        return f"Created Reminder: {intent.title}."


async def test_todos_become_confirmed_reminders_with_provenance(tmp_path: Path) -> None:
    completer = FakeCompleter([_SHAPED])
    durable, notes, insights = service(tmp_path / "joat.sqlite3", completer)
    productivity = FakeProductivity()
    commands = NoteInsightCommandController(
        insights,
        productivity=productivity,  # type: ignore[arg-type]
        local_time_zone=lambda: "America/Chicago",
        now=lambda: datetime(2026, 8, 31, 20, 0, tzinfo=timezone.utc),
    )
    with durable:
        retained_note(notes)
        await insights.summarize_last()

        receipt = await commands.handle_command("create reminders from the last note")

        assert receipt == (
            "Created 2 of 2 reminders from “Launch sync”, due tomorrow 09:00. "
            "Adjust times in Planning."
        )
        assert [intent.title for intent, _ in productivity.intents] == [
            "Email the release notes", "Book the retro room",
        ]
        first_intent, first_origin = productivity.intents[0]
        assert first_intent.date == "2026-09-01"
        assert first_intent.time == "09:00"
        assert first_intent.time_zone == "America/Chicago"
        assert first_origin is not None
        assert first_origin.kind == "voice_note.todo"
        assert first_origin.identifier == "capture-1"
        assert [origin.ordinal for _, origin in productivity.intents if origin] == [0, 1]


async def test_reminders_require_a_summary_first(tmp_path: Path) -> None:
    durable, notes, insights = service(tmp_path / "joat.sqlite3", FakeCompleter([]))
    commands = NoteInsightCommandController(
        insights,
        productivity=FakeProductivity(),  # type: ignore[arg-type]
        local_time_zone=lambda: "America/Chicago",
    )
    with durable:
        retained_note(notes)
        receipt = await commands.handle_command("create reminders from the last note")
        assert receipt == (
            "“Launch sync” has no summary yet. Say: summarize the last note, first."
        )


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
        assert "Summary" in body
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
