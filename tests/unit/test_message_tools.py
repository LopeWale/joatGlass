"""Message tool contracts against real preparation with synthetic I/O only."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
import json
from typing import Any

import pytest

from joat.agents.message_tools import message_tools
from joat.agents.tools import Tool, ToolBelt, ToolCall
from joat.channels.imessage import MessageThread
from joat.core.interfaces import MessageSender
from joat.services.message_reply import MessageActionStatus, MessageReplyController


class Sender(MessageSender):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.attempts: list[tuple[str, str]] = []
        self.sent: list[tuple[str, str]] = []

    async def send(self, recipient: str, text: str) -> None:
        self.attempts.append((recipient, text))
        self.started.set()
        await self.release.wait()
        self.sent.append((recipient, text))


class Confirmation:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.returned = asyncio.Event()
        self.accepted = True
        self.calls: list[tuple[str, str, float]] = []

    async def __call__(self, action_id: str, prompt: str, timeout_s: float, **_binding: str) -> bool:
        self.calls.append((action_id, prompt, timeout_s))
        self.started.set()
        await self.release.wait()
        self.returned.set()
        return self.accepted


class Contacts:
    def __init__(self) -> None:
        self.matches = [("Sam", "+14055550111")]
        self.queries: list[str] = []

    def search(self, query: str) -> list[tuple[str, str]]:
        self.queries.append(query)
        return list(self.matches)


@dataclass
class Rig:
    replies: MessageReplyController
    tools: dict[str, Tool]
    sender: Sender
    confirm: Confirmation
    contacts: Contacts

    async def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        result = json.loads(await self.tools[name].run(arguments))
        assert isinstance(result, dict)
        return result


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    sender, confirm, contacts = Sender(), Confirmation(), Contacts()
    replies = MessageReplyController(sender, confirm, contact_search=contacts.search)
    try:
        yield Rig(replies, {tool.name: tool for tool in message_tools(replies)}, sender, confirm, contacts)
    finally:
        await replies.aclose()


THREAD = MessageThread(sender="Dele", handle="+14055550222", text="Coming?", at=123.0)


async def completed(rig: Rig, action_id: str) -> dict[str, Any]:
    async def wait() -> dict[str, Any]:
        while True:
            status = await rig.call("message_action_status", action_id=action_id)
            if status["state"] not in {"awaiting_input", "awaiting_confirmation", "sending"}:
                return status
            await asyncio.sleep(0)

    return await asyncio.wait_for(wait(), timeout=.5)


async def test_prepare_returns_before_tool_deadline_and_confirmation_completes_later(rig: Rig) -> None:
    rig.replies.select(THREAD, trace_id="selected-thread")
    belt = ToolBelt(list(rig.tools.values()), timeout_s=.03)
    result = await belt.execute(ToolCall("prepare_message", {
        "mode": "reply", "action_id": "reply-1", "body": "On my way",
    }))
    status = json.loads(result)
    assert status["state"] == "awaiting_confirmation"
    await asyncio.wait_for(rig.confirm.started.wait(), timeout=.5)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(rig.confirm.returned.wait(), timeout=.06)
    assert rig.sender.attempts == []
    assert (await rig.call("message_action_status", action_id="reply-1"))["state"] == "awaiting_confirmation"

    rig.confirm.release.set()
    status = await completed(rig, "reply-1")
    assert status["state"] == "sent"
    assert status["verification_level"] == "delivery_unverified"
    assert "delivery unverified" in status["message"]
    assert rig.sender.sent == [(THREAD.handle, "On my way")]


async def test_new_message_keeps_exact_prompted_body_and_requires_confirmation(rig: Rig) -> None:
    status = await rig.call("prepare_message", mode="new", action_id="new-1", recipient_name="Sam")
    assert status["state"] == "awaiting_input" and status["needs_input"] == "body"
    assert rig.contacts.queries == ["Sam"]
    assert rig.confirm.calls == [] and rig.sender.attempts == []

    body = "  Héllo,\nreply: I'm on my way!  "
    status = await rig.call("provide_message_input", action_id="new-1", body=body)
    assert status["state"] == "awaiting_confirmation"
    await asyncio.wait_for(rig.confirm.started.wait(), timeout=.5)
    assert rig.confirm.calls[0][1] == f"Send to Sam? “{body}”"
    assert rig.sender.attempts == []
    rig.confirm.release.set()
    assert (await completed(rig, "new-1"))["state"] == "sent"
    assert rig.sender.sent == [("+14055550111", body)]


async def test_new_message_retries_keep_one_action_and_stable_trace_across_factory_instances(rig: Rig) -> None:
    arguments = {"mode": "new", "action_id": "stable-1", "recipient_name": "Sam", "body": "Hello"}
    first = await rig.call("prepare_message", **arguments)
    assert first["state"] == "awaiting_confirmation"
    await asyncio.wait_for(rig.confirm.started.wait(), timeout=.5)
    second_tools = {tool.name: tool for tool in message_tools(rig.replies)}
    assert json.loads(await second_tools["prepare_message"].run(arguments)) == first
    conflict = await rig.tools["prepare_message"].run({**arguments, "body": "Changed"})
    assert conflict.startswith("error:")
    assert len(rig.confirm.calls) == 1 and rig.contacts.queries == ["Sam"]

    rig.confirm.release.set()
    assert (await completed(rig, "stable-1"))["state"] == "sent"
    assert json.loads(await second_tools["prepare_message"].run(arguments))["state"] == "sent"
    assert rig.sender.sent == [("+14055550111", "Hello")]
    assert len(rig.confirm.calls) == 1


async def test_relationship_name_requires_contact_clarification_and_preserves_body(rig: Rig) -> None:
    status = await rig.call("prepare_message", mode="new", action_id="wife-1", recipient_name="my wife", body="Home at six")
    assert status["state"] == "awaiting_input" and status["needs_input"] == "recipient"
    assert rig.contacts.queries == [] and rig.sender.attempts == []
    status = await rig.call("provide_message_input", action_id="wife-1", recipient_name="Sam")
    assert status["state"] == "awaiting_confirmation"
    await asyncio.wait_for(rig.confirm.started.wait(), timeout=.5)
    rig.confirm.accepted = False
    rig.confirm.release.set()
    assert (await completed(rig, "wife-1"))["state"] == "cancelled"
    assert rig.confirm.calls[0][1] == "Send to Sam? “Home at six”"
    assert rig.sender.attempts == []


async def test_ambiguous_recipient_uses_action_scoped_reference_not_model_handle(rig: Rig) -> None:
    rig.contacts.matches = [("Alex", "+14055550111"), ("Alex", "+14055550333")]
    status = await rig.call("prepare_message", mode="new", action_id="choice-1", recipient_name="Alex", body="Running late")
    assert status["state"] == "awaiting_input" and len(status["candidates"]) == 2
    assert "+14055550" not in json.dumps(status)
    assert rig.confirm.calls == [] and rig.sender.attempts == []
    for reference in ("+14055550333", "unknown-reference"):
        result = await rig.tools["provide_message_input"].run({"action_id": "choice-1", "recipient_ref": reference})
        assert result.startswith("error:")
    chosen = status["candidates"][1]["reference"]
    status = await rig.call("provide_message_input", action_id="choice-1", recipient_ref=chosen)
    assert status["state"] == "awaiting_confirmation"
    rig.confirm.release.set()
    assert (await completed(rig, "choice-1"))["state"] == "sent"
    assert rig.sender.sent == [("+14055550333", "Running late")]


async def test_candidate_reference_from_another_action_is_rejected(rig: Rig) -> None:
    rig.contacts.matches = [("Alex One", "+14055550111"), ("Alex Two", "+14055550333")]
    first = await rig.call("prepare_message", mode="new", action_id="first", recipient_name="Alex")
    old_reference = first["candidates"][0]["reference"]
    await rig.call("cancel_message_action", action_id="first")
    second = await rig.call("prepare_message", mode="new", action_id="second", recipient_name="Alex")
    result = await rig.tools["provide_message_input"].run({"action_id": "second", "recipient_ref": old_reference})
    assert result.startswith("error:")
    assert (await rig.call("message_action_status", action_id="second")) == second
    assert rig.confirm.calls == [] and rig.sender.attempts == []


async def test_reply_without_selection_reports_required_selection(rig: Rig) -> None:
    result = await rig.call("prepare_message", mode="reply", action_id="unselected", body="Hi")
    assert result["state"] == "awaiting_input" and result["needs_input"] == "recipient"
    assert "Select" in result["message"]
    assert rig.contacts.queries == [] and rig.sender.attempts == []


async def test_cancel_during_confirmation_revokes_send_and_repeated_cancel_is_stable(rig: Rig) -> None:
    rig.replies.select(THREAD, trace_id="cancel-thread")
    await rig.call("prepare_message", mode="reply", action_id="cancel-1", body="Hi")
    await asyncio.wait_for(rig.confirm.started.wait(), timeout=.5)
    cancelled = await rig.call("cancel_message_action", action_id="cancel-1")
    assert cancelled["state"] == "cancelled"
    rig.confirm.release.set()
    assert await completed(rig, "cancel-1") == cancelled
    assert await rig.call("cancel_message_action", action_id="cancel-1") == cancelled
    assert rig.sender.attempts == []


async def test_status_preserves_sending_then_uncertainty_when_cancelled_during_sender_io(rig: Rig) -> None:
    rig.sender.release.clear()
    rig.replies.select(THREAD, trace_id="send-thread")
    arguments = {"mode": "reply", "action_id": "send-1", "body": "Hi"}
    await rig.call("prepare_message", **arguments)
    rig.confirm.release.set()
    await asyncio.wait_for(rig.sender.started.wait(), timeout=.5)
    status = await rig.call("message_action_status", action_id="send-1")
    assert status["state"] == "sending" and status["verification_level"] is None
    assert (await rig.call("cancel_message_action", action_id="send-1"))["state"] == "uncertain"
    assert (await rig.call("prepare_message", **arguments))["state"] == "uncertain"
    assert len(rig.sender.attempts) == 1 and rig.sender.sent == []


async def test_controller_review_limit_remains_a_truthful_failed_status(rig: Rig) -> None:
    rig.replies.select(THREAD, trace_id="review-cap")
    await rig.call("prepare_message", mode="reply", action_id="long-1", body="x" * 300)
    status = await completed(rig, "long-1")
    assert status["state"] == "failed" and "confirmation screen" in status["message"]
    assert rig.confirm.calls == [] and rig.sender.attempts == []


@pytest.mark.parametrize("arguments", [
    {}, {"mode": "new"}, {"mode": True, "action_id": "invalid-1"},
    {"mode": "send", "action_id": "invalid-1"}, {"mode": "new", "action_id": True},
    {"mode": "new", "action_id": []}, {"mode": "new", "action_id": ""},
    {"mode": "new", "action_id": "x" * 97}, {"mode": "new", "action_id": "unsafe id"},
    {"mode": "new", "action_id": "invalid-1", "recipient_name": True},
    {"mode": "new", "action_id": "invalid-1", "recipient_name": None},
    {"mode": "new", "action_id": "invalid-1", "recipient_name": " "},
    {"mode": "new", "action_id": "invalid-1", "recipient_name": "x" * 201},
    {"mode": "new", "action_id": "invalid-1", "recipient_name": "sam@example.test"},
    {"mode": "new", "action_id": "invalid-1", "recipient_name": "+14055550111"},
    {"mode": "reply", "action_id": "invalid-1", "recipient_name": "Sam"},
    {"mode": "new", "action_id": "invalid-1", "body": True},
    {"mode": "new", "action_id": "invalid-1", "body": None},
    {"mode": "new", "action_id": "invalid-1", "body": " "},
    {"mode": "new", "action_id": "invalid-1", "body": "bad\x00text"},
    {"mode": "new", "action_id": "invalid-1", "body": "bad\ud800text"},
    {"mode": "new", "action_id": "invalid-1", "body": "x" * 2001},
    {"mode": "new", "action_id": "invalid-1", "recipient_handle": "+14055550111"},
    {"mode": "new", "action_id": "invalid-1", "confirmed": True},
    {"mode": "new", "action_id": "invalid-1", "send": True},
    {"mode": "new", "action_id": "invalid-1", "trace_id": "model-trace"},
])
async def test_prepare_validates_application_arguments_before_action_creation(rig: Rig, arguments: dict[str, Any]) -> None:
    assert (await rig.tools["prepare_message"].run(arguments)).startswith("error:")
    with pytest.raises(ValueError, match="Unknown"):
        rig.replies.message_action_status("invalid-1")
    assert rig.contacts.queries == [] and rig.confirm.calls == [] and rig.sender.attempts == []


@pytest.mark.parametrize("fields", [
    {}, {"body": None}, {"body": 7}, {"body": ""}, {"body": "bad\x00text"},
    {"body": "bad\ud800text"}, {"body": "x" * 2001},
    {"recipient_name": True}, {"recipient_ref": True}, {"recipient_ref": ""},
    {"recipient_ref": "x" * 129}, {"recipient_ref": "user@example.test"},
    {"recipient_name": "Sam", "recipient_ref": "some-reference"},
    {"recipient_handle": "+14055550111"}, {"confirm": True}, {"send": True},
])
async def test_input_validates_application_arguments_without_advancing_draft(rig: Rig, fields: dict[str, Any]) -> None:
    original = await rig.replies.prepare_new_message(action_id="input-1", trace_id="test")
    assert (await rig.tools["provide_message_input"].run({"action_id": "input-1", **fields})).startswith("error:")
    assert rig.replies.message_action_status("input-1") == original
    assert rig.contacts.queries == [] and rig.confirm.calls == [] and rig.sender.attempts == []


@pytest.mark.parametrize("name", ["message_action_status", "cancel_message_action"])
@pytest.mark.parametrize("arguments", [{}, {"action_id": True}, {"action_id": []}, {"action_id": "known", "confirmed": True}])
async def test_status_and_cancel_validate_ids_and_reject_extra_fields(rig: Rig, name: str, arguments: dict[str, Any]) -> None:
    assert (await rig.tools[name].run(arguments)).startswith("error:")
    assert rig.sender.attempts == []


@pytest.mark.parametrize("name,arguments", [
    ("message_action_status", {"action_id": "unknown"}),
    ("cancel_message_action", {"action_id": "unknown"}),
    ("provide_message_input", {"action_id": "unknown", "body": "Hi"}),
])
async def test_unknown_actions_return_errors_without_invented_completion(rig: Rig, name: str, arguments: dict[str, Any]) -> None:
    result = await rig.tools[name].run(arguments)
    assert result.startswith("error:") and "Unknown" in result
    assert rig.sender.attempts == []


@pytest.mark.parametrize("text", ["Tell me a joke", "How are you today?", "Explain the sky"])
async def test_message_tools_do_not_add_local_decision_pass_for_ordinary_chat(rig: Rig, text: str) -> None:
    assert ToolBelt(list(rig.tools.values())).requires_model_decision(text) is False


@pytest.mark.parametrize("text", [
    "Send a message to my wife", "Could you tell her I'm on my way?",
    "Reply to Dele", "Choose this contact", "Was my message sent?", "Cancel the message",
])
async def test_message_hints_leave_tool_choice_to_model_and_publish_all_cli_tools(rig: Rig, text: str) -> None:
    belt = ToolBelt(list(rig.tools.values()))
    assert belt.requires_model_decision(text) is True
    assert belt.explicit_call(text) is None
    assert {item["name"] for item in belt.tool_definitions()} == {
        "prepare_message", "provide_message_input", "message_action_status", "cancel_message_action",
        "remember_relationship",
    }


async def test_mutation_callback_receives_controller_status_for_prepare_provide_cancel_only(rig: Rig) -> None:
    seen: list[MessageActionStatus] = []

    async def after_input(status: MessageActionStatus) -> None:
        seen.append(status)

    rig.tools = {tool.name: tool for tool in message_tools(rig.replies, after_input=after_input)}
    await rig.call("prepare_message", mode="new", action_id="callback-1", recipient_name="Sam")
    prepared = rig.replies.message_action_status("callback-1")
    assert seen == [prepared] and seen[0] is prepared
    await rig.call("message_action_status", action_id="callback-1")
    assert seen == [prepared]

    await rig.call("provide_message_input", action_id="callback-1", body="Hello")
    confirming = rig.replies.message_action_status("callback-1")
    assert confirming.state == "awaiting_confirmation"
    assert len(seen) == 2 and seen[1] is confirming
    await rig.call("cancel_message_action", action_id="callback-1")
    cancelled = rig.replies.message_action_status("callback-1")
    assert cancelled.state == "cancelled"
    assert len(seen) == 3 and seen[2] is cancelled
    await rig.call("message_action_status", action_id="callback-1")
    assert len(seen) == 3 and rig.sender.attempts == []


async def test_mutation_callback_gets_identical_status_on_duplicate_prepare(rig: Rig) -> None:
    seen: list[MessageActionStatus] = []

    async def after_input(status: MessageActionStatus) -> None:
        seen.append(status)

    tool = {item.name: item for item in message_tools(rig.replies, after_input=after_input)}["prepare_message"]
    arguments = {"mode": "new", "action_id": "callback-retry", "recipient_name": "Sam"}
    first = await tool.run(arguments)
    repeated = await tool.run(arguments)
    assert first == repeated
    assert len(seen) == 2 and seen[0] is seen[1]
    assert rig.contacts.queries == ["Sam"] and rig.sender.attempts == []


async def test_mutation_callback_failure_does_not_erase_a_successful_action_status(rig: Rig) -> None:
    seen: list[MessageActionStatus] = []

    async def after_input(status: MessageActionStatus) -> None:
        seen.append(status)
        raise OSError("synthetic prompt publication failure")

    rig.tools = {tool.name: tool for tool in message_tools(rig.replies, after_input=after_input)}
    result = await rig.call("prepare_message", mode="new", action_id="callback-failure", recipient_name="Sam")
    assert result["state"] == "awaiting_input" and result["needs_input"] == "body"
    assert len(seen) == 1 and seen[0] is rig.replies.message_action_status("callback-failure")
    assert rig.sender.attempts == []


@pytest.mark.parametrize("name,arguments", [
    ("prepare_message", {"mode": "new", "action_id": "invalid-callback", "body": None}),
    ("provide_message_input", {"action_id": "unknown", "body": "Hello"}),
    ("cancel_message_action", {"action_id": "unknown"}),
])
async def test_invalid_mutation_does_not_invoke_prompt_callback(
    rig: Rig, name: str, arguments: dict[str, Any],
) -> None:
    seen: list[MessageActionStatus] = []

    async def after_input(status: MessageActionStatus) -> None:
        seen.append(status)

    tool = {item.name: item for item in message_tools(rig.replies, after_input=after_input)}[name]
    assert (await tool.run(arguments)).startswith("error:")
    assert seen == [] and rig.sender.attempts == []
