import { measureTextWrap } from '@evenrealities/pretext';

/**
 * Page a reply for the firmware's own text containers.
 *
 * This is Even Realities' pagination from the official `text-heavy`
 * template (even-realities/evenhub-templates), kept faithful on purpose:
 * it measures with `@evenrealities/pretext`, which replicates the LVGL
 * shaping the G2 firmware actually uses, so a page fills its container
 * without clipping and without leaving a dead band at the bottom.
 *
 * The reason this exists at all: JOAT drew replies by rasterising them
 * into four image tiles. A measured frame on that path costs ~400ms, of
 * which ~330ms is the plugin's own TILE_CALL_GAP_MS pacing, before BLE.
 * A reply is text and nothing else, so the whole raster bill buys nothing.
 * Paged this way a turn is two `textContainerUpgrade` calls — in place,
 * flicker-free, no image pacing floor.
 */

/** LVGL line height in EvenHub's build. Same constant as HudPageBuilder's
 * FIRMWARE_LINE; duplicated here so the pager stays independent of layout. */
const LINE_HEIGHT = 27;

export interface PaginateBox {
  /** INNER width: container width minus padding and border. */
  width: number;
  /** INNER height: container height minus padding and border. */
  height: number;
}

export function paginate(source: string, box: PaginateBox): string[] {
  const maxLines = Math.max(1, Math.floor(box.height / LINE_HEIGHT));
  const paragraphs = source.split(/\n{2,}/).map((p) => p.trim()).filter(Boolean);

  const pages: string[] = [];
  let buffer: string[] = [];
  let bufferLines = 0;

  const flush = (): void => {
    if (buffer.length === 0) return;
    pages.push(buffer.join('\n\n'));
    buffer = [];
    bufferLines = 0;
  };

  for (const para of paragraphs) {
    const paraLines = measureTextWrap(para, box.width).lineCount;
    if (paraLines > maxLines) {
      flush();
      for (const chunk of splitParagraph(para, box.width, maxLines)) pages.push(chunk);
      continue;
    }
    // +1 line for the blank between two paragraphs sharing a page.
    const cost = paraLines + (buffer.length > 0 ? 1 : 0);
    if (bufferLines + cost > maxLines) {
      flush();
      buffer.push(para);
      bufferLines = paraLines;
    } else {
      buffer.push(para);
      bufferLines += cost;
    }
  }
  flush();
  // An empty answer still owns a page: the container must be written with
  // something, and a blank body is a truer report than the previous page.
  return pages.length > 0 ? pages : [''];
}

function splitParagraph(text: string, width: number, maxLines: number): string[] {
  const tokens = text.split(/(\s+)/);
  const chunks: string[] = [];
  let current = '';
  for (const token of tokens) {
    const candidate = current + token;
    if (measureTextWrap(candidate, width).lineCount > maxLines && current.trim()) {
      chunks.push(current.trim());
      current = token.replace(/^\s+/, '');
    } else {
      current = candidate;
    }
  }
  if (current.trim()) chunks.push(current.trim());
  return chunks;
}

/** Footer copy. One page says only how to leave; several say where you are
 * and that the swipe does something, because a reader who cannot see a
 * scrollbar has no other way to learn there is more. */
export function pagerLabel(index: number, total: number): string {
  return total <= 1
    ? 'tap: dismiss'
    : `${index + 1} / ${total}  ·  up / down: read  ·  tap: dismiss`;
}
