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
