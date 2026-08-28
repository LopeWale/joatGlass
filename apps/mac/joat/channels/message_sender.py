"""Messages.app adapter for explicitly confirmed outbound replies."""

from __future__ import annotations

import asyncio

from ..core.interfaces import MessageSender


class MessageSendError(RuntimeError):
    """Messages.app did not accept the outbound message."""


_SEND_SCRIPT = """
on run argv
    set recipientHandle to item 1 of argv
    set messageBody to item 2 of argv
    tell application "Messages"
        set targetParticipant to missing value
        repeat with targetAccount in accounts
            try
                set targetParticipant to participant recipientHandle of targetAccount
                if targetParticipant is not missing value then exit repeat
            end try
        end repeat
        if targetParticipant is missing value then error "No Messages participant for recipient"
        send messageBody to targetParticipant
    end tell
end run
""".strip()


class MessagesAppSender(MessageSender):
    """Send through Apple's Messages scripting dictionary, never chat.db."""

    async def send(self, recipient: str, text: str) -> None:
        handle = recipient.strip()
        body = text.strip()
        if not handle or not body:
            raise MessageSendError("recipient and message are required")
        process = await asyncio.create_subprocess_exec(
            "osascript",
            "-e",
            _SEND_SCRIPT,
            handle,
            body,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise MessageSendError(detail or "Messages rejected the send")
