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
