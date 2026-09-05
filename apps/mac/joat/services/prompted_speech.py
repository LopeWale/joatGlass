"""Runtime composition for acknowledged, owner-scoped speech input."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import hashlib
import json
import logging
import time

from ..core.protocol import AudioSource, Device
from .hud import HudWriter
from .agent_terminal import AgentTerminalCommands, TerminalCapture
from .message_prompts import ActivatePrompt, MessagePromptBridge, PromptPresentationAck
from .message_reply import MessageActionStatus, MessageReplyController
from .prompted_input import PromptedInputDispatcher, PromptTicket, RetirementReason
from .session import SessionManager, TopicCapture

log = logging.getLogger(__name__)


class PromptedSpeechRuntime:
    """Own the shared prompt dispatcher and its bounded runtime publications."""

    def __init__(
        self, hud: HudWriter, replies: MessageReplyController, *,
        sessions: Callable[[], SessionManager],
        connection_generation: Callable[[], str | None],
        fence_audio: Callable[[], int], capturing: Callable[[], bool],
        audio_source: Callable[[], AudioSource] = lambda: "glasses",
        terminal: Callable[[], AgentTerminalCommands] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        on_message_prompt_retired: Callable[[str], None] | None = None,
    ) -> None:
        self._hud = hud
        self._sessions = sessions
        self._terminal = terminal
        self._connection_generation = connection_generation
        self._fence_audio = fence_audio
        self._capturing = capturing
        self._audio_source = audio_source
        self._monotonic = monotonic
        self._on_message_prompt_retired = on_message_prompt_retired
        self._closed = False
        self._captures: dict[str, tuple[TopicCapture | TerminalCapture, Callable[[], None]]] = {}
        self._publications: dict[str, asyncio.Task[bool]] = {}
        self._cancellations: dict[str, asyncio.Task[None]] = {}
        self.dispatcher = PromptedInputDispatcher(
            monotonic=monotonic, on_retired=self._retired,
        )
        self._messages = MessagePromptBridge(
            replies, self.dispatcher, publish=self._publish,
            connection_generation=self._available_generation,
            audio_source=audio_source,
        )

    async def after_message_input(self, status: MessageActionStatus) -> None:
        """Publish only an explicit controller input step, never a status poll."""
        if not self._closed:
            await self._messages.after_input(status)

    async def publish_topic_capture(self, capture: TopicCapture) -> bool:
        """Bind the real capture owner before requesting its exact question."""
        manager = self._sessions()

        def clear() -> None:
            if manager.current_topic_capture is capture:
                manager.clear_topic_capture()

        async def consume(text: str) -> None:
            manager.consume_topic_capture(capture, text)

        return await self._publish_owned_capture(
            capture, owner_kind="brainstorm", field="topic",
            is_current=lambda: manager.topic_capture_is_current(capture),
            clear=clear, consume=consume,
        )

    async def publish_terminal_capture(self, capture: TerminalCapture) -> bool:
        """Publish a question for one exact Terminal lane and local context."""
        if self._terminal is None:
            return False
        commands = self._terminal()

        def clear() -> None:
            if commands.current_capture is capture:
                commands.clear_capture()

        async def consume(text: str) -> None:
            await commands.consume_capture(capture, text)

        return await self._publish_owned_capture(
            capture, owner_kind="terminal", field="instruction",
            is_current=lambda: commands.capture_is_current(capture),
            clear=clear, consume=consume,
        )

    async def _publish_owned_capture(
        self, capture: TopicCapture | TerminalCapture, *, owner_kind: str, field: str,
        is_current: Callable[[], bool], clear: Callable[[], None],
        consume: Callable[[str], Awaitable[None]],
    ) -> bool:
        generation = self._available_generation()
        if generation is None or not is_current():
            clear()
            return False
        # Preserve the original owner and deadline on duplicate callbacks.
        if any(current is capture for current, _clear in self._captures.values()):
            return False
        remaining = capture.expires_at - self._monotonic()
        if remaining <= 0:
            clear()
            return False

        def valid() -> bool:
            return (
                self._available_generation() == generation
                and is_current()
            )

        binding = hashlib.sha256(json.dumps(
            {"owner": owner_kind, "capture_id": capture.capture_id,
             "field": field, "question": capture.question},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        ticket = self.dispatcher.begin_publication(
            owner_kind=owner_kind, owner_id=capture.capture_id, field=field,
            source=self._audio_source(), connection_generation=generation,
            presentation_binding=binding, ttl_s=remaining,
            is_valid=valid, consume=consume,
        )
        self._captures[ticket.prompt_id] = capture, clear
        activation_open = True
        activated = False

        def activate(ack: PromptPresentationAck) -> bool:
            nonlocal activated
            if (
                not activation_open or activated
                or not isinstance(ack, PromptPresentationAck)
                or ack.prompt_id != ticket.prompt_id
                or self.dispatcher.current_ticket is not ticket
                or not valid()
            ):
                return False
            activated = self.dispatcher.acknowledge(
                ack.prompt_id, success=ack.success,
                presentation_binding=ack.presentation_binding,
                connection_generation=ack.connection_generation,
                audio_epoch=ack.audio_epoch, ingress_fence=ack.ingress_fence,
            )
            return activated

        try:
            published = await asyncio.wait_for(
                self._publish(ticket, capture.question, activate), timeout=min(5.0, remaining),
            )
            if published is not True or not activated:
                self.dispatcher.revoke(ticket.prompt_id)
                return False
            return True
        except asyncio.CancelledError:
            self.dispatcher.revoke(ticket.prompt_id)
            raise
        except Exception as exc:
            self.dispatcher.revoke(ticket.prompt_id)
            log.warning("%s prompt publication failed error=%s", owner_kind, type(exc).__name__)
            return False
        finally:
            activation_open = False

    def handle_device(self, device: Device) -> None:
        """An old question's mic release cannot revoke its successor's input."""
        if device.wearing is False or (device.mic is False and device.mic_prompt_id is None):
            self.revoke_current()
        elif device.mic is False and device.mic_prompt_id is not None:
            self.dispatcher.revoke(device.mic_prompt_id)

    def revoke_current(self) -> None:
        """Synchronously end admission and its exact owner; cleanup is scoped."""
        self.dispatcher.revoke()
        self._sessions().clear_topic_capture()
        # A tap binds its owner synchronously before publication is scheduled.
        # Home/recording/shutdown must also retire that pre-publication owner.
        if self._terminal is not None:
            self._terminal().clear_capture()

    def begin_shutdown(self) -> None:
        """Close admission before cancelling only this runtime's I/O tasks."""
        if self._closed:
            return
        self._closed = True
        self.revoke_current()
        for task in tuple(self._publications.values()):
            if task is not asyncio.current_task():
                task.cancel()

    async def aclose(self) -> None:
        """Drain owned publication and remote cancellation before transport stop."""
        self.begin_shutdown()
        while self._publications or self._cancellations:
            pending = tuple(self._publications.values()) + tuple(self._cancellations.values())
            await asyncio.gather(*pending, return_exceptions=True)
            # A gather of already completed tasks may finish without yielding
            # (Python 3.14). Their removal callbacks can still be queued. Do
            # not spin waiting for callbacks that this loop is starving.
            for owned in (self._publications, self._cancellations):
                for prompt_id, task in tuple(owned.items()):
                    if task.done():
                        owned.pop(prompt_id, None)

    def _available_generation(self) -> str | None:
        if self._closed or self._capturing():
            return None
        generation = self._connection_generation()
        return generation if isinstance(generation, str) and generation else None

    def _ticket_is_current(self, ticket: PromptTicket) -> bool:
        return (
            self._available_generation() == ticket.connection_generation
            and self.dispatcher.current_ticket is ticket
        )

    async def _publish(
        self, ticket: PromptTicket, question: str, activate: ActivatePrompt,
    ) -> bool:
        if not self._ticket_is_current(ticket):
            return False

        def presented(ack: PromptPresentationAck) -> bool:
            # HudWriter calls this in the same stack as the audio fence,
            # before gateway dispatch can receive the following PCM packet.
            return self._ticket_is_current(ticket) and activate(ack)

        task = asyncio.create_task(self._hud.publish_prompt(
            ticket, question, presented, fence_audio=self._fence_audio,
            revoke=lambda prompt_id: self._transport_revoked(ticket, prompt_id),
        ))
        self._publications[ticket.prompt_id] = task

        def finished(completed: asyncio.Task[bool]) -> None:
            if self._publications.get(ticket.prompt_id) is completed:
                del self._publications[ticket.prompt_id]

        task.add_done_callback(finished)
        try:
            return await task
        except asyncio.CancelledError:
            if self._closed or self.dispatcher.current_ticket is not ticket:
                # Cancelling our publication must not cancel its foreign
                # tool/STT caller or alter an already prepared action receipt.
                return False
            raise

    def _transport_revoked(self, ticket: PromptTicket, prompt_id: str) -> None:
        if prompt_id == ticket.prompt_id:
            # A late receipt for a consumed/replaced prompt cannot touch its
            # successor or cancel the typed owner's admitted operation.
            self.dispatcher.revoke(prompt_id)

    def _retired(self, ticket: PromptTicket, reason: RetirementReason) -> None:
        capture = self._captures.pop(ticket.prompt_id, None)
        if reason != "consumed":
            if ticket.owner_kind == "messages" and self._on_message_prompt_retired is not None:
                self._on_message_prompt_retired(ticket.owner_id)
            if capture is not None:
                _owner, clear = capture
                clear()
            publication = self._publications.get(ticket.prompt_id)
            if publication is not None and publication is not asyncio.current_task():
                publication.cancel()
        # All exits remove the question from the lenses. Consumed exits
        # deliberately preserve the owner until its consumer runs next.
        if ticket.prompt_id in self._cancellations:
            return

        async def cancel_remote() -> None:
            try:
                await asyncio.wait_for(self._hud.cancel_prompt(ticket.prompt_id), timeout=.5)
            except Exception as exc:
                log.warning("prompt cleanup failed error=%s", type(exc).__name__)

        cancellation = asyncio.create_task(cancel_remote())
        self._cancellations[ticket.prompt_id] = cancellation

        def cancelled(completed: asyncio.Task[None]) -> None:
            if self._cancellations.get(ticket.prompt_id) is completed:
                del self._cancellations[ticket.prompt_id]

        cancellation.add_done_callback(cancelled)
