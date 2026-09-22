import { utf8ByteLength } from '@evenrealities/even_hub_sdk';

/**
 * Payload limits the glasses enforce on page containers. A payload over any
 * of them is REJECTED WHOLE — `rebuildPageContainer` resolves false and the
 * glass keeps whatever it was showing.
 *
 * Source: Even Realities' evenhub-simulator README, changelog entries written
 * "to behave more like firmware":
 *   v0.7.1 "add text container bytes limit to 999"
 *   v0.7.3 "constraint list item text size to be maximum 63 bytes and 20 items"
 * Observed live (2026-09-22, simulator 0.9.5 log): "RebuildPageContainer
 * validation failed: text content length 1203 exceeds limit of 999 bytes" and
 * "list item text length 67 exceeds limit of 63 bytes" — the News tab card and
 * the expanded News list both silently failed to paint, and every status
 * push re-sent the same rejected page.
 */
export const TEXT_CONTAINER_MAX_BYTES = 999;
export const LIST_ITEM_MAX_BYTES = 63;
export const LIST_MAX_ITEMS = 20;

const ELLIPSIS = '…'; // 3 bytes

/** Cut `text` to at most `maxBytes` of UTF-8 without splitting a code point.
 * Prefers a line boundary when the text is multi-line and one exists past
 * the halfway mark, so a scrolling pane loses whole rows, not half a word.
 * Marks the cut with an ellipsis that is itself inside the budget. */
export function fitUtf8(text: string, maxBytes: number): string {
  if (utf8ByteLength(text) <= maxBytes) return text;
  const budget = Math.max(0, maxBytes - utf8ByteLength(ELLIPSIS));
  let out = '';
  let used = 0;
  for (const ch of text) {
    const n = utf8ByteLength(ch);
    if (used + n > budget) break;
    out += ch;
    used += n;
  }
  if (out.includes('\n')) {
    const lastBreak = out.lastIndexOf('\n');
    if (lastBreak > out.length / 2) out = out.slice(0, lastBreak);
  }
  return out.replace(/\s+$/, '') + ELLIPSIS;
}

/** A firmware list: at most 20 rows of at most 63 bytes each. */
export function fitListItems(items: string[]): string[] {
  return items.slice(0, LIST_MAX_ITEMS).map((s) => fitUtf8(s, LIST_ITEM_MAX_BYTES));
}

/** Like fitUtf8 but keeps the END of the text — for a stream whose newest
 * words are the ones being read. */
export function fitUtf8Tail(text: string, maxBytes: number): string {
  if (utf8ByteLength(text) <= maxBytes) return text;
  const budget = Math.max(0, maxBytes - utf8ByteLength(ELLIPSIS));
  const chars = Array.from(text);
  let used = 0;
  let start = chars.length;
  while (start > 0) {
    const n = utf8ByteLength(chars[start - 1] ?? '');
    if (used + n > budget) break;
    used += n;
    start -= 1;
  }
  return ELLIPSIS + chars.slice(start).join('').replace(/^\s+/, '');
}
