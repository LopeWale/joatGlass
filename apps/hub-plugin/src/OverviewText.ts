/**
 * Home as firmware text: what goes in each band, measured for the glass.
 *
 * Sources (everything-evenhub skills, read 2026-10-05):
 * - glasses-ui: 576x288 canvas, at most 8 text containers, exactly one
 *   event-capture container, `textContainerUpgrade` is flicker-free and
 *   "much faster than image updates" (an image frame costs 0.5-2 s over
 *   BLE), `rebuildPageContainer` flickers briefly.
 * - font-measurement: one 27 px firmware line; a container's text area is
 *   its size minus 2x(padding + border); measure with @evenrealities/pretext
 *   and cut each line with pxTruncate so nothing wraps into a scrollbar.
 * - design-guidelines: `>` marks the selected row, `━`/`─` draw a progress
 *   bar, `●`/`○` are selection dots, `│` is a rule; all are firmware glyphs.
 *
 * The band contents are pure functions of the overview frame so the
 * presenter can diff them and send only the bands that changed.
 */
import { getTextWidth, pxTruncate } from '@evenrealities/pretext';
import { resolveHudOverview } from './HudCanvasRenderer';
import type { HudVisualFrame } from './HudVisualSurface';
import {
  PANEL_TAB_TITLES,
  formatMinutesLeft,
  pomodoroBlockLabel,
  pomodoroCycleDots,
  pomodoroRemainingMs,
  type PanelContent,
} from './PanelRail';
import { CANVAS, PANEL_TABS, type HudPanelPomodoro } from './types';

/** Firmware line box (font-measurement: "Line height is 27px"). */
export const OVERVIEW_LINE = 27;
/** Inset on every band; the text box is size - 2*PAD. */
export const OVERVIEW_PAD = 2;
const band = (lines: number): number => lines * OVERVIEW_LINE + 2 * OVERVIEW_PAD;

/**
 * Six text containers tile the canvas edge to edge:
 *
 *   y=0    h=31   header   1 line, full width   (clock · word · chips · ● · ▇)
 *   y=31   h=112  info     4 lines | pane  8 lines, x=158..576
 *   y=143  h=112  tabs     4 lines |                         -> 255
 *   y=257  h=31   hint     1 line, full width                -> 288
 *
 * plus a 2 px bordered rule in the 8 px gutter at x=153. The pane has no
 * title line of its own: the rail's `>` already names the tab, so all eight
 * pane lines carry content, and a swipe changes two bands (rail + pane),
 * which is two flicker-free upgrades instead of a rebuild.
 */
export const OVERVIEW_TEXT_GEOMETRY = {
  header: { x: 0, y: 0, width: CANVAS.width, height: band(1) },
  info: { x: 0, y: band(1), width: 150, height: band(4) },
  tabs: { x: 0, y: band(1) + band(4), width: 150, height: band(4) },
  pane: { x: 158, y: band(1), width: CANVAS.width - 158, height: band(8) },
  rule: { x: 153, y: band(1) + OVERVIEW_PAD, width: 2, height: band(8) - 2 * OVERVIEW_PAD },
  hint: { x: 0, y: CANVAS.height - band(1), width: CANVAS.width, height: band(1) },
} as const;

/** Lines a band holds and its text width, by the font-measurement rule. */
export function bandBox(box: { width: number; height: number }, inset = OVERVIEW_PAD): {
  innerWidth: number; lines: number;
} {
  return {
    innerWidth: box.width - 2 * inset,
    lines: Math.max(1, Math.floor((box.height - 2 * inset) / OVERVIEW_LINE)),
  };
}

export const PANE_LINES = bandBox(OVERVIEW_TEXT_GEOMETRY.pane).lines;       // 8
export const PANE_TEXT_WIDTH = bandBox(OVERVIEW_TEXT_GEOMETRY.pane).innerWidth; // 414

export interface OverviewTextView {
  /** The same live header line every firmware page carries. */
  header: string;
  /** Left column, top: date, weather, unread. */
  info: string[];
  tabs: { label: string; active: boolean }[];
  /** Right pane, one line per entry; the first may be a title. */
  pane: string[];
  /** 0-4; Mac urgency carries into the pane. */
  paneBrightness: number;
  hint: string;
}

/** The phone's own calendar date ("Mon, Oct 5"): the plugin runs on the
 * wearer's phone, in the wearer's zone. The time is in the header. */
export function overviewDate(now: Date): string {
  try {
    return now.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
  } catch {
    return '';
  }
}

/** A progress bar of firmware glyphs that fills `width` pixels, measured,
 * not counted: `━` and `─` are the design-guidelines progress glyphs. */
export function progressBar(fraction: number, width: number): string {
  const unit = Math.max(1, getTextWidth('━'));
  const cells = Math.max(1, Math.floor(width / unit));
  const filled = Math.round(Math.min(1, Math.max(0, fraction)) * cells);
  return '━'.repeat(filled) + '─'.repeat(cells - filled);
}

/** The Pomodoro card: block, minutes left, a bar of the phase, the cycle. */
export function pomodoroPaneRows(
  pomodoro: HudPanelPomodoro | undefined, content: PanelContent | undefined, nowMs: number,
): string[] {
  if (content?.empty) return [content.headline ?? 'Timer unavailable', content.empty];
  if (!pomodoro) return ['Focus timer', 'Say “JOAT, start a pomodoro”'];
  if (pomodoro.phase === 'idle') {
    return [pomodoroBlockLabel(pomodoro), 'tap: start', pomodoroCycleDots(pomodoro.completed)];
  }
  const remaining = pomodoroRemainingMs(pomodoro, nowMs);
  const done = pomodoro.lengthMs > 0 ? 1 - remaining / pomodoro.lengthMs : 0;
  return [
    pomodoroBlockLabel(pomodoro),
    `${formatMinutesLeft(remaining)}${pomodoro.running ? '' : ' · paused'}`,
    progressBar(done, PANE_TEXT_WIDTH),
    pomodoroCycleDots(pomodoro.completed),
    ...(content?.rows ?? []),
  ];
}

/** Cut every line to the band's text width and the band's line count, so
 * the firmware never wraps a line and never draws a scrollbar. */
export function fitBand(lines: readonly string[], box: { width: number; height: number }): string {
  const { innerWidth, lines: capacity } = bandBox(box);
  const fitted = lines
    .map((line) => line.replace(/\s+$/u, ''))
    .slice(0, capacity)
    .map((line) => (line ? pxTruncate(line, innerWidth) : ''));
  const text = fitted.join('\n');
  return text.trim() ? text : ' ';
}

/** The rail: `>` marks the lit tab. The font is proportional, so the
 * unselected indent is measured to equal `> ` (15 px = three spaces). */
export function railRows(tabs: readonly { label: string; active: boolean }[]): string[] {
  const cursor = '> ';
  const spaces = Math.max(1, Math.round(getTextWidth(cursor) / Math.max(1, getTextWidth(' '))));
  const indent = ' '.repeat(spaces);
  return tabs.map(({ label, active }) => `${active ? cursor : indent}${label}`);
}

/**
 * What Home shows, from the same frame the image overview drew and with
 * the same priorities (resolveHudOverview): a link state first, then a
 * reply, a notification or a calendar line, then the lit tab's card.
 */
export function composeOverviewText(
  frame: Extract<HudVisualFrame, { kind: 'overview' }>,
  header: string,
  now: Date = new Date(),
): OverviewTextView {
  const overview = resolveHudOverview(frame);
  const rail = frame.rail;
  const tabs = PANEL_TABS.map((tab) => ({ label: PANEL_TAB_TITLES[tab], active: rail?.activeTab === tab }));
  const weather = frame.ambient
    ? `${frame.ambient.temperature} ${frame.ambient.condition}`.trim()
    : '';
  const unread = frame.ambient?.unread ?? 0;
  const info = [overviewDate(now), weather, unread > 0 ? `${unread} unread` : ''].filter(Boolean);
  const content = rail?.content;
  // Each tab's resting card, with the image overview's own empty copy.
  const tabRows = (): string[] => {
    switch (rail?.activeTab) {
      case 'pomodoro':
        return content?.pomodoro || content?.empty
          ? pomodoroPaneRows(content?.pomodoro, content, now.getTime())
          : ['Focus timer', 'tap: open the tab to start'];
      case 'news':
        if (!content) return ['Waiting for JOAT on your Mac…'];
        if (content.rows.length === 0) return [content.empty ?? 'No headlines yet.', 'tap: open · long-press: refresh'];
        return content.headline ? [content.headline, ...content.rows] : content.rows;
      case 'search':
        if (!content || (!content.headline && content.rows.length === 0)) {
          return [content?.empty ?? 'Say “JOAT, look up …”'];
        }
        return content.headline ? [content.headline, ...content.rows] : content.rows;
      default:
        // Notifications: the Mac's newest is in Main (handled above); at rest
        // the pane says what Home is for.
        return overview.kind === 'empty'
          ? [overview.primary, 'tap: open notifications']
          : ['No new notifications', 'tap: open notifications'];
    }
  };
  const boot = overview.kind === 'empty' && overview.title !== 'Ready';
  if (boot) {
    return {
      header, info, tabs,
      pane: [overview.title.toUpperCase(), overview.primary, overview.recovery],
      paneBrightness: frame.mainBrightness,
      hint: overview.hint,
    };
  }
  if (overview.kind === 'content' && overview.main) {
    return {
      header, info, tabs,
      pane: [overview.main.label, overview.main.value, ...(overview.tray ? [overview.tray] : [])],
      paneBrightness: frame.mainBrightness,
      hint: frame.hint,
    };
  }
  return {
    header,
    info,
    tabs,
    pane: tabRows(),
    paneBrightness: 4,
    // Home's own gesture line (Chrome's), else the resolver's.
    hint: frame.hint.trim() || (overview.kind === 'empty' ? overview.hint : ''),
  };
}
