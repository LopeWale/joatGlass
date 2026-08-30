# JOAT Productivity System: Research and Design Gate

**Status:** research complete; approved safe V1 implemented. Contract/build and
the complete official-simulator interaction flow have been exercised; signed-app
native EventKit and physical-G2 wearer acceptance remain explicit operator-run
gates.
**Scope:** Calendar, Reminders/tasks, AI notes, safe write actions, persistence,
and notification fan-in  
**Evidence date:** 2026-08-27

## Approved implementation snapshot

The owner approved the dependency-first plan. The repository now contains:

- an owner-only, single-writer SQLite store with immutable migration checks,
  a replay-resistant action journal, bound confirmation evidence, attempts
  committed before external calls, and honest success/failure/uncertain
  receipts;
- hardened pairing and session-bound exact-preview confirmation for Messages,
  Calendar, and Reminders writes;
- a typed, versioned, owner-only Swift Unix-socket bridge with one long-lived
  `EKEventStore`, explicit Mac-UI permission requests, store-generation
  invalidation, strict civil-time resolution, exact writable-container IDs,
  nonrecurring event/reminder creation, reminder completion, and normalized
  read-back;
- deterministic voice forms for `schedule … on YYYY-MM-DD at HH:MM for …
  minutes in IANA/Zone`, `remind me to … on … at … in IANA/Zone`, and
  `complete reminder EXACT TITLE`. Ambiguous calendars/lists, DST gaps,
  repeated local times, stale generations, duplicate titles, and recurring
  completion all refuse before a write;
- EventKit alarms as the primary reminder delivery plus persisted JOAT-created
  one-off occurrences for bounded overdue catch-up after restart. Before the
  overlay alerts, it verifies that the native Reminder is still incomplete;
  unknown evidence defers instead of producing a false alert. JOAT records
  `alert_enqueued` after the Mac HUD write, not a false wearer-seen receipt;
- text-only QuickNotes with exact visible transcript retention, local Unicode
  search, immediate reversible undo/restore, and owner-only Markdown export.
  Explicit export includes every active note rather than a hidden 100-note cap.
  Meeting recording, raw audio retention, and AI-generated summaries remain
  disabled because the existing audio relay cannot prove complete capture;
- independent Messages, Accessibility-banner, and available metadata lanes
  feeding a durable source-cursor/observation/outbox mux. Raw observations are
  retained; each best-effort Messages presentation can consume at most one
  nearby authoritative observation with identical sender and content, so event
  cardinality is preserved. Stable native identities survive overlapping or
  reordered batches, JSONL rewrites, and Biome timestamp ties; payload digests
  are verified on load; metadata-only rows never become opaque HUD cards; a
  native heartbeat exposes a stale Accessibility watcher; and the production
  path uses deterministic raw delivery without model-triage head-of-line delay;
  restart holds old fallback observations until authoritative catch-up, while
  malformed complete feed lines mark the source `parse_degraded`; and
- owner-only status, banner, debug, brain-log, database, note-export, and lock
  file modes. Accessibility diagnostics no longer persist title/body content.

Contract acceptance currently includes restart recovery, corrupted-schema
rejection, 45-row Messages backlog drain, independent source failure, raw
duplicate retention, strict DST cases, stale EventKit generations, exact
read-back and alarm mismatch, ambiguous post-write disconnect, 101-note export,
externally completed reminders, overdue catch-up, corrupted notification
payloads, stalled triage, and delivery acknowledgement at the Mac transport
boundary. These tests do not establish TCC, notification display, simulator,
or physical-G2 behavior.

### HUD flow delivered on 2026-08-28

#### Current unified Planning flow (updated 2026-08-30)

Calendar events and incomplete Reminders are deliberately **not** two competing
wearer destinations. EventKit remains two native stores, but JOAT projects their
read-only data into one `Planning` hierarchy:

```text
Planning month (fixed Monday-first 6 × 7 grid, E#/R# count per day)
  -> tap a displayed week band
Planning week (seven days, event/reminder counts)
  -> tap a day
Planning day (time-ordered mixed Event and Reminder rows)
  -> tap an item for read-only detail
```

The app re-queries both native stores at each scope change. A missing
authorization or bridge is a truthful `SETUP`/`UNAVAILABLE` result, never an
unbounded loading frame. Calendar and Reminder creation use distinct review and
verification surfaces, but every terminal outcome returns to the native-time
anchored day in this shared Planning view (or the wearer can use left double
press to return earlier). Reminder `SAVED` passes through a separate
`MONITORING` surface; `DUE` and `COMPLETED` are also explicit, content-safe HUD
states.

The official simulator has driven this full 34-transition fixture-backed path:
month/week/day/detail; Calendar review, verifying, saved, failed, check,
cancelled and not-saved; and Reminder review, verifying, saved, monitoring,
due, completion review, completed, failed, check, cancelled and not-saved. It
exercises the actual Hub renderer, Mac-to-Hub transport and action routing, but
does **not** read a person's EventKit data, prove a native OS banner, or prove a
physical G2 gesture. Those remain separate acceptance levels.

The capability UI now uses one shared 576×288 shell and one explicit flow.
The table below is retained as the historical 2026-08-28 design snapshot; its
separate Calendar/Tasks labels have been superseded by the unified Planning
flow above:

| Area | Collection state | Selection state | Write/result state |
|---|---|---|---|
| Notifications | recent card headers with age | complete two-line card detail | read-only |
| Calendar | next seven days | title, start, and end detail | exact preview, `VERIFYING`, then `DONE`, `CHECK`, or `FAILED` |
| Tasks | incomplete reminders with due metadata | due detail plus exact-title completion guidance | same durable write states as Calendar |
| QuickNotes | newest exact-text notes | complete exact text | local reversible note commands; no ambient recording claim |
| Activity/persistence | payload-free durable action summaries | state plus short action reference | no payload, target, or confirmation secret appears |

The current contextual capability menu puts `Planning` directly in its
continuous, scrollable first-level list rather than exposing independent
Calendar and Tasks roots. It preserves the same rail, type scale, Gray4
hierarchy, and Phosphor regular icon family. Populated collection rows show
`tap detail`; setup, empty, detail, and action-lifecycle pages are read-only
except for the explicit safe-write confirmation action. The current simulator
evidence is renderer-and-routing evidence from the official Even simulator;
physical-G2 acceptance remains distinct.

The review hardening pass binds each list render and row tap to a unique HUD
generation, rather than trusting capability titles or one mutable snapshot.
Selectability is a protocol boolean shared by the Mac, menu, presenter, and
renderer. Failed Sidebar/focus paints close to Overview before any hidden UI
state can accept input, and populated rows activate only after their complete
replacement frame paints successfully. Reminder completion now binds both the EventKit item ID
and the exact list ID shown in the preview; Swift refuses a moved reminder and
Python requires the same list in normalized read-back. Finally, failures while
canonicalizing or persisting a post-EventKit receipt transition the attempt to
`uncertain` and publish `ACTION · CHECK`, preventing a verified native commit
from stranding the HUD at `VERIFYING` or inviting a duplicate retry.

### Known release blockers and exclusions

- A crash after EventKit commits but before Python records the read-back is
  correctly quarantined as `uncertain`, but automatic EventKit reconciliation
  is not implemented. Clean success receipts atomically retain the external
  identifier; uncertain actions still require manual inspection.
- The current receipt retains EventKit's local calendar-item identifier, not
  the external identifier. Both may change, so automatic reconciliation still
  needs multiple identifiers plus a cached fingerprint and explicit ambiguity
  handling.
- The owner-only Unix socket now admits only the exact engine PID launched by
  the signed JOAT app and revokes that identity on exit. This prevents an
  arbitrary same-account client from using the bridge; it does not protect
  against a compromised admitted engine.
- Raw `ws://` remains available. Pairing, URL-credential rejection, and bound
  confirmation are implemented, but production write traffic still needs WSS
  or a Tailscale Serve/tailnet-certificate deployment proven end to end.
- The installed signed app has Calendar and Reminders full access. One
  OpenClaw/Hermes candidate traversed exact simulator confirmation, EventKit
  create, independent read-back, durable receipt, payload purge, exact-title
  cleanup, and native absence verification. Reminder banner delivery,
  sleep/wake catch-up, and physical-G2 acceptance remain open.
- QuickNote soft deletion intentionally retains text for undo. Permanent erase,
  bounded notification/note retention, export cleanup, and backup behavior
  remain product/privacy policy work.
- The Accessibility startup tail is bounded rather than lossless, malformed
  lines are discarded after health is degraded, and source reads have no
  per-poll timeout. A producer/consumer acknowledgement protocol, quarantine,
  rotation/TTL, and a bounded authoritative-read deadline remain required for
  a strong notification guarantee.
- MeetingNote, raw-audio journaling, recurrence, snooze, Calendar invitations,
  guaranteed Mail coverage, and plugin render acknowledgement remain excluded.

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

## Baseline repo facts at research start

The bullets in this section record why the original recommendation was not
safe to implement literally. The approved implementation snapshot above
supersedes the gaps that were subsequently closed.

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
  Before release, remove or TTL sensitive content, redact `brain.log`, and
  define cleanup for SQLite journals, free pages, FTS shadow tables, exports,
  and backups. Card history becomes a query projection backed by the database.

Implemented V1 tables:

- `schema_migrations`
- `actions`, `action_payloads`, `action_events`, `confirmation_challenges`,
  `action_attempts`, `action_receipts`
- `external_items`, `external_identifiers`
- `quick_notes`
- `notification_sources`, `notification_observations`, `notification_outbox`
- `task_occurrences`, `task_alert_receipts`

The default database location is
`~/Library/Application Support/JOAT/joat.sqlite3`, created with user-only
permissions and versioned migrations. Backup/restore behavior and bounded
content retention remain release work. SQLite is not encryption at rest; the
initial privacy claim depends on macOS account protection and FileVault.
Stronger field or database encryption requires a separate threat model and
key-recovery design.

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

- Authoritative sources own their domain, but health alone is never evidence
  that a particular event was observed. Exact nearby matches are consumed by
  cardinality: `N` authoritative observations can suppress at most `N`
  identical fallback appearances; any additional appearance remains visible.
- Every adapter emits a stable source-local native identity and durable cursor.
  The implemented fixes preserve repeated AX appearances, use JSONL byte
  identities bound to a detected file generation, retain all Biome identities
  at the timestamp boundary, and fully drain Messages pages. On first startup,
  JSONL scans only a five-second/256 KiB tail and requires a fresh content-free
  native heartbeat. That narrows the observed baseline-at-EOF race but does not
  turn an unsupported Accessibility scrape into a completeness guarantee. In
  particular, a same-launch line older than that bounded window can still be
  lost; a producer-generation/acknowledged-offset handshake is the robust fix.
- Deduplicate first by native ID. Cross-source reconciliation then uses bundle,
  sender, normalized content, and a narrow time window. A global text hash is
  unsafe because legitimate repeated alerts can have identical text.
- Persist source cursors, health, observations, and outbox state. Restart now
  performs bounded pending delivery; a durable “What did I miss?” query view is
  still future work.
- Deterministic Calendar/Reminder due events bypass model triage. A model may
  summarize routine content, but it does not decide whether an alarm existed.
- Urgent events may overlay an active session; routine events queue. The current
  all-cards queue does not meet this requirement.
- Independent ingestion prevents delivery work from stalling source cursors.
  Production currently bypasses model triage entirely, and durable outbox state
  advances only after the Mac transport write. The Hub/plugin protocol still
  lacks a render acknowledgement, so this is not wearer-seen proof.
- On restart, an authoritative source is `stale` until its first successful
  cursor commit, so old fallback rows cannot outrun catch-up. Malformed complete
  Accessibility lines advance the cursor but make health `parse_degraded`;
  content-free quarantine and replay tooling remain future work.
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

## Recommended implementation order (historical)

This is the dependency-first order approved at research time. Its foundation
and Calendar/Reminder verticals are now implemented; the remaining work is the
operator-run native/physical acceptance described above, not a return to split
Calendar and Task menus.

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

## Verification snapshots and limitations (historical 2026-08-27)

- The current dirty shared checkout passed 422 Python tests and strict mypy
  across 61 source files. Hub verification passed 198 tests plus the
  TypeScript/Vite production build. Native verification passed 18 XCTest tests
  plus 3 Swift Testing tests, including strict local-time, bridge digest,
  cancellation, socket replacement, and first-sweep notification cases.
- A Developer ID archive/export completed, passed strict signature verification,
  and was installed atomically with a retained prior bundle. The app is
  universal `x86_64 arm64`; its signed entitlement contains
  `com.apple.security.personal-information.calendars`, and its Info.plist
  contains both Calendar and Reminders full-access explanations. It is not
  notarized.
- The current Python SQLite build reports SQLite 3.53.2 with FTS5 and JSON
  support. The V1 implementation deliberately uses rollback journaling rather
  than assuming WAL solves cross-system atomicity.
- Existing EventKit full-access grants were observed. A randomized synthetic
  Calendar event was created and read back through the installed production
  engine, then removed with zero exact-title matches remaining. Official
  simulator confirmation was captured; physical-G2 proof remains open.
- Taskplane onboarding failed before touching the repository because its
  installed package is missing `taskplane/collision_registry.json`; this
  document manually preserves its research-only and human-approval boundary.
- Mobbin comparisons were unavailable under the installed plan. Visual Truth
  and ZzzOps exposed no callable capability in this session. Wolfram's detailed
  evaluator did not return usable results, so direct Foundation probes supplied
  the date-time evidence.
- Three independent hostile reviews found the untrusted pairing window,
  upstream notification loss, missing Reminder alarm evidence, notification
  identity/integrity bugs, truncated note export, discarded audio provenance,
  persistent privacy artifacts, and asynchronous bridge races. Those findings
  changed both the architecture and implementation; unresolved release gates
  are listed above instead of being converted into success claims.

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

## Human approval outcome

The owner approved:

1. the dependency-first implementation order;
2. transport/pairing and full-preview hardening before any external write;
3. full Calendar and Reminders access for query-plus-create;
4. EventKit/Reminders as system of record for ordinary tasks;
5. the gap-aware rather than absolute-lossless note contract;
6. local reversible QuickNote semantics (save-plus-undo) versus confirmation for
   every note; and
7. the stated V1 exclusions.

That approval authorized implementation, not live TCC grants, real Calendar or
Reminders writes, Tailscale Serve changes, app installation, notarization, or
physical-G2 acceptance. Those remain explicit operator-driven proof steps.
