# Architecture

## 1. Principles

- **OOP, layered, dependency-inverted.** Domain logic in `core/` depends on abstract interfaces only; hardware, CLIs, and network live behind adapters. Swap a transport (direct-BLE ↔ Hub relay) or an STT engine without touching the orchestrator.
- **Local-first for the data, the wearer's subscriptions for the mind.** WhisperKit STT and the Verifier's NLI judge run on the Mac. The assistant's seat is Claude Code (`claude -p`, Max subscription) or Codex CLI (`codex exec`, ChatGPT subscription), never an API key (RULES.md #9, #10); `--agent mlx` keeps the whole turn on the Mac. The spoken query and the tool results the mind reads leave the Mac for that turn, so tools hand it summaries, not archives.
- **Verify before trust.** No plan or critique passes between agents without every citation surviving the Verifier Gate.
- **Human-in-the-loop for consequences.** Computer-use actions are gated behind IMU-nod or R1-ring confirmation.

## 2. Component map

| Component | Package | Responsibility |
|---|---|---|
| G2 transport | `joat/ble` | Connect both lens peripherals, 7-packet auth replay, heartbeat, reconnect, host handoff |
| Audio pipeline | `joat/audio` | Ingests 16 kHz mono PCM, VAD/wake word, STT. **Relay path: the Hub SDK delivers PCM already decoded** — LC3 decode (`liblc3`, `0xF1`/NUS, mic enable `[0x0E, 0x01]`) applies only to the direct-BLE path |
| HUD renderer | `joat/display` | 576×288 canvas, text containers, cards, differential page updates |
| Reasoning | `joat/services`, `joat/agents` | Orchestrator: route utterances to the mind — `SeatSwitch` over `ClaudeCliReplyEngine` / `CodexCliReplyEngine` (the official CLIs on the wearer's subscriptions, spawned through the same `SubprocessAgent` as the Brainstorm seats, the belt offered over MCP) with the resident MLX engine as the offline seat — or Brainstorm (planning path; `debate` remains a compatibility trigger). `AgentEndpoint` serves the same mind to Even AI's Agent Configure and the belt to the CLIs at `/mcp` |
| Brainstorm agents | `joat/agents` | Subscription-authenticated `SubprocessAgent` adapters for `codex exec -s read-only` and `claude -p`, plus the Verifier Gate and JOAT synthesis; Hermes/OpenClaw are a separate future Agent Terminal boundary |
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

## Walking/cycling navigation

Navigation is an explicit, foreground-only walking/cycling capability. It is
enabled when the Mac has a `MAPBOX_ACCESS_TOKEN` (or legacy `MAPBOX_API_KEY`);
otherwise an explicit navigation request produces a wearer-visible unavailable
screen instead of falling through to the general assistant.

```text
spoken destination
  → Mac navigation session
  → Mapbox Search Box candidate search
  → canonical place/address confirmation on G2
  → explicit Walk or Cycle choice
  → fresh phone origin
  → Mapbox Directions route + complete geometry
  → Mapbox Static Images tiles, prefetched ALONG the route (every ~180 m)
  → phone-local progress matcher; the guidance page follows the wearer
  → semantic HUD state + content-free receipt to Mac
```

The cloud boundary is intentional and must be visible in product disclosure:
Mapbox receives the spoken destination plus approximate search proximity on
the Mac in order to produce canonical candidates. Canonical confirmation
therefore happens after search but before directions. Once a route is
installed, high-rate fixes stay in the plugin; the Mac receives semantic state
only. A precise coordinate leaves the phone again only for the fresh route
origin or after sustained off-route evidence requests a reroute. The street
tiles under the guidance page respect the same boundary: the Mac fetches
them along the route's geometry as soon as the route is known (a centre
every ~180 m of road, at most 24 per route, the origin tile first) and the
plugin draws the one nearest the wearer, so the map follows them without a
live position ever reaching the Mac. A position-only repaint of the
guidance page (the dot, the progress bar, the ETA; at most once a second)
sends nothing; only a semantic state change does.

A route keeps running behind every other page. While its own page (or Home)
is on the glass a progress frame repaints it; while a conversation, a
terminal, a list, a reader, an answer, a question or a decision gate is in
front, the frame updates the retained page and the header's turn chip and
never takes the glass (`HudPresenter.updateNavigation`). The pages that ask
the wearer something — the place, the mode, the preview, an error — still
take it. Until 2026-10-05 every frame went through the page-taking path, so
with once-a-second repaints a conversation or a Pomodoro page lasted until
the wearer's next step.

Saved places are the one exception to "spoken destination": the wearer
saves Home, Work and the rest on the phone page, the phone sends the list
whole to the Mac (`places` uplink, kept in `saved_places`), and "navigate
home" resolves on the Mac to the saved coordinates without a provider
search, or searches the saved address when that is all the place has. The
Navigation page lists them first, one tap from a route.

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

## 5. Durable Brainstorm conversation with Verifier Gate

Brainstorm is a wearer-initiated, steerable, potentially multi-round analysis
session around a topic, task, claim, problem, or evolving discussion. `debate`
remains a legacy voice alias, not the product or storage model. A durable local
Brainstorm record owns its session ID, title, branch lineage, ordered turns,
verification state, JOAT synthesis, and `active` / `settled` / `cancelled`
status. Opening Brainstorm restores the active conversation or presents recent
sessions plus `NEW`; it does not manufacture a new room every time.

JOAT is the glasses brain and session authority. The glasses speak to JOAT,
not directly to either CLI. JOAT interprets explicit session controls, owns
context and turn order, invokes the chosen collaborator, streams the result to
the wearer, applies verification/repair, records the synthesis, and restores
the semantic HUD after reconnect. Codex and Claude are bounded collaborators
inside that JOAT-owned loop.

Only the user's existing CLI subscriptions participate. Neither provider owns
a permanent opening slot: an explicit wearer direction chooses the next
speaker, and otherwise the session's recorded turn policy chooses without
changing the durable conversation identity.

```
G2 voice -> JOAT session/controller -> next collaborator selected from:
       - codex exec -s read-only  (subscription auth; cli_env.STRIP_ENV dropped)
       - claude -p                (subscription auth; cli_env.STRIP_ENV dropped)
     -> wearer-directed or continued collaborator turns
     -> JOAT VERIFY / bounded repair -> JOAT synthesis -> G2 HUD
```

Four properties are structural rather than policy, so a refactor cannot
quietly drop them:

- **One at a time.** Each speaker's prompt CONTAINS the transcript so far, so
  turn N+1 has no input until turn N exists. Nothing enforces sequencing —
  it is the only thing the data allows.
- **Two subscription CLIs, no remote-agent admission.** Codex CLI and Claude
  Code are the only collaborators. Missing either binary produces a visible
  unavailable/failed turn; Hermes, OpenClaw, endpoint health, tool policy, and
  Agent Terminal state never appear in the Brainstorm wire contract and never
  decide whether Brainstorm may start.
- **A failure is visible but never quotable.** A failing CLI prints its
  complaint on *stdout* (`claude -p` exits 1 and says "You're out of usage
  credits"), so only the exit code separates an answer from an error. A
  turn carrying a `[!]` failure notice streams to the wearer and is then
  excluded from the transcript — otherwise the next speaker rebuts a
  billing notice and the judge scores it.
- **Context changes are explicit.** Ordinary speech is the next user move in
  the selected conversation. `new Brainstorm`, `branch this`, and `continue
  <title>` are explicit lineage operations. A clearly unrelated replacement
  request requires confirmation; silence never resets a session.

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
the dense glasses transcript and the local console. The durable Brainstorm
store is the restart-safe source of truth for recent sessions, continuation,
branching, verification, and synthesis. G2 acceptance evidence contains only
synthetic content, bounded state transitions, gesture metadata, and frame
hashes; it never includes Brainstorm transcript text.

OpenClaw and Hermes remain separate from this section. Their future glasses
surface is Agent Terminal, with its own wire types, controller, session roster,
runtime authority, and acceptance gates. See
[`AGENT_TERMINAL_ROADMAP.md`](AGENT_TERMINAL_ROADMAP.md). Calendar proposal
lanes and other OpenClaw integrations retain their existing independent safety
boundaries; none grants Brainstorm remote-agent or tool authority.

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

### Whose clock is it — the phone's

Every time string the wearer READS is formatted on the Mac; the banner clock
beside it is `new Date()` in the phone's WebView. Those were two independent
defaults (`/etc/localtime` and the phone's zone), and nothing reconciled
them — so travelling made "Design review · 4:30" unreadable against a
09:41 banner, and *how long until* became uncomputable.

The phone is the authority, because the wearer is where the phone is. It
sends its IANA zone (`Intl.DateTimeFormat().resolvedOptions().timeZone`; the
Even Hub SDK exposes no timezone of its own) on `hello` **and with every
position fix** — landing somewhere is a position change, so the correction
rides the message that already reports it instead of waiting for a
reconnect. `core/wearer_zone.WearerZone` holds it, falling back to this
Mac's zone until a phone speaks.

Two zones, because two questions are being asked:

- `current()` — the wearer's. **Timed** events and reminders convert into it:
  an instant should be rendered where it will be read.
- `home()` — this Mac's, which is where the calendar's **all-day** entries
  were authored. An all-day event is a DATE, not an instant; converting its
  midnight slides it across another midnight and silently moves it a whole
  day. Date-only reminders are anchored the same way.

The agent's spoken clock (`agents/grounding.local_clock`) reads the same
source, and names both zones while they differ. Moving the screen without
moving the voice would have had JOAT *say* one time and *show* another —
worse than the original bug, because nothing on screen would contradict it.

## 7. Known caveats

- Protocol details are community-reverse-engineered; validate the auth bytes against `men-g2-ble-gateway`'s implementation before writing our own transport.
- iPhone Mirroring: no camera/mic mirroring, DRM blanks, EU-unavailable — never the vision pipeline (use SDK/companion camera + Qwen3-VL).
- iOS State Restoration dies on swipe-kill or BT toggle; design assumes the G2 stream drives background wakeups.
