# Open questions for Even Realities' official sources

Standing rule for this repo: **answer these from Even's published material
before writing code that depends on them.** An assumption that reaches the
glasses becomes a bug the wearer finds, and that is how this plugin
accumulated two months of them.

"There is no source for this" is not an acceptable answer. Even built the
G2 and the SDK and published both; a missing source means the search was
insufficient, not that the source is absent. Record where you looked.

## Where to look, in order

1. `@evenrealities/even_hub_sdk` `dist/index.d.ts` — the docs themselves say
   "the *.d.ts files are the authoritative source"
2. `even-realities/evenhub-templates` — minimal, asr, image, text-heavy
3. `even-realities/everything-evenhub` — the official skills, especially
   `glasses-ui` and `design-guidelines`
4. hub.evenrealities.com/docs
5. `nickustinov/even-g2-notes` — community, but cited BY Even's own
   design-guidelines skill; measured hardware numbers live here
6. `fabioglimb/even-toolkit` — community, also cited by Even
7. The official Figma design guidelines linked from `design-guidelines`
8. Even's product pages, manuals, videos and reviews for first-party UI
   behaviour that the developer docs do not cover

## Open

### 3. List selection across a Back navigation
Firmware lists cost far less than canvas but cannot have their selection set
programmatically: SDK 0.0.16's `index.d.ts` has no field or call that opens a
list at an index; the host only REPORTS `currentSelectItemIndex` on a list
event.

No longer a blocker (2026-10-05, 0.1.99). Every list but the Planning month
is a firmware list, the panel tabs included; the plugin-owned cursor and
`restorePanelSelection` are deleted; a list opened or returned to by Back
starts on row one, as the firmware's highlight does, and the menu follows
the index the firmware reports. The simulator's panels probe drills News →
article → Back without wedging the menu.

Still unanswered from Even's sources, so a better pattern may exist:

- How do Even's own apps preserve a cursor when returning to a list?
- Is there any supported way to open a firmware list at an index?
- If not, what is the intended pattern for drill-down and return?

### 4. `textColor` / text brightness
`MIN_TEXT_BRIGHTNESS = 0` and `MAX_TEXT_BRIGHTNESS = 4` exist in the SDK
`.d.ts`, and `MIN` is documented there as "including the fully dim level".
But `textColor` appears in NO official TextContainerProperty table, in none
of the four templates, and nowhere in even-g2-notes.

- Is `textColor` supported and simply undocumented, or provisional?
- What do levels 0-4 actually render as on hardware? (Measured off the
  simulator: level 1 -> 179/255, level 2 -> 197/255, level 4 -> 255.)

### 5. How far one swipe scrolls on the glasses
Answered for the simulator below ("What the firmware scrolls by itself").
Still open on hardware: does one swipe move an overflowing text container
by one line, as the 0.9.5 simulator does, or further? That one number
decides whether a reader should keep turning pages or hand its text to the
firmware.

## Answered

### What the firmware scrolls by itself — ANSWERED (2026-10-06)
Sources: hub.evenrealities.com/docs/build/display ("If content overflows
and the container has `isEventCapture: 1`, the firmware scrolls it"), the
official text-heavy template (`even-realities/evenhub-templates`, README:
"On G2 you can't scroll. You turn pages." — it pre-paginates with
`@evenrealities/pretext`, turns pages with `textContainerUpgrade` and keeps
a page counter), and a probe page measured in the official simulator 0.9.5:
- An overflowing capture text container scrolls ONE line (27 px) per
  swipe; the last step only aligns the final line. Mid-scroll swipes never
  reach the app. At the bottom every further swipe arrives as
  `SCROLL_BOTTOM_EVENT`, at the top as `SCROLL_TOP_EVENT`.
- The firmware draws its own scrollbar: 4 px wide at the container's right
  edge, a thumb proportional to the visible share (81 px for 8 of 20 lines),
  moving with each line.
- `textContainerUpgrade` of the scrolling container resets it to the top;
  an upgrade of another container leaves it; a rebuild resets it.
- A capture container whose text fits shows no scrollbar, and every swipe
  arrives as a boundary event.
What JOAT does with it: no text page is allowed to overflow
(`paginateMeasured` cuts each page to fit), so every swipe reaches the
plugin and turns seven lines — Even's own pattern. Two pages scroll by
themselves on purpose: the conversation (one line a swipe, because its dim
and bright text layers must move together and no firmware scroll moves
two containers) and the Agent Terminal (six rows a swipe; an upgrade per
streamed line would reset a firmware scroll to the top). No hand-rolled
scrollbar remains on a text page; the conversation's position column is
the only one.

### 2. The Even AI agent interface — ANSWERED (2026-09-24)
Source: Even's own product-page demo video (the wearer-supplied screen
recording of evenrealities.com; frame-extracted at 2 fps and measured).
The Even AI conversation surface is:
- ONE hairline ROUNDED box, centred, generous side margins; nothing else
  on the page — no status bar, no header, no hint line.
- Bottom-anchored: the bottom edge never moves; the box grows UPWARD as
  the exchange grows (five distinct card heights measured across one
  exchange: top 681 → 628 → 562 → 456 → 401 px at fixed bottom 879).
- The wearer's question in DIM text; the answer in BRIGHT text directly
  beneath it; a follow-up question dim again below the answer, separated
  from the previous pair by one blank line. Role is carried by brightness
  alone — the same two-layer dim/bright approach JOAT uses.
- ONE small state glyph centred ON the bottom border — listening bars,
  thinking dots, a working spinner, done/expand corners. It is the only
  chrome the card has.
- NO scrollbar anywhere: older lines simply leave through the top.
Implemented in JOAT 2026-09-24: frame borderWidth 1 / borderColor 8 /
borderRadius 10 (the simulator, matching firmware, defaults borderColor
to 0 = invisible, so a width alone draws nothing); the hand-rolled
scroll-position column removed (page is 4 containers); the footer glyph
straddles the card's bottom border (the continuation prompt, a sentence,
stays inside the card so the border never strikes through it); and
NATIVE_LINE_STAGES = [2, 3, 5, 7] so the card hugs its content and grows
at stage boundaries — one rebuild per crossing, upgrades within a stage.
Verified on the official simulator with the live sandbox brain
(--always-answer): two voice turns painted the bordered card, the
follow-up appended with the blank separator, and the card grew upward
from the fixed bottom. Still unchecked: the Figma guidelines (exact
margins/typography) and the shipped app.

### Payload limits a page must respect — ANSWERED (2026-09-22)
Source: Even's `@evenrealities/evenhub-simulator` README, changelog entries
written "to behave more like firmware":
- v0.7.1 — "add text container bytes limit to 999" → a text container's
  `content` is at most **999 UTF-8 bytes**.
- v0.7.3 — "constraint list item text size to be maximum 63 bytes and 20
  items" → a list has at most **20 rows of 63 UTF-8 bytes**.
- SDK d.ts: menu item names ≤ 32 UTF-8 bytes (`isMenuNameWithinLimit`).
A page over any limit is rejected WHOLE: `rebuildPageContainer` resolves
`false` and the glass keeps its previous page. Observed live in the
simulator log: "RebuildPageContainer validation failed: text content length
1203 exceeds limit of 999 bytes" (the News tab card) and "list item text
length 67 exceeds limit of 63 bytes" (the expanded News list). Both had
been failing silently — every status push re-sent the rejected page, which
is what "EXPAND stomped by the overview" actually was.
Implemented: `apps/hub-plugin/src/FirmwareLimits.ts` clamps every text
container, upgrade and list row; `BridgeClient` logs `[page-rejected]`
naming the offending container whenever the host refuses a page.

### Firmware scrolling swallows swipes inside an overflowing capture container — CONFIRMED (2026-09-22)
Observed on the simulator: with the overview's pane as the capture
container and a News card longer than its six lines, every swipe scrolled
the card and none reached the plugin; the tab rail never moved. The rail
(which never overflows) is now the capture container and the pane is a
six-row, one-line-per-row preview. Firmware paging inside the opened
article works the same way in our favour: swipes scroll the page for free
and only the boundary swipe reaches the Mac to turn the page. (No longer
so: since 2026-10-05 the opened article is a text page measured to fit,
so it never overflows and every swipe turns a page; see "What the
firmware scrolls by itself".)

### Why Even's Dashboard is cheap and our overview is not — ANSWERED

Our overview is a faithful copy of Even's Dashboard, and that is deliberate.
Their support page (support.evenrealities.com "Dashboard") describes exactly
our layout: a status section on the LEFT carrying "Time, Date and day of the
week, Glasses battery level, Weather and temperature, Number of unread
messages", and a widgets area to the RIGHT showing "cards for News, Stocks,
Calendar, QuickList, and Health", navigated by "Swipe on the TouchPad to
switch between widget cards. Single-tap the TouchPad to expand the current
widget card".

They render it cheaply because the Dashboard is a FIRMWARE screen, not a Hub
plugin. hub.evenrealities.com/docs/build/display on what a plugin gets:
"No font selection, no size control, not monospaced", one LVGL font baked
into firmware. The large clock is therefore not reachable from a text
container at all. Drawing it as pixels is not a mistake in our renderer, it
is the only path a plugin has — and it is why the overview costs 581ms.

**The middle path, and it keeps the design.** The overview spends 4 tiles
because its content spans all four 288x144 quadrants. Split the page by what
actually CHANGES rather than by geometry:

- the clock half is pixels, and changes once a minute
- the widget half is rows of text, and changes on every swipe

Give the clock its own image containers (two, stacked, since they cap at
288x144) and build the right half from TEXT containers. A tab switch then
rewrites text only — one or two textContainerUpgrade calls, ~83-166ms —
instead of four tiles at 416ms, and HudVisualSurface's existing pixelsEqual
cache already suppresses the unchanged clock tiles.

Budget allows it: 12 containers per page, up to 4 image and 8 text/list.

Not yet built. This is the next rendering change, and it is measurable with
the [host-call] probe the same way the panel-tab work was.

### Gesture vocabulary and the R1 ring — ANSWERED

Even's canonical ring grammar, from their own post *The Grammar of a Ring*
(evenrealities.com/blogs/even-insider/the-grammar-of-a-ring) — four
gestures, quoted:

- **Tap** — "Confirm."
- **Double-tap** — "Back, or dismiss."
- **Swipe** — "Navigate."
- **Long-press** — "Open the menu."

Corroborated by the official support page *How to Control*
(support.evenrealities.com/hc/en-us/articles/13772400722063): Single Tap
confirms in a list; Double Tap is Back while the display is on; Scroll
Up / Scroll Down navigate.

**The SDK reports which device sent the event.** `EventSourceType` in
`even_hub_sdk` `dist/index.d.ts`:

    TOUCH_EVENT_FORM_DUMMY_NULL = 0
    TOUCH_EVENT_FROM_GLASSES_R  = 1
    TOUCH_EVENT_FROM_RING       = 2
    TOUCH_EVENT_FROM_GLASSES_L  = 3

So a hint can name the device actually in the wearer's hand rather than
assuming a temple. Nothing in this plugin reads it yet — that is the
mechanism if device-aware hints are ever wanted.

**Requirement, from the wearer:** "I should be able to use anything I want,
ring or glasses." Both, interchangeably — not one or the other.

Input already honours that. `EventRouter.isLocalTapSource` and the IMU
tap-confirm both admit `TOUCH_EVENT_FROM_RING`, `_FROM_GLASSES_L` and
`_FROM_GLASSES_R`. A comment at EventRouter.ts:538 records an earlier gate
that admitted right/ring only, "so a left tap silently did nothing" — the
same bug pointing the other way.

What the wearer SEES has to honour it too, and that is the fragile half:
roughly forty hint strings are still AUTHORED as "left x2" and depend on
every render path remembering to call `formatHint`. One forgot, and it
reached the glass. `GestureHints.test.ts` now asserts no rendered hint
matches /left|right|temple|ring/, so the next forgotten call fails a test
instead of a wearer. Renaming the authored constants would be better still;
it is ~40 literals plus their assertions, so it is worth doing as its own
change rather than smuggled into a fix.

**Wording:** Even says "double-tap"; this plugin says "double-click". The
wearer has accepted "2x tap/click" as fine, so this is NOT worth churning
23 test assertions over. What is not acceptable is naming a physical
location: "left x2" names a temple an R1 wearer does not press. Every
wearer-facing hint goes through `GesturePolicy.formatHint`, and no hint
names a side. See dd63111 for a leak that reached the glass.

`hold` vs Even's `long-press` is the same class of cosmetic mismatch —
note it, do not churn it.

### Event capture does not require a border — CONFIRMED
All four official templates pair `isEventCapture: 1` with `borderWidth: 0`:
`asr`, `minimal` and `text-heavy` on their primary text container, and
`image` on its transparent full-screen `eventLayer`. A border is
decoration. See 79bb1eb.

### Host display calls are the rendering cost — CONFIRMED
Measured on a G2 (even-g2-notes/performance.md, firmware 2.2.7.14):
`textContainerUpgrade` ~83ms per call, `rebuildPageContainer` ~165ms flat
regardless of container count, `updateImageRawData` ~104ms fixed plus
~3.9ms per KB of gray4. Payload barely matters; call COUNT is the cost.
Even's own guidance: "Fewer host calls beats cheaper host calls" and "Text
updates are much faster than image updates."

### Why every screen costs 4 tiles — CONFIRMED
An image container caps at 288x144 and the canvas is 576x288, so the HUD is
four quadrant tiles. Any content change dirties most of them. Measured with
the `[host-call]` probe (0f591be) against the real brain and real feeds,
every reading checked against a pixel diff: expand a tab 416ms, scroll
208-312ms, leave 416ms, ~3.3s to visit four tabs.
