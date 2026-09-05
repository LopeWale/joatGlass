"""Typed conversational entry points into existing feature controllers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
import re
import time
from typing import TYPE_CHECKING, Any, Protocol

from ..services.capabilities import voice_request_id
from ..services.agent_terminal import MAX_TERMINAL_INSTRUCTION_CHARS
from ..services.note_commands import NoteCommandController
from ..services.pomodoro import PomodoroService
from ..services.session import (
    BrainstormStartResult, MAX_BRAINSTORM_TOPIC_CHARS, MAX_NAVIGATION_DESTINATION_CHARS,
)
from .tools import Tool

if TYPE_CHECKING:
    from ..persistence.notes import NoteStore
    from ..persistence.voice_notes import VoiceNoteStore


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


class NavigationStarter(Protocol):
    def start_navigation(self, destination: str) -> BrainstormStartResult: ...


class TerminalRosterReader(Protocol):
    def picker_rows(self) -> list[tuple[str, str]]: ...

    def tail(self, name: str | None = None, lines: int = 6) -> str: ...


def _age(unix_us: int, now: float | None = None) -> str:
    elapsed = max(0.0, (now if now is not None else time.time()) - unix_us / 1_000_000)
    if elapsed < 60:
        return "now"
    if elapsed < 3_600:
        return f"{int(elapsed // 60)}m"
    if elapsed < 86_400:
        return f"{int(elapsed // 3_600)}h"
    return f"{int(elapsed // 86_400)}d"


# One note read stays bounded so a long dictation cannot swallow the reply
# loop; the lens reader still shows every character.
NOTE_READ_CHARS = 1_200
TERMINAL_TAIL_LINES = 8


def quicknote_reader_tool(notes: NoteStore, voice_notes: VoiceNoteStore | None = None) -> Tool:
    """The wearer's notes, whole, for the mind to read back or search.

    The audit's Q3: the mind could save a note but never read one, and
    the spoken search returned snippets. This reads the same stores the
    QuickNotes page shows — exact text notes and the transcript segments
    of retained recordings — oldest to newest within each note.
    """

    async def run(arguments: dict[str, Any]) -> str:
        if not isinstance(arguments, dict):
            return "error: read_quicknotes takes an optional query and count"
        query = arguments.get("query")
        if query is not None and not isinstance(query, str):
            return "error: query must be text"
        query = (query or "").strip()
        try:
            count = int(arguments.get("count", 5))
        except (TypeError, ValueError):
            count = 5
        count = max(1, min(count, 10))
        lines: list[str] = []
        text_notes = notes.search(query, limit=count) if query else notes.active()[:count]
        for note in text_notes:
            body = note.text
            if len(body) > NOTE_READ_CHARS:
                body = body[:NOTE_READ_CHARS].rstrip() + "…"
            lines.append(f"[{_age(note.updated_at_us)}] note: {body}")
        if voice_notes is not None:
            needle = query.casefold()
            for recording in voice_notes.recent_retained(count):
                segments = voice_notes.segments(recording.capture_id)
                transcript = " ".join(
                    segment.text.strip() for segment in segments
                    if segment.kind == "transcript" and segment.text.strip()
                )
                title = recording.title or "Untitled"
                if needle and needle not in title.casefold() and needle not in transcript.casefold():
                    continue
                if not transcript:
                    transcript = "(no transcript is available for this recording)"
                elif len(transcript) > NOTE_READ_CHARS:
                    transcript = transcript[:NOTE_READ_CHARS].rstrip() + "…"
                gaps = f", {recording.gap_count} gap(s)" if recording.gap_count else ""
                lines.append(
                    f"[{_age(recording.updated_at_us)}] recording {title!r}{gaps}: {transcript}"
                )
        if not lines:
            return f"no quick notes match {query!r}" if query else "no quick notes yet"
        return "\n".join(lines)

    return Tool(
        name="read_quicknotes",
        description=(
            "Read the wearer's QuickNotes: exact saved text and the transcripts of "
            "their voice recordings, newest first. Call this when they ask what they "
            "noted, to find a note, or to read one back. Optional query filters by words."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words to search for; omit for the newest notes."},
                "count": {"type": "integer", "description": "How many notes, 1 to 10 (default 5)."},
            },
            "additionalProperties": False,
        },
        run=run,
        decision_hints=(
            "note", "notes", "quicknote", "quicknotes", "noted", "wrote down",
            "recording", "recorded", "transcript",
        ),
    )


def terminal_sessions_tool(roster: TerminalRosterReader) -> Tool:
    """What the Agent Terminal sessions are doing, readable by the mind.

    The audit's T2 in part: the roster and each lane's output existed on
    the lens but the mind could only send into a lane blind. This lists the
    configured sessions in the same shape the picker shows and reads one
    lane's recent output lines.
    """

    async def run(arguments: dict[str, Any]) -> str:
        if not isinstance(arguments, dict):
            return "error: terminal_sessions takes an optional lane and lines"
        rows = roster.picker_rows()
        if not rows:
            return "No terminal sessions are configured on this Mac."
        lane = arguments.get("lane")
        if lane is None:
            return "\n".join(row for _name, row in rows)
        names = [name for name, _row in rows]
        if not isinstance(lane, str) or lane not in names:
            return "error: lane must exactly match a configured Terminal session: " + ", ".join(names)
        try:
            lines = int(arguments.get("lines", TERMINAL_TAIL_LINES))
        except (TypeError, ValueError):
            lines = TERMINAL_TAIL_LINES
        lines = max(1, min(lines, 40))
        return roster.tail(lane, lines)

    return Tool(
        name="terminal_sessions",
        description=(
            "List the Agent Terminal sessions with their state, or read the recent "
            "output of one named session. Call this when the wearer asks what a "
            "terminal is doing, what it answered, or which sessions exist, before "
            "send_terminal_instruction if the lane is unclear."
        ),
        parameters={
            "type": "object",
            "properties": {
                "lane": {"type": "string", "description": "A configured session name; omit to list all."},
                "lines": {"type": "integer", "description": "Recent output lines to read, 1 to 40."},
            },
            "additionalProperties": False,
        },
        run=run,
        decision_hints=("terminal", "hermes", "session", "sessions", "openclaw", "codex", "claude code"),
    )


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
    navigation: NavigationStarter | None = None,
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
    started_routes: dict[str, bytes] = {}
    lane_names = tuple(terminal_lanes)

    async def start_navigation(arguments: dict[str, Any]) -> str:
        if not isinstance(arguments, dict) or set(arguments) != {"destination", "operation_id"}:
            return "error: start_navigation requires only destination and operation_id"
        destination, operation_id = arguments["destination"], arguments["operation_id"]
        if (
            not isinstance(destination, str) or not destination.strip() or "\x00" in destination
            or len(destination) > MAX_NAVIGATION_DESTINATION_CHARS
        ):
            return (
                "error: destination must be non-empty text of at most "
                f"{MAX_NAVIGATION_DESTINATION_CHARS} characters without null characters"
            )
        try:
            encoded = destination.encode("utf-8")
        except UnicodeEncodeError:
            return "error: destination must be valid UTF-8 text"
        if not isinstance(operation_id, str) or _OPERATION_ID.fullmatch(operation_id) is None:
            return "error: operation_id must be 1-128 letters, digits, dots, underscores, colons or hyphens, starting with a letter or digit"
        digest = sha256(encoded).digest()
        previous = started_routes.get(operation_id)
        if previous is not None:
            if previous != digest:
                return "error: operation_id was already used for a different destination"
            return "Navigation was already started for this request; no second route was requested."
        if len(started_routes) >= max_brainstorm_operations:
            return "error: navigation retry capacity reached for this assistant process; no route started"
        if navigation is None:
            return "error: navigation is unavailable on this Mac; no route started"
        result = navigation.start_navigation(destination)
        if result is BrainstormStartResult.STARTED:
            started_routes[operation_id] = digest
            return (
                "Navigation started. The glasses now search for that place, ask the wearer "
                "to confirm it and choose walking or cycling, then route from the phone's "
                "current position. Nothing else is needed from you."
            )
        return {
            BrainstormStartResult.BUSY: (
                "error: another session (a Brainstorm or a route) is active; it must finish "
                "or be stopped before a new route"
            ),
            BrainstormStartResult.UNAVAILABLE: "error: navigation is unavailable on this Mac; no route started",
            BrainstormStartResult.CLOSED: "error: the session controller is closed; no route started",
            BrainstormStartResult.INVALID: "error: destination is invalid; no route started",
        }[result]

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
                "Save the wearer's words as a local QuickNote and refresh QuickNotes. "
                "The text is stored verbatim: pass exactly what they said, without "
                "rewording, capitalising, or adding punctuation. "
                "Reuse operation_id only when retrying the same save; keep its text identical. "
                "Retry deduplication lasts for this assistant process."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string", "minLength": 1,
                        "description": (
                            "The note body exactly as the wearer spoke it, including their "
                            "capitalisation, punctuation and whitespace."
                        ),
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
                "Pass the topic verbatim, in the wearer's own words, not rephrased as a question; "
                "this starts the existing collaborators and does not "
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
                        "description": (
                            "The topic exactly as the wearer said it, e.g. the words after "
                            "'brainstorm about', unchanged."
                        ),
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
    if navigation is not None:
        tools.append(Tool(
            name="start_navigation",
            description=(
                "Start walking or cycling navigation to a place the wearer named. Pass the "
                "destination in their exact words (a name or an address); the glasses then "
                "confirm the matched place, ask walk or cycle, and route from the phone's "
                "position. Nothing is needed from you after a successful start. Reuse "
                "operation_id and the identical destination on retries."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "destination": {
                        "type": "string", "minLength": 1,
                        "maxLength": MAX_NAVIGATION_DESTINATION_CHARS,
                        "description": "The place exactly as the wearer said it, e.g. 'the library'.",
                    },
                    "operation_id": {"type": "string", "minLength": 1, "maxLength": 128,
                                     "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$"},
                },
                "required": ["destination", "operation_id"],
                "additionalProperties": False,
            },
            run=start_navigation,
            decision_hints=(
                "navigate", "navigation", "directions", "walk to", "walking", "cycle to",
                "bike to", "route", "take me", "get to", "way to",
            ),
        ))
    return tools
