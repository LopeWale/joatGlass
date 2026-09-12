# Agent Terminal provider permissions

Verified against public upstream documentation/source on September 12, 2026. This is an integration capability assessment, not an attestation of the installed remote versions or their tool configuration. No remote configuration or credentials were changed.

## OpenClaw

The A2A peer credential authenticates a peer; it does not make that peer an operator. Routed agents retain their remotely configured tools and permissions. The documented security boundary is explicit: A2A task text cannot alter settings or resolve approvals, including operator-like commands. Required exec approvals must be handled by an authorized operator through a supported user channel or Control UI. The original task remains working until that decision; polling then observes its result. A working task alone does not prove an approval is pending. [OpenClaw A2A security](https://docs.openclaw.ai/channels/a2a#security)

JOAT therefore must not advertise a no-tools guarantee, manufacture approval IDs, or send an ordinary A2A message as an approval decision. This assessed A2A route has no peer command opt-in. [OpenClaw A2A security](https://docs.openclaw.ai/channels/a2a#security)

## Hermes

Hermes documents authenticated peer identities, remote agent execution and untrusted inbound peer text. Remote peer messages cannot invoke operator slash commands. [Hermes A2A README](https://github.com/NousResearch/hermes-agent/blob/main/plugins/platforms/a2a/README.md)

The examined inbound `_METHOD_TABLE` exposes send/stream, task get/list/cancel/subscribe, and push configuration methods. It exposes no approval-decision RPC. This supports a conservative **no negotiated tool approval control** descriptor; it does not establish that Hermes has no approval facilities in its other interfaces. Do not invent a dashboard approval button or decision endpoint. [Hermes A2A adapter source](https://github.com/NousResearch/hermes-agent/blob/main/plugins/platforms/a2a/adapter.py)

## Authentication and authorization are separate from an approval UI

`TASK_STATE_AUTH_REQUIRED` may represent OAuth access or human authorization. It is an interrupted task state, not a schema defining the operation, scope, approval identity or credential. The protocol requires an out-of-band route unless an in-band mechanism has been negotiated. The state itself cannot authorize an operation or subsequent turns. Consequently, a peer's auth-required state produces external authorization guidance, while an HTTP credential failure produces separate saved-credential guidance. [A2A specification, in-task authorization](https://a2a-protocol.org/latest/specification/#76-in-task-authorization)

`input-required` remains conversational input. Free text saying “approve”, a fabricated approval ID, a card description or an arbitrary extension declaration cannot become a JOAT approve/deny control.

## Extension assessment and remaining external limit

A2A permits extensions but requires a defined URI/specification and activation negotiation. A declaration alone is not an implemented authorization contract. The public OpenClaw and Hermes surfaces reviewed above do not identify an approval extension that JOAT can negotiate. JOAT implements no such extension. This is not a claim that no extension exists anywhere. [A2A extension specification and activation](https://a2a-protocol.org/latest/topics/extensions/)

Adding wearable approvals would require a supported provider mechanism defining the requested operation, stable decision identity, authority/scope, expiry, exact decision transport and outcome. For OpenClaw's documented A2A route, operator decisions currently belong to its supported user interfaces. Sending “approve” through `message/send` cannot close this gap.

## Local helper contract

`joat.agents.a2a_permissions.provider_permissions(provider)` accepts an explicit local provider setting (`openclaw`, `hermes`, or unknown). It does not infer identity from a URL, display name, Agent Card or generated reply.

The immutable descriptor exposes `tool_policy="remote_managed"`, `verified_no_tools=False`, `approval_rpc=None`, and disabled approval/denial controls. `permission_guidance` explains remote tool policy. `authentication_guidance` explains task-level external authorization; `credential_guidance` addresses connection credentials separately.

`permission_notice(provider, task_state)` recognizes only exact `auth-required` and `TASK_STATE_AUTH_REQUIRED` states and returns a static `authorization_required` notice. Other states and untrusted objects do not manufacture an actionable permission request. The helper performs no I/O and emits no remote text, URL, credential or config mutation.
