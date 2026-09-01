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
  brainstormTranscriptLines,
  wrapHudText,
} from './HudCanvasRenderer';
import frames from './fixtures/live-brainstorm-frames.json';

/** The transcript column, from drawBrainstormTranscript. */
const COLUMN_PX = 504;
/** SF Mono at 11px: advance 1266/2048 em. */
const CHAR_PX = 11 * (1266 / 2048);
const measure = (value: string): number => value.length * CHAR_PX;

/** What the Mac's BRAINSTORM_ACTIVITY_MAX allows: 7 rows of 74, plus the
 * six newlines that separate them. Keep in step with protocol.py. */
const ACTIVITY_MAX = 74 * 7 + 6;

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
