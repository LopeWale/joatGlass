# Changelog

## 2026-09-11 — Agent Terminal chooser and isolated simulator acceptance

- Show only configured OpenClaw/Hermes agents in Agent Terminal; retain legacy Work/Calendar/session owners internally. Entry always opens the same chooser, including during a request. Use explicit connection states and consistent double-click Back/cancel instructions.
- Back retires the conversation binding and returns to the chooser; cancelling a question restores its selected conversation. Reject delayed retired-opening frames and handle cycling from an internal legacy session.
- Independent UI and ownership reviews informed the fixes. The final clean committed-source replay through `859870b` passed both complete official-simulator journeys, cancellation and busy re-entry; all 14 stable frames were visually inspected. Local TLS agent responses and acknowledged synthetic final speech transcripts are explicit fixture boundaries.
- TypeScript compilation, 255 scoped UI tests and 248 roster/opening tests passed separately. The runner closed its isolated processes/ports and removed its temporary TLS key. No installed engine, phone/glasses connection, pairing or remote credentials were accessed. See docs/audits/agent-terminal-simulator-flow.md.

## 2026-09-11 — Authenticated Agent Terminal A2A connections

- Added separate editable OpenClaw/Hermes profiles and endpoint-bound Keychain credentials in the signed Mac app. The engine verifies saved credentials at startup and adds both peers to the existing terminal roster; the earlier Gateway session remains distinct.
- Implemented task submission, real task-envelope decoding, bounded polling, input-required continuation, owned-task cancellation, explicit recovery and new-conversation commands. Uncertain submissions are not replayed and cancellation requires a remote task receipt.
- Parent review rejected the first adapter's simplified response fixtures and replaced them with real TLS contract tests. Live testing then caught a completed OpenClaw follow-up that forgot the conversation; bounded local history fixed it. Both peers subsequently completed real diagnostic and memory follow-up requests.
- Validation: 283 focused Python tests, three Swift A2A tests, five owned Python modules type-checked, signed installed app, native Keychain authentication for both peers, and actual Settings edit/save/reload/restore/restart interaction. Physical HUD delivery remains blocked by missing relay pairing; no pairing reset or bypass was performed. See `docs/AGENT_TERMINAL_ROADMAP.md` and `docs/MAC_CONTROLS_ACCEPTANCE.md` for exact acceptance boundaries.


## 2026-09-11 — Live A2A prerequisites verified

- Independently verified OpenClaw A2A over the supplied Tailscale HTTPS endpoint: unauthenticated rejection and a dedicated-peer `message/send` task completed with the exact expected artifact. Token stayed out of source and receipts. Hermes's agent card and unauthenticated rejection were verified; authenticated Hermes execution remains user-reported because SSH credential retrieval is unavailable.
- Found OpenClaw's public agent card advertising a loopback interface and omitting bearer-auth metadata. Updated `docs/AGENT_TERMINAL_ROADMAP.md` to distinguish now-live external endpoints from the still-unimplemented JOAT A2A adapter, and recorded ordered connection/acceptance work without substituting A2A for the session terminal or glasses relay.

## 2026-09-11 — Mini Gateway recovery and connection labels

- Verified that the saved Mini port-443 Gateway is reachable and speaks OpenClaw; a real JOAT restart authenticated the existing paired identity and returned Agent Terminal to ready. The prior refusal was the retained 13:40 attempt, not a new probe of the recovered service.
- Traced the shown plugin to a Vite simulator configured only for the isolated localhost:18870 fixture, distinct from the installed app. Verified that the Mini `/glasses` path currently serves OpenClaw Control and direct JOAT port 8787 refuses, so merely adding the Mini to that selector would not establish a JOAT relay.
- Renamed the Mac editor to OpenClaw connection / Agent Gateways / Add Gateway, clarified its distinction from phone JOAT-host selection, and added an existing-engine Restart and Reconnect action for a non-connected live state. Refusal guidance now describes the last attempt rather than asserting the remote Mac is currently down.

## 2026-09-11 — Working Mac controls and acceptance correction

- Replaced Run's disabled design preview with the working Developer ID-signed app at `/Applications/JOAT.app`; the canonical installer retains the previous app and the launcher waits for graceful shutdown before opening the replacement. Launch verification explicitly rejects preview mode and does not claim UI acceptance.
- Exercised the actual Hermes Settings form: Add Mac, editable input, dashboard-address rejection, Save, Reload Saved, Remove, restore, and Restart to Apply. Independently read the live saved roster through the production Python loader; restored the original one-host configuration before restart and confirmed child replacement/prompt clearing.
- Added backup-and-reset recovery for unreadable connection files, invalid/duplicate feed guidance, honest login-item/diagnostic feedback, and a Go menu with Command–1 through Command–6. Added targeted filesystem regression checks for recovery, symlink refusal, and externally repaired files.
- Source suite: 81 checks pass. News/sidebar switching still breaks the native automation connection; remaining input journeys and physical-glasses outcomes are explicitly unverified. See `docs/MAC_CONTROLS_ACCEPTANCE.md` for the complete control map, missing command paths, and actual acceptance evidence. The earlier preview delivery below is superseded.

## 2026-09-11 — Mac workspace redesign

- Replaced the single long Mac status/settings panel with native Overview, Glasses, Intelligence, News, Access, and Diagnostics destinations. Added system Liquid Glass toolbar grouping and a primary glass action, restored navigation selection, and separated current service status from last-known conversation output.
- Connected the Mac Hermes editor to the engine's existing multi-Mac configuration. It retains all configured hosts, shares drafts between Settings and the workspace, rejects duplicate/invalid Gateways, protects against external edits, and uses owner-only atomic persistence. A compiled Swift writer and the production Python loader verified the exact two-host contract without changing live settings.
- News saves now commit the displayed selection only after persistence succeeds and retain drafts on failure/navigation. Denied Mac permissions lead to their corresponding privacy settings. Restart now waits for child termination, Stop cancels pending recovery, and old shutdown timeouts cannot kill replacement children.
- Bounded read-only agents traced existing transport and reviewed the redesign; parent verification rejected the suggestion to use the plugin WebSocket as a desktop client because it would replace the phone session. The review's stale restart-needed prompt and continuity/density concerns were corrected.
- Mac suite: 79 tests pass. A signed Xcode Debug preview was built and its first Overview screenshot inspected. Further native UI interaction was blocked by repeated Computer Use native-pipe failures; final layout, navigation, keyboard, and live controls remain visually unverified. The installed app, phone/plugin work, and live configuration remain unchanged. See `docs/MAC_APP_REDESIGN.md` for the control map, evidence boundaries, and ordered direct-control bridge plan.

## 2026-09-10

- **Released private Hub build 0.1.84 and visually evaluated Navigation, Messages and Planning.** Implementation `daafac8`, package source `ca0c213`; portal submission confirmed after reload. The evaluation reproduced contradictory Navigation states during sequential quadrant updates, documented Planning date-context loss after operations and Messages reading-context/type differences, and retained working menu, paging and fixture action journeys. Obsolete or unrelated captures were rejected; physical-device and real-service behavior remain unverified. No new feature fixes were made. See `docs/audits/2026-09-10-navigation-messages-planning-visual-evaluation.md`.

- **Ordinary JOAT answers now remain in one Even-style conversation card.**
  Research was triangulated across the current official Even AI tutorial and
  support pages, current independent G2 through-lens reviews, the official
  G1 demo repository as a version boundary, and all four supplied JOAT screen
  recordings. The recordings showed one exchange split between dashboard
  prompt, transient strip, and modal answer; a long answer beginning with an
  invented ellipsis and ending before the requested fourth sentence; Up
  dismissing rather than scrolling; answered text disappearing while the
  prompt remained; and no persistent activity state. The replacement retains
  wearer and JOAT turns under one conversation identity, grows upward from a
  fixed lower edge one line at a time, uses the project's 14px proportional
  face with dim regular wearer rows and brighter 500-weight JOAT rows, streams
  partial answers in place, adds a narrow overflow scrollbar, and restores the
  newest underlying JOAT feature only after an accepted exit. While active,
  the conversation owns the complete content field below the status spine, so
  dashboard clocks, cards, and feature panes cannot compete with the one-card
  interaction; the retained underlay returns when the conversation closes. The
  requested inactivity question is explicitly JOAT behavior; current Even
  material did not establish a native labelled Continue control.

- **Rendering feedback no longer competes needlessly with answer latency.**
  The current official G2 reference uses one compact morphing glyph on the
  card's lower rule, rather than a three-dot chat indicator. JOAT now cycles
  through the observed bar, post, cross, and square silhouettes while
  thinking. The glyph stays within one lower image tile instead of straddling
  the x=288 seam, and streamed words receive priority: each answer update
  resets the activity timer, with another glyph tick only after a two-second
  streaming stall. The official-simulator acceptance at
  `/tmp/joat-conversation-raster-20260910-w/receipt.json` exercised the
  production Mac protocol, WebSocket, Hub plugin, Even SDK bridge, framebuffer,
  and input endpoint. It retained the exact three-exchange transcript through
  11 streamed revisions, visibly captured the joke setup before its punchline,
  grew through 1/2/4/5/6/9-line states, recovered all 15 wrapped rows with six
  swipes each way, restored the exact conversation-tail pixels, confined the
  activity-only difference to tile region 9, accepted Continue at the Mac,
  restored the underlay pixel-for-pixel outside the live clock, read eight
  declared rows from the actual SDK container, visibly opened five populated
  menu bands, and rebuilt no native page during the journey. The evidence
  harness was hardened after visual review caught false status-only,
  whole-frame, underlay, and menu assertions: captures now require matching
  card geometry, ignore unrelated live status changes, and compare the exact
  region, tile, menu structure, or underlay pixels owned by each claim.
  Physical-G2 BLE timing, optical brightness, source-specific left-temple
  double press, package parity, and deployment remain separate evidence gates.

## 2026-09-08

- **The Agent Terminal roster spans several Macs.** The roadmap's standing
  acceptance gate was "real multi-host roster with host labels only on
  ambiguity"; lanes were bound to one gateway, so a second Mac could not
  appear at all. `openclaw.json` gains a `{"hosts": [...]}` shape beside the
  original single-host object, which still means exactly what it meant. Each
  entry is validated by the same rules — the Hermes control dashboard port
  stays refused on every one, and a repeated gateway is rejected rather than
  painted as two rows nothing could tell apart. A lane's identity became the
  (name, host) pair, because two Macs really can both offer "work"; the
  display label is computed against the whole roster, so a unique name stays
  bare and every row carrying a repeated one gains its host. That rule had
  governed discovered sessions only, and now lives in one function both
  surfaces call. Spoken resolution stops guessing: matched
  most-precise-first, a tier matching several lanes returns those candidates
  instead of falling through to a looser rule, so "switch terminal to work"
  across two Macs names both and moves focus nowhere. Other Macs are
  constructed but never dialled at boot — an asleep Mac costs an unprepared
  row the wearer can tap, not a boot failure, which is the lesson ddbe0ef
  taught. Mutation-checked: never qualifying a label fails 6 tests, and
  silently taking the first of an ambiguous pair fails 2.

- **The Even AI door is reachable from the phone.** It had been serving on
  loopback 8788 while this Mac's Tailscale Serve published only `/glasses`,
  so Even AI could never actually reach the brain — A8 was true on the
  engine and unreachable from the glasses. Published at `/agent`; verified
  end to end over the tailnet with history (a name given one turn, recalled
  the next) and real streamed SSE deltas.

- **A tree that had been failing since 8bb5cc1 is green again.** Twelve unit
  tests and strict mypy were red on HEAD from four unrelated causes, none of
  them cosmetic: both acceptance harnesses still passed HudSink a terminal
  projection it stopped taking, and so could not construct a sink at all;
  both read the new `HudRebuild` layout message as if it were a view and
  asked it for a `state`; `Card` gained a `body` field ahead of `urgency`,
  so tests passing urgency positionally rendered URGENT cards without the
  "!!" they existed to prove; and the topic-capture arm binds its clock at
  construction, so a test patching the module's `time.monotonic` was
  measuring nothing while claiming to prove the arm expires. Also narrowed
  this session's own message-supersession fix, which could evict a draft
  whose question was already armed on the glass.

- **996 plugin tests pass.** The suite had been red across two releases
  because `pack.sh` runs typecheck, not vitest. Repairs pinned navigation
  ownership by name instead of raw uplink counts, replayed the OS
  foreground-exit that closes the contextual menu (without it the router
  correctly swallowed every press, so Settings opened dead), and corrected a
  test still asserting the opposite of the O3 both-temples fix.


## 2026-09-06

- A dead gateway dial now says which way it is dead — and says only what it
  knows (the audit's reconnection item, continued). The 2026-09-06 outage
  surfaced as one blind "gateway_not_ready" through two different failures:
  `gaierror` while Tailscale on this Mac was stopped, then
  `ConnectionRefusedError` once the tailnet was restored. A dial-failure
  classifier in the gateway module (`classify_gateway_dial_failure`, frozen
  dataclass result, class-name detail only — socket errors can embed the
  endpoint host) folds those into `host_unreachable` vs `endpoint_refused`;
  the status allowlist accepts both and the glasses recovery copy names the
  remedy. The refusal code is deliberately named for the ENDPOINT: the mini
  fronts the gateway with Tailscale Serve on 443 (MAC_APP.md §Private
  Tailnet transport), and through this very outage the gateway process kept
  answering Telegram while its Serve front refused — a first draft that said
  "the gateway is stopped — start it there" was wrong about the remedy and
  was corrected the same day against that live evidence. Executed live:
  restarting the engine against the refused port put the classified pair in
  the log and status.json. The separate Hermes manager on the mini's port
  9443 reports its own supervised gateway stopped with zero platforms; it is
  not the process serving Telegram, and JOAT deliberately never connects to
  that dashboard (HERMES_CONTROL_DASHBOARD_PORT guard).

## 2026-09-05

- Fixed the live Hermes protocol failure and restarted the engine onto HEAD.
  The wearer's own gateway sends a chat event with `state: "status"` mid-run
  (captured live at seq=2 after adding a state-token log); the Terminal
  adapter treated every unknown state as a protocol violation, so every real
  Hermes run died at its first status line. Informational chat states are now
  skipped: text still enters only through delta/final with the divergence
  checks intact, and a stream that ends without a terminal event still raises
  and aborts, so an unknown terminal state degrades to a truthful failure,
  never a hang or a false success. After the fix the same instruction ran past
  the status event and surfaced the provider's own error truthfully — the Mac
  mini's model provider is rate-limited right now, so the completed spoken
  answer remains pending on that quota, not on JOAT. Also verified live on the
  restarted engine: all 22 MCP tools served, a streamed Even AI door turn with
  history answered the correct local time in 2.8 s, the utterance queue and
  the disconnect close-code log emitted their first production lines, and the
  official simulator reconnected through the replacement protocol.

- Gave the HUD one typography system, and gave notifications their whole
  message back. A typography audit of every surface found no system to
  break: 128 canvas font sites across 18 fixed pixel sizes and two families,
  eleven different leadings, and the wearer's own ruling ("official G2
  screens set light proportional type; mono reads as a bold slab")
  implemented on two surfaces out of twenty. `HudTypography.ts` is now the
  single source: sixteen named roles, six leadings, four row pitches, one
  hint baseline, one selection-rail height, one `upper()` for draw-time case.
  Every font site resolves to a role; a fresh sweep finds zero hardcoded font
  strings, leadings, pitches or case transforms left. Values were chosen to
  preserve the existing hierarchy — nothing grew "large for no reason".
  Defects the audit surfaced and this closes: a 20px title wrapped at 18px
  leading (overlapping lines), a dead 17px assignment the autofit overwrote,
  a row capacity contradicting its own comment, chrome that inherited
  whatever font was set last, and a footer hint drawn five different ways.
- Sized the firmware containers to the firmware's own grid. The status bar
  (32px tall, 6px padding = 20px inner) could not render a single 27px line;
  neither could the tray or hint strips, and the list title box was 3px
  short. The confirmation body overflowed at the exact 120-character maximum
  the code enforces. Containers are now whole multiples of the firmware's
  27px line and 40px row, and list rows, titles and hints are truncated by
  MEASURED pixels (`@evenrealities/pretext`) instead of character counts —
  that library shipped imported-but-unreachable, in a native spine-label path
  no layout declared and no caller wired, which is now removed rather than
  left looking load-bearing.
- Stopped notifications arriving pre-truncated. macOS renders banners with an
  elided body and the Accessibility watcher can only read what is on screen,
  so a long message reached the glasses already cut — no display budget could
  undo it ("Can I not see full messages?"). Cards now carry the full text
  alongside the glance summary, the detail page renders it through the
  reader's measured rows instead of reusing the two-line glance card, and a
  Messages banner ending in an ellipsis is recovered from chat.db by prefix
  within a five-minute window. Ambiguous matches are refused rather than risk
  showing the wrong person's message, and a decline logs its reason with
  counts only, never content. Measured live: a 47-character banner became the
  full 152-character message on the glasses. Two bugs a green suite would
  have hidden: the lookup window covered only nanosecond Apple dates when the
  schema also uses seconds, and the decline was silent.
- Wire text is now bounded in UTF-16 units, the way the SDK counts. A Python
  code-point slice let emoji-bearing rows carry up to 128 units past a
  64-unit cap. The transcript's column budget derives in code from the
  measured advance (548px / 5.18px = 105) instead of restating it, and the
  budgets that are payload bounds rather than layout say so.

- Made the mind able to use every feature by ordinary conversation (audit A1,
  M8, N2, NAV1, part of Q3 and T2). New belt tools: `read_conversation` (whole
  messages from one thread, not an 80-character snippet), `read_news_article`,
  `read_quicknotes` (exact text and recording transcripts), `terminal_sessions`
  (roster and a lane's recent output) and `start_navigation` (the same
  confirm-place / choose-mode flow as "navigate to"). The persona now names
  what the mind can do and requires the wearer's words to pass through tools
  verbatim; message-tool results name their own next step. Measured against
  the REAL `claude -p` and `codex exec` seats over the real MCP endpoint with
  recording controllers (`scratchpad/a1_probe.py`, 14 ordinary requests):
  before, Claude reached the right controller in 8 of 11 and Codex in 6 of 11,
  and both paraphrased a message body, a note and a topic; after, both seats
  reach it in 14 of 14 with every phrase intact. Codex turns take 7–15 s,
  Claude 4–6 s; STT and paint are on top of that and were not measured here.
- A spoken correction now interrupts the answer in progress (A3): completed
  utterances queue in order (three deep) instead of replacing each other, the
  next one is transcribed while the current reply streams, and speech that
  ends while an answer is under way — an addressed request or a bare stop
  word — cancels that reply (and its CLI child) and takes its place. A second
  request spoken before any answer existed is answered after the first, not
  instead of it. Executed against the production orchestrator with a slow
  streaming replier: the first query was cancelled, the correction answered,
  room talk deferred.
- Latency receipts count from the wearer's end of speech (A6), so the first
  bucket is the queue wait; deterministic command answers, prompted answers
  and interrupted replies now emit stages too. The paint receipt from the
  plugin's `tile_upload` trace is still not correlated into the receipt.
- Batch WhisperKit transcription has an inference deadline (A5, 90 s, above
  the measured cold path); a hung child is terminated and the utterance
  dropped instead of holding "hearing you…" forever. Executed: a hung fake
  binary returned at the 1 s probe deadline.
- JOAT's own chores (Brainstorm judge, synthesis, rewording) run on separate
  worker seats and CLI sessions with a task persona (A7); the judge line
  names the seat that answered instead of "local". The Even AI door has its
  own seats that never resume a CLI session: the app's supplied history is
  the prompt (A8), and streamed answers reach the phone chunk by chunk over a
  close-delimited SSE body instead of one buffered event. Executed: history
  in the seat prompt, chunks spread over 1.2 s, markup stripped.
- Terminal approvals report the recorded verdict as data (T1): every
  successful approval was announced as "another controller answered first"
  because the roster's sentence was compared with the bare verdict.
- Overview's unread number is Messages.app's own unread count (O1), read off
  the loop thread every 20 s, not the retained notification-card count.
- A draft still waiting for a name or body is superseded by a newer message
  request instead of blocking it; "note …" refreshes an open QuickNotes list;
  the Messages picker shows ten conversations and QuickNotes eight plus eight
  (M5, Q3); "open navigation/directions/map" is a spoken opener (NAV1); a
  Brainstorm or Terminal "tap" accepts either temple or the ring (O3); the
  synthesis ends with a VERIFY line saying what, if anything, was checked
  (B3); MAC_APP.md no longer requires a Hermes seat for Brainstorm.
  The audit itself was deleted from the tree by commit 8bb5cc1; the findings
  are read from `f1e4ada:docs/audits/2026-09-04-feature-audit/AUDIT.md`.
- "Message my wife" resolves (M6): a `RelationshipBook` under Application
  Support, written only from the wearer's own statement through the new
  `remember_relationship` tool (the name must match exactly one Mac contact);
  nothing is inferred. A draft too long for the 120-unit confirmation prompt
  is read whole in the exact reader first — "MESSAGES · REVIEW", tap: send,
  Back cancels — and the confirmation then carries the opening words marked
  "(reviewed)" (M7). Selecting a saved Brainstorm opens its transcript in the
  reader; a second tap continues it (B3). The latency receipt gains a
  `painted` stage from the plugin's tile-upload trace (A6, diagnostics-only).
  The gateway now logs a disconnect's close code, reason and duration: the
  wearer's log had "plugin disconnected" and nothing else for the 04:46–05:17
  drops on the physical glasses, which followed audio sequence gaps and
  returned after seven minutes. Executed: `scripts/probes/verify_m6_m7.py`.
  Note for the live engine: it restarted at 13:32 while a two-step edit was
  half applied (NameError on import) and recovered on the supervisor's
  retry; it is still running pre-commit code until deliberately restarted.
  Hub 0.1.74 package rebuilt from d4a6a58 (`joat.ehpk`, sha256 aa8c61c3…),
  with only a test-file typecheck patch applied in the packaging worktree.
- News freshness is truthful without a wire change (N1): the service
  publishes only when a pass changed the headlines or the failure state, a
  kept-stale list opens with "[!] Feeds unavailable · showing headlines from
  N ago", a partial pass says so, and "refresh the news" answers updated /
  partial / kept-from-earlier accordingly. When a publisher page cannot be
  extracted, the feed's own summary is the article body, labelled as such
  (N2). "Terminal history" entries are no longer cut to their first 160
  characters (T2).

- Repaired authenticated display handoff: replaced clients yield instead of
  redialing, retired painters/input cannot operate the successor, and explicit
  foreground restoration reconnects once. Concurrent server admissions now
  retain the newest owner. In the isolated official
  simulator: 2 unsolicited redials became 0 over 2.2 seconds; duplicate restoration
  produced one takeover. Mac repair loaded in supervised engine 4704. Hub 0.1.77 is built in the canonical
  `apps/hub-plugin/joat.ehpk`, with matching source/package hashes; physical migration and the broader feature goal remain open.
  This is a source checkpoint with inspected transport-harness evidence, not
  acceptance of the glasses UI or completion of the 34 audited findings.

- Built requested Hub 0.1.75 at `apps/hub-plugin/joat.ehpk`: 984 Hub tests,
  eight release checks, typing and official SDK-pinned packaging passed.
  During live use, the task's simulator and the other G2 connection replaced
  each other 76 times in one minute. Stopping the simulator ended the loop;
  the other G2 reconnected and stayed connected through the next four-minute
  observation. An explicit replacement/yield protocol is under verification.
  The package predates that correction. No upload was performed by this task.

- Corrected Hermes run-outcome classification in source: valid failed or
  cancelled runs preserve their proven connection rather than becoming a
  protocol rejection. All 125 Terminal/runtime tests and typing pass, with
  23 failing-before regressions corrected. The actual remote run cause and
  completed answer remain unresolved. Evidence: `LIVE_CONNECTION_AND_REPLY_REPAIR.md`.

- Added conversational Terminal instruction admission with an explicit lane,
  exact text, captured adapter context and cancellation-safe retry identity.
  Existing queued work and approvals are preserved. The new entry binds its
  display before preparation, retains timeout recovery and drops queued
  openings after Home. Assistant overlays yield after a view handoff.
  Parent-run checks passed: 377 Mac cases, one additional HTTP MCP contract,
  214 Hub ownership cases, Python typing and TypeScript. Broader checks and
  the actual assistant-to-Hermes request are pending.
  Also removed the reproduced Overview paint before every Brainstorm frame;
  the initial test showed two unwanted home paints for two live updates.
  Actual after-transfer measurements remain pending. Evidence:
  `docs/audits/2026-09-04-feature-audit/LIVE_CONNECTION_AND_REPLY_REPAIR.md`.

- Connected conversational Brainstorm topics to the existing session controller
  through a typed MCP tool, with exact-topic preservation, active-session
  protection and bounded retry deduplication. Assistant receipts now yield the
  HUD after their tool starts a session. The missing adapter and two overlay
  regressions were reproduced before repair; 290 related tests, 149 final
  feature/orchestrator/MCP checks, ten admission/revision checks and typing pass.
  The final broader gate passed 422 tests and typing on 108 modules. The signed
  app's actual assistant invoked the tool, both real collaborators responded,
  and the session settled in 26.077 s (HTTP response 4.901 s). The later simulator
  frame was blank, so automatic display acceptance failed. Separately retained
  a 93.382-second simulator stall before
  JavaScript ingress; a webview snapshot woke it without input. No renderer
  workaround or hardware-latency claim was made. Evidence and official sources:
  `docs/audits/2026-09-04-feature-audit/LIVE_CONNECTION_AND_REPLY_REPAIR.md`.

- Fixed a live Hermes false blocker: OpenClaw's `browser-filtered-by-profile`
  notice describes an excluded tool, but JOAT rejected it as unsettled inventory.
  Only documented, well-formed browser exclusion notices may now accompany an
  otherwise valid empty inventory; enabled tools and discovery/unknown notices
  still reject. The actual signed app now reports connected_operator/ready,
  and the official simulator lists Hermes and opens its READY terminal.
  Seven new failing cases now pass; 287 related Terminal tests and typing on
  108 modules pass. A complete spoken turn and Work's separate scope grant
  remain unverified/unresolved. Official source and live receipt are linked in
  `LIVE_CONNECTION_AND_REPLY_REPAIR.md`.
- Fixed MacLink retry ownership: manual reconnect cancels an obsolete retry,
  and application timeouts retire the socket without waiting for browser close.
  Six reproduced regressions now pass; 974 Hub cases and build pass. Official
  simulator WebKit fault tests retain the successful manual connection (two
  sockets rather than three) and recover the timed-out auth peer in 4.747 s.
  A separate real-runtime startup/reconnect delay remains unexplained.

- Recovered attributed Messages bodies in notifications, assistant/conversation
  previews and history with bounded data-only decoding. Real 500-row sample:
  14 archived strings decoded through both readers, all marker-only. A wider
  200-archive sample also contained no readable prose; live readable-text
  recovery is therefore unproven. Public text fixtures cover that capability.
  Marker-only output now uses Attachment or Message text unavailable. 305
  combined Messages tests pass; the pinned dependency is installed and
  the actual supervised engine restarted. Personal text was not retained.

- Fixed prompted microphone entry from mute for Brainstorm, Messages and
  Terminal, with per-question microphone ownership and selected-source reporting.
  The Mac freezes each prompt's source; automatic mic stops revoke only their
  owning prompt. Manual mute and glasses removal remain global. 968 Hub tests,
  342 Mac prompt/transport tests, typing and Hub build pass. On the actual Mac
  engine and official simulator, Brainstorm's muted tap now opens the question
  and microphone; manual cancellation restores MIC OFF. Full spoken response
  and physical-G2 acceptance remain open.

- Completed existing-chat reply targeting through the native Messages chat
  identifier, including groups, while preserving contact-based composition.
  In the real Mac engine and official simulator, a selected group reached
  reply confirmation and was cancelled without sending. Native chat resolution
  timed out; actual submission and delivery remain unverified.
- Reproduced Work's actual `AUTH_SCOPE_MISMATCH`, separate from historical
  DNS/refusal failures and Hermes inventory attestation. Added private-content-free
  connection diagnostics, truthful approval guidance, unchanged-token grant
  preservation and native Work-scope validation. The remote grant remains
  unresolved. Five native credential tests and the signed universal build/install
  passed; the real app was restarted from `/Applications/JOAT.app`. Focused
  Python checks, typing, renderer checks and Hub build pass.
  Sources and real-data evidence:
  `docs/audits/2026-09-04-feature-audit/LIVE_CONNECTION_AND_REPLY_REPAIR.md`.

## 2026-09-04

- Added Terminal capture ownership after 23 reproduced stale/wrong-input
  failures. All 121 Terminal unit cases and typing now pass; the real tap
  and shared prompt runtime integration remain in progress. Captured work
  retains its local adapter generation through queued and delayed dispatch.
  Adapter review reproduced four additional shutdown/generator cleanup
  failures; those and the earlier context failures now pass in 34 adapter
  tests. Local generation does not establish remote session-reset safety.
  Evidence: `docs/audits/2026-09-04-feature-audit/TERMINAL_CAPTURE_REPAIR.md`.

- Rejected the first prompted-speech simulator run: it stopped before the
  second synthetic answer and exposed an oversized sans-serif question.
  Question rendering now uses the existing 14px JOAT monospace face; a
  reproduced font regression, all 100 renderer tests and the Hub build pass.
  Fresh visual acceptance remains pending. Retained the failed screenshots
  and added replay-stage diagnostics without changing production speech
  admission in response to an unproven fixture stall.

- Repaired exact Message/News/Note text paging and the clipped Messages Back
  footer while retaining the existing compact JOAT banner. The corrected
  simulator entry retry delivered all 980 Message and 974 Note characters
  across three honest pages, with matching source/wire/presenter hashes.
  Root and independent review inspected all seven images: full Back labels,
  visible final markers, bounded rows and correct parent selections pass.
  News/source checks are reused from the prior frozen gate. Emoji glyph
  fidelity remains unresolved; Search has test coverage without its own
  visual journey. Evidence: `docs/audits/2026-09-04-feature-audit/READER_ENTRY_VISUAL_RECEIPT.md`.

- Removed Codex's process-EOF wait for completed answers. The final JSONL turn
  now reaches the HUD while the owned child drains and exits; late failures
  invalidate resume context without repeating already delivered tool work.
  Ten reproduced failures are repaired, all 27 CLI tests and mypy pass, and a
  controlled completion-to-HUD measurement is 0.171 ms before EOF release.
  This is host buffering proof, not model or physical-glasses latency.
  Evidence: `docs/audits/2026-09-04-feature-audit/CODEX_EOF_LATENCY_REPAIR.md`.

- Connected Message tools and Brainstorm topic capture to one production
  prompted-speech runtime, preserving exact owner/connection/audio-epoch
  matching and cleanup on Home, microphone-off, reconnect and shutdown.
  Retirement reasons prevent cleanup from erasing an admitted Brainstorm
  topic before consumption. Twenty runtime failures and six retirement
  regressions drove the implementation; all 26 runtime and 141 supporting
  cases pass, with strict typing. Fresh simulator prompt-flow acceptance
  remains pending. Terminal and legacy Messages UI capture remain separate
  unfinished adapters; no real send or physical-G2 speech claim is made.

- The combined prompt/recording/CLI gate passes 1,156 tests in 90.09 seconds
  and mypy on 104 source files with unchanged source hashes. A first run hit
  the runner's 60-second aggregate limit; its isolated next test passed, and
  the same source completed under a corrected bounded deadline. Starting a
  QuickNote also now revokes the previous question synchronously, preventing
  an old completed transcript from answering it after recording ends.

- Restored the existing compact JOAT canvas banner and News/Search typography
  after the wearer rejected the native-font migration. Fresh current-source
  simulator captures show ten complete News rows and the same 861-character
  Note over two pages instead of three; occupied first-page text width grew
  from 393px to 488px. Root and independent review inspected all 13 captures.
  The gate passed 397 Python/868 Hub tests, typing and build, but visual
  acceptance remains partial: Messages footer truncation was observed and is
  being repaired with exact long-text readers. No physical-G2 or latency
  improvement is claimed. Evidence: `docs/audits/2026-09-04-feature-audit/COMPACT_UI_VISUAL_RECEIPT.md`.

- Visually reviewed all 34 matched repair captures. Saved-note reading grew
  from 64/861 to exact 861/861 characters across three pages; Messages paging
  and Back/reopen now work; QuickNotes Back followed by Save no longer
  discards. Native News succeeded in both runs, demonstrating no causal gain,
  and visual review exposed a Back highlight/target mismatch that internal
  state checks missed. Its new failing regressions drove an awaited visual
  selection restoration. Fresh simulator review now confirms Story 02 stays
  visibly selected after Back and the next tap reopens it. The combined
  candidate passed 506 Python and 728 Hub tests, strict typing and build.
  The specific before/after receipt is `docs/audits/2026-09-04-feature-audit/NEWS_SELECTION_RECEIPT.md`.
  Full evidence and limitations: `docs/audits/2026-09-04-feature-audit/WAVE2_VISUAL_REVIEW.md`.

- Implemented scoped prompted-speech admission, exact typed message input,
  fresh-question recovery after invalid answers, and a VAD fence that preserves
  completed STT while rejecting speech already underway. The tool-to-PCM host
  journey reaches exact message review; 209 combined tests passed. Independent
  review reproduced and repaired queued-answer and awaited-send races that
  could cover a newer question. Live wiring still requires microphone-source,
  audio-epoch and exact presenter acknowledgment contracts; these host fixtures
  do not establish physical-glasses input or speech recognition quality.
  Design, official source boundaries and red/green logs are linked from
  `docs/audits/2026-09-04-feature-audit/PROMPTED_SPEECH_PLAN.md`.

- Added conversational feature and message-preparation tools to the shared
  assistant registry, exact QuickNote/transcript paging, and controller-owned
  message confirmation/status. Focused checks verify real SQLite note saves,
  timer changes, authenticated MCP calls, pending-action cancellation and
  reader state. Review caught and repaired swallowed conversation and missed
  Home/disconnect cancellation. Official contracts and retained regressions
  are linked in the feature remediation report. Sender execution now has a
  bounded lifetime and preserves exact body text; 26 lifecycle cases pass.
  The combined gate passed 315 Python and 720 Hub tests plus strict typing;
  a test-only TypeScript error was corrected and the build passes. Simulator comparisons,
  scoped prompted speech and the remaining audit findings are still open.

- Started repairs against the feature audit with failing regressions first:
  read-only live-WAL Messages access, QuickNotes discard cancellation on Back,
  Messages paging/reselection and late-read ownership, and native News list
  failure recovery through the existing visual renderer. Source-linked
  before/after controller and simulator evidence is tracked in
  `docs/audits/2026-09-04-feature-audit/REMEDIATION.md`; current-candidate final
  verification is pending, with no physical-G2 or deployment claim.
  The first combined run passed 80 Python tests, 713 Hub tests and strict
  mypy; its test-only TypeScript compatibility failure was corrected.
  Additional Messages Back highlight repair passed its failing regression
  and all 84 menu tests. Simulator acceptance remains pending.

- Added a read-only feature audit in `docs/audits/2026-09-04-feature-audit/`.
  Separate source reviews cover conversational actions/latency, Brainstorm,
  Agent Terminal, Messages, QuickNotes, Overview, News, Timer and navigation.
  The audit distinguishes implemented paths from incomplete interactions,
  reproduces live-WAL reading and QuickNotes/Messages state failures with
  synthetic data. Typecheck, 709 plugin tests and 303 focused Python tests pass;
  all 26 corrected simulator captures were inspected. A separate diagnostic
  reproduces native News list rejection and return to Overview. The report
  records 34 bounded findings; no product fix, real send, package upload or
  physical-G2 acceptance is claimed.

- Packaged the Hub plugin's startup and reply-path recovery as `0.1.74`, the
  first package to carry it. The bounded native spine-label write no longer
  blocks networking before `MacLink.connect()`; an assistant reply renders
  through the native text path when no visual surface is up; and a microphone
  pause reserves a sequence position and clears the carried odd PCM byte, so
  resumed speech cannot join the pre-mute utterance on an unchanged socket.
  The shipped `0.1.73` package was built at 17:22 on September 3 and the
  fixes were written between 21:58 and 22:45, so the phone had been running
  a build that predated all of them.

- Corrected the manifest's stale LAN candidate. It named `192.168.12.200`
  while this Mac is on `192.168.1.10`. Probed against the live gateway: the
  old address times out during the opening handshake, the corrected one
  reaches the pairing gate. The route a release actually packages is the
  tailnet `wss://…/glasses` through Tailscale Serve, and that was dead for a
  separate reason — Tailscale was stopped. Started, it connects. A
  DHCP-assigned literal will go stale again; the tailnet name is the durable
  route and the LAN entry is a same-Wi-Fi convenience.

- Verification: 709 Hub tests, typecheck and bundle build pass, and all three
  dial candidates were probed against the running gateway. The concurrent
  voice-gap and Whisper cold-start work in this tree is NOT part of this
  release and remains uncommitted. No physical G2 run, no upload.

- Replaced the assistant's mind with the wearer's own subscriptions, and
  removed the metered-API path added earlier the same day in `d7b2ace`.
  `claude -p` on the Max plan and `codex exec` on the ChatGPT plan now fill
  the seat, spawned through the same `SubprocessAgent` the Brainstorm seats
  use, with `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
  `ANTHROPIC_BASE_URL` and `OPENAI_API_KEY` stripped from every launch, and
  each seat running in its own directory so this repository's CLAUDE.md is
  not read as project context. The `anthropic` SDK is gone from the
  dependencies, the lock and the virtualenv. `--agent claude|codex|mlx`
  picks the boot seat; "use codex for the assistant" moves it per turn; a
  missing or signed-out seat is skipped for the next, and the resident MLX
  model is the offline seat.

- Gave those CLIs the brain's tools over MCP rather than a second copy. The
  Even AI endpoint also serves `/mcp` (initialize, tools/list, tools/call)
  on loopback behind the same bearer token. Claude receives it through
  `--mcp-config` with `--strict-mcp-config` and `--allowedTools
  mcp__joat__*`; Codex through `-c mcp_servers.joat.*` with
  `--ignore-user-config`. `ToolBelt.anthropic_tools` is renamed
  `tool_definitions`, since nothing here speaks to an SDK any more.

- Verified against the real logins rather than a mock. The Claude seat
  called a belt tool through MCP and answered in 4.3 seconds. Three
  findings came only from running it: `--bare` ignores the OAuth login and
  demands a key, so it is not used; without `--strict-mcp-config` the CLI
  loads the wearer's whole global MCP list and costs about four seconds of
  API time per turn; and print mode has nobody to answer a permission
  prompt, so the tool must be pre-allowed. Known gap: Codex reaches the
  route and asks for the tool, but `codex exec` refuses it under "approval
  policy is never", so that seat currently answers without the belt.
  1746 tests pass, strict mypy is clean on 98 files.

- Gave the Codex seat its tools back. `codex exec` had been refusing every
  MCP call with "MCP tool call requires approval, but approval policy is
  never", because print mode has nobody to ask. The key is
  `mcp_servers.joat.default_tools_approval_mode="approve"`, scoped to the
  brain's own server. A/B against the live CLI with the model's discretion
  removed by an explicit order to call the tool: without the key, no
  `tools/call` and the refusal relayed to the wearer; with it, the tool ran
  and its text reached the answer. Codex's own session record shows the turn
  still running `approval_policy: never` with `sandbox_policy: read-only`,
  so the per-tool gate is gone without the sandbox widening. Alongside it,
  `-c features.plugins=false`: `--ignore-user-config` does not suppress
  plugin-sourced MCP servers, so every turn was opening failing OAuth
  connections to the wearer's Cloudflare and RunPod servers — two per turn
  measured, zero after. A voice turn has no business dialling either.

## 2026-09-03

- Corrected September 3 zero counts to mean no usable glasses connection,
  not a successful voice recovery. Bounded the native spine-label write that
  could block networking before first light. The official simulator then
  exposed dropped replies on text fallback; restored streamed replies and
  retained-page dismissal there, including a readable native-list reply page.
  A delegated review reproduced a delayed reply covering consent; confirmation
  now waits for that write before presenting its exclusive gate. Verification:
  110 focused Hub tests and typecheck passed, with an official-simulator
  injected-startup-hang/audio-gap run visibly showing both replies. This Mac's
  gateway was absent and Tailscale stopped at read-only inspection; physical
  reconnection remains unverified. Evidence: `docs/VOICE_GAP_RECOVERY.md` and
  `docs/plugin-startup-recovery-evidence.json`. No package or upload.

- Repaired voice continuity across microphone pauses and replacement Hub
  connections. The orchestrator now uses connection generation, clears partial
  speech even when counters happen to line up, and protects completed active
  STT work from a new ingress reset. Mic stop clears the odd PCM byte and
  reserves a sequence boundary; real gaps still discard partial speech and
  retain the existing wake-word/privacy gates. Independent source/log reviews
  found all 398 historical resets immediately followed new connections, with
  no matched gateway gap warning; the actual reconnect causes remain unproven.
  Focused verification passed 135 Python tests, 104 Hub tests, strict mypy and
  Hub typecheck. The official Even Realities Simulator visibly displayed
  “First turn preserved” and “Voice recovered” using exact synthetic PCM and
  a held local STT fixture. Source/simulator evidence and physical limits are
  retained in `docs/VOICE_GAP_RECOVERY.md` and its receipt. No package, upload,
  installation, live app restart or physical-G2 acceptance was performed.

- Fixed resident WhisperKit cold-start session lock-in. The foreground wait
  remains 30 seconds, but the same adapter/child can finish loading behind
  temporary batch STT; exited children retry after a cooldown. A real isolated
  CLI probe first passed its canary at 80.17 seconds, while the controlled
  production-budget subprocess regression recovered with the same child.
  Complete utterances keep one backend, shutdown reaps resident/batch children,
  canary workers are joined before model changes, and temporary batch uses the
  exact selected model directory. A delegated read-only lifecycle review found
  worker-ownership and close races that are covered by regressions. See
  `docs/WHISPER_COLD_START.md` for evidence and the separate, still-open physical
  G2 latency gate. Final source verification: 229 tests passed (43 STT-focused),
  strict mypy clean on 98 files, and the real 30-second-budget fixture passed.
  No pairing/caller edits, package, upload, installation or
  production restart were performed by this task.

## 2026-08-31

- Prepared JOAT `0.1.40` as the uniquely versioned phone-installable
  navigation release. The prior upload replaced an existing `0.1.39` build
  under the same version, which did not give an already-cached phone a new
  version to fetch. This release retains the Even SDK phone-location source,
  walking/cycling route flow, and private tailnet `/glasses` bridge while
  giving the Even app an unambiguous upgrade target.

## 2026-08-30

- Repaired and causally verified the first-level Planning entry that could
  remain on `Loading recent planning…`. The custom sidebar now retains its
  visible Planning cursor when the simulator/firmware reports a stale
  protobuf-zero List index, and the development-only live-menu arm waits for
  the simulator's late startup press before exposing a selectable row. The
  official simulator acceptance now requires menu → Planning selection →
  authoritative Mac month → week → mixed day → native item detail. Three
  isolated Planning-only runs produced identical frame digests; the driver
  first observed a differing framebuffer 117–123 ms after Planning selection
  and 59 ms after the click that requested the Mac-backed month. These are
  screenshot polling observations, not renderer or physical-lens onset. This
  is source and official-simulator evidence only; no package,
  upload, installation, live EventKit read, or physical-G2 claim was made.

- Prepared JOAT `0.1.39` as a coherent walking/cycling navigation release.
  A configured `MAPBOX_ACCESS_TOKEN` or legacy `MAPBOX_API_KEY` now enables
  the local route provider without a second hidden experimental flag. The
  unavailable-provider voice state consumes only explicit navigation cancel
  commands, so unrelated JOAT requests continue to their owning capability.
  The Hub plugin uses Even's phone-location stream as the wearer-position
  source, starts the continuous SDK subscription before attaching the listener,
  and does not block live fixes behind the optional one-shot initial read.
  Mapbox remains the route/search provider; it is not used as a replacement
  for the Even SDK location source.

- Replaced the remaining Calendar/Reminder safe-write poster treatment with
  the same compact flat language as Planning and Messages. Reviews now use one
  left reading axis and one static gesture footer; lifecycle results retain
  only the evidence hierarchy and Back/automatic-return copy. The wide action
  band, countdown bar, lifecycle progress strip, and visual-only one-second
  confirmation repaint loop were removed. The actual eight-second decision
  timeout remains in the input gate, while unchanged confirmation pixels no
  longer trigger four complete image-tile retransfers. Left-temple double
  press now provides the footer's truthful Back/refusal path in addition to
  both swipe directions. A fresh official-simulator run causally passed all
  47 Calendar/Reminder states with a 172 ms
  maximum first framebuffer change, and the retained 576×288 frames were
  visually compared with the approved compact reference. No package, upload,
  installation, native EventKit write, or physical-G2 claim was made.

- Aligned the authoritative HUD contract with the shipped unified Planning
  implementation. Calendar and Reminder reviews now document the compact,
  flat, left-aligned renderer rather than the obsolete boxed card, and the
  wearer flow explicitly records month → week → mixed day → read-only detail,
  including anchored restoration after safe-write outcomes. The current
  official-simulator acceptance is documented as 47 causal screens rather
  than the stale 34-transition count. This is documentation of verified
  source/simulator behavior; it does not claim a native alarm, package install,
  or physical-G2 acceptance.

- Added a Mac-app Settings surface for the remote Hermes Agent Terminal
  Gateway. It accepts only a canonical WebSocket origin, keeps the agent fixed
  to `hermes`, and writes the exact owner-only, secret-free `openclaw.json`
  schema the Python runtime admits. The form rejects a remote `ws://` URL,
  URL credentials/paths/queries, and Hermes’s port-9443 control dashboard;
  save is atomic and never follows a symbolic link. It stores no pairing token
  and explains that restart plus an endpoint-specific visible pairing approval
  may be needed. SwiftPM and generated-Xcode native regressions passed, along
  with the Python OpenClaw runtime/Gateway/native suite. No Mac-mini
  connection, package, upload, install, or release action occurred.

- Explicit nearby-place questions now prefer a fresh, request-bound
  high-accuracy phone fix from Even Hub before safely falling back to the
  already-fresh location cache. Ambient tracking remains medium-accuracy and
  battery-bounded; coordinates stay private between the plugin and the local
  search provider, never entering the answer or diagnostic receipt. This is
  source/fixture evidence only—the official simulator cannot prove iPhone GPS
  delivery or physical-G2 walking behavior. No package, upload, install, or
  release action occurred.

- Removed always-on exact voice timing from normal logs. Successful STT no
  longer emits queue, POST, or audio-duration measurements, and the voice
  orchestrator no longer logs an utterance duration. The existing
  disabled-by-default voice-latency receipt remains the sole persisted timing
  evidence: fixed stages, coarse buckets, and no audio, transcript, response,
  timestamp, or identifier. This preserves the non-dropping resident-STT
  request behavior; no package, upload, install, or release action occurred.

- Corrected the lower-level physical Brainstorm acceptance runner so its
  no-tool Hermes stream uses the same production `HudSink` as the app instead
  of discarding chunks and painting a fixed status. A live-Hermes receipt now
  fails closed unless it has exactly two distinct, ordered Brainstorm-running
  frame SHA-256 digests before the Terminal transition; it stores no remote
  text, prompt, pixels, or lens video. The official simulator visually
  exercises the same production HUD path, while physical optics remain a
  separate video-backed acceptance gate. No package, upload, install, or
  release action occurred.

- Added a read-only, content-free home-Mini Tailscale Serve preflight. It
  verifies only the persisted private topology required for a glasses-first
  JOAT brain: the preserved OpenClaw/Hermes root handler, `/glasses` proxied
  to the loopback-only JOAT gateway, HTTPS on 443, and Funnel disabled. It
  emits fixed readiness reasons without exposing existing routes, hostnames,
  pairing tokens, G2 traffic, or Hermes content. This detects the concrete
  failure where `/glasses` falls through to the OpenClaw control page; it does
  not configure Serve, start JOAT, package, upload, install, or claim a G2
  connection.

- Corrected JOAT's remote OpenClaw handshake identity. The Tailnet-connected,
  signed device had incorrectly declared OpenClaw's reserved direct-loopback
  `gateway-client` / `backend` helper identity. It now uses the protocol's
  canonical `cli` / `operator` identity while retaining the same paired
  device key, bounded read/write scope request, and no-tool policy. Existing
  Mini pairings may request one visible metadata reapproval; this cannot grant
  more scopes or bypass pairing. Focused Gateway regressions cover the exact
  signed payload and connect frame. No Mini configuration, package, upload,
  install, or G2 surface changed.

- Marked `setup_brain.sh --service` as the legacy **core-only** Python service
  it actually is. Its launchd process has no JOAT.app native bridge and cannot
  host the Mini's Keychain-backed Hermes/Agent Terminal identity. The installer
  and historical next-steps guide now direct the unattended glasses-first Mini
  to the signed JOAT.app's Launch at Login path instead. This is a deployment
  guard only; it does not alter a live engine, package, or G2 surface.

- Replaced the standalone OpenClaw pairing probe's impossible direct native-
  bridge connection with a content-free, app-owned Mini readiness proof. The
  bridge intentionally admits only JOAT.app's Python child; the probe now reads
  its short-lived allowlisted status projection to verify the exact Gateway
  host, operator scopes, dedicated Hermes session, and no-tool preflight. It
  never opens Keychain, creates a session, sends a prompt, or emits a
  transcript/payload. Focused OpenClaw status/session tests and strict mypy
  passed. This is setup proof only, not a package, upload, or physical-G2
  claim.

- Built a clean-source **JOAT v0.1.38** EHPK candidate containing the
  committed unified Planning/Reminder fixes that postdate v0.1.37: legacy
  Reminder-row selection now opens the live nested planner, and due-HUD
  delivery uses its bounded acknowledgement path. The artifact is 78,592
  bytes with SHA-256 `01ca6a52479ec97b8375f357d1bf929ec70520d59a00dfe5adaa4ee2b635e143`;
  492 Hub tests, typecheck, production build, and pack passed from the clean
  archive. Portal/device acceptance remain distinct gates; this entry makes no
  installation or physical-G2 claim.

- Replaced the native Calendar/Reminder acceptance driver’s obsolete
  console-receipt dependency with a private official-simulator framebuffer
  transition followed by the existing 600 ms stability window. The harness now
  rejects console-only output as visual proof and retains only private hashes
  and screenshots for review; this changes neither EventKit, Focus, nor macOS
  notification settings. A live native write/alarm run remains a separate,
  operator-controlled gate.

- Reworked **Messages · Conversation** as a six-row, aligned transcript: one
  compact history position line, a divider, then flat sender/body rows. Incoming
  and outgoing text stays distinguishable by truthful label and brightness, but
  each message no longer wastes the lens on a rounded card. The native G2
  list-event tap now also dismisses a transient Home notification before it can
  select or relay a hidden capability row. Regressions cover notification
  dismissal, six-row density, and the older-message viewport; the full Hub
  suite passed 490 tests with typecheck and production build. An isolated
  official-simulator Canvas launch could render but correctly could not attach
  to its temporary loopback gateway because that origin is not package
  allowlisted, so it is not claimed as live-route or physical-G2 evidence. No
  package was created, installed, or uploaded.

- Repaired the Reminder-due HUD delivery boundary exposed by the official
  simulator. Its escaped-action hint had exceeded the protocol's fixed
  46-character limit, so the generation-bound client acknowledgement was
  never sent and the durable occurrence correctly remained pending. The hint
  is now within the wire bound and its exact serializer path is tested. The
  isolated Calendar/Reminders run completed all 34 causal HUD states,
  including the monitoring and due surfaces. Its retained latency evidence
  now uses only an official-simulator framebuffer change followed by a 600 ms
  stability window—no renderer console, bridge-transfer trace, or packaged
  telemetry. Every RGBA frame was converted to an optical preview and visually
  reviewed. This is simulator/fixture proof only; it is not an EventKit write,
  macOS notification banner, physical-G2, or wearer-attention claim.

- Corrected an unsupported Brainstorm “scroll to read” assumption. The
  production image HUD retains a bounded live activity window and reserves a
  downward swipe for cancellation; it does not have pageable transcript
  history. Brainstorm turns therefore again require a GIST-first, under-120-
  word response so latency and first-glance readability stay bounded. A real
  pageable transcript remains a separate interaction feature, not a claim
  hidden in a prompt change.

- Added an owner-only, explicitly opt-in voice-latency receipt
  (`--voice-latency-diagnostics`) for a controlled live run. It correlates a
  process-local turn ordinal with fixed voice stages and coarse buckets only;
  it never persists speech, transcripts, answers, timestamps, identifiers, or
  lens claims. The write is bounded, atomic, private-permissioned, and cannot
  disrupt a reply if persistence fails. Focused voice, STT, and startup tests
  passed (72); this prepares the next measured run and is not a latency or
  physical-G2 result by itself.

- Hardened the private home-Mini EHPK release path. `npm run pack` now requires
  explicit `JOAT_RELEASE_WS_URLS` and rejects raw development transport,
  unlisted origins, non-`/glasses` paths, and duplicate candidates before Vite
  can read the ignored `.env`. This makes the intended Tailscale Serve WSS
  route a reviewable package input; it does not build, upload, install, or
  prove a device package by itself.

- Made the real-G2 **Brainstorm / Agent Terminal acceptance route explicitly
  diagnostic-only**. A production `begin_acceptance` gesture is now refused
  before it can allocate a session unless the app-owned engine was deliberately
  launched with `--g2-diagnostics`; ordinary glasses-first Brainstorm use
  needs no Mac interaction. The isolated simulator harness remains a separate
  explicit test path. Focused acceptance and engine-policy tests passed; this
  is not physical-G2 evidence or a package release.

  A fresh isolated official-simulator run exercised that same armed service
  route and its nine visible states; the receipt contains only fixed gesture
  metadata and frame hashes. Visual review confirms that each frame is legible
  and the flow is causal. It also caught a separate Hub release issue: the
  normal Ready frame still advertises `right/ring x2: test`. The engine now
  rejects that unarmed action, but the Hub owner must remove that diagnostic
  affordance before packaging; no physical G2 or package claim is made.

- Added a one-time **Private Tailnet home brain** engine mode for an unattended
  JOAT Mac mini. The app persists the setting across Login Item launches and,
  after a normal engine restart, passes a narrow flag that binds the glasses
  gateway to `127.0.0.1` while explicitly retaining pairing for traffic that
  arrives from a Tailscale Serve reverse proxy. The known Mini's correct
  port-443 WSS origin is now declared in the package manifest; the old
  misleading `wss://…:8787` entries were removed. This prepares a private
  deployment but does not configure the Mini, upload a package, pair glasses,
  or prove a physical connection.

- Completed the actual glasses-first **Brainstorm** topic path: tapping the
  Brainstorm HUD arms one `TOPIC CAPTURE` utterance, and the orchestrator now
  consumes it before the ordinary wake-word gate only when the complete VAD/STT
  utterance is verified as **glasses** audio. Phone, mixed, missing, and
  unknown-source audio cannot satisfy that promise and leave the visible capture
  state armed. Repeated taps remain harmless; cancellation and connection
  replacement clear the capture. The focused Brainstorm/OpenClaw/gateway suite
  passed 309 tests and strict mypy. The current official simulator was driven
  through all nine Ready → Topic Capture → Cancel → Acceptance → Running →
  Agent Terminal → Return → Reconnect → Cancel HUD states, with every raw RGBA
  frame converted to and visually reviewed as its proper green-on-black optical
  interpretation. Per Even's current documentation, that is simulator evidence
  for layout and event logic only—not physical-G2 timing, source routing, or
  lens-legibility proof.

- Corrected the live Brainstorm HUD's first-token legibility: `Thinking…` now
  inserts one readable boundary before a first lexical Hermes chunk but keeps
  all later stream chunks byte-contiguous, so an ordinary token split cannot
  be changed into different prose. The isolated official-simulator runner now
  snapshots Hub application source instead of symlinking the live `src` tree;
  a concurrent HMR edit can no longer replace its WebView/socket in the middle
  of a causal gesture run. The frozen-source simulator pass visually confirmed
  all nine Brainstorm/Agent Terminal states, including the repaired running
  text. The retained receipt remains content-free and simulator-only.

- Fixed the live assistant's nearby-place gap: the Mapbox provider and fresh
  iPhone fix were already available, but the local model had never selected
  the advertised lookup. Explicit locality requests such as **coffee near
  me**, **nearby restaurants**, and **closest pharmacy** now invoke the
  existing private, request-bound phone-location lookup deterministically;
  ordinary questions still use model-selected tools. Tool diagnostics now
  retain only the tool name and result length, never a spoken place query or
  provider error text. Focused location/tool verification passed 59 tests and
  strict mypy. This source change awaits the user's controlled JOAT restart
  and glasses test; it is not a live nearby-search, physical-G2, or package
  claim.

- Hardened the opt-in **Brainstorm / Agent Terminal** G2 acceptance route
  against controls buffered from a replaced WebSocket: the authenticated
  gateway now binds every action to its current connection generation, and the
  acceptance controller rejects stale gestures. Its content-free diagnostic
  observer now establishes causal gesture/frame evidence before the
  best-effort receipt write. The official simulator visibly exercised and
  reviewed seven distinct optical states—ready, armed, running, terminal,
  returned, reconnect-restored, and cancelled—without retaining a transcript
  or OpenClaw payload. Focused Python verification passed 263 tests; the Hub
  suite passed 460 tests plus production build. The local `0.1.35` EHPK was
  rebuilt with the SDK 0.0.14-required Even App `2.2.9` minimum encoded in
  source, so an offline registry fallback cannot weaken it. No portal upload,
  installed-G2, physical-lens, or live Hermes proof is claimed.

- Scoped the glasses Hub pairing store to each approved Mac-brain origin and
  persists the last healthy candidate, so a stale credential from one home
  mini cannot be offered to or erase another mini's pairing. Hub verification
  passed 459 tests plus typecheck/build; an official-simulator run visually
  exercised Brainstorm → Agent Terminal → reconnect → cancel with seven
  reviewed 576×288 optical frames. The accompanying deployment guidance now
  distinguishes the raw core-engine service from the JOAT.app native bridge
  required for Keychain-backed OpenClaw/Hermes credentials. No package was
  created or uploaded; physical G2 lens/source proof remains separate.

- The no-send **Messages** simulator proof now drives every visible production
  selector itself—sidebar **click**, conversation **down + click** or compose
  **click**, history paging, confirmation control, and result dismissal—and
  records each causal action truthfully. Each retained transparent 576×288
  framebuffer now has an owner-only alpha preview for visual review; the
  runner fails rather than claim reviewable proof if that preview cannot be
  produced. This remains simulator-only with a `RecordingSender`.

- The no-send official-simulator **Messages** harness now retains and verifies
  every causal 576×288 frame—not only confirmation/result—through Sidebar,
  Conversations, selected Conversation/Compose, confirmation, result, and
  post-dismiss return. The receipt records only frame hashes and documented
  simulator actions; it never records fixture message content or recipient
  handles. Fresh accepted-reply evidence shows the returned frame exactly
  matches the selected Adaeze conversation after **TAP OR SWIPE TO DISMISS**.
  This remains simulator proof with a `RecordingSender`, not a physical-G2
  interaction or real Messages.app send.

- Hardened the no-send **Messages** official-simulator harness so every reply
  outcome has an explicit production-controller contract: **TAP · Confirm**
  for submitted/uncertain/failed, **SWIPE · Cancel** for cancellation, and a
  deliberate eight-second silence for timeout. The harness now clears stale
  console output, requires the Hub's content-free `confirmation-visible`
  receipt before it can inject an outcome control, and retains the confirmation
  and result frames with a privacy-minimized receipt. Focused verification
  passed 20 tests and strict mypy. Final visual evidence in
  `messages-compose-confirmation-final-20260830/` proves the fixture-only
  New message → confirmation → delivery-unverified result; it does not prove
  physical-G2 source delivery or a real Messages.app submission. A subsequent
  controlled simulator run retained Messages → Adaeze selection → visible
  **SWIPE · Cancel** confirmation/result with no sender call; the reliable
  procedure waits for the list frame between individual controls. No package
  was created or uploaded.

- Opening a fresh **Messages** picker now disarms any previously selected
  conversation or New message compose context before it draws. A subsequent
  spoken reply therefore cannot target a conversation the wearer no longer
  sees; the replacement picker, access guidance, and retry state all start
  clean. This is source/test hardening only—no package was created, installed,
  or uploaded.

- Extended the glasses **Messages → Conversation** surface from a three-row
  snapshot to Mac-private, request-bound older/newer pages. Attachment-only
  and unread conversation rows are now distinguished from notification cards,
  and outgoing rows truthfully label sent, delivered, or read when that
  Messages-store metadata exists. The Hub forwards a scroll only at the
  Mac-authored **down for older** / **up for newer** boundary, suppresses
  duplicates while a page is loading, and never puts a chat cursor or recipient
  on the glasses. Full Hub verification passed 442 tests plus production
  typecheck/build; focused Messages Python verification passed 39 tests and
  strict mypy. Official-simulator evidence retained in
  `messages-history-20260830/` shows Latest → Older → Latest returned using a
  disposable store and no-send `RecordingSender`. Physical-G2 temple ingress,
  lens legibility, and real Messages.app sending remain separate, unrun gates.

- Bounded streamed JOAT assistant rendering to one active four-tile paint and
  one newest tail frame, so model chunks cannot build a stale optical backlog.
  Wearer dismissal now waits for the active upload, cancels any completion-
  scheduled tail, and restores the underlying Messages, Navigation,
  Brainstorm, or productivity surface. Long assistant text is fitted and
  tail-clamped within the existing green HUD rather than changing the visual
  design. The official Even Hub simulator retained a complete 576x288 overlay
  frame and the fixed content-free `assistant_overlay` receipt; the first run
  also exposed and then verified the exact absent-type left-source startup
  announcement regression. Full Hub verification passed 438 tests plus the
  production typecheck/build, and a sealed security diff review closed all 27
  changed review items with no reportable findings. The signed app was not
  restarted, packaged, or uploaded; physical-G2 voice timing and optical proof
  remain separate gates.

- Added a direct **New message** row to the glasses Messages list, including
  empty-inbox access, Mac-local contact search that fails closed on missing or
  ambiguous recipients, exact recipient/body confirmation, and the existing
  delivery-unverified Messages submission receipt. The compose surface gives
  voice and microphone guidance and clears safely on Home or disconnect. The
  official Even Hub simulator produced five visually inspected, distinct
  576x288 frames for Sidebar → Conversations → Compose → Confirm → Result,
  using one fixture contact and `RecordingSender`; it did not read the user's
  Contacts database or send a real message. Combined verification passed 872
  Python tests, strict mypy across 80 source files, 438 Hub tests, the Hub
  typecheck/production build, and 48 XCTest plus 3 Swift Testing cases. No
  package was created or uploaded; physical-G2 input/lens proof and an
  explicitly authorized real Messages.app send remain separate gates.

- Reduced structural voice latency without adding a response-dropping timeout:
  the resident Whisper server now requests one text-only decode with zero
  temperature fallbacks, delayed SDK audio flushes are split into exact 200 ms
  sample messages without changing PCM order, VAD hangover uses PCM duration
  instead of callback count, and the reply engine sends an eight-character
  first chunk before returning to its normal 32-character batches. A local
  acceptance observer is disabled by default and exposes only fixed stages and
  coarse time buckets; no transcript, reply, frame, device identifier, or exact
  timing is retained. Focused Python tests passed 64 cases, strict mypy passed,
  and the full Hub suite passed 437 cases. A full Python snapshot passed 858
  cases but had 11 failures in the concurrently owned, in-progress Brainstorm
  acceptance/protocol lane. No package was built or uploaded, and no simulator
  or physical-G2 latency result is claimed; a restarted app and synchronized
  lens run remain required.

- Completed the production Messages wearer flow from the first-level G2
  sidebar through stable conversation selection, bounded incoming/outgoing
  history, voice-reply guidance, bound confirmation, and truthful submitted,
  uncertain, failed, access, timeout, Home, and disconnect states. The Hub now
  routes physical `listEvent` selections with the owning request, suppresses
  duplicate taps, and renders a conversation separately from notifications.
  JOAT.app adds a privacy-minimized Full Disk Access status/recovery surface.
  The full Hub suite passed 407 tests and its production bundle built; focused
  Python Messages suites passed 47 tests and all Swift package suites passed 48
  XCTest plus 3 Swift Testing cases. Five inspected official-simulator frames
  prove the forward path with a disposable database and recording sender. No
  package was created or uploaded; physical-G2 ingress, Home return, live voice,
  optical legibility, and real Messages.app submission remain device gates.

## 2026-08-29

- Routed the in-app physical-G2 Hermes acceptance stream through the production
  `HudSink`. The owner-only receipt now requires exactly two ordered, distinct
  Brainstorm running-frame hashes before the Agent Terminal transition, retains
  no response text, and fails closed when a normal wearer session is active.
  The official simulator flow remains separate support evidence; physical lens
  acceptance is still required. No package was built or uploaded for this
  change.

- Released the next canonical private Even Hub artifact under the existing
  `com.akinwale.joat` project as `v0.1.26`, rather than attempting to upload
  a changed artifact under the already-existing `v0.1.25` version. The fresh
  package was built from clean `d1c1185`, is 72,653 bytes, and has SHA-256
  `09cd82bf131ed4b2fa00e2336f7b4abd8ed15b9ac8979d5b7c0971a45ff231e3`.
  The full Hub suite (389 tests), Gate 0 protocol/trace checks (32 tests),
  typecheck, production build, and private-stage package build passed.
  Portal upload/install and physical-G2 location proof remain separate gates.

- Fixed the installed Finder-launched JOAT app's experimental-navigation
  configuration handoff. It now forwards only the selected repository's
  Mapbox token (`MAPBOX_ACCESS_TOKEN` or legacy `MAPBOX_API_KEY`) to its Python
  child and treats that local configuration as explicit navigation opt-in;
  unrelated `.env` values never enter the engine environment. Regression tests
  cover legacy-key mapping, inherited-setting precedence, and absent-config
  behavior. This source change requires a new Mac-app build/restart before
  the running engine can use it; it does not constitute routing or G2 proof.

- Repaired the physical G2 image transport: the visual renderer now sends one
  Gray4 value (`0..15`) per pixel at the SDK boundary, rather than packing two
  pixels into a byte. The previous packed payload could be acknowledged by the
  phone bridge while producing no lens-visible HUD. Release this as v0.1.22;
  it requires a fresh private-build install and physical lens check.

- Rebuilt the current integrated Hub source as private `com.akinwale.joat`
  `v0.1.21`, 69,397 bytes, SHA-256
  `b213de98185f3f347883ee8c961706638728443cf304d1695c20962a8463b661`.
  This supersedes the earlier `v0.1.20` candidate because the shared source
  changed after it was packaged. Full verification before the rebuild: 348 Hub
  tests, Hub typecheck/production build, and 743 Python tests. It still awaits
  existing-project upload, phone installation, and physical-G2 evidence.

- Built the next canonical private JOAT package from the integrated Hub source:
  `com.akinwale.joat` `v0.1.20`, 69,398 bytes, SHA-256
  `70b8dcd2d8347bac44e2d672a1ae81fc724c0e390bd0fbbf526d8754a2a73ffe`.
  The fresh-stage pack excludes development fixtures; focused opt-in Gate 0
  diagnostics/protocol tests pass and the full Hub suite passes 348 tests.
  This is a build artifact only: it has not yet been installed or accepted on
  a physical G2, and no simulator or bridge acknowledgement is presented as
  lens-visible proof.

- Drove the production navigation recovery path through the official Even
  simulator: canonical-place confirmation → mode selection → high-accuracy
  location request → unavailable-location recovery → retry → cancel to
  Overview. Every simulated gesture produced the expected semantic action in
  the real Hub event router and Mac `NavigationSession`. During this run, an
  immediate location rejection was found to replace “Getting your location”
  before it could be perceived; the Mac session now retains that state for up
  to 0.8 seconds only when the request fails immediately. This does not delay
  a successful GPS request or route lookup. The simulator cannot emulate the
  physical left-temple menu gesture or verified phone GPS, so sidebar entry,
  walking/cycling, guidance, and lens visibility remain real-G2 gates.

- Repaired the packaged-G2 visual HUD contract after a private `v0.1.18`
  install showed the legacy text header/footer while the simulator showed the
  four-tile renderer. The image-first startup and rebuild pages now keep their
  required space-only event-capture text container behind every image tile,
  matching the current Even hardware guidance instead of relying on the
  simulator's tolerance for an empty foreground text layer. Visual container
  names are also now unique and at most 16 characters; the shipped page used
  names as long as 20 characters even though the packaged host limit is 16.
  Focused regressions failed on both hardware-contract violations and now
  pass; the five visual boot/render suites pass 137 tests, the production
  bundle builds, the full Hub-plugin suite passes 336 tests, and the official
  simulator framebuffer shows the modern rounded header/footer.
  The release owner rebuilt this repair as private `com.akinwale.joat`
  `v0.1.19` (68,794 bytes, SHA-256
  `7c6a5868daa6cc7316b6de60fb0ae96fa5705c3a858e7061778e7b0cf6596b49`)
  and the existing JOAT Even Hub project visibly lists that build. Phone
  installation and physical-lens confirmation remain open; portal submission
  and simulator frames are not substitutes for either.

## 2026-08-28

- Closed the installed-engine OpenClaw → Hermes → JOAT Calendar simulator gate.
  A user-observed Mac app status confirmed Calendar and Reminders full access;
  the remaining failure was routing, not TCC. Whisper's common “asks Hermes”
  rendering now reaches the same explicit candidate-only route as “ask Hermes”.
  A development-only, loopback-only, tokenized PCM fixture kept the official
  Even simulator as the sole glasses connection; production build inspection
  proves the fixture route and content are absent. Hermes proposed the synthetic
  “JOAT Hermes Green Orbit” event, JOAT rendered the exact Calendar/date/time/
  duration/zone preview, the simulator accepted it, EventKit logged one event
  add, and the durable action/attempt finished `succeeded` with
  `eventkit_saved_and_read_back` / `read_back_matched` after payload purge. A
  native Calendar query found exactly one 10:00–10:30 event, exact-title cleanup
  removed one, and a second query found zero. Owner-readable framebuffer and
  receipt evidence is retained under
  `~/Library/Application Support/JOAT/acceptance/hermes-calendar/20260828-143952-green-orbit/`.
  This is official-simulator proof, not physical-lens evidence. The earlier
  two-Calendar-record cleanup discrepancy was separately resolved from macOS
  logs: JOAT saved one event and `calaccessd` added one Calendar projection for
  the scheduled Reminder; it was not a duplicate JOAT event write. The older
  external native-acceptance driver remains incompatible with the exact
  app-launched-PID bridge boundary and must not be used to weaken that boundary.

- Re-ran the opt-in, content-free Gate 0 location probe against the current
  `com.akinwale.joat` v0.1.14 plugin without issuing any navigation action.
  The official simulator retained complete, four-quadrant 576×288 frames for
  Ready, the fixed confirmation marker, and restored Ready; a separate v2
  receipt recorded one system tap, two successful page transitions, and 28
  successful ordered tile uploads. The simulator rejected the requested high
  accuracy profile and delivered no samples, which remains simulator-only
  capability evidence rather than a packaged-phone finding. A timing
  falsification run also reproduced bridge acknowledgement before framebuffer
  change, so callback and pixel proof remain separate. Packaging now builds in
  a fresh private staging directory: concurrent DEV PCM fixtures can remain in
  the shared `dist/` tree without entering `joat.ehpk`. The resulting canonical
  package is 65,964 bytes with SHA-256
  `ff43e4494a9cf2c20204255fe42b64cef97a4f20e5c8812c0e748dfe016ab282`.
  Physical walking/cycling receipts and synchronized lens video remain open.

- Implemented an experimental, explicit-invocation walking/cycling navigation
  lane without treating comparison-plugin distances as release truth. Spoken
  destinations resolve to bounded canonical candidates, require G2
  confirmation and an explicit mode, then request a fresh origin and a
  generation-bound Mapbox route. The phone keeps rapid fixes local, matches
  within a bounded progress corridor, distinguishes uncertain, wrong-way,
  off-route, rerouting, and arrival states, and sends only semantic progress
  except for one reroute coordinate after sustained evidence. A deterministic
  production-path replay now covers walking noise/off-route debounce and
  cycling wrong-way recovery/arrival; a cancellation-resistant provider test
  rejects a late obsolete reroute. Generation-bound bridge-acknowledgement and
  origin-failure receipts now prevent the Mac from silently waiting on a view
  or unavailable phone location. Awaited HUD/location work is lifecycle-bound,
  and navigation uplinks are session-only rather than queued across reconnects.
  Revocation also treats a four-tile paint already in flight as HUD ownership,
  so Overview is queued behind any obsolete frame, and reroute provider work
  waits for the exact rerouting view's bridge acknowledgement.
  Provider responses are rejected before preview unless their selected mode,
  route geometry, ordered steps, wire bounds, coordinates, distances, and ETA
  are usable. Socket replacement cancels any route the replacement renderer
  cannot possess. The official simulator's first capture
  proved that a four-tile bridge acknowledgement could still precede a missing
  quadrant, so the acceptance harness now requires a one-second stable
  framebuffer and nonempty pixels in all four image quadrants. Its v10 recapture
  produced 11 distinct, complete 576×288 frames while continuing to label
  physical-lens evidence as not captured.
  Interaction copy now matches Click/Up/Down ownership, and stopping before
  route installation releases the stale navigation HUD. Mapbox remains behind
  `JOAT_EXPERIMENTAL_NAVIGATION=1`; its destination/proximity cloud boundary is
  documented. A local `0.1.14` `.ehpk` was rebuilt under the existing
  `com.akinwale.joat` package ID; it was not uploaded. Recorded field traces, a
  live provider call, packaged GPS, and synchronized physical-lens cue latency
  remain open and are not claimed.

- Fixed the More → Brainstorm ownership gap: starting a live Brainstorm now
  sends an explicit default-surface rebuild before its first frame, clears the
  plugin menu's logical input ownership, and uses Brainstorm rather than
  Debate in wearer-visible HUD, terminal, and saved-snapshot headings. The
  full Hub suite passes 270 tests and its TypeScript/Vite build; the full
  Python suite passes 678 tests with strict mypy. Real-G2 interaction proof
  remains pending.
- Wired the first Hermes-to-productivity slice without giving Hermes EventKit,
  journal, delivery, Telegram, or OpenClaw tool authority. An explicit
  “ask Hermes to draft a calendar event…” command permits one 20-second total
  local envelope, with separately bounded generation, abort, stream cleanup,
  WebSocket send, and connection close, and accepts only `NO_PROPOSAL` or one exact
  six-field `JOAT_CALENDAR_V1` object. JOAT rejects prose, unknown/duplicate
  fields, Unicode controls, invalid civil time/time zone, oversize output,
  timeout, and widened
  semantics before creating a draft; unsafe controls in the wearer request
  are rejected before contacting Hermes. A valid candidate
  receives opaque local
  provenance and enters the existing EventKit container/time resolution,
  exact G2 preview, authenticated confirmation, attempt-before-write,
  independent read-back, and durable receipt path. The full Python suite and
  strict mypy pass;
  installed-app restart, live Hermes proposal, TCC, native write/read-back,
  simulator confirmation, and physical-G2 proof remain open.
- Split the existing Mac-mini `hermes` agent into two JOAT-local uses without
  installing a second Hermes: Brainstorm and Calendar proposals now open
  separate WSS connections, exact session labels, key namespaces, locks, and
  persistent transcript histories. Each connection's firewall rejects the
  other lane's keys before transmission. The exact release omits `agentId`
  from list/describe rows, so JOAT binds the agent through the owned
  `agent:hermes:` key plus the exact `hermes` identity returned by
  `tools.effective`. The Calendar lane is deliberately described
  as separate and persistent, not ephemeral: current upstream OpenClaw offers
  archive/delete lifecycle RPCs, but the live Mac-mini advertisement and
  non-admin archive-only contract still require runtime verification before
  JOAT may depend on deletion.
- Installed the corrected production bundle atomically with the previous app
  retained at `/Applications/JOAT-backup.Gp3EWu/JOAT.app`, then exercised two
  production launches. The live engine holds two established private-WSS
  connections to the same Mac mini, reports the three-seat Brainstorm ready,
  and confirms the separate Calendar proposal session ready. Exact-release
  source inspection corrected an invalid client assumption: OpenClaw
  `2026.7.1-2d2ddc43d0dc` omits `agentId` from list/describe rows, so JOAT now
  uses the owned `agent:hermes:` key plus `tools.effective`'s returned agent
  identity instead of requiring a field the server cannot send. A synthesized
  production-path voice command reached Hermes and created one
  `openclaw.hermes.proposal` action; with no G2 connected, confirmation refused
  it and left zero retained payloads, zero native attempts, and zero receipts.
  Unified TCC logs across both launches and the voice run contain no Documents,
  Full Disk Access, or Debug-principal request. A confirmed native EventKit
  write/read-back and physical-G2 confirmation remain open.
- Remediated the hostile-review cancellation and local-boundary findings.
  Spoken Stop now cancels the active Brainstorm task rather than waiting for a
  blocked participant to yield; remote abort and socket close have independent
  deadlines. Cancelled confirmations and restart-time `ready` drafts are
  invalidated and have their cleartext payload purged. The Swift bridge now
  admits only the exact app-launched engine PID and revokes that identity on
  exit, instead of treating every same-UID process as the engine. Hub downlinks
  are serialized in authenticated wire order within a per-socket generation;
  reconnect revokes the old generation, discards its queued work, cancels its
  confirmation gate, and fail-closes late audio, IMU, and high-rate location
  starts. The production visual painter preserves one device-write lane, so a
  permanently hung SDK image upload can still stall HUD progress rather than
  allowing a stale late frame to overwrite a newer one. Native one-shot
  write-capability proof and real-G2 acceptance remain open gates; the PID
  binding is not described as proof against a compromised engine.
- Added a repeatable, exact-title native Calendar/Reminders acceptance driver
  using the production safe-write controller, signed Mac EventKit bridge, and
  official Even Simulator HUD. One live run verified existing Calendar and
  Reminders full access, accepted both rendered confirmations, created one
  nonrecurring event plus one nonrecurring reminder, and matched native
  read-back including the absolute alarm. It retained an immutable private
  receipt plus a provenance-bound post-run amendment and reviewable
  framebuffer-alpha previews, removed every synthetic match, and restored the
  Mac app configuration. Acceptance remains blocked: no Reminders banner
  appeared during the due-plus-120-second window, and exact-title cleanup found
  two Calendar records after the bounded EventKit query found one. Notification
  settings and Focus were intentionally left unchanged.
- Began replacing the legacy loopback OpenClaw seat with an OpenClaw-first
  Brainstorm / Agent Terminal against the
  private managed Tailscale Serve origin
  `wss://admins-mac-mini.tail3a3363.ts.net`. The new protocol-v4 client rejects
  plaintext remote endpoints and embedded credentials, signs the exact
  challenge-bound device proof, requests only explicit operator scopes,
  validates `hello-ok`, correlates interleaved RPC/events, and keeps issued
  tokens out of status objects. The Mac app now owns one per-origin Ed25519
  identity and paired token in lock-gated, device-only Keychain records; Python
  can only use four same-user bridge operations and never receives the private
  key. A dedicated Agent Terminal foundation refuses Telegram/main sessions,
  requires the advertised session/history methods, checks `tools.effective`
  before every send, streams only its owned run, and aborts on event gaps,
  divergent replacements, or tool/approval activity. Focused verification is
  38 Python tests with strict mypy, four Swift credential/bridge tests, and a
  strict Developer ID universal archive. The verified build was installed with
  the prior bundle retained at
  `/Applications/JOAT.app.backup-openclaw-20260828-1008`; the app, engine, and
  mode-0600 bridge restarted cleanly. A live signed request created pending
  device approval `d2ce0e8c-0b89-4a2f-a258-df98dc1e23f9` for device
  `6d840a1425c8ec98f1ff91b8459175bbf6b8ed56a7cc5efd1cb64c7fae28ab3c`
  with only `operator.read`. At that checkpoint, manual Mac-mini approval,
  read-only `hello-ok`,
  Telegram coexistence proof, a dedicated no-tool agent, write-scope approval,
  history recovery, and application runtime wiring remain open; no Telegram
  bot token, poller, session, or configuration was touched.
- Completed the private-WSS, dedicated no-tool Agent Terminal seat acceptance
  lane on the private Mac mini. The approved
  `JOAT Brainstorm` device has only `operator.read`/`operator.write`; the
  dedicated `hermes` agent has an isolated workspace, no bindings, no memory,
  no subagents, and an observed effective 0/43 tools. Startup now adopts the
  one exact dedicated session instead of colliding on its label, revalidates
  the empty inventory before every send, accepts
  the release's optional empty `tools.effective.notices`, tolerates throttled
  chat-delta sequence values while retaining gateway-frame loss detection, and
  accepts only the exact targetless webchat provenance OpenClaw adds after a
  send. A live prompt returned the exact requested response, and restart
  retained one idle dedicated session while the existing main/Telegram runtime
  remained connected and unchanged.
- Removed the identified causes of the repeated Documents-folder prompt;
  final installed-app approval and two-relaunch acceptance remain pending.
  The unified TCC log proved that the 12:56 approval was recorded for
  `com.joat.shell.debug`, not the installed `com.joat.shell`; both had displayed
  the name “JOAT,” so the short-lived test host looked like the production app.
  Debug now displays as “JOAT Debug,” uses its own stable Developer-ID
  requirement, and detects its hosted XCTest environment before initializing
  Accessibility, the native bridge, notification watchers, or the Python
  engine. All 10 hosted Swift tests pass and a post-test TCC trace contains no
  Debug Documents/Full-Disk request. Shutdown also cancels the active
  Brainstorm, closes transports, terminates the resident Whisper process, and
  leaves no orphan engine. An obsolete signed backup bundle and stale 329 MB
  Xcode test build were moved recoverably to Trash; Launch Services resolves
  the production identifier only to `/Applications/JOAT.app`. Debug owns
  engine/STT ports 18787/51060 and stale cleanup is port-scoped, so tests cannot
  restart the installed port-8787 engine or disturb its port-50060 speech
  server. An
  authenticated non-loopback Tailscale
  probe then sent real G2-format PCM through pairing, VAD, Whisper, the wake
  gate, and the installed session router. It exposed and fixed natural
  `brainstorm, …` punctuation falling through to the ordinary assistant; the
  rerun observed Brainstorm plus Codex/Claude/OpenClaw headers, Agent Terminal
  `ready → running → ready`, and an explicit spoken cancellation ending at
  `cancelled/closing`.
- Added a deny-by-default local firewall at the paired OpenClaw socket boundary.
  It permits only the nine exact RPC shapes used by the isolated `hermes` Agent
  Terminal and recovery path. Server advertisements cannot authorize channel,
  configuration, agent-management, tool-invocation, delivery-routing, `main`,
  Telegram, or malformed requests; rejected calls are proven to create neither
  a WebSocket frame nor a pending request. The five focused OpenClaw modules
  pass 114 tests. Shared handler-level redaction now covers early OpenClaw
  `token`/`deviceToken` Frame logs before socket authentication, opaque
  acknowledged run IDs remain cancellable, and shutdown permanently cancels
  an active stream before invalidating its socket so recovery cannot reopen a
  late orphan connection. All six affected OpenClaw/transport source modules
  pass strict mypy; a local log audit found zero unredacted credential-shaped
  frames. The empty tool inventory is still a pre-send snapshot rather than an
  atomic server-side execution guarantee, so no OpenClaw tools or general JOAT
  capability RPCs were enabled in this Brainstorm lane.
- Closed the productivity HUD wiring gap without claiming unsupported ambient
  recording. The five-row root is now Messages, Notifications, Calendar,
  Tasks, and More; More opens QuickNotes, Brainstorm, and Activity in the same
  transient rail. Capability icons are label-driven instead of stale
  row-position mappings. Notifications, Calendar, Tasks, QuickNotes, and
  payload-free durable Activity summaries open deterministic read-only details,
  while setup/empty/detail/receipt pages no longer draw fake selection chrome.
  Confirmed Calendar/Reminder writes now surface `ACTION · VERIFYING` followed
  by an honest `DONE`, `CHECK`, or `FAILED` page without making HUD delivery
  write authority. Added an inert development-only 576×288 browser preview that
  stitches the exact four Gray4 tiles from the production presenter/renderer;
  visually inspected root/More, all five productivity collections, their detail
  states, Activity, a verified receipt, and the confirmation screen. Full Hub
  verification passes 219 tests plus the TypeScript/Vite build; strict mypy
  passes across 64 Python files; the full Python run passes all 465 tests; and
  the Swift package passes 24 XCTest plus 3 Swift Testing cases.
  Official Even simulator,
  installed-app TCC/EventKit writes, and physical-G2 legibility remain release
  gates. Meeting recording, AI summaries, recurrence, and snooze remain
  deliberately excluded pending their consent/capture/native-semantics proofs.
- Closed every finding across two independent hostile-review rounds.
  Reminder completion now binds the previewed EventKit list in the Python
  payload, Swift pre-save validation, and normalized read-back. Post-native
  receipt/canonicalization failures become durable uncertainty and publish
  `ACTION · CHECK` instead of escaping at `VERIFYING`. Capability lists carry a
  unique generation ID end to end, so reverse-completing same-capability opens
  cannot mix rendered rows and selection snapshots. Tappability is explicit
  protocol metadata rather than title parsing, and Sidebar, More, Settings,
  and loading navigation commit only after a successful paint; failed paints
  restore Overview without leaving hidden controls active. Mac response rows
  also remain inert until their complete four-tile frame is visible, and the
  rare receipt-committed-then-raised path purges its cleartext payload before
  returning verified success.
- Completed the implementation and simulator portions of navigation Gate 0
  with one owner for the Even host's unkeyed location stream. It preserves
  ambient grounding, switches only after the explicit Mac opt-in, revokes both
  diagnostic modes on disconnect, falls back to ambient, and stops the stream
  at shutdown. The versioned Python firewall now exact-checks the outer and
  inner `g2.diagnostic` shapes and persists only bounded cadence, capability-
  presence, accuracy, displacement, lifecycle, and heading-delta buckets; raw
  probe coordinates and motion values are rejected. The official simulator
  produced separate 576x288 pixel frames for Ready, visible confirmation, and
  restored Ready plus a v2 receipt with the system tap, both page transitions,
  and ordered tile acknowledgements. Its high-accuracy request failed with no
  samples, correctly retained as simulator-only capability evidence. Focused
  verification passes 119 Python gateway/protocol/capture-harness tests with
  strict mypy and all 219 Hub tests plus the production build. The two
  remaining full-Python
  failures are current-tree notification-mux timing/duplication failures outside
  Gate 0. The canonical existing-project package is now `joat.ehpk` v0.1.13
  (59,154 bytes, SHA-256
  `3927c6fa621c8e68d0180d6f777b2fac7de5917ba6e03961e0ea4027075266cb`).
  The portal now shows v0.1.13 as an uploaded private build under the existing
  `com.akinwale.joat` project. A physical-run harness now preserves walking and
  cycling in separate owner-only files, refuses evidence overwrite, requires a
  stationary lens marker, and cannot call `high/success` without samples a
  pass. Private installation and the actual physical-G2 receipts plus
  synchronized lens video remain open. No routing provider, destination UX,
  guidance, or navigation thresholds were added.

## 2026-08-27

- Implemented the approved research-first productivity foundation across all
  six requested areas. Consequential actions now move through durable draft,
  validation, exact bound HUD preview, explicit decision, pre-dispatch attempt,
  native execution, normalized read-back, and an honest receipt; restart turns
  in-flight calls uncertain instead of silently retrying them.
- Added the native EventKit bridge and explicit Mac permission controls.
  Calendar and Tasks HUD views query real authorized containers; strict voice
  commands create nonrecurring events/reminders and complete uniquely named
  nonrecurring reminders. DST gaps/repeated times, duplicate targets, stale
  EventKit generations, and read-back mismatches fail closed.
- Kept Reminders/EventKit as the ordinary task source of truth and primary
  alarm provider. JOAT persists only its own one-off occurrences for overdue
  restart catch-up, reconciles native completion before alerting, verifies the
  requested EventKit alarm in read-back, and retains prior alert evidence after
  completion. Unknown native state defers instead of producing a false alert;
  recurrence, snooze, and recurring completion remain gated on device proof.
- Added text-only QuickNotes: `take a note`, local `find notes`, reversible
  `undo last note`/`restore last note`, and owner-only Markdown export. Meeting
  recording and AI summaries remain disabled because upstream audio can drop
  an unacknowledged tail; no “lossless” capture claim was introduced. Explicit
  export now includes every active note rather than silently stopping at 100.
- Replaced startup's single notification source with independently polling
  Messages, Accessibility-banner, and available metadata lanes backed by
  durable cursors, raw observations, health, and an enqueue outbox. The 20-row
  Messages backlog defect now drains 20/20/5, source failure is isolated, and
  exact nearby Messages observations suppress fallback banners one-for-one, so
  repeated-event cardinality is preserved. Native identities survive reordered
  or overlapping batches, JSONL rewrites, and Biome timestamp ties; a native
  heartbeat exposes a stale Accessibility watcher; payload digests are checked
  on load; metadata-only events stay off the HUD; and production delivery no
  longer waits on serial model triage. Restart now withholds old fallback rows
  until authoritative catch-up completes, and malformed complete AX feed lines
  surface `parse_degraded` instead of a false healthy state.
- Fixed the Accessibility watcher’s quiet-period priming bug and removed
  destructive five-minute content deduplication. Identical separate banners
  remain separate appearance edges; status, banner, debug, brain-log, SQLite,
  and export artifacts are owner-only, and AX debug logging is content-free.
- Contract verification passes 422 Python tests, strict mypy across 61 source
  files, 198 Hub-plugin tests, the TypeScript/Vite production build, 18 XCTest
  plus 3 Swift Testing cases, and diff whitespace checks. A universal Developer
  ID archive/export passes strict signature verification and contains the
  EventKit entitlement plus Calendar/Reminders full-access usage strings. It is
  not notarized, and this rebuilt bundle was not installed; the existing
  `/Applications/JOAT.app` is an older build. TCC grant/deny, real EventKit
  writes, simulator, physical-G2, WSS/Tailscale Serve, retention, and crash
  reconciliation after a native commit remain separate release gates.

- Added a production-safe, default-off G2 diagnostic receipt. An authenticated
  Mac opt-in now captures only page transitions, gesture classes, ordered
  four-tile upload outcomes, and HUD self-heal decisions; the Mac shell exports
  a separately allowlisted, content-free archive. The official Even Hub
  simulator exercised the real gateway/plugin path and retained a 576×288
  confirmation frame plus ordered upload, tap, show, and dismiss receipts.
  The trace drops while disconnected and persists via a coalesced background
  writer, so it cannot evict real uplinks or block a control session. Focused
  plugin coverage is 88 passing tests; Python unit/integration, strict mypy,
  and Swift shell tests all pass. The unrelated shared menu-label
  edit currently leaves the broad Hub-plugin suite with one stale expectation.

- Replaced the Confirmation surface's stacked `RIGHT / RING / NOD` and `LEFT`
  instructions with one horizontal, source-agnostic CTA row: `TAP · Confirm`
  and `SWIPE · Cancel`. Either built-in G2 temple now confirms, either swipe
  direction cancels, and optional R1 compatibility remains invisible so the
  decision never implies that a ring is required.
- Wired the explicit simulator Confirmation fixture through the production
  decision gate instead of merely painting it. Simulator Click now accepts and
  dismisses; Up/Down refuses and dismisses; timeout remains fail-closed. IMU
  motion is diagnostic-only and can no longer silently approve an action that
  advertises TAP as its sole acceptance gesture.
- Verified the corrected fixture in the official Even Hub simulator: the live
  trace reached `confirmation-visible`, Click settled `accepted=true`, and a
  fresh run's Up gesture settled `accepted=false`; both restored the prior HUD.
  All 189 Hub-plugin tests and the production TypeScript/Vite build pass.
  Repacked v0.1.11 as a 55,731-byte `joat.ehpk` with SHA-256
  `78925309946a28bcbdbbcf3002b9aa7e5cf85f745092c6e85544b425cd6e6705`.
- Completed a research-first design gate for Calendar, Reminders/tasks, AI
  notes, safe write actions, persistence, and notification fan-in. The design
  records platform permission boundaries, failure states, privacy constraints,
  adversarial acceptance tests, and deliberately unproven claims in
  `docs/PRODUCTIVITY_SYSTEM_RESEARCH.md`.
- Corrected three implementation assumptions before code: query-plus-create
  needs full Calendar access; ordinary reminders should remain EventKit's
  system of record rather than a parallel scheduler; and the current audio
  relay permits only gap-aware capture, not an absolute lossless guarantee.
- An independent hostile review exposed an untrusted-pairing P0 and upstream
  data-loss seams. Reordered delivery so transport identity, exact full-preview
  UX, privacy redaction, durable typed actions, SQLite reconciliation, and a
  bidirectional native Swift bridge precede Calendar and Reminder writes. No
  production implementation was started pending owner approval and resolution
  of the active concurrent worktree changes.
- Repaired the Messages reply path end to end. Incoming cards now carry
  explicit notification provenance and render as `MESSAGE`/`NOTIFICATION`
  instead of the fabricated `REPLY`; selecting a one-to-one Messages row arms
  a voice draft, shows the exact recipient and full draft on the exclusive
  Confirmation screen, and reaches Messages.app only after acceptance.
- Kept group chats explicitly read-only so a group response cannot be
  misrouted to one participant, and reject drafts that cannot fit completely
  on the 120-character confirmation surface. The eight-second visible decision
  window is preserved inside a twelve-second transport envelope so HUD paint
  time cannot silently shorten wearer consent.
- Added privacy-safe action correlation across plugin selection, confirmation
  visibility/verdict, real WebSocket gateway, and final sender outcome. Removed
  raw notification, transcript, reply, and queued-card content from operational
  logs; regressions assert that neither message drafts nor recipient handles
  appear in captured logs.
- Added a safe ordinary-simulator trace harness that uses the production reply
  controller and real gateway but a recording final sender. Official simulator
  evidence reached `confirmation-visible`, accepted one Click, resolved
  `confirmation-result accepted=True`, and reached exactly one recording-send
  boundary. The fixture is now visibly named `SIMULATOR TEST (NO SEND)` and
  contains no real-looking contact or message, so its extra window cannot be
  confused with the ordinary live JOAT simulator; the built-in confirmation
  visual state uses the same unmistakable copy.
- Routed simple salutations through a deterministic pre-inference command:
  `Good morning, JOATs.` now yields `Good morning. How can I help?` without
  requesting personal grounding. Greeting-plus-request speech still reaches
  the general reply path, unaddressed greetings remain silent, and wake-word
  punctuation cleanup no longer leaves doubled commas.
- Cleared unowned Main/Context/Tray content when a fresh Mac brain connects, so
  a reply painted by an older process cannot masquerade as a new response.
  Every completed, ignored, or empty utterance now also returns the transient
  `HEARING YOU` chrome to truthful `LISTENING` state, while an active session
  retains its own status through speech and reconnects.
- Current verification: 309 Python tests, including 44 real-gateway integration
  tests; 184 Hub-plugin tests; strict mypy across 47 source files; clean
  TypeScript/Vite production build and diff whitespace. Repacked v0.1.11 as a
  55,849-byte `joat.ehpk` with SHA-256
  `3f1c56e948fc28048fe6a010610beed0cb5ad2fb84245e7f6035675cbcbb503f`.
- Repaired the ordinary, fixture-free Overview state that had fabricated
  `REPLY / Nothing scheduled`, an orphan notification bell, and an empty
  `LAST QUERY`. Connecting, reconnecting, offline, and connected-empty sessions
  now use one named full-focus state with truthful recovery guidance; partial
  Overview payloads render only the semantic regions actually supplied.
- Retained the latest `MacLink` lifecycle state and replay it when Chrome is
  created, closing the fast-local-gateway race that could leave a connected
  session visually stuck on `CONNECTING` and hide new content as stale.
- Added regressions for status-only boot, stale content during reconnect,
  partial Overview frames, the populated Overview fixture, and late HUD link
  observation. A fresh ordinary simulator session reached `JOAT · LISTENING`
  and the intentional `Ready` screen; the populated Overview output remained
  byte-for-byte identical to the prior accepted capture.
- Current verification: 177 Hub-plugin tests, TypeScript/Vite production
  build, 98 focused Python pairing/protocol/capability/real-gateway tests,
  strict mypy across 42 source files, and clean diff whitespace. The named
  real-gateway regression still completes pairing, retains unauthenticated
  `401`, and rejects both the full pairing token and its prefix from emitted
  logs. Repacked v0.1.11 as a 55,306-byte `joat.ehpk` with SHA-256
  `d2d4502e4eff28059de1448f4f64670742a82e99d47b108e307e5610c5fa79b4`.
- Replaced the full-width capability menu with a transient 160px left Sidebar
  that appears only on left-temple double-click, keeps a dimmed live Overview
  visible at right, and disappears into a full-focus destination on selection.
- Replaced the firmware-default text/list presentation with a custom 576×288
  Gray4 HUD renderer: four SDK-safe 288×144 image tiles form one rounded visual
  surface while an empty brightness-zero text layer preserves real gestures.
- Added a consistent Phosphor icon family, continuous status spine, measured
  typography, structured message rows, custom selection states, and a live
  confirmation countdown; image uploads are globally serialized to prevent a
  torn frame across quadrants.
- Added four explicit HUD surfaces—Overview, Sidebar, full-focus content, and a
  dedicated Confirmation screen—with buffered Overview restoration and
  truthful per-state gesture copy.
- Added regression coverage for exact 576×288 geometry, single-capture and
  z-order invariants, Sidebar-to-Messages wiring, confirmation restoration, and
  the unchanged left/right double-click split.
- Visual verification used the official Even Hub simulator against the real
  presenter/router path: all four states were compared beside the approved
  visual reference, a simulator scroll moved the custom Sidebar selection, and
  click replaced the Sidebar with the selected loading focus page. The retained
  evidence and final severity audit are in `design-qa.md`.
- Re-ran the visual build from the approved source image and corrected the
  measured status, Overview, Sidebar, list, and Confirmation anchors. The
  renderer now sentence-cases destination titles and uses the matching dotted
  message/calendar assets.
- Repaired the live image protocol boundary: antialiased 8-bit canvas pixels
  are now quantized to the SDK's unpacked Gray4 values 0...15 before upload.
  This preserves focal/support/chrome hierarchy instead of clipping most
  authored intensities to full green in the simulator and glasses.
- Closed the final cross-module failure modes before freeze: newest-tail reply
  visibility, complete long-prompt wrapping, later-row reachability, countdown
  and input-gate alignment, structured mic truth, preserved Mac urgency,
  coalesced pending frames, active rollback after a partial four-tile upload,
  and capability-response correlation across Back/newer selections.
- Removed redundant Mac capability status writes; focus titles now own that
  state, so a delayed capability response cannot stale Home for twelve seconds
  even when its list is correctly rejected.
- Prior visual-slice verification: 171 Hub-plugin tests; 98 focused Python
  capability/protocol/pairing/real-gateway tests; strict mypy across 42 source
  files; TypeScript/Vite production build; no production source map or DEV
  fixture marker; clean diff whitespace. The real-gateway regression preserves
  the pairing handshake and unauthenticated `401` while proving pairing tokens
  and token prefixes never reach captured logs.
- Established the earlier dashboard/focus/confirmation foundation and its
  single SDK event-capture invariant; the current Sidebar and 40/184/32px
  full-focus geometry above supersede that first full-width menu iteration.
- Added the exclusive confirmation state with explicit accept/cancel/timeout
  copy, automatic fail-closed timeout, input consumption, background-content
  deferral, and restoration of the interrupted dashboard or focus page.
- Moved Mac capability empty/setup/data states onto the shared focus-page
  protocol and documented the canonical runtime contract in `DESIGN.md`.
- Prior-slice verification: 118 Hub-plugin tests, 175 Python unit tests, TypeScript/Vite
  production build, strict mypy across 39 source files, clean Design.md and
  Frontend Design Premium audits, and real Python gateway → Hub plugin →
  official Even Hub simulator proof of confirmation timeout/restoration.

## 2026-08-26

- Repaired verbose WebSocket logging so pairing tokens are redacted from both
  authenticated request URLs and token-bearing pairing offer/ack frames.
- Added a real-gateway regression covering the TOFU offer and acknowledgment,
  authenticated reconnect, unchanged unauthenticated `401`, and captured-log
  token absence.
- Verification: `42 passed` in `tests/integration/test_hub_gateway.py`, `15
  passed` in `tests/unit/test_pairing.py`, and strict mypy completed with no
  issues across 39 source files.

### September 4: prompted input transport and visual acceptance correction

- Added exact question presentation, source/epoch-preserving audio, and
  generation-bound prompt traffic. The Hub stages cancellation at wire ingress
  while retaining ordinary downlink order; Back and mute cannot operate the
  feature hidden behind a question.
- Connected the production HUD publisher to the authenticated gateway and
  synchronous owner activation. A real WebSocket fixture now completes two
  spoken message fields without a wake phrase, preserving literal command-like
  body text and refusing the synthetic send. Retirement clears the exact lease
  before consumption and preserves any successor.
- Root checked 533 host tests and 149 Hub controller/gesture tests. Full-current
  build/simulator gates remain pending. Component checks do not prove physical
  microphone recognition or optical delivery, or assistant latency improvement.
- The wearer's screenshot review rejected cross-feature typography: native
  News text differs sharply from raster Messages text, with a clipped footer.
  Earlier selection receipts are now explicitly limited to interaction
  correctness. Root owns the visual integration repair; the typography agent
  is comparing native and raster options against the supplied screens and
  official Even constraints before editing.


### September 4: compact HUD correction after wearer review (verification pending)

- The source history identifies mixed rendering generations: September 2's native status label and September 3's native News/Search lists replaced parts of the authored compact JOAT UI. Earlier interaction receipts did not establish design fidelity.
- Restored the existing canvas font for the banner at 10px, with one separate mic indicator; removed the production native banner overlay. News/Search are being returned to the same custom surface through selection and Back.
- Expanded the shared reader bounds from x36..540 to x14..562. Exact QuickNote rows now use 62 conservative columns instead of 52, preserving UTF-16 limits, CRLF, Unicode and full source reconstruction.
- Root observed four failing banner/layout regressions before repair and 135 passing renderer/builder tests afterward; seven width regressions failed before repair and ten focused Note tests pass afterward. New actual-simulator images and their review remain required before visual acceptance.
- Brainstorm's separate scoped capture now passes all 18 new cases. The attempted full session suite was interrupted after 118 passes at 51.82s; the full gate will rerun it with a bounded job timeout. This is not a failing Brainstorm test or a completed full-suite receipt.

- Fresh adversarial review found that applying the 14px reader edge to News/Search articles would overlap the 14px tab icons at x12. Two reproduced regressions now reserve x36 for rail-bearing readers, while Notes/Messages retain x14 and all readers can use the right edge at x562.

- JOAT audit follow-up: the frozen Terminal capture/composition candidate passed 1,444 Python tests, 877 Hub tests, mypy (104 files), and the Hub build. First-use Terminal opening remains a separate repair: preparation tests reproduced false readiness, while Mac/Hub tests reproduced late opening frames after navigation. Work is underway on selected-lane initialization, truthful connecting/retry state, and visit/selection ownership through image painting. Simulator speech-flow acceptance remains failed/pending; details and source citations are in `docs/audits/2026-09-04-feature-audit/TERMINAL_CAPTURE_REPAIR.md`.
- Terminal first-use repair now passes 600 focused Python tests, all-source mypy (105 files), 206 focused Hub tests and build. Opening initializes the selected lane before capture; visit/selection guards reject late paints; retry/cycle retain their original selection; explicit voice entry remains available with a navigation-owned view. The broad gate and fresh simulator verification remain pending, and unbound frames/actions already in transit retain their documented limits.

### Messages UI typed speech and outcome repair (in progress)

New Message and selected-conversation Reply now enter the typed acknowledged speech owner; dictated bodies retain exact text. Added visit/action-bound Messages receipts, narrow unaddressed Reply admission using captured glasses provenance, cancellation before queued work, and observer isolation. Three bounded agent assignments cover controller, Hub display and speech entry, with root-owned execution. Initial behavioral failures and intermediate focused results are retained in `docs/audits/2026-09-04-feature-audit/MESSAGES_UI_REPAIR.md`. Final regression and simulator acceptance remain pending. No real message sends or package changes.

- Messages focused verification: 57 host cases and 359 Hub cases pass; Python typing checks cover 106 source files and the Hub TypeScript/build passes. Additional reproduced confirmation ownership and tagged double-Back failures are corrected. The frozen regression gate remains pending; there is no new simulator or physical-G2 acceptance for this slice.

- Messages frozen regression candidate: 1,706 Python tests, 947 Hub tests, mypy (106 files) and production build passed with no source changes. Simulator acceptance remains pending while the Mac is locked.

### Timer restart recovery (in progress)

- Reproduced running focus, paused focus and a completed focus block's break all resetting to idle on service reconstruction. Implementing one checkpoint in the existing local SQLite store, delayed recovery activation after composition, and shutdown that preserves deadlines. Root owns integration and execution; bounded agents own checkpoint storage and timer behavior/tests. No timer visual acceptance or production database migration is claimed yet.

- Timer source/store reconstruction now preserves all three reproduced phases (previously 0/3), including exact paused time and the running absolute deadline. Existing typography renders explicit recovery errors; spoken Reset now reaches recovery. Mypy (107 files) and Hub build pass. The initial combined suite hit its 15-second check bound after 76 passing cases; broader regression and simulator acceptance remain pending.

### 2026-09-05 — Timer recovery gate and Messages reading repair

- Timer host gate passed: 570 Python tests, 129 Hub tests, strict mypy and production build. Frozen source unchanged. Three synthetic restart scenarios now retain expected timer state (previously none). Full-engine and simulator/device acceptance remain pending.
- Beginning M4/M10: group reading with per-participant labels, no participant reply target, and Messages service retention across startup access failure. See `docs/audits/2026-09-04-feature-audit/MESSAGE_READING_ACCESS_REPAIR.md`.
- Implemented group history/detail paging and participant labels using the existing compact UI. Group reading cannot arm a private reply target or offer reply prompts. The original seven host checks now pass; three additional selection races preserve direct-thread/Home ownership with zero sends.
- Removed boot-only Messages availability filtering from router/tool/notification registration; model reads now return access/retry guidance without exposing exception paths. Added same-instance database appearance/access recovery checks. Combined regression gate pending; no new simulator or physical-G2 acceptance claim.
- Messages group/access combined gate passed: 726 Python tests, 333 Hub tests, strict types and production build; all 298 frozen files matched. Same notification lane recovered after database appearance and ingested a later delivery.
- Began M5 attributed-body recovery: pinned parser/independent-fixture experiment prepared, with vendor notices retained and no live Messages access or runtime dependency change. See `MESSAGE_BODY_REPAIR.md` in the feature audit.

### 2026-09-05: UI stabilization after wearer challenge

- Paused feature expansion. Rechecked official Even documentation against installed SDK 0.0.14 and current rendering/audio code; version drift and unresolved bridge/fallback issues are recorded in `EVEN_SDK_RECHECK.md`.
- Fresh current-source simulator inspection confirms compact News typography but rejects complete visual acceptance: emoji remain malformed, and an immediate conversation tap selects metadata and opens nothing. Earlier reader automation swiped first and missed that entry failure. Root and an independent reviewer inspected the screens; the new bounded repair targets first-message selection before tap. See `UI_STABILIZATION_REVIEW.md` for retained pixels and source hashes. Installed-app delivery remains unverified.
- Fixed the reproduced inert first conversation tap: first actual message selected in the initial custom frame, informational rows skipped, conflicting hidden cursor indices rejected. Fresh simulator tap/Back/reopen reached the same full message twice with selection 1 (previously metadata index 0 opened nothing). All 960 Hub tests and build pass; 185 copied source hashes unchanged. Emoji appearance and installed-app acceptance remain open.
- Reviewed the completed attributed-body parser experiment: nine public samples decoded (eight nonempty), while unchanged production readers exposed none of the synthetic rows. Two samples matched independent expected text; remaining decoded samples have structural/hash evidence. No decoder integration or runtime dependency installation occurred. Recorded in `MESSAGE_BODY_REPAIR.md`; UI stabilization remains the immediate priority.
