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
from collections.abc import Awaitable, Callable
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
    ActionDraft,
    ActionJournal,
    ConfirmationEvidence,
    ExternalReference,
)
from ..persistence.tasks import TaskOccurrenceConflict, TaskOccurrenceStore
from .task_monitor import native_reminder_matches_persisted_due

Confirm = Callable[..., Awaitable[bool | ConfirmationResult | None]]
WorkflowPhase = Literal[
    "verifying", "saved", "completed", "failed", "check", "cancelled", "not_saved",
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
_CONFIRM_SECONDS = 12.0
log = logging.getLogger(__name__)


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
class CalendarEventIntent:
    """Validated-shape candidate; EventKit remains authoritative for meaning."""

    title: str
    date: str
    time: str
    duration_minutes: int
    time_zone: str
    calendar_title: str | None = None


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
        normalized = " ".join(text.strip().split())
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
            uncertain="Calendar may have created the event. Check Calendar before retrying.",
            failed="Calendar did not create the event. Check JOAT on the Mac.",
            monitor_due_unix_ms=None,
            planning_anchor_unix_ms=start_ms,
            origin=origin,
        )

    async def _create_reminder(self, match: re.Match[str]) -> str:
        if self._bridge is None:
            return "Open JOAT on the Mac and connect Reminders first."
        title = self._valid_title(match.group("title"))
        if title is None:
            return "Reminder title must be 1 to 200 visible characters."
        try:
            civil = self._civil(match.group("date"), match.group("time"), match.group("zone"))
        except ValueError:
            return _REMINDER_USAGE

        prepared = await self._prepare(
            entity="reminders",
            selected_title=match.group("container"),
            civil=civil,
        )
        if isinstance(prepared, str):
            return prepared
        container, chosen_ms = prepared
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
        prompt = (
            f"Create reminder + JOAT alert “{title}”? {container.title} · "
            f"{civil.display()} · {civil.zone}"
        )
        return await self._confirm_and_write(
            kind="reminders.create",
            method="reminders.create",
            body=body,
            target=container,
            display_title=title,
            prompt=prompt,
            success=f"Created Reminder: {title}.",
            cancelled="Reminder cancelled.",
            uncertain="The reminder may have been created. Check Reminders before retrying.",
            failed="Reminders did not create the reminder. Check JOAT on the Mac.",
            monitor_due_unix_ms=chosen_ms,
            planning_anchor_unix_ms=chosen_ms,
        )

    async def _complete_reminder(self, raw_title: str) -> str:
        if self._bridge is None:
            return "Open JOAT on the Mac and connect Reminders first."
        title = self._valid_title(raw_title)
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
            return f"Reminders is unavailable: {exc}."
        except (NativeBridgeUnavailable, NativeBridgeProtocolError):
            return "Reminders is unavailable. Check JOAT on the Mac."
        if (
            listed.store_instance_id != queried.store_instance_id
            or listed.store_generation != queried.store_generation
        ):
            return "Reminders changed while I prepared the completion. Please try again."
        reminders = queried.result if isinstance(queried.result, list) else []
        matches = [
            item for item in reminders
            if isinstance(item, dict)
            and str(item.get("title") or "").casefold() == title.casefold()
            and item.get("is_completed") is False
        ]
        if not matches:
            return f"No incomplete reminder is exactly named {title}."
        if len(matches) > 1:
            return f"More than one incomplete reminder is named {title}. Complete it on the Mac."
        reminder = matches[0]
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
        return await self._confirm_and_write(
            kind="reminders.complete",
            method="reminders.complete",
            body=body,
            target=container,
            display_title=title,
            prompt=f"Complete reminder “{title}”? {container.title}",
            success=f"Completed Reminder: {title}.",
            cancelled="Reminder completion cancelled.",
            uncertain="The reminder may have been completed. Check Reminders before retrying.",
            failed="Reminders did not complete the reminder. Check JOAT on the Mac.",
            monitor_due_unix_ms=None,
            planning_anchor_unix_ms=self._known_reminder_due_anchor(item_id, reminder),
            success_phase="completed",
        )

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
            await self._publish_workflow_status(
                kind,
                "not_saved",
                display_title,
                planning_anchor_unix_ms,
            )
            return cancelled
        if not isinstance(decision, ConfirmationResult):
            self._journal.cancel_unconfirmed(
                record.action_id,
                reason_code="confirmation_missing_evidence",
                now_us=time.time_ns() // 1_000,
            )
            self._journal.purge_payload(record.action_id)
            await self._publish_workflow_status(
                kind,
                "not_saved",
                display_title,
                planning_anchor_unix_ms,
            )
            return cancelled
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
            await self._publish_workflow_status(
                kind,
                "cancelled",
                display_title,
                planning_anchor_unix_ms,
            )
            return cancelled

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
    ) -> None:
        if self._publish_status is None:
            return
        primary, secondary, hint = self._workflow_copy(kind, phase)
        try:
            await self._publish_status(ProductivityHudView(
                domain=self._workflow_domain(kind),
                phase=phase,
                title=display_title[:72],
                primary=primary,
                secondary=secondary,
                hint=hint,
                planning_anchor_unix_ms=planning_anchor_unix_ms,
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
    ) -> tuple[str, str, str]:
        native_app = "Calendar" if cls._workflow_domain(kind) == "calendar" else "Reminders"
        # Both productivity domains return to one live planner, rather than
        # splitting the wearer into stale Calendar and Reminders lists.
        return_hint = "left x2 Planning"
        if phase == "verifying":
            subject = "calendar event" if kind == "calendar.create" else "reminder"
            return (f"Saving {subject}", "Checking native read-back", "Please wait")
        if phase == "saved":
            return ("Saved and verified", f"{native_app} read-back matched", return_hint)
        if phase == "completed":
            return ("Completed and verified", "Reminders read-back matched", return_hint)
        if phase == "check":
            return (f"Check {native_app}", "It may have changed. Do not retry.", "left x2 Planning")
        if phase == "cancelled":
            return ("Cancelled", f"No {native_app.lower()} item was changed.", return_hint)
        if phase == "not_saved":
            return ("Not saved", f"No {native_app.lower()} item was changed.", return_hint)
        return ("Not saved", f"No {native_app.lower()} item was changed.", return_hint)

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
