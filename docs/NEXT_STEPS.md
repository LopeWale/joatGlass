# Next Steps

Ordered, with the reasoning. Everything here is Phase 0 unless noted.

## 1. Scaffold the real Hub plugin build — DONE

Build config (`package.json`, `vite.config.ts`, `tsconfig.json`) and `app.json` exist; the
manifest was generated with the official CLI (`evenhub init`, from
`@evenrealities/evenhub-cli` — the binary is `evenhub`/`eh`, not `even-hub`) and merged.
The whitelist currently carries `ws://emmanuels-macbook-pro.local:8787` (this MacBook's real
mDNS name, LAN-only — fine for desk dev).

**Still open:** Tailscale is not installed yet. When it is, add the MagicDNS URL
(`ws://<machine>.<tailnet>.ts.net:8787`) to BOTH `app.json`'s network whitelist and
`VITE_MAC_WS_URL` in `apps/hub-plugin/.env` — and when the brain moves to the Mac Studio,
swap the hostname in the same two places.

## 2. Stand up the Mac side of the link

Minimal Python WebSocket server in `apps/mac/jarvis/ble/hub_gateway_transport.py` implementing `GlassesTransport`, speaking the message protocol in `apps/hub-plugin/src/types.ts`. Mirror those types in `jarvis/core` as dataclasses so the two ends can't drift.

Order inside this step: accept `hello` → log `device` → buffer `audio` frames → echo a `hud.text` back. Prove the round-trip with a hardcoded string before any STT exists.

## 3. Test in the simulator before touching hardware

The desktop simulator accepts `window.postMessage` pushes for audio, IMU, and device events. That makes `AudioStreamer`, `ImuMonitor`, and `EventRouter` unit-testable with synthetic PCM — do this before debugging on glasses, where you can't set breakpoints.

## 4. Wire STT + MLX reply

`SttEngine` adapter over WhisperKit (CLI or Swift binary) fed by the buffered PCM, then a local MLX model, then `hud.text` back down. **This closes Phase 0.**

**Metric: round-trip voice → HUD < 3 s.** Measure it before adding anything else.

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
