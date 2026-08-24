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
Remaining: the local MLX model between flush() and send(). **That closes Phase 0.**

**Metric: round-trip voice → HUD < 3 s.** First live measurement: 7.1 s utterance-end
→ transcript (spoken through speakers → sim mic → tailnet → WhisperKit → HUD). Most of
that is the CLI cold-starting the model per utterance — a persistent WhisperKit process
(or Swift binding) is the known fix, not VAD or transport.

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
