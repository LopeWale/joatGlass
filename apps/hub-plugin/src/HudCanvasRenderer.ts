import { GesturePolicy } from './GesturePolicy';
import { HOME_COLUMN, type HomeColumnView } from './HomeImages';
import { REMINDER_CARD, reminderCardLayout, type ReminderCardView } from './ReminderCard';
import {
  EVEN_READER, drawEvenText, evenLineHeight, evenTextWidth, fitOneLineEven, loadEvenFont, readerHint,
  readerLineTops,
  type EvenReaderLine,
} from './EvenFont';
import chatCircleSvg from '@phosphor-icons/core/regular/chat-circle-dots.svg?raw';
import bellSvg from '@phosphor-icons/core/regular/bell.svg?raw';
import calendarSvg from '@phosphor-icons/core/regular/calendar-dots.svg?raw';
import gearSvg from '@phosphor-icons/core/regular/gear.svg?raw';
import taskSvg from '@phosphor-icons/core/regular/check-square-offset.svg?raw';
import moreSvg from '@phosphor-icons/core/regular/dots-three-circle.svg?raw';
import quickNoteSvg from '@phosphor-icons/core/regular/note-pencil.svg?raw';
import activitySvg from '@phosphor-icons/core/regular/pulse.svg?raw';
import navigationSvg from '@phosphor-icons/core/regular/navigation-arrow.svg?raw';
import microphoneSvg from '@phosphor-icons/core/regular/microphone.svg?raw';
import batteryHighSvg from '@phosphor-icons/core/regular/battery-high.svg?raw';
import batteryFullSvg from '@phosphor-icons/core/regular/battery-full.svg?raw';
import batteryMediumSvg from '@phosphor-icons/core/regular/battery-medium.svg?raw';
import batteryLowSvg from '@phosphor-icons/core/regular/battery-low.svg?raw';
import batteryEmptySvg from '@phosphor-icons/core/regular/battery-empty.svg?raw';
import batteryChargingSvg from '@phosphor-icons/core/regular/battery-charging.svg?raw';
import microphoneSlashSvg from '@phosphor-icons/core/regular/microphone-slash.svg?raw';
import {
  HUD_LOW_BATTERY_PCT, type HudStatusModel, hudStatusLabel, statusModelFromText,
} from './HudStatus';
import weatherSvg from '@phosphor-icons/core/regular/cloud-sun.svg?raw';
import newspaperSvg from '@phosphor-icons/core/regular/newspaper.svg?raw';
import timerSvg from '@phosphor-icons/core/regular/timer.svg?raw';
import magnifyingGlassSvg from '@phosphor-icons/core/regular/magnifying-glass.svg?raw';
import {
  formatMinutesLeft,
  freshnessLabel,
  pomodoroBlockLabel,
  pomodoroCycleDots,
  pomodoroRemainingMs,
  type PanelRailVisual,
} from './PanelRail';
import {
  CANVAS,
  HeaderImageId,
  HomeColumnId,
  PANEL_TABS,
  ReminderCardId,
  SPINE_LABEL_BOX,
  type HudContentKind,
  type PanelContentTab,
  type PanelTab,
  type ProductivityHudModel,
} from './types';
import {
  splitGrayscaleTiles,
  type AssistantOverlayVisual,
  type HudFrameRenderer,
  type HudImageTile,
  type HudVisualFrame,
} from './HudVisualSurface';
import {
  formatNavigationDistance,
  type NavigationHudModel,
  type NavigationManeuver,
} from './NavigationHud';
import { buildRouteSchematic, type RouteSchematic, type ScreenPoint } from './NavigationSchematic';

import {
  TYPE,
  LEAD,
  mono,
  HINT_BASELINE_Y,
  upper,
} from './HudTypography';

const BRIGHT = 238;
const SUPPORT = 170;
const CHROME = 102;
const QUIET = 68;
const HAIRLINE = 51;

/** Where content starts and stops on the 576px lens.
 *
 * Earlier layouts ran x=36..540 — 504px of 576, leaving 72px unused across
 * the side margins and causing premature line wrapping.
 *
 * Fourteen, not zero: the outermost pixels of a waveguide are where the
 * image is least reliable, and a glyph clipped by the lens edge is worse
 * than one that never reached it. Everything between is content. */
export const NOTIFICATION_DISMISS_HINT = 'tap: dismiss';

/** One size for an answer, always.
 *
 * The card previously scaled font sizes from 21px down to 12px per reply,
 * causing assistant answers to render at inconsistent sizes depending on
 * length. 14 is TYPE.body: the size the assistant PILL already used, and
 * the size apps/mac/joat/channels/cards.py computes its 46-char line
 * budget against. HudTypography.ts states the rule this restores: "no
 * draw site invents its own number any more".
 */


/**
 * The resting screen is a DASHBOARD, not a sentence.
 *
 * The wearer glances at these lenses dozens of times an hour without asking
 * JOAT anything, so the idle frame has to be worth the glance on its own:
 * a large clock anchoring the left, and whatever JOAT currently has to say
 * filling a second pane on the right. A single hairline divides them — one
 * frame, not a stack of cards.
 *
 * Time and date come from the phone clock, so this pane stays truthful even
 * when the Mac is offline: the HUD is never a dead rectangle.
 */
/** The last baseline a notification headline may occupy before it would
 * collide with the footer hint. */
export const OVERVIEW_TRAY_LAST_Y = 248;

export const OVERVIEW_DASHBOARD = {
  // The left column is ONE block — clock, then conditions — centred both
  // ways in the pane between the spine (y44) and the hint (y267). The date
  // used to open it at y60, balancing the ambient row 92 below the centre;
  // it now lives in the spine's left slot, so the two clock rows straddle
  // 156 and the ambient row closes the column.
  clockX: 26,
  /** The clock column is centred in the left half of the canvas. It used
   * to centre on the divider, which now sits east of the tile seam. */
  clockCenterX: 144,
  clockFontSize: 82,
  hoursBaselineY: 116,
  minutesBaselineY: 196,
  // The hairline is the right pane's LEFT EDGE, the way the platform's own
  // dashboard frames its panel; the tab rail sits between the clock and it.
  // Everything east of x=288 lives in the two right tiles, so a tab change
  // repaints those two and leaves the clock's tiles untouched — the same
  // economy the retired sidebar bought by leaving the right half alone.
  dividerX: 308.5,
  dividerTopY: 34,
  dividerBottomY: 252,
  paneX: 318,
  paneWidth: 230,
  labelBaselineY: 62,
  mainTopBaselineY: 92,
  mainLineHeight: LEAD.primary,
  mainMaxLines: 4,
  contextLabelBaselineY: 196,
  contextValueBaselineY: 214,
  trayBaselineY: 244,
  //: The margin facts sit under the clock, where the reference HUD puts
  //: them: what day it is, what it is like outside, what is waiting.
  ambientBaselineY: 246,
} as const;

/**
 * The overview's tab rail: notifications, news, pomodoro, search, stacked
 * on the outer-left edge of the right pane. Tabs sit OUTSIDE the pane so
 * the content keeps its width — the wearer's ruling, from the platform's
 * own dashboard — and east of the tile seam so a swipe costs two tiles.
 */
export const PANEL_RAIL = {
  /** Icon left edge on the resting overview: just past the seam at 288. */
  x: 290,
  iconSize: 14,
  /** Icon TOP for each of the five slots, evenly spaced between the spine
   * and the footer; the icon's centre is 7px lower. */
  slotY: [70, 106, 142, 178, 214],
} as const;

/** What a swipe, a tap and a hold do on a panel tab. Truthful: all wired.
 *
 * The resting hints name the mic because long press IS still the mic on
 * every resting tab — swiping off notifications used to drop the
 * affordance from the footer while it kept working. The pomodoro page
 * names it too: that tab is excluded from the refresh carve-out
 * (CapabilityMenu.isPanelRefreshable), so a hold there opens the mic. */
export const PANEL_OVERVIEW_HINT = 'tap: open · swipe: tabs';
export const PANEL_EXPANDED_HINT = 'tap: open · long tap: refresh · 2x tap: back';
export const POMODORO_EXPANDED_HINT = 'tap: choose · 2x tap: back';

export interface HudNotificationRow {
  source: string;
  age: string;
  headline: string;
}

/**
 * The home pane shows the notifications THEMSELVES, not a count.
 *
 * A counter ("2 notifications") tells the wearer only that they must open
 * another page — the exact menu page the reference HUDs prove is
 * unnecessary. The Mac sends one row per card as
 * "Source · age · headline", newest first; the wearer reads them at a
 * glance and never taps.
 */
export function parseHudNotificationRows(tray: string): HudNotificationRow[] {
  return tray
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const parts = line.split(' · ');
      if (parts.length < 3) return { source: '', age: '', headline: line };
      return {
        source: (parts[0] ?? '').trim(),
        age: (parts[1] ?? '').trim(),
        headline: parts.slice(2).join(' · ').trim(),
      };
    })
    .filter((row) => row.headline.length > 0);
}

/** Split "9:41" into the two stacked rows the dashboard prints. A missing
 * or malformed clock still renders placeholders rather than an empty pane. */
export function hudClockParts(time: string): { hours: string; minutes: string } {
  const match = time.trim().match(/^(\d{1,2}):(\d{2})/);
  if (!match) return { hours: '--', minutes: '--' };
  return {
    hours: (match[1] ?? '').padStart(2, '0'),
    minutes: match[2] ?? '--',
  };
}

const HUD_WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'] as const;

/** "Mon 9/1" — the phone's own date, which the Mac never has to supply. */
export function hudDateLabel(now: Date): string {
  const weekday = HUD_WEEKDAYS[now.getDay()] ?? '';
  return `${weekday} ${now.getMonth() + 1}/${now.getDate()}`;
}

/** Measured from the selected 576×288 reference panels. Keeping the major
 * anchors together makes visual drift obvious in review and in tests. */
export const HUD_VISUAL_GEOMETRY = {
  /** The spine is 24 px tall since 2026-10-05 (it was 36): the clock and
   * the page's word at a middle baseline of 12, the mic and battery as
   * icons alone. Everything below it moved up by the same 12 px. */
  spineHeight: 24,
  statusBaselineY: 12,
} as const;

/** Where the spine's right-tile icons sit. Both stay east of the tile
 * seam (x=288) so a mic or battery change dirties one tile. The mic is a
 * 14 px glyph; the battery is Phosphor's wide 18 px cell. From the
 * low-battery threshold down the number returns to the left of the cell. */
export const SPINE_ICONS = {
  /** The running-feature chips ("Focus 12m · → 80 m") sit right-aligned
   * between the tile seam and the mic, so a chip change dirties one tile. */
  activityLeftX: 296,
  activityRightX: 476,
  micX: 486,
  micY: 5,
  micSize: 14,
  batteryX: 536,
  batteryY: 3,
  batterySize: 18,
  lowBatteryTextRightX: 530,
} as const;

/** The battery cell that matches the level: five Phosphor cells, and the
 * charging cell whatever the level — the charge mark used to be stripped
 * by the string parser before the spine ever saw it. */
export function batteryIconFor(
  status: Pick<HudStatusModel, 'battery' | 'charging'>,
): Extract<HudIconName, `battery${string}`> {
  if (status.charging) return 'batteryCharging';
  const level = status.battery ?? 0;
  if (level >= 90) return 'batteryFull';
  if (level >= 60) return 'batteryHigh';
  if (level >= 30) return 'batteryMedium';
  if (level > HUD_LOW_BATTERY_PCT) return 'batteryLow';
  return 'batteryEmpty';
}

/** The raster lists draw flat: every pixel the rounded frame occupied was
 * a pixel of content. The dashboard and the live map keep the shell. */
export function usesFlatHudShell(kind: HudVisualFrame['kind']): boolean {
  return kind === 'list' || kind === 'reader';
}

/** One run of text on a reader page, where it lands and how bright. */
export interface ReaderRun {
  text: string;
  x: number;
  y: number;
  width: number;
  alpha: number;
}

/** One mark of Home's column: words in Even's font, or an icon. */
export interface HomeColumnMark {
  tile: 0 | 1;
  x: number;
  /** Top, within the tile. */
  y: number;
  width: number;
  height: number;
  alpha: number;
  text?: string;
  icon?: HudIconName;
  /** A lit rule: the lit tab's underline. */
  bar?: boolean;
}

/** Inside each column tile: 16 px from the screen's edge, 16 px short of
 * the column's right edge, so nothing meets the rule or the edge. */
/** The dimmest Even-font words (an unlit tab, a reader's hint): the
 * firmware's own dimmest text level, 179/255 measured in the simulator,
 * avoiding lower alpha values that appear washed out on optical waveguides. */
const DIM_WORDS = 179 / 255;

/** Home's column beneath a notification card: dimmed well below the card's
 * own words, as Even's page under its card. */
const UNDER_CARD_ALPHA = 0.45;

const COLUMN_LEFT = 16;
const COLUMN_RIGHT = HOME_COLUMN.width - 16;
const COLUMN_ICON = 13;
const COLUMN_TEXT_X = COLUMN_LEFT + COLUMN_ICON + 6;

/**
 * Where Home's column draws each mark. The top tile: the date, the weather
 * with its icon, the unread count with the Messages icon. The bottom tile:
 * the tab rail, one row per tab with its icon, the lit tab bright and the
 * others dim. Pure, so the margins are tested without a canvas.
 */
export function homeColumnLayout(view: HomeColumnView): HomeColumnMark[] {
  const line = evenLineHeight();
  const marks: HomeColumnMark[] = [];
  const words = (tile: 0 | 1, x: number, y: number, text: string, alpha: number) => {
    const fitted = fitOneLineEven(text, COLUMN_RIGHT - x);
    if (fitted) marks.push({ tile, x, y, width: evenTextWidth(fitted), height: line, alpha, text: fitted });
  };
  const icon = (tile: 0 | 1, y: number, name: HudIconName, alpha: number) => {
    marks.push({ tile, x: COLUMN_LEFT, y: y + (line - COLUMN_ICON) / 2, width: COLUMN_ICON, height: COLUMN_ICON, alpha, icon: name });
  };
  let y = 10;
  if (view.info.date) { words(0, COLUMN_LEFT, y, view.info.date, 1); y += 26; }
  if (view.info.weather) { icon(0, y, 'weather', 1); words(0, COLUMN_TEXT_X, y, view.info.weather, 1); y += 24; }
  if (view.info.unread > 0) {
    icon(0, y, 'messages', 1);
    words(0, COLUMN_TEXT_X, y, `${view.info.unread} unread`, 1);
  }
  // 26 px rows fit four tabs in the 116 px tile; five (Notifications came
  // back first, 2026-11-09) take 22 px rows from 6 px, the last underline
  // ending at 115.
  const pitch = Math.min(26, Math.floor((HOME_COLUMN.tileHeight - 28) / Math.max(1, view.tabs.length - 1)));
  const railTop = pitch < 26 ? 6 : 8;
  view.tabs.forEach((tab, index) => {
    const rowY = railTop + index * pitch;
    const alpha = tab.active ? 1 : DIM_WORDS;
    icon(1, rowY, tab.tab, alpha);
    words(1, COLUMN_TEXT_X, rowY, tab.label, alpha);
    // The lit tab is underlined. Brightness alone does not mark it: in the
    // simulator (0.1.119) the lit and unlit labels measured the same, so a
    // swipe looked like it did nothing. The rule stays inside the column's
    // margins, under the label it marks.
    const label = marks[marks.length - 1];
    if (tab.active && label?.text && label.tile === 1 && label.y === rowY) {
      marks.push({ tile: 1, x: label.x, y: rowY + Math.ceil(line) + 1, width: label.width, height: 2, alpha: 1, bar: true });
    }
  });
  if (view.dim) for (const mark of marks) mark.alpha *= UNDER_CARD_ALPHA;
  const { clipBottom, clipTop } = view;
  if (clipBottom === undefined && clipTop === undefined) return marks;
  // Under JOAT's box or a notification card a row is left out whole, never
  // cut through a glyph.
  return marks.filter((mark) => {
    const top = HOME_COLUMN.top + mark.tile * HOME_COLUMN.tileHeight + mark.y;
    const extent = mark.bar ? mark.height : Math.max(mark.height, line);
    return (clipBottom === undefined || top + extent <= clipBottom)
      && (clipTop === undefined || top >= clipTop);
  });
}

/** Brightness of a reader's parts: the speaker over the words, the hint
 * well under both. */
const READER_ALPHA = { speaker: 1, body: 1, hint: DIM_WORDS } as const;

/**
 * Every run a reader page draws, in page coordinates. The renderer draws
 * exactly these, so the margin rule (16 px each side, 3 px top, 8 px
 * bottom) is checked here without a canvas.
 */
export function readerLayout(lines: readonly EvenReaderLine[], hint: string, clipBottom?: number): ReaderRun[] {
  const runs = readerRuns(lines, hint);
  // Under JOAT's box nothing is drawn: a line that would reach past the
  // box's top border goes, and so does the hint.
  return clipBottom === undefined ? runs : runs.filter((run) => run.y + evenLineHeight() <= clipBottom);
}

function readerRuns(lines: readonly EvenReaderLine[], hint: string): ReaderRun[] {
  const runs: ReaderRun[] = [];
  const tops = readerLineTops(lines);
  lines.forEach((line, index) => {
    const y = tops[index] ?? EVEN_READER.bodyTop;
    const x = EVEN_READER.marginX;
    const label = line.speaker ? line.text.slice(0, line.speaker) : '';
    if (label) runs.push({ text: label, x, y, width: evenTextWidth(label), alpha: READER_ALPHA.speaker });
    const rest = line.text.slice(label.length);
    const restX = x + evenTextWidth(label);
    if (rest.trim()) {
      runs.push({ text: rest, x: restX, y, width: evenTextWidth(line.text) - evenTextWidth(label), alpha: READER_ALPHA.body });
    }
  });
  const { left, right } = readerHint(hint);
  const hintY = EVEN_READER.hintBottom - evenLineHeight();
  if (left) {
    runs.push({ text: left, x: EVEN_READER.marginX, y: hintY, width: evenTextWidth(left), alpha: READER_ALPHA.hint });
  }
  if (right) {
    const width = evenTextWidth(right);
    runs.push({
      text: right, x: EVEN_READER.marginX + EVEN_READER.width - width, y: hintY, width, alpha: READER_ALPHA.hint,
    });
  }
  return runs;
}

export type HudIconName =
  | 'messages'
  | 'notifications'
  | 'calendar'
  | 'tasks'
  | 'planner'
  | 'more'
  | 'quicknotes'
  | 'activity'
  | 'navigation'
  | 'settings'
  | 'microphone'
  | 'microphoneSlash'
  | 'batteryFull'
  | 'batteryHigh'
  | 'batteryMedium'
  | 'batteryLow'
  | 'batteryEmpty'
  | 'batteryCharging'
  | 'weather'
  | 'news'
  | 'pomodoro'
  | 'search';

const ICON_SOURCES: Readonly<Record<HudIconName, string>> = {
  messages: chatCircleSvg,
  notifications: bellSvg,
  calendar: calendarSvg,
  tasks: taskSvg,
  // The overview's first tab: the open reminders, so the checkbox.
  planner: taskSvg,
  more: moreSvg,
  quicknotes: quickNoteSvg,
  activity: activitySvg,
  navigation: navigationSvg,
  settings: gearSvg,
  microphone: microphoneSvg,
  microphoneSlash: microphoneSlashSvg,
  batteryFull: batteryFullSvg,
  batteryHigh: batteryHighSvg,
  batteryMedium: batteryMediumSvg,
  batteryLow: batteryLowSvg,
  batteryEmpty: batteryEmptySvg,
  batteryCharging: batteryChargingSvg,
  weather: weatherSvg,
  news: newspaperSvg,
  pomodoro: timerSvg,
  search: magnifyingGlassSvg,
};

/** Transient receipts are a strip over the live page; failed/check/due stay full pages. */
export function usesActionPillLayout(phase: ProductivityHudModel['phase']): boolean {
  return phase === 'verifying'
    || phase === 'saved'
    || phase === 'monitoring'
    || phase === 'completed'
    || phase === 'cancelled'
    || phase === 'not_saved';
}

/** Geometry for the outcome strip: low on the glass like the reference pill,
 * clear of the status spine and the footer line. */
export const ACTION_PILL = {
  x: 88,
  y: 196,
  width: 400,
  height: 44,
  radius: 10,
  iconX: 106,
  iconY: 207,
  iconSize: 20,
  headlineX: 136,
  headlineY: 212,
  detailY: 229,
} as const;

export function productivityPhaseLabel(phase: ProductivityHudModel['phase']): string {
  const labels: Readonly<Record<ProductivityHudModel['phase'], string>> = {
    verifying: 'VERIFYING',
    saved: 'SAVED',
    monitoring: 'MONITORING',
    completed: 'COMPLETED',
    // These are terminal truth states, not generic progress failures.  The
    // wearer needs to know whether EventKit definitely refused the write or
    // whether it may have committed and therefore must not be retried.
    failed: 'FAILED',
    check: 'UNCERTAIN',
    cancelled: 'CANCELLED',
    not_saved: 'NOT SAVED',
    due: 'DUE NOW',
  };
  return labels[phase];
}

/** The wearable owns the escape wording for a live productivity surface.
 *
 * Native status text may describe a write or a due reminder, but it must not
 * conceal the one local action that is always available: return to the
 * unified Planning screen. This changes only the display route; it neither
 * cancels an in-flight native write nor claims an unavailable post-save undo.
 */
export function productivityActionHint(model: Pick<ProductivityHudModel, 'phase' | 'monitoringArmed'>): string {
  if (model.phase === 'due') return 'tap: complete · 2x tap: Reminders';
  if (model.phase === 'saved' && model.monitoringArmed) {
    return '2x tap: Reminders · monitoring next';
  }
  if (
    model.phase === 'saved'
    || model.phase === 'monitoring'
    || model.phase === 'completed'
    || model.phase === 'failed'
    || model.phase === 'check'
    || model.phase === 'cancelled'
    || model.phase === 'not_saved'
  ) {
    return '2x tap: Reminders · auto in 5s';
  }
  return '2x tap: Reminders';
}

function gray(level: number): string {
  const value = Math.max(0, Math.min(255, Math.round(level)));
  return `rgb(${value}, ${value}, ${value})`;
}

/** Convert the SDK's sticky 0...4 textColor contract to the authored 8-bit
 * canvas palette so Mac urgency survives the custom raster renderer. */
export function hudBrightnessGray(level: number): number {
  return [34, QUIET, CHROME, SUPPORT, BRIGHT][Math.max(0, Math.min(4, Math.round(level)))]!;
}

export function hudDisplayTitle(value: string): string {
  const normalized = value.replace(/\s*·\s*/g, ' · ').trim();
  if (!normalized || normalized !== normalized.toUpperCase()) return normalized;
  const planning = normalized.match(/^(PLANNING|CALENDAR|TASKS) · PLAN · (.+)$/);
  if (planning) {
    const root = titleCaseHudWords(planning[1] ?? 'PLANNING');
    const period = planning[2] ?? '';
    const scope = planningScopeForDisplay(period);
    // The month is named as the grid names it ("SEP 2026"): spelled out,
    // "PLANNING · MONTH · SEPTEMBER 2026" ran past the spine's label box
    // and drew "SEPTEMB…" on every month view.
    const displayedPeriod = titleCaseHudWords(
      scope === 'Month' ? period.replace(/^([A-Z]{3})[A-Z]+ /, '$1 ') : period,
    );
    const full = scope ? `${root} · ${scope} · ${displayedPeriod}` : `${root} · ${displayedPeriod}`;
    // A cross-month week ("AUG 31–SEP 6") still does not fit with its scope
    // word; the range already says it is a week.
    return evenTextWidth(upper(full)) <= SPINE_LABEL_BOX.width ? full : `${root} · ${displayedPeriod}`;
  }
  return normalized.split(' · ').map((part) => {
    if (part === 'JOAT' || /^\d+$/.test(part)) return part;
    if (part === 'QUICKNOTES') return 'QuickNotes';
    return titleCaseHudWords(part);
  }).join(' · ');
}

function planningScopeForDisplay(period: string): 'Month' | 'Week' | 'Day' | undefined {
  if (/^[A-Z]+ \d{4}$/.test(period)) return 'Month';
  if (/^[A-Z]{3} \d+–(?:[A-Z]{3} )?\d+$/.test(period)) return 'Week';
  if (/^(?:MON|TUE|WED|THU|FRI|SAT|SUN) [A-Z]{3} \d+$/.test(period)) return 'Day';
  return undefined;
}

function titleCaseHudWords(value: string): string {
  return value.toLowerCase().replace(/\b[a-z]/g, (letter) => letter.toUpperCase());
}

function loadSvg(source: string): Promise<HTMLImageElement> {
  const image = new Image();
  const svg = source.replace(/currentColor/g, '#ffffff');
  image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  return new Promise((resolve, reject) => {
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error('HUD icon asset failed to load'));
  });
}

function roundedRect(
  context: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
): void {
  const r = Math.min(radius, width / 2, height / 2);
  context.beginPath();
  context.moveTo(x + r, y);
  context.arcTo(x + width, y, x + width, y + height, r);
  context.arcTo(x + width, y + height, x, y + height, r);
  context.arcTo(x, y + height, x, y, r);
  context.arcTo(x, y, x + width, y, r);
  context.closePath();
}

function splitPair(value: string): [string, string] {
  const [first = '', ...rest] = value.split('\n');
  return [first.trim(), rest.join(' ').trim()];
}

export interface HudLabeledValue {
  label: string;
  value: string;
  structured: boolean;
}

export interface HudOverviewInput {
  status: string;
  main: string;
  mainKind?: HudContentKind;
  context: string;
  tray: string;
}

export type ResolvedHudOverview =
  | {
    kind: 'empty';
    title: string;
    primary: string;
    recovery: string;
    hint: string;
  }
  | {
    kind: 'content';
    main?: HudLabeledValue;
    tray?: string;
    context?: HudLabeledValue;
  };

/** Dashboard fixtures may send `LABEL\nVALUE`, but ordinary Mac replies and
 * transcripts are plain strings. Plain content is the value, never a label. */
export function parseHudLabeledValue(
  input: string,
  fallbackLabel: string,
  emptyValue = '',
): HudLabeledValue {
  const trimmed = input.trim();
  if (!trimmed) return { label: fallbackLabel, value: emptyValue, structured: false };
  if (!input.includes('\n')) {
    return { label: fallbackLabel, value: trimmed, structured: false };
  }
  const [label, value] = splitPair(input);
  return {
    label: label || fallbackLabel,
    value: value || emptyValue,
    structured: true,
  };
}

/** Resolve the Mac-owned Overview regions before drawing. An absent payload
 * is a real transport state, not a reply, schedule, notification, or query.
 * Keeping this pure makes the ordinary (fixture-free) boot state testable. */
export function resolveHudOverview(input: HudOverviewInput): ResolvedHudOverview {
  const main = input.main.trim();
  const context = input.context.trim();
  const tray = input.tray.trim().replace(/\s+waiting$/i, '');
  if (/\bclosed\b|\boffline\b/i.test(input.status)) {
    return {
      kind: 'empty',
      title: 'Offline',
      primary: 'JOAT is not connected.',
      recovery: 'Open JOAT Brain on the iPhone, or JOAT on the Mac.',
      hint: '2x tap: menu',
    };
  }
  if (/\breconnecting\b/i.test(input.status)) {
    return {
      kind: 'empty',
      title: 'Reconnecting',
      primary: 'Reconnecting to JOAT…',
      recovery: 'This screen will refresh automatically.',
      hint: '2x tap: menu',
    };
  }
  if (/\bconnecting\b/i.test(input.status)) {
    return {
      kind: 'empty',
      title: 'Connecting',
      primary: 'Waiting for JOAT on your Mac…',
      recovery: 'This screen will refresh automatically.',
      hint: '2x tap: menu',
    };
  }
  if (!main && !context && !tray) {
    return {
      kind: 'empty',
      title: 'Ready',
      primary: 'Ask JOAT anything.',
      recovery: 'Say “JOAT …” · tap then hold for menu.',
      hint: '2x tap: menu',
    };
  }

  return {
    kind: 'content',
    ...(main ? {
      main: input.mainKind === 'notification'
        ? {
          label: /(?:^|·)\s*Messages(?:\s|$)/i.test(main.split('\n', 1)[0] ?? '')
            ? 'MESSAGE'
            : 'NOTIFICATION',
          value: main.replace(/\s*\n\s*/g, ' ').trim(),
          structured: false,
        }
        : parseHudLabeledValue(main, input.mainKind === 'calendar' ? 'NEXT' : 'REPLY'),
    } : {}),
    ...(tray ? { tray } : {}),
    ...(context ? { context: parseHudLabeledValue(context, 'LAST QUERY') } : {}),
  };
}

function splitMessage(value: string): [string, string] {
  const marker = value.indexOf('·');
  if (marker < 0) return [value.trim(), ''];
  return [value.slice(0, marker).trim(), value.slice(marker + 1).trim()];
}

export interface HudListItemParts {
  primary: string;
  secondary: string;
  age: string;
}

/** The Mac prefixes real relative ages as `[2m]`; fixtures and empty/setup
 * rows omit the prefix, so the renderer never invents a timestamp. */
export function parseHudListItem(value: string, preservePreview = false): HudListItemParts {
  const ageMatch = value.match(/^\[([^\]]+)]\s*/);
  const content = ageMatch ? value.slice(ageMatch[0].length) : value;
  const [primary, secondary] = preservePreview ? [content, ''] : splitMessage(content);
  return { primary, secondary, age: ageMatch?.[1]?.trim() ?? '' };
}

/** Wrap every character into measured lines. Long unbroken values are split
 * instead of clipped, which matters for file names and confirmation targets. */
export function wrapHudText(
  text: string,
  maxWidth: number,
  measure: (value: string) => number,
): string[] {
  const words = text.trim().split(/\s+/).filter(Boolean);
  const lines: string[] = [];
  let line = '';

  const pushLongWord = (word: string): string => {
    const points = Array.from(word);
    let offset = 0;
    while (offset < points.length) {
      const remaining = points.length - offset;
      const fits = (count: number) => measure(points.slice(offset, offset + count).join('')) <= maxWidth;
      let low = 0;
      let high = 1;
      // Probe outward from one glyph, never repeatedly measure an entire
      // remaining URL or code token. Each row costs logarithmic probes.
      while (high < remaining && fits(high)) {
        low = high;
        high = Math.min(remaining, high * 2);
      }
      if (fits(high)) low = high;
      else {
        while (high - low > 1) {
          const middle = Math.floor((low + high) / 2);
          if (fits(middle)) low = middle;
          else high = middle;
        }
      }
      const count = Math.max(1, low);
      const chunk = points.slice(offset, offset + count).join('');
      offset += count;
      if (offset === points.length) return chunk;
      lines.push(chunk);
    }
    return '';
  };

  for (const word of words) {
    const candidate = line ? `${line} ${word}` : word;
    if (word.length <= 128 && measure(candidate) <= maxWidth) {
      line = candidate;
      continue;
    }
    if (line) {
      lines.push(line);
      line = '';
    }
    line = pushLongWord(word);
  }
  if (line) lines.push(line);
  return lines;
}

/** The compact banner names the current page, or the Mac's live state word
 * when it is not resting ("hearing you…", "reconnecting"). The native
 * conversation page carries its own in-card indicator, but it only opens
 * once speech-to-text has finished; this word is the sole lens-visible
 * acknowledgement during that window, and the Mac sends it first precisely
 * so an unchanged HUD never reads as "the assistant is dead". The box sits
 * inside the top-left tile, so a state change dirties one tile, not two. */
export function hudSpineLabel(frame: HudVisualFrame): string {
  const status = frame.statusModel ?? statusModelFromText(frame.status, frame.micOn);
  return hudStatusLabel(status, hudSpinePageTitle(frame) || undefined);
}

export function hudSpinePageTitle(frame: HudVisualFrame): string {
  switch (frame.kind) {
    case 'list':
      return hudDisplayTitle(frame.title);
    case 'navigation':
      return 'Navigation';
    case 'reader':
      return hudDisplayTitle(frame.title);
    default:
      return '';
  }
}

/**
 * The guidance pane: the right half of the canvas, from just under the
 * status spine down to the bottom band, full-bleed. Left of it belongs to
 * the maneuver the wearer must act on. Since 2026-11-23 it holds the route
 * schematic (NavigationSchematic.ts) — never a raster street tile.
 */
export const NAVIGATION_MAP_PANEL = {
  x: 292,
  y: 28,
  width: 284,
  height: 220,
  padding: 0,
} as const;

export class HudCanvasRenderer implements HudFrameRenderer {
  readonly drawsReaders = true;
  private readonly icons: Promise<Record<HudIconName, HTMLImageElement>>;

  constructor() {
    this.icons = Promise.all(
      (Object.entries(ICON_SOURCES) as Array<[HudIconName, string]>)
        .map(async ([name, source]) => [name, await loadSvg(source)] as const),
    ).then((entries) => Object.fromEntries(entries) as Record<HudIconName, HTMLImageElement>);
  }

  async render(frame: HudVisualFrame): Promise<readonly HudImageTile[]> {
    // Normalize only gesture guidance, never the wearer's content.
    if ('hint' in frame) frame = { ...frame, hint: GesturePolicy.formatHint(frame.hint) };
    if (frame.kind === 'navigation') {
      frame = { ...frame, navigation: { ...frame.navigation,
        hint: GesturePolicy.formatHint(frame.navigation.hint) } };
    }
    const icons = await this.icons;
    const canvas = document.createElement('canvas');
    canvas.width = CANVAS.width;
    canvas.height = CANVAS.height;
    const context = canvas.getContext('2d', { alpha: false });
    if (!context) throw new Error('2D canvas is unavailable');

    context.fillStyle = '#000000';
    context.fillRect(0, 0, CANVAS.width, CANVAS.height);
    context.imageSmoothingEnabled = true;

    switch (frame.kind) {
      case 'overview':
        this.drawOverview(context, frame, icons);
        break;
      case 'list':
        // Every list is a firmware list (CapabilityMenu): a list frame draws
        // only the shell.
        break;
      case 'navigation':
        this.drawNavigation(context, frame.navigation);
        break;
      case 'reader': {
        const atlas = await loadEvenFont();
        for (const run of readerLayout(frame.lines, frame.hint, frame.clipBottom)) {
          drawEvenText(context, atlas, run.text, run.x, run.y, { alpha: run.alpha, fallbackFont: TYPE.body });
        }
        break;
      }
    }
    // Chrome is the final compositing layer. Besides making ownership
    // explicit, this prevents state-specific paths from ever obscuring the
    // clock/privacy/battery spine on host canvas implementations.
    this.drawShell(
      context,
      frame.statusModel ?? statusModelFromText(frame.status, frame.micOn),
      icons,
      usesFlatHudShell(frame.kind),
      frame.kind !== 'overview',
      hudSpineLabel(frame),
      await loadEvenFont(),
    );
    if (frame.actionOverlay) {
      this.drawActionPill(context, frame.actionOverlay.productivity, icons);
    }
    // The assistant is not a page. It is painted last into the same four G2
    // image tiles, so it can speak over any feature without allocating an
    // impossible fifth image container or replacing the feature underneath.
    if (frame.assistantOverlay) {
      this.drawAssistantOverlay(context, frame.assistantOverlay);
    }

    const rgba = context.getImageData(0, 0, CANVAS.width, CANVAS.height).data;
    const pixels = new Uint8Array(CANVAS.width * CANVAS.height);
    for (let source = 0, target = 0; source < rgba.length; source += 4, target += 1) {
      pixels[target] = Math.max(rgba[source] ?? 0, rgba[source + 1] ?? 0, rgba[source + 2] ?? 0);
    }
    return splitGrayscaleTiles(pixels);
  }

  /** Home's left column as its two images (HomeImages.ts). */
  async renderHomeColumn(view: HomeColumnView): Promise<readonly HudImageTile[]> {
    const icons = await this.icons;
    const atlas = await loadEvenFont();
    const width = HOME_COLUMN.width;
    const height = HOME_COLUMN.tileHeight;
    const marks = homeColumnLayout(view);
    return [HomeColumnId.Top, HomeColumnId.Bottom].map((containerId, tile) => {
      const canvas = document.createElement('canvas');
      canvas.width = width;
      canvas.height = height;
      const context = canvas.getContext('2d', { alpha: false });
      if (!context) throw new Error('2D canvas is unavailable');
      context.fillStyle = '#000000';
      context.fillRect(0, 0, width, height);
      context.imageSmoothingEnabled = true;
      for (const mark of marks.filter((item) => item.tile === tile)) {
        if (mark.bar) {
          context.fillStyle = gray(mark.alpha * 255);
          context.fillRect(mark.x, mark.y, mark.width, mark.height);
        } else if (mark.icon) this.drawIcon(context, icons[mark.icon], mark.x, mark.y, mark.width, Math.round(mark.alpha * 255));
        else if (mark.text) {
          drawEvenText(context, atlas, mark.text, mark.x, mark.y, { alpha: mark.alpha, fallbackFont: TYPE.statusMeta });
        }
      }
      const rgba = context.getImageData(0, 0, width, height).data;
      const pixels = new Uint8Array(width * height);
      for (let source = 0, target = 0; source < rgba.length; source += 4, target += 1) {
        pixels[target] = Math.max(rgba[source] ?? 0, rgba[source + 1] ?? 0, rgba[source + 2] ?? 0);
      }
      return { containerId, pixels, width, height };
    });
  }

  /** The Reminders page's next-up card as its one image (ReminderCard.ts). */
  async renderReminderCard(view: ReminderCardView): Promise<readonly HudImageTile[]> {
    const atlas = await loadEvenFont();
    const width = REMINDER_CARD.width;
    const height = REMINDER_CARD.height;
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext('2d', { alpha: false });
    if (!context) throw new Error('2D canvas is unavailable');
    context.fillStyle = '#000000';
    context.fillRect(0, 0, width, height);
    context.imageSmoothingEnabled = true;
    for (const mark of reminderCardLayout(view)) {
      if (mark.frame) {
        context.strokeStyle = gray(mark.alpha * 255);
        context.lineWidth = 2;
        roundedRect(context, mark.x + 1, mark.y + 1, mark.width - 2, mark.height - 2, 10);
        context.stroke();
      } else if (mark.text) {
        drawEvenText(context, atlas, mark.text, mark.x, mark.y, {
          alpha: mark.alpha, fallbackFont: TYPE.statusMeta,
          ...(mark.scale === undefined ? {} : { scale: mark.scale }),
        });
      }
    }
    const rgba = context.getImageData(0, 0, width, height).data;
    const pixels = new Uint8Array(width * height);
    for (let source = 0, target = 0; source < rgba.length; source += 4, target += 1) {
      pixels[target] = Math.max(rgba[source] ?? 0, rgba[source + 1] ?? 0, rgba[source + 2] ?? 0);
    }
    return [{ containerId: ReminderCardId, pixels, width, height }];
  }

  /** The spine alone, as the two 288x24 halves of a firmware page's image
   * header (HeaderImage.ts): the same drawing as the image pages' spine. */
  async renderHeader(model: HudStatusModel, label: string): Promise<readonly HudImageTile[]> {
    const icons = await this.icons;
    const width = CANVAS.width;
    const height = HUD_VISUAL_GEOMETRY.spineHeight;
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext('2d', { alpha: false });
    if (!context) throw new Error('2D canvas is unavailable');
    context.fillStyle = '#000000';
    context.fillRect(0, 0, width, height);
    context.imageSmoothingEnabled = true;
    this.drawShell(context, model, icons, true, true, label, await loadEvenFont());
    const rgba = context.getImageData(0, 0, width, height).data;
    const half = width / 2;
    return [HeaderImageId.Left, HeaderImageId.Right].map((containerId, index) => {
      const pixels = new Uint8Array(half * height);
      for (let row = 0; row < height; row += 1) {
        for (let column = 0; column < half; column += 1) {
          const source = (row * width + index * half + column) * 4;
          pixels[row * half + column] = Math.max(rgba[source] ?? 0, rgba[source + 1] ?? 0, rgba[source + 2] ?? 0);
        }
      }
      return { containerId, pixels, width: half, height };
    });
  }

  private drawShell(
    context: CanvasRenderingContext2D,
    status: HudStatusModel,
    icons: Record<HudIconName, HTMLImageElement>,
    flat: boolean,
    /** The dashboard prints the time at full size; a designer never sets
     * the same fact twice on one screen, so the spine yields its clock —
     * and carries the DATE in that slot instead, where a banner normally
     * puts it, rather than stacking it on top of the big clock. */
    showTime = true,
    label = hudStatusLabel(status),
    /** Even's font (EvenFont.ts): the spine's words are set in it, the
     * mic and battery are the only icons (the wearer's ask, 2026-10-06). */
    atlas?: HTMLImageElement,
  ): void {
    if (!flat) {
      context.strokeStyle = gray(HAIRLINE);
      context.lineWidth = 1;
      roundedRect(context, 1.5, 1.5, 573, 285, 20);
      context.stroke();
    }

    // One 24 px band. Left tile: the clock and the page's word. Right tile:
    // the microphone and the battery as icons alone — the words ON/OFF and
    // the percentage are gone (wearer's ask, 2026-10-05: "simple icons and
    // smaller"); the cell's fill carries the level, the slash carries the
    // closed mic, and the number comes back only when the battery is low.
    const baseline = HUD_VISUAL_GEOMETRY.statusBaselineY;
    // The words sit centred on the band's middle line, in Even's font.
    const top = baseline - evenLineHeight() / 2;
    const words = (text: string, x: number) => this.drawSpineWords(context, atlas, text, x, top);
    context.textBaseline = 'middle';
    context.fillStyle = gray(SUPPORT);
    context.textAlign = 'left';
    words(showTime ? (status.clock || '--:--') : hudDateLabel(new Date()), 30);
    // The label box is measured (types.ts SPINE_LABEL_BOX) and ends on the
    // tile seam, so the changing word stays inside the top-left tile.
    words(fitOneLineEven(label, SPINE_LABEL_BOX.width), SPINE_LABEL_BOX.x);
    this.drawIcon(
      context,
      status.mic ? icons.microphone : icons.microphoneSlash,
      SPINE_ICONS.micX, SPINE_ICONS.micY, SPINE_ICONS.micSize,
      status.mic ? SUPPORT : QUIET,
    );
    if (status.battery !== undefined) {
      this.drawIcon(
        context, icons[batteryIconFor(status)],
        SPINE_ICONS.batteryX, SPINE_ICONS.batteryY, SPINE_ICONS.batterySize, SUPPORT,
      );
      if (!status.charging && status.battery <= HUD_LOW_BATTERY_PCT) {
        const level = `${Math.round(status.battery)}%`;
        words(level, SPINE_ICONS.lowBatteryTextRightX - evenTextWidth(level));
      }
    }

    // The other running features, right-aligned against the mic: the
    // Pomodoro's minutes on the map, the next turn on the dashboard.
    const chips = (status.activities ?? []).filter((chip) => chip.trim().length > 0);
    if (chips.length > 0) {
      const text = fitOneLineEven(chips.join(' · '), SPINE_ICONS.activityRightX - SPINE_ICONS.activityLeftX);
      words(text, SPINE_ICONS.activityRightX - evenTextWidth(text));
    }

    // No rule under the spine. It separated nothing that the gap below it
    // does not already separate, and it lit 540 pixels to do it (the
    // wearer's ruling: another waste).
  }

  /** One run of the spine's words in Even's font, its cell top at `top`. */
  private drawSpineWords(
    context: CanvasRenderingContext2D, atlas: HTMLImageElement | undefined, text: string, x: number, top: number,
  ): void {
    drawEvenText(context, atlas, text, x, top, { alpha: 1, fallbackFont: TYPE.statusMeta });
  }

  /** A receipt strip over an image page (the dashboard, the month, the live
   * map): two tiles, cheaper than a page swap and its four-tile return. An
   * answer is never pixels — it is Even's own text page
   * (HudPresenter.usesNativeAnswerPage), paged in place. */
  private drawAssistantOverlay(
    context: CanvasRenderingContext2D,
    overlay: AssistantOverlayVisual,
  ): void {
    this.drawAssistantPill(context, overlay.text);
  }

  private drawOverview(
    context: CanvasRenderingContext2D,
    frame: {
      status: string;
      micOn?: boolean;
      statusModel?: HudStatusModel;
      main: string;
      mainBrightness: number;
      mainKind?: HudContentKind;
      context: string;
      tray: string;
      hint: string;
      ambient?: { temperature: string; condition: string; unread: number };
      rail?: PanelRailVisual;
    },
    icons: Record<HudIconName, HTMLImageElement>,
  ): void {
    context.save();
    const overview = resolveHudOverview(frame);
    const layout = OVERVIEW_DASHBOARD;
    const status = frame.statusModel ?? statusModelFromText(frame.status, frame.micOn ?? false);

    // ── Left pane: the clock anchor ──────────────────────────────────────
    // Phone-clock truth, so this half stays alive and useful even when the
    // Mac is unreachable.
    context.textAlign = 'left';
    context.textBaseline = 'middle';
    // The left pane is one centred column — clock, then conditions — so it
    // reads as a single object instead of text stranded against the edge.
    const columnX = layout.clockCenterX;
    context.textAlign = 'center';

    // The date used to sit here, stacked directly on the clock. It reads as
    // banner chrome, not as a headline fact, so it moved to the spine's
    // left slot — which this screen leaves empty anyway, having yielded its
    // small clock to the big one below.
    const clock = hudClockParts(status.clock);
    context.fillStyle = gray(BRIGHT);
    context.font = mono(layout.clockFontSize);
    context.fillText(clock.hours, columnX, layout.hoursBaselineY);
    context.fillText(clock.minutes, columnX, layout.minutesBaselineY);
    context.textAlign = 'left';

    // Weather and unread count live under the clock. The Even Hub SDK has
    // no weather API (checked against the installed 0.0.14 typings), so the
    // Mac resolves it from the phone's own position and sends the words.
    const ambient = frame.ambient;
    if (ambient && (ambient.temperature || ambient.unread > 0)) {
      context.font = TYPE.body;
      const unread = ambient.unread > 0 ? String(ambient.unread) : '';
      // Measure the whole row first so it centres under the clock rather
      // than starting wherever the left margin happened to be.
      const temperatureWidth = ambient.temperature
        ? 20 + context.measureText(ambient.temperature).width : 0;
      const unreadWidth = unread ? 20 + context.measureText(unread).width : 0;
      const gap = temperatureWidth && unreadWidth ? 22 : 0;
      let ambientX = columnX - (temperatureWidth + gap + unreadWidth) / 2;
      if (ambient.temperature) {
        this.drawIcon(
          context, icons.weather, ambientX, layout.ambientBaselineY - 8, 15, CHROME,
        );
        context.fillStyle = gray(SUPPORT);
        context.fillText(ambient.temperature, ambientX + 20, layout.ambientBaselineY);
        ambientX += temperatureWidth + gap;
      }
      if (unread) {
        this.drawIcon(
          context, icons.notifications, ambientX, layout.ambientBaselineY - 8, 15, CHROME,
        );
        context.fillStyle = gray(SUPPORT);
        context.fillText(unread, ambientX + 20, layout.ambientBaselineY);
      }
    }

    // One hairline divides the two panes. Not a card: a single frame edge.
    context.strokeStyle = gray(HAIRLINE);
    context.lineWidth = 1;
    context.beginPath();
    context.moveTo(layout.dividerX, layout.dividerTopY);
    context.lineTo(layout.dividerX, layout.dividerBottomY);
    context.stroke();

    // ── Right pane: whatever JOAT currently has to say ───────────────────
    const paneX = layout.paneX;
    const paneWidth = layout.paneWidth;

    // The tab rail, and the tab it has selected. Notifications IS the
    // resting pane below; the other tabs draw their cached push instead.
    if (frame.rail) {
      this.drawTabRail(context, frame.rail.activeTab, icons);
      if (frame.rail.activeTab !== 'notifications') {
        this.drawPanelPane(context, frame.rail, icons);
        context.fillStyle = gray(QUIET);
        context.font = TYPE.hint;
        context.textAlign = 'center';
        context.fillText(PANEL_OVERVIEW_HINT, 288, 267);
        context.restore();
        return;
      }
    }

    if (overview.kind === 'empty') {
      context.fillStyle = gray(CHROME);
      context.font = TYPE.label;
      context.fillText(upper(overview.title), paneX, layout.labelBaselineY);
      context.fillStyle = gray(SUPPORT);
      context.font = TYPE.primary;
      this.drawWrappedText(
        context, overview.primary, paneX, layout.mainTopBaselineY,
        paneWidth, layout.mainLineHeight, 3, 'leading',
      );
      context.fillStyle = gray(CHROME);
      context.font = TYPE.meta;
      this.drawWrappedText(
        context, overview.recovery, paneX, layout.contextValueBaselineY,
        paneWidth, LEAD.meta, 2, 'leading',
      );
      context.fillStyle = gray(QUIET);
      context.font = TYPE.hint;
      context.textAlign = 'center';
      context.fillText(frame.rail ? PANEL_OVERVIEW_HINT : overview.hint, 288, 267);
      context.restore();
      return;
    }

    // The pane FLOWS: each block starts under the one above it. Pinning
    // these to fixed baselines left a hole in the middle whenever JOAT's
    // answer was short — the exact wasted space the whole redesign is
    // about. The tray still anchors to the bottom of the pane.
    let cursorY = layout.labelBaselineY;
    if (overview.main) {
      const main = overview.main;
      const mainLevel = hudBrightnessGray(frame.mainBrightness);
      context.fillStyle = gray(CHROME);
      context.font = TYPE.label;
      context.fillText(upper(main.label), paneX, cursorY);
      context.fillStyle = gray(mainLevel);
      if (main.structured) {
        // A calendar/reminder value is one short fact — give it size, but
        // not a whole band: what is BELOW it is content too. The pane is
        // narrower since the rail; a fact that no longer fits at 21px drops
        // to 18px before it is ever truncated ("Design review · 4…" is not
        // a time the wearer can act on).
        cursorY += 28;
        context.font = mono(21);
        if (context.measureText(main.value).width > paneWidth) {
          context.font = mono(18);
        }
        this.fitText(context, main.value, paneX, cursorY, paneWidth);
        cursorY += 10;
      } else {
        cursorY += 24;
        context.font = TYPE.primary;
        const drawn = this.drawWrappedText(
          context,
          main.value,
          paneX,
          cursorY,
          paneWidth,
          layout.mainLineHeight,
          layout.mainMaxLines,
          frame.mainKind === 'notification' ? 'leading' : 'tail',
        );
        cursorY += (drawn - 1) * layout.mainLineHeight + 6;
      }
    }

    if (overview.context) {
      cursorY += 22;
      context.fillStyle = gray(CHROME);
      context.font = TYPE.label;
      context.fillText(upper(overview.context.label), paneX, cursorY);
      cursorY += 16;
      context.fillStyle = gray(SUPPORT);
      context.font = TYPE.secondary;
      this.fitText(context, overview.context.value, paneX, cursorY, paneWidth);
    }

    if (overview.tray) {
      // Notifications are CONTENT on the home screen: source, age, and the
      // headline itself, stacked. No count, and no menu page to reach them.
      // As many notifications as the pane can actually hold. The count is
      // decided by the remaining pixels, not by a number picked in advance:
      // a short answer above should buy the wearer another card here.
      const rows = parseHudNotificationRows(overview.tray);
      let trayY = cursorY + 22;
      for (const row of rows) {
        if (trayY > OVERVIEW_TRAY_LAST_Y) break;
        if (row.source) {
          this.drawIcon(context, icons.notifications, paneX, trayY - 7, 12, CHROME);
          context.fillStyle = gray(CHROME);
          context.font = TYPE.micro;
          const label = row.age ? `${row.source} · ${row.age}` : row.source;
          this.fitText(context, label, paneX + 16, trayY, paneWidth - 16);
          trayY += 14;
        }
        context.fillStyle = gray(SUPPORT);
        context.font = TYPE.body;
        this.fitText(context, row.headline, paneX + 16, trayY, paneWidth - 16);
        trayY += 20;
      }
    }

    context.fillStyle = gray(QUIET);
    context.font = TYPE.hint;
    context.textAlign = 'center';
    // One footer for every resting tab. The hint is centred on the tile
    // seam, so a footer that differed between the notifications tab and
    // the other three made a swipe repaint the bottom-left tile as well —
    // three image writes where the rail and the pane need two.
    context.fillText(
      frame.mainKind === 'notification'
        ? NOTIFICATION_DISMISS_HINT
        : frame.rail ? PANEL_OVERVIEW_HINT : frame.hint,
      288,
      267,
    );
    context.restore();
  }

  /**
   * The tab rail. On the overview it stands between the clock and the
   * pane's hairline, and the active tab reaches the hairline with a short
   * bracket — the platform dashboard's own tab, drawn in JOAT's language.
   * Expanded, it moves to the left margin so the wearer still sees where
   * they are, with the same bright leading edge every selection uses.
   */
  private drawTabRail(
    context: CanvasRenderingContext2D,
    // Search's own page (from the menu) lights no rail tab.
    activeTab: PanelTab | PanelContentTab,
    icons: Record<HudIconName, HTMLImageElement>,
  ): void {
    const rail = PANEL_RAIL;
    const x = rail.x;
    const iconFor: Record<PanelTab, HTMLImageElement> = {
      notifications: icons.notifications,
      planner: icons.planner,
      news: icons.news,
      pomodoro: icons.pomodoro,
      quicknotes: icons.quicknotes,
    };
    PANEL_TABS.forEach((tab, index) => {
      const top = rail.slotY[index] ?? rail.slotY[0];
      const active = tab === activeTab;
      this.drawIcon(context, iconFor[tab], x, top, rail.iconSize, active ? BRIGHT : CHROME);
      if (!active) return;
      const centerY = top + rail.iconSize / 2;
      context.fillStyle = gray(BRIGHT);
      // The bracket: the lit tab attaches to the pane it opens.
      context.fillRect(x + rail.iconSize + 2, Math.round(centerY) - 1, 6, 2);
    });
  }

  /** The active tab's resting pane: what the Mac last pushed, at a glance.
   * Three headlines, one timer, or the last lookup — never a count. */
  private drawPanelPane(
    context: CanvasRenderingContext2D,
    rail: PanelRailVisual,
    icons: Record<HudIconName, HTMLImageElement>,
  ): void {
    const layout = OVERVIEW_DASHBOARD;
    const paneX = layout.paneX;
    const paneWidth = layout.paneWidth;
    const content = rail.content;
    context.textAlign = 'left';
    context.textBaseline = 'middle';

    const label = (text: string): void => {
      context.fillStyle = gray(CHROME);
      context.font = TYPE.label;
      this.fitText(context, text, paneX, layout.labelBaselineY, paneWidth);
    };
    const quiet = (lines: string[], startY: number): void => {
      context.fillStyle = gray(SUPPORT);
      context.font = TYPE.body;
      lines.forEach((line, index) => {
        this.drawWrappedText(context, line, paneX, startY + index * 36, paneWidth, LEAD.tight, 2, 'leading');
      });
    };

    if (rail.activeTab === 'pomodoro') {
      label('POMODORO');
      if (content?.empty) {
        quiet([content.headline ?? 'Timer unavailable', content.empty], layout.mainTopBaselineY);
        return;
      }
      const pomodoro = content?.pomodoro;
      if (!pomodoro) {
        quiet(['Focus timer · open the tab to start'], layout.mainTopBaselineY);
        return;
      }
      const now = Date.now();
      if (pomodoro.phase === 'idle') {
        context.fillStyle = gray(BRIGHT);
        context.font = TYPE.primary;
        context.fillText('Ready', paneX, layout.mainTopBaselineY + 4);
        quiet([`${Math.round(pomodoro.lengthMs / 60_000)} min focus · tap to start`], layout.mainTopBaselineY + 34);
        return;
      }
      const remaining = pomodoroRemainingMs(pomodoro, now);
      const minutes = formatMinutesLeft(remaining);
      context.fillStyle = gray(BRIGHT);
      context.font = mono(48);
      const number = minutes.split(' ')[0] ?? '0';
      context.fillText(number, paneX, layout.mainTopBaselineY + 22);
      const numberWidth = context.measureText(number).width;
      context.fillStyle = gray(SUPPORT);
      context.font = TYPE.meta;
      context.fillText('min left', paneX + numberWidth + 8, layout.mainTopBaselineY + 30);
      context.fillStyle = gray(pomodoro.running ? SUPPORT : CHROME);
      context.font = TYPE.label;
      this.fitText(context, pomodoroBlockLabel(pomodoro), paneX, layout.mainTopBaselineY + 62, paneWidth);
      context.fillStyle = gray(CHROME);
      context.font = TYPE.secondary;
      if (pomodoro.phase !== 'timer') {
        context.fillText(pomodoroCycleDots(pomodoro.completed, pomodoro.blocksPerSet), paneX, layout.mainTopBaselineY + 84);
      }
      return;
    }

    if (rail.activeTab === 'search' || rail.activeTab === 'quicknotes' || rail.activeTab === 'planner') {
      const planner = rail.activeTab === 'planner';
      const notes = rail.activeTab === 'quicknotes';
      label(planner ? 'REMINDERS' : notes ? 'QUICKNOTES' : 'SEARCH');
      if (!content || (!content.headline && content.rows.length === 0)) {
        quiet([
          content?.empty
            ?? (planner ? 'Waiting for JOAT on your Mac…' : notes ? 'No notes yet.' : 'Say “JOAT, look up …”'),
        ], layout.mainTopBaselineY);
        return;
      }
      let y = layout.mainTopBaselineY;
      if (content.headline) {
        context.fillStyle = gray(BRIGHT);
        context.font = TYPE.body;
        y += (this.drawWrappedText(context, content.headline, paneX, y, paneWidth, LEAD.tight, 2, 'leading') - 1) * LEAD.tight + 24;
      }
      for (const row of content.rows.slice(0, 3)) {
        if (y > OVERVIEW_TRAY_LAST_Y - 14) break;
        const parts = parseHudListItem(row);
        context.fillStyle = gray(SUPPORT);
        context.font = TYPE.body;
        // A reminder's "[Late]" / "[09:00]" is the point of the row.
        const shown = parts.primary || row;
        const lines = this.drawWrappedText(
          context, planner && parts.age ? `${parts.age} · ${shown}` : shown,
          paneX, y, paneWidth, LEAD.tight, 2, 'leading',
        );
        y += lines * 16;
        if (parts.secondary) {
          context.fillStyle = gray(CHROME);
          context.font = TYPE.micro;
          this.fitText(context, parts.secondary, paneX, y - 2, paneWidth);
          y += 12;
        }
        y += 6;
      }
      return;
    }

    // News: the headlines themselves, newest first, as many as fit.
    label(content ? `NEWS · ${freshnessLabel(content.receivedAtMs, Date.now())}` : 'NEWS');
    if (!content) {
      quiet(['Waiting for JOAT on your Mac…'], layout.mainTopBaselineY);
      return;
    }
    if (content.rows.length === 0) {
      quiet([content.empty ?? 'No headlines yet.', 'Tap to open, then hold to refresh.'], layout.mainTopBaselineY);
      return;
    }
    let y = layout.labelBaselineY + 22;
    for (const row of content.rows) {
      if (y > OVERVIEW_TRAY_LAST_Y - 16) break;
      const parts = parseHudListItem(row);
      this.drawIcon(context, icons.news, paneX, y - 7, 12, CHROME);
      context.fillStyle = gray(CHROME);
      context.font = TYPE.micro;
      const source = [parts.secondary, parts.age].filter(Boolean).join(' · ');
      this.fitText(context, source || 'News', paneX + 16, y, paneWidth - 16);
      y += 14;
      context.fillStyle = gray(SUPPORT);
      context.font = TYPE.body;
      const lines = this.drawWrappedText(
        context, parts.primary || row, paneX + 16, y, paneWidth - 16, LEAD.tight, 2, 'leading',
      );
      y += lines * LEAD.tight + 10;
    }
  }

  /** Strip over the live page; black-fills its own rect so rows beneath can't bleed through. */
  private drawActionPill(
    context: CanvasRenderingContext2D,
    model: ProductivityHudModel,
    icons: Record<HudIconName, HTMLImageElement>,
  ): void {
    const isCalendar = model.domain === 'calendar';
    const icon = icons[isCalendar ? 'calendar' : 'tasks'];
    const headline = `${isCalendar ? 'Calendar' : 'Reminders'} · ${
      productivityPhaseLabel(model.phase)}`;

    context.fillStyle = gray(0);
    roundedRect(
      context,
      ACTION_PILL.x,
      ACTION_PILL.y,
      ACTION_PILL.width,
      ACTION_PILL.height,
      ACTION_PILL.radius,
    );
    context.fill();
    context.strokeStyle = gray(SUPPORT);
    context.lineWidth = 1;
    context.stroke();

    context.textBaseline = 'middle';
    context.textAlign = 'left';
    this.drawIcon(
      context, icon, ACTION_PILL.iconX, ACTION_PILL.iconY, ACTION_PILL.iconSize, BRIGHT,
    );
    context.fillStyle = gray(BRIGHT);
    context.font = TYPE.body;
    this.fitText(
      context,
      headline,
      ACTION_PILL.headlineX,
      ACTION_PILL.headlineY,
      ACTION_PILL.x + ACTION_PILL.width - ACTION_PILL.headlineX - 16,
    );
    context.fillStyle = gray(CHROME);
    context.font = TYPE.meta;
    this.fitText(
      context,
      model.title,
      ACTION_PILL.headlineX,
      ACTION_PILL.detailY,
      ACTION_PILL.x + ACTION_PILL.width - ACTION_PILL.headlineX - 16,
    );
  }

  /** A short spoken receipt ("Reply sent.") over the live page — the same
   * strip shape as an outcome pill, so every transient acknowledgement in the
   * product reads the same way regardless of which subsystem produced it. */
  private drawAssistantPill(context: CanvasRenderingContext2D, text: string): void {
    context.save();
    context.fillStyle = gray(0);
    roundedRect(
      context,
      ACTION_PILL.x,
      ACTION_PILL.y,
      ACTION_PILL.width,
      ACTION_PILL.height,
      ACTION_PILL.radius,
    );
    context.fill();
    context.strokeStyle = gray(SUPPORT);
    context.lineWidth = 1;
    context.stroke();

    context.textBaseline = 'middle';
    context.textAlign = 'center';
    context.fillStyle = gray(BRIGHT);
    context.font = TYPE.body;
    this.fitText(
      context,
      text.trim(),
      ACTION_PILL.x + ACTION_PILL.width / 2,
      ACTION_PILL.y + ACTION_PILL.height / 2,
      ACTION_PILL.width - 32,
    );
    context.restore();
  }

  /** The one navigation frame that is pixels: live guidance with its map.
   * Every frame before and around it (confirm the place, the mode, the
   * preview, the warnings, arrival, an error) is a firmware text page
   * (NavigationHud.navigationNativeBody). */
  private drawNavigation(
    context: CanvasRenderingContext2D,
    navigation: NavigationHudModel,
  ): void {
    this.drawActiveNavigation(context, navigation);
  }

  private drawActiveNavigation(
    context: CanvasRenderingContext2D,
    navigation: NavigationHudModel,
  ): void {
    // Four bands, as the reference HUD lays them out: the status spine, two
    // panes side by side, and one fact line along the bottom. The right pane
    // is the route schematic; only a route with no geometry at all gives the
    // instruction the whole width. A stray raster `map.panel` is ignored.
    const projection = navigation.map
      ? buildRouteSchematic(navigation.map, NAVIGATION_MAP_PANEL, navigation.mode ?? 'walking')
      : undefined;
    const actWidth = projection ? 236 : 504;

    context.textBaseline = 'middle';
    context.textAlign = 'left';
    context.fillStyle = gray(CHROME);
    context.font = TYPE.label;
    const activeHeading = navigation.mode === 'driving'
      ? 'DRIVING'
      : navigation.mode === 'cycling'
        ? 'CYCLING'
        : 'WALKING';
    context.fillText(`${activeHeading} · ACTIVE`, 36, 44);

    this.drawManeuver(context, navigation.maneuver ?? 'continue', 60, 90, BRIGHT);
    context.fillStyle = gray(BRIGHT);
    context.font = mono(20);
    context.fillText(formatNavigationDistance(navigation.maneuverDistanceM ?? 0), 98, 90);

    context.fillStyle = gray(BRIGHT);
    context.font = TYPE.guide;
    this.fitText(context, navigation.primary, 36, 134, actWidth);
    context.fillStyle = gray(SUPPORT);
    context.font = TYPE.body;
    this.drawWrappedText(context, navigation.secondary, 36, 162, actWidth, LEAD.body, 2);

    context.fillStyle = gray(CHROME);
    context.font = TYPE.meta;
    this.fitText(context, navigation.hint, 36, 198, actWidth);

    if (projection) this.drawNavigationMap(context, projection);

    // Keep live facts with the maneuver when a map owns the right pane. A
    // full-width changing footer dirties the bottom-right image tile on every
    // distance update, forcing one unrelated serial BLE upload before the
    // next usable frame can finish.
    const liveBandWidth = projection ? actWidth : 504;
    this.drawNavigationProgress(context, navigation.progress ?? 0, liveBandWidth);
    const mode = navigation.mode === 'driving'
      ? 'Driving'
      : navigation.mode === 'cycling'
        ? 'Cycling'
        : 'Walking';
    const arrival = this.arrivalClock(navigation.etaMinutes);
    const compactFacts = `${formatNavigationDistance(navigation.remainingDistanceM ?? 0)} left · ${navigation.etaMinutes ?? 0} min`;
    const facts = projection ? compactFacts : `${mode} · ${compactFacts}`;
    this.drawNavigationFooter(
      context,
      arrival ? `${facts} · ${arrival}` : facts,
      liveBandWidth,
    );
  }

  /**
   * The route schematic, heading-up: one thick bright line for the road
   * still ahead, the travelled part as a dim thread, the next maneuver as a
   * ringed node with faint cross-street stubs and its street name, and a
   * chevron for "you" at the fixed lower-centre anchor, always pointing up
   * the way the wearer is going. Nothing else: no tiles, no buildings, no
   * frame — on a transparent lens black is the real street showing through.
   */
  private drawNavigationMap(
    context: CanvasRenderingContext2D,
    schematic: RouteSchematic,
  ): void {
    const pane = NAVIGATION_MAP_PANEL;
    context.save();
    context.beginPath();
    context.rect(pane.x, pane.y, pane.width, pane.height);
    context.clip();
    context.lineJoin = 'round';
    context.lineCap = 'round';

    const trace = (points: readonly ScreenPoint[]): void => {
      const [first, ...rest] = points;
      if (!first) return;
      context.beginPath();
      context.moveTo(first[0], first[1]);
      for (const [x, y] of rest) context.lineTo(x, y);
      context.stroke();
    };

    // Behind the wearer: present, but quiet.
    context.strokeStyle = gray(QUIET);
    context.lineWidth = 2;
    trace(schematic.travelled);

    const maneuver = schematic.maneuver;
    if (maneuver) {
      context.strokeStyle = gray(CHROME);
      context.lineWidth = 3;
      for (const stub of maneuver.stubs) trace(stub);
    }

    if (schematic.offRoute && schematic.nearestOnRoute) {
      // Off the line: a faint dashed thread back to it.
      context.save();
      context.setLineDash?.([4, 5]);
      context.strokeStyle = gray(CHROME);
      context.lineWidth = 2;
      trace([schematic.you, schematic.nearestOnRoute]);
      context.restore();
    }

    // The road ahead: the one bright, wide thing in the pane.
    context.strokeStyle = gray(BRIGHT);
    context.lineWidth = 8;
    trace(schematic.remaining);

    if (schematic.destination) {
      const [x, y] = schematic.destination;
      context.fillStyle = gray(BRIGHT);
      context.fillRect(x - 6, y - 6, 12, 12);
      context.fillStyle = '#000000';
      context.fillRect(x - 2, y - 2, 4, 4);
    }

    if (maneuver) {
      const [x, y] = maneuver.point;
      // A ringed node: a black gap around a bright core, so the corner is a
      // point the eye lands on rather than a bend in the line.
      context.fillStyle = '#000000';
      context.beginPath();
      context.arc(x, y, 8, 0, Math.PI * 2);
      context.fill();
      context.strokeStyle = gray(BRIGHT);
      context.lineWidth = 2.5;
      context.beginPath();
      context.arc(x, y, 7, 0, Math.PI * 2);
      context.stroke();
      context.fillStyle = gray(BRIGHT);
      context.beginPath();
      context.arc(x, y, 3, 0, Math.PI * 2);
      context.fill();
      if (maneuver.label) this.drawSchematicLabel(context, maneuver.label, maneuver.point, maneuver.exitSide);
    }

    this.drawYouChevron(context, schematic.you);
    context.restore();
  }

  /** The maneuver's street name, beside the node on the side the route does
   * NOT leave by, on a black plate so the line never runs through letters. */
  private drawSchematicLabel(
    context: CanvasRenderingContext2D,
    label: string,
    point: ScreenPoint,
    exitSide: 'left' | 'right' | 'ahead',
  ): void {
    const pane = NAVIGATION_MAP_PANEL;
    context.font = TYPE.meta;
    context.textBaseline = 'middle';
    context.textAlign = 'left';
    const maxWidth = pane.width - 16;
    const width = Math.min(maxWidth, context.measureText(label).width);
    let x = exitSide === 'right' ? point[0] - 14 - width : point[0] + 14;
    if (exitSide === 'ahead') x = point[0] - width / 2;
    x = Math.max(pane.x + 6, Math.min(pane.x + pane.width - 6 - width, x));
    let y = point[1] - 16;
    y = Math.max(pane.y + 10, Math.min(pane.y + pane.height - 10, y));
    context.fillStyle = '#000000';
    context.fillRect(x - 3, y - 9, width + 6, 18);
    context.fillStyle = gray(BRIGHT);
    this.fitText(context, label, x, y, maxWidth);
  }

  /** "You": a solid chevron pointing up the pane, outlined in black so it
   * stays a distinct shape where it sits on the bright route line. */
  private drawYouChevron(context: CanvasRenderingContext2D, at: ScreenPoint): void {
    const [x, y] = at;
    context.beginPath();
    context.moveTo(x, y - 14);
    context.lineTo(x + 10, y + 9);
    context.lineTo(x, y + 4);
    context.lineTo(x - 10, y + 9);
    context.closePath();
    context.strokeStyle = '#000000';
    context.lineWidth = 5;
    context.lineJoin = 'round';
    context.stroke();
    context.fillStyle = gray(BRIGHT);
    context.fill();
  }

  /** "arrive 9:54" from a minutes-remaining figure, or '' when there isn't one.
   *
   * A duration answers "how much longer"; a CLOCK TIME answers "do I make
   * the meeting", which is the question someone walking somewhere is
   * actually asking, and it is the fact the reference drive HUD leads with
   * that JOAT's fact line was missing. This is a presentation of
   * `etaMinutes` — the renderer derives a wall-clock face from a number the
   * Mac already sent; it does not invent a fact of its own. */
  private arrivalClock(etaMinutes: number | undefined): string {
    if (etaMinutes === undefined || !Number.isFinite(etaMinutes)) return '';
    const arrival = new Date(Date.now() + etaMinutes * 60_000);
    const hours = arrival.getHours() % 12 || 12;
    return `arrive ${hours}:${String(arrival.getMinutes()).padStart(2, '0')}`;
  }

  private drawNavigationFooter(
    context: CanvasRenderingContext2D,
    copy: string,
    width = 504,
  ): void {
    // Left-aligned on the bottom band, not centred: it is a row of facts,
    // and facts line up with the content above them.
    context.fillStyle = gray(SUPPORT);
    context.font = TYPE.meta;
    context.textAlign = 'left';
    context.textBaseline = 'middle';
    this.fitText(context, copy, 36, HINT_BASELINE_Y, width);
  }

  private drawNavigationProgress(
    context: CanvasRenderingContext2D,
    progress: number,
    width = 504,
  ): void {
    const normalized = Math.max(0, Math.min(1, progress));
    // Directly above the fact line it measures.
    context.fillStyle = gray(HAIRLINE);
    context.fillRect(36, 253, width, 3);
    context.fillStyle = gray(SUPPORT);
    context.fillRect(36, 253, Math.round(width * normalized), 3);
  }

  private drawManeuver(
    context: CanvasRenderingContext2D,
    maneuver: NavigationManeuver,
    x: number,
    y: number,
    level: number,
  ): void {
    context.save();
    context.translate(x, y);
    if (maneuver === 'turn_left' || maneuver === 'slight_left') context.scale(-1, 1);
    context.strokeStyle = gray(level);
    context.fillStyle = gray(level);
    context.lineWidth = 5;
    context.lineCap = 'round';
    context.lineJoin = 'round';
    context.beginPath();
    if (maneuver === 'turn_left' || maneuver === 'turn_right') {
      context.moveTo(0, 25);
      context.lineTo(0, -8);
      context.quadraticCurveTo(0, -21, 13, -21);
      context.lineTo(23, -21);
      context.stroke();
      context.beginPath();
      context.moveTo(18, -30);
      context.lineTo(29, -21);
      context.lineTo(18, -12);
      context.fill();
    } else if (maneuver === 'u_turn') {
      context.moveTo(16, 24);
      context.lineTo(16, -7);
      context.quadraticCurveTo(16, -25, 0, -25);
      context.quadraticCurveTo(-16, -25, -16, -7);
      context.stroke();
      context.beginPath();
      context.moveTo(-25, -12);
      context.lineTo(-16, -1);
      context.lineTo(-7, -12);
      context.fill();
    } else {
      context.moveTo(0, 27);
      context.lineTo(0, -24);
      context.stroke();
      context.beginPath();
      context.moveTo(-9, -14);
      context.lineTo(0, -28);
      context.lineTo(9, -14);
      context.fill();
    }
    context.restore();
  }

  private drawIcon(
    context: CanvasRenderingContext2D,
    icon: HTMLImageElement,
    x: number,
    y: number,
    size: number,
    level: number,
  ): void {
    context.save();
    context.globalAlpha *= level / 255;
    context.drawImage(icon, x, y, size, size);
    context.restore();
  }

  private fitText(
    context: CanvasRenderingContext2D,
    text: string,
    x: number,
    y: number,
    maxWidth: number,
  ): void {
    let fitted = text;
    while (fitted.length > 1 && context.measureText(fitted).width > maxWidth) {
      fitted = fitted.slice(0, -1);
    }
    if (fitted !== text) fitted = `${fitted.slice(0, -1)}…`;
    context.fillText(fitted, x, y);
  }

  private drawWrappedText(
    context: CanvasRenderingContext2D,
    text: string,
    x: number,
    y: number,
    maxWidth: number,
    lineHeight: number,
    maximumLines: number,
    retention: 'tail' | 'leading' = 'tail',
  ): number {
    const lines = wrapHudText(text, maxWidth, (value) => context.measureText(value).width);
    const count = Math.max(1, maximumLines);
    const visible = retention === 'leading' ? lines.slice(0, count) : lines.slice(-count);
    // The cut is marked where it happened: an ellipsis after the last kept
    // line, or before the first when the oldest lines scrolled away.
    if (lines.length > count && visible.length > 0) {
      if (retention === 'leading') visible[visible.length - 1] = `${visible[visible.length - 1]}…`;
      else visible[0] = `…${visible[0]}`;
    }
    visible.forEach((value, index) => {
      this.fitText(context, value, x, y + index * lineHeight, maxWidth);
    });
    // The count lets a flowing pane place whatever comes next directly
    // under the real last line instead of guessing a fixed baseline.
    return visible.length;
  }

}
