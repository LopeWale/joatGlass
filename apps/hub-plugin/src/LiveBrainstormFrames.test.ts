/**
 * Real frames from a real Brainstorm, run through the real renderer.
 *
 * The bound lives on both sides of the wire in different units: the Mac
 * counts CHARACTERS (BRAINSTORM_ACTIVITY_MAX), this renderer wraps
 * PIXELS. Nothing checked those against each other, and nothing checked
 * either against text a model actually produces — every fixture in this
 * repo was hand-written to be convenient.
 *
 * These frames were captured from a live two-round session (real Codex,
 * real Claude) by scratch capture into
 * src/fixtures/live-brainstorm-frames.json.
 */
import { describe, expect, it } from 'vitest';

import {
  HUD_TEXT_MEAN_ADVANCE_PX,
  brainstormTranscriptLines,
  wrapHudText,
} from './HudCanvasRenderer';
import frames from './fixtures/live-brainstorm-frames.json';

/** The transcript column: HUD_CONTENT_WIDTH, x=14..562. All three
 * constants here were stale for a full release — a 504px column, an SF
 * Mono advance, and a 524-character bound — surviving from the old narrow
 * monospace design while the shipped surface drew 548px of proportional
 * type against a 4239-character scrollback. A "live frames" test measured
 * against a renderer that no longer existed. */
const COLUMN_PX = 576 - 14 * 2;
/** Proportional Helvetica Neue at 11px, measured on the real simulator. */
const CHAR_PX = HUD_TEXT_MEAN_ADVANCE_PX;
const measure = (value: string): number => value.length * CHAR_PX;

/** What the Mac's BRAINSTORM_ACTIVITY_MAX allows: 40 scrollback rows of
 * 105, plus the 39 newlines separating them. Keep in step with
 * protocol.py — tests/unit/test_sinks.py re-derives this equality. */
const ACTIVITY_MAX = 105 * 40 + 39;

describe('live Brainstorm frames', () => {
  it('captured something worth testing', () => {
    expect(frames.length).toBeGreaterThan(10);
    expect(frames.some((f) => f.state === 'settled')).toBe(true);
  });

  it('never exceeds the character bound the Mac promises', () => {
    for (const frame of frames) {
      expect(frame.activity.length).toBeLessThanOrEqual(ACTIVITY_MAX);
      expect(frame.topic.length).toBeLessThanOrEqual(96);
      for (const seat of frame.collaborators) {
        expect(seat.length).toBeLessThanOrEqual(48);
      }
    }
  });

  it('wraps every frame inside the optical column', () => {
    for (const frame of frames) {
      for (const line of wrapHudText(frame.activity, COLUMN_PX, measure)) {
        expect(measure(line)).toBeLessThanOrEqual(COLUMN_PX);
      }
    }
  });

  it('shows the wearer the answer, not just the last thing said', () => {
    // The point of sitting through a session. It survived a bug that
    // replaced it with a status line, and it must also survive the tail
    // trim that keeps only the newest rows.
    const settled = frames.filter((f) => f.state === 'settled');
    const final = settled[settled.length - 1];
    expect(final).toBeDefined();

    const visible = brainstormTranscriptLines(
      final!.activity, COLUMN_PX, 7, measure,
    ).join(' ');
    expect(visible).toContain('AGREED');
    expect(visible).toContain('converged');
  });

  it('keeps a live turn readable rather than one long unbroken row', () => {
    // Finished turns collapse to one row each; the live turn keeps its
    // prose. If collapsing ever stopped working, a single turn would fill
    // the whole region and the debate would be unfollowable again.
    const running = frames.filter((f) => f.state === 'running');
    const busiest = running.reduce(
      (a, b) => (b.activity.length > a.activity.length ? b : a),
      running[0]!,
    );
    const rows = brainstormTranscriptLines(busiest.activity, COLUMN_PX, 7, measure);
    expect(rows.length).toBeGreaterThan(2);
    const speakerRows = rows.filter((row) => / > |^…/.test(row));
    expect(speakerRows.length).toBeGreaterThan(1);
  });
});
