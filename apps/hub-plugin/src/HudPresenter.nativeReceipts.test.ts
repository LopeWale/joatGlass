import { describe, expect, it, vi } from 'vitest';
import type { RebuildPageContainer, TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import { HudPageBuilder } from './HudPageBuilder';
import { HudPresenter, setNativeOverviewForTest } from './HudPresenter';
import type { HudVisualFrame } from './HudVisualSurface';
import type { ProductivityHudModel } from './types';

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
