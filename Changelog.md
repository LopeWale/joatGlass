# Changelog

## 2026-08-30

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
