# Even Hub Plugin — `@evenrealities/even_hub_sdk` (v0.0.12)

The Hub plugin is our **officially supported path** to the glasses: a TypeScript web app running in the Even companion app's WebView that owns the G2 and streams audio, IMU, and input events to the Mac brain over WebSocket. No BLE reverse engineering, no sideloading, no firmware risk.

```
apps/hub-plugin (this) ⇄ EvenAppBridge ⇄ Even App (host) ⇄ G2 glasses
        │ WebSocket
     Mac brain (jarvis)
```

## Why this changes the architecture

**The SDK hands us decoded PCM.** `bridge.audioControl(true, AudioInputSource.Glasses)` delivers the G2 four-mic array as `audioEvent.audioPcm` — **PCM 16 kHz, signed 16-bit LE, mono**. In relay mode there is no LC3 decoding, no `0xF1`/NUS frame parsing, no `[0x0E, 0x01]` mic command. `liblc3` is needed **only** for the Phase-5 direct-BLE path. Phase 0 gets substantially cheaper.

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
- `updateImageRawData` calls must be **serial** (await each). The SDK LZ4-compresses internally since 0.0.12; pass raw 4-bit greyscale pixels as before.
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

We use **left**-temple double-press for the settings menu and **right**-temple double-press for the exit dialog.

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

## Deployment constraints

- **WebSocket domains must be listed in `app.json` — exact match, no wildcards.** The Hub silently blocks anything else. The Mac endpoint needs a stable hostname (Tailscale MagicDNS or a static LAN name), not an IP that churns.
- `navigator.geolocation` only works in a **published** Hub plugin, not via QR sideload — use `bridge.getAppLocation()` instead, which works in both.
- The Even Hub app suspends when the phone locks or backgrounds → Guided Access + Auto-Lock Never + plugged in, or a dedicated spare iPhone. Phase 5 (custom companion) is the real fix.
- Dev loop: `even-hub` CLI (`init`, `qr`, `pack`) + the desktop simulator, which accepts `window.postMessage` pushes for audio/IMU/device events — so `AudioStreamer` and `ImuMonitor` are testable without hardware.

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
