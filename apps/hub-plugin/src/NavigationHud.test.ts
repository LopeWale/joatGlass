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
    expect(destination.primary).toBe('Corner Bakery');
    expect(destination.secondary).toContain('2301 N Lincoln Ave');
    expect(destination.hint).toMatch(/confirm/i);

    const repair = navigationVisualFixture('nav-repair');
    expect(repair.options).toHaveLength(3);
    expect(repair.options[2]).toMatch(/retry/i);
    expect(repair.hint).not.toMatch(/long-press/i);

    const mode = navigationVisualFixture('nav-mode');
    expect(mode.options).toEqual(['Walk · 18 min', 'Cycle · 7 min', 'Drive · 4 min']);
    expect(mode.choiceIds).toEqual(['walking', 'cycling', 'driving']);
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

describe('the guidance pane route schematic', () => {
  const route = {
    geometry: [
      { latitude: 0, longitude: 0 },
      { latitude: 0, longitude: 0.009 },
      { latitude: 0.009, longitude: 0.009 },
    ],
  };

  function recordingContext() {
    const strokes: number[] = [];
    const arcs: Array<[number, number]> = [];
    const texts: Array<{ text: string; x: number; y: number }> = [];
    let images = 0;
    const state: Record<string, unknown> = {
      font: '', textAlign: 'left', lineWidth: 0,
      measureText: (value: string) => ({ width: value.length * 6 }),
      fillText: (text: string, x: number, y: number) => texts.push({ text, x, y }),
      stroke: () => strokes.push(state.lineWidth as number),
      arc: (x: number, y: number) => arcs.push([x, y]),
      drawImage: () => { images += 1; },
    };
    const context = new Proxy(state, {
      get: (target, key) => (key in target ? target[String(key)] : () => undefined),
      set: (target, key, value) => { target[String(key)] = value; return true; },
    }) as unknown as CanvasRenderingContext2D;
    return { context, strokes, arcs, texts, images: () => images };
  }

  it('draws the route schematic — never a raster tile, even when a stray one is attached', async () => {
    const { HudCanvasRenderer } = await import('./HudCanvasRenderer');
    const rec = recordingContext();
    const renderer = Object.create(HudCanvasRenderer.prototype) as InstanceType<typeof HudCanvasRenderer>;
    (renderer as unknown as { drawNavigation(c: unknown, n: unknown): void }).drawNavigation(rec.context, {
      view: 'guidance', primary: 'Turn right', secondary: 'onto Test Ave', hint: 'Then continue',
      interaction: 'none', options: [], selectedIndex: 0, mode: 'walking',
      maneuver: 'turn_right', maneuverDistanceM: 80, remainingDistanceM: 900, etaMinutes: 13,
      progress: 0.1,
      map: {
        ...route,
        position: { latitude: 0, longitude: 0.0083 },
        maneuverAlongM: 1000,
        maneuverLabel: 'Test Ave',
        panel: { latitude: 0, longitude: 0, zoom: 16, width: 284, height: 220, image: {} as CanvasImageSource },
      },
    });
    expect(rec.images()).toBe(0);
    // The road ahead is the one wide bright stroke; the travelled part is thin.
    expect(rec.strokes).toContain(8);
    expect(rec.strokes).toContain(2);
    // The maneuver node and its street label sit inside the right pane.
    const label = rec.texts.find(({ text }) => text === 'Test Ave');
    expect(label).toBeDefined();
    expect(label!.x).toBeGreaterThanOrEqual(292);
    expect(rec.arcs.some(([x]) => x > 292)).toBe(true);
    // The instruction kept to its own pane: nothing drawn past the seam.
    expect(rec.texts.filter(({ text }) => text === 'Turn right').every(({ x }) => x < 288)).toBe(true);
  });

  it('renders DRIVING · ACTIVE heading and Driving footer fact on the guidance canvas', async () => {
    const { HudCanvasRenderer } = await import('./HudCanvasRenderer');
    const texts: string[] = [];
    const state: Record<string, unknown> = {
      font: '', textAlign: 'left', lineWidth: 0,
      measureText: (value: string) => ({ width: value.length * 6 }),
      fillText: (text: string) => texts.push(text),
    };
    const context = new Proxy(state, {
      get: (target, key) => (key in target ? target[String(key)] : () => undefined),
      set: (target, key, value) => { target[String(key)] = value; return true; },
    }) as unknown as CanvasRenderingContext2D;
    const renderer = Object.create(HudCanvasRenderer.prototype) as InstanceType<typeof HudCanvasRenderer>;
    (renderer as unknown as { drawNavigation(c: unknown, n: unknown): void }).drawNavigation(context, {
      view: 'guidance', primary: 'Keep left', secondary: 'onto I-90 W', hint: '2x tap: menu',
      interaction: 'none', options: [], selectedIndex: 0, mode: 'driving',
      maneuver: 'slight_left', maneuverDistanceM: 350, remainingDistanceM: 4_200, etaMinutes: 6,
      progress: 0.3,
    });
    expect(texts).toContain('DRIVING · ACTIVE');
    expect(texts.some((t) => t.startsWith('Driving · 4.2 km left · 6 min'))).toBe(true);
  });
});
