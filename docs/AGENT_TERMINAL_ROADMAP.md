# Agent Terminal roadmap

Agent Terminal is a dense CLI/chat interface for controlling existing Hermes
and OpenClaw work from the glasses. It is separate from Brainstorm and
never uses Codex CLI or Claude Code.

Status: the wearer-facing control surface and its gateway policy lane are
now implemented; A2A remains untouched and unenabled. See "Implemented" and
"Still outstanding" below — the acceptance gates at the end are unchanged and
none of them has been met, because none can be met without a live gateway.

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
3. Read a dense newest-first terminal tail and speak ordinary instructions to
   steer the focused session immediately.
4. Leave other sessions running while another one is focused.
5. Cancel, resume, or inspect a session through its runtime's direct session
   API. Host names appear only when two rows otherwise cannot be distinguished.

The interaction should feel like a glasses-scale Claude Code or Codex CLI:
speaker/tool/system-prefixed lines, minimal chrome, no cards, and no separate
"Home Brain" page duplicating the terminal.

## Implemented

Engine side, verified by unit tests and by policy tests that run without a
gateway. None of it has been exercised against a live Hermes/OpenClaw.

| Capability | Where | Spoken as |
|---|---|---|
| Session roster with focus | `services/agent_terminal.py` | `terminal sessions` |
| Switch focus, work continues | same | `switch terminal to <lane>` |
| Voice becomes terminal input | same | `terminal <instruction>` |
| Cancel with honest recovery wording | same | `terminal cancel` |
| Bounded per-lane output tail | same (deque, 200) | `terminal tail` |
| Enumerate the wearer's sessions | `agents/openclaw_terminal.py` | `terminal discover` |
| Resume a listed session | `agents/openclaw_runtime.py` | `terminal resume <label>` |
| Approvals: receive, list, answer | `agents/openclaw_terminal.py` | `terminal approve/deny [N]` |
| Dense terminal view on the lenses | `services/agent_terminal.py` | (repaints on change) |

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

## Still outstanding

- **A live-gateway pass.** Nothing here has spoken to a real Hermes/OpenClaw.
  The first run will settle whether the documented `approve`/`deny` literals
  match the deployment, and it will surface at the adapter seam.
- **Its own HUD surface.** The terminal still renders as a `screen` variant of
  the Brainstorm frame — one model, one controller, one wire kind. Splitting it
  touches `types.ts` and `core/protocol.py` in the same commit.
- **Concurrency beyond the configured lanes.** Lanes are fixed at boot; a
  resumed session replaces a lane's target rather than adding a row.
- **A2A.** Unchanged, unenabled, and still not the terminal transport.

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

The currently inactive/unverified A2A services are a future live prerequisite,
not something this Brainstorm slice configures. Runtime credentials, pairing,
host selection, and Tailscale origin approval remain per-host setup boundaries.

## Future acceptance gates

- Real multi-host roster with host labels only on ambiguity.
- Two concurrent sessions continue while focus changes.
- History, streaming, steering, cancellation, and approval flows use direct
  runtime receipts rather than inferred UI state.
- No cross-host queued speech, approval, or transcript replay after reconnect.
- Official simulator visual/gesture flow and separate physical-G2 lens/source
  proof.
