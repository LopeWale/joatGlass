import { describe, expect, it } from 'vitest';
import { pxTruncate } from '@evenrealities/pretext';
import {
  OVERVIEW_DASHBOARD,
  PANEL_EXPANDED_HINT,
  PANEL_OVERVIEW_HINT,
  PANEL_RAIL,
  POMODORO_EXPANDED_HINT,
  HudCanvasRenderer,
  usesPanelReaderLayout,
} from './HudCanvasRenderer';
import { PANEL_TABS } from './types';
import type { PanelRailVisual } from './PanelRail';
import type { HudVisualFrame } from './HudVisualSurface';

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

describe('timer recovery failure presentation', () => {
  function draw(mode: 'overview' | 'expanded', error: boolean) {
    const painted: Array<{ text: string; font: string }> = [];
    const state: Record<string, unknown> = { font: '12px monospace' };
    state.measureText = (value: string) => ({ width: value.length * 7 });
    state.fillText = (text: string) => painted.push({ text, font: String(state.font) });
    const context = new Proxy(state, { get: (target, key) => target[String(key)] ?? (() => {}) });
    const rail: PanelRailVisual = { activeTab: 'pomodoro', content: {
      tab: 'pomodoro', revision: 1, receivedAtMs: 0, rows: ['Reset'],
      ...(error ? { headline: 'Timer unavailable', empty: 'Saved timer cannot be read. Reset to start again.' } : {}),
      pomodoro: { phase: 'idle', running: false, remainingMs: 1_500_000, lengthMs: 1_500_000, completed: 0 },
    } };
    const renderer = Object.create(HudCanvasRenderer.prototype) as {
      drawPanelPane(context: CanvasRenderingContext2D, rail: PanelRailVisual, icons: object): void;
      drawPomodoroPage(context: CanvasRenderingContext2D, frame: Extract<HudVisualFrame, { kind: 'list' }>): void;
    };
    if (mode === 'overview') renderer.drawPanelPane(context as unknown as CanvasRenderingContext2D, rail, {});
    else renderer.drawPomodoroPage(context as unknown as CanvasRenderingContext2D, {
      kind: 'list', status: 'JOAT', micOn: false, title: 'POMODORO',
      items: ['Reset'], selectedIndex: 0, selectable: true, hint: POMODORO_EXPANDED_HINT, rail,
    });
    return painted;
  }

  it.each(['overview', 'expanded'] as const)('shows recovery failure without a false ready/countdown in %s', (mode) => {
    const painted = draw(mode, true);
    const text = painted.map((entry) => entry.text).join(' ');
    expect(text).toContain('Timer unavailable');
    expect(text).toContain('Saved timer cannot be read.');
    expect(text).not.toContain('Ready');
    expect(text).not.toContain('25:00');
    expect(painted.every((entry) => !/^(?:48|56)px/.test(entry.font))).toBe(true);
    expect(painted.filter((entry) => /Timer unavailable|Saved timer/.test(entry.text))
      .every((entry) => /Helvetica Neue/.test(entry.font))).toBe(true);
  });

  it('keeps the normal idle overview ready when recovery has no error', () => {
    expect(draw('overview', false).some((entry) => entry.text === 'Ready')).toBe(true);
  });
});

describe('panel page copy', () => {
  it('names only wired gestures, in the one vocabulary, and fits a firmware hint row', () => {
    // The hint row is a 576px firmware text container with 4px padding;
    // pxTruncate is the measured bound (the old 46-char cap was the canvas
    // footer's). One vocabulary everywhere: tap, swipe, long-press,
    // double-click — "hold" was the odd one out (GesturePolicy.formatHint).
    for (const hint of [PANEL_OVERVIEW_HINT, PANEL_EXPANDED_HINT, POMODORO_EXPANDED_HINT]) {
      expect(pxTruncate(hint, 576 - 8)).toBe(hint);
      expect(hint).not.toMatch(/\bhold\b/);
    }
    expect(PANEL_OVERVIEW_HINT).toContain('swipe');
    expect(PANEL_EXPANDED_HINT).toContain('long-press: refresh');
    // Long press is the mic on every resting tab and on the pomodoro page
    // (excluded from the refresh carve-out), so both must say so. The
    // pomodoro hint must NOT promise a refresh it will never send.
    expect(PANEL_OVERVIEW_HINT).toContain('long-press: mic');
    expect(POMODORO_EXPANDED_HINT).toContain('long-press: mic');
    expect(POMODORO_EXPANDED_HINT).not.toContain('refresh');
  });

  it('routes only an opened headline or result through the shared reader', () => {
    expect(usesPanelReaderLayout('NEWS · ARTICLE')).toBe(true);
    expect(usesPanelReaderLayout('search · result')).toBe(true);
    expect(usesPanelReaderLayout('NEWS')).toBe(false);
    expect(usesPanelReaderLayout('MESSAGES · MESSAGE')).toBe(false);
  });
});
