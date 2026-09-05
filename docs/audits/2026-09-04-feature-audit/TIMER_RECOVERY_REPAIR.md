# Timer restart recovery

Audit finding P1-TIMER remains open for restart recovery. The existing conversational timer tool already delegates to PomodoroService; recovery must preserve the same service and the current compact timer display.

## Reproduced behavior

`evidence/remediation/timer-restart-baseline.json` records three synthetic-clock scenarios through the production service constructor used by main. A running focus block with 20 minutes left, the same block paused with 20 minutes left, and a running short break with one completed block each became idle with 25 minutes and zero completed blocks after reconstruction. This is controlled service reconstruction evidence, not a report of a physical-glasses incident or a complete engine startup test.

## Implementation contract

1. Append migration 11 to the existing leased DurableStore without changing previous migrations. Store one typed checkpoint: phase, running state, phase length, remaining time, completed blocks, absolute deadline and save time.
2. Persist a proposed transition before changing the in-memory state, arming callbacks, publishing or reporting success. A failed write retains the previous state. Missing checkpoint means a new idle timer; malformed checkpoints must not silently overwrite progress.
3. Load without scheduling. Main explicitly activates recovery after SessionManager and PanelPublisher exist, before opening the transport. A running future deadline continues; paused time remains frozen.
4. If the recorded deadline expired while the engine was unavailable, reconcile that phase once, persist the next phase paused, and issue one truthful recovery notification. Never infer a backlog of completed focus sessions from downtime. Repeated recovery must not count it twice.
5. Preserve the saved running deadline on shutdown; cancel the TimerHandle and drain owned publications before sessions, transport and SQLite close. Recheck the wall deadline when a scheduled callback fires.
6. Keep current 25/5/15-minute cycle controls and existing HUD wire shape. Reconnection publishes the stored service state; it does not start a replacement timer.

Storage and service behavior are separate bounded agent assignments. Root owns main composition, execution, integration review and evidence acceptance. No live user database is opened by the test fixtures.

## Official sources and scope

Python documents epoch-based wall time and an undefined reference point for monotonic time. Persisting the existing Unix deadline makes it interpretable after engine or machine restart; live callbacks still need to check the deadline because the wall clock can change. [Python time documentation](https://docs.python.org/3/library/time.html#time.time), [monotonic clock semantics](https://docs.python.org/3/library/time.html#time.monotonic).

SQLite documents transaction atomicity across interruption and the filesystem assumptions behind durable commits. This implementation reuses JOAT's already verified transaction and single-writer configuration. The documentation supports the storage mechanism; temporary-database reopen, rollback and migration tests must establish this integration. [SQLite atomic commit](https://sqlite.org/atomiccommit.html).

Even Realities documents the startup-page prerequisite and serial image updates. This repair keeps the existing bridge and renderer; it adds Mac-owned recovery, not a new glasses timer API. [Official Even Realities SDK reference](https://github.com/even-realities/everything-evenhub/blob/main/plugins/everything-evenhub/skills/sdk-reference/SKILL.md).

The one-phase overdue recovery policy is a JOAT product decision. It is not a vendor requirement. Tests and simulator evidence remain separate from fresh physical-G2 acceptance.

## Acceptance work remaining

Reopen actual temporary SQLite files for running and paused phases, short and long breaks, overdue recovery, reset, repeated restart and saved-duration preservation. Verify failed writes retain state without success/notifications, shutdown drains held publications, timer callbacks recheck deadlines, migration preserves existing notes/actions, and PanelPublisher emits the restored values. Inspect the existing compact timer in the official simulator after the Mac is unlocked. No new simulator acceptance is claimed while it is locked.

## Failure presentation correction

The first recovery contract collection stops because the new persistence module does not exist yet (`timer-recovery-contract-red.log`); this is not additional product-defect evidence. The separate real reconstruction baseline above remains the defect evidence.

Two renderer regressions reproduce Overview and the expanded Timer ignoring a provided recovery error and painting Ready/25:00. Both now show the existing headline/error text using the current compact JOAT font; normal idle presentation remains unchanged. `timer-recovery-renderer-red.log`: 2 failed, 7 passed. `timer-recovery-renderer-after.log`: 9 passed. These record drawing calls, not simulator pixels.

## Current measured result before broader regression

`timer-restart-after.json` repeats all three reconstruction scenarios with a real temporary SQLite close/reopen. Running focus keeps its deadline and shows 18 minutes after two minutes offline; paused focus retains exactly 20 minutes; short break retains its completed block and shows 3 minutes. Baseline restored zero of three phases correctly; the current source restores all three. This uses a controlled clock and the production service/store, not full engine startup.

The first combined after suite reached 76 passing cases before a 15-second wrapper deadline. It is incomplete, not a passing full suite or a proven test failure; the broader gate will use a 240-second check timeout. Python typechecking passes 107 source files; Hub production build passes.

Three additional spoken recovery-command tests reproduced idle guards hiding the recovery error. The guards now distinguish an unavailable saved timer from a genuinely idle timer, allowing explicit Reset to replace the checkpoint. No new phase or wire schema is needed.

Migration 11 is additive; earlier migration statements remain unchanged. Older builds reject a newer durable-store schema by existing policy. This task has not started the production engine or migrated the wearer's database.

## Accepted host regression result

CPJ `job-mtnsrp19-ad8aac08` completed exit 0. Retained gate: 570 Python tests, 129 Hub tests across four suites, strict mypy on 107 source files, production Hub build. All 237 frozen files still matched at root's post-completion inspection. Gate receipt SHA-256: `3047f2c9f35da2aee7c32eef0a5a71bf5bfdf3e5b7a851a1f28ec6b9d67b1a49`.

The earlier focused timeout is superseded by this completed regression run. Restart behavior remains measured by the three synthetic SQLite close/reopen scenarios, from 0/3 to 3/3 expected recoveries. This does not establish full engine startup, simulator rendering, a production database migration, or physical G2 acceptance.

The Anti-Churn plugin helper is no longer installed at its previous path; no plugin acceptance record was forged. The original reservation, frozen candidate, evaluator, gate receipt, logs and root source comparison retain the evidence directly.
