# CLAUDE.md — J.A.R.V.I.S. // Even Realities G2

Personal AI assistant for the Even Realities G2 smart glasses: a Mac Studio brain,
an iPhone relay, voice in and HUD out. See [README.md](README.md) for the system diagram.

## Repository layout

| Path | Contents |
|---|---|
| `apps/mac/jarvis/` | Python package — the brain (core, ble, audio, display, agents, verifier, channels, computeruse, services) |
| `apps/ios-companion/` | Phase 5: Swift background-BLE companion |
| `apps/hub-plugin/` | Even Hub SDK plugin (TypeScript + Vite) |
| `config/` | App config, schemas, allowlists |
| `scripts/` | Setup, clone-vendors, dev helpers |
| `tests/` | `unit/` and `integration/` |
| `vendor/` | Cloned third-party repos — gitignored, reproduce via `scripts/clone_vendors.sh` |

## Status

Phase 0. The hub plugin (`apps/hub-plugin`) is a real Vite + TypeScript
package: `npm run build` (typecheck + bundle) and `npm test` (vitest) both
work — run them from that directory after touching plugin code. The Python
brain (`apps/mac/jarvis`) is still an empty skeleton with no `pyproject.toml`
and no test runner — establish one before relying on any workflow that says
"run the test suite" against Python code.

## Key constraints

Read [docs/RULES.md](docs/RULES.md) before coding. The load-bearing ones:

- HUD canvas is **576x288 monochrome green** — build against this only.
- One BLE central per lens; handoff = disconnect, rescan, 7-packet auth replay, resend page state.
- Claude critic: invoke the official `claude` binary only; never reuse OAuth tokens in third-party clients.
- Every consequential computer-use action requires explicit confirmation (IMU nod or R1 ring).
- Stay on stock G2 firmware (ECDSA-P256-signed images block custom firmware).

## Cross-Model Collaboration (Codex)

Claude Code can delegate work to OpenAI Codex via `.claude/bin/codex-bridge.sh`.
Both run on subscriptions — no API keys. The bridge script unsets `OPENAI_API_KEY`
so Codex never accidentally uses your project's API key for billing.

**Commands:**

| Command | Purpose |
|---------|---------|
| `/collab <task>` | Full collaboration: think, build, or debug with Codex |
| `/collab-review` | Quick second opinion from Codex on current changes |

**How it works:** Claude calls `codex exec` via bash. Codex's response streams
directly into Claude's context. Claude reads it, reasons about it, synthesizes.
No tmux, no file polling, no third-party tools. Build tasks run asynchronously
in the background so the user can keep talking to Claude while Codex works.

**Bridge modes:**
- `codex-bridge.sh think "prompt"` — read-only, for debate and review (sync)
- `codex-bridge.sh build "prompt"` — workspace-write, for implementation (async)
- `codex-bridge.sh build "prompt" path/to/spec.md` — build from spec file (async)

**Build specs** go in `.collab/specs/`. **Reports** go in `.collab/reports/`.

**Rules:**
- Never assign overlapping files to both Claude subagents and Codex
- Always run your test suite after Codex builds — never trust the self-report
- Keep Codex prompts concise (500 words max) — it has no session memory
- Synthesize Codex output for the user, don't relay it raw
- Compact context before starting a collab workflow
- Break large builds into 2-3 small focused Codex calls (max 3 files each)

### Local setup notes (this machine)

The bridge is adapted to the installed CLI (`codex-cli 0.148.0-alpha.15`):

- **`codex` ships inside the ChatGPT app bundle** and is not on `PATH` by default.
  Symlinked: `~/.local/bin/codex` -> `/Applications/ChatGPT.app/Contents/Resources/codex`.
- **`--full-auto` no longer exists** on `codex exec` in this version. Build mode uses
  `-s workspace-write -c approval_policy="never"` instead — same intent, current syntax.
- **The model is pinned in the bridge**, not inherited from `~/.codex/config.toml`, so a
  collab run cannot silently change which model your interactive `codex` sessions use.
  Default `gpt-5.3-codex-spark`; override per call with `CODEX_MODEL=<model>`.
  Reason: `gpt-5.6-terra` subscription credits are exhausted until 2026-08-28.
  Revisit that default once credits reset.
- **`npm test` applies only to `apps/hub-plugin`** (vitest; run it from that
  directory). The Python side has no test runner yet — substitute the real
  command in the rules above once one is wired.

## Docs

| Doc | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, OOP design, interfaces, data flow, debate loop + Verifier Gate |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Phases 0-5 with success metrics |
| [docs/RULES.md](docs/RULES.md) | Engineering rules, safety gates, ToS constraints |
| [docs/MAC_APP.md](docs/MAC_APP.md) | Mac Studio brain — detailed design |
| [docs/STARTER_REPOS.md](docs/STARTER_REPOS.md) | Verified GitHub repos to clone so we don't build from zero |
