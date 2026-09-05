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
} from './HudCanvasRenderer';
import { resolveMessagesViewState, type MessagesScreen } from './MessagesViewState';
import { PANEL_TAB_TITLES, panelTabForTitle } from './PanelRail';
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
  type ResumePolicy,
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
  title: string;
  items: string[];
  selectable: boolean;
  hint?: string;
  planningGrid?: PlanningGridCell[];
  planningPane?: { dateIndex: number; focused: boolean; itemIndex: number };
  selectedIndex: number;
  messagesRetry: boolean;
  messagesScreen?: MessagesScreen;
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

/** Short literal rows stay within even the compact list's secondary-column
 * budget. All status characters remain reachable through its existing scroll;
 * no text is passed through a lossy ellipsis or native 20-row fallback. */
function messageStatusRows(text: string): string[] {
  const rows: string[] = [];
  for (const line of text.match(/[^\r\n]*(?:\r\n|\r|\n|$)/g) ?? []) {
    let row = '';
    let width = 0;
    for (const character of line) {
      const cost = character === '\t' ? 8 : /[\r\n]/.test(character) ? 0 : character.length;
      if (row && width + cost > 24) { rows.push(row); row = ''; width = 0; }
      row += character;
      width += cost;
    }
    if (row) rows.push(row);
  }
  return rows.length ? rows : [''];
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
 * The capability views behind the wearer's ONE menu.
 *
 * That menu is the firmware contextual menu (tap, then long-press): the
 * glasses draw it from the CAPABILITIES rows in types.ts, zero image tiles.
 * This class no longer draws a menu of its own — it opens what the wearer
 * picked and owns the pages beneath it.
 *
 * View machine, and what left double-tap means in each state:
 *   home ──firmware menu──▶ data view, settings, or direct action
 *   settings/data ──left double-tap──▶ home  (left double-tap is BACK, always)
 */

const POLICY_ROWS: ReadonlyArray<{ policy: ResumePolicy; label: string }> = [
  { policy: 'auto', label: 'Resume mic: instantly' },
  { policy: 'mac', label: 'Resume mic: Mac decides' },
  { policy: 'confirm', label: 'Resume mic: ask me first' },
];

const SETTINGS_ACTION_ROWS = [
  { action: 'repeat', label: 'Repeat last reply' },
  { action: 'reconnect', label: 'Reconnect JOAT' },
] as const;

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
  private view: 'closed' | 'transition' | 'settings' | 'data' | 'productivity' = 'closed';
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
  private noteReaderTraceId?: string;
  /** Messages has two real read levels (conversation then full message), so
   * retain those exact Mac-authored frames for contextual Back. */
  private dataSurface?: DataSurfaceSnapshot;
  private dataReturn?: DataSurfaceSnapshot;
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
  /** A Planning row may open a day or item detail. Back returns to the live
   * schedule rather than throwing the wearer all the way to the overview. */
  private planningDrillDown = false;
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
    private readonly settings: Settings,
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
    if (messageFinished(message.state)) selection.retiredActionIds.add(message.actionId);
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
    const selection = this.messageSelection;
    this.messageSelection = undefined;
    this.messagePaintRevision += 1;
    if (selection) this.clearResponseTimeout();
    if (!notifyMac) this.messageLifecycleBinding = undefined;
    if (notifyMac && selection?.action && !messageFinished(selection.action.message.state)) {
      this.sendMessageCancel(selection, selection.action.message.actionId);
    }
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
    if (this.messagesScreen === 'message_detail' || this.messagesScreen === 'compose'
      || this.messagesScreen === 'read_only') {
      parent = this.dataReturn;
      grandparent = this.dataGrandparent;
    }
    if (parent === this.dataSurface && parent) parent = { ...parent, selectedIndex: this.selectedIndex };
    const rows = messageStatusRows(accepted.text);
    const title = `MESSAGES · ${accepted.state.toUpperCase()}`;
    const hint = 'swipe: read · left x2: back';
    const transition = this.beginTransition();
    const stillCurrent = () => current() && this.transitionId === transition;
    const shown = await this.hud.showList(rows, { title, hint, selectable: false }, stillCurrent);
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
    this.dataSurface = { title, items: rows, hint, selectable: false, selectedIndex: 0, messagesRetry: false, messagesScreen: 'read_only' };
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

  /** Contextual-menu entry: settings remains reachable without consuming one
   * of the five 576x288 sidebar rows. */
  async openSettings(): Promise<void> {
    this.departSurface('settings');
    this.leaveMessages();
    this.retireQuickNotesReader('home');
    this.pendingTitle = undefined;
    this.pendingRequestId = undefined;
    const transition = this.beginTransition();
    this.selectedIndex = 0;
    const shown = await this.hud.showList(
      [
        ...POLICY_ROWS.map((row) => this.label(row)),
        ...SETTINGS_ACTION_ROWS.map((row) => row.label),
      ],
      { ...SETTINGS_SURFACE, selectable: true },
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

    const parent = this.detailReturnSurface(title);
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
      this.dashboard = {
        grid: [...planningGrid], dateIndex, paneFocused: false, paneIndex: 0,
      };
    } else {
      this.dashboard = undefined;
    }
    const expectedRequestId = this.pendingRequestId;
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
    const messageRows = messagesState?.screen === 'conversation' && effectiveSelectable
      && this.hud.usesCustomListSelection?.()
      ? items.flatMap((item, index) => parseMessagesConversationRow(item).kind !== 'instruction' ? [index] : [])
      : [];
    const initialSelection = messageRows[0] ?? 0;
    const shown = await this.hud.showList(items, {
      title,
      hint: effectiveHint,
      selectable: effectiveSelectable,
      planningGrid,
      planningPane: this.dashboardPane(),
      ...(initialSelection > 0 ? { initialSelectedIndex: initialSelection } : {}),
      ...(railTab ? { railTab } : {}),
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
    this.selectedIndex = initialSelection;
    this.dataSurface = {
      title,
      items: [...items],
      selectable: effectiveSelectable,
      hint: effectiveHint,
      planningGrid: planningGrid ? [...planningGrid] : undefined,
      selectedIndex: initialSelection,
      messagesRetry: this.messagesRetry,
      messagesScreen: this.messagesScreen,
      ...(railTab ? { railTab } : {}),
    };
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
    this.selectedIndex = 0;
    stateReady = true;
    // A deferred confirmation, failed paint, or newer transition can be
    // handled without a current visual page. Only a retained exact surface is
    // eligible for the due-HUD acknowledgement.
    const visible = this.hud.isProductivityVisible(productivity);
    if (visible || visibilityArrivedEarly) afterProductivityVisible();
    return visible;
  }

  /** Left double-tap is BACK, and only back. The wearer's menu is the
   * firmware contextual menu (tap, then long-press); this gesture never
   * draws a second one. On the overview there is no level left to leave. */
  async toggle(): Promise<void> {
    this.terminalPreludeRevision += 1;
    if (this.view === 'closed') {
      return;
    } else if (this.view === 'productivity' && this.productivityReturn) {
      const capability = this.productivityReturn;
      await this.openCapability(
        capability,
        CAPABILITIES.indexOf(capability),
        this.productivityAnchorUnixMs,
      );
    } else if (this.view === 'data' && this.dashboard?.paneFocused) {
      // First Back hands input from the day pane to the date grid.
      this.dashboard.paneFocused = false;
      this.dashboard.paneIndex = 0;
      await this.hud.updatePlanningPane(this.dashboardPane()!);
    } else if (
      this.view === 'data'
      && this.pendingTitle === 'PLANNING'
      && this.planningDrillDown
    ) {
      this.planningDrillDown = false;
      await this.openCapability('Planning', CAPABILITIES.indexOf('Planning'));
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
    // Settings is the one remaining locally-selectable page.
    const maximum = POLICY_ROWS.length + SETTINGS_ACTION_ROWS.length - 1;
    this.selectedIndex = Math.max(0, Math.min(maximum, this.selectedIndex + delta));
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

  /** Scroll on the dashboard: dates while the grid owns input, items once
   * the pane does. Scrolling past either month edge asks the Mac for the
   * neighbouring month through the existing row contract (rows 6 and 7 of
   * the month list are its previous/next actions). */
  private dashboardScroll(delta: number): void {
    const dashboard = this.dashboard;
    if (!dashboard || delta === 0) return;
    if (dashboard.paneFocused) {
      const maximum = Math.max(0, this.dashboardEntryCount() - 1);
      dashboard.paneIndex = Math.max(0, Math.min(maximum, dashboard.paneIndex + delta));
      void this.hud.updatePlanningPane(this.dashboardPane()!);
      return;
    }
    const next = dashboard.dateIndex + delta;
    if (next < 0 || next > 41) {
      const monthRow = next < 0 ? 6 : 7;
      if (this.pendingTitle && this.pendingRequestId) {
        this.link.send({
          kind: 'input',
          event: {
            containerName: 'capdata',
            selectedIndex: monthRow,
            selectedName: this.pendingTitle,
            requestId: this.pendingRequestId,
          },
        });
        this.armResponseTimeout(this.pendingTitle, this.pendingRequestId);
      }
      return;
    }
    dashboard.dateIndex = next;
    dashboard.paneIndex = 0;
    void this.hud.updatePlanningPane(this.dashboardPane()!);
  }

  /** Tap on the dashboard: the first hands input to the right pane, the
   * second opens the hovered item. The wire carries only the date and the
   * row position — the Mac re-resolves the entry from a fresh projection. */
  private dashboardTap(): void {
    const dashboard = this.dashboard;
    if (!dashboard) return;
    if (!dashboard.paneFocused) {
      if (this.dashboardEntryCount() === 0) return;
      dashboard.paneFocused = true;
      dashboard.paneIndex = 0;
      void this.hud.updatePlanningPane(this.dashboardPane()!);
      return;
    }
    const cell = dashboard.grid[dashboard.dateIndex];
    if (!cell || !this.pendingTitle || !this.pendingRequestId) return;
    const anchor = Date.parse(`${cell.day}T12:00:00Z`);
    if (!Number.isFinite(anchor)) return;
    this.planningDrillDown = true;
    this.link.send({
      kind: 'input',
      event: {
        containerName: 'capgrid',
        selectedIndex: dashboard.paneIndex,
        selectedName: this.pendingTitle,
        requestId: this.pendingRequestId,
        planningAnchorUnixMs: anchor,
      },
    });
    this.armResponseTimeout(this.pendingTitle, this.pendingRequestId);
  }

  private selectDataRow(): true {
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
    if (this.dataSelectionPending) return true;
    this.dataSelectionPending = true;
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

  private select(index: number): boolean {
    const policy = POLICY_ROWS[index];
    if (policy) {
      this.settings.setResumePolicy(policy.policy);
      this.link.send({ kind: 'policy', resume: policy.policy });
      void this.close();
      return true;
    }
    const action = SETTINGS_ACTION_ROWS[index - POLICY_ROWS.length];
    if (action?.action === 'repeat') {
      void this.repeatLastReply();
    } else if (action?.action === 'reconnect') {
      void this.reconnectToMac();
    }
    return true;
  }

  private label(row: (typeof POLICY_ROWS)[number]): string {
    const active = row.policy === this.settings.resumePolicy;
    return `${active ? '*' : ' '} ${row.label}`;
  }

  private async repeatLastReply(): Promise<void> {
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

  /** A row chosen in the firmware contextual menu — the platform's own menu
   * mechanism, and the wearer's only one. Settings is a local page; every
   * other row is the same Mac-backed open a menu tap performs. */
  async openFromContextualMenu(index: number): Promise<void> {
    const picked = CAPABILITIES[index];
    if (picked === undefined) return;
    this.terminalPreludeRevision += 1;
    if (picked === 'Settings') {
      await this.openSettings();
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
  async openNotifications(): Promise<void> {
    await this.openCapability('Notifications', CAPABILITIES.length);
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

  private isQuickNotesReader(): boolean {
    return this.view === 'data' && this.pendingTitle === 'QUICKNOTES'
      && this.dataSurface !== undefined
      && usesQuickNotesReaderLayout(this.dataSurface.title, this.dataSurface.items);
  }

  private requestQuickNotesPage(eventType: number): void {
    if (!this.isQuickNotesReader() || this.dataPagePending || !this.noteReaderTraceId
      || !this.pendingRequestId || !this.hintOffersPage(eventType)) return;
    const counter = /^(\d+)\/(\d+) · /.exec(this.dataSurface?.items[0] ?? '');
    if (!counter) return;
    this.dataPagePending = true;
    this.link.send({kind: 'input', event: {
      containerName: 'cappage', selectedName: 'QUICKNOTES',
      selectedIndex: Number(counter[1]) - 1,
      eventType, requestId: this.pendingRequestId, traceId: this.noteReaderTraceId,
    }});
    this.armResponseTimeout('QUICKNOTES', this.pendingRequestId);
  }

  private retireQuickNotesReader(destination: 'list' | 'home'): void {
    if (this.pendingTitle === 'QUICKNOTES' && this.noteReaderTraceId && this.pendingRequestId) {
      this.link.send({kind: 'input', event: {
        containerName: 'capability-lifecycle', selectedName: `quicknotes-${destination}`,
        requestId: this.pendingRequestId,
      }});
    }
    this.noteReaderTraceId = undefined;
  }

  private async openCapability(
    picked: Exclude<(typeof CAPABILITIES)[number], 'Settings'> | 'Notifications',
    index: number,
    planningAnchorUnixMs?: number,
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
  private canRequestMessagePage(eventType: number): boolean {
    if (this.dataPagePending || !this.pendingRequestId) return false;
    const isReader = this.messagesScreen === 'message_detail';
    if (this.messagesScreen !== 'conversation' && !isReader) return false;
    if (isReader) return this.hintOffersPage(eventType);
    const messageRows = this.customMessageRows();
    const atTop = this.selectedIndex === (messageRows?.[0] ?? 0);
    const atBottom = this.selectedIndex === (messageRows?.[messageRows.length - 1]
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
    const shown = await this.hud.showList(parent.items, {
      title: parent.title,
      hint: parent.hint,
      selectable: parent.selectable,
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
    if (parent.selectedIndex > 0 && !this.dashboard) {
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
