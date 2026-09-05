"""The reader wire contract preserves source text within a complete screen."""

from __future__ import annotations

import unicodedata

import pytest

from joat.core.protocol import HudList
from joat.services.reader import paginate_reader_text


def columns(text: str) -> int:
    return sum(
        8 if char == "\t" else 2
        if ord(char) > 0xFFFF or unicodedata.east_asian_width(char) in "WF" else 1
        for char in text
    )


@pytest.mark.parametrize(("source", "first"), [
    pytest.param("x" * 70, "x" * 62, id="long-word"),
    pytest.param("word " * 30, "word " * 12, id="whole-words"),
    pytest.param("漢" * 40, "漢" * 31, id="wide-cjk"),
    pytest.param("😀" * 40, "😀" * 31, id="astral-code-points"),
    pytest.param("x" * 62 + "\r\nZ", "x" * 62 + "\r\n", id="crlf"),
    pytest.param("\t" * 10, "\t" * 7, id="conservative-tabs"),
    pytest.param("A" * 61 + "e\u0301Z", "A" * 61, id="combining-boundary"),
    pytest.param("A" * 59 + "👩\u200d💻Z", "A" * 59, id="joined-emoji-boundary"),
])
def test_prepared_reader_rows_use_full_safe_width(source: str, first: str) -> None:
    pages = paginate_reader_text(source)
    assert pages[0][0] == first
    assert "".join(row for page in pages for row in page) == source


@pytest.mark.parametrize("source", [
    pytest.param("x" * 2500 + "FINAL", id="long-unbroken-value"),
    pytest.param("漢" * 440 + "FINAL", id="cjk-article"),
    pytest.param("😀" * 320 + "FINAL", id="emoji-message"),
    pytest.param(("w" * 31 + " ") * 40 + "FINAL", id="word-wrap-line-budget"),
    pytest.param("  Start\r\n\r\n" + "a\tb  c \n" * 80 + "FINAL\r\n  ", id="whitespace"),
    pytest.param("e\u0301 👩\u200d💻 🇳🇬 " * 80 + "FINAL", id="combined-unicode"),
    pytest.param("e" + "\u0301" * 150 + "FINAL", id="oversized-combining-group"),
    pytest.param("👩\u200d" * 50 + "💻FINAL", id="oversized-joined-group"),
])
def test_every_reader_character_survives_serialized_ten_row_pages(source: str) -> None:
    pages = paginate_reader_text(source)
    assert len(pages) >= 1
    recovered: list[str] = []
    for index, page in enumerate(pages):
        assert 1 <= len(page) <= 10
        assert all(columns(row.removesuffix("\r\n").removesuffix("\r").removesuffix("\n")) <= 62
                   for row in page)
        assert all(len(row.encode("utf-16-le")) // 2 <= 64 for row in page)
        message = HudList(items=[f"{index + 1}/{len(pages)} · source", *page])
        wire = message.to_wire()
        assert wire["items"] == message.items
        recovered.extend(wire["items"][1:])
    assert "".join(recovered) == source
    assert "FINAL" in "".join(pages[-1])


def test_reader_keeps_explicit_blank_rows_and_empty_source() -> None:
    assert paginate_reader_text("") == (("",),)
    assert paginate_reader_text("\r\n\n\r") == (("\r\n", "\n", "\r"),)
    assert paginate_reader_text(" \t ") == ((" \t ",),)


def test_tenth_display_row_is_the_page_boundary_not_a_character_estimate() -> None:
    source = ("x" * 62 + "\r\n") * 11
    pages = paginate_reader_text(source)
    assert tuple(map(len, pages)) == (10, 1)
    assert all(row == "x" * 62 + "\r\n" for page in pages for row in page)


def test_oversized_grapheme_fallback_preserves_code_points_without_replacement() -> None:
    # A group exceeding the hard wire cap cannot remain atomic. Splitting it
    # is explicit fallback; the complete sequence must still be recoverable.
    source = "e" + "\u0301" * 80 + "👩\u200d" * 40 + "💻"
    rows = [row for page in paginate_reader_text(source) for row in page]
    assert len(rows) > 1
    assert all(len(row.encode("utf-16-le")) // 2 <= 64 for row in rows)
    assert "".join(rows) == source
    assert "�" not in "".join(rows)
