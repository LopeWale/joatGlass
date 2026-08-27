"""Answers the on-glasses capability menu.

The plugin's menu is navigation only — the DATA lives here. When the wearer
picks a row, the plugin sends an `input` event with containerName "capmenu"
and the row's name; this router draws the view. It also owns the two other
glasses-initiated requests: the contextual menu's "repeat last reply"
(containerName "sysmenu") and the repaint after a menu closes (the rebuild
cleared every region, and a silent Mac means a blank screen).

Deterministic by design: these are reflexes to explicit gestures, not model
decisions.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..channels.cards import CardLog
from ..channels.imessage import IMessageStore
from ..core.protocol import HudList, InputEvent
from ..core.interfaces import GlassesTransport
from .hud import HudWriter
from .status import StatusPublisher

log = logging.getLogger(__name__)


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
    ) -> None:
        self._hud = hud
        self._transport = transport
        self._status = status
        self._messages = messages
        self._cards = cards

    def on_input(self, event: InputEvent) -> None:
        """Transport callback — synchronous, so work is scheduled."""
        if event.container_name == "capmenu" and event.selected_name:
            asyncio.get_running_loop().create_task(self.open(event.selected_name))
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
                await self._hud.show(
                    'Say: "JOAT, debate <topic>" — codex proposes, claude '
                    "critiques, streamed here as it happens.")
                await self._hud.status("JOAT · debate")
            case "Calendar":
                await self._hud.show("Calendar is not connected yet.")
                await self._hud.status("JOAT · calendar")
            case _:
                log.info("unknown capability requested: %s", name)

    async def _show_messages(self) -> None:
        if self._messages is None or not self._messages.available:
            await self._hud.show("Messages needs Full Disk Access on the Mac.")
            return
        rows = await asyncio.to_thread(self._messages.recent, 6)
        if not rows:
            await self._hud.show("No recent messages.")
            await self._hud.status("JOAT · messages")
            return
        await self._transport.send(HudList(
            items=[f"{sender} · {text}" for sender, text, _ in rows]))
        await self._hud.status("JOAT · messages")

    async def _show_notifications(self) -> None:
        entries = self._cards.recent(6)
        if not entries:
            await self._hud.show("Nothing shown recently.")
            await self._hud.status("JOAT · notifications")
            return
        await self._transport.send(HudList(items=[
            f"[{_age(at)}] {card.splitlines()[0]}" for at, card in entries]))
        await self._hud.status("JOAT · notifications")

    async def _repeat(self) -> None:
        last = self._status.get("last_reply")
        await self._hud.show(last if last else "Nothing to repeat yet.")

    async def repaint_home(self) -> None:
        """Refill the regions the menu's rebuild blanked."""
        last_reply = self._status.get("last_reply")
        if last_reply:
            await self._hud.show(last_reply)
        last_heard = self._status.get("last_transcript")
        if last_heard:
            await self._hud.context(f"you  {last_heard}")
