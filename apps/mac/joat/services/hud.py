"""HudWriter — the Mac side's one way to put words on the glasses.

This is the first shared seam from the session-model design pass
(docs/SESSION_MODEL.md): every consumer — voice replies, debate turns,
future triage cards — writes through this instead of hand-building
HudText messages. That keeps append/replace semantics, the status bar,
and (later) surface arbitration in exactly one place.
"""

from __future__ import annotations

from ..core.interfaces import GlassesTransport
from ..core.protocol import (
    AgentTerminalHudView,
    AssistantOverlay,
    BrainstormHudView,
    ContainerId,
    HudRebuild,
    HudText,
)


class HudWriter:
    """Streaming text surface over a GlassesTransport."""

    def __init__(self, transport: GlassesTransport) -> None:
        self._transport = transport

    async def begin_brainstorm(self) -> None:
        """Release any menu focus before the first live Brainstorm frame."""
        await self._transport.send(HudRebuild(reason="brainstorm"))

    async def show_agent_terminal(self, view: AgentTerminalHudView) -> None:
        """Paint the Agent Terminal, which is its own surface and its own
        frame — not a `screen` variant of the Brainstorm one."""
        await self._transport.send(view)

    async def show_brainstorm(self, view: BrainstormHudView) -> None:
        """Paint the dedicated production Brainstorm / Hermes surface."""
        await self._transport.send(view)

    async def status(self, text: str) -> None:
        """Status bar line (top 32px)."""
        await self._transport.send(HudText(text=text, container_id=ContainerId.STATUS_BAR))

    async def assistant(self, text: str, *, append: bool = False) -> None:
        """Show JOAT above the active feature without replacing its surface."""
        await self._transport.send(AssistantOverlay(
            text=text,
            operation="append" if append else "replace",
        ))

    async def settle_assistant(self, dismiss_after_ms: int = 6_000) -> None:
        """Leave the completed answer readable briefly, then reveal the same view."""
        await self._transport.send(AssistantOverlay(
            operation="settle",
            dismiss_after_ms=dismiss_after_ms,
        ))

    async def show(
        self,
        text: str,
        container: ContainerId = ContainerId.MAIN,
        *,
        content_kind: str | None = "system",
        trace_id: str | None = None,
    ) -> None:
        """Replace the container's content."""
        await self._transport.send(HudText(
            text=text,
            container_id=container,
            content_kind=content_kind,  # type: ignore[arg-type]
            trace_id=trace_id,
        ))

    async def show_with_brightness(
        self,
        text: str,
        level: int,
        container: ContainerId = ContainerId.MAIN,
        *,
        content_kind: str | None = "assistant",
        trace_id: str | None = None,
    ) -> None:
        """Replace content at an explicit firmware brightness (0-4)."""
        await self._transport.send(
            HudText(
                text=text,
                container_id=container,
                text_color=level,
                content_kind=content_kind,  # type: ignore[arg-type]
                trace_id=trace_id,
            ))

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
