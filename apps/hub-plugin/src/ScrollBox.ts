import { utf8ByteLength } from '@evenrealities/even_hub_sdk';

/**
 * The glasses' own scrolling, as the one piece every long text uses: a
 * News article, a Search result, a Messages conversation and a saved
 * QuickNote.
 *
 * What the G2 does (Even's display and device-API docs; measured on the
 * wearer's glasses 2026-10-14, project doc
 * claude/news-reader-template-and-sources-2026-10-14.md):
 *  - a capture text container whose text overflows is scrolled by the
 *    firmware itself, a line a swipe, animated, with its own scrollbar;
 *    the plugin hears nothing until the text's top or bottom edge
 *    (SCROLL_TOP_EVENT / SCROLL_BOTTOM_EVENT), so there is no round trip;
 *  - one container took 1,784 bytes (51 lines) through
 *    `textContainerUpgrade` (the simulator holds 999);
 *  - every `textContainerUpgrade` replaces the whole text (contentOffset
 *    and contentLength are ignored), and the view starts again at the top.
 *
 * So a document longer than one box is read as stretches, each as many
 * whole lines as the box holds. At a stretch's bottom edge the next one
 * starts one line below the top line on screen: the next swipe shows the
 * same screen moved up a line, as if the scroll had carried on. Up past a
 * stretch's top goes back a screen (the view always lands at a stretch's
 * top, so a single line up would cost a rewrite a swipe).
 */

/** The most one box was seen to take on the glasses (1,784 bytes). */
export const SCROLL_BOX_MAX_BYTES = 1_784;

const NEWLINE = '\n';

export class ScrollBox {
  private start = 0;
  private end = 0;
  private maxBytes: number;

  /**
   * @param lines the document as the explicit lines the box shows
   *   (FirmwareText.layoutLines), so a stretch never wraps differently
   * @param visible whole lines the box shows at once
   * @param maxBytes the most one stretch may hold
   * @param from the line to open at (a page reopened where it was left)
   */
  constructor(
    private readonly lines: readonly string[],
    private readonly visible: number,
    maxBytes = SCROLL_BOX_MAX_BYTES,
    from = 0,
  ) {
    this.maxBytes = maxBytes;
    this.place(Math.max(0, Math.min(Math.floor(from), Math.max(0, lines.length - 1))));
  }

  /** The first line of the stretch in the box. */
  get position(): number {
    return this.start;
  }

  /** The most one stretch holds. */
  get bytes(): number {
    return this.maxBytes;
  }

  /** The stretch reaches the document's last line. */
  get atEnd(): boolean {
    return this.end >= this.lines.length;
  }

  /** The stretch in the box. */
  text(): string {
    return this.lines.slice(this.start, this.end).join(NEWLINE) || ' ';
  }

  /** The stretch cut to `maxBytes`: the page's first write (a rebuild
   * holds 999 bytes), which the whole stretch then replaces. */
  head(maxBytes: number): string {
    return this.lines.slice(this.start, this.fit(this.start, maxBytes)).join(NEWLINE) || ' ';
  }

  /** The glasses refused a stretch this big (the simulator holds 999
   * bytes): every stretch from here is cut to `maxBytes`. */
  limit(maxBytes: number): void {
    this.maxBytes = Math.min(this.maxBytes, maxBytes);
    this.place(this.start);
  }

  /** The next stretch, one line below the top line on screen. False at
   * the document's end, where there is nothing more to show. */
  down(): boolean {
    if (this.atEnd) return false;
    this.place(this.continuation());
    return true;
  }

  /**
   * Where a box over `lines` opens when they are this document grown
   * longer (older messages loaded below the ones read): a line below the
   * last screen when the wearer had reached this document's end, else the
   * same stretch. The same document reopens where it was. Undefined when
   * `lines` is another document.
   */
  resume(lines: readonly string[]): number | undefined {
    if (lines.length < this.lines.length) return undefined;
    for (let index = 0; index < this.lines.length; index += 1) {
      if (lines[index] !== this.lines[index]) return undefined;
    }
    return lines.length > this.lines.length && this.atEnd ? this.continuation() : this.start;
  }

  /** The stretch a screen above. False at the document's start. */
  up(): boolean {
    if (this.start === 0) return false;
    let previous = Math.max(0, this.start - Math.max(1, this.visible - 1));
    while (previous > 0 && this.lines[previous] === '') previous -= 1;
    this.place(previous);
    return true;
  }

  /** Back to `position` (a stretch the glasses did not take). */
  moveTo(position: number): void {
    this.place(Math.max(0, Math.min(position, Math.max(0, this.lines.length - 1))));
  }

  /** One line below the top line of the stretch's last screen; a screen
   * never opens on the empty line between paragraphs. */
  private continuation(): number {
    let next = Math.max(this.start + 1, this.end - this.visible + 1);
    while (this.lines[next] === '' && next < this.end) next += 1;
    return next;
  }

  private place(start: number): void {
    this.start = start;
    this.end = this.fit(start, this.maxBytes);
  }

  /** The end of the longest run of whole lines from `start` within
   * `maxBytes` (one line at least, however long). */
  private fit(start: number, maxBytes: number): number {
    let end = start;
    let bytes = 0;
    while (end < this.lines.length) {
      const add = utf8ByteLength(this.lines[end] ?? '') + (end > start ? 1 : 0);
      if (end > start && bytes + add > maxBytes) break;
      bytes += add;
      end += 1;
    }
    return end;
  }
}
