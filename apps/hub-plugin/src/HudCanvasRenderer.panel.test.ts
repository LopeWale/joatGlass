import { describe, expect, it } from 'vitest';
import {
  OVERVIEW_DASHBOARD,
  PANEL_EXPANDED_HINT,
  PANEL_OVERVIEW_HINT,
  PANEL_RAIL,
  POMODORO_EXPANDED_HINT,
  usesPanelReaderLayout,
} from './HudCanvasRenderer';
import { PANEL_TABS } from './types';

/**
 * The rail's geometry is a latency decision: the frame ships as four
 * 288×144 tiles and only changed tiles are uploaded, so everything a tab
 * swipe can change must live east of x=288. These pin that, and the
 * footer copy every panel page promises.
 */

describe('tab rail geometry', () => {
  it('keeps the rail, the pane edge, and the pane inside the two right tiles', () => {
    expect(PANEL_RAIL.x).toBeGreaterThanOrEqual(288);
    expect(OVERVIEW_DASHBOARD.dividerX).toBeGreaterThan(PANEL_RAIL.x + PANEL_RAIL.iconSize);
    expect(OVERVIEW_DASHBOARD.paneX).toBeGreaterThan(OVERVIEW_DASHBOARD.dividerX);
    expect(OVERVIEW_DASHBOARD.paneX + OVERVIEW_DASHBOARD.paneWidth).toBeLessThanOrEqual(548);
  });

  it('has one evenly spaced slot per tab between the spine and the footer', () => {
    expect(PANEL_RAIL.slotY).toHaveLength(PANEL_TABS.length);
    const pitches = PANEL_RAIL.slotY.slice(1).map((y, i) => y - PANEL_RAIL.slotY[i]!);
    expect(new Set(pitches).size).toBe(1);
    expect(PANEL_RAIL.slotY[0]).toBeGreaterThanOrEqual(OVERVIEW_DASHBOARD.dividerTopY);
    expect(PANEL_RAIL.slotY[PANEL_RAIL.slotY.length - 1]! + PANEL_RAIL.iconSize)
      .toBeLessThanOrEqual(OVERVIEW_DASHBOARD.dividerBottomY);
  });

  it('leaves the expanded rail clear of the list selection rail at x=36', () => {
    expect(PANEL_RAIL.expandedX + PANEL_RAIL.iconSize).toBeLessThan(36);
  });

  it('keeps the clock column centred in the left half', () => {
    expect(OVERVIEW_DASHBOARD.clockCenterX).toBe(144);
  });
});

describe('panel page copy', () => {
  it('names only wired gestures and fits the 46-character footer', () => {
    for (const hint of [PANEL_OVERVIEW_HINT, PANEL_EXPANDED_HINT, POMODORO_EXPANDED_HINT]) {
      expect(hint.length).toBeLessThanOrEqual(46);
    }
    expect(PANEL_OVERVIEW_HINT).toContain('swipe');
    expect(PANEL_EXPANDED_HINT).toContain('hold');
    expect(POMODORO_EXPANDED_HINT).not.toContain('hold');
  });

  it('routes only an opened headline or result through the shared reader', () => {
    expect(usesPanelReaderLayout('NEWS · ARTICLE')).toBe(true);
    expect(usesPanelReaderLayout('search · result')).toBe(true);
    expect(usesPanelReaderLayout('NEWS')).toBe(false);
    expect(usesPanelReaderLayout('MESSAGES · MESSAGE')).toBe(false);
  });
});
