import { describe, expect, it, vi } from 'vitest';
import type { RebuildPageContainer, TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import { HudPageBuilder, PROMPT_QUESTION_HINT, READER_TEXT_PAGE } from './HudPageBuilder';
import { HudPresenter, setNativeOverviewForTest } from './HudPresenter';
import { paginateHanging, paginateMeasured } from './FirmwareText';
import { getTextWidth } from '@evenrealities/pretext';
import { ContainerId } from './types';
import type { HudVisualFrame } from './HudVisualSurface';
import { readerParagraphs } from './EvenFont';

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
function rig() {
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
        return true;
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
