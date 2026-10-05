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

describe('the guidance pane without a street tile', () => {
  const route = {
    geometry: [
      { latitude: 0, longitude: 0 },
      { latitude: 0, longitude: 0.009 },
      { latitude: 0.009, longitude: 0.009 },
    ],
  };

  it('centres on the wearer at the tiles\' own zoom, or fits the whole route before the first fix', async () => {
    const { navigationMapFallbackCentre, NAVIGATION_MAP_PANEL, projectNavigationMapToPanel } = await import('./HudCanvasRenderer');
    expect(navigationMapFallbackCentre({ ...route, position: { latitude: 0, longitude: 0.001 } }))
      .toEqual({ latitude: 0, longitude: 0.001, zoom: 16 });
    const fitted = navigationMapFallbackCentre(route);
    expect(fitted).toMatchObject({ latitude: 0.0045, longitude: 0.0045 });
    expect(fitted!.zoom).toBeLessThan(16);
    const projection = projectNavigationMapToPanel(route, fitted!)!;
    for (const [x, y] of projection.points) {
      expect(x).toBeGreaterThanOrEqual(NAVIGATION_MAP_PANEL.x);
      expect(x).toBeLessThanOrEqual(NAVIGATION_MAP_PANEL.x + NAVIGATION_MAP_PANEL.width);
      expect(y).toBeGreaterThanOrEqual(NAVIGATION_MAP_PANEL.y);
      expect(y).toBeLessThanOrEqual(NAVIGATION_MAP_PANEL.y + NAVIGATION_MAP_PANEL.height);
    }
    expect(navigationMapFallbackCentre({ geometry: [{ latitude: 0, longitude: 0 }] })).toBeUndefined();
  });

  it('draws the route\'s shape and the wearer in the pane when no tile has arrived', async () => {
    const { HudCanvasRenderer } = await import('./HudCanvasRenderer');
    const strokes: number[] = [];
    const arcs: Array<[number, number]> = [];
    let images = 0;
    const texts: Array<{ text: string; x: number }> = [];
    const state: Record<string, unknown> = {
      font: '', textAlign: 'left', lineWidth: 0,
      measureText: (value: string) => ({ width: value.length * 6 }),
      fillText: (text: string, x: number) => texts.push({ text, x }),
      stroke: () => strokes.push(state.lineWidth as number),
      arc: (x: number, y: number) => arcs.push([x, y]),
      drawImage: () => { images += 1; },
    };
    const context = new Proxy(state, {
      get: (target, key) => (key in target ? target[String(key)] : () => undefined),
      set: (target, key, value) => { target[String(key)] = value; return true; },
    }) as unknown as CanvasRenderingContext2D;
    const renderer = Object.create(HudCanvasRenderer.prototype) as InstanceType<typeof HudCanvasRenderer>;
    (renderer as unknown as { drawNavigation(c: unknown, n: unknown): void }).drawNavigation(context, {
      view: 'guidance', primary: 'Turn right', secondary: 'onto Test Ave', hint: 'Then continue',
      interaction: 'none', options: [], selectedIndex: 0, mode: 'walking',
      maneuver: 'turn_right', maneuverDistanceM: 80, remainingDistanceM: 900, etaMinutes: 13,
      progress: 0.1,
      map: { ...route, position: { latitude: 0, longitude: 0.001 } },
    });
    // No tile was blitted, the route was traced once in the thin "shape"
    // weight, and the wearer's marker was drawn — nothing disappeared.
    expect(images).toBe(0);
    expect(strokes).toContain(2);
    expect(arcs.length).toBeGreaterThan(0);
    // The instruction kept to its own pane: nothing drawn past the seam.
    expect(texts.filter(({ text }) => text === 'Turn right').every(({ x }) => x < 288)).toBe(true);
  });
});
