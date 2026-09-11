# Agent Terminal screen inventory — 2026-09-11

## Evidence and limits

Official Even Hub simulator, live installed JOAT engine on port 8787, production plugin build with isolated loopback transport on 18871, served on 5209; automation port 9899. This was not the unrelated feature fixture on 18870/5208. Current plugin source was previously compared with the uploaded 0.1.85 source receipt. No production code changed during this inspection.

Captured actual rendered screens, not test assertions. Screenshots are retained under `dist/mac-workspace/control-audit/agent-terminal-inventory/`; raw alpha-channel images and black-background optical copies are separate.

The complete firmware-menu journey is NOT accepted: the installed simulator documentation supports long-press, but tap/hold through the HTTP controls did not display the firmware contextual menu. Native app discovery did not expose the simulator for direct UI control. Initial OpenClaw entry used the live engine's terminal instruction tool with a harmless response-only prompt. Subsequent session selections used the actual Mac capability protocol through the temporary proxy. Those verify real session pages but do not prove the user's tap-to-picker journey. The picker did not replace the existing terminal through that externally initiated entry. Do not call protocol-selected pages gesture acceptance.

## Observed pages

| Capture | Page | Observed result |
|---|---|---|
| 01-initial | Home | Live dashboard; tap opens selected tab |
| 02-click | Notifications | Real message notification rows; Back returns home |
| 04-home-down | News tab | Live feed content |
| 05-assistant-tab | Search | Search instruction page |
| 06-terminal-entry | OpenClaw opening | Transitional Opening openclaw text |
| 08-terminal-back | OpenClaw transcript | Actual response Screen inspection ready; header READY / TOOL STATE NOT CLEARED |
| 12-context | OpenClaw question | Tap opened What should openclaw do? |
| 13-question-back | Return from question | Double-click cancelled question and restored transcript |
| 14-terminal-menu | Terminal Back | Double-click did not open menu, despite footer saying menu; externally initiated entry must be distinguished from normal menu entry |
| 18-hermes-settled | Hermes | READY; previous real connection-diagnostic reply visible |
| 19-work | Work | NEEDS APPROVAL; approve Work permissions in OpenClaw, then retry; no output yet |
| 20-hermes-session | Hermes Session | READY, no tools checked before send, no output yet |
| 21-calendar | Calendar | READY, no tools checked before send, no output yet; an agent transcript, not a calendar |
| 11-webview | Phone control page | Connected; one Mac connection selector, Reconnect, listening preference, Troubleshooting |

## Why the options differ

`apps/mac/joat/__main__.py:1423` constructs older Gateway sessions named work, hermes and calendar. Enabled A2A profiles are prepended; a label collision changes the older hermes name to hermes session. Current live roster therefore contains five: openclaw, hermes, work, hermes session, calendar. Work is a separate Gateway work session, not the name of another agent. Calendar is a Calendar-proposal policy session. Hermes Session is the older Brainstorm-related no-tools Gateway session, not the new direct Hermes A2A service.

`apps/mac/joat/services/capabilities.py:758` opens a session picker when sessions exist and nothing is running, but opens a current terminal when work is busy. Thus the same feature entry is state-dependent. The exact six-option screen reported by the user was not reproduced; do not invent a sixth item. The app-wide capability menu source contains eight entries, distinct from the five-session roster.

`apps/mac/joat/services/agent_terminal.py:674` truncates the summary to the first three lanes. This was visible on all terminal screenshots: only OpenClaw, Hermes and Work appeared, even when Calendar or Hermes Session was open.

`apps/hub-plugin/src/HudCanvasRenderer.ts:641` uses TOOL STATE NOT CLEARED whenever noToolsChecked is false. Real A2A services can be READY while showing that alarming copy. This observation does not establish an actual unapproved tool run.

`HudCanvasRenderer.ts:627` advertises double-click: menu, while `CapabilityMenu.ts:861` explicitly implements Back and relies on firmware for the menu. The copy and navigation contract disagree.

## Ordered repair and acceptance plan

1. Make Agent Terminal's visible choices the two configured user-facing agents, OpenClaw and Hermes. Place older Work/Calendar/Brainstorm sessions in explicitly named advanced session management if still needed; preserve runtime ownership until migration is verified.
2. Use one consistent entry screen. Restore the selected conversation on return, with a clearly available Change agent action. Do not silently switch between a five-session picker and a transcript based on unrelated activity.
3. Show the active agent, connection/task state, readable conversation and Speak action. Remove unexplained internal policy labels; retain real actionable approval/errors with accurate recovery guidance.
4. Fix Back/gesture labels across normal menu entry, externally opened terminal, question, transcript and busy states. Do not claim a gesture works from an assertion about a emitted event alone.
5. Replace silent three-lane truncation with an explicit count or overflow affordance if a roster summary remains.
6. Verify with official simulator captures of the complete menu-to-agent-to-question-to-reply-to-Back journey for both real A2A agents; verify busy/approval/disconnection separately. Repeat on the physical G2 because firmware contextual menu entry remains unverified here.

## Cleanup

The task-owned simulator, proxy and HTTP server were stopped after inspection. Existing pairing credentials were preserved. Phone reconnection was not independently observed after cleanup; reopening JOAT on the phone can reuse its existing pairing. No new package was uploaded and no product source was changed.
