import { TextContainerUpgrade, type RebuildPageContainer } from '@evenrealities/even_hub_sdk';
import { GesturePolicy } from './GesturePolicy';
import { PANEL_TAB_TITLES } from './PanelRail';
import { PANEL_TABS } from './types';

/** Render the overview as firmware text rather than four image tiles.
 *
 * Measured with the [host-call] probe against the real brain, every reading
 * checked against a pixel diff. Canvas overview vs this one:
 *
 *   swipe between tabs   208ms -> 165ms
 *   expand a tab         416ms -> 165ms   (with the firmware-list tabs)
 *   leave a tab          416ms -> 330ms
 *   the walk above      2037ms -> 825ms
 *
 * updateImageRawData does not appear at all: the overview stops being four
 * 288x144 quadrant tiles at ~104ms each. Even's guidance is the basis —
 * "Text updates are much faster than image updates: use text for anything
 * that needs to feel instant" (everything-evenhub skills/glasses-ui).
 *
 * ON by default; VITE_NATIVE_OVERVIEW=0 falls back to the canvas overview,
 * which stays in the product and stays tested as the fallback. */
let nativeOverviewEnabled = String(import.meta.env.VITE_NATIVE_OVERVIEW ?? '1') !== '0';

/** Tests that pin the CANVAS overview call this to select the fallback the
 * flag still ships; the text path has its own tests. Never product code. */
export function setNativeOverviewForTest(on: boolean): void {
  nativeOverviewEnabled = on;
}

/** The Brainstorm on firmware text (the Agent Terminal's two-container
 * native document) instead of four image tiles. A streamed token is one
 * textContainerUpgrade (~80ms) where a tile write was 0.5-2s over BLE, and
 * the local "Thinking… 12s" counter costs one text write per second.
 *
 * ON by default; VITE_NATIVE_BRAINSTORM=0 restores the canvas page. */
let nativeBrainstormEnabled = String(import.meta.env.VITE_NATIVE_BRAINSTORM ?? '1') !== '0';

/** Tests that pin the CANVAS Brainstorm select the fallback here. */
export function setNativeBrainstormForTest(on: boolean): void {
  nativeBrainstormEnabled = on;
}

/** Whether this frame draws as the native text document. */
function nativeDocument(model: { screen: string }): boolean {
  return model.screen === 'agent_terminal'
    || (model.screen === 'brainstorm' && nativeBrainstormEnabled);
}

function parseOverviewStatus(status: string): { time: string; date: string } {
  return {
    time: /\b(\d{1,2}:\d{2})\b/.exec(status)?.[1] ?? '',
    // The chrome's status text usually carries no date ("9:52 · MIC OFF ·
    // bat 100%"), so the phone's own calendar date fills it: the plugin
    // runs on the wearer's phone, in the wearer's zone.
    date: /\b([A-Z][a-z]{2}\s+\d{1,2}\/\d{1,2})\b/.exec(status)?.[1] ?? overviewDate(new Date()),
  };
}

function overviewDate(now: Date): string {
  try {
    return now.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
  } catch {
    return '';
  }
}
import type { BridgeClient } from './BridgeClient';
import type { HudPageBuilder } from './HudPageBuilder';
import { ASSISTANT_ANSWER_GEOMETRY } from './HudPageBuilder';
import { paginate, pagerLabel } from './AssistantAnswerPager';
import type {
  AssistantOverlayPresentation,
  AssistantOverlayVisual,
  HudFramePainter,
  HudVisualFrame,
} from './HudVisualSurface';
import type { G2DiagnosticTrace, G2TracePage } from './G2DiagnosticTrace';
import {
  AssistantConversationSurface,
  validAssistantConversationSnapshot,
  type ConversationExitResult,
} from './AssistantConversationSurface';
import { NativeTerminalSurface } from './NativeTerminalSurface';
import { resolveHudOverview, usesActionPillLayout } from './HudCanvasRenderer';
import { TEXT_CONTAINER_MAX_BYTES, fitUtf8, fitUtf8Tail } from './FirmwareLimits';
import type { NavigationHudModel } from './NavigationHud';
import {
  PanelRail,
  formatCountdown,
  formatMinutesLeft,
  pomodoroBlockLabel,
  pomodoroCycleDots,
  pomodoroRemainingMs,
  pomodoroTickDelayMs,
  type HudPanelMessage,
  type PanelContent,
} from './PanelRail';
import {
  CONFIRM_PROMPT_MAX_UNITS,
  ContainerId,
  type BrainstormHudModel,
  type AssistantConversationSnapshot,
  type HudContentKind,
  type PanelContentTab,
  type PanelTab,
  type PlanningGridCell,
  type ProductivityHudDomain,
  type ProductivityHudModel,
} from './types';

// A text container holds at most 999 UTF-8 bytes (FirmwareLimits); an
// upgrade beyond that is rejected whole, and the 2000-char cap here used to
// let one through.
const MAX_UPGRADE_BYTES = TEXT_CONTAINER_MAX_BYTES;

/**
 * Repaint throttle for streamed text. Tokens arrive far faster than BLE can
 * move frames to the glasses; painting per token floods the link and, on
 * real hardware, rendered as flashing fragments ("cutting out, one line").
 * ~3 paints/second reads as live streaming while keeping BLE sane.
 */
const PAINT_INTERVAL_MS = 300;

const DEFAULT_LIST_TITLE = 'JOAT';
const DEFAULT_LIST_HINT = 'left x2 overview';
const MAX_FOCUS_COPY_CHARS = 46;
// The compact image footer supports more copy than the title. Preserve all
// controls through normalization; Python HudList uses the same bound.
const MAX_FOCUS_HINT_CHARS = 96;

/** The downlink is a WebSocket boundary, so treat its optional visual
 * metadata as untrusted even though TypeScript says it is well shaped. A bad
 * or partial grid must fall back to the ordinary selectable list. */
/** Rebuild a container-keyed Map from a JSON record, dropping bad entries.
 * A background snapshot crosses a JSON boundary and a process boundary, so it
 * is untrusted input, not a value this page wrote. */
function numericMap<T>(
  raw: unknown,
  valid: (value: unknown) => value is T,
): Map<number, T> {
  const map = new Map<number, T>();
  if (typeof raw !== 'object' || raw === null) return map;
  for (const [key, value] of Object.entries(raw)) {
    const id = Number(key);
    if (Number.isInteger(id) && valid(value)) map.set(id, value);
  }
  return map;
}

function normalizePlanningEntries(
  entries: PlanningGridCell['entries'],
): PlanningGridCell['entries'] {
  if (!Array.isArray(entries)) return undefined;
  const kept = entries
    .filter((entry) => (
      typeof entry === 'object' && entry !== null
      && typeof entry.time === 'string' && entry.time.length <= 8
      && (entry.kind === 'event' || entry.kind === 'reminder')
      && typeof entry.title === 'string' && entry.title.length > 0
    ))
    .slice(0, 6)
    .map((entry) => ({
      time: entry.time,
      kind: entry.kind,
      title: entry.title.slice(0, 64),
    }));
  return kept.length > 0 ? kept : undefined;
}

function normalizePlanningGrid(
  planningGrid: readonly PlanningGridCell[] | undefined,
): PlanningGridCell[] | undefined {
  if (!planningGrid || planningGrid.length !== 42) return undefined;
  const normalized = planningGrid.map((cell) => {
    const entries = normalizePlanningEntries(cell.entries);
    return {
      day: cell.day,
      eventCount: cell.eventCount,
      reminderCount: cell.reminderCount,
      inMonth: cell.inMonth,
      ...(entries ? { entries } : {}),
    };
  });
  const isValid = normalized.every((cell) => (
    typeof cell.day === 'string'
    && /^\d{4}-\d{2}-\d{2}$/.test(cell.day)
    && Number.isInteger(cell.eventCount)
    && cell.eventCount >= 0
    && cell.eventCount <= 99
    && Number.isInteger(cell.reminderCount)
    && cell.reminderCount >= 0
    && cell.reminderCount <= 99
    && typeof cell.inMonth === 'boolean'
  ));
  return isValid ? normalized : undefined;
}

const DEFAULT_BRIGHTNESS: Readonly<Record<number, number>> = {
  [ContainerId.StatusBar]: 2,
  [ContainerId.Main]: 4,
  [ContainerId.Context]: 3,
  [ContainerId.Tray]: 2,
  [ContainerId.Hint]: 1,
};

/** Plugin-local dashboard focus: which date is hovered, whether the right
 * pane owns input, and which of its rows is hovered. Never crosses the wire —
 * a hover must not cost a Mac round trip on the serialized lane. */
export interface PlanningPaneState {
  dateIndex: number;
  focused: boolean;
  itemIndex: number;
}

export interface ListSurfaceOptions {
  /** A matching terminal Planning refresh keeps its independently expiring receipt. */
  retainProductivityReceipt?: boolean;
  title?: string;
  contextLabel?: string;
  controlStartIndex?: number;
  planningEntries?: Array<{ time: string; kind: "event" | "reminder"; title: string } | null>;
  hint?: string;
  selectable?: boolean;
  /** Plugin-local initial cursor, applied in the first custom frame only. */
  initialSelectedIndex?: number;
  planningGrid?: readonly PlanningGridCell[];
  planningPane?: PlanningPaneState;
  /** This list is an expanded panel tab: keep its rail lit on the left. */
  railTab?: PanelContentTab;
  /** Opt into the firmware-owned list and its native gesture cursor. */
  nativeFirmwareList?: boolean;
  /** With nativeFirmwareList: this "list" is prose (an opened article or
   * note) and renders as a firmware TEXT page — head line, scrolling body,
   * hint — instead of rows. Even's own text-heavy pattern; no image tiles. */
  nativeReaderPage?: boolean;
}

interface ResolvedListSurfaceOptions {
  title: string;
  contextLabel?: string;
  controlStartIndex?: number;
  planningEntries?: Array<{ time: string; kind: "event" | "reminder"; title: string } | null>;
  hint: string;
  selectable: boolean;
  planningGrid?: readonly PlanningGridCell[];
  planningPane?: PlanningPaneState;
  railTab?: PanelContentTab;
  /** This page is intentionally rendered by the G2 firmware list. */
  nativeFirmwareList?: boolean;
  nativeReaderPage?: boolean;
}

interface HudContentState {
  notificationId?: string;
  streams: Map<number, string>;
  brightness: Map<number, number>;
  kinds: Map<number, HudContentKind>;
}

interface PresenterSnapshot extends HudContentState {
  surface: HudSurface;
  focusReturn?: HudContentState;
}

type HudSurface =
  | { kind: 'default' }
  | {
    kind: 'list';
    items: string[];
    options: ResolvedListSurfaceOptions;
    selectedIndex: number;
  }
  | { kind: 'navigation'; navigation: NavigationHudModel }
  | { kind: 'brainstorm'; brainstorm: BrainstormHudModel }
  | { kind: 'productivity'; productivity: ProductivityHudModel };

type DisplayPageOwner =
  | 'visual'
  | 'assistant-native'
  | 'terminal-list-native'
  | 'terminal-conversation-native';

interface ConfirmationReturnState extends HudContentState {
  id: string;
  prompt: string;
  diagnosticKind?: 'messages';
  isCurrent?: () => boolean;
  surface: HudSurface;
  onDeferredProductivityVisible?: () => void;
  /** Pill deferred behind an in-flight dismissal; fires once the restore paints. */
  onDeferredPillVisible?: () => void;
}

interface AssistantOverlayState extends AssistantOverlayVisual {
  dismissTimer?: ReturnType<typeof setTimeout>;
}

interface PromptQuestionState {
  terminalConversationId?: string;
  inlineTerminal?: boolean;
  /** Which native document the inline question belongs to. */
  inlineScreen?: 'agent_terminal' | 'brainstorm';
  id: string;
  question: string;
  isCurrent: () => boolean;
  paint?: Promise<boolean>;
}

interface ActionOverlayState {
  productivity: ProductivityHudModel;
  dismissTimer?: ReturnType<typeof setTimeout>;
}

/** Failsafe only — CapabilityMenu owns the real auto-return; this stops a
 * lost frame from leaving the receipt on the glass forever. */
const ACTION_PILL_FAILSAFE_MS = 8_000;

function sameProductivity(a: ProductivityHudModel, b: ProductivityHudModel): boolean {
  return a.domain === b.domain
    && a.phase === b.phase
    && a.title === b.title
    && a.primary === b.primary
    && a.secondary === b.secondary
    && a.hint === b.hint
    && a.planningAnchorUnixMs === b.planningAnchorUnixMs
    && a.monitoringArmed === b.monitoringArmed
    && a.reminderDueId === b.reminderDueId
    && a.reminderDueAttemptId === b.reminderDueAttemptId
    && a.reminderDueGeneration === b.reminderDueGeneration
    && a.presentationReceiptId === b.presentationReceiptId
    && a.presentationReceiptGeneration === b.presentationReceiptGeneration;
}

/** A Home notification temporarily replaces Main, but must never destroy the
 * card it covered. Keep only the local visual tuple; acknowledging or
 * mutating a native notification remains a separate Mac-owned operation. */
interface NotificationReturnState {
  notificationId?: string;
  text?: string;
  brightness?: number;
  kind?: HudContentKind;
}

/**
 * Renders text to the HUD.
 *
 * Streaming is ACCUMULATE-AND-REPAINT, not splice-at-offset: every paint
 * carries the full accumulated text from offset 0, so each frame is
 * self-contained. Offset splicing looked fine in the simulator but real
 * firmware rendered only the latest chunk — and with per-token BLE writes a
 * dropped frame silently corrupted everything after it. Full frames make
 * dropped/reordered updates self-healing.
 */
export class HudPresenter {
  private lastLength = new Map<number, number>();
  private streams = new Map<number, string>();
  private lastPaint = new Map<number, number>();
  private pendingPaint = new Map<number, ReturnType<typeof setTimeout>>();
  private brightness = new Map<number, number>();
  private kinds = new Map<number, HudContentKind>();
  private notificationId?: string;
  private paintedNotificationId?: string;
  private readonly retiredNotificationIds = new Set<string>();
  private surface: HudSurface = { kind: 'default' };
  /** Margin facts (weather, unread) resolved by the Mac for the dashboard. */
  private ambient?: { temperature: string; condition: string; unread: number };
  /** The right pane's tab rail: which tab is active, and what the Mac last
   * pushed for each. Local state — a swipe between tabs never round-trips. */
  readonly rail = new PanelRail();
  /** Repaints the visible pomodoro when its next digit moves. */
  private panelTicker?: ReturnType<typeof setTimeout>;
  private focusReturn?: HudContentState;
  private confirmation?: ConfirmationReturnState;
  private confirmationQueue: Promise<unknown> = Promise.resolve();
  private assistantOverlay?: AssistantOverlayState;
  private assistantOverlayRevision = 0;
  private promptQuestion?: PromptQuestionState;
  private promptQuestionPaint?: Promise<boolean>;
  private actionOverlay?: ActionOverlayState;
  private nativeReplyPage = false;
  private assistantOverlayPaint?: Promise<boolean>;
  private assistantOverlayPaintTimer?: ReturnType<typeof setTimeout>;
  private assistantOverlayPaintDirty = false;
  private assistantOverlayPaintStartedAt = 0;
  private notificationReturn?: NotificationReturnState;
  private micOn = false;
  private displayActive = true;
  private displayRevision = 0;
  private readonly assistantConversation: AssistantConversationSurface;
  private readonly nativeTerminal: NativeTerminalSurface;
  private restoringAssistantConversation = false;
  /** Which set of SDK containers currently exists on the lenses. Content
   * state alone is insufficient: native text and firmware lists use different
   * container IDs from the four-image HUD. */
  private pageOwner: DisplayPageOwner = 'visual';

  constructor(
    private readonly client: BridgeClient,
    private readonly layouts: HudPageBuilder,
    private readonly visual?: HudFramePainter,
    private readonly diagnostics?: G2DiagnosticTrace,
  ) {
    this.assistantConversation = new AssistantConversationSurface(
      client,
      layouts,
      () => this.restoreAfterAssistantConversation(),
      {
        replacePage: (page) => this.replaceNativePage(page, 'assistant-native'),
      },
    );
    this.nativeTerminal = new NativeTerminalSurface(
      client,
      (page) => layouts.nativeTerminalLayout(page),
      (page) => this.replaceNativePage(page, 'terminal-conversation-native'),
    );
  }

  /** Switch from the image HUD to an SDK-native text page under the same
   * serialized display barrier. This is the path Even's own text-heavy and
   * AI surfaces use for responsive content and scroll updates. */
  private async replaceNativePage(
    page: RebuildPageContainer,
    owner: Exclude<DisplayPageOwner, 'visual'>,
  ): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent()) return false;
    // Any other native page replaces the text overview on the glass, so its
    // unchanged-signature shortcut must not survive the transition.
    this.overviewTextLive = false;
    const visual = this.visual;
    const replace = () => this.client.require().rebuildPageContainer(page);
    if (!visual) {
      const replaced = await replace();
      if (replaced && displayCurrent()) this.pageOwner = owner;
      return replaced && displayCurrent();
    }
    if (!visual.withPageTransition) return false;
    try {
      const replaced = await visual.withPageTransition(replace, displayCurrent);
      if (replaced && displayCurrent()) {
        visual.forgetPage?.();
        this.pageOwner = owner;
      }
      return replaced && displayCurrent();
    } catch {
      return false;
    }
  }

  /** Recreate the four image containers at a native-to-visual boundary. */
  private async rebuildVisualPage(): Promise<boolean> {
    this.overviewTextLive = false;
    const visual = this.visual;
    const displayCurrent = this.captureDisplayGuard();
    if (!visual || !displayCurrent()) return false;
    if (this.pageOwner === 'visual') return true;
    if (!visual.withPageTransition) return false;
    try {
      const rebuilt = await visual.withPageTransition(
        () => this.client.require().rebuildPageContainer(this.layouts.visualLayout()),
        displayCurrent,
      );
      if (!rebuilt || !displayCurrent()) return false;
      visual.forgetPage?.();
      this.pageOwner = 'visual';
      return true;
    } catch {
      return false;
    }
  }

  /** Restore the retained visual underlay only after its containers exist. */
  private async restoreVisualPage(): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    // Back from a native page lands HERE, not in paintVisual — the first cut
    // intercepted only paintVisual, so Back rebuilt the empty canvas page and
    // the wearer got a blank screen. The overview must come back as the same
    // text page it left as.
    if (nativeOverviewEnabled && !this.confirmation && !this.promptQuestion) {
      const frame = this.visualFrame();
      if (frame.kind === 'overview' && !frame.actionOverlay && !frame.assistantOverlay) {
        return this.paintOverviewAsText(frame);
      }
    }
    if (!await this.rebuildVisualPage() || !displayCurrent() || !this.visual) return false;
    const painted = this.visual.paintExact
      ? await this.visual.paintExact(this.visualFrame(), displayCurrent)
      : await this.visual.paint(this.visualFrame());
    return painted && displayCurrent();
  }

  /** Restore the retained feature beneath a native assistant conversation.
   * A terminal/list underlay must return to its native page type; painting its
   * canvas projection would reintroduce the four-tile path after Back. */
  private async restoreRetainedPage(): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent()) return false;
    if (this.surface.kind === 'brainstorm'
      && nativeDocument(this.surface.brainstorm)) {
      return this.nativeTerminal.present(this.surface.brainstorm,
        this.streams.get(ContainerId.StatusBar), displayCurrent);
    }
    if (this.surface.kind === 'list' && this.surface.options.nativeFirmwareList) {
      return this.replaceNativePage(
        this.nativeListLayout(this.surface),
        'terminal-list-native',
      );
    }
    return this.restoreVisualPage();
  }

  /** Stop this document's painters, including delayed native fallback work. */
  suspendDisplay(): void {
    if (!this.displayActive) return;
    this.displayActive = false;
    this.displayRevision += 1;
    this.client.suspendDisplay?.();
    this.visual?.suspendDisplay?.();
    this.guardedListPaintRevision += 1;
    if (this.thinkingTicker !== undefined) clearTimeout(this.thinkingTicker);
    this.thinkingTicker = undefined;
    this.thinkingTicks = 0;
    this.cancelAssistantOverlayPaintTimer();
    this.assistantOverlayPaintDirty = false;
    this.assistantOverlayPaint = undefined;
    if (this.assistantOverlay?.dismissTimer !== undefined) clearTimeout(this.assistantOverlay.dismissTimer);
    this.assistantOverlay = undefined;
    this.assistantConversation.retireForHandoff();
    this.nativeTerminal.retireForHandoff();
    this.pageOwner = 'visual';
    this.clearActionOverlay();
    this.promptQuestion = undefined;
    this.promptQuestionPaint = undefined;
    this.confirmation = undefined;
    this.confirmationQueue = Promise.resolve();
    this.healing = false;
    this.notificationReturn = undefined;
    this.focusReturn = undefined;
    this.resetAll();
    this.surface = { kind: 'default' };
  }

  /** Recovery permits fresh work; no paint or timer is replayed here. */
  resumeDisplay(): void {
    this.client.resumeDisplay?.();
    this.visual?.resumeDisplay?.();
    this.displayActive = true;
  }

  /** A firmware list page is still on the glass under a Home surface: the
   * next visual paint must replace it (text overview) or rebuild the image
   * page before painting tiles. */
  private visualPagePending(): boolean {
    return this.pageOwner === 'terminal-list-native'
      && this.surface.kind === 'default' && this.visual !== undefined;
  }

  private captureDisplayGuard(): () => boolean {
    const revision = this.displayRevision;
    const active = this.displayActive;
    const bridgeCurrent = this.client.captureDisplayGuard?.() ?? (() => true);
    return () => active && this.displayActive && revision === this.displayRevision && bridgeCurrent();
  }

  /** The custom list owns its cursor; the compatibility list is firmware-owned. */
  usesCustomListSelection(): boolean {
    return this.visual !== undefined
      && !(this.surface.kind === 'list' && this.surface.options.nativeFirmwareList);
  }

  /** Replace a container's full text. Also resets any stream in progress.
   *
   * `textColor` (firmware brightness 0-4) sticks to the container until the
   * next showText sets or clears it — the Mac computes it per card (urgent
   * bright, routine dim) and the old handler silently dropped the field, so
   * the feature had never worked on glass (observed live). */
  async showText(
    text: string,
    containerId: ContainerId = ContainerId.Main,
    textColor?: number,
    contentKind?: HudContentKind,
    notificationId?: string,
  ): Promise<boolean> {
    if (this.confirmation && containerId !== ContainerId.StatusBar) {
      this.deferText(text, containerId, textColor, contentKind, notificationId);
      return true;
    }
    // A focus page owns the entire non-status canvas. Background replies,
    // transcripts, tray updates, and Chrome's dashboard hint are buffered
    // until Back instead of overwriting its header or targeting containers
    // that intentionally do not exist in this layout.
    if (this.surface.kind !== 'default' && containerId !== ContainerId.StatusBar) {
      this.deferFocusText(text, containerId, textColor, contentKind, notificationId);
      return true;
    }
    return this.replaceText(text, containerId, textColor, contentKind, notificationId);
  }

  private async replaceText(
    text: string,
    containerId: ContainerId,
    textColor?: number,
    contentKind?: HudContentKind,
    notificationId?: string,
  ): Promise<boolean> {
    if (containerId === ContainerId.Main) {
      if (contentKind === 'notification') {
        if (!this.notificationReturn) {
          this.notificationReturn = {
            text: this.streams.get(ContainerId.Main),
            brightness: this.brightness.get(ContainerId.Main),
            kind: this.kinds.get(ContainerId.Main),
            notificationId: this.notificationId,
          };
        }
      } else {
        // A newer normal card is authoritative Home content; never resurrect
        // an older snapshot when the wearer later taps the screen.
        this.notificationReturn = undefined;
      }
    }
    this.cancelPending(containerId);
    this.streams.set(containerId, text);
    if (textColor !== undefined) {
      this.brightness.set(containerId, textColor);
    } else {
      this.brightness.delete(containerId);
    }
    if (containerId === ContainerId.Main) {
      this.kinds.set(containerId, contentKind ?? 'assistant');
      this.notificationId = contentKind === 'notification' && /^[a-f0-9]{32}$/i.test(notificationId ?? '')
        ? notificationId : undefined;
      this.paintedNotificationId = undefined;
    }
    return this.paint(containerId);
  }

  /** Stream a chunk: accumulate, repaint (throttled) with the full text. */
  async appendText(chunk: string, containerId: ContainerId = ContainerId.Main): Promise<boolean> {
    if (this.confirmation && containerId !== ContainerId.StatusBar) {
      if (containerId === ContainerId.Main) {
        this.prepareAssistantAppend(this.confirmation);
      }
      const streams = this.confirmation.streams;
      streams.set(containerId, (streams.get(containerId) ?? '') + chunk);
      return true;
    }
    if (this.surface.kind !== 'default' && containerId !== ContainerId.StatusBar) {
      const deferred = this.ensureFocusReturn();
      if (containerId === ContainerId.Main) this.prepareAssistantAppend(deferred);
      deferred.streams.set(
        containerId,
        (deferred.streams.get(containerId) ?? '') + chunk,
      );
      return true;
    }
    if (containerId === ContainerId.Main) {
      const content = { streams: this.streams, brightness: this.brightness, kinds: this.kinds,
        notificationId: this.notificationId };
      this.prepareAssistantAppend(content);
      this.notificationId = undefined;
      this.paintedNotificationId = undefined;
      this.notificationReturn = undefined;
    }
    this.streams.set(containerId, (this.streams.get(containerId) ?? '') + chunk);
    const now = Date.now();
    const elapsed = now - (this.lastPaint.get(containerId) ?? 0);
    if (elapsed >= PAINT_INTERVAL_MS) {
      return this.paint(containerId);
    }
    if (!this.pendingPaint.has(containerId)) {
      // Trailing paint so the tail of a burst always lands — without it the
      // final tokens of a reply would sit unpainted forever.
      this.pendingPaint.set(containerId, setTimeout(() => {
        this.pendingPaint.delete(containerId);
        void this.paint(containerId);
      }, PAINT_INTERVAL_MS - elapsed));
    }
    return true;
  }

  private prepareAssistantAppend(content: HudContentState): void {
    // An assistant stream starts its own Main content; it must neither
    // append prose to a notification nor inherit that card's dismiss id.
    if (content.kinds.get(ContainerId.Main) === 'notification') {
      content.streams.delete(ContainerId.Main);
      content.brightness.delete(ContainerId.Main);
    }
    content.kinds.set(ContainerId.Main, 'assistant');
    content.notificationId = undefined;
  }

  /** Present one complete question without replacing the feature beneath it.
   * Only this prompt's current, completed frame may acknowledge publication. */
  showPromptQuestion(promptId: string, question: string, isCurrent: () => boolean, ownerKind?: 'messages' | 'brainstorm' | 'terminal'): Promise<boolean> {
    if (this.confirmation || !promptId || !question.trim() || question.length > 1000) {
      return Promise.resolve(false);
    }
    const inlineScreen = ownerKind === 'terminal' ? 'agent_terminal'
      : ownerKind === 'brainstorm' && nativeBrainstormEnabled
        && this.surface.kind === 'brainstorm' && this.surface.brainstorm.screen === 'brainstorm'
        && this.nativeTerminal.isActive() ? 'brainstorm' : undefined;
    if (inlineScreen !== undefined) {
      if (!this.nativeTerminal.isActive() || this.surface.kind !== 'brainstorm'
        || this.surface.brainstorm.screen !== inlineScreen) return Promise.resolve(false);
      const existing = this.promptQuestion;
      if (existing?.id === promptId) {
        return existing.question === question && this.isPromptCurrent(existing)
          ? existing.paint ?? Promise.resolve(false)
          : Promise.resolve(false);
      }
      const prompt: PromptQuestionState = {
        id: promptId,
        question,
        isCurrent,
        inlineTerminal: true,
        inlineScreen,
        terminalConversationId: this.surface.brainstorm.conversationId,
      };
      this.promptQuestion = prompt;
      const paint = this.nativeTerminal.showPrompt(
        promptId,
        question,
        () => this.isPromptCurrent(prompt),
      ).then((painted) => {
        const accepted = painted && this.isPromptCurrent(prompt);
        if (!accepted && this.promptQuestion === prompt) this.promptQuestion = undefined;
        return accepted;
      }).catch(() => {
        if (this.promptQuestion === prompt) this.promptQuestion = undefined;
        return false;
      });
      prompt.paint = paint;
      this.promptQuestionPaint = paint;
      return paint;
    }
    if (!this.visual?.paintExact) return Promise.resolve(false);
    if (this.assistantConversation.isActive()) {
      return this.prepareAssistantConversationHandoff().then((ready) => (
        ready ? this.showPromptQuestion(promptId, question, isCurrent, ownerKind) : false
      ));
    }
    const existing = this.promptQuestion;
    if (existing?.id === promptId) {
      return existing.question === question && this.isPromptCurrent(existing)
        ? existing.paint ?? Promise.resolve(false)
        : Promise.resolve(false);
    }
    const prompt: PromptQuestionState = { id: promptId, question, isCurrent };
    this.promptQuestion = prompt;
    this.cancelAssistantOverlayPaintTimer();
    this.assistantOverlayPaintDirty = false;
    const paint = this.renderPromptQuestion(prompt, this.promptQuestionPaint);
    prompt.paint = paint;
    this.promptQuestionPaint = paint;
    return paint;
  }

  async dismissPromptQuestion(promptId: string): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    const prompt = this.promptQuestion;
    if (!prompt || prompt.id !== promptId) return false;
    this.promptQuestion = undefined;
    if (prompt.inlineTerminal) {
      this.promptQuestionPaint = undefined;
      return this.nativeTerminal.dismissPrompt(promptId);
    }
    // The old frame may already have one bridge write in flight. Its guard
    // prevents further writes; restore only after that owned work settles.
    await prompt.paint?.catch(() => false);
    if (!displayCurrent()) return false;
    if (this.promptQuestion || this.confirmation) return false;
    return this.paintVisual();
  }

  private isPromptCurrent(prompt: PromptQuestionState): boolean {
    try {
      return this.promptQuestion === prompt && !this.confirmation && prompt.isCurrent()
        && (!prompt.inlineTerminal || (this.nativeTerminal.isActive()
          && this.surface.kind === 'brainstorm'
          && this.surface.brainstorm.screen === (prompt.inlineScreen ?? 'agent_terminal')
          && this.surface.brainstorm.conversationId === prompt.terminalConversationId));
    } catch {
      return false;
    }
  }

  private async renderPromptQuestion(prompt: PromptQuestionState, previous?: Promise<boolean>): Promise<boolean> {
    let painted = false;
    try {
      // Serialize prompt layout changes too, and drain an already-dispatched
      // assistant frame before claiming an exact question receipt.
      await previous?.catch(() => false);
      await this.assistantOverlayPaint?.catch(() => false);
      if (!this.isPromptCurrent(prompt) || !this.visual?.paintExact) return false;
      if (!this.isPromptCurrent(prompt)) return false;
      painted = await this.visual.paintExact(this.visualFrame(), () => this.isPromptCurrent(prompt));
      return painted && this.isPromptCurrent(prompt);
    } catch {
      return false;
    } finally {
      if ((!painted || !this.isPromptCurrent(prompt)) && this.promptQuestion === prompt) {
        this.promptQuestion = undefined;
        // Recover the latest underlying content without converting that
        // different frame's successful paint into a question acknowledgement.
        await this.paintVisual().catch(() => false);
      }
    }
  }

  /** JOAT replies leave the current feature intact; a question or consent
   * surface has priority over incidental assistant content. */
  async showAssistantOverlay(
    text: string,
    append = false,
    answersWearer = false,
    presentation: AssistantOverlayPresentation = 'answer',
  ): Promise<boolean> {
    // Not over a brainstorm/terminal page — UNLESS it answers something
    // the wearer just asked there. Seen live in the simulator: a queued
    // reply drained the instant a session settled and painted "REPLY —
    // Using the phone mic…" across the synthesis, default-layout text
    // stamped over a full-frame surface. The session's answer owns that
    // screen and unrelated chatter must wait.
    //
    // But the rule was written as "never", and that silenced the replies
    // TO the wearer: "make that shorter" produced its answer into nothing,
    // and the question asked before replacing a live session was never
    // seen — so the wearer was waiting on a prompt that did not exist.
    // An answer is not an interruption.
    if (this.surface.kind === 'brainstorm' && !answersWearer) return false;
    if (!text) return false;
    // A receipt must be visible, not merely recorded behind the native
    // conversation page. Quiesce that page first; the retained conversation
    // may resume later under the same Mac identity.
    if (this.assistantConversation.isActive()
      && !await this.prepareAssistantConversationHandoff()) return false;
    if (!await this.prepareNativeTerminalHandoff()) return false;
    const current = this.assistantOverlay;
    if (current?.dismissTimer !== undefined) clearTimeout(current.dismissTimer);
    const revision = append && current
      ? current.revision
      : ++this.assistantOverlayRevision;
    this.assistantOverlay = {
      text: append ? `${current?.text ?? ''}${text}` : text,
      presentation: append ? (current?.presentation ?? presentation) : presentation,
      revision,
    };
    if (this.confirmation) return true;
    if (append || this.assistantOverlayPaint) {
      this.scheduleAssistantOverlayPaint();
      return true;
    }
    return this.paintAssistantOverlayNow();
  }

  /** Install and retain one Even-style conversation surface. The live
   * JOAT feature model keeps accepting updates underneath it and is painted
   * at its newest state when the wearer leaves the conversation. */
  async showAssistantConversation(snapshot: AssistantConversationSnapshot): Promise<boolean> {
    if (!this.displayActive || this.confirmation || this.promptQuestion
      || !validAssistantConversationSnapshot(snapshot)) return false;
    if (!await this.prepareNativeTerminalHandoff()) return false;
    const entering = !this.assistantConversation.isActive();
    if (entering) {
      const displayCurrent = this.captureDisplayGuard();
      this.clearActionOverlay();
      this.cancelAssistantOverlayPaintTimer();
      this.assistantOverlayPaintDirty = false;
      if (this.assistantOverlay?.dismissTimer !== undefined) {
        clearTimeout(this.assistantOverlay.dismissTimer);
      }
      this.assistantOverlay = undefined;
      this.clearPanelTicker();
      if (this.thinkingTicker !== undefined) clearTimeout(this.thinkingTicker);
      this.thinkingTicker = undefined;
      if (!displayCurrent()) return false;
    }
    const presented = await this.assistantConversation.present(snapshot);
    if (!presented && entering) {
      this.assistantConversation.retireForHandoff();
      await this.restoreAfterAssistantConversation();
    }
    return presented;
  }

  hasAssistantConversation(): boolean {
    return this.assistantConversation.isActive();
  }

  isAssistantConversationContinuePromptVisible(): boolean {
    return this.assistantConversation.isContinuePromptVisible();
  }

  scrollAssistantConversation(direction: 'up' | 'down'): boolean {
    return this.assistantConversation.scroll(direction);
  }

  continueAssistantConversation(): Promise<boolean> {
    return this.assistantConversation.continueConversation();
  }

  assistantConversationIdentity(): { conversationId: string; revision: number } | undefined {
    return this.assistantConversation.currentIdentity();
  }

  requestAssistantConversationExit(): Promise<ConversationExitResult> {
    return this.assistantConversation.requestExit();
  }

  dismissAssistantConversation(conversationId: string, revision: number): Promise<boolean> {
    return this.assistantConversation.dismiss(conversationId, revision);
  }

  whenAssistantConversationIdle(): Promise<void> {
    return this.assistantConversation.whenIdle();
  }

  whenNativeTerminalIdle(): Promise<void> {
    return this.nativeTerminal.whenIdle();
  }

  private async prepareAssistantConversationHandoff(): Promise<boolean> {
    if (!this.assistantConversation.isActive()) return true;
    if (!await this.assistantConversation.pauseForHandoff()) return false;
    try {
      if (this.visual) {
        // Paint the retained feature before relinquishing conversation
        // ownership. This is the rollback point: if the exact underlay does
        // not reach the bridge, the same conversation resumes in place.
        if (!await this.restoreRetainedPage()) {
          await this.assistantConversation.resumeAfterFailedHandoff();
          return false;
        }
      }
      return this.assistantConversation.completeHandoff();
    } catch {
      await this.assistantConversation.resumeAfterFailedHandoff();
      return false;
    }
  }

  /** Quiesce a terminal body before a graphical page replaces its containers. */
  private async prepareNativeTerminalHandoff(): Promise<boolean> {
    if (!this.nativeTerminal.isActive()) {
      if (this.pageOwner !== 'terminal-list-native') return true;
      // Leaving a firmware list for Home: the text overview is itself a
      // full page rebuild, so it replaces the list page directly. Rebuilding
      // the four-tile image page first cost a second 165ms rebuild and a
      // blank flash on every Back-to-Home (measured [1,7] → [7], 2026-09-22).
      // The image page is rebuilt lazily by paintVisual only when the next
      // frame is NOT an overview (visualPagePending).
      if (nativeOverviewEnabled && this.visual) return true;
      return this.rebuildVisualPage();
    }
    if (!await this.nativeTerminal.pauseForHandoff()) return false;
    try {
      if (this.visual && !await this.rebuildVisualPage()) {
        await this.nativeTerminal.resumeAfterFailedHandoff();
        return false;
      }
      return this.nativeTerminal.completeHandoff();
    } catch {
      await this.nativeTerminal.resumeAfterFailedHandoff();
      return false;
    }
  }

  /** Back from a selected provider goes directly to the firmware picker. */
  private async replaceWithNativeTerminalList(page: RebuildPageContainer): Promise<boolean> {
    const active = this.nativeTerminal.isActive();
    if (active && !await this.nativeTerminal.pauseForHandoff()) return false;
    try {
      const replaced = await this.replaceNativePage(page, 'terminal-list-native');
      if (!active) return replaced;
      if (replaced) return this.nativeTerminal.completeHandoff();
      await this.nativeTerminal.resumeAfterFailedHandoff();
      return false;
    } catch {
      if (active) await this.nativeTerminal.resumeAfterFailedHandoff();
      return false;
    }
  }

  settleAssistantOverlay(dismissAfterMs: number): boolean {
    const overlay = this.assistantOverlay;
    if (!overlay || !Number.isInteger(dismissAfterMs) || dismissAfterMs < 1_000 || dismissAfterMs > 15_000) {
      return false;
    }
    if (overlay.dismissTimer !== undefined) clearTimeout(overlay.dismissTimer);
    // A multipage answer must remain until the wearer explicitly dismisses
    // it. Starting a timer on page one recreates the recorded loss even when
    // the hidden rows technically exist.
    if ((this.visual?.assistantOverlayPageCount?.() ?? 1) > 1) return true;
    overlay.dismissTimer = setTimeout(() => {
      if (this.assistantOverlay === overlay) void this.dismissAssistantOverlay();
    }, dismissAfterMs);
    return true;
  }

  async dismissAssistantOverlay(): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    const overlay = this.assistantOverlay;
    if (!overlay) return true;
    if (overlay.dismissTimer !== undefined) clearTimeout(overlay.dismissTimer);
    this.assistantOverlay = undefined;
    if (this.confirmation) return true;
    this.cancelAssistantOverlayPaintTimer();
    const active = this.assistantOverlayPaint;
    if (active) await active;
    if (!displayCurrent()) return false;
    // The active paint's completion may have queued the newest reply frame.
    // Cancel it after the await so an old tail can never repaint over a local
    // wearer dismissal.
    this.assistantOverlayPaintDirty = false;
    this.cancelAssistantOverlayPaintTimer();
    return this.paintAssistantOverlayNow();
  }

  /** Full raster frames are slower than model chunks. Keep at most one frame
   * active and one newest tail so streaming can never outrun the four-tile
   * bridge, including when the underlying surface is Navigation. */
  private paintAssistantOverlayNow(): Promise<boolean> {
    if (!this.displayActive) return Promise.resolve(false);
    if (this.confirmation) return Promise.resolve(true);
    if (this.assistantOverlayPaint) {
      this.assistantOverlayPaintDirty = true;
      return Promise.resolve(true);
    }
    this.assistantOverlayPaintStartedAt = Date.now();
    const paint = this.assistantOverlayPaintTarget();
    this.assistantOverlayPaint = paint;
    const completed = (): void => {
      if (this.assistantOverlayPaint !== paint) return;
      this.assistantOverlayPaint = undefined;
      if (!this.assistantOverlayPaintDirty) return;
      this.assistantOverlayPaintDirty = false;
      this.scheduleAssistantOverlayPaint();
    };
    void paint.then(completed, completed);
    return paint;
  }

  /**
   * A reply is text, so it goes on text containers — except over the map.
   *
   * Streaming used to run through paintVisual() whenever a visual surface
   * existed: four image uploads, each behind TILE_CALL_GAP_MS. Measured,
   * one such frame is ~400ms with ~330ms of that pacing alone, before the
   * BLE hop, and PAINT_INTERVAL_MS then throttles streaming so it cannot
   * outrun it. AssistantOverlayVisual carries `text` and nothing else.
   *
   * Navigation keeps the canvas. There the answer is composited OVER a
   * live map, and a text page would replace it — the wearer loses their
   * turn-by-turn mid-junction to save a few hundred milliseconds. That is
   * the trade that broke when this was attempted as a blanket switch.
   */
  private assistantOverlayNeedsCanvas(): boolean {
    if (this.surface.kind === 'navigation' || this.actionOverlay !== undefined) return true;
    // A painter that cannot hand the page over cannot take it back either:
    // replaceNativePage refuses without withPageTransition, and a refusal
    // here would report a failed paint rather than simply drawing the
    // answer the old way. Absent that capability the canvas keeps the job.
    return this.visual !== undefined && this.visual.withPageTransition === undefined;
  }

  private assistantOverlayPaintTarget(): Promise<boolean> {
    if (!this.visual) return this.paintNativeAssistantOverlay();
    if (this.assistantOverlay && !this.assistantOverlayNeedsCanvas()) {
      return this.paintAssistantAnswerPage();
    }
    if (this.assistantOverlay || this.pageOwner !== 'assistant-native') {
      return this.paintVisual();
    }
    // The borrowed text page goes back. paintVisual() returns true without
    // drawing while another owner holds the page, so the way home has to be
    // the explicit restore or the wearer is stranded on a stale answer.
    this.answerPages = undefined;
    this.nativeReplyPage = false;
    return this.restoreVisualPage();
  }

  /** Pages of the current answer, measured for the firmware's own box. */
  private answerPages?: { revision: number; pages: string[]; index: number };

  private answerPageState(): { pages: string[]; index: number } {
    const overlay = this.assistantOverlay;
    const text = overlay?.text ?? '';
    const current = this.answerPages;
    if (current && current.revision === overlay?.revision) {
      // Streaming appends to the same answer: re-measure, but hold the
      // wearer's place. A chunk arriving must not yank them back to page 1.
      const pages = paginate(text, ASSISTANT_ANSWER_GEOMETRY.inner);
      const index = Math.min(current.index, pages.length - 1);
      this.answerPages = { revision: current.revision, pages, index };
      return { pages, index };
    }
    const pages = paginate(text, ASSISTANT_ANSWER_GEOMETRY.inner);
    this.answerPages = { revision: overlay?.revision ?? 0, pages, index: 0 };
    return { pages, index: 0 };
  }

  private async paintAssistantAnswerPage(): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent()) return false;
    const { pages, index } = this.answerPageState();
    const body = pages[index] ?? '';
    const footer = pagerLabel(index, pages.length);
    if (this.pageOwner !== 'assistant-native') {
      const status = this.captureLiveStatus();
      if (!await this.replaceNativePage(
        this.layouts.assistantAnswerLayout(body, footer), 'assistant-native',
      )) return false;
      this.lastLength.clear();
      this.nativeReplyPage = true;
      this.streams.set(ContainerId.Main, body);
      this.streams.set(ContainerId.Hint, footer);
      await this.restoreLiveStatus(status);
      return displayCurrent();
    }
    // Already ours: two in-place writes, which is the whole point.
    return this.writeAnswerPage(body, footer, displayCurrent);
  }

  private async writeAnswerPage(
    body: string, footer: string, displayCurrent: () => boolean,
  ): Promise<boolean> {
    const client = this.client.require();
    const wrote = await client.textContainerUpgrade(new TextContainerUpgrade({
      containerID: ContainerId.Main, containerName: 'main',
      content: fitUtf8(body, TEXT_CONTAINER_MAX_BYTES) || ' ',
    }));
    if (!wrote || !displayCurrent()) return false;
    this.streams.set(ContainerId.Main, body);
    const paged = await client.textContainerUpgrade(new TextContainerUpgrade({
      containerID: ContainerId.Hint, containerName: 'answer-pager',
      content: fitUtf8(footer, TEXT_CONTAINER_MAX_BYTES) || ' ',
    }));
    if (paged) this.streams.set(ContainerId.Hint, footer);
    return paged && displayCurrent();
  }

  private async paintNativeAssistantOverlay(): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent()) return false;
    if (this.surface.kind === 'list') {
      if (!this.assistantOverlay && this.nativeReplyPage) return this.renderList(this.surface);
      if (this.assistantOverlay && !this.nativeReplyPage) {
        // A list's Main is only its short title. Give a reply the ordinary
        // reading area while retaining the list and its selection underneath.
        const status = this.captureLiveStatus();
        const rebuilt = await this.rebuildPage(this.layouts.defaultLayout());
        if (!rebuilt) return false;
        this.nativeReplyPage = true;
        this.lastLength.clear();
        await this.restoreLiveStatus(status);
        if (!displayCurrent()) return false;
      }
    }
    if (this.confirmation) return true;
    return this.paint(ContainerId.Main);
  }

  private scheduleAssistantOverlayPaint(): void {
    if (!this.displayActive) return;
    if (this.confirmation) return;
    if (this.assistantOverlayPaint) {
      this.assistantOverlayPaintDirty = true;
      return;
    }
    if (this.assistantOverlayPaintTimer !== undefined) return;
    const elapsed = Date.now() - this.assistantOverlayPaintStartedAt;
    const delay = Math.max(0, PAINT_INTERVAL_MS - elapsed);
    this.assistantOverlayPaintTimer = setTimeout(() => {
      this.assistantOverlayPaintTimer = undefined;
      void this.paintAssistantOverlayNow();
    }, delay);
  }

  private cancelAssistantOverlayPaintTimer(): void {
    if (this.assistantOverlayPaintTimer !== undefined) {
      clearTimeout(this.assistantOverlayPaintTimer);
    }
    this.assistantOverlayPaintTimer = undefined;
  }

  /** The transient reply owns ordinary input while it is readable, so a
   * wearer cannot accidentally activate the obscured feature underneath. */
  hasAssistantOverlay(): boolean {
    return this.assistantOverlay !== undefined;
  }

  /** Swipes belong to the answer reader. Boundary swipes remain consumed so
   * they can never operate the obscured feature or dismiss unread copy. */
  scrollAssistantOverlay(direction: 'up' | 'down'): boolean {
    if (!this.assistantOverlay) return false;
    // On the native answer page the pager is ours, not the renderer's: the
    // canvas is not drawing this answer, so asking it to scroll would move
    // a frame nobody is looking at.
    const paged = this.answerPages;
    if (paged && this.pageOwner === 'assistant-native') {
      const next = Math.max(0, Math.min(paged.pages.length - 1,
        paged.index + (direction === 'down' ? 1 : -1)));
      if (next !== paged.index) {
        paged.index = next;
        void this.paintAssistantOverlayNow();
      }
      return true;
    }
    if (this.visual?.scrollAssistantOverlay?.(direction)) {
      void this.paintAssistantOverlayNow();
    }
    return true;
  }

  /** Only a fully visible Home card is tappable. Focus pages keep incoming
   * notifications deferred, assistant overlays keep their higher priority,
   * and confirmations remain exclusive. */
  hasVisibleNotification(): boolean {
    return this.surface.kind === 'default'
      && !this.confirmation
      && !this.assistantOverlay
      && !this.assistantConversation.isActive()
      && !this.promptQuestion
      && this.kinds.get(ContainerId.Main) === 'notification'
      && (this.streams.get(ContainerId.Main)?.trim().length ?? 0) > 0;
  }

  visibleNotificationId(): string | undefined {
    return this.hasVisibleNotification() && this.notificationId === this.paintedNotificationId
      ? this.notificationId : undefined;
  }

  /** An explicit Mac acknowledgement clears only that JOAT card. A delayed
   * clear can never remove the newer card that arrived in the meantime. */
  async retireNotification(cardId: string): Promise<boolean> {
    if (!/^[a-f0-9]{32}$/i.test(cardId)) return false;
    this.retiredNotificationIds.add(cardId);
    // Retain tombstones for in-flight rollback snapshots, with a bounded
    // session-local window far larger than the number of retained surfaces.
    if (this.retiredNotificationIds.size > 512) {
      this.retiredNotificationIds.delete(this.retiredNotificationIds.values().next().value!);
    }
    const wasVisible = this.hasVisibleNotification() && this.notificationId === cardId;
    this.removeRetiredNotifications();
    return wasVisible ? this.paint(ContainerId.Main) : true;
  }

  private removeRetiredNotifications(): void {
    const clear = (content: HudContentState | undefined): void => {
      if (!content?.notificationId || !this.retiredNotificationIds.has(content.notificationId)
        || content.kinds.get(ContainerId.Main) !== 'notification') return;
      content.streams.delete(ContainerId.Main);
      content.brightness.delete(ContainerId.Main);
      content.kinds.delete(ContainerId.Main);
      content.notificationId = undefined;
    };
    if (this.notificationId && this.retiredNotificationIds.has(this.notificationId)
      && this.kinds.get(ContainerId.Main) === 'notification') {
      this.cancelPending(ContainerId.Main);
      this.streams.delete(ContainerId.Main);
      this.brightness.delete(ContainerId.Main);
      this.kinds.delete(ContainerId.Main);
      this.notificationId = undefined;
      this.paintedNotificationId = undefined;
      const prior = this.notificationReturn;
      this.notificationReturn = undefined;
      if (prior && prior.kind !== 'notification') {
        if (prior.text !== undefined) this.streams.set(ContainerId.Main, prior.text);
        if (prior.brightness !== undefined) this.brightness.set(ContainerId.Main, prior.brightness);
        if (prior.kind !== undefined) this.kinds.set(ContainerId.Main, prior.kind);
      }
    }
    clear(this.focusReturn);
    clear(this.confirmation);
    if (this.notificationReturn?.notificationId
      && this.retiredNotificationIds.has(this.notificationReturn.notificationId)) {
      this.notificationReturn = undefined;
    }
  }

  /** Hide the visible card locally and restore the exact displaced Main
   * content. This deliberately does not mark a native notification read. */
  async dismissVisibleNotification(): Promise<boolean> {
    if (!this.hasVisibleNotification()) return false;
    const prior = this.notificationReturn;
    this.notificationReturn = undefined;
    this.notificationId = prior?.notificationId;
    this.paintedNotificationId = undefined;
    this.cancelPending(ContainerId.Main);
    this.streams.delete(ContainerId.Main);
    this.brightness.delete(ContainerId.Main);
    this.kinds.delete(ContainerId.Main);
    if (prior?.text !== undefined) this.streams.set(ContainerId.Main, prior.text);
    if (prior?.brightness !== undefined) this.brightness.set(ContainerId.Main, prior.brightness);
    if (prior?.kind !== undefined) this.kinds.set(ContainerId.Main, prior.kind);
    return this.paint(ContainerId.Main);
  }

  /**
   * Announce a transient state ("listening", "settings"). This does NOT
   * write container 1 directly any more: the status bar has ONE owner
   * (Chrome), which composes clock + state + battery. Direct writes from
   * four call sites were clobbering each other — observed live
   * and the collision the dashboard design existed to prevent. Until
   * Chrome registers, announcements fall back to a direct paint so early
   * boot is never mute.
   */
  onStatus?: (text: string) => void;

  setStatus(text: string): Promise<boolean> {
    if (this.onStatus) {
      this.onStatus(text);
      return Promise.resolve(true);
    }
    return this.showText(text, ContainerId.StatusBar);
  }

  /** Privacy state is structured; it must never be inferred from a transient
   * status word such as "connecting" or "paused". */
  setMicState(open: boolean): void {
    this.micOn = open;
  }

  private guardedListPaintRevision = 0;

  async showList(
    items: string[], options: ListSurfaceOptions = {}, isCurrent?: () => boolean,
  ): Promise<boolean> {
    if (!await this.prepareAssistantConversationHandoff()) return false;
    if (!options.nativeFirmwareList && !await this.prepareNativeTerminalHandoff()) return false;
    if (isCurrent) return this.renderBoundList(items, options, isCurrent);
    const resolved = this.resolveListOptions(options);
    const surface: HudSurface = {
      kind: 'list', items: [...items], options: resolved,
      selectedIndex: this.initialListSelection(items, options),
    };
    if (this.confirmation) {
      if (this.confirmation.surface.kind === 'default' && !this.focusReturn) {
        this.focusReturn = {
          streams: new Map(this.confirmation.streams),
          brightness: new Map(this.confirmation.brightness),
          kinds: new Map(this.confirmation.kinds), notificationId: this.confirmation.notificationId,
        };
      }
      this.confirmation.surface = surface;
      return true;
    }
    if (this.surface.kind === 'default' && !this.focusReturn) {
      this.focusReturn = {
        streams: new Map(this.streams),
        brightness: new Map(this.brightness),
        kinds: new Map(this.kinds), notificationId: this.notificationId,
      };
    }
    if (!options.retainProductivityReceipt) this.clearActionOverlay();
    return this.renderList(surface);
  }

  /** Stage an owned raster list without adopting it before its exact receipt.
   * A failed or superseded upload leaves both the prior view and its Back
   * snapshot intact; it cannot become a deferred confirmation surface. */
  private async renderBoundList(
    items: string[], options: ListSurfaceOptions, isCurrent: () => boolean,
  ): Promise<boolean> {
    if (!this.visual?.paintExact || this.confirmation || this.promptQuestion) return false;
    const priorSurface = this.surface;
    const surface: Extract<HudSurface, { kind: 'list' }> = {
      kind: 'list', items: [...items], options: this.resolveListOptions(options),
      selectedIndex: this.initialListSelection(items, options),
    };
    const revision = ++this.guardedListPaintRevision;
    let committed = false;
    const current = () => {
      try {
        return revision === this.guardedListPaintRevision && isCurrent()
          && !this.confirmation && !this.promptQuestion
          && this.surface === (committed ? surface : priorSurface);
      } catch { return false; }
    };
    if (!current()) return false;
    const frame: HudVisualFrame = {
      kind: 'list', status: this.streams.get(ContainerId.StatusBar) ?? '', micOn: this.micOn,
      title: surface.options.title, items: [...surface.items], selectedIndex: surface.selectedIndex,
      contextLabel: surface.options.contextLabel,
      controlStartIndex: surface.options.controlStartIndex,
      planningEntries: surface.options.planningEntries,
      selectable: surface.options.selectable, hint: surface.options.hint,
      planningGrid: surface.options.planningGrid, planningPane: surface.options.planningPane,
      ...(surface.options.railTab ? { rail: this.rail.visualFor(surface.options.railTab) } : {}),
    };
    let painted = false;
    try { painted = await this.visual.paintExact(frame, current); } catch { /* No exact receipt. */ }
    if (!painted || !current()) return false;
    const liveStatus = this.captureLiveStatus();
    if (priorSurface.kind === 'default' && !this.focusReturn) {
      this.focusReturn = {
        streams: new Map(this.streams), brightness: new Map(this.brightness), kinds: new Map(this.kinds), notificationId: this.notificationId,
      };
    }
    this.clearActionOverlay();
    this.resetAll();
    committed = true;
    this.surface = surface;
    this.streams.set(ContainerId.Main, surface.options.title);
    this.brightness.set(ContainerId.Main, 4);
    this.streams.set(ContainerId.Hint, surface.options.hint);
    this.brightness.set(ContainerId.Hint, 1);
    if (liveStatus) {
      this.streams.set(ContainerId.StatusBar, liveStatus.text);
      if (liveStatus.brightness !== undefined) this.brightness.set(ContainerId.StatusBar, liveStatus.brightness);
    }
    return true;
  }

  /** Paint a complete navigation snapshot. The Mac/session owns the state;
   * this presenter only gives it exclusive focus and exact restoration. */
  /** A retained route does not own input over a different visible feature. */
  isNavigationVisible(): boolean {
    return this.surface.kind === 'navigation';
  }

  async showNavigation(navigation: NavigationHudModel, statusOverride?: string): Promise<boolean> {
    if (!this.visual) return false;
    if (!await this.prepareAssistantConversationHandoff()) return false;
    if (!await this.prepareNativeTerminalHandoff()) return false;
    const prior = this.capturePresenterSnapshot();
    const surface: HudSurface = {
      kind: 'navigation',
      navigation: this.cloneNavigation(navigation),
    };
    if (this.confirmation) {
      // A deferred surface has not crossed the SDK bridge. Navigation relies
      // on an exact generation-bound bridge receipt, so reject instead of
      // reporting a paint that never happened or resurrecting it on dismiss.
      return false;
    }
    if (this.surface.kind === 'default' && !this.focusReturn) {
      this.focusReturn = {
        streams: new Map(this.streams),
        brightness: new Map(this.brightness),
        kinds: new Map(this.kinds), notificationId: this.notificationId,
      };
    }
    // Route progress updates the SAME page; only entering navigation
    // supersedes a receipt strip.
    if (this.surface.kind !== 'navigation') this.clearActionOverlay();
    return this.renderNavigation(surface, statusOverride, prior);
  }

  /** Dedicated four-tile production page for a live Brainstorm or Hermes. */
  private thinkingTicker?: ReturnType<typeof setTimeout>;

  /** While the transcript tail is a silent "Thinking…" marker, repaint
   * locally once a second so the elapsed counter moves.
   *
   * No frame arrives from the Mac until the collaborator produces a
   * token — the engine used to compute this counter and re-ship a whole
   * frame over BLE every three seconds, which the wearer spotted on
   * sight: "it's only changing every three seconds … we're doing it the
   * difficult way." Device-local animation is smooth and costs the link
   * nothing. */
  /** How long the counter has been ticking, so it can slow down. */
  private thinkingTicks = 0;

  /** Whole seconds of silence after which the counter stops needing to be
   * precise. A turn measured against the real CLIs runs 12-40s, so a
   * minute of silence means something is wrong, not slow — and past that
   * the wearer is deciding whether to cancel, not reading digits. */
  private static readonly THINKING_PRECISE_S = 60;

  private armThinkingTicker(): void {
    if (!this.displayActive) return;
    if (this.thinkingTicker !== undefined) return;
    if (this.surface.kind !== 'brainstorm') return;
    const native = nativeDocument(this.surface.brainstorm);
    const thinking = native
      ? this.nativeTerminal.isThinking()
      : (this.visual?.transcriptIsThinking?.() ?? false);
    if (!thinking) {
      this.thinkingTicks = 0;
      return;
    }
    // Measured, not assumed: this costs ONE bridge write per tick. The
    // engine's old version shipped a whole frame every three seconds, so
    // at 1Hz this is 3x the writes — cheaper per update, more often, and
    // unbounded for as long as a turn stays silent. A hung collaborator
    // could hold the link at 1Hz indefinitely. So: one second while the
    // number is worth reading, five after that.
    this.thinkingTicks += 1;
    const interval = this.thinkingTicks < HudPresenter.THINKING_PRECISE_S
      ? 1000
      : 5000;
    this.thinkingTicker = setTimeout(() => {
      this.thinkingTicker = undefined;
      if (this.surface.kind !== 'brainstorm') return;
      if (nativeDocument(this.surface.brainstorm)) {
        // One body upgrade with new digits; then keep counting.
        if (this.nativeTerminal.tick()) this.armThinkingTicker();
        else this.thinkingTicks = 0;
        return;
      }
      // localTick: only the counter's digits moved, so this repaint is
      // NOT recorded as a wearer-visible frame receipt.
      void this.paintVisual(true);
    }, interval);
  }

  /** Swipe on the brainstorm/terminal surface pages through history. */
  scrollTranscript(direction: 'up' | 'down'): boolean {
    if (this.surface.kind !== 'brainstorm') return false;
    if (nativeDocument(this.surface.brainstorm)) {
      return this.nativeTerminal.scroll(direction);
    }
    if (!this.visual) return false;
    if (!(this.visual.scrollTranscript?.(direction) ?? false)) return true;
    void this.paintVisual();
    return true;
  }

  async showBrainstorm(
    brainstorm: BrainstormHudModel, statusOverride?: string, isCurrent?: () => boolean,
  ): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent()) return false;
    if (this.confirmation || (isCurrent && !isCurrent())) return false;
    if (!await this.prepareAssistantConversationHandoff()) return false;
    if (nativeDocument(brainstorm)) {
      const shown = await this.showNativeTerminal(brainstorm, statusOverride, isCurrent);
      if (shown && brainstorm.screen === 'brainstorm') this.armThinkingTicker();
      return shown;
    }
    if (!this.visual || !await this.prepareNativeTerminalHandoff()) return false;
    this.clearActionOverlay();
    const prior = this.capturePresenterSnapshot();
    const surface: HudSurface = {
      kind: 'brainstorm',
      brainstorm: this.cloneBrainstorm(brainstorm),
    };
    if (this.surface.kind === 'default' && !this.focusReturn) {
      this.focusReturn = {
        streams: new Map(this.streams),
        brightness: new Map(this.brightness),
        kinds: new Map(this.kinds), notificationId: this.notificationId,
      };
    }
    const liveStatus = statusOverride === undefined
      ? this.captureLiveStatus()
      : { text: statusOverride, brightness: 2 };
    this.resetAll();
    this.surface = surface;
    if (liveStatus) {
      this.streams.set(ContainerId.StatusBar, liveStatus.text);
      if (liveStatus.brightness !== undefined) {
        this.brightness.set(ContainerId.StatusBar, liveStatus.brightness);
      }
    }
    const stillCurrent = () => displayCurrent() && this.surface === surface && (isCurrent?.() ?? true);
    const painted = isCurrent
      ? await (this.visual.paintExact?.(this.visualFrame(), stillCurrent) ?? Promise.resolve(false))
      : await this.paintVisual();
    // A departed opening cannot acknowledge or restore its old snapshot over
    // Home, another feature, or a newer Terminal selection.
    if (!stillCurrent()) return false;
    if (!painted) {
      this.restorePresenterSnapshot(prior);
      await this.paintRollbackSurface();
    }
    return painted;
  }

  private async showNativeTerminal(
    terminal: BrainstormHudModel,
    statusOverride?: string,
    isCurrent?: () => boolean,
  ): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent() || !nativeDocument(terminal)) return false;
    const prior = this.capturePresenterSnapshot();
    const wasActive = this.nativeTerminal.isActive();
    const surface: HudSurface = {
      kind: 'brainstorm',
      brainstorm: this.cloneBrainstorm(terminal),
    };
    if (this.surface.kind === 'default' && !this.focusReturn) {
      this.focusReturn = {
        streams: new Map(this.streams),
        brightness: new Map(this.brightness),
        kinds: new Map(this.kinds), notificationId: this.notificationId,
      };
    }
    const liveStatus = statusOverride === undefined
      ? this.captureLiveStatus()
      : { text: statusOverride, brightness: 2 };
    this.clearActionOverlay();
    this.resetAll();
    this.surface = surface;
    if (liveStatus) {
      this.streams.set(ContainerId.StatusBar, liveStatus.text);
      if (liveStatus.brightness !== undefined) {
        this.brightness.set(ContainerId.StatusBar, liveStatus.brightness);
      }
    }
    const stillCurrent = () => displayCurrent() && this.surface === surface && (isCurrent?.() ?? true);
    const painted = await this.nativeTerminal.present(
      surface.brainstorm,
      liveStatus?.text,
      stillCurrent,
    );
    if (!stillCurrent()) return false;
    if (painted) return true;

    this.restorePresenterSnapshot(prior);
    if (!wasActive) {
      this.nativeTerminal.retire();
      if (this.pageOwner === 'terminal-conversation-native') {
        if (prior.surface.kind === 'list' && prior.surface.options.nativeFirmwareList) {
          await this.replaceNativePage(
            this.nativeListLayout(prior.surface),
            'terminal-list-native',
          );
        } else {
          await this.restoreVisualPage();
        }
      }
    }
    return false;
  }

  /** Native Calendar/Reminders lifecycle page. The preceding confirmation is
   * the only write authority; this surface only renders bridge evidence. */
  async showProductivity(
    productivity: ProductivityHudModel,
    statusOverride?: string,
    onDeferredProductivityVisible?: () => void,
  ): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent()) return false;
    if (!this.visual) return false;
    if (!await this.prepareAssistantConversationHandoff()) return false;
    if (!await this.prepareNativeTerminalHandoff()) return false;
    if (usesActionPillLayout(productivity.phase)) {
      return this.showActionPill(productivity, onDeferredProductivityVisible);
    }
    this.clearActionOverlay();
    const surface: HudSurface = {
      kind: 'productivity',
      productivity: this.cloneProductivity(productivity),
    };
    if (this.confirmation) {
      // Native read-back can start before the async confirmation dismissal
      // finishes its last tile. Defer the page rather than claiming a screen
      // paint which the wearer never saw; dismiss restores it atomically.
      this.confirmation.surface = surface;
      this.confirmation.onDeferredProductivityVisible = onDeferredProductivityVisible;
      return true;
    }
    const prior = this.capturePresenterSnapshot();
    if (this.surface.kind === 'default' && !this.focusReturn) {
      this.focusReturn = {
        streams: new Map(this.streams),
        brightness: new Map(this.brightness),
        kinds: new Map(this.kinds), notificationId: this.notificationId,
      };
    }
    const liveStatus = statusOverride === undefined
      ? this.captureLiveStatus()
      : { text: statusOverride, brightness: 2 };
    this.resetAll();
    this.surface = surface;
    if (liveStatus) {
      this.streams.set(ContainerId.StatusBar, liveStatus.text);
      if (liveStatus.brightness !== undefined) {
        this.brightness.set(ContainerId.StatusBar, liveStatus.brightness);
      }
    }
    const painted = await this.paintVisual();
    if (!displayCurrent()) return false;
    if (!painted) {
      this.restorePresenterSnapshot(prior);
      await this.paintRollbackSurface();
    }
    return painted;
  }

  /** Receipt strip over whatever page the wearer is on; no surface swap. */
  private async showActionPill(
    productivity: ProductivityHudModel,
    onVisible?: () => void,
  ): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent()) return false;
    this.clearActionOverlay();
    if (this.confirmation) {
      // A retained truth page under the gate is superseded by this newer
      // outcome — never restore FAILED beneath a SAVED strip.
      if (this.confirmation.surface.kind === 'productivity') {
        this.confirmation.surface = { kind: 'default' };
      }
      this.armActionOverlay(productivity);
      this.confirmation.onDeferredPillVisible = onVisible;
      return true;
    }
    if (this.surface.kind === 'productivity') {
      await this.showDefault();
      if (!displayCurrent()) return false;
    }
    const overlay = this.armActionOverlay(productivity);
    const painted = await this.paintVisual();
    if (!painted && this.actionOverlay === overlay) this.clearActionOverlay();
    return painted;
  }

  private armActionOverlay(productivity: ProductivityHudModel): ActionOverlayState {
    const overlay: ActionOverlayState = {
      productivity: this.cloneProductivity(productivity),
    };
    overlay.dismissTimer = setTimeout(() => {
      if (this.actionOverlay !== overlay) return;
      this.actionOverlay = undefined;
      void this.paintVisual();
    }, ACTION_PILL_FAILSAFE_MS);
    this.actionOverlay = overlay;
    return overlay;
  }

  private clearActionOverlay(): void {
    if (this.actionOverlay?.dismissTimer !== undefined) {
      clearTimeout(this.actionOverlay.dismissTimer);
    }
    this.actionOverlay = undefined;
  }

  /** Whether this exact productivity surface is currently retained after a
   * successful paint. A deferred confirmation or a failed paint is not a
   * visible surface and must not be used as a delivery acknowledgement. */
  isProductivityVisible(productivity: ProductivityHudModel): boolean {
    if (!this.confirmation && this.actionOverlay) {
      if (sameProductivity(this.actionOverlay.productivity, productivity)) return true;
    }
    if (this.surface.kind !== 'productivity') return false;
    return sameProductivity(this.surface.productivity, productivity);
  }

  /** Repaint the Planning dashboard with a new hover/pane focus. Local-only
   * state: no Mac request, one visual paint, and the unchanged-tile skip
   * keeps a hover move to the tiles whose pixels actually differ. */
  async updatePlanningPane(pane: PlanningPaneState): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    const current = this.surface;
    const foregroundAvailable = () => !this.confirmation && !this.promptQuestion
      && !this.assistantConversation.isActive();
    if (!displayCurrent() || current.kind !== 'list' || !current.options.planningGrid
      || !foregroundAvailable() || !this.visual) return false;
    const surface: HudSurface = {
      ...current,
      options: { ...current.options, planningPane: { ...pane } },
    };
    this.surface = surface;
    const painted = await this.paintVisual();
    if (!displayCurrent()) return false;
    if (!painted && this.surface === surface && foregroundAvailable()) {
      this.surface = current;
      // One bounded rollback; never overwrite a newer foreground owner.
      await this.visual.paint(this.visualFrame());
    }
    return painted && this.surface === surface && foregroundAvailable();
  }

  async setSelection(index: number): Promise<boolean> {
    if (
      this.surface.kind === 'default'
      || this.surface.kind === 'navigation'
      || this.surface.kind === 'brainstorm'
      || this.surface.kind === 'productivity'
    ) return false;
    const maximum = Math.max(0, this.surface.items.length - 1);
    const selectedIndex = Math.max(0, Math.min(maximum, index));
    const surface: HudSurface = { ...this.surface, selectedIndex };
    return this.renderList(surface);
  }

  /** Restore only after the selected custom row has painted and still owns
   * the surface. Native compatibility lists have no cursor-setting API. */
  async restoreSelection(index: number): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent()) return false;
    const current = this.surface;
    if (current.kind !== 'list' || this.confirmation || this.promptQuestion
      || this.assistantConversation.isActive()) return false;
    const selectedIndex = Math.max(0, Math.min(Math.max(0, current.items.length - 1), index));
    if (!this.visual) return selectedIndex === 0 ? this.setSelection(0) : false;
    const surface: HudSurface = { ...current, selectedIndex };
    const painted = await this.renderList(surface);
    if (!displayCurrent()) return false;
    if (!painted && this.surface === surface && !this.confirmation && !this.promptQuestion
      && !this.assistantConversation.isActive()) {
      this.surface = current;
      // One direct rollback paint follows the existing bounded recovery.
      // Never recurse into another healing attempt or overwrite a successor.
      await this.visual.paint(this.visualFrame());
    }
    return painted && this.surface === surface && !this.confirmation && !this.promptQuestion
      && !this.assistantConversation.isActive();
  }

  moveSelection(delta: number): Promise<boolean> {
    if (
      this.surface.kind === 'default'
      || this.surface.kind === 'navigation'
      || this.surface.kind === 'brainstorm'
      || this.surface.kind === 'productivity'
    ) {
      return Promise.resolve(false);
    }
    return this.setSelection(this.surface.selectedIndex + delta);
  }

  private async renderNavigation(
    surface: Extract<HudSurface, { kind: 'navigation' }>,
    statusOverride?: string,
    prior?: PresenterSnapshot,
  ): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent()) return false;
    const from = this.tracePage();
    const liveStatus = statusOverride === undefined
      ? this.captureLiveStatus()
      : { text: statusOverride, brightness: 2 };
    this.resetAll();
    this.surface = surface;
    if (liveStatus) {
      this.streams.set(ContainerId.StatusBar, liveStatus.text);
      if (liveStatus.brightness !== undefined) {
        this.brightness.set(ContainerId.StatusBar, liveStatus.brightness);
      }
    }
    const painted = await this.paintVisual();
    if (!displayCurrent()) return false;
    if (!painted && prior) {
      this.restorePresenterSnapshot(prior);
      // paintVisual already spent its one bounded self-heal on the failed new
      // frame. Make one direct rollback paint so pixels and input ownership
      // converge on the same prior surface, but keep the caller result false.
      await this.paintRollbackSurface();
    }
    this.recordPageTransition(from, 'navigation', 'show_navigation', painted ? 'success' : 'failed');
    return painted;
  }

  private async renderList(surface: Extract<HudSurface, { kind: 'list' }>): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent()) return false;
    const from = this.tracePage();
    const liveStatus = this.captureLiveStatus();
    // Every list is a firmware-owned list now.
    //
    // Agent selection went first and the rest were left on "JOAT's authored
    // visual treatment in this migration slice". Measured on the simulator,
    // that slice costs 721ms to open a feature list — 607ms of it asleep in
    // TILE_CALL_GAP_MS between four image uploads — and ~310ms for every
    // step of the tab rail. News, Messages, QuickNotes and Planning are rows
    // of text; the firmware scrolls its own highlight and reports the pick
    // as `listEvent`, with no raster bill at all.
    //
    // Opt out with `nativeFirmwareList: false` where a surface genuinely
    // needs the canvas (a prose reader that is not a row list).
    // Firmware lists cannot have their selection set programmatically and
    // cannot be updated in place, so surfaces that restore a cursor across
    // Back, or page through message rows, cannot live on one.
    const firmwareList = surface.options.nativeFirmwareList === true;
    const nativeReplyPage = !this.visual && !!this.assistantOverlay && !this.confirmation;
    const ok = firmwareList
      ? await this.replaceWithNativeTerminalList(
        this.nativeListLayout(surface),
      )
      : this.visual
      ? true
      : await this.rebuildPage(nativeReplyPage
          ? this.layouts.defaultLayout()
          : this.nativeListLayout(surface));
    if (!ok || !displayCurrent()) {
      this.recordPageTransition(from, 'list', 'show_list', 'failed');
      return false;
    }
    this.resetAll();
    this.surface = surface;
    this.nativeReplyPage = nativeReplyPage;
    // Mirror payload content in presenter state so confirmation restoration
    // and a same-layout self-heal retain the named focus page.
    this.streams.set(ContainerId.Main, surface.options.title);
    this.brightness.set(ContainerId.Main, 4);
    this.streams.set(ContainerId.Hint, surface.options.hint);
    this.brightness.set(ContainerId.Hint, 1);
    if (this.visual && !firmwareList) {
      if (!await this.rebuildVisualPage()) {
        this.recordPageTransition(from, 'list', 'show_list', 'failed');
        return false;
      }
      if (liveStatus) {
        this.streams.set(ContainerId.StatusBar, liveStatus.text);
        if (liveStatus.brightness !== undefined) {
          this.brightness.set(ContainerId.StatusBar, liveStatus.brightness);
        }
      }
      const painted = await this.paintVisual();
      this.recordPageTransition(from, 'list', 'show_list', painted ? 'success' : 'failed');
      return painted;
    }
    if (firmwareList) {
      const painted = displayCurrent();
      this.recordPageTransition(from, 'list', 'show_list', painted ? 'success' : 'failed');
      if (painted) this.armPanelTicker();
      return painted;
    }
    const restored = await this.restoreLiveStatus(liveStatus);
    if (!displayCurrent()) return false;
    const painted = nativeReplyPage ? (await this.paint(ContainerId.Main)) && restored : restored;
    this.recordPageTransition(from, 'list', 'show_list', painted ? 'success' : 'failed');
    return painted;
  }

  /** Adopt the Mac's margin facts and repaint if the dashboard is showing. */
  async setAmbient(
    ambient: { temperature: string; condition: string; unread: number },
  ): Promise<boolean> {
    const changed = this.ambient?.temperature !== ambient.temperature
      || this.ambient?.condition !== ambient.condition
      || this.ambient?.unread !== ambient.unread;
    this.ambient = ambient;
    if (!changed) return true;
    // Only the resting screen wears these; repainting a feature page for a
    // temperature would cost BLE transfers the wearer never asked for.
    if (this.surface.kind !== 'default') return true;
    if (!this.visual) return true;
    return this.paintVisual();
  }

  /** Which tab the overview's right pane shows. Local: the Mac never learns it. */
  activeTab(): PanelTab {
    return this.rail.active();
  }

  async setActiveTab(tab: PanelTab): Promise<boolean> {
    if (!this.rail.setActive(tab)) return true;
    return this.repaintRestingPane();
  }

  /** A swipe on the resting overview: one tab along the rail. Returns the
   * tab now active, or undefined at either end of the rail. */
  async stepTab(delta: number): Promise<PanelTab | undefined> {
    const next = this.rail.step(delta);
    if (next !== undefined) await this.repaintRestingPane();
    return next;
  }

  /** Cache a Mac push. Repaints only when the pushed tab is on the glass;
   * an expanded page for that tab is CapabilityMenu's to refresh. */
  async acceptPanel(message: HudPanelMessage): Promise<PanelContent> {
    const content = this.rail.accept(message);
    if (this.rail.active() === message.tab) await this.repaintRestingPane();
    return content;
  }

  /** Back to the first tab — what a fresh brain connection lands on. */
  resetTab(): void {
    this.rail.reset();
  }

  private async repaintRestingPane(): Promise<boolean> {
    if (this.confirmation) return true;
    if (this.surface.kind !== 'default') return true;
    if (!this.visual) return true;
    return this.paintVisual();
  }

  /**
   * Repaint the visible pomodoro exactly when its next digit moves: every
   * second on the expanded tab, every minute on the resting overview. A
   * tick is a localTick — no new information from the Mac — so it is not a
   * wearer-visible frame receipt, and it stops the moment the timer is not
   * on the glass or not running.
   */
  private armPanelTicker(): void {
    if (!this.displayActive) return;
    if (this.panelTicker !== undefined) return;
    const delay = this.panelTickDelay();
    if (delay === undefined) return;
    this.panelTicker = setTimeout(() => {
      this.panelTicker = undefined;
      if (this.panelTickDelay() === undefined) return;
      void this.tickPanel();
    }, delay);
  }

  /** The list page for a list surface. The expanded Pomodoro carries its
   * countdown in the title line — a firmware list cannot be updated in
   * place, but its title container can (one textContainerUpgrade per
   * second while running; the canvas page re-uploaded tiles for the same). */
  private nativeListLayout(surface: Extract<HudSurface, { kind: 'list' }>): RebuildPageContainer {
    return this.layouts.listLayout(surface.items, {
      ...surface.options, title: this.panelListTitle(surface.options),
    });
  }

  private panelListTitle(options: ResolvedListSurfaceOptions): string {
    if (options.railTab !== 'pomodoro') return options.title;
    const pomodoro = this.rail.content('pomodoro')?.pomodoro;
    if (!pomodoro || pomodoro.phase === 'idle') return options.title;
    const remaining = pomodoroRemainingMs(pomodoro, Date.now());
    return `${options.title} · ${formatCountdown(remaining)} · ${pomodoroBlockLabel(pomodoro)}`;
  }

  /** What the resting Pomodoro card says, from the same helpers the
   * expanded page uses, so the two never disagree about where you are. */
  private pomodoroCardRows(content: PanelContent | undefined): string[] {
    if (content?.empty) return [content.headline ?? 'Timer unavailable', content.empty];
    const pomodoro = content?.pomodoro;
    if (!pomodoro) return ['Say “JOAT, start a pomodoro”'];
    if (pomodoro.phase === 'idle') {
      return ['Ready', `${Math.round(pomodoro.lengthMs / 60_000)} min focus · tap to start`];
    }
    const remaining = pomodoroRemainingMs(pomodoro, Date.now());
    return [
      formatMinutesLeft(remaining),
      pomodoroBlockLabel(pomodoro),
      pomodoroCycleDots(pomodoro.completed),
      ...(content?.rows ?? []),
    ];
  }

  /** A local tick: the countdown's digit moved, nothing new from the Mac.
   * On the expanded firmware list only the title container is rewritten. */
  private async tickPanel(): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    if (this.surface.kind === 'list' && this.surface.options.nativeFirmwareList
      && this.surface.options.railTab === 'pomodoro' && this.pageOwner === 'terminal-list-native') {
      const title = this.panelListTitle(this.surface.options);
      const previous = this.streams.get(ContainerId.Main) ?? '';
      if (title === previous) { this.armPanelTicker(); return true; }
      const ok = await this.client.require().textContainerUpgrade(new TextContainerUpgrade({
        containerID: ContainerId.Main, contentOffset: 0,
        contentLength: Math.max(previous.length, title.length),
        content: fitUtf8(title, TEXT_CONTAINER_MAX_BYTES),
      }));
      if (ok && displayCurrent()) this.streams.set(ContainerId.Main, title);
      this.armPanelTicker();
      return ok;
    }
    return this.paintVisual(true);
  }

  private panelTickDelay(): number | undefined {
    const pomodoro = this.rail.content('pomodoro')?.pomodoro;
    if (!pomodoro) return undefined;
    const now = Date.now();
    if (this.surface.kind === 'default' && this.rail.active() === 'pomodoro') {
      return pomodoroTickDelayMs(pomodoro, now, 60_000);
    }
    if (this.surface.kind === 'list' && this.surface.options.railTab === 'pomodoro') {
      return pomodoroTickDelayMs(pomodoro, now, 1000);
    }
    return undefined;
  }

  private clearPanelTicker(): void {
    if (this.panelTicker === undefined) return;
    clearTimeout(this.panelTicker);
    this.panelTicker = undefined;
  }

  async showDefault(): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent()) return false;
    if (!await this.prepareAssistantConversationHandoff()) return false;
    if (!await this.prepareNativeTerminalHandoff()) return false;
    if (this.confirmation) {
      this.confirmation.surface = { kind: 'default' };
      if (this.focusReturn) {
        this.confirmation.streams = new Map(this.focusReturn.streams);
        this.confirmation.brightness = new Map(this.focusReturn.brightness);
        this.confirmation.kinds = new Map(this.focusReturn.kinds);
        this.confirmation.notificationId = this.focusReturn.notificationId;
        this.focusReturn = undefined;
      }
      return true;
    }
    this.clearActionOverlay();
    const from = this.tracePage();
    const deferred = this.focusReturn;
    const liveStatus = this.captureLiveStatus();
    if (this.visual) {
      const content = deferred ?? {
        streams: new Map(this.streams),
        brightness: new Map(this.brightness),
        kinds: new Map(this.kinds), notificationId: this.notificationId,
      };
      this.resetAll();
      this.surface = { kind: 'default' };
      this.focusReturn = undefined;
      this.streams = new Map(content.streams);
      this.brightness = new Map(content.brightness);
      this.kinds = new Map(content.kinds);
      this.notificationId = content.notificationId;
      if (liveStatus) {
        this.streams.set(ContainerId.StatusBar, liveStatus.text);
        if (liveStatus.brightness !== undefined) {
          this.brightness.set(ContainerId.StatusBar, liveStatus.brightness);
        }
      }
      const painted = await this.paintVisual();
      this.recordPageTransition(from, 'overview', 'show_overview', painted ? 'success' : 'failed');
      return painted;
    }
    const ok = await this.rebuildPage(this.layouts.defaultLayout());
    if (!ok) {
      this.recordPageTransition(from, 'overview', 'show_overview', 'failed');
      return false;
    }
    this.resetAll();
    this.surface = { kind: 'default' };
    this.focusReturn = undefined;
    let restored = await this.restoreLiveStatus(liveStatus);
    if (!displayCurrent()) return false;
    if (!deferred) {
      this.recordPageTransition(from, 'overview', 'show_overview', restored ? 'success' : 'failed');
      return restored;
    }

    for (const [id, text] of deferred.streams) {
      if (id !== ContainerId.StatusBar) this.streams.set(id, text);
    }
    for (const [id, level] of deferred.brightness) {
      if (id !== ContainerId.StatusBar) this.brightness.set(id, level);
    }
    for (const [id, kind] of deferred.kinds) {
      if (id !== ContainerId.StatusBar) this.kinds.set(id, kind);
    }
    this.notificationId = deferred.notificationId;
    for (const id of [
      ContainerId.Main,
      ContainerId.Context,
      ContainerId.Tray,
      ContainerId.Hint,
    ] as const) {
      if (this.streams.has(id)) restored = (await this.paint(id)) && restored;
      if (!displayCurrent()) return false;
    }
    this.recordPageTransition(from, 'overview', 'show_overview', restored ? 'success' : 'failed');
    return restored;
  }

  /** Replace only the return snapshot of the exact, already claimed consent gate. */
  stageConfirmationReturnList(id: string, items: string[], options: ListSurfaceOptions): boolean {
    if (this.confirmation?.id !== id) return false;
    this.confirmation.surface = {
      kind: 'list', items: [...items], options: this.resolveListOptions(options),
      selectedIndex: this.initialListSelection(items, options),
    };
    return true;
  }

  /**
   * The one exclusive app-owned decision surface.
   *
   * It keeps plugin-owned status visible, removes competing navigation and
   * content, and defers ordinary paints until the gate settles. Dismissal
   * rebuilds the exact prior surface rather than dropping the wearer at Home.
   */
  showConfirmation(
    prompt: string,
    id = 'hud:confirmation',
    diagnosticKind?: 'messages',
    isCurrent?: () => boolean,
  ): Promise<boolean> {
    if (isCurrent) {
      if (!this.visual?.paintExact) return Promise.resolve(false);
      try { if (!isCurrent()) return Promise.resolve(false); } catch { return Promise.resolve(false); }
    }
    const priorPage = this.tracePage();
    const openedHere = this.confirmation === undefined;
    // A new decision gate supersedes any lingering outcome strip.
    this.clearActionOverlay();
    const safePrompt = prompt.trim() || 'Confirm requested action.';
    if (safePrompt.length > CONFIRM_PROMPT_MAX_UNITS) {
      console.error('[HudPresenter] exact confirmation preview exceeds HUD capacity');
      return Promise.resolve(false);
    }
    if (this.assistantConversation.isActive()) {
      return this.prepareAssistantConversationHandoff().then((ready) => (
        ready ? this.showConfirmation(prompt, id, diagnosticKind, isCurrent) : false
      ));
    }
    if (this.nativeTerminal.isActive() || this.pageOwner === 'terminal-list-native') {
      return this.prepareNativeTerminalHandoff().then((ready) => (
        ready ? this.showConfirmation(prompt, id, diagnosticKind, isCurrent) : false
      ));
    }
    // Asking for text never survives a transition into write consent.
    this.promptQuestion = undefined;
    if (!this.confirmation) {
      const content = this.surface.kind !== 'default' && this.focusReturn
        ? this.focusReturn
        : { streams: this.streams, brightness: this.brightness, kinds: this.kinds, notificationId: this.notificationId };
      this.confirmation = {
        id,
        prompt: safePrompt,
        ...(diagnosticKind ? { diagnosticKind } : {}),
        ...(isCurrent ? { isCurrent } : {}),
        surface: this.cloneSurface(this.surface),
        streams: new Map(content.streams),
        brightness: new Map(content.brightness),
        kinds: new Map(content.kinds), notificationId: content.notificationId,
      };
    } else {
      // Claim ownership synchronously, before an awaited bridge call. A stale
      // result callback can now only enqueue a no-op dismissal for its old id.
      this.confirmation = { ...this.confirmation, id, prompt: safePrompt, diagnosticKind, isCurrent };
    }

    const claimed = this.confirmation;
    return this.enqueueConfirmation(() => this.renderConfirmation(id, openedHere, priorPage, claimed));
  }

  private isConfirmationCurrent(confirmation: ConfirmationReturnState): boolean {
    try { return this.confirmation === confirmation && (confirmation.isCurrent?.() ?? true); }
    catch { return false; }
  }

  private async renderConfirmation(
    id: string,
    openedHere = false,
    from: G2TracePage = this.tracePage(),
    claimed?: ConfirmationReturnState,
  ): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent()) return false;
    // Native replies and consent share Main. Finish an already-dispatched
    // reply before rebuilding consent so its late acknowledgement cannot
    // leave reply text covering an armed decision gate.
    // A failed reply is settled too; it must not strand this new gate.
    if (this.assistantOverlayPaint) await this.assistantOverlayPaint.catch(() => false);
    if (!displayCurrent()) return false;
    const current = this.confirmation;
    if (!current || current.id !== id || (claimed && current !== claimed)) return !claimed?.isCurrent;
    if (current.isCurrent && !this.isConfirmationCurrent(current)) {
      this.confirmation = undefined;
      await this.restoreAfterConfirmation(current);
      return false;
    }
    const liveStatus = this.captureLiveStatus();
    if (this.visual) {
      this.resetAll();
      this.streams.set(ContainerId.Main, `CONFIRM ACTION\n\n${current.prompt}`);
      this.streams.set(ContainerId.Context, 'TAP · confirm   SWIPE · cancel');
      this.streams.set(ContainerId.Tray, '');
      this.streams.set(ContainerId.Hint, '8 seconds · timeout cancels');
      if (liveStatus) {
        this.streams.set(ContainerId.StatusBar, liveStatus.text);
        if (liveStatus.brightness !== undefined) {
          this.brightness.set(ContainerId.StatusBar, liveStatus.brightness);
        }
      }
      const painted = (await this.paintVisual()) && (!current.isCurrent || this.isConfirmationCurrent(current));
      if (!displayCurrent()) return false;
      if (!painted && current.isCurrent) {
        // A failed or retired exact preview cannot keep an invisible gate.
        // Restore only this claim; a successor owns its own return surface.
        if (this.confirmation === current) {
          this.confirmation = undefined;
          await this.restoreAfterConfirmation(current);
        }
        this.recordPageTransition(from, 'confirmation', 'show_confirmation', 'failed');
        return false;
      }
      if (!painted && openedHere && this.confirmation?.id === id) {
        // A failed first paint must not leave an invisible decision gate owning
        // every later content update. Restore the logical surface snapshot so
        // the next healthy frame can recover, while still returning false so
        // the IMU/tap gate never activates.
        this.confirmation = undefined;
        this.surface = this.cloneSurface(current.surface);
        this.streams = new Map(current.streams);
        this.brightness = new Map(current.brightness);
        this.kinds = new Map(current.kinds);
        this.notificationId = current.notificationId;
        this.paintedNotificationId = undefined;
        // One failed tile can leave a torn confirmation visible even though
        // the logical gate has rolled back. Actively repaint the complete
        // prior frame before returning failure to the input gate.
        if (liveStatus) {
          this.streams.set(ContainerId.StatusBar, liveStatus.text);
          if (liveStatus.brightness !== undefined) {
            this.brightness.set(ContainerId.StatusBar, liveStatus.brightness);
          }
        }
        await this.paintVisual();
      }
      this.recordPageTransition(from, 'confirmation', 'show_confirmation', painted ? 'success' : 'failed');
      return painted;
    }
    const ok = await this.rebuildPage(this.layouts.confirmationLayout(current.prompt));
    if (!displayCurrent()) return false;
    if (this.confirmation?.id !== id) return true;
    if (!ok) {
      if (openedHere) {
        this.confirmation = undefined;
      }
      this.recordPageTransition(from, 'confirmation', 'show_confirmation', 'failed');
      return false;
    }
    this.resetAll();
    this.streams.set(ContainerId.Main, `CONFIRM ACTION\n\n${current.prompt}`);
    this.streams.set(ContainerId.Context, 'TAP · confirm   SWIPE · cancel');
    this.streams.set(ContainerId.Tray, '');
    this.streams.set(ContainerId.Hint, '8 seconds · timeout cancels');
    this.brightness.set(ContainerId.Main, 4);
    this.brightness.set(ContainerId.Context, 3);
    this.brightness.set(ContainerId.Tray, 3);
    this.brightness.set(ContainerId.Hint, 1);
    const restored = await this.restoreLiveStatus(liveStatus);
    this.recordPageTransition(from, 'confirmation', 'show_confirmation', restored ? 'success' : 'failed');
    return restored;
  }

  dismissConfirmation(id?: string): Promise<boolean> {
    return this.enqueueConfirmation(() => this.performDismissConfirmation(id));
  }

  private async performDismissConfirmation(id?: string): Promise<boolean> {
    const prior = this.confirmation;
    // A bound dismissal is also the write-authorization barrier. It must
    // distinguish a real dismissal from an already-replaced or expired modal
    // so an old gesture cannot authorize a different action in the background.
    if (!prior) return id === undefined;
    if (id !== undefined && prior.id !== id) return false;

    // Clear this synchronously before any awaited paint. A replacement
    // confirmation may be queued by a callback in the same event turn.
    this.confirmation = undefined;

    const result = await this.restoreAfterConfirmation(prior);
    // A pill deferred behind this gate rode along in the restore repaint.
    if (result && this.actionOverlay) prior.onDeferredPillVisible?.();
    return result;
  }

  private async restoreAfterConfirmation(prior: ConfirmationReturnState): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent()) return false;
    if (prior.surface.kind === 'list') {
      this.focusReturn = {
        streams: new Map(prior.streams),
        brightness: new Map(prior.brightness),
        kinds: new Map(prior.kinds), notificationId: prior.notificationId,
      };
      const restored = await this.renderList(prior.surface);
      const result = await this.repaintReplacementConfirmation(restored, displayCurrent);
      this.recordPageTransition('confirmation', 'list', 'dismiss_confirmation', result ? 'success' : 'failed');
      return result;
    }

    if (prior.surface.kind === 'navigation') {
      this.focusReturn = {
        streams: new Map(prior.streams),
        brightness: new Map(prior.brightness),
        kinds: new Map(prior.kinds), notificationId: prior.notificationId,
      };
      const restored = await this.renderNavigation(prior.surface);
      const result = await this.repaintReplacementConfirmation(restored, displayCurrent);
      this.recordPageTransition('confirmation', 'navigation', 'dismiss_confirmation', result ? 'success' : 'failed');
      return result;
    }

    if (prior.surface.kind === 'brainstorm') {
      this.focusReturn = {
        streams: new Map(prior.streams),
        brightness: new Map(prior.brightness),
        kinds: new Map(prior.kinds), notificationId: prior.notificationId,
      };
      const restored = await this.showBrainstorm(prior.surface.brainstorm);
      const result = await this.repaintReplacementConfirmation(restored, displayCurrent);
      this.recordPageTransition('confirmation', 'list', 'dismiss_confirmation', result ? 'success' : 'failed');
      return result;
    }

    if (prior.surface.kind === 'productivity') {
      this.focusReturn = {
        streams: new Map(prior.streams),
        brightness: new Map(prior.brightness),
        kinds: new Map(prior.kinds), notificationId: prior.notificationId,
      };
      const restored = await this.showProductivity(prior.surface.productivity);
      if (!displayCurrent()) return false;
      if (restored && this.isProductivityVisible(prior.surface.productivity)) {
        prior.onDeferredProductivityVisible?.();
      }
      const result = await this.repaintReplacementConfirmation(restored, displayCurrent);
      this.recordPageTransition('confirmation', 'list', 'dismiss_confirmation', result ? 'success' : 'failed');
      return result;
    }

    const liveStatus = this.captureLiveStatus();
    if (this.visual) {
      this.resetAll();
      this.surface = { kind: 'default' };
      this.streams = new Map(prior.streams);
      this.brightness = new Map(prior.brightness);
      this.kinds = new Map(prior.kinds);
      this.notificationId = prior.notificationId;
      if (liveStatus) {
        this.streams.set(ContainerId.StatusBar, liveStatus.text);
        if (liveStatus.brightness !== undefined) {
          this.brightness.set(ContainerId.StatusBar, liveStatus.brightness);
        }
      }
      const result = await this.repaintReplacementConfirmation(await this.paintVisual(), displayCurrent);
      this.recordPageTransition('confirmation', 'overview', 'dismiss_confirmation', result ? 'success' : 'failed');
      return result;
    }
    const rebuilt = await this.rebuildPage(this.layouts.defaultLayout());
    if (!rebuilt) {
      this.recordPageTransition('confirmation', 'overview', 'dismiss_confirmation', 'failed');
      return false;
    }
    this.resetAll();
    this.surface = { kind: 'default' };
    for (const id of this.pendingPaint.keys()) this.cancelPending(id);

    // Status can keep changing while the gate is open. Preserve that live
    // plugin-owned line rather than restoring a stale snapshot.
    this.streams = new Map(prior.streams);
    this.brightness = new Map(prior.brightness);
    this.kinds = new Map(prior.kinds);
    this.notificationId = prior.notificationId;
    if (liveStatus) {
      this.streams.set(ContainerId.StatusBar, liveStatus.text);
      if (liveStatus.brightness !== undefined) {
        this.brightness.set(ContainerId.StatusBar, liveStatus.brightness);
      }
    }

    let ok = true;
    for (const id of [
      ContainerId.Main,
      ContainerId.Context,
      ContainerId.Tray,
      ContainerId.Hint,
    ] as const) {
      this.streams.set(id, this.streams.get(id) ?? '');
      this.brightness.set(id, this.brightness.get(id) ?? DEFAULT_BRIGHTNESS[id]!);
      ok = (await this.paint(id)) && ok;
      if (!displayCurrent()) return false;
    }
    const result = await this.repaintReplacementConfirmation(ok, displayCurrent);
    this.recordPageTransition('confirmation', 'overview', 'dismiss_confirmation', result ? 'success' : 'failed');
    return result;
  }

  /**
   * Rebuild the page and retry once when a paint reports failure.
   *
   * textContainerUpgrade returns false when the container does not exist —
   * which happens whenever the host already had a startup page and our
   * createStartUpPageContainer therefore returned `invalid` (it is one-shot
   * PER HOST SESSION, not per page load: a WebView reload, a relaunch, or a
   * second plugin instance all land here). BridgeClient's rebuild fallback
   * reports success without necessarily materialising the containers, so
   * every later paint failed SILENTLY and the HUD went dead for the rest of
   * the session — observed in the simulator as 382 consecutive
   * "container N not found" warnings behind a blank display.
   *
   * Every paint result is now checked, so any cause of a missing page
   * self-heals on the next write.
   */
  /** Every presenter page-definition write shares the image lane's native
   * barrier. A stalled upload aborts recovery instead of racing a rebuild. */
  private async rebuildPage(layout: RebuildPageContainer): Promise<boolean> {
    const current = this.captureDisplayGuard();
    if (!current()) return false;
    const replace = () => this.client.require().rebuildPageContainer(layout);
    if (!this.visual) return replace();
    if (!this.visual.withPageTransition) return false;
    try {
      const rebuilt = await this.visual.withPageTransition(replace, current);
      return rebuilt && current();
    } catch { return false; }
  }

  private healing = false;

  private async healAndRetry(containerId: number): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent()) return false;
    if (this.healing) {
      this.recordSelfHeal('skipped', containerId);
      return false; // one attempt; never recurse
    }
    this.healing = true;
    this.recordSelfHeal('attempt', containerId);
    try {
      const layout = this.visual
        ? this.layouts.visualLayout()
        : this.confirmation
        ? this.layouts.confirmationLayout(this.confirmation.prompt)
        : this.surface.kind === 'list' && !this.nativeReplyPage
          ? this.nativeListLayout(this.surface)
          : this.layouts.defaultLayout();
      if (import.meta.env.DEV) console.info('[blank-trace]', Date.now(), 'heal-rebuild-start');
      const rebuilt = await this.rebuildPage(layout);
      if (import.meta.env.DEV) console.info('[blank-trace]', Date.now(), 'heal-rebuild-result', rebuilt);
      if (!rebuilt || !displayCurrent()) {
        this.recordSelfHeal('abandoned', containerId);
        return false;
      }
      this.visual?.forgetPage?.();
      console.warn('[HudPresenter] page was missing — rebuilt and repainting');
      const recovered = this.visual
        ? await this.paintVisual()
        : await this.paint(containerId);
      this.recordSelfHeal(recovered ? 'recovered' : 'abandoned', containerId);
      return recovered;
    } catch {
      this.recordSelfHeal('abandoned', containerId);
      return false;
    } finally {
      if (displayCurrent()) this.healing = false;
    }
  }

  private async paint(containerId: number): Promise<boolean> {
    this.removeRetiredNotifications();
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent()) return false;
    if (this.assistantConversation.isActive() && !this.restoringAssistantConversation) return true;
    if (this.pageOwner !== 'visual' && !this.visualPagePending()) return true;
    if (this.visual) return this.paintVisual();
    // No custom renderer: retain native text compatibility updates.
    // The text compatibility surface owns the same transient reply as the
    // visual HUD. Keep page content underneath it for dismissal, and never
    // cover an exclusive confirmation with an assistant answer.
    const overlay = !this.visual && !this.confirmation && containerId === ContainerId.Main
      ? this.assistantOverlay?.text
      : undefined;
    const stored = overlay ?? this.streams.get(containerId) ?? '';
    // Native compatibility shares the same quiet top line as the raster
    // shell. Keep transient activity in the conversation, and use the
    // structured microphone state rather than inferring it from wording.
    const full = containerId === ContainerId.StatusBar
      ? [stored.match(/^\d{1,2}:\d{2}/)?.[0], `MIC ${this.micOn ? 'ON' : 'OFF'}`,
        stored.match(/bat\s*\d+%\+?/i)?.[0]].filter(Boolean).join(' · ')
      : containerId === ContainerId.Hint ? GesturePolicy.formatHint(stored) : stored;
    // Overflow keeps the TAIL: mid-stream, the newest words are the ones
    // being read; the opening lines are already consumed.
    const content = fitUtf8Tail(full, MAX_UPGRADE_BYTES);
    const previous = this.lastLength.get(containerId) ?? 0;
    this.lastPaint.set(containerId, Date.now());
    const level = this.brightness.get(containerId);
    const notificationId = containerId === ContainerId.Main && this.hasVisibleNotification() ? this.notificationId : undefined;
    const ok = await this.client.require().textContainerUpgrade(
      new TextContainerUpgrade({
        containerID: containerId,
        contentOffset: 0,
        // Overwrite whatever was there before, otherwise stale tail text survives.
        contentLength: Math.max(previous, content.length),
        content,
        // Omitted = keep the container's current brightness (SDK contract).
        ...(level !== undefined ? { textColor: level } : {}),
      }),
    );
    if (!displayCurrent()) return false;
    if (ok && containerId === ContainerId.Main && notificationId === this.notificationId) {
      this.paintedNotificationId = notificationId;
    }
    this.lastLength.set(containerId, content.length);
    if (!ok && !this.healing) {
      // The container is gone. Rebuild the page and paint again, rather
      // than dropping this and every future write on the floor.
      return this.healAndRetry(containerId);
    }
    return ok;
  }

  private tracePage(): G2TracePage {
    if (this.confirmation) return 'confirmation';
    if (this.surface.kind === 'default') return 'overview';
    // The trace vocabulary predates this visual page; it remains content-free.
    return this.surface.kind === 'brainstorm' || this.surface.kind === 'productivity'
      ? 'list'
      : this.surface.kind;
  }

  private recordPageTransition(
    from: G2TracePage,
    to: G2TracePage,
    reason: 'show_list' | 'show_overview' | 'show_confirmation' | 'show_navigation' | 'dismiss_confirmation',
    outcome: 'success' | 'failed',
  ): void {
    if (from === to) return;
    this.diagnostics?.record({ type: 'page_transition', from, to, reason, outcome });
  }

  private recordSelfHeal(
    decision: 'attempt' | 'recovered' | 'abandoned' | 'skipped',
    containerId: number,
  ): void {
    this.diagnostics?.record({
      type: 'self_heal',
      surface: this.tracePage(),
      target: this.visual ? 'visual' : 'text',
      decision,
      containerId,
    });
  }

  private cancelPending(containerId: number): void {
    const timer = this.pendingPaint.get(containerId);
    if (timer !== undefined) {
      clearTimeout(timer);
      this.pendingPaint.delete(containerId);
    }
  }

  private deferText(
    text: string,
    containerId: ContainerId,
    textColor?: number,
    contentKind?: HudContentKind,
    notificationId?: string,
  ): void {
    const deferred = this.confirmation;
    if (!deferred) return;
    deferred.streams.set(containerId, text);
    if (textColor !== undefined) deferred.brightness.set(containerId, textColor);
    else deferred.brightness.delete(containerId);
    if (containerId === ContainerId.Main) {
      deferred.kinds.set(containerId, contentKind ?? 'assistant');
      deferred.notificationId = contentKind === 'notification' && /^[a-f0-9]{32}$/i.test(notificationId ?? '')
        ? notificationId : undefined;
    }
  }

  private deferFocusText(
    text: string,
    containerId: ContainerId,
    textColor?: number,
    contentKind?: HudContentKind,
    notificationId?: string,
  ): void {
    const deferred = this.ensureFocusReturn();
    deferred.streams.set(containerId, text);
    if (textColor !== undefined) deferred.brightness.set(containerId, textColor);
    else deferred.brightness.delete(containerId);
    if (containerId === ContainerId.Main) {
      deferred.kinds.set(containerId, contentKind ?? 'assistant');
      deferred.notificationId = contentKind === 'notification' && /^[a-f0-9]{32}$/i.test(notificationId ?? '')
        ? notificationId : undefined;
    }
  }

  private ensureFocusReturn(): HudContentState {
    this.focusReturn ??= { streams: new Map(), brightness: new Map(), kinds: new Map() };
    return this.focusReturn;
  }

  private captureLiveStatus(): { text: string; brightness?: number } | undefined {
    const text = this.streams.get(ContainerId.StatusBar);
    if (text === undefined) return undefined;
    return { text, brightness: this.brightness.get(ContainerId.StatusBar) };
  }

  private async restoreLiveStatus(
    status: { text: string; brightness?: number } | undefined,
  ): Promise<boolean> {
    if (!status) return true;
    this.streams.set(ContainerId.StatusBar, status.text);
    if (status.brightness !== undefined) {
      this.brightness.set(ContainerId.StatusBar, status.brightness);
    }
    return this.paint(ContainerId.StatusBar);
  }

  private enqueueConfirmation(operation: () => Promise<boolean>): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    const owned = () => displayCurrent() ? operation() : Promise.resolve(false);
    const next = this.confirmationQueue.then(owned, owned);
    this.confirmationQueue = next.then(() => undefined, () => undefined);
    return next;
  }

  private async repaintReplacementConfirmation(
    restored: boolean, displayCurrent: () => boolean,
  ): Promise<boolean> {
    if (!displayCurrent()) return false;
    const replacement = this.confirmation;
    if (!replacement) return restored;
    // The queued dismissal belongs to the confirmation that was visible when
    // its gesture began. If another confirmation claimed the decision surface
    // while the prior page was restoring, render that replacement but report
    // the old dismissal as unsuccessful. Returning success here would let an
    // old tap authorize action A after the wearer is already reviewing B.
    await this.renderConfirmation(replacement.id);
    return false;
  }

  private initialListSelection(items: readonly string[], options: ListSurfaceOptions): number {
    const requested = options.initialSelectedIndex;
    if (!this.visual || options.nativeFirmwareList || requested === undefined || !Number.isInteger(requested)) return 0;
    return Math.max(0, Math.min(Math.max(0, items.length - 1), requested));
  }

  private resolveListOptions(options: ListSurfaceOptions): ResolvedListSurfaceOptions {
    const planningGrid = normalizePlanningGrid(options.planningGrid);
    const planningEntries = Array.isArray(options.planningEntries)
      ? options.planningEntries.slice(0, 20).map(entry => entry && typeof entry.title === 'string'
        && typeof entry.time === 'string' && (entry.kind === 'event' || entry.kind === 'reminder')
        ? { title: entry.title.slice(0, 200), time: entry.time.slice(0, 32), kind: entry.kind } : null)
      : undefined;
    return {
      title: (options.title?.trim() || DEFAULT_LIST_TITLE).slice(0, MAX_FOCUS_COPY_CHARS),
      ...(options.contextLabel?.trim()
        ? { contextLabel: Array.from(options.contextLabel.trim()).slice(0, 96).join('') }
        : {}),
      ...(Number.isInteger(options.controlStartIndex) && options.controlStartIndex! >= 0
        ? { controlStartIndex: Math.min(20, options.controlStartIndex!) }
        : {}),
      hint: (options.hint?.trim() || DEFAULT_LIST_HINT).slice(0, MAX_FOCUS_HINT_CHARS),
      selectable: options.selectable ?? false,
      ...(options.nativeFirmwareList ? { nativeFirmwareList: true } : {}),
      ...(options.nativeFirmwareList && options.nativeReaderPage ? { nativeReaderPage: true } : {}),
      ...(options.railTab ? { railTab: options.railTab } : {}),
      ...(planningGrid ? { planningGrid } : {}),
      ...(planningEntries ? { planningEntries } : {}),
      // The pane rides only with a valid grid: hover state without dates
      // would put the renderer on the dashboard path with nothing to draw.
      ...(planningGrid && options.planningPane
        ? { planningPane: { ...options.planningPane } }
        : {}),
    };
  }

  private visualFrame(): HudVisualFrame {
    const status = this.streams.get(ContainerId.StatusBar) ?? '';
    if (this.confirmation) {
      return {
        kind: 'confirmation',
        status,
        micOn: this.micOn,
        prompt: this.confirmation.prompt,
        reviewDomain: this.productivityReviewDomain(this.confirmation.prompt),
        ...(this.confirmation.diagnosticKind
          ? { diagnosticKind: this.confirmation.diagnosticKind }
          : {}),
      };
    }
    if (this.promptQuestion) {
      if (this.promptQuestion.inlineTerminal && this.surface.kind === 'brainstorm'
        && nativeDocument(this.surface.brainstorm)) {
        return {kind: 'brainstorm', status, micOn: this.micOn,
          brainstorm: {...this.cloneBrainstorm(this.surface.brainstorm),
            terminalPrompt: {id: this.promptQuestion.id, question: this.promptQuestion.question}}};
      }
      return {
        kind: 'prompt_question', status, micOn: this.micOn,
        promptId: this.promptQuestion.id, question: this.promptQuestion.question,
      };
    }
    if (this.surface.kind === 'list') {
      const railTab = this.surface.options.railTab;
      return this.withOverlays({
        kind: 'list',
        status,
        micOn: this.micOn,
        title: this.surface.options.title,
        contextLabel: this.surface.options.contextLabel,
        controlStartIndex: this.surface.options.controlStartIndex,
        planningEntries: this.surface.options.planningEntries,
        items: [...this.surface.items],
        selectedIndex: this.surface.selectedIndex,
        selectable: this.surface.options.selectable,
        hint: this.surface.options.hint,
        planningGrid: this.surface.options.planningGrid,
        planningPane: this.surface.options.planningPane,
        ...(railTab ? { rail: this.rail.visualFor(railTab) } : {}),
      });
    }
    if (this.surface.kind === 'navigation') {
      return this.withOverlays({
        kind: 'navigation',
        status,
        micOn: this.micOn,
        navigation: this.cloneNavigation(this.surface.navigation),
      });
    }
    if (this.surface.kind === 'brainstorm') {
      return this.withOverlays({
        kind: 'brainstorm',
        status,
        micOn: this.micOn,
        brainstorm: this.cloneBrainstorm(this.surface.brainstorm),
      });
    }
    if (this.surface.kind === 'productivity') {
      return this.withOverlays({
        kind: 'productivity',
        status,
        micOn: this.micOn,
        productivity: this.cloneProductivity(this.surface.productivity),
      });
    }
    return this.withOverlays({
      kind: 'overview',
      status,
      micOn: this.micOn,
      ...(this.ambient ? { ambient: this.ambient } : {}),
      mainBrightness: this.brightness.get(ContainerId.Main)
        ?? DEFAULT_BRIGHTNESS[ContainerId.Main]!,
      main: this.streams.get(ContainerId.Main) ?? '',
      mainKind: this.kinds.get(ContainerId.Main),
      context: this.streams.get(ContainerId.Context) ?? '',
      tray: this.streams.get(ContainerId.Tray) ?? '',
      hint: this.streams.get(ContainerId.Hint) ?? '',
      rail: this.rail.visual(),
    });
  }

  private withOverlays<T extends HudVisualFrame>(frame: T): T {
    let out = frame;
    if (this.actionOverlay) {
      out = {
        ...out,
        actionOverlay: {
          productivity: this.cloneProductivity(this.actionOverlay.productivity),
        },
      };
    }
    if (this.assistantOverlay) {
      out = {
        ...out,
        assistantOverlay: {
          text: this.assistantOverlay.text,
          presentation: this.assistantOverlay.presentation,
          revision: this.assistantOverlay.revision,
        },
      };
    }
    return out;
  }

  /** Restore the current owner after host UI may have erased its pixels.
   * A remembered upload is no longer evidence that its tile is on the glass.
   * Invalidate before either renderer so unchanged content is delivered again. */
  async repaintRestoredSurface(): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent()) return false;
    // A native menu may close halfway through a capability paint. Cancelling
    // that paint makes its caller mistake our invalidation for a missing page
    // and rebuild an otherwise healthy layout. Let the lane settle first.
    if (this.visual?.whenIdle && !await this.visual.whenIdle()) return false;
    if (!displayCurrent()) return false;
    this.visual?.forgetPage?.();
    if (this.assistantConversation.isActive()) return this.assistantConversation.repaint();
    if (this.surface.kind === 'brainstorm'
      && nativeDocument(this.surface.brainstorm)) {
      if (this.nativeTerminal.isActive()) return this.nativeTerminal.repaint();
      return this.nativeTerminal.present(this.surface.brainstorm,
        this.streams.get(ContainerId.StatusBar), displayCurrent);
    }
    if (this.surface.kind === 'list' && this.surface.options.nativeFirmwareList) {
      return this.replaceNativePage(
        this.nativeListLayout(this.surface),
        'terminal-list-native',
      );
    }
    return this.paintVisual();
  }

  private async restoreAfterAssistantConversation(): Promise<boolean> {
    this.restoringAssistantConversation = true;
    try {
      if (this.visual) {
        const painted = await this.restoreRetainedPage();
        if (painted) {
          this.armThinkingTicker();
          this.armPanelTicker();
        }
        return painted;
      }

      const surface = this.surface;
      const rebuilt = surface.kind === 'list'
        ? await this.rebuildPage(
          this.nativeListLayout(surface),
        )
        : await this.rebuildPage(this.layouts.defaultLayout());
      if (!rebuilt) return false;
      this.lastLength.clear();
      let painted = true;
      const ids = surface.kind === 'list'
        ? [ContainerId.StatusBar]
        : [ContainerId.StatusBar, ContainerId.Main, ContainerId.Context, ContainerId.Tray, ContainerId.Hint];
      for (const id of ids) {
        if (this.streams.has(id)) painted = (await this.paint(id)) && painted;
      }
      return painted;
    } finally {
      this.restoringAssistantConversation = false;
    }
  }

  /** Restore pixels once without starting another recovery cycle. A restored
   * notification becomes actionable only after this replacement paint. */
  private async paintRollbackSurface(): Promise<boolean> {
    if (!this.visual) return false;
    const displayCurrent = this.captureDisplayGuard();
    const surface = this.surface;
    const notificationId = this.hasVisibleNotification() ? this.notificationId : undefined;
    const current = () => displayCurrent() && this.surface === surface
      && notificationId === (this.hasVisibleNotification() ? this.notificationId : undefined);
    if (!current()) return false;
    const frame = this.visualFrame();
    const painted = await (this.visual.paintExact?.(frame, current) ?? this.visual.paint(frame));
    if (!painted || !current()) return false;
    this.paintedNotificationId = notificationId;
    return true;
  }

  /** The text overview's last rendered content, so an unchanged repaint
   * costs nothing. Without this the swipe handler's several paint calls
   * each became a rebuildPageContainer — measured at three per swipe,
   * 495ms, worse than the four tiles it replaced. */
  private lastOverviewText?: string;
  /** The band contents of the overview page on the glass, by container id,
   * so a change to ONE band can be an in-place upgrade instead of a rebuild. */
  private lastOverviewBands?: Map<number, { content: string; textColor?: number }>;
  /** Whether the text overview is what the glass is PHYSICALLY showing.
   * The signature alone is not enough: after a native list took the page,
   * an unchanged signature made Back skip the rebuild and the wearer got a
   * blank screen — the signature matched a page that was no longer there. */
  private overviewTextLive = false;

  private async paintOverviewAsText(
    frame: Extract<HudVisualFrame, { kind: 'overview' }>,
  ): Promise<boolean> {
    // Taken BEFORE any await. A status push that started painting the
    // overview a moment before the wearer expanded a tab used to finish
    // after the firmware list was on the glass and write the overview over
    // it — the list appeared and vanished, and the menu closed as failed.
    // The display guard alone was not enough, because rebuildPage only
    // consults it for the return value; the write still went out. So the
    // write itself is conditional at the instant it would fire.
    const current = this.captureDisplayGuard();
    const rail = frame.rail;
    const content = rail?.content;
    // The same content-priority rules the canvas overview uses: boot and
    // link states first, then a notification/reply in the main region, then
    // the resting dashboard. Sharing the resolver is what keeps a Mac reply
    // or a Home notification from being silently swallowed by the tab pane —
    // the first cut of this path did exactly that.
    const overview = resolveHudOverview(frame);
    const parsed = parseOverviewStatus(frame.status);
    // Mic state is structured (frame.micOn), never inferred from status
    // copy — same contract as the native compatibility status line.
    // The date rides the status line and the time the clock column: the
    // 2026-10-05 audit caught the time printed in both. One minute tick now
    // changes ONE band (an in-place upgrade) instead of two (a rebuild).
    const status = [
      parsed.date,
      `MIC ${frame.micOn ? 'ON' : 'OFF'}`,
      /bat\s*\d+%\+?/i.exec(frame.status)?.[0],
    ].filter(Boolean).join(' · ');
    const tabRows = rail?.activeTab === 'pomodoro'
      ? this.pomodoroCardRows(content).join('\n')
      : content?.rows?.length
        ? content.rows.join('\n')
        : (content?.empty ?? '');
    const tabs = PANEL_TABS.map((tab: (typeof PANEL_TABS)[number]) => ({
      label: PANEL_TAB_TITLES[tab], active: rail?.activeTab === tab,
    }));
    const ambientLine = frame.ambient
      ? `${frame.ambient.temperature} ${frame.ambient.condition}`.trim()
      : '';
    const unread = frame.ambient?.unread ?? 0;
    const extra = unread > 0 ? `${unread} unread` : '';
    const tabCard = {
      title: rail ? PANEL_TAB_TITLES[rail.activeTab] : '',
      body: content?.headline && rail?.activeTab !== 'pomodoro' ? `${content.headline}\n${tabRows}` : tabRows,
    };
    // Who owns the pane, in the canvas overview's own priority order:
    //   - a link/boot state (Offline, Reconnecting, Connecting): nothing
    //     else on the screen is valid, so its copy takes the pane;
    //   - a notification, message or reply: the wearer must SEE it;
    //   - otherwise the active tab's card — the Dashboard's whole purpose.
    // The "Ready" resting copy is NOT a pane owner. The first cut let it be
    // one, and the wearer swiped to NEWS and kept reading "Ask JOAT anything".
    const boot = overview.kind === 'empty' && overview.title !== 'Ready';
    const view = boot
      ? {
        status, clock: parsed.time, date: parsed.date, extra, ambient: ambientLine, tabs,
        paneTitle: overview.title.toUpperCase(),
        paneBody: `${overview.primary}\n${overview.recovery}`,
        paneBrightness: frame.mainBrightness,
        hint: overview.hint,
      }
      : overview.kind === 'content' && overview.main
        ? {
          status, clock: parsed.time, date: parsed.date, extra, ambient: ambientLine, tabs,
          paneTitle: overview.main.label,
          paneBody: [overview.main.value, overview.tray].filter(Boolean).join('\n'),
          paneBrightness: frame.mainBrightness,
          hint: frame.hint,
        }
        : {
          status, clock: parsed.time, date: parsed.date, extra,
          // Resting: the ambient row carries the ready copy when the Mac has
          // sent no weather, so the clock column is never a bare number.
          ambient: ambientLine
            || (overview.kind === 'empty' ? overview.primary
              : overview.context ? `${overview.context.label}: ${overview.context.value}` : ''),
          tabs,
          paneTitle: tabCard.title,
          paneBody: tabCard.body,
          paneBrightness: frame.mainBrightness,
          hint: overview.kind === 'empty' ? overview.hint : frame.hint,
        };
    const signature = JSON.stringify(view);
    if (this.lastOverviewText === signature && this.overviewTextLive
      && this.pageOwner === 'visual') return true;
    const layout = this.layouts.overviewTextLayout(view);
    const bands = new Map((layout.textObject ?? []).map((t) => [
      t.containerID ?? 0, { content: t.content ?? '', textColor: t.textColor },
    ]));
    // Ours while Home owns the glass — either the visual page or a firmware
    // list page that Home is replacing (visualPagePending). A list that is
    // live under a list SURFACE is never ours: that is the stomp this guard
    // exists for.
    const stillOurs = (): boolean => current()
      && (this.pageOwner === 'visual' || this.visualPagePending());
    if (!stillOurs()) return false;
    // ONE band changed on a live overview — a Pomodoro tick, a fresh
    // headline, a reply — is one textContainerUpgrade (~83ms), not a page
    // rebuild (~165ms). Two or more changed bands (a minute tick moves the
    // status bar and the clock) cost more as upgrades than as one rebuild
    // (even-g2-notes performance: break-even at two containers).
    const live = this.overviewTextLive && this.pageOwner === 'visual' && this.lastOverviewBands;
    const changed = live
      ? [...bands].filter(([id, band]) => {
        const was = this.lastOverviewBands!.get(id);
        return was?.content !== band.content || was?.textColor !== band.textColor;
      })
      : [];
    if (live && changed.length === 1 && (this.lastOverviewBands!.size === bands.size)) {
      const [[containerID, band]] = changed as [[number, { content: string; textColor?: number }]];
      const previous = this.lastOverviewBands!.get(containerID)?.content ?? '';
      const upgraded = await this.client.require().textContainerUpgrade(new TextContainerUpgrade({
        containerID, contentOffset: 0,
        contentLength: Math.max(previous.length, band.content.length),
        content: band.content,
        ...(band.textColor !== undefined ? { textColor: band.textColor } : {}),
      }));
      if (upgraded && stillOurs()) {
        this.lastOverviewText = signature;
        this.lastOverviewBands = bands;
        this.armPanelTicker();
        return true;
      }
      if (!stillOurs()) return false;
      // The container is gone or refused: fall through to a full rebuild.
    }
    const replace = (): Promise<boolean> => (stillOurs()
      ? this.client.require().rebuildPageContainer(layout)
      : Promise.resolve(false));
    let ok = false;
    if (!this.visual) ok = await replace();
    else if (this.visual.withPageTransition) {
      try { ok = await this.visual.withPageTransition(replace, stillOurs); } catch { ok = false; }
    }
    if (ok) {
      this.lastOverviewText = signature;
      this.lastOverviewBands = bands;
      this.overviewTextLive = true;
      // This page IS the visual owner's page. rebuildVisualPage() is what
      // normally records that, and this path replaces it — leaving the
      // owner stale made every later paint misjudge who held the glass.
      this.pageOwner = 'visual';
      this.visual?.forgetPage?.();
      this.armPanelTicker();
    }
    return ok;
  }

  private async paintVisual(localTick = false): Promise<boolean> {
    this.removeRetiredNotifications();
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent()) return false;
    if (this.assistantConversation.isActive() && !this.restoringAssistantConversation) return true;
    if (this.pageOwner !== 'visual' && !this.visualPagePending()) return true;
    if (!this.visual) return false;
    if (nativeOverviewEnabled && !this.confirmation && !this.promptQuestion) {
      const overview = this.visualFrame();
      if (overview.kind === 'overview' && !overview.actionOverlay && !overview.assistantOverlay) {
        return this.paintOverviewAsText(overview);
      }
      this.lastOverviewText = undefined;
    }
    // Not an overview frame, and a firmware list page is still on the glass:
    // the tiles need their image containers back first.
    if (this.visualPagePending() && !await this.rebuildVisualPage()) return false;
    if (!displayCurrent()) return false;
    const confirmation = this.confirmation;
    if (confirmation?.isCurrent) {
      if (!this.isConfirmationCurrent(confirmation)) return false;
      try {
        return await (this.visual.paintExact?.(this.visualFrame(), () => this.isConfirmationCurrent(confirmation))
          ?? Promise.resolve(false));
      } catch { return false; }
    }
    const prompt = this.promptQuestion;
    // Incidental status/content paints must retain the same question owner,
    // and must never heal to a different frame while reporting its success.
    if (prompt) {
      return this.visual.paintExact?.(this.visualFrame(), () => this.isPromptCurrent(prompt)) ?? false;
    }
    const notificationId = this.hasVisibleNotification() ? this.notificationId : undefined;
    const ok = await this.visual.paint(this.visualFrame(), localTick);
    if (ok && displayCurrent() && notificationId === this.notificationId) {
      this.paintedNotificationId = notificationId;
    }
    if (!displayCurrent()) return false;
    // Keep the local elapsed counter moving between Mac frames.
    this.armThinkingTicker();
    this.armPanelTicker();
    if (!ok && !this.healing) return this.healAndRetry(ContainerId.Main);
    return ok;
  }

  private cloneSurface(surface: HudSurface): HudSurface {
    if (surface.kind === 'default') return { kind: 'default' };
    if (surface.kind === 'navigation') {
      return { kind: 'navigation', navigation: this.cloneNavigation(surface.navigation) };
    }
    if (surface.kind === 'brainstorm') {
      return { kind: 'brainstorm', brainstorm: this.cloneBrainstorm(surface.brainstorm) };
    }
    if (surface.kind === 'productivity') {
      return { kind: 'productivity', productivity: this.cloneProductivity(surface.productivity) };
    }
    return {
      kind: 'list',
      items: [...surface.items],
      options: {
        ...surface.options,
        ...(surface.options.planningGrid
          ? { planningGrid: surface.options.planningGrid.map((cell) => ({ ...cell })) }
          : {}),
      },
      selectedIndex: surface.selectedIndex,
    };
  }

  private cloneContentState(content: HudContentState): HudContentState {
    return {
      streams: new Map(content.streams),
      brightness: new Map(content.brightness),
      kinds: new Map(content.kinds), notificationId: content.notificationId,
    };
  }

  /**
   * A JSON-safe projection of everything the wearer is currently looking at.
   *
   * The host's background migration reloads the plugin URL into a fresh
   * WebView, so anything held only in memory is gone on return. The internal
   * snapshot uses Maps, which do not survive a JSON round trip, so the
   * container keys become plain records here.
   */
  exportBackgroundState(): Record<string, unknown> {
    const content = {
      streams: new Map(this.streams),
      brightness: new Map(this.brightness),
      kinds: new Map(this.kinds),
    };
    // A restored WebView has no authority to acknowledge the old Mac's
    // notification. Persist the displaced assistant content instead, if any.
    if (content.kinds.get(ContainerId.Main) === 'notification') {
      content.streams.delete(ContainerId.Main);
      content.brightness.delete(ContainerId.Main);
      content.kinds.delete(ContainerId.Main);
      const prior = this.notificationReturn;
      if (prior && prior.kind !== 'notification') {
        if (prior.text !== undefined) content.streams.set(ContainerId.Main, prior.text);
        if (prior.brightness !== undefined) content.brightness.set(ContainerId.Main, prior.brightness);
        if (prior.kind !== undefined) content.kinds.set(ContainerId.Main, prior.kind);
      }
    }
    const assistantConversation = this.assistantConversation.exportState();
    const nativeTerminal = this.surface.kind === 'brainstorm'
      && nativeDocument(this.surface.brainstorm)
      ? this.nativeTerminal.exportState()
      : undefined;
    return {
      surface: this.cloneSurface(this.surface),
      streams: Object.fromEntries(content.streams),
      brightness: Object.fromEntries(content.brightness),
      kinds: Object.fromEntries(content.kinds),
      micOn: this.micOn,
      rail: this.rail.export(),
      ...(assistantConversation ? { assistantConversation } : {}),
      ...(nativeTerminal ? { nativeTerminal } : {}),
    };
  }

  /**
   * Adopt a snapshot from the page this one replaced, WITHOUT painting.
   *
   * The caller repaints once afterwards; painting here would race the startup
   * frame on the serialized BLE lane. Unknown or malformed fields are skipped
   * rather than trusted — a corrupt snapshot must not brick the surface.
   */
  importBackgroundState(saved: Record<string, unknown>): boolean {
    this.notificationId = undefined;
    this.paintedNotificationId = undefined;
    const surface = saved.surface;
    if (typeof surface !== 'object' || surface === null) return false;
    const kind = (surface as { kind?: unknown }).kind;
    if (typeof kind !== 'string') return false;
    this.surface = this.cloneSurface(surface as HudSurface);
    // A replacement WebView starts from the declared visual startup page.
    // repaintRestoredSurface performs the one native rebuild, if needed.
    this.pageOwner = 'visual';
    this.streams = numericMap(saved.streams, (v): v is string => typeof v === 'string');
    this.brightness = numericMap(saved.brightness, (v): v is number => typeof v === 'number');
    this.kinds = numericMap(
      saved.kinds,
      (v): v is HudContentKind => typeof v === 'string',
    );
    // Also reject notification Main content from snapshots written by older
    // builds. The authenticated Mac publishes current notification state.
    if (this.kinds.get(ContainerId.Main) === 'notification') {
      this.streams.delete(ContainerId.Main);
      this.brightness.delete(ContainerId.Main);
      this.kinds.delete(ContainerId.Main);
    }
    if (typeof saved.micOn === 'boolean') this.micOn = saved.micOn;
    if (typeof saved.rail === 'object' && saved.rail !== null) {
      this.rail.import(saved.rail as Record<string, unknown>);
    }
    if (saved.assistantConversation !== undefined
      && !this.assistantConversation.importState(saved.assistantConversation)) return false;
    if (saved.nativeTerminal !== undefined
      && !this.nativeTerminal.importState(saved.nativeTerminal)) return false;
    return true;
  }

  private capturePresenterSnapshot(): PresenterSnapshot {
    return {
      surface: this.cloneSurface(this.surface),
      streams: new Map(this.streams),
      brightness: new Map(this.brightness),
      kinds: new Map(this.kinds), notificationId: this.notificationId,
      ...(this.focusReturn ? { focusReturn: this.cloneContentState(this.focusReturn) } : {}),
    };
  }

  private restorePresenterSnapshot(snapshot: PresenterSnapshot): void {
    this.surface = this.cloneSurface(snapshot.surface);
    this.streams = new Map(snapshot.streams);
    this.brightness = new Map(snapshot.brightness);
    this.kinds = new Map(snapshot.kinds);
    this.notificationId = snapshot.notificationId;
    this.paintedNotificationId = undefined;
    this.focusReturn = snapshot.focusReturn
      ? this.cloneContentState(snapshot.focusReturn)
      : undefined;
  }

  private cloneNavigation(navigation: NavigationHudModel): NavigationHudModel {
    return { ...navigation, options: [...navigation.options] };
  }

  private cloneBrainstorm(brainstorm: BrainstormHudModel): BrainstormHudModel {
    return { ...brainstorm, collaborators: [...brainstorm.collaborators],
      messages: brainstorm.messages?.map(message => ({...message})) };
  }

  private cloneProductivity(productivity: ProductivityHudModel): ProductivityHudModel {
    return { ...productivity };
  }

  private productivityReviewDomain(prompt: string): ProductivityHudDomain | undefined {
    const normalized = prompt.trim().toLowerCase();
    if (normalized.startsWith('create event ')) return 'calendar';
    if (normalized.startsWith('create reminder ') || normalized.startsWith('complete reminder ')) {
      return 'reminders';
    }
    return undefined;
  }

  private resetAll(): void {
    this.nativeReplyPage = false;
    for (const id of this.pendingPaint.keys()) this.cancelPending(id);
    this.clearPanelTicker();
    this.lastLength.clear();
    this.streams.clear();
    this.lastPaint.clear();
    this.brightness.clear();
    this.kinds.clear();
    this.notificationId = undefined;
    this.paintedNotificationId = undefined;
  }
}
