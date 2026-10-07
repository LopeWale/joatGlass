import { describe, expect, it } from 'vitest';
import { getTextWidth } from '@evenrealities/pretext';
import {
  GLYPH_COVERAGE_CUT, boxCoverage,
  EVEN_FONT_SCALE, EVEN_READER, EVEN_WORD_SPACE, evenLineHeight, evenTextWidth, fitOneLineEven,
  paginateEven, readerHint, readerLineTops, readerParagraphs, wrapEven,
} from './EvenFont';
import { readerLayout } from './HudCanvasRenderer';

/** A thread page exactly as the Mac sends it: paginate_reader_text's
 * 62-column slices (services/reader.py), a link cut mid-token included. */
const MAC_THREAD = [
  '1/2 · Adele',
  'Adele: Can you send the latest mockups before the review? I ',
  'want to walk Marcus through the onboarding flow first.\n',
  'You: Both palettes are in the file. Link: ',
  'https://www.figma.com/design/k3J9xQ2/joat-hud-review?node-id=1',
  '204-5521&t=Ab12\n',
  'Adele: You are a lifesaver.',
];

const LINK = 'https://www.figma.com/design/k3J9xQ2/joat-hud-review?node-id=1204-5521&t=Ab12';

describe('Even font measurement', () => {
  it('measures as the firmware advances at 66% plus the word spacing', () => {
    expect(evenTextWidth('Hello')).toBeCloseTo(getTextWidth('Hello') * EVEN_FONT_SCALE);
    expect(evenTextWidth('Hello there you')).toBeCloseTo(
      getTextWidth('Hello there you') * EVEN_FONT_SCALE + 2 * EVEN_WORD_SPACE,
    );
    expect(evenTextWidth('')).toBe(0);
  });
});

describe('responsive layout', () => {
  it('wraps prose at word boundaries, never wider than the line, losing nothing', () => {
    const prose = 'Can you send the latest mockups before the review? I want to walk Marcus '
      + 'through the onboarding flow first, and then the empty states for the inbox screen.';
    const lines = wrapEven(prose, 300);
    expect(lines.length).toBeGreaterThan(1);
    for (const line of lines) expect(evenTextWidth(line)).toBeLessThanOrEqual(300);
    expect(lines.join(' ')).toBe(prose);
  });

  it('breaks only a token wider than the whole line, after a separator, keeping every character', () => {
    const lines = wrapEven(`Link: ${LINK}`, 200);
    for (const line of lines) expect(evenTextWidth(line)).toBeLessThanOrEqual(200);
    expect(lines[0]).toBe('Link:');
    const pieces = lines.slice(1);
    expect(pieces.join('')).toBe(LINK);
    expect(pieces.slice(0, -1).every((piece) => /[/.\-_?&=#]$/u.test(piece))).toBe(true);
  });

  it('shortens a one-line slot with an ellipsis and keeps its suffix whole', () => {
    const fitted = fitOneLineEven('A very long title that cannot possibly fit in the space', 120, EVEN_FONT_SCALE, ' 12:40');
    expect(fitted.endsWith('… 12:40')).toBe(true);
    expect(evenTextWidth(fitted)).toBeLessThanOrEqual(120);
    expect(fitOneLineEven('Short', 120)).toBe('Short');
  });

  it("joins the Mac's 62-column slices back into messages", () => {
    expect(readerParagraphs(MAC_THREAD)).toEqual([
      '1/2 · Adele',
      'Adele: Can you send the latest mockups before the review? I want to walk Marcus through the onboarding flow first.',
      `You: Both palettes are in the file. Link: ${LINK}`,
      'Adele: You are a lifesaver.',
    ]);
  });

  it('fills pages to the body band and carries a message on rather than cutting it', () => {
    const rows = Array.from({ length: 12 }, (_, index) => (
      `Speaker ${index}: ${'word '.repeat(30 + index).trim()}\n`
    ));
    const pages = paginateEven(rows, true);
    expect(pages.length).toBeGreaterThan(1);
    for (const page of pages) {
      const tops = readerLineTops(page);
      expect(tops[tops.length - 1]! + evenLineHeight()).toBeLessThanOrEqual(EVEN_READER.bodyBottom);
      for (const line of page) expect(evenTextWidth(line.text)).toBeLessThanOrEqual(EVEN_READER.width);
    }
    const all = pages.flat();
    // Every word arrives, in order: nothing was dropped at a page edge.
    expect(all.map((line) => line.text).join(' ').split(' ').filter((w) => w === 'word'))
      .toHaveLength(rows.reduce((sum, _, index) => sum + 30 + index, 0));
    // Some page opens mid-message: a continuation, not a cut.
    expect(pages.slice(1).some((page) => page[0]?.opens === false)).toBe(true);
    expect(all.filter((line) => line.speaker !== undefined)).toHaveLength(12);
  });

  it('marks the speaker only on Messages pages', () => {
    expect(paginateEven(['Adele: hello\n'], true)[0]?.[0]?.speaker).toBe('Adele:'.length);
    expect(paginateEven(['Note: hello\n'], false)[0]?.[0]?.speaker).toBeUndefined();
  });

  it('keeps the page count whole and drops the swipe clause before shortening the hint', () => {
    expect(readerHint('2/3 · swipe: page · tap: reply · double-click: back'))
      .toEqual({ left: 'swipe: page · tap: reply · double-click: back', right: '2/3' });
    const long = readerHint(`2/3 · swipe: page · tap: reply · ${'double-click: back to the conversation list '.repeat(4)}`);
    expect(long.right).toBe('2/3');
    expect(long.left.startsWith('tap: reply')).toBe(true);
    expect(long.left.endsWith('…')).toBe(true);
  });
});

describe('reader page margins', () => {
  it('draws nothing within 16 px of a side, 3 px of the top or 8 px of the bottom', () => {
    const stress = [
      `${'Bartholomew-Featherstonehaugh'.repeat(3)}: ${'a long message that keeps going '.repeat(12)}\n`,
      `You: ${LINK}${LINK}\n`,
      'x'.repeat(200),
    ];
    const hints = [
      '1/9 · swipe: page · tap: reply · double-click: back',
      `12/40 · ${'tap: do the long thing · '.repeat(10)}double-click: back`,
    ];
    for (const page of [...paginateEven(MAC_THREAD, true), ...paginateEven(stress, true)]) {
      for (const hint of hints) {
        for (const run of readerLayout(page, hint)) {
          expect(run.x).toBeGreaterThanOrEqual(16);
          expect(run.x + run.width).toBeLessThanOrEqual(560);
          expect(run.y).toBeGreaterThanOrEqual(3);
          expect(run.y + evenLineHeight()).toBeLessThanOrEqual(280);
        }
      }
    }
  });
});

describe('crisp glyph coverage', () => {
  /** A cell `width` px wide, one lit column at `column`, `height` rows. */
  const stroke = (width: number, height: number, column: number) => ({
    width,
    alpha: Array.from({ length: width * height }, (_, index) => (index % width === column ? 255 : 0)),
  });
  const CUT = GLYPH_COVERAGE_CUT;

  it('averages exactly the area under each pixel', () => {
    // A 2x1 cell, its left pixel lit, scaled to one pixel: half covered.
    expect(boxCoverage({ width: 2, alpha: [255, 0] }, 0, 0, 2, 1, 1, 1)).toEqual([0.5]);
    // Unscaled: unchanged.
    expect(boxCoverage({ width: 2, alpha: [255, 0] }, 0, 0, 2, 1, 2, 1)).toEqual([1, 0]);
  });

  it('never loses a 1 px stroke at the wearer\'s scale, wherever it falls', () => {
    // Even's strokes are 1 px. Scaled by s, a stroke split evenly over two
    // pixels gives each a share of s / 2: 0.33 at 66%, above the cut.
    expect(EVEN_FONT_SCALE / 2).toBeGreaterThanOrEqual(CUT);
    const width = 20;
    const scaled = Math.round(width * EVEN_FONT_SCALE);
    for (let column = 0; column < width; column += 1) {
      const shares = boxCoverage(stroke(width, 1, column), 0, 0, width, 1, scaled, 1);
      expect(Math.max(...shares)).toBeGreaterThanOrEqual(CUT);
    }
  });
});

describe('reader paragraphs', () => {
  it('never glues a row wider than the Mac\'s 62 columns to the next one', () => {
    const rows = [
      '1/2 · Fixture Wire',
      'The simulator build of the plugin now draws every reader on a firmware',
      'text page. The article body is the Mac\'s own paged rows.',
    ];
    expect(readerParagraphs(rows)).toEqual(rows);
  });

  it('still joins the Mac\'s slices: a wrap at a space, and a link cut at 62 columns', () => {
    const link = 'https://www.figma.com/design/k3J9xQ2/joat-hud-review?node-id=1';
    expect(link.length).toBe(62);
    expect(readerParagraphs(['Adele: Can you send the mockups? I ', 'want them first.\n', link, '204-5521\n']))
      .toEqual(['Adele: Can you send the mockups? I want them first.', `${link}204-5521`]);
  });
});
