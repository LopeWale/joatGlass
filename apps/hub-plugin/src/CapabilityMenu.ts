import { OsEventTypeList, type EvenHubEvent } from '@evenrealities/even_hub_sdk';
import type { HudPresenter } from './HudPresenter';
import type { Settings } from './Settings';
import type { MacLink } from './MacLink';
import {
  PANEL_EXPANDED_HINT,
  POMODORO_EXPANDED_HINT,
  parseMessagesConversationRow,
  parsePlanningMonthTitle,
  usesQuickNotesReaderLayout,
  usesMessagesPreparedReaderLayout,
  quickNotesProsePages,
} from './HudCanvasRenderer';
import { resolveMessagesViewState, type MessagesScreen } from './MessagesViewState';
import { PANEL_TAB_TITLES, panelTabForTitle } from './PanelRail';

/**
 * Lists the G2 firmware draws itself (`ListContainerProperty`): plain rows
 * the wearer scrolls and taps. The firmware moves the highlight at zero BLE
 * cost and the page is one `rebuildPageContainer` (~165 ms) instead of four
 * image tiles at 0.2–2 s each, which is the trade Even's `glasses-ui` skill
 * recommends for anything that must feel instant. The price is the firmware
 * font, no tab rail, and four visible rows.
 *
 * Only pages whose cursor the firmware can own belong here. The Planning
 * grid, prose readers (QuickNotes notes, prepared Messages results), the
 * QuickNotes archive pager and the Messages conversation keep JOAT's raster
 * treatment: they page or select by a plugin-owned cursor the firmware list
 * cannot express. The NEWS and SEARCH tabs stay raster too: Back from an
 * article restores the row the wearer left, and a firmware list always
 * starts on row one. Titles are the exact Mac-sent page names, so
 * "MESSAGES · CONVERSATION" is not "MESSAGES".
 */
const FIRMWARE_LIST_TITLES: ReadonlySet<string> = new Set([
  'AGENT TERMINAL', 'MESSAGES', 'ACTIVITY',
]);

export function usesFirmwareList(title: string | undefined): boolean {
  return title !== undefined && FIRMWARE_LIST_TITLES.has(title.trim().toUpperCase());
}
import {
  CAPABILITIES,
  VOICE_REQUEST_PREFIX,
  type InputEventPayload,
  type MessageUIMessage,
  type PromptPresentMessage,
  type BrainstormHudModel,
  type PanelContentTab,
  type PlanningGridCell,
  type ProductivityHudModel,
} from './types';

/** What an expanded tab says before the Mac has pushed anything for it.
 * Truthful about the way in: news and search only fill by voice or by
 * the Mac's own poll; the timer starts with a word. */
const PANEL_EMPTY_COPY: Readonly<Record<PanelContentTab, string>> = {
  news: 'No headlines yet — hold to refresh.',
  pomodoro: 'Say “JOAT, start a pomodoro” to begin.',
  search: 'Say “JOAT, look up …” to search.',
};

type ListEvent = NonNullable<EvenHubEvent['listEvent']>;

interface DataSurfaceSnapshot {
  planningNavigation?: { canGoBack: boolean; selectedIndex: number };
  readerPage?: { id: string; index: number; count: number };
  quickNotesPage?: { index: number; count: number };
  title: string;
  contextLabel?: string;
  controlStartIndex?: number;
  planningEntries?: Array<{ time: string; kind: "event" | "reminder"; title: string } | null>;
  items: string[];
  selectable: boolean;
  hint?: string;
  planningGrid?: PlanningGridCell[];
  planningPane?: { dateIndex: number; focused: boolean; itemIndex: number };
  selectedIndex: number;
  messagesRetry: boolean;
  messagesScreen?: MessagesScreen;
  messageProse?: { pages: string[][]; index: number };
  /** An expanded panel tab keeps its rail lit, on the page and on the
   * list Back restores; without this the rail vanished on return. */
  railTab?: PanelContentTab;
}

interface TerminalOpeningBinding {
  requestId: string;
  selectionId: string;
}

interface MessageSelectionBinding {
  readonly requestId: string;
  readonly selectionId: string;
  readonly retiredActionIds: Set<string>;
  action?: { message: Readonly<MessageUIMessage>; readonly isCurrent: () => boolean };
}

const MESSAGE_UI_PREFIX = 'messages-ui-';
const MESSAGE_UI_STATES: readonly string[] = [
  'preparing', 'awaiting_input', 'awaiting_confirmation', 'sending',
  'cancelled', 'sent', 'failed', 'uncertain',
];
const MESSAGE_UI_KEYS = ['kind', 'requestId', 'selectionId', 'actionId', 'state', 'text'] as const;

function messageFinished(state: MessageUIMessage['state']): boolean {
  return state === 'cancelled' || state === 'sent' || state === 'failed' || state === 'uncertain';
}

function parseMessageUI(value: unknown): MessageUIMessage | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return;
  const data = value as Record<string, unknown>;
  if (Object.keys(data).length !== MESSAGE_UI_KEYS.length
    || !MESSAGE_UI_KEYS.every((key) => Object.prototype.hasOwnProperty.call(data, key))
    || data.kind !== 'message.ui'
    || typeof data.requestId !== 'string' || !/^[A-Za-z0-9._:-]{1,128}$/.test(data.requestId)
    || typeof data.selectionId !== 'string' || !/^[A-Za-z0-9._:-]{1,128}$/.test(data.selectionId)
    || typeof data.actionId !== 'string' || !data.actionId.startsWith(MESSAGE_UI_PREFIX)
    || !/^[A-Za-z0-9._:-]{1,96}$/.test(data.actionId)
    || typeof data.state !== 'string' || !MESSAGE_UI_STATES.includes(data.state)
    || typeof data.text !== 'string' || data.text.length > 1000
    || /\0|[\uD800-\uDFFF]/u.test(data.text)) return;
  return {
    kind: 'message.ui', requestId: data.requestId, selectionId: data.selectionId,
    actionId: data.actionId, state: data.state as MessageUIMessage['state'], text: data.text,
  };
}

const PLANNING_ANCHOR_MAX_UNIX_MS = 32_503_680_000_000;
/**
 * The native bridge itself has a bounded call deadline. Give it a small
 * allowance, then replace the local loading frame with an honest retry state
 * instead of leaving a wearer on a spinner indefinitely.
 */
const CAPABILITY_RESPONSE_TIMEOUT_MS = 12_000;
/** Failure pages stay readable before return; success receipts remain over
 * the promptly refreshed agenda. Monitoring follows the saved receipt. */
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
 * The capability views behind the wearer's ONE menu.
 *
 * That menu is the firmware contextual menu (tap, then long-press): the
 * glasses draw it from the CAPABILITIES rows in types.ts, zero image tiles.
 * This class no longer draws a menu of its own — it opens what the wearer
 * picked and owns the pages beneath it.
 *
 * View machine, and what double-click means in each state:
 *   home ──firmware menu──▶ data view, settings, or direct action
 *   settings/data ──double-click──▶ home  (double-click is BACK, always)
 */

const SETTINGS_SURFACE = {
  title: 'PHONE CONTROLS',
  // GesturePolicy owns the source-independent Back meaning.
  hint: 'left x2: back',
} as const;

export function isActionLifecycleTitle(title?: string): boolean {
  return title?.trim().toUpperCase().startsWith('ACTION ·') ?? false;
}

export class CapabilityMenu {
  private view: 'closed' | 'transition' | 'settings' | 'data' | 'productivity' = 'closed';
  private selectedIndex = 0;
  private dataItemCount = 0;
  private dataRowsSelectable = false;
  private dataSelectionPending = false;
  private rowSelection?: {
    surface: DataSurfaceSnapshot;
    requestId: string | undefined;
    transition: number;
    desired: number;
    painted: number;
    rows: number[];
    tap: boolean;
    boundary?: number;
  };
  private dashboardSelectionPaint?: Promise<boolean>;
  private dashboardFailedPaint?: object;
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
  private noteReaderTraceId?: string;
  private noteProse?: { pages: string[][]; index: number };
  private notePageDirection = 1;
  private noteProsePainting = false;
  private messageProsePainting = false;
  private messagePageDirection = 1;
  /** Reader identities belong to one capability visit, not the recipient/action.
   * Keep tombstones through local Back/Home so delayed pages cannot revive it.
   * At the cap, refuse new readers for this visit rather than forget a tombstone. */
  private reviewVisit?: { requestId: string; activeId?: string; retired: Set<string> };

  /** Messages has two real read levels (conversation then full message), so
   * retain those exact Mac-authored frames for contextual Back. */
  private dataSurface?: DataSurfaceSnapshot;
  private dataReturn?: DataSurfaceSnapshot;
  private notificationRefreshSelection?: { requestId: string; index: number };
  private dataGrandparent?: DataSurfaceSnapshot;
  private pendingTitle?: string;
  private pendingRequestId?: string;
  /** One capability visit and its particular lane choice. Keep this owner
   * across connecting/ready frames after the picker gives up input. */
  private terminalOpening?: TerminalOpeningBinding;
  private terminalPaintRevision = 0;
  private terminalPreludeRevision = 0;
  private messageSelection?: MessageSelectionBinding;
  private messageLifecycleBinding?: TerminalOpeningBinding;
  private messagePaintRevision = 0;
  private responseTimeout?: ReturnType<typeof setTimeout>;
  private productivityReturn?: 'Planning';
  private productivityAnchorUnixMs?: number;
  private productivityReturnTimer?: ReturnType<typeof setTimeout>;
  private productivityReturnEpoch = 0;
  private productivitySelection?: { contextLabel?: string; item: string; index: number };
  private planningReceiptRefresh?: {
    requestId: string; monitoring?: ProductivityHudModel;
    selection?: { contextLabel?: string; item: string; index: number };
  };
  /** A Planning row may open a day or item detail. Back returns to the live
   * schedule rather than throwing the wearer all the way to the overview. */
  private planningDrillDown = false;
  private planningBackPending = false;
  /** The split Planning dashboard: hovered date, and whether the right pane
   * owns input. Plugin-local — a hover never costs a Mac round trip. */
  private dashboard?: {
    grid: PlanningGridCell[];
    dateIndex: number;
    paneFocused: boolean;
    paneIndex: number;
  };
  private transitionId = 0;
  private brainstormVisit = 0;
  private brainstormOwnsSurface = false;
  private contextualMenuOpen = false;
  /** Retire the gesture controller before any replacement paint can wait. */
  onSurfaceDeparture: () => void = () => {};
  /** Live mic state, injected so the restored status line tells the truth. */
  micIsOpen: () => boolean = () => false;

  constructor(
    private readonly hud: HudPresenter,
    _settings: Settings,
    private readonly link: MacLink,
  ) {}

  isOpen(): boolean {
    return this.view !== 'closed';
  }

  isAwaitingResponse(): boolean {
    return this.pendingRequestId !== undefined;
  }

  /** Capture at ingress: queued frames lose authority on local departure. */
  captureBrainstormVisit(): () => boolean {
    const visit = this.brainstormVisit;
    return () => visit === this.brainstormVisit;
  }

  /** Only an explicit local open or Mac start/replay rebuild grants focus. */
  claimBrainstormVisit(isCurrent: () => boolean): boolean {
    if (!isCurrent()) return false;
    this.brainstormOwnsSurface = true;
    return true;
  }

  mayPresentBrainstorm(isCurrent: () => boolean): boolean {
    return isCurrent() && this.brainstormOwnsSurface && !this.contextualMenuOpen;
  }

  /** Mac-authored replacement frames retire focus without echoing a request.
   * Do not increment the local visit: an explicit rebuild already queued
   * after that replacement must still be able to open Brainstorm. */
  retireBrainstormSurface(): void {
    this.brainstormOwnsSurface = false;
    this.onSurfaceDeparture();
  }

  setContextualMenuOpen(open: boolean): void {
    if (open === this.contextualMenuOpen) return;
    this.contextualMenuOpen = open;
    this.brainstormVisit += 1;
    this.sendSurfaceLifecycle(open ? 'surface-context-menu-open' : 'surface-context-menu-closed');
  }

  private departSurface(surface: 'home' | 'settings' | 'news' | 'search' | 'pomodoro' | 'feature'): void {
    this.brainstormVisit += 1;
    this.retireBrainstormSurface();
    this.sendSurfaceLifecycle(`surface-${surface}`);
  }

  private sendSurfaceLifecycle(selectedName: string): void {
    this.link.send({ kind: 'input', event: { containerName: 'capability-lifecycle', selectedName } });
  }

  /** Synchronous ingress reserves a typed action only for the row we sent. */
  observeMessageUI(value: unknown, isCurrent: () => boolean): boolean {
    const message = parseMessageUI(value);
    const selection = this.messageSelection;
    if (!message || !selection || !this.messageSelectionIsCurrent(selection)
      || message.requestId !== selection.requestId || message.selectionId !== selection.selectionId) return false;
    try { if (!isCurrent()) return false; } catch { return false; }
    const action = selection.action;
    if (message.state === 'preparing') {
      if (selection.retiredActionIds.has(message.actionId)
        || selection.retiredActionIds.size >= 256
        || (action && !messageFinished(action.message.state))) return false;
      selection.action = { message: Object.freeze(message), isCurrent };
    } else {
      if (!action || action.message.actionId !== message.actionId
        || messageFinished(action.message.state)) return false;
      try { if (!action.isCurrent()) return false; } catch { return false; }
      action.message = Object.freeze(message);
    }
    if (messageFinished(message.state)) {
      selection.retiredActionIds.add(message.actionId);
      this.retireReviewReader();
    }
    this.messagePaintRevision += 1;
    this.clearResponseTimeout();
    return true;
  }

  isMessagePromptCurrent(message: Readonly<PromptPresentMessage>): boolean {
    if (!message.ownerId.startsWith(MESSAGE_UI_PREFIX)) return true;
    return message.ownerKind === 'messages' && this.isMessageActionCurrent(message.ownerId);
  }

  isMessageActionCurrent(actionId: string): boolean {
    const selection = this.messageSelection;
    const action = selection?.action;
    if (!selection || !action
      || !this.messageSelectionIsCurrent(selection)
      || action.message.actionId !== actionId || messageFinished(action.message.state)) return false;
    try { return action.isCurrent(); } catch { return false; }
  }

  cancelMessageAction(actionId?: string): boolean {
    const selection = this.messageSelection;
    const action = selection?.action;
    if (!selection || !action || !this.messageSelectionIsCurrent(selection)
      || (actionId !== undefined && action.message.actionId !== actionId)
      || messageFinished(action.message.state)) return false;
    try { if (!action.isCurrent()) return false; } catch { return false; }
    this.retireReviewReader();
    selection.retiredActionIds.add(action.message.actionId);
    selection.action = undefined;
    this.messagePaintRevision += 1;
    this.clearResponseTimeout();
    this.dataSelectionPending = false;
    this.sendMessageCancel(selection, action.message.actionId);
    return true;
  }

  /** Disconnect retires locally; explicit departure also cancels its draft. */
  retireMessageUI(notifyMac = false): void {
    this.retireReviewReader();
    const selection = this.messageSelection;
    this.messageSelection = undefined;
    this.messagePaintRevision += 1;
    if (selection) this.clearResponseTimeout();
    if (!notifyMac) this.messageLifecycleBinding = undefined;
    if (notifyMac && selection?.action && !messageFinished(selection.action.message.state)) {
      this.sendMessageCancel(selection, selection.action.message.actionId);
    }
  }

  /** Consume a completed voice review without painting underneath its consent gate. */
  stageLegacyReviewReturn(confirmationId: string): void {
    const reader = this.dataSurface?.readerPage;
    const parent = this.dataReturn;
    if (this.view !== 'data' || this.messagesScreen !== 'review'
      || this.pendingTitle !== 'MESSAGES' || !this.dataSelectionPending
      || !reader || reader.index !== reader.count - 1 || !parent
      || this.reviewVisit?.requestId !== this.pendingRequestId
      || this.reviewVisit?.activeId !== reader.id) return;
    const staged = this.hud.stageConfirmationReturnList(confirmationId,
      parent.messageProse ? this.noteDisplayItems(parent.items, parent.messageProse) : parent.items, {
        title: parent.title, initialSelectedIndex: parent.selectedIndex,
        contextLabel: parent.contextLabel, controlStartIndex: parent.controlStartIndex,
        hint: parent.messageProse ? this.messageDisplayHint(parent.hint, parent.messageProse) : parent.hint,
        selectable: parent.selectable && (!parent.messageProse
          || parent.messageProse.index === parent.messageProse.pages.length - 1),
      });
    if (!staged) return;
    this.retireReviewReader();
    this.dataReturn = this.dataGrandparent;
    this.dataGrandparent = undefined;
    this.clearResponseTimeout();
    this.dataItemCount = parent.items.length;
    this.dataRowsSelectable = parent.selectable;
    this.dataSelectionPending = false;
    this.messagesRetry = parent.messagesRetry;
    this.messagesScreen = parent.messagesScreen;
    this.dataHint = parent.hint;
    this.dataPagePending = false;
    this.selectedIndex = parent.selectedIndex;
    this.dataSurface = parent;
  }

  private retireReviewReader(): void {
    const visit = this.reviewVisit;
    if (!visit?.activeId) return;
    visit.retired.add(visit.activeId);
    visit.activeId = undefined;
  }

  private sendMessageCancel(selection: MessageSelectionBinding, actionId: string): void {
    this.link.send({ kind: 'input', event: {
      containerName: 'message-cancel', selectedName: actionId,
      requestId: selection.requestId, traceId: selection.selectionId,
    } });
  }

  private messagesLifecycle(selectedName: string, binding = this.messageLifecycleBinding): void {
    this.link.send({ kind: 'input', event: {
      containerName: 'capability-lifecycle', selectedName,
      ...(binding ? { requestId: binding.requestId, traceId: binding.selectionId } : {}),
    } });
  }

  private leaveMessages(): void {
    const leaving = this.pendingTitle === 'MESSAGES';
    const binding = this.messageLifecycleBinding;
    this.retireMessageUI(true);
    this.messageLifecycleBinding = undefined;
    if (leaving) this.messagesLifecycle('messages-home', binding);
  }

  private messageSelectionIsCurrent(selection: MessageSelectionBinding): boolean {
    return this.messageSelection === selection && this.view !== 'closed'
      && this.pendingTitle === 'MESSAGES' && this.pendingRequestId === selection.requestId;
  }

  async presentMessageUI(message: MessageUIMessage, isCurrent: () => boolean): Promise<boolean> {
    const selection = this.messageSelection;
    const action = selection?.action;
    if (!selection || !action || !this.messageSelectionIsCurrent(selection)
      || !MESSAGE_UI_KEYS.every((key) => action.message[key] === message[key])) return false;
    const accepted = action.message;
    const revision = this.messagePaintRevision;
    const current = () => {
      try {
        return this.messageSelectionIsCurrent(selection) && selection.action === action
          && action.message === accepted && this.messagePaintRevision === revision
          && action.isCurrent() && isCurrent();
      } catch { return false; }
    };
    if (!current()) return false;
    // Questions and confirmation own intermediate states. Only final receipts
    // replace the underlying feature, and Back retains its actual Mac rows.
    if (!messageFinished(accepted.state)) return true;
    let parent = this.dataSurface;
    let grandparent = this.dataReturn;
    if (this.messagesScreen === 'message_detail' || this.messagesScreen === 'review' || this.messagesScreen === 'compose'
      || this.messagesScreen === 'read_only') {
      parent = this.dataReturn;
      grandparent = this.dataGrandparent;
    }
    if (parent === this.dataSurface && parent) parent = { ...parent, selectedIndex: this.selectedIndex };
    const rows = [`1/1 · ${accepted.state === 'sent' ? 'Submitted' : accepted.state}`, accepted.text];
    const messageProse = { pages: quickNotesProsePages(rows), index: 0 };
    const title = `MESSAGES · ${accepted.state.toUpperCase()}`;
    const hint = 'left x2: back';
    const transition = this.beginTransition();
    const stillCurrent = () => current() && this.transitionId === transition;
    const shown = await this.hud.showList(this.noteDisplayItems(rows, messageProse), {
      title, hint: this.messageDisplayHint(hint, messageProse), selectable: false,
    }, stillCurrent);
    if (!stillCurrent()) return false;
    this.view = 'data';
    if (!shown) return false;
    this.dataReturn = parent;
    this.dataGrandparent = grandparent;
    this.selectedIndex = 0;
    this.dataRowsSelectable = false;
    this.dataItemCount = rows.length;
    this.dataSelectionPending = false;
    this.dataPagePending = false;
    this.messagesRetry = false;
    this.messagesScreen = 'read_only';
    this.dataHint = hint;
    this.dataSurface = { title, items: rows, hint, messageProse, selectable: false, selectedIndex: 0, messagesRetry: false, messagesScreen: 'read_only' };
    return true;
  }

  /** Older Macs can answer an initial Terminal request without opening IDs.
   * Once a roster or bound session owns the screen, an untagged update has
   * no authority to replace it. Ordinary handoffs remain available at Home. */
  mayPresentUnboundTerminal(): boolean {
    if (this.view === 'closed') return this.terminalOpening === undefined;
    return this.view === 'data'
      && this.pendingTitle === 'AGENT TERMINAL'
      && this.pendingRequestId !== undefined
      && this.dataItemCount === 0
      && this.terminalOpening?.requestId === this.pendingRequestId
      && this.terminalOpening.selectionId === this.pendingRequestId;
  }

  /** Capture at socket ingress, then check once before a queued voice list
   * adopts its visit. Only local navigation or disconnect retires this
   * guard; painting an earlier server frame must not reject a later one. */
  captureTerminalPreludeGuard(): () => boolean {
    const revision = this.terminalPreludeRevision;
    return () => this.terminalPreludeRevision === revision;
  }

  /** The request is reused by picker rows; only their trace IDs distinguish
   * two selections within the same visit. Never infer one from a frame. */
  async presentTerminalOpening(
    model: BrainstormHudModel,
    binding: TerminalOpeningBinding,
    isCurrent: () => boolean,
  ): Promise<boolean> {
    const opening = this.terminalOpening;
    if (
      !opening || model.screen !== 'agent_terminal'
      || !binding.requestId || !binding.selectionId
      || opening.requestId !== binding.requestId
      || opening.selectionId !== binding.selectionId
      || !isCurrent()
    ) return false;

    this.retireBrainstormSurface();
    const revision = ++this.terminalPaintRevision;
    const stillCurrent = () => this.terminalOpening === opening
      && this.terminalPaintRevision === revision && isCurrent();
    const shown = await this.hud.showBrainstorm(model, undefined, stillCurrent);
    if (!shown || !stillCurrent()) return false;
    // Commit input ownership only after the exact frame paints. A failed
    // paint leaves the picker and its bounded response timeout available.
    // Releasing the menu must retain this opening for its later ready frame.
    this.releaseMenuToLiveSession();
    return true;
  }

  /** Local departure wins synchronously, including an in-flight paint. A
   * disconnected link must retire locally without queuing a lifecycle event. */
  retireTerminalOpening(notifyMac = true): void {
    if (!notifyMac) this.terminalPreludeRevision += 1;
    const opening = this.terminalOpening;
    this.terminalOpening = undefined;
    if (!opening || !notifyMac) return;
    this.link.send({
      kind: 'input',
      event: {
        containerName: 'capability-lifecycle', selectedName: 'terminal-home',
        requestId: opening.requestId, traceId: opening.selectionId,
      },
    });
  }

  async beginLiveSession(session: 'brainstorm'): Promise<void> {
    void session;
    // The caller immediately paints its live frame. Painting Overview here
    // doubled tile traffic and exposed unrelated quadrants between updates.
    this.retireTerminalOpening();
    this.releaseMenuToLiveSession();
  }

  /** An external live surface (Navigation) is taking the lenses. Clear menu
   * input ownership, timers, and any pending capability request WITHOUT
   * painting: the session's own frame is the replacement, and an extra
   * Overview paint would race it on the serialized BLE lane. Also prevents a
   * response timeout armed for the menu request from later repainting a
   * retry list over the live session's frame. */
  releaseToLiveSession(): void {
    this.retireBrainstormSurface();
    this.retireTerminalOpening();
    this.releaseMenuToLiveSession();
  }

  private releaseMenuToLiveSession(): void {
    this.retireQuickNotesReader('home');
    this.leaveMessages();
    this.clearResponseTimeout();
    this.dashboard = undefined;
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
  }

  /** Compatibility for cached firmware Settings links. Configuration now
   * belongs to the phone; this surface offers only directions and Back. */
  async openSettings(): Promise<void> {
    this.departSurface('settings');
    this.leaveMessages();
    this.retireQuickNotesReader('home');
    this.pendingTitle = undefined;
    this.pendingRequestId = undefined;
    const transition = this.beginTransition();
    this.selectedIndex = 0;
    const shown = await this.hud.showList(
      ['Manage listening and Mac connections', 'in JOAT’s phone controls in Even Hub.'],
      { ...SETTINGS_SURFACE, selectable: false },
    );
    if (!this.isCurrentTransition(transition)) return;
    if (shown) this.view = 'settings';
    else await this.close();
  }

  /**
   * Contextual-menu escape. This intentionally uses the same Mac-owned
   * Planning request as double-click Back: it cannot mutate Calendar or
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
    contextLabel?: string,
    controlStartIndex?: number,
    planningEntries?: Array<{ time: string; kind: "event" | "reminder"; title: string } | null>,
    quickNotesPage?: { index: number; count: number },
    readerPage?: { id: string; index: number; count: number },
    planningNavigation?: { canGoBack: boolean; selectedIndex: number },
  ): Promise<boolean> {
    // Read-only Planning details answer a row tap. Once Back retires that
    // tap, a delayed detail cannot replace its restored agenda.
    if (title?.trim().toUpperCase() === 'PLANNING · EVENT'
      && requestId === this.pendingRequestId && !this.dataSelectionPending) return true;
    const isReview = title?.trim().toUpperCase() === 'MESSAGES · REVIEW';
    const reviewVisit = this.reviewVisit;
    if (isReview && reviewVisit && reviewVisit.requestId === requestId
      && (!readerPage || reviewVisit.retired.has(readerPage.id)
        || (reviewVisit.retired.size >= 256 && readerPage.id !== reviewVisit.activeId))) return true;
    // A reader response is always answering a row tap in this visit. Check
    // before voice-generation adoption so a late page cannot reopen it.
    if (usesQuickNotesReaderLayout(title ?? '', items)
      && (!this.noteReaderTraceId || requestId !== this.pendingRequestId)) return false;
    const conversationalTerminal = title?.trim().toUpperCase() === 'AGENT TERMINAL'
      && !selectable && requestId?.startsWith(VOICE_REQUEST_PREFIX)
      && requestId !== this.pendingRequestId;
    // A voice-prefixed generation is the Mac acting on the wearer's spoken
    // request ("show my messages", "start a quicknote"). Adopt it instead of
    // dropping it as stale — voice must not require a preceding menu tap.
    if (
      title
      && requestId !== undefined
      && requestId.startsWith(VOICE_REQUEST_PREFIX)
      && requestId !== this.pendingRequestId
    ) {
      this.adoptVoiceGeneration(title, requestId);
    }
    if (this.view !== 'data' || !this.pendingTitle || !title) return false;
    if (!this.pendingRequestId || requestId !== this.pendingRequestId) return false;
    const normalized = title.trim().toUpperCase();
    const matches = normalized === this.pendingTitle
      || normalized.startsWith(`${this.pendingTitle} ·`);
    if (!matches) return false;
    if (this.pendingTitle !== 'BRAINSTORM') this.retireBrainstormSurface();
    const messagesState = this.pendingTitle === 'MESSAGES'
      ? resolveMessagesViewState(title, selectable)
      : undefined;
    const effectiveSelectable = messagesState?.selectable ?? selectable;
    const effectiveHint = messagesState?.rowAction === 'retry'
      ? 'tap: retry · left x2: back'
      : hint;

    if (isReview) {
      if (!readerPage || typeof readerPage.id !== 'string' || !readerPage.id.length
        || readerPage.id.length > 128 || !Number.isInteger(readerPage.index)
        || !Number.isInteger(readerPage.count) || readerPage.index < 0
        || readerPage.count <= readerPage.index) return true;
      if (this.reviewVisit?.requestId !== requestId) {
        this.reviewVisit = { requestId, retired: new Set() };
      }
      if (this.reviewVisit.activeId !== readerPage.id) this.retireReviewReader();
      if (this.reviewVisit.retired.size >= 256) return true;
      // Reserve before the first await: Back during painting also retires it.
      this.reviewVisit.activeId = readerPage.id;
    }

    const planningOwner = title?.trim().toUpperCase().startsWith('PLANNING')
      && planningNavigation && typeof planningNavigation.canGoBack === 'boolean'
      && Number.isInteger(planningNavigation.selectedIndex)
      && planningNavigation.selectedIndex >= 0 && planningNavigation.selectedIndex <= 41
      ? { ...planningNavigation } : undefined;
    // Current Planning publishes its own parent and row mapping. Cached peer
    // scope rows must never become actionable against a newer Mac projection.
    if (planningOwner) {
      this.dataReturn = undefined;
      this.dataGrandparent = undefined;
    }
    const parent = planningOwner ? undefined : this.detailReturnSurface(title);
    if (parent) {
      this.dataGrandparent = this.dataReturn;
      this.dataReturn = parent;
    }

    this.clearResponseTimeout();
    this.messagesRetry = messagesState?.rowAction === 'retry';
    // A month with a full grid becomes the split dashboard: grid left, the
    // hovered day's items right. Hover starts on today when the month shows
    // it, else the first in-month day.
    const month = parsePlanningMonthTitle(title);
    if (month && planningGrid?.length === 42 && effectiveSelectable) {
      const todayIso = new Date().toLocaleDateString('en-CA');
      let dateIndex = planningGrid.findIndex(
        (cell) => cell.inMonth && cell.day === todayIso,
      );
      if (dateIndex < 0) {
        dateIndex = Math.max(0, planningGrid.findIndex((cell) => cell.inMonth));
      }
      if (planningOwner) dateIndex = planningOwner.selectedIndex;
      this.dashboard = {
        grid: [...planningGrid], dateIndex, paneFocused: false, paneIndex: 0,
      };
    } else {
      this.dashboard = undefined;
    }
    const expectedRequestId = this.pendingRequestId;
    const receiptRefresh = this.planningReceiptRefresh?.requestId === expectedRequestId
      ? this.planningReceiptRefresh : undefined;
    const transition = this.beginTransition();
    if (conversationalTerminal && requestId) {
      // Bind after beginTransition retires the old view, and before awaiting
      // paint so a local Home can synchronously revoke this new visit.
      this.terminalOpening = { requestId, selectionId: requestId };
    }
    this.dataRowsSelectable = false;
    this.dataItemCount = 0;
    // An opened headline or result is still inside its tab: the rail
    // stays on the reader so the wearer sees where they are.
    const railTab = panelTabForTitle(this.pendingTitle);
    // Decided before any cursor question below: the presenter answers for
    // the page about to be shown, not the (possibly firmware-owned) one that
    // is still on the glass.
    const firmwareList = usesFirmwareList(normalized) && !planningGrid && !readerPage && !quickNotesPage;
    const customSelection = (this.hud.usesCustomListSelection?.({ nativeFirmwareList: firmwareList }) ?? false)
      && !firmwareList;
    const messageRows = messagesState?.screen === 'conversation' && effectiveSelectable
      && customSelection
      ? items.flatMap((item, index) => parseMessagesConversationRow(item).kind !== 'instruction' ? [index] : [])
      : [];
    const refreshSelection = normalized === 'NOTIFICATIONS'
      && this.notificationRefreshSelection?.requestId === requestId
      ? this.notificationRefreshSelection.index : undefined;
    const rowSelection = normalized.startsWith('PLANNING')
      && receiptRefresh?.selection?.contextLabel === contextLabel
      ? receiptRefresh?.selection : undefined;
    const matchedPlanningIndex = rowSelection
      ? items[rowSelection.index] === rowSelection.item ? rowSelection.index
        : items.indexOf(rowSelection.item)
      : -1;
    const planningContentCount = controlStartIndex ?? items.length;
    const restoredPlanningIndex = rowSelection
      ? matchedPlanningIndex >= 0 ? matchedPlanningIndex
        : Math.max(0, Math.min(rowSelection.index, planningContentCount - 1))
      : undefined;
    const preparedReader = readerPage && typeof readerPage.id === 'string'
      && readerPage.id.length > 0 && readerPage.id.length <= 128
      && Number.isInteger(readerPage.index) && Number.isInteger(readerPage.count)
      && readerPage.index >= 0 && readerPage.count > readerPage.index
      ? { ...readerPage } : undefined;
    const archivePage = normalized === 'QUICKNOTES' && quickNotesPage
      && Number.isInteger(quickNotesPage.index) && Number.isInteger(quickNotesPage.count)
      && quickNotesPage.index >= 0 && quickNotesPage.count > quickNotesPage.index
      ? { ...quickNotesPage } : undefined;
    const previousArchivePage = this.dataSurface?.quickNotesPage;
    const archiveSelection = archivePage && previousArchivePage && this.dataPagePending
      && archivePage.index < previousArchivePage.index ? Math.max(0, items.length - 1) : undefined;
    const authoritativePlanningIndex = planningOwner && !this.dashboard
      ? Math.max(0, Math.min(planningOwner.selectedIndex, items.length - 1)) : undefined;
    const initialSelection = archiveSelection ?? restoredPlanningIndex ?? authoritativePlanningIndex ?? (refreshSelection === undefined ? messageRows[0] ?? 0
      : Math.max(0, Math.min(refreshSelection, items.length - 1)));
    if (refreshSelection !== undefined) this.notificationRefreshSelection = undefined;
    const noteProse = usesQuickNotesReaderLayout(title, items) && customSelection
      ? { pages: quickNotesProsePages(items), index: 0 } : undefined;
    if (noteProse && this.notePageDirection < 0) noteProse.index = noteProse.pages.length - 1;
    const messageProse = usesMessagesPreparedReaderLayout(title, items) && customSelection
      ? { pages: quickNotesProsePages(items), index: 0 } : undefined;
    if (messageProse && this.messagePageDirection < 0 && this.dataPagePending
      && this.dataSurface?.title === title) messageProse.index = messageProse.pages.length - 1;
    const shown = await this.hud.showList(messageProse ? this.noteDisplayItems(items, messageProse)
      : noteProse ? this.noteDisplayItems(items, noteProse) : items, {
      title,
      contextLabel,
      controlStartIndex,
      planningEntries,
      retainProductivityReceipt: receiptRefresh !== undefined,
      hint: messageProse ? this.messageDisplayHint(effectiveHint, messageProse)
        : noteProse ? this.noteDisplayHint(effectiveHint, noteProse) : effectiveHint,
      selectable: effectiveSelectable && (!messageProse || messageProse.index === messageProse.pages.length - 1),
      planningGrid,
      planningPane: this.dashboardPane(),
      ...(initialSelection > 0 ? { initialSelectedIndex: initialSelection } : {}),
      ...(railTab ? { railTab } : {}),
      ...(firmwareList ? { nativeFirmwareList: true } : {}),
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
    this.noteProse = noteProse;
    this.notePageDirection = 1;
    this.messagePageDirection = 1;
    this.dataItemCount = items.length;
    this.dataRowsSelectable = effectiveSelectable;
    this.dataSelectionPending = false;
    this.dataSelectionUsesStaleNativeIndex = false;
    this.messagesScreen = messagesState?.screen;
    this.dataHint = effectiveHint;
    this.dataPagePending = false;
    this.selectedIndex = initialSelection;
    this.dataSurface = {
      planningNavigation: planningOwner,
      title,
      quickNotesPage: archivePage,
      readerPage: preparedReader,
      messageProse,
      contextLabel,
      controlStartIndex,
      planningEntries,
      items: [...items],
      selectable: effectiveSelectable,
      hint: effectiveHint,
      planningGrid: planningGrid ? [...planningGrid] : undefined,
      selectedIndex: initialSelection,
      messagesRetry: this.messagesRetry,
      messagesScreen: this.messagesScreen,
      ...(railTab ? { railTab } : {}),
    };
    if (receiptRefresh?.monitoring) {
      this.scheduleMonitoringReceipt(receiptRefresh.monitoring, expectedRequestId);
    }
    if (conversationalTerminal && requestId) this.armResponseTimeout('AGENT TERMINAL', requestId);
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
    this.retireBrainstormSurface();
    this.leaveMessages();
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
    if (this.view !== 'productivity') {
      this.productivitySelection = this.pendingTitle === 'PLANNING' && this.dataSurface
        && this.selectedIndex < this.dataSurface.items.length
        ? { contextLabel: this.dataSurface.contextLabel,
          item: this.dataSurface.items[this.selectedIndex]!, index: this.selectedIndex }
        : undefined;
    }
    this.retireBrainstormSurface();
    this.leaveMessages();
    this.clearResponseTimeout();
    const transition = this.beginTransition();
    this.dataRowsSelectable = false;
    this.dataItemCount = 0;
    let stateReady = false;
    let visibilityArrivedEarly = false;
    let activated = false;
    const afterProductivityVisible = (): void => {
      if (!stateReady) {
        visibilityArrivedEarly = true;
        return;
      }
      if (
        activated
        || this.transitionId !== transition
        || this.view !== 'productivity'
      ) return;
      activated = true;
      onVisible?.();
      if (
        productivity.domain === 'reminders'
        && productivity.phase === 'saved'
        && productivity.monitoringArmed === true
      ) {
        this.scheduleReminderMonitoring(productivity);
      } else {
        this.scheduleProductivityReturn(productivity.phase);
      }
    };
    const shown = await this.hud.showProductivity(
      productivity,
      undefined,
      afterProductivityVisible,
    );
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
    this.selectedIndex = this.productivitySelection?.index ?? 0;
    stateReady = true;
    // A deferred confirmation, failed paint, or newer transition can be
    // handled without a current visual page. Only a retained exact surface is
    // eligible for the due-HUD acknowledgement.
    const visible = this.hud.isProductivityVisible(productivity);
    if (visible || visibilityArrivedEarly) afterProductivityVisible();
    return visible;
  }

  /** Double-click is BACK, and only back. The wearer's menu is the
   * firmware contextual menu (tap, then long-press); this gesture never
   * draws a second one. On the overview there is no level left to leave. */
  async toggle(): Promise<void> {
    // Back retires any queued tap/page intent before awaiting another paint.
    this.rowSelection = undefined;
    if (this.dashboardSelectionPaint) {
      const dashboard = this.dashboard;
      await this.dashboardSelectionPaint;
      if (this.dashboard !== dashboard || this.view !== 'data') return;
    }
    this.terminalPreludeRevision += 1;
    if (this.view === 'closed') {
      if (this.terminalOpening) {
        this.retireTerminalOpening();
        await this.openCapability('Agent Terminal', CAPABILITIES.indexOf('Agent Terminal'));
      }
      return;
    } else if (this.view === 'productivity' && this.productivityReturn) {
      const capability = this.productivityReturn;
      await this.openCapability(
        capability,
        CAPABILITIES.indexOf(capability),
        this.productivityAnchorUnixMs,
      );
    } else if (this.view === 'data' && this.dashboard?.paneFocused) {
      // Keep Back ordered with any unfinished pane selection paint.
      await this.queueDashboardInteraction(async () => {
        this.dashboard!.paneFocused = false;
        this.dashboard!.paneIndex = 0;
        return this.hud.updatePlanningPane(this.dashboardPane()!);
      });
    } else if (this.view === 'data' && this.pendingTitle === 'PLANNING'
      && this.dataSurface?.planningNavigation) {
      if (this.planningBackPending) return;
      if (!this.dataSurface.planningNavigation.canGoBack) {
        await this.close();
        return;
      }
      if (!this.pendingRequestId) return;
      this.planningBackPending = true;
      this.dataRowsSelectable = false;
      this.dataSelectionPending = true;
      this.link.send({ kind: 'input', event: {
        containerName: 'capability-lifecycle', selectedName: 'planning-back',
        requestId: this.pendingRequestId,
      } });
      this.armResponseTimeout('PLANNING', this.pendingRequestId);
    } else if (
      this.view === 'data'
      && this.pendingTitle === 'PLANNING'
      && this.planningDrillDown
    ) {
      this.planningDrillDown = false;
      const parent = this.dataReturn;
      const selected = parent?.planningEntries?.[parent.selectedIndex];
      // An event detail is read-only and leaves the Mac's published row map
      // unchanged. Scope/day/paging controls and reminder mutations instead
      // keep their authoritative refresh path.
      if (this.dataSurface?.title.trim().toUpperCase() === 'PLANNING · EVENT'
        && !this.dataSurface.selectable && parent?.selectable
        && selected?.kind === 'event' && this.pendingRequestId) {
        this.dataSelectionPending = false;
        await this.returnToDataSurface();
      } else {
        await this.openCapability('Planning', CAPABILITIES.indexOf('Planning'));
      }
    } else if (
      this.view === 'data'
      && this.pendingTitle === 'QUICKNOTES'
      && this.dataSurface?.title.trim().toUpperCase() === 'QUICKNOTES · DISCARD?'
      && this.pendingRequestId
    ) {
      if (this.dataSelectionPending) return;
      // Recording controls are state changes, not read-only detail pages.
      // The Mac must cancel discard and publish its current action mapping
      // before a row can be selected again.
      this.dataReturn = undefined;
      this.dataGrandparent = undefined;
      this.dataRowsSelectable = false;
      this.dataSelectionPending = true;
      this.link.send({
        kind: 'input',
        event: {
          containerName: 'capability-lifecycle',
          selectedName: 'quicknotes-back',
          requestId: this.pendingRequestId,
        },
      });
      this.armResponseTimeout('QUICKNOTES', this.pendingRequestId);
    } else if (this.view === 'data' && this.dataReturn) {
      await this.returnToDataSurface();
    } else {
      // Data and settings leave to the overview: their parent used to be the
      // tile sidebar, and the wearer's menu is the firmware's now.
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
      if (this.dataSurface?.messageProse && eventType !== OsEventTypeList.CLICK_EVENT) {
        return this.handleScroll(eventType);
      }
      if (eventType !== OsEventTypeList.CLICK_EVENT && this.canRequestPanelPage(eventType)) {
        this.requestPanelPage(eventType);
        return true;
      }
      if (this.dashboard) {
        if (eventType === OsEventTypeList.CLICK_EVENT) return this.selectDataRow();
        this.dashboardScroll(
          eventType === OsEventTypeList.SCROLL_BOTTOM_EVENT
            ? 1
            : eventType === OsEventTypeList.SCROLL_TOP_EVENT ? -1 : 0,
        );
        return true;
      }
      if (this.usesOrderedSelection()) {
        // Some hosts wrap custom-surface gestures in a native List event.
        // Its hidden index does not own the selection visible on this HUD.
        if (eventType !== OsEventTypeList.CLICK_EVENT) return this.handleScroll(eventType);
        this.dataSelectionUsesStaleNativeIndex = false;
        return this.selectDataRow();
      }
      const messageRows = this.customMessageRows();
      if (messageRows) {
        // Some hosts report a hidden native cursor for the custom text
        // capture. A differing report must never replace the visible cursor.
        if (eventType !== OsEventTypeList.CLICK_EVENT) return this.handleScroll(eventType);
        const reported = list.currentSelectItemIndex;
        if (reported !== undefined && reported !== 0 && reported !== this.selectedIndex) return true;
        this.dataSelectionUsesStaleNativeIndex = false;
        return this.selectDataRow();
      }
      if (this.dataSurface?.quickNotesPage && this.dataPagePending) return true;
      const maximum = this.messagesRetry ? 0 : Math.max(0, this.dataItemCount - 1);
      if (eventType !== OsEventTypeList.CLICK_EVENT) {
        if (this.isQuickNotesReader()) {
          this.requestQuickNotesPage(eventType);
          return true;
        }
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
        // Hidden custom-list cursors can keep reporting zero even at the
        // endpoint. Preserve the mirrored boundary too, or the next Down
        // resets the controller to row zero without moving the visible HUD.
        const staleNativeIndex = delta !== 0
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
        if (stayedAtBoundary && this.canRequestQuickNotesListPage(eventType)) {
          this.requestQuickNotesListPage(eventType);
          return true;
        }
        const messagesReader = this.messagesScreen === 'message_detail' || this.messagesScreen === 'review';
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
    const reported = list.currentSelectItemIndex ?? 0;
    // Custom visual surfaces own their cursor through handleScroll(). Some
    // G2 hosts still deliver their subsequent tap as a native List event,
    // whose hidden list cursor remains at row zero. A zero report after our
    // cursor visibly moved is therefore stale, not an instruction to open
    // Messages. Preserve a genuine non-zero List selection for the fallback
    // hosts that do keep that native cursor in sync.
    const index = reported === 0 && this.selectedIndex !== 0
      ? this.selectedIndex
      : reported;
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
      if (this.usesOrderedSelection()) {
        this.moveRowSelection(delta, eventType);
        return true;
      }
      if (this.dataSurface?.quickNotesPage && this.dataPagePending) return true;
      if (this.canRequestQuickNotesListPage(eventType)) {
        this.requestQuickNotesListPage(eventType);
        return true;
      }
      if (this.messagesScreen === 'review' || this.dataSurface?.messageProse) {
        if (this.canRequestMessagePage(eventType)) this.requestMessagePage(eventType);
        return true;
      }
      if (this.isQuickNotesReader()) {
        this.requestQuickNotesPage(eventType);
        return true;
      }
      if (this.dashboard) {
        this.dashboardScroll(delta);
        return true;
      }
      if (this.canRequestPanelPage(eventType)) {
        this.requestPanelPage(eventType);
        return true;
      }
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
      const messageRows = this.customMessageRows();
      const next = messageRows
        ? messageRows[Math.max(0, Math.min(messageRows.length - 1, messageRows.indexOf(this.selectedIndex) + delta))]!
        : Math.max(0, Math.min(maximum, this.selectedIndex + delta));
      const applied = next - this.selectedIndex;
      this.selectedIndex = next;
      if (applied !== 0) void this.hud.moveSelection(applied);
      return true;
    }
    // Retired Settings links show phone guidance, with no local choices.
    return true;
  }

  private usesOrderedSelection(): boolean {
    if (this.view !== 'data' || this.dashboard || !this.dataRowsSelectable
      || !this.dataSurface?.selectable || !this.hud.usesCustomListSelection()
      || this.dataSurface.messageProse) return false;
    if (this.pendingTitle === 'PLANNING') return true;
    if (this.pendingTitle === 'QUICKNOTES') return !!this.dataSurface.quickNotesPage;
    return this.pendingTitle === 'MESSAGES' && this.messagesScreen !== 'review'
      && this.messagesScreen !== 'message_detail';
  }

  private rowSelectionCurrent(driver: NonNullable<CapabilityMenu['rowSelection']>): boolean {
    return this.rowSelection === driver && this.view === 'data'
      && this.dataSurface === driver.surface && this.pendingRequestId === driver.requestId
      && this.transitionId === driver.transition;
  }

  private moveRowSelection(delta: number, eventType: number): void {
    const surface = this.dataSurface;
    if (!surface || !this.usesOrderedSelection() || this.dataSelectionPending || this.dataPagePending) return;
    let driver = this.rowSelection;
    const start = !driver || !this.rowSelectionCurrent(driver);
    if (start) {
      const rows = this.messagesRetry ? [0] : this.customMessageRows()
        ?? Array.from({ length: this.dataItemCount }, (_, index) => index);
      if (!rows.length) return;
      driver = {
        surface, requestId: this.pendingRequestId, transition: this.transitionId,
        desired: this.selectedIndex, painted: this.selectedIndex, rows, tap: false,
      };
      this.rowSelection = driver;
    }
    if (!driver || driver.tap || driver.boundary !== undefined) return;
    const position = Math.max(0, driver.rows.indexOf(driver.desired));
    const next = driver.rows[Math.max(0, Math.min(driver.rows.length - 1, position + delta))]!;
    if (next === driver.desired && (this.canRequestQuickNotesListPage(eventType, next)
      || this.canRequestMessagePage(eventType, next))) {
      // Preserve the first endpoint gesture, but let its row finish painting
      // before the existing Mac-owned pager advances the source snapshot.
      driver.boundary = eventType;
    }
    driver.desired = next;
    if (start) void this.paintRowSelection(driver);
  }

  private async paintRowSelection(driver: NonNullable<CapabilityMenu['rowSelection']>): Promise<void> {
    let success = false;
    try {
      while (this.rowSelectionCurrent(driver) && driver.painted !== driver.desired) {
        const target = driver.desired;
        const painted = await this.hud.restoreSelection(target);
        if (!this.rowSelectionCurrent(driver)) return;
        if (!painted) return;
        driver.painted = target;
        this.selectedIndex = target;
      }
      success = this.rowSelectionCurrent(driver);
    } catch {
      // Failed or superseded paints cannot authorize a tap or page request.
    } finally {
      if (this.rowSelectionCurrent(driver)) {
        this.selectedIndex = driver.painted;
        this.rowSelection = undefined;
        if (success && driver.boundary !== undefined) {
          if (this.canRequestQuickNotesListPage(driver.boundary)) this.requestQuickNotesListPage(driver.boundary);
          else if (this.canRequestMessagePage(driver.boundary)) this.requestMessagePage(driver.boundary);
        } else if (success && driver.tap) this.selectDataRow();
      } else if (this.rowSelection === driver) {
        this.rowSelection = undefined;
      }
    }
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

  private dashboardPane():
  { dateIndex: number; focused: boolean; itemIndex: number } | undefined {
    return this.dashboard
      ? {
        dateIndex: this.dashboard.dateIndex,
        focused: this.dashboard.paneFocused,
        itemIndex: this.dashboard.paneIndex,
      }
      : undefined;
  }

  private dashboardEntryCount(): number {
    const cell = this.dashboard?.grid[this.dashboard.dateIndex];
    return cell?.entries?.length ?? 0;
  }

  /** Input stays bound to one published month and follows visible paints.
   * A failed selection paint retires already queued taps; the next gesture
   * starts from the last committed selection. */
  private queueDashboardInteraction(action: () => Promise<boolean>): Promise<boolean> {
    const dashboard = this.dashboard;
    const request = this.pendingRequestId;
    const previous = this.dashboardSelectionPaint ?? Promise.resolve(true);
    const paint = previous.then(async ok => {
      if (!ok || !dashboard || this.dashboard !== dashboard || this.view !== 'data'
        || this.pendingRequestId !== request || this.dataSelectionPending) return false;
      if (this.dashboardFailedPaint === dashboard) {
        if (!await this.hud.updatePlanningPane(this.dashboardPane()!)) return false;
        if (this.dashboard !== dashboard || this.view !== 'data' || this.pendingRequestId !== request) return false;
        this.dashboardFailedPaint = undefined;
      }
      const prior = { dateIndex: dashboard.dateIndex, paneFocused: dashboard.paneFocused, paneIndex: dashboard.paneIndex };
      let painted = false;
      try { painted = await action(); } catch { /* Retain the last visible binding. */ }
      if (!painted && this.dashboard === dashboard) {
        Object.assign(dashboard, prior);
        this.dashboardFailedPaint = dashboard;
      }
      return painted;
    }).catch(() => false);
    this.dashboardSelectionPaint = paint;
    void paint.then(() => {
      if (this.dashboardSelectionPaint === paint) this.dashboardSelectionPaint = undefined;
    });
    return paint;
  }

  /** Dates own grid scrolling; the pane includes its entries and Open day.
   * Month edges use the existing previous/next Mac action rows. */
  private dashboardScroll(delta: number): void {
    if (!this.dashboard || delta === 0) return;
    void this.queueDashboardInteraction(async () => {
      const dashboard = this.dashboard!;
      if (dashboard.paneFocused) {
        dashboard.paneIndex = Math.max(0, Math.min(this.dashboardEntryCount(), dashboard.paneIndex + delta));
        return this.hud.updatePlanningPane(this.dashboardPane()!);
      }
      const next = dashboard.dateIndex + delta;
      if (next < 0 || next >= dashboard.grid.length) {
        if (!this.pendingTitle || !this.pendingRequestId) return false;
        this.dataSelectionPending = true;
        this.link.send({ kind: 'input', event: {
          containerName: 'capdata', selectedIndex: next < 0 ? 6 : 7,
          selectedName: this.pendingTitle, requestId: this.pendingRequestId,
        } });
        this.armResponseTimeout(this.pendingTitle, this.pendingRequestId);
        return true;
      }
      dashboard.dateIndex = next;
      dashboard.paneIndex = 0;
      return this.hud.updatePlanningPane(this.dashboardPane()!);
    });
  }

  /** Empty dates also have Open day. Item taps retain the Mac's published
   * day/index identity binding; Open day exposes the complete paginated list. */
  private dashboardTap(): void {
    if (!this.dashboard) return;
    void this.queueDashboardInteraction(async () => {
      const dashboard = this.dashboard!;
      if (!dashboard.paneFocused) {
        dashboard.paneFocused = true;
        dashboard.paneIndex = 0;
        return this.hud.updatePlanningPane(this.dashboardPane()!);
      }
      const cell = dashboard.grid[dashboard.dateIndex];
      if (!cell || !this.pendingTitle || !this.pendingRequestId) return false;
      this.planningDrillDown = true;
      this.dataSelectionPending = true;
      this.link.send({ kind: 'input', event: {
        containerName: 'capgrid', selectedIndex: dashboard.paneIndex,
        selectedName: this.pendingTitle, requestId: this.pendingRequestId,
        planningDay: cell.day,
      } });
      this.armResponseTimeout(this.pendingTitle, this.pendingRequestId);
      return true;
    });
  }

  private selectDataRow(): true {
    if (this.usesOrderedSelection() && this.dataPagePending) return true;
    if (this.messageProsePainting) return true;
    const messageProse = this.dataSurface?.messageProse;
    if (this.messagesScreen === 'review' && messageProse
      && messageProse.index !== messageProse.pages.length - 1) return true;
    if (this.rowSelection && this.rowSelectionCurrent(this.rowSelection)) {
      if (this.rowSelection.boundary === undefined) this.rowSelection.tap = true;
      return true;
    }
    if (this.dashboard) {
      this.dashboardTap();
      return true;
    }
    if (this.messagesRetry) {
      this.messagesRetry = false;
      void this.openCapability('Messages', 0);
      return true;
    }
    if (!this.dataRowsSelectable || this.dataItemCount === 0) return true;
    if (this.dataSurface?.quickNotesPage && this.dataPagePending) return true;
    if (this.dataSelectionPending) return true;
    this.dataSelectionPending = true;
    if (this.messagesScreen === 'review' && this.pendingRequestId) {
      // This is the current draft's review action, not a new recipient row.
      // Retain the original selection/action binding for its confirmation.
      const reader = this.dataSurface?.readerPage;
      if (!reader || reader.index !== reader.count - 1) {
        this.dataSelectionPending = false;
        return true;
      }
      this.link.send({ kind: 'input', event: {
        containerName: 'capdata', selectedName: 'MESSAGES', selectedIndex: reader.index,
        requestId: this.pendingRequestId, traceId: reader.id,
      } });
      return true;
    }
    if (this.pendingTitle === 'PLANNING') this.planningDrillDown = true;
    const random = globalThis.crypto?.randomUUID?.()
      ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
    const traceId = `hud-${random}`;
    if (this.pendingTitle === 'AGENT TERMINAL' && this.pendingRequestId) {
      this.retireTerminalOpening();
      this.terminalOpening = { requestId: this.pendingRequestId, selectionId: traceId };
    }
    if (this.pendingTitle === 'QUICKNOTES' && this.dataSurface?.title.trim().toUpperCase() === 'QUICKNOTES') {
      this.noteReaderTraceId = traceId;
    }
    const readingMessage = this.pendingTitle === 'MESSAGES'
      && this.messagesScreen === 'conversation';
    if (this.pendingTitle === 'MESSAGES' && !readingMessage && this.pendingRequestId) {
      this.retireMessageUI(true);
      this.messageSelection = {
        requestId: this.pendingRequestId, selectionId: traceId, retiredActionIds: new Set(),
      };
      this.messageLifecycleBinding = { requestId: this.pendingRequestId, selectionId: traceId };
    }
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
        ...(this.pendingTitle === 'QUICKNOTES' && this.dataSurface?.quickNotesPage
          ? { quickNotesPageIndex: this.dataSurface.quickNotesPage.index } : {}),
        selectedIndex: this.selectedIndex,
        selectedName: this.pendingTitle,
        traceId,
        requestId: this.pendingRequestId,
      },
    });
    // A row tap has left the currently rendered frame visible while the Mac
    // loads the replacement, and `dataSelectionPending` blocks further taps
    // meanwhile. Do not leave that frame permanently tap-dead if the bridge
    // drops the response: every selectable data surface (Messages,
    // Planning drill-downs, Notifications, Activity, QuickNotes) reuses the
    // same truthful retry state as an initial menu request.
    if (this.pendingTitle && this.pendingRequestId
      && !(this.pendingTitle === 'MESSAGES' && this.messageSelection?.action)) {
      this.armResponseTimeout(this.pendingTitle, this.pendingRequestId);
    }
    return true;
  }

  private select(_index: number): boolean {
    // Cached firmware Settings pages are guidance only; no configuration or
    // connection action can be triggered by an old row index.
    return true;
  }

  /** A row chosen in the firmware contextual menu — the platform's own menu
   * mechanism, and the wearer's only one. A cached retired Settings slot
   * offers phone guidance; visible rows keep their existing Mac-backed open. */
  async openFromContextualMenu(index: number): Promise<void> {
    const picked = CAPABILITIES[index];
    if (picked === undefined) return;
    this.terminalPreludeRevision += 1;
    if (picked === 'Settings') {
      await this.openSettings();
      return;
    }
    if (picked === 'Planning' && this.view === 'data'
      && this.pendingTitle === 'PLANNING' && this.dataSurface && this.dataRowsSelectable) {
      // Reopening the current agenda refreshes its data without first
      // replacing it with a four-tile Loading page. Keep the visible focus
      // bound to the refreshed rows through the same outcome-return path.
      const surface = this.dataSurface;
      await this.openCapability(picked, index, undefined, {
        selection: { contextLabel: surface.contextLabel,
          item: surface.items[this.selectedIndex] ?? '', index: this.selectedIndex },
      });
      return;
    }
    await this.openCapability(picked, index);
  }

  /**
   * A single click on the resting overview opens the tray it is already
   * showing.
   *
   * Notifications is deliberately not a menu row: the dashboard shows the
   * cards themselves, so a row that opens a page to re-read the home screen
   * costs a page and a round trip for nothing. This is the drill-in for what
   * is already on the glass — the same gesture the wearer knows from the
   * platform's own readers.
   */
  async openNotifications(selectedIndex = 0): Promise<void> {
    await this.openCapability('Notifications', CAPABILITIES.length, undefined, undefined, selectedIndex);
  }

  /** A click on the resting overview opens whichever tab the rail has
   * selected. Notifications still asks the Mac; the other three expand
   * from the content the Mac last pushed, so the page paints once, with
   * no "Loading…" frame in between. */
  async openActiveTab(): Promise<void> {
    const tab = this.hud.activeTab();
    if (tab === 'notifications') {
      await this.openNotifications();
      return;
    }
    await this.openPanelTab(tab);
  }

  /**
   * A swipe on the resting overview moves along the tab rail. Consumed at
   * either end too: a swipe that changes nothing is still a swipe on the
   * overview, not an input for the Mac. Returns false when no menu view is
   * closed-and-resting, so every other surface keeps its own scroll.
   */
  handleOverviewSwipe(eventType: number): boolean {
    if (this.view !== 'closed') return false;
    const delta = eventType === OsEventTypeList.SCROLL_BOTTOM_EVENT
      ? 1
      : eventType === OsEventTypeList.SCROLL_TOP_EVENT
        ? -1
        : 0;
    if (delta === 0) return false;
    void this.hud.stepTab(delta);
    return true;
  }

  /** The top level of an expanded panel tab. Structural only — ask
   * `isPanelRefreshable` before reading a gesture off this. */
  isPanelExpanded(): boolean {
    return this.view === 'data'
      && panelTabForTitle(this.pendingTitle) !== undefined
      && this.dataReturn === undefined;
  }

  /** Where long press means refresh rather than the mic: the top level of
   * news or search, whose rows come from a feed that can have moved.
   *
   * POMODORO is excluded. Its page has nothing to re-fetch — the Mac
   * pushes on every phase transition and the plugin ticks the countdown
   * locally — so routing the hold there bought a repaint the wearer
   * cannot see and cost them the mic on that one page, with the hint
   * naming neither. Found by the simulator sweep, 2026-09-02.
   */
  isPanelRefreshable(): boolean {
    return this.isPanelExpanded() && panelTabForTitle(this.pendingTitle) !== 'pomodoro';
  }

  /** Ask the Mac to re-fetch the expanded tab. The reply is an ordinary
   * `hud.panel` push; presentPanelContent repaints this page from it. */
  refreshPanel(): void {
    const tab = panelTabForTitle(this.pendingTitle);
    if (!this.isPanelRefreshable() || tab === undefined) return;
    this.link.send({
      kind: 'input',
      event: {
        containerName: 'caprefresh',
        selectedName: PANEL_TAB_TITLES[tab],
        requestId: this.hud.rail.requestId(tab),
      },
    });
  }

  /**
   * Expand a panel tab from the plugin's cache. This enters the same `data`
   * state a Mac-served list does, so scroll, tap, the 12s retry state,
   * two-deep Back, and reader paging all work unchanged: a row tap sends
   * `capdata` carrying the cached revision, and the Mac answers an opened
   * row with a `hud.list` titled "<TAB> · …" that presentList matches.
   */
  async openPanelTab(tab: PanelContentTab, keepSelection = 0, notifyMac = true): Promise<void> {
    if (notifyMac) this.departSurface(tab);
    else this.retireBrainstormSurface();
    this.leaveMessages();
    this.retireQuickNotesReader('home');
    this.dashboard = undefined;
    this.clearResponseTimeout();
    this.messagesRetry = false;
    const transition = this.beginTransition();
    const title = PANEL_TAB_TITLES[tab];
    const content = this.hud.rail.content(tab);
    // Rows and revision are read together, BEFORE the paint: a push that
    // lands while the page paints must not pair the rows the wearer sees
    // with a revision the Mac would honour for a different list.
    const requestId = this.hud.rail.requestId(tab);
    const rows = content?.rows ?? [];
    const items = rows.length > 0
      ? rows
      : [content?.empty ?? PANEL_EMPTY_COPY[tab]];
    const selectable = rows.length > 0;
    const hint = tab === 'pomodoro' ? POMODORO_EXPANDED_HINT : PANEL_EXPANDED_HINT;
    const shown = await this.hud.showList(items, {
      title, hint, selectable, railTab: tab,
    });
    if (!this.isCurrentTransition(transition)) return;
    if (!shown) {
      await this.close();
      return;
    }
    if (this.hud.rail.requestId(tab) !== requestId) {
      // The cache moved on while the page painted; what is on the glass is
      // already stale. Paint once more from the new cache.
      return this.openPanelTab(tab, keepSelection, false);
    }
    const selectedIndex = await this.restorePanelSelection(keepSelection, items.length, transition);
    if (selectedIndex === undefined || !this.isCurrentTransition(transition)) return;
    if (this.hud.rail.requestId(tab) !== requestId) return this.openPanelTab(tab, keepSelection, false);
    this.view = 'data';
    this.pendingTitle = title;
    this.pendingRequestId = requestId;
    this.dataItemCount = items.length;
    this.dataRowsSelectable = selectable;
    this.dataSelectionPending = false;
    this.dataSelectionUsesStaleNativeIndex = false;
    this.messagesScreen = undefined;
    this.dataHint = hint;
    this.dataPagePending = false;
    this.selectedIndex = selectedIndex;
    this.dataSurface = {
      title,
      items: [...items],
      selectable,
      hint,
      selectedIndex,
      messagesRetry: false,
      railTab: tab,
    };
    this.dataReturn = undefined;
    this.dataGrandparent = undefined;
  }

  /**
   * A fresh push for a tab whose page is on the glass: repaint it in place,
   * keeping the wearer's row where it was. A push for any other tab, or
   * while an opened row is showing, is only cached (HudPresenter did that)
   * — Back will find the newer rows.
   */
  async presentPanelContent(tab: PanelContentTab): Promise<boolean> {
    if (!this.isPanelExpanded() || panelTabForTitle(this.pendingTitle) !== tab) return false;
    const keep = this.selectedIndex;
    await this.openPanelTab(tab, keep, false);
    return true;
  }

  /** After a repaint from the cache, put the wearer's row back, clamped
   * to the rows that exist now. */
  private async restorePanelSelection(
    keep: number, itemCount: number, transition: number,
  ): Promise<number | undefined> {
    const clamped = Math.max(0, Math.min(Math.max(0, itemCount - 1), keep));
    if (clamped > 0) {
      const painted = await this.hud.restoreSelection(clamped);
      if (!this.isCurrentTransition(transition)) return undefined;
      if (!painted) {
        await this.close();
        return undefined;
      }
    }
    return clamped;
  }

  /** The reader for an opened headline or result: the Mac owns the page
   * cursor, and only a hint it authored ("down more", "up previous")
   * makes a boundary swipe into a page request. */
  private canRequestPanelPage(eventType: number): boolean {
    if (this.view !== 'data' || this.dataPagePending || !this.pendingRequestId) return false;
    if (panelTabForTitle(this.pendingTitle) === undefined || this.dataReturn === undefined) {
      return false;
    }
    return this.hintOffersPage(eventType);
  }

  /** Every reader shares one contract: the Mac's own footer says which
   * neighbouring page exists, and only that swipe becomes a request. */
  private hintOffersPage(eventType: number): boolean {
    if (eventType === OsEventTypeList.SCROLL_TOP_EVENT) {
      return this.dataHint?.includes('up previous') === true;
    }
    if (eventType === OsEventTypeList.SCROLL_BOTTOM_EVENT) {
      return this.dataHint?.includes('down more') === true;
    }
    return false;
  }

  private requestPanelPage(eventType: number): void {
    if (!this.pendingRequestId || !this.pendingTitle) return;
    this.dataPagePending = true;
    this.link.send({
      kind: 'input',
      event: {
        containerName: 'cappage',
        selectedName: this.pendingTitle,
        eventType,
        requestId: this.pendingRequestId,
      },
    });
    this.armResponseTimeout(this.pendingTitle, this.pendingRequestId);
  }

  private canRequestQuickNotesListPage(eventType: number, selectedIndex = this.selectedIndex): boolean {
    const page = this.dataSurface?.quickNotesPage;
    if (this.view !== 'data' || this.pendingTitle !== 'QUICKNOTES'
      || this.dataSurface?.title !== 'QUICKNOTES' || !page
      || this.dataPagePending || this.dataSelectionPending || !this.pendingRequestId) return false;
    return eventType === OsEventTypeList.SCROLL_TOP_EVENT
      ? selectedIndex === 0 && page.index > 0
      : eventType === OsEventTypeList.SCROLL_BOTTOM_EVENT
        && selectedIndex === this.dataItemCount - 1 && page.index + 1 < page.count;
  }

  private requestQuickNotesListPage(eventType: number): void {
    const page = this.dataSurface?.quickNotesPage;
    if (!page || !this.pendingRequestId) return;
    this.dataPagePending = true;
    this.link.send({ kind: 'input', event: {
      containerName: 'cappage', selectedName: 'QUICKNOTES · LIST',
      selectedIndex: page.index, eventType, requestId: this.pendingRequestId,
    } });
    this.armResponseTimeout('QUICKNOTES', this.pendingRequestId);
  }

  private isQuickNotesReader(): boolean {
    return this.view === 'data' && this.pendingTitle === 'QUICKNOTES'
      && this.dataSurface !== undefined
      && usesQuickNotesReaderLayout(this.dataSurface.title, this.dataSurface.items);
  }

  private requestQuickNotesPage(eventType: number): void {
    if (!this.isQuickNotesReader() || this.dataPagePending || this.noteProsePainting || !this.noteReaderTraceId
      || !this.pendingRequestId) return;
    const direction = eventType === OsEventTypeList.SCROLL_TOP_EVENT ? -1
      : eventType === OsEventTypeList.SCROLL_BOTTOM_EVENT ? 1 : 0;
    if (!direction) return;
    const prose = this.noteProse;
    if (prose && prose.index + direction >= 0 && prose.index + direction < prose.pages.length) {
      const surface = this.dataSurface!;
      const request = this.pendingRequestId;
      const next = { ...prose, index: prose.index + direction };
      this.noteProsePainting = true;
      void this.hud.showList(this.noteDisplayItems(surface.items, next), {
        title: surface.title, contextLabel: surface.contextLabel,
        hint: this.noteDisplayHint(surface.hint, next), selectable: false,
      }, () => this.isQuickNotesReader() && this.dataSurface === surface
        && this.pendingRequestId === request && this.noteProse === prose).then(ok => {
        if (this.dataSurface !== surface || this.pendingRequestId !== request || this.noteProse !== prose) return;
        if (ok) this.noteProse = next;
        else void this.close();
      }).catch(() => {
        if (this.dataSurface === surface && this.pendingRequestId === request && this.noteProse === prose) {
          void this.close();
        }
      }).finally(() => { this.noteProsePainting = false; });
      return;
    }
    if (!this.hintOffersPage(eventType)) return;
    const counter = /^(\d+)\/(\d+) · /.exec(this.dataSurface?.items[0] ?? '');
    if (!counter) return;
    this.dataPagePending = true;
    this.notePageDirection = direction;
    this.link.send({kind: 'input', event: {
      containerName: 'cappage', selectedName: 'QUICKNOTES',
      selectedIndex: Number(counter[1]) - 1,
      eventType, requestId: this.pendingRequestId, traceId: this.noteReaderTraceId,
    }});
    this.armResponseTimeout('QUICKNOTES', this.pendingRequestId);
  }

  private retireQuickNotesReader(destination: 'list' | 'home'): void {
    if (this.pendingTitle === 'QUICKNOTES' && this.pendingRequestId
      && (this.noteReaderTraceId || destination === 'home')) {
      this.link.send({kind: 'input', event: {
        containerName: 'capability-lifecycle', selectedName: `quicknotes-${destination}`,
        requestId: this.pendingRequestId,
      }});
    }
    this.noteReaderTraceId = undefined;
    this.noteProse = undefined;
    this.notePageDirection = 1;
  }

  private noteDisplayItems(items: readonly string[], prose: { pages: string[][]; index: number }): string[] {
    const suffix = prose.pages.length > 1 ? ` · part ${prose.index + 1}/${prose.pages.length}` : '';
    return [`${items[0] ?? ''}${suffix}`, ...prose.pages[prose.index]!];
  }

  private noteDisplayHint(hint: string | undefined, prose: { pages: string[][]; index: number }): string {
    const parts = (hint ?? '').split(' · ').filter(part => part !== 'up previous' && part !== 'down more');
    if (prose.index > 0 || hint?.includes('up previous')) parts.unshift('up previous');
    if (prose.index + 1 < prose.pages.length || hint?.includes('down more')) parts.unshift('down more');
    return parts.join(' · ');
  }

  private messageDisplayHint(hint: string | undefined, prose: { pages: string[][]; index: number }): string {
    const display = this.noteDisplayHint(hint, prose);
    return prose.index === prose.pages.length - 1 ? display
      : display.split(' · ').filter(part => part !== 'tap: confirm').join(' · ');
  }

  private async openCapability(
    picked: Exclude<(typeof CAPABILITIES)[number], 'Settings'> | 'Notifications',
    index: number,
    planningAnchorUnixMs?: number,
    receiptRefresh?: {
      monitoring?: ProductivityHudModel;
      selection?: { contextLabel?: string; item: string; index: number };
    },
    initialSelectedIndex = 0,
  ): Promise<void> {
    this.departSurface('feature');
    if (picked === 'Brainstorm') this.claimBrainstormVisit(this.captureBrainstormVisit());
    // Retrying an unopened Messages picker replaces its request on the Mac.
    // There is no selected conversation or draft requiring a second Home.
    if (picked !== 'Messages' || this.messageLifecycleBinding) this.leaveMessages();
    this.retireQuickNotesReader('home');
    if (picked === 'Planning') this.planningDrillDown = false;
    this.dashboard = undefined;
    this.clearResponseTimeout();
    this.messagesRetry = false;
    const transition = this.beginTransition();
    const title = picked.toUpperCase();
    const requestId = `hud-request-${this.randomId()}`;
    // A verified outcome already supplies feedback. Request its authoritative
    // agenda immediately instead of spending another paint on a loading page.
    const shown = receiptRefresh !== undefined || await this.hud.showList([`Loading recent ${picked.toLowerCase()}…`], {
      title,
      hint: 'left x2: back',
      ...(usesFirmwareList(title) ? { nativeFirmwareList: true } : {}),
    });
    if (!this.isCurrentTransition(transition)) return;
    if (!shown) {
      await this.close();
      return;
    }
    this.view = 'data';
    this.pendingTitle = title;
    this.pendingRequestId = requestId;
    this.notificationRefreshSelection = picked === 'Notifications'
      ? { requestId, index: initialSelectedIndex } : undefined;
    if (receiptRefresh) this.planningReceiptRefresh = { requestId, ...receiptRefresh };
    if (picked === 'Agent Terminal') {
      // A busy lane opens directly without a picker row, so the request
      // itself identifies this explicit selection on both sides of the wire.
      this.terminalOpening = { requestId, selectionId: requestId };
    }
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
    // Every capability answers over the same lossy path (hud.list,
    // brainstorm.view, or navigation.view). Arm the honest retry state for
    // all of them: a dropped response must never strand the wearer on a
    // "Loading…" frame with only Back as an undocumented escape.
    this.armResponseTimeout(title, requestId);
  }

  /** Take ownership of a Mac-initiated (voice) generation. Mirrors the state
   * openCapability establishes, minus the request we never sent; the normal
   * presentList path then paints and activates it. An unknown title family
   * leaves state untouched so the ordinary guards drop the frame. */
  private adoptVoiceGeneration(title: string, requestId: string): void {
    const family = (title.split('·')[0] ?? '').trim().toUpperCase();
    const known = CAPABILITIES.some((name) => name.toUpperCase() === family);
    if (!known) return;
    this.leaveMessages();
    this.retireTerminalOpening();
    this.retireQuickNotesReader('home');
    this.clearResponseTimeout();
    this.clearProductivityReturn();
    this.view = 'data';
    this.selectedIndex = 0;
    this.pendingTitle = family;
    this.pendingRequestId = requestId;
    this.messagesRetry = false;
    this.messagesScreen = undefined;
    this.dataItemCount = 0;
    this.dataRowsSelectable = false;
    this.dataSelectionPending = false;
    this.dataSelectionUsesStaleNativeIndex = false;
    this.dataHint = undefined;
    this.dataPagePending = false;
    this.dataSurface = undefined;
    this.dataReturn = undefined;
    this.dataGrandparent = undefined;
    this.planningDrillDown = false;
  }

  private armResponseTimeout(title: string, requestId: string): void {
    // One live timer: re-arming replaces rather than orphaning the previous
    // timeout, so a stale timer can never fire behind a newer request.
    this.clearResponseTimeout();
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
    // The guidance must name the row exactly as the sidebar prints it
    // ('QuickNotes', 'Agent Terminal'), so map the timeout title back through
    // CAPABILITIES instead of case-folding it.
    const readable = CAPABILITIES.find((cap) => cap.toUpperCase() === title)
      ?? title.charAt(0) + title.slice(1).toLowerCase();
    this.dataRowsSelectable = false;
    this.dataItemCount = 0;
    const shown = await this.hud.showList(isMessagesRetry
      ? ['Mac did not answer. Tap to retry Messages.']
      : [
        'No response from the Mac yet.',
        `Return to Overview, then open ${readable} to retry.`,
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
    // The timeout page has replaced the old controls. Back must never
    // interpret an abandoned discard dialog as the still-visible surface.
    this.dataSurface = undefined;
    this.dataRowsSelectable = isMessagesRetry;
    this.dataItemCount = isMessagesRetry ? 1 : 0;
    this.messagesScreen = isMessagesRetry ? 'retry' : undefined;
    this.dataHint = isMessagesRetry ? 'tap: retry · left x2: back' : undefined;
    this.dataPagePending = false;
    // A tap whose response was lost must not keep swallowing later taps on
    // the replacement retry surface.
    this.dataSelectionPending = false;
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
  private canRequestMessagePage(eventType: number, selectedIndex = this.selectedIndex): boolean {
    if (this.dataPagePending || this.messageProsePainting || !this.pendingRequestId) return false;
    const isReader = this.messagesScreen === 'message_detail' || this.messagesScreen === 'review'
      || (this.messagesScreen === 'read_only' && this.dataSurface?.messageProse !== undefined);
    if (this.messagesScreen !== 'conversation' && !isReader) return false;
    if (isReader) {
      const prose = this.dataSurface?.messageProse;
      const direction = eventType === OsEventTypeList.SCROLL_TOP_EVENT ? -1
        : eventType === OsEventTypeList.SCROLL_BOTTOM_EVENT ? 1 : 0;
      if (prose && direction && prose.index + direction >= 0 && prose.index + direction < prose.pages.length) return true;
      return this.hintOffersPage(eventType);
    }
    const messageRows = this.customMessageRows();
    const atTop = selectedIndex === (messageRows?.[0] ?? 0);
    const atBottom = selectedIndex === (messageRows?.[messageRows.length - 1]
      ?? Math.max(0, this.dataItemCount - 1));
    if (eventType === OsEventTypeList.SCROLL_TOP_EVENT) {
      return atTop && this.dataHint?.includes('up newer') === true;
    }
    if (eventType === OsEventTypeList.SCROLL_BOTTOM_EVENT) {
      return atBottom && this.dataHint?.includes('down older') === true;
    }
    return false;
  }

  private customMessageRows(): number[] | undefined {
    if (this.messagesScreen !== 'conversation' || !this.dataRowsSelectable
      || !this.hud.usesCustomListSelection?.()) return undefined;
    const rows = this.dataSurface?.items.flatMap((item, index) =>
      parseMessagesConversationRow(item).kind !== 'instruction' ? [index] : []) ?? [];
    // Older arbitrary-row payloads retain their established index contract.
    return rows.length ? rows : undefined;
  }

  private requestMessagePage(eventType: number): void {
    if (!this.pendingRequestId) return;
    const surface = this.dataSurface;
    const prose = surface?.messageProse;
    const direction = eventType === OsEventTypeList.SCROLL_TOP_EVENT ? -1 : 1;
    if (surface && prose && prose.index + direction >= 0 && prose.index + direction < prose.pages.length) {
      const requestId = this.pendingRequestId;
      const next = { ...prose, index: prose.index + direction };
      const current = () => this.view === 'data' && this.dataSurface === surface
        && this.pendingRequestId === requestId && surface.messageProse === prose;
      this.messageProsePainting = true;
      void this.hud.showList(this.noteDisplayItems(surface.items, next), {
        title: surface.title, contextLabel: surface.contextLabel,
        hint: this.messageDisplayHint(surface.hint, next),
        selectable: surface.selectable && next.index === next.pages.length - 1,
      }, current).then(ok => {
        if (!current()) return;
        if (ok) surface.messageProse = next;
        else void this.close();
      }).catch(() => { if (current()) void this.close(); })
        .finally(() => { this.messageProsePainting = false; });
      return;
    }
    const reader = surface?.readerPage;
    // Review uses the shared exact-reader contract. Conversation/history
    // keeps its established request shape and selected-message owner.
    if (this.messagesScreen === 'review' && !reader) return;
    this.messagePageDirection = direction;
    this.dataPagePending = true;
    const traceId = reader?.id ?? `hud-${this.randomId()}`;
    console.info(`[joat trace] trace=${traceId} stage=message-page-requested event=${eventType}`);
    this.link.send({
      kind: 'input',
      event: {
        containerName: 'cappage',
        selectedName: 'MESSAGES',
        ...(reader ? { selectedIndex: reader.index } : {}),
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
    this.planningBackPending = false;
    this.retireTerminalOpening();
    this.clearProductivityReturn();
    this.view = 'transition';
    this.transitionId += 1;
    return this.transitionId;
  }

  private detailReturnSurface(nextTitle: string): DataSurfaceSnapshot | undefined {
    if (!this.pendingTitle || !this.dataSurface) return undefined;
    const current = this.dataSurface.title.trim().toUpperCase();
    const next = nextTitle.trim().toUpperCase();
    if (
      this.pendingTitle === 'QUICKNOTES'
      && [current, next].some((title) => [
        'QUICKNOTES · RECORDING', 'QUICKNOTES · PAUSED', 'QUICKNOTES · DISCARD?',
      ].includes(title))
    ) {
      this.dataReturn = undefined;
      this.dataGrandparent = undefined;
      return undefined;
    }
    if (current === next || next === this.pendingTitle) return undefined;
    if (!next.startsWith(`${this.pendingTitle} ·`)) return undefined;
    return {
      ...this.dataSurface,
      items: [...this.dataSurface.items],
      planningGrid: this.dataSurface.planningGrid ? [...this.dataSurface.planningGrid] : undefined,
      planningPane: this.dashboardPane(),
      selectedIndex: this.selectedIndex,
    };
  }

  private async returnToDataSurface(): Promise<void> {
    const parent = this.dataReturn;
    if (!parent) return;
    if (this.messagesScreen === 'review') {
      this.retireReviewReader();
      const selection = this.messageSelection;
      const action = selection?.action;
      if (selection && action && !messageFinished(action.message.state)) {
        this.sendMessageCancel(selection, action.message.actionId);
        selection.retiredActionIds.add(action.message.actionId);
        selection.action = undefined;
        this.messagePaintRevision += 1;
      }
    }
    if (this.pendingTitle === 'NOTIFICATIONS' && parent.title.trim().toUpperCase() === 'NOTIFICATIONS') {
      // Reading acknowledges a JOAT card. A cached parent would still offer
      // its old active rows against the Mac's updated action mapping.
      await this.openNotifications(parent.selectedIndex);
      return;
    }
    const messagesBinding = this.messageLifecycleBinding;
    if (this.pendingTitle === 'MESSAGES' && parent.title.trim().toUpperCase() === 'MESSAGES') {
      this.retireMessageUI(true);
    }
    if (this.pendingTitle === 'QUICKNOTES') this.retireQuickNotesReader('list');
    if (parent.railTab) {
      // Back from an opened row lands on the tab's CURRENT rows. The Mac
      // pushes every poll, so the list the wearer left may have moved on
      // while they read; a snapshot of it would tap rows the Mac refuses.
      const keep = parent.selectedIndex;
      this.dataReturn = undefined;
      this.dataGrandparent = undefined;
      await this.openPanelTab(parent.railTab, keep);
      return;
    }
    this.dataReturn = this.dataGrandparent;
    this.dataGrandparent = undefined;
    this.clearResponseTimeout();
    const transition = this.beginTransition();
    if (parent.planningGrid?.length === 42 && parent.planningPane) {
      // Back from an entry lands on the dashboard with the same date
      // hovered and the GRID owning input again.
      this.dashboard = {
        grid: [...parent.planningGrid],
        dateIndex: parent.planningPane.dateIndex,
        paneFocused: false,
        paneIndex: 0,
      };
    }
    const shown = await this.hud.showList(parent.messageProse
      ? this.noteDisplayItems(parent.items, parent.messageProse) : parent.items, {
      title: parent.title,
      initialSelectedIndex: this.dashboard ? 0 : parent.selectedIndex,
      contextLabel: parent.contextLabel,
      controlStartIndex: parent.controlStartIndex,
      planningEntries: parent.planningEntries,
      hint: parent.messageProse ? this.messageDisplayHint(parent.hint, parent.messageProse) : parent.hint,
      selectable: parent.selectable && (!parent.messageProse
        || parent.messageProse.index === parent.messageProse.pages.length - 1),
      planningGrid: parent.planningGrid,
      planningPane: this.dashboardPane(),
      ...(parent.railTab ? { railTab: parent.railTab } : {}),
    });
    if (!this.isCurrentTransition(transition)) return;
    if (!shown) {
      await this.close();
      return;
    }
    // Rebuilding a list resets its visible cursor. Restore that cursor before
    // accepting a tap, so the highlighted row and the target stay identical.
    if (parent.selectedIndex > 0 && !this.dashboard && !this.hud.usesCustomListSelection()) {
      const selected = await this.hud.setSelection(parent.selectedIndex);
      if (!this.isCurrentTransition(transition)) return;
      if (!selected) {
        await this.close();
        return;
      }
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
        this.messagesLifecycle('messages-list', messagesBinding);
      } else if (returnedTitle === 'MESSAGES · CONVERSATION') {
        this.messagesLifecycle('messages-conversation', messagesBinding);
      }
    }
  }

  private scheduleProductivityReturn(phase: ProductivityHudModel['phase']): void {
    if (!isTerminalProductivityOutcome(phase) || !this.productivityReturn) return;
    if (phase === 'saved' || phase === 'completed' || phase === 'monitoring') {
      void this.openCapability(
        'Planning', CAPABILITIES.indexOf('Planning'), this.productivityAnchorUnixMs,
        { selection: this.productivitySelection },
      );
      return;
    }
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
        { selection: this.productivitySelection },
      );
    }, PRODUCTIVITY_OUTCOME_RETURN_DELAY_MS);
  }

  /** The bridge verified the Reminder and the Mac persisted its monitor.
   * Reload Planning now, then show that monitoring fact over the same agenda. */
  private scheduleReminderMonitoring(saved: ProductivityHudModel): void {
    if (!this.productivityReturn) return;
    void this.openCapability(
      'Planning', CAPABILITIES.indexOf('Planning'), saved.planningAnchorUnixMs,
      { monitoring: saved, selection: this.productivitySelection },
    );
  }

  /** Refresh data first; the subsequent receipt never reopens or reloads a page. */
  private scheduleMonitoringReceipt(saved: ProductivityHudModel, requestId: string): void {
    const epoch = ++this.productivityReturnEpoch;
    this.productivityReturnTimer = setTimeout(() => {
      this.productivityReturnTimer = undefined;
      if (
        this.productivityReturnEpoch !== epoch
        || this.view !== 'data'
        || this.pendingTitle !== 'PLANNING'
        || this.pendingRequestId !== requestId
        || this.contextualMenuOpen
      ) return;
      void this.hud.showProductivity({
        domain: 'reminders',
        phase: 'monitoring',
        title: saved.title,
        primary: 'Monitoring armed',
        secondary: 'Native alarm is scheduled. JOAT will check at due time.',
        hint: 'left x2: back',
        planningAnchorUnixMs: saved.planningAnchorUnixMs,
      });
    }, PRODUCTIVITY_OUTCOME_RETURN_DELAY_MS);
  }

  private clearProductivityReturn(): void {
    this.planningReceiptRefresh = undefined;
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
    this.departSurface('home');
    this.retireTerminalOpening();
    this.retireQuickNotesReader('home');
    this.leaveMessages();
    this.dashboard = undefined;
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
    // Restore a LIVE home, not a souvenir of the menu — a stale status line
    // over a blank page reads as a dead assistant (observed on hardware).
    await this.hud.showDefault();
    await this.hud.setStatus(this.micIsOpen() ? 'JOAT · listening' : 'JOAT · mic off');
    // HudPresenter restores the buffered dashboard atomically. Asking the Mac
    // to repaint here used to overwrite a newer card with an older last_reply.
  }
}
