# Roadmap

Each phase has a hard success metric. Do not advance until the metric is met.

## Phase 0 — Voice → HUD loop (this week)
- Clone and run `men-g2-ble-gateway` on the Mac (stock firmware v2.2.20, no firmware mods).
- Wire WhisperKit (or Parakeet-TDT v3 MLX) STT → local MLX reply → `textContainerUpgrade` on the HUD.
- Build `GlassesTransport` + `SttEngine` interfaces and their first adapters around the gateway.

**Metric: round-trip voice → HUD < 3 s.**

## Phase 1 — Brain + channels
- OpenClaw gateway integration (channels, MCP, cron, skills).
- iMessage + Mail channels; Notification Center scrape → MLX triage → HUD cards.

**Metric: triaged alert latency < 5 s.**

## Phase 2 — Debate loop + Verifier Gate v1
- `ProposerAgent` (Codex CLI: `codex exec --json --output-schema`, `web_search=live`).
- `CriticAgent` (official `claude -p --output-format json`; architect so it can flip to a metered API key + `--max-budget-usd` without a rewrite).
- `JudgeAgent` (local MLX). Verifier Gate v1: fetch + Wayback snapshot + normalized fuzzy quote match. Add the NLI claim-support judge (MiniCheck) second.

**Metric: 0 unverified citations ever pass the gate.**

## Phase 3 — Computer use
- cua driver in a Lume VM sandbox; nod (IMU) or R1-ring confirmation on every consequential action.

**Metric: > 80% task confirm accuracy.**

## Phase 4 — Calls + iPhone control
- BlackHole routing: G2 mic → FaceTime input; live transcript + teleprompter replies on HUD.
- iPhone Mirroring control via mirroir-mcp pattern (ScreenCaptureKit + Vision OCR + Karabiner).

**Metric: live call transcript on HUD.**

## Phase 5 — Background companion + auto handoff
- Swift iOS companion: CoreBluetooth `bluetooth-central` + State Preservation/Restoration (port MentraOS `G2.kt` or use `ffs-os` driver).
- `HandoffManager`: automatic Mac ↔ phone host switching.

**Metric: locked-phone mic stream stays alive.**

## Plan-changing signals
- Apple restricts Mirroring capture/synthetic input → fall back to Continuity/Shortcuts.
- Anthropic un-pauses Agent-SDK credit metering → switch critic to capped API key immediately.
- G2 firmware update changes the auth handshake → pin working firmware; re-derive from community repos before updating.
