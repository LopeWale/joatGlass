# Agent Terminal task lifecycle

## Earlier task lifecycle slice

This slice makes accepted A2A task progress, same-task questions, and stop outcomes visible through the existing conversation and isolated official simulator. The uploaded 0.1.88 package remains unchanged while this source work proceeds.

The adapter owns task identity and protocol state. It publishes typed, awaited updates with status text separate from answer artifacts. Poll snapshots must not be concatenated into a reply. The roster owns one replaceable status message per turn, preserving transcript identity and limiting redundant paints. Final answers and input-required questions enter the conversation once. Back changes navigation only; it must not cancel or resubmit work.

Stop first shows that confirmation is pending. A confirmed remote cancellation and a locally stopped wait with an unknown remote outcome must remain visibly distinct. Unknown outcomes retain the existing recovery requirement and must not submit another instruction automatically. Questions resume the same task and context; an authorization-required state does not imply approval for a tool.

Implementation order: adapter typed progress and protocol tests; roster presentation and cancellation integration; isolated simulator submitted/working/completed, input-required continuation, confirmed and unconfirmed cancellation for both routes; independent source review; final-source replay and scoped commit/push.

OpenClaw's current agent card advertises streaming disabled; Hermes advertises streaming enabled. This slice uses the already supported task polling contract. Hermes SSE event semantics, provider-specific tool approval negotiation, and durable restart persistence remain distinct subsequent work. No installed engine, pairing or physical glasses connection is used for verification.

## Streaming, permissions, and restart continuation — September 12

This implementation adds Hermes SSE observation and durable Mac restart recovery. Provider identity is an explicit saved connection setting, independent of display name. Hermes uses its streaming binding; OpenClaw retains task polling. Neither path may resend an instruction after an ambiguous transport failure. Stream artifact updates replace or append by artifact identity and preserve one answer in the visible conversation.

Tool permissions follow the actual provider contract. The OpenClaw A2A peer credential authorizes task submission, not operator approval resolution. Hermes peer trust and advertised skills likewise do not constitute a per-tool approval API. The UI must identify where authorization belongs and must never turn arbitrary agent prose or generic auth-required into an executable approval request.

The existing DurableStore retains bounded transcript and adapter state atomically, keyed by stable profile ID plus exact endpoint. Persist admission before sending; restore accepted work as Check task status, and unknown submissions without a task ID as uncertain with no automatic replay. A normal engine shutdown detaches local observation rather than invoking wearer Stop. New process captures are always fresh; drafts, microphone ownership, and queued instructions never restore.

Verification covers fragmented SSE, duplicate/replaced artifacts, interruption without resubmission, provider permission boundaries, database failure and separate-process restart recovery, followed by an immutable-source official simulator walkthrough. Physical glasses and the installed engine remain outside this verification run.

### Provider contract limits

The reviewed [Hermes streaming handler](https://github.com/NousResearch/hermes-agent/blob/main/plugins/platforms/a2a/adapter.py) emits a submitted task, working status, then the final artifact/status. It ignores non-final gateway sends. [Its protocol helper](https://github.com/NousResearch/hermes-agent/blob/main/plugins/platforms/a2a/protocol.py) uses JSON-RPC envelopes containing nested `statusUpdate` and `artifactUpdate` objects, blank-line SSE boundaries, and a final comment. Partial artifact append/replacement tests exercise A2A client capability, not proof of token deltas from this deployed provider. Hermes can report failure when its stream client disconnects; JOAT preserves the receipt and queries the provider's actual result rather than promising that remote work survives every interruption.

[OpenClaw A2A](https://docs.openclaw.ai/channels/a2a) supports `configuration.returnImmediately`, defaults to 30 calls/minute per peer, and refuses cancellation without a real abort seam. Production OpenClaw polling therefore uses three-second intervals. Both providers may keep task records only in process memory: local Mac restart persistence cannot recreate task records lost by a separate provider restart. A missing remote task remains a recovery issue and is never permission to resend it.

### Final source verification

The final isolated official simulator run passed with 40 captured screens and 208 identical production source fingerprints before/after execution. The primary checkout also matched every captured source hash after the run. The source-map SHA-256 (sorted compact JSON) is `83ead992b580083bfd965ebad860022382dae57d7452d0db765d399832b88bae`.

- Menu → exactly OpenClaw/Hermes → prompt → working reply → Back/reopen, with exact transcript-pixel retention for both providers.
- Hermes fragmented nested v1 SSE: partial artifact, append, replacement, scrollback and final artifact. This is a controlled protocol fixture, not a claim of token streaming from the deployed Hermes server.
- Both lanes reconstruct the Mac engine objects and reopen the same leased SQLite database, recover their exact accepted task, and retain conversation identity/history. Independent frame checks confirm the latest durable request has exactly one complete assistant answer and only Completed progress. Separate subprocess tests cover actual process death.
- Both providers show static external-authorization guidance; subsequent owned-task queries observe fixture operator completion and remove stale guidance. Exactly three `message/send` calls and three `message/stream` calls; no replay, remote cancellation or approval RPC.
- All four isolated listeners (18980, 18981, 5298, 9998) were released and the generated TLS private key removed. No installed engine, phone pairing, physical glasses, remote credential or live provider was used.

Local receipts and raw/green framebuffer captures: `dist/mac-workspace/control-audit/agent-terminal-streaming-restart/receipt.json` and adjacent images. Receipt SHA-256: `c6b6d18389494077b2468f7aa4de35a91aa5f6e5e26155a5fe80b49e9d80baf6`. Parent visually inspected partial/appended/replaced content, reading position, restart recovery, and both providers' authorization and completed screens.

Final diagnostics: 544 focused Python tests, including six subprocess restart scenarios and the held-recovery shutdown race; Python type checking passed for six affected modules; four native A2A connection settings tests passed. These are scoped checks, not a claim that every repository test passes. No package, installed Mac app or remote permission configuration was changed in this slice.
