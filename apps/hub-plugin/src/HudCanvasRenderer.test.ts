import { describe, expect, it } from 'vitest';
import * as HudCanvas from './HudCanvasRenderer';
import type { HudVisualFrame } from './HudVisualSurface';
import type { HudStatusModel } from './HudStatus';
import { SPINE_LABEL_BOX } from './types';
import {
  HudCanvasRenderer,
  HUD_VISUAL_GEOMETRY,
  hudBrightnessGray,
  hudDisplayTitle,
  parseHudLabeledValue,
  parseHudListItem,
  canRenderPlanningMonthGrid,
  planningGridMarker,
  planningGridMatchesMonth,
  parsePlanningMonthTitle,
  planningMonthDays,
  productivityActionHint,
  resolveHudOverview,
} from './HudCanvasRenderer';

describe('hudBrightnessGray', () => {
  it('keeps routine and urgent Mac cards visually distinct', () => {
    expect(hudBrightnessGray(3)).toBeLessThan(hudBrightnessGray(4));
    expect(hudBrightnessGray(3)).toBe(170);
    expect(hudBrightnessGray(4)).toBe(238);
  });

});

describe('productivity escape copy', () => {
  // GesturePolicy routes a double-click as Back on every source (temple or
  // ring), so the escape is named by the gesture, not by a hand.
  it.each([
    [{ phase: 'verifying', monitoringArmed: false }, 'Back: double-click to Planning'],
    [{ phase: 'saved', monitoringArmed: true }, 'Back: double-click to Planning · monitoring next'],
    [{ phase: 'monitoring', monitoringArmed: false }, 'Back: double-click to Planning · auto in 5s'],
    [{ phase: 'completed', monitoringArmed: false }, 'Back: double-click to Planning · auto in 5s'],
    [{ phase: 'failed', monitoringArmed: false }, 'Back: double-click to Planning · auto in 5s'],
    [{ phase: 'check', monitoringArmed: false }, 'Back: double-click to Planning · auto in 5s'],
    [{ phase: 'cancelled', monitoringArmed: false }, 'Back: double-click to Planning · auto in 5s'],
    [{ phase: 'not_saved', monitoringArmed: false }, 'Back: double-click to Planning · auto in 5s'],
    [{ phase: 'due', monitoringArmed: false }, 'tap: complete · Back: double-click to Planning'],
  ] as const)('keeps %o visibly escapable', (model, hint) => {
    expect(productivityActionHint(model)).toBe(hint);
  });
});

describe('selected visual geometry', () => {
  it('pins the measured reference anchors instead of a generic wide menu', () => {
    expect(HUD_VISUAL_GEOMETRY).toMatchObject({
      statusDividerY: 23.5,
      overviewMainX: 176,
      overviewTrayY: 168,
      listContentX: 93,
    });
  });

  it('uses the reference sentence-case focus heading', () => {
    expect(hudDisplayTitle('MESSAGES · 3')).toBe('Messages · 3');
    expect(hudDisplayTitle('MESSAGES · SETUP')).toBe('Messages · Setup');
    expect(hudDisplayTitle('JOAT')).toBe('JOAT');
    expect(hudDisplayTitle('NOTIFICATIONS')).toBe('Notifications');
    expect(hudDisplayTitle('QUICKNOTES')).toBe('QuickNotes');
    expect(hudDisplayTitle('PLANNING · PLAN · SEPTEMBER 2026')).toBe('Planning · Month · September 2026');
    expect(hudDisplayTitle('PLANNING · PLAN · AUG 31–SEP 6')).toBe('Planning · Week · Aug 31–Sep 6');
    expect(hudDisplayTitle('PLANNING · PLAN · TUE SEP 1')).toBe('Planning · Day · Tue Sep 1');
    expect(hudDisplayTitle('PLANNING · PLAN · SETUP')).toBe('Planning · Setup');
    expect(hudDisplayTitle('Already Mixed')).toBe('Already Mixed');
  });
});

describe('Planning month geometry', () => {
  it('recognizes the native Planning month title and creates a stable Monday-first grid', () => {
    const month = parsePlanningMonthTitle('PLANNING · PLAN · SEPTEMBER 2026');

    expect(month).toEqual({ year: 2026, month: 8, label: 'SEPTEMBER' });
    expect(planningMonthDays(month!).slice(0, 7)).toEqual([
      { day: 31, inMonth: false },
      { day: 1, inMonth: true },
      { day: 2, inMonth: true },
      { day: 3, inMonth: true },
      { day: 4, inMonth: true },
      { day: 5, inMonth: true },
      { day: 6, inMonth: true },
    ]);
    expect(planningMonthDays(month!)).toHaveLength(42);
    const days = planningMonthDays(month!);
    expect(days[days.length - 1]).toEqual({ day: 11, inMonth: false });

    const planningGrid = Array.from({ length: 42 }, (_, index) => {
      const value = new Date(Date.UTC(2026, 7, 31 + index));
      return {
        day: value.toISOString().slice(0, 10),
        eventCount: index === 1 ? 1 : 0,
        reminderCount: index === 1 ? 1 : 0,
        inMonth: value.getUTCMonth() === 8,
      };
    });
    expect(planningGridMatchesMonth(month!, planningGrid)).toBe(true);
    expect(planningGridMarker(planningGrid[1]!)).toBe('E1 R1');
    expect(planningGridMarker(planningGrid[0]!)).toBe('');
    expect(canRenderPlanningMonthGrid(month, planningGrid, true, 0, 11)).toBe(true);
    expect(canRenderPlanningMonthGrid(month, planningGrid, true, 5, 11)).toBe(true);
    expect(canRenderPlanningMonthGrid(month, planningGrid, true, 6, 11)).toBe(false);
    expect(planningGridMatchesMonth(month!, [
      ...planningGrid.slice(0, 1),
      { ...planningGrid[1]!, day: '2026-09-02' },
      ...planningGrid.slice(2),
    ])).toBe(false);
  });

  it('does not mistake a week, day, or arbitrary list for a calendar month', () => {
    expect(parsePlanningMonthTitle('PLANNING · PLAN · SEP 1–7')).toBeUndefined();
    expect(parsePlanningMonthTitle('PLANNING · PLAN · TUE SEP 1')).toBeUndefined();
    expect(parsePlanningMonthTitle('SEPTEMBER 2026')).toBeUndefined();
    expect(parsePlanningMonthTitle('PLANNING · PLAN · JANUARY 0099')).toBeUndefined();
  });

  it('marks the selected month week with a slim rail instead of an enclosing box', () => {
    expect(HudCanvas.planningMonthSelectionRail(3)).toEqual({
      x: 28,
      y: 141,
      width: 3,
      height: 22,
    });
  });
});

describe('parseHudLabeledValue', () => {
  it('treats a normal assistant reply as content, never as a label', () => {
    expect(parseHudLabeledValue('Your meeting starts in 19 minutes.', 'REPLY')).toEqual({
      label: 'REPLY',
      value: 'Your meeting starts in 19 minutes.',
      structured: false,
    });
  });

  it('preserves the approved label/value fixture shape', () => {
    expect(parseHudLabeledValue('NEXT\nDesign review · 4:30', 'REPLY')).toEqual({
      label: 'NEXT',
      value: 'Design review · 4:30',
      structured: true,
    });
  });
});

describe('resolveHudOverview', () => {
  it('turns a status-only connecting boot into one intentional focus state', () => {
    expect(resolveHudOverview({
      status: '4:28   JOAT connecting',
      main: '  ',
      context: '',
      tray: '\n',
    })).toEqual({
      kind: 'empty',
      title: 'Connecting',
      primary: 'Waiting for JOAT on your Mac…',
      recovery: 'This screen will refresh automatically.',
      hint: 'double-click: menu · long-press: mic',
    });
  });

  it('never mixes stale Overview rows into a reconnecting frame', () => {
    expect(resolveHudOverview({
      status: '4:28   JOAT reconnecting',
      main: 'REPLY\nOld answer from the prior connection',
      context: 'LAST QUERY\nold query',
      tray: '2 notifications',
    })).toEqual({
      kind: 'empty',
      title: 'Reconnecting',
      primary: 'Restoring JOAT on your Mac…',
      recovery: 'This screen will refresh automatically.',
      hint: 'double-click: menu · long-press: mic',
    });
  });

  it('never invents tray or context chrome for a partial main-only frame', () => {
    expect(resolveHudOverview({
      status: '9:41   JOAT mic off',
      main: 'Your meeting starts in 19 minutes.',
      context: '',
      tray: '',
    })).toEqual({
      kind: 'content',
      main: {
        label: 'REPLY',
        value: 'Your meeting starts in 19 minutes.',
        structured: false,
      },
    });
  });

  it('labels a typed incoming Messages card as MESSAGE, never REPLY', () => {
    expect(resolveHudOverview({
      status: '9:41   JOAT listening',
      main: '(201) 462-3963 · Messages\nCajun Corner order confirmed',
      mainKind: 'notification',
      context: '',
      tray: '',
    })).toEqual({
      kind: 'content',
      main: {
        label: 'MESSAGE',
        value: '(201) 462-3963 · Messages Cajun Corner order confirmed',
        structured: false,
      },
    });
  });

  it('preserves every semantic section in the populated Overview fixture', () => {
    expect(resolveHudOverview({
      status: '9:41   JOAT ready',
      main: 'NEXT\nDesign review · 4:30',
      context: 'LAST QUERY\nnearest coffee shop?',
      tray: '2 notifications waiting',
    })).toEqual({
      kind: 'content',
      main: {
        label: 'NEXT',
        value: 'Design review · 4:30',
        structured: true,
      },
      tray: '2 notifications',
      context: {
        label: 'LAST QUERY',
        value: 'nearest coffee shop?',
        structured: true,
      },
    });
  });
});

describe('parseHudListItem', () => {
  it('extracts only a real Mac-provided age from a message row', () => {
    expect(parseHudListItem('[18m] Marcus · Heading to the lab in 10.')).toEqual({
      primary: 'Marcus',
      secondary: 'Heading to the lab in 10.',
      age: '18m',
    });
  });

  it('does not invent an age for loading, empty, or settings rows', () => {
    expect(parseHudListItem('Loading recent notifications…')).toEqual({
      primary: 'Loading recent notifications…',
      secondary: '',
      age: '',
    });
  });
});

describe('action pill outcomes', () => {
  it('renders transient receipts as a strip and truth states as pages', () => {
    // Wearer's ruling (2026-09-01): a save/delete receipt is a small wide
    // pill like the platform's own listening indicator, not a page takeover.
    for (const phase of ['verifying', 'saved', 'monitoring', 'completed',
      'cancelled', 'not_saved'] as const) {
      expect(HudCanvas.usesActionPillLayout(phase)).toBe(true);
    }
    // failed/check decide whether a retry is safe; due is an interactive
    // alert with an acknowledged delivery. Those must be read, not glanced.
    for (const phase of ['failed', 'check', 'due'] as const) {
      expect(HudCanvas.usesActionPillLayout(phase)).toBe(false);
    }
  });

  it('keeps the strip inside the content field, clear of spine and footer', () => {
    expect(HudCanvas.ACTION_PILL.y).toBeGreaterThan(36);
    expect(HudCanvas.ACTION_PILL.y + HudCanvas.ACTION_PILL.height).toBeLessThan(260);
    expect(HudCanvas.ACTION_PILL.x + HudCanvas.ACTION_PILL.width).toBeLessThanOrEqual(576);
  });
});

describe('home dashboard content', () => {
  it('shows the notifications themselves, never a count', () => {
    // The reference HUDs put the cards on the resting screen; a counter
    // would only tell the wearer to open a page they should not need.
    expect(HudCanvas.parseHudNotificationRows(
      'Calendar · 8m · Design review moved to 4:30\nMail · 23m · Q3 plan is ready',
    )).toEqual([
      { source: 'Calendar', age: '8m', headline: 'Design review moved to 4:30' },
      { source: 'Mail', age: '23m', headline: 'Q3 plan is ready' },
    ]);
  });

  it('keeps a headline that contains the separator intact', () => {
    expect(HudCanvas.parseHudNotificationRows('Mail · 2m · Q3 · final draft')[0])
      .toEqual({ source: 'Mail', age: '2m', headline: 'Q3 · final draft' });
  });

  it('treats an unstructured line as a plain headline', () => {
    expect(HudCanvas.parseHudNotificationRows('Reminder monitoring armed'))
      .toEqual([{ source: '', age: '', headline: 'Reminder monitoring armed' }]);
    expect(HudCanvas.parseHudNotificationRows('')).toEqual([]);
  });

  it('reads the clock and date from the phone, not the Mac', () => {
    expect(HudCanvas.hudClockParts('9:41')).toEqual({ hours: '09', minutes: '41' });
    expect(HudCanvas.hudClockParts('')).toEqual({ hours: '--', minutes: '--' });
    expect(HudCanvas.hudDateLabel(new Date(2026, 8, 1))).toBe('Tue 9/1');
  });
});

describe('navigation map projection', () => {
  const panel = {
    latitude: 41.9147, longitude: -87.6379, zoom: 16,
    width: 248, height: 168, image: {} as CanvasImageSource,
  };

  it('puts the panel centre exactly at the pane centre', () => {
    const bounds = HudCanvas.NAVIGATION_MAP_PANEL;
    const [x, y] = HudCanvas.projectMercatorPanel(
      { latitude: panel.latitude, longitude: panel.longitude }, panel, bounds,
    );
    expect(x).toBeCloseTo(bounds.x + bounds.width / 2, 6);
    expect(y).toBeCloseTo(bounds.y + bounds.height / 2, 6);
  });

  it('places north up and east right, at the Mapbox 512px tile scale', () => {
    const bounds = HudCanvas.NAVIGATION_MAP_PANEL;
    const north = HudCanvas.projectMercatorPanel(
      { latitude: panel.latitude + 0.001, longitude: panel.longitude }, panel, bounds,
    );
    const east = HudCanvas.projectMercatorPanel(
      { latitude: panel.latitude, longitude: panel.longitude + 0.001 }, panel, bounds,
    );
    expect(north[1]).toBeLessThan(bounds.y + bounds.height / 2);
    expect(east[0]).toBeGreaterThan(bounds.x + bounds.width / 2);
    // 512·2^16 px span 360°, so 0.001° of longitude is 93.2px — which is
    // also what makes the 248px pane ≈220m wide. If this drifts, JOAT's
    // route stops sitting on the roads the Mac drew.
    expect(east[0] - (bounds.x + bounds.width / 2)).toBeCloseTo(93.2, 0);
  });
  it('keeps changing mapped-guidance facts inside the left tile column', () => {
    const widths: Array<number | undefined> = [];
    const renderer = Object.create(HudCanvasRenderer.prototype) as HudCanvasRenderer;
    const stub = renderer as never as {
      drawActiveNavigation(c: CanvasRenderingContext2D, navigation: unknown): void;
      drawManeuver(): void;
      fitText(): void;
      drawWrappedText(): void;
      drawNavigationMap(): void;
      arrivalClock(): string;
      drawNavigationProgress(c: CanvasRenderingContext2D, progress: number, width?: number): void;
      drawNavigationFooter(c: CanvasRenderingContext2D, copy: string, width?: number): void;
    };
    stub.drawManeuver = () => {};
    stub.fitText = () => {};
    stub.drawWrappedText = () => {};
    stub.drawNavigationMap = () => {};
    stub.arrivalClock = () => '';
    stub.drawNavigationProgress = (_context, _progress, width) => { widths.push(width); };
    stub.drawNavigationFooter = (_context, _copy, width) => { widths.push(width); };
    const context = {
      fillStyle: '', font: '', textAlign: 'left', textBaseline: 'middle',
      fillText() {},
    } as unknown as CanvasRenderingContext2D;

    stub.drawActiveNavigation(context, {
      view: 'guidance', primary: 'Turn right', secondary: 'onto Main Street',
      hint: 'Then continue', interaction: 'none', options: [], selectedIndex: 0,
      mode: 'walking', maneuver: 'turn_right', maneuverDistanceM: 40,
      remainingDistanceM: 900, etaMinutes: 12, progress: 0.25,
      map: {
        geometry: [
          { latitude: panel.latitude, longitude: panel.longitude },
          { latitude: panel.latitude + 0.001, longitude: panel.longitude + 0.001 },
        ],
        position: { latitude: panel.latitude, longitude: panel.longitude },
        panel,
      },
    });

    // The left guidance pane ends at x=272, before the x=288 image-tile
    // seam. A full-width progress/footer dirties the bottom-right map tile
    // for every distance update and adds one unnecessary serial BLE write.
    expect(widths).toEqual([236, 236]);
  });

  it('draws the mapped-guidance live band before the image-tile seam', () => {
    const bounds: Array<{ x: number; width: number }> = [];
    const renderer = Object.create(HudCanvasRenderer.prototype) as HudCanvasRenderer;
    const stub = renderer as never as {
      fitText(
        c: CanvasRenderingContext2D, text: string, x: number, y: number, width: number,
      ): void;
      drawNavigationProgress(c: CanvasRenderingContext2D, progress: number, width?: number): void;
      drawNavigationFooter(c: CanvasRenderingContext2D, copy: string, width?: number): void;
    };
    stub.fitText = (_context, _text, x, _y, width) => { bounds.push({ x, width }); };
    const context = {
      fillStyle: '', font: '', textAlign: 'left', textBaseline: 'middle',
      fillRect(x: number, _y: number, width: number) { bounds.push({ x, width }); },
    } as unknown as CanvasRenderingContext2D;

    stub.drawNavigationProgress(context, 0.4, 236);
    stub.drawNavigationFooter(context, '900 m left · 12 min', 236);

    expect(bounds).toHaveLength(3);
    expect(bounds.every(({ x, width }) => x + width < 288)).toBe(true);
  });
});

describe('every speaker row splits dim tag from bright words', () => {
  // Verified against the real HudSink output, not invented: these are the
  // exact rows it paints, including the repair label the engine builds as
  // "<speaker> — repair <n>".
  const TAG = /^([A-Z][A-Z0-9 .—-]{0,22}) > /;

  it('tags every label the engine actually produces', () => {
    for (const row of [
      'CODEX > cache on the session id',
      'CLAUDE > that breaks on handoff',
      'VERIFIER > [1] OK — quote found',
      'YOU > I think that is the wrong layer',
      'JOAT > AGREED: key on the generation',
      // A repair rendered FLAT before this: no dim tag, no bright body, on
      // the one row where the split matters most.
      'CODEX — REPAIR 1 > corrected with a real quote',
      'CLAUDE — ROUND 2 > building on that',
    ]) {
      expect(TAG.test(row), row).toBe(true);
    }
  });

  it('never claims a tag in ordinary prose', () => {
    for (const row of [
      'the cache is the wrong layer',
      'Keep the next step explicit.',
      'not, after a lens handoff — the auth replay resets the page.',
    ]) {
      expect(TAG.test(row), row).toBe(false);
    }
  });
});

describe('the spine\'s left slot', () => {
  /** drawShell with the icon and fitText helpers stubbed, so the assertion
   * is about the slot's TEXT and not about canvas image compositing. */
  function shell(showTime: boolean): string[] {
    const texts: string[] = [];
    const context = {
      fillStyle: '', font: '', textAlign: 'left', textBaseline: 'middle',
      measureText: (value: string) => ({ width: value.length * 5.18 }),
      save() {}, restore() {}, beginPath() {}, stroke() {},
      moveTo() {}, lineTo() {}, arcTo() {}, closePath() {},
      fillText(text: string) { texts.push(text); },
    } as unknown as CanvasRenderingContext2D;
    const renderer = Object.create(HudCanvasRenderer.prototype) as HudCanvasRenderer;
    const stub = renderer as never as {
      drawIcon(): void;
      fitText(c: CanvasRenderingContext2D, t: string): void;
      drawShell(
        c: CanvasRenderingContext2D, s: HudStatusModel, i: unknown,
        f: boolean, showTime: boolean,
      ): void;
    };
    stub.drawIcon = () => {};
    stub.fitText = (c, t) => { c.fillText(t, 0, 0); };
    stub.drawShell(
      context, { clock: '9:41', link: 'connected', mic: false, battery: 87, charging: false },
      {} as unknown, true, showTime,
    );
    return texts;
  }

  it('carries the clock on a feature page and the DATE on the overview', () => {
    // The dashboard already prints the time at full size, so its spine
    // yields the small clock — and that slot would otherwise sit empty,
    // which is why the date used to be stacked on top of the big clock.
    expect(shell(true)[0]).toBe('9:41');
    expect(shell(false)[0]).toBe(HudCanvas.hudDateLabel(new Date()));
    // Never both: one screen must not set the same fact twice.
    expect(shell(false)).not.toContain('9:41');
  });
});

describe('the compact JOAT banner uses the existing canvas font', () => {
  const SEAM = 288;

  function shell(status: HudStatusModel): {
    texts: Array<{ text: string; x: number; font: string }>;
    icons: Array<{ name: string; x: number; y: number; size: number }>;
  } {
    const texts: Array<{ text: string; x: number; font: string }> = [];
    const icons: Array<{ name: string; x: number; y: number; size: number }> = [];
    const context = {
      fillStyle: '', font: '', textAlign: 'left', textBaseline: 'middle',
      measureText: (value: string) => ({ width: value.length * 5.18 }),
      save() {}, restore() {}, beginPath() {}, stroke() {},
      moveTo() {}, lineTo() {}, arcTo() {}, closePath() {},
      fillText(this: {font: string}, text: string, x: number) { texts.push({ text, x, font: this.font }); },
    } as unknown as CanvasRenderingContext2D;
    const renderer = Object.create(HudCanvasRenderer.prototype) as HudCanvasRenderer;
    const stub = renderer as never as {
      drawIcon(c: unknown, i: unknown, x: number, y: number, size: number): void;
      drawShell(
        c: CanvasRenderingContext2D, s: HudStatusModel, i: unknown,
        f: boolean, showTime: boolean,
      ): void;
    };
    stub.drawIcon = (_c, i, x, y, size) => { icons.push({ name: String(i), x, y, size }); };
    // Named stand-ins, so the assertions can tell which cell was drawn.
    const iconSet = Object.fromEntries(
      ['microphone', 'microphoneSlash', 'batteryFull', 'batteryHigh', 'batteryMedium',
        'batteryLow', 'batteryEmpty', 'batteryCharging'].map((name) => [name, name]),
    );
    stub.drawShell(context, status, iconSet, true, true);
    return { texts, icons };
  }

  const hearing: HudStatusModel = {
    clock: '9:41', link: 'connected', mic: true, battery: 87, charging: false, macState: 'hearing you…',
  };

  it('paints the state word in the compact banner and keeps the icons east of the seam', () => {
    const { texts, icons } = shell(hearing);
    // No words for the mic or the battery any more: the clock, the state
    // word, and two icons.
    expect(texts.map((entry) => entry.text)).toEqual(['9:41', 'JOAT · HEARING YOU…']);
    const label = texts.find((entry) => entry.text === 'JOAT · HEARING YOU…');
    expect(label?.font).toContain('10px');
    expect(label?.font).toContain('SF Mono');
    const spineClock = texts.find((entry) => entry.text === '9:41');
    expect(spineClock?.font).toContain('11px');
    expect((label?.x ?? 0) + 'JOAT · HEARING YOU…'.length * 6).toBeLessThan(SEAM);
    expect(icons.map((icon) => icon.name)).toEqual(['microphone', 'batteryHigh']);
    for (const icon of icons) {
      expect(icon.x).toBeGreaterThanOrEqual(SEAM);
      // Inside the 24 px band.
      expect(icon.y + icon.size).toBeLessThanOrEqual(HUD_VISUAL_GEOMETRY.spineHeight);
    }
    // Clock and changing label remain separate inside the top-left tile.
    const clock = texts.find((entry) => entry.text === '9:41');
    expect((clock?.x ?? 0) + '9:41'.length * 7.2).toBeLessThan(SPINE_LABEL_BOX.x);
  });

  it('draws the slashed mic when closed, the level-correct cell, and the number only when low', () => {
    expect(shell({ ...hearing, mic: false }).icons.map((icon) => icon.name))
      .toEqual(['microphoneSlash', 'batteryHigh']);
    expect(shell({ ...hearing, battery: 100 }).icons[1]?.name).toBe('batteryFull');
    expect(shell({ ...hearing, battery: 45 }).icons[1]?.name).toBe('batteryMedium');
    expect(shell({ ...hearing, battery: 20 }).icons[1]?.name).toBe('batteryLow');
    // Charging wins whatever the level — the mark the string parser used
    // to strip before the spine could draw it.
    expect(shell({ ...hearing, battery: 20, charging: true }).icons[1]?.name).toBe('batteryCharging');
    const low = shell({ ...hearing, battery: 12 });
    expect(low.icons[1]?.name).toBe('batteryEmpty');
    expect(low.texts.map((entry) => entry.text)).toContain('12%');
    expect(shell({ ...hearing, battery: 12, charging: true }).texts.map((entry) => entry.text))
      .not.toContain('12%');
    // No battery reported yet: no cell, no number, no crash.
    const { battery: _omit, ...unknown } = hearing;
    expect(shell(unknown).icons.map((icon) => icon.name)).toEqual(['microphone']);
  });

  const overview = (status: string): HudVisualFrame => ({
    kind: 'overview', status, micOn: false, mainBrightness: 4,
    main: '', context: '', tray: '', hint: '',
  });
  const list = (status: string, title: string): HudVisualFrame => ({
    kind: 'list', status, micOn: false, title, items: [], selectable: false,
    selectedIndex: 0, hint: '',
  });

  it('hudSpineLabel names the link state at home and the page elsewhere', () => {
    // Ready is resting: the spine says JOAT, and the mic icon says the rest.
    expect(HudCanvas.hudSpineLabel(overview('9:41  JOAT ready  bat 87%'))).toBe('JOAT');
    expect(HudCanvas.hudSpineLabel(overview('9:41  JOAT hearing you…'))).toBe('JOAT · HEARING YOU…');
    expect(HudCanvas.hudSpineLabel(list('9:41  JOAT ready', 'NEWS'))).toBe('NEWS');
    expect(HudCanvas.hudSpineLabel(list('9:41 JOAT mic off', 'NEWS'))).toBe('NEWS');
    expect(HudCanvas.hudSpineLabel(overview('9:41 JOAT mic off'))).toBe('JOAT');
    // A degraded link outranks any page name.
    expect(HudCanvas.hudSpineLabel(list('9:41  JOAT connecting', 'NEWS'))).toBe('JOAT · CONNECTING');
  });
});

describe('the resting overview wears one footer on every tab', () => {
  // The footer is centred on the tile seam, so a footer that differed
  // between the notifications tab and the other three made a swipe repaint
  // the bottom-left tile too: three image writes where the rail and the
  // pane need two.
  function overviewFooter(rail: boolean): string {
    const texts: string[] = [];
    const context = {
      fillStyle: '', font: '', textAlign: 'left', textBaseline: 'middle',
      lineWidth: 1, strokeStyle: '', globalAlpha: 1,
      measureText: (value: string) => ({ width: value.length * 5.18 }),
      save() {}, restore() {}, beginPath() {}, stroke() {}, fill() {},
      moveTo() {}, lineTo() {}, arcTo() {}, closePath() {}, fillRect() {},
      fillText(text: string) { texts.push(text); },
    } as unknown as CanvasRenderingContext2D;
    const renderer = Object.create(HudCanvasRenderer.prototype) as HudCanvasRenderer;
    const stub = renderer as never as {
      drawIcon(): void;
      drawTabRail(): void;
      drawPanelPane(): void;
      drawOverview(c: CanvasRenderingContext2D, frame: unknown, icons: unknown): void;
    };
    stub.drawIcon = () => {};
    stub.drawTabRail = () => {};
    stub.drawPanelPane = () => {};
    stub.drawOverview(context, {
      status: '9:41  JOAT ready  bat 87%',
      main: 'Nothing new.',
      mainBrightness: 4,
      context: '',
      tray: '',
      hint: 'tap: open · swipe: tabs · hold: mic · “JOAT …”',
      ...(rail ? { rail: { activeTab: 'notifications' } } : {}),
    }, {});
    const footer = texts[texts.length - 1];
    if (footer === undefined) throw new Error('no footer drawn');
    return footer;
  }

  it('draws the shared rail hint on the notifications tab, not the chrome hint', () => {
    expect(overviewFooter(true)).toBe(HudCanvas.PANEL_OVERVIEW_HINT);
  });

  it('keeps the chrome hint where there is no rail to swipe', () => {
    expect(overviewFooter(false)).toBe('tap: open · swipe: tabs · hold: mic · “JOAT …”');
  });
});
