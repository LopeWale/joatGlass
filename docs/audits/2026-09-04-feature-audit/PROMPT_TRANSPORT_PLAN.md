# Production prompted-input boundary

September 4, 2026. Read-only design from the Hub and Mac agents, consolidated
by root. This is the next implementation slice, not a working-product receipt.
Official contracts are linked in [REPAIR_SOURCES.md](REPAIR_SOURCES.md).

## Ordered path

1. Mac begins one current owner/field ticket with a monotonic deadline. Send
   `prompt.present` with prompt ID, exact question and SHA256 presentation
   binding, authenticated session string, expected microphone source and
   remaining expiry. Register the pending request before sending. A matching
   `prompt.cancel` revokes it; duplicate publication must not renew it.
2. The Hub owns one current question. Its presenter returns an exact result
   for the complete question, after every bridge write succeeds. Neither an
   ordinary overlay queued behind another frame nor a question hidden by a
   confirmation counts. The current four-line tail truncation cannot qualify
   as full-question presentation. Reject unsupported/overflowing questions
   until a complete presentation is available.
3. While presentation is pending, incoming PCM retains its original source,
   speaker provenance and epoch. Successful presentation establishes a new
   application ingress boundary: retire buffered pre-prompt audio under its
   original identity and discard any partial-byte carry. Send `prompt.ack`
   for this exact session/prompt/binding, then activate the epoch synchronously
   only if the nonqueued send succeeds. Subsequent SDK events inherit it.
4. Mac validates the acknowledgment against the pending object and actual
   authenticated socket. Fence VAD and activate the dispatcher synchronously
   before any following audio callback can be admitted. Resolving a future
   and waiting for the bridge coroutine to resume is insufficient ordering.
   The host ingress ordinal is local and must never come from the wire.
5. Preserve received epoch/source through VAD, completed STT and pending work.
   Accepted final text goes straight to the typed owner. Consumption/cancel,
   Back/Home, mute/source change, disconnect, timeout, owner replacement and
   confirmation transition revoke the Hub question and audio epoch as well
   as the Mac lease. Late completion cannot resurrect either side.

## Minimal seams

- Python `core/protocol.py`: strict bounded present/ack/cancel shapes and
  optional audio epoch. Legacy epochless audio stays ordinary conversation.
- Python `ble/hub_gateway.py`: current authenticated audio-session accessor,
  one owned request/waiter, direct captured-connection send, synchronous
  accepted-ack callback and audio-epoch forwarding. Preserve pairing, reminder,
  confirmation and the foreign voice-recovery edits. Clear the exact waiter
  on replacement/stop/disconnect/timeout/cancel; old cleanup cannot clear new.
- `services/hud.py`: optional typed prompt publisher, unavailable on transports
  without the contract. Ordinary `assistant()` is not a presentation receipt.
- `__main__.py`: bind the existing dispatcher/bridge, connection lifecycle and
  tools once. Migrate Brainstorm tap-to-topic before enabling the dispatcher,
  because its legacy unscoped bypass intentionally no longer applies then.
- Hub `types.ts` and `main.ts`: mirrored strict wire contract and dispatch.
  Cancellation must invalidate a pending paint immediately, even if ordinary
  downlinks are serialized behind the paint.
- `HudPresenter.ts`, `HudVisualSurface.ts`, renderer: exact question-owned paint
  and cancellation. Ordinary coalescing currently resolves replaced frames
  with successor results; that is insufficient. Incidental overlays cannot
  cover the current question. Consent remains authoritative.
- `AudioStreamer.ts` and `EventRouter.ts`: use the actual SDK event source.
  Preserve explicit-other evidence across batching; current aggregation turns
  self/other into eligible unknown. Homogeneous source/speaker/epoch segments
  avoid relabeling old PCM. Do not join an odd byte across a source/epoch edge.
- `MacLink.ts`: nonqueued generation-checked send for acknowledgments and
  epoch-bearing audio; neither may replay onto a new connection.

## Required failing scenarios

The first real token-authenticated gateway before run is retained in
`evidence/remediation/prompt-gateway-epoch-before.log`: all seven cases fail.
Both microphone sources lose old/current epochs, and malformed present epochs
are silently forwarded. The same socket remains usable; these are observed
transport defects, not fixture import errors. The new protocol tests are
authored separately and are outside the earlier frozen `prompt-news` gate.

- Hold the last tile: no ack/epoch before completion. Tile failure, replacement,
  hidden confirmation, overflow and stale completion never acknowledge success.
- Old buffered samples and an odd byte precede fresh samples; only fresh
  complete samples carry the new epoch. Phone events remain phone despite a
  requested glasses mic. Self/other and absent/other retain explicit other.
- Disconnect, Back, mute and replacement during paint invalidate immediately.
  Duplicate requests do not repaint/renew; old finally blocks preserve a new
  waiter. Pending acknowledgments never replay after reconnect.
- Real authenticated WebSocket sends ack immediately followed by PCM, with no
  sleeps: fencing and activation precede fresh voice admission. Wrong session,
  binding, source and epoch fail; old-epoch audio remains old after the ack.
- Re-run the real tool/bridge/PCM two-question journey through the actual
  gateway and typed publisher. Contacts, STT result and refusal are fixtures;
  production routing, exact review and no-send behavior must be observed.

SDK source metadata is not a physical acquisition timestamp. These contracts
prove application ingress and buffer separation, not optical visibility or
when host-delayed samples were physically captured. Simulator pixels and a
fresh physical-G2 speech journey remain separate acceptance gates.

## Activation callback ownership

The existing message publisher returns an acknowledgment asynchronously. Live
wiring must not merely fence in the gateway and then wait for that coroutine
to arm the dispatcher: a following audio task may run first. Prefer an explicit
synchronous activation callback supplied by the owner bridge to the publisher.
The gateway validates the ack, establishes the local ingress fence and invokes
that callback before returning to frame dispatch. The bridge validates the
same ticket/owner/session and arms exactly once there; its eventual publisher
return is a delivery result, not a second activation. Controlled host publishers
must exercise this same callback ordering in the next integration gate.
