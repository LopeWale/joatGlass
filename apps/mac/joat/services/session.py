"""Session model (docs/SESSION_MODEL.md seam #3).

One session owns the HUD at a time — one screen, one wearer. The manager
routes wearer transcripts: an active session consumes them ("stop" cancels),
otherwise a trigger phrase can start one, otherwise the default assistant
behavior applies (the orchestrator's streamed reply).
"""

from __future__ import annotations

import asyncio
import logging
import time
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

from ..core.interfaces import ReplyEngine
from ..core.protocol import AudioSource, BrainstormAction, BrainstormHudView
from ..verifier.citations import parse_citations
from ..verifier.gate import CitationStatus, VerifierGate
from ..channels.cards import Card, CardLog, Urgency
from .hud import HudWriter
from .roundtable import (
    Participant, RelayMode, RoundTable, build_prompt, is_failure,
    parse_direction, parse_relay_mode,
)
from .sinks import TranscriptSink

from .agent_terminal import parse_reply_route

# "Note that" / "save Claude's reply as a note", spoken into a live
# session. Anchored so a REMARK that happens to open with "note that…"
# ("note that the cache is stale") still lands as a YOU turn — only the
# closed phrasings below are claimed as actions.
_SAVE_REPLY = re.compile(
    r"^(?:save|note|keep) (?:the )?"
    r"(?:(?P<who>codex|claude)(?: ?s)? )?"
    r"(?:last |latest )?(?:reply|response|answer|that|this|it)"
    r"(?: down| as a note| to (?:my )?notes)?$"
)

log = logging.getLogger(__name__)

# Firmware textColor: a corroborated-urgent card is the brightest thing on
# the lens; routine traffic sits one step down.
_CARD_BRIGHTNESS = {Urgency.NOTIFY: 3, Urgency.URGENT: 4}

# One glance can carry six cards; a session that runs for an hour must not
# accumulate an hour of notifications to paint at the end of it. Trimming at
# ENQUEUE rather than at drain also lets a waiting producer learn immediately
# that its card was displaced, instead of blocking until the session ends.
_QUEUED_CARD_LIMIT = 6

# How long a cancelled session may take to actually die before the manager
# stops waiting and hands the wearer their voice commands back.
CANCEL_GRACE_S = 8.0

_UNSET_ADDRESSED = "\x00joat-unset-addressed"
"""Sentinel: the caller did not evaluate address at all (tests, internal
flows), as distinct from None, which means the wearer did NOT address JOAT."""


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
        terminal_capture: Callable[[], Awaitable[None]] | None = None,
        on_state: Callable[[str], None] | None = None,
        starters: Sequence[SessionStarter] = (),
        terminal_retry: Callable[[], Awaitable[None]] | None = None,
        topic_capture_view: TopicCaptureView | None = None,
        brainstorm_available: BrainstormAvailable | None = None,
        route_reply: Callable[
            [str | None, str | None], Awaitable[str]] | None = None,
        save_reply: Callable[[str | None], Awaitable[str]] | None = None,
    ) -> None:
        self._hud = hud
        self._factory = factory
        self._active: Session | None = None
        self._task: asyncio.Task[None] | None = None
        self._pending_cards: list[tuple[Card, asyncio.Future[None] | None]] = []
        self._card_log = card_log
        self._terminal_capture = terminal_capture
        self._on_state = on_state
        self._starters = list(starters)
        self._terminal_retry = terminal_retry
        self._topic_capture_view = topic_capture_view
        self._brainstorm_available = brainstorm_available
        # JOAT as the glue between surfaces: "send Claude's last reply to
        # Hermes" is spoken INTO a live Brainstorm but acts on the Agent
        # Terminal. Injected so this manager stays ignorant of the roster.
        self._route_reply = route_reply
        self._save_reply = save_reply
        # A tap on the Brainstorm HUD is a deliberate one-utterance capture,
        # not decoration. Keep it explicit so the next wearer phrase starts
        # the round without requiring them to repeat the capability name.
        self._topic_capture_armed_until = 0.0

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

    TOPIC_CAPTURE_WINDOW_S = 45.0
    """How long a tap's promise of "the next phrase is the topic" lasts.

    The gesture is consent for the NEXT utterance, which is why this path
    deliberately bypasses the wake gate. Consent to the next thing the
    wearer says does not extend to whatever they happen to say an hour
    later — and an unbounded arm turns a forgotten tap into exactly the
    ambient capture the wake gate exists to prevent, with two paid
    collaborators on the other end of it.

    AgentTerminalCommands.ARM_WINDOW_S bounds the sibling promise for the
    same reason and with the same number.
    """

    def _arm_topic_capture(self) -> None:
        self._topic_capture_armed_until = (
            time.monotonic() + self.TOPIC_CAPTURE_WINDOW_S
        )

    def _disarm_topic_capture(self) -> None:
        self._topic_capture_armed_until = 0.0

    @property
    def topic_capture_armed(self) -> bool:
        """Whether a visible G2 tap is waiting for one glasses-mic phrase."""
        return time.monotonic() < self._topic_capture_armed_until

    def notify(self, card: Card) -> None:
        """Channel-event interrupt policy (SESSION_MODEL.md): an idle HUD
        shows the card now; an active session is never interrupted — cards
        queue and drain the moment it ends."""
        if self._active is not None:
            self._enqueue_card((card, None))
            log.info("card queued behind active session urgency=%s", card.urgency.name)
            return
        asyncio.get_running_loop().create_task(self._show_card(card))

    async def notify_and_wait(self, card: Card) -> None:
        """Return only after the Mac HUD write completes.

        This is the durable-outbox seam. It still is not physical-G2 proof,
        but it prevents a volatile active-session queue from being mistaken
        for a completed write.
        """
        if self._active is None:
            await self._show_card(card)
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        entry = (card, future)
        self._enqueue_card(entry)
        log.info("card queued behind active session urgency=%s", card.urgency.name)
        try:
            await future
        except BaseException:
            # The caller gave up — a bounded delivery timeout, or shutdown.
            # Withdraw the card unless the drain already claimed it, so an
            # abandoned announcement is not painted minutes later behind a
            # caller that has already decided it was never delivered.
            try:
                self._pending_cards.remove(entry)
            except ValueError:
                pass
            raise

    async def _show_card(self, card: Card) -> None:
        await self._paint(card.render(), card.urgency)
        if self._card_log is not None:
            self._card_log.add(card)
            await self._publish_home_notifications()

    async def _publish_home_notifications(self) -> None:
        """Put the RECENT cards on the resting screen, not a count.

        The newest card already owns the bright main region; these are the
        ones behind it. Showing them here is what removes the need to open a
        Notifications page to find out what happened.
        """
        if self._card_log is None:
            return
        recent = self._card_log.recent(3)
        # The newest is the one just painted large; the rest stack below it.
        previous = list(reversed(recent[:-1]))[:2]
        await self._hud.tray("\n".join(card.home_row() for card in previous))

    def _enqueue_card(self, entry: tuple[Card, asyncio.Future[None] | None]) -> None:
        """Queue one card behind the active session, bounded.

        The queue was unbounded and only trimmed when the session ended, so a
        busy hour grew a list nobody would ever read — and every producer
        waiting on a displaced card stayed blocked for that whole hour.
        """
        self._pending_cards.append(entry)
        while len(self._pending_cards) > _QUEUED_CARD_LIMIT:
            _card, future = self._pending_cards.pop(0)
            if future is not None and not future.done():
                future.set_exception(
                    RuntimeError("notification queue overflowed before HUD write")
                )

    async def _paint(self, rendered: str, urgency: Urgency) -> None:
        # Urgency gets BRIGHTNESS from the enum the triage layer already
        # decided, rather than re-deriving it by sniffing rendered text.
        await self._hud.show_with_brightness(
            rendered,
            _CARD_BRIGHTNESS.get(urgency, 3),
            content_kind="notification",
        )

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
        combined = "\n".join(card.render() for card, _ in selected)
        # One glance carries the batch, but history keeps each event: a
        # drained batch logged as a single blob is unreadable afterwards.
        loudest = max((card.urgency for card, _ in selected), default=Urgency.NOTIFY)

        async def show_and_resolve() -> None:
            try:
                await self._paint(combined, loudest)
            except BaseException as exc:
                for _, future in selected:
                    if future is not None and not future.done():
                        future.set_exception(exc)
                log.exception("queued notification HUD write failed")
            else:
                if self._card_log is not None:
                    for card, _ in selected:
                        self._card_log.add(card)
                for _, future in selected:
                    if future is not None and not future.done():
                        future.set_result(None)

        asyncio.get_running_loop().create_task(show_and_resolve())

    def handle_transcript(
        self,
        text: str,
        *,
        capture_source: AudioSource | None = None,
        addressed: str | None = _UNSET_ADDRESSED,
    ) -> bool:
        """True = consumed (session active or started); False = default flow.

        ``addressed`` is the utterance with the wake word stripped, or None
        when the wearer did not address JOAT. Callers that omit it (tests,
        internal flows) are treated as addressed, preserving the previous
        behaviour for everything except the live microphone path.
        """
        lowered = text.lower().strip().rstrip(".!?")
        if self._active is not None:
            # A live session consumes every transcript, so the escape hatch
            # must match how people actually say it. "JOAT, stop" and
            # "stop it" failing here left the wearer trapped until the
            # session ended on its own (observed: no command worked at all
            # while a session was live). Wake-word prefix and filler are
            # stripped before the exact match; the vocabulary stays small so
            # dictating a sentence CONTAINING "stop" cannot end a session.
            escape = lowered
            for wake in ("joat,", "joat"):
                if escape.startswith(wake + " "):
                    escape = escape[len(wake):].strip()
                    break
            if escape in (
                "stop", "cancel", "stop it", "cancel that", "cancel it",
                "stop the debate", "stop the brainstorm", "end the brainstorm",
                "stop the session", "end the session", "never mind", "nevermind",
            ):
                log.info("wearer cancelled the active session")
                self._cancel_active()
                return True
            # STEERING requires address. An overheard sentence must not
            # reorder the round table, and must never become a follow-up
            # the collaborators answer.
            steer = text if addressed is _UNSET_ADDRESSED else addressed
            if not steer:
                log.info(
                    "session active — unaddressed speech ignored chars=%d",
                    len(text),
                )
                return True
            if self._active.consume_transcript(steer):
                return True
            # Routing outranks steering: "send Claude's last reply to
            # Hermes" is an instruction TO JOAT about another surface, not
            # a redirection of who speaks next — the wearer thinks WITH the
            # collaborators here and hands the useful parts to the workers.
            normalized = " ".join(
                re.sub(r"[^a-z0-9]+", " ", steer.lower()).split()
            )
            route = parse_reply_route(normalized)
            if route is not None and self._route_reply is not None:
                router = self._route_reply

                async def run_route() -> None:
                    try:
                        receipt = await router(*route)
                        log.info("reply routed: %s", receipt)
                    except Exception:
                        log.exception("reply routing failed")

                asyncio.get_running_loop().create_task(run_route())
                return True
            save = _SAVE_REPLY.match(normalized)
            if save is not None and self._save_reply is not None:
                saver, who = self._save_reply, save.group("who")

                async def run_save() -> None:
                    try:
                        receipt = await saver(who)
                        log.info("reply saved: %s", receipt)
                    except Exception:
                        log.exception("reply save failed")

                asyncio.get_running_loop().create_task(run_save())
                return True
            # Chairing the room is not an interruption: "claude, respond"
            # steers who speaks next without ending anything.
            directed = self._active.direct(steer)
            if directed is not None:
                log.info("wearer gave the floor to %s", directed)
            else:
                log.info(
                    "session active — transcript not routed to assistant chars=%d",
                    len(text),
                )
            return True
        if self.topic_capture_armed:
            # The wearer armed this path on the glasses. A phone or legacy
            # source may still exist as a general voice fallback, but it must
            # not satisfy the explicit glasses-owned next-utterance promise.
            if capture_source != "glasses":
                log.info("ignoring non-glasses transcript while G2 topic capture is armed")
                self._show_topic_capture(armed=True)
                return True
            self._disarm_topic_capture()
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
                self._arm_topic_capture()
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
        # Whisper PUNCTUATES. The live transcript of the exact phrase this
        # surface tells the wearer to say is
        #     "JOAT. Brainstorm. Should a wearable assistant cache …?"
        # — the capability name arrives with a full stop, every time. The
        # delimiter grammar accepted only a colon, comma, or space, so the
        # spoken opener had NEVER matched once in this engine's whole log:
        # zero voice-started sessions, 25 chat replies, a wearer told to
        # say words that could not work. Accept sentence punctuation after
        # the trigger; the word boundary still keeps "brainstorming …" from
        # becoming an invocation.
        request = text.strip().rstrip(".!?")
        trigger = re.match(
            r"^(?:brainstorm|debate)(?:\s*[.:,!?\u2014-]\s*|\s+)(.+)$",
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
        if action.action == "begin_terminal_input" and active is None:
            # One utterance, into the focused terminal session. Symmetrical
            # with topic capture and deliberately separate from it: this
            # feature has different workers and must never start a
            # Brainstorm by accident.
            arm = self._terminal_capture
            if arm is not None:
                async def run_arm() -> None:
                    await arm()
                asyncio.get_running_loop().create_task(run_arm())
            return
        if action.action == "begin_topic_capture" and active is None:
            if self.topic_capture_armed:
                return
            if not self._brainstorm_is_available():
                # A glasses tap must never promise an ordinary local
                # Brainstorm when the required Hermes seat is unavailable.
                # The cleared view is supplied by the host and renders the
                # current Agent Terminal recovery state on the lenses.
                self._show_topic_capture(armed=False)
                return
            self._arm_topic_capture()
            self._show_topic_capture(armed=True)
            return
        if action.action == "cancel" and active is None and self.topic_capture_armed:
            self._disarm_topic_capture()
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
        self._disarm_topic_capture()

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
            # ...and a subprocess wedged in uninterruptible I/O may not honour
            # even that. While _active stays set, EVERY transcript is consumed
            # and the wearer has no working voice commands at all, so refuse
            # to stay hostage to a task that will not die.
            asyncio.get_running_loop().create_task(self._force_release(task))

    async def _force_release(self, task: asyncio.Task[None]) -> None:
        try:
            await asyncio.wait_for(asyncio.shield(task), CANCEL_GRACE_S)
        except (TimeoutError, asyncio.CancelledError, Exception):
            pass
        if self._task is not task:
            return
        log.warning(
            "session did not end within %.0fs of cancel; releasing the wearer",
            CANCEL_GRACE_S,
        )
        self._active = None
        self._task = None
        if self._on_state is not None:
            self._on_state("cancelled")
        self._drain_cards()

    def _start(self, topic: str) -> bool:
        if not self._brainstorm_is_available():
            # Consume an explicit "brainstorm" request rather than falling
            # through to the unrelated assistant. The wearer receives the
            # same terminal recovery frame as a glasses tap, and can retry
            # it from the glasses when the dedicated Hermes seat recovers.
            self._disarm_topic_capture()
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
                # A force-release may already have handed ownership on. Only
                # the owning runner clears the slot, or it would wipe a
                # session the wearer has since started.
                if self._task is asyncio.current_task():
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
        self._wearer_turns = 0
        self._next_mode: RelayMode | None = None
        self._calls = 0
        self._speaking_label = ""
        self._interrupted = False
        self._cancelled = False
        self._failed = False

    def cancel(self) -> None:
        self._cancelled = True

    WEARER_LABEL = "YOU"

    def consume_transcript(self, text: str) -> bool:
        """The wearer adds a thought mid-session — the YOU turn.

        docs/BRAINSTORM.md lists YOU alongside CODEX / CLAUDE / VERIFY /
        JOAT as a transcript prefix and calls the session steerable; this
        is what makes the wearer a participant rather than an audience.
        The turn lands in the transcript, so the next speaker answers it.

        Only reached with ADDRESSED speech (SessionManager gates that), and
        never for a nomination — "claude, respond" steers the room rather
        than becoming a quoted remark.
        """
        said = text.strip()
        if not said or self._cancelled:
            return False
        if parse_direction(said, self._table) is not None:
            return False
        # "tear that apart" / "build on that" ROUTE the next turn; they are
        # instructions about the relay, not remarks to be answered. Quoting
        # them as a YOU turn would make the collaborators discuss the
        # wearer's stage direction.
        mode = parse_relay_mode(said)
        if mode is not None:
            self._next_mode = mode
            log.info("wearer routed the next turn: %s", mode)
            return True
        self._transcript.append((self.WEARER_LABEL, said))
        self._wearer_turns += 1
        asyncio.get_running_loop().create_task(self._show_wearer_turn(said))
        log.info("wearer added a turn chars=%d", len(said))
        return True

    async def _show_wearer_turn(self, said: str) -> None:
        """Paint the YOU turn immediately, then hand the floor back.

        The wearer speaks WHILE a collaborator is streaming, so this lands
        between two of that speaker's chunks. Interleaving is honest — it
        is what happened — but the chunks that follow would sit under the
        YOU header and read as the wearer's words. Setting _interrupted
        makes the streaming turn re-announce itself before it continues,
        so every line stays attributed to whoever actually said it.
        """
        try:
            await self._sink.turn(self.WEARER_LABEL)
            await self._sink.chunk(said + "\n")
            if self._speaking_label:
                self._interrupted = True
        except Exception:  # a dead surface must not lose the turn
            log.warning("wearer turn not painted", exc_info=True)

    @property
    def status(self) -> str:
        return f"BRAINSTORM · {self._topic[:30]}"

    @property
    def completion_state(self) -> str:
        if self._cancelled:
            return "cancelled"
        return "failed" if self._failed else "settled"

    def direct(self, text: str) -> str | None:
        """Give the floor to a named seat. Returns the name, or None.

        A nomination may carry its instruction — "claude, tear that apart"
        is one breath, and splitting it into two commands is not how
        anyone speaks.
        """
        seat = parse_direction(text, self._table)
        mode = parse_relay_mode(text)
        if seat is None and mode is not None:
            # "claude, tear that apart" is not one of the nomination
            # phrasings, but naming a seat beside an instruction is
            # unambiguously handing them the floor.
            seat = self._table.find(text)
        if seat is None:
            return None
        self._table.direct(seat.name)
        if mode is not None:
            self._next_mode = mode
            log.info("wearer routed %s: %s", seat.name, mode)
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
        """The starter design, fully visible: independent reviews →
        cross-review → meta-review → synthesis.

        A ROUND is one turn for every seat. Round 1 is INDEPENDENT — each
        seat answers blind, because two views with no cross-contamination
        are worth more than one view plus an echo. From round 2 the seats
        cross-review each other. After each round the judge's meta-review
        scores agreement, and the loop CONVERGES at ≥ threshold with no
        blocking issues — otherwise the room goes around again, capped at
        max_rounds — after which JOAT streams the verified synthesis.
        """
        self._set_phase("opening")
        await self._sink.open(self._topic, self._table.names)
        outcome = "no judge — one round"
        for round_no in range(1, self._max_rounds + 1):
            self._set_phase("independent" if round_no == 1 else "cross_review")
            # One turn from every seated collaborator — the wearer's round
            # invariant. The snapshot makes mid-round redirects reorder the
            # seats still owing a turn instead of recounting them.
            self._table.start_round()
            await self._report_progress(round_no)
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
                        await self._offer_recovery(speaker)
                    log.warning("%s could not take its turn", speaker.name)
                elif said:
                    self._transcript.append((speaker.label, said))
            # A debate needs at least two positions in the room. One voice
            # plus a stack trace is a monologue, and scoring it would report
            # convergence with nobody to converge with.
            # The wearer's own turns are not collaborator positions: a
            # transcript of YOU plus one failed seat is still a monologue.
            positions = len(self._transcript) - self._wearer_turns
            if positions < 2:
                spoke = positions
                # The room is ending, so "rejoin" is not the advice: there
                # is no next round to rejoin. Switching the model and
                # asking again is what actually works from here.
                remedy = " — switch its model, then ask again" if self._silenced else ""
                await self._finish(
                    f"insufficient collaborators — {spoke} of {len(self._table)} spoke"
                    + (f" ({', '.join(self._silenced)} silent)" if self._silenced else "")
                    + remedy
                )
                return
            if self._judge is None:
                break
            self._set_phase("meta_review")
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
        if (self._judge is not None and not self._cancelled
                and len(self._transcript) >= 2):
            self._set_phase("synthesis")
            await self._synthesis_turn(outcome)
        await self._finish(f"done — {outcome}")

    async def _offer_recovery(self, speaker: Participant) -> None:
        """Name the way out, at the moment the wearer needs it.

        A seat dies for reasons the wearer can actually fix — an exhausted
        model quota is the common one — and switching that seat's model by
        voice already works. Reporting only "claude silent" leaves them
        holding a broken session and no idea that a one-sentence remedy
        exists.
        """
        try:
            await self._sink.chunk(
                f"say “use another model for the {speaker.role}” to retry\n"
            )
        except Exception:
            log.warning("recovery hint not painted", exc_info=True)

    async def _report_progress(self, round_no: int) -> None:
        """Tell the surfaces where we are and what it has cost.

        Optional on the sink, like the other HUD-only hooks: a terminal or
        a snapshot has no use for it and must not have to implement it.
        """
        callback = getattr(self._sink, "progress", None)
        if callback is None:
            return
        try:
            await callback(round_no, self._max_rounds, self._calls)
        except Exception:
            log.warning("progress not painted", exc_info=True)

    async def _synthesis_turn(self, outcome: str) -> None:
        """JOAT's visible verified synthesis — the product of the session.

        BRAINSTORM.md: JOAT "produces the visible synthesis" and "never
        turns a missing citation or provider failure into a verified
        synthesis". So the prompt hands the local model the verifier's
        per-turn verdicts and forbids asserting anything unverified —
        an open disagreement is a finding to list, not something to
        paper over. Runs even when the judge could not score: an
        illegible meta-review blocks CONVERGENCE, not the summary the
        wearer walks away with.
        """
        assert self._judge is not None
        await self._sink.turn("JOAT (synthesis)")
        verification = "; ".join(self._verify_summary) or "no citations offered"
        collected: list[str] = []
        async for chunk in self._judge.stream(
            "Write the verified synthesis of this discussion. Under 120 "
            "words, plain text. Exactly two sections with these headers:\n"
            "AGREED: what the collaborators now agree on — assert ONLY "
            "claims the verifier passed or that need no citation.\n"
            "OPEN: unresolved disagreements and any claim that stayed "
            "unverified — name who holds what.\n\n"
            f"Topic: {self._topic}\n\n{self._transcript_text()}\n\n"
            f"Judge outcome: {outcome}\n"
            f"Citation verification: {verification}"
        ):
            if self._cancelled:
                break
            collected.append(chunk)
            await self._sink.chunk(chunk)
        text = "".join(collected).strip()
        if text:
            # The synthesis is a turn like any other: it lands in the
            # transcript so the reopenable panel holds the product, not
            # just the path to it.
            self._transcript.append(("JOAT (synthesis)", text))

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
        # Round 1 is independent by construction: colleague turns are
        # WITHHELD, so a first-round speaker cannot echo a peer even though
        # the turns still execute one at a time on the HUD. The wearer's own
        # turns are NOT withheld — independence means not seeing the other
        # collaborators, never ignoring the person who asked.
        prior = (
            [t for t in self._transcript if t[0] == self.WEARER_LABEL]
            if round_no == 1 else self._transcript
        )
        # A routed mode applies to the NEXT turn only, then the room
        # returns to ordinary cross-review — the wearer steered one relay,
        # not the rest of the session.
        if not prior:
            # Round 1 is independent — there is nothing yet to critique or
            # build on, so a mode set before the session began must SURVIVE
            # to the first cross-review turn instead of being eaten here.
            mode: RelayMode = "open"
        else:
            mode = self._next_mode or "cross"
            self._next_mode = None
        prompt = build_prompt(self._topic, speaker, prior,
                              self.SOURCES_CONTRACT, mode=mode)
        self._calls += 1
        text = await self._stream_turn(label, speaker.stream(prompt))
        if self._gate is None or self._cancelled or is_failure(text):
            return text
        for attempt in range(self._max_repairs + 1):
            verdict_ok, repair = await self._verify_visibly(label, text)
            if verdict_ok or self._cancelled or attempt == self._max_repairs:
                return text
            self._calls += 1
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
        self._speaking_label = label
        collected: list[str] = []
        try:
            async for chunk in stream:
                if self._cancelled:
                    break
                if self._interrupted:
                    # The wearer spoke mid-turn. Re-announce this speaker so
                    # the rest of its answer is not read as the wearer's.
                    self._interrupted = False
                    await self._sink.turn(label)
                collected.append(chunk)
                await self._sink.chunk(chunk)
        finally:
            self._speaking_label = ""
            self._interrupted = False
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
