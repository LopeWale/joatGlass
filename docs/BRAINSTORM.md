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

The 576x288 optical surface keeps the required JOAT status spine, then a
flat title/state row, a hairline divider, the transcript, and a truthful
gesture footer. Session rows use a selection rail, not cards.

There is no TOPIC/SEATS/LIVE block and no outer frame. Both were removed on
the wearer's ruling — "stop wasting so much place on things that does not
affect functionality" — after they measured the waste by eye: the content
column ran x=36..540 of a 576px lens, and the longest transcript row ended
at x=362. Header rows that restate what the spine already says, and a
decorative border, are exactly the chrome this surface cannot afford.

The transcript is **13 rows** on Brainstorm and **12** on a running Agent
Terminal (`agentTerminalTranscriptLayout`), drawn from y=82 at 14px
spacing. Each row holds roughly **105 characters**: HudCanvasRenderer sets
it in `HUD_TEXT_FONT` — `"Helvetica Neue", "SF Pro Text", Arial,
sans-serif` — at 11px across `HUD_CONTENT_WIDTH` (548px, x=14 to x=562).
The advance is MEASURED, not assumed: a 63-character row rendered by the
real simulator spans 326px, so 5.18px per character.

That column used to be 504px (x=36 to x=540) in a monospace face at 74
columns, and this paragraph said so for a release after the face changed —
which is what the wearer saw: "why are some words being cut off when there
is obviously room on the right". Both numbers move together. If the font
or the column changes again, **this paragraph is wrong until it is
re-derived**, and `BRAINSTORM_ROW_COLUMNS` in `core/protocol.py` carries
the derivation next to the constant.

Proportional type has no true column count — 105 is an average. The
renderer measures each row with the canvas and wraps on the measurement
(`brainstormTranscriptRowCount`); the constant only sizes the wire budget
(`BRAINSTORM_ACTIVITY_MAX`), which carries 40 rows of scrollback, not one
screenful. A test re-derives that equality from the renderer so the two
cannot drift apart.

Those rows hold about one 120-word turn, so a FINISHED turn collapses to
the one-line `GIST:` its speaker is instructed to open with, and only the
live turn keeps its prose. The synthesis is never collapsed. Without that
the glass showed a single turn and the wearer could not follow who had
said what — which is the whole point of watching.

The speaker tag is drawn dim and the words bright. One brightness for
everything made a screen of discussion a flat wall with no way in, on the
surface whose entire job is being read.

No card borders, boxed panels, oversized topic title, permanent collaborator
row, decorative empty space, or fake waveform is permitted. A recording dot is
drawn only while the capture pipeline reports active capture.

## Evidence boundary

Simulator and physical-G2 acceptance evidence may retain fixed state names,
bounded gesture metadata, timestamps, and SHA-256 frame hashes. It must not
retain a Brainstorm transcript, recognized wearer content, Codex/Claude output,
or provider payload.
