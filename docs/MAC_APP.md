# Mac App — the Brain (`apps/mac/joat`)


## Native shell — JOAT.app (the Mac app proper)

The brain's ENGINE is Python (the AI stack — mlx-lm, MiniCheck, subprocess
agents — is Python-native and stays that way). Its macOS CITIZENSHIP is the
native Swift app in `apps/mac/JOATShell/`. This section was audited against
Apple's primary documentation (2026-08-25); claims below carry their audit
status.

**Current shape (post-audit):** a REGULAR app — Dock icon, ⌘-Tab, one main
window (engine status + live log + controls) — plus a `MenuBarExtra` status
surface. `Settings` scene for preferences. The engine runs as an app-owned
child `Process`; Quit uses `applicationShouldTerminate → .terminateLater`
and WAITS for the child (SIGKILL escalation after 5 s). Closing the window
does not stop the engine; quitting the app does — a closed app is not
listening.

| Concern | How | Audit status |
|---|---|---|
| Launch at login | `SMAppService.mainApp`, `.requiresApproval` surfaced via `openSystemSettingsLoginItems()`, status re-read on menu open | correct API (documented) |
| Signing | Developer ID + hardened runtime + secure timestamp + entitlements file | required for notarization |
| Icon | asset catalog (`Assets.xcassets`), compiled by `actool` | Apple's documented path; Icon Composer `.icon` can drop in later |
| Build | `xcodegen` + `xcodebuild archive` + `exportArchive` | Apple's documented distribution flow |
| Engine supervision | direct child, crash circuit-breaker, port-ownership reaping, engine-side orphan watchdog (exit on reparent to PID 1) | pattern documented; details ours |
| Repo location | UserDefaults + folder picker | dev harness, see debt list |

**TCC reality (do not overstate):** the Python child is EXPECTED to inherit
the app's Full Disk Access via responsible-process attribution. Apple DTS
describes this behavior but it is NOT a documented API contract, and it
regressed once before (macOS 11.4 stopped helper FDA inheritance as a CVE
fix). The engine must treat a TCC denial as a visible state, never assume
the grant. Never "fix" a broken grant by adding the bare python binary to
the FDA list — that grants FDA to every Python script on the machine.

The generated Debug host is deliberately a different TCC principal:
`com.joat.shell.debug`, displayed as **JOAT Debug**, on ports 18787/51060.
When Xcode hosts the application for XCTest, the shell does not initialize
Accessibility, its native bridge, notification watchers, or the Python engine.
This prevents a disposable test launch from presenting a Documents prompt that
can be mistaken for the installed Developer-ID app at `/Applications/JOAT.app`.

**Known architectural debt (from the audit, in priority order):**
1. ~~No Xcode app target.~~ **DONE.** `apps/mac/JOATShell/project.yml` is an
   XcodeGen spec; `scripts/build_app.sh` regenerates the project, archives,
   and exports a Developer ID app. The `.xcodeproj` is generated and
   gitignored — a checked-in pbxproj drifts from its sources and conflicts
   on every merge. This unblocked: the asset-catalog icon (Assets.car),
   entitlements, Swift 6 strict concurrency (which immediately caught a
   non-concurrency-safe global), universal binaries (archive honours
   ARCHS_STANDARD; plain `build` does not), and the notarization path.
2. **Engine interpreter lives outside the bundle** (user repo `.venv`).
   Ship-quality fix: vendor a relocatable Python into `Contents/Helpers/`,
   sign every nested Mach-O inside-out (never `codesign --deep`), engine id
   `com.joat.shell.engine`. Makes the app self-contained and gives TCC its
   strongest (documented-adjacent) attribution case.
3. **Not notarized YET** — the pipeline exists (`build_app.sh --notarize`:
   ditto → `notarytool submit --wait` → `stapler staple` → `spctl` check).
   It needs a one-time credential:
   `xcrun notarytool store-credentials joat --apple-id … --team-id … --password <app-specific>`.
   Until then the app runs here but Gatekeeper BLOCKS it on any other Mac.
4. No update channel (Sparkle needs the Xcode target's nested-code layout),
   no dSYM archiving, no localization, shell has no os_log.

JOAT.app owns the local Python engine as a child process on the **home Mac that
is acting as the brain**. That may be the development Mac during work, but the
wearer's ordinary interface remains the G2 HUD. BLE transport, audio,
Brainstorm coordination, verification, native productivity execution, and
action authority stay on the selected home Mac. The currently wired OpenClaw
role is hosted separately on the private Mac mini and is reached only through
its managed Tailscale Serve WSS origin. Scoped OpenClaw
channels/MCP/cron/skills integration remains planned but is not yet wired.

## Home Mac mini brain contract

`./scripts/setup_brain.sh --service --warm` is intentionally a **core-engine**
installer, not a complete Agent Terminal installer. It starts the Python
process through `launchd`; it does not start JOAT.app's native bridge. The
bridge is the sole authority for the per-Mac Keychain identity and paired
OpenClaw device token, so a raw service correctly reports Agent Terminal as
`native_credentials_unavailable` rather than substituting an environment token
or a shared credential.

For a mini to be an unattended Brainstorm/Agent Terminal brain, perform this
once while logged into that mini's normal macOS user:

1. Clone JOAT at a conventional location, run `setup_brain.sh --warm`, and
   install a notarized Developer ID JOAT.app. A merely locally signed build
   can be blocked by Gatekeeper on a different mini. The app's child-engine
   discovery deliberately finds `~/joatGlass` without a daily folder-picker
   step.
2. Launch JOAT.app once and grant only the permissions that mini will use. It
   starts the native bridge and its Python child under the same user identity.
3. Add the approved OpenClaw Gateway origin plus `hermes` agent ID to that
   mini's `~/Library/Application Support/JOAT/openclaw.json`, then approve the
   resulting **per-mini** OpenClaw device request. The identity and token stay
   in that mini's Keychain; they are not copied from the development Mac.
4. Ensure the mini's exact Tailscale WebSocket origin is present in the packed
   Even `app.json` network whitelist and in the comma-separated candidate list
   baked into the package. Even Hub enforces exact declared network origins;
   adding a new mini requires a reviewed package update, not a runtime URL
   field.
5. Pair the G2 once through JOAT.app's local 120-second pairing window, then
   enable JOAT.app's Login Item. Thereafter the glasses/phone reconnect to the
   remembered healthy home brain without visiting the Mac.

### Private Tailnet transport (one-time)

On the home Mini, enable **Settings → Private Tailnet home brain** and restart
JOAT.app. That passes `--private-tailnet-gateway` to the app-owned engine: it
binds the glasses gateway to `127.0.0.1:8787` and keeps its pairing check on.
This matters because the secure Tailscale reverse proxy itself reaches JOAT
from loopback; loopback is not an authority boundary for a remote G2 package.

Then, from the Mini's logged-in macOS user, configure private Serve—not public
Funnel—and retain its status with the Tailnet:

```sh
tailscale serve --https=443 --bg http://127.0.0.1:8787
tailscale serve status --json
```

The wearer-facing candidate in the packaged plugin is the port-443 origin
`wss://home-mac.example-tailnet.ts.net/glasses`; the accompanying manifest
whitelist uses that same origin without a path. Tailscale's HTTPS edge is the
TLS boundary, while the app-level pairing token is still required after the
WebSocket upgrade. Do not point the plugin directly at `wss://…:8787`: JOAT's
local gateway intentionally speaks plain WebSocket and does not mint TLS
certificates itself.

This is a setup operation, not a daily control step. Tailscale Serve persists
when configured in background mode, and JOAT.app starts at login. A Mini user
session must still be logged in and unlocked for its Keychain-backed OpenClaw
credentials and native bridge to be available.

Home-brain failover is a fresh authenticated session, not transparent process
migration: pending audio, confirmations, navigation, and a running Hermes turn
are deliberately cleared rather than replayed on another mini. The Hub stores
pairing tokens by approved origin and remembers the last healthy candidate, so
a rejected token for one mini cannot erase a healthy pairing for another.

## Runtime layout

```
joat/
├── core/          # ABCs + dataclasses + EventBus (zero external deps)
├── ble/           # DirectBleTransport (bleak/CoreBluetooth), HubGatewayTransport (WS client to men-g2-ble-gateway)
├── audio/         # Lc3Decoder (liblc3), VadWakeWord, WhisperKitEngine, ParakeetMlxEngine
├── display/       # HudRenderer: 576x288 pages, text containers, cards, diff updates
├── agents/        # Codex/Claude seats, local judge, and paired OpenClawTerminalAgent
├── verifier/      # VerifierGate: Fetcher, WaybackClient, QuoteMatcher, NliJudge (MiniCheck), Verdict
├── channels/      # IMessageChannel, MailChannel, NotificationTriage
├── computeruse/   # CuaDriver (Lume VM), IphoneMirrorDriver (mirroir-mcp pattern)
└── services/      # Orchestrator, SessionManager, HandoffManager, HudStreamer
```

## Key classes

**`Orchestrator` (services/)** — the entry point. Subscribes to `TranscriptReady` events, classifies intent:
- *Fast path*: question/command → local MLX → `HudRenderer.show()`.
- *Planning path*: complex/multi-step → Brainstorm `RoundTable` / internal `DebateSession` → stream progress to HUD → confirmed plan → optional `CuaDriver` execution behind `Confirmer`.

**`DirectBleTransport` (ble/)** — connects both lens peripherals, replays the 7-packet auth + init (time-sync, pipe-role, heartbeat), maintains heartbeat, auto-reconnects. Validate auth bytes against `men-g2-ble-gateway` before implementing; in Phase 0 we use `HubGatewayTransport` (the gateway's WS/MCP API) and defer raw BLE.

**`HandoffManager` (services/)** — decides desk vs. mobile mode (Mac BLE RSSI / phone reachability), executes the disconnect→rescan→re-auth→resend-page sequence.

**Audio chain (audio/)** — `Lc3Decoder` (0xF1/NUS frames → 16 kHz PCM) → `VadWakeWord` (always-listening) → `SttEngine`. WhisperKit runs on the Neural Engine (99 langs); Parakeet-TDT v3 on MLX is the low-WER alternative (25 EU langs). Engine selected in `config/`.

**Brainstorm (`RoundTable` + internal `DebateSession`)** — state machine per
docs/ARCHITECTURE.md §5. `debate` remains a compatibility voice trigger. Seats
include:
- `ProposerAgent`: `codex exec --json --output-schema plan.schema.json` (web_search=live, sandbox read-only).
- `CriticAgent`: `claude -p --output-format json --allowedTools "WebSearch,WebFetch" --max-turns N`. Constructor takes an `AuthStrategy` (subscription binary | metered API key + `--max-budget-usd`) so the ToS fallback is a config change.
- `JudgeAgent`: local MLX model; emits agreement score + blocking issues.
- `OpenClawTerminalAgent`: optional paired private-WSS adapter. Brainstorm and
  Calendar proposals address the same configured Mac-mini `hermes` agent but
  use distinct route-isolated session labels, key namespaces, connections,
  locks, and persistent transcript histories.

**`VerifierGate` (verifier/)** — pipeline objects, each a small class: `Fetcher` (httpx, retries, headless/reader fallback), `WaybackClient` (availability + Save-Page-Now), `QuoteMatcher` (normalized fuzzy match), `NliJudge` (MiniCheck or MLX), composed by `VerifierGate.verify()` → `Verdict`. Denials build `RepairRequest`s listing each failed citation and reason.

**Calls (channels/ + audio/)** — G2 PCM → BlackHole virtual device → FaceTime input; caller → AirPods on Mac; `HudStreamer` shows live transcript + suggested replies; answer/decline via App Intents/Shortcuts (verify per macOS build).

**`IphoneMirrorDriver` (computeruse/)** — ScreenCaptureKit capture of the iPhone Mirroring window + Apple Vision OCR + Karabiner DriverKit virtual input (mirroir-mcp pattern). Hard limits: no camera/mic, DRM blanks, EU-unavailable — never used for vision.

## Config (`config/`)

- `joat.toml`: mode (desk/mobile/auto), STT engine, MLX model ids, Brainstorm caps (internally still named `debate`: max rounds, retries, judge threshold 0.85), budget caps.
- `plan.schema.json` / `critique.schema.json`: the debate JSON schemas.
- `domains.allowlist`: verifier domain reputation seeds; Hub `app.json` WS domains.
- Pinned CLI versions (Codex v0.148.0 at time of writing) — re-verify flags on upgrade.

## Service management

- JOAT.app owns and shuts down the local Python engine child; the healthcheck
  remains loopback-only, with structured logs that retain hashes rather than
  content where practical.
- The optional OpenClaw Brainstorm seat uses paired protocol-v4 WSS over managed
  Tailscale Serve to the private Mac mini. A second JOAT connection uses the
  same installed/configured `hermes` agent for Calendar proposals, without
  installing another Hermes. Each connection adopts only its own exact
  route-isolated session, requires an empty effective tool inventory at
  admission and immediately before each send, and rejects the other JOAT
  namespace plus existing `main`/Telegram sessions. A local exact-shape RPC
  firewall rejects channel, configuration, agent-management, tool invocation,
  delivery-routing, and non-Hermes requests before a WebSocket frame is
  written. That inventory check is not an atomic server-side execution policy;
  Hermes therefore remains configured with no tools in either JOAT lane.
  The explicit Calendar-proposal path accepts only deadline-bounded,
  versioned Hermes text and hands the resulting candidate to JOAT's local
  preview, confirmation, native execution, read-back, and receipt boundary;
  it grants Hermes no EventKit handle or OpenClaw tool. Its remote session is
  separate but persistent; archive/delete remains a live Gateway verification
  question, not an ephemeral-session claim. General OpenClaw
  channels/MCP/cron/skills and automation wiring remains planned but unwired.
- The native bridge accepts only the exact Python PID launched by `JOAT.app`
  and revokes it on exit. This prevents another same-UID process from replacing
  the engine socket peer. A compromised engine remains inside the trusted
  boundary; native one-shot confirmation capability is still a hardening gate.
