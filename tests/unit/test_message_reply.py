"""Selected-conversation reply flow: draft, explicit approval, then send."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

import pytest

from joat.channels.imessage import MessageThread
from joat.channels.message_sender import MessageSendError, MessagesAppSender
from joat.core.interfaces import MessageSender
from joat.core.confirmation import ConfirmationResult
from joat.persistence.actions import ActionJournal
from joat.persistence.store import DurableStore
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

    async def __call__(
        self,
        action_id: str,
        prompt: str,
        timeout_s: float,
        **_binding: str,
    ) -> bool:
        self.calls.append((action_id, prompt, timeout_s))
        return self.accepted


class BlockingConfirm:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(
        self,
        _action_id: str,
        _prompt: str,
        _timeout_s: float,
        **_binding: str,
    ) -> bool:
        self.started.set()
        await self.release.wait()
        return True


class FakeContacts:
    def __init__(self, matches: list[tuple[str, str]]) -> None:
        self._matches = matches
        self.queries: list[str] = []

    def search(self, query: str) -> list[tuple[str, str]]:
        self.queries.append(query)
        return self._matches


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

    async def test_new_message_resolves_one_contact_then_uses_exact_confirmation(
        self,
    ) -> None:
        sender, confirm = FakeSender(), ScriptedConfirm(True)
        contacts = FakeContacts([("Adele Vance", "+14055550177")])
        replies = MessageReplyController(sender, confirm, contact_search=contacts.search)
        replies.begin_compose(trace_id="hud-new-message")

        result = await replies.handle_command("message Adele saying Running late")

        assert result == "Submitted to Messages for Adele Vance; delivery unverified."
        assert contacts.queries == ["Adele"]
        assert sender.sent == [("+14055550177", "Running late")]
        assert confirm.calls[0][1] == "Send to Adele Vance? “Running late”"

    async def test_new_message_arms_a_two_step_recipient_then_body_flow(self) -> None:
        sender, confirm = FakeSender(), ScriptedConfirm(True)
        contacts = FakeContacts([("Adele Vance", "+14055550177")])
        replies = MessageReplyController(sender, confirm, contact_search=contacts.search)
        replies.begin_compose(trace_id="hud-two-step-compose")

        assert await replies.handle_command("Adele") == (
            "Recipient Adele Vance selected. Say the message."
        )
        assert sender.sent == []
        assert confirm.calls == []

        assert await replies.handle_command("Good morning") == (
            "Submitted to Messages for Adele Vance; delivery unverified."
        )
        assert contacts.queries == ["Adele"]
        assert sender.sent == [("+14055550177", "Good morning")]

    async def test_new_message_compose_expiry_never_falls_through_to_a_send(self) -> None:
        now = [100.0]
        sender, confirm = FakeSender(), ScriptedConfirm(True)
        replies = MessageReplyController(
            sender,
            confirm,
            selection_ttl_s=120.0,
            monotonic=lambda: now[0],
            contact_search=FakeContacts([("Adele", "+14055550177")]).search,
        )
        replies.begin_compose(trace_id="hud-expired-compose")
        now[0] = 221.0

        assert await replies.handle_command("Adele") == (
            "New message draft expired. Open Messages and choose New message again."
        )
        assert confirm.calls == []
        assert sender.sent == []

    @pytest.mark.parametrize(
        ("matches", "expected"),
        [
            ([], "No Mac contact matched Adele. Try a fuller name."),
            (
                [("Adele Vance", "+14055550177"), ("Adele Okafor", "+14055550188")],
                "More than one Mac contact address matched Adele. "
                "Say the full name and message again.",
            ),
        ],
    )
    async def test_new_message_never_sends_to_a_missing_or_ambiguous_contact(
        self,
        matches: list[tuple[str, str]],
        expected: str,
    ) -> None:
        sender, confirm = FakeSender(), ScriptedConfirm(True)
        replies = MessageReplyController(
            sender,
            confirm,
            contact_search=FakeContacts(matches).search,
        )
        replies.begin_compose(trace_id="hud-compose-safe")

        assert await replies.handle_command("message Adele saying Hello") == expected
        assert confirm.calls == []
        assert sender.sent == []

    async def test_new_message_requires_the_glasses_compose_surface(self) -> None:
        sender, confirm = FakeSender(), ScriptedConfirm(True)
        replies = MessageReplyController(
            sender,
            confirm,
            contact_search=FakeContacts([("Adele", "+14055550177")]).search,
        )

        assert await replies.handle_command("message Adele saying Hello") == (
            "Open Messages and choose New message first."
        )
        assert confirm.calls == []
        assert sender.sent == []

    async def test_home_cleanup_disarms_new_message_compose(self) -> None:
        sender, confirm = FakeSender(), ScriptedConfirm(True)
        replies = MessageReplyController(
            sender,
            confirm,
            contact_search=FakeContacts([("Adele", "+14055550177")]).search,
        )
        replies.begin_compose(trace_id="hud-compose-home")

        replies.clear_selection("home")

        assert await replies.handle_command("message Adele saying Hello") == (
            "Open Messages and choose New message first."
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

        assert result == "Submitted to Messages for Dele; delivery unverified."
        assert sender.sent == [("+14055550123", "On my way")]

    async def test_accepted_confirmation_clears_the_selected_recipient(self) -> None:
        sender, confirm = FakeSender(), ScriptedConfirm(True)
        replies = MessageReplyController(sender, confirm)
        replies.select(THREAD, trace_id="hud-one-shot")

        assert await replies.handle_command("reply saying First") == (
            "Submitted to Messages for Dele; delivery unverified."
        )
        assert await replies.handle_command("reply saying Second") == (
            "Select a Messages conversation first."
        )
        assert sender.sent == [("+14055550123", "First")]

    async def test_selected_recipient_expires_before_a_late_voice_draft(self) -> None:
        now = [100.0]
        sender, confirm = FakeSender(), ScriptedConfirm(True)
        replies = MessageReplyController(
            sender,
            confirm,
            selection_ttl_s=120.0,
            monotonic=lambda: now[0],
        )
        replies.select(THREAD, trace_id="hud-expiring")
        now[0] = 221.0

        assert await replies.handle_command("reply saying Too late") == (
            "Message selection expired. Open Messages and choose the conversation again."
        )
        assert confirm.calls == []
        assert sender.sent == []

    async def test_second_draft_cannot_open_while_confirmation_is_visible(self) -> None:
        sender, confirm = FakeSender(), BlockingConfirm()
        replies = MessageReplyController(sender, confirm)
        replies.select(THREAD, trace_id="hud-confirming")

        first = asyncio.create_task(replies.handle_command("reply saying First"))
        await confirm.started.wait()
        second: str | None = None
        try:
            second = await asyncio.wait_for(
                replies.handle_command("reply saying Second"), timeout=0.05,
            )
        finally:
            confirm.release.set()
            await first
        assert second == "A message reply is already awaiting confirmation."
        assert sender.sent == [("+14055550123", "First")]

    async def test_sender_failure_is_visible_and_never_claims_sent(self) -> None:
        sender = FakeSender(MessageSendError(
            "Messages unavailable", possible_external_commit=False,
        ))
        replies = MessageReplyController(sender, ScriptedConfirm(True))
        replies.select(THREAD, trace_id="hud-9")

        assert await replies.handle_command("reply saying On my way") == (
            "Could not send to Dele. Check Messages on your Mac."
        )

    async def test_uncertain_sender_failure_never_claims_the_reply_failed(self) -> None:
        sender = FakeSender(MessageSendError(
            "Messages outcome unknown", possible_external_commit=True,
        ))
        replies = MessageReplyController(sender, ScriptedConfirm(True))
        replies.select(THREAD, trace_id="hud-uncertain")

        assert await replies.handle_command("reply saying On my way") == (
            "Messages may have submitted the reply to Dele. "
            "Check Messages on your Mac before trying again."
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

    async def test_utf16_capacity_does_not_truncate_emoji(self) -> None:
        sender, confirm = FakeSender(), ScriptedConfirm(True)
        replies = MessageReplyController(sender, confirm)
        replies.select(THREAD, trace_id="hud-emoji")

        result = await replies.handle_command(f"reply saying {'🥽' * 60}")

        assert result == (
            "Reply is too long for the confirmation screen. Shorten it and try again."
        )
        assert confirm.calls == []

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

        assert await replies.handle_command(f"reply saying {secret_body}") == (
            "Submitted to Messages for Dele; delivery unverified."
        )

        assert "hud-privacy-1" in caplog.text
        assert "stage=selected" in caplog.text
        assert "stage=confirmation-requested" in caplog.text
        assert "stage=submitted delivery=unverified" in caplog.text
        assert secret_body not in caplog.text
        assert THREAD.handle not in caplog.text

    async def test_durable_flow_commits_intent_and_honest_receipt_before_cleanup(
        self,
        tmp_path: Path,
    ) -> None:
        sender = FakeSender()

        async def confirm(
            action_id: str,
            prompt: str,
            _timeout_s: float,
            *,
            payload_digest: str,
        ) -> ConfirmationResult:
            return ConfirmationResult(
                action_id=action_id,
                accepted=True,
                challenge_id="challenge-durable",
                session_id="session-durable",
                payload_digest=bytes.fromhex(payload_digest),
                preview_digest=hashlib.sha256(
                    b"joat-hud-confirm-v1\0" + prompt.encode("utf-8")
                ).digest(),
                pairing_identity_digest=b"p" * 32,
                authorization_digest=b"a" * 32,
                issued_at_us=10,
                expires_at_us=1_000,
                preview_completed_at_us=20,
                decided_at_us=20,
            )

        with DurableStore(tmp_path / "joat.sqlite3") as store:
            journal = ActionJournal(store)
            replies = MessageReplyController(sender, confirm, journal)
            replies.select(THREAD, trace_id="hud-durable")

            assert await replies.handle_command("reply saying On my way") == (
                "Submitted to Messages for Dele; delivery unverified."
            )
            action = store.execute(
                "SELECT action_id,state FROM actions WHERE kind='message.reply'"
            ).fetchone()
            assert action is not None
            assert action[1] == "succeeded"
            assert store.execute(
                "SELECT verification_level FROM action_receipts WHERE action_id=?",
                (action[0],),
            ).fetchone() == ("delivery_unverified",)
            assert store.execute(
                "SELECT count(*) FROM action_payloads WHERE action_id=?",
                (action[0],),
            ).fetchone() == (0,)


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
