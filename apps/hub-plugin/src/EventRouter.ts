import { OsEventTypeList, EventSourceType, type EvenHubEvent } from '@evenrealities/even_hub_sdk';
import { GesturePolicy, type GestureSource } from './GesturePolicy';
import type { BridgeClient } from './BridgeClient';
import type { MacLink } from './MacLink';
import type { AudioStreamer } from './AudioStreamer';
import type { ImuMonitor } from './ImuMonitor';
import type { CapabilityMenu } from './CapabilityMenu';
import type { G2DiagnosticTrace, G2TraceEvent } from './G2DiagnosticTrace';
import {
  CAPABILITY_MENU_ID_BASE,
  QUICKNOTES_UNDO_MENU_ID,
  type BrainstormAction,
  type BrainstormHudScreen,
} from './types';

type G2Gesture = Extract<G2TraceEvent, { type: 'gesture' }>['gesture'];
type G2GestureDelivery = Extract<G2TraceEvent, { type: 'gesture' }>['delivery'];

/**
 * Lifecycle hooks. The SDK reports app suspend/resume/exit as sysEvents, and
 * they are the only chance we get to stop the mic and flush state — if the
 * wearer exits and we ignore it, the glasses mic keeps streaming.
 */
export interface LifecycleHandler {
  onForegroundEnter(): void;
  onForegroundExit(): void;
  /** Temporary native menu overlay; does not suspend the app. */
  onContextualMenu?(open: boolean): void;
  /** Terminal: SYSTEM_EXIT (user confirmed) or ABNORMAL_EXIT (disconnect). */
  onExit(reason: 'system' | 'abnormal'): void;
}

export interface NavigationInputOwner {
  isActive(): boolean;
  handleClick(): boolean;
  handleScroll(direction: 'up' | 'down'): Promise<boolean>;
  /** Double press exits the current navigation session. */
  handleCancel(): boolean;
}

/** The live Brainstorm surface gets first claim on its explicit gestures. */
export interface BrainstormInputOwner {
  isActive(): boolean;
  handleClick(): boolean;
  /** Double press cancels an active Brainstorm round. */
  handleCancel(): boolean;
  /** A swipe during topic capture backs out of the armed capture; every
   * other state lets the swipe fall through to transcript scrolling. */
  handleSwipe(): boolean;
  /** Long-press on a running work session cancels its job; false elsewhere,
   * where long-press stays the microphone. */
  handleLongPress(): boolean;
  /** Fixed receipt of a successfully handed-off action; never prompt content. */
  consumeAcceptedAction(): { action: BrainstormAction; surface: BrainstormHudScreen } | undefined;
}

/** A transient JOAT reply sits above the active feature, without destroying
 * it. Ordinary input dismisses that reply first; long-press remains the
 * always-available microphone privacy control. */
export interface AssistantOverlayInputOwner {
  hasAssistantOverlay(): boolean;
  scrollAssistantOverlay(direction: 'up' | 'down'): boolean;
  dismissAssistantOverlay(): Promise<boolean>;
  hasAssistantConversation(): boolean;
  assistantConversationIdentity(): { conversationId: string; revision: number } | undefined;
  isAssistantConversationContinuePromptVisible(): boolean;
  scrollAssistantConversation(direction: 'up' | 'down'): boolean;
  continueAssistantConversation(): Promise<boolean>;
  requestAssistantConversationExit(): Promise<'inactive' | 'prompted' | 'exited' | 'failed'>;
  hasVisibleNotification(): boolean;
  visibleNotificationId?(): string | undefined;
  dismissVisibleNotification(): Promise<boolean>;
  /** The notification card over the page (NotificationCard.ts). */
  hasNotificationCard?(): boolean;
  /** Mark the shown card read and move to the next unread, or close. The
   * id of the card marked read, for the Mac. */
  tapNotificationCard?(): string | undefined;
  /** Close the card without marking it read (Even's double tap). */
  closeNotificationCard?(): Promise<boolean>;
}

export interface PromptInputOwner {
  isActive(): boolean;
  cancel(reason?: string): void;
  /** Tap on the listening box: the acknowledged question's ID to close now. */
  commit?(): string | undefined;
}

/**
 * Single onEvenHubEvent subscription fanned out to typed handlers.
 *
 * One subscription, not four: the SDK delivers every event class through the
 * same callback, and multiple subscriptions make teardown error-prone.
 *
 * ── Event routing, per the SDK's actual behaviour ──
 * With a TEXT container active (our default page):
 *   swipe up/down  → textEvent, eventType 1 / 2
 *   single press   → sysEvent,  eventType 0
 *   double press   → sysEvent,  eventType 3
 * Clicks do NOT arrive as textEvent. Only scrolls do.
 *
 * With a LIST container active (settings menu):
 *   single press   → listEvent with currentSelectItemIndex
 *   double press   → sysEvent, eventType 3
 *   swipe          → handled internally by the SDK, no event
 *
 * ── The protobuf trap ──
 * The transport drops zero-valued fields, so they arrive as `undefined`
 * rather than 0. CLICK_EVENT is 0, and the first list index is 0 — both
 * vanish. Every numeric read below uses `?? 0`. Comparing `eventType === 0`
 * directly silently never matches, which is exactly how a confirm gate ends
 * up looking wired while never firing.
 */
export class EventRouter {
  /**
   * The SDK still delivers raw long-press/release events when the OS opens a
   * contextual menu with tap-then-long-press. Hold the mic action until the
   * release so the subsequent foreground-overlay event can disambiguate a
   * menu gesture without stealing plain long-press mute.
   */
  private readonly gestures = new GesturePolicy();
  private backPending = false;
  private pendingMicLongPress = false;
  /** A long press is talking into an open QuickNote (FEAT-002). */
  private pendingNoteHold = false;
  private contextualMenuOverlayOpen = false;

  constructor(
    private readonly client: BridgeClient,
    private readonly link: MacLink,
    private readonly audio: AudioStreamer,
    private readonly imu: ImuMonitor,
    private readonly menu: CapabilityMenu,
    private readonly lifecycle?: LifecycleHandler,
    private readonly diagnostics?: G2DiagnosticTrace,
    private readonly navigation?: NavigationInputOwner,
    private readonly brainstorm?: BrainstormInputOwner,
    private readonly assistantOverlay?: AssistantOverlayInputOwner,
    private readonly transcriptScroll?: (direction: 'up' | 'down') => boolean,
    private readonly promptedInput?: PromptInputOwner,
    /** The open decision gate's retry, if it offers one — a Messages
     * reply's consent: say it again. Undefined for every other gate, which
     * keeps 2x tap and swipe as its refusal. The retry is true when the
     * gate took the gesture. */
    private readonly confirmationRetry?: () => (() => boolean) | undefined,
  ) {}

  attach(): void {
    this.client.track(this.client.require().onEvenHubEvent((event) => this.dispatch(event)));
  }

  /**
   * Long-press: hold to talk on an open note, refresh News/Search, cancel a
   * running work session — and nothing elsewhere. It was the mic mute until
   * 2026-10-12; the mic now stays on (doff still stops it).
   *
   * Checked in EVERY event class because the SDK README is explicit that
   * Long press/release are delivered inside the matching list, text, or
   * system event. Separately, the supported contextual menu opens after
   * tap-then-long-press and emits foreground enter/exit plus a top-level
   * menu item event. The two paths must compose: raw long press alone mutes;
   * a menu-opening long press does not also mute the microphone.
   *
   * The action is LOCAL: a privacy control must not need the Mac, and
   * handling it here also sidesteps the relay, which routed these events
   * to a Mac that ignored them.
   *
   * Returns true when the event was consumed.
   */
  private handleLongPress(eventType: number | undefined, via: string): boolean {
    const type = eventType ?? 0;
    if (type === OsEventTypeList.LONG_PRESS_RELEASE_EVENT && this.pendingNoteHold) {
      // Letting go ends the paragraph; the mic was never the gesture's.
      this.pendingMicLongPress = false;
      this.endNoteHold(via);
      return true;
    }
    if (type === OsEventTypeList.LONG_PRESS_RELEASE_EVENT) {
      const toggleMic = this.pendingMicLongPress && !this.contextualMenuOverlayOpen;
      this.pendingMicLongPress = false;
      if (toggleMic && !this.promptedInput?.isActive()
        && this.brainstorm?.isActive() && this.brainstorm.handleLongPress()) {
        // The second carve-out (2026-10-06): on a running work session the
        // gesture cancels the job, the wearer's choice over "terminal cancel".
        console.info(`[EventRouter] long-press via ${via} — cancelling the terminal job`);
        return true;
      }
      if (toggleMic && !this.promptedInput?.isActive() && this.menu.isPanelRefreshable()) {
        // The one carve-out from "long press is the mic": on the top level
        // of news or search it re-fetches that tab (the wearer's ruling,
        // 2026-09-02). Everywhere else — the resting overview, an opened
        // article, and the pomodoro page, which has nothing to re-fetch —
        // the privacy control keeps the gesture.
        console.info(`[EventRouter] long-press via ${via} — refreshing panel`);
        this.menu.refreshPanel();
        return true;
      }
      // Long tap no longer mutes (the wearer, 2026-10-12: it fought the
      // QuickNotes hold, and "the mic can stay on"). Outside the carve-outs
      // above and a held note it does nothing; doff still stops the mic.
      // Consumed silently: the release must not reach the Mac as a phantom
      // input. (`pendingMicLongPress` keeps its name: it still separates a
      // plain long press from the OS menu's tap-then-long-press.)
      return true;
    }
    if (type !== OsEventTypeList.LONG_PRESS_EVENT) return false;
    if (!this.contextualMenuOverlayOpen && !this.promptedInput?.isActive()
      && this.menu.isQuickNotesLive?.()) {
      // The third carve-out (FEAT-002, the wearer's Even QuickNote): on an
      // open note the held touchpad talks into it, from the PRESS — the
      // words must not wait for the release. `pendingMicLongPress` still
      // lets the OS contextual menu (tap, then long press) claim the
      // gesture, which then ends the hold.
      this.pendingNoteHold = true;
      this.pendingMicLongPress = true;
      console.info('[EventRouter] long-press on a live note — listening');
      this.menu.sendQuickNotesHold?.('start');
      return true;
    }
    this.pendingMicLongPress = !this.contextualMenuOverlayOpen;
    return true;
  }

  /** A press that ends without its release reaching handleLongPress: the
   * flag it left must not read the next return to JOAT as the glasses' menu
   * opening. A held note paragraph ends with the words already said. */
  private endPress(via: string): void {
    this.pendingMicLongPress = false;
    this.endNoteHold(via);
  }

  /** End a held note paragraph exactly once: kept on release, dropped when
   * the press turned out to open the glasses' menu. */
  private endNoteHold(via: string, phase: 'end' | 'cancel' = 'end'): void {
    if (!this.pendingNoteHold) return;
    this.pendingNoteHold = false;
    console.info(`[EventRouter] long-press ${phase === 'end' ? 'released' : 'cancelled'} via ${via} — note paragraph ${phase === 'end' ? 'done' : 'dropped'}`);
    this.menu.sendQuickNotesHold?.(phase);
  }

  /** A decision gate that offers retry (a Messages draft) takes the 2x tap
   * as retry: back to the listening box, say it again (the wearer's ruling,
   * 2026-10-11: "'confirm' tap | 'retry' 2x"). A gate with no retry keeps
   * 2x tap as its refusal. Long tap and swipe are consumed on a draft, as
   * before, so the mic never toggles behind the decision. */
  private retryOrDeclineConfirmation(): void {
    this.pendingMicLongPress = false;
    const retry = this.confirmationRetry?.();
    if (retry === undefined) { this.imu.declineByTap(); return; }
    if (retry()) {
      console.info('[EventRouter] 2x tap on a reply draft — retry');
    }
  }

  /** The one mute implementation, shared by the gesture and menu item 2. */
  private toggleMic(): void {
    if (this.audio.isOpen) {
      void this.audio.stop();
    } else {
      void this.audio.start(this.audio.lastSource);
    }
  }

  private dispatch(event: EvenHubEvent): void {
    if (this.link.wasReplaced) {
      // A foreground/headless predecessor may still receive host events.
      // Only lifecycle delivery can restore it; gestures must not control
      // the successor's microphone, menu or display.
      this.pendingMicLongPress = false;
      this.contextualMenuOverlayOpen = false;
      const type = event.sysEvent?.eventType;
      if (event.sysEvent && (
        type === OsEventTypeList.FOREGROUND_ENTER_EVENT
        || type === OsEventTypeList.FOREGROUND_EXIT_EVENT
        || type === OsEventTypeList.SYSTEM_EXIT_EVENT
        || type === OsEventTypeList.ABNORMAL_EXIT_EVENT
      )) this.handleSys(event.sysEvent);
      return;
    }
    if (event.audioEvent) {
      this.audio.ingest(
        event.audioEvent.audioPcm, event.audioEvent.speakerRole, event.audioEvent.source,
      );
    }
    const gesture = this.gestures.resolve(event);
    if (gesture?.intent === 'back' && !this.contextualMenuOverlayOpen) {
      this.recordGesture('double_press', gesture.delivery);
      this.handleBack();
      return;
    }


    // A decision card takes a press's release too: the press ends here.
    if (this.imu.isPending() && [event.sysEvent, event.textEvent, event.listEvent]
      .some((envelope) => envelope?.eventType === OsEventTypeList.LONG_PRESS_RELEASE_EVENT)) {
      this.endPress('decision');
    }

    // The decision surface owns every non-lifecycle input. A G2 swipe is the
    // explicit refusal gesture; all other non-system input is consumed so no
    // list or contextual-menu state can mutate behind the prompt.
    if (this.imu.isPending() && !event.sysEvent) {
      if (event.textEvent) {
        this.recordGesture(this.classify(event.textEvent.eventType), 'text');
        const eventType = event.textEvent.eventType ?? OsEventTypeList.CLICK_EVENT;
        if (eventType === OsEventTypeList.CLICK_EVENT) {
          this.imu.confirmByTap();
        } else if (eventType === OsEventTypeList.DOUBLE_CLICK_EVENT) {
          this.retryOrDeclineConfirmation();
        } else if (
          this.confirmationRetry?.() === undefined && (
            eventType === OsEventTypeList.SCROLL_TOP_EVENT ||
            eventType === OsEventTypeList.SCROLL_BOTTOM_EVENT
          )
        ) {
          // A swipe still refuses a gate that has no retry (a calendar or
          // reminder write). On a draft it is consumed: tap and 2x tap only.
          this.imu.declineByTap();
        }
      }
      return;
    }

    // Contextual-menu clicks (SDK 0.0.14). Without this branch a declared
    // menu renders but every selection vanishes silently — the event has
    // its own field and matches none of the other branches.
    if (event.menuItemClickEvent) {
      this.promptedInput?.cancel('context-menu');
      // The simulator's `context_menu` automation input does not replay the
      // physical raw long-press. Preserve the same overlay boundary before
      // its following foreground-exit event reaches lifecycle consumers.
      if (!this.contextualMenuOverlayOpen) this.lifecycle?.onContextualMenu?.(true);
      this.contextualMenuOverlayOpen = true;
      this.pendingMicLongPress = false;
      this.recordGesture('menu_item', 'menu');
      this.handleMenu(event.menuItemClickEvent.itemID ?? 0);
      return;
    }

    if (event.sysEvent) {
      // The host emits a left-source announcement at startup with no event
      // type. CLICK is also protobuf zero, so keep absent right/ring events as
      // real presses, and keep an absent left press while confirmation owns
      // it. The startup announcement is ignored only when it would erase the
      // just-painted assistant overlay; outside that exact state, the legacy
      // protobuf-zero click contract remains intact.
      if (
        event.sysEvent.eventType === undefined
        && event.sysEvent.eventSource === EventSourceType.TOUCH_EVENT_FROM_GLASSES_L
        && !this.imu.isPending()
        && this.assistantOverlay?.hasAssistantOverlay()
      ) return;
      this.recordGesture(this.classify(event.sysEvent.eventType), 'sys');
      // Confirmation owns long press too; it must not toggle the microphone
      // behind a decision or make the visible TAP/SWIPE contract incomplete.
      if (!this.imu.isPending() && this.handleLongPress(event.sysEvent.eventType, 'sysEvent')) {
        return;
      }
      if (this.imu.isPending() && event.sysEvent.eventType === OsEventTypeList.LONG_PRESS_EVENT) {
        this.pendingMicLongPress = false; // consumed: the decision owns it
        return;
      }
      this.handleSys(event.sysEvent);
      return;
    }

    if (this.contextualMenuOverlayOpen) return;

    if (event.textEvent) {
      this.recordGesture(this.classify(event.textEvent.eventType), 'text');
      // Long-press first: on a text-capture page the SDK delivers 9/10
      // HERE, and the relay below would forward it as an ordinary scroll.
      if (this.handleLongPress(event.textEvent.eventType, 'textEvent')) return;
      if (this.handlePromptInput(event.textEvent.eventType)) return;
      if (this.handleNotificationCard(event.textEvent.eventType)) return;
      if (this.handleAssistantConversation(event.textEvent.eventType)) return;
      if (this.dismissAssistantOverlayFor(event.textEvent.eventType)) return;
      if (this.dismissVisibleNotificationFor(event.textEvent.eventType)) return;
      if (this.navigation?.isActive()) {
        const eventType = event.textEvent.eventType ?? OsEventTypeList.CLICK_EVENT;
        if (eventType === OsEventTypeList.SCROLL_TOP_EVENT) {
          void this.navigation.handleScroll('up');
          return;
        }
        if (eventType === OsEventTypeList.SCROLL_BOTTOM_EVENT) {
          void this.navigation.handleScroll('down');
          return;
        }
      }
      // The Brainstorm/terminal page HAS scrollback now — the wearer asked
      // for it in as many words, and a swipe that CANCELLED the session
      // while they tried to re-read a reply was the harshest possible
      // answer to "can I scroll?". Swipes page the transcript; cancel is
      // the deliberate double-press.
      if (this.brainstorm?.isActive()) {
        const swipe = event.textEvent.eventType ?? OsEventTypeList.CLICK_EVENT;
        if (
          swipe === OsEventTypeList.SCROLL_TOP_EVENT
          || swipe === OsEventTypeList.SCROLL_BOTTOM_EVENT
        ) {
          // Topic capture first: it has nothing to scroll, and a swipe
          // there is the wearer backing out of the armed capture.
          if (this.brainstorm.handleSwipe()) return;
          this.transcriptScroll?.(
            swipe === OsEventTypeList.SCROLL_TOP_EVENT ? 'up' : 'down',
          );
          return;
        }
      }
      const eventType = event.textEvent.eventType ?? OsEventTypeList.CLICK_EVENT;
      // The image HUD's invisible capture surface normally receives taps as
      // sysEvent, but some hosts send a textEvent with CLICK omitted by the
      // protobuf encoder. If a menu view owns that surface, select its
      // highlighted row rather than consuming the input as a zero-distance
      // scroll. Closed menus still relay the event unchanged.
      if (
        eventType === OsEventTypeList.CLICK_EVENT
        && this.menu.handleClick()
      ) return;
      if (this.menu.handleScroll(eventType)) return;
      // On the resting overview a swipe moves along the tab rail. Local:
      // the Mac never learns which tab is up, and nothing else owns this
      // gesture there (it used to relay to a Mac that dropped it).
      if (
        !this.navigation?.isActive()
        && !this.brainstorm?.isActive()
        && this.menu.handleOverviewSwipe(eventType)
      ) return;
      // Scrolls and otherwise unowned text clicks relay to the Mac.
      this.link.send({
        kind: 'input',
        event: {
          containerId: event.textEvent.containerID,
          containerName: event.textEvent.containerName,
          eventType,
        },
      });
      return;
    }

    if (event.listEvent) {
      this.recordGesture(this.classify(event.listEvent.eventType), 'list');
      if (this.handleLongPress(event.listEvent.eventType, 'listEvent')) return;
      if (this.handlePromptInput(event.listEvent.eventType)) return;
      if (this.handleNotificationCard(event.listEvent.eventType)) return;
      if (this.handleAssistantConversation(event.listEvent.eventType)) return;
      if (this.dismissAssistantOverlayFor(event.listEvent.eventType)) return;
      // Some G2 firmware represents a Home-tile tap as a List event. A
      // transient notification must own that ordinary tap before the hidden
      // capability row can select or relay it to the Mac.
      if (this.dismissVisibleNotificationFor(event.listEvent.eventType)) return;
      // Settings menu open → selection is a local concern, not Mac input.
      if (this.menu.handleList(event.listEvent)) return;
      this.link.send({
        kind: 'input',
        event: {
          containerId: event.listEvent.containerID,
          containerName: event.listEvent.containerName,
          eventType: event.listEvent.eventType ?? 0,
          selectedIndex: event.listEvent.currentSelectItemIndex ?? 0,
          selectedName: event.listEvent.currentSelectItemName,
        },
      });
    }
  }

  /** One semantic Back action for every SDK delivery envelope and temple.
   * The active owner determines what is left, never whether Back exits JOAT. */
  private handleBack(): void {
    this.pendingMicLongPress = false;
    if (this.imu.isPending()) { this.retryOrDeclineConfirmation(); return; }
    if (this.backPending) return;
    if (this.handlePromptInput(OsEventTypeList.DOUBLE_CLICK_EVENT)) return;
    if (this.handleNotificationCard(OsEventTypeList.DOUBLE_CLICK_EVENT)) return;
    if (this.handleAssistantConversation(OsEventTypeList.DOUBLE_CLICK_EVENT)) return;
    if (this.dismissAssistantOverlayFor(OsEventTypeList.DOUBLE_CLICK_EVENT)) return;
    if (this.navigation?.isActive() && this.navigation.handleCancel()) return;
    if (this.brainstorm?.isActive() && this.brainstorm.handleCancel()) return;
    this.backPending = true;
    void this.menu.toggle().finally(() => { this.backPending = false; });
  }

  /**
   * The notification card owns the page's taps while it is up, as Even's
   * own card does (the wearer's ruling, 2026-10-07): a tap marks it read and
   * shows the next unread, or closes the card after the last; a double tap
   * closes it and leaves it unread. Swipes stay with the card. Long press
   * is not taken: the microphone keeps it.
   */
  private handleNotificationCard(eventType: number | undefined): boolean {
    if (!this.assistantOverlay?.hasNotificationCard?.()) return false;
    const type = eventType ?? OsEventTypeList.CLICK_EVENT;
    if (type === OsEventTypeList.CLICK_EVENT) {
      const read = this.assistantOverlay.tapNotificationCard?.();
      if (read && this.link.linkState === 'connected') {
        this.link.send({ kind: 'input', event: { containerName: 'notification-dismiss', traceId: read } });
      }
      return true;
    }
    if (type === OsEventTypeList.DOUBLE_CLICK_EVENT) {
      void this.assistantOverlay.closeNotificationCard?.();
      return true;
    }
    return type === OsEventTypeList.SCROLL_TOP_EVENT || type === OsEventTypeList.SCROLL_BOTTOM_EVENT;
  }

  /** Questions retain the feature underneath; only Back releases this owner.
   * The listening box's contract (the wearer's ruling, 2026-10-11): tap =
   * review what was heard now, 2x tap = cancel. Swipes are consumed. */
  private handlePromptInput(eventType: number | undefined): boolean {
    if (!this.promptedInput?.isActive()) return false;
    const type = eventType ?? OsEventTypeList.CLICK_EVENT;
    if (type === OsEventTypeList.DOUBLE_CLICK_EVENT) {
      this.promptedInput.cancel('back');
      return true;
    }
    if (type === OsEventTypeList.CLICK_EVENT) {
      const promptId = this.promptedInput.commit?.();
      if (promptId !== undefined && this.link.linkState === 'connected') {
        this.link.send({ kind: 'input', event: { containerName: 'prompt-commit', selectedName: promptId } });
      }
      return true;
    }
    return type === OsEventTypeList.SCROLL_TOP_EVENT
      || type === OsEventTypeList.SCROLL_BOTTOM_EVENT;
  }

  /** Classify raw SDK events without retaining source IDs or user content. */
  private classify(eventType: number | undefined): G2Gesture {
    switch (eventType ?? OsEventTypeList.CLICK_EVENT) {
      case OsEventTypeList.CLICK_EVENT: return 'tap';
      case OsEventTypeList.DOUBLE_CLICK_EVENT: return 'double_press';
      case OsEventTypeList.LONG_PRESS_EVENT: return 'long_press';
      case OsEventTypeList.LONG_PRESS_RELEASE_EVENT: return 'long_press_release';
      case OsEventTypeList.SCROLL_TOP_EVENT: return 'scroll_up';
      case OsEventTypeList.SCROLL_BOTTOM_EVENT: return 'scroll_down';
      case OsEventTypeList.FOREGROUND_ENTER_EVENT: return 'foreground_enter';
      case OsEventTypeList.FOREGROUND_EXIT_EVENT: return 'foreground_exit';
      case OsEventTypeList.SYSTEM_EXIT_EVENT: return 'system_exit';
      case OsEventTypeList.ABNORMAL_EXIT_EVENT: return 'abnormal_exit';
      case OsEventTypeList.IMU_DATA_REPORT: return 'imu_report';
      default: return 'unknown';
    }
  }

  private recordGesture(gesture: G2Gesture, delivery: G2GestureDelivery): void {
    // Motion reports arrive ten a second while the IMU is open; recorded,
    // they push every tap and swipe out of the bounded ring.
    if (gesture === 'imu_report') return;
    this.diagnostics?.record({ type: 'gesture', gesture, delivery });
  }

  private recordAcceptedBrainstormAction(
    sourceName: GestureSource | undefined,
    gesture: 'tap' | 'double_press',
  ): void {
    const accepted = this.brainstorm?.consumeAcceptedAction();
    if (!accepted || sourceName === undefined) return;
    // The acceptance trace keeps a deliberately BOUNDED vocabulary of
    // actions (see G2DiagnosticTrace). An approval decision is not part of
    // it: the acceptance run never exercises one, and a receipt is not the
    // place to record what the wearer authorised a work session to do.
    if (
      accepted.action === 'approve_terminal'
      || accepted.action === 'deny_terminal'
      || accepted.action === 'cancel_terminal'
    ) return;
    this.diagnostics?.record({
      type: 'brainstorm_action',
      action: accepted.action,
      surface: accepted.surface,
      source: sourceName,
      gesture,
      delivery: 'sys',
    });
  }

  private handleSys(sys: NonNullable<EvenHubEvent['sysEvent']>): void {
    const type = sys.eventType ?? 0;
    const source = sys.eventSource ?? EventSourceType.TOUCH_EVENT_FORM_DUMMY_NULL;

    if (type === OsEventTypeList.IMU_DATA_REPORT && sys.imuData) {
      this.imu.ingest(sys.imuData);
      return;
    }

    // Lifecycle first: these are not wearer gestures and must never be
    // mistaken for confirms.
    switch (type) {
      case OsEventTypeList.FOREGROUND_ENTER_EVENT:
        if (this.pendingMicLongPress) {
          // OS contextual overlay, not an app foreground transition. Do not
          // pause location or repaint underneath the still-mounted HUD.
          // The press was the menu gesture (tap, then long press), not the
          // wearer talking: the hold it started is cancelled, its audio not
          // kept, and the release belongs to the menu now.
          this.endNoteHold('contextual-menu', 'cancel');
          this.pendingMicLongPress = false;
          this.contextualMenuOverlayOpen = true;
          this.lifecycle?.onContextualMenu?.(true);
          return;
        }
        this.lifecycle?.onForegroundEnter();
        return;
      case OsEventTypeList.FOREGROUND_EXIT_EVENT:
        if (this.contextualMenuOverlayOpen) {
          // Closing the contextual overlay is not app backgrounding. In
          // particular, do not leave LocationSessionManager marked background
          // after the wearer chose a menu action.
          this.contextualMenuOverlayOpen = false;
          this.lifecycle?.onContextualMenu?.(false);
          return;
        }
        // Gone to the background mid-press: its release will not come, and
        // the next return to JOAT must not read as the glasses' menu opening.
        this.endPress('background');
        this.lifecycle?.onForegroundExit();
        return;
      case OsEventTypeList.SYSTEM_EXIT_EVENT:
        this.lifecycle?.onExit('system');
        return;
      case OsEventTypeList.ABNORMAL_EXIT_EVENT:
        this.lifecycle?.onExit('abnormal');
        return;
    }

    if (this.contextualMenuOverlayOpen) return;

    // Confirmation is exclusive: no menu navigation, OS exit dialog, or
    // generic Mac input may occur behind it. Either built-in G2 temple is the
    // same source-agnostic TAP action. Double press is the
    // consistent Back gesture and refuses the pending write. Optional R1
    // input remains compatible, but the decision never depends on owning that
    // accessory.
    if (this.imu.isPending()) {
      if (
        type === OsEventTypeList.CLICK_EVENT &&
        (
          source === EventSourceType.TOUCH_EVENT_FROM_GLASSES_L ||
          source === EventSourceType.TOUCH_EVENT_FROM_RING ||
          source === EventSourceType.TOUCH_EVENT_FROM_GLASSES_R
        )
      ) {
        this.imu.confirmByTap();
      }
      return;
    }

    if (this.handlePromptInput(type)) return;
    if (this.handleNotificationCard(type)) return;
    if (this.handleAssistantConversation(type)) return;
    if (this.dismissAssistantOverlayFor(type)) return;
    if (this.dismissVisibleNotificationFor(type)) return;


    if (
      type === OsEventTypeList.CLICK_EVENT
      && this.navigation?.isActive()
      && this.isLocalTapSource(source)
    ) {
      this.navigation.handleClick();
      return;
    }

    // "tap" on the Brainstorm and Terminal footers means a single press on
    // EITHER temple or the ring: Even's input reference gives both temples
    // the same gesture set, and double press is JOAT's Back.
    // The gate used to admit right/ring only, so a left tap silently did
    // nothing while the footer promised it would (the audit's O3).
    if (
      type === OsEventTypeList.CLICK_EVENT
      && this.brainstorm?.isActive()
      && this.isLocalTapSource(source)
      && this.brainstorm.handleClick()
    ) {
      this.recordAcceptedBrainstormAction(GesturePolicy.sourceOf(source), 'tap');
      return;
    }

    // Checking Messages mid-Brainstorm no longer needs a plugin gesture: the
    // firmware contextual menu (tap, then long-press) is registered on every
    // page, including this one, and cancel stays on double-click.

    // On the resting overview a click drills into the tab the rail has
    // selected: notifications become a focused, scrollable page (the Mac
    // already serves them), and the other tabs expand from the content the
    // Mac last pushed. The next click opens the row the wearer stopped on.
    // Any visible card was already dismissed above, so this only fires on
    // a settled overview.
    if (
      type === OsEventTypeList.CLICK_EVENT
      && this.isLocalTapSource(source)
      && !this.menu.isOpen()
      && !this.navigation?.isActive()
      && !this.brainstorm?.isActive()
    ) {
      void this.menu.openActiveTab();
      return;
    }

    if (
      type === OsEventTypeList.CLICK_EVENT &&
      this.isLocalTapSource(source) &&
      this.menu.handleClick()
    ) {
      return;
    }

    // Long-press start/release are regular SDK list/text/sys events. They are
    // consumed before this method so this generic system route never erases
    // the microphone control or reinterprets its gesture vocabulary.

    // Relayed after local handling — the Mac may want the raw input too.
    this.link.send({ kind: 'input', event: { eventType: type, eventSource: source } });
  }

  private handleMenu(itemID: number): void {
    // The capability rows ARE the menu now; ids 1-5 below are the retired
    // internal controls, still routed because a host may show a cached menu.
    if (itemID >= CAPABILITY_MENU_ID_BASE) {
      void this.menu.openFromContextualMenu(itemID - CAPABILITY_MENU_ID_BASE);
      return;
    }
    if (itemID === QUICKNOTES_UNDO_MENU_ID) {
      // An open note's "Undo last input" (Even's QuickNote menu). Anywhere
      // else a cached row of it does nothing.
      this.menu.sendQuickNotesUndo?.();
      return;
    }
    switch (itemID) {
      case 1: // Repeat last reply — the Mac holds it
        this.link.send({ kind: 'input', event: { containerName: 'sysmenu', selectedIndex: 1 } });
        return;
      case 2: // Mute/unmute is LOCAL: a privacy control must not need the Mac
        this.toggleMic();
        return;
      case 3: // Cached reconnect configuration now belongs to the phone.
        void this.menu.openSettings();
        return;
      case 4: // Retired row id from a cached host menu; settings still opens.
        void this.menu.openSettings();
        return;
      case 5: // Non-destructive escape from visual safe-write outcomes.
        void this.menu.openPlanning();
        return;
      default:
        // Future firmware items: relay so the Mac can learn about them.
        this.link.send({ kind: 'input', event: { containerName: 'sysmenu', selectedIndex: itemID } });
    }
  }

  private dismissAssistantOverlayFor(eventType: number | undefined): boolean {
    if (!this.assistantOverlay?.hasAssistantOverlay()) return false;
    const type = eventType ?? OsEventTypeList.CLICK_EVENT;
    if (type === OsEventTypeList.SCROLL_TOP_EVENT) {
      this.assistantOverlay.scrollAssistantOverlay('up');
      return true;
    }
    if (type === OsEventTypeList.SCROLL_BOTTOM_EVENT) {
      this.assistantOverlay.scrollAssistantOverlay('down');
      return true;
    }
    if (
      type !== OsEventTypeList.CLICK_EVENT
      && type !== OsEventTypeList.DOUBLE_CLICK_EVENT
    ) return false;
    void this.assistantOverlay.dismissAssistantOverlay();
    return true;
  }

  private handleAssistantConversation(
    eventType: number | undefined,
  ): boolean {
    const owner = this.assistantOverlay;
    if (!owner?.hasAssistantConversation()) return false;
    const identity = owner.assistantConversationIdentity();
    if (!identity) return true;
    const type = eventType ?? OsEventTypeList.CLICK_EVENT;
    if (type === OsEventTypeList.SCROLL_TOP_EVENT) {
      owner.scrollAssistantConversation('up');
      return true;
    }
    if (type === OsEventTypeList.SCROLL_BOTTOM_EVENT) {
      owner.scrollAssistantConversation('down');
      return true;
    }
    if (type === OsEventTypeList.CLICK_EVENT) {
      if (owner.isAssistantConversationContinuePromptVisible()) {
        void owner.continueAssistantConversation().then((continued) => {
          if (!continued) return;
          this.link.sendConversationAction({
            kind: 'input',
            event: {
              containerName: 'assistant-conversation',
              selectedName: 'continue',
              conversationId: identity.conversationId,
              conversationRevision: identity.revision,
            },
          });
        });
      }
      return true;
    }
    if (type !== OsEventTypeList.DOUBLE_CLICK_EVENT) return false;
    void owner.requestAssistantConversationExit().then((result) => {
      if (result !== 'exited') return;
      this.link.sendConversationAction({
        kind: 'input',
        event: {
          containerName: 'assistant-conversation',
          selectedName: 'exit',
          conversationId: identity.conversationId,
          conversationRevision: identity.revision,
        },
      });
    });
    return true;
  }

  private isLocalTapSource(source: number): boolean {
    return source === EventSourceType.TOUCH_EVENT_FROM_GLASSES_L
      || source === EventSourceType.TOUCH_EVENT_FORM_DUMMY_NULL
      || source === EventSourceType.TOUCH_EVENT_FROM_RING
      || source === EventSourceType.TOUCH_EVENT_FROM_GLASSES_R;
  }

  /** A Home notification is a local, transient visual card. One tap removes
   * it before a hidden capability row or generic Mac input can activate. */
  private dismissVisibleNotificationFor(eventType: number | undefined): boolean {
    if (!this.assistantOverlay?.hasVisibleNotification()) return false;
    if ((eventType ?? OsEventTypeList.CLICK_EVENT) !== OsEventTypeList.CLICK_EVENT) return false;
    const cardId = this.assistantOverlay.visibleNotificationId?.();
    // A replacement card can own the model before its pixels are ready.
    // Consume that tap without dismissing unseen content. Older owners
    // without identity support retain their local-only dismissal behavior.
    if (this.assistantOverlay.visibleNotificationId && !cardId) return true;
    // Dismiss synchronously retires the identity before any subsequent tap.
    // Never queue this receipt for a replacement Mac session.
    void this.assistantOverlay.dismissVisibleNotification();
    if (cardId && this.link.linkState === 'connected') {
      this.link.send({ kind: 'input', event: { containerName: 'notification-dismiss', traceId: cardId } });
    }
    return true;
  }
}
