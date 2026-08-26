"""HudWriter — the Mac side's one way to put words on the glasses.

This is the first shared seam from the session-model design pass
(docs/SESSION_MODEL.md): every consumer — voice replies, debate turns,
future triage cards — writes through this instead of hand-building
HudText messages. That keeps append/replace semantics, the status bar,
and (later) surface arbitration in exactly one place.
"""

from __future__ import annotations

from ..core.interfaces import GlassesTransport
from ..core.protocol import ContainerId, HudText


class HudWriter:
    """Streaming text surface over a GlassesTransport."""

    def __init__(self, transport: GlassesTransport) -> None:
        self._transport = transport

    async def status(self, text: str) -> None:
        """Status bar line (top 32px)."""
        await self._transport.send(HudText(text=text, container_id=ContainerId.STATUS_BAR))

    async def show(self, text: str, container: ContainerId = ContainerId.MAIN) -> None:
        """Replace the container's content."""
        await self._transport.send(HudText(text=text, container_id=container))

    async def context(self, text: str) -> None:
        """What the wearer last said (docs/HUD_DASHBOARD.md).

        Its own region because the reply used to be streamed over the top of
        it: the proof of being heard was destroyed by the answer to it,
        about 200ms after appearing.
        """
        await self._transport.send(HudText(text=text, container_id=ContainerId.CONTEXT))

    async def tray(self, text: str) -> None:
        """What is waiting — queued cards, counts. Never steals MAIN."""
        await self._transport.send(HudText(text=text, container_id=ContainerId.TRAY))

    async def append(self, chunk: str, container: ContainerId = ContainerId.MAIN) -> None:
        """Stream a chunk onto the container without repainting — the
        primitive behind visible, turn-by-turn debate streaming."""
        await self._transport.send(HudText(text=chunk, container_id=container, append=True))
