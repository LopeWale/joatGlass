# J.A.R.V.I.S. // Even Realities G2

A personal AI assistant for the Even Realities G2 smart glasses, powered by an always-on Mac Studio (the brain) and an iPhone (relay/companion). Voice in, HUD out — with a cited planning debate loop (Codex proposes, Claude critiques, local MLX judges) gated by a source Verifier that denies anything it can't verify against the live web.

## System at a glance

```
G2 glasses (BLE, 576x288 HUD, LC3 mic)
   │
   ├── Mac direct-BLE (at desk, lowest latency)
   └── iPhone (Even Hub relay → later: custom background-BLE companion)
   │
Mac Studio brain
   ├── STT (WhisperKit / Parakeet-MLX) + wake word
   ├── Local MLX LLM (reasoning + debate Judge)
   ├── Debate loop: Codex CLI (Proposer) ⇄ Claude CLI (Critic) ⇄ Verifier Gate
   ├── Computer use (cua + Lume sandbox, iPhone Mirroring)
   └── Channels (iMessage, Mail, Calendar, FaceTime via BlackHole)
```

## Repository layout

```
utilsG2/
├── docs/                     # Architecture, roadmap, rules, Mac app, starter repos
├── apps/
│   ├── mac/jarvis/           # Python package — the brain (OOP, layered)
│   │   ├── core/             # Domain models, abstract interfaces, events
│   │   ├── ble/              # G2 transport adapters (direct-BLE, gateway client)
│   │   ├── audio/            # LC3 decode, STT engines, VAD/wake word
│   │   ├── display/          # HUD renderer (576x288 canvas, pages, cards)
│   │   ├── agents/           # Proposer / Critic / Judge classes + loop state machine
│   │   ├── verifier/         # Verifier Gate (fetch, quote-match, NLI, Wayback)
│   │   ├── channels/         # iMessage, Mail, Notification triage
│   │   ├── computeruse/      # cua driver, iPhone Mirroring adapters
│   │   └── services/         # Orchestrator, session, handoff manager
│   ├── ios-companion/        # Phase 5: Swift background-BLE companion
│   └── hub-plugin/           # Even Hub SDK plugin (TypeScript + Vite)
├── vendor/                   # Cloned third-party repos (see docs/STARTER_REPOS.md)
├── config/                   # App config, schemas, allowlists
├── scripts/                  # Setup, clone-vendors, dev helpers
└── tests/                    # unit/ and integration/
```

## Docs

| Doc | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, OOP design, interfaces, data flow, debate loop + Verifier Gate |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Phases 0–5 with success metrics |
| [docs/RULES.md](docs/RULES.md) | Engineering rules, safety gates, ToS constraints |
| [docs/MAC_APP.md](docs/MAC_APP.md) | Mac Studio brain — detailed design |
| [docs/HUB_PLUGIN.md](docs/HUB_PLUGIN.md) | Even Hub SDK reference — the relay path, container model, audio/IMU/location APIs |
| [docs/STARTER_REPOS.md](docs/STARTER_REPOS.md) | Verified GitHub repos to clone so we don't build from zero |
| [docs/NEXT_STEPS.md](docs/NEXT_STEPS.md) | What to do next, in order, and what's deliberately deferred |

## Quick start (Phase 0)

1. Clone the vendor repos: see `docs/STARTER_REPOS.md` (start with `men-g2-ble-gateway`).
2. G2 on stock firmware v2.2.20; disconnect the phone (one BLE central per lens).
3. Run the gateway on the Mac, wire STT → MLX reply → `textContainerUpgrade` on the HUD.
4. Target: round-trip voice → HUD under 3 seconds.

## Key constraints (read RULES.md before coding)

- HUD canvas is **576×288 monochrome green** — build against this only.
- One BLE central per lens; handoff = disconnect → rescan → 7-packet auth replay → resend page state.
- Claude critic: invoke the official `claude` binary only; never reuse OAuth tokens in third-party clients.
- Every consequential computer-use action requires explicit confirmation (IMU nod or R1 ring).
- Stay on stock G2 firmware (ECDSA-P256-signed images block custom firmware).
