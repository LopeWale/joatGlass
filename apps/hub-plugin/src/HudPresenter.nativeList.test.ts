import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import { HudPresenter } from './HudPresenter';
import { HudPageBuilder } from './HudPageBuilder';
import type { BridgeClient } from './BridgeClient';
import type { HudVisualFrame, HudVisualSurface } from './HudVisualSurface';
import { ContainerId } from './types';

/** News and Search use the same custom typography and banner as Messages.
 * The old native-first contract changed both when a reader opened or Back
 * restored a nonzero selection. Native lists remain a compatibility path
 * only when there is no graphical renderer. */
interface RebuiltPage {
  listObject?: unknown[];
  imageObject?: unknown[];
  textObject?: Array<{ containerID?: number; content?: string }>;
}

function harness(withVisual = true) {
  const rebuilds: RebuiltPage[] = [];
  const upgrades: TextContainerUpgrade[] = [];
  const frames: HudVisualFrame[] = [];
  const state = { paintAccepted: true, rebuildAccepted: true };
  const client = {
    require: () => ({
      textContainerUpgrade: async (update: TextContainerUpgrade) => { upgrades.push(update); return true; },
      rebuildPageContainer: async (page: RebuiltPage) => { rebuilds.push(page); return state.rebuildAccepted; },
    }),
  } as unknown as BridgeClient;
  const visual = {
    paint: async (frame: HudVisualFrame) => { frames.push(frame); return state.paintAccepted; },
    paintExact: async (frame: HudVisualFrame, current: () => boolean) => {
      if (!current()) return false;
      frames.push(frame);
      return state.paintAccepted && current();
    },
    forgetPage: () => undefined,
  } as unknown as HudVisualSurface;
  const hud = new HudPresenter(client, new HudPageBuilder(), withVisual ? visual : undefined);
  return { hud, rebuilds, upgrades, frames, state };
}

const NEWS = ['Story 01 · 2h', 'Story 02 · 3h', 'Story 03 · 5h'];
const NEWS_OPTIONS = {
  title: 'NEWS', hint: 'tap: open · hold: refresh · left x2: back',
  selectable: true, railTab: 'news' as const,
};

async function openNews(h: ReturnType<typeof harness>) {
  await h.hud.showText('9:41  JOAT ready  bat 87%', ContainerId.StatusBar);
  return h.hud.showList(NEWS, NEWS_OPTIONS);
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); });

describe('consistent custom News and Search surface', () => {
  it.each([
    ['NEWS', 'news'], ['SEARCH', 'search'],
  ] as const)('opens %s with the existing custom banner, rows and rail', async (title, railTab) => {
    const h = harness();
    h.hud.setMicState(true);
    await h.hud.showText('9:41  JOAT ready  bat 87%', ContainerId.StatusBar);
    expect(await h.hud.showList(NEWS, { ...NEWS_OPTIONS, title, railTab })).toBe(true);
    expect(h.rebuilds).toEqual([]);
    expect(h.upgrades).toEqual([]);
    expect(h.frames[h.frames.length - 1]).toMatchObject({
      kind: 'list', title, items: NEWS, selectedIndex: 0, selectable: true,
      status: '9:41  JOAT ready  bat 87%', micOn: true, rail: { activeTab: railTab },
    });
  });

  it('keeps status and real microphone state on the same custom frame', async () => {
    const h = harness();
    await openNews(h);
    h.hud.setMicState(false);
    await h.hud.showText('9:42  JOAT hearing you…  bat 87%', ContainerId.StatusBar);
    expect(h.frames[h.frames.length - 1]).toMatchObject({
      kind: 'list', title: 'NEWS', status: '9:42  JOAT hearing you…  bat 87%', micOn: false,
    });
    expect(h.rebuilds).toEqual([]);
    expect(h.upgrades).toEqual([]);
  });

  it('paints actual selected rows on local scrolling without a page-type rebuild', async () => {
    const h = harness();
    await openNews(h);
    const before = h.frames.length;
    expect(await h.hud.setSelection(2)).toBe(true);
    expect(h.frames[h.frames.length - 1]).toMatchObject({ kind: 'list', selectedIndex: 2 });
    expect(await h.hud.moveSelection(-1)).toBe(true);
    expect(h.frames[h.frames.length - 1]).toMatchObject({ kind: 'list', selectedIndex: 1 });
    expect(h.frames).toHaveLength(before + 2);
    expect(h.rebuilds).toEqual([]);
  });

  it.each([
    ['NEWS', 'NEWS · ARTICLE', 'news'], ['SEARCH', 'SEARCH · RESULT', 'search'],
  ] as const)('%s open-scroll-reader-Back-reopen preserves typography and the selected item', async (title, readerTitle, railTab) => {
    const h = harness();
    const options = { ...NEWS_OPTIONS, title, railTab };
    await h.hud.showList(NEWS, options);
    await h.hud.moveSelection(1);
    expect(h.frames[h.frames.length - 1]).toMatchObject({ title, selectedIndex: 1 });
    await h.hud.showList([NEWS[1]!, 'Complete article body'], {
      title: readerTitle, hint: 'left x2: back', railTab,
    });
    expect(h.frames[h.frames.length - 1]).toMatchObject({
      kind: 'list', title: readerTitle, items: [NEWS[1], 'Complete article body'],
    });
    await h.hud.showList(NEWS, options);
    expect(await h.hud.restoreSelection(1)).toBe(true);
    expect(h.frames[h.frames.length - 1]).toMatchObject({ kind: 'list', title, selectedIndex: 1 });
    await h.hud.showList([NEWS[1]!, 'Complete article body'], { title: readerTitle, railTab });
    expect(h.frames[h.frames.length - 1]).toMatchObject({ title: readerTitle, items: [NEWS[1], 'Complete article body'] });
    expect(h.rebuilds).toEqual([]);
  });

  it('refreshes rows and returns Home on the same custom display', async () => {
    const h = harness();
    await openNews(h);
    const refreshed = [...NEWS, 'Story 04 · 1m'];
    await h.hud.showList(refreshed, NEWS_OPTIONS);
    expect(h.frames[h.frames.length - 1]).toMatchObject({ kind: 'list', items: refreshed });
    await h.hud.showDefault();
    expect(h.frames[h.frames.length - 1]).toMatchObject({ kind: 'overview' });
    expect(h.rebuilds).toEqual([]);
  });

  it('restores the selected News row after confirmation without entering a native page', async () => {
    const h = harness();
    await openNews(h);
    await h.hud.setSelection(1);
    expect(await h.hud.showConfirmation('Send this message?', 'gate-1')).toBe(true);
    expect(h.frames[h.frames.length - 1]).toMatchObject({ kind: 'confirmation' });
    await h.hud.dismissConfirmation('gate-1');
    expect(h.frames[h.frames.length - 1]).toMatchObject({ kind: 'list', title: 'NEWS', selectedIndex: 1 });
    expect(h.rebuilds).toEqual([]);
  });

  it('keeps an exact question dominant over News updates and restores the current custom list on cancel', async () => {
    const h = harness();
    await openNews(h);
    await h.hud.setSelection(1);
    expect(await h.hud.showPromptQuestion('prompt-one', 'What should the message say?', () => true)).toBe(true);
    await h.hud.showText('9:42  JOAT ready', ContainerId.StatusBar);
    expect(h.frames[h.frames.length - 1]).toMatchObject({ kind: 'prompt_question', promptId: 'prompt-one' });
    await h.hud.dismissPromptQuestion('prompt-one');
    expect(h.frames[h.frames.length - 1]).toMatchObject({ kind: 'list', title: 'NEWS', selectedIndex: 1 });
    expect(h.rebuilds).toEqual([]);
  });

  it('heals a refused custom frame using the image layout, never a firmware News list', async () => {
    const h = harness();
    await openNews(h);
    h.state.paintAccepted = false;
    expect(await h.hud.setSelection(1)).toBe(false);
    expect(h.rebuilds).toHaveLength(1);
    expect(h.rebuilds[0]?.imageObject).toHaveLength(4);
    expect(h.rebuilds[0]?.listObject).toBeUndefined();
  });

  it('retains native list compatibility when no custom renderer exists', async () => {
    const h = harness(false);
    expect(await openNews(h)).toBe(true);
    expect(h.rebuilds).toHaveLength(1);
    expect(h.rebuilds[0]?.listObject).toHaveLength(1);
    expect(h.rebuilds[0]?.imageObject).toBeUndefined();
    expect(h.frames).toEqual([]);
    h.state.rebuildAccepted = false;
    expect(await h.hud.showList(['Result'], { title: 'SEARCH', railTab: 'search' })).toBe(false);
  });
});
