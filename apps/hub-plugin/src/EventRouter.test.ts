import { describe, expect, it, vi } from 'vitest';
import { EventSourceType, OsEventTypeList, type EvenHubEvent } from '@evenrealities/even_hub_sdk';
import { EventRouter } from './EventRouter';
import type { BridgeClient } from './BridgeClient';
import type { MacLink } from './MacLink';
import type { AudioStreamer } from './AudioStreamer';
import type { ImuMonitor } from './ImuMonitor';
import type { SettingsMenu } from './SettingsMenu';
import type { UplinkMessage } from './types';

function make(menuConsumesLists = false) {
  let dispatch: (e: EvenHubEvent) => void = () => {};
  const sent: UplinkMessage[] = [];
  const audio = { ingest: vi.fn() };
  const imu = { ingest: vi.fn(), confirmByRing: vi.fn() };
  const menu = { toggle: vi.fn(), handleList: vi.fn(() => menuConsumesLists) };
  const client = {
    track: (u: () => void) => u,
    require: () => ({
      onEvenHubEvent: (cb: (e: EvenHubEvent) => void) => {
        dispatch = cb;
        return () => {};
      },
    }),
  };
  new EventRouter(
    client as unknown as BridgeClient,
    { send: (m: UplinkMessage) => sent.push(m) } as unknown as MacLink,
    audio as unknown as AudioStreamer,
    imu as unknown as ImuMonitor,
    menu as unknown as SettingsMenu,
  ).attach();
  return { fire: (e: unknown) => dispatch(e as EvenHubEvent), sent, audio, imu, menu };
}

describe('EventRouter', () => {
  it('audio frames go to the streamer', () => {
    const { fire, audio } = make();
    fire({ audioEvent: { audioPcm: [1, 2, 3] } });
    expect(audio.ingest).toHaveBeenCalledWith([1, 2, 3]);
  });

  it('IMU reports go to the monitor and are not relayed', () => {
    const { fire, imu, sent } = make();
    fire({ sysEvent: { eventType: OsEventTypeList.IMU_DATA_REPORT, imuData: { y: 0.1 } } });
    expect(imu.ingest).toHaveBeenCalled();
    expect(sent).toEqual([]);
  });

  it('left-temple double-tap toggles the menu and is consumed', () => {
    const { fire, menu, sent } = make();
    fire({
      sysEvent: {
        eventType: OsEventTypeList.DOUBLE_CLICK_EVENT,
        eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_L,
      },
    });
    expect(menu.toggle).toHaveBeenCalledTimes(1);
    expect(sent).toEqual([]);
  });

  it('right-temple double-tap is NOT the settings gesture — it relays', () => {
    const { fire, menu, sent } = make();
    fire({
      sysEvent: {
        eventType: OsEventTypeList.DOUBLE_CLICK_EVENT,
        eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_R,
      },
    });
    expect(menu.toggle).not.toHaveBeenCalled();
    expect(sent).toHaveLength(1);
  });

  it('ring click confirms and still relays as input', () => {
    const { fire, imu, sent } = make();
    fire({
      sysEvent: {
        eventType: OsEventTypeList.CLICK_EVENT,
        eventSource: EventSourceType.TOUCH_EVENT_FROM_RING,
      },
    });
    expect(imu.confirmByRing).toHaveBeenCalledTimes(1);
    expect(sent).toHaveLength(1);
  });

  it('list events: consumed by an open menu, relayed otherwise', () => {
    const open = make(true);
    open.fire({ listEvent: { containerID: 3, eventType: OsEventTypeList.CLICK_EVENT } });
    expect(open.sent).toEqual([]);

    const closed = make(false);
    closed.fire({ listEvent: { containerID: 3, eventType: OsEventTypeList.CLICK_EVENT } });
    expect(closed.sent).toHaveLength(1);
    expect(closed.sent[0]?.kind).toBe('input');
  });
});
