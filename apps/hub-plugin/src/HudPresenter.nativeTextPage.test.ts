import { describe, expect, it, vi } from 'vitest';
import { utf8ByteLength, type RebuildPageContainer, type TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import { TEXT_CONTAINER_MAX_BYTES } from './FirmwareLimits';
import type { BridgeClient } from './BridgeClient';
import { HudPageBuilder, PROMPT_QUESTION_HINT, READER_TEXT_PAGE } from './HudPageBuilder';
import { HudPresenter, SCROLL_BOX_LINES, setNativeOverviewForTest } from './HudPresenter';
import { SCROLL_BOX_MAX_BYTES } from './ScrollBox';
import { firmwareLineCount, paginateHanging, paginateMeasured } from './FirmwareText';
import { getTextWidth } from '@evenrealities/pretext';
import { ContainerId } from './types';
import type { HudVisualFrame } from './HudVisualSurface';
import { readerParagraphs, readerRows } from './EvenFont';

/** A Messages reader's firmware pages: explicit lines at the inset reader
 * width, each message's rows hanging under its first word. */
// A Messages page scrolls on the glass (2026-10-11): it is cut only by the
// container's byte cap (900 units), never into seven-line pages.
const readerPages = (rows: readonly string[]) => paginateHanging(
  readerParagraphs(rows).join('\n'), READER_TEXT_PAGE.textWidth - 10, 40, 900, // 10: the firmware scrollbar
);

// These tests pin the IMAGE overview (VITE_NATIVE_OVERVIEW=0). The text
// overview, which is Home by default, has its own: HudPresenter.textOverview.test.ts.
setNativeOverviewForTest(false);

/**
 * A Mac-paged reader (the Messages message) and the application question
 * ("What should I say?") over the visual HUD are Even's own text pages, not
 * four forced image tiles. These cases pin the one-rebuild entry, in-place
 * paging, the hand-back to the Mac at a local edge, and the return to the
 * retained feature on dismissal.
 */
function rig(accepts: (update: TextContainerUpgrade) => boolean = () => true) {
  const operations: string[] = [];
  const rebuilds: RebuildPageContainer[] = [];
  const upgrades: TextContainerUpgrade[] = [];
  const client = {
    require: () => ({
      rebuildPageContainer: vi.fn(async (page: RebuildPageContainer) => {
        operations.push(`rebuild:${page.imageObject?.length ?? 0}:${page.textObject?.length ?? 0}:${page.listObject?.length ?? 0}`);
        rebuilds.push(page);
        return true;
      }),
      textContainerUpgrade: vi.fn(async (update: TextContainerUpgrade) => {
        operations.push(`upgrade:${update.containerName ?? update.containerID}`);
        upgrades.push(update);
        return accepts(update);
      }),
    }),
  } as unknown as BridgeClient;
  const frames: HudVisualFrame[] = [];
  const visual = {
    paint: vi.fn(async (frame: HudVisualFrame) => {
      operations.push(`visual:${frame.kind}`);
      frames.push(frame);
      return true;
    }),
    paintExact: vi.fn(async (frame: HudVisualFrame, current: () => boolean) => {
      if (!current()) return false;
      operations.push(`exact:${frame.kind}`);
      frames.push(frame);
      return current();
    }),
    forgetPage: vi.fn(() => operations.push('forget')),
    whenIdle: vi.fn(async () => true),
    withPageTransition: vi.fn(async (replace: () => Promise<boolean>) => replace()),
    // The painter could draw a reader as an image; readers still are not.
    canPaintReader: () => true,
  };
  const hud = new HudPresenter(client, new HudPageBuilder(), visual);
  return { hud, operations, rebuilds, upgrades, frames, visual };
}

function text(page: RebuildPageContainer | undefined, name: string): string | undefined {
  return page?.textObject?.find((container) => container.containerName === name)?.content;
}

// Long enough to pass the scrolling page's byte cap, so it still splits
// into local pages: the fallback for a Mac page the container cannot hold.
const longMessage = Array.from({ length: 24 }, (_, index) => (
  `Row ${index + 1} of a long message that the Mac cut into sixty-two column rows.`
));
const READER = { title: 'MESSAGES · MESSAGE', hint: 'swipe: more · say reply · 2x tap: back', selectable: false, nativeTextPage: true };

describe('HudPresenter native text page for a Mac-paged reader', () => {
  it('shows a Messages thread as firmware text, the Mac\'s slices joined back into sentences', async () => {
    const r = rig();
    expect(await r.hud.showList([
      '1/1 · Adele',
      'Adele: Can you send the latest mockups before the review? I ',
      'want to walk Marcus through the onboarding flow first.\n',
      'You: Exporting them now.\n',
    ], {
      title: 'MESSAGES · CONVERSATION', hint: 'tap: reply · 2x tap: back', selectable: true, nativeTextPage: true,
    })).toBe(true);
    expect(r.frames.some((frame) => frame.kind === 'reader')).toBe(false);
    const page = r.rebuilds[r.rebuilds.length - 1];
    expect(page?.imageObject?.some((image) => image.containerID === ContainerId.Image) ?? false).toBe(false);
    const body = text(page, 'main') ?? '';
    const lines = body.split('\n');
    // One sentence, not a 62-column slice per line: set at the page's width
    // with the sender's name in front, its wrapped rows standing under the
    // first row's words.
    expect(lines[0]).toBe('1/1 · Adele');
    expect(lines[1]).toMatch(/^Adele: Can you send the latest mockups/u);
    expect(lines[2]).toMatch(/^ +\S/u);
    expect(lines[lines.length - 1]).toBe('You: Exporting them now.');
    const message = lines.slice(1, -1).map((line) => line.trim()).join(' ');
    expect(message).toBe('Adele: Can you send the latest mockups before the review? I want to walk Marcus through the onboarding flow first.');
    // The wrapped row starts where the words after "Adele: " start.
    const lead = getTextWidth(/^ */u.exec(lines[2]!)![0]);
    expect(Math.abs(lead - getTextWidth('Adele: '))).toBeLessThanOrEqual(getTextWidth(' ') / 2);
  });

  it('shows the reader as one text page with the live status and no image tiles', async () => {
    const r = rig();
    await r.hud.setStatusModel({ clock: '12:34', link: 'connected', mic: false, battery: 80, charging: false });
    await r.hud.showText('home card', ContainerId.Main);
    const framesBefore = r.frames.length;

    expect(await r.hud.showList(['1/1 · From Dele', 'Call me when you arrive.'], READER)).toBe(true);

    const page = r.rebuilds[r.rebuilds.length - 1];
    expect(page?.imageObject ?? []).toHaveLength(0);
    expect(page?.listObject ?? []).toHaveLength(0);
    expect(page?.textObject?.map((c) => c.containerName)).toEqual(['status', 'main', 'hint']);
    expect(text(page, 'main')).toBe('1/1 · From Dele\nCall me when you arrive.');
    // The header names the page: clock · page · mic glyph · battery glyph,
    // one line composed from the status model, instead of a status line in
    // words plus a second line for the title.
    expect(text(page, 'status')).toBe('12:34 · MESSAGES · MESSAGE · ○ · ▆');
    expect(text(page, 'hint')).toBe(READER.hint);
    expect(r.frames).toHaveLength(framesBefore);
  });

  it('pages a long Mac page in place and hands the edge swipe back to the caller', async () => {
    const r = rig();
    expect(await r.hud.showList(longMessage, READER)).toBe(true);
    const pages = readerPages(longMessage);
    expect(pages.length).toBeGreaterThan(1);
    const first = r.rebuilds[r.rebuilds.length - 1];
    expect(text(first, 'main')).toBe(pages[0]);
    expect(text(first, 'hint')).toBe(`1/${pages.length} · ${READER.hint}`);
    const rebuildsAfterOpen = r.rebuilds.length;

    expect(r.hud.scrollTextPage('up')).toBe(false);
    expect(r.hud.scrollTextPage('down')).toBe(true);
    await vi.waitFor(() => expect(r.upgrades.some((u) => u.containerName === 'main' && u.content === pages[1])).toBe(true));
    // From the second local page the swipe goes both ways.
    await vi.waitFor(() => expect(r.upgrades.some((u) => (
      u.containerName === 'hint' && u.content === `2/${pages.length} · swipe: page · say reply · 2x tap: back`
    ))).toBe(true));
    expect(r.rebuilds).toHaveLength(rebuildsAfterOpen);

    for (let page = 1; page < pages.length - 1; page += 1) expect(r.hud.scrollTextPage('down')).toBe(true);
    // Past the last local page the Mac decides, so the swipe is not consumed.
    expect(r.hud.scrollTextPage('down')).toBe(false);
    expect(r.rebuilds).toHaveLength(rebuildsAfterOpen);
  });

  it('opens the Mac\'s previous page on its last local page when asked', async () => {
    const r = rig();
    const pages = readerPages(longMessage);
    expect(await r.hud.showList(longMessage, { ...READER, nativeTextPageFromEnd: true })).toBe(true);
    const page = r.rebuilds[r.rebuilds.length - 1];
    expect(text(page, 'main')).toBe(pages[pages.length - 1]);
    // Earlier local pages and the Mac's next page: the swipe goes both ways.
    expect(text(page, 'hint')).toBe(`${pages.length}/${pages.length} · swipe: page · say reply · 2x tap: back`);
    expect(r.hud.scrollTextPage('down')).toBe(false);
    expect(r.hud.scrollTextPage('up')).toBe(true);
  });

  it('offers a review\'s tap only on its last local page, and promises the swipe for both layers', async () => {
    const r = rig();
    const pages = readerPages(longMessage);
    expect(pages.length).toBeGreaterThan(1);
    // The Mac's last page of a review: it offers the tap and a previous page.
    const REVIEW = {
      title: 'MESSAGES · REVIEW', hint: 'up previous · tap: confirm · 2x tap: back',
      selectable: true, nativeTextPage: true, reviewTapOnLastPage: true,
    };
    expect(await r.hud.showList(longMessage, REVIEW)).toBe(true);
    expect(text(r.rebuilds[r.rebuilds.length - 1], 'hint'))
      .toBe(`1/${pages.length} · swipe: page · 2x tap: back`);
    expect(r.hud.isTextPageAtEnd()).toBe(false);

    for (let page = 1; page < pages.length; page += 1) expect(r.hud.scrollTextPage('down')).toBe(true);
    expect(r.hud.isTextPageAtEnd()).toBe(true);
    await vi.waitFor(() => expect(r.upgrades.some((u) => u.containerName === 'hint'
      && u.content === `${pages.length}/${pages.length} · tap: confirm · swipe: previous · 2x tap: back`)).toBe(true));
  });

  it('puts a Cyrillic thread page on the glass whole: no line lost to the byte cap', async () => {
    const r = rig();
    const sentence = 'Договорились, позвоню тебе вечером, когда закончу работу.';
    const thread = [
      '1/1 · Анна',
      ...Array.from({ length: 12 }, () => `Анна: ${sentence}`),
      'Вы: Договорились, позвоню в девять.',
    ];
    expect(await r.hud.showList(thread, { ...READER, title: 'MESSAGES · CONVERSATION' })).toBe(true);
    const pages = readerPages(thread);
    const body = text(r.rebuilds[r.rebuilds.length - 1], 'main') ?? '';
    // The page the glass gets is the page the plugin counted, not one the
    // byte cap cut short with "…".
    expect(body).toBe(pages[0]);
    expect(utf8ByteLength(body)).toBeLessThanOrEqual(TEXT_CONTAINER_MAX_BYTES);
    expect(pages.join('\n')).toContain('Вы: Договорились, позвоню в девять.');
  });

  it('keeps the page count with the glass when a page turn is refused', async () => {
    const r = rig((update) => update.containerName !== 'main');
    const REVIEW = {
      title: 'MESSAGES · REVIEW', hint: 'up previous · tap: confirm · 2x tap: back',
      selectable: true, nativeTextPage: true, reviewTapOnLastPage: true,
    };
    expect(await r.hud.showList(longMessage, REVIEW)).toBe(true);
    const pages = readerPages(longMessage);
    expect(pages.length).toBeGreaterThan(1);
    expect(r.hud.scrollTextPage('down')).toBe(true);
    await vi.waitFor(() => expect(r.upgrades.some((u) => u.containerName === 'main')).toBe(true));
    // The words never changed, so neither do the count and the review's tap.
    await vi.waitFor(() => expect(r.hud.isTextPageAtEnd()).toBe(false));
    expect(r.upgrades.some((u) => u.containerName === 'hint')).toBe(false);
    expect(r.hud.scrollTextPage('up')).toBe(false);
  });

  it('reads every swipe the Mac offers, and forgets an earlier page once another is on the glass', async () => {
    const r = rig();
    // Not a Messages page: seven-line local pages, as before.
    const pages = paginateMeasured(
      readerParagraphs(longMessage).join('\n'), READER_TEXT_PAGE.textWidth, READER_TEXT_PAGE.lineCapacity,
    );
    // The Mac's promises need not sit side by side to count.
    expect(await r.hud.showList(longMessage, {
      ...READER, title: 'BRAINSTORM · SAVED', hint: 'up previous · tap: continue · down more · 2x tap: back',
    })).toBe(true);
    expect(text(r.rebuilds[r.rebuilds.length - 1], 'hint'))
      .toBe(`1/${pages.length} · tap: continue · swipe: page · 2x tap: back`);
    expect(r.hud.isTextPageAtEnd()).toBe(false);
    // A firmware list replaces the reader: its page state no longer counts.
    expect(await r.hud.showList(['Latest messages', 'Dele · Hi'], {
      title: 'MESSAGES · CONVERSATION', hint: 'tap read', selectable: true, nativeFirmwareList: true,
    })).toBe(true);
    expect(r.hud.isTextPageAtEnd()).toBe(true);
  });

  it('keeps the Mac\'s own footer on a reader that fits one local page', async () => {
    const r = rig();
    expect(await r.hud.showList(['1/1 · To work', 'open a PR'], {
      ...READER, title: 'AGENT TERMINAL · CHECK', hint: 'tap: send · 2x tap: back', reviewTapOnLastPage: true,
    })).toBe(true);
    expect(text(r.rebuilds[r.rebuilds.length - 1], 'hint')).toBe('tap: send · 2x tap: back');
    expect(r.hud.isTextPageAtEnd()).toBe(true);
  });

  it('accepts a bound reader on the rebuild receipt and refuses a retired one', async () => {
    const r = rig();
    let current = true;
    expect(await r.hud.showList(['1/1 · From Dele', 'Hi'], READER, () => current)).toBe(true);
    expect(r.visual.paintExact).not.toHaveBeenCalled();
    current = false;
    expect(await r.hud.showList(['1/1 · From Dele', 'Again'], READER, () => current)).toBe(false);
  });

  it('keeps the reader page across a confirmation and returns to it without an image rebuild', async () => {
    const r = rig();
    expect(await r.hud.showList(longMessage, READER)).toBe(true);
    expect(r.hud.scrollTextPage('down')).toBe(true);
    await r.hud.showConfirmation('Send to Dele? “On my way.”', 'send-1');
    const before = r.operations.length;

    expect(await r.hud.dismissConfirmation('send-1')).toBe(true);

    const since = r.operations.slice(before);
    expect(since.filter((op) => op.startsWith('rebuild:4:') || op.startsWith('visual:') || op.startsWith('exact:'))).toHaveLength(0);
    expect(since.filter((op) => op.startsWith('rebuild:0:3:0'))).toHaveLength(1);
    const pages = readerPages(longMessage);
    expect(text(r.rebuilds[r.rebuilds.length - 1], 'main')).toBe(pages[1]);
  });

  it('hands the reader straight to a firmware list, with no image page between', async () => {
    const r = rig();
    await r.hud.showText('home card', ContainerId.Main);
    expect(await r.hud.showList(['1/1 · From Dele', 'Hi'], READER)).toBe(true);
    const before = r.operations.length;
    expect(await r.hud.showList(['Latest messages', 'Dele · Hi'], {
      title: 'MESSAGES · CONVERSATION', hint: 'tap read', selectable: true,
    })).toBe(true);
    const since = r.operations.slice(before);
    expect(since.filter((op) => op.startsWith('visual:') || op.startsWith('rebuild:4:'))).toEqual([]);
    expect(since.filter((op) => /^rebuild:0:\d+:1$/.test(op))).toHaveLength(1);
  });
});

/** The lines a reader reads: each screen's lines after the ones it carried
 * over from the screen before (the longest end of one that opens the next). */
function readThrough(screens: readonly (readonly string[])[]): string[] {
  const read: string[] = [];
  for (const screen of screens) {
    let carried = Math.min(read.length, screen.length);
    while (carried > 0 && read.slice(read.length - carried).join('\n') !== screen.slice(0, carried).join('\n')) carried -= 1;
    read.push(...screen.slice(carried));
  }
  return read;
}

/**
 * A News article is read in the glasses' own scroll box (ScrollBox): the
 * firmware scrolls it a line a swipe with no round trip, and only a
 * stretch's edge reaches the plugin, which writes the next stretch one
 * line below the top line on screen (hardware, 2026-10-14: every
 * textContainerUpgrade replaces the whole text and opens it at the top).
 */
describe('HudPresenter News article in the glasses\' scroll box', () => {
  const ARTICLE = { title: 'NEWS · ARTICLE', hint: 'left x2: back', selectable: false, nativeTextPage: true };
  const paragraphs = Array.from({ length: 30 }, (_, index) => (
    `Paragraph ${index + 1} of the article runs long enough to wrap across several lines of the reader page, the way prose does.`
  ));
  // The article template (PanelPublisher): headline, source and age, a
  // blank line, then the paragraphs a blank line apart.
  const rows = readerRows([], `Rates hold\nFixture Wire · 12m\n\n${paragraphs.join('\n\n')}`);
  const mains = (r: ReturnType<typeof rig>) => r.upgrades.filter((u) => u.containerName === 'main');
  const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
  /** The stretch the page opened on: what its rebuild carried. */
  const opening = (r: ReturnType<typeof rig>) => (
    r.rebuilds[r.rebuilds.length - 1]?.textObject?.find((box) => box.containerName === 'main')?.content ?? ''
  );

  it('opens as a box of whole lines in one write: the rebuild carries the first stretch', async () => {
    const r = rig();
    expect(await r.hud.showList(rows, ARTICLE)).toBe(true);
    const page = r.rebuilds[r.rebuilds.length - 1];
    const main = page?.textObject?.find((box) => box.containerName === 'main');
    // Seven whole firmware lines inside the padding: the scroll ends on a line.
    expect(main?.height).toBe(SCROLL_BOX_LINES * 27 + 2 * (main?.paddingLength ?? 0));
    expect(main?.isEventCapture).toBe(1);
    expect(utf8ByteLength(main?.content ?? '')).toBeLessThanOrEqual(TEXT_CONTAINER_MAX_BYTES);
    expect(main?.content?.startsWith('Rates hold\nFixture Wire · 12m\n\nParagraph 1 ')).toBe(true);
    expect(text(page, 'hint')).toBe('swipe: scroll · 2x tap: back');
    for (const line of (main?.content ?? '').split('\n')) {
      expect(firmwareLineCount(line, READER_TEXT_PAGE.textWidth - 10)).toBeLessThanOrEqual(1);
    }
    // One write to the glasses, the rebuild; nothing redraws it afterwards
    // (a second write replacing the same text was the blink on every article).
    await settle();
    await settle();
    expect(r.upgrades).toHaveLength(0);
    expect(r.rebuilds).toHaveLength(1);
    // The first full-size stretch is written when the wearer reaches the
    // bottom edge of the opening one.
    expect(r.hud.scrollTextPage('down')).toBe(true);
    await vi.waitFor(() => expect(mains(r)).toHaveLength(1));
    const stretch = mains(r)[0]!.content ?? '';
    expect(utf8ByteLength(stretch)).toBeGreaterThan(TEXT_CONTAINER_MAX_BYTES);
    expect(utf8ByteLength(stretch)).toBeLessThanOrEqual(SCROLL_BOX_MAX_BYTES);
    for (const line of stretch.split('\n')) {
      expect(firmwareLineCount(line, READER_TEXT_PAGE.textWidth - 10)).toBeLessThanOrEqual(1);
    }
  });

  it('carries on a line below the last screen at each edge, every word once, and stops at the end', async () => {
    const r = rig();
    await r.hud.showList(rows, ARTICLE);
    const stretches = [opening(r)];
    while (r.hud.scrollTextPage('down')) {
      await settle();
      const written = mains(r);
      if (written.length === stretches.length - 1) break; // the end: nothing written
      stretches.push(written[written.length - 1]!.content ?? '');
    }
    expect(stretches.length).toBeGreaterThan(2);
    for (const [index, stretch] of stretches.entries()) {
      const lines = stretch.split('\n');
      expect(lines.length).toBeGreaterThanOrEqual(SCROLL_BOX_LINES);
      if (index === 0) continue;
      // The new stretch opens on the old one's last screen moved up a line
      // (or two, past the blank between paragraphs).
      const previous = stretches[index - 1]!.split('\n');
      const lastScreen = previous.slice(previous.length - SCROLL_BOX_LINES);
      const shift = lastScreen.findIndex((_, at) => at > 0 && lastScreen.slice(at).every((line, j) => lines[j] === line));
      expect([1, 2]).toContain(shift);
      expect(lines[0]).not.toBe('');
    }
    const words = (value: string) => value.split(/\s+/u).filter(Boolean);
    expect(words(readThrough(stretches.map((stretch) => stretch.split('\n'))).join(' ')))
      .toEqual(words(readerParagraphs(rows).join(' ')));
    // At the article's end a swipe writes nothing and is left to the menu
    // (an article's one Mac page has nothing after it).
    const before = mains(r).length;
    expect(r.hud.scrollTextPage('down')).toBe(false);
    await settle();
    expect(mains(r)).toHaveLength(before);
    expect(r.rebuilds).toHaveLength(1);
  });

  it('goes back a screen at a stretch\'s top, and stays put at the article\'s start', async () => {
    const r = rig();
    await r.hud.showList(rows, ARTICLE);
    expect(r.hud.scrollTextPage('up')).toBe(false);
    await settle();
    expect(mains(r)).toHaveLength(0);
    r.hud.scrollTextPage('down');
    await vi.waitFor(() => expect(mains(r)).toHaveLength(1));
    await settle();
    const second = (mains(r)[0]!.content ?? '').split('\n');
    r.hud.scrollTextPage('up');
    await vi.waitFor(() => expect(mains(r)).toHaveLength(2));
    const back = (mains(r)[1]!.content ?? '').split('\n');
    // A screen above: the old top line is now the last line of the screen.
    expect(back[SCROLL_BOX_LINES - 1] === second[0] || back[SCROLL_BOX_LINES] === second[0]).toBe(true);
  });

  it('answers the swipe that asked while a stretch is being written', async () => {
    // The glass holds the next stretch's write until released.
    let hold = false;
    let release: (() => void) | undefined;
    const r = rig(() => (hold
      ? new Promise<boolean>((resolve) => { release = () => resolve(true); }) as unknown as boolean
      : true));
    await r.hud.showList(rows, ARTICLE);
    await settle();
    hold = true;
    expect(r.hud.scrollTextPage('down')).toBe(true);
    expect(r.hud.scrollTextPage('down')).toBe(true);
    expect(r.hud.scrollTextPage('down')).toBe(true);
    await settle();
    expect(mains(r)).toHaveLength(1);
    hold = false;
    release?.();
    await settle();
    r.hud.scrollTextPage('down');
    await vi.waitFor(() => expect(mains(r)).toHaveLength(2));
  });

  it('cuts every stretch to the rebuild size when the glasses refuse a bigger one', async () => {
    // The simulator holds 999 bytes a text container.
    const r = rig((update) => utf8ByteLength(update.content ?? '') <= TEXT_CONTAINER_MAX_BYTES);
    await r.hud.showList(rows, ARTICLE);
    await settle();
    r.hud.scrollTextPage('down');
    await vi.waitFor(() => expect(mains(r).length).toBeGreaterThanOrEqual(2));
    await settle();
    const last = mains(r)[mains(r).length - 1]!;
    expect(utf8ByteLength(last.content ?? '')).toBeLessThanOrEqual(TEXT_CONTAINER_MAX_BYTES);
  });
});

/**
 * A Messages conversation and a saved QuickNote read in the same scroll box
 * as an article (the wearer's ruling, 2026-10-15): the Mac sends each whole,
 * in the reader template (who or what, a blank line, then the text).
 */
describe('HudPresenter Messages and QuickNotes in the glasses\' scroll box', () => {
  const THREAD = { title: 'MESSAGES · CONVERSATION', hint: 'tap: reply · left x2: back', selectable: true, nativeTextPage: true };
  const message = (index: number) => (
    `${index % 2 ? 'You' : 'Dele'}: Message ${index} runs long enough to wrap onto a second line of the reader, as messages do.`
  );
  const messages = (count: number) => Array.from({ length: count }, (_, index) => message(count - index)).join('\n');
  const thread = (count: number) => readerRows([], `Dele\n\n${messages(count)}`);
  const mains = (r: ReturnType<typeof rig>) => r.upgrades.filter((u) => u.containerName === 'main');
  const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

  it('scrolls a conversation, each message\'s words hanging under its first row\'s', async () => {
    const r = rig();
    expect(await r.hud.showList(thread(4), THREAD)).toBe(true);
    const page = r.rebuilds[r.rebuilds.length - 1];
    const main = page?.textObject?.find((box) => box.containerName === 'main');
    expect(main?.height).toBe(SCROLL_BOX_LINES * 27 + 2 * (main?.paddingLength ?? 0));
    expect(text(page, 'hint')).toBe('tap: reply · swipe: scroll · 2x tap: back');
    const lines = (main?.content ?? '').split('\n');
    expect(lines.slice(0, 2)).toEqual(['Dele', '']);
    const first = lines.findIndex((line) => line.startsWith('Dele: Message 4 '));
    expect(first).toBe(2);
    // The wrapped row starts under the words, not under the name.
    const pad = lines[first + 1]!;
    expect(pad.startsWith(' ')).toBe(true);
    expect(getTextWidth(pad.slice(0, pad.length - pad.trimStart().length)))
      .toBeGreaterThanOrEqual(getTextWidth('Dele:'));
  });

  it('carries on into older messages a line below where the wearer stopped', async () => {
    const r = rig();
    await r.hud.showList(thread(20), THREAD);
    await settle();
    while (r.hud.scrollTextPage('down')) {
      await settle();
    }
    await settle();
    // At the thread's end the swipe is the menu's: it asks the Mac for
    // older messages, which arrive as the same thread grown longer.
    expect(r.hud.isTextPageAtEnd()).toBe(true);
    const shown = (mains(r)[mains(r).length - 1]!.content ?? '').split('\n');
    const lastScreen = shown.slice(shown.length - SCROLL_BOX_LINES);
    const grown = readerRows([], `Dele\n\n${messages(20)}\n${[0, -1, -2].map(message).join('\n')}`);
    expect(await r.hud.showList(grown, THREAD)).toBe(true);
    const reopened = (text(r.rebuilds[r.rebuilds.length - 1], 'main') ?? '').split('\n');
    // Not back at the top: the last screen moved up a line, then the older
    // messages below it.
    expect(reopened[0]).not.toBe('Dele');
    expect(reopened.slice(0, SCROLL_BOX_LINES - 1)).toEqual(lastScreen.slice(1));
    expect(reopened.some((line) => line.startsWith('Dele: Message 0 '))).toBe(true);
  });

  it('reopens a Brainstorm room on the wearer\'s move and the new answers, wherever they had stopped reading', async () => {
    const ROOM = { title: 'BRAINSTORM · ROOM', hint: 'speak: your move', selectable: true, nativeTextPage: true };
    const round = (speaker: string, count: number) => (
      `${speaker} > ${Array.from({ length: count }, (_, i) => `Point ${i + 1} of the answer, said at some length so it wraps.`).join(' ')}`
    );
    const roundOne = [round('CLAUDE-B', 150), round('CLAUDE', 150)].join('\n\n');
    const r = rig();
    await r.hud.showList(readerRows([], roundOne), ROOM);
    await settle();
    // The wearer read a screen or two of round one, then spoke.
    for (let swipe = 0; swipe < 2; swipe += 1) {
      r.hud.scrollTextPage('down');
      await settle();
    }
    expect(r.hud.isTextPageAtEnd()).toBe(false);
    const grown = `${roundOne}\n\nYOU > Okay, now a step by step plan.\n\n${round('CLAUDE-B', 150)}`;
    expect(await r.hud.showList(readerRows([], grown), ROOM)).toBe(true);
    const reopened = (text(r.rebuilds[r.rebuilds.length - 1], 'main') ?? '').split('\n');
    expect(reopened[0]).toBe('YOU > Okay, now a step by step plan.');
  });

  it('opens a thread that changed at its top (a sent reply) at the top', async () => {
    const r = rig();
    await r.hud.showList(thread(20), THREAD);
    await settle();
    r.hud.scrollTextPage('down');
    await vi.waitFor(() => expect(mains(r)).toHaveLength(1));
    await settle();
    const replied = readerRows([], `Dele\n\nYou: On my way ▶\n${messages(20)}`);
    expect(await r.hud.showList(replied, THREAD)).toBe(true);
    expect(text(r.rebuilds[r.rebuilds.length - 1], 'main')?.startsWith('Dele\n\nYou: On my way ▶')).toBe(true);
  });

  it('scrolls a saved QuickNote, its exact text and the Brainstorm conversation the same way', async () => {
    for (const title of ['QUICKNOTES · NOTE', 'QUICKNOTES · DETAIL', 'BRAINSTORM · ROOM']) {
      const r = rig();
      const note = readerRows([], [
        'Standup notes', 'Recording · 2 transcript sections', '',
        'OVERVIEW', 'The team agreed to ship Friday.', '',
        'TRANSCRIPT', ...Array.from({ length: 80 }, (_, index) => `Transcript sentence ${index + 1} of the standup.`),
      ].join('\n'));
      expect(await r.hud.showList(note, { title, hint: 'left x2: back', selectable: false, nativeTextPage: true })).toBe(true);
      const page = r.rebuilds[r.rebuilds.length - 1];
      const main = page?.textObject?.find((box) => box.containerName === 'main');
      expect(main?.height).toBe(SCROLL_BOX_LINES * 27 + 2 * (main?.paddingLength ?? 0));
      expect(main?.content).toMatch(/^Standup notes\nRecording · 2 transcript sections\n\nOVERVIEW\n/u);
      expect(text(page, 'hint')).toBe('swipe: scroll · 2x tap: back');
      await settle();
      expect(mains(r)).toHaveLength(0);
      expect(r.hud.scrollTextPage('down')).toBe(true);
      await vi.waitFor(() => expect(mains(r)).toHaveLength(1));
      expect(r.rebuilds).toHaveLength(1);
    }
  });
});

describe('HudPresenter native prompt question over the visual HUD', () => {
  it('shows the question as one text page carrying the status, with no image tiles', async () => {
    const r = rig();
    await r.hud.showText('12:34 JOAT listening bat 80%', ContainerId.StatusBar);
    await r.hud.showText('home card', ContainerId.Main);
    const framesBefore = r.frames.length;

    expect(await r.hud.showPromptQuestion('prompt-1', 'What should I say?', () => true)).toBe(true);

    const page = r.rebuilds[r.rebuilds.length - 1];
    expect(page?.imageObject ?? []).toHaveLength(0);
    expect(page?.textObject?.map((c) => c.containerName)).toEqual(['status', 'main', 'hint']);
    expect(text(page, 'main')).toBe('What should I say?');
    expect(text(page, 'status')).toBe('12:34 JOAT listening bat 80%');
    expect(text(page, 'hint')).toBe(PROMPT_QUESTION_HINT);
    expect(r.frames).toHaveLength(framesBefore);
    expect(r.visual.paintExact).not.toHaveBeenCalled();
    // Nothing repaints behind the open question.
    expect(await r.hud.showText('arrived while asking', ContainerId.Main)).toBe(true);
    expect(r.frames).toHaveLength(framesBefore);
  });

  it('returns to the image page on dismissal: one visual rebuild, forget, then the retained overview', async () => {
    const r = rig();
    await r.hud.showText('home card', ContainerId.Main);
    await r.hud.showPromptQuestion('prompt-2', 'What should I say?', () => true);
    const before = r.operations.length;

    expect(await r.hud.dismissPromptQuestion('prompt-2')).toBe(true);

    expect(r.operations.slice(before)).toEqual(['rebuild:4:1:0', 'forget', 'exact:overview']);
    expect(r.frames[r.frames.length - 1]).toMatchObject({ kind: 'overview', main: 'home card' });
  });

  it('returns a firmware list thread directly to its own page after the question', async () => {
    const r = rig();
    expect(await r.hud.showList(['Latest messages', 'Dele · Hi'], {
      title: 'MESSAGES · CONVERSATION', hint: 'tap read · say reply', selectable: true, nativeFirmwareList: true,
    })).toBe(true);
    await r.hud.showPromptQuestion('prompt-3', 'What should I say?', () => true);
    const before = r.operations.length;

    expect(await r.hud.dismissPromptQuestion('prompt-3')).toBe(true);

    const since = r.operations.slice(before);
    expect(since.filter((op) => op.startsWith('rebuild:0:2:1'))).toHaveLength(1);
    expect(since.filter((op) => op.startsWith('rebuild:4:') || op.startsWith('visual:') || op.startsWith('exact:'))).toHaveLength(0);
  });

  it('refuses a retired question and restores the feature beneath it', async () => {
    const r = rig();
    await r.hud.showText('home card', ContainerId.Main);
    let current = true;
    r.visual.withPageTransition.mockImplementationOnce(async (replace: () => Promise<boolean>) => {
      const ok = await replace();
      current = false;
      return ok;
    });
    expect(await r.hud.showPromptQuestion('prompt-4', 'What should I say?', () => current)).toBe(false);
    expect(r.operations[r.operations.length - 1]).toBe('exact:overview');
    expect(await r.hud.showText('after refused question', ContainerId.Main)).toBe(true);
  });

  it('refuses a question longer than one firmware page', async () => {
    const r = rig();
    const long = Array.from({ length: 12 }, (_, i) => `Line ${i + 1} of a very long question that keeps wrapping.`).join('\n');
    expect(await r.hud.showPromptQuestion('prompt-5', long, () => true)).toBe(false);
    expect(r.rebuilds.filter((p) => text(p, 'hint') === PROMPT_QUESTION_HINT)).toHaveLength(0);
  });
});
