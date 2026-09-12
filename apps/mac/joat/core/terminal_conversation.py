"""Bounded, lossless retained messages for an agent conversation.

Display wrapping is a client concern. Retention evicts whole old messages,
never an arbitrary suffix of the message a user is trying to read.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

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
