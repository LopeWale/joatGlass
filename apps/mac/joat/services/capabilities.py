"""Answers the on-glasses capability menu.

The plugin's menu is navigation only — the DATA lives here. When the wearer
picks a row, the plugin sends an `input` event with containerName "capmenu"
and the row's name; this router draws the view. It also owns the two other
glasses-initiated requests: the contextual menu's "repeat last reply"
(containerName "sysmenu") and the legacy repaint request older plugin builds
sent after a menu closed. Current builds restore a buffered dashboard locally.

Deterministic by design: these are reflexes to explicit gestures, not model
decisions.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets
import sqlite3
import time
from collections.abc import Awaitable
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone, tzinfo
from typing import Any, Callable, Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..channels.cards import Card, CardLog
from ..channels.imessage import IMessageStore, MessageEntry, MessageHistoryPage, MessageThread
from ..core.protocol import (
    VOICE_REQUEST_PREFIX,
    AgentTerminalHudView,
    BrainstormHudView,
    EventType,
    HudList,
    InputEvent,
    PlanningGridCell,
    PlanningGridEntry,
)
from ..core.interfaces import GlassesTransport
from ..native.bridge import (
    NativeBridgeClient,
    NativeBridgeError,
    NativeBridgeProtocolError,
    NativeBridgeUnavailable,
)
from ..persistence.actions import ActionActivity, ActionJournal
from ..persistence.notes import NoteStore, QuickNote
from ..persistence.voice_notes import VoiceNote, VoiceNoteStore
from .hud import HudWriter
from .sinks import DebateSnapshot
from .voice_note_capture import VoiceNoteCaptureSnapshot
from ..core.wearer_zone import WearerZone
from .hermes_productivity import system_local_time_zone
from .planning import (
    PlanningAuthorizationError,
    PlanningEntry,
    PlanningProjection,
    PlanningScope,
    advance_selected_day,
    load_native_planning,
)
from .status import StatusPublisher
from .message_reply import MessageReplyController
from .message_ui import MessageUIController
from .panels import PanelPublisher
from .reader import paginate_reader_text

log = logging.getLogger(__name__)

BACK_HINT = "left x2: back"
DETAIL_HINT = "tap: detail · left x2: back"
PLANNING_HINT = "tap: choose · swipe: move · left x2: back"
MESSAGES_ACCESS_ITEMS = [
    "Tap to retry after granting Full Disk Access.",
    "Mac Settings · Privacy & Security · Full Disk Access",
]
MESSAGES_RETRY_ITEMS = [
    "Messages is temporarily unavailable.",
    "Tap to try Messages again.",
]
NEW_MESSAGE_ROW = "New message · voice recipient + text"


# These strings are the only explanation of a terminal health state that
# leaves the Mac. The status model retains compact, allowlisted reason codes
# for diagnostics; displaying those codes on the lenses forces a wearer away
# from the home brain to translate a developer state.
_AGENT_TERMINAL_RECOVERY_COPY: dict[tuple[str, str], str] = {
    (
        "approval_required",
        "write_scope_required",
    ): "Approve the JOAT device in OpenClaw, then tap retry.",
    (
        "unavailable",
        "gateway_not_ready",
    ): "Hermes is unavailable; retry when the Mac connection recovers.",
    (
        "blocked",
        "tools_enabled",
    ): "Hermes tools are enabled; this JOAT session remains blocked.",
    (
        "blocked",
        "dedicated_session_not_proven",
    ): "Hermes session isolation is not proven yet; tap retry.",
    (
        "blocked",
        "history_recovery_required",
    ): "Hermes needs recovery before it can continue; tap retry.",
}


def _agent_terminal_instruction(state: str, reason: str) -> str:
    """Translate a safe terminal reason into a wearable next action.

    This never forwards a gateway exception, URL, or pairing identifier. A
    generic fallback still tells the wearer what their tap does when a newer
    engine introduces another allowlisted safe reason.
    """

    fixed = _AGENT_TERMINAL_RECOVERY_COPY.get((state, reason))
    if fixed is not None:
        return fixed
    if state == "approval_required":
        return "Approve JOAT in OpenClaw, then tap retry."
    if state == "unavailable":
        return "Hermes is unavailable; retry when the Mac connection recovers."
    if state == "blocked":
        return "Hermes is not ready; correct it, then tap retry."
    return ""


@dataclass(frozen=True, slots=True)
class _PlanningRow:
    """One deliberate drill-down action in the native Planning projection."""

    label: str
    action: Literal["scope", "previous", "next", "week", "day", "entry"]
    scope: PlanningScope | None = None
    day: date | None = None
    entry: PlanningEntry | None = None


class VoiceCaptureControls(Protocol):
    """The narrow live-recording surface the QuickNotes page drives.

    During an exclusive capture the microphone belongs to the recording, so
    these HUD taps are the wearer's only controls; each returns the same
    receipt string the (paused-state) voice grammar produces.
    """

    @property
    def active(self) -> bool: ...

    @property
    def discard_pending(self) -> bool: ...

    @property
    def snapshot(self) -> VoiceNoteCaptureSnapshot: ...

    def control_pause(self) -> str: ...

    def control_resume(self) -> str: ...

    def control_request_discard(self) -> str: ...

    def control_confirm_discard(self) -> str: ...

    def control_keep(self) -> str: ...

    async def control_stop(self) -> str: ...


_RECORDING_CONTROLS: dict[str, tuple[tuple[str, str], ...]] = {
    "recording": (
        ("Save and finish", "stop"),
        ("Pause recording", "pause"),
        ("Discard recording", "discard"),
    ),
    "paused": (
        ("Resume recording", "resume"),
        ("Save and finish", "stop"),
        ("Discard recording", "discard"),
    ),
    "discard_pending": (
        ("Confirm discard", "confirm_discard"),
        ("Keep recording", "keep"),
    ),
}


@dataclass(frozen=True, slots=True)
class _NoteRow:
    """One row on the unified QuickNotes page: recording control or note."""

    label: str
    kind: Literal["control", "voice", "text"]
    control: str | None = None
    voice_note: VoiceNote | None = None
    text_note: QuickNote | None = None


@dataclass(slots=True)
class _NoteReaderState:
    title: str
    label: str
    pages: tuple[tuple[str, ...], ...]
    trace_id: str
    index: int = 0
    page_pending: bool = False


@dataclass(slots=True)
class _MessageHistoryState:
    """Mac-private page stack for the currently selected conversation."""

    thread: MessageThread
    pages: list[MessageHistoryPage]
    index: int = 0


@dataclass(slots=True)
class _MessageDetailState:
    """Mac-private reader pages for one entry in the selected conversation."""

    thread: MessageThread
    entry: MessageEntry
    pages: tuple[tuple[str, ...], ...]
    index: int = 0


def _age(unix: float) -> str:
    mins = max(0, int((time.time() - unix) / 60))
    if mins < 1:
        return "now"
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    return f"{hours}h" if hours < 24 else f"{hours // 24}d"


_NEW_BRAINSTORM_ROW = "+ New brainstorm"


def _session_row(past: Any) -> str:
    """One stored session, as the wearer would pick it out of a list.

    Title first because that is what they scan for; the metadata is
    budgeted after it so a long question cannot push the age off the end.
    """
    count = len(past.turns)
    meta = f" · {count} turn{'' if count == 1 else 's'} · {past.age()}"
    room = max(8, 46 - len(meta))
    title = past.title
    if len(title) > room:
        title = title[:room - 1].rstrip() + "…"
    return f"{title}{meta}"


def system_home_zone() -> tzinfo:
    """This Mac's zone, as a real IANA ZoneInfo where the OS exposes one.

    A fixed UTC offset from ``datetime.now().astimezone()`` looks usable
    today but shifts future dates into the wrong civil day at a DST
    transition.  The same resolver that validates Calendar proposals
    returns a real ``ZoneInfo`` on macOS; the fallback is only for hosts
    without an IANA ``/etc/localtime`` symlink.
    """

    zone_name = system_local_time_zone()
    if zone_name is not None:
        try:
            return ZoneInfo(zone_name)
        except ZoneInfoNotFoundError:
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


class CapabilityRouter:
    """Glasses gesture in, drawn view out."""

    def __init__(
        self,
        hud: HudWriter,
        transport: GlassesTransport,
        status: StatusPublisher,
        messages: IMessageStore | None,
        cards: CardLog,
        replies: MessageReplyController | None = None,
        personal_data: NativeBridgeClient | None = None,
        notes: NoteStore | None = None,
        voice_notes: VoiceNoteStore | None = None,
        voice_capture: VoiceCaptureControls | None = None,
        actions: ActionJournal | None = None,
        acceptance_armed: bool = False,
        terminal_view: Callable[[], Awaitable[None]] | None = None,
        brainstorm_recap: Callable[[], DebateSnapshot] | None = None,
        brainstorm_replay: Callable[[], Awaitable[bool]] | None = None,
        brainstorm_sessions: Callable[[], list[Any]] | None = None,
        brainstorm_continue: Callable[[str], Awaitable[bool]] | None = None,
        terminal_rows: Callable[[], list[tuple[str, str]]] | None = None,
        terminal_focus: Callable[[str], Awaitable[None]] | None = None,
        terminal_busy: Callable[[], bool] | None = None,
        brainstorm_recent: Callable[..., list[str]] | None = None,
        panels: PanelPublisher | None = None,
        wearer_zone: WearerZone | None = None,
        terminal_open: Callable[[str, str | None, str, Callable[[], bool]], Awaitable[None]] | None = None,
        terminal_current: Callable[[], str | None] | None = None,
        message_ui: MessageUIController | None = None,
    ) -> None:
        self._hud = hud
        # Defaults to this Mac's zone, which is exactly the pre-wearer-zone
        # behaviour for any construction that does not inject one.
        self._wearer_zone = wearer_zone or WearerZone(system_home_zone)
        self._transport = transport
        self._status = status
        self._messages = messages
        self._cards = cards
        self._replies = replies
        self._message_ui = message_ui
        self._personal_data = personal_data
        self._notes = notes
        self._voice_notes = voice_notes
        self._voice_capture = voice_capture
        self._actions = actions
        self._acceptance_armed = acceptance_armed
        self._terminal_view = terminal_view
        self._brainstorm_recap = brainstorm_recap
        # Restores a LIVE session's own frame — see _show_brainstorm.
        self._brainstorm_replay = brainstorm_replay
        self._brainstorm_sessions = brainstorm_sessions
        self._brainstorm_continue = brainstorm_continue
        #: What the picker last drew, so a row tap resolves to a
        #: session id rather than to a row number that may have moved.
        self._brainstorm_rows: list[Any] = []
        self._terminal_rows = terminal_rows
        self._terminal_focus = terminal_focus
        self._terminal_open = terminal_open
        self._terminal_current = terminal_current
        self._terminal_opening_revision = 0
        self._terminal_selection_id: str | None = None
        self._terminal_busy = terminal_busy
        self._terminal_row_names: list[str] = []
        self._brainstorm_recent = brainstorm_recent
        # The overview's News / Pomodoro / Search tabs. Their rows are
        # pushed, not requested, so their taps carry a content revision
        # instead of one of this router's request generations.
        self._panels = panels
        self._message_rows: list[MessageThread] = []
        self._message_history: _MessageHistoryState | None = None
        self._message_detail: _MessageDetailState | None = None
        self._message_page_pending = False
        self._message_selection_epoch = 0
        # The reply controller keeps its selected handle private. Mirror only
        # whether this router armed one, so a new wearer-visible picker can
        # reliably disarm it without clearing an unrelated controller state.
        self._message_selection_active = False
        self._notification_rows: list[Card] = []
        # Calendar and Reminders have different EventKit stores, but a wearer
        # navigates time, not stores. Keep one bounded read-only projection and
        # the action rows that produced its current visible page.
        self._planning_selected_day = datetime.now().astimezone().date()
        # The first wearable page is the calendar itself. A wearer chooses a
        # week, then a day, instead of first having to navigate past a control
        # panel before seeing their month.
        self._planning_scope = PlanningScope.MONTH
        self._planning_rows: list[_PlanningRow] = []
        self._note_rows: list[_NoteRow] = []
        self._note_reader: _NoteReaderState | None = None
        self._activity_rows: list[ActionActivity] = []
        self._active_request_id: str | None = None
        self._active_capability: str | None = None
        self._contextual_menu_open = False
        self._request_context: ContextVar[str | None] = ContextVar(
            f"capability_request_{id(self)}",
            default=None,
        )

    def on_input(self, event: InputEvent) -> None:
        """Transport callback — synchronous, so work is scheduled."""
        if event.container_name == "capability-lifecycle":
            if event.selected_name in {"surface-context-menu-open", "surface-context-menu-closed"}:
                self._contextual_menu_open = event.selected_name.endswith("-open")
                return
            if event.selected_name in {
                "surface-home", "surface-menu", "surface-settings", "surface-news",
                "surface-search", "surface-pomodoro", "surface-feature",
            }:
                # These transitions are local on the Hub. Retire ownership
                # synchronously, without painting over the page it just chose.
                self._terminal_opening_revision += 1
                self._active_capability = None
                self._active_request_id = None
                self._note_reader = None
                self.clear_message_selection("surface-departure")
                return
        if event.container_name == "capmenu" and event.selected_name:
            self.clear_message_selection("capability-open")
            self._terminal_opening_revision += 1
            revision = self._terminal_opening_revision
            self._active_capability = self._canonical_capability(event.selected_name)
            self._active_request_id = event.request_id

            async def open_requested() -> None:
                if revision != self._terminal_opening_revision:
                    return
                await self.open(
                    event.selected_name or "",
                    request_id=event.request_id,
                    planning_anchor_unix_ms=event.planning_anchor_unix_ms,
                )

            asyncio.get_running_loop().create_task(open_requested())
        elif event.container_name == "capdata" and event.selected_index is not None:
            terminal_revision = None
            message_epoch = None
            if event.selected_name == "MESSAGES":
                if (event.request_id != self._active_request_id
                        or self._active_capability != "MESSAGES"):
                    return
                if self._message_ui is not None and self._message_ui.matches(event.request_id, event.trace_id):
                    return
                self._message_selection_epoch += 1
                message_epoch = self._message_selection_epoch
                if self._replies is not None and self._message_selection_active:
                    self._replies.clear_selection("new-selection")
                self._message_selection_active = False
                self._message_history = None
                self._message_detail = None
                if self._message_ui is not None:
                    epoch = message_epoch
                    self._message_ui.select(event.request_id, event.trace_id or "hud",
                        lambda: epoch == self._message_selection_epoch
                        and event.request_id == self._active_request_id
                        and self._active_capability == "MESSAGES")
            if event.selected_name == "AGENT TERMINAL":
                if (event.request_id != self._active_request_id
                        or self._active_capability != "AGENT TERMINAL"):
                    return
                self._terminal_opening_revision += 1
                terminal_revision = self._terminal_opening_revision
            asyncio.get_running_loop().create_task(self._select_capability_row(
                event.selected_name or "",
                event.selected_index,
                event.trace_id or "hud",
                event.request_id,
                terminal_revision=terminal_revision,
                message_epoch=message_epoch,
            ))
        elif event.container_name == "message-cancel" and self._message_ui is not None:
            if self._message_ui.matches(event.request_id, event.trace_id):
                self._message_ui.cancel(event.selected_name or "")
        elif (
            event.container_name == "capgrid"
            and event.selected_index is not None
            and event.planning_anchor_unix_ms is not None
        ):
            # Dashboard right-pane tap: the hovered DATE travels as the
            # planning anchor and the item's position as the index. No
            # calendar/reminder identifier crosses the wire — the entry is
            # re-resolved from a fresh authoritative DAY projection, so a
            # native edit between paint and tap can never open the wrong item.
            asyncio.get_running_loop().create_task(self._select_grid_entry(
                event.planning_anchor_unix_ms,
                event.selected_index,
                event.request_id,
            ))
        elif event.container_name == "capmessage" and event.selected_index is not None:
            asyncio.get_running_loop().create_task(self._select_message_detail(
                event.selected_index,
                event.trace_id or "hud",
                event.request_id,
            ))
        elif (
            event.container_name == "caprefresh"
            and self._panels is not None
            and self._panels.owns(event.selected_name)
        ):
            # Long press at the top of an expanded tab: fetch it again.
            asyncio.get_running_loop().create_task(
                self._panels.refresh(event.selected_name, event.request_id)
            )
        elif (
            event.container_name == "cappage"
            and self._panels is not None
            and self._panels.owns(event.selected_name)
        ):
            # A boundary swipe inside an opened headline or result.
            asyncio.get_running_loop().create_task(
                self._panels.page(event.selected_name, event.event_type, event.request_id)
            )
        elif event.container_name == "cappage" and event.selected_name == "QUICKNOTES":
            reader = self._note_reader
            if (
                reader is None or reader.page_pending
                or self._active_capability != "QUICKNOTES"
                or event.request_id != self._active_request_id
                or event.trace_id != reader.trace_id
                or event.selected_index != reader.index
            ):
                return
            reader.page_pending = True
            asyncio.get_running_loop().create_task(self._page_note_reader(reader, event))
        elif event.container_name == "cappage" and event.event_type is not None:
            if self._message_page_pending:
                return
            self._message_page_pending = True
            task = asyncio.get_running_loop().create_task(self._page_message_history(
                event.event_type,
                event.trace_id or "hud",
                event.request_id,
            ))
            task.add_done_callback(lambda _task: self._clear_message_page_pending())
        elif event.container_name == "sysmenu" and event.selected_index == 1:
            asyncio.get_running_loop().create_task(self._repeat())
        elif (
            event.container_name == "capability-lifecycle"
            and event.selected_name in ("quicknotes-list", "quicknotes-home")
        ):
            if event.request_id == self._active_request_id and self._active_capability == "QUICKNOTES":
                self._note_reader = None
                if event.selected_name == "quicknotes-home":
                    self._note_rows = []
        elif (
            event.container_name == "capability-lifecycle"
            and event.selected_name == "quicknotes-back"
        ):
            asyncio.get_running_loop().create_task(
                self._cancel_note_discard(event.request_id)
            )
        elif (
            event.container_name == "capability-lifecycle"
            and event.selected_name in ("messages-home", "messages-list", "messages-conversation")
        ):
            if event.request_id is not None and (self._active_capability != "MESSAGES"
                    or event.request_id != self._active_request_id
                    or (self._message_ui is not None and self._message_ui.has_visit and event.trace_id is not None
                        and not self._message_ui.matches(event.request_id, event.trace_id))):
                return
            if event.selected_name == "messages-conversation":
                # Hub Back is local and restores its retained conversation
                # frame. Keep the selected thread armed, but retire the
                # reader cursor so the next page gesture addresses history.
                self._message_detail = None
                self._message_page_pending = False
            else:
                self.clear_message_selection(
                    "home" if event.selected_name == "messages-home" else "list"
                )
        elif (event.container_name == "capability-lifecycle"
              and event.selected_name == "terminal-home"):
            if (event.request_id == self._active_request_id
                    and event.trace_id == self._terminal_selection_id):
                self._terminal_opening_revision += 1
                self._terminal_selection_id = None
                self._active_capability = None
                self._active_request_id = None
        elif event.container_name == "menu" and event.selected_name == "closed":
            self._terminal_opening_revision += 1
            self._active_capability = None
            self._active_request_id = None
            self._note_reader = None
            self.clear_message_selection("home")
            asyncio.get_running_loop().create_task(self.repaint_home())

    def clear_message_selection(self, reason: str) -> None:
        """Disarm a private reply target when its wearer-visible context ends."""
        # A pending read must not re-arm the recipient after Back or disconnect,
        # even when the Hub retains the same request ID for its cached picker.
        self._message_selection_epoch += 1
        if self._message_ui is not None:
            self._message_ui.retire()
        # Back restores the same picker on the Hub. Its row mapping remains
        # valid, while the private reply target and reader must be disarmed.
        if reason != "list":
            self._message_rows = []
        self._message_history = None
        self._message_detail = None
        self._message_page_pending = False
        # Conversational drafts can exist without a picker selection. The
        # controller owns their lifetime, including pending confirmations.
        if self._replies is not None:
            self._replies.clear_selection(reason)
        self._message_selection_active = False

    def on_disconnect(self) -> None:
        """Drop any Mac-private reply target when the glasses link disappears."""
        self._terminal_opening_revision += 1
        self._active_capability = None
        self._active_request_id = None
        self._contextual_menu_open = False
        self.clear_message_selection("disconnect")
        self._note_reader = None

    async def open(
        self,
        name: str,
        *,
        request_id: str | None = None,
        planning_anchor_unix_ms: int | None = None,
    ) -> None:
        """Open one list generation; stale concurrent generations cannot paint."""
        self._terminal_opening_revision += 1
        self._note_reader = None
        if self._active_capability == "MESSAGES" and self._canonical_capability(name) != "MESSAGES":
            self.clear_message_selection("capability-open")
        self._active_request_id = request_id
        self._active_capability = self._canonical_capability(name)
        token = self._request_context.set(request_id)
        try:
            match name:
                case "Messages":
                    await self._show_messages()
                case "Notifications":
                    await self._show_notifications()
                # Both spellings: a wearer's installed plugin build may still send
                # "Debate", and a rename must not make the menu row inert.
                case "Brainstorm" | "Debate":
                    await self._show_brainstorm()
                case "Agent Terminal":
                    await self._show_agent_terminal()
                # Old installed packages can still send either label. All
                # aliases converge on one Planning surface instead of
                # presenting the two native stores as separate calendars.
                case "Planning" | "Calendar" | "Tasks" | "Reminders":
                    self._restore_planning_anchor(planning_anchor_unix_ms)
                    await self._show_planning()
                case "Navigation":
                    await self._show_navigation()
                case "QuickNotes":
                    await self._show_notes()
                case "Activity":
                    await self._show_activity()
                # The panel tabs are pushed, not drawn on request: a voice
                # open publishes the tab with `activate` set, and the plugin
                # switches to and expands it from that push.
                case "News" | "Pomodoro" | "Search" if self._panels is not None:
                    await self._panels.activate(name.lower())
                case _:
                    log.info("unknown capability requested: %s", name)
        finally:
            self._request_context.reset(token)

    async def _show_brainstorm(self) -> None:
        """Open Brainstorm on its LAST session, or on an invitation.

        This used to refuse unless the Hermes terminal was ready and to
        seat Hermes beside Codex and Claude. Both contradict the boundary:
        Brainstorm's collaborators are the Codex and Claude CLIs, and
        Hermes/OpenClaw are Agent Terminal workers that neither participate
        nor gate. A wearer opening this row was told to go fix a daemon
        their conversation never speaks to.

        When a session has run, the row shows what was actually said —
        the snapshot exists precisely so the wearer can re-read the
        synthesis they walked away with.

        And when one is RUNNING, opening this row returns the wearer to it.
        docs/SESSION_MODEL.md:46 says so in one line — "open Brainstorm
        during an active run → restore the current conversation" — and the
        opposite shipped: any non-empty transcript painted state="settled",
        which renders DONE on the glass and makes the tap a no-op. A wearer
        who opened the menu mid-session was told their live conversation had
        finished, and had no way back into it.
        """
        # The live session's own sink is the authority for its frame, so
        # ask it to repaint rather than rebuilding one from the snapshot:
        # a reconstruction would drop the streaming turn in progress.
        if self._brainstorm_replay is not None and await self._brainstorm_replay():
            return
        # IDLE: the picker. docs/SESSION_MODEL.md:47 — "open Brainstorm while
        # idle → show recent sessions plus NEW."
        #
        # This was four lines of dead text inside an activity string: the
        # wearer could SEE that past sessions existed and could not open one,
        # because nothing carried an id to select them by. The record does
        # now, so these are real rows on the same selectable surface every
        # other data view uses — no new frame, no new gesture to learn.
        history = self._brainstorm_sessions() if self._brainstorm_sessions else []
        if history:
            # The picker, WHENEVER idle — docs/SESSION_MODEL.md:47 has no
            # other condition. This was gated on the in-process recap
            # having zero rows, so the moment any session closed in this
            # process the idle view became that session's dead text and
            # the picker was unreachable until a restart. The last session
            # is not lost: it is the top row, and tapping it reopens the
            # real conversation instead of a snapshot.
            self._brainstorm_rows = list(history)
            await self._show_focus(
                "BRAINSTORM",
                [_NEW_BRAINSTORM_ROW, *(_session_row(p) for p in history)],
                hint="tap: open · say “JOAT, brainstorm …” to start",
                selectable=True,
            )
            return
        recap = self._brainstorm_recap() if self._brainstorm_recap else None
        rows = recap.rows() if recap is not None else []
        if not rows and self._brainstorm_recent is not None:
            past = self._brainstorm_recent(4)
            if past:
                rows = ["Recent:", *past]
        await self._hud.show_brainstorm(BrainstormHudView(
            state="settled" if rows else "ready",
            topic=(recap.topic[:40] if recap is not None and recap.topic
                   else "Ready for a question"),
            collaborators=list(recap.speakers[:3]) if recap is not None and recap.speakers
            else ["Codex", "Claude"],
            activity=("\n".join(rows) if rows
                      else 'Say “JOAT, brainstorm …” to begin.'),
            acceptance_armed=self._acceptance_armed,
        ))

    async def _show_agent_terminal(self) -> None:
        """Open the terminal on the wearer's actual work sessions.

        The roster paints the real thing — which sessions exist, which is
        focused, what each last said. Only when no roster is configured
        does this fall back to the readiness/recovery card, which is all
        this surface used to show.
        """
        # Sessions you can SEE and tap, not a phrase to memorise. The idle
        # terminal said: Say "terminal sessions" to choose a work session —
        # and the wearer's response was "why do I have to say something to
        # choose a work session? WHAT DOES THAT EVEN MEAN?" Fair: a chooser
        # that requires you to already know the words is not a chooser.
        #
        # Only when nothing is running. A lane with live output owns the
        # lenses — the wearer opened the terminal to watch work, and a
        # picker over the top of it would hide the thing they came for.
        rows = self._terminal_rows() if self._terminal_rows else []
        working = self._terminal_busy() if self._terminal_busy else False
        if rows and not working:
            self._terminal_row_names = [name for name, _row in rows]
            await self._show_focus(
                "AGENT TERMINAL",
                [row for _name, row in rows],
                # Short enough to RENDER. The longer form
                # ("terminal <instruction>") was cut mid-word on the lens —
                # a hint that does not fit teaches nothing.
                hint="tap: open · say “terminal …” to send",
                selectable=True,
            )
            return
        if self._terminal_open is not None and self._terminal_current is not None:
            current = self._terminal_current()
            if current is not None:
                await self._open_terminal_lane(current, self._active_request_id or "hud")
                return
        if self._terminal_view is not None:
            await self._terminal_view()
            return
        terminal = self._status.agent_terminal_projection()
        state = str(terminal["state"])
        reason = str(terminal["reason"])
        await self._hud.show_agent_terminal(AgentTerminalHudView(
            state=state,
            # The lane name, in the same shape the roster sends
            # ("<lane> · <state>"): the renderer reads the first segment as
            # the summary spine's focus label. "Hermes Agent Terminal" put
            # HERMES AGENT TERMINAL > UNAVAILABLE under a screen already
            # titled Agent Terminal.
            focus="hermes",
            lanes=[],
            activity="No terminal sessions are configured on this Mac.",
            endpoint_host=str(terminal["endpoint_host"]),
            reason=_agent_terminal_instruction(state, reason),
            no_tools_checked=bool(terminal["no_tools_checked"]),
        ))

    async def _show_messages(self) -> None:
        # A new Messages visit is a new consent context. Never leave a voice
        # reply pointing at the conversation from the preceding visit while
        # the wearer sees the fresh picker (including access/retry states).
        self.clear_message_selection("messages-open")
        if self._messages is None or not self._messages.available:
            if self._request_is_current():
                self._message_rows = []
            await self._show_focus(
                "MESSAGES · SETUP",
                MESSAGES_ACCESS_ITEMS,
            )
            return
        try:
            rows = await asyncio.to_thread(self._messages.recent_threads, 5)
        except PermissionError as exc:
            log.warning("Messages capability unavailable: %s", type(exc).__name__)
            if self._request_is_current():
                self._message_rows = []
            await self._show_messages_read_failure(exc)
            return
        except (OSError, sqlite3.Error) as exc:
            log.warning("Messages capability temporarily unavailable: %s", type(exc).__name__)
            if self._request_is_current():
                self._message_rows = []
            await self._show_messages_read_failure(exc)
            return
        if not self._request_is_current():
            return
        self._message_rows = rows
        items = [NEW_MESSAGE_ROW]
        items.extend(self._message_list_row(row) for row in rows)
        await self._send_list(HudList(
            items=items,
            title="MESSAGES",
            hint="tap: select · left x2: back",
            selectable=True,
        ))

    async def _select_terminal(self, index: int, trace_id: str = "hud") -> None:
        """Focus the work session the wearer tapped, and show it."""
        names = self._terminal_row_names
        if not 0 <= index < len(names) or (self._terminal_focus is None and self._terminal_open is None):
            # A session can finish between the paint and the tap. Saying so
            # is the difference between "the list moved" and "the glasses
            # ignored me".
            log.info("terminal row %d is no longer on screen", index)
            await self._hud.assistant(
                "That session is no longer listed — open the terminal again.",
                answers_wearer=True,
            )
            return
        if self._terminal_open is not None:
            await self._open_terminal_lane(names[index], trace_id)
        elif self._terminal_focus is not None:
            await self._terminal_focus(names[index])

    def claim_terminal_voice(
        self, request_id: str | None = None, selection_id: str | None = None,
    ) -> Callable[[], bool]:
        """A recognized spoken Terminal command starts a host-owned visit."""
        self._terminal_opening_revision += 1
        self._active_request_id = request_id
        self._active_capability = "AGENT TERMINAL"
        self._terminal_selection_id = selection_id
        revision = self._terminal_opening_revision
        return lambda: revision == self._terminal_opening_revision

    @property
    def view_revision(self) -> int:
        """Navigation changes retire assistant overlays from the preceding view."""
        return self._terminal_opening_revision

    @property
    def brainstorm_view_current(self) -> bool:
        """A running discussion is not necessarily the wearer's current view."""
        return self._active_capability == "BRAINSTORM" and not self._contextual_menu_open

    @property
    def terminal_view_current(self) -> bool:
        return self._active_capability == "AGENT TERMINAL" and not self._contextual_menu_open

    def claim_brainstorm_view(self) -> None:
        """Only an explicit start or return may reclaim the discussion view."""
        self._terminal_opening_revision += 1
        if self._active_capability != "BRAINSTORM":
            self.clear_message_selection("brainstorm-open")
            self._note_reader = None
            self._active_request_id = None
        self._active_capability = "BRAINSTORM"

    async def open_terminal_from_brainstorm(self) -> bool:
        """Use the same picker and lane preparation as an ordinary Terminal open."""
        if not self.brainstorm_view_current:
            return False
        request_id = voice_request_id()
        await self.open("Agent Terminal", request_id=request_id)
        return self.terminal_view_current and self._active_request_id == request_id

    async def prepare_terminal_instruction(self, name: str, request_id: str) -> bool:
        """Open one explicitly named lane through the normal owned visit."""
        names = [lane for lane, _row in self._terminal_rows()] if self._terminal_rows else []
        if (name not in names or self._terminal_open is None
                or not request_id.startswith(VOICE_REQUEST_PREFIX)):
            return False
        self.clear_message_selection("terminal-open")
        self._note_reader = None
        current = self.claim_terminal_voice(request_id, request_id)
        token = self._request_context.set(request_id)
        try:
            # The existing voice list establishes the same request/selection
            # binding as a picker tap, before the first prepared lane frame.
            await self._show_focus(
                "AGENT TERMINAL", [f"Opening {name}…"], hint=BACK_HINT,
            )
            if not current():
                return False
            return await self._open_terminal_lane(name, request_id)
        finally:
            self._request_context.reset(token)

    async def _open_terminal_lane(self, name: str, selection_id: str) -> bool:
        self._terminal_opening_revision += 1
        revision = self._terminal_opening_revision
        request_id = self._active_request_id
        self._terminal_selection_id = selection_id

        def current() -> bool:
            return (
                revision == self._terminal_opening_revision
                and request_id == self._active_request_id
                and self._active_capability == "AGENT TERMINAL"
            )

        if self._terminal_open is not None and current():
            try:
                await self._terminal_open(name, request_id, selection_id, current)
                return current()
            except Exception:
                log.exception("terminal opening failed")
                if current():
                    await self._hud.assistant("That session did not open. Tap retry.", answers_wearer=True)
        return False

    async def _select_brainstorm(self, index: int) -> None:
        """Open the session the wearer picked.

        Row 0 is NEW. The rest resolve through the rows this surface last
        DREW rather than through the store, because a session can finish
        between the paint and the tap — indexing the store again would open
        whatever had shifted into that position.
        """
        if index == 0:
            await self._hud.show_brainstorm(BrainstormHudView(
                state="ready",
                topic="Ready for a question",
                collaborators=["Codex", "Claude"],
                activity='Say “JOAT, brainstorm …” to begin.',
                acceptance_armed=self._acceptance_armed,
            ))
            return
        rows = self._brainstorm_rows
        picked = index - 1
        if not 0 <= picked < len(rows) or self._brainstorm_continue is None:
            log.info("brainstorm row %d is no longer on screen", index)
            await self._hud.assistant(
                "That session is no longer listed — open Brainstorm again.",
                answers_wearer=True,
            )
            return
        if not await self._brainstorm_continue(rows[picked].session_id):
            await self._show_focus(
                "BRAINSTORM",
                ["That session could not be reopened.",
                 "Say “JOAT, brainstorm …” to start a new one."],
            )

    async def _select_message(self, index: int, trace_id: str, selection_epoch: int | None = None) -> None:
        if (
            self._messages is None
            or not 0 <= index <= len(self._message_rows)
        ):
            return
        if selection_epoch is None:
            self._message_selection_epoch += 1
            selection_epoch = self._message_selection_epoch
        if selection_epoch != self._message_selection_epoch:
            return
        if index == 0:
            if self._replies is None:
                return
            if self._message_ui is not None:
                self._message_selection_active = True
                await self._message_ui.begin_new()
                return
            self._replies.begin_compose(trace_id)
            self._message_selection_active = True
            await self._send_list(HudList(
                items=[
                    "Say the recipient name first.",
                    "Then say the message.",
                    "One matching Mac contact is required.",
                ],
                title="MESSAGES · COMPOSE",
                hint="speak: recipient · left x2: back",
            ))
            return
        thread = self._message_rows[index - 1]
        try:
            page = await asyncio.to_thread(
                # 8 entries = exactly one conversation window on the lens
                # now that the page name moved into the status spine, so a
                # page turn is a screen turn — nothing half-hidden.
                self._messages.thread_message_page, thread, limit=8,
            )
        except (OSError, sqlite3.Error) as exc:
            log.warning("Messages history unavailable: %s", type(exc).__name__)
            if (
                not self._request_is_current()
                or selection_epoch != self._message_selection_epoch
            ):
                return
            self.clear_message_selection("access-denied")
            await self._show_messages_read_failure(exc)
            return
        if (
            not self._request_is_current()
            or selection_epoch != self._message_selection_epoch
        ):
            return
        # The Mac-owned chat GUID targets the whole conversation. A group
        # without that identity must never fall back to its latest sender.
        can_reply = thread.reply_target is not None and self._replies is not None
        if can_reply and self._replies is not None:
            self._replies.select(thread, trace_id)
        self._message_selection_active = can_reply
        self._message_history = _MessageHistoryState(thread=thread, pages=[page])
        self._message_detail = None
        await self._show_message_history_page()
        if (can_reply and self._message_ui is not None and selection_epoch == self._message_selection_epoch
                and self._request_is_current()):
            self._message_ui.conversation_ready()

    async def _select_message_detail(
        self,
        index: int,
        trace_id: str,
        request_id: str | None,
    ) -> None:
        """Open one transcript entry without changing the reply target.

        The Hub transmits only the rendered row index. The selected thread and
        entry remain Mac-private, so a forged G2 request cannot redirect a
        reply or make another conversation readable.
        """

        if (
            request_id != self._active_request_id
            or self._active_capability != "MESSAGES"
            or self._message_history is None
        ):
            log.info("dropping stale Messages detail request=%s", request_id)
            return
        history = self._message_history
        page = history.pages[history.index]
        token = self._request_context.set(request_id)
        try:
            # Row zero is the page heading; speaker rows follow it. The final
            # voice instructions are intentionally not message actions.
            if not 1 <= index <= len(page.entries):
                await self._show_message_history_page()
                return
            entry = page.entries[index - 1]
            self._message_detail = _MessageDetailState(
                thread=history.thread,
                entry=entry,
                pages=paginate_reader_text(entry.text or "No text is available for this message."),
            )
            log.info("message detail trace=%s stage=page-visible page=0", trace_id)
            await self._show_message_detail()
        finally:
            self._request_context.reset(token)

    async def _page_message_history(
        self,
        event_type: int,
        trace_id: str,
        request_id: str | None,
    ) -> None:
        if (
            request_id != self._active_request_id
            or self._active_capability != "MESSAGES"
            or self._messages is None
            or self._message_history is None
        ):
            log.info("dropping stale Messages page request=%s", request_id)
            return
        if self._message_detail is not None:
            await self._page_message_detail(event_type, trace_id, request_id)
            return
        state = self._message_history
        next_index = state.index
        if event_type == EventType.SCROLL_TOP:
            if state.index == 0:
                return
            next_index -= 1
        elif event_type == EventType.SCROLL_BOTTOM:
            if state.index + 1 < len(state.pages):
                next_index += 1
            else:
                current = state.pages[state.index]
                if not current.has_older:
                    return
                row_ids = [entry.row_id for entry in current.entries if entry.row_id > 0]
                if not row_ids:
                    return
                try:
                    older = await asyncio.to_thread(
                        self._messages.thread_message_page,
                        state.thread,
                        limit=8,
                        before_row_id=min(row_ids),
                    )
                except (OSError, sqlite3.Error) as exc:
                    log.warning("Messages history unavailable: %s", type(exc).__name__)
                    if (
                        request_id != self._active_request_id
                        or self._message_history is not state
                    ):
                        return
                    self.clear_message_selection("access-denied")
                    token = self._request_context.set(request_id)
                    try:
                        await self._show_messages_read_failure(exc)
                    finally:
                        self._request_context.reset(token)
                    return
                if not older.entries:
                    return
                state.pages.append(older)
                next_index += 1
        else:
            return
        if request_id != self._active_request_id or self._message_history is not state:
            return
        state.index = next_index
        log.info(
            "message history trace=%s stage=page-visible page=%d",
            trace_id,
            state.index,
        )
        token = self._request_context.set(request_id)
        try:
            await self._show_message_history_page()
        finally:
            self._request_context.reset(token)

    async def _page_message_detail(
        self,
        event_type: int,
        trace_id: str,
        request_id: str | None,
    ) -> None:
        state = self._message_detail
        if state is None:
            return
        next_index = state.index
        if event_type == EventType.SCROLL_TOP:
            if state.index == 0:
                return
            next_index -= 1
        elif event_type == EventType.SCROLL_BOTTOM:
            if state.index + 1 >= len(state.pages):
                return
            next_index += 1
        else:
            return
        if request_id != self._active_request_id or self._message_detail is not state:
            return
        state.index = next_index
        log.info("message detail trace=%s stage=page-visible page=%d", trace_id, state.index)
        token = self._request_context.set(request_id)
        try:
            await self._show_message_detail()
        finally:
            self._request_context.reset(token)

    async def _show_message_history_page(self) -> None:
        state = self._message_history
        if state is None:
            return
        page = state.pages[state.index]
        if state.index == 0:
            page_label = (
                "Latest messages — down for older"
                if page.has_older else "Complete conversation on this Mac"
            )
        else:
            page_label = (
                "Older messages — up newer, down older"
                if page.has_older else "Older messages — up for newer"
            )
        # The Hub's boundary gesture gate consumes these paging affordances.
        # Keep the wire hint and visible actions aligned with the cursor.
        hint_parts = []
        if state.index > 0:
            hint_parts.append("up newer")
        if page.has_older:
            hint_parts.append("down older")
        can_reply = state.thread.reply_target is not None and self._replies is not None
        hint = " · ".join([
            *hint_parts, "tap read", "say reply" if can_reply else "read-only", BACK_HINT,
        ])
        context = [self._message_history_row(state.thread, entry) for entry in page.entries]
        instructions = (
            ['Say “JOAT, reply saying …”', "Mic off? Long press to enable."]
            if can_reply else []
        )
        await self._send_list(HudList(
            items=[page_label, *context, *instructions],
            title="MESSAGES · CONVERSATION",
            hint=hint,
            selectable=bool(page.entries),
        ))

    async def _show_message_detail(self) -> None:
        state = self._message_detail
        if state is None:
            return
        total = len(state.pages)
        hint_parts = []
        if state.index > 0:
            hint_parts.append("up previous")
        if state.index + 1 < total:
            hint_parts.append("down more")
        can_reply = state.thread.reply_target is not None and self._replies is not None
        hint = " · ".join([
            *hint_parts, "say reply" if can_reply else "read-only", BACK_HINT,
        ])
        await self._send_list(HudList(
            items=[
                self._message_detail_metadata(state.thread, state.entry, state.index, total),
                *state.pages[state.index],
            ],
            title="MESSAGES · MESSAGE",
            hint=hint,
        ))

    @staticmethod
    def _message_detail_metadata(
        thread: MessageThread,
        entry: MessageEntry,
        page_index: int,
        page_count: int,
    ) -> str:
        role = "To" if entry.from_me else "From"
        sender = "you" if entry.from_me else CapabilityRouter._message_sender(thread, entry)
        details: list[str] = []
        if entry.unread:
            details.append("unread")
        if entry.delivery is not None:
            details.append(entry.delivery)
        if entry.attachment_count:
            details.append("attachment")
        metadata = " · ".join([f"{page_index + 1}/{page_count}", f"{role} {sender}", *details])
        # Counter first identifies prepared rows even when a long sender
        # fills the metadata budget. Never split an astral code point.
        units = 0
        for index, char in enumerate(metadata):
            units += 2 if ord(char) > 0xFFFF else 1
            if units > 64:
                return metadata[:index]
        return metadata

    async def _show_messages_read_failure(self, error: BaseException) -> None:
        """Keep a real permission denial distinct from a retryable DB blip.

        iMessage's live database can briefly reject a read while it is being
        rotated. That is not evidence that Full Disk Access is absent, and the
        recovery card must be an actionable retry rather than stale setup
        instructions. No exception text or Message content reaches the HUD.
        """
        if isinstance(error, PermissionError):
            await self._show_focus(
                "MESSAGES · ACCESS",
                MESSAGES_ACCESS_ITEMS,
                selectable=True,
            )
            return
        await self._show_focus(
            "MESSAGES · RETRY",
            MESSAGES_RETRY_ITEMS,
            selectable=True,
        )

    @staticmethod
    def _message_list_row(thread: MessageThread) -> str:
        """Keep the conversation picker distinct from the notification card.

        A blank preview is an attachment-only message, not an absent message;
        surface that and unread state without exposing attachment names or
        filesystem paths to the glasses.
        """
        # ● marks an unread thread — the one scannable state in the picker —
        # instead of a wordy "(unread)" that reads like body text.
        marker = "● " if thread.unread else ""
        label = f"[{_age(thread.at)}] {marker}{thread.sender}"
        if thread.is_group or not thread.replyable:
            # No " · " inside the label: that token is the primary/secondary
            # split on the lens. Group identity is independent of reply support.
            label += " (group)"
        preview = thread.text
        if thread.attachment_count:
            attachment = (
                "Attachment" if thread.attachment_count == 1
                else f"{thread.attachment_count} attachments"
            )
            preview = f"{preview} · {attachment}" if preview else attachment
        return f"{label} · {preview or 'Message unavailable'}"

    @staticmethod
    def _message_sender(thread: MessageThread, entry: MessageEntry) -> str:
        return entry.sender or (
            "Participant" if thread.is_group or not thread.replyable else thread.sender
        )

    @staticmethod
    def _message_history_row(thread: MessageThread, entry: MessageEntry) -> str:
        label = "You" if entry.from_me else CapabilityRouter._message_sender(thread, entry)
        details: list[str] = []
        if entry.unread:
            details.append("Unread")
        if entry.delivery is not None:
            details.append(entry.delivery.title())
        if entry.attachment_count:
            details.append(
                "Attachment" if entry.attachment_count == 1
                else f"{entry.attachment_count} attachments"
            )
        if entry.text:
            details.append(entry.text)
        return f"{label} · {' · '.join(details) or 'Message unavailable'}"

    def _clear_message_page_pending(self) -> None:
        self._message_page_pending = False

    async def _select_capability_row(
        self,
        name: str,
        index: int,
        trace_id: str,
        request_id: str | None,
        *,
        terminal_revision: int | None = None,
        message_epoch: int | None = None,
    ) -> None:
        if message_epoch is not None and message_epoch != self._message_selection_epoch:
            return
        if terminal_revision is not None and terminal_revision != self._terminal_opening_revision:
            return
        if self._panels is not None and self._panels.owns(name):
            # A panel row tap is validated against the tab's content
            # revision, not this router's request generation.
            await self._panels.select(name, index, request_id)
            return
        if (
            request_id != self._active_request_id
            or self._canonical_capability(name) != self._active_capability
        ):
            log.info("dropping stale capability selection request=%s", request_id)
            return
        token = self._request_context.set(request_id)
        try:
            match name:
                case "BRAINSTORM":
                    await self._select_brainstorm(index)
                case "AGENT TERMINAL":
                    await self._select_terminal(index, trace_id)
                case "MESSAGES":
                    await self._select_message(index, trace_id, message_epoch)
                case "NOTIFICATIONS":
                    await self._select_notification(index)
                # ``Reminders`` was a first-level name in an older installed
                # HUD.  ``open`` already maps it to the single Planning
                # projection, so its row taps must use that same handler.
                # Otherwise the rendered legacy page looks live but is inert.
                case "PLANNING" | "CALENDAR" | "TASKS" | "REMINDERS":
                    await self._select_planning(index)
                case "QUICKNOTES":
                    await self._select_note(index, trace_id)
                case "ACTIVITY":
                    await self._select_activity(index)
        except Exception:
            # One guard for all eight selections. There was a `try` with
            # only a `finally`, so a handler that raised — a lane that
            # vanished between the paint and the tap, a store that could
            # not be read — became an unhandled task exception: the tap did
            # nothing, said nothing, and the trace went somewhere the
            # wearer will never look. A gesture that fails must say so on
            # the surface it was made on.
            log.exception("capability selection failed name=%s index=%d", name, index)
            with contextlib.suppress(Exception):
                await self._hud.assistant(
                    "That did not open — try it again.", answers_wearer=True,
                )
        finally:
            self._request_context.reset(token)

    async def _show_notifications(self) -> None:
        entries = self._cards.recent(5)
        if not self._request_is_current():
            return
        self._notification_rows = entries
        if not entries:
            await self._show_focus("NOTIFICATIONS · EMPTY", ["Nothing shown recently."])
            return
        await self._send_list(HudList(
            items=[f"[{_age(c.at)}] {c.render().splitlines()[0]}" for c in entries],
            title="NOTIFICATIONS",
            hint=DETAIL_HINT,
            selectable=True,
        ))

    async def _select_notification(self, index: int) -> None:
        if not 0 <= index < len(self._notification_rows):
            return
        card = self._notification_rows[index]
        await self._show_focus(
            "NOTIFICATIONS · DETAIL",
            card.render().splitlines() or ["Notification content unavailable."],
        )

    async def _show_planning(self) -> None:
        """Show the one calendar-shaped view of native events and reminders.

        This does not write, infer missing records, or treat Calendar.app's
        Scheduled Reminders projection as a second event source.  The initial
        list, each scope change, and every day drill-down fetches the bounded
        authoritative EventKit range again so a native edit is never hidden by
        a stale in-memory agenda.
        """

        client = self._personal_data
        origin = self._planning_origin()
        if client is None:
            if not self._request_is_current():
                return
            self._planning_rows = []
            self._status.set_personal_data(state="not_configured")
            await self._show_focus(
                f"{origin} · PLAN · SETUP",
                ["Start JOAT from the Mac app to use Calendar and Reminders."],
            )
            return
        try:
            projection = await load_native_planning(
                client,
                selected_day=self._planning_selected_day,
                scope=self._planning_scope,
                zone=self._planning_zone(),
                all_day_zone=self._planning_home_zone(),
            )
        except PlanningAuthorizationError:
            if not self._request_is_current():
                return
            self._planning_rows = []
            self._status.set_personal_data(state="authorization_required")
            await self._show_focus(
                f"{origin} · PLAN · SETUP",
                ["Calendar and Reminders full access is required."],
            )
            return
        except NativeBridgeUnavailable:
            if not self._request_is_current():
                return
            self._planning_rows = []
            self._status.set_personal_data(state="unavailable")
            await self._show_focus(
                f"{origin} · PLAN · UNAVAILABLE",
                ["Calendar and Reminders bridge is unavailable. Restart JOAT on the Mac."],
            )
            return
        except NativeBridgeProtocolError:
            if not self._request_is_current():
                return
            self._planning_rows = []
            self._status.set_personal_data(state="protocol_error")
            await self._show_focus(
                f"{origin} · PLAN · UNAVAILABLE",
                ["Calendar and Reminders bridge needs a JOAT update on the Mac."],
            )
            return
        except NativeBridgeError:
            if not self._request_is_current():
                return
            self._planning_rows = []
            self._status.set_personal_data(state="unavailable")
            await self._show_focus(
                f"{origin} · PLAN · UNAVAILABLE",
                ["Calendar and Reminders bridge is unavailable. Restart JOAT on the Mac."],
            )
            return

        if not self._request_is_current():
            return
        # A successful authoritative Planning read is stronger than the
        # startup probe. It carries no planning content into the status file.
        self._status.set_personal_data(state="ready")
        self._planning_rows = self._planning_rows_for(projection)
        await self._show_focus(
            f"{origin} · {projection.title}",
            [row.label for row in self._planning_rows],
            hint=PLANNING_HINT,
            selectable=True,
            planning_grid=self._planning_grid_for(projection),
        )

    @staticmethod
    def _planning_grid_for(
        projection: PlanningProjection,
    ) -> tuple[PlanningGridCell, ...] | None:
        """Publish only native per-day totals for the one Month renderer.

        Names, native IDs, containers, and times remain on the next tap's
        Week/Day agenda. The Month needs just enough truthful information to
        mark a day as containing Events and/or Reminders rather than looking
        silently free.
        """

        if projection.scope is not PlanningScope.MONTH or len(projection.days) != 42:
            return None
        return tuple(
            PlanningGridCell(
                day=day.day,
                event_count=day.event_count,
                reminder_count=day.reminder_count,
                in_month=day.day.month == projection.selected_day.month,
                # The dashboard's right pane repaints from these on hover;
                # a per-hover Mac request would ride the serialized BLE lane.
                entries=tuple(
                    PlanningGridEntry(
                        time=entry.time_label,
                        kind=entry.kind,
                        title=entry.title,
                    )
                    for entry in day.entries[:6]
                ),
            )
            for day in projection.days
        )

    def _planning_rows_for(self, projection: PlanningProjection) -> list[_PlanningRow]:
        """Encode scope, time travel, and drill-down into ordinary G2 rows.

        The existing transparent capture layer reports a selected list row to
        the Mac.  Keeping the action identity here makes that click a real
        navigation command, rather than asking the HUD to parse visible text.
        """

        scope_rows = [
            _PlanningRow(
                label=f"{'●' if projection.scope is scope else '○'} VIEW · {scope.value.upper()}",
                action="scope",
                scope=scope,
            )
            for scope in (PlanningScope.MONTH, PlanningScope.WEEK, PlanningScope.DAY)
        ]
        if projection.scope is PlanningScope.MONTH:
            previous = advance_selected_day(projection.selected_day, projection.scope, -1)
            following = advance_selected_day(projection.selected_day, projection.scope, 1)
            rows: list[_PlanningRow] = []
            for first in range(0, len(projection.days), 7):
                week = projection.days[first:first + 7]
                dated = [day for day in week if day.day.month == projection.selected_day.month]
                item_count = sum(len(day.entries) for day in dated)
                rows.append(_PlanningRow(
                    label=(
                        f"{week[0].day.strftime('%b').upper()} {week[0].day.day}–"
                        f"{week[-1].day.strftime('%b').upper()} {week[-1].day.day}"
                        f" · {item_count} planned"
                    ),
                    action="week",
                    day=week[0].day,
                ))
            rows.extend([
                _PlanningRow(
                    label=f"‹ {previous.strftime('%B').upper()} {previous.year}",
                    action="previous",
                ),
                _PlanningRow(
                    label=f"{following.strftime('%B').upper()} {following.year} ›",
                    action="next",
                ),
                *scope_rows,
            ])
            return rows

        if projection.scope is PlanningScope.WEEK:
            previous = advance_selected_day(projection.selected_day, projection.scope, -1)
            following = advance_selected_day(projection.selected_day, projection.scope, 1)
            rows = []
            for day in projection.days:
                events = day.event_count
                reminders = day.reminder_count
                pieces = []
                if events:
                    pieces.append(f"{events} event{'s' if events != 1 else ''}")
                if reminders:
                    pieces.append(f"{reminders} reminder{'s' if reminders != 1 else ''}")
                summary = " · ".join(pieces) if pieces else "free"
                rows.append(_PlanningRow(
                    label=f"{day.day.strftime('%a').upper()} {day.day.strftime('%b').upper()} {day.day.day} · {summary}",
                    action="day",
                    day=day.day,
                ))
            rows.extend([
                _PlanningRow(
                    label=f"‹ {previous.strftime('%b').upper()} {previous.day}",
                    action="previous",
                ),
                _PlanningRow(
                    label=f"{following.strftime('%b').upper()} {following.day} ›",
                    action="next",
                ),
                *scope_rows,
            ])
            return rows

        previous = advance_selected_day(projection.selected_day, projection.scope, -1)
        following = advance_selected_day(projection.selected_day, projection.scope, 1)
        rows = []
        selected = projection.days[0]
        if not selected.entries:
            rows.append(_PlanningRow(label="Nothing planned", action="day", day=selected.day))
        for entry in selected.entries:
            # Planning merges the two halves — calendar events and
            # reminders/tasks — so a row must say WHICH it is. DESIGN.md:
            # kind is a tiny aligned marker from the firmware-safe glyph set,
            # "never by an icon badge or a spelled-out word". Spelling out
            # "EVENT"/"REMINDER" cost ten characters of a 46-character line
            # on every single row. The bracketed time reads as the same quiet
            # leading chip QuickNotes uses, then the marker, then the title.
            # A reminder is an open todo, which is exactly what □ means in
            # that vocabulary. An event gets no marker rather than borrow ●,
            # which means "demanding attention now" — the renderer reserves
            # the marker column for the whole list, so titles still align.
            marker = "□ " if entry.kind == "reminder" else ""
            rows.append(_PlanningRow(
                label=f"[{entry.time_label}] {marker}{entry.title}",
                action="entry",
                entry=entry,
            ))
        rows.extend([
            _PlanningRow(
                label=f"‹ {previous.strftime('%a').upper()} {previous.strftime('%b').upper()} {previous.day}",
                action="previous",
            ),
            _PlanningRow(
                label=f"{following.strftime('%a').upper()} {following.strftime('%b').upper()} {following.day} ›",
                action="next",
            ),
            *scope_rows,
        ])
        return rows

    async def _select_planning(self, index: int) -> None:
        if not 0 <= index < len(self._planning_rows):
            return
        row = self._planning_rows[index]
        if row.action == "scope" and row.scope is not None:
            self._planning_scope = row.scope
            await self._show_planning()
            return
        if row.action == "previous":
            self._planning_selected_day = advance_selected_day(
                self._planning_selected_day, self._planning_scope, -1
            )
            await self._show_planning()
            return
        if row.action == "next":
            self._planning_selected_day = advance_selected_day(
                self._planning_selected_day, self._planning_scope, 1
            )
            await self._show_planning()
            return
        if row.action == "week" and row.day is not None:
            self._planning_selected_day = row.day
            self._planning_scope = PlanningScope.WEEK
            await self._show_planning()
            return
        if row.action == "day" and row.day is not None:
            self._planning_selected_day = row.day
            self._planning_scope = PlanningScope.DAY
            await self._show_planning()
            return
        if row.action == "entry" and row.entry is not None:
            entry = row.entry
            await self._show_focus(
                f"{self._planning_origin()} · {entry.kind.upper()}",
                [
                    entry.title,
                    f"{entry.time_label} · {entry.kind.title()}",
                    "Read-only native item · left x2 Planning",
                ],
                hint="left x2 Planning",
            )

    async def _select_grid_entry(
        self,
        anchor_unix_ms: int,
        entry_index: int,
        request_id: str | None,
    ) -> None:
        """Open one entry tapped on the dashboard's right pane.

        The wire carries only the date and the item's position. The entry is
        re-resolved from a fresh authoritative DAY projection — the same
        never-stale rule every Planning read follows — so a native edit
        between the month paint and the tap opens current truth or nothing.
        """
        client = self._personal_data
        if client is None or not (0 <= anchor_unix_ms <= 32_503_680_000_000):
            return
        selected_day = datetime.fromtimestamp(
            anchor_unix_ms / 1_000, tz=timezone.utc
        ).astimezone(self._planning_zone()).date()
        token = self._request_context.set(request_id)
        try:
            try:
                projection = await load_native_planning(
                    client,
                    selected_day=selected_day,
                    scope=PlanningScope.DAY,
                    zone=self._planning_zone(),
                    all_day_zone=self._planning_home_zone(),
                )
            except (PlanningAuthorizationError, NativeBridgeError):
                # The month view stays on screen; a failed drill-down must
                # not replace it with an error page for a tap.
                log.info("dashboard entry open failed; leaving the month view")
                return
            if not self._request_is_current():
                return
            entries = projection.days[0].entries if projection.days else ()
            if not 0 <= entry_index < len(entries):
                log.info("dashboard entry index stale; leaving the month view")
                return
            # Anchor follow-up Back navigation on the tapped day.
            self._planning_selected_day = selected_day
            entry = entries[entry_index]
            await self._show_focus(
                f"{self._planning_origin()} · {entry.kind.upper()}",
                [
                    entry.title,
                    f"{entry.time_label} · {entry.kind.title()}",
                    "Read-only native item · left x2 Planning",
                ],
                hint="left x2 Planning",
            )
        finally:
            self._request_context.reset(token)

    def _planning_origin(self) -> str:
        """Name the current menu contract while accepting old package aliases."""

        if self._active_capability == "TASKS":
            return "TASKS"
        if self._active_capability == "CALENDAR":
            return "CALENDAR"
        return "PLANNING"

    def _restore_planning_anchor(self, anchor_unix_ms: int | None) -> None:
        """Return a safe-write outcome to its actual day in the local agenda.

        The anchor is an absolute instant, so a Calendar proposal created in
        another IANA zone lands on the right civil day for this Mac's native
        EventKit/Reminders read model. Invalid direct callers keep the normal
        Month view rather than manufacturing a date.
        """

        if (
            isinstance(anchor_unix_ms, bool)
            or not isinstance(anchor_unix_ms, int)
            or not 0 <= anchor_unix_ms <= 32_503_680_000_000
        ):
            return
        try:
            selected = datetime.fromtimestamp(
                anchor_unix_ms / 1_000,
                tz=self._planning_zone(),
            ).date()
        except (OverflowError, OSError, ValueError):
            return
        self._planning_selected_day = selected
        self._planning_scope = PlanningScope.DAY

    def _planning_zone(self) -> tzinfo:
        """Where the WEARER is — the phone's zone, else this Mac's.

        Formerly the Mac's zone unconditionally, which was never a decision:
        it is what Python hands you when nothing says otherwise. The HUD's
        own banner clock is the phone's, so a Mac-formatted event time was
        unreadable against it the moment the two diverged.
        """

        return self._wearer_zone.current()

    def _planning_home_zone(self) -> tzinfo:
        """Where the calendar's all-day events were authored — this Mac."""

        return self._wearer_zone.home()

    async def _show_notes(self) -> None:
        """One QuickNotes surface: live recording controls, else all notes.

        Recordings (transcribed + summarized meetings/speeches) and exact
        dictated text notes are one wearer concept; the storage split stays
        invisible. While a capture is live the page IS the recorder's control
        panel — the exclusive microphone means taps are the only controls.
        """
        self._note_reader = None
        capture = self._voice_capture
        if capture is not None and capture.active:
            await self._show_recording_controls()
            return
        if self._notes is None and self._voice_notes is None:
            if self._request_is_current():
                self._note_rows = []
            await self._show_focus(
                "QUICKNOTES · SETUP",
                ['Say “JOAT, note …” to save exact text.'],
            )
            return
        rows: list[_NoteRow] = []
        if self._voice_notes is not None:
            for note in self._voice_notes.recent_retained(4):
                age = _age(note.updated_at_us / 1_000_000)
                suffix = "" if note.state == "saved" else " · gaps"
                title = note.title or "Untitled"
                rows.append(_NoteRow(
                    # ● is the record mark: one glyph distinguishes a
                    # recording from dictated text, where a word cost width.
                    label=f"[{age}] ● {title}{suffix}",
                    kind="voice",
                    voice_note=note,
                ))
        if self._notes is not None:
            for text_note in list(self._notes.active()[:4]):
                rows.append(_NoteRow(
                    label=f"[{_age(text_note.updated_at_us / 1_000_000)}] {text_note.text}",
                    kind="text",
                    text_note=text_note,
                ))
        if not self._request_is_current():
            return
        self._note_rows = rows
        await self._show_focus(
            "QUICKNOTES" if rows else "QUICKNOTES · EMPTY",
            [row.label for row in rows] or [
                'Say “JOAT, start a quicknote …” to record.',
                'Say “JOAT, note …” to save exact text.',
            ],
            hint=DETAIL_HINT if rows else BACK_HINT,
            selectable=bool(rows),
        )

    async def _cancel_note_discard(self, request_id: str | None) -> None:
        if (
            request_id != self._active_request_id
            or self._active_capability != "QUICKNOTES"
            or self._voice_capture is None
            or not self._voice_capture.discard_pending
        ):
            return
        token = self._request_context.set(request_id)
        try:
            await self._apply_recording_control("keep")
        finally:
            self._request_context.reset(token)

    async def _show_recording_controls(self) -> None:
        capture = self._voice_capture
        assert capture is not None
        snapshot = capture.snapshot
        if capture.discard_pending:
            page, controls = "DISCARD?", _RECORDING_CONTROLS["discard_pending"]
        elif snapshot.state == "paused":
            page, controls = "PAUSED", _RECORDING_CONTROLS["paused"]
        elif snapshot.state == "recording":
            page, controls = "RECORDING", _RECORDING_CONTROLS["recording"]
        else:
            # Finalizing: no tappable rows would do anything, so say so in
            # the transient band rather than building a page for it.
            if self._request_is_current():
                self._note_rows = []
            await self._hud.assistant("Saving quicknote…")
            return
        if not self._request_is_current():
            return
        self._note_rows = [
            _NoteRow(label=label, kind="control", control=action)
            for label, action in controls
        ]
        title = snapshot.title or "Untitled"
        await self._show_focus(
            f"QUICKNOTES · {page}",
            [row.label for row in self._note_rows],
            hint=f"{title[:24]} · tap: choose",
            selectable=True,
        )

    async def _select_note(self, index: int, trace_id: str) -> None:
        if not 0 <= index < len(self._note_rows):
            return
        row = self._note_rows[index]
        if row.kind == "control":
            await self._apply_recording_control(row.control or "")
            return
        if row.kind == "voice" and row.voice_note is not None:
            await self._show_voice_note_detail(row.voice_note, trace_id)
            return
        if row.text_note is not None:
            await self._open_note_reader("QUICKNOTES · DETAIL", "Exact text", row.text_note.text, trace_id)

    async def _open_note_reader(self, title: str, label: str, text: str, trace_id: str) -> None:
        self._note_reader = _NoteReaderState(title, label, paginate_reader_text(text), trace_id)
        await self._show_note_reader()

    async def _show_note_reader(self) -> None:
        reader = self._note_reader
        if reader is None:
            return
        hint = []
        if reader.index > 0:
            hint.append("up previous")
        if reader.index + 1 < len(reader.pages):
            hint.append("down more")
        hint.append(BACK_HINT)
        await self._show_focus(
            reader.title,
            [f"{reader.index + 1}/{len(reader.pages)} · {reader.label}"[:64], *reader.pages[reader.index]],
            hint=" · ".join(hint),
        )

    async def _page_note_reader(self, reader: _NoteReaderState, event: InputEvent) -> None:
        try:
            if self._note_reader is not reader or event.request_id != self._active_request_id:
                return
            delta = -1 if event.event_type == EventType.SCROLL_TOP else (
                1 if event.event_type == EventType.SCROLL_BOTTOM else 0
            )
            target = reader.index + delta
            if delta == 0 or not 0 <= target < len(reader.pages):
                return
            reader.index = target
            token = self._request_context.set(event.request_id)
            try:
                await self._show_note_reader()
            finally:
                self._request_context.reset(token)
        finally:
            reader.page_pending = False

    async def _apply_recording_control(self, action: str) -> None:
        capture = self._voice_capture
        if capture is None:
            return
        receipt: str | None = None
        if action == "stop":
            # Transcription of a long recording takes real time, so the
            # wearer needs to know it is happening — but a whole page for
            # one transient line is waste (the wearer's ruling). It goes out
            # as the same slim band Even AI uses for "listening": the page
            # underneath stays, and the band replaces itself on completion.
            await self._hud.assistant("Saving quicknote…")
            receipt = await capture.control_stop()
        elif action == "pause":
            receipt = capture.control_pause()
        elif action == "resume":
            receipt = capture.control_resume()
        elif action == "discard":
            receipt = capture.control_request_discard()
        elif action == "confirm_discard":
            receipt = capture.control_confirm_discard()
        elif action == "keep":
            receipt = capture.control_keep()
        if receipt is not None:
            log.info("quicknote control %s: %s", action, receipt)
            # The receipt itself is the band's final word; it settles on its
            # own rather than occupying the lens until the wearer dismisses.
            await self._hud.assistant(receipt)
            await self._hud.settle_assistant()
        # Re-render whatever is now true: updated controls, or — after a save
        # or discard — the unified notes list itself.
        await self._show_notes()

    async def _show_voice_note_detail(self, note: VoiceNote, trace_id: str) -> None:
        store = self._voice_notes
        if store is None:
            return
        title = note.title or "Untitled"
        sections = note.transcribed_segments
        # A healthy note stays quiet about gaps; "0 gaps" is noise. Only a
        # real integrity problem earns a dim trailing annotation.
        meta = f"{sections} transcript section{'s' if sections != 1 else ''}"
        if note.gap_count:
            meta += f" · {note.gap_count} gap{'s' if note.gap_count != 1 else ''}"
        rows: list[str] = [title, meta]
        insights = store.insights(note.capture_id)
        latest = store.recent_retained(1)
        is_latest = bool(latest) and latest[0].capture_id == note.capture_id
        if insights is None:
            rows.append("Summary is not ready yet.")
            if is_latest:
                rows.append("Say: summarize the last note.")
        else:
            rows.append("SUMMARY")
            rows.append(insights.summary)
            if insights.key_points:
                rows.append("KEY POINTS")
                rows.extend(f"– {point}" for point in insights.key_points)
            if insights.todos:
                rows.append("TODOS")
                # □ reads as an unchecked box: these are the actionable rows,
                # and "create reminders from the last note" consumes them.
                rows.extend(f"□ {todo}" for todo in insights.todos)
        if is_latest:
            rows.append("Say: ask the last note …")
        rows.append("TRANSCRIPT")
        segments = store.segments(note.capture_id)
        rows.extend(segment.text for segment in segments if segment.kind in ("transcript", "gap"))
        if not any(segment.kind == "transcript" for segment in segments):
            rows.append("No transcript is available; retained audio may need recovery.")
        await self._open_note_reader("QUICKNOTES · NOTE", "Recording", "\n".join(rows), trace_id)

    async def _show_navigation(self) -> None:
        """Give the wearer the next natural action, not a hidden CLI contract.

        Navigation's explicit spoken grammar prevents a background transcript
        from becoming a destination. The NavigationSession then resolves and
        shows the canonical place before any routing begins.
        """
        await self._show_focus(
            "NAVIGATION",
            [
                'Say “JOAT, navigate to …”',
                "I’ll confirm the place. Then choose Walk or Cycle.",
            ],
            hint="say destination · left x2: back",
        )

    async def _show_activity(self) -> None:
        if self._actions is None:
            if self._request_is_current():
                self._activity_rows = []
            await self._show_focus("ACTIVITY · EMPTY", ["No durable actions yet."])
            return
        activity_rows = list(self._actions.recent(limit=5))
        if not self._request_is_current():
            return
        self._activity_rows = activity_rows
        rows = [
            f"{self._action_label(item.kind)} · {item.state.replace('_', ' ').title()}"
            for item in self._activity_rows
        ]
        await self._show_focus(
            "ACTIVITY" if rows else "ACTIVITY · EMPTY",
            rows or ["No durable actions yet."],
            hint=DETAIL_HINT if rows else BACK_HINT,
            selectable=bool(rows),
        )

    async def _select_activity(self, index: int) -> None:
        if not 0 <= index < len(self._activity_rows):
            return
        item = self._activity_rows[index]
        # The row already reads "{label} · {state}"; a whole page to repeat it
        # plus a truncated action id is the page this list was meant to avoid.
        await self._hud.assistant(
            f"{self._action_label(item.kind)} · "
            f"{item.state.replace('_', ' ').title()} · "
            f"ref {item.action_id[:8]}",
        )
        await self._hud.settle_assistant()

    @staticmethod
    def _action_label(kind: str) -> str:
        return kind.replace(".", " ").capitalize()

    async def _show_focus(
        self,
        title: str,
        items: list[str],
        *,
        hint: str = BACK_HINT,
        selectable: bool = False,
        planning_grid: tuple[PlanningGridCell, ...] | None = None,
    ) -> None:
        """Render non-dashboard capability states in the shared focus shell."""
        await self._send_list(HudList(
            items=items,
            title=title,
            hint=hint,
            selectable=selectable,
            planning_grid=planning_grid,
        ))

    async def _send_list(self, message: HudList) -> None:
        """Attach the generation ID and drop replies from superseded opens."""
        if not self._request_is_current():
            return
        request_id = self._request_context.get()
        await self._transport.send(replace(message, request_id=request_id))

    def _request_is_current(self) -> bool:
        request_id = self._request_context.get()
        return request_id is None or request_id == self._active_request_id

    @staticmethod
    def _canonical_capability(name: str) -> str:
        normalized = name.strip().upper()
        if normalized == "REMINDERS":
            return "TASKS"
        if normalized == "DEBATE":
            return "BRAINSTORM"
        return normalized

    async def _repeat(self) -> None:
        last = self._status.get("last_reply")
        await self._hud.assistant(last if last else "Nothing to repeat yet.")
        await self._hud.settle_assistant()

    async def repaint_home(self) -> None:
        """Compatibility path for older plugins that did not buffer Home."""
        last_reply = self._status.get("last_reply")
        if last_reply:
            await self._hud.assistant(last_reply)
            await self._hud.settle_assistant()
        last_heard = self._status.get("last_transcript")
        if last_heard:
            await self._hud.context(f"you  {last_heard}")


def voice_request_id() -> str:
    """Mint one Mac-initiated generation id the plugin will adopt."""
    return f"{VOICE_REQUEST_PREFIX}{secrets.token_hex(6)}"


_OPEN_CAPABILITY = re.compile(
    r"^(?:open|show|read)(?:\s+(?:my|the))?\s+(?P<name>"
    r"messages|texts|conversations|quick\s?notes|notes|planning|calendar|"
    r"tasks|reminders|notifications|activity|"
    # The two surfaces that had no spoken opener. 'agent terminal' before
    # 'terminal' so the longer name wins the alternation.
    r"brainstorm|debate|agent\s?terminal|terminal)$",
    re.IGNORECASE,
)

_SPOKEN_CAPABILITY = {
    "messages": "Messages",
    "texts": "Messages",
    "conversations": "Messages",
    "quicknotes": "QuickNotes",
    "quick notes": "QuickNotes",
    "notes": "QuickNotes",
    "planning": "Planning",
    "calendar": "Calendar",
    "tasks": "Tasks",
    "reminders": "Reminders",
    "notifications": "Notifications",
    "activity": "Activity",
    # Brainstorm and Agent Terminal are DIFFERENT features; both need a
    # spoken opener, and neither maps to the other.
    "brainstorm": "Brainstorm",
    "debate": "Brainstorm",
    "agent terminal": "Agent Terminal",
    "agentterminal": "Agent Terminal",
    "terminal": "Agent Terminal",
}


class CapabilityOpenController:
    """Open a capability surface from speech alone.

    "JOAT, show my messages" must land the wearer on the same page a menu tap
    reaches. The router runs the identical open path under a fresh
    voice-prefixed generation id, which the plugin adopts on arrival. The
    router reference is a provider because composition builds the voice
    command chain before the capability router exists.
    """

    def __init__(self, capabilities: Callable[[], CapabilityRouter]) -> None:
        self._capabilities = capabilities

    async def handle_command(self, text: str) -> str | None:
        # Whisper punctuates short utterances ("Show my messages."). Terminal
        # punctuation is STT noise for a deterministic surface open.
        normalized = " ".join(text.strip().split()).rstrip(".,!?").rstrip()
        match = _OPEN_CAPABILITY.fullmatch(normalized)
        if match is None:
            return None
        name = _SPOKEN_CAPABILITY[" ".join(match.group("name").lower().split())]
        await self._capabilities().open(name, request_id=voice_request_id())
        shown = "Planning" if name in {"Calendar", "Tasks", "Reminders"} else name
        return f"Opening {shown}."

    async def reflect_quicknotes(self) -> None:
        """Repaint the QuickNotes surface after a voice capture command.

        A voice-started recording must show its control page even when no
        menu is open; after a stop, the refreshed list is the receipt.
        """
        await self._capabilities().open("QuickNotes", request_id=voice_request_id())
