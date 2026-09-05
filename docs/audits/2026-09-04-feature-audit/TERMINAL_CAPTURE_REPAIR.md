# Terminal capture repair

Status: implementation and focused host checks complete; the full combined gate and Terminal simulator visual acceptance are pending. This is a bounded repair to Terminal speech ownership, not acceptance of the whole Agent Terminal feature.

## Defect and intended behavior

Before repair, `AgentTerminalCommands.arm()` stores only a 45-second deadline. Its next claimed utterance is stripped and passed to `roster.send()` without a captured target; `send()` resolves the current focus. Changing focus, replacing a lane, or resuming another session inside the same adapter can therefore redirect speech promised to an earlier target. The old command parser also interprets cancellation words instead of preserving every prompted instruction literally. [Terminal commands and roster](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/services/agent_terminal.py).

The intended journey is: tap the selected Terminal, see its exact question, then speak one instruction that reaches only that captured context. A late answer must not start work after leaving the feature or transfer into a replacement context. Ordinary explicitly addressed Terminal commands and the existing single queued instruction while busy remain available.

## Implemented binding and API

- `AgentTerminalCommands.begin_capture()` returns an owned capture containing a unique `capture_id`, displayed `lane_name`, exact `question`, and original `expires_at`. `current_capture`, `clear_capture()`, and `consume_capture(capture, text)` manage that owner. Duplicate taps preserve its identity and deadline; copying its fields does not confer ownership.
- The roster validates the captured lane and adapter identities, focus/adoption revision, adapter context generation, and relevant turn/queue revision. Resume invalidates before its first await, including failed resume. Focus away and back, same-name replacement, intervening work, changed queue, Home and reconnect cannot revive an old capture.
- The adapter exposes `capture_context_generation: int | None` and `stream_captured(prompt, *, context_generation)`. Captured submission rechecks the generation after acquiring the adapter lock and must not initialize a missing or replacement context. A stale request must leave a newer valid context intact. Scheduled and queued dispatch also retain the original guard.
- Terminal and Brainstorm now share `_publish_owned_capture()` in `PromptedSpeechRuntime`: publish the exact question, await its matching presentation acknowledgment, and admit one fresh source/epoch-bound utterance before consumption. Retirement clears only the corresponding owner. The typed path does not arm the legacy command parser and passes the accepted body exactly, including punctuation, newlines and outer spaces. This exactness promise covers prompted input.
- The main callback binds the capture synchronously before returning its publication coroutine. Global Home/recording/shutdown revocation also clears a capture that has no ticket yet. SessionManager checks its lifecycle before invoking the callback. [Main callback](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/__main__.py:1050), [pre-publication revocation](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/services/prompted_speech.py:167).
- The existing session-key/agent-scoped `sessions.send`, idempotency key and run-scoped abort contract remain. An immediate local sent/queued receipt does not establish completed remote execution.

The owner/queue regressions live in [test_agent_terminal.py](/Users/emmanuelakinwale/Developer/joatGlass/tests/unit/test_agent_terminal.py); adapter-lock/lifecycle regressions live in [test_openclaw_runtime.py](/Users/emmanuelakinwale/Developer/joatGlass/tests/unit/test_openclaw_runtime.py). Shared presentation and audio admission belong to [prompted_speech.py](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/services/prompted_speech.py:74), with real Commands/Roster composition tested in [test_prompted_speech_runtime.py](/Users/emmanuelakinwale/Developer/joatGlass/tests/integration/test_prompted_speech_runtime.py).

## Observed before evidence

| Retained result | What it establishes |
|---|---|
| [Terminal capture red](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/terminal-capture-red.log): **23 failed in 0.38s** | Failures cover exact one-time input, legacy-parser separation, Home/reconnect, focus away/back, same-name replacement, held/failed resume, intervening work, busy queue changes, adapter changes before/scheduled/queued dispatch, duplicate taps, copied owner and unavailable context. |
| [Adapter context red](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/terminal-context-red.log): **3 failed, 1 passed, 25 deselected in 0.07s** | Reinitialization reused the baseline context value; a send held behind the real adapter lock reached a replacement; captured submission initialized a missing context. Literal input in an unchanged context passed. |

The baseline tests use compatibility adapters to exercise the old arm/stream behavior when the planned APIs are absent. Their failures are behavioral, rather than import or missing-method errors. Dependencies are synthetic; the adapter cases exercise the production adapter lifecycle with a recording Terminal fixture. These are local reproduced mechanisms, not observed sends to a live model or Gateway.

## Focused host evidence

| Boundary | Observed result and scope |
|---|---|
| Terminal ownership and queue | After the initial 23 failures, review reproduced a cancellation race between turn completion and queued-input settlement: [one additional failing case](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/terminal-queue-cancel-red.log). Cancellation now clears the captured queue before that completion callback can dispatch it. The [full Terminal unit suite](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/terminal-capture-after.log) passes **122 tests in 0.68s**. |
| Adapter context and lifecycle | The initial three failures were followed by [four lifecycle regressions](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/terminal-context-lifecycle-red.log) covering generator close, close during initialization, and concurrent initialization. The [full adapter suite](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/terminal-context-lifecycle-after.log) passes **34 tests in 0.09s**; [mypy passes on two source files](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/terminal-context-mypy.log). |
| Shared prompted-speech runtime | [Twenty new integration cases failed before wiring](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/terminal-runtime-red.log), with 28 existing cases deselected. The [complete runtime suite](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/terminal-runtime-after.log) then passes **48 tests in 0.15s**, and [mypy passes on three source files](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/terminal-runtime-mypy.log). Coverage includes literal consumption, exact acknowledgment, stale/replaced owner cleanup, held publication, unchanged duplicate deadlines, unavailable transport, and recording start while completed STT is held. |
| Session tap/publication ordering | [Two cases failed and one passed before the change](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/terminal-entry-red.log). Home before queued publication and a closed SessionManager could still invoke capture. [All three focused cases now pass](/Users/emmanuelakinwale/Developer/joatGlass/docs/audits/2026-09-04-feature-audit/evidence/remediation/terminal-entry-after.log) in **0.07s**. |

The runtime regressions establish a new integration contract; they are not evidence that these exact scenarios occurred in the original user incident. The real AudioRouter/VAD case uses synthetic PCM and deterministic STT, and the Terminal adapter records controlled submissions without calling a model or Gateway. Temporary constructor/method compatibility seams have now been removed from the runtime tests; those tests bind directly to the implemented provider and publication method. Two additional Brainstorm pre-publication lifecycle regressions now await the root's baseline run: recording start/end and shutdown before its question task runs. The recorded 48-test result precedes that test-only cleanup and those additions, so a fresh combined gate remains pending.

These host results do not establish Terminal simulator visual acceptance, real microphone recognition, live remote execution, physical-G2 delivery or normal-response latency improvement.

## Official contracts and JOAT policy

Official pages below were opened during this investigation. OpenClaw pages were read through the web tool; the Even pages were fetched live through Keenable after the web reader failed. The Even pages report a 2026-08-25 update date.

| Official source | Supported contract and boundary |
|---|---|
| [OpenClaw session attachment](https://docs.openclaw.ai/concepts/session-attachment) | The Gateway owns session state; clients continue a canonical session instead of cloning its transcript. Reference resolution belongs to the Gateway. JOAT's display lane name is not authoritative remote identity. |
| [OpenClaw session lifecycle](https://docs.openclaw.ai/concepts/session) | A reset can change `sessionId` while retaining the session key. Local generation checks must not be represented as proof against every remote reset. |
| [OpenClaw Gateway protocol](https://docs.openclaw.ai/gateway/protocol) | Session-specific sends, idempotency keys and explicit run-scoped abort are supported. Submission acknowledgment is distinct from transcript persistence. `expectedSessionId` is documented for particular mutations; it must not be invented as a universal send field. |
| [OpenClaw client recovery](https://docs.openclaw.ai/gateway/clients) | Reconnect requires renewed subscriptions and authoritative history/run reconciliation. Exact run identities and sequence checks matter; disconnect alone does not prove accepted work stopped. Current documentation requires version compatibility to be verified with the deployed Gateway. |
| [Even Device APIs](https://hub.evenrealities.com/docs/build/device-apis) | Select glasses/phone using `audioControl`; `audioEvent.source` identifies PCM origin. Glasses capture requires startup-page creation. Ordinary input uses one capture target; long-press events use `sysEvent`, lack `eventSource`, and may be OS-owned, so cannot be the sole feature entry. |
| [Even display](https://hub.evenrealities.com/docs/build/display) | Serialize image updates and check results. Accepted updates are call results, not optical delivery receipts. The question/utterance ownership protocol is JOAT's responsibility. |
| [Even page lifecycle](https://hub.evenrealities.com/docs/build/page-lifecycle) | Startup, rebuild and in-place updates have distinct result contracts. Rebuild replaces page state; retained menus must be supplied again. This does not provide a vendor `promptId` or audio-epoch API. |

The same-connection `involvingMe` listed-key allowlist, separate Brainstorm/work policy lanes, capture expiry, prompt acknowledgment binding and no stale speech replay are **JOAT ownership policies**. The [roadmap](/Users/emmanuelakinwale/Developer/joatGlass/docs/AGENT_TERMINAL_ROADMAP.md:76) and [gateway adapter](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/agents/openclaw_gateway.py:420) define those restrictions; they are not additional OpenClaw or Even requirements.

## Explicit remaining limitations

**First-use setup remains a blocker.** The default work lane is deliberately
uninitialized at startup, but its idle roster frame says ready. Selecting it
currently focuses and repaints without initialization. The new capture
correctly refuses a missing context and therefore cannot yet offer a question
on that fresh lane. Normal lane opening needs bounded setup and a truthful
ready/failure receipt, with ownership rechecked before any late repaint.
Captured input must continue to reject reconnecting itself into a new context.
This prevents declaring the Terminal feature usable despite passing the scoped
ownership tests; it is the next repair after the combined gate.

The latest runtime check is **50 passing cases**. Its two new pre-publication
Brainstorm cases first had a fixture registration error (retained separately);
after correcting the AudioFrameSource fixture, both reproduced the actual
surviving TopicCapture in `topic-prepublication-corrected-red.log`. Global
runtime revocation now clears that pending owner before recording or shutdown.
The Terminal-entry check now has **5 passing cases**, including an active
Brainstorm that has handed the HUD to Terminal; the ready Terminal tap no
longer falls through its unrelated session handler. These are host gates,
not visual acceptance or an assertion that all cross-feature opening paths work.

Question screenshots independently confirmed the compact monospace repair,
but exposed a conflicting resting wake-word banner. A reproduced renderer
case now keeps that question banner at `JOAT` and retains its direct-answer
footer; all **101 renderer tests** pass. A fresh screenshot of this final
banner cue is still pending along with the simulator speech journey.

**Remote session reincarnation:** the inspected `OpenClawAgentTerminal.attach()` stores an empty `session_id`. A local context generation detects local invalidation/replacement; it does not by itself detect a remote reset behind an unchanged key and connection. Full incarnation protection needs an authoritative identity/reconciliation contract and separate tests. [Attachment implementation](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/agents/openclaw_terminal.py:253).

**Adopted-session recovery:** initialization re-lists an adopted key on the new connection, but `OpenClawHistoryRecovery.recover()` currently rejects keys outside the dedicated namespace. Successful local capture binding does not close that recovery limitation. Do not replay an uncertain submitted instruction to make recovery appear successful. [Runtime recovery and attachment](/Users/emmanuelakinwale/Developer/joatGlass/apps/mac/joat/agents/openclaw_runtime.py).

## Explicit opening repair, in progress

The combined pre-opening candidate passed its frozen regression gate: **1,444
Python tests (90.91s), 877 Hub tests, mypy on 104 files, and the Hub build**.
The [gate receipt](evidence/remediation/terminal-composition-gate-receipt.json)
records unchanged source hashes and the remaining simulator/first-use limits.
This result is supporting host evidence, not a successful simulator journey.

The first-use failure is now separately reproduced. A fresh work lane reports
ready without an attached context, and its row tap only focuses/repaints.
Sixteen preparation cases failed before implementation; one already-prepared
busy-lane control passed. Seven Mac opening tests reproduced missing selection
correlation and stale work surviving Home/new selection/disconnect. Sixteen Hub
cases reproduced unbound late frames; one synchronous selection control passed.
These controlled lifecycle cases test the mechanism, not a claim that every
race occurred in the user's original incident.

The repair prepares only the explicitly selected lane, shows connecting/error
state until a context exists, and keeps capture consumption unable to reconnect.
Opaque visit and selection IDs follow its connecting, ready and subsequent
frames. Both the Mac and Hub retire them when navigation moves. A guarded image
paint must also reject a departed frame after an SDK await. Retry targets the
same selected lane. A small composition controller binds the actual capability
router to the roster and transport generation, making the production opening
path testable without starting models or sending terminal instructions.

Official contracts were reopened for this slice: [OpenClaw attachment](https://docs.openclaw.ai/concepts/session-attachment)
assigns canonical session state to the Gateway; [client recovery](https://docs.openclaw.ai/gateway/clients)
requires renewed subscriptions/reconciliation. The eight-second local preparation
budget and the visit/selection IDs are JOAT policies, not vendor APIs.
[Even page lifecycle](https://hub.evenrealities.com/docs/build/page-lifecycle)
requires serialized image writes and distinguishes rebuilds from in-place
updates. [Even display](https://hub.evenrealities.com/docs/build/display) describes
image acceptance; that receipt alone does not prove illuminated pixels.

Fresh composition tests, final regression checks, and simulator inspection for
this opening repair remain pending. The existing failed prompt simulator runs
remain failures. No physical-G2 or live Gateway execution claim is made.

The opening repair now passes its [focused composition check](evidence/remediation/terminal-opening-focused-after.log):
**600 tests in 63.82s**, spanning the roster, actual capability-to-opening-to-prompt
composition, protocol, router and session controls. [Full mypy](evidence/remediation/terminal-opening-mypy.log)
passes on **105 files**. The [Hub checks](evidence/remediation/terminal-opening-hub-after.log)
pass **206 tests** across the opening, menus and presenter; the [build](evidence/remediation/terminal-opening-hub-build-after.log)
passes after correcting two test-only uses of an unsupported array helper.
The broad frozen-candidate regression gate is next; these focused results are
not a simulator pass.

Review also reproduced and corrected a regression introduced by the new guard:
a spoken Terminal command after Home changed focus but could not repaint
([failing case](evidence/remediation/terminal-opening-voice-red.log)). Recognized
commands now acquire a navigation-owned view before their first await. Commands
inside an accepted picker visit preserve its frame binding; spoken entry after
Home uses the existing unbound route with a Mac navigation guard. Ambient speech
and scoped capture never acquire that view. A separate unbound narration no
longer duplicates an owned Terminal's bound transcript frame. Retry and cycle
capture their original opening before their scheduled task runs, so a new
selection cannot become their target.

The [first composition run](evidence/remediation/terminal-opening-composition-first.log)
also caught two concrete boundary errors, subsequently corrected: long setup
copy exceeded the actual 64-character wire field, and a lane named work-calendar
matched work before its exact name. Its third failure was a test expectation:
a departed cycle must return empty text so SessionManager does not narrate over
Home. The initial SessionManager regression fixture used an undefined fake;
only [the corrected failing run](evidence/remediation/terminal-opening-session-corrected-red.log)
is product evidence. The corrected presenter tests pass all three stale-paint
cases ([result](evidence/remediation/terminal-opening-paint-after.log)).

Remaining limits for this slice: a previously sent unbound voice frame and an
old action already in transit without opening IDs cannot acquire the selected
picker's identity guarantee retrospectively. Actual Gateway readiness, complete
main-process startup, spoken recognition, simulator appearance/interaction, and
physical-G2 behavior remain separate acceptance gates. The local preparation
timeout is eight seconds plus bounded cleanup/presentation grace, not an external
service latency guarantee.

The frozen 214-file Terminal-opening candidate completed its broader gate:
**1,648 Python tests in 91.54s, 904 Hub tests, mypy on 105 files, and build all
passed**. The [receipt](evidence/remediation/terminal-opening-gate-receipt.json)
records no source changes during the run; [accepted evidence](evidence/remediation/terminal-opening-accepted.json)
attaches its digest. This advances host verification only. The next CUA visual
inspection was blocked because the Mac was locked; an unlock was requested.
