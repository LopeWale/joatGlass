# Changelog

## 2026-08-27

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
