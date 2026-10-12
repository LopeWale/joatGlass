# FEAT-002 tasks — QuickNotes: Conversate + QuickNote, saved to Apple Notes

Checkbox state is the position. Design: `technical-design.md` in this folder.

- [x] **T1** Storage + audio seams: migration 15; `VoiceNoteStore` rename,
  frame ranges, quiet spans, speaker labels, coverage in `finalize`;
  `AudioRouter` pre-roll; `SttEngine.transcribe_once` (+ WhisperServe).
  Done-when: `test_voice_note_store.py`, `test_audio_router*.py`,
  `test_whisper_serve*.py` cases green; mypy clean on the files.
- [x] **T2** Recorder: live chunked transcription, quick-note start, hold
  start/end, stop drains + retries, empty-note discard, change hook,
  no-audio state. Done-when: `test_voice_note_capture.py` green.
- [x] **T3** Insights + Apple Notes: TITLE / OVERVIEW / NEXT STEPS, rename,
  exporter create/update by id, auto-save voice and text notes, `notes.json`
  setting + voice toggle, reminders for any note. Done-when:
  `test_note_insights.py`, `test_notes_export.py`, `test_note_commands.py`
  green. ✔ 2026-10-11 (T1–T3: Mac 3544 passed; mypy unchanged).
- [x] **T4** Capability router pages: list rows, LIVE (throttled repaint),
  FINISH, hold events, Apple Notes row, detail tap → reminders, `__main__`
  wiring. Done-when: `test_capabilities.py` QuickNotes cases green. ✔ 2026-10-11.
- [x] **T5** Plugin: LIVE in-place text upgrade, double-tap → finish,
  long press → hold events. Done-when: vitest + `tsc` green. ✔ 2026-10-11 (1190 tests).
- [ ] **T6** Simulator: AC1 + AC2 with synthetic audio; full Mac + plugin
  suites; Changelog; build 0.1.122.
- [ ] **T7** Hardware (wearer): AC1–AC5 on the G2.
