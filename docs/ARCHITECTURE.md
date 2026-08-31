# Architecture

## 1. Principles

- **OOP, layered, dependency-inverted.** Domain logic in `core/` depends on abstract interfaces only; hardware, CLIs, and network live behind adapters. Swap a transport (direct-BLE ↔ Hub relay) or an STT engine without touching the orchestrator.
- **Local-first.** MLX LLM, WhisperKit/Parakeet STT, and the Verifier's NLI judge run on the Mac. Cloud is opt-in per task.
- **Verify before trust.** No plan or critique passes between agents without every citation surviving the Verifier Gate.
- **Human-in-the-loop for consequences.** Computer-use actions are gated behind IMU-nod or R1-ring confirmation.

## 2. Component map

| Component | Package | Responsibility |
|---|---|---|
| G2 transport | `joat/ble` | Connect both lens peripherals, 7-packet auth replay, heartbeat, reconnect, host handoff |
| Audio pipeline | `joat/audio` | Ingests 16 kHz mono PCM, VAD/wake word, STT. **Relay path: the Hub SDK delivers PCM already decoded** — LC3 decode (`liblc3`, `0xF1`/NUS, mic enable `[0x0E, 0x01]`) applies only to the direct-BLE path |
| HUD renderer | `joat/display` | 576×288 canvas, text containers, cards, differential page updates |
| Reasoning | `joat/services` | Orchestrator: route utterances to MLX (fast path) or Brainstorm (planning path; `debate` remains a compatibility trigger) |
| Brainstorm agents | `joat/agents` | `SubprocessAgent` (Codex + Claude CLIs), paired private-WSS `OpenClawTerminalAgent` (required Hermes seat for wearable Brainstorm), local MLX judge; `RoundTable` seats them |
| Verifier Gate | `joat/verifier` | Citation fetch → Wayback → fuzzy quote match → NLI claim support → verdict + repair request |
| Channels | `joat/channels` | iMessage/Mail/Calendar, macOS Notification Center triage → HUD cards |
| Computer use | `joat/computeruse` | cua driver in Lume sandbox (Mac), iPhone Mirroring + OCR + Karabiner (iOS) |

## 3. Core abstractions (`joat/core`)

```python
class GlassesTransport(ABC):        # implemented by DirectBleTransport, HubGatewayTransport
    async def connect(self) -> None: ...
    async def send_page(self, page: HudPage) -> None: ...
    def on_audio_frame(self, cb: Callable[[bytes], None]) -> None: ...
    def on_input_event(self, cb: Callable[[InputEvent], None]) -> None: ...

class SttEngine(ABC):               # WhisperKitEngine, ParakeetMlxEngine
    async def transcribe(self, pcm: bytes) -> Transcript: ...

class Agent(ABC):                   # ProposerAgent, CriticAgent, JudgeAgent
    async def run(self, task: DebateTask, context: DebateContext) -> AgentOutput: ...

class CitationVerifier(ABC):        # VerifierGate
    async def verify(self, submission: AgentOutput) -> Verdict: ...

class Channel(ABC):                 # IMessageChannel, MailChannel, NotificationChannel
    async def poll(self) -> list[Event]: ...
    async def send(self, message: OutboundMessage) -> None: ...

class Confirmer(ABC):               # ImuNodConfirmer, RingConfirmer
    async def confirm(self, action: ProposedAction) -> bool: ...
```

Domain models (dataclasses): `HudPage`, `Transcript`, `DebateTask`, `Plan`, `Claim`, `Citation`, `Verdict`, `InputEvent`, `ProposedAction`. All events flow through an in-process `EventBus` so components stay decoupled.

## 4. Operating modes & host handoff

1. **Mac direct-BLE** (desk): CoreBluetooth via the gateway; lowest latency; needs raw LC3 decode.
2. **Even Hub relay** (mobility, and the fastest path to a working system): our Hub plugin (WebView + WebSocket) → Mac over LAN. Officially supported APIs — decoded PCM, IMU, location, camera. Phone must stay foregrounded (Guided Access, Auto-Lock Never). WebSocket domains must be exact-matched in `app.json`. See [HUB_PLUGIN.md](HUB_PLUGIN.md).
3. **Custom iOS companion** (Phase 5): background BLE via `bluetooth-central` + State Preservation/Restoration.

`HandoffManager` (in `services/`) owns the switch: disconnect current central → rescan both lens addresses → reconnect → replay 7-packet auth + init (time-sync, pipe-role, heartbeat) → resend current page state. Only one central per lens, ever.

## Experimental walking/cycling navigation

Navigation is an explicit, foreground-only experimental lane. It is enabled
only when the Mac has both `JOAT_EXPERIMENTAL_NAVIGATION=1` and a
`MAPBOX_ACCESS_TOKEN`; otherwise an explicit navigation request produces a
wearer-visible unavailable screen instead of falling through to the general
assistant.

```text
spoken destination
  → Mac navigation session
  → Mapbox Search Box candidate search
  → canonical place/address confirmation on G2
  → explicit Walk or Cycle choice
  → fresh phone origin
  → Mapbox Directions route + complete geometry
  → phone-local progress matcher
  → semantic HUD state + content-free receipt to Mac
```

The cloud boundary is intentional and must be visible in product disclosure:
Mapbox receives the spoken destination plus approximate search proximity on
the Mac in order to produce canonical candidates. Canonical confirmation
therefore happens after search but before directions. Once a route is
installed, high-rate fixes stay in the plugin; the Mac receives semantic state
only. A precise coordinate leaves the phone again only for the fresh route
origin or after sustained off-route evidence requests a reroute.

The Mac owns destination, mode, provider generation, route revision, and
restart-safe workflow state. The plugin is a disposable renderer/sensor: it
owns the one Even location subscription, projects fixes into a bounded route
corridor, derives course from movement when host heading is absent, paints the
HUD, and rejects stale generations/revisions. Walking and cycling are always a
wearer choice; speed never changes the mode.

`candidate_navigation_policies()` contains replay candidates, not release
truth. Values are injected per mode and remain experimental until anonymized
recorded-route replay plus physical G2 walking/cycling trials measure false
warnings, missed deviations, detection distance, reroute latency, and
fix-to-visible-lens cue time. The supported baseline is phone foreground and
unlocked; background continuation is not promised.

## 5. Brainstorm relay with Verifier Gate

A **round table** of N collaborators, not a fixed proposer/critic pair
(`services/roundtable.py`, driven by the internally named `DebateSession`).
`debate` remains a legacy voice alias; Brainstorm is the wearer-facing name.
One round = every
seat speaks once, in an order the wearer can redirect by voice; then the
local judge scores how far apart the room still is.

```
seat 1  codex     (proposer)      ──▶ Verifier Gate ──▶ deny? → repair request → same seat (capped)
seat 2  claude    (critic)        ──▶ Verifier Gate ──▶ deny? → repair request → same seat
seat 3  openclaw  (third opinion) ──▶ Verifier Gate ──▶ …          [paired, isolated Hermes terminal only]
                                                        │ pass
Judge (local MLX) ── agreement ≥ 0.85 & no blocking issues? ──▶ CONVERGE, else next round (max rounds capped)
```

Three properties are structural rather than policy, so a refactor cannot
quietly drop them:

- **One at a time.** Each speaker's prompt CONTAINS the transcript so far, so
  turn N+1 has no input until turn N exists. Nothing enforces sequencing —
  it is the only thing the data allows.
- **A seat costs a file, not a redesign.** A participant is anything with
  `stream(prompt) -> AsyncIterator[str]`, which the codex CLI, the claude
  CLI, and the OpenClaw terminal all already satisfy. Who is actually seated
  is a runtime fact: absent binaries and an unavailable or unsafe Agent
  Terminal are not admitted. Wearable Brainstorm needs its three idle-ready seats—Codex,
  Claude, and Hermes—so a missing Hermes session renders the retryable Agent
  Terminal state rather than a local-only round. The OpenClaw seat is admitted
  only after exact operator scopes, dedicated session identity, route
  isolation, and an empty `tools.effective` inventory have all been verified.
  It never adopts `main` or Telegram sessions.
- **A failure is visible but never quotable.** A failing CLI prints its
  complaint on *stdout* (`claude -p` exits 1 and says "You're out of usage
  credits"), so only the exit code separates an answer from an error. A
  turn carrying a `[!]` failure notice streams to the wearer and is then
  excluded from the transcript — otherwise the next speaker rebuts a
  billing notice and the judge scores it.

**Models are a setting, not a launch flag** (`services/models.py`). Each
switchable model is a slot the agents read PER TURN, so a change lands on the
next turn instead of the next launch. Choices are named the way they are
spoken ("terra", "sonnet", "turbo") because whisper mangles full model ids,
and the wearer changes them by voice — "JOAT, use sonnet for the critic",
"switch codex to spark", "what models are you using" — which needs no menu
row, no new wire verb, and no plugin change. The grammar is deliberately
narrow, for the same reason turn-direction is: a config change triggered by
ordinary conversation is worse than one that needs rephrasing.

Brainstorm seats cost nothing to switch (the model is an argv field on the next
subprocess call). Whisper holds weights in a resident process, so its slot
carries an applier that respawns serve and **rolls back on failure** — an
unknown model name otherwise leaves the glasses silently deaf with no way to
ask for the old one back.

`ModelRegistry.rows()` renders the same `*`-marked shape as the resume-policy
menu, so a Settings → Models submenu is a view over existing verbs whenever
the plugin wants one.

Brainstorm streams to every `TranscriptSink` at once (`services/sinks.py`):
the glasses, the local console terminal, and a `DebateStateSink` snapshot. The
snapshot is currently write-only in production; More → Brainstorm renders the
setup/invocation page and does not yet reopen history.

Each paired OpenClaw connection enforces its own local, deny-by-default RPC
firewall before socket transmission. Brainstorm and Calendar proposals use
the same configured `hermes` agent on the Mac mini, but distinct WSS
connections, session labels, key namespaces, locks, and persistent transcript
histories. Each permits only the nine exact method-and-parameter shapes used by
its isolated terminal and recovery path, and rejects the other JOAT lane's
session keys. Server-advertised channel,
configuration, agent-management, tool, delivery, `main`, and Telegram
authority cannot widen that local allowlist. OpenClaw-triggered consequential
work must remain behind JOAT's preview, confirmation, native execution,
read-back, and receipt boundary; tools are not enabled on the Brainstorm
session.

The empty `tools.effective` check is an admission and pre-send snapshot, not an
atomic guarantee that a remote administrator cannot change policy between that
check and execution. JOAT detects tool/approval activity and aborts, but that
does not prove a side effect could not already have started. Tools therefore
remain disabled on both JOAT sessions. The first separate productivity lane is
a strict `JOAT_CALENDAR_V1` text proposal: Hermes returns one candidate inside
a total local deadline with separately bounded generation and cleanup, JOAT
validates an exact six-field schema, generates opaque local provenance, and
enters the existing confirmed EventKit path. This is transcript separation,
not a claim of an ephemeral remote session: archive/delete support must be
verified against the live Mac-mini Gateway before JOAT depends on it. A future OpenClaw
automation lane must bind each run to an immutable scoped-tool policy revision,
or equivalent server-side proof, before it can enter the same JOAT action
boundary. Channels/MCP/cron/skills integration is not yet wired.

The native macOS bridge binds its one accepted Unix-socket peer to the exact
Python PID launched by `JOAT.app`, revoking the previous peer when that PID
changes or exits. Directory and socket modes remain defense in depth. This
closes the former same-UID peer-substitution path; it does not make a
compromised engine trustworthy, and a native-verifiable one-shot capability
for each confirmed external write remains a release-hardening gate.

Plan/critique JSON schema: `{ plan_id, round, role, goal, steps[{id, text, claims[{claim_id, text, citation_ids[]}]}], citations[{id, url, title, quote, accessed_at, type}], open_questions[], verdict }`. Every non-trivial claim needs ≥1 citation; every citation carries a verbatim quote.

**Verifier Gate pipeline** (per citation): URL/domain check → HTTP fetch with retries (headless browser / r.jina.ai / Firecrawl fallback on 403/JS) → Wayback availability + Save-Page-Now → normalized fuzzy quote-in-page match → NLI claim-support (MiniCheck / local MLX) → reputation + recency → `{verified | partially_verified | unverifiable}`. Any required citation unverifiable ⇒ **DENY** with a structured repair request. Content hashes logged for audit.

Progress streams to the HUD ("Round 2/4: 3 citations failed, resubmitting…").

## 6. Data flow examples

**Voice → HUD:** long-press temple → mic enable → LC3 stream → decode → STT → orchestrator → MLX → HUD text container.
**Call:** incoming (Continuity) → G2 PCM → BlackHole → FaceTime input; caller audio → AirPods on Mac; live transcript + suggested replies on HUD; answer/decline via App Intents/Shortcuts.
**Triage:** iPhone notification → macOS Notification Center → scrape → MLX priority triage → urgent card on HUD.
**Computer use:** voice command → Brainstorm plan → cua drives Mac in Lume VM → nod/ring confirm → result summary on HUD.

## 7. Known caveats

- Protocol details are community-reverse-engineered; validate the auth bytes against `men-g2-ble-gateway`'s implementation before writing our own transport.
- iPhone Mirroring: no camera/mic mirroring, DRM blanks, EU-unavailable — never the vision pipeline (use SDK/companion camera + Qwen3-VL).
- iOS State Restoration dies on swipe-kill or BT toggle; design assumes the G2 stream drives background wakeups.
