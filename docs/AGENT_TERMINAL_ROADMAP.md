# Agent Terminal roadmap

Agent Terminal is a dense CLI/chat interface for controlling existing Hermes
and OpenClaw work from the glasses. It is separate from Brainstorm and
never uses Codex CLI or Claude Code.

Status: the wearer-facing control surface and its gateway policy lane are
implemented, and as of 2026-08-31 the engine REACHES A LIVE GATEWAY — the
connect handshake is accepted and all three lanes (work, brainstorm,
calendar) construct without error. As of 2026-09-11, JOAT also contains an
authenticated A2A task adapter for OpenClaw and native Hermes. See the current
implementation and acceptance section below; direct Gateway session operations
and A2A task conversations have separate capability boundaries.

What that does and does not settle: the transport, the identity handshake
and the policy boundary are now exercised against a real deployment. The
wearer-facing operations on top of them — listing real sessions, sending
into one, answering a real approval — still need sessions to exist and be
driven. The acceptance gates at the end are unchanged; the blocker on them
is no longer "there is no live gateway".

JOAT remains the glasses brain and wearer-facing controller. Agent Terminal is
one JOAT capability: JOAT shows the external session roster, binds speech and
gestures to the focused session, relays bounded history and live output to the
HUD, and preserves host/session identity across navigation. Hermes and
OpenClaw remain the workers on their selected Macs; they do not replace JOAT as
the glasses interface.

## Intended wearer flow

1. Open one unified session roster across Hermes, OpenClaw, and configured
   Tailscale Macs.
2. Focus any existing running, waiting, completed, or approval-blocked session,
   or start a new runtime session when that runtime supports it.
3. Read a dense terminal tail and speak ordinary instructions to steer the
   focused session immediately. The tail is **oldest-first, bottom-anchored**
   — "keep the bottom pinned to now", like every terminal — which is what
   the implementation does and what this line used to contradict.
   Steering a session that is ALREADY RUNNING queues one line, newest
   wins, landed when the turn settles: the runtime serializes turns, and
   refusing outright left cancel as the wearer's only mid-run verb.
   `terminal tail` shows what scrolled past here; `terminal history` asks
   the runtime what the session actually contains, which is the only
   useful answer for a session this engine did not itself run.
4. Leave other sessions running while another one is focused.
5. Cancel, resume, or inspect a session through its runtime's direct session
   API. Host names appear only when two rows otherwise cannot be distinguished
   — which means they DO appear when a label repeats: two real sessions can
   share a name, and showing neither made them one session, with
   `terminal resume <label>` silently taking the first. A spoken name that
   still matches more than one names the candidates rather than guessing.

The interaction should feel like a glasses-scale Claude Code or Codex CLI:
speaker/tool/system-prefixed lines, minimal chrome, no cards, and no separate
"Home Brain" page duplicating the terminal.

## Implemented

Engine side, verified by unit tests, by policy tests that need no gateway,
and by `tests/unit/test_work_lane_live.py`, which drives the shipping client
through a real WebSocket server. The CONNECT path is additionally verified
against the live gateway; the operations below are not yet.

| Capability | Where | Spoken as |
|---|---|---|
| Session roster with focus | `services/agent_terminal.py` | `terminal sessions` |
| Switch focus, work continues | same | `switch terminal to <lane>`, or TAP a busy terminal |
| Voice becomes terminal input | same | `terminal <instruction>` |
| Steer a RUNNING session | same (one queued line, newest wins) | `terminal <instruction>` while busy |
| Cancel with honest recovery wording | same | `terminal cancel` |
| Bounded per-lane output tail | same (deque, 200) | `terminal tail`, `terminal output` |
| Read the session's OWN history | `agents/openclaw_terminal.py` | `terminal history`, `terminal inspect` |
| Enumerate the wearer's sessions | `agents/openclaw_terminal.py` | `terminal discover` |
| Resume a listed session | `agents/openclaw_runtime.py` | `terminal resume <label>` |
| Approvals: receive, list, answer | `agents/openclaw_terminal.py` | `terminal approve/deny [N]` |
| Dense terminal view on the lenses | `services/agent_terminal.py` | (repaints on change) |
| Background lane state on the lane strip | same | (repaints on transition) |

Two rules the implementation holds, both load-bearing:

- **Approvals ride a separate policy lane.** `WORK_TERMINAL_POLICY` carries
  `allows_approvals` and `roster`; the Brainstorm lane's no-tools tripwire is
  untouched, and its policy identity includes the flags so a Brainstorm-shaped
  lane with approvals cannot be constructed at all.
- **Resumable targets are the gateway's own listings.** A roster lane may
  address a session outside its namespace only after the gateway named it in
  an `involvingMe` listing on that same connection. The key set is
  per-connection, so a reconnect re-earns authority rather than inheriting it
  — which is the reconnect-without-replay requirement, not a separate feature.

Every gateway capability above was checked against the published protocol
documentation before it was written (`sessions.list` with `involvingMe`;
`session.approval` opted into via `sessions.messages.subscribe`
`includeApprovals`; answered with `approval.resolve {id, kind, decision}`,
first answer wins).

That is necessary and it is not sufficient. The published documentation
lists `"operator"` as a valid `client.mode`; the deployed gateway refuses
it, accepting only node / backend / cli, and rejected every connect attempt
with INVALID_REQUEST until the value was corrected. The docs describe some
version; the deployment is the one that has to accept you. Where a service
is reachable, ask its validator — sending candidate values and reading the
schema errors settled in seconds what the prose had wrong.

## Still outstanding

- **A live-gateway pass, continued.** The connect path is done. The first
  real mismatch arrived EARLIER than this document predicted and not at the
  adapter seam: it was `client.mode` in the connect envelope, before any
  session operation could run. The documented `approve`/`deny` literals are
  still unconfirmed against the deployment, and now reachable to confirm.
- ~~**Its own HUD surface.**~~ DONE — and this bullet said otherwise for a
  release after the split landed, directly contradicting SESSION_MODEL.md's
  account of the same wire; a blind-pair audit had to file the pair as a
  spec conflict (DOC-FRAME-01) because two normative documents disagreed
  about the CURRENT state of the code. The facts: `core/protocol.py`
  defines two frozen frames (`BrainstormHudView`, `AgentTerminalHudView`)
  emitting two wire kinds, and `types.ts` types each payload separately
  (`BrainstormViewPayload` carries no terminal, endpoint, approval or
  tool-policy field, so a cross-surface read no longer compiles). The
  RENDER model behind one shared renderer keeps a `screen` discriminator;
  that is a plugin-internal drawing detail, not the wire.
- **Concurrency beyond the configured lanes.** Lanes are fixed at boot; a
  resumed session replaces a lane's target rather than adding a row. Several
  MACS are configurable as of 2026-09-08 (`{"hosts": [...]}`), each
  contributing its own work lane; what stays fixed is the set of lanes per
  host, not the number of hosts.
- **A2A acceptance remaining.** The task adapter is implemented and real replies have been observed from both peers. Physical G2 delivery, live remote cancellation, provider-wide session discovery, remote approval resolution, and streaming/push support remain unverified or unimplemented; task completion does not establish them.

## Runtime contract required before implementation

Each runtime adapter must provide authenticated, bounded operations for:

- list and identify sessions;
- read bounded history;
- start and stream a turn;
- steer the selected running session;
- cancel/abort with confirmed outcome;
- receive and answer approvals;
- reconnect/recover without replaying stale commands to another host.

An approval alternative means: deny the original request, then steer the same
session with the replacement instruction. It is not an approval of modified
work. Concurrent sessions retain independent lifecycle and output streams.

## Hermes, OpenClaw, and A2A boundary

Hermes and OpenClaw remain independent agent runtimes. Agent Terminal is the
glasses transport/control surface over their direct session APIs. A2A, when
enabled and verified in the future, is an explicit Hermes-to-OpenClaw or
OpenClaw-to-Hermes delegation operation; it is not the terminal transport and
is not inferred from ordinary session speech.

The external A2A services are now reachable; this changes the live prerequisite,
not the existing direct-session terminal contract. Runtime credentials, pairing,
host selection, and Tailscale origin approval remain per-host setup boundaries.

## Future acceptance gates

- ~~Real multi-host roster with host labels only on ambiguity.~~ Done
  2026-09-08. A lane's identity is its (name, host) pair, a name stays bare
  while it is unique, and a spoken name matching two sessions answers with
  both rather than taking the first. Proven by mutation against the shipping
  loader; a second Mac has not yet been stood up to prove it on hardware.
- Two concurrent sessions continue while focus changes.
- History, streaming, steering, cancellation, and approval flows use direct
  runtime receipts rather than inferred UI state.
- No cross-host queued speech, approval, or transcript replay after reconnect.
- Official simulator visual/gesture flow and separate physical-G2 lens/source
  proof.

## Earlier A2A prerequisite verification — 2026-09-11

Historical prerequisite check; the later implementation section supersedes its missing-credential and unimplemented-adapter statements.

User-provided deployment update was verified from this Mac over Tailscale HTTPS:

- **OpenClaw** at `https://home-mac.example-tailnet.ts.net:8443/a2a/v1`: unauthenticated `tasks/get` rejected with HTTP 401. One authenticated `message/send` using the dedicated glasses peer returned HTTP 200, `TASK_STATE_COMPLETED`, and one artifact containing exactly the requested diagnostic response. Elapsed time was 8.46 seconds. The request asked for no tools, file access, or external communications. This proves this submitted task's result, not an attestation that tools are disabled for every A2A task. The peer token was supplied through hidden input, retained only in process memory, and omitted from source and the receipt.
- **Hermes** at `https://home-mac.example-tailnet.ts.net:9900/`: public agent card returned HTTP 200 and unauthenticated `tasks/get` returned HTTP 401. Authenticated Hermes completion is user-reported and has not been independently repeated here: SSH to the Mini on port 22 returned connection refused, so its pre-existing bearer credential could not be retrieved through the supplied route. OpenClaw's peer token was not reused for Hermes.
- Both cards advertise JSONRPC protocol 1.0. OpenClaw advertises no streaming or push notifications; Hermes advertises both. Those advertised capabilities require their own authenticated acceptance tests before the app enables them.
- **Discovery defect:** OpenClaw's public `supportedInterfaces[0].url` advertises `http://127.0.0.1:18789/a2a/v1`. Remote discovery must advertise the public `https://home-mac.example-tailnet.ts.net:8443/a2a/v1` address. The diagnostic explicitly used the supplied public endpoint and did not follow the loopback URL. The card also omits bearer security metadata despite the API requiring it. No remote configuration was modified.

Existing port-443 OpenClaw WebSocket pairing is a separate authenticated connection and remains configured.

### Ordered connection work

1. Correct OpenClaw's advertised public interface and bearer-auth metadata; retain exact endpoint binding in the client and never forward a token to an address taken uncritically from an agent card or redirect.
2. Introduce explicit connection kinds in the Mac app: JOAT host, OpenClaw session Gateway, and A2A task endpoint. Store A2A bearer secrets in the Mac Keychain; configuration and phone/HUD state contain only non-secret profiles and capability/status projections.
3. Implement the A2A task adapter through the existing engine owner: submit once, retain returned task/context identity, normalize result artifacts, poll only known submitted tasks when supported, and handle failed/rejected/cancelled/input-required/auth-required/timeout states. Never retry a possibly submitted message as a fresh task after an uncertain response.
4. Expose an explicit delegated-task action and a real connection test in the Mac interface, with honest progress for non-streaming OpenClaw and capability-gated Hermes streaming. Keep session listing/history/steering/approval controls on their existing direct-runtime transport until separately implemented and verified.
5. Verify unauthenticated rejection, authenticated completion, wrong token, exact destination binding, malformed/error responses, nonterminal tasks, cancellation acknowledgement, reconnect without duplicate sends, and physical HUD delivery. A completed connection-check task does not establish those other capabilities.

The phone's JOAT-host dropdown remains a list of glasses relay candidates; A2A endpoints cannot be inserted as substitute `/glasses` transports.


## A2A implementation and current acceptance — 2026-09-11

The installed signed `/Applications/JOAT.app` now has editable Agent Terminal profiles in Settings and Intelligence. OpenClaw and Hermes have distinct HTTPS endpoints and endpoint-bound Keychain credentials; neither secret is stored in the JSON configuration or projected to the phone. The app-launched engine is the only process authorized to retrieve credentials through the native bridge. Enabled profiles receive a bounded authenticated startup check without submitting work. Save, Reload, enable/disable, remove, and restart controls are wired; saved changes do not claim readiness before restart.

The existing Agent Terminal roster includes `openclaw` and `hermes`. The earlier no-tool Gateway lane is retained as `hermes session`; its policy does not apply to native Hermes A2A. The A2A adapter sends JSON-RPC 2.0 `message/send`, handles actual `status.state` task envelopes, polls only its task, retains context identity, and supplies at most eight prior local turns/12,000 text characters for peers that echo context IDs without remembering previous turns. `terminal history` explicitly describes local conversation history. `terminal recover` checks an uncertain known task without replaying the instruction; reopening/retrying an uncertain lane also performs that owned-task check; `terminal new conversation` resets local context only when no known running/interrupted task remains. Cancellation reports remote success only after a matching canceled task receipt.

Evidence:

- Installed engine and native Keychain bridge: both profiles reported `ready` after authenticated `tasks/get` probes. Unauthenticated requests to both live endpoints returned HTTP 401. Separate test processes were rejected by the native bridge's engine-PID boundary.
- Real production adapter/roster against Tailscale: OpenClaw completed the diagnostic in 9.03 seconds and remembered the preceding phrase on follow-up in 8.54 seconds; Hermes completed both turns in 3.76 seconds each. Each returned a completed task, expected text, preserved context ID, and terminal completion announcement. These were separate diagnostic roster instances using hidden-input credentials; they are not physical HUD delivery receipts.
- The first live OpenClaw follow-up returned the wrong phrase despite a matching context ID and completed task. Bounded local history corrected the behavior, and the identical follow-up was repeated successfully. A state-envelope-only assertion would have missed this defect.
- 283 focused Python tests pass, including real local TLS contract tests for malformed envelopes, authentication, redirects, task/context ownership, input-required continuation, uncertain submission without replay, recovery and cancellation acknowledgements. Five owned Python modules pass type checking. Three Swift A2A model/native-bridge tests pass. These checks do not establish physical-device behavior or live cancellation support.
- Actual installed Settings interaction: editable name field, changed Save enabling, Save persisted a temporary name verified by the production Python loader, Reload reproduced it, and the original name was restored and saved. Restart to Apply was clicked; the prompt cleared and both profiles returned to Connected in the actual UI. No secret was displayed during the interaction.

Remaining acceptance: the private relay has no saved glasses pairing credential and rejected the simulated connection with HTTP 403. No pairing was reset or bypassed. Real plugin/physical-G2 selection, speech, rendered replies, and cancellation must still be exercised after normal pairing. Existing remote session discovery, approvals, forking, and streaming/push are not supplied by this A2A task adapter. The phone's Mac selector continues to choose JOAT relay hosts, not A2A agents.
