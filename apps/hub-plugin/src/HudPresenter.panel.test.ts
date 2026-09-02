import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import { HudPresenter } from './HudPresenter';
import { HudPageBuilder } from './HudPageBuilder';
import type { BridgeClient } from './BridgeClient';
import type { HudVisualFrame, HudVisualSurface } from './HudVisualSurface';
import { ContainerId } from './types';

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
  const localTicks: boolean[] = [];
  const visual = {
    paint: async (frame: HudVisualFrame, localTick = false) => {
      frames.push(frame);
      localTicks.push(localTick);
      return true;
    },
  } as HudVisualSurface;
  const hud = new HudPresenter(fakeClient(), new HudPageBuilder(), visual);
  return { hud, frames, localTicks };
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

  it('an expanded tab keeps its rail on the list frame; other lists carry none', async () => {
    const { hud, frames } = presenter();
    await hud.acceptPanel({ kind: 'hud.panel', tab: 'search', revision: 4, rows: ['Result · site'] });
    await hud.showList(['Result · site'], { title: 'SEARCH', railTab: 'search', selectable: true });
    expect(frames[frames.length - 1]).toMatchObject({
      kind: 'list', title: 'SEARCH', rail: { activeTab: 'search', content: { revision: 4 } },
    });
    await hud.showList(['Loading…'], { title: 'MESSAGES' });
    expect(frames[frames.length - 1]).not.toHaveProperty('rail');
  });

  it('a running pomodoro ticks by the minute on the overview and by the second expanded', async () => {
    const { hud, frames, localTicks } = presenter();
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
    expect(localTicks[localTicks.length - 1]).toBe(true);

    await hud.showList(['Pause', 'Reset'], { title: 'POMODORO', railTab: 'pomodoro', selectable: true });
    const expanded = frames.length;
    await vi.advanceTimersByTimeAsync(2_000);
    expect(frames.length).toBeGreaterThanOrEqual(expanded + 2);

    // Off the glass, the ticker stops: no repaint of a page that is gone.
    await hud.showList(['x'], { title: 'MESSAGES' });
    const away = frames.length;
    await vi.advanceTimersByTimeAsync(5_000);
    expect(frames).toHaveLength(away);
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
