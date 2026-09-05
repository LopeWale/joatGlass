# Messages UI speech repair

At the recorded baseline, the New Message row called the legacy `begin_compose`; bare Reply armed `_PendingReply`. Neither entry used the acknowledged prompt runtime. Typed preparation inside the legacy draft lock rejected itself as busy. Unaddressed Reply was also dropped by the upstream wake gate despite the reader footer teaching it. The current implementation connects these entries to the acknowledged runtime and binds prompts and receipts to their owning conversation visit.

## Bounded implementation

1. Preserve the existing compact renderer, conversation reader and explicit send confirmation.
2. Reserve a Messages visit synchronously when its row is selected. New Message prepares a typed action; selected Reply prepares against the private, unexpired selected conversation. Both publish through the existing acknowledged prompt runtime.
3. Give each UI action an opaque identity. An additive `message.ui` envelope binds action, request and selection. Hub admission and guarded painting reject late questions/outcomes after Back or another selection, including an in-transit response.
4. Observe committed controller outcomes without changing send semantics. Display submitted/delivery-unverified, definite failure, cancellation and uncertainty honestly. Observer failure cannot cause another send.
5. Admit only the literal selected-conversation Reply entry before the ordinary wake gate, with fresh glasses audio provenance, current connection and selection. Prompted body text remains exact and bypasses command parsing. Other ambient speech retains its existing gate.

Root owns test execution. Agent assignments: controller seam and focused tests; Hub binding and display tests; narrow orchestrator entry and tests. Root owns Mac UI composition, protocol and journey integration. First record behavioral failures, then equivalent focused results and a frozen regression gate. Visual acceptance remains pending while the Mac is locked; host tests are not simulator or physical-G2 proof. No real messages are sent.

## Recorded evidence so far

- `message-ui-entry-baseline.log`: 2 behavioral failures. The New Message tap did not publish a question; a queued tap after Home armed the old compose flow. The earlier `message-ui-entry-red.log` and `message-ui-entry-corrected-red.log` included fixture setup/cleanup errors and are not accepted defect evidence.
- `message-ui-controller-entry-red.log`: 25 failures, 10 passing controls, 0.34 seconds. These cover missing typed Reply callback/status observation and the unaddressed entry hook. The new handled-empty response control was authored after that baseline.
- `message-ui-hub-ownership-red.log`: 25 ownership/display failures. `message-ui-hub-red.log`: 11 failures and 2 controls for prompt binding and guarded presenter lists; unrelated cases were skipped by the selector.
- `message-ui-entry-bounded-after.log`: 50 focused cases passed in 0.17 seconds at that intermediate source state. Source changes and additional cancellation controls afterward require a final gate.
- The first combined journey stalled during cleanup. Root terminated only the owned test process; `message-ui-stall-diagnostic.log` preserves a bounded 8-second reproduction with a stack in asyncio task gathering. Explicitly removing completed owned tasks after gathering fixed the observed shutdown spin. This is host runtime evidence, not a glasses latency measurement.
- `message-ui-mypy-first.log`: 106 source files passed at the intermediate source state.

## Focused verification before the regression freeze

- `message-ui-host-focused.log`: 57 host cases passed in 0.25 seconds, covering UI entry, controller seams, wake handling and confirmation ownership. `message-ui-protocol-after.log` separately passed the actual Python/TypeScript envelope match.
- `message-ui-hub-focused-after.log`: 359 tests across five Hub files passed in 529 ms. `message-ui-hub-build-after.log` passed TypeScript checking and the production Vite build. `message-ui-mypy-after.log` passed all 106 Mac source files.
- `message-ui-hub-confirmation-red.log` records five missing confirmation-owner failures before the correction. The passing focused run includes these controls. `message-ui-double-back-red.log` records a tagged Back-to-picker then Home leaving a stale row mapping; the passing host run includes its correction.
- The first combined Hub run exposed an incorrect test expectation for an existing reader hint and three strict-indexing test type errors. Those were fixture corrections, not additional product defects or evidence of product improvement.

These are controlled source and runtime results. A frozen broader regression run is still required. The test counts and runtimes do not measure wearer interaction speed or speech-recognition accuracy.

## External source and limits

Even Realities documents that glasses microphone audio requires a successful startup page, `audioControl` chooses the microphone source, audio arrives through `audioEvent`, and image updates must be awaited serially. This repair reuses those existing SDK paths and the compact renderer. It does not invent a glasses-side transcription API. [Official Even Realities SDK reference](https://github.com/even-realities/everything-evenhub/blob/main/plugins/everything-evenhub/skills/sdk-reference/SKILL.md).

The request, selection and action identities, audio fence and 45-second question lease are JOAT policies. The vendor source does not prove JOAT's integration, recognition accuracy, optical presentation or message delivery. Those require separate evidence. The Mac is locked at the last computer-tool check, so this slice has no new visually accepted simulator capture yet.

## Integration review corrections

The review found that cancelling an exact Messages prompt called global prompt retirement, which also clears prepublication captures for other features. The production callback now revokes only the matching prompt ID. Preparing writes are owned and cancelled/drained on retirement; stale failures return no generic narration. Four added controls cover held contact lookup, replacement selection, shutdown with a held write, and late failure after Home. `message-ui-cancellation-controls.log`: all 18 current UI integration cases passed in 0.13 seconds.

A second wire boundary appeared at confirmation: the durable journal action ID differs from the typed UI action ID. A delayed confirmation preview therefore also needs its UI owner carried separately. `message-ui-confirmation-red.log` records the missing owner on the actual typed UI path; `message-ui-confirmation-wire-after.log` passes the owner through the real WebSocket confirmation envelope while retaining the independent send identity and bound decline. The new optional field is presentation ownership only; it does not replace the existing session/challenge/payload/preview authorization.

Compatibility: the optional UI composition leaves legacy embeddings available. A Mac using this UI flow requires a matching Hub build for the added wire envelopes. Ambiguous contact address picking, physical speech recognition, real submission/delivery and the broad remaining audit findings are not closed by this slice.

## Frozen regression result

The `message-ui` candidate freezes 219 files. Its regression run passed 1,706 Python tests in 92.09 seconds, 947 Hub tests across 43 files, mypy for 106 source files, and the production Hub build. The runner and subsequent comparison both found no changed source. Evidence: `evidence/remediation/message-ui-gate-receipt.json`; accepted digest `sha256:8f7cd429d8103856c97bec2d343af9e4fd1cce3358e5753b341ff39e8812adaf`. This closes this candidate's host regression gate only; the Messages simulator journey remains pending.
