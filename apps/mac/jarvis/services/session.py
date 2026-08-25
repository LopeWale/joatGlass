"""Session model (docs/SESSION_MODEL.md seam #3).

One session owns the HUD at a time — one screen, one wearer. The manager
routes wearer transcripts: an active session consumes them ("stop" cancels),
otherwise a trigger phrase can start one, otherwise the default assistant
behavior applies (the orchestrator's streamed reply).
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable

from ..core.interfaces import ReplyEngine
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

    @property
    def active(self) -> bool:
        return self._active is not None

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

        self._task = asyncio.get_running_loop().create_task(runner())
        return True


class DebateSession(Session):
    """One debate round, streamed turn by turn — the wearer watches it happen.

    Proposer (codex) → Critic (claude) → Judge (local MLX verdict). Every
    chunk goes through HudWriter.append the moment it exists; a background
    debate that only reports a verdict would violate the wearer's stated
    requirement (visibility IS the proof it was debated).
    """

    def __init__(
        self,
        topic: str,
        hud: HudWriter,
        propose: Callable[[str], AsyncIterator[str]],
        critique: Callable[[str], AsyncIterator[str]],
        judge: ReplyEngine | None,
    ) -> None:
        self._topic = topic
        self._hud = hud
        self._propose = propose
        self._critique = critique
        self._judge = judge
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    async def run(self) -> None:
        await self._hud.status(f"DEBATE · {self._topic[:40]}")
        proposal = await self._turn(
            "» PROPOSER (codex)\n",
            self._propose(
                f"Propose a concrete answer, under 120 words, plain text: {self._topic}"
            ),
        )
        if self._cancelled or not proposal:
            await self._finish("debate cancelled" if self._cancelled else "proposer silent")
            return
        critique = await self._turn(
            "\n\n» CRITIC (claude)\n",
            self._critique(
                "Critique this proposal in under 100 words, plain text. Name the "
                f"weakest claim.\n\nTopic: {self._topic}\n\nProposal:\n{proposal}"
            ),
        )
        if self._cancelled:
            await self._finish("debate cancelled")
            return
        if self._judge is not None:
            await self._hud.append("\n\n» JUDGE (local)\n")
            async for chunk in self._judge.stream(
                "You are the judge of a short debate. In under 60 words give a "
                f"verdict and what to do.\n\nTopic: {self._topic}\n\n"
                f"Proposal:\n{proposal}\n\nCritique:\n{critique}"
            ):
                if self._cancelled:
                    break
                await self._hud.append(chunk)
        await self._finish("debate done — say something to continue")

    async def _turn(self, header: str, stream: AsyncIterator[str]) -> str:
        if header.startswith("»"):
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
