"""Session model (docs/SESSION_MODEL.md seam #3).

One session owns the HUD at a time — one screen, one wearer. The manager
routes wearer transcripts: an active session consumes them ("stop" cancels),
otherwise a trigger phrase can start one, otherwise the default assistant
behavior applies (the orchestrator's streamed reply).
"""

from __future__ import annotations

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Sequence

from ..core.interfaces import ReplyEngine
from ..verifier.citations import parse_citations
from ..verifier.gate import CitationStatus, VerifierGate
from ..channels.cards import CardLog
from .hud import HudWriter
from .roundtable import (
    Participant, RoundTable, build_prompt, is_failure, parse_direction,
)
from .sinks import TranscriptSink

log = logging.getLogger(__name__)


class Session(ABC):
    """A focused interaction that owns the HUD until run() returns."""

    @abstractmethod
    async def run(self) -> None: ...

    @abstractmethod
    def cancel(self) -> None: ...

    def direct(self, text: str) -> str | None:
        """The wearer naming who speaks next; None when this isn't a direction.

        Lives on the base class so the manager can offer every session the
        chance to claim an utterance without knowing what kind of session it
        is. Most sessions have no notion of turn order and decline.
        """
        return None

    @property
    def status(self) -> str:
        """Status chrome to restore after transient voice/link feedback."""
        return "JOAT · session active"


SessionFactory = Callable[[str], Session | None]
"""topic → Session, or None when the trigger can't be honored (missing agents)."""


class SessionManager:
    """Routes transcripts; enforces the one-active-session invariant."""

    def __init__(self, hud: HudWriter, factory: SessionFactory,
                 card_log: CardLog | None = None) -> None:
        self._hud = hud
        self._factory = factory
        self._active: Session | None = None
        self._task: asyncio.Task[None] | None = None
        self._pending_cards: list[str] = []
        self._card_log = card_log

    @property
    def active(self) -> bool:
        return self._active is not None

    @property
    def active_status(self) -> str | None:
        return self._active.status if self._active is not None else None

    def notify(self, card: str) -> None:
        """Channel-event interrupt policy (SESSION_MODEL.md): an idle HUD
        shows the card now; an active session is never interrupted — cards
        queue and drain the moment it ends."""
        if self._active is not None:
            self._pending_cards.append(card)
            log.info("card queued behind active session chars=%d", len(card))
            return
        asyncio.get_running_loop().create_task(self._show_card(card))

    async def _show_card(self, card: str) -> None:
        if self._card_log is not None:
            self._card_log.add(card)
        # Urgency gets BRIGHTNESS now, not merely punctuation: firmware
        # textColor 4 for "!!" cards, a shade dimmer for routine ones.
        urgent = card.startswith("!!")
        await self._hud.show_with_brightness(
            card,
            4 if urgent else 3,
            content_kind="notification",
        )

    def _drain_cards(self) -> None:
        if not self._pending_cards:
            return
        cards, self._pending_cards = self._pending_cards, []
        combined = "\n".join(cards[-6:])  # HUD is small; newest wins
        asyncio.get_running_loop().create_task(self._show_card(combined))

    def handle_transcript(self, text: str) -> bool:
        """True = consumed (session active or started); False = default flow."""
        lowered = text.lower().strip().rstrip(".!?")
        if self._active is not None:
            if lowered in ("stop", "cancel", "stop the debate"):
                log.info("wearer cancelled the active session")
                self._active.cancel()
                return True
            # Chairing the room is not an interruption: "claude, respond"
            # steers who speaks next without ending anything.
            directed = self._active.direct(text)
            if directed is not None:
                log.info("wearer gave the floor to %s", directed)
            else:
                log.info(
                    "session active — transcript not routed to assistant chars=%d",
                    len(text),
                )
            return True
        # "brainstorm" is what the capability is called; "debate" stays a
        # synonym because it is one of the MOVES inside a brainstorm (relay
        # a turn to be torn apart rather than built on), it is what the
        # wearer already says, and older plugin builds still send it.
        for trigger in ("brainstorm ", "brainstorm: ", "debate ", "debate: "):
            if lowered.startswith(trigger):
                # Whisper ends utterances with punctuation; keep topics clean.
                topic = text.strip()[len(trigger):].strip().rstrip(".!?")
                return self._start(topic)
        return False

    def _start(self, topic: str) -> bool:
        session = self._factory(topic)
        if session is None:
            log.warning("debate requested but agents unavailable")
            return False
        self._active = session

        async def runner() -> None:
            try:
                await session.run()
            except Exception:
                log.exception("session crashed")
            finally:
                self._active = None
                self._task = None
                self._drain_cards()

        self._task = asyncio.get_running_loop().create_task(runner())
        return True


class DebateSession(Session):
    """One debate, streamed turn by turn — the wearer watches it happen.

    Every seat at the RoundTable speaks once per round, in an order the
    wearer can redirect by name, and then a local judge scores how far apart
    the room still is. Each chunk reaches the sinks the moment it exists: a
    background debate that only reported a verdict would violate the stated
    requirement, where visibility IS the proof it was debated.

    N participants rather than a hardcoded proposer/critic pair is what lets
    OpenClaw take a third seat without a second debate implementation to
    drift out of sync — and at two seats this reduces exactly to the
    propose → critique → judge loop it replaced.
    """

    SOURCES_CONTRACT = (
        "\nIf you make factual claims, end with a SOURCES: block, one line "
        'per source, formatted exactly: [1] https://url :: "exact verbatim '
        'quote from that page". Only cite pages you are confident contain '
        "the quote word-for-word."
    )

    def __init__(
        self,
        topic: str,
        table: RoundTable,
        sink: TranscriptSink,
        judge: ReplyEngine | None,
        gate: VerifierGate | None = None,
        max_repairs: int = 1,
        max_rounds: int = 2,
        agreement_threshold: float = 0.85,
    ) -> None:
        self._topic = topic
        self._table = table
        self._sink = sink
        self._judge = judge
        self._gate = gate
        self._max_repairs = max_repairs
        self._max_rounds = max_rounds
        self._agreement_threshold = agreement_threshold
        self._verify_summary: list[str] = []
        self._transcript: list[tuple[str, str]] = []
        self._silenced: list[str] = []
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def status(self) -> str:
        return f"DEBATE · {self._topic[:34]}"

    def direct(self, text: str) -> str | None:
        """Give the floor to a named seat. Returns the name, or None."""
        seat = parse_direction(text, self._table)
        if seat is None:
            return None
        self._table.direct(seat.name)
        return seat.name

    async def run(self) -> None:
        """The DebateLoop state machine (ARCHITECTURE §5), fully visible.

        A ROUND is one turn for every seat. After each round the judge scores
        agreement, and the loop CONVERGES at ≥ threshold with no blocking
        issues — otherwise the room goes around again, capped at max_rounds.
        """
        await self._sink.open(self._topic, self._table.names)
        outcome = "no judge — one round"
        for round_no in range(1, self._max_rounds + 1):
            for _ in range(len(self._table)):
                speaker = self._table.next_speaker()
                said = await self._verified_turn(speaker, round_no)
                if self._cancelled:
                    await self._finish("cancelled")
                    return
                if is_failure(said):
                    # Seen, not quoted: the wearer watched the failure stream
                    # past, but it is not a position for anyone to answer.
                    if speaker.name not in self._silenced:
                        self._silenced.append(speaker.name)
                    log.warning("%s could not take its turn", speaker.name)
                elif said:
                    self._transcript.append((speaker.label, said))
            # A debate needs at least two positions in the room. One voice
            # plus a stack trace is a monologue, and scoring it would report
            # convergence with nobody to converge with.
            if len(self._transcript) < 2:
                spoke = len(self._transcript)
                await self._finish(
                    f"no debate — {spoke} of {len(self._table)} spoke"
                    + (f" ({', '.join(self._silenced)} silent)" if self._silenced else "")
                )
                return
            if self._judge is None:
                break
            judged = await self._judge_turn(round_no)
            if self._cancelled:
                await self._finish("cancelled")
                return
            agreement, blocking = parse_judgment(judged)
            if agreement is not None and agreement >= self._agreement_threshold \
                    and not blocking:
                outcome = f"converged (agreement {agreement:.2f}, round {round_no})"
                break
            if agreement is None:
                # An unparseable judgment must not spin the loop forever.
                outcome = f"judge gave no score — stopped at round {round_no}"
                break
            if round_no == self._max_rounds:
                outcome = f"no convergence after {round_no} round(s) (last {agreement:.2f})"
                break
        await self._finish(f"done — {outcome}")

    async def _judge_turn(self, round_no: int) -> str:
        assert self._judge is not None
        await self._sink.turn(f"JUDGE (local) — round {round_no}")
        verification = "; ".join(self._verify_summary) or "no citations offered"
        collected: list[str] = []
        async for chunk in self._judge.stream(
            "You are the judge of a short debate between "
            f"{len(self._table)} collaborators. In under 60 words give a "
            "verdict and what to do. Weigh verified claims above unverified "
            "ones. End with exactly two lines:\n"
            "AGREEMENT: <0.00-1.00, how much the participants now agree>\n"
            "BLOCKING: <yes|no, do the objections block acting on this>\n\n"
            f"Topic: {self._topic}\n\n{self._transcript_text()}\n\n"
            f"Citation verification: {verification}"
        ):
            if self._cancelled:
                break
            collected.append(chunk)
            await self._sink.chunk(chunk)
        return "".join(collected)

    def _transcript_text(self) -> str:
        return "\n\n".join(f"[{label}]\n{text}" for label, text in self._transcript)

    async def _verified_turn(self, speaker: Participant, round_no: int) -> str:
        """One speaker's turn plus its visible verification (and one repair).

        The verifier is itself a turn on the transcript — the wearer watches
        each citation get checked and fabrications get bounced. A turn is
        never silently rewritten: every repair streams under its own header.
        """
        label = speaker.label if round_no == 1 else f"{speaker.label} — round {round_no}"
        prompt = build_prompt(self._topic, speaker, self._transcript,
                              self.SOURCES_CONTRACT)
        text = await self._stream_turn(label, speaker.stream(prompt))
        if self._gate is None or self._cancelled or is_failure(text):
            return text
        for attempt in range(self._max_repairs + 1):
            verdict_ok, repair = await self._verify_visibly(label, text)
            if verdict_ok or self._cancelled or attempt == self._max_repairs:
                return text
            text = await self._stream_turn(
                f"{label} — repair {attempt + 1}",
                speaker.stream(
                    f"{prompt}\n\nYour previous answer:\n{text}\n\n"
                    f"The citation verifier rejected it:\n{repair}\n"
                    "Fix the citations (verbatim quotes at real URLs) or drop "
                    "the unverifiable claims, then answer again."
                ),
            )
        return text

    async def _verify_visibly(self, label: str, text: str) -> tuple[bool, str]:
        assert self._gate is not None
        citations = parse_citations(text)
        await self._sink.turn("VERIFIER")
        if not citations:
            await self._sink.chunk("no sources cited\n")
            self._verify_summary.append(f"{label}: no citations")
            return True, ""
        verdict = await self._gate.verify(citations, prose=text)
        marks = {
            CitationStatus.VERIFIED: "OK",
            CitationStatus.PARTIALLY_VERIFIED: "PARTIAL",
            CitationStatus.UNVERIFIABLE: "FAIL",
        }
        for cv in verdict.citations:
            await self._sink.chunk(f"[{cv.citation_id}] {marks[cv.status]} — {cv.reason}\n")
        outcome = "PASS" if verdict.passed else "DENIED — repair requested"
        await self._sink.chunk(outcome + "\n")
        self._verify_summary.append(f"{label}: {outcome} ({len(citations)} citation(s))")
        return verdict.passed, verdict.repair_request

    async def _stream_turn(self, label: str, stream: AsyncIterator[str]) -> str:
        await self._sink.turn(label)
        collected: list[str] = []
        async for chunk in stream:
            if self._cancelled:
                break
            collected.append(chunk)
            await self._sink.chunk(chunk)
        return "".join(collected).strip()

    async def _finish(self, status: str) -> None:
        await self._sink.close(status)


def seat_participants(
    seats: Sequence[tuple[str, str, object]],
) -> RoundTable:
    """Build a table from (name, role, agent) triples, skipping absent agents.

    Agents come from binaries and daemons that may simply not be installed,
    so "who is at the table" is a runtime fact. Anything falsy is left out.
    """
    present = [
        Participant(name=name, role=role, stream=agent.stream)  # type: ignore[attr-defined]
        for name, role, agent in seats if agent
    ]
    return RoundTable(present)


_AGREEMENT = re.compile(r"AGREEMENT:\s*([01](?:\.\d+)?)", re.IGNORECASE)
_BLOCKING = re.compile(r"BLOCKING:\s*(yes|no)", re.IGNORECASE)


def parse_judgment(text: str) -> tuple[float | None, bool]:
    """(agreement or None if unscored, blocking). Malformed → (None, True):
    an illegible judge never green-lights convergence."""
    m = _AGREEMENT.search(text)
    agreement = min(1.0, max(0.0, float(m.group(1)))) if m else None
    b = _BLOCKING.search(text)
    blocking = True if b is None else b.group(1).lower() == "yes"
    return agreement, blocking
