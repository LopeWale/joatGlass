"""Typed message prompt ownership with real controller/dispatcher and fake I/O.

These tests exercise the bridge callback, not its future message_tools wiring.
The root-owned integration fixture covers that registration boundary.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace

import pytest

from joat.channels.imessage import MessageThread
from joat.core.interfaces import MessageSender, Transcript
from joat.core.protocol import AudioSource
from joat.services.message_prompts import MessagePromptBridge, PromptPresentationAck
from joat.services.message_reply import MessageActionStatus, MessageReplyController
from joat.services.prompted_input import (
    PromptedInputDispatcher, PromptTicket, UtteranceProvenance,
)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class Sender(MessageSender):
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, recipient: str, text: str) -> None:
        self.sent.append((recipient, text))


class Confirmation:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[str] = []

    async def __call__(self, _action_id: str, prompt: str, _timeout_s: float, **_binding: str) -> bool:
        self.calls.append(prompt)
        self.started.set()
        await self.release.wait()
        return False


class Contacts:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.matches = {
            "Sam": [("Sam", "+14055550111")],
            "Alex": [("Alex One", "+14055550222"), ("Alex Two", "+14055550333")],
        }

    def search(self, name: str) -> list[tuple[str, str]]:
        self.queries.append(name)
        return list(self.matches.get(name, []))


class Publisher:
    """A controllable presenter acknowledgment, never a gateway-send receipt."""

    def __init__(self) -> None:
        self.calls: list[tuple[PromptTicket, str]] = []
        self.started = asyncio.Event()
        self.hold: asyncio.Event | None = None
        self.failure: str | None = None
        self.cancelled = 0
        self.forced_ack: PromptPresentationAck | None = None

    async def __call__(
        self, ticket: PromptTicket, question: str,
        activate: Callable[[PromptPresentationAck], bool],
    ) -> bool:
        self.calls.append((ticket, question))
        self.started.set()
        hold = self.hold
        try:
            if hold is not None:
                await hold.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if self.failure == "exception":
            raise OSError("synthetic presenter failure")
        if self.failure == "no_ack":
            return False
        if self.forced_ack is not None:
            return activate(self.forced_ack)
        return activate(PromptPresentationAck(
            prompt_id=ticket.prompt_id,
            success=self.failure != "rejected",
            presentation_binding=ticket.presentation_binding,
            connection_generation=ticket.connection_generation,
            audio_epoch=ticket.prompt_id,
            ingress_fence=100,
        ))


@dataclass
class Rig:
    clock: Clock
    generation: list[str | None]
    replies: MessageReplyController
    dispatcher: PromptedInputDispatcher
    bridge: MessagePromptBridge
    publisher: Publisher
    sender: Sender
    confirm: Confirmation
    contacts: Contacts

    async def prepare(self, **inputs: str) -> MessageActionStatus:
        return await self.replies.prepare_new_message(
            action_id="action-1", trace_id="test-action", **inputs,
        )

    async def say(self, text: str, ticket: PromptTicket | None = None) -> bool:
        chosen = ticket or self.dispatcher.current_ticket
        assert chosen is not None
        return await self.dispatcher.dispatch(
            Transcript(text=text, is_final=True),
            UtteranceProvenance(
                source="glasses", connection_generation=chosen.connection_generation,
                audio_epoch=chosen.prompt_id,
                first_ingress_ordinal=101, last_ingress_ordinal=110,
                speaker_roles=frozenset(), mixed=False,
            ),
        )


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    clock, sender, confirm, contacts = Clock(), Sender(), Confirmation(), Contacts()
    generation: list[str | None] = ["audio-connection-1"]
    replies = MessageReplyController(
        sender, confirm, contact_search=contacts.search,
        monotonic=clock, selection_ttl_s=30.0,
    )
    dispatcher = PromptedInputDispatcher(monotonic=clock)
    publisher = Publisher()
    bridge = MessagePromptBridge(
        replies, dispatcher, publish=publisher,
        connection_generation=lambda: generation[0],
    )
    try:
        yield Rig(clock, generation, replies, dispatcher, bridge, publisher, sender, confirm, contacts)
    finally:
        dispatcher.revoke()
        await replies.aclose()


async def test_prepare_callback_publishes_once_and_status_polling_does_not_rearm(rig: Rig) -> None:
    status = await rig.prepare(recipient_name="Sam")
    assert rig.publisher.calls == []
    await rig.bridge.after_input(status)
    ticket = rig.dispatcher.current_ticket
    assert ticket is not None and rig.dispatcher.is_armed
    assert ticket.source == "glasses"
    assert ticket.owner_id == status.action_id and ticket.field == "body"
    assert rig.publisher.calls == [(ticket, status.message)]

    rig.clock.now += 5.0
    assert rig.replies.message_action_status(status.action_id) is status
    assert rig.dispatcher.current_ticket is ticket
    await rig.bridge.after_input(status)
    assert rig.dispatcher.current_ticket is ticket
    assert rig.publisher.calls == [(ticket, status.message)]


async def test_message_prompt_audio_source_is_frozen_until_the_next_question(rig: Rig) -> None:
    source: list[AudioSource] = ["phone"]
    bridge = MessagePromptBridge(
        rig.replies, rig.dispatcher, publish=rig.publisher,
        connection_generation=lambda: rig.generation[0], audio_source=lambda: source[0],
    )
    status = await rig.prepare()
    release = rig.publisher.hold = asyncio.Event()
    pending = asyncio.create_task(bridge.after_input(status))
    try:
        await asyncio.wait_for(rig.publisher.started.wait(), .5)
        first = rig.dispatcher.current_ticket
        assert first is not None and first.source == "phone" and first.field == "recipient"
        source[0] = "glasses"
        release.set()
        await pending
        assert rig.dispatcher.current_ticket is first and rig.dispatcher.is_armed
        assert first.source == "phone"
        assert await rig.say("Sam", first) is False
        assert rig.contacts.queries == []
        assert await rig.dispatcher.dispatch(
            Transcript("Sam", True),
            UtteranceProvenance(
                source="phone", connection_generation=first.connection_generation,
                audio_epoch=first.prompt_id, first_ingress_ordinal=101,
                last_ingress_ordinal=110, speaker_roles=frozenset(),
            ),
        )
        second = rig.dispatcher.current_ticket
        assert second is not None and second.field == "body"
        assert second.prompt_id != first.prompt_id and second.source == "glasses"
        assert rig.contacts.queries == ["Sam"]
        assert rig.sender.sent == []
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_duplicate_prepare_during_suspended_publication_preserves_ticket_and_expiry(rig: Rig) -> None:
    status = await rig.prepare(recipient_name="Sam")
    release = rig.publisher.hold = asyncio.Event()
    first = asyncio.create_task(rig.bridge.after_input(status))
    second: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(rig.publisher.started.wait(), timeout=.5)
        ticket = rig.dispatcher.current_ticket
        assert ticket is not None and not rig.dispatcher.is_armed
        rig.clock.now += 3.0
        assert await rig.prepare(recipient_name="Sam") is status
        second = asyncio.create_task(rig.bridge.after_input(status))
        await asyncio.sleep(0)
        assert rig.dispatcher.current_ticket is ticket
        assert len(rig.publisher.calls) == 1
        release.set()
        await asyncio.gather(first, second)
        assert rig.dispatcher.current_ticket is ticket and rig.dispatcher.is_armed
        assert len(rig.publisher.calls) == 1
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second is not None else []), return_exceptions=True)


async def test_body_is_delivered_exactly_once_without_an_overlay_over_confirmation(rig: Rig) -> None:
    status = await rig.prepare(recipient_name="Sam")
    await rig.bridge.after_input(status)
    ticket = rig.dispatcher.current_ticket
    assert ticket is not None
    body = "  Good morning,\nstop: open the door!  "
    assert await rig.say(body, ticket)
    await asyncio.wait_for(rig.confirm.started.wait(), timeout=.5)
    assert rig.confirm.calls == [f"Send to Sam? “{body}”"]
    assert rig.replies.message_action_status(status.action_id).state == "awaiting_confirmation"
    assert len(rig.publisher.calls) == 1
    assert not rig.dispatcher.is_armed
    assert await rig.say(body, ticket) is False
    assert rig.sender.sent == []


async def test_spoken_contact_advances_to_a_fresh_body_question_without_a_model(rig: Rig) -> None:
    status = await rig.prepare()
    await rig.bridge.after_input(status)
    first = rig.dispatcher.current_ticket
    assert first is not None and first.field == "recipient"
    assert await rig.say("Sam", first)
    second = rig.dispatcher.current_ticket
    assert second is not None and second.field == "body"
    assert second.prompt_id != first.prompt_id
    assert rig.contacts.queries == ["Sam"]
    assert len(rig.publisher.calls) == 2
    assert rig.publisher.calls[-1][1] == rig.replies.message_action_status(status.action_id).message
    assert await rig.say("Good morning", first) is False
    assert await rig.say("Good morning", second)
    await asyncio.wait_for(rig.confirm.started.wait(), timeout=.5)
    assert rig.confirm.calls == ["Send to Sam? “Good morning”"]
    assert len(rig.publisher.calls) == 2 and rig.sender.sent == []


@pytest.mark.parametrize(("field", "rejected"), [
    pytest.param("recipient", "+1 (405) 555-0111", id="raw-phone-contact"),
    pytest.param("body", "Rejected\x00body", id="nul-body"),
    pytest.param("body", "x" * 2001, id="overlength-body"),
])
async def test_rejected_spoken_input_republishes_question_and_accepts_corrected_speech(
    rig: Rig, field: str, rejected: str, caplog: pytest.LogCaptureFixture,
) -> None:
    status = await rig.prepare(**({"recipient_name": "Sam"} if field == "body" else {}))
    await rig.bridge.after_input(status)
    original = rig.dispatcher.current_ticket
    assert original is not None and original.field == field

    # A fresh, owned utterance was handled even when its exact content cannot
    # fill the typed field. It must not escape to another speech consumer.
    assert await rig.say(rejected, original)
    replacement = rig.dispatcher.current_ticket
    assert replacement is not None and rig.dispatcher.is_armed
    assert replacement.prompt_id != original.prompt_id and replacement.field == field
    assert rig.replies.message_action_status(status.action_id) is status
    assert len(rig.publisher.calls) == 2
    assert rig.confirm.calls == [] and rig.sender.sent == []
    assert rejected not in caplog.text

    # Retrying the original tool result must not republish again, and a valid
    # answer captured under the consumed prompt cannot satisfy the new one.
    await rig.bridge.after_input(status)
    assert rig.dispatcher.current_ticket is replacement and len(rig.publisher.calls) == 2
    corrected_body = "  Good morning,\nopen the door.  "
    corrected = "Sam" if field == "recipient" else corrected_body
    assert await rig.say(corrected, original) is False
    assert await rig.say(corrected, replacement)
    if field == "recipient":
        body_prompt = rig.dispatcher.current_ticket
        assert body_prompt is not None and body_prompt.field == "body"
        assert await rig.say(corrected_body, body_prompt)
    await asyncio.wait_for(rig.confirm.started.wait(), timeout=.5)
    assert rig.contacts.queries == ["Sam"]
    assert rig.confirm.calls == [f"Send to Sam? “{corrected_body}”"]
    assert rig.replies.message_action_status(status.action_id).state == "awaiting_confirmation"
    assert rig.dispatcher.current_ticket is None and rig.sender.sent == []


async def test_ambiguous_spoken_name_keeps_explicit_candidate_clarification(rig: Rig) -> None:
    status = await rig.prepare(body="Running late")
    await rig.bridge.after_input(status)
    assert await rig.say("Alex")
    ambiguous = rig.replies.message_action_status(status.action_id)
    assert ambiguous.state == "awaiting_input" and ambiguous.needs_input == "recipient"
    assert len(ambiguous.candidates) == 2
    assert rig.contacts.queries == ["Alex"]
    assert rig.confirm.calls == [] and rig.sender.sent == []
    # A candidate reference is a separate explicit UI/tool choice. The bridge
    # may request a fuller name but cannot select an address on its own.
    if rig.dispatcher.current_ticket is not None:
        assert rig.dispatcher.current_ticket.field == "recipient"


async def test_selected_reply_without_a_conversation_never_arms_an_unusable_contact_prompt(rig: Rig) -> None:
    status = await rig.replies.prepare_selected_reply(action_id="unselected", body="Hello")
    assert status.state == "awaiting_input" and status.needs_input == "recipient"
    await rig.bridge.after_input(status)
    assert rig.publisher.calls == []
    assert rig.dispatcher.current_ticket is None
    assert rig.contacts.queries == [] and rig.sender.sent == []


async def test_a_new_equal_text_question_rearms_but_an_old_immutable_status_does_not(rig: Rig) -> None:
    old = await rig.prepare(recipient_name="Nobody")
    await rig.bridge.after_input(old)
    first = rig.dispatcher.current_ticket
    assert first is not None
    assert await rig.say("Nobody", first)
    current_status = rig.replies.message_action_status(old.action_id)
    assert current_status == old and current_status is not old
    second = rig.dispatcher.current_ticket
    assert second is not None and second.prompt_id != first.prompt_id
    assert len(rig.publisher.calls) == 2
    await rig.bridge.after_input(old)
    assert rig.dispatcher.current_ticket is second
    assert len(rig.publisher.calls) == 2 and rig.sender.sent == []


async def test_an_unrelated_failed_action_cannot_revoke_the_current_question(rig: Rig) -> None:
    current = await rig.prepare(recipient_name="Sam")
    await rig.bridge.after_input(current)
    ticket = rig.dispatcher.current_ticket
    other = await rig.replies.prepare_new_message(action_id="other-action", trace_id="other")
    assert other.state == "failed"
    await rig.bridge.after_input(other)
    assert rig.dispatcher.current_ticket is ticket and rig.dispatcher.is_armed
    assert len(rig.publisher.calls) == 1


@pytest.mark.parametrize("reason", ["cancel", "list", "home", "disconnect", "expiry", "target_change"])
async def test_owner_invalidation_during_async_paint_cannot_arm_a_late_ack(rig: Rig, reason: str) -> None:
    status = await rig.prepare(recipient_name="Sam")
    release = rig.publisher.hold = asyncio.Event()
    pending = asyncio.create_task(rig.bridge.after_input(status))
    try:
        await asyncio.wait_for(rig.publisher.started.wait(), timeout=.5)
        ticket = rig.dispatcher.current_ticket
        assert ticket is not None and not rig.dispatcher.is_armed
        if reason == "cancel":
            rig.replies.cancel_message_action(status.action_id)
        elif reason == "expiry":
            rig.clock.now += 31.0
        elif reason == "target_change":
            rig.replies.select(MessageThread(sender="Other", handle="other@example.test", text="", at=1.0), "changed")
        else:
            rig.replies.clear_selection(reason)
        release.set()
        await pending
        assert not rig.dispatcher.is_armed
        assert await rig.say("This must not become a body", ticket) is False
        assert rig.confirm.calls == [] and rig.sender.sent == []
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("reason", ["list", "home", "disconnect"])
async def test_back_home_or_disconnect_rejects_input_after_prompt_ack(rig: Rig, reason: str) -> None:
    status = await rig.prepare(recipient_name="Sam")
    await rig.bridge.after_input(status)
    ticket = rig.dispatcher.current_ticket
    assert ticket is not None and rig.dispatcher.is_armed
    rig.replies.clear_selection(reason)
    assert await rig.say("Good morning", ticket) is False
    assert rig.confirm.calls == [] and rig.sender.sent == []


async def test_mutating_cancel_callback_revokes_the_matching_prompt_without_publishing(rig: Rig) -> None:
    status = await rig.prepare(recipient_name="Sam")
    await rig.bridge.after_input(status)
    cancelled = rig.replies.cancel_message_action(status.action_id)
    await rig.bridge.after_input(cancelled)
    assert rig.dispatcher.current_ticket is None and not rig.dispatcher.is_armed
    assert len(rig.publisher.calls) == 1 and rig.sender.sent == []


async def test_new_body_question_wins_over_late_recipient_question_paint(rig: Rig) -> None:
    recipient = await rig.prepare()
    release = rig.publisher.hold = asyncio.Event()
    pending = asyncio.create_task(rig.bridge.after_input(recipient))
    try:
        await asyncio.wait_for(rig.publisher.started.wait(), timeout=.5)
        old_ticket = rig.dispatcher.current_ticket
        rig.publisher.hold = None
        body = await rig.replies.provide_message_input(recipient.action_id, recipient_name="Sam")
        await rig.bridge.after_input(body)
        current = rig.dispatcher.current_ticket
        assert current is not None and current is not old_ticket and current.field == "body"
        release.set()
        await pending
        assert rig.dispatcher.current_ticket is current and rig.dispatcher.is_armed
        assert len(rig.publisher.calls) == 2
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_external_body_provision_preserves_confirmation_instead_of_repainting_prompt(rig: Rig) -> None:
    status = await rig.prepare(recipient_name="Sam")
    await rig.bridge.after_input(status)
    confirming = await rig.replies.provide_message_input(status.action_id, body="Hello")
    assert confirming.state == "awaiting_confirmation"
    await rig.bridge.after_input(confirming)
    await asyncio.wait_for(rig.confirm.started.wait(), timeout=.5)
    assert rig.dispatcher.current_ticket is None
    assert len(rig.publisher.calls) == 1
    assert rig.confirm.calls == ["Send to Sam? “Hello”"] and rig.sender.sent == []


@pytest.mark.parametrize("failure", ["exception", "no_ack", "rejected"])
async def test_failed_presentation_preserves_action_status_without_arming(rig: Rig, failure: str) -> None:
    status = await rig.prepare(recipient_name="Sam")
    rig.publisher.failure = failure
    await rig.bridge.after_input(status)
    assert rig.replies.message_action_status(status.action_id) is status
    assert rig.dispatcher.current_ticket is None and not rig.dispatcher.is_armed
    assert rig.confirm.calls == [] and rig.sender.sent == []


async def test_publication_deadline_is_contained_and_does_not_consume_the_action(rig: Rig) -> None:
    status = await rig.prepare(recipient_name="Sam")
    rig.publisher.hold = asyncio.Event()
    bridge = MessagePromptBridge(
        rig.replies, rig.dispatcher, publish=rig.publisher,
        connection_generation=lambda: rig.generation[0], publication_timeout_s=.02,
    )
    await asyncio.wait_for(bridge.after_input(status), timeout=.5)
    assert rig.publisher.cancelled == 1
    assert rig.dispatcher.current_ticket is None
    assert rig.replies.message_action_status(status.action_id) is status
    assert await rig.prepare(recipient_name="Sam") is status
    assert rig.contacts.queries == ["Sam"]
    assert rig.confirm.calls == [] and rig.sender.sent == []


async def test_explicit_task_cancellation_revokes_publication_then_propagates(rig: Rig) -> None:
    status = await rig.prepare(recipient_name="Sam")
    rig.publisher.hold = asyncio.Event()
    pending = asyncio.create_task(rig.bridge.after_input(status))
    try:
        await asyncio.wait_for(rig.publisher.started.wait(), timeout=.5)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert rig.dispatcher.current_ticket is None
        assert rig.replies.message_action_status(status.action_id) is status
        assert rig.publisher.cancelled == 1 and rig.sender.sent == []
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_connection_replacement_during_paint_invalidates_the_old_publication(rig: Rig) -> None:
    status = await rig.prepare(recipient_name="Sam")
    release = rig.publisher.hold = asyncio.Event()
    pending = asyncio.create_task(rig.bridge.after_input(status))
    try:
        await asyncio.wait_for(rig.publisher.started.wait(), timeout=.5)
        rig.generation[0] = "replacement-audio-connection"
        release.set()
        await pending
        assert not rig.dispatcher.is_armed
        assert rig.confirm.calls == [] and rig.sender.sent == []
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_missing_trusted_audio_generation_cannot_publish_a_spoken_prompt(rig: Rig) -> None:
    status = await rig.prepare(recipient_name="Sam")
    rig.generation[0] = None
    await rig.bridge.after_input(status)
    assert rig.publisher.calls == [] and rig.dispatcher.current_ticket is None
    assert rig.replies.message_action_status(status.action_id) is status


async def test_copied_or_unknown_status_is_not_an_authoritative_input_step(rig: Rig) -> None:
    status = await rig.prepare(recipient_name="Sam")
    await rig.bridge.after_input(replace(status))
    await rig.bridge.after_input(MessageActionStatus(
        action_id="unknown", state="awaiting_input", message="Say a body.", needs_input="body",
    ))
    assert rig.publisher.calls == [] and rig.dispatcher.current_ticket is None
    assert rig.sender.sent == []


async def test_foreign_ack_returned_to_an_old_publisher_cannot_arm_another_owner(rig: Rig) -> None:
    status = await rig.prepare(recipient_name="Sam")
    release = rig.publisher.hold = asyncio.Event()
    pending = asyncio.create_task(rig.bridge.after_input(status))
    received: list[str] = []

    async def consume(text: str) -> None:
        received.append(text)

    try:
        await asyncio.wait_for(rig.publisher.started.wait(), timeout=.5)
        other = rig.dispatcher.begin_publication(
            owner_kind="terminal", owner_id="other-owner", field="instruction",
            source="glasses", connection_generation="audio-connection-1",
            presentation_binding="other-presentation", ttl_s=45.0,
            is_valid=lambda: True, consume=consume,
        )
        rig.publisher.forced_ack = PromptPresentationAck(
            prompt_id=other.prompt_id, success=True,
            presentation_binding=other.presentation_binding,
            connection_generation=other.connection_generation,
            audio_epoch=other.prompt_id, ingress_fence=100,
        )
        release.set()
        await pending
        assert rig.dispatcher.current_ticket is other
        assert not rig.dispatcher.is_armed
        assert await rig.say("Wrong owner", other) is False
        assert received == [] and rig.sender.sent == []
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


def activation_ack(ticket: PromptTicket) -> PromptPresentationAck:
    return PromptPresentationAck(
        prompt_id=ticket.prompt_id, success=True,
        presentation_binding=ticket.presentation_binding,
        connection_generation=ticket.connection_generation,
        audio_epoch=ticket.prompt_id, ingress_fence=100,
    )


async def test_synchronous_activation_accepts_literal_body_before_publisher_returns(rig: Rig) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    late_activation: list[bool] = []
    questions: list[PromptTicket] = []

    async def publish(
        ticket: PromptTicket, _question: str,
        activate: Callable[[PromptPresentationAck], bool],
    ) -> bool:
        questions.append(ticket)
        ack = activation_ack(ticket)
        activated = activate(ack)
        entered.set()
        await release.wait()
        late_activation.append(activate(ack))
        return activated

    bridge = MessagePromptBridge(
        rig.replies, rig.dispatcher, publish=publish,
        connection_generation=lambda: rig.generation[0],
    )
    status = await rig.prepare(recipient_name="Sam")
    pending = asyncio.create_task(bridge.after_input(status))
    try:
        await asyncio.wait_for(entered.wait(), timeout=.5)
        ticket = rig.dispatcher.current_ticket
        assert ticket is not None and rig.dispatcher.is_armed
        assert not pending.done()
        body = "  Stop,\nopen the door.  "
        assert await rig.say(body, ticket)
        await asyncio.wait_for(rig.confirm.started.wait(), timeout=.5)
        assert rig.confirm.calls == [f"Send to Sam? “{body}”"]
        assert rig.dispatcher.current_ticket is None
        release.set()
        await pending
        assert late_activation == [False]
        assert rig.dispatcher.current_ticket is None and len(questions) == 1
        assert await rig.say(body, ticket) is False
        assert rig.sender.sent == []
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("invalid", ["wrong_binding", "expired", "cancelled"])
async def test_synchronous_activation_rejects_wrong_binding_or_ended_owner(rig: Rig, invalid: str) -> None:
    results: list[bool] = []

    async def publish(
        ticket: PromptTicket, _question: str,
        activate: Callable[[PromptPresentationAck], bool],
    ) -> bool:
        ack = activation_ack(ticket)
        if invalid == "wrong_binding":
            ack = replace(ack, presentation_binding="f" * 64)
        elif invalid == "expired":
            rig.clock.now += 31.0
        else:
            rig.replies.cancel_message_action(ticket.owner_id)
        activated = activate(ack)
        results.append(activated)
        return activated

    bridge = MessagePromptBridge(
        rig.replies, rig.dispatcher, publish=publish,
        connection_generation=lambda: rig.generation[0],
    )
    status = await rig.prepare(recipient_name="Sam")
    await bridge.after_input(status)
    assert results == [False]
    assert rig.dispatcher.current_ticket is None and not rig.dispatcher.is_armed
    assert rig.confirm.calls == [] and rig.sender.sent == []
    if invalid == "wrong_binding":
        assert rig.replies.message_action_status(status.action_id) is status
    else:
        assert rig.replies.message_action_status(status.action_id).state == "cancelled"


@pytest.mark.parametrize("completion", ["own_failure", "successor_failure", "successor_success"])
async def test_synchronous_activation_late_completion_only_retires_its_own_ticket(
    rig: Rig, completion: str,
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    questions: list[PromptTicket] = []

    async def publish(
        ticket: PromptTicket, _question: str,
        activate: Callable[[PromptPresentationAck], bool],
    ) -> bool:
        questions.append(ticket)
        first = len(questions) == 1
        ack = activation_ack(ticket)
        activated = activate(ack)
        if first:
            entered.set()
            await release.wait()
            if completion.endswith("failure"):
                raise OSError("synthetic post-activation publisher failure")
        return activated

    bridge = MessagePromptBridge(
        rig.replies, rig.dispatcher, publish=publish,
        connection_generation=lambda: rig.generation[0],
    )
    status = await rig.prepare(**({"recipient_name": "Sam"} if completion == "own_failure" else {}))
    pending = asyncio.create_task(bridge.after_input(status))
    try:
        await asyncio.wait_for(entered.wait(), timeout=.5)
        original = rig.dispatcher.current_ticket
        assert original is not None and rig.dispatcher.is_armed
        successor: PromptTicket | None = None
        if completion != "own_failure":
            assert await rig.say("Sam", original)
            successor = rig.dispatcher.current_ticket
            assert successor is not None and successor is not original
            assert successor.field == "body" and rig.dispatcher.is_armed
        release.set()
        await pending
        assert rig.dispatcher.current_ticket is successor
        assert await rig.say("Must not replay", original) is False
        if successor is not None:
            assert rig.dispatcher.is_armed and len(questions) == 2
            assert await rig.say("  Hello,\nSam!  ", successor)
            await asyncio.wait_for(rig.confirm.started.wait(), timeout=.5)
            assert rig.confirm.calls == ["Send to Sam? “  Hello,\nSam!  ”"]
        else:
            assert not rig.dispatcher.is_armed and len(questions) == 1
            assert rig.replies.message_action_status(status.action_id) is status
            assert rig.confirm.calls == []
        assert rig.sender.sent == []
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
