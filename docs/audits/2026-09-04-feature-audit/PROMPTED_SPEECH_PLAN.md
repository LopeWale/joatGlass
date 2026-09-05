# Scoped prompted speech: bounded A2 plan

Status: host admission, message owner bridge, authenticated publication and
PCM integration are implemented and tested. Hub prompt/controller/gesture
composition and whole-application Mac wiring for Messages tools and Brainstorm
topic capture are now implemented. All 26 production-runtime integration cases
pass. Fresh simulator prompt-flow acceptance is pending; no physical-glasses
acceptance is claimed. Terminal and legacy Messages UI adapters remain open.
September 4, 2026. The source-seam descriptions below record the pre-repair
baseline; the implementation receipt at the end records current progress.

## Intended next result

After the application visibly asks for the recipient or body of a specific
message action, one fresh glasses utterance can answer that question without
another wake phrase or model decision. The exact final transcript goes to
`MessageReplyController.provide_message_input`, retaining the selected target,
body validation, explicit glasses confirmation and controller-owned send.

This is a scoped answer to an application question. It does not make general
unaddressed conversation actionable and does not interpret message bodies as
commands. Brainstorm and Terminal need the same admission boundary, but their
owner adapters are separate follow-on slices.

## Baseline source seams and evidence limits

- [AudioFrame](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/core/interfaces.py:28)
  contains authenticated audio session generation, sequence, source, speaker
  attribution and host receipt time. [AudioRouter](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/audio/router.py:66)
  preserves that frame for conversational subscribers and keeps exclusive
  capture ahead of them. The new path must remain behind this router.
- [VoiceOrchestrator](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/services/orchestrator.py:156)
  currently retains only `(pcm, source)` for completed/pending utterances.
  Generation is used for incoming sequence resets, then lost at the
  [worker boundary](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/services/orchestrator.py:318).
  Consequently a transcript finishing after a prompt cannot be distinguished
  from one whose audio arrived before it.
- [The current topic bypass](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/services/orchestrator.py:402)
  runs before the wake gate and checks current arm state plus source. Active
  session interception follows it; ordinary commands run after the wake gate.
  A fresh scoped dispatcher belongs before all three. This is a source
  mechanism finding, not evidence that a particular historical utterance was
  accepted on a physical device.
- [VoiceCommandRouter](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/services/commands.py:72)
  gives legacy message compose precedence over greetings, but it cannot admit
  an unaddressed utterance that the orchestrator already rejected. Typed actions
  intentionally do not arm [the legacy compose flag](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/services/message_reply.py:571).
- Typed message [preparation](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/services/message_reply.py:161),
  [input](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/services/message_reply.py:201)
  and [status](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/services/message_reply.py:240)
  already provide the required owner boundary. The [tool adapter](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/agents/message_tools.py:54)
  has no prompt-publication hook yet. Root observed 72 adapter tests plus two
  MCP contracts passing; those receipts do not prove prompted microphone input.

## Minimal host implementation

1. Introduce one `PromptedInputDispatcher` with one current lease. A lease
   contains a server-generated prompt identity, owner kind, action/session
   identity, requested field, allowed mic source, authenticated audio
   generation, ingress fence ordinal/epoch and monotonic expiry. The owner
   supplies a current-validity check and a typed consumer. The dispatcher has
   no command parser, no model and no recipient-handle selection policy.
2. Carry an immutable completed-utterance receipt alongside PCM through both
   the active worker and the existing latest-wins pending slot. Retain source,
   generation and ingress fence identity from audio receipt; never reconstruct
   them from the device connected when STT finishes. Legacy/source-less calls
   cannot satisfy a glasses prompt. A final transcript is required.
3. Publish a deterministic application-owned question, not merely an
   `awaiting_input` status or an expectation that the model will ask later.
   For host fixtures, establish the ingress fence after that explicit prompt
   send finishes and recheck owner/connection validity before arming. For live
   unaddressed admission, wait for the matching successful plugin presentation
   acknowledgment and its audio fence described below. An earlier cancel,
   replacement or disconnect must win over late publication/acknowledgment.
   [HudWriter.assistant](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/services/hud.py:46)
   currently awaits only gateway send; it provides neither plugin paint proof
   nor optical proof. Status creation can precede the model's displayed
   question by seconds and is not an admissible arming point.
4. Fence VAD carefully. [RmsVad](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/audio/vad.py:125)
   retains pre-roll and has a loud-frame debounce before `in_speech` becomes
   true. Merely resetting VAD at arm can turn the tail of preexisting speech
   into an apparently new answer. Add a small explicit VAD prompt-fence API:
   clear idle pre-roll; if speech or its debounce has begun, discard through a
   real quiet boundary before admitting a new utterance. A maximum-duration
   cut alone is not that quiet boundary. Discarded frames must not contaminate
   the next utterance's source/speaker metadata. Add focused VAD regressions.
5. Fencing must not reset an STT engine serving a completed turn, empty the
   pending slot or cancel the active worker. Keep the existing
   [partial-reset guard](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/services/orchestrator.py:307)
   and continuity behavior. An old immutable receipt stays old even when its
   STT result arrives late. Rejected prompt input may only retain existing
   ordinary addressed behavior; it must never fall through into an alternate
   unscoped armed-topic or Terminal capture branch.
6. At admission, check current lease, expiry, generation, fence, source and
   owner validity synchronously, then consume the lease before the first await.
   Pass the exact text to the owner. Empty/nonfinal STT and wrong-source/stale
   utterances do not spend the valid lease. A new question after an unsuccessful
   answer requires a new visible prompt and fence; do not reuse the old audio.
7. Prevent an older voice answer from overwriting the newly published question
   while its worker finishes. Keep controller confirmation UI authoritative
   after a body is delivered; do not run greeting, command, session or answer
   generation for a successfully claimed prompted utterance.

Use the same audio session identity on both sides of the comparison:
`AudioFrame.connection_generation` is the gateway session ID string, while
`HubGatewayTransport.connection_generation` is a different integer counter.
The first host slice can fail closed until a trusted frame generation exists;
it must not guess a generation for a tap before the first audio frame. Normal
voice remains available. Reconnect/replace/disconnect must synchronously revoke
leases through the existing `greet`/`farewell` composition hooks.

## Message prompt owner

Add a small `MessagePromptBridge` around the dispatcher and existing controller:

- Only an actual prepare/provide result with `state=awaiting_input` and a known
  field can publish and arm a message question. A status poll or duplicate tool
  retry must not create a new lease, extend its expiry or move its audio fence.
- On `needs_input=body`, deliver `provide_message_input(action_id, body=text)`
  exactly. Do not trim, remove a wake word, parse a greeting or redispatch the
  body as a command. Confirmation remains the controller's responsibility.
- On `needs_input=recipient`, a spoken contact name can go directly to
  `provide_message_input(action_id, recipient_name=text)`. Relationship phrases
  still need a verified contact name. Multiple matches still require explicit
  clarification; do not map arbitrary speech or a raw handle to a candidate.
  Controller-issued `recipient_ref` selection remains a separate typed/UI
  choice. Candidate ordinals or a new speech-choice grammar are not implied by
  this body/contact slice.
- Before consumption and after asynchronous prompt publication, re-read the
  action's state and requested field. Existing controller cancellation on
  [Back/Home/disconnect](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/services/capabilities.py:532)
  must invalidate the lease even if no separate prompt callback ran. Explicit
  cancellation also revokes any pending publication token.
- Re-publish only an actual next question. Once the action is awaiting
  confirmation/sending/terminal, revoke the lease and let its existing owner
  paint confirmation/results. Do not insert an assistant overlay over review.

The first composition hook can be an optional typed status callback on
`message_tools`, called after prepare/provide only, with `__main__.py` composing
the bridge. A controller/UI publisher callback is needed separately to bring
the legacy New Message picker and bare “reply” prompts onto this same boundary;
their current string-return/boolean-arm path is not evidence that this typed
tool slice fixes those journeys.

## Brainstorm and Terminal follow-on scope

Brainstorm currently [arms before scheduling its prompt paint](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/services/session.py:1019)
and [consumes by current source/arm state](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/services/session.py:558).
Route its gesture through the shared fence after publication, bind a capture
nonce, retain its 45-second window and collaborator-availability checks, and
extract a typed topic consumer from the existing branch. Once integrated,
remove/suppress the old unscoped pre-wake bypass. Active-session cross-feature
conversation routing remains another requirement; this dispatcher is not a
replacement for that repair.

Terminal currently [arms only a clock](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/services/agent_terminal.py:864)
and later sends to the then-current focus. Capture the actual lane/session
identity at arm and validate it before direct
[`roster.send(text, name=bound_lane)`](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/services/agent_terminal.py:452).
A focus/session replacement revokes the promise rather than retargeting it.
Use the 45-second window, publish via the existing
[terminal capture callback](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/__main__.py:1050),
and bypass `handle_command` for accepted raw input. Existing Terminal armed
unit tests call that controller directly and do not prove the live wake gate.

## Proposed disjoint claims and failing fixtures

Root should claim these paths only after the running candidate verification:

| Lane | Exact paths | Purpose |
|---|---|---|
| Admission core | NEW `apps/mac/joat/services/prompted_input.py`, NEW `tests/unit/test_prompted_input.py` | Lease, receipt, expiry, single consumption and invalidation; no feature imports |
| Voice boundary, one owner | `apps/mac/joat/services/orchestrator.py`, `apps/mac/joat/audio/vad.py`, `tests/unit/test_orchestrator.py`, `tests/unit/test_vad.py` | Carry immutable receipts, fence preexisting speech, dispatch before interception; preserve foreign voice-recovery edits |
| Messages | NEW `apps/mac/joat/services/message_prompts.py`, NEW `tests/unit/test_message_prompts.py`, `apps/mac/joat/agents/message_tools.py`, `tests/unit/test_message_tools.py` | Typed prompt publication/input bridge and optional tool callback |
| Root composition | `apps/mac/joat/__main__.py`, NEW `tests/integration/test_prompted_speech.py` | Bind existing controllers, transport lifecycle and production voice path |
| Later owner adapters | `apps/mac/joat/services/session.py`, `tests/unit/test_session.py`; `apps/mac/joat/services/agent_terminal.py`, `tests/unit/test_agent_terminal.py` | Migrate Brainstorm/Terminal arms without a second admission path |

The existing `tests/unit/test_vad.py` path was confirmed during this review.
A separate controller claim is required before migrating legacy Messages UI arms.

First failing fixture journeys should use real `VoiceOrchestrator`, `RmsVad`,
`AudioRouter`, message controller and tool bridge; only transport, STT output,
contacts, confirmation and sender are controlled. Drive `AudioFrame` callbacks,
not `_handle_utterance` alone, and record actual controller effects:

1. Prepare a body prompt, emit fresh glasses PCM whose final transcript is
   `Good morning`, and observe that exact reviewed body, zero greeting/command/
   model calls and zero sends before controlled confirmation.
2. Resolve a fresh prompted contact, publish the next body question, then accept
   a second fresh utterance. An ambiguous name remains awaiting selection and
   sends nothing; no raw number or invented reference is accepted.
3. Hold STT for audio received before the prompt, arm the prompt, release STT:
   it cannot fill the body. Repeat with an old completed utterance in the busy
   pending slot. A subsequent fresh utterance succeeds exactly once.
4. Begin speech/debounce before arm and continue after it. Reject the whole
   carryover through a quiet boundary; do not accept its tail. Include VAD
   pre-roll and maximum-duration-cut cases.
5. Parameterize phone, mixed source, legacy provenance, other-speaker and old
   connection frames. They cannot satisfy the glasses lease; a valid fresh
   utterance can still do so. Source labels are not biometric identity proof.
6. Expiry, Back, Home, action cancel, target replacement and disconnect during
   STT or prompt paint reject late input/publication. Duplicate transcript/tool
   delivery cannot consume twice or extend the prompt.
7. A known body containing `stop`, `open` or other command-shaped text remains
   literal. A fresh addressed request without a prompt retains ordinary routing.
   A blocked prior replier must not overwrite the new prompt on release.
8. Preserve exclusive audio capture, latest-wins queue behavior, and existing
   [gap/replacement recovery](/Users/emmanuelakinwale/Developer/joatGlass/tests/unit/test_orchestrator.py:271).
   Brainstorm/Terminal follow-on fixtures must additionally prove their bound
   owner receives the phrase while no other session/controller receives it.

## What the host slice cannot establish

[Wire Audio](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/core/protocol.py:138)
has no capture timestamp or prompt epoch. The gateway creates
[`received_at_us` on arrival](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/ble/hub_gateway.py:1159);
the [plugin batches PCM before sending](/Users/emmanuelakinwale/Developer/joatGlass/apps/hub-plugin/src/AudioStreamer.ts:137).
Therefore the proposed host fence proves rejection of pre-fence received/VAD/
STT/queued input. It cannot prove when later-arriving SDK/network-buffered PCM
was physically captured. Wall-clock receipt comparisons or a fresh sequence
number must not be presented as that proof.

The host slice is useful for deterministic admission and rejection fixtures,
but cannot safely enable live unaddressed capture on the claim that a prompt
was displayed. Keep that admission closed until a separate prompt publication
contract is implemented; do not tell the wearer that queued gateway send is
visible acknowledgment.

The minimum follow-up plugin/gateway contract is:

1. Send a typed prompt request carrying a fresh prompt identity and the exact
   presentation binding. The plugin invokes its real presenter, not a timer
   that guesses paint completion. Failed/replaced presentation does not arm.
2. On successful presenter completion, clear/tag preexisting buffered PCM and
   establish a new plugin audio epoch. Return the matching acknowledgment
   before sending audio tagged with that epoch. A stale acknowledgment, wrong
   presentation binding, wrong connection or timeout cannot activate a lease.
3. Bind the Mac lease to that acknowledgment and audio epoch. Preserve the epoch
   through VAD, STT and the pending slot. Never relabel earlier queued audio on
   acknowledgment receipt. Legacy audio without a matching epoch cannot supply
   an unaddressed prompted answer.
4. Test delayed/failed paint, old PCM arriving after the acknowledgment, a
   replacement prompt overtaking the old paint, and a disconnect during paint.

This follow-up needs its own disjoint claims over `apps/hub-plugin/src/types.ts`,
`apps/hub-plugin/src/main.ts`, `apps/hub-plugin/src/AudioStreamer.ts`, the
presenter/bridge seam, and `apps/mac/joat/core/protocol.py`,
`apps/mac/joat/ble/hub_gateway.py`, `apps/mac/joat/services/hud.py`, plus protocol,
presenter/audio and authenticated transport tests. It is not included in the
host-only file claims above.

Even a successful presenter acknowledgment proves the documented presenter
contract, not optical visibility. The epoch proves the instrumented plugin
audio-event boundary unless the SDK supplies reliable capture-time provenance.
Physical microphone, optical prompt timing and real speech recognition still
require their own device receipts.

## Implementation seams selected during test preparation

- `PromptedInputDispatcher` separates beginning a publication from acknowledging
  it. A fresh server prompt ID is also its audio epoch. A frozen utterance
  receipt carries source, authenticated session string, epoch, first/last
  ingress ordinals and observed speaker roles. Expiry starts when publication
  begins; late paint and duplicate calls never extend it.
- `VoiceOrchestrator.fence_prompt_audio()` will fence partial VAD state and
  return the current ingress ordinal. `AudioFrame.audio_epoch` will be an
  optional field so legacy normal conversation remains compatible. Completed
  STT and the pending utterance retain their original immutable receipts.
- `MessagePromptBridge.after_input(status)` will publish only a new controller
  step, identified by its immutable status object. A typed controller accessor
  `message_prompt_field(action_id)` distinguishes a spoken contact/body input
  from a reply that first needs a conversation selected. Status polls remain
  silent. A five-second publication deadline is below the existing ten-second
  tool deadline; failures revoke the prompt and preserve the action status.
- Speaker attribution is optional and explicitly not firmware identity.
  Reject explicit other-speaker input; self, unknown and absent labels remain
  eligible with the authenticated glasses source and fresh matching epoch.
- The plugin must preserve the actual SDK event source through batching.
  Its current requested-microphone label is insufficient for a late event
  from another source. The new prompt presenter must await its own frame;
  the existing overlay boolean also reports queued or confirmation-hidden
  content as true and cannot be reused as a visibility acknowledgment.

## Host implementation receipt

The dispatcher, typed message bridge, optional tool callback, audio provenance
and VAD fence now exist. Controlled PCM travels through the real AudioRouter,
VoiceOrchestrator and message controller. A tool-prepared recipient question
accepts a fresh contact name, paints a new body question through a controlled
publisher, and reviews the second exact transcript without command/model
reinterpretation or a message send. Invalid contact/body input gets a fresh
question and epoch only while the same action remains valid.

Retained meaningful before evidence:

- `evidence/remediation/prompt-core-before.log`: 54 failures, one pass.
- `evidence/remediation/prompt-vad-before.log`: nine failures, one pass.
- `evidence/remediation/prompted-speech-before-corrected.log`: 14 failures,
  two passes. The earlier uncorrected log is a fixture setup failure.
- `evidence/remediation/message-prompts-before.log`: 22 failures, six passes.
- `evidence/remediation/message-prompt-validation-before.log`: three failures.
- `evidence/remediation/prompted-pending-overlay-before.log`: one failure.
- `evidence/remediation/prompted-send-race-before.log`: two failures.
- `evidence/remediation/prompted-owner-error-before.log`: one failure,
  reproduced with synthetic private text only.

After evidence includes 55 dispatcher cases, 148 combined VAD/voice/admission
cases, and 209 message/bridge/integration cases. Subsequent independent review
found ordinary pending replies could cover a live question and that an awaited
context send could let an old answer/settle follow a new prompt. Their same
regressions now pass. Owner exceptions are logged by class only after a
regression showed raw input in exception messages; the latest focused
voice/integration run has 74 passes and proves the next turn still works.
The frozen `prompt-news` gate will verify the combined candidate and a fresh
News simulator journey. Its process result still requires visual review.

At the time of that host-only receipt, live composition remained absent. Its
required follow-up contracts were:
the real event microphone source, an explicit-other marker that survives
coalescing, an exact prompt-owned presentation result, ordered acknowledgment
and audio epoch activation, authenticated connection identity, and lifecycle
revocation. Migrate Brainstorm's existing tap-to-topic owner before enabling
the shared dispatcher; it must not silently lose that advertised behavior.
Terminal and legacy Messages UI prompts require their own typed adapters.
The consolidated next-slice design is [PROMPT_TRANSPORT_PLAN.md](PROMPT_TRANSPORT_PLAN.md).

## Production runtime integration receipt

`PromptedSpeechRuntime` now shares one dispatcher between the typed Messages
bridge and the real `SessionManager.TopicCapture` owner. `__main__.py` supplies
the authenticated gateway audio generation, actual AudioRouter exclusive-capture
state and `VoiceOrchestrator.fence_prompt_audio`. The same dispatcher reaches
the production orchestrator, Message tool callback and Brainstorm publisher.
Home, mic-off/doff, reconnect/disconnect and shutdown revoke admission.

The retirement callback now reports consumed, revoked, expired, replaced or
owner-invalid. This prevents retirement cleanup from clearing the Brainstorm
capture before its literal topic consumer runs. Other exits clear only the
matching capture, cancel its owned publication and send matching-ID cleanup.
Shutdown stops new questions synchronously, then drains runtime-owned HUD work
without cancelling unrelated tool or completed-STT workers.

[Runtime red](evidence/remediation/prompted-runtime-red.log) records 20 failures
and six passes against the empty composition. [Runtime after](evidence/remediation/prompted-runtime-after.log)
records all 26 cases passing in 0.14 seconds. These include authenticated
gateway acknowledgments immediately followed by PCM through AudioRouter,
VAD and controlled STT for both owners; exact recipient/body review; Back,
mute, expiry and disconnect; cross-feature replacement; cancellation-resistant
late paint; and held-publication shutdown. The six new retirement-reason cases
failed before the callback change; the [combined support check](evidence/remediation/prompt-retirement-reasons-after.log)
passes all 141 dispatcher/message-bridge/speech cases. [Runtime mypy](evidence/remediation/prompted-runtime-mypy.log)
passes on all three changed production files.

The runtime integration fixtures use controlled contacts, STT text and session
work, with no sends. Simulator visuals and real microphone recognition are
separate gates. The former unscoped Brainstorm topic bypass is disabled by the
publisher configuration; Terminal capture and the legacy Messages picker/voice
reply grammar still need migration to this owner boundary.

The composition review then reproduced a recording transition defect: a
completed transcript held in STT could cross an entire short QuickNote
recording and answer an old prompt once the polled `capturing` flag was false
again. Both Message and Brainstorm owners consumed that stale answer in the
[before check](evidence/remediation/prompt-capture-entry-integration-red.log).
`AudioRouter.begin_capture` now invokes a synchronous capture-start callback,
and production binds it to runtime revocation. It does not cancel the STT
worker. [After](evidence/remediation/prompt-capture-entry-after.log): all 35
AudioRouter/runtime cases pass, including the two reproduced stale answers
and failed-capture acquisition cleanup.

The same review confirmed that the legacy Terminal arm still survives Home
and reconnect and can route a later addressed phrase into its focused lane.
This remains an explicit next repair together with the legacy New Message
picker, which still advertises bare speech through the wake-gated compose
path. Neither is included in the scoped runtime acceptance above.

The unchanged-source combined gate now passes **1,156 tests in 90.09 seconds**
and strict mypy on **104 source files**. [Gate receipt](evidence/remediation/prompt-runtime-duration-gate-receipt.json),
[per-test log](evidence/remediation/prompt-runtime-duration-gate-python.log),
[typing log](evidence/remediation/prompt-runtime-duration-gate-mypy.log).
The earlier gate's 60-second aggregate deadline expired after 196 passing
cases. Its next test passed alone in 5.13 seconds; increasing only the runner
deadline to 240 seconds and adding per-test progress completed the full suite
with no changed production/test hashes. Both attempts remain retained.

The first official-simulator prompt journey is **not accepted**. Recipient
PCM reached the production voice pipeline and the next body question was
acknowledged and captured, but the fixture timed out before any second PCM
frame arrived. The partial receipt and both screenshots are retained in
`evidence/remediation/prompt-runtime-visual-after`. They do not establish a
production admission failure or a completed speech journey.

Visual inspection also caught a separate typography regression: question
bodies used 21px sans-serif while the surrounding JOAT UI uses monospace.
A regression test failed on that exact face and size, then passed after
`drawPromptQuestion` adopted the existing `HUD_FONT` at 14px, retaining its
12px lower fitting bound and complete-question fit check. All 100 renderer
tests and the Hub build pass (`prompt-font-red.log`, `prompt-font-after.log`,
`prompt-font-build.log`). Fresh screenshots are still required for acceptance.
The next fixture records bounded control-fetch and PCM replay stages, keeps
its loopback driver history on failure, and drains the synthetic session's
ready publication before starting another question. Its instrumented Hub
source typechecks (`prompt-runtime-diagnostic-typecheck.log`).

The bounded retry (`job-mtnnweac-345bffbf`) also failed, in 18.54 seconds,
at `question:body-question`. Its recipient PCM replay completed, and Mac
prepared the body question, but Hub did not acknowledge that question. No
body answer was injected and no send occurred. The fixture history and
console show callbacks slowing and then stopping; they do not prove a
production rejection. The SDK's shadow timers and WebKit background timer
alignment are a hypothesis requiring visibility and call-lifecycle receipts.
Both failed runs remain failed evidence; neither is a speech-flow acceptance.

Root inspected the retry's original 576×288 recipient screenshot. The body
now uses the compact monospace face, the complete question fits across the
content width, and the footer is readable. This one screen supports the
font repair only. It does not establish the remaining question, review,
decline, Brainstorm, Back or mute journeys.

![Compact monospace recipient question](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/prompt-runtime-bounded-after/recipient-question.png)

### Handoff diagnostic preparation

The two prior simulator runs remain failures. The retained console is
insufficient to conclude that WebView timers were suspended: the fixture's
`fixtureBusy` guard suppresses later interval work during a stalled fetch, and
its old control log appeared only every tenth nonbusy poll. A requested body
question in the Mac receipt is also not proof of downlink arrival.

The disposable runner now records lifecycle/visibility/focus, a heartbeat before
that busy guard, SDK call start/settlement, authenticated wire ingress, handler
entry/settlement, and fetch response/body/timeout stages. `--diagnose-handoff`
stops after recipient PCM reaches an acknowledged, captured body question;
it cannot report full message/Brainstorm journey acceptance. Production SDK
timers and acknowledgments are unchanged. The instrumented temporary copy
[typechecks](evidence/remediation/prompt-handoff-diagnostic-preflight-after.json)
after a fixture-only naming collision was corrected. No UI or simulator was
launched for that compile check.

CUA reported a locked Mac before foreground inspection. The user was asked to
unlock it; a live visual run remains pending. No attempt was made to circumvent
the lock or the earlier blocked, unrelated emoji probe.
