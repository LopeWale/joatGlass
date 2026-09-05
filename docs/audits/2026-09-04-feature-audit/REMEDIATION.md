# JOAT feature remediation

Goal: repair the 34 findings in AUDIT.md and demonstrate the changed behavior.
The audit is a historical baseline, not a completion receipt for repairs.

September 5 checkpoint: the 34-finding goal remains open. A passing automated
check is supporting engineering evidence only. Accept a user-facing repair
after inspecting the equivalent before/after visual journey and its actual
behavior; record simulator, live-service and physical-G2 limits separately.
The commit preserves the integrated voice-boundary prerequisite used by the
prompted-speech changes. Independent Whisper work remains outside this task.
Raw runtime captures remain local; publishing source does not establish product
acceptance or authorize a real message send or package upload.

## Execution plan

1. Repair unsafe or broken state transitions: Q1 QuickNotes Back, M1 live WAL,
   M2 message paging, M3 picker reselection, N3 News opening.
2. Connect conversational requests and explicitly prompted speech to feature
   controllers, preserving target selection, review and confirmation. Resolve
   speech ownership across Brainstorm, Terminal, Messages and navigation.
3. Complete reading and feature lifecycle gaps: message/note pagination,
   groups, history, notifications, news freshness and timer recovery.
4. Repair and measure the latency pipeline and remaining session/Terminal
   defects. Finish with a source-linked disposition for every audit finding.

Each slice follows: failing regression, minimal repair, the same regression
afterward, independent review, and simulator interaction where relevant.
Shared voice-recovery changes remain owned by their existing task. No real
message is sent and no package is uploaded as part of these fixtures.

## First-slice design and gates

| Finding | Bounded change | Baseline | Required after evidence |
|---|---|---|---|
| Q1 | Back from discard asks the Mac to keep the capture and render its current controls; never restore stale action rows | Save label dispatches confirm_discard | Back, refreshed Save and finish dispatches stop rather than confirm_discard; persistence requires a separate capture check |
| M1 | Read the live database through a normal read-only connection | Committed WAL row invisible | Committed rows visible; writes refused |
| M2 | Restore agreement between Mac paging affordances and Hub boundary gestures | 20 downward scrolls emit zero paging requests | Older and newer page requests reach the router |
| M3 | Disarm reply ownership on Back while retaining the mapping for the displayed picker | Reselection emits no downlink | Second selection opens history and arms the chosen thread |
| N3 | Prefer native list; recover using the existing visual list when native rebuild is rejected | Native false, menu closes, article not reached | List remains usable, selected article opens; both-failure remains false |

Root owns CapabilityMenu, Python capabilities, integration evidence and
documentation. One subagent owns the IMessageStore repair; another owns
HudPresenter/HudPageBuilder recovery. Parent owns test and simulator execution.

## Status

Current repair evidence after the typography correction:

| Requested improvement | Current evidence | Remaining boundary |
| --- | --- | --- |
| Existing compact banner/News font and usable reader width | [Compact UI receipt](COMPACT_UI_VISUAL_RECEIPT.md): existing font restored, same 861-character Note takes two pages rather than three, text width 393px to 488px | Physical-G2 readability |
| Full Message/Note/News text, complete Back footer and correct return selection | [Reader receipt](READER_ENTRY_VISUAL_RECEIPT.md): exact 980/974-character Message/Note bodies, seven inspected retry images, reused News journey | Emoji appearance unresolved; Search has no separate visual journey |
| Completed assistant answer appears without waiting for CLI EOF | [Latency repair](CODEX_EOF_LATENCY_REPAIR.md): held-EOF failure repaired, 27 CLI cases pass, measured 0.171 ms completion-to-HUD in a controlled fixture | Real model/STT/end-to-end glasses latency |
| Spoken answers to Message and Brainstorm questions | [Runtime integration](PROMPTED_SPEECH_PLAN.md#production-runtime-integration-receipt): production composition and 26 runtime cases pass | Fresh prompt-flow simulator acceptance pending; legacy Messages UI and Terminal capture still open |

The broader 34-finding goal remains incomplete. These scoped results do not
close the remaining assistant, session, navigation, messaging, overview, news
retrieval or timer findings. Records below retain historical approaches and
failures; the current font direction is the user's existing custom canvas UI.

The matched Wave 2 comparisons now demonstrate controller and simulator
improvement for Q1, M2, M3 and the complete-text reader in Q2. All 34 images
were reviewed; [the visual report](WAVE2_VISUAL_REVIEW.md) links measurements
and before/after captures. News succeeds before and after in isolation, so
that run does not prove its earlier recovery repair. It exposes a separate
Back highlight/target mismatch. Its subsequent repair is now visually
verified: Story 02 remains selected and reopens, documented in
[the News selection receipt](NEWS_SELECTION_RECEIPT.md). Physical-G2 acceptance,
the longer multi-feature sequence and recording-transcript visuals remain
open. The chronology below retains earlier checks and failed attempts.

- M1: five live-WAL reader failures reproduced; read-only guard passed.
- Q1/M2/M3: the new normal-path tests failed first, then the targeted after
  run passed 76 Python and 164 Hub tests. Those results precede subsequent
  race/timeout hardening and are not a final gate for the current candidate.
- Independent review found a lost-response QuickNotes Back loop and two
  Messages read races. Their new regressions failed before the corresponding
  repairs. A fresh combined check is required.
- N3: both recovery tests failed before the change and passed in the targeted
  Hub run. Actual native-simulator opening, selection, article and Back are
  still pending.
- CPJ job `job-mtngihzc-03ff1764` passed 80 Python tests, strict mypy across
  98 source files and 713 Hub tests. It then failed compilation because three
  new assertions used Array.at beyond the repository's TypeScript target.
  Indexed access corrected that failure and the subsequent build passed.
  The simulator stages did not run in that job.
- Further review found Back restored the menu cursor without its visible
  highlight. The new regression observed row zero instead of row one;
  restoring the presenter selection before enabling input now passes all
  84 menu tests. The changed Hub source requires a fresh Hub gate.
- The comparison harness now explicitly selects its isolated gateway,
  awaits plugin transition completion and verifies News Back/reselection.
  Its upcoming simulator run still requires frame-by-frame review.
- CPJ `job-mtngwa52-31d20218` now owns the revised Hub gate and both simulator
  journeys. The earlier Python/mypy results remain applicable because their
  source is unchanged. The runner checks the exact delta against the first
  batch and retains a new `remaining-receipt.json`.

That second job stopped before the simulator: 712 Hub tests passed and the
EventRouter composition test dispatched its second Back before restored
selection completed. Its happy-path synchronization now awaits the actual
`messages-list` receipt. Two new regressions separately verify rapid Back
cancellation with late selection success and failure. All 153 menu/router
tests and a fresh Hub build pass. Product source is unchanged since the
second job; only tests and evidence harnesses changed. The still-unexecuted
simulator pair is now a separate finite runner, `verify_simulator_pair.py`.
It ran as CPJ `job-mtnh2o3z-ade94d59` and failed in the baseline after
41.83 seconds. The baseline visually reproduced the QuickNotes defect:
Back displayed Save and finish, but selecting it displayed Discarded Voice
Note. The runner then failed waiting for Messages; the candidate run did
not execute. Its frozen source is recorded by `simulator-pair-event.json`.
The accepted Anti-Churn receipt records a local failure, not product success.

Two bounded diagnostics subsequently passed: immediate QuickNotes to
Messages, and immediate Save to Messages. They do not explain the failed
longer sequence. Live Computer inspection is currently unavailable because
the tool reported the Mac locked and automatic unlock unsuccessful. This
does not establish that the lock caused the simulator failure. The original
failed output and both diagnostic receipts are retained separately.

The first failed batch has an accepted Anti-Churn local-failure receipt.
`remaining-reservation.json` was superseded before execution when the review
found the highlight defect; it is not passing evidence. The actual launched
candidate is bound by `remaining-2-event.json` and its accepted local-failure
receipt. The later 153 menu/router tests and build have a separate accepted
passing receipt.

The comparison runner is `verify_first_wave.py`. Baseline uses the original
HEAD versions of the four repaired production files in disposable source
copies. Both runs keep the rest of the current source, identical synthetic
data and the same simulator actions. Neither run modifies the shared source,
uses the host microphone, or sends a real message. Initial feature entry is
injected at the Mac controller; disputed subsequent gestures use the actual
simulator, EventRouter, WebSocket and Mac router. The installed simulator
hardcodes right-temple event sources. Its double-click source is explicitly
normalized to the official left-temple enum in the disposable fixture to
exercise contextual Back; the original and normalized sources are recorded.
This is not proof of physical left-temple recognition. QuickNotes uses fake
capture controls and proves dispatch only, not recording persistence or STT.

## QuickNotes persistence evidence

`verify_note_persistence.py` completed in 0.92 seconds using production
AudioRouter, CapabilityRouter, capture controller, HudWriter, DurableStore
and VoiceNoteStore. Input was synthetic PCM and deterministic transcription;
there was no live microphone, real recognition, external call or device.
The receipt is `evidence/remediation/note-persistence-after.json`.

| Journey | Observed persisted outcome after closing and reopening the database |
|---|---|
| Discard prompt, Back, Save | Same capture and audio survived Back; two transcript sections retained; healthy-save policy intentionally purged raw audio |
| Discard prompt, Back, partial transcription failure during Save | One completed transcript section and both raw frames (640 bytes) retained for recovery |
| Discard prompt, explicit Confirm discard | No transcript or raw frames retained; no save hook; durable discarded state |

This supplies the production persistence gate absent from the simulator's
fake capture controller. It does not replace the pending matched UI journey
or establish speech-recognition quality or physical-G2 delivery.

## Conversational and full-reader implementation in progress

Three agents own disjoint slices: typed feature tools, complete QuickNotes
reading, and message preparation with controller-owned confirmation/send
lifecycle. Root owns composition and execution. The active board records the
exact paths; existing voice recovery and release work remain preserved.

The new feature adapter is registered after CapabilityRouter composition in
the same ToolBelt used by the local engine and subscription CLI MCP endpoint.
It opens the existing features, saves exact QuickNote text with same-process
retry protection, and controls the real Pomodoro service. Its initial 50
behavior regressions failed against importable stubs. All 70 current adapter
tests now pass, including four ordinary-chat cases that initially exposed an
extra local-model decision pass. Hints avoid that regression for those cases;
they remain a heuristic, and elapsed model/device latency is not established.

The root's first MCP integration fixture failed in test setup because it
passed a transport where StatusPublisher requires a file path. That fixture
is corrected; the failed attempt is retained and is not defect evidence.
The actual endpoint/controller/persistence integration gate is separate.

That corrected integration passed in 0.25 seconds. A second MCP journey now
performs relationship clarification, real-controller contact selection,
exact-body preparation, held confirmation, status polling and same-ID replay.
All 72 message adapter tests plus both endpoint integration journeys pass
(74 total). The sender is synthetic in the message journey; this is not a
live-contact, Apple delivery or voice-recognition receipt.

The reader baseline now contains three valid Python failures and four Hub
failures. Earlier transcript-padding and canvas mock errors were corrected
before counting them as product evidence. Reader implementation is underway;
the first after run passes 70 Mac tests and 244 Hub tests. The subsequent
independent typed-message cancellation repair passes all 76 Mac capability
tests. The reader's matched simulator result remains pending.

Message controller tests initially passed 49 cases. Integration review then
reproduced nine cases where a pending action swallowed unrelated speech and
six where Home/disconnect failed to cancel a draft created without selecting
a picker row. The corrected controller passes 58 tests; the corrected router
passes the 76 tests above. Strict mypy passed 100 source files before the
subsequent sender edit. These results do not establish prompted speech:
the existing wake gate, greeting interception and local-model decision
context still need the scoped A2 dispatcher.

Sender review also found body stripping and unbounded subprocess lifetime.
After adding only the constructor seam, 23 meaningful regressions fail and
three existing cases pass. A real isolated Python child remains alive after
cancellation before the repair; test cleanup reaps it. No test launches
osascript. The repaired sender passes all 26 cases, including observed child
exit after cancellation and timeout. Apple delivery remains unverified.

`verify_repair_wave2.py` freezes the candidate, runs one combined source gate,
then compares QuickNotes controls, Messages paging/Back, News recovery and
the complete-note reader in fresh simulator instances. It retains all
independent journeys if one fails. The unresolved longer multi-feature
sequence remains explicitly open; isolated runs cannot silently close it.

That Wave 2 run passed 315 Python tests, strict mypy on 100 files and all 720
Hub tests, then stopped at a test-only TypeScript optional-value error before
any simulator run. The guarded test access now passes all 88 menu tests and
the complete build. `verify_wave2_visual.py` reuses those logs after checking
the source delta, freezes the additional Hub configuration inputs, and runs
only the previously unexecuted comparisons. The reader comparison also
checks honest page-counter progression. Its visual results remain pending;
recording-transcript visuals and the longer multi-feature sequence remain
separate open acceptance work.

See [official repair contracts](REPAIR_SOURCES.md) for cited API constraints
and the distinction between documented support and demonstrated improvement.
Source/unit, simulator and physical-G2 evidence remain separate; full device
latency and external-service behavior cannot be inferred from fixture timing.
