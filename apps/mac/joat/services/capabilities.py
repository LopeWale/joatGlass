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
from collections.abc import Awaitable, Coroutine, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone, tzinfo
from typing import Any, Callable, Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..channels.cards import Card, CardLog
from ..channels.imessage import IMessageStore, MessageEntry, MessageHistoryPage, MessageThread
from ..core.protocol import (
    SavedPlace,
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
from ..persistence.notes import NoteSource, NoteStore, QuickNote
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
    action: Literal["scope", "previous", "next", "week", "day", "entry", "earlier_entries", "more_entries"]
    scope: PlanningScope | None = None
    day: date | None = None
    entry: PlanningEntry | None = None


@dataclass(frozen=True, slots=True)
class _PlanningParent:
    scope: PlanningScope
    day: date
    day_offset: int
    selected_index: int


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

    def control_start(self) -> str: ...


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
class _NoteListState:
    """Immutable collection for one visit; page indexes never identify notes."""

    rows: tuple[_NoteRow, ...]
    index: int = 0
    page_pending: bool = False


# The firmware list draws at most 20 rows (HudPageBuilder.listLayout caps
# there), and an archive page carries up to two paging rows after its notes.
#: Sixteen notes, two control rows and two paging rows fit the firmware
#: list's twenty-row cap on every page.
_NOTE_LIST_PAGE_SIZE = 16
#: The QuickNotes page is a control surface: its first rows start a
#: recording or take dictation by tap. Voice works from anywhere.
_NOTE_CONTROLS: tuple[tuple[str, str], ...] = (
    ("● Record a voice note", "start"),
    ("✎ Dictate a note", "dictate"),
)
#: The Navigation page's open control; the row asks for the place itself.
_NAVIGATION_CONTROL_ROW = "➤ Navigate to a place"
#: Saved places listed above it; the firmware list holds 20 rows.
_NAVIGATION_PLACE_ROWS = 16
#: The conversation page's reply control, drawn after the messages.
_REPLY_CONTROL_ROW = "✎ Reply"
#: ask(question, field, consume) -> published; PromptedSpeechRuntime.ask_capability.
AskWearer = Callable[[str, str, Callable[[str], Awaitable[None]]], Awaitable[bool]]


@dataclass(slots=True)
class _NoteReaderState:
    """The exact reader: any long text, paged, under one capability.

    Born for QuickNotes; now also the read-only Brainstorm transcript
    (the audit's B3) and the long-message review before confirmation
    (M7). `on_tap` is what a row tap does while the reader is open —
    continue the session, confirm the draft — and `tap_hint` is how the
    footer promises it.
    """

    title: str
    label: str
    pages: tuple[tuple[str, ...], ...]
    trace_id: str
    index: int = 0
    page_pending: bool = False
    capability: str = "QUICKNOTES"
    on_tap: Callable[[], Awaitable[None]] | None = None
    tap_hint: str | None = None


@dataclass(slots=True)
class _MessageHistoryState:
    """Mac-private page stack for the currently selected conversation."""

    thread: MessageThread
    pages: list[MessageHistoryPage]
    index: int = 0
    #: Row index of the "✎ Reply" control on the page as drawn, or None.
    reply_index: int | None = None


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
        terminal_view: Callable[[], Awaitable[None]] | None = None,
        brainstorm_recap: Callable[[], DebateSnapshot] | None = None,
        brainstorm_replay: Callable[[], Awaitable[bool]] | None = None,
        brainstorm_sessions: Callable[[], list[Any]] | None = None,
        brainstorm_continue: Callable[[str], Awaitable[bool]] | None = None,
        terminal_rows: Callable[[], list[tuple[str, str]]] | None = None,
        terminal_focus: Callable[[str], Awaitable[None]] | None = None,
        terminal_busy: Callable[[], bool] | None = None,
        brainstorm_recent: Callable[..., list[str]] | None = None,
        brainstorm_unavailable: Callable[[], BrainstormHudView | None] | None = None,
        panels: PanelPublisher | None = None,
        wearer_zone: WearerZone | None = None,
        terminal_open: Callable[[str, str | None, str, Callable[[], bool]], Awaitable[None]] | None = None,
        terminal_current: Callable[[], str | None] | None = None,
        message_ui: MessageUIController | None = None,
        complete_reminder: Callable[[str], Awaitable[str | None]] | None = None,
        ask: AskWearer | None = None,
        navigate: Callable[[str], Awaitable[str | None]] | None = None,
        places: Callable[[], Sequence[SavedPlace]] | None = None,
    ) -> None:
        self._hud = hud
        # A tapped control row asks its own question on the lens and routes
        # the answer; `navigate` starts a route to a spoken destination.
        self._ask = ask
        self._navigate = navigate
        # The wearer's saved places (phone page), listed first on the
        # Navigation page so a tap on "Home" starts the route outright.
        self._places = places
        self._place_rows: list[SavedPlace] = []
        # Defaults to this Mac's zone, which is exactly the pre-wearer-zone
        # behaviour for any construction that does not inject one.
        self._wearer_zone = wearer_zone or WearerZone(system_home_zone)
        self._transport = transport
        self._status = status
        self._messages = messages
        self._cards = cards
        self._complete_reminder = complete_reminder
        self._planning_completion_active = False
        self._replies = replies
        self._message_ui = message_ui
        self._personal_data = personal_data
        self._notes = notes
        self._voice_notes = voice_notes
        self._voice_capture = voice_capture
        self._actions = actions
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
        # The host's own word on a missing seat: when it answers with a frame,
        # the idle row and NEW show that frame instead of a promise the tap
        # cannot keep.
        self._brainstorm_unavailable = brainstorm_unavailable
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
        self._planning_day_offset = 0
        self._planning_page_day: date | None = None
        self._planning_rows: list[_PlanningRow] = []
        self._planning_parents: list[_PlanningParent] = []
        self._planning_selected_index = -1
        self._planning_back_pending = False
        self._planning_revision = 0
        self._planning_context: ContextVar[int | None] = ContextVar(
            f"planning_revision_{id(self)}", default=None,
        )
        self._planning_grid_entries: dict[date, tuple[PlanningEntry, ...]] = {}
        self._planning_grid_request_id: str | None = None
        self._note_rows: list[_NoteRow] = []
        self._note_list: _NoteListState | None = None
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
        if event.container_name == "notification-dismiss":
            if event.trace_id and self._cards.acknowledge(event.trace_id):
                asyncio.get_running_loop().create_task(self._retire_notification(event.trace_id))
            return
        if event.container_name == "capability-lifecycle":
            if event.selected_name == "planning-back":
                if (event.request_id is not None and event.request_id == self._active_request_id
                        and self._active_capability in {"PLANNING", "CALENDAR", "TASKS", "REMINDERS"}
                        and self._planning_parents and not self._planning_back_pending
                        and not self._planning_completion_active):
                    self._planning_back_pending = True
                    self._schedule_planning(self._back_planning(event.request_id))
                return
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
                self._note_list = None
                self._note_rows = []
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

            if self._active_capability in {"PLANNING", "CALENDAR", "TASKS", "REMINDERS"}:
                self._schedule_planning(open_requested())
            else:
                asyncio.get_running_loop().create_task(open_requested())
        elif event.container_name == "capdata" and event.selected_index is not None:
            terminal_revision = None
            message_epoch = None
            reader = self._note_reader
            if (
                reader is not None and reader.on_tap is not None
                and event.selected_name == reader.capability
                and self._active_capability == reader.capability
                and event.request_id == self._active_request_id
            ):
                if reader.title == "MESSAGES · REVIEW" and (
                    reader.page_pending or event.trace_id != reader.trace_id
                    or event.selected_index != reader.index
                ):
                    return
                # A tap inside an open reader is the reader's own action
                # (continue this Brainstorm, confirm this draft) — never a
                # row selection on the list underneath it.
                asyncio.get_running_loop().create_task(self._tap_reader(reader))
                return
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
            schedule: Callable[[Coroutine[Any, Any, None]], asyncio.Task[None]] = (
                asyncio.get_running_loop().create_task
            )
            if event.selected_name in {"PLANNING", "CALENDAR", "TASKS", "REMINDERS"}:
                if (event.request_id != self._active_request_id or self._planning_back_pending
                        or self._planning_completion_active):
                    return
                schedule = self._schedule_planning
            schedule(self._select_capability_row(
                event.selected_name or "",
                event.selected_index,
                event.trace_id or "hud",
                event.request_id,
                terminal_revision=terminal_revision,
                message_epoch=message_epoch,
                quick_notes_page_index=event.quick_notes_page_index,
            ))
        elif event.container_name == "message-cancel" and self._message_ui is not None:
            if self._message_ui.matches(event.request_id, event.trace_id):
                self._message_ui.cancel(event.selected_name or "")
        elif (
            event.container_name == "capgrid"
            and event.selected_index is not None
            and (event.planning_day is not None or event.planning_anchor_unix_ms is not None)
        ):
            # Dashboard right-pane tap: the hovered DATE travels as the
            # planning anchor and the item's position as the index. No
            # calendar/reminder identifier crosses the wire — the entry is
            # re-resolved from a fresh authoritative DAY projection, so a
            # native edit between paint and tap can never open the wrong item.
            if (event.request_id != self._active_request_id or self._planning_back_pending
                    or self._planning_completion_active):
                return
            self._schedule_planning(self._select_grid_entry(
                event.planning_anchor_unix_ms,
                event.selected_index,
                event.request_id,
                planning_day=event.planning_day,
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
        elif event.container_name == "cappage" and event.selected_name == "QUICKNOTES · LIST":
            state = self._note_list
            if (state is None or state.page_pending or self._note_reader is not None
                    or self._active_capability != "QUICKNOTES"
                    or event.request_id != self._active_request_id
                    or event.selected_index != state.index):
                return
            state.page_pending = True
            asyncio.get_running_loop().create_task(self._page_note_list(state, event))
        elif (
            event.container_name == "cappage"
            and self._note_reader is not None
            and event.selected_name == self._note_reader.capability
        ):
            reader = self._note_reader
            if (
                reader.page_pending
                or self._active_capability != reader.capability
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
                    self._note_list = None
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
                if self._note_reader is not None and self._note_reader.title == "MESSAGES · REVIEW":
                    self._note_reader = None
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
        if self._note_reader is not None and self._note_reader.title == "MESSAGES · REVIEW":
            self._note_reader = None
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
        planning_token = None
        if (self._canonical_capability(name) in {"PLANNING", "CALENDAR", "TASKS", "REMINDERS"}
                and self._planning_context.get() is None):
            self._planning_revision += 1
            planning_token = self._planning_context.set(self._planning_revision)
        self._terminal_opening_revision += 1
        self._note_reader = None
        self._note_list = None
        self._planning_parents.clear()
        self._planning_back_pending = False
        self._planning_selected_index = -1
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
            if planning_token is not None:
                self._planning_context.reset(planning_token)

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
        # A missing CLI seat used to be invisible here: the row opened on
        # "Ready for a question", the tap went to the Mac, and only then did
        # the unavailable frame appear. Say it on the first screen.
        unavailable = self._brainstorm_unavailable() if self._brainstorm_unavailable else None
        if unavailable is not None:
            await self._hud.begin_brainstorm()
            await self._hud.show_brainstorm(unavailable)
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
                hint="tap: open · left x2: back",
                selectable=True,
            )
            return
        recap = self._brainstorm_recap() if self._brainstorm_recap else None
        rows = recap.rows() if recap is not None else []
        if not rows and self._brainstorm_recent is not None:
            past = self._brainstorm_recent(4)
            if past:
                rows = ["Recent:", *past]
        # The plugin paints a Brainstorm frame only after the Mac has claimed
        # the surface with this rebuild — a spoken start always sends it
        # (DebateStateSink.open). The idle frame did not, so a voice-opened
        # Brainstorm showed nothing and its tap went nowhere.
        await self._hud.begin_brainstorm()
        await self._hud.show_brainstorm(BrainstormHudView(
            state="settled" if rows else "ready",
            topic=(recap.topic[:40] if recap is not None and recap.topic
                   else "Ready for a question"),
            collaborators=list(recap.speakers[:3]) if recap is not None and recap.speakers
            else ["Codex", "Claude"],
            activity=("\n".join(rows) if rows
                      else "Tap to speak a topic."),
        ))

    async def _show_agent_terminal(self) -> None:
        """Always open the agent chooser, including while another agent is busy."""
        rows = self._terminal_rows() if self._terminal_rows else []
        if rows:
            self._terminal_row_names = [name for name, _row in rows]
            await self._show_focus(
                "AGENT TERMINAL",
                [row for _name, row in rows],
                # Short enough to RENDER. The longer form
                # ("terminal <instruction>") was cut mid-word on the lens —
                # a hint that does not fit teaches nothing.
                hint="tap: choose agent · double-click: back",
                selectable=True,
            )
            return
        if self._terminal_rows is not None:
            self._terminal_row_names = []
            await self._show_focus(
                "AGENT TERMINAL", ["Add an agent in JOAT on your Mac.",
                                   "Intelligence → Agent connections"],
                hint="double-click: back", selectable=False,
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
            # Ten conversations, the store's ceiling: the custom list windows
            # around the cursor (visibleHudListItems), so a picker longer
            # than one screen scrolls instead of hiding threads (the
            # audit's M5 found five, with no way to reach the sixth).
            rows = await asyncio.to_thread(self._messages.recent_threads, 10)
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

    async def review_message(self, recipient_label: str, body: str, *, timeout_s: float = 90.0) -> bool:
        """Show a whole draft before its confirmation; True when the wearer taps.

        The audit's M7: a draft longer than the confirmation prompt's 120
        UTF-16 units was rejected outright ("shorten it and try again").
        The wearer said what they said; they should read it, in full, and
        then confirm. Back, Home, another selection or the timeout all
        return False and nothing is sent.
        """
        loop = asyncio.get_running_loop()
        decided: asyncio.Future[bool] = loop.create_future()

        async def confirm_from_review() -> None:
            reader = self._note_reader
            if (reader is not None and reader.title == "MESSAGES · REVIEW"
                    and reader.index == len(reader.pages) - 1 and not decided.done()):
                decided.set_result(True)

        # Reviewing this draft is an internal step of the same action. A new
        # request ID makes the Hub retire its selected-message owner; clearing
        # selection here also cancels the prepared action calling this method.
        request_id = (self._active_request_id if self._active_capability == "MESSAGES"
                      and self._active_request_id else voice_request_id())
        self._active_capability = "MESSAGES"
        self._active_request_id = request_id
        token = self._request_context.set(request_id)
        reader: _NoteReaderState | None = None
        try:
            reader = _NoteReaderState(
                "MESSAGES · REVIEW", f"To {recipient_label}", paginate_reader_text(body), f"review-{secrets.token_hex(16)}",
                capability="MESSAGES", on_tap=confirm_from_review, tap_hint="tap: confirm",
            )
            self._note_reader = reader
            await self._show_note_reader()
            deadline = loop.time() + timeout_s
            while not decided.done():
                if (self._note_reader is not reader or loop.time() >= deadline
                        or self._active_request_id != request_id or self._active_capability != "MESSAGES"):
                    return False
                await asyncio.sleep(0.2)
            return decided.result()
        finally:
            self._request_context.reset(token)
            if reader is not None and self._note_reader is reader:
                self._note_reader = None

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
            unavailable = self._brainstorm_unavailable() if self._brainstorm_unavailable else None
            # NEW from the picker claims the surface exactly as a spoken start.
            await self._hud.begin_brainstorm()
            if unavailable is not None:
                await self._hud.show_brainstorm(unavailable)
                return
            await self._hud.show_brainstorm(BrainstormHudView(
                state="ready",
                topic="Ready for a question",
                collaborators=["Codex", "Claude"],
                activity="Tap to speak a topic.",
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
        past = rows[picked]
        turns = getattr(past, "turns", ())
        if turns and self._note_reader is None:
            # Read first, continue on purpose (the audit's B3): selecting a
            # saved session used to start a continuation — two subscription
            # CLIs — before the wearer had seen a word of it. The exact
            # reader shows the whole transcript; a tap continues it.
            session_id = past.session_id
            continue_session = self._brainstorm_continue

            async def continue_saved() -> None:
                self._note_reader = None
                if not await continue_session(session_id):
                    await self._show_focus(
                        "BRAINSTORM",
                        ["That session could not be reopened.",
                         f"Tap “{_NEW_BRAINSTORM_ROW}” to start another."],
                    )

            body = "\n\n".join(f"[{label}]\n{text}" for label, text in turns)
            await self._open_note_reader(
                "BRAINSTORM · SAVED", getattr(past, "title", "session"), body, "brainstorm",
                capability="BRAINSTORM", on_tap=continue_saved, tap_hint="tap: continue",
            )
            return
        if not await self._brainstorm_continue(past.session_id):
            await self._show_focus(
                "BRAINSTORM",
                ["That session could not be reopened.",
                 f"Tap “{_NEW_BRAINSTORM_ROW}” to start another."],
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
                await self._show_focus(
                    "MESSAGES · COMPOSE",
                    ["Sending is unavailable in this session.", "You can still read your conversations."],
                    hint=BACK_HINT,
                )
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
        if index == history.reply_index and self._message_ui is not None:
            # The tapped "✎ Reply" row: the same prepared reply the spoken
            # "reply" starts, with its question on the lens.
            self._message_selection_active = True
            await self._message_ui.begin_reply()
            return
        token = self._request_context.set(request_id)
        try:
            # Row zero is the page heading; speaker rows follow it, then
            # the reply control, which is not a message.
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
        reader = self._note_reader
        if reader is not None and reader.title == "MESSAGES · REVIEW":
            # Review paging belongs exclusively to on_input's exact reader
            # identity/index gate, never the generic history fallback.
            return
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
            page_label = "Latest messages" if page.has_older else "Complete conversation on this Mac"
        else:
            page_label = "Older messages"
        can_reply = state.thread.reply_target is not None and self._replies is not None
        # Paging travels as rows, not swipes: the Hub draws this thread as a
        # firmware list (Even's own ListContainerProperty, one rebuild, no
        # image tiles), and a firmware list reports taps but no scroll
        # events. A tap on one of these rows is the page request; their
        # indices ride in `page_controls` so the plugin never matches copy.
        hint = " · ".join([
            "tap read" if can_reply else "tap read · read-only", BACK_HINT,
        ])
        context = [self._message_history_row(state.thread, entry) for entry in page.entries]
        items = [page_label, *context]
        # The page is a control surface: replying is a row to tap, which
        # asks "What should I say?" on the lens. Voice works from anywhere.
        state.reply_index = None
        if can_reply and self._message_ui is not None:
            state.reply_index = len(items)
            items.append(_REPLY_CONTROL_ROW)
        newer_index: int | None = None
        older_index: int | None = None
        if state.index > 0:
            newer_index = len(items)
            items.append("▲ Newer messages")
        if page.has_older:
            older_index = len(items)
            items.append("▼ Older messages")
        await self._send_list(HudList(
            items=items,
            title="MESSAGES · CONVERSATION",
            context_label=state.thread.sender,
            hint=hint,
            selectable=bool(page.entries),
            page_controls=(newer_index, older_index),
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
        # Back lands on the conversation, whose "✎ Reply" row is the reply
        # control; the reader itself only reads.
        hint = " · ".join([
            *hint_parts, *([] if can_reply else ["read-only"]), BACK_HINT,
        ])
        await self._send_list(HudList(
            items=[
                self._message_detail_metadata(state.thread, state.entry, state.index, total),
                *state.pages[state.index],
            ],
            title="MESSAGES · MESSAGE",
            context_label=state.thread.sender,
            hint=hint,
        ))

    @staticmethod
    def _message_detail_metadata(
        thread: MessageThread,
        entry: MessageEntry,
        page_index: int,
        page_count: int,
    ) -> str:
        sender = "you" if entry.from_me else CapabilityRouter._message_sender(thread, entry)
        details: list[str] = []
        if entry.unread:
            details.append("unread")
        if entry.delivery is not None:
            details.append(entry.delivery)
        if entry.attachment_count:
            details.append("attachment")
        metadata = " · ".join([f"{page_index + 1}/{page_count}", f"From {sender}", *details])
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
        if thread.is_group:
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
            "Participant" if thread.is_group else thread.sender
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
        quick_notes_page_index: int | None = None,
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
                    await self._select_note(index, trace_id, quick_notes_page_index)
                case "ACTIVITY":
                    await self._select_activity(index)
                case "NAVIGATION":
                    await self._select_navigation(index)
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
        entries = self._cards.active()
        if not self._request_is_current():
            return
        self._notification_rows = entries
        if not entries:
            await self._show_focus("NOTIFICATIONS · EMPTY", ["No unread JOAT notifications."])
            return
        await self._send_list(HudList(
            items=[f"[{_age(c.at)}] {c.render().splitlines()[0]}" for c in entries]
            + ["Clear JOAT notifications"],
            title="NOTIFICATIONS",
            control_start_index=len(entries),
            hint="tap: read · left x2: back",
            selectable=True,
        ))

    async def _select_notification(self, index: int) -> None:
        # Selection is admitted by the capability request/generation above.
        # IDs bind the displayed snapshot; a new arrival cannot shift its target.
        if not self._request_is_current():
            return
        if self._notification_rows and index == len(self._notification_rows):
            snapshot_ids = [card.card_id for card in self._notification_rows]
            active_ids = {card.card_id for card in self._cards.active()}
            self._cards.dismiss_snapshot(snapshot_ids)
            for card_id in snapshot_ids:
                if card_id in active_ids:
                    await self._retire_notification(card_id)
            await self._show_notifications()
            return
        if not 0 <= index < len(self._notification_rows):
            return
        card = self._notification_rows[index]
        # A cached Back snapshot can legitimately reopen the same retained
        # card. Reading it again is idempotent, not a dead tap or a shifted row.
        if self._cards.acknowledge(card.card_id):
            await self._retire_notification(card.card_id)
        if not self._request_is_current():
            return
        header = card.render().splitlines()[:1]
        full = card.body or card.summary
        body_rows = [row for page in paginate_reader_text(full) for row in page]
        rows = [*header, *body_rows][:19] or ["Notification content unavailable."]
        await self._show_focus("NOTIFICATIONS · DETAIL", rows)

    async def _retire_notification(self, card_id: str) -> None:
        """Retire only this JOAT card's current/deferred Hub presentation.

        The opaque identity makes an interleaved newer notification safe;
        an empty generic Main write would otherwise erase unrelated content.
        """
        await self._hud.show("", content_kind="notification", trace_id=card_id)

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
        # Mirror only entries actually published to each month pane. A tap
        # supplies its position in THIS snapshot, not in a later native query.
        self._planning_grid_entries = (
            {day.day: day.entries[:6] for day in projection.days}
            if projection.scope is PlanningScope.MONTH else {}
        )
        self._planning_grid_request_id = self._request_context.get()
        if self._planning_selected_index < 0:
            self._planning_selected_index = (
                next((index for index, day in enumerate(projection.days)
                      if day.day == projection.selected_day), 0)
                if projection.scope is PlanningScope.MONTH else 0
            )
        day_delta = (projection.selected_day - datetime.now(self._planning_zone()).date()).days
        day_label = "Today" if day_delta == 0 else "Tomorrow" if day_delta == 1 else projection.selected_day.strftime("%a %b %d")
        await self._show_focus(
            f"{origin} · {projection.title}",
            [row.label for row in self._planning_rows],
            hint=PLANNING_HINT,
            selectable=True,
            planning_grid=self._planning_grid_for(projection),
            planning_entries=(
                tuple(PlanningGridEntry(time=f"{day_label}, {row.entry.time_label}", kind=row.entry.kind, title=row.entry.title)
                      if row.entry is not None else None for row in self._planning_rows)
                if projection.scope is PlanningScope.DAY else None
            ),
            context_label=f"{origin.title()} · {projection.title.removeprefix('PLAN · ')}",
            # Semantic Mac-owned actions define the boundary; localized text
            # and the number of native agenda entries do not change it.
            control_start_index=next((
                index for index, row in enumerate(self._planning_rows)
                if row.action in ("previous", "next", "scope")
            ), len(self._planning_rows)),
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

        # A wire list has at most 20 rows. Keep the five date/scope actions
        # reachable on every day page and bind entries to these exact rows.
        if projection.scope is not PlanningScope.DAY or self._planning_page_day != projection.selected_day:
            self._planning_day_offset = 0
        self._planning_page_day = projection.selected_day if projection.scope is PlanningScope.DAY else None

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
        total = len(selected.entries)
        if total > 15:
            last_offset = ((total - 1) // 13) * 13
            self._planning_day_offset = min(max(0, self._planning_day_offset), last_offset)
            page_entries = selected.entries[self._planning_day_offset:self._planning_day_offset + 13]
        else:
            self._planning_day_offset = 0
            page_entries = selected.entries
        if self._planning_day_offset:
            rows.append(_PlanningRow(label="‹ Earlier entries", action="earlier_entries"))
        for entry in page_entries:
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
        if self._planning_day_offset + len(page_entries) < total:
            rows.append(_PlanningRow(label="More entries ›", action="more_entries"))
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

    def _schedule_planning(self, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        self._planning_revision += 1
        revision = self._planning_revision

        async def run() -> None:
            token = self._planning_context.set(revision)
            try:
                if revision != self._planning_revision:
                    work.close()
                    return
                await work
            finally:
                self._planning_context.reset(token)

        return asyncio.get_running_loop().create_task(run())

    def _push_planning_parent(self, selected_index: int) -> None:
        self._planning_parents.append(_PlanningParent(
            self._planning_scope, self._planning_selected_day,
            self._planning_day_offset, selected_index,
        ))
        # Scope controls can revisit levels indefinitely; retain only bounded
        # navigation context, never native records or confirmation authority.
        self._planning_parents = self._planning_parents[-8:]
        self._planning_selected_index = -1

    async def _back_planning(self, request_id: str) -> None:
        token = self._request_context.set(request_id)
        try:
            if request_id != self._active_request_id or not self._planning_parents:
                return
            parent = self._planning_parents.pop()
            self._planning_scope = parent.scope
            self._planning_selected_day = parent.day
            self._planning_day_offset = parent.day_offset
            self._planning_page_day = parent.day if parent.scope is PlanningScope.DAY else None
            self._planning_selected_index = parent.selected_index
            await self._show_planning()
        finally:
            self._planning_back_pending = False
            self._request_context.reset(token)

    async def _select_planning(self, index: int) -> None:
        if not 0 <= index < len(self._planning_rows):
            return
        if not self._request_is_current():
            return
        row = self._planning_rows[index]
        self._planning_selected_index = -1
        if row.action in ("earlier_entries", "more_entries"):
            self._planning_day_offset = max(
                0, self._planning_day_offset + (-13 if row.action == "earlier_entries" else 13)
            )
            await self._show_planning()
            return
        if row.action == "scope" and row.scope is not None:
            self._planning_day_offset = 0
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
            self._push_planning_parent(
                list(self._planning_grid_entries).index(row.day)
                if row.day in self._planning_grid_entries else index
            )
            self._planning_selected_day = row.day
            self._planning_scope = PlanningScope.WEEK
            await self._show_planning()
            return
        if row.action == "day" and row.day is not None:
            if self._planning_scope is not PlanningScope.DAY:
                self._push_planning_parent(index)
            self._planning_day_offset = 0
            self._planning_selected_day = row.day
            self._planning_scope = PlanningScope.DAY
            await self._show_planning()
            return
        if row.action == "entry" and row.entry is not None:
            self._push_planning_parent(index)
            self._planning_rows = []
            entry = row.entry
            if entry.kind == "reminder":
                await self._complete_planning_reminder(entry)
                return
            await self._show_planning_event_detail(entry)

    async def _complete_planning_reminder(self, entry: PlanningEntry) -> None:
        if not self._request_is_current() or self._planning_completion_active:
            return
        if self._complete_reminder is None:
            await self._show_focus(
                "PLANNING · REMINDER",
                [row for page in paginate_reader_text(
                    f"{entry.title}\nReminder completion is unavailable. Open JOAT on the Mac."
                ) for row in page][:19],
            )
            return
        self._planning_completion_active = True
        try:
            explanation = await self._complete_reminder(entry.native_identifier)
            if explanation and self._request_is_current():
                await self._show_focus(
                    "PLANNING · REMINDER",
                    [row for page in paginate_reader_text(f"{entry.title}\n{explanation}") for row in page][:19],
                    context_label=f"Planning · {self._planning_selected_day.strftime('%a %b %d')}",
                )
        finally:
            self._planning_completion_active = False

    async def _select_grid_entry(
        self,
        anchor_unix_ms: int | None,
        entry_index: int,
        request_id: str | None,
        *,
        planning_day: str | None = None,
    ) -> None:
        """Open one entry tapped on the dashboard's right pane.

        The wire carries a date and index into the published month pane.
        Re-resolve that snapshot identity in current native data; a reorder
        must not change which reminder the wearer asked to complete.
        """
        client = self._personal_data
        if client is None:
            return
        if request_id != self._active_request_id or request_id != self._planning_grid_request_id:
            return
        try:
            if planning_day is not None:
                selected_day = date.fromisoformat(planning_day)
            elif (not isinstance(anchor_unix_ms, bool) and isinstance(anchor_unix_ms, int)
                  and 0 <= anchor_unix_ms <= 32_503_680_000_000):
                selected_day = datetime.fromtimestamp(
                    anchor_unix_ms / 1_000, tz=self._planning_zone()
                ).date()
            else:
                return
        except (ValueError, OverflowError, OSError):
            return
        if selected_day not in self._planning_grid_entries:
            return
        displayed = self._planning_grid_entries.get(selected_day, ())
        token = self._request_context.set(request_id)
        try:
            if not self._request_is_current():
                return
            if entry_index == len(displayed):
                # The final pane row is Open day, including on empty dates.
                self._push_planning_parent(list(self._planning_grid_entries).index(selected_day))
                self._planning_selected_day = selected_day
                self._planning_scope = PlanningScope.DAY
                self._planning_day_offset = 0
                await self._show_planning()
                return
            if not 0 <= entry_index < len(displayed):
                await self._show_focus("PLANNING · REFRESH", ["This item is no longer in the displayed agenda.", "Open Planning again."])
                return
            selected_entry = displayed[entry_index]
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
            matches = [entry for entry in entries
                       if entry.native_identifier == selected_entry.native_identifier
                       and entry.kind == selected_entry.kind]
            if len(matches) != 1:
                await self._show_focus(
                    "PLANNING · REFRESH",
                    [row for page in paginate_reader_text(
                        f"{selected_entry.title}\nThis item changed or is no longer on this day. Refresh Planning."
                    ) for row in page][:19],
                )
                return
            self._push_planning_parent(list(self._planning_grid_entries).index(selected_day))
            self._planning_rows = []
            self._planning_grid_entries = {}
            # Anchor follow-up Back navigation and completion on the tapped day.
            self._planning_selected_day = selected_day
            entry = matches[0]
            if entry.kind == "reminder":
                await self._complete_planning_reminder(entry)
                return
            await self._show_planning_event_detail(entry)
        finally:
            self._request_context.reset(token)

    async def _show_planning_event_detail(self, entry: PlanningEntry) -> None:
        """Use the same read-only event presentation from every Planning view."""
        await self._show_focus(
            f"{self._planning_origin()} · {entry.kind.upper()}",
            [entry.title, f"{entry.time_label} · {entry.kind.title()}"],
            hint="left x2 Planning",
        )

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
        self._planning_day_offset = 0
        self._planning_page_day = selected
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
        self._note_list = None
        capture = self._voice_capture
        if capture is not None and capture.active:
            await self._show_recording_controls()
            return
        if self._notes is None and self._voice_notes is None:
            if self._request_is_current():
                self._note_rows = []
            await self._show_focus(
                "QUICKNOTES · SETUP",
                ["Notes storage is unavailable on this Mac."],
            )
            return
        rows: list[_NoteRow] = []
        # Snapshot every retained note, then send bounded pages. Storage kind
        # never partitions the wearer's chronological collection.
        if self._voice_notes is not None:
            for note in self._voice_notes.retained():
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
            for text_note in self._notes.active():
                rows.append(_NoteRow(
                    label=f"[{_age(text_note.updated_at_us / 1_000_000)}] {text_note.text}",
                    kind="text",
                    text_note=text_note,
                ))
        if not self._request_is_current():
            return
        def chronology(row: _NoteRow) -> tuple[int, str, str]:
            if row.voice_note is not None:
                return (-row.voice_note.updated_at_us, row.kind, row.voice_note.capture_id)
            assert row.text_note is not None
            return (-row.text_note.updated_at_us, row.kind, row.text_note.note_id)
        self._note_list = _NoteListState(tuple(sorted(rows, key=chronology)))
        await self._show_note_list_page()

    async def _show_note_list_page(self) -> None:
        state = self._note_list
        if state is None or not self._request_is_current():
            return
        start = state.index * _NOTE_LIST_PAGE_SIZE
        notes = list(state.rows[start:start + _NOTE_LIST_PAGE_SIZE])
        # The page is a control surface: recording and dictation are rows
        # to tap, ahead of the notes on every page, so the tap mapping
        # (`_note_rows`) and the drawn list agree row for row.
        controls = [
            _NoteRow(label=label, kind="control", control=action)
            for label, action in _NOTE_CONTROLS
        ]
        rows = [*controls, *notes]
        self._note_rows = rows
        count = max(1, (len(state.rows) + _NOTE_LIST_PAGE_SIZE - 1) // _NOTE_LIST_PAGE_SIZE)
        hints = [DETAIL_HINT if notes else "tap: choose · " + BACK_HINT]
        if state.index:
            hints.insert(0, "up newer")
        if state.index + 1 < count:
            hints.insert(0, "down older")
        labels = [row.label for row in rows]
        # The firmware list reports taps, never scrolls, so the archive pages
        # through rows: the plugin turns a tap on either into the page request
        # the swipe used to send. Both sit past the note rows, so a tap on
        # them never reaches _select_note.
        newer_index = older_index = None
        if state.index:
            newer_index = len(labels)
            labels.append("▲ Newer notes")
        if state.index + 1 < count:
            older_index = len(labels)
            labels.append("▼ Older notes")
        await self._show_focus(
            "QUICKNOTES" if notes else "QUICKNOTES · EMPTY",
            labels,
            hint=" · ".join(hints),
            selectable=True,
            quick_notes_page=(state.index, count),
            page_controls=(newer_index, older_index),
        )

    async def _page_note_list(self, state: _NoteListState, event: InputEvent) -> None:
        try:
            if (self._note_list is not state or self._note_reader is not None
                    or event.request_id != self._active_request_id
                    or self._active_capability != "QUICKNOTES"):
                return
            delta = -1 if event.event_type == EventType.SCROLL_TOP else (
                1 if event.event_type == EventType.SCROLL_BOTTOM else 0)
            count = max(1, (len(state.rows) + _NOTE_LIST_PAGE_SIZE - 1) // _NOTE_LIST_PAGE_SIZE)
            target = state.index + delta
            if not delta or not 0 <= target < count:
                return
            state.index = target
            token = self._request_context.set(event.request_id)
            try:
                await self._show_note_list_page()
            finally:
                self._request_context.reset(token)
        finally:
            state.page_pending = False

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
            await self._hud.receipt("Saving quicknote…")
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

    async def _select_note(self, index: int, trace_id: str, page_index: int | None = None) -> None:
        state = self._note_list
        if state is not None and (state.page_pending or (page_index if page_index is not None else 0) != state.index):
            return
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

    async def _open_note_reader(
        self, title: str, label: str, text: str, trace_id: str, *,
        capability: str = "QUICKNOTES",
        on_tap: Callable[[], Awaitable[None]] | None = None,
        tap_hint: str | None = None,
    ) -> None:
        self._note_reader = _NoteReaderState(
            title, label, paginate_reader_text(text), trace_id,
            capability=capability, on_tap=on_tap, tap_hint=tap_hint,
        )
        await self._show_note_reader()

    async def _tap_reader(self, reader: _NoteReaderState) -> None:
        if self._note_reader is not reader or reader.on_tap is None:
            return
        try:
            await reader.on_tap()
        except Exception:
            log.exception("reader action failed capability=%s", reader.capability)
            with contextlib.suppress(Exception):
                await self._hud.assistant("That did not go through — try again.", answers_wearer=True)

    async def _show_note_reader(self) -> None:
        reader = self._note_reader
        if reader is None:
            return
        hint = []
        if reader.index > 0:
            hint.append("up previous")
        if reader.index + 1 < len(reader.pages):
            hint.append("down more")
        review = reader.title == "MESSAGES · REVIEW"
        review_complete = reader.index == len(reader.pages) - 1
        if reader.tap_hint and (not review or review_complete):
            hint.append(reader.tap_hint)
        hint.append(BACK_HINT)
        await self._show_focus(
            reader.title,
            [f"{reader.index + 1}/{len(reader.pages)} · {reader.label}"[:64], *reader.pages[reader.index]],
            hint=" · ".join(hint),
            # A reader with a tap action (continue this Brainstorm, confirm
            # this draft on its last page) must report the tap; the plugin
            # drops taps on a page it was told is not selectable.
            selectable=bool(reader.tap_hint) and (not review or review_complete),
            reader_page=(reader.trace_id, reader.index, len(reader.pages)),
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

    async def _dictate_note(self) -> None:
        """The tapped "✎ Dictate a note" row: ask, then save the exact text."""
        notes = self._notes
        if self._ask is None or notes is None:
            await self._hud.receipt("Dictation is unavailable on this Mac.")
            return

        async def consume(text: str) -> None:
            visible = " ".join(text.split())
            if not visible:
                return
            identifier = secrets.token_urlsafe(18)
            notes.create(
                note_id=identifier,
                idempotency_key=f"quick-note:{identifier}",
                text=visible,
                source=NoteSource(kind="hud.quick-note", identifier=identifier),
                now_us=time.time_ns() // 1_000,
            )
            preview = visible if len(visible) <= 72 else visible[:69] + "…"
            await self._hud.receipt(f"Saved QuickNote: {preview}")
            await self._hud.settle_assistant()
            await self._show_notes()

        if not await self._ask("What should I note?", "note", consume):
            await self._hud.receipt("The question did not reach the glasses — try again.")

    async def _apply_recording_control(self, action: str) -> None:
        capture = self._voice_capture
        if capture is None:
            if action == "dictate":
                await self._dictate_note()
            else:
                await self._hud.receipt("Recording is unavailable on this Mac.")
            return
        receipt: str | None = None
        if action == "start":
            receipt = capture.control_start()
        elif action == "dictate":
            await self._dictate_note()
            return
        elif action == "stop":
            # Transcription of a long recording takes real time, so the
            # wearer needs to know it is happening — but a whole page for
            # one transient line is waste (the wearer's ruling). It goes out
            # as the same slim band Even AI uses for "listening": the page
            # underneath stays, and the band replaces itself on completion.
            await self._hud.receipt("Saving quicknote…")
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
            await self._hud.receipt(receipt)
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
        places = list(self._places()) if self._places is not None else []
        # The saved places first, each one tap from a route; the open
        # question last. Both fit a firmware list's rows with room to spare.
        self._place_rows = places[:_NAVIGATION_PLACE_ROWS]
        rows = [f"➤ {place.name}" for place in self._place_rows] + [
            _NAVIGATION_CONTROL_ROW,
            "I’ll confirm the place. Then choose Walk or Cycle."
            if not self._place_rows
            else "Add places on the phone page.",
        ]
        await self._show_focus(
            "NAVIGATION",
            rows,
            hint="tap: choose · left x2: back",
            selectable=True,
        )

    async def _select_navigation(self, index: int) -> None:
        """A saved place starts its route; the control row asks "Where to?"."""
        navigate = self._navigate
        if index < len(self._place_rows):
            if navigate is None:
                await self._hud.receipt("Navigation cannot be started from this Mac.")
                return
            receipt = await navigate(self._place_rows[index].name)
            if receipt:
                await self._hud.receipt(receipt)
            return
        if index != len(self._place_rows):
            return
        if self._ask is None or navigate is None:
            await self._hud.receipt("Navigation cannot be started from this Mac.")
            return

        async def consume(destination: str) -> None:
            receipt = await navigate(destination)
            if receipt:
                await self._hud.receipt(receipt)

        if not await self._ask("Where to?", "destination", consume):
            await self._hud.receipt("The question did not reach the glasses — try again.")

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
        await self._hud.receipt(
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
        planning_entries: tuple[PlanningGridEntry | None, ...] | None = None,
        context_label: str | None = None,
        quick_notes_page: tuple[int, int] | None = None,
        reader_page: tuple[str, int, int] | None = None,
        control_start_index: int | None = None,
        page_controls: tuple[int | None, int | None] | None = None,
    ) -> None:
        """Render non-dashboard capability states in the shared focus shell."""
        await self._send_list(HudList(
            page_controls=page_controls,
            items=items,
            title=title,
            hint=hint,
            selectable=selectable,
            planning_grid=planning_grid,
            planning_entries=planning_entries,
            planning_navigation=(
                (bool(self._planning_parents), self._planning_selected_index)
                if self._active_capability in {"PLANNING", "CALENDAR", "TASKS", "REMINDERS"}
                else None
            ),
            context_label=context_label,
            quick_notes_page=quick_notes_page,
            reader_page=reader_page,
            control_start_index=control_start_index,
        ))

    async def _send_list(self, message: HudList) -> None:
        """Attach the generation ID and drop replies from superseded opens."""
        if not self._request_is_current():
            return
        request_id = self._request_context.get()
        await self._transport.send(replace(message, request_id=request_id))

    def _request_is_current(self) -> bool:
        request_id = self._request_context.get()
        planning_revision = self._planning_context.get()
        return ((planning_revision is None or planning_revision == self._planning_revision)
                and (request_id is None or request_id == self._active_request_id))

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
    r"brainstorm|debate|agent\s?terminal|terminal|"
    # Navigation sat in the sidebar with no spoken door (the audit's NAV1).
    # "navigate to <place>" still starts a route directly; this only opens
    # the feature's own surface.
    r"navigation|directions|map)$",
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
    "navigation": "Navigation",
    "directions": "Navigation",
    "map": "Navigation",
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
