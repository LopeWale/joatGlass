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

  it('shows a short document whole, with nothing beyond it', () => {
    const box = new ScrollBox(['Two short words.'], VISIBLE);
    expect(box.text()).toBe('Two short words.');
    expect(box.atEnd).toBe(true);
    expect(box.down()).toBe(false);
    expect(new ScrollBox([], VISIBLE).text()).toBe(' ');
  });
});
