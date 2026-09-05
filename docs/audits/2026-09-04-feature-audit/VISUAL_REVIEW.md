# Simulator visual review

All twenty-six captures from corrected job `job-mtnfhaxt-87fd1ed7` were inspected at their native 576 by 288 size, including five contact sheets and focused image inspection. The twenty-five distinct hashes and zero captured console errors establish successful capture diversity, not feature acceptance. Twenty-four captures show their intended layouts; two News drill-ins do not. Product-source hashes still match the job snapshot.

## What the frames show

The display is capable of clear hierarchy: Navigation guidance has a prominent turn/distance, the expanded Pomodoro has a large countdown, and the short Messages reader displays multiple complete lines. Those observations contradict a claim that every screen is blank or incapable of readable content.

The design varies substantially between features. Overview devotes nearly half the display to the clock while the right column holds several kinds of information. Messages, Notes and notifications occupy only the upper part of the display. Live Brainstorm fits much denser text, and Terminal uses a smaller operational style. Several long page names are ellipsized in the native header. Footers vary between tap, speech, swipe and Back instructions; source audit A2/M2/O3 shows why some of those promises are not fulfilled by the corresponding input path. These are concrete observations supporting the need for consistent reading and navigation behavior, not a measured physical readability score.

A static recording screen can look correct while the next interaction is unsafe. QuickNotes recording and discard screens both look coherent here; the separate Q1 controller replay nevertheless maps a restored Save and finish row to discard. The Messages screenshots likewise do not overcome the independently reproduced paging and Back defects.

## Per-state observations

| Fixture | Inspected observation |
|---|---|
| [overview](evidence/verified/gallery/overview.png) | Large left clock, narrow right information column, icon-only tab rail. |
| [messages](evidence/verified/gallery/messages.png) | Clear picker and New message row; most of the lower display is unused. |
| [messages-conversation](evidence/verified/gallery/messages-conversation.png) | Five short message rows; page title is ellipsized. Static pixels do not exercise paging. |
| [message-reader](evidence/verified/gallery/message-reader.png) | Readable multi-line body and page indication. This short fixture does not cover the M9 overflow case. |
| [messages-compose](evidence/verified/gallery/messages-compose.png) | Recipient-first prompt; no editable transcript in this fixture. |
| [notifications](evidence/verified/gallery/notifications.png) | Three preview rows with a detail hint. |
| [notification-detail](evidence/verified/gallery/notification-detail.png) | Sender, short message and receipt age; most of the display is empty. |
| [quicknotes](evidence/verified/gallery/quicknotes.png) | Four note summaries; static fixture is not a complete-history check. |
| [quicknote-detail](evidence/verified/gallery/quicknote-detail.png) | Summary, key points and todos; complete transcript is not displayed. |
| [quicknote-recording](evidence/verified/gallery/quicknote-recording.png) | Save, Pause and Discard rows; static view does not prove safe Back behavior. |
| [quicknote-discard](evidence/verified/gallery/quicknote-discard.png) | Confirm discard and Keep recording rows. |
| [brainstorm-sessions](evidence/verified/gallery/brainstorm-sessions.png) | Eight rows fit; visible metadata distinguishes saved sessions. |
| [brainstorm-ready](evidence/verified/gallery/brainstorm-ready.png) | Short topic prompt with tap/speech and Back hints. |
| [brainstorm-live](evidence/verified/gallery/brainstorm-live.png) | Dense multi-speaker text in a smaller body style; footer offers Terminal and cancel. |
| [terminal-sessions](evidence/verified/gallery/terminal-sessions.png) | Three named lanes and states. |
| [agent-terminal-ready](evidence/verified/gallery/agent-terminal-ready.png) | Status, lane summary and instruction; substantially smaller text than Messages. |
| [agent-terminal-blocked](evidence/verified/gallery/agent-terminal-blocked.png) | Blocked, host/policy/session lines and retry hint. Fixture host is synthetic. |
| [panel-news](evidence/verified/gallery/panel-news.png) | Three headline previews beside the clock; Updated now is a receipt-time label, not source verification. |
| [panel-news-expanded](evidence/verified/gallery/panel-news-expanded.png) | Capture shows underlying Overview, native NEWS rebuild returns false in the focused diagnostic. Excluded from list-layout acceptance. |
| [panel-article](evidence/verified/gallery/panel-article.png) | Capture shows underlying Overview, native News opening failed, so article text is not reached. Excluded from article-layout acceptance. |
| [panel-pomodoro](evidence/verified/gallery/panel-pomodoro.png) | Overview timer shows rounded remaining minutes and block count. |
| [panel-pomodoro-expanded](evidence/verified/gallery/panel-pomodoro-expanded.png) | Large exact countdown, block progress and Pause/Skip/Reset controls. |
| [assistant-overlay](evidence/verified/gallery/assistant-overlay.png) | Large centered response overlay with a dismissal hint; no latency evidence. |
| [nav-destination](evidence/verified/gallery/nav-destination.png) | Destination name/address and confirm/retry hints. |
| [nav-guidance](evidence/verified/gallery/nav-guidance.png) | Prominent turn arrow, distance, street and trip summary; no live-route proof. |
| [nav-error](evidence/verified/gallery/nav-error.png) | Visible location-start error and retry/cancel hints. |

## Evidence boundaries

The News list and article captures are excluded from successful layout acceptance because they show Overview. A separate short diagnostic in an instrumented temporary copy resolved this case: the native NEWS list rebuild returned false, then the fixture completed with the menu closed and the default Notifications surface. The post-completion screenshot still shows Overview. The article is never reached. A comment about keeping Overview in browser preview does not explain these native captures. The rejection reason and physical firmware outcome remain unknown; this is N3 in the audit. See [transition receipt](evidence/news-transition/receipt.json), [native result and completed state](evidence/news-transition/console.json), and [post-completion frame](evidence/news-transition/after-completion.png).

The gallery uses developer fixtures, not Mac-authored live feature journeys. It does not prove a real microphone transcript, full-message database read, contact resolution, send confirmation/submission, remote Terminal authorization, route provider or phone GPS. The screenshot endpoint and simulator cannot establish physical BLE timing, optical comfort or left/right source behavior. The legacy Messages flow was rejected as evidence because its entry hook drifted to Planning; no successful live simulator send/reply journey is claimed.

The first job's twenty-six identical Overview captures are superseded and explicitly rejected in `evidence/first-run-review.json`. They are not counted above. Alpha-channel extraction is used to display the simulator framebuffer as grayscale; no rendered content was redrawn.

Installed verification tools: official EvenHub simulator 0.9.3 and Even Hub SDK 0.0.14. No product source was changed by the diagnostic; only its temporary copy adds content-free transition instrumentation plus synthetic fixture payload/state logging.
