---
version: alpha
name: "JOAT G2 HUD"
description: "A quiet, glanceable control surface for the Even Realities G2 optical HUD."
colors:
  primary: "#00FF00"
typography:
  mono:
    fontFamily: "SF Mono, SFMono-Regular, Menlo, monospace"
spacing:
  status-spine: "36px"
  sidebar-width: "160px"
  shell-radius: "20px"
components:
  dashboard: { }
  sidebar: { }
  focus-page: { }
  confirmation: { }
omitted:
  - section: color-semantics
    reason: "The G2 emits one green channel; hierarchy must survive in the Gray4 protocol values alone."
---

# JOAT G2 HUD Design System

## Overview

### Creative North Star

An aircraft head-up display at walking speed: one bright fact, dim instrument
chrome, and no visual element that asks the wearer to study the glass.

### Product context and register

- **Audience and primary job:** one wearer who needs to understand JOAT's state,
  read a short answer, choose a capability, or approve a consequential action
  without reaching for the phone.
- **Target market(s) and evidence:** current English-language private build; no
  market-specific visual claim is made.
- **Locale(s) and language policy:** English today. Owned copy stays short enough
  for the measured 46-character working line; localization requires fresh
  on-device line-budget evidence.
- **Usage scene:** transparent 576×288 display, often while walking, controlled
  by temple/ring gestures and voice. Attention and optical contrast are scarce.
- **Register:** instrument-like product UI, not a branded web dashboard.
- **Memorable signature:** exactly one full-intensity focal region in a decision
  state; everything else recedes.
- **Restraint:** whitespace, truthful verbs, and stable regions win over
  ornament, animation, or dense telemetry. The navigation rail is the one
  intentional border because it clarifies foreground from dimmed context.
- **Anti-references:** floating browser dialogs, glassmorphism, card grids,
  decorative icons, and gesture hints for actions the runtime does not implement.
- **Token ownership/runtime mapping:** this file mirrors the canonical runtime
  geometry in `apps/hub-plugin/src/HudPageBuilder.ts` and the pixels authored by
  `apps/hub-plugin/src/HudCanvasRenderer.ts`. Drift is gated by builder,
  presenter, renderer/surface tests, the official Even Hub simulator, and the
  retained comparison in `design-qa.md`.

## Colors

The device emits one green channel. `#00FF00` names that physical primary, not
an authored brand choice. Meaning never depends on hue. The canvas authors
8-bit antialiased values—238 focal, 170 support, 102 chrome, 68 quiet, and 51
hairlines—then quantizes every pixel to the SDK's unpacked Gray4 range 0...15
before upload (normally 14, 10, 6, 4, and 3). Mac `textColor` levels 0...4 map
onto that authored scale, so a routine card stays visibly quieter than an
urgent one. Passing the 8-bit values directly is invalid and clips most of the
hierarchy to full green. Black pixels are transparent on glass; the black
simulator composite is a legibility aid, not an owned backdrop.

## Typography

JOAT rasterizes an SF Mono-first system stack into the image surface. Sentence
case carries content; compact uppercase labels identify sections and modes.
The renderer measures text before it can cross a visual boundary. Streaming
replies show the newest two wrapped lines; confirmation prompts wrap every
allowed character across as many as three fitted lines rather than hiding the
decisive suffix. Copy stays short because 576×288 is an attention budget, not a
miniature web page. Real Phosphor regular icons supplement labels but never
replace them.

## Layout

The canvas is fixed at 576×288. Four 288×144 Gray4 image containers tile into
one continuous authored frame. A fifth, empty brightness-zero text container
sits above them as the sole event-capture owner. This is the SDK-compatible
way to get custom layout and keep touch/ring events: image containers cannot
capture input and the SDK permits no image larger than 288×144.

Overview, Sidebar, and live-session states share the 20px rounded shell; every
list, confirmation, and productivity page renders FLAT under the 36px status
spine (wearer's ruling, 2026-08-31: the card was the waste — its border,
margins, and per-row chrome, not any single line). Overview is sparse and
icon-led. A left-temple double-click opens the
transient 160px Sidebar while the current Overview stays visible at 24% intensity
to preserve place. Selecting a row removes the rail and gives the destination
the full frame. Presenter updates are serialized across all four image tiles,
and pending frames coalesce to the newest complete state so token streams and
countdowns cannot build a stale BLE backlog. A failed partial frame triggers
one bounded page rebuild; a failed first Confirmation then actively restores
the prior four tiles before the input gate stays closed.

Content never relies on pixel-perfect right alignment. Space is attention, not
unused capacity. A mode that needs more than its visible line budget becomes a
scrollable/list focus page or a separate detail state; it does not silently
overflow a title or action strip.

## Elevation & Depth

There is no shadow, blur, or simulated glass on the optical HUD. Hierarchy is
intensity plus progressive disclosure. The Sidebar is the only split surface:
its selected row uses a bright three-pixel leading edge over a black rail and a
dimmed, still-recognizable Overview. Full-focus pages replace that split.
Confirmation preserves the status spine, removes competing content, and gives
one bright question the wearer's full attention.

## Shapes

The 20px rounded rule survives only where four tiles must read as one HUD
(Overview, Sidebar, live sessions). List rows use ONE language everywhere:
dense reading rows — a bright three-pixel selection rail on rows that accept a
tap, hairline separators, six rows per screen (four on two-line Planning day
agendas). The former per-row card (icon badge, 56px pitch, rounded selection
outline) is retired; it fit three rows where six belong. There are no
floating modal cards. Row KIND is indicated by a tiny aligned marker from
the firmware-safe glyph set — ● a recording, □ an open todo, – a key point —
never by an icon badge or a spelled-out word; markers indicate, they do not
decorate. A selectable list that mixes marked and unmarked rows reserves the
marker column on every row so all titles share one left edge; non-selectable
detail pages keep hanging-indent bullets under their section labels. When a
page holds more rows than one window, a 2px hairline scrollbar at x=556
shows position and proportion — words never announce overflow, and healthy
metadata stays quiet ("2 transcript sections", not "… · 0 gaps").

## Components

### Foundational visual states

- **Overview:** Main is brightest; Context supports; Tray and Hint recede. It is
  the only resting state and never carries permanent navigation chrome.
- **Transient Sidebar:** appears only after a left-temple double-click. Its five
  capability rows occupy the left 160px while the current Overview remains
  visible and dimmed at right. Selecting a row removes the Sidebar immediately.
- **Full-focus page:** a bright destination title, semantic Phosphor markers,
  primary and secondary row text, timestamps, and a quiet action hint use the
  whole canvas after selection. Messages may use a sender initial; Calendar,
  Tasks, Notifications, QuickNotes, and Activity keep their domain glyph.
- **Confirmation:** the question is focal intensity; accept is primary, cancel
  is supporting, and the countdown meter recedes. Ordinary content
  updates are deferred. The input gate consumes gestures while rendering but
  cannot approve until the dedicated surface exists successfully.
- **Timeout/cancel:** fails closed, restores the prior surface, and reports the
  settled state through the plugin-owned status line.
- **Offline/empty/error:** receives a named focus page with recovery or exit
  guidance. It never shares the frame with stale rows.

The microphone indicator is driven by structured audio state, never parsed
from transient status copy. Capability data is accepted only when its unique
request generation matches the currently visible destination; titles describe
content but are not response identity. Back or a newer selection makes an
older response and its row snapshot inert. Whether a focus page accepts row
taps is explicit protocol state, never inferred from title copy.

### Buttons and actions

There are no ordinary visual buttons. Gesture copy is the action system. Use
explicit verbs and keep one primary action per decision area. Confirmation is
the exception: it names both outcomes as `TAP · Confirm` and `SWIPE · Cancel`;
either built-in temple tap confirms, either swipe refuses, and eight seconds
without action also refuses. Nod is not an approval path.

### Navigation and data display

Navigation grammar is `Overview -> Sidebar -> full-focus destination -> detail
-> Overview`. The Sidebar is one continuous, native-scrollable first-level
rail: `Messages`, `Planning`, `Navigation`, `Brainstorm`, `Notifications`,
`QuickNotes`, `Agent Terminal`, `Activity`, then direct `Repeat`, `Reconnect`,
and `Settings` actions. Nothing is hidden behind a `More` choice; the wearer
can keep scrolling while moving. Only a left-temple double-click opens or
closes app navigation; a right-temple double-click remains the glasses OS exit
confirmation. Populated Messages, Notifications, Planning, QuickNotes, and
Activity collections accept a tap. Setup, empty, detail, and action-lifecycle
pages are read-only and therefore draw no selection outline or tap promise. A
navigation state becomes interactive only after its visual paint succeeds; a
failed paint restores Overview instead of exposing invisible controls.

Brainstorm is the wearer-facing capability name; `debate` remains a legacy
voice alias. Brainstorm is a wearer-initiated, steerable, potentially
multi-round analysis session around a topic, task, claim, problem, or evolving
discussion. Codex CLI and Claude Code challenge and expand each other's work,
repair unsupported claims, and converge on a verified JOAT synthesis while the
process remains visible on the glasses. Hermes and OpenClaw are not Brainstorm
participants and their availability never gates Brainstorm.

JOAT is the glasses brain and the only wearer-facing Brainstorm controller. It
receives voice from the glasses, owns the selected durable session and its
lineage, routes each move to the wearer-directed or session-selected
collaborator, streams the actual work back to the HUD, runs verification and
bounded repair, produces the synthesis, and handles resume, branch, stop, and
reconnect restoration. Codex and Claude are collaborators inside JOAT's
Brainstorm; neither one replaces JOAT or owns the glasses interaction.

Opening Brainstorm while a conversation is active restores that conversation.
Opening it while idle shows a compact recent-session list plus `NEW`. Ordinary
speech continues the selected context; explicit `new Brainstorm`, `branch
this`, and `continue <title>` change session lineage. If a second Brainstorm
request could replace a clearly unrelated selected context, JOAT asks before
replacing it rather than silently resetting or guessing.

The Brainstorm focus page is a flat optical transcript, not a dashboard: one
compact title/state line, 10–12 speaker-prefixed transcript lines, one
voice-composer line, and one truthful gesture footer. It has no cards, boxed
panels, permanent seat row, oversized topic type, decorative empty space, or
fake waveform. A recording mark appears only while capture is actually active.

Agent Terminal is a separate future capability: a dense CLI/chat interface for
existing Hermes and OpenClaw sessions on configured Tailscale Macs. It is not a
Brainstorm seat and never uses Codex or Claude. See
[`docs/AGENT_TERMINAL_ROADMAP.md`](docs/AGENT_TERMINAL_ROADMAP.md).

### Forms and overlays

The HUD has no data-entry form. Its three temporary surface families are:

| Surface | Layout owner | Input owner | Exit and restoration |
|---|---|---|---|
| Sidebar | Custom image frame with the latest Overview as backdrop | Transparent capture layer; left-double returns to Overview | Selection removes the rail into a full-focus destination |
| Focus page | Custom image frame; Mac or plugin rows | Transparent capture layer; left-double returns to Overview | Paint Overview and replay the presenter's buffered, latest regions |
| Confirmation | Plugin presenter | Built-in temple tap confirms; either swipe or timeout refuses | Restore the exact prior Overview, Sidebar, or focus page, including deferred content |

The glasses OS owns the app-exit confirmation. JOAT preserves mode-1 shutdown
and does not restyle, replace, or pre-empt that platform surface.

### Iconography

All interface icons come from `@phosphor-icons/core` regular assets.
Calendar/Planning, Messages, Notifications, Tasks, Navigation, QuickNotes,
Brainstorm, Agent Terminal, Activity, Settings,
microphone, and battery use one stroke family and are rasterized with the
surrounding frame. Short text labels remain mandatory; icons never carry
meaning alone.

### Motion

JOAT does not animate navigation. Scroll changes move the authored selection
band in one frame, including a moving three-row window for later results.
Confirmation repaints its countdown once per second. All four tile uploads are
serialized because the bridge is a single command lane; queued requests
coalesce to the newest frame.

### Content and data visualization

Copy is direct, short, and operational. Headers name the mode, rows contain the
object or state, and the footer names active gestures. Backend errors and secret
material never appear. No chart or decorative visualization belongs on this
canvas.

## Do's and Don'ts

- **Do:** keep Status present everywhere and buffer Overview paints behind every temporary surface.
- **Do:** show the Sidebar only in response to left-temple double-click and remove it after selection.
- **Do:** make every footer a truthful description of the active gesture map.
- **Do:** restore the interrupted surface after confirmation settles.
- **Don't:** leave the Sidebar visible beside destination content.
- **Don't:** imitate a web modal with a floating bordered card.
- **Don't:** advertise open, stop, dismiss, or cancel behavior that is not wired.
- **Don't:** let a Chrome paint rebuild a focus page into a different mode.
