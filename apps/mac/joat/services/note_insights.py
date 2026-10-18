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
import hashlib
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as clock_time
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
from .commitments import Commitment, extract_commitments
from .notes_sync import voice_document
from .voice_note_capture import DEFAULT_TITLE

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
_COMMITMENT_CACHE_NOTES = 16
#: Shown the whole list of reminders a note would add; True = go on to the
#: one confirmation, False = the wearer left, None = no answer in time.
ReminderReview = Callable[[str, tuple[str, ...]], Awaitable[bool | None]]
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_INSIGHTS_SYSTEM = (
    "You distill the transcript of a recorded conversation, meeting, speech or "
    "dictated note. Reply in EXACTLY this plain-text shape, with "
    "these four headers:\n"
    "TITLE:\n"
    "2 to 6 words naming what it is about\n"
    "OVERVIEW:\n"
    "2 to 5 sentences capturing what was said, discussed and decided.\n"
    "KEY POINTS:\n"
    "- one point per line, 1 to 8 lines\n"
    "NEXT STEPS:\n"
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
    #: A short generated title; "" when the model gave none.
    title: str = ""


#: Header words the model may use, mapped to the section they fill. The
#: FEAT-002 prompt asks for TITLE / OVERVIEW / KEY POINTS / NEXT STEPS; the
#: older SUMMARY / TODOS shape still parses.
_HEADERS = {
    "title": "title",
    "summary": "summary",
    "overview": "summary",
    "key points": "key points",
    "todos": "todos",
    "next steps": "todos",
}


def parse_insights(raw: str) -> ParsedInsights | None:
    """Tolerant section parser for the fixed TITLE/OVERVIEW/KEY POINTS/NEXT STEPS shape.

    Models decorate headers unpredictably (case, bold markers, trailing
    colons). Parse by header word, strip bullet glyphs, and refuse only when
    no usable summary text exists — a wrong summary is worse than a retry.
    """
    sections: dict[str, list[str]] = {
        "title": [], "summary": [], "key points": [], "todos": [],
    }
    current: str | None = None
    for line in raw.splitlines():
        text = line.strip()
        undecorated = re.sub(r"^[*_#\s]+", "", text)
        lowered = undecorated.casefold()
        word: str | None = None
        remainder = ""
        for candidate in _HEADERS:
            if not lowered.startswith(candidate):
                continue
            tail = undecorated[len(candidate):]
            if re.fullmatch(r"[*_#:\s]*", tail):
                # A header alone on its line ("## Summary:", "**KEY POINTS**").
                word = candidate
                break
            inline = re.match(r"[*_\s]*:[*_\s]*", tail)
            if inline is not None:
                # A header with its content after the colon ("TITLE: Launch").
                word = candidate
                remainder = tail[inline.end():].strip()
                break
        if word is not None:
            current = _HEADERS[word]
            if remainder:
                sections[current].append(remainder)
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
        title=clean_title(" ".join(sections["title"])),
    )


def clean_title(raw: str) -> str:
    """A generated title fit for a list row and an Apple Note: one line,
    no quotes or trailing punctuation, at most 80 characters."""
    text = " ".join(raw.split()).strip("\"'“”‘’*_#").strip()
    text = text.rstrip(".:;,!").strip()
    if len(text) > 80:
        text = text[:80].rsplit(" ", 1)[0] or text[:80]
    return text


class NoteInsightsService:
    """Distill and interrogate retained notes with explicit engine choice."""

    def __init__(
        self,
        notes: VoiceNoteStore,
        local: TaskCompleter | None,
        *,
        claude: TaskCompleter | None = None,
        clock_us: Callable[[], int] | None = None,
        after_summary: Callable[[str], Awaitable[object]] | None = None,
    ) -> None:
        self._notes = notes
        self._local = local
        self._claude = claude
        self._clock_us = clock_us or (lambda: time.time_ns() // 1_000)
        self._summarizing: set[str] = set()
        # What the wearer said they will do, per note and transcript.
        self._commitment_cache: dict[str, tuple[str, tuple[Commitment, ...]]] = {}
        self._commitment_lock = asyncio.Lock()
        # What follows the automatic summary of a just-saved note, whether
        # or not the summary worked (FEAT-002: the Apple Notes copy).
        self._after_summary = after_summary

    @property
    def claude_available(self) -> bool:
        return self._claude is not None

    @property
    def store(self) -> VoiceNoteStore:
        return self._notes

    def note(self, capture_id: str) -> VoiceNote:
        return self._notes.get(capture_id)

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

    async def commitments(self, capture_id: str) -> tuple[Commitment, ...] | None:
        """What the wearer said they will do in a note (services/commitments.py).

        None when the local model cannot be asked or did not answer. Found once
        per transcript and kept in memory: the model is greedy, so asking again
        would give the same answer, and a restart simply asks again.
        """
        completer = self._local
        if completer is None:
            return None
        try:
            self._notes.get(capture_id)
        except VoiceNoteError:
            return None
        transcript = self.transcript_text(capture_id)
        if not transcript.strip():
            return ()
        digest = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
        async with self._commitment_lock:
            cached = self._commitment_cache.get(capture_id)
            if cached is not None and cached[0] == digest:
                return cached[1]
            found = await extract_commitments(completer, transcript)
            if found is None:
                return None
            self._commitment_cache.pop(capture_id, None)
            self._commitment_cache[capture_id] = (digest, found)
            while len(self._commitment_cache) > _COMMITMENT_CACHE_NOTES:
                self._commitment_cache.pop(next(iter(self._commitment_cache)))
            return found

    def schedule_summarize(self, capture_id: str) -> None:
        """Fire-and-forget local summarization after a retained save."""

        async def run() -> None:
            receipt = await self.summarize(capture_id, engine="local")
            log.info("auto-summary: %s", receipt)
            if self._after_summary is not None:
                try:
                    await self._after_summary(capture_id)
                except Exception:
                    log.exception("post-summary step failed for a saved note")

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
        if parsed.title and DEFAULT_TITLE.fullmatch(title):
            # A tapped start is titled by the clock until there is something
            # better; a title the wearer spoke is never replaced.
            try:
                title = self._notes.rename(
                    capture_id, parsed.title, now_us=self._clock_us(),
                ).title or title
            except (VoiceNoteError, ValueError):
                log.warning("generated title was not applied")
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


def _due_date(day: str | None, note_day: date, now: datetime) -> date:
    """The day a spoken commitment is due, at 09:00.

    The day the wearer named wins ("tomorrow", a weekday after the note's day);
    otherwise the note's own day. A 09:00 that has already gone by cannot
    alert, so it moves to the first 09:00 still ahead.
    """
    base = note_day
    if day == "tomorrow":
        base = note_day + timedelta(days=1)
    elif day in _WEEKDAYS:
        base = note_day + timedelta(days=(_WEEKDAYS[day] - note_day.weekday() - 1) % 7 + 1)
    nine = datetime.combine(base, clock_time(9, 0), tzinfo=now.tzinfo)
    if nine > now:
        return base
    ahead = datetime.combine(now.date(), clock_time(9, 0), tzinfo=now.tzinfo)
    if ahead <= now:
        ahead += timedelta(days=1)
    return ahead.date()


def _log_task_failure(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("auto-summary task failed", exc_info=task.exception())


class NoteSync(Protocol):
    """The Apple Notes copy (``notes_sync.NotesSync``), as this module needs it."""

    async def sync_voice(self, capture_id: str, *, force: bool = False) -> str | None: ...

    def handle_toggle(self, text: str) -> str | None: ...


class NoteInsightCommandController:
    """Exact spoken commands over the last retained note."""

    def __init__(
        self,
        insights: NoteInsightsService,
        *,
        productivity: ProductivityActionController | None = None,
        exporter: NoteExporter | None = None,
        sync: NoteSync | None = None,
        local_time_zone: Callable[[], str | None] | None = None,
        now: Callable[[], datetime] | None = None,
        review_reminders: ReminderReview | None = None,
    ) -> None:
        self._insights = insights
        self._review = review_reminders
        self._productivity = productivity
        self._exporter = exporter
        self._sync = sync
        self._local_time_zone = local_time_zone or (lambda: None)
        self._now = now or (lambda: datetime.now().astimezone())

    async def handle_command(self, text: str) -> str | None:
        # Whisper punctuates short utterances ("Summarize the last note.").
        # Terminal punctuation is STT noise for a deterministic command; a
        # trailing "?" on the ask-question capture is equally disposable.
        normalized = " ".join(text.strip().split()).rstrip(".,!?").rstrip()
        summarize = _SUMMARIZE.fullmatch(normalized)
        if summarize is not None:
            engine = "claude" if summarize.group("claude") else "local"
            receipt = await self._insights.summarize_last(engine=engine)
            await self._refresh_apple_note()
            return receipt
        if _RESUMMARIZE_CLAUDE.fullmatch(normalized) is not None:
            receipt = await self._insights.summarize_last(engine="claude")
            await self._refresh_apple_note()
            return receipt
        ask = _ASK.fullmatch(normalized)
        if ask is not None:
            return await self._insights.ask_last(ask.group("question"))
        if _REMINDERS.fullmatch(normalized) is not None:
            note = self._insights.latest_retained()
            if note is None:
                return "There is no retained note to create reminders from."
            # The same flow as the note page's tap: the wearer reads every
            # reminder, then confirms once.
            return await self.create_reminders_for(note.capture_id)
        if _EXPORT_NOTES.fullmatch(normalized) is not None:
            return await self._export_to_notes()
        if self._sync is not None:
            return self._sync.handle_toggle(normalized)
        return None

    async def _refresh_apple_note(self) -> None:
        """A new summary rewrites the note's existing Apple Note (if any)."""
        if self._sync is None:
            return
        note = self._insights.latest_retained()
        if note is None:
            return
        try:
            await self._sync.sync_voice(note.capture_id)
        except Exception:
            log.exception("refreshing the Apple Note after a summary failed")

    async def create_reminders_for(self, capture_id: str) -> str:
        """The tasks the wearer said they will do, as one confirmed batch of reminders.

        The tasks are not the summary's NEXT STEPS (the model's own ideas): they
        are sentences of the transcript that carry a commitment, found by
        services/commitments.py. The wearer reads every reminder in full, then
        confirms the lot once; each is written whole.
        """
        if self._productivity is None:
            return "Reminders are unavailable on this Mac."
        if self._review is None:
            return "Reminders cannot be shown on the glasses right now."
        try:
            note = self._insights.note(capture_id)
        except VoiceNoteError:
            return "That note no longer exists."
        title = note.title or "Untitled"
        zone = self._local_time_zone()
        if zone is None:
            return "The Mac's time zone is unavailable, so reminders cannot be scheduled."
        try:
            local_now = self._now().astimezone(ZoneInfo(zone))
        except ZoneInfoNotFoundError:
            return "The Mac's time zone is unavailable, so reminders cannot be scheduled."
        found = await self._insights.commitments(note.capture_id)
        if found is None:
            return "The local model did not answer. Try again in a moment."
        if not found:
            return f"“{title}” has no reminders: I add only what you said you will do."
        started_us = note.started_at_us or note.created_at_us
        note_day = datetime.fromtimestamp(started_us / 1_000_000, ZoneInfo(zone)).date()
        already = 0
        items: list[tuple[ReminderIntent, ActionOrigin]] = []
        lines: list[str] = []
        for commitment in found:
            # The sentence names the reminder, so saying it again, or tapping
            # twice, finds the one already made.
            origin = ActionOrigin(
                kind="voice_note.commitment",
                identifier=f"{note.capture_id}:{commitment.key}",
                ordinal=0,
            )
            if self._productivity.is_created(origin):
                already += 1
                continue
            due = _due_date(commitment.day, note_day, local_now)
            items.append((
                ReminderIntent(
                    title=commitment.task, date=due.isoformat(), time="09:00", time_zone=zone,
                ),
                origin,
            ))
            lines.append(f"□ {commitment.task} — {due:%b} {due.day} at 09:00")
        if not items:
            plural = "s" if already != 1 else ""
            verb = "are" if already != 1 else "is"
            return f"The reminder{plural} from “{title}” {verb} already in Reminders."
        decision = await self._review(title, tuple(lines))
        if decision is None:
            return "Reminders were not added: the list was not answered in time."
        if not decision:
            return "Reminders cancelled."
        result = await self._productivity.create_reminder_batch(items, label=title)
        already += result.already
        for problem in result.problems:
            log.info("note reminder for %s not created: %s", note.capture_id, problem)
        if result.refused is not None:
            return result.refused
        if result.declined:
            return "Reminders cancelled."
        total = len(items)
        plural = "s" if total != 1 else ""
        summary = (
            f"Added {result.created} reminder{plural} from “{title}”."
            if result.created == total
            else f"Added {result.created} of {total} reminder{plural} from “{title}”."
        )
        if already:
            summary += f" {already} already in Reminders."
        if result.problems:
            return f"{summary} {result.problems[0]}"
        return f"{summary} Adjust times in Planning."

    async def _export_to_notes(self) -> str:
        note = self._insights.latest_retained()
        if self._sync is not None:
            if note is None:
                return "There is no retained note to send to Notes."
            receipt = await self._sync.sync_voice(note.capture_id, force=True)
            return receipt or "Notes export is unavailable on this Mac."
        if self._exporter is None:
            return "Notes export is unavailable on this Mac."
        if note is None:
            return "There is no retained note to send to Notes."
        document = voice_document(self._insights.store, note.capture_id)
        return await self._exporter.export(title=document.title, body_lines=document.lines)


__all__ = [
    "NoteExporter",
    "NoteInsightCommandController",
    "NoteInsightsService",
    "NoteSync",
    "ParsedInsights",
    "ReminderReview",
    "TaskCompleter",
    "clean_title",
    "parse_insights",
]
