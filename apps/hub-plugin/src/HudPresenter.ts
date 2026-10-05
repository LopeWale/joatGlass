import { TextContainerUpgrade, type RebuildPageContainer } from '@evenrealities/even_hub_sdk';
import { GesturePolicy } from './GesturePolicy';
import type { BridgeClient } from './BridgeClient';
import type { HudPageBuilder } from './HudPageBuilder';
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
  wrapConversationText,
  type ConversationExitResult,
} from './AssistantConversationSurface';
import { ASSISTANT_ANSWER_PAGE } from './HudPageBuilder';
import { NativeTerminalSurface } from './NativeTerminalSurface';
import { productivityActionHint, productivityPhaseLabel, usesActionPillLayout } from './HudCanvasRenderer';
import { navigationNativeBody, type NavigationHudModel } from './NavigationHud';
import {
  PanelRail,
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
  type PromptOwnerKind,
  type ProductivityHudModel,
} from './types';

const MAX_UPGRADE_CHARS = 2000;

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
  /** Show these rows as one firmware text page (a reader), paged in place. */
  nativeTextPage?: boolean;
  /** Open the text page on its last local page: the Mac answered a swipe up. */
  nativeTextPageFromEnd?: boolean;
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
  /** This page is a firmware text page when the visual surface can hand over. */
  nativeTextPage?: boolean;
  nativeTextPageFromEnd?: boolean;
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
  | 'terminal-conversation-native'
  | 'confirmation-native'
  | 'overlay-native'
  | 'prompt-native'
  | 'text-page-native'
  | 'navigation-native';

/** Firmware pages that hold JOAT content of their own (a list, a reader, a
 * navigation card). An image paint over one of them must recreate the four
 * image containers first; a receipt or an outcome strip over one of them is
 * drawn as a firmware page instead, because an image strip cannot land on
 * a page that has no image containers and a rebuild costs four tiles. */
const NATIVE_CONTENT_OWNERS: ReadonlySet<DisplayPageOwner> = new Set<DisplayPageOwner>([
  'terminal-list-native', 'text-page-native', 'navigation-native',
]);

/** How long an outcome strip stays on a firmware page before the page
 * beneath returns; the raster pill's own failsafe is eight seconds. */
const NATIVE_PILL_DISMISS_MS = 5_000;

/** Local paging state of a Mac-paged reader shown as a firmware text page. */
interface NativeTextPageState {
  /** The rows this page was paged from: a restore of the same rows resumes
   * the page the wearer was reading; new rows start at the first page. */
  source: string;
  pages: string[];
  page: number;
  body: string;
  hint: string;
}

function textPageHint(page: number, count: number, macHint: string): string {
  // The Mac still writes "left x2"; every page says "double-click".
  const hint = GesturePolicy.formatHint(macHint);
  return count > 1 ? `${page + 1}/${count} · ${hint}` : hint;
}

/** Split an answer into firmware pages with the installed glyph metrics. */
export function paginateAnswer(
  text: string,
  lineCapacity: number = ASSISTANT_ANSWER_PAGE.lineCapacity,
  width: number = ASSISTANT_ANSWER_PAGE.textWidth,
): string[] {
  const rows = wrapConversationText(text, width);
  const pages: string[] = [];
  for (let start = 0; start < rows.length; start += lineCapacity) {
    pages.push(rows.slice(start, start + lineCapacity).join('\n'));
  }
  return pages.length > 0 ? pages : [' '];
}

function answerPageHint(page: number, count: number): string {
  return count > 1 ? `${page + 1}/${count} · swipe: page · tap: dismiss` : 'tap: dismiss';
}

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
  /** The answer as the firmware page shows it: pages and the one on glass. */
  private nativeOverlay?: { revision: number; pages: string[]; page: number; body: string; hint: string };
  private promptQuestion?: PromptQuestionState;
  private promptQuestionPaint?: Promise<boolean>;
  private nativeTextPage?: NativeTextPageState;
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
      && this.usesNativeConversation(this.surface.brainstorm)) {
      return this.nativeTerminal.present(this.surface.brainstorm,
        this.streams.get(ContainerId.StatusBar), displayCurrent);
    }
    if (this.surface.kind === 'list' && this.surface.options.nativeFirmwareList) {
      return this.replaceNativePage(this.nativeListLayout(this.surface), 'terminal-list-native');
    }
    if (this.surface.kind === 'list' && this.usesNativeTextPage(this.surface.options)) {
      return this.renderNativeTextPage(this.surface);
    }
    if (this.surface.kind === 'productivity' && this.visual?.withPageTransition !== undefined
      && !usesActionPillLayout(this.surface.productivity.phase)) {
      return this.renderNativeProductivityPage(this.surface);
    }
    if (this.surface.kind === 'navigation' && this.visual?.withPageTransition !== undefined) {
      const native = navigationNativeBody(this.surface.navigation);
      if (native !== undefined) return this.renderNativeNavigationPage(this.surface, native);
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
    this.nativeOverlay = undefined;
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

  private captureDisplayGuard(): () => boolean {
    const revision = this.displayRevision;
    const active = this.displayActive;
    const bridgeCurrent = this.client.captureDisplayGuard?.() ?? (() => true);
    return () => active && this.displayActive && revision === this.displayRevision && bridgeCurrent();
  }

  /** The custom list owns its cursor; the compatibility list is firmware-owned.
   * Without `next` this describes the page on the glass. With `next` it
   * describes the page about to be shown: a menu deciding how the NEXT page
   * selects must not read the answer off the current one, or a raster
   * conversation opened from a firmware picker inherits the picker's cursor. */
  usesCustomListSelection(next?: { nativeFirmwareList?: boolean; nativeTextPage?: boolean }): boolean {
    if (this.visual === undefined) return false;
    if (next) return next.nativeFirmwareList !== true && !this.usesNativeTextPage(next);
    return !(this.surface.kind === 'list'
      && (this.surface.options.nativeFirmwareList || this.usesNativeTextPage(this.surface.options)));
  }

  /** The Agent Terminal is always Even's own text page. A Brainstorm is the
   * same page whenever the visual surface can hand the page over under its
   * transition barrier (or there is no visual surface); otherwise it keeps
   * the raster transcript. */
  private usesNativeConversation(brainstorm: Pick<BrainstormHudModel, 'screen'>): boolean {
    if (brainstorm.screen === 'agent_terminal') return true;
    return this.visual === undefined || this.visual.withPageTransition !== undefined;
  }

  /** A reader is Even's own text page whenever the visual surface can hand
   * the page over under its transition barrier; otherwise it stays raster. */
  private usesNativeTextPage(options: { nativeTextPage?: boolean }): boolean {
    return options.nativeTextPage === true && this.visual?.withPageTransition !== undefined;
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
  showPromptQuestion(promptId: string, question: string, isCurrent: () => boolean, ownerKind?: PromptOwnerKind): Promise<boolean> {
    if (this.confirmation || !promptId || !question.trim() || question.length > 1000) {
      return Promise.resolve(false);
    }
    if (ownerKind === 'terminal') {
      if (!this.nativeTerminal.isActive() || this.surface.kind !== 'brainstorm'
        || this.surface.brainstorm.screen !== 'agent_terminal') return Promise.resolve(false);
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
    const nativePrompt = this.visual?.withPageTransition !== undefined;
    if (!this.visual?.paintExact && !nativePrompt) return Promise.resolve(false);
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
    const paint = nativePrompt
      ? this.renderNativePromptQuestion(prompt, this.promptQuestionPaint)
      : this.renderPromptQuestion(prompt, this.promptQuestionPaint);
    prompt.paint = paint;
    this.promptQuestionPaint = paint;
    return paint;
  }

  /** The question as Even's own text page: one rebuild in, and the retained
   * feature (image page, firmware list or terminal) returns on dismissal.
   * The raster prompt refused a question that did not fit its region; the
   * text page refuses one that needs more than one firmware page. */
  private async renderNativePromptQuestion(prompt: PromptQuestionState, previous?: Promise<boolean>): Promise<boolean> {
    let painted = false;
    try {
      await previous?.catch(() => false);
      await this.assistantOverlayPaint?.catch(() => false);
      if (!this.isPromptCurrent(prompt)) return false;
      const pages = paginateAnswer(prompt.question);
      if (pages.length !== 1) return false;
      // A native conversation page (Agent Terminal, Brainstorm) must hand
      // over first, as it does for a confirmation, or its surface keeps
      // writing into containers the question page has replaced.
      if (!await this.prepareNativeTerminalHandoff()) return false;
      if (!this.isPromptCurrent(prompt)) return false;
      const layout = this.layouts.promptQuestionLayout(pages[0] ?? ' ', this.streams.get(ContainerId.StatusBar));
      painted = await this.replaceNativePage(layout, 'prompt-native');
      return painted && this.isPromptCurrent(prompt);
    } catch {
      return false;
    } finally {
      if ((!painted || !this.isPromptCurrent(prompt)) && this.promptQuestion === prompt) {
        this.promptQuestion = undefined;
        await this.restoreRetainedPage().catch(() => false);
      }
    }
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
    if (this.pageOwner === 'prompt-native') return this.restoreRetainedPage();
    return this.paintVisual();
  }

  private isPromptCurrent(prompt: PromptQuestionState): boolean {
    try {
      return this.promptQuestion === prompt && !this.confirmation && prompt.isCurrent()
        && (!prompt.inlineTerminal || (this.nativeTerminal.isActive()
          && this.surface.kind === 'brainstorm'
          && this.surface.brainstorm.screen === 'agent_terminal'
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
    // Over a firmware page the overlay is a firmware page too (see
    // usesNativeAnswerPage); the image rebuild would be four wasted tiles.
    const overNativePage = !this.nativeTerminal.isActive()
      && this.visual?.withPageTransition !== undefined
      && (NATIVE_CONTENT_OWNERS.has(this.pageOwner) || this.pageOwner === 'overlay-native');
    if (!overNativePage && !await this.prepareNativeTerminalHandoff()) return false;
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
      if (!NATIVE_CONTENT_OWNERS.has(this.pageOwner)) return true;
      if (!this.visual) {
        // The text compatibility surface replaces the firmware list with its
        // own rebuild; hand the page back so its text writes are not skipped.
        this.pageOwner = 'visual';
        return true;
      }
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
  /** The firmware list page for a native list surface, carrying the live
   * status line: `paint()` is a no-op while the firmware owns the page. */
  private nativeListLayout(surface: Extract<HudSurface, { kind: 'list' }>): RebuildPageContainer {
    return this.layouts.listLayout(surface.items, {
      ...surface.options,
      status: this.streams.get(ContainerId.StatusBar),
    });
  }

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
    const pageCount = this.nativeOverlay !== undefined
      ? this.nativeOverlay.pages.length
      : (this.visual?.assistantOverlayPageCount?.() ?? 1);
    if (pageCount > 1) return true;
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
    const paint = this.visual
      ? this.usesNativeAnswerPage() || this.pageOwner === 'overlay-native'
        ? this.paintNativeAnswerPage()
        : this.paintVisual()
      : this.paintNativeAssistantOverlay();
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

  /** An answer over the visual HUD is Even's own text page whenever the
   * surface can hand the page over under its transition barrier. Receipts
   * stay on the image page: a short strip is two tiles, cheaper than a
   * page swap and its four-tile return. */
  private usesNativeAnswerPage(): boolean {
    if (this.visual?.withPageTransition === undefined || !this.assistantOverlay) return false;
    if (this.assistantOverlay.presentation === 'answer') return true;
    // A receipt over a page the firmware owns is a firmware page as well:
    // one rebuild in, and the retained page comes back on dismissal.
    return NATIVE_CONTENT_OWNERS.has(this.pageOwner) || this.nativeOverlay !== undefined;
  }

  private async paintNativeAnswerPage(): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent()) return false;
    const overlay = this.assistantOverlay;
    if (!overlay || !this.usesNativeAnswerPage()) {
      // Dismissed, or replaced by an image-strip receipt: leave the firmware
      // page and repaint whatever JOAT feature is retained underneath.
      this.nativeOverlay = undefined;
      if (this.pageOwner !== 'overlay-native') return this.paintVisual();
      return this.restoreRetainedPage();
    }
    const pages = paginateAnswer(overlay.text);
    const revision = overlay.revision ?? 0;
    const previous = this.nativeOverlay;
    // The same answer keeps its page across streamed appends; a new answer
    // starts on page one.
    const page = previous && previous.revision === revision
      ? Math.min(previous.page, pages.length - 1)
      : 0;
    const body = pages[page] ?? ' ';
    const hint = answerPageHint(page, pages.length);
    const next = { revision, pages, page, body, hint };
    if (this.pageOwner !== 'overlay-native') {
      const replaced = await this.replaceNativePage(
        this.layouts.assistantAnswerLayout(body, { status: this.streams.get(ContainerId.StatusBar), hint }),
        'overlay-native',
      );
      if (!replaced || !displayCurrent()) return false;
      this.nativeOverlay = next;
      return true;
    }
    this.nativeOverlay = next;
    let ok = true;
    if (!previous || previous.body !== body) {
      ok = await this.upgradeNativeText(ContainerId.Main, 'main', body, previous?.body) && ok;
      if (!displayCurrent()) return false;
    }
    if (!previous || previous.hint !== hint) {
      ok = await this.upgradeNativeText(ContainerId.Hint, 'hint', hint, previous?.hint) && ok;
    }
    return ok && displayCurrent();
  }

  private upgradeNativeText(
    containerID: number, containerName: string, content: string, previous = '',
  ): Promise<boolean> {
    const replacement = content || ' ';
    return this.client.require().textContainerUpgrade(new TextContainerUpgrade({
      containerID,
      containerName,
      contentOffset: 0,
      contentLength: Math.max(previous.length, replacement.length),
      content: replacement,
    }));
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
    const native = this.nativeOverlay;
    if (native && this.pageOwner === 'overlay-native') {
      const page = direction === 'down' ? native.page + 1 : native.page - 1;
      if (page >= 0 && page < native.pages.length) {
        native.page = page;
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
    if (isCurrent && this.usesNativeTextPage(options)) {
      return this.renderNativeTextPage({
        kind: 'list', items: [...items], options: this.resolveListOptions(options), selectedIndex: 0,
      }, isCurrent);
    }
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
  /** A Mac-paged reader as one firmware text page. The Mac's rows are
   * joined and re-paged for the firmware font; swipes page locally through
   * `textContainerUpgrade`, and a swipe past the local edge is left to the
   * menu, which asks the Mac for its neighbouring page exactly as before. */
  private async renderNativeTextPage(
    surface: Extract<HudSurface, { kind: 'list' }>,
    isCurrent?: () => boolean,
  ): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    const current = () => displayCurrent() && !this.confirmation && !this.promptQuestion
      && (isCurrent?.() ?? true);
    if (!current()) return false;
    const from = this.tracePage();
    const liveStatus = this.captureLiveStatus();
    const source = surface.items.join('\n');
    const pages = paginateAnswer(source);
    const page = this.nativeTextPage?.source === source
      ? Math.min(this.nativeTextPage.page, pages.length - 1)
      : surface.options.nativeTextPageFromEnd ? pages.length - 1 : 0;
    const body = pages[page] ?? ' ';
    const hint = textPageHint(page, pages.length, surface.options.hint);
    const ok = await this.replaceNativePage(
      this.layouts.textPageLayout(body, { status: liveStatus?.text, hint }), 'text-page-native',
    );
    if (!ok || !current()) {
      this.recordPageTransition(from, 'list', 'show_list', 'failed');
      return false;
    }
    if (this.surface.kind === 'default' && !this.focusReturn) {
      this.focusReturn = {
        streams: new Map(this.streams), brightness: new Map(this.brightness),
        kinds: new Map(this.kinds), notificationId: this.notificationId,
      };
    }
    this.clearActionOverlay();
    this.resetAll();
    this.surface = surface;
    this.streams.set(ContainerId.Main, surface.options.title);
    this.brightness.set(ContainerId.Main, 4);
    this.streams.set(ContainerId.Hint, surface.options.hint);
    this.brightness.set(ContainerId.Hint, 1);
    if (liveStatus) {
      this.streams.set(ContainerId.StatusBar, liveStatus.text);
      if (liveStatus.brightness !== undefined) this.brightness.set(ContainerId.StatusBar, liveStatus.brightness);
    }
    this.nativeTextPage = { source, pages, page, body, hint };
    this.recordPageTransition(from, 'list', 'show_list', 'success');
    return true;
  }

  /** Page the firmware text page locally. False at an edge, so the caller
   * can ask the Mac for its neighbouring page. */
  scrollTextPage(direction: 'up' | 'down'): boolean {
    const state = this.nativeTextPage;
    const macHint = this.surface.kind === 'list' && this.usesNativeTextPage(this.surface.options)
      ? this.surface.options.hint
      : this.surface.kind === 'productivity' ? productivityActionHint(this.surface.productivity) : undefined;
    if (!state || this.pageOwner !== 'text-page-native' || macHint === undefined) return false;
    const page = direction === 'down' ? state.page + 1 : state.page - 1;
    if (page < 0 || page >= state.pages.length) return false;
    const previous = { body: state.body, hint: state.hint };
    state.page = page;
    state.body = state.pages[page] ?? ' ';
    state.hint = textPageHint(page, state.pages.length, macHint);
    void (async () => {
      await this.upgradeNativeText(ContainerId.Main, 'main', state.body, previous.body);
      if (this.nativeTextPage !== state) return;
      await this.upgradeNativeText(ContainerId.Hint, 'hint', state.hint, previous.hint);
    })().catch(() => undefined);
    return true;
  }

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
    // Every frame before the live map (confirm the place, choose the mode,
    // the preview, the warnings, arrival, an error) is a text card: Even's
    // own text page, one rebuild in and an in-place upgrade for a cursor
    // move. Only the guidance frame with its map is pixels.
    const native = this.visual.withPageTransition !== undefined && !this.nativeTerminal.isActive()
      ? navigationNativeBody(navigation) : undefined;
    if (native === undefined && !await this.prepareNativeTerminalHandoff()) return false;
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
    if (native !== undefined) return this.renderNativeNavigationPage(surface, native, statusOverride);
    return this.renderNavigation(surface, statusOverride, prior);
  }

  /** The text page on the glass for a navigation frame, so the next frame
   * of the same step can move its cursor marker in place. */
  private nativeNavigation?: { body: string; hint: string };

  private async renderNativeNavigationPage(
    surface: Extract<HudSurface, { kind: 'navigation' }>,
    native: { body: string; hint: string },
    statusOverride?: string,
  ): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent()) return false;
    const from = this.tracePage();
    const liveStatus = statusOverride === undefined
      ? this.captureLiveStatus()
      : { text: statusOverride, brightness: 2 };
    const previous = this.pageOwner === 'navigation-native' ? this.nativeNavigation : undefined;
    if (previous === undefined) {
      const ok = await this.replaceNativePage(
        this.layouts.textPageLayout(native.body, { status: liveStatus?.text, hint: native.hint }),
        'navigation-native',
      );
      if (!ok || !displayCurrent()) {
        this.recordPageTransition(from, 'navigation', 'show_navigation', 'failed');
        return false;
      }
    } else {
      let ok = true;
      if (previous.body !== native.body) {
        ok = await this.upgradeNativeText(ContainerId.Main, 'main', native.body, previous.body) && ok;
        if (!displayCurrent()) return false;
      }
      if (previous.hint !== native.hint) {
        ok = await this.upgradeNativeText(ContainerId.Hint, 'hint', native.hint, previous.hint) && ok;
        if (!displayCurrent()) return false;
      }
      if (!ok) return false;
    }
    this.resetAll();
    this.surface = surface;
    this.streams.set(ContainerId.Main, native.body);
    this.brightness.set(ContainerId.Main, 4);
    this.streams.set(ContainerId.Hint, native.hint);
    this.brightness.set(ContainerId.Hint, 1);
    if (liveStatus) {
      this.streams.set(ContainerId.StatusBar, liveStatus.text);
      if (liveStatus.brightness !== undefined) this.brightness.set(ContainerId.StatusBar, liveStatus.brightness);
    }
    this.nativeNavigation = native;
    this.recordPageTransition(from, 'navigation', 'show_navigation', 'success');
    return true;
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
    if (!(this.visual?.transcriptIsThinking?.() ?? false)) {
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
      // localTick: only the counter's digits moved, so this repaint is
      // NOT recorded as a wearer-visible frame receipt.
      void this.paintVisual(true);
    }, interval);
  }

  /** Swipe on the brainstorm/terminal surface pages through history. */
  scrollTranscript(direction: 'up' | 'down'): boolean {
    if (this.surface.kind !== 'brainstorm') return false;
    if (this.usesNativeConversation(this.surface.brainstorm)) {
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
    if (this.usesNativeConversation(brainstorm)) {
      return this.showNativeTerminal(brainstorm, statusOverride, isCurrent);
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
    if (!displayCurrent() || !this.usesNativeConversation(terminal)) return false;
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
          await this.replaceNativePage(this.nativeListLayout(prior.surface), 'terminal-list-native');
        } else if (prior.surface.kind === 'list' && this.usesNativeTextPage(prior.surface.options)) {
          await this.renderNativeTextPage(prior.surface);
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
    // A firmware list or text page hands straight to the firmware page this
    // outcome becomes (a strip or a text page); recreating the four image
    // containers first was four tiles for nothing.
    const stayNative = this.visual.withPageTransition !== undefined
      && !this.nativeTerminal.isActive() && NATIVE_CONTENT_OWNERS.has(this.pageOwner);
    if (!stayNative && !await this.prepareNativeTerminalHandoff()) return false;
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
    if (this.visual.withPageTransition !== undefined) {
      return this.renderNativeProductivityPage(surface, statusOverride);
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

  /** A Calendar or Reminders outcome page (failed, uncertain, due) as Even's
   * own text page: the same chrome as a reader, paged in place when its
   * explanation is long. The raster page cost four tiles in and four out. */
  private async renderNativeProductivityPage(
    surface: Extract<HudSurface, { kind: 'productivity' }>,
    statusOverride?: string,
  ): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent()) return false;
    const from = this.tracePage();
    const model = surface.productivity;
    const liveStatus = statusOverride === undefined
      ? this.captureLiveStatus()
      : { text: statusOverride, brightness: 2 };
    const label = model.domain === 'calendar' ? 'Calendar' : 'Reminders';
    const source = [
      `${label} · ${productivityPhaseLabel(model.phase)}`, model.title, model.primary, model.secondary,
    ].filter((line) => line.trim().length > 0).join('\n');
    const pages = paginateAnswer(source);
    const page = this.nativeTextPage?.source === source
      ? Math.min(this.nativeTextPage.page, pages.length - 1) : 0;
    const body = pages[page] ?? ' ';
    const macHint = productivityActionHint(model);
    const hint = textPageHint(page, pages.length, macHint);
    const ok = await this.replaceNativePage(
      this.layouts.textPageLayout(body, { status: liveStatus?.text, hint }), 'text-page-native',
    );
    if (!ok || !displayCurrent()) {
      this.recordPageTransition(from, 'list', 'show_list', 'failed');
      return false;
    }
    if (this.surface.kind === 'default' && !this.focusReturn) {
      this.focusReturn = {
        streams: new Map(this.streams), brightness: new Map(this.brightness),
        kinds: new Map(this.kinds), notificationId: this.notificationId,
      };
    }
    this.clearActionOverlay();
    this.resetAll();
    this.surface = surface;
    this.streams.set(ContainerId.Main, model.title);
    this.brightness.set(ContainerId.Main, 4);
    this.streams.set(ContainerId.Hint, macHint);
    this.brightness.set(ContainerId.Hint, 1);
    if (liveStatus) {
      this.streams.set(ContainerId.StatusBar, liveStatus.text);
      if (liveStatus.brightness !== undefined) this.brightness.set(ContainerId.StatusBar, liveStatus.brightness);
    }
    this.nativeTextPage = { source, pages, page, body, hint };
    this.recordPageTransition(from, 'list', 'show_list', 'success');
    return true;
  }

  /** Receipt strip over whatever page the wearer is on; no surface swap. */
  private async showActionPill(
    productivity: ProductivityHudModel,
    onVisible?: () => void,
  ): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent()) return false;
    if (this.visual?.withPageTransition !== undefined && !this.confirmation
      && this.surface.kind !== 'productivity' && NATIVE_CONTENT_OWNERS.has(this.pageOwner)) {
      // The outcome over a firmware list (a saved reminder over the Planning
      // day) used to rebuild the four image containers and repaint the list
      // as pixels under a two-tile strip. It is one firmware page now, and
      // the list returns, still a firmware list, when the strip settles.
      this.clearActionOverlay();
      const text = `${productivityPhaseLabel(productivity.phase)} · ${productivity.title}\n${productivity.primary}`;
      const shown = await this.showAssistantOverlay(text, false, true, 'receipt');
      if (!shown || !displayCurrent()) return false;
      this.settleAssistantOverlay(NATIVE_PILL_DISMISS_MS);
      onVisible?.();
      return true;
    }
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
    if (this.usesNativeTextPage(surface.options)) return this.renderNativeTextPage(surface);
    const from = this.tracePage();
    const liveStatus = this.captureLiveStatus();
    // Agent selection is a firmware-owned list: its highlight and paging stay
    // smooth without four sequential image uploads. Other lists retain JOAT's
    // authored visual treatment in this migration slice.
    const firmwareList = surface.options.nativeFirmwareList === true;
    const nativeReplyPage = !this.visual && !!this.assistantOverlay && !this.confirmation;
    const ok = firmwareList
      ? await this.replaceWithNativeTerminalList(this.nativeListLayout(surface))
      : this.visual
      ? true
      : await this.rebuildPage(nativeReplyPage
          ? this.layouts.defaultLayout()
          : this.layouts.listLayout(surface.items, surface.options));
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
      // paintVisual arms this for raster pages; a firmware page arms it here.
      if (painted) this.armPanelTicker();
      this.recordPageTransition(from, 'list', 'show_list', painted ? 'success' : 'failed');
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
  /** The expanded Pomodoro on the firmware list cannot be upgraded in
   * place: once a minute the owner of its rows (CapabilityMenu) rebuilds
   * the page with a fresh countdown row. */
  onNativePanelTick?: () => void;

  private armPanelTicker(): void {
    if (!this.displayActive) return;
    if (this.panelTicker !== undefined) return;
    const delay = this.panelTickDelay();
    if (delay === undefined) return;
    this.panelTicker = setTimeout(() => {
      this.panelTicker = undefined;
      if (this.panelTickDelay() === undefined) return;
      if (this.surface.kind === 'list' && this.surface.options.nativeFirmwareList) {
        this.onNativePanelTick?.();
        return;
      }
      void this.paintVisual(true);
    }, delay);
  }

  private panelTickDelay(): number | undefined {
    const pomodoro = this.rail.content('pomodoro')?.pomodoro;
    if (!pomodoro) return undefined;
    const now = Date.now();
    if (this.surface.kind === 'default' && this.rail.active() === 'pomodoro') {
      return pomodoroTickDelayMs(pomodoro, now, 60_000);
    }
    if (this.surface.kind === 'list' && this.surface.options.railTab === 'pomodoro') {
      // A firmware list is rebuilt per minute; the raster page repainted
      // one tile per second.
      return pomodoroTickDelayMs(pomodoro, now, this.surface.options.nativeFirmwareList ? 60_000 : 1000);
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
    // The decision gate is a firmware text page whenever the visual surface
    // can hand the page over under its transition barrier: one
    // rebuildPageContainer (~165 ms) instead of four forced image tiles at
    // 0.2–2 s each, so "Send to X?" is readable before the Mac's
    // confirmation envelope has been spent on pixels. A visual surface
    // without that barrier keeps the exact raster gate below.
    if (this.visual && !this.visual.withPageTransition) {
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
    const layout = this.layouts.confirmationLayout(current.prompt, liveStatus?.text);
    const ok = this.visual
      ? await this.replaceNativePage(layout, 'confirmation-native')
      : await this.rebuildPage(layout);
    if (!displayCurrent()) return false;
    if (this.confirmation?.id !== id) return true;
    if (!ok || (current.isCurrent && !this.isConfirmationCurrent(current))) {
      // A bound gate that was retired while its page was being replaced is
      // not visible authority; restore its own return surface, exactly as
      // the raster path does.
      if (current.isCurrent && this.confirmation === current) {
        this.confirmation = undefined;
        await this.restoreAfterConfirmation(current);
      } else if (!ok && openedHere) {
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
    // The native page already carries the status line; the compatibility
    // surface still writes it so a later status change can be upgraded.
    const restored = this.visual ? true : await this.restoreLiveStatus(liveStatus);
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
    // Leaving the firmware decision page: recreate the four image containers
    // first unless the prior page is itself firmware-owned, in which case its
    // own replacement below moves the page directly.
    if (this.pageOwner === 'confirmation-native'
      && !(prior.surface.kind === 'list'
        && (prior.surface.options.nativeFirmwareList || this.usesNativeTextPage(prior.surface.options)))
      && !await this.rebuildVisualPage()) {
      this.recordPageTransition('confirmation', this.tracePage(), 'dismiss_confirmation', 'failed');
      return false;
    }
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
          ? this.layouts.listLayout(this.surface.items, this.surface.options)
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
    if (this.pageOwner !== 'visual') return true;
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
    const content = full.length > MAX_UPGRADE_CHARS ? full.slice(-MAX_UPGRADE_CHARS) : full;
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
      ...(options.nativeTextPage ? { nativeTextPage: true } : {}),
      ...(options.nativeTextPage && options.nativeTextPageFromEnd ? { nativeTextPageFromEnd: true } : {}),
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
        && this.surface.brainstorm.screen === 'agent_terminal') {
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
      && this.usesNativeConversation(this.surface.brainstorm)) {
      if (this.nativeTerminal.isActive()) return this.nativeTerminal.repaint();
      return this.nativeTerminal.present(this.surface.brainstorm,
        this.streams.get(ContainerId.StatusBar), displayCurrent);
    }
    if (this.surface.kind === 'list' && this.surface.options.nativeFirmwareList) {
      return this.replaceNativePage(this.nativeListLayout(this.surface), 'terminal-list-native');
    }
    if (this.surface.kind === 'list' && this.usesNativeTextPage(this.surface.options)) {
      return this.renderNativeTextPage(this.surface);
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
          this.layouts.listLayout(surface.items, surface.options),
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

  private async paintVisual(localTick = false): Promise<boolean> {
    this.removeRetiredNotifications();
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent()) return false;
    if (this.assistantConversation.isActive() && !this.restoringAssistantConversation) return true;
    if (this.pageOwner !== 'visual') return true;
    if (!this.visual) return false;
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
      && this.usesNativeConversation(this.surface.brainstorm)
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
