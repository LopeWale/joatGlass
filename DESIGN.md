---
version: alpha
name: "JOAT G2 HUD"
description: "A quiet, glanceable control surface for the Even Realities G2 optical HUD."
colors:
  primary: "#00FF00"
typography:
  mono:
    fontFamily: "Even G2 firmware text renderer"
spacing:
  status-height: "32px"
  focus-title-height: "48px"
  dashboard-hint-height: "32px"
components:
  dashboard: { }
  focus-page: { }
  confirmation: { }
omitted:
  - section: rounded
    reason: "The firmware text and list containers own geometry; rounded decoration has no evidenced optical benefit."
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
- **Memorable signature:** exactly one brightness-4 focal region in a decision
  state; everything else recedes.
- **Restraint:** whitespace, truthful verbs, and stable regions win over borders,
  ornament, animation, or dense telemetry.
- **Anti-references:** floating browser dialogs, glassmorphism, card grids,
  decorative icons, and gesture hints for actions the runtime does not implement.
- **Token ownership/runtime mapping:** this file mirrors the canonical runtime
  geometry in `apps/hub-plugin/src/HudPageBuilder.ts` and firmware brightness
  values passed by `apps/hub-plugin/src/HudPresenter.ts`. It does not generate
  SDK containers. Drift is gated by `HudPageBuilder.test.ts`, presenter behavior
  tests, the Even Hub simulator, and the premium design audit.

## Colors

The device emits one green channel. `#00FF00` names that physical primary, not
an authored brand choice. Meaning must never depend on hue. Hierarchy uses the
firmware's brightness scale: 4 focal, 3 supporting content, 2 persistent chrome,
1 quiet chrome, and 0 deliberately suppressed content. The background is the
real world through the optical display, so there is no owned opaque surface.

## Typography

The G2 firmware owns the typeface, metrics, wrapping, and rasterization. JOAT
uses sentence case for content and compact uppercase labels for focus-page and
confirmation titles. Copy is written for a 46-character working line, but that
number remains an empirical project assumption until measured on physical G2
hardware. ASCII words carry meaning; optional punctuation is decorative only.

## Layout

The canvas is fixed at 576×288. The dashboard keeps five horizontal regions:
Status 32px, Main 128px, Context 64px, Tray 32px, Hint 32px. A focus page uses
Status 32px, Title/Actions 48px, and List 208px. Exactly one container captures
events: Main on the dashboard and List on a focus page. The dashboard-only Hint
paint and all Mac-owned dashboard content paints are suspended while a focus
page is active, so intentionally absent Context, Tray, and Hint containers can
never trigger a layout repair or overwrite the focus header.

Content never relies on pixel-perfect right alignment. Space is attention, not
unused capacity. A mode that needs more than its visible line budget becomes a
scrollable/list focus page or a separate detail state; it does not silently
overflow a title or action strip.

## Elevation & Depth

There is no shadow, blur, backdrop, or z-index theater on the optical HUD.
Hierarchy is brightness plus exclusive attention. Focus pages replace the
dashboard layout. Confirmation is the only app-owned overlay state: it preserves
the status line, suppresses competing Context and Tray content, and raises the
prompt and action hint to brightness 4.

## Shapes

Firmware selection borders identify the active list row. JOAT adds no decorative
containers or rounded rectangles until physical-device evidence proves they aid
legibility. Region edges are established through whitespace and fixed geometry.

## Components

### Foundational visual states

- **Rest:** Main is brightest; Context supports; Tray and Hint recede.
- **Focused list:** the compact Title/Actions header is brightness 3 and rows
  use firmware selection. Combining those two lines preserves room for all five
  core capability rows without a required scroll.
- **Confirmation:** prompt and action Hint are brightness 4; Context and Tray
  are blank at brightness 0; ordinary content updates are deferred. The input
  gate consumes gestures while rendering but cannot approve until every modal
  region has painted successfully.
- **Timeout/cancel:** fails closed, restores the prior surface, and reports the
  settled state through the plugin-owned status line.
- **Offline/empty/error:** receives a named focus page with recovery or exit
  guidance. It is never squeezed into the 48px title while stale rows remain.

### Buttons and actions

There are no visual buttons. Gesture copy is the action system. Use explicit
verbs and keep one primary action per decision area. A confirmation names both
outcomes: right temple/ring/nod accepts, left temple cancels, and eight seconds
without action times out as refusal.

### Navigation and data display

Focus-page grammar is `TITLE + ACTION / selectable or readable rows`.
Capability and Settings rows are actionable. Message and notification lists are
browse-only until a real detail handler exists, so their footer must not promise
"open". Left-temple double-tap is Back from every app-owned focus page.

### Forms and overlays

The HUD has no data-entry form. Its two modal families are:

| Surface | Layout owner | Input owner | Exit and restoration |
|---|---|---|---|
| Focus page | Plugin layout; Mac or plugin rows | List selection plus left-double Back | Rebuild dashboard and replay the presenter's buffered, latest regions |
| Confirmation | Plugin presenter | Right/ring/nod accept; left cancel; timeout refuses | Restore the exact prior dashboard or focus page, including deferred content |

The glasses OS owns the app-exit confirmation. JOAT preserves mode-1 shutdown
and does not restyle, replace, or pre-empt that platform surface.

### Iconography

No icon library is available or needed. Short text labels remain mandatory.
Symbols such as `*` may supplement state but never replace a word or action.

### Motion

The firmware owns list movement. JOAT does not animate app-owned regions.
Content changes use in-place `textContainerUpgrade`; page rebuilds are reserved
for real layout changes so the display does not flicker.

### Content and data visualization

Copy is direct, short, and operational. Headers name the mode, rows contain the
object or state, and the footer names active gestures. Backend errors and secret
material never appear. No chart or decorative visualization belongs on this
canvas.

## Do's and Don'ts

- **Do:** keep Status present everywhere and suspend dashboard-only content paints on focus pages.
- **Do:** make every footer a truthful description of the active gesture map.
- **Do:** restore the interrupted surface after confirmation settles.
- **Don't:** imitate a web modal with a floating bordered card.
- **Don't:** advertise open, stop, dismiss, or cancel behavior that is not wired.
- **Don't:** let a Chrome paint rebuild a focus page into a different mode.
