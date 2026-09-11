# JOAT Mac app redesign

## Outcome and boundary

Make the Mac the clear place to understand JOAT, recover its connection, and configure the services that support the glasses. Keep the existing Python engine, native personal-data bridge, notification watcher, and Even Hub protocol as their canonical owners. This task owns the Swift Mac shell; active phone and HUD repair work remains separate.

The supplied screenshot is a baseline of the current interface, not a specification or source of instructions. It mixes monitoring, setup, content preferences, and support into one scrolling panel. The new experience separates those jobs while keeping live health visible.

## Experience and structure

The person opens JOAT to answer one of three questions: Is it ready? What just happened? How do I change or fix it? A conversation-only window hides setup; an expanded settings form repeats the current scrolling problem. A native sidebar with a focused overview keeps these jobs reachable without inventing a second glasses interface.

| Destination | Purpose | Existing owner / connection |
| --- | --- | --- |
| Overview | Latest exchange, readiness, next useful action | EngineStatus and EngineController |
| Glasses | Relay status and pairing recovery | Engine status, PairingWindowStore |
| Intelligence | Speech, model, Brainstorm, and Hermes connection | Engine status and OpenClawRuntimeConfiguration |
| News | Categories, custom feeds, update interval | NewsConfigStore; engine reloads news.json |
| Access | Understand and explicitly grant each Mac permission | NativeBridgeServer, MessagesAccessProbe, NotesAutomationProbe, NotificationWatcher |
| Diagnostics | Support status, logs, recording, redacted export | EngineController and G2DiagnosticExport |

Use NavigationSplitView, a standard toolbar, system settings, native controls, and semantic colors. Let the system render Liquid Glass on navigation and control surfaces. Keep reading surfaces calm. Gate macOS 26 glass APIs and preserve older-system controls. Respect keyboard navigation and reduced-transparency settings.

## Ordered implementation

1. Record existing files and active ownership; trace controls to their real service owners.
2. Replace the long root form with sidebar navigation and focused views. Preserve last-known data with an explicit stale label, and avoid equating an engine process with a working glasses connection.
3. Surface existing actions in their relevant destinations. Make configuration saves truthful, preserve input on error, and fix restart ordering so a slow shutdown cannot swallow Restart.
4. Add only command connections backed by an existing receiver. Do not add decorative mic, send, reconnect, or capability toggles without their complete transport and acknowledgement path.
5. Build through the existing Xcode project and signing identity. Verify Swift tests, then a real .app window, navigation, resizing, keyboard behavior, stale state, and configuration/error journeys.

## Acceptance and evidence

- Every visible action reaches its existing owner or explains a concrete prerequisite.
- News edits persist successfully before the displayed selection changes; failed writes preserve the previous saved state and typed input.
- A stopped or stale engine cannot appear ready. Last exchange is labelled as a previous observation rather than a current HUD mirror.
- Lifecycle actions serialize shutdown and restart; Quit and Stop cancel any scheduled recovery.
- Main window, settings, and menu bar describe the same operations.
- Build/test evidence, actual app visual evidence, and physical glasses acceptance are reported separately.

## Implemented in this pass

- Six native destinations, scene-restored selection, standard Liquid Glass toolbar grouping, and a glass primary connection action with an older-system fallback.
- Overview distinguishes a running process, fresh service status, and the phone relay. It labels the latest exchange as engine output instead of claiming it is the current physical display.
- News drafts survive navigation. Successful persistence is the commit point for selections and draft clearing; failed writes retain the previous saved state. Duplicate hand-edited feeds have stable unique row identities.
- The Hermes editor now loads and saves all 1–16 configured Macs using the engine's existing single-host or `hosts` schema. It preserves draft identity, rejects duplicate Gateways, preserves owner-only storage, and detects external changes before saving. Main workspace and Settings share one model.
- Calendar, Reminders, Notes, Messages, and notification permissions retain their existing owners; denied Calendar/Reminders/Notes routes open the relevant privacy settings.
- Restart waits for child exit; later Stop or Quit supersedes pending restart; crash recovery is cancellable; an old shutdown timeout cannot kill a replacement child.
- The menu calls the operation “Stop Engine,” matching the toolbar. Closing the main window still leaves the engine running.

## Verification and running build

The original delivery used a read-only preview that disabled setup controls and did not satisfy the working-app request. That launch path is superseded: Run now uses the canonical signed Release build, installs `/Applications/JOAT.app` with a retained backup, closes the prior app/preview gracefully, and starts the working app with its existing identity. The installed preview marker is NO and only the working production app is running.

See [Mac controls acceptance](MAC_CONTROLS_ACCEPTANCE.md) for the complete control inventory, real interactions, verified downstream effects, and remaining gaps. The Hermes form has been exercised through actual add, invalid-input rejection, save, disk reload, removal, restoration, and restart; the production Python loader independently read the saved rosters. The user's original Gateway configuration was restored before restarting. Further source corrections add unreadable-file recovery, feed validation feedback, truthful login/diagnostic status, and native Go-menu keyboard navigation.

The source suite records 81 passing checks (77 XCTest and four Swift Testing). These results support model/filesystem behavior only; they do not establish UI acceptance. The final signed universal build passed strict signature verification. The native tool captured Overview and exercised Settings, but selecting News breaks the tool connection before the resulting page can be inspected. The remaining News/Access/Diagnostics/sidebar journeys are not accepted as verified. No new permissions, pairing reset, physical-device acceptance, or Hub package upload is claimed.

## Next connection layer, in dependency order

The existing `HubGateway._session` replaces the current plugin when another client connects. The Mac must not impersonate a plugin or send downlink frames into its uplink. Direct Mac mic control, capability activation, and model/provider switching therefore need a distinct local command path before they can become honest buttons.

1. Add a bounded local shell-to-engine command channel with process identity checks, request IDs, timeouts, and explicit result acknowledgements. Keep the existing phone connection and its session ownership untouched. Review this with the owner of the active Python service work before editing shared composition.
2. Publish a sanitized capability/settings projection from the existing service registry. Distinguish configured, available, active, denied, disconnected, and pending states; do not infer availability from installed names.
3. Connect a Mac capability library to that projection. Start with news refresh and service retry, then mic state and agent selection through their existing owners. Preserve confirmation and cancellation rules for consequential actions; never manufacture a glasses confirmation from a Mac click.
4. Exercise successful commands, refusal, timeout, disconnect, restart, and superseded-session cases. A command must not take focus away from a new glasses task, resurrect recording, or report success before the owner acknowledges it.
5. Run the signed production Mac together with the phone and physical glasses: verify each visible control, voice response behavior, and no new latency or rendering regressions. Only then call the direct glasses controls connected end to end.

Primary design reference: [Apple, Build a SwiftUI app with the new design](https://developer.apple.com/videos/play/wwdc2025/323/).
