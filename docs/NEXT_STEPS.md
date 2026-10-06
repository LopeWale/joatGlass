# Next Steps

Ordered, with the reasoning. Everything here is Phase 0 unless noted.

## 1. Scaffold the real Hub plugin build — DONE

Build config (`package.json`, `vite.config.ts`, `tsconfig.json`) and `app.json` exist; the
manifest was generated with the official CLI (`evenhub init`, from
`@evenrealities/evenhub-cli` — the binary is `evenhub`/`eh`, not `even-hub`) and merged.
The whitelist carries both tailnet names (MacBook + mac mini, `ws://` and `wss://` each)
plus the MacBook's `.local` mDNS fallback; `VITE_MAC_WS_URL` dials the MacBook's tailnet
URL. Adding another Mac = one more whitelist pair + (if it should be the dial target)
the `.env` line — `scripts/setup_brain.sh` detects and prints exactly this when run on
a non-whitelisted machine.

## 2. Stand up the Mac side of the link — DONE

`joat/ble/hub_gateway.py` implements `GlassesTransport`; `joat/core/protocol.py`
mirrors `types.ts` (drift pinned by `tests/unit/test_protocol.py`). Round-trip proven
live against the simulator over the tailnet, including HUD text rendered from Python.

## 3. Test in the simulator before touching hardware — DONE

Tier 2 complete (see docs/TESTING.md, including the simulator findings: no IMU,
right-temple-only input, double-click = system exit).

## 4. Wire STT + MLX reply — WIRED; PHYSICAL LATENCY OPEN

`python -m joat` runs the core voice loop: mic → RmsVad → WhisperKitEngine →
transcript as `hud.text`. `./scripts/setup_brain.sh --service --warm` is only
the legacy Python-core deployment. An unattended glasses-first Brainstorm /
Agent Terminal Mini must run the installed **JOAT.app** with **Launch at Login**
so its app-owned native bridge can use the Mini's Keychain identity; see
`docs/MAC_APP.md`.
The voice loop is wired, but the earlier desk result did not survive current
physical use: roughly 15-second replies have been observed and the production-G2
metric is reopened.

**A lead on that, 2026-08-31.** Whisper serve had been FAILING to start, and
when it does the engine says so and falls back to the batch engine for the
rest of the session — the slower path, which is a strong candidate for the
15-second replies. From `~/Library/Logs/joat/brain.log`:

    2026-08-30 22:34:42 ERROR whisper serve failed to start:
                              serve not ready after 30.0s
    2026-08-31 22:38:43 ERROR whisper serve failed to start:
                              serve not ready after 30.0s
    2026-08-31 22:56:25 INFO  whisper serve ready at http://127.0.0.1:50060
                              (model large-v3_turbo)

Measured, not inferred: every start since has been ready in about FIVE
seconds, against a `DEFAULT_READY_TIMEOUT_S` of 30. A warm load has six
times the margin it needs; the two failures were both first-starts after a
long gap.

Inferred, and worth confirming before acting: that a COLD load — first run
after a reboot, or after the model cache is evicted — exceeds 30s, and that
the timed-out process keeps loading in the background, which is why the next
start finds it warm. If that holds, one cold start costs the whole session
its fast STT path, and the timeout is the wrong knob to be tight on: a
generous wait costs seconds once, while the fallback costs every utterance.

To confirm: evict the WhisperKit model cache, start the engine, and time the
first load. Do not raise the timeout on this reasoning alone — nobody has
watched a genuinely cold load. Phase 2 is in progress: debate loop with visible verifier
turns and repair, wake-word address gate, and the DebateLoop
convergence state machine (agreement >= 0.85, capped rounds). NLI claim-support (MiniCheck flan-t5-large, optional `nli` extra) now runs
inside the gate: a real quote propping up a misrepresenting claim gets visibly
downgraded ('claim support 0.12 — quote real but claim weakly supported').
Remaining in Phase 2: JSON plan/critique schemas once the debate graduates
from prose contracts. CAUTION for future installers: PyPI's 'minicheck' is an
UNRELATED formal-methods tool — the fact-checker installs from git (pinned in
pyproject's nli extra).

**Metric: round-trip voice → first lens-visible reply < 3 s — OPEN.** The
historical desk measurement was utterance-end → transcript **0.79–0.89 s** and
transcript → reply **0.51–0.55 s**, with an older 0.8-second callback-count VAD
hangover. It is retained as a historical Mac-side measurement, not current
simulator or physical-G2 proof.

The focused latency repair now keeps Whisper non-dropping while requesting one
text-only decode (temperature fallback count zero), measures the one-second VAD
hangover from actual PCM sample duration, preserves 200 ms messages when SDK
callbacks coalesce, and emits the first reply after eight characters before
returning to the normal 32-character batching. A local acceptance observer is
disabled by default and exposes only fixed stage names plus coarse latency
buckets; it retains no transcript, reply, frame, device, or exact timing data.
These changes need a fresh app restart and synchronized physical-G2 run before
the metric can be closed. Simulator, Mac process, bridge acknowledgement, and
physical lens evidence must remain separate.

**Phase 2 requirement (wearer-stated):** the debate must be LIVE-STREAMED to the HUD
turn by turn — visible as it happens, one exchange at a time — not a background job
that surfaces only a verdict. Visibility is the proof it was debated.
`HudPresenter.appendText` is the streaming primitive for this.

## 5. Calibrate motion before any IMU gesture can decide anything

The old `threshold = 0.35` / `windowMs = 1200` nod detector was removed:
`ImuMonitor` now treats motion as observational only, and confirmation is an
explicit temple/ring TAP (swipe or timeout refuses). That is the deliberate
fail-safe until real data exists. The calibration path is wired on both
sides as of 2026-10-05: the plugin relays every raw sample uplink (~10/s at
P100) once asked, and the Mac asks when started with `--imu-trace PATH`
(`joat/services/imu_trace.py`: `imu.control` at 100 ms plus `imu.trace` on
every hello, samples appended to PATH as JSON lines). Before that flag
existed nothing on the Mac ever sent those downlinks, so this section
described a path that could not run. Record labelled traces (nod /
head-shake / walking / nothing), derive thresholds from the data, and only
then consider promoting a motion gesture back into the confirm gate
(RULES.md #6).

## 6. The mind on the wearer's subscriptions, and the Even AI door — WIRED 2026-09-03

The diagnosis of 2026-09-02 (nine days of `brain.log`, plus a live benchmark
of the 3B model with the brain's own prompt): the mind was
`Llama-3.2-3B-Instruct-4bit` told to "say you do not have it", so it answered
"I do not have a joke." in 0.34 s; the text-level wake gate discarded 639
utterances against 29 replies; the 1 s VAD hangover split requests into
fragments of which only the one carrying "JOAT" was answered; and every
reply chunk repainted BLE image tiles at 0.5–2 s each. None of that is the
brain — the tools, channels, gateway and confirmation gates are the part
worth keeping. The mind and the brain are different seats.

A first version of the new mind called the Anthropic API on a metered key.
That was wrong for this project and was removed the same day: CLAUDE.md
says both CLIs run on subscriptions with no API keys, and RULES.md #9/#10
name the official `claude` and `codex` binaries as the only paths. What
stands now:

- `agents/cli_reply_engine.py` — two seats on the wearer's own plans:
  `ClaudeCliReplyEngine` (`claude -p --output-format stream-json
  --include-partial-messages`, streamed; `--session-id` then `--resume` for
  memory; `--tools ""` so the mind never holds a shell on the Mac) and
  `CodexCliReplyEngine` (`codex exec --json`, `resume <thread>` for memory).
  Both spawn through the Brainstorm seats' `SubprocessAgent`, dropping the
  shared credential list every CLI launch drops (`agents/cli_env.py`
  `STRIP_ENV`), in their own working directory so the repo's CLAUDE.md
  is not project context. `--bare` is not used: it ignores the OAuth login.
- `SeatSwitch` — the Assistant slot chooses the seat per turn ("use codex
  for the assistant"; `--agent claude|codex|mlx` sets the boot seat). A
  seat that is missing or signed out is skipped for the next one; the
  resident MLX model is the offline seat. Which model a seat runs is the
  existing Claude seat / Codex seat slot.
- The belt is offered to both CLIs over MCP: `services/agent_endpoint.py`
  serves `/mcp` (initialize, tools/list, tools/call) on loopback behind the
  same bearer token; Claude gets `--mcp-config … --strict-mcp-config`,
  Codex `-c mcp_servers.joat.url=… -c mcp_servers.joat.bearer_token_env_var=JOAT_AGENT_TOKEN`.
- The Even AI door is unchanged: `POST /v1/chat/completions` on loopback
  `:8788`, plain text cut for the lens. `status.json` now carries
  `accounts` (per-seat login state) and `seat` (the one answering).

To turn it on:

1. **Sign in once per CLI, in Terminal** — no keys anywhere:
   `claude auth login` (Max) and `codex login` (ChatGPT). `claude auth
   status` must say `"loggedIn": true`; on 2026-09-03 it did not until the
   login ran, while Codex was already signed in. The dashboard's `accounts`
   shows both states after a Restart.
2. **The token.** The first boot writes
   `~/Library/Application Support/JOAT/agent-token` (0600); `JOAT_AGENT_TOKEN`
   overrides it.
3. **Expose the door over the tailnet**, next to the glasses proxy. One
   script does the Mac half and checks each step (door up, token enforced,
   mount present, Funnel off, door answering through the tailnet), then
   copies the token to the clipboard and prints the URL for step 4:

       ./scripts/setup_even_ai_door.sh            # add --probe for one real question

   By hand, it is:

       tailscale serve --bg --https=443 --set-path=/agent http://127.0.0.1:8788

4. **On the phone** (Tailscale on): Even app → Settings → Even AI →
   Agent Configure → add an agent with
   `https://laptop-mac.example-tailnet.ts.net/agent/v1/chat/completions`
   and the token → select it as the active AI. Say "Hey Even, tell me a
   joke."

Through this door Even owns the wake word, speech-to-text and rendering; the
brain owns the mind and the tools. Questions, answers and reads go this way.
Actions that need the tap/ring confirmation — send a message, write a
calendar entry — stay on the plugin path, where the brain owns the gesture.

Measured 2026-09-03 on this Mac: `codex exec` 6–8 s per cold turn (it ships
~16k tokens of its own context); `claude -p` on the Max login ~3 s for a
one-word answer. Open: whether `--tools ""` also hides the MCP tools from
Claude (the first signed-in run with a tool question settles it); a
resident `--input-format stream-json` process to drop the per-turn spawn;
the Accounts section in the Mac app (login buttons and seat choice), which
needs Xcode to build.

**The Codex seat's tools, closed 2026-09-04.** `codex exec` refused every
MCP tool call with "MCP tool call requires approval, but approval policy is
never", because print mode has nobody to ask. The key is
`mcp_servers.joat.default_tools_approval_mode="approve"`, scoped to the
brain's own server; the session record confirms the turn still runs
`approval_policy: never` with `sandbox_policy: read-only`, so it removes the
per-tool gate without widening the sandbox. `-c features.plugins=false` went
in beside it: `--ignore-user-config` does not suppress plugin-sourced MCP
servers, and each turn was opening failing OAuth connections to the wearer's
Cloudflare and RunPod servers — two per turn measured, zero after.

Still open on the plugin path, in order of payoff: push-to-talk instead of
the text wake gate; native text (`textContainerUpgrade`) for the reply
stream instead of tiles; a speech engine that never silently falls back to
batch. The 1 s VAD split and the wake gate are what the button removes.

## Deferred deliberately

- **`men-g2-ble-gateway` / direct-BLE** — still the desk path, but the Hub plugin gets to a working system faster and with no reverse-engineering risk. Pick it up when relay latency actually annoys you.
- **`liblc3`** — not needed at all until direct-BLE.
- **Debate loop + Verifier Gate (Phase 2)** — `alecnielsen/adversarial-review` is the starting point; don't start until voice → HUD is solid.
- **iOS companion (Phase 5)** — the only piece needing sideloaded Swift, and the highest-risk item. Guided Access is the interim answer to the phone-locking problem.

## Open questions to resolve before Phase 1

- Does the Even Hub app tolerate a long-lived WebSocket while foregrounded, or does it throttle after some idle period? Test with a 30-minute session before designing around it.
- What's the actual sustained PCM throughput over the relay? 16 kHz × 16-bit mono is ~32 KB/s raw; the 200 ms batching in `AudioStreamer` may need tuning, and JSON number arrays are a wasteful encoding — consider base64 if it bites.
