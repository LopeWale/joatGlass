"""Session model (docs/SESSION_MODEL.md seam #3).

One session owns the HUD at a time — one screen, one wearer. The manager
routes wearer transcripts: an active session consumes them ("stop" cancels),
otherwise a trigger phrase can start one, otherwise the default assistant
behavior applies (the orchestrator's streamed reply).
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any
import logging
import time
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from ..core.interfaces import ReplyEngine
from ..core.protocol import AudioSource, BrainstormAction, BrainstormHudView
from ..verifier.citations import parse_citations
from ..verifier.gate import CitationStatus, VerifierGate
from ..channels.cards import Card, CardLog, Urgency
from .hud import HudWriter
from .roundtable import (
    FAILURE_MARK, Participant, RelayMode, RoundTable, build_prompt, is_failure,
    _mode_at_start, direction_tail, parse_direction, parse_relay_mode,
)
from .sinks import TranscriptSink


MAX_BRAINSTORM_TOPIC_CHARS = 4096
# A spoken place name or address; Mapbox Search Box caps a query at 256
# characters, and nothing a wearer says about a destination is longer.
MAX_NAVIGATION_DESTINATION_CHARS = 200


class BrainstormStartResult(StrEnum):
    """Admission result, distinct from whether a transcript was consumed."""

    STARTED = "started"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"
    CLOSED = "closed"
    INVALID = "invalid"

#: docs/SESSION_MODEL.md:49-51. "continue <title>" resumes a stored
#: session's lineage; "branch this" forks the one on screen; "new
#: brainstorm" starts a fresh lineage explicitly. All three require the
#: word, so ordinary speech cannot trip them.
_CONTINUE = re.compile(
    r"^continue(?:\s+(?:the\s+)?(?:brainstorm\s+)?(?:about\s+|on\s+)?)(?P<title>.+)$",
    re.IGNORECASE,
)
#: docs/SESSION_MODEL.md:50 — "new Brainstorm: start an explicit new
#: lineage." Being explicit is what removes the ambiguity, so this does NOT
#: ask before replacing a live session: :52 requires the question for a
#: request that MIGHT be a replacement, and the wearer has just said it is.
_NEW_LINEAGE = re.compile(
    r"^new\s+(?:brainstorm|debate)(?:\s*[.:,!?\u2014-]\s*|\s+)(?P<topic>.+)$",
    re.IGNORECASE,
)
_BRANCH = re.compile(r"^branch(?:\s+(?:this|it|that))?$", re.IGNORECASE)

#: The open-ended verb layer. The wearer's own framing: "the JOAT
#: assistant should be able to take anything and do something I ask it to
#: do with it" — sending a reply to Hermes was their EXAMPLE, not the
#: specification. What existed was two hardcoded verbs.
#:
#: Two conditions, both required, because this runs inside a live session
#: that already consumes every utterance:
#:
#:  1. an IMPERATIVE opening — this is how people give instructions, and
#:     it is what separates "make that shorter" from "that seems short";
#:  2. an explicit REFERENCE to the material, so a remark that merely
#:     contains a verb cannot be mistaken for a command about it.
#:
#: A remark still becomes a YOU turn. Being wrong in that direction costs
#: the wearer a line in the transcript; being wrong the other way silently
#: eats the thought they were trying to contribute.
_MATERIAL_VERB = re.compile(
    r"^(?P<instruction>"
    r"(?:make|turn|rewrite|reword|shorten|trim|summarise|summarize|expand|"
    r"elaborate|explain|simplify|clarify|translate|convert|tighten|"
    r"compare|extract|list|outline|draft|title|name|critique|check|"
    # Audited against phrasings people reach for, not the ones listed
    # first: "clean that up", "polish that", "fix the grammar in that",
    # "put that in plain english" and "give me the gist of that" were all
    # silently unrecognised, and an unrecognised instruction here becomes
    # an ordinary YOU turn — the wearer's request quietly filed as a
    # remark instead of executed.
    r"fix|clean|polish|tidy|put|give|show|read|repeat|shorten|lengthen|"
    r"bullet|number|rank|group|sort|caption|quote)"
    r"\b.*?"
    # The pronoun must actually BE the material — the object, not a
    # subordinating conjunction or determiner. "explain that the cache is
    # stale" and "check that the numbers add up" are remarks TO THE ROOM
    # that happen to open with an imperative; matching their "that" hijacked
    # them into transforms of the last reply. A pronoun is material only at
    # the end of the utterance or before a closed set of continuations
    # ("shorter", "into", "up", "as bullets"…), never before a clause.
    r"\b(?:(?:that|it|this)"
    r"(?=\s+(?:down|up|out|into|in|as|to|for|with|and|again|please|"
    r"a|an|more|less|shorter|longer|simpler|plainer|tighter|better)\b"
    r"|\s*$)"
    r"|the\s+(?:last\s+|latest\s+)?"
    r"(?:reply|answer|response|message|point|turn))"
    r".*)$",
    re.IGNORECASE,
)

_BRAINSTORM_TRIGGER = re.compile(
    # Whisper PUNCTUATES. The live transcript of the exact phrase this
    # surface tells the wearer to say is
    #     "JOAT. Brainstorm. Should a wearable assistant cache …?"
    # — the capability name arrives with a full stop, every time. The
    # delimiter grammar accepted only a colon, comma, or space, so the
    # spoken opener had NEVER matched once in this engine's whole log:
    # zero voice-started sessions, 25 chat replies, a wearer told to say
    # words that could not work. The word boundary still keeps
    # "brainstorming …" from becoming an invocation.
    r"^(?:brainstorm|debate)(?:\s*[.:,!?\u2014-]\s*|\s+)(.+)$",
    re.IGNORECASE,
)

from .agent_terminal import ApprovalReceipt, parse_reply_route

# "Note that" / "save Claude's reply as a note", spoken into a live
# session. Anchored so a REMARK that happens to open with "note that…"
# ("note that the cache is stale") still lands as a YOU turn — only the
# closed phrasings below are claimed as actions.
_SAVE_REPLY = re.compile(
    # Bare verbs: the material sits BETWEEN the verb and any particle —
    # "write that down", not "write down that" — and the optional " down"
    # suffix below already absorbs the tail.
    r"^(?:save|note|keep|remember|record|log|write|jot|add) (?:the )?"
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


@dataclass(frozen=True, slots=True)
class TopicCapture:
    """One explicit request for a topic, independent of audio admission."""

    capture_id: str
    question: str
    expires_at: float


TopicCapturePublisher = Callable[[TopicCapture], Awaitable[bool]]


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
        history: Any | None = None,
        terminal_decide: Callable[[str], Awaitable[str | ApprovalReceipt]] | None = None,
        terminal_cycle: Callable[[], Awaitable[str]] | None = None,
        last_reply: Callable[[str | None], tuple[str, str] | None] | None = None,
        transform: Callable[[str, str], Awaitable[str]] | None = None,
        continuation_factory: Callable[
            [str, tuple[tuple[str, str], ...]], Session | None] | None = None,
        publish_topic_capture: TopicCapturePublisher | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        terminal_view_current: Callable[[], bool] | None = None,
        brainstorm_view_current: Callable[[], bool] | None = None,
        surface_revision: Callable[[], int] | None = None,
        navigation_factory: Callable[[str], Session | None] | None = None,
    ) -> None:
        self._hud = hud
        self._factory = factory
        # A typed destination, for the mind's start_navigation tool. The
        # spoken "navigate to …" grammar reaches the same sessions through
        # `starters`; this is the conversational door to the same room.
        self._navigation_factory = navigation_factory
        self._active: Session | None = None
        self._session_revision = 0
        self._task: asyncio.Task[None] | None = None
        self._pending_cards: list[tuple[Card, asyncio.Future[None] | None]] = []
        self._card_log = card_log
        self._terminal_capture = terminal_capture
        self._terminal_view_current = terminal_view_current
        self._brainstorm_view_current = brainstorm_view_current
        self._surface_revision = surface_revision or (lambda: 0)
        self._on_state = on_state
        self._starters = list(starters)
        self._terminal_retry = terminal_retry
        self._topic_capture_view = topic_capture_view
        self._brainstorm_available = brainstorm_available
        self._publish_topic_capture = publish_topic_capture
        self._monotonic = monotonic
        self._closed = False
        self._topic_capture: TopicCapture | None = None
        self._topic_capture_task: asyncio.Task[None] | None = None
        self._topic_publication_tasks: set[asyncio.Task[None]] = set()
        # JOAT as the glue between surfaces: "send Claude's last reply to
        # Hermes" is spoken INTO a live Brainstorm but acts on the Agent
        # Terminal. Injected so this manager stays ignorant of the roster.
        self._route_reply = route_reply
        # A new topic offered while a session is live, held until
        # the wearer answers. Never started behind their back.
        self._pending_topic: str = ""
        #: More than one stored session matched a spoken `continue <title>`;
        #: the wearer picks by ordinal. docs/SESSION_MODEL.md:49 — "ask on
        #: ambiguity", never silently take the newest.
        self._pending_resume: list[Any] = []
        # docs/SESSION_MODEL.md:49-51 — "continue <title>" and "branch this"
        # need a store to resume FROM and a factory that accepts the earlier
        # turns. Optional so existing embeddings keep working unchanged.
        self._history = history
        # Answers the tool request a work session is blocked on.
        self._terminal_decide = terminal_decide
        self._terminal_cycle = terminal_cycle
        # Resolves the last reply once the session has ended.
        self._last_reply = last_reply
        #: Whether "branch this" is available (needs a factory).
        self._branch = continuation_factory is not None
        # JOAT acting ON the material, rather than routing it elsewhere.
        self._transform = transform
        self._continuation_factory = continuation_factory
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

    @property
    def session_revision(self) -> int:
        """Count admitted starts even after their sessions have finished."""
        return self._session_revision

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
        self._topic_capture_armed_until = self._monotonic() + self.TOPIC_CAPTURE_WINDOW_S
        if self._publish_topic_capture is not None:
            self._topic_capture = TopicCapture(
                capture_id=secrets.token_urlsafe(18),
                question="What would you like to brainstorm?",
                expires_at=self._topic_capture_armed_until,
            )

    def _disarm_topic_capture(self) -> None:
        self._topic_capture_armed_until = 0.0
        self._topic_capture = None
        task, self._topic_capture_task = self._topic_capture_task, None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    @property
    def topic_capture_armed(self) -> bool:
        """Whether a visible G2 tap is waiting for one glasses-mic phrase."""
        if self._publish_topic_capture is not None:
            return self.current_topic_capture is not None
        return not self._closed and self._monotonic() < self._topic_capture_armed_until

    @property
    def current_topic_capture(self) -> TopicCapture | None:
        capture = self._topic_capture
        return capture if capture is not None and self.topic_capture_is_current(capture) else None

    def topic_capture_is_current(self, capture: TopicCapture) -> bool:
        """Validate the exact owner without admitting or interpreting speech."""
        if self._topic_capture is not capture:
            return False
        if (
            self._closed or self._active is not None
            or self._monotonic() >= capture.expires_at
            or not self._scoped_brainstorm_available()
        ):
            self._disarm_topic_capture()
            return False
        return True

    def consume_topic_capture(self, capture: TopicCapture, text: str) -> bool:
        """Start from exact, already-admitted topic text, at most once.

        The caller owns source/epoch admission. This API never gives the
        legacy transcript parser permission to fill a scoped capture.
        """
        if not self.topic_capture_is_current(capture) or not isinstance(text, str) or not text.strip():
            return False
        self._disarm_topic_capture()
        try:
            session = self._factory(text)
        except Exception as exc:
            log.warning("scoped brainstorm factory failed error=%s", type(exc).__name__)
            session = None
        if session is None:
            self._show_topic_capture(armed=False)
            return False
        return self.try_start_session(session, publish_state=True)

    def _scoped_brainstorm_available(self) -> bool:
        try:
            return self._brainstorm_is_available()
        except Exception as exc:
            log.warning("scoped brainstorm availability failed error=%s", type(exc).__name__)
            return False

    def set_answer_probe(self, probe: Callable[[], bool]) -> None:
        """Let the session manager see whether a reply is still being read.

        Late-bound because the orchestrator is built after this manager and
        owns that clock; a constructor argument would force one of them to
        exist twice.
        """
        self._answer_probe = probe

    def _answer_being_read(self) -> bool:
        probe = getattr(self, "_answer_probe", None)
        try:
            return bool(probe()) if probe is not None else False
        except Exception:
            return False  # never let a probe decide the HUD by failing

    def notify(self, card: Card) -> None:
        """Channel-event interrupt policy (SESSION_MODEL.md): an idle HUD
        shows the card now; an active session is never interrupted — cards
        queue and drain the moment it ends.

        An ANSWER the wearer is still reading is protected the same way. It
        was not, so a message could take the lens mid-sentence — which the
        wearer sees as the answer being replaced by a different screen.
        """
        if self._active is None and self._answer_being_read():
            self._enqueue_card((card, None))
            log.info("card queued behind an answer still being read")
            self._drain_after_answer()
            return
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

    def _drain_after_answer(self) -> None:
        """Deliver what waited, once the answer has had its reading time.

        Only a session ENDING drained this queue, so a card held back for an
        answer would have waited for a Brainstorm that may never come. The
        wearer would simply never see the message.
        """
        existing = getattr(self, "_answer_drain", None)
        if existing is not None and not existing.done():
            return

        async def wait_then_drain() -> None:
            # Poll rather than sleep on a computed deadline: the answer's
            # hold can be extended by a new reply arriving behind it.
            for _ in range(240):  # 15s max hold, generous ceiling
                await asyncio.sleep(0.25)
                if self._closed:
                    return
                if self._active is not None or self._answer_being_read():
                    continue
                self._drain_cards()
                return
            if not self._closed and self._active is None:
                self._drain_cards()

        self._answer_drain = asyncio.get_running_loop().create_task(wait_then_drain())

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
        """True = consumed by the selected session; False = default flow.

        ``addressed`` is the utterance with the wake word stripped, or None
        when the wearer did not address JOAT. Callers that omit it (tests,
        internal flows) are treated as addressed, preserving the previous
        behaviour for everything except the live microphone path.
        A running Brainstorm only owns ordinary speech while its feature is
        selected. The injected predicate comes from navigation, not task state.
        """
        lowered = text.lower().strip().rstrip(".!?")
        if self._active is not None:
            background_brainstorm = (
                isinstance(self._active, DebateSession)
                and self._brainstorm_view_current is not None
                and not self._brainstorm_view_current()
            )
            # A live session's escape hatch must match how people say it.
            # "JOAT, stop" and
            # "stop it" failing here left the wearer trapped until the
            # session ended on its own (observed: no command worked at all
            # while a session was live). Wake-word prefix and filler are
            # stripped before the exact match; the vocabulary stays small so
            # dictating a sentence CONTAINING "stop" cannot end a session.
            if self._pending_topic and not background_brainstorm:
                # ADDRESSED speech only. This resolved from the raw
                # transcript, so with the question on the glass an overheard
                # bystander's "yes" — or the wearer agreeing with someone in
                # the room — cancelled the live session and started the
                # replacement. The same address gate every other live-path
                # action stands behind applies here, where the cost of an
                # accident is highest.
                spoken = text if addressed is _UNSET_ADDRESSED else addressed
                verdict = (
                    self._resolve_pending_topic(spoken)
                    if spoken else None
                )
                if verdict is True:
                    topic, self._pending_topic = self._pending_topic, ""
                    log.info("wearer replaced the live session")
                    asyncio.get_running_loop().create_task(
                        self._replace_with(topic)
                    )
                    return True
                if verdict is False:
                    self._pending_topic = ""
                    log.info("wearer kept the live session")
                    return True
                # Anything else falls through and is handled normally; the
                # question stays up rather than being answered by accident.
            escape = lowered
            for wake in ("joat,", "joat"):
                if escape.startswith(wake + " "):
                    escape = escape[len(wake):].strip()
                    break
            # Apostrophes out, straight AND curly: Whisper writes "that's"
            # and sometimes "that’s", and an escape hatch that depends
            # on which quote character the transcriber chose is not an
            # escape hatch.
            escape = escape.replace("’", "").replace("'", "")
            # WHOLE-utterance matches, and heard without the wake word —
            # so each entry is a real tradeoff, not free. The design has
            # always been "failing toward stopped is safe": a wearer who
            # cannot end a session is trapped watching two subscription
            # CLIs bill, and observed live, no command worked at all while
            # one was running.
            #
            # Six natural endings were missing — "that's enough", "we're
            # done", "forget it", "abort", "quit", "enough" — and a wearer
            # who says one of those and is ignored has no way to learn
            # which words are real. Each is only matched as the ENTIRE
            # utterance, so "that's enough sugar" is not a cancel and
            # neither is any sentence merely containing "stop".
            if escape in (
                "stop", "cancel", "stop it", "cancel that", "cancel it",
                "stop the debate", "stop the brainstorm", "end the brainstorm",
                "stop the session", "end the session", "never mind", "nevermind",
                "thats enough", "that is enough", "enough",
                "were done", "we are done", "done",
                "forget it", "abort", "quit", "end it", "stop this",
                "cancel this", "cancel the brainstorm",
            ):
                log.info("wearer cancelled the active session")
                self._cancel_active()
                return True
            if background_brainstorm:
                # This check covers both the raw-transcript and addressed
                # admission paths. Leave other features' requests, dictation,
                # and the assistant wake gate to their existing owners.
                return False
            # STEERING requires address. An overheard sentence must not
            # reorder the round table, and must never become a follow-up
            # the collaborators answer.
            steer = text if addressed is _UNSET_ADDRESSED else addressed
            if steer:
                # A NEW TOPIC is not a remark. "brainstorm <something else>"
                # spoken during a live session was swallowed whole: it became
                # a YOU turn inside the OLD conversation, so the wearer's new
                # question was quietly filed as a comment on the previous one
                # and the session they asked for never started.
                # SESSION_MODEL.md:52 forbids exactly this — "ask before
                # replacing context; never guess or silently reset."
                replacement = _BRAINSTORM_TRIGGER.match(
                    steer.strip().rstrip(".!?")
                )
                if replacement is not None:
                    self._pending_topic = replacement.group(1).strip()
                    log.info("new topic offered while a session is live")
                    asyncio.get_running_loop().create_task(
                        self._ask_before_replacing(self._pending_topic)
                    )
                    return True
            if not steer:
                log.info(
                    "session active — unaddressed speech ignored chars=%d",
                    len(text),
                )
                return True
            # Routing outranks steering: "send Claude's last reply to
            # Hermes" is an instruction TO JOAT about another surface, not
            # a redirection of who speaks next — the wearer thinks WITH the
            # collaborators here and hands the useful parts to the workers.
            if self._handle_instruction(steer):
                return True
            # …and only now does the session get it as a REMARK. This order
            # is load-bearing and was wrong: consume_transcript ran first,
            # and production's DebateSession.consume_transcript returns True
            # for essentially any addressed text — so every instruction above
            # was being filed as a YOU turn instead of executed. The unit
            # tests missed it because their session double returned False,
            # which real sessions never do.
            if self._active.consume_transcript(steer):
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
        if self._publish_topic_capture is not None and self.current_topic_capture is not None:
            # The generic dispatcher alone delivers scoped topic input. Even
            # a glasses label here lacks its prompt epoch and receive fence.
            return False
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
        # An instruction about the last reply, with no session running.
        # This is the common case, not an edge one: the wearer reads the
        # synthesis, the room closes, and THEN they decide what to do with
        # the answer. Requires address, like every other steering path.
        # The SAME rule the live branch uses. An omitted `addressed` means
        # "already addressed" — the orchestrator strips the wake word and
        # then calls handle_transcript(query) with no keyword at all
        # (orchestrator.py, the idle path). Requiring the keyword here made
        # every instruction after a session ended dead in PRODUCTION while
        # every test passed, because the tests supplied an argument the
        # real caller does not.
        spoken = text if addressed is _UNSET_ADDRESSED else addressed
        if spoken and self._handle_instruction(spoken):
            return True
        request = text.strip().rstrip(".!?")
        if self._pending_resume and spoken:
            chosen = self._resolve_pending_resume(spoken)
            if chosen is not None:
                self._pending_resume = []
                asyncio.get_running_loop().create_task(
                    self.resume_by_id(chosen)
                )
                return True
        fresh_topic = _NEW_LINEAGE.match(request)
        if fresh_topic is not None:
            self._pending_resume = []
            return self._start(fresh_topic.group("topic").strip())
        resumed = _CONTINUE.match(request)
        if resumed is not None and self._resume(resumed.group("title").strip()):
            return True
        trigger = _BRAINSTORM_TRIGGER.match(request)
        if trigger is not None:
            return self._start(trigger.group(1).strip())
        return False

    #: What the wearer can say to resolve a pending replacement. Kept tiny
    #: on purpose: a live session already consumes every utterance, so a
    #: broad vocabulary here would eat ordinary speech.
    _REPLACE_YES = ("yes", "yeah", "yep", "do it", "go ahead", "replace it",
                    "start it", "new one", "switch")
    _REPLACE_NO = ("no", "nope", "never mind", "nevermind", "keep going",
                   "stay", "cancel that")

    #: How the wearer answers "which one?" — ordinals and recency words.
    _ORDINALS: dict[str, int] = {
        "one": 0, "first": 0, "1": 0, "the first": 0, "the first one": 0,
        "latest": 0, "the latest": 0, "newest": 0, "the newest": 0,
        "two": 1, "second": 1, "2": 1, "the second": 1, "the second one": 1,
        "three": 2, "third": 2, "3": 2, "the third": 2, "the third one": 2,
    }

    def _resolve_pending_resume(self, said: str) -> str | None:
        answer = said.lower().strip().rstrip(".!?")
        index = self._ORDINALS.get(answer)
        if index is None or index >= len(self._pending_resume):
            return None
        session_id = getattr(self._pending_resume[index], "session_id", "")
        return session_id or None

    async def _ask_which_session(self, matches: list[Any]) -> None:
        """Two or more stored sessions fit the spoken title — the wearer
        chooses. docs/SESSION_MODEL.md:49 forbids guessing here."""
        rows = "; ".join(
            f"{n} — {getattr(p, 'title', '')[:36]}, {p.age()}"
            for n, p in enumerate(matches[:3], start=1)
        )
        try:
            await self._hud.assistant(
                f"{len(matches)} sessions match: {rows}. "
                "Say first, second, or third.",
                answers_wearer=True,
            )
        except Exception:
            log.warning("ambiguity question not painted", exc_info=True)

    async def _ask_before_replacing(self, topic: str) -> None:
        """Put the question on the glass. It is the wearer's call."""
        try:
            await self._hud.assistant(
                f"Start a new Brainstorm on “{topic[:48]}”? "
                "The current one is still running — say yes or no.",
                answers_wearer=True,
            )
        except Exception:
            log.warning("replacement question not painted", exc_info=True)

    def _handle_instruction(self, steer: str) -> bool:
        """An instruction ABOUT a reply, rather than a remark in the room.

        Runs whether or not a session is live. It used to sit inside the
        live branch, so the moment a synthesis settled every one of these
        went dead — and that is exactly when the wearer reads the answer
        and wants to send it somewhere. "Send that to Hermes" after the
        room finished did nothing at all.

        The material outlives the session: DebateStateSink keeps the
        snapshot precisely so a finished answer can still be re-read.
        """
        normalized = " ".join(
            re.sub(r"[^a-z0-9]+", " ", steer.lower()).split()
        )
        route = parse_reply_route(normalized)
        if route is not None and self._route_reply is not None:
            router = self._route_reply

            async def run_route() -> None:
                # The receipt goes to the GLASS, not just the log. This
                # only logged, so the wearer's headline verb — "send that
                # to Hermes" — produced nothing visible whether it worked,
                # hit a lane that does not exist, or failed at the
                # gateway. An action whose result is invisible is
                # indistinguishable from one that did not happen, and this
                # is the verb the whole feature was asked for.
                try:
                    receipt = await router(*route)
                    log.info("reply routed: %s", receipt)
                except Exception as exc:
                    reason = str(exc) or exc.__class__.__name__
                    log.exception("reply routing failed")
                    receipt = f"Could not send that — {reason}"
                await self._say(receipt)

            asyncio.get_running_loop().create_task(run_route())
            return True
        fresh = _NEW_LINEAGE.match(steer.strip().rstrip(".!?"))
        if fresh is not None and self._active is not None:
            topic = fresh.group("topic").strip()
            log.info("wearer asked for an explicit new lineage")
            asyncio.get_running_loop().create_task(self._replace_with(topic))
            return True
        if self._branch and _BRANCH.match(steer.strip().rstrip(".!?")):
            # docs/SESSION_MODEL.md:51 — "branch this: fork the
            # selected transcript into a new child lineage." The wearer
            # is taking the conversation somewhere else without losing
            # where it came from.
            asyncio.get_running_loop().create_task(self._branch_here())
            return True
        instruction = _MATERIAL_VERB.match(steer.strip().rstrip("."))
        if (
            instruction is not None
            and self._transform is not None
            and self._save_reply is not None      # implies a snapshot exists
        ):
            asked = instruction.group("instruction").strip()
            asyncio.get_running_loop().create_task(
                self._act_on_material(asked)
            )
            return True
        save = _SAVE_REPLY.match(normalized)
        if save is not None and self._save_reply is not None:
            saver, who = self._save_reply, save.group("who")

            async def run_save() -> None:
                try:
                    receipt = await saver(who)
                    log.info("reply saved: %s", receipt)
                except Exception as exc:
                    reason = str(exc) or exc.__class__.__name__
                    log.exception("reply save failed")
                    receipt = f"Could not save that — {reason}"
                await self._say(receipt)

            asyncio.get_running_loop().create_task(run_save())
            return True
        return False

    async def _say(self, text: str) -> None:
        """Tell the wearer what a verb did, on the surface they are on.

        `answers_wearer` matters: the plugin refuses ordinary overlays over
        a live Brainstorm, so without it every receipt for an instruction
        given ON that surface would be silently dropped — which is the
        failure this exists to end, reappearing one layer down.
        """
        if not text:
            return
        try:
            await self._hud.assistant(text, answers_wearer=True)
        except Exception:
            log.warning("verb receipt not painted", exc_info=True)

    async def _act_on_material(self, instruction: str) -> None:
        """Do what the wearer asked WITH the last reply.

        This is the general case the two hardcoded verbs were special cases
        of. The material is resolved the same way "send that to Hermes"
        resolves it; what changes is that the instruction is open — "make
        that shorter", "turn that into a spec", "explain the last reply" —
        and JOAT answers it locally instead of routing it somewhere.

        Local on purpose: this runs while two subscription CLIs are already
        mid-session, and spending another billed turn to reword an answer
        the wearer is looking at would be the wrong trade.
        """
        material = self._material()
        if not material:
            log.info("nothing to act on yet")
            await self._say(
                "There is no answer to work with yet — ask something first."
            )
            return
        try:
            answer = await self._transform(instruction, material)  # type: ignore[misc]
        except Exception as exc:
            log.exception("instruction on the last reply failed")
            await self._say(
                f"I could not do that — {str(exc) or exc.__class__.__name__}"
            )
            return
        if not answer:
            await self._say("I had nothing useful to say about that.")
            return
        try:
            # answers_wearer: the plugin refuses ordinary overlays over a
            # Brainstorm, and would otherwise swallow the reply to the
            # instruction the wearer just gave on that very surface.
            await self._hud.assistant(answer, answers_wearer=True)
        except Exception:
            log.warning("instruction result not painted", exc_info=True)

    def _material(self) -> str:
        """The text "that" refers to: the newest usable reply.

        The live session first, then the snapshot. A finished session is
        the LIKELIEST moment for an instruction — the wearer has just read
        the synthesis — and reading only from `_active` meant every verb
        went dead the instant the room closed.
        """
        active = self._active
        found = getattr(active, "last_reply", None)
        if callable(found):
            reply = found()
            if reply:
                return str(reply[1])
        recall = self._last_reply
        if recall is not None:
            reply = recall(None)
            if reply:
                return str(reply[1])
        return ""

    async def _branch_here(self) -> None:
        """Fork the live conversation, keeping its turns as context.

        The parent keeps running until the fork is ready to take the slot,
        so a branch that cannot start leaves the wearer where they were
        rather than with nothing.
        """
        active = self._active
        turns = getattr(active, "transcript", None)
        prior = tuple(turns() if callable(turns) else ())
        topic = getattr(active, "topic", "") or ""
        if self._continuation_factory is None or not prior:
            log.info("nothing to branch from yet")
            return
        session = self._continuation_factory(topic, prior)
        if session is None:
            log.warning("branch requested but agents unavailable")
            return
        # Lineage before the parent goes: a branch is a CHILD
        # (docs/SESSION_MODEL.md:32) — its own fresh id, its parent's id as
        # parent, and the family's root carried through. This wrote
        # parent_id="" and root_id=<its own id>, so no stored branch could
        # be traced to the conversation it forked from.
        parent_sid = getattr(active, "session_id", "")
        parent_root = getattr(active, "root_id", "")
        self._adopt_lineage(
            session, parent_id=parent_sid,
            root_id=parent_root or parent_sid,
        )
        self._cancel_active()
        deadline = time.monotonic() + CANCEL_GRACE_S + 1.0
        while self._active is not None and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if self._active is not None:
            log.warning("previous session would not release; branch dropped")
            return
        log.info("branched a live brainstorm (%d prior turns)", len(prior))
        self.try_start_session(session, publish_state=True)

    async def _replace_with(self, topic: str) -> None:
        """End the running session, then start the one they asked for.

        Sequential on purpose: try_start_session refuses while a session is
        active, so starting before the old one has actually released the
        slot would drop the new topic on the floor — silently, which is the
        failure this whole path exists to prevent.
        """
        self._cancel_active()
        deadline = time.monotonic() + CANCEL_GRACE_S + 1.0
        while self._active is not None and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if self._active is not None:
            log.warning("previous session would not release; new topic dropped")
            try:
                await self._hud.assistant(
                    "The previous Brainstorm did not stop — say it again.",
                    answers_wearer=True,
                )
            except Exception:
                log.warning("replacement failure not painted", exc_info=True)
            return
        self._start(topic)

    def _resolve_pending_topic(self, said: str) -> bool | None:
        """True = start it, False = keep going, None = not an answer."""
        answer = said.lower().strip().rstrip(".!?")
        for wake in ("joat,", "joat"):
            if answer.startswith(wake + " "):
                answer = answer[len(wake):].strip()
                break
        if answer in self._REPLACE_YES:
            return True
        if answer in self._REPLACE_NO:
            return False
        return None

    def handle_brainstorm_action(self, action: BrainstormAction) -> None:
        """Route an explicit G2 control to the session currently on screen."""
        if action.action in ("approve_terminal", "deny_terminal"):
            # Answered on the glass. The roster owns the decision and
            # reports back what the gateway RECORDED — first answer wins,
            # so another controller may already have decided, and the
            # wearer is told that rather than shown a fiction.
            decide = self._terminal_decide
            if decide is not None:
                verdict = (
                    "approve" if action.action == "approve_terminal" else "deny"
                )

                async def run_decision() -> None:
                    # The receipt is the POINT here. First answer wins on
                    # the gateway, so a wearer who taps approve may have
                    # their choice recorded as the opposite by another
                    # controller — and they must be told that, not shown a
                    # fiction. The audit's T1 found the OPPOSITE fiction:
                    # the roster's whole sentence was compared with the
                    # bare verdict, so every successful approval was
                    # announced as a conflict. The verdict now arrives as
                    # data (ApprovalReceipt); a legacy string is parsed.
                    try:
                        answer = await decide(verdict)
                        log.info("terminal approval answered: %s", answer)
                    except Exception as exc:
                        reason = str(exc) or exc.__class__.__name__
                        log.exception("terminal approval failed")
                        await self._say(f"Could not answer that — {reason}")
                        return
                    if isinstance(answer, ApprovalReceipt):
                        recorded, outcome, sentence = answer.recorded, answer.outcome, answer.sentence
                    else:
                        # Legacy shapes: a bare recorded verdict, or the
                        # roster's sentence. Either way, say what stood.
                        sentence = str(answer or "")
                        bare = sentence.strip().lower()
                        if bare in ("approve", "deny"):
                            recorded = bare
                            outcome = "recorded" if bare == verdict else "conflict"
                        else:
                            recorded = verdict if f"recorded {verdict}" in sentence else None
                            outcome = (
                                "conflict" if "another controller" in sentence
                                else "recorded" if recorded
                                else "no_receipt" if not sentence else "failed"
                            )
                            if outcome == "conflict":
                                found = re.search(r"recorded (approve|deny)", sentence)
                                recorded = found.group(1) if found else "the other answer"
                    if outcome == "recorded":
                        await self._say(f"Recorded: {recorded}.")
                    elif outcome == "conflict":
                        await self._say(
                            f"Recorded as {recorded} — another controller "
                            "answered first."
                        )
                    elif outcome == "no_receipt":
                        await self._say(
                            "The gateway gave no receipt for that answer."
                        )
                    else:
                        await self._say(sentence or "That answer did not go through.")

                asyncio.get_running_loop().create_task(run_decision())
            return
        if action.action == "retry_terminal":
            if self._closed:
                return
            retry = self._terminal_retry
            if retry is not None:
                operation = retry()

                async def run_retry() -> None:
                    # Unguarded, this raised into a bare task: the wearer
                    # tapped retry, the daemon was still down, and the only
                    # trace was "Task exception was never retrieved" in a
                    # log they will never read.
                    try:
                        await operation
                    except Exception as exc:
                        reason = str(exc) or exc.__class__.__name__
                        log.exception("terminal retry failed")
                        await self._say(f"Could not reach it — {reason}")
                asyncio.get_running_loop().create_task(run_retry())
            return
        active = self._active
        if action.action == "show_brainstorm" and active is None:
            # A tap on a BUSY terminal used to send this, reach the
            # `active is None` return below, and do nothing at all — a
            # dead gesture on the surface where the wearer most wants
            # control, and one the running footer never even named. With
            # no Brainstorm to go back to, the useful move is the one they
            # asked for and could otherwise only SPEAK: switch sessions.
            if self._closed:
                return
            cycle = self._terminal_cycle
            if cycle is not None:
                cycle_operation = cycle()

                async def run_cycle() -> None:
                    try:
                        moved = await cycle_operation
                    except Exception as exc:
                        reason = str(exc) or exc.__class__.__name__
                        log.exception("terminal focus cycle failed")
                        await self._say(f"Could not switch — {reason}")
                        return
                    log.info("terminal focus cycled: %s", moved)
                    # A gesture whose effect is a one-chip change on the
                    # lane strip needs to say where it landed, and needs to
                    # say when it landed NOWHERE (a single-lane Mac).
                    if moved:
                        await self._say(moved)

                asyncio.get_running_loop().create_task(run_cycle())
            return
        if action.action == "begin_terminal_input" and (active is None or self.terminal_owns_hud()):
            # One utterance, into the focused terminal session. Symmetrical
            # with topic capture and deliberately separate from it: this
            # feature has different workers and must never start a
            # Brainstorm by accident.
            if self._closed:
                return
            arm = self._terminal_capture
            if arm is not None:
                # Bind the owner in this input callback, before queued work
                # yields to Home or reconnect. Await only its publication.
                publication = arm()
                async def run_arm() -> None:
                    await publication
                asyncio.get_running_loop().create_task(run_arm())
            return
        if action.action == "begin_topic_capture" and active is None:
            if self._closed:
                return
            if self.topic_capture_armed:
                return
            available = (
                self._scoped_brainstorm_available()
                if self._publish_topic_capture is not None else self._brainstorm_is_available()
            )
            if not available:
                # A glasses tap must never promise an ordinary local
                # Brainstorm when the required Hermes seat is unavailable.
                # The cleared view is supplied by the host and renders the
                # current Agent Terminal recovery state on the lenses.
                self._show_topic_capture(armed=False)
                return
            self._arm_topic_capture()
            if self._topic_capture is not None:
                self._publish_scoped_topic_capture(self._topic_capture)
            else:
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

        surface_revision = self._surface_revision()

        async def route() -> None:
            if self._active is not active or self._surface_revision() != surface_revision:
                return
            try:
                await active.handle_brainstorm_action(action)
            except Exception:
                log.exception("brainstorm action failed action=%s", action.action)

        asyncio.get_running_loop().create_task(route())

    def clear_topic_capture(self) -> None:
        """Drop a pending spoken topic when its G2 connection is replaced."""
        self._disarm_topic_capture()

    def _publish_scoped_topic_capture(self, capture: TopicCapture) -> None:
        publish = self._publish_topic_capture
        assert publish is not None

        async def run_publication() -> None:
            try:
                accepted = await publish(capture)
            except asyncio.CancelledError:
                if self._topic_capture is capture:
                    self._disarm_topic_capture()
                raise
            except Exception as exc:
                log.warning("scoped topic publication failed error=%s", type(exc).__name__)
                accepted = False
            if self._topic_capture is not capture:
                return
            if accepted is not True or not self.topic_capture_is_current(capture):
                self._disarm_topic_capture()
                self._show_topic_capture(armed=False)

        task = asyncio.get_running_loop().create_task(run_publication())
        self._topic_capture_task = task
        self._topic_publication_tasks.add(task)
        task.add_done_callback(self._topic_publication_done)

    def _topic_publication_done(self, task: asyncio.Task[None]) -> None:
        self._topic_publication_tasks.discard(task)
        if self._topic_capture_task is task:
            self._topic_capture_task = None

    def _show_topic_capture(self, *, armed: bool) -> None:
        if self._closed:
            return
        view = self._topic_capture_view

        async def show_capture() -> None:
            try:
                if view is not None:
                    await self._hud.show_brainstorm(view(armed))
                elif armed:
                    # Older embeddings retain their status-only signal.
                    await self._hud.status("BRAINSTORM · speak your question")
            except Exception as exc:
                log.warning("topic capture view failed error=%s", type(exc).__name__)

        task = asyncio.get_running_loop().create_task(show_capture())
        self._topic_publication_tasks.add(task)
        task.add_done_callback(self._topic_publication_done)

    async def replay_active_hud(self) -> bool:
        active = self._active
        return await active.replay_hud() if active is not None else False

    def interrupted_session(self) -> object | None:
        """The newest durable record still marked `active` — crash evidence.

        A record is written `active` at open and settled at close; an
        engine that died mid-session leaves it standing. The reconnect
        greeting names it so the wearer learns the way back — the audit
        left BSD-08 unresolved on exactly this: the replay source was
        process memory, so after a restart there was nothing to replay
        FROM and the glass painted an empty Ready as if no conversation
        had ever been happening."""
        if self.active or self._history is None:
            return None
        lister = getattr(self._history, "sessions", None)
        if lister is None:
            return None
        try:
            rows = list(lister(5))
        except Exception:
            return None
        for past in rows:
            if getattr(past, "status", "") == "active":
                return past  # type: ignore[no-any-return]
        return None

    def terminal_owns_hud(self) -> bool:
        """True while a live session has handed the lenses to the terminal.

        The terminal's own paint gate needs this: gating on `active` alone
        silenced the terminal EXACTLY when the wearer tapped away to it
        mid-Brainstorm — the view they switched to was a frozen snapshot,
        new lane output and approvals never painting until the session
        ended."""
        active = self._active
        if self._terminal_view_current is not None and self._terminal_view_current():
            return True
        probe = getattr(active, "terminal_owns_hud", None) if active else None
        return bool(probe()) if callable(probe) else False

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

    @staticmethod
    def _adopt_lineage(
        session: Session,
        *,
        session_id: str = "",
        parent_id: str = "",
        root_id: str = "",
    ) -> None:
        """Hand a continuation its durable identity, when it can carry one.

        Factories stay identity-blind — a test double or a legacy session
        without the fields is left untouched — but a resumed session gets
        the STORED id so it continues the same record instead of minting a
        stranger, and a branch gets its parent's lineage.
        """
        for name, value in (
            ("session_id", session_id),
            ("parent_id", parent_id),
            ("root_id", root_id),
        ):
            if value and hasattr(session, name):
                try:
                    setattr(session, name, value)
                except AttributeError:
                    return

    def start_brainstorm(self, topic: str) -> BrainstormStartResult:
        """Start an exact typed topic without replacing the wearer's session.

        A successful receipt means the session was admitted, not that any
        collaborator has answered. No await separates admission and return.
        """
        if self._closed:
            return BrainstormStartResult.CLOSED
        if (
            not isinstance(topic, str) or not topic.strip()
            or len(topic) > MAX_BRAINSTORM_TOPIC_CHARS or "\x00" in topic
        ):
            return BrainstormStartResult.INVALID
        try:
            topic.encode("utf-8")
        except UnicodeEncodeError:
            return BrainstormStartResult.INVALID
        if self._active is not None:
            return BrainstormStartResult.BUSY
        if not self._scoped_brainstorm_available():
            return BrainstormStartResult.UNAVAILABLE
        try:
            session = self._factory(topic)
        except Exception as exc:
            log.warning("typed brainstorm factory failed error=%s", type(exc).__name__)
            return BrainstormStartResult.UNAVAILABLE
        if session is None:
            return BrainstormStartResult.UNAVAILABLE
        if self.try_start_session(session, publish_state=True):
            return BrainstormStartResult.STARTED
        return BrainstormStartResult.CLOSED if self._closed else BrainstormStartResult.BUSY

    def start_navigation(self, destination: str) -> BrainstormStartResult:
        """Start a route to a typed destination, the way "navigate to …" does.

        The spoken grammar in `starters` and this typed door build the same
        session; only the admission receipt differs. A STARTED receipt means
        the destination search began, not that a route exists — the glasses
        still confirm the place and the mode with the wearer.
        """
        if self._closed:
            return BrainstormStartResult.CLOSED
        if (
            not isinstance(destination, str) or not destination.strip()
            or len(destination) > MAX_NAVIGATION_DESTINATION_CHARS or "\x00" in destination
        ):
            return BrainstormStartResult.INVALID
        try:
            destination.encode("utf-8")
        except UnicodeEncodeError:
            return BrainstormStartResult.INVALID
        if self._navigation_factory is None:
            return BrainstormStartResult.UNAVAILABLE
        if self._active is not None:
            return BrainstormStartResult.BUSY
        try:
            session = self._navigation_factory(destination.strip())
        except Exception as exc:
            log.warning("typed navigation factory failed error=%s", type(exc).__name__)
            return BrainstormStartResult.UNAVAILABLE
        if session is None:
            return BrainstormStartResult.UNAVAILABLE
        # Same publish_state as the spoken starter path: navigation is not a
        # Brainstorm, so it must not announce a Brainstorm state.
        if self.try_start_session(session, publish_state=False):
            return BrainstormStartResult.STARTED
        return BrainstormStartResult.CLOSED if self._closed else BrainstormStartResult.BUSY

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

    async def resume_by_id(self, session_id: str) -> bool:
        """Open a stored session by its id — what a picker row taps.

        Distinct from `_resume`, which matches a SPOKEN title. A tap has
        already chosen unambiguously; there is nothing left to disambiguate
        and nothing to guess.
        """
        if self._history is None or self._continuation_factory is None:
            return False
        past = self._history.load(session_id)
        if past is None:
            return False
        if self._active is not None:
            return False
        if not self._brainstorm_is_available():
            # Say WHICH binary is missing, on the surface they tapped.
            # "That session could not be reopened" is true and useless: the
            # wearer cannot tell a missing CLI from a corrupted record, and
            # only one of those is something they can fix.
            self._show_topic_capture(armed=False)
            return True
        session = self._continuation_factory(past.topic, tuple(past.turns))
        if session is None:
            return False
        # The SAME record continues: docs/SESSION_MODEL.md:49 says resume,
        # not replay-into-a-new-session.
        stored_sid = getattr(past, "session_id", "")
        self._adopt_lineage(
            session, session_id=stored_sid,
            parent_id=getattr(past, "parent_id", ""),
            root_id=getattr(past, "root_id", "") or stored_sid,
        )
        log.info("reopened a stored brainstorm (%d prior turns)", len(past.turns))
        return self.try_start_session(session, publish_state=True)

    def _resume(self, title: str) -> bool:
        """Continue a stored session, carrying its turns forward.

        docs/SESSION_MODEL.md:49 — "continue <title> → resume the matching
        durable session; ask on ambiguity." Until now nothing could resume
        anything: the record had no id, so twenty stored conversations were
        four lines of dead text and every question started from zero.
        """
        if self._history is None or self._continuation_factory is None:
            return False
        finder = getattr(self._history, "find_all", None)
        matches = finder(title) if finder is not None else []
        if finder is None:
            single = self._history.find(title)
            matches = [single] if single is not None else []
        if not matches:
            log.info("no stored brainstorm matches that title")
            return False
        if len(matches) > 1:
            self._pending_resume = list(matches[:3])
            asyncio.get_running_loop().create_task(
                self._ask_which_session(self._pending_resume)
            )
            return True
        past = matches[0]
        if not self._brainstorm_is_available():
            self._show_topic_capture(armed=False)
            return True
        session = self._continuation_factory(past.topic, tuple(past.turns))
        if session is None:
            log.warning("continuation requested but agents unavailable")
            return False
        stored_sid = getattr(past, "session_id", "")
        self._adopt_lineage(
            session, session_id=stored_sid,
            parent_id=getattr(past, "parent_id", ""),
            root_id=getattr(past, "root_id", "") or stored_sid,
        )
        log.info("continuing a stored brainstorm (%d prior turns)",
                 len(past.turns))
        return self.try_start_session(session, publish_state=True)

    def _brainstorm_is_available(self) -> bool:
        """Keep legacy embeddings permissive until they opt into the policy."""
        available = self._brainstorm_available
        return available is None or available()

    def try_start_session(self, session: Session, *, publish_state: bool = False) -> bool:
        """Atomically reserve the single wearer session for a typed trigger."""

        if self._closed or self._active is not None:
            return False
        return self._start_session(session, publish_state=publish_state)

    def _start_session(self, session: Session, *, publish_state: bool) -> bool:
        self._disarm_topic_capture()
        self._active = session
        self._session_revision += 1
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
                    # The replacement question dies with the session it was
                    # about. Left set, the next session's first "yes" — an
                    # answer to anything — replaced it with a topic offered
                    # to a conversation that no longer exists.
                    self._pending_topic = ""
                    if publish_state and self._on_state is not None:
                        self._on_state(completion_state)
                    self._drain_cards()

        self._task = asyncio.get_running_loop().create_task(runner())
        return True

    async def close(self, timeout_s: float = 5.0) -> None:
        """Cancel and await the active session before transports close."""
        self._closed = True
        self._disarm_topic_capture()
        publications = tuple(
            task for task in self._topic_publication_tasks if task is not asyncio.current_task()
        )
        for publication in publications:
            publication.cancel()
        if publications:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*publications, return_exceptions=True), timeout=timeout_s,
                )
            except TimeoutError:
                for publication in publications:
                    publication.cancel()
                await asyncio.gather(*publications, return_exceptions=True)
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


def _eager(stream: AsyncIterator[str]) -> AsyncIterator[str]:
    """Start consuming a model stream NOW, buffering until someone reads.

    A collaborator's turn costs ~7.3s of fixed overhead before a single
    token — process spawn plus subscription auth, measured against both
    real binaries — and then 12-40s of generation. Round 1 is INDEPENDENT
    by construction (neither seat sees the other's answer), so running the
    seats one after another spends that twice for no reason: the wearer
    waits through the whole of Codex before Claude has even been launched.

    Draining eagerly lets the second seat's turn run WHILE the first is on
    screen, so the round costs about as long as its slowest seat instead of
    the sum of both. Display order is unchanged — the wearer still watches
    one turn at a time, in order, which is the point of the surface.
    """
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    failure: list[BaseException] = []

    async def drain() -> None:
        try:
            async for chunk in stream:
                await queue.put(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Held, not converted. A collaborator that DISCONNECTS is a
            # blocked session with its own wearer-visible recovery — not a
            # turn that produced bad text. Draining eagerly must not
            # downgrade one into the other, so the reader re-raises it at
            # the point the caller would have seen it anyway.
            failure.append(exc)
        finally:
            await queue.put(None)

    task = asyncio.get_running_loop().create_task(drain())

    async def replay() -> AsyncIterator[str]:
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                yield chunk
            if failure:
                raise failure[0]
        finally:
            if not task.done():
                task.cancel()

    return replay()


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
        prior: tuple[tuple[str, str], ...] = (),
        session_id: str = "",
        parent_id: str = "",
        root_id: str = "",
    ) -> None:
        self._topic = topic
        # Durable identity, born WITH the conversation
        # (docs/SESSION_MODEL.md:30-32). Minted here when not carried in,
        # so a fresh session, a branch (child id + parent lineage), and a
        # resume (the stored id, so the same record continues) all flow
        # through one field instead of being re-minted at close — which is
        # what happened: the history API accepted identity and the close
        # path never passed it, so every record's id was an orphan minted
        # after the fact and "branch this" produced a child no record
        # could trace to its parent.
        self.session_id = session_id or secrets.token_hex(8)
        self.parent_id = parent_id
        self.root_id = root_id or self.session_id
        self._table = table
        self._sink = sink
        self._judge = judge
        self._gate = gate
        self._max_repairs = max_repairs
        self._max_rounds = max_rounds
        self._agreement_threshold = agreement_threshold
        self._on_phase = on_phase
        #: Where a session's wall-clock actually went, by fixed phase name.
        self._phase_seconds: dict[str, float] = {}
        self._phase_name = ""
        self._phase_started: float | None = None
        self._verify_summary: list[str] = []
        # A CONTINUATION's earlier turns. Deliberately not seeded into
        # `_transcript`: that list is also how this session counts how many
        # positions were actually taken THIS run ("insufficient
        # collaborators — 1 of 2 spoke"), and seeding it would make that
        # guard pass on borrowed history while nobody present said a word.
        # Context for the prompts, evidence for nothing.
        # Failures filtered HERE, not only in-run: the in-run exclusion
        # keeps a dead seat out of _transcript, but a resumed or branched
        # session inherits `prior` from the durable record, whose turns
        # are the sink's UNFILTERED stream — so a provider's own error
        # text ("You're out of usage credits") rode back in as a quoted
        # position, exactly what BRAINSTORM.md:53-54 forbids. History
        # keeps the failure (it happened); prompts never see it.
        self._prior: tuple[tuple[str, str], ...] = tuple(
            (label, text) for label, text in prior if not is_failure(text)
        )
        self._transcript: list[tuple[str, str]] = []
        self._silenced: list[str] = []
        self._wearer_turns = 0
        self._next_mode: RelayMode | None = None
        self._calls = 0
        #: Round-1 turns already in flight, by seat name.
        self._prefetch: dict[str, AsyncIterator[str]] = {}
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
            # The nomination is routed by the manager (direct()), but
            # anything the wearer asked ALONGSIDE it has to reach the seat
            # — "ask codex if that's true" gave codex the floor and threw
            # the question away, so it was prompted with the prior turns
            # and "add what the others missed" and answered a question
            # nobody had asked. Recording the tail as the YOU turn puts it
            # in the transcript the next prompt is built from. Still
            # returns False: the direction itself is not consumed here.
            tail = direction_tail(said, self._table)
            if tail:
                self._transcript.append((self.WEARER_LABEL, tail))
                self._wearer_turns += 1
                asyncio.get_running_loop().create_task(
                    self._show_wearer_turn(tail)
                )
                log.info("nomination carried a question chars=%d", len(tail))
            return False
        # "tear that apart" / "build on that" ROUTE the next turn; they are
        # instructions about the relay, not remarks to be answered. Quoting
        # them as a YOU turn would make the collaborators discuss the
        # wearer's stage direction.
        mode = parse_relay_mode(said)
        if mode is not None:
            self._next_mode = mode
            log.info("wearer routed the next turn: %s", mode)
            # A stage direction with a substantial tail is a direction AND
            # a remark — "tear that apart, especially the latency claim" —
            # and dropping the tail swallows the wearer's contribution.
            # The bare direction stays unquoted, as before.
            normal = " ".join(re.sub(r"[^a-z0-9' ]+", " ", said.lower()).split())
            hit = _mode_at_start(normal)
            tail = normal[hit[1]:].strip() if hit is not None else ""
            if len(tail.split()) <= 3:
                return True
        self._transcript.append((self.WEARER_LABEL, said))
        self._wearer_turns += 1
        asyncio.get_running_loop().create_task(self._show_wearer_turn(said))
        log.info("wearer added a turn chars=%d", len(said))
        return True

    def transcript(self) -> tuple[tuple[str, str], ...]:
        """This session's turns, for a branch to carry forward.

        Includes the earlier conversation when this session is itself a
        continuation, so a branch of a branch keeps the whole thread rather
        than only the newest fork.
        """
        return (*self._prior, *self._transcript)

    @property
    def topic(self) -> str:
        return self._topic

    def last_reply(self) -> tuple[str, str] | None:
        """The newest collaborator turn — what "that" refers to.

        The wearer's own turns are skipped: "make that shorter" means the
        answer they just read, not the thing they themselves just said.
        Failed turns are skipped too — a provider's error text is not a
        position, and rewording it would be acting on a stack trace.
        """
        for label, text in reversed(self._transcript):
            if label == self.WEARER_LABEL or is_failure(text):
                continue
            return label, text
        return None

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
        mode = parse_relay_mode(text, self._table)
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

    def terminal_owns_hud(self) -> bool:
        callback = getattr(self._sink, "terminal_owns_hud", None)
        return bool(callback()) if callable(callback) else False

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
        adopt = getattr(self._sink, "adopt_identity", None)
        if adopt is not None:
            adopt(self.session_id, self.parent_id, self.root_id)
        await self._sink.open(self._topic, self._table.names)
        outcome = "no judge — one round"
        for round_no in range(1, self._max_rounds + 1):
            self._set_phase("independent" if round_no == 1 else "cross_review")
            # One turn from every seated collaborator — the wearer's round
            # invariant. The snapshot makes mid-round redirects reorder the
            # seats still owing a turn instead of recounting them.
            self._table.start_round()
            await self._report_progress(round_no)
            # Round 1 only: every seat is independent, so start them all
            # now and show them in order. Measured, both real binaries:
            # ~7.3s of spawn-and-auth before a token, then 12-40s of
            # generation. Serial, that is paid once per seat; concurrent,
            # the round costs about its slowest seat. The wearer still
            # watches one turn at a time — only the waiting is shared.
            self._prefetch = (
                self._prefetch_round_one() if round_no == 1 else {}
            )
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
                # FAIL CLOSED — docs/BRAINSTORM.md:44-45: "an illegible
                # judgment fails closed and never green-lights convergence."
                # This broke the loop instead, which is the OPPOSITE
                # failure: an unreadable judge ended the session at that
                # round, indistinguishable on the glass from a converged
                # one. Not-converged means the room goes around again;
                # max_rounds is what bounds the loop, not the judge's
                # legibility.
                if round_no == self._max_rounds:
                    outcome = (
                        f"no convergence after {round_no} round(s) "
                        "(judge unreadable)"
                    )
                    break
                outcome = f"judge unreadable at round {round_no} — continuing"
                continue
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
            if not self._cancelled:
                await self._verify_synthesis(text)

    async def _verify_synthesis(self, text: str) -> None:
        """Say what the "verified synthesis" actually rests on (the audit's B3).

        The synthesis prompt forbids asserting unverified claims, but the
        label promised verification the pipeline never performed on the
        synthesis itself, and a session in which nobody cited anything
        still ended under the same banner. One VERIFY line closes the gap:
        the synthesis's own citations are checked when it offers any, and
        otherwise the wearer is told plainly how many sources the whole
        session had checked — which may be none.
        """
        citations = parse_citations(text)
        if citations:
            try:
                await self._verify_visibly("JOAT (synthesis)", text)
            except Exception:
                log.exception("synthesis verification failed")
                await self._sink.turn("VERIFY")
                await self._sink.chunk("the synthesis's sources could not be checked\n")
            return
        checked = sum(
            1 for entry in self._verify_summary
            if "sources check out" in entry or "sources don't hold up" in entry
        )
        await self._sink.turn("VERIFY")
        if checked:
            await self._sink.chunk(
                f"the synthesis cites nothing new; {checked} earlier turn(s) had their sources checked\n"
            )
        else:
            await self._sink.chunk(
                "no sources were cited in this session — this is the collaborators' "
                "agreement, not verified fact\n"
            )

    async def _judge_turn(self, round_no: int) -> str:
        assert self._judge is not None
        # Name the seat that actually judges. It read "local" while the
        # verdict came from a cloud CLI (the audit's A7).
        judge_name = str(getattr(self._judge, "name", "") or "local")
        await self._sink.turn(f"JUDGE ({judge_name}) — round {round_no}")
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
        """What the collaborators are shown: the earlier conversation, then
        this one. A continuation that dropped the earlier turns would be a
        new session wearing an old title."""
        turns = [*self._prior, *self._transcript]
        return "\n\n".join(f"[{label}]\n{text}" for label, text in turns)

    def _prefetch_round_one(self) -> dict[str, AsyncIterator[str]]:
        """Launch every seat's independent first turn at once.

        Only round 1: from round 2 a seat's prompt contains what the others
        said, so there is nothing to start early — and starting it anyway
        would ask a collaborator to cross-review a transcript that does not
        exist yet.
        """
        started: dict[str, AsyncIterator[str]] = {}
        prior = [t for t in self._transcript if t[0] == self.WEARER_LABEL]
        # NOT the seat about to speak. The visible turn streams live, as it
        # always has, because a cancel mid-turn must keep what already
        # reached the glass — and a buffered producer runs ahead of the
        # display, so chunks generated before the wearer said stop would be
        # discarded unshown. Everyone BEHIND the current speaker is started
        # early, which is where the waiting actually was.
        speaking = self._table.peek()
        for seat in self._table.seats:
            if speaking is not None and seat.name == speaking.name:
                continue
            prompt = build_prompt(
                self._topic, seat, [*self._prior, *prior],
                self.SOURCES_CONTRACT, mode="open",
            )
            started[seat.name] = _eager(seat.stream(prompt))
        return started

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
        # A CONTINUATION's earlier conversation is never withheld: it is
        # the reason the wearer said "continue" rather than starting fresh,
        # and independence means not seeing the other seats' answers to THIS
        # question — not amnesia about the one before it.
        prior = (
            [*self._prior,
             *[t for t in self._transcript if t[0] == self.WEARER_LABEL]]
            if round_no == 1 else [*self._prior, *self._transcript]
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
        started = self._prefetch.pop(speaker.name, None)
        text = await self._stream_turn(
            label, started if started is not None else speaker.stream(prompt),
        )
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
        if not citations:
            # Silent. Most turns in a thinking session cite nothing — the
            # wearer is working an idea out loud, not writing a paper — and
            # painting "VERIFIER > no sources cited" under every one of them
            # spent a row per turn to report that nothing happened. The
            # summary still records it; the lens is for the discussion.
            self._verify_summary.append(f"{label}: no citations")
            return True, ""
        # VERIFY, exactly — docs/SESSION_MODEL.md:33 and BRAINSTORM.md:68 both
        # name the five transcript prefixes as YOU / CODEX / CLAUDE / VERIFY /
        # JOAT, and this emitted a sixth. Every durable record, export and
        # painted turn carried a label the spec does not have.
        await self._sink.turn("VERIFY")
        verdict = await self._gate.verify(citations, prose=text)
        marks = {
            CitationStatus.VERIFIED: "OK",
            CitationStatus.PARTIALLY_VERIFIED: "PARTIAL",
            CitationStatus.UNVERIFIABLE: "FAIL",
        }
        for cv in verdict.citations:
            await self._sink.chunk(f"[{cv.citation_id}] {marks[cv.status]} — {cv.reason}\n")
        # Words the wearer already uses. "DENIED — repair requested" is this
        # codebase talking to itself; they asked what our vocabulary meant.
        outcome = "sources check out" if verdict.passed else "sources don't hold up"
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

    def _verified_overall(self) -> bool | None:
        """The verifier's whole-session outcome, for the durable record.

        SESSION_MODEL.md:34 keeps "verification state" on the record; every
        stored row had null because nothing ever passed it. None = nothing
        needed checking (most thinking sessions — NOT a failure); the
        per-turn summary's LAST entry per speaker wins, because a repaired
        turn is re-verified and the repair is the standing outcome.
        """
        last: dict[str, bool] = {}
        for entry in self._verify_summary:
            label, _, outcome = entry.partition(": ")
            if outcome.startswith("sources check out"):
                last[label] = True
            elif outcome.startswith("sources don't hold up"):
                last[label] = False
        if not last:
            return None
        return all(last.values())

    async def _finish(self, status: str) -> None:
        self._set_phase("closing")
        # One line the wearer's own log can be searched for after a slow
        # session, instead of "it felt slow".
        if self._phase_seconds:
            log.info(
                "brainstorm phases: %s",
                ", ".join(
                    f"{name} {seconds:.1f}s"
                    for name, seconds in self._phase_seconds.items()
                ),
            )
        adopt = getattr(self._sink, "adopt_verification", None)
        if adopt is not None:
            adopt(self._verified_overall())
        await self._sink.close(status)

    def _set_phase(self, phase: str) -> None:
        # Time each phase as it closes. "JOAT is fucking slow" was about
        # THESE seconds — two subscription CLIs, a verifier and a judge per
        # round — and nothing measured them: voice_latency.py covers
        # endpointing/STT/first-reply, and a Brainstorm turn was timed once,
        # by hand, in a session that is now history. An unmeasured cost
        # regresses silently.
        #
        # Content-free by construction, matching the evidence boundary
        # (docs/BRAINSTORM.md:125-128): a FIXED phase name and an elapsed
        # float. No topic, no turn, no provider payload — the names are the
        # same handful this class already emits to the HUD.
        now = time.monotonic()
        if self._phase_started is not None and self._phase_name:
            self._phase_seconds[self._phase_name] = (
                self._phase_seconds.get(self._phase_name, 0.0)
                + (now - self._phase_started)
            )
        self._phase_name, self._phase_started = phase, now
        if self._on_phase is not None:
            self._on_phase(phase)

    #: Every phase name this class emits. A CLOSED set, named here rather
    #: than left implicit: the timings are a diagnostic, so what may appear
    #: as a key is a privacy boundary, and a test that re-lists these from
    #: memory gets them wrong (mine did — it forgot meta_review).
    PHASES = (
        "opening", "independent", "cross_review",
        "meta_review", "synthesis", "closing",
    )

    def phase_timings(self) -> dict[str, float]:
        """Seconds spent per phase, for a diagnostic or a receipt."""
        return dict(self._phase_seconds)


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
