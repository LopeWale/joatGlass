# FEAT-002 — QuickNotes: one Conversate + QuickNote feature that saves to Apple Notes

Status: design for build 0.1.122, written 2026-10-11 before coding. Amend it
before changing a contract below.

## 1. The wearer's ask

"My quick note feature should be like the Even Conversate + QuickNote
features in one, and save it to my Notes app." Agreed scope (the wearer's
"go ahead" on 2026-10-11, items 1–7 of the brief in the joatGlass project,
`claude/quicknotes-conversate-redesign.md`).

Even's reference behaviour (support.evenrealities.com, checked 2026-10-11):

- QuickNote: hold the TouchPad while speaking; release and that speech is one
  paragraph; repeat for more; double-tap → Save / Discard / Cancel; a title
  is generated on save.
- Conversate: live transcription on the lens; pause/resume; double-tap ends;
  afterwards a summary (overview, highlights, next steps → Reminders) and the
  full transcript.

A Hub plugin cannot write into Even's own QuickNote or Conversate history (no
SDK call), so the note's home is Apple Notes on the Mac, carried to the
iPhone by iCloud.

## 2. What exists (before this feature)

- `services/voice_note_capture.py`: exclusive-lane recorder; the whole
  recording is transcribed only after Save; title is the clock
  ("Note 21:10") and never replaced.
- `services/note_insights.py`: local summary → SUMMARY / KEY POINTS / TODOS
  after a retained save; reminders and Apple Notes export only by voice, only
  for the latest recording.
- `services/notes_export.py`: AppleScript into a "JOAT" folder.
- QuickNotes page: "● Record a voice note", "✎ Dictate a note", then notes.
  While recording the lens shows only Save / Pause / Discard rows.
- `AudioRouter.begin_capture` does not open the plugin mic; a muted mic makes
  a silent recording with no warning.

SDK facts this design rests on (installed `@evenrealities/even_hub_sdk`
0.0.16, `dist/index.d.ts`): `OsEventTypeList.LONG_PRESS_EVENT = 9` (press
starts) and `LONG_PRESS_RELEASE_EVENT = 10` (release), delivered in the
sys/text/list event of the capture container; `AudioEvent.speakerRole` is
`self | other | unknown` per PCM frame — "App algorithm result; this is not a
firmware identity assertion".

## 3. Design

### 3.1 One note, two ways to talk

QuickNotes page control rows:

| Row | Starts |
|---|---|
| `● Record` | a note that is recording continuously (Conversate) |
| `✎ Quick note · hold to talk` | a note that is open and paused (QuickNote) |
| `⇪ Save to Apple Notes: On/Off` | toggles the setting (§3.5) |

Both open the same live page, `QUICKNOTES · LIVE` (a firmware text page):

| Gesture on the live page | Effect |
|---|---|
| tap | recording ⇄ paused |
| hold (press … release) | records while held, when paused; one paragraph per hold |
| double-tap | `QUICKNOTES · FINISH`: Save note / Keep recording / Discard note |

Double-tap on FINISH returns to the live page. Discard keeps the existing
`QUICKNOTES · DISCARD?` confirmation. Voice commands keep working (they are
heard only while paused, as before: recording owns the microphone).

Body of the live page: one status line (`● Recording 03:12`,
`Paused 03:12 · hold to talk`, `● Listening…` while held, or the no-audio
warning), then the newest transcript lines, labelled `You:` / `Them:` when
the frames' `speaker` agrees (≥ 2/3 of labelled frames). Hint per state.

### 3.2 Live transcription (Mac)

The recorder cuts the journaled frame stream into chunks and transcribes
each chunk while recording continues:

- cut when the recorder's own `RmsVad` closes an utterance (≈1 s pause),
  when a chunk reaches 20 s, on a capture discontinuity, on pause, on hold
  release and on stop;
- a chunk whose loudest frame is below `speech_rms` (0.004, digital
  near-silence) is recorded as a quiet span without STT;
- any other chunk goes to `SttEngine.transcribe_once(pcm)` — new, stateless,
  never touches the conversational utterance buffer. Text → transcript
  segment (+ speaker label); no words → quiet span (`no_words`); engine
  unavailable → retried once at stop, then a `transcription_gap`;
- Save drains the queue, so it costs at most the last chunk's STT.

`finalize` counts a frame as covered when a transcript segment OR a quiet
span covers it; raw audio is purged only when every frame is covered and
there are no gaps (unchanged rule). A note saved with zero frames is
discarded instead ("Nothing was recorded").

### 3.3 Hold-to-talk and the microphone

- Plugin: on the live page, `LONG_PRESS_EVENT` sends
  `capability-lifecycle / quicknotes-hold-start` and the release sends
  `quicknotes-hold-end`. The mic-mute toggle does not fire there.
- `AudioRouter` keeps a 1.2 s ring of recent frames; a hold start replays
  that pre-roll to the recorder so the words spoken before the press
  registered as long are kept.
- No-audio guard: if capture is on and no frame has arrived for 1.5 s, the
  Mac sends `AudioControl(open=True)` once; after 3 s the status line says
  `No audio from the mic — check the glasses`.

### 3.4 Title and summary

The local summarizer prompt gains `TITLE:` and uses OVERVIEW / KEY POINTS /
NEXT STEPS (parser keeps accepting SUMMARY / TODOS). A note still titled by
the clock (`Note HH:MM`) is renamed to the generated title.

### 3.5 Apple Notes

After the automatic summary (success or not) the note is written to Apple
Notes, folder "JOAT", default account: title, date · duration, Overview, Key
points, Next steps, Transcript (with You/Them labels and gap markers). The
Apple Notes id is stored (`note_exports`); a later re-send updates that note
instead of creating a duplicate. Text notes ("take a note …", "save Claude's
reply as a note") are written too. Setting:
`~/Library/Application Support/JOAT/notes.json` `{"saveToAppleNotes": true}`
(default on), toggled from the QuickNotes page row or by voice ("stop saving
notes to Apple Notes" / "save notes to Apple Notes"). The wearer asked for
this cloud-synced copy explicitly (RULES.md #8).

### 3.6 Reminders

A recording's detail page: tap = add its next steps to Reminders, through
`ProductivityActionController.create_reminder` (each confirmed, idempotent by
origin), for any note, not only the latest.

### 3.7 Storage (migration 15, CREATE only)

- `voice_note_quiet_spans(capture_id, first_frame_ordinal, last_frame_ordinal, reason)`
- `voice_note_segment_speakers(capture_id, segment_ordinal, speaker)`
- `note_exports(note_kind, note_id, target, external_id, exported_at_us)`

## 4. Acceptance criteria

- AC1 Record: `● Record` → live page; spoken sentences appear within a few
  seconds of each pause; tap pauses and resumes; double-tap → Save → note in
  the list with a generated title.
- AC2 Quick note: `✎ Quick note` → hold, speak, release → a paragraph
  appears; twice → two paragraphs; Save.
- AC3 Apple Notes: after save the note exists in Notes › JOAT with summary,
  next steps and transcript; with the setting off nothing is written.
- AC4 No audio: with the mic muted, starting a recording reopens it, or the
  status line says so within 3 s.
- AC5 Reminders: tap on a note's page → confirmed reminder per next step.
- AC6 Suites: Mac pytest + mypy, plugin vitest + tsc green; simulator run of
  AC1/AC2 with synthetic audio.

## 5. Out of scope

AI cues during a recording, Prep Notes, a phone-mic choice per note,
PDF/DOCX export, word-by-word partials while a sentence is still being
spoken.
