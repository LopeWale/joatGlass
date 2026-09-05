"""An explicit Terminal visit owns its preparation and subsequent HUD frames."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hashlib import sha256

from .agent_terminal import (
    MAX_TERMINAL_INSTRUCTION_CHARS,
    AgentTerminalRoster,
    TerminalFrame,
    TerminalInstructionResult,
)
from .capabilities import voice_request_id

OpeningPaint = Callable[[TerminalFrame, str | None, str | None], Awaitable[None]]
InstructionVisit = Callable[[str, str], Awaitable[bool]]
MAX_INSTRUCTION_OPERATIONS = 256


@dataclass(frozen=True)
class _Opening:
    name: str
    request_id: str | None
    selection_id: str
    connection: str | None
    focus_generation: int
    is_current: Callable[[], bool]


@dataclass(frozen=True)
class _LegacyOpening:
    connection: str | None
    is_current: Callable[[], bool]
    request_id: str | None = None
    selection_id: str | None = None


@dataclass(slots=True)
class _InstructionOperation:
    digest: bytes
    accepted: bool = False


class TerminalOpeningController:
    """Prepare only the chosen lane; retire its paint rights when context moves."""

    def __init__(
        self, roster: AgentTerminalRoster, *, paint: OpeningPaint,
        connection_generation: Callable[[], str | None],
    ) -> None:
        self._roster = roster
        self._paint = paint
        self._connection_generation = connection_generation
        self._opening: _Opening | None = None
        self._legacy: _LegacyOpening | None = None
        self._instruction_operations: dict[str, _InstructionOperation] = {}

    def _is_current(self, opening: _Opening) -> bool:
        return (
            self._opening is opening
            and opening.is_current()
            and opening.connection is not None
            and opening.connection == self._connection_generation()
            and opening.name == self._roster.focused
            and opening.focus_generation == self._roster.focus_generation
        )

    def _legacy_is_current(self, legacy: _LegacyOpening) -> bool:
        return (self._legacy is legacy and legacy.is_current()
                and legacy.connection is not None
                and legacy.connection == self._connection_generation())

    def claim_legacy(self, claim: Callable[[str | None, str | None], Callable[[], bool]]) -> None:
        """Only an explicit recognized command may acquire this legacy view.

        Its frame stays unbound on the old wire, so this is a host navigation
        guard, not the selected-picker frame delivery guarantee.
        """
        opening, legacy = self._opening, self._legacy
        request_id = selection_id = None
        if opening is not None and self._is_current(opening):
            request_id, selection_id = opening.request_id, opening.selection_id
        elif legacy is not None and self._legacy_is_current(legacy):
            request_id, selection_id = legacy.request_id, legacy.selection_id
        is_current = claim(request_id, selection_id)
        self._opening = None
        self._legacy = _LegacyOpening(
            self._connection_generation(), is_current, request_id, selection_id,
        )

    @property
    def current(self) -> bool:
        opening = self._opening
        if opening is not None:
            return self._is_current(opening)
        legacy = self._legacy
        return legacy is not None and self._legacy_is_current(legacy)

    async def submit_instruction(
        self, name: str, text: str, operation_id: str, open_visit: InstructionVisit,
    ) -> str:
        """Prepare one explicit visit, then admit its exact instruction once.

        The bounded retry ledger includes pending preparations. Accepted IDs
        survive caller cancellation and view departure, but not process exit.
        This returns local admission; the roster owns remote results.
        """
        if not isinstance(name, str) or name not in self._roster.names():
            return "error: unknown Terminal lane; no instruction submitted"
        if (
            not isinstance(text, str) or not text.strip() or "\x00" in text
            or len(text) > MAX_TERMINAL_INSTRUCTION_CHARS
        ):
            return "error: invalid Terminal instruction; no instruction submitted"
        if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 128:
            return "error: invalid Terminal operation_id; no instruction submitted"
        try:
            digest = sha256(name.encode("utf-8") + b"\x00" + text.encode("utf-8")).digest()
        except UnicodeEncodeError:
            return "error: invalid Terminal instruction encoding; no instruction submitted"
        previous = self._instruction_operations.get(operation_id)
        if previous is not None:
            if previous.digest != digest:
                return "error: operation_id was already used for a different lane or instruction"
            if previous.accepted:
                return (
                    "Terminal instruction was already accepted for this request; no new submission. "
                    "This receipt does not report its current status."
                )
            return "Terminal instruction preparation is already in progress; no additional submission launched."
        if len(self._instruction_operations) >= MAX_INSTRUCTION_OPERATIONS:
            return "error: Terminal retry capacity reached for this process; no instruction submitted"

        operation = _InstructionOperation(digest)
        self._instruction_operations[operation_id] = operation
        try:
            generation = self._connection_generation()
            if generation is None:
                return "error: Terminal connection is unavailable; no instruction submitted"
            request_id = voice_request_id()
            prepared = await open_visit(name, request_id)
            # A callback suppressing cancellation must not turn an expired
            # tool call into a send. There is no await after this boundary.
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise asyncio.CancelledError
            opening = self._opening
            if (
                prepared is not True or opening is None or opening.name != name
                or opening.request_id != request_id or opening.selection_id != request_id
                or opening.connection != generation or not self._is_current(opening)
            ):
                return "error: Terminal visit changed or did not open; no instruction submitted"
            result = self._roster.admit_instruction(name, text)
            if result is TerminalInstructionResult.ACCEPTED:
                # Commit the receipt in the same event-loop turn as admission.
                # A later timeout/retry cannot repeat the roster's owned task.
                operation.accepted = True
                return f"Terminal instruction accepted for {name}. Follow Agent Terminal for its result."
            return {
                TerminalInstructionResult.INVALID: "error: invalid Terminal instruction; no instruction submitted",
                TerminalInstructionResult.WRONG_FOCUS: "error: Terminal focus changed; no instruction submitted",
                TerminalInstructionResult.UNAVAILABLE: "error: Terminal context is unavailable; no instruction submitted",
                TerminalInstructionResult.BUSY: "error: Terminal is busy; existing input was preserved",
                TerminalInstructionResult.APPROVAL_REQUIRED: "error: Terminal is awaiting approval; no instruction submitted",
                TerminalInstructionResult.CONTEXT_CHANGED: "error: Terminal context changed; no instruction submitted",
            }[result]
        except asyncio.CancelledError:
            raise
        except Exception:
            # Neither arbitrary provider errors nor instruction text belong
            # in a tool failure receipt. Failed preparations remain retryable.
            return "error: Terminal preparation failed; no instruction submitted"
        finally:
            if not operation.accepted and self._instruction_operations.get(operation_id) is operation:
                del self._instruction_operations[operation_id]

    async def open(
        self, name: str, request_id: str | None, selection_id: str,
        is_current: Callable[[], bool],
    ) -> None:
        # Focus and ownership are bound before the first await. Preparation
        # never changes focus after a slower initialize finishes.
        if not is_current() or not self._roster.focus(name):
            return
        opening = _Opening(
            name, request_id, selection_id, self._connection_generation(),
            self._roster.focus_generation, is_current,
        )
        self._opening = opening
        self._legacy = None

        async def paint(frame: TerminalFrame) -> None:
            if self._is_current(opening):
                await self._paint(
                    frame, opening.request_id,
                    opening.selection_id if opening.request_id is not None else None,
                )

        if self._is_current(opening):
            await self._roster.prepare_lane(
                name, is_current=lambda: self._is_current(opening), paint=paint,
            )

    async def paint(self, frame: TerminalFrame) -> None:
        opening = self._opening
        if opening is None:
            legacy = self._legacy
            if legacy is None:
                await self._paint(frame, None, None)
            elif self._legacy_is_current(legacy):
                await self._paint(frame, legacy.request_id, legacy.selection_id)
        elif self._is_current(opening):
            await self._paint(
                frame, opening.request_id,
                opening.selection_id if opening.request_id is not None else None,
            )

    @property
    def owns_opening(self) -> bool:
        return self._opening is not None or self._legacy is not None

    def retry(self) -> Awaitable[bool]:
        opening, legacy = self._opening, self._legacy

        async def run() -> bool:
            if opening is None:
                if legacy is not None:
                    name = self._roster.focused
                    if name is not None and self._legacy_is_current(legacy):
                        await self.open(name, legacy.request_id, legacy.selection_id or "voice", legacy.is_current)
                    return True
                return self.owns_opening
            if self._is_current(opening):
                await self.open(
                    opening.name, opening.request_id, opening.selection_id,
                    opening.is_current,
                )
            return True

        return run()

    def cycle(self) -> Awaitable[str]:
        opening, legacy = self._opening, self._legacy

        async def run() -> str:
            if opening is None:
                if legacy is not None:
                    if not self._legacy_is_current(legacy):
                        return ""
                    receipt = await self._roster.focus_next()
                    return receipt if self._legacy_is_current(legacy) else ""
                return await self._roster.focus_next() if not self.owns_opening else ""
            if not self._is_current(opening):
                return ""
            names = [name for name, _row in self._roster.picker_rows()]
            if len(names) < 2:
                return f"{opening.name} is the only session."
            target = names[(names.index(opening.name) + 1) % len(names)]
            await self.open(target, opening.request_id, opening.selection_id, opening.is_current)
            return self._roster.frame().topic if self.current else ""

        return run()
