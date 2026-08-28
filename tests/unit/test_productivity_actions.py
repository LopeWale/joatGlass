"""Falsification tests for confirmed EventKit writes."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

from joat.core.confirmation import ConfirmationResult
from joat.native.bridge import NativeBridgeUnavailable
from joat.persistence.actions import ActionJournal
from joat.persistence.store import DurableStore
from joat.services.productivity_actions import ProductivityActionController


class FakeBridge:
    def __init__(self, *, containers, resolution=None, write_result=None) -> None:  # type: ignore[no-untyped-def]
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
        }
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
            commit_phase="read_only" if not method.endswith("create") else "committed_readback",
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
            challenge_id="challenge-1",
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


def rig(tmp_path: Path, bridge: FakeBridge, *, accept: bool = True):
    store = DurableStore(tmp_path / "joat.sqlite3")
    journal = ActionJournal(store)
    confirms = ConfirmationHarness(accept=accept)
    controller = ProductivityActionController(bridge, confirms, journal)  # type: ignore[arg-type]
    return controller, confirms, journal, store


class TestCalendarCreation:
    async def test_exact_event_is_confirmed_committed_and_read_back(self, tmp_path: Path) -> None:
        bridge = FakeBridge(containers=[calendar_container("Work", "cal-work", default=True)])
        controller, confirms, journal, store = rig(tmp_path, bridge)

        result = await controller.handle_command(
            "schedule Design review on 2026-09-01 at 09:30 for 45 minutes "
            "in America/Chicago"
        )

        assert result == "Created Calendar event: Design review."
        assert confirms.prompts == [
            "Create event “Design review”? Work · Sep 1, 09:30 · 45m · America/Chicago"
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
        store.close()

    async def test_decline_never_calls_eventkit_write(self, tmp_path: Path) -> None:
        bridge = FakeBridge(containers=[calendar_container("Work", "cal-work", default=True)])
        controller, _, _, store = rig(tmp_path, bridge, accept=False)

        result = await controller.handle_command(
            "schedule Design review on 2026-09-01 at 09:30 for 45 minutes "
            "in America/Chicago"
        )

        assert result == "Calendar event cancelled."
        assert all(method != "calendar.create" for method, *_ in bridge.calls)
        assert store.execute("SELECT state FROM actions").fetchone()[0] == "rejected"
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
            },
        )
        controller, confirms, _, store = rig(tmp_path, bridge)

        result = await controller.handle_command(
            "remind me to Buy filters on 2026-09-01 at 18:00 in America/Chicago"
        )

        assert result == "Created Reminder: Buy filters."
        assert confirms.prompts == [
            "Create reminder “Buy filters”? Personal · Sep 1, 18:00 · America/Chicago"
        ]
        create = bridge.calls[-1]
        assert create[0] == "reminders.create"
        assert create[1]["calendar_identifier"] == "list-personal"
        assert create[1]["alarm_offsets_seconds"] == [0]
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
