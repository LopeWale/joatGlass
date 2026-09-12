"""Acceptance-oriented retention checks: exact prose survives to the wire."""
import pytest

from joat.core.protocol import AgentTerminalHudView, ProtocolError
from joat.core.terminal_conversation import (
    TERMINAL_HISTORY_BYTES, TERMINAL_MESSAGE_LIMIT, TerminalConversation, TerminalMessage,
)


def wire(conversation: TerminalConversation) -> dict:
    return AgentTerminalHudView(
        state="ready", focus="OpenClaw", lanes=[], activity="compatibility preview",
        conversation_id=conversation.id, messages=conversation.messages,
        history_truncated=conversation.truncated,
    ).to_wire()["terminal"]


def test_full_unicode_paragraphs_survive_retention_and_wire() -> None:
    conversation = TerminalConversation()
    prose = "BEGIN " + "A long paragraph — café 🧠 " * 350 + "\n\nMIDDLE\n\n" + "Second paragraph. " * 350 + " END"
    conversation.append("user", "Read the complete result")
    identity = conversation.append("assistant", prose)
    result = wire(conversation)
    assert result["messages"][1] == {"id": identity, "role": "assistant", "text": prose}
    assert result["conversationId"] == conversation.id
    assert result["historyTruncated"] is False


def test_whole_message_retention_reports_eviction_without_cutting_latest() -> None:
    conversation = TerminalConversation()
    first = conversation.append("assistant", "x" * 100000)
    latest = "🧠" * 60000
    identity = conversation.append("assistant", latest)
    assert conversation.truncated
    assert conversation.messages == (TerminalMessage(identity, "assistant", latest),)
    assert first != identity
    assert wire(conversation)["messages"][0]["text"] == latest


def test_append_updates_keep_identity_and_evict_old_messages() -> None:
    conversation = TerminalConversation()
    for index in range(TERMINAL_MESSAGE_LIMIT):
        conversation.append("user", str(index))
    identity = conversation.append("assistant", "partial")
    conversation.replace(identity, "partial\ncompleted")
    assert len(conversation.messages) == TERMINAL_MESSAGE_LIMIT
    assert conversation.truncated
    assert conversation.messages[-1] == TerminalMessage(identity, "assistant", "partial\ncompleted")


def test_oversize_message_is_refused_without_corrupting_retained_text() -> None:
    conversation = TerminalConversation()
    identity = conversation.append("assistant", "previous")
    with pytest.raises(ValueError):
        conversation.replace(identity, "x" * (TERMINAL_HISTORY_BYTES + 1))
    assert conversation.messages[-1].text == "previous"


def test_protocol_rejects_duplicate_identity_or_unbounded_history() -> None:
    for entries in [
        (TerminalMessage("same", "user", "a"), TerminalMessage("same", "assistant", "b")),
        (TerminalMessage("a", "user", "🧠" * 40000), TerminalMessage("b", "assistant", "🧠" * 40000)),
    ]:
        with pytest.raises(ProtocolError):
            AgentTerminalHudView(state="ready", focus="OpenClaw", lanes=[], activity="",
                                 conversation_id="test", messages=entries).to_wire()


def test_protocol_rejects_orphan_messages_and_invalid_unicode() -> None:
    with pytest.raises(ProtocolError):
        AgentTerminalHudView(state="ready", focus="OpenClaw", lanes=[], activity="",
                             messages=(TerminalMessage("a", "user", "orphan"),)).to_wire()
    with pytest.raises(ProtocolError):
        AgentTerminalHudView(state="ready", focus="OpenClaw", lanes=[], activity="",
                             conversation_id="test", messages=(TerminalMessage("a", "user", "\ud800"),)).to_wire()
