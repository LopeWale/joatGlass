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
| `new Brainstorm` | Start an explicit new lineage |
| `branch this` | Fork the selected transcript into a new child lineage |
| A clearly unrelated replacement request | Ask before replacing context; never guess or silently reset |
| `stop` / `cancel` | Cancel the active run and persist the cancelled state |

While Codex or Claude is responding, contextual speech queues as the next user
move. Participant-directed speech chooses which collaborator speaks next; it
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

The G2 model carries only the current bounded viewport: Brainstorm view/state,
session title/ID, up to twelve speaker-prefixed lines, recent-session rows,
selection/scroll metadata, and the real voice-composer state. It contains no
Agent Terminal, Hermes, OpenClaw, endpoint, approval, or tool-policy fields.

## HUD ownership and other events

Exactly one live `Session` owns the Brainstorm surface. Incoming notifications
queue behind it and drain after the run. A reconnect replays the current
semantic Brainstorm frame from durable state; it does not invent a new session.
The official simulator can prove rendered transitions, taps, and swipes. Only a
physical G2 can prove left/right/ring source and wearer-visible lens behavior.
