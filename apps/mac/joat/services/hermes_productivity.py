"""Strict Hermes proposal adapters for JOAT-owned productivity actions.

Hermes returns candidate data only. It never receives a native bridge,
ActionJournal, confirmation handle, OpenClaw automation method, or delivery
route. JOAT validates the candidate again and remains the sole action owner.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import unicodedata
from collections.abc import AsyncGenerator, Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .productivity_actions import ActionOrigin, CalendarEventIntent


log = logging.getLogger(__name__)

_TRIGGER = re.compile(
    r"^(?:asks?\s+hermes\s+to\s+|hermes\s*[:,]\s*)"
    r"(?:draft|propose)\s+(?:a\s+)?calendar event"
    r"(?:\s+for)?\s+(?P<request>.+)$",
    re.IGNORECASE,
)
_UNSUPPORTED_SCOPE = re.compile(
    r"\b(?:invite|inviting|guest|guests|attendee|attendees|recurring|recurrence|"
    r"repeat|repeating|alarm|alarms|reminder|reminders|update|edit|delete|cancel|"
    r"reschedule|move)\b|\ball[\s-]?day\b|"
    r"\bevery\s+(?:day|weekday|week|month|year|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday)\b",
    re.IGNORECASE,
)
_IANA_ZONE = re.compile(
    r"^[A-Za-z][A-Za-z0-9._+-]*(?:/[A-Za-z0-9._+-]+)*$"
)
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^\d{2}:\d{2}$")
_PREFIX = "JOAT_CALENDAR_V1\n"
_NO_PROPOSAL = "NO_PROPOSAL"
_FIELDS = {
    "title",
    "date",
    "time",
    "duration_minutes",
    "time_zone",
    "calendar",
}
_MAX_RESPONSE_CHARS = 2_048
_MAX_REQUEST_CHARS = 512
_MAX_PROMPT_BYTES = 4_096
_USAGE = "Say: ask Hermes to draft a calendar event for DETAILS."


class ProposalAgent(Protocol):
    def stream(self, prompt: str) -> AsyncGenerator[str, None]: ...


class CalendarActions(Protocol):
    async def create_calendar_event(
        self,
        intent: CalendarEventIntent,
        *,
        origin: ActionOrigin,
    ) -> str: ...


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number {value}")


def _visible(value: object, *, maximum: int = 200) -> str | None:
    if not isinstance(value, str) or value != value.strip():
        return None
    if (
        not value
        or len(value) > maximum
        or _has_unsafe_unicode_controls(value)
    ):
        return None
    return value


def _has_unsafe_unicode_controls(value: str) -> bool:
    return any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        for character in value
    )


def parse_calendar_candidate(text: str) -> CalendarEventIntent | None:
    """Accept exactly one versioned object; prose and widened schemas fail."""
    candidate = text.strip()
    if not candidate.startswith(_PREFIX):
        return None
    payload = candidate[len(_PREFIX):]
    try:
        decoded: object = json.loads(
            payload,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, UnicodeError, ValueError):
        return None
    if not isinstance(decoded, dict) or set(decoded) != _FIELDS:
        return None

    title = _visible(decoded.get("title"))
    date = decoded.get("date")
    clock = decoded.get("time")
    duration = decoded.get("duration_minutes")
    zone = decoded.get("time_zone")
    calendar_value = decoded.get("calendar")
    if (
        title is None
        or not isinstance(date, str)
        or _DATE.fullmatch(date) is None
        or not isinstance(clock, str)
        or _TIME.fullmatch(clock) is None
        or isinstance(duration, bool)
        or not isinstance(duration, int)
        or not 1 <= duration <= 1_440
        or not isinstance(zone, str)
        or len(zone) > 128
        or _IANA_ZONE.fullmatch(zone) is None
    ):
        return None
    try:
        datetime.strptime(f"{date} {clock}", "%Y-%m-%d %H:%M")
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        return None
    calendar: str | None
    if calendar_value is None:
        calendar = None
    else:
        calendar = _visible(calendar_value)
        if calendar is None:
            return None
    return CalendarEventIntent(
        title=title,
        date=date,
        time=clock,
        duration_minutes=duration,
        time_zone=zone,
        calendar_title=calendar,
    )


def system_local_time_zone() -> str | None:
    """Return the Mac's IANA zone without treating an abbreviation as one."""
    configured = os.environ.get("TZ")
    if configured and len(configured) <= 128 and _IANA_ZONE.fullmatch(configured):
        return configured
    try:
        resolved = Path("/etc/localtime").resolve(strict=True)
    except OSError:
        return None
    for raw_root in (Path("/var/db/timezone/zoneinfo"), Path("/usr/share/zoneinfo")):
        try:
            root = raw_root.resolve(strict=True)
        except OSError:
            continue
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            continue
        zone = relative.as_posix()
        if len(zone) <= 128 and _IANA_ZONE.fullmatch(zone):
            try:
                ZoneInfo(zone)
            except ZoneInfoNotFoundError:
                return None
            return zone
    return None


class HermesCalendarProposalController:
    """Claim explicit Hermes Calendar drafts and fail closed before action."""

    def __init__(
        self,
        agent: ProposalAgent | None,
        actions: CalendarActions,
        *,
        now: Callable[[], datetime] | None = None,
        local_time_zone: str | Callable[[], str | None] | None = None,
        proposal_id: Callable[[], str] | None = None,
        timeout_s: float = 20.0,
        cleanup_timeout_s: float | None = None,
    ) -> None:
        self._agent = agent
        self._actions = actions
        self._now = now or (lambda: datetime.now().astimezone())
        if callable(local_time_zone):
            self._local_time_zone = local_time_zone
        else:
            self._local_time_zone = lambda: local_time_zone
        self._proposal_id = proposal_id or (lambda: secrets.token_hex(16))
        if timeout_s <= 0:
            raise ValueError("Hermes proposal timeout must be positive")
        cleanup = (
            min(1.0, timeout_s / 4)
            if cleanup_timeout_s is None
            else cleanup_timeout_s
        )
        if cleanup <= 0 or cleanup >= timeout_s:
            raise ValueError("Hermes cleanup timeout must fit inside the proposal timeout")
        self._timeout_s = timeout_s
        self._generation_timeout_s = timeout_s - cleanup
        self._cleanup_timeout_s = cleanup

    async def handle_command(self, text: str) -> str | None:
        normalized = " ".join(text.strip().split())
        matched = _TRIGGER.fullmatch(normalized)
        if matched is None:
            lowered = normalized.casefold()
            if (
                "calendar event" in lowered
                and (
                    lowered.startswith("ask hermes")
                    or lowered.startswith("asks hermes")
                    or lowered.startswith("hermes")
                )
            ):
                return _USAGE
            return None
        request = matched.group("request")
        if len(request) > _MAX_REQUEST_CHARS:
            return "Calendar request is too long for Hermes. Nothing was created."
        if _has_unsafe_unicode_controls(request):
            return "Calendar request contains unsafe text controls. Nothing was created."
        if _UNSUPPORTED_SCOPE.search(request):
            return (
                "That Calendar request includes unsupported actions. "
                "Nothing was created."
            )
        if self._agent is None:
            return "Hermes Agent Terminal is not ready. Nothing was created."
        prompt = self._prompt(request)
        if len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
            return "Calendar request is too long for Hermes. Nothing was created."
        stream = self._agent.stream(prompt)
        collection = asyncio.create_task(self._collect_stream(stream))
        try:
            done, _ = await asyncio.wait(
                {collection},
                timeout=self._generation_timeout_s,
            )
        except asyncio.CancelledError:
            await self._stop_collection(collection, stream)
            raise
        if collection not in done:
            if not await self._stop_collection(collection, stream):
                log.warning("Hermes Calendar remote stop confirmation timed out")
                return (
                    "Hermes timed out and remote stop was not confirmed. "
                    "Nothing was created."
                )
            return "Hermes could not prepare a Calendar proposal. Nothing was created."

        try:
            parts, failure = collection.result()
        except Exception as exc:
            log.warning(
                "Hermes Calendar proposal failed type=%s",
                type(exc).__name__,
            )
            failure = "Hermes could not prepare a Calendar proposal. Nothing was created."
            parts = []

        if not await self._close_stream(stream):
            return "Hermes remote stop was not confirmed. Nothing was created."
        if failure is not None:
            return failure

        response = "".join(parts).strip()
        if response == _NO_PROPOSAL:
            return "Hermes needs more exact Calendar details. Nothing was created."
        intent = parse_calendar_candidate(response)
        if intent is None:
            return "Hermes returned no valid Calendar proposal. Nothing was created."
        proposal_id = self._proposal_id()
        if re.fullmatch(r"[0-9a-f]{32}", proposal_id) is None:
            return "JOAT could not create a safe proposal identity. Nothing was created."
        return await self._actions.create_calendar_event(
            intent,
            origin=ActionOrigin(
                kind="openclaw.hermes.proposal",
                identifier=proposal_id,
                ordinal=0,
            ),
        )

    async def _stop_collection(
        self,
        collection: asyncio.Task[tuple[list[str], str | None]],
        stream: AsyncGenerator[str, None],
    ) -> bool:
        collection.cancel()
        stopped, _ = await asyncio.wait(
            {collection},
            timeout=self._cleanup_timeout_s,
        )
        if collection not in stopped:
            collection.cancel()
            collection.add_done_callback(self._consume_collection_result)
            return False
        try:
            collection.result()
        except BaseException:
            pass
        return await self._close_stream(stream)

    async def _collect_stream(
        self,
        stream: AsyncGenerator[str, None],
    ) -> tuple[list[str], str | None]:
        parts: list[str] = []
        response_bytes = 0
        async for chunk in stream:
            response_bytes += len(chunk.encode("utf-8"))
            if response_bytes > _MAX_RESPONSE_CHARS:
                return parts, (
                    "Hermes returned no valid Calendar proposal. "
                    "Nothing was created."
                )
            parts.append(chunk)
        return parts, None

    async def _close_stream(self, stream: AsyncGenerator[str, None]) -> bool:
        cleanup = asyncio.create_task(stream.aclose())
        done, _ = await asyncio.wait(
            {cleanup},
            timeout=self._cleanup_timeout_s,
        )
        if cleanup not in done:
            cleanup.cancel()
            cleanup.add_done_callback(self._consume_cleanup_result)
            log.warning("Hermes Calendar remote stop confirmation timed out")
            return False
        try:
            cleanup.result()
        except BaseException as exc:
            log.warning(
                "Hermes Calendar remote stop failed type=%s",
                type(exc).__name__,
            )
            return False
        return True

    @staticmethod
    def _consume_cleanup_result(task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except BaseException:
            pass

    @staticmethod
    def _consume_collection_result(
        task: asyncio.Task[tuple[list[str], str | None]],
    ) -> None:
        try:
            task.result()
        except BaseException:
            pass

    def _prompt(self, request: str) -> str:
        zone = self._local_time_zone() or "UNKNOWN (do not infer; use NO_PROPOSAL)"
        request_json = json.dumps(request, ensure_ascii=False)
        return (
            "You are Hermes preparing one candidate for JOAT. "
            "Do not execute, schedule, send, or call a tool.\n"
            f"Current local datetime: {self._now().isoformat()}\n"
            f"Default IANA time zone: {zone}\n"
            "Return exactly NO_PROPOSAL when required details cannot be resolved.\n"
            "Otherwise return exactly two parts with no markdown or prose:\n"
            "JOAT_CALENDAR_V1\n"
            '{"title":"...","date":"YYYY-MM-DD","time":"HH:MM",'
            '"duration_minutes":30,"time_zone":"Area/City","calendar":null}\n'
            "Use exactly those six fields. calendar is null or an exact local calendar name. "
            "No attendees, recurrence, alarms, links, notes, deletion, or additional actions.\n"
            "If the request asks for unsupported semantics, return NO_PROPOSAL; never "
            "silently discard requested behavior.\n"
            f"Untrusted wearer request as JSON string: {request_json}"
        )


__all__ = [
    "HermesCalendarProposalController",
    "parse_calendar_candidate",
    "system_local_time_zone",
]
