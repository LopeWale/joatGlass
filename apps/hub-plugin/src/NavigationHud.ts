import { pxTruncate } from '@evenrealities/pretext';
import { layoutLines } from './FirmwareText';

export const NAVIGATION_VISUAL_STATES = [
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
] as const;

export type NavigationVisualState = typeof NAVIGATION_VISUAL_STATES[number];

export type NavigationView =
  | 'destination'
  | 'repair'
  | 'mode'
  | 'preview'
  | 'guidance'
  | 'uncertain'
  | 'wrong_way'
  | 'off_route'
  | 'rerouting'
  | 'arrived'
  | 'error';

export type NavigationMode = 'walking' | 'cycling' | 'driving';

export type NavigationManeuver =
  | 'depart'
  | 'continue'
  | 'turn_left'
  | 'turn_right'
  | 'slight_left'
  | 'slight_right'
  | 'u_turn'
  | 'arrive';

/**
 * Complete renderer input for one navigation frame. The Mac owns navigation
 * state; the plugin only paints this generation-bound, content-minimized view.
 */
export interface NavigationHudModel {
  view: NavigationView;
  primary: string;
  secondary: string;
  hint: string;
  interaction: 'stationary' | 'none';
  options: readonly string[];
  /** Stable semantic IDs; labels may be localized or provider-authored. */
  choiceIds?: readonly string[];
  selectedIndex: number;
  mode?: NavigationMode;
  maneuver?: NavigationManeuver;
  maneuverDistanceM?: number;
  remainingDistanceM?: number;
  etaMinutes?: number;
  progress?: number;
  /** The route's own shape, so guidance can draw a map instead of leaving
   * half the lens black. Points are the Mac-authored polyline; `position`
   * is the wearer's latest fix. Absent until a route installs. */
  map?: NavigationMapModel;
}

/** Pre-route progress and recovery share one independently delivered region. */
export function isNavigationSetupStatus(navigation: NavigationHudModel): boolean {
  return navigation.view === 'error' || (
    navigation.view === 'uncertain'
    && navigation.interaction === 'none'
    && navigation.remainingDistanceM === undefined
    && !navigation.map
  );
}

export interface NavigationMapPoint {
  latitude: number;
  longitude: number;
}

export interface NavigationMapModel {
  geometry: readonly NavigationMapPoint[];
  position?: NavigationMapPoint;
  /** The fix's course, degrees clockwise from true north. The schematic is
   * heading-up: this faces the top of the pane when it is a real course;
   * otherwise the route's own bearing at the wearer does. */
  heading?: number;
  /** Ground speed of the fix, m/s; a near-stationary course is ignored. */
  speed?: number;
  /** The tracker's along-route match, metres from the route's start. */
  progressM?: number;
  /** Where the next maneuver is, metres along the route from its start. */
  maneuverAlongM?: number;
  /** The street the next maneuver turns onto, as the schematic labels it. */
  maneuverLabel?: string;
  /**
   * @deprecated Raster street tiles are no longer drawn on the glasses
   * (2026-11-23 route schematic). Kept only so an old producer's field
   * type-checks; the renderer ignores it.
   */
  panel?: NavigationMapPanelModel;
}

/**
 * The street a maneuver instruction names, for the schematic's label:
 * "Turn right onto N Michigan Ave" → "N Michigan Ave". An instruction with
 * no recognisable street keeps its own words.
 */
export function navigationStreetLabel(instruction: string | undefined): string | undefined {
  const text = instruction?.replace(/\s+/gu, ' ').trim();
  if (!text) return undefined;
  const match = /\b(?:onto|on to|into|on|toward|towards|at|to)\s+(.+?)[.\s]*$/iu.exec(text);
  const street = (match?.[1] ?? text).trim();
  return street.length > 28 ? `${street.slice(0, 27).trimEnd()}…` : street;
}

export interface NavigationMapPanelModel {
  latitude: number;
  longitude: number;
  zoom: number;
  width: number;
  height: number;
  /** Decoded once by the controller; the renderer only blits it. */
  image: CanvasImageSource;
}

export function isNavigationVisualState(value: string): value is NavigationVisualState {
  return (NAVIGATION_VISUAL_STATES as readonly string[]).includes(value);
}

/** Round for glanceability: 10 m nearby, 50 m farther away, then compact km. */
export function formatNavigationDistance(distanceM: number): string {
  const meters = Math.max(0, distanceM);
  if (meters < 5) return 'now';
  if (meters < 100) return `${Math.round(meters / 10) * 10} m`;
  if (meters < 975) return `${Math.round(meters / 50) * 50} m`;
  if (meters < 10_000) return `${(meters / 1_000).toFixed(1)} km`;
  return `${Math.round(meters / 1_000)} km`;
}

/**
 * Deterministic review data used by the browser preview and official G2
 * simulator. It never starts location, search, routing, or native actions.
 */
/** Synthetic walking route near N Lincoln Ave & W Fullerton Ave, Chicago:
 * north up Lincoln, right at the corner ~57 m ahead, then a short dog-leg.
 * The schematic draws it heading-up, so the turn reads as a right turn. */
const VISUAL_ROUTE_MAP: NavigationMapModel = {
  geometry: [
    { latitude: 41.92350, longitude: -87.64620 },
    { latitude: 41.92420, longitude: -87.64620 },
    { latitude: 41.92490, longitude: -87.64620 },
    { latitude: 41.92541, longitude: -87.64620 },
    { latitude: 41.92541, longitude: -87.64473 },
    { latitude: 41.92541, longitude: -87.64331 },
    { latitude: 41.92610, longitude: -87.64331 },
  ],
  position: { latitude: 41.92490, longitude: -87.64620 },
  maneuverAlongM: 212,
  maneuverLabel: 'W Fullerton Ave',
};

export function navigationVisualFixture(state: NavigationVisualState): NavigationHudModel {
  switch (state) {
    case 'nav-destination':
      return {
        view: 'destination',
        primary: 'Corner Bakery',
        secondary: '2301 N Lincoln Ave, Chicago',
        hint: 'tap: confirm · swipe: retry',
        interaction: 'stationary',
        options: [],
        selectedIndex: 0,
      };
    case 'nav-repair':
      return {
        view: 'repair',
        primary: 'Which Corner Bakery?',
        secondary: 'We heard “Corner Bakery.”',
        hint: 'swipe: choose · tap: confirm',
        interaction: 'stationary',
        options: [
          '2301 N Lincoln Ave · 1.2 km',
          'W Chicago Ave · 4.8 km',
          'None of these · retry',
        ],
        choiceIds: ['place-lincoln', 'place-chicago', 'retry'],
        selectedIndex: 0,
      };
    case 'nav-mode':
      return {
        view: 'mode',
        primary: 'Choose travel mode',
        secondary: 'Corner Bakery · 1.2 km',
        hint: 'swipe: choose · tap: continue',
        interaction: 'stationary',
        options: ['Walk · 18 min', 'Cycle · 7 min', 'Drive · 4 min'],
        choiceIds: ['walking', 'cycling', 'driving'],
        selectedIndex: 0,
      };
    case 'nav-preview':
      return {
        view: 'preview',
        primary: 'Corner Bakery',
        secondary: '1.2 km · 18 min · via Lincoln Ave',
        hint: 'tap: start · up: mode · down: place',
        interaction: 'stationary',
        options: [],
        selectedIndex: 0,
        mode: 'walking',
        maneuver: 'depart',
        remainingDistanceM: 1_240,
        etaMinutes: 18,
        progress: 0,
      };
    case 'nav-guidance':
      return {
        view: 'guidance',
        primary: 'Turn right',
        secondary: 'onto W Fullerton Ave',
        hint: 'Then continue 350 m',
        interaction: 'none',
        options: [],
        selectedIndex: 0,
        mode: 'walking',
        maneuver: 'turn_right',
        maneuverDistanceM: 48,
        remainingDistanceM: 920,
        etaMinutes: 13,
        progress: 0.26,
        map: VISUAL_ROUTE_MAP,
      };
    case 'nav-uncertain':
      return {
        view: 'uncertain',
        primary: 'Finding your position',
        secondary: 'GPS accuracy is low',
        hint: 'Pause safely · route held',
        interaction: 'none',
        options: [],
        selectedIndex: 0,
        mode: 'walking',
        remainingDistanceM: 880,
        etaMinutes: 13,
        progress: 0.29,
      };
    case 'nav-wrong-way':
      return {
        view: 'wrong_way',
        primary: 'Wrong direction',
        secondary: 'Turn around when safe',
        hint: 'Route resumes automatically',
        interaction: 'none',
        options: [],
        selectedIndex: 0,
        mode: 'cycling',
        maneuver: 'u_turn',
        remainingDistanceM: 1_050,
        etaMinutes: 8,
        progress: 0.24,
      };
    case 'nav-off-route':
      return {
        view: 'off_route',
        primary: 'Off route',
        secondary: 'Stop when safe',
        hint: 'Checking a new route…',
        interaction: 'none',
        options: [],
        selectedIndex: 0,
        mode: 'cycling',
        remainingDistanceM: 1_180,
        etaMinutes: 9,
        progress: 0.22,
      };
    case 'nav-rerouting':
      return {
        view: 'rerouting',
        primary: 'Rerouting',
        secondary: 'Finding a safer path',
        hint: 'Old directions paused',
        interaction: 'none',
        options: [],
        selectedIndex: 0,
        mode: 'cycling',
        maneuver: 'continue',
        remainingDistanceM: 1_180,
        etaMinutes: 9,
        progress: 0.22,
      };
    case 'nav-arrived':
      return {
        view: 'arrived',
        primary: 'You have arrived',
        secondary: 'Corner Bakery · on your right',
        hint: 'tap: finish',
        interaction: 'stationary',
        options: [],
        selectedIndex: 0,
        mode: 'walking',
        maneuver: 'arrive',
        remainingDistanceM: 0,
        etaMinutes: 0,
        progress: 1,
      };
    case 'nav-error':
      return {
        view: 'error',
        primary: 'Navigation unavailable',
        secondary: 'Location could not be started',
        hint: 'tap: retry · swipe: cancel',
        interaction: 'stationary',
        options: [],
        selectedIndex: 0,
      };
  }
}

// ---------------------------------------------------------------------------
// Native text-page & hybrid schematic rendering
//
// Every navigation frame uses Even's uniform borderless firmware page:
// - Pre-route, warning, arrival, and error views use the full-width 16 px-inset
//   firmware text region (`NAVIGATION_NATIVE_TEXT_WIDTH = 544`, matching
//   `READER_TEXT_PAGE.textWidth` across Mail, Messages, QuickNotes, and News).
// - Active `guidance` pairs a left-half native firmware text column
//   (`NAVIGATION_GUIDANCE_TEXT_WIDTH = 268`) — updated in place via
//   `textContainerUpgrade` (~83 ms, zero image tiles on distance/ETA ticks) —
//   with a single right-half `264x144` vector route schematic image container
//   (`NavigationCard.ts`) that repaints only when quantized route geometry moves.
// ---------------------------------------------------------------------------

/**
 * Firmware lines the text page's main container holds (`ASSISTANT_ANSWER_PAGE.lineCapacity`).
 */
export const NAVIGATION_NATIVE_LINE_BUDGET = 7;

/** Inner text width of an inset full-width region: 576 − 2·16 (`READER_TEXT_PAGE.textWidth`). */
export const NAVIGATION_NATIVE_TEXT_WIDTH = 544;

/** Inner text width of the left guidance column beside the schematic (`276 − 2·4`). */
export const NAVIGATION_GUIDANCE_TEXT_WIDTH = 268;

/** Firmware-font-verified maneuver arrows (matches `ActivityChips.ts`). */
const GUIDANCE_MANEUVER_ARROW: Readonly<Record<NavigationManeuver, string>> = {
  depart: '↑',
  continue: '↑',
  turn_left: '←',
  turn_right: '→',
  slight_left: '↖',
  slight_right: '↗',
  u_turn: '↓',
  arrive: '◆',
};

/** Rows of options a repair/mode card shows. The step name, primary and
 * secondary take three lines; the cursor window takes the remaining four. */
export const NAVIGATION_NATIVE_OPTION_ROWS = NAVIGATION_NATIVE_LINE_BUDGET - 3;

export const NAVIGATION_NATIVE_SELECTED_MARKER = '▶ ';
export const NAVIGATION_NATIVE_UNSELECTED_MARKER = '  ';

/**
 * Control-surface hints per view: tap, swipe, double-click only. The Mac's
 * hint is reused when it names a control; one that tells the wearer to SAY
 * something is dropped, because the text page is a control surface the
 * firmware delivers through sysEvent/textEvent, not a voice prompt. These
 * are the words `navigationVisualFixture` shows in the browser preview and
 * the simulator, so native and raster say the same thing.
 */
export const NAVIGATION_NATIVE_DEFAULT_HINTS: Readonly<Record<NavigationView, string>> = {
  destination: 'tap: confirm · swipe: retry',
  repair: 'swipe: choose · tap: confirm',
  mode: 'swipe: choose · tap: continue',
  preview: 'tap: start · up: mode · down: place',
  guidance: '2x tap: menu',
  uncertain: 'Pause safely · route held',
  wrong_way: 'Route resumes automatically',
  off_route: 'Checking a new route…',
  rerouting: 'Old directions paused',
  arrived: 'tap: finish',
  error: 'tap: retry · swipe: cancel',
};

const SAY_WORD = /\bsay\b/iu;
const CONTROL_HINT_WORD = /\b(?:tap|swipe|2x\s*tap|double-left|up:|down:)\b/iu;

/** Keep a Mac hint's control segments ("tap retry · swipe cancel"), drop its
 * voice ones ("say first/second"), and reduce "tap/say" to "tap". On active
 * guidance, non-control lookahead prose ("Then continue 350 m") belongs in
 * the maneuver body, while the bottom hint bar stays uniform gesture chrome. */
export function navigationNativeHint(model: NavigationHudModel): string {
  const control = model.hint
    .split('·')
    .map((segment) => segment.replace(/\s*\/\s*say\b/giu, '').trim())
    .map((segment) => segment.replace(/\bdouble-left\s+stops\b/giu, '2x tap: stop'))
    .filter((segment) => segment.length > 0 && !SAY_WORD.test(segment))
    .filter((segment) => model.view !== 'guidance' || CONTROL_HINT_WORD.test(segment))
    .join(' · ');
  const hint = control || NAVIGATION_NATIVE_DEFAULT_HINTS[model.view];
  return pxTruncate(hint.slice(0, 96), NAVIGATION_NATIVE_TEXT_WIDTH);
}

/** The step name, in the raster renderer's vocabulary (drawNavigation*). */
export function navigationNativeHeading(model: NavigationHudModel): string {
  switch (model.view) {
    case 'destination': return 'CONFIRM DESTINATION';
    case 'repair': return 'REPAIR DESTINATION';
    case 'mode': return 'TRAVEL MODE';
    case 'preview': {
      const prefix = model.mode === 'driving' ? 'DRIVE' : model.mode === 'cycling' ? 'CYCLE' : 'WALK';
      return `${prefix} · ROUTE PREVIEW`;
    }
    case 'guidance': {
      const prefix = model.mode === 'driving' ? 'DRIVING' : model.mode === 'cycling' ? 'CYCLING' : 'WALKING';
      return `${prefix} · ACTIVE`;
    }
    case 'uncertain': return 'UNCERTAIN';
    case 'wrong_way': return 'WRONG WAY';
    case 'off_route': return 'OFF ROUTE';
    case 'rerouting': return 'REROUTING';
    case 'arrived': return 'ARRIVED';
    case 'error': return 'NAVIGATION ERROR';
  }
}

/** The warning views' fact line, as drawNavigationWarning's footer words it
 * ("880 m left") with the active view's mode and minutes when the model has
 * them ("Walking · 880 m left · 13 min"). A pre-route wait (no distance, no
 * route) has no facts and gets no line. */
function navigationNativeFacts(model: NavigationHudModel): string | undefined {
  if (model.view !== 'uncertain' && model.view !== 'wrong_way'
    && model.view !== 'off_route' && model.view !== 'rerouting') {
    return undefined;
  }
  if (model.remainingDistanceM === undefined) {
    return isNavigationSetupStatus(model) ? undefined : 'Route held';
  }
  const facts: string[] = [];
  if (model.mode) {
    facts.push(model.mode === 'driving' ? 'Driving' : model.mode === 'cycling' ? 'Cycling' : 'Walking');
  }
  facts.push(`${formatNavigationDistance(model.remainingDistanceM)} left`);
  if (model.etaMinutes !== undefined && Number.isFinite(model.etaMinutes)) {
    facts.push(`${model.etaMinutes} min`);
  }
  return facts.join(' · ');
}

/** One firmware row from Mac text: inner runs of whitespace (and any
 * newline) become one space, so a row count is a line count. */
function foldWhitespace(text: string): string {
  return text.replace(/\s+/gu, ' ').trim();
}

/** The option rows with the cursor marker, windowed so the selected row is
 * always on the page; a swipe moves the marker, never the words. */
function navigationNativeOptionRows(model: NavigationHudModel): string[] {
  const options = model.options.map(foldWhitespace).filter((option) => option.length > 0);
  if (options.length === 0) return [];
  const rows = NAVIGATION_NATIVE_OPTION_ROWS;
  const selected = Math.max(0, Math.min(options.length - 1, Math.floor(model.selectedIndex)));
  const start = Math.min(Math.max(0, selected - rows + 1), Math.max(0, options.length - rows));
  return options.slice(start, start + rows).map((option, offset) => (
    `${start + offset === selected ? NAVIGATION_NATIVE_SELECTED_MARKER : NAVIGATION_NATIVE_UNSELECTED_MARKER}${option}`
  ));
}

function wrapFittedLines(text: string, width: number, maxLines: number): string[] {
  const clean = foldWhitespace(text);
  if (!clean || maxLines <= 0) return [];
  const wrapped = layoutLines(clean, width)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
  if (wrapped.length <= maxLines) {
    return wrapped.map((line) => pxTruncate(line, width));
  }
  const head = wrapped.slice(0, maxLines - 1).map((line) => pxTruncate(line, width));
  const tail = pxTruncate(wrapped.slice(maxLines - 1).join(' '), width);
  return [...head, tail];
}

function navigationGuidanceBody(model: NavigationHudModel, width: number): string {
  const rows: string[] = [pxTruncate(navigationNativeHeading(model), width)];
  const arrow = GUIDANCE_MANEUVER_ARROW[model.maneuver ?? 'continue'];
  const dist = model.maneuverDistanceM !== undefined && Number.isFinite(model.maneuverDistanceM)
    ? formatNavigationDistance(model.maneuverDistanceM)
    : '';
  const primary = foldWhitespace(model.primary);
  const lead = [dist ? `${arrow} ${dist}` : arrow, primary].filter(Boolean).join(' · ');
  const leadWrapped = wrapFittedLines(lead, width, 2);
  rows.push(...leadWrapped);

  let secondary = foldWhitespace(model.secondary);
  if (primary && secondary.toLowerCase().startsWith(`${primary.toLowerCase()} `)) {
    secondary = secondary.slice(primary.length).trim();
  }
  const thenSegments = model.hint
    .split('·')
    .map((segment) => foldWhitespace(segment))
    .filter((segment) => segment.length > 0 && !SAY_WORD.test(segment) && !CONTROL_HINT_WORD.test(segment));
  const thenLine = thenSegments.join(' · ');

  const factsParts: string[] = [];
  if (model.remainingDistanceM !== undefined && Number.isFinite(model.remainingDistanceM)) {
    factsParts.push(`${formatNavigationDistance(model.remainingDistanceM)} left`);
  }
  if (model.etaMinutes !== undefined && Number.isFinite(model.etaMinutes)) {
    factsParts.push(`${Math.max(0, Math.round(model.etaMinutes))} min`);
  }
  const factsLine = factsParts.length > 0 ? pxTruncate(factsParts.join(' · '), width) : undefined;

  const reservedBottom = (thenLine ? 1 : 0) + (factsLine ? 1 : 0);
  const secondaryBudget = Math.max(0, NAVIGATION_NATIVE_LINE_BUDGET - rows.length - reservedBottom);
  if (secondary && secondaryBudget > 0) {
    rows.push(...wrapFittedLines(secondary, width, Math.min(2, secondaryBudget)));
  }
  if (thenLine && rows.length < NAVIGATION_NATIVE_LINE_BUDGET - (factsLine ? 1 : 0)) {
    rows.push(pxTruncate(thenLine, width));
  }
  if (factsLine && rows.length < NAVIGATION_NATIVE_LINE_BUDGET) {
    rows.push(factsLine);
  }
  return rows.slice(0, NAVIGATION_NATIVE_LINE_BUDGET).join('\n');
}

export interface NavigationNativePage {
  body: string;
  hint: string;
  schematic?: boolean;
}

/**
 * The firmware text page for every navigation frame. Pre-route, warning,
 * arrival, and error views span the full 544 px inset width; active `guidance`
 * narrows its text column to `NAVIGATION_GUIDANCE_TEXT_WIDTH` (268 px) whenever
 * `options.schematic` is true and the model carries a usable route polyline.
 */
export function navigationNativeBody(
  model: NavigationHudModel,
  options: { schematic?: boolean } = {},
): NavigationNativePage {
  if (model.view === 'guidance') {
    const hasGeometry = Boolean(model.map && model.map.geometry.length >= 2);
    const schematic = Boolean(options.schematic && hasGeometry);
    const width = schematic ? NAVIGATION_GUIDANCE_TEXT_WIDTH : NAVIGATION_NATIVE_TEXT_WIDTH;
    return {
      body: navigationGuidanceBody(model, width),
      hint: navigationNativeHint(model),
      ...(schematic ? { schematic: true } : {}),
    };
  }
  const lines: string[] = [navigationNativeHeading(model)];
  const primary = foldWhitespace(model.primary);
  if (primary) lines.push(primary);
  const secondary = foldWhitespace(model.secondary);
  if (secondary) lines.push(secondary);
  const facts = navigationNativeFacts(model);
  if (facts) lines.push(facts);
  if (model.view === 'repair' || model.view === 'mode') lines.push(...navigationNativeOptionRows(model));
  const body = lines
    .slice(0, NAVIGATION_NATIVE_LINE_BUDGET)
    .map((line) => pxTruncate(line, NAVIGATION_NATIVE_TEXT_WIDTH))
    .join('\n');
  return { body, hint: navigationNativeHint(model) };
}
