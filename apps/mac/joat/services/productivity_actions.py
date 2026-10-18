"""Deterministic, confirmed Calendar and Reminders creation.

V1 intentionally accepts a narrow grammar. Consequential writes are never
handed to a language model: the native store resolves the exact local time and
container, the HUD previews that exact target, and the durable journal commits
an attempt before EventKit is called.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
import time
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from ..core.confirmation import ConfirmationResult
from ..core.protocol import (
    CONFIRM_PROMPT_MAX_UTF16_UNITS,
    ProductivityHudView,
    utf16_units,
)
from ..native.bridge import (
    NativeBridgeClient,
    NativeBridgeError,
    NativeBridgeProtocolError,
    NativeBridgeResponse,
    NativeBridgeUnavailable,
)
from ..persistence.actions import (
    ActionConflictError,
    ActionDraft,
    ActionJournal,
    ActionRecord,
    ConfirmationEvidence,
    ExternalReference,
)
from ..persistence.tasks import TaskOccurrenceConflict, TaskOccurrenceStore
from .task_monitor import native_reminder_matches_persisted_due

Confirm = Callable[..., Awaitable[bool | ConfirmationResult | None]]
WorkflowPhase = Literal[
    "verifying", "saved", "monitoring", "completed", "failed", "check", "cancelled", "not_saved",
]
ActionStatusPublisher = Callable[[ProductivityHudView], Awaitable[None]]

_ZONE = r"[A-Za-z][A-Za-z0-9._+-]*(?:/[A-Za-z0-9._+-]+)*"
_EVENT = re.compile(
    rf"^(?:schedule|create calendar event)\s+(?P<title>.+?)\s+"
    rf"on\s+(?P<date>\d{{4}}-\d{{2}}-\d{{2}})\s+"
    rf"at\s+(?P<time>\d{{2}}:\d{{2}})\s+"
    rf"for\s+(?P<minutes>\d{{1,4}})\s+minutes?\s+"
    rf"in\s+(?P<zone>{_ZONE})"
    rf"(?:\s+in calendar\s+(?P<container>.+))?$",
    re.IGNORECASE,
)
_REMINDER = re.compile(
    rf"^remind me to\s+(?P<title>.+?)\s+"
    rf"on\s+(?P<date>\d{{4}}-\d{{2}}-\d{{2}})\s+"
    rf"at\s+(?P<time>\d{{2}}:\d{{2}})\s+"
    rf"in\s+(?P<zone>{_ZONE})"
    rf"(?:\s+in list\s+(?P<container>.+))?$",
    re.IGNORECASE,
)
_COMPLETE_REMINDER = re.compile(r"^complete reminder\s+(?P<title>.+)$", re.IGNORECASE)

_EVENT_USAGE = (
    "Say: schedule TITLE on YYYY-MM-DD at HH:MM for MINUTES minutes "
    "in America/Chicago."
)
_REMINDER_USAGE = (
    "Say: remind me to TITLE on YYYY-MM-DD at HH:MM in America/Chicago."
)
# Same envelope as a Messages reply (message_reply.CONFIRM_TRANSPORT_SECONDS):
# the plugin's eight-second window plus the gate's paint before it and the
# page restore after the tap, which an acceptance waits for before leaving.
_CONFIRM_SECONDS = 20.0
# What the journal's one row per origin says about asking again (see
# ProductivityActionController._origin_for_next_attempt).
_ORIGIN_CREATED = frozenset({"succeeded", "reconciled_succeeded"})
_ORIGIN_MAY_EXIST = frozenset({
    "approved", "dispatching", "verifying", "uncertain", "reconciling", "manual_review",
})
_ORIGIN_NOT_WRITTEN = frozenset({
    "rejected", "expired", "cancelled", "superseded", "invalidated", "failed",
    "reconciled_absent",
})
_ORIGIN_ATTEMPTS = 25
log = logging.getLogger(__name__)


def _fit_to_units(text: str, units: int) -> str:
    """Cut ``text`` at a word to at most ``units`` UTF-16 units, ending in an ellipsis."""
    if utf16_units(text) <= units:
        return text
    kept = text
    while kept and utf16_units(kept + "…") > units:
        kept = kept[:-1]
    head, space, _ = kept.rpartition(" ")
    if space and len(head) >= len(kept) // 2:
        kept = head
    return kept.rstrip(" ,;:.-–—") + "…"


@dataclass(frozen=True)
class _ResolvedContainer:
    identifier: str
    title: str
    store_instance_id: str
    store_generation: int


@dataclass(frozen=True)
class ActionOrigin:
    """Opaque local provenance for a proposed consequential action."""

    kind: str
    identifier: str
    ordinal: int = 0


@dataclass(frozen=True)
class _OriginRefusal:
    """Why an origin's earlier attempts mean the reminder is not asked again."""

    message: str
    #: True when the reminder already exists: the answer is "already there".
    created: bool = False


@dataclass(frozen=True)
class ReminderBatchResult:
    """What one confirmed batch of reminders came to."""

    created: int = 0
    #: Reminders the journal already shows as created, so they were not asked again.
    already: int = 0
    #: Wearer-facing reasons for reminders that were not written, in order.
    problems: tuple[str, ...] = ()
    #: The wearer said no (or did not answer) to the one confirmation.
    declined: bool = False
    #: Set when nothing could be asked at all.
    refused: str | None = None


@dataclass(frozen=True)
class _BatchMember:
    title: str
    body: dict[str, object]
    payload: bytes
    chosen_ms: int
    origin: ActionOrigin


@dataclass(frozen=True)
class CalendarEventIntent:
    """Validated-shape candidate; EventKit remains authoritative for meaning."""

    title: str
    date: str
    time: str
    duration_minutes: int
    time_zone: str
    calendar_title: str | None = None


@dataclass(frozen=True)
class ReminderIntent:
    """Validated-shape reminder candidate; EventKit remains authoritative."""

    title: str
    date: str
    time: str
    time_zone: str
    list_title: str | None = None


@dataclass(frozen=True)
class _CivilIntent:
    date: datetime
    zone: str

    def to_wire(self) -> dict[str, object]:
        return {
            "year": self.date.year,
            "month": self.date.month,
            "day": self.date.day,
            "hour": self.date.hour,
            "minute": self.date.minute,
            "second": 0,
            "time_zone_id": self.zone,
            "repeated_time_choice": None,
        }

    def display(self) -> str:
        return (
            f"{self.date.strftime('%b')} {self.date.day}, {self.date.year} "
            f"at {self.date:%H:%M}"
        )

    def display_short(self) -> str:
        """The day and time alone, for a prompt that has no room for more."""
        return f"{self.date.strftime('%b')} {self.date.day} at {self.date:%H:%M}"


class ProductivityActionController:
    """Claim only strict productivity intents and execute them fail-closed."""

    def __init__(
        self,
        bridge: NativeBridgeClient | None,
        confirm: Confirm,
        journal: ActionJournal,
        task_occurrences: TaskOccurrenceStore | None = None,
        *,
        publish_status: ActionStatusPublisher | None = None,
    ) -> None:
        self._bridge = bridge
        self._confirm = confirm
        self._journal = journal
        self._task_occurrences = task_occurrences
        self._publish_status = publish_status

    async def handle_command(self, text: str) -> str | None:
        # Whisper punctuates short utterances ("Remind me to call at 5pm.").
        # Terminal punctuation is STT noise for a deterministic command.
        normalized = " ".join(text.strip().split()).rstrip(".,!?").rstrip()
        event = _EVENT.fullmatch(normalized)
        if event is not None:
            return await self._create_event(event)
        reminder = _REMINDER.fullmatch(normalized)
        if reminder is not None:
            return await self._create_reminder(reminder)
        completion = _COMPLETE_REMINDER.fullmatch(normalized)
        if completion is not None:
            return await self._complete_reminder(completion.group("title"))
        lowered = normalized.casefold()
        if lowered.startswith(("schedule ", "create calendar event ")):
            return _EVENT_USAGE
        if lowered.startswith("remind me"):
            return _REMINDER_USAGE
        if lowered.startswith("complete reminder"):
            return "Say: complete reminder EXACT TITLE."
        return None

    async def _create_event(self, match: re.Match[str]) -> str:
        return await self.create_calendar_event(CalendarEventIntent(
            title=match.group("title"),
            date=match.group("date"),
            time=match.group("time"),
            duration_minutes=int(match.group("minutes")),
            time_zone=match.group("zone"),
            calendar_title=match.group("container"),
        ))

    async def create_calendar_event(
        self,
        intent: CalendarEventIntent,
        *,
        origin: ActionOrigin | None = None,
    ) -> str:
        """Validate a typed candidate, then use the same confirmed native path."""
        if self._bridge is None:
            return "Open JOAT on the Mac and connect Calendar first."
        title = self._valid_title(intent.title)
        if title is None:
            return "Calendar event title must be 1 to 200 visible characters."
        try:
            minutes = intent.duration_minutes
            if isinstance(minutes, bool) or not isinstance(minutes, int):
                raise ValueError
            if not 1 <= minutes <= 1_440:
                return "Calendar event duration must be 1 to 1440 minutes."
            if re.fullmatch(_ZONE, intent.time_zone) is None or len(intent.time_zone) > 128:
                raise ValueError
            civil = self._civil(intent.date, intent.time, intent.time_zone)
        except ValueError:
            return _EVENT_USAGE
        selected_calendar = intent.calendar_title
        if selected_calendar is not None:
            selected_calendar = self._valid_title(selected_calendar)
            if selected_calendar is None:
                return "Calendar name must be 1 to 200 visible characters."
        if origin is not None and (
            not origin.kind
            or len(origin.kind) > 128
            or not origin.identifier
            or len(origin.identifier) > 256
            or isinstance(origin.ordinal, bool)
            or not isinstance(origin.ordinal, int)
            or origin.ordinal < 0
        ):
            return "Calendar event proposal provenance was invalid. Nothing was created."

        prepared = await self._prepare(
            entity="calendar",
            selected_title=selected_calendar,
            civil=civil,
        )
        if isinstance(prepared, str):
            return prepared
        container, start_ms = prepared
        body: dict[str, object] = {
            "store_instance_id": container.store_instance_id,
            "store_generation": container.store_generation,
            "calendar_identifier": container.identifier,
            "title": title,
            "is_all_day": False,
            "start_unix_ms": start_ms,
            "end_unix_ms": start_ms + minutes * 60_000,
            "time_zone_id": civil.zone,
            "alarm_offsets_seconds": [],
        }
        prompt = (
            f"Create event “{title}”? {container.title} · {civil.display()} · "
            f"{minutes}m · {civil.zone}"
        )
        return await self._confirm_and_write(
            kind="calendar.create",
            method="calendar.create",
            body=body,
            target=container,
            display_title=title,
            prompt=prompt,
            success=f"Created Calendar event: {title}.",
            cancelled="Calendar event cancelled.",
            not_saved=(
                "Calendar event was not saved because confirmation could not be verified."
            ),
            uncertain="Calendar may have created the event. Check Calendar before retrying.",
            failed="Calendar did not create the event. Check JOAT on the Mac.",
            monitor_due_unix_ms=None,
            planning_anchor_unix_ms=start_ms,
            origin=origin,
        )

    async def _create_reminder(self, match: re.Match[str]) -> str:
        return await self.create_reminder(ReminderIntent(
            title=match.group("title"),
            date=match.group("date"),
            time=match.group("time"),
            time_zone=match.group("zone"),
            list_title=match.group("container"),
        ))

    async def create_reminder(
        self,
        intent: ReminderIntent,
        *,
        origin: ActionOrigin | None = None,
        fit_title: bool = False,
    ) -> str:
        """Validate a typed candidate, then use the same confirmed native path.

        ``fit_title`` is for text the wearer did not dictate word for word (a
        note's to-do): the confirmation is the short form and the title is cut
        at a word to fit it, so the screen shows exactly what is written.
        Without it a title too long for the exact prompt is refused.
        """
        if self._bridge is None:
            return "Open JOAT on the Mac and connect Reminders first."
        title = self._valid_title(intent.title)
        if title is None:
            return "Reminder title must be 1 to 200 visible characters."
        try:
            if re.fullmatch(_ZONE, intent.time_zone) is None or len(intent.time_zone) > 128:
                raise ValueError
            civil = self._civil(intent.date, intent.time, intent.time_zone)
        except ValueError:
            return _REMINDER_USAGE
        selected_list = intent.list_title
        if selected_list is not None:
            selected_list = self._valid_title(selected_list)
            if selected_list is None:
                return "Reminder list name must be 1 to 200 visible characters."
        if origin is not None and (
            not origin.kind
            or len(origin.kind) > 128
            or not origin.identifier
            or len(origin.identifier) > 256
            or isinstance(origin.ordinal, bool)
            or not isinstance(origin.ordinal, int)
            or origin.ordinal < 0
        ):
            return "Reminder proposal provenance was invalid. Nothing was created."
        if origin is not None:
            next_origin = self._origin_for_next_attempt(origin, title)
            if isinstance(next_origin, _OriginRefusal):
                return next_origin.message
            origin = next_origin

        prepared = await self._prepare(
            entity="reminders",
            selected_title=selected_list,
            civil=civil,
        )
        if isinstance(prepared, str):
            return prepared
        container, chosen_ms = prepared
        if fit_title:
            head, tail = "Remind + alert “", f"”? {civil.display_short()}"
            title = _fit_to_units(
                title, CONFIRM_PROMPT_MAX_UTF16_UNITS - utf16_units(head + tail),
            )
            prompt = f"{head}{title}{tail}"
        else:
            prompt = (
                f"Create reminder + JOAT alert “{title}”? {container.title} · "
                f"{civil.display()} · {civil.zone}"
            )
        body: dict[str, object] = {
            "store_instance_id": container.store_instance_id,
            "store_generation": container.store_generation,
            "calendar_identifier": container.identifier,
            "title": title,
            "due": civil.to_wire(),
            "resolved_due_unix_ms": chosen_ms,
            # EventKit owns the due alarm; JOAT monitors are a separate overlay.
            "alarm_offsets_seconds": [0],
        }
        return await self._confirm_and_write(
            kind="reminders.create",
            method="reminders.create",
            body=body,
            target=container,
            display_title=title,
            prompt=prompt,
            success=f"Created Reminder: {title}.",
            cancelled="Reminder cancelled.",
            not_saved=(
                "Reminder was not saved because confirmation could not be verified."
            ),
            uncertain="The reminder may have been created. Check Reminders before retrying.",
            failed="Reminders did not create the reminder. Check JOAT on the Mac.",
            monitor_due_unix_ms=chosen_ms,
            planning_anchor_unix_ms=chosen_ms,
            origin=origin,
        )

    def _origin_for_next_attempt(
        self, origin: ActionOrigin, title: str,
    ) -> ActionOrigin | _OriginRefusal:
        """Say whether an origin's earlier attempts allow asking again.

        The journal keeps one row per origin so a second tap cannot write the
        same reminder twice: a created one is answered, not repeated. A row that
        never wrote anything (declined, timed out, refused) must not block the
        retry its own receipt invites, so the next attempt takes a numbered
        origin of its own and the earlier rows stay behind as the audit trail.
        """
        for attempt in range(_ORIGIN_ATTEMPTS):
            candidate = origin if attempt == 0 else ActionOrigin(
                kind=origin.kind,
                identifier=f"{origin.identifier}~{attempt}",
                ordinal=origin.ordinal,
            )
            state = self._journal.state_for_origin(
                candidate.kind, candidate.identifier, candidate.ordinal,
            )
            if state is None:
                return candidate
            if state in _ORIGIN_CREATED:
                return _OriginRefusal(f"Reminder already created: {title}.", created=True)
            if state in _ORIGIN_MAY_EXIST:
                return _OriginRefusal(
                    "The reminder may have been created. Check Reminders before retrying."
                )
            if state not in _ORIGIN_NOT_WRITTEN:
                return _OriginRefusal("That reminder is still waiting for your answer.")
        return _OriginRefusal("That reminder was declined too many times. Add it in Reminders.")

    def is_created(self, origin: ActionOrigin) -> bool:
        """True when an earlier confirmation already created this origin's reminder."""
        refusal = self._origin_for_next_attempt(origin, "")
        return isinstance(refusal, _OriginRefusal) and refusal.created

    async def create_reminder_batch(
        self,
        items: Sequence[tuple[ReminderIntent, ActionOrigin]],
        *,
        label: str,
    ) -> ReminderBatchResult:
        """Ask the wearer once, then write every reminder in ``items`` whole.

        The one confirmation is bound to a batch action whose payload lists
        the digest of each reminder's exact payload, so approving it approves
        exactly those reminders and nothing else. Each reminder is still its
        own journal action with its own origin, read-back and receipt; it is
        approved only through the batch (``approve_batch_child``). A reminder
        whose origin already shows as created is left alone, so tapping again
        never writes it twice. ``label`` names the source (a note's title) on
        the confirmation.
        """
        if self._bridge is None:
            return ReminderBatchResult(refused="Open JOAT on the Mac and connect Reminders first.")
        members: list[_BatchMember] = []
        container: _ResolvedContainer | None = None
        already = 0
        problems: list[str] = []
        for intent, origin in items:
            title = self._valid_title(intent.title)
            if title is None:
                problems.append("Reminder title must be 1 to 200 visible characters.")
                continue
            try:
                if re.fullmatch(_ZONE, intent.time_zone) is None or len(intent.time_zone) > 128:
                    raise ValueError
                civil = self._civil(intent.date, intent.time, intent.time_zone)
            except ValueError:
                problems.append(_REMINDER_USAGE)
                continue
            if (
                not origin.kind
                or len(origin.kind) > 128
                or not origin.identifier
                or len(origin.identifier) > 256
                or isinstance(origin.ordinal, bool)
                or not isinstance(origin.ordinal, int)
                or origin.ordinal < 0
            ):
                problems.append("Reminder proposal provenance was invalid. Nothing was created.")
                continue
            next_origin = self._origin_for_next_attempt(origin, title)
            if isinstance(next_origin, _OriginRefusal):
                if next_origin.created:
                    already += 1
                else:
                    problems.append(next_origin.message)
                continue
            prepared = await self._prepare(
                entity="reminders", selected_title=intent.list_title, civil=civil,
            )
            if isinstance(prepared, str):
                return ReminderBatchResult(already=already, refused=prepared)
            member_container, chosen_ms = prepared
            if container is not None and (
                member_container.identifier != container.identifier
                or member_container.store_generation != container.store_generation
            ):
                return ReminderBatchResult(
                    already=already,
                    refused="Reminders changed while I prepared the list. Please try again.",
                )
            container = member_container
            body: dict[str, object] = {
                "store_instance_id": member_container.store_instance_id,
                "store_generation": member_container.store_generation,
                "calendar_identifier": member_container.identifier,
                "title": title,
                "due": civil.to_wire(),
                "resolved_due_unix_ms": chosen_ms,
                "alarm_offsets_seconds": [0],
            }
            members.append(_BatchMember(
                title=title, body=body, payload=self._canonical(body),
                chosen_ms=chosen_ms, origin=next_origin,
            ))
        if not members or container is None:
            return ReminderBatchResult(already=already, problems=tuple(problems))

        count = len(members)
        head = f"Add {count} reminder{'s' if count != 1 else ''} from “"
        source = " ".join(label.split()) or "Untitled"
        prompt = head + _fit_to_units(
            source, CONFIRM_PROMPT_MAX_UTF16_UNITS - utf16_units(head + "”?"),
        ) + "”?"
        parent_payload = self._canonical({
            "kind": "reminders.create_batch",
            "children": [hashlib.sha256(member.payload).hexdigest() for member in members],
            # Two batches of the same reminders are two decisions.
            "nonce": secrets.token_hex(16),
        })
        now_us = time.time_ns() // 1_000
        parent = self._journal.create_draft(
            ActionDraft(
                kind="reminders.create_batch",
                canonical_payload=parent_payload,
                target_digest=hashlib.sha256(
                    b"joat-eventkit-container-v1\0" + container.identifier.encode("utf-8")
                ).digest(),
                idempotency_key=f"reminders.create_batch:{secrets.token_hex(16)}",
            ),
            now_us=now_us,
        )
        self._journal.mark_ready(
            parent.action_id,
            reason_code="native_schema_and_target_valid",
            now_us=now_us + 1,
        )
        verdict = await self._decide(parent, prompt)
        if verdict != "approved":
            return ReminderBatchResult(
                already=already,
                problems=tuple(problems),
                declined=True,
                refused=(
                    "Reminders were not added because confirmation could not be verified."
                    if verdict == "unverified" else None
                ),
            )
        attempt = self._journal.begin_attempt(
            parent.action_id,
            adapter="eventkit.native-bridge",
            adapter_version="1",
            request_id=f"native:{parent.action_id}",
            now_us=time.time_ns() // 1_000,
        )
        created = 0
        uncertain = False
        evidence_parts: list[str] = []
        for member in members:
            receipt = await self._write_batch_member(parent, member, container)
            if receipt.startswith("Created Reminder:"):
                created += 1
                evidence_parts.append(f"{member.origin.identifier}:{member.origin.ordinal}:created")
            else:
                problems.append(receipt)
                if "may have been created" in receipt:
                    uncertain = True
                evidence_parts.append(f"{member.origin.identifier}:{member.origin.ordinal}:not")
        self._finish_batch(parent, attempt.attempt_id, created, len(members), uncertain, evidence_parts)
        return ReminderBatchResult(
            created=created, already=already, problems=tuple(problems),
        )

    async def _write_batch_member(
        self,
        parent: ActionRecord,
        member: _BatchMember,
        container: _ResolvedContainer,
    ) -> str:
        """Journal one reminder of an approved batch, then write it."""
        now_us = time.time_ns() // 1_000
        origin = member.origin
        try:
            record = self._journal.create_draft(
                ActionDraft(
                    kind="reminders.create",
                    canonical_payload=member.payload,
                    target_digest=hashlib.sha256(
                        b"joat-eventkit-container-v1\0" + container.identifier.encode("utf-8")
                    ).digest(),
                    idempotency_key=f"reminders.create:{origin.kind}:{origin.identifier}:{origin.ordinal}",
                    origin_kind=origin.kind,
                    origin_id=origin.identifier,
                    origin_ordinal=origin.ordinal,
                ),
                now_us=now_us,
            )
            self._journal.mark_ready(
                record.action_id,
                reason_code="native_schema_and_target_valid",
                now_us=now_us + 1,
            )
            self._journal.approve_batch_child(
                parent.action_id, record.action_id, now_us=now_us + 2,
            )
        except ActionConflictError:
            log.exception("a batch reminder could not be journaled")
            return "Reminders did not create the reminder. Check JOAT on the Mac."
        return await self._write_approved(
            record,
            kind="reminders.create",
            method="reminders.create",
            body=member.body,
            target=container,
            display_title=member.title,
            success=f"Created Reminder: {member.title}.",
            uncertain="The reminder may have been created. Check Reminders before retrying.",
            failed="Reminders did not create the reminder. Check JOAT on the Mac.",
            monitor_due_unix_ms=member.chosen_ms,
            planning_anchor_unix_ms=member.chosen_ms,
        )

    def _finish_batch(
        self,
        parent: ActionRecord,
        attempt_id: str,
        created: int,
        total: int,
        uncertain: bool,
        evidence_parts: list[str],
    ) -> None:
        """Close the batch action: succeeded only if every reminder was written."""
        now_us = time.time_ns() // 1_000
        try:
            if created == total:
                self._journal.record_success(
                    parent.action_id,
                    attempt_id,
                    outcome="batch_written",
                    verification_level="read_back_matched",
                    evidence_digest=hashlib.sha256(
                        self._canonical({"members": evidence_parts})
                    ).digest(),
                    now_us=now_us,
                )
                self._purge_payload_best_effort(parent.action_id)
            else:
                self._journal.record_failure(
                    parent.action_id,
                    attempt_id,
                    error_code="batch_member_uncertain" if uncertain else "batch_member_failed",
                    possible_external_commit=uncertain,
                    now_us=now_us,
                )
                if not uncertain:
                    self._purge_payload_best_effort(parent.action_id)
        except Exception:
            log.exception("could not close the batch action %s", parent.action_id)

    async def complete_reminder_by_identity(self, native_identifier: str) -> str | None:
        """Complete the selected native item through the ordinary confirmed flow.

        A returned string is a preflight explanation. None means the existing
        productivity workflow owns the visible confirmation and outcome.
        """
        if not isinstance(native_identifier, str) or not native_identifier.strip():
            return "The selected reminder has no valid identity. Refresh Planning."
        return await self._complete_reminder("", native_identifier=native_identifier)

    async def _complete_reminder(
        self, raw_title: str, *, native_identifier: str | None = None,
    ) -> str | None:
        if self._bridge is None:
            return "Open JOAT on the Mac and connect Reminders first."
        title = self._valid_title(raw_title) if native_identifier is None else ""
        if title is None:
            return "Reminder title must be 1 to 200 visible characters."
        try:
            listed = await self._bridge.call("reminders.listContainers")
            containers = listed.result if isinstance(listed.result, list) else []
            identifiers = [
                item["calendar_identifier"]
                for item in containers
                if isinstance(item, dict)
                and isinstance(item.get("calendar_identifier"), str)
            ]
            if native_identifier is not None:
                queried = await self._bridge.call(
                    "reminders.get", {"calendar_item_identifier": native_identifier},
                )
            else:
                queried = await self._bridge.call(
                    "reminders.query",
                    {
                        "calendar_identifiers": identifiers,
                        "include_completed": False,
                        "limit": 500,
                    },
                )
        except NativeBridgeError as exc:
            if exc.code == "full_access_required":
                return "Open JOAT on the Mac and connect Reminders first."
            if native_identifier is not None and exc.code == "unknown_method":
                return "Update JOAT on your Mac to complete this reminder."
            return f"Reminders is unavailable: {exc}."
        except (NativeBridgeUnavailable, NativeBridgeProtocolError):
            return "Reminders is unavailable. Check JOAT on the Mac."
        if (
            listed.store_instance_id != queried.store_instance_id
            or listed.store_generation != queried.store_generation
        ):
            return "Reminders changed while I prepared the completion. Please try again."
        reminders = (
            [queried.result] if native_identifier is not None and isinstance(queried.result, dict)
            else queried.result if native_identifier is None and isinstance(queried.result, list)
            else []
        )
        matches = [
            item for item in reminders
            if isinstance(item, dict)
            and (
                item.get("calendar_item_identifier") == native_identifier
                if native_identifier is not None
                else str(item.get("title") or "").casefold() == title.casefold()
            )
            and item.get("is_completed") is False
        ]
        if not matches:
            return (
                "This reminder is no longer incomplete. Refresh Planning."
                if native_identifier is not None else f"No incomplete reminder is exactly named {title}."
            )
        if len(matches) > 1:
            return (
                "The selected reminder identity is ambiguous. Refresh Planning."
                if native_identifier is not None
                else f"More than one incomplete reminder is named {title}. Complete it on the Mac."
            )
        reminder = matches[0]
        if native_identifier is not None:
            resolved_title = self._valid_title(str(reminder.get("title") or ""))
            if resolved_title is None:
                return "The selected reminder has an invalid title. Nothing was completed."
            title = resolved_title
        if reminder.get("has_recurrence_rules") is True:
            return "Recurring reminder completion is not enabled until occurrence behavior is device-verified."
        item_id = reminder.get("calendar_item_identifier")
        calendar_id = reminder.get("calendar_identifier")
        if not isinstance(item_id, str) or not isinstance(calendar_id, str):
            return "Reminders returned an invalid item identity. Nothing was completed."
        container_titles = {
            item.get("calendar_identifier"): item.get("title")
            for item in containers
            if isinstance(item, dict)
        }
        container_title = container_titles.get(calendar_id)
        if not isinstance(container_title, str):
            return "The reminder list changed. Please try again."
        container = _ResolvedContainer(
            identifier=calendar_id,
            title=container_title,
            store_instance_id=queried.store_instance_id,
            store_generation=queried.store_generation,
        )
        body: dict[str, object] = {
            "store_instance_id": container.store_instance_id,
            "store_generation": container.store_generation,
            "calendar_identifier": container.identifier,
            "calendar_item_identifier": item_id,
        }
        outcome = await self._confirm_and_write(
            kind="reminders.complete",
            method="reminders.complete",
            body=body,
            target=container,
            display_title=title,
            prompt=f"Complete reminder “{title}”? {container.title}",
            success=f"Completed Reminder: {title}.",
            cancelled="Reminder completion cancelled.",
            not_saved=(
                "Reminder completion was not saved because confirmation could not be verified."
            ),
            uncertain="The reminder may have been completed. Check Reminders before retrying.",
            failed="Reminders did not complete the reminder. Check JOAT on the Mac.",
            monitor_due_unix_ms=None,
            planning_anchor_unix_ms=self._known_reminder_due_anchor(item_id, reminder),
            success_phase="completed",
        )
        return None if native_identifier is not None else outcome

    async def _prepare(
        self,
        *,
        entity: str,
        selected_title: str | None,
        civil: _CivilIntent,
    ) -> tuple[_ResolvedContainer, int] | str:
        assert self._bridge is not None
        label = "Calendar" if entity == "calendar" else "Reminders"
        try:
            listed = await self._bridge.call(f"{entity}.listContainers")
            container = self._select_container(listed, selected_title, entity=entity)
            if isinstance(container, str):
                return container
            resolved = await self._bridge.call(
                "localTime.resolve", {"civil_time": civil.to_wire()}
            )
        except NativeBridgeError as exc:
            if exc.code == "full_access_required":
                return f"Open JOAT on the Mac and connect {label} first."
            return f"{label} is unavailable: {exc}."
        except (NativeBridgeUnavailable, NativeBridgeProtocolError):
            return f"{label} is unavailable. Check JOAT on the Mac."

        if (
            resolved.store_instance_id != container.store_instance_id
            or resolved.store_generation != container.store_generation
        ):
            return f"{label} changed while I prepared the draft. Please try again."
        result = resolved.result
        if not isinstance(result, dict):
            return f"{label} returned an invalid time result. Nothing was created."
        status = result.get("status")
        if status == "ambiguous":
            return (
                f"That local time occurs twice in {civil.zone}. "
                "Use the Mac to choose the first or second occurrence."
            )
        if status == "nonexistent":
            return f"That local time does not exist in {civil.zone} because of a clock change."
        if status == "invalid_time_zone":
            return f"{civil.zone} is not a recognized IANA time zone."
        chosen = result.get("chosen_unix_ms")
        if status != "unique" or not isinstance(chosen, (int, float)):
            return f"{label} could not resolve that local time. Nothing was created."
        return container, int(chosen)

    @staticmethod
    def _select_container(
        response: NativeBridgeResponse,
        selected_title: str | None,
        *,
        entity: str,
    ) -> _ResolvedContainer | str:
        raw = response.result
        containers = raw if isinstance(raw, list) else []
        writable = [
            item for item in containers
            if isinstance(item, dict)
            and item.get("allows_content_modifications") is True
            and isinstance(item.get("calendar_identifier"), str)
            and isinstance(item.get("title"), str)
        ]
        noun = "calendar" if entity == "calendar" else "list"
        if selected_title is not None:
            wanted = selected_title.strip().casefold()
            matches = [item for item in writable if str(item["title"]).casefold() == wanted]
            if not matches:
                return f"No writable {noun} is exactly named {selected_title.strip()}."
            if len(matches) > 1:
                return f"There is more than one writable {noun} named {selected_title.strip()}. Choose it on the Mac."
            selected = matches[0]
        else:
            defaults = [item for item in writable if item.get("is_default") is True]
            if len(defaults) == 1:
                selected = defaults[0]
            elif len(writable) == 1:
                selected = writable[0]
            else:
                suffix = "in calendar NAME" if entity == "calendar" else "in list NAME"
                return f"Choose an exact writable {noun}: add “{suffix}” to the command."
        return _ResolvedContainer(
            identifier=str(selected["calendar_identifier"]),
            title=str(selected["title"]),
            store_instance_id=response.store_instance_id,
            store_generation=response.store_generation,
        )

    def _known_reminder_due_anchor(
        self,
        external_item_id: str,
        native_reminder: dict[str, object],
    ) -> int | None:
        """Return the actual due instant only when JOAT durably recorded it.

        EventKit reminder queries expose civil components, which cannot encode
        a fall-back clock-hour choice. A native reminder created outside JOAT,
        or a JOAT-created one rescheduled in EventKit, therefore returns to
        generic Planning; its completion is still safe, but we never
        manufacture a potentially wrong day.
        """

        if self._task_occurrences is None:
            return None
        try:
            occurrence = self._task_occurrences.find_by_external_item_id(external_item_id)
        except (ValueError, TaskOccurrenceConflict):
            return None
        if occurrence is None:
            return None
        if not native_reminder_matches_persisted_due(occurrence, native_reminder):
            return None
        anchor = occurrence.due_unix_ms
        if isinstance(anchor, bool) or not 0 <= anchor <= 32_503_680_000_000:
            return None
        return anchor

    async def _decide(
        self, record: ActionRecord, prompt: str,
    ) -> Literal["approved", "declined", "unverified"]:
        """Ask the wearer once and record the answer in the journal.

        ``approved`` only when the authenticated channel returned evidence
        bound to this record's payload and the wearer said yes; ``declined``
        for an answer of no or a timed-out confirmation; ``unverified`` when
        no evidence came back at all. Anything but ``approved`` has already
        had its payload dropped.
        """
        try:
            decision = await self._confirm(
                record.action_id,
                prompt,
                _CONFIRM_SECONDS,
                payload_digest=record.payload_digest.hex(),
            )
        except asyncio.CancelledError:
            self._journal.cancel_unconfirmed(
                record.action_id,
                reason_code="confirmation_cancelled",
                now_us=time.time_ns() // 1_000,
            )
            self._journal.purge_payload(record.action_id)
            raise
        except Exception:
            self._journal.cancel_unconfirmed(
                record.action_id,
                reason_code="confirmation_channel_failed",
                now_us=time.time_ns() // 1_000,
            )
            self._journal.purge_payload(record.action_id)
            return "unverified"
        if not isinstance(decision, ConfirmationResult):
            self._journal.cancel_unconfirmed(
                record.action_id,
                reason_code="confirmation_missing_evidence",
                now_us=time.time_ns() // 1_000,
            )
            self._journal.purge_payload(record.action_id)
            return "unverified"
        self._journal.record_confirmation(
            record.action_id,
            ConfirmationEvidence(
                challenge_id=decision.challenge_id,
                challenge_token=decision.challenge_id,
                session_id=decision.session_id,
                pairing_identity_digest=decision.pairing_identity_digest,
                authorization_digest=decision.authorization_digest,
                payload_digest=decision.payload_digest,
                preview_digest=decision.preview_digest,
                issued_at_us=decision.issued_at_us,
                expires_at_us=decision.expires_at_us,
                preview_completed_at_us=decision.preview_completed_at_us,
                decided_at_us=decision.decided_at_us,
                accepted=decision.accepted,
            ),
        )
        if self._journal.get(record.action_id).state != "approved":
            self._journal.purge_payload(record.action_id)
            return "declined"
        return "approved"

    async def _confirm_and_write(
        self,
        *,
        kind: str,
        method: str,
        body: dict[str, object],
        target: _ResolvedContainer,
        display_title: str,
        prompt: str,
        success: str,
        cancelled: str,
        not_saved: str,
        uncertain: str,
        failed: str,
        monitor_due_unix_ms: int | None,
        planning_anchor_unix_ms: int | None,
        success_phase: Literal["saved", "completed"] = "saved",
        origin: ActionOrigin | None = None,
    ) -> str:
        assert self._bridge is not None
        if utf16_units(prompt) > CONFIRM_PROMPT_MAX_UTF16_UNITS:
            return "The exact action is too long for the confirmation screen. Shorten the title."
        payload = self._canonical(body)
        now_us = time.time_ns() // 1_000
        record = self._journal.create_draft(
            ActionDraft(
                kind=kind,
                canonical_payload=payload,
                target_digest=hashlib.sha256(
                    b"joat-eventkit-container-v1\0"
                    + target.identifier.encode("utf-8")
                ).digest(),
                idempotency_key=(
                    f"{kind}:{origin.kind}:{origin.identifier}:{origin.ordinal}"
                    if origin is not None
                    else f"{kind}:{secrets.token_hex(16)}"
                ),
                origin_kind=origin.kind if origin is not None else None,
                origin_id=origin.identifier if origin is not None else None,
                origin_ordinal=origin.ordinal if origin is not None else None,
            ),
            now_us=now_us,
        )
        self._journal.mark_ready(
            record.action_id,
            reason_code="native_schema_and_target_valid",
            now_us=now_us + 1,
        )
        verdict = await self._decide(record, prompt)
        if verdict == "unverified":
            await self._publish_workflow_status(
                kind,
                "not_saved",
                display_title,
                planning_anchor_unix_ms,
            )
            return not_saved
        if verdict == "declined":
            await self._publish_workflow_status(
                kind,
                "cancelled",
                display_title,
                planning_anchor_unix_ms,
            )
            return cancelled

        return await self._write_approved(
            record,
            kind=kind,
            method=method,
            body=body,
            target=target,
            display_title=display_title,
            success=success,
            uncertain=uncertain,
            failed=failed,
            monitor_due_unix_ms=monitor_due_unix_ms,
            planning_anchor_unix_ms=planning_anchor_unix_ms,
            success_phase=success_phase,
        )

    async def _write_approved(
        self,
        record: ActionRecord,
        *,
        kind: str,
        method: str,
        body: dict[str, object],
        target: _ResolvedContainer,
        display_title: str,
        success: str,
        uncertain: str,
        failed: str,
        monitor_due_unix_ms: int | None,
        planning_anchor_unix_ms: int | None,
        success_phase: Literal["saved", "completed"] = "saved",
    ) -> str:
        """Write one approved action to EventKit and record exactly what happened.

        The action is already ``approved``: by its own confirmation, or as a
        member of a batch the wearer confirmed whole.
        """
        assert self._bridge is not None
        # A Reminder can be rescheduled while its confirmation is visible.
        # Do not keep showing its queried pre-confirmation day after the
        # point where native state may have changed. A successful completion
        # below re-establishes an anchor only from its own native read-back.
        post_confirmation_anchor = (
            None if method == "reminders.complete" else planning_anchor_unix_ms
        )
        terminal_planning_anchor = post_confirmation_anchor
        attempt = self._journal.begin_attempt(
            record.action_id,
            adapter="eventkit.native-bridge",
            adapter_version="1",
            request_id=f"native:{record.action_id}",
            now_us=time.time_ns() // 1_000,
        )
        await self._publish_workflow_status(
            kind,
            "verifying",
            display_title,
            post_confirmation_anchor,
        )
        try:
            response = await self._bridge.call(
                method,
                body,
                attempt_id=attempt.attempt_id,
                payload_digest=record.payload_digest.hex(),
            )
            if response.commit_phase != "committed_readback":
                raise NativeBridgeUnavailable(
                    "native write response did not prove committed read-back",
                    possible_external_commit=True,
                )
            if not self._readback_matches(body, response.result):
                raise NativeBridgeUnavailable(
                    "native read-back did not match the confirmed payload",
                    possible_external_commit=True,
                )
            if monitor_due_unix_ms is not None and self._task_occurrences is not None:
                result = response.result
                assert isinstance(result, dict)
                external_item_id = result.get("calendar_item_identifier")
                if not isinstance(external_item_id, str) or not external_item_id:
                    raise NativeBridgeUnavailable(
                        "native reminder read-back omitted its identifier",
                        possible_external_commit=True,
                    )
                due = body.get("due")
                zone = due.get("time_zone_id") if isinstance(due, dict) else None
                if not isinstance(zone, str):
                    raise NativeBridgeUnavailable(
                        "native reminder read-back omitted its time zone",
                        possible_external_commit=True,
                    )
                self._task_occurrences.register(
                    action_id=record.action_id,
                    external_item_id=external_item_id,
                    calendar_identifier=target.identifier,
                    title=str(body["title"]),
                    due_unix_ms=monitor_due_unix_ms,
                    time_zone_id=zone,
                    now_us=time.time_ns() // 1_000,
                )
            elif method == "reminders.complete" and self._task_occurrences is not None:
                result = response.result
                assert isinstance(result, dict)
                external_item_id = result.get("calendar_item_identifier")
                if not isinstance(external_item_id, str):
                    raise NativeBridgeUnavailable(
                        "native reminder completion omitted its identifier",
                        possible_external_commit=True,
                    )
                self._task_occurrences.mark_completed_external_if_present(
                    external_item_id,
                    now_us=time.time_ns() // 1_000,
                )
                terminal_planning_anchor = self._known_reminder_due_anchor(
                    external_item_id,
                    result,
                )
        except NativeBridgeError as exc:
            possible = exc.commit_phase.startswith("committed")
            self._record_write_failure(record.action_id, attempt.attempt_id, exc.code, possible)
            await self._publish_write_failure(
                kind, display_title, possible, post_confirmation_anchor,
            )
            return uncertain if possible else failed
        except NativeBridgeUnavailable as exc:
            self._record_write_failure(
                record.action_id,
                attempt.attempt_id,
                "native_bridge_unavailable",
                exc.possible_external_commit,
            )
            await self._publish_write_failure(
                kind, display_title, exc.possible_external_commit, post_confirmation_anchor,
            )
            return uncertain if exc.possible_external_commit else failed
        except NativeBridgeProtocolError:
            # A malformed receipt arrived only after the framed write was sent.
            self._record_write_failure(
                record.action_id,
                attempt.attempt_id,
                "native_bridge_invalid_receipt",
                True,
            )
            await self._publish_write_failure(
                kind, display_title, True, post_confirmation_anchor,
            )
            return uncertain
        except Exception:
            # EventKit may already have committed even if local occurrence
            # registration or receipt validation failed. Never offer a retry.
            self._record_write_failure(
                record.action_id,
                attempt.attempt_id,
                "post_commit_local_failure",
                True,
            )
            await self._publish_write_failure(
                kind, display_title, True, post_confirmation_anchor,
            )
            return uncertain

        try:
            evidence = self._canonical(response.result)
            result = response.result
            assert isinstance(result, dict)
            external_identifier = result.get("calendar_item_identifier")
            external_reference = None
            if isinstance(external_identifier, str) and external_identifier:
                external_reference = ExternalReference(
                    system="eventkit",
                    kind=(
                        "calendar.event"
                        if method == "calendar.create"
                        else "reminders.reminder"
                    ),
                    identifier_kind="eventkit.calendar_item_identifier",
                    identifier_value=external_identifier,
                    scope_digest=record.target_digest,
                )
            self._journal.record_success(
                record.action_id,
                attempt.attempt_id,
                outcome="eventkit_saved_and_read_back",
                verification_level="read_back_matched",
                evidence_digest=hashlib.sha256(evidence).digest(),
                now_us=time.time_ns() // 1_000,
                external_reference=external_reference,
            )
        except Exception:
            # EventKit already proved the external commit. If canonical receipt
            # construction or durable success recording fails, convert the
            # still-dispatching action to uncertainty before telling the HUD.
            # A blind retry could duplicate the native object.
            try:
                self._record_write_failure(
                    record.action_id,
                    attempt.attempt_id,
                    "post_commit_receipt_failure",
                    True,
                )
            except Exception:
                # A store may fail after committing record_success. In that
                # narrow case the durable success is authoritative; otherwise
                # retain the original exception in logs for crash recovery.
                try:
                    if self._journal.get(record.action_id).state == "succeeded":
                        self._purge_payload_best_effort(record.action_id)
                        await self._publish_workflow_status(
                            kind,
                            success_phase,
                            display_title,
                            terminal_planning_anchor,
                            reminder_monitoring_armed=(
                                method == "reminders.create"
                                and self._task_occurrences is not None
                            ),
                        )
                        return success
                except Exception:
                    pass
                log.exception("could not persist terminal state for action %s", record.action_id)
            await self._publish_write_failure(
                kind, display_title, True, terminal_planning_anchor,
            )
            return uncertain

        self._purge_payload_best_effort(record.action_id)
        await self._publish_workflow_status(
            kind,
            success_phase,
            display_title,
            terminal_planning_anchor,
            reminder_monitoring_armed=(
                method == "reminders.create" and self._task_occurrences is not None
            ),
        )
        return success

    async def _publish_write_failure(
        self,
        kind: str,
        display_title: str,
        possible_external_commit: bool,
        planning_anchor_unix_ms: int | None,
    ) -> None:
        if possible_external_commit:
            await self._publish_workflow_status(
                kind,
                "check",
                display_title,
                planning_anchor_unix_ms,
            )
        else:
            await self._publish_workflow_status(
                kind,
                "failed",
                display_title,
                planning_anchor_unix_ms,
            )

    async def _publish_workflow_status(
        self,
        kind: str,
        phase: WorkflowPhase,
        display_title: str,
        planning_anchor_unix_ms: int | None,
        *,
        reminder_monitoring_armed: bool = False,
    ) -> None:
        if self._publish_status is None:
            return
        primary, secondary, hint = self._workflow_copy(
            kind,
            phase,
            reminder_monitoring_armed=reminder_monitoring_armed,
        )
        try:
            await self._publish_status(ProductivityHudView(
                domain=self._workflow_domain(kind),
                phase=phase,
                title=display_title[:72],
                primary=primary,
                secondary=secondary,
                hint=hint,
                planning_anchor_unix_ms=planning_anchor_unix_ms,
                monitoring_armed=reminder_monitoring_armed,
            ))
        except Exception:
            # HUD feedback is evidence presentation, never write authority.
            # A disconnected display must not corrupt the durable outcome.
            return

    @staticmethod
    def _workflow_domain(kind: str) -> Literal["calendar", "reminders"]:
        return "calendar" if kind == "calendar.create" else "reminders"

    @classmethod
    def _workflow_copy(
        cls,
        kind: str,
        phase: WorkflowPhase,
        *,
        reminder_monitoring_armed: bool = False,
    ) -> tuple[str, str, str]:
        native_app = "Calendar" if cls._workflow_domain(kind) == "calendar" else "Reminders"
        # Both productivity domains return to one live planner, rather than
        # splitting the wearer into stale Calendar and Reminders lists.
        # Terminal receipts are readable for five seconds, then the glasses
        # reload the unified live planner. Double-left remains an immediate
        # wearer-controlled return, rather than an alternative destination.
        # Every terminal state has a wearer-controlled escape in addition to
        # its automatic return. Name both the destination and gesture plainly:
        # a vague countdown made the working left-double route look absent.
        return_hint = "Back to Planning: left x2 · auto in 5s"
        if phase == "verifying":
            if kind == "reminders.complete":
                return (
                    "Completing reminder",
                    "Checking native completion read-back",
                    "Please wait",
                )
            subject = "calendar event" if kind == "calendar.create" else "reminder"
            return (f"Saving {subject}", "Checking native read-back", "Please wait")
        if phase == "saved" and reminder_monitoring_armed:
            # A durable occurrence was written only after EventKit's own
            # alarm and the post-save receipt were accepted.  This informs
            # the wearer that the unified due overlay is armed without
            # pretending it proves a future macOS notification banner.
            return (
                "Saved and verified",
                "Native alarm set · JOAT monitoring armed.",
                "Back to Planning: left x2 · monitoring next",
            )
        if phase == "monitoring":
            return ("Monitoring armed", "Native alarm is scheduled. JOAT will check at due time.", return_hint)
        if phase == "saved":
            return ("Saved and verified", f"{native_app} read-back matched", return_hint)
        if phase == "completed":
            return ("Completed and verified", "Reminders read-back matched", return_hint)
        if phase == "check":
            return (f"Check {native_app}", "It may have changed. Do not retry.", return_hint)
        if phase == "cancelled":
            return ("Cancelled", f"No {native_app.lower()} item was changed.", return_hint)
        if phase == "not_saved":
            return ("Not saved", f"No {native_app.lower()} item was changed.", return_hint)
        if phase == "failed":
            if kind == "reminders.complete":
                return (
                    "Completion failed",
                    "Reminder was not completed.",
                    return_hint,
                )
            return ("Save failed", f"No {native_app.lower()} item was changed.", return_hint)
        raise ValueError(f"unsupported productivity workflow phase: {phase}")

    def _record_write_failure(
        self,
        action_id: str,
        attempt_id: str,
        code: str,
        possible_external_commit: bool,
    ) -> None:
        self._journal.record_failure(
            action_id,
            attempt_id,
            error_code=code,
            possible_external_commit=possible_external_commit,
            now_us=time.time_ns() // 1_000,
        )
        if not possible_external_commit:
            self._journal.purge_payload(action_id)

    def _purge_payload_best_effort(self, action_id: str) -> None:
        try:
            self._journal.purge_payload(action_id)
        except Exception:
            # The receipt is already durable and authoritative. Payload cleanup
            # is retriable maintenance and must not turn a verified save into a
            # false uncertainty or strand the HUD at VERIFYING.
            log.exception("could not purge payload for succeeded action %s", action_id)

    @staticmethod
    def _readback_matches(body: dict[str, object], result: Any) -> bool:
        if not isinstance(result, dict):
            return False
        if "calendar_item_identifier" in body:
            return (
                result.get("calendar_item_identifier")
                == body.get("calendar_item_identifier")
                and result.get("calendar_identifier")
                == body.get("calendar_identifier")
                and result.get("is_completed") is True
            )
        required = ("calendar_identifier", "title")
        if any(result.get(key) != body.get(key) for key in required):
            return False
        external_identifier = result.get("calendar_item_identifier")
        if not isinstance(external_identifier, str) or not external_identifier:
            return False
        if "start_unix_ms" in body:
            fields_match = all(
                result.get(key) == body.get(key)
                for key in ("start_unix_ms", "end_unix_ms", "is_all_day", "time_zone_id")
            )
        else:
            fields_match = (
                result.get("due") == body.get("due")
                and result.get("is_completed") is False
            )
        if not fields_match:
            return False
        requested_alarms = body.get("alarm_offsets_seconds")
        actual_alarms = result.get("alarms")
        if not isinstance(requested_alarms, list) or not isinstance(actual_alarms, list):
            return False
        try:
            offsets = sorted({int(offset) for offset in requested_alarms})
        except (TypeError, ValueError):
            return False
        if "due" in body:
            due_unix_ms = body.get("resolved_due_unix_ms")
            if isinstance(due_unix_ms, bool) or not isinstance(due_unix_ms, int):
                return False
            expected_absolute = sorted(due_unix_ms - offset * 1_000 for offset in offsets)
            actual_absolute: list[int] = []
            for alarm in actual_alarms:
                if (
                    not isinstance(alarm, dict)
                    or alarm.get("relative_offset_seconds") is not None
                    or isinstance(alarm.get("absolute_unix_ms"), bool)
                    or not isinstance(alarm.get("absolute_unix_ms"), int)
                ):
                    return False
                actual_absolute.append(alarm["absolute_unix_ms"])
            return sorted(actual_absolute) == expected_absolute

        expected_relative = sorted(-offset for offset in offsets)
        actual_relative: list[int] = []
        for alarm in actual_alarms:
            if (
                not isinstance(alarm, dict)
                or alarm.get("absolute_unix_ms") is not None
                or isinstance(alarm.get("relative_offset_seconds"), bool)
                or not isinstance(alarm.get("relative_offset_seconds"), int)
            ):
                return False
            actual_relative.append(alarm["relative_offset_seconds"])
        return sorted(actual_relative) == expected_relative

    @staticmethod
    def _valid_title(raw: str) -> str | None:
        title = raw.strip()
        if (
            not title
            or len(title) > 200
            or any(
                unicodedata.category(character) in {"Cc", "Cf", "Cs"}
                for character in title
            )
        ):
            return None
        return title

    @staticmethod
    def _civil(date: str, clock: str, zone: str) -> _CivilIntent:
        parsed = datetime.strptime(f"{date} {clock}", "%Y-%m-%d %H:%M")
        # strptime accepts neither out-of-range fields nor non-zero-padded input
        # under this grammar. The native resolver remains authoritative for DST.
        return _CivilIntent(parsed, zone)

    @staticmethod
    def _canonical(value: Any) -> bytes:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
