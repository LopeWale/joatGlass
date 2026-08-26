# Mac App — the Brain (`apps/mac/jarvis`)


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
