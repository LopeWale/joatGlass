# Live connections and message replies

2026-09-05, America/New_York. Scope: the running JOAT Mac engine, current Hub source in the official simulator, the configured remote OpenClaw Gateway, and existing Messages data. This supplements the feature audit; it does not close it.

[Content-free execution receipt and source hashes](evidence/remediation/live-connections-20260905.json).

## Live connection contention, 04:45 local

The user reported rapid connected/reconnecting transitions after opening the
new package. The live engine log showed 76 `replacing existing plugin connection`
events during 04:38, alternating two G2 identity digests. One matched the
official simulator used by this task. Stopping only that simulator ended the
replacement loop. The other G2 reconnected at 04:41:08 and remained connected
through the 04:45 check without another replacement or disconnect.

This incident was caused by two clients repeatedly taking ownership of the
same gateway: the server closed its predecessor normally, and each client's
automatic retry immediately took the connection back. The test simulator
should not have been left competing with the wearer's live display.

The correction now uses private WebSocket code 4409 for authenticated replacement.
A retired client stops automatic redials, queued input, clock/transient paints,
and old native/image work. Ordinary disconnects retain retry behavior. Explicit
phone-panel reconnect or the host's foreground restoration permits one takeover.
Concurrent authenticated arrivals publish ownership before awaiting old closure;
stale cleanup cannot clear their successor. The Mac change is loaded in the
supervised engine PID 4704 as of 12:09 America/Los_Angeles. Its OpenClaw connection
is ready; physical-G2 handoff acceptance remains unverified.

Supporting automated checks, not visual acceptance: 394 Mac transport/prompt/protocol/Terminal cases and 995 Hub cases
passed; Python typing on 108 modules and TypeScript passed. The display tests
held rendering, a tile upload and native text across suspend/resume, and proved
that old work could not trigger subsequent writes or self-healing. An SDK call
already issued before retirement cannot be recalled.

In the official simulator's isolated WebKit transport harness, old behavior
made 2 unsolicited redials in 2,202 ms; the corrected class made 0 in the same
window. Two explicit restoration callbacks made one new connection, authenticated
at 414 ms and stable through 2,203 ms. These are transport measurements and inspected
WebView screenshots, not physical foreground/headless migration proof.
[Before receipt](evidence/remediation/reconnect-live/before-replaced.json),
[after receipt](evidence/remediation/reconnect-live/replacement-replaced.json),
[restoration receipt](evidence/remediation/reconnect-live/replacement-restore.json).

The real connection also disconnected at 04:46 and 04:53, with no further
replacement loop; the cause of those separate disconnects is not established.
Times in that incident log are America/New_York; the host timezone subsequently
changed to America/Los_Angeles. The simulator used for later transport checks
ran only on isolated 9797/9798 and was stopped afterward.

Private application codes are defined by
[RFC 6455 section 7.4.2](https://www.rfc-editor.org/rfc/rfc6455.html#section-7.4.2).
Foreground recovery follows [Even's documented headless WebView migration](https://github.com/even-realities/everything-evenhub/blob/main/plugins/everything-evenhub/skills/background-state/SKILL.md).
The replacement code is JOAT's agreement, not an Even SDK code.

## Current package, 12:10 America/Los_Angeles

`apps/hub-plugin/joat.ehpk` contains version **0.1.77**, 133,418 bytes, SHA-256
`c25b0592c9fdb89e449d86a9211b326577ebc5e3c140a0ab54586df814a221e0`.
The canonical hash and every frozen source-file hash match the receipt.
The build passed 995 Hub tests, eight release contracts, TypeScript and Vite;
the official CLI packaged SDK 0.0.14 with derived Even App minimum 2.2.9.
It includes replacement yielding, guarded display retirement, explicit host
restoration and the startup microphone guard. No upload was performed, and
physical-G2 acceptance remains unverified.

The CPJ job `job-mtorbau5-61544d0e` exited before building because version
0.1.76 already had a successful receipt. That artifact was verified valid,
but its `main.ts` hash preceded the latest startup microphone guard. The next
version was built successfully; no receipt or duplicate-version guard was
bypassed and no extra package copy or backup was created.
[Current build receipt](evidence/remediation/reconnect-live/package-0.1.77.json),
[official packaging command](https://hub.evenrealities.com/docs/reference/cli).
The earlier 0.1.75 receipt remains under `artifacts/releases/joat-0.1.75` as
historical evidence and predates the connection correction.

## Actual Hermes failure, 03:28 local; classification repair checked 04:35

The real assistant invoked `send_terminal_instruction`, but Hermes ended the
run unsuccessfully. Source checks passed (578 Mac tests, 984 Hub tests and
typing); the actual feature check failed and its captured display showed that
failure. JOAT discarded the valid run-outcome category, closed the connection
and reported `protocol_rejected`. That label did not identify the remote cause.

OpenClaw defines `error` and `aborted` as valid terminal chat events:
[official v2026.7.1 schema](https://github.com/openclaw/openclaw/blob/v2026.7.1/packages/gateway-protocol/src/schema/logs-chat.ts).
The repair preserves a healthy session after those outcomes while surfacing
fixed messages for the documented error categories. Arbitrary provider text is
not logged. Twenty-three failing cases and four passing controls became 27
passing cases; all 125 Terminal/runtime tests and typing on 108 modules pass.
The remote failure cause and a successful real Hermes answer remain unverified.

## Conversational Terminal repair plan and acceptance

The confirmed gap is distinct from connectivity: Hermes can connect and open,
but the model registry cannot submit an instruction to it. The bounded repair
adds `send_terminal_instruction(lane, instruction, operation_id)` using the
existing selected-session controller and captured adapter generation. The
controller reserves a retry identity, prepares the explicit lane, rechecks
navigation and connection ownership, and atomically records admission with
the launch. Busy lanes retain their queued input; pending Work approvals and
Hermes attestation remain with the existing adapters.

The display uses the existing voice-prefixed list entry to establish the same
request/selection pair as a picker tap. Later frames cannot reclaim the view
after Home. Assistant receipts yield when their tool changes the view, while
the successful assistant stream still completes. No command-string rewrite
or new intent model is introduced.

Acceptance requires failing-before/passing-after tests for exact input,
deduplication, interrupted preparation and display ownership, followed by one
actual assistant request to Hermes and inspection of the real simulator.
Source gates alone do not close this feature. The tool schema and execution
errors follow the [official MCP contract](https://modelcontextprotocol.io/specification/2025-06-18/server/tools).
The host still owns execution and the glasses display it, consistent with
[Even's official device overview](https://hub.evenrealities.com/docs/guides/device-apis).

Implemented and parent-checked:377 Mac tests plus the new real-HTTP MCP
composition test passed;214 Hub ownership tests, mypy108 and TypeScript passed.
The admission, missing-entry, failed-paint timeout, queued-after-Home and
assistant-overlay failures were observed before their repairs. Two legitimate
queued openings remain allowed when no local departure occurred. The broader
gate and actual assistant-to-Hermes run are pending.

The fresh official simulator also exposed a separate transfer defect:
`beginLiveSession` painted Overview before every Brainstorm update. A test
observed two extra Overview paints for two updates; the repaired method
releases menu ownership without painting the unrelated page. Live traces and
mixed-frame evidence are retained, with equivalent after-measurement pending.
[Blank-screen and transfer evidence](evidence/remediation/display-latency-live/blank-trace/README.md).

## Conversational Brainstorm executes against both real collaborators

The actual MCP registry exposed fourteen tools, including `open_feature`, but
none could submit a Brainstorm topic. The exact legacy phrase could start a
session; a natural paraphrase routed to the model could only open the view.
The new `start_brainstorm(topic, operation_id)` tool invokes the existing
session controller with the exact topic. It checks availability and current
session ownership, and retains successful operation IDs to prevent duplicate
collaborator launches on retries. A start receipt does not claim answers or
visible pixels. Failed admission can be retried after recovery.

The related assistant receipt no longer paints over a session its tool just
started, including a session that finishes before that receipt returns. The
guard compares admitted-session revisions, preserving ordinary replies that
do not change session ownership.

Independent review then found two handoff gaps, both reproduced before their
correction: the first running frame could be hidden behind the root menu,
and returning early from the assistant iterator could lose its conversation
continuity. `HudSink.open` now sends the existing initial-entry message once
before streaming. The overlay guard suppresses obsolete painting while still
draining a successful assistant turn. Later menu escapes retain their existing
streaming guard. Eight targeted checks and typing passed after these changes.
The final broader gate then passed 422 tests in 81.18 seconds, typing on
108 modules and the whitespace check. The signed app replaced its supervised
engine with PID 35176 and loaded this final revision.

The missing-topic integration test failed before implementation; two overlay
ownership cases failed before that guard. Afterwards, 290 feature/session/MCP
tests passed, then 149 feature/orchestrator/MCP tests and ten focused typed
admission/revision cases passed against the final changes. Mypy passed on
108 modules. These intermediate counts describe successive revisions;
the 422-test gate above covers the final handoff corrections.

At 02:58:51 local, the actual assistant accepted one public desk-organization
request and invoked `start_brainstorm`. HTTP response took 4,901 ms; the
new session settled in 26,077 ms with CODEX, CLAUDE, JUDGE and JOAT output
and no failure markers. This proves real collaborator execution. The later
official-simulator framebuffer was blank in three consecutive samples;
a diagnostic webview snapshot did not immediately restore it. Automatic
display completion therefore failed acceptance. Spoken STT and physical G2
remain unverified. [Live execution receipt](evidence/remediation/display-latency-live/conversational-brainstorm.json)
and [frame measurements](evidence/remediation/display-latency-live/brainstorm-post-run-frame.json).

This follows MCP's model-selected tools and execution-error contract, using
JOAT's existing controller rather than adding another phrase parser.
[Official MCP tools specification](https://modelcontextprotocol.io/specification/2025-06-18/server/tools).
Even's architecture places application execution in the host WebView and
exposes the glasses as display/input; it does not supply a Brainstorm engine.
[Official Even architecture](https://hub.evenrealities.com/docs/get-started/architecture).

The retained live check submits one public desk-organization request to the
actual assistant and observes only the resulting new session's labels,
lengths, failure markers and state. It does not replay recorded speech,
inject mock provider answers or wake the webview during the request.
[Live check script](evidence/remediation/display-latency-live/check_conversational_brainstorm.py).

## Hermes false blocker fixed, 02:24 local update

The actual Hermes connection now succeeds. Before the fix, the supervised
engine's 02:19:31.931 receipt reported `browser-filtered-by-profile`, zero
tool groups and zero tool entries, but JOAT mapped that response to
`dedicated_session_not_proven`. This replaces the earlier hypothesis that
the live failure was necessarily a cold MCP catalog.

OpenClaw's own implementation emits this informational notice when the
profile excludes the browser tool. Its comment states, “Browser can be
configured yet absent after policy/profile/plugin filtering; surface why.”
[Official OpenClaw source, v2026.7.1](https://github.com/openclaw/openclaw/blob/v2026.7.1/src/agents/tools-effective-inventory.ts#L50).
The notice uses `id`, not `code`:
[official wire type](https://github.com/openclaw/openclaw/blob/v2026.7.1/src/agents/tools-effective-inventory.types.ts#L31).
The initial diagnostic used the wrong field; it was corrected against this
type before the matched live receipt and behavior change.

JOAT now accepts only the three documented browser exclusion notices with
their expected severity and a nonblank message string, then validates every
tool group and rejects any enabled tool. MCP discovery, mixed, unknown,
malformed and schema-quarantine notices remain rejected. This changes no
remote permission or session identity rule.

Seven new public-constructor regressions failed before the repair. Afterward,
287 related Terminal tests passed, plus mypy on 108 modules. The signed app's
actual supervised engine (PID 4534 at capture) reported `connected_operator`
with `operator.read`/`operator.write` and Terminal `ready`. Through the actual
MCP feature entry and simulator gestures, Hermes appears in the roster and
opens its READY terminal. The mic was returned to OFF.

![Actual session roster now includes Hermes](evidence/remediation/reconnect-live/terminal-roster-after.png)
![Actual Hermes terminal opens READY](evidence/remediation/reconnect-live/hermes-ready-after.png)

[Current state, source hashes and acceptance limits](evidence/remediation/reconnect-live/receipt.json).
This proves connection, attestation, roster inclusion and selected-terminal
rendering. A completed spoken Hermes turn is still unverified. Work's separate
`AUTH_SCOPE_MISMATCH` was not repaired by this change.

## Display delay localized, 02:39 local update

The real MCP feature request returned in 11 ms, but the official simulator
executed no new JavaScript ingress for 93.382 seconds. A read-only webview
snapshot then woke ingress within 3 ms, without a glasses gesture. Embedded
JavaScript timestamps distinguish this from buffered console output. The
render queue cannot be the initiating cause because ingress precedes it.

This is a measured pre-JavaScript host stall. Apple's inactive WKWebView
suspension contract fits, but the simulator's exact native setting has not
been inspected. No speculative renderer, pacing or microphone keep-alive
change was made. A foreground-webview comparison is pending; no automatic
feature-opening latency improvement is claimed from this run.
[Trace, source comparison, diagnostic patch and official citations](evidence/remediation/display-latency-live/README.md).

## Connection retry ownership fixed

### Real conversational entry follow-up, 02:29 local

The actual assistant received “Please open Agent Terminal so I can choose
Hermes.” It invoked `open_feature` at 2,807 ms and returned HTTP 200 in 4,230 ms.
The tool log and endpoint timings confirm real execution, not an assertion
in the model's reply. However, the initial simulator screenshots still showed
the prior Hermes READY terminal. The requested picker appeared only after an
`up` simulator input. This is conditional visual evidence, not successful
automatic rendering or a measured end-to-end display latency. The wake/render
delay remains open and must not be hidden by the process's successful exit.
[Request and verification receipt](evidence/remediation/reconnect-live/conversational-open.json).

![Requested picker visible after simulator input](evidence/remediation/reconnect-live/conversational-open-after-wake.png)

MacLink now cancels an obsolete reconnect timer on explicit connect/close and
retires a timed-out dial, authentication or HUD dispatcher without waiting for
the browser's asynchronous close event. Retry scheduling precedes state
notification so a synchronous observer's reconnect can cancel it too.
[WHATWG's WebSocket close contract](https://websockets.spec.whatwg.org/#dom-websocket-close)
defines closing and the later close event separately.

Six new regressions failed before their respective repairs. Final source
verification: 56 MacLink tests, 974 Hub tests, typecheck and production build.
The official simulator's real WebKit/WebSocket fault test opened an unwanted
third connection after manual recovery before the fix; afterward, the restored
second connection remained stable. With a peer withholding authentication and
close acknowledgement, final after-source connected in 4,747 ms. The before
run ended at 37,435 ms without recovery; its requested 12-second timer was
delivered late, so these are observations, not a controlled percentage speedup.

[Reproduction, unmodified screenshots and timestamped receipts](evidence/remediation/reconnect-live/README.md).
The separate observed long startup/reconnect gap against the Mac remains
unexplained. These transport tests do not establish physical G2 performance.

## Capability correction

There is no established glasses-wide read-only messaging limitation. The intended architecture is valid: glasses input and display, a companion transport, transcription and agent execution on the user's computers, then a messaging-specific send adapter.

- **Even Realities, official Device APIs**, updated 2026-08-25: `audioControl` selects the G2 or phone microphone; `audioEvent.audioPcm` carries 16 kHz, signed 16-bit little-endian mono PCM. This establishes the input available to JOAT, not a messaging-service API. [Official audio contract](https://hub.evenrealities.com/docs/build/device-apis#audio).
- **Even Messages, original community implementation**: reads and replies through the Beeper Desktop API; its documented reply flow uses preset responses. This proves an existing integration approach, not that every advertised messaging network works in JOAT. [Maintainer repository](https://github.com/thibautrey/even-messages), [Beeper's official API overview](https://developers.beeper.com/).
- **Matrix G2, original community implementation**: the glasses WebView relays audio over WebSocket to a Python bridge, which performs local Whisper transcription and Matrix sending after Send/Cancel review. Its author reports end-to-end support; this audit inspected its source and documentation, not that author's physical device. [Maintainer repository and implementation](https://github.com/logicalpeyote777/matrix-g2).
- **Hermes EvenHub Bridge, original implementation**: a Hermes platform adapter exposes a local WebSocket with optional Tailscale Serve, voice transcription and streamed responses. This corroborates the user's two-computer integration direction without requiring a second glasses plugin. [Maintainer repository](https://github.com/huntsyea/hermes-evenhub-bridge).
- **Apple, installed Messages scripting dictionary**: `send` accepts either a `participant` or a `chat`; a chat has a GUID identifier. [Messages.sdef, command](/System/Applications/Messages.app/Contents/Resources/Messages.sdef:55), lines 55–65; chat identifier at 123–126. The dictionary does not document the chat.db schema or prove every database GUID resolves through scripting.

Sources accessed 2026-09-05. Community implementations establish concrete mechanisms; they are not Even Realities guarantees or substitutes for JOAT acceptance.

## Distinct incident boundaries

| Observed evidence | What it establishes | What it does not establish |
| --- | --- | --- |
| September 3 local logs: `gaierror` | Historical DNS failure | Current network failure |
| September 4 16:01:43: `ConnectionRefusedError` | A historical connection refusal | Cause of subsequent authenticated failures |
| Running engine had started September 4 16:01:36; after supervised restart, tool inventory increased from 7 to 14 | Repairs on disk had not all reached the running engine | All seven newly loaded tools work end to end |
| Current HTTPS 200, WebSocket 101 and `connect.challenge`; Tailscale peer online | The configured Gateway is currently reachable | Authorization or session readiness |
| September 5 01:08:58 and 01:12:25: `AUTH_SCOPE_MISMATCH` on Work | The Gateway recognizes the device token but rejects Work's requested grant | A pending approval request already exists |
| Hermes startup reports `dedicated_session_not_proven` from tool-inventory attestation | A separate session-policy failure after connection setup | Brainstorm's local Codex/Claude pair is unavailable |
| Actual group conversation previously classified read-only | JOAT's sender lacked chat targeting | Apple Messages or G2 cannot send group replies |
| Most recent 500 incoming database rows: 481 plain-text, 16 attributed-body-only, including 1 without an attachment marker | Missing rich-body text is a real data-path gap | All 16 rows are absent; attachment-marked rows can still appear with incomplete text |
| Codex-launched native Messages chat lookup timed out twice at eight seconds | That probe did not resolve a native target | A specific permission denial, that the signed JOAT process behaves identically, or that sending is impossible |

The all-day failures have multiple boundaries. None of these observations is promoted into a single explanation for every feature.

## Repairs and acceptance

**Existing chat replies:** `IMessageStore` now retains the chat GUID privately. Selection, confirmation, replay identity and the journal bind target kind plus ID; `MessagesAppSender.send_chat` resolves that exact chat. Group replies cannot fall back to the latest individual participant. Existing contact-based composition remains supported. Existing direct chats also retain their account/service through chat identity.

In the running Mac engine and official simulator, an actual group conversation now displays `say reply`. A reply prepared through the authenticated assistant tool reached `awaiting_confirmation` with its selected recipient bound; the same action was immediately cancelled. No confirmation was accepted and no message was sent. This is live preparation/cancellation proof, not microphone transcription or delivery proof. No personal message text, chat identifier or credential was retained in this report.

**Work authentication recovery:** exceptions no longer disappear behind a generic retry message. The local log retains class and code location without arbitrary remote payloads; the Gateway logs its sanitized refusal code. Scope mismatch and device-pairing refusal now produce `approval_required`, distinct from pending tool approvals. The actual Work simulator screen changed from OFFLINE/connection unavailable to NEEDS APPROVAL with explicit OpenClaw permission guidance. The footer puts approval before retry. This improves diagnosis; it does not grant permissions or establish a working terminal session.

**Credential contract alignment:** Swift now accepts `operator.approvals` together with read/write, matching Python's Work client. Admin, incomplete approval grants and duplicates remain rejected. When a successful connection returns the identical stored device token, Python preserves its recorded grant instead of replacing it with the socket's narrower scopes. A new or changed token still stores the newly negotiated grant. These follow [OpenClaw's official authentication contract](https://docs.openclaw.ai/gateway/protocol#auth). All five native credential tests passed, and the signed universal app was built and installed. JOAT was restarted from `/Applications/JOAT.app`; its new supervised engine is listening on port 8787. This removes the local credential-store rejection after a valid remote grant, but does not change the remote grant.

**Focused verification:** 520 Python tests passed after the initial combined changes; the subsequent added pairing-recovery branch passed all 21 Terminal-preparation cases. Python typing passed. All 102 renderer tests and the production Hub build passed. Five Swift credential tests and the signed universal build/install passed in CPJ job `job-mtnxrlsm-f2e7c2c1` (exit 0).

## Prompted microphone repair and live check

The previous `tap: speak` path only worked when the microphone was already open. `PromptedInputController` now paints the question, acquires a microphone for that question, and acknowledges after activation. Cleanup restores mute only when the question opened that microphone. Native microphone control calls, including late-start compensation, execute in sequence. Display calls do not share that queue.

The Hub reports the selected `micSource` even while muted and when reconnecting. The Mac freezes that selection into each prompt ticket. SDK PCM retains its actual source; this change does not relabel phone audio as glasses audio. Automatic stops carry `micPromptId`, so an old question cannot globally revoke its replacement. Manual mute and glasses removal remain global cancellation actions.

**Current verification:** 968 Hub tests and production build passed; 342 Mac protocol, transport and prompted-runtime tests passed; typing passed for the five changed Mac modules. Tests cover all three prompt owners, selected phone source, cancellation during a held native start, existing ambient capture, replacement ownership, invalid wire fields, connection replacement and scoped versus manual mute.

**Actual simulator + actual Mac engine:** from Brainstorm READY with MIC OFF, tapping displayed “What would you like to brainstorm?” and opened the microphone. The initial status observation was 517 ms after the tap. A repeat measured 828.4 ms including a deliberate 300 ms stability check. Manual mute settled OFF in 334 ms, also including that stability check, and the screen returned to READY. With no answer, automatic expiry closed the microphone at 45.06 seconds. Both the cancellation and expiry screenshots have body/footer pixels identical to the pre-capture READY frame (crop x=0..575, y=40..287). These are capture-control timings, not STT or collaborator response latency. The official simulator used the host AirPods input and emitted SDK source `glasses` during this run; phone fallback is covered by regressions, not claimed as this run's source.

[Before tap](evidence/remediation/brainstorm-muted-live/before-tap.png), [question and microphone ON](evidence/remediation/brainstorm-muted-live/after-tap.png), [cancelled and microphone OFF](evidence/remediation/brainstorm-muted-live/after-cancel.png), [measurement details](evidence/remediation/brainstorm-muted-live/measurement.json), [expiry and pixel comparison](evidence/remediation/brainstorm-muted-live/expiry-measurement.json). These are unmodified official RGBA screenshots. A complete real spoken topic and collaborator answer remain unverified.

## Full message body recovery

`IMessageStore` now includes `attributedBody` candidates in notifications, recent conversations/assistant reads, and conversation history. Nonempty `message.text` remains verbatim. The new `message_body` boundary accepts only the attributed string's first NSString, preserving Unicode, whitespace and full text. It does not scan arbitrary printable metadata. Archives are capped at 256 KiB before SQL materialization, 32,768 events and nesting depth 64. Unsupported, malformed or oversized content yields “Message text unavailable” for that row; blank archives advance the notification cursor so later deliveries remain reachable.

This uses the unmodified pinned dependency [pytypedstream 0.1.0](https://github.com/dgelessus/python-typedstream) and public format/fixture evidence from [imessage-exporter at the recorded commit](https://github.com/ReagentX/imessage-exporter/blob/4d90fc8d20a745c0a8acc1e01c0631c4bab89cb4/imessage-database/src/tables/messages/body.rs). These are original implementations of an undocumented format, not an Apple-supported chat.db API. Fixture provenance, exact expectations and upstream license are retained under `tests/fixtures/messages/typedstream`.

**Real-data verification and its limit:** a fresh 500-row incoming sample contained 483 plain-text rows, 14 attributed-only bodies, and three without either body. The production history and notification readers initially matched all 14 archived strings. Closer inspection found no readable prose in those 14: eleven contain only attachment markers/whitespace, and three contain only replacement markers. The previously omitted non-attachment row is itself a replacement marker, so surfacing it does not prove useful message-text recovery. A wider sample of the latest 200 attributed-only archives likewise contained 173 object-marker/blank bodies and 27 replacement-marker bodies, with no readable prose. Marker-only output is now corrected: object markers use the existing Attachment label, and replacement-only content shows “Message text unavailable.” The running-engine simulator displayed that corrected unavailable row in an actual conversation, with `say reply` still available. No personal screenshot was retained. Public fixtures prove recovery of actual long/Unicode/multiline text; **live readable-text recovery is not established by these samples**. No personal text or identifiers are retained in the [reader receipt](evidence/remediation/message-body-live-reader-after.json).

All 53 decoder/store cases and 305 combined Messages/capability/UI integration tests passed after the marker correction; typing passed on all 108 Mac modules. The dependency was installed in the actual `.venv`, then the signed JOAT app's supervised engine was restarted (final PID78846). This proves reader behavior against the real database and deployment into the engine, not attachment rendering, message delivery, or physical glasses acceptance.

## Outstanding work

### Active repair: prompted speech from mute

Acceptance: the existing Brainstorm, Terminal and Messages question flow paints its exact question, opens the selected microphone, and only then acknowledges fresh audio. Cancelling while opening must prevent admission and close a late native start. A question that opened a muted microphone must restore mute on completion/cancel; an already-open microphone remains open. New control requests cannot be stopped by an older question's cleanup. Selection is reported independently from actual PCM source; all existing source and connection checks stay enforced. Verify these lifecycle cases in focused tests, then exercise the real muted Brainstorm entry in the official simulator against the running Mac engine.

- Work still needs a Gateway-approved scope contract covering read/write/approvals. The current refusal is documented, not an assumed network blocker. No remote approval or credential rotation was performed. This Mac has no configured SSH route to the Mini and its TCP port 22 refuses connections; HTTPS availability alone does not provide administration authority.
- Group sending must resolve a real native chat and demonstrate authorized submission/delivery. The successful preparation check is insufficient to close this requirement.
- Attributed-body recovery now passes the real-data checks above. Unsupported archive classes remain explicitly unavailable, and attachment rendering is separate.
- Brainstorm capture activation and manual cancellation now pass the real muted-entry check above. A complete speech-to-topic-to-collaborator journey still needs live verification. Phone fallback versus explicitly requested glasses audio remains separately traced under [Even's audio contract](https://hub.evenrealities.com/docs/build/device-apis#audio).
- Simulator microphone fallback and source execution are distinct from physical G2 microphone, installed Hub package and glasses acceptance. Those gates remain open.
