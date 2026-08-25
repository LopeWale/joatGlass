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
from collections.abc import AsyncIterator, Callable

from ..core.interfaces import ReplyEngine
from ..verifier.citations import parse_citations
from ..verifier.gate import CitationStatus, VerifierGate
from .hud import HudWriter

log = logging.getLogger(__name__)


class Session(ABC):
    """A focused interaction that owns the HUD until run() returns."""

    @abstractmethod
    async def run(self) -> None: ...

    @abstractmethod
    def cancel(self) -> None: ...


SessionFactory = Callable[[str], Session | None]
"""topic → Session, or None when the trigger can't be honored (missing agents)."""


class SessionManager:
    """Routes transcripts; enforces the one-active-session invariant."""

    def __init__(self, hud: HudWriter, factory: SessionFactory) -> None:
        self._hud = hud
        self._factory = factory
        self._active: Session | None = None
        self._task: asyncio.Task[None] | None = None
        self._pending_cards: list[str] = []

    @property
    def active(self) -> bool:
        return self._active is not None

    def notify(self, card: str) -> None:
        """Channel-event interrupt policy (SESSION_MODEL.md): an idle HUD
        shows the card now; an active session is never interrupted — cards
        queue and drain the moment it ends."""
        if self._active is not None:
            self._pending_cards.append(card)
            log.info("card queued behind active session: %s", card)
            return
        asyncio.get_running_loop().create_task(self._show_card(card))

    async def _show_card(self, card: str) -> None:
        await self._hud.show(card)

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
            else:
                log.info("session active — transcript not routed to assistant: %s", text)
            return True
        for trigger in ("debate ", "debate: "):
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
    """One debate round, streamed turn by turn — the wearer watches it happen.

    Proposer (codex) → Critic (claude) → Judge (local MLX verdict). Every
    chunk goes through HudWriter.append the moment it exists; a background
    debate that only reports a verdict would violate the wearer's stated
    requirement (visibility IS the proof it was debated).
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
        hud: HudWriter,
        propose: Callable[[str], AsyncIterator[str]],
        critique: Callable[[str], AsyncIterator[str]],
        judge: ReplyEngine | None,
        gate: VerifierGate | None = None,
        max_repairs: int = 1,
        max_rounds: int = 2,
        agreement_threshold: float = 0.85,
    ) -> None:
        self._topic = topic
        self._hud = hud
        self._propose = propose
        self._critique = critique
        self._judge = judge
        self._gate = gate
        self._max_repairs = max_repairs
        self._max_rounds = max_rounds
        self._agreement_threshold = agreement_threshold
        self._verify_summary: list[str] = []
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    async def run(self) -> None:
        """The DebateLoop state machine (ARCHITECTURE §5), fully visible:

        propose → critique → judge; the judge scores agreement, and the loop
        CONVERGES at ≥ threshold with no blocking issues — otherwise the
        proposer revises against the critique and the next round begins,
        capped at max_rounds. Every turn of every round streams to the HUD.
        """
        await self._hud.status(f"DEBATE · {self._topic[:40]}")
        proposal = await self._verified_turn(
            "PROPOSER (codex)", self._propose,
            f"Propose a concrete answer, under 120 words, plain text: "
            f"{self._topic}{self.SOURCES_CONTRACT}",
            first=True,
        )
        if self._cancelled or not proposal:
            await self._finish("debate cancelled" if self._cancelled else "proposer silent")
            return

        outcome = "no judge — single round"
        for round_no in range(1, self._max_rounds + 1):
            critique = await self._verified_turn(
                "CRITIC (claude)", self._critique,
                "Critique this proposal in under 100 words, plain text. Name the "
                f"weakest claim.\n\nTopic: {self._topic}\n\nProposal:\n{proposal}"
                f"{self.SOURCES_CONTRACT}",
            )
            if self._cancelled:
                await self._finish("debate cancelled")
                return
            if self._judge is None:
                break
            judged = await self._judge_turn(proposal, critique, round_no)
            if self._cancelled:
                await self._finish("debate cancelled")
                return
            agreement, blocking = parse_judgment(judged)
            converged = (
                agreement is not None
                and agreement >= self._agreement_threshold
                and not blocking
            )
            if converged:
                outcome = f"converged (agreement {agreement:.2f}, round {round_no})"
                break
            if agreement is None:
                # An unparseable judgment must not spin the loop forever.
                outcome = f"judge gave no score — stopped at round {round_no}"
                break
            if round_no == self._max_rounds:
                outcome = f"no convergence after {round_no} round(s) (last {agreement:.2f})"
                break
            proposal = await self._verified_turn(
                f"PROPOSER — round {round_no + 1}", self._propose,
                f"Revise your proposal for: {self._topic}\n\nYour previous "
                f"proposal:\n{proposal}\n\nThe critic said:\n{critique}\n\n"
                "Address the critique head-on, under 120 words, plain text."
                f"{self.SOURCES_CONTRACT}",
            )
            if self._cancelled or not proposal:
                await self._finish("debate cancelled" if self._cancelled else "proposer silent")
                return
        await self._finish(f"debate done — {outcome}")

    async def _judge_turn(self, proposal: str, critique: str, round_no: int) -> str:
        assert self._judge is not None
        await self._hud.append(f"\n\n» JUDGE (local) — round {round_no}\n")
        verification = "; ".join(self._verify_summary) or "no citations offered"
        collected: list[str] = []
        async for chunk in self._judge.stream(
            "You are the judge of a short debate. In under 60 words give a "
            "verdict and what to do. Weigh verified claims above unverified "
            "ones. End with exactly two lines:\n"
            "AGREEMENT: <0.00-1.00, how much proposer and critic now agree>\n"
            "BLOCKING: <yes|no, does the critique block acting on the proposal>\n\n"
            f"Topic: {self._topic}\n\nProposal:\n{proposal}\n\n"
            f"Critique:\n{critique}\n\nCitation verification: {verification}"
        ):
            if self._cancelled:
                break
            collected.append(chunk)
            await self._hud.append(chunk)
        return "".join(collected)

    async def _verified_turn(
        self,
        role: str,
        agent: Callable[[str], AsyncIterator[str]],
        prompt: str,
        first: bool = False,
    ) -> str:
        """One debater turn plus its visible verification (and one repair).

        The verifier is itself a turn on the HUD — the wearer watches each
        citation get checked and fabrications get bounced. A turn is never
        silently rewritten: every repair attempt streams under its own header.
        """
        text = await self._stream_turn(f"» {role}\n", agent(prompt), replace=first)
        if self._gate is None or self._cancelled:
            return text
        for attempt in range(self._max_repairs + 1):
            verdict_ok, repair = await self._verify_visibly(role, text)
            if verdict_ok or self._cancelled or attempt == self._max_repairs:
                return text
            text = await self._stream_turn(
                f"\n\n» {role} — repair {attempt + 1}\n",
                agent(
                    f"{prompt}\n\nYour previous answer:\n{text}\n\n"
                    f"The citation verifier rejected it:\n{repair}\n"
                    "Fix the citations (verbatim quotes at real URLs) or drop "
                    "the unverifiable claims, then answer again."
                ),
            )
        return text

    async def _verify_visibly(self, role: str, text: str) -> tuple[bool, str]:
        assert self._gate is not None
        citations = parse_citations(text)
        await self._hud.append("\n\n» VERIFIER\n")
        if not citations:
            await self._hud.append("no sources cited\n")
            self._verify_summary.append(f"{role}: no citations")
            return True, ""
        verdict = await self._gate.verify(citations, prose=text)
        marks = {
            CitationStatus.VERIFIED: "OK",
            CitationStatus.PARTIALLY_VERIFIED: "PARTIAL",
            CitationStatus.UNVERIFIABLE: "FAIL",
        }
        for cv in verdict.citations:
            await self._hud.append(f"[{cv.citation_id}] {marks[cv.status]} — {cv.reason}\n")
        outcome = "PASS" if verdict.passed else "DENIED — repair requested"
        await self._hud.append(outcome + "\n")
        self._verify_summary.append(
            f"{role}: {outcome} ({len(citations)} citation(s))"
        )
        return verdict.passed, verdict.repair_request

    async def _stream_turn(
        self, header: str, stream: AsyncIterator[str], replace: bool = False
    ) -> str:
        if replace:
            await self._hud.show(header)
        else:
            await self._hud.append(header)
        collected: list[str] = []
        async for chunk in stream:
            if self._cancelled:
                break
            collected.append(chunk)
            await self._hud.append(chunk)
        return "".join(collected).strip()

    async def _finish(self, status: str) -> None:
        await self._hud.status(f"JOAT · {status}")


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
