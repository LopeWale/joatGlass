"""Messages.app adapter for explicitly confirmed outbound replies."""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Literal

from ..core.interfaces import MessageSender


log = logging.getLogger(__name__)

AutomationState = Literal["granted", "denied", "pending", "unavailable"]


class MessageSendError(RuntimeError):
    """Messages.app did not accept the outbound message."""

    def __init__(
        self,
        message: str,
        *,
        possible_external_commit: bool = True,
        code: str = "send",
    ) -> None:
        super().__init__(message)
        self.possible_external_commit = possible_external_commit
        #: Content-free reason for logs and traces (never the body).
        self.code = code


# Sending is an Apple Event to Messages, so macOS's Automation permission
# ("JOAT" may control "Messages") gates it. That is NOT the Full Disk Access
# that lets JOAT read chat.db. Until it is decided, the first send shows a
# consent prompt on the Mac and osascript blocks on it — with the wearer away,
# the send hit its deadline and the glasses sat on "sending" (2026-10-12).
# This read-only event carries no message body, so asking it is harmless.
_PERMISSION_PROBE_SCRIPT = 'tell application "Messages" to count accounts'
# errAEEventNotPermitted, as osascript reports a refused Apple Event.
_NOT_PERMITTED_MARKERS = ("-1743", "Not authorized to send Apple events")
AUTOMATION_SETTINGS_PATH = "System Settings > Privacy & Security > Automation > JOAT > Messages"


def _not_permitted(stderr: bytes) -> bool:
    text = stderr.decode("utf-8", errors="replace")
    return any(marker in text for marker in _NOT_PERMITTED_MARKERS)


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

# Messages.sdef exposes send's `to` parameter as participant OR chat and the
# chat's id as its GUID. Resolve that exact chat, never its latest participant.
_SEND_CHAT_SCRIPT = """
on run argv
    set chatGuid to item 1 of argv
    set messageBody to item 2 of argv
    tell application "Messages"
        set targetChat to chat id chatGuid
        send messageBody to targetChat
    end tell
end run
""".strip()


class MessagesAppSender(MessageSender):
    """Send through Apple's Messages scripting dictionary, never chat.db."""

    def __init__(
        self, *, timeout_s: float = 15.0,
        terminate_grace_s: float = 1.0, kill_grace_s: float = 1.0,
        preflight: bool = False, preflight_timeout_s: float = 3.0,
    ) -> None:
        for name, value in (
            ("timeout_s", timeout_s), ("terminate_grace_s", terminate_grace_s),
            ("kill_grace_s", kill_grace_s), ("preflight_timeout_s", preflight_timeout_s),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        self._timeout_s = timeout_s
        self._terminate_grace_s = terminate_grace_s
        self._kill_grace_s = kill_grace_s
        self._preflight = preflight
        self._preflight_timeout_s = preflight_timeout_s

    async def check_permission(self, timeout_s: float) -> AutomationState:
        """Whether this process may control Messages, by asking it something
        harmless. ``pending``: macOS is showing (or would show) the consent
        prompt and nobody answered in time. Run at start with a long timeout
        while the wearer is at the Mac; before a send with a short one."""
        try:
            process = await asyncio.create_subprocess_exec(
                "osascript", "-e", _PERMISSION_PROBE_SCRIPT,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError, RuntimeError):
            return "unavailable"
        communication = asyncio.create_task(process.communicate())
        try:
            _stdout, stderr = await asyncio.wait_for(asyncio.shield(communication), timeout=timeout_s)
        except asyncio.CancelledError:
            await self._cleanup_owned_process(process, communication)
            raise
        except Exception:
            _reaped, cancelled = await self._cleanup_owned_process(process, communication)
            if cancelled:
                raise asyncio.CancelledError from None
            return "pending"
        if process.returncode == 0:
            return "granted"
        return "denied" if _not_permitted(stderr) else "unavailable"

    async def send(self, recipient: str, text: str) -> None:
        await self._send(_SEND_SCRIPT, recipient, text, strip_recipient=True)

    async def send_chat(self, chat_guid: str, text: str) -> None:
        await self._send(_SEND_CHAT_SCRIPT, chat_guid, text, strip_recipient=False)

    async def _send(
        self, script: str, recipient: str, text: str, *, strip_recipient: bool,
    ) -> None:
        if (
            not isinstance(recipient, str) or not isinstance(text, str)
            or not recipient.strip() or not text.strip()
            or "\0" in recipient or "\0" in text
        ):
            raise MessageSendError(
                "recipient and message must be nonblank strings without NUL",
                possible_external_commit=False,
            )
        try:
            recipient.encode("utf-8")
            text.encode("utf-8")
        except UnicodeError as exc:
            raise MessageSendError(
                "recipient and message must contain valid Unicode",
                possible_external_commit=False,
            ) from exc
        # Preserve an existing chat's identity byte for byte, just like its
        # confirmed body. Only the legacy participant path trims user handles.
        target_id = recipient.strip() if strip_recipient else recipient
        # The confirmation binds the body byte for byte. Whitespace used only
        # for the nonblank check above must never change the submitted argv.
        body = text
        if self._preflight:
            # Nothing has been handed to Messages yet: a failure here is a
            # definite "not sent", shown at once instead of a stuck "sending".
            state = await self.check_permission(self._preflight_timeout_s)
            if state != "granted":
                reasons = {
                    "denied": f"The Mac is not allowed to control Messages. Allow it in {AUTOMATION_SETTINGS_PATH}.",
                    "pending": "macOS is asking on the Mac whether JOAT may control Messages. Answer it there, then send again.",
                    "unavailable": "Messages on the Mac did not answer.",
                }
                log.warning("messages automation %s — reply not sent (%s)", state, AUTOMATION_SETTINGS_PATH)
                raise MessageSendError(
                    reasons[state], possible_external_commit=False, code=f"automation-{state}",
                )
        try:
            process = await asyncio.create_subprocess_exec(
                "osascript",
                "-e",
                script,
                target_id,
                body,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            raise MessageSendError(
                "Messages executor could not start",
                possible_external_commit=False,
            ) from exc
        # Keep one pipe-draining task alive through deadline/caller cancellation.
        # Cancelling communicate alone neither terminates nor reaps its child.
        communication = asyncio.create_task(process.communicate())
        try:
            _stdout, stderr = await asyncio.wait_for(
                asyncio.shield(communication), timeout=self._timeout_s,
            )
        except asyncio.CancelledError:
            await self._cleanup_owned_process(process, communication)
            raise
        except Exception as exc:
            reaped, cancelled = await self._cleanup_owned_process(process, communication)
            if cancelled:
                raise asyncio.CancelledError from exc
            reason = "Messages executor timed out" if isinstance(exc, TimeoutError) else "Messages executor result could not be read"
            if not reaped:
                reason += "; owned child cleanup could not be confirmed"
            # After spawn, even a killed executor might already have handed
            # the message to Messages. Never turn this into an automatic retry.
            raise MessageSendError(reason, possible_external_commit=True, code="timeout") from exc
        if process.returncode != 0:
            if _not_permitted(stderr):
                # A refused Apple Event never reached Messages: definitely not sent.
                raise MessageSendError(
                    f"The Mac is not allowed to control Messages. Allow it in {AUTOMATION_SETTINGS_PATH}.",
                    possible_external_commit=False, code="automation-denied",
                )
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise MessageSendError(detail or "Messages rejected the send")

    async def _cleanup_owned_process(
        self, process: asyncio.subprocess.Process,
        communication: asyncio.Task[tuple[bytes, bytes]],
    ) -> tuple[bool, bool]:
        """Join cleanup even if shutdown requests cancellation a second time."""
        cleanup = asyncio.create_task(self._terminate_and_reap(process, communication))
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        return cleanup.result(), cancelled

    async def _terminate_and_reap(
        self, process: asyncio.subprocess.Process,
        communication: asyncio.Task[tuple[bytes, bytes]],
    ) -> bool:
        """Signal only this Process and wait for its exit while pipes drain."""
        try:
            for stop, grace in (
                (process.terminate, self._terminate_grace_s),
                (process.kill, self._kill_grace_s),
            ):
                if process.returncode is None:
                    try:
                        stop()
                    except ProcessLookupError:
                        pass  # The owned child exited between inspection and signal.
                    except OSError:
                        pass  # Still attempt the next escalation and honest wait.
                try:
                    await asyncio.wait_for(process.wait(), timeout=grace)
                    return True
                except (TimeoutError, OSError):
                    continue
            return process.returncode is not None
        finally:
            # wait() runs alongside communicate so a full output pipe cannot
            # stall child exit. Once cleanup's bounded waits end, release the
            # reader task as well; never leave its exception unobserved.
            if not communication.done():
                communication.cancel()
            await asyncio.gather(communication, return_exceptions=True)
