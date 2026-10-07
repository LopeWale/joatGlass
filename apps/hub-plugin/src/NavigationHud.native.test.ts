import { describe, expect, it } from 'vitest';
import { getTextWidth, measureTextWrap } from '@evenrealities/pretext';

import { ASSISTANT_ANSWER_PAGE } from './HudPageBuilder';
import {
  NAVIGATION_NATIVE_DEFAULT_HINTS,
  NAVIGATION_NATIVE_LINE_BUDGET,
  NAVIGATION_NATIVE_OPTION_ROWS,
  NAVIGATION_NATIVE_SELECTED_MARKER,
  NAVIGATION_NATIVE_TEXT_WIDTH,
  NAVIGATION_NATIVE_UNSELECTED_MARKER,
  NAVIGATION_VISUAL_STATES,
  navigationNativeBody,
  navigationNativeHint,
  navigationVisualFixture,
  type NavigationHudModel,
  type NavigationView,
} from './NavigationHud';

/**
 * Every navigation frame but live guidance is a text card Even's firmware
 * text page can draw with one `rebuildPageContainer`. These cases pin what
 * the page says per view, the cursor on repair/mode, the line budget, and
 * that the hint never asks the wearer to SAY anything: the page is a
 * control surface (tap / swipe / double-click), not a voice prompt.
 *
 * Line budget: the text page's main container is ASSISTANT_ANSWER_PAGE
 * (HudPageBuilder): 214 px high with 4 px padding at the firmware's fixed
 * 27 px line height, so floor((214 − 8) / 27) = 7 lines of at most
 * 576 − 2·4 = 568 px each. NAVIGATION_NATIVE_LINE_BUDGET restates the 7 so
 * NavigationHud stays free of the page builder; the first case pins them
 * equal so a geometry change there cannot silently overflow here.
 */
const FIRMWARE_LINE_BUDGET = ASSISTANT_ANSWER_PAGE.lineCapacity;

const VIEWS: readonly NavigationView[] = [
  'destination', 'repair', 'mode', 'preview', 'guidance',
  'uncertain', 'wrong_way', 'off_route', 'rerouting', 'arrived', 'error',
];

function fixture(view: NavigationView): NavigationHudModel {
  const state = NAVIGATION_VISUAL_STATES.find((candidate) => (
    navigationVisualFixture(candidate).view === view
  ));
  if (!state) throw new Error(`no fixture for ${view}`);
  return navigationVisualFixture(state);
}

function lines(body: string): string[] {
  return body.split('\n');
}

function expectWithinBudget(body: string): void {
  const rows = lines(body);
  expect(rows.length).toBeLessThanOrEqual(FIRMWARE_LINE_BUDGET);
  expect(rows.length).toBeGreaterThan(0);
  for (const row of rows) {
    expect(row).not.toBe('');
    expect(getTextWidth(row)).toBeLessThanOrEqual(ASSISTANT_ANSWER_PAGE.textWidth);
  }
  // And as the firmware itself wraps the page: no row breaks into two.
  expect(measureTextWrap(body, ASSISTANT_ANSWER_PAGE.textWidth).lineCount).toBe(rows.length);
}

describe('navigationNativeBody', () => {
  it('restates the text page geometry it is budgeted against', () => {
    expect(NAVIGATION_NATIVE_LINE_BUDGET).toBe(FIRMWARE_LINE_BUDGET);
    expect(NAVIGATION_NATIVE_LINE_BUDGET).toBe(7);
    expect(NAVIGATION_NATIVE_TEXT_WIDTH).toBe(ASSISTANT_ANSWER_PAGE.textWidth);
    expect(NAVIGATION_NATIVE_OPTION_ROWS).toBe(4);
  });

  it('keeps live guidance raster: it is the only view that draws the map', () => {
    expect(navigationNativeBody(fixture('guidance'))).toBeUndefined();
    const withoutMap = { ...fixture('guidance'), map: undefined };
    expect(navigationNativeBody(withoutMap)).toBeUndefined();
  });

  it('gives every other view a text page within the firmware line budget', () => {
    for (const view of VIEWS) {
      if (view === 'guidance') continue;
      const page = navigationNativeBody(fixture(view));
      expect(page, view).toBeDefined();
      expectWithinBudget(page!.body);
      expect(page!.hint).not.toMatch(/\bsay\b/iu);
    }
  });

  it('is deterministic for the same model', () => {
    const model = fixture('repair');
    expect(navigationNativeBody(model)).toEqual(navigationNativeBody({ ...model }));
  });

  it('destination: confirm step, place, address', () => {
    const page = navigationNativeBody(fixture('destination'))!;
    expect(lines(page.body)).toEqual([
      'CONFIRM DESTINATION',
      'Cajun Corner',
      '2301 N Lincoln Ave, Chicago',
    ]);
    expect(page.hint).toBe('tap: confirm · swipe: retry');
  });

  it('repair: question, heard phrase, and the options with the cursor on the selected one', () => {
    const page = navigationNativeBody(fixture('repair'))!;
    expect(lines(page.body)).toEqual([
      'REPAIR DESTINATION',
      'Which Cajun Corner?',
      'We heard “Cajun Corner.”',
      '▶ 2301 N Lincoln Ave · 1.2 km',
      '  W Chicago Ave · 4.8 km',
      '  None of these · retry',
    ]);
    expect(page.hint).toBe('swipe: choose · tap: confirm');
  });

  it('mode: the travel options with the cursor and the destination summary', () => {
    const page = navigationNativeBody(fixture('mode'))!;
    expect(lines(page.body)).toEqual([
      'TRAVEL MODE',
      'Choose travel mode',
      'Cajun Corner · 1.2 km',
      '▶ Walk · 18 min',
      '  Cycle · 7 min',
    ]);
    expect(page.hint).toBe('swipe: choose · tap: continue');
  });

  it('moves the marker, and only the marker, when selectedIndex changes', () => {
    for (const view of ['repair', 'mode'] as const) {
      const model = fixture(view);
      const before = lines(navigationNativeBody(model)!.body);
      const after = lines(navigationNativeBody({ ...model, selectedIndex: 1 })!.body);
      expect(after).toHaveLength(before.length);
      const first = before.length - model.options.length;
      for (let row = 0; row < before.length; row += 1) {
        if (row < first) {
          expect(after[row]).toBe(before[row]);
          continue;
        }
        const option = model.options[row - first]!;
        const selectedBefore = row - first === 0;
        const selectedAfter = row - first === 1;
        expect(before[row]).toBe(
          `${selectedBefore ? NAVIGATION_NATIVE_SELECTED_MARKER : NAVIGATION_NATIVE_UNSELECTED_MARKER}${option}`,
        );
        expect(after[row]).toBe(
          `${selectedAfter ? NAVIGATION_NATIVE_SELECTED_MARKER : NAVIGATION_NATIVE_UNSELECTED_MARKER}${option}`,
        );
      }
      expect(before.filter((row) => row.startsWith(NAVIGATION_NATIVE_SELECTED_MARKER))).toHaveLength(1);
      expect(after.filter((row) => row.startsWith(NAVIGATION_NATIVE_SELECTED_MARKER))).toHaveLength(1);
    }
  });

  it('windows a long option list so the selected row is always on the page', () => {
    const options = Array.from({ length: 6 }, (_, index) => `Option ${index + 1}`);
    const model = { ...fixture('repair'), options };
    const top = lines(navigationNativeBody({ ...model, selectedIndex: 0 })!.body);
    expect(top).toHaveLength(FIRMWARE_LINE_BUDGET);
    expect(top.slice(3)).toEqual(['▶ Option 1', '  Option 2', '  Option 3', '  Option 4']);
    const last = lines(navigationNativeBody({ ...model, selectedIndex: 5 })!.body);
    expect(last).toHaveLength(FIRMWARE_LINE_BUDGET);
    expect(last.slice(3)).toEqual(['  Option 3', '  Option 4', '  Option 5', '▶ Option 6']);
    const clamped = lines(navigationNativeBody({ ...model, selectedIndex: 99 })!.body);
    expect(clamped.slice(3)).toEqual(last.slice(3));
  });

  it('preview: the mode in the step name and the Mac summary, no repeated facts', () => {
    const walking = navigationNativeBody(fixture('preview'))!;
    expect(lines(walking.body)).toEqual([
      'WALK · ROUTE PREVIEW',
      'Cajun Corner',
      '1.2 km · 18 min · via Lincoln Ave',
    ]);
    expect(walking.hint).toBe('tap: start · up: mode · down: place');
    const cycling = navigationNativeBody({ ...fixture('preview'), mode: 'cycling' })!;
    expect(lines(cycling.body)[0]).toBe('CYCLE · ROUTE PREVIEW');
  });

  it('uncertain: the warning with the live facts in the renderer\'s words', () => {
    const page = navigationNativeBody(fixture('uncertain'))!;
    expect(lines(page.body)).toEqual([
      'UNCERTAIN',
      'Finding your position',
      'GPS accuracy is low',
      'Walking · 900 m left · 13 min',
    ]);
    expect(page.hint).toBe('Pause safely · route held');
  });

  it('uncertain before a route (setup status): no fact line', () => {
    const page = navigationNativeBody({
      ...fixture('uncertain'),
      mode: undefined, remainingDistanceM: undefined, etaMinutes: undefined, progress: undefined,
      primary: 'Finding phone location', secondary: 'Hold on',
      hint: 'Finding phone location · say stop',
    })!;
    expect(lines(page.body)).toEqual(['UNCERTAIN', 'Finding phone location', 'Hold on']);
    expect(page.hint).toBe('Finding phone location');
  });

  it('wrong_way: route held when the Mac sends no remaining distance', () => {
    const page = navigationNativeBody(fixture('wrong_way'))!;
    expect(lines(page.body)).toEqual([
      'WRONG WAY',
      'Wrong direction',
      'Turn around when safe',
      'Cycling · 1.1 km left · 8 min',
    ]);
    expect(page.hint).toBe('Route resumes automatically');
    const held = navigationNativeBody({
      ...fixture('wrong_way'), interaction: 'stationary', remainingDistanceM: undefined,
    })!;
    expect(lines(held.body)[3]).toBe('Route held');
  });

  it('off_route and rerouting: warning cards with the facts line', () => {
    const off = navigationNativeBody(fixture('off_route'))!;
    expect(lines(off.body)).toEqual([
      'OFF ROUTE',
      'Off route',
      'Stop when safe',
      'Cycling · 1.2 km left · 9 min',
    ]);
    expect(off.hint).toBe('Checking a new route…');
    const rerouting = navigationNativeBody(fixture('rerouting'))!;
    expect(lines(rerouting.body)).toEqual([
      'REROUTING',
      'Rerouting',
      'Finding a safer path',
      'Cycling · 1.2 km left · 9 min',
    ]);
    expect(rerouting.hint).toBe('Old directions paused');
  });

  it('arrived: the step name, the arrival, and where the place is', () => {
    const page = navigationNativeBody(fixture('arrived'))!;
    expect(lines(page.body)).toEqual([
      'ARRIVED',
      'You have arrived',
      'Cajun Corner · on your right',
    ]);
    expect(page.hint).toBe('tap: finish');
  });

  it('error: the recovery card', () => {
    const page = navigationNativeBody(fixture('error'))!;
    expect(lines(page.body)).toEqual([
      'NAVIGATION ERROR',
      'Navigation unavailable',
      'Location could not be started',
    ]);
    expect(page.hint).toBe('tap: retry · swipe: cancel');
  });

  it('drops empty primary/secondary rather than leaving blank rows', () => {
    const page = navigationNativeBody({ ...fixture('destination'), secondary: '   ' })!;
    expect(lines(page.body)).toEqual(['CONFIRM DESTINATION', 'Cajun Corner']);
  });

  it('fits every row to the region width and folds embedded newlines', () => {
    const page = navigationNativeBody({
      ...fixture('destination'),
      primary: 'A'.repeat(300),
      secondary: 'one\ntwo\n\nthree',
    })!;
    const rows = lines(page.body);
    expect(rows).toHaveLength(3);
    expect(rows[1]!.endsWith('...')).toBe(true);
    expect(getTextWidth(rows[1]!)).toBeLessThanOrEqual(NAVIGATION_NATIVE_TEXT_WIDTH);
    expect(rows[2]).toBe('one two three');
  });
});

describe('navigationNativeHint', () => {
  it('reuses a Mac control hint word for word', () => {
    expect(navigationNativeHint({ ...fixture('mode'), hint: 'swipe: pick · tap: go' }))
      .toBe('swipe: pick · tap: go');
  });

  it('keeps the control segments of a Mac hint and drops the voice ones', () => {
    // The Mac's own navigation hints (services/navigation.py).
    expect(navigationNativeHint({ ...fixture('error'), hint: 'tap/say retry · swipe cancel' }))
      .toBe('tap retry · swipe cancel');
    expect(navigationNativeHint({
      ...fixture('repair'), hint: 'tap choose · swipe select · say first/second',
    })).toBe('tap choose · swipe select');
    expect(navigationNativeHint({ ...fixture('mode'), hint: 'tap choose · swipe select · say walk/cycle' }))
      .toBe('tap choose · swipe select');
    expect(navigationNativeHint({ ...fixture('preview'), hint: 'tap/say start · swipe up/down to edit' }))
      .toBe('tap start · swipe up/down to edit');
    expect(navigationNativeHint({ ...fixture('rerouting'), hint: 'Old directions paused · say stop' }))
      .toBe('Old directions paused');
  });

  it('falls back to the view default when the Mac hint is voice-only or empty', () => {
    expect(navigationNativeHint({ ...fixture('destination'), hint: 'say the destination again' }))
      .toBe(NAVIGATION_NATIVE_DEFAULT_HINTS.destination);
    expect(navigationNativeHint({ ...fixture('arrived'), hint: '' }))
      .toBe(NAVIGATION_NATIVE_DEFAULT_HINTS.arrived);
    expect(navigationNativeHint({ ...fixture('repair'), hint: 'Say first · say second' }))
      .toBe(NAVIGATION_NATIVE_DEFAULT_HINTS.repair);
  });

  it('never names a word that is not tap, swipe, or 2x tap', () => {
    for (const view of VIEWS) {
      expect(NAVIGATION_NATIVE_DEFAULT_HINTS[view]).not.toMatch(/\bsay\b/iu);
      expect(navigationNativeHint({ ...fixture(view), hint: 'say anything · say stop' }))
        .not.toMatch(/\bsay\b/iu);
    }
  });

  it('fits the hint region', () => {
    const hint = navigationNativeHint({ ...fixture('mode'), hint: 'tap: ' + 'x'.repeat(400) });
    expect(hint.length).toBeLessThanOrEqual(96);
    expect(getTextWidth(hint)).toBeLessThanOrEqual(NAVIGATION_NATIVE_TEXT_WIDTH);
  });
});
