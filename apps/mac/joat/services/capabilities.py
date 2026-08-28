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

from ..channels.cards import CardLog
from ..channels.imessage import IMessageStore, MessageThread
from ..core.protocol import HudList, InputEvent
from ..core.interfaces import GlassesTransport
from .hud import HudWriter
from .status import StatusPublisher
from .message_reply import MessageReplyController

log = logging.getLogger(__name__)

BACK_HINT = "left x2 overview"


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
    ) -> None:
        self._hud = hud
        self._transport = transport
        self._status = status
        self._messages = messages
        self._cards = cards
        self._replies = replies
        self._message_rows: list[MessageThread] = []

    def on_input(self, event: InputEvent) -> None:
        """Transport callback — synchronous, so work is scheduled."""
        if event.container_name == "capmenu" and event.selected_name:
            asyncio.get_running_loop().create_task(self.open(event.selected_name))
        elif (event.container_name == "capdata"
              and event.selected_name == "MESSAGES"
              and event.selected_index is not None):
            asyncio.get_running_loop().create_task(self._select_message(
                event.selected_index,
                event.trace_id or "hud",
            ))
        elif event.container_name == "sysmenu" and event.selected_index == 1:
            asyncio.get_running_loop().create_task(self._repeat())
        elif event.container_name == "menu" and event.selected_name == "closed":
            asyncio.get_running_loop().create_task(self.repaint_home())

    async def open(self, name: str) -> None:
        match name:
            case "Messages":
                await self._show_messages()
            case "Notifications":
                await self._show_notifications()
            case "Debate":
                await self._show_focus(
                    "DEBATE",
                    [
                        'Say: "JOAT, debate <topic>"',
                        "Codex proposes · Claude critiques · JOAT judges",
                    ],
                )
            case "Calendar":
                await self._show_focus("CALENDAR", ["Calendar is not connected yet."])
            case _:
                log.info("unknown capability requested: %s", name)

    async def _show_messages(self) -> None:
        if self._messages is None or not self._messages.available:
            await self._show_focus(
                "MESSAGES · SETUP",
                ["Messages needs Full Disk Access on the Mac."],
            )
            return
        rows = await asyncio.to_thread(self._messages.recent_threads, 5)
        self._message_rows = rows
        if not rows:
            await self._show_focus("MESSAGES", ["No recent messages."])
            return
        await self._transport.send(HudList(
            items=[
                f"[{_age(row.at)}] {row.sender}"
                f"{' (group · read only)' if not row.replyable else ''} · {row.text}"
                for row in rows
            ],
            title="MESSAGES",
            hint="tap select · left x2 overview",
        ))

    async def _select_message(self, index: int, trace_id: str) -> None:
        if self._replies is None or not 0 <= index < len(self._message_rows):
            return
        thread = self._message_rows[index]
        if not thread.replyable:
            await self._transport.send(HudList(
                items=[
                    "Group replies stay read-only for now.",
                    "Open Messages on the Mac to reply.",
                ],
                title="MESSAGES · READ ONLY",
                hint=BACK_HINT,
            ))
            return
        self._replies.select(thread, trace_id)
        await self._transport.send(HudList(
            items=[
                f"{thread.sender} · {thread.text}",
                'Say “JOAT, reply saying …”',
            ],
            title="MESSAGES · REPLY",
            hint="voice draft · left x2 overview",
        ))

    async def _show_notifications(self) -> None:
        entries = self._cards.recent(5)
        if not entries:
            await self._show_focus("NOTIFICATIONS", ["Nothing shown recently."])
            return
        await self._transport.send(HudList(
            items=[f"[{_age(at)}] {card.splitlines()[0]}" for at, card in entries],
            title="NOTIFICATIONS",
            hint=BACK_HINT,
        ))

    async def _show_focus(self, title: str, items: list[str]) -> None:
        """Render non-dashboard capability states in the shared focus shell."""
        await self._transport.send(HudList(items=items, title=title, hint=BACK_HINT))

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
