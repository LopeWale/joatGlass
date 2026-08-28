# Changelog

## 2026-08-27

- Added a production-safe, default-off G2 diagnostic receipt. An authenticated
  Mac opt-in now captures only page transitions, gesture classes, ordered
  four-tile upload outcomes, and HUD self-heal decisions; the Mac shell exports
  a separately allowlisted, content-free archive. The official Even Hub
  simulator exercised the real gateway/plugin path and retained a 576×288
  confirmation frame plus ordered upload, tap, show, and dismiss receipts.
  The trace drops while disconnected and persists via a coalesced background
  writer, so it cannot evict real uplinks or block a control session. Focused
  plugin coverage is 88 passing tests; Python unit/integration, strict mypy,
  and Swift shell tests all pass. The unrelated shared menu-label
  edit currently leaves the broad Hub-plugin suite with one stale expectation.

- Replaced the Confirmation surface's stacked `RIGHT / RING / NOD` and `LEFT`
  instructions with one horizontal, source-agnostic CTA row: `TAP · Confirm`
  and `SWIPE · Cancel`. Either built-in G2 temple now confirms, either swipe
  direction cancels, and optional R1 compatibility remains invisible so the
  decision never implies that a ring is required.
- Wired the explicit simulator Confirmation fixture through the production
  decision gate instead of merely painting it. Simulator Click now accepts and
  dismisses; Up/Down refuses and dismisses; timeout remains fail-closed. IMU
  motion is diagnostic-only and can no longer silently approve an action that
  advertises TAP as its sole acceptance gesture.
- Verified the corrected fixture in the official Even Hub simulator: the live
  trace reached `confirmation-visible`, Click settled `accepted=true`, and a
  fresh run's Up gesture settled `accepted=false`; both restored the prior HUD.
  All 189 Hub-plugin tests and the production TypeScript/Vite build pass.
  Repacked v0.1.11 as a 55,731-byte `joat.ehpk` with SHA-256
  `78925309946a28bcbdbbcf3002b9aa7e5cf85f745092c6e85544b425cd6e6705`.
- Completed a research-first design gate for Calendar, Reminders/tasks, AI
  notes, safe write actions, persistence, and notification fan-in. The design
  records platform permission boundaries, failure states, privacy constraints,
  adversarial acceptance tests, and deliberately unproven claims in
  `docs/PRODUCTIVITY_SYSTEM_RESEARCH.md`.
- Corrected three implementation assumptions before code: query-plus-create
  needs full Calendar access; ordinary reminders should remain EventKit's
  system of record rather than a parallel scheduler; and the current audio
  relay permits only gap-aware capture, not an absolute lossless guarantee.
- An independent hostile review exposed an untrusted-pairing P0 and upstream
  data-loss seams. Reordered delivery so transport identity, exact full-preview
  UX, privacy redaction, durable typed actions, SQLite reconciliation, and a
  bidirectional native Swift bridge precede Calendar and Reminder writes. No
  production implementation was started pending owner approval and resolution
  of the active concurrent worktree changes.
- Repaired the Messages reply path end to end. Incoming cards now carry
  explicit notification provenance and render as `MESSAGE`/`NOTIFICATION`
  instead of the fabricated `REPLY`; selecting a one-to-one Messages row arms
  a voice draft, shows the exact recipient and full draft on the exclusive
  Confirmation screen, and reaches Messages.app only after acceptance.
- Kept group chats explicitly read-only so a group response cannot be
  misrouted to one participant, and reject drafts that cannot fit completely
  on the 120-character confirmation surface. The eight-second visible decision
  window is preserved inside a twelve-second transport envelope so HUD paint
  time cannot silently shorten wearer consent.
- Added privacy-safe action correlation across plugin selection, confirmation
  visibility/verdict, real WebSocket gateway, and final sender outcome. Removed
  raw notification, transcript, reply, and queued-card content from operational
  logs; regressions assert that neither message drafts nor recipient handles
  appear in captured logs.
- Added a safe ordinary-simulator trace harness that uses the production reply
  controller and real gateway but a recording final sender. Official simulator
  evidence reached `confirmation-visible`, accepted one Click, resolved
  `confirmation-result accepted=True`, and reached exactly one recording-send
  boundary. The fixture is now visibly named `SIMULATOR TEST (NO SEND)` and
  contains no real-looking contact or message, so its extra window cannot be
  confused with the ordinary live JOAT simulator; the built-in confirmation
  visual state uses the same unmistakable copy.
- Routed simple salutations through a deterministic pre-inference command:
  `Good morning, JOATs.` now yields `Good morning. How can I help?` without
  requesting personal grounding. Greeting-plus-request speech still reaches
  the general reply path, unaddressed greetings remain silent, and wake-word
  punctuation cleanup no longer leaves doubled commas.
- Cleared unowned Main/Context/Tray content when a fresh Mac brain connects, so
  a reply painted by an older process cannot masquerade as a new response.
  Every completed, ignored, or empty utterance now also returns the transient
  `HEARING YOU` chrome to truthful `LISTENING` state, while an active session
  retains its own status through speech and reconnects.
- Current verification: 309 Python tests, including 44 real-gateway integration
  tests; 184 Hub-plugin tests; strict mypy across 47 source files; clean
  TypeScript/Vite production build and diff whitespace. Repacked v0.1.11 as a
  55,849-byte `joat.ehpk` with SHA-256
  `3f1c56e948fc28048fe6a010610beed0cb5ad2fb84245e7f6035675cbcbb503f`.
- Repaired the ordinary, fixture-free Overview state that had fabricated
  `REPLY / Nothing scheduled`, an orphan notification bell, and an empty
  `LAST QUERY`. Connecting, reconnecting, offline, and connected-empty sessions
  now use one named full-focus state with truthful recovery guidance; partial
  Overview payloads render only the semantic regions actually supplied.
- Retained the latest `MacLink` lifecycle state and replay it when Chrome is
  created, closing the fast-local-gateway race that could leave a connected
  session visually stuck on `CONNECTING` and hide new content as stale.
- Added regressions for status-only boot, stale content during reconnect,
  partial Overview frames, the populated Overview fixture, and late HUD link
  observation. A fresh ordinary simulator session reached `JOAT · LISTENING`
  and the intentional `Ready` screen; the populated Overview output remained
  byte-for-byte identical to the prior accepted capture.
- Current verification: 177 Hub-plugin tests, TypeScript/Vite production
  build, 98 focused Python pairing/protocol/capability/real-gateway tests,
  strict mypy across 42 source files, and clean diff whitespace. The named
  real-gateway regression still completes pairing, retains unauthenticated
  `401`, and rejects both the full pairing token and its prefix from emitted
  logs. Repacked v0.1.11 as a 55,306-byte `joat.ehpk` with SHA-256
  `d2d4502e4eff28059de1448f4f64670742a82e99d47b108e307e5610c5fa79b4`.
- Replaced the full-width capability menu with a transient 160px left Sidebar
  that appears only on left-temple double-click, keeps a dimmed live Overview
  visible at right, and disappears into a full-focus destination on selection.
- Replaced the firmware-default text/list presentation with a custom 576×288
  Gray4 HUD renderer: four SDK-safe 288×144 image tiles form one rounded visual
  surface while an empty brightness-zero text layer preserves real gestures.
- Added a consistent Phosphor icon family, continuous status spine, measured
  typography, structured message rows, custom selection states, and a live
  confirmation countdown; image uploads are globally serialized to prevent a
  torn frame across quadrants.
- Added four explicit HUD surfaces—Overview, Sidebar, full-focus content, and a
  dedicated Confirmation screen—with buffered Overview restoration and
  truthful per-state gesture copy.
- Added regression coverage for exact 576×288 geometry, single-capture and
  z-order invariants, Sidebar-to-Messages wiring, confirmation restoration, and
  the unchanged left/right double-click split.
- Visual verification used the official Even Hub simulator against the real
  presenter/router path: all four states were compared beside the approved
  visual reference, a simulator scroll moved the custom Sidebar selection, and
  click replaced the Sidebar with the selected loading focus page. The retained
  evidence and final severity audit are in `design-qa.md`.
- Re-ran the visual build from the approved source image and corrected the
  measured status, Overview, Sidebar, list, and Confirmation anchors. The
  renderer now sentence-cases destination titles and uses the matching dotted
  message/calendar assets.
- Repaired the live image protocol boundary: antialiased 8-bit canvas pixels
  are now quantized to the SDK's unpacked Gray4 values 0...15 before upload.
  This preserves focal/support/chrome hierarchy instead of clipping most
  authored intensities to full green in the simulator and glasses.
- Closed the final cross-module failure modes before freeze: newest-tail reply
  visibility, complete long-prompt wrapping, later-row reachability, countdown
  and input-gate alignment, structured mic truth, preserved Mac urgency,
  coalesced pending frames, active rollback after a partial four-tile upload,
  and capability-response correlation across Back/newer selections.
- Removed redundant Mac capability status writes; focus titles now own that
  state, so a delayed capability response cannot stale Home for twelve seconds
  even when its list is correctly rejected.
- Prior visual-slice verification: 171 Hub-plugin tests; 98 focused Python
  capability/protocol/pairing/real-gateway tests; strict mypy across 42 source
  files; TypeScript/Vite production build; no production source map or DEV
  fixture marker; clean diff whitespace. The real-gateway regression preserves
  the pairing handshake and unauthenticated `401` while proving pairing tokens
  and token prefixes never reach captured logs.
- Established the earlier dashboard/focus/confirmation foundation and its
  single SDK event-capture invariant; the current Sidebar and 40/184/32px
  full-focus geometry above supersede that first full-width menu iteration.
- Added the exclusive confirmation state with explicit accept/cancel/timeout
  copy, automatic fail-closed timeout, input consumption, background-content
  deferral, and restoration of the interrupted dashboard or focus page.
- Moved Mac capability empty/setup/data states onto the shared focus-page
  protocol and documented the canonical runtime contract in `DESIGN.md`.
- Prior-slice verification: 118 Hub-plugin tests, 175 Python unit tests, TypeScript/Vite
  production build, strict mypy across 39 source files, clean Design.md and
  Frontend Design Premium audits, and real Python gateway → Hub plugin →
  official Even Hub simulator proof of confirmation timeout/restoration.

## 2026-08-26

- Repaired verbose WebSocket logging so pairing tokens are redacted from both
  authenticated request URLs and token-bearing pairing offer/ack frames.
- Added a real-gateway regression covering the TOFU offer and acknowledgment,
  authenticated reconnect, unchanged unauthenticated `401`, and captured-log
  token absence.
- Verification: `42 passed` in `tests/integration/test_hub_gateway.py`, `15
  passed` in `tests/unit/test_pairing.py`, and strict mypy completed with no
  issues across 39 source files.
