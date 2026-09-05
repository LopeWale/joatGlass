# Conversational feature repair: next slice

Status: reviewed design, not implemented or accepted. Existing first-wave
verification owns a frozen source candidate. Begin these source edits only
after that run finishes and the coordinator claim is expanded.

## Requested behavior

Ordinary addressed conversation must reach the same feature controllers used
by the UI. A direct question from the app must accept the subsequent answer
without requiring another wake phrase. Both paths must preserve the selected
target, truthful results, cancellation and explicit message confirmation.

## Architecture

Register a small typed feature adapter in the existing ToolBelt after
controller composition in `__main__.py`. Reuse the same registry through the
existing subscription CLI MCP endpoint. Do not generate command strings or
introduce another intent model. Validate enum values and opaque references
in application code, independently of model schemas.

| Operation | Existing owner and boundary |
|---|---|
| open_feature | CapabilityRouter with a finite feature enum and request identity |
| read_messages | IMessageStore paging plus a public router read facade; available full text, continuation, server-issued conversation references |
| prepare_message | MessageReplyController typed compose/reply methods; existing target validation, review and confirmation |
| message_action_status | Controller-owned pending/completed action and existing journal receipt |
| create_quicknote | NoteCommandController.save_text and QuickNotes refresh; preserve exact supplied text and prevent duplicate delivery |
| control_timer | PomodoroService start/pause/resume/skip/reset/status; existing Pomodoro semantics |
| start_brainstorm | SessionManager typed entry; preserve single active session and replacement clarification |
| terminal_session | AgentTerminalRoster references and authority; existing focus/history/send/cancel operations |
| start_navigation | Typed destination factory and existing destination/mode/origin/route flow; preserve confirmation and location checks |

Message preparation must return a pending action promptly. The existing
ToolBelt deadline is ten seconds, while message confirmation allows twelve
seconds and the send subprocess currently has no application deadline.
Wrapping confirmation/send directly in a normal tool can cancel execution
across the external side effect. A controller-owned single-flight task must
own confirmation, send and receipt. A repeated action reference must not
duplicate the send; uncertain sends must not be retried automatically.

Contacts currently resolve names and handles, not family relationships.
"My wife" therefore requires a contact clarification. Do not infer a spouse
from names, message frequency or recency. Multiple candidate addresses also
require selection. Persisted relationship aliases are separate behavior.

## Prompt ownership

Use one scoped prompted-speech dispatcher before the ordinary wake gate and
active-session catch-all. Bind each prompt to its owner, target/session and
connection generation, microphone source, expiry, one-use consumption and
the audio boundary when the question was issued. Reject audio captured
before that boundary, including queued ambient utterances. Reject mixed or
wrong-source audio. Back, cancel, target change, expiry and disconnect revoke
ownership. Deliver accepted text directly to the typed owner, without
re-entering general command parsing.

The existing Brainstorm glasses-only policy is the starting constraint.
Messages and Terminal currently have armed state after the wake gate. Active
Brainstorm also consumes addressed requests before shared assistant routing.
Explicitly addressed cross-feature requests must reach that shared decision
path while preserving the discussion. Moving every deterministic command
ahead of a session would incorrectly reinterpret contextual words such as
"pause" and is not an adequate repair.

## Required acceptance journeys

Each journey requires controller operation/result evidence. Relevant display
and input changes also require official-simulator interaction. Model prose
alone is not evidence of an executed operation. Message send adapters use
controlled fixtures; no real recipient is contacted.

1. "JOAT, could you bring up my messages?" opens Messages despite not matching
   the current exact opener grammar.
2. "Read the latest message from Dele in full" resolves a real candidate and
   returns available text with continuation, not an eighty-character summary.
3. "Send a message to my wife", a contact clarification and "I'll be home at
   six" produce a reviewed draft for the selected contact.
4. "Tell Alex I'm running late" with two candidates sends nothing until the
   target is selected and the action confirmed.
5. "Could you tell her I'm on my way?" uses the selected conversation;
   refusing confirmation produces zero sends.
6. After "Say the reply", "Good morning" without JOAT becomes the body.
   The same unaddressed speech after Back, expiry or disconnect is ignored.
7. "Keep this as a quick note: the spare key is with Sam" stores the exact
   body and refreshes the view. Duplicate tool delivery creates one note.
8. "Give me a focus session", then "Could you pause that timer?" change
   actual timer state and report its resulting status.
9. During Brainstorm, "JOAT, open Agent Terminal", session selection and a
   prompted "Show the failing tests" reach the correct Terminal target while
   preserving the discussion.
10. "JOAT, help me walk to the library" enters the existing destination/mode
    flow. Prompted choices work, stale/unavailable location stays truthful,
    and pre-prompt queued speech is rejected.

## Official contracts and evidence limits

The tool execution and result-feedback boundary follows [OpenAI function
calling](https://developers.openai.com/api/docs/guides/function-calling) and
[Anthropic tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview),
verified by the independent source reviewer on September 4, 2026. These
contracts do not prove JOAT's implementation works or require replacing the
existing subscription CLI engines with metered API access.

Display, input and lifecycle verification must also honor the official Even
contracts collected in [REPAIR_SOURCES.md](REPAIR_SOURCES.md). Simulator
acceptance remains distinct from physical glasses, microphone recognition,
live recipient data and end-to-end assistant latency.

## Execution constraints

Root owns process execution and retained evidence. Agents receive disjoint
controller/adapter/review assignments after claim expansion. Existing voice
recovery edits remain preserved and require integration review, not reset.

ZzzOps installation is current, but its checkpoint reports `policy:missing`
and no initialized repository policy. No ZzzOps-managed workflow is claimed.
Continue repairs with the established Coordinator, CPJ and Anti-Churn
workflow; plugin initialization is not a prerequisite for these authorized
repairs.
