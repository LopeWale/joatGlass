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

### 2. The Even AI agent interface
How does Even's own assistant distinguish the wearer's words from the
agent's reply on screen? Looked in all four templates and both official
repos: the ASR template is transcription only and carries no role concept.
Reviews describe a listening animation on activation and an adjustable
"response speed" for the answer. Not yet checked: the Figma guidelines,
Even's videos, and the shipped app itself.

Blocked on this: the conversation surface currently stacks two containers
at different brightness, which measured 197 vs 255 and is illegible as a
role cue. Relevant constraint, from Even's own tracker
(`everything-evenhub#23`): "Text containers render at a single uniform
brightness — there is no API to set a highlight color behind a span."

### 3. List selection across a Back navigation
Firmware lists cost far less than canvas but cannot have their selection set
programmatically, so `restorePanelSelection` cannot restore a cursor and
`openPanelTab` wedges the whole capability menu. Proven by breaking it and
un-breaking it.

- How do Even's own apps preserve a cursor when returning to a list?
- Is there any supported way to open a firmware list at an index?
- If not, what is the intended pattern for drill-down and return?

This is the single blocker on moving the panel tabs off the image path.

### 4. `textColor` / text brightness
`MIN_TEXT_BRIGHTNESS = 0` and `MAX_TEXT_BRIGHTNESS = 4` exist in the SDK
`.d.ts`, and `MIN` is documented there as "including the fully dim level".
But `textColor` appears in NO official TextContainerProperty table, in none
of the four templates, and nowhere in even-g2-notes.

- Is `textColor` supported and simply undocumented, or provisional?
- What do levels 0-4 actually render as on hardware? (Measured off the
  simulator: level 1 -> 179/255, level 2 -> 197/255, level 4 -> 255.)

### 5. Scroll indicators and firmware scrolling
even-g2-notes states that a text container with `isEventCapture: 1` scrolls
its overflow internally and shows its own scrollbar, with the app receiving
only `SCROLL_TOP_EVENT` / `SCROLL_BOTTOM_EVENT` boundary events. Confirm
against Even's own docs, then delete any hand-rolled scrollbar that
duplicates it.

## Answered

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
