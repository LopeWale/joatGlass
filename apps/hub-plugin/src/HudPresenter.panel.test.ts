import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import { HudPresenter, setNativeOverviewForTest } from './HudPresenter';
import { HudPageBuilder } from './HudPageBuilder';
import type { BridgeClient } from './BridgeClient';
import type { HudVisualFrame, HudVisualSurface } from './HudVisualSurface';
import { ContainerId } from './types';

// These tests pin the IMAGE overview (VITE_NATIVE_OVERVIEW=0). The text
// overview, which is Home by default, has its own: HudPresenter.textOverview.test.ts.
setNativeOverviewForTest(false);

/**
 * The presenter carries the tab rail into every overview frame, repaints
 * the resting pane when the rail moves or the shown tab is pushed, keeps
 * an expanded tab's rail on its list frame, and ticks a running pomodoro
 * on the plugin clock — one repaint per digit, none while it is off-glass.
 */

function fakeClient(): BridgeClient {
  return {
    require: () => ({
      textContainerUpgrade: async (_u: TextContainerUpgrade) => true,
      rebuildPageContainer: async () => true,
    }),
  } as unknown as BridgeClient;
}

function presenter() {
  const frames: HudVisualFrame[] = [];
  const visual = {
    paint: async (frame: HudVisualFrame) => {
      frames.push(frame);
      return true;
    },
    withPageTransition: async (replace: () => Promise<boolean>) => replace(),
  } as unknown as HudVisualSurface;
  const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);
  return { hud, frames };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-09-02T10:00:00Z'));
});

afterEach(() => {
  vi.useRealTimers();
});

describe('HudPresenter — the tab rail', () => {
  it('every overview frame names the active tab, Notifications first', async () => {
    const { hud, frames } = presenter();
    await hud.showText('9:41   JOAT ready', ContainerId.StatusBar);
    expect(frames[frames.length - 1]).toMatchObject({
      kind: 'overview', rail: { activeTab: 'notifications' },
    });
  });

  it('a swipe steps the rail and repaints the resting pane once', async () => {
    const { hud, frames } = presenter();
    await hud.showText('home', ContainerId.Main);
    const before = frames.length;
    expect(await hud.stepTab(1)).toBe('news');
    expect(frames).toHaveLength(before + 1);
    expect(frames[frames.length - 1]).toMatchObject({ kind: 'overview', rail: { activeTab: 'news' } });
    // At the end of the rail nothing changes, so nothing is painted.
    expect(await hud.stepTab(-1)).toBe('notifications');
    expect(await hud.stepTab(-1)).toBeUndefined();
    expect(frames).toHaveLength(before + 2);
  });

  it('a push repaints only when its tab is the one on the glass', async () => {
    const { hud, frames } = presenter();
    await hud.showText('home', ContainerId.Main);
    const before = frames.length;
    await hud.acceptPanel({ kind: 'hud.panel', tab: 'news', revision: 1, rows: ['[1m] A · BBC'] });
    expect(frames).toHaveLength(before); // cached, not shown
    await hud.setActiveTab('news');
    expect(frames[frames.length - 1]).toMatchObject({
      kind: 'overview',
      rail: { activeTab: 'news', content: { revision: 1, rows: ['[1m] A · BBC'] } },
    });
    const shown = frames.length;
    await hud.acceptPanel({ kind: 'hud.panel', tab: 'news', revision: 2, rows: ['[0m] B · NPR'] });
    expect(frames).toHaveLength(shown + 1);
    expect(frames[frames.length - 1]).toMatchObject({
      rail: { content: { revision: 2 } },
    });
  });


  it('a running pomodoro ticks by the minute on the overview and asks the expanded list to rebuild', async () => {
    const { hud, frames } = presenter();
    const endsAtUnixMs = Date.now() + 90_500; // 1:30.5 left
    await hud.acceptPanel({
      kind: 'hud.panel', tab: 'pomodoro', revision: 1, rows: ['Pause', 'Reset'],
      pomodoro: {
        phase: 'focus', running: true, remainingMs: 90_500, lengthMs: 1_500_000,
        completed: 0, endsAtUnixMs,
      },
    });
    await hud.setActiveTab('pomodoro');
    const painted = frames.length;
    await vi.advanceTimersByTimeAsync(30_000);
    expect(frames).toHaveLength(painted); // 1:00.5 left: the minute digit has not moved
    await vi.advanceTimersByTimeAsync(1_000);
    expect(frames).toHaveLength(painted + 1); // 0:59.5 → "1 min left" still, but the boundary crossed

    // Expanded, the timer is a firmware list: its owner rebuilds it on the
    // minute, and no image frame is painted for it.
    const ticks: number[] = [];
    hud.onNativePanelTick = () => { ticks.push(Date.now()); };
    await hud.acceptPanel({
      kind: 'hud.panel', tab: 'pomodoro', revision: 2, rows: ['Pause', 'Reset'],
      pomodoro: {
        phase: 'focus', running: true, remainingMs: 600_500, lengthMs: 1_500_000,
        completed: 0, endsAtUnixMs: Date.now() + 600_500,
      },
    });
    await hud.showList(['Pause', 'Reset'], {
      title: 'POMODORO', railTab: 'pomodoro', selectable: true, nativeFirmwareList: true,
    });
    const expanded = frames.length;
    await vi.advanceTimersByTimeAsync(61_000);
    expect(ticks.length).toBeGreaterThanOrEqual(1);
    expect(frames).toHaveLength(expanded);

    // Off the glass, the ticker stops.
    await hud.showList(['x'], { title: 'MESSAGES', nativeFirmwareList: true });
    const after = ticks.length;
    await vi.advanceTimersByTimeAsync(120_000);
    expect(ticks).toHaveLength(after);
  });

  it('the rail survives the host background migration', async () => {
    const { hud } = presenter();
    await hud.acceptPanel({ kind: 'hud.panel', tab: 'news', revision: 9, rows: ['[1m] A · BBC'] });
    await hud.setActiveTab('news');
    const saved = JSON.parse(JSON.stringify(hud.exportBackgroundState())) as Record<string, unknown>;
    const restored = presenter().hud;
    expect(restored.importBackgroundState(saved)).toBe(true);
    expect(restored.activeTab()).toBe('news');
    expect(restored.rail.content('news')?.revision).toBe(9);
  });
});
