from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import joat.persistence.store as store_module
from joat.core.protocol import ProductivityHudView
from joat.persistence.actions import ActionDraft, ActionJournal
from joat.persistence.store import DurableStore
from joat.persistence.tasks import TaskOccurrenceConflict, TaskOccurrenceStore
from joat.services.task_monitor import (
    ReminderNativeState,
    TaskMonitor,
    evaluate_reminder_occurrence,
    native_reminder_matches_persisted_due,
    reminder_due_hud_view,
)


def action(journal: ActionJournal, *, now_us: int = 10) -> str:
    record = journal.create_draft(
        ActionDraft(
            kind="reminders.create",
            canonical_payload=b"{}",
            target_digest=hashlib.sha256(b"list").digest(),
            idempotency_key="reminder-action-1",
        ),
        now_us=now_us,
    )
    return record.action_id


def register(store: DurableStore, *, due_unix_ms: int = 1_000):
    occurrences = TaskOccurrenceStore(store)
    action_id = action(ActionJournal(store))
    occurrence = occurrences.register(
        action_id=action_id,
        external_item_id="eventkit-1",
        calendar_identifier="list-1",
        title="Take medicine",
        due_unix_ms=due_unix_ms,
        time_zone_id="America/Chicago",
        now_us=10,
    )
    return occurrences, occurrence


class ReminderEvaluationBridge:
    def __init__(
        self,
        *,
        reminder: dict[str, object] | None,
        list_generation: int = 7,
        query_generation: int = 7,
    ) -> None:
        self.reminder = reminder
        self.list_generation = list_generation
        self.query_generation = query_generation

    async def call(self, method: str, body=None):  # type: ignore[no-untyped-def]
        del body
        if method == "reminders.listContainers":
            return SimpleNamespace(
                store_instance_id="store-A",
                store_generation=self.list_generation,
                result=[{"calendar_identifier": "list-1"}],
            )
        if method == "reminders.query":
            return SimpleNamespace(
                store_instance_id="store-A",
                store_generation=self.query_generation,
                result=[] if self.reminder is None else [self.reminder],
            )
        raise AssertionError(f"unexpected native call: {method}")


def native_due_for(occurrence) -> dict[str, object]:  # type: ignore[no-untyped-def]
    value = datetime.fromtimestamp(
        occurrence.due_unix_ms / 1_000,
        ZoneInfo(occurrence.time_zone_id),
    )
    return {
        "year": value.year,
        "month": value.month,
        "day": value.day,
        "hour": value.hour,
        "minute": value.minute,
        "second": value.second,
        "time_zone_id": occurrence.time_zone_id,
    }


def test_reminder_due_hud_view_is_the_single_read_only_projection() -> None:
    view = reminder_due_hud_view(
        "!! Take medicine · Reminder\nDue now",
        planning_anchor_unix_ms=1_788_274_200_000,
    )

    assert (view.domain, view.phase, view.title) == ("reminders", "due", "Take medicine")
    assert (view.primary, view.secondary, view.hint) == (
        "Due now",
        "This reminder is still incomplete.",
        "say complete reminder title · left x2 Planning",
    )
    assert view.planning_anchor_unix_ms == 1_788_274_200_000
    assert reminder_due_hud_view(
        "!! Take medicine · Reminder\nDue now",
        planning_anchor_unix_ms=True,  # type: ignore[arg-type]
    ).planning_anchor_unix_ms is None


async def test_native_reminder_evaluation_is_exact_and_fail_quiet(tmp_path: Path) -> None:
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        _, occurrence = register(store)
        complete = ReminderEvaluationBridge(
            reminder={"calendar_item_identifier": "eventkit-1", "is_completed": True},
        )
        incomplete = ReminderEvaluationBridge(
            reminder={
                "calendar_item_identifier": "eventkit-1",
                "is_completed": False,
                "due": native_due_for(occurrence),
            },
        )
        missing = ReminderEvaluationBridge(reminder=None)
        stale = ReminderEvaluationBridge(
            reminder={
                "calendar_item_identifier": "eventkit-1",
                "is_completed": False,
                "due": native_due_for(occurrence),
            },
            query_generation=8,
        )

        assert await evaluate_reminder_occurrence(occurrence, complete) is ReminderNativeState.COMPLETED  # type: ignore[arg-type]
        assert await evaluate_reminder_occurrence(occurrence, incomplete) is ReminderNativeState.INCOMPLETE  # type: ignore[arg-type]
        assert await evaluate_reminder_occurrence(occurrence, missing) is None  # type: ignore[arg-type]
        assert await evaluate_reminder_occurrence(occurrence, stale) is None  # type: ignore[arg-type]


def test_native_due_match_requires_the_original_civil_time_and_zone(tmp_path: Path) -> None:
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        _, occurrence = register(store, due_unix_ms=1_788_303_600_000)
        matching = native_due_for(occurrence)

        assert native_reminder_matches_persisted_due(occurrence, {"due": matching})
        assert not native_reminder_matches_persisted_due(
            occurrence,
            {"due": {**matching, "day": 2}},
        )
        assert not native_reminder_matches_persisted_due(
            occurrence,
            {"due": {**matching, "time_zone_id": "America/New_York"}},
        )
        assert not native_reminder_matches_persisted_due(occurrence, {"due": None})


async def test_restart_catches_up_due_once_without_duplicate(tmp_path: Path) -> None:
    path = tmp_path / "joat.sqlite3"
    with DurableStore(path) as store:
        _, occurrence = register(store)

    cards: list[ProductivityHudView] = []
    with DurableStore(path) as reopened:
        occurrences = TaskOccurrenceStore(reopened)
        monitor = TaskMonitor(occurrences, cards.append)
        assert await monitor.run_due_once(now_unix_ms=2_000, now_us=2_000_000) == 1
        assert await monitor.run_due_once(now_unix_ms=3_000, now_us=3_000_000) == 0
        assert [
            (card.domain, card.phase, card.title, card.planning_anchor_unix_ms)
            for card in cards
        ] == [("reminders", "due", "Take medicine", 1_000)]
        assert occurrences._get_with(reopened._db, occurrence.occurrence_id).state == "alerted"


async def test_future_occurrence_is_not_alerted(tmp_path: Path) -> None:
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        occurrences, _ = register(store, due_unix_ms=10_000)
        cards: list[ProductivityHudView] = []
        assert await TaskMonitor(occurrences, cards.append).run_due_once(
            now_unix_ms=9_999, now_us=20
        ) == 0
        assert cards == []


async def test_enqueue_failure_leaves_pending_for_retry(tmp_path: Path) -> None:
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        occurrences, _ = register(store)
        calls = 0

        def flaky(_card: ProductivityHudView) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("HUD unavailable")

        monitor = TaskMonitor(occurrences, flaky)
        assert await monitor.run_due_once(now_unix_ms=2_000, now_us=20) == 0
        assert await monitor.run_due_once(now_unix_ms=2_000, now_us=30) == 1
        assert calls == 2


async def test_completed_native_reminder_is_completed_locally_without_alert(
    tmp_path: Path,
) -> None:
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        occurrences, occurrence = register(store)
        cards: list[ProductivityHudView] = []

        async def completed(_occurrence):  # type: ignore[no-untyped-def]
            return ReminderNativeState.COMPLETED

        monitor = TaskMonitor(occurrences, cards.append, evaluate=completed)
        assert await monitor.run_due_once(now_unix_ms=2_000, now_us=20) == 0
        assert cards == []
        assert occurrences._get_with(store._db, occurrence.occurrence_id).state == "completed"


async def test_unknown_native_state_defers_without_false_alert(tmp_path: Path) -> None:
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        occurrences, occurrence = register(store)
        cards: list[ProductivityHudView] = []

        async def unknown(_occurrence):  # type: ignore[no-untyped-def]
            return None

        monitor = TaskMonitor(occurrences, cards.append, evaluate=unknown)
        assert await monitor.run_due_once(now_unix_ms=2_000, now_us=20) == 0
        assert cards == []
        assert occurrences._get_with(store._db, occurrence.occurrence_id).state == "pending"


async def test_rescheduled_native_reminder_retires_old_local_due_overlay(
    tmp_path: Path,
) -> None:
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        occurrences, occurrence = register(store, due_unix_ms=1_788_303_600_000)
        cards: list[ProductivityHudView] = []
        bridge = ReminderEvaluationBridge(reminder={
            "calendar_item_identifier": "eventkit-1",
            "is_completed": False,
            "due": {**native_due_for(occurrence), "day": 2},
        })
        monitor = TaskMonitor(
            occurrences,
            cards.append,
            evaluate=lambda current: evaluate_reminder_occurrence(current, bridge),  # type: ignore[arg-type]
        )

        assert await monitor.run_due_once(
            now_unix_ms=1_788_303_600_001, now_us=20,
        ) == 0
        assert cards == []
        assert occurrences._get_with(store._db, occurrence.occurrence_id).state == "cancelled"


async def test_failed_native_evaluation_defers_without_stopping_monitor(
    tmp_path: Path,
) -> None:
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        occurrences, occurrence = register(store)
        cards: list[ProductivityHudView] = []

        async def broken(_occurrence):  # type: ignore[no-untyped-def]
            raise RuntimeError("EventKit unavailable")

        monitor = TaskMonitor(occurrences, cards.append, evaluate=broken)
        assert await monitor.run_due_once(now_unix_ms=2_000, now_us=20) == 0
        assert cards == []
        assert occurrences._get_with(store._db, occurrence.occurrence_id).state == "pending"


def test_completion_retains_prior_alert_receipt(tmp_path: Path) -> None:
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        occurrences, occurrence = register(store)
        alerted = occurrences.mark_alert_enqueued(occurrence.occurrence_id, now_us=20)
        completed = occurrences.mark_completed(occurrence.occurrence_id, now_us=30)

        assert completed.state == "completed"
        assert completed.alerted_at_us == alerted.alerted_at_us == 20
        assert completed.completed_at_us == 30


def test_current_migrations_upgrade_prior_v4_without_checksum_drift(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "joat.sqlite3"
    all_migrations = store_module._MIGRATIONS
    monkeypatch.setattr(store_module, "_MIGRATIONS", all_migrations[:4])
    with DurableStore(path) as store:
        action_id = action(ActionJournal(store))
        store.execute(
            "INSERT INTO task_occurrences(occurrence_id,action_id,external_system,"
            "external_item_id,calendar_identifier,title,due_unix_ms,time_zone_id,"
            "state,created_at_us,updated_at_us,alerted_at_us) "
            "VALUES ('occurrence-1',?,'eventkit.reminders','eventkit-1','list-1',"
            "'Take medicine',1000,'America/Chicago','alerted',10,20,20)",
            (action_id,),
        )

    monkeypatch.setattr(store_module, "_MIGRATIONS", all_migrations)
    with DurableStore(path) as upgraded:
        occurrences = TaskOccurrenceStore(upgraded)
        completed = occurrences.mark_completed("occurrence-1", now_us=30)
        assert upgraded.schema_version == all_migrations[-1][0]
        assert completed.alerted_at_us == 20


def test_registration_is_idempotent_but_rejects_changed_due_time(tmp_path: Path) -> None:
    with DurableStore(tmp_path / "joat.sqlite3") as store:
        occurrences, first = register(store)
        assert occurrences.register(
            action_id=first.action_id,
            external_item_id=first.external_item_id,
            calendar_identifier=first.calendar_identifier,
            title=first.title,
            due_unix_ms=first.due_unix_ms,
            time_zone_id=first.time_zone_id,
            now_us=999,
        ) == first
        try:
            occurrences.register(
                action_id=first.action_id,
                external_item_id=first.external_item_id,
                calendar_identifier=first.calendar_identifier,
                title=first.title,
                due_unix_ms=2_000,
                time_zone_id=first.time_zone_id,
                now_us=999,
            )
        except TaskOccurrenceConflict:
            pass
        else:
            raise AssertionError("changed task data must conflict")
