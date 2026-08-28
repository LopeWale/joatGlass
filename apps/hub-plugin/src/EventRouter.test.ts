import { describe, expect, it, vi } from 'vitest';
import { EventSourceType, OsEventTypeList, type EvenHubEvent } from '@evenrealities/even_hub_sdk';
import { EventRouter, type LifecycleHandler } from './EventRouter';
import type { BridgeClient } from './BridgeClient';
import type { MacLink } from './MacLink';
import type { AudioStreamer } from './AudioStreamer';
import type { ImuMonitor } from './ImuMonitor';
import type { CapabilityMenu } from './CapabilityMenu';
import type { UplinkMessage } from './types';

function make(menuConsumesLists = false) {
  let dispatch: (e: EvenHubEvent) => void = () => {};
  const sent: UplinkMessage[] = [];
  const audio = {
    ingest: vi.fn(),
    isOpen: true,
    lastSource: 0,
    stop: vi.fn(async () => {}),
    start: vi.fn(async () => true),
  };
  const imu = {
    ingest: vi.fn(),
    isPending: vi.fn(() => false),
    confirmByTap: vi.fn(),
    declineByTap: vi.fn(() => false),
  };
  const menu = {
    toggle: vi.fn(),
    handleList: vi.fn(() => menuConsumesLists),
    handleScroll: vi.fn(() => menuConsumesLists),
    handleClick: vi.fn(() => menuConsumesLists),
  };
  const shutDownPageContainer = vi.fn(async (_mode: number) => true);
  const lifecycle: LifecycleHandler = {
    onForegroundEnter: vi.fn(),
    onForegroundExit: vi.fn(),
    onExit: vi.fn(),
  };
  const client = {
    track: (u: () => void) => u,
    require: () => ({
      onEvenHubEvent: (cb: (e: EvenHubEvent) => void) => {
        dispatch = cb;
        return () => {};
      },
      shutDownPageContainer,
    }),
  };
  new EventRouter(
    client as unknown as BridgeClient,
    { send: (m: UplinkMessage) => sent.push(m) } as unknown as MacLink,
    audio as unknown as AudioStreamer,
    imu as unknown as ImuMonitor,
    menu as unknown as CapabilityMenu,
    lifecycle,
  ).attach();
  return {
    fire: (e: unknown) => dispatch(e as EvenHubEvent),
    sent,
    audio,
    imu,
    menu,
    lifecycle,
    shutDownPageContainer,
  };
}

/**
 * The protobuf transport omits zero-valued fields, so a single press arrives
 * with eventType ABSENT rather than 0. Every click test below omits it
 * deliberately — that is the real wire shape, and asserting against an
 * explicit 0 would pass while the device fails.
 */
const click = (eventSource: number) => ({ sysEvent: { eventSource } });

describe('EventRouter — audio and IMU', () => {
  it('audio frames go to the streamer', () => {
    const { fire, audio } = make();
    fire({ audioEvent: { audioPcm: [1, 2, 3] } });
    expect(audio.ingest).toHaveBeenCalledWith([1, 2, 3], undefined);
  });

  it('IMU reports go to the monitor and are not relayed', () => {
    const { fire, imu, sent } = make();
    fire({ sysEvent: { eventType: OsEventTypeList.IMU_DATA_REPORT, imuData: { y: 0.1 } } });
    expect(imu.ingest).toHaveBeenCalled();
    expect(sent).toEqual([]);
  });
});

describe('EventRouter — confirm gestures', () => {
  it('right-temple press confirms, with eventType omitted as the device sends it', () => {
    const { fire, imu, sent } = make();
    imu.isPending.mockReturnValue(true);
    fire(click(EventSourceType.TOUCH_EVENT_FROM_GLASSES_R));
    expect(imu.confirmByTap).toHaveBeenCalledTimes(1);
    expect(sent).toEqual([]);
  });

  it('ring press confirms', () => {
    const { fire, imu } = make();
    imu.isPending.mockReturnValue(true);
    fire(click(EventSourceType.TOUCH_EVENT_FROM_RING));
    expect(imu.confirmByTap).toHaveBeenCalledTimes(1);
  });

  it('a non-decision temple press remains ordinary input', () => {
    const { fire, imu, sent } = make();
    fire(click(EventSourceType.TOUCH_EVENT_FROM_GLASSES_L));
    expect(imu.confirmByTap).not.toHaveBeenCalled();
    expect(imu.declineByTap).not.toHaveBeenCalled();
    expect(sent).toHaveLength(1);
  });

  it.each([
    EventSourceType.TOUCH_EVENT_FROM_GLASSES_L,
    EventSourceType.TOUCH_EVENT_FROM_GLASSES_R,
  ])('G2 temple source %s confirms exactly once and is consumed', (eventSource) => {
    const { fire, imu, sent } = make();
    imu.isPending.mockReturnValue(true);

    fire(click(eventSource));

    expect(imu.confirmByTap).toHaveBeenCalledTimes(1);
    expect(imu.declineByTap).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
  });

  it.each([
    OsEventTypeList.SCROLL_TOP_EVENT,
    OsEventTypeList.SCROLL_BOTTOM_EVENT,
  ])('a G2 swipe (%s) cancels and consumes an active confirmation', (eventType) => {
    const { fire, imu, sent } = make();
    imu.isPending.mockReturnValue(true);

    fire({ textEvent: { containerID: 2, eventType } });

    expect(imu.declineByTap).toHaveBeenCalledTimes(1);
    expect(imu.confirmByTap).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
  });

  it('accepts the capturing text container click shape while a confirmation is active', () => {
    const { fire, imu, sent } = make();
    imu.isPending.mockReturnValue(true);

    // CLICK_EVENT is protobuf zero and is omitted on the wire.
    fire({ textEvent: { containerID: 2 } });

    expect(imu.confirmByTap).toHaveBeenCalledTimes(1);
    expect(imu.declineByTap).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
  });

  it('never approves an active confirmation from an unknown sysEvent source', () => {
    const { fire, imu, sent } = make();
    imu.isPending.mockReturnValue(true);

    fire(click(EventSourceType.TOUCH_EVENT_FORM_DUMMY_NULL));

    expect(imu.confirmByTap).not.toHaveBeenCalled();
    expect(imu.declineByTap).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
  });

  it('consumes long press without toggling the mic behind an active confirmation', () => {
    const { fire, imu, audio, sent } = make();
    imu.isPending.mockReturnValue(true);

    fire({ sysEvent: { eventType: OsEventTypeList.LONG_PRESS_EVENT } });

    expect(audio.stop).not.toHaveBeenCalled();
    expect(audio.start).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
  });

  it('an explicit eventType 0 also confirms, for hosts that do send it', () => {
    const { fire, imu } = make();
    imu.isPending.mockReturnValue(true);
    fire({
      sysEvent: {
        eventType: OsEventTypeList.CLICK_EVENT,
        eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_R,
      },
    });
    expect(imu.confirmByTap).toHaveBeenCalledTimes(1);
  });
});

describe('EventRouter — double press', () => {
  it('does not open a menu or OS dialog behind an active confirmation', () => {
    const { fire, imu, menu, sent, shutDownPageContainer } = make();
    imu.isPending.mockReturnValue(true);

    fire({
      sysEvent: {
        eventType: OsEventTypeList.DOUBLE_CLICK_EVENT,
        eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_L,
      },
    });
    fire({
      sysEvent: {
        eventType: OsEventTypeList.DOUBLE_CLICK_EVENT,
        eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_R,
      },
    });

    expect(menu.toggle).not.toHaveBeenCalled();
    expect(shutDownPageContainer).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
  });

  it('left temple toggles the settings menu and is consumed', () => {
    const { fire, menu, sent, shutDownPageContainer } = make();
    fire({
      sysEvent: {
        eventType: OsEventTypeList.DOUBLE_CLICK_EVENT,
        eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_L,
      },
    });
    expect(menu.toggle).toHaveBeenCalledTimes(1);
    expect(shutDownPageContainer).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
  });

  it('right temple opens the system exit dialog, not an immediate exit', () => {
    const { fire, menu, shutDownPageContainer, lifecycle } = make();
    fire({
      sysEvent: {
        eventType: OsEventTypeList.DOUBLE_CLICK_EVENT,
        eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_R,
      },
    });
    expect(menu.toggle).not.toHaveBeenCalled();
    // Mode 1 = confirmation dialog. Mode 0 would close under the wearer.
    expect(shutDownPageContainer).toHaveBeenCalledWith(1);
    // Teardown waits for SYSTEM_EXIT — the wearer can still cancel.
    expect(lifecycle.onExit).not.toHaveBeenCalled();
  });
});

describe('EventRouter — lifecycle', () => {
  it('foreground enter and exit reach the handler', () => {
    const { fire, lifecycle } = make();
    fire({ sysEvent: { eventType: OsEventTypeList.FOREGROUND_ENTER_EVENT } });
    fire({ sysEvent: { eventType: OsEventTypeList.FOREGROUND_EXIT_EVENT } });
    expect(lifecycle.onForegroundEnter).toHaveBeenCalledTimes(1);
    expect(lifecycle.onForegroundExit).toHaveBeenCalledTimes(1);
  });

  it('system exit tears down', () => {
    const { fire, lifecycle } = make();
    fire({ sysEvent: { eventType: OsEventTypeList.SYSTEM_EXIT_EVENT } });
    expect(lifecycle.onExit).toHaveBeenCalledWith('system');
  });

  it('abnormal exit tears down', () => {
    const { fire, lifecycle } = make();
    fire({ sysEvent: { eventType: OsEventTypeList.ABNORMAL_EXIT_EVENT } });
    expect(lifecycle.onExit).toHaveBeenCalledWith('abnormal');
  });

  it('lifecycle events never reach the confirm gate', () => {
    const { fire, imu } = make();
    for (const eventType of [
      OsEventTypeList.FOREGROUND_ENTER_EVENT,
      OsEventTypeList.FOREGROUND_EXIT_EVENT,
      OsEventTypeList.SYSTEM_EXIT_EVENT,
      OsEventTypeList.ABNORMAL_EXIT_EVENT,
    ]) {
      fire({ sysEvent: { eventType } });
    }
    expect(imu.confirmByTap).not.toHaveBeenCalled();
  });
});

describe('EventRouter — text and list', () => {
  it('scrolls arrive as textEvent and relay', () => {
    const { fire, sent, imu } = make();
    fire({ textEvent: { containerID: 2, eventType: OsEventTypeList.SCROLL_TOP_EVENT } });
    expect(sent).toHaveLength(1);
    // Clicks never arrive as textEvent, so this must not confirm.
    expect(imu.confirmByTap).not.toHaveBeenCalled();
  });

  it('custom-surface scrolls are consumed by the open capability menu', () => {
    const { fire, sent, menu } = make(true);
    fire({ textEvent: { containerID: 2, eventType: OsEventTypeList.SCROLL_BOTTOM_EVENT } });
    expect(menu.handleScroll).toHaveBeenCalledWith(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    expect(sent).toEqual([]);
  });

  it('right click selects the custom menu row instead of confirming or relaying', () => {
    const { fire, sent, menu, imu } = make(true);
    fire(click(EventSourceType.TOUCH_EVENT_FROM_GLASSES_R));
    expect(menu.handleClick).toHaveBeenCalledTimes(1);
    expect(imu.confirmByTap).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
  });

  it('list events: consumed by an open menu, relayed otherwise', () => {
    const open = make(true);
    open.fire({ listEvent: { containerID: 3 } });
    expect(open.sent).toEqual([]);

    const closed = make(false);
    closed.fire({ listEvent: { containerID: 3 } });
    expect(closed.sent).toHaveLength(1);
    expect(closed.sent[0]?.kind).toBe('input');
  });

  it('a first-item selection relays index 0, not undefined', () => {
    // currentSelectItemIndex 0 is omitted by protobuf; without `?? 0` the Mac
    // would receive undefined and mis-handle the top row.
    const { fire, sent } = make(false);
    fire({ listEvent: { containerID: 3, currentSelectItemName: 'first' } });
    expect(sent[0]).toMatchObject({ kind: 'input', event: { selectedIndex: 0 } });
  });
});

describe('EventRouter — long-press mutes the mic', () => {
  /**
   * An earlier build ceded long-press to the glasses OS, believing a
   * declared menuObject made the OS open its contextual menu on that
   * gesture. The SDK README says the opposite twice — long press and
   * release "remain regular list/text/sys events", "delivered inside the
   * matching list, text, or system event" — there is no bridge verb that
   * opens a menu, and on hardware the gesture did nothing at all.
   *
   * It must therefore be handled in EVERY event class, and BEFORE each
   * branch's relay: on a text-capture page the textEvent relay would
   * otherwise forward it to the Mac as an ordinary scroll.
   */
  const LONG_PRESS = OsEventTypeList.LONG_PRESS_EVENT;
  const RELEASE = OsEventTypeList.LONG_PRESS_RELEASE_EVENT;

  it('toggles the mic from a sysEvent', () => {
    const r = make();
    r.fire({ sysEvent: { eventType: LONG_PRESS, eventSource: 1 } });
    expect(r.audio.stop).toHaveBeenCalledTimes(1);
  });

  it('toggles the mic from a textEvent — the relay must not swallow it', () => {
    const r = make();
    r.fire({ textEvent: { eventType: LONG_PRESS, containerName: 'main' } });
    expect(r.audio.stop).toHaveBeenCalledTimes(1);
    expect(r.sent).toEqual([]); // not relayed to the Mac as a scroll
  });

  it('toggles the mic from a listEvent', () => {
    const r = make();
    r.fire({ listEvent: { eventType: LONG_PRESS } });
    expect(r.audio.stop).toHaveBeenCalledTimes(1);
    expect(r.sent).toEqual([]);
  });

  it('un-mutes when the mic is off, reusing lastSource', () => {
    const r = make();
    r.audio.isOpen = false;
    r.fire({ sysEvent: { eventType: LONG_PRESS, eventSource: 1 } });
    expect(r.audio.start).toHaveBeenCalledWith(r.audio.lastSource);
  });

  it('release is consumed silently — one press, one toggle, no phantom input', () => {
    const r = make();
    r.fire({ sysEvent: { eventType: RELEASE, eventSource: 1 } });
    expect(r.audio.stop).not.toHaveBeenCalled();
    expect(r.audio.start).not.toHaveBeenCalled();
    expect(r.sent).toEqual([]);
  });

  it('the contextual-menu mute item takes the SAME path as the gesture', () => {
    const r = make();
    r.fire({ menuItemClickEvent: { itemID: 2 } });
    expect(r.audio.stop).toHaveBeenCalledTimes(1);
  });
});
