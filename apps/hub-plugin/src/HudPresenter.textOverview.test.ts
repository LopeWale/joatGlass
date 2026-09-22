import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { RebuildPageContainer, TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import { HudPresenter, setNativeOverviewForTest } from './HudPresenter';
import { HudPageBuilder } from './HudPageBuilder';
import type { BridgeClient } from './BridgeClient';
import type { HudVisualFrame, HudVisualSurface } from './HudVisualSurface';
import { ContainerId } from './types';

/**
 * The DEFAULT overview: firmware text, one rebuildPageContainer per real
 * change, no image tiles. These tests pin, on the text path, the behaviours
 * the canvas-overview tests pin on the VITE_NATIVE_OVERVIEW=0 fallback —
 * because the first cut of this path silently swallowed Home notifications
 * and Mac replies whenever the tab pane had rows, and only the canvas tests
 * knew.
 */
setNativeOverviewForTest(true);

type Page = RebuildPageContainer & {
  textObject?: Array<{ containerName?: string; content?: string; textColor?: number }>;
  imageObject?: unknown[];
};

function harness() {
  const pages: Page[] = [];
  const upgrades: TextContainerUpgrade[] = [];
  const canvasFrames: HudVisualFrame[] = [];
  const client = {
    require: () => ({
      textContainerUpgrade: async (u: TextContainerUpgrade) => { upgrades.push(u); return true; },
      rebuildPageContainer: async (page: Page) => { pages.push(page); return true; },
    }),
  } as unknown as BridgeClient;
  const visual = {
    paint: async (frame: HudVisualFrame) => { canvasFrames.push(frame); return true; },
    withPageTransition: async (replace: () => Promise<boolean>) => replace(),
  } as unknown as HudVisualSurface;
  const hud = new HudPresenter(client, new HudPageBuilder(), visual);
  const named = (page: Page, name: string) =>
    page.textObject?.find((entry) => entry.containerName === name);
  return { hud, pages, upgrades, canvasFrames, named };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-09-22T10:00:00Z'));
});
afterEach(() => { vi.useRealTimers(); });

describe('the text overview', () => {
  it('is one all-text rebuild — no image containers, no canvas paint', async () => {
    const { hud, pages, canvasFrames } = harness();
    await hud.showText('9:41 JOAT ready bat 100%', ContainerId.StatusBar);
    expect(pages.length).toBeGreaterThan(0);
    const page = pages[pages.length - 1]!;
    expect(page.imageObject ?? []).toHaveLength(0);
    expect(page.textObject?.length).toBe(7);
    expect(canvasFrames.filter((f) => f.kind === 'overview')).toHaveLength(0);
  });

  it('an unchanged repaint costs nothing', async () => {
    const { hud, pages } = harness();
    await hud.showText('9:41 JOAT ready bat 100%', ContainerId.StatusBar);
    const before = pages.length;
    await hud.showText('9:41 JOAT ready bat 100%', ContainerId.StatusBar);
    expect(pages.length).toBe(before);
  });

  it('shows a Home notification in the pane instead of swallowing it', async () => {
    const { hud, pages, named } = harness();
    await hud.acceptPanel({ kind: 'hud.panel', tab: 'news', revision: 1, rows: ['[1m] A · BBC'] });
    await hud.showText('Marta: lunch at noon?', ContainerId.Main, undefined, 'notification');
    const page = pages[pages.length - 1]!;
    expect(named(page, 'pane-title')?.content).toContain('NOTIFICATION');
    expect(named(page, 'pane')?.content).toContain('Marta: lunch at noon?');
  });

  it('keeps a plain Mac reply and its appended stream in the pane', async () => {
    const { hud, pages, upgrades, named } = harness();
    await hud.showText('Meeting starts in', ContainerId.Main, 4);
    expect(named(pages[pages.length - 1]!, 'pane')?.content).toContain('Meeting starts in');
    vi.advanceTimersByTime(400);
    const rebuilds = pages.length;
    await hud.appendText(' 19 minutes.', ContainerId.Main);
    // The stream changes ONE band: an in-place upgrade of the pane
    // (~83ms), never another page rebuild (~165ms).
    expect(pages.length).toBe(rebuilds);
    const last = upgrades[upgrades.length - 1]!;
    expect(last.containerID).toBe(ContainerId.Main);
    expect(last.content).toContain('Meeting starts in 19 minutes.');
  });

  it('a change to one band is one in-place upgrade; a minute tick is one rebuild', async () => {
    const { hud, pages, upgrades } = harness();
    await hud.acceptPanel({ kind: 'hud.panel', tab: 'news', revision: 1, rows: ['[1m] A · BBC'] });
    expect(await hud.stepTab(1)).toBe('news');
    const rebuilds = pages.length;
    const writes = upgrades.length;
    await hud.acceptPanel({ kind: 'hud.panel', tab: 'news', revision: 2, rows: ['[1m] Fresh headline · BBC', '[2m] A · BBC'] });
    expect(pages.length).toBe(rebuilds);
    expect(upgrades.length).toBe(writes + 1);
    expect(upgrades[upgrades.length - 1]!.content).toContain('Fresh headline');
    // Status bar AND clock move together on a minute tick: two bands, one rebuild.
    await hud.showText('10:01 JOAT ready bat 100%', ContainerId.StatusBar);
    expect(pages.length).toBe(rebuilds + 1);
    expect(upgrades.length).toBe(writes + 1);
  });

  it('carries Mac urgency brightness into the pane container', async () => {
    const { hud, pages, named } = harness();
    await hud.showText('urgent', ContainerId.Main, 3);
    expect(named(pages[pages.length - 1]!, 'pane')?.textColor).toBe(3);
  });

  it('states mic privacy from structured state, not status copy', async () => {
    const { hud, pages, named } = harness();
    hud.setMicState(false);
    await hud.showText('9:41 JOAT mic on says the copy', ContainerId.StatusBar);
    expect(named(pages[pages.length - 1]!, 'status')?.content).toContain('MIC OFF');
    hud.setMicState(true);
    await hud.showText('9:42 JOAT paused', ContainerId.StatusBar);
    expect(named(pages[pages.length - 1]!, 'status')?.content).toContain('MIC ON');
  });

  it('renders the reconnecting boot state, not a stale dashboard', async () => {
    const { hud, pages, named } = harness();
    await hud.showText('9:41 JOAT reconnecting', ContainerId.StatusBar);
    const page = pages[pages.length - 1]!;
    expect(named(page, 'pane-title')?.content).toContain('RECONNECTING');
    expect(named(page, 'pane')?.content).toContain('Restoring JOAT on your Mac');
  });

  it('the resting Pomodoro card counts down, one upgrade per minute', async () => {
    const { hud, pages, upgrades, named } = harness();
    const now = Date.now();
    await hud.acceptPanel({
      kind: 'hud.panel', tab: 'pomodoro', revision: 1, rows: ['Pause', 'Reset'],
      pomodoro: { phase: 'focus', running: true, lengthMs: 25 * 60_000, remainingMs: 24 * 60_000 + 30_000, endsAtUnixMs: now + 24 * 60_000 + 30_000, completed: 1 },
    });
    expect(await hud.stepTab(1)).toBe('news');
    expect(await hud.stepTab(1)).toBe('pomodoro');
    const card = named(pages[pages.length - 1]!, 'pane')?.content ?? '';
    expect(card).toContain('25 min left');
    expect(card).toContain('Focus · block 2 of 4');
    expect(card).toContain('● ○ ○ ○');
    const rebuilds = pages.length;
    const writes = upgrades.length;
    await vi.advanceTimersByTimeAsync(31_000); // the minute digit moves
    expect(pages.length).toBe(rebuilds);
    expect(upgrades.length).toBe(writes + 1);
    expect(upgrades[upgrades.length - 1]!.content).toContain('24 min left');
  });

  it('a swipe is exactly one write and moves the > cursor', async () => {
    const { hud, pages, upgrades, named } = harness();
    await hud.acceptPanel({ kind: 'hud.panel', tab: 'news', revision: 1, rows: ['[1m] A · BBC'] });
    await hud.showText('9:41 JOAT ready bat 100%', ContainerId.StatusBar);
    // Tab card showing: a swipe changes rail, pane title and pane — one rebuild.
    let rebuilds = pages.length;
    let writes = upgrades.length;
    expect(await hud.stepTab(1)).toBe('news');
    expect(pages.length).toBe(rebuilds + 1);
    expect(upgrades.length).toBe(writes);
    expect(named(pages[pages.length - 1]!, 'tabs')?.content).toContain('> NEWS');
    // A reply owns the pane: a swipe changes only the rail — one upgrade.
    await hud.showText('home', ContainerId.Main);
    rebuilds = pages.length;
    writes = upgrades.length;
    expect(await hud.stepTab(1)).toBe('pomodoro');
    expect(pages.length).toBe(rebuilds);
    expect(upgrades.length).toBe(writes + 1);
    expect(upgrades[upgrades.length - 1]!.content).toContain('> POMODORO');
  });
});
