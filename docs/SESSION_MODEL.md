# Session model — the shared seams for Phases 1–3

Why this document exists: bolting either Phase 1 (channels) or Phase 2 (debate)
directly onto `VoiceOrchestrator` would let whichever ships first define the
session model unilaterally, forcing a restructure when the other arrives. These
seams are designed against BOTH phases' constraints before either is built.

## The constraint each consumer imposes

| Consumer | What it demands of the seams |
|---|---|
| Voice loop (exists) | utterance → transcript → streamed reply; foreground |
| **Debate (Phase 2)** | wearer-initiated; turns STREAM to the HUD live, one at a time (wearer requirement: visibility IS the proof of debate — never background-only); cancellable; confirm gates; owns the screen while active |
| Channels (Phase 1) | unsolicited events (messages/notifications arrive on their own); triage; screen contention policy (what interrupts what) |
| Computer use (Phase 3) | consequential actions gated by `Confirmer` (exists) |

## The seams

1. **`HudWriter`** (`services/hud.py`) — DONE. The one way Mac code writes to
   the glasses: `status()` / `show()` / `append()`. Streaming (`append`) rides
   the `hud.text` protocol message's `append` flag (mirrored in `types.ts`,
   rendered by the plugin's `HudPresenter.appendText` — no repaint flicker).
   Every consumer streams through this, so surface arbitration later lands in
   ONE place.

2. **`ReplyEngine.stream()`** (`core/interfaces.py`) — DONE. All reply-shaped
   generation yields chunks; engines without true token streaming inherit a
   one-chunk default. The voice reply and each debate turn use the identical
   wearer-visible contract.

3. **`Session` / `SessionManager`** (`services/session.py`) — NEXT, lands with
   `DebateSession` (its first real consumer beyond the voice loop; defining it
   with one consumer would repeat the mistake this doc exists to avoid).
   Sketch held to when both consumers are in hand:
   - `Session`: owns the HUD while active; receives transcripts/inputs;
     `cancel()`; exactly one active at a time (one screen, one wearer).
   - `SessionManager`: routes events — active session first, else the default
     assistant behavior; Phase 1 events become interrupt *requests*, and the
     arbitration policy lives here, not in any session.

## Staging

- Now (this commit): HudWriter + protocol `append` + streamed voice replies —
  the streaming path Phase 2 rides is proven end-to-end on real traffic.
- Phase 2 entry: `Session`/`SessionManager` + `DebateSession` streaming each
  Proposer/Critic/Judge turn through `HudWriter.append`, gated by the Verifier.
- Phase 1 entry: channel events plug into the SAME manager as interrupt
  requests; no orchestrator surgery.
