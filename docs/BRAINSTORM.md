# Brainstorm

Brainstorm is JOAT's dense, voice-first analysis conversation on the glasses.
It is not a dashboard, a one-question form, a Debate panel, or an Agent
Terminal wrapper.

## Product definition

> A wearer-initiated, steerable, potentially multi-round analysis session
> around a topic, task, claim, problem, or evolving discussion. Collaborators
> challenge and expand each other's work, repair unsupported claims, and
> converge on a verified synthesis while the process remains visible on the
> glasses.

## Collaborators and authority

JOAT is the glasses brain and the only wearer-facing controller. It receives
voice from the G2, owns session memory and lineage, interprets start/continue/
branch/stop controls, chooses the next collaborator according to the wearer's
direction and the recorded turn policy, streams work to the HUD, verifies or
repairs claims, and produces the visible synthesis. Codex and Claude operate
inside this JOAT-owned conversation; neither is the interface or session owner.

- `codex exec -s read-only` uses the user's Codex subscription and cannot write
  the workspace.
- `claude -p` uses the user's Claude Code subscription.
- JOAT strips `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` before launching those
  CLIs so an ambient API key cannot silently change the billing/auth boundary.
- Hermes and OpenClaw do not participate and do not gate availability.
- Neither Codex nor Claude is permanently first. The wearer may direct either
  one to speak next; otherwise the session continues its recorded turn policy.

## Round structure

A round is one turn from every seated collaborator — an invariant, not a
default: a mid-round voice redirect reorders the seats still owing a turn and
can never make one speak twice. The starter design runs:

1. **Independent** — round 1 withholds the transcript, so each seat answers
   blind. Two uncontaminated views are worth more than one view and an echo.
2. **Cross-review** — round 2 onward, every seat sees everything said.
3. **Meta-review** — after each round the local judge scores agreement and
   blocking objections; the session converges above threshold, otherwise the
   room goes around again up to the configured maximum. An illegible judgment
   fails closed and never green-lights convergence.
4. **Synthesis** — JOAT streams the verified result: `AGREED` asserts only
   claims the verifier passed or that need no citation, `OPEN` names the
   unresolved disagreements and who holds them. It runs even when the judge
   could not score, and never runs on fewer than two positions.

Unverified claims return to the SAME collaborator for repair, streamed under
its own header so a turn is never silently rewritten. A collaborator that
fails is shown and then excluded from the transcript — a provider error is
never quoted as a position.

While a session runs, only the cancel words are heard without the wake word.
Steering requires the wearer to address JOAT, so an overheard sentence can
neither reorder the round table nor reach a collaborator.

## Conversation behavior

The wearer can start, continue, steer, branch, stop, and later resume a
Brainstorm without touching the Mac. Follow-ups inherit the selected context
by default. Explicit new/branch/continue commands change lineage. Ambiguous or
clearly unrelated replacement requests produce a confirmation question rather
than a silent reset.

Every actual turn is visible with a `YOU`, `CODEX`, `CLAUDE`, `VERIFY`, or
`JOAT` prefix. The order is the order that occurred, not a fixed provider
sequence. Unsupported claims remain visibly uncertain until repaired; JOAT
never turns a missing citation or provider failure into a verified synthesis.

## G2 layout

The 576x288 optical surface keeps the required JOAT status spine, then uses a
flat title/state row, approximately eleven transcript rows, a compact voice
composer, and a truthful gesture footer. Transcript continuation lines align
under their speaker text. Session rows use a selection rail, not cards.

No card borders, boxed panels, oversized topic title, permanent collaborator
row, decorative empty space, or fake waveform is permitted. A recording dot is
drawn only while the capture pipeline reports active capture.

## Evidence boundary

Simulator and physical-G2 acceptance evidence may retain fixed state names,
bounded gesture metadata, timestamps, and SHA-256 frame hashes. It must not
retain a Brainstorm transcript, recognized wearer content, Codex/Claude output,
or provider payload.
