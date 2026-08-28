"""Selected-conversation reply flow: draft, explicit approval, then send."""

from __future__ import annotations

import asyncio
import logging

import pytest

from joat.channels.imessage import MessageThread
from joat.channels.message_sender import MessageSendError, MessagesAppSender
from joat.core.interfaces import MessageSender
from joat.services.message_reply import MessageReplyController


class FakeSender(MessageSender):
    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self._error = error

    async def send(self, recipient: str, text: str) -> None:
        if self._error is not None:
            raise self._error
        self.sent.append((recipient, text))


class ScriptedConfirm:
    def __init__(self, accepted: bool) -> None:
        self.accepted = accepted
        self.calls: list[tuple[str, str, float]] = []

    async def __call__(self, action_id: str, prompt: str, timeout_s: float) -> bool:
        self.calls.append((action_id, prompt, timeout_s))
        return self.accepted


THREAD = MessageThread(
    sender="Dele",
    handle="+14055550123",
    text="Are you still coming?",
    at=123.0,
)


class TestMessageReplyController:
    async def test_requires_a_selected_conversation_before_drafting(self) -> None:
        sender, confirm = FakeSender(), ScriptedConfirm(True)
        replies = MessageReplyController(sender, confirm)

        assert await replies.handle_command("reply saying On my way") == (
            "Select a Messages conversation first."
        )
        assert confirm.calls == []
        assert sender.sent == []

    async def test_declined_draft_never_reaches_the_sender(self) -> None:
        sender, confirm = FakeSender(), ScriptedConfirm(False)
        replies = MessageReplyController(sender, confirm)
        replies.select(THREAD, trace_id="hud-7")

        result = await replies.handle_command("reply saying On my way")

        assert result == "Reply cancelled."
        assert sender.sent == []
        assert len(confirm.calls) == 1
        action_id, prompt, timeout_s = confirm.calls[0]
        assert action_id.startswith("message-reply:hud-7:")
        assert prompt == "Send to Dele? “On my way”"
        # The plugin owns an eight-second VISIBLE window. The transport gets
        # four extra seconds for the four-tile HUD paint before that begins.
        assert timeout_s == 12.0

    async def test_confirmed_draft_sends_exactly_once_to_the_selected_handle(self) -> None:
        sender, confirm = FakeSender(), ScriptedConfirm(True)
        replies = MessageReplyController(sender, confirm)
        replies.select(THREAD, trace_id="hud-8")

        result = await replies.handle_command("reply saying On my way")

        assert result == "Sent to Dele."
        assert sender.sent == [("+14055550123", "On my way")]

    async def test_sender_failure_is_visible_and_never_claims_sent(self) -> None:
        sender = FakeSender(MessageSendError("Messages unavailable"))
        replies = MessageReplyController(sender, ScriptedConfirm(True))
        replies.select(THREAD, trace_id="hud-9")

        assert await replies.handle_command("reply saying On my way") == (
            "Could not send to Dele. Check Messages on your Mac."
        )

    async def test_never_sends_text_the_confirmation_screen_cannot_show(self) -> None:
        sender, confirm = FakeSender(), ScriptedConfirm(True)
        replies = MessageReplyController(sender, confirm)
        replies.select(THREAD, trace_id="hud-visibility")

        result = await replies.handle_command(f"reply saying {'x' * 120}")

        assert result == (
            "Reply is too long for the confirmation screen. Shorten it and try again."
        )
        assert confirm.calls == []
        assert sender.sent == []

    async def test_unrelated_command_falls_through(self) -> None:
        replies = MessageReplyController(FakeSender(), ScriptedConfirm(True))
        replies.select(THREAD, trace_id="hud-10")
        assert await replies.handle_command("what is the weather") is None

    async def test_action_trace_never_logs_recipient_or_draft(self, caplog) -> None:  # type: ignore[no-untyped-def]
        caplog.set_level(logging.INFO)
        sender, confirm = FakeSender(), ScriptedConfirm(True)
        replies = MessageReplyController(sender, confirm)
        secret_body = "The vault phrase is marigold"
        replies.select(THREAD, trace_id="hud-privacy-1")

        assert await replies.handle_command(f"reply saying {secret_body}") == "Sent to Dele."

        assert "hud-privacy-1" in caplog.text
        assert "stage=selected" in caplog.text
        assert "stage=confirmation-requested" in caplog.text
        assert "stage=sent" in caplog.text
        assert secret_body not in caplog.text
        assert THREAD.handle not in caplog.text


class TestMessagesAppSender:
    async def test_passes_recipient_and_body_as_argv_never_shell_text(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        captured: tuple[object, ...] | None = None

        class Process:
            returncode = 0

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"", b""

        async def spawn(*args: object, **_kwargs: object) -> Process:
            nonlocal captured
            captured = args
            return Process()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        body = 'Quote " and newline\nstay literal'
        await MessagesAppSender().send("+14055550123", body)

        assert captured is not None
        assert captured[0] == "osascript"
        assert captured[-2:] == ("+14055550123", body)

    async def test_nonzero_osascript_exit_raises_a_typed_failure(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        class Process:
            returncode = 1

            async def communicate(self) -> tuple[bytes, bytes]:
                return b"", b"Messages is not signed in"

        async def spawn(*_args: object, **_kwargs: object) -> Process:
            return Process()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        with pytest.raises(MessageSendError, match="Messages is not signed in"):
            await MessagesAppSender().send("+14055550123", "hello")
