"""Bind a Messages UI visit to typed speech and its eventual action receipt."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
import re
import secrets

from ..core.protocol import MessageUIView
from .message_reply import MessageActionStatus, MessageReplyController
from .prompted_input import UtteranceProvenance

log = logging.getLogger(__name__)
_TERMINAL = {"sent", "failed", "cancelled", "uncertain"}


@dataclass(frozen=True, slots=True)
class _Visit:
    request_id: str
    selection_id: str
    generation: str
    is_current: Callable[[], bool]


@dataclass(slots=True)
class _Action:
    visit: _Visit
    action_id: str
    status: MessageActionStatus | None = None


class MessageUIController:
    """Own UI drafts only; conversational tool actions keep their own owner."""

    def __init__(self, replies: MessageReplyController, *,
                 publish: Callable[[MessageUIView], Awaitable[None]],
                 after_input: Callable[[MessageActionStatus], Awaitable[None]],
                 connection_generation: Callable[[], str | None],
                 fence_audio: Callable[[], int], capturing: Callable[[], bool],
                 revoke_prompt: Callable[[str], None]) -> None:
        self._replies = replies
        self._publish = publish
        self._after_input = after_input
        self._generation = connection_generation
        self._fence_audio = fence_audio
        self._capturing = capturing
        self._revoke_prompt = revoke_prompt
        self._visit: _Visit | None = None
        self._action: _Action | None = None
        self._reply_fence: int | None = None
        self._closed = False
        self._tasks: set[asyncio.Task[None]] = set()
        self._start: tuple[_Action, asyncio.Task[None]] | None = None

    def select(self, request_id: str | None, selection_id: str,
               is_current: Callable[[], bool]) -> None:
        self.retire()
        generation = self._generation()
        if self._closed or not request_id or not generation:
            return
        self._visit = _Visit(request_id, selection_id, generation, is_current)

    def conversation_ready(self) -> None:
        visit = self._visit
        if visit is not None and self._current(visit):
            self._reply_fence = self._fence_audio()

    def conversation_on_glass(self) -> bool:
        """The selected conversation is still the page the wearer is on."""
        visit = self._visit
        return visit is not None and self._reply_fence is not None and self._current(visit)

    def matches(self, request_id: str | None, selection_id: str | None) -> bool:
        visit = self._visit
        return visit is not None and (request_id, selection_id) == (visit.request_id, visit.selection_id)

    @property
    def has_visit(self) -> bool:
        return self._visit is not None

    def retire(self) -> None:
        action = self._action
        self._visit = None
        self._action = None
        self._reply_fence = None
        if action is not None:
            self._cancel(action)

    def cancel(self, action_id: str) -> None:
        action = self._action
        if action is not None and action.action_id == action_id:
            # The Hub already dismissed the exact question; retain its visit
            # so a subsequent explicit Reply can start a fresh action.
            self._action = None
            self._cancel(action)

    def _cancel(self, action: _Action) -> None:
        if self._start is not None and self._start[0] is action:
            self._start[1].cancel()
        try:
            self._replies.cancel_message_action(action.action_id)
        except ValueError:
            pass  # Navigation can win before preparation creates the action.
        self._revoke_prompt(action.action_id)

    def _current(self, visit: _Visit) -> bool:
        return (not self._closed and self._visit is visit
                and self._generation() == visit.generation
                and not self._capturing() and visit.is_current())

    async def begin_new(self) -> str:
        return await self._begin(reply=False)

    async def begin_reply(self) -> str:
        if self._reply_fence is None or not self._replies.selected_reply_available():
            return "Select a Messages conversation first."
        return await self._begin(reply=True)

    async def handle_unaddressed_reply(self, text: str, provenance: UtteranceProvenance) -> bool:
        visit = self._visit
        if (visit is None or not self._current(visit) or self._reply_fence is None
                or not self._replies.selected_reply_available()
                or not re.fullmatch(r"\s*(?:reply|respond)[.!?,]*\s*", text, re.I)
                or provenance.source != "glasses" or provenance.mixed
                or provenance.connection_generation != visit.generation
                or any(role.casefold() == "other" for role in provenance.speaker_roles)):
            return False
        first, last = provenance.first_ingress_ordinal, provenance.last_ingress_ordinal
        if (not isinstance(first, int) or isinstance(first, bool)
                or not isinstance(last, int) or isinstance(last, bool)
                or first <= self._reply_fence or last < first):
            return False
        await self.begin_reply()
        return True

    async def _begin(self, *, reply: bool) -> str:
        visit = self._visit
        if visit is None or not self._current(visit):
            if visit is not None and self._capturing():
                return "Wait for the microphone to finish, then reply."
            return "Open Messages and choose a conversation first."
        previous = self._action
        if previous is not None and (previous.status is None or previous.status.state not in _TERMINAL):
            return "Finish or cancel the current message first."
        action = _Action(visit, "messages-ui-" + secrets.token_hex(16))
        self._action = action
        try:
            async def publish_start() -> None:
                await self._publish(MessageUIView(visit.request_id, visit.selection_id,
                    action.action_id, "preparing", "Preparing your message…"))

            start = asyncio.create_task(publish_start())
            self._start = (action, start)
            self._tasks.add(start)
            start.add_done_callback(self._tasks.discard)
            try:
                await asyncio.wait_for(start, 5.0)
            finally:
                if self._start is not None and self._start[0] is action:
                    self._start = None
            if self._action is not action or not self._current(visit):
                return ""
            if reply:
                status = await self._replies.prepare_selected_reply(action_id=action.action_id)
            else:
                status = await self._replies.prepare_new_message(
                    action_id=action.action_id, trace_id=visit.selection_id)
            action.status = status
            if self._action is not action or not self._current(visit):
                self._cancel(action)
                return ""
            await self._after_input(status)
            if self._action is not action or not self._current(visit):
                self._cancel(action)
            # The question owns the HUD; a generic command answer would cover it.
            return ""
        except asyncio.CancelledError:
            stale = self._action is not action or not self._current(visit)
            if self._action is action:
                self._action = None
            self._cancel(action)
            if stale:
                return ""
            raise
        except Exception as exc:
            current = self._action is action and self._current(visit)
            if self._action is action:
                self._action = None
            self._cancel(action)
            log.warning("Messages UI preparation failed error=%s", type(exc).__name__)
            if current and self._current(visit):
                try:
                    await asyncio.wait_for(self._publish(MessageUIView(
                        visit.request_id, visit.selection_id, action.action_id, "failed",
                        "Message preparation could not finish. Open Messages and try again.")), 5.0)
                except Exception:
                    pass  # A failed receipt cannot justify a send or retry.
            return ""

    def on_status(self, status: MessageActionStatus) -> None:
        action = self._action
        if action is None or action.action_id != status.action_id:
            return
        if action.status == status:
            return
        action.status = status
        if status.state not in _TERMINAL or not self._current(action.visit):
            return

        async def publish() -> None:
            if (self._action is not action or not self._current(action.visit)
                    or self._replies.message_action_status(status.action_id) is not status):
                return
            try:
                await asyncio.wait_for(self._publish(MessageUIView(
                    action.visit.request_id, action.visit.selection_id,
                    action.action_id, status.state, status.message)), 5.0)
            except Exception as exc:
                log.warning("Messages UI receipt failed error=%s", type(exc).__name__)

        task = asyncio.create_task(publish())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        self._closed = True
        self.retire()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
