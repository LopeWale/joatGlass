"""Composition for deterministic voice commands before general inference."""

from __future__ import annotations

from typing import Protocol

from .greetings import greeting_response
from .models import ModelRegistry, handle_model_command


class CommandController(Protocol):
    async def handle_command(self, text: str) -> str | None: ...


class VoiceCommandRouter:
    """Claim exact commands in safe precedence order.

    Greeting-only speech is side-effect free. Message replies may cause an
    external send after confirmation, and model controls mutate runtime state,
    so they are considered only after the narrow salutation matcher declines.
    Returning ``None`` hands the complete query to the general reply engine.
    """

    def __init__(
        self,
        message_replies: CommandController,
        models: ModelRegistry,
    ) -> None:
        self._message_replies = message_replies
        self._models = models

    async def __call__(self, text: str) -> str | None:
        greeting_result = greeting_response(text)
        if greeting_result is not None:
            return greeting_result
        reply_result = await self._message_replies.handle_command(text)
        if reply_result is not None:
            return reply_result
        return await handle_model_command(text, self._models)
