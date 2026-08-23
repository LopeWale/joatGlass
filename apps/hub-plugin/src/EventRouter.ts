import { OsEventTypeList, EventSourceType, type EvenHubEvent } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import type { MacLink } from './MacLink';
import type { AudioStreamer } from './AudioStreamer';
import type { ImuMonitor } from './ImuMonitor';

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
  ) {}

  attach(): void {
    this.client.track(this.client.require().onEvenHubEvent((event) => this.dispatch(event)));

    this.client.track(
      this.client.require().onDeviceStatusChanged((status) => {
        this.link.send({
          kind: 'device',
          battery: status.batteryLevel,
          wearing: status.isWearing,
          charging: status.isCharging,
          connect: status.connectType,
        });
      }),
    );
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
      // R1 ring tap is a clean confirm signal — no nod heuristics needed.
      if (
        sys.eventSource === EventSourceType.TOUCH_EVENT_FROM_RING &&
        sys.eventType === OsEventTypeList.CLICK_EVENT
      ) {
        this.imu.confirmByRing();
      }
      this.link.send({
        kind: 'input',
        event: { eventType: sys.eventType, eventSource: sys.eventSource },
      });
      return;
    }

    if (event.textEvent) {
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
