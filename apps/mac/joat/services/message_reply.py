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
from dataclasses import dataclass

from ..channels.imessage import MessageThread
from ..channels.message_sender import MessageSendError
from ..core.interfaces import MessageSender
from ..core.confirmation import ConfirmationResult
from ..core.protocol import CONFIRM_PROMPT_MAX_UTF16_UNITS, utf16_units
from ..persistence.actions import (
    ActionDraft,
    ActionJournal,
    ConfirmationEvidence,
)

log = logging.getLogger(__name__)

Confirm = Callable[..., Awaitable[bool | ConfirmationResult | None]]
_REPLY = re.compile(r"^reply(?:\s+(?:saying|with))?\s+(?P<body>.+)$", re.IGNORECASE)
# The plugin begins its eight-second decision clock only after the complete
# frame is visible. The gateway envelope also has to include that BLE paint.
CONFIRM_TRANSPORT_SECONDS = 12.0
MESSAGE_SELECTION_TTL_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class _MessageSelection:
    thread: MessageThread
    trace_id: str
    expires_at: float


def _recipient_key(handle: str) -> str:
    return hashlib.sha256(handle.encode("utf-8")).hexdigest()[:10]


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
    ) -> None:
        if selection_ttl_s <= 0:
            raise ValueError("message selection TTL must be positive")
        self._sender = sender
        self._confirm = confirm
        self._journal = journal
        self._selection_ttl_s = selection_ttl_s
        self._monotonic = monotonic
        self._selection: _MessageSelection | None = None
        self._draft_lock = asyncio.Lock()

    def select(self, thread: MessageThread, trace_id: str) -> None:
        safe_trace = re.sub(r"[^a-zA-Z0-9._:-]", "-", trace_id)[:64] or "hud"
        self._selection = _MessageSelection(
            thread=thread,
            trace_id=safe_trace,
            expires_at=self._monotonic() + self._selection_ttl_s,
        )
        log.info(
            "message reply trace=%s stage=selected recipient=%s",
            safe_trace,
            _recipient_key(thread.handle),
        )

    def clear_selection(self, reason: str = "explicit") -> None:
        selection = self._selection
        self._selection = None
        if selection is not None:
            log.info(
                "message reply trace=%s stage=selection-cleared reason=%s",
                selection.trace_id,
                re.sub(r"[^a-zA-Z0-9._:-]", "-", reason)[:32] or "explicit",
            )

    async def handle_command(self, text: str) -> str | None:
        match = _REPLY.match(text.strip())
        if match is None:
            return None
        if self._draft_lock.locked():
            return "A message reply is already awaiting confirmation."
        async with self._draft_lock:
            return await self._handle_reply(match)

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
        idempotency_key = f"message-reply:{trace_id}:{secrets.token_hex(4)}"
        action_id = idempotency_key
        prompt = f"Send to {thread.sender}? “{body}”"
        if utf16_units(prompt) > CONFIRM_PROMPT_MAX_UTF16_UNITS:
            log.info(
                "message reply trace=%s stage=draft-rejected reason=confirmation-overflow chars=%d",
                action_id,
                len(body),
            )
            return (
                "Reply is too long for the confirmation screen. "
                "Shorten it and try again."
            )
        payload = json.dumps(
            {"body": body, "recipient": thread.handle},
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
                    target_digest=hashlib.sha256(
                        b"joat-message-recipient-v1\0" + thread.handle.encode("utf-8")
                    ).digest(),
                    idempotency_key=idempotency_key,
                    origin_kind="messages.selection",
                    origin_id=trace_id,
                    origin_ordinal=now_us,
                ),
                now_us=now_us,
            )
            action_id = durable.action_id
            self._journal.mark_ready(
                action_id,
                reason_code="message_schema_valid",
                now_us=now_us + 1,
            )
        log.info(
            "message reply trace=%s stage=confirmation-requested recipient=%s chars=%d",
            action_id,
            _recipient_key(thread.handle),
            len(body),
        )
        decision = await self._confirm(
            action_id,
            prompt,
            CONFIRM_TRANSPORT_SECONDS,
            payload_digest=payload_digest.hex(),
        )
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
            return "Reply cancelled."
        self.clear_selection("confirmation-accepted")
        attempt = None
        if self._journal is not None:
            attempt = self._journal.begin_attempt(
                action_id,
                adapter="messages.applescript",
                adapter_version="1",
                request_id=f"messages:{action_id}",
                now_us=time.time_ns() // 1000,
            )
        try:
            await self._sender.send(thread.handle, body)
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
                return (
                    f"Messages may have submitted the reply to {thread.sender}. "
                    "Check Messages on your Mac before trying again."
                )
            return f"Could not send to {thread.sender}. Check Messages on your Mac."
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
        return f"Submitted to Messages for {thread.sender}; delivery unverified."
