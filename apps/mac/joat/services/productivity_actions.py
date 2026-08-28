"""Deterministic, confirmed Calendar and Reminders creation.

V1 intentionally accepts a narrow grammar. Consequential writes are never
handed to a language model: the native store resolves the exact local time and
container, the HUD previews that exact target, and the durable journal commits
an attempt before EventKit is called.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..core.confirmation import ConfirmationResult
from ..core.protocol import CONFIRM_PROMPT_MAX_UTF16_UNITS, utf16_units
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
)

Confirm = Callable[..., Awaitable[bool | ConfirmationResult | None]]

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

_EVENT_USAGE = (
    "Say: schedule TITLE on YYYY-MM-DD at HH:MM for MINUTES minutes "
    "in America/Chicago."
)
_REMINDER_USAGE = (
    "Say: remind me to TITLE on YYYY-MM-DD at HH:MM in America/Chicago."
)
_CONFIRM_SECONDS = 12.0


@dataclass(frozen=True)
class _ResolvedContainer:
    identifier: str
    title: str
    store_instance_id: str
    store_generation: int


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
        return f"{self.date.strftime('%b')} {self.date.day}, {self.date:%H:%M}"


class ProductivityActionController:
    """Claim only strict productivity intents and execute them fail-closed."""

    def __init__(
        self,
        bridge: NativeBridgeClient | None,
        confirm: Confirm,
        journal: ActionJournal,
    ) -> None:
        self._bridge = bridge
        self._confirm = confirm
        self._journal = journal

    async def handle_command(self, text: str) -> str | None:
        normalized = " ".join(text.strip().split())
        event = _EVENT.fullmatch(normalized)
        if event is not None:
            return await self._create_event(event)
        reminder = _REMINDER.fullmatch(normalized)
        if reminder is not None:
            return await self._create_reminder(reminder)
        lowered = normalized.casefold()
        if lowered.startswith(("schedule ", "create calendar event ")):
            return _EVENT_USAGE
        if lowered.startswith("remind me"):
            return _REMINDER_USAGE
        return None

    async def _create_event(self, match: re.Match[str]) -> str:
        if self._bridge is None:
            return "Open JOAT on the Mac and connect Calendar first."
        title = self._valid_title(match.group("title"))
        if title is None:
            return "Calendar event title must be 1 to 200 visible characters."
        try:
            minutes = int(match.group("minutes"))
            if not 1 <= minutes <= 1_440:
                return "Calendar event duration must be 1 to 1440 minutes."
            civil = self._civil(match.group("date"), match.group("time"), match.group("zone"))
        except ValueError:
            return _EVENT_USAGE

        prepared = await self._prepare(
            entity="calendar",
            selected_title=match.group("container"),
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
            prompt=prompt,
            success=f"Created Calendar event: {title}.",
            cancelled="Calendar event cancelled.",
            uncertain="Calendar may have created the event. Check Calendar before retrying.",
            failed="Calendar did not create the event. Check JOAT on the Mac.",
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
        container, _chosen_ms = prepared
        body: dict[str, object] = {
            "store_instance_id": container.store_instance_id,
            "store_generation": container.store_generation,
            "calendar_identifier": container.identifier,
            "title": title,
            "due": civil.to_wire(),
            # EventKit owns the due alarm; JOAT monitors are a separate overlay.
            "alarm_offsets_seconds": [0],
        }
        prompt = (
            f"Create reminder “{title}”? {container.title} · "
            f"{civil.display()} · {civil.zone}"
        )
        return await self._confirm_and_write(
            kind="reminders.create",
            method="reminders.create",
            body=body,
            target=container,
            prompt=prompt,
            success=f"Created Reminder: {title}.",
            cancelled="Reminder cancelled.",
            uncertain="The reminder may have been created. Check Reminders before retrying.",
            failed="Reminders did not create the reminder. Check JOAT on the Mac.",
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

    async def _confirm_and_write(
        self,
        *,
        kind: str,
        method: str,
        body: dict[str, object],
        target: _ResolvedContainer,
        prompt: str,
        success: str,
        cancelled: str,
        uncertain: str,
        failed: str,
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
                idempotency_key=f"{kind}:{secrets.token_hex(16)}",
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
        except Exception:
            self._journal.cancel_unconfirmed(
                record.action_id,
                reason_code="confirmation_channel_failed",
                now_us=time.time_ns() // 1_000,
            )
            self._journal.purge_payload(record.action_id)
            return cancelled
        if not isinstance(decision, ConfirmationResult):
            self._journal.cancel_unconfirmed(
                record.action_id,
                reason_code="confirmation_missing_evidence",
                now_us=time.time_ns() // 1_000,
            )
            self._journal.purge_payload(record.action_id)
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
            return cancelled

        attempt = self._journal.begin_attempt(
            record.action_id,
            adapter="eventkit.native-bridge",
            adapter_version="1",
            request_id=f"native:{record.action_id}",
            now_us=time.time_ns() // 1_000,
        )
        try:
            response = await self._bridge.call(
                method,
                body,
                attempt_id=attempt.attempt_id,
                payload_digest=record.payload_digest.hex(),
            )
            if not self._readback_matches(body, response.result):
                raise NativeBridgeUnavailable(
                    "native read-back did not match the confirmed payload",
                    possible_external_commit=True,
                )
        except NativeBridgeError as exc:
            possible = exc.commit_phase.startswith("committed")
            self._record_write_failure(record.action_id, attempt.attempt_id, exc.code, possible)
            return uncertain if possible else failed
        except NativeBridgeUnavailable as exc:
            self._record_write_failure(
                record.action_id,
                attempt.attempt_id,
                "native_bridge_unavailable",
                exc.possible_external_commit,
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
            return uncertain

        evidence = self._canonical(response.result)
        self._journal.record_success(
            record.action_id,
            attempt.attempt_id,
            outcome="eventkit_saved_and_read_back",
            verification_level="read_back_matched",
            evidence_digest=hashlib.sha256(evidence).digest(),
            now_us=time.time_ns() // 1_000,
        )
        self._journal.purge_payload(record.action_id)
        return success

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

    @staticmethod
    def _readback_matches(body: dict[str, object], result: Any) -> bool:
        if not isinstance(result, dict):
            return False
        required = ("calendar_identifier", "title")
        if any(result.get(key) != body.get(key) for key in required):
            return False
        if "start_unix_ms" in body:
            return all(
                result.get(key) == body.get(key)
                for key in ("start_unix_ms", "end_unix_ms", "is_all_day", "time_zone_id")
            )
        return result.get("due") == body.get("due")

    @staticmethod
    def _valid_title(raw: str) -> str | None:
        title = raw.strip()
        if not title or len(title) > 200 or any(ord(char) < 32 for char in title):
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
