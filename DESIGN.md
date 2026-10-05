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
  status-spine: "24px"
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
list, confirmation, productivity, and stationary navigation page (confirm
destination, repair, travel mode, route preview, arrival, error) renders FLAT
under the 24px status spine (wearer's ruling, 2026-08-31: the card was the
waste — its border, margins, and per-row chrome, not any single line). Only
the moving navigation states (guidance, GPS-uncertain, wrong-way, off-route,
rerouting) keep the live-session shell, alongside Brainstorm. Overview is sparse and
icon-led. A left-temple double-click opens the
transient 160px Sidebar while the current Overview stays visible UNCHANGED
behind it to preserve place. Unchanged is literal and load-bearing: the frame
ships as four 288x144 tiles over a serialized BLE lane and the presenter skips
tiles whose pixels did not change, so dimming or shifting the backdrop would
move every lit pixel, dirty all four tiles, and make the wearer watch the top
half of the menu arrive before the bottom (observed on hardware, 2026-08-31).
Leaving the right half untouched sends only the two tiles carrying the rail.
Selecting a row removes the rail and gives the destination the full frame. Presenter updates are serialized across all four image tiles,
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
its selected row uses a bright three-pixel leading edge over a black rail, set
against the untouched Overview. The rail's own black field is what separates
foreground from context; the backdrop is not dimmed, because re-tinting it
would cost two extra tile transfers on every menu open. Full-focus pages
replace that split.
Confirmation preserves the status spine, removes competing content, and gives
one bright question the wearer's full attention.

## Shapes

The 20px rounded rule survives only where four tiles must read as one HUD
(Overview, Sidebar, live sessions). List rows use ONE language everywhere:
dense reading rows — a bright three-pixel selection rail on rows that accept a
tap, NO separator lines, nine single-line rows per screen, Planning days
included. The former per-row card (icon badge, 56px pitch, rounded selection
outline) is retired; it fit three rows where nine belong. Planning's two-line
agenda variant is retired too: a day row spelled out "16:30 · EVENT · Title"
and needed a second line to fit it, which cost three rows per glance for a
word the marker already says. Rows read "[16:30] □ Buy filters" — quiet time
chip, marker, title — so a day shows nine entries like every other list.
There are no floating modal cards. Row KIND is indicated by a tiny aligned
marker from the firmware-safe glyph set — ● the row demanding attention now
(a live recording, an unread thread), □ an open todo, – a key point — never
by an icon badge or a spelled-out word; markers indicate, they do not
decorate. Only two Messages screens keep bespoke layouts: the two-column
conversation transcript (seven rows; INCOMING text carries the weight, the
wearer's own words recede to support) and the full-text reader (quiet
metadata line, bright 14px body). The resting Overview wears an icon only
when its main content has a KIND (calendar for what's next, bell for a
notification); plain assistant text carries no icon — JOAT's home screen is
JOAT, not a Messages app. A selectable list that mixes marked and unmarked rows reserves the
marker column on every row so all titles share one left edge; non-selectable
detail pages keep hanging-indent bullets under their section labels. When a
page holds more rows than one window, a 2px hairline scrollbar at x=556
shows position and proportion — words never announce overflow, and healthy
metadata stays quiet ("2 transcript sections", not "… · 0 gaps").

### The resting screen is a dashboard

Home is not a sentence on an empty field. The left pane carries the date and
a large two-row clock drawn from the PHONE's own time, so the lenses stay
useful when the Mac is unreachable; a single hairline divides it from the
right pane, which flows JOAT's current content — label, answer or next
event, last query, and an ambient tray line anchored to the pane's bottom.
The spine yields its small clock there: no screen prints the same fact
twice.

### The right pane is a rail of tabs

Between the clock and the pane's hairline stands a vertical rail of four
icons — notifications, news, pomodoro, search — the platform dashboard's
own idiom, drawn in JOAT's: tabs OUTSIDE the pane so the content keeps its
width, the lit tab attached to the hairline by a short bracket. A swipe on
the resting overview moves along the rail; a tap expands the lit tab
full-width, with the rail moved to the left margin so the wearer still
sees where they are; inside, swipe scrolls rows, tap opens one, left
double steps back one level. Long press refreshes ONLY at the top of an
expanded NEWS or SEARCH tab — the two whose rows come from a feed that can
have moved. Everywhere else it stays the microphone, the pomodoro page
included: that tab has nothing to re-fetch, since the Mac pushes on every
phase transition and the plugin ticks the countdown locally, so routing
the hold there bought an invisible repaint and cost the wearer the mic on
one page. Every footer names the hold it actually performs.

The rail, the hairline and the pane all sit east of x=288. That is a
latency rule, not a taste: the frame ships as four tiles and only changed
tiles are uploaded, so a tab change costs the two right tiles, and the
pomodoro's second hand — drawn inside the top-right tile — costs one.
Notifications is the resting pane itself. The other three tabs paint from
what the Mac last pushed; nothing on the glass ever fetches.

### The page's name lives in the status spine

On a feature page the spine reads MESSAGES or PLANNING where it reads JOAT
at home, and the body below it belongs entirely to that feature — no second
title bar. That reclaimed band is worth a whole extra row of content
(eleven dense rows per list under the 24 px spine, ten reader lines, eight
transcript rows; five rows on a firmware list, where the name shares the
header line). A degraded link outranks the label: if JOAT is not READY, the
spine says so instead.

### Guidance is two panes: act, then see

Active navigation splits the canvas like an instrument cluster — maneuver
glyph, distance, instruction and next action on the left; the real route
polyline on the right, projected from the Mac-authored geometry with the
wearer's position ringed on it. The road is drawn in chrome so the eye
still lands on the maneuver first, and the map carries no border: the map
IS the content.

The pane follows the wearer. The streets under the route are tiles the Mac
fetched along the whole route the moment it was known; the pane shows the
one nearest the wearer, so the ground ahead is always in view and nothing
jumps when the next tile takes over. Until the first tile arrives, or when
a tile fails, the pane shows the route's own shape around the wearer at
the same zoom (fit to the whole route before the first fix); it never goes
blank. The dot, the progress bar and the ETA move with every fix that moved
the wearer, at most once a second — not only when the instruction changes.

## Components

### Foundational visual states

- **Overview:** Main is brightest; Context supports; Tray and Hint recede. It is
  the only resting state and never carries permanent navigation chrome.
- **Transient Sidebar:** appears only after a left-temple double-click. Its five
  capability rows occupy the left 160px while the current Overview remains
  visible and pixel-identical at right, so only the two left tiles transfer.
  Selecting a row removes the Sidebar immediately.
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

Navigation's stationary pages speak the same dense reading language as every
focus page: a 10px uppercase kicker, one 16px primary fact, 11px support, and
12px choice rows on a 20px pitch behind the 3px selection rail. The decorative
route sketch, the canonical-place pin restating the header, the duplicated
DISTANCE/ETA tiles, and the centered 24–25px poster arrival are retired
(wearer's ruling, 2026-08-31: text-only navigation pages carried card chrome,
oversized type, and duplicated data). Small semantic marks survive where they
aid recognition — the error triangle, a compact arrival check — because a mark
indicates; it never decorates.

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
meaning alone — with two deliberate exceptions. The overview's tab rail is
four icons without labels: four fixed positions in a fixed order, learned
once, on a rail the wearer swipes through in seconds, and the tab's name
appears in the status spine the moment it is expanded. A label per tab
would cost the pane the width the rail exists to preserve. And the status
spine's microphone and battery are icons alone (wearer's ruling,
2026-10-05: "simple icons and smaller"): a slashed mic is a closed mic, the
cell's fill is the level, the charging cell is charging, and the number
returns beside the cell only from 15% down, when it matters. The firmware
pages, which cannot draw a bitmap, spell the same two facts with glyphs the
firmware font has: ● and ○ for the microphone, ▁ to █ for the battery, a
trailing + for charging, the number from 15% down.

### The status spine

One 24 px band (it was 36). Left tile: the phone's clock (the date on the
overview, which prints the time at full size) and the spine's WORD. Right
tile: the two icons. The word is the page's name at rest, JOAT at home, a
degraded link above everything ("JOAT · RECONNECTING"), and the Mac's
transient state ("JOAT · HEARING YOU…") while it lasts. The microphone is
no longer a word: a page keeps its name while the wearer talks, where it
used to read "JOAT · LISTENING" the moment the mic opened.

A firmware page spends one line on the same facts, in the same order:
`9:41 · POMODORO · ● · ▇`. It used to spend two — a status line in words
("9:41 JOAT mic off bat 100%") and a separate title line — and that second
line is where every firmware list's fifth row came from. The line is live:
a clock, microphone or battery change on a firmware page is one in-place
`textContainerUpgrade` of its header container, never a rebuild and never
a tile; before 2026-10-05 it was dropped, so a list opened at 9:41 still
said 9:41 at 10:05. When the line overruns the glass, the word is
shortened, never the clock or the glyphs (`HudPageBuilder.fitHeader`).

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
