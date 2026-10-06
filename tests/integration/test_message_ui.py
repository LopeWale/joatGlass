"""Messages menu -> acknowledged input -> controlled confirmation/outcome.

All contacts, transcripts and submissions are synthetic. No live Messages I/O.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from joat.channels.cards import CardLog
from joat.channels.imessage import MessageEntry, MessageHistoryPage, MessageThread
from joat.core.interfaces import Transcript
from joat.core.protocol import InputEvent
from joat.services.capabilities import CapabilityRouter
from joat.services.hud import HudWriter
from joat.services.message_reply import MessageReplyController
from joat.services.message_ui import MessageUIController
from joat.services.prompted_input import UtteranceProvenance
from joat.services.prompted_speech import PromptedSpeechRuntime


async def until(predicate: Any) -> None:
    async with asyncio.timeout(0.5):
        while not predicate():
            await asyncio.sleep(0)


class Transport:
    def __init__(self) -> None:
        self.sent: list[Any] = []
        self.questions: list[Any] = []
        self.hold: asyncio.Event | None = None
        self.hold_entered = asyncio.Event()
        self.hold_exited = asyncio.Event()

    async def send(self, message: Any) -> None:
        if self.hold is not None and message.to_wire()['kind'] == 'message.ui':
            self.hold_entered.set()
            try:
                await self.hold.wait()
            finally:
                self.hold_exited.set()
        self.sent.append(message)

    async def request_prompt(self, request: Any, *, on_presented: Any,
                             on_revoked: Any = None, timeout_s: float = 5.0) -> bool:
        self.questions.append(request)
        return on_presented(SimpleNamespace(
            prompt_id=request.prompt_id, success=True,
            presentation_binding=request.presentation_binding,
            connection_generation=request.connection_generation,
            audio_epoch=request.prompt_id,
        ))

    async def cancel_prompt(self, prompt_id: str | None = None) -> bool:
        return True


class Messages:
    available = True
    def recent_threads(self, limit: int) -> list[MessageThread]:
        return [MessageThread('Sam', 'synthetic-contact', 'Are you coming?', 1.0)]

    def thread_message_page(self, thread: MessageThread, **kwargs: Any) -> MessageHistoryPage:
        return MessageHistoryPage((MessageEntry(thread.text, thread.at, False, row_id=1),), False)


class Rig:
    def __init__(self, *, contact_search: Callable[[str], list[tuple[str, str]]] | None = None) -> None:
        self.transport = Transport()
        #: Bodies the controller reported as sent, in order.
        self.sent_bodies: list[str] = []
        self.generation = 'ui-connection'
        self.fence = 10
        self.capturing = False
        self.now = 1_000.0
        self.submitted: list[tuple[str, str]] = []
        self.confirmations: list[str] = []
        self.confirm_bindings: list[dict[str, Any]] = []
        self.confirm_release = asyncio.Event()
        self.confirm_result = False
        rig = self

        class Sender:
            async def send(self, recipient: str, body: str) -> None:
                rig.submitted.append((recipient, body))

        async def confirm(_action: str, text: str, *args: Any, **kwargs: Any) -> bool:
            self.confirmations.append(text)
            self.confirm_bindings.append(kwargs)
            await self.confirm_release.wait()
            return self.confirm_result

        seams = {'begin_reply': lambda: self.ui.begin_reply(),
                 'on_status': lambda status: self.ui.on_status(status),
                 'selection_on_glass': lambda: self.ui.conversation_on_glass()}
        self.replies = MessageReplyController(Sender(), confirm, monotonic=lambda: self.now,
            contact_search=contact_search or (
                lambda name: [('Sam', 'synthetic-contact')] if name == 'Sam' else []),
            **seams)
        self.hud = HudWriter(self.transport)
        self.runtime = PromptedSpeechRuntime(self.hud, self.replies,
            sessions=lambda: SimpleNamespace(clear_topic_capture=lambda: None),
            connection_generation=lambda: self.generation,
            fence_audio=lambda: self.fence, capturing=lambda: self.capturing,
            on_message_prompt_retired=lambda action_id: self.ui.cancel(action_id))
        self.ui = MessageUIController(self.replies,
                publish=self.transport.send, after_input=self.runtime.after_message_input,
                connection_generation=lambda: self.generation,
                fence_audio=lambda: self.fence, capturing=lambda: self.capturing,
                revoke_prompt=lambda action_id: self.runtime.dispatcher.revoke(self.runtime.dispatcher.current_ticket.prompt_id)
                if self.runtime.dispatcher.current_ticket is not None
                and self.runtime.dispatcher.current_ticket.owner_id == action_id else None,
                on_sent=self.on_sent)
        self.router = CapabilityRouter(self.hud, self.transport,
            SimpleNamespace(get=lambda key: ''), Messages(), CardLog(), replies=self.replies,
            message_ui=self.ui)

    async def select(self, index: int = 0) -> None:
        await self.router.open('Messages', request_id='request-messages')
        self.tap(index)

    def tap(self, index: int = 0, trace: str = 'selection-message') -> None:
        self.router.on_input(InputEvent(container_name='capdata', selected_name='MESSAGES',
            selected_index=index, request_id='request-messages', trace_id=trace))

    def tap_thread(self, index: int = 1) -> None:
        # Any tap on the conversation replies; the Hub's row index is ignored.
        assert self.router._message_history is not None
        self.router.on_input(InputEvent(container_name='capmessage', selected_name='MESSAGES',
            selected_index=index, request_id='request-messages', trace_id='selection-message'))

    async def on_sent(self, body: str) -> None:
        # Production wires this to the router: the thread repaints with the reply.
        self.sent_bodies.append(body)
        await self.router.show_sent_reply(body)

    def receipts(self) -> list[str]:
        return [m.to_wire().get('text', '') for m in self.transport.sent
                if m.to_wire().get('presentation') == 'receipt']

    def home(self) -> None:
        self.router.on_input(InputEvent(container_name='menu', selected_name='closed'))

    async def answer(self, text: str) -> bool:
        question = self.transport.questions[-1]
        return await self.runtime.dispatcher.dispatch(Transcript(text, True), self.provenance(question.prompt_id))

    def provenance(self, epoch: str | None = None) -> UtteranceProvenance:
        return UtteranceProvenance('glasses', self.generation, epoch, 11, 12, frozenset({'self'}))

    async def close(self) -> None:
        if self.ui is not None:
            self.ui.retire()
            await asyncio.wait_for(self.ui.aclose(), 1)
        self.confirm_release.set()
        await asyncio.wait_for(self.runtime.aclose(), 1)
        await asyncio.wait_for(self.replies.aclose(), 1)


@pytest.mark.asyncio
async def test_new_message_row_publishes_recipient_then_exact_body_question() -> None:
    rig = Rig()
    try:
        await rig.select()
        await until(lambda: rig.transport.questions)
        assert rig.transport.questions[-1].field == 'recipient'
        assert not rig.replies.is_compose_armed()
        assert await rig.answer('Sam')
        assert rig.transport.questions[-1].field == 'body'
        body = '  Good morning! Stop, open News?!  '
        assert await rig.answer(body)
        await until(lambda: rig.confirmations)
        assert body in rig.confirmations[0]
        assert rig.submitted == []
        rig.confirm_release.set()
        await until(lambda: any(m.to_wire().get('state') == 'cancelled' for m in rig.transport.sent))
        outcomes = [m.to_wire() for m in rig.transport.sent if m.to_wire()['kind'] == 'message.ui']
        assert outcomes[0]['state'] == 'preparing'
        assert outcomes[-1]['state'] == 'cancelled'
        assert all(m['selectionId'] == 'selection-message' for m in outcomes)
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_home_before_queued_new_message_cannot_arm_legacy_or_typed_draft() -> None:
    rig = Rig()
    try:
        await rig.select()
        rig.home()
        for _ in range(20):
            await asyncio.sleep(0)
        assert not rig.replies.is_compose_armed()
        assert rig.transport.questions == []
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_selected_conversation_reply_uses_fresh_acknowledged_body() -> None:
    rig = Rig()
    try:
        await rig.select(1)
        await until(lambda: rig.replies.selected_reply_available())
        await until(lambda: rig.ui._reply_fence is not None)
        assert await rig.ui.handle_unaddressed_reply('  Reply?!  ', rig.provenance())
        assert rig.transport.questions[-1].field == 'body'
        body = '  Good morning. Keep  both spaces!  '
        assert await rig.answer(body)
        await until(lambda: rig.confirmations)
        assert body in rig.confirmations[0]
        assert rig.submitted == []
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_thread_tap_after_long_reading_still_asks_for_the_body() -> None:
    # The reply clock ran from the moment the conversation opened, so after
    # two minutes of reading a tap on the thread silently did nothing.
    rig = Rig()
    try:
        await rig.select(1)
        await until(lambda: rig.ui._reply_fence is not None)
        rig.now += 300.0
        asked = len(rig.transport.questions)
        rig.tap_thread()
        await until(lambda: len(rig.transport.questions) > asked)
        assert rig.transport.questions[-1].field == 'body'
        assert rig.transport.questions[-1].question == 'What do you want to say back to Sam?'
        assert rig.receipts() == []
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_off_the_glass_the_conversation_still_expires() -> None:
    rig = Rig()
    try:
        await rig.select(1)
        await until(lambda: rig.ui._reply_fence is not None)
        rig.ui.retire()
        rig.now += 300.0
        assert not rig.replies.selected_reply_available()
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_a_refused_thread_tap_says_why_on_the_glass() -> None:
    rig = Rig()
    try:
        await rig.select(1)
        await until(lambda: rig.ui._reply_fence is not None)
        rig.capturing = True
        asked = len(rig.transport.questions)
        rig.tap_thread()
        await until(lambda: rig.receipts())
        assert rig.receipts() == ['Wait for the microphone to finish, then reply.']
        assert len(rig.transport.questions) == asked
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_a_forged_tap_on_a_read_only_group_asks_nothing_and_sends_nothing() -> None:
    # A group without a Mac-side chat target is read-only: the page is not
    # selectable, and a forged tap cannot arm a reply to one participant.
    rig = Rig()

    class GroupOnly(Messages):
        def recent_threads(self, limit: int) -> list[MessageThread]:
            return [MessageThread('Project team', 'synthetic-group-participant', 'Group', 2.0,
                                  is_group=True)]

    rig.router._messages = GroupOnly()
    try:
        await rig.select(1)
        await until(lambda: rig.router._message_history is not None)
        await until(lambda: any(getattr(m, 'title', None) == 'MESSAGES · CONVERSATION'
                                for m in rig.transport.sent))
        frame = [m for m in rig.transport.sent if getattr(m, 'title', None) == 'MESSAGES · CONVERSATION'][-1]
        assert frame.selectable is False
        assert 'read-only' in (frame.hint or '')
        rig.tap_thread(0)
        await until(lambda: rig.receipts())
        assert rig.receipts() == ['This conversation is read-only on the glasses.']
        assert not rig.replies.selected_reply_available()
        assert rig.transport.questions == []
        assert rig.confirmations == [] and rig.submitted == []
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_retry_after_a_cancelled_draft_asks_the_question_again() -> None:
    rig = Rig()
    try:
        await rig.select(1)
        await until(lambda: rig.ui._reply_fence is not None)
        rig.tap_thread()
        await until(lambda: rig.transport.questions)
        first = rig.transport.questions[-1]
        assert first.field == 'body'
        assert await rig.answer('Running late')
        await until(lambda: rig.confirmations)
        # Long-press on the draft: the Hub declines its confirmation itself.
        rig.router.on_input(InputEvent(container_name='message-retry', selected_name=first.owner_id,
            request_id='request-messages', trace_id='selection-message'))
        rig.confirm_release.set()
        await until(lambda: len(rig.transport.questions) > 1)
        again = rig.transport.questions[-1]
        assert again.field == 'body'
        assert again.question == first.question == 'What do you want to say back to Sam?'
        assert again.owner_id != first.owner_id
        assert rig.replies.message_action_status(first.owner_id).state == 'cancelled'
        assert rig.submitted == [] and rig.sent_bodies == []
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_retry_for_another_or_unknown_draft_asks_nothing() -> None:
    rig = Rig()
    try:
        await rig.select(1)
        await until(lambda: rig.ui._reply_fence is not None)
        rig.tap_thread()
        await until(lambda: rig.transport.questions)
        assert await rig.answer('Running late')
        await until(lambda: rig.confirmations)
        rig.ui.retry('messages-ui-forged')
        # A retry from another selection is not this visit's.
        rig.router.on_input(InputEvent(container_name='message-retry',
            selected_name=rig.transport.questions[-1].owner_id,
            request_id='request-messages', trace_id='another-selection'))
        rig.confirm_release.set()
        await until(lambda: any(m.to_wire().get('state') == 'cancelled' for m in rig.transport.sent))
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(rig.transport.questions) == 1
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_a_sent_reply_reports_its_body_and_shows_on_the_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import joat.services.capabilities as capabilities
    # The synthetic chat.db never records the send: the thread falls back to it.
    monkeypatch.setattr(capabilities, '_SENT_REPLY_WAIT_S', 0.0)
    rig = Rig()
    try:
        rig.confirm_result = True
        await rig.select(1)
        await until(lambda: rig.ui._reply_fence is not None)
        rig.tap_thread()
        await until(lambda: rig.transport.questions)
        body = '  On my way!  '
        assert await rig.answer(body)
        await until(lambda: rig.confirmations)
        assert rig.sent_bodies == []
        rig.confirm_release.set()
        await until(lambda: rig.sent_bodies)
        assert rig.sent_bodies == [body]
        assert rig.submitted == [('synthetic-contact', body)]
        await until(lambda: [m for m in rig.transport.sent
                             if getattr(m, 'title', None) == 'MESSAGES · CONVERSATION'][-1].items[1]
                    .startswith('You: '))
        frame = [m for m in rig.transport.sent if getattr(m, 'title', None) == 'MESSAGES · CONVERSATION'][-1]
        assert frame.items == ['1/1 · Sam', f'You: {body}\n', 'Sam: Are you coming?']
        assert frame.reader_page == ('selection-message', 0, 1)
        assert frame.request_id == 'request-messages'
    finally:
        await rig.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [
    {'source': 'phone'}, {'mixed': True}, {'connection_generation': 'old'},
    {'first_ingress_ordinal': 10}, {'last_ingress_ordinal': 9},
    {'speaker_roles': frozenset({'self', 'other'})},
])
async def test_reply_does_not_admit_audio_outside_selected_visit(change: dict[str, Any]) -> None:
    rig = Rig()
    try:
        await rig.select(1)
        await until(lambda: rig.ui._reply_fence is not None)
        assert not await rig.ui.handle_unaddressed_reply('reply', replace(rig.provenance(), **change))
        assert not await rig.ui.handle_unaddressed_reply('reply with send this now', rig.provenance())
        assert rig.transport.questions == []
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_home_while_start_receipt_is_held_prevents_preparation() -> None:
    rig = Rig()
    try:
        hold = rig.transport.hold = asyncio.Event()
        await rig.select()
        await until(lambda: rig.ui._action is not None)
        rig.home()
        hold.set()
        for _ in range(20):
            await asyncio.sleep(0)
        assert rig.transport.questions == []
        assert rig.replies._active_action is None
    finally:
        await rig.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('departure', ['home', 'new_selection'])
async def test_departure_during_recipient_lookup_cannot_publish_body_or_confirm(
    departure: str,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def contact_search(name: str) -> list[tuple[str, str]]:
        entered.set()
        try:
            if not release.wait(timeout=1):
                raise TimeoutError('Controlled contact lookup was not released')
            assert name == 'Sam'
            return [('Sam', 'synthetic-contact')]
        finally:
            finished.set()

    rig = Rig(contact_search=contact_search)
    answer: asyncio.Task[bool] | None = None
    try:
        await rig.select()
        await until(lambda: rig.runtime.dispatcher.is_armed)
        ticket = rig.runtime.dispatcher.current_ticket
        assert ticket is not None
        assert rig.transport.questions[-1].field == 'recipient'
        answer = asyncio.create_task(rig.answer('Sam'))
        await until(entered.is_set)
        assert not answer.done()

        if departure == 'home':
            rig.home()
        else:
            await rig.router.open('Messages', request_id='request-messages')
            rig.tap(1, trace='following-conversation')
            await until(lambda: rig.ui._reply_fence is not None)
            assert rig.ui.matches('request-messages', 'following-conversation')
        assert rig.replies.message_action_status(ticket.owner_id).state == 'cancelled'

        release.set()
        assert await asyncio.wait_for(answer, 1)
        await until(finished.is_set)
        assert [question.field for question in rig.transport.questions] == ['recipient']
        assert rig.runtime.dispatcher.current_ticket is None
        assert rig.replies.message_action_status(ticket.owner_id).state == 'cancelled'
        assert rig.confirmations == []
        assert rig.submitted == []
    finally:
        release.set()
        try:
            if answer is not None:
                if not answer.done():
                    answer.cancel()
                await asyncio.wait_for(asyncio.gather(answer, return_exceptions=True), 1)
            if entered.is_set():
                await until(finished.is_set)
        finally:
            await rig.close()


@pytest.mark.asyncio
async def test_close_drains_held_preparing_publication_and_returns_silent_result() -> None:
    rig = Rig()
    caller: asyncio.Task[str] | None = None
    hold = rig.transport.hold = asyncio.Event()
    try:
        await rig.select(1)
        await until(lambda: rig.ui._reply_fence is not None)
        caller = asyncio.create_task(rig.ui.begin_reply())
        await asyncio.wait_for(rig.transport.hold_entered.wait(), 1)
        assert rig.ui._start is not None
        owned_io = rig.ui._start[1]
        assert owned_io in rig.ui._tasks and not owned_io.done()

        await asyncio.wait_for(rig.ui.aclose(), 1)
        assert owned_io.done()
        assert rig.transport.hold_exited.is_set()
        assert not hold.is_set(), 'Shutdown must drain the held send without releasing it'
        assert await asyncio.wait_for(caller, 1) == ''
        assert rig.ui._start is None
        assert not rig.ui._tasks
        assert rig.replies._active_action is None
        assert rig.transport.questions == []
        assert not any(m.to_wire()['kind'] == 'message.ui' for m in rig.transport.sent)
        assert rig.confirmations == []
        assert rig.submitted == []
    finally:
        hold.set()
        try:
            if caller is not None:
                if not caller.done():
                    caller.cancel()
                await asyncio.wait_for(asyncio.gather(caller, return_exceptions=True), 1)
        finally:
            await rig.close()


@pytest.mark.asyncio
async def test_preparing_publication_failure_after_home_returns_no_stale_answer() -> None:
    rig = Rig()
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    caller: asyncio.Task[str] | None = None
    original_publish = rig.ui._publish

    async def controlled_publish(message: Any) -> None:
        if message.to_wire().get('state') != 'preparing':
            await original_publish(message)
            return
        entered.set()
        deadline = asyncio.get_running_loop().time() + 1
        try:
            while not release.is_set():
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError('Controlled preparing publication was not released')
                try:
                    await asyncio.wait_for(release.wait(), remaining)
                except asyncio.CancelledError:
                    # Model a send which can settle after cancellation, including
                    # repeated cancellations during owner teardown.
                    cancelled.set()
            raise RuntimeError('Controlled late preparation failure')
        finally:
            finished.set()

    rig.ui._publish = controlled_publish
    try:
        await rig.select(1)
        await until(lambda: rig.ui._reply_fence is not None)
        caller = asyncio.create_task(rig.ui.begin_reply())
        await asyncio.wait_for(entered.wait(), 1)
        rig.home()
        await asyncio.wait_for(cancelled.wait(), 1)
        assert not caller.done()
        release.set()

        assert await asyncio.wait_for(caller, 1) == ''
        assert finished.is_set()
        assert not any(m.to_wire()['kind'] == 'message.ui' for m in rig.transport.sent)
        assert rig.transport.questions == []
        assert rig.replies._active_action is None
        assert rig.confirmations == []
        assert rig.submitted == []
    finally:
        release.set()
        try:
            if caller is not None:
                if not caller.done():
                    caller.cancel()
                await asyncio.wait_for(asyncio.gather(caller, return_exceptions=True), 1)
        finally:
            await rig.close()


@pytest.mark.asyncio
async def test_duplicate_tap_keeps_action_and_prompt_lease() -> None:
    rig = Rig()
    try:
        await rig.select()
        await until(lambda: rig.runtime.dispatcher.is_armed)
        ticket = rig.runtime.dispatcher.current_ticket
        rig.tap()
        for _ in range(20):
            await asyncio.sleep(0)
        assert rig.runtime.dispatcher.current_ticket is ticket
        assert len(rig.transport.questions) == 1
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_prompt_back_retires_only_its_ui_action_and_can_start_again() -> None:
    rig = Rig()
    try:
        await rig.select(1)
        await until(lambda: rig.ui._reply_fence is not None)
        await rig.ui.begin_reply()
        ticket = rig.runtime.dispatcher.current_ticket
        assert ticket is not None
        rig.runtime.dispatcher.revoke(ticket.prompt_id)
        assert rig.replies.message_action_status(ticket.owner_id).state == 'cancelled'
        await rig.ui.begin_reply()
        following = rig.runtime.dispatcher.current_ticket
        assert following is not None and following.owner_id != ticket.owner_id
        rig.ui.cancel(ticket.owner_id)
        assert rig.runtime.dispatcher.current_ticket is following
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_home_during_confirmation_never_sends_or_publishes_stale_outcome() -> None:
    rig = Rig()
    try:
        rig.confirm_result = True
        await rig.select()
        await until(lambda: rig.runtime.dispatcher.is_armed)
        await rig.answer('Sam')
        await rig.answer('Hello')
        await until(lambda: rig.confirmations)
        rig.home()
        rig.confirm_release.set()
        for _ in range(30):
            await asyncio.sleep(0)
        assert rig.submitted == []
        assert not any(m.to_wire().get('state') in ('sent', 'cancelled') for m in rig.transport.sent)
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_submitted_outcome_is_explicitly_delivery_unverified() -> None:
    rig = Rig()
    try:
        rig.confirm_result = True
        await rig.select()
        await until(lambda: rig.runtime.dispatcher.is_armed)
        await rig.answer('Sam')
        await rig.answer('  Hello!  ')
        await until(lambda: rig.confirmations)
        rig.confirm_release.set()
        await until(lambda: any(m.to_wire().get('state') == 'sent' for m in rig.transport.sent))
        assert rig.submitted == [('synthetic-contact', '  Hello!  ')]
        outcome = [m.to_wire() for m in rig.transport.sent if m.to_wire().get('state') == 'sent'][-1]
        assert 'delivery' in outcome['text'].lower()
        assert 'unverified' in outcome['text'].lower()
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_ui_confirmation_carries_its_action_owner_beside_the_send_identity() -> None:
    rig = Rig()
    try:
        await rig.select()
        await until(lambda: rig.runtime.dispatcher.is_armed)
        ticket = rig.runtime.dispatcher.current_ticket
        assert ticket is not None
        await rig.answer('Sam')
        await rig.answer('Hello')
        await until(lambda: rig.confirmations)
        assert rig.confirm_bindings[0].get('message_ui_action_id') == ticket.owner_id
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_tagged_home_after_picker_back_retires_the_remaining_row_mapping() -> None:
    rig = Rig()
    try:
        await rig.select(1)
        await until(lambda: rig.ui._reply_fence is not None)
        for destination in ('messages-list', 'messages-home'):
            rig.router.on_input(InputEvent(container_name='capability-lifecycle',
                selected_name=destination, request_id='request-messages', trace_id='selection-message'))
        assert rig.router._message_rows == []
    finally:
        await rig.close()


@pytest.mark.parametrize('next_view', ['group', 'direct', 'home'])
async def test_group_read_disarms_previous_reply_and_cannot_rearm_after_navigation(next_view: str) -> None:
    import threading
    rig = Rig()
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    class GroupMessages(Messages):
        def recent_threads(self, limit: int) -> list[MessageThread]:
            return [MessageThread('Sam', 'synthetic-direct', 'Direct', 1.0),
                    MessageThread('Project team', 'synthetic-group-participant', 'Group', 2.0,
                                  replyable=False)]

        def thread_message_page(self, thread: MessageThread, **kwargs: Any) -> MessageHistoryPage:
            if not thread.replyable:
                started.set()
                try:
                    assert release.wait(1), 'test must release its held group read'
                finally:
                    finished.set()
            return super().thread_message_page(thread, **kwargs)

    rig.router._messages = GroupMessages()
    try:
        await rig.select(1)
        await until(rig.replies.selected_reply_available)
        rig.tap(2, 'group-selection')
        # This must be synchronous with selection ingress, before database I/O.
        assert not rig.replies.selected_reply_available()
        await until(started.is_set)
        assert not await rig.ui.handle_unaddressed_reply('Reply', rig.provenance())
        assert 'select' in (await rig.replies.handle_command('reply saying hello') or '').lower()
        if next_view == 'direct':
            rig.tap(1, 'new-direct-selection')
            await until(rig.replies.selected_reply_available)
        elif next_view == 'home':
            rig.home()
        release.set()
        await until(finished.is_set)
        # Wait for the queued production selection coroutine to consume the result.
        async with asyncio.timeout(0.5):
            while any(not t.done() and '_select_capability_row' in t.get_coro().__qualname__
                      for t in asyncio.all_tasks()):
                await asyncio.sleep(0)
        if next_view == 'group':
            assert rig.router._message_history.thread.sender == 'Project team'
            assert not rig.replies.selected_reply_available()
            assert not await rig.ui.handle_unaddressed_reply('Reply', rig.provenance())
        elif next_view == 'direct':
            assert rig.router._message_history.thread.sender == 'Sam'
            assert rig.replies.selected_reply_available()
        else:
            assert rig.router._message_history is None
            assert not rig.replies.selected_reply_available()
        assert rig.submitted == []
        assert rig.confirmations == []
        assert rig.transport.questions == []
    finally:
        release.set()
        await rig.close()
