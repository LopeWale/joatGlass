"""Falsification tests for confirmed EventKit writes."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from joat.core.confirmation import ConfirmationResult
from joat.core.protocol import (
    CONFIRM_PROMPT_MAX_UTF16_UNITS,
    ProductivityHudView,
    utf16_units,
)
from joat.native.bridge import NativeBridgeUnavailable
from joat.persistence.actions import ActionDraft, ActionJournal
from joat.persistence.store import DurableStore
from joat.persistence.tasks import TaskOccurrenceStore
from joat.services.productivity_actions import ProductivityActionController
from joat.services.productivity_actions import (
    ActionOrigin,
    CalendarEventIntent,
    ReminderBatchResult,
    ReminderIntent,
)


class FakeBridge:
    def __init__(
        self,
        *,
        containers,
        resolution=None,
        write_result=None,
        reminder_query=None,
        complete_result=None,
        write_commit_phase="committed_readback",
    ) -> None:  # type: ignore[no-untyped-def]
        self.containers = containers
        self.resolution = resolution or {
            "status": "unique",
            "chosen_unix_ms": 1_788_274_200_000,
            "chosen_utc_offset_seconds": -18_000,
        }
        self.write_result = write_result or {
            "calendar_item_identifier": "native-123",
            "calendar_identifier": "cal-work",
            "title": "Design review",
            "is_all_day": False,
            "start_unix_ms": 1_788_274_200_000,
            "end_unix_ms": 1_788_276_900_000,
            "time_zone_id": "America/Chicago",
            "alarms": [],
        }
        self.reminder_query = reminder_query or []
        self.complete_result = complete_result or {
            "calendar_item_identifier": "reminder-1",
            "calendar_identifier": "list-personal",
            "is_completed": True,
        }
        self.write_commit_phase = write_commit_phase
        self.calls: list[tuple[str, dict[str, object], str | None, str | None]] = []
        self.write_error: Exception | None = None
        self.bump_generation_on_resolve = False

    async def call(
        self,
        method: str,
        body=None,  # type: ignore[no-untyped-def]
        *,
        attempt_id: str | None = None,
        payload_digest: str | None = None,
        timeout_s: float | None = None,
    ):  # type: ignore[no-untyped-def]
        del timeout_s
        body = body or {}
        self.calls.append((method, body, attempt_id, payload_digest))
        generation = 8 if self.bump_generation_on_resolve and method == "localTime.resolve" else 7
        if method.endswith("listContainers"):
            result = self.containers
        elif method == "localTime.resolve":
            result = self.resolution
        elif method == "reminders.query":
            result = self.reminder_query
        elif method == "reminders.complete":
            if self.write_error is not None:
                raise self.write_error
            result = self.complete_result
        elif method.endswith("create"):
            if self.write_error is not None:
                raise self.write_error
            result = self.write_result
        else:
            raise AssertionError(f"unexpected bridge method {method}")
        return SimpleNamespace(
            store_instance_id="store-A",
            store_generation=generation,
            authorization=SimpleNamespace(calendar="full_access", reminders="full_access"),
            commit_phase=(
                self.write_commit_phase
                if method in ("calendar.create", "reminders.create", "reminders.complete")
                else "read_only"
            ),
            result=result,
        )


class ConfirmationHarness:
    def __init__(self, *, accept: bool) -> None:
        self.accept = accept
        self.prompts: list[str] = []

    async def __call__(
        self,
        action_id: str,
        prompt: str,
        timeout_s: float,
        *,
        payload_digest: str,
    ) -> ConfirmationResult:
        del timeout_s
        self.prompts.append(prompt)
        payload = bytes.fromhex(payload_digest)
        return ConfirmationResult(
            action_id=action_id,
            accepted=self.accept,
            # A real challenge is single-use; a second confirmation in one
            # journal must not reuse the first one's id.
            challenge_id=f"challenge-{len(self.prompts)}",
            session_id="session-1",
            payload_digest=payload,
            preview_digest=hashlib.sha256(prompt.encode()).digest(),
            pairing_identity_digest=b"p" * 32,
            authorization_digest=b"a" * 32,
            issued_at_us=1_000_000,
            expires_at_us=2_000_000,
            preview_completed_at_us=1_100_000,
            decided_at_us=1_200_000,
        )


def calendar_container(title: str, identifier: str, *, default: bool = False):  # type: ignore[no-untyped-def]
    return {
        "calendar_identifier": identifier,
        "title": title,
        "source_title": "iCloud",
        "allows_content_modifications": True,
        "is_default": default,
    }


def reminder_container(title: str, identifier: str, *, default: bool = False):  # type: ignore[no-untyped-def]
    return calendar_container(title, identifier, default=default)


def rig(
    tmp_path: Path,
    bridge: FakeBridge,
    *,
    accept: bool = True,
    updates: list[ProductivityHudView] | None = None,
):
    store = DurableStore(tmp_path / "joat.sqlite3")
    journal = ActionJournal(store)
    confirms = ConfirmationHarness(accept=accept)
    async def publish(view: ProductivityHudView) -> None:
        assert updates is not None
        updates.append(view)

    controller = ProductivityActionController(
        bridge,
        confirms,
        journal,
        TaskOccurrenceStore(store),
        publish_status=publish if updates is not None else None,
    )  # type: ignore[arg-type]
    return controller, confirms, journal, store


def workflow_projection(
    updates: list[ProductivityHudView],
) -> list[tuple[str, str, str, str, str, str]]:
    return [
        (view.domain, view.phase, view.title, view.primary, view.secondary, view.hint)
        for view in updates
    ]


class TestCalendarCreation:
    async def test_calendar_lifecycle_restores_the_requested_native_day(
        self, tmp_path: Path
    ) -> None:
        bridge = FakeBridge(
            containers=[calendar_container("Work", "cal-work", default=True)],
        )
        updates: list[ProductivityHudView] = []
        controller, _, _, store = rig(tmp_path, bridge, updates=updates)

        result = await controller.create_calendar_event(CalendarEventIntent(
            title="Design review",
            date="2026-09-01",
            time="09:30",
            duration_minutes=45,
            time_zone="America/Chicago",
        ))

        assert result == "Created Calendar event: Design review."
        assert [view.planning_anchor_unix_ms for view in updates] == [
            1_788_274_200_000,
            1_788_274_200_000,
        ]
        store.close()

    async def test_unicode_direction_controls_never_reach_preview_or_eventkit(
        self, tmp_path: Path
    ) -> None:
        bridge = FakeBridge(
            containers=[calendar_container("Work", "cal-work", default=True)]
        )
        controller, confirms, _, store = rig(tmp_path, bridge)

        result = await controller.create_calendar_event(CalendarEventIntent(
            title="Design review\u202eHidden",
            date="2026-09-01",
            time="09:30",
            duration_minutes=45,
            time_zone="America/Chicago",
            calendar_title="Work",
        ))

        assert result == "Calendar event title must be 1 to 200 visible characters."
        assert confirms.prompts == []
        assert bridge.calls == []
        assert store.execute("SELECT count(*) FROM actions").fetchone()[0] == 0
        store.close()

    async def test_typed_hermes_proposal_keeps_local_confirmation_and_origin_receipt(
        self, tmp_path: Path
    ) -> None:
        bridge = FakeBridge(containers=[calendar_container("Work", "cal-work", default=True)])
        controller, confirms, _, store = rig(tmp_path, bridge)

        result = await controller.create_calendar_event(
            CalendarEventIntent(
                title="Design review",
                date="2026-09-01",
                time="09:30",
                duration_minutes=45,
                time_zone="America/Chicago",
                calendar_title="Work",
            ),
            origin=ActionOrigin(
                kind="openclaw.hermes.proposal",
                identifier="proposal-1",
                ordinal=0,
            ),
        )

        assert result == "Created Calendar event: Design review."
        assert confirms.prompts == [
            "Create event “Design review”? Work · Sep 1, 2026 at 09:30 · 45m · America/Chicago"
        ]
        assert store.execute(
            "SELECT origin_kind,origin_id,origin_ordinal FROM actions"
        ).fetchone() == ("openclaw.hermes.proposal", "proposal-1", 0)
        store.close()

    async def test_action_lifecycle_reports_verification_and_receipt(self, tmp_path: Path) -> None:
        updates: list[ProductivityHudView] = []
        bridge = FakeBridge(containers=[calendar_container("Work", "cal-work", default=True)])
        controller, _, _, store = rig(tmp_path, bridge, updates=updates)

        result = await controller.handle_command(
            "schedule Design review on 2026-09-01 at 09:30 for 45 minutes "
            "in America/Chicago"
        )

        assert result == "Created Calendar event: Design review."
        assert workflow_projection(updates) == [
            ("calendar", "verifying", "Design review", "Saving calendar event", "Checking native read-back", "Please wait"),
            ("calendar", "saved", "Design review", "Saved and verified", "Calendar read-back matched", "Back to Planning: left x2 · auto in 5s"),
        ]
        store.close()

    async def test_receipt_persistence_failure_after_native_commit_becomes_uncertain(
        self, tmp_path: Path, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        updates: list[ProductivityHudView] = []
        bridge = FakeBridge(containers=[calendar_container("Work", "cal-work", default=True)])
        controller, _, journal, store = rig(tmp_path, bridge, updates=updates)

        def fail_receipt(*args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            del args, kwargs
            raise OSError("disk full")

        monkeypatch.setattr(journal, "record_success", fail_receipt)

        result = await controller.handle_command(
            "schedule Design review on 2026-09-01 at 09:30 for 45 minutes "
            "in America/Chicago"
        )

        assert "may have created" in str(result)
        assert store.execute("SELECT state FROM actions").fetchone()[0] == "uncertain"
        assert workflow_projection(updates)[-1] == (
            "calendar", "check", "Design review", "Check Calendar",
            "It may have changed. Do not retry.", "Back to Planning: left x2 · auto in 5s",
        )
        store.close()

    async def test_committed_receipt_that_raises_still_purges_payload(
        self, tmp_path: Path, monkeypatch
    ) -> None:  # type: ignore[no-untyped-def]
        updates: list[ProductivityHudView] = []
        bridge = FakeBridge(containers=[calendar_container("Work", "cal-work", default=True)])
        controller, _, journal, store = rig(tmp_path, bridge, updates=updates)
        record_success = journal.record_success

        def commit_then_raise(*args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            record_success(*args, **kwargs)
            raise OSError("late fsync report")

        monkeypatch.setattr(journal, "record_success", commit_then_raise)

        result = await controller.handle_command(
            "schedule Design review on 2026-09-01 at 09:30 for 45 minutes "
            "in America/Chicago"
        )

        assert result == "Created Calendar event: Design review."
        assert store.execute("SELECT state FROM actions").fetchone()[0] == "succeeded"
        assert store.execute("SELECT count(*) FROM action_payloads").fetchone()[0] == 0
        assert workflow_projection(updates)[-1] == (
            "calendar", "saved", "Design review", "Saved and verified",
            "Calendar read-back matched", "Back to Planning: left x2 · auto in 5s",
        )
        store.close()

    async def test_exact_event_is_confirmed_committed_and_read_back(self, tmp_path: Path) -> None:
        bridge = FakeBridge(containers=[calendar_container("Work", "cal-work", default=True)])
        controller, confirms, journal, store = rig(tmp_path, bridge)

        result = await controller.handle_command(
            "schedule Design review on 2026-09-01 at 09:30 for 45 minutes "
            "in America/Chicago"
        )

        assert result == "Created Calendar event: Design review."
        assert confirms.prompts == [
            "Create event “Design review”? Work · Sep 1, 2026 at 09:30 · 45m · America/Chicago"
        ]
        create = bridge.calls[-1]
        assert create[0] == "calendar.create"
        assert create[1] == {
            "store_instance_id": "store-A",
            "store_generation": 7,
            "calendar_identifier": "cal-work",
            "title": "Design review",
            "is_all_day": False,
            "start_unix_ms": 1_788_274_200_000,
            "end_unix_ms": 1_788_276_900_000,
            "time_zone_id": "America/Chicago",
            "alarm_offsets_seconds": [],
        }
        assert create[2] is not None
        action = store.execute("SELECT action_id,state FROM actions").fetchone()
        assert action is not None and action[1] == "succeeded"
        assert journal.event_states(str(action[0]))[-4:] == (
            "awaiting_confirmation", "approved", "dispatching", "succeeded",
        )
        assert store.execute("SELECT count(*) FROM action_payloads").fetchone()[0] == 0
        assert store.execute(
            "SELECT x.identifier_kind,x.identifier_value "
            "FROM action_receipts r JOIN external_identifiers x "
            "ON x.external_item_id=r.external_item_id"
        ).fetchone() == ("eventkit.calendar_item_identifier", "native-123")
        store.close()

    async def test_noncommitted_response_cannot_be_reported_as_calendar_success(
        self, tmp_path: Path
    ) -> None:
        updates: list[ProductivityHudView] = []
        bridge = FakeBridge(
            containers=[calendar_container("Work", "cal-work", default=True)],
            write_commit_phase="read_only",
        )
        controller, _, _, store = rig(tmp_path, bridge, updates=updates)

        result = await controller.handle_command(
            "schedule Design review on 2026-09-01 at 09:30 for 45 minutes "
            "in America/Chicago"
        )

        assert "may have created" in str(result)
        assert store.execute("SELECT state FROM actions").fetchone()[0] == "uncertain"
        assert workflow_projection(updates)[-1] == (
            "calendar", "check", "Design review", "Check Calendar",
            "It may have changed. Do not retry.", "Back to Planning: left x2 · auto in 5s",
        )
        store.close()

    async def test_precommit_native_failure_is_visible_but_never_claims_uncertainty(
        self, tmp_path: Path
    ) -> None:
        updates: list[ProductivityHudView] = []
        bridge = FakeBridge(containers=[calendar_container("Work", "cal-work", default=True)])
        bridge.write_error = NativeBridgeUnavailable(
            "not dispatched",
            possible_external_commit=False,
        )
        controller, _, _, store = rig(tmp_path, bridge, updates=updates)

        result = await controller.handle_command(
            "schedule Design review on 2026-09-01 at 09:30 for 45 minutes "
            "in America/Chicago"
        )

        assert result == "Calendar did not create the event. Check JOAT on the Mac."
        assert store.execute("SELECT state FROM actions").fetchone()[0] == "failed"
        assert workflow_projection(updates)[-1] == (
            "calendar", "failed", "Design review", "Save failed",
            "No calendar item was changed.", "Back to Planning: left x2 · auto in 5s",
        )
        store.close()

    async def test_missing_calendar_item_identifier_cannot_be_success(
        self, tmp_path: Path
    ) -> None:
        bridge = FakeBridge(
            containers=[calendar_container("Work", "cal-work", default=True)],
            write_result={
                "calendar_identifier": "cal-work",
                "title": "Design review",
                "is_all_day": False,
                "start_unix_ms": 1_788_274_200_000,
                "end_unix_ms": 1_788_276_900_000,
                "time_zone_id": "America/Chicago",
                "alarms": [],
            },
        )
        controller, _, _, store = rig(tmp_path, bridge)

        result = await controller.handle_command(
            "schedule Design review on 2026-09-01 at 09:30 for 45 minutes "
            "in America/Chicago"
        )

        assert "may have created" in str(result)
        assert store.execute("SELECT state FROM actions").fetchone()[0] == "uncertain"
        store.close()

    async def test_decline_never_calls_eventkit_write(self, tmp_path: Path) -> None:
        bridge = FakeBridge(containers=[calendar_container("Work", "cal-work", default=True)])
        updates: list[ProductivityHudView] = []
        controller, _, _, store = rig(tmp_path, bridge, accept=False, updates=updates)

        result = await controller.handle_command(
            "schedule Design review on 2026-09-01 at 09:30 for 45 minutes "
            "in America/Chicago"
        )

        assert result == "Calendar event cancelled."
        assert all(method != "calendar.create" for method, *_ in bridge.calls)
        assert store.execute("SELECT state FROM actions").fetchone()[0] == "rejected"
        assert workflow_projection(updates) == [
            ("calendar", "cancelled", "Design review", "Cancelled",
             "No calendar item was changed.", "Back to Planning: left x2 · auto in 5s"),
        ]
        store.close()

    async def test_missing_confirmation_evidence_is_not_reported_as_wearer_cancellation(
        self, tmp_path: Path,
    ) -> None:
        bridge = FakeBridge(
            containers=[calendar_container("Work", "cal-work", default=True)]
        )
        store = DurableStore(tmp_path / "joat.sqlite3")
        updates: list[ProductivityHudView] = []

        async def no_evidence(*_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            return None

        async def publish(view: ProductivityHudView) -> None:
            updates.append(view)

        controller = ProductivityActionController(
            bridge,
            no_evidence,
            ActionJournal(store),
            TaskOccurrenceStore(store),
            publish_status=publish,
        )  # type: ignore[arg-type]

        result = await controller.handle_command(
            "schedule Design review on 2026-09-01 at 09:30 for 45 minutes "
            "in America/Chicago"
        )

        assert result == "Calendar event was not saved because confirmation could not be verified."
        assert workflow_projection(updates)[-1][1:4] == (
            "not_saved", "Design review", "Not saved",
        )
        assert all(method != "calendar.create" for method, *_ in bridge.calls)
        store.close()

    async def test_confirmation_task_cancellation_purges_uncommitted_payload(
        self, tmp_path: Path
    ) -> None:
        bridge = FakeBridge(
            containers=[calendar_container("Work", "cal-work", default=True)]
        )
        store = DurableStore(tmp_path / "joat.sqlite3")
        journal = ActionJournal(store)
        confirmation_started = asyncio.Event()

        async def never_confirm(*args, **kwargs) -> ConfirmationResult:  # type: ignore[no-untyped-def]
            del args, kwargs
            confirmation_started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        controller = ProductivityActionController(
            bridge,
            never_confirm,
            journal,
            TaskOccurrenceStore(store),
        )  # type: ignore[arg-type]
        task = asyncio.create_task(controller.handle_command(
            "schedule Design review on 2026-09-01 at 09:30 for 45 minutes "
            "in America/Chicago"
        ))
        await confirmation_started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.execute("SELECT state FROM actions").fetchone()[0] == "cancelled"
        assert store.execute("SELECT count(*) FROM action_payloads").fetchone()[0] == 0
        assert all(method != "calendar.create" for method, *_ in bridge.calls)
        store.close()

    async def test_ambiguous_container_is_clarified_before_draft(self, tmp_path: Path) -> None:
        bridge = FakeBridge(containers=[
            calendar_container("Work", "one"),
            calendar_container("Work", "two"),
        ])
        controller, confirms, _, store = rig(tmp_path, bridge)

        result = await controller.handle_command(
            "schedule Review on 2026-09-01 at 09:30 for 30 minutes "
            "in America/Chicago in calendar Work"
        )

        assert "more than one writable calendar named Work" in str(result)
        assert confirms.prompts == []
        assert store.execute("SELECT count(*) FROM actions").fetchone()[0] == 0
        store.close()

    async def test_dst_ambiguity_is_refused_before_confirmation(self, tmp_path: Path) -> None:
        bridge = FakeBridge(
            containers=[calendar_container("Work", "cal-work", default=True)],
            resolution={
                "status": "ambiguous",
                "first_unix_ms": 1,
                "last_unix_ms": 2,
                "chosen_unix_ms": None,
            },
        )
        controller, confirms, _, store = rig(tmp_path, bridge)

        result = await controller.handle_command(
            "schedule Handoff on 2026-11-01 at 01:30 for 30 minutes "
            "in America/Chicago"
        )

        assert "occurs twice" in str(result)
        assert confirms.prompts == []
        assert store.execute("SELECT count(*) FROM actions").fetchone()[0] == 0
        store.close()

    async def test_store_change_between_selection_and_time_resolution_refuses_preview(self, tmp_path: Path) -> None:
        bridge = FakeBridge(containers=[calendar_container("Work", "cal-work", default=True)])
        bridge.bump_generation_on_resolve = True
        controller, confirms, _, store = rig(tmp_path, bridge)

        result = await controller.handle_command(
            "schedule Handoff on 2026-09-01 at 09:30 for 30 minutes "
            "in America/Chicago"
        )

        assert result == "Calendar changed while I prepared the draft. Please try again."
        assert confirms.prompts == []
        store.close()


class TestReminderCreation:
    async def test_exact_reminder_uses_default_list_and_native_alarm(self, tmp_path: Path) -> None:
        bridge = FakeBridge(
            containers=[reminder_container("Personal", "list-personal", default=True)],
            write_result={
                "calendar_item_identifier": "reminder-1",
                "calendar_identifier": "list-personal",
                "title": "Buy filters",
                "due": {
                    "year": 2026,
                    "month": 9,
                    "day": 1,
                    "hour": 18,
                    "minute": 0,
                    "second": 0,
                    "time_zone_id": "America/Chicago",
                    "repeated_time_choice": None,
                },
                "alarms": [{
                    "relative_offset_seconds": None,
                    "absolute_unix_ms": 1_788_274_200_000,
                }],
                "is_completed": False,
            },
        )
        updates: list[ProductivityHudView] = []
        controller, confirms, _, store = rig(tmp_path, bridge, updates=updates)

        result = await controller.handle_command(
            "remind me to Buy filters on 2026-09-01 at 18:00 in America/Chicago"
        )

        assert result == "Created Reminder: Buy filters."
        assert confirms.prompts == [
            "Create reminder + JOAT alert “Buy filters”? Personal · "
            "Sep 1, 2026 at 18:00 · America/Chicago"
        ]
        create = bridge.calls[-1]
        assert create[0] == "reminders.create"
        assert create[1]["calendar_identifier"] == "list-personal"
        assert create[1]["alarm_offsets_seconds"] == [0]
        assert create[1]["resolved_due_unix_ms"] == 1_788_274_200_000
        occurrence = store.execute(
            "SELECT external_item_id,due_unix_ms,state FROM task_occurrences"
        ).fetchone()
        assert occurrence == ("reminder-1", 1_788_274_200_000, "pending")
        assert store.execute(
            "SELECT count(*) FROM action_receipts WHERE external_item_id IS NOT NULL"
        ).fetchone()[0] == 1
        assert workflow_projection(updates) == [
            ("reminders", "verifying", "Buy filters", "Saving reminder", "Checking native read-back", "Please wait"),
            ("reminders", "saved", "Buy filters", "Saved and verified", "Native alarm set · JOAT monitoring armed.", "Back to Planning: left x2 · monitoring next"),
        ]
        assert updates[-1].monitoring_armed is True
        store.close()

    async def test_missing_native_alarm_is_uncertain_after_possible_commit(
        self, tmp_path: Path
    ) -> None:
        bridge = FakeBridge(
            containers=[reminder_container("Personal", "list-personal", default=True)],
            write_result={
                "calendar_item_identifier": "reminder-1",
                "calendar_identifier": "list-personal",
                "title": "Buy filters",
                "due": {
                    "year": 2026,
                    "month": 9,
                    "day": 1,
                    "hour": 18,
                    "minute": 0,
                    "second": 0,
                    "time_zone_id": "America/Chicago",
                    "repeated_time_choice": None,
                },
                "alarms": [],
                "is_completed": False,
            },
        )
        controller, _, _, store = rig(tmp_path, bridge)

        result = await controller.handle_command(
            "remind me to Buy filters on 2026-09-01 at 18:00 in America/Chicago"
        )

        assert "may have been created" in str(result)
        assert store.execute("SELECT state FROM actions").fetchone()[0] == "uncertain"
        assert store.execute("SELECT count(*) FROM task_occurrences").fetchone()[0] == 0
        store.close()

    async def test_write_disconnect_stays_uncertain_and_is_not_retried(self, tmp_path: Path) -> None:
        bridge = FakeBridge(containers=[reminder_container("Personal", "list", default=True)])
        bridge.write_error = NativeBridgeUnavailable(
            "disconnected", possible_external_commit=True
        )
        controller, _, _, store = rig(tmp_path, bridge)

        result = await controller.handle_command(
            "remind me to Buy filters on 2026-09-01 at 18:00 in America/Chicago"
        )

        assert "may have been created" in str(result)
        assert store.execute("SELECT state FROM actions").fetchone()[0] == "uncertain"
        assert len([call for call in bridge.calls if call[0] == "reminders.create"]) == 1
        store.close()

    async def test_malformed_productivity_intent_never_falls_through_to_model(self, tmp_path: Path) -> None:
        bridge = FakeBridge(containers=[])
        controller, _, _, store = rig(tmp_path, bridge)

        result = await controller.handle_command("remind me tomorrow to buy filters")

        assert result is not None and "YYYY-MM-DD" in result
        assert bridge.calls == []
        store.close()

    async def test_exact_nonrecurring_reminder_can_be_completed_with_confirmation(
        self, tmp_path: Path
    ) -> None:
        bridge = FakeBridge(
            containers=[reminder_container("Personal", "list-personal", default=True)],
            reminder_query=[{
                "calendar_item_identifier": "reminder-1",
                "calendar_identifier": "list-personal",
                "title": "Buy filters",
                "due": {
                    "year": 2026,
                    "month": 9,
                    "day": 1,
                    "hour": 18,
                    "minute": 0,
                    "second": 0,
                    "time_zone_id": "America/Chicago",
                },
                "resolved_due_unix_ms": 1_788_303_600_000,
                "is_completed": False,
                "has_recurrence_rules": False,
            }],
            complete_result={
                "calendar_item_identifier": "reminder-1",
                "calendar_identifier": "list-personal",
                "is_completed": True,
                "due": {
                    "year": 2026,
                    "month": 9,
                    "day": 1,
                    "hour": 18,
                    "minute": 0,
                    "second": 0,
                    "time_zone_id": "America/Chicago",
                },
            },
        )
        updates: list[ProductivityHudView] = []
        controller, confirms, _, store = rig(tmp_path, bridge, updates=updates)

        result = await controller.handle_command("complete reminder Buy filters")

        assert result == "Completed Reminder: Buy filters."
        assert confirms.prompts == ["Complete reminder “Buy filters”? Personal"]
        assert bridge.calls[-1][0] == "reminders.complete"
        assert bridge.calls[-1][1]["calendar_item_identifier"] == "reminder-1"
        assert bridge.calls[-1][1]["calendar_identifier"] == "list-personal"
        assert workflow_projection(updates) == [
            ("reminders", "verifying", "Buy filters", "Completing reminder", "Checking native completion read-back", "Please wait"),
            ("reminders", "completed", "Buy filters", "Completed and verified", "Reminders read-back matched", "Back to Planning: left x2 · auto in 5s"),
        ]
        # Civil due components, even if a newer native payload happens to
        # attach an instant, are not enough to prove which DST-fold instant
        # this external reminder meant. Generic Planning is the safe return.
        assert [view.planning_anchor_unix_ms for view in updates] == [None, None]
        store.close()

    async def test_definite_reminder_completion_failure_uses_completion_copy(
        self, tmp_path: Path
    ) -> None:
        bridge = FakeBridge(
            containers=[reminder_container("Personal", "list-personal", default=True)],
            reminder_query=[{
                "calendar_item_identifier": "reminder-1",
                "calendar_identifier": "list-personal",
                "title": "Buy filters",
                "due": None,
                "is_completed": False,
                "has_recurrence_rules": False,
            }],
        )
        bridge.write_error = NativeBridgeUnavailable(
            "not dispatched",
            possible_external_commit=False,
        )
        updates: list[ProductivityHudView] = []
        controller, _, _, store = rig(tmp_path, bridge, updates=updates)

        result = await controller.handle_command("complete reminder Buy filters")

        assert result == "Reminders did not complete the reminder. Check JOAT on the Mac."
        assert workflow_projection(updates)[-1] == (
            "reminders", "failed", "Buy filters", "Completion failed",
            "Reminder was not completed.", "Back to Planning: left x2 · auto in 5s",
        )
        store.close()

    async def test_completion_of_a_joat_created_reminder_keeps_its_durable_due_anchor(
        self, tmp_path: Path
    ) -> None:
        bridge = FakeBridge(
            containers=[reminder_container("Personal", "list-personal", default=True)],
            reminder_query=[{
                "calendar_item_identifier": "reminder-1",
                "calendar_identifier": "list-personal",
                "title": "Buy filters",
                # The external DTO is deliberately only civil components; the
                # persisted creation receipt supplies the safe absolute time
                # only after the current EventKit civil value confirms it has
                # not been rescheduled.
                "due": {
                    "year": 2026,
                    "month": 9,
                    "day": 1,
                    "hour": 18,
                    "minute": 0,
                    "second": 0,
                    "time_zone_id": "America/Chicago",
                },
                "is_completed": False,
                "has_recurrence_rules": False,
            }],
            complete_result={
                "calendar_item_identifier": "reminder-1",
                "calendar_identifier": "list-personal",
                "is_completed": True,
                "due": {
                    "year": 2026,
                    "month": 9,
                    "day": 1,
                    "hour": 18,
                    "minute": 0,
                    "second": 0,
                    "time_zone_id": "America/Chicago",
                },
            },
        )
        updates: list[ProductivityHudView] = []
        controller, _, journal, store = rig(tmp_path, bridge, updates=updates)
        action = journal.create_draft(ActionDraft(
            kind="reminders.create",
            canonical_payload=b"{}",
            target_digest=hashlib.sha256(b"list-personal").digest(),
            idempotency_key="known-reminder-anchor",
        ), now_us=10)
        TaskOccurrenceStore(store).register(
            action_id=action.action_id,
            external_item_id="reminder-1",
            calendar_identifier="list-personal",
            title="Buy filters",
            due_unix_ms=1_788_303_600_000,
            time_zone_id="America/Chicago",
            now_us=11,
        )

        assert await controller.handle_command("complete reminder Buy filters") == (
            "Completed Reminder: Buy filters."
        )
        assert [view.planning_anchor_unix_ms for view in updates] == [
            None,
            1_788_303_600_000,
        ]
        store.close()

    async def test_completion_of_rescheduled_joat_reminder_returns_generic_planning(
        self, tmp_path: Path
    ) -> None:
        bridge = FakeBridge(
            containers=[reminder_container("Personal", "list-personal", default=True)],
            reminder_query=[{
                "calendar_item_identifier": "reminder-1",
                "calendar_identifier": "list-personal",
                "title": "Buy filters",
                "due": {
                    "year": 2026,
                    "month": 9,
                    "day": 2,
                    "hour": 18,
                    "minute": 0,
                    "second": 0,
                    "time_zone_id": "America/Chicago",
                },
                "is_completed": False,
                "has_recurrence_rules": False,
            }],
        )
        updates: list[ProductivityHudView] = []
        controller, _, journal, store = rig(tmp_path, bridge, updates=updates)
        action = journal.create_draft(ActionDraft(
            kind="reminders.create",
            canonical_payload=b"{}",
            target_digest=hashlib.sha256(b"list-personal").digest(),
            idempotency_key="rescheduled-reminder-anchor",
        ), now_us=10)
        TaskOccurrenceStore(store).register(
            action_id=action.action_id,
            external_item_id="reminder-1",
            calendar_identifier="list-personal",
            title="Buy filters",
            due_unix_ms=1_788_303_600_000,
            time_zone_id="America/Chicago",
            now_us=11,
        )

        assert await controller.handle_command("complete reminder Buy filters") == (
            "Completed Reminder: Buy filters."
        )
        assert [view.planning_anchor_unix_ms for view in updates] == [None, None]
        store.close()

    async def test_completion_readback_reschedule_drops_the_preconfirmation_anchor(
        self, tmp_path: Path
    ) -> None:
        original_due = {
            "year": 2026,
            "month": 9,
            "day": 1,
            "hour": 18,
            "minute": 0,
            "second": 0,
            "time_zone_id": "America/Chicago",
        }
        bridge = FakeBridge(
            containers=[reminder_container("Personal", "list-personal", default=True)],
            reminder_query=[{
                "calendar_item_identifier": "reminder-1",
                "calendar_identifier": "list-personal",
                "title": "Buy filters",
                "due": original_due,
                "is_completed": False,
                "has_recurrence_rules": False,
            }],
            # The item was changed while the HUD confirmation was open. The
            # verified completion is successful, but Sep 1 is no longer a
            # truthful destination for its terminal HUD.
            complete_result={
                "calendar_item_identifier": "reminder-1",
                "calendar_identifier": "list-personal",
                "is_completed": True,
                "due": {**original_due, "day": 2},
            },
        )
        updates: list[ProductivityHudView] = []
        controller, _, journal, store = rig(tmp_path, bridge, updates=updates)
        action = journal.create_draft(ActionDraft(
            kind="reminders.create",
            canonical_payload=b"{}",
            target_digest=hashlib.sha256(b"list-personal").digest(),
            idempotency_key="completion-readback-reschedule",
        ), now_us=10)
        TaskOccurrenceStore(store).register(
            action_id=action.action_id,
            external_item_id="reminder-1",
            calendar_identifier="list-personal",
            title="Buy filters",
            due_unix_ms=1_788_303_600_000,
            time_zone_id="America/Chicago",
            now_us=11,
        )

        assert await controller.handle_command("complete reminder Buy filters") == (
            "Completed Reminder: Buy filters."
        )
        assert [view.planning_anchor_unix_ms for view in updates] == [None, None]
        store.close()

    async def test_completion_after_due_tracking_retirement_stays_native_verified(
        self, tmp_path: Path
    ) -> None:
        moved_due = {
            "year": 2026,
            "month": 9,
            "day": 2,
            "hour": 18,
            "minute": 0,
            "second": 0,
            "time_zone_id": "America/Chicago",
        }
        bridge = FakeBridge(
            containers=[reminder_container("Personal", "list-personal", default=True)],
            reminder_query=[{
                "calendar_item_identifier": "reminder-1",
                "calendar_identifier": "list-personal",
                "title": "Buy filters",
                "due": moved_due,
                "is_completed": False,
                "has_recurrence_rules": False,
            }],
            complete_result={
                "calendar_item_identifier": "reminder-1",
                "calendar_identifier": "list-personal",
                "is_completed": True,
                "due": moved_due,
            },
        )
        updates: list[ProductivityHudView] = []
        controller, _, journal, store = rig(tmp_path, bridge, updates=updates)
        action = journal.create_draft(ActionDraft(
            kind="reminders.create",
            canonical_payload=b"{}",
            target_digest=hashlib.sha256(b"list-personal").digest(),
            idempotency_key="retired-reminder-completion",
        ), now_us=10)
        occurrences = TaskOccurrenceStore(store)
        occurrence = occurrences.register(
            action_id=action.action_id,
            external_item_id="reminder-1",
            calendar_identifier="list-personal",
            title="Buy filters",
            due_unix_ms=1_788_303_600_000,
            time_zone_id="America/Chicago",
            now_us=11,
        )
        occurrences.retire_due_tracking(occurrence.occurrence_id, now_us=12)

        assert await controller.handle_command("complete reminder Buy filters") == (
            "Completed Reminder: Buy filters."
        )
        assert store.execute("SELECT state FROM actions ORDER BY created_at_us DESC").fetchone()[0] == "succeeded"
        assert occurrences._get_with(store._db, occurrence.occurrence_id).state == "cancelled"
        assert [view.planning_anchor_unix_ms for view in updates] == [None, None]
        store.close()

    async def test_completion_readback_from_another_list_is_uncertain(
        self, tmp_path: Path
    ) -> None:
        updates: list[ProductivityHudView] = []
        bridge = FakeBridge(
            containers=[reminder_container("Personal", "list-personal", default=True)],
            reminder_query=[{
                "calendar_item_identifier": "reminder-1",
                "calendar_identifier": "list-personal",
                "title": "Buy filters",
                "is_completed": False,
                "has_recurrence_rules": False,
            }],
            complete_result={
                "calendar_item_identifier": "reminder-1",
                "calendar_identifier": "list-work",
                "is_completed": True,
            },
        )
        controller, _, _, store = rig(tmp_path, bridge, updates=updates)

        result = await controller.handle_command("complete reminder Buy filters")

        assert "may have been completed" in str(result)
        assert store.execute("SELECT state FROM actions").fetchone()[0] == "uncertain"
        assert workflow_projection(updates)[-1] == (
            "reminders", "check", "Buy filters", "Check Reminders",
            "It may have changed. Do not retry.", "Back to Planning: left x2 · auto in 5s",
        )
        store.close()

    async def test_recurring_completion_is_refused_until_occurrence_semantics_are_proven(
        self, tmp_path: Path
    ) -> None:
        bridge = FakeBridge(
            containers=[reminder_container("Personal", "list-personal", default=True)],
            reminder_query=[{
                "calendar_item_identifier": "series-1",
                "calendar_identifier": "list-personal",
                "title": "Water plants",
                "is_completed": False,
                "has_recurrence_rules": True,
            }],
        )
        controller, confirms, _, store = rig(tmp_path, bridge)

        result = await controller.handle_command("complete reminder Water plants")

        assert "Recurring reminder completion is not enabled" in str(result)
        assert confirms.prompts == []
        assert all(call[0] != "reminders.complete" for call in bridge.calls)
        store.close()


class TestTypedReminderIntent:
    async def test_typed_intent_uses_the_same_confirmed_native_path(
        self, tmp_path: Path
    ) -> None:
        bridge = FakeBridge(
            containers=[reminder_container("Personal", "list-personal", default=True)],
            write_result={
                "calendar_item_identifier": "reminder-1",
                "calendar_identifier": "list-personal",
                "title": "Email the release notes",
                "due": {
                    "year": 2026,
                    "month": 9,
                    "day": 1,
                    "hour": 9,
                    "minute": 0,
                    "second": 0,
                    "time_zone_id": "America/Chicago",
                    "repeated_time_choice": None,
                },
                "alarms": [{
                    "relative_offset_seconds": None,
                    "absolute_unix_ms": 1_788_274_200_000,
                }],
                "is_completed": False,
            },
        )
        controller, confirms, _, store = rig(tmp_path, bridge)

        result = await controller.create_reminder(
            ReminderIntent(
                title="Email the release notes",
                date="2026-09-01",
                time="09:00",
                time_zone="America/Chicago",
            ),
            origin=ActionOrigin(
                kind="voice_note.todo", identifier="capture-1", ordinal=0,
            ),
        )

        assert result == "Created Reminder: Email the release notes."
        assert confirms.prompts and "Email the release notes" in confirms.prompts[0]
        assert any(call[0] == "reminders.create" for call in bridge.calls)
        store.close()

    async def test_typed_intent_rejects_invalid_provenance_before_any_bridge_call(
        self, tmp_path: Path
    ) -> None:
        bridge = FakeBridge(
            containers=[reminder_container("Personal", "list-personal", default=True)],
        )
        controller, confirms, _, store = rig(tmp_path, bridge)

        result = await controller.create_reminder(
            ReminderIntent(
                title="Email the release notes",
                date="2026-09-01",
                time="09:00",
                time_zone="America/Chicago",
            ),
            origin=ActionOrigin(kind="", identifier="capture-1", ordinal=0),
        )

        assert result == "Reminder proposal provenance was invalid. Nothing was created."
        assert confirms.prompts == []
        assert bridge.calls == []
        store.close()


# Lengths (characters) of the 39 to-dos a real JOAT summarized on this Mac,
# 2026-10-17. Only 2 of them fit the exact reminder prompt, so a note's
# "tap: reminders" refused the rest before anything was written.
REAL_TODO_LENGTHS = [
    33, 34, 37, 37, 37, 37, 44, 46, 46, 46, 47, 47, 50, 56, 58, 58, 58, 59, 60, 64,
    65, 67, 69, 69, 74, 75, 75, 76, 77, 81, 81, 81, 86, 86, 90, 93, 97, 99, 117,
]
_WORDS = "call the school office about pickup and book room for review notes".split()


class EchoingReminderBridge(FakeBridge):
    """Answers a reminder write with the reminder it was asked to make."""

    async def call(self, method, body=None, **kwargs):  # type: ignore[no-untyped-def]
        if method == "reminders.create" and body is not None:
            self.write_result = {
                "calendar_item_identifier": f"reminder-{len(self.calls)}",
                "calendar_identifier": body["calendar_identifier"],
                "title": body["title"],
                "due": body["due"],
                "alarms": [{
                    "relative_offset_seconds": None,
                    "absolute_unix_ms": body["resolved_due_unix_ms"],
                }],
                "is_completed": False,
            }
        return await super().call(method, body, **kwargs)


def todo_of_length(length: int) -> str:
    words: list[str] = []
    while len(" ".join(words)) < length:
        words.append(_WORDS[len(words) % len(_WORDS)])
    return " ".join(words)[:length].rstrip()


class TestNoteReminderFit:
    """A note's to-do is a model's sentence; the confirm screen is 120 units."""

    DUE = {"date": "2026-09-01", "time": "09:00", "time_zone": "America/Chicago"}
    LONG = (
        "Call the school office about the schedule change and ask whether the "
        "Thursday pickup moves to the afternoon"
    )

    @staticmethod
    def origin(ordinal: int = 0) -> ActionOrigin:
        return ActionOrigin(kind="voice_note.todo", identifier="capture-1", ordinal=ordinal)

    @staticmethod
    def rig_with_list(tmp_path: Path, *, accept: bool = True):  # type: ignore[no-untyped-def]
        bridge = EchoingReminderBridge(
            containers=[reminder_container("Reminders", "list-1", default=True)],
        )
        return (bridge, *rig(tmp_path, bridge, accept=accept))

    @staticmethod
    def written_titles(bridge: FakeBridge) -> list[object]:
        return [call[1]["title"] for call in bridge.calls if call[0] == "reminders.create"]

    async def test_the_exact_prompt_still_refuses_a_title_it_cannot_show_whole(
        self, tmp_path: Path
    ) -> None:
        bridge, controller, confirms, _, store = self.rig_with_list(tmp_path)

        result = await controller.create_reminder(
            ReminderIntent(title=self.LONG, **self.DUE), origin=self.origin(),
        )

        assert result == (
            "The exact action is too long for the confirmation screen. Shorten the title."
        )
        assert confirms.prompts == []
        assert self.written_titles(bridge) == []
        store.close()

    async def test_a_long_todo_is_cut_at_a_word_and_written_as_shown(
        self, tmp_path: Path
    ) -> None:
        bridge, controller, confirms, _, store = self.rig_with_list(tmp_path)

        result = await controller.create_reminder(
            ReminderIntent(title=self.LONG, **self.DUE),
            origin=self.origin(),
            fit_title=True,
        )

        (written,) = self.written_titles(bridge)
        assert isinstance(written, str)
        assert result == f"Created Reminder: {written}."
        assert written.endswith("…") and self.LONG.startswith(written[:-1])
        assert self.LONG[len(written) - 1] == " "
        assert confirms.prompts == [f"Remind + alert “{written}”? Sep 1 at 09:00"]
        assert utf16_units(confirms.prompts[0]) <= CONFIRM_PROMPT_MAX_UTF16_UNITS
        store.close()

    async def test_a_title_that_already_fits_is_written_untouched(
        self, tmp_path: Path
    ) -> None:
        bridge, controller, confirms, _, store = self.rig_with_list(tmp_path)

        result = await controller.create_reminder(
            ReminderIntent(title="Email the release notes", **self.DUE),
            origin=self.origin(),
            fit_title=True,
        )

        assert result == "Created Reminder: Email the release notes."
        assert self.written_titles(bridge) == ["Email the release notes"]
        assert confirms.prompts == ["Remind + alert “Email the release notes”? Sep 1 at 09:00"]
        store.close()

    @pytest.mark.parametrize("length", sorted(set(REAL_TODO_LENGTHS)))
    async def test_every_real_todo_length_reaches_the_confirmation(
        self, tmp_path: Path, length: int
    ) -> None:
        bridge, controller, confirms, _, store = self.rig_with_list(tmp_path)

        result = await controller.create_reminder(
            ReminderIntent(title=todo_of_length(length), **self.DUE),
            origin=self.origin(),
            fit_title=True,
        )

        assert result.startswith("Created Reminder: ")
        assert utf16_units(confirms.prompts[0]) <= CONFIRM_PROMPT_MAX_UTF16_UNITS
        store.close()

    async def test_the_cut_counts_utf16_units_not_characters(
        self, tmp_path: Path
    ) -> None:
        bridge, controller, confirms, _, store = self.rig_with_list(tmp_path)

        result = await controller.create_reminder(
            ReminderIntent(title="😀" * 70, **self.DUE),
            origin=self.origin(),
            fit_title=True,
        )

        assert result.startswith("Created Reminder: ")
        assert utf16_units(confirms.prompts[0]) <= CONFIRM_PROMPT_MAX_UTF16_UNITS
        store.close()

    async def test_a_declined_reminder_can_be_asked_again_from_the_same_note(
        self, tmp_path: Path
    ) -> None:
        bridge, controller, confirms, journal, store = self.rig_with_list(
            tmp_path, accept=False,
        )
        intent = ReminderIntent(title="Email the release notes", **self.DUE)

        first = await controller.create_reminder(intent, origin=self.origin())
        confirms.accept = True
        second = await controller.create_reminder(intent, origin=self.origin())

        assert first == "Reminder cancelled."
        assert second == "Created Reminder: Email the release notes."
        assert journal.state_for_origin("voice_note.todo", "capture-1", 0) == "rejected"
        assert journal.state_for_origin("voice_note.todo", "capture-1~1", 0) == "succeeded"
        store.close()

    async def test_a_declined_reminder_can_be_asked_again_the_next_day(
        self, tmp_path: Path
    ) -> None:
        bridge, controller, confirms, _, store = self.rig_with_list(tmp_path, accept=False)
        first = await controller.create_reminder(
            ReminderIntent(title="Email the release notes", **self.DUE),
            origin=self.origin(),
        )
        confirms.accept = True
        second = await controller.create_reminder(
            ReminderIntent(title="Email the release notes", **{**self.DUE, "date": "2026-09-02"}),
            origin=self.origin(),
        )

        assert first == "Reminder cancelled."
        assert second == "Created Reminder: Email the release notes."
        store.close()

    async def test_several_declines_in_a_row_still_leave_the_way_open(
        self, tmp_path: Path
    ) -> None:
        bridge, controller, confirms, journal, store = self.rig_with_list(
            tmp_path, accept=False,
        )
        intent = ReminderIntent(title="Email the release notes", **self.DUE)

        for _ in range(3):
            assert await controller.create_reminder(
                intent, origin=self.origin(),
            ) == "Reminder cancelled."
        confirms.accept = True
        last = await controller.create_reminder(intent, origin=self.origin())

        assert last == "Created Reminder: Email the release notes."
        assert journal.state_for_origin("voice_note.todo", "capture-1~3", 0) == "succeeded"
        store.close()

    async def test_a_created_reminder_is_answered_not_created_twice(
        self, tmp_path: Path
    ) -> None:
        bridge, controller, _, _, store = self.rig_with_list(tmp_path)
        intent = ReminderIntent(title="Email the release notes", **self.DUE)

        first = await controller.create_reminder(intent, origin=self.origin())
        second = await controller.create_reminder(intent, origin=self.origin())

        assert first == "Created Reminder: Email the release notes."
        assert second == "Reminder already created: Email the release notes."
        assert self.written_titles(bridge) == ["Email the release notes"]
        store.close()

    async def test_a_reminder_that_may_exist_is_never_asked_again(
        self, tmp_path: Path
    ) -> None:
        bridge, controller, confirms, _, store = self.rig_with_list(tmp_path)
        bridge.write_error = NativeBridgeUnavailable(
            "reply lost", possible_external_commit=True,
        )
        intent = ReminderIntent(title="Email the release notes", **self.DUE)

        first = await controller.create_reminder(intent, origin=self.origin())
        bridge.write_error = None
        second = await controller.create_reminder(intent, origin=self.origin())

        assert first == "The reminder may have been created. Check Reminders before retrying."
        assert second == first
        assert len(confirms.prompts) == 1
        store.close()

    async def test_a_reminder_that_failed_before_writing_can_be_asked_again(
        self, tmp_path: Path
    ) -> None:
        bridge, controller, _, _, store = self.rig_with_list(tmp_path)
        bridge.write_error = NativeBridgeUnavailable(
            "not dispatched", possible_external_commit=False,
        )
        intent = ReminderIntent(title="Email the release notes", **self.DUE)

        first = await controller.create_reminder(intent, origin=self.origin())
        bridge.write_error = None
        second = await controller.create_reminder(intent, origin=self.origin())

        assert first == "Reminders did not create the reminder. Check JOAT on the Mac."
        assert second == "Created Reminder: Email the release notes."
        store.close()

    async def test_an_unanswered_prompt_is_not_stacked_with_a_second_one(
        self, tmp_path: Path
    ) -> None:
        bridge, controller, confirms, journal, store = self.rig_with_list(tmp_path)
        journal.create_draft(
            ActionDraft(
                kind="reminders.create",
                canonical_payload=b"{}",
                target_digest=hashlib.sha256(b"list-1").digest(),
                idempotency_key="reminders.create:voice_note.todo:capture-1:0",
                origin_kind="voice_note.todo",
                origin_id="capture-1",
                origin_ordinal=0,
            ),
            now_us=1,
        )

        result = await controller.create_reminder(
            ReminderIntent(title="Email the release notes", **self.DUE),
            origin=self.origin(),
        )

        assert result == "That reminder is still waiting for your answer."
        assert confirms.prompts == []
        store.close()

    def test_an_origin_with_no_action_has_no_state(self, tmp_path: Path) -> None:
        _, _, _, journal, store = self.rig_with_list(tmp_path)

        assert journal.state_for_origin("voice_note.todo", "nobody", 0) is None
        store.close()


class FailingNthWriteBridge(EchoingReminderBridge):
    """Fails the reminder writes whose (1-based) position is in ``failures``."""

    def __init__(self, *args, failures=None, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.failures: dict[int, Exception] = failures or {}
        self.writes = 0

    async def call(self, method, body=None, **kwargs):  # type: ignore[no-untyped-def]
        if method == "reminders.create":
            self.writes += 1
            if self.writes in self.failures:
                self.calls.append((method, body or {}, None, None))
                raise self.failures[self.writes]
        return await super().call(method, body, **kwargs)


class TestReminderBatch:
    """One wearer confirmation adds every reminder of a note, each written whole."""

    DUE = {"date": "2026-10-19", "time": "09:00", "time_zone": "America/Chicago"}

    @staticmethod
    def item(title: str, key: str, **due: str) -> tuple[ReminderIntent, ActionOrigin]:
        fields = {**TestReminderBatch.DUE, **due}
        return (
            ReminderIntent(title=title, **fields),
            ActionOrigin(kind="voice_note.commitment", identifier=f"capture-1:{key}", ordinal=0),
        )

    @staticmethod
    def rig_with_list(tmp_path: Path, *, accept: bool = True, failures=None):  # type: ignore[no-untyped-def]
        bridge = FailingNthWriteBridge(
            containers=[reminder_container("Reminders", "list-1", default=True)],
            failures=failures,
        )
        return (bridge, *rig(tmp_path, bridge, accept=accept))

    @staticmethod
    def written_titles(bridge: FakeBridge) -> list[object]:
        return [call[1]["title"] for call in bridge.calls if call[0] == "reminders.create"]

    @staticmethod
    def batch_state(store: DurableStore) -> str:
        row = store.execute(
            "SELECT state FROM actions WHERE kind = 'reminders.create_batch'"
        ).fetchone()
        assert row is not None
        return str(row[0])

    async def test_one_confirmation_writes_every_reminder_whole(self, tmp_path: Path) -> None:
        bridge, controller, confirms, journal, store = self.rig_with_list(tmp_path)
        items = [
            self.item("Call the plumber", "a"),
            self.item("Send the invoice to Evan", "b"),
            self.item("Book the dentist for Friday", "c", date="2026-10-20"),
        ]

        result = await controller.create_reminder_batch(items, label="Plumbing and invoices")

        assert result == ReminderBatchResult(created=3)
        assert confirms.prompts == ["Add 3 reminders from “Plumbing and invoices”?"]
        assert self.written_titles(bridge) == [
            "Call the plumber",
            "Send the invoice to Evan",
            "Book the dentist for Friday",
        ]
        assert self.batch_state(store) == "succeeded"
        for _, origin in items:
            assert journal.state_for_origin(origin.kind, origin.identifier, 0) == "succeeded"
        # Each reminder was approved through the one batch, not by a confirmation of its own.
        assert store.execute(
            "SELECT count(*) FROM action_events WHERE reason_code = 'batch_approved'"
        ).fetchone()[0] == 3
        assert store.execute("SELECT count(*) FROM confirmation_challenges").fetchone()[0] == 1
        store.close()

    async def test_a_single_reminder_reads_in_the_singular(self, tmp_path: Path) -> None:
        _, controller, confirms, _, store = self.rig_with_list(tmp_path)

        result = await controller.create_reminder_batch(
            [self.item("Call the plumber", "a")], label="Plumber",
        )

        assert result.created == 1
        assert confirms.prompts == ["Add 1 reminder from “Plumber”?"]
        store.close()

    async def test_a_long_note_title_is_cut_to_fit_the_confirm_screen(
        self, tmp_path: Path
    ) -> None:
        _, controller, confirms, _, store = self.rig_with_list(tmp_path)

        await controller.create_reminder_batch(
            [self.item("Call the plumber", "a"), self.item("Send the invoice", "b")],
            label="Notes about the kitchen renovation, the invoice for the tiles, "
            "the plumber and everything I promised the school this week " * 2,
        )

        (prompt,) = confirms.prompts
        assert prompt.startswith("Add 2 reminders from “") and prompt.endswith("…”?")
        assert utf16_units(prompt) <= CONFIRM_PROMPT_MAX_UTF16_UNITS
        store.close()

    async def test_a_declined_batch_writes_nothing_and_can_be_asked_again(
        self, tmp_path: Path
    ) -> None:
        bridge, controller, confirms, journal, store = self.rig_with_list(tmp_path, accept=False)
        items = [self.item("Call the plumber", "a"), self.item("Send the invoice", "b")]

        first = await controller.create_reminder_batch(items, label="Plumbing")
        # The declined attempt left no reminder rows behind and wrote nothing.
        assert store.execute(
            "SELECT count(*) FROM actions WHERE kind = 'reminders.create'"
        ).fetchone()[0] == 0
        assert self.written_titles(bridge) == []
        assert self.batch_state(store) == "rejected"
        confirms.accept = True
        second = await controller.create_reminder_batch(items, label="Plumbing")

        assert first.declined and first.created == 0
        assert second == ReminderBatchResult(created=2)
        assert len(confirms.prompts) == 2
        assert self.written_titles(bridge) == ["Call the plumber", "Send the invoice"]
        assert journal.state_for_origin("voice_note.commitment", "capture-1:a", 0) == "succeeded"
        store.close()

    async def test_reminders_already_created_are_not_asked_or_written_again(
        self, tmp_path: Path
    ) -> None:
        bridge, controller, confirms, _, store = self.rig_with_list(tmp_path)
        first_two = [self.item("Call the plumber", "a"), self.item("Send the invoice", "b")]
        three = [*first_two, self.item("Book the dentist", "c")]

        await controller.create_reminder_batch(first_two, label="Plumbing")
        second = await controller.create_reminder_batch(three, label="Plumbing")
        third = await controller.create_reminder_batch(three, label="Plumbing")

        assert second == ReminderBatchResult(created=1, already=2)
        assert third == ReminderBatchResult(already=3)
        assert confirms.prompts == [
            "Add 2 reminders from “Plumbing”?",
            "Add 1 reminder from “Plumbing”?",
        ]
        assert self.written_titles(bridge) == [
            "Call the plumber", "Send the invoice", "Book the dentist",
        ]
        store.close()

    async def test_a_reminder_that_failed_before_writing_is_the_only_one_asked_again(
        self, tmp_path: Path
    ) -> None:
        bridge, controller, confirms, _, store = self.rig_with_list(
            tmp_path,
            failures={2: NativeBridgeUnavailable("not dispatched", possible_external_commit=False)},
        )
        items = [self.item("Call the plumber", "a"), self.item("Send the invoice", "b")]

        first = await controller.create_reminder_batch(items, label="Plumbing")
        second = await controller.create_reminder_batch(items, label="Plumbing")

        assert first == ReminderBatchResult(
            created=1,
            problems=("Reminders did not create the reminder. Check JOAT on the Mac.",),
        )
        assert second == ReminderBatchResult(created=1, already=1)
        assert confirms.prompts == [
            "Add 2 reminders from “Plumbing”?",
            "Add 1 reminder from “Plumbing”?",
        ]
        store.close()

    async def test_a_reminder_that_may_exist_makes_the_batch_uncertain_and_is_not_retried(
        self, tmp_path: Path
    ) -> None:
        bridge, controller, confirms, _, store = self.rig_with_list(
            tmp_path,
            failures={2: NativeBridgeUnavailable("reply lost", possible_external_commit=True)},
        )
        items = [self.item("Call the plumber", "a"), self.item("Send the invoice", "b")]

        first = await controller.create_reminder_batch(items, label="Plumbing")
        second = await controller.create_reminder_batch(items, label="Plumbing")

        assert first.created == 1
        assert first.problems == (
            "The reminder may have been created. Check Reminders before retrying.",
        )
        assert self.batch_state(store) == "uncertain"
        assert second.created == 0 and second.already == 1
        assert second.problems == first.problems
        assert len(confirms.prompts) == 1
        assert self.written_titles(bridge).count("Send the invoice") == 1
        store.close()

    async def test_a_reminder_with_no_usable_title_is_reported_and_the_rest_proceed(
        self, tmp_path: Path
    ) -> None:
        bridge, controller, confirms, _, store = self.rig_with_list(tmp_path)

        result = await controller.create_reminder_batch(
            [self.item("   ", "a"), self.item("Send the invoice", "b")], label="Plumbing",
        )

        assert result == ReminderBatchResult(
            created=1, problems=("Reminder title must be 1 to 200 visible characters.",),
        )
        assert confirms.prompts == ["Add 1 reminder from “Plumbing”?"]
        store.close()

    async def test_nothing_to_add_asks_nothing(self, tmp_path: Path) -> None:
        bridge, controller, confirms, _, store = self.rig_with_list(tmp_path)

        result = await controller.create_reminder_batch([], label="Plumbing")

        assert result == ReminderBatchResult()
        assert confirms.prompts == [] and bridge.calls == []
        store.close()

    async def test_without_the_native_bridge_nothing_is_asked(self, tmp_path: Path) -> None:
        store = DurableStore(tmp_path / "joat.sqlite3")
        confirms = ConfirmationHarness(accept=True)
        controller = ProductivityActionController(
            None, confirms, ActionJournal(store), TaskOccurrenceStore(store),
        )  # type: ignore[arg-type]

        result = await controller.create_reminder_batch(
            [self.item("Call the plumber", "a")], label="Plumbing",
        )

        assert result.refused == "Open JOAT on the Mac and connect Reminders first."
        assert confirms.prompts == []
        store.close()

    async def test_is_created_is_true_only_after_a_confirmed_write(self, tmp_path: Path) -> None:
        _, controller, confirms, _, store = self.rig_with_list(tmp_path, accept=False)
        _, origin = self.item("Call the plumber", "a")

        assert not controller.is_created(origin)
        await controller.create_reminder_batch([self.item("Call the plumber", "a")], label="P")
        assert not controller.is_created(origin)
        confirms.accept = True
        await controller.create_reminder_batch([self.item("Call the plumber", "a")], label="P")
        assert controller.is_created(origin)
        store.close()
