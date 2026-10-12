import { describe, expect, it, vi } from 'vitest';
import type { RebuildPageContainer, TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import { HudPageBuilder, READER_TEXT_PAGE } from './HudPageBuilder';
import { HudPresenter, setNativeOverviewForTest } from './HudPresenter';
import type { HudVisualFrame } from './HudVisualSurface';
import { QUICKNOTES_UNDO_MENU_ID, type ProductivityHudModel } from './types';

// These tests pin the IMAGE overview (VITE_NATIVE_OVERVIEW=0). The text
// overview, which is Home by default, has its own: HudPresenter.textOverview.test.ts.
setNativeOverviewForTest(false);

/**
 * Receipts, outcome strips and productivity pages over a page the firmware
 * owns. Before: a receipt over a firmware list rebuilt the four image
 * containers and repainted the list as pixels under a two-tile strip; over a
 * text page it painted nothing at all; a productivity page was four tiles;
 * and Home from a text page never redrew the glass. Now each is one
 * firmware page, and the retained page comes back as the page it was.
 */
function rig() {
  const operations: string[] = [];
  const rebuilds: RebuildPageContainer[] = [];
  const client = {
    require: () => ({
      rebuildPageContainer: vi.fn(async (page: RebuildPageContainer) => {
        operations.push(`rebuild:${page.imageObject?.length ?? 0}:${page.textObject?.length ?? 0}:${page.listObject?.length ?? 0}`);
        rebuilds.push(page);
        return true;
      }),
      textContainerUpgrade: vi.fn(async (update: TextContainerUpgrade) => {
        operations.push(`upgrade:${update.containerName ?? update.containerID}`);
        return true;
      }),
    }),
  } as unknown as BridgeClient;
  const frames: HudVisualFrame[] = [];
  const visual = {
    paint: vi.fn(async (frame: HudVisualFrame) => { operations.push(`visual:${frame.kind}`); frames.push(frame); return true; }),
    paintExact: vi.fn(async (frame: HudVisualFrame, current: () => boolean) => {
      if (!current()) return false;
      operations.push(`exact:${frame.kind}`); frames.push(frame); return true;
    }),
    forgetPage: vi.fn(() => operations.push('forget')),
    whenIdle: vi.fn(async () => true),
    withPageTransition: vi.fn(async (replace: () => Promise<boolean>) => replace()),
  };
  const hud = new HudPresenter(client, new HudPageBuilder(), visual);
  // `forget` is the page cache being dropped around a page swap; it is not a write.
  const writes = () => operations.filter((operation) => operation !== 'forget');
  const reset = () => { operations.length = 0; };
  return { hud, operations, writes, reset, rebuilds, frames, visual };
}

function main(page: RebuildPageContainer | undefined): string | undefined {
  return page?.textObject?.find((container) => container.containerName === 'main')?.content;
}

const due: ProductivityHudModel = {
  domain: 'reminders', phase: 'due', title: 'Buy filters',
  primary: 'Due now', secondary: 'This reminder is still incomplete.', hint: '2x tap: Planning',
};
const saved: ProductivityHudModel = {
  domain: 'reminders', phase: 'saved', title: 'Buy filters',
  primary: 'Saved', secondary: 'Reminders read-back matched', hint: '2x tap: Planning',
};

describe('a receipt over a firmware page', () => {
  it('is one firmware page and returns the list as a firmware list', async () => {
    const r = rig();
    expect(await r.hud.showList(['row one', 'row two'], { title: 'ACTIVITY', hint: 'tap: read', nativeFirmwareList: true })).toBe(true);
    r.reset();
    expect(await r.hud.showAssistantOverlay('Noted.', false, true, 'receipt')).toBe(true);
    // No image containers were recreated and no tile was painted.
    expect(r.writes()).toEqual(['rebuild:0:3:0']);
    expect(main(r.rebuilds[r.rebuilds.length - 1])).toBe('Noted.');
    r.reset();
    expect(await r.hud.dismissAssistantOverlay()).toBe(true);
    expect(r.writes()).toEqual(['rebuild:0:2:1']);
    expect(r.frames).toEqual([]);
  });

  it('settles on its own when the Mac asks, back to a text page', async () => {
    vi.useFakeTimers();
    try {
      const r = rig();
      expect(await r.hud.showList(['A short note'], { title: 'QUICKNOTES · DETAIL', hint: '2x tap: back', nativeTextPage: true })).toBe(true);
      expect(await r.hud.showAssistantOverlay('Saved QuickNote: A short note', false, true, 'receipt')).toBe(true);
      expect(r.hud.settleAssistantOverlay(3_000)).toBe(true);
      r.reset();
      await vi.advanceTimersByTimeAsync(3_100);
      expect(r.writes()).toEqual(['rebuild:0:3:0']);
      expect(main(r.rebuilds[r.rebuilds.length - 1])).toBe('A short note');
      expect(r.frames).toEqual([]);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('an outcome strip over a firmware list', () => {
  it('is one firmware page that returns the list after five seconds', async () => {
    vi.useFakeTimers();
    try {
      const r = rig();
      expect(await r.hud.showList(['[18:00] □ Buy filters'], { title: 'PLANNING', hint: 'tap: open', nativeFirmwareList: true })).toBe(true);
      r.reset();
      const visible = vi.fn();
      expect(await r.hud.showProductivity(saved, undefined, visible)).toBe(true);
      expect(visible).toHaveBeenCalledOnce();
      expect(r.writes()).toEqual(['rebuild:0:3:0']);
      expect(main(r.rebuilds[r.rebuilds.length - 1])).toBe('SAVED · Buy filters\nSaved');
      r.reset();
      await vi.advanceTimersByTimeAsync(5_100);
      expect(r.writes()).toEqual(['rebuild:0:2:1']);
      expect(r.frames).toEqual([]);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('a productivity page under the transition barrier', () => {
  it('is a firmware text page, not four tiles', async () => {
    const r = rig();
    expect(await r.hud.showProductivity(due)).toBe(true);
    expect(r.writes()).toEqual(['rebuild:0:3:0']);
    expect(main(r.rebuilds[0])).toBe('Reminders · DUE NOW\nBuy filters\nDue now\nThis reminder is still incomplete.');
    expect(r.hud.isProductivityVisible(due)).toBe(true);
    expect(r.frames).toEqual([]);
  });

  it('gives way to Home with the image page recreated', async () => {
    const r = rig();
    expect(await r.hud.showProductivity(due)).toBe(true);
    r.reset();
    expect(await r.hud.showDefault()).toBe(true);
    expect(r.writes()[0]).toBe('rebuild:4:1:0');
    expect(r.frames.map((frame) => frame.kind)).toEqual(['overview']);
  });
});

describe('Home from a firmware text page', () => {
  it('recreates the image page and paints the overview', async () => {
    const r = rig();
    expect(await r.hud.showList(['➤ Navigate to a place'], { title: 'NAVIGATION', hint: 'tap: choose', nativeTextPage: true })).toBe(true);
    r.reset();
    expect(await r.hud.showDefault()).toBe(true);
    expect(r.writes()[0]).toBe('rebuild:4:1:0');
    expect(r.frames.map((frame) => frame.kind)).toEqual(['overview']);
  });
});

describe('navigation frames before the live map', () => {
  it('confirm-place, mode and preview are firmware text pages with in-place cursor moves', async () => {
    const r = rig();
    const destination = {
      view: 'destination' as const, primary: 'Central Library', secondary: '1.2 km away',
      hint: 'tap/say confirm · swipe retry', interaction: 'stationary' as const, options: [], selectedIndex: 0,
    };
    expect(await r.hud.showNavigation(destination)).toBe(true);
    expect(r.writes()).toEqual(['rebuild:0:3:0']);
    expect(main(r.rebuilds[0])).toContain('Central Library');
    r.reset();
    const mode = {
      view: 'mode' as const, primary: 'How are you travelling?', secondary: '',
      hint: 'tap choose · swipe select', interaction: 'stationary' as const,
      options: ['Walk', 'Cycle'], selectedIndex: 0,
    };
    expect(await r.hud.showNavigation(mode)).toBe(true);
    expect(r.writes()).toEqual(['upgrade:main', 'upgrade:hint']);
    r.reset();
    expect(await r.hud.showNavigation({ ...mode, selectedIndex: 1 })).toBe(true);
    expect(r.writes()).toEqual(['upgrade:main']);
    expect(r.frames).toEqual([]);
  });

  it('the live map still paints as pixels after recreating the image page', async () => {
    const r = rig();
    const destination = {
      view: 'destination' as const, primary: 'Central Library', secondary: '',
      hint: 'tap confirm', interaction: 'stationary' as const, options: [], selectedIndex: 0,
    };
    expect(await r.hud.showNavigation(destination)).toBe(true);
    r.reset();
    const guidance = {
      view: 'guidance' as const, primary: 'Turn left', secondary: 'onto Main St',
      hint: '2x tap: menu', interaction: 'none' as const, options: [], selectedIndex: 0,
      mode: 'walking' as const, maneuver: 'turn_left' as const, maneuverDistanceM: 80,
    };
    expect(await r.hud.showNavigation(guidance)).toBe(true);
    expect(r.writes()[0]).toBe('rebuild:4:1:0');
    expect(r.frames.map((frame) => frame.kind)).toEqual(['navigation']);
  });
});

describe('the page of an open QuickNote — Even\'s "New note" (FEAT-002)', () => {
  const live = { title: 'QUICKNOTES · NEW NOTE', hint: '● REC 00:00 · tap: pause · left x2: finish', nativeTextPage: true };

  it('takes newer words as in-place upgrades, never a rebuilt page', async () => {
    const r = rig();
    expect(await r.hud.showList(['Listening…'], live)).toBe(true);
    expect(r.writes()).toEqual(['rebuild:0:3:0']);
    r.reset();

    expect(await r.hud.updateLiveTextPage(
      ['We can deliver on the 14th.\n', 'Great, book it.'],
      { ...live, hint: '● REC 00:05 · tap: pause · left x2: finish' },
    )).toBe(true);
    expect(r.writes()).toEqual(['upgrade:main', 'upgrade:hint']);

    r.reset();
    // Only the clock moved: one upgrade, of the footer.
    expect(await r.hud.updateLiveTextPage(
      ['We can deliver on the 14th.\n', 'Great, book it.'],
      { ...live, hint: '● REC 00:10 · tap: pause · left x2: finish' },
    )).toBe(true);
    expect(r.writes()).toEqual(['upgrade:hint']);
    expect(r.frames).toEqual([]);
  });

  it('declines when another page is on the glass, so the caller draws it', async () => {
    const r = rig();
    expect(await r.hud.showList(['A short note'], {
      title: 'QUICKNOTES · DETAIL', hint: '2x tap: back', nativeTextPage: true,
    })).toBe(true);
    r.reset();
    expect(await r.hud.updateLiveTextPage(['words'], live)).toBe(false);
    expect(r.writes()).toEqual([]);
  });

  it('fills the page with the newest words', async () => {
    const r = rig();
    expect(await r.hud.showList(['Listening…'], live)).toBe(true);
    const many = Array.from({ length: 18 }, (_, i) => `Sentence number ${i + 1} of a long meeting.\n`);
    expect(await r.hud.updateLiveTextPage(many, live)).toBe(true);
    const state = (r.hud as unknown as { nativeTextPage?: { page: number; pages: string[]; body: string; hint: string } })
      .nativeTextPage;
    expect(state?.page).toBe((state?.pages.length ?? 1) - 1);
    const lines = state?.body.split('\n') ?? [];
    expect(lines[lines.length - 1]).toBe('Sentence number 18 of a long meeting.');
    // A full page, not the remainder of a page cut from the start.
    expect(lines.length).toBe(READER_TEXT_PAGE.lineCapacity);
    expect(lines.every((line) => line.startsWith('Sentence number '))).toBe(true);
    // One footer line: no page counter, a swipe for the earlier words.
    expect(state?.hint).toBe('● REC 00:00 · tap: pause · swipe: page · 2x tap: finish');

    // One long paragraph (a continuous stretch flows on): still a full page.
    const flowing = Array.from({ length: 40 }, (_, i) => `word${i + 1} and more words`).join(' ');
    expect(await r.hud.updateLiveTextPage([flowing], live)).toBe(true);
    const after = (r.hud as unknown as { nativeTextPage?: { body: string } }).nativeTextPage;
    const rows = after?.body.split('\n') ?? [];
    expect(rows.length).toBe(READER_TEXT_PAGE.lineCapacity);
    expect(rows[rows.length - 1]).toContain('word40 and more words');
  });

  it("carries Undo last input first in the glasses' menu (Even's QuickNote menu)", async () => {
    const r = rig();
    expect(await r.hud.showList(['Listening…'], live)).toBe(true);
    const items = r.rebuilds[r.rebuilds.length - 1]?.menuObject?.menuItems ?? [];
    expect(items[0]).toMatchObject({ itemID: QUICKNOTES_UNDO_MENU_ID, itemName: 'Undo last input' });
    expect(items.map((item: { itemName?: string }) => item.itemName)).toContain('QuickNotes');

    // Any other page keeps the plain capability menu.
    expect(await r.hud.showList(['A short note'], {
      title: 'QUICKNOTES · DETAIL', hint: '2x tap: back', nativeTextPage: true,
    })).toBe(true);
    const other = r.rebuilds[r.rebuilds.length - 1]?.menuObject?.menuItems ?? [];
    expect(other.some((item: { itemID?: number }) => item.itemID === QUICKNOTES_UNDO_MENU_ID)).toBe(false);
  });
});
