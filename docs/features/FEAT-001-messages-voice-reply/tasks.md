# FEAT-001 tasks — Messages: reply and compose by voice

Program for `agentic-sdlc-kit:feature-implementation`. Checkbox state is the
position. Design: `technical-design.md` in this folder.

- [x] **T1** Mac names the plugin's downlink-watchdog loop in brain.log.
  `joat/ble/hub_gateway.py` `_note_link_end` (+ `PLUGIN_DOWNLINK_WATCHDOG_S`,
  `PLUGIN_WATCHDOG_TOLERANCE_S`, `PLUGIN_WATCHDOG_LOOP_THRESHOLD`).
  Done-when: `tests/unit/test_hub_gateway_watchdog_loop.py` green (9 cases incl.
  the existing drop-log test); `mypy` clean for the file. ✔ 2026-10-11.
- [x] **T2** `scripts/reply_trace.py` tells where a spoken reply stopped.
  Done-when: `tests/unit/test_reply_trace.py` green; run against the real log
  for 2026-10-08 09:10 → verdict names the watchdog loop (2291 of 2311
  connections); `mypy --strict scripts/reply_trace.py` clean. ✔ 2026-10-11.
- [x] **T3** Page-part image writes stop holding the link: `HudVisualSurface`
  6 s page-part timeout, page parts switch off after a hang/3 failures, page
  transitions wait ≤ 8 s; trace accepts containers 16–19 (`protocol.py`,
  `G2DiagnosticTrace.ts`). Landed by the parallel session as `b1f1c6a`,
  packaged as 0.1.121 (`21c7a32`): plugin 1175 tests, Mac 3481 tests. ✔
  2026-10-11. Still owed by the wearer before T4: install 0.1.121 on the phone
  and **restart JOAT from this revision**.
- [ ] **T4** Hardware run (wearer): diagnostics on, Messages → thread → Reply →
  speak → consent → tap; then row 0 → recipient → body → consent → tap; one
  long-tap "say it again"; one swipe cancel.
  Done-when: `scripts/reply_trace.py --since 1h` shows AC1–AC3, AC5, AC6, AC8
  of the design; the lens photo shows "You: …" filling while speaking.
- [ ] **T5** One task per defect T4 surfaces (add here as `T5.n` with its
  reply_trace verdict line as the done-when).
- [ ] **T6** Feature verification: every AC in technical-design §5 demonstrated;
  Mac suite + mypy green; plugin suite + typecheck green; Changelog records the
  hardware verification; no WIP markers.
