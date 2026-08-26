# Mac App — the Brain (`apps/mac/jarvis`)


## Native shell — JOAT.app (the Mac app proper)

The brain's ENGINE is Python (the AI stack — mlx-lm, MiniCheck, subprocess
agents — is Python-native and stays that way). Its macOS CITIZENSHIP is a
native Swift shell, per Apple's current recommendations:

| Concern | How | Apple guidance followed |
|---|---|---|
| App shape | SwiftUI `MenuBarExtra`, `LSUIElement` background app | modern menu-bar app API |
| Engine lifecycle | JOAT.app spawns/supervises `.venv/bin/python -m jarvis` as a child `Process`; crash → backoff restart; Quit stops listening | app-owned subprocess |
| Launch at login | `SMAppService.mainApp` toggle in the menu | sanctioned login-item API (replaces hand-written launchd plists) |
| Permissions | ONE Full Disk Access grant on **JOAT.app** — the child engine inherits the app's TCC identity | signed-app TCC attribution |
| Signing | Developer ID Application + hardened runtime | stable identity: grants survive rebuilds |
| Repo location | chosen via standard folder picker, stored in `UserDefaults` — nothing machine-specific in code | |

Source lives in `apps/mac/JOATShell/` (SwiftPM package — opens directly in
Xcode; `scripts/build_app.sh` builds, signs, and installs JOAT.app).
The earlier `com.joat.brain` launchd agent is superseded by the app and must
be unloaded when the app manages the engine (port 8787 conflict otherwise).

Honest note: notification triage reads Apple's Biome store
(`Notification.Delivery` SEGB segments) — there is NO sanctioned API for
reading other apps' notifications, in Swift or anything else. That read is
unsupported by design and implemented read-only + fail-dormant.


The Mac Studio runs everything: BLE transport, audio, reasoning, the debate loop, verifier, channels, and computer use. Python 3.12+ package, structured as a long-running service (launchd), OOP throughout.

## Runtime layout

```
jarvis/
├── core/          # ABCs + dataclasses + EventBus (zero external deps)
├── ble/           # DirectBleTransport (bleak/CoreBluetooth), HubGatewayTransport (WS client to men-g2-ble-gateway)
├── audio/         # Lc3Decoder (liblc3), VadWakeWord, WhisperKitEngine, ParakeetMlxEngine
├── display/       # HudRenderer: 576x288 pages, text containers, cards, diff updates
├── agents/        # ProposerAgent (subprocess codex), CriticAgent (subprocess claude), JudgeAgent (MLX), DebateLoop
├── verifier/      # VerifierGate: Fetcher, WaybackClient, QuoteMatcher, NliJudge (MiniCheck), Verdict
├── channels/      # IMessageChannel, MailChannel, NotificationTriage
├── computeruse/   # CuaDriver (Lume VM), IphoneMirrorDriver (mirroir-mcp pattern)
└── services/      # Orchestrator, SessionManager, HandoffManager, HudStreamer
```

## Key classes

**`Orchestrator` (services/)** — the entry point. Subscribes to `TranscriptReady` events, classifies intent:
- *Fast path*: question/command → local MLX → `HudRenderer.show()`.
- *Planning path*: complex/multi-step → `DebateLoop.run(task)` → stream progress to HUD → confirmed plan → optional `CuaDriver` execution behind `Confirmer`.

**`DirectBleTransport` (ble/)** — connects both lens peripherals, replays the 7-packet auth + init (time-sync, pipe-role, heartbeat), maintains heartbeat, auto-reconnects. Validate auth bytes against `men-g2-ble-gateway` before implementing; in Phase 0 we use `HubGatewayTransport` (the gateway's WS/MCP API) and defer raw BLE.

**`HandoffManager` (services/)** — decides desk vs. mobile mode (Mac BLE RSSI / phone reachability), executes the disconnect→rescan→re-auth→resend-page sequence.

**Audio chain (audio/)** — `Lc3Decoder` (0xF1/NUS frames → 16 kHz PCM) → `VadWakeWord` (always-listening) → `SttEngine`. WhisperKit runs on the Neural Engine (99 langs); Parakeet-TDT v3 on MLX is the low-WER alternative (25 EU langs). Engine selected in `config/`.

**`DebateLoop` (agents/)** — state machine per docs/ARCHITECTURE.md §5. Agents are subprocess wrappers:
- `ProposerAgent`: `codex exec --json --output-schema plan.schema.json` (web_search=live, sandbox read-only).
- `CriticAgent`: `claude -p --output-format json --allowedTools "WebSearch,WebFetch" --max-turns N`. Constructor takes an `AuthStrategy` (subscription binary | metered API key + `--max-budget-usd`) so the ToS fallback is a config change.
- `JudgeAgent`: local MLX model; emits agreement score + blocking issues.

**`VerifierGate` (verifier/)** — pipeline objects, each a small class: `Fetcher` (httpx, retries, headless/reader fallback), `WaybackClient` (availability + Save-Page-Now), `QuoteMatcher` (normalized fuzzy match), `NliJudge` (MiniCheck or MLX), composed by `VerifierGate.verify()` → `Verdict`. Denials build `RepairRequest`s listing each failed citation and reason.

**Calls (channels/ + audio/)** — G2 PCM → BlackHole virtual device → FaceTime input; caller → AirPods on Mac; `HudStreamer` shows live transcript + suggested replies; answer/decline via App Intents/Shortcuts (verify per macOS build).

**`IphoneMirrorDriver` (computeruse/)** — ScreenCaptureKit capture of the iPhone Mirroring window + Apple Vision OCR + Karabiner DriverKit virtual input (mirroir-mcp pattern). Hard limits: no camera/mic, DRM blanks, EU-unavailable — never used for vision.

## Config (`config/`)

- `jarvis.toml`: mode (desk/mobile/auto), STT engine, MLX model ids, debate caps (max rounds, retries, judge threshold 0.85), budget caps.
- `plan.schema.json` / `critique.schema.json`: the debate JSON schemas.
- `domains.allowlist`: verifier domain reputation seeds; Hub `app.json` WS domains.
- Pinned CLI versions (Codex v0.148.0 at time of writing) — re-verify flags on upgrade.

## Service management

- launchd plist keeps the brain alive; healthcheck endpoint on localhost; structured JSON logs with content hashes (not content) for audit.
- OpenClaw gateway (`ws://127.0.0.1:18789`) provides channels/cron/skills; the debate loop is exposed as an OpenClaw skill.
