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

`jarvis/ble/hub_gateway.py` implements `GlassesTransport`; `jarvis/core/protocol.py`
mirrors `types.ts` (drift pinned by `tests/unit/test_protocol.py`). Round-trip proven
live against the simulator over the tailnet, including HUD text rendered from Python.

## 3. Test in the simulator before touching hardware — DONE

Tier 2 complete (see docs/TESTING.md, including the simulator findings: no IMU,
right-temple-only input, double-click = system exit).

## 4. Wire STT + MLX reply — STT HALF DONE

`python -m jarvis` runs the voice loop: mic → RmsVad → WhisperKitEngine → transcript
as `hud.text`. Deploy to any Mac with `./scripts/setup_brain.sh --service --warm`.
Phase 0 CLOSED (metric met at ~2.2 s). Phase 2 in progress: debate loop with
visible verifier turns and repair, wake-word address gate, and the DebateLoop
convergence state machine (agreement >= 0.85, capped rounds). NLI claim-support (MiniCheck flan-t5-large, optional `nli` extra) now runs
inside the gate: a real quote propping up a misrepresenting claim gets visibly
downgraded ('claim support 0.12 — quote real but claim weakly supported').
Remaining in Phase 2: JSON plan/critique schemas once the debate graduates
from prose contracts. CAUTION for future installers: PyPI's 'minicheck' is an
UNRELATED formal-methods tool — the fact-checker installs from git (pinned in
pyproject's nli extra).

**Metric: round-trip voice → HUD < 3 s — MET.** With `whisperkit-cli serve`
(resident model, `WhisperServeEngine`): utterance-end → transcript **0.79–0.89 s**,
transcript → reply **0.51–0.55 s**; plus the 0.8 s VAD hangover ≈ **2.2 s total**
(was 8.4 s with the per-utterance CLI). Serve warms once at brain boot (~7 s) and
auto-respawns if it dies mid-session (verified by killing it live).

**Phase 2 requirement (wearer-stated):** the debate must be LIVE-STREAMED to the HUD
turn by turn — visible as it happens, one exchange at a time — not a background job
that surfaces only a verdict. Visibility is the proof it was debated.
`HudPresenter.appendText` is the streaming primitive for this.

## 5. Calibrate the nod thresholds

`ImuMonitor`'s `threshold = 0.35` and `windowMs = 1200` are guesses. Record real IMU traces at P100 for nod / head-shake / walking / nothing, then set the threshold from the data. Until that's done, treat the R1 ring tap as the only trustworthy confirm — RULES.md #6 depends on this being reliable.

## Deferred deliberately

- **`men-g2-ble-gateway` / direct-BLE** — still the desk path, but the Hub plugin gets to a working system faster and with no reverse-engineering risk. Pick it up when relay latency actually annoys you.
- **`liblc3`** — not needed at all until direct-BLE.
- **Debate loop + Verifier Gate (Phase 2)** — `alecnielsen/adversarial-review` is the starting point; don't start until voice → HUD is solid.
- **iOS companion (Phase 5)** — the only piece needing sideloaded Swift, and the highest-risk item. Guided Access is the interim answer to the phone-locking problem.

## Open questions to resolve before Phase 1

- Does the Even Hub app tolerate a long-lived WebSocket while foregrounded, or does it throttle after some idle period? Test with a 30-minute session before designing around it.
- What's the actual sustained PCM throughput over the relay? 16 kHz × 16-bit mono is ~32 KB/s raw; the 200 ms batching in `AudioStreamer` may need tuning, and JSON number arrays are a wasteful encoding — consider base64 if it bites.
