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

## Not yet done

- No `package.json` / `vite.config.ts` / `app.json` yet — scaffold with the `even-hub` CLI (`even-hub init`) and merge this `src/` in, so the generated manifest and build config stay canonical.
- `app.json` must list the Mac WebSocket domain as an **exact match** (no wildcards).
- IMU nod thresholds in `ImuMonitor` are untuned guesses — calibrate against hardware.
- No tests yet; the desktop simulator accepts `window.postMessage` pushes, so `AudioStreamer`, `ImuMonitor`, and `EventRouter` are testable without glasses.

## Dev loop

```bash
even-hub init          # generate manifest + build config
npm install @evenrealities/even_hub_sdk
even-hub qr            # sideload to the phone for testing
even-hub pack          # build .ehpk for submission
```
