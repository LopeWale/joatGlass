# Brainstorm

Brainstorm is JOAT's dense, voice-first analysis conversation on the glasses.
It is not a dashboard, a one-question form, a Debate panel, or an Agent
Terminal wrapper.

## Product definition

> A wearer-led thinking partner on the glasses. The wearer asks, and each
> collaborator answers in full: a question broken down in detail, or a plan
> worked step by step. After every round the room stops and waits; the
> wearer reads, and speaks the next move when ready. Sources are checked and
> shown beside each answer, never used to rewrite it.

The wearer leads. Nothing in a Brainstorm carries on, agrees, scores or
summarises on the wearer's behalf: a round is every seat going once, and the
next one starts when the wearer says so.

## Collaborators and authority

JOAT is the glasses brain and the only wearer-facing controller. It receives
voice from the G2, owns session memory and lineage, interprets start/continue/
branch/stop controls, chooses the next collaborator according to the wearer's
direction and the recorded turn policy, streams work to the HUD, and checks the
sources each answer cites. It does not judge the collaborators, vote, or
write a summary of its own: the wearer reads what each of them said and
decides what happens next. Codex and Claude operate
inside this JOAT-owned conversation; neither is the interface or session owner.

- `codex exec -s read-only` uses the user's Codex subscription and cannot write
  the workspace.
- `claude -p` uses the user's Claude Code subscription.
- JOAT drops one shared list (`agents/cli_env.py` `STRIP_ENV`) before every
  launch of those CLIs, so an ambient credential cannot silently change the
  billing/auth boundary. It covers API keys, bearer tokens, base URLs,
  cloud-provider switches, Anthropic profiles and `CODEX_API_KEY`, which
  `codex exec` takes over the ChatGPT login.
- Hermes and OpenClaw do not participate and do not gate availability.
- Neither Codex nor Claude is permanently first. The wearer may direct either
  one to speak next; otherwise the session continues its recorded turn policy.

## Round structure

A round is one turn from every seated collaborator — an invariant, not a
default: a mid-round voice redirect reorders the seats still owing a turn and
can never make one speak twice. The room runs ONE round per wearer move, then
stops:

1. **Independent** — round 1 withholds the transcript, so each seat answers
   blind. Two uncontaminated views are worth more than one view and an echo.
2. **Cross-review** — a later round: every seat sees everything said,
   including the wearer's move, and answers the latest move before anything
   else.
3. **Waiting** — the room stops and the glasses put the whole conversation
   up to read (see "The room" below). Nothing carries it on: no judge scores
   agreement, no round limit runs out, no synthesis closes the discussion, and
   no idle timer starts another round. The wearer's spoken words are the next
   move — a new question, "now give me a step by step plan", a stage
   direction ("tear that apart") or a nomination ("ask Codex if that's
   true"). They need no wake word, because the room asked for them; only the
   noise words a transcriber writes for silence ("you", "thanks", "um") are
   not a move. The way out is the cancel words, or Back.

The room also ends by itself when fewer than two collaborators gave a
position ("insufficient collaborators"): a conversation needs two sides.

Each seat is asked for its answer in full and in detail. A question is
broken down — what it depends on, the options, the trade-offs, an example; a
request for a plan is worked as numbered steps with their order, what each
needs and where it can go wrong. There is no length cap on a turn, and
nothing on the glasses collapses a finished turn to a gist.

A seat has ten minutes to answer (`SEAT_TURN_TIMEOUT_S`). The ceiling is for
a seat that has stalled, not a pace: the wearer watches the words arrive. A
seat that runs out of time shows "ran out of time"; one that is signed out or
over its usage limit is named as such, with where to fix it.

A nomination may carry the question it is asking — "ask Codex if that's
true" hands over the floor AND records the question, so the seat is
prompted with it rather than answering something nobody asked.

While a round runs, only the cancel words are heard without the wake word.
Steering then requires the wearer to address JOAT, so an overheard sentence
can neither reorder the round table nor reach a collaborator; an addressed
remark made mid-round counts as the wearer's move, and the next round starts
on it. While the room waits, the wearer's unaddressed speech is the move —
but only while Brainstorm is the view in front. With another feature in
front, speech goes to JOAT as usual.

### Source check

As each answer finishes, JOAT looks up the sources it cited. The result is
shown beside the answer as `VERIFY` rows: each source OK or FAIL, with how
much of the quote was found. It is a display, not a gate: a failed source is
never sent back to the seat, and nothing is rewritten, shortened or withheld.
The wearer sees the answer and the check, and decides how far to trust it.

Two structures with opposite jobs hold a session, and confusing them invents a
defect: the DURABLE RECORD (built from the sink stream) keeps every turn,
including a failed one, because that is the story the wearer opens later; the
PROMPT CONTEXT (`DebateSession.transcript()`) carries only each speaker's
final text, because re-feeding the room a seat's provider error would be a
defect of its own. A collaborator that fails is shown and then excluded from
the transcript — a provider error is never quoted as a position.

## Conversation behavior

The wearer can start, continue, steer, branch, stop, and later resume a
Brainstorm without touching the Mac. Follow-ups inherit the selected context
by default. Explicit new/branch/continue commands change lineage. Ambiguous or
clearly unrelated replacement requests produce a confirmation question rather
than a silent reset.

Every actual turn is visible with a `YOU`, `CODEX`, `CLAUDE`, `VERIFY`, or
`JOAT` prefix. The order is the order that occurred, not a fixed provider
sequence. A source that fails the check stays visibly marked FAIL; JOAT never
presents a missing citation or a provider failure as verified.

## The room

When a round ends the glasses open `BRAINSTORM · ROOM`: the whole
conversation, every turn in the order it happened, nothing collapsed or cut.
It is the glasses' own scroll text box — the same one a News article uses, a
line a swipe with the firmware's scrollbar — and the footer reads
`swipe: scroll · speak: your move · 2x tap: back`. A tap does nothing; the
move is spoken. When the wearer's move starts the next round the glasses
return to the live view, and when that round ends the room reopens on the
wearer's move and the new answers, not where they had stopped reading the
round before. Back leaves the room to the overview and the session carries on
in the background.

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
(`brainstormTranscriptRowCount`); the constant sizes the Agent Terminal's
wire budget (`BRAINSTORM_ACTIVITY_MAX`), which carries 40 rows of
scrollback, not one screenful. A test re-derives that equality from the
renderer so the two cannot drift apart.

A Brainstorm turn is whole. A finished turn is never collapsed to a
one-line gist: the live view follows the newest words while a round runs,
and the conversation that rides the frame is bounded only by
`BRAINSTORM_TRANSCRIPT_MAX` (64 KB — several rounds of full answers; the
oldest rows drop first from the live frame, while the whole conversation is
kept on the Mac and in the saved record). When the round ends, the room
reader (above) carries the conversation whole. An earlier release cut each
answer to about 120 words and a one-line gist so that two turns fitted the
screen; the wearer could not read what the collaborators had said, so the
cap and the collapse were removed rather than tuned.

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
