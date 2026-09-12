"""Bounded, lossless retained messages for an agent conversation.

Display wrapping is a client concern. Retention evicts whole old messages,
never an arbitrary suffix of the message a user is trying to read.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import uuid4
from typing import Any, cast

TERMINAL_MESSAGE_LIMIT = 64
TERMINAL_HISTORY_BYTES = 262144
TerminalRole = Literal["user", "assistant", "status"]


@dataclass(frozen=True, slots=True)
class TerminalMessage:
    id: str
    role: TerminalRole
    text: str


class TerminalConversation:
    """One in-memory conversation; identity survives view and task changes."""

    def __init__(self) -> None:
        self.id = str(uuid4())
        self._messages: list[TerminalMessage] = []
        self._bytes = 0
        self._sequence = 0
        self.truncated = False

    @property
    def messages(self) -> tuple[TerminalMessage, ...]:
        return tuple(self._messages)

    def snapshot(self) -> dict[str, Any]:
        return {"id": self.id, "sequence": self._sequence, "truncated": self.truncated,
                "messages": [{"id": m.id, "role": m.role, "text": m.text} for m in self._messages]}

    @classmethod
    def restore(cls, value: object) -> TerminalConversation:
        if not isinstance(value, dict) or set(value) != {"id", "sequence", "truncated", "messages"}:
            raise ValueError("Invalid terminal conversation checkpoint")
        identity, sequence = value["id"], value["sequence"]
        if (not isinstance(identity, str) or not identity or len(identity) > 64
                or type(sequence) is not int or sequence < 0 or type(value["truncated"]) is not bool
                or not isinstance(value["messages"], list) or len(value["messages"]) > TERMINAL_MESSAGE_LIMIT):
            raise ValueError("Invalid terminal conversation checkpoint")
        restored = cls()
        restored.id, restored._sequence, restored.truncated = identity, sequence, value["truncated"]
        previous = 0
        for raw in value["messages"]:
            if (not isinstance(raw, dict) or set(raw) != {"id", "role", "text"}
                    or raw["role"] not in {"user", "assistant", "status"}
                    or not isinstance(raw["id"], str) or not raw["id"].startswith(identity + ":")
                    or not isinstance(raw["text"], str)):
                raise ValueError("Invalid terminal retained message")
            suffix = raw["id"][len(identity) + 1:]
            if not suffix.isascii() or not suffix.isdigit() or not previous < int(suffix) <= sequence:
                raise ValueError("Invalid terminal retained sequence")
            previous = int(suffix)
            restored._bytes += cls._size(raw["text"])
            restored._messages.append(TerminalMessage(raw["id"], cast(TerminalRole, raw["role"]), raw["text"]))
        if restored._bytes > TERMINAL_HISTORY_BYTES:
            raise ValueError("Oversized terminal retained conversation")
        return restored

    def append(self, role: TerminalRole, text: str) -> str:
        size = self._size(text)
        self._sequence += 1
        identity = f"{self.id}:{self._sequence}"
        self._messages.append(TerminalMessage(identity, role, text))
        self._bytes += size
        self._trim()
        return identity

    def replace(self, identity: str, text: str) -> None:
        size = self._size(text)
        for index, old in enumerate(self._messages):
            if old.id == identity:
                self._messages[index] = TerminalMessage(identity, old.role, text)
                self._bytes += size - len(old.text.encode("utf-8"))
                self._trim()
                return
        raise ValueError("Terminal message no longer retained")

    def remove(self, identity: str) -> None:
        for index, message in enumerate(self._messages):
            if message.id == identity:
                self._bytes -= len(message.text.encode("utf-8"))
                self._messages.pop(index)
                return

    @staticmethod
    def _size(text: str) -> int:
        size = len(text.encode("utf-8"))
        if size > TERMINAL_HISTORY_BYTES:
            raise ValueError("Agent reply exceeds the retained conversation limit")
        return size

    def _trim(self) -> None:
        while len(self._messages) > TERMINAL_MESSAGE_LIMIT or self._bytes > TERMINAL_HISTORY_BYTES:
            old = self._messages.pop(0)
            self._bytes -= len(old.text.encode("utf-8"))
            self.truncated = True
