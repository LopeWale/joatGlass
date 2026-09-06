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
| A6 | Source | Stages measured from VAD close (queue wait visible); `reply_complete` for command answers, prompted dispatch and navigation opener; `interrupted` stage; `painted` stage from the plugin's first successful `tile_upload` trace after `first_reply_sent` (gateway `on_trace`, diagnostics-only like the receipt). |
| A7 | Source | Worker `SeatSwitch` (own CLI sessions, `WORKER_PERSONA`, local task engine) for judge, synthesis and `joat_transform`; judge label names the seat. |
| A8 | Executed | Door seats with `session_ttl_s=0`; the request's prior turns become the prompt (bounded 8 turns / 2,400 chars); SSE streamed chunk by chunk, close-delimited, markup stripped per chunk, 600-char cap across chunks. |
| M1 | Verified present | `IMessageStore._connect` uses `mode=ro`, not `immutable=1`. |
| M2 | Verified present | Mac footer carries `up newer` / `down older`; plugin gate reads them. |
| M3 | Verified present | Back keeps the row mapping and disarms reply ownership (`capabilities.py` ~575). |
| M4 | Verified present | Groups open for reading with participant labels; reply target never a participant. |
| M5 | Source | `attributedBody` decoding present. Picker now shows 10 conversations (store ceiling); the custom list windows around the cursor so it scrolls. No conversation-list pager beyond that. |
| M6 | Executed | `RelationshipBook` (owner-only JSON under Application Support, names only) written solely by the wearer's own statement through `remember_relationship`, which requires exactly one matching Mac contact; "my wife" then resolves to that contact. Probe `verify_m6_m7.py`: before remembering the controller asks for the name; an ambiguous name is refused; after remembering, the draft binds Sam Okafor and reaches confirmation. |
| M7 | Executed | A draft longer than the 120-unit confirmation prompt is shown whole in the exact reader ("MESSAGES · REVIEW", tap: send, Back cancels, 90 s timeout); the confirmation then carries the opening words marked "(reviewed)". Probe: a 217-character body was reviewed, confirmed within the cap and sent to the fake sender; a declined review sent nothing. Without a reviewer the old refusal stands. |
| M8 | Executed | `read_conversation` gives whole messages; `get_recent_messages` snippet widened to 160 chars and points at it. Sender subprocess already bounded (15 s). Reply match order unchanged (typed reply entry precedes the broad regex). |
| M9 | Verified present | Prepared rows (62 columns × 10 rows, exact source spans) replace character pages. |
| M10 | Verified present | `IMessageStore` constructed unconditionally; router retries the same store. |
| Q1 | Verified present | Back asks the Mac to keep the capture and re-renders the current controls. |
| Q2 | Verified present | Exact reader pages replace the 64-char list item. |
| Q3 | Source | `read_quicknotes` tool (text notes + recording transcripts); glasses list shows 8 + 8; "note …" refreshes an open list. Voice-note detail already includes the full transcript segments in the reader. |
| B1 | Verified present | A background Brainstorm returns speech to the normal path (`background_brainstorm` guard). |
| B2 | Verified present | Terminal handoff opens the real picker via `open_terminal_from_brainstorm`. |
| B3 | Source | Synthesis is followed by a VERIFY line: its own citations are checked, otherwise it states how many turns had sources checked (possibly none). Selecting a saved session now opens its whole transcript in the exact reader ("BRAINSTORM · SAVED", tap: continue); continuation starts only on that second, deliberate tap. Collapsed live-turn gists are unchanged (a density choice). |
| T1 | Source | `ApprovalReceipt` carries the recorded verdict as data; the glasses handler no longer compares a sentence with a word. |
| T2 | Source (partial) | `terminal_sessions` tool (roster + lane tail); the spoken "terminal history" no longer cuts each entry to its first 160 characters (whole entry, whitespace collapsed, 400-char cap). Multi-host discovery and full-output paging on the lens remain roadmap items. |
| O1 | Source | Unread = Messages.app `is_read = 0` incoming count, refreshed every 20 s off the loop thread. |
| O2 | Verified present | The concurrent session's commit 52885e6 landed `Card.body` (the full notification text) and the detail page reading it through the reader's rows; 66b4fea keeps a re-shown banner finding its whole message. |
| O3 | Source | Brainstorm/Terminal tap accepts left temple, right temple and ring (Even's input reference gives both temples the same gesture set; only left double is Back). |
| N1 | Source | No wire change needed: `NewsService.run` now publishes only when a pass changed the headlines or the failure state (the plugin dates a panel by its push), a kept-stale list carries a first row "[!] Feeds unavailable · showing headlines from N ago", a partial pass says so, and the spoken "refresh the news" answer distinguishes updated / partial / kept-from-earlier. |
| N2 | Executed (assistant side) + Source | `read_news_article` opens the same article the tab opens; when publisher extraction fails, the feed's own summary (tags stripped) is the article body, labelled as such, instead of the headline repeated. |
| N3 | Verified moot | News and Search render on the custom canvas again; the native list rebuild path the audit exercised is no longer the News path. |
| P1-TIMER | Verified present | `PomodoroStore` checkpoint + `activate_restored()` at boot; `control_timer` tool. |
| NAV1 | Executed | `start_navigation` tool reaches the same session factory as "navigate to"; "open navigation/directions/map" spoken opener. Provider/location/physical route remain unverified. |
| Reconnection | Source + log analysis | Code 4409 replacement protocol in the gateway and MacLink is present. The "separate disconnect incidents": in `~/Library/Logs/JOAT/brain.log`, the physical glasses (serial S200LACC…) dropped at 04:46:10 right after two audio sequence gaps (04:45:51, 04:46:02 — frames lost on the phone link), reconnected at 04:53:32, dropped again 12 s later, and returned at 04:55:13; the afternoon "disconnects" (13:34, 13:36, 13:50) are the official simulator (serial S2001234567890) reloading between another session's runs. The log recorded no close code or reason for any of them; the gateway now logs code, reason, duration and whether a replacement took over, so the next incident carries the peer's own words. Physical cause (phone lock/background, Wi-Fi/Tailscale roaming) is not determinable from the Mac side alone. |
| OpenClaw/Hermes | Executed (protocol fixed; provider limited) | Live probe against the wearer's own gateway through the running engine: the instruction was accepted, dispatched, and every run died at its first chat event because the gateway sends `state: "status"` mid-run (captured 2026-09-05 17:19, seq=2) and the adapter treated any unknown state as a protocol violation. Fix: informational chat states are skipped — text still enters only through delta/final with the divergence checks, and a stream ending without a terminal event still raises and aborts. After the fix the run proceeded past the status event and surfaced the provider's own truthful error ("provider rate limit reached"), repeated across spaced retries. The full "ready." answer is blocked by the Mac mini provider's rate limit, not by JOAT; retry when the quota lifts. |
| Background takeover | Verified present | `HudSink` paints only while `_view_is_current()`. |
| Navigation ownership | Verified by inventory | Every local surface transition now informs the Mac: `close()` departs home, panel tabs, Settings and feature entry depart, Terminal sends `terminal-home`, QuickNotes sends `quicknotes-*`, the firmware contextual menu reports open/closed, and app backgrounding sends `prompt.cancel` upstream so a lease cannot survive the wearer leaving. The wearer's menu is the firmware contextual menu; the plugin no longer has a local menu of its own, so the legacy `surface-menu` vocabulary has no sender by design. |
| QuickNotes refresh | Source | `note …` and `create_quicknote` both re-open QuickNotes. |
| Documentation | Source (partial) | MAC_APP.md no longer requires a Hermes seat for Brainstorm. Typography and navigation document conflicts are in the concurrent session's scope. |
