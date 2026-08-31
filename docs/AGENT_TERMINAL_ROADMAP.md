# Agent Terminal roadmap

Agent Terminal is a future dense CLI/chat interface for controlling existing
Hermes and OpenClaw work from the glasses. It is separate from Brainstorm and
never uses Codex CLI or Claude Code.

This document is a roadmap only. The Brainstorm documentation update does not
implement Agent Terminal, alter Hermes/OpenClaw configuration, or enable A2A
services.

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
