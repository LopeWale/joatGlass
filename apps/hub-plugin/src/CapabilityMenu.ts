import { OsEventTypeList, type EvenHubEvent } from '@evenrealities/even_hub_sdk';
import type { HudPresenter } from './HudPresenter';
import type { Settings } from './Settings';
import type { MacLink } from './MacLink';
import { resolveMessagesViewState } from './MessagesViewState';
import type {
  InputEventPayload,
  PlanningGridCell,
  ProductivityHudModel,
  ResumePolicy,
} from './types';

type ListEvent = NonNullable<EvenHubEvent['listEvent']>;

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
  hint: 'tap apply · left x2 overview',
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
  private messagesRetry = false;
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
      ? 'tap retry · left x2 overview'
      : hint;

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
    this.selectedIndex = 0;
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
  async presentProductivity(productivity: ProductivityHudModel): Promise<boolean> {
    this.clearResponseTimeout();
    const transition = this.beginTransition();
    this.dataRowsSelectable = false;
    this.dataItemCount = 0;
    const shown = await this.hud.showProductivity(productivity);
    if (!this.isCurrentTransition(transition)) return true;
    if (!shown) {
      await this.close();
      return true;
    }
    this.view = 'productivity';
    this.productivityReturn = 'Planning';
    this.productivityAnchorUnixMs = isPlanningAnchor(productivity.planningAnchorUnixMs)
      ? productivity.planningAnchorUnixMs
      : undefined;
    this.pendingTitle = this.productivityReturn.toUpperCase();
    this.pendingRequestId = undefined;
    this.selectedIndex = 0;
    this.scheduleProductivityReturn(productivity.phase);
    return true;
  }

  /** Left double-tap. Closed: open the capability menu. Anywhere else: BACK
   *  home — one gesture, one meaning, matching the hint line. */
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
      if ((list.eventType ?? 0) !== OsEventTypeList.CLICK_EVENT) return true;
      const maximum = this.messagesRetry ? 0 : Math.max(0, this.dataItemCount - 1);
      this.selectedIndex = Math.max(
        0,
        Math.min(maximum, list.currentSelectItemIndex ?? this.selectedIndex),
      );
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
    const stage = this.pendingTitle === 'MESSAGES'
      ? 'message-selected'
      : 'capability-row-selected';
    console.info(`[joat trace] trace=${traceId} stage=${stage} index=${this.selectedIndex}`);
    this.link.send({
      kind: 'input',
      event: {
        containerName: 'capdata',
        selectedIndex: this.selectedIndex,
        selectedName: this.pendingTitle,
        traceId,
        requestId: this.pendingRequestId,
      },
    });
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
      hint: 'left x2 overview',
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
    this.selectedIndex = 0;
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
      hint: isMessagesRetry ? 'tap retry · left x2 overview' : 'left x2 overview',
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
    this.selectedIndex = 0;
  }

  private randomId(): string {
    return globalThis.crypto?.randomUUID?.()
      ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
  }

  private beginTransition(): number {
    this.clearProductivityReturn();
    this.view = 'transition';
    this.transitionId += 1;
    return this.transitionId;
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
