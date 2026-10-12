/**
 * The overview's right panel: a vertical rail of tabs, plugin-owned.
 *
 * The rail is the wearer's model from the platform's own dashboard — tabs
 * on the outer-left edge of the pane, swipe to move between them, click to
 * drill in. Which tab is active is purely local state: the Mac never learns
 * it, and a swipe never costs a round trip. What the Mac owns is the CONTENT
 * behind news, pomodoro and quicknotes (and Search, which the menu opens),
 * pushed as `hud.panel` and cached here so a swipe onto a tab paints from
 * memory. A tap on QuickNotes opens QuickNotes itself. Notifications, the first tab, has
 * no cache of its own: it is the overview's Main/Context/Tray streams, the
 * resting screen JOAT already drew.
 *
 * Every push carries a `revision`. A row tap echoes it (`panelRequestId`),
 * so a tap that lands after a refresh is refused by the Mac rather than
 * opening whatever now sits at that row.
 */

import {
  PANEL_CONTENT_TABS,
  PANEL_TABS,
  panelRequestId,
  type DownlinkMessage,
  type HudPanelPomodoro,
  type PanelContentTab,
  type PanelTab,
  type PomodoroPhase,
} from './types';

export type HudPanelMessage = Extract<DownlinkMessage, { kind: 'hud.panel' }>;

/** One tab's content as the Mac last pushed it. */
export interface PanelContent {
  tab: PanelContentTab;
  revision: number;
  rows: string[];
  headline?: string;
  empty?: string;
  pomodoro?: HudPanelPomodoro;
  /** Plugin clock at receipt, for the "updated Nm ago" freshness line. */
  receivedAtMs: number;
}

/** What the renderer needs to draw the rail and the active tab's pane. */
export interface PanelRailVisual {
  /** A rail tab, or Search's own page (opened from the menu). */
  activeTab: PanelTab | PanelContentTab;
  /** The active tab's cached content; absent for Notifications, or before
   * the Mac has pushed anything for that tab. */
  content?: PanelContent;
}

/** The focus-page title each tab expands into. The Mac answers a row tap
 * with a `hud.list` whose title extends this ("NEWS · ARTICLE"), which is
 * how CapabilityMenu's existing two-deep Back knows the parent. */
export const PANEL_TAB_TITLES: Readonly<Record<PanelTab | PanelContentTab, string>> = {
  notifications: 'NOTIFICATIONS',
  news: 'NEWS',
  pomodoro: 'POMODORO',
  quicknotes: 'QUICKNOTES',
  search: 'SEARCH',
};

/** The tabs that expand into a panel page of their own. QuickNotes does not:
 * its tap opens the QuickNotes capability, whose pages it owns. */
const PAGE_TABS = ['news', 'pomodoro', 'search'] as const;
export type PanelPageTab = (typeof PAGE_TABS)[number];

/** "NEWS" or "NEWS · ARTICLE" → 'news'; anything else → undefined. */
export function panelTabForTitle(title: string | undefined): PanelPageTab | undefined {
  const family = (title ?? '').split('·')[0]?.trim().toUpperCase();
  return PAGE_TABS.find((tab) => PANEL_TAB_TITLES[tab] === family);
}

export function isPanelContentTab(value: unknown): value is PanelContentTab {
  return (PANEL_CONTENT_TABS as readonly unknown[]).includes(value);
}

function isPanelTab(value: unknown): value is PanelTab {
  return (PANEL_TABS as readonly unknown[]).includes(value);
}

/** Time left on the timer at `nowMs`, on the plugin's own clock. */
export function pomodoroRemainingMs(pomodoro: HudPanelPomodoro, nowMs: number): number {
  if (pomodoro.running && pomodoro.endsAtUnixMs !== undefined) {
    return Math.max(0, pomodoro.endsAtUnixMs - nowMs);
  }
  return Math.max(0, pomodoro.remainingMs);
}

/** "24 min left" — the overview's minute-granular line. A running timer
 * rounds UP so it never reads "0 min left" while seconds remain. */
export function formatMinutesLeft(ms: number): string {
  const minutes = Math.max(0, Math.ceil(ms / 60_000));
  return `${minutes} min left`;
}

export function pomodoroPhaseLabel(phase: PomodoroPhase): string {
  switch (phase) {
    case 'focus': return 'Focus';
    case 'break': return 'Break';
    case 'long_break': return 'Long break';
    default: return 'Ready';
  }
}

/** "Focus · block 2 of 4", "Break · 1 of 4 done", "Ready · 25 min focus";
 * a paused timer says so first. One string for both the resting pane and
 * the expanded page, so the two can never disagree about where you are. */
export function pomodoroBlockLabel(pomodoro: HudPanelPomodoro): string {
  if (pomodoro.phase === 'idle') {
    return `Ready · ${Math.round(pomodoro.lengthMs / 60_000)} min focus`;
  }
  const block = pomodoro.phase === 'focus'
    ? `Focus · block ${Math.min(4, pomodoro.completed + 1)} of 4`
    : `${pomodoroPhaseLabel(pomodoro.phase)} · ${pomodoro.completed} of 4 done`;
  return pomodoro.running ? block : `Paused · ${block}`;
}

/** "● ● ○ ○" — the set of four, from the firmware-safe glyph set. */
export function pomodoroCycleDots(completed: number): string {
  return Array.from({ length: 4 }, (_, index) => (index < completed ? '●' : '○')).join(' ');
}

/** Milliseconds until the countdown's next visible change at the given
 * granularity, so the local ticker repaints exactly when a digit moves. */
export function pomodoroTickDelayMs(
  pomodoro: HudPanelPomodoro,
  nowMs: number,
  granularityMs: 1000 | 60_000,
): number | undefined {
  if (!pomodoro.running || pomodoro.endsAtUnixMs === undefined) return undefined;
  const remaining = pomodoro.endsAtUnixMs - nowMs;
  if (remaining <= 0) return undefined;
  const untilBoundary = remaining % granularityMs;
  return untilBoundary === 0 ? granularityMs : untilBoundary;
}

/** "updated 3m ago" — feed freshness from the plugin's own receipt time. */
export function freshnessLabel(receivedAtMs: number, nowMs: number): string {
  const elapsed = Math.max(0, nowMs - receivedAtMs);
  if (elapsed < 60_000) return 'updated now';
  if (elapsed < 3_600_000) return `updated ${Math.floor(elapsed / 60_000)}m ago`;
  return `updated ${Math.floor(elapsed / 3_600_000)}h ago`;
}

export class PanelRail {
  private activeTab: PanelTab = 'notifications';
  private readonly contents = new Map<PanelContentTab, PanelContent>();

  active(): PanelTab {
    return this.activeTab;
  }

  /** Returns true when the active tab changed. */
  setActive(tab: PanelTab): boolean {
    if (this.activeTab === tab) return false;
    this.activeTab = tab;
    return true;
  }

  /** Move one tab along the rail. Clamped at both ends, deliberately: a
   * rail that wraps makes "swipe down once" ambiguous at the edges. */
  step(delta: number): PanelTab | undefined {
    const index = PANEL_TABS.indexOf(this.activeTab);
    const next = PANEL_TABS[Math.max(0, Math.min(PANEL_TABS.length - 1, index + Math.sign(delta)))];
    if (next === undefined || next === this.activeTab) return undefined;
    this.activeTab = next;
    return next;
  }

  /** The resting screen's default. A fresh brain connection lands here. */
  reset(): void {
    this.activeTab = 'notifications';
  }

  content(tab: PanelContentTab): PanelContent | undefined {
    return this.contents.get(tab);
  }

  /** Adopt a push. The Mac is authoritative, so a lower revision (a brain
   * that restarted and counts from zero again) replaces the cache too. */
  accept(message: HudPanelMessage, nowMs = Date.now()): PanelContent {
    const content: PanelContent = {
      tab: message.tab,
      revision: message.revision,
      rows: [...message.rows],
      ...(message.headline !== undefined ? { headline: message.headline } : {}),
      ...(message.empty !== undefined ? { empty: message.empty } : {}),
      ...(message.pomodoro ? { pomodoro: { ...message.pomodoro } } : {}),
      receivedAtMs: nowMs,
    };
    this.contents.set(message.tab, content);
    return content;
  }

  /** The id a row tap on this tab must carry. */
  requestId(tab: PanelContentTab): string {
    return panelRequestId(tab, this.contents.get(tab)?.revision ?? 0);
  }

  visual(): PanelRailVisual {
    return this.visualFor(this.activeTab);
  }

  /** The rail as it looks with `tab` highlighted — an expanded page keeps
   * its own tab lit whatever the overview's active tab is. */
  visualFor(tab: PanelTab | PanelContentTab): PanelRailVisual {
    const content = tab === 'notifications' ? undefined : this.contents.get(tab);
    return content ? { activeTab: tab, content: cloneContent(content) } : { activeTab: tab };
  }

  /** JSON-safe, for the host's background migration. */
  export(): Record<string, unknown> {
    return {
      activeTab: this.activeTab,
      contents: Object.fromEntries(
        [...this.contents.entries()].map(([tab, content]) => [tab, cloneContent(content)]),
      ),
    };
  }

  /** Adopt an exported slice. Malformed entries are skipped, never trusted. */
  import(saved: Record<string, unknown>): boolean {
    if (!isPanelTab(saved.activeTab)) return false;
    this.activeTab = saved.activeTab;
    const contents = saved.contents;
    if (typeof contents === 'object' && contents !== null) {
      for (const [tab, value] of Object.entries(contents as Record<string, unknown>)) {
        if (!isPanelContentTab(tab)) continue;
        const restored = restoreContent(tab, value);
        if (restored) this.contents.set(tab, restored);
      }
    }
    return true;
  }
}

function cloneContent(content: PanelContent): PanelContent {
  return {
    ...content,
    rows: [...content.rows],
    ...(content.pomodoro ? { pomodoro: { ...content.pomodoro } } : {}),
  };
}

function restoreContent(tab: PanelContentTab, value: unknown): PanelContent | undefined {
  if (typeof value !== 'object' || value === null) return undefined;
  const record = value as Record<string, unknown>;
  if (typeof record.revision !== 'number' || !Array.isArray(record.rows)) return undefined;
  const rows = record.rows.filter((row): row is string => typeof row === 'string');
  const content: PanelContent = {
    tab,
    revision: record.revision,
    rows,
    receivedAtMs: typeof record.receivedAtMs === 'number' ? record.receivedAtMs : 0,
  };
  if (typeof record.headline === 'string') content.headline = record.headline;
  if (typeof record.empty === 'string') content.empty = record.empty;
  const pomodoro = record.pomodoro;
  if (typeof pomodoro === 'object' && pomodoro !== null) {
    const p = pomodoro as Record<string, unknown>;
    if (
      (p.phase === 'idle' || p.phase === 'focus' || p.phase === 'break' || p.phase === 'long_break')
      && typeof p.running === 'boolean'
      && typeof p.remainingMs === 'number'
      && typeof p.lengthMs === 'number'
      && typeof p.completed === 'number'
    ) {
      content.pomodoro = {
        phase: p.phase,
        running: p.running,
        remainingMs: p.remainingMs,
        lengthMs: p.lengthMs,
        completed: p.completed,
        ...(typeof p.endsAtUnixMs === 'number' ? { endsAtUnixMs: p.endsAtUnixMs } : {}),
      };
    }
  }
  return content;
}
