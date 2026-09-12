# Agent Terminal task lifecycle

This slice makes accepted A2A task progress, same-task questions, and stop outcomes visible through the existing conversation and isolated official simulator. The uploaded 0.1.88 package remains unchanged while this source work proceeds.

The adapter owns task identity and protocol state. It publishes typed, awaited updates with status text separate from answer artifacts. Poll snapshots must not be concatenated into a reply. The roster owns one replaceable status message per turn, preserving transcript identity and limiting redundant paints. Final answers and input-required questions enter the conversation once. Back changes navigation only; it must not cancel or resubmit work.

Stop first shows that confirmation is pending. A confirmed remote cancellation and a locally stopped wait with an unknown remote outcome must remain visibly distinct. Unknown outcomes retain the existing recovery requirement and must not submit another instruction automatically. Questions resume the same task and context; an authorization-required state does not imply approval for a tool.

Implementation order: adapter typed progress and protocol tests; roster presentation and cancellation integration; isolated simulator submitted/working/completed, input-required continuation, confirmed and unconfirmed cancellation for both routes; independent source review; final-source replay and scoped commit/push.

OpenClaw's current agent card advertises streaming disabled; Hermes advertises streaming enabled. This slice uses the already supported task polling contract. Hermes SSE event semantics, provider-specific tool approval negotiation, and durable restart persistence remain distinct subsequent work. No installed engine, pairing or physical glasses connection is used for verification.
