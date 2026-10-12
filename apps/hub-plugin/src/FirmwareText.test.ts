import { utf8ByteLength } from '@evenrealities/even_hub_sdk';
import { getTextWidth, measureTextWrap } from '@evenrealities/pretext';
import { describe, expect, it } from 'vitest';
import { TEXT_CONTAINER_MAX_BYTES } from './FirmwareLimits';
import {
  NATIVE_PAGE_MAX_UNITS,
  containerLineCapacity,
  firmwareLineCount,
  firmwareLineWidth,
  fitLines,
  paginateFromEnd,
  paginateHanging,
  paginateMeasured,
} from './FirmwareText';
import { ASSISTANT_ANSWER_PAGE } from './HudPageBuilder';

/**
 * The firmware wraps a text container itself; what the plugin decides is
 * how much text a page or a container holds, and it decides that with
 * `measureTextWrap`, Even's own model of the LVGL renderer. These pin that
 * every page measures within its lines under that model, not under a
 * re-implementation of it.
 */

const WIDTH = ASSISTANT_ANSWER_PAGE.textWidth; // 568
const LINES = ASSISTANT_ANSWER_PAGE.lineCapacity; // 7

const prose = Array.from({ length: 14 }, (_, index) => (
  `Paragraph ${index + 1} says that the quick brown fox jumps over the lazy dog, then keeps running well past the end of a firmware line.`
)).join('\n');

const words = (text: string) => text.split(/\s+/u).filter((word) => word.length > 0);

describe('paginateMeasured', () => {
  it('fills pages to the measured line capacity and keeps every word in order', () => {
    const pages = paginateMeasured(prose, WIDTH, LINES);
    expect(pages.length).toBeGreaterThan(1);
    for (const page of pages) {
      expect(measureTextWrap(page, WIDTH).lineCount).toBeLessThanOrEqual(LINES);
      expect(page.length).toBeLessThanOrEqual(NATIVE_PAGE_MAX_UNITS);
      expect(page.startsWith('\n')).toBe(false);
      expect(page.endsWith('\n')).toBe(false);
    }
    expect(pages.flatMap(words)).toEqual(words(prose));
    // Nothing was held back: adding the first word of the next page to a
    // page would have needed one line more.
    pages.slice(0, -1).forEach((page, index) => {
      const nextWord = words(pages[index + 1]!)[0]!;
      expect(measureTextWrap(`${page} ${nextWord}`, WIDTH).lineCount).toBeGreaterThan(LINES);
    });
  });

  it('keeps paragraph breaks, folds inner whitespace, and never starts a page on a blank line', () => {
    expect(paginateMeasured('one\n\ntwo   three\r\nfour', WIDTH, LINES)).toEqual(['one\n\ntwo three\nfour']);
    // A page boundary that lands on a paragraph break drops the blank.
    const paragraphs = Array.from({ length: 6 }, (_, index) => `Paragraph ${index + 1}`).join('\n\n');
    const pages = paginateMeasured(paragraphs, WIDTH, 4);
    expect(pages).toEqual([
      'Paragraph 1\n\nParagraph 2\n\n', // trailing blanks trimmed below
      'Paragraph 3\n\nParagraph 4',
      'Paragraph 5\n\nParagraph 6',
    ].map((page) => page.replace(/\n+$/u, '')));
  });

  it('cuts a single word longer than a page at the measured glyph and loses nothing', () => {
    const token = 'x'.repeat(900);
    const pages = paginateMeasured(token, WIDTH, 3);
    expect(pages.length).toBeGreaterThan(1);
    for (const page of pages) expect(measureTextWrap(page, WIDTH).lineCount).toBeLessThanOrEqual(3);
    expect(pages.join('')).toBe(token);
  });

  it('caps a page at the SDK content limit even when the lines would hold more', () => {
    const dots = '.'.repeat(1_500);
    const pages = paginateMeasured(dots, WIDTH, LINES);
    for (const page of pages) expect(page.length).toBeLessThanOrEqual(NATIVE_PAGE_MAX_UNITS);
    expect(pages.join('')).toBe(dots);
  });

  it('gives one blank page for empty text', () => {
    expect(paginateMeasured('', WIDTH, LINES)).toEqual([' ']);
    expect(paginateMeasured('\n\n  \n', WIDTH, LINES)).toEqual([' ']);
  });
});

describe('fitLines', () => {
  it('returns a prompt that fits unchanged, with its whitespace folded', () => {
    expect(fitLines('Send reply to  Adele?', WIDTH, 3)).toBe('Send reply to Adele?');
  });

  it('cuts at a word to the measured line budget and ends in an ellipsis', () => {
    const long = Array.from({ length: 40 }, (_, index) => `word${index + 1}`).join(' ');
    expect(measureTextWrap(long, WIDTH).lineCount).toBeGreaterThan(3);
    const fitted = fitLines(long, WIDTH, 3);
    expect(fitted.endsWith('…')).toBe(true);
    expect(measureTextWrap(fitted, WIDTH).lineCount).toBe(3);
    const kept = fitted.slice(0, -1);
    expect(long.startsWith(kept)).toBe(true);
    expect(kept.endsWith(' ')).toBe(false);
    // One more word would not have fitted.
    const nextWord = words(long.slice(kept.length))[0]!;
    expect(measureTextWrap(`${kept} ${nextWord}…`, WIDTH).lineCount).toBeGreaterThan(3);
  });

  it('measures glyph widths, not characters: wide glyphs get fewer and narrow glyphs more', () => {
    const wide = 'W'.repeat(200);
    const narrow = 'i'.repeat(200);
    expect(fitLines(wide, WIDTH, 1).length).toBeLessThan(fitLines(narrow, WIDTH, 1).length);
    expect(measureTextWrap(fitLines(wide, WIDTH, 1), WIDTH).lineCount).toBe(1);
    expect(measureTextWrap(fitLines(narrow, WIDTH, 1), WIDTH).lineCount).toBe(1);
  });
});

describe('single lines and containers', () => {
  it('counts lines and widths as the firmware draws them', () => {
    expect(firmwareLineCount('', WIDTH)).toBe(0);
    expect(firmwareLineCount('tap: dismiss', WIDTH)).toBe(1);
    expect(firmwareLineCount('W'.repeat(60), WIDTH)).toBe(2);
    // The firmware drops leading spaces at the start of a line; the plain
    // glyph sum does not know that.
    expect(firmwareLineWidth('   indented')).toBeLessThan(getTextWidth('   indented'));
    expect(firmwareLineWidth('indented')).toBe(getTextWidth('indented'));
    expect(firmwareLineWidth('short\nmuch longer line')).toBe(getTextWidth('much longer line'));
  });

  it('derives a container\'s line capacity from its height, padding and border', () => {
    expect(containerLineCapacity(218, 4)).toBe(7);
    expect(containerLineCapacity(35, 4)).toBe(1);
    expect(containerLineCapacity(258, 8, 1)).toBe(8);
    expect(containerLineCapacity(10, 4)).toBe(0);
    expect(ASSISTANT_ANSWER_PAGE.lineCapacity).toBe(containerLineCapacity(ASSISTANT_ANSWER_PAGE.bodyHeight, 4));
  });
});

/**
 * A page is one text container, and the glasses refuse a container over
 * TEXT_CONTAINER_MAX_BYTES of UTF-8. Accented letters, Cyrillic and Greek
 * take two or three bytes a code unit, so a page counted in units alone
 * passed the unit budget and came out over the byte cap: the rebuild then cut
 * its last lines and the next page started after them (2026-10-12).
 */
describe('pages within the glasses\' byte cap', () => {
  const sentence = 'Договорились, позвоню тебе вечером, когда закончу работу. ';
  const thread = `Анна: ${sentence.repeat(12).trim()}\nВы: Договорились, позвоню в девять.`;
  const cyrillic = Array.from({ length: 6 }, () => sentence.repeat(6).trim()).join('\n');

  const underCap = (pages: readonly string[]) => {
    for (const page of pages) expect(utf8ByteLength(page)).toBeLessThanOrEqual(TEXT_CONTAINER_MAX_BYTES);
  };

  it('pages a Messages thread by bytes too, and loses no line', () => {
    const pages = paginateHanging(thread, WIDTH - 10, 40, 900);
    underCap(pages);
    expect(pages.length).toBeGreaterThan(1);
    expect(words(pages.join('\n'))).toEqual(words(thread));
  });

  it('pages measured prose by bytes too, and keeps every word in order', () => {
    const pages = paginateMeasured(cyrillic, WIDTH, 30);
    underCap(pages);
    expect(words(pages.join('\n'))).toEqual(words(cyrillic));
  });

  it('pages live words from the end by bytes too', () => {
    const pages = paginateFromEnd(cyrillic, WIDTH, 30);
    underCap(pages);
    expect(words(pages.join('\n'))).toEqual(words(cyrillic));
  });
});
