"""Conversational message preparation; the controller owns review and send."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict
from enum import StrEnum
from hashlib import sha256
import json
import logging
import re
from typing import Any

from ..services.message_reply import MessageActionStatus, MessageReplyController
from .tools import Tool


class MessageMode(StrEnum):
    NEW = "new"
    REPLY = "reply"


_ACTION_ID = re.compile(r"[a-zA-Z0-9._:-]{1,96}")
_RECIPIENT_REF = re.compile(r"[A-Za-z0-9_-]{1,128}")
log = logging.getLogger(__name__)


def _fields(arguments: dict[str, Any], *, required: set[str], allowed: set[str]) -> None:
    if not isinstance(arguments, dict) or not required <= set(arguments) <= allowed:
        raise ValueError("Missing required message fields or unsupported fields supplied.")


def _action_id(arguments: dict[str, Any]) -> str:
    value = arguments["action_id"]
    if not isinstance(value, str) or _ACTION_ID.fullmatch(value) is None:
        raise ValueError("Action ID must be 1 to 96 safe identifier characters.")
    return value


def _optional_text(arguments: dict[str, Any], key: str, maximum: int) -> str | None:
    if key not in arguments:
        return None
    value = arguments[key]
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{key} must contain between 1 and {maximum} text characters without null characters.")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{key} must be valid UTF-8 text.") from exc
    return value


def _next_step(status: MessageActionStatus) -> str:
    """What the mind should do with this result — said in the result itself.

    Observed live on the Codex seat: told "I don't have a verified contact,
    what is the name?", the model answered the wearer, then started a NEW
    prepare_message with the name, which the single-flight controller
    refused. The result now names the continuation, so no seat has to
    infer the protocol from a description it read three turns ago.
    """
    if status.state == "awaiting_input":
        if status.candidates:
            return (
                "Ask the wearer which one, then call provide_message_input with this same "
                "action_id and that candidate's recipient_ref. Do not start a new prepare_message."
            )
        if status.needs_input == "recipient":
            return (
                "Ask the wearer for the contact's name, then call provide_message_input with "
                "this same action_id and recipient_name. Do not start a new prepare_message."
            )
        if status.needs_input == "body":
            return (
                "Ask the wearer what to say, then call provide_message_input with this same "
                "action_id and their words as body, verbatim. Do not start a new prepare_message."
            )
        return "Call provide_message_input with this same action_id."
    if status.state == "awaiting_confirmation":
        return (
            "Nothing more from you: the wearer confirms or declines on the glasses. "
            "Tell them the message is ready to confirm."
        )
    if status.state == "sending":
        return "Nothing more from you; message_action_status reports the outcome."
    if status.state == "failed":
        return "This action is over; tell the wearer why. Start again only if they ask again."
    return ""


def _result(status: MessageActionStatus) -> str:
    payload = asdict(status)
    payload["next_step"] = _next_step(status)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def message_tools(
    replies: MessageReplyController,
    *,
    after_input: Callable[[MessageActionStatus], Awaitable[None]] | None = None,
) -> list[Tool]:
    """Expose typed preparation and status without a model approval switch.

    The controller retains action identity, confirmation work and send results.
    Recreating this adapter must not mint a different trace for the same action.
    Reply actions retain the trace of the controller's selected conversation.
    """

    async def mutation_result(status: MessageActionStatus) -> str:
        if after_input is not None:
            try:
                await after_input(status)
            except Exception as exc:
                # The controller has already performed its operation. A
                # failed prompt cannot turn that receipt into an invented
                # failed action or cause the caller to repeat a send.
                log.warning("message prompt callback failed error=%s", type(exc).__name__)
        return _result(status)

    async def prepare_message(arguments: dict[str, Any]) -> str:
        try:
            _fields(
                arguments, required={"mode", "action_id"},
                allowed={"mode", "action_id", "recipient_name", "body"},
            )
            identifier = _action_id(arguments)
            raw_mode = arguments["mode"]
            if not isinstance(raw_mode, str) or raw_mode not in MessageMode:
                raise ValueError("Message mode must be new or reply.")
            mode = MessageMode(raw_mode)
            name = _optional_text(arguments, "recipient_name", 200)
            text = _optional_text(arguments, "body", 2000)
            if mode == MessageMode.REPLY:
                if name is not None:
                    raise ValueError("A reply uses the selected Messages conversation; do not supply a recipient.")
                status = await replies.prepare_selected_reply(action_id=identifier, body=text)
            else:
                trace = "message-tool:" + sha256(identifier.encode("utf-8")).hexdigest()[:32]
                status = await replies.prepare_new_message(
                    action_id=identifier, trace_id=trace, recipient_name=name, body=text,
                )
            return await mutation_result(status)
        except (ValueError, RuntimeError) as exc:
            return f"error: {exc}"

    async def provide_message_input(arguments: dict[str, Any]) -> str:
        try:
            _fields(
                arguments, required={"action_id"},
                allowed={"action_id", "recipient_name", "recipient_ref", "body"},
            )
            identifier = _action_id(arguments)
            if len(arguments) == 1:
                raise ValueError("Provide a missing recipient name, candidate reference or message body.")
            name = _optional_text(arguments, "recipient_name", 200)
            reference = _optional_text(arguments, "recipient_ref", 128)
            text = _optional_text(arguments, "body", 2000)
            if name is not None and reference is not None:
                raise ValueError("Supply a contact name or a candidate reference, not both.")
            if reference is not None and _RECIPIENT_REF.fullmatch(reference) is None:
                raise ValueError("recipient_ref must be a controller-issued candidate reference.")
            status = await replies.provide_message_input(
                identifier, recipient_name=name, recipient_ref=reference, body=text,
            )
            return await mutation_result(status)
        except (ValueError, RuntimeError) as exc:
            return f"error: {exc}"

    async def message_action_status(arguments: dict[str, Any]) -> str:
        try:
            _fields(arguments, required={"action_id"}, allowed={"action_id"})
            return _result(replies.message_action_status(_action_id(arguments)))
        except (ValueError, RuntimeError) as exc:
            return f"error: {exc}"

    async def cancel_message_action(arguments: dict[str, Any]) -> str:
        try:
            _fields(arguments, required={"action_id"}, allowed={"action_id"})
            return await mutation_result(replies.cancel_message_action(_action_id(arguments)))
        except (ValueError, RuntimeError) as exc:
            return f"error: {exc}"

    action_id = {
        "type": "string", "minLength": 1, "maxLength": 96,
        "pattern": "^[a-zA-Z0-9._:-]+$",
        "description": "Unique logical message action ID; reuse the same ID and inputs for retries.",
    }
    recipient_name = {
        "type": "string", "minLength": 1, "maxLength": 200,
        "description": "A Mac contact name. Phone numbers and email addresses are not accepted.",
    }
    body = {
        "type": "string", "minLength": 1, "maxLength": 2000,
        "description": "Exact message text, preserving punctuation and whitespace; must fit glasses review.",
    }
    return [
        Tool(
            name="prepare_message",
            description=(
                "Prepare a new message by contact name, or reply to the selected Messages conversation. "
                "Omit missing recipient/body to ask for input. Returns action status promptly; "
                "only the wearer can confirm on the glasses. This tool cannot approve a send."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": [mode.value for mode in MessageMode]},
                    "action_id": action_id, "recipient_name": recipient_name, "body": body,
                },
                "required": ["mode", "action_id"], "additionalProperties": False,
            },
            run=prepare_message,
            # Positive phrase hints only govern the extra local tool-choice
            # pass, not CLI/MCP availability. A generic "tell" hint would also
            # slow "tell me a joke"; arbitrary "tell <name>" remains outside
            # this deliberately non-exhaustive local heuristic.
            decision_hints=(
                "message", "messages", "text", "texts", "send", "reply", "respond",
                "tell her", "tell him", "tell them",
            ),
        ),
        Tool(
            name="provide_message_input",
            description=(
                "Provide missing input for an existing message action. Choose a controller-issued "
                "recipient_ref or a contact name; never provide both or replace a bound recipient/body. "
                "The existing glasses confirmation still controls sending."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action_id": action_id, "recipient_name": recipient_name, "body": body,
                    "recipient_ref": {
                        "type": "string", "minLength": 1, "maxLength": 128,
                        "pattern": "^[A-Za-z0-9_-]+$",
                        "description": "An opaque candidate reference returned for this action.",
                    },
                },
                "required": ["action_id"],
                "anyOf": [{"required": [key]} for key in ("recipient_name", "recipient_ref", "body")],
                "additionalProperties": False,
            },
            run=provide_message_input,
            decision_hints=("message", "reply", "recipient", "contact", "draft"),
        ),
        Tool(
            name="message_action_status",
            description=(
                "Read a message action's current state without repeating confirmation or send. "
                "Sent means submitted to Messages, with delivery unverified. Actions are process-scoped."
            ),
            parameters={
                "type": "object", "properties": {"action_id": action_id},
                "required": ["action_id"], "additionalProperties": False,
            },
            run=message_action_status,
            decision_hints=("message", "messages", "sent", "submitted", "delivered", "reply"),
        ),
        Tool(
            name="cancel_message_action",
            description=(
                "Cancel an existing message action. Cancellation after submission starts cannot "
                "prove that nothing was sent; preserve the returned uncertainty and do not retry."
            ),
            parameters={
                "type": "object", "properties": {"action_id": action_id},
                "required": ["action_id"], "additionalProperties": False,
            },
            run=cancel_message_action,
            decision_hints=("cancel", "discard", "message", "reply", "draft"),
        ),
    ]
