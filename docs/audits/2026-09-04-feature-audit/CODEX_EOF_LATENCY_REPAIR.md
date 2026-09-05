# Codex completed-answer latency

The Codex CLI adapter accumulated stdout until process EOF before parsing the final assistant message. A completed answer therefore remained hidden while the process finished other cleanup. This was a host buffering defect, separate from model inference time, speech recognition and the image transport.

## Selected behavior and official evidence

Parse the CLI's JSONL events as they arrive. Keep the latest completed `agent_message`, then emit it once at `turn.completed` while continuing to drain and reap the child. The [official event contract](https://github.com/openai/codex/blob/main/sdk/typescript/src/events.ts) identifies turn completion separately from item completion. The [official SDK response collector](https://github.com/openai/codex/blob/main/sdk/typescript/src/thread.ts) retains the latest completed assistant message. Both source files were read live September 4, 2026.

An individual completed item is not enough to choose the final answer. Interim messages, reasoning and tool output must stay out of the wearer's final reply. Explicit generator closure must preserve child ownership after the first answer is yielded. A failure before delivery retains fallback behavior; a late failure after delivery must not rerun the turn or its tools through fallback, and must invalidate unsafe resume state.

## Reproduced evidence

The tests use the real subprocess adapter and `HudWriter`, with a scripted child emitting synthetic official JSONL events. Fixture files hold process EOF after turn completion, so the main assertion proves ordering rather than relying on an invented total latency. Companion cases cover interim/fragmented events, duplicate completion, EOF compatibility, fatal events with zero process exit, late failures, cancellation, closure and clean resume.

[Red log](evidence/remediation/codex-eof-red.log): ten failures and two passes in 5.88 seconds. The completed answer did not reach the HUD while EOF was held. Fatal JSON events were also mishandled. The two clean-EOF compatibility cases already passed. [JUnit evidence](evidence/remediation/codex-eof-red.xml).

The adapter now parses complete JSONL lines incrementally, retains the latest completed assistant item, and emits it once at `turn.completed`. It continues draining the same owned subprocess. Explicit closure and cancellation reap that child. Fatal stream events, nonzero exit or timeout invalidate its resume IDs; an answer already delivered is not rerun through fallback. Clean older streams without a completion event keep their EOF compatibility behavior.

[After log](evidence/remediation/codex-eof-after.log): **27 passed in 3.36 seconds**. The measured completed-event-to-first-HUD-send interval was **0.171 ms**, with `first_hud_send_before_eof_release=True`; before the fix the same held-EOF assertion failed. The improvement is removal of the entire intervening EOF wait, whose real duration depends on the child process. This single controlled measurement does not estimate model, STT, optical transport or physical-G2 latency. [After JUnit measurements](evidence/remediation/codex-eof-after.xml), [mypy](evidence/remediation/codex-eof-mypy.log).

Root source review checked final-event semantics, child closure, late-failure fallback suppression and thread invalidation. Account state represents known authentication, so a generic failure need not erase a prior successful login. Split UTF-8 decoding in the existing subprocess adapter remains a separate unverified concern; no multilingual CLI-output fidelity claim is made here.
