# JOAT Mac controls: wiring and acceptance

Date: 2026-09-11. Scope: the installed signed Mac app and its existing engine/configuration connections. A control is accepted only at the evidence level actually observed below. A passing model test is not a UI result, and a saved setting is not delivery to physical glasses.

## Agent Terminal A2A update

OpenClaw and native Hermes are now distinct editable Agent Terminal connections, with endpoint-bound Keychain tokens and actual authenticated engine status. The installed Settings UI was used to edit, save, reload, restore a profile and invoke Restart to Apply. A changed field enabled Save; unchanged saved fields kept Save disabled. The token fields were editable secure fields and remained empty when saved credentials were retained. Production Python read back the UI's saved profile.

Real requests and follow-up memory checks passed against both agents through production terminal classes. An initial OpenClaw follow-up failed behaviorally despite a completed task; bounded local conversation context fixed that failure. See `AGENT_TERMINAL_ROADMAP.md` for times, test scope, and remaining pairing/device boundaries. Earlier missing-A2A statements in this document are superseded by this update.

## Correction to the earlier delivery

The previous Run action deliberately launched a read-only Debug preview, disabling News, Hermes, Access, engine controls, and Settings without an enabling action. That delivery did not satisfy a working-app request. Run now builds with the canonical Release/signing pipeline, installs `/Applications/JOAT.app` with a retained backup, gracefully closes the prior app and project preview, and opens the working app. It checks the production bundle identity and rejects the preview marker. This check verifies the launch mode, not the controls.

## Control inventory

All entries below are wired in source unless specifically identified as missing. Preview still disables mutations if explicitly constructed by a developer; it is no longer the Run destination. References are relative to `apps/mac/JOATShell/Sources/JOAT`.

| Control | Availability and owner | Actual effect / acceptance boundary |
| --- | --- | --- |
| Six sidebar destinations | `MainWindowView.selection` | Changes the focused page. Native pointer automation fails on sidebar selection; this is not an accepted click journey. Go menu and Command–1 through Command–6 are implemented; Command–4 also breaks the tool connection before the new page can be inspected. Neither route is accepted as a completed navigation journey. |
| View Connection / Notifications on Overview | `OverviewView` navigation closure | Navigates to Glasses / Access. No remote command is sent. |
| Toolbar Start / Stop / Restart; menu-bar equivalents | `EngineController.start/stop/restart`; disabled during shutdown | Owns the Python child process. Restart to Apply was clicked; old child 55177 exited, replacement 55758 appeared, and the prompt cleared. Menu-bar duplicates still need their own interaction checks. |
| Settings toolbar / Command–comma | SwiftUI `openSettings` / Settings scene | Actual keyboard action opened the working Settings window, with enabled editable controls. |
| Gateway text fields | Shared `HermesConnectionsModel.drafts` | Actual text entry verified. A dashboard address using port 9443 kept Save disabled with validation guidance; a valid distinct origin enabled Save. |
| Add Mac / Remove Gateway | Draft roster, 1–16 hosts | Actual Add produced another editable row. Actual Remove removed the test row and enabled Save. Last remaining row has no Remove button. |
| Save Connections / Return in a Gateway field | Valid, changed, distinct roster; no unreadable saved file | Actual Save wrote a two-host roster; the production Python loader read two hosts. UI Reload reproduced both. Test host then removed and original one-host content restored through Save; production loader read one original host. Return remains a source-mapped alternate, not separately exercised. |
| Reload Saved | `HermesConnectionsModel.reload` | Actual reload from disk verified; replaces drafts. |
| Restart to Apply | A save newer than last engine start; running engine | Actual click disabled transition-sensitive controls, replaced the child, and removed the restart prompt. |
| Back Up and Reset / Show Saved File / Show Backup | Appears after connection load failure; explicit confirmation before reset | New recovery path preserves an unreadable owned regular file and resets the editor. Refuses a symlink and preserves a file repaired elsewhere. Real filesystem regression checks pass; live malformed-config UI journey not exercised against the user's good configuration. |
| News category checkboxes | `NewsSettingsModel.setCategory` → `NewsConfigStore.save` | Writes `news.json`; engine reloads through `NewsService.config`. UI selection commits only after save. Physical headline refresh is a separate result. |
| Feed URL / Add Feed / Return | Valid distinct http(s) address, no credentials | Writes `customFeeds`; failed saves keep typed input. Invalid/duplicate input now explains disabled Add. UI interaction evidence is recorded below. |
| Remove Feed | `NewsSettingsModel.removeFeed` | Removes that saved custom URL. Removing the last content choice restores the engine's default categories. |
| Refresh interval stepper | 5–1440 minutes in five-minute steps | Writes `refreshMinutes`. Engine uses changes on its next news pass; this is not a Refresh Now action. |
| Calendar / Reminders Grant Access or Privacy Settings | Existing native permission service, state-dependent action | Permission request or corresponding system privacy page. Already-granted permissions show Granted. Existing bridge reports ready; no new permission grant was requested for testing. |
| Messages Privacy Settings | Full Disk Access pane | Opens system settings; it does not grant itself access. |
| Apple Notes Grant Access / Privacy Settings | `NotesAutomationProbe` | Explicit automation permission request or denied-state recovery page. No Notes content was created for this audit. |
| Notification text Grant Access | `NotificationWatcher.requestAccess` | Requests Accessibility via existing owner. Running engine reports notifications live. No new grant was made. |
| Check Access / Privacy & Security | Existing status probes / system settings URL | Permission-status refresh or navigation; not access to personal content. |
| Open Pairing Window | Running engine, not stopping; `PairingWindowStore.open` | Writes a 120-second authorization marker and removes the previous pairing token. Wired but not clicked: testing this would reset current trust. Requires phone/physical-device acceptance. |
| Record G2 diagnostics | `setG2DiagnosticsEnabled`; disabled during shutdown | Enabling clears the old trace; changes restart a running engine. Copy now distinguishes selected mode from an already-started capture. Not toggled against an existing trace during this audit. |
| Export Redacted Trace | `NSSavePanel` → `G2DiagnosticExport.export` | Actual export requires an artifact; output is redacted by the existing exporter. No success is inferred from opening a panel. |
| Open Full Log / menu-bar Open Log | `NSWorkspace.open(logURL)` | Opens the existing log. Source has no inline error if the OS cannot open it. |
| Launch at Login | `SMAppService` | Registration state comes back from the system, not the requested checkbox value. Pending approval and errors now have text feedback. Existing On state observed; enable/disable not exercised against the user's login preference. |
| Private Tailnet home brain | `setPrivateTailnetGateway` → defaults → next engine arguments | Persists a topology choice for next start. No immediate networking success claim. Not toggled against the user's live topology. |
| Change Engine Folder | `NSOpenPanel` → repository validation → defaults → restart | Folder choice is validated before use. Cancellation does not change the engine. |
| Open JOAT / close window / Quit | `openWindow`, AppDelegate lifecycle | Closing a window retains the engine; Quit waits for child exit. Installer graceful quit/relaunch observed; menu-bar entry and quit-during-restart still need separate UI checks. |

## Not connected as Mac commands

Microphone recording, glasses Bluetooth/battery, starting Agent Terminal/Brainstorm, model/provider selection, and plugin enable/disable are status/external-device responsibilities in this Mac interface. There is no completed shell-to-engine command path for them. The existing phone WebSocket replaces its active client, so attaching the Mac as another plugin would evict the phone. These features need a separate acknowledged local command channel, followed by receiver-level and physical-device checks; adding clickable decorations would reproduce the problem.

## Regression and evidence rules

1. Verify the exact installed artifact, signing identity, preview marker, and running process before any UI acceptance.
2. Exercise the visible control; observe the resulting UI. For a save, independently read the actual file through the production consumer. For a restart, verify old-child exit, new-child ownership, and fresh service status.
3. Restore test edits and verify equality with the initial configuration. Never count temporary test hosts as connected Gateways.
4. Keep success, invalid input, write failure, external edit, recovery, cancellation, and stale/disconnected service cases separate.
5. Maintain an explicit unverified list. Unit tests, source review, compiler success, accessible enabled flags, and a screenshot are useful but insufficient to close the complete journey.

The adversarial source reviewer suggested a quit callback was lost during an existing stop. Parent inspection rejected that finding: `pendingStopCompletion` is stored before the transition guard and drained by `engineExited`. No code was changed based on that unsupported claim. The race remains a useful runtime check, not a confirmed defect.

## Evidence files

`dist/mac-workspace/control-audit/` retains private before-values and the final verification receipt. `dist/mac-workspace/controls-tests.log` records 77 XCTest plus four Swift Testing checks, including new backup/recovery tests. These counts do not certify the UI. The native interaction transcript records the form actions and resulting accessibility trees. No physical-G2 acceptance is claimed.

## Remaining acceptance and blocker

Completed: functional production install; enabled Settings inspection; live Hermes add/edit/invalid/save/reload/remove/restore/restart; source inventory of all visible controls; targeted recovery checks.

Remaining: News typing/add/remove/category/cadence through the actual UI; Access refresh and navigation; diagnostic export and cancellation; ordinary sidebar, Go-menu and Overview navigation; window reopening and quit-during-restart; controlled failure/recovery forms; login/topology changes in an appropriate reversible journey. Source wiring is recorded above, but none of these are marked passed merely because the build or model suite passed.

Blocker: the native computer-use tool repeatedly reports `Sky Computer Use native pipe closed before response` while switching the main window to News, through either a sidebar click or Command–4. It can read Overview and operate Settings; this is not a blanket inability to automate any control. Direct macOS Accessibility fallback was requested because the tool's instructions require explicit user permission for another computer-interaction mechanism; no such fallback was used without that response.

Outside this Mac slice: new microphone/model/plugin command transport and physical-G2 receipt. The live relay is disconnected. Existing news and Gateway settings were restored; pairing and permission grants were not changed.

## Mini connection follow-up, 2026-09-11 14:45 CDT

The saved `wss://home-mac.example-tailnet.ts.net` origin is correct for the tested OpenClaw route. The installed engine retained an `endpoint_refused` outcome from 13:40. A fresh unauthenticated transport probe at 14:44 received `connect.challenge` on port 443. Restarting the actual signed app then authenticated its existing paired identity, established operator read/write scopes, and made Agent Terminal ready. `scripts/openclaw_pair_probe.py` independently checked the fresh app-owned readiness projection, including session and tool preflight. No Gateway address or pairing credentials were changed, and no chat message was sent.

The displayed phone webview is a separate boundary: the currently served Vite source on local port 5208 sets `VITE_MAC_WS_URL` to `ws://127.0.0.1:18870/glasses`. That listener belongs to `/tmp/joat_feature_repair.py`, not the installed app on port 8787. Its dropdown is built only from those plugin candidates. Mac agent Gateways never automatically populate that JOAT-host selector.

Read-only route observations from this Mac:

| Mini route | Result |
| --- | --- |
| Port 443 `/` | OpenClaw Control HTTP 200; WebSocket challenge; actual app authentication subsequently ready |
| Port 8443 `/` | HTTP and WebSocket rejected with 403 `proxy_attribution_required` |
| Port 9443 `/` | Hermes Dashboard HTTP 200; tested root WebSocket returned 502 `Hermes WebSocket backend unavailable` |
| Port 443 `/glasses` | OpenClaw Control HTML, not the JOAT glasses handler |
| Direct port 8787 `/glasses` | Connection refused |

These observations do not dispute that the user's browser session can open a dashboard or that supervisor processes are running. They establish the routes available to this client, separately from process health. Native Hermes dashboard health is not the OpenClaw operator-session contract JOAT uses.

The Mac editor is now labelled OpenClaw connection / Agent Gateways / Add Gateway, with an explanation that phone JOAT-host selection is separate. Restart and Reconnect uses the existing engine restart owner; it does not introduce another WebSocket client or evict the phone. Broad UI acceptance gaps listed earlier remain separate. No remote Serve configuration, proxy trust setting, test fixture process, or physical-glasses pairing was changed.

## A2A update

External A2A availability has changed since the original control audit. OpenClaw's supplied A2A endpoint passed an authenticated task round trip from this Mac; Hermes's card and unauthenticated rejection were independently observed. See `docs/AGENT_TERMINAL_ROADMAP.md` for exact receipts, discovery-card defects, the remaining Hermes credential-access gap, and ordered integration work. No A2A endpoint has been silently inserted into the WSS-only Gateway editor or phone JOAT-host list, and the current Mac app still does not have an A2A task adapter.
# Agent Terminal A2A implementation — active scope

The current request authorizes wiring OpenClaw and Hermes A2A into the existing Agent Terminal, including Mac configuration and real request/result behavior. The direct Gateway session path remains available; A2A is an additional explicit terminal connection, not a glasses relay replacement.

Implementation order: (1) add endpoint-bound Keychain configuration and native credential access; (2) add a typed A2A adapter with context continuation and truthful task states; (3) compose configured peers into the existing terminal roster, capture, output, and cancellation owners; (4) verify adversarial HTTP behavior, the production controller journey against the live OpenClaw peer, and the signed Mac configuration interface. Native Hermes authentication depends on its separate credential; physical lens acceptance is separate from simulator/controller proof.

Acceptance rejects automatic replay after an uncertain send, credential forwarding on redirects, claiming a pending/failed task completed, and claiming local cancellation stopped remote work without a receipt.


## Physical relay acceptance continuation — 2026-09-11, 15:44–15:46 CDT

The installed engine still reported both A2A connections ready and the glasses relay disconnected. Read-only Tailscale Serve inspection confirmed `laptop-mac.example-tailnet.ts.net:443/glasses` forwards to the installed engine on loopback port 8787; an HTTPS GET returned HTTP 426 (WebSocket upgrade required), establishing route reachability, not pairing.

After the user instructed continuation, the production `PairingStore.begin_pairing()` routine opened the normal 120-second window. Its owner-only marker was verified; there was no prior glasses pairing token to replace. Native Computer Use failed repeatedly with a closed pipe, so this does not establish a click on the Mac pairing button. The window expired at 15:46:27 CDT without a peer authenticating, no pairing token was created, and the relay remained disconnected. Phone/physical-G2 acceptance therefore remains blocked on opening JOAT in the phone's Even app and completing normal pairing.

The existing development webview on port 5208 still targets the separate fixture engine on port 18870, not the installed engine on port 8787. Its connected state was not counted as installed-app or physical-device evidence. No fixture process, release package, remote service, or access policy was changed during this continuation.


## Updated plugin pairing and live terminal checks — 2026-09-11, 16:10–16:12 CDT

The user installed the new plugin and reported a reconnect loop. Current engine logs showed incoming requests rejected with HTTP 403 because the local pairing window was closed; the private Tailscale route was correctly configured, both A2A agents were ready, and no pairing token existed. Reopening the normal 120-second pairing window through the production PairingStore immediately saved a pairing credential and connected the reported G2 relay. The user independently confirmed that it was connected. No authentication policy, relay address, agent credential, or plugin source was changed to resolve the loop.

The relay remained connected during a ten-second stability check. Through the installed engine's authenticated MCP tool path, `send_terminal_instruction` then opened each actual terminal lane and submitted one harmless diagnostic instruction. `terminal_sessions` returned the exact standalone response line from OpenClaw after 9.7 seconds and Hermes after 6.2 seconds, while the relay stayed connected. These were not separate adapter instances or simulated WebSocket peers. The checks establish paired relay and live installed-engine terminal responses; the exact rendered lens output and spoken-input journey still need wearer confirmation.

## Reconnection incident and scoped commit verification — 2026-09-11, 18:05–18:09 CDT

The user reported the phone stuck Connecting after the simulator inventory. Live inspection found no inventory simulator, proxy, or established connection on the installed engine's port 8787. The Tailscale /glasses route still forwarded to that listener. Engine logs at 18:02–18:04 showed repeated authentication refusal and an explicit pairing-token mismatch. These observations establish that the phone reached the Mac; they do not identify why the phone and Mac credentials diverged. The earlier inspection should have verified the phone's reconnection before treating cleanup as sufficient.

The normal local pairing routine opened a 120-second replacement window. It expired without another phone request; the user was asked to reopen JOAT and tap Connect. A subsequent five-minute bounded window was opened. At this record's creation no new phone handshake had been observed, so reconnection remains unverified. No agent credentials, Tailscale route or authentication policy changed.

Commit preparation isolated the task's Mac workspace, phone connection controls, A2A transport/integration and audit documentation from the other task's newer rendering, notification and productivity work. An archive of the exact staged source passed 233 focused Python terminal/A2A tests, a complete Swift source/test compilation with three A2A credential tests, TypeScript checking, and a production Vite build. This is source and contract verification; it does not close the physical menu/gesture defects documented in the screen inventory. No private pairing or agent credential is included in the staged changes. No package version bump or new upload is part of this commit.
