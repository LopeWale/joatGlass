# Even Hub Plugin — `@evenrealities/even_hub_sdk` (v0.0.14)

The Hub plugin is our **officially supported path** to the glasses: a TypeScript web app running in the Even companion app's WebView that owns the G2 and streams audio, IMU, and input events to the Mac brain over WebSocket. No BLE reverse engineering, no sideloading, no firmware risk.

```
apps/hub-plugin (this) ⇄ EvenAppBridge ⇄ Even App (host) ⇄ G2 glasses
        │ WebSocket
     Mac brain (jarvis)
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

- **The Mac's WebSocket URL must be in `app.json`'s `network` permission `whitelist`** (there is no `websocketDomains` key — that was a pre-verification guess). The Hub silently blocks anything unlisted: the socket simply never opens, indistinguishable from an offline Mac. Keep `VITE_MAC_WS_URL` and the whitelist in sync, scheme included — the Phase-0 gateway is plain `ws://`, and a `wss://`-only whitelist fails the same silent way. Stable hostname over churning DHCP IP (Tailscale MagicDNS or the Mac's `.local` mDNS name).
- `navigator.geolocation` only works in a **published** Hub plugin, not via QR sideload — use `bridge.getAppLocation()` instead, which works in both.
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

### Path B — packed private build (survives without the dev server)

```
cd apps/hub-plugin && npm run pack     # -> joat.ehpk, --sdk-ver pinned 0.0.14
```

Upload at hub.evenrealities.com → project → **Private builds**; install on
the phone via Even Hub tab → Me → Apps → Private builds. Account-bound, no
store review. Iteration is rebuild → re-upload → reinstall, so do Path A
first.

### First-session checklist (maps to the Phase 0/1 metrics)

1. Page renders: status bar + main text on the real HUD.
2. Say "JOAT, what time is it" — transcript back on the HUD; note the
   wall-clock latency vs the ~2.2 s desk-mic number.
3. Right-temple click and R1 ring answer a `confirm.request`.
4. Send yourself an iMessage — card appears on the HUD (< 5 s target).
5. Left-temple double-tap — settings menu opens on the glasses.
6. Lock the phone 5 minutes, unlock — page state intact (the store QA gate).

### Pairing (transport auth — RULES.md #5, implemented 2026-08-26)

The gateway no longer trusts the LAN. Trust-on-first-use, in three steps:

1. The first remote client to connect is **offered** a token (`pairing.set`
   downlink). That client is your phone at QR-scan time.
2. The plugin persists it to localStorage and **acks** (`pairing` uplink) —
   only on a successful write.
3. The Mac persists on the ack, and from then on every remote dial must
   carry `?token=` or is rejected 401 before the handshake completes.

The offer/ack split exists because persisting at offer time strands the
wearer: a Wi-Fi blip during the pairing frame would leave the Mac holding a
token no phone has, 401-ing the real phone forever. Unacked offers keep the
window open, so a failed pairing costs a retry, not a manual reset.

Loopback is exempt — the simulator and `speak_test.py` run on the Mac
itself, and a local process could read the token file anyway.

- Token store: `~/Library/Application Support/JOAT/pairing.json` (0600).
- **Re-pair a new/reset phone**: delete that file, reconnect. Symptom that
  you need to: the plugin's socket never opens on a phone that used to work
  (or never paired), while the simulator connects fine.
- **Dev server ↔ packed build**: localStorage is origin-scoped, so Path A and
  Path B hold *separate* tokens. Switching between them hits the same 401
  symptom — and looks exactly like the undocumented `ws://` risk below.
  Delete `pairing.json` and let the build you are using re-pair; the other
  build's stored token is then stale. Pick one path per bring-up session.
- The plugin **ignores a second `pairing.set`** while it already holds a
  token. Without that guard, any LAN peer answering the plain-`ws` mDNS name
  in the whitelist could overwrite the good token and lock the wearer out of
  their own brain until localStorage was wiped by hand.
- Honest limits: the token rides the URL over plain `ws://`, so an *active*
  LAN sniffer can capture it, and TOFU means a hostile first-connect during
  the pairing window wins. `--verbose` request-line logging is redacted, but
  the wire is not. This closes "anyone on the LAN owns the HUD and the
  confirm gate"; the strong path is still the tailnet (`wss` via Tailscale)
  once TLS lands.

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
- SDK upgraded to 0.0.14 (2026-08-26; typecheck + 53 tests green). New
  surface not yet wired: contextual menus, `textColor`, `zOrderIndex`,
  Gray8/Gray4 raw images via `updateImageRawData`, audio
  `direction`/`speakerRole` (speaker attribution — useful for wearer-only
  filtering). Long-press (eventType 9/10) needs no wiring: EventRouter's fall-through already relays unmatched sysEvents to the Mac as `input`.

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
