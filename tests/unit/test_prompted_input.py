"""Prompt admission contracts; fixtures do not imply optical or biometric proof."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace

import pytest

from joat.core.interfaces import Transcript
from joat.services.prompted_input import (
    PromptedInputDispatcher,
    PromptTicket,
    UtteranceProvenance,
)


class PromptFixture:
    def __init__(self) -> None:
        self.now = 100.0
        self.valid = True
        self.received: list[str] = []
        self.dispatcher = PromptedInputDispatcher(monotonic=lambda: self.now)

    async def consume(self, text: str) -> None:
        self.received.append(text)

    def begin(self, *, owner_id: str = "action-1", field: str = "body") -> PromptTicket:
        return self.dispatcher.begin_publication(
            owner_kind="messages", owner_id=owner_id, field=field,
            source="glasses", connection_generation="audio-session-1",
            presentation_binding=f"{owner_id}:{field}:preview-1", ttl_s=60.0,
            is_valid=lambda: self.valid, consume=self.consume,
        )

    def acknowledge(self, ticket: PromptTicket, *, fence: int = 10) -> bool:
        return self.dispatcher.acknowledge(
            ticket.prompt_id, success=True,
            presentation_binding=ticket.presentation_binding,
            connection_generation=ticket.connection_generation,
            audio_epoch=ticket.prompt_id, ingress_fence=fence,
        )


def receipt(ticket: PromptTicket, *, first: int = 11, last: int = 14) -> UtteranceProvenance:
    return UtteranceProvenance(
        source="glasses", connection_generation="audio-session-1",
        audio_epoch=ticket.prompt_id, first_ingress_ordinal=first,
        last_ingress_ordinal=last, speaker_roles=frozenset({"self"}), mixed=False,
    )


class TestPromptedInputDispatcher:
    async def test_a_refused_answer_logs_why_never_the_words(self, caplog) -> None:  # type: ignore[no-untyped-def]
        # A tap-to-speak that "does nothing" on the glasses: the Mac log says
        # which check turned the answer down (2026-10-08).
        caplog.set_level(logging.INFO, logger="joat.services.prompted_input")
        fixture = PromptFixture()
        ticket = fixture.begin()
        assert await fixture.dispatcher.dispatch(Transcript("secret words", True), receipt(ticket)) is False
        assert fixture.acknowledge(ticket) is True
        early = receipt(ticket, first=5, last=14)
        assert await fixture.dispatcher.dispatch(Transcript("secret words", True), early) is False
        foreign = UtteranceProvenance(
            source=ticket.source, connection_generation=ticket.connection_generation,
            audio_epoch=None, first_ingress_ordinal=11, last_ingress_ordinal=14,
            speaker_roles=frozenset(),
        )
        assert await fixture.dispatcher.dispatch(Transcript("secret words", True), foreign) is False
        reasons = [record.getMessage() for record in caplog.records]
        assert reasons == [
            "prompted answer not taken owner=messages reason=not_acknowledged",
            "prompted answer not taken owner=messages reason=started_before_question",
            "prompted answer not taken owner=messages reason=audio_before_question",
        ]
        assert not any("secret" in reason for reason in reasons)

    async def test_publication_alone_does_not_admit_an_answer(self) -> None:
        fixture = PromptFixture()
        ticket = fixture.begin()

        assert fixture.dispatcher.current_ticket == ticket
        assert fixture.dispatcher.is_armed is False
        assert await fixture.dispatcher.dispatch(Transcript("Good morning", True), receipt(ticket)) is False
        assert fixture.received == []
        assert fixture.acknowledge(ticket) is True
        assert fixture.dispatcher.is_armed is True
        assert await fixture.dispatcher.dispatch(Transcript("Good morning", True), receipt(ticket)) is True
        assert fixture.received == ["Good morning"]
        assert fixture.dispatcher.current_ticket is None

    @pytest.mark.parametrize(
        "change",
        [
            {"prompt_id": "another-prompt"},
            {"presentation_binding": "another-preview"},
            {"connection_generation": "another-audio-session"},
            {"audio_epoch": "reused-or-unrelated-epoch"},
            {"audio_epoch": None},
            {"success": "true"},
            {"ingress_fence": -1},
            {"ingress_fence": True},
        ],
    )
    async def test_mismatched_acknowledgment_cannot_arm_or_spend_current_publication(
        self, change: dict[str, object],
    ) -> None:
        fixture = PromptFixture()
        ticket = fixture.begin()
        arguments: dict[str, object] = {
            "prompt_id": ticket.prompt_id, "success": True,
            "presentation_binding": ticket.presentation_binding,
            "connection_generation": ticket.connection_generation,
            "audio_epoch": ticket.prompt_id, "ingress_fence": 10,
        }
        arguments.update(change)

        assert fixture.dispatcher.acknowledge(**arguments) is False  # type: ignore[arg-type]
        assert fixture.dispatcher.is_armed is False
        assert fixture.dispatcher.current_ticket == ticket
        assert fixture.acknowledge(ticket) is True
        assert await fixture.dispatcher.dispatch(Transcript("Adele", True), receipt(ticket)) is True
        assert fixture.received == ["Adele"]

    async def test_failed_presentation_revokes_its_ticket_and_late_success_cannot_rearm(self) -> None:
        fixture = PromptFixture()
        ticket = fixture.begin()

        assert fixture.dispatcher.acknowledge(
            ticket.prompt_id, success=False,
            presentation_binding=ticket.presentation_binding,
            connection_generation=ticket.connection_generation,
            audio_epoch=ticket.prompt_id, ingress_fence=10,
        ) is False
        assert fixture.dispatcher.current_ticket is None
        assert fixture.acknowledge(ticket) is False
        assert await fixture.dispatcher.dispatch(Transcript("Hello", True), receipt(ticket)) is False
        assert fixture.received == []

    async def test_replacement_is_a_new_epoch_and_late_old_ack_cannot_win(self) -> None:
        fixture = PromptFixture()
        old = fixture.begin()
        current = fixture.begin(owner_id="action-2", field="recipient")

        assert current.prompt_id != old.prompt_id
        assert fixture.acknowledge(old) is False
        assert fixture.dispatcher.current_ticket == current
        assert fixture.dispatcher.is_armed is False
        assert fixture.acknowledge(current) is True
        assert await fixture.dispatcher.dispatch(Transcript("Old queued body", True), receipt(old)) is False
        assert fixture.dispatcher.is_armed is True
        assert await fixture.dispatcher.dispatch(Transcript("Adele", True), receipt(current)) is True
        assert fixture.received == ["Adele"]

    async def test_duplicate_ack_never_extends_expiry_or_moves_the_audio_fence(self) -> None:
        fixture = PromptFixture()
        ticket = fixture.begin()
        assert ticket.expires_at == 160.0
        assert fixture.acknowledge(ticket, fence=10) is True
        fixture.now = 120.0

        assert fixture.acknowledge(ticket, fence=100) is False
        assert fixture.dispatcher.current_ticket == ticket
        assert await fixture.dispatcher.dispatch(Transcript("Still valid", True), receipt(ticket)) is True
        assert fixture.received == ["Still valid"]
        assert fixture.acknowledge(ticket) is False

    @pytest.mark.parametrize("armed", [False, True])
    async def test_expiry_begins_at_publication_and_late_paint_cannot_extend_it(self, armed: bool) -> None:
        fixture = PromptFixture()
        ticket = fixture.begin()
        if armed:
            assert fixture.acknowledge(ticket) is True
        fixture.now = ticket.expires_at

        assert fixture.acknowledge(ticket) is False
        assert await fixture.dispatcher.dispatch(Transcript("Too late", True), receipt(ticket)) is False
        assert fixture.dispatcher.current_ticket is None
        assert fixture.received == []

    @pytest.mark.parametrize("armed", [False, True])
    async def test_owner_invalidation_prevents_both_late_publication_and_consumption(self, armed: bool) -> None:
        fixture = PromptFixture()
        ticket = fixture.begin()
        if armed:
            assert fixture.acknowledge(ticket) is True
        fixture.valid = False

        if not armed:
            assert fixture.acknowledge(ticket) is False
        assert await fixture.dispatcher.dispatch(Transcript("Stale action", True), receipt(ticket)) is False
        assert fixture.dispatcher.current_ticket is None
        assert fixture.received == []

    @pytest.mark.parametrize("armed", [False, True])
    async def test_revoke_wins_over_inflight_paint_and_stt(self, armed: bool) -> None:
        fixture = PromptFixture()
        ticket = fixture.begin()
        if armed:
            assert fixture.acknowledge(ticket) is True

        assert fixture.dispatcher.revoke(ticket.prompt_id) is True
        assert fixture.dispatcher.revoke(ticket.prompt_id) is False
        assert fixture.acknowledge(ticket) is False
        assert await fixture.dispatcher.dispatch(Transcript("Late STT", True), receipt(ticket)) is False
        assert fixture.received == []

    def test_stale_targeted_revoke_cannot_cancel_a_replacement(self) -> None:
        fixture = PromptFixture()
        old = fixture.begin()
        current = fixture.begin(owner_id="replacement")

        assert fixture.dispatcher.revoke(old.prompt_id) is False
        assert fixture.dispatcher.current_ticket == current
        assert fixture.dispatcher.revoke() is True
        assert fixture.dispatcher.current_ticket is None

    @pytest.mark.parametrize(
        "change",
        [
            {"source": "phone"}, {"source": None},
            {"connection_generation": "previous-session"}, {"connection_generation": None},
            {"audio_epoch": "pre-prompt-epoch"}, {"audio_epoch": None},
            {"first_ingress_ordinal": 9}, {"first_ingress_ordinal": 10},
            {"first_ingress_ordinal": None}, {"last_ingress_ordinal": None},
            {"first_ingress_ordinal": -1}, {"first_ingress_ordinal": True},
            {"last_ingress_ordinal": 10},
            {"speaker_roles": frozenset({"other"})},
            {"speaker_roles": frozenset({"self", "other"})},
            {"speaker_roles": frozenset({"unknown", "other"})},
            {"mixed": True},
        ],
    )
    async def test_inadmissible_provenance_does_not_consume_a_valid_lease(
        self, change: dict[str, object],
    ) -> None:
        fixture = PromptFixture()
        ticket = fixture.begin()
        assert fixture.acknowledge(ticket) is True
        invalid = replace(receipt(ticket), **change)

        assert await fixture.dispatcher.dispatch(Transcript("Rejected input", True), invalid) is False
        assert fixture.dispatcher.is_armed is True
        assert fixture.received == []
        assert await fixture.dispatcher.dispatch(Transcript("Fresh answer", True), receipt(ticket)) is True
        assert fixture.received == ["Fresh answer"]

    @pytest.mark.parametrize(
        "roles", [frozenset(), frozenset({"self"}), frozenset({"unknown"}), frozenset({"self", "unknown"})],
    )
    async def test_optional_sdk_speaker_attribution_is_not_a_biometric_requirement(
        self, roles: frozenset[str],
    ) -> None:
        fixture = PromptFixture()
        ticket = fixture.begin()
        fixture.acknowledge(ticket)

        assert await fixture.dispatcher.dispatch(
            Transcript("Answer", True), replace(receipt(ticket), speaker_roles=roles),
        ) is True
        assert fixture.received == ["Answer"]

    @pytest.mark.parametrize(
        "transcript", [Transcript("Partial", False), Transcript("", True), Transcript(" \n\t ", True)],
    )
    async def test_nonfinal_and_empty_transcripts_do_not_spend_the_lease(self, transcript: Transcript) -> None:
        fixture = PromptFixture()
        ticket = fixture.begin()
        fixture.acknowledge(ticket)

        assert await fixture.dispatcher.dispatch(transcript, receipt(ticket)) is False
        assert fixture.dispatcher.is_armed is True
        assert fixture.received == []

    async def test_legacy_transcript_without_provenance_cannot_claim_prompted_input(self) -> None:
        fixture = PromptFixture()
        ticket = fixture.begin()
        fixture.acknowledge(ticket)

        assert await fixture.dispatcher.dispatch(Transcript("No receipt", True), None) is False
        assert fixture.dispatcher.is_armed is True
        assert fixture.received == []

    async def test_literal_final_transcript_is_consumed_before_first_await_exactly_once(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        fixture = PromptFixture()
        started, release = asyncio.Event(), asyncio.Event()

        async def consume(text: str) -> None:
            fixture.received.append(text)
            started.set()
            await release.wait()

        ticket = fixture.dispatcher.begin_publication(
            owner_kind="messages", owner_id="action-1", field="body",
            source="glasses", connection_generation="audio-session-1",
            presentation_binding="exact-preview", ttl_s=60.0,
            is_valid=lambda: True, consume=consume,
        )
        fixture.acknowledge(ticket)
        literal = "  JOAT, stop.\nOpen News? Reply: prompt-private-phrase!  "
        first = asyncio.create_task(fixture.dispatcher.dispatch(Transcript(literal, True), receipt(ticket)))
        try:
            await asyncio.wait_for(started.wait(), timeout=0.2)
            assert fixture.dispatcher.current_ticket is None
            assert await fixture.dispatcher.dispatch(Transcript(literal, True), receipt(ticket)) is False
            assert fixture.acknowledge(ticket) is False
            release.set()
            assert await asyncio.wait_for(first, timeout=0.2) is True
            assert fixture.received == [literal]
            assert "prompt-private-phrase" not in caplog.text
        finally:
            release.set()
            if not first.done():
                first.cancel()
            await asyncio.gather(first, return_exceptions=True)

    async def test_owner_failure_does_not_restore_or_replay_the_consumed_lease(self) -> None:
        fixture = PromptFixture()

        async def failing_consumer(text: str) -> None:
            fixture.received.append(text)
            raise RuntimeError("controlled owner failure")

        ticket = fixture.dispatcher.begin_publication(
            owner_kind="terminal", owner_id="lane-1", field="prompt",
            source="glasses", connection_generation="audio-session-1",
            presentation_binding="terminal-preview", ttl_s=45.0,
            is_valid=lambda: True, consume=failing_consumer,
        )
        fixture.acknowledge(ticket)

        with pytest.raises(RuntimeError, match="controlled owner failure"):
            await fixture.dispatcher.dispatch(Transcript("Do this once", True), receipt(ticket))
        assert fixture.dispatcher.current_ticket is None
        assert await fixture.dispatcher.dispatch(Transcript("Do this once", True), receipt(ticket)) is False
        assert fixture.received == ["Do this once"]

    async def test_owner_can_publish_next_question_without_old_completion_revoking_it(self) -> None:
        fixture = PromptFixture()
        published: list[PromptTicket] = []

        async def next_question(text: str) -> None:
            fixture.received.append(text)
            next_ticket = fixture.begin(field="body")
            published.append(next_ticket)
            assert fixture.acknowledge(next_ticket, fence=20) is True
            await asyncio.sleep(0)

        initial = fixture.dispatcher.begin_publication(
            owner_kind="messages", owner_id="action-1", field="recipient",
            source="glasses", connection_generation="audio-session-1",
            presentation_binding="recipient-preview", ttl_s=60.0,
            is_valid=lambda: True, consume=next_question,
        )
        fixture.acknowledge(initial)

        assert await fixture.dispatcher.dispatch(Transcript("Adele", True), receipt(initial)) is True
        current = published[0]
        assert fixture.dispatcher.current_ticket == current
        assert await fixture.dispatcher.dispatch(Transcript("Adele", True), receipt(initial)) is False
        assert await fixture.dispatcher.dispatch(Transcript("Good morning", True), receipt(current, first=21, last=24)) is True
        assert fixture.received == ["Adele", "Good morning"]

    def test_ticket_and_receipt_cannot_be_relabelled_after_audio_capture(self) -> None:
        fixture = PromptFixture()
        ticket = fixture.begin()
        provenance = receipt(ticket)

        with pytest.raises(FrozenInstanceError):
            ticket.prompt_id = "later-prompt"  # type: ignore[misc]
        with pytest.raises(FrozenInstanceError):
            provenance.audio_epoch = "later-prompt"  # type: ignore[misc]
        roles = {"self"}
        copied = replace(provenance, speaker_roles=roles)
        roles.add("other")
        assert copied.speaker_roles == frozenset({"self"})

    @pytest.mark.parametrize("ttl_s", [0.0, -1.0, float("inf"), float("nan")])
    def test_invalid_expiry_cannot_replace_a_current_publication(self, ttl_s: float) -> None:
        fixture = PromptFixture()
        current = fixture.begin()

        with pytest.raises(ValueError):
            fixture.dispatcher.begin_publication(
                owner_kind="messages", owner_id="bad", field="body",
                source="glasses", connection_generation="audio-session-1",
                presentation_binding="bad-preview", ttl_s=ttl_s,
                is_valid=lambda: True, consume=fixture.consume,
            )
        assert fixture.dispatcher.current_ticket == current

    @pytest.mark.parametrize("generation", [None, ""])
    def test_unknown_authenticated_audio_generation_cannot_publish_a_lease(
        self, generation: str | None,
    ) -> None:
        fixture = PromptFixture()
        with pytest.raises(ValueError):
            fixture.dispatcher.begin_publication(
                owner_kind="messages", owner_id="action-1", field="body",
                source="glasses", connection_generation=generation,  # type: ignore[arg-type]
                presentation_binding="preview", ttl_s=60.0,
                is_valid=lambda: True, consume=fixture.consume,
            )
        assert fixture.dispatcher.current_ticket is None

    async def test_synchronous_revocation_during_owner_check_wins_before_consumption(self) -> None:
        fixture = PromptFixture()
        revoke_on_check = False

        def validity() -> bool:
            if revoke_on_check:
                fixture.dispatcher.revoke()
            return True

        ticket = fixture.dispatcher.begin_publication(
            owner_kind="messages", owner_id="action-1", field="body",
            source="glasses", connection_generation="audio-session-1",
            presentation_binding="preview", ttl_s=60.0,
            is_valid=validity, consume=fixture.consume,
        )
        assert fixture.acknowledge(ticket) is True
        revoke_on_check = True

        assert await fixture.dispatcher.dispatch(Transcript("Revoked input", True), receipt(ticket)) is False
        assert fixture.dispatcher.current_ticket is None
        assert fixture.received == []


def observe_retirement(fixture: PromptFixture, callback: Callable[[PromptTicket], None]) -> None:
    fixture.dispatcher = PromptedInputDispatcher(
        monotonic=lambda: fixture.now, on_retired=lambda ticket, _reason: callback(ticket),
    )


class TestPromptRetirementObserver:
    @pytest.mark.parametrize("reason", ["consumed", "revoked", "expired", "replaced", "owner_invalid"])
    async def test_retirement_reason_distinguishes_consumption_from_owner_cleanup(self, reason: str) -> None:
        fixture = PromptFixture()
        events: list[tuple[PromptTicket, str]] = []

        def retired(ticket: PromptTicket, cause: str) -> None:
            events.append((ticket, cause))
            # Brainstorm's owner must survive until its consumer uses it.
            if cause != "consumed":
                fixture.valid = False

        fixture.dispatcher = PromptedInputDispatcher(
            monotonic=lambda: fixture.now, on_retired=retired,
        )
        ticket = fixture.begin()
        assert fixture.acknowledge(ticket)
        if reason == "consumed":
            assert await fixture.dispatcher.dispatch(Transcript("Exact topic", True), receipt(ticket))
            assert fixture.valid and fixture.received == ["Exact topic"]
        elif reason == "revoked":
            assert fixture.dispatcher.revoke(ticket.prompt_id)
        elif reason == "expired":
            fixture.now = ticket.expires_at
            assert fixture.dispatcher.current_ticket is None
        elif reason == "replaced":
            fixture.begin(owner_id="successor")
        else:
            fixture.valid = False
            assert fixture.dispatcher.is_armed is False
        assert events == [(ticket, reason)]

    def test_expiry_during_owner_validation_is_reported_as_expiry(self) -> None:
        fixture = PromptFixture()
        events: list[tuple[PromptTicket, str]] = []
        fixture.dispatcher = PromptedInputDispatcher(
            monotonic=lambda: fixture.now,
            on_retired=lambda ticket, reason: events.append((ticket, reason)),
        )

        def owner_valid() -> bool:
            fixture.now += 60.0
            return True

        ticket = fixture.dispatcher.begin_publication(
            owner_kind="brainstorm", owner_id="topic", field="topic",
            source="glasses", connection_generation="audio-session-1",
            presentation_binding="topic-preview", ttl_s=60.0,
            is_valid=owner_valid, consume=fixture.consume,
        )
        assert fixture.dispatcher.current_ticket is None
        assert events == [(ticket, "expired")]

    @pytest.mark.parametrize("armed", [False, True])
    @pytest.mark.parametrize("reason", ["targeted_revoke", "revoke", "failed_paint", "expiry", "owner_invalid", "owner_error"])
    async def test_retirement_observer_reports_each_exit_once_after_clearing_current(
        self, armed: bool, reason: str,
    ) -> None:
        fixture = PromptFixture()
        retired: list[tuple[PromptTicket, PromptTicket | None]] = []
        observe_retirement(fixture, lambda ticket: retired.append((ticket, fixture.dispatcher.current_ticket)))

        def owner_valid() -> bool:
            if not fixture.valid and reason == "owner_error":
                raise RuntimeError("private owner validity failure")
            return fixture.valid

        ticket = fixture.dispatcher.begin_publication(
            owner_kind="messages", owner_id="action-1", field="body",
            source="glasses", connection_generation="audio-session-1",
            presentation_binding="bound-preview", ttl_s=60.0,
            is_valid=owner_valid, consume=fixture.consume,
        )
        if armed:
            assert fixture.acknowledge(ticket)
        if reason == "targeted_revoke":
            assert fixture.dispatcher.revoke(ticket.prompt_id)
        elif reason == "revoke":
            assert fixture.dispatcher.revoke()
        elif reason == "failed_paint":
            assert fixture.dispatcher.acknowledge(
                ticket.prompt_id, success=False,
                presentation_binding=ticket.presentation_binding,
                connection_generation=ticket.connection_generation,
                audio_epoch=None, ingress_fence=10,
            ) is False
        elif reason == "expiry":
            fixture.now = ticket.expires_at
            assert fixture.dispatcher.current_ticket is None
        else:
            fixture.valid = False
            if armed:
                assert await fixture.dispatcher.dispatch(Transcript("Too late", True), receipt(ticket)) is False
            else:
                assert fixture.acknowledge(ticket) is False
        assert retired == [(ticket, None)]
        assert retired[0][0] is ticket
        assert fixture.dispatcher.revoke(ticket.prompt_id) is False
        assert fixture.acknowledge(ticket) is False
        assert await fixture.dispatcher.dispatch(Transcript("Late retry", True), receipt(ticket)) is False
        assert fixture.dispatcher.current_ticket is None and fixture.received == []
        assert retired == [(ticket, None)]

    @pytest.mark.parametrize("read", ["current_ticket", "is_armed"])
    def test_retirement_observer_detects_invalid_owner_on_public_state_read(self, read: str) -> None:
        fixture = PromptFixture()
        retired: list[PromptTicket] = []
        observe_retirement(fixture, retired.append)
        ticket = fixture.begin()
        assert fixture.acknowledge(ticket)
        fixture.valid = False
        observed = getattr(fixture.dispatcher, read)
        assert observed is None if read == "current_ticket" else observed is False
        assert retired == [ticket]
        assert fixture.dispatcher.current_ticket is None

    async def test_retirement_observer_runs_before_the_consumer_first_await(self) -> None:
        fixture = PromptFixture()
        events: list[tuple[str, object]] = []
        started, release = asyncio.Event(), asyncio.Event()
        observe_retirement(fixture, lambda ticket: events.append(("retired", ticket)))

        async def consume(text: str) -> None:
            events.append(("consume", text))
            started.set()
            await release.wait()

        ticket = fixture.dispatcher.begin_publication(
            owner_kind="messages", owner_id="action-1", field="body",
            source="glasses", connection_generation="audio-session-1",
            presentation_binding="exact-preview", ttl_s=60.0,
            is_valid=lambda: True, consume=consume,
        )
        assert fixture.acknowledge(ticket)
        literal = "  Stop.\nKeep this exact.  "
        pending = asyncio.create_task(fixture.dispatcher.dispatch(Transcript(literal, True), receipt(ticket)))
        try:
            await asyncio.wait_for(started.wait(), timeout=.2)
            assert events == [("retired", ticket), ("consume", literal)]
            assert fixture.dispatcher.current_ticket is None
            assert await fixture.dispatcher.dispatch(Transcript(literal, True), receipt(ticket)) is False
            release.set()
            assert await pending
            assert events == [("retired", ticket), ("consume", literal)]
        finally:
            release.set()
            await asyncio.gather(pending, return_exceptions=True)

    def test_retirement_observer_can_publish_during_revoke_without_losing_successor(self) -> None:
        fixture = PromptFixture()
        retired: list[PromptTicket] = []
        successors: list[PromptTicket] = []
        seen_current: list[PromptTicket | None] = []

        def on_retired(ticket: PromptTicket) -> None:
            retired.append(ticket)
            seen_current.append(fixture.dispatcher.current_ticket)
            if ticket.owner_id == "original":
                successors.append(fixture.begin(owner_id="observer-successor"))

        observe_retirement(fixture, on_retired)
        original = fixture.begin(owner_id="original")
        assert fixture.dispatcher.revoke(original.prompt_id)
        assert retired == [original] and seen_current == [None]
        assert fixture.dispatcher.current_ticket is successors[0]
        assert fixture.dispatcher.revoke(original.prompt_id) is False
        assert fixture.acknowledge(successors[0])
        assert retired == [original]

    def test_retirement_observer_reentrant_replacement_preserves_newest_publication(self) -> None:
        fixture = PromptFixture()
        retired: list[PromptTicket] = []
        successors: list[PromptTicket] = []
        seen_current: list[PromptTicket | None] = []
        stale_revokes: list[bool] = []

        def on_retired(ticket: PromptTicket) -> None:
            retired.append(ticket)
            seen_current.append(fixture.dispatcher.current_ticket)
            stale_revokes.append(fixture.dispatcher.revoke(ticket.prompt_id))
            if ticket.owner_id == "original":
                successors.append(fixture.begin(owner_id="observer-successor"))

        observe_retirement(fixture, on_retired)
        original = fixture.begin(owner_id="original")
        replaced = fixture.begin(owner_id="outer-replacement")
        assert retired == [original, replaced]
        assert seen_current == [replaced, successors[0]]
        assert stale_revokes == [False, False]
        assert fixture.dispatcher.current_ticket is successors[0]
        assert fixture.acknowledge(original) is False
        assert fixture.acknowledge(replaced) is False
        assert fixture.acknowledge(successors[0])
        assert retired == [original, replaced]

    async def test_retirement_observer_failure_is_content_free_and_does_not_lose_owned_answer(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        fixture = PromptFixture()
        calls: list[PromptTicket] = []
        private = "private-retirement-observer-content"

        def failing_observer(ticket: PromptTicket) -> None:
            calls.append(ticket)
            raise OSError(private)

        observe_retirement(fixture, failing_observer)
        ticket = fixture.begin()
        assert fixture.acknowledge(ticket)
        literal = "An exact answer"
        assert await fixture.dispatcher.dispatch(Transcript(literal, True), receipt(ticket))
        assert calls == [ticket] and fixture.received == [literal]
        assert fixture.dispatcher.current_ticket is None
        assert fixture.dispatcher.revoke(ticket.prompt_id) is False
        assert private not in caplog.text and literal not in caplog.text

    async def test_retirement_observer_ignores_invalid_attempts_until_actual_revoke(self) -> None:
        fixture = PromptFixture()
        retired: list[PromptTicket] = []
        observe_retirement(fixture, retired.append)
        ticket = fixture.begin()
        assert fixture.acknowledge(ticket)
        assert fixture.dispatcher.revoke("foreign-prompt") is False
        assert fixture.acknowledge(ticket, fence=20) is False
        assert await fixture.dispatcher.dispatch(
            Transcript("Wrong source", True), replace(receipt(ticket), source="phone"),
        ) is False
        with pytest.raises(ValueError):
            fixture.dispatcher.begin_publication(
                owner_kind="messages", owner_id="invalid", field="body",
                source="glasses", connection_generation="audio-session-1",
                presentation_binding="invalid-preview", ttl_s=0.0,
                is_valid=lambda: True, consume=fixture.consume,
            )
        assert retired == [] and fixture.dispatcher.current_ticket is ticket
        assert fixture.dispatcher.revoke(ticket.prompt_id)
        assert retired == [ticket]
