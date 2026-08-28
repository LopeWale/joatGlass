# JOAT Productivity System: Research and Design Gate

**Status:** research complete; implementation requires human approval  
**Scope:** Calendar, Reminders/tasks, AI notes, safe write actions, persistence,
and notification fan-in  
**Evidence date:** 2026-08-27

## Why this document exists

The original capability audit correctly identified six high-value gaps, but a
literal implementation would create avoidable privacy, reliability, and safety
failures. This document separates repository facts, platform facts, design
decisions, and claims that still require customer-path proof.

The core product goal is not merely to add six menu destinations. It is to let
JOAT remember, schedule, and act without letting ambiguous model output become
an external side effect.

## Evidence standard

Every acceptance claim must identify its level:

1. **Contract proof** — deterministic parser, state machine, persistence, and
   adapter tests.
2. **Native integration proof** — the signed macOS app exercises the real
   framework, TCC denial/grant, sleep/restart, and read-back behavior.
3. **Simulator proof** — the actual presenter/router path shows the full draft,
   confirmation, receipt, and failure states at 576×288.
4. **Physical-G2 proof** — real gestures, timing, legibility, audio gaps, and
   reconnection are observed on the glasses.

Contract tests do not substitute for native, simulator, or physical-device
evidence. Tests should include counterexamples selected to falsify the design,
not only examples copied from the implementation.

## Existing system constraints

- The Calendar capability is still a placeholder in
  `apps/mac/joat/services/capabilities.py`.
- The generic Python `ToolBelt` executes parsed model-selected calls directly.
  It currently exposes reads, but it is not an acceptable home for future
  writes.
- The active worktree contains a separate, uncommitted Messages reply flow with
  draft and confirmation concepts. It is useful prior art, not a finished
  general action architecture.
- Production configuration still permits raw LAN `ws://`, carries the pairing
  credential in the connection URL, and can reopen trust-on-first-use when the
  pairing file is unreadable. The confirmation channel is therefore not yet a
  trustworthy authorization boundary for consequential writes.
- Cards, pending session cards, debate snapshots, and conversation history are
  memory-bound or short-lived. `status.json` is an operational projection, not
  a durable store.
- Startup selects one notification store rather than combining sources.
- The glasses audio relay can discard oldest queued frames while disconnected.
  A downstream note service cannot reconstruct frames it never receives. The
  wire includes source/speaker metadata, but the current Python audio callback
  discards it and has no persisted connection/session generation.
- `JOATShell` currently starts the Python engine as a child process but has no
  typed bidirectional action bridge.
- The signed app inspected during this research has hardened runtime enabled,
  but has neither Calendar/Reminders usage descriptions nor EventKit
  entitlements. Its current EventKit authorization states were
  `notDetermined`; this research did not request access. The shell is currently
  not sandboxed, so the missing entitlement is a packaging observation rather
  than an immediate blocker; the usage descriptions and TCC flows still need
  to be implemented and verified.
- `status.json` is a persistent filesystem artifact containing last transcript,
  reply, and card content, and debate output can reach `brain.log`. Calling it
  an operational projection does not make it ephemeral or privacy-safe.

## Corrected architecture

The six capabilities should be researched in the requested order, but they
cannot be implemented safely in that order. Safe actions, persistence, and the
native bridge are foundations for Calendar and Reminders writes.

```text
voice / G2 selection
        |
        v
intent candidate -- no side effects
        |
        v
deterministic validation -> durable action draft -> exact HUD preview
        |                                           |
        |                                  reject / expire / confirm
        |                                           |
        +-------------------------------------------v
                                        typed action executor
                                                |
                                  native Swift personal-data bridge
                                                |
                                    EventKit / UserNotifications
                                                |
                                  read-back / reconcile / receipt
```

The Python process should be the single SQLite writer. The Swift service owns
TCC-protected Apple frameworks. Their bridge must be typed, versioned, and
bidirectional: requests and responses cover writes, while unsolicited events
cover EventKit changes, permission changes, notification actions, and externally
completed reminders. Framing, peer authentication, timeouts, cancellation,
version skew, and crash ownership are part of the contract—not transport
details to decide after the action model.

## 1. Calendar

### Assumption challenged

“Add a native Swift EventKit bridge for querying and creating events” is
directionally correct, but it hides a material permission split. EventKit
write-only Calendar access can create events, yet it cannot query real
calendars or read back even the event it just created. Query plus create
therefore requires full Calendar access. Reminders has no write-only mode.

### Decision

- `JOATShell` owns EventKit and requests permission just in time.
- The requested query-and-create experience uses full Calendar access. A later
  privacy-minimal “quick add only” mode may use write-only access, but must not
  pretend it can select a real calendar or verify through read-back.
- V1 supports title, start, end or duration, all-day, selected calendar, time
  zone, and alarms. Calendar recurrence is excluded from V1 along with
  attendees, invitations, deletion, and recurring-series edits.
- Natural-language parsing produces a candidate only. Foundation performs
  deterministic validation with an explicit time zone and time-matching policy.
- Persist the EventKit local identifier, external identifier when present, and
  a cached fingerprint. Neither identifier is a universal durable primary key.

### Adversarial findings

A direct Foundation probe for `2026-03-08 02:30 America/Chicago` found an
invalid local date. Permissive matching policies silently produced either
03:00 or 03:30, while strict matching moved to another day. For the repeated
`2026-11-01 01:30`, the first and last policies selected different UTC offsets.
JOAT must ask the user how to resolve a DST gap or repeated time instead of
silently choosing one.

### Proof gate

- Denied, restricted, not-determined, and granted TCC paths.
- Assert the macOS 15 authorization states precisely, including `fullAccess`
  and `writeOnly`, rather than collapsing them into “granted.”
- Create in a disposable calendar, query it, and compare read-back fields.
- DST gaps, repeated times, all-day events, time-zone travel, and calendar
  deletion between preview and execution.
- Crash after EventKit commit but before SQLite receipt: reconcile before any
  retry.
- Read-only/subscribed calendars, `allowsContentModifications`, event-store
  change invalidation during preview, account/source removal, external edits,
  sync delay, and calendar deletion between preview and commit.
- Exact full preview and receipt in the simulator and on physical G2.

## 2. Reminders and tasks

### Assumption challenged

Building an independent task store, recurrence engine, and alarm scheduler for
ordinary reminders would compete with the operating system and produce two
sources of truth. EventKit and the Reminders app already provide persistence,
sync, recurrence, alarms, and native completion.

### Decision

- Use `EKReminder` plus `EKAlarm` as the source of truth and primary delivery
  for ordinary reminders.
- JOAT stores provenance, action receipts, external references, monitor
  policies, and optional snooze overlays—not a second authoritative copy of
  every reminder.
- “Remind me…” defaults to a Reminder. “Schedule…” or “block time…” defaults to
  a Calendar event. Ambiguous requests require a question.
- JOAT-specific condition monitors use a separate persisted state machine only
  after an authoritative evidence query exists for that condition. The current
  Messages adapter reads incoming rows only, so it cannot truthfully evaluate
  “I have not replied by Friday.” Missing or stale evidence is `unknown`, not
  evidence that the condition is true.
- Local notifications may support snooze or “check this condition” prompts,
  but they cannot suppress a Reminders-app notification and are not guaranteed
  to arrive at an exact instant.

EventKit only exposes the first incomplete occurrence of a recurring reminder;
the next appears after completion. JOAT must not fabricate future reminder
objects and then claim they match Reminders.

### Monitor state model

```text
scheduled -> evaluating -> condition_clear -> completed
                       \-> condition_open -> notified -> acknowledged
                       \-> unknown | source_stale | permission_lost
                       \-> evaluation_failed | cancelled
notified  -> delivery_failed | snoozed -> scheduled
```

Each monitor definition names its authoritative query and maximum evidence age.
Each occurrence has a stable key, next-evaluation time, time zone, evidence
timestamp, evaluation result, delivery attempts, and acknowledgement. After
sleep or restart, JOAT performs bounded catch-up and never emits an
interruption storm.

### Proof gate

- Native create/read-back/complete behavior and recurring-reminder advancement.
- DST, time-zone change, sleep across due time, restart, duplicate evaluation,
  snooze, done, list deletion, and permission revocation.
- A monitor whose condition cannot be evaluated while JOAT is asleep must say
  “check condition” on wake; it cannot claim it monitored continuously.

## 3. AI note taker

### Assumption challenged

“Lossless NoteCaptureSession” is too strong for the existing transport. The G2
relay may drop queued audio under backpressure, and the current voice loop can
drop completed utterances while speech-to-text or reply generation is busy.
The honest contract is lossless persistence **within received frames**, with
detectable gaps and a visible degraded state.

### Decision

- Separate `QuickNote` from `MeetingNote`.
- Extend the public audio callback before capture: preserve source, optional
  speaker, sequence, and a connection/session generation. Then introduce an
  `AudioRouter`; note capture must not share the conversational reply loop or
  its `_busy` dropping behavior.
- Journal received segments to disk before transcription. Each segment records
  source, sequence, monotonic time, wall-clock time, checksum, and gap state.
- On a sequence gap, pause or visibly mark the session degraded. Never generate
  a seamless transcript that conceals missing audio.
- Show a persistent recording indicator, sound consent tones, and provide
  explicit pause, stop, save, discard, retention, export, and delete controls.
- If the journal write or recording indicator cannot be maintained, stop or
  pause capture immediately. A room recording must fail closed; “degraded” is
  reserved for an already-received stream with an explicitly marked source gap.
- Delete raw PCM after the transcript checkpoint by default unless the user
  explicitly enables recording retention.
- Summaries and action items are generated only after stop. Extracted actions
  remain drafts and go through the same confirmation pipeline as spoken ones.
- SQLite FTS5 supports local search when available; runtime probe it and retain
  a non-FTS fallback.

V1 covers glasses or microphone audio. System/meeting audio would require a
separate ScreenCaptureKit lane, screen-recording permission, and source-picker
UX. Speaker diarization is not a V1 claim.

### Proof gate

- A long capture with simultaneous transcription and no received-frame loss.
- Injected sequence gaps, disconnect/reconnect, full disk, quota pressure,
  process crash, app relaunch, pause/resume, and deletion.
- Transcript timestamps remain monotonic; a missing region remains explicit in
  raw transcript, summary, export, and search.
- Recording indicator and consent tones survive every HUD state on physical G2.
- Independent transport counters prove received, journaled, transcribed, and
  discarded frame counts; a transcript that merely contains every journal row
  is not sufficient proof of capture completeness.

## 4. Safe write actions

### Assumption challenged

A single confirmation call is necessary but insufficient. It does not prove
that the approved bytes are the executed bytes, prevent a duplicate after a
crash, or distinguish “the adapter accepted the call” from “the recipient or
system received the result.”

The uncommitted Messages flow already demonstrates a useful deterministic
controller boundary. The general design should build on that idea, but must add
durability, payload binding, reconciliation, and explicit verification levels.

### Decision

Consequential writes never enter the generic model-selected `ToolBelt`. A typed
action registry owns deterministic validation and execution.

Before this state machine can authorize anything, the transport must require an
explicit Mac-side pairing window, fail closed on corrupt or unreadable pairing
state, bind the confirmation to the authenticated device and connection
session, remove production raw-LAN WebSockets, and eliminate credentials from
query strings.

```text
drafted -> ready -> awaiting_confirmation -> rejected | expired
                                      \-> approved -> executing
                                                \-> succeeded | failed | uncertain
                                                                       \-> reconciled
```

- The confirmation token is single-use and binds action ID, schema version,
  canonical payload digest, target, and expiry.
- The G2 preview shows the complete consequential payload. If it cannot fit,
  require scrolling or a full Mac review; approval of truncated content is
  invalid.
- The current 120-character, non-scrolling confirmation surface cannot meet
  that requirement. Selecting and proving the full-review interaction is a
  prerequisite to finalizing action contracts, because it defines the exact
  bytes the user can authorize.
- Execution revalidates the digest, permission, target, and preconditions.
- An external write is never automatically retried after an uncertain crash.
  Reconcile first, then ask before a new attempt if the outcome is unknowable.
- Receipts state their verification level: executor accepted, read-back
  matched, delivery observed, or delivery unverified.
- “Submitted to Messages” is accurate when AppleScript exits successfully;
  “sent” requires independent read-back or delivery evidence.
- Tailscale protects data in transit but does not replace application pairing,
  action confirmation, or least-privilege grants.
- V1 does not perform compound writes across multiple external objects. A
  single Calendar event may include its own alarms; otherwise “schedule this
  and create a separate reminder” becomes two separately previewed actions.

### Proof gate

- Confirm another action ID, stale token, changed payload, expired action,
  double confirmation, double delivery callback, and permission revocation.
- Kill the process before commit, during commit, and after commit but before
  receipt; verify no blind retry.
- Long and Unicode payloads, hidden-control characters, target changes, and HUD
  disconnect during confirmation.
- Verify audit logs contain hashes and state transitions, not message, note, or
  calendar content.

## 5. Persistence

### Assumption challenged

SQLite is a good local foundation, but “turn on WAL and store everything” is
not an architecture. WAL still has a single writer, adds checkpointing and
sidecar lifecycle, and does not make EventKit plus SQLite one atomic
transaction.

### Decision

- Python is the single canonical database writer. Swift returns typed native
  results to Python instead of independently mutating the database.
- Start with SQLite's rollback journal. Adopt WAL only if measured concurrent
  readers require it.
- Enable foreign keys and a bounded busy timeout. Durable action intent commits
  with an explicitly tested `synchronous` policy before the native call. Define
  integrity-check, lock-contention, and multi-engine startup behavior.
- EventKit remains authoritative for Calendar and Reminders. SQLite stores
  durable action state, provenance, references, cached fingerprints, and
  reconciliation evidence.
- Cross-system writes use a saga: durable intent, native commit, read-back,
  durable receipt. An interrupted saga becomes `uncertain` and reconciles.
- `status.json` remains a persistent operational projection until redesigned.
  Before notes ship, remove or TTL sensitive content, redact `brain.log`, and
  define cleanup for SQLite journals, free pages, FTS shadow tables, exports,
  and backups. Card history becomes a query projection backed by the database.

Initial tables:

- `schema_migrations`
- `action_drafts`, `action_attempts`, `action_receipts`
- `external_items`
- `notes`, `note_segments`
- `notification_events`, `source_cursors`, `delivery_attempts`
- `monitors`, `monitor_occurrences`

The default database location is
`~/Library/Application Support/JOAT/joat.sqlite3`, created with user-only
permissions, versioned migrations, backup/restore documentation, bounded
content retention, and content-free operational logs. SQLite is not encryption
at rest; the initial privacy claim depends on macOS account protection and
FileVault. Stronger field or database encryption requires a separate threat
model and key-recovery design.

`secure_delete` alone cannot guarantee erasure across APFS copy-on-write,
backups, exports, and synchronized copies. The product must describe retention
honestly and minimize durable sensitive content rather than claim forensic
erasure it has not proven.

### Proof gate

- Forward migrations from every supported schema and refusal of unknown newer
  schemas.
- Atomic cursor-plus-event ingestion, duplicate occurrence keys, crash recovery,
  full disk, malformed database, backup/restore, file permissions, and FTS5
  unavailable.
- A fault-injection adapter creates EventKit success without a SQLite receipt;
  restart must reconcile instead of re-create.

## 6. Notification fan-in

### Assumption challenged

“Fan in Messages, Mail, Calendar, Reminders and general banners simultaneously”
would double-deliver many events. macOS also has no sanctioned API that lets an
app read every other app's Notification Center history. UserNotifications can
read only JOAT's own delivered notifications. The current Accessibility watcher
is a best-effort fallback, not an authoritative system feed.

### Decision

Use an authority-aware source mux:

```text
Messages database -------- authoritative for Messages --+
EventKit ----------------- authoritative Calendar/Tasks -+-> normalized store
Accessibility/Biome ------ best-effort catch-all --------+       |
Mail-specific adapter ---- optional, permissioned -------+       v
                                                        policy/outbox
                                                        /          \
                                               urgent overlay   routine queue
```

- Authoritative sources own their domain. Catch-all adapters suppress domains
  already covered by a healthy authoritative adapter.
- Redesign every adapter to emit `(source, opaque_native_id, cursor, has_more,
  observed_at, source_health)` before building the mux. The current AX watcher
  suppresses repeated text upstream, JSONL fabricates timestamp IDs, Biome can
  lose equal-timestamp rows, and Messages can stop with an undrained backlog.
  A downstream mux cannot recover events or identities already discarded.
- Deduplicate first by native ID. Cross-source reconciliation then uses bundle,
  sender, normalized content, and a narrow time window. A global text hash is
  unsafe because legitimate repeated alerts can have identical text.
- Persist source cursors and health. Restart performs bounded catch-up into a
  “What did I miss?” view rather than replaying everything as urgent.
- Deterministic Calendar/Reminder due events bypass model triage. A model may
  summarize routine content, but it does not decide whether an alarm existed.
- Urgent events may overlay an active session; routine events queue. The current
  all-cards queue does not meet this requirement.
- Use bounded per-source queues, fairness, backpressure, and a HUD render
  acknowledgement. The current best-effort gateway send cannot prove delivery
  merely because a frame was submitted while connected.
- Replace the unbounded full-content Accessibility JSONL with private IPC or a
  rotated, permission-restricted buffer and explicit retention.
- Mail completeness is not a V1 claim. A separately permissioned Mail adapter
  may be added later; the catch-all path remains best effort.

The current watcher performs repeated Accessibility scans on `@MainActor` with
a large fraction of every polling interval available to scanning. Move work off
the main actor only after proving the Accessibility calls remain safe; in all
cases, measure UI latency and enforce a budget.

### Proof gate

- The same Messages event observed by database and banner produces one card.
- Two legitimate identical alerts at different times remain two events.
- Authoritative-source failure and recovery, fallback promotion, restart
  catch-up, cursor truncation, permission loss, and active-session urgency.
- Verify unavailable domains are labeled unavailable or best effort; never
  report “all caught up” from an incomplete source set.

## Network boundary

Tailscale's data plane is end-to-end WireGuard encrypted whether traffic is
direct or relayed. That supports a private G2-to-Mac path, but network location
alone is not identity or authorization.

- Require explicit Mac-side pairing-window activation; never reopen pairing
  merely because the credential file is corrupt or unreadable.
- Keep JOAT's pairing token and rotate/revoke UX. Bind consequential
  confirmations to the authenticated device/session and payload digest.
- Restrict the JOAT service port with narrow tailnet grants.
- Use Tailscale Serve or a tailnet certificate to terminate HTTPS/WSS for
  tailnet clients when the WebView requires a secure context. Serve is not the
  public/LAN fallback; either provide separately authenticated native TLS there
  or decline that connection mode.
- Remove production raw-LAN `ws://` endpoints and query-string credentials
  before any write action ships. Use a mutually authenticated handshake over a
  protected transport.

## Recommended implementation order

1. Resolve ownership and stabilize the active Messages/provenance worktree.
2. Harden pairing, transport, confirmation authenticity, status/log privacy,
   and the recording-indicator fail-closed invariant. Select and prove the
   exact full-preview UX.
3. Define the typed bidirectional Swift bridge, durable action/SQLite contracts,
   and reconciliation behavior.
4. Migrate Messages reply onto that architecture as the first external-write
   vertical proof.
5. Repair adapter identity/backlog semantics and add the authority-aware
   notification source mux.
6. Deliver Calendar through draft, preview, EventKit, read-back, receipt,
   simulator, and physical G2.
7. Deliver Reminders. Add a monitor only after its authoritative evidence query
   and explicit unknown/failure behavior exist.
8. Deliver gap-aware QuickNote, then MeetingNote.
9. Run native TCC, restart/sleep, permission-revocation, simulator, and
   physical-G2 gates for each vertical slice, not only at the end.

## Deliberately out of scope for the first implementation

- Calendar attendees/invitations, deletion, and recurring-series edits.
- Guaranteed global Mail ingestion.
- System-audio meeting capture and speaker diarization.
- Automatic execution of AI-extracted action items.
- iOS background delivery or locked-phone guarantees.
- Portal publication, package installation, or real-G2 acceptance without fresh
  retained receipts.

## Verification snapshots and limitations

- The repository stayed dirty and changed concurrently throughout research at
  committed `HEAD 6f130ca39a2dcc600793ace1c7b486c8e543fa2f`. An earlier focused
  Python run passed 105 unit and real-gateway integration tests in 2.19s; the
  hostile reviewer later observed 299 passing Python tests on a different dirty
  snapshot.
- A final full Python rerun did **not** pass: collection failed because the new
  untracked `tests/unit/test_commands.py` imports absent
  `joat.services.commands`. This is retained as an unstable-worktree blocker,
  not papered over with the earlier green result.
- On that final snapshot, `.venv/bin/mypy apps/mac/joat` passed 47 source files,
  Hub `npm test -- --run` passed 183 tests, `npm run typecheck` passed, and
  `git diff --check` passed.
- The current Python SQLite build reports SQLite 3.53.2 with FTS5 and JSON
  support. Swift compilation probes covered EventKit imports, recurrence APIs,
  and explicit DST gap/repetition behavior.
- The signed app bundle was inspected for hardened runtime, usage descriptions,
  and entitlements.
- No EventKit permission was requested and no real event or reminder was
  created. No fresh simulator or physical-G2 proof was collected for these six
  capabilities.
- Two requested read-only research subagents could not start because the
  mandated model quota was exhausted. No alternate model was substituted.
- Taskplane onboarding failed before touching the repository because its
  installed package is missing `taskplane/collision_registry.json`; this
  document manually preserves its research-only and human-approval boundary.
- Mobbin comparisons were unavailable under the installed plan. Visual Truth
  and ZzzOps exposed no callable capability in this session. Wolfram's detailed
  evaluator did not return usable results, so direct Foundation probes supplied
  the date-time evidence.
- An independent hostile repository review found the untrusted pairing window,
  upstream notification loss, discarded audio provenance, persistent privacy
  artifacts, and incomplete asynchronous bridge requirements. Those findings
  changed the architecture and implementation order above.

## Primary references

- Apple: [Accessing the event store](https://developer.apple.com/documentation/eventkit/accessing-the-event-store)
- Apple: [Creating events and reminders](https://developer.apple.com/documentation/eventkit/creating-events-and-reminders)
- Apple: [Creating a recurring event](https://developer.apple.com/documentation/eventkit/creating-a-recurring-event)
- Apple: [Calendar item identifiers](https://developer.apple.com/documentation/eventkit/ekcalendaritem/calendaritemidentifier)
- Apple: [Scheduling a notification locally](https://developer.apple.com/documentation/usernotifications/scheduling-a-notification-locally-from-your-app)
- Apple: [Reading JOAT's delivered notifications](https://developer.apple.com/documentation/usernotifications/unusernotificationcenter/getdeliverednotifications%28completionhandler%3A%29)
- Apple: [Foundation Calendar](https://developer.apple.com/documentation/foundation/calendar)
- Apple: [NSDataDetector](https://developer.apple.com/documentation/foundation/nsdatadetector)
- Apple: [ScreenCaptureKit](https://developer.apple.com/documentation/ScreenCaptureKit)
- SQLite: [Write-ahead logging](https://www.sqlite.org/wal.html)
- SQLite: [Transactions](https://www.sqlite.org/lang_transaction.html)
- SQLite: [FTS5](https://www.sqlite.org/fts5.html)
- IETF: [RFC 5545 recurrence rules](https://www.rfc-editor.org/rfc/rfc5545)
- Tailscale: [End-to-end encryption](https://tailscale.com/docs/concepts/tailscale-encryption)
- Tailscale: [Serve](https://tailscale.com/docs/reference/tailscale-cli/serve)
- Tailscale: [Grants versus ACLs](https://tailscale.com/docs/reference/grants-vs-acls)
- NIST: [Zero Trust Architecture](https://csrc.nist.gov/pubs/sp/800/207/final)

## Human approval gate

Implementation must not start until the owner approves:

1. the dependency-first implementation order;
2. transport/pairing and full-preview hardening before any external write;
3. full Calendar and Reminders access for query-plus-create;
4. EventKit/Reminders as system of record for ordinary tasks;
5. the gap-aware rather than absolute-lossless note contract;
6. local reversible QuickNote semantics (save-plus-undo) versus confirmation for
   every note; and
7. the stated V1 exclusions.
