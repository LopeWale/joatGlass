import { TextContainerUpgrade, type RebuildPageContainer } from '@evenrealities/even_hub_sdk';
import { GesturePolicy } from './GesturePolicy';

import type { BridgeClient } from './BridgeClient';
import type { HudPageBuilder } from './HudPageBuilder';
import { TEXT_CONTAINER_MAX_BYTES, fitUtf8Tail } from './FirmwareLimits';
import type {
  AssistantOverlayPresentation,
  AssistantOverlayVisual,
  HudFramePainter,
  HudVisualFrame,
} from './HudVisualSurface';
import type { G2DiagnosticTrace, G2TracePage } from './G2DiagnosticTrace';
import {
  AssistantConversationSurface,
  replyDraftView,
  validAssistantConversationSnapshot,
  type ConversationExitResult,
} from './AssistantConversationSurface';
import { paginateHanging, paginateMeasured } from './FirmwareText';
import { OVERLAY_GAP, cardTop, overlayConversation } from './ConversationOverlay';
import { paginateEven, readerParagraphs, type EvenReaderLine } from './EvenFont';
import { hasImageHeader, withImageHeader } from './HeaderImage';
import { hasHomeImages, homeColumnView, withHomeImages, type HomeColumnView } from './HomeImages';
import { ASSISTANT_ANSWER_PAGE, READER_TEXT_PAGE, confirmationLabels, fitHeader } from './HudPageBuilder';
import { type HudStatusModel, hudStatusLabel, nativeStatusLine } from './HudStatus';
import { composeOverviewText, type OverviewTextView } from './OverviewText';
import {
  NOTIFICATION_CARD, NOTIFICATION_CARD_MS, cardFromRenderedText, notificationCardBottom,
  notificationCardLayout, overlayNotificationCard,
} from './NotificationCard';
import { isRouteProgressView, navigationChip, pomodoroChip } from './ActivityChips';
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
  type ConfirmationPurpose,
  type AssistantConversationSnapshot,
  type AssistantConversationTurn,
  type HudContentKind,
  type HudNotificationCard,
  type PanelContentTab,
  type PanelTab,
  type PlanningGridCell,
  type PromptOwnerKind,
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
  /** Show these rows as one firmware text page (a reader), paged in place. */
  nativeTextPage?: boolean;
  /** Open the text page on its last local page: the Mac answered a swipe up. */
  nativeTextPageFromEnd?: boolean;
  /** A review: its tap is offered only on the last local page. */
  reviewTapOnLastPage?: boolean;
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
  reviewTapOnLastPage?: boolean;
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

/** Home as firmware text (OverviewText.ts) rather than four image tiles:
 * opening it is one rebuild and a change is an in-place upgrade of the band
 * that changed, where the image overview cost four tile uploads at
 * 0.5-2 s each over BLE (everything-evenhub glasses-ui). ON by default;
 * VITE_NATIVE_OVERVIEW=0 draws the image overview. */
let nativeOverviewEnabled = String(import.meta.env.VITE_NATIVE_OVERVIEW ?? '1') !== '0';
/** Every firmware page carries the image spine as its header
 * (HeaderImage.ts); `VITE_IMAGE_HEADER=0` keeps the firmware text line. */
const imageHeaderEnabled = String(import.meta.env.VITE_IMAGE_HEADER ?? '1') !== '0';
/** Readers are firmware text pages. Drawn in Even's font as images
 * (0.1.108-0.1.113) they had to be repainted tile by tile on every swipe
 * and their thin strokes looked faded; the wearer scrolls readers, and
 * ruled that what is scrolled is firmware text (2026-10-07).
 * `VITE_EVEN_FONT_PAGES=1` brings the image readers back. */
let evenFontPagesEnabled = String(import.meta.env.VITE_EVEN_FONT_PAGES ?? '0') === '1';
/** Readers that stay firmware text pages even where Even-font pages are on.
 * A News article is long prose the wearer scrolls through page after page:
 * the firmware text pages in place (one upgrade per swipe) and draws every
 * stroke at full brightness, where the 55% image's thin strokes look faded
 * (the wearer, 2026-10-07). */
const FIRMWARE_TEXT_READERS = new Set(['NEWS · ARTICLE']);
/** Home's left column as images in Even's font (HomeImages.ts);
 * `VITE_HOME_IMAGES=0` keeps it firmware text. */
const homeImagesEnabled = String(import.meta.env.VITE_HOME_IMAGES ?? '1') !== '0';

/** Tests that pin the IMAGE overview select it here. Never product code. */
export function setNativeOverviewForTest(on: boolean): void {
  nativeOverviewEnabled = on;
}

export function setEvenFontPagesForTest(on: boolean): void {
  evenFontPagesEnabled = on;
}

/** One band of the text overview as the glass last accepted it. */
interface OverviewBand { name: string; content: string; textColor?: number }

type DisplayPageOwner =
  | 'visual'
  | 'assistant-native'
  | 'terminal-list-native'
  | 'terminal-conversation-native'
  | 'confirmation-native'
  | 'overlay-native'
  | 'prompt-native'
  | 'text-page-native'
  | 'navigation-native'
  | 'notification-native';

/** Firmware pages that hold JOAT content of their own (a list, a reader, a
 * navigation card). An image paint over one of them must recreate the four
 * image containers first; a receipt or an outcome strip over one of them is
 * drawn as a firmware page instead, because an image strip cannot land on
 * a page that has no image containers and a rebuild costs four tiles. */
const NATIVE_CONTENT_OWNERS: ReadonlySet<DisplayPageOwner> = new Set<DisplayPageOwner>([
  'terminal-list-native', 'text-page-native', 'navigation-native',
]);

/** Firmware pages whose layout declares the `status` text container (id 1)
 * as their one-line header. A clock, microphone or battery change on one of
 * them is a single in-place `textContainerUpgrade` of that container, not a
 * rebuild — and not a dropped update, which is what it was until 2026-10-05:
 * `paint()` is a no-op while the firmware owns the page, so the header
 * froze at whatever the rebuild carried. The Agent Terminal page composes
 * its own head line and the conversation page has no header. */
const NATIVE_STATUS_OWNERS: ReadonlySet<DisplayPageOwner> = new Set<DisplayPageOwner>([
  'terminal-list-native', 'text-page-native', 'navigation-native',
  'confirmation-native', 'overlay-native', 'prompt-native',
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
  /** Present when the page is set in Even's font on an image page
   * (EvenFont.ts) rather than as a firmware text page. */
  lines?: EvenReaderLine[][];
}

/** The swipe promises a Mac reader writes for its own pages, formatted. */
/** The reply card's footers. The wearer's own words come back for
 * consent: tap sends them, long-press says them again, and Back cancels to
 * the thread. Both fit the card's 458 px footer (pretext). */
/** The working glyph's cadence on the wearer's row of a reply card. */
const REPLY_LISTENING_TICK_MS = 450;

/** The Mac's reply consent, `Send to Ada? “words”` (or, after a long
 * draft's review, `Send to Ada? (reviewed) “opening…”`), split back into
 * who and what. Anything else keeps the plain decision page. */
export function parseMessageDraft(prompt: string): { recipient: string; body: string } | undefined {
  const match = /^Send to ([\s\S]+?)\? (?:\(reviewed\) )?“([\s\S]*)”$/u.exec(prompt.trim());
  if (!match?.[1] || !match[2]?.trim()) return undefined;
  return { recipient: match[1], body: match[2] };
}

const PAGE_SWIPES = new Set(['swipe: previous', 'swipe: more', 'swipe: page']);

/** Whether two pages have the same containers in the same places, so the
 * second can be reached by upgrading text in place instead of a rebuild. */
function sameGeometry(left: RebuildPageContainer, right: RebuildPageContainer): boolean {
  const shape = (page: RebuildPageContainer) => JSON.stringify([
    ...(page.textObject ?? []).map((item) => [item.containerID, item.xPosition, item.yPosition, item.width, item.height, item.zOrderIndex]),
    ...(page.listObject ?? []).map((item) => [item.containerID, item.xPosition, item.yPosition, item.width, item.height, item.zOrderIndex]),
    ...(page.imageObject ?? []).map((item) => [item.containerID, item.xPosition, item.yPosition, item.width, item.height]),
  ]);
  return shape(left) === shape(right);
}

function textPageHint(page: number, count: number, macHint: string, review = false): string {
  // The Mac still writes "left x2"; every page says "2x tap".
  const hint = GesturePolicy.formatHint(macHint);
  if (count <= 1) return hint;
  // Two layers page this text: the Mac's pages, and the firmware pages cut
  // from each. The swipe promise covers both, and a review offers its tap
  // only on the last firmware page (the Mac offers it only on its last).
  const clauses = hint.split(' · ');
  const swipes = clauses.filter((clause) => clause.startsWith('swipe:'));
  if (swipes.some((clause) => !PAGE_SWIPES.has(clause))) return `${page + 1}/${count} · ${hint}`;
  const back = page > 0 || swipes.some((clause) => clause !== 'swipe: more');
  const more = page < count - 1 || swipes.some((clause) => clause !== 'swipe: previous');
  const swipe = back && more ? 'swipe: page' : more ? 'swipe: more' : back ? 'swipe: previous' : undefined;
  const last = page === count - 1;
  const rest = clauses.filter((clause) => !clause.startsWith('swipe:')
    && (last || !review || !clause.startsWith('tap:')));
  return [`${page + 1}/${count}`, ...(swipe ? [swipe] : []), ...rest].join(' · ');
}

/** Every list is Even's own list unless it is a reader (a text page) or
 * carries the Planning month's grid, the one list drawn as an image. A
 * caller that forgets the flag still gets a firmware list, never an empty
 * image page: the renderer draws no other list. */
function isFirmwareList(options: ListSurfaceOptions, visual: boolean): boolean {
  if (options.nativeFirmwareList === true) return true;
  return visual && options.nativeTextPage !== true && normalizePlanningGrid(options.planningGrid) === undefined;
}

/** An answer, a reader or a question as firmware pages: the firmware wraps
 * each page's words at its own break points, and `measureTextWrap` decides
 * how many a page holds (FirmwareText). */
export function paginateAnswer(
  text: string,
  lineCapacity: number = ASSISTANT_ANSWER_PAGE.lineCapacity,
  width: number = ASSISTANT_ANSWER_PAGE.textWidth,
): string[] {
  return paginateMeasured(text, width, lineCapacity);
}

function answerPageHint(page: number, count: number): string {
  return count > 1 ? `${page + 1}/${count} · swipe: page · tap: dismiss` : 'tap: dismiss';
}

interface ConfirmationReturnState extends HudContentState {
  id: string;
  prompt: string;
  diagnosticKind?: 'messages';
  purpose?: ConfirmationPurpose;
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
  /** A Messages reply's question, drawn in JOAT's conversation card. */
  messageReply?: boolean;
  id: string;
  question: string;
  isCurrent: () => boolean;
  paint?: Promise<boolean>;
  /** The wearer's words so far, while they speak a reply (prompt.partial). */
  partial?: string;
  /** The reply card as last built, so a partial can upgrade it in place. */
  layout?: RebuildPageContainer;
  /** The working glyph's frame on the wearer's row until their words come. */
  listeningFrame?: number;
  /** A tick of the glyph waits to be painted. */
  listeningDirty?: boolean;
  listeningTimer?: ReturnType<typeof setTimeout>;
  /** The newest partial waiting for the one in flight to land. */
  pendingPartial?: string;
  partialPaint?: Promise<boolean>;
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
  /** Text Home: Main's content (a reply, a calendar line) heads the pane
   * until the wearer swipes; from then on the pane follows the rail, until
   * new Main content arrives. A swipe always shows the tab it lands on. */
  private homePaneFollowsRail = false;
  private surface: HudSurface = { kind: 'default' };
  /** Margin facts (weather, unread) resolved by the Mac for the dashboard. */
  private ambient?: { temperature: string; condition: string; unread: number };
  /** The right pane's tab rail: which tab is active, and what the Mac last
   * pushed for each. Local state — a swipe between tabs never round-trips. */
  readonly rail = new PanelRail();
  /** Repaints the visible pomodoro when its next digit moves. */
  private panelTicker?: ReturnType<typeof setTimeout>;
  /** The newest frame of a route in progress, kept whichever page is on the
   * glass: the header chip reads its next turn. Cleared when the route ends. */
  private routeActivity?: NavigationHudModel;
  /** Moves the Pomodoro chip on its minute, whatever page is on the glass. */
  private activityTicker?: ReturnType<typeof setTimeout>;
  /** The chips the glass last received, so a change writes once. */
  private shownActivities = '';
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
  /** The phone's status facts (HudStatus.ts), set by Chrome. The raster
   * spine and the firmware headers both draw from this; the StatusBar
   * stream keeps only its untitled line for the legacy text paths. */
  private statusModel?: HudStatusModel;
  /** What the native `status` container currently shows, so a live
   * refresh can size its in-place upgrade. Set by every native rebuild. */
  private nativeStatusPainted?: string;
  /** Where JOAT's box begins over a reader's tiles (ConversationOverlay):
   * the tiles draw the header, so a status change repaints them. */
  private readerUnderBox?: number;
  /** Every unread JOAT notification, newest first (hud.notifications). */
  private notificationStack: HudNotificationCard[] = [];
  /** Cards the wearer marked read here, until the Mac's next list. */
  private readonly notificationsReadHere = new Set<string>();
  /** The notification card over the page, while it is up. */
  private notificationCard?: {
    card: HudNotificationCard;
    /** Opened from Home's Notifications tab: no timer, the wearer reads. */
    browsing: boolean;
    /** Drawn from the Mac's rendered line before the full list arrived. */
    provisional: boolean;
    timer?: ReturnType<typeof setTimeout>;
    revision: number;
  };
  private notificationCardRevision = 0;
  private displayActive = true;
  private displayRevision = 0;
  private readonly assistantConversation: AssistantConversationSurface;
  /** The page on the glass carries the two header images. */
  private imageHeaderShown = false;
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
        // The box floats over the page it was called on (Even AI's card).
        replacePage: (page) => this.replaceNativePage(
          overlayConversation(page, this.conversationUnderlay()), 'assistant-native',
        ),
        statusText: () => this.nativeHeader(this.nativePageTitle()),
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
  /** The page replacement in flight, if any (replaceNativePage). */
  private pageReplacement?: Promise<boolean>;

  /** Replace the whole page with a native one. The visual surface refuses a
   * second replacement while one runs; `pageReplacement` lets a surface
   * that must not be refused (the consent card) wait for it first. */
  private replaceNativePage(
    page: RebuildPageContainer,
    owner: Exclude<DisplayPageOwner, 'visual'>,
  ): Promise<boolean> {
    const replacement = this.replaceNativePageNow(page, owner);
    this.pageReplacement = replacement;
    void replacement.finally(() => {
      if (this.pageReplacement === replacement) this.pageReplacement = undefined;
    }).catch(() => undefined);
    return replacement;
  }

  private async replaceNativePageNow(
    page: RebuildPageContainer,
    owner: Exclude<DisplayPageOwner, 'visual'>,
  ): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent()) return false;
    const visual = this.visual;
    const statusShown = page.textObject?.find((container) =>
      container.containerID === ContainerId.StatusBar)?.content;
    page = this.withHeader(page);
    const replace = () => this.client.require().rebuildPageContainer(page);
    const header = hasImageHeader(page);
    if (!visual) {
      const replaced = await replace();
      if (replaced && displayCurrent()) {
        this.pageOwner = owner;
        this.nativeStatusPainted = statusShown;
        this.overviewText = undefined;
      }
      return replaced && displayCurrent();
    }
    if (!visual.withPageTransition) return false;
    try {
      const replaced = await visual.withPageTransition(replace, displayCurrent);
      if (replaced && displayCurrent()) {
        visual.forgetPage?.();
        if (owner !== 'notification-native') this.clearNotificationCard();
        this.pageOwner = owner;
        this.nativeStatusPainted = statusShown;
        this.overviewText = undefined;
        this.imageHeaderShown = header;
        // The halves arrive just after the page's text, one send each, once
        // the caller has recorded which page this is (the header names it).
        if (header) setTimeout(() => { void this.paintImageHeader(); }, 0);
        this.readerUnderBox = undefined;
        this.paintReaderUnderBox(page);
        this.paintHomeColumnUnderBox(page);
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
    if (this.pageOwner === 'visual' && !this.overviewTextOnGlass()) return true;
    if (!visual.withPageTransition) return false;
    try {
      const rebuilt = await visual.withPageTransition(
        () => this.client.require().rebuildPageContainer(this.layouts.visualLayout()),
        displayCurrent,
      );
      if (!rebuilt || !displayCurrent()) return false;
      visual.forgetPage?.();
      this.pageOwner = 'visual';
      this.overviewText = undefined;
      this.imageHeaderShown = false;
      this.readerUnderBox = undefined;
      return true;
    } catch {
      return false;
    }
  }

  /** Restore the retained visual underlay only after its containers exist. */
  private async restoreVisualPage(): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    const home = this.plainOverviewFrame();
    if (home) return this.paintOverviewAsText(home, true);
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
    if (this.surface.kind === 'brainstorm') {
      return this.nativeTerminal.present(this.surface.brainstorm,
        this.nativeHeader(this.nativePageTitle()), displayCurrent);
    }
    if (this.surface.kind === 'list' && this.surface.options.nativeFirmwareList) {
      return this.replaceNativePage(this.nativeListLayout(this.surface), 'terminal-list-native');
    }
    if (this.surface.kind === 'list' && this.usesNativeTextPage(this.surface.options)) {
      return this.renderNativeTextPage(this.surface);
    }
    if (this.surface.kind === 'productivity'
      && !usesActionPillLayout(this.surface.productivity.phase)) {
      return this.renderNativeProductivityPage(this.surface);
    }
    if (this.surface.kind === 'navigation') {
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
    if (this.thinkingTicker !== undefined) clearTimeout(this.thinkingTicker);
    this.thinkingTicker = undefined;
    this.thinkingTicks = 0;
    this.client.suspendDisplay?.();
    this.visual?.suspendDisplay?.();
    this.clearActivityTicker();
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
    this.clearNotificationCard();
    this.focusReturn = undefined;
    this.resetAll();
    this.surface = { kind: 'default' };
  }

  /** Recovery permits fresh work; no paint or timer is replayed here. */
  resumeDisplay(): void {
    this.client.resumeDisplay?.();
    this.visual?.resumeDisplay?.();
    this.displayActive = true;
    this.armActivityTicker();
  }

  /** A firmware list page is still on the glass under a Home surface: the
   * next visual paint must replace it (text overview) or rebuild the image
   * page before painting tiles. */
  private visualPagePending(): boolean {
    return NATIVE_CONTENT_OWNERS.has(this.pageOwner)
      && this.surface.kind === 'default' && this.visual !== undefined;
  }

  /** A firmware page is on the glass: JOAT content of its own, or the text
   * overview. A receipt or an outcome over one is a firmware page too. */
  private onFirmwarePage(): boolean {
    return NATIVE_CONTENT_OWNERS.has(this.pageOwner) || this.overviewTextOnGlass();
  }

  private captureDisplayGuard(): () => boolean {
    const revision = this.displayRevision;
    const active = this.displayActive;
    const bridgeCurrent = this.client.captureDisplayGuard?.() ?? (() => true);
    return () => active && this.displayActive && revision === this.displayRevision && bridgeCurrent();
  }

  /** The page a JOAT box was called over, as its own layout would rebuild
   * it now, for `overlayConversation` to cut beneath the box: Home, a
   * firmware list, a text page or a native navigation page. The image pages
   * (the Planning month, the live map) and the terminal return undefined;
   * the box still stands alone over those. */
  private conversationUnderlay(): RebuildPageContainer | undefined {
    if (!this.visual) return undefined;
    const surface = this.surface;
    if (surface.kind === 'default') {
      if (!nativeOverviewEnabled) return undefined;
      const frame = this.visualFrame();
      if (frame.kind !== 'overview') return undefined;
      // Home as it is drawn: its text and its column images. The header
      // comes from the box's own status line (replaceNativePage).
      const view = this.overviewView(frame);
      const layout = this.layouts.overviewTextLayout(view);
      return this.homeColumnFor(frame, view) ? withHomeImages(layout) : layout;
    }
    if (surface.kind === 'list' && surface.options.nativeFirmwareList) return this.nativeListLayout(surface);
    // A reader in Even's font keeps its own tiles under the box.
    if (this.evenFontReaderRetained()) return this.layouts.visualLayout();
    const text = this.nativeTextPage;
    if (text && ((surface.kind === 'list' && this.usesNativeTextPage(surface.options))
      || (surface.kind === 'productivity' && !usesActionPillLayout(surface.productivity.phase)))) {
      return this.layouts.textPageLayout(
        text.body, { status: '', hint: text.hint }, { inset: surface.kind === 'list' },
      );
    }
    if (surface.kind === 'navigation') {
      const native = navigationNativeBody(surface.navigation);
      if (native !== undefined) return this.layouts.textPageLayout(native.body, { status: '', hint: native.hint });
    }
    return undefined;
  }

  /** Home's left column for this frame, where the painter can draw it. */
  private homeColumnFor(
    frame: Extract<HudVisualFrame, { kind: 'overview' }>,
    view: ReturnType<typeof composeOverviewText>,
  ): HomeColumnView | undefined {
    return homeImagesEnabled && this.visual?.paintHomeColumn && this.visual.canPaintHomeColumn?.()
      ? homeColumnView(
        view,
        frame.ambient ? `${frame.ambient.temperature} ${frame.ambient.condition}`.trim() : '',
        frame.ambient?.unread ?? 0,
      )
      : undefined;
  }

  /** Under JOAT's box over Home: the page's column images were just
   * recreated blank, so they are painted again with no row under the box. */
  private paintHomeColumnUnderBox(page: RebuildPageContainer): void {
    const top = cardTop(page);
    if (!hasHomeImages(page) || top === undefined || this.surface.kind !== 'default') return;
    const frame = this.visualFrame();
    if (frame.kind !== 'overview') return;
    const column = this.homeColumnFor(frame, this.overviewView(frame));
    if (!column) return;
    const current = this.captureDisplayGuard();
    void this.paintHomeColumn({ ...column, clipBottom: top - OVERLAY_GAP }, current);
  }

  /** Home's column images; the image lane sends only a changed tile. */
  private paintHomeColumn(view: HomeColumnView, current: () => boolean): Promise<boolean> {
    const visual = this.visual;
    if (!visual?.paintHomeColumn) return Promise.resolve(false);
    return visual.paintHomeColumn(view, current).catch(() => false);
  }

  /** A firmware page with the image spine in place of its status line. */
  private withHeader(page: RebuildPageContainer): RebuildPageContainer {
    return imageHeaderEnabled && this.visual?.paintHeader && this.visual.canPaintHeader?.()
      ? withImageHeader(page) : page;
  }

  /** Draw the header for the page on the glass: the clock, the page's
   * word, the running features' chips, the mic and the battery. The image
   * lane sends only a half whose pixels changed. */
  private paintImageHeader(): Promise<boolean> {
    const visual = this.visual;
    const model = this.statusModel;
    if (!this.imageHeaderShown || !visual?.paintHeader || !model) return Promise.resolve(false);
    const title = this.nativePageTitle();
    const home = this.surface.kind === 'default' && !this.conversationInFront()
      && !this.confirmation && !this.promptQuestion;
    const headerModel = home
      ? this.withActivities(model, { frame: 'overview' })
      : this.withActivities(model, { title });
    return visual.paintHeader(headerModel, hudStatusLabel(headerModel, title), this.captureDisplayGuard())
      .catch(() => false);
  }

  /** A reader is Even's own text page over the visual HUD; the text
   * compatibility surface (no visual HUD) shows it as its text layout. */
  private usesNativeTextPage(options: { nativeTextPage?: boolean }): boolean {
    return options.nativeTextPage === true && this.visual !== undefined;
  }

  /** A reader drawn in Even's font on the four image tiles: only when
   * opted in (VITE_EVEN_FONT_PAGES=1) and the painter can draw one, and
   * never for FIRMWARE_TEXT_READERS. Otherwise a firmware text page. */
  private usesEvenFontPage(options: { nativeTextPage?: boolean; title: string }): boolean {
    return evenFontPagesEnabled && this.usesNativeTextPage(options)
      && !FIRMWARE_TEXT_READERS.has(options.title.trim().toUpperCase())
      && this.visual?.canPaintReader?.() === true;
  }

  /** The reader on the glass is an Even-font image page. Derived, not
   * stored, so it cannot outlive the page. */
  private evenFontPageOnGlass(): boolean {
    return this.pageOwner === 'visual' && this.evenFontReaderRetained();
  }

  /** The surface is an Even-font reader, on the glass or beneath JOAT's box. */
  private evenFontReaderRetained(): boolean {
    const state = this.nativeTextPage;
    return this.surface.kind === 'list'
      && this.usesEvenFontPage(this.surface.options)
      && state?.lines !== undefined && state.source === this.surface.items.join('\n');
  }

  /** The reader's page as an image frame; `clipBottom` leaves the area
   * under JOAT's box dark. */
  private readerFrame(clipBottom?: number): HudVisualFrame | undefined {
    const reader = this.nativeTextPage;
    if (this.surface.kind !== 'list' || !reader?.lines) return undefined;
    const frame: HudVisualFrame = {
      kind: 'reader',
      status: this.streams.get(ContainerId.StatusBar) ?? '',
      micOn: this.micOn,
      title: this.surface.options.title,
      lines: (reader.lines[reader.page] ?? []).map((line) => ({ ...line })),
      hint: reader.hint,
      ...(clipBottom !== undefined ? { clipBottom } : {}),
    };
    return this.statusModel
      ? { ...frame, statusModel: this.withActivities(this.statusModel, { frame: frame.kind }) }
      : frame;
  }

  /** A box page that kept the reader's tiles (ConversationOverlay) starts
   * with them blank: paint the reader above the box. */
  private paintReaderUnderBox(page: RebuildPageContainer): void {
    const tiles = (page.imageObject ?? []).some((image) => image.containerID === ContainerId.Image);
    const top = cardTop(page);
    if (!tiles || top === undefined || !this.visual || !this.evenFontReaderRetained()) return;
    const frame = this.readerFrame(top - OVERLAY_GAP);
    if (!frame) return;
    this.readerUnderBox = top - OVERLAY_GAP;
    const displayCurrent = this.captureDisplayGuard();
    const visual = this.visual;
    setTimeout(() => {
      if (!displayCurrent()) return;
      void (visual.paintExact ? visual.paintExact(frame, displayCurrent) : visual.paint(frame)).catch(() => false);
    }, 0);
  }

  /** Lay the reader out in Even's font and paint it as an image page. */
  private async renderEvenFontPage(
    surface: Extract<HudSurface, { kind: 'list' }>,
    current: () => boolean,
  ): Promise<boolean> {
    const from = this.tracePage();
    const source = surface.items.join('\n');
    const lines = paginateEven(surface.items, surface.options.title.trim().toUpperCase().startsWith('MESSAGES'));
    const pages = lines.map((page) => page.map((line) => line.text).join('\n'));
    const page = this.nativeTextPage?.source === source
      ? Math.max(0, Math.min(this.nativeTextPage.page, pages.length - 1))
      : surface.options.nativeTextPageFromEnd ? pages.length - 1 : 0;
    const hint = textPageHint(page, pages.length, surface.options.hint, surface.options.reviewTapOnLastPage);
    if (!await this.rebuildVisualPage() || !current()) {
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
    this.nativeTextPage = { source, pages, page, body: pages[page] ?? ' ', hint, lines };
    const visual = this.visual!;
    const painted = visual.paintExact
      ? await visual.paintExact(this.visualFrame(), current)
      : await visual.paint(this.visualFrame());
    if (!painted || !current()) {
      this.recordPageTransition(from, 'list', 'show_list', 'failed');
      return false;
    }
    this.recordPageTransition(from, 'list', 'show_list', 'success');
    return true;
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
    // A new notification pops up as a card over whatever page is up (the
    // wearer's ruling, 2026-10-07: over every page, as Even's own does).
    // Home's own record of it below is unchanged.
    if (containerId === ContainerId.Main && contentKind === 'notification'
      && /^[a-f0-9]{32}$/i.test(notificationId ?? '') && text.trim()) {
      void this.popNotification(notificationId!, text);
    }
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
      // New content heads Home's pane again; a notification never does.
      if (contentKind !== 'notification' && text.trim()) this.homePaneFollowsRail = false;
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
      // A stream that starts now heads Home's pane again; later chunks of
      // the same stream leave a wearer who swiped away on their tab.
      const starts = this.kinds.get(ContainerId.Main) !== 'assistant'
        || !(this.streams.get(ContainerId.Main) ?? '').trim();
      const content = { streams: this.streams, brightness: this.brightness, kinds: this.kinds,
        notificationId: this.notificationId };
      this.prepareAssistantAppend(content);
      this.notificationId = undefined;
      this.paintedNotificationId = undefined;
      this.notificationReturn = undefined;
      if (starts && chunk.trim()) this.homePaneFollowsRail = false;
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
    const prompt: PromptQuestionState = {
      id: promptId, question, isCurrent,
      ...(ownerKind === 'messages' ? { messageReply: true } : {}),
    };
    this.promptQuestion = prompt;
    this.cancelAssistantOverlayPaintTimer();
    this.assistantOverlayPaintDirty = false;
    const paint = this.renderNativePromptQuestion(prompt, this.promptQuestionPaint);
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
      const layout = prompt.messageReply
        ? this.replyCardLayout(prompt)
        : this.layouts.promptQuestionLayout(pages[0] ?? ' ', this.nativeHeader(this.nativePageTitle()));
      prompt.layout = layout;
      painted = await this.replaceNativePage(layout, 'prompt-native');
      if (painted && prompt.messageReply) this.armReplyListening(prompt);
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

  /** The Messages reply card over the thread: JOAT's question and, while
   * the wearer speaks, their words so far. */
  private replyCardLayout(prompt: PromptQuestionState): RebuildPageContainer {
    const turns: AssistantConversationTurn[] = [{ role: 'assistant', text: prompt.question }];
    const spoken = Boolean(prompt.partial?.trim());
    if (spoken) turns.push({ role: 'user', text: prompt.partial! });
    return overlayConversation(this.layouts.assistantConversationLayout(
      replyDraftView(turns, spoken ? undefined : prompt.listeningFrame ?? 0),
      this.nativeHeader(this.nativePageTitle()),
    ), this.conversationUnderlay());
  }

  /**
   * The wearer's words so far, in the reply card while they speak (the
   * wearer, 2026-10-06: it "does not show my speech as I am speaking").
   * Display only: the final transcript alone answers the question. While
   * the card keeps its size the words are upgraded in place; a card that
   * must grow is rebuilt. One paint at a time, the newest words win.
   */
  showPromptPartial(promptId: string, text: string): Promise<boolean> {
    const prompt = this.promptQuestion;
    if (!prompt || prompt.id !== promptId || !prompt.messageReply || !this.isPromptCurrent(prompt)) {
      return Promise.resolve(false);
    }
    prompt.pendingPartial = text;
    return this.drainReplyCard(prompt);
  }

  /** The working glyph moves on the wearer's row of a reply card until
   * their first words replace it. */
  private armReplyListening(prompt: PromptQuestionState): void {
    if (prompt.listeningTimer !== undefined) clearTimeout(prompt.listeningTimer);
    prompt.listeningTimer = setTimeout(() => {
      prompt.listeningTimer = undefined;
      if (!this.isPromptCurrent(prompt) || prompt.partial?.trim()) return;
      prompt.listeningFrame = (prompt.listeningFrame ?? 0) + 1;
      prompt.listeningDirty = true;
      void this.drainReplyCard(prompt).finally(() => {
        if (this.isPromptCurrent(prompt) && !prompt.partial?.trim()) this.armReplyListening(prompt);
      });
    }, REPLY_LISTENING_TICK_MS);
  }

  /** Paint the reply card's newest words or glyph frame: one paint at a
   * time, the newest state wins. */
  private drainReplyCard(prompt: PromptQuestionState): Promise<boolean> {
    if (prompt.partialPaint) return prompt.partialPaint;
    const run = async (): Promise<boolean> => {
      let painted = false;
      try {
        await prompt.paint?.catch(() => false);
        while ((prompt.pendingPartial !== undefined || prompt.listeningDirty) && this.isPromptCurrent(prompt)) {
          const words = prompt.pendingPartial ?? prompt.partial;
          const tick = prompt.listeningDirty === true;
          prompt.pendingPartial = undefined;
          prompt.listeningDirty = false;
          if (words === prompt.partial && !(tick && !words?.trim())) continue;
          prompt.partial = words;
          const previous = prompt.layout;
          const next = this.replyCardLayout(prompt);
          prompt.layout = next;
          painted = previous && sameGeometry(previous, next)
            ? await this.upgradeChangedTexts(previous, next)
            : await this.replaceNativePage(next, 'prompt-native');
        }
        return painted && this.isPromptCurrent(prompt);
      } catch {
        return false;
      } finally {
        prompt.partialPaint = undefined;
      }
    };
    prompt.partialPaint = run();
    return prompt.partialPaint;
  }

  /** Upgrade, in place, each text container whose words changed. */
  private async upgradeChangedTexts(previous: RebuildPageContainer, next: RebuildPageContainer): Promise<boolean> {
    let ok = true;
    for (const text of next.textObject ?? []) {
      if (text.containerID === ContainerId.StatusBar && this.imageHeaderShown) continue;
      const before = previous.textObject?.find((item) => item.containerID === text.containerID)?.content ?? '';
      if (before === text.content) continue;
      ok = await this.upgradeNativeText(text.containerID ?? 0, text.containerName ?? '', text.content ?? ' ', before) && ok;
    }
    return ok;
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
    await this.settlePromptPaints(prompt);
    if (!displayCurrent()) return false;
    if (this.promptQuestion || this.confirmation) return false;
    if (this.pageOwner === 'prompt-native') return this.restoreRetainedPage();
    return this.paintVisual();
  }

  /** Wait for everything a prompt is painting: its first page and the
   * words-so-far paints (showPromptPartial). A partial that grows the card
   * is a page replacement; the page guard refuses a second replacement
   * while it runs, so a restore or a confirmation that did not wait was
   * refused and the cancelled card stayed on the glass (review, 2026-10-06).
   * Once the prompt is no longer current no new partial paint starts, so
   * the loop ends. */
  private async settlePromptPaints(prompt: PromptQuestionState): Promise<void> {
    prompt.pendingPartial = undefined;
    prompt.listeningDirty = false;
    if (prompt.listeningTimer !== undefined) clearTimeout(prompt.listeningTimer);
    prompt.listeningTimer = undefined;
    await prompt.paint?.catch(() => false);
    while (prompt.partialPaint) {
      const painting = prompt.partialPaint;
      await painting.catch(() => false);
      if (prompt.partialPaint === painting) prompt.partialPaint = undefined;
    }
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
      && (this.onFirmwarePage() || this.pageOwner === 'overlay-native');
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
  private async prepareNativeTerminalHandoff(toHome = false): Promise<boolean> {
    if (!this.nativeTerminal.isActive()) {
      if (!this.onFirmwarePage()) return true;
      // Home is a text page itself: it replaces the firmware page in one
      // rebuild. Recreating the image containers first was a second rebuild
      // and a blank flash on every Back-to-Home.
      if (toHome && nativeOverviewEnabled && this.visual) return true;
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
      status: this.nativeHeader(surface.options.title),
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
    const pageCount = this.nativeOverlay?.pages.length ?? 1;
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

  /** An answer over the visual HUD is Even's own text page. Receipts stay
   * on an image page: a short strip is two tiles, cheaper than a page swap
   * and its four-tile return. */
  private usesNativeAnswerPage(): boolean {
    if (this.visual === undefined || !this.assistantOverlay) return false;
    if (this.assistantOverlay.presentation === 'answer') return true;
    // A receipt over a page the firmware owns is a firmware page as well:
    // one rebuild in, and the retained page comes back on dismissal.
    return this.onFirmwarePage() || this.nativeOverlay !== undefined;
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
        this.layouts.assistantAnswerLayout(body, { status: this.nativeHeader(this.nativePageTitle()), hint }),
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
    // Every page's gestures in the one template (GesturePolicy.formatHint).
    const hint = containerID === ContainerId.Hint;
    const replacement = (hint ? GesturePolicy.formatHint(content) : content) || ' ';
    const before = hint ? Math.max(previous.length, GesturePolicy.formatHint(previous).length) : previous.length;
    return this.client.require().textContainerUpgrade(new TextContainerUpgrade({
      containerID,
      containerName,
      contentOffset: 0,
      contentLength: Math.max(before, replacement.length),
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
    return true;
  }

  /** The Mac's list of unread notifications, newest first. */
  acceptNotifications(cards: readonly HudNotificationCard[]): void {
    this.notificationStack = cards
      .filter((card) => /^[a-f0-9]{32}$/i.test(card.id) && typeof card.body === 'string')
      .slice(0, 10)
      .map((card) => ({ ...card }));
    const ids = new Set(this.notificationStack.map((card) => card.id));
    for (const id of [...this.notificationsReadHere]) if (!ids.has(id)) this.notificationsReadHere.delete(id);
    // A card drawn from the Mac's short line before the list arrived is
    // redrawn whole now.
    const shown = this.notificationCard;
    const full = shown && this.notificationStack.find((card) => card.id === shown.card.id);
    if (shown && full && this.hasNotificationCard()
      && (shown.provisional || full.body !== shown.card.body || full.sender !== shown.card.sender)) {
      shown.card = full;
      shown.provisional = false;
      void this.paintNotificationCard();
      return;
    }
    // Home's Notifications tab lists the unread cards: keep it current. A
    // repaint that changes nothing sends nothing (paintOverviewAsText).
    if (!shown && nativeOverviewEnabled && this.rail.active() === 'notifications') {
      void this.repaintRestingPane();
    }
  }

  /** Unread cards, newest first, that the wearer has not marked read here. */
  unreadNotifications(): HudNotificationCard[] {
    return this.notificationStack.filter((card) => !this.notificationsReadHere.has(card.id));
  }

  hasNotificationCard(): boolean {
    return this.notificationCard !== undefined && this.pageOwner === 'notification-native';
  }

  /** A new notification: its card pops up over the page. Not over a
   * decision, a question or JOAT's conversation, which own the glass until
   * they end; it then waits as unread (Home's bell, the Notifications tab). */
  private async popNotification(id: string, renderedText: string): Promise<boolean> {
    if (!this.visual || !this.displayActive || this.confirmation || this.promptQuestion
      || this.assistantConversation.isActive() || this.assistantOverlay) return false;
    const full = this.notificationStack.find((card) => card.id === id);
    const card = full ?? cardFromRenderedText(id, renderedText, Date.now());
    this.showNotificationCard(card, false, !full);
    return this.paintNotificationCard();
  }

  /** Home's Notifications tab: the newest unread card, to read at leisure. */
  async browseNotifications(): Promise<boolean> {
    const newest = this.unreadNotifications()[0];
    if (!newest || !this.visual || this.confirmation || this.promptQuestion
      || this.assistantConversation.isActive()) return false;
    this.showNotificationCard(newest, true, false);
    return this.paintNotificationCard();
  }

  private showNotificationCard(card: HudNotificationCard, browsing: boolean, provisional: boolean): void {
    const previous = this.notificationCard;
    if (previous?.timer !== undefined) clearTimeout(previous.timer);
    const state = {
      card, browsing: browsing || (previous?.browsing ?? false), provisional,
      revision: ++this.notificationCardRevision,
    } as NonNullable<HudPresenter['notificationCard']>;
    this.notificationCard = state;
    this.armNotificationCardTimer();
  }

  private armNotificationCardTimer(): void {
    const state = this.notificationCard;
    if (!state) return;
    if (state.timer !== undefined) clearTimeout(state.timer);
    state.timer = undefined;
    if (state.browsing) return;
    const revision = state.revision;
    state.timer = setTimeout(() => {
      if (this.notificationCard?.revision === revision) void this.closeNotificationCard();
    }, NOTIFICATION_CARD_MS);
  }

  /** The card over the page as it is now; the page's column, if any, is
   * repainted with nothing under the card. */
  private async paintNotificationCard(): Promise<boolean> {
    const state = this.notificationCard;
    if (!state) return false;
    const unread = this.unreadNotifications();
    const list = unread.some((card) => card.id === state.card.id) ? unread : [state.card, ...unread];
    const index = Math.max(0, list.findIndex((card) => card.id === state.card.id));
    const layout = notificationCardLayout(
      state.card, { index, count: list.length }, Date.now(), this.nativeHeader(this.nativePageTitle()),
    );
    const page = overlayNotificationCard(layout, this.conversationUnderlay());
    const shown = await this.replaceNativePage(page, 'notification-native');
    if (!shown || this.notificationCard !== state) return false;
    const bottom = notificationCardBottom(page);
    if (bottom !== undefined && hasHomeImages(page) && this.surface.kind === 'default') {
      const frame = this.visualFrame();
      if (frame.kind === 'overview') {
        const column = this.homeColumnFor(frame, this.overviewView(frame));
        if (column) {
          void this.paintHomeColumn(
            { ...column, clipTop: bottom + NOTIFICATION_CARD.gap, dim: true }, this.captureDisplayGuard(),
          );
        }
      }
    }
    return true;
  }

  /** A tap on the card: it is read; the next unread takes its place, or the
   * card closes after the last. Returns the id read, for the Mac. */
  tapNotificationCard(): string | undefined {
    const state = this.notificationCard;
    if (!state || !this.hasNotificationCard()) return undefined;
    const read = state.card.id;
    this.notificationsReadHere.add(read);
    // Home's own copy of it goes too, in state only (the card is on the
    // glass): Home comes back without it before the Mac's retire arrives.
    this.retiredNotificationIds.add(read);
    this.removeRetiredNotifications();
    const next = this.unreadNotifications().find((card) => card.id !== read);
    if (next) {
      state.card = next;
      state.provisional = false;
      state.revision = ++this.notificationCardRevision;
      this.armNotificationCardTimer();
      void this.paintNotificationCard();
    } else {
      void this.closeNotificationCard();
    }
    return read;
  }

  /** Close the card, the page beneath as it is now. */
  async closeNotificationCard(): Promise<boolean> {
    if (!this.notificationCard) return false;
    const onGlass = this.pageOwner === 'notification-native';
    this.clearNotificationCard();
    return onGlass ? this.restoreRetainedPage() : true;
  }

  private clearNotificationCard(): void {
    if (this.notificationCard?.timer !== undefined) clearTimeout(this.notificationCard.timer);
    this.notificationCard = undefined;
  }

  /** Only a fully visible Home card is tappable. Focus pages keep incoming
   * notifications deferred, assistant overlays keep their higher priority,
   * and confirmations remain exclusive. Home in firmware text never draws a
   * notification in its pane (the card over the page shows it), so there
   * is none there to tap away: Home's tap opens the lit tab. */
  hasVisibleNotification(): boolean {
    return !(nativeOverviewEnabled && this.visual)
      && this.surface.kind === 'default'
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
    if (this.statusModel) this.statusModel = { ...this.statusModel, mic: open };
  }

  /**
   * The status facts, from Chrome. On the visual page they ride the next
   * frame; on a firmware page that declares the status container they are
   * one in-place upgrade of its header line; on the two native pages
   * without one they wait in the stream for the next page. The untitled
   * line also goes into the StatusBar stream so the text-only paths and the
   * capture/restore logic see the same facts.
   */
  async setStatusModel(model: HudStatusModel): Promise<boolean> {
    this.statusModel = { ...model };
    this.micOn = model.mic;
    if (this.imageHeaderShown) {
      // One header on every firmware page: redraw the spine and send only
      // the half that changed.
      this.streams.set(ContainerId.StatusBar, nativeStatusLine(model));
      return this.paintImageHeader();
    }
    if (this.readerUnderBox !== undefined && this.visual && this.evenFontReaderRetained()) {
      // The reader's tiles under JOAT's box carry the header.
      this.streams.set(ContainerId.StatusBar, nativeStatusLine(model));
      const frame = this.readerFrame(this.readerUnderBox);
      return frame ? this.visual.paint(frame).catch(() => false) : Promise.resolve(false);
    }
    if (this.assistantConversation.isActive()) {
      this.streams.set(ContainerId.StatusBar, nativeStatusLine(model));
      return this.assistantConversation.setStatus(this.nativeHeader(this.nativePageTitle()));
    }
    if (this.pageOwner === 'terminal-conversation-native') {
      this.streams.set(ContainerId.StatusBar, nativeStatusLine(model));
      return this.nativeTerminal.setStatus(this.nativeHeader(this.nativePageTitle()));
    }
    if (this.pageOwner !== 'visual' && NATIVE_STATUS_OWNERS.has(this.pageOwner)) {
      this.streams.set(ContainerId.StatusBar, nativeStatusLine(model));
      const header = this.nativeHeader(this.nativePageTitle());
      const previous = this.nativeStatusPainted;
      if (header === previous) return true;
      const displayCurrent = this.captureDisplayGuard();
      this.nativeStatusPainted = header;
      try {
        const ok = await this.upgradeNativeText(ContainerId.StatusBar, 'status', header, previous ?? '');
        return ok && displayCurrent();
      } catch {
        return false;
      }
    }
    return this.showText(nativeStatusLine(model), ContainerId.StatusBar);
  }

  /** The one-line header a firmware page shows: clock · page · mic · battery
   * from the model when Chrome has supplied one; otherwise the page's name
   * and whatever line the StatusBar stream holds. */
  private nativeHeader(pageTitle?: string): string {
    if (this.statusModel) {
      return fitHeader(nativeStatusLine(this.withActivities(this.statusModel, { title: pageTitle }), pageTitle));
    }
    return fitHeader([pageTitle?.trim(), this.streams.get(ContainerId.StatusBar)?.trim()]
      .filter((part): part is string => Boolean(part))
      .join(' · '));
  }

  /** The name the header carries for the page the presenter holds. */
  private nativePageTitle(): string | undefined {
    // A conversation is JOAT's own page, whatever feature it covers.
    if (this.conversationInFront()) return undefined;
    switch (this.surface.kind) {
      case 'list': return this.surface.options.title;
      case 'navigation': return 'NAVIGATION';
      case 'productivity': return 'PLANNING';
      case 'brainstorm':
        return this.surface.brainstorm.screen === 'agent_terminal' ? 'AGENT TERMINAL' : 'BRAINSTORM';
      default: return undefined;
    }
  }


  async showList(
    items: string[], options: ListSurfaceOptions = {}, isCurrent?: () => boolean,
  ): Promise<boolean> {
    if (!await this.prepareAssistantConversationHandoff()) return false;
    if (!isFirmwareList(options, this.visual !== undefined) && !await this.prepareNativeTerminalHandoff()) return false;
    if (isCurrent && this.usesNativeTextPage(options)) {
      return this.renderNativeTextPage({
        kind: 'list', items: [...items], options: this.resolveListOptions(options), selectedIndex: 0,
      }, isCurrent);
    }
    if (isCurrent) {
      try { if (!isCurrent()) return false; } catch { return false; }
    }
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
    if (this.usesEvenFontPage(surface.options)) return this.renderEvenFontPage(surface, current);
    const from = this.tracePage();
    const liveStatus = this.captureLiveStatus();
    const source = surface.items.join('\n');
    // The Mac's 62-column slices joined back into paragraphs, then set at
    // the page's own width: a slice per line left ragged short lines.
    // A Messages page stands each message's wrapped rows under its words,
    // after the sender's name (the wearer's ruling, 2026-10-07).
    const paginate = surface.options.title.startsWith('MESSAGES') ? paginateHanging : paginateMeasured;
    const pages = paginate(
      readerParagraphs(surface.items).join('\n'), READER_TEXT_PAGE.textWidth, READER_TEXT_PAGE.lineCapacity,
    );
    const page = this.nativeTextPage?.source === source
      ? Math.min(this.nativeTextPage.page, pages.length - 1)
      : surface.options.nativeTextPageFromEnd ? pages.length - 1 : 0;
    const body = pages[page] ?? ' ';
    const hint = textPageHint(page, pages.length, surface.options.hint, surface.options.reviewTapOnLastPage);
    const ok = await this.replaceNativePage(
      this.layouts.textPageLayout(body, { status: this.nativeHeader(surface.options.title), hint }, { inset: true }),
      'text-page-native',
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

  /** Whether the firmware text page shows its last local page, where a
   * review's tap counts. True when no text page is on the glass: the state
   * of an earlier one must never hold a later page's tap back. */
  isTextPageAtEnd(): boolean {
    const state = this.nativeTextPage;
    if (!state || (this.pageOwner !== 'text-page-native' && !this.evenFontPageOnGlass())) return true;
    return state.page >= state.pages.length - 1;
  }

  /** Page the firmware text page locally. False at an edge, so the caller
   * can ask the Mac for its neighbouring page. */
  scrollTextPage(direction: 'up' | 'down'): boolean {
    const state = this.nativeTextPage;
    const macHint = this.surface.kind === 'list' && this.usesNativeTextPage(this.surface.options)
      ? this.surface.options.hint
      : this.surface.kind === 'productivity' ? productivityActionHint(this.surface.productivity) : undefined;
    const evenFont = this.evenFontPageOnGlass();
    if (!state || (this.pageOwner !== 'text-page-native' && !evenFont) || macHint === undefined) return false;
    const page = direction === 'down' ? state.page + 1 : state.page - 1;
    if (page < 0 || page >= state.pages.length) return false;
    if (evenFont) {
      // The image page repaints; only the tiles whose pixels changed are sent.
      const review = this.surface.kind === 'list' && this.surface.options.reviewTapOnLastPage === true;
      state.page = page;
      state.body = state.pages[page] ?? ' ';
      state.hint = textPageHint(page, state.pages.length, macHint, review);
      void this.paintVisual();
      return true;
    }
    const previous = { body: state.body, hint: state.hint };
    const review = this.surface.kind === 'list' && this.surface.options.reviewTapOnLastPage === true;
    state.page = page;
    state.body = state.pages[page] ?? ' ';
    state.hint = textPageHint(page, state.pages.length, macHint, review);
    void (async () => {
      await this.upgradeNativeText(ContainerId.Main, 'main', state.body, previous.body);
      if (this.nativeTextPage !== state) return;
      await this.upgradeNativeText(ContainerId.Hint, 'hint', state.hint, previous.hint);
    })().catch(() => undefined);
    return true;
  }

  /** Paint a complete navigation snapshot. The Mac/session owns the state;
   * this presenter only gives it exclusive focus and exact restoration. */
  /** A retained route does not own input over a different visible feature. */
  isNavigationVisible(): boolean {
    return this.navigationOnGlass();
  }

  /**
   * A route's progress. While the route owns the glass (its page, or Home
   * with nothing else open) it repaints exactly as `showNavigation` does.
   * Behind another page — a conversation, a terminal, a list, a reader, an
   * answer, a question or a decision gate — it never takes the glass: the
   * retained page keeps the newest frame for when the wearer comes back,
   * and the header's chip carries the next turn and its distance meanwhile.
   * Until 2026-10-05 every frame (once a second while walking) went through
   * `showNavigation`, which hands off whatever was on the glass, so a
   * conversation or a Pomodoro page lasted until the next step.
   */
  async updateNavigation(navigation: NavigationHudModel, statusOverride?: string): Promise<boolean> {
    if (this.navigationOwnsGlass()) return this.showNavigation(navigation, statusOverride);
    if (!this.displayActive) return false;
    this.noteRoute(navigation);
    const latest: HudSurface = { kind: 'navigation', navigation: this.cloneNavigation(navigation) };
    if (this.surface.kind === 'navigation') this.surface = latest;
    if (this.confirmation?.surface.kind === 'navigation') this.confirmation.surface = latest;
    this.refreshActivities();
    return true;
  }

  /** The route ended: its chip leaves every header. */
  navigationEnded(): void {
    this.routeActivity = undefined;
    this.refreshActivities();
  }

  private noteRoute(navigation: NavigationHudModel): void {
    if (isRouteProgressView(navigation)) this.routeActivity = this.cloneNavigation(navigation);
  }

  /** Nothing the wearer chose is in front of the route: its own page, or
   * Home. A firmware page, a conversation or a pending gate always is. */
  private navigationOwnsGlass(): boolean {
    if (this.confirmation || this.promptQuestion) return false;
    if (this.conversationInFront() || this.nativeTerminal.isActive()) return false;
    if (this.pageOwner !== 'visual' && this.pageOwner !== 'navigation-native') return false;
    return this.surface.kind === 'navigation' || this.surface.kind === 'default';
  }

  private navigationOnGlass(): boolean {
    return this.surface.kind === 'navigation' && !this.conversationInFront()
      && !this.confirmation && !this.promptQuestion
      && (this.pageOwner === 'visual' || this.pageOwner === 'navigation-native');
  }

  /** The conversation is on the glass — not merely being handed back. */
  private conversationInFront(): boolean {
    return this.assistantConversation.isActive() && !this.restoringAssistantConversation;
  }

  /**
   * The other features still running, as chips for one page: the page
   * being built names itself (its header word, or its image frame), and
   * the feature it already shows has no chip of its own there — the map
   * carries no turn chip, the Pomodoro page no timer chip (ActivityChips).
   */
  private activityChips(page: { title?: string; frame?: HudVisualFrame['kind'] }): string[] {
    const title = page.title?.trim().toUpperCase() ?? '';
    const showsRoute = page.frame === 'navigation' || title.startsWith('NAVIGATION');
    const showsPomodoro = title === 'POMODORO'
      || (page.frame === 'overview' && this.rail.active() === 'pomodoro');
    const chips: string[] = [];
    const pomodoro = this.rail.content('pomodoro')?.pomodoro;
    if (pomodoro && !showsPomodoro) {
      const chip = pomodoroChip(pomodoro, Date.now());
      if (chip) chips.push(chip);
    }
    if (this.routeActivity && !showsRoute) {
      const chip = navigationChip(this.routeActivity);
      if (chip) chips.push(chip);
    }
    return chips;
  }

  /** The page on the glass, as activityChips needs to know it. */
  private pageOnGlass(): { title?: string; frame?: HudVisualFrame['kind'] } {
    if (this.conversationInFront() || this.pageOwner !== 'visual') {
      const title = this.nativePageTitle();
      return title === undefined ? {} : { title };
    }
    return { frame: this.baseVisualFrame().kind };
  }

  /** The status model with this page's chips; also records what the glass
   * is about to be told, so a later refresh writes only a change. */
  private withActivities(
    model: HudStatusModel, page: { title?: string; frame?: HudVisualFrame['kind'] },
  ): HudStatusModel {
    const chips = this.activityChips(page);
    this.shownActivities = chips.join('\u0000');
    const next: HudStatusModel = {
      clock: model.clock, link: model.link, mic: model.mic, charging: model.charging,
      ...(model.battery !== undefined ? { battery: model.battery } : {}),
      ...(model.macState !== undefined ? { macState: model.macState } : {}),
    };
    return chips.length > 0 ? { ...next, activities: chips } : next;
  }

  /** Re-show the header when the running features read differently: one
   * in-place header upgrade on a firmware page, one spine tile on an image
   * page, nothing when the chips are unchanged. */
  private refreshActivities(): void {
    if (!this.statusModel || !this.displayActive) return;
    if (this.activityChips(this.pageOnGlass()).join('\u0000') === this.shownActivities) return;
    void this.setStatusModel(this.statusModel).catch(() => false);
  }

  private armActivityTicker(): void {
    this.clearActivityTicker();
    const pomodoro = this.rail.content('pomodoro')?.pomodoro;
    if (!pomodoro || !this.displayActive) return;
    const delay = pomodoroTickDelayMs(pomodoro, Date.now(), 60_000);
    if (delay === undefined) return;
    this.activityTicker = setTimeout(() => {
      this.activityTicker = undefined;
      this.refreshActivities();
      this.armActivityTicker();
    }, delay);
  }

  private clearActivityTicker(): void {
    if (this.activityTicker !== undefined) clearTimeout(this.activityTicker);
    this.activityTicker = undefined;
  }

  async showNavigation(navigation: NavigationHudModel, statusOverride?: string): Promise<boolean> {
    if (!this.visual) return false;
    this.noteRoute(navigation);
    if (!await this.prepareAssistantConversationHandoff()) return false;
    // Every frame before the live map (confirm the place, choose the mode,
    // the preview, the warnings, arrival, an error) is a text card: Even's
    // own text page, one rebuild in and an in-place upgrade for a cursor
    // move. Only the guidance frame with its map is pixels.
    const native = navigationNativeBody(navigation);
    // The map rebuilds the image page; a text page replaces whatever page is
    // on the glass directly, unless the terminal must hand over first.
    if ((native === undefined || this.nativeTerminal.isActive())
      && !await this.prepareNativeTerminalHandoff()) return false;
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
        this.layouts.textPageLayout(native.body, { status: this.nativeHeader('NAVIGATION'), hint: native.hint }),
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
  /** Swipe on the brainstorm/terminal surface pages through history. */
  scrollTranscript(direction: 'up' | 'down'): boolean {
    if (this.surface.kind !== 'brainstorm') return false;
    return this.nativeTerminal.scroll(direction);
  }

  async showBrainstorm(
    brainstorm: BrainstormHudModel, statusOverride?: string, isCurrent?: () => boolean,
  ): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent()) return false;
    if (this.confirmation || (isCurrent && !isCurrent())) return false;
    if (!await this.prepareAssistantConversationHandoff()) return false;
    const shown = await this.showNativeTerminal(brainstorm, statusOverride, isCurrent);
    if (shown && brainstorm.screen === 'brainstorm') this.armThinkingTicker();
    return shown;
  }

  /** While a Brainstorm seat's row is the silent "Thinking…" marker, move
   * its elapsed counter on the glass once a second: one in-place body
   * upgrade on the native page, no Mac frame. The Mac ships nothing until
   * the seat produces a token, so the counter is device-local. */
  private thinkingTicker?: ReturnType<typeof setTimeout>;
  /** How long the counter has been ticking, so it can slow down. */
  private thinkingTicks = 0;
  /** Past a minute of silence the wearer is deciding whether to cancel,
   * not reading digits: the counter drops to one write every five seconds. */
  private static readonly THINKING_PRECISE_S = 60;

  private armThinkingTicker(): void {
    if (!this.displayActive || this.thinkingTicker !== undefined) return;
    if (this.surface.kind !== 'brainstorm' || !this.nativeTerminal.isThinking()) {
      this.thinkingTicks = 0;
      return;
    }
    this.thinkingTicks += 1;
    const interval = this.thinkingTicks < HudPresenter.THINKING_PRECISE_S ? 1000 : 5000;
    this.thinkingTicker = setTimeout(() => {
      this.thinkingTicker = undefined;
      if (this.surface.kind === 'brainstorm' && this.nativeTerminal.tick()) this.armThinkingTicker();
      else this.thinkingTicks = 0;
    }, interval);
  }

  private async showNativeTerminal(
    terminal: BrainstormHudModel,
    statusOverride?: string,
    isCurrent?: () => boolean,
  ): Promise<boolean> {
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent()) return false;
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
      this.nativeHeader(this.nativePageTitle()),
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
    const stayNative = !this.nativeTerminal.isActive() && this.onFirmwarePage();
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
    return this.renderNativeProductivityPage(surface, statusOverride);
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
      this.layouts.textPageLayout(body, { status: this.nativeHeader('PLANNING'), hint }), 'text-page-native',
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
    if (this.visual !== undefined && !this.confirmation
      && this.surface.kind !== 'productivity' && this.onFirmwarePage()) {
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
      ? await this.replaceWithNativeTerminalList(this.nativeListLayout(surface))
      : this.visual
      ? true
      : await this.rebuildPage(nativeReplyPage
          ? this.layouts.defaultLayout()
          : this.layouts.listLayout(surface.items, {
            ...surface.options, status: this.nativeHeader(surface.options.title),
          }));
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
    const moved = this.rail.setActive(tab);
    if (!this.followRail() && !moved) return true;
    return this.repaintRestingPane();
  }

  /** A swipe on the resting overview: one tab along the rail. Returns the
   * tab now active, or undefined at either end of the rail. The pane
   * shows the tab from this swipe on, even when the swipe was at an end
   * and the tab stayed put (it may have held a reply until now). */
  async stepTab(delta: number): Promise<PanelTab | undefined> {
    const next = this.rail.step(delta);
    const released = this.followRail();
    if (next !== undefined || released) await this.repaintRestingPane();
    return next;
  }

  /** Home's pane follows the rail from now on. True when it did not before. */
  private followRail(): boolean {
    const released = !this.homePaneFollowsRail;
    this.homePaneFollowsRail = true;
    return released;
  }

  /** Cache a Mac push. Repaints only when the pushed tab is on the glass;
   * an expanded page for that tab is CapabilityMenu's to refresh. */
  async acceptPanel(message: HudPanelMessage): Promise<PanelContent> {
    const content = this.rail.accept(message);
    if (this.rail.active() === message.tab) await this.repaintRestingPane();
    if (message.tab === 'pomodoro') {
      this.armActivityTicker();
      this.refreshActivities();
    }
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
   * minute on the resting overview, and it stops the moment the timer is
   * not on the glass or not running.
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
      void this.paintVisual();
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
      return pomodoroTickDelayMs(pomodoro, now, 60_000);
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
    if (!await this.prepareNativeTerminalHandoff(true)) return false;
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
    purpose?: ConfirmationPurpose,
  ): Promise<boolean> {
    if (isCurrent) {
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
        ready ? this.showConfirmation(prompt, id, diagnosticKind, isCurrent, purpose) : false
      ));
    }
    if (this.nativeTerminal.isActive() || this.pageOwner === 'terminal-list-native') {
      return this.prepareNativeTerminalHandoff().then((ready) => (
        ready ? this.showConfirmation(prompt, id, diagnosticKind, isCurrent, purpose) : false
      ));
    }
    // Asking for text never survives a transition into write consent. The
    // question's paints settle first: a words-so-far rebuild still running
    // would make the page guard refuse the consent card's own rebuild.
    const askedPrompt = this.promptQuestion;
    this.promptQuestion = undefined;
    if (askedPrompt && !askedPrompt.inlineTerminal) {
      return this.settlePromptPaints(askedPrompt).then(() => (
        this.showConfirmation(prompt, id, diagnosticKind, isCurrent, purpose)
      ));
    }
    if (!this.confirmation) {
      const content = this.surface.kind !== 'default' && this.focusReturn
        ? this.focusReturn
        : { streams: this.streams, brightness: this.brightness, kinds: this.kinds, notificationId: this.notificationId };
      this.confirmation = {
        id,
        prompt: safePrompt,
        ...(diagnosticKind ? { diagnosticKind } : {}),
        ...(purpose ? { purpose } : {}),
        ...(isCurrent ? { isCurrent } : {}),
        surface: this.cloneSurface(this.surface),
        streams: new Map(content.streams),
        brightness: new Map(content.brightness),
        kinds: new Map(content.kinds), notificationId: content.notificationId,
      };
    } else {
      // Claim ownership synchronously, before an awaited bridge call. A stale
      // result callback can now only enqueue a no-op dismissal for its old id.
      this.confirmation = { ...this.confirmation, id, prompt: safePrompt, diagnosticKind, purpose, isCurrent };
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
    // A page replacement already running (the page a spoken answer's
    // question restores as it closes) would make the guard refuse the
    // consent card: the Mac's confirmation lands while that restore is in
    // flight, the card is refused, and the draft is cancelled — the wearer
    // speaks and the card just goes away (simulator, 2026-10-08). Let it
    // finish; the consent card then replaces it.
    while (this.pageReplacement) {
      const running = this.pageReplacement;
      await running.catch(() => false);
      if (this.pageReplacement === running) break;
    }
    if (!displayCurrent()) return false;
    const current = this.confirmation;
    if (!current || current.id !== id || (claimed && current !== claimed)) return !claimed?.isCurrent;
    if (current.isCurrent && !this.isConfirmationCurrent(current)) {
      this.confirmation = undefined;
      await this.restoreAfterConfirmation(current);
      return false;
    }
    const liveStatus = this.captureLiveStatus();
    // The decision gate is a firmware text page: one rebuildPageContainer
    // (~165 ms) instead of four forced image tiles at 0.2–2 s each, so
    // "Send to X?" is readable before the Mac's confirmation envelope has
    // been spent on pixels.
    const header = this.visual ? this.nativeHeader(this.nativePageTitle()) : liveStatus?.text;
    const draft = current.diagnosticKind === 'messages' ? parseMessageDraft(current.prompt) : undefined;
    // A reply's consent is the same gate in JOAT's conversation card: the
    // question it answered, then the wearer's exact words, then the gestures.
    const layout = draft
      ? overlayConversation(this.layouts.assistantConversationLayout(replyDraftView([
        { role: 'assistant', text: `What do you want to say back to ${draft.recipient}?` },
        { role: 'user', text: draft.body },
      ]), header), this.conversationUnderlay())
      : this.layouts.confirmationLayout(current.prompt, header, current.purpose);
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
    const labels = confirmationLabels(current.purpose);
    this.streams.set(ContainerId.Main, `${labels.title}\n\n${current.prompt}`);
    this.streams.set(ContainerId.Context, labels.choices);
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
      if (rebuilt && current()) this.imageHeaderShown = false;
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
        ? this.layouts.confirmationLayout(
          this.confirmation.prompt, this.nativeHeader(this.nativePageTitle()), this.confirmation.purpose,
        )
        : this.surface.kind === 'list' && !this.nativeReplyPage
          ? this.layouts.listLayout(this.surface.items, {
            ...this.surface.options, status: this.nativeHeader(this.surface.options.title),
          })
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
      ? this.statusModel
        ? this.nativeHeader(this.nativePageTitle())
        : [stored.match(/^\d{1,2}:\d{2}/)?.[0], `MIC ${this.micOn ? 'ON' : 'OFF'}`,
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
      ...(isFirmwareList(options, this.visual !== undefined) ? { nativeFirmwareList: true } : {}),
      ...(options.nativeTextPage ? { nativeTextPage: true } : {}),
      ...(options.nativeTextPage && options.nativeTextPageFromEnd ? { nativeTextPageFromEnd: true } : {}),
      ...(options.nativeTextPage && options.reviewTapOnLastPage ? { reviewTapOnLastPage: true } : {}),
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
    const frame = this.baseVisualFrame();
    return this.statusModel
      ? { ...frame, statusModel: this.withActivities(this.statusModel, { frame: frame.kind }) }
      : frame;
  }

  private baseVisualFrame(): HudVisualFrame {
    const status = this.streams.get(ContainerId.StatusBar) ?? '';
    // A confirmation, a question, a conversation page and a Planning outcome
    // are firmware pages; while one is on the glass the image page beneath
    // is not painted (pageOwner), so this frame describes the retained page.
    const reader = this.nativeTextPage;
    if (this.surface.kind === 'list' && reader?.lines && this.evenFontPageOnGlass()) {
      return this.withOverlays({
        kind: 'reader',
        status,
        micOn: this.micOn,
        title: this.surface.options.title,
        lines: (reader.lines[reader.page] ?? []).map((line) => ({ ...line })),
        hint: reader.hint,
      });
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
    // With Home in firmware text a notification is the card's to show, never
    // the pane's, so an image page composited over Home leaves it out too.
    const homeCard = nativeOverviewEnabled && this.kinds.get(ContainerId.Main) === 'notification';
    return this.withOverlays({
      kind: 'overview',
      status,
      micOn: this.micOn,
      ...(this.ambient ? { ambient: this.ambient } : {}),
      mainBrightness: this.brightness.get(ContainerId.Main)
        ?? DEFAULT_BRIGHTNESS[ContainerId.Main]!,
      main: homeCard ? '' : this.streams.get(ContainerId.Main) ?? '',
      mainKind: homeCard ? undefined : this.kinds.get(ContainerId.Main),
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
    if (this.surface.kind === 'brainstorm') {
      if (this.nativeTerminal.isActive()) return this.nativeTerminal.repaint();
      return this.nativeTerminal.present(this.surface.brainstorm,
        this.nativeHeader(this.nativePageTitle()), displayCurrent);
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
          this.armPanelTicker();
        }
        return painted;
      }

      const surface = this.surface;
      const rebuilt = surface.kind === 'list'
        ? await this.rebuildPage(
          this.layouts.listLayout(surface.items, {
            ...surface.options, status: this.nativeHeader(surface.options.title),
          }),
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

  /** The text overview the glass shows, and the page write that put it
   * there: any later full-page write (a list, a reader, the image page)
   * replaces it, and an upgrade must never land on that other page. */
  private overviewText?: { epoch?: number; bands: Map<number, OverviewBand> };

  private overviewTextOnGlass(): boolean {
    const shown = this.overviewText;
    if (!shown || this.pageOwner !== 'visual') return false;
    const epoch = this.client.pageEpoch?.();
    return epoch === undefined || shown.epoch === undefined || epoch === shown.epoch;
  }

  /** Home as the wearer would see it now, when it is a plain overview: no
   * outcome strip or receipt composited over it, no gate or question. */
  private plainOverviewFrame(): Extract<HudVisualFrame, { kind: 'overview' }> | undefined {
    if (!nativeOverviewEnabled || !this.visual || this.confirmation || this.promptQuestion) return undefined;
    if (this.surface.kind !== 'default' || this.conversationInFront()) return undefined;
    const frame = this.visualFrame();
    return frame.kind === 'overview' && !frame.actionOverlay && !frame.assistantOverlay ? frame : undefined;
  }

  /** The header line Home carries: the same live line as every firmware
   * page, with the chips for what else is running (a Pomodoro chip only
   * when its tab is not the one lit, ActivityChips). */
  private overviewHeader(): string {
    if (this.statusModel) {
      return fitHeader(nativeStatusLine(this.withActivities(this.statusModel, { frame: 'overview' })));
    }
    return this.nativeHeader();
  }

  /** Home's text view of a frame: Main's content heads the pane until the
   * wearer swipes, and the Notifications tab lists the unread cards. */
  private overviewView(frame: Extract<HudVisualFrame, { kind: 'overview' }>): OverviewTextView {
    return composeOverviewText(frame, this.overviewHeader(), new Date(), {
      pinMain: !this.homePaneFollowsRail,
      unread: this.unreadNotifications(),
    });
  }

  /**
   * Home as six firmware text containers (HudPageBuilder.overviewTextLayout).
   *
   * Cost, by the everything-evenhub glasses-ui guidance: entering Home is
   * one rebuildPageContainer; afterwards only the bands that changed are
   * sent, each as a flicker-free textContainerUpgrade. A minute tick is the
   * header (one upgrade); a rail swipe is the rail and the pane (two). Three
   * or more changed bands are one rebuild, which is cheaper than three
   * upgrades. No image tile is ever sent for Home.
   *
   * `taking`: Back from another page — the page on the glass is not Home's
   * yet, and this rebuild replaces it.
   */
  private async paintOverviewAsText(
    frame: Extract<HudVisualFrame, { kind: 'overview' }>,
    taking = false,
  ): Promise<boolean> {
    // Taken BEFORE any await: a paint that started a moment before the
    // wearer expanded a tab must not land over the list that replaced Home.
    const current = this.captureDisplayGuard();
    const stillOurs = (): boolean => current() && this.surface.kind === 'default'
      && !this.confirmation && !this.promptQuestion && !this.conversationInFront()
      && !this.nativeTerminal.isActive()
      && (taking || this.pageOwner === 'visual' || this.visualPagePending());
    if (!stillOurs()) return false;
    const view = this.overviewView(frame);
    let layout = this.withHeader(this.layouts.overviewTextLayout(view));
    const column = this.homeColumnFor(frame, view);
    if (column) layout = withHomeImages(layout);
    const header = hasImageHeader(layout);
    // The column's images follow the page's text: a tab swipe moves the
    // pane first (an in-place upgrade), then the rail's one tile.
    const paintColumn = (): void => {
      if (column && hasHomeImages(layout)) void this.paintHomeColumn(column, stillOurs);
    };
    const bands = new Map<number, OverviewBand>((layout.textObject ?? []).map((t) => [
      t.containerID ?? 0,
      { name: t.containerName ?? '', content: t.content ?? '', ...(t.textColor !== undefined ? { textColor: t.textColor } : {}) },
    ]));
    const shown = !taking && this.overviewTextOnGlass() ? this.overviewText : undefined;
    if (shown) {
      const changed = [...bands].filter(([id, band]) => {
        const was = shown.bands.get(id);
        return was?.content !== band.content || was?.textColor !== band.textColor;
      });
      if (changed.length === 0) {
        if (header) void this.paintImageHeader();
        paintColumn();
        this.armPanelTicker();
        return true;
      }
      // A tab swipe changes three bands (the rail, the grid's labels and
      // its values): three in-place upgrades, never a rebuild, which would
      // also resend the page's images.
      if (changed.length <= 3) {
        let upgraded = true;
        for (const [containerID, band] of changed) {
          if (!stillOurs() || !this.overviewTextOnGlass()) return false;
          const previous = shown.bands.get(containerID)?.content ?? '';
          const ok = await this.client.require().textContainerUpgrade(new TextContainerUpgrade({
            containerID,
            containerName: band.name,
            contentOffset: 0,
            contentLength: Math.max(previous.length, band.content.length),
            content: band.content || ' ',
            ...(band.textColor !== undefined ? { textColor: band.textColor } : {}),
          }));
          if (!ok) { upgraded = false; break; }
          shown.bands.set(containerID, band);
        }
        if (!stillOurs()) return false;
        if (upgraded) {
          if (header) void this.paintImageHeader();
          paintColumn();
          this.armPanelTicker();
          return true;
        }
        // A refused upgrade means the container is not what we think it
        // is: fall through and rebuild the page.
      }
    }
    const replace = (): Promise<boolean> => (stillOurs()
      ? this.client.require().rebuildPageContainer(layout)
      : Promise.resolve(false));
    let ok = false;
    if (!this.visual?.withPageTransition) ok = await replace();
    else {
      try { ok = await this.visual.withPageTransition(replace, stillOurs); } catch { ok = false; }
    }
    if (!ok || !stillOurs()) return false;
    this.overviewText = { epoch: this.client.pageEpoch?.(), bands };
    // Home's page is the visual owner's page; the image painter must not
    // assume any tile it drew before is still on the glass.
    this.pageOwner = 'visual';
    this.visual?.forgetPage?.();
    this.imageHeaderShown = header;
    if (header) void this.paintImageHeader();
    paintColumn();
    this.armPanelTicker();
    return true;
  }

  private async paintVisual(): Promise<boolean> {
    this.removeRetiredNotifications();
    const displayCurrent = this.captureDisplayGuard();
    if (!displayCurrent()) return false;
    if (this.assistantConversation.isActive() && !this.restoringAssistantConversation) return true;
    if (this.pageOwner !== 'visual' && !this.visualPagePending()) return true;
    if (!this.visual) return false;
    // A decision gate or a question on its way to the glass is a firmware
    // page: the image page beneath it is not repainted while it is pending.
    if (this.confirmation || this.promptQuestion) return true;
    const home = this.plainOverviewFrame();
    if (home) return this.paintOverviewAsText(home);
    // An image frame over the text overview (or over a firmware page left
    // under Home) needs the four image containers back first.
    if ((this.overviewTextOnGlass() || this.visualPagePending()) && !await this.rebuildVisualPage()) return false;
    if (!displayCurrent()) return false;
    const notificationId = this.hasVisibleNotification() ? this.notificationId : undefined;
    const ok = await this.visual.paint(this.visualFrame());
    if (ok && displayCurrent() && notificationId === this.notificationId) {
      this.paintedNotificationId = notificationId;
    }
    if (!displayCurrent()) return false;
    // Keep the local elapsed counter moving between Mac frames.
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
      // The reader's own page: a return from the background resumes it
      // instead of starting the wearer over at page one.
      ...(this.nativeTextPage ? { textPage: { source: this.nativeTextPage.source, page: this.nativeTextPage.page } } : {}),
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
    const textPage = saved.textPage as { source?: unknown; page?: unknown } | undefined;
    if (typeof textPage?.source === 'string' && typeof textPage.page === 'number'
      && Number.isInteger(textPage.page) && textPage.page >= 0) {
      // Only the page index: the render that follows pages the rows again
      // and resumes here when they are the same rows.
      this.nativeTextPage = { source: textPage.source, pages: [], page: textPage.page, body: ' ', hint: '' };
    }
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
