# Audit disposition, 2026-09-05

Source of the findings: `docs/audits/2026-09-04-feature-audit/AUDIT.md` at
commit `f1e4ada` (the directory was deleted by `8bb5cc1`; read it with
`git show f1e4ada:docs/audits/2026-09-04-feature-audit/AUDIT.md`).

Evidence classes used here:

- **Executed** — the production class or the real CLI seat was driven by a
  probe script in this session and the behaviour was observed.
- **Source** — the defect's mechanism was read and changed; no execution.
- **Verified present** — the earlier repair was confirmed in the current tree.
- **Open** — not repaired in this session, with the reason.

Probe scripts live in the session scratchpad and are reproducible:
`a1_probe.py` (real Claude/Codex seats over the real MCP endpoint, recording
controllers), `verify_a3_a5_a8.py` (orchestrator interrupt, WhisperKit
deadline, Even AI door history and streaming).

| ID | Disposition | What changed / what was observed |
|---|---|---|
| A1 | Executed | Persona names every feature action and requires verbatim wearer text; tools added: `read_conversation`, `read_news_article`, `read_quicknotes`, `terminal_sessions`, `start_navigation`. 14/14 journeys reach the correct controller on both seats (was 8/11 Claude, 6/11 Codex). |
| A2 | Verified present | Prompted speech for Messages (typed and UI entry), Brainstorm topic and Terminal instruction dispatches before the wake gate (`orchestrator._act` → `PromptedInputDispatcher.dispatch`). Not re-executed on glasses here. |
| A3 | Executed | Queue of three completed utterances; next utterance transcribed while the reply streams; addressed speech or a stop word that ends after the answer began cancels it. A second request spoken before any answer existed is answered in order. |
| A4 | Verified present | Codex seat yields at `turn.completed` before process EOF (`cli_reply_engine.py`). |
| A5 | Executed | `WhisperKitEngine(inference_deadline_s=90)`; batch fallback inherits it. Hung fake binary returned at the 1 s probe deadline; the real binary has no grandchild holding the pipe. |
| A6 | Source | Stages measured from VAD close (queue wait visible); `reply_complete` for command answers, prompted dispatch and navigation opener; `interrupted` stage. Still open: correlating the plugin's `tile_upload` durationMs into the receipt. |
| A7 | Source | Worker `SeatSwitch` (own CLI sessions, `WORKER_PERSONA`, local task engine) for judge, synthesis and `joat_transform`; judge label names the seat. |
| A8 | Executed | Door seats with `session_ttl_s=0`; the request's prior turns become the prompt (bounded 8 turns / 2,400 chars); SSE streamed chunk by chunk, close-delimited, markup stripped per chunk, 600-char cap across chunks. |
| M1 | Verified present | `IMessageStore._connect` uses `mode=ro`, not `immutable=1`. |
| M2 | Verified present | Mac footer carries `up newer` / `down older`; plugin gate reads them. |
| M3 | Verified present | Back keeps the row mapping and disarms reply ownership (`capabilities.py` ~575). |
| M4 | Verified present | Groups open for reading with participant labels; reply target never a participant. |
| M5 | Source | `attributedBody` decoding present. Picker now shows 10 conversations (store ceiling); the custom list windows around the cursor so it scrolls. No conversation-list pager beyond that. |
| M6 | Source (partial) | Relationship phrases ("my wife") produce a controller question, which the mind relays; the mind is told to call the tool rather than ask first. A persisted relationship book is not implemented. |
| M7 | Open | Draft review is the confirmation prompt; recipient plus body must fit 120 UTF-16 units (`CONFIRM_PROMPT_MAX_UTF16_UNITS`); longer drafts are still rejected for redictation. Needs a reader-paged review before the confirmation. |
| M8 | Executed | `read_conversation` gives whole messages; `get_recent_messages` snippet widened to 160 chars and points at it. Sender subprocess already bounded (15 s). Reply match order unchanged (typed reply entry precedes the broad regex). |
| M9 | Verified present | Prepared rows (62 columns × 10 rows, exact source spans) replace character pages. |
| M10 | Verified present | `IMessageStore` constructed unconditionally; router retries the same store. |
| Q1 | Verified present | Back asks the Mac to keep the capture and re-renders the current controls. |
| Q2 | Verified present | Exact reader pages replace the 64-char list item. |
| Q3 | Source | `read_quicknotes` tool (text notes + recording transcripts); glasses list shows 8 + 8; "note …" refreshes an open list. Voice-note detail already includes the full transcript segments in the reader. |
| B1 | Verified present | A background Brainstorm returns speech to the normal path (`background_brainstorm` guard). |
| B2 | Verified present | Terminal handoff opens the real picker via `open_terminal_from_brainstorm`. |
| B3 | Source (partial) | Synthesis is followed by a VERIFY line: its own citations are checked, otherwise it states how many turns had sources checked (possibly none). Saved-session selection still continues rather than showing a read-only transcript. |
| T1 | Source | `ApprovalReceipt` carries the recorded verdict as data; the glasses handler no longer compares a sentence with a word. |
| T2 | Source (partial) | `terminal_sessions` tool (roster + lane tail). Multi-host discovery and full-output paging on the lens remain roadmap items. |
| O1 | Source | Unread = Messages.app `is_read = 0` incoming count, refreshed every 20 s off the loop thread. |
| O2 | Other session | Full notification bodies (`Card.body`, reader rows) are in the concurrent typography session's working tree. |
| O3 | Source | Brainstorm/Terminal tap accepts left temple, right temple and ring (Even's input reference gives both temples the same gesture set; only left double is Back). |
| N1 | Open | `refresh_once` keeps stale headlines on total failure without touching `fetched_at`, but the plugin's receipt clock still advances on every push and partial failures are silent. Needs a wire field; deferred until the concurrent session's protocol changes land. |
| N2 | Executed (assistant side) | `read_news_article` opens the same article the tab opens. RSS description fallback when extraction fails is not added. |
| N3 | Verified moot | News and Search render on the custom canvas again; the native list rebuild path the audit exercised is no longer the News path. |
| P1-TIMER | Verified present | `PomodoroStore` checkpoint + `activate_restored()` at boot; `control_timer` tool. |
| NAV1 | Executed | `start_navigation` tool reaches the same session factory as "navigate to"; "open navigation/directions/map" spoken opener. Provider/location/physical route remain unverified. |
| Reconnection | Verified present | Code 4409 replacement protocol in the gateway and MacLink. |
| OpenClaw/Hermes | Open | Needs the live gateway; not exercised here. |
| Background takeover | Verified present | `HudSink` paints only while `_view_is_current()`. |
| Navigation ownership | Open (plugin) | Local Home/menu transitions that do not send `capability-lifecycle` events are in the plugin's territory, currently being edited by the concurrent session. |
| QuickNotes refresh | Source | `note …` and `create_quicknote` both re-open QuickNotes. |
| Documentation | Source (partial) | MAC_APP.md no longer requires a Hermes seat for Brainstorm. Typography and navigation document conflicts are in the concurrent session's scope. |
