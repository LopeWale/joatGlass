"""HudWriter — the Mac side's one way to put words on the glasses.

This is the first shared seam from the session-model design pass
(docs/SESSION_MODEL.md): every consumer — voice replies, debate turns,
future triage cards — writes through this instead of hand-building
HudText messages. That keeps append/replace semantics, the status bar,
and (later) surface arbitration in exactly one place.
"""

from __future__ import annotations

from collections.abc import Callable
import math
import time
from typing import Literal, Protocol, cast, runtime_checkable

from ..core.interfaces import GlassesTransport
from ..core.protocol import (
    AgentTerminalHudView,
    AssistantOverlay,
    BrainstormHudView,
    ContainerId,
    HudRebuild,
    HudText,
    PromptAck,
    PromptPresent,
)
from .message_prompts import ActivatePrompt, PromptPresentationAck
from .prompted_input import PromptTicket


@runtime_checkable
class _PromptTransport(Protocol):
    async def request_prompt(
        self, request: PromptPresent, *, on_presented: Callable[[PromptAck], bool],
        on_revoked: Callable[[], None] | None = None, timeout_s: float = 5.0,
    ) -> bool: ...

    async def cancel_prompt(self, prompt_id: str | None = None) -> bool: ...


class HudWriter:
    """Streaming text surface over a GlassesTransport."""

    def __init__(self, transport: GlassesTransport) -> None:
        self._transport = transport

    async def publish_prompt(
        self, ticket: PromptTicket, question: str, activate: ActivatePrompt, *,
        fence_audio: Callable[[], int], revoke: Callable[[str], object],
    ) -> bool:
        """Arm only inside an exact authenticated presentation receipt."""
        transport = self._transport
        if not isinstance(transport, _PromptTransport):
            return False
        remaining_ms = math.floor((ticket.expires_at - time.monotonic()) * 1000)
        if remaining_ms <= 0 or ticket.source not in ("glasses", "phone"):
            return False
        if ticket.owner_kind not in ("messages", "brainstorm", "terminal"):
            return False
        request = PromptPresent(
            prompt_id=ticket.prompt_id, question=question,
            presentation_binding=ticket.presentation_binding,
            connection_generation=ticket.connection_generation,
            source="glasses" if ticket.source == "glasses" else "phone",
            expires_in_ms=min(45_000, remaining_ms),
            owner_kind=cast(Literal["messages", "brainstorm", "terminal"], ticket.owner_kind),
            owner_id=ticket.owner_id, field=ticket.field,
        )

        def presented(ack: PromptAck) -> bool:
            # No task/future boundary between this fence and owner activation:
            # the next WebSocket PCM callback must already see the new lease.
            return activate(PromptPresentationAck(
                prompt_id=ack.prompt_id, success=ack.success,
                presentation_binding=ack.presentation_binding,
                connection_generation=ack.connection_generation,
                audio_epoch=ack.audio_epoch or "", ingress_fence=fence_audio(),
            ))

        def retired() -> None:
            revoke(ticket.prompt_id)

        return await transport.request_prompt(
            request, on_presented=presented, on_revoked=retired,
            # Leave cleanup room inside the bridge's five-second publication
            # budget. The question's separate expiry never renews on receipt.
            timeout_s=min(4.5, remaining_ms / 1000),
        )

    async def cancel_prompt(self, prompt_id: str) -> bool:
        transport = self._transport
        return await transport.cancel_prompt(prompt_id) if isinstance(transport, _PromptTransport) else False

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

    async def assistant(
        self, text: str, *, append: bool = False, answers_wearer: bool = False,
    ) -> None:
        """Show JOAT above the active feature without replacing its surface."""
        await self._transport.send(AssistantOverlay(
            text=text,
            operation="append" if append else "replace",
            answers_wearer=answers_wearer,
        ))

    @staticmethod
    def reading_ms(text: str) -> int:
        """How long to leave an answer up, from how long it takes to READ.

        Six seconds was a constant, applied to every answer whatever its
        length, and it is not an SDK or firmware limit — @evenrealities/
        even_hub_sdk 0.0.14 has no dismiss contract at all; the number was
        ours. Measured against six real replies on the wearer's glasses,
        five of them were taken off the lens before an average reader could
        finish, and the two longest before a FAST reader could.

        Brysbaert (2019), "How many words do we read per minute?" — 190
        studies, 18,573 participants — puts English non-fiction silent
        reading at 238 wpm, most adults between 175 and 300. This uses the
        SLOW end of that range, because the cost of holding an answer a
        second too long is nothing and the cost of taking it away early is
        the wearer losing the answer entirely.

        Clamped to the 1-15s the plugin accepts (HudPresenter validates the
        range), with the old six seconds as the floor so nothing that fits
        today gets shorter.
        """
        words = len(text) / 5.5  # ~5.5 chars/word incl. space, non-fiction
        needed_ms = int(words / 175 * 60_000) + 1_500  # + a beat to look up
        return max(6_000, min(15_000, needed_ms))

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
