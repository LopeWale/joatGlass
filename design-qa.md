# JOAT G2 HUD — design QA

## Source visual truth

- Approved reference: `/Users/emmanuelakinwale/.codex/visualizations/2026/08/27/01a04173-649c-7d01-9f6a-803b07c3b7b2/joat-hud-redesign-v1.png`
- Source pixels: 1536×1024 presentation board containing four conceptual HUD states.
- Runtime target: official Even Hub simulator, exact G2 framebuffer 576×288.
- States compared: Overview, Sidebar open, Messages, Confirmation.

The source board draws each concept panel at approximately 1.70:1 while the G2
framebuffer is fixed at 2:1. Each source panel was therefore isolated and
normalized to 576×288 before comparison with a same-state 576×288 simulator
capture.

## Final comparison evidence

- Same-viewport four-state comparison, reference at left and live simulator at
  right:
  `/Users/emmanuelakinwale/.codex/visualizations/2026/08/27/01a04173-649c-7d01-9f6a-803b07c3b7b2/hud-design-qa/iteration-3/final-reference-vs-simulator.png`
- Enlarged Sidebar comparison:
  `/Users/emmanuelakinwale/.codex/visualizations/2026/08/27/01a04173-649c-7d01-9f6a-803b07c3b7b2/hud-design-qa/iteration-3/final-sidebar-focus.png`
- Exact simulator captures:
  `iteration-3/05-overview-after.png`, `06-sidebar-after.png`,
  `07-messages-after.png`, and `08-confirmation-after.png` in the same QA
  directory. Each `-raw` file retains the simulator's transparent green output;
  the named inspection image composites it over black without changing the
  pixels' geometry.
- Real gesture evidence:
  `iteration-3/09-sidebar-down.png` shows Notifications selected and
  `iteration-3/10-sidebar-click.png` shows the resulting Notifications loading
  page. The simulator console reported no errors.
- Normal, fixture-free runtime evidence:
  `iteration-4/07-normal-runtime-final.png` shows the repaired connected-empty
  Overview at the ordinary plugin URL, with no `joatVisual` fixture parameter.
- Same-viewport ordinary-runtime before/fixed comparison:
  `iteration-4/08-normal-runtime-before-vs-final.png` shows the reported
  fabricated/orphaned sections at left and the intentional `Ready` focus state
  at right.
- Populated-state regression comparison:
  `iteration-4/06-reference-vs-populated-regression.png`; its raw simulator
  capture is byte-for-byte identical to `iteration-3/05-overview-after-raw.png`.

## Screenshot-driven iteration history

1. **Pre-change — P1 visual failure:** fresh simulator captures showed the
   firmware's plain text/list containers rather than the approved authored HUD.
   The missing shell, icon family, dimmed Overview backdrop, structured rows,
   and decision hierarchy made the result read like unstyled HTML. The runtime
   was replaced with one custom frame split across four SDK-legal image tiles
   plus a transparent gesture-capture layer.
2. **First custom pass — P1 proportional drift:** a new fresh comparison showed
   a 220px Sidebar where the approved rail measures 160px, a 46px status area
   where the source divider sits at 35.5px, and shifted Overview/list/decision
   anchors. The first custom pass had implemented the right ideas but not the
   visual source's measured layout.
3. **Protocol audit — P1 hierarchy defect:** `updateImageRawData` consumes one
   unpacked 4-bit grayscale value per pixel. The renderer was uploading its
   authored 0...255 canvas bytes directly, which clipped most intensity levels
   to full green and erased the intended hierarchy. The final renderer keeps
   8-bit antialiasing internally and quantizes every outgoing pixel to 0...15.
   A regression test fixes the conversion boundary.
4. **Measured rebuild — pass:** moved the status baseline/divider, Overview
   content, 160px Sidebar, list row anchors, and Confirmation action/progress
   positions to the normalized source measurements. Replaced the empty message
   bubble and numbered calendar with the matching dotted Phosphor assets and
   restored sentence-case destination titles.
5. **Live interaction pass — pass:** drove real `down` and `click` input through
   `visual-gesture-layer`. Selection moved from Messages to Notifications and
   click replaced the Sidebar with the selected loading focus page. The
   simulator console contained no errors or uncaught exceptions.
6. **Hostile cross-module pass — pass:** preserved newest-tail reply visibility,
   complete long-prompt wrapping, later-row reachability, countdown/input-gate
   alignment, structured mic truth, Mac urgency, coalesced frames, active
   rollback after a partial tile upload, and capability-response correlation
   across Back or a newer selection.
7. **Regression gate — pass:** 171 Hub-plugin tests, the TypeScript/Vite build,
   98 focused Python pairing/protocol/capability/real-gateway tests, strict mypy
   across 42 Python source files, and `git diff --check` passed. The gateway
   suite retains the pairing handshake and unauthenticated 401 while proving
   pairing tokens cannot reach captured logs.
8. **Ordinary-runtime repair — P1 to pass:** the fixture-free simulator state
   had been accepted even though the renderer invented `REPLY / Nothing
   scheduled`, always drew a notification bell, and always drew `LAST QUERY`
   over empty Mac-owned regions. The renderer now resolves the semantic frame
   first: unavailable links replace stale rows with a named connection focus
   state; connected-empty renders `Ready`; partial frames draw only present
   sections. A retained link-state replay also prevents a fast gateway from
   leaving the status falsely stuck on `CONNECTING`. Fresh 576x288 ordinary
   captures, the combined before/fixed image, and a byte-identical populated
   fixture recapture leave no P0/P1/P2 finding. The current gate is 177 Hub
   tests, 98 focused Python tests, strict mypy over 42 files, production build,
   package, and clean diff whitespace.

## Fidelity surfaces

| Surface | Result | Evidence |
|---|---|---|
| Layout and shapes | Pass | Continuous rounded shell/status notch, measured 160px transient Sidebar, full-focus rows, and exclusive Confirmation match the approved hierarchy at the actual 2:1 viewport. |
| Spacing | Pass | The source/current board shows the same major x/y anchors without clipped titles, row collisions, broken borders, or footer overlap. |
| Typography | Pass | SF Mono-first measured text preserves the technical Apple-adjacent register, with sentence-case content and compact uppercase labels. Simulator rasterization is coarser and heavier than the presentation mock. |
| Intensity and contrast | Pass | Authored 8-bit antialiasing is correctly converted to Gray4 focal/support/chrome/quiet levels. The simulator exposes the device's pure-green output; the reference's warmer lime hue is not an available G2 color channel. |
| Icons | Pass | Dotted messages, dotted calendar, notifications, debate, settings, microphone, battery, and arrows use real Phosphor regular assets from one family. |
| Copy and content | Pass | Empty and unavailable sessions use truthful named focus copy and never fabricate a reply, schedule, notification, or query. Runtime hints intentionally differ only where the reference advertised unwired behavior: the Sidebar truthfully says left-double returns to Overview, and browse-only Messages does not promise an open action. |
| States and interactions | Pass | The ordinary URL transitions into a clean connected-empty `Ready` frame; unavailable links replace rather than mix with stale rows. Overview → Sidebar → moved selection → focus loading state works through the real simulator gateway. Confirmation keeps explicit confirm/cancel and a live fail-closed countdown. |
| Accessibility and optical resilience | Pass | High-contrast focal content, icon-plus-label meaning, full-row targets, no color-only semantics, complete confirmation wrapping, and newest-tail reply visibility. |

## Remaining severity audit

- P0: none.
- P1: none.
- P2: none.
- P3: inspect on physical G2 hardware for optical comfort and real-world
  brightness. The simulator validates geometry, intensity protocol, and live
  interaction but cannot reproduce the wearer's environment or left-temple
  source discrimination.

final result: passed
