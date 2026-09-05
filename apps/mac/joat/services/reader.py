"""Shared exact-text display pages for the wearable readers."""

from __future__ import annotations

import re
import unicodedata

_ROW_COLUMNS = 62
_PAGE_ROWS = 10


def _columns(char: str) -> int:
    if char == "\t":
        # Reserve a full conventional tab stop regardless of its position.
        return 8
    return 2 if ord(char) > 0xFFFF or unicodedata.east_asian_width(char) in "WF" else 1


def _continues_group(text: str, boundary: int, row_start: int) -> bool:
    char = text[boundary]
    if (
        unicodedata.category(char).startswith("M")
        or char == "\u200d"
        or text[boundary - 1] == "\u200d"
        or 0x1F3FB <= ord(char) <= 0x1F3FF
    ):
        return True
    # Regional indicators form flag pairs; do not strand half a pair.
    if 0x1F1E6 <= ord(char) <= 0x1F1FF:
        previous = boundary - 1
        while previous >= row_start and 0x1F1E6 <= ord(text[previous]) <= 0x1F1FF:
            previous -= 1
        return (boundary - previous - 1) % 2 == 1
    return False


def paginate_reader_text(text: str) -> tuple[tuple[str, ...], ...]:
    """Lay out exact source slices in complete ten-row reader pages.

    Each body row costs at most 62 conservative mono columns. Wide and
    astral characters cost two, tabs eight, and every other code point one.
    This also reserves two of the SDK's 64 UTF-16 units for an original CRLF.
    CRLF, CR, and LF are physical row endings. They and all other whitespace
    remain in the source slices, so concatenation is lossless.

    Prefer word, combining, and joined-emoji boundaries. A group larger than
    the hard row budget must be split; that fallback preserves every Unicode
    code point rather than deleting text or exceeding the wire limit.
    """
    rows: list[str] = []
    for paragraph in re.findall(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+$", text) or [""]:
        ending = "\r\n" if paragraph.endswith("\r\n") else (
            paragraph[-1:] if paragraph.endswith(("\r", "\n")) else ""
        )
        stop = len(paragraph) - len(ending)
        if stop == 0:
            rows.append(paragraph)
            continue
        start = 0
        while start < stop:
            columns = 0
            end = start
            while end < stop:
                width = _columns(paragraph[end])
                if columns + width > _ROW_COLUMNS:
                    break
                columns += width
                end += 1
            if end == stop:
                rows.append(paragraph[start:])
                break
            boundary = paragraph.rfind(" ", start, end)
            if boundary > start:
                end = boundary + 1
            safe_end = end
            while safe_end > start and _continues_group(paragraph, safe_end, start):
                safe_end -= 1
            # An oversized group still makes progress without losing a code
            # point. Normal groups move intact to the next row.
            end = safe_end if safe_end > start else end
            rows.append(paragraph[start:end])
            start = end
    return tuple(tuple(rows[index:index + _PAGE_ROWS])
                 for index in range(0, len(rows), _PAGE_ROWS))
