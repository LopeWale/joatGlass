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
| Reasoning | `joat/services` | Orchestrator: route utterances to MLX (fast path) or the debate loop (planning path) |
| Debate agents | `joat/agents` | `SubprocessAgent` (Codex + Claude CLIs), `OpenClawAgent` (local gateway, optional third seat), local MLX judge; `RoundTable` seats them |
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

## 5. Debate loop with Verifier Gate

A **round table** of N collaborators, not a fixed proposer/critic pair
(`services/roundtable.py`, driven by `DebateSession`). One round = every
seat speaks once, in an order the wearer can redirect by voice; then the
local judge scores how far apart the room still is.

```
seat 1  codex     (proposer)      ──▶ Verifier Gate ──▶ deny? → repair request → same seat (capped)
seat 2  claude    (critic)        ──▶ Verifier Gate ──▶ deny? → repair request → same seat
seat 3  openclaw  (third opinion) ──▶ Verifier Gate ──▶ …          [seated only if its gateway answers]
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
  CLI, and the OpenClaw gateway all already satisfy. Who is actually seated
  is a runtime fact: absent binaries and unreachable daemons are simply not
  at the table, and a debate needs two.
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

Debate seats cost nothing to switch (the model is an argv field on the next
subprocess call). Whisper holds weights in a resident process, so its slot
carries an applier that respawns serve and **rolls back on failure** — an
unknown model name otherwise leaves the glasses silently deaf with no way to
ask for the old one back.

`ModelRegistry.rows()` renders the same `*`-marked shape as the resume-policy
menu, so a Settings → Models submenu is a view over existing verbs whenever
the plugin wants one.

The debate streams to every `TranscriptSink` at once (`services/sinks.py`):
the glasses, the terminal, and a `DebateStateSink` snapshot that the HUD's
Debate panel can re-open after the fact.

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
