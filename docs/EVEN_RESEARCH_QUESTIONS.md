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

### 1. Gesture vocabulary, and the R1 ring
The wearer uses an R1 ring, so any hint naming a temple ("left x2") names a
gesture they cannot perform. `GesturePolicy.formatHint` rewrites "left x2"
to "double-click", but that is OUR invention, not Even's wording.

- What vocabulary does Even use in their own on-lens hints?
- Is there a canonical term covering temple and ring together?
- Does the SDK report which input device sent an event, so hints could name
  the right one? (`EventSourceType` exists in the SDK exports — check it.)
- Are ring gestures a distinct event source, or indistinguishable from
  temple taps?

Until answered, every wearer-facing hint goes through `formatHint`, and no
hint names a physical location. See dd63111 for a leak that shipped.

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
