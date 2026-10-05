"""Prompt wire contracts, separate from gateway ownership and paint proof.

The parser validates an acknowledgment's shape and internal epoch consistency.
Only the gateway can match it to the current authenticated pending request.
"""

from __future__ import annotations

from typing import Any

import pytest

from joat.core.protocol import (
    Audio,
    PromptAck,
    PromptCancel,
    PromptPresent,
    ProtocolError,
    parse_uplink,
)


PROMPT_ID = "prompt-token_1"
SESSION = "fixture-session"
BINDING = "0123456789abcdef" * 4
QUESTION = "  What should I say?\nCaf\u00e9, e\u0301, and \U0001f600 stay exact.  "


def present(**changes: Any) -> PromptPresent:
    return PromptPresent(**({
        "prompt_id": PROMPT_ID,
        "question": QUESTION,
        "presentation_binding": BINDING,
        "connection_generation": SESSION,
        "source": "glasses",
        "expires_in_ms": 45_000,
        "owner_kind": "messages",
        "owner_id": "message-action:1.retry_2",
        "field": "body",
    } | changes))


def acknowledgement(**changes: Any) -> dict[str, Any]:
    return {
        "kind": "prompt.ack",
        "promptId": PROMPT_ID,
        "presentationBinding": BINDING,
        "connectionGeneration": SESSION,
        "source": "glasses",
        "success": True,
        "audioEpoch": PROMPT_ID,
    } | changes


INVALID_TOKENS = [
    pytest.param(None, id="null"),
    pytest.param(True, id="bool"),
    pytest.param(3, id="number"),
    pytest.param([], id="array"),
    pytest.param({}, id="object"),
    pytest.param("", id="empty"),
    pytest.param(" ", id="whitespace"),
    pytest.param("token with space", id="embedded-space"),
    pytest.param("token/segment", id="slash"),
    pytest.param("token\x00tail", id="nul"),
    pytest.param("token\n", id="newline"),
    pytest.param("caf\u00e9", id="non-ascii"),
    pytest.param("token\ud800", id="surrogate"),
    pytest.param("x" * 129, id="too-long"),
]


def test_present_serializes_exact_question_and_ownership_without_extra_keys() -> None:
    assert present().to_wire() == {
        "kind": "prompt.present",
        "promptId": PROMPT_ID,
        "question": QUESTION,
        "presentationBinding": BINDING,
        "connectionGeneration": SESSION,
        "source": "glasses",
        "expiresInMs": 45_000,
        "ownerKind": "messages",
        "ownerId": "message-action:1.retry_2",
        "field": "body",
    }


@pytest.mark.parametrize(("owner", "field"), [
    ("messages", "recipient"), ("brainstorm", "topic"), ("terminal", "instruction"),
    # A tapped page row asking its own question ("Where to?").
    ("capability", "destination"),
])
@pytest.mark.parametrize("source", ["glasses", "phone"])
def test_present_supports_declared_owners_and_sources(owner: str, field: str, source: str) -> None:
    wire = present(owner_kind=owner, field=field, source=source).to_wire()
    assert (wire["ownerKind"], wire["field"], wire["source"]) == (owner, field, source)


@pytest.mark.parametrize("question", ["x" * 1000, "\U0001f600" * 500])
def test_question_limit_counts_utf16_units_including_non_bmp_characters(question: str) -> None:
    assert present(question=question).to_wire()["question"] == question


@pytest.mark.parametrize("question", [
    None, False, 42, [], {}, "", " \n\t", "private\x00question",
    "broken\ud800", "broken\udfff", "x" * 1001,
    "x" * 999 + "\U0001f600", "\U0001f600" * 501,
])
def test_present_rejects_blank_invalid_or_unreviewably_large_question(question: object) -> None:
    with pytest.raises(ProtocolError):
        present(question=question).to_wire()


@pytest.mark.parametrize("expiry", [1, 45_000])
def test_present_accepts_expiry_boundaries(expiry: int) -> None:
    assert present(expires_in_ms=expiry).to_wire()["expiresInMs"] == expiry


@pytest.mark.parametrize("expiry", [None, True, False, "1000", 1.0, 0, -1, 45_001, float("inf")])
def test_present_rejects_non_integer_or_out_of_range_expiry(expiry: object) -> None:
    with pytest.raises(ProtocolError):
        present(expires_in_ms=expiry).to_wire()


@pytest.mark.parametrize("name", ["prompt_id", "connection_generation"])
@pytest.mark.parametrize("value", INVALID_TOKENS)
def test_present_rejects_invalid_prompt_or_session_identity(name: str, value: object) -> None:
    with pytest.raises(ProtocolError):
        present(**{name: value}).to_wire()


@pytest.mark.parametrize(("name", "wire_name", "maximum"), [
    ("prompt_id", "promptId", 128),
    ("connection_generation", "connectionGeneration", 128),
    ("owner_id", "ownerId", 256),
    ("field", "field", 64),
])
def test_present_accepts_bounded_safe_identifier_at_limit(name: str, wire_name: str, maximum: int) -> None:
    value = "x" * maximum
    assert present(**{name: value}).to_wire()[wire_name] == value


@pytest.mark.parametrize(("name", "value"), [
    ("owner_id", None), ("owner_id", True), ("owner_id", ""),
    ("owner_id", "owner name"), ("owner_id", "../owner"),
    ("owner_id", "owner\x00"), ("owner_id", "\U0001f600"), ("owner_id", "x" * 257),
    ("field", None), ("field", False), ("field", ""), ("field", "body text"),
    ("field", "field/path"), ("field", "field\ud800"), ("field", "x" * 65),
    ("owner_kind", None), ("owner_kind", True), ("owner_kind", ""),
    ("owner_kind", "assistant"), ("owner_kind", "Messages"),
    ("source", None), ("source", True), ("source", ""), ("source", "microphone"),
])
def test_present_rejects_invalid_owner_field_or_source(name: str, value: object) -> None:
    with pytest.raises(ProtocolError):
        present(**{name: value}).to_wire()


@pytest.mark.parametrize("binding", [
    None, True, 64, [], "", "a" * 63, "a" * 65, "A" * 64,
    "g" * 64, " " * 64, "a" * 63 + "\x00",
])
def test_both_present_and_ack_require_canonical_sha256_binding(binding: object) -> None:
    with pytest.raises(ProtocolError):
        present(presentation_binding=binding).to_wire()
    with pytest.raises(ProtocolError):
        parse_uplink(acknowledgement(presentationBinding=binding))


def test_cancel_is_scoped_to_one_prompt_and_connection_and_contains_no_question() -> None:
    assert PromptCancel(PROMPT_ID, SESSION).to_wire() == {
        "kind": "prompt.cancel",
        "promptId": PROMPT_ID,
        "connectionGeneration": SESSION,
    }


def test_hub_cancel_parses_as_the_same_scoped_cancel_type() -> None:
    parsed = parse_uplink({
        "kind": "prompt.cancel",
        "promptId": PROMPT_ID,
        "connectionGeneration": SESSION,
    })
    assert isinstance(parsed, PromptCancel)
    assert parsed.prompt_id == PROMPT_ID and parsed.connection_generation == SESSION
    assert parsed.to_wire() == {
        "kind": "prompt.cancel",
        "promptId": PROMPT_ID,
        "connectionGeneration": SESSION,
    }


@pytest.mark.parametrize("missing", ["promptId", "connectionGeneration"])
def test_hub_cancel_requires_prompt_and_connection_identity(missing: str) -> None:
    wire = {"kind": "prompt.cancel", "promptId": PROMPT_ID, "connectionGeneration": SESSION}
    del wire[missing]
    with pytest.raises(ProtocolError):
        parse_uplink(wire)


@pytest.mark.parametrize("extra", ["question", "ownerId", "audioEpoch", "ingressFence", "reason"])
def test_hub_cancel_rejects_unrelated_or_private_fields(extra: str) -> None:
    with pytest.raises(ProtocolError):
        parse_uplink({
            "kind": "prompt.cancel", "promptId": PROMPT_ID,
            "connectionGeneration": SESSION, extra: "unexpected",
        })


@pytest.mark.parametrize("name", ["promptId", "connectionGeneration"])
@pytest.mark.parametrize("value", INVALID_TOKENS)
def test_hub_cancel_rejects_invalid_prompt_or_connection_identity(name: str, value: object) -> None:
    wire = {"kind": "prompt.cancel", "promptId": PROMPT_ID, "connectionGeneration": SESSION, name: value}
    with pytest.raises(ProtocolError):
        parse_uplink(wire)


def test_valid_foreign_hub_cancel_preserves_identity_for_gateway_matching() -> None:
    # Syntax validation cannot decide whether a cancellation owns the active
    # lease. Preserve the foreign identity so the gateway can reject it.
    parsed = parse_uplink({
        "kind": "prompt.cancel", "promptId": "other-prompt",
        "connectionGeneration": "other-session",
    })
    assert isinstance(parsed, PromptCancel)
    assert parsed.prompt_id == "other-prompt" and parsed.connection_generation == "other-session"


@pytest.mark.parametrize("name", ["prompt_id", "connection_generation"])
@pytest.mark.parametrize("value", INVALID_TOKENS)
def test_cancel_rejects_invalid_prompt_or_session_identity(name: str, value: object) -> None:
    arguments = {"prompt_id": PROMPT_ID, "connection_generation": SESSION, name: value}
    with pytest.raises(ProtocolError):
        PromptCancel(**arguments).to_wire()  # type: ignore[arg-type]


def test_successful_ack_parses_as_a_typed_receipt_not_a_confirmation() -> None:
    parsed = parse_uplink(acknowledgement())
    assert isinstance(parsed, PromptAck)
    assert parsed.prompt_id == PROMPT_ID
    assert parsed.presentation_binding == BINDING
    assert parsed.connection_generation == SESSION
    assert parsed.source == "glasses" and parsed.success is True
    assert parsed.audio_epoch == PROMPT_ID


@pytest.mark.parametrize("with_null_epoch", [False, True])
def test_failed_paint_can_report_failure_without_creating_an_audio_epoch(with_null_epoch: bool) -> None:
    wire = acknowledgement(success=False)
    if with_null_epoch:
        wire["audioEpoch"] = None
    else:
        del wire["audioEpoch"]
    parsed = parse_uplink(wire)
    assert isinstance(parsed, PromptAck)
    assert parsed.success is False and parsed.audio_epoch is None


@pytest.mark.parametrize("missing", [
    "promptId", "presentationBinding", "connectionGeneration", "source", "success", "audioEpoch",
])
def test_successful_ack_requires_all_binding_fields(missing: str) -> None:
    wire = acknowledgement()
    del wire[missing]
    with pytest.raises(ProtocolError):
        parse_uplink(wire)


@pytest.mark.parametrize("extra", ["question", "ownerId", "ingressFence", "accepted", "futureExtension"])
def test_ack_rejects_unsupported_fields_including_peer_supplied_host_fence(extra: str) -> None:
    with pytest.raises(ProtocolError):
        parse_uplink(acknowledgement(**{extra: "unexpected"}))


@pytest.mark.parametrize("success", [None, 0, 1, "true", "false", [], {}])
def test_ack_success_is_a_boolean_not_a_coerced_truth_value(success: object) -> None:
    with pytest.raises(ProtocolError):
        parse_uplink(acknowledgement(success=success))


@pytest.mark.parametrize("name", ["promptId", "connectionGeneration"])
@pytest.mark.parametrize("value", INVALID_TOKENS)
def test_ack_rejects_invalid_prompt_or_session_identity(name: str, value: object) -> None:
    wire = acknowledgement(**{name: value})
    if name == "promptId":
        wire["audioEpoch"] = value
    with pytest.raises(ProtocolError):
        parse_uplink(wire)


@pytest.mark.parametrize("epoch", [None, "", "another-valid-prompt", 1, True, "x" * 129])
def test_accepted_ack_requires_its_own_matching_audio_epoch(epoch: object) -> None:
    with pytest.raises(ProtocolError):
        parse_uplink(acknowledgement(audioEpoch=epoch))


def test_failed_ack_cannot_claim_an_active_audio_epoch() -> None:
    with pytest.raises(ProtocolError):
        parse_uplink(acknowledgement(success=False))


@pytest.mark.parametrize("source", [None, True, 1, "", "unknown", "Glasses"])
def test_ack_rejects_undeclared_source(source: object) -> None:
    with pytest.raises(ProtocolError):
        parse_uplink(acknowledgement(source=source))


@pytest.mark.parametrize("foreign_fields", [
    {"connectionGeneration": "another-session"},
    {"presentationBinding": "f" * 64},
    {"promptId": "another-prompt", "audioEpoch": "another-prompt"},
    {"source": "phone"},
])
def test_valid_foreign_ack_is_parseable_but_gateway_must_match_pending_context(
    foreign_fields: dict[str, str],
) -> None:
    # Parsing has no current request. Rejecting these valid wire receipts here
    # would hide the session/owner comparison the real gateway must perform.
    parsed = parse_uplink(acknowledgement(**foreign_fields))
    assert isinstance(parsed, PromptAck)
    assert parsed.connection_generation == foreign_fields.get("connectionGeneration", SESSION)
    assert parsed.presentation_binding == foreign_fields.get("presentationBinding", BINDING)
    assert parsed.prompt_id == foreign_fields.get("promptId", PROMPT_ID)
    assert parsed.source == foreign_fields.get("source", "glasses")


@pytest.mark.parametrize("source", ["glasses", "phone"])
def test_audio_preserves_valid_epoch_source_and_speaker_without_relabeling(source: str) -> None:
    parsed = parse_uplink({
        "kind": "audio", "source": source, "seq": 7,
        "pcm": [1, -1], "speaker": "other", "audioEpoch": "earlier-prompt",
    })
    assert isinstance(parsed, Audio)
    assert parsed.audio_epoch == "earlier-prompt"
    assert parsed.source == source and parsed.speaker == "other"
    assert parsed.seq == 7 and parsed.pcm == [1, -1]


def test_legacy_audio_without_epoch_remains_valid_without_prompt_admission() -> None:
    parsed = parse_uplink({
        "kind": "audio", "source": "glasses", "seq": 0, "pcm": [1, -1],
        "futureOptionalMetadata": {"value": 1},
    })
    assert isinstance(parsed, Audio)
    assert parsed.audio_epoch is None
    assert parsed.seq == 0 and parsed.pcm == [1, -1]


@pytest.mark.parametrize("epoch", INVALID_TOKENS)
def test_present_audio_epoch_must_be_a_safe_token_including_rejecting_explicit_null(epoch: object) -> None:
    with pytest.raises(ProtocolError):
        parse_uplink({
            "kind": "audio", "source": "glasses", "seq": 7,
            "pcm": [1, -1], "audioEpoch": epoch,
        })


def test_audio_accepts_maximum_epoch_token_without_truncation() -> None:
    epoch = "x" * 128
    parsed = parse_uplink({
        "kind": "audio", "source": "glasses", "seq": 7,
        "pcm": [1, -1], "audioEpoch": epoch,
    })
    assert isinstance(parsed, Audio) and parsed.audio_epoch == epoch
