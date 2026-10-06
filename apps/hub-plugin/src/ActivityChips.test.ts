import { getAdvW } from '@evenrealities/pretext';
import { describe, expect, it } from 'vitest';
import { isRouteProgressView, navigationChip, pomodoroChip } from './ActivityChips';
import { navigationVisualFixture, type NavigationHudModel } from './NavigationHud';
import type { HudPanelPomodoro } from './types';

const focus = (remainingMs: number, running = true): HudPanelPomodoro => ({
  phase: 'focus', running, remainingMs, lengthMs: 1_500_000, completed: 1,
  ...(running ? { endsAtUnixMs: 1_000_000 + remainingMs } : {}),
});

const guidance = (overrides: Partial<NavigationHudModel> = {}): NavigationHudModel => ({
  ...navigationVisualFixture('nav-guidance'), ...overrides,
});

describe('the Pomodoro chip', () => {
  it('reads the phase and whole minutes, rounding up so it never says 0 early', () => {
    expect(pomodoroChip(focus(12 * 60_000), 1_000_000)).toBe('Focus 12m');
    expect(pomodoroChip(focus(11 * 60_000 + 1), 1_000_000)).toBe('Focus 12m');
    expect(pomodoroChip(focus(30_000), 1_000_000)).toBe('Focus 1m');
    expect(pomodoroChip({ ...focus(4 * 60_000), phase: 'break' }, 1_000_000)).toBe('Break 4m');
    expect(pomodoroChip({ ...focus(9 * 60_000), phase: 'long_break' }, 1_000_000)).toBe('Break 9m');
  });

  it('says paused, says done at the end, and says nothing while idle', () => {
    expect(pomodoroChip(focus(7 * 60_000, false), 1_000_000)).toBe('Paused 7m');
    expect(pomodoroChip(focus(0), 1_000_000)).toBe('Focus done');
    expect(pomodoroChip({ ...focus(0, false), phase: 'idle' }, 1_000_000)).toBeUndefined();
  });
});

describe('the navigation chip', () => {
  it('is the next turn as a firmware arrow and its distance', () => {
    expect(navigationChip(guidance({ maneuver: 'turn_right', maneuverDistanceM: 80 }))).toBe('→ 80 m');
    expect(navigationChip(guidance({ maneuver: 'turn_left', maneuverDistanceM: 1_240 }))).toBe('← 1.2 km');
    expect(navigationChip(guidance({ maneuver: 'slight_right', maneuverDistanceM: 300 }))).toBe('↗ 300 m');
    expect(navigationChip(guidance({ maneuver: 'arrive', maneuverDistanceM: 30 }))).toBe('◆ 30 m');
    expect(navigationChip(guidance({ maneuver: 'continue', maneuverDistanceM: undefined }))).toBe('↑');
  });

  it('names a route in doubt in a word, and stays silent on the pages that ask the wearer', () => {
    expect(navigationChip(guidance({ view: 'off_route' }))).toBe('Off route');
    expect(navigationChip(guidance({ view: 'wrong_way' }))).toBe('Wrong way');
    expect(navigationChip(guidance({ view: 'arrived' }))).toBe('Arrived');
    for (const view of ['destination', 'repair', 'mode', 'preview', 'error'] as const) {
      expect(navigationChip(guidance({ view }))).toBeUndefined();
      expect(isRouteProgressView(guidance({ view }))).toBe(false);
    }
    expect(isRouteProgressView(guidance())).toBe(true);
  });

  it('uses only glyphs the firmware font has', () => {
    for (const glyph of ['↑', '↗', '→', '↘', '↓', '↙', '←', '↖', '◆']) {
      expect(getAdvW(glyph.codePointAt(0)!)).toBeGreaterThan(0);
    }
  });
});
