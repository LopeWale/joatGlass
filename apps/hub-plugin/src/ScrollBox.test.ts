import { describe, expect, it } from 'vitest';
import { utf8ByteLength } from '@evenrealities/even_hub_sdk';
import { SCROLL_BOX_MAX_BYTES, ScrollBox } from './ScrollBox';

/** A document of numbered lines, a blank line after every fifth. */
function document(count: number): string[] {
  const lines: string[] = [];
  for (let n = 1; n <= count; n += 1) {
    lines.push(`L${String(n).padStart(3, '0')} the quick brown fox jumps over the lazy dog`);
    if (n % 5 === 0 && n < count) lines.push('');
  }
  return lines;
}

const VISIBLE = 7;

describe('ScrollBox', () => {
  it('holds as many whole lines as fit its bytes', () => {
    const lines = document(120);
    const box = new ScrollBox(lines, VISIBLE);
    const text = box.text();
    expect(utf8ByteLength(text)).toBeLessThanOrEqual(SCROLL_BOX_MAX_BYTES);
    expect(text.startsWith(lines[0]!)).toBe(true);
    // One more line would not have fit.
    const shown = text.split('\n').length;
    expect(utf8ByteLength(`${text}\n${lines[shown]}`)).toBeGreaterThan(SCROLL_BOX_MAX_BYTES);
    expect(box.atEnd).toBe(false);
    expect(utf8ByteLength(box.head(999))).toBeLessThanOrEqual(999);
    expect(text.startsWith(box.head(999))).toBe(true);
  });

  it('carries on one line below the last screen, skipping a blank line, to the end and no further', () => {
    const lines = document(120);
    const box = new ScrollBox(lines, VISIBLE);
    let stretch = box.text().split('\n');
    let moves = 0;
    while (box.down()) {
      moves += 1;
      const next = box.text().split('\n');
      const lastScreen = stretch.slice(stretch.length - VISIBLE);
      expect(next[0]).not.toBe('');
      expect([lastScreen[1], lastScreen[2]]).toContain(next[0]);
      expect(next.length).toBeGreaterThanOrEqual(VISIBLE);
      stretch = next;
    }
    expect(moves).toBeGreaterThan(2);
    expect(box.atEnd).toBe(true);
    expect(stretch[stretch.length - 1]).toBe(lines[lines.length - 1]);
    const end = box.position;
    expect(box.down()).toBe(false);
    expect(box.position).toBe(end);
  });

  it('goes back a screen at a time, to the start and no further', () => {
    const box = new ScrollBox(document(120), VISIBLE);
    box.down();
    box.down();
    const top = box.position;
    expect(box.up()).toBe(true);
    expect(box.position).toBeLessThan(top);
    expect(top - box.position).toBeLessThanOrEqual(VISIBLE);
    while (box.up());
    expect(box.position).toBe(0);
    expect(box.up()).toBe(false);
  });

  it('cuts every stretch smaller when the glasses refuse one, from the same line', () => {
    const box = new ScrollBox(document(120), VISIBLE);
    box.down();
    const top = box.position;
    box.limit(999);
    expect(box.position).toBe(top);
    expect(utf8ByteLength(box.text())).toBeLessThanOrEqual(999);
    expect(box.bytes).toBe(999);
  });

  it('opens where the wearer left a page, and puts back a stretch the glass did not take', () => {
    const lines = document(60);
    const box = new ScrollBox(lines, VISIBLE, SCROLL_BOX_MAX_BYTES, 40);
    expect(box.position).toBe(40);
    box.down();
    box.moveTo(40);
    expect(box.text().split('\n')[0]).toBe(lines[40]);
    expect(new ScrollBox(lines, VISIBLE, SCROLL_BOX_MAX_BYTES, 10_000).position).toBe(lines.length - 1);
  });

  it('resumes a document grown longer a line below the last screen, and anything else afresh', () => {
    const lines = document(60);
    const box = new ScrollBox(lines, VISIBLE);
    while (box.down());
    expect(box.atEnd).toBe(true);
    const lastScreenTop = lines.length - VISIBLE;
    // Older messages loaded below: carry on a line below the last screen
    // (two, past the blank between paragraphs).
    const grown = [...lines, 'Older message one.', 'Older message two.'];
    const resumed = box.resume(grown) ?? -1;
    expect([1, 2]).toContain(resumed - lastScreenTop);
    expect(grown[resumed]).not.toBe('');
    // The same document reopens where it was.
    expect(box.resume(lines)).toBe(box.position);
    // Another document (a reply at the top, a shorter text) is not resumed.
    expect(box.resume(['You: On my way', ...lines])).toBeUndefined();
    expect(box.resume(lines.slice(0, 10))).toBeUndefined();
    // Grown while the wearer is mid-document: the same stretch.
    const early = new ScrollBox(lines, VISIBLE);
    expect(early.resume(grown)).toBe(0);
  });

  it('opens a document that grows at its end on the first new line, wherever the wearer stopped', () => {
    const lines = document(300);
    // The wearer read a little of round one and spoke their move.
    const box = new ScrollBox(lines, VISIBLE);
    box.down();
    box.down();
    expect(box.atEnd).toBe(false);
    const grown = [...lines, '', 'YOU > give me a plan', '', 'CLAUDE > Here is the plan.'];
    expect(box.resume(grown, true)).toBe(lines.length + 1);
    expect(grown[box.resume(grown, true) ?? -1]).toBe('YOU > give me a plan');
    // Without the flag a mid-document reader stays put, as before.
    expect(box.resume(grown)).toBe(box.position);
    // Nothing new: where the wearer was. Another document: afresh.
    expect(box.resume(lines, true)).toBe(box.position);
    expect(box.resume(['A different text', ...lines], true)).toBeUndefined();
  });

  it('opens within the bytes a page rebuild may carry, so opening needs no second write', () => {
    const lines = document(120);
    const box = new ScrollBox(lines, VISIBLE);
    // The whole stretch is bigger than a rebuild's 999 bytes.
    expect(utf8ByteLength(box.text())).toBeGreaterThan(999);
    box.openWithin(999);
    const opening = box.text();
    expect(utf8ByteLength(opening)).toBeLessThanOrEqual(999);
    expect(box.head(999)).toBe(opening);
    expect(opening.split('\n').length).toBeGreaterThanOrEqual(VISIBLE);
    expect(opening.startsWith(lines[0]!)).toBe(true);
    expect(box.position).toBe(0);
    // The wearer's first screen of scrolling still reaches the larger stretches.
    expect(box.down()).toBe(true);
    expect(box.position).toBeGreaterThan(0);
    // A limit larger than the box's own changes nothing.
    const roomy = new ScrollBox(lines, VISIBLE);
    const whole = roomy.text();
    roomy.openWithin(SCROLL_BOX_MAX_BYTES * 4);
    expect(roomy.text()).toBe(whole);
  });

  it('shows a short document whole, with nothing beyond it', () => {
    const box = new ScrollBox(['Two short words.'], VISIBLE);
    expect(box.text()).toBe('Two short words.');
    expect(box.atEnd).toBe(true);
    expect(box.down()).toBe(false);
    expect(new ScrollBox([], VISIBLE).text()).toBe(' ');
  });
});
