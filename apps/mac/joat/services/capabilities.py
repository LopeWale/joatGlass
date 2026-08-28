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
import time
from contextvars import ContextVar
from dataclasses import replace
from datetime import datetime
from typing import Any

from ..channels.cards import CardLog
from ..channels.imessage import IMessageStore, MessageThread
from ..core.protocol import HudList, InputEvent
from ..core.interfaces import GlassesTransport
from ..native.bridge import NativeBridgeClient, NativeBridgeError, NativeBridgeUnavailable
from ..persistence.actions import ActionActivity, ActionJournal
from ..persistence.notes import NoteStore, QuickNote
from .hud import HudWriter
from .status import StatusPublisher
from .message_reply import MessageReplyController

log = logging.getLogger(__name__)

BACK_HINT = "left x2 overview"
DETAIL_HINT = "tap detail · left x2 overview"


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
        self._calendar_rows: list[dict[str, Any]] = []
        self._task_rows: list[dict[str, Any]] = []
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
                self.open(event.selected_name, request_id=event.request_id)
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
        elif event.container_name == "menu" and event.selected_name == "closed":
            asyncio.get_running_loop().create_task(self.repaint_home())

    async def open(self, name: str, *, request_id: str | None = None) -> None:
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
                    await self._show_focus(
                        "BRAINSTORM",
                        [
                            'Say: "JOAT, brainstorm <question>"',
                            "Ready collaborators answer · you route turns",
                        ],
                    )
                case "Calendar":
                    await self._show_calendar()
                case "Tasks" | "Reminders":
                    await self._show_tasks()
                case "QuickNotes":
                    await self._show_notes()
                case "Activity":
                    await self._show_activity()
                case _:
                    log.info("unknown capability requested: %s", name)
        finally:
            self._request_context.reset(token)

    async def _show_messages(self) -> None:
        if self._messages is None or not self._messages.available:
            if self._request_is_current():
                self._message_rows = []
            await self._show_focus(
                "MESSAGES · SETUP",
                ["Messages needs Full Disk Access on the Mac."],
            )
            return
        rows = await asyncio.to_thread(self._messages.recent_threads, 5)
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
        if self._replies is None or not 0 <= index < len(self._message_rows):
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
        self._replies.select(thread, trace_id)
        await self._send_list(HudList(
            items=[
                f"{thread.sender} · {thread.text}",
                'Say “JOAT, reply saying …”',
            ],
            title="MESSAGES · REPLY",
            hint="voice draft · left x2 overview",
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
                case "CALENDAR":
                    await self._select_calendar(index)
                case "TASKS":
                    await self._select_task(index)
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

    async def _show_calendar(self) -> None:
        client = self._personal_data
        if client is None:
            if self._request_is_current():
                self._calendar_rows = []
            await self._show_focus(
                "CALENDAR · SETUP",
                ["Open JOAT on the Mac to connect Calendar."],
            )
            return
        try:
            status = await client.call("authorization.status")
            if status.authorization.calendar != "full_access":
                await self._show_focus(
                    "CALENDAR · SETUP",
                    ["Open JOAT on the Mac and connect Calendar."],
                )
                return
            containers = await client.call("calendar.listContainers")
            calendars = containers.result if isinstance(containers.result, list) else []
            identifiers = [
                item["calendar_identifier"]
                for item in calendars
                if isinstance(item, dict) and isinstance(item.get("calendar_identifier"), str)
            ]
            now_ms = int(time.time() * 1000)
            upcoming = await client.call(
                "calendar.query",
                {
                    "start_unix_ms": now_ms,
                    "end_unix_ms": now_ms + 7 * 24 * 60 * 60 * 1000,
                    "calendar_identifiers": identifiers,
                    "limit": 5,
                },
            )
            events = upcoming.result if isinstance(upcoming.result, list) else []
            calendar_rows = [item for item in events if isinstance(item, dict)]
            if not self._request_is_current():
                return
            self._calendar_rows = calendar_rows
            rows = [self._calendar_row(item) for item in calendar_rows]
            await self._show_focus(
                "CALENDAR · 7 DAYS" if rows else "CALENDAR · EMPTY",
                rows or ["No upcoming events."],
                hint=DETAIL_HINT if rows else BACK_HINT,
                selectable=bool(rows),
            )
        except (NativeBridgeUnavailable, NativeBridgeError):
            await self._show_focus(
                "CALENDAR · UNAVAILABLE",
                ["Check Calendar access in JOAT on the Mac."],
            )

    async def _show_tasks(self) -> None:
        client = self._personal_data
        if client is None:
            if self._request_is_current():
                self._task_rows = []
            await self._show_focus(
                "TASKS · SETUP",
                ["Open JOAT on the Mac to connect Reminders."],
            )
            return
        try:
            status = await client.call("authorization.status")
            if status.authorization.reminders != "full_access":
                await self._show_focus(
                    "TASKS · SETUP",
                    ["Open JOAT on the Mac and connect Reminders."],
                )
                return
            containers = await client.call("reminders.listContainers")
            lists = containers.result if isinstance(containers.result, list) else []
            identifiers = [
                item["calendar_identifier"]
                for item in lists
                if isinstance(item, dict) and isinstance(item.get("calendar_identifier"), str)
            ]
            pending = await client.call(
                "reminders.query",
                {
                    "calendar_identifiers": identifiers,
                    "include_completed": False,
                    "limit": 5,
                },
            )
            reminders = pending.result if isinstance(pending.result, list) else []
            task_rows = [item for item in reminders if isinstance(item, dict)]
            if not self._request_is_current():
                return
            self._task_rows = task_rows
            rows = [
                self._task_row(item)
                for item in task_rows
            ]
            await self._show_focus(
                "TASKS" if rows else "TASKS · EMPTY",
                rows or ["No incomplete reminders."],
                hint=DETAIL_HINT if rows else BACK_HINT,
                selectable=bool(rows),
            )
        except (NativeBridgeUnavailable, NativeBridgeError):
            await self._show_focus(
                "TASKS · UNAVAILABLE",
                ["Check Reminders access in JOAT on the Mac."],
            )

    @staticmethod
    def _calendar_row(item: dict[str, Any]) -> str:
        title = str(item.get("title") or "Untitled event")
        start = item.get("start_unix_ms")
        if not isinstance(start, (int, float)):
            return title
        when = datetime.fromtimestamp(start / 1000).astimezone().strftime("%a %H:%M")
        return f"{title} · {when}"

    @staticmethod
    def _task_row(item: dict[str, Any]) -> str:
        title = str(item.get("title") or "Untitled reminder")
        due = item.get("due")
        if not isinstance(due, dict):
            return title
        fields = tuple(due.get(key) for key in ("year", "month", "day", "hour", "minute"))
        if not all(isinstance(value, int) for value in fields):
            return title
        year, month, day, hour, minute = fields
        return f"{title} · Due {year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}"

    async def _select_calendar(self, index: int) -> None:
        if not 0 <= index < len(self._calendar_rows):
            return
        item = self._calendar_rows[index]
        title = str(item.get("title") or "Untitled event")
        start = item.get("start_unix_ms")
        end = item.get("end_unix_ms")
        rows = [title]
        if isinstance(start, (int, float)):
            rows.append(datetime.fromtimestamp(start / 1000).astimezone().strftime("Starts %a %b %-d · %H:%M"))
        if isinstance(end, (int, float)):
            rows.append(datetime.fromtimestamp(end / 1000).astimezone().strftime("Ends %a %b %-d · %H:%M"))
        await self._show_focus("CALENDAR · DETAIL", rows)

    async def _select_task(self, index: int) -> None:
        if not 0 <= index < len(self._task_rows):
            return
        item = self._task_rows[index]
        rows = [str(item.get("title") or "Untitled reminder")]
        rendered = self._task_row(item)
        if " · " in rendered:
            rows.append(rendered.split(" · ", 1)[1])
        rows.append("Complete by voice: JOAT, complete reminder …")
        await self._show_focus("TASKS · DETAIL", rows)

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
    ) -> None:
        """Render non-dashboard capability states in the shared focus shell."""
        await self._send_list(HudList(
            items=items,
            title=title,
            hint=hint,
            selectable=selectable,
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
        await self._hud.show(last if last else "Nothing to repeat yet.")

    async def repaint_home(self) -> None:
        """Compatibility path for older plugins that did not buffer Home."""
        last_reply = self._status.get("last_reply")
        if last_reply:
            await self._hud.show(last_reply)
        last_heard = self._status.get("last_transcript")
        if last_heard:
            await self._hud.context(f"you  {last_heard}")
