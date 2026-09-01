import { OsEventTypeList, EventSourceType, type EvenHubEvent } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import type { MacLink } from './MacLink';
import type { AudioStreamer } from './AudioStreamer';
import type { ImuMonitor } from './ImuMonitor';
import type { CapabilityMenu } from './CapabilityMenu';
import type { G2DiagnosticTrace, G2TraceEvent } from './G2DiagnosticTrace';
import {
  CAPABILITY_MENU_ID_BASE,
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
  /** Terminal: SYSTEM_EXIT (user confirmed) or ABNORMAL_EXIT (disconnect). */
  onExit(reason: 'system' | 'abnormal'): void;
}

export interface NavigationInputOwner {
  isActive(): boolean;
  handleClick(): boolean;
  handleScroll(direction: 'up' | 'down'): Promise<boolean>;
  /** Left-temple double press exits the current navigation session. */
  handleCancel(): boolean;
}

/** The live Brainstorm surface gets first claim on its explicit gestures. */
export interface BrainstormInputOwner {
  isActive(): boolean;
  handleClick(): boolean;
  /** Left-temple double press cancels an active Brainstorm round. */
  handleCancel(): boolean;
  /** A swipe during topic capture backs out of the armed capture; every
   * other state lets the swipe fall through to transcript scrolling. */
  handleSwipe(): boolean;
  /** True only when the Mac has explicitly armed the isolated diagnostic. */
  isAcceptanceArmed(): boolean;
  /** Right-temple/ring double press starts the fixed content-free test only
   * after that arm is visible on the current Brainstorm frame. */
  handleAcceptance(source: 'right' | 'ring'): boolean;
  /** Fixed receipt of a successfully handed-off action; never prompt content. */
  consumeAcceptedAction(): { action: BrainstormAction; surface: BrainstormHudScreen } | undefined;
}

/** A transient JOAT reply sits above the active feature, without destroying
 * it. Ordinary input dismisses that reply first; long-press remains the
 * always-available microphone privacy control. */
export interface AssistantOverlayInputOwner {
  hasAssistantOverlay(): boolean;
  dismissAssistantOverlay(): Promise<boolean>;
  hasVisibleNotification(): boolean;
  dismissVisibleNotification(): Promise<boolean>;
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
  private pendingMicLongPress = false;
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
  ) {}

  attach(): void {
    this.client.track(this.client.require().onEvenHubEvent((event) => this.dispatch(event)));
  }

  /**
   * Long-press → mute/unmute the glasses mic.
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
    if (type === OsEventTypeList.LONG_PRESS_RELEASE_EVENT) {
      const toggleMic = this.pendingMicLongPress && !this.contextualMenuOverlayOpen;
      this.pendingMicLongPress = false;
      if (toggleMic) {
        console.info(`[EventRouter] long-press via ${via} — toggling mic`);
        this.toggleMic();
      }
      // Consumed silently: one press is one toggle, and the release must not
      // reach the Mac as a phantom input.
      return true;
    }
    if (type !== OsEventTypeList.LONG_PRESS_EVENT) return false;
    this.pendingMicLongPress = !this.contextualMenuOverlayOpen;
    return true;
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
    if (event.audioEvent) {
      this.audio.ingest(event.audioEvent.audioPcm, event.audioEvent.speakerRole);
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
        } else if (
          eventType === OsEventTypeList.SCROLL_TOP_EVENT ||
          eventType === OsEventTypeList.SCROLL_BOTTOM_EVENT
        ) {
          this.imu.declineByTap();
        }
      }
      return;
    }

    // Contextual-menu clicks (SDK 0.0.14). Without this branch a declared
    // menu renders but every selection vanishes silently — the event has
    // its own field and matches none of the other branches.
    if (event.menuItemClickEvent) {
      // The simulator's `context_menu` automation input does not replay the
      // physical raw long-press. Preserve the same overlay boundary before
      // its following foreground-exit event reaches lifecycle consumers.
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
      this.handleSys(event.sysEvent);
      return;
    }

    if (event.textEvent) {
      this.recordGesture(this.classify(event.textEvent.eventType), 'text');
      // Long-press first: on a text-capture page the SDK delivers 9/10
      // HERE, and the relay below would forward it as an ordinary scroll.
      if (this.handleLongPress(event.textEvent.eventType, 'textEvent')) return;
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
      // the deliberate left double-press only.
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
      // protobuf encoder. If our own sidebar owns that surface, select its
      // highlighted row rather than consuming the input as a zero-distance
      // scroll. Closed menus still relay the event unchanged.
      if (
        eventType === OsEventTypeList.CLICK_EVENT
        && this.menu.handleClick()
      ) return;
      if (this.menu.handleScroll(eventType)) return;
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
    this.diagnostics?.record({ type: 'gesture', gesture, delivery });
  }

  private recordAcceptedBrainstormAction(
    source: number,
    gesture: 'tap' | 'double_press',
  ): void {
    const accepted = this.brainstorm?.consumeAcceptedAction();
    const sourceName = source === EventSourceType.TOUCH_EVENT_FROM_GLASSES_L
      ? 'left'
      : source === EventSourceType.TOUCH_EVENT_FROM_GLASSES_R
        ? 'right'
        : source === EventSourceType.TOUCH_EVENT_FROM_RING
          ? 'ring'
          : undefined;
    if (!accepted || sourceName === undefined) return;
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
          this.pendingMicLongPress = false;
          this.contextualMenuOverlayOpen = true;
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
          return;
        }
        this.lifecycle?.onForegroundExit();
        return;
      case OsEventTypeList.SYSTEM_EXIT_EVENT:
        this.lifecycle?.onExit('system');
        return;
      case OsEventTypeList.ABNORMAL_EXIT_EVENT:
        this.lifecycle?.onExit('abnormal');
        return;
    }

    // Confirmation is exclusive: no menu navigation, OS exit dialog, or
    // generic Mac input may occur behind it. Either built-in G2 temple is the
    // same source-agnostic TAP action. Left-temple double press is the
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
      } else if (
        type === OsEventTypeList.DOUBLE_CLICK_EVENT &&
        source === EventSourceType.TOUCH_EVENT_FROM_GLASSES_L
      ) {
        this.imu.declineByTap();
      }
      return;
    }

    if (this.dismissAssistantOverlayFor(type)) return;
    if (this.dismissVisibleNotificationFor(type)) return;

    if (type === OsEventTypeList.DOUBLE_CLICK_EVENT) {
      // Left temple: the transient capability sidebar. Consumed locally,
      // never relayed to the Mac or confused with the OS exit gesture.
      if (this.isBackSource(source)) {
        // Navigation owns the wearer surface as a single interaction. Do not
        // paint a sidebar over an active route while tap/swipe continue to
        // target the hidden navigation frame. The same deliberate gesture
        // becomes the visible navigation stop/recovery action.
        if (this.navigation?.isActive() && this.navigation.handleCancel()) return;
        // A live Brainstorm owns the same stop gesture. This check is after
        // navigation so one active surface always has deterministic priority.
        if (this.brainstorm?.isActive() && this.brainstorm.handleCancel()) {
          this.recordAcceptedBrainstormAction(source, 'double_press');
          return;
        }
        void this.menu.toggle();
        return;
      }
      const acceptanceSource = source === EventSourceType.TOUCH_EVENT_FROM_GLASSES_R
        ? 'right'
        : source === EventSourceType.TOUCH_EVENT_FROM_RING
          ? 'ring'
          : undefined;
      if (
        acceptanceSource !== undefined
        && this.brainstorm?.isActive()
        && this.brainstorm.isAcceptanceArmed()
        && this.brainstorm.handleAcceptance(acceptanceSource)
      ) {
        this.recordAcceptedBrainstormAction(source, 'double_press');
        return;
      }
      // Anything else: the SDK's own exit dialog. The platform expects every
      // app to offer an exit gesture, and double-tap is the canonical one.
      // Mode 1 shows a confirmation — do NOT tear down here, the wearer can
      // still cancel. Cleanup happens in onExit when SYSTEM_EXIT arrives.
      void this.client.require().shutDownPageContainer(1);
      return;
    }

    if (
      type === OsEventTypeList.CLICK_EVENT
      && this.navigation?.isActive()
      && this.isLocalTapSource(source)
    ) {
      this.navigation.handleClick();
      return;
    }

    if (
      type === OsEventTypeList.CLICK_EVENT
      && this.brainstorm?.isActive()
      && (
        source === EventSourceType.TOUCH_EVENT_FROM_RING
        || source === EventSourceType.TOUCH_EVENT_FROM_GLASSES_R
      )
      && this.brainstorm.handleClick()
    ) {
      this.recordAcceptedBrainstormAction(source, 'tap');
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
    switch (itemID) {
      case 1: // Repeat last reply — the Mac holds it
        this.link.send({ kind: 'input', event: { containerName: 'sysmenu', selectedIndex: 1 } });
        return;
      case 2: // Mute/unmute is LOCAL: a privacy control must not need the Mac
        this.toggleMic();
        return;
      case 3: // Global Refresh: re-open the real bridge/gateway link.
        this.link.connect();
        return;
      case 4: // Five sidebar rows are spoken for; settings lives here.
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
    if (
      type !== OsEventTypeList.CLICK_EVENT
      && type !== OsEventTypeList.DOUBLE_CLICK_EVENT
      && type !== OsEventTypeList.SCROLL_TOP_EVENT
      && type !== OsEventTypeList.SCROLL_BOTTOM_EVENT
    ) return false;
    void this.assistantOverlay.dismissAssistantOverlay();
    return true;
  }

  /** Firmware/simulator builds that omit the physical source still expose a
   * real local gesture. Treat only the dummy source as the compatibility
   * fallback; explicit right/ring double presses retain their OS/Brainstorm
   * meaning. */
  private isBackSource(source: number): boolean {
    return source === EventSourceType.TOUCH_EVENT_FROM_GLASSES_L
      || source === EventSourceType.TOUCH_EVENT_FORM_DUMMY_NULL;
  }

  private isLocalTapSource(source: number): boolean {
    return this.isBackSource(source)
      || source === EventSourceType.TOUCH_EVENT_FROM_RING
      || source === EventSourceType.TOUCH_EVENT_FROM_GLASSES_R;
  }

  /** A Home notification is a local, transient visual card. One tap removes
   * it before a hidden capability row or generic Mac input can activate. */
  private dismissVisibleNotificationFor(eventType: number | undefined): boolean {
    if (!this.assistantOverlay?.hasVisibleNotification()) return false;
    if ((eventType ?? OsEventTypeList.CLICK_EVENT) !== OsEventTypeList.CLICK_EVENT) return false;
    void this.assistantOverlay.dismissVisibleNotification();
    return true;
  }
}
