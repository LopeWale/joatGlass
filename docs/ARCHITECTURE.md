# Architecture

## 1. Principles

- **OOP, layered, dependency-inverted.** Domain logic in `core/` depends on abstract interfaces only; hardware, CLIs, and network live behind adapters. Swap a transport (direct-BLE ↔ Hub relay) or an STT engine without touching the orchestrator.
- **Local-first.** MLX LLM, WhisperKit/Parakeet STT, and the Verifier's NLI judge run on the Mac. Cloud is opt-in per task.
- **Verify before trust.** No plan or critique passes between agents without every citation surviving the Verifier Gate.
- **Human-in-the-loop for consequences.** Computer-use actions are gated behind IMU-nod or R1-ring confirmation.

## 2. Component map

| Component | Package | Responsibility |
|---|---|---|
| G2 transport | `jarvis/ble` | Connect both lens peripherals, 7-packet auth replay, heartbeat, reconnect, host handoff |
| Audio pipeline | `jarvis/audio` | Mic enable `[0x0E, 0x01]`, LC3 (0xF1/NUS) → `liblc3` → 16 kHz PCM, VAD/wake word, STT |
| HUD renderer | `jarvis/display` | 576×288 canvas, text containers, cards, differential page updates |
| Reasoning | `jarvis/services` | Orchestrator: route utterances to MLX (fast path) or the debate loop (planning path) |
| Debate agents | `jarvis/agents` | `ProposerAgent` (Codex CLI), `CriticAgent` (Claude CLI), `JudgeAgent` (local MLX), `DebateLoop` state machine |
| Verifier Gate | `jarvis/verifier` | Citation fetch → Wayback → fuzzy quote match → NLI claim support → verdict + repair request |
| Channels | `jarvis/channels` | iMessage/Mail/Calendar, macOS Notification Center triage → HUD cards |
| Computer use | `jarvis/computeruse` | cua driver in Lume sandbox (Mac), iPhone Mirroring + OCR + Karabiner (iOS) |

## 3. Core abstractions (`jarvis/core`)

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

1. **Mac direct-BLE** (primary, at desk): CoreBluetooth via the gateway; lowest latency.
2. **Even Hub relay** (mobility): Hub plugin (WebView + WebSocket) → Mac over LAN. Phone must stay foregrounded (Guided Access, Auto-Lock Never). WebSocket domains must be exact-matched in `app.json`.
3. **Custom iOS companion** (Phase 5): background BLE via `bluetooth-central` + State Preservation/Restoration.

`HandoffManager` (in `services/`) owns the switch: disconnect current central → rescan both lens addresses → reconnect → replay 7-packet auth + init (time-sync, pipe-role, heartbeat) → resend current page state. Only one central per lens, ever.

## 5. Debate loop with Verifier Gate

State machine (`DebateLoop`):

```
Proposer (codex exec --json --output-schema)   ──▶ Verifier Gate ──▶ deny? → repair request → Proposer (retry, capped)
                                                        │ pass
Critic (claude -p --output-format json)        ──▶ Verifier Gate ──▶ deny? → repair request → Critic
                                                        │ pass
Judge (local MLX) ── agreement ≥ 0.85 & no blocking issues? ──▶ CONVERGE, else next round (max rounds capped)
```

Plan/critique JSON schema: `{ plan_id, round, role, goal, steps[{id, text, claims[{claim_id, text, citation_ids[]}]}], citations[{id, url, title, quote, accessed_at, type}], open_questions[], verdict }`. Every non-trivial claim needs ≥1 citation; every citation carries a verbatim quote.

**Verifier Gate pipeline** (per citation): URL/domain check → HTTP fetch with retries (headless browser / r.jina.ai / Firecrawl fallback on 403/JS) → Wayback availability + Save-Page-Now → normalized fuzzy quote-in-page match → NLI claim-support (MiniCheck / local MLX) → reputation + recency → `{verified | partially_verified | unverifiable}`. Any required citation unverifiable ⇒ **DENY** with a structured repair request. Content hashes logged for audit.

Progress streams to the HUD ("Round 2/4: 3 citations failed, resubmitting…").

## 6. Data flow examples

**Voice → HUD:** long-press temple → mic enable → LC3 stream → decode → STT → orchestrator → MLX → HUD text container.
**Call:** incoming (Continuity) → G2 PCM → BlackHole → FaceTime input; caller audio → AirPods on Mac; live transcript + suggested replies on HUD; answer/decline via App Intents/Shortcuts.
**Triage:** iPhone notification → macOS Notification Center → scrape → MLX priority triage → urgent card on HUD.
**Computer use:** voice command → debate plan → cua drives Mac in Lume VM → nod/ring confirm → result summary on HUD.

## 7. Known caveats

- Protocol details are community-reverse-engineered; validate the auth bytes against `men-g2-ble-gateway`'s implementation before writing our own transport.
- iPhone Mirroring: no camera/mic mirroring, DRM blanks, EU-unavailable — never the vision pipeline (use SDK/companion camera + Qwen3-VL).
- iOS State Restoration dies on swipe-kill or BT toggle; design assumes the G2 stream drives background wakeups.
