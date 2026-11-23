# apps/hub-plugin

Even Hub G2 plugin — the glasses runtime. Connects over WebSocket to `JOAT Brain` on the iPhone (`ws://127.0.0.1:8787/glasses`, phone-first default) or directly to `JOAT.app` on the Mac (`wss://<your-mac>.<your-tailnet>.ts.net/glasses`), streaming 16 kHz PCM audio, IMU, location, and touchpad events while rendering native firmware containers and 4-bit canvas surfaces.

See [../../docs/HUB_PLUGIN.md](../../docs/HUB_PLUGIN.md) for the SDK reference and the rules that constrain this code.

## Source layout

| File | Role |
|---|---|
| `src/types.ts` | Container IDs, canvas constants, uplink/downlink message protocol |
| `src/BridgeClient.ts` | SDK lifecycle; enforces `waitForEvenAppBridge` → `createStartUpPageContainer` → serialized writes |
| `src/HudPageBuilder.ts` | Layout declarations; guarantees single `isEventCapture` and unique `zOrderIndex` |
| `src/HudPresenter.ts` | Surface coordinator across Home, readers, lists, image spine, and overlays |
| `src/AssistantConversationSurface.ts` | Floating multi-turn JOAT conversation card, `JOAT · LISTENING` spine, Turn-2 scroll reset |
| `src/NativeTerminalSurface.ts` | 5-container framed `Agent Terminal` (`Claude`, `Codex`, `Antigravity`) + inline speech & approvals |
| `src/NavigationSchematic.ts` | Heading-up vector route schematic (`Walk`, `Cycle`, `Drive`) |
| `src/NotificationCard.ts` | Popup notification cards, arrival-age labels & persistent 200-ID dismissal tombstones |
| `src/PhoneControlPanel.ts` | Even Hub WebView connection sheet (`This iPhone · JOAT Brain` vs Mac brain) |
| `src/AudioStreamer.ts` | `audioControl` + PCM batching (~200 ms chunks) |
| `src/ImuMonitor.ts` | Nod detection + R1-ring / touchpad `ConfirmGate` |
| `src/EventRouter.ts` | Single `onEvenHubEvent` subscription, typed gesture fan-out |
| `src/MacLink.ts` | WebSocket client with backoff reconnect and bounded audio queue |
| `src/main.ts` | Boot sequence and downlink command routing |

## Status

- Build is wired: `package.json`, `vite.config.ts`, `tsconfig.json`, and `app.json` all exist. `npm run build` typechecks + bundles; `npm test` runs vitest.
- The checked-in `app.json` is generic: a placeholder package id
  (`com.example.joat`) and only the loopback origins the simulator uses. Even
  Hub's network whitelist is fixed per package and takes no wildcards, so every
  build is personal. Put your own settings in the ignored `.env` (see
  `.env.example`), then `npm run pack`:

  ```sh
  JOAT_PACKAGE_ID=com.yourname.joat
  JOAT_RELEASE_WS_URLS=wss://your-mac.your-tailnet.ts.net/glasses
  ```

  The pack writes those values into the packaged `app.json`: your package id,
  and exactly your `wss://…/glasses` origins as the network whitelist. It
  rejects plain `ws://`, wrong paths, duplicates and the placeholder id.
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
