"""Derived QuickNote intelligence: summary, key points, todos, Q&A, actions.

The capture journal (``voice_note_capture``) records evidence; this module
distills it. Distillation is local-first — the resident MLX model summarizes
by default, and the official ``claude`` CLI runs only on an explicit
"with claude" ask (RULES.md #8/#9). Insights are stored as replaceable,
engine-attributed rows, never as capture truth.

Downstream actions stay behind the existing consequential-write boundaries:
todo → Reminder handoff goes through ``ProductivityActionController``'s
confirmed EventKit path one item at a time, and Notes export goes through an
injected exporter so this module never owns AppleScript or process spawning.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..persistence.voice_notes import (
    VoiceNote,
    VoiceNoteError,
    VoiceNoteInsights,
    VoiceNoteStore,
)
from .productivity_actions import (
    ActionOrigin,
    ProductivityActionController,
    ReminderIntent,
)

log = logging.getLogger(__name__)


class TaskCompleter(Protocol):
    """One prompt in, one completion out; None means the engine declined."""

    async def complete(
        self, *, system: str, text: str, max_tokens: int = 700
    ) -> str | None: ...


class NoteExporter(Protocol):
    """Deliver one rendered note document; returns a wearer-visible receipt."""

    async def export(self, *, title: str, body_lines: tuple[str, ...]) -> str: ...


# A transcript longer than this is folded into partial summaries first. The
# local 3B context fits this comfortably; folding keeps very long meetings
# honest instead of silently truncating them.
_MAX_DIRECT_CHARS = 20_000
_MAX_FOLD_CHUNKS = 8
_ASK_TAIL_CHARS = 18_000
_MAX_REMINDER_HANDOFF = 8

_INSIGHTS_SYSTEM = (
    "You distill a recorded speech or meeting transcript. Reply in EXACTLY "
    "this plain-text shape, with these three headers:\n"
    "SUMMARY:\n"
    "3 to 6 sentences capturing what was discussed and decided.\n"
    "KEY POINTS:\n"
    "- one point per line, 2 to 8 lines\n"
    "TODOS:\n"
    "- one actionable task per line, at most 8 lines; write '- none' if the "
    "transcript contains no tasks\n"
    "No markdown emphasis, no extra sections, no commentary, no preamble."
)

_FOLD_SYSTEM = (
    "Condense this portion of a longer meeting transcript into at most 120 "
    "words of plain prose. Keep decisions, owners, numbers, and dates. No "
    "lists, no commentary."
)

_ASK_SYSTEM = (
    "You answer one question about a recorded note. Use ONLY the note "
    "content provided. If the answer is not in it, say the note does not "
    "cover that. Answer in under 60 words of plain text."
)

_NOTE_NOUN = r"(?:voice note|quick ?note|note)"
_SUMMARIZE = re.compile(
    rf"^(?:re)?summari[sz]e the last {_NOTE_NOUN}(?P<claude>,? with claude)?$",
    re.IGNORECASE,
)
_RESUMMARIZE_CLAUDE = re.compile(
    r"^resummari[sz]e with claude$", re.IGNORECASE
)
_ASK = re.compile(
    rf"^ask the (?:last )?{_NOTE_NOUN}[,:]? (?P<question>.+)$", re.IGNORECASE
)
_REMINDERS = re.compile(
    rf"^create reminders from the last {_NOTE_NOUN}$", re.IGNORECASE
)
_EXPORT_NOTES = re.compile(
    rf"^(?:send|save) the last {_NOTE_NOUN} to (?:my )?notes$", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class ParsedInsights:
    summary: str
    key_points: tuple[str, ...]
    todos: tuple[str, ...]


def parse_insights(raw: str) -> ParsedInsights | None:
    """Tolerant section parser for the fixed SUMMARY/KEY POINTS/TODOS shape.

    Models decorate headers unpredictably (case, bold markers, trailing
    colons). Parse by header word, strip bullet glyphs, and refuse only when
    no usable summary text exists — a wrong summary is worse than a retry.
    """
    sections: dict[str, list[str]] = {"summary": [], "key points": [], "todos": []}
    current: str | None = None
    for line in raw.splitlines():
        text = line.strip()
        header = re.sub(r"[*_#:\s]+$", "", re.sub(r"^[*_#\s]+", "", text)).casefold()
        if header in sections and (
            text.rstrip().endswith(":") or header == text.casefold() or len(text) <= len(header) + 4
        ):
            current = header
            remainder = text[text.casefold().find(header) + len(header):]
            remainder = remainder.lstrip(":*_# ").strip()
            if remainder:
                sections[header].append(remainder)
            continue
        if current is not None and text:
            sections[current].append(text)
    summary = " ".join(" ".join(sections["summary"]).split())
    if not summary:
        return None

    def bullets(lines: list[str]) -> tuple[str, ...]:
        cleaned: list[str] = []
        for line in lines:
            item = re.sub(r"^[-•*•\d.)\s]+", "", line).strip()
            if not item or item.casefold() in {"none", "none."}:
                continue
            cleaned.append(item[:500])
        return tuple(cleaned[:12])

    return ParsedInsights(
        summary=summary[:4_000],
        key_points=bullets(sections["key points"]),
        todos=bullets(sections["todos"]),
    )


class NoteInsightsService:
    """Distill and interrogate retained notes with explicit engine choice."""

    def __init__(
        self,
        notes: VoiceNoteStore,
        local: TaskCompleter | None,
        *,
        claude: TaskCompleter | None = None,
        clock_us: Callable[[], int] | None = None,
    ) -> None:
        self._notes = notes
        self._local = local
        self._claude = claude
        self._clock_us = clock_us or (lambda: time.time_ns() // 1_000)
        self._summarizing: set[str] = set()

    @property
    def claude_available(self) -> bool:
        return self._claude is not None

    def latest_retained(self) -> VoiceNote | None:
        notes = self._notes.recent_retained(1)
        return notes[0] if notes else None

    def insights(self, capture_id: str) -> VoiceNoteInsights | None:
        return self._notes.insights(capture_id)

    def transcript_text(self, capture_id: str) -> str:
        """Join transcript sections with honest gap markers, in order."""
        parts: list[str] = []
        for segment in self._notes.segments(capture_id):
            if segment.kind == "transcript":
                parts.append(segment.text)
            elif segment.kind == "gap":
                parts.append("[recording gap]")
        return "\n".join(parts)

    def schedule_summarize(self, capture_id: str) -> None:
        """Fire-and-forget local summarization after a retained save."""

        async def run() -> None:
            receipt = await self.summarize(capture_id, engine="local")
            log.info("auto-summary: %s", receipt)

        try:
            task = asyncio.get_running_loop().create_task(run())
            task.add_done_callback(_log_task_failure)
        except RuntimeError:
            # No running loop (synchronous tests): summarize on demand later.
            log.debug("no running loop; summary deferred to explicit command")

    async def summarize_last(self, *, engine: str = "local") -> str:
        note = self.latest_retained()
        if note is None:
            return "There is no retained note to summarize."
        return await self.summarize(note.capture_id, engine=engine)

    async def summarize(self, capture_id: str, *, engine: str = "local") -> str:
        if engine == "claude":
            completer, engine_name = self._claude, "claude-cli"
            if completer is None:
                return "Claude is not available on this Mac."
        else:
            completer, engine_name = self._local, "mlx-local"
            if completer is None:
                return "The local summarizer is not available on this Mac."
        try:
            note = self._notes.get(capture_id)
        except VoiceNoteError:
            return "That note no longer exists."
        title = note.title or "Untitled"
        if note.transcribed_segments < 1:
            return f"“{title}” has no transcript to summarize."
        if capture_id in self._summarizing:
            return f"“{title}” is already being summarized. One moment."
        transcript = self.transcript_text(capture_id)
        if not transcript.strip():
            return f"“{title}” has no transcript to summarize."
        self._summarizing.add(capture_id)
        try:
            parsed = await self._distill(completer, transcript)
        finally:
            self._summarizing.discard(capture_id)
        if parsed is None:
            return (
                "The summarizer returned an unusable answer. "
                "Say: summarize the last note, to retry."
            )
        try:
            stored = self._notes.replace_insights(
                capture_id,
                engine=engine_name,
                summary=parsed.summary,
                key_points=parsed.key_points,
                todos=parsed.todos,
                now_us=self._clock_us(),
            )
        except (VoiceNoteError, ValueError):
            log.exception("storing insights failed for %s", capture_id)
            return "The summary could not be stored. Say: summarize the last note, to retry."
        points = len(stored.key_points)
        todos = len(stored.todos)
        return (
            f"Summarized “{title}”: {points} key point{'s' if points != 1 else ''}, "
            f"{todos} todo{'s' if todos != 1 else ''}. Open QuickNotes to read it."
        )

    async def ask_last(self, question: str) -> str:
        note = self.latest_retained()
        if note is None:
            return "There is no retained note to ask about."
        completer = self._local
        if completer is None:
            return "The local answer engine is not available on this Mac."
        cleaned = " ".join(question.split())
        if not cleaned:
            return "Say the question after: ask the last note."
        title = note.title or "Untitled"
        transcript = self.transcript_text(note.capture_id)
        if not transcript.strip():
            return f"“{title}” has no transcript to ask about."
        context_parts: list[str] = [f"NOTE TITLE: {title}"]
        stored = self._notes.insights(note.capture_id)
        if stored is not None:
            context_parts.append(f"SUMMARY: {stored.summary}")
            if stored.key_points:
                context_parts.append(
                    "KEY POINTS: " + " · ".join(stored.key_points)
                )
        if len(transcript) > _ASK_TAIL_CHARS:
            context_parts.append(
                "TRANSCRIPT (earlier content omitted):\n"
                + transcript[-_ASK_TAIL_CHARS:]
            )
        else:
            context_parts.append(f"TRANSCRIPT:\n{transcript}")
        answer = await completer.complete(
            system=_ASK_SYSTEM,
            text="\n\n".join(context_parts) + f"\n\nQUESTION: {cleaned}",
            max_tokens=200,
        )
        if answer is None or not answer.strip():
            return "The answer engine did not respond. Try again."
        return " ".join(answer.split())

    async def _distill(
        self, completer: TaskCompleter, transcript: str
    ) -> ParsedInsights | None:
        if len(transcript) <= _MAX_DIRECT_CHARS:
            raw = await completer.complete(
                system=_INSIGHTS_SYSTEM, text=transcript, max_tokens=700
            )
            return parse_insights(raw) if raw else None
        # Fold long recordings: condense bounded chunks, then distill the
        # condensed sections. Beyond the fold cap the remainder is omitted
        # and the omission is stated to the model rather than hidden.
        chunks = [
            transcript[start:start + _MAX_DIRECT_CHARS]
            for start in range(0, len(transcript), _MAX_DIRECT_CHARS)
        ]
        omitted = len(chunks) > _MAX_FOLD_CHUNKS
        partials: list[str] = []
        for chunk in chunks[:_MAX_FOLD_CHUNKS]:
            partial = await completer.complete(
                system=_FOLD_SYSTEM, text=chunk, max_tokens=250
            )
            if partial:
                partials.append(" ".join(partial.split()))
        if not partials:
            return None
        combined = "\n\n".join(
            f"Condensed section {index + 1}: {text}"
            for index, text in enumerate(partials)
        )
        if omitted:
            combined += (
                "\n\n(The recording continued beyond these sections; the "
                "remainder was not condensed.)"
            )
        raw = await completer.complete(
            system=_INSIGHTS_SYSTEM, text=combined, max_tokens=700
        )
        return parse_insights(raw) if raw else None


def _log_task_failure(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("auto-summary task failed", exc_info=task.exception())


class NoteInsightCommandController:
    """Exact spoken commands over the last retained note."""

    def __init__(
        self,
        insights: NoteInsightsService,
        *,
        productivity: ProductivityActionController | None = None,
        exporter: NoteExporter | None = None,
        local_time_zone: Callable[[], str | None] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._insights = insights
        self._productivity = productivity
        self._exporter = exporter
        self._local_time_zone = local_time_zone or (lambda: None)
        self._now = now or (lambda: datetime.now().astimezone())

    async def handle_command(self, text: str) -> str | None:
        normalized = " ".join(text.strip().split())
        summarize = _SUMMARIZE.fullmatch(normalized)
        if summarize is not None:
            engine = "claude" if summarize.group("claude") else "local"
            return await self._insights.summarize_last(engine=engine)
        if _RESUMMARIZE_CLAUDE.fullmatch(normalized) is not None:
            return await self._insights.summarize_last(engine="claude")
        ask = _ASK.fullmatch(normalized)
        if ask is not None:
            return await self._insights.ask_last(ask.group("question"))
        if _REMINDERS.fullmatch(normalized) is not None:
            return await self._create_reminders()
        if _EXPORT_NOTES.fullmatch(normalized) is not None:
            return await self._export_to_notes()
        return None

    async def _create_reminders(self) -> str:
        if self._productivity is None:
            return "Reminders are unavailable on this Mac."
        note = self._insights.latest_retained()
        if note is None:
            return "There is no retained note to create reminders from."
        title = note.title or "Untitled"
        stored = self._insights.insights(note.capture_id)
        if stored is None:
            return f"“{title}” has no summary yet. Say: summarize the last note, first."
        todos = stored.todos[:_MAX_REMINDER_HANDOFF]
        if not todos:
            return f"“{title}” has no todos to create reminders from."
        zone = self._local_time_zone()
        if zone is None:
            return "The Mac's time zone is unavailable, so reminders cannot be scheduled."
        try:
            local_now = self._now().astimezone(ZoneInfo(zone))
        except ZoneInfoNotFoundError:
            return "The Mac's time zone is unavailable, so reminders cannot be scheduled."
        due_date = (local_now + timedelta(days=1)).strftime("%Y-%m-%d")
        created = 0
        for ordinal, todo in enumerate(todos):
            receipt = await self._productivity.create_reminder(
                ReminderIntent(
                    title=todo[:200],
                    date=due_date,
                    time="09:00",
                    time_zone=zone,
                ),
                origin=ActionOrigin(
                    kind="voice_note.todo",
                    identifier=note.capture_id,
                    ordinal=ordinal,
                ),
            )
            if receipt.startswith("Created Reminder:"):
                created += 1
        total = len(todos)
        return (
            f"Created {created} of {total} reminder{'s' if total != 1 else ''} "
            f"from “{title}”, due tomorrow 09:00. Adjust times in Planning."
        )

    async def _export_to_notes(self) -> str:
        if self._exporter is None:
            return "Notes export is unavailable on this Mac."
        note = self._insights.latest_retained()
        if note is None:
            return "There is no retained note to send to Notes."
        title = note.title or "Untitled"
        stored = self._insights.insights(note.capture_id)
        transcript = self._insights.transcript_text(note.capture_id)
        lines: list[str] = []
        if stored is not None:
            lines.append("Summary")
            lines.append(stored.summary)
            if stored.key_points:
                lines.append("")
                lines.append("Key points")
                lines.extend(f"• {point}" for point in stored.key_points)
            if stored.todos:
                lines.append("")
                lines.append("Todos")
                lines.extend(f"• {todo}" for todo in stored.todos)
            lines.append("")
        if note.state != "saved":
            lines.append("Transcript (incomplete — recording had gaps)")
        else:
            lines.append("Transcript")
        lines.extend(transcript.splitlines() or ["(no transcript)"])
        return await self._exporter.export(title=title, body_lines=tuple(lines))


__all__ = [
    "NoteExporter",
    "NoteInsightCommandController",
    "NoteInsightsService",
    "ParsedInsights",
    "TaskCompleter",
    "parse_insights",
]
