"""The spoken opener must survive Whisper's punctuation.

The Brainstorm surface tells the wearer: Say "JOAT, brainstorm …" to
begin. The live transcript of exactly that phrase is
"JOAT. Brainstorm. Should a wearable assistant cache embeddings on
disk?" — Whisper punctuates, and the capability name arrives with a full
stop every time. The old delimiter grammar (colon, comma, or space)
therefore NEVER matched: this engine's entire log holds zero
voice-started sessions and 25 chat replies where a Brainstorm was asked
for. The wearer was following instructions that could not work.
"""

from __future__ import annotations

from joat.services.session import SessionManager


class FakeHud:
    async def show(self, *a: object, **k: object) -> None: ...
    async def show_brainstorm(self, *a: object, **k: object) -> None: ...


def manager(record: list[str]) -> SessionManager:
    def factory(topic: str) -> None:
        record.append(topic)
        return None  # no session object needed; the CLAIM is the point
    return SessionManager(FakeHud(), factory)  # type: ignore[arg-type]


class TestTheSpokenOpener:
    def test_whispers_punctuation_reaches_the_factory(self) -> None:
        """The trigger's whole job: the phrase becomes a topic handed to
        the session factory. (The factory here returns None, so the
        manager reports not-started — session construction is the
        factory's concern, not the grammar's.)"""
        for said in (
            "Brainstorm. Should a wearable assistant cache embeddings on disk?",
            "brainstorm, compare these",
            "Brainstorm: the plan",
            "brainstorm the plan for tomorrow",
            "Debate. Tabs or spaces?",
        ):
            topics: list[str] = []
            manager(topics).handle_transcript(said)
            assert topics, said

    def test_the_word_inside_a_sentence_does_not(self) -> None:
        for said in (
            "brainstorming is fun",
            "we should brainstorm later",
            "brainstorms happen",
        ):
            topics: list[str] = []
            assert manager(topics).handle_transcript(said) is False, said
            assert topics == [], said

    def test_the_topic_survives_the_stripping(self) -> None:
        topics: list[str] = []
        manager(topics).handle_transcript(
            "Brainstorm. Should a wearable assistant cache embeddings on disk?"
        )
        assert topics == ["Should a wearable assistant cache embeddings on disk"]
