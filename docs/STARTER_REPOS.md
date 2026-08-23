# Starter Repos — verified, clone these instead of building from zero

Clone into `vendor/` via `scripts/clone_vendors.sh`. Wrap with adapters (RULES.md #4); never copy source into `apps/`. Check licenses before deriving code.

## Tier 1 — clone now (Phase 0–2)

| Repo | What it gives us | Replaces building |
|---|---|---|
| [gpsnmeajp/men-g2-ble-gateway](https://github.com/gpsnmeajp/men-g2-ble-gateway/) | Python BLE gateway: G2 ↔ HTTP/WebSocket/MCP + browser UI. BLE layer ported from MentraOS `G2.kt`. Works on stock fw 2.2.20. FastMCP tools, auto-reconnect, fast text path, diff display updates | The entire Phase 0 transport + display path |
| [i-soxi/even-g2-protocol](https://github.com/i-soxi/even-g2-protocol) | Reverse-engineered G2 BLE protocol docs (7-packet auth, services, commands) | Protocol discovery |
| [pangoleen/awesome-even-realities-g2](https://github.com/pangoleen/awesome-even-realities-g2) | Curated index of all G2 dev resources | Ongoing discovery |
| [alecnielsen/adversarial-review](https://github.com/alecnielsen/adversarial-review) | **The codex↔claude debate that already exists**: multi-agent review with Claude + Codex in an adversarial loop (independent reviews → cross-review → meta-review → synthesis) | Debate loop skeleton — adapt its orchestration, add our Verifier Gate + Judge |
| [Liyan06/MiniCheck](https://github.com/Liyan06/MiniCheck) | EMNLP-2024 fact-checking model (GPT-4-level at 400x lower cost; Bespoke-MiniCheck-7B on HF) | Verifier NLI claim-support judge |
| [amazon-science/RefChecker](https://github.com/amazon-science/RefChecker) | Hallucination-detection pipeline, NLI-based checkers | Verifier claim decomposition patterns |
| [argmaxinc/WhisperKit](https://github.com/argmaxinc/WhisperKit) | On-device Whisper on the Neural Engine (Swift, CLI available) | STT engine |
| [senstella/parakeet-mlx](https://github.com/senstella/parakeet-mlx) | Parakeet-TDT on MLX (low-WER alternative STT) | STT engine (alt) |
| [google/liblc3](https://github.com/google/liblc3) | LC3 codec — decode G2 mic frames to PCM | Audio codec |

## Tier 2 — Phases 3–4

| Repo | What it gives us |
|---|---|
| [trycua/cua](https://github.com/trycua/cua) | Computer-use agent framework + Lume VM sandboxing for Mac control |
| [jfarcand/mirroir-mcp](https://github.com/jfarcand/mirroir-mcp) | iPhone control via iPhone Mirroring: ScreenCaptureKit + Apple Vision OCR + Karabiner DriverKit virtual input, exposed as MCP (also drives macOS apps). Alt: [kae-builds/iphone-mirror-mcp](https://github.com/kae-builds/iphone-mirror-mcp), [nickatnight96/iphone-mirror-mcp](https://glama.ai/mcp/servers/nickatnight96/iphone-mirror-mcp) |
| [ExistentialAudio/BlackHole](https://github.com/ExistentialAudio/BlackHole) | Zero-latency virtual audio driver — G2 mic → FaceTime input. **GPL-3.0: install, don't vendor/link** |
| [awslabs/cli-agent-orchestrator](https://github.com/awslabs/cli-agent-orchestrator) | Multi-CLI-agent orchestration (Claude Code, Codex, …) in isolated tmux sessions — reference for supervising the debate CLIs |
| [markrussinovich/refchecker](https://github.com/markrussinovich/refchecker) | Academic citation validation vs Semantic Scholar/OpenAlex/CrossRef — for DOI/paper citations in the verifier |

## Tier 3 — Phase 5 (iOS companion)

| Repo | What it gives us |
|---|---|
| [Mentra-Community/MentraOS](https://github.com/Mentra-Community/MentraOS) | Full G2 support on iOS + Android (shipped May 2026); `G2.kt` driver to port to Swift — or use MentraOS as the whole phone layer |
| ffs-os G2 driver (see awesome list) | Clean-room Swift G2 BLE driver for the companion |
| [gpsnmeajp/g2_helloworld](https://github.com/gpsnmeajp/g2_helloworld) | Sideload app + WebSocket demo for Hub plugin patterns |
| [brianmatzelle/even-realities-g2-glasses](https://github.com/brianmatzelle/even-realities-g2-glasses) | TypeScript + Vite starter template for Hub plugins → seed for `apps/hub-plugin` |

## Also worth watching (not cloning yet)

- [dsifry/metaswarm](https://github.com/dsifry/metaswarm) — multi-agent framework across Claude/Gemini/Codex CLIs with quality gates; heavier than we need, good patterns.
- Grounded-search APIs for the verifier fetch fallback: r.jina.ai, Firecrawl, Crawl4AI, trafilatura.

## clone_vendors.sh (to write in `scripts/`)

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../vendor"
repos=(
  gpsnmeajp/men-g2-ble-gateway
  i-soxi/even-g2-protocol
  alecnielsen/adversarial-review
  Liyan06/MiniCheck
  argmaxinc/WhisperKit
  senstella/parakeet-mlx
  google/liblc3
)
for r in "${repos[@]}"; do
  d="${r##*/}"
  [ -d "$d" ] && git -C "$d" pull --ff-only || git clone --depth 1 "https://github.com/$r.git" "$d"
done
```
