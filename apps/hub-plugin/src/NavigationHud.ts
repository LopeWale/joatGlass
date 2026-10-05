import { pxTruncate } from '@evenrealities/pretext';

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

export type NavigationMode = 'walking' | 'cycling';

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
  /** Street surroundings rendered by the Mac. Without it the pane still
   * draws the route's shape; with it the wearer sees the actual blocks. */
  panel?: NavigationMapPanelModel;
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
/** A real walking route near 1007 N Shartel Ave, Oklahoma City — the same
 * place the checked-in map sample covers, so the development frame shows
 * the true composite: streets underneath, route and position on top. A
 * fixture whose route sits in a different city verifies nothing. */
const VISUAL_ROUTE_MAP: NavigationMapModel = {
  geometry: [
    { latitude: 35.47590, longitude: -97.52564 },
    { latitude: 35.47660, longitude: -97.52564 },
    { latitude: 35.47730, longitude: -97.52564 },
    { latitude: 35.47781, longitude: -97.52564 },
    { latitude: 35.47781, longitude: -97.52430 },
    { latitude: 35.47781, longitude: -97.52300 },
    { latitude: 35.47850, longitude: -97.52300 },
  ],
  position: { latitude: 35.47730, longitude: -97.52564 },
};

export function navigationVisualFixture(state: NavigationVisualState): NavigationHudModel {
  switch (state) {
    case 'nav-destination':
      return {
        view: 'destination',
        primary: 'Cajun Corner',
        secondary: '2301 N Lincoln Ave, Chicago',
        hint: 'tap: confirm · swipe: retry',
        interaction: 'stationary',
        options: [],
        selectedIndex: 0,
      };
    case 'nav-repair':
      return {
        view: 'repair',
        primary: 'Which Cajun Corner?',
        secondary: 'We heard “Cajun Corner.”',
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
        secondary: 'Cajun Corner · 1.2 km',
        hint: 'swipe: choose · tap: continue',
        interaction: 'stationary',
        options: ['Walk · 18 min', 'Cycle · 7 min'],
        choiceIds: ['walking', 'cycling'],
        selectedIndex: 0,
      };
    case 'nav-preview':
      return {
        view: 'preview',
        primary: 'Cajun Corner',
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
        secondary: 'Cajun Corner · on your right',
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
// Native text-page rendering
//
// Every navigation frame except live guidance is a text card: a step name,
// the Mac's primary and secondary, and for repair/mode a short option list
// with a cursor. Even's firmware text page draws all of that with ONE
// `rebuildPageContainer` and updates it in place with `textContainerUpgrade`,
// where the raster path uploads four image tiles at 0.2–2 s each over BLE.
// Guidance keeps the raster path: it is the only view whose renderer draws
// the map panel (HudCanvasRenderer.drawActiveNavigation); the warning views
// draw a glyph beside their words, and the words alone carry the meaning.
// ---------------------------------------------------------------------------

/**
 * Firmware lines the text page's main container holds. This is
 * `ASSISTANT_ANSWER_PAGE.lineCapacity` in HudPageBuilder — floor((214 −
 * 2·4) / 27) with the firmware's fixed 27 px line height — restated here so
 * this module stays free of the page builder; NavigationHud.native.test.ts
 * pins the two equal.
 */
export const NAVIGATION_NATIVE_LINE_BUDGET = 7;

/** Inner text width of a full-width region: 576 − 2·REGION_PAD (HudPageBuilder). */
export const NAVIGATION_NATIVE_TEXT_WIDTH = 568;

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
  guidance: 'double-click: menu',
  uncertain: 'Pause safely · route held',
  wrong_way: 'Route resumes automatically',
  off_route: 'Checking a new route…',
  rerouting: 'Old directions paused',
  arrived: 'tap: finish',
  error: 'tap: retry · swipe: cancel',
};

const SAY_WORD = /\bsay\b/iu;

/** Keep a Mac hint's control segments ("tap retry · swipe cancel"), drop its
 * voice ones ("say first/second"), and reduce "tap/say" to "tap". */
export function navigationNativeHint(model: NavigationHudModel): string {
  const control = model.hint
    .split('·')
    .map((segment) => segment.replace(/\s*\/\s*say\b/giu, '').trim())
    .filter((segment) => segment.length > 0 && !SAY_WORD.test(segment))
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
    case 'preview': return `${model.mode === 'cycling' ? 'CYCLE' : 'WALK'} · ROUTE PREVIEW`;
    case 'guidance': return `${model.mode === 'cycling' ? 'CYCLING' : 'WALKING'} · ACTIVE`;
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
  if (model.mode) facts.push(model.mode === 'cycling' ? 'Cycling' : 'Walking');
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

/**
 * The firmware text page for one navigation frame, or `undefined` when the
 * frame must stay raster (live guidance: the map). Pure — the same model
 * always gives the same page, and nothing here measures against a canvas;
 * `pxTruncate` is the installed glyph table.
 *
 * `body` is at most NAVIGATION_NATIVE_LINE_BUDGET lines, each fitted to the
 * region width: the step name, the primary, the secondary, then the fact
 * line (warnings) or the option rows (repair/mode). `hint` is a control
 * hint only; see `navigationNativeHint`.
 */
export function navigationNativeBody(
  model: NavigationHudModel,
): { body: string; hint: string } | undefined {
  if (model.view === 'guidance') return undefined;
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
