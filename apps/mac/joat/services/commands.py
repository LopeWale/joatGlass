"""Composition for deterministic voice commands before general inference."""

from __future__ import annotations

from typing import Protocol

from .greetings import greeting_response
from .models import ModelRegistry, handle_model_command


class CommandController(Protocol):
    async def handle_command(self, text: str) -> str | None: ...


class ComposeCommandController(CommandController, Protocol):
    """The one controller allowed to claim a phrase before greetings.

    Productivity, notes, and Hermes are ordinary command controllers. Keeping
    this short-lived compose state on the Messages controller avoids widening
    their interface merely because a greeting-shaped message body is valid.
    """

    def is_compose_armed(self) -> bool: ...


class CapabilityOpenCommandController(CommandController, Protocol):
    """Opens capability surfaces by voice and reflects capture state."""

    async def reflect_quicknotes(self) -> None: ...


class VoiceCommandRouter:
    """Claim exact commands in safe precedence order.

    Greeting-only speech is side-effect free. Message replies may cause an
    external send after confirmation, and model controls mutate runtime state,
    so they are considered only after the narrow salutation matcher declines.
    Returning ``None`` hands the complete query to the general reply engine.
    """

    def __init__(
        self,
        message_replies: ComposeCommandController,
        models: ModelRegistry,
        productivity: CommandController | None = None,
        notes: CommandController | None = None,
        voice_notes: CommandController | None = None,
        note_insights: CommandController | None = None,
        hermes: CommandController | None = None,
        terminal: CommandController | None = None,
        capability_opens: CapabilityOpenCommandController | None = None,
    ) -> None:
        self._message_replies = message_replies
        self._models = models
        self._productivity = productivity
        self._notes = notes
        self._voice_notes = voice_notes
        self._note_insights = note_insights
        self._hermes = hermes
        self._terminal = terminal
        self._capability_opens = capability_opens

    async def __call__(self, text: str) -> str | None:
        # New Message is an explicit, short-lived wearer flow. It gets first
        # refusal while armed so a body such as “Good morning” is not mistaken
        # for a generic greeting and sent to inference instead.
        if self._message_replies.is_compose_armed():
            compose_result = await self._message_replies.handle_command(text)
            if compose_result is not None:
                return compose_result
        greeting_result = greeting_response(text)
        if greeting_result is not None:
            return greeting_result
        reply_result = await self._message_replies.handle_command(text)
        if reply_result is not None:
            return reply_result
        # "Show my messages" / "open quicknotes" — deterministic surface
        # opens must never fall through to the general reply engine.
        if self._capability_opens is not None:
            open_result = await self._capability_opens.handle_command(text)
            if open_result is not None:
                return open_result
        if self._voice_notes is not None:
            voice_note_result = await self._voice_notes.handle_command(text)
            if voice_note_result is not None:
                # Every capture command earns a visible receipt: the live
                # control page while recording, the refreshed list after a
                # stop — even when no menu was open (the plugin adopts the
                # voice-prefixed generation this repaint arrives under).
                if self._capability_opens is not None:
                    await self._capability_opens.reflect_quicknotes()
                return voice_note_result
        # Derived-note commands (summarize / ask / reminders / notes export)
        # sit between the recorder's exact lifecycle grammar and the broad
        # "note …" text prefix so neither can shadow them.
        if self._note_insights is not None:
            insight_result = await self._note_insights.handle_command(text)
            if insight_result is not None:
                return insight_result
        if self._notes is not None:
            note_result = await self._notes.handle_command(text)
            if note_result is not None:
                return note_result
        if self._productivity is not None:
            productivity_result = await self._productivity.handle_command(text)
            if productivity_result is not None:
                return productivity_result
        # Agent Terminal claims only utterances that SAY "terminal", so it
        # sits late in the order: nothing broader can be shadowed by it.
        if self._terminal is not None:
            terminal_result = await self._terminal.handle_command(text)
            if terminal_result is not None:
                return terminal_result
        if self._hermes is not None:
            hermes_result = await self._hermes.handle_command(text)
            if hermes_result is not None:
                return hermes_result
        return await handle_model_command(text, self._models)
