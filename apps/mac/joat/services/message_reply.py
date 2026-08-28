"""Voice draft and fail-closed confirmation for a selected Messages thread."""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from collections.abc import Awaitable, Callable

from ..channels.imessage import MessageThread
from ..channels.message_sender import MessageSendError
from ..core.interfaces import MessageSender

log = logging.getLogger(__name__)

Confirm = Callable[[str, str, float], Awaitable[bool]]
_REPLY = re.compile(r"^reply(?:\s+(?:saying|with))?\s+(?P<body>.+)$", re.IGNORECASE)
# The plugin begins its eight-second decision clock only after the complete
# frame is visible. The gateway envelope also has to include that BLE paint.
CONFIRM_TRANSPORT_SECONDS = 12.0
MAX_CONFIRM_PROMPT_CHARS = 120


def _recipient_key(handle: str) -> str:
    return hashlib.sha256(handle.encode("utf-8")).hexdigest()[:10]


class MessageReplyController:
    """One selected conversation; one explicitly confirmed send at a time."""

    def __init__(self, sender: MessageSender, confirm: Confirm) -> None:
        self._sender = sender
        self._confirm = confirm
        self._selection: tuple[MessageThread, str] | None = None

    def select(self, thread: MessageThread, trace_id: str) -> None:
        safe_trace = re.sub(r"[^a-zA-Z0-9._:-]", "-", trace_id)[:64] or "hud"
        self._selection = (thread, safe_trace)
        log.info(
            "message reply trace=%s stage=selected recipient=%s",
            safe_trace,
            _recipient_key(thread.handle),
        )

    async def handle_command(self, text: str) -> str | None:
        match = _REPLY.match(text.strip())
        if match is None:
            return None
        if self._selection is None:
            return "Select a Messages conversation first."
        body = match.group("body").strip()
        if not body:
            return "Say what you want the reply to contain."

        thread, trace_id = self._selection
        action_id = f"message-reply:{trace_id}:{secrets.token_hex(4)}"
        prompt = f"Send to {thread.sender}? “{body}”"
        if len(prompt) > MAX_CONFIRM_PROMPT_CHARS:
            log.info(
                "message reply trace=%s stage=draft-rejected reason=confirmation-overflow chars=%d",
                action_id,
                len(body),
            )
            return (
                "Reply is too long for the confirmation screen. "
                "Shorten it and try again."
            )
        log.info(
            "message reply trace=%s stage=confirmation-requested recipient=%s chars=%d",
            action_id,
            _recipient_key(thread.handle),
            len(body),
        )
        accepted = await self._confirm(
            action_id,
            prompt,
            CONFIRM_TRANSPORT_SECONDS,
        )
        if not accepted:
            log.info("message reply trace=%s stage=cancelled", action_id)
            return "Reply cancelled."
        try:
            await self._sender.send(thread.handle, body)
        except MessageSendError as exc:
            log.warning(
                "message reply trace=%s stage=send-failed error=%s",
                action_id,
                type(exc).__name__,
            )
            return f"Could not send to {thread.sender}. Check Messages on your Mac."
        log.info("message reply trace=%s stage=sent", action_id)
        return f"Sent to {thread.sender}."
