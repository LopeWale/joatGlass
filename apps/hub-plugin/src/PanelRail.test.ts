import { describe, expect, it } from 'vitest';
import {
  PanelRail,
  formatCountdown,
  formatMinutesLeft,
  freshnessLabel,
  panelTabForTitle,
  pomodoroRemainingMs,
  pomodoroTickDelayMs,
  type HudPanelMessage,
} from './PanelRail';
import { PANEL_TABS, panelRequestId, parsePanelRequestId } from './types';

/**
 * The overview's right pane is a rail of tabs the wearer swipes between.
 * Which tab is active is local; what the Mac pushes is cached so a swipe
 * never waits on a round trip. These pin that ownership split.
 */

const news = (revision: number, rows: string[]): HudPanelMessage => ({
  kind: 'hud.panel', tab: 'news', revision, rows,
});

describe('PanelRail', () => {
  it('starts on Notifications, the tab the resting screen already shows', () => {
    expect(new PanelRail().active()).toBe('notifications');
    expect(PANEL_TABS[0]).toBe('notifications');
  });

  it('steps along the rail and clamps at both ends instead of wrapping', () => {
    const rail = new PanelRail();
    expect(rail.step(-1)).toBeUndefined();
    expect(rail.step(1)).toBe('news');
    expect(rail.step(1)).toBe('pomodoro');
    expect(rail.step(1)).toBe('search');
    expect(rail.step(1)).toBeUndefined();
    expect(rail.active()).toBe('search');
    rail.reset();
    expect(rail.active()).toBe('notifications');
  });

  it('caches the newest push per tab and mints the revision a tap must echo', () => {
    const rail = new PanelRail();
    expect(rail.requestId('news')).toBe(panelRequestId('news', 0));
    rail.accept(news(3, ['[2m] First · BBC']), 1_000);
    rail.accept(news(4, ['[1m] Second · NPR']), 2_000);
    expect(rail.content('news')).toMatchObject({
      revision: 4, rows: ['[1m] Second · NPR'], receivedAtMs: 2_000,
    });
    expect(rail.requestId('news')).toBe('panel-news-4');
    expect(parsePanelRequestId(rail.requestId('news'))).toEqual({ tab: 'news', revision: 4 });
    // A brain that restarted counts from zero again; the Mac is authoritative.
    rail.accept(news(0, []), 3_000);
    expect(rail.content('news')?.revision).toBe(0);
  });

  it('describes the active tab for the renderer without leaking other tabs', () => {
    const rail = new PanelRail();
    rail.accept(news(1, ['[2m] First · BBC']));
    expect(rail.visual()).toEqual({ activeTab: 'notifications' });
    rail.setActive('news');
    expect(rail.visual()).toMatchObject({
      activeTab: 'news', content: { tab: 'news', rows: ['[2m] First · BBC'] },
    });
    rail.setActive('search');
    expect(rail.visual()).toEqual({ activeTab: 'search' });
  });

  it('survives the host background migration as plain JSON', () => {
    const rail = new PanelRail();
    rail.accept({
      kind: 'hud.panel', tab: 'pomodoro', revision: 2, rows: ['Pause', 'Reset'],
      pomodoro: {
        phase: 'focus', running: true, remainingMs: 1_000, lengthMs: 1_500_000,
        completed: 1, endsAtUnixMs: 99,
      },
    });
    rail.setActive('pomodoro');
    const restored = new PanelRail();
    expect(restored.import(JSON.parse(JSON.stringify(rail.export())))).toBe(true);
    expect(restored.visual()).toEqual(rail.visual());
    expect(new PanelRail().import({ activeTab: 'lunch' })).toBe(false);
    expect(new PanelRail().import({
      activeTab: 'news',
      contents: { news: { revision: 'x', rows: [] }, bogus: { revision: 1, rows: [] } },
    })).toBe(true);
  });

  it('maps focus-page titles back to their tab, detail pages included', () => {
    expect(panelTabForTitle('NEWS')).toBe('news');
    expect(panelTabForTitle('news · article')).toBe('news');
    expect(panelTabForTitle('SEARCH · RESULT')).toBe('search');
    expect(panelTabForTitle('NOTIFICATIONS')).toBeUndefined();
    expect(panelTabForTitle('MESSAGES')).toBeUndefined();
    expect(panelTabForTitle(undefined)).toBeUndefined();
  });
});

describe('pomodoro time on the plugin clock', () => {
  const running = {
    phase: 'focus' as const, running: true, remainingMs: 0, lengthMs: 1_500_000,
    completed: 0, endsAtUnixMs: 1_000_000,
  };

  it('counts down from the Mac-sent end instant, never below zero', () => {
    expect(pomodoroRemainingMs(running, 1_000_000 - 61_500)).toBe(61_500);
    expect(pomodoroRemainingMs(running, 2_000_000)).toBe(0);
    expect(pomodoroRemainingMs({ ...running, running: false, remainingMs: 4_000 }, 0)).toBe(4_000);
  });

  it('formats like a timer, and rounds minutes up so it never says 0 min left early', () => {
    expect(formatCountdown(61_500)).toBe('01:02');
    expect(formatCountdown(0)).toBe('00:00');
    expect(formatCountdown(1_500_000)).toBe('25:00');
    expect(formatMinutesLeft(61_500)).toBe('2 min left');
    expect(formatMinutesLeft(500)).toBe('1 min left');
    expect(formatMinutesLeft(0)).toBe('0 min left');
  });

  it('schedules the next repaint exactly when a digit will move', () => {
    expect(pomodoroTickDelayMs(running, 1_000_000 - 61_500, 1000)).toBe(500);
    expect(pomodoroTickDelayMs(running, 1_000_000 - 61_500, 60_000)).toBe(1_500);
    expect(pomodoroTickDelayMs(running, 1_000_000 - 60_000, 60_000)).toBe(60_000);
    expect(pomodoroTickDelayMs({ ...running, running: false }, 0, 1000)).toBeUndefined();
    expect(pomodoroTickDelayMs(running, 1_000_000, 1000)).toBeUndefined();
  });

  it('labels feed freshness from receipt time', () => {
    expect(freshnessLabel(1_000, 30_000)).toBe('updated now');
    expect(freshnessLabel(0, 3 * 60_000 + 5)).toBe('updated 3m ago');
    expect(freshnessLabel(0, 2 * 3_600_000)).toBe('updated 2h ago');
  });
});

describe('pomodoro labels shared by the pane and the page', () => {
  it('names the block, the break, the paused state, and the idle offer', async () => {
    const { pomodoroBlockLabel, pomodoroCycleDots } = await import('./PanelRail');
    const base = { running: true, remainingMs: 1, lengthMs: 1_500_000, completed: 1 };
    expect(pomodoroBlockLabel({ ...base, phase: 'focus' })).toBe('Focus · block 2 of 4');
    expect(pomodoroBlockLabel({ ...base, phase: 'focus', completed: 4 })).toBe('Focus · block 4 of 4');
    expect(pomodoroBlockLabel({ ...base, phase: 'break' })).toBe('Break · 1 of 4 done');
    expect(pomodoroBlockLabel({ ...base, phase: 'long_break', running: false })).toBe('Paused · Long break · 1 of 4 done');
    expect(pomodoroBlockLabel({ ...base, phase: 'idle', running: false })).toBe('Ready · 25 min focus');
    expect(pomodoroCycleDots(0)).toBe('○ ○ ○ ○');
    expect(pomodoroCycleDots(3)).toBe('● ● ● ○');
  });
});
