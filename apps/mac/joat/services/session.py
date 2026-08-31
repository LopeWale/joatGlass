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
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

from ..core.interfaces import ReplyEngine
from ..core.protocol import AudioSource, BrainstormAction, BrainstormHudView
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

    def consume_transcript(self, text: str) -> bool:
        """Consume correction/retry speech while this session owns focus."""
        return False

    async def handle_brainstorm_action(self, action: BrainstormAction) -> bool:
        """Claim a bounded glasses action while this session owns focus."""
        return False

    async def replay_hud(self) -> bool:
        """Restore a wearer-visible session frame after link re-authentication."""
        return False

    @property
    def status(self) -> str:
        """Status chrome to restore after transient voice/link feedback."""
        return "JOAT · session active"

    @property
    def completion_state(self) -> str:
        """Safe status projection after run() returns normally."""
        return "settled"


SessionFactory = Callable[[str], Session | None]
"""topic → Session, or None when the trigger can't be honored (missing agents)."""
SessionStarter = Callable[[str], Session | None]
TopicCaptureView = Callable[[bool], BrainstormHudView]
"""Build the Brainstorm frame for an armed capture or its cleared recovery."""
BrainstormAvailable = Callable[[], bool]
"""Whether the required remote Brainstorm collaborator is ready right now."""


class SessionManager:
    """Routes transcripts; enforces the one-active-session invariant."""

    def __init__(
        self,
        hud: HudWriter,
        factory: SessionFactory,
        card_log: CardLog | None = None,
        on_state: Callable[[str], None] | None = None,
        starters: Sequence[SessionStarter] = (),
        terminal_retry: Callable[[], Awaitable[None]] | None = None,
        topic_capture_view: TopicCaptureView | None = None,
        brainstorm_available: BrainstormAvailable | None = None,
    ) -> None:
        self._hud = hud
        self._factory = factory
        self._active: Session | None = None
        self._task: asyncio.Task[None] | None = None
        self._pending_cards: list[tuple[str, asyncio.Future[None] | None]] = []
        self._card_log = card_log
        self._on_state = on_state
        self._starters = list(starters)
        self._terminal_retry = terminal_retry
        self._topic_capture_view = topic_capture_view
        self._brainstorm_available = brainstorm_available
        # A tap on the Brainstorm HUD is a deliberate one-utterance capture,
        # not decoration. Keep it explicit so the next wearer phrase starts
        # the round without requiring them to repeat the capability name.
        self._topic_capture_armed = False

    @property
    def active(self) -> bool:
        return self._active is not None

    @property
    def active_status(self) -> str | None:
        return self._active.status if self._active is not None else None

    @property
    def active_session(self) -> Session | None:
        """Typed event-routing seam; ownership remains read-only to callers."""
        return self._active

    @property
    def topic_capture_armed(self) -> bool:
        """Whether a visible G2 tap is waiting for one glasses-mic phrase."""
        return self._topic_capture_armed

    def notify(self, card: str) -> None:
        """Channel-event interrupt policy (SESSION_MODEL.md): an idle HUD
        shows the card now; an active session is never interrupted — cards
        queue and drain the moment it ends."""
        if self._active is not None:
            self._pending_cards.append((card, None))
            log.info("card queued behind active session chars=%d", len(card))
            return
        asyncio.get_running_loop().create_task(self._show_card(card))

    async def notify_and_wait(self, card: str) -> None:
        """Return only after the Mac HUD write completes.

        This is the durable-outbox seam. It still is not physical-G2 proof,
        but it prevents a volatile active-session queue from being mistaken
        for a completed write.
        """
        if self._active is None:
            await self._show_card(card)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._pending_cards.append((card, future))
        log.info("card queued behind active session chars=%d", len(card))
        await future

    async def _show_card(self, card: str) -> None:
        # Urgency gets BRIGHTNESS now, not merely punctuation: firmware
        # textColor 4 for "!!" cards, a shade dimmer for routine ones.
        urgent = card.startswith("!!")
        await self._hud.show_with_brightness(
            card,
            4 if urgent else 3,
            content_kind="notification",
        )
        if self._card_log is not None:
            self._card_log.add(card)

    def _drain_cards(self) -> None:
        if not self._pending_cards:
            return
        pending, self._pending_cards = self._pending_cards, []
        selected = pending[-6:]
        for _, future in pending[:-6]:
            if future is not None and not future.done():
                future.set_exception(
                    RuntimeError("notification queue overflowed before HUD write")
                )
        combined = "\n".join(card for card, _ in selected)

        async def show_and_resolve() -> None:
            try:
                await self._show_card(combined)
            except BaseException as exc:
                for _, future in selected:
                    if future is not None and not future.done():
                        future.set_exception(exc)
                log.exception("queued notification HUD write failed")
            else:
                for _, future in selected:
                    if future is not None and not future.done():
                        future.set_result(None)

        asyncio.get_running_loop().create_task(show_and_resolve())

    def handle_transcript(
        self,
        text: str,
        *,
        capture_source: AudioSource | None = None,
    ) -> bool:
        """True = consumed (session active or started); False = default flow."""
        lowered = text.lower().strip().rstrip(".!?")
        if self._active is not None:
            if lowered in ("stop", "cancel", "stop the debate"):
                log.info("wearer cancelled the active session")
                self._cancel_active()
                return True
            if self._active.consume_transcript(text):
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
        if self._topic_capture_armed:
            # The wearer armed this path on the glasses. A phone or legacy
            # source may still exist as a general voice fallback, but it must
            # not satisfy the explicit glasses-owned next-utterance promise.
            if capture_source != "glasses":
                log.info("ignoring non-glasses transcript while G2 topic capture is armed")
                self._show_topic_capture(armed=True)
                return True
            self._topic_capture_armed = False
            # The capture surface explicitly promises that the next phrase is
            # the topic. Treat cancel as an exit from that surface rather than
            # accidentally debating the word "cancel".
            if lowered in ("stop", "cancel", "never mind", "nevermind"):
                self._show_topic_capture(armed=False)
                return True
            topic = text.strip().rstrip(".!?")
            if not topic:
                # An empty STT result is not a topic. Re-arm and preserve the
                # truthful lens instruction for the next utterance.
                self._topic_capture_armed = True
                self._show_topic_capture(armed=True)
                return True
            started = self._start(topic)
            if not started:
                # A failing factory must not leave a stale "speak now" UI
                # behind. The cleared recovery frame tells the wearer that a
                # new tap is required once collaborators are ready. The
                # utterance was still deliberately captured by the wearer, so
                # do not let it fall through to the unrelated assistant path.
                self._show_topic_capture(armed=False)
            return True
        for starter in self._starters:
            session = starter(text)
            if session is not None:
                return self.try_start_session(session, publish_state=False)
        # "brainstorm" is what the capability is called; "debate" stays a
        # synonym because it is one of the MOVES inside a brainstorm (relay
        # a turn to be torn apart rather than built on), it is what the
        # wearer already says, and older plugin builds still send it.
        # Natural speech transcription commonly inserts a comma after the
        # capability name ("brainstorm, compare these"). Keep the delimiter
        # grammar narrow so "brainstorming" cannot become an invocation.
        request = text.strip().rstrip(".!?")
        trigger = re.match(
            r"^(?:brainstorm|debate)(?:\s*[:,]\s*|\s+)(.+)$",
            request,
            flags=re.IGNORECASE,
        )
        if trigger is not None:
            return self._start(trigger.group(1).strip())
        return False

    def handle_brainstorm_action(self, action: BrainstormAction) -> None:
        """Route an explicit G2 control to the session currently on screen."""
        if action.action == "retry_terminal":
            retry = self._terminal_retry
            if retry is not None:
                async def run_retry() -> None:
                    await retry()
                asyncio.get_running_loop().create_task(run_retry())
            return
        active = self._active
        if action.action == "begin_topic_capture" and active is None:
            if self._topic_capture_armed:
                return
            if not self._brainstorm_is_available():
                # A glasses tap must never promise an ordinary local
                # Brainstorm when the required Hermes seat is unavailable.
                # The cleared view is supplied by the host and renders the
                # current Agent Terminal recovery state on the lenses.
                self._show_topic_capture(armed=False)
                return
            self._topic_capture_armed = True
            self._show_topic_capture(armed=True)
            return
        if action.action == "cancel" and active is None and self._topic_capture_armed:
            self._topic_capture_armed = False
            self._show_topic_capture(armed=False)
            return
        if active is None:
            return
        if action.action == "cancel":
            # A typed acceptance session must confirm its remote abort and
            # paint the final cancelled HUD before ownership is released.
            # Ordinary sessions decline this action and retain the existing
            # immediate cancellation fallback.
            async def route_cancel() -> None:
                try:
                    claimed = await active.handle_brainstorm_action(action)
                except Exception:
                    claimed = False
                    log.exception("brainstorm cancel action failed")
                if not claimed and self._active is active:
                    self._cancel_active()

            asyncio.get_running_loop().create_task(route_cancel())
            return

        async def route() -> None:
            try:
                await active.handle_brainstorm_action(action)
            except Exception:
                log.exception("brainstorm action failed action=%s", action.action)

        asyncio.get_running_loop().create_task(route())

    def clear_topic_capture(self) -> None:
        """Drop a pending spoken topic when its G2 connection is replaced."""
        self._topic_capture_armed = False

    def _show_topic_capture(self, *, armed: bool) -> None:
        view = self._topic_capture_view

        async def show_capture() -> None:
            if view is not None:
                await self._hud.show_brainstorm(view(armed))
            elif armed:
                # Older embeddings can still request a topic before they
                # supply the visual factory. Preserve the old status-only
                # signal rather than rejecting their action outright.
                await self._hud.status("BRAINSTORM · speak your question")

        asyncio.get_running_loop().create_task(show_capture())

    async def replay_active_hud(self) -> bool:
        active = self._active
        return await active.replay_hud() if active is not None else False

    def _cancel_active(self) -> None:
        active = self._active
        if active is None:
            return
        active.cancel()
        task = self._task
        if task is not None and not task.done():
            # A cooperative flag cannot wake an agent stream blocked in remote
            # I/O. The session's cancellation handler paints a final frame.
            task.cancel()

    def _start(self, topic: str) -> bool:
        if not self._brainstorm_is_available():
            # Consume an explicit "brainstorm" request rather than falling
            # through to the unrelated assistant. The wearer receives the
            # same terminal recovery frame as a glasses tap, and can retry
            # it from the glasses when the dedicated Hermes seat recovers.
            self._topic_capture_armed = False
            self._show_topic_capture(armed=False)
            log.info("brainstorm requested while required terminal is unavailable")
            return True
        session = self._factory(topic)
        if session is None:
            log.warning("debate requested but agents unavailable")
            return False
        return self.try_start_session(session, publish_state=True)

    def _brainstorm_is_available(self) -> bool:
        """Keep legacy embeddings permissive until they opt into the policy."""
        available = self._brainstorm_available
        return available is None or available()

    def try_start_session(self, session: Session, *, publish_state: bool = False) -> bool:
        """Atomically reserve the single wearer session for a typed trigger."""

        if self._active is not None:
            return False
        return self._start_session(session, publish_state=publish_state)

    def _start_session(self, session: Session, *, publish_state: bool) -> bool:
        self._active = session
        if publish_state and self._on_state is not None:
            self._on_state("running")

        async def runner() -> None:
            completion_state = "settled"
            try:
                await session.run()
            except asyncio.CancelledError:
                completion_state = "cancelled"
                raise
            except Exception:
                completion_state = "failed"
                log.exception("session crashed")
            else:
                completion_state = session.completion_state
            finally:
                self._active = None
                self._task = None
                if publish_state and self._on_state is not None:
                    self._on_state(completion_state)
                self._drain_cards()

        self._task = asyncio.get_running_loop().create_task(runner())
        return True

    async def close(self, timeout_s: float = 5.0) -> None:
        """Cancel and await the active session before transports close."""
        session = self._active
        task = self._task
        if session is not None:
            session.cancel()
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout_s)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class DebateSession(Session):
    """One debate, streamed turn by turn — the wearer watches it happen.

    Every seat at the RoundTable speaks once per round, in an order the
    wearer can redirect by name, and then a local judge scores how far apart
    the room still is. Each chunk reaches the sinks the moment it exists: a
    background debate that only reported a verdict would violate the stated
    requirement, where visibility IS the proof it was debated.

    N participants rather than a hardcoded proposer/critic pair keeps one
    round-table implementation for however many CLI seats are installed.
    Per docs/BRAINSTORM.md the seats are the Codex and Claude CLIs only:
    Hermes/OpenClaw are Agent Terminal workers, and neither participates in
    a Brainstorm nor gates its availability.
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
        on_phase: Callable[[str], None] | None = None,
    ) -> None:
        self._topic = topic
        self._table = table
        self._sink = sink
        self._judge = judge
        self._gate = gate
        self._max_repairs = max_repairs
        self._max_rounds = max_rounds
        self._agreement_threshold = agreement_threshold
        self._on_phase = on_phase
        self._verify_summary: list[str] = []
        self._transcript: list[tuple[str, str]] = []
        self._silenced: list[str] = []
        self._cancelled = False
        self._failed = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def status(self) -> str:
        return f"BRAINSTORM · {self._topic[:30]}"

    @property
    def completion_state(self) -> str:
        if self._cancelled:
            return "cancelled"
        return "failed" if self._failed else "settled"

    def direct(self, text: str) -> str | None:
        """Give the floor to a named seat. Returns the name, or None."""
        seat = parse_direction(text, self._table)
        if seat is None:
            return None
        self._table.direct(seat.name)
        return seat.name

    async def handle_brainstorm_action(self, action: BrainstormAction) -> bool:
        if action.action == "show_terminal":
            callback = getattr(self._sink, "show_terminal", None)
            if callback is None:
                return False
            await callback()
            return True
        if action.action == "show_brainstorm":
            return await self.replay_hud()
        return False

    async def replay_hud(self) -> bool:
        callback = getattr(self._sink, "replay", None)
        if callback is None:
            return False
        await callback()
        return True

    async def run(self) -> None:
        try:
            await self._run()
        except asyncio.CancelledError:
            self._cancelled = True
            await self._finish("cancelled")
            raise
        except Exception:
            self._failed = True
            log.exception("Brainstorm collaborator failed")
            await self._finish("blocked · collaborator disconnected; open Hermes to retry")

    async def _run(self) -> None:
        """The DebateLoop state machine (ARCHITECTURE §5), fully visible.

        A ROUND is one turn for every seat. After each round the judge scores
        agreement, and the loop CONVERGES at ≥ threshold with no blocking
        issues — otherwise the room goes around again, capped at max_rounds.
        """
        self._set_phase("opening")
        await self._sink.open(self._topic, self._table.names)
        self._set_phase("turn")
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
                    f"insufficient collaborators — {spoke} of {len(self._table)} spoke"
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
        self._set_phase("closing")
        await self._sink.close(status)

    def _set_phase(self, phase: str) -> None:
        if self._on_phase is not None:
            self._on_phase(phase)


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
