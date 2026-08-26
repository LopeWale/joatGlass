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
3. **If `rejected unpaired`:** `rm ~/Library/Application\ Support/JOAT/pairing.json`, then reconnect. Most common real-world failure — fixed in one command.
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
| **Mac holds a pairing token, plugin doesn't** (WebView data cleared, reinstall, or Path A ↔ Path B origins hold separate tokens) | `rejected unpaired connection from ('<phone ip>', …)` once per backoff tick, **and** `ls -l ~/Library/Application\ Support/JOAT/pairing.json` exists | `rm ~/Library/Application\ Support/JOAT/pairing.json`, reconnect. Pick one delivery path per session; key the stored token by host |
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

- **Pairing never confirmed.** Log shows `pairing: token offered to <addr>` on *every* connect and never `pairing: confirmed …`, while `pairing.json` does not exist. Everything works, the TOFU window stays open to any LAN peer, and the day another peer acks first the real phone is 401'd forever. Fix: re-ack when the offered token equals the stored one; only ignore a *different* token.
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
