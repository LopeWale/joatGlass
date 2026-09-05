"""Voice draft and fail-closed confirmation for a selected Messages thread."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from ..channels.contacts import ContactResolver
from ..channels.imessage import MessageThread
from ..channels.message_sender import MessageSendError
from ..core.interfaces import MessageSender
from ..core.confirmation import ConfirmationResult
from ..core.protocol import CONFIRM_PROMPT_MAX_UTF16_UNITS, utf16_units
from ..persistence.actions import (
    ActionConflictError,
    ActionDraft,
    ActionJournal,
    ConfirmationEvidence,
)

log = logging.getLogger(__name__)

Confirm = Callable[..., Awaitable[bool | ConfirmationResult | None]]
# Whisper punctuates freely on short utterances ("Reply, saying on my way.").
# Optional commas/colons around the verbs keep the grammar deaf-proof.
_REPLY = re.compile(
    r"^(?:reply|respond)[,:]?(?:\s+(?:saying|with)[,:]?)?\s+(?P<body>.+)$",
    re.IGNORECASE,
)
# The conversation footer teaches exactly "say reply" — the bare verb must do
# something: it arms a short two-turn flow whose next utterance is the body.
_BARE_REPLY = re.compile(
    r"^(?:reply|respond)(?:\s+(?:to\s+)?(?:this|that|them|him|her))?$",
    re.IGNORECASE,
)
_NEW_MESSAGE = re.compile(
    r"^message[,:]?(?:\s+to)?\s+(?P<recipient>.+?)[,:]?\s+(?:saying|with)[,:]?\s+(?P<body>.+)$",
    re.IGNORECASE,
)
ContactSearch = Callable[[str], list[tuple[str, str]]]
# The plugin begins its eight-second decision clock only after the complete
# frame is visible. The gateway envelope also has to include that BLE paint.
CONFIRM_TRANSPORT_SECONDS = 12.0
MESSAGE_SELECTION_TTL_SECONDS = 120.0
# A bare "reply" prompt stays armed briefly; the wearer answers in one breath.
REPLY_PROMPT_TTL_SECONDS = 60.0
CONTACT_LOOKUP_TIMEOUT_SECONDS = 3.0
MAX_PREPARED_ACTIONS = 256
MAX_RECIPIENT_CANDIDATES = 20
_RELATIONSHIP = re.compile(r"^(?:(?:my|our)\s+)?(?:wife|husband|spouse|partner)$", re.I)

MessageActionState = Literal[
    "awaiting_input", "awaiting_confirmation", "sending", "cancelled", "sent", "failed", "uncertain",
]


@dataclass(frozen=True, slots=True)
class MessageRecipientCandidate:
    """An action-scoped contact choice; handles never come from model arguments."""

    reference: str
    label: str


@dataclass(frozen=True, slots=True)
class MessageActionStatus:
    """Observed preparation/send state; sent means submission, not delivery."""

    action_id: str
    state: MessageActionState
    message: str
    recipient_name: str | None = None
    needs_input: Literal["recipient", "body"] | None = None
    candidates: tuple[MessageRecipientCandidate, ...] = ()
    verification_level: Literal["delivery_unverified"] | None = None


@dataclass(slots=True)
class _PreparedMessage:
    fingerprint: str
    status: MessageActionStatus
    trace_id: str
    origin_kind: str
    expires_at: float
    body: str | None = None
    body_digest: str | None = None
    target_digest: str | None = None
    recipient: MessageThread | None = None
    candidates: dict[str, MessageThread] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None
    expiry: asyncio.TimerHandle | None = None
    resolving: bool = False
    cancel_requested: bool = False
    send_started: bool = False
    journal_id: str | None = None
    attempt_id: str | None = None


@dataclass(frozen=True, slots=True)
class _MessageSelection:
    thread: MessageThread
    trace_id: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class _PendingReply:
    """A bare "reply" was spoken; the next utterance is the message body."""

    trace_id: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class _ComposeSelection:
    trace_id: str
    expires_at: float
    recipient: MessageThread | None = None


def _target_bytes(target: tuple[str, str]) -> bytes:
    return json.dumps(
        {"kind": target[0], "id": target[1]},
        ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")


def _target_digest(thread: MessageThread) -> str | None:
    target = thread.reply_target
    if target is None:
        return None
    return hashlib.sha256(b"joat-message-target-v2\0" + _target_bytes(target)).hexdigest()


class MessageReplyController:
    """One selected conversation; one explicitly confirmed send at a time."""

    def __init__(
        self,
        sender: MessageSender,
        confirm: Confirm,
        journal: ActionJournal | None = None,
        *,
        selection_ttl_s: float = MESSAGE_SELECTION_TTL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        contact_search: ContactSearch | None = None,
        begin_reply: Callable[[], Awaitable[str]] | None = None,
        on_status: Callable[[MessageActionStatus], None] | None = None,
    ) -> None:
        if selection_ttl_s <= 0:
            raise ValueError("message selection TTL must be positive")
        self._sender = sender
        self._confirm = confirm
        self._journal = journal
        self._selection_ttl_s = selection_ttl_s
        self._monotonic = monotonic
        self._pending_reply: _PendingReply | None = None
        self._contact_search = contact_search or ContactResolver().search
        self._begin_reply = begin_reply
        self._on_status = on_status
        self._selection: _MessageSelection | None = None
        self._compose: _ComposeSelection | None = None
        self._draft_lock = asyncio.Lock()
        self._prepared: dict[str, _PreparedMessage] = {}
        self._active_action: str | None = None
        self._closed = False

    async def prepare_new_message(
        self,
        *,
        action_id: str,
        trace_id: str,
        recipient_name: str | None = None,
        body: str | None = None,
    ) -> MessageActionStatus:
        """Bind a new-message request, returning before confirmation or send."""
        recipient_name = self._validated_name(recipient_name)
        action, fresh = self._new_action(
            action_id, ["compose", trace_id, recipient_name, body], trace_id,
            "messages.compose", body,
        )
        if fresh and action.status.state == "awaiting_input":
            if recipient_name:
                await self._resolve_prepared_recipient(action, recipient_name)
            self._advance_preparation(action)
        return action.status

    async def prepare_selected_reply(
        self, *, action_id: str, body: str | None = None,
    ) -> MessageActionStatus:
        """Bind the current, unexpired conversation without accepting a handle."""
        selection = self._selection
        previous = self._prepared.get(action_id)
        # A completed action can still return its receipt after selection was
        # cleared. With a selection present, a different conversation is a
        # different request even when its label, latest sender and body match.
        target_digest = (
            _target_digest(selection.thread) if selection is not None
            else previous.target_digest if previous is not None else None
        )
        action, fresh = self._new_action(
            action_id, ["reply", body, target_digest], selection.trace_id if selection else "conversation",
            "messages.selection", body,
        )
        if fresh:
            action.target_digest = target_digest
        if not fresh or action.status.state != "awaiting_input":
            return action.status
        if selection is None:
            return self._set_status(action, "awaiting_input", "Select a Messages conversation first.", needs_input="recipient")
        if self._monotonic() > selection.expires_at:
            return self._set_status(action, "failed", "Message selection expired. Open Messages and choose the conversation again.")
        if selection.thread.reply_target is None:
            return self._set_status(action, "failed", "This conversation has no Messages send target. Open it again on your Mac.")
        action.recipient = selection.thread
        action.expires_at = min(action.expires_at, selection.expires_at)
        self._advance_preparation(action)
        return action.status

    async def provide_message_input(
        self,
        action_id: str,
        *,
        recipient_name: str | None = None,
        recipient_ref: str | None = None,
        body: str | None = None,
    ) -> MessageActionStatus:
        """Fill missing fields; never replace an already bound target or body."""
        action = self._known_action(action_id)
        self._validate_body(body)
        if body is not None and action.body_digest not in (None, self._body_digest(body)):
            raise ValueError("This action already has a different message body.")
        if action.status.state != "awaiting_input":
            if recipient_name is not None or recipient_ref is not None:
                raise ValueError("This action can no longer select a recipient.")
            return action.status
        if action.resolving:
            raise ValueError("Contact lookup is already in progress for this action.")
        recipient_name = self._validated_name(recipient_name)
        if recipient_name is not None and recipient_ref is not None:
            raise ValueError("Supply a contact name or a candidate reference, not both.")
        if action.origin_kind == "messages.selection" and (recipient_name or recipient_ref):
            raise ValueError("A selected-conversation reply cannot change its recipient.")
        if action.recipient is not None and (recipient_name or recipient_ref):
            raise ValueError("This action already has a selected recipient.")
        if recipient_ref is not None:
            selected = action.candidates.get(recipient_ref)
            if selected is None:
                raise ValueError("Unknown or stale contact candidate reference.")
            action.recipient = selected
            action.candidates.clear()
        if body is not None:
            action.body, action.body_digest = body, self._body_digest(body)
        if recipient_name is not None:
            await self._resolve_prepared_recipient(action, recipient_name)
        self._advance_preparation(action)
        return action.status

    def message_action_status(self, action_id: str) -> MessageActionStatus:
        """Read an action without reissuing a confirmation or external send."""
        return self._known_action(action_id).status

    def message_prompt_field(self, action_id: str) -> Literal["recipient", "body"] | None:
        """The exact typed input this action can currently accept from speech."""
        action = self._known_action(action_id)
        if action.status.state != "awaiting_input" or action.resolving:
            return None
        if action.recipient is None:
            # A selected reply cannot be retargeted by supplying a name. Its
            # missing-selection status requires a Messages UI selection.
            return "recipient" if action.origin_kind == "messages.compose" else None
        return "body" if action.body is None else None

    def cancel_message_action(self, action_id: str) -> MessageActionStatus:
        """Cancel before submission, or retain uncertainty once sending began."""
        action = self._known_action(action_id, expire=False)
        if action.status.state not in {"awaiting_input", "awaiting_confirmation", "sending"}:
            return action.status
        action.cancel_requested = True
        if action.task is not None:
            action.task.cancel()
        return self._set_status(
            action, "uncertain" if action.send_started else "cancelled",
            "Messages may have submitted this message. Check Messages on your Mac before trying again."
            if action.send_started else "Message cancelled.",
        )

    async def aclose(self) -> None:
        """Revoke incomplete drafts and cancel/join controller-owned work."""
        self._closed = True
        for action_id in tuple(self._prepared):
            self.cancel_message_action(action_id)
        tasks = [action.task for action in self._prepared.values() if action.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.clear_selection("shutdown")

    @staticmethod
    def _body_digest(body: str) -> str:
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_body(body: str | None) -> None:
        if body is not None:
            if not isinstance(body, str) or not body.strip() or len(body) > 2000 or "\0" in body:
                raise ValueError("Message body must contain between 1 and 2000 characters without NUL.")
            body.encode("utf-8")

    @staticmethod
    def _validated_name(name: str | None) -> str | None:
        if name is None:
            return None
        if not isinstance(name, str) or len(name) > 200 or "\0" in name:
            raise ValueError("Recipient name must be at most 200 characters.")
        name.encode("utf-8")
        name = " ".join(name.split())
        if "@" in name or re.fullmatch(r"[+\d().\s-]{7,}", name):
            raise ValueError("Choose a Mac contact by name; recipient handles are not accepted.")
        return name or None

    def _new_action(
        self, action_id: str, request: list[str | None], trace_id: str,
        origin_kind: str, body: str | None,
    ) -> tuple[_PreparedMessage, bool]:
        if self._closed:
            raise RuntimeError("Message controller is closed.")
        if not isinstance(action_id, str) or not re.fullmatch(r"[a-zA-Z0-9._:-]{1,96}", action_id):
            raise ValueError("Action ID must be 1 to 96 safe identifier characters.")
        if not isinstance(trace_id, str) or len(trace_id) > 200:
            raise ValueError("Trace ID must be at most 200 characters.")
        self._validate_body(body)
        fingerprint = self._body_digest(json.dumps(request, ensure_ascii=False))
        if action_id in self._prepared:
            action = self._known_action(action_id)
            if action.fingerprint != fingerprint:
                raise ValueError("Action ID already belongs to a different message request.")
            return action, False
        # Never evict a replay identity and then allow it to send again. Refuse
        # new identities at the bound; receipts retain only hashes and status.
        if len(self._prepared) >= MAX_PREPARED_ACTIONS:
            raise RuntimeError("Message action limit reached for this controller session.")
        if self._active_action is not None:
            self._known_action(self._active_action)
        action = _PreparedMessage(
            fingerprint=fingerprint,
            status=MessageActionStatus(action_id, "awaiting_input", "Say the recipient's contact name.", needs_input="recipient"),
            trace_id=re.sub(r"[^a-zA-Z0-9._:-]", "-", trace_id)[:64] or "conversation",
            origin_kind=origin_kind,
            expires_at=self._monotonic() + self._selection_ttl_s,
            body=body,
            body_digest=self._body_digest(body) if body is not None else None,
        )
        self._prepared[action_id] = action
        if self._active_action is not None:
            previous = self._prepared.get(self._active_action)
            if (
                previous is not None
                and previous.status.state == "awaiting_input"
                and previous.task is None
                and not previous.resolving
                and not previous.send_started
            ):
                # A draft still waiting for a name or a body has nothing on
                # the glass to protect and nothing in flight. The wearer's
                # newer request replaces it — observed live: "tell Alex I'm
                # late" after an abandoned "message my wife" was refused as
                # "already awaiting input", and every message after it too.
                # Confirmation, sending and a contact lookup in progress
                # still hold the single flight.
                log.info("message draft superseded by a newer request")
                self.cancel_message_action(self._active_action)
        if self._active_action is not None or self._draft_lock.locked():
            self._set_status(action, "failed", "A message reply is already awaiting input or confirmation.")
        else:
            self._active_action = action_id
            action.expiry = asyncio.get_running_loop().call_later(
                self._selection_ttl_s, self._expire_preparation, action_id,
            )
        return action, True

    def _known_action(self, action_id: str, *, expire: bool = True) -> _PreparedMessage:
        action = self._prepared.get(action_id)
        if action is None:
            raise ValueError("Unknown message action in this controller session; earlier send status is unavailable.")
        if expire:
            self._expire_preparation(action_id)
        return action

    def _expire_preparation(self, action_id: str) -> None:
        action = self._prepared[action_id]
        if action.status.state == "awaiting_input" and self._monotonic() >= action.expires_at:
            self.cancel_message_action(action_id)
            self._set_status(action, "cancelled", "Message preparation expired. Start a new draft.")

    def _set_status(
        self, action: _PreparedMessage, state: MessageActionState, message: str, *,
        needs_input: Literal["recipient", "body"] | None = None,
        candidates: tuple[MessageRecipientCandidate, ...] = (),
    ) -> MessageActionStatus:
        action.status = MessageActionStatus(
            action.status.action_id, state, message,
            recipient_name=action.recipient.sender if action.recipient else action.status.recipient_name,
            needs_input=needs_input, candidates=candidates,
            verification_level="delivery_unverified" if state == "sent" else None,
        )
        if state not in {"awaiting_input", "awaiting_confirmation", "sending"}:
            action.body = None
            action.recipient = None
            action.candidates.clear()
            if action.expiry is not None:
                action.expiry.cancel()
                action.expiry = None
            if self._active_action == action.status.action_id:
                self._active_action = None
        if self._on_status is not None:
            try:
                self._on_status(action.status)
            except Exception as exc:
                # Publication observes a committed receipt. Its failure must
                # never alter that result or retry an external submission.
                log.warning("message status observer failed error=%s", type(exc).__name__)
        return action.status

    async def _resolve_prepared_recipient(self, action: _PreparedMessage, query: str) -> None:
        if _RELATIONSHIP.fullmatch(query):
            self._set_status(action, "awaiting_input", "I don't have a verified relationship contact. What is their Mac contact name?", needs_input="recipient")
            return
        action.resolving = True
        try:
            matches = await asyncio.wait_for(
                asyncio.to_thread(self._contact_search, query), CONTACT_LOOKUP_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            self.cancel_message_action(action.status.action_id)
            raise
        except Exception as exc:
            log.warning("message contact lookup failed error=%s", type(exc).__name__)
            if action.status.state == "awaiting_input":
                self._set_status(action, "failed", "Mac contact lookup did not complete. No message was sent.")
            return
        finally:
            action.resolving = False
        self._expire_preparation(action.status.action_id)
        if action.status.state != "awaiting_input":
            return
        # Deduplicate identical addresses, but never collapse distinct addresses
        # for one name into an arbitrary recipient.
        matches = list(dict.fromkeys(matches))
        action.candidates.clear()
        if len(matches) == 1:
            name, handle = matches[0]
            action.recipient = MessageThread(sender=name, handle=handle, text="", at=0.0)
            return
        if not matches:
            self._set_status(action, "awaiting_input", f"No Mac contact matched {query}. Try a fuller name.", needs_input="recipient")
            return
        choices = []
        for name, handle in matches[:MAX_RECIPIENT_CANDIDATES]:
            reference = secrets.token_urlsafe(12)
            label = name
            if sum(candidate_name == name for candidate_name, _ in matches) > 1:
                if "@" in handle:
                    local, _, domain = handle.partition("@")
                    label += f" ({local[:1]}…@{domain})"
                else:
                    label += f" (number ending {re.sub(r'\D', '', handle)[-4:]})"
            action.candidates[reference] = MessageThread(sender=name, handle=handle, text="", at=0.0)
            choices.append(MessageRecipientCandidate(reference, label))
        self._set_status(action, "awaiting_input", "More than one contact address matched. Choose a listed contact or give a fuller name.", needs_input="recipient", candidates=tuple(choices))

    def _advance_preparation(self, action: _PreparedMessage) -> None:
        if action.status.state != "awaiting_input":
            return
        if action.recipient is None:
            return
        if action.recipient.reply_target is None:
            self._set_status(action, "failed", "This conversation has no Messages send target. Open it again on your Mac.")
            return
        if action.body is None:
            self._set_status(action, "awaiting_input", f"Recipient {action.recipient.sender} selected. Say the message.", needs_input="body")
            return
        self._set_status(action, "awaiting_confirmation", "Awaiting your confirmation on the glasses.")
        if action.expiry is not None:
            action.expiry.cancel()
            action.expiry = None
        # The caller never awaits this task. Tool deadlines cannot cancel an
        # approved send; only explicit controller cleanup owns cancellation.
        action.task = asyncio.create_task(self._run_prepared(action), name="message-confirm-and-send")

    async def _run_prepared(self, action: _PreparedMessage) -> None:
        try:
            if action.cancel_requested:
                return
            async with self._draft_lock:
                assert action.recipient is not None and action.body is not None
                await self._confirm_and_send(
                    action.recipient, action.trace_id, action.body,
                    origin_kind=action.origin_kind, prepared=action,
                )
        except asyncio.CancelledError:
            self._record_interrupted_action(action, "controller_cancelled")
            self._set_status(action, "uncertain" if action.send_started else "cancelled", "Messages may have submitted this message. Check Messages on your Mac before trying again." if action.send_started else "Message cancelled.")
        except ActionConflictError:
            # A stable journal key can already belong to an earlier process.
            # Its receipt cannot be looked up by caller ID through this API.
            # Do not create a fresh key, retry, or invent the previous result.
            self._set_status(action, "uncertain", "The journal could not establish this action's current outcome. Check Messages on your Mac before trying again.")
        except Exception as exc:
            self._record_interrupted_action(action, type(exc).__name__)
            self._set_status(action, "uncertain" if action.send_started else "failed", "Messages may have submitted this message. Check Messages on your Mac before trying again." if action.send_started else "Message preparation failed. No message was sent.")
            log.warning("prepared message failed error=%s", type(exc).__name__)

    def _record_interrupted_action(self, action: _PreparedMessage, reason: str) -> None:
        if self._journal is None or action.journal_id is None:
            return
        try:
            state = self._journal.get(action.journal_id).state
            if state in {"dispatching", "verifying"} and action.attempt_id is not None:
                self._journal.record_failure(action.journal_id, action.attempt_id, error_code=reason, possible_external_commit=True, now_us=time.time_ns() // 1000)
            elif state in {"ready", "awaiting_confirmation"}:
                self._journal.cancel_unconfirmed(action.journal_id, reason_code=reason, now_us=time.time_ns() // 1000)
                self._journal.purge_payload(action.journal_id)
        except Exception as exc:
            # A receipt failure cannot turn a possibly submitted send into a
            # definite failure. Durable startup recovery retains that boundary.
            log.warning("message cleanup journal error=%s", type(exc).__name__)

    def select(self, thread: MessageThread, trace_id: str) -> None:
        if self._active_action is not None:
            self.cancel_message_action(self._active_action)
        safe_trace = re.sub(r"[^a-zA-Z0-9._:-]", "-", trace_id)[:64] or "hud"
        self._compose = None
        self._pending_reply = None
        self._selection = _MessageSelection(
            thread=thread,
            trace_id=safe_trace,
            expires_at=self._monotonic() + self._selection_ttl_s,
        )
        log.info(
            "message reply trace=%s stage=selected recipient=%s",
            safe_trace,
            (_target_digest(thread) or "unavailable")[:10],
        )

    def begin_compose(self, trace_id: str) -> None:
        if self._active_action is not None:
            self.cancel_message_action(self._active_action)
        safe_trace = re.sub(r"[^a-zA-Z0-9._:-]", "-", trace_id)[:64] or "hud"
        self._selection = None
        self._pending_reply = None
        self._compose = _ComposeSelection(
            trace_id=safe_trace,
            expires_at=self._monotonic() + self._selection_ttl_s,
        )
        log.info("message reply trace=%s stage=compose-selected", safe_trace)

    def clear_selection(self, reason: str = "explicit") -> None:
        if reason != "confirmation-accepted" and self._active_action is not None:
            self.cancel_message_action(self._active_action)
        selection = self._selection
        compose = self._compose
        self._selection = None
        self._compose = None
        self._pending_reply = None
        if selection is not None:
            log.info(
                "message reply trace=%s stage=selection-cleared reason=%s",
                selection.trace_id,
                re.sub(r"[^a-zA-Z0-9._:-]", "-", reason)[:32] or "explicit",
            )
        elif compose is not None:
            log.info(
                "message reply trace=%s stage=selection-cleared reason=%s",
                compose.trace_id,
                re.sub(r"[^a-zA-Z0-9._:-]", "-", reason)[:32] or "explicit",
            )

    async def handle_command(self, text: str) -> str | None:
        command = " ".join(text.strip().split())
        # Terminal punctuation is STT noise for grammar matching; the body a
        # match captures still keeps its own interior punctuation.
        stripped = command.rstrip(".,!?").rstrip()
        match = _REPLY.match(stripped)
        compose_match = _NEW_MESSAGE.match(stripped) if match is None else None
        message_shaped = (
            match is not None or compose_match is not None
            or _BARE_REPLY.fullmatch(stripped) is not None
        )
        if self._active_action is not None:
            status = self._known_action(self._active_action).status
            if self._active_action is not None:
                # Typed prompts belong to their explicit action-input API.
                # This command hook is asked about every conversational turn;
                # swallowing a name or unrelated request would prevent the
                # assistant from calling that API or another feature tool.
                if not message_shaped:
                    return None
                if status.state == "awaiting_input":
                    return "A message reply is already awaiting input. Finish or cancel that draft first."
                if status.state == "sending":
                    return "A message submission is already in progress. Do not send it again."
                return "A message reply is already awaiting confirmation."
        if not message_shaped and self._compose is None and self._pending_reply is None:
            return None
        if self._draft_lock.locked():
            return "A message reply is already awaiting confirmation."
        if (
            self._begin_reply is not None
            and self._compose is None
            and self._pending_reply is None
            and _BARE_REPLY.fullmatch(stripped) is not None
        ):
            # The UI callback prepares a typed action, which owns this same
            # single-flight lock later during confirmation and submission.
            # Enter before acquiring the legacy lock or preparation refuses
            # its own invocation as an already-running draft.
            return await self._begin_reply()
        async with self._draft_lock:
            # A compose selection accepts natural recipient/body turns, so it
            # must use the same single-flight boundary as a selected-thread
            # reply. Otherwise a duplicate speech event can race through a
            # selected recipient and open a second confirmation/send.
            if self._compose is not None:
                if compose_match is not None:
                    return await self._handle_new_message(compose_match)
                return await self._handle_compose_turn(command)
            if self._pending_reply is not None:
                return await self._handle_pending_reply_turn(
                    command, stripped, match,
                )
            if match is None and compose_match is None:
                if _BARE_REPLY.fullmatch(stripped) is not None:
                    return self._arm_pending_reply()
                return None
            if match is not None:
                return await self._handle_reply(match)
            assert compose_match is not None
            return await self._handle_new_message(compose_match)

    def selected_reply_available(self) -> bool:
        """Read whether the current conversation can start a scoped reply."""
        selection = self._selection
        return (
            not self._closed
            and selection is not None
            and selection.thread.reply_target is not None
            and self._monotonic() < selection.expires_at
            and self._compose is None
            and self._pending_reply is None
            and self._active_action is None
            and not self._draft_lock.locked()
        )

    def is_compose_armed(self) -> bool:
        # A pending bare-reply prompt needs the same first refusal a compose
        # draft gets: its body may be greeting-shaped ("Good morning").
        return self._compose is not None or self._pending_reply is not None

    def _arm_pending_reply(self) -> str:
        """The wearer said exactly what the lens teaches: "reply"."""
        selection = self._selection
        if selection is None:
            return "Select a Messages conversation first."
        if self._monotonic() > selection.expires_at:
            self.clear_selection("expired")
            return (
                "Message selection expired. "
                "Open Messages and choose the conversation again."
            )
        if selection.thread.reply_target is None:
            return "This conversation has no Messages send target. Open it again on your Mac."
        self._pending_reply = _PendingReply(
            trace_id=selection.trace_id,
            expires_at=self._monotonic() + REPLY_PROMPT_TTL_SECONDS,
        )
        log.info(
            "message reply trace=%s stage=reply-prompt-armed", selection.trace_id,
        )
        return "Say the reply."

    async def _handle_pending_reply_turn(
        self,
        command: str,
        stripped: str,
        match: re.Match[str] | None,
    ) -> str:
        pending = self._pending_reply
        assert pending is not None
        self._pending_reply = None
        if self._monotonic() > pending.expires_at:
            return 'Reply prompt expired. Say "reply" again.'
        if stripped.casefold() in {"cancel", "cancel reply", "never mind"}:
            log.info(
                "message reply trace=%s stage=reply-prompt-cancelled",
                pending.trace_id,
            )
            return "Reply cancelled."
        # A full command spoken while prompted wins over body capture.
        if match is not None:
            return await self._handle_reply(match)
        if not stripped:
            self._pending_reply = pending
            return "Say the reply."
        selection = self._selection
        if selection is None or self._monotonic() > selection.expires_at:
            self.clear_selection("expired")
            return (
                "Message selection expired. "
                "Open Messages and choose the conversation again."
            )
        return await self._confirm_and_send(
            selection.thread,
            selection.trace_id,
            command,
            origin_kind="messages.selection",
        )

    async def _handle_compose_turn(self, command: str) -> str:
        compose = self._compose
        assert compose is not None
        if self._monotonic() > compose.expires_at:
            self.clear_selection("expired")
            return "New message draft expired. Open Messages and choose New message again."
        if command.casefold() in {"cancel", "cancel message", "never mind"}:
            self.clear_selection("cancelled")
            return "New message cancelled."
        if not command:
            return (
                "Say the recipient name."
                if compose.recipient is None
                else "Say the message."
            )
        if compose.recipient is None:
            matches = await asyncio.to_thread(self._contact_search, command)
            if not matches:
                return f"No Mac contact matched {command}. Try a fuller name."
            if len(matches) > 1:
                return (
                    f"More than one Mac contact address matched {command}. "
                    "Say the full name."
                )
            recipient_name, recipient_handle = matches[0]
            self._compose = _ComposeSelection(
                trace_id=compose.trace_id,
                expires_at=compose.expires_at,
                recipient=MessageThread(
                    sender=recipient_name,
                    handle=recipient_handle,
                    text="",
                    at=0.0,
                ),
            )
            return f"Recipient {recipient_name} selected. Say the message."
        return await self._confirm_and_send(
            compose.recipient,
            compose.trace_id,
            command,
            origin_kind="messages.compose",
        )

    async def _handle_reply(self, match: re.Match[str]) -> str:
        if self._selection is None:
            return "Select a Messages conversation first."
        if self._monotonic() > self._selection.expires_at:
            self.clear_selection("expired")
            return (
                "Message selection expired. "
                "Open Messages and choose the conversation again."
            )
        body = match.group("body").strip()
        if not body:
            return "Say what you want the reply to contain."

        thread = self._selection.thread
        trace_id = self._selection.trace_id
        return await self._confirm_and_send(
            thread,
            trace_id,
            body,
            origin_kind="messages.selection",
        )

    async def _handle_new_message(self, match: re.Match[str]) -> str:
        if self._compose is None:
            return "Open Messages and choose New message first."
        if self._monotonic() > self._compose.expires_at:
            self.clear_selection("expired")
            return "New message draft expired. Open Messages and choose New message again."
        recipient_query = match.group("recipient").strip()
        body = match.group("body").strip()
        matches = await asyncio.to_thread(self._contact_search, recipient_query)
        if not matches:
            return f"No Mac contact matched {recipient_query}. Try a fuller name."
        if len(matches) > 1:
            return (
                f"More than one Mac contact address matched {recipient_query}. "
                "Say the full name and message again."
            )
        recipient_name, recipient_handle = matches[0]
        thread = MessageThread(
            sender=recipient_name,
            handle=recipient_handle,
            text="",
            at=0.0,
        )
        return await self._confirm_and_send(
            thread,
            self._compose.trace_id,
            body,
            origin_kind="messages.compose",
        )

    async def _confirm_and_send(
        self,
        thread: MessageThread,
        trace_id: str,
        body: str,
        *,
        origin_kind: str,
        prepared: _PreparedMessage | None = None,
    ) -> str:
        target = thread.reply_target
        if target is None:
            message = "This conversation has no Messages send target. Open it again on your Mac."
            if prepared is not None:
                self._set_status(prepared, "failed", message)
            return message
        target_kind, target_id = target
        idempotency_key = (
            f"message-prepared:{prepared.status.action_id}" if prepared is not None
            else f"message-reply:{trace_id}:{secrets.token_hex(4)}"
        )
        action_id = idempotency_key
        recipient_label = f"group {thread.sender}" if thread.is_group else thread.sender
        prompt = f"Send to {recipient_label}? “{body}”"
        if utf16_units(prompt) > CONFIRM_PROMPT_MAX_UTF16_UNITS:
            log.info(
                "message reply trace=%s stage=draft-rejected reason=confirmation-overflow chars=%d",
                action_id,
                len(body),
            )
            message = (
                "Reply is too long for the confirmation screen. "
                "Shorten it and try again."
            )
            if prepared is not None:
                self._set_status(prepared, "failed", message)
            return message
        payload = json.dumps(
            {"body": body, "target": {"kind": target_kind, "id": target_id}},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        payload_digest = hashlib.sha256(payload).digest()
        if self._journal is not None:
            now_us = time.time_ns() // 1000
            durable = self._journal.create_draft(
                ActionDraft(
                    kind="message.reply",
                    canonical_payload=payload,
                    schema_version=2,
                    target_digest=hashlib.sha256(
                        b"joat-message-target-v2\0" + _target_bytes(target)
                    ).digest(),
                    idempotency_key=idempotency_key,
                    origin_kind=origin_kind,
                    origin_id=trace_id,
                    origin_ordinal=now_us,
                ),
                now_us=now_us,
            )
            action_id = durable.action_id
            if prepared is not None:
                prepared.journal_id = action_id
            # An existing journal entry is evidence of an earlier action,
            # never permission to replay its confirmation or external send.
            if durable.state != "drafted":
                raise ActionConflictError("message action already exists")
            self._journal.mark_ready(
                action_id,
                reason_code="message_schema_valid",
                now_us=now_us + 1,
            )
        log.info(
            "message reply trace=%s stage=confirmation-requested recipient=%s chars=%d",
            action_id,
            (_target_digest(thread) or "unavailable")[:10],
            len(body),
        )
        decision = await self._confirm(
            action_id,
            prompt,
            CONFIRM_TRANSPORT_SECONDS,
            payload_digest=payload_digest.hex(),
            diagnostic_kind="messages",
            **({"message_ui_action_id": prepared.status.action_id}
               if prepared is not None and prepared.status.action_id.startswith("messages-ui-") else {}),
        )
        if prepared is not None and prepared.cancel_requested:
            raise asyncio.CancelledError
        accepted = decision.accepted if isinstance(decision, ConfirmationResult) else bool(decision)
        if self._journal is not None:
            if not isinstance(decision, ConfirmationResult):
                self._journal.cancel_unconfirmed(
                    action_id,
                    reason_code="confirmation_missing_evidence",
                    now_us=time.time_ns() // 1000,
                )
                self._journal.purge_payload(action_id)
                accepted = False
            else:
                self._journal.record_confirmation(
                    action_id,
                    ConfirmationEvidence(
                        challenge_id=decision.challenge_id,
                        challenge_token=decision.challenge_id,
                        session_id=decision.session_id,
                        pairing_identity_digest=decision.pairing_identity_digest,
                        authorization_digest=decision.authorization_digest,
                        payload_digest=decision.payload_digest,
                        preview_digest=decision.preview_digest,
                        issued_at_us=decision.issued_at_us,
                        expires_at_us=decision.expires_at_us,
                        preview_completed_at_us=decision.preview_completed_at_us,
                        decided_at_us=decision.decided_at_us,
                        accepted=decision.accepted,
                    ),
                )
                accepted = self._journal.get(action_id).state == "approved"
        if not accepted:
            log.info("message reply trace=%s stage=cancelled", action_id)
            if self._journal is not None:
                state = self._journal.get(action_id).state
                if state in ("rejected", "expired"):
                    self._journal.purge_payload(action_id)
            if prepared is not None:
                self._set_status(prepared, "cancelled", "Reply cancelled.")
            return "Reply cancelled."
        self.clear_selection("confirmation-accepted")
        attempt = None
        if self._journal is not None:
            attempt = self._journal.begin_attempt(
                action_id,
                adapter="messages.applescript",
                adapter_version="2",
                request_id=f"messages:{action_id}",
                now_us=time.time_ns() // 1000,
            )
        if prepared is not None:
            prepared.attempt_id = attempt.attempt_id if attempt is not None else None
            prepared.send_started = True
            self._set_status(
                prepared, "sending",
                "Confirmation accepted; Messages submission is in progress. Its outcome is not yet known. Do not retry.",
            )
        try:
            if target_kind == "chat":
                await self._sender.send_chat(target_id, body)
            else:
                await self._sender.send(target_id, body)
        except MessageSendError as exc:
            if self._journal is not None and attempt is not None:
                self._journal.record_failure(
                    action_id,
                    attempt.attempt_id,
                    error_code=type(exc).__name__,
                    possible_external_commit=exc.possible_external_commit,
                    now_us=time.time_ns() // 1000,
                )
                if not exc.possible_external_commit:
                    self._journal.purge_payload(action_id)
            log.warning(
                "message reply trace=%s stage=send-failed error=%s",
                action_id,
                type(exc).__name__,
            )
            if exc.possible_external_commit:
                message = (
                    f"Messages may have submitted the reply to {thread.sender}. "
                    "Check Messages on your Mac before trying again."
                )
                if prepared is not None:
                    self._set_status(prepared, "uncertain", message)
                return message
            message = f"Could not send to {thread.sender}. Check Messages on your Mac."
            if prepared is not None:
                self._set_status(prepared, "failed", message)
            return message
        if self._journal is not None and attempt is not None:
            self._journal.record_success(
                action_id,
                attempt.attempt_id,
                outcome="submitted_to_messages",
                verification_level="delivery_unverified",
                evidence_digest=hashlib.sha256(
                    b"joat-messages-osascript-exit-zero-v1\0" + action_id.encode("utf-8")
                ).digest(),
                now_us=time.time_ns() // 1000,
            )
            self._journal.purge_payload(action_id)
        log.info("message reply trace=%s stage=submitted delivery=unverified", action_id)
        message = f"Submitted to Messages for {thread.sender}; delivery unverified."
        if prepared is not None:
            self._set_status(prepared, "sent", message)
        return message
