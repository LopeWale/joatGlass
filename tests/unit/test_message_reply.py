"""Selected-conversation reply flow: draft, explicit approval, then send."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import threading
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from joat.channels.imessage import MessageThread
from joat.channels.message_sender import MessageSendError, MessagesAppSender
from joat.core.interfaces import MessageSender
from joat.core.confirmation import ConfirmationResult
from joat.persistence.actions import ActionJournal
from joat.persistence.store import DurableStore
from joat.services.commands import VoiceCommandRouter
from joat.services.message_reply import MessageActionStatus, MessageReplyController
from joat.services.models import ModelRegistry


class FakeSender(MessageSender):
    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.chats: list[tuple[str, str]] = []
        self._error = error

    async def send(self, recipient: str, text: str) -> None:
        if self._error is not None:
            raise self._error
        self.sent.append((recipient, text))

    async def send_chat(self, chat_guid: str, text: str) -> None:
        if self._error is not None:
            raise self._error
        self.chats.append((chat_guid, text))


class ScriptedConfirm:
    def __init__(self, accepted: bool) -> None:
        self.accepted = accepted
        self.calls: list[tuple[str, str, float]] = []
        self.bindings: list[dict[str, str]] = []

    async def __call__(
        self,
        action_id: str,
        prompt: str,
        timeout_s: float,
        **binding: str,
    ) -> bool:
        self.calls.append((action_id, prompt, timeout_s))
        self.bindings.append(binding)
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
    @pytest.mark.parametrize("accepted", [False, True])
    async def test_group_reply_confirms_and_targets_the_whole_selected_chat(
        self, accepted: bool,
    ) -> None:
        sender, confirm = FakeSender(), ScriptedConfirm(accepted)
        replies = MessageReplyController(sender, confirm)
        thread = MessageThread(
            "Project team", "+14055550123", "Latest participant", 1.0,
            chat_guid="iMessage;+;group-a", is_group=True,
        )
        replies.select(thread, "group-reply")
        assert replies.selected_reply_available()
        body = "  Exact reply,\nwith punctuation!  "
        await replies.prepare_selected_reply(action_id="group-reply", body=body)

        assert await _completed_message(replies, "group-reply") == (
            "sent" if accepted else "cancelled"
        )
        assert sender.chats == ([(thread.chat_guid, body)] if accepted else [])
        assert sender.sent == []
        assert confirm.calls[0][1] == f"Send to group Project team? “{body}”"
        expected_payload = json.dumps(
            {"body": body, "target": {"kind": "chat", "id": thread.chat_guid}},
            ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        ).encode()
        assert confirm.bindings[0]["payload_digest"] == hashlib.sha256(expected_payload).hexdigest()
        assert thread.chat_guid not in confirm.calls[0][1]
        await replies.aclose()

    async def test_group_without_guid_refuses_all_reply_entry_points(self) -> None:
        sender, confirm = FakeSender(), ScriptedConfirm(True)
        replies = MessageReplyController(sender, confirm)
        replies.select(MessageThread("Team", "latest-person", "", 0, is_group=True), "missing-guid")

        assert not replies.selected_reply_available()
        assert "no Messages send target" in (await replies.handle_command("reply") or "")
        assert "no Messages send target" in (await replies.handle_command("reply saying Hi") or "")
        status = await replies.prepare_selected_reply(action_id="no-guid", body="Hi")
        assert status.state == "failed"
        assert confirm.calls == []
        assert sender.sent == sender.chats == []
        await replies.aclose()

    async def test_reply_identity_distinguishes_chat_ids_and_target_kinds(self) -> None:
        sender, confirm = FakeSender(), ScriptedConfirm(False)
        replies = MessageReplyController(sender, confirm)
        first = MessageThread("Team", "latest-person", "", 0, chat_guid="same-id", is_group=True)
        replies.select(first, "first")
        await replies.prepare_selected_reply(action_id="bound-chat", body="Hi")
        assert await _completed_message(replies, "bound-chat") == "cancelled"
        for other in (
            MessageThread("Team", "latest-person", "", 0, chat_guid="other-id", is_group=True),
            MessageThread("Team", "same-id", "", 0),
        ):
            replies.select(other, "different-target")
            with pytest.raises(ValueError, match="different message request"):
                await replies.prepare_selected_reply(action_id="bound-chat", body="Hi")
        assert len(confirm.calls) == 1
        assert sender.sent == sender.chats == []
        await replies.aclose()

    async def test_group_journal_binds_guid_instead_of_participant(self, tmp_path: Path) -> None:
        sender = FakeSender()
        guid = "iMessage;+;private-group-guid"
        with DurableStore(tmp_path / "joat.sqlite3") as store:
            async def confirm(
                action_id: str, _prompt: str, _timeout_s: float, **binding: str,
            ) -> bool:
                payload = store.execute(
                    "SELECT canonical_payload FROM action_payloads WHERE action_id=?", (action_id,),
                ).fetchone()[0]
                decoded = json.loads(bytes(payload))
                assert decoded == {"body": "Hello", "target": {"kind": "chat", "id": guid}}
                assert binding["payload_digest"] == hashlib.sha256(bytes(payload)).hexdigest()
                target_bytes = json.dumps(
                    decoded["target"], separators=(",", ":"), sort_keys=True,
                ).encode()
                assert store.execute(
                    "SELECT target_digest FROM actions WHERE action_id=?", (action_id,),
                ).fetchone()[0] == hashlib.sha256(b"joat-message-target-v2\0" + target_bytes).digest()
                return False

            replies = MessageReplyController(sender, confirm, ActionJournal(store))
            replies.select(MessageThread("Team", "latest-person", "", 0, chat_guid=guid, is_group=True), "journal-group")
            assert await replies.handle_command("reply saying Hello") == "Reply cancelled."
            assert sender.sent == sender.chats == []

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
        # the paint before that window begins AND the restore after the tap,
        # since an acceptance is sent only once the page beneath is back.
        assert timeout_s == 20.0
        assert confirm.bindings == [{"diagnostic_kind": "messages", "payload_digest": confirm.bindings[0]["payload_digest"]}]

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

    async def test_second_compose_body_cannot_open_while_confirmation_is_visible(self) -> None:
        sender, confirm = FakeSender(), BlockingConfirm()
        replies = MessageReplyController(
            sender,
            confirm,
            contact_search=FakeContacts([("Adele Vance", "+14055550177")]).search,
        )
        replies.begin_compose(trace_id="hud-compose-confirming")
        assert await replies.handle_command("Adele") == (
            "Recipient Adele Vance selected. Say the message."
        )

        first = asyncio.create_task(replies.handle_command("First"))
        await confirm.started.wait()
        second: str | None = None
        try:
            second = await asyncio.wait_for(replies.handle_command("Second"), timeout=0.05)
        finally:
            confirm.release.set()
            await first

        assert second == "A message reply is already awaiting confirmation."
        assert sender.sent == [("+14055550177", "First")]

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
            diagnostic_kind: str | None = None,
        ) -> ConfirmationResult:
            assert diagnostic_kind == "messages"
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


class _ControlledMessageProcess:
    def __init__(self, *, ignore_terminate: bool = False) -> None:
        self.returncode: int | None = None
        self.ignore_terminate = ignore_terminate
        self.started = asyncio.Event()
        self.exited = asyncio.Event()
        self.terminated = asyncio.Event()
        self.events: list[str] = []

    async def communicate(self) -> tuple[bytes, bytes]:
        self.started.set()
        await self.exited.wait()
        self.events.append("communicated")
        return b"", b""

    def terminate(self) -> None:
        self.events.append("terminate")
        self.terminated.set()
        if not self.ignore_terminate:
            self.returncode = -15
            self.exited.set()

    def kill(self) -> None:
        self.events.append("kill")
        self.returncode = -9
        self.exited.set()

    async def wait(self) -> int:
        await self.exited.wait()
        self.events.append("waited")
        assert self.returncode is not None
        return self.returncode


class TestMessagesAppSender:
    async def test_chat_send_uses_exact_chat_id_and_literal_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[object, ...]] = []

        async def spawn(*args: object, **_kwargs: object) -> _ControlledMessageProcess:
            calls.append(args)
            process = _ControlledMessageProcess()
            process.returncode = 0
            process.exited.set()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        guid = ' iMessage;+;literal-"-chat '
        body = '  Text " stays literal\nwith newlines  '
        await MessagesAppSender().send_chat(guid, body)

        assert len(calls) == 1
        assert calls[0][-2:] == (guid, body)
        script = str(calls[0][2])
        assert "chat id chatGuid" in script
        assert "send messageBody to targetChat" in script
        assert "participant" not in script
        assert guid not in script

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
        body = '  Quote " and newline\nstay literal  \n'
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

    @pytest.mark.parametrize(
        ("recipient", "body"),
        [
            ("", "hello"), ("+14055550123", " \n "),
            ("person\0@example.test", "hello"), ("+14055550123", "hello\0there"),
            ("\ud800", "hello"), ("+14055550123", "hello\udfff"),
        ],
    )
    @pytest.mark.parametrize("method", ["send", "send_chat"])
    async def test_invalid_input_is_rejected_before_spawning(
        self, monkeypatch: pytest.MonkeyPatch, recipient: str, body: str, method: str,
    ) -> None:
        calls: list[tuple[object, ...]] = []

        async def spawn(*args: object, **_kwargs: object) -> _ControlledMessageProcess:
            calls.append(args)
            process = _ControlledMessageProcess()
            process.returncode = 0
            process.exited.set()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        with pytest.raises(MessageSendError) as caught:
            await getattr(MessagesAppSender(), method)(recipient, body)
        assert caught.value.possible_external_commit is False
        assert calls == []

    @pytest.mark.parametrize("ignore_terminate", [False, True])
    async def test_communicate_timeout_terminates_and_reaps_only_owned_process(
        self, monkeypatch: pytest.MonkeyPatch, ignore_terminate: bool,
    ) -> None:
        owned = _ControlledMessageProcess(ignore_terminate=ignore_terminate)
        unrelated = _ControlledMessageProcess()
        calls = 0

        async def spawn(*_args: object, **_kwargs: object) -> _ControlledMessageProcess:
            nonlocal calls
            calls += 1
            return owned

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        sender = MessagesAppSender(timeout_s=0.01, terminate_grace_s=0.01, kill_grace_s=0.1)
        with pytest.raises(MessageSendError) as caught:
            await asyncio.wait_for(sender.send("+14055550123", "hello"), timeout=0.3)

        assert caught.value.possible_external_commit is True
        assert calls == 1
        assert owned.events[0] == "terminate"
        assert ("kill" in owned.events) is ignore_terminate
        assert "communicated" in owned.events or "waited" in owned.events
        assert owned.returncode is not None
        assert unrelated.events == []

    @pytest.mark.parametrize("cancel_twice", [False, True])
    async def test_cancellation_waits_for_owned_child_cleanup(
        self, monkeypatch: pytest.MonkeyPatch, cancel_twice: bool,
    ) -> None:
        owned = _ControlledMessageProcess(ignore_terminate=True)

        async def spawn(*_args: object, **_kwargs: object) -> _ControlledMessageProcess:
            return owned

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        sender = MessagesAppSender(timeout_s=10.0, terminate_grace_s=0.03, kill_grace_s=0.1)
        sending = asyncio.create_task(sender.send("+14055550123", "hello"))
        try:
            await asyncio.wait_for(owned.started.wait(), timeout=0.2)
            sending.cancel()
            await asyncio.wait_for(owned.terminated.wait(), timeout=0.2)
            if cancel_twice:
                sending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await sending
            assert owned.events[0:2] == ["terminate", "kill"]
            assert "communicated" in owned.events or "waited" in owned.events
            assert owned.returncode == -9
        finally:
            owned.kill()
            await asyncio.gather(sending, return_exceptions=True)

    @pytest.mark.parametrize("bound", ["timeout_s", "terminate_grace_s", "kill_grace_s"])
    @pytest.mark.parametrize("value", [0.0, -1.0, float("inf"), float("nan")])
    def test_executor_bounds_must_be_positive_and_finite(self, bound: str, value: float) -> None:
        with pytest.raises(ValueError):
            MessagesAppSender(**{bound: value})

    @pytest.mark.parametrize("cancel", [False, True])
    async def test_isolated_python_child_is_gone_after_timeout_or_cancellation(
        self, monkeypatch: pytest.MonkeyPatch, cancel: bool,
    ) -> None:
        real_spawn = asyncio.create_subprocess_exec
        children: list[asyncio.subprocess.Process] = []
        started = asyncio.Event()

        async def spawn(*args: object, **_kwargs: object) -> asyncio.subprocess.Process:
            assert args[0] == "osascript"
            # Replace the executable completely. This test cannot open or
            # send through Messages and signals only the Python child it owns.
            child = await real_spawn(
                sys.executable, "-c",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print('ready', flush=True); time.sleep(60)",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            children.append(child)
            assert child.stdout is not None
            assert await asyncio.wait_for(child.stdout.readline(), timeout=1.0) == b"ready\n"
            started.set()
            return child

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        sender = MessagesAppSender(timeout_s=0.05, terminate_grace_s=0.02, kill_grace_s=0.5)
        sending = asyncio.create_task(sender.send("+14055550123", "hello"))
        try:
            await asyncio.wait_for(started.wait(), timeout=1.0)
            if cancel:
                sending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(sending, timeout=1.0)
            else:
                with pytest.raises(MessageSendError) as caught:
                    await asyncio.wait_for(sending, timeout=1.0)
                assert caught.value.possible_external_commit is True
            assert len(children) == 1
            child = children[0]
            assert child.returncode == -9
            with pytest.raises(ProcessLookupError):
                os.kill(child.pid, 0)
        finally:
            for child in children:
                if child.returncode is None:
                    child.kill()
                await child.communicate()
            sending.cancel()
            await asyncio.gather(sending, return_exceptions=True)


async def _completed_message(replies: MessageReplyController, action_id: str) -> str:
    async def wait_for_result() -> str:
        while True:
            status = replies.message_action_status(action_id)
            if status.state not in {"awaiting_input", "awaiting_confirmation", "sending"}:
                return status.state
            await asyncio.sleep(0)

    return await asyncio.wait_for(wait_for_result(), timeout=1.0)


class TestPreparedMessages:
    """Real controller tasks with controlled contacts, approval and send adapters."""

    @pytest.mark.parametrize("text", ["Dele", "Tell me a joke", "Could you open News?"])
    async def test_typed_input_does_not_swallow_shared_conversation(self, text: str) -> None:
        sender, confirm = FakeSender(), ScriptedConfirm(True)
        replies = MessageReplyController(sender, confirm)
        replies.select(THREAD, trace_id="typed-input-routing")
        before = await replies.prepare_selected_reply(action_id="typed-input-routing")
        assert before.state == "awaiting_input"
        try:
            assert await replies.handle_command(text) is None
            assert replies.message_action_status(before.action_id) == before
            assert sender.sent == []
            assert confirm.calls == []
        finally:
            await replies.aclose()

    @pytest.mark.parametrize("text", ["Dele", "Tell me a joke", "Could you open News?"])
    async def test_typed_confirmation_does_not_swallow_shared_conversation(self, text: str) -> None:
        sender, confirm = FakeSender(), BlockingConfirm()
        replies = MessageReplyController(sender, confirm)
        replies.select(THREAD, trace_id="typed-confirm-routing")
        before = await replies.prepare_selected_reply(action_id="typed-confirm-routing", body="Hello")
        try:
            await asyncio.wait_for(confirm.started.wait(), timeout=0.2)
            assert await replies.handle_command(text) is None
            assert replies.message_action_status(before.action_id) == before
            assert sender.sent == []
        finally:
            await replies.aclose()

    @pytest.mark.parametrize("text", ["reply", "reply saying Duplicate", "message Adele saying Duplicate"])
    async def test_message_shaped_duplicate_reports_pending_input(self, text: str) -> None:
        sender, confirm = FakeSender(), ScriptedConfirm(True)
        replies = MessageReplyController(sender, confirm)
        replies.select(THREAD, trace_id="typed-input-duplicate")
        before = await replies.prepare_selected_reply(action_id="typed-input-duplicate")
        try:
            result = await replies.handle_command(text)
            assert result is not None and "awaiting input" in result.lower()
            assert replies.message_action_status(before.action_id) == before
            assert sender.sent == []
            assert confirm.calls == []
        finally:
            await replies.aclose()

    async def test_preparation_returns_while_confirmation_is_still_pending(self) -> None:
        sender, confirm = FakeSender(), BlockingConfirm()
        replies = MessageReplyController(sender, confirm)
        replies.select(THREAD, trace_id="typed-reply")

        pending = await asyncio.wait_for(
            replies.prepare_selected_reply(action_id="request-1", body="On my way"),
            timeout=0.05,
        )
        try:
            await asyncio.wait_for(confirm.started.wait(), timeout=0.2)
            assert pending.action_id == "request-1"
            assert pending.state == "awaiting_confirmation"
            assert sender.sent == []
            confirm.release.set()
            assert await _completed_message(replies, pending.action_id) == "sent"
            status = replies.message_action_status(pending.action_id)
            assert status.verification_level == "delivery_unverified"
            assert "delivery unverified" in status.message
            assert sender.sent == [(THREAD.handle, "On my way")]
        finally:
            await replies.aclose()

    async def test_unknown_relationship_requires_a_real_contact_name(self) -> None:
        contacts = FakeContacts([("Wrong Guess", "+14055550999")])
        sender, confirm = FakeSender(), ScriptedConfirm(True)
        replies = MessageReplyController(sender, confirm, contact_search=contacts.search)

        status = await replies.prepare_new_message(
            action_id="wife-request", trace_id="conversation", recipient_name="my wife",
        )

        assert status.state == "awaiting_input"
        assert status.needs_input == "recipient"
        assert "contact" in status.message.lower()
        assert contacts.queries == []
        assert confirm.calls == []
        assert sender.sent == []
        await replies.aclose()

    async def test_ambiguous_contacts_require_an_opaque_candidate_choice(self) -> None:
        contacts = FakeContacts([("Alex Chen", "+14055550111"), ("Alex Lee", "+14055550222")])
        sender, confirm = FakeSender(), ScriptedConfirm(False)
        replies = MessageReplyController(sender, confirm, contact_search=contacts.search)

        status = await replies.prepare_new_message(
            action_id="alex-request", trace_id="conversation",
            recipient_name="Alex", body="Running late",
        )

        assert status.state == "awaiting_input"
        assert status.needs_input == "recipient"
        assert len(status.candidates) == 2
        assert "+14055550" not in repr(status)
        assert confirm.calls == []
        with pytest.raises(ValueError):
            await replies.provide_message_input("alex-request", recipient_ref="+14055550222")
        chosen = status.candidates[1]
        await replies.provide_message_input("alex-request", recipient_ref=chosen.reference)
        assert await _completed_message(replies, "alex-request") == "cancelled"
        assert confirm.calls[0][1] == "Send to Alex Lee? “Running late”"
        assert sender.sent == []
        await replies.aclose()

    async def test_prompted_body_keeps_punctuation_and_never_redispatches_commands(self) -> None:
        contacts = FakeContacts([("Adele Vance", "+14055550177")])
        sender, confirm = FakeSender(), ScriptedConfirm(True)
        replies = MessageReplyController(sender, confirm, contact_search=contacts.search)
        status = await replies.prepare_new_message(
            action_id="body-request", trace_id="conversation", recipient_name="Adele",
        )
        assert status.state == "awaiting_input"
        assert status.needs_input == "body"

        body = "Good morning,\nplease reply: I'm coming!"
        await replies.provide_message_input(status.action_id, body=body)

        assert await _completed_message(replies, status.action_id) == "sent"
        assert sender.sent == [("+14055550177", body)]
        assert confirm.calls[0][1] == f"Send to Adele Vance? “{body}”"
        await replies.aclose()

    async def test_replayed_action_cannot_duplicate_or_change_a_send(self) -> None:
        contacts = FakeContacts([("Adele", "+14055550177")])
        sender, confirm = FakeSender(), BlockingConfirm()
        replies = MessageReplyController(sender, confirm, contact_search=contacts.search)
        request = dict(action_id="stable-action", trace_id="conversation", recipient_name="Adele", body="First")
        first = await replies.prepare_new_message(**request)
        try:
            await asyncio.wait_for(confirm.started.wait(), timeout=0.2)
            replay = await replies.prepare_new_message(**request)
            assert replay == first
            with pytest.raises(ValueError):
                await replies.prepare_new_message(**{**request, "body": "Changed"})
            with pytest.raises(ValueError):
                await replies.provide_message_input("stable-action", body="Changed")
            confirm.release.set()
            assert await _completed_message(replies, first.action_id) == "sent"
            assert (await replies.prepare_new_message(**request)).state == "sent"
            assert contacts.queries == ["Adele"]
            assert sender.sent == [("+14055550177", "First")]
        finally:
            await replies.aclose()

    async def test_legacy_confirmation_and_prepared_action_share_single_flight(self) -> None:
        sender, confirm = FakeSender(), BlockingConfirm()
        replies = MessageReplyController(sender, confirm)
        replies.select(THREAD, trace_id="legacy")
        legacy = asyncio.create_task(replies.handle_command("reply saying First"))
        await asyncio.wait_for(confirm.started.wait(), timeout=0.2)
        try:
            status = await replies.prepare_selected_reply(action_id="competing", body="Second")
            assert status.state == "failed"
            assert "already" in status.message.lower()
        finally:
            confirm.release.set()
            await legacy
            await replies.aclose()
        assert sender.sent == [(THREAD.handle, "First")]

    async def test_target_change_revokes_pending_confirmation(self) -> None:
        sender, confirm = FakeSender(), BlockingConfirm()
        replies = MessageReplyController(sender, confirm)
        replies.select(THREAD, trace_id="first")
        await replies.prepare_selected_reply(action_id="selected-action", body="First")
        try:
            await asyncio.wait_for(confirm.started.wait(), timeout=0.2)
            replies.select(MessageThread(sender="Other", handle="other@example.test", text="", at=1.0), "second")
            confirm.release.set()
            assert await _completed_message(replies, "selected-action") == "cancelled"
            assert sender.sent == []
        finally:
            await replies.aclose()

    async def test_close_cancels_confirmation_and_rejects_later_preparations(self) -> None:
        sender, confirm = FakeSender(), BlockingConfirm()
        replies = MessageReplyController(sender, confirm)
        replies.select(THREAD, trace_id="closing")
        await replies.prepare_selected_reply(action_id="closing-action", body="Hello")
        await asyncio.wait_for(confirm.started.wait(), timeout=0.2)

        await replies.aclose()
        confirm.release.set()

        assert replies.message_action_status("closing-action").state == "cancelled"
        assert sender.sent == []
        with pytest.raises(RuntimeError):
            await replies.prepare_selected_reply(action_id="after-close", body="Hello")

    async def test_close_during_send_reports_uncertain_and_never_retries(self) -> None:
        class BlockedSender(MessageSender):
            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.calls = 0

            async def send(self, recipient: str, text: str) -> None:
                self.calls += 1
                self.started.set()
                await asyncio.Event().wait()

        sender = BlockedSender()
        replies = MessageReplyController(sender, ScriptedConfirm(True))
        replies.select(THREAD, trace_id="closing-send")
        await replies.prepare_selected_reply(action_id="uncertain-action", body="Hello")
        await asyncio.wait_for(sender.started.wait(), timeout=0.2)

        assert replies.message_action_status("uncertain-action").state == "sending"

        await replies.aclose()

        status = replies.message_action_status("uncertain-action")
        assert status.state == "uncertain"
        assert status.verification_level is None
        assert sender.calls == 1

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (MessageSendError("before send", possible_external_commit=False), "failed"),
            (MessageSendError("unknown commit", possible_external_commit=True), "uncertain"),
            (RuntimeError("unexpected adapter error"), "uncertain"),
        ],
    )
    async def test_status_reports_the_observed_sender_outcome(
        self, error: Exception, expected: str,
    ) -> None:
        sender = FakeSender(error)
        replies = MessageReplyController(sender, ScriptedConfirm(True))
        replies.select(THREAD, trace_id="failure")

        await replies.prepare_selected_reply(action_id="failed-action", body="Hello")

        assert await _completed_message(replies, "failed-action") == expected
        assert replies.message_action_status("failed-action").verification_level is None
        await replies.aclose()

    async def test_unreviewable_body_never_opens_confirmation(self) -> None:
        sender, confirm = FakeSender(), ScriptedConfirm(True)
        replies = MessageReplyController(sender, confirm)
        replies.select(THREAD, trace_id="overflow")

        await replies.prepare_selected_reply(action_id="long-action", body="x" * 120)

        assert await _completed_message(replies, "long-action") == "failed"
        assert sender.sent == []
        assert confirm.calls == []
        await replies.aclose()

    async def test_missing_and_expired_selection_never_becomes_a_reply(self) -> None:
        now = [100.0]
        sender, confirm = FakeSender(), ScriptedConfirm(True)
        replies = MessageReplyController(sender, confirm, monotonic=lambda: now[0])
        assert (await replies.prepare_selected_reply(action_id="missing", body="Hi")).state == "awaiting_input"
        replies.select(THREAD, trace_id="expired")
        now[0] = 221.0

        status = await replies.prepare_selected_reply(action_id="expired", body="Hi")

        assert status.state == "failed"
        assert "expired" in status.message.lower()
        assert confirm.calls == []
        assert sender.sent == []
        await replies.aclose()

    async def test_input_expiry_and_home_cleanup_revoke_drafts(self) -> None:
        now = [100.0]
        contacts = FakeContacts([("Adele", "+14055550177")])
        sender, confirm = FakeSender(), ScriptedConfirm(True)
        replies = MessageReplyController(sender, confirm, contact_search=contacts.search, monotonic=lambda: now[0])
        first = await replies.prepare_new_message(action_id="expires", trace_id="conversation", recipient_name="Adele")
        now[0] = 221.0
        assert (await replies.provide_message_input(first.action_id, body="Too late")).state == "cancelled"
        second = await replies.prepare_new_message(action_id="home", trace_id="conversation", recipient_name="Adele")
        replies.clear_selection("home")
        assert (await replies.provide_message_input(second.action_id, body="Too late")).state == "cancelled"
        assert sender.sent == []
        assert confirm.calls == []
        await replies.aclose()

    @pytest.mark.parametrize("name", ["+14055550123", "person@example.test"])
    async def test_model_supplied_handles_are_rejected_before_contact_lookup(self, name: str) -> None:
        contacts = FakeContacts([("Adele", "+14055550177")])
        replies = MessageReplyController(FakeSender(), ScriptedConfirm(True), contact_search=contacts.search)
        with pytest.raises(ValueError, match="handles"):
            await replies.prepare_new_message(action_id="raw-handle", trace_id="conversation", recipient_name=name, body="Hi")
        assert contacts.queries == []
        await replies.aclose()

    async def test_identity_retention_is_bounded_without_eviction_or_replay(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("joat.services.message_reply.MAX_PREPARED_ACTIONS", 2)
        replies = MessageReplyController(FakeSender(), ScriptedConfirm(True))
        await replies.prepare_new_message(action_id="first", trace_id="conversation")
        replies.cancel_message_action("first")
        await replies.prepare_new_message(action_id="second", trace_id="conversation")
        replies.cancel_message_action("second")

        with pytest.raises(RuntimeError, match="limit"):
            await replies.prepare_new_message(action_id="third", trace_id="conversation")
        assert (await replies.prepare_new_message(action_id="first", trace_id="conversation")).state == "cancelled"
        with pytest.raises(ValueError, match="controller session"):
            replies.message_action_status("previous-process-action")
        await replies.aclose()

    async def test_contact_lookup_deadline_never_dispatches_a_late_result(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("joat.services.message_reply.CONTACT_LOOKUP_TIMEOUT_SECONDS", 0.01)
        release = threading.Event()

        def blocked_contacts(_query: str) -> list[tuple[str, str]]:
            release.wait(timeout=1.0)
            return [("Adele", "+14055550177")]

        sender, confirm = FakeSender(), ScriptedConfirm(True)
        replies = MessageReplyController(sender, confirm, contact_search=blocked_contacts)
        try:
            status = await asyncio.wait_for(replies.prepare_new_message(action_id="slow-contact", trace_id="conversation", recipient_name="Adele", body="Hello"), timeout=0.1)
            assert status.state == "failed"
        finally:
            release.set()
            await replies.aclose()
        assert sender.sent == []
        assert confirm.calls == []

    async def test_prepared_action_reuses_durable_confirmation_and_submission_receipt(
        self, tmp_path: Path,
    ) -> None:
        async def confirm(
            action_id: str, prompt: str, _timeout_s: float, *, payload_digest: str,
            diagnostic_kind: str | None = None,
        ) -> ConfirmationResult:
            assert diagnostic_kind == "messages"
            return ConfirmationResult(
                action_id=action_id, accepted=True, challenge_id="prepared-challenge",
                session_id="prepared-session", payload_digest=bytes.fromhex(payload_digest),
                preview_digest=hashlib.sha256(b"joat-hud-confirm-v1\0" + prompt.encode()).digest(),
                pairing_identity_digest=b"p" * 32, authorization_digest=b"a" * 32,
                issued_at_us=10, expires_at_us=1_000, preview_completed_at_us=20, decided_at_us=20,
            )

        with DurableStore(tmp_path / "joat.sqlite3") as store:
            journal = ActionJournal(store)
            sender = FakeSender()
            replies = MessageReplyController(sender, confirm, journal)
            replies.select(THREAD, trace_id="prepared-durable")
            await replies.prepare_selected_reply(action_id="durable-request", body="On my way")
            assert await _completed_message(replies, "durable-request") == "sent"
            assert (await replies.prepare_selected_reply(action_id="durable-request", body="On my way")).state == "sent"
            assert store.execute("SELECT state FROM actions").fetchall() == [("succeeded",)]
            assert store.execute("SELECT verification_level FROM action_receipts").fetchall() == [("delivery_unverified",)]
            assert store.execute("SELECT count(*) FROM action_payloads").fetchone() == (0,)
            assert sender.sent == [(THREAD.handle, "On my way")]
            await replies.aclose()

            # Caller identities are process-scoped. The durable key still
            # prevents replay after the prior controller's receipts are gone.
            restarted = MessageReplyController(sender, confirm, journal)
            restarted.select(THREAD, trace_id="after-restart")
            await restarted.prepare_selected_reply(action_id="durable-request", body="On my way")
            assert await _completed_message(restarted, "durable-request") == "uncertain"
            assert sender.sent == [(THREAD.handle, "On my way")]
            assert store.execute("SELECT state FROM actions").fetchall() == [("succeeded",)]
            await restarted.aclose()

    async def test_shutdown_cancels_and_purges_an_unconfirmed_durable_action(self, tmp_path: Path) -> None:
        with DurableStore(tmp_path / "joat.sqlite3") as store:
            sender, confirm = FakeSender(), BlockingConfirm()
            replies = MessageReplyController(sender, confirm, ActionJournal(store))
            replies.select(THREAD, trace_id="durable-close")
            await replies.prepare_selected_reply(action_id="close-durable", body="Hello")
            await asyncio.wait_for(confirm.started.wait(), timeout=0.2)

            await replies.aclose()

            assert replies.message_action_status("close-durable").state == "cancelled"
            assert store.execute("SELECT state FROM actions").fetchall() == [("cancelled",)]
            assert store.execute("SELECT count(*) FROM action_payloads").fetchone() == (0,)
            assert sender.sent == []


class TestSpokenReplyGrammar:
    """The lens footer teaches exactly "say reply" — every phrasing a wearer
    would naturally produce from that hint must reach the confirmed send."""

    async def test_bare_reply_prompts_then_next_utterance_becomes_the_body(self) -> None:
        sender, confirm = FakeSender(), ScriptedConfirm(True)
        replies = MessageReplyController(sender, confirm)
        replies.select(THREAD, trace_id="hud-1")

        assert await replies.handle_command("Reply.") == "Say the reply."
        assert replies.is_compose_armed() is True  # first refusal for the body

        result = await replies.handle_command("Good morning, on my way.")
        assert confirm.calls, "the prompted body must reach confirmation"
        assert sender.sent == [(THREAD.handle, "Good morning, on my way.")]
        assert result is not None and "Submitted" in result

    async def test_bare_reply_without_a_selection_says_so(self) -> None:
        sender, confirm = FakeSender(), ScriptedConfirm(True)
        replies = MessageReplyController(sender, confirm)

        assert await replies.handle_command("reply") == (
            "Select a Messages conversation first."
        )
        assert replies.is_compose_armed() is False

    async def test_prompted_reply_can_be_cancelled(self) -> None:
        sender, confirm = FakeSender(), ScriptedConfirm(True)
        replies = MessageReplyController(sender, confirm)
        replies.select(THREAD, trace_id="hud-1")

        assert await replies.handle_command("reply") == "Say the reply."
        assert await replies.handle_command("never mind") == "Reply cancelled."
        assert replies.is_compose_armed() is False
        assert sender.sent == []

    async def test_whisper_commas_and_periods_never_break_the_one_shot_form(self) -> None:
        sender, confirm = FakeSender(), ScriptedConfirm(True)
        replies = MessageReplyController(sender, confirm)
        replies.select(THREAD, trace_id="hud-1")

        result = await replies.handle_command("Reply, saying, On my way.")
        assert sender.sent == [(THREAD.handle, "On my way")]
        assert result is not None and "Submitted" in result

    async def test_full_command_spoken_while_prompted_wins_over_body_capture(self) -> None:
        sender, confirm = FakeSender(), ScriptedConfirm(True)
        replies = MessageReplyController(sender, confirm)
        replies.select(THREAD, trace_id="hud-1")

        assert await replies.handle_command("reply") == "Say the reply."
        await replies.handle_command("reply saying Running late")
        assert sender.sent == [(THREAD.handle, "Running late")]


def _message_ui_controller(
    sender: MessageSender,
    confirm: ScriptedConfirm | BlockingConfirm,
    *,
    begin_reply: Callable[[], Awaitable[str]] | None = None,
    on_status: Callable[[MessageActionStatus], None] | None = None,
    **options: Any,
) -> MessageReplyController:
    return MessageReplyController(
        sender, confirm, begin_reply=begin_reply, on_status=on_status, **options,
    )


def _reply_available(replies: MessageReplyController) -> bool:
    return replies.selected_reply_available()


class AttemptedSender(FakeSender):
    def __init__(self, error: Exception | None = None) -> None:
        super().__init__(error)
        self.attempts = 0

    async def send(self, recipient: str, text: str) -> None:
        self.attempts += 1
        await super().send(recipient, text)


class TestMessageUiSeams:
    """Controller/command integration only; no microphone or delivery proof."""

    @pytest.mark.parametrize("entry", ["Reply.", "respond", "reply to them"])
    async def test_bare_reply_callback_prepares_outside_legacy_lock_and_keeps_later_body_exact(
        self, entry: str,
    ) -> None:
        sender, confirm = AttemptedSender(), ScriptedConfirm(True)
        prepared: list[MessageActionStatus] = []

        async def begin_reply() -> str:
            status = await replies.prepare_selected_reply(action_id="ui-reply")
            prepared.append(status)
            return status.message

        replies = _message_ui_controller(sender, confirm, begin_reply=begin_reply)
        replies.select(THREAD, "ui-selection")
        commands = VoiceCommandRouter(replies, ModelRegistry([]))
        try:
            result = await commands(entry)
            assert len(prepared) == 1, "the actual bare Reply command must call typed preparation"
            assert prepared[0].state == "awaiting_input", "preparation must not run inside the legacy draft lock"
            assert prepared[0].needs_input == "body"
            assert result == prepared[0].message
            assert not replies.is_compose_armed(), "the shared prompt runtime owns the next body"
            assert confirm.calls == [] and sender.attempts == 0
            duplicate = await commands("reply")
            assert duplicate is not None and "awaiting input" in duplicate
            assert len(prepared) == 1

            body = "  Good morning,\r\nJOAT, stop!  "
            await replies.provide_message_input(prepared[0].action_id, body=body)
            assert await _completed_message(replies, prepared[0].action_id) == "sent"
            assert confirm.calls[0][1] == f"Send to {THREAD.sender}? “{body}”"
            assert len(confirm.calls) == 1
            assert sender.sent == [(THREAD.handle, body)]
            assert sender.attempts == 1
        finally:
            await replies.aclose()

    async def test_callback_preserves_inline_reply_and_unrelated_greeting(self) -> None:
        sender, confirm = AttemptedSender(), ScriptedConfirm(True)
        entries: list[str] = []

        async def begin_reply() -> str:
            entries.append("entry")
            return "typed entry"

        replies = _message_ui_controller(sender, confirm, begin_reply=begin_reply)
        replies.select(THREAD, "inline")
        commands = VoiceCommandRouter(replies, ModelRegistry([]))
        try:
            assert await commands("Good morning") == "Good morning. How can I help?"
            result = await commands("reply saying On my way")
            assert result is not None and "Submitted" in result
            assert sender.sent == [(THREAD.handle, "On my way")]
            assert entries == []
        finally:
            await replies.aclose()

    async def test_callback_cannot_claim_a_legacy_compose_body_named_reply(self) -> None:
        sender, confirm = AttemptedSender(), ScriptedConfirm(True)
        entries: list[str] = []

        async def begin_reply() -> str:
            entries.append("entry")
            return "typed entry"

        replies = _message_ui_controller(
            sender, confirm, begin_reply=begin_reply,
            contact_search=FakeContacts([(THREAD.sender, THREAD.handle)]).search,
        )
        replies.begin_compose("legacy-compose")
        commands = VoiceCommandRouter(replies, ModelRegistry([]))
        try:
            result = await commands(THREAD.sender)
            assert result is not None and "selected" in result
            await commands("reply")
            assert entries == []
            assert sender.sent == [(THREAD.handle, "reply")]
        finally:
            await replies.aclose()

    @pytest.mark.parametrize("body", [None, "Hello"])
    async def test_callback_does_not_replace_an_active_typed_action(self, body: str | None) -> None:
        sender, confirm = AttemptedSender(), BlockingConfirm()
        entries: list[str] = []

        async def begin_reply() -> str:
            entries.append("entry")
            return "typed entry"

        replies = _message_ui_controller(sender, confirm, begin_reply=begin_reply)
        replies.select(THREAD, "existing")
        before = await replies.prepare_selected_reply(action_id="existing", body=body)
        commands = VoiceCommandRouter(replies, ModelRegistry([]))
        try:
            result = await commands("reply")
            assert result is not None and "awaiting" in result
            assert entries == []
            assert replies.message_action_status("existing") is before
            assert sender.attempts == 0
        finally:
            await replies.aclose()

    async def test_availability_is_read_only_and_expires_at_the_selection_deadline(self) -> None:
        now = [100.0]
        replies = _message_ui_controller(
            AttemptedSender(), ScriptedConfirm(True),
            selection_ttl_s=10.0, monotonic=lambda: now[0],
        )
        try:
            assert not _reply_available(replies)
            replies.select(THREAD, "available")
            assert _reply_available(replies)
            now[0] = 109.999
            assert _reply_available(replies)
            now[0] = 110.0
            assert not _reply_available(replies)
            now[0] = 111.0
            assert not _reply_available(replies)
            replies.select(THREAD, "new-selection")
            assert _reply_available(replies)
            replies.clear_selection("home")
            assert not _reply_available(replies)
        finally:
            await replies.aclose()

    @pytest.mark.parametrize("owner", ["compose", "typed-input", "typed-confirmation", "closed"])
    async def test_availability_rejects_another_owner_or_closed_controller(self, owner: str) -> None:
        sender, confirm = AttemptedSender(), BlockingConfirm()
        replies = _message_ui_controller(sender, confirm)
        replies.select(THREAD, "before-owner")
        try:
            assert _reply_available(replies)
            if owner == "compose":
                replies.begin_compose("legacy-compose")
            elif owner.startswith("typed"):
                await replies.prepare_selected_reply(
                    action_id="typed-owner", body="Hello" if owner == "typed-confirmation" else None,
                )
            else:
                await replies.aclose()
                replies.select(THREAD, "late-selection-after-close")
            assert not _reply_available(replies)
            assert sender.attempts == 0
        finally:
            await replies.aclose()

    async def test_legacy_confirmation_blocks_callback_and_availability(self) -> None:
        sender, confirm = AttemptedSender(), BlockingConfirm()
        entries: list[str] = []

        async def begin_reply() -> str:
            entries.append("entry")
            return "typed entry"

        replies = _message_ui_controller(sender, confirm, begin_reply=begin_reply)
        replies.select(THREAD, "legacy-confirmation")
        commands = VoiceCommandRouter(replies, ModelRegistry([]))
        pending = asyncio.create_task(commands("reply saying First"))
        try:
            await asyncio.wait_for(confirm.started.wait(), .2)
            assert not _reply_available(replies)
            result = await commands("reply")
            assert result is not None and "confirmation" in result
            assert entries == [] and sender.attempts == 0
        finally:
            confirm.release.set()
            await pending
            await replies.aclose()

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (None, "sent"),
            (MessageSendError("before submit", possible_external_commit=False), "failed"),
            (MessageSendError("possible submit", possible_external_commit=True), "uncertain"),
        ],
    )
    async def test_observer_receives_committed_sender_outcome_without_poll_republication(
        self, error: Exception | None, expected: str,
    ) -> None:
        sender, confirm = AttemptedSender(error), ScriptedConfirm(True)
        observed: list[MessageActionStatus] = []
        committed: list[bool] = []

        def observe(status: MessageActionStatus) -> None:
            observed.append(status)
            committed.append(replies.message_action_status(status.action_id) is status)

        replies = _message_ui_controller(sender, confirm, on_status=observe)
        replies.select(THREAD, "outcome")
        try:
            await replies.prepare_selected_reply(action_id="outcome", body="Hello")
            assert await _completed_message(replies, "outcome") == expected
            assert [status.state for status in observed] == ["awaiting_confirmation", "sending", expected]
            assert committed == [True, True, True]
            current = replies.message_action_status("outcome")
            assert observed[-1] is current
            assert current.verification_level == ("delivery_unverified" if expected == "sent" else None)
            assert sender.attempts == 1 and len(confirm.calls) == 1
            before_poll = list(observed)
            replies.message_action_status("outcome")
            assert observed == before_poll
        finally:
            await replies.aclose()

    async def test_terminal_observer_runs_after_action_owner_cleanup(self) -> None:
        sender, confirm = AttemptedSender(), ScriptedConfirm(True)
        available_at_cancellation: list[bool] = []

        def observe(status: MessageActionStatus) -> None:
            if status.state == "cancelled":
                available_at_cancellation.append(_reply_available(replies))

        replies = _message_ui_controller(sender, confirm, on_status=observe)
        replies.select(THREAD, "cancel-before-confirmation")
        try:
            await replies.prepare_selected_reply(action_id="cancel-before-confirmation")
            status = replies.cancel_message_action("cancel-before-confirmation")
            assert status.state == "cancelled"
            assert available_at_cancellation == [True], "a cancelled action must release its owner before observation"
            assert sender.attempts == 0 and confirm.calls == []
        finally:
            await replies.aclose()

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (None, "sent"),
            (MessageSendError("before submit", possible_external_commit=False), "failed"),
            (MessageSendError("possible submit", possible_external_commit=True), "uncertain"),
        ],
    )
    async def test_observer_exception_preserves_result_and_never_retries_send(
        self, error: Exception | None, expected: str, caplog: pytest.LogCaptureFixture,
    ) -> None:
        sender, confirm = AttemptedSender(error), ScriptedConfirm(True)
        observed: list[str] = []
        private_error = "observer-private-fixture-content"

        def observe(status: MessageActionStatus) -> None:
            observed.append(status.state)
            raise RuntimeError(private_error)

        replies = _message_ui_controller(sender, confirm, on_status=observe)
        replies.select(THREAD, "observer-failure")
        try:
            await replies.prepare_selected_reply(action_id="observer-failure", body="Hello")
            assert await _completed_message(replies, "observer-failure") == expected
            assert observed == ["awaiting_confirmation", "sending", expected]
            assert sender.attempts == 1 and len(confirm.calls) == 1
            assert private_error not in caplog.text
            assert replies.message_action_status("observer-failure").state == expected
        finally:
            await replies.aclose()
