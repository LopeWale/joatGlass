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
    BrainstormHudView,
    EventType,
    HudList,
    InputEvent,
    PlanningGridCell,
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
from .voice_note_capture import VoiceNoteCaptureSnapshot
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
# Eight visual body lines leave a safe footer margin on the G2 lens. This is
# deliberately a content page, not a short notification preview: ordinary
# texts should be readable without an unnecessary second page.
_MESSAGE_READER_PAGE_CHARACTERS = 320
_MESSAGE_READER_DISPLAY_LINE_CHARACTERS = 52


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


def _wrap_note_text(text: str, width: int = 60) -> list[str]:
    """Greedy word wrap for HUD list rows; long words are hard-split."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        while len(word) > width:
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:width])
            word = word[width:]
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


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
    pages: tuple[str, ...]
    index: int = 0


def _age(unix: float) -> str:
    mins = max(0, int((time.time() - unix) / 60))
    if mins < 1:
        return "now"
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    return f"{hours}h" if hours < 24 else f"{hours // 24}d"


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
    ) -> None:
        self._hud = hud
        self._transport = transport
        self._status = status
        self._messages = messages
        self._cards = cards
        self._replies = replies
        self._personal_data = personal_data
        self._notes = notes
        self._voice_notes = voice_notes
        self._voice_capture = voice_capture
        self._actions = actions
        self._acceptance_armed = acceptance_armed
        self._terminal_view = terminal_view
        self._message_rows: list[MessageThread] = []
        self._message_history: _MessageHistoryState | None = None
        self._message_detail: _MessageDetailState | None = None
        self._message_page_pending = False
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
        self._activity_rows: list[ActionActivity] = []
        self._active_request_id: str | None = None
        self._active_capability: str | None = None
        self._request_context: ContextVar[str | None] = ContextVar(
            f"capability_request_{id(self)}",
            default=None,
        )

    def on_input(self, event: InputEvent) -> None:
        """Transport callback — synchronous, so work is scheduled."""
        if event.container_name == "capmenu" and event.selected_name:
            asyncio.get_running_loop().create_task(
                self.open(
                    event.selected_name,
                    request_id=event.request_id,
                    planning_anchor_unix_ms=event.planning_anchor_unix_ms,
                )
            )
        elif event.container_name == "capdata" and event.selected_index is not None:
            asyncio.get_running_loop().create_task(self._select_capability_row(
                event.selected_name or "",
                event.selected_index,
                event.trace_id or "hud",
                event.request_id,
            ))
        elif event.container_name == "capmessage" and event.selected_index is not None:
            asyncio.get_running_loop().create_task(self._select_message_detail(
                event.selected_index,
                event.trace_id or "hud",
                event.request_id,
            ))
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
            and event.selected_name in ("messages-home", "messages-list", "messages-conversation")
        ):
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
        elif event.container_name == "menu" and event.selected_name == "closed":
            self.clear_message_selection("home")
            asyncio.get_running_loop().create_task(self.repaint_home())

    def clear_message_selection(self, reason: str) -> None:
        """Disarm a private reply target when its wearer-visible context ends."""
        self._message_rows = []
        self._message_history = None
        self._message_detail = None
        self._message_page_pending = False
        if self._replies is not None and self._message_selection_active:
            self._replies.clear_selection(reason)
        self._message_selection_active = False

    def on_disconnect(self) -> None:
        """Drop any Mac-private reply target when the glasses link disappears."""
        self.clear_message_selection("disconnect")

    async def open(
        self,
        name: str,
        *,
        request_id: str | None = None,
        planning_anchor_unix_ms: int | None = None,
    ) -> None:
        """Open one list generation; stale concurrent generations cannot paint."""
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
                case _:
                    log.info("unknown capability requested: %s", name)
        finally:
            self._request_context.reset(token)

    async def _show_brainstorm(self) -> None:
        """Open Brainstorm only when its required Hermes seat is ready."""
        terminal = self._status.agent_terminal_projection()
        state = str(terminal["state"])
        reason = str(terminal["reason"])
        if state != "ready":
            activity = (
                "Hermes must be set up on this home brain."
                if state == "unavailable" and reason == "endpoint_missing"
                else "Hermes is finishing the current response. Wait for ready."
                if state == "running"
                else "Hermes is required for Brainstorm. Tap to retry."
            )
            # The glasses are the control surface: a wearer selecting
            # Brainstorm must land on the actionable recovery state instead
            # of a deceptive local-only ready room. Once Hermes is ready,
            # the same menu row opens the normal round-table surface.
            await self._hud.show_brainstorm(BrainstormHudView(
                screen="agent_terminal",
                state=state,
                topic="Hermes Agent Terminal",
                collaborators=[],
                activity=activity,
                terminal_state=state,
                endpoint_host=str(terminal["endpoint_host"]),
                terminal_reason=_agent_terminal_instruction(state, reason),
                no_tools_checked=bool(terminal["no_tools_checked"]),
                acceptance_armed=False,
            ))
            return
        await self._hud.show_brainstorm(BrainstormHudView(
            screen="brainstorm",
            state="ready",
            topic="Ready for a question",
            collaborators=["Codex", "Claude", "Hermes"],
            activity='Say “JOAT, brainstorm …” to begin.',
            terminal_state=state,
            endpoint_host=str(terminal["endpoint_host"]),
            terminal_reason=reason,
            no_tools_checked=bool(terminal["no_tools_checked"]),
            acceptance_armed=self._acceptance_armed,
        ))

    async def _show_agent_terminal(self) -> None:
        """Open the terminal on the wearer's actual work sessions.

        The roster paints the real thing — which sessions exist, which is
        focused, what each last said. Only when no roster is configured
        does this fall back to the readiness/recovery card, which is all
        this surface used to show.
        """
        if self._terminal_view is not None:
            await self._terminal_view()
            return
        terminal = self._status.agent_terminal_projection()
        state = str(terminal["state"])
        reason = str(terminal["reason"])
        await self._hud.show_brainstorm(BrainstormHudView(
            screen="agent_terminal",
            state=state,
            topic="Hermes Agent Terminal",
            collaborators=[],
            activity="No terminal sessions are configured on this Mac.",
            terminal_state=state,
            endpoint_host=str(terminal["endpoint_host"]),
            terminal_reason=_agent_terminal_instruction(state, reason),
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

    async def _select_message(self, index: int, trace_id: str) -> None:
        if (
            self._messages is None
            or self._replies is None
            or not 0 <= index <= len(self._message_rows)
        ):
            return
        if index == 0:
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
        if not thread.replyable:
            await self._send_list(HudList(
                items=[
                    "Group replies stay read-only for now.",
                    "Open Messages on the Mac to reply.",
                ],
                title="MESSAGES · READ ONLY",
                hint=BACK_HINT,
            ))
            return
        try:
            page = await asyncio.to_thread(
                # 7 entries = exactly one conversation window on the lens, so
                # a page turn is a screen turn — nothing half-hidden.
                self._messages.thread_message_page, thread, limit=7,
            )
        except (OSError, sqlite3.Error) as exc:
            log.warning("Messages history unavailable: %s", type(exc).__name__)
            if not self._request_is_current():
                return
            self.clear_message_selection("access-denied")
            await self._show_messages_read_failure(exc)
            return
        if not self._request_is_current():
            return
        self._replies.select(thread, trace_id)
        self._message_selection_active = True
        self._message_history = _MessageHistoryState(thread=thread, pages=[page])
        self._message_detail = None
        await self._show_message_history_page()

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
                pages=self._message_reader_pages(entry.text),
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
                        limit=7,
                        before_row_id=min(row_ids),
                    )
                except (OSError, sqlite3.Error) as exc:
                    log.warning("Messages history unavailable: %s", type(exc).__name__)
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
            # Paging is already spelled out in the Mac-authored heading.
            # Keep the footer short enough to show every remaining action.
            hint = "tap read · say reply · left x2: back"
        else:
            page_label = (
                "Older messages — up newer, down older"
                if page.has_older else "Older messages — up for newer"
            )
            hint = "tap read · say reply · left x2: back"
        context = [self._message_history_row(state.thread, entry) for entry in page.entries]
        await self._send_list(HudList(
            items=[page_label, *context,
                'Say “JOAT, reply saying …”',
                "Mic off? Long press to enable.",
            ],
            title="MESSAGES · CONVERSATION",
            hint=hint,
            selectable=bool(page.entries),
        ))

    async def _show_message_detail(self) -> None:
        state = self._message_detail
        if state is None:
            return
        total = len(state.pages)
        if state.index == 0:
            hint = (
                "down more · say reply · left x2: back"
                if total > 1 else "say reply · left x2: back"
            )
        elif state.index + 1 < total:
            hint = "up previous · down more · say reply · left x2: back"
        else:
            hint = "up previous · say reply · left x2: back"
        await self._send_list(HudList(
            items=[
                self._message_detail_metadata(state.thread, state.entry, state.index, total),
                *self._message_reader_pages(
                    state.pages[state.index],
                    _MESSAGE_READER_DISPLAY_LINE_CHARACTERS,
                ),
            ],
            title="MESSAGES · MESSAGE",
            hint=hint,
        ))

    @staticmethod
    def _message_reader_pages(
        text: str,
        maximum_characters: int = _MESSAGE_READER_PAGE_CHARACTERS,
    ) -> tuple[str, ...]:
        """Split text at word boundaries for fixed G2 reader pages or lines."""

        remaining = " ".join(text.split())
        if not remaining:
            return ("No text is available for this message.",)
        pages: list[str] = []
        while remaining:
            if len(remaining) <= maximum_characters:
                pages.append(remaining)
                break
            boundary = remaining.rfind(" ", 0, maximum_characters + 1)
            if boundary <= 0:
                boundary = maximum_characters
            pages.append(remaining[:boundary].rstrip())
            remaining = remaining[boundary:].lstrip()
        return tuple(pages)

    @staticmethod
    def _message_detail_metadata(
        thread: MessageThread,
        entry: MessageEntry,
        page_index: int,
        page_count: int,
    ) -> str:
        role = "To" if entry.from_me else "From"
        sender = "you" if entry.from_me else thread.sender
        details: list[str] = []
        if entry.unread:
            details.append("unread")
        if entry.delivery is not None:
            details.append(entry.delivery)
        if entry.attachment_count:
            details.append("attachment")
        details.append(f"{page_index + 1}/{page_count}")
        return f"{role} {sender} · {' · '.join(details)}"

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
        if not thread.replyable:
            # No " · " inside the label: that token is the primary/secondary
            # split on the lens. The READ ONLY page explains the constraint.
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
    def _message_history_row(thread: MessageThread, entry: MessageEntry) -> str:
        label = "You" if entry.from_me else thread.sender
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
    ) -> None:
        if (
            request_id != self._active_request_id
            or self._canonical_capability(name) != self._active_capability
        ):
            log.info("dropping stale capability selection request=%s", request_id)
            return
        token = self._request_context.set(request_id)
        try:
            match name:
                case "MESSAGES":
                    await self._select_message(index, trace_id)
                case "NOTIFICATIONS":
                    await self._select_notification(index)
                # ``Reminders`` was a first-level name in an older installed
                # HUD.  ``open`` already maps it to the single Planning
                # projection, so its row taps must use that same handler.
                # Otherwise the rendered legacy page looks live but is inert.
                case "PLANNING" | "CALENDAR" | "TASKS" | "REMINDERS":
                    await self._select_planning(index)
                case "QUICKNOTES":
                    await self._select_note(index)
                case "ACTIVITY":
                    await self._select_activity(index)
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
            rows.append(_PlanningRow(
                label=f"{entry.time_label} · {entry.kind.upper()} · {entry.title}",
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

    @staticmethod
    def _planning_zone() -> tzinfo:
        """Resolve an IANA zone when the local OS exposes one.

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

    async def _show_notes(self) -> None:
        """One QuickNotes surface: live recording controls, else all notes.

        Recordings (transcribed + summarized meetings/speeches) and exact
        dictated text notes are one wearer concept; the storage split stays
        invisible. While a capture is live the page IS the recorder's control
        panel — the exclusive microphone means taps are the only controls.
        """
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
            # Transient (finalizing) — render truth without dead actions.
            if self._request_is_current():
                self._note_rows = []
            await self._show_focus(
                "QUICKNOTES · SAVING",
                ["Finishing the recording…"],
            )
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

    async def _select_note(self, index: int) -> None:
        if not 0 <= index < len(self._note_rows):
            return
        row = self._note_rows[index]
        if row.kind == "control":
            await self._apply_recording_control(row.control or "")
            return
        if row.kind == "voice" and row.voice_note is not None:
            await self._show_voice_note_detail(row.voice_note)
            return
        if row.text_note is not None:
            await self._show_focus("QUICKNOTES · DETAIL", [row.text_note.text])

    async def _apply_recording_control(self, action: str) -> None:
        capture = self._voice_capture
        if capture is None:
            return
        receipt: str | None = None
        if action == "stop":
            # Transcription of a long recording takes real time. Replace the
            # controls with an honest progress frame first, so the wearer is
            # not staring at tappable rows that no longer do anything.
            await self._show_focus(
                "QUICKNOTES · SAVING",
                ["Finishing the recording…", "Transcribing on this Mac."],
            )
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
        # Re-render whatever is now true: updated controls, or — after a save
        # or discard — the unified notes list itself.
        await self._show_notes()

    async def _show_voice_note_detail(self, note: VoiceNote) -> None:
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
        rows: list[str] = [f"{title[:60]}", meta]
        insights = store.insights(note.capture_id)
        latest = store.recent_retained(1)
        is_latest = bool(latest) and latest[0].capture_id == note.capture_id
        if insights is None:
            rows.append("Summary is not ready yet.")
            if is_latest:
                rows.append("Say: summarize the last note.")
        else:
            rows.append("SUMMARY")
            rows.extend(_wrap_note_text(insights.summary)[:5])
            if insights.key_points:
                rows.append("KEY POINTS")
                rows.extend(f"– {point[:58]}" for point in insights.key_points[:6])
            if insights.todos:
                rows.append("TODOS")
                # □ reads as an unchecked box: these are the actionable rows,
                # and "create reminders from the last note" consumes them.
                rows.extend(f"□ {todo[:58]}" for todo in insights.todos[:4])
        hint = (
            "say: ask the last note … · left x2: back"
            if is_latest
            else BACK_HINT
        )
        await self._show_focus("QUICKNOTES · NOTE", rows[:20], hint=hint)

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
        await self._show_focus(
            "ACTIVITY · DETAIL",
            [
                self._action_label(item.kind),
                f"State · {item.state.replace('_', ' ').title()}",
                f"Action ref · {item.action_id[:8]}",
            ],
        )

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
    r"tasks|reminders|notifications|activity)$",
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
