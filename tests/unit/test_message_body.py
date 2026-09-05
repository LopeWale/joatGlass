"""Text recovery from public archives, with explicit malformed-input bounds."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from joat.channels import message_body
from joat.channels.message_body import (
    MAX_ATTRIBUTED_BODY_BYTES,
    MESSAGE_TEXT_UNAVAILABLE,
    decode_message_body,
)

FIXTURES = Path(__file__).parents[1] / "fixtures/messages/typedstream"
EXPECTED: dict[str, str] = json.loads((FIXTURES / "expected.json").read_text())


def _short_archived_text(text: str) -> bytes:
    """Replace only the empty NSString in the public Blank archive."""
    payload = text.encode("utf-8")
    assert len(payload) < 128
    blank = (FIXTURES / "Blank").read_bytes()
    needle = b"\x84\x01+\x00"
    assert blank.count(needle) == 1
    return blank.replace(needle, b"\x84\x01+" + bytes([len(payload)]) + payload, 1)


@pytest.mark.parametrize("name", list(EXPECTED))
def test_public_archive_recovers_exact_complete_text(name: str) -> None:
    blob = (FIXTURES / name).read_bytes()
    hashes = json.loads((FIXTURES / "sha256.json").read_text())
    assert hashlib.sha256(blob).hexdigest() == hashes[name]

    result = decode_message_body(None, blob)

    assert result.text == EXPECTED[name]
    assert result.source == ("attributed" if EXPECTED[name] else "empty")


@pytest.mark.parametrize("text", ["\ufffc", " \ufffc\n\ufffc\t", " \n\t"])
def test_object_markers_and_whitespace_are_not_recovered_prose(text: str) -> None:
    result = decode_message_body(None, _short_archived_text(text))
    assert result.text == ""
    assert result.source == "empty"


@pytest.mark.parametrize("text", ["\ufffd", " \ufffd\n\ufffd", "\ufffc \ufffd"])
def test_replacement_markers_without_readable_text_are_unavailable(text: str) -> None:
    result = decode_message_body(None, _short_archived_text(text))
    assert result.text == MESSAGE_TEXT_UNAVAILABLE
    assert result.source == "unavailable"


@pytest.mark.parametrize("text", [" \ufffcPhoto caption\n ", "Text with \ufffd and \ufffc inline"])
def test_real_archive_text_preserves_inline_markers_and_spacing(text: str) -> None:
    result = decode_message_body(None, _short_archived_text(text))
    assert result.text == text
    assert result.source == "attributed"


@pytest.mark.parametrize("plain", ["  exact\n spaces 🥽  ", " \n "])
def test_plain_text_is_verbatim_and_never_depends_on_archive(plain: str) -> None:
    result = decode_message_body(plain, b"malformed", attributed_size=10**8)
    assert result.text == plain
    assert result.source == "plain"


@pytest.mark.parametrize("plain", [None, ""])
def test_empty_plain_field_uses_archive(plain: str | None) -> None:
    result = decode_message_body(plain, (FIXTURES / "AttributedBodyTextOnly").read_bytes())
    assert result.text == "Noter test"
    assert result.source == "attributed"


@pytest.mark.parametrize("blob", [None, b""])
def test_absent_archive_is_empty_not_corrupt(blob: bytes | None) -> None:
    result = decode_message_body(None, blob)
    assert result.text == ""
    assert result.source == "empty"


@pytest.mark.parametrize("blob", [
    b"not an archive with a misleading printable message",
    b"bplist00keyed archive is a different format",
    # Public pytypedstream README's standalone NSString, not an attributed root.
    b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x08NSString\x01"
    b"\x84\x84\x08NSObject\x00\x85\x84\x01+\x0cstring value\x86",
    "wrong SQLite storage type",
])
def test_malformed_or_wrong_root_never_becomes_printable_archive_noise(blob: object) -> None:
    result = decode_message_body(None, blob)
    assert result.text == MESSAGE_TEXT_UNAVAILABLE
    assert result.source == "unavailable"


@pytest.mark.parametrize("cut", [1, 15, 45, -1])
def test_truncated_archive_is_unavailable(cut: int) -> None:
    blob = (FIXTURES / "EmojiBoldUnderline").read_bytes()
    result = decode_message_body(None, blob[:cut])
    assert result.text == MESSAGE_TEXT_UNAVAILABLE
    assert result.source == "unavailable"


def test_size_limit_handles_both_direct_blob_and_sql_omission() -> None:
    for blob, size in [(b"x" * (MAX_ATTRIBUTED_BODY_BYTES + 1), None),
                       (None, MAX_ATTRIBUTED_BODY_BYTES + 1)]:
        result = decode_message_body(None, blob, attributed_size=size)
        assert result.source == "unavailable"
        assert result.text == MESSAGE_TEXT_UNAVAILABLE


@pytest.mark.parametrize("limit_name,value", [("MAX_ARCHIVE_EVENTS", 5), ("MAX_ARCHIVE_DEPTH", 1)])
def test_decode_enforces_event_and_nesting_budget(
    monkeypatch: pytest.MonkeyPatch, limit_name: str, value: int,
) -> None:
    monkeypatch.setattr(message_body, limit_name, value)
    result = decode_message_body(None, (FIXTURES / "MultiPart").read_bytes())
    assert result.source == "unavailable"
    assert result.text == MESSAGE_TEXT_UNAVAILABLE
