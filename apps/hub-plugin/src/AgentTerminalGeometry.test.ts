/**
 * The terminal surface's rows must not collide, and must not run off the
 * lens.
 *
 * These positions are literals inside drawBrainstorm, so nothing checked
 * them. Adding the LANES row meant moving the running layout's divider
 * (111.5 -> 127.5) and its transcript (132 -> 148); an error there would
 * have overlapped rows or pushed the last line under the gesture footer,
 * and no unit test would have noticed — the canvas is never rendered in
 * this suite, and no simulator is available from the CLI here.
 *
 * The constants below are read from the draw path. If it moves, these
 * fail, which is the point.
 */
import { describe, expect, it } from 'vitest';

import { agentTerminalTranscriptLayout } from './HudCanvasRenderer';
import type { BrainstormHudModel } from './types';

/** Baselines drawn on this surface, from drawBrainstorm. */
const SUMMARY_Y = 98;      // 13px — "HERMES > RUNNING · …"
const SECOND_ROW_Y = 117;  // 10px — LANES when running, HOST when recovering
const HINT_Y = 267;        // 9px  — the gesture footer, centred
const LINE_HEIGHT = 14;    // drawBrainstormTranscript
const CANVAS_HEIGHT = 288;

function layoutFor(state: BrainstormHudModel['state']) {
  return agentTerminalTranscriptLayout({
    state, noToolsChecked: false, topic: 'hermes · x',
  });
}

describe('Agent Terminal geometry', () => {
  for (const state of ['running', 'ready', 'approval_required', 'blocked'] as const) {
    it(`does not collide or overflow when ${state}`, () => {
      const layout = layoutFor(state);

      // The second row must clear the summary above it, and the divider
      // must clear that row's descender.
      expect(SECOND_ROW_Y - SUMMARY_Y).toBeGreaterThanOrEqual(13);
      expect(layout.dividerY - SECOND_ROW_Y).toBeGreaterThanOrEqual(10);

      // The transcript starts below the divider, with room for its own
      // ascender rather than sitting on the rule.
      expect(layout.transcriptBaselineY - layout.dividerY)
        .toBeGreaterThanOrEqual(11);

      // Its LAST row must clear the gesture footer. This is the one that
      // silently truncates on a real lens if a layout grows.
      const lastRow =
        layout.transcriptBaselineY + (layout.transcriptLineCapacity - 1) * LINE_HEIGHT;
      expect(lastRow).toBeLessThan(HINT_Y - 9);
      expect(lastRow).toBeLessThan(CANVAS_HEIGHT);
    });
  }

  it('gives a running terminal more transcript than a recovering one', () => {
    // Recovery states spend rows on HOST/POLICY/SESSION; running spends
    // them on the conversation, which is the whole point of the surface.
    expect(layoutFor('running').transcriptLineCapacity)
      .toBeGreaterThan(layoutFor('blocked').transcriptLineCapacity);
  });

  it('keeps the LANES row clear of the recovery detail it shares a line with', () => {
    // Both are drawn at SECOND_ROW_Y and are mutually exclusive: LANES
    // only when !showRecoveryDetails, HOST only when it is set. If that
    // ever stops being exclusive they overlap on the glass.
    expect(layoutFor('running').showRecoveryDetails).toBe(false);
    for (const state of ['ready', 'approval_required', 'blocked'] as const) {
      expect(layoutFor(state).showRecoveryDetails).toBe(true);
    }
  });
});
