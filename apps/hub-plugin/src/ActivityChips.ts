import { pomodoroRemainingMs } from './PanelRail';
import { formatNavigationDistance, type NavigationHudModel, type NavigationManeuver } from './NavigationHud';
import type { HudPanelPomodoro } from './types';

/**
 * What else is running, in a few characters each.
 *
 * Even's own features hold the display one at a time: Navigate, Conversate
 * and a timer cannot share the lens. JOAT's reason to exist is that they
 * can. Whatever page is on the glass, its one header line carries the other
 * features that are still running — the Pomodoro's minutes and the next
 * turn with its distance — so a conversation, a list or a reader never
 * hides them. Every glyph here is in the firmware font (checked against
 * `getAdvW` in `@evenrealities/pretext`): the arrows ↑ ↗ → ↘ ↓ ↙ ← ↖ and ◆
 * are, the clock and turn-arrow glyphs (⏱ ↱ ↰) are not.
 */

const PHASE_WORD: Readonly<Record<Exclude<HudPanelPomodoro['phase'], 'idle'>, string>> = {
  focus: 'Focus',
  break: 'Break',
  long_break: 'Break',
};

/** "Focus 12m", "Break 4m", "Paused 12m"; nothing while the timer is idle.
 * Minutes round up, so the chip never says 0 while time is left. */
export function pomodoroChip(pomodoro: HudPanelPomodoro, nowMs: number): string | undefined {
  if (pomodoro.phase === 'idle') return undefined;
  const remaining = pomodoroRemainingMs(pomodoro, nowMs);
  const minutes = Math.ceil(remaining / 60_000);
  if (!pomodoro.running) return `Paused ${minutes}m`;
  return remaining > 0 ? `${PHASE_WORD[pomodoro.phase]} ${minutes}m` : `${PHASE_WORD[pomodoro.phase]} done`;
}

const MANEUVER_ARROW: Readonly<Record<NavigationManeuver, string>> = {
  depart: '↑',
  continue: '↑',
  turn_left: '←',
  turn_right: '→',
  slight_left: '↖',
  slight_right: '↗',
  u_turn: '↓',
  arrive: '◆',
};

/** "→ 80 m", "↑ 1.2 km", "◆ 30 m" while guiding; the warning in a word
 * while the route is in doubt; nothing for the pages before a route. */
export function navigationChip(navigation: NavigationHudModel): string | undefined {
  switch (navigation.view) {
    case 'guidance': {
      const arrow = MANEUVER_ARROW[navigation.maneuver ?? 'continue'];
      return navigation.maneuverDistanceM === undefined || !Number.isFinite(navigation.maneuverDistanceM)
        ? arrow
        : `${arrow} ${formatNavigationDistance(navigation.maneuverDistanceM)}`;
    }
    case 'uncertain': return 'Locating';
    case 'wrong_way': return 'Wrong way';
    case 'off_route': return 'Off route';
    case 'rerouting': return 'Rerouting';
    case 'arrived': return 'Arrived';
    default: return undefined;
  }
}

/** The views that belong to a route in progress: the ones a chip stands in
 * for when another page is on the glass. The rest (choose the place, the
 * mode, the preview, an error) ask the wearer something and take the glass. */
export function isRouteProgressView(navigation: NavigationHudModel): boolean {
  return navigationChip(navigation) !== undefined;
}
