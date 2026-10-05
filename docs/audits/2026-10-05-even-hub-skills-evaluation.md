# Even Hub skills evaluation of the JOAT plugin — 2026-10-05

Evaluated against the installed `@evenrealities/even_hub_sdk@0.0.14` type
definitions and README, and the official `everything-evenhub` skills
(`handle-input`, `glasses-ui`, `design-guidelines`, `font-measurement`,
`sdk-reference`, `device-features`, `template`) cloned from
`github.com/even-realities/everything-evenhub`. Source: branch
`feat/vibrant-faraday-zmz1do` at `f74641b`, identical to `origin/main`.
Nothing was changed; this is findings only.

Paths are relative to the repo root. `src/` means `apps/hub-plugin/src/`,
`joat/` means `apps/mac/joat/`.

## 0. Read this first

1. **The package on the glasses is 0.1.88.** `Changelog.md:65` ("The uploaded
   0.1.88 package is unchanged") and `Changelog.md:7` (0.1.90 "has not been
   submitted to Even Hub, installed in Even App, or verified on physical
   glasses"). Everything in 0.1.89 and 0.1.90, including the native-text
   assistant conversation and the explicit-Mac reconnect fix, is not on your
   phone. Several of the symptoms below are already fixed in source and only
   need a package upload.
2. **The plugin test suite is red at HEAD:** 151 failed / 910 passed across 14
   files (`npx vitest run`). Root cause is one missing method on the test
   doubles: `TypeError: this.hud.usesCustomListSelection is not a function`
   (`src/CapabilityMenu.ts:1246`, method added in `src/HudPresenter.ts:462`).
   The Changelog admits a "25 baseline failures" set; it is now 151. The
   suite cannot currently prove anything about Messages, menus, or events.
3. **The Mac unit tests could not be run in this container** (Python 3.11
   here, project requires 3.12). The wire-protocol pin
   `tests/unit/test_protocol.py` is therefore unverified in this session.

## 1. Unwired components

### 1a. Protocol frames with one side missing

| Frame | Defined | Reality | Evidence |
|---|---|---|---|
| `imu.control` downlink | `joat/core/protocol.py` `ImuControl` | Mac never constructs it. Plugin handler at `src/main.ts:879` is dead. | `grep -rn "ImuControl(" joat` finds only protocol.py |
| `imu.trace` downlink + `imu` uplink | `ImuTrace`, `ImuMonitor.setTrace` | Never sent; `on_imu` in `hub_gateway.py:397` has no subscriber. **`docs/NEXT_STEPS.md:97-107` says IMU calibration is "already wired". It is not.** | same grep |
| `policy.set` downlink | `PolicySet` | Never sent. `src/main.ts:949` dead. | |
| `shutdown` downlink | `Shutdown` | Never sent. `src/main.ts:954` dead. | |
| `assistant.conversation` `dismiss` | `HudWriter.dismiss_conversation` `joat/services/hud.py:174` | No caller anywhere. The plugin's dismiss branch (`src/main.ts:819`) never runs. | |
| `audio.control` `open=false` / `source` | `AudioControl` | Mac only ever sends `open=True` once on connect (`joat/services/orchestrator.py:423`). It never closes the mic and never picks a source. | |
| `productivity.view.ack` uplink | `on_productivity_view_ack` `hub_gateway.py:445` | Parsed, no subscriber. | |
| `confirm` uplink callback | `on_confirm` `hub_gateway.py:401` | No subscriber (the pending future is still resolved, so confirmation works; the callback is dead). | |
| `brainstorm.action` `show_brainstorm` | handled `joat/services/session.py:1238, 2029` | Plugin never emits it (`src/BrainstormController.ts:85-167`). | |
| `hud.rebuild` layouts `list` / `image` | `types.ts:502` | Only `reason="brainstorm"` is ever sent (`hud.py:100`). | |
| `g2.diagnostic.control`, `location.probe.control` | | Only behind `--g2-diagnostics` / `--location-probe`; JOATShell never passes `--location-probe`. | `hub_gateway.py:345, 1138, 1141` |

### 1b. Resume policy "mac" does nothing

`src/DeviceMonitor.ts:88-105` waits for the Mac to send `audio.control` when
the glasses go back on. The Mac's `on_device` (`joat/__main__.py:1671-1683`)
never sends it. Wearers who pick "mac" get a mic that never resumes.

### 1c. Capability rows that land on nothing useful

| Row | Where it lands |
|---|---|
| Navigation | `_show_navigation` (`joat/services/capabilities.py:2478`) paints a static two-line instruction page. Routing needs `MAPBOX_ACCESS_TOKEN` (`joat/__main__.py:424`); without it: "Mapbox is not configured on this Mac". |
| Agent Terminal | With no lanes configured: fallback text only (`capabilities.py:852-875`). The `terminal_focus` constructor argument is never passed by `__main__.py`. |
| Settings | **No Mac handler at all.** Plugin paints two fixed non-selectable lines (`src/CapabilityMenu.ts:601-615`). |
| Search panel tab | Pushed but always empty until a voice "look up" (`joat/services/search.py:136-138`). No glasses-side entry point. |

### 1d. Device features the SDK offers that the plugin never touches

- `captureImageFromCamera`, `pickImageFromAlbum`: never called; `camera` /
  `album` not in `app.json`. `docs/HUB_PLUGIN.md:17` advertises a "vision path
  → Qwen3-VL on the Mac"; there is no Qwen3-VL code in `joat/`.
- `audioEvent.direction` (per-frame speaker bearing): never read. Only
  `speakerRole` is forwarded (`src/EventRouter.ts:208`).
- `callEvenApp`: never used (fine).
- Everything else the `device-features` skill lists IS wired: glasses and
  phone mic, IMU, one-shot and continuous location, device info, user info,
  host localStorage, launch source, LONG_PRESS, `menuItemClickEvent`,
  `textColor`.

### 1e. Dead code and docs describing code that does not exist

- `joat/display/` and `joat/computeruse/` are empty `__init__.py` packages;
  `docs/ARCHITECTURE.md:21` and `docs/MAC_APP.md:212-255` describe
  `HudRenderer`, `CuaDriver`, `IphoneMirrorDriver` as if present.
- `joat/agents/openclaw.py` `OpenClawAgent` is reachable only from a test.
- Plugin exports with no caller: `restoreTextHudPage`
  (`src/HudPageBootstrap.ts:103`), `projectNavigationMap`
  (`src/HudCanvasRenderer.ts:1733`), `CapabilityName` (`types.ts:647`).
- `docs/HUB_PLUGIN.md` is stale in three places that matter for this
  evaluation: `NATIVE_LIST_TITLES` / `glassPage` / NEWS and SEARCH as native
  lists (`:138, :181-182, :230-236`) no longer exist; the native `SpineLabel`
  (`:178`) was removed (`types.ts:39-43`); `:50` says raw Gray4 pixels while
  `:529` and the code send PNG.
- Root `.env.example` says "no code reads these yet" but
  `MAPBOX_ACCESS_TOKEN` is read at `joat/__main__.py:424`. `JINA_API_KEY` and
  `FIRECRAWL_API_KEY` are never read.

## 2. The image UI that slows rendering

Ground truth from the `glasses-ui` skill: image frames cost 0.5–2 s each over
BLE, no delta encoding; text updates are "much faster"; serialize all bridge
calls. Your own live Mac logs measured 0.2–1.4 s per tile
(`Changelog.md:14`). So speed is the number of 288x144 tiles an interaction
dirties.

### 2a. What is still raster in current source (and all of it in 0.1.88)

| Surface | Tiles per change | Native path available? | Evidence |
|---|---|---|---|
| Overview dashboard | 2 per tab swipe, ~2 per `hud.text` card | partly: text containers (`dashboard()` layout already exists in `HudPageBuilder`) | `src/HudPresenter.ts:1692-1723, 2222-2228` |
| Every `CapabilityMenu` list (Messages, QuickNotes, Planning, News, Search, readers) | 2–4 per selection move, 4 on open | **Yes.** `ListContainerProperty` in `HudPageBuilder.listLayout` (`src/HudPageBuilder.ts:499-541`) is used only for the Agent Terminal picker (`src/CapabilityMenu.ts:811, 1890`). Firmware scrolls at zero BLE. | `src/HudPresenter.ts:1531-1578, 1644-1657` |
| Confirmation prompt ("Send to X?") | **4 forced** on show, 4 on dismiss | **Yes.** `confirmationLayout` exists (`src/HudPageBuilder.ts:546-562`) but only runs when there is no visual surface (`src/HudPresenter.ts:1921`, `if (this.visual)`). | `src/HudVisualSurface.ts:635` |
| Assistant answer / error overlay (`assistant.overlay`) | **4 show + 4 dismiss**, 4 per streamed append (300 ms throttle). The card is centred on x=288 and y≈146, straddling both seams. | **Yes.** `paintNativeAssistantOverlay` (`src/HudPresenter.ts:929-948`) exists but only runs without a visual surface. | `src/HudCanvasRenderer.ts:154-191, 1979-2050` |
| Prompt question ("What should I say?") | 4 (`paintExact`) | no native path | `src/HudCanvasRenderer.ts:1951-1977` |
| Receipt overlay | 2 + 2 | | `src/HudCanvasRenderer.ts:830-857` |
| Brainstorm transcript + "Thinking…" counter | 1–2 tiles **every second** | | `src/HudPresenter.ts:1245-1270` |
| Status bar | 1 tile per mic/battery change; clock 1 tile/min | native text container was removed | `src/HudCanvasRenderer.ts:1928-1944` |
| Assistant conversation | **0 in 0.1.90 source** (native text, `rebuildPageContainer` on entry then `textContainerUpgrade` only). **4 per revision in 0.1.88.** | | `src/HudPresenter.ts:321-328`; `src/AssistantConversationSurface.ts:617-734` |

### 2b. Pipeline costs that multiply the above

- **Every page switch forgets the delivered-tile cache and the PNG cache**
  (`src/HudVisualSurface.ts:423-437`, called from `withPageTransition` at
  `:476`). Leaving the assistant conversation therefore costs one rebuild plus
  a full four-tile re-encode and re-send (`src/HudPresenter.ts:366-394`).
- **Entering the conversation waits for the image lane to drain**, up to
  `TILE_UPLOAD_TIMEOUT_MS = 15_000` (`src/HudVisualSurface.ts:275, 459-487`).
  The mic-on tile (top-right) is typically in flight at the start of a turn
  (`src/main.ts:506`).
- **Downlink handlers are strictly serial and await full tile uploads**
  (`src/MacLink.ts:351-379`; `src/main.ts:758, 794-798`). A queued "thinking"
  frame waits behind notification or panel tiles.
- **PNG, not raw pixels.** `encodeGray8TilePng` copies Gray8 to RGBA,
  `canvas.toBlob('image/png')`, then `Array.from` into a JSON number array
  (`src/HudVisualSurface.ts:358-384, 688`). The SDK README's example is
  `imageData: [/* grayscale bytes */]`; LZ4 happens on the host. Raw bytes
  would skip an encode and the 3.6x JSON inflation, but nobody has verified
  the host accepts them; this is the single unmeasured lever with the
  largest possible payoff.
- **256-level anti-aliased text, no quantisation** (`src/HudCanvasRenderer.ts:1841, 1896-1900`).
  The `glasses-ui` skill and your own `HUB_PLUGIN.md:237` both note fewer
  grey levels compress better; untried.
- **Whole-frame redraw on every render** (`src/HudCanvasRenderer.ts:1825-1902`),
  including Chrome's 30 s clock tick (two renders per paint,
  `src/Chrome.ts:52, 138-140`), the 12 s state-word revert (`:82-85`),
  un-deduplicated battery events (`src/DeviceMonitor.ts:53`) and 1 Hz
  Brainstorm ticks. CPU only when no pixels change, but it occupies the one
  paint lane that page transitions wait on.

### 2c. The "hearing you…" acknowledgement is invisible

The Mac sends `JOAT · hearing you…` FIRST, precisely because "an unchanged
HUD for that long reads as the assistant is dead"
(`joat/services/orchestrator.py:668-672`). The plugin routes it to
`Chrome.setMacState` (`src/main.ts:787`), which repaints, but the renderer
returns an empty spine label by design (`hudSpineLabel` returns `''`,
`src/HudCanvasRenderer.ts:1622-1627`) and the native `SpineLabel` twin was
removed (`types.ts:39-43`). So during the whole STT window the lenses show
nothing new, then the card appears. This is the biggest perceived-latency
gap and it costs a full render with zero tiles changed each time.

## 3. Custom logic that duplicates what Even already provides

Ordered by how much it costs you today.

1. **Lists.** Plugin-owned cursor, plugin-drawn rows, repaint per swipe,
   `fitText` trimming one character per `measureText` call
   (`src/HudCanvasRenderer.ts:4303-4316`), rows re-wrapped every frame
   (`:2933-2945`). Even gives `ListContainerProperty` with firmware scroll and
   selection highlight (`isItemSelectBorderEn`) at zero BLE per step, max 20
   items x 64 chars. Your `HUB_PLUGIN.md` records that the wearer preferred
   the firmware font "for the scroll" and that NEWS/SEARCH were moved to it;
   that move has since been reverted in code. Only the Agent Terminal picker
   uses it now.
2. **Confirmation dialog.** Raster card, four forced tiles, plus a plugin-side
   8 s timer. Even's pattern per `glasses-ui`/`handle-input`: a text
   container (fast `textContainerUpgrade`) and a tap. You already have
   `confirmationLayout`; it is gated off whenever the visual surface exists.
3. **Assistant answer card and error card** (`assistant.overlay`). Raster,
   four tiles in and four out. Native `paintNativeAssistantOverlay` exists and
   is gated off the same way.
4. **Status bar / clock / battery.** Raster text in the top tiles. Even's
   pattern is one text container updated with `textContainerUpgrade` (~83 ms
   fixed cost, your own figure at `HUB_PLUGIN.md:200-205`). The native twin
   was built and then removed.
5. **Scrolling text** (overlay pages, Brainstorm transcript, readers): whole
   image repaints per page. `glasses-ui`: pre-paginate at 400–500 chars and
   `textContainerUpgrade` per page, flicker-free.
6. **Background state** (`src/BackgroundState.ts:90-113`) is a local
   re-implementation; the SDK bundle lacks it, so this one is justified.
7. **Menu.** Correctly uses `menuObject`; the old tile sidebar is retired.
   No duplication here.
8. **Text measurement** for the native conversation uses `@evenrealities/pretext`
   correctly (`src/AssistantConversationSurface.ts:124-159`). The raster
   surfaces instead measure with browser `measureText` against system fonts
   ("Helvetica Neue"/"SF Pro"/Arial, `src/HudTypography.ts:19-26`), which is
   fine for pixels you draw yourself but is why none of it can move to native
   containers without re-measuring.

The trade you documented at `HUB_PLUGIN.md:119-135` is real: native
containers cannot be dense. But the three surfaces that define how the
assistant *feels* (acknowledgement, answer, confirmation) are exactly the
ones where speed matters more than density, and all three have a native path
already written and gated off.

## 4. Why the AI interface is not smooth like Even AI

Even AI's own surface is a native text page updated in place. Yours, as
installed (0.1.88), is four image tiles per streamed revision. In addition:

1. **Version skew.** The native conversation shipped in 0.1.90 source only.
   Upload and install it before measuring anything else.
2. **Acknowledgement never shows** (section 2c). Even AI shows "listening"
   immediately. JOAT shows nothing until the first reply chunk.
3. **Wake-word gate and VAD split.** `docs/NEXT_STEPS.md:113-117` records 639
   utterances discarded against 29 replies and a 1 s hangover splitting
   requests. Push-to-talk is listed as the top open item (`:193-196`).
   `handle-input` gives you `LONG_PRESS_EVENT (9)` / `LONG_PRESS_RELEASE_EVENT
   (10)`; the router already decodes them (`src/EventRouter.ts:153-178`).
4. **STT fallback.** `NEXT_STEPS.md:33-64`: when `whisper serve` misses its 30 s
   ready timeout, the session silently uses the slow batch engine for the rest
   of the session. Unconfirmed cold-load hypothesis; nobody has watched one.
5. **Mind latency.** `claude -p` ~3 s, `codex exec` 6–8 s per cold turn
   (`NEXT_STEPS.md:174-176`). That is before any rendering.
6. **Even in 0.1.90**, entering the conversation waits for the image lane
   (section 2b), and every revision is 1–4 serial text writes sharing one lane
   with a 450 ms indicator tick (`src/AssistantConversationSurface.ts:699-763`).
   The Mac re-sends the entire transcript per chunk with no throttle
   (`orchestrator.py:1027-1031`). *Fixed later the same day: streamed
   "answering" frames are coalesced to one per 200 ms
   (`STREAM_PUBLISH_INTERVAL_S`); the first chunk and the final frame are
   never held.*

**The Even AI door is the shortcut you already built.**
`joat/services/agent_endpoint.py` serves an OpenAI-shaped
`/v1/chat/completions` on loopback `:8788`. Pointing Even app → Settings →
Even AI → Agent Configure at
`https://<mac>.<tailnet>.ts.net/agent/v1/chat/completions` with the token in
`~/Library/Application Support/JOAT/agent-token` gives you Even's native
wake word, STT and rendering with JOAT's mind behind it, for questions and
answers. Steps are at `docs/NEXT_STEPS.md:150-170`. Only actions that need
the tap confirmation (send a message, write a calendar entry) have to stay on
the plugin path.

## 5. Device features you are not using

Per `device-features`, available and unused: phone camera and album
(`captureImageFromCamera`, `pickImageFromAlbum`, need `camera` / `album`
permissions in `app.json`), per-frame `audioEvent.direction`. Wired but
dead because the Mac never drives them: IMU pacing (`imu.control`), IMU
trace, mic close and mic-source switch (`audio.control`), resume policy
"mac", `policy.set`, `shutdown`.

One correctness note from `handle-input`: the protobuf zero-value rule
(`eventType === 0` arrives as `undefined`) is handled correctly throughout
`src/EventRouter.ts` (`?? 0` / `?? CLICK_EVENT` on every numeric read), and
`GesturePolicy.ts` routes double-click as Back on every envelope. Exit uses
`shutDownPageContainer(1)` with cleanup deferred to the exit events, as the
skill prescribes. No findings there.

## 6. Why you still cannot send a reply

Sending is a plain `osascript` call against Messages.app
(`joat/channels/message_sender.py:44-53`, `tell application "Messages" … send
messageBody to chat id chatGuid`). No native bridge, no Shortcuts, no feature
flag, no dry-run switch: `joat/__main__.py:1351-1362` always wires the real
sender. **A real Messages.app send has never been verified anywhere in the
repo**: every acceptance run used `RecordingSender` (`Changelog.md:1081, 1118,
1147`; `docs/TESTING.md:262-266, 320-324`; `test_real_g2_messages_acceptance.py`
marks send `not_proven`).

Ranked causes, with how to tell which one you are hitting:

### 6.1 The confirmation round trip blows the Mac's 12 s deadline

*Fixed later the same day: the gate is a firmware text page (one rebuild
each way when the page beneath is a list), the Mac's envelope is 20 s
(`CONFIRM_TRANSPORT_SECONDS`, `_CONFIRM_SECONDS`), and the plugin derives
its decision window from the Mac's `expiresAtMs`
(`confirmationDecisionWindowMs`). The analysis below describes 0.1.88-0.1.91.*

- Mac: `CONFIRM_TRANSPORT_SECONDS = 12.0` (`joat/services/message_reply.py:51`),
  enforced by `asyncio.wait_for(future, 12)` and `time.time() <=
  pending.expires_at_s` (`joat/ble/hub_gateway.py:858, 893-895, 1245`). The
  clock starts when `confirm.request` is sent.
- Plugin: the request sits in the serial downlink queue; the confirmation
  paints as **four forced image tiles** (0.2–2 s each); only then does the
  8 s decision window open (`src/ImuMonitor.ts:133-140`, `src/main.ts:931`);
  after the tap, `deliverAfterConfirmationDismissal` **awaits a full repaint
  of the previous list before sending `confirm`**
  (`src/ConfirmationCommit.ts:20-32`).
- Four tiles in, up to 8 s, four tiles out easily exceeds 12 s. You tap, the
  screen returns to the list, then "Reply cancelled."
- Check `~/Library/Logs/joat/brain.log` for `confirm … timed out — treating
  as declined` followed by `stage=confirmation-ignored reason=binding-mismatch`.
- The plugin also ignores the Mac's `expiresAtMs` (`types.ts:529`) and uses its
  own 8 s.

### 6.2 Messages.app Automation permission was never requested

- JOAT.app's access screen checks Full Disk Access (for `chat.db` reads) and
  Notes Automation, but **not Messages Automation**
  (`apps/mac/JOATShell/Sources/JOAT/AccessSettingsView.swift:34-47`;
  `docs/MAC_CONTROLS_ACCEPTANCE.md:36`).
- If you run `python -m joat` from Terminal, the process macOS asks about is
  Terminal, not JOAT.app, for both Full Disk Access and Automation.
- A denied Apple Event (`-1743`), Messages not signed in, or an unanswered TCC
  dialog (15 s timeout) all raise `MessageSendError` with
  `possible_external_commit=True` (`message_sender.py:128-142`), so the lens
  says "Messages may have submitted the reply … Check Messages on your Mac"
  and the state is "uncertain". Log: `stage=send-failed error=MessageSendError`.
- Fix: System Settings › Privacy & Security › Automation › JOAT.app (or
  Terminal) › Messages: on. Keep the Mac unlocked the first time so the
  dialog can appear.

### 6.3 The bare "reply" path drops silently

The hint on the conversation page says "say reply". That word is admitted
only if ALL of these hold (`joat/services/message_ui.py:116-131`): visit
current, reply fence set, selection still within its **120 s TTL**
(`message_reply.py:52`), text is exactly "reply"/"respond", **source is the
glasses mic (the phone mic is refused)**, no "other" speaker role, same
connection generation. Then the body prompt must be acknowledged by the
plugin within **4.5 s** (`joat/services/hud.py:91`), which again waits on an
image paint and the mic epoch. If the ack is late the prompt is retired and
`cancel()` clears `_action` before publishing, so **no receipt is shown**
(`message_ui.py:86-92`). Say "JOAT, reply saying …" instead to use the
legacy path with explicit error messages, and re-tap the conversation if
more than two minutes have passed.

### 6.4 Other gates worth knowing

| Gate | Where | Effect |
|---|---|---|
| Prompt text over 120 UTF-16 units | `src/HudPresenter.ts:1876`, `message_reply.py:905-923` | Goes to a review flow; you must tap on the LAST page within 90 s |
| listEvent taps swallowed while a confirmation is pending | `src/EventRouter.ts:222-236, 506-518` | Only a sysEvent tap from L/R temple or ring counts |
| Text or minimal HUD fallback at boot | `src/HudPresenter.ts:1867-1870, 628` | Message-UI confirmations and prompts need `visual.paintExact`; they fail closed |
| Any surface departure (menu, Back, panel, context menu, disconnect) | `capabilities.py:450-458, 643-696` | Clears the selection and cancels the draft |
| Whisper transcribes "Reply. Saying …" | `message_reply.py:34-37` | Grammar allows only `[,:]` after reply; falls through to the LLM path |
| Group chat with no `chat.guid` | `imessage.py:57-66` | Thread shows "read-only" |
| Reconnect mid-confirm | `hub_gateway.py:1240-1272` | Session mismatch, "Reply cancelled." |

## 7. What to do, in order

1. Upload and install the 0.1.90 package (`apps/hub-plugin/joat.ehpk`, SHA
   recorded at `Changelog.md:6`). It removes image tiles from the assistant
   conversation and fixes explicit-Mac reconnect.
2. Fix the test doubles so `npx vitest run` is green (add
   `usesCustomListSelection` to the HUD stubs). Until then no change to
   Messages or menus is provable.
3. Grant Messages Automation to the process that runs the brain, then do one
   real send with a test contact and record the receipt in `docs/TESTING.md`.
4. Lengthen or restructure the confirmation deadline: start the Mac clock at
   plugin ack rather than at send, or send `confirm` before the dismissal
   repaint for acceptances, or move the confirmation page to the native
   `confirmationLayout` so it costs ~165 ms instead of four tiles each way.
5. Make the acknowledgement visible: either restore the native spine label
   container and write "hearing you…" with `textContainerUpgrade`, or paint it
   inside one tile. Then measure `durationMs` per tile from
   `g2-diagnostics.json` on real hardware, which the docs ask for and nobody
   has recorded.
6. Un-gate the native paths for the answer overlay and confirmation when the
   visual surface is present; move Messages/QuickNotes/Planning lists to
   `ListContainerProperty`.
7. Try raw Gray8 or Gray4 `imageData` instead of PNG on one tile on hardware.
8. Either wire or delete: `imu.control`/`imu.trace`, resume policy "mac",
   `policy.set`, `shutdown`, `assistant.conversation dismiss`,
   `show_brainstorm`. Fix `NEXT_STEPS.md §5`, `HUB_PLUGIN.md` and
   `ARCHITECTURE.md` to match the code.
9. Configure the Even AI door for questions so the plugin only has to be fast
   for actions.
