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
import sqlite3
import time
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone, tzinfo
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..channels.cards import CardLog
from ..channels.imessage import IMessageStore, MessageThread
from ..core.protocol import BrainstormHudView, HudList, InputEvent, PlanningGridCell
from ..core.interfaces import GlassesTransport
from ..native.bridge import (
    NativeBridgeClient,
    NativeBridgeError,
    NativeBridgeProtocolError,
    NativeBridgeUnavailable,
)
from ..persistence.actions import ActionActivity, ActionJournal
from ..persistence.notes import NoteStore, QuickNote
from .hud import HudWriter
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

BACK_HINT = "left x2 overview"
DETAIL_HINT = "tap detail · left x2 overview"
PLANNING_HINT = "tap choose · swipe move · left x2 overview"
MESSAGES_ACCESS_ITEMS = [
    "Tap to retry after granting Full Disk Access.",
    "Mac Settings · Privacy & Security · Full Disk Access",
]


@dataclass(frozen=True, slots=True)
class _PlanningRow:
    """One deliberate drill-down action in the native Planning projection."""

    label: str
    action: Literal["scope", "previous", "next", "week", "day", "entry"]
    scope: PlanningScope | None = None
    day: date | None = None
    entry: PlanningEntry | None = None


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
        actions: ActionJournal | None = None,
    ) -> None:
        self._hud = hud
        self._transport = transport
        self._status = status
        self._messages = messages
        self._cards = cards
        self._replies = replies
        self._personal_data = personal_data
        self._notes = notes
        self._actions = actions
        self._message_rows: list[MessageThread] = []
        self._notification_rows: list[tuple[float, str]] = []
        # Calendar and Reminders have different EventKit stores, but a wearer
        # navigates time, not stores. Keep one bounded read-only projection and
        # the action rows that produced its current visible page.
        self._planning_selected_day = datetime.now().astimezone().date()
        # The first wearable page is the calendar itself. A wearer chooses a
        # week, then a day, instead of first having to navigate past a control
        # panel before seeing their month.
        self._planning_scope = PlanningScope.MONTH
        self._planning_rows: list[_PlanningRow] = []
        self._note_rows: list[QuickNote] = []
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
        elif event.container_name == "sysmenu" and event.selected_index == 1:
            asyncio.get_running_loop().create_task(self._repeat())
        elif (
            event.container_name == "capability-lifecycle"
            and event.selected_name == "messages-home"
        ):
            self.clear_message_selection("home")
        elif event.container_name == "menu" and event.selected_name == "closed":
            self.clear_message_selection("home")
            asyncio.get_running_loop().create_task(self.repaint_home())

    def clear_message_selection(self, reason: str) -> None:
        """Disarm a private reply target when its wearer-visible context ends."""
        self._message_rows = []
        if self._replies is not None:
            self._replies.clear_selection(reason)

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
        """Open the actual visual round-table surface from the glasses menu."""
        terminal = self._status.agent_terminal_projection()
        await self._hud.show_brainstorm(BrainstormHudView(
            screen="brainstorm",
            state="ready",
            topic="Ready for a question",
            collaborators=["Codex", "Claude", "Hermes"],
            activity='Say “JOAT, brainstorm …” to begin.',
            terminal_state=str(terminal["state"]),
            endpoint_host=str(terminal["endpoint_host"]),
            terminal_reason=str(terminal["reason"]),
            no_tools_checked=bool(terminal["no_tools_checked"]),
        ))

    async def _show_agent_terminal(self) -> None:
        """Expose Hermes readiness without leaking an endpoint credential or chat."""
        terminal = self._status.agent_terminal_projection()
        state = str(terminal["state"])
        await self._hud.show_brainstorm(BrainstormHudView(
            screen="agent_terminal",
            state=state,
            topic="Hermes Agent Terminal",
            collaborators=[],
            activity="Hermes participates as a Brainstorm collaborator.",
            terminal_state=state,
            endpoint_host=str(terminal["endpoint_host"]),
            terminal_reason=str(terminal["reason"]),
            no_tools_checked=bool(terminal["no_tools_checked"]),
        ))

    async def _show_messages(self) -> None:
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
        except (OSError, sqlite3.Error) as exc:
            log.warning("Messages capability unavailable: %s", type(exc).__name__)
            if self._request_is_current():
                self._message_rows = []
            await self._show_focus(
                "MESSAGES · ACCESS",
                MESSAGES_ACCESS_ITEMS,
            )
            return
        if not self._request_is_current():
            return
        self._message_rows = rows
        if not rows:
            await self._show_focus("MESSAGES · EMPTY", ["No recent messages."])
            return
        await self._send_list(HudList(
            items=[
                f"[{_age(row.at)}] {row.sender}"
                f"{' (group · read only)' if not row.replyable else ''} · {row.text}"
                for row in rows
            ],
            title="MESSAGES",
            hint="tap select · left x2 overview",
            selectable=True,
        ))

    async def _select_message(self, index: int, trace_id: str) -> None:
        if (
            self._messages is None
            or self._replies is None
            or not 0 <= index < len(self._message_rows)
        ):
            return
        thread = self._message_rows[index]
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
            history = await asyncio.to_thread(
                self._messages.thread_messages, thread, 3,
            )
        except (OSError, sqlite3.Error) as exc:
            log.warning("Messages history unavailable: %s", type(exc).__name__)
            if not self._request_is_current():
                return
            self.clear_message_selection("access-denied")
            await self._show_focus("MESSAGES · ACCESS", MESSAGES_ACCESS_ITEMS)
            return
        if not self._request_is_current():
            return
        self._replies.select(thread, trace_id)
        context = [
            f"{'You' if entry.from_me else thread.sender} · {entry.text}"
            for entry in history
        ] or [f"{thread.sender} · {thread.text}"]
        await self._send_list(HudList(
            items=[*context,
                'Say “JOAT, reply saying …”',
                "Mic off? Long press to enable.",
            ],
            title="MESSAGES · CONVERSATION",
            hint="voice reply · left x2 overview",
        ))

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
                case "PLANNING" | "CALENDAR" | "TASKS":
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
            items=[f"[{_age(at)}] {card.splitlines()[0]}" for at, card in entries],
            title="NOTIFICATIONS",
            hint=DETAIL_HINT,
            selectable=True,
        ))

    async def _select_notification(self, index: int) -> None:
        if not 0 <= index < len(self._notification_rows):
            return
        _, card = self._notification_rows[index]
        await self._show_focus(
            "NOTIFICATIONS · DETAIL",
            card.splitlines() or ["Notification content unavailable."],
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
        if self._notes is None:
            if self._request_is_current():
                self._note_rows = []
            await self._show_focus(
                "QUICKNOTES · SETUP",
                ['Say “JOAT, note …” to save exact text.'],
            )
            return
        note_rows = list(self._notes.active()[:5])
        if not self._request_is_current():
            return
        self._note_rows = note_rows
        rows = [f"[{_age(note.updated_at_us / 1_000_000)}] {note.text}" for note in self._note_rows]
        await self._show_focus(
            "QUICKNOTES" if rows else "QUICKNOTES · EMPTY",
            rows or ['Say “JOAT, note …” to save exact text.'],
            hint=DETAIL_HINT if rows else BACK_HINT,
            selectable=bool(rows),
        )

    async def _select_note(self, index: int) -> None:
        if not 0 <= index < len(self._note_rows):
            return
        await self._show_focus("QUICKNOTES · DETAIL", [self._note_rows[index].text])

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
                "I’ll confirm the exact place before starting.",
            ],
            hint="say destination · left x2 overview",
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
