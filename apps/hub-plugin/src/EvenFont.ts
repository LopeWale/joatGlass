import { getTextWidth } from '@evenrealities/pretext';
import atlasUrl from './assets/even-font.png?url';
import atlasData from './assets/even-font.json';

/**
 * Even's own letterforms, drawn smaller than the firmware will draw them.
 *
 * The firmware gives a plugin one font at one size (a 27 px line: seven
 * lines of body on a page), and no SDK through 0.0.16 offers a size. The
 * wearer asked for that font smaller (55%) across the whole 576x288, the way the
 * Planning month already fills it. The glyphs were cut from the official
 * simulator (scripts/build_even_font.py) at exactly the positions pretext
 * gives them, so a line set here measures as `getTextWidth × scale`: the
 * same advances and kerning as the firmware, scaled.
 *
 * The layout is responsive, element by element:
 *   - prose (a message, an article) wraps at word boundaries and is never
 *     cut; what does not fit the page continues on the next one;
 *   - a single token wider than the whole line (a long link) is the only
 *     thing broken mid-word, after `/ . - _ ?` where it can be;
 *   - a one-line slot (a title, the hint) is shortened with "…", keeping
 *     the part that must stay whole (the page count).
 */

/** The wearer's choice of the four scales shown (2026-10-06): first 66%,
 * then the smallest row, 55% ("the fonts are still too big"). */
export const EVEN_FONT_SCALE = 0.55;
/** The firmware's line, which the atlas cells keep. */
export const EVEN_FONT_LINE = 27;
const ELLIPSIS = '…';

interface EvenFontAtlasData {
  lineHeight: number;
  pad: number;
  /** codepoint -> [x, y, cell width, advance] */
  glyphs: Record<string, [number, number, number, number]>;
}

const ATLAS = atlasData as unknown as EvenFontAtlasData;

/** Whether the atlas carries this character's glyph. */
export function hasEvenGlyph(char: string): boolean {
  const point = char.codePointAt(0);
  return point !== undefined && ATLAS.glyphs[String(point)] !== undefined;
}

/** Extra space after each word. The firmware's space shrinks with the
 * glyphs to about 3 px at 55%, which runs words together on the glass. */
export const EVEN_WORD_SPACE = 1.5;

/** The width of one line of text in Even's font at `scale`: pretext's
 * advances scaled, plus the word spacing. Drawing places each glyph at its
 * prefix's width, so measuring and drawing always agree. */
export function evenTextWidth(text: string, scale = EVEN_FONT_SCALE): number {
  if (text.length === 0) return 0;
  const spaces = text.match(/ /gu)?.length ?? 0;
  return getTextWidth(text) * scale + spaces * EVEN_WORD_SPACE;
}

/** Where to break a token wider than the whole line: after a URL or path
 * separator that still fits, otherwise at the last character that fits. */
function breakToken(token: string, maxWidth: number, scale: number): [string, string] {
  const chars = Array.from(token);
  let fit = 0;
  while (fit < chars.length && evenTextWidth(chars.slice(0, fit + 1).join(''), scale) <= maxWidth) fit += 1;
  fit = Math.max(1, fit);
  for (let at = fit; at > Math.floor(fit / 2); at -= 1) {
    if ('/.-_?&=#'.includes(chars[at - 1] ?? '')) { fit = at; break; }
  }
  return [chars.slice(0, fit).join(''), chars.slice(fit).join('')];
}

/** Prose wrapped at word boundaries to `maxWidth`. Nothing is dropped:
 * only a token wider than the whole line is broken inside itself. */
export function wrapEven(text: string, maxWidth: number, scale = EVEN_FONT_SCALE): string[] {
  const words = text.replace(/\s+/gu, ' ').trim().split(' ').filter((word) => word.length > 0);
  const lines: string[] = [];
  let line = '';
  for (let word of words) {
    const joined = line ? `${line} ${word}` : word;
    if (evenTextWidth(joined, scale) <= maxWidth) {
      line = joined;
      continue;
    }
    if (line) lines.push(line);
    line = '';
    while (evenTextWidth(word, scale) > maxWidth) {
      const [head, rest] = breakToken(word, maxWidth, scale);
      lines.push(head);
      word = rest;
    }
    line = word;
  }
  if (line) lines.push(line);
  return lines;
}

/** A one-line slot: the text if it fits, otherwise shortened with "…".
 * `keep` (a page count, a time) is appended whole and never shortened. */
export function fitOneLineEven(
  text: string, maxWidth: number, scale = EVEN_FONT_SCALE, keep = '',
): string {
  const full = `${text}${keep}`;
  if (evenTextWidth(full, scale) <= maxWidth) return full;
  const chars = Array.from(text.trimEnd());
  let low = 0;
  let high = chars.length;
  while (low < high) {
    const mid = Math.ceil((low + high) / 2);
    const candidate = `${chars.slice(0, mid).join('').trimEnd()}${ELLIPSIS}${keep}`;
    if (evenTextWidth(candidate, scale) <= maxWidth) low = mid; else high = mid - 1;
  }
  return low > 0 ? `${chars.slice(0, low).join('').trimEnd()}${ELLIPSIS}${keep}` : keep;
}

/** One line of a reader page. `speaker` is the length of a "Name:" label
 * that opens a message, drawn brighter than the words after it. */
export interface EvenReaderLine {
  text: string;
  /** A message or paragraph starts on this line: a gap opens above it. */
  opens: boolean;
  speaker?: number;
}

/** services/reader.py `_ROW_COLUMNS`: the Mac's row budget. */
const READER_ROW_COLUMNS = 62;

/** A row's width in the Mac's columns: wide and astral characters cost
 * two, tabs eight, everything else one (services/reader.py `_columns`). */
function readerColumns(row: string): number {
  let columns = 0;
  for (const char of row) {
    const point = char.codePointAt(0) ?? 0;
    if (char === '\t') columns += 8;
    else if (point > 0xffff || (point >= 0x1100 && point <= 0x115f) || (point >= 0x2e80 && point <= 0xa4cf)
      || (point >= 0xac00 && point <= 0xd7a3) || (point >= 0xf900 && point <= 0xfaff)
      || (point >= 0xfe30 && point <= 0xfe4f) || (point >= 0xff00 && point <= 0xff60)
      || (point >= 0xffe0 && point <= 0xffe6)) columns += 2;
    else columns += 1;
  }
  return columns;
}

/**
 * The Mac's reader rows as paragraphs. The Mac pages every reader with
 * `paginate_reader_text` (services/reader.py): lossless slices of the
 * source, 62 mono columns each, where a row ending in a line break ends a
 * paragraph and a row ending in the space it was wrapped at continues
 * into the next. The smaller font holds far more than 62 columns, so the
 * slices are joined back and set again at the new width. A row with no
 * break and no trailing space (a context line, a hand-written row) stands
 * as its own paragraph.
 */
export function readerParagraphs(rows: readonly string[]): string[] {
  const paragraphs: string[] = [];
  let open = '';
  for (const row of rows) {
    open += row;
    // A row continues when it was wrapped at a space, or cut mid-token at
    // the Mac's full 62 columns (a long link).
    const continues = / $/u.test(row) || readerColumns(row) >= READER_ROW_COLUMNS - 2;
    if (/[\r\n]$/u.test(row) || !continues) {
      paragraphs.push(open.replace(/[\r\n]+$/u, ''));
      open = '';
    }
  }
  if (open.trim()) paragraphs.push(open);
  return paragraphs.flatMap((paragraph) => paragraph.split(/\r\n|\r|\n/u));
}

const SPEAKER = /^([^:\s][^:]{0,31}): \S/u;

/** The geometry of a reader page (576x288, the wearer's margins:
 * nothing within 16 px of a side, 3 px of the top or 8 px of the bottom;
 * the text starts at 18, since a glyph's ink may reach a pixel or two
 * left of its origin). */
export const EVEN_READER = {
  marginX: 18,
  width: 540,
  bodyTop: 30,
  bodyBottom: 258,
  /** The 55% glyph line is 15 px; 3 px of leading keeps lines apart. */
  pitch: 18,
  /** Between messages, so they never run together. */
  gap: 5,
  hintTop: 262,
  hintBottom: 280,
} as const;

/** The glyph line drawn at `scale`. */
export function evenLineHeight(scale = EVEN_FONT_SCALE): number {
  return EVEN_FONT_LINE * scale;
}

/** Each line's top on the page: one pitch per line, a gap above each
 * message after the first. */
export function readerLineTops(lines: readonly EvenReaderLine[]): number[] {
  const tops: number[] = [];
  let y: number = EVEN_READER.bodyTop;
  lines.forEach((line, index) => {
    if (index > 0 && line.opens) y += EVEN_READER.gap;
    tops.push(y);
    y += EVEN_READER.pitch;
  });
  return tops;
}

/** Whether a set of lines fits the body band. */
function fitsBody(lines: readonly EvenReaderLine[]): boolean {
  const tops = readerLineTops(lines);
  const last = tops[tops.length - 1];
  return last === undefined || last + evenLineHeight() <= EVEN_READER.bodyBottom;
}

/** The reader's rows set in Even's font and cut into pages that fill the
 * body band. A message that does not fit continues on the next page. */
export function paginateEven(rows: readonly string[], speakers = false): EvenReaderLine[][] {
  const lines: EvenReaderLine[] = [];
  for (const paragraph of readerParagraphs(rows)) {
    const wrapped = wrapEven(paragraph, EVEN_READER.width);
    if (wrapped.length === 0) continue;
    const label = speakers ? SPEAKER.exec(paragraph)?.[1] : undefined;
    wrapped.forEach((text, index) => lines.push({
      text, opens: index === 0,
      ...(index === 0 && label && text.startsWith(`${label}:`) ? { speaker: label.length + 1 } : {}),
    }));
  }
  const pages: EvenReaderLine[][] = [];
  let page: EvenReaderLine[] = [];
  for (const line of lines) {
    if (page.length > 0 && !fitsBody([...page, line])) {
      pages.push(page);
      page = [];
    }
    page.push(line);
  }
  if (page.length > 0) pages.push(page);
  return pages.length > 0 ? pages : [[{ text: ' ', opens: true }]];
}

/** The hint row: the gestures on the left, the page count whole on the
 * right. Gesture clauses are dropped from the swipe first (swiping is the
 * one gesture a page always offers), then the rest is shortened. */
export function readerHint(hint: string): { left: string; right: string } {
  const clauses = hint.split(' · ').map((clause) => clause.trim()).filter(Boolean);
  const count = /^\d+\/\d+\+?$/u.test(clauses[0] ?? '') ? clauses.shift()! : '';
  const room = EVEN_READER.width - (count ? evenTextWidth(count) + 16 : 0);
  let rest = clauses;
  if (evenTextWidth(rest.join(' · ')) > room) rest = rest.filter((clause) => !clause.startsWith('swipe:'));
  return { left: fitOneLineEven(rest.join(' · '), room), right: count };
}

/** The atlas as an image, loaded once on first draw. */
let atlasImage: Promise<HTMLImageElement | undefined> | undefined;

export function loadEvenFont(): Promise<HTMLImageElement | undefined> {
  atlasImage ??= new Promise((resolve) => {
    if (typeof Image === 'undefined') { resolve(undefined); return; }
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => resolve(undefined);
    image.src = atlasUrl;
  });
  return atlasImage;
}

/**
 * Draw one line with its cell top at `y`. Each glyph lands where pretext
 * puts it (the prefix's width), so kerning matches the firmware. A
 * character the atlas lacks is drawn by the canvas in the prose face at
 * the same size, so nothing goes missing.
 */
export function drawEvenText(
  context: CanvasRenderingContext2D,
  atlas: HTMLImageElement | undefined,
  text: string,
  x: number,
  y: number,
  options: { scale?: number; alpha?: number; fallbackFont: string },
): void {
  const scale = options.scale ?? EVEN_FONT_SCALE;
  const chars = Array.from(text);
  context.save();
  context.globalAlpha = options.alpha ?? 1;
  let prefix = '';
  for (const char of chars) {
    const left = x + evenTextWidth(prefix, scale);
    prefix += char;
    if (/\s/u.test(char)) continue;
    const cell = ATLAS.glyphs[String(char.codePointAt(0))];
    if (cell && atlas) {
      const [ax, ay, width] = cell;
      context.drawImage(
        atlas, ax, ay, width, ATLAS.lineHeight,
        left - ATLAS.pad * scale, y, width * scale, ATLAS.lineHeight * scale,
      );
    } else {
      context.fillStyle = '#ffffff';
      context.font = options.fallbackFont;
      context.textBaseline = 'alphabetic';
      context.fillText(char, left, y + 0.72 * ATLAS.lineHeight * scale);
    }
  }
  context.restore();
}
