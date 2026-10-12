# FEAT-001 — Messages: reply and compose by voice from the glasses

Status: as-built design, written 2026-10-11 after the feature had been coded
and simulator-verified (0.1.117–0.1.119) but had never worked on the real G2.
This document records what exists, what blocked it, and what "done" means on
hardware. Amend it before changing a contract below.

## 1. The wearer's ask

On the Messages page: tap a conversation, tap Reply, speak; see the words as
they are heard; say it again if they were misheard; confirm; the message goes.
Also start a new message to a contact by voice. The report that opened this
work (2026-10-11): "tap to reply does not work at all; the listening does not
show anything I am saying".

## 2. As-built flow and contracts

Wire kinds live twice by design (`apps/hub-plugin/src/types.ts`,
`apps/mac/joat/core/protocol.py`; pinned by `tests/unit/test_protocol.py`).

| Step | Glasses / plugin | Mac |
|---|---|---|
| Open Messages | `CapabilityMenu` → Messages list; row 0 is `NEW_MESSAGE_ROW` ("New message · voice recipient + text"), rows 1.. are conversations | `capabilities._select_message` |
| Pick a thread | tap row | `message reply trace=… stage=selected`; `MessageUIController` visit with a reply fence |
| Reply | tap the Reply row, or say "reply" | `MessageUIController.begin_reply` → `MessageReplyController.prepare_selected_reply` → `stage=reply-prompt-armed` |
| New message | tap row 0 | `begin_new` → `prepare_new_message` → recipient question (`field=recipient`), then body |
| Question | `prompt.present` → `PromptedInputController.handle`: paint (`showPromptQuestion`), open the glasses mic (`AudioStreamer.openPromptMicrophone`), arm the epoch, send `prompt.ack{success, audioEpoch=promptId}` | `PromptedSpeechRuntime` publishes via `PromptedInputDispatcher`; the ack sets the ingress fence; VAD is fenced (`fence_prompt_audio`) |
| Listening | JOAT's box: "JOAT: What do you want to say back to X?" then "You: …" filling as the words arrive. **tap = review now** (`input` `prompt-commit`, `selectedName` = the question's ID), **2x tap = cancel**; swipes consumed | frames before the fence / another epoch / another mic are refused; partials every ~1 s → `prompt.partial`; a `prompt-commit` closes the utterance at once (`RmsVad.finish_now`) and runs the ordinary completion path; silence still closes it as before |
| Answer | — | final transcript → `dispatch` eligibility (acknowledged, owner current, not mixed, same mic, same connection, same epoch, no `other` speaker frame, started after the fence) → `consume` → `stage=review-requested` → `stage=confirmation-requested` |
| Review (consent) | the same box with the draft: **tap = send**, **2x tap = retry** (`input` `message-retry`; the Mac re-asks and the listening box returns). Long tap and swipe do nothing on a draft. No gesture notes printed (wearer's rulings 2026-10-06, 2026-10-11) | `confirm` → `stage=submitted delivery=unverified` via `channels/message_sender`; `send-failed`/`cancelled` otherwise |
| After confirm | the thread, no page of its own; the Mac repaints it. A `message.ui` receipt of any finished state paints nothing | `MessageUIController.on_status`: `sending` → `show_reply_status(body, "sending")` draws the draft as the newest row with **▷**; `sent` → `show_sent_reply` replaces it with the real row, **▶**; `failed` → **×**; `uncertain` keeps ▷. The glyph sits after the newest own row only; no status word anywhere (firmware font has no check marks) |
| Diagnostics | `prompt_stage` events: presented, painted/paint_refused, mic_ready/mic_refused, acknowledged/ack_refused, retired; `messages_confirmation` accepted/declined/timed_out | `prompted answer not taken owner=… reason=…`; `g2-diagnostics.json` when the toggle is on |

Mac-side tests already pin this path end to end with synthetic audio:
`tests/integration/test_prompted_speech*.py`, `test_prompt_gateway.py`,
`test_message_reply_entry.py`, `test_message_ui.py` (including "a misheard
reply is asked for again and only the restatement goes"). Plugin tests:
`PromptedInputController.test.ts`, `AudioStreamer.test.ts`, `MessageUI.test.ts`.

## 3. Why it never worked on hardware

From `~/Library/Logs/joat/brain.log` and `g2-diagnostics.json`, 2026-10-11:

- Since 2026-10-08 08:48 every real-G2 connection lived ~15.1 s and closed
  with code 1005 (no close frame): 2,352 connections on 10-08/09, still
  looping 10-11. Before that (10-06/07) connections lasted 13–53 minutes and
  the mic path transcribed speech normally.
- `MacLink.downlinkHandlerTimeoutMs` (15 s) expires the socket when one
  Mac→plugin message is not handled in time. A header/Home-column image write
  (containers 16–19) hung on the glasses; every page change waited on it.
- Each reconnect aborts the downlink dispatcher, which retires any open
  question (`prompt.cancel`). No question could live long enough to show words.
- The plugin reported the hung writes as `tile_upload` diagnostics; the Mac
  dropped them as `g2 diagnostic containerId is invalid` (it accepted 4/8/9/10
  only), so the trace never explained the loop.
- The same loop is the likelier cause of the 10-11 "Home tabs don't switch"
  report than the notification that 0.1.120 blamed.

Fixed in `b1f1c6a` (Build 0.1.121, same day): `HudVisualSurface` page-part
upload timeout 6 s (`PAGE_PART_UPLOAD_TIMEOUT_MS`), page parts switch
themselves off after a hang or three failures and the page is redrawn as
firmware text, page transitions wait at most 8 s (`PAGE_TRANSITION_IDLE_MS`),
and the trace accepts containers 16–19. Verified in the simulator with the
first header write held forever; not yet on physical glasses (that is §5).

## 4. What this feature adds (2026-10-11)

1. `joat/ble/hub_gateway.py` `_note_link_end`: three consecutive authenticated
   connections that end 15 ± 3 s after connecting with code 1005/1006 log one
   WARNING naming the plugin's downlink watchdog and pointing at the trace
   script; a link that then holds closes the streak. The failure class now
   explains itself in `brain.log`.
2. `scripts/reply_trace.py`: one command over `brain.log` + `g2-diagnostics.json`
   that lists recent connections (flagging watchdog-shaped ones), the Messages
   reply stages, the glasses-side `prompt_stage` steps, refusal reasons,
   consent decisions, voice-path counts, and a plain-words verdict. Content-free:
   no bodies, recipients, question text, transcripts, or serials.

3. Gesture contract on the box (T7, T8): listening tap = review now, 2x = cancel;
   review tap = send, 2x = retry. One template, two states of the same box.
4. Thread status glyph (T9): ▷ / ▶ / × on the newest own row; finished
   receipts paint no page.

## 5. Acceptance criteria (hardware)

Run on the real G2 with "Record G2 diagnostics" on and the brain restarted from
the revision that includes §3's fix. Then `.venv/bin/python scripts/reply_trace.py --since 1h`.

- AC1 Link: the connection that carried the run lived longer than 5 minutes;
  no connection in the window is watchdog-shaped; no watchdog WARNING.
- AC2 Reply: stages `selected → reply-prompt-armed → review-requested →
  confirmation-requested → submitted`, and the message appears in Messages.
- AC3 Listening: `prompt_stage` shows `presented → painted → mic_ready →
  acknowledged → retired`; the wearer saw their words appear on the card
  while speaking (photograph through the lens).
- AC4 Retry: a 2x tap on the review box re-asks the question and the
  listening box returns; only the restatement is sent.
- AC5 Cancel: a 2x tap on the listening box sends nothing (`stage=cancelled`).
- AC5b Review now: a tap while speaking closes the words so far and shows the
  review box without waiting for silence.
- AC5c Thread: after tap-send the thread shows "You: <message> ▷" at once and
  "You: <message> ▶" once Messages records it; no status text page.
- AC6 Compose: row 0 → recipient question → body question → consent → sent.
- AC7 No refusal reasons in the window other than ones the wearer caused on
  purpose (e.g. speaking before the card).
- AC8 `reply_trace.py`'s verdict for the run reads "A message was submitted to
  Messages." and nothing about the watchdog.

## 6. Out of scope / follow-ups

- Word-level spoken corrections ("change 4:30 to 5") on the consent card: a new
  prompt field and a protocol change; blocked this round because `protocol.py`
  is under edit. "Say it again" is the modify path for now.
- Showing the consent gestures on the card: the wearer asked for no gesture
  notes in JOAT's box (0.1.118). Revisit only if AC4/AC5 fail for discoverability.
