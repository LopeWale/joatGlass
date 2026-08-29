"""Production voice-command composition and precedence."""

from joat.services.commands import VoiceCommandRouter
from joat.services.models import ModelRegistry


class RecordingReplies:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def handle_command(self, text: str) -> str | None:
        self.seen.append(text)
        return None


class RecordingProductivity:
    def __init__(self, result: str | None = None) -> None:
        self.result = result
        self.seen: list[str] = []

    async def handle_command(self, text: str) -> str | None:
        self.seen.append(text)
        return self.result


class TestVoiceCommandRouter:
    async def test_greeting_is_claimed_before_side_effect_commands(self) -> None:
        replies = RecordingReplies()
        commands = VoiceCommandRouter(replies, ModelRegistry([]))

        assert await commands("Good morning.") == "Good morning. How can I help?"
        assert replies.seen == []

    async def test_substantive_greeting_falls_through(self) -> None:
        replies = RecordingReplies()
        commands = VoiceCommandRouter(replies, ModelRegistry([]))

        assert await commands("Good morning, what is on my calendar?") is None
        assert replies.seen == ["Good morning, what is on my calendar?"]

    async def test_productivity_claims_exact_intent_before_model_controls(self) -> None:
        replies = RecordingReplies()
        productivity = RecordingProductivity("Calendar event cancelled.")
        commands = VoiceCommandRouter(
            replies,
            ModelRegistry([]),
            productivity=productivity,
        )

        text = "schedule Review on 2026-09-01 at 09:30 for 30 minutes in America/Chicago"
        assert await commands(text) == "Calendar event cancelled."
        assert replies.seen == [text]
        assert productivity.seen == [text]

    async def test_quick_note_claims_explicit_capture_before_productivity(self) -> None:
        replies = RecordingReplies()
        notes = RecordingProductivity("Saved QuickNote: exact")
        productivity = RecordingProductivity()
        commands = VoiceCommandRouter(
            replies,
            ModelRegistry([]),
            productivity=productivity,
            notes=notes,
        )

        assert await commands("take a note exact") == "Saved QuickNote: exact"
        assert notes.seen == ["take a note exact"]
        assert productivity.seen == []

    async def test_voice_note_control_is_claimed_before_quick_note(self) -> None:
        replies = RecordingReplies()
        voice_notes = RecordingProductivity("Recording Voice Note: review")
        notes = RecordingProductivity("wrong controller")
        commands = VoiceCommandRouter(
            replies,
            ModelRegistry([]),
            notes=notes,
            voice_notes=voice_notes,
        )

        assert await commands("start a voice note review") == (
            "Recording Voice Note: review"
        )
        assert voice_notes.seen == ["start a voice note review"]
        assert notes.seen == []

    async def test_hermes_calendar_proposal_is_claimed_before_general_inference(self) -> None:
        replies = RecordingReplies()
        productivity = RecordingProductivity()
        hermes = RecordingProductivity("Calendar event cancelled.")
        commands = VoiceCommandRouter(
            replies,
            ModelRegistry([]),
            productivity=productivity,
            hermes=hermes,
        )

        text = "ask Hermes to draft a calendar event for review tomorrow"
        assert await commands(text) == "Calendar event cancelled."
        assert hermes.seen == [text]
