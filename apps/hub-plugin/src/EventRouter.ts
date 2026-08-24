import { OsEventTypeList, EventSourceType, type EvenHubEvent } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import type { MacLink } from './MacLink';
import type { AudioStreamer } from './AudioStreamer';
import type { ImuMonitor } from './ImuMonitor';
import type { SettingsMenu } from './SettingsMenu';

/**
 * Single onEvenHubEvent subscription fanned out to typed handlers.
 *
 * One subscription, not four: the SDK delivers every event class through the
 * same callback, and multiple subscriptions make teardown error-prone.
 */
export class EventRouter {
  constructor(
    private readonly client: BridgeClient,
    private readonly link: MacLink,
    private readonly audio: AudioStreamer,
    private readonly imu: ImuMonitor,
    private readonly menu: SettingsMenu,
  ) {}

  attach(): void {
    this.client.track(this.client.require().onEvenHubEvent((event) => this.dispatch(event)));
  }

  private dispatch(event: EvenHubEvent): void {
    if (event.audioEvent) {
      this.audio.ingest(event.audioEvent.audioPcm);
    }

    if (event.sysEvent) {
      const sys = event.sysEvent;
      if (sys.eventType === OsEventTypeList.IMU_DATA_REPORT && sys.imuData) {
        this.imu.ingest(sys.imuData);
        return;
      }
      // Left-temple double-tap toggles the on-glasses settings menu —
      // consumed locally, never relayed. Right temple + ring stay Mac-facing.
      if (
        sys.eventSource === EventSourceType.TOUCH_EVENT_FROM_GLASSES_L &&
        sys.eventType === OsEventTypeList.DOUBLE_CLICK_EVENT
      ) {
        this.menu.toggle();
        return;
      }
      // Deliberate taps are clean confirm signals — no nod heuristics needed.
      // Right temple matters most: a wearer with no R1 ring has no other
      // trustworthy way to confirm, since the nod thresholds are uncalibrated.
      // Still relayed uplink afterwards — the Mac may want the raw input too.
      if (
        (sys.eventSource === EventSourceType.TOUCH_EVENT_FROM_RING ||
          sys.eventSource === EventSourceType.TOUCH_EVENT_FROM_GLASSES_R) &&
        sys.eventType === OsEventTypeList.CLICK_EVENT
      ) {
        this.imu.confirmByTap();
      }
      this.link.send({
        kind: 'input',
        event: { eventType: sys.eventType, eventSource: sys.eventSource },
      });
      return;
    }

    if (event.textEvent) {
      // Hardware-uncertainty hedge. Text_ItemEvent carries no eventSource, so
      // if the host delivers a temple tap on the event-capture container as a
      // textEvent rather than a sysEvent, the sysEvent branch above never
      // fires and a ringless wearer has NO confirm path at all.
      //
      // Scoped tight so this cannot become an accidental-confirm bug: only a
      // CLICK, only while a confirm window is already armed, and the prompt is
      // on screen when it is. Verify which shape the G2 actually emits with
      // the event dump in docs/TESTING.md, then delete whichever branch is
      // dead rather than leaving both in place.
      if (event.textEvent.eventType === OsEventTypeList.CLICK_EVENT) {
        this.imu.confirmByTap();
      }
      this.link.send({
        kind: 'input',
        event: {
          containerId: event.textEvent.containerID,
          containerName: event.textEvent.containerName,
          eventType: event.textEvent.eventType,
        },
      });
      return;
    }

    if (event.listEvent) {
      // Settings menu open → selection is a local concern, not Mac input.
      if (this.menu.handleList(event.listEvent)) return;
      this.link.send({
        kind: 'input',
        event: {
          containerId: event.listEvent.containerID,
          containerName: event.listEvent.containerName,
          eventType: event.listEvent.eventType,
          selectedIndex: event.listEvent.currentSelectItemIndex,
          selectedName: event.listEvent.currentSelectItemName,
        },
      });
    }
  }
}
