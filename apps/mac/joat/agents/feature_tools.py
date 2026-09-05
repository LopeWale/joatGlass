"""Typed conversational entry points into existing feature controllers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import re
from typing import Any, Protocol

from ..services.capabilities import voice_request_id
from ..services.agent_terminal import MAX_TERMINAL_INSTRUCTION_CHARS
from ..services.note_commands import NoteCommandController
from ..services.pomodoro import PomodoroService
from ..services.session import BrainstormStartResult, MAX_BRAINSTORM_TOPIC_CHARS
from .tools import Tool


class FeatureName(StrEnum):
    MESSAGES = "messages"
    NOTIFICATIONS = "notifications"
    BRAINSTORM = "brainstorm"
    AGENT_TERMINAL = "agent_terminal"
    PLANNING = "planning"
    NAVIGATION = "navigation"
    QUICKNOTES = "quicknotes"
    ACTIVITY = "activity"
    NEWS = "news"
    TIMER = "timer"
    SEARCH = "search"


class TimerAction(StrEnum):
    START = "start"
    PAUSE = "pause"
    RESUME = "resume"
    SKIP = "skip"
    RESET = "reset"
    STATUS = "status"


_FEATURE_TARGETS: dict[FeatureName, str] = {
    FeatureName.MESSAGES: "Messages",
    FeatureName.NOTIFICATIONS: "Notifications",
    FeatureName.BRAINSTORM: "Brainstorm",
    FeatureName.AGENT_TERMINAL: "Agent Terminal",
    FeatureName.PLANNING: "Planning",
    FeatureName.NAVIGATION: "Navigation",
    FeatureName.QUICKNOTES: "QuickNotes",
    FeatureName.ACTIVITY: "Activity",
    FeatureName.NEWS: "News",
    FeatureName.TIMER: "Pomodoro",
    FeatureName.SEARCH: "Search",
}
_OPERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


@dataclass(frozen=True, slots=True)
class _SavedNote:
    text_digest: bytes
    receipt: str


class FeatureOpener(Protocol):
    async def open(self, name: str, *, request_id: str | None = None) -> None: ...


class PanelActivator(Protocol):
    async def activate(self, tab: str) -> None: ...


class BrainstormStarter(Protocol):
    def start_brainstorm(self, topic: str) -> BrainstormStartResult: ...


def feature_tools(
    *,
    capabilities: FeatureOpener,
    notes: NoteCommandController,
    pomodoro: PomodoroService,
    panels: PanelActivator,
    max_note_operations: int = 256,
    brainstorm: BrainstormStarter | None = None,
    max_brainstorm_operations: int = 256,
    terminal: Callable[[str, str, str], Awaitable[str]] | None = None,
    terminal_lanes: Sequence[str] = (),
) -> list[Tool]:
    """Compose one adapter instance for the lifetime of the assistant's belt.

    Note retry identities survive tool cancellation, not process restarts.
    The bounded ledger never evicts an identity: once full, new saves fail
    explicitly while known retries remain safe.
    """
    if type(max_note_operations) is not int or max_note_operations < 1:
        raise ValueError("max_note_operations must be a positive integer")
    if type(max_brainstorm_operations) is not int or max_brainstorm_operations < 1:
        raise ValueError("max_brainstorm_operations must be a positive integer")
    saved_notes: dict[str, _SavedNote] = {}
    started_brainstorms: dict[str, bytes] = {}
    lane_names = tuple(terminal_lanes)

    async def send_terminal_instruction(arguments: dict[str, Any]) -> str:
        if not isinstance(arguments, dict) or set(arguments) != {"lane", "instruction", "operation_id"}:
            return "error: send_terminal_instruction requires only lane, instruction and operation_id"
        lane, instruction, operation_id = (arguments[key] for key in ("lane", "instruction", "operation_id"))
        if not isinstance(lane, str) or lane not in lane_names:
            return "error: lane must exactly match a configured Terminal session"
        if (not isinstance(instruction, str) or not instruction.strip() or "\x00" in instruction
                or len(instruction) > MAX_TERMINAL_INSTRUCTION_CHARS):
            return f"error: instruction must be non-empty text of at most {MAX_TERMINAL_INSTRUCTION_CHARS} characters without null characters"
        try:
            instruction.encode("utf-8")
        except UnicodeEncodeError:
            return "error: instruction must be valid UTF-8 text"
        if not isinstance(operation_id, str) or _OPERATION_ID.fullmatch(operation_id) is None:
            return "error: operation_id must be 1-128 letters, digits, dots, underscores, colons or hyphens, starting with a letter or digit"
        if terminal is None:
            return "error: Terminal is unavailable; no instruction submitted"
        return await terminal(lane, instruction, operation_id)

    async def start_brainstorm(arguments: dict[str, Any]) -> str:
        if not isinstance(arguments, dict) or set(arguments) != {"topic", "operation_id"}:
            return "error: start_brainstorm requires only topic and operation_id"
        topic, operation_id = arguments["topic"], arguments["operation_id"]
        if (
            not isinstance(topic, str) or not topic.strip() or "\x00" in topic
            or len(topic) > MAX_BRAINSTORM_TOPIC_CHARS
        ):
            return f"error: topic must be non-empty text of at most {MAX_BRAINSTORM_TOPIC_CHARS} characters without null characters"
        try:
            encoded = topic.encode("utf-8")
        except UnicodeEncodeError:
            return "error: topic must be valid UTF-8 text"
        if not isinstance(operation_id, str) or _OPERATION_ID.fullmatch(operation_id) is None:
            return "error: operation_id must be 1-128 letters, digits, dots, underscores, colons or hyphens, starting with a letter or digit"
        digest = sha256(encoded).digest()
        previous = started_brainstorms.get(operation_id)
        if previous is not None:
            if previous != digest:
                return "error: operation_id was already used for a different topic"
            return (
                "Brainstorm was already started for this request; no new session launched. "
                "This receipt does not report its current status."
            )
        if len(started_brainstorms) >= max_brainstorm_operations:
            return "error: Brainstorm retry capacity reached for this assistant process; no session started"
        if brainstorm is None:
            return "error: Brainstorm is unavailable; no session started"
        result = brainstorm.start_brainstorm(topic)
        if result is BrainstormStartResult.STARTED:
            # Admission and ledger insertion are synchronous. Tool timeout or
            # simultaneous retries cannot launch a second collaborator session.
            started_brainstorms[operation_id] = digest
            return "Brainstorm started with the supplied topic. Follow the Brainstorm view for collaborator responses."
        return {
            BrainstormStartResult.BUSY: "error: another session is active; no new Brainstorm started",
            BrainstormStartResult.UNAVAILABLE: "error: Brainstorm collaborators are unavailable; no session started",
            BrainstormStartResult.CLOSED: "error: the session controller is closed; no Brainstorm started",
            BrainstormStartResult.INVALID: "error: Brainstorm topic is invalid; no session started",
        }[result]

    async def open_feature(arguments: dict[str, Any]) -> str:
        if not isinstance(arguments, dict) or set(arguments) != {"feature"}:
            return "error: open_feature requires only feature"
        raw_feature = arguments["feature"]
        if not isinstance(raw_feature, str):
            return "error: feature must be a supported feature name"
        try:
            feature = FeatureName(raw_feature)
        except ValueError:
            return "error: feature must be a supported feature name"
        target = _FEATURE_TARGETS[feature]
        await capabilities.open(target, request_id=voice_request_id())
        return f"Opening {target}."

    async def create_quicknote(arguments: dict[str, Any]) -> str:
        if not isinstance(arguments, dict) or set(arguments) != {"text", "operation_id"}:
            return "error: create_quicknote requires only text and operation_id"
        text, operation_id = arguments["text"], arguments["operation_id"]
        if not isinstance(text, str) or not text.strip() or "\x00" in text:
            return "error: text must be non-empty UTF-8 text without null characters"
        try:
            encoded = text.encode("utf-8")
        except UnicodeEncodeError:
            return "error: text must be valid UTF-8 text"
        if not isinstance(operation_id, str) or _OPERATION_ID.fullmatch(operation_id) is None:
            return (
                "error: operation_id must be 1-128 letters, digits, dots, underscores, "
                "colons or hyphens, starting with a letter or digit"
            )
        digest = sha256(encoded).digest()
        saved = saved_notes.get(operation_id)
        if saved is not None:
            if saved.text_digest != digest:
                return "error: operation_id was already used for different text"
        else:
            if len(saved_notes) >= max_note_operations:
                return "error: QuickNote retry capacity reached for this assistant process; no note saved"
            receipt = notes.save_text(text)
            # No await may separate the synchronous save and ledger entry.
            # A timeout/cancel during refresh must leave retries able to reuse
            # the committed result rather than creating another note.
            saved = saved_notes[operation_id] = _SavedNote(digest, receipt)
        try:
            await capabilities.open("QuickNotes", request_id=voice_request_id())
        except Exception:
            return f"{saved.receipt} (QuickNotes display refresh failed.)"
        return saved.receipt

    timer_operations = {
        TimerAction.START: pomodoro.start,
        TimerAction.PAUSE: pomodoro.pause,
        TimerAction.RESUME: pomodoro.resume,
        TimerAction.SKIP: pomodoro.skip,
        TimerAction.RESET: pomodoro.reset,
        TimerAction.STATUS: pomodoro.status_text,
    }

    async def control_timer(arguments: dict[str, Any]) -> str:
        if not isinstance(arguments, dict) or set(arguments) != {"action"}:
            return "error: control_timer requires only action"
        raw_action = arguments["action"]
        if not isinstance(raw_action, str):
            return "error: action must be a supported timer action"
        try:
            action = TimerAction(raw_action)
        except ValueError:
            return "error: action must be a supported timer action"
        result = timer_operations[action]()
        try:
            await panels.activate("pomodoro")
        except Exception:
            return f"{result} (Timer display refresh failed.)"
        return result

    tools = [
        Tool(
            name="open_feature",
            description=(
                "Open an existing JOAT feature on the glasses. Opening Brainstorm only "
                "shows its view; use start_brainstorm to submit a supplied topic."
                if brainstorm is not None else "Open an existing JOAT feature on the glasses."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "feature": {"type": "string", "enum": [v.value for v in FeatureName]},
                },
                "required": ["feature"],
                "additionalProperties": False,
            },
            run=open_feature,
            # These hints only decide whether the local reply engine needs
            # its extra tool-choice pass. They are deliberately heuristic;
            # the MCP registry always publishes every tool above.
            decision_hints=(
                "open", "bring up", "messages", "message", "texts", "notifications",
                "brainstorm", "debate", "agent terminal", "terminal", "planning",
                "navigation", "quicknote", "quicknotes", "quick note", "quick notes",
                "notes", "activity", "news", "pomodoro", "timer", "search",
            ),
        ),
        Tool(
            name="create_quicknote",
            description=(
                "Save the exact supplied text as a local QuickNote and refresh QuickNotes. "
                "Reuse operation_id only when retrying the same save; keep its text identical. "
                "Retry deduplication lasts for this assistant process."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string", "minLength": 1,
                        "description": "The exact note body, including punctuation and whitespace.",
                    },
                    "operation_id": {
                        "type": "string", "minLength": 1, "maxLength": 128,
                        "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$",
                        "description": "A unique ID for this save. Reuse the ID and text on retries.",
                    },
                },
                "required": ["text", "operation_id"],
                "additionalProperties": False,
            },
            run=create_quicknote,
            decision_hints=(
                "note", "notes", "quicknote", "quicknotes", "keep this", "save this",
                "write this down", "remember this", "jot",
            ),
        ),
        Tool(
            name="control_timer",
            description=(
                "Start, pause, resume, skip, reset, or check the existing Pomodoro timer. "
                "Uses its configured focus and break lengths and shows the timer panel."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": [v.value for v in TimerAction]},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            run=control_timer,
            decision_hints=(
                "timer", "pomodoro", "focus session", "focus block", "focus time",
                "start focus", "break", "pause", "resume", "skip", "reset",
            ),
        ),
    ]
    if brainstorm is not None:
        tools.append(Tool(
            name="start_brainstorm",
            description=(
                "Start a collaborative Brainstorm on the user's supplied topic. "
                "Pass the exact topic; this starts the existing collaborators and does not "
                "replace an active session. The receipt confirms admission, not completed answers. "
                "Reuse operation_id and identical topic on retries. Deduplication lasts for "
                "this assistant process. If no topic is supplied, ask for it or open the Brainstorm view."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string", "minLength": 1,
                        "maxLength": MAX_BRAINSTORM_TOPIC_CHARS,
                        "description": "The exact question or topic the user wants collaborators to consider.",
                    },
                    "operation_id": {
                        "type": "string", "minLength": 1, "maxLength": 128,
                        "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$",
                        "description": "A unique ID for this start; reuse the ID and exact topic on retries.",
                    },
                },
                "required": ["topic", "operation_id"],
                "additionalProperties": False,
            },
            run=start_brainstorm,
            decision_hints=("brainstorm", "debate", "collaborators", "think together"),
        ))
    if terminal is not None and lane_names:
        tools.append(Tool(
            name="send_terminal_instruction",
            description=(
                "Open an explicitly named Agent Terminal session and submit the user's exact instruction. "
                "Keeps the configured session permissions and approval requirements. Busy or unready sessions "
                "are refused without replacing their work. The receipt confirms admission, not a completed "
                "answer. Reuse operation_id and identical lane/instruction on retries; deduplication lasts "
                "for this assistant process. If the intended lane is unclear, ask which one."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "lane": {"type": "string", "enum": list(lane_names)},
                    "instruction": {"type": "string", "minLength": 1, "maxLength": MAX_TERMINAL_INSTRUCTION_CHARS},
                    "operation_id": {"type": "string", "minLength": 1, "maxLength": 128,
                                     "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$"},
                },
                "required": ["lane", "instruction", "operation_id"],
                "additionalProperties": False,
            },
            run=send_terminal_instruction,
            decision_hints=("terminal", "hermes"),
        ))
    return tools
