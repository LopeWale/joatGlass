import { OsEventTypeList, EventSourceType, type EvenHubEvent } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import type { MacLink } from './MacLink';
import type { AudioStreamer } from './AudioStreamer';
import type { ImuMonitor } from './ImuMonitor';
import type { CapabilityMenu } from './CapabilityMenu';

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
  constructor(
    private readonly client: BridgeClient,
    private readonly link: MacLink,
    private readonly audio: AudioStreamer,
    private readonly imu: ImuMonitor,
    private readonly menu: CapabilityMenu,
    private readonly lifecycle?: LifecycleHandler,
  ) {}

  attach(): void {
    this.client.track(this.client.require().onEvenHubEvent((event) => this.dispatch(event)));
  }

  /**
   * Long-press → mute/unmute the glasses mic.
   *
   * Checked in EVERY event class because the SDK README is explicit that
   * 9/10 are "delivered inside the matching list, text, or system event
   * rather than as separate top-level events" (README:392) and that they
   * "remain regular list/text/sys events" (README:433). An earlier build
   * ceded this gesture to the glasses OS on the theory that a declared
   * menuObject makes long-press open the contextual menu — the SDK says
   * the opposite, there is no bridge verb that opens a menu, and on
   * hardware the gesture simply did nothing.
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
      // Consumed silently: one press is one toggle, and the release must
      // not reach the Mac as a phantom input.
      return true;
    }
    if (type !== OsEventTypeList.LONG_PRESS_EVENT) return false;
    // Logged with its event class: if firmware never delivers long-press,
    // this line's ABSENCE is the proof, instead of a third guess.
    console.info(`[EventRouter] long-press via ${via} — toggling mic`);
    this.toggleMic();
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
      this.handleMenu(event.menuItemClickEvent.itemID ?? 0);
      return;
    }

    if (event.sysEvent) {
      // Confirmation owns long press too; it must not toggle the microphone
      // behind a decision or make the visible TAP/SWIPE contract incomplete.
      if (!this.imu.isPending() && this.handleLongPress(event.sysEvent.eventType, 'sysEvent')) {
        return;
      }
      this.handleSys(event.sysEvent);
      return;
    }

    if (event.textEvent) {
      // Long-press first: on a text-capture page the SDK delivers 9/10
      // HERE, and the relay below would forward it as an ordinary scroll.
      if (this.handleLongPress(event.textEvent.eventType, 'textEvent')) return;
      if (this.menu.handleScroll(event.textEvent.eventType ?? 0)) return;
      // Scrolls and clicks: clicks on a text container arrive as sysEvent,
      // so what reaches this line is a scroll.
      this.link.send({
        kind: 'input',
        event: {
          containerId: event.textEvent.containerID,
          containerName: event.textEvent.containerName,
          eventType: event.textEvent.eventType ?? 0,
        },
      });
      return;
    }

    if (event.listEvent) {
      if (this.handleLongPress(event.listEvent.eventType, 'listEvent')) return;
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
        this.lifecycle?.onForegroundEnter();
        return;
      case OsEventTypeList.FOREGROUND_EXIT_EVENT:
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
    // same source-agnostic TAP action. Optional R1 input remains compatible,
    // but the decision never depends on owning that accessory.
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

    if (type === OsEventTypeList.DOUBLE_CLICK_EVENT) {
      // Left temple: the transient capability sidebar. Consumed locally,
      // never relayed to the Mac or confused with the OS exit gesture.
      if (source === EventSourceType.TOUCH_EVENT_FROM_GLASSES_L) {
        void this.menu.toggle();
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
      type === OsEventTypeList.CLICK_EVENT &&
      (source === EventSourceType.TOUCH_EVENT_FROM_RING ||
        source === EventSourceType.TOUCH_EVENT_FROM_GLASSES_R) &&
      this.menu.handleClick()
    ) {
      return;
    }

    // Long-press belongs to the glasses OS: with a menuObject declared it
    // opens the contextual menu (Repeat / Mute / Reconnect). Our own
    // long-press handler competed with that — double-firing where the OS
    // passed the event through, dead where the OS consumed it — so repeat
    // now lives ONLY as contextual-menu item 1.

    // Relayed after local handling — the Mac may want the raw input too.
    this.link.send({ kind: 'input', event: { eventType: type, eventSource: source } });
  }

  private handleMenu(itemID: number): void {
    switch (itemID) {
      case 1: // Repeat last reply — the Mac holds it
        this.link.send({ kind: 'input', event: { containerName: 'sysmenu', selectedIndex: 1 } });
        return;
      case 2: // Mute/unmute is LOCAL: a privacy control must not need the Mac
        this.toggleMic();
        return;
      case 3: // Re-dial the brain
        this.link.connect();
        return;
      default:
        // Future firmware items: relay so the Mac can learn about them.
        this.link.send({ kind: 'input', event: { containerName: 'sysmenu', selectedIndex: itemID } });
    }
  }
}
