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
import { parseHudListItem, resolveHudOverview } from './HudCanvasRenderer';
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
 *   y=31   h=112  info     4 lines | labels 8 lines | pane 8 lines
 *   y=143  h=112  tabs     4 lines |   x=158..254   |  x=254..576   -> 255
 *   y=257  h=31   hint     1 line, full width                       -> 288
 *
 * plus a 2 px bordered rule in the 8 px gutter at x=153. The pane is a
 * grid of two containers, a label column and a value column, the way
 * weather-even-g2 lays out its stats (feels · wind · humid beside their
 * values): the font is proportional, so columns can only line up as two
 * containers at fixed x, never as padded spaces in one. The pane has no
 * title line of its own: the rail's `>` already names the tab, so all eight
 * rows carry content, and a swipe changes the bands that differ, each a
 * flicker-free upgrade instead of a rebuild.
 */
export const OVERVIEW_TEXT_GEOMETRY = {
  header: { x: 0, y: 0, width: CANVAS.width, height: band(1) },
  info: { x: 0, y: band(1), width: 150, height: band(4) },
  tabs: { x: 0, y: band(1) + band(4), width: 150, height: band(4) },
  paneLabels: { x: 158, y: band(1), width: 96, height: band(8) },
  pane: { x: 254, y: band(1), width: CANVAS.width - 254, height: band(8) },
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
export const PANE_TEXT_WIDTH = bandBox(OVERVIEW_TEXT_GEOMETRY.pane).innerWidth; // 318

/** One grid row: the label column's word and the value column's text. */
export type PaneRow = readonly [label: string, value: string];

export interface OverviewTextView {
  /** The same live header line every firmware page carries. */
  header: string;
  /** Left column, top: date, weather, unread. */
  info: string[];
  tabs: { label: string; active: boolean }[];
  /** Right pane, the grid's label column, one entry per row ('' for none). */
  paneLabels: string[];
  /** Right pane, the grid's value column, one line per row. */
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

/** The Pomodoro card as grid rows: the block, minutes left, a bar of the
 * phase, the cycle, then the tab's action rows. */
export function pomodoroPaneGrid(
  pomodoro: HudPanelPomodoro | undefined, content: PanelContent | undefined, nowMs: number,
): PaneRow[] {
  if (content?.empty) return [['', content.headline ?? 'Timer unavailable'], ['', content.empty]];
  if (!pomodoro) return [['Timer', 'Focus timer'], ['', 'Say “JOAT, start a pomodoro”']];
  if (pomodoro.phase === 'idle') {
    return [['Timer', pomodoroBlockLabel(pomodoro)], ['', 'tap: start'], ['Cycle', pomodoroCycleDots(pomodoro.completed)]];
  }
  const remaining = pomodoroRemainingMs(pomodoro, nowMs);
  const done = pomodoro.lengthMs > 0 ? 1 - remaining / pomodoro.lengthMs : 0;
  return [
    ['Timer', pomodoroBlockLabel(pomodoro)],
    ['Left', `${formatMinutesLeft(remaining)}${pomodoro.running ? '' : ' · paused'}`],
    ['', progressBar(done, PANE_TEXT_WIDTH)],
    ['Cycle', pomodoroCycleDots(pomodoro.completed)],
    ...(content?.rows ?? []).map((row): PaneRow => ['', row]),
  ];
}

/** The Pomodoro card's value column alone. */
export function pomodoroPaneRows(
  pomodoro: HudPanelPomodoro | undefined, content: PanelContent | undefined, nowMs: number,
): string[] {
  return pomodoroPaneGrid(pomodoro, content, nowMs).map(([, value]) => value);
}

/** The resolver's kind words, in the label column's 92 px: measured with
 * pretext, NOTIFICATION (116) and LAST QUERY (108) do not fit; the rest do. */
function gridLabel(label: string): string {
  if (label === 'NOTIFICATION') return 'NOTICE';
  if (label === 'LAST QUERY') return 'QUERY';
  return label;
}

/** A Mac row as a grid row: "[12m] Headline · BBC" puts its age in the
 * label column; a row without one keeps the label column empty. */
function listRow(row: string): PaneRow {
  const parts = parseHudListItem(row, true);
  return [parts.age, parts.primary];
}

/** A "Source · rest" line (the tray's "Calendar · 8m · moved") puts its
 * source in the label column. */
function sourceRow(line: string): PaneRow {
  const marker = line.indexOf(' · ');
  if (marker < 0) return ['', line.trim()];
  return [line.slice(0, marker).trim(), line.slice(marker + 3).trim()];
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

/** An unread notification as the Notifications tab lists it. */
export interface OverviewUnreadCard {
  app: string;
  sender: string;
  body: string;
}

export interface OverviewTextOptions {
  /** Whether Main's content (a reply, a calendar line) heads the pane.
   * Home pins it until the wearer swipes; after a swipe the pane follows
   * the rail, so a swipe always shows the tab it lands on. */
  pinMain?: boolean;
  /** Unread notifications, newest first: the Notifications tab's rows. */
  unread?: readonly OverviewUnreadCard[];
}

/**
 * What Home shows, from the same frame the image overview drew and with
 * the same priorities (resolveHudOverview): a link state first, then a
 * reply or a calendar line (until the wearer swipes), then the lit tab's
 * card. A notification never heads the pane: its card over the page showed
 * it, and the Notifications tab lists it while it is unread.
 */
export function composeOverviewText(
  frame: Extract<HudVisualFrame, { kind: 'overview' }>,
  header: string,
  now: Date = new Date(),
  options: OverviewTextOptions = {},
): OverviewTextView {
  const overview = resolveHudOverview(
    frame.mainKind === 'notification' ? { ...frame, main: '' } : frame,
  );
  const unreadCards = options.unread ?? [];
  const rail = frame.rail;
  const tabs = PANEL_TABS.map((tab) => ({ label: PANEL_TAB_TITLES[tab], active: rail?.activeTab === tab }));
  const weather = frame.ambient
    ? `${frame.ambient.temperature} ${frame.ambient.condition}`.trim()
    : '';
  const unread = frame.ambient?.unread ?? 0;
  const info = [overviewDate(now), weather, unread > 0 ? `${unread} unread` : ''].filter(Boolean);
  const content = rail?.content;
  // Each tab's resting card, with the image overview's own empty copy.
  const tabRows = (): PaneRow[] => {
    switch (rail?.activeTab) {
      case 'pomodoro':
        return content?.pomodoro || content?.empty
          ? pomodoroPaneGrid(content?.pomodoro, content, now.getTime())
          : [['Timer', 'Focus timer'], ['', 'tap: open the tab to start']];
      case 'news':
        if (!content) return [['', 'Waiting for JOAT on your Mac…']];
        if (content.rows.length === 0) {
          return [['', content.empty ?? 'No headlines yet.'], ['', 'tap: open · long tap: refresh']];
        }
        return [
          ...(content.headline ? [['', content.headline] as PaneRow] : []),
          ...content.rows.map(listRow),
        ];
      case 'quicknotes':
        // Even's QuickNote widget: the newest notes, age in the label
        // column; a tap opens QuickNotes ("+ Create new note" leads there).
        if (!content) return [['', 'Waiting for JOAT on your Mac…']];
        if (content.rows.length === 0) return [['', content.empty ?? 'No notes yet.'], ['', 'tap: open']];
        return content.rows.map(listRow);
      default:
        // Notifications: the unread cards, newest first, the app in the
        // label column; a tap opens the newest as its card.
        if (unreadCards.length > 0) {
          return [
            ...unreadCards.slice(0, PANE_LINES - 1).map((card): PaneRow => {
              const body = card.body.replace(/\s+/gu, ' ').trim();
              return [card.app.trim(), card.sender.trim() ? `${card.sender.trim()}: ${body}` : body];
            }),
            ['', 'tap: read'],
          ];
        }
        // Nothing unread: the pane says what Home is for.
        return overview.kind === 'empty'
          ? [['', overview.primary], ['', 'tap: open notifications']]
          : [['', 'No new notifications'], ['', 'tap: open notifications']];
    }
  };
  const grid = (rows: PaneRow[]): { paneLabels: string[]; pane: string[] } => ({
    paneLabels: rows.map(([label]) => label),
    pane: rows.map(([, value]) => value),
  });
  const boot = overview.kind === 'empty' && overview.title !== 'Ready';
  if (boot) {
    return {
      header, info, tabs,
      // A boot title (RECONNECTING, 130 px) is wider than the label column:
      // it heads the value column instead, as it did before the grid.
      ...grid([['', overview.title.toUpperCase()], ['', overview.primary], ['', overview.recovery]]),
      paneBrightness: frame.mainBrightness,
      hint: overview.hint,
    };
  }
  if (overview.kind === 'content' && overview.main && options.pinMain !== false) {
    return {
      header, info, tabs,
      ...grid([
        [gridLabel(overview.main.label), overview.main.value],
        ...(overview.tray ? overview.tray.split('\n').map(sourceRow) : []),
      ]),
      paneBrightness: frame.mainBrightness,
      hint: frame.hint,
    };
  }
  return {
    header,
    info,
    tabs,
    ...grid(tabRows()),
    paneBrightness: 4,
    // Home's own gesture line (Chrome's), else the resolver's.
    hint: frame.hint.trim() || (overview.kind === 'empty' ? overview.hint : ''),
  };
}
