import { describe, expect, it, vi } from 'vitest';
import type { RebuildPageContainer, TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import { HudPageBuilder, PROMPT_QUESTION_HINT } from './HudPageBuilder';
import { HudPresenter, paginateAnswer } from './HudPresenter';
import { ContainerId } from './types';
import type { HudVisualFrame } from './HudVisualSurface';

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
  };
  const hud = new HudPresenter(client, new HudPageBuilder(), visual);
  return { hud, operations, rebuilds, upgrades, frames, visual };
}

function text(page: RebuildPageContainer | undefined, name: string): string | undefined {
  return page?.textObject?.find((container) => container.containerName === name)?.content;
}

const longMessage = Array.from({ length: 10 }, (_, index) => (
  `Row ${index + 1} of a long message that the Mac cut into sixty-two column rows.`
));
const READER = { title: 'MESSAGES · MESSAGE', hint: 'swipe: more · say reply · double-click: back', selectable: false, nativeTextPage: true };

describe('HudPresenter native text page for a Mac-paged reader', () => {
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
    expect(r.hud.usesCustomListSelection()).toBe(false);
    expect(r.hud.usesCustomListSelection({ nativeTextPage: true })).toBe(false);
  });

  it('pages a long Mac page in place and hands the edge swipe back to the caller', async () => {
    const r = rig();
    expect(await r.hud.showList(longMessage, READER)).toBe(true);
    const pages = paginateAnswer(longMessage.join('\n'));
    expect(pages.length).toBeGreaterThan(1);
    const first = r.rebuilds[r.rebuilds.length - 1];
    expect(text(first, 'main')).toBe(pages[0]);
    expect(text(first, 'hint')).toBe(`1/${pages.length} · ${READER.hint}`);
    const rebuildsAfterOpen = r.rebuilds.length;

    expect(r.hud.scrollTextPage('up')).toBe(false);
    expect(r.hud.scrollTextPage('down')).toBe(true);
    await vi.waitFor(() => expect(r.upgrades.some((u) => u.containerName === 'main' && u.content === pages[1])).toBe(true));
    await vi.waitFor(() => expect(r.upgrades.some((u) => (
      u.containerName === 'hint' && u.content === `2/${pages.length} · ${READER.hint}`
    ))).toBe(true));
    expect(r.rebuilds).toHaveLength(rebuildsAfterOpen);

    for (let page = 1; page < pages.length - 1; page += 1) expect(r.hud.scrollTextPage('down')).toBe(true);
    // Past the last local page the Mac decides, so the swipe is not consumed.
    expect(r.hud.scrollTextPage('down')).toBe(false);
    expect(r.rebuilds).toHaveLength(rebuildsAfterOpen);
  });

  it('opens the Mac\'s previous page on its last local page when asked', async () => {
    const r = rig();
    const pages = paginateAnswer(longMessage.join('\n'));
    expect(await r.hud.showList(longMessage, { ...READER, nativeTextPageFromEnd: true })).toBe(true);
    const page = r.rebuilds[r.rebuilds.length - 1];
    expect(text(page, 'main')).toBe(pages[pages.length - 1]);
    expect(text(page, 'hint')).toBe(`${pages.length}/${pages.length} · ${READER.hint}`);
    expect(r.hud.scrollTextPage('down')).toBe(false);
    expect(r.hud.scrollTextPage('up')).toBe(true);
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
    const pages = paginateAnswer(longMessage.join('\n'));
    expect(text(r.rebuilds[r.rebuilds.length - 1], 'main')).toBe(pages[1]);
  });

  it('returns to the image page when the next list is raster', async () => {
    const r = rig();
    await r.hud.showText('home card', ContainerId.Main);
    expect(await r.hud.showList(['1/1 · From Dele', 'Hi'], READER)).toBe(true);
    const before = r.operations.length;
    expect(await r.hud.showList(['Latest messages', 'Dele · Hi'], {
      title: 'MESSAGES · CONVERSATION', hint: 'tap read', selectable: true,
    })).toBe(true);
    expect(r.operations.slice(before)).toEqual(['rebuild:4:1:0', 'forget', 'visual:list']);
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
