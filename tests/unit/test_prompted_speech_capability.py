"""A tapped page row asks its own question through the acknowledged prompt."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from joat.core.interfaces import Transcript
from joat.core.protocol import PromptAck, PromptPresent
from joat.services.hud import HudWriter
from joat.services.message_reply import MessageReplyController
from joat.services.prompted_input import UtteranceProvenance
from joat.services.prompted_speech import PromptedSpeechRuntime


class FakePromptTransport:
    """Answers every presentation at once, as a lens that painted it would."""

    def __init__(self) -> None:
        self.presented: list[PromptPresent] = []
        self.cancelled: list[str | None] = []

    async def request_prompt(
        self, request: PromptPresent, *, on_presented: Callable[[PromptAck], bool],
        on_revoked: Callable[[], None] | None = None, timeout_s: float = 5.0,
    ) -> bool:
        self.presented.append(request)
        return on_presented(PromptAck(
            prompt_id=request.prompt_id, presentation_binding=request.presentation_binding,
            connection_generation=request.connection_generation, source=request.source,
            success=True, audio_epoch=request.prompt_id,
        ))

    async def cancel_prompt(self, prompt_id: str | None = None) -> bool:
        self.cancelled.append(prompt_id)
        return True

    async def send(self, message: object) -> None:
        return None


def runtime(transport: FakePromptTransport) -> PromptedSpeechRuntime:
    replies = MessageReplyController(sender=None, confirm=None)  # type: ignore[arg-type]
    return PromptedSpeechRuntime(
        HudWriter(transport), replies,  # type: ignore[arg-type]
        sessions=lambda: None,  # type: ignore[arg-type, return-value]
        connection_generation=lambda: "gen-1",
        fence_audio=lambda: 0, capturing=lambda: False,
    )


def spoken(prompt_id: str, text: str) -> tuple[Transcript, UtteranceProvenance]:
    return Transcript(text=text, is_final=True), UtteranceProvenance(
        source="glasses", connection_generation="gen-1", audio_epoch=prompt_id,
        first_ingress_ordinal=1, last_ingress_ordinal=1, speaker_roles=frozenset(),
    )


@pytest.mark.asyncio
async def test_a_capability_question_is_published_and_its_answer_routed_once() -> None:
    transport = FakePromptTransport()
    speech = runtime(transport)
    answers: list[str] = []

    async def consume(text: str) -> None:
        answers.append(text)

    assert await speech.ask_capability("Where to?", "destination", consume) is True
    request = transport.presented[-1]
    assert (request.owner_kind, request.field, request.question) == ("capability", "destination", "Where to?")
    assert request.owner_id.startswith("capability-")

    transcript, provenance = spoken(request.prompt_id, "the library")
    assert await speech.dispatcher.dispatch(transcript, provenance) is True
    assert answers == ["the library"]
    # Consumed means retired: the same words cannot be admitted twice.
    assert await speech.dispatcher.dispatch(transcript, provenance) is False
    assert speech.dispatcher.current_ticket is None


@pytest.mark.asyncio
async def test_a_newer_question_replaces_the_older_one() -> None:
    transport = FakePromptTransport()
    speech = runtime(transport)
    first: list[str] = []
    second: list[str] = []

    async def take_first(text: str) -> None:
        first.append(text)

    async def take_second(text: str) -> None:
        second.append(text)

    assert await speech.ask_capability("Where to?", "destination", take_first) is True
    older = transport.presented[-1]
    assert await speech.ask_capability("What should I note?", "note", take_second) is True
    newer = transport.presented[-1]
    assert newer.prompt_id != older.prompt_id

    assert await speech.dispatcher.dispatch(*spoken(older.prompt_id, "stale")) is False
    assert await speech.dispatcher.dispatch(*spoken(newer.prompt_id, "buy filters")) is True
    assert (first, second) == ([], ["buy filters"])


@pytest.mark.asyncio
async def test_no_audio_generation_means_no_question() -> None:
    transport = FakePromptTransport()
    speech = PromptedSpeechRuntime(
        HudWriter(transport), MessageReplyController(sender=None, confirm=None),  # type: ignore[arg-type]
        sessions=lambda: None,  # type: ignore[arg-type, return-value]
        connection_generation=lambda: None,
        fence_audio=lambda: 0, capturing=lambda: False,
    )

    async def consume(text: str) -> None:
        raise AssertionError("nothing to consume")

    assert await speech.ask_capability("Where to?", "destination", consume) is False
    assert transport.presented == []
    await asyncio.sleep(0)
