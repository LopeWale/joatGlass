# Testing — glasses, no ring

Three tiers, cheapest first. Each catches a different class of bug, and **none of them substitutes for the others.**

| Tier | Needs | Catches | Cannot catch |
|---|---|---|---|
| 1. Vitest | nothing | Logic: event routing by source, confirm resolution, settings persistence | Whether the G2 emits the events we assume |
| 2. Simulator | simulator binary | Boot, HUD rendering, list navigation, container validity | **Which input source fired** — the API has no source field |
| 3. Real glasses | G2 + phone | Everything real: event shapes, latency, wearing detection | — |

## Tier 1 — Vitest (start here)

```bash
cd apps/hub-plugin && npm test
```

Covers the confirm paths without hardware, because `confirmByTap()` is just a method — no ring required to call it. `EventRouter.test.ts` asserts the source routing: left temple → settings menu, right temple → confirm, ring → confirm, and that a left-temple *single* click does not confirm.

> If you're on Linux/CI and hit `Cannot find native binding`, the `node_modules` was installed on a different platform. Remove `node_modules` and `package-lock.json`, then `npm i`.

## Tier 2 — Simulator (no glasses)

A plain browser will not work: `waitForEvenAppBridge()` never resolves without a host, so `boot()` hangs forever. You need the simulator, which injects the bridge.

Install (one-time; `~/.local/bin` because this machine's npm global prefix is root-owned):

```bash
npm install -g --prefix="$HOME/.local" @evenrealities/evenhub-simulator @evenrealities/evenhub-cli
```

```bash
npm run dev                                            # serve the plugin
evenhub-simulator http://localhost:5190 --automation-port 9898
sleep 4                                                # SDK init + startup page take time
curl -s http://127.0.0.1:9898/api/ping                 # → pong
```

Drive it and observe:

```bash
# Console output — start WITHOUT since_id, then track last_id (a negative value errors)
curl -s http://127.0.0.1:9898/api/console | jq '.entries[-5:]'

# Send touchpad input
curl -s -XPOST http://127.0.0.1:9898/api/input \
  -H 'Content-Type: application/json' -d '{"action":"click"}'

# Screenshot the HUD (576x288 RGBA PNG)
curl -s http://127.0.0.1:9898/api/screenshot/glasses -o /tmp/hud.png
```

Actions available: `up`, `down`, `click`, `double_click`, and (simulator ≥0.9.1) `long_press` / `long_press_release`. Input is **silently ignored** if no event-capture container is active — so a dead click is a real signal that `createStartUpPageContainer` failed or `isEventCapture` is wrong. `DELETE /api/console` clears the console buffer between test phases.

**Reading screenshots:** keep them RGBA. Background is `(0,255,0,α=0)`, lit pixels are `(0,255,0,α=255)` — converting to RGB makes both pure green and indistinguishable. Test lit-ness with `px[3] > 0`.

### The simulator's hard limit

`POST /api/input` takes an **action only — no source field**, and (verified empirically,
v0.9.3) every injected event arrives as `eventSource: 1` — **always the right temple**. So
the simulator cannot exercise the left-temple settings menu or the ring at all, and cannot
prove source discrimination. It *can* exercise the known right-temple Click acceptance
path once a real gateway has made a confirmation visible. Source discrimination remains
Tier 1 (vitest) and Tier 3 (hardware).

### Safe real-gateway reply trace

This drives the production reply controller and a real `HubGatewayTransport`
through the ordinary simulator URL. The last Messages.app boundary is replaced
with a recording sender, so the evidence run cannot contact a real recipient.
Its confirmation is deliberately labeled `SIMULATOR TEST (NO SEND)` and uses
no real-looking person or message, so the extra test window cannot be mistaken
for the ordinary live JOAT simulator:

```bash
# terminal 1
cd apps/hub-plugin
VITE_MAC_WS_URL=ws://127.0.0.1:18790/glasses npm run dev -- --host 127.0.0.1 --port 5192

# terminal 2 — start this before the simulator
PYTHONPATH=apps/mac .venv/bin/python -u scripts/simulator_reply_trace.py --port 18790

# terminal 3
evenhub-simulator http://127.0.0.1:5192 --automation-port 9900
curl -sS -X POST http://127.0.0.1:9900/api/input \
  -H 'Content-Type: application/json' -d '{"action":"click"}'
```

Expected trace stages are `selected`, `confirmation-requested`,
`confirmation-received`, `confirmation-visible`, `confirmation-result`,
`confirmation-settled`, `recording-sender-reached`, and `sent`. Logs include
only action IDs, character counts, and a recipient hash—not message text or the
recipient handle.

### Simulator findings (verified against 0.9.3 — expect these, they are not bugs)

- **No IMU.** The host bridge implements only `getUserInfo`, `getGlassesInfo`,
  `setLocalStorage`, `getLocalStorage`, `createStartUpPageContainer`,
  `rebuildPageContainer`, `updateImageRawData`, `textContainerUpgrade`,
  `shutDownPageContainer`, `audioControl` — no `imuControl` variant. The plugin logs
  `[ImuMonitor] imuControl unavailable on this host` and boots on regardless (mic still
  works). Nod paths are Tier 1/3 only.
- **Hot reload boots twice per host session.** `createStartUpPageContainer` is one-shot
  per simulator session, so a vite reload's second boot gets `invalid`; `BridgeClient`
  falls back to `rebuildPageContainer` and adopts the existing page. If you see
  `boot failed: startup page failed: invalid`, you are on a plugin bundle older than that
  fallback.
- **Double Click blanks the canvas — it is the system-exit gesture.** The simulator's
  Double Click button (and `POST /api/input {"action":"double_click"}`) injects a
  right-temple double-press, which is the host's app-exit path: the page is cleared and
  the canvas goes blank with a clean console. (Hardware shows an exit dialog; the
  simulator just clears.) Verified deterministic: greeting on screen → one double_click
  → blank. Use Click when poking around; recover with a vite reload. This masqueraded
  as two earlier false diagnoses (full disk, orphaned callbacks) — check the console
  tail for `eventType:3, eventSource:1` before believing anything else.
- **Occasional wedged boot after rapid reloads.** `[TAURI] Couldn't find callback id`
  warnings mean a reload orphaned an in-flight bridge call. Self-heals on the next
  reload; restart the simulator if it persists.

## Tier 3 — Real glasses

**The event-shape question is now answered — no probe needed.** A single press on a text container arrives as `sysEvent`, not `textEvent` (only scrolls arrive as `textEvent`), so `eventSource` is available and left/right/ring routing works as designed. The earlier `textEvent` CLICK fallback was dead code and has been deleted.

What still needs hardware confirmation is narrower: that the **right** temple reports `eventSource: 1` and the **left** reports `3`, rather than the reverse. If they're swapped, settings and confirm swap sides — annoying, one-line fix, but you want to know before muscle memory sets in.

Run the probe to check:

```bash
python3 scripts/imu_capture.py --out /tmp/probe.csv --label probe
```

It accepts the plugin's WebSocket and logs every uplink message, including `input`. Then, wearing the glasses:

1. Press the **right** temple once → expect `input` with `eventSource: 1`. Confirms.
2. Press the **left** temple once → expect `eventSource: 3`. Relays, must **not** confirm.
3. Double-press the **left** temple → settings menu opens, nothing relays.
4. Double-press the **right** temple → system exit dialog appears. Cancel it; the app must still be listening afterwards.

If 1 and 2 are swapped, flip the two `EventSourceType` constants in `EventRouter.handleSys`.

## Nod calibration

The `0.35` threshold is a guess. Calibrate it:

```bash
python3 scripts/imu_capture.py --out traces/nod.csv   --label nod    # nod repeatedly
python3 scripts/imu_capture.py --out traces/walk.csv  --label walk   # walk around
python3 scripts/imu_capture.py --out traces/still.csv --label still  # sit still
python3 scripts/imu_analyze.py traces/*.csv
```

`imu_capture.py` turns on trace mode (`imu.trace` downlink), writes CSV, and flushes per sample so Ctrl-C mid-gesture keeps the data. `imu_analyze.py` replays each trace through the *same* sliding-window swing calculation `ImuMonitor` uses and reports the distribution per gesture.

**Capture `walk`, not just `still`.** The threshold has to clear the loudest *non-nod* gesture, and that's walking. Calibrating against a still baseline gives you a confirmer that fires while you cross the street.

The analyzer exits `2` and refuses to suggest a number when the nod and non-nod distributions overlap — that verdict means the nod is not a usable confirmer with this metric, and you should rely on the temple tap rather than ship a gate that fires on footsteps. A better feature (zero-crossing count, or swing-and-return rather than raw peak-to-peak) would be the next thing to try.

## Order of operations

1. `npm test` — green before touching hardware.
2. Simulator — boot, HUD renders, clicks land.
3. Glasses — dump events, fix the routing, delete the dead branch.
4. Calibrate the nod, or formally decide the nod isn't trustworthy and document that.
