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

Covers the confirmation paths without hardware. `EventRouter.test.ts` asserts
that either built-in G2 temple press confirms while the decision surface is
active, either swipe direction cancels, unknown event sources fail closed, and
every verdict gesture is consumed instead of leaking into menus or Mac input.
The optional R1 remains compatible but is not required.

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
prove source discrimination. It *can* prove that Click accepts and Up/Down refuse once
the production confirmation gate is visible. Source discrimination remains Tier 1
(Vitest) and Tier 3 (hardware).

The explicit `?joatVisual=confirmation` development surface is interactive:
it arms the production confirmation gate without connecting to the Mac or any
external sender. Click accepts; Up or Down refuses; either verdict dismisses
the fixture back to its prior safe HUD state.

Navigation has its own production-renderer visual matrix: `nav-destination`,
`nav-repair`, `nav-mode`, `nav-preview`, `nav-guidance`, `nav-uncertain`,
`nav-wrong-way`, `nav-off-route`, `nav-rerouting`, `nav-arrived`, and
`nav-error`. Capture every state through the official simulator with:

```bash
PYTHONPATH=apps/mac .venv/bin/python scripts/simulator_navigation_visual_acceptance.py \
  --base-url http://127.0.0.1:5194/ \
  --out /tmp/joat-navigation-visual-evidence
```

The summary keeps four-tile bridge acknowledgement separate from the raw
576x288 simulator framebuffer. After the bridge callback, the harness polls
until that framebuffer has remained byte-identical for at least one second;
this is necessary because a callback can precede one or more visible tile
updates. The alpha extraction is only a readable view of that framebuffer.
All three remain simulator evidence; synchronized video through the physical
lens is the separate real-G2 visibility gate.

The production-path replay tests drive timestamped synthetic walking and
cycling fixes through `NavigationController` → `NavigationProgressTracker` →
`HudPresenter` → `HudVisualSurface`. They prove uncertainty/off-route debounce,
one reroute request, cycling wrong-way recovery, stable arrival, location
release, and that four ordered tile callbacks precede every semantic state
receipt:

```bash
npm --prefix apps/hub-plugin test -- --run NavigationReplay
.venv/bin/python -m pytest -q tests/unit/test_navigation.py \
  tests/unit/test_mapbox_navigation.py tests/unit/test_navigation_policies.py
```

These are deterministic synthetic replays, not field recordings and not
threshold calibration. The simulator matrix remains a static renderer matrix;
it does not claim that Mapbox, phone GPS, or physical lens latency participated
in the run.

Live provider testing is opt-in on the Mac:

```bash
JOAT_EXPERIMENTAL_NAVIGATION=1 MAPBOX_ACCESS_TOKEN=... \
  PYTHONPATH=apps/mac .venv/bin/python -m joat
```

Candidate search sends the spoken destination and approximate proximity to
Mapbox before JOAT can show the provider's canonical place for confirmation.
Directions are requested only after canonical confirmation, explicit mode
choice, and a fresh origin. Never put the token in a package, screenshot, log,
or retained evidence file.

For a fast native-size renderer review without an Even host, add
`&joatPreview=browser`. For example,
`/?joatVisual=calendar&joatPreview=browser` draws the production presenter's
four Gray4 tiles into one exact 576×288 canvas. Available productivity states
include `sidebar`, `more`, `notifications`, `notification-detail`, `calendar`,
`calendar-detail`, `tasks`, `task-detail`, `quicknotes`, `quicknote-detail`,
`activity`, `activity-detail`, `action`, and `confirmation`. This route is
development-only and proves layout/renderer output; it does not prove BLE tile
upload, Even input delivery, or physical-glasses legibility.

Capability navigation falsification is covered at both protocol ends. The Hub
suite forces rejected Sidebar, More, Settings, and loading-focus paints and
asserts that no invisible view remains interactive. It also rejects delayed
responses with the wrong `requestId` and obeys only the Mac's explicit
`selectable` bit. Python tests complete two concurrent Messages opens in reverse
order, prove that only the newest generation renders, and reject a row tap from
the superseded snapshot. Productivity tests additionally inject a completion
read-back from a different reminder list and a local receipt-persistence
failure after native commit; both must finish `uncertain` with `ACTION · CHECK`.

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

### Complete simulator Messages flow

`scripts/simulator_messages_flow_acceptance.py` extends the narrow confirmation
trace through the actual Messages menu and conversation router. It creates a
disposable two-conversation `chat.db`, then composes the production
`HubGatewayTransport`, `CapabilityRouter`, `IMessageStore`, and
`MessageReplyController`. The final sender is still a `RecordingSender`, so the
run cannot invoke Messages.app or contact a recipient.

Point a disposable development manifest at its isolated gateway and open the
ordinary plugin with `?joatTestOpenMenu=1`:

```bash
# terminal 1
PYTHONPATH=apps/mac uv run python -u \
  scripts/simulator_messages_flow_acceptance.py --port 18791

# terminal 2 — use a disposable manifest that allowlists only this loopback origin
VITE_MAC_WS_URL=ws://127.0.0.1:18791/glasses npx vite <simulator-plugin-root> \
  --config apps/hub-plugin/vite.config.ts --host 127.0.0.1 --port 5193 --strictPort

# terminal 3
evenhub-simulator 'http://127.0.0.1:5193/?joatTestOpenMenu=1' \
  --automation-port 9901
```

The causal frame order is Sidebar → Conversations → Conversation → Confirm →
Result. The first sidebar frame uses the development-only live-menu hook because
the official simulator cannot inject a left-temple double-click. Click/list
selection and confirmation use the official simulator input endpoint and the
production EventRouter/runtime path. Home return is covered by a
production-composed Hub test but remains a physical-G2 acceptance gate, as do
left-temple ingress, optical legibility, live voice transcription, and any real
Messages.app submission.

Run the same production controller in new-message mode with
`--flow compose`. After opening Messages, select the first **New message** row;
the causal frame order is Sidebar → Conversations → Compose → Confirm → Result.
The injected voice command resolves one fixture-only **Simulator Contact**
through the controller's Mac-local contact-search seam. It still terminates at
`RecordingSender`, so this proves G2 selection, compose rendering, exact
confirmation, and result routing without reading the user's Contacts database
or sending a real message. `tests/unit/test_contacts.py` separately exercises
the real read-only Contacts SQLite adapter with a disposable schema and proves
that missing or ambiguous names never reach confirmation.

For the reply path, `--outcome` makes the terminal contract explicit without
ever invoking Messages.app: `accepted` (the default), `uncertain`, and `failed`
use the visible **TAP · Confirm** action; `cancelled` uses the visible
**SWIPE · Cancel** action (the official simulator's `down` control);
`timed_out` deliberately sends no input until the displayed eight-second
window expires. For retained
official-simulator proof, run the gateway with the simulator origin and a fresh
evidence directory:

```bash
PYTHONPATH=apps/mac uv run python -u scripts/simulator_messages_flow_acceptance.py \
  --port 18791 --flow reply --outcome cancelled \
  --automation-base http://127.0.0.1:9901 \
  --evidence-dir /absolute/path/to/messages-cancel-evidence
```

The driver clears prior simulator logs, waits for the production Hub's
content-free `confirmation-visible` receipt, saves the real confirmation frame
before injecting the documented control, then saves the resulting frame and a
privacy-minimized receipt. It checks the exact production-controller result
text and whether its `RecordingSender` was called: cancellation and timeout
must not call it, uncertain must never claim failure, and failed must never
claim submission. The fixture remains a no-send boundary, and this evidence is
not physical-G2 proof. Non-success outcomes apply to `--flow reply` only;
compose and history remain their narrow no-send fixtures.

The current official simulator API can drive the first visible **New message**
row, but its `down` action remains bound to the native sidebar after the
custom Messages list is painted. It cannot therefore autonomously select a
later conversation row or establish the physical G2 swipe delivery for a
reply-outcome run. The reply driver intentionally waits for a real
`capdata` selection instead of preselecting a fixture thread; use a physical
G2 (or a future simulator input that delivers that event) for that evidence.
The retained official-simulator compose receipt is separate proof only.

Run `--flow history` for the non-writing conversation proof. Select the
fixture **Dele** conversation (it has more than one eight-message page), scroll
down until the HUD says **Older messages — up for newer**, then scroll up until
the latest page returns. The driver records only the paging stages and fails if
the fixture `RecordingSender` is reached. This is official-simulator proof of
the production sidebar → conversation → older/newer routing and visible
labels; it remains separate from physical-G2 gestures, lens legibility, and
any real Messages.app send.

On the Mac, JOAT.app now shows **Messages → Conversation history** with
**Open Full Disk Access…** and **Check Again**. The probe only attempts to open
the database from JOAT's signed process; it does not query a table or read a
conversation. The glasses `MESSAGES · ACCESS` page puts the real retry action
first and identifies the external System Settings path rather than claiming a
tap can grant macOS permission.

### Opt-in G2 diagnostic receipt

The Mac shell’s **Record G2 trace** switch is off by default. When enabled it
restarts the engine with `--g2-diagnostics`; the gateway then enables the
plugin capture for that authenticated session. The trace contains only page
names, gesture classes, four-tile order/results, and self-heal decisions —
never HUD text, pixels, audio, URLs, tokens, device IDs, or input payloads.
Use **Export Redacted G2 Trace…** in JOAT.app to choose an archive location.

#### Packaged location capability probe (Gate 0)

Ordinary **Record G2 trace** capture does not change location accuracy. The
high-accuracy probe is a separate, explicit terminal opt-in because it consumes
more phone battery and the Even host exposes one unkeyed location stream:

```bash
PYTHONPATH=apps/mac .venv/bin/python -m joat \
  --g2-diagnostics --location-probe
```

The authenticated gateway first enables the diagnostic receipt and then sends
`location.probe.control`. The plugin's single location-session owner switches
from the existing medium/60-second/250-metre ambient profile to a high-accuracy
1-second/1-metre *measurement request*. Those probe settings are not production
walking or cycling thresholds. Disconnect, shutdown, or an explicit disable
returns the owner to ambient mode, and teardown stops the host stream.

Use an installed `.ehpk` on a physical phone and G2 for evidence; simulator or
sideload findings are not evidence about packaged host delivery. Receipt schema
`joat.g2-diagnostic.v2` first records a `location_probe_state` with the requested
profile and start outcome, then records only bounded sample metadata: sample
order, foreground or background lifecycle, field-presence bits (timestamp=1,
speed=2, heading=4), cadence, accuracy, timestamp age, displacement, and
heading/course-delta buckets. It never records probe coordinates, raw speed,
raw heading, or the host timestamp. The pre-existing coarse `location` uplink
remains a separate ambient grounding contract.

A failed `high` state is a profile-start failure. A successful `high` state with
no following samples is inconclusive host-delivery evidence rather than a pass.
Interpret missing optional fields on delivered samples as a measured host
capability result, not a parser failure. In particular, absent heading means
later wrong-way logic must derive course from successive accepted fixes. Record
foreground walking and cycling separately; background/locked delivery is a
stretch observation, not a release assumption. For later cue-latency
acceptance, film the lens beside a synchronized clock and compare fix timestamp
to pixels becoming visible—bridge acknowledgment alone is not visible-time
evidence.

For a retained physical run, quit the ordinary JOAT.app engine first so port
8787 is free, start the synchronized lens recording, and run the dedicated
harness from the repository root. It reuses the production authenticated
gateway and existing pairing, shows one fixed marker that must be tapped while
stationary, then records the requested movement interval:

```bash
mkdir -m 700 -p /absolute/private/evidence/gate0-real-g2

PYTHONPATH=apps/mac .venv/bin/python -u \
  scripts/real_g2_location_probe_trace.py \
  --mode walking \
  --out /absolute/private/evidence/gate0-real-g2/walking-receipt.json \
  --duration 120

PYTHONPATH=apps/mac .venv/bin/python -u \
  scripts/real_g2_location_probe_trace.py \
  --mode cycling \
  --out /absolute/private/evidence/gate0-real-g2/cycling-receipt.json \
  --duration 120
```

Keep the phone foreground and unlocked for both runs. The harness refuses to
overwrite an existing receipt or summary and writes both with owner-only
permissions. Its summary reports `pass` only when a `high/success` state is
followed by at least one privacy-bucketed sample; `high/success` without a
sample remains `inconclusive`, and `high/failed` is `fail`. Page, gesture, and
tile counts are bridge evidence. Preserve the synchronized lens video as the
separate proof that the fixed marker and restored HUD were physically visible.
Do not tap while moving, and do not interpret this capture as route guidance.

The following provides the simulator receipt using exactly that production
gateway and protocol, but writes to a disposable path instead of the Mac-shell
support file:

```bash
# terminal 1
cd apps/hub-plugin
VITE_MAC_WS_URL=ws://127.0.0.1:18791/glasses npm run dev -- --host 127.0.0.1 --port 5193

# terminal 2
PYTHONPATH=apps/mac .venv/bin/python -u scripts/simulator_g2_diagnostic_trace.py \
  --port 18791 --out /tmp/joat-g2-diagnostic.json --pre-confirm-delay 10

# terminal 3
evenhub-simulator http://127.0.0.1:5193 --automation-port 9901
curl -sS -X POST http://127.0.0.1:9901/api/input \
  -H 'Content-Type: application/json' -d '{"action":"click"}'

jq . /tmp/joat-g2-diagnostic.json
```

The receipt must contain a `location_probe_state` record plus
`page_transition`, `gesture`, and four ordered `tile_upload` records. A failed
`high` state is the simulator bridge rejecting the location request; a
successful state with no samples is still inconclusive. Neither outcome says
what an installed package delivers on a phone and G2. Keep bridge receipts and
screenshots separate: the receipt proves callback/control results, while
`GET /api/screenshot/glasses` proves the simulator pixels for the initial,
confirmation, and restored HUD states. Self-heal decisions are covered by
focused tests; do not manufacture a failed HUD page merely to collect them
from a simulator.

Simulator 0.9.3 returns a 576x288 RGBA PNG whose monochrome HUD mask is in the
alpha channel. Retain that raw file as the authoritative capture. Some ordinary
image viewers composite its constant green RGB plane and appear blank; create a
human-readable derivative without replacing the raw evidence:

```bash
ffmpeg -y -loglevel error -i gate0-hud.rgba.png \
  -vf alphaextract gate0-hud.png
```

The extracted grayscale image is only a view of the same simulator framebuffer,
not a new renderer and not physical-lens evidence.

### Simulator findings (verified against 0.9.3 — expect these, they are not bugs)

- **No IMU.** The host bridge implements only `getUserInfo`, `getGlassesInfo`,
  `setLocalStorage`, `getLocalStorage`, `createStartUpPageContainer`,
  `rebuildPageContainer`, `updateImageRawData`, `textContainerUpgrade`,
  `shutDownPageContainer`, `audioControl` — no `imuControl` variant. The plugin logs
  `[ImuMonitor] imuControl unavailable on this host` and boots on regardless (mic still
  works). IMU motion is diagnostic only and never approves an action.
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

**The hardware event shape is known.** A single press on the tested text
container arrived as `sysEvent`, so `eventSource` was available. The router
also keeps a source-agnostic `textEvent` CLICK compatibility path for hosts
that deliver the capturing-container event shape described by the SDK; unknown
`sysEvent` sources still fail closed.

What still needs hardware confirmation is narrower: that a single press from
each built-in temple reaches the same active confirmation gate, and that both
swipe directions refuse exactly once. Outside confirmation, the double-press
source split still matters for the JOAT Sidebar versus the system exit dialog.

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
