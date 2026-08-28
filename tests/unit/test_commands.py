"""Production voice-command composition and precedence."""

from joat.services.commands import VoiceCommandRouter
from joat.services.models import ModelRegistry


class RecordingReplies:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def handle_command(self, text: str) -> str | None:
        self.seen.append(text)
        return None


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

