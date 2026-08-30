# apps/hub-plugin

Even Hub G2 plugin — the mobility path. Streams glasses mic PCM, IMU, and input events to the Mac brain over WebSocket; renders HUD replies via `textContainerUpgrade`.

See [../../docs/HUB_PLUGIN.md](../../docs/HUB_PLUGIN.md) for the SDK reference and the rules that constrain this code.

## Source layout

| File | Role |
|---|---|
| `src/types.ts` | Container IDs, canvas constants, uplink/downlink message protocol (mirrored in Python) |
| `src/BridgeClient.ts` | SDK lifecycle; enforces `waitForEvenAppBridge` → `createStartUpPageContainer` → rest |
| `src/HudPageBuilder.ts` | Layout declarations; guarantees one `isEventCapture` and unique `zOrderIndex` |
| `src/HudPresenter.ts` | `showText` / `appendText` (streaming) / `showList` |
| `src/AudioStreamer.ts` | `audioControl` + PCM batching (~200 ms chunks) |
| `src/ImuMonitor.ts` | Nod detection + R1-ring confirm → `Confirmer` for consequential actions |
| `src/EventRouter.ts` | Single `onEvenHubEvent` subscription, typed fan-out |
| `src/MacLink.ts` | WebSocket to the Mac: backoff reconnect, bounded queue (drops oldest audio) |
| `src/main.ts` | Boot sequence and downlink command handling |

## Status

- Build is wired: `package.json`, `vite.config.ts`, `tsconfig.json`, and `app.json` all exist. `npm run build` typechecks + bundles; `npm test` runs vitest.
- `app.json`'s `network` permission `whitelist` must carry the Mac's WebSocket
  origin exactly (scheme and port included). Direct development can use plain
  `ws://…:8787`; an unattended Tailnet home brain uses portless
  `wss://<mini>.ts.net` behind Tailscale Serve. Keep that build-time candidate
  in sync with `.env`; a mismatch fails silently.
- IMU nod thresholds in `ImuMonitor` are untuned guesses — calibrate with `scripts/imu_capture.py` (see [docs/TESTING.md](../../docs/TESTING.md)).

## Dev loop

```bash
# one-time: official CLI + simulator (binaries: evenhub / eh / evenhub-simulator)
npm install -g --prefix="$HOME/.local" @evenrealities/evenhub-cli @evenrealities/evenhub-simulator

npm run dev                                  # serve the plugin
evenhub-simulator http://localhost:5190      # desktop simulator (injects the bridge)
evenhub qr                                   # QR sideload to the phone (auto-detects LAN IP)
npm run pack                                 # isolated .ehpk production snapshot
```
