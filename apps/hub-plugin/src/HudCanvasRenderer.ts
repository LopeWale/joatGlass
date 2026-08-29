import chatCircleSvg from '@phosphor-icons/core/regular/chat-circle-dots.svg?raw';
import bellSvg from '@phosphor-icons/core/regular/bell.svg?raw';
import calendarSvg from '@phosphor-icons/core/regular/calendar-dots.svg?raw';
import scalesSvg from '@phosphor-icons/core/regular/scales.svg?raw';
import gearSvg from '@phosphor-icons/core/regular/gear.svg?raw';
import taskSvg from '@phosphor-icons/core/regular/check-square-offset.svg?raw';
import moreSvg from '@phosphor-icons/core/regular/dots-three-circle.svg?raw';
import quickNoteSvg from '@phosphor-icons/core/regular/note-pencil.svg?raw';
import activitySvg from '@phosphor-icons/core/regular/pulse.svg?raw';
import navigationSvg from '@phosphor-icons/core/regular/navigation-arrow.svg?raw';
import microphoneSvg from '@phosphor-icons/core/regular/microphone.svg?raw';
import batteryHighSvg from '@phosphor-icons/core/regular/battery-high.svg?raw';
import {
  CANVAS,
  type BrainstormHudModel,
  type HudContentKind,
  type PlanningGridCell,
  type ProductivityHudModel,
} from './types';
import {
  splitGray4Tiles,
  type HudFrameRenderer,
  type HudImageTile,
  type HudVisualFrame,
} from './HudVisualSurface';
import {
  formatNavigationDistance,
  type NavigationHudModel,
  type NavigationManeuver,
} from './NavigationHud';

const BRIGHT = 238;
const SUPPORT = 170;
const CHROME = 102;
const QUIET = 68;
const HAIRLINE = 51;
const HUD_FONT = '"SF Mono", SFMono-Regular, Menlo, monospace';

/** Measured from the selected 576×288 reference panels. Keeping the major
 * anchors together makes visual drift obvious in review and in tests. */
export const HUD_VISUAL_GEOMETRY = {
  statusBaselineY: 20,
  statusDividerY: 35.5,
  overviewMainX: 176,
  overviewTrayY: 168,
  overviewContextY: 214,
  sidebarWidth: 160,
  sidebarBackdropOffsetX: 78,
  listContentX: 93,
  confirmationActionsY: 178,
  confirmationPrimaryX: 112,
  confirmationSecondaryX: 294,
  confirmationActionWidth: 170,
  confirmationActionHeight: 36,
  confirmationProgressX: 97,
  confirmationProgressWidth: 367,
} as const;

export type HudIconName =
  | 'messages'
  | 'notifications'
  | 'calendar'
  | 'tasks'
  | 'more'
  | 'quicknotes'
  | 'brainstorm'
  | 'activity'
  | 'navigation'
  | 'settings'
  | 'microphone'
  | 'battery';

const ICON_SOURCES: Readonly<Record<HudIconName, string>> = {
  messages: chatCircleSvg,
  notifications: bellSvg,
  calendar: calendarSvg,
  tasks: taskSvg,
  more: moreSvg,
  quicknotes: quickNoteSvg,
  brainstorm: scalesSvg,
  activity: activitySvg,
  navigation: navigationSvg,
  settings: gearSvg,
  microphone: microphoneSvg,
  battery: batteryHighSvg,
};

export type HudListKind =
  | 'messages'
  | 'notifications'
  | 'calendar'
  | 'tasks'
  | 'quicknotes'
  | 'activity'
  | 'generic';

/** A month is intentionally a real six-row grid on the lens, rather than a
 * web-calendar imitation. The Mac still owns the selected week/action row;
 * this parser only recognizes its established, source-backed title. */
export interface PlanningMonth {
  year: number;
  month: number;
  label: string;
}

const PLANNING_MONTHS = [
  'JANUARY', 'FEBRUARY', 'MARCH', 'APRIL', 'MAY', 'JUNE',
  'JULY', 'AUGUST', 'SEPTEMBER', 'OCTOBER', 'NOVEMBER', 'DECEMBER',
] as const;

/**
 * Parse only the native Planning month title. Compatibility aliases remain
 * renderable because an older installed package may still send CALENDAR or
 * TASKS while the Mac service converges them on the same plan.
 */
export function parsePlanningMonthTitle(title: string): PlanningMonth | undefined {
  const match = title.trim().toUpperCase().match(
    /^(?:PLANNING|CALENDAR|TASKS) · PLAN · ([A-Z]+) (\d{4})$/,
  );
  if (!match) return undefined;
  const label = match[1] ?? '';
  const year = Number(match[2]);
  const month = PLANNING_MONTHS.indexOf(label as (typeof PLANNING_MONTHS)[number]);
  // Date.UTC maps 0…99 to 1900…1999. EventKit can still render those dates
  // in a normal list, but a month grid must never silently map them to a
  // different century.
  if (month < 0 || !Number.isInteger(year) || year < 100 || year > 9999) return undefined;
  return { year, month, label };
}

/**
 * Keep the wearable footer aligned with every action the physical G2 router
 * accepts. This is presentation only: input ownership stays in EventRouter.
 */
export function brainstormActionHint(
  model: Pick<BrainstormHudModel, 'screen' | 'state' | 'terminalState'>,
): string {
  if (model.screen === 'brainstorm') {
    if (model.state === 'running') {
      return 'tap: Hermes · swipe down or left x2: cancel';
    }
    return model.state === 'ready'
      ? 'tap: speak question · left x2: menu'
      : 'left x2: menu · long-press: mic';
  }
  if (model.state === 'running') return 'tap: return to Brainstorm · left x2: menu';
  return model.terminalState === 'ready'
    ? 'tap: begin Brainstorm · left x2: menu'
    : 'tap: retry Hermes · left x2: menu';
}

export interface PlanningMonthDay {
  day: number;
  inMonth: boolean;
}

/** A fixed Monday-first 6 × 7 grid keeps the optical layout stable month to
 * month. UTC operations avoid a wearer time-zone or daylight-saving shift
 * changing the grid geometry in the renderer. */
export function planningMonthDays(month: PlanningMonth): readonly PlanningMonthDay[] {
  const first = planningMonthDateAt(month, 0);
  const mondayOffset = (first.getUTCDay() + 6) % 7;
  return Array.from({ length: 42 }, (_, offset) => {
    const value = planningMonthDateAt(month, offset - mondayOffset);
    return { day: value.getUTCDate(), inMonth: value.getUTCMonth() === month.month };
  });
}

function planningMonthDateAt(month: PlanningMonth, dayOffset: number): Date {
  return new Date(Date.UTC(month.year, month.month, 1 + dayOffset));
}

function isoUtcDay(value: Date): string {
  return [
    String(value.getUTCFullYear()).padStart(4, '0'),
    String(value.getUTCMonth() + 1).padStart(2, '0'),
    String(value.getUTCDate()).padStart(2, '0'),
  ].join('-');
}

/** Accept the graphical treatment only when all bounded, redacted cells name
 * exactly the Month already shown in the title. A malformed/stale payload
 * safely uses the normal list rather than drawing misleading counts. */
export function planningGridMatchesMonth(
  month: PlanningMonth,
  planningGrid: readonly PlanningGridCell[] | undefined,
): planningGrid is readonly PlanningGridCell[] {
  if (!planningGrid || planningGrid.length !== 42) return false;
  const first = planningMonthDateAt(month, 0);
  const mondayOffset = (first.getUTCDay() + 6) % 7;
  return planningGrid.every((cell, offset) => {
    const expected = planningMonthDateAt(month, offset - mondayOffset);
    return cell.day === isoUtcDay(expected)
      && cell.inMonth === (expected.getUTCMonth() === month.month);
  });
}

/** Compact count labels preserve the Calendar/Reminders distinction without
 * exposing titles or identifiers on the month overview. */
export function planningGridMarker(cell: PlanningGridCell): string {
  const marker = [];
  if (cell.eventCount > 0) marker.push(`E${cell.eventCount}`);
  if (cell.reminderCount > 0) marker.push(`R${cell.reminderCount}`);
  return marker.join(' ');
}

/** The graphical calendar owns only the six week action rows. Month controls
 * remain an ordinary list once selected, so they are never hidden behind the
 * grid or given an invented local interaction. */
export function canRenderPlanningMonthGrid(
  month: PlanningMonth | undefined,
  planningGrid: readonly PlanningGridCell[] | undefined,
  selectable: boolean,
  selectedIndex: number,
  itemCount: number,
): boolean {
  return month !== undefined
    && planningGridMatchesMonth(month, planningGrid)
    && selectable
    && selectedIndex >= 0
    && selectedIndex < 6
    && itemCount >= 6;
}

export function hudSidebarIconName(item: string): HudIconName {
  const normalized = item.trim().toLowerCase();
  const icons: Readonly<Record<string, HudIconName>> = {
    messages: 'messages',
    notifications: 'notifications',
    planning: 'calendar',
    calendar: 'calendar',
    tasks: 'tasks',
    navigation: 'navigation',
    more: 'more',
    quicknotes: 'quicknotes',
    brainstorm: 'brainstorm',
    'agent terminal': 'brainstorm',
    debate: 'brainstorm',
    activity: 'activity',
  };
  return icons[normalized] ?? 'settings';
}

export function resolveHudListKind(title: string): HudListKind {
  const normalized = title.trim().toUpperCase();
  if (normalized.startsWith('MESSAGES')) return 'messages';
  if (normalized.startsWith('NOTIFICATIONS')) return 'notifications';
  if (normalized.startsWith('PLANNING')) return 'calendar';
  if (normalized.startsWith('CALENDAR')) return 'calendar';
  if (normalized.startsWith('TASKS')) return 'tasks';
  if (normalized.startsWith('QUICKNOTES')) return 'quicknotes';
  if (normalized.startsWith('ACTIVITY') || normalized.startsWith('ACTION ·')) return 'activity';
  return 'generic';
}

export function productivityPhaseLabel(phase: ProductivityHudModel['phase']): string {
  const labels: Readonly<Record<ProductivityHudModel['phase'], string>> = {
    verifying: 'VERIFYING',
    saved: 'SAVED',
    completed: 'COMPLETED',
    failed: 'NOT SAVED',
    check: 'CHECK REQUIRED',
    cancelled: 'CANCELLED',
    not_saved: 'NOT SAVED',
    due: 'DUE NOW',
  };
  return labels[phase];
}

export function productivityPhaseProgress(phase: ProductivityHudModel['phase']): number {
  if (phase === 'saved' || phase === 'completed' || phase === 'due') return 1;
  if (phase === 'verifying') return 0.45;
  return 0.18;
}

interface StatusParts {
  time: string;
  state: string;
  battery?: string;
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

/** The canvas uses normal 8-bit color for antialiasing; its G2 transport is
 * packed Gray4. Quantize before tile packing so authored hierarchy survives
 * inside the 16 native grayscale levels. */
export function gray8ToGray4(value: number): number {
  return Math.max(0, Math.min(15, Math.round(value * 15 / 255)));
}

export function hudDisplayTitle(value: string): string {
  const normalized = value.replace(/\s*·\s*/g, ' · ').trim();
  if (!normalized || normalized !== normalized.toUpperCase()) return normalized;
  return normalized.split(' · ').map((part) => {
    if (part === 'JOAT' || /^\d+$/.test(part)) return part;
    if (part === 'QUICKNOTES') return 'QuickNotes';
    const lower = part.toLowerCase();
    return lower.charAt(0).toUpperCase() + lower.slice(1);
  }).join(' · ');
}

function parseStatus(status: string): StatusParts {
  const compact = status.replace(/\s+/g, ' ').trim();
  const time = compact.match(/^\d{1,2}:\d{2}/)?.[0] ?? '';
  const battery = compact.match(/bat\s*(\d+)%/i)?.[1];
  let state = compact
    .replace(/^\d{1,2}:\d{2}\s*/, '')
    .replace(/\s*bat\s*\d+%\+?$/i, '')
    .trim();
  state = state.replace(/^JOAT\s*[·-]?\s*/i, '').trim();
  return {
    time,
    state: `JOAT · ${(state || 'READY').toUpperCase()}`,
    ...(battery ? { battery } : {}),
  };
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
      primary: 'JOAT on your Mac is unavailable.',
      recovery: 'Open JOAT on your Mac to reconnect.',
      hint: 'left x2: menu · long-press: mic',
    };
  }
  if (/\breconnecting\b/i.test(input.status)) {
    return {
      kind: 'empty',
      title: 'Reconnecting',
      primary: 'Restoring JOAT on your Mac…',
      recovery: 'This screen will refresh automatically.',
      hint: 'left x2: menu · long-press: mic',
    };
  }
  if (/\bconnecting\b/i.test(input.status)) {
    return {
      kind: 'empty',
      title: 'Connecting',
      primary: 'Waiting for JOAT on your Mac…',
      recovery: 'This screen will refresh automatically.',
      hint: 'left x2: menu · long-press: mic',
    };
  }
  if (!main && !context && !tray) {
    return {
      kind: 'empty',
      title: 'Ready',
      primary: 'Ask JOAT anything.',
      recovery: 'Say “JOAT …” or double-click left for the menu.',
      hint: 'left x2: menu · long-press: mic',
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
export function parseHudListItem(value: string): HudListItemParts {
  const ageMatch = value.match(/^\[([^\]]+)]\s*/);
  const content = ageMatch ? value.slice(ageMatch[0].length) : value;
  const [primary, secondary] = splitMessage(content);
  return { primary, secondary, age: ageMatch?.[1]?.trim() ?? '' };
}

export interface HudVisibleListItem {
  item: string;
  index: number;
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
    let remainder = word;
    while (remainder && measure(remainder) > maxWidth) {
      let end = remainder.length;
      while (end > 1 && measure(remainder.slice(0, end)) > maxWidth) end -= 1;
      lines.push(remainder.slice(0, end));
      remainder = remainder.slice(end);
    }
    return remainder;
  };

  for (const word of words) {
    const candidate = line ? `${line} ${word}` : word;
    if (measure(candidate) <= maxWidth) {
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

/** Streaming replies show the newest complete lines. The opening words have
 * already been read; pinning them would make every later token invisible. */
export function visibleHudTailLines(
  lines: readonly string[],
  maximumLines: number,
): string[] {
  const count = Math.max(1, maximumLines);
  const visible = lines.slice(-count);
  if (lines.length > count && visible.length > 0) visible[0] = `…${visible[0]}`;
  return visible;
}

/** Keep the selected row visible on the three-row optical page without
 * throwing away rows four and five from the Mac's capability results. */
export function visibleHudListItems(
  items: readonly string[],
  selectedIndex: number,
  capacity = 3,
): HudVisibleListItem[] {
  const count = Math.max(1, capacity);
  const maximumStart = Math.max(0, items.length - count);
  const start = Math.min(Math.max(0, selectedIndex - count + 1), maximumStart);
  return items.slice(start, start + count).map((item, offset) => ({
    item,
    index: start + offset,
  }));
}

/** The wearable sidebar has room for five rows, but the capability model is
 * one continuous scrollable list. Keep the selected row visible instead of
 * burying capabilities behind a second menu. */
export function visibleHudSidebarItems(
  items: readonly string[],
  selectedIndex: number,
  capacity = 5,
): HudVisibleListItem[] {
  const count = Math.max(1, capacity);
  const maximumStart = Math.max(0, items.length - count);
  const start = Math.min(Math.max(0, selectedIndex - count + 1), maximumStart);
  return items.slice(start, start + count).map((item, offset) => ({
    item,
    index: start + offset,
  }));
}

export class HudCanvasRenderer implements HudFrameRenderer {
  private readonly icons: Promise<Record<HudIconName, HTMLImageElement>>;

  constructor() {
    this.icons = Promise.all(
      (Object.entries(ICON_SOURCES) as Array<[HudIconName, string]>)
        .map(async ([name, source]) => [name, await loadSvg(source)] as const),
    ).then((entries) => Object.fromEntries(entries) as Record<HudIconName, HTMLImageElement>);
  }

  async render(frame: HudVisualFrame): Promise<readonly HudImageTile[]> {
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
      case 'sidebar':
        this.drawSidebar(context, frame, icons);
        break;
      case 'list':
        this.drawList(context, frame, icons);
        break;
      case 'confirmation':
        this.drawConfirmation(context, frame, icons);
        break;
      case 'navigation':
        this.drawNavigation(context, frame.navigation);
        break;
      case 'brainstorm':
        this.drawBrainstorm(context, frame.brainstorm, icons);
        break;
      case 'productivity':
        this.drawProductivity(context, frame.productivity, icons);
        break;
    }
    // Chrome is the final compositing layer. Besides making ownership
    // explicit, this prevents state-specific paths from ever obscuring the
    // clock/privacy/battery spine on host canvas implementations.
    this.drawShell(context, frame.status, frame.micOn, icons);
    // The assistant is not a page. It is painted last into the same four G2
    // image tiles, so it can speak over any feature without allocating an
    // impossible fifth image container or replacing the feature underneath.
    if (frame.assistantOverlay) {
      this.drawAssistantOverlay(context, frame.assistantOverlay.text, icons);
    }

    const rgba = context.getImageData(0, 0, CANVAS.width, CANVAS.height).data;
    const pixels = new Uint8Array(CANVAS.width * CANVAS.height);
    for (let source = 0, target = 0; source < rgba.length; source += 4, target += 1) {
      pixels[target] = gray8ToGray4(
        Math.max(rgba[source] ?? 0, rgba[source + 1] ?? 0, rgba[source + 2] ?? 0),
      );
    }
    return splitGray4Tiles(pixels);
  }

  private drawShell(
    context: CanvasRenderingContext2D,
    statusText: string,
    micOn: boolean,
    icons: Record<HudIconName, HTMLImageElement>,
  ): void {
    context.strokeStyle = gray(HAIRLINE);
    context.lineWidth = 1;
    roundedRect(context, 1.5, 1.5, 573, 285, 20);
    context.stroke();

    const status = parseStatus(statusText);
    context.textBaseline = 'middle';
    context.fillStyle = gray(SUPPORT);
    context.font = `12px ${HUD_FONT}`;
    context.textAlign = 'left';
    context.fillText(
      status.time || '--:--',
      30,
      HUD_VISUAL_GEOMETRY.statusBaselineY,
    );
    context.textAlign = 'center';
    context.font = `10px ${HUD_FONT}`;
    context.fillText(status.state, 288, HUD_VISUAL_GEOMETRY.statusBaselineY);

    this.drawIcon(context, icons.microphone, 410, 12, 15, micOn ? SUPPORT : QUIET);
    context.textAlign = 'left';
    context.fillText(micOn ? 'ON' : 'OFF', 428, HUD_VISUAL_GEOMETRY.statusBaselineY);
    if (status.battery) {
      context.textAlign = 'right';
      context.fillText(`${status.battery}%`, 531, HUD_VISUAL_GEOMETRY.statusBaselineY);
      this.drawIcon(context, icons.battery, 538, 12, 20, SUPPORT);
    }

    context.strokeStyle = gray(HAIRLINE);
    context.beginPath();
    context.moveTo(17, HUD_VISUAL_GEOMETRY.statusDividerY);
    context.lineTo(280, HUD_VISUAL_GEOMETRY.statusDividerY);
    context.lineTo(284, HUD_VISUAL_GEOMETRY.statusDividerY + 4);
    context.lineTo(292, HUD_VISUAL_GEOMETRY.statusDividerY + 4);
    context.lineTo(296, HUD_VISUAL_GEOMETRY.statusDividerY);
    context.lineTo(559, HUD_VISUAL_GEOMETRY.statusDividerY);
    context.stroke();
  }

  private drawAssistantOverlay(
    context: CanvasRenderingContext2D,
    text: string,
    icons: Record<HudIconName, HTMLImageElement>,
  ): void {
    context.save();
    // The assistant preserves the feature *state*, but temporarily owns the
    // complete content field beneath the status spine. A smaller card leaves
    // recognisable rows around its edges, which still reads as two entangled
    // screens on a monochrome HUD.
    context.fillStyle = gray(0);
    context.fillRect(2, 37, CANVAS.width - 4, CANVAS.height - 39);
    roundedRect(context, 58, 51, 460, 183, 12);
    context.fill();
    context.strokeStyle = gray(SUPPORT);
    context.lineWidth = 1;
    context.stroke();

    this.drawIcon(context, icons.microphone, 82, 70, 15, BRIGHT);
    context.textAlign = 'left';
    context.textBaseline = 'middle';
    context.fillStyle = gray(BRIGHT);
    context.font = `11px ${HUD_FONT}`;
    context.fillText('JOAT', 105, 78);
    context.fillStyle = gray(CHROME);
    context.font = `9px ${HUD_FONT}`;
    context.fillText('ASSISTANT', 150, 78);
    context.strokeStyle = gray(HAIRLINE);
    context.beginPath();
    context.moveTo(82, 94);
    context.lineTo(494, 94);
    context.stroke();

    context.fillStyle = gray(BRIGHT);
    context.font = `17px ${HUD_FONT}`;
    context.textAlign = 'center';
    this.drawFittedWrappedText(context, text, 288, 130, 386, 4);

    context.fillStyle = gray(QUIET);
    context.font = `9px ${HUD_FONT}`;
    context.textAlign = 'center';
    context.fillText('TAP TO CONTINUE', 288, 214);
    context.restore();
  }

  private drawOverview(
    context: CanvasRenderingContext2D,
    frame: {
      status: string;
      main: string;
      mainBrightness: number;
      context: string;
      tray: string;
      hint: string;
    },
    icons: Record<HudIconName, HTMLImageElement>,
    opacity = 1,
    offsetX = 0,
  ): void {
    context.save();
    context.globalAlpha = opacity;
    context.translate(offsetX, 0);
    const overview = resolveHudOverview(frame);

    if (overview.kind === 'empty') {
      const left = opacity < 1 ? 110 : 36;
      context.textAlign = 'left';
      context.textBaseline = 'middle';
      context.fillStyle = gray(BRIGHT);
      context.font = `20px ${HUD_FONT}`;
      context.fillText(overview.title, left, 64);
      context.fillStyle = gray(SUPPORT);
      context.font = `16px ${HUD_FONT}`;
      context.fillText(overview.primary, left, 106);
      context.fillStyle = gray(CHROME);
      context.font = `10px ${HUD_FONT}`;
      context.fillText(overview.recovery, left, 132);
      context.fillStyle = gray(QUIET);
      context.font = `9px ${HUD_FONT}`;
      context.textAlign = 'center';
      context.fillText(overview.hint, 288 - offsetX, 267);
      context.restore();
      return;
    }

    if (overview.main) {
      const main = overview.main;
      context.textAlign = 'left';
      context.textBaseline = 'middle';
      context.fillStyle = gray(SUPPORT);
      context.font = `10px ${HUD_FONT}`;
      context.fillText(main.label.toUpperCase(), 110, 76);
      const mainIcon = main.structured && /NEXT|CALENDAR/i.test(main.label)
        ? icons.calendar
        : icons.messages;
      const mainLevel = hudBrightnessGray(frame.mainBrightness);
      this.drawIcon(context, mainIcon, 111, 91, 32, mainLevel);
      context.fillStyle = gray(mainLevel);
      const mainSize = opacity < 1 ? 19 : main.structured ? 25 : 18;
      context.font = `${mainSize}px ${HUD_FONT}`;
      if (main.structured || opacity < 1) {
        this.fitText(
          context,
          main.value,
          HUD_VISUAL_GEOMETRY.overviewMainX,
          112,
          opacity < 1 ? 330 : 359,
        );
      } else {
        this.drawWrappedText(
          context,
          main.value,
          HUD_VISUAL_GEOMETRY.overviewMainX,
          103,
          359,
          22,
          2,
        );
      }
    }

    if (overview.tray) {
      this.drawIcon(context, icons.notifications, 112, 158, 19, SUPPORT);
      context.fillStyle = gray(SUPPORT);
      context.font = `14px ${HUD_FONT}`;
      context.fillText(
        overview.tray,
        147,
        HUD_VISUAL_GEOMETRY.overviewTrayY,
      );
    }

    if (overview.context) {
      context.fillStyle = gray(CHROME);
      context.font = `9px ${HUD_FONT}`;
      context.fillText(
        overview.context.label.toUpperCase(),
        110,
        HUD_VISUAL_GEOMETRY.overviewContextY,
      );
      context.fillStyle = gray(SUPPORT);
      context.font = `10px ${HUD_FONT}`;
      this.fitText(
        context,
        overview.context.value,
        205,
        HUD_VISUAL_GEOMETRY.overviewContextY,
        330,
      );
    }

    context.fillStyle = gray(QUIET);
    context.font = `9px ${HUD_FONT}`;
    context.textAlign = 'center';
    context.fillText(frame.hint, 288 - offsetX, 263);
    context.restore();
  }

  private drawSidebar(
    context: CanvasRenderingContext2D,
    frame: Extract<HudVisualFrame, { kind: 'sidebar' }>,
    icons: Record<HudIconName, HTMLImageElement>,
  ): void {
    this.drawOverview(
      context,
      frame,
      icons,
      0.24,
      HUD_VISUAL_GEOMETRY.sidebarBackdropOffsetX,
    );
    context.fillStyle = '#000000';
    context.fillRect(3, 38, HUD_VISUAL_GEOMETRY.sidebarWidth - 3, 246);
    context.strokeStyle = gray(HAIRLINE);
    context.beginPath();
    context.moveTo(1.5, 38);
    context.lineTo(1.5, 267);
    context.stroke();

    context.strokeStyle = gray(17);
    context.beginPath();
    context.moveTo(HUD_VISUAL_GEOMETRY.sidebarWidth + 0.5, 55);
    context.lineTo(HUD_VISUAL_GEOMETRY.sidebarWidth + 0.5, 250);
    context.stroke();

    const visibleItems = visibleHudSidebarItems(frame.items, frame.selectedIndex);
    visibleItems.forEach(({ item, index }, row) => {
      const y = 70 + row * 39;
      if (index === frame.selectedIndex) {
        context.fillStyle = '#000000';
        context.fillRect(12, y - 17, HUD_VISUAL_GEOMETRY.sidebarWidth - 14, 34);
        context.fillStyle = gray(BRIGHT);
        context.fillRect(12, y - 17, 3, 34);
      }
      const level = index === frame.selectedIndex ? BRIGHT : SUPPORT;
      const icon = icons[hudSidebarIconName(item)];
      this.drawIcon(context, icon, 26, y - 9, 18, level);
      context.fillStyle = gray(level);
      context.textAlign = 'left';
      context.textBaseline = 'middle';
      context.font = `13px ${HUD_FONT}`;
      context.fillText(item, 54, y);
    });

    // Tiny chevrons make the continuous capability list discoverable without
    // adding another interactive control or consuming a wearable row.
    const firstVisible = visibleItems[0]?.index ?? 0;
    const lastVisible = visibleItems[visibleItems.length - 1]?.index ?? -1;
    context.strokeStyle = gray(SUPPORT);
    context.lineWidth = 1;
    if (firstVisible > 0) {
      context.beginPath();
      context.moveTo(145, 49);
      context.lineTo(149, 45);
      context.lineTo(153, 49);
      context.stroke();
    }
    if (lastVisible < frame.items.length - 1) {
      context.beginPath();
      context.moveTo(145, 254);
      context.lineTo(149, 258);
      context.lineTo(153, 254);
      context.stroke();
    }
  }

  private drawList(
    context: CanvasRenderingContext2D,
    frame: Extract<HudVisualFrame, { kind: 'list' }>,
    icons: Record<HudIconName, HTMLImageElement>,
  ): void {
    const planningMonth = parsePlanningMonthTitle(frame.title);
    const planningGrid = planningMonth && planningGridMatchesMonth(
      planningMonth,
      frame.planningGrid,
    )
      ? frame.planningGrid
      : undefined;
    // Rows 0…5 are the source-backed weekly drill-down actions. Once the
    // wearer scrolls to prior/next/view controls, use the normal list so the
    // selected control remains explicit and reachable instead of hiding it
    // beneath a decorative month grid.
    if (planningMonth && planningGrid && canRenderPlanningMonthGrid(
      planningMonth,
      planningGrid,
      frame.selectable,
      frame.selectedIndex,
      frame.items.length,
    )) {
      this.drawPlanningMonth(context, frame, planningMonth, planningGrid);
      return;
    }

    context.textAlign = 'left';
    context.textBaseline = 'middle';
    context.fillStyle = gray(BRIGHT);
    context.font = `20px ${HUD_FONT}`;
    context.fillText(hudDisplayTitle(frame.title), 36, 64);

    const listKind = resolveHudListKind(frame.title);
    visibleHudListItems(frame.items, frame.selectedIndex).forEach((visible, row) => {
      const { item, index } = visible;
      const { primary: name, secondary: message, age } = parseHudListItem(item);
      const top = 82 + row * 56;
      if (frame.selectable && index === frame.selectedIndex) {
        context.strokeStyle = gray(BRIGHT);
        context.lineWidth = 1.2;
        roundedRect(context, 23, top - 8, 527, 48, 5);
        context.stroke();
      } else {
        context.strokeStyle = gray(70);
        context.lineWidth = 0.7;
        context.beginPath();
        context.moveTo(26, top + 40);
        context.lineTo(550, top + 40);
        context.stroke();
      }

      context.strokeStyle = gray(SUPPORT);
      context.lineWidth = 1;
      context.beginPath();
      context.arc(56, top + 16, 14, 0, Math.PI * 2);
      context.stroke();
      if (listKind === 'messages' || listKind === 'generic') {
        context.fillStyle = gray(SUPPORT);
        context.textAlign = 'center';
        context.font = `14px ${HUD_FONT}`;
        context.fillText((name || item).charAt(0).toUpperCase(), 56, top + 16);
      } else {
        this.drawIcon(context, icons[listKind], 47, top + 7, 18, SUPPORT);
      }

      context.textAlign = 'left';
      context.fillStyle = gray(index === frame.selectedIndex ? BRIGHT : SUPPORT);
      context.font = `16px ${HUD_FONT}`;
      context.fillText(name || item, HUD_VISUAL_GEOMETRY.listContentX, top + 8);
      if (message) {
        context.fillStyle = gray(SUPPORT);
        context.font = `10px ${HUD_FONT}`;
        this.fitText(context, message, HUD_VISUAL_GEOMETRY.listContentX, top + 28, 360);
      }
      context.textAlign = 'right';
      context.fillStyle = gray(CHROME);
      context.font = `9px ${HUD_FONT}`;
      context.fillText(age, 520, top + 8);
    });

    context.fillStyle = gray(QUIET);
    context.font = `9px ${HUD_FONT}`;
    context.textAlign = 'center';
    context.fillText(frame.hint, 288, 267);
  }

  /**
   * Calendar is the one Planning surface that benefits from a dense spatial
   * map. Each horizontal band is also one existing selectable week row, so
   * TAP still follows the real Mac-owned action contract (month → week), not
   * a local visual approximation with a second selection model.
   */
  private drawPlanningMonth(
    context: CanvasRenderingContext2D,
    frame: Extract<HudVisualFrame, { kind: 'list' }>,
    month: PlanningMonth,
    planningGrid: readonly PlanningGridCell[],
  ): void {
    const gridLeft = 37;
    const gridRight = 539;
    const cellWidth = (gridRight - gridLeft) / 7;
    const gridTop = 96;
    const rowHeight = 22;
    const weekdays = ['M', 'T', 'W', 'T', 'F', 'S', 'S'];
    const selectedWeek = frame.selectedIndex;

    context.textBaseline = 'middle';
    context.textAlign = 'left';
    context.fillStyle = gray(BRIGHT);
    context.font = `19px ${HUD_FONT}`;
    context.fillText('Planning', 36, 60);
    context.textAlign = 'right';
    context.fillStyle = gray(SUPPORT);
    context.font = `10px ${HUD_FONT}`;
    context.fillText(`${month.label.slice(0, 3)} ${month.year} · E EVENT · R REMINDER`, 540, 60);

    context.textAlign = 'center';
    context.fillStyle = gray(CHROME);
    context.font = `10px ${HUD_FONT}`;
    weekdays.forEach((weekday, column) => {
      context.fillText(weekday, gridLeft + cellWidth * (column + 0.5), 80);
    });
    context.strokeStyle = gray(HAIRLINE);
    context.lineWidth = 1;
    context.beginPath();
    context.moveTo(gridLeft, 87.5);
    context.lineTo(gridRight, 87.5);
    context.stroke();

    const days = planningMonthDays(month);
    for (let row = 0; row < 6; row += 1) {
      const top = gridTop + row * rowHeight;
      if (row === selectedWeek) {
        context.strokeStyle = gray(BRIGHT);
        context.lineWidth = 1.2;
        roundedRect(context, gridLeft - 9, top - 9, gridRight - gridLeft + 18, rowHeight, 4);
        context.stroke();
      } else {
        context.strokeStyle = gray(HAIRLINE);
        context.lineWidth = 0.7;
        context.beginPath();
        context.moveTo(gridLeft, top + 10.5);
        context.lineTo(gridRight, top + 10.5);
        context.stroke();
      }
      for (let column = 0; column < 7; column += 1) {
        const day = days[row * 7 + column];
        if (!day) continue;
        const cell = planningGrid[row * 7 + column];
        const level = day.inMonth
          ? row === selectedWeek ? BRIGHT : SUPPORT
          : QUIET;
        context.fillStyle = gray(level);
        context.font = `${day.inMonth ? 13 : 11}px ${HUD_FONT}`;
        const x = gridLeft + cellWidth * (column + 0.5);
        context.fillText(String(day.day), x, top - 1);
        const marker = cell ? planningGridMarker(cell) : '';
        if (marker) {
          context.fillStyle = gray(row === selectedWeek ? BRIGHT : SUPPORT);
          context.font = `7px ${HUD_FONT}`;
          context.fillText(marker, x, top + 9);
        }
      }
    }

    const selectedSummary = frame.items[selectedWeek]?.replace(/^.*? · /, '') ?? '';
    context.fillStyle = gray(SUPPORT);
    context.font = `10px ${HUD_FONT}`;
    context.fillText(
      selectedSummary ? `TAP WEEK · ${selectedSummary.toUpperCase()}` : 'TAP WEEK · AGENDA',
      288,
      243,
    );
    const nextMonth = frame.items[7]?.replace(/^[‹›\s]+/, '').toUpperCase() ?? 'MONTH CONTROLS';
    context.fillStyle = gray(CHROME);
    context.font = `8px ${HUD_FONT}`;
    this.fitText(context, `WEEK ${selectedWeek + 1}/6 · NEXT ${nextMonth}`, 288, 257, 500);
    context.fillStyle = gray(QUIET);
    context.font = `9px ${HUD_FONT}`;
    this.fitText(context, frame.hint, 288, 267, 500);
  }

  private drawConfirmation(
    context: CanvasRenderingContext2D,
    frame: Extract<HudVisualFrame, { kind: 'confirmation' }>,
    icons: Record<HudIconName, HTMLImageElement>,
  ): void {
    context.textAlign = 'center';
    context.textBaseline = 'middle';
    context.fillStyle = gray(SUPPORT);
    context.font = `13px ${HUD_FONT}`;
    if (frame.reviewDomain) {
      const isCalendar = frame.reviewDomain === 'calendar';
      this.drawIcon(context, icons[isCalendar ? 'calendar' : 'tasks'], 220, 51, 18, SUPPORT);
      context.fillText(isCalendar ? 'Calendar review' : 'Reminder review', 302, 60);
    } else {
      context.fillText('Confirm action', 288, 79);
    }
    context.strokeStyle = gray(CHROME);
    context.beginPath();
    context.moveTo(274, 96.5);
    context.lineTo(302, 96.5);
    context.stroke();

    context.fillStyle = gray(BRIGHT);
    this.drawFittedWrappedText(context, frame.prompt, 288, 127, 470, 3);

    const drawAction = (x: number, copy: string, level: number): void => {
      context.strokeStyle = gray(level);
      context.lineWidth = 1;
      context.beginPath();
      context.roundRect(
        x,
        HUD_VISUAL_GEOMETRY.confirmationActionsY,
        HUD_VISUAL_GEOMETRY.confirmationActionWidth,
        HUD_VISUAL_GEOMETRY.confirmationActionHeight,
        8,
      );
      context.stroke();
      context.fillStyle = gray(level);
      context.font = `10px ${HUD_FONT}`;
      context.textAlign = 'center';
      context.fillText(
        copy,
        x + HUD_VISUAL_GEOMETRY.confirmationActionWidth / 2,
        HUD_VISUAL_GEOMETRY.confirmationActionsY
          + HUD_VISUAL_GEOMETRY.confirmationActionHeight / 2,
      );
    };

    drawAction(HUD_VISUAL_GEOMETRY.confirmationPrimaryX, 'TAP · Confirm', BRIGHT);
    drawAction(HUD_VISUAL_GEOMETRY.confirmationSecondaryX, 'SWIPE · Cancel', SUPPORT);

    const progress = Math.max(0, Math.min(1, frame.secondsRemaining / 8));
    context.fillStyle = gray(38);
    context.fillRect(
      HUD_VISUAL_GEOMETRY.confirmationProgressX,
      246,
      HUD_VISUAL_GEOMETRY.confirmationProgressWidth,
      3,
    );
    context.fillStyle = gray(SUPPORT);
    context.fillRect(
      HUD_VISUAL_GEOMETRY.confirmationProgressX,
      246,
      Math.round(HUD_VISUAL_GEOMETRY.confirmationProgressWidth * progress),
      3,
    );
    context.fillStyle = gray(CHROME);
    context.font = `9px ${HUD_FONT}`;
    context.textAlign = 'center';
    context.fillText(`${frame.secondsRemaining}s remaining`, 288, 267);
  }

  private drawProductivity(
    context: CanvasRenderingContext2D,
    model: ProductivityHudModel,
    icons: Record<HudIconName, HTMLImageElement>,
  ): void {
    const isCalendar = model.domain === 'calendar';
    const label = isCalendar ? 'Calendar' : 'Reminders';
    const phase = productivityPhaseLabel(model.phase);
    const icon = icons[isCalendar ? 'calendar' : 'tasks'];

    context.textBaseline = 'middle';
    context.textAlign = 'left';
    this.drawIcon(context, icon, 36, 49, 20, BRIGHT);
    context.fillStyle = gray(BRIGHT);
    context.font = `15px ${HUD_FONT}`;
    context.fillText(label, 66, 60);
    context.textAlign = 'right';
    context.fillStyle = gray(model.phase === 'due' ? BRIGHT : SUPPORT);
    context.font = `10px ${HUD_FONT}`;
    context.fillText(phase, 540, 60);
    context.strokeStyle = gray(HAIRLINE);
    context.beginPath();
    context.moveTo(36, 76.5);
    context.lineTo(540, 76.5);
    context.stroke();

    context.textAlign = 'left';
    context.fillStyle = gray(BRIGHT);
    context.font = `20px ${HUD_FONT}`;
    this.drawWrappedText(context, model.title, 36, 105, 504, 18, 2);

    context.strokeStyle = gray(model.phase === 'due' ? SUPPORT : CHROME);
    context.lineWidth = 1;
    roundedRect(context, 36, 143, 504, 70, 7);
    context.stroke();
    context.fillStyle = gray(BRIGHT);
    context.font = `14px ${HUD_FONT}`;
    this.fitText(context, model.primary, 54, 166, 468);
    context.fillStyle = gray(SUPPORT);
    context.font = `10px ${HUD_FONT}`;
    this.drawWrappedText(context, model.secondary, 54, 188, 468, 14, 2);

    context.fillStyle = gray(model.phase === 'due' ? SUPPORT : HAIRLINE);
    context.fillRect(36, 230, 504, 3);
    const progress = productivityPhaseProgress(model.phase);
    context.fillStyle = gray(model.phase === 'due' ? BRIGHT : SUPPORT);
    context.fillRect(36, 230, Math.round(504 * progress), 3);

    context.fillStyle = gray(QUIET);
    context.font = `9px ${HUD_FONT}`;
    context.textAlign = 'center';
    this.fitText(context, model.hint, 288, 267, 500);
  }

  private drawBrainstorm(
    context: CanvasRenderingContext2D,
    model: BrainstormHudModel,
    icons: Record<HudIconName, HTMLImageElement>,
  ): void {
    const state = model.state.replace(/_/g, ' ').toUpperCase();
    context.textAlign = 'left';
    context.textBaseline = 'middle';
    this.drawIcon(context, icons.brainstorm, 36, 49, 20, BRIGHT);
    context.fillStyle = gray(BRIGHT);
    context.font = `15px ${HUD_FONT}`;
    context.fillText(model.screen === 'agent_terminal' ? 'Agent Terminal' : 'Brainstorm', 66, 60);
    context.textAlign = 'right';
    context.fillStyle = gray(SUPPORT);
    context.font = `10px ${HUD_FONT}`;
    context.fillText(state, 540, 60);
    context.strokeStyle = gray(HAIRLINE);
    context.beginPath();
    context.moveTo(36, 76.5);
    context.lineTo(540, 76.5);
    context.stroke();

    if (model.screen === 'agent_terminal') {
      context.textAlign = 'left';
      context.fillStyle = gray(BRIGHT);
      context.font = `21px ${HUD_FONT}`;
      this.fitText(context, `HERMES · ${state}`, 36, 108, 500);
      context.fillStyle = gray(SUPPORT);
      context.font = `11px ${HUD_FONT}`;
      this.fitText(context, model.endpointHost || 'Dedicated JOAT session', 36, 135, 500);
      context.strokeStyle = gray(CHROME);
      roundedRect(context, 36, 154, 504, 53, 6);
      context.stroke();
      context.fillStyle = gray(SUPPORT);
      context.font = `11px ${HUD_FONT}`;
      context.fillText(
        model.noToolsChecked ? 'NO TOOLS CHECKED BEFORE SEND' : 'TOOL STATE NOT CLEARED',
        54,
        176,
      );
      context.fillStyle = gray(CHROME);
      context.font = `10px ${HUD_FONT}`;
      this.fitText(context, model.terminalReason || 'Hermes participates as a Brainstorm seat.', 54, 195, 462);
    } else {
      context.textAlign = 'left';
      context.fillStyle = gray(BRIGHT);
      context.font = `18px ${HUD_FONT}`;
      this.fitText(context, model.topic || 'Choose a question to explore', 36, 106, 504);
      context.fillStyle = gray(CHROME);
      context.font = `9px ${HUD_FONT}`;
      context.fillText('TABLE', 36, 129);
      model.collaborators.slice(0, 3).forEach((collaborator, index) => {
        const x = 36 + index * 168;
        context.strokeStyle = gray(index === 0 ? SUPPORT : CHROME);
        roundedRect(context, x, 139, 156, 27, 5);
        context.stroke();
        context.fillStyle = gray(SUPPORT);
        context.font = `10px ${HUD_FONT}`;
        this.fitText(context, collaborator, x + 12, 156, 132);
      });
      context.fillStyle = gray(SUPPORT);
      context.font = `10px ${HUD_FONT}`;
      this.fitText(context, model.activeSpeaker ? `NOW · ${model.activeSpeaker}` : 'NOW · waiting for a collaborator', 36, 187, 504);
      context.fillStyle = gray(CHROME);
      context.font = `11px ${HUD_FONT}`;
      this.drawWrappedText(context, model.activity || 'Say “JOAT, brainstorm …” to begin.', 36, 207, 504, 16, 2);
    }
    const actionHint = brainstormActionHint(model);
    context.fillStyle = gray(QUIET);
    context.font = `9px ${HUD_FONT}`;
    context.textAlign = 'center';
    this.fitText(context, actionHint, 288, 267, 500);
  }

  private drawNavigation(
    context: CanvasRenderingContext2D,
    navigation: NavigationHudModel,
  ): void {
    if (navigation.view === 'guidance') {
      this.drawActiveNavigation(context, navigation);
      return;
    }
    if (
      navigation.view === 'uncertain'
      || navigation.view === 'wrong_way'
      || navigation.view === 'off_route'
      || navigation.view === 'rerouting'
    ) {
      this.drawNavigationWarning(context, navigation);
      return;
    }
    if (navigation.view === 'preview') {
      this.drawNavigationPreview(context, navigation);
      return;
    }
    if (navigation.view === 'arrived') {
      this.drawNavigationArrival(context, navigation);
      return;
    }
    this.drawNavigationDecision(context, navigation);
  }

  private drawNavigationDecision(
    context: CanvasRenderingContext2D,
    navigation: NavigationHudModel,
  ): void {
    const labels: Readonly<Record<string, string>> = {
      destination: 'Confirm destination',
      repair: 'Repair destination',
      mode: 'Travel mode',
      error: 'Navigation',
    };
    context.textAlign = 'left';
    context.textBaseline = 'middle';
    context.fillStyle = gray(CHROME);
    context.font = `10px ${HUD_FONT}`;
    context.fillText(labels[navigation.view] ?? 'Navigation', 36, 59);
    context.fillStyle = gray(BRIGHT);
    context.font = `21px ${HUD_FONT}`;
    this.fitText(context, navigation.primary, 36, 84, 504);
    context.fillStyle = gray(SUPPORT);
    context.font = `11px ${HUD_FONT}`;
    this.fitText(context, navigation.secondary, 36, 108, 504);

    if (navigation.options.length > 0) {
      const rowHeight = navigation.options.length > 2 ? 36 : 45;
      const firstY = navigation.options.length > 2 ? 124 : 130;
      navigation.options.slice(0, 3).forEach((option, index) => {
        const y = firstY + index * rowHeight;
        const selected = index === navigation.selectedIndex;
        context.strokeStyle = gray(selected ? BRIGHT : HAIRLINE);
        context.lineWidth = selected ? 1.2 : 0.8;
        roundedRect(context, 36, y, 504, rowHeight - 7, 5);
        context.stroke();
        if (selected) {
          context.fillStyle = gray(BRIGHT);
          context.fillRect(36, y, 3, rowHeight - 7);
        }
        context.fillStyle = gray(selected ? BRIGHT : SUPPORT);
        context.font = `${navigation.options.length > 2 ? 11 : 14}px ${HUD_FONT}`;
        this.fitText(context, option, 52, y + (rowHeight - 7) / 2, 470);
      });
    } else {
      context.strokeStyle = gray(navigation.view === 'error' ? SUPPORT : CHROME);
      context.lineWidth = 1;
      roundedRect(context, 36, 130, 504, 78, 8);
      context.stroke();
      if (navigation.view === 'destination') {
        this.drawMapPin(context, 67, 169, BRIGHT);
        context.fillStyle = gray(CHROME);
        context.font = `9px ${HUD_FONT}`;
        context.fillText('CANONICAL PLACE', 99, 153);
        context.fillStyle = gray(BRIGHT);
        context.font = `16px ${HUD_FONT}`;
        this.fitText(context, navigation.primary, 99, 177, 420);
        context.fillStyle = gray(SUPPORT);
        context.font = `10px ${HUD_FONT}`;
        this.fitText(context, navigation.secondary, 99, 197, 420);
      } else {
        this.drawWarningMark(context, 76, 169, SUPPORT);
        context.fillStyle = gray(SUPPORT);
        context.font = `13px ${HUD_FONT}`;
        this.drawWrappedText(context, navigation.secondary, 112, 157, 390, 18, 2);
      }
    }
    this.drawNavigationFooter(context, navigation.hint);
  }

  private drawNavigationPreview(
    context: CanvasRenderingContext2D,
    navigation: NavigationHudModel,
  ): void {
    context.textBaseline = 'middle';
    context.textAlign = 'left';
    context.fillStyle = gray(CHROME);
    context.font = `10px ${HUD_FONT}`;
    context.fillText(`${navigation.mode === 'cycling' ? 'CYCLE' : 'WALK'} · ROUTE PREVIEW`, 36, 59);
    context.fillStyle = gray(BRIGHT);
    context.font = `21px ${HUD_FONT}`;
    this.fitText(context, navigation.primary, 36, 85, 504);
    context.fillStyle = gray(SUPPORT);
    context.font = `11px ${HUD_FONT}`;
    this.fitText(context, navigation.secondary, 36, 108, 504);

    context.strokeStyle = gray(HAIRLINE);
    context.lineWidth = 7;
    context.lineCap = 'round';
    context.lineJoin = 'round';
    context.beginPath();
    context.moveTo(71, 202);
    context.lineTo(71, 153);
    context.lineTo(156, 153);
    context.lineTo(156, 187);
    context.lineTo(251, 187);
    context.stroke();
    context.strokeStyle = gray(BRIGHT);
    context.lineWidth = 3;
    context.setLineDash([7, 7]);
    context.stroke();
    context.setLineDash([]);
    this.drawMapPin(context, 251, 177, BRIGHT);

    context.fillStyle = gray(CHROME);
    context.font = `9px ${HUD_FONT}`;
    context.fillText('DISTANCE', 322, 148);
    context.fillText('ETA', 322, 190);
    context.fillStyle = gray(BRIGHT);
    context.font = `18px ${HUD_FONT}`;
    context.fillText(formatNavigationDistance(navigation.remainingDistanceM ?? 0), 409, 148);
    context.fillText(`${navigation.etaMinutes ?? 0} min`, 409, 190);
    this.drawNavigationFooter(context, navigation.hint);
  }

  private drawActiveNavigation(
    context: CanvasRenderingContext2D,
    navigation: NavigationHudModel,
  ): void {
    context.textBaseline = 'middle';
    context.textAlign = 'left';
    context.fillStyle = gray(CHROME);
    context.font = `10px ${HUD_FONT}`;
    context.fillText(`${navigation.mode === 'cycling' ? 'CYCLING' : 'WALKING'} · ACTIVE`, 36, 59);

    context.strokeStyle = gray(HAIRLINE);
    context.lineWidth = 1;
    roundedRect(context, 36, 76, 92, 108, 12);
    context.stroke();
    this.drawManeuver(context, navigation.maneuver ?? 'continue', 82, 118, BRIGHT);
    context.fillStyle = gray(BRIGHT);
    context.font = `16px ${HUD_FONT}`;
    context.textAlign = 'center';
    context.fillText(formatNavigationDistance(navigation.maneuverDistanceM ?? 0), 82, 164);

    context.textAlign = 'left';
    context.fillStyle = gray(BRIGHT);
    context.font = `25px ${HUD_FONT}`;
    this.fitText(context, navigation.primary, 156, 95, 384);
    context.fillStyle = gray(SUPPORT);
    context.font = `16px ${HUD_FONT}`;
    this.drawWrappedText(context, navigation.secondary, 156, 128, 384, 21, 2);
    context.fillStyle = gray(CHROME);
    context.font = `10px ${HUD_FONT}`;
    this.fitText(context, navigation.hint, 156, 190, 384);

    this.drawNavigationProgress(context, navigation.progress ?? 0);
    const footer = `${formatNavigationDistance(navigation.remainingDistanceM ?? 0)} left · ${navigation.etaMinutes ?? 0} min`;
    this.drawNavigationFooter(context, footer);
  }

  private drawNavigationWarning(
    context: CanvasRenderingContext2D,
    navigation: NavigationHudModel,
  ): void {
    const warning = navigation.view !== 'rerouting';
    context.textBaseline = 'middle';
    context.textAlign = 'center';
    context.fillStyle = gray(warning ? BRIGHT : SUPPORT);
    context.font = `11px ${HUD_FONT}`;
    context.fillText(navigation.view.replace('_', ' ').toUpperCase(), 288, 63);
    if (navigation.view === 'wrong_way') {
      this.drawManeuver(context, 'u_turn', 288, 105, BRIGHT);
    } else if (navigation.view === 'rerouting') {
      this.drawRerouteMark(context, 288, 105, SUPPORT);
    } else {
      this.drawWarningMark(context, 288, 105, warning ? BRIGHT : SUPPORT);
    }
    context.fillStyle = gray(BRIGHT);
    context.font = `24px ${HUD_FONT}`;
    context.fillText(navigation.primary, 288, 155);
    context.fillStyle = gray(SUPPORT);
    context.font = `13px ${HUD_FONT}`;
    context.fillText(navigation.secondary, 288, 184);
    context.fillStyle = gray(CHROME);
    context.font = `10px ${HUD_FONT}`;
    context.fillText(navigation.hint, 288, 211);
    this.drawNavigationFooter(
      context,
      navigation.remainingDistanceM === undefined
        ? 'Route held'
        : `${formatNavigationDistance(navigation.remainingDistanceM)} left`,
    );
  }

  private drawNavigationArrival(
    context: CanvasRenderingContext2D,
    navigation: NavigationHudModel,
  ): void {
    this.drawArrivalMark(context, 288, 97, BRIGHT);
    context.textAlign = 'center';
    context.textBaseline = 'middle';
    context.fillStyle = gray(BRIGHT);
    context.font = `25px ${HUD_FONT}`;
    context.fillText(navigation.primary, 288, 151);
    context.fillStyle = gray(SUPPORT);
    context.font = `13px ${HUD_FONT}`;
    this.drawFittedWrappedText(context, navigation.secondary, 288, 183, 470, 2);
    this.drawNavigationFooter(context, navigation.hint);
  }

  private drawNavigationFooter(context: CanvasRenderingContext2D, copy: string): void {
    context.fillStyle = gray(QUIET);
    context.font = `9px ${HUD_FONT}`;
    context.textAlign = 'center';
    context.textBaseline = 'middle';
    this.fitText(context, copy, 288, 267, 500);
  }

  private drawNavigationProgress(context: CanvasRenderingContext2D, progress: number): void {
    const normalized = Math.max(0, Math.min(1, progress));
    context.fillStyle = gray(HAIRLINE);
    context.fillRect(36, 228, 504, 3);
    context.fillStyle = gray(SUPPORT);
    context.fillRect(36, 228, Math.round(504 * normalized), 3);
  }

  private drawMapPin(
    context: CanvasRenderingContext2D,
    x: number,
    y: number,
    level: number,
  ): void {
    context.strokeStyle = gray(level);
    context.lineWidth = 2;
    context.beginPath();
    context.arc(x, y - 5, 10, Math.PI * 0.15, Math.PI * 0.85, true);
    context.lineTo(x, y + 16);
    context.closePath();
    context.stroke();
    context.beginPath();
    context.arc(x, y - 5, 3, 0, Math.PI * 2);
    context.stroke();
  }

  private drawWarningMark(
    context: CanvasRenderingContext2D,
    x: number,
    y: number,
    level: number,
  ): void {
    context.strokeStyle = gray(level);
    context.fillStyle = gray(level);
    context.lineWidth = 2;
    context.beginPath();
    context.moveTo(x, y - 26);
    context.lineTo(x + 25, y + 20);
    context.lineTo(x - 25, y + 20);
    context.closePath();
    context.stroke();
    context.fillRect(x - 1.5, y - 9, 3, 17);
    context.beginPath();
    context.arc(x, y + 14, 2, 0, Math.PI * 2);
    context.fill();
  }

  private drawRerouteMark(
    context: CanvasRenderingContext2D,
    x: number,
    y: number,
    level: number,
  ): void {
    context.strokeStyle = gray(level);
    context.lineWidth = 3;
    context.beginPath();
    context.arc(x, y, 22, -Math.PI * 0.25, Math.PI * 1.4);
    context.stroke();
    context.fillStyle = gray(level);
    context.beginPath();
    context.moveTo(x + 20, y - 17);
    context.lineTo(x + 30, y - 18);
    context.lineTo(x + 25, y - 8);
    context.closePath();
    context.fill();
  }

  private drawArrivalMark(
    context: CanvasRenderingContext2D,
    x: number,
    y: number,
    level: number,
  ): void {
    context.strokeStyle = gray(level);
    context.lineWidth = 2;
    context.beginPath();
    context.arc(x, y, 25, 0, Math.PI * 2);
    context.stroke();
    context.beginPath();
    context.moveTo(x - 12, y);
    context.lineTo(x - 3, y + 9);
    context.lineTo(x + 14, y - 11);
    context.stroke();
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
  ): void {
    const lines = wrapHudText(text, maxWidth, (value) => context.measureText(value).width);
    const visible = visibleHudTailLines(lines, maximumLines);
    visible.forEach((value, index) => {
      this.fitText(context, value, x, y + index * lineHeight, maxWidth);
    });
  }

  private drawFittedWrappedText(
    context: CanvasRenderingContext2D,
    text: string,
    x: number,
    y: number,
    maxWidth: number,
    maximumLines: number,
  ): void {
    const previous = context.textAlign;
    context.textAlign = 'center';
    let lines: string[] = [];
    let fontSize = 21;
    for (; fontSize >= 12; fontSize -= 1) {
      context.font = `${fontSize}px ${HUD_FONT}`;
      lines = wrapHudText(text, maxWidth, (value) => context.measureText(value).width);
      if (lines.length <= maximumLines) break;
    }
    const lineHeight = Math.max(16, fontSize + 2);
    const firstY = y - ((lines.length - 1) * lineHeight) / 2;
    lines.forEach((line, index) => context.fillText(line, x, firstY + index * lineHeight));
    context.textAlign = previous;
  }
}
