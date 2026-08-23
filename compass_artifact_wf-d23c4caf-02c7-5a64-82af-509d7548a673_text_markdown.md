# J.A.R.V.I.S. for Even Realities G2 — Plan, Architecture & Interactive Artifact

## TL;DR
- **Every prior "limit" is an engineering problem with a verified path.** Combining the always-on Mac Studio (the brain: local LLM, STT, computer-use, the debate CLIs, and the Verifier Gate) with the iPhone (either a foregrounded Even Hub relay or a custom background-BLE companion) removes each constraint — nothing has to be left out. The two genuinely hard problems (keeping the BLE link alive when the phone is locked, and the Claude critic's Terms of Service) both have concrete, safe solutions.
- **The debate loop is CLI-only and gated by a source verifier.** Codex CLI (`codex exec --json --output-schema`) proposes, Claude Code CLI (`claude -p --output-format json`) critiques, a local MLX model judges — and between every hop a Python **Verifier Gate** fetches each cited URL, checks a verbatim quote against the live page (with Wayback + headless fallbacks), runs an NLI claim-support check, and **denies** any submission whose citations can't be verified, returning a structured repair request.
- **The interactive React artifact (below) is the primary deliverable**: a dark, HUD-green, phone-responsive component with seven sections (Overview system map, Modes with auto-handoff, animated Flows, a simulatable Debate Loop with the JSON schema + verifier checklist, filterable Capabilities, a phased Roadmap, and Risks & Decisions), with every fact embedded in its data structures.

---

## Key Findings

**Backgrounding is solvable two ways.** iOS `UIBackgroundModes: bluetooth-central` plus Core Bluetooth **State Preservation & Restoration** (`CBCentralManagerOptionRestoreIdentifierKey`) lets a sideloaded Swift companion keep the G2 link alive and be relaunched on BLE events even after the system terminates it — Apple's own docs state the system "relaunches your app into the background." The continuous G2 microphone stream (LC3 notifications) is peripheral-driven traffic, which is exactly what keeps such an app resident. The cheaper path is to run the official Even Hub app foregrounded (Guided Access + Auto-Lock Never + plugged in), optionally on a dedicated spare iPhone appliance.

**The G2 BLE protocol is open and permissive.** Per the reverse-engineering community (i-soxi/even-g2-protocol; MentraOS `G2.kt` ported into gpsnmeajp/men-g2-ble-gateway), the G2 uses "standard BLE, no special pairing," authenticated by a **fixed 7-packet handshake** that establishes a session. There is no evidence of device-bound challenge-response, so any host that replays the sequence connects — a generic M5AtomS3 microcontroller and a plain PC both authenticate. Each lens is a **separate BLE peripheral** (left/right), and the glasses accept **one central at a time**, so Mac↔phone handoff must be disconnect → re-scan → reconnect both lenses → replay 7-packet auth + init → resend page state.

**The Claude critic ToS has a clean answer.** Anthropic's Legal & Compliance page (updated Feb 19, 2026; enforced from April 4–5, 2026) prohibits reusing Free/Pro/Max **OAuth tokens** in third-party tools or the Agent SDK — but running the genuine `claude` binary (including headless `claude -p`) is Anthropic's own product and remains supported. Orchestrate by invoking that binary directly; keep tokens out of any other client. A metered API key with `--max-budget-usd` is the unambiguous fallback.

**iPhone Mirroring makes the Mac able to drive any iOS app**, and BlackHole makes the G2 mic usable as a call microphone. Both are proven with off-the-shelf open-source components.

---

## Details

### Part 1 — "No limits": how Mac + iPhone remove each constraint

**1. Custom iPhone companion owning the BLE link in the background — FEASIBLE (Medium).** APIs: CoreBluetooth with `bluetooth-central` background mode and State Preservation/Restoration; a Swift G2 driver (`ffs-os` clean-room driver, or a Swift port of MentraOS `G2.kt`). MentraOS shipped **full Even Realities G2 support on iOS and Android** (announced May 22, 2026) and can serve as the entire phone layer if you don't want to write a driver. Real constraints (from Apple docs/QA1962/TN3115): a user swipe-kill or a Bluetooth power-toggle disables restoration; there are no timer-based background wakeups (the peripheral must drive traffic); background throughput is reduced. Sideloading: free Xcode 7-day signing, paid developer signing, TestFlight, AltStore/SideStore, or EU alternative distribution. Cost of this mode: while the custom host holds the link, native Even features (Even AI, navigation, translate, teleprompter, R1 ring) are unavailable — but re-pairing to the Even app restores them quickly.

**2. Phone as always-awake relay appliance — FEASIBLE (High).** The Even Hub app is a WebView + WebSocket + mic capture; its documented weakness is that it is suspended when the phone locks or backgrounds. Guided Access, Auto-Lock = Never, plugged in, Low Power Mode off keeps it foregrounded indefinitely. A spare iPhone dedicated to this role avoids disrupting the main phone. Note: WebSocket domains must be listed in `app.json` (exact match, no wildcards) or Even Hub silently blocks them.

**3. Glasses roaming / host handoff — FEASIBLE (Medium).** Confirmed: separate peripheral per lens, single central at a time, and the G2 won't advertise while connected to the phone (men-g2-ble-gateway explicitly requires disconnecting the phone first). Automatic handoff logic: at the desk use Mac direct-BLE (CoreBluetooth) for lowest latency; away, use the phone. The switch is a protocol sequence — disconnect current central, re-scan both lens addresses, reconnect, replay the 7-packet auth + init (time-sync, pipe-role, heartbeat), then resend the current page state.

**4. Mac operating the iPhone via iPhone Mirroring — FEASIBLE (proven).** macOS Sequoia 15+/Tahoe 26 iPhone Mirroring window is capturable via **ScreenCaptureKit**; open-source MCP servers `jfarcand/iphone-mirroir-mcp` and `nickatnight96/iphone-mirror-mcp` drive a real iPhone using Apple Vision OCR to locate UI elements and a **Karabiner DriverKit** virtual keyboard/mouse for input. Requirements: same Apple Account with two-factor auth, iPhone locked and within ~10 m, Mac not using AirPlay/Sidecar/Internet Sharing. Limits (verified): camera/mic are not mirrored; **DRM content captures black** (hardware-enforced); Face ID/Control Center/hardware buttons are unreachable; pinch/rotate can't be synthesized; the feature is unavailable in the EU. iPhone notifications also surface in macOS Notification Center even without an active mirroring session, so triaged alerts can be scraped and forwarded to the HUD.

**5. Vision through the phone — FEASIBLE (High).** The Even Hub SDK exposes the phone camera and album picker (device-features) with user permission; a custom companion adds continuous camera. Flow: phone photo → local **Qwen3-VL** on MLX (or cloud) → answer on the HUD.

**6. Calls with the G2 mic — FEASIBLE (High).** Route the decoded G2 PCM into **BlackHole** (free, zero-latency virtual audio driver; GPL-3.0) and select BlackHole as FaceTime's input; send caller audio to AirPods paired to the Mac; show a live transcript + suggested replies on the HUD. Answer/decline programmatically via the macOS 26 Phone app through Accessibility scripting, App Intents, or Shortcuts (verify exact intents per macOS build). Handle consent tones for recording.

**7. Always-listening — FEASIBLE (High).** Wake-word + VAD on the Mac over the continuous stream. Mic enable command is `[0x0E, 0x01]`; LC3 frames arrive over the Nordic UART Service (0xF1 prefix) and decode via `liblc3` to 16 kHz mono 16-bit PCM.

**8. Location & navigation — FEASIBLE (Medium).** `navigator.geolocation` inside the WebView (declare `location` permission; note it works only when run as a published Hub plugin, not via QR sideload), or a companion app; the `appsbridge` project exposes the phone's magnetometer/step-count/GPS over a local WebSocket for compass and navigation cards.

**9. Other unlocks.** iMessage/SMS through the Mac's Messages (Text Message Forwarding); Shortcuts automations, HomeKit control, and Focus modes triggered from the Mac; AirPods/Apple Watch as clearly optional extras.

*Display note:* the Even Hub SDK's drawable canvas is documented as **576×288** monochrome green (per the Even Hub quickstart and `TextContainerProperty` dimensions), which is what all app code targets. Even Realities' marketing lists a higher native panel spec, and the third-party `g2flash` custom firmware adds a "full-screen 576×288 single-image" mode — treat 576×288 as the canvas you build against.

### Part 2 — The debate loop with a mandatory source-verification gate (CLI-only)

**Codex CLI (Proposer).** `codex exec` is the non-interactive mode; `--json` emits a JSONL event stream (which includes `web_search` items so you can audit which sources were touched), `-o/--output-last-message` writes the final message to a file, and `--output-schema schema.json` validates output against a JSON Schema. Web search is controlled by `web_search = "live" | "cached" | "disabled"` in `~/.codex/config.toml` (or a `--search`-style flag); sandbox via `--sandbox read-only|workspace-write|danger-full-access`; profiles via `--profile`. Latest tracked release is **v0.148.0 (Aug 18, 2026)**. OpenAI **permits** ChatGPT-subscription authentication in the Codex CLI.

**Claude Code CLI (Critic).** `claude -p` supports `--output-format json|stream-json`, `--json-schema`, `--allowedTools "WebSearch,WebFetch"`, `--max-turns`, `--max-budget-usd`, `--append-system-prompt`, `--permission-mode`, and session resume. **ToS status:** OAuth-token reuse in third-party tools/Agent SDK is prohibited (Legal & Compliance page, Feb 19, 2026; enforced April 4–5, 2026), but invoking the official `claude` CLI is allowed. Importantly, the planned **June 15, 2026 change that would have moved `claude -p`/Agent SDK usage to a separate metered credit was *paused* on its effective date** — Anthropic's Help Center states: "We're pausing the changes… For now, nothing has changed: Claude Agent SDK, `claude -p`, and third-party app usage still draw from your subscription's usage limits." (The previously announced tiers were $20 Pro / $100 Max 5x / $200 Max 20x, but they were never activated.) **Recommendation:** orchestrate by spawning the official `claude` binary directly; if you want zero ambiguity for an always-on setup, use a metered API key for the critic with a `--max-budget-usd` cap.

**Plan/critique JSON schema (with citations).**
```
{ plan_id, round, role, goal,
  steps: [{ id, text, claims: [{ claim_id, text, citation_ids[] }] }],
  citations: [{ id, url, title, quote, accessed_at, type }],
  open_questions[], verdict }
```
Rule: every non-trivial claim needs ≥1 citation, and every citation must carry a verbatim `quote`.

**Verifier Gate service (Python, on the Mac).** For each citation: (1) URL syntax + domain check; (2) HTTP fetch with retries, falling back to a headless browser or reader services (r.jina.ai / Firecrawl / Crawl4AI / trafilatura) for 403 or JS-rendered pages; (3) Wayback Machine availability API + Save-Page-Now for reproducibility; (4) normalized fuzzy quote-in-page match; (5) claim-support check with a local NLI/LLM judge (DeBERTa-NLI or the local MLX model); (6) domain reputation/recency; result `{verified, partially_verified, unverifiable}` with reasons. **Deny** if any required citation is unverifiable, return a structured repair request to the author CLI, cap retries, and log content hashes for audit. Reuse existing research/tooling: **FActScore, VeriScore, VeriFastScore, MiniCheck** (decompose→retrieve→verify), **AutoAIS/AIS** attribution, **RefChecker** (existence checks via Crossref/OpenAlex/Semantic Scholar), the **Anthropic Citations API** pattern (grounded verbatim quotes), and grounded-search APIs (Exa, Tavily, Firecrawl, Perplexity Sonar).

**Loop state machine.** Proposer (Codex) → Verifier Gate → Critic (Claude) → Verifier Gate → Judge (local MLX, free) → converge or next round. Convergence = no blocking issues + judge agreement above a threshold (e.g., 0.85); cap max rounds. Progress streams to the HUD ("Round 2/4: 3 citations failed, resubmitting…") and the whole thing runs as an OpenClaw skill.

---

## The Interactive Visual (React artifact — primary deliverable)

Single self-contained default-export component. Dark HUD/monochrome-green style, responsive to ~390 px, seven keyboard-accessible sections, all facts embedded.

```jsx
import React, { useState } from "react";
import {
  Glasses, Smartphone, Cpu, Brain, Mic, MousePointerClick,
  Terminal, ShieldCheck, MessageSquare, Network,
  CheckCircle2, XCircle, RefreshCw, ChevronRight, Zap, AlertTriangle
} from "lucide-react";

const GREEN = "#39FF88";
const nodes = [
  { id: "g2", icon: Glasses, name: "Even Realities G2", role: "HUD + mic + touch + IMU + R1 ring. No speaker, no camera.",
    tech: "576x288 SDK green canvas; mic LC3 (0xF1/NUS) -> 16kHz mono PCM; enable [0x0E,0x01]; each lens a separate BLE peripheral; 7-packet auth (replayable, no bonding crypto).",
    caveat: "One central at a time per lens; on-device firmware ECDSA-P256 signed (custom fw blocked; g2flash claims a workaround)." },
  { id: "iphone", icon: Smartphone, name: "iPhone (companion / relay)", role: "Owns BLE link OR runs Even Hub relay; Continuity for calls.",
    tech: "CoreBluetooth bluetooth-central + State Preservation/Restoration; ffs-os Swift driver / MentraOS iOS (full G2 support May 2026); camera+GPS via SDK.",
    caveat: "Swipe-kill or BT toggle stops restoration; Even Hub app suspends when locked/backgrounded." },
  { id: "mac", icon: Cpu, name: "Mac Studio (always-on brain)", role: "Runs the agent, local LLM, STT, computer-use, CLIs, Verifier Gate.",
    tech: "Apple Silicon; MLX; direct-BLE via CoreBluetooth when at desk.", caveat: "Must be reachable on LAN for phone relay." },
  { id: "openclaw", icon: Network, name: "OpenClaw gateway", role: "The brain: channels, MCP, cron, skills.",
    tech: "Gateway ws://127.0.0.1:18789; iMessage/WhatsApp/Telegram/Signal/Slack/email.", caveat: "Use Codex OAuth or API keys, not Claude OAuth." },
  { id: "mlx", icon: Brain, name: "Local MLX LLM", role: "Free local reasoning + Judge role in debate.",
    tech: "MLX + Qwen3-class MoE; Qwen3-VL for vision.", caveat: "Quality below frontier for hardest tasks." },
  { id: "stt", icon: Mic, name: "STT pipeline", role: "Continuous transcription + wake word/VAD.",
    tech: "WhisperKit (Neural Engine) or Parakeet-TDT v3 (MLX, 6.34% avg WER); liblc3 decode.", caveat: "Parakeet = 25 EU langs; Whisper = 99 langs." },
  { id: "cua", icon: MousePointerClick, name: "Cua computer-use", role: "Drives Mac + iOS apps in background.",
    tech: "trycua cua-driver (Rust, MCP/CLI); Lume VM sandbox; iPhone Mirroring + OCR + Karabiner.", caveat: "OSWorld-Verified ~72-75% for frontier agents; OSWorld 2.0 only ~20%." },
  { id: "codex", icon: Terminal, name: "Codex CLI (Proposer)", role: "Proposes plans with citations.",
    tech: "codex exec --json --output-schema; web_search=live; v0.148.0.", caveat: "ChatGPT-subscription auth permitted by OpenAI." },
  { id: "claude", icon: Terminal, name: "Claude Code CLI (Critic)", role: "Critiques plans with citations.",
    tech: "claude -p --output-format json --json-schema --max-budget-usd.", caveat: "Run official binary only; OAuth-in-3rd-party banned; subscription claude -p still draws from limits (June 15 change paused)." },
  { id: "gate", icon: ShieldCheck, name: "Verifier Gate", role: "Screens every citation; pass or deny+repair.",
    tech: "Python: fetch->Wayback->fuzzy quote match->NLI judge; FActScore/VeriScore/MiniCheck/RefChecker.", caveat: "JS/403 pages need headless/reader fallback." },
  { id: "channels", icon: MessageSquare, name: "Channels", role: "iMessage / Mail / Calendar / FaceTime / iPhone Mirroring.",
    tech: "Text Message Forwarding; BlackHole for call audio; Notification Center scrape.", caveat: "DRM blanks; camera/mic not mirrored; EU-unavailable." },
];

const modes = [
  { id: "relay", name: "Even Hub relay mode", feas: "High", unlocks: "Fastest to build; official SDK; keeps Even AI, nav, translate, teleprompter, R1.",
    costs: "Even Hub app suspends when phone locks/backgrounds; WebSocket domain must be app.json-whitelisted (exact match)." },
  { id: "companion", name: "Custom iPhone companion (background BLE)", feas: "Medium", unlocks: "Background BLE via State Restoration; continuous mic + display when locked.",
    costs: "Loses native Even features while it holds the link; sideload signing needed; swipe-kill stops it." },
  { id: "macble", name: "Mac direct-BLE mode", feas: "High", unlocks: "No phone needed at the desk; lowest latency; full protocol control.",
    costs: "Only in Bluetooth range of the Mac; must disconnect phone first (single central)." },
];

const flows = {
  "voice-hud": ["Long-press temple -> mic enable [0x0E,0x01]", "LC3 frames stream (0xF1/NUS)", "liblc3 decode -> 16kHz PCM", "WhisperKit/Parakeet STT", "OpenClaw + MLX reason", "textContainerUpgrade -> HUD reply"],
  "notes": ["Speak note", "STT transcript", "MLX structures + tags", "Save to notes store / Obsidian", "Confirmation card on HUD"],
  "compute": ["Voice command", "Codex/Claude plan via debate", "Cua drives Mac in Lume VM", "Nod (IMU) or R1 ring to confirm", "Result summary on HUD"],
  "call": ["Incoming call (Continuity)", "G2 PCM -> BlackHole -> FaceTime input", "Caller -> AirPods on Mac", "Live transcript + suggested replies on HUD", "Answer/decline via App Intents/Shortcuts"],
  "triage": ["iPhone notif -> macOS Notification Center", "Scrape + MLX triage by priority", "Urgent -> HUD card", "Reply via Messages / Mirroring"],
  "eyes": ["Point phone, ask", "SDK/companion captures photo", "Qwen3-VL (MLX) or cloud vision", "Answer on HUD"],
  "control": ["Ask for an app action", "iPhone Mirroring window (ScreenCaptureKit)", "Vision OCR finds element", "Karabiner virtual tap/type", "State read back to HUD"],
};

const capabilities = [
  { name: "Talk to agent, read reply on HUD", feas: 5, wow: 4, path: "Relay or Mac direct-BLE + STT + MLX" },
  { name: "Background BLE when phone locked", feas: 3, wow: 5, path: "Custom companion: State Restoration" },
  { name: "Phone calls with G2 mic + teleprompter", feas: 4, wow: 5, path: "BlackHole + Continuity + App Intents" },
  { name: "Operate the Mac by voice", feas: 4, wow: 5, path: "Cua driver + Lume sandbox" },
  { name: "Control any iPhone app", feas: 4, wow: 5, path: "iPhone Mirroring + OCR + Karabiner" },
  { name: "Phone camera as eyes", feas: 4, wow: 4, path: "SDK camera -> Qwen3-VL" },
  { name: "Message + email triage to HUD", feas: 4, wow: 4, path: "Notification Center + Messages + Mail" },
  { name: "Always-listening wake word", feas: 4, wow: 4, path: "VAD over continuous LC3 stream" },
  { name: "Turn-by-turn nav cards", feas: 3, wow: 3, path: "navigator.geolocation / appsbridge" },
  { name: "Cited planning debate w/ verifier gate", feas: 4, wow: 5, path: "Codex + Claude CLIs + Verifier Gate" },
  { name: "Mac<->phone auto host handoff", feas: 3, wow: 4, path: "Disconnect->reconnect->re-auth" },
  { name: "HomeKit / Shortcuts / Focus", feas: 4, wow: 3, path: "Mac-triggered automations" },
];

const roadmap = [
  { phase: "Phase 0 (this week)", items: ["men-g2-ble-gateway on the Mac (stock fw v2.2.20)", "WhisperKit STT + MLX reply -> HUD", "Prove voice->HUD loop"], metric: "Round-trip voice->HUD < 3s" },
  { phase: "Phase 1", items: ["OpenClaw brain + channels (iMessage/Mail)", "Message triage to HUD"], metric: "Triaged alert latency < 5s" },
  { phase: "Phase 2", items: ["Codex+Claude debate loop", "Verifier Gate v1 (fetch+Wayback+fuzzy quote)"], metric: "0 unverified citations pass" },
  { phase: "Phase 3", items: ["Cua computer-use + Lume", "Nod/ring confirmation"], metric: ">80% task confirm accuracy" },
  { phase: "Phase 4", items: ["BlackHole call routing + teleprompter", "iPhone Mirroring control"], metric: "Live call transcript on HUD" },
  { phase: "Phase 5", items: ["Custom iPhone companion (background BLE)", "Auto Mac<->phone handoff"], metric: "Locked-phone mic stream stays alive" },
];

const risks = [
  { q: "Claude critic ToS", rec: "Invoke the official `claude` binary directly (allowed); never reuse OAuth in another client. Subscription claude -p still draws from usage limits (June 15 metering change paused). Fallback: metered API key + --max-budget-usd." },
  { q: "Backgrounding strategy", rec: "Start with foregrounded Even Hub relay; graduate to a custom companion using CoreBluetooth State Restoration for locked-phone operation." },
  { q: "Host handoff", rec: "Single central per lens: automate disconnect->reconnect->re-auth (7-packet)->resend page state; prefer Mac direct-BLE at desk." },
  { q: "Privacy/security", rec: "Local-first (MLX/WhisperKit); consent tones for call recording; log content hashes; keep OAuth tokens out of third-party clients." },
  { q: "Firmware", rec: "Stay on stock firmware (ECDSA-P256 blocks custom images); use the app-layer BLE protocol, which needs no firmware mod." },
];

export default function JarvisG2() {
  const [tab, setTab] = useState("overview");
  const [sel, setSel] = useState(nodes[0]);
  const [mode, setMode] = useState(modes[0]);
  const [flow, setFlow] = useState("voice-hud");
  const [step, setStep] = useState(0);
  const [round, setRound] = useState(0);
  const [filter, setFilter] = useState("all");

  const tabs = [
    ["overview", "Overview"], ["modes", "Modes"], ["flows", "Flows"],
    ["debate", "Debate Loop"], ["caps", "Capabilities"], ["roadmap", "Roadmap"], ["risks", "Risks & Decisions"],
  ];

  const debateSteps = [
    { who: "Proposer (Codex CLI)", detail: "codex exec --json --output-schema plan.json; web_search=live", state: "run" },
    { who: "Verifier Gate", detail: "3 of 8 citations failed quote-match -> DENY, repair request", state: "deny" },
    { who: "Proposer (repair)", detail: "Resubmits with corrected citations + Wayback snapshots", state: "run" },
    { who: "Verifier Gate", detail: "All citations verified -> PASS", state: "pass" },
    { who: "Critic (Claude CLI)", detail: "claude -p --json-schema critique.json --allowedTools WebSearch,WebFetch", state: "run" },
    { who: "Verifier Gate", detail: "Critique citations verified -> PASS", state: "pass" },
    { who: "Judge (local MLX)", detail: "Agreement 0.91 > 0.85 threshold, no blocking issues -> CONVERGE", state: "pass" },
  ];

  const wrap = { background: "#050a07", color: "#d7ffe9", minHeight: "100vh", fontFamily: "ui-monospace, monospace" };

  return (
    <div style={wrap} className="p-3 sm:p-5">
      <header className="mb-4">
        <h1 className="text-lg sm:text-2xl font-bold" style={{ color: GREEN }}>J.A.R.V.I.S. // Even Realities G2</h1>
        <p className="text-xs sm:text-sm opacity-70">Mac + iPhone control center — no limits. Current: Aug 22, 2026.</p>
        <div className="flex flex-wrap gap-1 mt-3">
          {tabs.map(([k, label]) => (
            <button key={k} onClick={() => setTab(k)}
              className="px-2 py-1 text-xs rounded border"
              style={{ borderColor: GREEN, background: tab === k ? GREEN : "transparent", color: tab === k ? "#050a07" : GREEN }}>
              {label}
            </button>
          ))}
        </div>
      </header>

      {tab === "overview" && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <div className="md:col-span-2 grid grid-cols-2 sm:grid-cols-3 gap-2">
            {nodes.map((n) => {
              const I = n.icon;
              return (
                <button key={n.id} onClick={() => setSel(n)}
                  className="p-2 rounded border text-left"
                  style={{ borderColor: sel.id === n.id ? GREEN : "#1c3a2a", background: sel.id === n.id ? "#0c1f15" : "#080f0b" }}>
                  <I size={18} color={GREEN} />
                  <div className="text-xs mt-1 font-semibold">{n.name}</div>
                </button>
              );
            })}
          </div>
          <div className="p-3 rounded border" style={{ borderColor: GREEN, background: "#0c1f15" }}>
            <h3 className="font-bold text-sm" style={{ color: GREEN }}>{sel.name}</h3>
            <p className="text-xs mt-2"><b>Role:</b> {sel.role}</p>
            <p className="text-xs mt-2"><b>Tech/repos:</b> {sel.tech}</p>
            <p className="text-xs mt-2" style={{ color: "#ffd27f" }}><b>Caveat:</b> {sel.caveat}</p>
          </div>
        </div>
      )}

      {tab === "modes" && (
        <div>
          <div className="flex flex-wrap gap-1 mb-3">
            {modes.map((m) => (
              <button key={m.id} onClick={() => setMode(m)} className="px-2 py-1 text-xs rounded border"
                style={{ borderColor: GREEN, background: mode.id === m.id ? GREEN : "transparent", color: mode.id === m.id ? "#050a07" : GREEN }}>
                {m.name}
              </button>
            ))}
          </div>
          <div className="p-3 rounded border" style={{ borderColor: GREEN, background: "#0c1f15" }}>
            <h3 className="font-bold text-sm" style={{ color: GREEN }}>{mode.name} — feasibility: {mode.feas}</h3>
            <p className="text-xs mt-2"><b>Unlocks:</b> {mode.unlocks}</p>
            <p className="text-xs mt-2" style={{ color: "#ffd27f" }}><b>Costs:</b> {mode.costs}</p>
          </div>
          <div className="mt-3 p-3 rounded border text-xs" style={{ borderColor: "#1c3a2a" }}>
            <b style={{ color: GREEN }}>Auto handoff:</b> At the desk -> Mac direct-BLE (lowest latency). Away -> phone relay/companion.
            Switch = disconnect current central, re-scan both lenses, replay 7-packet auth + init, resend page state. Only one central per lens.
          </div>
        </div>
      )}

      {tab === "flows" && (
        <div>
          <div className="flex flex-wrap gap-1 mb-3">
            {Object.keys(flows).map((f) => (
              <button key={f} onClick={() => { setFlow(f); setStep(0); }} className="px-2 py-1 text-xs rounded border"
                style={{ borderColor: GREEN, background: flow === f ? GREEN : "transparent", color: flow === f ? "#050a07" : GREEN }}>
                {f}
              </button>
            ))}
          </div>
          <div className="space-y-1">
            {flows[flow].map((s, i) => (
              <button key={i} onClick={() => setStep(i)} className="w-full text-left p-2 rounded border text-xs flex items-center gap-2"
                style={{ borderColor: i <= step ? GREEN : "#1c3a2a", background: i <= step ? "#0c1f15" : "#080f0b" }}>
                <span style={{ color: GREEN }}>{i + 1}</span><ChevronRight size={12} />{s}
              </button>
            ))}
          </div>
          <button onClick={() => setStep((step + 1) % flows[flow].length)} className="mt-3 px-3 py-1 text-xs rounded"
            style={{ background: GREEN, color: "#050a07" }}><Zap size={12} className="inline" /> Step</button>
        </div>
      )}

      {tab === "debate" && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div>
            <div className="space-y-1">
              {debateSteps.map((d, i) => {
                const active = i <= round;
                const color = d.state === "deny" ? "#ff6b6b" : d.state === "pass" ? GREEN : "#ffd27f";
                const Icon = d.state === "deny" ? XCircle : d.state === "pass" ? CheckCircle2 : RefreshCw;
                return (
                  <div key={i} className="p-2 rounded border text-xs" style={{ borderColor: active ? color : "#1c3a2a", background: active ? "#0c1f15" : "#080f0b" }}>
                    <div className="flex items-center gap-2 font-semibold" style={{ color: active ? color : "#456" }}>
                      {active && <Icon size={13} />}{d.who}
                    </div>
                    {active && <div className="opacity-80 mt-1">{d.detail}</div>}
                  </div>
                );
              })}
            </div>
            <button onClick={() => setRound((round + 1) % debateSteps.length)} className="mt-3 px-3 py-1 text-xs rounded"
              style={{ background: GREEN, color: "#050a07" }}>Simulate a round</button>
            <div className="mt-2 text-xs p-2 rounded border" style={{ borderColor: "#1c3a2a" }}>
              HUD stream: "Round {Math.min(round + 1, 4)}/4: {round === 1 ? "3 citations failed, resubmitting…" : "verifying…"}"
            </div>
          </div>
          <div className="p-3 rounded border text-xs" style={{ borderColor: GREEN, background: "#0c1f15" }}>
            <b style={{ color: GREEN }}>Plan/critique JSON schema</b>
            <pre className="mt-1 whitespace-pre-wrap opacity-90">{`{ plan_id, round, role,
  goal,
  steps:[{ id, text,
    claims:[{ claim_id, text, citation_ids[] }] }],
  citations:[{ id, url, title, quote,
    accessed_at, type }],
  open_questions[], verdict }`}</pre>
            <b style={{ color: GREEN }} className="block mt-2">Verifier checklist</b>
            <ul className="list-disc ml-4 mt-1 opacity-90">
              <li>URL syntax + domain check</li>
              <li>HTTP fetch (retry; headless/r.jina.ai/Firecrawl on 403/JS)</li>
              <li>Wayback availability + Save-Page-Now</li>
              <li>Normalized fuzzy quote-in-page match</li>
              <li>Claim-support NLI (local MLX / DeBERTa)</li>
              <li>Reputation + recency; log content hashes</li>
              <li>Any required citation unverifiable -> DENY + repair</li>
            </ul>
          </div>
        </div>
      )}

      {tab === "caps" && (
        <div>
          <div className="flex flex-wrap gap-1 mb-3">
            {["all", "high-wow", "quick-win"].map((f) => (
              <button key={f} onClick={() => setFilter(f)} className="px-2 py-1 text-xs rounded border"
                style={{ borderColor: GREEN, background: filter === f ? GREEN : "transparent", color: filter === f ? "#050a07" : GREEN }}>{f}</button>
            ))}
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            {capabilities.filter((c) => filter === "all" || (filter === "high-wow" && c.wow >= 5) || (filter === "quick-win" && c.feas >= 5)).map((c, i) => (
              <div key={i} className="p-2 rounded border text-xs" style={{ borderColor: "#1c3a2a", background: "#080f0b" }}>
                <div className="font-semibold" style={{ color: GREEN }}>{c.name}</div>
                <div className="opacity-80 mt-1">Path: {c.path}</div>
                <div className="mt-1">Feasibility {"■".repeat(c.feas)}{"□".repeat(5 - c.feas)} · Wow {"■".repeat(c.wow)}{"□".repeat(5 - c.wow)}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {tab === "roadmap" && (
        <div className="space-y-2">
          {roadmap.map((p, i) => (
            <div key={i} className="p-3 rounded border" style={{ borderColor: i === 0 ? GREEN : "#1c3a2a", background: i === 0 ? "#0c1f15" : "#080f0b" }}>
              <div className="font-bold text-sm flex items-center gap-2" style={{ color: GREEN }}>
                {i === 0 && <Zap size={14} />}{p.phase}{i === 0 && <span className="text-xs opacity-70">(build first)</span>}
              </div>
              <ul className="list-disc ml-4 text-xs mt-1">{p.items.map((it, j) => <li key={j}>{it}</li>)}</ul>
              <div className="text-xs mt-1" style={{ color: "#ffd27f" }}>Success: {p.metric}</div>
            </div>
          ))}
        </div>
      )}

      {tab === "risks" && (
        <div className="space-y-2">
          {risks.map((r, i) => (
            <div key={i} className="p-3 rounded border" style={{ borderColor: "#1c3a2a", background: "#080f0b" }}>
              <div className="font-semibold text-sm flex items-center gap-2" style={{ color: GREEN }}><AlertTriangle size={14} />{r.q}</div>
              <div className="text-xs mt-1 opacity-90"><b style={{ color: GREEN }}>Recommendation:</b> {r.rec}</div>
            </div>
          ))}
        </div>
      )}

      <footer className="mt-5 text-[10px] opacity-50 flex flex-wrap gap-3">
        <span>Legend: ■ = strength (1-5)</span><span style={{ color: "#ffd27f" }}>amber = caveat</span>
        <span style={{ color: "#ff6b6b" }}>red = deny</span><span style={{ color: GREEN }}>green = pass/HUD</span>
      </footer>
    </div>
  );
}
```

---

## Recommendations

1. **Build the voice→HUD loop this week (Phase 0).** Run `gpsnmeajp/men-g2-ble-gateway` on the Mac (it works on stock G2 firmware v2.2.20 — no firmware modification), wire WhisperKit (or Parakeet-TDT v3) STT into an MLX reply that renders via `textContainerUpgrade`. **Threshold to advance:** round-trip voice→HUD under ~3 s.
2. **Adopt Mac direct-BLE as the primary desk mode; add the Even Hub relay for mobility.** This gives you the fastest path to a working system without touching iOS signing. Defer the custom iPhone companion (background BLE via State Restoration) to Phase 5, once the rest works — that is the only piece that needs sideloaded Swift and is the highest-risk item.
3. **For the debate loop, invoke the official `claude` binary directly and keep OAuth tokens out of any other client.** Because the June 15, 2026 metering change was paused, subscription `claude -p` still draws from your normal limits today — but treat that as reversible: architect the critic so you can flip to a metered API key with `--max-budget-usd` without rewriting the loop. Codex-subscription auth is explicitly permitted, so the Proposer side is unambiguous.
4. **Ship the Verifier Gate before turning the loop autonomous (Phase 2).** Start minimal: fetch + Wayback snapshot + normalized fuzzy quote match; add the NLI claim-support judge second. **Threshold:** zero unverifiable citations ever pass. Reuse MiniCheck/VeriScore for the judge and RefChecker for any DOI/academic citations.
5. **Sequence the "wow" features by feasibility:** iPhone Mirroring control and BlackHole call-routing (Phase 4) are proven and high-impact; nav cards and multi-host auto-handoff are the shakiest (feasibility 3) and should come last.

**Signals that would change the plan:** if Apple restricts iPhone Mirroring window capture or synthetic input in a macOS update, fall back to Continuity/Shortcuts for iOS actions; if Anthropic un-pauses and enforces the Agent-SDK credit metering, switch the critic to a capped API key immediately; if a G2 firmware update changes the auth handshake, pin the working firmware version and re-derive the sequence from the community repos before updating.

---

## Caveats

- **Protocol byte-level details are community-reverse-engineered, not vendor-confirmed.** The "7-packet auth handshake" is documented by count and purpose (i-soxi/even-g2-protocol: "Standard BLE, no special pairing"), but the exact bytes of all seven packets are not fully published in accessible files; validate against `men-g2-ble-gateway`'s `dev_settings.py` before implementing. Custom on-device firmware is blocked by ECDSA-P256-signed images (Apollo510b bootloader holds the public key); the `g2flash` project *claims* custom firmware but its method could not be verified — do not rely on it.
- **The G2 display figure has two readings.** The Even Hub SDK drawable canvas is documented as 576×288 (build against this); Even Realities' marketing cites a higher native panel spec, and 576×288 full-screen single-image is specifically a `g2flash` custom-firmware feature. This does not affect the architecture.
- **Computer-use is capable but imperfect.** Frontier agents now score roughly 72–75% on OSWorld-Verified, but the harder OSWorld 2.0 benchmark tops out near ~20% — so always gate consequential Mac/iPhone actions behind an explicit nod (IMU) or R1-ring confirmation, and sandbox with Lume.
- **iPhone Mirroring has hard limits:** camera/mic aren't mirrored, DRM content blanks, it's EU-unavailable, and it needs the iPhone locked and nearby on the same Apple Account. It cannot be your vision pipeline (use the SDK/companion camera for that).
- **iOS backgrounding is not guaranteed.** State Restoration relaunches your app on BLE events but not after a user swipe-kill or a Bluetooth toggle, and there are no timer-based wakeups — the design depends on the G2's own stream driving traffic.
- **Version drift is a live risk.** Codex CLI (v0.148.0), Claude Code CLI flags, macOS 26/iOS 18+ behavior, and Anthropic's billing posture are all moving fast as of Aug 22, 2026; re-verify CLI flags with `--help` and the official docs before wiring them into automation. macOS 26 Phone-app answer/decline via App Intents/Shortcuts in particular should be confirmed on your exact build.