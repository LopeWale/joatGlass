# Changelog

## 2026-11-27 — Build JOAT 0.1.163 (`JOAT Brain` `202611271249`): persistent glasses notification read state across disconnects, Gmail IMAP `\Seen` read sync, and Mac Mini / iCloud iMessage read sync

- **Persistent glasses notification read state across disconnects & open-conversation arrival suppression** (`apps/hub-plugin/src/HudPresenter.ts`, `NotificationCard.ts`, `main.ts`, `HudPresenter.notificationCard.test.ts`, `apps/ios-companion/Sources/Cards.swift`, `CapabilityMenu.swift`, `Brain.swift`):
  - Marked incoming notification popups as read (`onNotificationRead` -> `notification-dismiss`) once their full `5,000 ms` display window completes uninterrupted, and auto-marked browsed `browseNotifications` cards as read when departing the card or suspending the HUD.
  - Hydrated and mirrored both `notificationsReadHere` and `PendingNotificationDismissals` to Even Hub host storage (`client.storage` via `getLocalStorage` / `setLocalStorage`, flushed on `teardown()`) alongside `localStorage` so disconnecting and reopening the JOAT plugin never resurrects already-read notifications.
  - Added `Cards.acknowledgeMatching`, `Cards.onAcknowledgeCards`, and `Cards.recordReadOrigin` so opening or replying to `MESSAGES · CONVERSATION` or `MAIL · CONVERSATION` clears matching unread notification cards, and messages/emails arriving while their conversation is already open on the lens are recorded as read in place instead of stacking unread notification popups.
  - Packed and promoted `0.1.163` to Even Hub `beta` (`apps/hub-plugin/joat.ehpk`, 184554 bytes, SHA-256 `889c2d9aa740326dc6ca2a7985812d2a4d4d40f79674b648bd77deaf84a5d98a`).
- **Upstream Gmail IMAP `\Seen` read sync & Mac Mini / iCloud `Messages.app` read sync** (`apps/ios-companion/Sources/GmailTransport.swift`, `Mail.swift`, `MailView.swift`, `MacMessages.swift`, `Messages.swift`, `apps/mac/joat/services/messages_endpoint.py`, `apps/mac/joat/__main__.py`, `apps/ios-companion/Shortcuts/Mark Message Read from JOAT.shortcut`, `apps/ios-companion/Shortcuts/source/Mark Message Read from JOAT.plist`, `apps/ios-companion/Shortcuts/README.md`, `tests/ios/test_gmail_mime_and_imap.swift`, `tests/ios/test_phone_brain_surfaces_and_prompts.swift`, `tests/ios/test_assistant_omnifunction.swift`, `tests/unit/test_messages_endpoint.py`):
  - Added `GmailIMAP.markSeenCommand(for:)` and `GmailIMAP.markSeen` (`SELECT "INBOX"` + `UID STORE <uids> +FLAGS.SILENT (\Seen)`) wired into `Mail.markThreadRead` and `Mail.markCardRead` so opening a Gmail thread on the glasses, reading it in `MailView` or via voice tools, or dismissing its notification card syncs `\Seen` upstream across iPhone, Mac, and web without reverting on periodic IMAP flag refreshes.
  - Added `POST /v1/read` and durable `0600` HMAC-keyed read watermarks (`~/Library/Application Support/JOAT/messages-read.json`) in `messages_endpoint.py`, paired with `joat.messages.readthrough.v1` and `MacMessages.markRead` / `markReadAsync` on iOS so read conversations stay read across restarts until a newer message arrives.
  - Added Apple-signed `Mark Message Read from JOAT.shortcut` (`com.apple.MobileSMS.MarkConversationAsUnreadIntent`, `OpenWhenRun = false`, `unreadState = false`) and `ShortcutsMessagesReadMarker` on the Mac Mini so reading or dismissing an iMessage/SMS on the glasses silently clears the unread indicator in `Messages.app` on macOS and syncs read status to iPhone via iCloud Messages.

## 2026-11-26 — Build JOAT 0.1.162 (`JOAT Brain` `202611261341`): JOAT modal auto-scroll & LVGL wrap safety on long exchanges, resilient Messages/Mail reply dictation settling, and `2x tap: retry` handoff fix

- **JOAT Conversation Modal auto-scroll, LVGL 1-line wrap guarantee & stage-growth resilience on Even G2** (`apps/hub-plugin/src/AssistantConversationSurface.ts`, `AssistantConversationSurface.test.ts`, `apps/ios-companion/Sources/Brain.swift`):
  - Measured full rendered row prefixes (`You: ` and `JOAT:` hanging-indent pad) against both `getTextWidth` and `firmwareLineCount(..., 458) <= 1` in `turnLines` so kerning at the 458 px boundary never wraps a logical row onto 2 physical LVGL lines or clips row 7 off the bottom of the 189 px container, and clamped `TextContainerUpgrade` payloads with `fitUtf8(..., TEXT_CONTAINER_MAX_BYTES)`.
  - Auto-reset viewport scroll (`desiredScroll = 0`, `committedScroll = 0`) whenever JOAT is actively thinking, streaming, or advancing turn text so long exchanges always scroll older lines upward to show the working glyph and newest words at the bottom.
  - Added immediate in-place fallback upgrades plus bounded `250 ms` stage-growth rebuild retries when `replacePage` is transiently held by an in-flight BLE header/underlay image transfer, emitted `state: "thinking"` (`450 ms` working glyph cadence) before the first streamed token in `Brain.swift`, and bounded conversation snapshot UTF-16 length (`<= 12,000` units with `earlierTurnsOmitted`).
- **Messages & Mail reply listening -> review reliability & `2x tap: retry` handoff** (`apps/hub-plugin/src/HudPresenter.ts`, `main.ts`, `MessageUI.test.ts`, `apps/ios-companion/Sources/Prompts.swift`, `Confirm.swift`, `Messages.swift`, `Mail.swift`, `Transcriber.swift`, `tests/ios/test_phone_brain_surfaces_and_prompts.swift`):
  - Fixed the `2x tap: retry` confirmation-to-prompt race: `ConfirmGate.cancelPending(forMessageActionId:reason:)` immediately cancels the retried draft's pending confirmation before re-presenting the reply prompt, `main.ts` dismisses cancelled confirmations on `confirm.cancel`, and `HudPresenter.showPromptQuestion` awaits any in-flight confirmation dismissal / page replacement instead of rejecting the retry prompt and cancelling the draft.
  - Tuned `Prompts.multiClauseSettleSeconds` to `2.4 s` (with a trailing-connector pause extension so natural mid-sentence pauses between `DictationTranscriber` bursts are not cut off early), armed automatic settle on volatile-only dictation when ambient noise prevents `.final` VAD closure, and latched early `prompt-commit` taps (`Tap: review`) while final STT audio is still in flight.
  - Reset `Transcriber` sample-clock state (`samplesFed`, `arrivals`) across `SpeechAnalyzer` restarts and bounded computed `latencyMs` (`0...5,000 ms`) so Bluetooth HFP/A2DP route transitions never produce stale pre-prompt timestamps.

## 2026-11-25 — Build JOAT 0.1.159 (`JOAT Brain` `202611251058`): Even R1 ring BLE isolation, R1-safe Hold-to-Talk & Phone Mic modes, foreground mic recovery, 1st-try standalone/split wake, and BLE coexistence diagnostics

- **Even R1 ring BLE `sn` isolation, foreground mic recovery & R1-safe microphone modes** (`apps/hub-plugin/src/DeviceMonitor.ts`, `AudioStreamer.ts`, `Settings.ts`, `EventRouter.ts`, `PhoneControlPanel.ts`, `main.ts`, `types.ts`, `index.html`, `apps/mac/joat/core/protocol.py`, `apps/mac/joat/__main__.py`, `.gitignore`):
  - Filtered SDK `onDeviceStatusChanged` events by G2 serial number (`DeviceMonitor.setGlassesSn`) so non-glasses accessory status frames (such as Even R1 ring connect/disconnect events whose `DeviceStatus.fromJson` defaults `isWearing: false` and `batteryLevel: 0`) are relayed as `deviceRole: 'other'` telemetry without ever stopping the G2 microphone, triggering `Resume listening?`, or flashing `0%` battery warnings.
  - Preserved `AudioStreamer.wantsCapture` across background host refusals (`suspendForHostRefusal`, `setBackgroundPaused`, `resumeIfWanted('foreground')`) so app-switching or screen-locking never strands the glasses in `JOAT · mic off`, and added long-press overview mic recovery when muted.
  - Added persisted `MicMode` (`glasses` | `on_demand` `Hold to Talk (R1-Safe)` | `phone` `Phone Mic (R1-Safe)`) synced live between `JOAT Brain` and Even Hub (`policy` / `policy.set`) so continuous G2 BLE audio can be dropped to `0 pkt/s` near AirPods or car Bluetooth while keeping full voice assistant access.
  - Packed and committed `0.1.159` (`apps/hub-plugin/joat.ehpk`, 181468 bytes, SHA-256 `2713259d8047a54d19990bf671ca60c082323432e35eeeb537713779631876b7`).
- **Standalone/split `"JOAT"` wake, `Transcriber` health watchdog & BLE coexistence diagnostics in `JOAT Brain`** (`apps/ios-companion/Sources/WakeGate.swift`, `Brain.swift`, `Features.swift`, `KeepAlive.swift`, `LoopbackServer.swift`, `NameModel.swift`, `Transcriber.swift`, `QuickNotes.swift`, `Navigation.swift`, `Log.swift`, `AppScreens.swift`, `JOATBrainApp.swift`, `tests/ios/test_wake_discipline.swift`):
  - Restored standalone 1-word phonetic wake calls (`"JOAT"`, `"Joe."`, `"Jo?"`, `"Joats."`, `"Jode."`, `"Joke?"` -> `"Yes?"` + 8 s follow-up window, also rejoining progressive dictation splits like `"Joe."` + `"what time is it?"`), added hesitation lead-in support (`"Um, …"`, `"Okay, …"`), and expanded `joeRequestLeads` while keeping casual bystander speech ignored.
  - Allowed explicit voice control commands (`"Hey JOAT, pause/stop voice note"`) and backgrounded note wake queries to reach `Brain` instead of being transcribed into an open recording, forwarded non-navigation `"Hey JOAT, …"` queries to the assistant during active navigation, added a `3.5 s` timeout on `NameModel.prepare()`, and added `Transcriber.check()` + stalled-analyzer recovery.
  - Added `BleDiagnostics` (`HomeView` + `DiagnosticsView`) tracking G2 vs. R1/accessory BLE status transitions, R1 ring gestures, G2 mic re-arms, and Classic BT `AVAudioSession` route changes (`BluetoothA2DP` / `CarAudio`), defaulted iPhone `MicResume` to automatic recovery, and changed **Share the log** to export plain `.txt` (`Log.exportShareURL()`) alongside 1-tap clipboard copy.

## 2026-11-24 — Build JOAT 0.1.158 (`JOAT Brain` `202611242114`): post-close-only AI note summaries, lag-free page editing, `AI cues` off parity across glasses & app, and repository privacy cleanup

- **Post-close-only AI summaries & lag-free Notebook editing in `JOAT Brain`** (`apps/ios-companion/Sources/NotesInsights.swift`, `Notebook.swift`, `QuickNotes.swift`, `AppScreens.swift`, `MacLink.swift`, `tests/ios/test_notes_insights.swift`, `tests/ios/test_phone_brain_surfaces_and_prompts.swift`):
  - Removed the `4.0 s` mid-edit `ocrIdleTimer` (`NotesInsights.summarizesWhileEditing == false`) and tracked active open pages (`QuickNotes.editingPageIds`) so neither typing/writing pauses nor `retryPendingInsights()` ever run handwriting OCR or AI summarization while a page is open.
  - Moved mid-edit `PKDrawing` persistence onto a background `.utility` queue (`PageStore.saveAsync`) with quiet store saves (`savePage(..., quiet: true)`), hid `PageInsightCard` on unsummarized pages during editing so the canvas never shifts, and moved PNG/JPEG tile rasterization in `Notebook.read` off `@MainActor` so closing a page (`Done`, back, split-view switch, or app backgrounding) stays instantaneous while post-close OCR + AI summary run in the background.
  - Added post-save and post-edit AI summarization (`QuickNotes.summarizeTextNote`, `updateTextNote`) and tap-to-edit (`TextNoteEditorSheet`) for Quick Text Notes alongside handwritten/typed/pasted Notebook pages and Voice QuickNotes.
- **`AI cues during QuickNotes` (`joat.cues.enabled`) parity across Even G2 & `JOAT Brain`** (`apps/ios-companion/Sources/Cues.swift`, `QuickNotes.swift`, `AppScreens.swift`, `apps/hub-plugin/src/HudPresenter.ts`, `CapabilityMenu.ts`, `main.ts`, `types.ts`, `HudPresenter.nativeReceipts.test.ts`, `CapabilityMenu.test.ts`):
  - Gated `Cues.heard` and live `UserDefaults` changes so turning `AI cues during QuickNotes` off immediately cancels pause timers, dismisses active cue cards, exits cue views, and repaints `QUICKNOTES · NEW NOTE` with `cuesEnabled: false`.
  - When `cuesEnabled === false`, `HudPresenter` omits `AI cues` from the glasses' contextual menu (rebuilding in place if toggled mid-note), `CapabilityMenu.sendQuickNotesMenu('cues')` rejects cue opens, `openVoiceReader` omits `AI CUES`, and `AppScreens` hides cue pills and sections on iOS.
  - Packed `0.1.158` (`apps/hub-plugin/joat.ehpk`, 179746 bytes, SHA-256 `49718b33b3cea047c6ccd76767b4fabb3b909b52c1ccc55d77905ad5eda89e9a`).
- **Repository fixture & identifier cleanup**: Replaced personal, client, location, network, and credential/team-ID fixtures across comments, prompts, tests, plists, documentation, and screenshots with synthetic test data.

## 2026-11-23 — Build JOAT 0.1.157 (`JOAT Brain` `202611231228`): multi-turn conversation reliability, Turn-2 viewport reset, `JOAT · LISTENING` header, 1st-try phonetic wake, and sub-1.5 s Gemini latency

- **Multi-turn conversation reliability, Turn-2 scroll reset & `JOAT · LISTENING` spine on Even G2** (`apps/hub-plugin/src/AssistantConversationSurface.ts`, `HudPresenter.ts`, `types.ts`, `AssistantConversationSurface.test.ts`):
  - Fixed Turn-2 viewport scroll lock (`AssistantConversationSurface.present`): starting a new turn resets `desiredScroll = 0` and `committedScroll = 0` so follow-up responses never stream off-screen below a scrolled Turn 1.
  - Replaced the phantom `"You: Listening… (no name needed)"` user turn with structured `listening?: boolean` on `AssistantConversationSnapshot`, updating the top spine (`JOAT · LISTENING` vs `JOAT`) with zero duplicate `ready` frames and guarding `ContainerId.StatusBar` text upgrades behind `imageHeader()` when `withImageHeader` owns the spine.
  - Added `abortFailedEnter()` so a transient bridge rebuild failure on entry clears surface state without poisoning `AssistantConversationSurface.retired`.
  - Packed `0.1.157` (`apps/hub-plugin/joat.ehpk`, 179606 bytes, SHA-256 `0fbe66d62588e1b1e9b541398acb371bb4c1c6d5559c6c12b50c64bf8efcb574`).
- **Retired-`conversationId` rotation, speech-start latching, 1st-try phonetic wake & sub-1.5 s latency in `JOAT Brain`** (`apps/ios-companion/Sources/Brain.swift`, `WakeGate.swift`, `OnPhoneAI.swift`, `AssistantTools.swift`, `QuickNotes.swift`, `Reminders.swift`, `Voice.swift`, `LoopbackServer.swift`, `tests/ios/test_wake_discipline.swift`, `scripts/simulator_wake_discipline_e2e.py`):
  - Separated glasses card `turns` from 30 s `modelHistory` and rotated `conversationId` (`rotateCardConversation()`) on touchpad exit, tool/command lens handoff, and WebSocket reconnect so `JOAT Brain` never reuses an ID retired by Even Hub.
  - Replaced premature `.volatile` `assistant.conversation` BLE previews with speech-start window latching (`speechLatchedUntil`, `nameOnlyLatchedUntil`), filler-word (`"um"`, `"uh"`) immunity, and post-TTS `Voice.onFinishSpeaking` follow-up extension.
  - Restored 1st-try Apple Dictation phonetic wake (`"Joe, <request>"`, `"Jot, …"`, `"Jolt, …"`) while blocking casual speech (`"Joe said…"`, `"Joe, pass…"`), cleaned the connect status word (`"JOAT · online"`) so `Chrome` never latches over page titles for 12 s, kept `glasses_command` (`dismissesConversationBeforeRun: false`) from prematurely dismissing text replies, added `<25 ms` local fast handlers for time/date/calendar/reminders/notes, and configured `thinkingConfig: { thinkingLevel: "MINIMAL" }` + fast retry on `GeminiLink` (`gemini-3.5-flash-lite`).

## 2026-11-23 — Build JOAT 0.1.156: heading-up route schematic, assistant wake discipline, and inline notes insights

- **Route-schematic navigation HUD on Even G2** (`apps/hub-plugin/src/NavigationSchematic.ts`, `HudCanvasRenderer.ts`, `NavigationHud.ts`, `NavigationController.ts`, `apps/ios-companion/Sources/Navigation.swift`, `scripts/simulator_nav_schematic_flow.py`):
  - Replaced the 4-bit raster street map with a heading-up route schematic drawn from route geometry and the live fix in one projection: wearer chevron fixed at lower-centre, bright remaining route, dim travelled route, ringed next-maneuver node with its street label, cross-street stubs, and dashed off-route connector; per-mode zoom clamps. Stray `navigation.map` tiles are ignored and the phone no longer prefetches them.
  - Packed `0.1.156` (`apps/hub-plugin/joat.ehpk`, 179416 bytes, SHA-256 `b1cc7fa36fb32900ed437dc3e60a58c582bf40370e04b8ae7f54995d1802726e`).
- **Assistant wake discipline & capability reach in `JOAT Brain`** (`apps/ios-companion/Sources/Brain.swift`, `WakeGate.swift`, `AssistantTools.swift`, `Messages.swift`, `Mail.swift`, `OnPhoneAI.swift`, `tests/ios/test_wake_discipline.swift`, `scripts/simulator_wake_discipline_e2e.py`):
  - Volatile previews appear only when addressed (wake word in the first two tokens, name-only follow-up, or open window) and are dismissed with the on-lens revision when the final transcript is unaddressed; bare "joe"/"jo" no longer wake JOAT.
  - The follow-up window (the wearer's `joat.followUpSeconds` setting) opens only after JOAT answers an addressed request, shows a "Listening… (no name needed)" cue, and is never armed by receipts, arrivals, or navigation cues; unaddressed "reply …" requires the thread on the lens.
  - Corrected the assistant's phone-first system prompt and added tool #30 `glasses_command`, routing any deterministic voice command through `Brain.routeCommand`.
- **Inline notes insights, faster handwriting reading & zoom in `JOAT Brain`** (`apps/ios-companion/Sources/NotesInsights.swift`, `Notebook.swift`, `QuickNotes.swift`, `AppScreens.swift`, `OnPhoneAI.swift`, `tests/ios/test_notes_insights.swift`):
  - AI summary, key points, and next steps shown inline on every page (collapsible card) and under each Notes row, with reading/summarizing states; summaries kept (marked stale) until the refreshed one lands.
  - Handwriting is read after a 4 s writing pause, on leave, and on backgrounding; pinch zoom from half of width-fit (page centred) to 4×+; handwriting OCR prefers Claude whenever a Claude key is configured, Gemini as fallback.

## 2026-11-22 — Build JOAT 0.1.155: phone-first WebSocket architecture, Mac `/messages` & `/terminal` relays, multi-clause dictation, and persistent notification sync

- **Phone-first WebSocket routing & persistent notification sync on Even G2** (`apps/hub-plugin/src/CapabilityMenu.ts`, `PhoneControlPanel.ts`, `BrainHandoff.ts`, `HudPresenter.ts`, `EventRouter.ts`, `NotificationCard.ts`, `main.ts`, `types.ts`):
  - Stopped `openFromContextualMenu` from hijacking the plugin WebSocket onto the Mac brain for `Messages`, `Agent Terminal`, or `Activity` when paired to `This iPhone · JOAT Brain` (`ws://127.0.0.1:8787/glasses`), keeping 100% of speech recognition (`SpeechAnalyzer`) and JOAT Assistant (`GeminiLink` `gemini-3.5-flash-lite`) local on the iPhone with zero loopback latency.
  - Wired `DOUBLE_CLICK_EVENT` (`2x tap`) on notification popup cards (`HudPresenter.dismissNotificationCard`) to mark unread cards read and emit `notification-dismiss`, added persistent 200-ID FIFO dismissal tombstones (`joat.notifications.read.v1`) and offline dismissal replay (`joat.notifications.pending-dismiss.v1`), synced unread counts immediately on `hud.notifications` (`keep = 25`), and refreshed relative ages (`15m ago`, `2h ago`) on minute status ticks.
  - Packed `0.1.155` (`apps/hub-plugin/joat.ehpk`, 178263 bytes, SHA-256 `daa32ac202f86393edbbe8ee9388af4a3a8369f8b71ba8c5f6cdc1bf0750a41e`).
- **On-phone `GlassesTerminal`, multi-clause `Prompts` dictation, and true-timestamp persistent `Cards` in `JOAT Brain`** (`apps/ios-companion/Sources/GlassesTerminal.swift`, `CapabilityMenu.swift`, `MacTerminal.swift`, `Prompts.swift`, `Brain.swift`, `Messages.swift`, `Mail.swift`, `Cards.swift`, `AssistantTools.swift`, `JOATBrainApp.swift`):
  - Cleared `CapabilityMenu.macPages` (`[]`) and added `GlassesTerminal` driving the full 3-level G2 `Agent Terminal` (`AGENT TERMINAL` lane picker -> `AGENT TERMINAL · <LANE>` session picker -> 5-container `agent_terminal.view` with `[!] Approval needed`, live 1.2 s / 3.0 s diff polling, and iPhone-transcribed spoken instructions) over `MacTerminal` (`/terminal`).
  - Added `Prompts` pre-prompt speech fencing (`askedAt`/`listeningSince`), `1.5 s` multi-clause settle accumulation across volatile `hearing(_:)` partials on `body`/`instruction`, `0 ms` tap-to-commit (`prompt-commit`), and clean Mac `/messages` handle formatting (`confirmHandleSuffix`).
  - Upgraded `Cards` to `Codable` (`joat.cards.store.v2`, `keep = 25`, 200-ID `dismissedOriginIds`), preserved true `Messages` (`t.at`) and `Mail` (`item.at`) arrival timestamps instead of stamping `"Now"`, and published `hud.notifications` + `hud.ambient` ahead of `hud.text` popups.
- **Mac `/terminal` session relay & direct-Mac WhisperKit/VAD hardening** (`apps/mac/joat/services/terminal_endpoint.py`, `services/orchestrator.py`, `audio/vad.py`, `audio/whisper_serve.py`, `scripts/simulator_phone_first_e2e.py`):
  - Enriched `TerminalEndpoint` `GET /v1/state` with resumable `sessions` per lane and added `POST /v1/config` `action: "resume_session"`.
  - Added `prompt_hangover_batches=11` (`2.2 s`), `fence_quiet_batches=2` (`400 ms`), bounded adaptive RMS ceilings (`0.028`/`0.050`), `vad.closing` partial suppression, strict prompt provenance gating, and explicit `--language en` on `whisperkit-cli serve`.

## 2026-11-21 — Build JOAT 0.1.154: Mac-pinned `Messages` & `Agent Terminal` split-brain handoff, `/messages` search, and `/terminal` control

- **Seamless split-brain Mac handoff on Even G2** (`apps/hub-plugin/src/types.ts`, `BrainHandoff.ts`, `CapabilityMenu.ts`, `PhoneControlPanel.ts`, `main.ts`, `index.html`):
  - Added `'Messages'` to `HANDOFF_CAPABILITIES` alongside `'Agent Terminal'` and `'Activity'`, intercepted Mac-pinned menu opens directly on the phone brain with zero loopback round-trip, and added `prepareMacHandoffPage` + `switchBrain(..., { skipDefaultPaint: true })` so dialing the Mac preserves one upfront list frame without extra BLE rebuilds before returning automatically to the phone brain on Home or `hud.rebuild`.
  - Fixed startup `hello` announcement (`handoff: true, heartbeat: true` sent immediately on connect), fenced departing-socket downlinks (`handoffDepartingGeneration`) so trailing overlays behind `brain.handoff` skip BLE paint, and added the Mac-pinned connection status row in Even Hub's phone sheet.
  - Packed `0.1.154` (`apps/hub-plugin/joat.ehpk`, 177268 bytes, SHA-256 `3bc1ac5787a42f4994b2fdd702f1904a391518450d0e2f3be23deb580b47bf0c`).
- **Mac `/messages` query search & `/terminal` execution controls** (`apps/mac/joat/services/messages_endpoint.py`, `terminal_endpoint.py`, `channels/imessage.py`):
  - Raised `MessagesEndpoint` thread and message page ceilings to `50` and added case-insensitive `?q=<search>&limit=<n>` thread search registering opaque HMAC thread IDs without exposing private handles or chat GUIDs.
  - Enriched `TerminalEndpoint` `GET /v1/state` with secret-free `recentMessages` and `pendingApprovalItems` per agent, and added `POST /v1/config` actions `send_instruction` (non-blocking async dispatch with optional `newSession`), `cancel_run`, and `decide_approval`.
- **Always-on-Mac `Messages` & `Agent Terminal` in `JOAT Brain`** (`apps/ios-companion/Sources/CapabilityMenu.swift`, `MacMessages.swift`, `Messages.swift`, `MacTerminal.swift`, `AssistantTools.swift`, `OnPhoneAI.swift`, `Brain.swift`, `Commands.swift`, `AppScreens.swift`):
  - Defaulted `pluginHandsOff = true` from initial connect, routed `Messages`, `Agent Terminal`, and `Activity` opens to `brain.handoff` while suppressing redundant trailing `hud.receipt` overlays, made Mac `chat.db` authoritative for `Messages` tools with `?q=` sender lookup, and added Tool #29 `agent_terminal_action` (`send_instruction`, `approve`, `deny`, `cancel_run`, `new_session`, `set_model`).

## 2026-11-21 — Omnicapable JOAT Assistant (`JOAT Brain` 1.0 · `202611211537`): 28-tool `PhoneToolBelt`, multi-round SSE tool calling, Calendar access, and polite lead-in normalization

- **28-tool `PhoneToolBelt` & `ConfirmGate`-enforced personal actions** (`apps/ios-companion/Sources/AssistantTools.swift`, `Reminders.swift`, `Messages.swift`, `Mail.swift`, `Recipients.swift`, `QuickNotes.swift`, `CapabilityMenu.swift`, `AppScreens.swift`, `project.yml`, `Info.plist`):
  - Added `PhoneToolBelt` exposing 28 on-phone tools across Calendar (`calendar_agenda`), Reminders (`open_reminders`, `create_reminder`, `complete_reminder`), Gmail (`get_recent_emails`, `read_email_thread`, `prepare_email`), Messages (`get_recent_messages`, `read_message_thread`, `prepare_message`), Contacts & relationships (`contacts_lookup`, `remember_relationship`), Navigation & location (`start_navigation`, `stop_navigation`, `find_nearby_places`, `where_am_i`, `list_saved_places`), Notes (`read_notes` across QuickNotes, Voice Notes, Apple Pencil Notebook pages, and Prep folders; `create_quicknote`; `control_voice_note`), Timers (`control_timer`), live info (`get_weather`, `news_headlines`, `read_news_article`, `web_search`), surfaces (`open_surface` including `claude`/`codex`/`antigravity` -> `Agent Terminal` and `home`), `terminal_status`, and `notifications_and_voice`.
  - Enforced `ConfirmGate` hardware touchpad confirmation on every outbound email, text message, and EventKit reminder write/completion, added recursive Gemini `v1beta` schema sanitization (uppercase OpenAPI types, zero unsupported JSON Schema keywords), and added `NSCalendarsFullAccessUsageDescription` + `Calendar` permission management in `JOAT Brain › Home`.
- **Multi-round SSE tool calling & universal polite lead-in normalization** (`apps/ios-companion/Sources/Brain.swift`, `OnPhoneAI.swift`, `ClaudeLink.swift`, `tests/ios/test_assistant_omnifunction.swift`):
  - Added `Brain.stripPoliteLeadIn(_:)` stripping conversational prefixes (`"can you"`, `"could you please"`, `"go ahead and"`, `"I'd like you to"`, etc.) and trailing `"please"` ahead of deterministic `VoiceCommand` routing, plus `dismissActiveStreamingConversation()` and `AssistantTurnResult.claimedLens` so lens-claiming tools cleanly dismiss `assistant.conversation` before presenting `confirm.request`, `navigation.view`, `hud.list`, `hud.panel`, `brain.handoff`, or `hud.rebuild`.
  - Added `GeminiLink.streamWithTools` (`gemini-3.5-flash-lite`, preserving raw candidate `parts` and `thoughtSignature` across `functionCall` -> `functionResponse` rounds with zero `googleSearch` or `thinkingBudget: 0` payloads) and `ClaudeLink.streamWithTools` (`claude-haiku-5-5` `tool_use` + `input_json_delta` streaming).

## 2026-11-21 — Build JOAT 0.1.153: complete turn-by-turn navigation, 4-bit street map tiles, selectable `NAVIGATION` menu, `Drive` mode, and `JOAT Brain` Navigation & Places hub

- **Selectable `NAVIGATION` menu, live `"Where to?"` speech-in-text, `Drive` mode & single-source `navigation.fix`** (`apps/hub-plugin/src/NavigationHud.ts`, `HudCanvasRenderer.ts`, `NavigationController.ts`, `NavigationProgress.ts`, `CapabilityMenu.ts`, `HudPresenter.ts`, `types.ts`, `main.ts`, `index.html`, `apps/mac/joat/core/protocol.py`, `core/navigation.py`, `navigation/policies.py`, `navigation/mapbox.py`, `services/navigation.py`, `services/capabilities.py`):
  - Removed `'NAVIGATION'` from `TEXT_PAGE_TITLES` so the glasses render a native selectable firmware list (`➤ Show active guidance`, `⏹ Stop navigation`, `➤ Navigate to a place…`, and 1-tap Saved Places), extended `HudPresenter` + `CapabilityMenu` (`noteCapabilityPromptPresented` / `noteCapabilityPromptCancelled`) so `ownerKind === 'capability'` renders live `prompt.partial` speech-in-text with `'tap: search · 2x tap: cancel'` without firing the 12 s selection timeout, and added `{ kind: 'navigation.resume', sessionId }` (`resumeToGlass`) so active guidance reclaims the lens cleanly.
  - Extended `NavigationMode` to `'walking' | 'cycling' | 'driving'` across Hub, Mac, and iOS (`DRIVE · ROUTE PREVIEW`, `DRIVING · ACTIVE`, zoom `15` / `360 m` tile stride, driving corridor/arrival policy, and mode-aware voice triggers `"walk to …"`, `"bike/cycle to …"`, `"drive to …"`, `"drive home"`).
  - Added `phoneFixes?: boolean` on `navigation.route` (0 ms `installRoute` skipping `LocationSessionManager`) + `{ kind: 'navigation.fix' }` (`acceptExternalFix`), detached `navigation.origin.request` and `location.refresh` from blocking `MacLink` downlinks, enriched privacy-clean `navigation.state` uplinks with optional `maneuver`, `maneuverDistanceM`, `instruction`, `stepId`, `remainingDistanceM`, and `etaMinutes` (zero coordinates), and simplified Even Hub's WebView (`index.html`) to a single-screen **Connection** sheet with non-destructive legacy place migration (`saved.length > 0`).
  - Packed `0.1.153` (`apps/hub-plugin/joat.ehpk`, 177639 bytes, SHA-256 `a17da726f673bbbda03f1ec6d30bfab40a9eb283f4faf873bfe7f5a75e23aa56`).
- **Key-free 4-bit G2 street map tiles, `PhoneLocation` lock-screen GPS & spoken headphone cues** (`apps/ios-companion/Sources/PhoneLocation.swift`, `NavigationMapTiles.swift`, `NavigationMaps.swift`, `Navigation.swift`, `Voice.swift`, `Brain.swift`, `CapabilityMenu.swift`, `project.yml`, `Info.plist`):
  - Added `PhoneLocation` (`WhenInUse` authorization + active-route-only `CLBackgroundActivitySession` and blue navigation indicator torn down immediately on stop/arrival) and `Voice.sayNavigation` for concise turn, wrong-way/off-route, and arrival prompts in connected headphones (`joat.navigation.voiceCues`).
  - Added `NavigationMapTiles` rendering `284×220` 4-bit monochrome Web Mercator street tiles aligned to `HudCanvasRenderer.projectMercatorPanel` (`512 * 2^zoom`), strictly gating `MKMapSnapshotter` to `UIApplication.shared.applicationState == .active` (cancelling immediately on `willResignActiveNotification` / `didEnterBackgroundNotification`) and stitching dark Web Mercator slippy tiles strictly on CPU when backgrounded.
- **Duo-adaptive `Navigation & Places` hub in `JOAT Brain`** (`apps/ios-companion/Sources/NavigationPlacesView.swift`, `AppScreens.swift`, `JOATBrainApp.swift`, `DebugSeed.swift`, `docs/IOS_APP.md`, `scripts/simulator_navigation_flow.py`):
  - Added `HomeNavigationCard` on `HomeView`, `Location` permission management, and `NavigationPlacesView` with side-by-side split controls + interactive `MapKit.Map` route polylines/pins on unfolded iPhone Duo, `MKLocalSearchCompleter` autocomplete, `Walk | Cycle | Drive` quick launch (`Start on Glasses`), and Saved Places CRUD with one-shot `joat.navigation.migratedPluginPlaceIds.v1` import.

## 2026-11-21 — Build JOAT 0.1.152: standalone multi-account Gmail, streaming JOAT assistant, and iPhone Duo + Apple Pencil redesign

- **Glasses `Mail` surface, 7-row review header generalization & remote `confirm.cancel`** (`apps/hub-plugin/src/types.ts`, `MessagesViewState.ts`, `CapabilityMenu.ts`, `HudPresenter.ts`, `ImuMonitor.ts`, `ConfirmationCommit.ts`, `main.ts`, `apps/mac/joat/core/protocol.py`, `services/capabilities.py`):
  - Added `Mail` at index 1 of the Even G2 contextual menu (`Messages`, `Mail`, `Reminders`, `Navigation`, `Search`, `Agent Terminal`, `Activity`, `Settings`) and generalized the shared comms list, `ScrollBox` conversation reader (`MAIL · CONVERSATION`), `capmessage` tap-to-reply, `mail-list` / `mail-conversation` / `mail-home` lifecycle events, and `message.ui` draft state machine across `MESSAGES` and `MAIL`.
  - Generalized `parseMessageDraft` to render `Reply (<acct>) to <recipient>?` and `Email (<acct>) to <recipient>?` on the 7-row review card while preserving `What do you want to say to <recipient>?` for `Send to <recipient>?` SMS drafts and `tap: confirm · 2x tap: retry` hardware gestures.
  - Added `{ kind: 'confirm.cancel', actionId }` (`ConfirmCancel` in `protocol.py` + `ImuMonitor.disarm`) so spoken cancellations immediately tear down active confirmation cards without emitting an uplink verdict, plus `MAIL · SETUP` fallback on direct Mac connections.
  - Packed `0.1.152` (`apps/hub-plugin/joat.ehpk`, 179104 bytes, SHA-256 `011b9dee07076c73ce71b0c94ee812128f35bf3ac863020281f114c81e050db4`).
- **Standalone multi-account Gmail engine on iPhone** (`apps/ios-companion/Sources/GmailKeychain.swift`, `MIMEParser.swift`, `GmailTransport.swift`, `Mail.swift`):
  - Direct on-phone TLS IMAP (`imap.gmail.com:993`, read-only `EXAMINE "INBOX"`, `UIDVALIDITY`/`UIDNEXT` quota-safe delta sync, `BODY.PEEK` unread preservation, byte-accurate `{N}\r\n` literal framing) and implicit TLS SMTP (`smtp.gmail.com:465`, RFC 5322 `In-Reply-To`/`References` threading, dot-stuffing) backed by normalized 16-character App Passwords in iOS Keychain (`com.joat.brain.gmail`).
  - Added unified inbox + per-account filtering, silent first-sync watermarks, `directOnlyOnGlasses` bulk/newsletter gating, `SpamCheck` + `MessageHold` integration, account-preserving tap/voice replies, auto-generated subjects, and UTF-16 confirmation ceiling enforcement (`<= 270` code units).
- **Sub-second SSE streaming JOAT assistant & voice/confirm conflict fixes** (`apps/ios-companion/Sources/OnPhoneAI.swift`, `ClaudeLink.swift`, `MacLink.swift`, `Confirm.swift`, `Brain.swift`, `Messages.swift`, `Navigation.swift`, `Search.swift`, `scripts/build_ios.sh`):
  - Added `OnPhoneAI` streaming router (`GeminiLink` `gemini-3.5-flash-lite` SSE + multimodal vision without 0-quota `googleSearch` tool calls, `ClaudeLink` SSE, and `MacLink` `/agent` SSE fallback), non-blocking `navigation.cachedArea()`, parallel 2.5 s `Search.snippets` grounding, live volatile speech-in-text on `assistant.conversation`, and mode-safe `Generated/gemini.json` seeding in `build_ios.sh`.
  - Fixed all 6 voice/confirmation conflicts: `Messages.draft` `diagnosticKind: "messages"` + 270-UTF16-unit `ConfirmGate.fitMessagePrompt`, retired dead 60 s Shortcuts outbox wait, guarded `confirm.hasPending` (spoken cancel -> `confirm.cancel`; spoken yes -> `StatusBar` touchpad reminder; zero `conversationUntil` clobbering), arbitrated unaddressed `"reply"` across `Mail` and `Messages`, prioritized open `QuickNotes` over `PomodoroCommands` for `"pause"`/`"resume"`, and guarded `WakeGate` against self-echo.
- **4-tab G2 Phosphor Emerald iPhone Duo UI & Apple Pencil Notebook** (`apps/ios-companion/Sources/Theme.swift`, `MailView.swift`, `AppScreens.swift`, `Notebook.swift`, `QuickNotes.swift`, `JOATBrainApp.swift`, `project.yml`, `Info.plist`):
  - Redesigned `JOAT Brain` into 4 tabs (`Home | Mail | Notes | Agents`) supporting portrait + landscape and two-column `NavigationSplitView` layouts on regular horizontal width (`Mail` and `Notes`), folding `Prep` into `Notes` (`All | Pages | Voice | Prep`) and moving `Diagnostics` into `Home`.
  - Upgraded `Notebook.swift` with warm `#FAF8F4` stationery, vector `CAShapeLayer` paper templates (`Ruled`, `Dots`, `Grid`, `Blank`), continuous per-`noteId` auto-save, quota-safe 25 s idle / on-close vision OCR via `OnPhoneAI.readHandwriting`, inline page rename, `Undo`/`Redo`, `PhotosPicker` + `PasteButton`, and the `sparkles` AI Insights & Prep inspector.

## 2026-11-20 — Build JOAT 0.1.151: framed dual-brightness Agent Terminal, 3-level session picker, `/terminal` endpoint, and Brainstorm removal

- **5-container framed dual-brightness Agent Terminal on Even G2** (`apps/hub-plugin/src/NativeTerminalSurface.ts`, `HudPageBuilder.ts`, `TerminalController.ts`, `CapabilityMenu.ts`, `HudPresenter.ts`):
  - Rebuilt the 576×288 Agent Terminal into a 5-container native layout (`terminal-head`, rounded `terminal-card` with `borderWidth: 1, borderColor: 8, borderRadius: 4` as the sole `isEventCapture: 1` owner, 7-row `terminal-dim` at `textColor: 2`, 7-row `terminal-bright` at `textColor: 4`, and 1-line `terminal-foot` at `textColor: 2`), installing once per visit via `RebuildPageContainer` and driving all streaming, live speech partials, scrolling, spinner ticks, and approvals via selective `TextContainerUpgrade` writes (`<= 999` UTF-8 bytes, measured with `@evenrealities/pretext` + `firmwareLineWidth <= 528`).
  - Formatted user turns as `> ` hanging-indent dim rows, tool invocations as 1-line `>> <tool> · <detail>` dim rows, assistant prose full-width on bright (no `AGENT >`, markdown link URLs or `U+0060` backticks), inline voice partials as `> <words>` (`<= 4` rows, no `QUESTION >` or `YOU >`), pending approvals as a firmware-measured Unicode callout box (`╭── Approve command ──╮` / `tap: approve · swipe: deny · 2x: sessions`), and scrubbed raw model IDs from all glasses HUD surfaces.
  - Simplified Even Hub's phone sheet (`apps/hub-plugin/index.html`, `main.ts`) to 2 tabs (**Connection** and **Places**) and packed `0.1.151` (`apps/hub-plugin/joat.ehpk`, 177063 bytes, SHA-256 `22faa35c55f42a264e8628968e705cc0252f1e000d09b2e58ae1d6eefb9c073b`).
- **3-level touch session navigation, multi-session Antigravity, Brainstorm removal & `/terminal` endpoint** (`apps/mac/joat/agents/antigravity_terminal.py`, `services/agent_terminal.py`, `services/capabilities.py`, `services/terminal_endpoint.py`, `core/protocol.py`, `ble/hub_gateway.py`, `services/session.py`, `__main__.py`):
  - Added `sessions.json` (`0600`) conversation indexing (`list_sessions`, `resume_session`) in `AntigravityTerminalAgent` so `+ New session` starts fresh without erasing prior sessions, and wired 3-level navigation (`AGENT TERMINAL` -> `AGENT TERMINAL · <LANE>` listing `Active · …`, `+ New session`, and saved sessions -> live framed session, with `2x tap` stepping back through Sessions -> Agents -> Overview).
  - Purged `brainstorm.view`, `brainstorm.action`, `control.state`, `control.action`, `DebateSession`, and `PhoneControlScreen` across the shared wire protocol, Hub plugin, Mac engine, `JOATShell`, and iOS companion, replacing `brainstorm.action` with `terminal.action` (`on_terminal_action`).
  - Added bearer-authenticated loopback `TerminalEndpoint` (`127.0.0.1:8790`, `~/Library/Application Support/JOAT/terminal-token` at `0600`, served over Tailscale Serve at `/terminal`) with secret-redacted `GET /v1/state` and `POST /v1/config` (`set_model`, `set_cwd`, write-only `set_gemini_key`, `new_session`, `set_assistant_seat`, and CLI account/sign-in actions).
- **`JOAT Brain` iOS `Agents` tab & simulator visual proof** (`apps/ios-companion/Sources/MacTerminal.swift`, `AppScreens.swift`, `JOATBrainApp.swift`, `scripts/build_ios.sh`, `scripts/simulator_antigravity_flow.py`): replaced the obsolete Shortcuts `Messages` tab in `JOAT Brain` with `AgentsView` backed by `Generated/mac-terminal.json`, and verified the full 3-level Agent Picker -> Session Picker -> `+ New session` -> live speech-in-text -> boxed shell approval -> `2x tap` Sessions -> `2x tap` Agents flow on `evenhub-simulator`.

## 2026-11-20 — Build JOAT 0.1.150: live speech-in-text and fast tap-to-speak in Agent Terminal

- **Live speech-in-text and voice state feedback in Agent Terminal** (`apps/hub-plugin/src/NativeTerminalSurface.ts`, `HudPresenter.ts`, `PromptedInputController.ts`, `EventRouter.ts`):
  - `HudPresenter.showPromptPartial()` now forwards `prompt.partial` updates for inline terminal prompts instead of limiting partials to Messages reply cards.
  - `NativeTerminalSurface` renders live `YOU > …` words wrapped to firmware metrics and tail-sliced within `MAX_PROMPT_ROWS` via serialized `TextContainerUpgrade` writes (zero page rebuilds), shows `LISTENING` (`Listening · tap: done · 2x tap: cancel`), transitions immediately to `TRANSCRIBING…` (`Transcribing… · 2x tap: cancel`) when the wearer taps to finish (`prompt-commit`), ticks elapsed `WORKING · Ns` locally while a turn runs, and shows `APPROVAL REQUIRED` alongside `[approval] …` lines in the conversation body.
  - Packed `0.1.150` (`apps/hub-plugin/joat.ehpk`, 180839 bytes, SHA-256 `72b6a2919a8931e6d53c7ac453f0429be72001c7ed4f7fde2deb4f259349ee01`).
- **Low-memory Mac STT latency & single-flight locking** (`apps/mac/joat/services/models.py`, `apps/mac/joat/audio/whisper_serve.py`, `apps/mac/joat/services/orchestrator.py`, `apps/mac/joat/__main__.py`):
  - `recommended_stt_model()` selects WhisperKit `base` (~145 MB, ~0.1 s warm) by default on Macs with <= 12 GiB RAM instead of `large-v3_turbo` (~3 GB, which swapped past 60 s under load), while keeping `large-v3_turbo` on >= 16 GiB Macs and keeping all models switchable at runtime.
  - `WhisperServeEngine` serializes inference with `_infer_lock` (skipping partial requests when inference is already in flight instead of piling up concurrent HTTP POSTs) and skips the wake-word bias prompt when flushing an armed prompt answer.
  - `VoiceOrchestrator.commit_prompt_answer()` sends an empty `PromptPartial` when tapped before speech starts so the HUD resets `TRANSCRIBING…` back to `LISTENING`.
- **Antigravity lane & simulator verification** (`apps/mac/joat/agents/antigravity_terminal.py`, `scripts/simulator_antigravity_flow.py`, `scripts/simulator_harness.py`): added snake_case tool argument formatting (`command_line`, `target_file`, etc.), stripped `<thought>` tags and `file://` markdown links for 576x288 legibility, isolated simulator manifests from live loopback ports, and verified the end-to-end tap-to-speak -> live speech-in-text -> tap-to-done -> confirm -> touchpad approval flow on `evenhub-simulator`.

## 2026-11-20 — Build JOAT 0.1.149: phone brain back in the package allowlist, and Antigravity session persistence

- **Phone brain restored in the packed plugin.** `apps/hub-plugin/.env` on the build Mac omitted `JOAT_RELEASE_ALLOW_LOOPBACK=1`, so 0.1.148 packed only the Tailscale WSS Mac origin and dropped `ws://127.0.0.1:8787/glasses` (`This iPhone · JOAT Brain`) from both the candidate list and the manifest network allowlist. Added `JOAT_RELEASE_ALLOW_LOOPBACK=1` and packed 0.1.149 (`apps/hub-plugin/joat.ehpk`, 180304 bytes, SHA-256 `0926104b7d34bd6f1da0250164f19b4893f24a695f0d572c0880595604da32a4`).
- **Antigravity lane** (`apps/mac/joat/agents/antigravity_terminal.py`): saves and resumes conversations across JOAT restarts, isolates `save_dir` and `app_data_dir` under `~/Library/Application Support/JOAT/antigravity/`, formats Antigravity tool call and approval arguments (`CommandLine`, `AbsolutePath`, etc.), surfaces retryable 503/429 model delays with a bounded timeout and one-line errors, reads saved session history for `terminal history`, and adds `set-key` / `status` / `check` CLI helpers.

## 2026-11-19 — Brainstorm leaves the menu; OpenClaw and Hermes leave the Agent Terminal (plugin 0.1.147, JOAT Brain 202611182154)

- **Brainstorm** is gone from the glasses menu (`types.ts` CAPABILITIES), the phone's hand-off list and its spoken openers ("open brainstorm" now goes to the assistant), since the conversational JOAT assistant handles open-ended brainstorming directly. The Mac engine's Brainstorm code is unreachable from the glasses and is left in place for now.
- **Agent Terminal** lists only the lanes JOAT hosts (Claude Code, Codex, Antigravity). OpenClaw and Hermes lanes and A2A peers are no longer put on the roster; their boot code stays inert.
- The Mac's `ax-banners` notification source logs its startup "file not there yet" once per spell instead of every 5 s.
- The phone's Messages page reads the Mac's conversations and history (202611182027); `build_ios.sh` and `pack.sh` run on the mini (Tailscale CLI from the app bundle, the evenhub CLI).

## 2026-11-19 — Texts no longer go missing when the glasses are off or the phone slept

Texts could be missed when the glasses were off or the phone was asleep. Two causes in JOAT Brain, both fixed:

- **Late texts got no card.** A text more than ten minutes old when the phone caught up (JOAT Brain suspended, or off the tailnet) was saved quietly and summed up in one "N texts while you were away" card. Now every text gets its own card, in order; only the old ones are not read aloud.
- **A card raised to absent glasses was lost.** A text that arrived while the glasses were off, in the case, or disconnected was painted anyway, and the next card replaced it. `MessageHold` (Messages.swift) now holds such texts and shows each one when the glasses are back on and connected.
- The phone keeps 50 cards instead of 10, so a backlog stays in the notifications list.
- Not solved here: if iOS suspends JOAT Brain itself, texts wait on the Mac and arrive when it resumes (now each as its own card).

## 2026-11-19 — The assistant can look up your contacts

- **Mac app** (`PersonalDataBridge/ContactsService.swift`): read-only `contacts.status` and `contacts.search` on the native bridge, from Apple's Contacts store (whatever accounts the Mac syncs). Search by name or nickname, email, or phone number; up to 25 results with phones, emails, company, job title and birthday. The permission prompt only comes from the Mac UI: Access > Contacts > Grant Access (new row). `NSContactsUsageDescription` and the Hardened Runtime `personal-information.addressbook` entitlement are added in `project.yml` (XcodeGen rewrites `JOAT.entitlements` from it).
- **Engine** (`agents/contact_tools.py`): belt tool `contacts_lookup`, reachable by every assistant seat; results are marked as the wearer's data.
- Verified live on the mini over the agent endpoint: "what's on my calendar today?" called `calendar_agenda` and answered; a contacts question called `contacts_lookup` and, before access is granted, says how to grant it.
- Tests: `tests/unit/test_contact_tools.py`.

## 2026-11-19 — Agent Terminal: Claude Code sessions hosted by JOAT

The Agent Terminal's first lane is now Claude Code, run by JOAT itself (Codex and Antigravity follow). It leads the roster ahead of the OpenClaw and Hermes lanes, which stay until those are removed.

- **`apps/mac/joat/agents/claude_code_terminal.py`.** JOAT runs the session through the official Claude Agent SDK (`claude-agent-sdk`, new dependency), which drives the installed `claude` CLI under the wearer's own login. JOAT therefore owns every turn:
  - streams the reply and each tool call ("⏺ Bash: …") to the lenses;
  - every permission prompt becomes a terminal approval ("terminal approve" / "terminal deny"), and the turn waits for it;
  - "terminal cancel" interrupts the turn and reports whether Claude Code confirmed the stop;
  - each turn continues the same session; "new conversation" starts a fresh one.
- **Existing sessions.** "terminal discover" lists Claude Code's saved sessions from every folder on this Mac, including ones started in a Terminal window. "terminal resume <name>" continues one in its own folder, in a process JOAT runs. JOAT cannot type into, or approve prompts in, a Terminal window it did not start.
- **Settings.** One lane named "claude" in the home folder by default. Optional `~/Library/Application Support/JOAT/claude-terminal.json`: `{"lanes": [{"name": "...", "cwd": "...", "permission_mode": "default"}]}`. Nothing is hard-coded.
- If Claude Code is missing, opening the lane says "Install Claude Code on this Mac and sign in, then retry." instead of a generic dial failure.
- `scripts/build_ios.sh`: finds the Tailscale CLI inside Tailscale.app when it is not on PATH (without this, a mini build silently left out the Mac links), and falls back to the Apple account signed in to Xcode when there is no issuer ID for the App Store Connect key.
- **Codex** (`agents/codex_terminal.py`): JOAT starts `codex app-server` and drives the thread over JSON-RPC: streamed command and file-change items and agent messages, command and file-change approvals answered `accept`/`decline` from the glasses, `turn/interrupt` for cancel, `thread/list` + `thread/resume` for saved threads. Optional `codex-terminal.json` (`name`, `cwd`, `sandbox`).
- **Antigravity** (`agents/antigravity_terminal.py`): Google's `google-antigravity` SDK, hosted by JOAT; shell commands wait for the wearer, cancel stops the turn. It needs a Gemini API key (`GEMINI_API_KEY`, or `~/Library/Application Support/JOAT/gemini-api-key` at 0600): Google meters it, and the SDK does not use the Antigravity app's sign-in. The SDK cannot list sessions, so Antigravity app sessions are not resumable from the glasses.
- "terminal discover" lists the focused lane's sessions (Claude Code or Codex), and "terminal resume" sends the choice back to the lane that listed it.
- Tests: `tests/unit/test_claude_code_terminal.py`, `test_codex_terminal.py`, `test_antigravity_terminal.py` (fakes of each SDK/server; output, approvals, cancel, discover/resume through the real roster). Not yet run live: Claude Code and Codex are not installed on the mini, and there is no Gemini key.

## 2026-11-19 — The assistant can read your calendar and reminders

It said it had no access to your calendar because it had no tool for it, and its instructions listed the calendar as unknown. The Mac already read Calendar and Reminders (the Planning page does), so the assistant now gets the same read-only view.

- **Two belt tools** (`apps/mac/joat/agents/planning_tools.py`): `calendar_agenda` (events and dated reminders for a day or up to 31 days) and `open_reminders` (overdue, then upcoming). Every seat reaches them over the same MCP route as the other belt tools: Claude Code, Codex and the local model.
- Calendar accounts are whatever the Mac holds through EventKit (iCloud, Google, Exchange), so JOAT names none of them. A Google calendar appears once that Google account is added to Calendar on the Mac.
- Titles are marked as the wearer's data, not instructions to the assistant, because an invite can carry text somebody else wrote.
- The "their calendar or schedule" entry is gone from the assistant's list of unknowns.
- Not yet: Contacts (the Mac has no Contacts bridge), and the phone-side Claude path, which has only a search tool.
- Tests: `tests/unit/test_planning_tools.py`. `tests/integration/test_whisper_cold_start.py::test_shutdown_after_foreground_timeout_reaps_child_and_stops_retries` fails on a clean checkout too and is unrelated.

## 2026-11-18 — Messages through the home Mac: the glasses stay on the iPhone

The glasses connect to the iPhone alone. When a home Mac holds your Messages, JOAT Brain on the phone asks it, over the tailnet, for new texts and sends replies through it. No Shortcuts automation is needed for Messages on such a phone, and nothing switches by hand.

- **The Mac's Messages door** (`apps/mac/joat/services/messages_endpoint.py`, `--messages-endpoint`, on whenever the Private Tailnet setting is).
  - Loopback only, port 8789, carried over the tailnet by `tailscale serve --bg --set-path /messages http://127.0.0.1:8789` (never Funnel). Its own bearer token lives in `~/Library/Application Support/JOAT/messages-token` (0600, or `JOAT_MESSAGES_TOKEN`).
  - `GET /v1/events` long-polls for new texts. A phone that has never connected gets only the current position, so a mailbox that iCloud filled with years of history is not replayed.
  - `GET /v1/threads`, `GET /v1/threads/<id>/messages`, `POST /v1/send` (to a thread or a number or email; a retry with the same `client_id` never texts twice).
  - Chat GUIDs and numbers stay on the Mac. The phone holds opaque thread ids (an HMAC under the token), so a forged id can't redirect a reply.
  - `IMessageStore.read_new_texts` is a stateless row-id read, so it does not take the Mac's own reader's change edge.
- **The phone** (`MacMessages.swift`, `Messages.swift`, `Brain.swift`). It long-polls from now, runs each text through the same spam check and cards as an automation's text, and sends a confirmed reply through the Mac. Group replies work this way, because the Mac replies to the chat itself. A text is never dropped for being late: texts that arrive while the phone was away are saved to Messages, and one card ("N texts while you were away") says so instead of reading each onto the glasses. An address and token that come from the build (`scripts/build_ios.sh` writes git-ignored `Generated/mac-messages.json`); without them the phone keeps the Shortcuts road.
- If both roads are on, a text can arrive twice: turn the Shortcuts Message automation off on a phone that uses the Mac.
- Tests: `tests/unit/test_messages_endpoint.py`; the engine-arguments test now expects `--messages-endpoint`.

## 2026-11-14 — JOAT Brain 1.0 (202611140823): a text reaches the glasses even when Find Conversation fails

Build 202611131322 went to TestFlight this morning, once Apple's agreement was accepted. This build follows it. No plugin change.

- **A text never reached the glasses** (`Messages.swift`, `Intents.swift`, `Shortcuts/source/Show Texts on Glasses.plist`). In Show Texts on Glasses, Shortcuts could fail with "The action “Find Conversation” could not run because an unknown error occurred."
  - Find Conversation was the shortcut's first step, and Shortcuts can't catch a step's error. So the next step, Show Text on Glasses, never ran.
  - The shortcut is now: Show Text on Glasses (sender and text), Find Conversation, the new Add Group to Text (the conversation's Group Name), Mark as Unread.
  - JOAT Brain saves the text at the first step and shows it when the group arrives, or after 2.5 s without one. A failed Find Conversation now costs only the group's name.
  - Simulator: a text whose group came a second later showed as "Weekend Crew: Jordan: Bringing the pie tonight." A text with no group step showed 2.6 s later as one person's (log only, no picture).
  - The new shortcut imports on the Mac. It is not on the phone yet: it needs this build first, and the Mac's Shortcuts library syncs to the phone.
  - The in-app "Add Show Texts on Glasses" button still opens the earlier shortcut.
- **Group sends, what was tried** (no code change):
  - Messages' own Send Message intent (`com.apple.MobileSMS.SendMessageIntent`) takes a conversation, but a shortcut containing it won't import: "contains features not supported on this device" (macOS 27.2).
  - The Send Message step Shortcuts offers takes only people (the Mac's Shortcuts tool database), so sending to a Conversation object fails in Shortcuts.
  - Apple, MacRumors, 9to5Mac and Appdook all say iOS 27 Send Message supports group chats. None says how a shortcut hands it the group.
  - Group replies from the glasses are still refused.

## 2026-11-13 (afternoon) — Glasses 0.1.146 and JOAT Brain 1.0 (202611131322): texts to one person stop going through the group branch, and Messages answers after Back

JOAT Brain 1.0 (202611131322) archived. The TestFlight upload was refused again for the same reason: Apple's updated agreement has to be accepted in App Store Connect. Glasses 0.1.146 packed (phone brain on the allowlist).

- **Why 1:1 texts hit the group branch and failed** (`Intents.swift`). In the Send shortcut, the group branch errored on an individual reply: "Send Message failed because Shortcuts couldn't convert from Conversation to Contact, Phone Number, or Email Address."
  - "Get Text to Send" gave Group as "" for one person, and the shortcut's "If Group has any value" is true for "".
  - Every text to one person therefore ran Find Conversation and passed whatever recent conversation it returned into Send Message instead of the recipient's direct handle.
  - JOAT's own resolved recipient handle was right every time in the log.
  - Now Group has no value at all for one person, and a new yes/no field, Is Group, is true only for a group.
  - The installed shortcut needs no change: its Otherwise branch sends to Recipient, the number JOAT checked.
  - Not testable in the simulator (no Shortcuts or Messages app there). The first text on the phone tells: the log says `group=none`, then `text out sent`.
- **Group texts are refused** (`Messages.swift`). Send Message can't send to a conversation, so the group branch never sent a group text. A reply to a group now says "Group texts can't be sent from the glasses yet. Reply to one person instead." before anything is dictated. Simulator: the card showed on the Family thread, and a tap returned to the thread.
- **"Mac did not answer" in Messages** (`CapabilityMenu.swift`). Opening a conversation emptied JOAT Brain's list of who is on each row.
  - Back on the list, a tap on a person's row was dropped with no answer, and after 12 s the plugin said "Mac did not answer. Tap to retry Messages."
  - Now the rows are kept. A row JOAT doesn't know gets the list again, never silence.
  - Simulator: opened Family, went back, tapped Jordan. That conversation opened and was still there 12 s later.
- **No more sitting on "Sending…"** (`Messages.swift`). When the Send shortcut stops with an error, it never reaches "Text Sent on Glasses". After 20 s the glasses now say "Not sent: the Send shortcut stopped with an error. Open Shortcuts on the iPhone to see it."
- **Glasses 0.1.146: no "Mac" when the phone is the brain** (`CapabilityMenu.ts`, `HudCanvasRenderer.ts`):
  - "JOAT did not answer. Tap to retry Messages."
  - "No response from JOAT yet."
  - Offline: "JOAT is not connected. Open JOAT Brain on the iPhone, or JOAT on the Mac."
  - Reconnecting: "Reconnecting to JOAT…"

## 2026-11-13 — JOAT Brain 1.0 (202611131209): no text to a number that isn't the person's, and a note stays on the glasses

JOAT Brain 1.0 (202611131209) archived. The TestFlight upload was refused: Apple requires an updated agreement to be accepted in App Store Connect (403 FORBIDDEN.REQUIRED_AGREEMENTS_MISSING_OR_EXPIRED). No plugin change.

- **Verified destination handles for 1:1 messages** (`Messages.swift`, `Recipients.swift`). Hardened recipient verification so a 1:1 message can never be sent to an unverified or unrelated handle (see also the Send shortcut group-branch fix above):
  - A text to one person goes out only to a number or email that Contacts says is theirs. Anything else is refused before the confirmation, with the reason on the glasses ("Not sent: I have no number for …" or "…0199 isn't …'s in Contacts"). A bare name is never handed to Send Message. A number not in Contacts goes out only when the glasses showed that number itself. Group texts are unchanged.
  - The confirmation shows the number's last four digits: "Send to Jordan (…0123)?".
  - A reply never reuses the number an earlier send recorded. A wrong number stored with a sent text was otherwise used for every reply after it.
  - The log line for each send now says which number it went to (last four digits).
  - Simulator: New message to a saved contact asked "Send to Jordan (…0123)?" and queued it. A reply to a sender with no saved number said "Not sent: I have no number for Jordan." and asked nothing. A group reply went to the group, as before.
- **A note "stopped halfway"** (`CapabilityMenu.swift`). Twice in one meeting, the wearer's note stopped partway through.
  - Simulator: a new plugin page connected mid-recording. That's what happens when the phone locks and Even reloads the plugin in the background, or when the link drops and comes back. The glasses went to Home with nothing saying a note was open, while JOAT Brain went on recording out of sight.
  - Now a new plugin page with a note open puts the note back on the glasses. Simulator: REC 00:13 then 00:19 after the reconnect.
  - Not yet confirmed that this was the cause on his phone: his log will say ("rx auth" in the middle of a note).
- **No swipe stops a note.** On the open note a swipe scrolls the transcript and nothing else. What does pause or stop one:
  - a tap (record / pause);
  - 2x tap (Save / Discard / Cancel);
  - the glasses reporting they're off, which stops the mic. With "Ask first", it stays off until "Resume listening?" is answered.

## 2026-11-12 (later) — Glasses 0.1.145 and JOAT Brain 1.0 (202611122136): Brainstorm opens on the Mac, and the send shortcut fixed

Glasses 0.1.145 packed (`joat.ehpk`; the wearer uploads it to Even Hub). JOAT Brain 1.0 (202611122136) uploaded to TestFlight.

- **Brainstorm on the Mac, from the phone brain** (`BrainHandoff.ts`, `CapabilityMenu.swift`). The wearer chose to keep Brainstorm on the Mac. Until now, with the phone as the brain, Brainstorm said "not on the phone yet".
  - Now, when he picks Brainstorm, JOAT Brain answers `brain.handoff`. The plugin moves the glasses to the first of the package's Macs that answers, tried in the package's order, with the Mac that answered last time first. No Mac is named in the code. The plugin opens Brainstorm there.
  - When the wearer goes Home, the glasses go back to the phone brain. They also go back if the Mac stays away for 20 s.
  - When no Mac answers within 10 s each: "No Mac answered. Is JOAT open there, with Tailscale on?"
  - The Agent Terminal and Activity, also Mac-only, are handed over the same way.
  - The glasses hold one brain at a time. That's one plugin connection, and the Mac lets one display hold the glasses. So while Brainstorm is open, phone-only things such as texts, phone reminders and QuickNotes wait until Home. Notification cards are pushed again then, and a reminder that came due is shown again.
  - A hand-off is never saved as the preferred brain, so a relaunch starts on the phone.
  - The plugin says `handoff: true` in its hello. An older plugin still gets "not on the phone yet". The Mac ignores the new field.
  - Simulators: JOAT Brain sent the hand-off and the plugin moved to a stand-in Mac on 127.0.0.1:8798, which drew Brainstorm READY. 2x tap went Home, and the plugin was back on JOAT Brain. The real Mac rightly refused the simulator: it has no pairing token. Unit tests cover the order, no answer, going Home while dialing, a Mac that goes away and comes back, and the wearer's own choice of brain.
- **Send shortcut: the group search fixed** (`Shortcuts/source/Send Message from JOAT Brain.plist`). On the wearer's iPhone, Find Conversation's group filter showed an empty red field. The file searched by `conversationName`, which iOS doesn't offer as a filter; the filter it offers is Display Name. The filter is now `displayName` is Group.
  - On his phone the trigger also had an empty "Subtitle contains" row ("Configure 'Subtitle' to enable this automation"). It isn't in the file. Remove the row with ⊖.
  - The in-app Add to Shortcuts link still installs the earlier copy. The wearer is fixing his on the phone; a new link follows once it sends.
- **A one-person thread is not a group** (`Messages.swift`). If the show shortcut's Group is the sender's own name, the text is treated as one person's.

## 2026-11-12 — JOAT Brain 1.0 (202611122018): tap records again, a mic check, one-tap Messages shortcuts, white notebook paper

JOAT Brain 1.0 (202611122018) uploaded to TestFlight. No plugin change.

- **Tap records, long press talks** (`QuickNotes.swift`). Restored tap-to-record on the open note, with AI cues moved into the contextual menu so tap-to-record and push-to-talk both work:
  - Since 2026-11-07 a tap on the open note opened the AI cues. That left holding the touchpad as the only way to record from the glasses.
  - Now:
    - A tap records or pauses.
    - A long press talks one paragraph.
    - 2x tap gives Save / Discard / Cancel.
    - AI cues and Prep notes are in the note's menu (tap, then long press).
  - The menu opens with a tap and then a long press, and that tap reaches the note first. When the menu opens within 3 s of it, the recording goes back to how it was.
  - New hints: "tap: record · hold: talk", and "● REC · tap: pause".
- **Checking the glasses' mic** (`AudioCheck.swift`). A Genesis 26 reading through the car's speakers kept 146 of 345 words (42%). The words it kept were nearly all right; whole stretches of 10 to 30 words were missing. The wearer's own voice comes through whole.
  - Even says the G2's four mics "isolate the wearer's voice from background noise".
  - The same chapter, spoken by macOS `say` at 16 kHz and run through the same engine on the Mac, kept 290 of 347 words (84%). No stretch of 5 or more words was missing; the misses were "thee", "thy" and "unto".
  - So the engine doesn't drop stretches on clear audio. Now JOAT Brain shows what the glasses send:
    - While a note records, the log gets the glasses' sound level once a second: frames, seconds of sound, RMS and peak in dBFS.
    - Diagnostics › Save note audio (off by default) keeps each note's audio as a WAV, to share and listen to.
    - Diagnostics › Share the log sends `brain.log`.
  - Simulator: the level lines came every second while recording. A 12 s WAV played as 16 kHz mono.
- **Messages: one-tap install, and a three-action shortcut** (`Intents.swift`, `Messages.swift`, `Shortcuts/`).
  - The show shortcut is now **Show Texts on Glasses**: Find Conversations (the newest), Show Text on Glasses (From: the sender, Message: the text, Group: the conversation's name) and Mark as Unread. It replaces about a dozen actions.
  - JOAT Brain looks up the sender's name in Contacts and takes the group from its new **Group** parameter. An older shortcut that sends "Group (Person)" still works.
  - JOAT Brain › Messages: **Add to Shortcuts** buttons open iCloud links to the two shortcuts, straight into Shortcuts. They were made in Shortcuts on the Mac: Share › Copy iCloud Link. Apple's `shortcuts://import-shortcut` takes only iCloud links.
  - The in-app steps follow iOS 27, per Apple's guide: Info › Privacy › Allow Running When Locked, then Always Allow if an action asks. On iOS 27 a shared shortcut carries its automation ("Automations are now built directly into shortcuts, including shared shortcuts").
  - Simulator: a text from a raw number in "Book Club" showed as "Book Club · <the contact's name>: …".
- **Notebook: white paper in Dark Mode** (`Notebook.swift`). In Dark Mode, PencilKit drew black ink as white on a black page, and the palette showed the black swatch as white (simulator). The page is now always white paper (`overrideUserInterfaceStyle = .light`), and the palette shows its colors as they draw (`colorUserInterfaceStyle = .light`). The note's page preview is ink on white too.
- **The blank color picker:** not fixed yet, because the cause isn't known.
  - Apple's color picker is drawn by iOS outside the app. In the wearer's screenshot, the Paste button, also drawn by iOS, was blank as well.
  - Simulator: no leak. After five pages and three trips to the background, one page view and one tool picker were alive each time, and the picker drew every time.
  - Apple's forums report system-drawn controls going blank in an app that has run for hours or days.
  - The page-open log line now gives the page views and tool pickers alive, and how long JOAT Brain has been running.
  - Next: when the picker is blank, force-quit and reopen JOAT Brain.

## 2026-11-11 (later) — JOAT Brain 1.0 (202611111326): prep notes are folders, and the glasses pick one

JOAT Brain 1.0 (202611111326) uploaded to TestFlight. No plugin change.

- **Goal.** Group multiple preparation items into a folder-style prep note so selecting a prep note feeds every file, link, and page inside it into the active conversation.
- **Even's own sources:**
  - The Conversate article: create a Prep Note by typing preparation details, by uploading supporting files, or both ("manually enter preparation details while simultaneously uploading supporting files"). Then select it to start, or tap Start for none.
  - The Even app's release notes: "Add notes or upload documents to automatically generate Prep Notes."
  - Read on 2026-11-03, the same article gave the limits: 5,000 characters, or five PDF/DOCX/TXT files of 5 MB each. Today's version doesn't give them.
- **Before:** each file, link, typed note or notebook page was its own prep item, and the cues used only the one ticked.
- **Now** (`Prep.swift`):
  - A prep note holds typed notes, files, links and notebook pages, as many as needed.
  - Talking points (up to eight) come from all of it. They're made again when an item is added or removed, and again if it changed while they were being made.
  - The AI cues get every item. The 60,000-character cap is shared evenly, and a short item leaves its share to the others (`Prep.Note.material`).
  - A prep note keeps a name the wearer gives it; otherwise Claude names it.
  - Earlier prep items each became a prep note holding that item (the wearer's choice). The one in use stays in use.
- **JOAT Brain › Prep:**
  - New prep note.
  - The list: tick the one in use; tap one to open it.
  - In a prep note: its name, Add files… (several at once), Paste (a link or text), Type notes…, the items (swipe to remove), and the talking points.
  - Share a file to JOAT Brain: it asks which prep note, or a new one (the wearer's choice).
  - Notes › a page › Add to a prep note… asks the same.
- **On the glasses** (`QuickNotes.swift`), as Even's Conversate: + Create new note asks for a prep note ("QUICKNOTES · PREP NOTE"). The one in use is first (●), then the others, then Skip & start. 2x tap goes back to the list.
  - Skip & start: that note has no prep note; the cues give no prep answers, and its cue list has no prep row.
  - The prep reader lists what the talking points came from.
  - The title isn't "QUICKNOTES · PREP": the plugin draws that title as a text page, so its rows couldn't be chosen. On the new title, 2x tap goes back to the list in the plugin itself, so the next tap there counts as a list tap.
- **Simulator:**
  - UI test `PrepUITests`: a new prep note, named, with typed notes and pasted text. Its four talking points came from both (the budget, and the pasted learning rate). The list showed it in use with 2 items.
  - Two simulators (`e2e_1030.py prep`, `prepskip`): two old-format prep items became two prep notes. The glasses' picker showed both and Skip & start. 2x tap went back to the list. The second prep note was picked, and its talking points showed in the note. Skip & start opened a note without prep.
  - The notebook test's Add to a prep note → New prep note, and the other tests, still pass.
- **Not tested here:** a file shared from another app (the Share sheet), and the picker on the G2.

## 2026-11-11 (afternoon) — JOAT Brain 1.0 (202611111237): a notebook page is Letter paper, and a page can't be lost

JOAT Brain 1.0 (202611111237) uploaded to TestFlight. No plugin change.

- **Goals.**
  - A page started on the Duo's outer screen and continued on the inner screen in landscape showed a big gap: the page was as wide as the screen, so the typing reflowed and the ink stayed where it was.
  - Standardize on a fixed paper sheet size with horizontal and vertical scrolling when zoomed in.
  - Auto-save drafts continuously so an open page is never lost if the editor closes or the app is suspended before tapping Done.
- **The page is paper** (`Notebook.swift`, `PageView`):
  - US Letter, 612 × 792 points a sheet, as in GoodNotes (Letter by default in the US; its pages match the paper they print on) and Notability. It doesn't depend on the screen, so the typing wraps the same everywhere.
  - It opens zoomed to fill the screen's width: zoom = view width ÷ page width, as Apple's PencilKit sample fits its fixed-width canvas. On the Duo's outer screen the page's view is 382 points (beside the sensor bar), so 0.62×. The inner screen is 669 points wide, or 951 in landscape (the simulator's device profile).
  - Pinch to zoom in (up to 4×, or 4× the fitted zoom if that's more); zoomed in, it scrolls sideways as well as up and down.
  - When the width changes (folding, unfolding), the zoom keeps its ratio to the fitted zoom, and the same part of the page stays in view.
  - A dashed line and the sheet's number mark where a sheet ends. Another sheet is added as the writing nears the end of the last.
  - Pasted images fit the sheet's width.
  - A page written before keeps its ink: the page is wider than Letter when its ink reaches further, since it was as wide as the screen it was written on. Its typing now wraps at the page's width.
  - Typed text is redrawn sharp up to 2.5× zoom, not 4×: each line and pasted image is drawn at that scale, and more costs memory the phone may not have.
- **A page can't be lost:**
  - Code review found nothing that deletes a saved page. Until now, though, nothing was written until Done, and Done saved what the editor's SwiftUI state held. A page whose editor went away without Done, or whose app was stopped (by iOS too), was lost.
  - The page now writes itself to disk 1.5 s after each change, and at once when JOAT Brain leaves the screen (`pages/draft.*`). Cancel drops it.
  - Done saves the page from the editor itself, so a draft that couldn't be written doesn't matter. (The Mac's disk filled up during a test run, and a draft wasn't written: "errno 28".)
  - A page left behind is saved as Done would have: when its editor goes away, at the next start of JOAT Brain, when the Notes list appears, or before another page opens. A new page whose editor went away carries on from it.
  - What happened to yesterday's pages is still open: the wearer was asked whether they showed in Notes after Done, and whether the phone was folded or the app left while they were open.
- **A page starts with a pen.** The tool picker keeps its last tool from page to page. Left on the eraser, a new page had no pen to go back to, so every stroke erased. The simulator's notebook test found it: its strokes left no ink.
- **Simulator** (UI test `PaperUITests`, iPhone Duo simulator):
  - The page's view narrowed to 300 points, then back to 382, as a fold changes the width. The page filled the width both times, and the text wrapped the same at both widths (screenshots).
  - Zoomed in: one finger scrolled the page sideways.
  - Typed, waited, quit JOAT Brain without Done; at the next start the page was in Notes.
  - Typed, then the editor was closed without Done or Cancel; the page was in Notes.
  - The notebook, zoom, paste and screens tests still pass.
- **Not tested here:**
  - A real fold: the simulator can't fold, so the test narrows the page's view instead.
  - Turning the phone: JOAT Brain lists portrait only, so the simulator doesn't turn it.
  - XCUITest reports the zoomed text view's frame scaled twice (612 × zoom × zoom). The app's layout log has the right width, and the test allows for it.

## 2026-11-11 (later) — JOAT Brain 1.0 (202611110940): paste words and images into a notebook page; Recent texts show their date

JOAT Brain 1.0 (202611110940) uploaded to TestFlight. No plugin change.

- **Goal.** Support pasting text and screenshot slides inline into notebook pages so Claude reads both handwriting and pasted slide images.
- **Paste** (`Notebook.swift`):
  - Type: the edit menu's Paste (and ⌘V) puts words, or an image, at the cursor. A text view pastes only words by itself (`allowsEditingTextAttributes` is off), so `PastingTextView` handles images. Choosing Paste in the edit menu asks no "Allow Paste?".
  - Write and Type: a Paste button in the toolbar (SwiftUI `PasteButton`, also no "Allow Paste?"). In Write it adds to the end of the page.
  - The keyboard's "Photo · Paste from …" suggestion (iOS 27) and drops onto the typing go through the text view's paste delegate (`UITextPasteDelegate`).
  - Each image sits on its own line, as wide as the page (or its own width), and follows the page's width.
  - Kept beside the ink: each image as a JPEG of up to 2048 px (`pages/<id>-<hash>.jpg`), and its place in the typing (`pages/<id>.page.json`). An image taken off the page loses its file at Done.
  - The saved page shows the images under the ink.
- **Claude reads them** (`ClaudeLink.readHandwriting`): up to 10 pasted images (long edge at most 1568 px) go with the handwriting. Claude writes each one's words after the handwriting (title, then points; no player controls), or "[image: …]" for a picture with no words. The page's text, title, overview and next steps then include the slides. "Read the pasted images" shows when a page couldn't be read.
- **Messages › Recent:** a text from before today shows its date as well as its time. The wearer's three-day-old texts showed a time only.
- **Simulator** (UI test `PasteUITests`, iPhone Duo simulator; screenshots in `docs/IOS_APP.md`):
  - Typed a line, then pasted words with the edit menu: they landed as typed.
  - Pasted a 1280 × 720 slide with the edit menu (Type) and a second with the toolbar button (Write, at the end).
  - Done: the saved page shows both. Claude read their titles and points into the page's text (193 characters, under 2 s) and titled the page.
  - The notebook, zoom and screens tests still pass.
- **Fixed on the way:**
  - At its own size, a 1280-point slide showed cut off and pushed the toolbar off the page. Images are now sized to the page.
  - UIKit's own paste put a blank line before pasted words once the view took images. Words are now put in as typed.
- **Not tested here:**
  - The keyboard's paste suggestion: the test's tap didn't reach it.
  - Drops.
  - The toolbar button with a copy from another app. Copies made by the test runner didn't load in the app; a copy made by JOAT Brain itself pasted.
  - A real screenshot on the phone.

## 2026-11-11 — JOAT Brain 1.0 (202611110811): a new message no longer stops after the name

JOAT Brain 1.0 (202611110811) uploaded to TestFlight. No plugin change.

- **Symptom.** Starting a new message recognized the spoken recipient name, then closed back to the Messages list before asking for the message body.
- **Code review.** After "Who should I text?" is answered, JOAT Brain closed the question card at once, then looked the contact up.
  - The lookup had no time limit and showed nothing on the glasses. A name that isn't one exact contact name goes to Apple's on-device model (up to 300 contacts; rate limited for an app in the background), then Claude (up to 1500 contacts; the request allows 90 s).
  - Meanwhile the glasses sat on the Messages list. A tap there cancels the draft, and a lookup that finished after that asked nothing.
  - The simulator check of 2026-11-07 had two contacts (0.7 s), and its seeded answers ignore cancels.
- **Fixes:**
  - The recipient question stays on the glasses after the answer and shows "<name> · looking up…"; "What should I say to …?" replaces it (`Prompts.ask(hold:)`, `note`). No match takes it down.
  - One contact with every word said (a first or a last name) is picked with no model.
  - Time limits: the on-device model 4 s, the whole lookup 12 s.
- **Simulator** (two simulators, seeded answers): New message → "my wife" → the test contact, an on-device pick in 1.7 s → "What should I say to …?" → "See you at seven" → confirm → queued for the send shortcut. A reply in Family still goes to the group. The question cards can't be seen there: Even's simulator closes each question about 50 ms after showing it (its mic quirk).
- **Not tested here:** the held card and "looking up…" on the glasses, and a lookup against the wearer's real contacts.

## 2026-11-10 (later) — JOAT Brain 1.0 (202611102035): notebook: pinch to zoom, the eraser hands back the pen, Edit page fixed; Messages shortcuts and JOAT Brain docs in the repo

JOAT Brain 1.0 (202611102035) uploaded to TestFlight. No plugin change.

- **Pinch to zoom**:
  - Up to 4×, in Write and in Type. In Write, two fingers scroll and zoom; in Type, one finger scrolls.
  - The canvas zooms itself, as in Apple's PencilKit sample, so the ink stays sharp. Zooming a parent scroll view blurs PencilKit ink (Apple forums 698317).
  - The typed text has its own scroll view under the canvas; the two follow each other's scroll and zoom (`PageView.follow`). Write moves the canvas, Type moves the text. The text is redrawn at the zoom it's shown at.
  - Simulator: a box drawn at 1× zooms in and comes back to the same spot, in both modes. Strokes drawn at 2.3× and 4× land in place on the saved page. The typing shows at the ink's zoom.
  - First try, the text inside the canvas: the text view left the accessibility tree (VoiceOver and the UI test couldn't find it). Hence the separate scroll view.
- **The eraser hands back the pen**:
  - `canvasViewDidEndUsingTool` (the Pencil lifted): if the tool picker's item is the eraser, it goes back to the last inking item (`PKToolPicker.selectedToolItem`, iOS 18; `selectedTool` is deprecated).
  - Simulator: eraser picked, one stroke erased part of a line, the log said "eraser lifted, back to com.apple.ink.pen", and the next strokes drew in ink.
- **Messages shortcuts in the repo** (`apps/ios-companion/Shortcuts`): the unsigned shortcut sources, `make_shortcuts.py` to point them at a team and bundle ID and sign them, and install steps.
  - Anyone-mode signing: the certificate is Apple's, with a random common name and no person in it.
- **Docs:** `docs/IOS_APP.md` (screens, the notebook, Messages, building your own) with screenshots from the iPhone Duo simulator, and the README's "On the iPhone".
  - `build_ios.sh` builds `ANTHROPIC_API_KEY` into the app, so the docs say to keep a build to yourself.
- **Example names:** contact nicknames in code comments, simulator seeds and this changelog use synthetic placeholder names.

- **Headless Xcode Simulator UI testing:**
  - When Simulator.app is not running visibly, `simctl io screenshot` returns "Invalid argument"; XCUITest captures PencilKit Metal layers reliably in headless runs.
- **A UI test** (`dist/sim-cues-1030/uitest/`, run with `zsh run.sh`). It is its own XcodeGen project that includes `apps/ios-companion/project.yml`, so the app's project is unchanged.
  - Finger strokes (the Simulator has no Pencil) write "SHIP V2" and "MEET SAM AT 4" in block capitals.
  - Then it switches to Type, types "Design review, Thursday", scrolls, switches back to Write, adds a tick and taps Done.
  - It opens the saved page, then does Edit + Cancel, Edit + Done, and Use as prep notes.
  - XCUITest's screenshots work headless (`XCUIScreen`, exported from the result bundle). The app's own window pictures miss ink drawn by PencilKit's Metal tiles, so they are not evidence.
- **Results on the iPhone Duo simulator (466 × 678 points; the tabs are a rail at the side):**
  - One finger draws in Write without scrolling the page. In Type, one finger scrolls it. Typing lands on the page under the ink, and switching modes keeps both.
  - Claude read the page as "Design review, Thursday / SHIP V2 ✓ / MEET SAM AT 4" and titled it "Thursday Design Review Notes". The overview notes that v2 shipping "is marked as completed", with next step "Meet Sam at 4".
  - **Bug: Edit page opened a blank page.** The canvas had the saved strokes (47) but drew none of them, and new strokes didn't show either, though they were saved.
    - Fix (`PageView`): the saved drawing goes to the canvas in `layoutSubviews`, once it has its size, not in `init`. A new page (no saved ink) already worked.
    - Confirmed in the next run: Edit shows the ink, a new line shows as it's drawn, Cancel drops it and Done keeps it (read again).
  - Each editor closes cleanly (`page view closed` in the log).
- **DEBUG-only additions:** `JOAT_UITEST=1` picture hook (`Notebook.listenForSnaps`). `debugSnapshot` now logs the canvas's frame, offset and stroke count.
- **Not tested here:** the Pencil itself, two-finger scrolling in Write (XCUITest has no two-finger drag), and the tool picker's tools.

## 2026-11-10 — JOAT Brain 1.0 (202611101256): a notebook in the Notes tab, pages in QuickNotes

JOAT Brain 1.0 (202611101256) uploaded to TestFlight. No plugin change.

- **Goal.** Add a PencilKit + typed-text notebook inside the Notes tab so Apple Pencil pages, QuickNotes, and Prep notes share one workflow:
  - The iPhone Duo takes the USB-C Apple Pencil from iOS 27.2: tilt, no pressure, no double-tap or squeeze, on both screens (MacRumors 2026-09-09; smartworld.it 2026-10-21).
  - His picks: write and draw with the Pencil, type and ink on one page, and handwriting to text.
  - Pages show on the glasses, sit in the same list as QuickNotes, and a page can be prep notes.
  - No handwriting font for now.
- **The page** (`Notebook.swift`): Notes tab › ✎. A PencilKit canvas sits over a text view on one page.
  - **Write:** the Pencil, or a finger unless iOS is set to draw with Apple Pencil only, draws. Two fingers scroll. Apple's tool picker has the pen, highlighter, eraser, lasso and colours.
  - **Type:** the keyboard, or Scribble with the Pencil, types on the same page under the ink.
- **Done:**
  - The ink is kept in `Documents/pages/<id>.drawing`. The page is a QuickNote (`VoiceNote` with `page`, `typed`, `handwriting`), so the glasses list and read it.
  - Claude (the note model) reads the handwriting. It gets PNG tiles cut between lines of writing, each about a screen tall, because Claude downscales an image past its long-edge limit (vision docs). It returns one line per sentence or item.
  - Apple's Vision docs do not say its text recognition reads handwriting, so the page goes to Claude.
  - Then come the summary and title, and the reminders offer, as after a voice note. A page's written to-dos count as the writer's own tasks (`pageCommitmentNote`; `keepBacked(page:)` needs no "I will").
- **The page's screen:**
  - the ink
  - **Edit page**
  - **Read my handwriting**, when it could not be read (offline, no key)
  - **Use as prep notes** (`Prep.addText(_:title:source:)`), which the AI cues then answer from
- **Simulator check** (`JOAT_DEBUG_PAGE=1`; the headless Simulator has no Pencil and takes no screenshots, so the app draws its own window to files):
  - A page with "Design review, Tuesday" typed and three lines of script ink. The editor showed both on one page, with the canvas first responder and the tools shown.
  - Claude read "Send Liam the slide deck on Friday at 3 pm" and "Book design review room" in 1.4 s.
  - Titled "Design Review Action Notes". Two reminders were offered: Fri Nov 13, 3:00 PM, and Wed Nov 11, 9:00 AM (no day written).
  - Two fixes found on the way. A page's to-dos were dropped for lacking "I will". A wrapped line lost its day, until the transcription joined wrapped sentences.
- **Not tested here:** the Pencil itself on the iPhone Duo.

## 2026-11-09 — JOAT 0.1.144 and JOAT Brain 1.0 (202611091814): Notifications first on the overview; texts that never reached the glasses

Plugin 0.1.144, lab package (loopback allowed, dirty tree on 47ae638), `dist/hub-0.1.144/joat-0.1.144.ehpk`, 177822 bytes, SHA-256 `0a3bc88acc9e6d8ddc981125a06d5a9d0d4dc407ad951bd5ceb8803993603b5d`. Not uploaded to Even Hub yet. JOAT Brain 1.0 (202611091814) uploaded to TestFlight.

- **Overview** (Notifications first on the rail, Reminders second):
  - The rail is NOTIFICATIONS, REMINDERS, NEWS, POMODORO, QUICKNOTES (`PANEL_TABS`).
  - Notifications lists the unread cards, newest first, with the app in the label column. A tap opens the newest as its card, and the next tap reads on. Home starts on it after a reconnect.
  - It leaves the overview's menu (id 7 removed), so it is in one place only. This reverses the 2026-10-15 change (61fb08e) except for Reminders, which stays on the rail.
  - Five tabs on Home:
    - The text bands are now info 3 lines and rail 5 lines.
    - The image column's rail rows are 22 px apart, starting 6 px down. The last underline ends at 115 of the 116 px tile.
    - The pixel overview has five slots.
  - Tests updated (1288 passed, typecheck clean).
  - In the Even Hub simulator with JOAT Brain's seeded texts, Home opened on NOTIFICATIONS listing Family and Jordan (the USPS scam kept off). A swipe went to REMINDERS, then NEWS. A tap opened the newest card and a second tap the next.
- **Messages: texts that never reached JOAT Brain**. Code review found two causes:
  - **A real text could be read as spam.**
    - `SpamCheck.parse` took any reply containing "spam" as SPAM, so "OK, not spam" hid the text.
    - Now the first word decides.
    - Saved contacts and group chats skip the check: a name from the automation is a saved contact. In the simulator, two contacts with the same name had made `resolve` fail and sent Jordan's text through the check.
  - **Show Text on Glasses returned before the work was done.**
    - The spam check ran after the action had already finished, and iOS can suspend the app once an action finishes. Apple gives a background action about 30 s (LongRunningIntent docs). The on-device model is also rate limited in the background on battery (Apple DTS, forums 793787).
    - The action now waits until the text is shown or kept off.
    - A check with no verdict after 10 s shows the text.
  - Simulator, seeded texts:
    - Jordan's text showed 76 ms after arriving (3.7 s before).
    - The group text showed at once.
    - The USPS scam was kept off in 1.7 s.
    - Reply and New message ("my partner") still reach the confirm and the hand-off to Shortcuts.

## 2026-11-07 — JOAT 0.1.143 and JOAT Brain 1.0 (202611071349): Messages that send, group chats, Shortcuts automations fixed, QuickNotes cues as cards

Plugin 0.1.143, lab package (loopback allowed, dirty tree on 47ae638), `dist/hub-0.1.143/joat-0.1.143.ehpk`, 177817 bytes, SHA-256 `f22e5bdc07ba0b2c9176d3fbfde57f5fbbdf0573a61786d3628648d352d829f1`. Not uploaded to Even Hub yet. JOAT Brain 1.0 (202611071349) uploaded to TestFlight (202611071343 was stopped mid-upload).

- **Why no text was ever sent** (traced in the two Shortcuts workflows):
  - The send automation waits for a notification titled "JOAT • text to send"; the installed app posted "JOAT · text to send". The app now posts "•".
  - Four actions in the send shortcut wait for a tap on the phone: Ask for Input, Show Alert, and both Send Message actions (Show When Run was on). The Contacts action had no contacts picked, so Apple's model never saw a list to match against.
  - The show shortcut: when Apple's model refuses a text, the whole automation stops and the text is lost (Shortcuts cannot catch an action's error). Group texts: Find Conversations by the sender's name finds the one-to-one chat, never the group, so every group text arrived as one person's.
- **JOAT Brain does what those steps were for** (`Recipients.swift`), and the glasses show the result before the tap:
  - Who: a spoken or typed number, an email, an exact name or nickname, else Apple's on-device model picks from the contacts, else Claude. Simulator: "my partner" → on-device pick → Jordan.
  - Spam: Apple's on-device model in its permissive mode, then Claude; a text nobody can judge is shown. Simulator: the USPS "package on hold" text was kept off the glasses.
  - A number with no name is looked up in Contacts.
  - Group chats: "Family (Jordan)" threads under Family with who wrote it. A reply in a group goes to the group: Text to Send has a new **Group** property, and the group's name is never looked up in Contacts (that could have texted one person).
- **On the glasses**, checked end to end in both simulators: Messages › conversation › tap → "What should I say to Jordan?" → "Send to Jordan?" → tap → handed to the Shortcuts notification; New message → "Who should I text?" → "my partner" → Jordan; a reply in Family → "Send to Family?" → queued as a group. The Simulator cannot grant notifications, so the last hop shows "Not sent: allow notifications"; the phone asks at launch.
- **Shortcuts workflows rebuilt** (`dist/shortcuts/gen_shortcuts.py`, signed with `shortcuts sign --mode anyone`):
  - Send: Get Text to Send → if Group has a value, Find Conversations (Group Name) and Send Message to it, otherwise Send Message to Recipient → Text Sent on Glasses → Find Conversations + Mark as Unread (Open When Run off). Show When Run off on both sends.
  - Show: runs without the Use Model spam check. Group texts: Find Conversations no longer filters on Display Name = Sender (that finds the one-to-one chat); it takes the conversation active last (sort Date, Latest First, limit 1: `WFContentItemSortProperty` `dateLastActive`), whose Group Name is set for a group, so a Family text reaches JOAT as "Family (Jordan)". Unnamed groups have no Group Name and still show by person.
  - Not testable here: Send Message to a group conversation (new in iOS 27).
- **Plugin**: the confirm card reads "What do you want to say to X?" (it confirms new messages too, not only replies).
- **QuickNotes, combining Even's Conversate and QuickNote**:
  - Only a hold records. A tap opens AI cues & prep.
  - A new cue pops up as a card for 6 s, then clears. A tap on the card opens that cue.
  - QUICKNOTES · AI CUES lists the prep notes and the cues, newest first; an entry opens to read and scroll; 2x tap goes back.
  - A swipe reads back through a long note; the page holds while new words arrive.
  - The note's menu has AI cues and Prep notes, next to Undo last input. The swipe between views (`hud.list.liveViews`) is gone.
- **Simulator note**: the Even Hub simulator tags audio "glasses" while streaming the phone mic, so `AudioStreamer.ingest` closes every spoken question at once. DEBUG builds let the seed answer anyway (`JOAT_DEBUG_ANSWERS`); the phone is not affected.

## 2026-10-30 — JOAT Brain review: fixes, reminders from notes in one tap, a real app with tabs, Messages answered

JOAT Brain 1.0 (202610292033) uploaded to TestFlight, then 202610292038, which adds Home's optional "Start JOAT Brain with the Even app" automation steps (App trigger › Even Realities › Is Opened › Start JOAT Brain). A full audit of all 36 Swift files and live API checks surfaced and fixed the issues below.

- **Errors fixed**
  - **"Hey JOAT" answers came back empty.** Claude Haiku 5.5 thinks before answering by default, and its thinking used up the 400-token budget. Tested against the API: "Who won the World Series?" came back empty, and the weather answer was cut off. Answers, note summaries, reminders-from-notes and prep points now ask for no thinking. Each model takes its own value, also tested: Haiku takes `"disabled"`, while Sonnet 5.5 refuses that and takes `"between_tools"`. A 400 that names thinking retries once with the model's default. Answers get 600 tokens and are read after the last search result, without the "I'll look that up" preamble.
  - **Speech recognition never came back after a failure.** A failed start (no model and no network at launch) or ended results left every voice feature dead for the life of the process. It now restarts after 5 s, doubling up to 60 s, and the backoff resets on the next sentence. Seen working in the Simulator, which has no dictation model: "Model not available" → "restarting in 20 s".
  - **The own-voice filter could stick.** A cut-off reply (`didCancel`) never cleared `speaking`, so every note sentence and follow-up after it was dropped as JOAT's own voice. That case now clears it, and a `speaking` flag older than a minute with nothing playing is cleared too. After a media-services reset, a new speech synthesizer is made.
  - **Saved data could be wiped.** A file that failed to decode loaded as empty, and the next save replaced it. The same pattern was in QuickNotes, prep and messages. Such a file is now kept aside (`*.unreadable-<time>.json`, or `<key>.unreadable` in UserDefaults) before anything is written. Notes saved before `keyPoints`, `todos`, `deleted` and `cues` existed now decode, because those fields are read as optional.
  - **Reminders access was checked only once.** It is now read from iOS on every use, the store is reset once access is granted, and the app asks when it opens if a background first start couldn't.
  - **Words lost at the edges of a note.** A sentence still running up to 1 s after a hold is released is now the note's (before: 0.3 s), with a 3 s grace (before: 2.5 s). Save waits out the grace, and words dictation hasn't finalised by then are kept. A second Save during the wait answers "Saving quicknote…".
  - **Smaller:**
    - "Text sent" now matches the newest text collected, and texts nobody sent expire.
    - A news category listed twice no longer crashes the news pass.
    - The log writes with the throwing file API (the old one crashed on a full disk).
    - A plugin connection that ends without a close frame is closed instead of spinning, and failed connections are cancelled.
    - The keep-alive shows "interrupted" during a call, and "Start JOAT Brain" (or opening the app) on a running brain re-checks the keep-alive and the listener.
    - A tap on the Reminders page opened by voice as "planning" is no longer dropped as stale.
  - **Not fixed:** the loopback server accepts any token, so another app on the phone could connect and answer a confirmation. That needs pairing on the phone without risking the link the wearer depends on. Next.
- **Reminders from notes, one tap** (the wearer's choice: on the glasses and in the app):
  - After a save, Claude reads the note for what the wearer said they will do and when, alongside the summary.
  - If QuickNotes is on the glasses, the list appears there ("Add 3 reminders from “Call with Sam”", each with its day and time); otherwise it appears the next time QuickNotes opens.
  - A tap adds them all, and 2x tap leaves. The second "Add N reminders?" confirmation is gone: the tap on the list is the confirmation (`Reminders.createBatch(confirmed:)`). The same applies to "create reminders from the last note" and the saved note's tap.
  - Checked end to end in both simulators with a seeded note recorded on Thursday evening:
    - "send the slide deck to Liam by Friday at 3" → Fri Oct 30, 3:00 PM
    - "call the bank tomorrow morning at 9" → Fri Oct 30, 9:00 AM
    - "next Wednesday … pick up the security review form" → Wed Nov 4 (all day)
    - One tap → three reminders created in Reminders, no second question.
- **JOAT Brain is an app now** (`AppScreens.swift`):
  - **Home:** glasses connected or not and the steps to connect, keeps-running status with Restart, speech status, whether a Claude key is present, permissions that still need an OK (Allow / Settings), and the settings.
  - **Notes:** saved notes with summaries. A note shows its overview, key points, next steps, AI cues and transcript. **Find reminders in this note** opens a sheet: untick any, edit the words, the day or time (or all day), then Add.
  - **Prep:** the prep notes, with the active one's talking points.
  - **Messages:** what's working, a guided two-step Shortcuts setup, and the texts received and sent.
  - **Diagnostics:** the old lab readouts and the log.
  - `-JOATTab <tab>` at launch opens a tab.
  - Built and launched in the Simulator, but no screenshot of it could be taken: this Mac has no Simulator app, so the headless simulator returns black frames.
- **iOS Messages platform constraints**:
  - The Mac reads Messages' database with Full Disk Access; iOS gives no app that.
  - Checked and ruled out:
    - Even Hub SDK: no notification or message API.
    - Even app: can't reply.
    - Bluetooth MAP (what cars use): needs a second Classic-Bluetooth device, and the G2 is BLE only.
    - ANCS: read-only.
    - AccessoryNotifications: EU-only, and its data can't come back to the iPhone.
    - Mac-free iMessage clients: jailbreak, plus a ban risk.
  - The Shortcuts route stays the way to read and send from your own number, now with in-app setup.
  - Without setup: Even's own notifications can show texts, and Siri with AirPods (Announce Notifications, Reply Without Confirmation) can read and reply.

## 2026-10-29 — JOAT 0.1.142 and JOAT Brain 1.0 (202610291831): prep notes and AI cues in QuickNotes

Plugin 0.1.142, lab package (loopback allowed, dirty tree on 47ae638), `apps/hub-plugin/joat.ehpk`, 177644 bytes, SHA-256 `5ffbdb21708b4baaecbbb29155e6008d157dfd677797d266135a5ef1a9dc7a12`. Not uploaded to Even Hub yet. JOAT Brain 1.0 (202610291831) uploaded to TestFlight.

Even's Conversate has Prep Notes and AI cues. The wearer asked for both in our QuickNotes and picked: all four cue kinds, all three ways to add prep, shown Even's way, with a swipe to switch views (tap stays record/pause).

- **Prep notes** (`Prep.swift`, `PrepExtract.swift`). JOAT Brain has a new first section, "Prep notes & AI cues", with **Add a file…** (PDF, text, Markdown, RTF, HTML), **Paste** (a link is fetched and its page text kept, anything else is kept as text), **Type notes…**, and an on/off switch for cues. A PDF or text file shared from any app (Share › JOAT Brain) arrives the same way (`CFBundleDocumentTypes`). Claude Sonnet turns each prep into a title and up to 8 talking points. The ticked prep (the newest by default) is the one the cues use. Kept in `Documents/prep.json`, capped at 60,000 characters.
- **AI cues** (`Cues.swift`). While a note records, Claude Haiku looks at what was just said, with the earlier talk, the prep and the cues already shown. It answers NONE or one cue: ANSWER (only from the prep), SUGGESTION, CONCEPT or BIO (public figures, or people the prep describes). It is asked when the talk pauses after 4 or more new words, or after 14 new words of steady talk, at most every 8 s, one request at a time. The prep is sent as a cached system block. Haiku 5.5 thinks first by default, and 160 tokens of thinking left 3 of 5 test replies empty, so cue requests turn thinking off (`ClaudeLink.cue`). Checked against the real API on a sample meeting: replies in 0.7–1.1 s, NONE on small talk, a SUGGESTION about canary latency, and "ANSWER | Rollout window | Staging soak finishes Thursday; production opens Friday…" from the prep.
- **On the glasses.** A new cue shows under the note's words for 10 s ("▶ ANSWER · Rollout window" and its text), then goes. A swipe moves through the note → AI CUES (newest at the bottom) → PREP · title (6 talking-point rows per page, "1/2", "2/2") → back to the note. Tap, hold and 2x tap work the same on every view. Each view fits one glasses page (7 lines), so a swipe always changes view and never pages. Saved notes keep their cues, and the note reader lists them under AI CUES.
- **Plugin**: `hud.list.liveViews` marks a note page that has views. On such a page a swipe sends `quicknotes-view-next` / `quicknotes-view-prev` to the brain (`CapabilityMenu.sendQuickNotesView`). A Mac brain never sends the flag, so its notes page as before. A brain whose hint already names the swipe gets no extra "swipe: page". Dev fixtures `quicknote-live-cue`, `quicknote-live-cues`, `quicknote-live-prep`.
- **Checked:** typecheck clean; 1287 tests pass (1 new). Both simulators end to end: JOAT Brain in the iOS Simulator (port 8797, a prep seeded) linked to the plugin in the official Even Hub simulator. Opened QuickNotes, created a note, swiped down four times and up once. JOAT Brain logged views 1, 2, 3, 0, then 3, and the glasses showed the note, "AI CUES · None yet", PREP 1/2 and PREP 2/2 with their hints. The cue popup is not seen on a simulator (no speech there), and the share sheet is not tested. Not tested on the glasses.

## 2026-10-29 — JOAT Brain 1.0 (202610291513) on TestFlight: stays up behind the Even app, the glasses can always reconnect

The wearer, on their own iPhone: it connected once, stopped as soon as they switched to the Even app or turned the screen off, never reconnected (Authenticated "no"), then the app crashed. No crash report reached App Store Connect, so the causes below come from reading the code. They are not confirmed on the device.

- **The keep-alive no longer opens the phone's microphone.** It held a record session with a tap on the phone mic, and JOAT never used that mic: the glasses' audio comes over the socket. Coming back to the app, an interruption ending, or a Bluetooth route change restarted that engine with the tap still in the old input format, a known way for iOS to throw an exception (the likely crash). A stopped engine also stayed stopped, so iOS suspended the app behind the Even app. Now the keep-alive is inaudible playback only (mixes with other audio, no mic indicator). It is rebuilt after an interruption, an audio configuration change or a media-services reset, checked on every route change and every 15 s, and checked when the app comes to the front.
- **The loopback listener rebuilds itself.** It used to log a failure and stay dead, so the plugin dialled a closed port forever. Now a failed listener is rebuilt with backoff (1, 2, 4 … 30 s). Every 10 s a listener that isn't listening is rebuilt. Coming to the front with no plugin connected rebuilds it anyway, because iOS takes listening sockets back from a suspended app. Open plugin connections stay up.
- **Authenticated no longer drops to "no" because of a half-open connection.** Before, a connection that died during the handshake took a connected plugin's count, and its "Authenticated", down with it.
- `JOAT_LOOPBACK_PORT` (Simulator only: the Simulator shares the Mac's 127.0.0.1, where the Mac's JOAT holds 8787).
- **Checked:** the device build installed on the test iPhone, though launch waited until the phone was unlocked. In the Simulator on port 8797: listening in front, after 40 s behind Settings, and back in front; the WebSocket handshake answered 101 each time, and coming to the front logged "listener rebuild (app opened)". Real suspension and the lock are not tested yet: the Simulator doesn't suspend apps the way a phone does.

## 2026-10-29 — Build JOAT 0.1.141: the phone's own brain is a choice again; JOAT Brain build 202610291313

Plugin 0.1.141, lab package, dirty tree on 47ae638, `apps/hub-plugin/joat.ehpk`, 177554 bytes, SHA-256 `f5f20c9adad866a35f5328e11d16079420440b7e5ddef43af1c475ef746cb9c9`. Not uploaded to Even Hub yet. JOAT Brain 1.0 (202610291313) uploaded to TestFlight.

- **0.1.140 could not reach JOAT Brain.** It was packed with `JOAT_RELEASE_ALLOW_LOOPBACK=1` but `JOAT_RELEASE_WS_URLS` came from `.env`, which names only the two Macs, so `ws://127.0.0.1:8787/glasses` was not in the package: the wearer's phone page offered only the MacBook Pro and the Mac mini, with JOAT Brain running on the same phone (2026-10-29). `pack.sh` now puts that loopback candidate first in every lab package (3 candidates in 0.1.141). 0.1.139 had it because its candidates were set by hand at pack time.
- **The phone page names it "This iPhone · JOAT Brain"** (it would have read "127.0.0.1 · Local network"), and its recovery line says "Open JOAT Brain on this iPhone, then reconnect." A phone that remembered a Mac keeps it until the wearer picks This iPhone and taps Connect.
- **JOAT Brain: Start no longer stops the keep-alive.** Start and Stop sat as plain buttons in one List row, and SwiftUI fires every plain button in a row on a tap, so Start logged "keep-alive on (record)" and "keep-alive off" in the same millisecond and Active stayed "no". Both, and Self-test, are borderless buttons now.
- Typecheck clean, 1286 tests pass.

## 2026-10-24 — Build JOAT 0.1.140: Reminders replaces Planning; the calendar is gone

Glasses plugin 0.1.140, lab package (`JOAT_RELEASE_ALLOW_LOOPBACK=1`, dirty tree on 47ae638), `apps/hub-plugin/joat.ehpk`, 177452 bytes, SHA-256 `359726887f33317d08935a5fd1c0208d2d581b955480d6c46bad26b8f0509b00`. Not uploaded to Even Hub yet. JOAT Brain Debug build with the phone half; built, not installed.

- **The Reminders page** (the wearer's pick of three layouts): the next reminder as an image card on the left, every open reminder as Even's own list on the right; a tap on a row asks to mark it done. The card (`ReminderCard.ts`, image container 21 `rem-card`, 288×144 under the header) shows NEXT, the time ("10:05 PM", "Tomorrow 9:00 AM", "Wed", all-day reminders by day), the title on up to two lines, the time left ("in 1 h 19 min", "in 25 min", "tomorrow", "in 3 days", "due now") and the counts ("1 late · 3 today · 1 tmrw · 3 more"); with nothing upcoming it says "Nothing due" or "Only late ones". The brain sends only facts (`hud.list.reminderCard`: the next not-late reminder's title, due time and all-day flag, and the late/today/tomorrow/later/undated counts); the words are made on the glasses, and the card is repainted with the header's minute tick, sent only when its pixels change. Under JOAT's box or a notification card the card is redrawn with nothing under the box, dimmed under a card, as Home's column is. JOAT Brain's `Reminders.listPage` now sends rows in the overview tab's labels ("[Late] …", "[3:00 PM] …", "[Tmrw] 9:00 AM …", "[No date] …"), overdue first, then the soonest, undated last, up to 20, with the card.
- **The calendar is deleted.** The Planning month image dashboard (grid left, hovered day right), its hover/pane state, the `capgrid` taps, `updatePlanningPane`, the month geometry, its renderer and its fixture are removed; every list is a firmware list. The Mac's Planning pages (week, day, entries, receipts, `planning-back`) still work: a Mac brain answers the menu's Reminders row with its PLANNING pages, which the plugin takes as the same visit, including a voice-opened one. Its month shows as a list.
- **Renamed.** The menu row Planning is Reminders; the overview's PLANNER tab is REMINDERS ("tap: open reminders"); productivity hints say "2x tap: Reminders"; the outcome page's header word is REMINDERS.
- **Fix found on the way:** list rows came out as "..." on every firmware list when `fitListRow` gained a width parameter (`map(fitListRow)` passed the row index as the width); fixed before packaging.
- **Checked:** typecheck clean; 1286 tests pass (calendar-only tests deleted, rename expectations updated, 15 new tests for the card, 2 for the page wiring). Official simulator: the Reminders page with a live card (the countdown moved with the clock) and full rows on the right; the overview rail reads REMINDERS. Not yet on the glasses.

## 2026-10-25 — JOAT Brain on TestFlight, build 1.0 (202610251627)

The phone half of 0.1.140 (the Reminders card, note reminders on the day and time said, the Messages actions) uploaded to App Store Connect for TestFlight while the test iPhone was away from the cable. Fix to `--testflight`: XcodeGen wrote a fixed CFBundleVersion "1" into the generated Info.plist, so the build-number override never reached the bundle and the second upload was refused as a duplicate; `project.yml` now takes CFBundleVersion and CFBundleShortVersionString from the build settings (MARKETING_VERSION 1.0, the version App Store Connect already holds), and the script now fails when the upload fails instead of reporting success.

## 2026-10-24 — iPhone-as-brain: reminders from notes keep the day and time said

JOAT Brain Debug build; built, not installed.

- **A note's reminder is due when the wearer said.** Before, a task found in a QuickNote took only a weekday word ("Wednesday") and always 09:00; "5 o'clock" was dropped and "next week" was right only by luck. Now the commitment request also asks Claude for WHEN (date and time, or date alone, or NONE), counted from the moment the note was recorded, which the instructions now carry. WHEN is taken only when the transcript sentence holding the quote names a day or a time (Claude may quote only part of the sentence). A day with no time is an all-day reminder (the wearer's choice). Nothing said keeps the old rule (09:00 on the note's day, or the next 09:00). The review page lists each date and time ("□ Drop permit forms at the city office — Wed Oct 28, 5:00 PM", "□ Call the bank — Mon Oct 26 (all day)"), and "Adjust times in Reminders." is gone from the receipt. Checked against Claude Sonnet 5.5 with a five-task transcript recorded on a Saturday and on a Monday: "Wednesday, which is next week at 5 o'clock" → Wed Oct 28, 5:00 PM both times; "tomorrow at 8 in the morning" → the next day 8:00 AM; "by Friday at 3" → that Friday 3:00 PM; "on Monday" → all day; "buy filters" → 09:00.

## 2026-10-23 — iPhone-as-brain: Navigation with Apple Maps

JOAT Brain Debug build. Plugin unchanged (0.1.139): it still tracks the route on the phone's location fixes and draws the turn pages itself; the phone now answers everything the Mac answered. Builds for iOS (generic); not yet installed on the test iPhone.

- **Navigation** (`Navigation.swift`, port of `services/navigation.py`, the Navigation half of `capabilities.py`, `make_navigation_session`). The same views, strings, ids and bindings as the Mac: place search, "Which destination?" with two matches and "None of these · retry", confirm, Walk/Cycle, "Getting your location" and the origin request, "Finding a walking route", the preview, "Starting navigation", the route, its acknowledgement, arrival, finish, reroute (the Rerouting page first, then the new route with no preview), every error page and its retry, a rejected view ending the session, `navigation.stop` at every end. The voice grammar is the Mac's ("navigate to …", "take me to …", "how do I get to …", confirm/first/second/walk/cycle/start/change mode/retry/finish, "no, I meant …", "nearest …" / "… near me" with a fresh fix through `location.refresh`), plus "navigate home" / "take me home", which the Mac only caught through its assistant tool. While a route is live, the cancel words work with or without the name, "open my notes" still opens, and nothing else reaches a command or the Mac. Saved places from the plugin's phone page (`places`) are kept on the phone; "my home" resolves to its coordinates with no search.
- **Apple Maps instead of Mapbox** (`NavigationMaps.swift`, the wearer's choice): `MKLocalSearch` with the Mac's ranking, `MKDirections` walking and cycling, the Mac's walking and cycling policies, no key. Measured on San Francisco, New York and Chicago routes: a step's instruction is the move at the end of that step's line, so each maneuver sits at the distance walked through its step; Apple gives words, not maneuver types, so the words pick the maneuver (English) and the line's own turn decides otherwise. Cycling routes come back labelled "automobile" while using paths and one-way streets against car traffic; the label is logged, the route used.
- **Changes from the Mac, on purpose.** The Navigation page puts "➤ Navigate to a place" first and lists saved places by name: the plugin draws NAVIGATION as a text page whose tap always arrives as row zero, so on the Mac a tap could only ever start the first saved place. "Say the destination again" takes the next sentence without the name for 15 s (the phone's rule for a question JOAT asked). Lens minutes are whole ("Starting navigation" showed "17.833333333333332 min"). A failed cycling route suggests walking, not cycling. "the Ferry Building" ranks the Ferry Building above "The Embarcadero & Ferry Building Stop", a place Apple returns twice shows once, and "nearest …" puts the closest first.
- **No street map behind the route.** Apple delivers a map snapshot only to an app on screen (`MKMapSnapshotter` docs), and JOAT Brain runs behind the Even app during a walk, so no `navigation.map` tiles are sent. The route line, the position and the turn pages are unaffected.
- **Checked on the Mac:** a harness drove the whole flow against live Apple Maps with a fake plugin answering every downlink (40 checks, every downlink within `protocol.py`'s limits); the walking and cycling routes it produced ran through the plugin's own `NavigationController` with a fix every 6 m: turn pages counted down, arrival fired, no off-route or wrong-way. Not yet checked on the glasses or in the Even Hub simulator.
- **On the glasses, first session** (installed just after midnight on 2026-10-23; JOAT Brain started at 08:04 local, the plugin connected 08:19). Planning opened twice. A QuickNote recorded for 23 minutes (30 sentences into the note, taps and holds all arrived) and saved. "Hey JOAT" (written "hey jokes") answered "Yes?". The wearer's speech arrived from the glasses tagged `self`, everything else `other`. Questions for the Mac ("JOAT, can you tell me my location") and the note's summary got "I can't reach the Mac right now": Tailscale was off on the test iPhone and JOAT was not running on the Mac. "Joe, can you tell me my location" was not taken as addressed ("Joe" is not a wake word). Navigation not tried yet; the phone page had no saved places.
- **Fix: two News feeds.** techradar.com/rss and tomshardware.com/feeds/all answer https with a 301 to plain http, which iOS refuses (App Transport Security, -1022), so both were missing from the phone's News. Redirects are now followed on https (`SecureRedirects` in `News.swift`); checked on the Mac: both feeds answer 200 over https. Built, not yet installed.
- **Fix: "where am I" on the phone.** "Can you tell me my location", "where am I", "what's my current location", "what city am I in" no longer go to the Mac: the phone asks the plugin for a fresh fix (`location.refresh`, 7 s), looks the address up with Apple (`MKReverseGeocodingRequest`; `CLGeocoder` is deprecated in iOS 26), and shows and says it ("You're at Millennium Park, 201 E Randolph St, Chicago."). A fix over 50 m rough says "near"; an older fix says how old.
- **Fix: the name as the phone hears it.** This morning Apple's dictation wrote "Hey JOAT" as "hey Joe" and "Joe", and those went unanswered. "hey joe", "hey jo", "joe" and "jo" now wake JOAT when they open the sentence (never mid-sentence: "I told Joe" is not addressed).
- **Fix: Navigation's "Where to?" answer.** "hey Joe take me to the nearest coffee shop" was searched as a 42-character place name; "Bijo take me to the nearest coffee shop" after "Say the destination again" found nothing. An answer now keeps the place after "take me to" / "navigate to" / "how do I get to" wherever it is in the sentence. Harness: 54 checks pass, including the sentences from this morning's log. Built, not yet installed.
- **Claude, direct from the phone** (`ClaudeLink.swift`, so summaries and questions work without requiring a reachable Mac). The Mac's notes model is a local Llama 3.2 3B on MLX; the iPhone 12 Pro Max cannot run Apple's on-device model (Apple Intelligence needs iPhone 15 Pro or later), and iOS refuses GPU work from a background app (Apple's "Preparing your Metal app to run in the background"), so the phone cannot run a model itself while the Even app is in front. Now: everyday questions go to Claude Haiku 5.5 with web search (max 3 searches, the wearer's city from Apple's reverse geocoding for context and search location), lens-sized with the Mac's `lens_text` rules; note summaries, titles, "ask the last note" and a note's tasks go to Claude Sonnet 5.5 with the Mac's prompts. The Mac is used only when the build carries no key. `build_ios.sh` copies only `ANTHROPIC_API_KEY` from the repo's `.env` into `Generated/claude.json` (ignored by git), so the key is inside this lab build. Checked on the Mac with the real key: "What's the weather like right now?" → one web search, current local conditions in 4 s; a follow-up kept its context; a sample note came back TITLE / OVERVIEW / KEY POINTS / NEXT STEPS in 2.8 s; a wrong key says "Claude refused this phone's key."
- **Notes summarized after the fact.** A note saved while no model answered is summarized and retitled once one does: 20 s after launch and every 10 minutes (three at a time; an unusable answer is not retried on the timer). This morning's 23-minute note gets its summary on the first pass after install.
- **Build script:** the app is built for any iPhone (`generic/platform=iOS`) and the previous build is deleted first, so a failed build can no longer install an older app (this morning the phone dropped off mid-run and the previous app was about to go on).
- **Installed on the test iPhone at 12:13 local.** Claude configured at launch; News answered 65 of 65 feeds (TechRadar and Tom's Hardware included); the plugin connected 16 s after launch; 28 s after launch both notes saved with no summary were summarized and retitled by Sonnet 5.5 (6.2 s and 9.7 s; 8 key points and 5 todos, 8 key points and 8 todos).
- **Why voice went dead after unplugging (12:13–12:18).** Two causes, both in the log. (1) The glasses came out of the case at 12:13:42; the plugin had stopped the mic at 12:13:28 when they reported "not worn, charging" (DeviceMonitor.ts: the mic stops on doff), and on "worn" it followed the phone page's "After an interruption: Ask first", which asks "Resume listening?" on the lens and waits 8 s for a nod or tap; unanswered, the mic stayed off until a new QuickNote reopened it at 12:17:42. (2) "Compare engines" was on in JOAT Brain, and with it the dictation engine that answers the wearer wrote nothing for five minutes while the comparison engine wrote "What, Mr.?" and "Um." on the same audio. Fixes: the comparison engine and its switch are removed; the phone now honours "Let Mac decide" the way the Mac does (glasses on, mic off → it asks for the mic; `MicResume` in `Features.swift`). Built, not yet installed.
- **No more answers out of the phone's speaker.** With no headphones connected, every reply ("Yes?") was spoken through the phone's own loudspeaker. Replies are now spoken only into AirPods or other headphones (Bluetooth A2DP/HFP/LE, wired, USB, car); otherwise the answer is on the lens only (`Voice.swift`). Installed 12:23 local with the two fixes above: one speech engine, `MicResume` registered, the plugin reporting "Resume automatically", the glasses mic open within a second of connecting.
- **Spoken replies are opt-in and off by default.** Every answer is on the lens; JOAT speaks only when spoken replies are on, and then only into headphones. On: "JOAT, speak your answers", "turn on spoken replies", "talk to me", "read the answers out loud", "voice on". Off: "JOAT, stop talking", "be quiet", "don't talk", "turn off the voice", "voice off". Also a switch in JOAT Brain, "Speak replies in headphones". Kept across launches. 13 grammar checks pass ("stop" alone and "talk about the weather" are not toggles).
- **Follow-ups without the name, and Claude knows where the wearer is.** At 12:30 "what's my location" got the phone's address, then the next question went to Claude, which "corrected" it ("I don't have verified location data") because it was told the location was unknown; and "can you verify and check my location", said right after, was ignored for lacking the name. Now: for a while after any answer the next sentence needs no name (a setting in JOAT Brain, "Follow-up without 'Hey JOAT'", 0–30 s, 0 is off, 8 by default; the Mac uses 30 s) (the Mac's `ConversationWindow`, filler words ignored, the conversation page's Exit closes it); Claude gets the phone's position as street, city and coordinates (one reverse-geocode per 150 m or 10 minutes), and coordinates alone when Apple has no address, never "unknown". Installed 12:33 local.
- **Planning removed; the menu opens just the reminders.** Reminders-only scope on iOS: `Planning.swift` is deleted. The menu's Planning entry (the plugin's label) now opens REMINDERS: every open reminder, soonest first, undated ones last, up to 20; a tap asks to complete it. "Open my reminders" says "Opening Reminders." Installed 12:47 local.
- **Texts on the glasses with the phone alone, through Shortcuts** (`Messages.swift`, `Intents.swift`; research in the project doc `iphone-messages-without-mac-research-2026-10-23`). No app may read or send Messages itself, so JOAT Brain now has four Shortcuts actions that run in the background with the phone locked: "Show Text on Glasses" (for a Message automation: the text becomes a Messages card), "Get Text to Send" and "Text Sent on Glasses" (for a Notification automation on JOAT Brain around Send Message with Show When Run off), and "Start JOAT Brain". The Mac's grammar: "reply …", a bare "reply" ("What should I say to Mom?"), "message Mom saying …" (also "text Mom saying …"). Within 60 s of a text, "reply …" needs no name. Nothing is sent before the glasses' confirm tap; a confirmed text the automation does not collect within 60 s is dropped, never sent late, and the lens says so. Names become numbers through Contacts. The menu's Messages entry lists conversations, newest first; a tap opens the thread and makes "reply …" answer it. Grammar checked on the Mac (12 sentences). Installed on the test iPhone 2026-10-24 about 08:13 local; the Shortcuts automations are not set up yet.
- **JOAT Brain starts without the screen.** Everything it runs now starts when the process launches, not when its screen appears, so any of the Shortcuts actions above starts the brain with the phone locked. iOS refuses to start recording in the background, so a brain launched that way keeps itself alive on inaudible mixed playback and returns to the record session when the app is next in front. Installed with the above; not yet tested launched from Shortcuts.
- **TestFlight, so the phone updates away from the Mac.** `build_ios.sh --testflight` archives a Release build (build number = upload time) and uploads it to App Store Connect app "JOAT Brain Lab" (com.joat.brain) with the App Store Connect API key; `JOAT_ASC_KEY_ID` and `JOAT_ASC_ISSUER_ID` are read from the ignored .env, only those lines. Added for the upload: an app icon (`Sources/Assets.xcassets`), `ITSAppUsesNonExemptEncryption` false, and iPhone-only at the target (XcodeGen's default "1,2" made App Store Connect refuse the portrait-only bundle as an iPad app). Build 202610240817 uploaded 2026-10-24.
- **Reminders page grouped by day** (the wearer's pick of three layouts): "REMINDERS · N OPEN", then OVERDUE, TODAY, TOMORROW, LATER headings with "□ 3:00 PM  Pick up cake" rows (day and time for overdue and later ones, "All day" / "No date" where there is no time), undated last, at most 20 rows with "+N more". A tap on a reminder asks to mark it done; a tap on a heading does nothing. Installed 13:12 local.

## 2026-10-22 — iPhone-as-brain: QuickNotes, News, notifications and the Mac link on the phone

JOAT Brain Debug build. Plugin unchanged (0.1.139): every page below speaks the wire the Mac already used. Builds for iOS (generic, unsigned); not yet installed on the test iPhone.

- **QuickNotes** (`QuickNotes.swift`, port of `voice_note_capture.py`, the QuickNotes half of `capabilities.py`, `quicknotes_panel.py`, `note_commands.py`). The list (15 a page, "+ Create new note", ▲/▼ paging rows, `quickNotesPage`, `pageControls`), the live page (newest 18 rows of the newest 12 paragraphs through `paginate_reader_text`, REC/Listening clocks, "No audio from the mic — check the glasses." after 3 s, a mic reopen request after 1.5 s), FINISH (Save/Discard/Cancel) and DISCARD?, the note and text readers sent whole, hold-to-talk with release and menu-cancel, "Undo last input", the voice grammar (start/pause/resume/stop/discard/confirm/keep, "take a note …", "find notes …", undo/restore), and the rail tab (four newest). While a note listens, finished sentences go to the note and never reach the wake gate (the Mac's exclusive capture lane); a sentence that ends within 0.3 s after a release still belongs to it. The phone keeps no raw audio: a saved note is its transcript. A note left open when JOAT Brain stopped is saved at the next launch. Mac-side harness run: tap create → hold → words → release → late words joined the held paragraph → tap resumes continuous → new paragraph → undo removed it → FINISH → Save → list shows "[now] ● Note HH:MM" → reader body matches the Mac's template.
- **Reader rows**: `ReaderText.swift` is an exact port of `paginate_reader_text`; 10 of 10 cases (CRLF, tabs, CJK, joined emoji, flags, long tokens) give the same rows as the Python.
- **News** (`News.swift`, `NewsFeed.swift`, port of `news.py` and the news grammar in `panel_commands.py`). Same catalog file (project.yml bundles `apps/mac/joat/services/news_sources.json`), the wearer's choice from the Mac app copied at build time (`Generated/news-config.json`, ignored by git), RSS 2.0 / RDF / Atom through XMLParser with a lenient fallback, the Mac's dedupe, subject shares, feed caps, health and benching, the 8-at-once / 2-per-publisher reads with the 20 s / 45 s deadlines, the readability gate and paywall check, `tidy_article` and `tidy_title` rules, the `[!]` status row, the article reader (no `readerPage`), and the voice commands. The page text comes from the phone's own extractor (largest `<article>`, else `<main>`/`<body>`; paragraphs, headings, quotes, list items of four words or more; nav/header/footer/aside/figure and share/byline/related/newsletter blocks dropped) instead of trafilatura, so the list can differ from the Mac's. Story pages skip Low Data Mode. Mac-side run with the wearer's 65 feeds: one pass in 5 s, 20 stories listed, readers clean.
- **Search** (`Search.swift`, port of `search.py` and its grammar): "look up …", "search for …", "open the second result", the tab (results then "⌕ Look something up", headline = the query), result readers sent whole (`SEARCH · RESULT`), long-press re-runs the query, the control row asks "What should I look up?" and takes the next sentence. Key-free like the Mac's ddgs: DuckDuckGo's HTML results page. Mac-side run: 8 results for "even realities g2 review", the fourth opened to the article's own text.
- **Notifications and the dashboard margin** (`Cards.swift`): JOAT's own cards (Pomodoro now) in Main with their id, the Tray's two previous rows, `hud.notifications`, the Notifications page (list, detail, "Clear JOAT notifications"), `notification-dismiss`, and `hud.ambient` with Open-Meteo weather from the phone's position. iOS gives an app no access to other apps' notifications or Messages, so only JOAT's own cards appear.
- **Capability opens by voice**: "open my notes", "show planning", … (`CapabilityOpenController`) under fresh `voice-` generations.
- **The Mac link** (`MacLink.swift`, the wearer's choice: ask the Mac when reachable). Requests no command answers go to the Mac's agent endpoint over the tailnet (`https://<mac>.<tailnet>.ts.net/agent`, already served by Tailscale Serve), with the conversation so far; the answer shows on the conversation page and is spoken. When the Mac is unreachable the wearer hears why. `build_ios.sh` writes `Generated/mac-link.json` (address from `tailscale status`, the endpoint's token from `~/Library/Application Support/JOAT/agent-token`), so the token is inside the lab build. The "You said: …" echo is gone.
- **Mac change: whole answers on request** (`agent_endpoint.py`): a request with `"joat_raw": true` gets the answer with its lines (up to 12,000 characters) instead of lens text; anything else is unchanged. Test added (`test_a_raw_request_gets_the_whole_answer_with_its_lines`); 18 passed, mypy clean on the file. Takes effect when the Mac engine restarts.
- **Note insights on the Mac**: after a save the phone asks the Mac for TITLE / OVERVIEW / KEY POINTS / NEXT STEPS (the Mac's prompt and parser), and a clock title ("Note 20:26") is replaced by the generated one. "Summarize the last note", "ask the last note …", and "create reminders from the last note" (or a tap on the note page) run the Mac's commitments prompt; the phone keeps only tasks the transcript backs word for word (`keep_backed_commitments`), dates them at 09:00 (`_due_date`), shows the review page, asks once on the glasses ("Add N reminders from “T”?"), writes each to Reminders with an alarm, reads each back, and remembers where each came from so a second ask finds it. Harness run: of four proposed tasks, "Sarah will buy paint" and "I think blue is nice" were dropped, the two the speaker committed to were kept with their days.

## 2026-10-22 — iPhone-as-brain: Spike 2 PASS, a question about which mic, and the first features ported to the phone

Lab test iPhone, JOAT 0.1.139 private build, JOAT Brain Debug builds.

- **Spike 2 (locked hour): PASS.** Log pulled at 69 min: JOAT Brain alive and in the background the whole run, plugin linked in one session, 827 heartbeats, longest gap between two 5.0 s, memory 21-22 MB, thermal nominal, no restart. The Private-build 5-minute lock caveat from Even's docs did not cut the link in this run.
- **Which mic is JOAT hearing? Open.** The wearer: wake words work near the phone and get no answer a few metres away with the glasses on. Every audio frame was tagged `source=glasses`, but the Even SDK's `normalizeAudioInputSource` maps any value other than "phone" (missing included) to "glasses", so the tag proves nothing. From 22:28:54 on, frames kept arriving at the usual rate while the recogniser produced nothing, not even partials, for more than 4 minutes. Every `device` report all session said `wearing:false`. Not yet verified: whether the Even app hands the plugin the phone's mic when it thinks the glasses are not worn. JOAT Brain itself only transcribes plugin audio; its keep-alive opens the phone mic and discards it.
- **Diagnostic: `AudioMeter`.** Every 2 s with speech-level audio (peak above -45 dBFS), and every 30 s otherwise, one log line: the source tags, Even's speaker tags (wearer/other exist only for glasses audio), audio received against real time, and level/peak in dBFS. The frame's `speaker` field is now decoded.
- **Reminders on the phone** (`Reminders.swift`, `ReminderTime.swift`, `Confirm.swift`, `RemindersStore.swift`): "remind me…", "set/add/create a reminder…", "complete/finish/check off the reminder…", "what are my reminders". Times: Apple's date detector, plus the cases it misses (relative "in 20 minutes", a bare "at 5", a day with no time → 09:00, a time already past → tomorrow); no time said → "When should I remind you to …?" and the next sentence answers it, no name needed. The write goes through the glasses' confirm gate (`confirm.request`/`confirm`, port of `hub_gateway.request_confirm`: 20 s window, payload/preview SHA-256, challenge and session ids), then the Mac's `productivity.view` pages and wording (verifying → saved "Native alarm set · JOAT monitoring armed." / completed / cancelled / not saved / failed / check), EventKit with an alarm at the due minute, and a read-back of title and due minute. Due times rounded down to the minute so read-back and monitor compare equal. Due monitor: `productivity.view` phase `due` with occurrence/attempt UUIDs and a connection generation, `reminder_due.ack` stops the retries, `reminder_due.complete` runs the confirmed completion. Recurring reminders refuse completion, as on the Mac. The Planner rail tab lists open reminders (7 days back, 6 ahead, 8 rows, `+N more`), polled every minute.
- **The capability menu answers on the phone** (`CapabilityMenu.swift`). Every `capmenu` open gets a `hud.list` under its request id, so no row waits 12 s for "No response from the Mac yet." Planning is ported (`Planning.swift`: month grid with 42 cells and the hovered day's entries, week and day pages, the Mac's rows and labels, Back through `planning-back`, the right-pane tap with re-resolution in fresh data, an outcome's anchor reopening its day). Reminders only: events are left out at the wearer's request, so event counts are 0. A reminder row asks to complete it. News, Pomodoro and Search open their rail tabs. Pages not ported yet say "<Name> is not on the phone yet."
- **Echo guard.** While JOAT is talking, or within 0.3 s after, a sentence that ends is not taken as the answer to JOAT's question or as the request after "JOAT" alone (speaker out loud, glasses mic close by).
- Installed on the test iPhone at 17:41 local, after the hour had passed. The plugin reconnected within a second; the first `AudioMeter` lines showed audio arriving at 1.0× real time and Even tagging some of it `self`. Not yet verified on the glasses: the reminder flows, Planning, the far/near mic test.

## 2026-10-22 — iPhone-as-brain: the voice loop on the phone, and the name in spoken wake phrases

Lab test iPhone (iPhone 12 Pro Max, iOS 27.0.1), JOAT 0.1.139 private build, JOAT Brain Debug builds. No Mac in the loop.

- **Voice loop works end to end on the phone.** JOAT Brain asks for the glasses mic after auth (`audio.control`, as the Mac does), feeds the plugin's 16 kHz frames to Apple's on-device speech engine, gates on the wake words, answers on the glasses' native conversation page (`assistant.conversation`) and speaks through AirPods (`AVSpeechSynthesizer` on the keep-alive session, now with Bluetooth A2DP output). The lab reply repeats the request back ("You said: ..."); no model yet. Name alone → "Yes?" and the next sentence is the request, as on the Mac.
- **Connect greeting.** The plugin's first frame is its image overview ("JOAT · connecting"); the native overview arrives on the first repaint a brain causes. The Brain now sends what `VoiceOrchestrator.on_connect` sends (clear Main/Context/Tray, status word), and the wearer confirmed it opens on the native overview. The Spike 1 receipt overlay is gone.
- **The name.** Apple's newer engine (SpeechTranscriber) writes "JOAT" in the wearer's voice as Jote, Jolt, Joke, Jokes or Joe; the Mac's rule accepts the English ones only after "hey", so the name said alone was missed. Apple's dictation engine with custom words only dropped the name. With a custom pronunciation model (`SFCustomLanguageModelData`, X-SAMPA "dZ o t" and "dZ O t" — Apple's en-US set rejected the three gliding-o variants — plus weighted "Hey JOAT"/"JOAT ..." phrases) dictation writes "JOAT". Both engines run on the same audio for comparison. Over two runs of the same four phrases: dictation recognised the name 5 of 8 times (the second run 3 of 3, including name alone → "Yes?" → "what time is it"), the newer engine 3 of 8; neither woke on "Let's grab lunch at noon". Dictation finalises 42-93 ms after the audio, the newer engine 469-1688 ms. Remaining misses: dictation still writes "Joe" sometimes.
- **Tuning pass.** Apple's en-US inventory (79 symbols, now logged at startup) has no separate "oU" glide; its "o" sits where the GOAT vowel belongs, so "dZ o t" most likely already was the glide (inferred from the inventory; Apple does not document the mapping). The spellings now add primary stress (`dZ "o t`, `dZ "O t`), the phrase weights for "JOAT", "Hey JOAT" and "JOAT remind me to" are about three times higher, and punctuation carried over from the previous sentence is stripped before anything is shown. Next run: dictation recognised the name 4 of 4 ("Hey JOAT, what is my current location", "JOAT, remind me to call Mom", "JOAT" → "Yes?" → "what time is it", and "Hey JOAT" alone), the newer engine 1 of 4 ("Hey, Joe", "Joats", "Joke"); no wake on "Let's grab lunch at noon" or on room sounds the newer engine wrote as "Oh, man", "Mm hmm", "Uh". Dictation finalised 45-251 ms after the audio, the newer engine 76-1293 ms.
- **Dictation configuration.** Plain dictation held every sentence open and finalised none; it now uses Apple's `progressiveShortDictation` preset (short-form hint, volatile results, frequent finalisation, punctuation) plus audio time ranges.
- `WakeGate.swift` is a spike-time copy of the exact-match part of `orchestrator.py`'s wake gate; the fuzzy fallback is not ported. It must become one shared list before the phone brain stops being a lab experiment.

## 2026-10-22 — iPhone-as-brain Spike 1: PASS (plugin → JOAT Brain on the same phone)

Lab test iPhone (iPhone 12 Pro Max, iOS 26.6.1), JOAT 0.1.139 private build, JOAT Brain Debug build. Project doc: `iphone-brain-spike1-loopback-result-2026-10-22`.

- The plugin dialled `ws://127.0.0.1:8787/glasses`, authenticated, and sent its opening uplinks (`hello`, `policy`, `places`, `device`, `location`). 100 heartbeats, round trip median 2 ms, p90 3 ms, max 6 ms, with JOAT Brain in the background on the `audio` keep-alive and the Even app in front.
- The plugin page's origin is `http://127.0.0.1:<port>` (a new port per WebView load): the Even app serves plugins over plain HTTP on loopback, so a loopback `ws://` is not mixed content, and Even's whitelist accepted the loopback origin. No Local Network prompt.
- JOAT Brain changes from this run: the self-test dials through Network.framework (the app's own `URLSession` to `ws://127.0.0.1` is refused by App Transport Security on iOS 17+; the Even app's WebView is not affected) and is answered with `selftest.ok` without touching auth state; every log line is mirrored to stderr so `xcrun devicectl device process launch --console` streams it to the Mac (the device system log needs root); the keep-alive starts with the server at launch, since a suspended app's listener stops accepting.

## 2026-10-22 — Build JOAT 0.1.139 (lab: iPhone-as-brain, Spike 1)

- Packed from a DIRTY tree based on `47ae638` with `scripts/pack.sh`, the builder's `.env` package id, and a lab candidate list: `ws://127.0.0.1:8787/glasses` first (JOAT Brain on the phone), then the builder's two Macs. Needs the new `JOAT_RELEASE_ALLOW_LOOPBACK=1`; without it the pack refuses the loopback origin as before. Version guard: 0.1.139 above 0.1.138; app.json, package.json and package-lock.json all 0.1.139. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Typecheck clean; release-script tests 12 passed.
- `apps/hub-plugin/joat.ehpk`: 179065 bytes, SHA-256 `00afd5c68060f413f322babbb51ce360af9a86c77bd717197799c12b7aadf173`. Plugin code unchanged from 0.1.138: only the allowlist and the candidate order differ, so the same build still reaches the Macs when no brain is on the phone (MacLink rotates). Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-22 — iPhone-as-brain lab: JOAT Brain app and the loopback pack option

The hypothesis (project doc `iphone-as-brain-feasibility-2026-10-21`): the wearer's spare iPhone 17 runs the whole brain, the glasses are the interface, the Mac is out of the loop on the go. Spike 1 is the go/no-go: can the Hub plugin reach a WebSocket server on its own phone?

- **`apps/ios-companion/` — JOAT Brain** (SwiftUI, XcodeGen `project.yml`, generated `.xcodeproj` ignored like the Mac shell's). A WebSocket server bound to `127.0.0.1:8787` only (`NWListener`, `requiredLocalEndpoint`; nothing on Wi-Fi can reach it) speaking the first steps of the wire protocol: `auth` → `auth.accepted`, `link.ping`/`link.pong` every 5 s with the round trip measured, one `assistant.overlay` receipt on the glasses after auth, every other uplink counted by kind. The handshake's `Origin` header is logged and shown: that is the plugin page's own origin, which decides whether WebKit treats a loopback `ws://` as mixed content. A keep-alive switch holds an `AVAudioSession` record session with a running engine (`audio` background mode, Spike 2), a self-test dials the server from the app itself, and everything goes to the unified log under subsystem `com.joatlabs.joatbrain`. `scripts/build_ios.sh [--install] [--launch]` regenerates, builds a Debug app for the paired phone and installs it with `devicectl`. Compiles for iOS (unsigned build); not yet signed or installed — Xcode has no Apple account on this Mac.
- **Pack option.** `scripts/release_candidates.mjs` takes `{ allowLoopback }`: a plain-ws candidate is accepted only when its host is `127.0.0.1` or `localhost` and the flag is set; any other plain-ws host, and any path but `/glasses`, are refused as before. `release_manifest.mjs` passes the option through; `pack.sh` reads `JOAT_RELEASE_ALLOW_LOOPBACK` from the environment or `.env` and prints a warning when it is on. `.env.example` documents it. Tests: two new (candidates with and without the flag, LAN host still refused with it, wrong path refused; the manifest lists loopback first then the Macs).
## 2026-10-18 — Build JOAT 0.1.138

- Packed from `49b40bb` (clean tree) with `scripts/pack.sh` and the builder's `.env` package id and Mac candidates. Version guard: 0.1.138 above the last recorded build (0.1.137); app.json, package.json and package-lock.json all 0.1.138. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Typecheck clean; the code is `4405d7c` (plugin 1281 tests passed), and `49b40bb` changes only the version numbers.
- `apps/hub-plugin/joat.ehpk`: 179048 bytes, SHA-256 `b83f6e6c263bc42a70686e8d391a2208c5a17faaae34dc52d56c4ea9672bc930`. Adds to 0.1.137 the Planning month's shared footer and no "Planning" title beside the month. Plugin only: the Mac needs no restart for it. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-18 — Planning month: the shared footer, and no title beside the month

Aligns the Planning month page with the reusable footer used across all other pages and removes the redundant "Planning" label from the month/year header row.

- **Footer.** The Planning month is the one page drawn as a picture, and it drew a footer of its own: small, grey and centred in the canvas face. It now draws the line every firmware page carries: the firmware font at full size, 4 px in from the left, in the bottom band, at firmware brightness 1 (image gray 68), from the glyph atlas (`FOOTER_HINT`, `footerHintRun`, `HudCanvasRenderer.drawFooterHint`). Both month layouts use it, the split dashboard and the grid-only month. `evenTextWidth`, `fitOneLineEven` and `drawEvenText` take an optional `wordSpace` (default unchanged): the extra gap at spaces is for the smaller scales, and at full size it made the line wider than the firmware's.
- **Title.** The bold "Planning" at the left of the month row is gone from both layouts; the header already says PLANNING · MONTH. The month and year label stays where it was.
- **Verified in the simulator** (`scripts/simulator_planning_native_probe.py`, fixture data): the month page's footer lights the same number of pixels (762), on the same rows (263 to 283) and at the same alpha (179) as the week page's native footer, from x = 4. The line ends at x = 341 where the firmware's ends at 339. Not measured on glasses.
- **Tests.** Four new: the footer run (position, level, width against the firmware's), a too-long footer cut with an ellipsis, no "Planning" title, and the footer drawn bottom-left rather than centred (the last two fail with the old drawing). Plugin 1281 passed, typecheck clean. Plugin only; shipped in build 0.1.138.

## 2026-10-18 — Build JOAT 0.1.137

- Packed from `c058f6d` (clean under `apps/hub-plugin`) with `scripts/pack.sh` and the builder's `.env` package id and Mac candidates. Version guard: 0.1.137 above the last recorded build (0.1.136); app.json, package.json and package-lock.json all 0.1.137. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1277 tests passed, typecheck clean.
- `apps/hub-plugin/joat.ehpk`: 178890 bytes, SHA-256 `971840f5f7175b97644ac8c6ef8972dc2516cf206011f85efd3c90525c4b567a`. Adds to 0.1.136 the spoken reminders list: the glasses app now takes the `QUICKNOTES · DETAIL` list the Mac opens for "create reminders from the last note". It needs the Mac at the same revision (restart JOAT). Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-18 — QuickNote reminders: only what you said, read first, one confirm

Previously, reminders generated from a note came from model-invented summary "next steps" and prompted one confirmation per item. Reminders now come strictly from explicit commitments in the transcript, presented together on one review list with a single batch confirmation.

- **Where reminders come from.** Not the summary's NEXT STEPS. A second, narrow pass over the transcript (`services/commitments.py`): the local model proposes `TASK` + `QUOTE` blocks, and plain code keeps a task only if the quote is in the transcript word for word, carries a commitment cue ("I need to", "I'll", "I have to", "remind me", "don't forget" ...), and the task's own words are mostly in that quote. One task per sentence, near-duplicates merged, at most eight. On the 14 saved notes with a transcript on this Mac (counts only, a copy of the live database) the model proposed 60 tasks and 7 were kept, most of the rest for lacking a cue sentence; a pass takes 0.3 to 9 s. The filter is strict by design, so a note where nothing was promised says so. The model runs once per note per transcript and is remembered until the engine restarts.
- **Read first, then one confirm.** A note's "tap: reminders" now opens a scrolling list of every reminder in full with its day (`□ Email the release notes — Oct 19 at 09:00`, `QUICKNOTES · DETAIL`). "tap: add N" goes on to one native confirmation, "Add N reminders from “<note>”?" (the note's title is cut to fit 120 units; the reminders are not). Leaving the page, or no tap in 90 s, adds nothing. No summary is needed first.
- **One confirm, still the journal's gate.** A confirmation is bound to one action and one payload, so the batch is an action of its own (`reminders.create_batch`) whose payload lists the SHA-256 of each reminder's payload; the one confirmation approves exactly that list. Each reminder is still its own journal row (`voice_note.commitment`, one per spoken sentence) with its own read-back and receipt; it can be approved only through that batch while the batch is dispatching, once per listed copy (`ActionJournal.approve_batch_child`, event `batch_approved` recording the batch). The batch is succeeded only if every reminder was written; a reminder that may exist makes it uncertain. No schema change and no migration. A restart invalidates any reminder not yet written.
- **When they fire.** 09:00 in the default Reminders list, on the day you named ("tomorrow", or a weekday after the note's day), else the note's day; a 09:00 already past moves to the next one ahead.
- **Taps and retries.** Declining writes nothing and the note can be asked again. A sentence already made is counted "already in Reminders" and never written twice; one that failed before writing is asked again; one that may exist is not retried. Receipts: `Added N reminders from “T”.`, `Added X of N reminders ...` with the first problem, `“T” has no reminders: I add only what you said you will do.`, `The local model did not answer. Try again in a moment.`
- **Spoken command.** "create reminders from the last note" runs the same flow as the tap: the list, then the one confirmation. With a note's page open the list answers that page; said from anywhere else the Mac puts the list up as a request of its own (a fresh `voice-` request id). The glasses app took a QuickNotes note or detail page only for the visit a row tap had opened, so a list for a spoken request would have waited 90 s in vain; that needed a plugin change (0.1.137). `presentList` now takes a `QUICKNOTES · DETAIL` page that arrives with a new `voice-` request id, once. A repeat frame of the same id, any `QUICKNOTES · NOTE` page and any non-voice id are still dropped.
- **Found by the new tests.** The first version of the list page sent only its heading and an empty list (the page's text was not carried to the scroll box). Fixed in `_review_draft`.
- **Tests.** The old per-to-do flow's tests are replaced. New: the journal batch approval (listed digest only, once per copy, ready-only, restart), the batch writer (one confirmation for N, declined, already made, partial failure, may-exist, no bridge), the extraction filters (32 cases), the note flow (provenance, dates, caching, every receipt), the list page and the note-page tap, and for the spoken request: the Mac opens the list as a request of its own unless a note page is open (3 tests), the spoken command runs the same flow and adds nothing unless the list is confirmed (2), and the glasses app takes that list once and still drops the repeat frames, the `NOTE` page and a non-voice id (4; three fail against the old gate, the fourth guards the drop that stays). Plugin 1277 tests passed, typecheck clean. Python full suite 3846 passed, 1 failed: `tests/integration/test_whisper_cold_start.py::test_shutdown_after_foreground_timeout_reaps_child_and_stops_retries`, a timing test of the whisper server that fails the same way on `b596d96`, the commit before this work, on this Mac as it is now (it passed in the earlier run). mypy's one existing error in `reply_engine.py`.
- **Verified on real notes.** The production controllers with the real local model on a copy of this Mac's 14 saved notes that have a transcript, a throwaway journal and a bridge that echoes each reminder back: 5 notes offered reminders (7 in all) and each got exactly one confirmation (longest prompt 71 of 120 units); the other 9 said they have no reminders; a second pass over every note wrote nothing new; the journal ended with 7 reminders and 5 batches all succeeded and 7 `batch_approved` events.
- **Verified in the simulator.** The plugin dev server in the official simulator against the real gateway and router with fixture data (`scripts/simulator_spoken_reminders_probe.py`): with the Notifications page up, the spoken list opened as a new voice request and painted in 1.5 s with all four reminders and "tap: add 4"; a tap reached the Mac as the answer; Back left the list and the Mac saw the exit. Nothing was written.
- **Not verified.** The real EventKit write and the list and confirmation on physical glasses: the Mac app's bridge accepts only the engine it launched. Needs a JOAT restart on the Mac and the 0.1.137 glasses build.

## 2026-10-17 — QuickNote reminders: fit the confirm screen, allow a retry, say why

Fixes silent failures when creating reminders from a saved QuickNote.

- **What the logs showed.** Nothing: no error, no traceback, no reminder line since the engine started, and the action journal has no `reminders.create` row at all. So no attempt had reached the confirmation. The tap's receipt said only "Created 0 of N reminders".
- **Why.** The reminder confirmation is the exact prompt `Create reminder + JOAT alert “<title>”? <list> · <date>, <year> at <time> · <zone>`, and the native confirmation page holds 120 UTF-16 units. The fixed words use 84, which leaves 36 for the to-do. A to-do is a model's sentence (the 39 on this Mac: median 64, longest 117), so 37 of 39 were refused with "too long for the confirmation screen" before the journal was touched, and `create_reminders_for` kept only the count.
- **A second fault behind it.** A declined or timed-out reminder could never be asked again: its payload is purged on cancel, the origin's idempotency key then conflicts, `ActionConflictError` escaped and the tap answered "try again". A reminder already created threw the same way on a second tap.
- **Fix.** `create_reminder(fit_title=True)`, for the note flow only: the short confirmation `Remind + alert “<title>”? Oct 18 at 09:00` with the title cut at a word to fit (the reminder is written with the cut title, so the screen shows exactly what is written; the full text stays in the note). The spoken "remind me to ..." keeps the strict exact prompt. A retry after an attempt that wrote nothing takes a numbered origin (`<capture>~N`) and the earlier rows stay as the audit trail; a created one answers "already in Reminders" and is never written twice; one that may exist stays "check Reminders"; one still waiting says so (`ActionJournal.state_for_origin`). The receipt names the first reason a to-do was not created, and each one is logged (`note reminder 2 of 4 for <capture> not created: ...`).
- **Verified.** 39 of 39 real to-dos reach the confirmation through the production controller (5 shortened, widest prompt 120 of 120) against a throwaway journal and a bridge that echoes the reminder back. Tests: the 39 real lengths, emoji at the cut, retry after one and several declines and the next day, no double write, may-exist and failed-before-write, and the note-level receipts. Full suite 3758 passed, mypy's one existing error in `reply_engine.py`.
- **Not verified.** The real EventKit write and the confirmation on the glasses: the Mac app's bridge accepts only the engine it launched, so a probe from outside is refused. Mac engine only, no plugin change; it needs a JOAT restart.

## 2026-10-17 — Build JOAT 0.1.136

- Packed from `d34eb72` (clean under `apps/hub-plugin`) with `scripts/pack.sh` and the builder's `.env` package id and Mac candidates. Version guard: 0.1.136 above the last recorded build; app.json, package.json and package-lock.json all 0.1.136. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1273 tests passed, typecheck clean.
- `apps/hub-plugin/joat.ehpk`: 178965 bytes, SHA-256 `deb6993f6b5deb6252c256a02a144e44836aefb4f617107ae4aa82cf66765854`. Adds to 0.1.135 the Pomodoro sessions count, the `timer` phase and `blocksPerSet` on the wire. It needs the Mac at the same revision (restart JOAT; migration 17 runs on start). Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-17 — Pomodoro: set the sessions and the lengths by voice, and a plain countdown

Adds configurable focus/break durations, session counts per set, and one-shot countdown timers to Pomodoro.

- **What was missing.** The only way to change a length was to tap an idle row and step through fixed presets (focus 15-60, short break 3-15, long break 10-30); the four-block set was hard-coded in the Mac service, the persistence validator and the plugin's labels; there was no countdown that runs once; and the voice commands covered only start, pause, resume, skip, reset, open and status.
- **Sessions per set are a setting.** 1 to 12, stored with the timer (`pomodoro_state.blocks_per_set`, default 4) and sent on the wire as `blocksPerSet`; the glasses' "block 2 of 6" and dots follow it. Say "set sessions to six", "do 3 sessions" or "five sessions", or tap the new `Sessions: N` row (2, 3, 4, 5, 6, 8). A set is a whole number of blocks and then loops, as before; "reset" ends it. Changing the count while a block is under way is accepted when that block still fits the new set, and refused in words when it would not ("You are 2 blocks into this set. Say reset, then set 2 sessions.").
- **Any length, by voice.** "Set focus to 40 minutes", "short break 10", "long break time twenty minutes", "set work time to fifty", "change focus time to half an hour": any whole number of minutes from 1 to 1440 (`set_length`). The tap rows still step the presets. A block under way keeps the length it started with; the new one applies from the next of its kind (as before).
- **A plain countdown.** "Set a timer for 12 minutes", "start a 10 minute timer", "time me for forty-five minutes", "set a timer for an hour", or the idle page's `▶ 10 min timer` row. It runs once, shows `Timer · N min left` on the page and `Timer Nm` on the chip, can be paused, resumed or cancelled, and when it ends goes idle and drops a `Timer done` card. It is a new `timer` phase of the same service, so there is one timer at a time (it will not start over a pomodoro, and says so); "start a timer" with no length still starts the pomodoro, as the existing test pins.
- **The model can set it too.** A `pomodoro_set` tool (focus, short break, long break, sessions, timer) calls the same setters with the same bounds, so "make my focus blocks forty minutes long" or "I want six rounds today" works when it misses the exact verbs.
- **Storage.** Migration 17 drops `pomodoro_checkpoint` and creates `pomodoro_state` (migration 11's table pinned the phase names and a set of four in CHECK constraints and is checksummed history; the manifest accepts only CREATE and DROP). A timer in flight at upgrade comes back idle; the saved lengths are kept (`pomodoro_lengths`, migration 13), so a restart does not lose what you set. The Mac needs the matching revision, so restart JOAT; the plugin tolerates a Mac that does not send `blocksPerSet` (4).
- **Found in the simulator, not fixed.** The firmware list has no cursor-setting API, so every repaint puts the cursor back on the first row. After a tap on `Focus: 25 min` the next tap needs two scrolls again, and `Sessions` and the timer row sit below the fold (rows 5 and 6; five show at once). That is why exact values are voice: a preset step costs a scroll and a tap each. The row-tap mapping past the plugin's countdown row is correct (a tap on Focus reaches the Mac as row 1).
- Verified in the official simulator through the real gateway, plugin and the real `PomodoroService` answering taps (`scripts/simulator_panels_native_probe.py`, extended): Focus 25→30, Sessions 4→5, and the 10 min timer starting, with the page reading `Timer · 10 min left` / `Pause` / `Cancel timer`. Not verified on physical glasses.
- Tests: Python 3722 passed, mypy one error in `reply_engine.py` (untouched), plugin 1273 passed, `tsc` clean. New: sessions (preset ladder, bounds, refusal mid-set, long break after N), `set_length`, the countdown (runs once, pause, cancel, one at a time), the voice phrases and the phrases that must fall through ("set a timer for 90 seconds", "set my alarm for 7 am"), the wire (`timer` phase, `blocksPerSet`), the store constraints, migration 17 and the plugin labels.

## 2026-10-16 — Build JOAT 0.1.135

- Packed from `ba6aab2` (clean tree) with `scripts/pack.sh` and the builder's `.env` package id and Mac candidates. Version guard: 0.1.135 above the last recorded build; app.json, package.json and package-lock.json all 0.1.135.
- `apps/hub-plugin/joat.ehpk`: 178854 bytes, SHA-256 `668e65752cefbbcb57aa23a6257e661f1388fd3810d643c67a3e06f11f0a7ce1`. Adds to 0.1.134 the wearer-led Brainstorm: one round per move, whole answers, the `BRAINSTORM · ROOM` reader (`ba6aab2`). The Mac engine needs the same revision (restart); the plugin alone shows the reader only when the engine sends it.

## 2026-10-16 — Brainstorm: the wearer leads, and every answer is shown in full

Reworks Brainstorm as an interactive thinking partner that breaks down questions and plans in full detail without truncating turns to one-line gists or auto-advancing rounds without user input. The 0.1.133 redesign was reverted (`81fe353`); this applies the targeted fix on top of the original flow.

- **Why answers were one line.** Four things stacked: the prompt asked each seat for 120 words opening with a one-line `GIST:`; the glasses collapsed every finished turn to that gist; the wire carried about 4,200 characters, enough for two collapsed turns and cutting one full answer; and a failed source check sent the answer back to its seat to be rewritten, shorter. All four are gone. A seat is asked for its answer in full and in detail: a question broken down (what it depends on, the options, trade-offs, an example), or, when asked for a plan, numbered steps with their order, what each needs and where it can go wrong. No word cap; `BRAINSTORM_TRANSCRIPT_MAX` is 64 KB.
- **The wearer leads.** A round is every seat going once (round 1 blind, as before), then the room stops and waits. No judge, no agreement score, no round limit, no synthesis, no idle timer starts anything: the next round begins when the wearer speaks. They need no wake word, because the room asked for them; noise words a transcriber writes for silence ("you", "thanks", "um") are not a move. The cancel words and Back remain the way out. A nomination or a stage direction ("ask Codex if that's true", "tear that apart") is still a move.
- **Reading the room.** When a round ends the glasses open `BRAINSTORM · ROOM`: the whole conversation, every turn in order and none collapsed, in the same scroll text box a News article uses (a line a swipe, the firmware scrollbar, `swipe: scroll · speak: your move · 2x tap: back`). It reopens on the wearer's move and the new answers, not where they stopped reading the round before (`ScrollBox.resume(lines, opensOnNew)`).
- **The source check no longer repairs.** It still looks up each cited source and shows OK or FAIL beside the answer (`VERIFY`), but a failed source is never sent back and nothing is rewritten or shortened. RULES.md #13 (DENY plus a structured repair request) and #14 (judge threshold) describe the debate loop and are not changed here; Brainstorm no longer applies them.
- **A seat has ten minutes, not four.** A detailed answer with its facts checked and a second opinion taken ran past the 240 s default in the first real run, and the whole answer was thrown away as "could not answer". The two seats now have `SEAT_TURN_TIMEOUT_S` (600 s), and a seat that does run out of time says "ran out of time". Text a Claude seat writes around a tool call ("I'll check first", the answer) no longer runs together ("first.Here is").
- **Verified end to end with real seats, not fixtures.** Two real Claude seats stand in for Codex, whose account is at its usage limit until Oct 23 (a real Codex seat shows "usage limit", and the room ends after round 1 with "insufficient collaborators"; that is the existing design). In the official simulator, through the real gateway and plugin: round 1 answers came out at 1,164 and 1,029 words, the room stopped and waited (45 s idle, no new calls), a spoken move with no wake word started round 2, and both seats answered in full (1,311 and 1,031 words, the second reading the first and withdrawing a point). The room reopened on the wearer's line after the round had been skimmed, 392 swipes reached the last line, Back returned to the overview with the session alive, and "stop" ended it; no console errors. A first run found the three defects above. The busiest frame was 26.6 KB (1,513 frames, 19.7 MB over the two rounds); the probe is `scripts/simulator_brainstorm_room_probe.py`.
- Tests: Python 3684 passed (the whisper cold-start integration file excluded: one of its tests fails without this change), plugin 1272 passed, `tsc` clean, mypy one error in `reply_engine.py` (untouched). New: the seat ceiling and the timed-out wording, tool-call text blocks, `ScrollBox` opening on new words and the same through `HudPresenter`, plus the room-waits, round-per-move and whole-turn cases in `test_session.py`, `test_sinks.py` and `test_brainstorm_end_to_end.py`.
- Docs brought in line: `BRAINSTORM.md`, `SESSION_MODEL.md`, `ARCHITECTURE.md`, `MAC_APP.md`, `HUD_DASHBOARD.md`.

## 2026-10-15 — Build JOAT 0.1.134

- Packed from `cd86662` (clean tree) with `scripts/pack.sh` and the builder's `.env` package id and Mac candidates. Version guard: 0.1.134 above the last recorded build; app.json, package.json and package-lock.json all 0.1.134. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1270 tests passed, typecheck clean.
- `apps/hub-plugin/joat.ehpk`: 178817 bytes, SHA-256 `874ac4c55b6abcf9dec8d675633b256fa23569c53e657c85dcf5a88698ffad2f`. Adds to 0.1.133 the PLANNER first tab and the Notifications menu row (`61fb08e`). The reminders on the tab are read Mac side: takes effect when JOAT restarts. A build of the same tree to a scratch folder carries the Planner strings and none of the DEV `?joatTestOpenMenu=` hook; the `.ehpk` itself is an opaque container and was not unpacked. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-15 — Overview: the Planner is the first tab, and Notifications moves to the menu

Moves Notifications from the overview rail to the first option of the overview's contextual menu, and promotes Planner reminders to the first tab on the overview rail while keeping Notification card behavior unchanged.

- **Notifications is the first row of the glasses' menu, on the overview only.** It opens exactly as the old tab tap did: the newest unread card over the page it was opened from, a tap reads it and shows the next, and the Notifications list when nothing is unread. It is the same code (`CapabilityMenu.openNotificationsFromMenu`), now called from `EventRouter.handleMenu` (menu id 7). Menus on other pages have no such row; with it the overview's menu is 1 lead row + 7 capabilities, inside the firmware's 10.
- **The overview rail's first tab is PLANNER** (the rail is PLANNER, NEWS, POMODORO, QUICKNOTES). It lists your open Reminders, each labelled by when it is due: `Late`, a time for today (`09:00`), `Today`, `Tmrw`, or the weekday. More than eight shows seven and `+N more`. A tap opens Planning.
- **Where the rows come from.** A new Mac-side `PlannerPanel` reads incomplete reminders from EventKit for a window from 7 days back to 6 days ahead (`_LOOKBACK_DAYS`, `_AHEAD_DAYS` in `planner_panel.py`), republishes when the list changes, re-reads every 60 seconds and straight after you tap "reminders" on a note (a voice "create reminders from the last note" shows up on the next 60-second read). A failed read keeps the last rows; the third failure in a row clears them. It reads only Reminders, so Calendar permission is not needed for this tab.
- **Why it has its own query.** Planning merges Calendar events and Reminders, but shows a reminder only on its due day, so a late or next-week reminder would never appear in it. The tab asks for the whole window instead.
- **QuickNotes → Reminders is still manual.** A saved note's action items become Reminders when you tap "reminders" on the note or say "create reminders from the last note": due tomorrow 09:00, up to 8, repeats skipped. Nothing changed there; making it automatic is a separate decision.
- The pixel-overview fallback (`VITE_NATIVE_OVERVIEW=0`) has no menu row; the default text overview does.
- Verified in the official simulator (real gateway, a Mac stand-in pushing the panel and two unread cards): PLANNER is the first tab with its labelled rows, Notifications is not on the rail, a swipe moves the rail, a tap on PLANNER sends the Planning request, and Notifications opened through the menu path paints the newest card over the overview, a tap shows the next card, and a tap past the last returns to the overview (`scripts/simulator_planner_menu_probe.py`). The simulator cannot deliver a firmware menu click, so the menu row itself is covered by the page-container and router unit tests and by the DEV hook calling the same method, not by glasses.
- Tests: Python full run 3713 passed, 2 failed; one was this change's `test_panel_tabs_match_the_plugin_content_tabs` (updated, passes), the other `test_whisper_cold_start` failed before it. `PlannerPanel` has 16 new tests. Plugin 1270 passed, typecheck clean, mypy clean apart from the old `reply_engine.py:110` note.

## 2026-10-15 — Build JOAT 0.1.133

- Packed from `e9681ac` (clean tree) with `scripts/pack.sh` and the builder's `.env` package id and Mac candidates. Version guard: 0.1.133 above the last recorded build; app.json, package.json and package-lock.json all 0.1.133. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1266 tests passed, typecheck clean.
- `apps/hub-plugin/joat.ehpk`: 178697 bytes, SHA-256 `b1dd2d9e9ea1f9dee0d202c275f5e2344443fea4ebdb258bb347a01f4ae40231`. Adds to 0.1.132 the Brainstorm turn reader and the plugin's hand-over of the page to it (`f3e0567`). The room that waits on the wearer is Mac side: takes effect when JOAT restarts. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-15 — Build JOAT 0.1.132

- Packed from `8c08fbf` (clean tree) with `scripts/pack.sh` and the builder's `.env` package id and Mac candidates. Version guard: 0.1.132 above the last recorded build; app.json, package.json and package-lock.json all 0.1.132. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1265 tests passed, typecheck clean.
- `apps/hub-plugin/joat.ehpk`: 178658 bytes, SHA-256 `08a5acbc76c75c9275228da21383cbf02f53b87c65cf0c22ae126baea34a84ca`. Adds to 0.1.131 the one-write opening of a News article (`68feb2e`). The news half (readable gate, feed health, the 409-source catalog, `791ba18`) is Mac side: takes effect when JOAT restarts. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-15 — News: articles open without a blink, and only stories read in full are listed

Eliminates the double-write flash when opening a News article, filters out unavailable or paywalled feeds before listing stories, and expands the catalog of free full-text feeds across technology, business, engineering, research, and world news.

- **Opening an article is one write.** It used to rebuild the page (999 bytes) and then write a 1,784-byte stretch straight after, redrawing the same visible text. `ScrollBox.openWithin(999)` cuts only the opening stretch to what the rebuild carries; the full-size stretches start at the first bottom-edge handoff. The simulator never blinked, so this is the cause the code points to, **not yet seen fixed on the glasses**.
- **Why an unavailable feed was "used":** the reader fell back to the feed's own summary (or an apology) when the page could not be read, the tab said "Some feeds unavailable · partial list", and dead feeds were polled forever.
- **A story is listed only after its article was fetched and read** (`news.is_readable`: at least 120 words' worth of text, no paywall wording). Unreadable pages are remembered for 30 minutes; a failed fetch is retried, not listed.
- **Feed health.** A feed that fails three times running is benched for an hour, doubling to 12 hours; a feed whose fresh stories are all unreadable (six checks, none readable) is benched for 24 hours. Only a page's own fault counts (4xx other than 408/425/429, bad data). Refresh-now clears the benches. In memory only.
- **A balanced list.** The 20 stories are shared out across the chosen subjects (water-filling), with a cap per feed so one busy publisher cannot fill a subject, newest first. Eight workers, two per host, a 20-second deadline on the first or forced pass and 45 seconds otherwise; a catch-up pass runs a minute later.
- **The catalog: 15 subjects, 409 free sources** (`news_sources.json`). New subjects: Nigeria politics (23), Nigeria business (19), Nigeria tech (16), US (11), Engineering (71), Research (35), alongside Top stories (21), World (27), Business (24), Technology (48), Science (28), Health (11), Sports (25), Entertainment (39), Oklahoma (11). A source was accepted only if, with an honest user agent, its feed had at least 3 items, the newest under 14 days old, and at least 4 of 5 sampled stories read in full; it is free (metered and hard paywalls left out) and https. Every subject has at least 10 sources and its defaults.
- **Sources taken out** because they failed that test or duplicated another: Al Jazeera, NPR World, CBS World, NPR National, Phys.org, Guardian Science, BBC Health and KOCO (each subject keeps its other defaults; World now starts on BBC, Guardian and DW), plus the candidates that were paywalled, headline-only, http-only (CNN) or off topic.
- **The new subjects are off until enabled in the Mac app's News page**; `~/Library/Application Support/JOAT/news.json` was not touched. Its custom feeds, checked live: Pragmatic Engineer works, The Decision Lab is dead (404) and Medium Engineering is blocked (403) — the last two are never listed and are benched by the health rule.
- **Article text:** zero-width and soft-hyphen characters are removed from titles and text; "By ..." lines and one-word kickers before the first paragraph are dropped; "[email protected]" becomes "(email hidden)"; and trade and tech page furniture (Dive Brief, Table of Contents, "About the Author", copyright, Related content, comment threads, Techdirt tails, "Deals:" promos) no longer reaches the glasses.
- Tests: Python 3659 passed, 1 failed (`test_whisper_cold_start`, in the whisper engine, failing before this change); the news suite gained the readable gate, balance, feed health and tidy cases. Plugin 1265 passed, typecheck clean. mypy: one unused `type: ignore` in `reply_engine.py` (the venv now has `mlx_lm`; file untouched). Live check against the real feeds: 20 stories over the five new subjects in 13 seconds, 15 publishers, none benched. **Mac: takes effect when JOAT restarts. Plugin: rebuild and upload.**

## 2026-10-15 — Build JOAT 0.1.131

- Packed from `396a72d` (clean tree) with `scripts/pack.sh` and the builder's `.env` package id and Mac candidates. Version guard: 0.1.131 above the last recorded build; app.json, package.json and package-lock.json all 0.1.131. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1264 tests passed, typecheck clean.
- `apps/hub-plugin/joat.ehpk`: 178636 bytes, SHA-256 `b3b426e5a57d8aa7ef7e76b793ed3d5e0458d95541423f87575e4004b35b0d58`. Adds to 0.1.130 the glasses' own scroll box for News and Search (`73fcd12`) and for Messages and QuickNotes (`650360f`). The Messages and QuickNotes half needs the Mac side too: takes effect when JOAT restarts. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-15 — Messages and QuickNotes scroll like a News article

Extends the continuous firmware scroll box (`ScrollBox`) from News and Search to Messages conversations and saved QuickNotes.

- **A Messages conversation, a saved QuickNote (`QUICKNOTES · NOTE`) and its exact text (`QUICKNOTES · DETAIL`) are read in the glasses' own scroll box** (`ScrollBox`, as News and Search since `73fcd12`): a line a swipe, both ways, no round trip until a stretch's edge, where the next stretch starts one line below the top line on screen.
- **The Mac sends each reader whole** (`hud.list` `body`) in the reader template: who or what, a blank line, then the text (a conversation one message a line, newest first; a note's sections a blank line apart). It pages nothing: no counter row, no "up previous / down more".
- **Older messages carry on where the wearer stopped.** Past the thread's end the swipe asks the Mac for older messages (the reader promises a second page while chat.db has more); they arrive as the same thread grown longer, and the box opens a line below the last screen (`ScrollBox.resume`). A sent reply changes the thread's top, so it opens at the top, on the reply.
- A message's wrapped rows still hang under its words (`layoutHanging`). `READER_BODY_MAX_UNITS` is now 480,000 so a long recording's transcript arrives whole.
- Verified in the official simulator (0.9.3, 999-byte boxes): the long thread fixture 26 swipes down and 5 up, the note fixture 15 down and 2 up, one line each, stretch handoffs included. Tests: plugin 1264 passed, typecheck clean; Mac 3644 passed (the one failure, `test_whisper_cold_start`, is in whisper_serve, which this change does not touch); mypy clean. **Mac and plugin: takes effect when JOAT restarts and the plugin is rebuilt.** Not yet checked on the glasses.

## 2026-10-14 — JOAT no longer wakes on other people's speech

Prevents unaddressed background conversations from triggering a false "Yes?" wake response.

- **08:15:02** (the "Yes?" card over the paused QuickNote): Whisper, prompted, heard "JOAT." on room sound. The 10-12 check heard the same audio without the prompt and kept it because *something* came back: people talking nearby (a dozen non-wearer utterances 08:14–08:18). The check proved speech, not the name.
- **Now the no-prompt pass must sound like the name** (`whisper_serve._NAME_ALOUD`): one short word on a j/ch sound, maybe after "hey". These are the renderings measured on the live model for a spoken JOAT (Chode, Chau, Jode, Jote, Joke(s), Jolt(s), Jules, Jobe, Joe, Jones). Anything else is dropped (`other speech, not the name, without the prompt`). Near-misses of the name ("Joats.", three similarity wakes this morning) now get the same check (`only_prompt_words`). Requests that carry words of their own ("JOAT, what time is it?") are unchanged and pay no second pass.
- Tests: other people's sentence dropped and logged without content; every measured rendering kept; "Joats." checked. Whisper and orchestrator suites 159 passed; mypy clean. **Mac only: takes effect when JOAT restarts.**

## 2026-10-14 — Build JOAT 0.1.130

- Packed from `f6cd5e8` (clean tree) with `scripts/pack.sh` and the builder's `.env` package id and Mac candidates. Version guard: 0.1.130 above the last recorded build; app.json, package.json and package-lock.json all 0.1.130. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1243 tests passed, typecheck clean.
- `apps/hub-plugin/joat.ehpk`: 179044 bytes, SHA-256 `d83ee67c0797e9c9f7c7438cae2c5de1de431a657e14c4339ec258e11b3f6d7b`. Adds to 0.1.129 the two freeze fixes (`1f5fe75`). Plugin only: no Mac restart. Not installed or verified on physical glasses.

## 2026-10-14 — The glasses no longer freeze: two input traps fixed

Fixes two input lockups where `brain.log` (07:37–07:43 and 08:15–08:18) showed microphone audio continuing to stream while touchpad taps no longer reached the Mac until JOAT was reopened.

- **The contextual-menu flag always comes down** (`EventRouter`). The router drops every gesture, 2x tap included, while it believes the OS menu is open. Even's Contextual Menu docs: opening the menu fires foreground-enter and closing it fires foreground-exit, and only that exit lowered the flag. A lost exit, a menu click delivered after it, or a stale long press (a hold on a QuickNote whose release never came) mistaken for the menu on a real return to JOAT locked the glasses until a reopen. Now a real tap, 2x tap or swipe reaching the page lowers it (while the menu is open those gestures belong to the menu), it expires after 30 s (`CONTEXT_MENU_MAX_MS`), and only a long press from the last 3 s can stand for the menu (`MENU_LONG_PRESS_WINDOW_MS`).
- **A JOAT card over another page never eats a gesture.** Reproduced in the official simulator (`scripts/simulator_freeze_probe.py`): with a QuickNote open and paused, the "JOAT: Yes?" card (the name alone at 08:15:02) took the touchpad: a tap did nothing, the first 2x tap only drew "Continue?", and even 33 s later taps reached nothing. Now a tap with no Continue? to answer, or one 2x tap, closes the card and gives the page back; swipes still scroll a long answer, and a tap on Continue? still continues. Even's QA rule: "Every gesture has a visible response". Re-run: one tap closed the card and the note's own 2x tap then finished it.
- Research (Even docs, SDK 0.0.12–0.0.16 packages, Apple, community protocol notes): the SDK has no bridge-call timeouts and no background-state API in any version; Private builds are documented not to survive a 5-minute phone lock, Beta builds are.
- Tests: the menu flag (late click after exit, lost exit then a swipe, the opening long press, 30 s expiry, stale press), the card (tap and 2x tap close, a failed close not reported). Plugin 1243 passed; typecheck clean.

## 2026-10-14 — Open-source ready: no personal Mac, glasses or signing values in the repo, MIT license

Removes hardcoded machine names, glasses identifiers, and signing values so contributors can build and package JOAT with their own environment configuration. Even Hub's network whitelist requires exact origins with no wildcards ([Networking](https://hub.evenrealities.com/docs/build/networking)), so each packaged `.ehpk` injects its target endpoints at pack time.

- **Plugin config moved to the ignored `apps/hub-plugin/.env`.** The checked-in `app.json` is now generic: package id `com.example.joat` and only the simulator's loopback origins. `npm run pack` takes `JOAT_PACKAGE_ID` and `JOAT_RELEASE_WS_URLS` (environment first, then `.env`; only those keys are read, the file is never sourced) and writes them into the packaged manifest: that package id, and exactly those `wss://…/glasses` origins as the whole network whitelist. It refuses the placeholder id, non-reverse-domain ids, plain `ws://`, other paths and duplicates. The maintainer's Changelog version guard now runs only with `JOAT_RELEASE_CHANGELOG_GUARD=1`, since nobody else's uploads are in this log. The wearer's own `.env` carries his package id, both Macs and the guard, so his packaged manifest gets the same package id and the same two-origin whitelist 0.1.129 shipped with (checked by generating it).
- **No personal hosts in code.** The plugin's no-config fallback is the loopback simulator gateway instead of the MacBook; the phone panel names a Mac by its Tailscale machine name instead of guessing "Mac Mini"/"MacBook Pro". OpenClaw/Hermes add-connection drafts, tests, fixtures and docs use `home-mac`/`laptop-mac.example-tailnet.ts.net`; the glasses serial in a test is now a fake one.
- **Signing team from config.** `project.yml` reads `DEVELOPMENT_TEAM` from `${JOAT_TEAM_ID}` (XcodeGen environment expansion); `scripts/build_app.sh` takes it from the environment or the root `.env` and stops with a clear message if unset.
- `scripts/setup_brain.sh` now checks the Mac against `JOAT_RELEASE_WS_URLS` instead of `app.json`. Added `LICENSE` (MIT) and a "Run your own" section to the README.

## 2026-10-13 — Repo cleanup: research notes, audits and stray images removed

The wearer asked for the research markdown and stray images to go. Removed: `docs/audits/`, `docs/plans/`, `docs/features/` (FEAT-001/002 specs and sim frames), `docs/design/` (old HUD mockups), and `PRODUCTIVITY_SYSTEM_RESEARCH`, `EVEN_RESEARCH_QUESTIONS`, `VOICE_GAP_RECOVERY`, `WHISPER_COLD_START`, `REAL_G2_NAVIGATION_FIELD_RUN`, `MAC_APP_REDESIGN`, `MAC_CONTROLS_ACCEPTANCE`, `LOCAL_COMMAND_CHANNEL` from `docs/`, plus `design-qa.md`, `sheet.png`, `font-scales-green.png` and the two `apps/hub-plugin/glasses_*.png` simulator shots. Links to them in `DESIGN.md`, `docs/HUD_DASHBOARD.md`, `docs/AGENT_TERMINAL_ROADMAP.md`, `HudPresenter.ts` and `whisperkit.py` now state the fact without the dead reference. Older entries below still name some of these files; they are history. Plugin 1236 tests and typecheck, Python 3620 tests passed.

## 2026-10-13 — Build JOAT 0.1.129

- Packed from `8489e5d` (clean tree) with `scripts/pack.sh`, the maintainer's own manifest and the MacBook-first, Mac Mini-second private WSS candidates. app.json, package.json and package-lock.json all 0.1.129 (last build 0.1.128). `--sdk-ver 0.0.16` (the newest on npm), minimum Even App 2.2.10. Plugin 1236 tests passed, typecheck clean.
- Canonical `apps/hub-plugin/joat.ehpk`: 178774 bytes, SHA-256 `77a3705ddf5bf739ffea2a765747431dd483a02ccb3dc94d960dc51dbaa0c8db`. Adds to 0.1.128: bounded mic calls and JOAT's heartbeat (`080823d`). Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-13 — A paused note survives a reconnect, a stuck mic call no longer drops the link, and JOAT keeps its own heartbeat

Addresses connection dropouts and paused-QuickNote input stalls traced in `claude/background-link-research-2026-10-13.md`.

**1. A paused QuickNote answers again after a reconnect** (`CapabilityRouter.reattach_open_note`, `__main__.greet`). Every hello and disconnect made the Mac let go of the open note's page, while the plugin kept showing it, so every tap on it was dropped as stale (brain.log 2026-10-13 08:08 and 08:16 "dropping stale capability selection"; the resume worked only on a later reopen). When the note's page (live, finish or discard) was on the glass when the link went and the note is still open, the next connection gets the page back after the greeting, under a fresh Mac-minted generation the plugin adopts. A wearer who had left the note for another page is not pulled back into it.

**2. Every native mic call is time-limited** (`AudioStreamer`). `audioControl` calls had no limit and ran one at a time; when one never answered, every later one waited behind it, and the Mac's `audio.control` waited too until MacLink's 15 s downlink watchdog dropped the link. That loop ran 45 times on 2026-10-13 (09:04–09:17 and from 12:44), each reconnect cancelling the question just asked. Now each call gets the same 5 s limit as page writes (`BRIDGE_CALL_TIMEOUT_MS`, after Even's "wrap calls … with a few-second cap"). A start that answers late is adopted if the mic is still wanted and turned off otherwise; a re-arm with no answer keeps the stream and retries on the next liveness check (Even's Background & Lifecycle page: re-enable capture on foreground with the same source, and do not assume the stream is live).

**3. JOAT's own heartbeat** (`protocol.LinkPing`/`LinkPong`, `HubGatewayTransport._heartbeat`, `MacLink.armLiveness`). The websockets library's Ping is answered by the phone's network stack and page JavaScript cannot send Pings (RFC 6455 §5.5.2–5.5.3), so neither side could tell a frozen plugin page or a vanished Mac from a quiet link. The plugin says `heartbeat: true` in its hello; the Mac then sends `link.ping` every 10 s, the plugin answers `link.pong` straight from MacLink (not behind slow HUD work), and the Mac closes a link silent for 45 s with 4408 "no heartbeat" so the plugin reconnects and re-syncs. The plugin, once pinged, drops a link that brings nothing for 45 s and dials again. Both sides restart the clock after their own stall (a Mac event-loop stall, a suspended phone page), so a pause on one side is not blamed on the other. Older plugins (no `heartbeat`) are never pinged or held to it; older Macs never ping, so the plugin never enforces it.

**4. Setup guide** (`docs/HUB_PLUGIN.md` Path B). Corrected the same day: the first version gave Beta-group steps copied from Even's Testing docs, but the portal (tabs Builds, Testing group, Store listing) has no "Beta groups" tab. The guide now says only what is confirmed: a Private build does not survive a 5-minute lock (Even's docs), and the Beta path from this portal is unconfirmed.

Tests: plugin 1236 passed (bounded mic: give up at 5 s, next call not stuck, late success adopted or turned off, hung stop finishes; heartbeat: pong outside the HUD queue, quiet link redialed, a Mac that never pings never enforced, a suspended page restarts the clock), typecheck clean. Mac: heartbeat over a real socket (answering plugin stays, silent one closed 4408, older plugin left alone), protocol wire shapes mirrored in types.ts, paused note re-attached after reconnect and its tap counts, no pull-back after the wearer left, nothing when no note is open.

## 2026-10-13 — Only your glasses can hold your glasses: the key moves to the keychain

Hardens pairing token storage and display takeover rules so local test harnesses or untrusted processes cannot claim an active glasses session.

**What was true (checked on this Mac).** JOAT listens on loopback only, reached through Tailscale Serve (tailnet only, no Funnel); with Private Tailnet home brain every peer needs the pairing key, and unkeyed peers are refused (2026-10-12 07:24–07:49 and 15:01–15:15 in brain.log). The key was not in git or the Project. But it lived in `~/Library/Application Support/JOAT/pairing.json`, which any program running as the wearer can read, and any program could open a pairing window by writing `pairing-window.json`. And the newest authenticated display always won, so anything holding the key took the glasses from the phone.

**Changes**
- **The key lives in the login keychain** (`PairingKeychain.swift`). An item JOAT.app adds without an access object gets the default access, which trusts only the app that created it; any other program reading it makes macOS ask the wearer ([SecAccessCreate](https://developer.apple.com/documentation/security/secaccesscreate(_:_:_:)), [Access Control Lists](https://developer.apple.com/documentation/security/access-control-lists)). The file-based keychain is used because the data protection keychain needs access-group entitlements authorized by a provisioning profile, and JOAT.app has none ([TN3137](https://developer.apple.com/documentation/technotes/tn3137-on-mac-keychains)).
- **The engine gets the key from JOAT.app over its private launch pipe** (`EngineCommandChannel` ↔ `ChildCommandPipe`): `pairing.state` app → engine, `pairing.confirmed` engine → app. The engine says `"pairing": 1` in its hello; JOAT.app sets `JOAT_PAIRING_CHANNEL=1` at launch. Either side without the other keeps the old file behaviour, so an old app and a new engine (or the reverse) still pair. The pipe now starts before the gateway listens, and the engine waits up to 10 s for the key, so the phone is never refused for a key not yet delivered (it would only see 403, which never clears its token).
- **First launch moves `pairing.json` into the keychain**, reads it back, then deletes both files. A `pairing.json` that appears after that is ignored and removed, never trusted.
- **Only JOAT.app's Pair / Replace Glasses opens a window**, sent over the pipe; `PairingWindowStore` is gone. A phone key is kept only if it was paired inside that window (+30 s for the acknowledgement).
- **Only the same glasses take the link** (`hub_gateway._may_take_over`). While a display that named its glasses holds the link, a newer display must name the same glasses in its hello within 3 s, or it is closed 4409 ("glasses held by another device") and waits as a standby. The phone coming back and its own second page still take over at once; a simulator or harness does not.
- `speak_test.py` refuses the live JOAT on :8787 unless `--live`.
- Stopped a leftover plugin dev server (port 5193) a Claude session started on 2026-10-11.
- **The private pipe now says hello at once.** On the first install the key never arrived: JOAT.app drops a pipe that has not said hello within 30 s of launch, and the engine said hello only after loading its models (35 s this time), so the app had already let go. The pipe now starts at the top of `main`; each command owner joins when it exists (refused as not configured until then). This also fixes the app's News refresh / service retry buttons going dead after a slow start. After the fix: "pairing state from JOAT.app: paired" 9 s after launch, `pairing.json` and `pairing-window.json` gone, the item `com.joat.shell.pairing` in the login keychain.

Tests: Swift `EngineCommandChannelTests` + new `PairingKeychainTests` (23 passed); Python pairing, local-command pipe (real child process: forged identity and invalid key ignored, window applied, new key handed back), gateway takeover (other glasses wait, silent display waits, same glasses take over without losing earlier frames). SecItem add/update/read/delete smoke-tested on this Mac with a throwaway item. **Needs a JOAT.app build + install and a restart to take effect.**

Not changed: the Even AI agent token (`agent-token`) is still a file, because the wearer copies it into the Even app; it can move to the keychain with a Reveal button in JOAT.app.

## 2026-10-12 — The overview's QuickNotes ages keep counting

Fixes stale relative timestamps on the overview's QuickNotes tab when QuickNotes is the active rail tab.

**Why.** The age on each row ("[5m] Title") is text the Mac writes into the `hud.panel` push (`QuickNotesPanel.snapshot`), and the plugin paints the last push it cached. The tab was pushed only when a note was saved, summarized, or the glasses connected. So "5m" stayed "5m" until the next save. The QuickNotes page computes ages when it opens, which is why it showed the right time.

**Change** (`QuickNotesPanel.run`, `__main__`): a background task wakes when the first shown age would turn over (the next minute under an hour, the next hour under a day), never more than 60 s apart so a new "now" becomes "1m" on time. It pushes the tab only when its rows would read differently, through the same `panels.publish("quicknotes")` the save already uses. The plugin's existing `acceptPanel` repaints the overview only when QuickNotes is the tab on the glass, the same per-minute repaint the Pomodoro tab already does. Nothing is pushed before the first publish (the plugin's connect fills the tab). The task is cancelled at shutdown with the news task.

Tests: ages pushed at each minute boundary (30 s, then 60 s, 60 s), nothing pushed when "[3h]" still reads "[3h]", nothing before the first publish. `tests/unit`: 3268 passed. **Mac only: takes effect when JOAT restarts; no plugin build.**

## 2026-10-12 — A QuickNote cut off by a JOAT restart is saved at the next start

Fixes interrupted recordings left in `Note [timestamp] · gaps` state with no summary or Apple Notes export when JOAT restarts mid-note.

**What happened (the database and brain.log, not a guess).** The three notes since 11:44 (13:48, 14:24, 15:22) were open when JOAT restarted: at 14:22:58, 15:15:53 and 15:35:00, each a restart to apply a change. `interrupt_for_shutdown` marked each `interrupted` (`graceful_shutdown_raw_retained`), and nothing ever finished it: no save, so no summary title, no Apple Notes copy, and the list shows any note that is not `saved` as "· gaps". The words and raw audio were all kept (2, 1 and 3 transcript sections). The latency and capitalization change (`015bb95`) was not involved: no note reached Save after it.

**Change** (`VoiceNoteCaptureController`, `VoiceNoteStore`, `__main__`):
- `finish_interrupted()`: at start, before the gateway accepts the glasses, every note a restart cut off (`graceful_shutdown_raw_retained` or `process_restart_raw_retained`, raw kept, at least one frame) goes `interrupted → finalizing` and through the same save as the glasses' Save: leftover audio transcribed, finalized, then the existing `on_saved` hook (summary title, Apple Notes). Save and this share one `_save`; nothing is duplicated. Discarded, failed and empty notes are untouched.
- The store allows `interrupted → finalizing`, and only that.
- Dry run on a copy of the live database (the copy since deleted), with the same model on a private port: all three notes saved complete (no gaps, raw purged), the save hook fired for each. Tests: a note interrupted mid-sentence is saved by the next start with its words and its leftover audio, once; a discarded note is never revived. Mac suite 3607 passed; the one failure is the intermittent `test_whisper_cold_start` timing test. **Mac only: takes effect, and saves the three notes, when JOAT restarts.**

## 2026-10-12 — Room noise no longer wakes JOAT: the bias prompt was putting the name into it

Eliminates false wake triggers right after engine startup caused by room tone opening `RmsVad` before the noise floor settled and Whisper echoing the wake-word bias prompt.

**What happened at 15:35:40 (brain restarted 15:35:27, glasses connected 15:35:36).** The first "utterance" opened 0.27 s after the mic, at `trigger=0.00400`. Whisper returned 5 characters, which were not ignored and reached no reply. That is the one branch that writes no log line: the name alone, which opens the JOAT card ("Yes?") and takes the next sentence without the name for 8 s.

**Why, established step by step.**
1. *Only one line puts "hearing you…" on the glass* (`VoiceOrchestrator._acknowledge`; nothing in the plugin or JOATShell). The running brain is this checkout (`.venv`, `apps/mac/joat`) with `e6f724c` in it.
2. *The speech detector opens on room tone right after every brain start.* `RmsVad.floor` is 0 until 10 batches (~2 s) of history, so the trigger is the bare `speech_rms` minimum, 0.004. G2 room tone is above that. The log shows this after every brain start today, 7 of 7 (07:44, 11:27, 12:06, 13:03, 14:23, 15:17, 15:35): the first utterance opens at 0.004, and Whisper returns 3–8 characters of nothing ("The"), or at 15:35, 5.
3. *Whisper puts the name into noise because our prompt does.* The conversational bias prompt is `"JOAT. The wearer addresses the assistant as JOAT."`. It opens with the wake word, as a 5-character sentence. `is_prompt_echo` needs 24 characters, so the full 43-character echo is dropped (logged) but "JOAT." passes. **Reproduced on the live model** (private `whisperkit-cli serve` on :50061, same model and flags, the real prompt, the brain's own echo filter and gate): 40 synthetic room-noise clips (2.4 s like turn 1; white noise and low hum at RMS 0.006–0.02) gave the full prompt (13), "The" (20), and **"JOAT"/"JOAT." 7 times, each a wake**. Low hum at RMS 0.02 gave "JOAT" 5 of 5.
4. *The prompt is the cause, and it is also what makes the name spell right.* The same noise with no prompt came back empty, 15 of 15. But macOS voices saying "JOAT." / "Hey JOAT." / "JOAT, what time is it?" (Samantha, Daniel, Moira; clean and over hum) were recognised 17 of 18 with the prompt and only 7 of 18 without it ("Chode", "Jobe", "Jules"…). Every spoken name still came back as *some* word without the prompt (9 of 9), while noise came back as nothing.
5. *Whisper's own scores do not separate them here.* The serve reports `no_speech_prob` 0 for everything. `avg_logprob` was −0.28 to −0.29 for hum "JOAT" and as low as −0.26 for a spoken one.

Outside sources agree. OpenAI's Whisper prompting guide treats the prompt as prior transcript ("intended to help stitch together multiple audio segments"), which is why it can come back on audio with no speech. A study of Whisper large-v3 on non-speech audio (arXiv 2501.11378) found hallucinations on 40.3% of 301,317 files, with an effective VAD as the largest mitigation.

**Changes**
- `whisper_serve.only_prompt_words` + `WhisperServeEngine._confirm_without_prompt`: a conversational transcript made only of prompt words, at least one a content word ("JOAT.", "assistant as JOAT"), is heard once more **without** the prompt. If nothing is there (empty or a sound annotation), it is dropped (`dropped prompt-word hallucination chars=N`). Otherwise the prompted spelling is kept (`… confirmed without the prompt`). Requests ("JOAT, what time is it?") and function words ("The") are not checked, so they cost nothing. A failed check keeps the transcript.
- `RmsVad.arm_after_floor` (on for JOAT's conversation VAD, off for QuickNotes' fresh per-note VAD): no utterance starts until the room's floor is known (10 batches).
- **Re-run on the live model with the fixed engine:** noise 0 of 13 wakes (the five hum "JOAT"s all dropped). Spoken names 17 of 18, the same as before the fix (the one miss, "JOAT." over hum → "Chau.", was misheard before the change too).
- Tests: `TestShortEchoIsHeardAgainWithoutThePrompt` (noise name dropped and logged without content; spoken name kept with prompted spelling; sound annotation counts as nothing; requests and "The" heard once; full echo still dropped by the old rule; failed check keeps it), `TestRoomBeforeSpeech` (room tone opens nothing with the guard, speech after the floor still opens; the old default unchanged). Full Mac suite 3605 passed, 1 failure in `test_whisper_cold_start` (pre-existing, recorded above); mypy clean. **Mac only: takes effect when JOAT restarts.**

## 2026-10-12 — Build JOAT 0.1.128

- Packed from `7151db3` (clean tree) with `scripts/pack.sh`, the maintainer's own manifest and the MacBook-first, Mac Mini-second private WSS candidates. Version guard: 0.1.128 above the last recorded build (0.1.127); app.json, package.json and package-lock.json all 0.1.128. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1227 tests passed, typecheck clean.
- Canonical `apps/hub-plugin/joat.ehpk`: 176799 bytes, SHA-256 `41d0c67948a0df734e220ed5b2007309fbdcd55e6f1d9f4e77acd2d8d531450f`. Adds to 0.1.127: a route runs on one-shot phone fixes when the location stream will not start (`b0c1844`). The Mac's navigation step logs take effect when JOAT restarts. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-12 — A route starts when the phone's location stream will not

Fixes "Navigation could not start · The route was not installed on the phone" when tapping a saved place while the host's continuous location stream fails to start.

**Cause.** The place, the mode, the origin and the Mapbox route all worked; the plugin refused the route at the last step. Installing a route switches the Even host's one location stream to high accuracy (`startAppLocationUpdates`, 1 s / 1 m), and since 2026-10-05 a route the stream cannot serve is rejected rather than left on "Starting navigation". On the wearer's iPhone the stream would not start, while one-shot fixes (`getAppLocation`) kept arriving: the route's own origin was one. Even's docs document both calls but not when the stream may refuse. The brain log shows the same pattern outside navigation: after the 15:35 connect no stream fix arrived for 4.5 minutes.

**Change** (`LocationSessionManager`, plugin):
- When the stream will not start for a route, the route runs on one-shot high-accuracy fixes, one after another with a 1 s rest, through the same path as stream fixes (the navigation consumer, the `navigation_location` diagnostic, the coarse Mac uplink). The first fix decides the route; only a phone that gives neither a stream nor a one-shot fix refuses it.
- While on one-shot fixes it tries the stream again every 30 s and hands over once it starts. Ending the route ends the reads.
- Mac (`NavigationSession`): content-free INFO lines for each step (view kind, origin received/unavailable, route sent, route installed/rejected and how long the phone took), so a failed walk shows where it stopped. Nothing else changed on the Mac.
- Tests: a route on one-shot fixes when the stream will not start (and the reads stop with the route), a refusal only when no fix comes either, the hand-back to the stream. Plugin 1227 passed, typecheck clean; Mac navigation tests 96 passed, mypy clean. The simulator cannot supply a phone fix, so this is unit-verified; the walk itself is the wearer's.

## 2026-10-12 — "hearing you…" only when JOAT is going to act

Suppresses premature "JOAT · hearing you…" status updates on unaddressed ambient speech before wake-word verification runs.

**What the log showed (15:19:16 reconnect → 15:22).** The gate held: the speech detector (`RmsVad`) opened 16 utterances in 3 minutes, 15 were ignored (`unaddressed speech ignored`, `STT produced no text`), and one woke JOAT (15:20:52, `'joats' ~ 'joat'`). But `_transcribe` painted "JOAT · hearing you…" on the status line *before* Whisper ran, so before the name check, for every one of them, for 1.3–2.4 s each. The status line said JOAT was listening to the wearer about every 10 s, while it was about to ignore them. The gate fixed the answers, not the acknowledgement.

**Change** (`VoiceOrchestrator`):
- `_speech_expected(turn)`: the acknowledgement goes up during transcription only when the speech will be taken without the name. That means: a question on the glass awaiting its answer (a prompt ticket), a Brainstorm topic tapped for dictation, the sentence after "JOAT" alone (`NAME_ONLY_WINDOW_S`), follow-ups in an open conversation (off in production), or no wake words configured.
- Otherwise it goes up in `_act` the moment the transcript carries the name, before feature navigation, commands or the model (`_acknowledge`, once per turn), so a real request still shows "hearing you…" before its answer card.
- Room talk and coughs never touch the status line: nothing goes up, and `_act` no longer repaints "listening" after an ignored utterance. A cough during a running answer no longer covers it. `_restore_status` defers only to an *acknowledged* transcription in flight (`_stt_acked`).
- Latency receipt: on a named request `ack_sent` now follows `stt_complete` (Whisper is ~1.3–2.4 s here); it still precedes the first model chunk.
- Tests: `TestHearingYouOnlyForJoat` (room talk and coughs leave the status line untouched; the name shows it once, before the conversation card; after the name alone the next sentence is acknowledged while Whisper runs; a cough during an answer does not cover it). Two older tests updated to the new order. Orchestrator, message-reply and latency suites 129 passed; full Mac suite 3594 passed, 1 failure in `test_whisper_cold_start` (spawns a Whisper child, does not touch the orchestrator; fails alone too, as recorded above). mypy clean. **Mac only: takes effect when JOAT restarts.**

## 2026-10-12 — Build JOAT 0.1.127

- Packed from `f6364e5` (clean tree) with `scripts/pack.sh`, the maintainer's own manifest and the MacBook-first, Mac Mini-second private WSS candidates. Version guard: 0.1.127 above the last recorded build (0.1.126); app.json, package.json and package-lock.json all 0.1.127. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1224 tests passed, typecheck clean.
- Canonical `apps/hub-plugin/joat.ehpk`: 178070 bytes, SHA-256 `ed0132ced81ccdf3f0113b82eb9d25e47feccb0aa0b2394d0592338eb8e7805a`. Adds to 0.1.126: long tap no longer turns the mic on or off, and "long tap: mic" is gone from the footers (`cda2c8f`). Simulator: the status card's footer reads "2x tap: menu". Plugin only: no Mac restart. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-12 — Long tap no longer turns the mic on or off

Removes the global long-tap mic mute toggle so long-pressing on QuickNotes for push-to-talk never conflicts with microphone state.

- **Long tap keeps only its specific jobs** (`EventRouter.handleLongPress`): hold to talk on an open QuickNote, refresh an expanded News or Search tab, cancel a running Brainstorm/terminal job. Anywhere else it does nothing. It is still consumed in every event class (list, text, sys), so the release never reaches the Mac as a scroll or phantom input, and a tap-then-long-press that opened the glasses menu still cancels a note hold.
- **"long tap: mic" is gone from every footer**: the resting overview (`Chrome.HINT`, now `tap: open · swipe: tabs · "JOAT …"`), the panel overview, Pomodoro, and the four status cards (now `2x tap: menu`).
- **What still controls the mic.** Taking the glasses off still stops it (`DeviceMonitor`, the privacy fail-safe). Putting them back on follows the phone setting Listening → After an interruption: "Ask first" shows "Resume listening?" for 8 s, and a missed or declined card leaves the mic off. There is now no glasses gesture to turn it back on; the Mac opens it again when the plugin reconnects, and QuickNotes reopens it for a note. "Resume automatically" turns it back on as soon as the glasses are on. A cached host menu's old Mute row (item 2) still toggles it.
- Tests: the long-tap block now asserts that the mic is never started or stopped, that every class is consumed, and that the carve-outs still run. The footer hints are asserted free of "long tap". Plugin 1224 passed; typecheck clean. **Plugin only: needs the new build.**

## 2026-10-12 — QuickNotes: words reach the glasses ~0.6 s sooner, and a pause no longer starts a new sentence

Reduces QuickNotes transcription latency by ~0.6 s per chunk without sacrificing accuracy, and adds a settle pass so mid-sentence pauses preserve casing and punctuation.

**Measured first** (private whisper serve on ports 50061–50063, same large-v3_turbo model and flags, synthesized dictation with room tone, cut exactly as `VoiceNoteCaptureController` cuts; live brain untouched; numbers in the project doc `quicknotes-latency-bench-2026-10-12.md`). Each chunk paid ~0.6 s for the wake-word bias prompt: WhisperKit decodes prompt tokens one at a time. Prompting with the previous text (OpenAI's documented way to stitch segments) cost +0.5–3 s and did not fix the capitals. Decoder or encoder on the GPU was 40–60 % slower with identical text. Live partial transcripts: whisper serve answers one request at a time, so a partial delayed the real words (3.6 → 5.4 s). Cutting at the quietest frame before the 15 s cap was no better (WER 3.9 % vs 3.4 %).

**Changes**
- `WhisperServeEngine.transcribe_once` (QuickNote chunks only) is sent without the bias prompt (`_post(bias=False)`); the conversational path keeps it. The bias also leaked into a note once ("The wearer can also see…").
- **Settle pass** (`VoiceNoteCaptureController`): once the wearer has been quiet ~2 s, or pauses, releases a hold or saves, the paragraph's newest stretch (up to 28 s, never across a hole, at least two chunks) is read again as one piece of audio and replaces the chunk-by-chunk draft. Whisper heard each chunk as a new sentence ("Friday. And then …"); heard whole, the sentence keeps its case and punctuation, and a word split by the 15 s cut is heard whole. It gives way to new words (runs only when no chunk waits and the wearer is not speaking; Save settles what is left), keeps the draft when the new reading loses words or runs on (`_keeps_the_words`), and never writes a paragraph that was undone meanwhile. `VoiceNoteStore.extend_transcript` may now revise a paragraph in place (it still never shrinks). The live page's "…" shows only while new words are on their way, not during a settle.
- Tests: bias absent on note chunks, present on conversation; settle on a pause, giving way to new words, keeping the draft, never across a hole, undo during a settle; store revision in place. Voice-note, store, commands and whisper suites pass; full Mac suite 3590 passed, 1 failure in `test_whisper_cold_start` that also fails intermittently on its own (unrelated spawn timing).

**Through the real recorder, fed in real time** (2 voices × 2 dictations with 1.3 s and 2.6 s pauses, before = bias + no settle): words stored after the cut median 1.58 → 0.99 s, p90 1.82 → 1.27 s; 4 of 32 chunks waited behind a settle (worst 2.44 s vs 1.92 s before). Wrong capitals 18 → 4, punctuation errors 16 → 7, WER 0.5 % → 0 %. Save median 1.31 → 1.86 s. Add ~1.0 s of pause detection before and ~0.25 s of repaint after. Synthesized voices; to be confirmed on the wearer's next real note. **Mac only: takes effect when JOAT restarts.**

## 2026-10-12 — JOAT answers only when named: "JOAT", "Hey JOAT", "Hey Joke"

Restricts ambient wake triggers strictly to explicit wake phrases ("JOAT", "Hey JOAT", "Hey Joke") and disables unprompted 30 s follow-up capture on short noise transcripts such as "The".

**Why it triggered.** Not the wake word: since 2026-10-08 only 4 utterances woke JOAT by similarity (`joats ~ joat`), none on "the". It was the 30 s conversation window: after JOAT answered, *any* speech in the next 30 s was taken as a follow-up, no name needed, and every answer re-opened the window. 14 such follow-ups were answered since 10-08; several were 3-character transcripts ("The") — e.g. 11:55:43 and 11:56:19 today, each followed by `reply_complete`. "The", "you", "Thank you" are what Whisper makes of a cough, a breath or room noise.

**What the microphone is doing (measured from brain.log, 10-08 → today).** The Mac's speech detector (`RmsVad`, an energy threshold) opened 104 utterances; 43 came back from Whisper as no text, a prompt echo or a sound annotation, and 31 of the 85 transcripts were 4 characters or fewer. The host's speaker label dropped only 4 as another person. Even's documentation for the G2 microphone (Device APIs) covers `audioControl(true, Glasses)` after the startup page and 16 kHz s16le mono PCM in `audioEvent` — nothing about voice detection, noise suppression or wake words; the SDK's `speakerRole` is "an app algorithm result, not a firmware identity assertion" and `direction` is an undocumented raw tag. The plugin follows that contract. Speech detection is ours, and it opens on non-speech; that alone was harmless while the name gated every request, and the window removed the gate.

**Change** (`VoiceOrchestrator`, `__main__`):
- `follow_ups_without_name=False` in production: no speech is a request without the name. The conversation card and its tap Continue are unchanged.
- The name alone ("JOAT" → "Yes?") makes the *one* utterance within 8 s the request (`NAME_ONLY_WINDOW_S`), then the name is needed again.
- Filler — "The", "you", "Thank you", "Thanks for watching", "um", "so" — is never a request, in any mode (`_is_filler`).
- `WAKE_WORDS` (one list, used by `__main__` and the tests): "JOAT"/"Jote" anywhere; the renderings that are ordinary English — joke, jokes, choke, jot, jolt — only after "hey" ("Hey Joke"). Bare "joke …" no longer wakes it; "that's a joke" never did and still doesn't.
- Tests: `TestOnlyTheNameWakesIt` — the 2026-10-12 sequence through real voice turns (an answer, then "The", room talk and "that's a joke" unanswered; name alone then the request answered, the next sentence not; "Hey Joke, …" answered), the gate rules, filler in both modes, the 8 s expiry. Orchestrator 104 passed; full Mac suite 3584 passed. **Mac only: JOAT restarted.**

## 2026-10-12 — The Mac measures the glasses' microphone (content-free)

Adds content-free microphone open-latency and PCM cadence telemetry on the Mac gateway.

- Even Hub's device-features contract: `audioControl(true, Glasses)` opens the G2 four-mic array (after `createStartUpPageContainer`); PCM arrives as `audioEvent` buffers, 16 kHz s16le mono; the plugin batches ~200 ms per `audio` message. It does not say how long `audioControl(true)` takes on the glasses, or whether PCM keeps flowing while nobody speaks — which decides whether the plugin's 1.5 s silent-stream re-arm (`STREAM_STALE_MS`, `f005189`) is safe.
- `joat/ble/mic_meter.py` (new, `40bc048`), wired into `HubGatewayTransport`: on every Mac `audio.control open`, `mic open: plugin ack +X ms, first pcm +Y ms (link rtt Z ms)` — ack is the plugin's `device mic=true`, sent once the host call resolved; the WebSocket ping round trip is beside it so the network can be subtracted. While the mic streams, `mic cadence: N messages, P s of pcm in W s (%), longest gap G ms, gaps over 1.5 s: K` 60 s after each open, then every 5 min, and on close/disconnect. Counts and times only; never audio content.
- Tests: `tests/unit/test_mic_meter.py` (open timing, a continuous stream at 100 % with no stale gaps, a 4 s host silence counted, the 5-minute cadence, nothing logged without audio); gateway 92 passed; mypy clean. Mac only; running since 13:03:07.

## 2026-10-12 — Build JOAT 0.1.126

- Packed from `fbc18cf` (clean tree) with `scripts/pack.sh`, the maintainer's own manifest and the MacBook-first, Mac Mini-second private WSS candidates. Version guard: 0.1.126 above the last recorded build (0.1.125); app.json, package.json and package-lock.json all 0.1.126. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1224 tests passed, typecheck clean.
- Canonical `apps/hub-plugin/joat.ehpk`: 178106 bytes, SHA-256 `4ed2b9e5785fa599a5d2bfe66b0788a0d2c2e786bf0dbe75c57a2a3986e928eb`. Adds to 0.1.125 the fixes below (`5521339`). Plugin only: no Mac restart. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-12 — Even's rules applied: cancel works on every card, no card is declined unseen, accented Messages pages keep their last lines, bridge calls have time limits

Audits every glasses surface against SDK 0.0.16, simulator byte limits, and Even's UI/input guidelines, hardening card cancellation, UTF-8 pagination, and bridge timeouts without altering visual layout.

- **Swipe and 2x tap cancel every card.** Since `b91ed1d` only a Messages draft heard them: the router was handed a function that always answered "no retry", so a calendar or reminder card waited out its 8 s. It now gets the open card's retry, which is undefined for every card but a draft (`EventRouter`, `main.ts`). Simulator: swipe declined in 0.02 s, 2x tap in 0.01 s, tap accepted.
- **A card is never declined before it shows, and the glass never freezes after one.** Every page change is now recorded as the change in flight (`HudPresenter.pageTransition`, the existing `pageReplacement`), and a card, Back to Home and a handoff wait for it (`settlePageReplacement`) instead of being turned away by the page barrier. Before: a card that landed while Home was redrawing was declined to the Mac unseen, and Back during another change left the old page on the glass. Home's rebuild hands the barrier the display's own guard, not "still Home": a card arriving mid-rebuild had kept the image lane shut for the rest of the session.
- **Messages pages in accented or non-Latin text keep their last lines.** Pages were counted in code units, but the glasses' cap is 999 bytes, and accented letters, Cyrillic and Greek are 2–3 bytes each. The rebuild cut the page with "…" and the next page started after the lost lines. Every paginator now asks one question, `pageFits` (units and bytes), and an in-place page turn is capped like a rebuild (`FirmwareText`, `upgradeNativeText`). A page turn the glass refuses no longer moves the page count, so a review's last-page tap is never offered for words that were not shown (`scrollTextPage`). Simulator: a Cyrillic thread with a Yoruba message paged 1/2 → 2/2, last line "Вы: Договорились, позвоню в девять." shown.
- **Time limits on bridge calls.** A rebuild or text update the host has not answered in 5 s fails (`BRIDGE_CALL_TIMEOUT_MS`, Even: "wrap calls in Promise.race with a few-second cap") and the page is no longer trusted, so a hung call cannot hold every later page change. `settleWithin` moved from `HudVisualSurface` to `BridgeClient` so both use the one copy.
- **One dropped image send no longer rebuilds the page.** The image lane says why a frame failed (`lastPaintFailure`): `sendFailed` or a timeout skips the self-heal rebuild, which wiped the last good frame on every frame of a weak link. An image the host turns down still heals.
- **Host storage has one door** (`BridgeClient.storage`): a value the host already holds is not written again (the preferred Mac was rewritten on every reconnect, racing the page writes), and writes go one after another. Settings, Places, pairing and the preferred Mac all use it; the two copies of the adapter and the scattered direct calls are gone.
- **A press cut short no longer swallows the return to JOAT.** Going to the background mid-press, or a card taking the release, left a flag that made the next return look like the glasses' menu opening: no repaint, then every gesture dropped.
- **Exit stops the mic, motion sensor and location one after another, each bounded**, so one stop that never answers cannot leave the others running.
- Verified in the official simulator on an isolated loopback brain: Home, the card and its swipe/2x tap/tap, the Cyrillic thread both pages, Back, Pomodoro and Home again. Not verified on physical glasses.
- Tests: new cases for each fix (each failed before the fix); plugin 1224 passed, typecheck clean. **Plugin only: a new package, no Mac restart.**

## 2026-10-12 — Build JOAT 0.1.125

- Packed from `709f263` (clean tree) with `scripts/pack.sh`, the maintainer's own manifest and the MacBook-first, Mac Mini-second private WSS candidates. Version guard: 0.1.125 above the last recorded build (0.1.124); app.json, package.json and package-lock.json all 0.1.125. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1203 tests passed, typecheck clean.
- Canonical `apps/hub-plugin/joat.ehpk`: 176244 bytes, SHA-256 `7a1f7ddc5de150f0ba4d6ac9ef6fd986980e49b7dcbc4847a517ab8853c55da9`. Adds to 0.1.124 the standby takeover and mic re-arm (`f005189`). The Mac side of `f005189`, the speaker-gate fix (`5972696`) and the send preflight (`9f19f21`) are already running: JOAT restarted 12:06:50, `messages automation granted`. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-12 — A reply that cannot be sent fails at once, unsent, instead of sitting on "sending"

Preflights macOS Automation permission for Messages.app so a blocked send fails fast as unsent (`×`) rather than hanging on `osascript` and remaining stuck on `▷`.

- **What happened** (11:58): the reply was heard, reviewed and confirmed (11:58:43); `stage=send-failed error=MessageSendError` 15 s later. This was the first send the brain has ever attempted — the log holds no earlier send. Sending is an Apple Event to Messages (`osascript` → `tell application "Messages"`), gated by macOS's **Automation** permission (JOAT may control Messages). The access JOAT already had is Full Disk Access, which reads chat.db. macOS put the Automation consent prompt up on the Mac; `osascript` blocked on it until the 15 s deadline killed it; a killed send is "possibly submitted", so the glasses stayed on ▷.
- **Fix** (`MessagesAppSender`): a read-only probe, `tell application "Messages" to count accounts` (no recipient, no body), reads the permission. The engine runs it at start in the background with a 3-minute window, while the wearer is likely at the Mac, so the prompt is answered there; the outcome goes to the log and `status.json` (`messages_send`). With `preflight=True` (production), every send probes first with a 3 s limit: denied, pending or no answer is a definite "not sent" (`possible_external_commit=False`) → × on the glasses, at once. A send refused with errAEEventNotPermitted (-1743) is likewise definite. The send-failed log line now carries a content-free `code=` and `possible_commit=`.
- The wearer's grant: System Settings → Privacy & Security → Automation → JOAT → Messages (one time; JOAT.app is Developer ID–signed, so it holds across rebuilds).
- Also fixed: `test_message_tools.py::test_controller_review_limit_remains_a_truthful_failed_status`, broken since `e675ec2` raised the Messages review limit to 280 — it now drafts 300 characters, the length that is still too long for the box.
- Tests: the probe's four states, its read-only argv, the preflight failing fast without spawning the send, a granted preflight sending as before, a -1743 refusal as a definite not-sent. Messages suites 236 passed, mypy clean. **Mac only: restart JOAT.**

## 2026-10-12 — The plugin no longer freezes after a second copy of it takes the glasses; a mic the host stopped is re-armed

Investigates and fixes background WebView lifecycle handoff stalls where a replaced plugin instance stayed halted after the newer instance closed, and re-arms microphone capture on foreground resume.

**What the logs showed.** At 07:49:35 the plugin connected; 0.7 s later a second copy of the plugin connected and replaced it (`4409 plugin connection replaced`) — two copies alive at once, the host's foreground/background handoff. At 07:57:43 the second copy died with no close frame (`1005`). From then until 11:42:16 — **3 h 45 min — not one dial reached the Mac.** A running plugin redials every few seconds; this one could not.

**Why (plugin).**
- A copy that receives `4409` called `halt()`: `closed = true`, no redial, display suspended, mic and IMU stopped, every gesture and audio frame dropped (`EventRouter.dispatch`). The only way back was `resumeIfReplaced()`, called from a host foreground event or `__restoreState`. When the copy that replaced it died first, nothing ever revived it: the glasses kept the last frame and ignored every tap.
- The mic flag outlives the host's capture. Even Hub's Background & Lifecycle page (hub.evenrealities.com/docs/build/background-lifecycle): `audioControl(true, …)` capture "stops if the WebView is suspended"; re-enable it on foreground with the same `AudioInputSource` and do not assume the stream is still live. `onForegroundEnter` repainted and set the status to "JOAT · listening" from `audio.isOpen` — this page's own belief — and never called `audioControl` again. `openPromptMicrophone` returned "ready" for any open flag without touching the host, so a tap-to-reply after a background trip opened the box on a dead stream: the listening animation, no words, stuck.

**Fix.**
- **Standby takeover** (both sides; wire constant twin `PLUGIN_SLOT_OCCUPIED_CLOSE_CODE = 4410`). A replaced copy still yields exactly as before, but every 10 s it dials with `?standby=1`. The Mac refuses a standby dial while a display holds the glasses — HTTP 409 at the upgrade (`HubGatewayTransport._authorize`), and again right before `auth.accepted` (close 4410, `_accept`) so a racing arrival cannot be evicted — so two copies never trade the glasses. When the slot is free the standby copy is admitted and is the display again (display resumed, hello, the Mac reopens its mic). A host foreground event still takes over at once.
- **Mic liveness** (`AudioStreamer`): it records when the host last delivered PCM. `rearm(reason)` re-issues `audioControl(true, sameSource)`; a refusal stops the mic honestly instead of keeping "listening" over nothing. Foreground enter re-arms (Even's guidance) and only then sets the status line; a reply question on a stream silent for 1.5 s re-arms before it says ready; an open stream silent for 1.5 s re-arms itself, at most every 10 s.

**Verified.** Official simulator (`scripts/simulator_display_takeover_probe.py`, new): the simulator's copy connected; a second display took the glasses and held them 25 s (the simulator's copy made no takeover; its standby dials were refused); the second display died without a close frame; the simulator's copy was back **3.2 s** later on its own, mic streaming, and a swipe + tap opened News. Frames: `docs/features/FEAT-001-messages-voice-reply/sim-frames/display-takeover.png`. The simulator does not stop its mic on a lifecycle event the way the phone host does, so the re-arm is verified by tests modelled on Even's documented behaviour, not on the simulator. Not yet verified on physical glasses.

**Tests.** `MacLink.test.ts`: the replaced display only standby-dials while held, takes the glasses back when the slot is free, treats a hung or failed standby dial as "not now", and a foreground event still takes over at once (the old "no dial for 60 s after 4409" assertion encoded the freeze and now asserts standby-only dials). `AudioStreamer.test.ts`: a question on a silent stream re-arms before ready; a live stream is left alone; a refused re-arm leaves the mic off; a silent stream re-arms at most every 10 s; re-arm keeps phone as phone. `test_hub_gateway.py`: a standby dial never evicts the live display (409); it takes a free slot; the pre-accept re-check closes 4410. Plugin 1203 passed, typecheck clean; gateway 92, protocol 184, mypy clean. **Both sides change: a new package and a JOAT restart.**

## 2026-10-12 — A tapped reply is no longer thrown away for one stray `other` speaker frame

Fixes tap-to-speak message replies being discarded when Even's speaker-role heuristic tags a single frame of the user's utterance as `other`.

- **What the log said** (`scripts/reply_trace.py --since "2026-10-12 07:49"`): two tapped replies on the glasses, both transcribed (37 and 88 chars — the second after a tap on the box, `prompt commit by tap`), both then `prompted answer not taken owner=messages reason=other_speaker`. The words were heard; the Mac refused them.
- **Cause.** `PromptedInputDispatcher.dispatch` refused a prompted answer if ANY frame of the utterance carried the host's `other` speaker tag, and `_maybe_publish_partial` suppressed the live words on the box by the same test. The Even app's speaker role is "an app algorithm result, not a firmware identity assertion" (SDK 0.0.16 `AudioEvent.speakerRole`); on the glasses it tags a frame or two of the wearer's own reply `other`. The utterances had reached Whisper at all only because the ambient gate already used the sane rule (`other` present and `self` absent).
- **Fix.** The prompt path uses the ambient rule: another speaker's words are the ones the host named `other` and never `self`. A reply the wearer tapped for, with `self` frames in it, is shown as it is heard and taken at the end; the review box is still the gate before anything is sent.
- Tests: `test_prompted_input.py` (`{self, other, unknown}` is now taken; `{other}` and `{unknown, other}` still refused), `test_orchestrator.py` (a stray `other` frame in a `self` reply keeps the words on the box); prompted/messages suites 235 passed, mypy clean. **Mac only: restart JOAT.** Not yet verified on physical glasses.
- Correction: this entry first called background state "not a factor here" after a grep, without investigating. It was a factor — see the entry above (a replaced copy that never came back, and a mic the host stopped while the plugin still believed it open).

## 2026-10-12 — Build JOAT 0.1.124

- Packed from `2b9571e` (clean tree) with `scripts/pack.sh`, the maintainer's own manifest and the MacBook-first, Mac Mini-second private WSS candidates (2 validated). Version guard: 0.1.124 above the last recorded build (0.1.123); app.json, package.json and package-lock.json all 0.1.124. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1195 tests passed, typecheck clean.
- Canonical `apps/hub-plugin/joat.ehpk`: 175753 bytes, SHA-256 `79345e6e4f3c20792e632fc6b7aa4fa43a99b13ce8393e96a7d99b72144f1ac1`. Adds to 0.1.123 the News tab fix below (`117c9bc`). Plugin only — no Mac restart needed for this one. Not uploaded to Even Hub, not installed, not verified on physical glasses.
- Note: this package is 9 KB smaller than the committed 0.1.123 one although a vite build of `ae1a7d8` (552.15 kB JS) matches this build (552.25 kB); the 0.1.123 package was packed before its commit and may hold more than that revision.

## 2026-10-12 — The News tab opens again: a list row could leave the width cut one byte over the firmware cap

Fixes News list rows exceeding the 63-byte firmware cap by 1 byte after pixel-width ellipsis truncation, which prevented the News page from opening on tap.

- **Cause.** `fitListRow` clamped a row to the firmware's 63 bytes and *then* cut it to the row's pixel width; `pxTruncate` appends `...` (three bytes) after it cuts, so a 63-byte headline came back at 64. One such row makes the firmware refuse the whole `rebuildPageContainer` — silently; the glass stays on the overview. Any News push with two headlines carrying a `…`, a `·` source separator and an accent did it (`[page-rejected] … row 10 64B > 63B; row 16 64B > 63B` in the plugin console). Pomodoro rows are short ASCII and never reached the cap.
- **Fix.** The byte cap has the last word: the budget steps down until the width-cut row measures ≤ 63 bytes (`HudPageBuilder.fitListRow`). Every firmware list goes through it — Messages threads and Search rows included.
- Reproduced and verified in the official simulator with the wearer's real feeds (`scripts/simulator_news_tab_probe.py`, new: a loopback brain that fetches `news.json` and pushes the tab as `publish_all` does, then swipes to News and taps): before, N1/N2/N3 frames identical after the tap; after, the NEWS list paints with `tap: open · long tap: refresh · 2x tap: back`. Not verified on physical glasses.
- Tests: `HudPageBuilder.test.ts` with the three rows that failed; plugin 1195 passed, typecheck clean. **Plugin only: a new package.**
- Also: `scripts/simulator_messages_voice_reply.py --retry-words` makes the second take after a 2x-tap retry say something else, so the retry is visibly a retry.

## 2026-10-11 — Build JOAT 0.1.123

- Packed from `ae1a7d8` with `scripts/pack.sh`, the maintainer's own manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates (2 validated). The version guard confirmed 0.1.123 above the last recorded build (0.1.122); app.json, package.json and package-lock.json all 0.1.123. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1194 tests passed, typecheck clean. Mac 3557 passed, 1 deselected (the known `test_whisper_cold_start` failure); 1 failed, `test_message_tools.py::test_controller_review_limit_remains_a_truthful_failed_status`, from FEAT-001's `e675ec2` (the Messages review limit), not these changes.
- Canonical `apps/hub-plugin/joat.ehpk`: 184915 bytes, SHA-256 `ce84d7695b40f78335725f374cf46743d0a67158df08480af68efd4b2929c642`. Adds to 0.1.122 QuickNotes matched to Even's QuickNote and Conversate, QuickNotes on the overview's rail with Search in the menu, and FEAT-001's long-draft review and scrolling thread. **The Mac side changed too: restart JOAT from this revision** (migration 16 runs on start). Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-11 — QuickNotes is on the overview's rail; Search moves into the menu

Moves QuickNotes onto the overview rail for direct access and moves Search into the capability menu.

- **The rail is NOTIFICATIONS, NEWS, POMODORO, QUICKNOTES** (`PANEL_TABS`). The QuickNotes tab shows the newest notes, age first, as Even's QuickNote widget on its Dashboard does; a tap opens QuickNotes ("+ Create new note" at the top). The Mac pushes the tab (`QuickNotesPanel`, `hud.panel` tab `quicknotes`) on connect, after every save, and after a summary renames a note.
- **The menu's fifth row is Search** where QuickNotes was (`CAPABILITIES`). It opens the Search page from the rows the Mac last pushed; "JOAT, look up …" still opens it by voice. Search keeps its content push (`PANEL_CONTENT_TABS`), it is only off the rail.
- QuickNotes pages from the Mac are still recognised now that QuickNotes is not a menu row (`adoptVoiceGeneration`); a voice "open quick notes" and the tab's tap reach the same page.
- Verified in the official simulator: the rail with QUICKNOTES, the tab's newest notes, its tap opening the QuickNotes list, Back to the overview, the menu with Search in fifth place, and Search's page. Not verified on physical glasses.
- Tests: plugin 1194 passed, typecheck clean; Mac tests for the tab (`test_quicknotes_panel.py`, 2 new) and the panel wire. **Both sides change: a new package and a JOAT restart.**

## 2026-10-11 — QuickNotes matches Even's QuickNote and Conversate: no speaker labels, one "+ Create new note", Save / Discard / Cancel, Undo last input

Aligns QuickNotes with Even's QuickNote and Conversate design: removes synthetic "You:" / "Them:" speaker labels derived from `speakerRole` and unifies note creation, exit prompts, and undo controls.

- **No speaker labels anywhere**: the open note, the note's page, the Apple Notes copy and the summary prompt. Migration 16 drops `voice_note_segment_speakers` (it had no rows) and nothing reads or writes a speaker.
- **One row, "+ Create new note"** (Even's QuickNote list), in place of "● Record" and "✎ Quick note · hold to talk" (✎ is not in the firmware font and drew as a blank). The note it opens, titled **QUICKNOTES · NEW NOTE**, takes both: hold the touchpad to talk a paragraph (QuickNote), tap to record continuously (Conversate), tap again to pause. The "Save to Apple Notes" row is gone from the glasses; saving stays automatic, and "stop saving notes to Apple Notes" still turns it off.
- **2x tap: Save / Discard / Cancel**, Even's exit prompt word for word. Discard leaves at once (no second question); Cancel or 2x tap returns to the note. A save shows itself on the list with no band (a note with gaps says "· gaps" on its row); the band had taken the next swipe. A spoken discard still asks again.
- **Undo last input** is the first row of the glasses' menu (tap, then long press — Even's in-feature menu gesture) while a note is open. It removes the newest paragraph, including words still being transcribed; the audio stays accounted for (`voice_note_undone_spans`, migration 16), so an undo never turns a note into one with gaps. A press that opens that menu no longer talks into the note: the hold is cancelled and its audio not kept.
- **Words flow like Conversate**: a continuous stretch of speech is one paragraph (the pause detector's cuts are not new lines; lines no longer start mid-sentence); a pause, a hold, or a whole chunk of silence starts the next. The page fills from the newest line (`paginateFromEnd`), with one footer line: no page counter, "swipe: page" when there are earlier words.
- **The REC timer ticks once a second**, on a steady clock timed to each second, instead of every 5 s from audio bytes (it jumped). Measured in the simulator: 00:01 to 00:12, one update each, 1.00 s apart.
- The note's page says "[recording gap]" instead of an internal code, and its tap hint is "tap: reminders".
- Verified in the official simulator, screen by screen: the list, the new note, recording, paused, holding, the flowing paragraph, swiping back, the no-audio notice, the glasses menu with Undo last input first, Undo, the exit prompt and its Cancel, Save, the list with the generated title, the note's page and its reminders tap. **Not verified on physical glasses.**
- Tests: Mac unit tests for the store (undo, extend), the recorder (paragraphs, undo, cancelled hold, timer, discard) and the pages; plugin 1192 passed, typecheck clean. **Both sides change: a new package and a JOAT restart (migration 16).**

## 2026-10-11 — Build JOAT 0.1.122

- Packed from `761b2a2` with `scripts/pack.sh`, the maintainer's own manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates (2 validated). The version guard confirmed 0.1.122 above the last recorded build (0.1.121); app.json, package.json and package-lock.json all 0.1.122. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1190 tests passed, typecheck clean. Mac 3546 passed, 1 deselected (the known `test_whisper_cold_start` failure).
- Canonical `apps/hub-plugin/joat.ehpk`: 184309 bytes, SHA-256 `3f6e8d526c7e85f5da53c4e060db37dd47e53d43d7bccf40c71c9f163069b33e`. Adds to 0.1.121 QuickNotes as one note (FEAT-002: live words, hold to talk, title and summary, Apple Notes copy) and reply by voice's tap to review / tap to send / 2x tap to retry with ▷ ▶ × in the thread (FEAT-001). **The Mac side changed too: restart JOAT from this revision** (migration 15 runs on start). Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-11 — QuickNotes is one note: words while you talk, hold to talk, a title and summary, and a copy in Apple Notes

Combines continuous Conversate-style live transcription and QuickNote push-to-talk into one surface with automatic summaries and Apple Notes sync (`docs/features/FEAT-002-quicknotes-conversate/`).

- **One place to start a note** (`capabilities._NOTE_CONTROLS`). The QuickNotes list opens with **● Record** (keeps listening, like Conversate) and **✎ Quick note · hold to talk** (like QuickNote). An open note is its live words: each new line is written into the page in place (`HudPresenter.updateLiveTextPage`, two `textContainerUpgrade` calls, no page rebuild), with the clock and the gestures in the hint row.
- **Words appear while you talk** (`VoiceNoteCaptureController`). Audio is still written to the journal first. It is cut at each pause (`RmsVad`) or at 15 s, and each piece is transcribed in the background by a stateless whisper call (`SttEngine.transcribe_once`, `WhisperServeEngine.transcribe_once`), so a note never shares whisper's streaming buffer with voice commands. At the end, any piece not yet transcribed is done then and retried once; what still fails is kept as a marked recording gap, not dropped. Long silences count as covered (`voice_note_quiet_spans`), so a quiet stretch is not a gap.
- **You / Them.** A piece is labelled from the SDK's `speakerRole` only when its voiced frames agree. The SDK calls this an app algorithm result, not an identity, so mixed or unknown pieces carry no label.
- **Gestures.** Tap: pause / keep recording. Hold the touchpad: talk (from the quick-note screen or a paused note; the 1.2 s before the press is kept), let go: done with that paragraph. 2x tap: finish, then **Save note**, **Keep recording** or **Discard note** (discard asks again). A note with no audio at all is discarded on finish.
- **No audio from the mic.** With no frames for 1.5 s the mic is reopened once; after 3 s the note says "No audio from the mic — check the glasses."
- **Title and summary** (`note_insights`). The local model writes a title (replacing "Note HH:MM"), Overview, Key points and Next steps; the open list repaints with the title. A note's page shows OVERVIEW, KEY POINTS, NEXT STEPS and TRANSCRIPT; tap there makes each next step a due-tomorrow reminder. If the summary fails, the note is still copied to Apple Notes with its transcript.
- **A copy in Apple Notes** (`notes_sync`, `notes_export`). Every saved note, spoken or dictated, is written to Apple Notes in the "JOAT" folder; a later save updates the same note (`note_exports` holds its Notes id). The body is the date, Overview, Key points, Next steps (☐) and the transcript with You:/Them: and [recording gap]. A note with no words is not sent. Turn it off with the **Save to Apple Notes** row or by saying "stop saving notes to Apple Notes" ("save notes to Apple Notes" turns it back on); the choice is kept in `notes.json`. macOS asks once to let JOAT control Notes.
- **Store:** migration 15 (`live_quicknote_transcription`) adds `voice_note_quiet_spans`, `voice_note_segment_speakers` and `note_exports`; CREATE only.
- Verified in the simulator with real whisper: list → Record → words appear in place (no rebuilds) → tap pauses → a hold adds a paragraph → 2x tap → Save → the list shows the generated title ("Delivery and van booking"). The Apple Notes body was checked through a stand-in writer. **Not verified on physical glasses, and no real Apple Notes write yet** (FEAT-002 T7).
- Tests: Mac 3546 passed, 1 deselected (the known `test_whisper_cold_start` failure); 44 new across capture, store, audio router, whisper, insights, export and sync, and the QuickNotes page tests rewritten. Plugin: 9 new (`EventRouter`, `CapabilityMenu`, `HudPresenter`); see the build entry. mypy's one error (`reply_engine.py` unused ignore) predates this. **Both sides change: a new package and a JOAT restart.**

## 2026-10-11 — Reply by voice: tap to review, tap to send, 2x tap to retry; the thread shows ▷ ▶ ×

Streamlines voice reply into a two-state modal (`tap: review | 2x: cancel` while listening, `tap: confirm | 2x: retry` on review) and returns directly to the thread with inline `▷` / `▶` / `×` delivery indicators.

- **Listening box: tap = review now** (`EventRouter.handlePromptInput`, `PromptedInputController.commit`). The plugin names the acknowledged question in the existing `input` uplink (`containerName` `prompt-commit`, `selectedName` its ID; no new wire kind). The Mac closes that question's utterance at once (`RmsVad.finish_now`, `VoiceOrchestrator.commit_prompt_answer`) and runs it through the same path a quiet boundary would, so every eligibility check still applies. A tap before any speech, or naming another question, changes nothing; silence still reviews as before. **2x tap = cancel**, as before.
- **Review box: tap = send, 2x tap = retry** (`EventRouter`). Retry is the existing `message-retry` input: the Mac re-asks and the listening box returns. Long tap and swipe do nothing on a draft. A gate with no retry (calendar, reminder, terminal) keeps 2x tap and swipe as its refusal.
- **The thread after a tap-send** (`capabilities.show_reply_status`, `MessageUIController.on_reply_status`, `MessageEntry.send_status`): the message is the newest row at once with **▷** while Messages has it, **▶** once Messages records it (the real row replaces the drawn one), **×** if Messages refused it; an uncertain outcome keeps ▷. The glyph follows the newest own row only; older rows carry none. Check marks do not exist in the firmware font (Dingbats are absent and unsupported glyphs vanish), so the glyphs come from the geometric set the HUD already uses.
- **No status text page.** A finished `message.ui` receipt of any state (`sent`, `cancelled`, `failed`, `uncertain`) paints nothing on the plugin (`CapabilityMenu.presentMessageUI`); the "MESSAGES · FAILED" reader page is gone. The thread is the receipt.
- Not verified on physical glasses: that is FEAT-001 T4 (`docs/features/FEAT-001-messages-voice-reply/tasks.md`), with `scripts/reply_trace.py` reading the run.
- Tests: plugin 1181 passed, typecheck clean (5 rewritten, 4 new, 2 retired with the page). Mac: see the suite line in the next entry; mypy's one error (`reply_engine.py` unused ignore) predates this. **Both sides change: a new package and a JOAT restart.**

## 2026-10-11 — The link's 15 s death loop is named in brain.log; one command says where a spoken reply stopped

Traces why spoken replies failed to display words during a 15.1 s WebSocket reconnect loop (`1005` close code): the plugin's 15 s downlink watchdog (`MacLink.downlinkHandlerTimeoutMs`) was expiring the socket when a header or Home-column image write hung on the glasses, cancelling any open reply prompt. `HudVisualSurface` fixes the hung write in 0.1.121 below; this change makes watchdog-loop drops self-describing in `brain.log` and adds `scripts/reply_trace.py`.

- **The watchdog loop is named** (`hub_gateway._note_link_end`): three authenticated connections in a row that end 15 ± 3 s after connecting with code 1005 or 1006 log one WARNING — "the plugin's downlink watchdog is firing because one Mac->plugin message is not handled in time on the glasses … every open question is cancelled by each reconnect" — and point at the trace script. A link that then holds closes the streak (an INFO says so), and a new loop is named again. Clean closes (1000), suspensions (1001), long links and dials that never settled are not called a loop.
- **`scripts/reply_trace.py`** reads `brain.log` and `g2-diagnostics.json` and prints, for the recent connections, how long each lived and how it ended (watchdog-shaped ones flagged), the Messages reply stages (`selected → reply-prompt-armed → review-requested → confirmation-requested → submitted`), each step the question reached on the glasses (`prompt_stage`), why spoken answers were turned down, consent decisions, voice-path counts, and a verdict in plain words. It prints durations, stage names, reason codes and counts only — no bodies, recipients, question text, transcripts or serials. `--since 2h`, `--connections N`. Against the real log for 2026-10-08 09:10 onward: "2291 of 2311 connections died about 15 s after connecting … no spoken reply can survive."
- **`docs/features/FEAT-001-messages-voice-reply/`**: the as-built reply/compose flow with its contracts, the evidence above, and the hardware acceptance run (`tasks.md` T4) that decides whether the feature is done once the link holds.
- Tests: `tests/unit/test_hub_gateway_watchdog_loop.py` (8 new), `tests/unit/test_reply_trace.py` (7 new); the new warning fails to appear without the fix. Mac suite and mypy: see the next build entry. **Mac-only: restart JOAT** for the warning; the script runs on the log as it is. No new package.

## 2026-10-11 — Build JOAT 0.1.121

- Packed from `0e722df` with `scripts/pack.sh`, the maintainer's own manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates (2 validated). The version guard confirmed 0.1.121 above the last recorded build (0.1.120); app.json, package.json and package-lock.json all 0.1.121. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1175 tests passed.
- Canonical `apps/hub-plugin/joat.ehpk`: 183855 bytes, SHA-256 `7fb61eb5aa04e7f2d080bd36434ad533204c09de93aa2cc0bee9a0d59b62d500`. Adds to 0.1.120 the end of the 15 s link drops (a stuck header or column image switches both to firmware text and frees the lane) and page-part failures in the diagnostics. **The Mac side changed too: restart JOAT from this revision** (diagnostics toggle, backlog freezes, quieter logs, page-part diagnostics). Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-11 — A stuck header or column image no longer drops the Mac link; the log errors, fixed

The wearer asked for every error in the current logs to be fixed. Each one below was traced in `brain.log` and to the code, and reproduced where it could be.

- **Fixed: one stuck image write dropped the Mac link every 15 s** (about 2,350 drops on 2026-10-08/09, each 15.1 s after connecting). On the glasses, one image write to a header half or a Home column tile never got an answer. Every later page change waited up to 15 s for it (`withPageTransition`), MacLink's 15 s downlink watchdog dropped the link on the same beat, and the plugin reconnected into the same wait. Reproduced in the simulator by holding the first header write: on 0.1.120 the glasses never drew again. Now (`HudVisualSurface`):
  - A header or column write gives up after 6 s, not 15 s (`PAGE_PART_UPLOAD_TIMEOUT_MS`).
  - When one hangs, or three fail in a row, header and column images are switched off for the session. The page on the glass is redrawn with both as firmware text (Home's `>` rail and text header, as in 0.1.104), and the hung write no longer holds the lane, because its container is gone from the page.
  - A full-tile write known to be hung is refused at once instead of waited on again. The one-writer rule stays.
  - A page change waits at most 8 s for image writes (`PAGE_TRANSITION_IDLE_MS`), inside the watchdog. A refused page leaves the link up.
  - Verified in the simulator with the first header write held forever: after 6 s Home came back in firmware text, swipes moved the rail and pane, a tap opened News, 2x tap went back, all on the one connection.
- **Fixed: the Mac threw away the glasses' report of that stuck write** (1,711 times: "g2 diagnostic containerId is invalid"). The diagnostic schema only took the four full-frame tiles. It now also takes the header halves (16, 17) and Home's column (18, 19), in `protocol.py` and `G2DiagnosticTrace.ts` together. The plugin now reports a failed page-part send too, not only one that threw. With "Record G2 diagnostics" on, the next hang names its container.
- **Fixed: JOAT froze for up to 12 s at a time** (368 "event loop stalled" warnings). The notification mux drained its backlog without ever yielding (`notification_mux._deliver`); each item is a few synchronous SQLite writes. At startup on 2026-10-11, 1,130 old banners froze JOAT for 9.7 s, and the 5–12 s freezes just after each glasses connection were the same drain. It now yields between items. The 2–4 s at startup while the local model loads is the model's own thread holding Python's lock, and is left alone.
- **Fixed: a cursor reset re-read the whole banner feed.** On 2026-10-11 the Accessibility feed was read again from the start: 1,130 banners from 46 days back. They were held back as too stale, but each was processed. After a reset (`jsonl_source.read_new`), lines already in the file at the last read are skipped, and the reason (shrank, replaced, rewritten) is logged.
- **Quieter logs.** "dropping … no plugin connected" is logged once per disconnected spell, with a count when the glasses return; it had logged ~1,500 times. The banner watcher's stale heartbeat while the Mac sleeps is logged once per spell, with "recovered after N failed polls"; it had logged once a second.
- **News names a dead feed.** "feed unavailable category=custom (HTTPError)" had been logged about 1,900 times since 2026-09-05 without naming the feed. It now logs the host and status, never the path. The dead one was `thedecisionlab.com/feed/` (HTTP 404; the site no longer has a feed), and it was removed from this Mac's `news.json`; the other three custom feeds work.
- Not code: the Mac mini's OpenClaw gateway answered HTTP 502 on 2026-10-08/09, and the Mini has been offline in Tailscale since. JOAT logs this once at startup. The glasses can reach this MacBook again: Tailscale is back on and the `/glasses` route answers.
- Tests: plugin 1175 passed, typecheck clean, 5 new tests. Mac 3481 passed, 7 new tests; `test_whisper_cold_start::test_shutdown_after_foreground_timeout_reaps_child_and_stops_retries` fails the same way without these changes. mypy reports one unused `type: ignore` in `reply_engine.py`, a file this change does not touch. Version 0.1.121. **Both sides change: a new package and a JOAT restart.**

## 2026-10-11 — "Record G2 diagnostics" off now means off

Fixes `tile_upload` trace lines and `g2-diagnostics.json` continuing to be written when `G2DiagnosticsEnabled` was turned off (`--g2-diagnostics` unset).

- **Fixed: the engine armed the G2 trace itself** (`joat/__main__.py`, since 2026-09-08). A second after the glasses connected, it built its own diagnostic store, so the toggle did nothing. That store wrote to the same `g2-diagnostics.json` and told the plugin to stream every event, again on every reconnect. That startup step is gone. The only trace is the one `--g2-diagnostics` (the toggle) or the location probe asks for, armed by the gateway as before.
- With the toggle off, the glasses send no trace and `brain.log` has no `tile_upload` lines. The voice latency record no longer gets its `painted` receipt unless the toggle is on.
- Tests: Mac 3465 passed. One failure, `test_whisper_cold_start::test_shutdown_after_foreground_timeout_reaps_child_and_stops_retries`, fails the same way without this change. A new test fails without the fix. **Mac-only: restart JOAT** for it to apply; no new package.

## 2026-10-11 — Build JOAT 0.1.120

- Packed from `cdb9e0e` with `scripts/pack.sh`, the maintainer's own manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates (2 validated). The version guard confirmed 0.1.120 above the last recorded build (0.1.119); app.json, package.json and package-lock.json all 0.1.120. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1170 tests passed.
- Canonical `apps/hub-plugin/joat.ehpk`: 183458 bytes, SHA-256 `99c6d86276c3efce3a517c88d6b3cccbcf7ddfe1074fc9acdb893622365a61c4`. Adds to 0.1.119 Home's tabs swiping and opening with a notification on Home, the Notifications tab's unread list and the lit tab's underline. Plugin-only on top of 0.1.119 (whose brain restart still applies). Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-11 — Home's tabs swipe and open again, with a notification on Home

The wearer, on 0.1.119 on the glasses: Home's tabs no longer switched the right pane on a swipe, and a tap no longer opened the tab. The gestures did reach the plugin. A notification was blocking them, and on the glasses the Mac sends notifications all the time. The simulator runs had none, which is why they passed.

- **Fixed: a notification's copy pinned Home's pane.** After its card closed (5 s), the notification stayed in Home's main slot, and Home shows that slot ahead of the lit tab. A swipe moved the lit tab, but the pane kept the notification. With Home in firmware text a notification is now the card's to show, never the pane's (`composeOverviewText`, `HudPresenter.baseVisualFrame`). The Notifications tab lists the unread cards instead, newest first, with the app in the label column and "tap: read" below.
- **Fixed: every tap on Home was swallowed while that notification was there.** A Home tap first dismisses a visible Home notification, but only one marked as drawn. Only the old image Home marked it; the text Home (0.1.103) never did. So every tap was consumed as "not drawn yet", nothing was dismissed, and the lit tab never opened. Text Home now has no Home notification for a tap to take (`HudPresenter.hasVisibleNotification`), so a tap opens the lit tab.
- **A swipe always shows the tab it lands on.** Other Main content (a calendar line, a Mac message) still heads the pane when it arrives. From the wearer's next swipe the pane follows the rail, even a swipe at either end of the rail, until new Main content arrives (`homePaneFollowsRail`).
- **The lit tab is underlined on the rail** (`homeColumnLayout`). Since 0.1.114 the lit and unlit tab labels rendered at the same brightness in the simulator, so the rail looked the same after a swipe. The underline sits under the lit label, inside the column's margins, and moves with each swipe.
- Verified in the official simulator, with the Mac's connect sequence, two unread cards and one new notification whose card closed. Down, down and up each changed the pane, and the underline followed. A tap opened News; 2x tap went back to Home. Up showed the Notifications tab's unread list. A tap opened the newest card, a tap read it and showed the next, and 2x tap closed it, with one card left on the list. Before the fix, the same run redrew nothing and opened nothing. Not verified on physical glasses.
- Tests: plugin 1170 passed; typecheck clean. Five new tests fail without the fix. Plugin-only: no brain restart.

## 2026-10-08 — Build JOAT 0.1.119

- Packed from `bad0b34` with `scripts/pack.sh`, the maintainer's own manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. The version guard confirmed 0.1.119 above the last recorded build (0.1.118); app.json, package.json and package-lock.json all 0.1.119. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1166 tests passed.
- Canonical `apps/hub-plugin/joat.ehpk`: 174041 bytes, SHA-256 `523535bc2900d9ebf993d2b4c00646bf68903e67ef247350eeca380a3919a0a9`. Adds to 0.1.118 the consent card after a spoken Messages reply and the tap-to-speak trace. **The Mac side changed too: restart the brain from this revision**, or the new `prompt_stage` diagnostics are refused and the refusal reasons are not logged. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-08 — A spoken Messages reply reaches its consent card; tap-to-speak is traced on the glasses

Verifies Brainstorm and Messages tap-to-speak end to end in the official simulator against the Mac's prompt runtime (`PromptedSpeechRuntime`, `SessionManager`, `MessageUIController`, `MessageReplyController`), fixing a page-replacement race that cancelled spoken Messages replies before the consent card could appear.

- **Fixed: a spoken Messages reply was cancelled the moment it was heard.** The words answered the question and the Mac asked for consent, but the consent card never showed. When an answer arrives, the hub starts restoring the thread under the closing question. The Mac's confirmation landed while that restore was still running. The page guard refuses a second page replacement while one runs, so it refused the consent card, the hub declined, and the Mac cancelled the draft. The consent card now waits for a page replacement already in flight, then replaces it (`HudPresenter.renderConfirmation`, `pageReplacement`). Verified in the simulator: "JOAT: What do you want to say back to Taylor? / You: On my way, see you at 4:30." on the glass, `confirmation-visible`. A test reproduces the order and fails without the fix.
- **Brainstorm with the stand-in microphone works end to end**, and was not changed. The ready frame's tap reached the Mac as `begin_topic_capture`. The question was acknowledged in 0.1 s. The spoken topic was taken, and the run's first frame painted. What the simulator cannot run is the real glasses microphone and the Mac turning that audio into the answer, so that part is unverified.
- **Tap-to-speak is now traced, so the glasses show where it stops.** The hub records each step of a question in the G2 diagnostics, as a new `prompt_stage` event: presented, painted or paint refused, mic ready or mic refused, acknowledged or acknowledgement refused, retired. Each event names its owner (messages, brainstorm, terminal, capability). The event is in `protocol.py` and `types.ts` (`G2DiagnosticTrace.ts`) together, and the Mac validates it. The Mac logs why it turned down a spoken answer while a question waited: `prompted answer not taken owner=… reason=…`, with no words. The reasons are not acknowledged, owner gone, mixed audio, other mic, other connection, audio before the question, other speaker, or started before the question.
- Tests: plugin 1166 passed; typecheck and build clean. Mac 3462 passed, 1 skipped; the container's IPv6 test fails as before. mypy clean. Both sides change: a new package and a brain restart.

## 2026-10-07 — Build JOAT 0.1.118

- Packed from `ebf86e5` with `scripts/pack.sh`, the maintainer's own manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. The version guard confirmed 0.1.118 above the last recorded build (0.1.117); app.json, package.json and package-lock.json all 0.1.118. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1164 tests passed.
- Canonical `apps/hub-plugin/joat.ehpk`: 173816 bytes, SHA-256 `5a8321c7e36970393b0321ffea1a7a30337fdc2062e0cfc3fe745c5c96a38857`. Adds to 0.1.117 the gesture-free JOAT box, the one gesture template and messages hanging under the name. Plugin-only on top of 0.1.117 (whose brain restart still applies). Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-07 — No gestures in JOAT's box; one gesture template; messages hang under the name

Unifies gesture terminology across all HUD surfaces, removes redundant gesture footers from JOAT's conversation box, and indents wrapped message lines beneath the first word after `Sender: `.

- **No gesture notes in JOAT's box** (`AssistantConversationSurface.ts`, `HudPresenter.ts`). The Messages reply card no longer has "listening · double-click: cancel" or "tap: send · long-press: retry · double-click: cancel". While waiting for speech, the working glyph moves on the user's row ("You: ■"), and incoming words replace it in place. The continuation question is just "Continue?".
- **One gesture template on every page** (`GesturePolicy.formatHint`): the words are tap, swipe, long tap and 2x tap. Double-click, long-press, hold and left x2 are gone. The order is always tap · swipe · long tap · 2x tap; a page count or a spoken command keeps its place. Every hint passes through it at the two places hints are written (`HudPageBuilder.region` and `HudPresenter.upgradeNativeText`), so a Mac's older wording becomes the template too. The plugin's own hint strings now use the template's words.
- **Wrapped rows hang under the first word** (`FirmwareText.hangingLines`, `paginateHanging`): on the Messages pages, the notification card and in JOAT's box, a message's wrapped rows start where its words start after "Name: ". The rows are set as explicit lines with leading spaces, which the firmware keeps after a line break; each line is measured to fit, so the firmware never re-wraps one.
- Verified in the official simulator: a long thread with every wrapped row under its first word, the reply card's glyph on the wearer's row with no footer, its consent card with no footer, the card's message hanging under "Sam: ", a long answer hanging under "JOAT: ", and Home's hints in the template. Not verified on physical glasses.
- Tests: plugin 1164 passed; typecheck and build clean. Plugin-only: no brain restart.

## 2026-10-07 — Build JOAT 0.1.117

- Packed from `e2e8bca` with `scripts/pack.sh`, the maintainer's own manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. The version guard confirmed 0.1.117 above the last recorded build (0.1.116); app.json, package.json and package-lock.json all 0.1.117. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1164 tests passed.
- Canonical `apps/hub-plugin/joat.ehpk`: 172945 bytes, SHA-256 `1a9a20b5e19925fa0bbb9992217190049a92709f06881e13762507d245b84e89`. Adds to 0.1.116 the speaker's name in front of their words in JOAT's box and on the notification card. **The Mac side changed too: restart the brain from this revision**, or a bare "JOAT" still shows "JOAT" above "Yes?". Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-07 — The speaker's name opens their words, on the same row

Places the speaker's name inline with the opening words (`Name: message`) on both notification cards and JOAT's conversation box instead of spending a separate header row.

- **JOAT's box** (`AssistantConversationSurface.ts`): each turn's first row opens with its speaker's name, "You:" or "JOAT:", and the words wrap full width beneath. JOAT's name is dim and its words bright (the two layers share the row; the bright words start a typed space past the name). The working glyph stands on JOAT's row ("JOAT: ■ □"). The blank row between exchanges is gone: the names already part the turns. The box now starts at one row.
- **A bare "JOAT"** (`orchestrator.py`): the name alone is not a question, so the box shows only "JOAT: Yes?". Before, the spoken wake word took a row above the answer and read as a name label. `_greet_assistant_conversation` adds JOAT's turn without replacing an earlier answer when the conversation is still open.
- **The notification card** (`NotificationCard.ts`): "Sam: Heading to the coffee shop…" under the app line, not the sender on a row of its own.
- **The Messages reply card** gets the same rows: "JOAT: What do you want to say back to Casey?", then "You: …" as the user speaks, with no blank row between.
- Verified in the official simulator: the wake answer as one row over Home, a question and its answer in two rows, the working glyph on JOAT's row, a long answer wrapping beneath the name, the card, and the reply card. Not verified on physical glasses.
- Tests: plugin 1164 passed; typecheck and build clean. Mac 3460 passed, 1 skipped; the container's IPv6 test fails as before. mypy clean. Both sides change: a new package and a brain restart.

## 2026-10-07 — Build JOAT 0.1.116

- Packed from `3375c92` with `scripts/pack.sh`, the maintainer's own manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. The version guard confirmed 0.1.116 above the last recorded build (0.1.115); app.json, package.json and package-lock.json all 0.1.116. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1163 tests passed.
- Canonical `apps/hub-plugin/joat.ehpk`: 172969 bytes, SHA-256 `df1bb8ae6294f1985e861554c273878633edd22df3888ae59bbc7daa3e45231d`. Adds to 0.1.115 the notification card and the IMU off at boot. **The Mac side changed too: restart the brain from this revision**, or the card only ever shows the Mac's short line. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-07 — Notifications pop up as Even's card, over every page

The wearer asked for notifications the way Even shows its own: a card that pops up over the page, a tap that shows the next unread card, and a double tap that dismisses. Studied first, from Even's support article and its images and from frame-by-frame extracts of Even's G2 videos (recorded in `docs/EVEN_RESEARCH_QUESTIONS.md`).

- **The card** (`NotificationCard.ts`) is laid out as Even's: a bordered card near the top, the app and the age ("Now", "7m ago") with its place among the unread ("1/3") on the first line, then the sender and the whole message. The page stays beneath it: everything under the card is left out, the rest is dimmed, Home's column is repainted dimmed below the card, and a list beneath loses its selection box. It is firmware text, so the next card is one page write.
- **Gestures, the wearer's ruling:** a tap marks the card read on the Mac and shows the next unread one, or closes the card after the last; a double tap closes it and leaves it unread; swipes stay with the card; long press still opens the mic.
- **When it pops up:** over every page, except while a decision, a question or JOAT's conversation owns the glass. Such a card waits as unread. A card that popped up on its own closes after 5 s, and each tap restarts that. Even's app shows 3 s, but this card holds the whole message.
- **Home's Notifications tab** opens the newest unread card, which stays until the wearer is done. The list page remains for when nothing is unread, and for "show notifications".
- **New wire message `hud.notifications`** (`protocol.py` and `types.ts`, changed together): every unread JOAT card, newest first, at most ten, each with its id, app, sender, whole message (at most 600 characters), time and urgency. The Mac republishes it from the same loop as the unread count, so a tap reaches the next card without a round trip. Until the list arrives, the card is drawn from the Mac's short line and redrawn whole when it does.
- **Not done: Even's slide between cards.** Even's 0.32 s vertical slide is its firmware's own animation. The SDK has no animation API, and faking it means sending every in-between frame as image tiles, about 104 ms per tile over Bluetooth.
- **The IMU no longer streams from boot.** It sent ten motion reports a second that nothing read, and filled the diagnostics ring: the wearer's export held 512 motion reports and no taps. Motion reports are also kept out of the diagnostics now.
- Verified in the official simulator: the card over Home and over the News list, tapping through three cards to Home, and a double tap back to the list. Also verified end to end with the Mac's real gateway: its list and pop drew the card, and each tap came back to the Mac as `notification-dismiss` for that card, in order. Not verified on physical glasses.
- Tests: plugin 1163 passed; typecheck and build clean. Mac 3460 passed, 1 skipped; the container's IPv6 test fails as before. mypy clean. Both sides change: a new package and a brain restart.

## 2026-10-07 — Build JOAT 0.1.115

- Packed from `db8f353` with `scripts/pack.sh`, the maintainer's own manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. The version guard confirmed 0.1.115 above the last recorded build (0.1.114); app.json, package.json and package-lock.json all 0.1.115. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1147 tests passed.
- Canonical `apps/hub-plugin/joat.ehpk`: 170954 bytes, SHA-256 `234590075706e5bb5bb6935738cd9b6ab06d1e4e994d9f1c1a4502b8a19b2225`. Adds to 0.1.114 JOAT's box over Home as Home is drawn. Plugin-only: no brain restart. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-07 — JOAT's box floats over Home as Home is drawn

The wearer reported that JOAT's box still looked like a page of its own: the Home behind it was not the current Home.

- **Cause, reproduced in the official simulator.** The box keeps the page it was called over beneath it. For Home, that underlay was rebuilt from Home's plain text layout. That layout has no image header and no left column, so the box sat over an older-looking Home: the date in the large firmware font at the very edge, no divider, no tab rail.
- **Now the underlay is Home as drawn** (`conversationUnderlay` in `HudPresenter.ts`): its text, its two column images, and the image header (from the box's own status line, as on every page). `overlayConversation` keeps a page's images beneath the box and cuts its text to end above the box. Before, it handled only all-text pages and all-image readers.
- **The column is repainted with nothing under the box** (`clipBottom` on `HomeColumnView`). A G2 box has no fill, so a rail row under it would shine through. Rows are left out whole, never cut through a glyph. When the box grows, the column is repainted with the new clip. When it closes, Home is rebuilt whole.
- **A safeguard in reader paragraphs** (`readerParagraphs`): a row wider than the Mac's 62-column reader slice now stands alone. Before, it was joined to the next row without a space. This showed up only with a probe's hand-written oversized rows; the Mac's reader slices are at most 62 columns.
- **Not changed: the News tab tap.** The wearer reported that a tap on the lit News tab does nothing. In the simulator it opened the News list in every sequence tried: from rest, with a notification on Home, after Back from the list and from an article, after closing JOAT's box, and connected to a live gateway with the panels probe. It was not reproduced, so nothing was changed for it.
- Verified in the official simulator: the box over Home with the column and header intact, rows under the box left out, the box growing, and Home whole again after closing. Not verified on physical glasses.
- Plugin 1147 passed; typecheck and build clean. Plugin-only.

## 2026-10-07 — Build JOAT 0.1.114

- Packed from `f6f18c3` with `scripts/pack.sh`, the maintainer's own manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. The version guard confirmed 0.1.114 above the last recorded build (0.1.113); app.json, package.json and package-lock.json all 0.1.114. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1142 tests passed.
- Canonical `apps/hub-plugin/joat.ehpk`: 170631 bytes, SHA-256 `5509a101283586cce01bba9d51ba8edbc33faed1156d6e8d84a04e29606db3b6`. Adds to 0.1.113 solid image words at 66% and readers as firmware text. Plugin-only: no brain restart. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-07 — Image words at 66% with every pixel fully lit; readers are firmware text

The wearer asked for the image fonts at 66%, reported the faded letters on every image surface, and ruled that more of the image text should be firmware text.

- **Every image word is drawn solid** (`crispGlyph` in `EvenFont.ts`). Each glyph is scaled once with an exact area average of the atlas pixels under each new pixel, then every pixel is cut to fully lit or dark at a 0.3 share. Even's strokes are 1 px wide, and a 1 px stroke at 66% split evenly over two pixels gives each a 0.33 share, so no stroke can vanish (pinned by a test). The canvas's own smoothed scaling lit only 21% of a word's pixels at full brightness. Its high-quality downscale, tried first, dropped the clock's colon and parts of W, E and H, as a capture showed.
- **66%** (`EVEN_FONT_SCALE`), up from 55%.
- **Brightness:** header words full (were 67%). Home's date, weather and unread full (were 80-90%). Unlit tabs at 179/255 (were 40%), the firmware's own dimmest text level.
- **Readers are firmware text pages** (the Messages thread, notes, notification detail, search result and the rest), as the article became in 0.1.113. `VITE_EVEN_FONT_PAGES=1` brings back the image readers. The Mac's 62-column slices are joined back into paragraphs (`readerParagraphs`) before the page is set, so a sentence is not broken into one slice per line.
- Still images: the header words, Home's left column, the Planning month and dashboard, and the live map.
- Verified in the official simulator: header words 100% at full brightness (55% build: 8%); every letter of "9:41", "Wed, Oct 7", "NEWS", "SEARCH" and "Focus 15m" intact; the thread as firmware text, paging in place. Not verified on physical glasses.
- Plugin 1142 passed; typecheck and build clean. Plugin-only.

## 2026-10-07 — Build JOAT 0.1.113

- Packed from `c7f7ddf` with `scripts/pack.sh`, the maintainer's own manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. The version guard confirmed 0.1.113 above the last recorded build (0.1.112); app.json, package.json and package-lock.json all 0.1.113. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1139 tests passed.
- Canonical `apps/hub-plugin/joat.ehpk`: 170101 bytes, SHA-256 `55b7c553c78d0819e3b8a7a420791eba43751f1c1063a7af141db4b56d10ab14`. Adds to 0.1.112 the News article as a firmware text page with 16 px side margins. Plugin-only: no brain restart. The package is now tracked in git (main stopped ignoring `*.ehpk`). Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-07 — A News article is firmware text again, 16 px clear of the sides

The wearer reported the open article drawn as an image and hard to read while scrolling, its letters partly faded.

- **Why it faded, measured.** The image readers draw Even's own glyphs at 55%. The glyph atlas cut from the simulator is one-bit, and 90% of its horizontal strokes are 1 px wide. A 1 px stroke at 55% covers about half a pixel, so the canvas spreads it over one or two pixels at partial brightness. A simulator capture of the 55% thread showed only 21% of its lit body pixels at full brightness, the rest over eight dimmer levels. The body text is also drawn at 82% brightness and the hint at 50%.
- **The article (`NEWS · ARTICLE`) is a firmware text page again** (`FIRMWARE_TEXT_READERS` in `HudPresenter.ts`). Every stroke is at full brightness, and a swipe pages it in place with one text upgrade instead of repainting image tiles. The Messages thread and the other readers stay 55% images.
- **A reader's firmware text page keeps 16 px from both sides** (`READER_TEXT_PAGE` in `HudPageBuilder.ts`). The page used the answer page's 4 px inset, so text touched the edge. Its pages are cut to the narrower 544 px, so the firmware never wraps a line past the page. Answer, navigation and Planning pages keep their width.
- Verified in the official simulator (`?joatVisual=panel-article`): the article as firmware text, no lit pixel within 16 px of either side below the header, and a swipe paging it in place. Not verified on physical glasses.
- Plugin 1139 passed; typecheck and build clean. Plugin-only: no brain restart.

## 2026-10-06 — Build JOAT 0.1.112

- Packed from `3b678f6` with `scripts/pack.sh`, the maintainer's own manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. The version guard confirmed 0.1.112 above the last recorded build (0.1.111); app.json, package.json and package-lock.json all 0.1.112. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1138 tests passed.
- Canonical `apps/hub-plugin/joat.ehpk`: 169963 bytes, SHA-256 `5b24290bc191aab2ab9a0180db1e80b3cdeddbe9034980b3252740c35eea4457`. Adds to 0.1.111 the two live-words fixes from the PR #17 review. **The Mac side changed too: restart the brain from this revision.** The package is gitignored (`*.ehpk`); this entry is its record. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-06 — Live words: a cancel waits for the card, and another speaker's words never show

A review of PR #17 found two defects in the live-transcription path, both confirmed in the code and fixed.

- **A cancel or a consent card could lose to a growing partial.** The words so far can grow the reply card, which is a page replacement, and the page guard refuses a second replacement while one runs. `dismissPromptQuestion` waited only for the card's first paint, so a cancel arriving mid-growth tried to restore the page beneath, was refused, and the cancelled card stayed on the glass; a consent card could be refused the same way. Both now wait for every paint the prompt owns (`settlePromptPaints`) before replacing the page. Pinned by a test that holds the partial's rebuild while a cancel arrives.
- **Another speaker's words could appear as the wearer's reply.** The partial publisher checked only the latest frame's prompt epoch and connection; a bystander talking for 0.9 s during an open prompt, with the host naming `speaker="other"`, was transcribed into the card. The final-transcript path already rejected such audio. The publisher now applies `dispatch`'s own eligibility over every buffered frame: no `other` speaker, the prompt's source, epoch and connection, nothing mixed.
- **Provisional words come off the card when the final does not answer the prompt:** a rejected non-wearer utterance, a final with no text, or a final `dispatch` refuses now sends an empty `prompt.partial`, which shrinks the card back to the question. Before, somebody else's words could stay until the next update.
- Tests: plugin 1138 passed, typecheck and build clean. Mac 3457 passed, 1 skipped; the container's IPv6 test fails as before. mypy clean. Both sides change: a new package and a brain restart.

## 2026-10-06 — Build JOAT 0.1.111

- Packed from `b80349d` with `scripts/pack.sh`, the maintainer's own manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. The version guard confirmed 0.1.111 above the last recorded build (0.1.110); app.json, package.json and package-lock.json all 0.1.111. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1137 tests passed.
- Canonical `apps/hub-plugin/joat.ehpk`: 169876 bytes, SHA-256 `c5313c7b0fa5a9f2bebc15aa3c3687a56cea7d6ffa6077bb7115ddbd976e7796`. Adds to 0.1.110 Home's label/value grid. Plugin-only since 0.1.109: a brain restarted for 0.1.109 needs no restart. The package is gitignored (`*.ehpk`): it lives only where `npm run pack` ran, and this entry is its record. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-06 — Home's pane is a label/value grid, as weather-even-g2 lays out its stats

The wearer pointed at weather-even-g2's right panel. Read from its source and captured live in the official simulator: its Today screen's right half is two native text containers side by side, a label column (feels, wind, humid, press, rise, set, uv) and a value column, with no border, icon or list. Two containers because the firmware font is proportional: columns line up only at fixed x, never as padded spaces in one container.

- **Home's pane is now that grid** (`OverviewText.ts`, `HudPageBuilder.overviewTextLayout`): a 96 px label column (`pane-l`, dim) beside the value column (`pane`, which stays `ContainerId.Main`, so replies still stream into it and it still holds the input).
  - Notifications: `NEXT` / `Calendar` / `Mail` beside their lines.
  - News: each headline's age (`12m`, `1h`) beside `headline · source`.
  - Pomodoro: `Timer` / `Left` / `Cycle` beside the block, minutes left and the cycle dots; the bar and the action rows span the value column.
  - Search: `Query` beside the question, then numbered results, so "open the second" names a row.
  - `NOTIFICATION` (116 px) and `LAST QUERY` (108 px) are wider than the column and show as `NOTICE` and `QUERY`; a boot title (`RECONNECTING`, 130 px) heads the value column as before.
- **A tab swipe is three in-place upgrades** (the rail, the labels, the values), never a rebuild; the presenter's in-place limit rose from two bands to three. A rebuild would also resend Home's four images.
- Dev fixture: `?joatVisual=panel-news&joatHomeRest=1` (and `panel-pomodoro`, `panel-search`) shows the lit tab's own card instead of the newest notification.
- Verified in the official simulator on all four tabs. Not verified on physical glasses.
- Plugin 1137 passed; typecheck clean. Plugin-only.

## 2026-10-06 — Build JOAT 0.1.110

- Packed from `a5b764b` with `scripts/pack.sh`, the maintainer's own manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. The version guard confirmed 0.1.110 above the last recorded build (0.1.109); app.json, package.json and package-lock.json all 0.1.110. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1137 tests passed.
- Canonical `apps/hub-plugin/joat.ehpk`: 169696 bytes, SHA-256 `8875ab7bd57c4e3fe0095148af4aab5e049c9627671db97831f484ecffb6b1df`. Adds to 0.1.109 Home's column in Even's font and the reader kept at 55% under JOAT's box. Plugin-only since 0.1.109: a brain restarted for 0.1.109 needs no restart. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-06 — Mixed rendering: Home's column in Even's font; the reader stays 55% under JOAT's box

The wearer asked for sources on the font limits, and they are now confirmed:
- Even's display docs say "a single LVGL font baked into firmware. No font selection, no size control" (hub.evenrealities.com/docs/build/display).
- App fonts are an open request, everything-evenhub#37.
- `TextContainerProperty` in SDK 0.0.16 has 14 fields and no font.

Every G2 plugin with other fonts draws that text as images; the fast ones mix the two (weather-even-g2, sandevistan, amap-hud). The wearer chose that mixed design: images in Even's font for what rarely changes, native text for what changes often.

- **Home's left column** (the date, weather, unread count and tab rail) is now two image tiles in Even's font at 55%, with icons (`HomeImages.ts`, `HudCanvasRenderer.renderHomeColumn`).
  - The right pane stays firmware text and keeps its in-place upgrade on every tab swipe; it now holds the page's input.
  - A swipe moves the pane first, then resends only the rail's tile (26 ms in the simulator; one image send on the glasses).
  - The weather or unread count resends the other tile. The clock is in the header, so nothing repaints on the minute.
  - `VITE_HOME_IMAGES=0` keeps the text column.
- **JOAT's box over a reader keeps the reader in Even's font.** The reply card used to rebuild the reader above it as a firmware text page, which jumped the thread back to the firmware's size. Now:
  - The reader's four tiles stay beneath the box (`ConversationOverlay` keeps an image underlay's tiles and drops its input layer and the box's status line).
  - The presenter repaints the reader with nothing below the box's top border (`clipBottom` on the reader frame).
  - A status change repaints those tiles.
  - Closing the box repaints the whole reader.
- **The image lane gained a strip kind** for page parts that are images: Home's column, alongside the header. These are never superseded by a full frame, and only changed tiles are sent.
- **Verified in the official simulator:** Home before and after, a tab swipe (rail tile only), and the reply box over the long thread with the thread at 55%. Not verified on physical glasses.
- Plugin 1137 passed; typecheck and build clean. Plugin-only.

## 2026-10-06 — Build JOAT 0.1.109

- Packed from `b33adaa` with `scripts/pack.sh`, the maintainer's own manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. The version guard confirmed 0.1.109 above the last recorded build (0.1.108); app.json, package.json and package-lock.json all 0.1.109. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1131 tests passed.
- Canonical `apps/hub-plugin/joat.ehpk`: 168065 bytes, SHA-256 `cf33ba02a80a1f2cacfb777635f2277ab849cc0c6af2081b3bf968b00ae0e304`. Adds to 0.1.108 Even's font at 55% and in the header, one-line list rows, your words in the reply card while you speak, and News taps that open the right story. **The Mac side changed too: restart the brain from this revision** (live words and the News fix are Mac-side). Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-06 — Your words while you reply; News taps open the right story; smaller font, header in Even's font

Addresses five HUD layout and interaction issues:
1. tapping a News row when a feed-status banner is present shifted row indices by one
2. live partial transcripts were not shown inside the reply card while speaking
3. multi-line list row previews wrapped onto the row below
4. reader font size reduced to 55% scale for higher information density
5. top header time and page title rendered in Even's font alongside mic and battery icons

- **Your words appear in the reply card while you speak.**
  - **Why they never appeared:** no partial transcript existed anywhere. Speech-to-text (whisper) only transcribed a whole utterance after 1 s of quiet, and the first time the glass saw the words was the consent card. JOAT's own conversation worked the same way.
  - **What the Mac does now:** while a prompt is open, it transcribes the speech so far about once a second through the warm whisper server and sends a new `prompt.partial` (`protocol.py` `PromptPartial`, `types.ts` `PromptPartialMessage`).
    - Only for that prompt's own audio: its epoch and its connection.
    - One request at a time.
    - Dropped once the utterance ends.
    - Never acted on: the final transcript alone answers.
  - **What the glasses do:** the Hub shows the words under JOAT's question. The first words grow the card by a row; after that each update is an in-place text upgrade, and only the newest words are painted.
  - A partial never starts or restarts the whisper server and never falls back to the batch CLI. With the batch CLI engine there are no partials.
- **A News tap opens the story on that row.** When any feed failed, News put a "[!] Some feeds unavailable" row above the headlines, but a tap's row index was used as a headline index. Every tap opened the story below it, and the last row opened nothing (a republish that looked like a dead tap). With 3 categories × 3 feeds, one failing feed is enough. The panel now skips the status row (`NewsService.status_rows`); tapping the status row itself opens nothing.
- **List rows stay on one line.**
  - A multi-line message preview reached the firmware row with its line break. Pretext measures a line break as 0 px, but the firmware breaks there: two 27 px lines in a 40 px row, over the row below.
  - Rows were also cut to the last pixel of the 536 px row, which the selected row's border narrows.
  - The fix, Mac side: the inbox row joins the preview's whitespace.
  - The fix, plugin side: `fitListRow` collapses whitespace in every firmware list row and cuts it to 524 px.
- **Even's font at 55%:** readers use an 18 px line (15 px glyphs) with a 5 px gap between messages. The thread fixture holds eight messages a page, up from six at 66% and two in the firmware font.
- **The header's words in Even's font:** the clock, the page's name, the activity chips and the low-battery number, on every image page and on the image header of every firmware page. The mic and battery stay icons.
- **Verified in the official simulator** (`?joatVisual=message-reply-question&joatReplyWords=…`, `messages-conversation-long`):
  - The words show in the reply card over the thread.
  - The thread at 55% and the header in Even's font render as described.
  - Not verified with live speech (the simulator has no microphone), and not on physical glasses.
  - Firmware lists (the Messages inbox, News) are still the firmware's own font: only image pages can be drawn smaller.
- **Tests:** plugin 1131 passed, typecheck clean. Mac 3455 passed, 1 skipped; the container's IPv6 test fails as before. mypy clean. Both sides change: this needs a new package and a brain restart.

## 2026-10-06 — Build JOAT 0.1.108

- Packed from `a0aa1bf` with `scripts/pack.sh`, the maintainer's own manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. The version guard confirmed 0.1.108 above the last recorded build (0.1.107); app.json, package.json and package-lock.json all 0.1.108. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1127 tests passed.
- Canonical `apps/hub-plugin/joat.ehpk`: 167555 bytes, SHA-256 `edbf5464f909a2c30f26ee2c3d13b352b281c6943cccb6278bd9e075de1a95d1`. Adds to 0.1.107 the readers in Even's font at 66% across the whole glass (the atlas adds about 9 KB). Plugin-only. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-06 — Readers in Even's font at 66%, spread across the whole glass

Renders reader surfaces in Even's font at 66% scale across the full 576×288 canvas with consistent line spacing and safe edge margins so text is never clipped at the display border.

- **Every reader is now set in Even's own font at 66%**: the Messages thread first, and also the message review, News, QuickNotes and Planning readers. That means every surface that went through the firmware text page (`renderNativeTextPage`).
  - **Why images:** the firmware gives plugin text one font at one size (a 27 px line, seven lines a page). No SDK through 0.0.16 offers a size. So the page is drawn into the four 288x144 image tiles over the full-screen input layer, as Planning is.
  - **The font itself:** the letterforms are the firmware's, from the atlas cut from the official simulator (`src/assets/even-font.*`, `scripts/build_even_font.py`). Each glyph sits where pretext places it, so the advances and kerning are the firmware's, scaled.
  - **Word spacing:** 1.5 px is added per space, because at 66% the firmware's space runs words together.
- **Spaced out, edge to edge:**
  - Body text sits on a 22 px line (the 18 px glyph line plus leading).
  - A 6 px gap separates messages.
  - Text runs from x 18 to 558.
  - The hint sits at the foot, with the page count right-aligned.
  - The thread fixture went from two messages on page 1 of 4 to six messages on page 1 of 2.
- **Responsive, element by element** (`EvenFont.ts`):
  - **Messages and prose** wrap at word boundaries and are never cut. A message that does not fit carries on to the next page.
  - **One token wider than the whole line** (a long link) is the only thing broken mid-word, after `/ . - _ ? & = #` where possible.
  - **The hint** keeps the page count whole. It drops its swipe clause first, then shortens with "…".
  - **Rows are re-set at the new width.** The Mac's 62-column reader rows are joined back first: a row wrapped at a space or cut at the full 62 columns continues, and a line break ends a paragraph.
  - **Sender names** ("Casey:", "You:") are drawn brighter than the words after them.
- **Margins:** nothing is drawn within 16 px of either side, 3 px of the top or 8 px of the bottom. A test checks every placed run on stress pages (long names, doubled links, a long hint). The simulator capture has no lit pixel in those bands.
- **Paging** repaints the tiles; it no longer upgrades the text in place. Only tiles whose pixels changed are sent, and paging back reuses the encoded tiles.
  - Measured in the simulator: opening the thread 144–357 ms (the first open includes loading the font); a page turn 75–81 ms.
  - On the glasses each tile is a BLE image send (about 0.1 s plus 3.9 ms/KB), so a page turn will cost more than the two text upgrades (about 83 ms each) it replaces. This is the one place the change adds latency, and it is not yet measured on hardware.
- **A reader keeps its page through the background migration.** The page index is in the background snapshot; before this, a return started the reader over at page 1.
- **Known limit:** when JOAT's box opens over the thread, the lines left above the box are firmware text in the firmware's size. Image tiles cannot be cut under a box without being resent.
- **How to verify, and what is not verified:**
  - `VITE_EVEN_FONT_PAGES=0` keeps the firmware text pages.
  - Dev fixture: `?joatVisual=messages-conversation-long`, a real Mac-paged thread with a link cut mid-token, driven through the menu so the simulator's swipes page it.
  - The simulator draws the firmware font as an approximation (Even's test-with-simulator guidance), so the letterforms need a look on the glasses.
  - Not verified on physical glasses.
- Plugin 1127 passed; typecheck clean. Plugin-only.

## 2026-10-06 — Build JOAT 0.1.107

- Packed from `47f7589` with `scripts/pack.sh`, the maintainer's own manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. The version guard confirmed 0.1.107 above the last recorded build (0.1.106); app.json, package.json and package-lock.json all 0.1.107. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1114 tests passed.
- Canonical `apps/hub-plugin/joat.ehpk`: 158434 bytes, SHA-256 `d650af5c7311cbae4c1ba2a18c16e49af7e6579991f8318de6ddbcbe575cc860`. Adds to 0.1.106 the one image header on every page and the working indicator inside JOAT's box. Plugin-only. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-06 — One header on every page; JOAT's working indicator inside its box

Unifies every page onto the shared 24 px top header template (icons for mic/battery, text for static labels) and moves JOAT's animated working indicator inside the conversation box rather than straddling its bottom border.

- **One header template.** The image pages (the Planning month, the live map) always drew the compact spine: the clock and the page's word in a small face, the mic and battery as icons. Every firmware page drew a line of the firmware font instead ("9:41 · MESSAGES · CONVERSATION · ● · ■"), bigger and different. Every firmware page (Home, lists, readers, the Messages thread, JOAT's box, the reply card, confirmations) now carries the same spine, drawn by the same renderer code (`HudCanvasRenderer.renderHeader`), as two 288x24 image containers (`HeaderImage.ts`; the SDK caps an image at 288 px wide).
- **Images only where they rarely change.** The halves go on the image lane after the page's text, one send each after a page rebuild; afterwards only a half whose pixels changed is sent (the lane's existing pixel cache): a minute tick resends the clock's half, a mic or battery change the other. The page's text stays firmware text and keeps upgrading in place. `VITE_IMAGE_HEADER=0` keeps the old text line.
- **The working indicator stands inside the box,** on the row the answer will take: a fresh question is a two-row box, the question and the animated glyph, and the answer replaces the glyph. Nothing straddles the border; the footer holds only a sentence (the continuation prompt).
- **The simulator captures now show the real header.** The dev fixtures used to write a placeholder status line ("9:41 JOAT ready bat 87%") no wearer ever saw; they now set the same structured status model Chrome gives production.
- Verified in the official simulator: the thread, Home, JOAT's box over Home and the reply card all carry the calendar's spine; the thread's header names its page; the indicator sits on the answer's row. Not verified on physical glasses.
- Plugin 1114 passed; typecheck clean. Plugin-only.

## 2026-10-06 — Build JOAT 0.1.106

- Packed from `4106a3f` with `scripts/pack.sh`, the maintainer's own manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. The version guard confirmed 0.1.106 above the last recorded build (0.1.105); app.json, package.json and package-lock.json all 0.1.106. `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1107 tests passed.
- Canonical `apps/hub-plugin/joat.ehpk`: 158161 bytes, SHA-256 `582559b8e8433df046a7527b087e78a9417169d949f467240616a45d63a87cbe`. Adds to 0.1.105 the JOAT box that floats over the page it was called on and grows from two rows, with the working indicator on its border. Plugin-only since 0.1.105: a brain restarted for 0.1.105 needs no restart. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-06 — JOAT's box floats over the page it was called on and grows as Even AI's does

Renders the JOAT assistant as a floating overlay card over the active page, starting at two rows and expanding upward to seven rows before scrolling, with an animated thinking/working indicator.

- **The box grows.** It opens at two rows and grows upward from a fixed bottom edge, in four sizes (2, 3, 5, 7 rows), as requests and replies are added. At seven rows it stops below the header and scrolls, showing the latest reply; a swipe up reads back. Each size step is one rebuild; streaming, scrolling and the indicator within a size stay in-place upgrades. This was built on 2026-09-24 (7e867a0) and lost when a later merge fixed the box at seven rows.
- **The working indicator rides on the box's bottom border**, centred, and animates only while JOAT is thinking or answering. A finished answer shows none; it used to leave a solid ■. A sentence (the continuation prompt, a reply draft's gestures) sits inside the box instead, and the box grows a row to hold it.
- **The box floats over the page it was called on** (`ConversationOverlay.ts`). The G2 has no fill colour (black is off, so a box cannot hide what is under it), so the page gives up the area under the box: each of its containers is cut to end above the box's top border, its text fitted to the rows left so nothing overflows, and the box takes the page's event capture and status line. Home, firmware lists, text pages (the Messages thread among them) and the native navigation page stay visible above the box; as the box grows, less of the page shows. The image pages (the Planning month, the live map) and the terminal still give the whole screen to the box: their tiles cannot be cut without resending them.
- **The Messages reply card floats over the thread** the same way: the question and the draft sit in the box with the latest messages above it.
- Scrolling was not lost with the scroll column: a swipe still scrolls the box (captured in the simulator).
- Verified in the official simulator with the dev fixtures (`?joatVisual=assistant-conversation`, `&joatConvTurns=short`, `&joatConvState=thinking`, and `message-reply-question` / `message-reply-confirm` now drawn over the thread): a two-row box over Home, the box at its cap over Home's top line, the indicator on the border, and both reply cards over the thread. Not verified on physical glasses.
- Plugin 1107 passed; typecheck clean. Plugin-only.

## 2026-10-06 — Build JOAT 0.1.105

- Packed from `14a6e64` with `scripts/pack.sh`, the maintainer's own manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. The version guard confirmed 0.1.105 above the last recorded build (0.1.104); app.json, package.json and package-lock.json all 0.1.105. `evenhub` CLI, `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1099 tests passed.
- Canonical `apps/hub-plugin/joat.ehpk`: 156924 bytes, SHA-256 `d01c25db1c625f52a6b5f6c9ee7bb5ec777d79f87731431ef8ac27a25448eaf7`. Carries the Messages thread reader with tap-to-reply in JOAT's conversation card, and the conversation card without its scroll column. The Mac side changed too: restart the brain from this revision. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-06 — Messages is one thread you read and tap to reply; no scrollbar boxes on the conversation card

Streamlines Messages into a single scrollable conversation thread where tapping initiates a voice reply inside JOAT's conversation card, and removes the broken glyph scrollbar column.

- **The thread is the only Messages reader.** `MESSAGES · CONVERSATION` is one Mac-paged text page, newest message first, each message whole ("Casey: …", "You: …"), fifteen read at a time. A swipe pages it; past the last page the Mac reads older messages from chat.db. The single-message reader (`MESSAGES · MESSAGE`), the "✎ Reply" row and the ▲/▼ paging rows are deleted.
- **A tap on the thread replies.** JOAT's conversation card opens over it: "What do you want to say back to Casey?". The user speaks; the card shows their exact words with "tap: send · long-press: retry · double-click: cancel".
  - Tap sends. The consent gate underneath is the same signed confirmation as before; only its page changed.
  - Long-press says it again: the Hub declines the draft and asks the Mac for a retry (`message-retry`), and the Mac asks the question afresh once the draft has ended, whichever arrives first.
  - Double-click cancels back to the thread.
- **After the send the wearer is back on the thread**, the reply its newest message: read back from chat.db, or from the send itself if Messages has not written it within 3 s. Sent and cancelled no longer get receipt pages; failed and uncertain still do.
- **The two-minute window** the wearer asked about is the Mac's expiry on the selected conversation, a guard against replying to a thread no longer in view. Since earlier today it restarts while the thread is on the glass, so a tap on the thread always replies; it still runs out once the wearer has left.
- **Fixed on the way:** a status change cleared a sent reply's words before the Messages UI could read them, so the thread would never have shown the reply (caught by the new integration test). The words are now dropped right after the observers run. A tap that reaches a read-only thread says so instead of arming anything.
- **The conversation card's scroll column is gone.** It was an 18 px text column of 20 px `│`/`█` glyphs, so the firmware wrapped each glyph onto its own line: black gaps and boxes down the card's right edge. 7e867a0 had removed it (Even AI draws no scrollbar); a later merge brought it back. Swipe still scrolls back.
- Verified in the official simulator with the dev fixtures (`?joatVisual=messages-conversation`, `message-reply-question`, `message-reply-confirm`): the thread page, the question card and the consent card render as described. Not verified end to end against a live brain, and not on physical glasses.
- Tests: plugin 1099 passed, typecheck clean. Mac 3448 passed, 1 skipped; the container's IPv6 test fails as before. mypy clean. Both sides change, so this needs a new package and a brain restart.

## 2026-10-06 — Claude sign-in finishes on the phone

The wearer signed in to Claude from the phone's plugin screen, and the sign-in stopped before the Authorize step. The Authorize page was waiting on the MacBook the brain runs on.

- **Cause.** `claude auth login --claudeai` does two things at once:
  - It opens the Mac's own browser on an authorize page that redirects to `localhost`.
  - It prints a second link, which the phone opens, ending at Claude's code page.

  Both links share one login, so the Mac's page was a second, competing copy of the flow. It sat on a Mac the wearer was not at. Checked against CLI 2.1.291 with a recording `BROWSER` script.
- **Fix.** The sign-in now hands the CLI `BROWSER=true`, an opener that opens nothing, so nothing appears on the Mac and the phone's link is the only one.
- **Instructions on the phone.** The phone now names all three steps:
  1. Sign in.
  2. Tap Authorize.
  3. Paste the code Claude shows.

  If the page comes back signed in without an Authorize button, the phone says to open it again or copy the link into Safari.
- **Tests.** `test_cli_env.py` checks that the sign-in's real spawn receives `BROWSER=true`, and `test_cli_signin.py` checks the steps message.
- **Not verified.** The phone side was not run on a device: the in-app browser, a universal link into the Claude app, or an emailed sign-in link opening in another browser could still split the flow.
- Mac-only: restart the brain from this revision.

## 2026-10-06 — A Messages reply works after reading, and a refused tap says why

The wearer asked why a reply to a message still could not be sent.

- **The reply clock ran from the moment a conversation opened.** A selected conversation stayed replyable for two minutes from when it opened, however long the wearer then spent reading it. A reply (spoken "reply", or the tapped "✎ Reply" row) after that was refused as an expired selection.
  - The clock now restarts whenever the conversation is checked while it is still the page on the glass (`MessageUIController.conversation_on_glass`), so reading never runs it out.
  - Off the glass it still expires after two minutes, as before.
- **The tapped Reply row refused in silence.** `begin_reply`'s refusal was dropped, so the tap did nothing visible. The refusal is now a receipt on the lens, and while the microphone is still capturing the reason is "Wait for the microphone to finish, then reply."
- **Tests.** `tests/integration/test_message_ui.py` reads a conversation for five minutes and then taps Reply, checks that the conversation off the glass still expires, and checks that a refused tap shows its reason. On the code before this change, the first two bug tests fail.
- Mac-only: restart the brain from this revision. Mac: 3433 passed, 1 skipped; the container's IPv6 test fails as before. mypy is clean.

## 2026-10-05 — Build JOAT 0.1.104

- Packed from `8f52f4a` (GitHub main at PR #13 plus the version bump) with `scripts/pack.sh`, the maintainer's own manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. The version guard confirmed 0.1.104 above the last recorded build (0.1.103); app.json, package.json and package-lock.json all 0.1.104. `evenhub` CLI 0.1.14, `--sdk-ver 0.0.16`, minimum Even App 2.2.10. Plugin 1076 tests passed.
- Canonical `apps/hub-plugin/joat.ehpk`: 164940 bytes, SHA-256 `c2dd1705ecb58ead5c6e4f84024aea8a76d6c6825c84b28491927a64d576c5a6`. Same plugin and Mac code as 0.1.103 (the commits since are merges and the simulator captures); a brain restarted for 0.1.103 needs no restart. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-05 — Build JOAT 0.1.103 text Home

- Packed from `38b9abb` with `scripts/pack.sh`, the maintainer's own manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. The version guard confirmed 0.1.103 above the last recorded build (0.1.102); app.json, package.json and package-lock.json all 0.1.103. `evenhub` CLI 0.1.14, `--sdk-ver 0.0.16`, minimum Even App 2.2.10.
- Canonical `apps/hub-plugin/joat.ehpk`: 164939 bytes, SHA-256 `a43cf1ac577838ecd64710ec28853b90cb3c84c63202bbb2f2daf088c63a9e1d`. Home is firmware text again (six text containers, no image tile): one rebuild to enter, then only changed bands as in-place upgrades — a minute tick is one upgrade, a tab swipe two. Carries 0.1.102 and the cloud session's credential-list fix (`9e8dc37`). Plugin 1076 tests, Mac 3105 passed. The Mac side changed in `9e8dc37`, so restart the brain from this revision. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-06 — Every CLI launch drops the same credential list

- Each launch path kept its own list of variables to drop before starting `claude` or `codex`:
  - the assistant seats and the sign-in dropped a bearer token and a base URL;
  - the Brainstorm critic dropped only `ANTHROPIC_API_KEY`, and the proposer only `OPENAI_API_KEY`;
  - the QuickNote completer dropped two names.

  An inherited token, endpoint or provider switch therefore moved those turns off the subscription without anyone choosing it (RULES.md #9, #10).
- One list now lives in `agents/cli_env.py` (`STRIP_ENV`), and every launch drops it: the assistant seats and their auth probes, both Brainstorm seats, the QuickNote completer and the phone's sign-in. A path can add names (the sign-in also drops the Mac's own login tokens) but can no longer narrow the list.
- The list follows the vendors' own sources:
  - **Claude Code's documented authentication precedence.** These rank above a `/login` subscription:
    - the cloud-provider switches (`CLAUDE_CODE_USE_BEDROCK`, `_VERTEX`, `_FOUNDRY`, `_ANTHROPIC_AWS`);
    - `ANTHROPIC_AUTH_TOKEN`;
    - `ANTHROPIC_API_KEY`, always used under `-p`;
    - an Anthropic profile or federation credential (`ANTHROPIC_PROFILE`, `ANTHROPIC_FEDERATION_RULE_ID`, `ANTHROPIC_ORGANIZATION_ID`).

    `ANTHROPIC_BASE_URL` sends every request, login and all, to another server.
  - **Codex's source.** `codex exec` takes `CODEX_API_KEY` over the ChatGPT login ("API key via env var takes precedence over any other auth method"), and no launch dropped it. Codex no longer reads `OPENAI_BASE_URL`, so it is not listed.
- **Saved Codex accounts.** A saved Codex account no longer runs as an inherited `CODEX_ACCESS_TOKEN`, which Codex reads before the login stored in `CODEX_HOME`. This is the same switching bug fixed earlier today for Claude's `CLAUDE_CODE_OAUTH_TOKEN`. The default account keeps the Mac's own login.
- **Tests.** Each launch path is spawned against a probe that records the environment it received. On the code before this change, every one of those tests failed, naming what leaked.
- **Results.** Mac: 3430 passed, 1 skipped; the container's IPv6 test fails as before. mypy is clean.

## 2026-10-06 — Build JOAT 0.1.102 reader-fixes package

- Packed from `a550639` with `scripts/pack.sh`, the maintainer's own manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. The version guard confirmed 0.1.102 above the last recorded build (0.1.101), and app.json, package.json and package-lock.json all say 0.1.102. `evenhub` CLI, `--sdk-ver 0.0.16`, minimum Even App 2.2.10.
- Canonical `apps/hub-plugin/joat.ehpk`: 155222 bytes, SHA-256 `91841ecc122392a47d03dc8c83c6152d63eade4334a5c341bce390d667fd4ad3`. Adds to 0.1.101 the minute-aligned home clock and the reader fixes: the spoken-reply check is a text page, every Mac-paged reader pages to its end, and a review sends only from its very last page. Plugin-only changes since 0.1.101, so a brain already restarted for 0.1.101 needs no restart. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-06 — Readers page to their end, and a review sends only from its last page

The wearer asked where the plugin re-implements scrolling the firmware already does. Readers are paged twice: the Mac pages a text into ten-row pages, and the plugin pages each of those again for the firmware font. Three readers failed in that seam. Each failure reproduced in the official simulator, each fix is plugin-only, and each has a test that fails on the code before it.

- **The spoken-reply check drew as a list.** `AGENT TERMINAL · CHECK` (0.1.101, for replies too long for the confirmation page) was not a text-page title, so the plugin drew it as a firmware list. Five of its ten rows showed, a swipe moved a highlight, and a tap on the fourth row sent the reply. It is a text page now.
- **A saved Brainstorm stopped at its first page.** Only QuickNotes and the Messages review asked the Mac for a reader's next page. `BRAINSTORM · SAVED` promised "swipe: more", and six swipes left it on the same page.
  - Every reader the Mac pages now asks past the local edge, naming the reader's id and page.
  - A swipe back opens the previous page on its end.
- **A review could be confirmed unread.** The Mac offers a review's tap only on its last page, but that page can span two firmware pages. On the first of them "tap: confirm" was offered, and a tap sent the message with its last two sentences unseen.
  - A review (the Messages review, the terminal check) now sends only from the last firmware page of the Mac's last page.
  - Before that page, a tap reads on (Even's text-heavy template also turns the page on a tap), and the footer offers the tap only on that page.
- **The footer's swipe promise covers both layers.** It now reads "swipe: page" when a page lies either way. Before, a page with more below it could say "swipe: previous", because only the Mac's pages were counted.
- **Deleted `AssistantAnswerPager.ts`.** It was a copy of Even's template pager that nothing had imported since `FirmwareText.paginateMeasured` replaced it.
- **What the firmware scrolls by itself**, measured with a probe page in simulator 0.9.5 and recorded in `docs/EVEN_RESEARCH_QUESTIONS.md`:
  - An overflowing capture text container scrolls one line per swipe and draws its own 4 px scrollbar.
  - Only boundary swipes reach the app.
  - Any upgrade of its text resets it to the top.
  - JOAT never lets a text page overflow, so every swipe turns seven lines, the pattern of Even's own text-heavy template ("On G2 you can't scroll. You turn pages.").
- Plugin 1065 tests; typecheck and the production build are clean.

## 2026-10-06 — The home clock turns with the minute

- Measured in the simulator with a host-call probe over an idle home screen: the clock repainted on a free-running 30-second interval, so it turned up to 30 s after the minute (14 s in the run) and the page was rendered twice a minute for one change. `Chrome` now schedules its repaint at each minute boundary (+50 ms) and once a minute after that. A test pins 09:41:30 → nothing until 09:42:00.1, which shows 9:42, then one repaint at 09:43:00.1.
- The same probe priced the everyday journeys at the repository's measured G2 costs (≈83 ms a text upgrade, ≈165 ms a page rebuild, ≈104 ms+ an image tile): boot ≈1.0–1.4 s (9–13 tiles), a rail swipe ≈208 ms (two tiles), opening a tab ≈165 ms (one rebuild), back to home ≈685 ms (a rebuild and five tiles), an answer over home ≈165 ms, dismissing it ≈581 ms (a rebuild and four tiles), an idle home minute two tiles (the clock, the timer chip). Returning home is the costliest common step; recorded for the next rendering change.

## 2026-10-06 — Build JOAT 0.1.101 terminal-checks package

- Packed from `3825a48` with `scripts/pack.sh`, the maintainer's own manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. The version guard confirmed 0.1.101 above the last recorded build (0.1.100), and the new three-file version check passed (app.json, package.json and package-lock.json all 0.1.101). `evenhub` CLI, `--sdk-ver 0.0.16`, minimum Even App 2.2.10.
- Canonical `apps/hub-plugin/joat.ehpk`: 154724 bytes, SHA-256 `7b07d47831639401e61676fc26021fda321dcdf1c413ac6d7ba9ea1695864076`. Carries the terminal's long-press cancel and the "CHECK WHAT WAS HEARD" page, and drops the self-test's double press. The Mac side changed (phone-control fixes, the terminal check and cancel, the self-test removal), so the brain needs a restart from this revision; brain and plugin should move together, since the cancel and the check are new wire messages. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-06 — Terminal: long press cancels a running job; a spoken reply is checked before it goes

- Maps long-press to cancel a running Agent Terminal job and requires a tap confirmation check on spoken replies so misheard transcriptions can be re-stated before reaching the session.
- **Long press cancels a running Agent Terminal job.** A new `cancel_terminal` action (protocol.py and types.ts in step) runs the same roster cancel as the spoken "terminal cancel", which still works; the receipt is spoken back. The gesture is claimed only on a running work session and consumed there even when the Mac is unreachable, so it never mutes the mic instead; everywhere else long press is still the microphone (and the NEWS/SEARCH refresh). The running page's footer reads "long-press: cancel · double-click: back" where it named the spoken command.
- **A spoken reply to a work session is shown before it is sent.** The terminal's armed utterance is no longer delivered on arrival: the runtime claims it (keeping the lane, focus and context the tap bound, so a check that outlasts the capture window still goes to that exact session and to nothing else if it moved on) and asks. Short replies use the confirmation gate with a new `purpose: "terminal_input"` that relabels it "CHECK WHAT WAS HEARD … TAP · send   SWIPE · say it again" (an older plugin ignores the field and shows the generic labels): tap sends, swipe publishes the question again so the wearer re-states it, no answer sends nothing. Replies too long for that page are shown whole on an "AGENT TERMINAL · CHECK" reader first (the tap that sends is offered on its last page, as the Messages review does): tap sends, Back drops. The check runs off the speech dispatcher, so admission is never held while the wearer reads.
- Verified in the official simulator (v0.9.5, whose automation API has long_press, long_press_release and context_menu — the skill's docs list only four actions): a long press and release on a running terminal reached the Mac as `cancel_terminal` and logged "cancelling the terminal job", not a mic toggle; the check page rendered the words with the new labels; a tap returned accepted, a swipe declined. The check page is one firmware text page (no image tiles). Tests: the controller's long press on and off a running job and while disconnected, the router's carve-out, the footer, the check labels and their one-line fit, the runtime's send / say it again / drop paths against the real prompt dispatcher, the long-reply reader (a tap on its last page sends, no answer sends nothing), the wire action and purpose. Plugin 1056 tests, Mac 3324 passed; tsc, mypy and the build clean.

## 2026-10-06 — The Brainstorm self-test is deleted

- The physical-G2 Brainstorm acceptance run (`--g2-diagnostics`, a right/ring double press on an armed ready frame) could only ever end "inconclusive": it waited for `visual_frame` receipts that hashed the four-tile Brainstorm raster, which no longer exists since Brainstorm became firmware text, and for a tap on the terminal back to the room, removed on purpose on 2026-09-11. The wearer chose deletion over a rewrite: the simulator probe (`scripts/simulator_brainstorm_native_probe.py`) and real use cover the same ground.
- Gone: `services/brainstorm_acceptance.py`, `scripts/real_g2_brainstorm_acceptance.py`, `scripts/simulator_brainstorm_action_flow_acceptance.py` and their tests; the `begin_acceptance` action, the `acceptance_ready` state and the `acceptanceArmed` field on both sides of the wire (protocol.py and types.ts in step); the Brainstorm `visual_frame` trace receipt; the plugin's armed right/ring double press and its "tap: start test" hint; the router's and main's `acceptance_armed` plumbing. A right double press on a live Brainstorm is the same Back as the left, cancelling the run.
- The simulator helpers three other scripts imported from the deleted one (process management, waiting, framebuffer capture, the plugin snapshot) live in `scripts/simulator_harness.py`.
- Kept: `--g2-diagnostics` still turns on the content-free gesture and paint trace; `show_brainstorm` stays on the wire with the Mac's handlers (no glasses gesture sends it today).
- Plugin 1049 tests, Mac 3100 passed, typecheck and mypy clean.

## 2026-10-06 — The phone's Brainstorm and Accounts screen, fixed where it failed

A review of the phone control screen (merged in 0.1.100) found these; each has a test that fails on the code before this change.

- **A seat could be marked signed out by its own answer.** A failed or timed-out turn was classified from everything the CLI printed, the model's answer included, so a Brainstorm about logins that failed marked a working seat signed out, and every later turn was refused without starting the CLI until the wearer tapped Refresh. The classifier now reads only the CLI's own complaints (`SubprocessAgent.last_diagnostic`): stderr, the non-JSON lines and `is_error` result of stream-json, the error events of `codex exec --json`, and the last lines of plain output — none after a timeout.
- **A web page on the Mac could control the accounts.** Loopback peers skip pairing so the simulator and the harnesses can connect, and a page open in a browser on the Mac also arrives from loopback. With the account controls it could sign the wearer out, remove accounts, or finish a sign-in with an account of its own and receive the wearer's turns. The exemption now also requires no `Origin` (a native client) or a loopback one (the simulator's dev server); a foreign page pairs like any network peer.
- **A wrong pasted Claude code left the phone on "Checking the code…" for 15 minutes.** When the CLI asks again the phone asks again ("That code didn't work"), and a code the CLI never answers fails after 90 s.
- **Remove and Sign out raced a sign-in.** They now stop a sign-in still running for that account; Remove keeps an account whose sign-out failed while the CLI still reports it signed in (deleting its home orphaned the login where nothing could revoke it); a sign-in finishing for a removed account is a notice, not an unhandled task error.
- **A phone update could be dropped.** A change landing while the previous state was still being sent is now sent after it, so a device code can no longer go missing behind a slow send.
- **The sign-in CLI outlived the brain** (it runs in its own session); shutdown now closes the control screen and stops it.
- **Reset said "reset" while the room was still active** until the 8 s cancel watchdog, refusing a new Brainstorm; after its grace it now releases the room.
- **A saved Claude account could run as an inherited login.** `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_AUTH_TOKEN` in the brain's environment outranks `CLAUDE_CONFIG_DIR`, so switching accounts did nothing; a saved account's spawns no longer inherit them (the Mac's own login is left as configured).
- `CliSignIn` had no tests; it has three now, against a scripted `claude` in a real pty. Mac unit suite and the gateway integration suites pass; mypy clean. One integration test (`test_wildcard_host_accepts_both_families`) fails in the cloud container with or without this change: the container has no IPv6.

## 2026-10-06 — What the merge left behind

- `HudPageBuilder.overviewTextLayout` is gone with its four tests. The 0.1.100 merge dropped the text overview's presenter path and kept the layout with no caller; it stays in history at `61e402e`.
- Six test files still stubbed the presenter's list-cursor calls (`setSelection`, `moveSelection`, `restoreSelection`, `usesCustomListSelection`) that 0.1.99 deleted, and several asserted those stubs were never called — true of any call that no longer exists. The stubs and the vacuous assertions are removed; the menu suites check what the firmware list actually does.
- `package.json` and `package-lock.json` had said 0.1.90 since 2026-09-13 while ten builds went out, because only `app.json` reaches the package. Both say 0.1.100 now, and `pack.sh` refuses a package whose three numbers differ, naming the `npm version` command that moves them together.
- `docs/EVEN_RESEARCH_QUESTIONS.md` #3 no longer calls the list cursor "the single blocker" on moving the panel tabs off the image path: they moved in 0.1.99. The question to Even stays open.
- Checked after the merge, in the simulator: the three-at-once fixture (a route behind a conversation, the Pomodoro and next turn in its header, the map back at the newest frame) and the Brainstorm probe (picker, ready, the tap that arms topic capture, a streamed run, the thinking counter, the settled synthesis) — zero image tiles on every Brainstorm page. Plugin 1055 tests, typecheck and build clean.

## 2026-10-05 — Build JOAT 0.1.100 merged package

- Packed from `7873ba0` with `scripts/pack.sh`, the maintainer's own manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. The version guard confirmed 0.1.100 above the last recorded build (0.1.99). Release-candidate validation, the release manifest, TypeScript checking and the production Vite build passed. `evenhub` CLI 0.1.14, `--sdk-ver 0.0.16`, minimum Even App 2.2.10.
- Canonical `apps/hub-plugin/joat.ehpk`: 163427 bytes, SHA-256 `c4e812cd1c1a82b58bcc453531e68522797c425faa761c865ef0e672f04b7513`. The first package with both lines in it: GitHub main through 0.1.98 and the cloud session's 0.1.99 work (route behind any page, activity chips, raster deletions finished), plus the local line — the phone page's Brainstorm and Accounts tabs beside Places and Connection, subscription account switching on the Mac, the Brainstorm thinking counter, the local command channel and the local Mac fixes. Plugin 1059 tests, Mac 3125 passed. The Mac side changed (accounts, sign-in, Brainstorm control, places), so the brain needs a restart from this revision. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-05 — Build JOAT 0.1.99 three-at-once package

- Rebuilt the existing JOAT package from `f870700` with the same `scripts/pack.sh`, the maintainer's same manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. The pack script's version guard confirmed 0.1.99 above the last recorded build. Release-candidate validation, the release manifest, TypeScript checking and the production Vite build passed. `evenhub` CLI 0.1.14, `--sdk-ver 0.0.14`, minimum Even App 2.2.9.
- Canonical `apps/hub-plugin/joat.ehpk`: 153816 bytes, SHA-256 `29cac9133909d2e158d51892bd0d6ddc38b946052b6fc6e2252f26fb9096d336`. Carries the route behind any page, the activity chips in every header and spine, the conversation page's header line, and the completed deletion of the raster duplicates and the plugin-owned list cursor. The Mac side changed only in a docstring and its tests; the brain needs no restart beyond the one 0.1.97 asked for. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-05 — Three features at once: a route behind any page, every header says what else is running

- The wearer's reason for building their own plugin, and the third item of the plan agreed on 2026-10-05 (header, then navigation, then the composite page) that had not been built: Even's Navigate, Conversate and a timer hold the lens one at a time; JOAT's features should not. They did, and worse: every navigation frame (once a second while walking, since 0.1.97) went through `showNavigation`, which hands off whatever is on the glass, so a conversation, a terminal or a Pomodoro page lasted until the wearer's next step and the map came back on its own.
- **A route keeps running behind any page.** `NavigationController` sends progress, and the Mac's own route views (guidance, warnings, arrival), through `HudPresenter.updateNavigation`. While the route owns the glass (its page, or Home with nothing open) that repaints exactly as before; behind a conversation, a terminal, a list, a reader, an answer, a question or a decision gate it updates the retained page and the header's turn chip and never takes the glass, so leaving the conversation shows the map at the newest frame. The pages that ask the wearer something (the place, the mode, the preview, an error) still take the glass. The Mac keeps hearing semantic states throughout. A stopped route clears its chip.
- **Every header says what else is running.** `ActivityChips` turns a running Pomodoro into "Focus 12m" ("Break 4m", "Paused 7m"; minutes rounded up so it never says 0 early) and a route into its next turn as a firmware arrow and distance ("→ 80 m", "← 1.2 km", "◆ 30 m") or the warning in a word ("Off route"). The firmware header line carries them between the page's word and the glyphs (`9:41 · JOAT · Focus 12m · → 80 m · ● · ▇`), fitted by the firmware's own measurement so the word shortens and goes before a chip or a glyph would; the image spine draws them right-aligned against the microphone, inside the right tile. The page that already shows a feature has no chip for it (the map carries the timer, not the turn; the Pomodoro page the turn, not the timer). A chip changes in place: the timer on its minute (its own ticker, whatever the page), the route when its rounded distance moves; never a rebuild.
- **The conversation page has the header line.** It had none, so a timer or a route vanished behind a conversation. `assistantConversationLayout` gains the header container; the card's lower edge moves to 274 so seven transcript lines still fit under it; the conversation surface writes header changes through its own serialized lane, so they never race the transcript on the bridge.
- **Corrections to 0.1.98, which claimed more than it did.** Its entry said the raster frame kinds were deleted; the script that should have done it aborted, and they, the transcript hooks and the `visual_frame` receipts were still there. It also kept the "conversation card" as pixels by design: the presenter only ever built the native conversation, so the raster card, its geometry and its canvas measurement never ran. And the plugin-owned list cursor (`CapabilityMenu`'s ordered-selection driver, the presenter's `setSelection`/`moveSelection`/`restoreSelection`/`usesCustomListSelection`) only ever moved raster lists. All of it is deleted now, with the expanded tab-rail mode, `preparePageTransition` and the typography and geometry keys only the deleted painters used; export and public-method sweeps over `src` find nothing unused in production but two named test seams. `HudCanvasRenderer.ts` is 2,157 lines (4,513 before 0.1.98). Two live bugs went with the cursor code: a swipe that stayed on a firmware list's last row rebuilt the list and put its highlight back on row one, and the menu's cursor started on the Mac's remembered row while the firmware highlight starts on row one, so a bare tap on row one could open another row. The cursor now starts at row one for every firmware list and on Back, and follows the index the firmware reports. The presenter makes any list without a flag a firmware list over the image surface, so no caller can produce an empty image page (the lifecycle path could). The Mac's Brainstorm row budget no longer claims a plugin mirror, and its `test_sinks` pins check the native terminal page.
- **Known gaps.** The conversation page shows the turn chip, not the map: the map stays a full page, and an image beside a conversation would narrow it to half the lens. The Mac's diagnostic-only Brainstorm acceptance route waits for a raster `visual_frame` receipt that has not existed since Brainstorm became a firmware page; it cannot complete until it reads the native page instead.
- Verified in the official simulator with the new `joatVisual=live-concurrency` dev state, which drives the production presenter through four stages: the map with "Focus 12m" in the spine; a conversation whose header reads `9:41 · JOAT · Focus 12m · → 250 m · ● · █`; two route updates behind it, written in place to "→ 80 m" with the conversation still up; the map back at 80 m. The transitions, planning, panels, messages and QuickNotes probes pass; the panels probe needs a fresh simulator (the transitions probe ends on the arrival page, where a swipe belongs to navigation). Tests: the chips and their glyphs, the fitted header, the spine chips, the presenter's background route behind a conversation and a list, Home still showing the map, the Pomodoro chip on and off its own page and on its minute, the controller's routing of progress and questions and the stop hook. Plugin 1035 tests in 59 files, typecheck and production build clean; Mac 3042 passed, 1 skipped, mypy clean.

## 2026-10-05 — Build JOAT 0.1.98 native-only package

- Rebuilt the existing JOAT package from `552ff16` with the same `scripts/pack.sh`, the maintainer's same manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. The pack script's version guard confirmed 0.1.98 above the last recorded build. Release-candidate validation, the release manifest, TypeScript checking and the production Vite build passed. `evenhub` CLI 0.1.14, `--sdk-ver 0.0.14`, minimum Even App 2.2.9.
- Canonical `apps/hub-plugin/joat.ehpk`: 154866 bytes (10.5 KB smaller than 0.1.97, the deleted raster painters), SHA-256 `f2164277fe1b0b23cbacd263b2bec8e1f8b02ea226dd62c675950af287368f56`. Carries the deletion of the raster duplicates, every list as a firmware list but the Planning month, and the pretext-measured native pages; the Mac side changed only in a comment and its tests, so no brain restart is needed beyond the one 0.1.97 asked for. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-05 — The raster duplicates are gone; native text is measured by the firmware's own rule

- The wearer's three points. The Planning month stays an image: a calendar grid has no firmware template, so the month and its split dashboard are still drawn as tiles, with the 24 px icon spine above and the firmware hint below — nothing about the grid changed. "Are you sure you are deleting any duplicates?" — they had not been deleted, only bypassed: the renderer still carried a canvas painter for every page the firmware now draws (the question, the confirmation, Brainstorm and the Agent Terminal transcript with its elapsed counter, the Calendar and Reminders pages, every navigation frame but the live map, the dense list, the readers, the Messages thread, the Pomodoro page, the paged answer card), the presenter kept a pixel branch for each behind a check of the visual surface's transition barrier that production always passes, and `CapabilityMenu` kept the prose pagers, the plugin-owned cursor and the title allowlist that decided which list went native. "You are not using the font measurement skills" — the plugin wrapped and paged native text with its own word-wrap over `getTextWidth` and sized the confirmation body by a 120-character guess.
- Deleted: the raster painters and their layout helpers, the presenter's raster branches and the thinking ticker, the menu's prose paging and custom message cursor, the overlay's pixel paging, the raster frame kinds, and the tests that pinned them (`AgentTerminalGeometry`, `AgentTerminalHint`, `LiveBrainstormFrames` and its captured frames, the raster cases in the renderer, presenter, menu and list suites). `HudCanvasRenderer.ts` is 2,328 lines (was 4,513), `HudPresenter.ts` 3,070 (was 3,407), `CapabilityMenu.ts` 2,395 (was 2,571). What is still pixels, by design: the overview with its rail, the Planning month and dashboard, the live map, the receipt strip over those three, and the conversation card. Every list is Even's own list unless it carries the month grid (`usesFirmwareList`); every reader is a text page; the browser preview hands pages over like the glass. A navigation frame that arrives while the Agent Terminal is on the glass is a text page too (it fell back to pixels).
- `FirmwareText.ts` measures every multi-line native page with `measureTextWrap` from `@evenrealities/pretext`, Even's model of the LVGL renderer: per-glyph rounding with kerning, breaks at spaces, hyphens and CJK boundaries, leading spaces dropped at a line start, a hard break only inside a word no break point could rescue. A page goes to the firmware unwrapped and `paginateMeasured` decides how many words it holds (readers, answers, the question, the Calendar and Reminders pages), capped at the SDK's content limit; the confirmation's prompt is cut by `fitLines` to the three lines it has, by glyph width rather than by character count, so wide glyphs never overflow and narrow ones are not cut short; a terminal row is one line when the firmware's own width (`firmwareLineWidth`) says so. The plugin keeps no wrapping rule of its own for the native pages.
- Known gap, recorded rather than hidden: the native terminal page shows the Mac's `Thinking…` marker as written, with no elapsed counter beside it. The counter lived on the raster transcript, which the firmware page had already replaced; an in-place upgrade of the status row once a second would be the native version. The Mac's cross-repository pins (`test_sinks`) now check the native page: the marker is not reinterpreted, the transcript window is the page's line capacity less its two fixed rows, and the Mac's row budget stays inside the plugin's per-row cap.
- `scripts/simulator_quicknotes_native_probe.py` was stale since the control rows joined the note page (it opened "✎ Dictate a note" instead of the newest note, on the old code and the new); it counts the control rows now.
- Tests: `FirmwareText.test.ts` (measured pages fill to capacity, keep every word and paragraph break, cut only a word longer than a page, respect the unit cap; fitted prompts end in an ellipsis within their lines; the navigation body measures as the firmware wraps it). Plugin 1043 tests in 57 files, typecheck clean; Mac 3042 passed, 1 skipped, mypy clean. Simulator: the transitions, planning, panels, messages and quicknotes probes record zero image tiles on every page but the overview, the month and the map.

## 2026-10-05 — Build JOAT 0.1.97 navigation-follows package

- Rebuilt the existing JOAT package from `36ee9b9` with the same `scripts/pack.sh`, the maintainer's same manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. The pack script's version guard confirmed 0.1.97 above the last recorded build. Release-candidate validation, the release manifest, TypeScript checking and the production Vite build passed. `evenhub` CLI 0.1.14, `--sdk-ver 0.0.14`, minimum Even App 2.2.9.
- Canonical `apps/hub-plugin/joat.ehpk`: 165374 bytes, SHA-256 `dcad799dfa773afc57ce847ca8836666b39614526f48a60080f6862b9a96d156`. Carries the following guidance page, the tile list, the tile-less pane, the fix tolerance, the stream retry and the Places tab; the Mac side (tile prefetch, the `places` uplink, `saved_places`, the Navigation page rows, place resolution) is in the brain and needs a restart. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-05 — Navigation follows the wearer; saved places

- Fixes five causes of stalled navigation visuals. The guidance page previously repainted only when the maneuver or the ROUNDED distance changed, so over a kilometre the dot, the progress bar and the ETA froze for a hundred metres at a time. The street tile was fetched once at the origin and never again (the 60 m refresh rule had no caller, and the Mac receives no positions during guidance by design), so the route moved off the initial tile within a few hundred metres. Without a tile there was no map at all: the "bare route shape" the comments promised was unreachable. A fix the host reported without an accuracy figure, or with a timestamp in seconds, was rejected as uncertain for the whole walk. And a route could be acked as installed while the location stream had never started, leaving "Starting navigation" on the glass forever.
- Now: a fix that moved the wearer repaints the page, at most once a second, coalesced to the newest fix, and the Mac hears nothing about it (only semantic states cross, the privacy boundary unchanged). The Mac prefetches street tiles along the whole route the moment it is known (a centre every ~180 m of road, at most 24, the origin tile first, sized to the taller pane), and the plugin draws the one nearest the wearer, so the map follows them; a tile that arrives late repaints the page it is for. Without a tile the pane draws the route's shape around the wearer at the same zoom, fit to the whole route before the first fix. A fix without an accuracy figure is trusted at the policy's limit; a timestamp's unit is read against its arrival time. A navigation consumer retries the stream start, and a route the stream cannot serve is rejected, so the Mac's "Navigation could not start" page shows instead of a wait. The first fix of each route logs its shape (which optional fields the host fills, no position).
- **Saved places.** The phone page has a Places tab: Home, Work and the rest, each a name plus an address and/or the phone's pinned location ("Use my location"), mirrored to the Even host's storage and sent whole to the Mac on every change and every connect (`places` uplink, bounded on both sides, kept in `saved_places`, migration 14). The glasses' Navigation page lists them first, one tap from a route; "JOAT, navigate home" resolves on the Mac to the pinned coordinates without a provider search, or searches the saved address under the place's name when that is all it has.
- Tests: the follow pace, the late tile and the nearest tile, fixes without accuracy and in seconds (plugin); the stream retry; the tile-less pane; the route centres and the prefetch flow, the places wire, store, session resolution and page rows (Mac). Both suites pass (plugin 1161, Mac 3042); mypy clean. The guidance pane with a real route is pinned by unit tests; the simulator cannot supply a phone fix stream.

## 2026-10-05 — Build JOAT 0.1.96 icon-spine package

- Rebuilt the existing JOAT package from `21811d3` with the same `scripts/pack.sh`, the maintainer's same manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. The pack script's version guard confirmed 0.1.96 above the last recorded build. Release-candidate validation, the release manifest, TypeScript checking and the production Vite build passed. `evenhub` CLI 0.1.14, `--sdk-ver 0.0.14`, minimum Even App 2.2.9.
- Canonical `apps/hub-plugin/joat.ehpk`: 161791 bytes, SHA-256 `fee6635c6f0c058c148a333a5471e31891f60ada84a2b792298c4fa3501f058f`. Carries the 24 px icon spine, the one live header line on every firmware page and the status model; nothing changed on the Mac side. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-05 — A smaller spine with icons, and one live header line on firmware pages

- Compacts the top header to a 24 px icon spine (was 36 px): smaller clock, page title beside it, and compact microphone and battery icons on the right tile — a slashed mic for a closed mic, the Phosphor cell matching battery level, a charging glyph while charging, and numeric percentage only at 15% and below. Every page's content moved up with it: eleven dense list rows, the live map 12 px taller, the reader, Pomodoro, Planning, confirmation, question, navigation, Brainstorm and terminal rows all 12 px higher.
- Firmware pages spend one line on the same facts in the same order, `9:41 · POMODORO · ○ · ▇` (● and ○ for the mic, ▁ to █ for the battery, + for charging, the number from 15% down), where they spent two — a status line in words and a separate title line. The firmware list shows five rows instead of four. The line is live: a clock, mic or battery change on a firmware page is one in-place upgrade of the header container (the terminal page's head line included), never a rebuild and never a tile; until now it was dropped, so a list opened at 9:41 still said 9:41 at 10:05. When the line overruns the glass the page's word is shortened, never the clock or the glyphs.
- The status is data now (`HudStatusModel`, `HudStatus.ts`): Chrome hands the presenter clock, link, mic, battery, charging and the Mac's transient word; the raster spine and the firmware header both draw from it, and nothing parses words back into facts. A page keeps its name while the wearer talks (it used to read "JOAT · LISTENING" the moment the mic opened), and the plugin's own "listening" / "mic off" announcements count as rest.
- `DESIGN.md` records the spine and the second icon exception; `docs/HUB_PLUGIN.md` and `docs/HUD_DASHBOARD.md` follow. Simulator captures (overview, News list and article, Pomodoro, Planning month, week, day and event, the due page, Messages review, the navigation frames) show the header on every page. Tests: the status model and glyphs, the live header on a firmware list, a text page and the terminal page, the spine's icons by level and charge, the fitted header, and every moved anchor; the plugin suite passes (1151).

## 2026-10-05 — Build JOAT 0.1.95 due-tap package

- Rebuilt the existing JOAT package from `7aa4ef6` with the same `scripts/pack.sh`, the maintainer's same manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. The pack script's version guard confirmed 0.1.95 above the last recorded build. Release-candidate validation, the release manifest, TypeScript checking and the production Vite build passed. `evenhub` CLI 0.1.14, `--sdk-ver 0.0.14`, minimum Even App 2.2.9.
- Canonical `apps/hub-plugin/joat.ehpk`: 160152 bytes, SHA-256 `152085e32a7658ac2db43aef7c512298e24125b7f4d6964cd5e3e45d50d6b0a5`. Carries the `reminder_due.complete` tap and the "tap: complete" hint; the Mac side (the gateway binding and the monitor's completion) is in the brain. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-05 — The due page completes by tap

- The pushed due Reminder page was the last page whose hint named a word to say. Its tap now sends `reminder_due.complete`, carrying exactly the opaque occurrence and attempt UUIDs and the connection generation the Mac bound the page to when the plugin acknowledged it. The gateway admits the tap only against that accepted page, once, and never after the socket it was shown on; the task monitor resolves the occurrence to its native item and runs the same confirmed completion the spoken "complete reminder" runs, so the glasses still ask before anything is written. The hint reads "tap: complete · Back: double-click to Planning".
- Tests: the wire shape and its rejections on both sides, the gateway binding (one tap, one occurrence, wrong generation or attempt or socket dropped), the monitor's completion path, and the menu's tap on a due page versus any other productivity page.

## 2026-10-05 — Build JOAT 0.1.94 firmware-modals package

- Rebuilt the existing JOAT package from `3fe45d1` with the same `scripts/pack.sh`, the maintainer's same manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. The pack script's version guard confirmed 0.1.94 above the last recorded build. Release-candidate validation, the release manifest, TypeScript checking and the production Vite build passed. `evenhub` CLI 0.1.14, `--sdk-ver 0.0.14`, minimum Even App 2.2.9.
- Canonical `apps/hub-plugin/joat.ehpk`: 160045 bytes, SHA-256 `384aa955acc33f96e766c138beff4e3cd0e108f7303d8f054f03793b1c11810d`. Carries the firmware receipts, outcome strips, productivity pages, pre-route navigation frames and the remaining firmware titles; the Mac side (a reader with a tap action is selectable) is in the brain. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-05 — The modals a tap lands on are firmware pages too

- The wearer's point: fixing a page is not enough when the page its tap redirects to is still pixels. A map of every HUD surface and every transition between them (tap, Back, scroll past an edge, Mac push) found thirteen raster targets one gesture away from a firmware page and one presenter fault: from a firmware text page, Home, a receipt, a productivity frame or a navigation frame never repainted the glass at all, because only the firmware-list owner triggered the image rebuild. Every native content owner now recreates the four image containers before an image paint.
- A receipt or an outcome strip over a firmware page is a firmware page: one rebuild in, and the list or reader comes back as the page it was when the strip settles. Before, a saved reminder over the Planning day rebuilt the image page and repainted the list as pixels under a two-tile strip, and a receipt over a text page was invisible.
- A Calendar or Reminders page (failed, uncertain, due) is a firmware text page, paged in place. Every navigation frame before the live map (confirm the place, repair, mode, preview, the warnings, arrival, error) is a firmware text page whose cursor moves upgrade the page in place; only the guidance frame with its map is pixels.
- The Messages result, draft review, compose, setup, access and retry pages, the QuickNotes recorder, the saved Brainstorm reader, `ACTIVITY · EMPTY`, the Planning refresh and reminder notices, the Planning loading page and every timeout page joined the firmware sets. A reader with a tap action is now selectable, so "tap: continue" on a saved Brainstorm reaches the Mac (it never did). The legacy review's staged return keeps the conversation's firmware-list shape. Back on a resting Brainstorm frame is Home. Text pages say "double-click" like every other page.
- `scripts/simulator_transitions_native_probe.py` drives all of it from a firmware list in the official simulator and records zero image tiles on every step but the map. Still pixels by design: the overview, the Planning month grid, the live map. Still a voice hint: the due reminder page, until its tap completes the reminder.
- Tests: new presenter cases for receipts, strips, productivity pages, Home from a text page and the navigation frames; the navigation replay pins that only guidance follows tiles; `navigationNativeBody` has its own file. Both suites pass (plugin 1131, Mac 3027).

## 2026-10-05 — Build JOAT 0.1.93 control-surface package

- Rebuilt the existing JOAT package from `2e59869` with the same `scripts/pack.sh`, the maintainer's same manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. Release-candidate validation, the release manifest, TypeScript checking and the production Vite build passed. `evenhub` CLI 0.1.14, `--sdk-ver 0.0.14`, minimum Even App 2.2.9.
- Canonical `apps/hub-plugin/joat.ehpk`: 158372 bytes, SHA-256 `f5a83d81dba1440100c000f4be4e1d1691118a8d339260cb12e751eda3d5f5d1`. Carries the `capability` prompt owner kind (a tapped row's question cannot reach an older plugin), the Pomodoro and Search tab copy, and the raster fallbacks without voice instructions. The Mac side (control rows, dictation, the Pomodoro lengths) is in the brain. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-05 — Pages opened by hand are control surfaces

- The wearer's rule: voice works from anywhere, so a page opened on the glasses is for hands. No page shows a "Say “JOAT, …”" row or empty state any more; each offers the tap that does the thing, and a tapped row asks its own question on the lens through the acknowledged prompt (`PromptedSpeechRuntime.ask_capability`, prompt owner kind `capability` on both sides of the wire, drawn by the same native question page the Messages body uses) and routes the answer.
- Navigation: "➤ Navigate to a place" asks "Where to?" and starts the route. QuickNotes: every page leads with "● Record a voice note" (a tap-started capture titled by the clock) and "✎ Dictate a note" ("What should I note?", saved as exact text); the page holds 16 notes so both fit the firmware list's 20-row cap. Messages: the conversation ends in "✎ Reply", the same prepared reply the spoken word starts, and the reader's hint no longer names a word to say. Search: the tab's last row "⌕ Look something up" asks for the query and republishes the tab. Brainstorm's idle frame says "Tap to speak a topic."; its picker already carried "+ New brainstorm".
- The expanded Pomodoro tab shows its controls and, while idle, the three lengths as rows ("Focus: 25 min", "Short break: 5 min", "Long break: 15 min") that cycle through presets on tap and persist across restarts (a new `pomodoro_lengths` table, migration 13; an older database loads the classic cycle). The plugin's empty copy for the Pomodoro and Search tabs, the raster Pomodoro card and the raster Brainstorm fallback stopped instructing voice.
- Tests: the Mac suite passes with the new navigation, dictation, recording, reply-row, search-control, prompted-speech and pomodoro-length cases; the plugin suite passes with the new prompt owner kind. The installed package must be 0.1.93 or later for a tapped row's question to reach the glasses: an older plugin rejects the `capability` owner kind.

## 2026-10-05 — Build JOAT 0.1.92 confirmation-window package

- Rebuilt the existing JOAT package from `c9db4cd` (version bump on top of the confirmation-window, streamed-frame and green-suite commits) with the same `scripts/pack.sh`, the maintainer's same manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. Release-candidate validation, the release manifest, TypeScript checking and the production Vite build passed. `evenhub` CLI 0.1.14, `--sdk-ver 0.0.14`, minimum Even App 2.2.9.
- Canonical `apps/hub-plugin/joat.ehpk`: 158371 bytes, SHA-256 `1ae1ddd9123822512f4aad67c77aa8366410a3454c5d03184cf59c3af402b784`. Carries the plugin side of the confirmation window (`confirmationDecisionWindowMs`) and the restored right/ring double press for an armed Brainstorm run; the Mac side (20 s envelope, streamed-frame throttle) is in the brain, not the package. Not uploaded to Even Hub, not installed, not verified on physical glasses.

## 2026-10-05 — Confirmation deadline, streamed-frame throttle, green plugin suite

- The confirmation round trip no longer outruns the Mac. The Mac timed a challenge 12 s from send while the plugin always gave the wearer 8 s once the gate was visible and sent an acceptance only after restoring the page beneath, so a tap the wearer saw land could come back as "Reply cancelled.". The Mac envelope is 20 s for Messages replies and calendar/reminder writes (`CONFIRM_TRANSPORT_SECONDS`, `_CONFIRM_SECONDS`), and the plugin derives its decision window from the Mac's `expiresAtMs` (`confirmationDecisionWindowMs`: four seconds reserved for the restore, clamped to 3-8 s so a skewed phone clock never cancels a visible gate).
- Streamed answers re-sent the whole transcript per model chunk. The orchestrator coalesces "answering" frames to one per 200 ms (`STREAM_PUBLISH_INTERVAL_S`); the first chunk and the final frame are never held. Two tests drive it with a fake clock.
- The plugin test suite is green: 1097 passed, 0 failed, from 84 failures that predated today's work and had been tolerated as a baseline. Eighty-three were stale assertions on behaviour the Changelog documents as deliberate (double-click is Back on every source, the structured clock · MIC · battery status line with the state word in the spine, Settings as phone guidance on a firmware list, verified productivity outcomes returning to Planning at once, firmware lists and text pages in place of raster readers, the delivered-tile cache, the single-writer boundary after a host timeout). Stubs now answer `usesCustomListSelection(next)` the way the presenter does, the MessageUI and QuickNotes cases reuse the canvas measurement stub, and the stitched-preview test checks its pixels in one loop instead of two million expects. One was a real regression: routing every double press through one Back owner (2026-09-11) dropped the only caller of the armed Brainstorm acceptance, while the Mac still requires a named right-temple or ring double press to start a diagnostic run. `GesturePolicy` names the source of a system double press again, `EventRouter` consults the armed owner for a right or ring press before cancel and Back, and the ready frame says "right/ring x2: test" only while the Mac has armed it. A dashboard test that asserted a free-day tap leaves the pane unfocused passed only because it never awaited the queued paint; it now pins the Open-day design.
- `docs/HUB_PLUGIN.md`, `docs/TESTING.md`, `docs/HUD_DASHBOARD.md`, the dashboard canvas and a page-builder docstring still said the right temple opened the exit dialog and that menu id 3 was a Refresh row; they now say what the code does (exit only through the Mac's `shutdown`; id 3 opens the Settings guidance for a cached menu).

## 2026-10-05 — Build JOAT 0.1.91 firmware-pages package

- Rebuilt the existing JOAT package from merged main (`b080f48`, version bump only on top of the firmware-pages merge) with the same `scripts/pack.sh`, the maintainer's same manifest and the reviewed MacBook-first, Mac Mini-second private WSS candidates. Release-candidate validation, the release manifest, TypeScript checking and the production Vite build passed. `evenhub` CLI 0.1.14, `--sdk-ver 0.0.14`, minimum Even App 2.2.9.
- Canonical `apps/hub-plugin/joat.ehpk`: 158173 bytes, SHA-256 `03b3a6f10b1aaa36e0cdf194cdb5f448ae9ed02391655975a41e95ad19587c91`. Not uploaded to Even Hub, not installed, not verified on physical glasses; the glasses still run 0.1.88.

## 2026-10-05 — Even Hub skills evaluation and the first dedupe slice

- Audit against the official `everything-evenhub` skills and SDK 0.0.14 types in `docs/audits/2026-10-05-even-hub-skills-evaluation.md`: unwired frames and rows, raster surfaces that duplicate native containers, why the assistant feels slower than Even AI, and the gates behind an unsent Messages reply. The installed package is still 0.1.88; 0.1.90's native conversation has not reached the glasses.
- Plugin test doubles gained `usesCustomListSelection`, taking the suite from 151 to 88 failures; the remaining failures predate this work and are unchanged.
- The spine draws the Mac's state word again ("hearing you…", "reconnecting") inside `SPINE_LABEL_BOX`: since 2026-09-11 the renderer returned an empty label and the lens showed nothing during speech-to-text. One top-left tile per state change. Three renderer tests that had kept asserting the word pass again.
- `scripts/setup_even_ai_door.sh` does the Mac half of pointing Even AI's Agent Configure at the brain: verifies the loopback door and its token, mounts `/agent` on the tailnet beside `/glasses`, checks the mount, Funnel and the tailnet answer, copies the token and prints the URL. `--probe` sends one real question.
- The confirmation gate ("Send to X?", calendar and reminder writes) is a firmware text page whenever the visual surface can hand the page over under its transition barrier: one `rebuildPageContainer` instead of four forced image tiles in, and the four image containers come back on dismissal. A visual surface without that barrier keeps the exact raster gate. Verified in the official simulator (Linux build under Xvfb) with `scripts/simulator_messages_native_probe.py`: the gate paints no image tile and is reported visible 0.05 s after the reply command, against 0.47 s and four tiles before; the full reply flow still reaches the recording sender.
- JOAT answers over the visual HUD (command results, errors, capability answers; `assistant.overlay` with the `answer` presentation) are a firmware text page under the same barrier: one rebuild in, in-place `textContainerUpgrade` for paging (seven firmware lines per page, measured with `@evenrealities/pretext`) and streamed appends, and the four image containers return on dismissal. Receipts keep the two-tile image strip. Probe run: the result card after a send paints no image tile (one four-tile frame in, four out before).
- `scripts/simulator_messages_native_probe.py` drives picker → conversation → reply → confirmation → tap → result → Back through the simulator automation API and records which steps paint image tiles. The DEV hook `?joatTestOpenMenu=` now also accepts a capability name.
- The Messages picker and Activity join the Agent Terminal picker on the firmware list (`FIRMWARE_LIST_TITLES` in `CapabilityMenu`): one rebuild instead of four image tiles to open, zero tiles per scroll. The firmware list page now carries the live status line in its rebuild. `usesCustomListSelection(next)` answers for the page about to be shown, so a raster conversation opened from the firmware picker keeps its own cursor; the text compatibility surface hands the page back after a firmware list instead of skipping its writes. NEWS and SEARCH stay raster for the rail and row memory. Simulator and physical-glasses verification have not been run for this slice.

- The Messages conversation thread is a firmware list. The firmware list reports taps but no scroll, so the Mac pages the thread: `hud.list` gains `pageControls` (`newerIndex`/`olderIndex`, mirrored in `HudList.page_controls`), the Mac appends "▲ Newer messages" and "▼ Older messages" rows, and a tap on either becomes the page request the raster thread used to send on a swipe. The first row names the slice ("Latest messages", "Older messages"); the thread opens on row one because the firmware has no cursor-setting call. Probe run: the thread, both pages, the gate, the result and Back painted no image tile, and the older page followed the tap in 0.2 s.
- Firmware list rows are clamped to 63 UTF-8 bytes in `listLayout`. The 0.9.5 simulator's host returns a silent `false` from `rebuildPageContainer` for any row of 64 bytes or more, which closed the thread to Home; the cap was found by bisecting row lengths, and a test pins it.

- The message reader (`MESSAGES · MESSAGE`) and the question page ("What should I say?") are firmware text pages. The reader joins the Mac's rows, pages them for the firmware font and pages in place on swipe; past the local edge the Mac's own footer still decides and the same page request goes out. The question page replaces the raster `paintExact` prompt under the transition barrier and hands back to whatever was beneath it (image page, firmware list or terminal). Back from the reader returns the thread as the firmware list it was shown as; before, the return path dropped the opt-in and redrew four tiles. Probe run: every Messages step from picker to result and back painted no image tile, and the question's receipt reached the Mac 0.01 s after it was sent. `?joatTestPromptMic=1` (DEV only) lets the microphone-less simulator keep a question on screen.

- Brainstorm works from the glasses again and is drawn by Even's own containers. Three Mac-side faults made it "do nothing": `find_codex_binary` looked only on PATH, so a brain started by launchd never found the ChatGPT-bundled `codex` and reported the seat missing (it now checks the user symlink, the package-manager prefixes and the app bundle; `find_claude_binary` gains the native-install paths); the idle row promised "Ready for a question" even with a seat missing (the host's unavailable frame now shows first, naming the missing CLI); and the idle, NEW and unavailable frames were sent without the rebuild that lets the plugin claim the surface, so a voice-opened Brainstorm showed nothing and its tap went nowhere. On the glasses the picker is a firmware list and every Brainstorm frame rides the Agent Terminal's two-text page: one rebuild in, one `textContainerUpgrade` per revision, swipe paging on the glasses, no per-second counter. Probe run (`scripts/simulator_brainstorm_native_probe.py`): zero image tiles from picker through ready, listening, a streamed run and the settled synthesis; the tap on the ready frame reached the Mac as `begin_topic_capture`.

- QuickNotes and Notifications are firmware lists, and their readers (a note, a notification's detail), the Planning entry page and the Navigation instructions are firmware text pages. The note archive pages through its own "▲ Newer notes" / "▼ Older notes" rows (`pageControls`, as the Messages thread does); a tap on either sends the page request the edge swipe used to. The Mac's note page shrank from 20 to 18 rows so both paging rows fit the list's 20-row cap. Back from a reader returns its list as the firmware list it was. A spoken "show notifications" is now adopted like the menu capabilities instead of being dropped. Probe run (`scripts/simulator_quicknotes_native_probe.py`): zero image tiles across the list, both archive pages, the note, Back, the Notifications list and a card's detail.

- The NEWS, SEARCH and POMODORO tabs expand to firmware lists, headlines and results open on firmware text pages, and the Settings page is a firmware list. The expanded Pomodoro no longer uploads a tile every second: its first row carries whole minutes and the page is rebuilt once a minute; the row is inert and the Mac's control rows keep their own indexes. Row memory on Back is gone with the raster list (the firmware has no cursor-setting API). Probe run (`scripts/simulator_panels_native_probe.py`): zero tiles on each expanded tab, the article, its paging, Back and the minute rebuild; the overview's own rail swipes are the only tiles. The Planning month grid and its day/week lists are the last raster capability pages.

- Two of the audit's dead frames are driven now. `joat --imu-trace PATH` arms `imu.control` and `imu.trace` on every hello and appends the glasses' raw samples to PATH as JSON lines, which is the calibration path docs/NEXT_STEPS.md §5 described as "already wired" while nothing on the Mac sent the downlinks. The Mac remembers the plugin's resume-policy report and, under "Mac decides", answers a worn report with the mic off by asking for ears; before, that policy left the mic off for good. `policy.set`, `shutdown`, the conversation dismiss and `show_brainstorm` stay as hooks without a caller, listed with their reasons in docs/HUB_PLUGIN.md. Two plugin exports nothing called are gone, and `.env.example` and the HUB_PLUGIN pixel note say what the code does.

- The Planning week and day pages are firmware lists. A Planning page with the Mac's row map and no month grid opts in; every row was already a Mac action by index and Back was already the Mac's, so the wire is unchanged. The month dashboard (grid plus hovered-day pane) is the one raster capability page left. Probe run (`scripts/simulator_planning_native_probe.py`): month → week → day → event and Back, zero tiles on every page but the month.
## 2026-09-24 — The conversation card matches Even AI's own

- Measured Even's product-page demo frame by frame and rebuilt the assistant conversation card to it: one hairline rounded box (borderWidth 1, borderColor 8, borderRadius 10 — the firmware default border color is 0, invisible, which is why the earlier border "removal" changed nothing on glass and why restoring the width alone would have drawn nothing), the wearer's question dim over the bright answer, and the state glyph centred on the card's bottom border. The continuation prompt is a sentence, so it alone moves inside the card.
- Removed the hand-rolled scroll-position column (█/│ glyphs): Even AI has no scrollbar — older lines leave through the top. The page is now 4 containers; scrollback still pages the window.
- The card hugs its content and grows upward from a fixed bottom, as in the demo: NATIVE_LINE_STAGES = [2, 3, 5, 7]. A stage crossing is one rebuild; streaming inside a stage stays in-place text upgrades.
- Verified on the official simulator against the live sandbox brain: two voice turns painted the bordered card, the follow-up appended below a blank separator, and the card grew from 3 to 7 lines without moving its bottom edge. Vitest: no new failures against HEAD.

## 2026-09-22 — Every tab flow on firmware text, within the firmware's limits

- Found why the expanded News list and the News card failed and the overview repainted over them: the host rejects a page whole when a text container exceeds 999 UTF-8 bytes or a list row exceeds 63 bytes / 20 rows (Even simulator README v0.7.1, v0.7.3, "to behave more like firmware"; simulator log "validation failed … exceeds limit"). Added `FirmwareLimits` clamps on every text container, upgrade and list row, and a `[page-rejected]` diagnostic naming the offending container.
- Opened articles, results and notification details are now a firmware text page (status, head line, scrolling body, hint) instead of the last four-tile canvas page: open costs one rebuild, a swipe inside the page costs nothing, the boundary swipe turns the Mac page.
- Leaving a firmware list for Home is one rebuild (was an image-page rebuild followed by the overview rebuild, with a blank flash).
- The overview's tab rail is the capture container; the pane is a six-row single-line preview, so a long card no longer swallows swipes. A change to one band (a Pomodoro tick, a fresh headline, a streamed reply) is one in-place text upgrade rather than a page rebuild; the resting Pomodoro card shows minutes left, block and cycle, and the expanded page carries the countdown in its title, one upgrade per second.
- One gesture vocabulary on every page — tap, swipe, long-press, double-click — normalised at render (`GesturePolicy.formatHint`): "hold", "down more", "up previous", "x2" no longer appear.
- News feed summaries decode HTML entities and keep one related headline per row; the Google News fallback no longer shows literal `&nbsp;`.
- Measured on the official simulator with the live sandbox brain: home → News → expand → open → page → back → home is 7 rebuilds/upgrades and zero image uploads.

## 2026-09-13 — Build JOAT 0.1.90 native conversation update

- Built the existing JOAT package from committed source `f068ebd` in an isolated export. Eight release-candidate and release-manifest checks, TypeScript checking, and the production Vite build passed. The package keeps the maintainer's package ID, minimum Even App 2.2.9, SDK 0.0.14, and the reviewed secure MacBook-first, Mac Mini-second Tailscale candidates.
- Canonical `apps/hub-plugin/joat.ehpk`: 154719 bytes, SHA-256 `8dc5dfe8157845b16a795734cfea2dcf886f30dacb3c83c2836ac91864f7b9d1`.
- Official simulator acceptance was completed separately against the committed implementation: 70-screen OpenClaw/Hermes conversation flow, 48-screen lifecycle, 42-screen Hermes streaming/restart recovery, and 18-frame assistant conversation. The package has not been submitted to Even Hub, installed in Even App, or verified on physical glasses.

## 2026-09-13 — Native conversations, working terminal gestures and nearby-place repair

- Replaced the four-image assistant and Agent Terminal documents with fixed Even-native text pages. Assistant thinking, streaming, continuation and transcript movement use in-place text upgrades; each selected terminal provider uses one complete native body update. The Agent Terminal chooser now contains only OpenClaw and Hermes, and recovery, prompt, working and Back labels describe the action the current gesture owner accepts.
- Preserved independent reading anchors for OpenClaw and Hermes through provider switching and WebView restoration. Up and Down report success only when the visible transcript actually moves. The provider picker is a firmware-owned list, and transitions between picker and terminal no longer expose serialized image quadrants.
- Interpreted explicit nearest/near-me navigation as a fresh phone-location search, strips proximity filler before Mapbox, ranks matching POIs and brands by distance, accepts a narrow in-session correction such as “I meant Shell, the gas station,” and reports when current location is unavailable. Mapped guidance keeps its changing progress/footer inside the left image column to avoid an unrelated fourth tile write.
- Official isolated simulator journeys passed against current source: 70-screen conversational OpenClaw/Hermes flow, 48-screen task lifecycle, 42-screen streaming/restart recovery and 18-frame assistant conversation. Both providers completed request/reply/Back flows; real swipes changed settled framebuffers and reversed to byte-identical tail pixels. Active assistant/terminal visits issued zero image writes. Live Mac logs independently showed the removed path spending roughly 0.2–1.4 seconds per serialized tile. No live engine, phone, package, installed build or physical glasses were used for simulator acceptance.

## 2026-09-13 — Bounded Mac shell command channel

- Added anonymous shell/child pipes with per-launch identity, UUID requests, engine admission and completion acknowledgements, bounded framing/deadlines/replay state, cancellation/supersession fences, EOF handling and live capability expiry. Command descriptors are not inherited by engine subprocesses. Hub phone session, pairing and 401/uplink paths are unchanged.
- News refresh calls the existing NewsService and panel publication owner; cancelled fetch batches retain their lock until worker threads drain and cannot commit retired results. Service retry calls the existing dedicated Hermes/OpenClaw owner without restarting the engine or changing the glasses view. Local retry cancellation preserves previously observed connection status.
- Added the News refresh and Intelligence service retry controls with configured, available, active, denied, disconnected and pending facts; status-file observations never authorize commands. Source and hostile boundary review completed with focused Python/Swift coverage and fixes for backpressure, inherited stdin, stale acknowledgements and cancelled thread draining.
- Validation: 168 Python tests passed across command, News, pairing and full Hub-gateway suites; 95 XCTest and 4 Swift Testing tests passed, with the final 15 command tests rerun after receipt-copy refinement. Focused Python mypy is clean. A pre-existing main-module callback typing error remains in the notification on_change lambda.
- Developer ID universal arm64/x86_64 archive/export and strict signature verification succeeded. With the user-approved Accessibility fallback, the signed foreground dist/JOAT.app showed News admission and partial-completion acknowledgement, service retry completion, and cancellation uncertainty. Captured and inspected News/service result screenshots. Fixed valid saved configuration being mislabeled absent when the runtime owner could not obtain credentials, with four regression cases. No new simulator or physical-G2 proof. No .ehpk packaging/upload.

## 2026-09-13 — Build JOAT 0.1.89 for existing Even Hub project

- Built current committed reconnect fix from source `ccd3185` in an isolated export. Eight release candidate/manifest checks, TypeScript and production Vite build passed. Preserved the maintainer's package ID and secure MacBook-first, Mini-second release candidates; explicit selections remain pinned.
- Canonical apps/hub-plugin/joat.ehpk: 150738 bytes, SHA-256 `76a1c04d0417fa8a8525aeab6470c6c62486b46c7ee6c55662b357cc9d29701f`. Minimum Even App 2.2.9 and SDK 0.0.14.
- Portal submission has not been attempted: Computer Use reports the Mac is locked. User authorized adding a build inside the existing JOAT project; no new portal project, public release, installation, pairing or physical connection change.

## 2026-09-13 — Keep an explicit Mac connection selected

- Pin a manually selected or remembered approved Mac through dial, authentication and disconnect failures. Discovery can still rotate before a choice; an explicit choice no longer silently fails over to another computer. Backoff applies per retry for pinned targets.
- Save the explicit target before authentication completes and prefer that local intent over a stale host mirror on relaunch. Credentials remain isolated by origin. Explain when the dropdown is only a pending choice and Connect must be pressed.
- Four new regression cases reproduced rotation before the fix; all 73 focused transport/pairing tests and TypeScript checking pass afterward. Browser inspection with production MacLink/PhoneControlPanel and two disposable local WebSocket servers verified unavailable MacBook retries never reached the available Mini, relaunch retained the MacBook, bringing it online connected successfully, and an explicit switch back worked. This fixture exercises the phone panel, not physical glasses or a full Even Hub startup.
- A separate read-only HTTPS request reached the actual MacBook and returned its closed-pairing-window response. Its log contains the same refusal at 20:31 on September 12. No pairing state or physical connection was changed. This source fix requires a new plugin package before the phone uses it.

## 2026-09-12 — Hermes streaming and durable Agent Terminal recovery

- Observe Hermes JSON-RPC SSE task/status/artifact events, including fragmented UTF-8, append/replacement updates, bounded frames and owned-task resubscription. Keep one assistant message identity across partial and final output. OpenClaw retains rate-conscious task polling; uncertain sends are never replayed.
- Save explicit provider identity in native connection settings. Describe each provider's remote tool policy and distinguish invalid credentials from task authorization. OpenClaw peer credentials cannot approve operator actions, and the reviewed Hermes A2A interface has no approval-decision RPC; JOAT exposes no fabricated approve/deny action.
- Persist whole bounded transcripts, context, task identity and dispatch receipts atomically in the existing leased SQLite store. Restore active work as Check task status. Drain local work and pending preparation on shutdown without sending remote cancellation; new captures and queued input never survive an engine restart. Recovered completion replaces stale working/authorization guidance.
- Final isolated official simulator acceptance passed with 40 screens and 208 matching production source hashes: both agents complete requests, preserve Back/reading position, restore accepted tasks after reconstruction, and resolve external authorization by querying without resending. All owned ports and the temporary TLS key were released. Receipts and exact boundaries are recorded in docs/plans/agent-terminal-task-lifecycle.md.
- Independent review closed SSE boundary/identity issues, receipt reconciliation and shutdown races. Validation includes 544 focused Mac tests (with subprocess crash/restart boundaries), six-file Python type checking and four native A2A settings tests. Physical glasses, phone pairing and the installed engine were not used.

## 2026-09-12 — Consistent prepared reply follow-up

- Apply the same guarded successful-recipient restoration to prepared and legacy selected-conversation replies. Preserve original expiry, context generation and fresh confirmation requirements.
- Official simulator verified accepted prepared reply, readable result, Back, a fresh follow-up body prompt without recipient reselection, prompt cancellation and return. One disposable sender invocation; no real message sent. All owned runs closed.

## 2026-09-12 — Readable prepared Messages results

- Replace 24-character receipt slicing with the existing full-width JOAT regular-prose reader and paging. Preserve terminal action identity and double-click return to the conversation.
- Official simulator with production prompt admission, prepared reply controller and disposable sender verified the complete long-review acceptance path, readable submission result and Back. No real message was sent. Partial transitions and short-confirmation transition stress remain unresolved.

## 2026-09-12 — Keep a successful legacy reply in its conversation

- Restore the original recipient context after successful legacy submission only while its selection generation and original expiry remain valid. Each next reply requires a new confirmation; failed submissions and changed contexts do not re-arm a recipient.
- Visually verified official simulator acceptance, receipt dismissal, retired-review rejection, a fresh follow-up review and confirmation timeout with one disposable sender invocation total. All owned runs closed. Navigation/expiry adverse guards still need runtime coverage; package and installed engine unchanged.

## 2026-09-12 — Restore conversation after legacy reply cancellation

- Consume the completed voice draft review into the retained conversation when its confirmation claims the screen. Stage the return snapshot synchronously without an intermediate paint; preserve the prepared MessageUI path.
- Official simulator replay against frozen 0.1.88 source plus this repair verified long review, cancellation, receipt dismissal, opening a restored message and double-click return. Disposable sender calls: zero. All owned simulator/support processes closed. Independent review confirmed the existing draft lock serializes this handoff.
- Acceptance and timeout branches still need candidate visual coverage. Partial-frame transitions remain unresolved; no new package or upload in this slice.

## 2026-09-12 — Agent Terminal task progress and Stop/Reply lifecycle

- Project accepted task status separately from answer artifacts. Poll snapshots update one retained status record, repeated snapshots do not duplicate transcript output, and final answers enter once. Input-required questions take priority over draft artifacts and remain in the same-task continuation context.
- Show pending Stop confirmation, confirmed remote cancellation, and unconfirmed outcomes distinctly. Bind Stop to its original local task and agent; preserve a known completion and never cancel a later replacement task. Progress-record eviction cannot interrupt remote polling.
- Show Check task and a matching status-check action for uncertain owned tasks instead of falsely reporting Offline or Connection failed. The new optional taskRecovery wire field keeps old frames compatible.
- Adapter and simulator specialists implemented typed updates and an isolated task-state fixture. Independent review found and closed stop ownership/final-paint races, clarification-history loss, progress eviction, and a recovery predicate/action mismatch. Focused diagnostics: 444 Mac tests, 112 UI conversation/input tests, the targeted recovery-label renderer check, TypeScript and Python type checking. Broader renderer/presenter suites retain the same 25 baseline failures with no new failing test names.
- Final acceptance is bound to bf2e052: 48 lifecycle screens (ten sends, four cancels) and 47 continuity screens passed against identical 205-file source fingerprints. A shared-source change correctly invalidated an intermediate run; an immutable export completed the final replay. Parent visually inspected progress, same-task question/reply, stop confirmations, and Check task recovery. All isolated ports and temporary keys were cleaned up.
- The uploaded 0.1.88 package is unchanged. Provider SSE, provider-specific tool permissions, and durable restart persistence are separate remaining work; no installed engine, pairing or physical glasses connection is used by this task.

## 2026-09-12 — JOAT 0.1.88 package built

- Built the canonical apps/hub-plugin/joat.ehpk from a temporary export of committed source 482fd0a (completed Agent Terminal implementation 29184ba plus version metadata). Release checks and TypeScript compilation passed; temporary export removed. No developer environment or simulator fixtures were packaged.
- Preserved released candidate order: secure MacBook Pro /glasses route, then secure Mac Mini /glasses route. Minimum Even App 2.2.9 / SDK 0.0.14. Package size 150304 bytes; SHA-256 9a05dbd35002ca9cb159b55c455078ee77cd21906d6a0f77e4c5332e2d1ae4c4.
- Replaced the existing canonical package only. No portal upload, physical installation, pairing change, or installed Mac engine restart performed.

## 2026-09-11 — Continuous Agent Terminal conversations

- Preserve full user and agent messages in a stable per-agent conversation, replacing the one-row-per-paragraph display loss. Retain up to 64 messages / 262144 UTF-8 bytes in memory; evict whole oldest messages with an explicit history notice.
- Keep follow-up input inside the selected conversation, preserve indentation and blank lines, and retain the reading position across Working/Ready, Back/reopen, agent switching, and new replies arriving while reading older text. Input-required tasks advertise Reply needed. Prompt acknowledgement still waits for the complete current question to be painted.
- UI and simulator specialists implemented the client viewport and strengthened acceptance runner; independent review identified and closed a long-token wrapping performance issue. Cached measurements and bounded Unicode-safe fitting prevent repeatedly measuring unchanged history or huge remaining tokens.
- Focused diagnostics passed: 415 Mac domain/protocol/roster/opening tests, 112 UI conversation/input/controller tests, seven prompt-ownership tests, five focused renderer tests, TypeScript checking and three-file Python type checking. Simulator acceptance details and explicit fixture boundaries are recorded in docs/audits/agent-terminal-simulator-flow.md.
- Final official-simulator replay passed 47 captured screens and nine authenticated local-fixture requests: three context-dependent turns per agent, complete long replies, Back/reopen, input cancellation, and new output preserving older-text reading position. The terminal now says Working consistently with the chooser. All 205 source fingerprints matched before/after execution; isolated listeners closed and temporary TLS key removed.
- Full renderer/presenter suites retain 25 baseline failures: an isolated HEAD comparison reproduced the same failing names and first failure messages, with no new failures from this slice. They are not reported as passing.
- This slice does not add provider streaming, restart-persistent history, a session-management menu, or tool-permission negotiation. It does not deploy or change the installed Mac/phone/glasses connection.

## 2026-09-11 — Display recovery integration follow-up

- Independent source review found that an explicit display resume could reopen image submission during a pending native page replacement. Submission and in-flight guards now also require that no replacement owns the display, independently of the resume flag.
- Three failed-feature rollback paths invalidated a restored notification's receipt but never renewed it after a successful direct repaint. They now use one bounded rollback helper, which stamps the same visible notification only after a successful current paint and does not start another recovery cycle.
- Official simulator verification held a real SDK rebuild before dispatch, explicitly resumed the display, and requested a paint: the paint returned false with no image calls during the hold and the prior Home remained visible. Release allowed rebuild and repaint. A separate notification E scenario rejected the new feature paint and recovery, held the rollback upload, and verified an early tap sent no dismissal. After release, E was visually restored, its painted identity renewed, and a tap dismissed exactly E and restored Home.
- TypeScript compilation and whitespace checks passed separately. The fault controls existed only in memory; this task closed the simulator, fixture, and Vite server and verified all three ports released. These controlled scenarios do not prove physical-glasses latency or atomic multi-tile rendering. The previously uploaded package does not contain these changes; the overall implementation goal remains active.

## 2026-09-11 — Agent Terminal chooser and isolated simulator acceptance

- Show only configured OpenClaw/Hermes agents in Agent Terminal; retain legacy Work/Calendar/session owners internally. Entry always opens the same chooser, including during a request. Use explicit connection states and consistent double-click Back/cancel instructions.
- Back retires the conversation binding and returns to the chooser; cancelling a question restores its selected conversation. Reject delayed retired-opening frames and handle cycling from an internal legacy session.
- Independent UI and ownership reviews informed the fixes. The final clean committed-source replay through `f1cf7bf` passed both complete official-simulator journeys, cancellation and busy re-entry; all 14 stable frames were visually inspected. Local TLS agent responses and acknowledged synthetic final speech transcripts are explicit fixture boundaries.
- TypeScript compilation, 255 scoped UI tests and 248 roster/opening tests passed separately. The runner closed its isolated processes/ports and removed its temporary TLS key. No installed engine, phone/glasses connection, pairing or remote credentials were accessed. See docs/audits/agent-terminal-simulator-flow.md.

## 2026-09-11 — Notification replacement tap verification

- In the official simulator, held the replacement notification's real SDK image call before dispatch. With card C still visible and card D pending, a tap neither removed the unseen replacement nor emitted a D dismissal. After release, D rendered and a later tap emitted only D's identity and restored Home. The first attempt armed too late and was excluded; the accepted attempt confirmed the hold before sending D.
- This verifies the identified-card pending-paint tap guard, not the separate failed-confirmation rollback path or native Even notification clearing. The screenshots also exposed an incorrect double-click-menu footer on the Home card; removed that extra instruction so the card advertises only its dismissal action. The copy change was compiled after the run and is not a new visual receipt.
- Closed this task's simulator, fixture and temporary Vite fault controls after verification. All fault controls remained in memory; the existing package was unchanged.

## 2026-09-11 — Bounded SDK timeout simulator check

- Official simulator ran current source with a temporary in-memory Vite transform that held one real SDK image call before dispatch and shortened the diagnostic reporting timeout to 500 ms. Initial wrapper incorrectly intercepted the display proxy and recursed; that run was rejected, stopped and replaced with a wrapper around the actual SDK method. Neither transform was written to source or package.
- Corrected run recorded the held write, upload timeout and failed recovery attempts. No SDK rebuild call was dispatched while the write remained held; the previous Home frame remained visible. Releasing the held write allowed subsequent image calls; root visually inspected restored Messages and double-click back to Home. This verifies the controlled timeout ordering, not normal latency or atomic multi-tile presentation.
- Committed reviewed EventRouter Back/routing hunks as `4ac6c8f`, excluding notification interface/dismissal hunks that still need their targeted visual check. Coordinated that commit with the separate Agent Terminal task after its index was cleared.
- Closed this task's simulator and stopped its fixture and temporary Vite server at the end of the check. No new artifact directory or duplicate package was created.

## 2026-09-11 — Display ownership review corrections

- Committed the shared Back policy, matching conversation hint and explicit Messaging review state as `131edd6`; remaining router/presenter integration is still local.
- Independent review found that a timed-out image upload could outlive its JavaScript paint queue and race recovery's native page rebuild. HudVisualSurface now includes the actual pending SDK write in its idle boundary and suspends new image submissions across page replacement; every HudPresenter native rebuild uses that boundary. An unresolved old write aborts recovery instead of allowing a competing rebuild. Targeted simulator fault-path verification remains pending; atomic four-tile presentation is not claimed.
- A second review found that logical notification replacement could make a tap dismiss an as-yet-unpainted card. Identified cards now require a completed paint identity before dismissal, and confirmation/snapshot rollbacks invalidate the old paint identity until restoration completes. These race corrections remain local and need runtime verification.

## 2026-09-11 — Reviewed implementation commits

- Committed notification cleanup thread ownership and bounded deletion as `889887e`.
- Committed matched Mac/Hub paging and display-context protocol fields plus complete retained voice-note access as `ea010ce`. The new optional input field is appended to preserve existing constructor argument positions. Current TypeScript compilation and a focused in-memory protocol compatibility check passed; earlier simulator journey evidence remains separate from those diagnostics.
- Shared feature controllers/rendering remain under integration review alongside a separate Agent Terminal task. These commits do not establish a completed release or physical-device acceptance. The canonical package was not rebuilt or uploaded during this step.

## 2026-09-11 — QuickNote and Messaging connected regression

- Visually inspected all six pages of a long fixture QuickNote through its final text, reverse paging, second-page Note 21 detail/Back, third-page Note 45 detail, and return to Home. The official simulator used the current plugin and capability router with in-memory fixture data; no saved user notes were touched.
- Inspected a four-page fixture message through its final text, detail Back, older history and return through the conversation picker. Found and corrected outgoing detail metadata saying `To you` instead of `From you`; a read-only direct thread is no longer mislabeled as a group, and its sender fallback retains its named peer. Independent source review checked the real IMessageStore construction paths.
- Selecting New message without an available reply controller now returns setup guidance instead of leaving the plugin waiting for a response timeout. The current Mac source was reloaded in the isolated fixture, and both that guidance and its double-click return were visually checked.
- Partial tile transitions remain reproducible. This is fixture-backed visual evidence, not real Messages sending, installed-engine, or physical-glasses acceptance. No new package copy or upload was made.

## 2026-09-11 — Planning connected-detail follow-up

- Official simulator checks on the current plugin verified rapid Up inputs clamp at the first event; a tap during a three-Down burst opens the fourth event despite subsequent Down inputs; and event detail Back restores the selected parent row. These are fixture-backed simulator journeys, not physical-glasses acceptance.
- Visual review caught uniform emphasis and duplicated Back copy on event details. Day and dashboard event entry points now share one Mac detail helper; the plugin uses the existing JOAT title emphasis, regular secondary metadata and one footer. The updated renderer was viewed in the official simulator with its event-detail Back journey. The Mac helper compiles but is not yet running in the installed engine.
- Reviewed notification observation pruning: the shared SQLite connection stays on its owning thread, with at most 500 settled observations deleted per batch and a yield between batches. This does not establish that the wearer's old notification count is fixed; live engine cleanup remains unverified.
- The existing `apps/hub-plugin/joat.ehpk` remains unchanged by this follow-up. No duplicate package, version folder or deleted `build/` directory was created. These latest local changes are not yet packaged or uploaded.

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

- **Released private Hub build 0.1.84 and visually evaluated Navigation, Messages and Planning.** Implementation `37a776d`, package source `9defa67`; portal submission confirmed after reload. The evaluation reproduced contradictory Navigation states during sequential quadrant updates, documented Planning date-context loss after operations and Messages reading-context/type differences, and retained working menu, paging and fixture action journeys. Obsolete or unrelated captures were rejected; physical-device and real-service behavior remain unverified. No new feature fixes were made. See `docs/audits/2026-09-10-navigation-messages-planning-visual-evaluation.md`.

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

- Corrected the manifest's stale LAN candidate (`192.0.2.10` placeholder vs active LAN address). Probed against the live gateway: the
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
  removed the metered-API path added earlier the same day in `4bae347`.
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
  matches the selected Taylor conversation after **TAP OR SWIPE TO DISMISS**.
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
  controlled simulator run retained Messages → Taylor selection → visible
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
  the maintainer's own project as `v0.1.26`, rather than attempting to upload
  a changed artifact under the already-existing `v0.1.25` version. The fresh
  package was built from clean `0c989a4`, is 72,653 bytes, and has SHA-256
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

- Rebuilt the current integrated Hub source as private the maintainer's own
  `v0.1.21`, 69,397 bytes, SHA-256
  `b213de98185f3f347883ee8c961706638728443cf304d1695c20962a8463b661`.
  This supersedes the earlier `v0.1.20` candidate because the shared source
  changed after it was packaged. Full verification before the rebuild: 348 Hub
  tests, Hub typecheck/production build, and 743 Python tests. It still awaits
  existing-project upload, phone installation, and physical-G2 evidence.

- Built the next canonical private JOAT package from the integrated Hub source:
  the maintainer's own `v0.1.20`, 69,398 bytes, SHA-256
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
  The release owner rebuilt this repair as private the maintainer's own
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
  the maintainer's own v0.1.14 plugin without issuing any navigation action.
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
  the maintainer's own package ID; it was not uploaded. Recorded field traces, a
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
  `wss://home-mac.example-tailnet.ts.net`. The new protocol-v4 client rejects
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
  the maintainer's own project. A physical-run harness now preserves walking and
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


### 2026-09-11: JOAT glasses hierarchy, phone controls and notification lifecycle

- Planning now carries a semantic content/control boundary from the Mac; the HUD distinguishes selected agenda content, date/time annotations and secondary navigation controls. Saved native-model results refresh the selected agenda immediately while retaining their receipt. Messages preserves conversation identity into transcript and full-text views.
- The phone plugin WebView now provides approved Mac selection, reconnect, listening policy and SDK location diagnostics. Explicit switching retires the former Mac session through the existing display/audio/navigation owners.
- JOAT notification count, active list and Home cards share CardLog state. Opaque card identities bind dismissal and targeted clears; historical Mac Messages unread totals no longer appear as the JOAT notification bell. Active cards can be cleared while bounded AI history remains available. Native Messages and native Even notifications are untouched.
- Navigation setup/status text occupies one update region, and unchanged acknowledged regions can be skipped. Pending native image calls remain serialized with bounded failure; these changes do not establish zero end-to-end latency.
- Validation uses direct Even simulator captures and production controllers with disposable data; compiler checks are diagnostic only. See docs/audits/2026-09-11-feature-repair-plan.md for evidence and remaining phone-control, left-temple and physical-device gaps. No release, Mac deployment or physical-device acceptance is claimed for this repair slice.

- Follow-up correction: compact neutral phone settings replace the verbose green page; troubleshooting is disclosed on demand. Glasses policy/reconnect configuration is removed in favor of phone ownership, with Repeat last reply retained as a direct action. Simulator phone/menu inspected. Historical speech logs identified follow-up rejection during the answer reading hold; a local addressing correction awaits live microphone/echo verification because the real engine reports no connected glasses.

- User-directed correction supersedes the preceding Repeat-menu and agenda-layout claims: removed the redundant visible menu item; rebuilt the DAY agenda around SF Mono-first typography, adjacent times, header date controls and a compact scope footer. Bounded DAY paging preserves controls beyond the20-row display limit. Direct simulator captures verify the ordinary agenda, menu ending Activity, three busy-day pages, Earlier return and Month selection. See feature-repair audit for receipts and remaining acceptance limits.

- QuickList follow-up supersedes the SF Mono agenda and rejected native-font experiment: Day and Month task reading now reuse JOAT AI's proportional type roles, title-first hierarchy, smaller due information and bounded selected outlines. Full display metadata preserves title wrapping without changing Mac action ownership. Both Day and Month reminders reach the existing confirmed identity-bound completion path.
- Connected simulator replay exposed and corrected cancellation restoring one visual row while tapping another, dropped rapid swipes, and host-menu restoration canceling an active paint and provoking a blank rebuild. Explicit right/ring Exit now uses a local JOAT confirmation before SDK shutdown0; Stay/timeout restore the feature. Left Back is preserved, not remapped to the simulator's right-only button.
- Native simulator journeys with disposable service data verify the selected reminder through cancel, return, repeat tap and completion, plus the Month drill-in. Partial image transitions, physical left-temple gestures, unexercised view paths and real voice/device acceptance remain open. See feature-repair audit for rejected captures and retained evidence; no release/deployment claim.

### Messaging, Planning and QuickNote connected repair — 2026-09-11

- Corrected long Messages review ownership and review→confirmation routing; actual departure still cancels the draft.
- Added proportional exact-source reader paging for QuickNotes and prepared Messages. Restored regular-content/selected-content hierarchy without changing approved JOAT AI typography.
- Added Month Open day routing and ordered dashboard paint/input handling.
- Root visually inspected retained U/V failure/comparison frames, W QuickNote four-page/reverse and Month→Day frames, and X Messages review→confirmation→cancel plus Planning typography. Independent specialist reviews covered Messages lifecycle, Planning ordering and QuickNote pagination.
- This remains partial feature acceptance: older notes, full gesture/overflow coverage and partial tile transitions are open. No package upload or deployment. See docs/audits/2026-09-11-feature-repair-plan.md.

### Consistent Back and simplified top bar — 2026-09-11

- Added shared GesturePolicy for source-independent double-click Back across sys/text/list inputs; removed right/ring exit-confirmation branch. Retained actual host exit lifecycle.
- Centralized Back owner dispatch and protected confirmation refusal, lifecycle/audio delivery, and visible navigation ownership.
- Removed redundant top status wording from canvas/native paths while retaining micON/OFF, time/date andbattery; updated gesture hints and Home menu guidance.
- Restored a reader parent’s saved selection in its first custom frame. Root visually exercised QuickNote, Messages and Planning returns/cancellation plus mic toggling in official simulator; independent reviewer found ordering gaps that were corrected. Retained Z/AB evidence, with partialtile rendering and physical-device proof still open.

## 2026-09-11 — Rendering scheduling and JOAT 0.1.86 package

- Removed idle batching from feature frames, pre-encoded changed tiles before serial transmission, cached two immutable encodings per tile container, and added development-only timing. Preserved custom typography, ownership guards and native write pacing. Independent source review found no new blocker.
- Compiled and packaged reviewed working-tree version 0.1.86. Visually exercised connected Messaging, Planning and QuickNote journeys in the official simulator with disposable data. Intermediate frames still mix old/new tiles; no atomic or zero-latency rendering acceptance.
- Private upload blocked before submission: Safari selected the exact package but kept Upload disabled. Last inspected private portal version remains 0.1.85. Package, hashes, sanitized receipt and visual evidence: build/rendering-release-0.1.86/. No physical-device or installed-build claim.

## 2026-09-11 — JOAT 0.1.86 private portal confirmation

- Live portal now shows JOAT v0.1.86 as the newest private build, superseding the earlier picker-blocked status. Submission actor and remote byte hash were not observed. No duplicate upload.
- Independent current SDK review still finds no supported atomic presentation for the approved full-screen custom-font design; partial rendering remains unresolved. Retained local two-artifact Completion Receipt; no new production edits or rebuild.

## 2026-09-11 — QuickNote archive paging candidate

- Replaced eight-per-kind UI truncation with a stable merged text/recording archive and twenty-row wire pages. Added explicit page metadata and page-bound selection, older/newer boundary swipes, reverse last-row arrival and Home retirement while retaining custom typography and reader Back snapshots.
- Current served-source simulator with56 disposable notes reaches oldest text and recording, reverses archive pages and returns from a long reader to the same selected item on page2. Empty/singlepage and pending-Home coverage remain outstanding; physical device and atomic rendering are not proven.
- Corrected observer provenance: stale Vite on5208 invalidated candidateb/c/d. Candidatef checks the served archive module and owned Vite process. Messaging longreview retry was not admitted by its prompt gate and remains incomplete. These changes are not in private build0.1.86 yet.

## 2026-09-11 — Connected reader and archive retirement repair

- Fixed Messaging long-review paging and final-tap identity across Mac and Hub. Independent review added retired-reader rejection so delayed responses cannot reopen a cancelled draft. Fresh simulator visually reaches all five pages, reverses, opens confirmation, cancels, and retains conversation after a held response is released. Fixture sender remains unused.
- Fixed archive cleanup on common Home departure. Empty/single-note journeys and held older-page response after Home visually verified in disposable fixtures.
- Planning busy-day More/Earlier routes verified after selection queue settled; earlier intermediate capture was not evidence of a skipped paging row. Rendering coherence, physical input, live speech and new integrated release remain unproven.

## 2026-09-11 — Existing JOAT package uploaded

- Uploaded apps/hub-plugin/joat.ehpk to the existing JOAT project. Portal confirmed newest private build v0.1.87 after Add build and refresh (previous newest v0.1.86). Package: 147768 bytes; SHA-256 923d7673257493b4308deb8cebb1d2a7b691d2c242d58ab56d5e78cab11962c7. No physical install or matching Mac engine restart is claimed.
- User intentionally removed build/ and requires updating the existing package in place; do not recreate build/ or versioned package copies. Earlier screenshot/receipt paths there no longer exist; transcript observations remain historical, not available local artifacts.

## 2026-09-11 — Planning pending-selection and Back repair (local)

- Replaced per-swipe awaited redraw chain with one in-flight paint and latest pending destination. Each input clamps independently; a queued tap freezes its target until successfully painted. No parallel presenter writes.
- Read-only event-detail Back restores the retained agenda selection instead of starting a new list at row zero. Mutation/date/scope paths retain authoritative refresh.
- Official simulator, actual served source: eight rapid Down inputs followed by tap opened Agenda09 (Mac capdata index8); Back restored Agenda09 selected; leaving during pending redraw retained Home. Screens viewed directly in chat without new artifact directories. These changes are not yet in uploaded v0.1.87.
- Running Mac brain still predates the fixes; Restart Engine requested because native Computer Use could not access JOAT controls. Existing package path remains apps/hub-plugin/joat.ehpk.


## 2026-09-11 — Shared row selection and authoritative Planning navigation

Implemented one ordered custom-row driver for Planning, Messaging and the QuickNote archive. It retains permitted message row indices, defers taps until selection paint completes, and defers an authorized page-boundary gesture until its endpoint paints. Back retires pending row intents. Dashboard and list selection acknowledgements now reject foreground conversation/prompt/confirmation ownership.

Planning grid actions now carry the published civil date as planningDay. The Mac validates it against the current grid; the final pane index opens the full day without starting a new visit. Actual productivity timestamps retain their prior absolute-instant semantics. Matched Python/TypeScript metadata planningNavigation supplies authoritative parent availability and restored selection. Bounded Mac parent frames distinguish child drill-down from peer scope/date/page controls; same-request Planning revisions retire delayed child publications.

Current official-simulator run used current served source, disposable in-memory EventKit/Messages/45-note data and Pacific/Kiritimati (UTC+14). Visually inspected Month Sep11 → Open day → Sep11 Day; four rapid Down then tap opened Agenda05. Back refetched Day with Agenda05 selected, then the original Month/date. Day's Week scope control opened Sep7–13; Friday Day returned to Friday selected in Week, then Month, then Home. QuickNote four rapid Down+tap opened Note05; Back retained it, 16 rapid Down crossed to Note21 on archive page2, and Up restored Note20 at page1 endpoint. Messaging rapid three Down+tap opened Message09; Back retained it, boundary swipes opened older messages4–1, and Up restored latest conversation content.

The first native-list injection referenced an unimported SDK enum and failed before dispatch; that attempt is excluded. A corrected SDK-enum replay opened the expected Messages conversation despite the hidden native index staying zero. Returning from older messages visibly restored the first speaker selection rail, with controller and presenter both at index1. Holding the actual Friday calendar query, double-clicking Back, then releasing it retained Week with Friday selected; the obsolete child did not reopen.

That run also exposed an unresolved display observation: after returning to Week, screenshots intermittently omitted header/other pixels and later became blank; the next Up restored the complete screen. Independent console review found nonempty rendered tiles, zero changed uploads, and no recorded rebuild/failure in that interval. This does not identify whether native display retention or framebuffer capture diverged from the delivered-tile cache. Do not substitute a periodic repaint workaround or claim rendering acceptance. Interrupted dashboard and final Messages-review replay remain pending. No real message or native item was written. Simulator and all temporary support processes were closed; ports9918/5208/18870 were confirmed free. All fixture/control code remained in memory. Compilation was diagnostic, not acceptance; these changes are not yet in the uploaded package.


## 2026-09-11 — Native-window comparison and integration review

Repeated the delayed Week-to-Day query/Back/release journey with both the actual macOS Glasses Display window and official simulator framebuffer captures. They agreed: Week retained Friday selection, including through an idle clock update. Eight consecutive framebuffer captures were stable. The earlier missing-pixel observation did not reproduce; its cause remains unresolved, and this is not atomic-rendering or physical-device acceptance. Rapid date movement followed by Back returned Month to Home without a residual child screen. Closed simulator and support sessions; verified ports9918/5208/18870 free. Captures and fixture code remained in memory.

Independent source review found no new blocker in matched reader/archive metadata, ordered row selection, Planning civil dates or authoritative parent/revision handling. This closes the reviewed integration slice, not the full goal. The existing uploaded package still predates these changes.


## 2026-09-11 — Mac notification delivery and selected-reminder lookup

Integration review identified and repaired two gaps. SessionManager now resolves accepted Main notification delivery independently of tray refresh; one event-triggered coalescing refresh task contains tray errors and is cancelled on close. An in-memory injected failure check completed both standalone and batched delivery with two Main writes, two failed tray attempts, and no retained refresh task. This is diagnostic completion evidence, not a visual or device claim.

Planning completion now uses a read-only reminders.get native bridge call backed by EventKit calendarItem(withIdentifier:), avoiding the mismatched global-first-500 query. Full Reminders permission, store generation checks, selected identity, explicit confirmation and write/readback gates remain. Older Mac bridges receive an update explanation. Swift sources compiled using the existing cache; Python checks passed. Updated native lookup still requires the matching installed app and connected journey replay. No native reminder was changed.


## 2026-09-11 — Installed Mac build and selected-reminder replay

Archive/export and strict signature verification succeeded. Updated the existing /Applications/JOAT.app from dist/JOAT.app after gracefully stopping the installed app; no backup/versioned directory was created. Installed and exported executable SHA-256: b29a03d930b6caf3e13632fa512ee192d7056b6f57c23c5b37c1de6bd24b73e1. Relaunched app PID5370, owned engine PID5380 on127.0.0.1:8787; fresh status showed personal-data ready, glasses disconnected, mic off. This is app/engine deployment evidence, not connected-glasses acceptance.

Official simulator replay used 601 in-memory reminders, an exact lookup for selected-601, the real productivity controller/journal/confirmation flow, and no allowed reminder writes. Visually observed the selected reminder in September11, its correct confirmation, swipe cancellation returning the same selected reminder, and Back to Home. The first cancellation capture was excluded because concurrent terminal edits triggered Vite HMR. Replayed against committed d6b2646 Hub source served directly from Git into memory with watching disabled. No checkout/package copies were created. All owned simulator/support processes closed and ports9918/5208/18870 released. Hub release remains pending; unrelated in-progress terminal source must not enter it unreviewed.


## 2026-09-12 — Current-build rendering evidence correction

Replayed connected QuickNote, Messaging reader and empty-day Planning returns against 0.1.88 Hub source. Withdrew the claim that these captures proved missing retained pixels: comparison with actual encoded images found zero missing bright pixels in the checked settled frames. Separately quantified a QuickNote list-to-reader transition: 12 of 50 samples mixed old/new pixels; first complete new frame at372ms. Partial-transition acceptance still fails. Details and limits are in the existing feature-repair audit. All owned simulator/support processes closed; no extra package or production workaround created.


## 2026-09-12 — Canonical rebuild after Messaging follow-up repairs

Rebuilt the existing apps/hub-plugin/joat.ehpk from committed source 22706d3 using the existing private WSS candidates and unchanged 0.1.88 manifest metadata. Type checking and production compilation passed (62 modules). Canonical artifact: 150653 bytes; SHA-256 40128047a55acdedd03d7a089066bc588ac30e97c16e427a1852d330c7442385. The deliberate dirty override covered only the pre-existing untracked tsconfig.tsbuildinfo under the plugin; tracked source was clean. Temporary release staging was removed; repository build/ was not created.

Safari showed JOAT 0.1.88 as the latest existing private build before the attempt. Opened that project's Upload a build dialog. The canonical file appeared in the native picker, but selection did not take effect and Upload remained disabled; a coordinate attempt returned noWindowsAvailable. Cancelled the picker successfully. No submission occurred, no version was changed, and no new simulator/device acceptance is claimed. Partial transitions remain an unmet requirement.
