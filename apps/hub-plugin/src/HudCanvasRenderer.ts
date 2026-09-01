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
  splitGrayscaleTiles,
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
export const ASSISTANT_OVERLAY_DISMISS_HINT = 'TAP OR SWIPE TO DISMISS';
export const NOTIFICATION_DISMISS_HINT = 'tap: dismiss · left x2: menu';

/** Error recovery copy belongs in its warning card; repeating it in the
 * decision header makes the action harder to scan at a glance. */
export function showNavigationSecondaryInHeader(
  view: NavigationHudModel['view'],
): boolean {
  return view !== 'error';
}

/** Geometry for choice rows on the G2 lens. Decisions speak the one dense
 * flat-list language: 12px rows on a 20px pitch with the 3px selection rail.
 * No separators, no per-count roomy variant — pitch divides rows (the
 * wearer's ruling: the chrome was the waste, not any single line). */
export interface NavigationDecisionRow {
  baselineY: number;
  railTopY: number;
  railHeight: number;
  fontSize: number;
}

export function navigationDecisionRows(optionCount: number): readonly NavigationDecisionRow[] {
  const count = Math.min(3, Math.max(0, Math.floor(optionCount)));
  return Array.from({ length: count }, (_, index) => {
    const baselineY = 132 + index * 20;
    return { baselineY, railTopY: baselineY - 10, railHeight: 19, fontSize: 12 };
  });
}

/** The card's header, body, and footer are separate optical regions. Keeping
 * these anchors together prevents a long reply from sharing the divider row. */
const ASSISTANT_OVERLAY_GEOMETRY = {
  dividerY: 92,
  bodyTopY: 108,
  bodyBottomY: 184,
  footerBaselineY: 214,
} as const;

export interface AssistantOverlayTextLayout {
  dividerY: number;
  firstBaselineY: number;
  firstGlyphTopY: number;
  lastGlyphBottomY: number;
  footerBaselineY: number;
  lineHeight: number;
}

/** Calculate a centered body block strictly inside the assistant card's
 * text-safe region. The renderer and regression test share this contract. */
export function assistantOverlayTextLayout(
  lineCount: number,
  fontSize: number,
): AssistantOverlayTextLayout {
  const safeLineCount = Math.max(1, Math.min(4, Math.floor(lineCount)));
  const lineHeight = Math.max(16, fontSize + 2);
  const bodyCenterY = (
    ASSISTANT_OVERLAY_GEOMETRY.bodyTopY + ASSISTANT_OVERLAY_GEOMETRY.bodyBottomY
  ) / 2;
  const firstBaselineY = bodyCenterY - ((safeLineCount - 1) * lineHeight) / 2;
  const lastBaselineY = firstBaselineY + (safeLineCount - 1) * lineHeight;
  return {
    dividerY: ASSISTANT_OVERLAY_GEOMETRY.dividerY,
    firstBaselineY,
    firstGlyphTopY: firstBaselineY - fontSize / 2,
    lastGlyphBottomY: lastBaselineY + fontSize / 2,
    footerBaselineY: ASSISTANT_OVERLAY_GEOMETRY.footerBaselineY,
    lineHeight,
  };
}

/** Measured from the selected 576×288 reference panels. Keeping the major
 * anchors together makes visual drift obvious in review and in tests. */
export const HUD_VISUAL_GEOMETRY = {
  statusBaselineY: 20,
  statusDividerY: 35.5,
  overviewMainX: 176,
  overviewTrayY: 168,
  overviewContextY: 214,
  sidebarWidth: 160,
  listContentX: 93,
} as const;

/** Safe-write gestures share one static footer with the other dense HUDs.
 * Omitting the changing seconds counter prevents needless image transfers
 * while the wearer reads an otherwise unchanged review. */
export function confirmationActionHint(): string {
  return 'tap: confirm · swipe: cancel · left x2: back · auto-cancel';
}

/** Calendar/Reminder review follows the same compact reading axis as
 * Planning, Messages, and Navigation: one left-aligned content column with
 * no centered poster treatment. */
export function confirmationReviewLayout(): {
  headerIconX: number;
  headerTextX: number;
  headerBaselineY: number;
  dividerY: number;
  contentX: number;
  contentWidth: number;
  promptTopBaselineY: number;
  promptFontSize: number;
  promptLineHeight: number;
  promptMaxLines: number;
  footerBaselineY: number;
} {
  return {
    headerIconX: 36,
    headerTextX: 62,
    headerBaselineY: 60,
    dividerY: 78.5,
    contentX: 36,
    contentWidth: 504,
    promptTopBaselineY: 104,
    promptFontSize: 16,
    promptLineHeight: 20,
    promptMaxLines: 4,
    footerBaselineY: 267,
  };
}

/** The navigation views a wearer reads while stopped. They are content
 * pages, not the moving live session, so they render flat like every list. */
const FLAT_NAVIGATION_VIEWS: ReadonlySet<string> = new Set([
  'destination', 'repair', 'mode', 'preview', 'arrived', 'error',
]);

/** Content pages use the status spine alone. The second rounded container
 * ("the card") spent border and margin pixels on decoration; every list,
 * confirmation, productivity, and stationary navigation page now renders
 * flat. Only the moving states (guidance, warnings, rerouting) keep the
 * live-session shell, like Brainstorm. */
export function usesFlatHudShell(
  kind: HudVisualFrame['kind'],
  title = '',
  navigationView?: NavigationHudModel['view'],
): boolean {
  void title;
  if (kind === 'navigation') return FLAT_NAVIGATION_VIEWS.has(navigationView ?? '');
  return kind === 'confirmation' || kind === 'productivity' || kind === 'list';
}

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
  model: Pick<BrainstormHudModel, 'screen' | 'state' | 'terminalState' | 'noToolsChecked' | 'acceptanceArmed'>,
): string {
  if (model.screen === 'brainstorm') {
    if (model.state === 'running') {
      // "tap: Hermes" described a different feature, and a downward swipe
      // used to CANCEL — the wearer asked to scroll, so swipes scroll and
      // cancel is the deliberate left double-press only.
      return 'swipe: scroll · tap: terminal · left x2: cancel';
    }
    if (model.state === 'acceptance_ready') {
      return 'tap: start test · left x2: menu';
    }
    if (model.state === 'topic_capture') {
      return 'speak your question · left x2: cancel';
    }
    if (
      model.state === 'ready'
      && model.terminalState === 'ready'
      && model.noToolsChecked
    ) {
      return model.acceptanceArmed === true
        ? 'tap: speak · right/ring x2: test · left x2: menu'
        : 'tap: speak · left x2: menu';
    }
    return 'swipe: scroll · left x2: menu · long-press: mic';
  }
  // The Agent Terminal surface. These must mirror handleClick(), which
  // branches on terminalState — NOT state — once no lane is running.
  //
  // Two of these were lying. handleClick was corrected so a tap here arms
  // TERMINAL INPUT ("it used to send begin_topic_capture — i.e. the Agent
  // Terminal's primary gesture started a Brainstorm, a different feature")
  // and this hint was never updated, so the glass still told the wearer
  // that tapping the terminal begins a Brainstorm. And a lane waiting on
  // an approval fell through to "tap: retry Hermes" — the wrong action,
  // with the only useful one ("terminal approve") never shown at all.
  if (model.state === 'approval_required') {
    // Only worth saying while the gateway can actually carry the answer.
    // A lane can hold a pending approval while the CONNECTION is blocked or
    // unavailable — different fields, and they disagree — and telling the
    // wearer to approve into a dead link is advice that cannot be followed,
    // which is the same defect as the "tap: retry Hermes" this replaced.
    if (model.terminalState === 'ready') {
      return 'say "terminal approve" or "terminal deny" · left x2: menu';
    }
    return 'tap: retry Hermes, then approve · left x2: menu';
  }
  if (model.state === 'running') {
    return 'swipe: scroll · say "terminal cancel" · left x2: menu';
  }
  return model.terminalState === 'ready'
    ? 'tap: speak to the terminal · left x2: menu'
    : 'tap: retry Hermes · left x2: menu';
}

/** The terminal's policy row must distinguish a pending device approval or
 * offline brain from an actual tool-policy failure. It is a status row, not
 * a decorative card: the transcript needs the space. */
export function agentTerminalSafetyLabel(
  model: Pick<BrainstormHudModel, 'state' | 'noToolsChecked'>,
): string {
  if (model.state === 'approval_required') return 'APPROVAL PENDING';
  if (model.state === 'unavailable') return 'CONNECTION UNAVAILABLE';
  if (model.state === 'blocked') return 'TERMINAL BLOCKED';
  return model.noToolsChecked
    ? 'NO TOOLS CHECKED BEFORE SEND'
    : 'TOOL STATE NOT CLEARED';
}

/** A live terminal is a transcript, not a connection-status dashboard.
 * Preserve the recovery detail only when the wearer needs it to act; once
 * Hermes is running, collapse it into one truthful status spine and give the
 * conversation the remaining lens area. */
export interface AgentTerminalTranscriptLayout {
  summary: string;
  dividerY: number;
  transcriptBaselineY: number;
  transcriptLineCapacity: number;
  showRecoveryDetails: boolean;
}

/** Which lane the terminal is focused on, for the summary spine.
 *
 * The engine sends "<lane> · <state>[ · N waiting]" as the topic. This
 * header said HERMES whatever was focused, so a wearer running a calendar
 * lane or a gateway session read someone else's name above their own
 * output. Falls back to HERMES only when no lane is named. */
export function agentTerminalFocusLabel(topic: string | undefined): string {
  const named = ((topic ?? '').split(' · ')[0] ?? '').trim();
  return (named || 'HERMES').slice(0, 24).toUpperCase();
}

export function agentTerminalTranscriptLayout(
  model: Pick<BrainstormHudModel, 'state' | 'noToolsChecked' | 'topic'>,
): AgentTerminalTranscriptLayout {
  const state = brainstormStateLabel(model.state);
  const focus = agentTerminalFocusLabel(model.topic);
  if (model.state === 'running') {
    return {
      summary: `${focus} > ${state} · ${agentTerminalSafetyLabel(model)}`,
      // Dense by demand: the wearer's words were "stop wasting so much
      // place on things that do not affect functionality". Header and
      // LANES are two tight rows; everything else is transcript.
      dividerY: 78.5,
      transcriptBaselineY: 92,
      transcriptLineCapacity: 12,
      showRecoveryDetails: false,
    };
  }
  return {
    summary: `${focus} > ${state}`,
    dividerY: 118.5,
    transcriptBaselineY: 132,
    transcriptLineCapacity: 9,
    showRecoveryDetails: true,
  };
}

export function brainstormStateLabel(state: BrainstormHudModel['state']): string {
  return state === 'topic_capture'
    ? 'SPEAK NOW'
    : state === 'acceptance_ready'
    ? 'TEST READY'
    : state.replace(/_/g, ' ').toUpperCase();
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

/** A selected calendar week needs a signal, not a seven-cell outline. */
export function planningMonthSelectionRail(selectedWeek: number): {
  x: number;
  y: number;
  width: number;
  height: number;
} {
  const week = Math.min(5, Math.max(0, Math.floor(selectedWeek)));
  return { x: 28, y: 87 + week * 22, width: 3, height: 22 };
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

export function productivityPhaseProgress(phase: ProductivityHudModel['phase']): number {
  if (phase === 'saved' || phase === 'monitoring' || phase === 'completed' || phase === 'due') return 1;
  if (phase === 'verifying') return 0.45;
  return 0.18;
}

/** The wearable owns the escape wording for a live productivity surface.
 *
 * Native status text may describe a write or a due reminder, but it must not
 * conceal the one local action that is always available: return to the
 * unified Planning screen. This changes only the display route; it neither
 * cancels an in-flight native write nor claims an unavailable post-save undo.
 */
export function productivityActionHint(model: Pick<ProductivityHudModel, 'phase' | 'monitoringArmed'>): string {
  if (model.phase === 'due') return 'say: complete reminder · Back: left x2 to Planning';
  if (model.phase === 'saved' && model.monitoringArmed) {
    return 'Back: left x2 to Planning · monitoring next';
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
    return 'Back: left x2 to Planning · auto in 5s';
  }
  return 'Back: left x2 to Planning';
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

export function hudDisplayTitle(value: string): string {
  const normalized = value.replace(/\s*·\s*/g, ' · ').trim();
  if (!normalized || normalized !== normalized.toUpperCase()) return normalized;
  const planning = normalized.match(/^(PLANNING|CALENDAR|TASKS) · PLAN · (.+)$/);
  if (planning) {
    const root = titleCaseHudWords(planning[1] ?? 'PLANNING');
    const period = planning[2] ?? '';
    const scope = planningScopeForDisplay(period);
    const displayedPeriod = titleCaseHudWords(period);
    return scope
      ? `${root} · ${scope} · ${displayedPeriod}`
      : `${root} · ${displayedPeriod}`;
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

export interface MessagesConversationRow {
  kind: 'incoming' | 'outgoing' | 'instruction';
  label: string;
  body: string;
}

export interface MessagesConversationTranscript {
  /** Mac-owned history position, for example “Latest messages — down for older”. */
  heading: string;
  /** Speaker rows only. Voice and microphone guidance belongs in the HUD hint. */
  rows: MessagesConversationRow[];
}

/** Seven transcript rows use the vertical budget between the Mac-owned
 * heading (y87) and the footer hint: 113 + 22·6 = 245. */
export const MESSAGES_CONVERSATION_ROW_CAPACITY = 7;

/** Conversation frames are deliberately not rendered as notification rows.
 * The Mac owns the text; this parser only distinguishes the already-labeled
 * speaker rows from truthful voice/microphone instructions. */
export function parseMessagesConversationRow(value: string): MessagesConversationRow {
  const [label, body] = splitMessage(value);
  if (!body) return { kind: 'instruction', label: '', body: label };
  return {
    kind: label.trim().toLowerCase() === 'you' ? 'outgoing' : 'incoming',
    label,
    body,
  };
}

/**
 * A Messages conversation is a compact transcript, not a stack of selectable
 * cards. Keep the Mac's history heading above it and leave the voice controls
 * in the existing truthful HUD hint below it. The raw list index remains the
 * selection contract, so paging keeps the selected speaker row in view
 * without inventing a second local selection model.
 */
export function messagesConversationTranscript(
  items: readonly string[],
  selectedIndex: number,
  capacity = MESSAGES_CONVERSATION_ROW_CAPACITY,
): MessagesConversationTranscript {
  const parsed = items.map((item, index) => ({ index, row: parseMessagesConversationRow(item) }));
  const heading = parsed.find(({ row }) => row.kind === 'instruction')?.row.body ?? 'Conversation';
  const speakerRows = parsed.filter(({ row }) => row.kind !== 'instruction');
  if (speakerRows.length === 0) return { heading, rows: [] };

  const count = Math.max(1, capacity);
  const selectedSpeaker = Math.max(
    0,
    speakerRows.filter(({ index }) => index <= selectedIndex).length - 1,
  );
  const start = Math.min(
    Math.max(0, selectedSpeaker - count + 1),
    Math.max(0, speakerRows.length - count),
  );
  return {
    heading,
    rows: speakerRows.slice(start, start + count).map(({ row }) => row),
  };
}

/** Keep the reader wire shape deliberately small: the Mac supplies one
 * bounded page, while the renderer preserves that page's opening words. */
export function messagesFullTextContent(items: readonly string[]): {
  metadata: string;
  body: string;
} {
  return {
    metadata: items[0]?.trim() || 'Message',
    body: items.slice(1).map((item) => item.trim()).filter(Boolean).join(' ')
      || 'No text is available for this message.',
  };
}

/** Keep every truthful Messages action label visible on the fixed G2 footer.
 * Dense transcript screens can expose paging, reading, reply, and Back at
 * once, so they use the small but still legible footer face. */
export function messagesConversationFooterFontSize(hint: string): 8 | 9 {
  return hint.length > 42 ? 8 : 9;
}

/** A full message uses the lens body, while preserving its status footer. */
export const MESSAGES_READER_BODY_LINE_CAPACITY = 8;

/** The official simulator under-reports the width of the G2 bitmap font.
 * This conservative visible-column limit keeps a rendered line inside the
 * actual lens instead of trusting that inaccurate measurement. */
export const MESSAGES_READER_VISIBLE_COLUMNS = 52;

export function messagesReaderBodyLines(text: string): string[] {
  return wrapHudText(
    text,
    MESSAGES_READER_VISIBLE_COLUMNS,
    (value) => value.length,
  );
}

/** The heading and spoken-reply instruction are informational. Only a real
 * speaker row earns the thin selection rail that opens the full-text reader. */
export function selectedMessagesConversationRow(
  items: readonly string[],
  selectedIndex: number,
  capacity = MESSAGES_CONVERSATION_ROW_CAPACITY,
): number | undefined {
  const speakerRows = items
    .map((item, index) => ({ index, row: parseMessagesConversationRow(item) }))
    .filter(({ row }) => row.kind !== 'instruction');
  const selectedSpeaker = speakerRows.findIndex(({ index }) => index === selectedIndex);
  if (selectedSpeaker < 0) return undefined;
  const count = Math.max(1, capacity);
  const start = Math.min(
    Math.max(0, selectedSpeaker - count + 1),
    Math.max(0, speakerRows.length - count),
  );
  return selectedSpeaker - start;
}

/** Every Mac-authored Messages screen is dense lens text, never a generic
 * browser-like list card. */
/** Only the two genuinely bespoke Messages screens keep a dedicated layout:
 * the two-column conversation transcript and the full-text reader. Every
 * other Messages page — the thread picker, compose, access/retry states —
 * speaks the one dense focus-list language like the rest of the HUD. */
export function usesMessagesCompactLayout(title: string): boolean {
  const normalized = title.trim().toUpperCase();
  return normalized === 'MESSAGES · CONVERSATION' || normalized === 'MESSAGES · MESSAGE';
}

/** Navigation starts by voice, so its non-selectable capability entry is a
 * compact instruction—not a generic list of card-like rows. */
export function usesNavigationSetupLayout(title: string, selectable: boolean): boolean {
  return title.trim().toUpperCase() === 'NAVIGATION' && !selectable;
}

export interface PlanningFocusRow {
  primaryBaselineY: number;
  secondaryBaselineY?: number;
  railTopY: number;
  railHeight: number;
}

export interface FocusListMarker {
  glyph: string;
  /** 'content' markers brighten with the row (● record); 'annotation'
   * markers stay quiet chrome (□ todo box, – key point). */
  emphasis: 'content' | 'annotation';
}

/** The semantic marker vocabulary for dense rows. Deliberately tiny and
 * firmware-font-safe (design-guidelines Unicode set) so the same strings
 * mean the same thing on the text-fallback surface: a marker indicates the
 * KIND of row, it never decorates. */
const FOCUS_LIST_MARKERS: Readonly<Record<string, FocusListMarker>> = {
  '●': { glyph: '●', emphasis: 'content' },     // a recording
  '□': { glyph: '□', emphasis: 'annotation' },  // an open todo
  '–': { glyph: '–', emphasis: 'annotation' },  // a key point
};

export function focusListMarker(text: string): FocusListMarker | undefined {
  return text.length > 2 && text[1] === ' '
    ? FOCUS_LIST_MARKERS[text[0] ?? '']
    : undefined;
}

/** A selectable list that mixes marked and unmarked rows reserves the marker
 * column for every row, so all titles share one left edge instead of a
 * ragged one. Non-selectable detail pages keep their hanging-indent bullets
 * under section labels, where the indent IS the structure. */
export function listReservesMarkerColumn(items: readonly string[]): boolean {
  return items.some((item) => {
    const { primary } = parseHudListItem(item);
    return focusListMarker(primary || item) !== undefined;
  });
}

/** THE list language for every focus page: dense reading rows with a
 * selection rail and hairline separators — never a card per row. The old
 * roomy layout spent 56 optical pixels, an icon badge, and a rounded outline
 * on each line; on a 288-pixel-tall lens that left three rows per glance
 * (wearer's ruling: wasteful). Planning day agendas keep a second line for
 * their time/kind pair; everything else shows six rows per screen. */
export function planningFocusRows(
  title: string,
  itemCount: number,
): readonly PlanningFocusRow[] {
  const normalized = title.trim().toUpperCase();

  const agenda = /^(?:PLANNING|CALENDAR|TASKS) · PLAN · (?:MON|TUE|WED|THU|FRI|SAT|SUN)\b/
    .test(normalized);
  // Use the vertical budget: the content column runs from under the title to
  // just above the footer. Nine single-line rows (or six two-line agenda
  // rows) per glance, no separator lines — pitch alone divides rows, and
  // every reclaimed pixel is another visible row of the wearer's content.
  const firstBaselineY = agenda ? 88 : 84;
  const pitch = agenda ? 30 : 20;
  const lastBaselineY = 246;
  const capacity = Math.floor((lastBaselineY - firstBaselineY) / pitch) + 1;
  const count = Math.min(capacity, Math.max(0, Math.floor(itemCount)));
  return Array.from({ length: count }, (_, index) => {
    const primaryBaselineY = firstBaselineY + index * pitch;
    return agenda
      ? {
        primaryBaselineY,
        secondaryBaselineY: primaryBaselineY + 13,
        railTopY: primaryBaselineY - 11,
        railHeight: 27,
      }
      : {
        primaryBaselineY,
        railTopY: primaryBaselineY - 10,
        railHeight: 19,
      };
  });
}

export interface ProductivityOutcomeLayout {
  titleBaselineY: number;
  dividerY: number;
  primaryBaselineY: number;
  secondaryBaselineY: number;
  footerBaselineY: number;
}

/** The lifecycle surface carries a single result, so it earns hierarchy—not a
 * 504-pixel card. A title, hairline, compact evidence rows, and progress mark
 * leave the lens calm while retaining the return gesture in the footer. */
export function productivityOutcomeLayout(): ProductivityOutcomeLayout {
  return {
    titleBaselineY: 98,
    dividerY: 136.5,
    primaryBaselineY: 158,
    secondaryBaselineY: 181,
    footerBaselineY: 267,
  };
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

/**
 * Preserve explicit speaker-line boundaries inside the small wearable
 * transcript, then retain the newest complete visual lines. The producer
 * caps the transcript too; this is only the final optical fit, never storage.
 */
export function brainstormTranscriptLines(
  transcript: string,
  maxWidth: number,
  maximumLines: number,
  measure: (value: string) => number,
  scrollFromTail = 0,
): string[] {
  const lines = transcript
    .split(/\r?\n/)
    .flatMap((entry) => wrapHudText(entry, maxWidth, measure));
  return windowHudTailLines(lines, maximumLines, scrollFromTail);
}

/** Total wrapped rows — the scroll clamp needs the real extent. */
export function brainstormTranscriptRowCount(
  transcript: string,
  maxWidth: number,
  measure: (value: string) => number,
): number {
  return transcript
    .split(/\r?\n/)
    .flatMap((entry) => wrapHudText(entry, maxWidth, measure))
    .length;
}

/** Streaming replies show the newest complete lines. The opening words have
 * already been read; pinning them would make every later token invisible. */
export function visibleHudTailLines(
  lines: readonly string[],
  maximumLines: number,
): string[] {
  return windowHudTailLines(lines, maximumLines, 0);
}

/** The tail window, moved `scrollFromTail` rows toward the beginning.
 *
 * Scrollback is the wearer's ask, verbatim: "can i scroll up and down to
 * see the replies?" — the settled synthesis is the whole point of a
 * session, and a tail-only view threw away every earlier turn the moment
 * it left the bottom. `…` marks hidden older rows; the caller announces
 * hidden NEWER rows in the hint, where the eye already is. */
export function windowHudTailLines(
  lines: readonly string[],
  maximumLines: number,
  scrollFromTail: number,
): string[] {
  const count = Math.max(1, maximumLines);
  const back = Math.max(0, Math.min(scrollFromTail, lines.length - count));
  const end = lines.length - back;
  const visible = lines.slice(Math.max(0, end - count), end);
  if (end - count > 0 && visible.length > 0) visible[0] = `…${visible[0]}`;
  return visible;
}

/** Incoming notifications are previews, not a live stream. Keep the sender
 * and opening sentence visible; the final ellipsis makes omitted tail copy
 * explicit instead of silently showing only the last words. */
export function visibleHudLeadingLines(
  lines: readonly string[],
  maximumLines: number,
): string[] {
  const count = Math.max(1, maximumLines);
  const visible = lines.slice(0, count);
  if (lines.length > count && visible.length > 0) {
    const last = visible.length - 1;
    visible[last] = `${visible[last]}…`;
  }
  return visible;
}

export interface FittedHudText {
  fontSize: number;
  lines: string[];
}

/** Fit bounded HUD copy without ever drawing outside its optical region.
 * When even the minimum readable size overflows, retain the newest lines so
 * a streamed reply still exposes its conclusion. */
export function fitHudWrappedText(
  text: string,
  maxWidth: number,
  maximumLines: number,
  measureAtSize: (value: string, fontSize: number) => number,
): FittedHudText {
  const lineLimit = Math.max(1, maximumLines);
  let minimumLines: string[] = [];
  for (let fontSize = 21; fontSize >= 12; fontSize -= 1) {
    const lines = wrapHudText(
      text,
      maxWidth,
      (value) => measureAtSize(value, fontSize),
    );
    if (lines.length <= lineLimit) return { fontSize, lines };
    minimumLines = lines;
  }
  return { fontSize: 12, lines: visibleHudTailLines(minimumLines, lineLimit) };
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
        this.drawList(context, frame);
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
    this.drawShell(
      context,
      frame.status,
      frame.micOn,
      icons,
      usesFlatHudShell(
        frame.kind,
        frame.kind === 'list' ? frame.title : '',
        frame.kind === 'navigation' ? frame.navigation.view : undefined,
      ),
    );
    // The assistant is not a page. It is painted last into the same four G2
    // image tiles, so it can speak over any feature without allocating an
    // impossible fifth image container or replacing the feature underneath.
    if (frame.assistantOverlay) {
      this.drawAssistantOverlay(context, frame.assistantOverlay.text, icons);
    }

    const rgba = context.getImageData(0, 0, CANVAS.width, CANVAS.height).data;
    const pixels = new Uint8Array(CANVAS.width * CANVAS.height);
    for (let source = 0, target = 0; source < rgba.length; source += 4, target += 1) {
      pixels[target] = Math.max(rgba[source] ?? 0, rgba[source + 1] ?? 0, rgba[source + 2] ?? 0);
    }
    return splitGrayscaleTiles(pixels);
  }

  private drawShell(
    context: CanvasRenderingContext2D,
    statusText: string,
    micOn: boolean,
    icons: Record<HudIconName, HTMLImageElement>,
    flat: boolean,
  ): void {
    if (!flat) {
      context.strokeStyle = gray(HAIRLINE);
      context.lineWidth = 1;
      roundedRect(context, 1.5, 1.5, 573, 285, 20);
      context.stroke();
    }

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
    context.moveTo(82, ASSISTANT_OVERLAY_GEOMETRY.dividerY);
    context.lineTo(494, ASSISTANT_OVERLAY_GEOMETRY.dividerY);
    context.stroke();

    context.fillStyle = gray(BRIGHT);
    context.font = `17px ${HUD_FONT}`;
    context.textAlign = 'center';
    this.drawAssistantOverlayText(context, text);

    context.fillStyle = gray(QUIET);
    context.font = `9px ${HUD_FONT}`;
    context.textAlign = 'center';
    context.fillText(
      ASSISTANT_OVERLAY_DISMISS_HINT,
      288,
      ASSISTANT_OVERLAY_GEOMETRY.footerBaselineY,
    );
    context.restore();
  }

  private drawOverview(
    context: CanvasRenderingContext2D,
    frame: {
      status: string;
      main: string;
      mainBrightness: number;
      mainKind?: HudContentKind;
      context: string;
      tray: string;
      hint: string;
    },
    icons: Record<HudIconName, HTMLImageElement>,
  ): void {
    context.save();
    const overview = resolveHudOverview(frame);

    if (overview.kind === 'empty') {
      const left = 36;
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
      context.fillText(overview.hint, 288, 267);
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
      // The icon states the KIND of the main content — calendar for what's
      // next, a chat glyph only for an actual message/notification. Plain
      // assistant text gets no icon at all: JOAT's resting screen is JOAT,
      // not a Messages app wearing a chat bubble.
      const mainIcon = main.structured && /NEXT|CALENDAR/i.test(main.label)
        ? icons.calendar
        : frame.mainKind === 'notification'
          ? icons.notifications
          : undefined;
      const mainLevel = hudBrightnessGray(frame.mainBrightness);
      if (mainIcon) this.drawIcon(context, mainIcon, 111, 91, 32, mainLevel);
      // Without an icon the text owns the icon column too — no dead gutter.
      const mainX = mainIcon ? HUD_VISUAL_GEOMETRY.overviewMainX : 110;
      const mainWidth = 359 + (HUD_VISUAL_GEOMETRY.overviewMainX - mainX);
      context.fillStyle = gray(mainLevel);
      const mainSize = main.structured ? 25 : 18;
      context.font = `${mainSize}px ${HUD_FONT}`;
      if (main.structured) {
        this.fitText(
          context,
          main.value,
          mainX,
          112,
          mainWidth,
        );
      } else {
        this.drawWrappedText(
          context,
          main.value,
          mainX,
          103,
          mainWidth,
          22,
          2,
          frame.mainKind === 'notification' ? 'leading' : 'tail',
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
    context.fillText(
      frame.mainKind === 'notification' ? NOTIFICATION_DISMISS_HINT : frame.hint,
      288,
      263,
    );
    context.restore();
  }

  private drawSidebar(
    context: CanvasRenderingContext2D,
    frame: Extract<HudVisualFrame, { kind: 'sidebar' }>,
    icons: Record<HudIconName, HTMLImageElement>,
  ): void {
    // The backdrop is drawn EXACTLY as the Overview draws it — same
    // intensity, same position. That is a transfer decision, not only a
    // visual one: the frame ships as four 288x144 tiles over a serialized
    // BLE lane, and the presenter skips any tile whose pixels are unchanged.
    // Dimming or shifting the backdrop moves every lit pixel, so all four
    // tiles go dirty and the wearer watches the top half arrive, then the
    // bottom. Leaving the right half identical means only the two left tiles
    // carrying the rail are sent, and the menu appears in half the transfers.
    this.drawOverview(context, frame, icons);
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

    if (usesMessagesCompactLayout(frame.title)) {
      this.drawMessagesConversation(context, frame);
      return;
    }

    if (usesNavigationSetupLayout(frame.title, frame.selectable)) {
      this.drawNavigationSetup(context, frame);
      return;
    }

    // Every remaining list renders in the one dense row language. The old
    // roomy per-row card (icon badge, 56px pitch, rounded selection outline)
    // is deliberately gone — it fit three rows where six belong.
    this.drawPlanningFocusList(context, frame);
  }

  private drawPlanningFocusList(
    context: CanvasRenderingContext2D,
    frame: Extract<HudVisualFrame, { kind: 'list' }>,
  ): void {
    const rows = planningFocusRows(frame.title, frame.items.length);
    const reserveMarker = frame.selectable && listReservesMarkerColumn(frame.items);

    context.textAlign = 'left';
    context.textBaseline = 'middle';
    context.fillStyle = gray(BRIGHT);
    context.font = `20px ${HUD_FONT}`;
    context.fillText(hudDisplayTitle(frame.title), 36, 60);

    const visible = visibleHudListItems(frame.items, frame.selectedIndex, rows.length);
    visible.forEach((entry, index) => {
      const row = rows[index];
      if (!row) return;
      const selected = frame.selectable && entry.index === frame.selectedIndex;
      if (selected) {
        context.fillStyle = gray(BRIGHT);
        context.fillRect(36, row.railTopY, 3, row.railHeight);
      }
      if (row.secondaryBaselineY !== undefined) {
        const { primary, secondary } = parseHudListItem(entry.item);
        context.fillStyle = gray(selected ? BRIGHT : SUPPORT);
        context.font = `14px ${HUD_FONT}`;
        this.fitText(context, primary || entry.item, 52, row.primaryBaselineY, 488);
        context.fillStyle = gray(selected ? SUPPORT : CHROME);
        context.font = `10px ${HUD_FONT}`;
        this.fitText(context, secondary, 52, row.secondaryBaselineY, 488);
        return;
      }
      this.drawFocusListLine(context, entry.item, row.primaryBaselineY, selected, reserveMarker);
    });

    // Content longer than one window gets a hairline scrollbar at the right
    // edge — position and proportion, no words. Without it a section label
    // stranded on the last row reads as an amputated page, not "scroll on".
    if (frame.items.length > rows.length && visible.length > 0) {
      const trackTop = 74;
      const trackHeight = 178;
      const start = visible[0]?.index ?? 0;
      const thumbHeight = Math.max(
        12,
        Math.round((rows.length / frame.items.length) * trackHeight),
      );
      const travel = trackHeight - thumbHeight;
      const range = Math.max(1, frame.items.length - rows.length);
      const thumbTop = trackTop + Math.round((start / range) * travel);
      context.fillStyle = gray(QUIET);
      context.fillRect(556, trackTop, 2, trackHeight);
      context.fillStyle = gray(CHROME);
      context.fillRect(556, thumbTop, 2, thumbHeight);
    }

    context.fillStyle = gray(QUIET);
    context.font = `9px ${HUD_FONT}`;
    context.textAlign = 'center';
    context.fillText(frame.hint, 288, 267);
    context.textAlign = 'left';
  }

  /** One dense line with real internal hierarchy, so weight means something:
   * a small dim age chip, an aligned semantic marker column (● recording,
   * □ todo, – key point — firmware-safe glyphs that INDICATE, never badge
   * chrome), normal-weight primary, dimmer trailing detail, and quiet 10px
   * label treatment for ALL-CAPS section markers (SUMMARY, KEY POINTS, …).
   * Uniform 12px SUPPORT across whole rows read as "everything bold" — the
   * wearer's exact complaint. */
  private drawFocusListLine(
    context: CanvasRenderingContext2D,
    item: string,
    baselineY: number,
    selected: boolean,
    reserveMarker = false,
  ): void {
    const trimmed = item.trim();
    if (/^[A-Z][A-Z ]{1,18}$/.test(trimmed)) {
      context.fillStyle = gray(CHROME);
      context.font = `10px ${HUD_FONT}`;
      context.fillText(trimmed, 52, baselineY);
      return;
    }
    const { age, primary, secondary } = parseHudListItem(item);
    let x = 52;
    if (age) {
      context.fillStyle = gray(selected ? SUPPORT : CHROME);
      context.font = `9px ${HUD_FONT}`;
      context.fillText(age, x, baselineY);
      x += 34;
    }
    let text = primary || item;
    const marker = focusListMarker(text);
    if (marker) {
      text = text.slice(2);
      context.fillStyle = gray(
        marker.emphasis === 'content' ? (selected ? BRIGHT : SUPPORT) : CHROME,
      );
      context.font = `11px ${HUD_FONT}`;
      context.fillText(marker.glyph, x, baselineY);
      x += 18;
    } else if (reserveMarker) {
      x += 18; // keep one shared title edge in lists that mix marked rows
    }
    context.fillStyle = gray(selected ? BRIGHT : SUPPORT);
    context.font = `12px ${HUD_FONT}`;
    if (!secondary) {
      this.fitText(context, text, x, baselineY, 540 - x);
      return;
    }
    const primaryWidth = Math.min(context.measureText(text).width, 320);
    this.fitText(context, text, x, baselineY, 320);
    context.fillStyle = gray(selected ? SUPPORT : CHROME);
    context.font = `10px ${HUD_FONT}`;
    this.fitText(context, secondary, x + primaryWidth + 12, baselineY, 540 - x - primaryWidth - 12);
  }

  private drawNavigationSetup(
    context: CanvasRenderingContext2D,
    frame: Extract<HudVisualFrame, { kind: 'list' }>,
  ): void {
    const [spokenStart = 'Say “JOAT, navigate to …”', expectation = 'I’ll confirm the place.'] = frame.items;
    context.textAlign = 'left';
    context.textBaseline = 'middle';
    context.fillStyle = gray(BRIGHT);
    context.font = `20px ${HUD_FONT}`;
    context.fillText('Navigation', 36, 64);

    context.font = `15px ${HUD_FONT}`;
    this.fitText(context, spokenStart, 36, 104, 504);
    context.fillStyle = gray(SUPPORT);
    context.font = `11px ${HUD_FONT}`;
    this.fitText(context, expectation, 36, 130, 504);

    context.fillStyle = gray(QUIET);
    context.font = `9px ${HUD_FONT}`;
    context.textAlign = 'center';
    this.fitText(context, frame.hint, 288, 267, 500);
  }

  private drawMessagesConversation(
    context: CanvasRenderingContext2D,
    frame: Extract<HudVisualFrame, { kind: 'list' }>,
  ): void {
    context.textAlign = 'left';
    context.textBaseline = 'middle';
    context.fillStyle = gray(BRIGHT);
    context.font = `20px ${HUD_FONT}`;
    context.fillText(hudDisplayTitle(frame.title), 36, 64);

    if (frame.title.trim().toUpperCase() === 'MESSAGES · CONVERSATION') {
      const transcript = messagesConversationTranscript(
        frame.items,
        frame.selectedIndex,
        MESSAGES_CONVERSATION_ROW_CAPACITY,
      );
      const selectedRow = frame.selectable
        ? selectedMessagesConversationRow(
          frame.items,
          frame.selectedIndex,
          MESSAGES_CONVERSATION_ROW_CAPACITY,
        )
        : undefined;
      context.fillStyle = gray(CHROME);
      context.font = `10px ${HUD_FONT}`;
      this.fitText(context, transcript.heading, 36, 87, 504);

      transcript.rows.forEach((parsed, row) => {
        const y = 113 + row * 22;
        const outgoing = parsed.kind === 'outgoing';
        const selected = row === selectedRow;
        if (selected) {
          context.fillStyle = gray(BRIGHT);
          context.fillRect(36, y - 10, 3, 18);
        }
        // The wearer reads what the OTHER person wrote; their own words are
        // context. Incoming carries the weight, outgoing recedes — never one
        // uniform brightness across the transcript.
        context.fillStyle = gray(outgoing ? CHROME : SUPPORT);
        context.font = `9px ${HUD_FONT}`;
        this.fitText(context, parsed.label, selected ? 48 : 36, y, selected ? 42 : 54);
        context.fillStyle = gray(outgoing ? CHROME : selected ? BRIGHT : SUPPORT);
        context.font = `12px ${HUD_FONT}`;
        this.fitText(context, parsed.body, 104, y, 436);
      });

      context.fillStyle = gray(QUIET);
      context.font = `${messagesConversationFooterFontSize(frame.hint)}px ${HUD_FONT}`;
      context.textAlign = 'center';
      this.fitText(context, frame.hint, 288, 267, 504);
      return;
    }

    // MESSAGES · MESSAGE — the full-text reader. Metadata is quiet chrome;
    // the message body is the one bright thing on the page.
    const detail = messagesFullTextContent(frame.items);
    context.fillStyle = gray(CHROME);
    context.font = `10px ${HUD_FONT}`;
    this.fitText(context, detail.metadata, 36, 87, 504);
    context.fillStyle = gray(BRIGHT);
    context.font = `14px ${HUD_FONT}`;
    visibleHudLeadingLines(
      messagesReaderBodyLines(detail.body),
      MESSAGES_READER_BODY_LINE_CAPACITY,
    ).forEach((line, index) => {
      this.fitText(context, line, 36, 119 + index * 17, 504);
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
        const rail = planningMonthSelectionRail(selectedWeek);
        context.fillStyle = gray(BRIGHT);
        context.fillRect(rail.x, rail.y, rail.width, rail.height);
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
    const reviewLayout = confirmationReviewLayout();
    context.textAlign = 'left';
    context.textBaseline = 'middle';
    context.fillStyle = gray(SUPPORT);
    context.font = `13px ${HUD_FONT}`;
    if (frame.reviewDomain) {
      const isCalendar = frame.reviewDomain === 'calendar';
      this.drawIcon(
        context,
        icons[isCalendar ? 'calendar' : 'tasks'],
        reviewLayout.headerIconX,
        51,
        18,
        SUPPORT,
      );
      context.fillText(
        isCalendar ? 'Calendar review' : 'Reminder review',
        reviewLayout.headerTextX,
        reviewLayout.headerBaselineY,
      );
    } else {
      context.fillText(
        'Confirm action',
        reviewLayout.contentX,
        reviewLayout.headerBaselineY,
      );
    }
    context.strokeStyle = gray(CHROME);
    context.beginPath();
    context.moveTo(reviewLayout.contentX, reviewLayout.dividerY);
    context.lineTo(
      reviewLayout.contentX + reviewLayout.contentWidth,
      reviewLayout.dividerY,
    );
    context.stroke();

    context.fillStyle = gray(BRIGHT);
    context.font = `${reviewLayout.promptFontSize}px ${HUD_FONT}`;
    this.drawWrappedText(
      context,
      frame.prompt,
      reviewLayout.contentX,
      reviewLayout.promptTopBaselineY,
      reviewLayout.contentWidth,
      reviewLayout.promptLineHeight,
      reviewLayout.promptMaxLines,
      'leading',
    );

    context.textAlign = 'center';
    context.fillStyle = gray(QUIET);
    context.font = `9px ${HUD_FONT}`;
    this.fitText(
      context,
      confirmationActionHint(),
      288,
      reviewLayout.footerBaselineY,
      500,
    );
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
    const layout = productivityOutcomeLayout();
    this.drawWrappedText(context, model.title, 36, layout.titleBaselineY, 504, 18, 2);

    context.fillStyle = gray(model.phase === 'due' ? SUPPORT : HAIRLINE);
    context.fillRect(36, layout.dividerY, 504, 1);
    context.fillStyle = gray(BRIGHT);
    context.font = `14px ${HUD_FONT}`;
    this.fitText(context, model.primary, 36, layout.primaryBaselineY, 504);
    context.fillStyle = gray(SUPPORT);
    context.font = `10px ${HUD_FONT}`;
    this.drawWrappedText(context, model.secondary, 36, layout.secondaryBaselineY, 504, 14, 2);

    context.fillStyle = gray(QUIET);
    context.font = `9px ${HUD_FONT}`;
    context.textAlign = 'center';
    this.fitText(context, productivityActionHint(model), 288, layout.footerBaselineY, 500);
  }

  private drawBrainstorm(
    context: CanvasRenderingContext2D,
    model: BrainstormHudModel,
    icons: Record<HudIconName, HTMLImageElement>,
  ): void {
    // Dense on purpose. The wearer, watching this render live: "why are we
    // not able to use the full screen? … stop wasting so much place on
    // things that do not affect functionality." The old layout spent four
    // rows and two rules on a title, TOPIC, SEATS and LIVE before the
    // first word of content; the transcript got six rows of a 288px lens.
    // Now: one header row, one rule, and everything else is transcript —
    // SEATS live in the speaker prefixes, LIVE is whoever's row is
    // growing, and the surface's name is its content.
    const state = brainstormStateLabel(model.state);
    context.textAlign = 'left';
    context.textBaseline = 'middle';
    const scroll = this.transcriptScrollFor(model);
    if (model.screen === 'agent_terminal') {
      const layout = agentTerminalTranscriptLayout(model);
      this.drawIcon(context, icons.brainstorm, 36, 52, 14, BRIGHT);
      context.fillStyle = gray(BRIGHT);
      context.font = `11px ${HUD_FONT}`;
      this.fitText(context, layout.summary, 56, 58, 380);
      context.textAlign = 'right';
      context.fillStyle = gray(SUPPORT);
      context.font = `9px ${HUD_FONT}`;
      context.fillText(state, 540, 58);
      context.textAlign = 'left';
      if (!layout.showRecoveryDetails) {
        // Each chip carries its lane's state ("work·run", "calendar·ask1")
        // so one glance covers every concurrent session.
        context.fillStyle = gray(SUPPORT);
        context.font = `9px ${HUD_FONT}`;
        this.fitText(
          context,
          `LANES > ${model.collaborators.join('  |  ') || 'one session'}`,
          36,
          72,
          504,
        );
      } else {
        context.fillStyle = gray(SUPPORT);
        context.font = `9px ${HUD_FONT}`;
        this.fitText(context, `HOST > ${model.endpointHost || 'Dedicated JOAT session'}`, 36, 72, 504);
        context.fillStyle = gray(SUPPORT);
        this.fitText(context, `POLICY > ${agentTerminalSafetyLabel(model)}`, 36, 88, 504);
        context.fillStyle = gray(CHROME);
        this.fitText(context, `SESSION > ${model.terminalReason || 'Your Hermes and OpenClaw work sessions.'}`, 36, 104, 504);
      }
      context.strokeStyle = gray(HAIRLINE);
      context.beginPath();
      context.moveTo(36, layout.dividerY);
      context.lineTo(540, layout.dividerY);
      context.stroke();
      context.fillStyle = gray(BRIGHT);
      context.font = `11px ${HUD_FONT}`;
      this.drawBrainstormTranscript(
        context,
        model.activity || 'JOAT > Terminal is ready. Speak after a tap.',
        36,
        layout.transcriptBaselineY,
        layout.transcriptLineCapacity,
        scroll,
      );
    } else {
      this.drawIcon(context, icons.brainstorm, 36, 52, 14, BRIGHT);
      context.fillStyle = gray(BRIGHT);
      context.font = `11px ${HUD_FONT}`;
      this.fitText(
        context,
        model.topic || 'Brainstorm — say “JOAT, brainstorm …” to begin.',
        56,
        58,
        360,
      );
      context.textAlign = 'right';
      context.fillStyle = gray(SUPPORT);
      context.font = `9px ${HUD_FONT}`;
      const liveNow = model.state === 'running' && model.activeSpeaker
        ? `${model.activeSpeaker} · ${state}`
        : state;
      this.fitText(context, liveNow, 540, 58, 120);
      context.textAlign = 'left';
      context.strokeStyle = gray(HAIRLINE);
      context.beginPath();
      context.moveTo(36, 66.5);
      context.lineTo(540, 66.5);
      context.stroke();
      context.fillStyle = gray(BRIGHT);
      context.font = `11px ${HUD_FONT}`;
      this.drawBrainstormTranscript(
        context,
        model.activity
          || `SEATS > ${model.collaborators.join(' · ') || 'waiting for collaborators'}`,
        36,
        82,
        13,
        scroll,
      );
    }
    const actionHint = scroll.offset > 0
      ? `▼ ${scroll.offset} newer — swipe down · left x2: ${model.state === 'running' ? 'cancel' : 'menu'}`
      : brainstormActionHint(model);
    context.fillStyle = gray(QUIET);
    context.font = `9px ${HUD_FONT}`;
    context.textAlign = 'center';
    this.fitText(context, actionHint, 288, 267, 500);
  }

  /** Scrollback state for the brainstorm/terminal transcript.

   * Lives on the renderer because the renderer is what knows how prose
   * wraps into rows. Keyed by screen+topic so a new session starts back
   * at the live tail; while the wearer is scrolled up, freshly arriving
   * rows must not drag the view — the offset grows with the content so
   * the words under the wearer's eyes stay put. */
  private transcriptScroll = { key: '', offset: 0, rows: 0, visible: 13 };

  private transcriptScrollFor(
    model: BrainstormHudModel,
  ): { offset: number; rows: number; visible: number } {
    const key = `${model.screen}|${model.topic}`;
    if (key !== this.transcriptScroll.key) {
      this.transcriptScroll = { key, offset: 0, rows: 0, visible: this.transcriptScroll.visible };
    }
    return this.transcriptScroll;
  }

  scrollTranscript(direction: 'up' | 'down'): boolean {
    const step = 6; // half a screen per swipe — enough to move, easy to track
    const state = this.transcriptScroll;
    const maximum = Math.max(0, state.rows - state.visible);
    const next = direction === 'up'
      ? Math.min(state.offset + step, maximum)
      : Math.max(0, state.offset - step);
    if (next === state.offset) return false;
    state.offset = next;
    return true;
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
    context.fillText((labels[navigation.view] ?? 'Navigation').toUpperCase(), 36, 59);
    context.fillStyle = gray(BRIGHT);
    context.font = `16px ${HUD_FONT}`;
    this.fitText(context, navigation.primary, 36, 84, 504);
    if (showNavigationSecondaryInHeader(navigation.view)) {
      context.fillStyle = gray(SUPPORT);
      context.font = `11px ${HUD_FONT}`;
      this.fitText(context, navigation.secondary, 36, 106, 504);
    }

    if (navigation.options.length > 0) {
      const rows = navigationDecisionRows(navigation.options.length);
      navigation.options.slice(0, rows.length).forEach((option, index) => {
        const row = rows[index]!;
        const selected = index === navigation.selectedIndex;
        if (selected) {
          context.fillStyle = gray(BRIGHT);
          context.fillRect(36, row.railTopY, 3, row.railHeight);
        }
        context.fillStyle = gray(selected ? BRIGHT : SUPPORT);
        context.font = `${row.fontSize}px ${HUD_FONT}`;
        this.fitText(context, option, 52, row.baselineY, 488);
      });
    } else if (navigation.view === 'error') {
      // A recovery state keeps a small unmistakable mark and its plain
      // instruction. The mark indicates; it is not a poster centerpiece.
      this.drawWarningMark(context, 54, 130, SUPPORT);
      context.fillStyle = gray(SUPPORT);
      context.font = `12px ${HUD_FONT}`;
      this.drawWrappedText(context, navigation.secondary, 94, 122, 446, 16, 2);
    }
    // 'destination' with no options ends at the header: the primary and
    // address ARE the page. The former pin + "CANONICAL PLACE" block only
    // restated them (wearer's ruling: decoration on a text page is waste).
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
    context.font = `16px ${HUD_FONT}`;
    this.fitText(context, navigation.primary, 36, 84, 504);
    // The Mac-authored secondary already carries distance · time · via. The
    // former fake route sketch and DISTANCE/ETA tiles drew the same numbers
    // again around a decorative polyline that never matched the real route.
    context.fillStyle = gray(SUPPORT);
    context.font = `11px ${HUD_FONT}`;
    this.fitText(context, navigation.secondary, 36, 106, 504);
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

    // One divider keeps the maneuver as a stable scan column without turning
    // the active guidance view into a dashboard of outlined cards.
    context.fillStyle = gray(HAIRLINE);
    context.fillRect(128, 76, 1, 108);
    this.drawManeuver(context, navigation.maneuver ?? 'continue', 82, 118, BRIGHT);
    context.fillStyle = gray(BRIGHT);
    context.font = `16px ${HUD_FONT}`;
    context.textAlign = 'center';
    context.fillText(formatNavigationDistance(navigation.maneuverDistanceM ?? 0), 82, 164);

    context.textAlign = 'left';
    context.fillStyle = gray(BRIGHT);
    context.font = `25px ${HUD_FONT}`;
    this.fitText(context, navigation.primary, 150, 95, 390);
    context.fillStyle = gray(SUPPORT);
    context.font = `16px ${HUD_FONT}`;
    this.drawWrappedText(context, navigation.secondary, 150, 128, 390, 21, 2);
    context.fillStyle = gray(CHROME);
    context.font = `10px ${HUD_FONT}`;
    this.fitText(context, navigation.hint, 150, 190, 390);

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
    context.textAlign = 'left';
    context.textBaseline = 'middle';
    context.fillStyle = gray(CHROME);
    context.font = `10px ${HUD_FONT}`;
    context.fillText('ARRIVED', 36, 59);
    // A small check aids recognition; the former 25px poster centerpiece
    // spent the page on it.
    this.drawArrivalMark(context, 47, 84, BRIGHT, 11);
    context.fillStyle = gray(BRIGHT);
    context.font = `16px ${HUD_FONT}`;
    this.fitText(context, navigation.primary, 68, 84, 472);
    context.fillStyle = gray(SUPPORT);
    context.font = `11px ${HUD_FONT}`;
    this.fitText(context, navigation.secondary, 36, 108, 504);
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
    radius = 25,
  ): void {
    context.strokeStyle = gray(level);
    context.lineWidth = 2;
    context.beginPath();
    context.arc(x, y, radius, 0, Math.PI * 2);
    context.stroke();
    context.beginPath();
    context.moveTo(x - radius * 0.48, y);
    context.lineTo(x - radius * 0.12, y + radius * 0.36);
    context.lineTo(x + radius * 0.56, y - radius * 0.44);
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
    retention: 'tail' | 'leading' = 'tail',
  ): void {
    const lines = wrapHudText(text, maxWidth, (value) => context.measureText(value).width);
    const visible = retention === 'leading'
      ? visibleHudLeadingLines(lines, maximumLines)
      : visibleHudTailLines(lines, maximumLines);
    visible.forEach((value, index) => {
      this.fitText(context, value, x, y + index * lineHeight, maxWidth);
    });
  }

  /** A dense terminal tail is the primary content surface for Brainstorm.
   * There are no message cards: only bounded, speaker-prefixed text. */
  private drawBrainstormTranscript(
    context: CanvasRenderingContext2D,
    transcript: string,
    x: number,
    y: number,
    maximumLines: number,
    scroll: { offset: number; rows: number; visible: number },
  ): void {
    const measure = (value: string) => context.measureText(value).width;
    const total = brainstormTranscriptRowCount(transcript, 504, measure);
    if (scroll.offset > 0 && total > scroll.rows) {
      // Content grew while the wearer was reading history: keep their
      // place, not the distance-from-tail.
      scroll.offset = Math.min(
        scroll.offset + (total - scroll.rows),
        Math.max(0, total - maximumLines),
      );
    }
    scroll.rows = total;
    scroll.visible = maximumLines;
    const lines = brainstormTranscriptLines(
      transcript, 504, maximumLines, measure, scroll.offset,
    );
    lines.forEach((line, index) => {
      this.fitText(context, line, x, y + index * 14, 504);
    });
  }

  private drawAssistantOverlayText(
    context: CanvasRenderingContext2D,
    text: string,
  ): void {
    const fitted = fitHudWrappedText(text, 386, 4, (value, fontSize) => {
      context.font = `${fontSize}px ${HUD_FONT}`;
      return context.measureText(value).width;
    });
    context.font = `${fitted.fontSize}px ${HUD_FONT}`;
    const layout = assistantOverlayTextLayout(fitted.lines.length, fitted.fontSize);
    fitted.lines.forEach((line, index) => {
      context.fillText(line, 288, layout.firstBaselineY + index * layout.lineHeight);
    });
  }
}
