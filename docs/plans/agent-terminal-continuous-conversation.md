# Continuous Agent Terminal conversation

The active slice preserves existing A2A context and task continuation while replacing the lossy terminal-tail presentation with a complete retained conversation. Verification belongs to the isolated official simulator; the installed engine and physical glasses connection are outside this work.

## Ownership and data flow

The Mac owns a stable conversation ID and ordered user, assistant and status messages per agent. A conversation retains at most 64 messages and 262144 UTF-8 bytes; eviction removes whole oldest messages and explicitly marks earlier history unavailable. A single accepted A2A response fits the transport's existing 262144-byte envelope limit. Content is never shortened to a display row. This is bounded in-memory history, not a claim of persistence across Mac engine restart.

The terminal wire frame carries the complete retained message snapshot and stable identity in additive fields. Existing activity text remains a bounded compatibility fallback. The Hub measures and pages the full snapshot locally, remembers a reading anchor by conversation identity, follows new output only at the live end, and restores the anchor after Back or agent switching. Display state changes must not create a new conversation.

Prompt publication still owns its exact presentation receipt, microphone epoch and cancellation. Terminal prompts render inside the current conversation with an inline input region; unrelated feature questions retain their existing presentation. No unacknowledged or expired speech submission is admitted.

## Ordered implementation and verification

1. Add the bounded conversation domain model, wire validation and Mac projection; record complete turns and preserve identity until an explicit new conversation.
2. Add the Hub full-text viewport, stable reading anchors and inline terminal question presentation. Preserve Back, cancellation and delayed-frame ownership protections.
3. Strengthen the isolated TLS A2A simulator fixture to reject broken continuation and exercise three consecutive turns for each agent. Verify context-dependent follow-up, full beginning/middle/end of a long answer, inline follow-up input, Back/reopen, separate histories and cancellation.
4. Review the integrated diff and visually inspect settled simulator frames from the final source. Compile and run focused tests as independent diagnostics. Commit only scoped files and push after passing acceptance.

True provider streaming, tool-permission discovery, durable restart persistence and a new session-management menu are subsequent slices. This change must neither claim those capabilities nor disturb their existing backend safeguards.
