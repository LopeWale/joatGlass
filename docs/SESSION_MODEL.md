# Brainstorm session model

Brainstorm is a durable conversation, not a transient dashboard panel and not
an Agent Terminal session.

## Product definition

> A wearer-initiated, steerable, potentially multi-round analysis session
> around a topic, task, claim, problem, or evolving discussion. Collaborators
> challenge and expand each other's work, repair unsupported claims, and
> converge on a verified synthesis while the process remains visible on the
> glasses.

Only Codex CLI and Claude Code participate. JOAT owns verification, synthesis,
session selection, and the glasses interaction. Hermes and OpenClaw belong to
Agent Terminal and are never Brainstorm seats or readiness dependencies.

JOAT is the glasses brain. It receives the wearer's voice, owns durable
conversation identity and branch lineage, decides which bounded action the
utterance represents, routes content to the selected collaborator, streams the
actual turn to the HUD, runs verification and repair, records the synthesis,
and restores the visible session after reconnect. Codex and Claude are
collaborators invoked by JOAT; they do not directly own the microphone, HUD,
session state, or navigation between Brainstorms.

## Durable record

Each local record contains:

- opaque local session ID;
- title and creation/update time;
- root and parent branch IDs;
- ordered `YOU`, `CODEX`, `CLAUDE`, `VERIFY`, and `JOAT` turns;
- verification state and latest synthesis;
- `active`, `settled`, or `cancelled` status.

The selected JOAT Mac retains full conversation content in its private local
store. Diagnostic and acceptance receipts retain no transcripts or provider
payloads.

## Invocation and context

| Input | Result |
|---|---|
| `JOAT, brainstorm <topic>` while no context is selected | Create and open a new session |
| Open Brainstorm during an active run | Restore the current conversation |
| Open Brainstorm while idle | Show recent sessions plus `NEW` |
| Ordinary speech in a selected Brainstorm | Append the next user move to that context |
| `continue <title>` | Resume the matching durable session; ask on ambiguity |
| Tap a row in the idle picker | Open that session by id — no title matching |
| `new Brainstorm` | Start an explicit new lineage — **not implemented** |
| `branch this` | Fork the live transcript into a child lineage; the parent yields the slot only once the fork is built |
| A clearly unrelated replacement request | Ask before replacing context; never guess or silently reset |
| A new topic spoken during a live run | Held, and the wearer is asked — yes cancels and starts it, no keeps going |
| `stop` / `cancel` | Cancel the active run and persist the cancelled state |

An INSTRUCTION about the last reply is executed rather than absorbed:
"send that to Hermes", "save that", "make that shorter", "turn that into a
spec". It must open with an imperative AND name the material, so a remark
that merely contains a verb stays a `YOU` turn — being wrong in that
direction costs a line in the transcript, being wrong the other way
silently eats the wearer's contribution. These checks run BEFORE the
session absorbs the utterance; that order is load-bearing, because
`DebateSession.consume_transcript` returns True for essentially any
addressed text and would otherwise swallow every instruction.

While Codex or Claude is responding, other contextual speech queues as the
next user move. Participant-directed speech chooses which collaborator speaks next; it
does not open Agent Terminal. Neither Codex nor Claude has a permanent first
slot. Without an explicit direction, the session continues its recorded turn
policy and the HUD shows the actual order. A stopped or failed collaborator
remains visibly marked and is excluded from later factual context.

## Visible state sequence

```text
YOU -> wearer-directed or session-selected CODEX / CLAUDE turns
    -> VERIFY / repair -> JOAT synthesis
```

The invariant is visibility of the actual turn order, not a fixed provider
order. Follow-up rounds may start with either collaborator.

The G2 model carries only the current bounded viewport: Brainstorm
view/state, session title/ID, the speaker-prefixed transcript rows,
recent-session rows, selection/scroll metadata, and the real voice-composer
state. It contains no Agent Terminal, Hermes, OpenClaw, endpoint, approval,
or tool-policy fields.

That last sentence was violated for the whole life of the feature:
`BrainstormHudView` carried `terminal_state`, `endpoint_host`,
`terminal_reason` and `no_tools_checked`, plus a `screen` discriminator
that made both surfaces literally one frame. It is why the Agent Terminal
printed "JOAT · BRAINSTORM" across its spine, why a healthy terminal spent
four rows on connection chrome, and why a tap on the terminal was gated on
the BRAINSTORM lane's readiness. There are two frames now
(`BrainstormHudView`, `AgentTerminalHudView`) and two wire kinds, so the
absence is structural rather than a promise.

The row count is a RENDERER fact, not a wire one: 13 on Brainstorm and 12
on a running terminal, derived in docs/BRAINSTORM.md from the drawn font
and column. The wire carries 40 rows of scrollback and the renderer
windows it.

## HUD ownership and other events

Exactly one live `Session` owns the Brainstorm surface. Incoming notifications
queue behind it and drain after the run. A reconnect replays the current
semantic Brainstorm frame from durable state; it does not invent a new session.
The official simulator can prove rendered transitions, taps, and swipes. Only a
physical G2 can prove left/right/ring source and wearer-visible lens behavior.
