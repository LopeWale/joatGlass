"""Publish scoped message questions and hand exact answers to their owner."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import hashlib
import json
import logging
import math

from ..core.protocol import AudioSource
from .message_reply import MessageActionStatus, MessageReplyController
from .prompted_input import PromptedInputDispatcher, PromptTicket


@dataclass(frozen=True, slots=True)
class PromptPresentationAck:
    prompt_id: str
    success: bool
    presentation_binding: str
    connection_generation: str
    audio_epoch: str
    ingress_fence: int


ActivatePrompt = Callable[[PromptPresentationAck], bool]
PromptPublisher = Callable[[PromptTicket, str, ActivatePrompt], Awaitable[bool]]
log = logging.getLogger(__name__)


class MessagePromptBridge:
    """One controller-held input step, published once with a bounded deadline."""

    def __init__(
        self,
        replies: MessageReplyController,
        dispatcher: PromptedInputDispatcher,
        *,
        publish: PromptPublisher,
        connection_generation: Callable[[], str | None],
        audio_source: Callable[[], AudioSource] = lambda: "glasses",
        ttl_s: float = 45.0,
        publication_timeout_s: float = 5.0,
    ) -> None:
        if not math.isfinite(ttl_s) or ttl_s <= 0:
            raise ValueError("message prompt TTL must be positive and finite")
        if not math.isfinite(publication_timeout_s) or publication_timeout_s <= 0:
            raise ValueError("message prompt publication timeout must be positive and finite")
        self._replies = replies
        self._dispatcher = dispatcher
        self._publish = publish
        self._connection_generation = connection_generation
        self._audio_source = audio_source
        self._ttl_s = ttl_s
        self._publication_timeout_s = min(publication_timeout_s, ttl_s)
        # The controller returns the identical frozen status for a retry and
        # a new instance for a new question, even when its copy is unchanged.
        self._step: MessageActionStatus | None = None
        self._ticket: PromptTicket | None = None

    async def after_input(self, status: MessageActionStatus) -> None:
        """Observe an explicit preparation/input result, never a status poll."""
        try:
            current = self._replies.message_action_status(status.action_id)
        except ValueError:
            return
        if current is not status:
            # A late callback must not replace a newer, even equal-text step.
            return
        field = self._replies.message_prompt_field(status.action_id)
        if field is None:
            if self._ticket is not None and self._ticket.owner_id == status.action_id:
                self._retire(self._ticket)
            return
        if self._step is status:
            return
        generation = self._connection_generation()
        if not isinstance(generation, str) or not generation:
            return

        async def consume(text: str) -> None:
            try:
                if field == "body":
                    following = await self._replies.provide_message_input(status.action_id, body=text)
                else:
                    following = await self._replies.provide_message_input(status.action_id, recipient_name=text)
            except ValueError:
                # This owned answer already spent its lease. A rejected field
                # needs a newly painted question and audio epoch, not a retry
                # of this transcript or renewal from status polling.
                if (
                    self._ticket is ticket
                    and self._dispatcher.current_ticket is None
                    and self._is_valid(status, field, generation)
                ):
                    self._step = None
                    await self.after_input(status)
                return
            await self.after_input(following)

        binding = hashlib.sha256(json.dumps(
            {"owner": "messages", "action_id": status.action_id, "field": field, "question": status.message},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        self._step = status
        ticket = self._dispatcher.begin_publication(
            owner_kind="messages", owner_id=status.action_id, field=field,
            source=self._audio_source(), connection_generation=generation,
            presentation_binding=binding, ttl_s=self._ttl_s,
            is_valid=lambda: self._is_valid(status, field, generation), consume=consume,
        )
        self._ticket = ticket
        activation_open = True
        activated = False

        def activate(acknowledgment: PromptPresentationAck) -> bool:
            nonlocal activated
            # The publisher invokes this at acknowledgment receipt, before
            # dispatching the next audio frame. Its eventual return cannot
            # move this boundary or reactivate a consumed question.
            if (
                not activation_open or activated
                or not isinstance(acknowledgment, PromptPresentationAck)
                or acknowledgment.prompt_id != ticket.prompt_id
                or acknowledgment.presentation_binding != ticket.presentation_binding
                or acknowledgment.connection_generation != ticket.connection_generation
                or acknowledgment.audio_epoch != ticket.prompt_id
                or self._ticket is not ticket
                or self._dispatcher.current_ticket is not ticket
                or not self._is_valid(status, field, generation)
            ):
                return False
            activated = self._dispatcher.acknowledge(
                acknowledgment.prompt_id,
                success=acknowledgment.success,
                presentation_binding=acknowledgment.presentation_binding,
                connection_generation=acknowledgment.connection_generation,
                audio_epoch=acknowledgment.audio_epoch,
                ingress_fence=acknowledgment.ingress_fence,
            )
            return activated

        try:
            published = await asyncio.wait_for(
                self._publish(ticket, status.message, activate), timeout=self._publication_timeout_s,
            )
            if published is not True or not activated:
                self._retire(ticket)
        except asyncio.CancelledError:
            self._retire(ticket)
            raise
        except Exception as exc:
            self._retire(ticket)
            # Keep the successful controller result intact. A publication
            # failure is not a failed draft and is not permission to retry send.
            log.warning("message prompt publication failed error=%s", type(exc).__name__)
        finally:
            activation_open = False

    def _is_valid(self, status: MessageActionStatus, field: str, generation: str) -> bool:
        try:
            return (
                self._connection_generation() == generation
                and self._replies.message_action_status(status.action_id) is status
                and self._replies.message_prompt_field(status.action_id) == field
            )
        except Exception:
            return False

    def _retire(self, ticket: PromptTicket) -> None:
        self._dispatcher.revoke(ticket.prompt_id)
        if self._ticket is ticket:
            self._ticket = None
