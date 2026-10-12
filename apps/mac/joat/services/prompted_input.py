"""Admission boundary for one explicitly presented application question."""

from __future__ import annotations

import secrets
import time
import math
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from ..core.interfaces import Transcript


log = logging.getLogger(__name__)

RetirementReason = Literal["consumed", "revoked", "expired", "replaced", "owner_invalid"]


@dataclass(frozen=True, slots=True)
class PromptTicket:
    prompt_id: str
    owner_kind: str
    owner_id: str
    field: str
    source: str
    connection_generation: str
    presentation_binding: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class UtteranceProvenance:
    """Capture-time receipt; never relabel queued audio after publication."""

    source: str | None
    connection_generation: str | None
    audio_epoch: str | None
    first_ingress_ordinal: int | None
    last_ingress_ordinal: int | None
    speaker_roles: frozenset[str]
    mixed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "speaker_roles", frozenset(self.speaker_roles))


@dataclass(slots=True)
class _Publication:
    ticket: PromptTicket
    is_valid: Callable[[], bool]
    consume: Callable[[str], Awaitable[None]]
    ingress_fence: int | None = None


class PromptedInputDispatcher:
    """One current publication; speech remains closed until acknowledgment."""

    def __init__(
        self, *, monotonic: Callable[[], float] = time.monotonic,
        on_retired: Callable[[PromptTicket, RetirementReason], None] | None = None,
    ) -> None:
        if on_retired is not None and not callable(on_retired):
            raise ValueError("prompt retirement observer must be callable")
        self._monotonic = monotonic
        self._on_retired = on_retired
        self._current: _Publication | None = None

    @property
    def current_ticket(self) -> PromptTicket | None:
        current = self._unexpired_publication()
        return current.ticket if current is not None and self._owner_is_current(current) else None

    @property
    def is_armed(self) -> bool:
        current = self._unexpired_publication()
        return (
            current is not None and self._owner_is_current(current)
            and current.ingress_fence is not None
        )

    def begin_publication(
        self, *, owner_kind: str, owner_id: str, field: str,
        source: str, connection_generation: str, presentation_binding: str,
        ttl_s: float, is_valid: Callable[[], bool],
        consume: Callable[[str], Awaitable[None]],
    ) -> PromptTicket:
        # Invalid publication requests must not supersede a still-valid owner.
        for name, value, limit in (
            ("owner_kind", owner_kind, 64), ("owner_id", owner_id, 256),
            ("field", field, 64), ("source", source, 32),
            ("connection_generation", connection_generation, 256),
            ("presentation_binding", presentation_binding, 512),
        ):
            if not isinstance(value, str) or not value.strip() or "\0" in value or len(value) > limit:
                raise ValueError(f"{name} must be a nonempty bounded string")
            value.encode("utf-8")
        if (
            isinstance(ttl_s, bool) or not isinstance(ttl_s, (int, float))
            or not math.isfinite(ttl_s) or ttl_s <= 0
        ):
            raise ValueError("prompt TTL must be positive and finite")
        if not callable(is_valid) or not callable(consume):
            raise ValueError("prompt owner must provide validity and consumption callbacks")
        expires_at = self._monotonic() + ttl_s
        if not math.isfinite(expires_at):
            raise ValueError("prompt expiry must be finite")
        ticket = PromptTicket(
            prompt_id=secrets.token_urlsafe(18), owner_kind=owner_kind,
            owner_id=owner_id, field=field, source=source,
            connection_generation=connection_generation,
            presentation_binding=presentation_binding,
            expires_at=expires_at,
        )
        previous = self._current
        self._current = _Publication(ticket, is_valid, consume)
        if previous is not None:
            # Remove the old publication before notifying. Install the new
            # one first so a reentrant observer can replace it without this
            # outer call subsequently overwriting that newer successor.
            self._notify_retirement(previous.ticket, "replaced")
        return ticket

    def acknowledge(
        self, prompt_id: str, *, success: bool, presentation_binding: str,
        connection_generation: str, audio_epoch: str | None, ingress_fence: int,
    ) -> bool:
        current = self._unexpired_publication()
        if current is None or current.ticket.prompt_id != prompt_id:
            return False
        ticket = current.ticket
        if (
            ticket.presentation_binding != presentation_binding
            or ticket.connection_generation != connection_generation
        ):
            return False
        # A failed matching paint may have no audio epoch at all. It revokes
        # its own publication, while a stale/wrong-binding failure does not.
        if success is False:
            self.revoke(prompt_id)
            return False
        if (
            success is not True or current.ingress_fence is not None
            or audio_epoch != ticket.prompt_id
            or not self._valid_ordinal(ingress_fence)
        ):
            return False
        if not self._owner_is_current(current):
            return False
        current.ingress_fence = ingress_fence
        return True

    def revoke(self, prompt_id: str | None = None) -> bool:
        current = self._current
        if current is None or (prompt_id is not None and current.ticket.prompt_id != prompt_id):
            return False
        return self._retire(current, "revoked")

    async def dispatch(
        self, transcript: Transcript, provenance: UtteranceProvenance | None,
    ) -> bool:
        current = self._unexpired_publication()
        if current is None:
            return False
        ticket = current.ticket

        def refused(reason: str) -> bool:
            # Why a spoken answer did not answer the question on the glasses
            # (a tap-to-speak that "does nothing"). Never the words.
            log.info("prompted answer not taken owner=%s reason=%s", ticket.owner_kind, reason)
            return False

        if current.ingress_fence is None:
            return refused("not_acknowledged")
        if not self._owner_is_current(current):
            return refused("owner_gone")
        if transcript.is_final is not True or not transcript.text.strip() or provenance is None:
            return refused("no_final_text")
        if provenance.mixed is not False:
            return refused("mixed_audio")
        if provenance.source != ticket.source:
            return refused("other_mic")
        if provenance.connection_generation != ticket.connection_generation:
            return refused("other_connection")
        if provenance.audio_epoch != ticket.prompt_id:
            return refused("audio_before_question")
        if (
            not self._valid_ordinal(provenance.first_ingress_ordinal)
            or not self._valid_ordinal(provenance.last_ingress_ordinal)
        ):
            return refused("no_ordinals")
        # SDK speaker attribution is optional and is not biometric identity
        # ("App algorithm result; this is not a firmware identity assertion",
        # SDK 0.0.16). Unknown/absent attribution stays eligible. The gate
        # used to refuse on ANY explicit other-speaker frame: on the glasses
        # the host tags a stray frame of the wearer's own reply `other`
        # often enough that every tapped reply was thrown away after Whisper
        # had it (2026-10-12, "reason=other_speaker" twice in a row; the
        # wearer saw a box that listened and never filled). Same rule as the
        # ambient path now: another speaker's words are the ones the host
        # named `other` and never `self`. The wearer reviews before anything
        # is sent.
        roles = provenance.speaker_roles
        if any(not isinstance(role, str) for role in roles):
            return refused("other_speaker")
        lowered = {role.casefold() for role in roles}
        if "other" in lowered and "self" not in lowered:
            return refused("other_speaker")
        first, last = provenance.first_ingress_ordinal, provenance.last_ingress_ordinal
        assert first is not None and last is not None
        if first <= current.ingress_fence or last < first:
            return refused("started_before_question")
        # No await or command interpretation may occur before consumption.
        # An owner can publish a successor while its callback runs; completion
        # of this callback must never revoke that newer publication.
        if not self._retire(current, "consumed"):
            return False
        await current.consume(transcript.text)
        return True

    @staticmethod
    def _valid_ordinal(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    def _unexpired_publication(self) -> _Publication | None:
        current = self._current
        if current is not None and self._monotonic() >= current.ticket.expires_at:
            self._retire(current, "expired")
            return None
        return current

    def _owner_is_current(self, current: _Publication) -> bool:
        try:
            valid = current.is_valid() is True
        except Exception:
            valid = False
        # Validity callbacks are synchronous, but they can revoke or replace
        # publication through another controller's lifecycle hook. That wins.
        if self._current is not current:
            return False
        expired = self._monotonic() >= current.ticket.expires_at
        if not valid or expired:
            self._retire(current, "expired" if expired else "owner_invalid")
            return False
        return True

    def _retire(self, current: _Publication, reason: RetirementReason) -> bool:
        if self._current is not current:
            return False
        self._current = None
        self._notify_retirement(current.ticket, reason)
        return True

    def _notify_retirement(self, ticket: PromptTicket, reason: RetirementReason) -> None:
        if self._on_retired is None:
            return
        try:
            self._on_retired(ticket, reason)
        except Exception as exc:
            # Local retirement remains authoritative even if remote cleanup
            # fails. Never include exception text or private owner fields.
            log.warning("prompt retirement observer failed error=%s", type(exc).__name__)
