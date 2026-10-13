# Even Hub Plugin — `@evenrealities/even_hub_sdk` (v0.0.14)

The Hub plugin is our **officially supported path** to the glasses: a TypeScript web app running in the Even companion app's WebView that owns the G2 and streams audio, IMU, and input events to the Mac brain over WebSocket. No BLE reverse engineering, no sideloading, no firmware risk.

```
apps/hub-plugin (this) ⇄ EvenAppBridge ⇄ Even App (host) ⇄ G2 glasses
        │ WebSocket
     Mac brain (joat)
```

## Why this changes the architecture

**The SDK hands us decoded PCM — as BYTES.** `bridge.audioControl(true, AudioInputSource.Glasses)` delivers the G2 four-mic array as `audioEvent.audioPcm: Uint8Array` — **s16le byte stream, 16 kHz mono** (PCM 字节码). Treating bytes as samples yields near-silent noise at half speed (peak 255, never above); `AudioStreamer` decodes byte pairs, with a carry byte for odd-length events. In relay mode there is no LC3 decoding, no `0xF1`/NUS frame parsing, no `[0x0E, 0x01]` mic command. `liblc3` is needed **only** for the Phase-5 direct-BLE path. Phase 0 gets substantially cheaper.

**IMU is a first-class API.** `bridge.imuControl(true, ImuReportPace.P100)` gives 10 Hz `x/y/z` via `sysEvent.imuData` — that is our nod `Confirmer`, no BLE work required. `EventSourceType` also distinguishes the R1 ring (`TOUCH_EVENT_FROM_RING`) from left/right temple touches (`TOUCH_EVENT_FROM_GLASSES_L/R`), so ring-confirm and temple-press are separable inputs today.

**Phone sensors come free:** one-shot + continuous location (`getAppLocation` / `startAppLocationUpdates`), album picker, and phone camera capture. The vision path (`captureImageFromCamera` → `base64` → Qwen3-VL on the Mac) needs no custom companion app.

## Required call order (hard requirement)

1. `await waitForEvenAppBridge()`
2. `bridge.createStartUpPageContainer(container)` — **exactly once**, must return `StartUpPageCreateResult.success` (0)
3. Everything else — `audioControl`, `imuControl`, `rebuildPageContainer`, `textContainerUpgrade`, listeners

`audioControl(true, Glasses)` and `imuControl` **fail** if step 2 hasn't succeeded. Phone-mic audio, location, album, and camera route through the phone and don't require it.

Register `onLaunchSource` immediately after step 1 — it fires exactly once (`'appMenu' | 'glassesMenu'`).

## Page/container model

Canvas: **576 × 288**, 4-bit greyscale (0 = black, 15 = white), origin top-left, +X right, +Y down.

| Limit | Value |
|---|---|
| `containerTotalNum` | 1–12 |
| `textObject` | max 8 |
| `imageObject` | max 4 |
| `isEventCapture: 1` | **exactly one container per page** |
| `textContainerUpgrade` content | max 2000 chars |
| Text container `content` | max 1000 chars |
| List items | max 20, 64 chars each |
| Image container | width 20–288, height 20–144 |
| `borderWidth` 0–5 · `borderColor` 0–15 · `borderRadius` 0–10 · `paddingLength` 0–32 | |

Rules that bite:

- `createStartUpPageContainer` is **one-shot**. Use `rebuildPageContainer` for later full redraws and `textContainerUpgrade` for in-place text — the latter is our default for streaming HUD updates (no flicker).
- `zOrderIndex` is **all-or-nothing per page** and must be **unique**. Violations fail SDK-side validation before reaching native: `createStartUpPageContainer` → `invalid`, `rebuildPageContainer` → `false`. Omit it everywhere to keep declaration-order behaviour.
- Image containers have no `isEventCapture` — pair them with a text container as the capture layer.
- `updateImageRawData` calls must be **serial** (await each). The plugin sends each tile as PNG bytes; the host converts to Gray4 and LZ4-compresses before BLE (the pipeline table below has the costs).
- Always call the unsubscribe function returned by `onEvenHubEvent` / `onDeviceStatusChanged` / `onLaunchSource`.

## Input routing — verified, and full of traps

Which event field a gesture lands in depends on the **active container** (the one with `isEventCapture: 1`).

**Text container active** (our default page):

| Gesture | Arrives as | `eventType` |
|---|---|---|
| Swipe up | `textEvent` | `1` |
| Swipe down | `textEvent` | `2` |
| Single press | **`sysEvent`** | `0` |
| Double press | **`sysEvent`** | `3` |

**List container active** (settings menu):

| Gesture | Arrives as | Detail |
|---|---|---|
| Swipe | — | SDK scrolls internally, no event |
| Single press | `listEvent` | `currentSelectItemIndex` |
| Double press | `sysEvent` | `eventType 3` |

**Clicks never arrive as `textEvent` — only scrolls do.** This is the single most common event-handling bug on this platform. It also resolves the source question: since presses land in `sysEvent`, `eventSource` **is** available (`1` right temple, `2` ring, `3` left temple), so left/right/ring routing works.

### The protobuf zero-value trap

The transport drops zero-valued fields, so **they arrive as `undefined`, not `0`**:

- `sysEvent.eventType` — a single press is `0`, so it arrives **absent**
- `listEvent.currentSelectItemIndex` — the first row is `0`, so it arrives **absent**

Comparing `eventType === CLICK_EVENT` (i.e. `=== 0`) therefore **never matches**, and the code looks correctly wired while silently doing nothing. **Always `?? 0` on every numeric event field.**

### Exit gesture

Every app is expected to offer an exit via the glasses. The canonical pattern is double-press → `shutDownPageContainer(1)` (confirmation dialog; mode `0` exits immediately with no warning). Do **not** tear down before calling it — the wearer can still cancel. Clean up in the `SYSTEM_EXIT_EVENT` (7) handler instead.

The wearer's menu is the **firmware contextual menu** (tap, then
long-press) carrying the CAPABILITIES rows — see "The three ways to draw a
menu, measured". A double-press on EITHER temple, or on the ring, is BACK
(a no-op on the overview; `GesturePolicy` gives every source the same
meaning because the simulator's generic double-click is right-sourced and
kept opening the exit confirmation), and a left SINGLE press on the
resting overview drills into the notifications tray. The exit dialog is
reachable only through the Mac's `shutdown` downlink. On a live Brainstorm
the double press CANCELS the session; the firmware menu remains reachable
mid-session for everything else. No press is named by source any more: the
Brainstorm acceptance run that armed a right/ring double press was deleted
on 2026-10-06 (its route had needed a raster receipt since Brainstorm became
firmware text).

### Lifecycle events (all `sysEvent`)

| `eventType` | Event | What we do |
|---|---|---|
| `4` | Foreground enter | Repaint status — the frame may be stale |
| `5` | Foreground exit | Flush dirty state (currently none; `Settings` writes through) |
| `6` | Abnormal exit | Stop mic + IMU, close link |
| `7` | System exit | Same — wearer confirmed the dialog |

Ignoring 6 and 7 leaves the glasses mic streaming after the wearer exits. That's a privacy bug, not a resource leak.

## Result codes to handle explicitly

`StartUpPageCreateResult`: `success 0` · `invalid 1` (bad config / z-order) · `oversize 2` (containers exceed display) · `outOfMemory 3`.
`ImageRawDataUpdateResult`: `success` · `imageException` · `imageSizeInvalid` (dims ≠ container dims) · `imageToGray4Failed` · `sendFailed`.

## Our plugin's OOP shape (`apps/hub-plugin/src/`)

| Class | Responsibility |
|---|---|
| `BridgeClient` | Wraps `waitForEvenAppBridge`, enforces the call order, owns all unsubscribes |
| `HudPageBuilder` | Declares the container layout (status bar, main text, list, image slot) with valid IDs + z-order |
| `HudPresenter` | `showText()` / `stream()` via `textContainerUpgrade`; full redraw via `rebuildPageContainer` |
| `AudioStreamer` | `audioControl` on/off; batches `audioPcm` frames and ships them to the Mac |
| `ImuMonitor` | `imuControl`; nod detection → confirm result back to the Mac |
| `EventRouter` | Single `onEvenHubEvent` subscription → typed dispatch (list/text/sys/audio) |
| `MacLink` | WebSocket client to the Mac brain: reconnect, backpressure, framed messages |
| `DeviceMonitor` | `onDeviceStatusChanged` — battery, wearing, charging, connect state. Doff stops the glasses mic locally (fail-safe); resume follows the wearer's `ResumePolicy` (`auto` / `mac` / `confirm`) |
| `Settings` | Wearer-adjustable settings (resume policy), persisted in WebView localStorage; Mac can also set them via `policy.set` downlink |
| `SettingsMenu` | On-glasses settings page: **left-temple double-tap** toggles it, list row click applies + persists + announces to the Mac. Events are consumed locally while open |

`MacLink` is the seam: the Mac's `HubGatewayTransport` speaks the same message protocol whether the peer is this plugin or `men-g2-ble-gateway`.

### The three ways to draw a menu, measured

Captured through the official simulator on 2026-09-01, because the choice
between them is not a matter of taste:

| Mechanism | Cost | Rows visible of 8 | Type | Verdict |
|---|---|---|---|---|
| `menuObject` (firmware contextual menu) | **zero image tiles** | ~5, then scrolls | OS font, OS row pitch, no icons | **In use.** The wearer's menu. |
| `ListContainerProperty` (native list) | text only, no tiles; **scrolls in firmware at zero BLE** | ~4.5 in a 184 px container, then the firmware scrolls (Even's display docs; the simulator clipped) | identical OS font and pitch; **each row ≤ 63 UTF-8 bytes** (the host refuses the rebuild with a silent `false` from 64 bytes, measured in the 0.9.5 simulator; `listLayout` clamps) | **In use for every list but the Planning month** (`usesFirmwareList` in `CapabilityMenu`: a list is Even's own unless it carries the month grid, which has no firmware template; a reader is a text page; the thread and the archive page by tappable rows when the Mac sends `pageControls`). Rejected for the menu itself — no denser, and it costs a page. |
| Four image tiles (our own canvas) | 2 tile writes, 0.5-2s each over BLE | 8 + icons + rail + live dashboard beside it | ours | The only way to get JOAT's density — and the reason page opens feel slow. |

Both native mechanisms render in the firmware's own proportional font at a
fixed row pitch: `MenuItemProperty` exposes only `itemName` and `itemID`, and
list containers have no per-item height or styling. **Nothing we write makes a
native surface dense.** That is the whole trade, and it is why this plugin
renders its pages as images at all.

Two constraints worth knowing before redesigning around the contextual menu:
it can only be opened by the wearer — **the bridge has no API to open it** —
and the SDK never documents which gesture does so. Omitting `menuObject`
does not remove the gesture; the README says a rebuild without it "restores
default registration", so the OS shows its own menu instead of ours.

The resting-page design and the pages the wearer moves between live in
[HUD_DASHBOARD.md](HUD_DASHBOARD.md).

### The rendering cost model, measured (2026-09-02)

Every page is one 576x288 canvas, split into the SDK's four maximum-size
image containers (288x144 each) and shipped with `updateImageRawData`, one
tile at a time, only for tiles whose pixels changed. Probing the installed
SDK (0.0.14) with a stubbed host showed what one tile costs on each hop:

| Hop | What crosses it | Cost |
|---|---|---|
| WebView → phone (`flutter_inappwebview`) | a JSON *number array* of the PNG bytes — ~3.6 text bytes per byte (74 KB of JSON for a 20 KB tile) | milliseconds |
| Phone → glasses (BLE) | the tile as packed Gray4, LZ4-compressed by the host | **0.5-2 s per tile** — Even's own figure in the `glasses-ui` skill; there is no delta encoding |
| Native text (`textContainerUpgrade`) | a few bytes | "much faster than image updates — use text for anything that needs to feel instant" (same skill) |

So the HUD's speed IS the number of tiles an interaction dirties, and the
seam at x=288 is the whole game: anything drawn across it costs two tiles.
Until 0.1.72 the state word was centred ON the seam and the microphone sat
at x=410, so "listening → hearing you… → reply" shipped both top tiles per
change; the resting footer differed between the notifications tab and the
other three, so a swipe shipped three tiles.

| Interaction | Tiles before | Tiles after |
|---|---|---|
| Mac state word ("hearing you…") | 2 | **1** — drawn inside `SPINE_LABEL_BOX`, which ends on the seam, so only the top-left tile changes (the native-text twin was never wired; see "levers" below) |
| Mic on/off, battery | 2 / 1 | **1** (top-right) on an image page; **0** on a firmware page — one in-place upgrade of its header line (2026-10-05; it used to be dropped there) |
| Swipe between resting tabs | 3 (rail, pane, footer) | **2** |
| Open MESSAGES, ACTIVITY or the AGENT TERMINAL picker | 4 | **0** — one `rebuildPageContainer` to a firmware list page |
| Open a Messages conversation thread from the picker | 4 | **0** — one rebuild to a firmware text page: the thread as one reader, newest message first, every message whole (2026-10-06) |
| Page the thread, older messages included | 2-4 | **0** — a swipe pages it in place; past its edge the menu asks the Mac for its next reader page, and past the last page the Mac reads older messages from chat.db |
| Reply: tap anywhere on the thread | 4 | **0** — the question ("What do you want to say back to Adele?") and then the wearer's words for consent draw in JOAT's conversation card (one rebuild each); tap sends, long-press says it again, double-click cancels to the thread |
| After the send | 4 | **0** — no receipt page: the thread is repainted with the reply as its newest message |
| Any other question page ("What should I note?") | 4 (`paintExact`) | **0** — one rebuild to a firmware text page; dismissal returns the retained page (image, firmware list or terminal) |
| Open Brainstorm (the idle picker) | 4 | **0** — one rebuild to a firmware list of past sessions plus NEW |
| A Brainstorm frame (ready, listening, running, settled, unavailable) | 1-2 per revision, plus 1-2 **every second** while thinking | **0** — the Agent Terminal's two-text page; each revision is one `textContainerUpgrade`, and the per-second counter is gone |
| Open QuickNotes or Notifications | 4 | **0** — firmware lists; the archive pages by its own "▲ Newer notes" / "▼ Older notes" rows (18 notes a page, so both rows fit the list's 20) |
| Open a note, a notification's detail, a Planning entry, or the Navigation instructions | 4 | **0** — firmware text pages (`TEXT_PAGE_TITLES`), paged in place; Back returns the list as the firmware list it was |
| Expand the NEWS, SEARCH or POMODORO tab from the rail | 4 | **0** — firmware lists; the title row names the tab, the rail stays on the overview |
| Open a headline or a result | 4 | **0** — `NEWS · ARTICLE` / `SEARCH · RESULT` text pages, paged in place; the Mac is asked only past the local edge |
| The expanded Pomodoro countdown | **1 tile every second** | **0** — a whole-minute first row ("Focus · block 2 of 4 · 24 min left"); the page is rebuilt once a minute |
| The Settings page | 4 | **0** — firmware list |
| Planning week and day pages, and Back between them | 4 per page | **0** — firmware lists; the Mac resolves every row by index and owns Back. The month dashboard keeps its four tiles |
| Scroll rows on those pages | 2-4 per step | **0** — the firmware moves the highlight |
| Back from a firmware list to the overview | 4 | 4 + one rebuild |
| Tap into any other tab, any page change | 4 | 4 — a new page is new pixels |
| Assistant answer show, then dismiss | 4 + 4 | 0 (one text rebuild) + 4 + one rebuild; receipts 2 + 2 |
| Clock minute tick on a feature page | 1 | 1 on an image page; **0** on a firmware page (header upgrade) |

Two host facts from Even's own display docs shape the pipeline
(`hub.evenrealities.com/docs/build/display`, read 2026-09-02):

- "Each `updateImageRawData` holds the image path for 100ms. A call that
  arrives inside that window is held and flushed on the next one." So a
  frame's LAST tile sent inside the window waits for a call that may never
  come — a torn page. `HudVisualSurface` paces tile calls
  `TILE_CALL_GAP_MS` (110 ms) apart.
- "a `success` means the update was accepted, not that the pixels are lit."
  `durationMs` on a `tile_upload` receipt is therefore the phone's answer
  time, not BLE delivery; only the wearer's eyes measure the hop.

Community measurements (nickustinov/even-g2-notes, `docs/performance.md`)
put the fixed per-call costs at ~104 ms for an image update (+ ~3.9 ms per
KB of Gray4), ~83 ms for a text upgrade and ~165 ms flat for a rebuild —
"fewer host calls beats cheaper host calls". That is why a firmware list
page (one rebuild) beats a pixel page (four calls plus BLE) on entry, and
why a highlight move on it costs nothing at all.

Two pipeline rules landed with it, in `HudVisualSurface`: frames that
arrive within `PAINT_COALESCE_MS` (40 ms) of each other render as ONE
frame, so a burst of Mac messages about one voice turn cannot ship the
spine twice; and a tile write that has not settled within
`TILE_UPLOAD_TIMEOUT_MS` (15 s) fails the frame instead of wedging the
single paint lane forever — a wedged lane looks exactly like
"notifications stopped showing and the menu stopped opening".

**Measure before the next change.** With `--g2-diagnostics` on the Mac,
every `tile_upload` receipt in
`~/Library/Application Support/JOAT/g2-diagnostics.json` carries
`durationMs` (the bridge round trip, from the call to the host's answer)
and `bytes` (the encoded PNG handed to the bridge). Read the per-tile
duration off real hardware before touching the pipeline again; the
simulator has no BLE and proves nothing about speed.

The levers that remain are design decisions, not code:

- **Native text for the spine word and the footer hint** — text containers
  layered over the tiles with `zOrderIndex`, updated with
  `textContainerUpgrade`. A voice-state change would then cost zero tiles.
  The price: the firmware's proportional font at a fixed 27 px line height
  (see the `font-measurement` skill), not the 10-12 px canvas type. Taken
  for the firmware pages on 2026-10-05: their one header line (`9:41 ·
  PAGE · ● · ▇`, composed from `HudStatusModel`) is upgraded in place on
  every clock, mic or battery change. The image pages keep the 24 px
  raster spine with icons, measured into the top tiles.
- ~~A native list for plain-row pages~~ — 2026-10-05: every list is a
  firmware list (`usesFirmwareList` in `CapabilityMenu`); the one exception
  is the Planning month grid, which has no Even template. The presenter
  tracks which page the glass holds (`pageOwner`): a native list rebuilds
  to `listLayout` with the live header in the rebuild, and the image pages
  (overview, month, map) rebuild the four-tile page first. The trade is the
  firmware font, no rail on an expanded tab, and a cursor that always
  starts on row one (the firmware has no cursor-setting API).
- ~~Native conversation thread~~ — 2026-10-05: the firmware list emits
  clicks but no scroll events, so the Mac pages the thread itself. The
  `hud.list` for `MESSAGES · CONVERSATION` carries `pageControls`
  (`newerIndex`, `olderIndex`): the Mac appends "▲ Newer messages" and
  "▼ Older messages" rows and names them, and `CapabilityMenu` turns a tap
  on either into the same `SCROLL_TOP/BOTTOM` page request the raster
  thread sent, ignoring the label and instruction rows. Rows are clamped
  to 63 UTF-8 bytes (`clampUtf8Bytes` in `HudPageBuilder`): the host
  refused every list whose row reached 64 bytes and the plugin fell to
  Home, which is why the thread first looked broken in the simulator.
  Verified with the probe: older page 0.2 s after the tap, zero image
  tiles from thread to Back.
- ~~Native confirmation~~ — 2026-10-05: the gate is a firmware text page
  (`confirmationLayout`, owner `confirmation-native`); dismissal recreates
  the four image containers, or hands straight to a firmware list. Verified in the official
  simulator with `scripts/simulator_messages_native_probe.py`. The decision
  clock fits inside the Mac's deadline: `confirmationDecisionWindowMs` in
  `ConfirmationCommit` takes the Mac's `expiresAtMs`, reserves four seconds
  for the restore an acceptance waits on, and clamps to 3-8 s, while the
  Mac's envelope (`CONFIRM_TRANSPORT_SECONDS`, `_CONFIRM_SECONDS`) is 20 s
  so the paint in, the eight-second window and the restore out all fit.
  Before, the Mac timed 12 s from send and the plugin always took 8 s, so a
  tap the wearer saw land could come back as "Reply cancelled.".
- ~~Native answer page~~ — 2026-10-05: an `answer` overlay over the visual
  HUD is `assistantAnswerLayout` (owner `overlay-native`): status, a
  seven-line body paged in place with `textContainerUpgrade`, a hint line.
  Receipts stay on the image page (two tiles beat a page swap).
- ~~Native message reader and question page~~ — 2026-10-05 (the
  single-message reader was folded into the thread reader on 2026-10-06,
  `MESSAGES · CONVERSATION`): a `hud.list` titled as a reader
  (`TEXT_PAGE_TITLES` in `CapabilityMenu`)
  opts into `nativeTextPage`: the presenter joins the Mac's rows, pages
  them for the firmware font with `paginateAnswer`, draws `textPageLayout`
  (owner `text-page-native`) and pages in place on swipe; a swipe past
  the local edge is not consumed, so the menu asks the Mac for its
  neighbouring page through the same `cappage` request. The question page
  is `promptQuestionLayout` (owner `prompt-native`). Every Messages page on
  the reply path is now firmware-drawn; the probe records zero image tiles
  from picker to result and back. `?joatTestPromptMic=1` (DEV) lets the
  simulator, which has no microphone, keep a question on screen.
  2026-10-06: every reader the Mac pages asks for its neighbouring page
  past the local edge (`requestReaderPage`: the title's family, the
  reader's id and page), where only QuickNotes and the Messages review
  could; the saved Brainstorm reader stopped at its first page. A review
  (`REVIEW_READER_TITLES`: the Messages review, the terminal's
  `AGENT TERMINAL · CHECK`) sends only from the last firmware page of the
  Mac's last page; before it a tap reads on, and the footer offers the tap
  only there.
- ~~Native Brainstorm~~ — 2026-10-05: a `brainstorm.view` frame rides the
  Agent Terminal's two-text page (`NativeTerminalSurface`, owner
  `terminal-conversation-native`): head `JOAT · BRAINSTORM`, a status row with the state
  and the live speaker, the Mac's activity text as the transcript (paged
  on the glasses by swipe), and a footer in the raster hint's words
  measured for the firmware font. The idle picker (`BRAINSTORM`) is a
  firmware list. Three Mac-side causes of "Brainstorm does nothing" went
  with it: `find_codex_binary` was PATH-only (a launchd-started brain
  never saw the ChatGPT-bundled `codex`), the idle row promised "Ready for
  a question" even with a seat missing (it now shows the host's
  unavailable frame first), and the idle, NEW and unavailable frames were
  sent without the `hud.rebuild reason=brainstorm` that claims the surface
  — the plugin dropped them, so a voice-opened Brainstorm showed nothing
  and its tap went nowhere. `scripts/simulator_brainstorm_native_probe.py`
  plays the Mac's side of a session; its run shows zero image tiles from
  picker to settled and the tap arriving as `begin_topic_capture`.
- ~~Native QuickNotes, Notifications, readers~~ — 2026-10-05: `QUICKNOTES`,
  `NOTIFICATIONS` (and their EMPTY/SETUP pages) join the firmware list;
  `QUICKNOTES · NOTE/DETAIL`, `NOTIFICATIONS · DETAIL`, `PLANNING · EVENT`
  (any origin and kind) and the static `NAVIGATION` page are text pages.
  The archive carries `pageControls` like the thread: the Mac appends the
  paging rows (`_NOTE_LIST_PAGE_SIZE` dropped to 18 so both fit the list's
  20-row cap) and a tap on either sends the `QUICKNOTES · LIST` page request
  the edge swipe used to. A spoken "show notifications" is adopted like the
  menu capabilities. Still raster: the Planning month grid (a calendar has no
  Even template). `scripts/simulator_quicknotes_native_probe.py`
  records zero tiles across list, both pages, the note, Back, the
  Notifications list and a card's detail.
- ~~Native panel tabs and Settings~~ — 2026-10-05: `openPanelTab` opts NEWS,
  SEARCH and POMODORO into the firmware list and the article/result readers
  into text pages; the Settings page is a firmware list too. The rail is
  the overview's; the expanded page has a title row instead. Row memory on
  Back is gone with the raster list (the firmware has no cursor-setting
  API). The
  Pomodoro's per-second tile is replaced by a plugin-authored whole-minute
  first row (`pomodoroCountdownRow`) that the tap mapping skips
  (`panelRowOffset`); the presenter's panel ticker runs at 60 s on a
  firmware page and calls `onNativePanelTick`, which the menu answers by
  rebuilding the page.
- ~~Native Planning week and day~~ — 2026-10-05: a Planning page that
  carries the Mac's row map (`planningNavigation`) and no month grid is a
  firmware list (`planningList` in `presentList`). Every row is a Mac
  action by index (day, entry, previous, next, scope, earlier/more
  entries) and Back is the Mac's `planning-back`, so nothing changes on
  the wire; the day page's 13-entry paging keeps it under the list's 20
  rows. The month dashboard (grid plus hovered-day pane) is the one raster
  capability page left: a calendar grid has no Even template. (A
  `PLANNING · EVENT` or `· REMINDER` page carries the row map too, so it is
  a firmware list, not the text page the earlier note above says.)
  `scripts/simulator_planning_native_probe.py` drives month → week → day →
  event and Back over a fake EventKit bridge: zero tiles on every page but
  the month.
  `scripts/simulator_panels_native_probe.py` pushes the three tabs and
  records zero tiles on each page, the article, its paging, Back and the
  minute rebuild; the overview's own rail swipes are the only tiles.

- ~~Pages as control surfaces~~ — 2026-10-05, the wearer's rule: a page
  opened by hand is for hands. Voice works from anywhere, so no page shows
  a "Say “JOAT, …”" row or empty state any more; each offers the tap that
  does the thing. The row asks its own question through the acknowledged
  prompt (`PromptedSpeechRuntime.ask_capability`, owner kind `capability`
  on `prompt.present`, the same native question page the Messages body
  uses) and routes the answer: NAVIGATION "➤ Navigate to a place" asks
  "Where to?" and starts the route; QUICKNOTES leads every page with
  "● Record a voice note" (a tap-started capture titled by the clock) and
  "✎ Dictate a note" ("What should I note?", saved as exact text; the page
  size dropped to 16 so both fit under the 20-row cap); a tap on the Messages
  conversation is the reply, the same prepared reply the spoken word
  starts (the "✎ Reply" row went with the list, 2026-10-06); the SEARCH tab's last row "⌕ Look something up" asks for the
  query and republishes the tab; the expanded POMODORO shows its controls
  and, while idle, the three lengths as rows that cycle presets on tap and
  persist. Brainstorm's idle frame says "Tap to speak a topic." and its
  picker already carried "+ New brainstorm".

- ~~The modals a tap lands on~~ — 2026-10-05, the wearer's point: fixing a
  page is not enough when the page its tap redirects to is still pixels. A
  surface-and-transition map (every page, every tap, Back and push target)
  found thirteen raster targets one gesture away from a firmware page, and
  one presenter fault: from a firmware text page, Home, a receipt, a
  productivity frame or a navigation frame never repainted the glass at all
  (only the firmware list owner triggered the image rebuild). Now:
  - every native content owner (`terminal-list-native`,
    `text-page-native`, `navigation-native`) recreates the four image
    containers before an image paint (`NATIVE_CONTENT_OWNERS`);
  - a receipt or an outcome strip over a firmware page is a firmware page
    (`usesNativeAnswerPage`, `showActionPill`): one rebuild in, and the
    list or reader comes back as the page it was when the strip settles;
    before, a saved reminder over the Planning day rebuilt the image page
    and repainted the list as pixels under a two-tile strip;
  - a Calendar or Reminders page (failed, uncertain, due) is a firmware
    text page (`renderNativeProductivityPage`), paged in place;
  - every navigation frame before the live map (confirm the place, repair,
    mode, preview, the warnings, arrival, error) is a firmware text page
    (`navigationNativeBody` in `NavigationHud`, owner `navigation-native`);
    a cursor move upgrades the page in place; only the guidance frame with
    its map is pixels;
  - the Messages result (`MESSAGES · SENT/FAILED/CANCELLED/UNCERTAIN`), the
    draft review, compose, setup, access and retry pages, the QuickNotes
    recorder (`RECORDING/PAUSED/DISCARD?`), the saved Brainstorm reader,
    `ACTIVITY · EMPTY`, the Planning refresh and reminder notices, the
    Planning loading page and every `X · RETRY` timeout page joined the
    firmware sets; a reader with a tap action is selectable so its tap
    reaches the Mac; the legacy review's staged return keeps the
    conversation's firmware-list shape; Back on a resting Brainstorm frame
    is Home.
  `scripts/simulator_transitions_native_probe.py` drives all of it from a
  firmware list and records zero image tiles on every step but the map.
  Still pixels by design: the overview, the Planning month grid, the live
  map.
- ~~The raster duplicates~~ — 2026-10-05, the wearer's question: "are you
  sure you are deleting any duplicates?" They were not deleted; they were
  bypassed. The renderer still carried a canvas painter for every page the
  firmware now draws (the question, the confirmation, Brainstorm and the
  Agent Terminal transcript, the Calendar and Reminders pages, every
  navigation frame but the map, the dense list, the readers, the Messages
  thread, the Pomodoro page, the paged answer card), the presenter kept a
  pixel branch for each behind a check of the visual surface's transition
  barrier that production always passes, and `CapabilityMenu` kept the
  prose pagers, the plugin-owned cursor and the title allowlist that
  decided which list went native. All of it is gone: `HudCanvasRenderer`
  draws the overview, the Planning month and dashboard, the live map and
  the receipt strip over those, and nothing else (2,157 lines, from 4,513);
  the presenter has one path per page; a list is Even's own unless it is
  the month grid (`usesFirmwareList`, and the presenter itself defaults any
  unflagged list to a firmware list); the browser preview hands pages over
  like the glass. The first pass (0.1.98) missed the raster frame kinds,
  the conversation card (the presenter only ever built the native one),
  and the plugin-owned list cursor; a second pass with export and method
  sweeps removed them. The tests that pinned the pixel versions went with
  them; the native pages keep theirs.
- ~~Text measured by hand~~ — 2026-10-05: the plugin wrapped and paged
  native text with its own word-wrap over `getTextWidth` and sized the
  confirmation body by a 120-character guess. `FirmwareText.ts` measures
  with `measureTextWrap` from `@evenrealities/pretext`, Even's model of the
  LVGL renderer (per-glyph rounding, breaks at spaces, hyphens and CJK
  boundaries, leading spaces dropped at a line start): a page goes to the
  firmware unwrapped and `paginateMeasured` decides how many words it
  holds; the confirmation's prompt is cut by `fitLines` to the three lines
  it has, by glyph width rather than character count; a terminal row is one
  line when the firmware's own width says so. Tests pin that no page
  exceeds its container under that model.
- ~~Three features at once~~ — 2026-10-05, the reason the wearer built
  their own plugin: Even's Navigate, Conversate and a timer cannot share the
  lens, JOAT's features can. Two changes. A route keeps running behind any
  other page: `NavigationController` sends progress through
  `HudPresenter.updateNavigation`, which repaints only while the route owns
  the glass (its page or Home) and otherwise updates the retained page and
  the header chip; before, every frame went through `showNavigation`, which
  hands off whatever is on the glass, so a conversation or a Pomodoro page
  lasted until the next step. And every header carries the other running
  features as chips (`ActivityChips`: "Focus 12m", "→ 80 m"), in the
  firmware header line and in the image spine, refreshed in place; the
  conversation page gained the header line (`assistantConversationLayout`,
  written through the conversation surface's own serialized lane). Verified
  in the official simulator with the `joatVisual=live-concurrency` dev
  state: the map with the timer in the spine, a conversation whose header
  carries the timer and the turn, two route updates behind it written in
  place, and the map back at the newest frame.
- ~~The due page completes by tap~~ — 2026-10-05: the pushed due Reminder
  page was the last page whose hint named a word to say. Its tap now sends
  `reminder_due.complete` with exactly the opaque occurrence and attempt
  UUIDs and the generation the Mac bound the page to when the plugin
  acknowledged it; the gateway admits the tap only against that accepted
  page, once, and never after the socket it was shown on; the task monitor
  resolves the occurrence to its native item and runs the same confirmed
  completion the spoken "complete reminder" runs, so the glasses still ask
  before anything is written. The hint reads "tap: complete".

### Downlinks and uplinks: what drives them (2026-10-05)

The audit found frames defined on both sides that nothing sent. Where they
stand now:

| Frame | Status |
|---|---|
| `imu.control`, `imu.trace` ↓ / `imu` ↑ | **Wired.** `joat --imu-trace PATH` arms them on every hello and records samples (docs/NEXT_STEPS.md §5). Off by default: ~10 messages/s. |
| `audio.control open=true` after a don | **Wired** for resume policy "mac": the Mac remembers the plugin's `policy` report and answers a worn report with the mic off by asking for ears. "auto" and "confirm" stay plugin-local. |
| `policy.set` ↓ | Mac-side hook, no caller: the wearer sets the policy on the phone/glasses; the Mac only reads it. Kept so a Mac-app settings pane can drive it. |
| `shutdown` ↓ | Mac-side hook, no caller: `shutDownPageContainer` exits the plugin on the glasses, which is wrong on a brain restart (the RECONNECTING card refreshes itself). Kept for an explicit quit. |
| `assistant.conversation dismiss` ↓ | Mac-side hook, no caller: the conversation page leaves on Back. Kept. |
| `brainstorm.action show_brainstorm` ↑ | Plugin never emits it (a running tap sends `show_terminal`). Harmless Mac branch, kept. |
| `productivity.view.ack`, `confirm` callbacks ↑ | Parsed; the pending futures resolve them, the callback lists stay empty. |
- **Fewer grey levels** — anti-aliased text is what LZ4 compresses worst.
  Quantising to four levels would shrink each tile's BLE payload, but the
  gain is unmeasured until `durationMs` is read from hardware.

### The startup page is one-shot PER HOST SESSION

`createStartUpPageContainer` succeeds once per HOST session, not per page
load. A WebView reload (vite HMR, backgrounding, a relaunch) therefore gets
`invalid` on the second call, and `rebuildPageContainer` can report success
WITHOUT materialising the containers — after which every
`textContainerUpgrade` fails and the HUD is blank for the rest of the
session with no error surfaced anywhere.

Seen in the simulator as 382 consecutive `TextContainerUpgrade failed:
container N not found` warnings behind a blank display, plus
`inject_input: no active event container`. A fresh simulator session
rendered the identical build correctly — which is the tell: if the HUD is
blank but the plugin is otherwise alive (audio streaming, link connected),
suspect a stale host session before suspecting the layout.

`HudPresenter` now checks EVERY paint result and rebuilds the page once on
failure, so any cause of a missing page self-heals on the next write.
Diagnose with the simulator's log, not its screenshot: the screenshot only
tells you it is blank.

## Deployment constraints

- **The Mac's WebSocket origin must be in `app.json`'s `network` permission `whitelist`** (there is no `websocketDomains` key — that was a pre-verification guess). The Hub silently blocks anything unlisted: the socket simply never opens, indistinguishable from an offline Mac. Keep `VITE_MAC_WS_URL` and the whitelist in sync, including scheme and port. Direct development may use the gateway's plain `ws://…:8787`; an unattended Tailnet home brain instead exposes a private Tailscale Serve port-443 TLS edge and therefore uses `wss://<mini>.ts.net/glasses`. Do not label the local plain-WebSocket port as `wss://…:8787`. Stable hostname over churning DHCP IP (Tailscale MagicDNS or the Mac's `.local` mDNS name).
- `navigator.geolocation` only works in a **published** Hub plugin, not via QR sideload — use `bridge.getAppLocation()` instead, which works in both.
- **The guidance page follows the wearer (2026-10-05).** It used to repaint only when the maneuver or the rounded distance changed, so over a kilometre the dot, the progress bar and the ETA froze for a hundred metres at a time; the street tile was fetched once at the origin and never again; a fix the host reported without an `accuracy` figure, or with a `timestamp` in seconds, was rejected as uncertain for the whole walk; and a route could be acked as installed while the location stream had never started. Now: a fix that moved the wearer repaints the page (at most once a second, coalesced to the newest fix, and the Mac hears nothing — only semantic states are sent); the Mac prefetches tiles along the route and the plugin draws the one nearest the wearer; without a tile the pane draws the route's shape around the wearer instead of disappearing; a fix without `accuracy` is trusted at the policy's limit and a timestamp's unit is read against its arrival time; a navigation consumer retries the stream start and the route is rejected (the Mac shows "Navigation could not start") when it truly cannot run. The first fix of each route logs its shape (`[joat] navigation fix shape`, no position) so a field run can tell which optional fields the host fills.
- **Saved places live on the phone page's Places tab** (`Places.ts`, `PlacesPanel.ts`): a name plus an address and/or the phone's pinned location, mirrored to the Even host's storage, sent whole to the Mac on every change and every connect (`places` uplink). The glasses' Navigation page lists them first; "JOAT, navigate home" resolves on the Mac.
- Phone lock/background (official lifecycle doc): on **iOS the WebView keeps running** — JS state and usually the WebSocket survive, and the store QA gate is exactly this (5-minute lock, state preserved). What always stops when the app is suspended is **audio capture** — so the HUD keeps working through a lock, but voice needs the Even app foregrounded. Continuous listening still wants Guided Access / Auto-Lock Never, or Phase 5 (custom companion). Android WebViews may be killed under memory pressure — treat resume as a cold restart (localStorage survives; we already persist settings there).
- Dev loop: the official CLI + simulator (`npm install -g --prefix="$HOME/.local" @evenrealities/evenhub-cli @evenrealities/evenhub-simulator`). Binaries: `evenhub` (alias `eh`) — `evenhub init` scaffolds `app.json` only, `evenhub qr` sideloads with hot reload, `evenhub pack app.json dist` builds the `.ehpk`. The simulator (`evenhub-simulator <url> --automation-port 9898`) injects the bridge and exposes an HTTP automation API — see [TESTING.md](TESTING.md) tier 2. No Even account needed for dev; `evenhub login` is only for store submission.

## On-device runbook (verified against hub.evenrealities.com/docs, 2026-08)

Everything above ran only in the simulator until now. This is the path onto
the real phone + glasses. Toolchain on this Mac: `evenhub` CLI 0.1.14 at
`~/.local/bin/evenhub` (current); `npm run pack` produces a valid `.ehpk`
(111 KB, verified).

### One-time: enable Developer Mode

1. Glasses paired in the Even Realities app, firmware updated ("most early
   connection failures trace back to stale firmware" — their words).
2. Sign in at <https://hub.evenrealities.com/login> with the **same account**
   as the phone app.
3. Force-quit the Even app (swipe away, not background) and reopen. A
   **developer section appears top-right of the Even Hub tab** with Scan QR.

### Path A — dev server + QR (hot reload; use this for iteration)

```
./scripts/dev_on_device.sh
```

Starts Vite on the LAN and prints the QR. Scan it from the Even Hub tab;
the glasses render the plugin within a second, and saved edits hot-reload
without re-scanning. Phone and Mac must be on the same network, and the
engine must be up for voice/cards (the script warns on both).

### Path B — packed build (survives without the dev server)

```
cd apps/hub-plugin && npm run pack     # -> apps/hub-plugin/joat.ehpk (gitignored)
```

Upload it with **Upload a build** in the project at hub.evenrealities.com;
the portal marks it **Private**. As of 2026-10-13 the project shows the
tabs **Builds**, **Testing group** and **Store listing**.

Lock survival depends on how a build is installed. Even's Testing pages:
"Private builds survive backgrounding briefly but don't pass the 5-minute
lock test the QA reviewer runs", and Beta builds behave like a released
app. Those pages describe a "Private builds" tab and a "Beta groups" tab,
which this portal does not show, so how to get a Beta-equivalent install
from **Testing group** is not confirmed. Until it is, expect a Private
install to be killed while the phone is locked; JOAT's reconnect, the
heartbeat and the QuickNote re-attach bring the session back when the
plugin returns. Iteration is rebuild → re-upload → reinstall, so do Path A
first.

**Bump the version before every upload.** `--enforce-manual-version` means
the packer never invents one; nothing bumps it automatically, so an
unbumped repack collides with what is already on the hub and the only
offer is "add and replace" — silently overwriting a build that was not
this one. The number lives in `app.json`, `package.json`, and
`package-lock.json`; all three move together (`npm version <x.y.z>
--no-git-tag-version` moves the last two), and `pack.sh` refuses a package
whose three numbers differ.

**The packer is deterministic** (measured 2026-09-01: two packs of one
clean worktree were byte-identical, SHA-256 `f4a597b5…` both times). So a
hash IS content identity: two artifacts on the same version string with
different hashes contain different code, and one of them is a build
nobody can name. Pack from a detached worktree at the revision you intend
to ship — never from a shared tree carrying another session's in-flight
edits — and record `<version, short-rev, sha256>` for whatever you upload.

### First-session checklist (maps to the Phase 0/1 metrics)

1. Page renders: status bar + main text on the real HUD.
2. Say "JOAT, what time is it" — transcript back on the HUD; note the
   wall-clock latency vs the ~2.2 s desk-mic number.
3. Right-temple click and R1 ring answer a `confirm.request`.
4. Send yourself an iMessage — card appears on the HUD (< 5 s target).
5. Left-temple double-tap — settings menu opens on the glasses.
6. Lock the phone 5 minutes, unlock — page state intact (the store QA gate).

### Pairing and application auth (RULES.md #5)

The gateway no longer treats missing state as consent. Pairing begins only
after the wearer presses **Pair / Replace Glasses** in JOAT.app, which creates
an owner-only marker that expires after 120 seconds. During that window:

1. Every socket sends an `auth` frame first. A new client sends a null token.
2. The Mac offers a token (`pairing.set`). The plugin persists it locally and
   acks (`pairing`) only after the write succeeds.
3. The Mac persists on the ack, consumes the pairing window, and sends
   `auth.accepted`. Only then does the plugin report `connected` or flush
   queued audio and commands.
4. On later dials the stored token is sent in the first `auth` frame. It never
   appears in the WebSocket URL, access logs, browser history, or status UI.

The offer/ack split exists because persisting at offer time strands the
wearer: a Wi-Fi blip during the pairing frame would leave the Mac holding a
token no phone has, 401-ing the real phone forever. Unacked offers keep the
window open, so a failed pairing costs a retry, not a manual reset.

With **Private Tailnet home brain** on (the normal setup), nothing is exempt:
every peer, loopback included, must present the key. A hand-run engine
without it still trusts loopback for the simulator and `speak_test.py`.

- Token store (since 2026-10-13): the login keychain, item
  `com.joat.shell.pairing` / `glasses`, readable without a macOS prompt only
  by JOAT.app. The app hands the key to the engine over its private launch
  pipe and keeps any newly paired key there. The first launch moves an old
  `pairing.json` into the keychain and deletes it; a `pairing.json` that
  reappears later is ignored. A hand-run engine still uses `pairing.json`.
- Pairing window: opened only by JOAT.app's **Pair / Replace Glasses**, over
  that same pipe, for 120 seconds. No file opens it.
- Only the same glasses take the link: while a display that named its
  glasses holds it, a newer display must name the same glasses in its hello
  within 3 s, or it is closed 4409 and waits as a standby.
- **Re-pair**: press **Pair / Replace Glasses** in JOAT.app, then connect the
  intended glasses within 120 seconds.
- **Stuck on "reconnecting" after reinstalling the plugin**: the Mac is
  paired to a token the phone no longer has (reinstall, cleared storage, a
  different phone). The phone cannot fix this itself — it cannot
  authenticate to ask — so the authority is physical access to the Mac:
  press **Pair / Replace Glasses** in JOAT.app. During that explicit window,
  the next dial can adopt the phone's already-persisted token. The engine logs a
  PAIRING LOCKOUT error naming this fix after five consecutive refusals.
- **Lost-ack recovery**: the token is not real on the Mac until the ack lands.
  If the phone stored a token but the ack was lost, press **Pair / Replace
  Glasses**; its first auth frame lets the Mac adopt that already-persisted
  token inside the bounded window.
- **Dev server ↔ packed build**: localStorage is origin-scoped, so Path A and
  Path B hold *separate* tokens. Switching between them hits the same 401
  symptom — and looks exactly like the undocumented `ws://` risk below.
  Use **Pair / Replace Glasses** with the build you are testing. Pick one path
  per bring-up session.
- The plugin **ignores a second `pairing.set`** while it already holds a
  token. Without that guard, any LAN peer answering the plain-`ws` mDNS name
  in the whitelist could overwrite the good token and lock the wearer out of
  their own brain until localStorage was wiped by hand.
- Honest limit: moving the token into an application frame removes URL/log
  leakage but does not encrypt plain `ws://`. An active LAN observer can still
  read frames. Consequential writes require the private Tailnet `wss://` path:
  enable JOAT.app's **Private Tailnet home brain** setting, restart it, and
  configure Tailscale Serve to proxy only `http://127.0.0.1:8787`. The proxy
  must not make JOAT trust loopback; paired Hub authentication remains required.

### Wrong screen: "asking for an auth token"

If the phone asks for an **auth token**, you are on the **Even Terminal**
connect screen (Terminal Mode, enabled in the Even app's *Settings*), not the
Hub developer screen. Terminal takes a URL *plus* a token because its own
server mints one; `evenhub qr` emits a bare URL, so the token field stays
empty and the connect fails.

**The Hub developer QR flow has no auth token at all** — verified three ways:
`evenhub qr` encodes only the literal URL (`~/.config/evenhub/qr.yaml` holds
just scheme/host/port/path), the CLI's `qr` handler never reads credentials,
and no Even Hub doc mentions a token for developer mode or QR scanning.

Right screen: **Even Hub tab → developer section, top right → Scan QR** (a
camera viewfinder, no text fields). If that section is missing, Developer
Mode is not active — and there is **no toggle**: sign in at
hub.evenrealities.com/login with the same account registered *in the phone
app*, then **force-quit** the Even app (swipe from recents; backgrounding is
not enough) and reopen.

`evenhub login` is NOT the fix — it only gates `pack --check`, writes
`~/.config/evenhub/credentials.yaml`, and is never read by `qr`.

Before blaming auth, open the dev-server URL in the **phone's browser** on the
same Wi-Fi. If that fails it is firewall or AP isolation, not a token.

### On-device triage (symptom first)

Three readings decide nearly everything: the **phone panel** (`p-url`, `p-link`, `p-mic`, `p-status`), the **engine log** (`plugin connected` / `rejected unpaired connection` / silence), and whether **SYNs reach 8787**. In every table below, causes are ordered most likely first.

#### First 60 seconds

1. **Read the phone panel.** Body replaced by `boot failed: …` → go to *"boot failed: …"*. `p-url` still `—` → *No JOAT page at all*. `p-url` filled but `p-link` stuck `idle` → bridge hang, *No JOAT page at all*. Panel intact and cycling `reconnecting` → continue.
2. **Tail the engine log.** Which line appears: nothing at all, `plugin connected`, or `rejected unpaired connection from …`? That fork routes almost every remaining case.
3. **If `PAIRING LOCKOUT` or auth code 4401 repeats:** press **Pair / Replace Glasses** in JOAT.app, then reconnect within 120 seconds. Do not delete the store file.
4. **If the log is silent:** `sudo tcpdump -ni any tcp port 8787` while the plugin retries. **Zero SYNs** = blocked before the network (whitelist / iOS Local Network / AP isolation). **SYNs present** = it reaches the Mac; the problem is auth or the app.
5. **Check the dial target against reality:** compare `p-url` literally to `apps/hub-plugin/app.json`, then `ipconfig getifaddr en0` and `lsof -nP -iTCP:8787 -sTCP:LISTEN` (IPv4-only listed is itself a cause — see *Stuck on "connecting"*).
6. **Reload the plugin page**, then `lsof -nP -iTCP:8787` — expect exactly one remote peer. Clears `outOfMemory`, a stale WebView, and a second client flapping the link.

#### Nothing on the HUD — no JOAT page ever appears

| Cause | Distinguisher | Fix |
|---|---|---|
| **Bridge ready event never fires** (`waitForEvenAppBridge` has no timeout) | `p-url` POPULATED, `p-link` still `idle`, engine log completely empty | Race the wait against a ~5 s rejecting timer so it reaches `boot().catch()` |
| Entry module never executed (404 or SyntaxError in WKWebView) | `p-url` stays `—` **and** body NOT replaced. `curl -sI http://<lan-ip>:5190/src/main.ts` (404 vs 200); Safari Web Inspector shows the parse error | Add top-level `esbuild: { target: 'safari15' }` (dev too, unlike `build.target`); add an inline `window.onerror` writing to `#p-status` |
| Boot threw | Body is one line of bare text → see *"boot failed: …"* | — |
| Vite not serving / QR points at a dead port | Phone shows a network error page, not the panel. `curl -sI http://<lan-ip>:5190/` refused; `lsof -i :5190` | Free 5190 before `npm run dev`; confirm the QR host matches the phone's LAN interface. Keep `strictPort: true` |

#### "boot failed: …" on the phone (HUD dark or frozen on the startup page)

The text names the cause — match it exactly.

| Text | Cause | Fix |
|---|---|---|
| `MacLink needs at least one URL` | `VITE_MAC_WS_URL=` present but blank (`??` doesn't guard `''`). Check `grep VITE_MAC_WS_URL apps/hub-plugin/.env` | Default *after* filtering; construct MacLink after `client.start()` |
| `startup page failed: oversize` | Layout payload too large. Reproduces on **every** reload | Fall back to a single-text minimal layout so the wearer sees an error on-glass |
| `startup page failed: outOfMemory` | Host resource state. **Clears on reload** — reload twice to tell it from `oversize` | Retry once after a short delay |
| `The operation is insecure` (SecurityError) | `ws://` from a secure page origin — Path B packed build only. Zero packets on tcpdump; same phone connects fine on Path A | Stay on Path A for bring-up; longer term, Tailscale HTTPS cert + `wss://` tailnet candidate |
| anything, with the link already open | `link.connect()` runs before `getDeviceInfo()` — Mac shows `plugin connected` but never `device … / …`; confirms then stall the full 8 s | Connect after device info, or call `teardown()` from boot's catch |

#### HUD page exists but is blank (empty green canvas)

| Cause | Distinguisher | Fix |
|---|---|---|
| First paint serialized behind a hung `getDeviceInfo()` | Log has `plugin connected` but **never** `device <model> / <sn>`; panel stuck at `booting…` with the link connected. (Empty log instead = bridge hang) | Move `hud.setStatus('JOAT · connecting')` above the call; wrap it in a timeout resolving to `null` (both fields already tolerate null) |
| `invalid` masked by the rebuild fallback (malformed layout treated as "page already exists") | A **fresh** launch (`client.getLaunchSource()`) that still took the rebuild path. Decisive: run `validateEvenHubPageContainer(container)` on the layout | Validate at the top of `BridgeClient.start` and throw; only then treat `invalid` as the reload case |
| Edited layout broke an unenforced invariant | Only if `HudPageBuilder.ts` was touched — shipped layouts are valid. `containerTotalNum` is a hand-kept literal (`2` / `3`) next to the arrays | Derive the count from the arrays; add a test running every layout through `validateEvenHubPageContainer` |

#### Stuck on "JOAT · connecting" — panel cycles connecting → reconnecting

The engine log fork does most of the work: `rejected unpaired connection` = it reached the Mac; silence + zero SYNs = blocked before the network.

| Cause | Distinguisher | Fix |
|---|---|---|
| **Mac holds a pairing token, plugin doesn't** (WebView data cleared, reinstall, or Path A ↔ Path B origins hold separate tokens) | Auth closes with code 4401 once per backoff cycle, and `pairing.json` exists | Press **Pair / Replace Glasses**, reconnect within 120 seconds. Pick one delivery path per session; key the stored token by host |
| **Rotation + backoff stall** (rotates off the good candidate on every close; no per-dial timeout — a blackholed IP wedges ~75 s) | Gap between `plugin disconnected` and the next `plugin connected` is tens of seconds while `lsof -nP -iTCP:8787 -sTCP:LISTEN` shows the listener up | Dial watchdog (~3 s in CONNECTING), sticky last-known-good candidate, reset `attempt` on FOREGROUND_ENTER |
| Dial URL not in the app.json whitelist (Hub blocks silently) | Compare `p-url` literally to `apps/hub-plugin/app.json`. Log shows `hub gateway listening` but never `plugin connected`; tcpdump shows zero SYNs | Import app.json at boot and assert every candidate's origin is whitelisted; fail loud on-glass |
| Built with no `.env` → `wss://mac.local:8787/glasses` fallback (wrong scheme, wrong host, not whitelisted) | `grep -o 'wss\?://[^"]*' apps/hub-plugin/dist/assets/*.js` prints `mac.local` | Commit a `.env.defaults`; make the fallback a loud boot error |
| Whitelist match semantics unverified — entries carry no `/glasses` path, dials do (**NEEDS-EMPIRICAL**) | Zero SYNs despite a host that looks whitelisted | Add one byte-identical entry including `/glasses`, repack, see if it opens. Keep `.env` and `app.json` in the same commit |
| Packed from a stale `dist/` (missing the raw-IP fallback) | `grep -c 192.168.12.200 apps/hub-plugin/dist/assets/*.js` → 0 while `.env` → 1 | Always `npm run pack`; never pack a dist you didn't just build |
| Hardcoded `192.168.12.200` rotted after DHCP move | `ipconfig getifaddr en0` ≠ the literal in `.env` / `app.json` | DHCP reservation. Note Path B bakes URLs at build time — an IP change needs rebuild + repack + reinstall |
| iOS Local Network permission denied | Path B only. Path A rendering the page at all already proves the grant. Mac-side: zero SYNs but `ping <phone ip>` from the Mac succeeds | Settings → Even Realities → Local Network on; put the raw IP first in `.env` for bring-up |
| `.local` resolves to IPv6, engine binds IPv4 only | `lsof -nP -iTCP:8787 -sTCP:LISTEN` shows IPv4 only; `nc -4 -vz 192.168.12.200 8787` OK while `nc -6 -vz <v6 addr> 8787` refused | Launch with `--host ::` |
| Off-LAN and Tailscale down on the phone | `tailscale status` — is `iphone182` online? A `100.x` peer in `plugin connected` means the tailnet path is healthy | Keep Tailscale connected. Transport switch needs no re-pair |
| Two Macs, one stored token | Run the engine on both: paired Mac logs `plugin connected`, the other logs `rejected unpaired` from the same phone IP at the same instant | Key localStorage per host and look up the token for the URL being dialed |
| AP / client isolation (guest, hotel, corporate Wi-Fi) | Zero SYNs **and** `ping <phone LAN ip>` from the Mac fails while both are on the same SSID. Works on cellular | Use the tailnet candidates (DERP traverses isolation), or a non-isolating SSID |

#### It was working, then died (backgrounded, locked, woke from sleep)

| Cause | Distinguisher | Fix |
|---|---|---|
| Server keepalive reaps the suspended WebView (`ping_interval=20`/`ping_timeout=20` defaults) | `plugin disconnected` lands ~40 s after the lock, no error, no `rejected` line; reconnects only on unlock | Pass `ping_interval=None` (or ≥120) to `serve()`; pair with the dial watchdog |
| `closed = true` and nothing ever clears it (`pagehide`/`beforeunload` → `teardown()`; FOREGROUND_ENTER only repaints) | **The one mode with no retry traffic at all** — `plugin disconnected` then total silence, zero new SYNs on tcpdump | Handle `pagehide` with persisted-state awareness; call `link.connect()` and reset `attempt` on FOREGROUND_ENTER |
| Slow recovery, not death | See *rotation + backoff stall* above | — |
| Oversize audio frame → 1009 message-too-big | `plugin disconnected` within a second of foregrounding, preceded by a large audio seq gap; `--verbose` shows the close on the receive side | Cap the splice in `AudioStreamer`; set `max_size` explicitly on `serve()` |

#### Link says connected but behaves badly

| Cause | Distinguisher | Fix |
|---|---|---|
| **Stale audio backlog replayed on reconnect** (200-message queue = 40 s of audio) | Post-reconnect burst of transcripts with **no** `audio gap: seq N → M` line — the absence despite an obvious outage is the tell | Don't queue audio at all; queue only control messages, or discard frames older than ~2 s at flush |
| Two clients flapping (newest-connection-wins vs unconditional reconnect) | Log alternates `replacing existing plugin connection` / `connected` / `disconnected` several times a second; `lsof -nP -iTCP:8787` shows two peers | Close the loser with code 4001 and stop retrying on it |
| Backpressure filling the WebView send buffer (no `bufferedAmount` check) | `netstat -an` shows a persistently non-zero Recv-Q on 8787 while transcripts stop and **no** `plugin disconnected` ever logs | Drop audio frames when `bufferedAmount` > ~256 KB; keep control messages unconditional |
| One message kind silently stops rendering (async handler rejection swallowed) | Link healthy, engine still logging card deliveries, HUD never changes. Add `window.addEventListener('unhandledrejection', …)` — the kind names itself | try/catch the `onMessage` body into `#p-status` + HUD; global `unhandledrejection` backstop |

#### Taps do nothing (list renders, no row highlights)

| Cause | Distinguisher | Fix |
|---|---|---|
| `isEventCapture: 1` left on the `main` text container instead of the list | Nothing errors and the list draws — that rules out every layout rejection. Log which `EvenHubEvent` branch arrives: a `textEvent` instead of a `listEvent` confirms it | In `listLayout()`, capture on the list container and `0` on `main` |

#### Mic still streaming after a reload (privacy — treat as a bug, not cosmetic)

| Cause | Distinguisher | Fix |
|---|---|---|
| `beforeunload` teardown registered on the last line of `boot()`, so any earlier throw or hang leaves no shutdown hook; no HMR dispose | `plugin connected` / `plugin disconnected` pair with audio frames continuing across the boundary, and `p-mic` never returns to `off` | Register teardown right after `client.init()`; add `import.meta.hot?.dispose(() => { void teardown(); })` |

#### Silent today, arms a failure later

- **Pairing never confirmed.** Log shows `pairing: token offered to <addr>` and
  never `pairing: confirmed …`, while `pairing.json` does not exist. The link
  stays `connecting`, queued uplinks do not flush, and the local pairing marker
  expires after 120 seconds. Re-open **Pair / Replace Glasses** and retry; do
  not widen the window or persist before the phone's ack.
- **HudPageBuilder invariants are documented but unenforced** and the SDK validates only z-order/menu/brightness. A layout edit will land silently and surface later as a blank HUD via the masked `invalid` path.

### Known unknowns (test on device, in this order)

- **Plain `ws://` from a WebView page to the LAN Mac** is undocumented for
  packed builds (no ATS discussion anywhere official). Path A demonstrably
  supports `http://` dev pages, so if the socket opens there but not in the
  Path B build, this is the cause — fixes are wss via Tailscale cert or
  keeping dev mode. Whitelist entries are hostname-exact either way.
- Whether the dev-mode QR flow enforces `app.json` permissions/whitelist at
  all, and how permission prompts surface to the wearer.
- Whether the phone resolves `.local` mDNS from inside the WebView; the
  Tailscale candidates need the Tailscale app installed on the phone.
- SDK 0.0.14 surface is now fully wired (2026-08-31): contextual menus
  (`HudPageBuilder.systemMenu` on create + every rebuild, clicks via
  `EventRouter.handleMenu`), `textColor` brightness on every layout and
  per-update, unique `zOrderIndex` on every page, four-tile image frames via
  `updateImageRawData` (PNG-encoded Gray8, host converts to Gray4), audio
  `speakerRole` batch attribution in `AudioStreamer`, and long-press
  (eventType 9/10) as the local mic mute/unmute in `EventRouter`. The SDK's
  `setBackgroundState`/`onBackgroundRestore` helpers documented by the Even
  Hub background-state guide do **not exist in the installed 0.0.14 bundle**
  (verified in `dist/index.js`) — durable state rides `setLocalStorage`
  (pairing tokens, preferred brain, resume policy) instead, and everything
  else is deliberately Mac-owned and re-established by the reconnect
  handshake, so a killed Android WebView cold-restarts into the same state.

## Host push formats (for simulator/tests)

```javascript
{ type:'listen_even_app_data', method:'evenHubEvent',
  data:{ type:'audioEvent', jsonData:{ source:'glasses', audioPcm:[/* ints */] } } }
{ type:'listen_even_app_data', method:'deviceStatusChanged',
  data:{ sn:'ABC123', connectType:'connected', isWearing:true, batteryLevel:80 } }
{ method:'evenAppLaunchSource', data:{ launchSource:'appMenu' } }
```

## What this changes in the roadmap

Phase 0 was "run `men-g2-ble-gateway` on the Mac." Keep that as the **desk** path. The Hub plugin is now the fastest end-to-end proof — glasses mic → PCM → Mac STT → MLX → `textContainerUpgrade` on the HUD, entirely through supported APIs.
