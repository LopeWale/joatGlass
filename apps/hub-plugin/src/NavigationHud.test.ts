import { describe, expect, it } from 'vitest';

import {
  NAVIGATION_VISUAL_STATES,
  formatNavigationDistance,
  navigationVisualFixture,
} from './NavigationHud';

describe('navigation HUD contract', () => {
  it('defines every wearer-visible state needed for destination recovery and guidance', () => {
    expect(NAVIGATION_VISUAL_STATES).toEqual([
      'nav-destination',
      'nav-repair',
      'nav-mode',
      'nav-preview',
      'nav-guidance',
      'nav-uncertain',
      'nav-wrong-way',
      'nav-off-route',
      'nav-rerouting',
      'nav-arrived',
      'nav-error',
    ]);
    expect(NAVIGATION_VISUAL_STATES.map((state) => navigationVisualFixture(state).view))
      .toEqual([
        'destination',
        'repair',
        'mode',
        'preview',
        'guidance',
        'uncertain',
        'wrong_way',
        'off_route',
        'rerouting',
        'arrived',
        'error',
      ]);
  });

  it('keeps movement views glanceable and non-interactive', () => {
    for (const state of [
      'nav-guidance',
      'nav-uncertain',
      'nav-wrong-way',
      'nav-off-route',
      'nav-rerouting',
    ] as const) {
      const fixture = navigationVisualFixture(state);
      expect(fixture.interaction).toBe('none');
      expect(fixture.hint).not.toMatch(/tap|swipe/i);
    }
  });

  it('makes destination repair and explicit walking/cycling selection visible', () => {
    const destination = navigationVisualFixture('nav-destination');
    expect(destination.primary).toBe('Cajun Corner');
    expect(destination.secondary).toContain('2301 N Lincoln Ave');
    expect(destination.hint).toMatch(/confirm/i);

    const repair = navigationVisualFixture('nav-repair');
    expect(repair.options).toHaveLength(3);
    expect(repair.options[2]).toMatch(/retry/i);
    expect(repair.hint).not.toMatch(/long-press/i);

    const mode = navigationVisualFixture('nav-mode');
    expect(mode.options).toEqual(['Walk · 18 min', 'Cycle · 7 min']);
    expect(mode.selectedIndex).toBe(0);
  });

  it('names the exact preview swipe directions and avoids unsafe warning claims', () => {
    expect(navigationVisualFixture('nav-preview').hint).toBe(
      'tap: start · up: mode · down: place',
    );
    expect(navigationVisualFixture('nav-uncertain').hint).toBe('Pause safely · route held');
    expect(navigationVisualFixture('nav-off-route').secondary).toBe('Stop when safe');
    expect(navigationVisualFixture('nav-rerouting')).toMatchObject({
      secondary: 'Finding a safer path',
      hint: 'Old directions paused',
    });
  });

  it('renders metric distance without fake precision', () => {
    expect(formatNavigationDistance(0)).toBe('now');
    expect(formatNavigationDistance(48)).toBe('50 m');
    expect(formatNavigationDistance(349)).toBe('350 m');
    expect(formatNavigationDistance(999)).toBe('1.0 km');
    expect(formatNavigationDistance(1_240)).toBe('1.2 km');
    expect(formatNavigationDistance(12_800)).toBe('13 km');
  });
});
