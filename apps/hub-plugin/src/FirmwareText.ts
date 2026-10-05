import { measureTextWrap } from '@evenrealities/pretext';

/**
 * Text as the firmware lays it out.
 *
 * Every multi-line native container is measured here with `measureTextWrap`
 * from `@evenrealities/pretext`, the library Even ships for the G2's LVGL
 * renderer: per-glyph advances rounded to pixels one at a time with
 * kerning, breaks at spaces, hyphens and CJK boundaries, leading spaces
 * dropped at the start of a line, a hard break inside a word no break point
 * could rescue. The plugin never re-implements that rule. A page goes to the
 * firmware unwrapped — the firmware breaks its lines — and this module only
 * decides how much text one page or one container can hold, so a line the
 * plugin counted is a line the glass shows.
 */

/** The firmware's fixed text line height. */
export const FIRMWARE_LINE_HEIGHT = 27;

/** Code units one text container takes per page (the SDK's content cap). */
export const NATIVE_PAGE_MAX_UNITS = 1000;

const NEWLINE = '\n';
const ELLIPSIS = '…';

/** Lines the firmware renders `text` on inside a container `innerWidth` wide. */
export function firmwareLineCount(text: string, innerWidth: number): number {
  return text.length === 0 ? 0 : measureTextWrap(text, innerWidth).lineCount;
}

/** Whether the firmware renders `text` on one line at `innerWidth`. */
export function fitsOneLine(text: string, innerWidth: number): boolean {
  return firmwareLineCount(text, innerWidth) <= 1;
}

/** The width the firmware draws `text` at on one line: leading spaces
 * dropped, glyphs rounded one by one. With a newline inside, the widest line. */
export function firmwareLineWidth(text: string): number {
  if (text.length === 0) return 0;
  return Math.max(0, ...measureTextWrap(text, Number.MAX_SAFE_INTEGER).lineWidths);
}

/** Firmware lines a text container holds: its height less padding and
 * border on both sides, 27 px each. */
export function containerLineCapacity(height: number, padding: number, border = 0): number {
  return Math.max(0, Math.floor((height - 2 * (padding + border)) / FIRMWARE_LINE_HEIGHT));
}

/** Words and paragraph breaks; other whitespace only separates words. */
function tokenise(text: string): string[] {
  const tokens: string[] = [];
  text.replace(/\r\n?/gu, NEWLINE).split(NEWLINE).forEach((line, index) => {
    if (index > 0) tokens.push(NEWLINE);
    tokens.push(...line.split(/\s+/u).filter((word) => word.length > 0));
  });
  return tokens;
}

/** Tokens back into text: one space between words, breaks as themselves. */
function join(tokens: readonly string[], start: number, end: number): string {
  let text = '';
  let previous: string | undefined;
  for (let index = start; index < end; index += 1) {
    const token = tokens[index]!;
    if (token === NEWLINE) text += NEWLINE;
    else if (previous !== undefined && previous !== NEWLINE) text += ` ${token}`;
    else text += token;
    previous = token;
  }
  return text;
}

function trimBreaks(text: string): string {
  return text.replace(/\n+$/u, '');
}

/** The largest count in [0, max] for which `fits` holds; `fits` is true for
 * a prefix and false for everything after it. */
function largestFitting(max: number, fits: (count: number) => boolean): number {
  if (max <= 0) return 0;
  if (fits(max)) return max;
  let low = 0;
  let high = max;
  while (high - low > 1) {
    const middle = (low + high) >> 1;
    if (fits(middle)) low = middle;
    else high = middle;
  }
  return low;
}

/** `text` on at most `maxLines` firmware lines at `innerWidth`: whole words,
 * ending in an ellipsis when anything was cut. */
export function fitLines(text: string, innerWidth: number, maxLines: number): string {
  const lines = Math.max(1, maxLines);
  const tokens = tokenise(text);
  const whole = trimBreaks(join(tokens, 0, tokens.length));
  if (firmwareLineCount(whole, innerWidth) <= lines) return whole;
  const fits = (count: number) => (
    firmwareLineCount(`${trimBreaks(join(tokens, 0, count))}${ELLIPSIS}`, innerWidth) <= lines
  );
  const count = largestFitting(tokens.length, fits);
  if (count > 0) return `${trimBreaks(join(tokens, 0, count))}${ELLIPSIS}`;
  // The first word alone overflows: cut it at the measured glyph.
  const points = Array.from(tokens[0] ?? '');
  const pointsFit = (n: number) => (
    firmwareLineCount(`${points.slice(0, n).join('')}${ELLIPSIS}`, innerWidth) <= lines
  );
  return `${points.slice(0, largestFitting(points.length, pointsFit)).join('')}${ELLIPSIS}`;
}

/** The longest head of `word` that fits `maxLines`, and the rest. */
function splitWord(
  word: string, innerWidth: number, maxLines: number, maxUnits: number,
): [string, string] {
  const points = Array.from(word);
  const fits = (count: number) => {
    const head = points.slice(0, count).join('');
    return head.length <= maxUnits && firmwareLineCount(head, innerWidth) <= maxLines;
  };
  const count = Math.max(1, largestFitting(points.length - 1, fits));
  return [points.slice(0, count).join(''), points.slice(count).join('')];
}

/**
 * `text` as firmware pages of at most `maxLines` lines each (and at most
 * `maxUnits` code units, the SDK's per-container cap). Pages break between
 * words or at paragraph breaks, never start or end with a blank line, and
 * keep every word in order; only a single word longer than a whole page is
 * cut, at the glyph the measurement puts last. Empty text is one blank page.
 */
export function paginateMeasured(
  text: string,
  innerWidth: number,
  maxLines: number,
  maxUnits = NATIVE_PAGE_MAX_UNITS,
): string[] {
  const lines = Math.max(1, maxLines);
  const units = Math.max(4, maxUnits);
  const tokens = tokenise(text);
  const pages: string[] = [];
  let start = 0;
  while (start < tokens.length) {
    while (start < tokens.length && tokens[start] === NEWLINE) start += 1;
    if (start >= tokens.length) break;
    const fits = (count: number) => {
      const page = join(tokens, start, start + count);
      return page.length <= units && firmwareLineCount(page, innerWidth) <= lines;
    };
    let count = largestFitting(tokens.length - start, fits);
    if (count === 0) {
      const [head, tail] = splitWord(tokens[start]!, innerWidth, lines, units);
      tokens.splice(start, 1, head, tail);
      count = 1;
    }
    pages.push(trimBreaks(join(tokens, start, start + count)));
    start += count;
  }
  return pages.length > 0 ? pages : [' '];
}
