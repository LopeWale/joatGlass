# HUD Dashboard — design

The resting display for the 576x288 monochrome-green HUD, and the pages the
wearer moves between. Written against the real container model and the
measured constraints in [HUB_PLUGIN.md](HUB_PLUGIN.md); every claim about
current behaviour carries a file:line.

STATUS: the custom visual dashboard is implemented. The current runtime uses
four 288×144 Gray4 image containers as one authored 576×288 frame and one empty
brightness-zero text container as the sole gesture-capture layer. The semantic
Main/Context/Tray streams below remain presenter inputs; their old firmware text
container diagrams are the architecture record, not the current render path.
The canonical contract is [DESIGN.md](../DESIGN.md), with visual acceptance in
official Even Hub simulator captures.

### The right pane is a rail of tabs (2026-09-02)

The overview's right pane carries a vertical rail — notifications, news,
pomodoro, search — on its outer-left edge (`PANEL_RAIL` in
`HudCanvasRenderer.ts`: icons at x=290, one slot per tab at y=70/118/166/214,
the pane's hairline at x=308.5, content from x=318). Everything a tab
change can alter lives east of the tile seam at x=288, so a swipe uploads
the two right tiles only; the expanded pomodoro's countdown sits inside the
top-right tile so a one-second tick uploads one.

| Gesture | Resting overview | Expanded tab | Opened row (reader) |
|---|---|---|---|
| swipe | move along the rail (local) | move the row | page, when the Mac's hint offers it |
| tap | expand the lit tab | open the row (`capdata`, echoing `panel-<tab>-<revision>`) | — |
| left double | — | back to the overview | back to the tab |
| long press | mic | refresh the tab (`caprefresh`) — news and search only; pomodoro keeps the mic | mic |

Which tab is up is plugin state (`PanelRail.ts`); the Mac never learns it.
What the Mac owns is pushed as `hud.panel` — one message per tab, up to 20
rows in the dense-row grammar, a revision, and for the timer its phase and
end instant — cached by the plugin so a swipe never round-trips. A row tap
that echoes an older revision is refused. Notifications is not a push: it
is the overview's own Main/Context/Tray streams, and its tap still asks the
Mac for the `NOTIFICATIONS` page. Mac side: `services/panels.py` publishes
and answers taps; `news.py`, `pomodoro.py`, `search.py` are the providers;
`panel_commands.py` is the voice; the Mac app writes `news.json`.

### Current visual container map

| id | role | x,y,w,h | z | capture |
|---|---|---|---|---|
| 4 | top-left Gray4 tile | 0,0,288,144 | 1 | 0 |
| 8 | top-right Gray4 tile | 288,0,288,144 | 2 | 0 |
| 9 | bottom-left Gray4 tile | 0,144,288,144 | 3 | 0 |
| 10 | bottom-right Gray4 tile | 288,144,288,144 | 4 | 0 |
| 2 | transparent gesture layer | 0,0,576,288 | 5 | **1** |

`HudCanvasRenderer.ts` paints the rounded shell, status spine, Phosphor icons,
Sidebar, focus rows, and confirmation. `HudVisualSurface.ts` splits the frame
and serializes all four uploads on the bridge's single command lane. Pending
frames coalesce to the newest state, a partial failed decision frame is actively
rolled back, mic truth is structured, and the Mac's 0...4 urgency level remains
visible after the authored 8-bit canvas is quantized to Gray4 output.

Three defects in the pre-dashboard single-container HUD motivated this and are worth
fixing regardless of how much of the design lands:

1. A notification card OVERWRITES an in-flight reply — both write MAIN.
2. The wearer's own words are erased ~200ms after being shown: the
   orchestrator paints the query, then streams the reply over the same
   container. The proof-of-being-heard is destroyed by the answer to it.
3. When the Mac is down the HUD is dead — every glanceable fact is either
   plugin-side and unrendered, or Mac-side and unreachable.

## 1. What the wearer sees at rest

### The spine of the design: chrome is plugin-owned, content is Mac-owned

Before this dashboard landed, the HUD was two containers and everything the
Mac said landed in one of them. That baseline had three consequences:

- A notification card **overwrites the reply**. `SessionManager.notify` → `_show_card` → `hud.show(card)` (`session.py:53-64`) writes the same `ContainerId.MAIN` the reply streams into (`orchestrator.py:149-157`).
- The wearer's own words are **erased 200 ms after being shown**. `orchestrator.py:149` does `await self._hud.show(query)`, then line 157 does `await self._hud.show(chunk)` on the same container. The proof-of-being-heard is destroyed by the answer to it.
- When the Mac is down the HUD is **dead**. Every glanceable fact (clock, battery, mic, link) is either plugin-side and unrendered (`DeviceMonitor.ts:44-54`, `MacLink.ts:4`) or Mac-side and unreachable.

So the dashboard is not "more text on the HUD". It is: **split MAIN into fixed regions, give each region one owner, and let the plugin render its own chrome without the Mac.**

### The original text-container grid

576×288, 4-bit greyscale (`docs/HUB_PLUGIN.md:31`), ~46 chars/line (`cards.py:25`), 32 px/line.

> **Both numbers are project assumptions, not SDK facts.** 46 comes from `cards.py:14-15` ("roughly 46 characters per line at the status font"); 32 px/line is inferred from `HudPageBuilder.ts:26` (a one-line status bar declared 32 px tall). The SDK exposes no font metric. Measure on device before locking: paint `MAIN` with `"0123456789" * 6` and count where it wraps, then with 9 lines of `"X"` and count where it clips.

The following diagram explains the presenter streams that fixed content
clobbering. It is not the current SDK layout. The custom raster surface can draw
hairlines, dividers, rounded geometry, and measured type while retaining those
same ownership boundaries.

### DASH — the resting page

```
 123456789|123456789|123456789|123456789|123456   <- 46 char columns
+----------------------------------------------+  y=0   id1 STATUS  z1  bright2  capture0
|09:41 · JOAT · ● · ▇                          |
+----------------------------------------------+  y=32  id2 MAIN    z2  bright4  capture1 <-- EVENT
|Dele . Messages                               |
|On my way, 10 min out                         |
|                                              |
|                                              |
+----------------------------------------------+  y=160 id5 CONTEXT z3  bright3  capture0
|you 09:38  ok see you at 7                    |
|                                              |
+----------------------------------------------+  y=224 id6 TRAY    z4  bright2  capture0
|2 waiting . Dele, Battery              12 asks|
+----------------------------------------------+  y=256 id7 HINT    z5  bright1  capture0
|                                              |
+----------------------------------------------+  y=288
```

| id | name | x,y,w,h | z | capture | textColor | owner | source |
|---|---|---|---|---|---|---|---|
| 1 | `status` | 0,0,576,35 | 1 | 0 | 2 | **plugin** | `HudStatusModel` (`HudStatus.ts`): clock + `MacLink` state + `audio` mic + `DeviceMonitor` battery/charging, composed as `9:41 · PAGE · ● · ▇` by `nativeStatusLine` and fitted by `HudPageBuilder.fitHeader` |
| 2 | `main` | 0,32,576,128 | 2 | **1** | 4 | Mac | `last_reply` / card summary / debate stream (`status.py:42-44`) |
| 5 | `context` | 0,160,576,64 | 3 | 0 | 3 | Mac | `last_transcript` (`status.py:42`), verifier verdicts (`session.py:283-286`) |
| 6 | `tray` | 0,224,576,32 | 4 | 0 | 2 | Mac | `_pending_cards` depth (`session.py:47`), `counts` (`status.py:45`) |
| 7 | `hint` | 0,256,576,32 | 5 | 0 | 1 | plugin | active gesture map; blank at rest |

The shipped visual page instead uses the five-object map above. `main` (id 2)
is empty and invisible but keeps `isEventCapture: 1`, so scroll events arrive as
`textEvent` and right/ring presses as `sysEvent`; the image tiles stay visual-only.

Four walking-safety choices, deliberately:

1. **The resting page is 4 short lines, not a dense grid.** Dead space on a see-through HUD is the feature. Density arrives only on long-press.
2. **`textColor` inverts the usual instinct**: chrome dim (1-2), content bright (4). Verified available at both create time (`index.d.ts:429`) *and* per-update (`index.d.ts:1136-1138`, "省略表示保持容器当前亮度" — omit to keep current), so brightness changes need **no rebuild**.
3. **The hint line is blank at rest** and only lights for ~3 s after a gesture, or when a mode has non-obvious gestures.
4. **Right-alignment is space padding**, which assumes a monospace advance. NEEDS-EMPIRICAL — if the font is proportional, fall back to a single `·`-separated run.

## 2. The other pages, and the gesture map

Dashboard states reuse the same five visual containers with different pixels. App
navigation is progressive disclosure: only a left-temple double-click opens the
temporary 160px Sidebar, with the current Overview dimmed but visible at right.
A selection removes the Sidebar into a full-focus frame with structured rows.
Confirmation paints an exclusive decision frame and restores the exact interrupted
Overview, Sidebar, or focus page on confirm, cancel, or timeout.

When a fresh Mac brain connects with no active session, it clears the Mac-owned
Main, Context, and Tray streams before announcing itself. This deliberately
returns the plugin to its connected-empty `Ready` surface instead of retaining
a reply from an older brain process. Active sessions retain their surface, and
the transient `HEARING YOU` status always settles back to `LISTENING` after an
utterance, including empty or unaddressed speech.

### REPLY — conversation

```
+----------------------------------------------+  y=0   id1 STATUS  bright2
|09:41 · JOAT · ANSWERING · ● · ▇              |
+----------------------------------------------+  y=32  id2 MAIN    bright4   <- streamed by append
|Standup is at 10:00. You have 19 minutes      |
|and the room is Ada.                          |
|                                              |
|                                              |
+----------------------------------------------+  y=160 id5 CONTEXT bright3   <- kept, not clobbered
|you 09:41  joat when is the standup         |
|                                              |
+----------------------------------------------+  y=224 id6 TRAY    bright2
|qwen3-4b . 1.8s                      2 waiting|
+----------------------------------------------+  y=256 id7 HINT    bright1
|tap stop                                      |
+----------------------------------------------+  y=288
```

This single change fixes the `orchestrator.py:149`/`:157` clobber: query goes to `CONTEXT`, reply streams into `MAIN`, both survive.

### CARD — notification

```
+----------------------------------------------+  y=0   id1 STATUS  bright2
|09:47   JOAT listening                 bat 86%|
+----------------------------------------------+  y=32  id2 MAIN    bright4
|!! Dele . Messages (3)                        |
|Call me when you land, its urgent             |
|                                              |
|                                              |
+----------------------------------------------+  y=160 id5 CONTEXT bright3
|09:38 ok see you at 7                         |
|09:44 you still coming                        |
+----------------------------------------------+  y=224 id6 TRAY    bright2
|3 waiting . Battery, Calendar          4 cards|
+----------------------------------------------+  y=256 id7 HINT    bright4  <- lifted for 3s
|tap next . hold open . 2 more behind          |
+----------------------------------------------+  y=288
```

Line 1 and line 2 of `Card.render()` (`cards.py:105-114`) become two *containers*, not two lines in one string — so the `!!` header can sit at brightness 4 while the body sits at 3, and a reply underneath is not destroyed.

### DEBATE — stream

```
+----------------------------------------------+  y=0   id1 STATUS  bright2
|DEBATE  swift vs rust for the shell       r2/2|
+----------------------------------------------+  y=32  id2 MAIN    bright4  <- append stream, scrollable
|> CRITIC (claude)                             |
|The weakest claim is the 40% figure;          |
|no source supports it for menu-bar            |
|apps on Apple silicon.                        |
+----------------------------------------------+  y=160 id5 CONTEXT bright3  <- verifier, OUT of the stream
|VERIFIER [1] OK  [2] FAIL no quote match      |
|DENIED - repair 1 of 1 requested              |
+----------------------------------------------+  y=224 id6 TRAY    bright2
|propose>critique>judge              agree 0.78|
+----------------------------------------------+  y=256 id7 HINT    bright1
|say stop to cancel                            |
+----------------------------------------------+  y=288
```

`_verify_visibly` currently appends `» VERIFIER` and per-citation verdicts **into the same stream as the argument** (`session.py:272-286`), which makes both unreadable. Moving verdicts to `CONTEXT` and round/agreement to `TRAY` preserves the wearer's stated requirement that verification is visible, while letting the argument stay legible.

### SIDEBAR — transient navigation over Overview

```
+-----------------+----------------------------+  y=0   STATUS  bright2
|09:47 JOAT ready                              |
+-----------------+----------------------------+  y=32
|> Messages       |NEXT                        |
|  Notifications  |Design review · 4:30        |
|  Calendar       |                            |
|  Tasks          |LAST QUERY                  |
|  More           |Where is the nearest coffee|
|                 |2 notifications waiting     |
+-----------------+----------------------------+  y=288
  x=0..160 Sidebar   x=164..576 dim Overview
  authored pixels   one shared capture layer
```

The Sidebar is not the app shell and is not a full-width menu. It exists only
between a left-temple double-click and either a selection or a second
left-temple double-click. The full-frame transparent layer remains the sole
event capture; the Overview is retained as spatial context at 24% intensity. Selecting a capability
removes the rail before its content is requested from the Mac.

The Sidebar is one continuous native-scrollable rail rather than a five-row
root plus `More`. It exposes `Messages`, `Planning`, `Navigation`,
`Brainstorm`, `Notifications`, `QuickNotes`, `Agent Terminal`, and `Activity`
as direct first-level destinations, followed by `Repeat`, `Reconnect`, and
`Settings`. The five visible optical rows are only a viewport; scrolling never
changes the hierarchy. Selecting any destination still removes the rail into
the same full-focus shell.

Every capability open now carries a unique `requestId` through `capmenu`, the
Mac's `hud.list` response, and any later `capdata` row selection. The Mac drops
a superseded generation before it can replace row snapshots, and the plugin
rejects a response that does not match the currently visible generation. This
matters even when the wearer reopens the *same* capability: title equality is
not response identity. `hud.list.selectable` is also explicit protocol data;
the renderer and event router no longer infer a tappable row from title copy.
Setup, empty, detail, and receipt frames therefore cannot acquire an invisible
action merely because their title happens to resemble a collection.

Navigation commits only after the corresponding visual paint succeeds. While
Sidebar, More, Settings, or a loading focus page is being painted, input is
consumed by a transition state. A rejected paint closes back to the restored
Overview; it cannot leave an invisible Settings or More state accepting taps.
The same two-phase rule applies when Mac data replaces a loading page: matching
generation first, complete four-tile paint second, row activation last.

### CONFIRM — the exclusive decision state

```
+----------------------------------------------+  status spine
|09:47 JOAT ready                     bat 86%  |
+----------------------------------------------+
|Calendar review                               |
+----------------------------------------------+
|Create event “Design review”? Work · Sep 1    |
|at 09:30 · 45m · America/Chicago              |
|                                              |
|                                              |
|tap: confirm · swipe: cancel · left x2: back ·|
|auto-cancel                                    |
+----------------------------------------------+  y=288
```

`RULES.md:15` makes this the highest-stakes surface on the device. The question
is the only full-intensity content; outcomes are explicit but visually
secondary. The review uses the same flat, left-aligned reading axis as
Planning, Navigation, and Messages. It has no outer rounded card, no action
box, faux button divider, progress strip, or changing countdown. That static
frame avoids resending all four image tiles once per second while the wearer is
reading; the real eight-second refusal window remains in the input gate.
Ordinary content is deferred. A single press on either built-in G2 temple
confirms, while either swipe refuses and left-temple double press is Back.
An optional R1 press remains compatible but is deliberately not advertised or
required; eight seconds without input refuses. The gate then restores the
interrupted Overview, Sidebar, or focus page.

### FULL-FOCUS PAGE — destination after Sidebar selection

```
+----------------------------------------------+  status spine
|09:47 JOAT ready                     bat 86%  |
+----------------------------------------------+
+|MESSAGES                                      |
+----------------------------------------------+
+|> New message · voice recipient + text        |
+|  Adele (unread) · Attachment                 |
+|  Marcus · Heading to the lab in 10.          |
+----------------------------------------------+
|tap select · left x2 overview                 |
+----------------------------------------------+  y=288
```

The shared transparent layer owns capture; the Sidebar and dimmed Overview are
gone, so the destination owns the whole canvas. Scroll moves the custom-selected
row and a tap/click selects it. Selecting a one-to-one Messages row opens the
full conversation page and arms a voice draft (`JOAT, reply saying ...`); it
does not send. Attachment-only rows say **Attachment** and unread incoming rows
say **unread** rather than copying a notification card. A page with older
history truthfully says **down for older**; at the bottom edge, the Hub forwards
that scroll through the production route and the Mac returns **Older messages —
up for newer**. The reverse top-edge scroll returns the latest cached page. A
complete local thread never advertises an older-page action. The full draft and
recipient must then fit on the exclusive Confirmation screen before the guarded
Messages.app boundary can run. Group rows remain explicitly read-only rather
than silently routing a group reply to one participant. Overview paints are
buffered until return. Notifications, Calendar, Tasks, QuickNotes, and Activity
rows now open deterministic read-only detail states. Setup, empty, detail, and
write-lifecycle pages do not draw a selected outline or advertise a nonexistent
tap action.

### PLANNING — one nested Calendar and Reminders hierarchy

`Planning` is the only first-level productivity destination. It re-queries
both EventKit stores at every scope and never presents stale independent
Calendar and Tasks silos:

```text
Planning month — Monday-first 6 × 7 grid with E#/R# counts
  swipe: move selected week · tap: open selected week
Planning week — seven day rows with event/reminder counts
  swipe: move selected day · tap: open selected day
Planning day — one time-ordered mix of EVENT and REMINDER rows
  swipe: move selected item · tap: open read-only detail
Planning detail
  left x2: return to Planning
```

Terminal Calendar and Reminder receipts return to the confirmed or native-
read-back day after five readable seconds. A verified Reminder create first
shows `SAVED` for five seconds and `MONITORING` for five seconds, then returns.
A left-temple double press returns immediately from either state. Successful
creates appear in that mixed day, definite failures and refusals do not, and a
successfully completed Reminder disappears. When a Reminder completion is
uncertain or its native due instant cannot be proven, JOAT returns to generic
Planning rather than displaying a possibly wrong day.

### QUICKNOTES — one recorder and one archive

`QuickNotes` unifies the meeting/speech recorder and exact dictated text
notes. "JOAT, start a quicknote about X" (or "voice note") begins a
journal-first capture in which the microphone belongs EXCLUSIVELY to the
recording — nothing said mid-meeting reaches the assistant. The QuickNotes
menu row therefore becomes the recorder's control panel while a capture is
live (Save and finish / Pause / Discard as tappable rows, with a two-row
Confirm/Keep page guarding discard); voice control returns while paused.
Save shows an honest `SAVING` frame through transcription, then the unified
list: recordings first ("· recorded", "· gaps" when degraded), text notes
after. On save the Mac auto-summarizes locally (resident MLX; transcripts
never leave the Mac) into summary + key points + todos, engine-attributed
and replaceable — "resummarize with claude" is the explicit cloud opt-in via
the official CLI. Tapping a recording opens summary/key points/todos;
"ask the last note …" answers from the transcript; "create reminders from
the last note" walks each todo through the existing confirmed EventKit gate
(due tomorrow 09:00, provenance `voice_note.todo`); "send the last note to
my notes" writes Apple Notes on the Mac (JOAT folder — the Even plugin has
no phone-side Notes API) and iCloud carries it to the phone.

### Gesture map — original exploration

This table records the design exploration that preceded implementation. It is
not the current interaction contract; use [DESIGN.md](../DESIGN.md) and
`EventRouter.test.ts` for shipped gestures. In particular, card/inbox actions
below remain future work.

**This section is stale exploration and is corrected below.** It was
written before the gestures were bound and several of its claims now
contradict the code and the installed SDK typings. Read `EventRouter.ts`
for the truth; what follows is kept for the reasoning, not the facts.

Corrections, verified against the source and `node_modules/@evenrealities/even_hub_sdk/dist/index.d.ts`:

- **Left-temple single press is no longer free.** It opens the capability
  menu while a Brainstorm is live, which is what lets the wearer reach the
  menu without cancelling their session (cancel is left DOUBLE press).
- **Confirmation accepts left as a confirm source**, not "only ring and
  right temple".
- **The Mac does register `on_input`** (`__main__.py`,
  `capabilities.on_input`). The bare relay is ignored because it carries no
  `containerName` and that handler has no else branch — a different reason
  with the same effect.
- **Long press does not "relay with no wiring".** It is consumed for the
  microphone control on every event class and never reaches the Mac.
- **Left temple double opens the capability sidebar**, not Settings.
  Settings is a row inside it, or contextual-menu item 4.
- **Clicks can arrive as `textEvent`**: some hosts omit CLICK in the
  protobuf encoder, and `EventRouter` handles exactly that.

| Gesture | Arrives as | DASH | CARD | REPLY / DEBATE | INBOX |
|---|---|---|---|---|---|
| **Left** temple single | `sysEvent` t0 s3 | cycle focus | next card | — | — |
| **Right** temple single | `sysEvent` t0 s1 | **confirm only** | confirm only | confirm only | select row |
| **Ring** single | `sysEvent` t0 s2 | **confirm only** | confirm only | confirm only | select row |
| Long press → release | `sysEvent` t9 → t10 | open INBOX (rebuild) | open full card | stop stream | back to DASH |
| Swipe up / down | `textEvent` t1 / t2 | scroll `MAIN` | scroll `MAIN` | scroll the log | SDK-internal, no event |
| Left temple double | `sysEvent` t3 s3 | Back (menu toggle) | Back | Back | Back |
| Right temple double | `sysEvent` t3 s1 | same Back (no exit dialog; exit is the Mac's `shutdown`) | same | same | same |
| OS contextual menu | `menuItemClickEvent` | Mic on/off · Inbox · Repeat · Settings | — | — | — |

Sources per `protocol.py:34-40` (LEFT is 3, RING is 2). Long-press 9/10 exists in the enum (`index.d.ts:880-883`) and is CONSUMED for the microphone control — it does not relay. Note `eventSource` exists only on `sysEvent` (`index.d.ts:989`): swipes and list events are source-blind by SDK design, so no left/right distinction is possible for them. Menu items: ≤10, 32-byte labels, non-zero unique ids (`index.d.ts:514-532`).

Three traps to encode as tests, not comments:

- **Every numeric read needs `?? 0`** — CLICK is 0 and the first list index is 0, and the protobuf transport drops zero values (`RULES.md:48`, `EventRouter.ts:38-43`). A gesture map is exactly the code where this bug looks correct and does nothing.
- **Omitting `menuObject` on a rebuild CLEARS the menu** (`index.d.ts:850`, `:1402`). Every layout in `HudPageBuilder` must carry it once menus land.
- **Whether the host auto-scrolls a text container on swipe is unverified.** `docs/HUB_PLUGIN.md:59-64` documents that the event *arrives*, not that the host scrolls. If it doesn't, paging is ours via `contentOffset` windows over the 2000-char budget — the bookkeeping already exists in `HudPresenter.ts:16,41-57`.

## 3. New protocol messages

Downlink kinds are dotted (`hud.text`, `policy.set`), uplink kinds are bare nouns (`hello`, `device`, `policy`) — `types.ts:36-60`. New messages follow that:

**Downlink (Mac → plugin)**

| Kind | Payload | Why |
|---|---|---|
| `hud.dash` | `{ status?, main?, context?, tray?, hint? }` — every field optional, absent = leave that region alone | One frame instead of five `hud.text` messages; one place that defines write ordering (content first, chrome last) so regions never tear |
| `hud.card` | `{ id, source, channel, summary, urgency, count, at }` | Send *structure*, not layout. `Card.render()` (`cards.py:105-114`) bakes two lines into a string only because the HUD is one blob; with regions the plugin places and brightens them |
| `hud.mode` | `{ mode: 'dash'\|'reply'\|'card'\|'debate'\|'inbox'\|'settings' }` | The plugin cannot know what a temple press means without it, and it decides whether a rebuild is even needed (only inbox/settings) |

**Uplink (plugin → Mac)**

| Kind | Payload | Why |
|---|---|---|
| `mode` | `{ mode: ... }` | Plugin-local page changes (settings menu, inbox) are invisible to the Mac today. Mirrors the `policy` / `policy.set` pair exactly |
| `card` | `{ id, action: 'next'\|'dismiss'\|'open' }` | Without it the "N waiting" count lies the moment the wearer advances a card |

**`ContainerId` gains `CONTEXT = 5`, `TRAY = 6`, `HINT = 7`** in both `types.ts:7-12` and `protocol.py:25-31`.

Deferred, not needed for the dashboard: `hud.menu` (`{ items: [{id, label}] }`) — keep the contextual menu plugin-local until the Mac has dynamic actions to put in it.

**Both files change in the same commit.** `protocol.py:1-10` states it, `CLAUDE.md` states it, and `tests/unit/test_protocol.py:1-7` is the enforcement — it pins the exact wire shapes `types.ts` emits and fails on drift. Each new message needs a `to_wire()` case there. Python side: `HudDash`, `HudCard`, `HudMode` dataclasses added to the `DownlinkMessage` union (`protocol.py:275-286`); `Mode` and `CardAction` added to `UplinkMessage` (`protocol.py:153`) plus `parse_uplink` match arms (`protocol.py:313-372`).

Mac-side seams that move with them: `HudWriter` (`services/hud.py:16-33`) gains `dash()` / `card()` / `mode()`; `SessionManager.notify` (`session.py:53`) takes a `Card` instead of a rendered `str`; `NotificationChannel`'s `deliver` callback type (`notifications.py:164`) follows.

## 4. Original build order

Historical sequencing follows. Completion of an early step does not imply the
speculative card, inbox, or debate steps shipped.

**Step 0 — brightness + plugin-composed status line.** Two files, no protocol, no Mac change.
Add `textColor: 2` to `statusBar()` and `textColor: 4` to `main()` (`HudPageBuilder.ts:19-50`); have `HudPresenter.setStatus` (`HudPresenter.ts:59-61`) compose `HH:MM  <mac text>  bat NN%` from data the plugin already holds — `DeviceMonitor`'s snapshot (`DeviceMonitor.ts:44-54`), `MacLink` state (`MacLink.ts:4`, wired at `main.ts:61-65`), `audio.isOpen` (`main.ts:85`). This is the smallest change that makes the HUD visibly better, and it establishes the chrome/content ownership line. It also means the clock and battery keep rendering when the Mac is down.

**Step 1 — enforce the invariants before adding containers.** Derive `containerTotalNum` from the arrays instead of the hand-kept literals `2`/`3` (`HudPageBuilder.ts:55,63,72`), and add `HudPageBuilder.test.ts` running every layout through `validateEvenHubPageContainer` (`index.d.ts:697`). `docs/HUB_PLUGIN.md:284` and `:338` both flag this as an armed failure — adding containers is precisely the change that trips it, and it surfaces as a *blank HUD* through the masked `invalid` path (`BridgeClient.ts:70-75`), not as an error.

**Step 2 — the 5-container layout.** `HudPageBuilder` + `ContainerId` 5/6/7 in `types.ts` and `protocol.py`, same commit. The Mac keeps writing only `STATUS_BAR` and `MAIN`; the new regions render empty. Nothing regresses.

**Step 3 — plugin-side chrome fills in.** Ticking clock, mic/link/battery/wearing indicators, transient hint line. Still no protocol change. At this point the dashboard exists and survives a dead Mac.

**Step 4 — `hud.dash` + `HudWriter.dash()`.** First real use is the clobber fix: query → `CONTEXT`, reply → `MAIN` (`orchestrator.py:149-157`). Also register `transport.on_device` in `__main__.py` — it is currently never registered anywhere in `apps/mac/joat`, so battery/wearing arrive at the gateway (`hub_gateway.py:178-179`) and are dropped.

**Step 5 — `hud.card` + `card` uplink + a plugin-side card queue.** Cards stop overwriting replies; `TRAY`'s "N waiting" becomes true because dismissals reach the Mac.

**Step 6 — `hud.mode` / `mode`, long-press → INBOX.** The first rebuild-requiring page; capture moves to the list.

**Step 7 — contextual menu** (`menuObject` on create + *every* rebuild).

**Step 8 — debate regions.** Verifier verdicts out of the argument stream into `CONTEXT` (`session.py:272-286`).

Steps 0-3 ship a real dashboard with **zero protocol change**. The protocol only moves once the Mac has something to say that the plugin cannot already render itself.
