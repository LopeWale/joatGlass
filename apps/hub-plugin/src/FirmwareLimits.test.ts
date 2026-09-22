import { describe, expect, it } from 'vitest';
import { utf8ByteLength } from '@evenrealities/even_hub_sdk';
import {
  LIST_ITEM_MAX_BYTES, LIST_MAX_ITEMS, TEXT_CONTAINER_MAX_BYTES,
  fitListItems, fitUtf8, fitUtf8Tail,
} from './FirmwareLimits';

/**
 * The limits are the firmware's, as Even's simulator enforces them "to
 * behave more like firmware" (evenhub-simulator README: v0.7.1 "add text
 * container bytes limit to 999"; v0.7.3 "constraint list item text size to
 * be maximum 63 bytes and 20 items"). A page over any of them is rejected
 * WHOLE — observed live on 2026-09-22: the News tab card (1203 bytes) and
 * the expanded News list (a 67-byte headline) both failed silently.
 */
describe('FirmwareLimits', () => {
  it('states the firmware limits', () => {
    expect(TEXT_CONTAINER_MAX_BYTES).toBe(999);
    expect(LIST_ITEM_MAX_BYTES).toBe(63);
    expect(LIST_MAX_ITEMS).toBe(20);
  });

  it('fitUtf8 counts BYTES, never splits a code point, and marks the cut', () => {
    expect(fitUtf8('short', 63)).toBe('short');
    const curly = '‘annihilate’ Iran'.repeat(6); // 3-byte quotes: chars ≠ bytes
    const cut = fitUtf8(curly, 63);
    expect(utf8ByteLength(cut)).toBeLessThanOrEqual(63);
    expect(cut.endsWith('…')).toBe(true);
    expect(cut.length).toBeLessThan(curly.length);
    // Exactly at the limit is untouched.
    expect(fitUtf8('a'.repeat(63), 63)).toBe('a'.repeat(63));
    expect(utf8ByteLength(fitUtf8('a'.repeat(64), 63))).toBeLessThanOrEqual(63);
  });

  it('fitUtf8 drops whole rows from a multi-line pane, not half a word', () => {
    const rows = Array.from({ length: 30 }, (_, i) => `[${i}m] Headline number ${i} from a source · BBC`);
    const cut = fitUtf8(rows.join('\n'), TEXT_CONTAINER_MAX_BYTES);
    expect(utf8ByteLength(cut)).toBeLessThanOrEqual(TEXT_CONTAINER_MAX_BYTES);
    const kept = cut.replace(/…$/, '').split('\n');
    for (const row of kept) expect(rows).toContain(row);
  });

  it('fitUtf8Tail keeps the newest text within the budget', () => {
    const tail = fitUtf8Tail('x'.repeat(1990) + 'THE-END', 999);
    expect(utf8ByteLength(tail)).toBeLessThanOrEqual(999);
    expect(tail.startsWith('…')).toBe(true);
    expect(tail.endsWith('THE-END')).toBe(true);
    expect(fitUtf8Tail('fits', 999)).toBe('fits');
  });

  it('fitListItems caps a firmware list at 20 rows of 63 bytes', () => {
    const rows = fitListItems(Array.from({ length: 25 }, (_, i) => `[20m] Plot targeting Manchester’s Jewish community… · BBC ${i}`));
    expect(rows).toHaveLength(20);
    for (const row of rows) expect(utf8ByteLength(row)).toBeLessThanOrEqual(63);
  });
});
