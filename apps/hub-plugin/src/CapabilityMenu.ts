import { OsEventTypeList, type EvenHubEvent } from '@evenrealities/even_hub_sdk';
import type { HudPresenter } from './HudPresenter';
import type { Settings } from './Settings';
import type { MacLink } from './MacLink';
import { resolveMessagesViewState, type MessagesScreen } from './MessagesViewState';
import type {
  InputEventPayload,
  PlanningGridCell,
  ProductivityHudModel,
  ResumePolicy,
} from './types';

type ListEvent = NonNullable<EvenHubEvent['listEvent']>;

interface DataSurfaceSnapshot {
  title: string;
  items: string[];
  selectable: boolean;
  hint?: string;
  planningGrid?: PlanningGridCell[];
  selectedIndex: number;
  messagesRetry: boolean;
  messagesScreen?: MessagesScreen;
}

const PLANNING_ANCHOR_MAX_UNIX_MS = 32_503_680_000_000;
/**
 * The native bridge itself has a bounded call deadline. Give it a small
 * allowance, then replace the local loading frame with an honest retry state
 * instead of leaving a wearer on a spinner indefinitely.
 */
const CAPABILITY_RESPONSE_TIMEOUT_MS = 12_000;
/** Terminal native-write receipts must remain readable, then lead back to the
 * live, unified planner rather than stranding the wearer on a dead end. */
const PRODUCTIVITY_OUTCOME_RETURN_DELAY_MS = 5_000;

function isTerminalProductivityOutcome(phase: ProductivityHudModel['phase']): boolean {
  return phase === 'saved'
    || phase === 'monitoring'
    || phase === 'completed'
    || phase === 'failed'
    || phase === 'check'
    || phase === 'cancelled'
    || phase === 'not_saved';
}

function isPlanningAnchor(value: unknown): value is number {
  return typeof value === 'number'
    && Number.isSafeInteger(value)
    && value >= 0
    && value <= PLANNING_ANCHOR_MAX_UNIX_MS;
}

/**
 * The control-center navigation from the approved dashboard design:
 * left double-tap opens the CAPABILITY menu — Messages, Planning,
 * Navigation, Brainstorm, and the remaining views and actions by
 * scroll. Data entries open a Mac-drawn view; direct actions retain their
 * existing local or gateway contracts. The
 * first build shipped only the old resume-policy list here, so the wearer
 * double-tapped into "a menu modal, but not the one we designed" (their
 * words, on hardware). Settings is a scroll-reachable first-level action.
 *
 * View machine, and what double-tap means in each state:
 *   home ──double-tap──▶ caps ──pick row──▶ data view, settings, or direct action
 *   caps/settings/data ──double-tap──▶ home  (double-tap is BACK, always)
 */
// The visual sidebar shows five rows at a time and scrolls this one continuous
// list. Nothing is hidden behind a second "More" interaction: a wearer can
// keep scrolling from Messages through every available glasses capability or
// direct action. The renderer and G2 list limit both accept these 11 rows.
const CAPABILITIES = [
  'Messages', 'Planning', 'Navigation', 'Brainstorm',
  'Notifications', 'QuickNotes', 'Agent Terminal', 'Activity',
  'Repeat', 'Reconnect', 'Settings',
] as const;

const POLICY_ROWS: ReadonlyArray<{ policy: ResumePolicy; label: string }> = [
  { policy: 'auto', label: 'Resume mic: instantly' },
  { policy: 'mac', label: 'Resume mic: Mac decides' },
  { policy: 'confirm', label: 'Resume mic: ask me first' },
];

const SETTINGS_SURFACE = {
  title: 'LISTENING RESUME',
  // Only the left-temple double press owns contextual Back. Do not advertise
  // a generic double-click: the right/ring double gesture has other runtime
  // meanings.
  hint: 'tap: apply · left x2: back',
} as const;

export function isActionLifecycleTitle(title?: string): boolean {
  return title?.trim().toUpperCase().startsWith('ACTION ·') ?? false;
}

export class CapabilityMenu {
  private view: 'closed' | 'transition' | 'caps' | 'settings' | 'data' | 'productivity' = 'closed';
  private selectedIndex = 0;
  private dataItemCount = 0;
  private dataRowsSelectable = false;
  private dataSelectionPending = false;
  /** Some supported hosts emit a scroll while retaining the prior native-list
   * index. Preserve the locally mirrored row for that gesture's following
   * click; a later credible native index still takes precedence. */
  private dataSelectionUsesStaleNativeIndex = false;
  private messagesRetry = false;
  /** The Mac owns conversation pagination. The Hub only forwards a boundary
   * gesture when the currently visible, Mac-authored hint says that page exists. */
  private messagesScreen?: MessagesScreen;
  private dataHint?: string;
  private dataPagePending = false;
  /** Messages has two real read levels (conversation then full message), so
   * retain those exact Mac-authored frames for contextual Back. */
  private dataSurface?: DataSurfaceSnapshot;
  private dataReturn?: DataSurfaceSnapshot;
  private dataGrandparent?: DataSurfaceSnapshot;
  private pendingTitle?: string;
  private pendingRequestId?: string;
  private responseTimeout?: ReturnType<typeof setTimeout>;
  private productivityReturn?: 'Planning';
  private productivityAnchorUnixMs?: number;
  private productivityReturnTimer?: ReturnType<typeof setTimeout>;
  private productivityReturnEpoch = 0;
  /** A Planning row may open a day or item detail. Back returns to the live
   * schedule rather than throwing the wearer all the way to the overview. */
  private planningDrillDown = false;
  private transitionId = 0;
  /** Live mic state, injected so the restored status line tells the truth. */
  micIsOpen: () => boolean = () => false;

  constructor(
    private readonly hud: HudPresenter,
    private readonly settings: Settings,
    private readonly link: MacLink,
  ) {}

  isOpen(): boolean {
    return this.view !== 'closed';
  }

  /** A live session owns the overview, not the menu page that launched it.
   * Clear input ownership synchronously before rebuilding so the first
   * streamed frame cannot remain trapped behind the Brainstorm instructions. */
  async beginLiveSession(session: 'brainstorm'): Promise<void> {
    void session;
    this.clearResponseTimeout();
    this.clearProductivityReturn();
    this.transitionId += 1;
    this.view = 'closed';
    this.selectedIndex = 0;
    this.pendingTitle = undefined;
    this.pendingRequestId = undefined;
    this.dataItemCount = 0;
    this.dataRowsSelectable = false;
    this.dataSelectionPending = false;
    this.messagesRetry = false;
    this.messagesScreen = undefined;
    this.dataHint = undefined;
    this.dataPagePending = false;
    this.dataSurface = undefined;
    this.dataReturn = undefined;
    this.dataGrandparent = undefined;
    this.planningDrillDown = false;
    await this.hud.showDefault();
  }

  /** Contextual-menu entry: settings remains reachable without consuming one
   * of the five 576x288 sidebar rows. */
  async openSettings(): Promise<void> {
    this.pendingTitle = undefined;
    this.pendingRequestId = undefined;
    const transition = this.beginTransition();
    this.selectedIndex = 0;
    const shown = await this.hud.showList(
      POLICY_ROWS.map((row) => this.label(row)),
      SETTINGS_SURFACE,
    );
    if (!this.isCurrentTransition(transition)) return;
    if (shown) this.view = 'settings';
    else await this.close();
  }

  /**
   * Contextual-menu escape. This intentionally uses the same Mac-owned
   * Planning request as left-double Back: it cannot mutate Calendar or
   * Reminders, and it retains the active safe-write day's anchor when there
   * is one. The OS menu itself stays one-shot; the rebuilt Planning page
   * carries the contextual menu forward.
   */
  async openPlanning(): Promise<void> {
    const anchor = this.view === 'productivity' ? this.productivityAnchorUnixMs : undefined;
    await this.openCapability('Planning', CAPABILITIES.indexOf('Planning'), anchor);
  }

  /** Match, paint, then activate one capability response. Row actions remain
   * disabled until the replacement frame is completely visible. */
  async presentList(
    title?: string,
    items: string[] = [],
    requestId?: string,
    selectable = false,
    hint?: string,
    planningGrid?: PlanningGridCell[],
  ): Promise<boolean> {
    if (this.view !== 'data' || !this.pendingTitle || !title) return false;
    if (!this.pendingRequestId || requestId !== this.pendingRequestId) return false;
    const normalized = title.trim().toUpperCase();
    const matches = normalized === this.pendingTitle
      || normalized.startsWith(`${this.pendingTitle} ·`);
    if (!matches) return false;
    const messagesState = this.pendingTitle === 'MESSAGES'
      ? resolveMessagesViewState(title, selectable)
      : undefined;
    const effectiveSelectable = messagesState?.selectable ?? selectable;
    const effectiveHint = messagesState?.rowAction === 'retry'
      ? 'tap: retry · left x2: back'
      : hint;

    const parent = this.detailReturnSurface(title);
    if (parent) {
      this.dataGrandparent = this.dataReturn;
      this.dataReturn = parent;
    }

    this.clearResponseTimeout();
    this.messagesRetry = messagesState?.rowAction === 'retry';
    const expectedRequestId = this.pendingRequestId;
    const transition = this.beginTransition();
    this.dataRowsSelectable = false;
    this.dataItemCount = 0;
    const shown = await this.hud.showList(items, {
      title,
      hint: effectiveHint,
      selectable: effectiveSelectable,
      planningGrid,
    });
    if (
      !this.isCurrentTransition(transition)
      || this.pendingRequestId !== expectedRequestId
    ) {
      return true;
    }
    if (!shown) {
      await this.close();
      return true;
    }
    this.view = 'data';
    this.dataItemCount = items.length;
    this.dataRowsSelectable = effectiveSelectable;
    this.dataSelectionPending = false;
    this.dataSelectionUsesStaleNativeIndex = false;
    this.messagesScreen = messagesState?.screen;
    this.dataHint = effectiveHint;
    this.dataPagePending = false;
    this.selectedIndex = 0;
    this.dataSurface = {
      title,
      items: [...items],
      selectable: effectiveSelectable,
      hint: effectiveHint,
      planningGrid: planningGrid ? [...planningGrid] : undefined,
      selectedIndex: 0,
      messagesRetry: this.messagesRetry,
      messagesScreen: this.messagesScreen,
    };
    return true;
  }

  /** Safe writes can finish after the confirmation overlay closes. Their
   * verification/receipt page is a real focus state even when no menu request
   * is pending, but it is always read-only. */
  async presentLifecycle(
    title?: string,
    items: string[] = [],
    hint?: string,
  ): Promise<boolean> {
    if (!isActionLifecycleTitle(title)) return false;
    const transition = this.beginTransition();
    this.dataRowsSelectable = false;
    this.dataItemCount = 0;
    const shown = await this.hud.showList(items, { title, hint, selectable: false });
    if (!this.isCurrentTransition(transition)) return true;
    if (!shown) {
      await this.close();
      return true;
    }
    this.view = 'data';
    this.pendingTitle = 'ACTION';
    this.pendingRequestId = undefined;
    this.dataItemCount = items.length;
    this.dataRowsSelectable = false;
    this.selectedIndex = 0;
    return true;
  }

  /** A real Calendar/Reminders outcome screen. Back reloads the unified
   * native-backed Planning collection so an outcome cannot strand the wearer
   * on a stale Calendar- or Tasks-only list. */
  async presentProductivity(
    productivity: ProductivityHudModel,
    onVisible?: () => void,
  ): Promise<boolean> {
    this.clearResponseTimeout();
    const transition = this.beginTransition();
    this.dataRowsSelectable = false;
    this.dataItemCount = 0;
    const shown = await this.hud.showProductivity(productivity, undefined, onVisible);
    if (!this.isCurrentTransition(transition)) return false;
    if (!shown) {
      await this.close();
      return false;
    }
    this.view = 'productivity';
    this.productivityReturn = 'Planning';
    this.productivityAnchorUnixMs = isPlanningAnchor(productivity.planningAnchorUnixMs)
      ? productivity.planningAnchorUnixMs
      : undefined;
    this.pendingTitle = this.productivityReturn.toUpperCase();
    this.pendingRequestId = undefined;
    this.selectedIndex = 0;
    if (
      productivity.domain === 'reminders'
      && productivity.phase === 'saved'
      && productivity.monitoringArmed === true
    ) {
      this.scheduleReminderMonitoring(productivity);
    } else {
      this.scheduleProductivityReturn(productivity.phase);
    }
    // A deferred confirmation, failed paint, or newer transition can be
    // handled without a current visual page. Only a retained exact surface is
    // eligible for the due-HUD acknowledgement.
    const visible = this.hud.isProductivityVisible(productivity);
    if (visible) onVisible?.();
    return visible;
  }

  /** Left double-tap. Closed: open the capability menu. Otherwise return one
   * contextual level, preserving a list/detail parent where it exists. */
  async toggle(): Promise<void> {
    if (this.view === 'closed') {
      const transition = this.beginTransition();
      this.selectedIndex = 0;
      const shown = await this.hud.showSidebar([...CAPABILITIES], this.selectedIndex);
      if (!this.isCurrentTransition(transition)) return;
      if (shown) this.view = 'caps';
      else await this.close();
    } else if (this.view === 'productivity' && this.productivityReturn) {
      const capability = this.productivityReturn;
      await this.openCapability(
        capability,
        CAPABILITIES.indexOf(capability),
        this.productivityAnchorUnixMs,
      );
    } else if (
      this.view === 'data'
      && this.pendingTitle === 'PLANNING'
      && this.planningDrillDown
    ) {
      this.planningDrillDown = false;
      await this.openCapability('Planning', CAPABILITIES.indexOf('Planning'));
    } else if (this.view === 'data' && this.dataReturn) {
      await this.returnToDataSurface();
    } else if (this.view === 'data' || this.view === 'settings') {
      await this.returnToSidebar();
    } else {
      await this.close();
    }
  }

  /** Returns true when the event was consumed (a menu view is open). */
  handleList(list: ListEvent): boolean {
    if (this.view === 'closed') return false;
    if (this.view === 'productivity') return true;
    if (this.view === 'transition') return true;

    if (this.view === 'data') {
      const eventType = list.eventType ?? 0;
      const maximum = this.messagesRetry ? 0 : Math.max(0, this.dataItemCount - 1);
      if (eventType !== OsEventTypeList.CLICK_EVENT) {
        // Native List containers move their own highlight before reporting a
        // scroll. Mirror that index so a second swipe at an actual endpoint
        // reaches the Mac-owned conversation pager. This is intentionally
        // different from text-capture scrolls, where handleScroll() moves our
        // custom selection itself.
        const before = this.selectedIndex;
        const delta = eventType === OsEventTypeList.SCROLL_BOTTOM_EVENT
          ? 1
          : eventType === OsEventTypeList.SCROLL_TOP_EVENT
            ? -1
            : 0;
        const expected = Math.max(0, Math.min(maximum, before + delta));
        const reported = list.currentSelectItemIndex;
        const staleNativeIndex = delta !== 0
          && expected !== before
          && (
            reported === before
            || (this.dataSelectionUsesStaleNativeIndex && reported === 0)
          );
        this.selectedIndex = staleNativeIndex
          ? expected
          : Math.max(0, Math.min(maximum, reported ?? expected));
        this.dataSelectionUsesStaleNativeIndex = staleNativeIndex;
        if (staleNativeIndex) void this.hud.setSelection(this.selectedIndex);
        const stayedAtBoundary = before === this.selectedIndex
          && (this.selectedIndex === 0 || this.selectedIndex === maximum);
        const messagesReader = this.messagesScreen === 'message_detail';
        if (
          (messagesReader || stayedAtBoundary)
          && this.pendingTitle === 'MESSAGES'
          && (this.messagesScreen === 'conversation' || messagesReader)
          && this.canRequestMessagePage(eventType)
        ) {
          this.requestMessagePage(eventType);
        }
        return true;
      }
      const reported = list.currentSelectItemIndex;
      const preserveMirroredSelection = this.dataSelectionUsesStaleNativeIndex
        && reported === 0
        && this.selectedIndex !== 0;
      this.selectedIndex = preserveMirroredSelection
        ? this.selectedIndex
        : Math.max(0, Math.min(maximum, reported ?? this.selectedIndex));
      this.dataSelectionUsesStaleNativeIndex = false;
      return this.selectDataRow();
    }

    // `?? 0` on both reads: the protobuf transport omits zero-valued fields,
    // so CLICK_EVENT (0) and the first row's index (0) both arrive as
    // `undefined`. Without this the top row is unselectable.
    if ((list.eventType ?? 0) !== OsEventTypeList.CLICK_EVENT) {
      return true; // scrolls: host moves the highlight; keep them off the uplink
    }
    const index = list.currentSelectItemIndex ?? 0;
    this.selectedIndex = index;
    return this.select(index);
  }

  /** Custom image surfaces use the transparent text capture layer, so JOAT
   * owns selection movement instead of the firmware List widget. */
  handleScroll(eventType: number): boolean {
    if (this.view === 'closed') return false;
    if (this.view === 'productivity') return true;
    if (this.view === 'transition') return true;
    const delta = eventType === OsEventTypeList.SCROLL_BOTTOM_EVENT
      ? 1
      : eventType === OsEventTypeList.SCROLL_TOP_EVENT
        ? -1
        : 0;
    if (delta === 0) return true;
    if (this.view === 'data') {
      if (
        this.pendingTitle === 'MESSAGES'
        && (this.messagesScreen === 'conversation' || this.messagesScreen === 'message_detail')
        && this.canRequestMessagePage(eventType)
      ) {
        this.requestMessagePage(eventType);
        return true;
      }
      // Access/setup has one real G2 action: retry. Keep its first row
      // highlighted while the second row remains Mac Settings guidance.
      const maximum = this.messagesRetry ? 0 : Math.max(0, this.dataItemCount - 1);
      const next = Math.max(0, Math.min(maximum, this.selectedIndex + delta));
      const applied = next - this.selectedIndex;
      this.selectedIndex = next;
      if (applied !== 0) void this.hud.moveSelection(applied);
      return true;
    }
    const rows = this.view === 'settings' ? POLICY_ROWS : CAPABILITIES;
    this.selectedIndex = Math.max(0, Math.min(rows.length - 1, this.selectedIndex + delta));
    void this.hud.setSelection(this.selectedIndex);
    return true;
  }

  /** Right temple/ring click selects the highlighted custom-rendered row. */
  handleClick(): boolean {
    if (this.view === 'closed') return false;
    if (this.view === 'productivity') return true;
    if (this.view === 'transition') return true;
    if (this.view === 'data') {
      return this.selectDataRow();
    }
    return this.select(this.selectedIndex);
  }

  private selectDataRow(): true {
    if (this.messagesRetry) {
      this.messagesRetry = false;
      void this.openCapability('Messages', 0);
      return true;
    }
    if (!this.dataRowsSelectable || this.dataItemCount === 0) return true;
    if (this.dataSelectionPending) return true;
    this.dataSelectionPending = true;
    if (this.pendingTitle === 'PLANNING') this.planningDrillDown = true;
    const random = globalThis.crypto?.randomUUID?.()
      ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
    const traceId = `hud-${random}`;
    const readingMessage = this.pendingTitle === 'MESSAGES'
      && this.messagesScreen === 'conversation';
    const stage = readingMessage
      ? 'message-read-requested'
      : this.pendingTitle === 'MESSAGES'
        ? 'message-selected'
      : 'capability-row-selected';
    console.info(`[joat trace] trace=${traceId} stage=${stage} index=${this.selectedIndex}`);
    this.link.send({
      kind: 'input',
      event: {
        containerName: readingMessage ? 'capmessage' : 'capdata',
        selectedIndex: this.selectedIndex,
        selectedName: this.pendingTitle,
        traceId,
        requestId: this.pendingRequestId,
      },
    });
    // A conversation, compose, or transcript tap has left the currently
    // rendered frame visible while the Mac loads the replacement. Do not
    // leave that frame permanently locked if the production bridge drops the
    // response: reuse the same truthful Messages retry state as an initial
    // menu request and an explicit page turn.
    if (this.pendingTitle === 'MESSAGES' && this.pendingRequestId) {
      this.armResponseTimeout('MESSAGES', this.pendingRequestId);
    }
    return true;
  }

  private select(index: number): boolean {
    if (this.view === 'settings') {
      const row = POLICY_ROWS[index];
      if (row) {
        this.settings.setResumePolicy(row.policy);
        this.link.send({ kind: 'policy', resume: row.policy });
        void this.close();
      }
      return true;
    }

    const picked = CAPABILITIES[index];
    if (picked) {
      if (picked === 'Repeat') {
        // Keep the established Mac contract: repeat only the last completed
        // stored reply. It never re-runs a prompt, resumes audio, or creates
        // a capability-data page.
        void this.repeatLastReply();
        return true;
      }
      if (picked === 'Reconnect') {
        // Force the real authenticated WebSocket redial. MacLink owns the
        // socket replacement, auth, failover/backoff, and Chrome's truthful
        // connecting/reconnecting/connected HUD state.
        void this.reconnectToMac();
        return true;
      }
      if (picked === 'Settings') {
        // This is the persisted listening-resume settings surface, not an
        // imitation static page. Its existing left-double return stays Home.
        void this.openSettings();
        return true;
      }
      // The data lives on the Mac; ask it to draw the view. The Mac answers
      // with hud.list / hud.text, which lands in the 'data' state where
      // double-tap still means back.
      void this.openCapability(picked, index);
    }
    return true;
  }

  private label(row: (typeof POLICY_ROWS)[number]): string {
    const active = row.policy === this.settings.resumePolicy;
    return `${active ? '*' : ' '} ${row.label}`;
  }

  private async repeatLastReply(): Promise<void> {
    // Match the G2 contextual-menu wire shape exactly so the Mac's existing
    // CapabilityRouter._repeat() owns the response and assistant overlay.
    this.link.send({
      kind: 'input',
      event: { containerName: 'sysmenu', selectedIndex: 1 },
    });
    await this.close();
  }

  private async reconnectToMac(): Promise<void> {
    this.link.connect();
    await this.close();
  }

  private async openCapability(
    picked: Exclude<(typeof CAPABILITIES)[number], 'Repeat' | 'Reconnect' | 'Settings'>,
    index: number,
    planningAnchorUnixMs?: number,
  ): Promise<void> {
    if (picked === 'Planning') this.planningDrillDown = false;
    this.clearResponseTimeout();
    this.messagesRetry = false;
    const transition = this.beginTransition();
    const title = picked.toUpperCase();
    const requestId = `hud-request-${this.randomId()}`;
    const shown = await this.hud.showList([`Loading recent ${picked.toLowerCase()}…`], {
      title,
      hint: 'left x2: back',
    });
    if (!this.isCurrentTransition(transition)) return;
    if (!shown) {
      await this.close();
      return;
    }
    this.view = 'data';
    this.pendingTitle = title;
    this.pendingRequestId = requestId;
    this.dataItemCount = 0;
    this.dataRowsSelectable = false;
    this.dataSelectionPending = false;
    this.messagesScreen = undefined;
    this.dataHint = undefined;
    this.dataPagePending = false;
    this.selectedIndex = 0;
    this.dataSurface = undefined;
    this.dataReturn = undefined;
    this.dataGrandparent = undefined;
    const event: InputEventPayload = {
      containerName: 'capmenu',
      eventType: 0,
      selectedIndex: index,
      selectedName: picked,
      requestId,
    };
    if (picked === 'Planning' && isPlanningAnchor(planningAnchorUnixMs)) {
      event.planningAnchorUnixMs = planningAnchorUnixMs;
    }
    this.link.send({
      kind: 'input',
      event,
    });
    if (picked === 'Planning' || picked === 'Messages') {
      this.armResponseTimeout(title, requestId);
    }
  }

  private armResponseTimeout(title: string, requestId: string): void {
    this.responseTimeout = setTimeout(() => {
      void this.showResponseTimeout(title, requestId);
    }, CAPABILITY_RESPONSE_TIMEOUT_MS);
  }

  private clearResponseTimeout(): void {
    if (this.responseTimeout !== undefined) clearTimeout(this.responseTimeout);
    this.responseTimeout = undefined;
  }

  /** A later matching native response still replaces this retry state. */
  private async showResponseTimeout(title: string, requestId: string): Promise<void> {
    this.responseTimeout = undefined;
    if (
      this.view !== 'data'
      || this.pendingTitle !== title
      || this.pendingRequestId !== requestId
    ) return;

    const isMessagesRetry = title === 'MESSAGES';
    this.dataRowsSelectable = false;
    this.dataItemCount = 0;
    const shown = await this.hud.showList(isMessagesRetry
      ? ['Mac did not answer. Tap to retry Messages.']
      : [
        'No response from the Mac yet.',
        'Return to Overview, then open Planning to retry.',
      ], {
      title: `${title} · RETRY`,
      hint: isMessagesRetry ? 'tap: retry · left x2: back' : 'left x2: back',
      selectable: isMessagesRetry,
    });
    if (
      this.view !== 'data'
      || this.pendingTitle !== title
      || this.pendingRequestId !== requestId
    ) return;
    if (!shown) {
      await this.close();
      return;
    }
    this.messagesRetry = isMessagesRetry;
    this.dataRowsSelectable = isMessagesRetry;
    this.dataItemCount = isMessagesRetry ? 1 : 0;
    this.messagesScreen = isMessagesRetry ? 'retry' : undefined;
    this.dataHint = isMessagesRetry ? 'tap: retry · left x2: back' : undefined;
    this.dataPagePending = false;
    this.selectedIndex = 0;
  }

  private randomId(): string {
    return globalThis.crypto?.randomUUID?.()
      ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  }

  /**
   * The text and hint shown by the Mac are the wire contract for paging: the
   * Hub must never fabricate an "older" action on a complete thread. Keeping
   * the cursor and private chat identity Mac-side also prevents G2 input from
   * choosing a different recipient.
   */
  private canRequestMessagePage(eventType: number): boolean {
    if (this.dataPagePending || !this.pendingRequestId) return false;
    const isReader = this.messagesScreen === 'message_detail';
    if (this.messagesScreen !== 'conversation' && !isReader) return false;
    if (isReader) {
      if (eventType === OsEventTypeList.SCROLL_TOP_EVENT) {
        return this.dataHint?.includes('up previous') === true;
      }
      if (eventType === OsEventTypeList.SCROLL_BOTTOM_EVENT) {
        return this.dataHint?.includes('down more') === true;
      }
      return false;
    }
    const atTop = this.selectedIndex === 0;
    const atBottom = this.selectedIndex === Math.max(0, this.dataItemCount - 1);
    if (eventType === OsEventTypeList.SCROLL_TOP_EVENT) {
      return atTop && this.dataHint?.includes('up newer') === true;
    }
    if (eventType === OsEventTypeList.SCROLL_BOTTOM_EVENT) {
      return atBottom && this.dataHint?.includes('down older') === true;
    }
    return false;
  }

  private requestMessagePage(eventType: number): void {
    if (!this.pendingRequestId) return;
    this.dataPagePending = true;
    const traceId = `hud-${this.randomId()}`;
    console.info(`[joat trace] trace=${traceId} stage=message-page-requested event=${eventType}`);
    this.link.send({
      kind: 'input',
      event: {
        containerName: 'cappage',
        selectedName: 'MESSAGES',
        eventType,
        traceId,
        requestId: this.pendingRequestId,
      },
    });
    // A dropped bridge response must not leave the wearer on a dead-end
    // conversation card. The existing timeout replaces it with the honest
    // Messages retry action; a matching page frame clears this timer.
    this.armResponseTimeout('MESSAGES', this.pendingRequestId);
  }

  private beginTransition(): number {
    this.clearProductivityReturn();
    this.view = 'transition';
    this.transitionId += 1;
    return this.transitionId;
  }

  private detailReturnSurface(nextTitle: string): DataSurfaceSnapshot | undefined {
    if (!this.pendingTitle || !this.dataSurface) return undefined;
    const current = this.dataSurface.title.trim().toUpperCase();
    const next = nextTitle.trim().toUpperCase();
    if (current === next || next === this.pendingTitle) return undefined;
    if (!next.startsWith(`${this.pendingTitle} ·`)) return undefined;
    return {
      ...this.dataSurface,
      items: [...this.dataSurface.items],
      planningGrid: this.dataSurface.planningGrid ? [...this.dataSurface.planningGrid] : undefined,
      selectedIndex: this.selectedIndex,
    };
  }

  private async returnToDataSurface(): Promise<void> {
    const parent = this.dataReturn;
    if (!parent) return;
    this.dataReturn = this.dataGrandparent;
    this.dataGrandparent = undefined;
    this.clearResponseTimeout();
    const transition = this.beginTransition();
    const shown = await this.hud.showList(parent.items, {
      title: parent.title,
      hint: parent.hint,
      selectable: parent.selectable,
      planningGrid: parent.planningGrid,
    });
    if (!this.isCurrentTransition(transition)) return;
    if (!shown) {
      await this.close();
      return;
    }
    this.view = 'data';
    this.dataItemCount = parent.items.length;
    this.dataRowsSelectable = parent.selectable;
    this.dataSelectionPending = false;
    this.messagesRetry = parent.messagesRetry;
    this.messagesScreen = parent.messagesScreen;
    this.dataHint = parent.hint;
    this.dataPagePending = false;
    this.selectedIndex = parent.selectedIndex;
    this.dataSurface = parent;
    if (this.pendingTitle === 'MESSAGES') {
      const returnedTitle = parent.title.trim().toUpperCase();
      if (returnedTitle === 'MESSAGES') {
        this.link.send({
          kind: 'input',
          event: { containerName: 'capability-lifecycle', selectedName: 'messages-list' },
        });
      } else if (returnedTitle === 'MESSAGES · CONVERSATION') {
        this.link.send({
          kind: 'input',
          event: { containerName: 'capability-lifecycle', selectedName: 'messages-conversation' },
        });
      }
    }
  }

  private async returnToSidebar(): Promise<void> {
    const leavingMessages = this.pendingTitle === 'MESSAGES';
    const activeIndex = this.pendingTitle
      ? Math.max(0, CAPABILITIES.findIndex((item) => item.toUpperCase() === this.pendingTitle))
      : this.view === 'settings' ? CAPABILITIES.indexOf('Settings') : 0;
    this.clearResponseTimeout();
    this.messagesRetry = false;
    this.messagesScreen = undefined;
    this.dataHint = undefined;
    this.dataPagePending = false;
    this.dataItemCount = 0;
    this.dataRowsSelectable = false;
    this.dataSelectionPending = false;
    this.dataSurface = undefined;
    this.dataReturn = undefined;
    this.dataGrandparent = undefined;
    this.pendingTitle = undefined;
    this.pendingRequestId = undefined;
    const transition = this.beginTransition();
    this.selectedIndex = activeIndex;
    if (leavingMessages) {
      this.link.send({
        kind: 'input',
        event: { containerName: 'capability-lifecycle', selectedName: 'messages-home' },
      });
    }
    const shown = await this.hud.showSidebar([...CAPABILITIES], activeIndex);
    if (!this.isCurrentTransition(transition)) return;
    if (shown) this.view = 'caps';
    else await this.close();
  }

  private scheduleProductivityReturn(phase: ProductivityHudModel['phase']): void {
    if (!isTerminalProductivityOutcome(phase) || !this.productivityReturn) return;
    const epoch = ++this.productivityReturnEpoch;
    this.productivityReturnTimer = setTimeout(() => {
      this.productivityReturnTimer = undefined;
      if (
        this.productivityReturnEpoch !== epoch
        || this.view !== 'productivity'
        || this.productivityReturn !== 'Planning'
      ) return;
      void this.openCapability(
        'Planning',
        CAPABILITIES.indexOf('Planning'),
        this.productivityAnchorUnixMs,
      );
    }, PRODUCTIVITY_OUTCOME_RETURN_DELAY_MS);
  }

  /** The native bridge has already verified the Reminder and the Mac has
   * persisted its monitor occurrence. Show that fact as its own HUD state
   * before returning to live Planning. The epoch guard means an immediate
   * wearer return or a newer due frame cancels this local transition. */
  private scheduleReminderMonitoring(saved: ProductivityHudModel): void {
    if (!this.productivityReturn) return;
    const epoch = ++this.productivityReturnEpoch;
    this.productivityReturnTimer = setTimeout(() => {
      this.productivityReturnTimer = undefined;
      if (
        this.productivityReturnEpoch !== epoch
        || this.view !== 'productivity'
        || this.productivityReturn !== 'Planning'
      ) return;
      void this.presentProductivity({
        domain: 'reminders',
        phase: 'monitoring',
        title: saved.title,
        primary: 'Monitoring armed',
        secondary: 'Native alarm is scheduled. JOAT will check at due time.',
        hint: 'Planning in 5s · left x2 now',
        planningAnchorUnixMs: saved.planningAnchorUnixMs,
      });
    }, PRODUCTIVITY_OUTCOME_RETURN_DELAY_MS);
  }

  private clearProductivityReturn(): void {
    this.productivityReturnEpoch += 1;
    if (this.productivityReturnTimer !== undefined) {
      clearTimeout(this.productivityReturnTimer);
      this.productivityReturnTimer = undefined;
    }
  }

  private isCurrentTransition(transition: number): boolean {
    return this.view === 'transition' && this.transitionId === transition;
  }

  private async close(): Promise<void> {
    const leavingMessages = this.pendingTitle === 'MESSAGES';
    this.clearResponseTimeout();
    this.clearProductivityReturn();
    this.transitionId += 1;
    this.view = 'closed';
    this.selectedIndex = 0;
    this.pendingTitle = undefined;
    this.pendingRequestId = undefined;
    this.productivityReturn = undefined;
    this.productivityAnchorUnixMs = undefined;
    this.planningDrillDown = false;
    this.dataItemCount = 0;
    this.dataRowsSelectable = false;
    this.dataSelectionPending = false;
    this.messagesRetry = false;
    this.messagesScreen = undefined;
    this.dataHint = undefined;
    this.dataPagePending = false;
    this.dataSurface = undefined;
    this.dataReturn = undefined;
    this.dataGrandparent = undefined;
    if (leavingMessages) {
      this.link.send({
        kind: 'input',
        event: {
          containerName: 'capability-lifecycle',
          selectedName: 'messages-home',
        },
      });
    }
    // Restore a LIVE home, not a souvenir of the menu — a stale status line
    // over a blank page reads as a dead assistant (observed on hardware).
    await this.hud.showDefault();
    await this.hud.setStatus(this.micIsOpen() ? 'JOAT · listening' : 'JOAT · mic off');
    // HudPresenter restores the buffered dashboard atomically. Asking the Mac
    // to repaint here used to overwrite a newer card with an older last_reply.
  }
}
