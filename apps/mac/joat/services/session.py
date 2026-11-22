"""Session model: active session arbitration, notification queuing, and terminal actions."""

from __future__ import annotations

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from enum import StrEnum

from ..channels.cards import Card, CardLog, Urgency
from ..core.protocol import (
    AudioSource,
    HudNotificationCard,
    HudNotifications,
    TerminalAction,
)
from .agent_terminal import ApprovalReceipt, parse_reply_route
from .hud import HudWriter

MAX_NAVIGATION_DESTINATION_CHARS = 200


class SessionStartResult(StrEnum):
    """Admission result, distinct from whether a transcript was consumed."""

    STARTED = "started"
    BUSY = "busy"
    UNAVAILABLE = "unavailable"
    CLOSED = "closed"
    INVALID = "invalid"


_MATERIAL_VERB = re.compile(
    r"^(?P<instruction>"
    r"(?:make|turn|rewrite|reword|shorten|trim|summarise|summarize|expand|"
    r"elaborate|explain|simplify|clarify|translate|convert|tighten|"
    r"compare|extract|list|outline|draft|title|name|critique|check|"
    r"fix|clean|polish|tidy|put|give|show|read|repeat|shorten|lengthen|"
    r"bullet|number|rank|group|sort|caption|quote)"
    r"\b.*?"
    r"\b(?:(?:that|it|this)"
    r"(?=\s+(?:down|up|out|into|in|as|to|for|with|and|again|please|"
    r"a|an|more|less|shorter|longer|simpler|plainer|tighter|better)\b"
    r"|\s*$)"
    r"|the\s+(?:last\s+|latest\s+)?"
    r"(?:reply|answer|response|message|point|turn))"
    r".*)$",
    re.IGNORECASE,
)


def notification_stack(active: Sequence[Card]) -> HudNotifications:
    """Every unread JOAT card, newest first, as the glasses' notification card reads them."""
    return HudNotifications(tuple(
        HudNotificationCard(
            card_id=card.card_id,
            app=card.channel,
            sender=card.source,
            body=card.body or card.summary,
            at_unix_ms=int(card.at * 1000),
            urgent=card.urgency is Urgency.URGENT,
        )
        for card in reversed(active)
    ))


_SAVE_REPLY = re.compile(
    r"^(?:save|note|keep|remember|record|log|write|jot|add) (?:the )?"
    r"(?:(?P<who>codex|claude|antigravity)(?: ?s)? )?"
    r"(?:last |latest )?(?:reply|response|answer|that|this|it)"
    r"(?: down| as a note| to (?:my )?notes)?$"
)

log = logging.getLogger(__name__)

_CARD_BRIGHTNESS = {Urgency.NOTIFY: 3, Urgency.URGENT: 4}
_QUEUED_CARD_LIMIT = 6
CANCEL_GRACE_S = 8.0
_UNSET_ADDRESSED = "\x00joat-unset-addressed"


class Session(ABC):
    """A focused interaction that owns the HUD until run() returns."""

    @abstractmethod
    async def run(self) -> None: ...

    @abstractmethod
    def cancel(self) -> None: ...

    def direct(self, text: str) -> str | None:
        return None

    def consume_transcript(self, text: str) -> bool:
        return False

    async def replay_hud(self) -> bool:
        return False

    @property
    def status(self) -> str:
        return "JOAT · session active"

    @property
    def completion_state(self) -> str:
        return "settled"


SessionFactory = Callable[[str], Session | None]
SessionStarter = Callable[[str], Session | None]


class SessionManager:
    """Routes transcripts, arbitrates notifications, and dispatches terminal actions."""

    def __init__(
        self,
        hud: HudWriter,
        factory: SessionFactory | None = None,
        card_log: CardLog | None = None,
        terminal_capture: Callable[[], Awaitable[None]] | None = None,
        on_state: Callable[[str], None] | None = None,
        starters: Sequence[SessionStarter] = (),
        terminal_retry: Callable[[], Awaitable[None]] | None = None,
        terminal_cancel: Callable[[], Awaitable[str]] | None = None,
        route_reply: Callable[[str | None, str | None], Awaitable[str]] | None = None,
        save_reply: Callable[[str | None], Awaitable[str]] | None = None,
        terminal_decide: Callable[[str], Awaitable[str | ApprovalReceipt]] | None = None,
        terminal_cycle: Callable[[], Awaitable[str]] | None = None,
        last_reply: Callable[[str | None], tuple[str, str] | None] | None = None,
        transform: Callable[[str, str], Awaitable[str]] | None = None,
        terminal_view_current: Callable[[], bool] | None = None,
        surface_revision: Callable[[], int] | None = None,
        navigation_factory: Callable[[str], Session | None] | None = None,
    ) -> None:
        self._hud = hud
        self._factory = factory
        self._navigation_factory = navigation_factory
        self._active: Session | None = None
        self._session_revision = 0
        self._task: asyncio.Task[None] | None = None
        self._pending_cards: list[tuple[Card, asyncio.Future[None] | None]] = []
        self._card_log = card_log
        self._home_notification_id: str | None = None
        self._notification_refresh_task: asyncio.Task[None] | None = None
        self._notification_refresh_pending = False
        self._terminal_capture = terminal_capture
        self._terminal_view_current = terminal_view_current
        self._surface_revision = surface_revision or (lambda: 0)
        self._on_state = on_state
        self._starters = list(starters)
        self._terminal_retry = terminal_retry
        self._terminal_cancel = terminal_cancel
        self._closed = False
        self._route_reply = route_reply
        self._terminal_decide = terminal_decide
        self._terminal_cycle = terminal_cycle
        self._last_reply = last_reply
        self._transform = transform
        self._save_reply = save_reply

    @property
    def active(self) -> bool:
        return self._active is not None

    @property
    def active_status(self) -> str | None:
        return self._active.status if self._active is not None else None

    @property
    def active_session(self) -> Session | None:
        return self._active

    @property
    def session_revision(self) -> int:
        return self._session_revision

    def set_answer_probe(self, probe: Callable[[], bool]) -> None:
        self._answer_probe = probe

    def _answer_being_read(self) -> bool:
        probe = getattr(self, "_answer_probe", None)
        try:
            return bool(probe()) if probe is not None else False
        except Exception:
            return False

    def notify(self, card: Card) -> None:
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
            try:
                self._pending_cards.remove(entry)
            except ValueError:
                pass
            raise

    async def _show_card(self, card: Card) -> None:
        if self._card_log is not None:
            self._card_log.begin_delivery(card.card_id)
        try:
            await self._paint(card.render(), card.urgency, trace_id=card.card_id)
        except BaseException:
            if self._card_log is not None:
                self._card_log.finish_delivery(card, accepted=False)
            raise
        if self._card_log is not None:
            self._home_notification_id = card.card_id
            self._card_log.finish_delivery(card, accepted=True)
            self._schedule_notification_refresh()

    def _schedule_notification_refresh(self) -> None:
        if self._closed or self._card_log is None:
            return
        self._notification_refresh_pending = True
        if self._notification_refresh_task is not None:
            return

        async def refresh() -> None:
            try:
                while self._notification_refresh_pending and not self._closed:
                    self._notification_refresh_pending = False
                    try:
                        await self.publish_home_notifications()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("notification tray refresh failed after accepted Main delivery")
            finally:
                self._notification_refresh_task = None

        self._notification_refresh_task = asyncio.get_running_loop().create_task(refresh())

    def reset_home_notification_identity(self) -> None:
        self._home_notification_id = None

    async def publish_home_notifications(self) -> None:
        if self._card_log is None:
            return
        active = self._card_log.active()
        previous = [card for card in reversed(active) if card.card_id != self._home_notification_id][:2]
        await self._hud.tray("\n".join(card.home_row() for card in previous))

    def _drain_after_answer(self) -> None:
        existing = getattr(self, "_answer_drain", None)
        if existing is not None and not existing.done():
            return

        async def wait_then_drain() -> None:
            for _ in range(240):
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
        self._pending_cards.append(entry)
        while len(self._pending_cards) > _QUEUED_CARD_LIMIT:
            _card, future = self._pending_cards.pop(0)
            if future is not None and not future.done():
                future.set_exception(
                    RuntimeError("notification queue overflowed before HUD write")
                )

    async def _paint(self, rendered: str, urgency: Urgency, *, trace_id: str | None = None) -> None:
        await self._hud.show_with_brightness(
            rendered,
            _CARD_BRIGHTNESS.get(urgency, 3),
            content_kind="notification",
            trace_id=trace_id,
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
        visible_card = selected[-1][0]
        loudest = max((card.urgency for card, _ in selected), default=Urgency.NOTIFY)

        async def show_and_resolve() -> None:
            try:
                if self._card_log is not None:
                    self._card_log.begin_delivery(visible_card.card_id)
                await self._paint(visible_card.render(), loudest, trace_id=visible_card.card_id)
            except BaseException as exc:
                if self._card_log is not None:
                    self._card_log.finish_delivery(visible_card, accepted=False)
                for _, future in selected:
                    if future is not None and not future.done():
                        future.set_exception(exc)
                log.exception("queued notification HUD write failed")
            else:
                if self._card_log is not None:
                    for card, _ in selected[:-1]:
                        self._card_log.add(card)
                    self._home_notification_id = visible_card.card_id
                    self._card_log.finish_delivery(visible_card, accepted=True)
                for _, future in selected:
                    if future is not None and not future.done():
                        future.set_result(None)
                self._schedule_notification_refresh()

        asyncio.get_running_loop().create_task(show_and_resolve())

    def handle_transcript(
        self,
        text: str,
        *,
        capture_source: AudioSource | None = None,
        addressed: str | None = _UNSET_ADDRESSED,
    ) -> bool:
        """True = consumed by the active session or a session starter; False = default flow."""
        _ = capture_source
        lowered = text.lower().strip().rstrip(".!?")
        if self._active is not None:
            escape = lowered
            for wake in ("joat,", "joat"):
                if escape.startswith(wake + " "):
                    escape = escape[len(wake):].strip()
                    break
            escape = escape.replace("’", "").replace("'", "")
            if escape in (
                "stop", "cancel", "stop it", "cancel that", "cancel it",
                "stop the session", "end the session", "never mind", "nevermind",
                "thats enough", "that is enough", "enough",
                "were done", "we are done", "done",
                "forget it", "abort", "quit", "end it", "stop this",
                "cancel this",
            ):
                log.info("wearer cancelled the active session")
                self._cancel_active()
                return True
            steer = text if addressed is _UNSET_ADDRESSED else addressed
            if not steer:
                log.info("session active — unaddressed speech ignored chars=%d", len(text))
                return True
            if self._handle_instruction(steer):
                return True
            if self._active.consume_transcript(steer):
                return True
            directed = self._active.direct(steer)
            if directed is not None:
                log.info("wearer gave the floor to %s", directed)
            else:
                log.info("session active — transcript not routed to assistant chars=%d", len(text))
            return True

        for starter in self._starters:
            session = starter(text)
            if session is not None:
                return self.try_start_session(session, publish_state=False)

        spoken = text if addressed is _UNSET_ADDRESSED else addressed
        if spoken and self._handle_instruction(spoken):
            return True
        return False

    def _handle_instruction(self, steer: str) -> bool:
        normalized = " ".join(re.sub(r"[^a-z0-9]+", " ", steer.lower()).split())
        route = parse_reply_route(normalized)
        if route is not None and self._route_reply is not None:
            router = self._route_reply

            async def run_route() -> None:
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
        instruction = _MATERIAL_VERB.match(steer.strip().rstrip("."))
        if (
            instruction is not None
            and self._transform is not None
            and self._save_reply is not None
        ):
            asked = instruction.group("instruction").strip()
            asyncio.get_running_loop().create_task(self._act_on_material(asked))
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
        if not text:
            return
        try:
            await self._hud.receipt(text, answers_wearer=True)
        except Exception:
            log.warning("verb receipt not painted", exc_info=True)

    async def _act_on_material(self, instruction: str) -> None:
        material = self._material()
        if not material:
            await self._say("There is no answer to work with yet — ask something first.")
            return
        try:
            answer = await self._transform(instruction, material)  # type: ignore[misc]
        except Exception as exc:
            log.exception("instruction on the last reply failed")
            await self._say(f"I could not do that — {str(exc) or exc.__class__.__name__}")
            return
        if not answer:
            await self._say("I had nothing useful to say about that.")
            return
        try:
            await self._hud.assistant(answer, answers_wearer=True)
        except Exception:
            log.warning("instruction result not painted", exc_info=True)

    def _material(self) -> str:
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

    def handle_terminal_action(self, action: TerminalAction) -> None:
        """Route an explicit G2 Agent Terminal gesture."""
        if action.action in ("approve_terminal", "deny_terminal"):
            decide = self._terminal_decide
            if decide is not None:
                verdict = "approve" if action.action == "approve_terminal" else "deny"

                async def run_decision() -> None:
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
                        if not self.terminal_owns_hud():
                            await self._say(f"Recorded: {recorded}.")
                    elif outcome == "conflict":
                        await self._say(
                            f"Recorded as {recorded} — another controller answered first."
                        )
                    elif outcome == "no_receipt":
                        await self._say("The gateway gave no receipt for that answer.")
                    else:
                        await self._say(sentence or "That answer did not go through.")

                asyncio.get_running_loop().create_task(run_decision())
            return

        if action.action == "cancel_terminal":
            if self._closed or self._terminal_cancel is None:
                return
            cancel_operation = self._terminal_cancel()

            async def run_cancel() -> None:
                try:
                    receipt = await cancel_operation
                except Exception as exc:
                    reason = str(exc) or exc.__class__.__name__
                    log.exception("terminal cancel failed")
                    await self._say(f"Could not cancel — {reason}")
                    return
                if receipt:
                    await self._say(receipt)

            asyncio.get_running_loop().create_task(run_cancel())
            return

        if action.action == "retry_terminal":
            if self._closed or self._terminal_retry is None:
                return
            operation = self._terminal_retry()

            async def run_retry() -> None:
                try:
                    await operation
                except Exception as exc:
                    reason = str(exc) or exc.__class__.__name__
                    log.exception("terminal retry failed")
                    await self._say(f"Could not reach it — {reason}")

            asyncio.get_running_loop().create_task(run_retry())
            return

        if action.action == "begin_terminal_input" and (self._active is None or self.terminal_owns_hud()):
            if self._closed or self._terminal_capture is None:
                return
            publication = self._terminal_capture()

            async def run_arm() -> None:
                await publication

            asyncio.get_running_loop().create_task(run_arm())

    async def replay_active_hud(self) -> bool:
        active = self._active
        return await active.replay_hud() if active is not None else False

    def terminal_owns_hud(self) -> bool:
        if self._terminal_view_current is not None and self._terminal_view_current():
            return True
        active = self._active
        probe = getattr(active, "terminal_owns_hud", None) if active else None
        return bool(probe()) if callable(probe) else False

    def _cancel_active(self) -> None:
        active = self._active
        if active is None:
            return
        active.cancel()
        task = self._task
        if task is not None and not task.done():
            task.cancel()
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
        self._release(task)

    def _release(self, task: asyncio.Task[None]) -> None:
        if self._task is not task:
            return
        self._active = None
        self._task = None
        if self._on_state is not None:
            self._on_state("cancelled")
        self._drain_cards()

    def start_navigation(self, destination: str) -> SessionStartResult:
        """Start a route to a typed destination, the way 'navigate to …' does."""
        if self._closed:
            return SessionStartResult.CLOSED
        if (
            not isinstance(destination, str) or not destination.strip()
            or len(destination) > MAX_NAVIGATION_DESTINATION_CHARS or "\x00" in destination
        ):
            return SessionStartResult.INVALID
        try:
            destination.encode("utf-8")
        except UnicodeEncodeError:
            return SessionStartResult.INVALID
        if self._navigation_factory is None:
            return SessionStartResult.UNAVAILABLE
        if self._active is not None:
            return SessionStartResult.BUSY
        try:
            session = self._navigation_factory(destination.strip())
        except Exception as exc:
            log.warning("typed navigation factory failed error=%s", type(exc).__name__)
            return SessionStartResult.UNAVAILABLE
        if session is None:
            return SessionStartResult.UNAVAILABLE
        if self.try_start_session(session, publish_state=False):
            return SessionStartResult.STARTED
        return SessionStartResult.CLOSED if self._closed else SessionStartResult.BUSY

    def try_start_session(self, session: Session, *, publish_state: bool = False) -> bool:
        if self._closed or self._active is not None:
            return False
        return self._start_session(session, publish_state=publish_state)

    def _start_session(self, session: Session, *, publish_state: bool) -> bool:
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
                if self._task is asyncio.current_task():
                    self._active = None
                    self._task = None
                    if publish_state and self._on_state is not None:
                        self._on_state(completion_state)
                    self._drain_cards()

        self._task = asyncio.get_running_loop().create_task(runner())
        return True

    async def close(self, timeout_s: float = 5.0) -> None:
        self._closed = True
        self._notification_refresh_pending = False
        refresh = self._notification_refresh_task
        if refresh is not None and refresh is not asyncio.current_task():
            refresh.cancel()
            await asyncio.gather(refresh, return_exceptions=True)
        session = self._active
        task = self._task
        if session is not None:
            session.cancel()
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout_s)
        except asyncio.CancelledError:
            if not task.done():
                raise
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
