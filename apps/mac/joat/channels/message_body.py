"""Bounded, data-only text recovery from Messages' attributedBody archives.

The pinned pytypedstream dependency reads the archive as Python data. It does
not instantiate Objective-C classes or run an NSUnarchiver on message bytes.
Only the attributed string's first NSString is message text; its formatting
and attachment metadata must never be mistaken for additional body text.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from typedstream.archiving import GenericArchivedObject, TypedValue, Unarchiver
from typedstream.stream import (
    BeginArray, BeginObject, BeginStruct, EndArray, EndObject, EndStruct,
    TypedStreamReader,
)
from typedstream.types.foundation import NSString

# JOAT resource budgets, not limits imposed by Messages or the glasses.
MAX_ATTRIBUTED_BODY_BYTES = 256 * 1024
MAX_ARCHIVE_EVENTS = 32_768
MAX_ARCHIVE_DEPTH = 64
MESSAGE_TEXT_UNAVAILABLE = "Message text unavailable"


@dataclass(frozen=True, slots=True)
class MessageBody:
    text: str
    source: Literal["plain", "attributed", "empty", "unavailable"]


class _BoundedEvents(Iterator[object]):
    """Limit decoder work without creating an uncancellable timeout thread."""

    def __init__(self, reader: Iterator[object]) -> None:
        self._reader = reader
        self._count = 0
        self._depth = 0

    def __next__(self) -> object:
        if self._count >= MAX_ARCHIVE_EVENTS:
            raise ValueError("Messages archive event budget exceeded")
        event = next(self._reader)
        self._count += 1
        if isinstance(event, (BeginObject, BeginArray, BeginStruct)):
            self._depth += 1
            if self._depth > MAX_ARCHIVE_DEPTH:
                raise ValueError("Messages archive nesting budget exceeded")
        elif isinstance(event, (EndObject, EndArray, EndStruct)):
            self._depth -= 1
        return event


def decode_message_body(
    plain_text: object, attributed_body: object, *, attributed_size: int | None = None,
) -> MessageBody:
    """Preserve plain text first; return an honest placeholder on decode failure.

    SQL supplies attributed_size when an oversized blob was deliberately not
    materialized. That omission must not be confused with a genuinely empty
    message. No exception text, archive bytes or decoded body is logged.
    """
    if isinstance(plain_text, str) and plain_text != "":
        return MessageBody(plain_text, "plain")
    unavailable = MessageBody(MESSAGE_TEXT_UNAVAILABLE, "unavailable")
    if attributed_size is not None and attributed_size > MAX_ATTRIBUTED_BODY_BYTES:
        return unavailable
    if attributed_body is None or attributed_body == b"":
        return unavailable if attributed_size else MessageBody("", "empty")
    if not isinstance(attributed_body, bytes) or len(attributed_body) > MAX_ATTRIBUTED_BODY_BYTES:
        return unavailable
    try:
        with TypedStreamReader.from_data(attributed_body) as reader:
            # Unarchiver only consumes next(reader); the outer context owns
            # closing the underlying byte stream on success and failure.
            with Unarchiver(_BoundedEvents(reader)) as archive:
                root = archive.decode_single_root()
        if not isinstance(root, GenericArchivedObject):
            return unavailable
        if root.clazz.name not in (b"NSAttributedString", b"NSMutableAttributedString"):
            return unavailable
        if not root.contents:
            return unavailable
        first = root.contents[0]
        if not isinstance(first, TypedValue) or not isinstance(first.value, NSString):
            return unavailable
        text = first.value.value
        if not isinstance(text, str):
            return unavailable
        # These NSStrings can be object placeholders, not message prose.
        # Keep real text (including inline markers) exact, but let the reader's
        # existing attachment metadata label a marker-only attachment.
        visible = text.replace("\ufffc", "").strip()
        if not visible:
            return MessageBody("", "empty")
        if not visible.replace("\ufffd", "").strip():
            return unavailable
        return MessageBody(text, "attributed")
    except Exception:
        # Unsupported classes, bad references, truncation, invalid UTF-8 and
        # exhausted budgets affect one row, never the rest of a conversation.
        return unavailable
