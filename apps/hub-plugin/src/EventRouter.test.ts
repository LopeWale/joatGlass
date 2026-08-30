import { describe, expect, it, vi } from 'vitest';
import { EventSourceType, OsEventTypeList, type EvenHubEvent } from '@evenrealities/even_hub_sdk';
import { EventRouter, type LifecycleHandler } from './EventRouter';
import type { BridgeClient } from './BridgeClient';
import type { MacLink } from './MacLink';
import type { AudioStreamer } from './AudioStreamer';
import type { ImuMonitor } from './ImuMonitor';
import { CapabilityMenu } from './CapabilityMenu';
import { Settings } from './Settings';
import type { HudPresenter } from './HudPresenter';
import type { UplinkMessage } from './types';

function make(
  menuConsumesLists = false,
  diagnostics?: { record: (event: unknown) => void },
  navigationActive = false,
  brainstormActive = false,
  assistantOverlayVisible = false,
  notificationVisible = false,
  brainstormAcceptanceArmed = false,
) {
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
    openSettings: vi.fn(),
  };
  const navigation = {
    isActive: vi.fn(() => navigationActive),
    handleClick: vi.fn(() => navigationActive),
    handleScroll: vi.fn(async () => navigationActive),
    handleCancel: vi.fn(() => navigationActive),
  };
  let acceptedBrainstormAction = 'show_terminal' as 'show_terminal' | 'begin_acceptance';
  const brainstorm = {
    isActive: vi.fn(() => brainstormActive),
    isAcceptanceArmed: vi.fn(() => brainstormAcceptanceArmed),
    handleClick: vi.fn(() => brainstormActive),
    handleCancel: vi.fn(() => brainstormActive),
    handleAcceptance: vi.fn(() => {
      acceptedBrainstormAction = 'begin_acceptance';
      return brainstormActive;
    }),
    consumeAcceptedAction: vi.fn(() => brainstormActive ? {
      action: acceptedBrainstormAction,
      surface: 'brainstorm' as const,
    } : undefined),
  };
  const assistantOverlay = {
    hasAssistantOverlay: vi.fn(() => assistantOverlayVisible),
    dismissAssistantOverlay: vi.fn(async () => true),
    hasVisibleNotification: vi.fn(() => notificationVisible),
    dismissVisibleNotification: vi.fn(async () => true),
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
    diagnostics,
    navigation,
    brainstorm,
    assistantOverlay,
  ).attach();
  return {
    fire: (e: unknown) => dispatch(e as EvenHubEvent),
    sent,
    audio,
    imu,
    menu,
    navigation,
    brainstorm,
    assistantOverlay,
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
  it('ignores the SDK startup source announcement instead of inventing a left tap', () => {
    const events: unknown[] = [];
    const { fire, assistantOverlay, sent } = make(
      false, { record: (event) => events.push(event) }, false, false, true,
    );

    fire({ sysEvent: { eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_L } });

    expect(assistantOverlay.dismissAssistantOverlay).not.toHaveBeenCalled();
    expect(events).toEqual([]);
    expect(sent).toEqual([]);
  });

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

  it('uses a source-less compatible G2 double press for local Back, not OS exit', () => {
    const { fire, menu, sent, shutDownPageContainer } = make();

    fire({
      sysEvent: {
        eventType: OsEventTypeList.DOUBLE_CLICK_EVENT,
        eventSource: EventSourceType.TOUCH_EVENT_FORM_DUMMY_NULL,
      },
    });

    expect(menu.toggle).toHaveBeenCalledTimes(1);
    expect(shutDownPageContainer).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
  });

  it('lets a Home notification own a source-less tap before menu selection', () => {
    const { fire, assistantOverlay, menu, sent } = make(
      false, undefined, false, false, false, true,
    );

    fire(click(EventSourceType.TOUCH_EVENT_FORM_DUMMY_NULL));

    expect(assistantOverlay.dismissVisibleNotification).toHaveBeenCalledTimes(1);
    expect(menu.handleClick).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
  });

  it('left temple stops active navigation instead of opening a hidden-overlay sidebar', () => {
    const { fire, menu, navigation, sent } = make(false, undefined, true);
    fire({
      sysEvent: {
        eventType: OsEventTypeList.DOUBLE_CLICK_EVENT,
        eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_L,
      },
    });

    expect(navigation.handleCancel).toHaveBeenCalledTimes(1);
    expect(menu.toggle).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
  });

  it('left temple cancels a live Brainstorm instead of opening a hidden sidebar', () => {
    const { fire, menu, brainstorm, sent } = make(false, undefined, false, true);
    fire({
      sysEvent: {
        eventType: OsEventTypeList.DOUBLE_CLICK_EVENT,
        eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_L,
      },
    });

    expect(brainstorm.handleCancel).toHaveBeenCalledTimes(1);
    expect(menu.toggle).not.toHaveBeenCalled();
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

  it.each([
    [EventSourceType.TOUCH_EVENT_FROM_GLASSES_R, 'right'],
    [EventSourceType.TOUCH_EVENT_FROM_RING, 'ring'],
  ])('uses a deliberate %s double press to start an explicitly armed Brainstorm test', (source, sourceName) => {
    const events: unknown[] = [];
    const { fire, brainstorm, shutDownPageContainer } = make(
      false, { record: (event) => events.push(event) }, false, true, false, false, true,
    );
    fire({
      sysEvent: {
        eventType: OsEventTypeList.DOUBLE_CLICK_EVENT,
        eventSource: source,
      },
    });

    expect(brainstorm.handleAcceptance).toHaveBeenCalledWith(sourceName);
    expect(shutDownPageContainer).not.toHaveBeenCalled();
    expect(events).toContainEqual({
      type: 'brainstorm_action', action: 'begin_acceptance', surface: 'brainstorm',
      source: sourceName, gesture: 'double_press', delivery: 'sys',
    });
  });

  it('does not route a right double press into acceptance on ordinary Brainstorm', () => {
    const events: unknown[] = [];
    const { fire, brainstorm, shutDownPageContainer } = make(
      false, { record: (event) => events.push(event) }, false, true,
    );

    fire({
      sysEvent: {
        eventType: OsEventTypeList.DOUBLE_CLICK_EVENT,
        eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_R,
      },
    });

    expect(brainstorm.isAcceptanceArmed).toHaveBeenCalledTimes(1);
    expect(brainstorm.handleAcceptance).not.toHaveBeenCalled();
    expect(shutDownPageContainer).toHaveBeenCalledWith(1);
    expect(events).not.toContainEqual(expect.objectContaining({
      type: 'brainstorm_action', action: 'begin_acceptance',
    }));
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
  it('dismisses a visible assistant reply before a hidden feature receives a tap', () => {
    const { fire, sent, menu, navigation, assistantOverlay } = make(
      false, undefined, true, false, true,
    );

    fire(click(EventSourceType.TOUCH_EVENT_FROM_GLASSES_R));

    expect(assistantOverlay.dismissAssistantOverlay).toHaveBeenCalledTimes(1);
    expect(navigation.handleClick).not.toHaveBeenCalled();
    expect(menu.handleClick).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
  });

  it('dismisses a visible assistant reply before a hidden Brainstorm swipe action', () => {
    const { fire, sent, brainstorm, assistantOverlay } = make(
      false, undefined, false, true, true,
    );

    fire({ textEvent: { containerID: 2, eventType: OsEventTypeList.SCROLL_BOTTOM_EVENT } });

    expect(assistantOverlay.dismissAssistantOverlay).toHaveBeenCalledTimes(1);
    expect(brainstorm.handleCancel).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
  });

  it('maps a single G2 tap to the active navigation action owner before menu or relay', () => {
    const { fire, sent, menu, navigation } = make(false, undefined, true);
    fire(click(EventSourceType.TOUCH_EVENT_FROM_GLASSES_R));
    expect(navigation.handleClick).toHaveBeenCalledTimes(1);
    expect(menu.handleClick).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
  });

  it('maps a right-temple tap to the live Brainstorm owner before menu or relay', () => {
    const { fire, sent, menu, brainstorm } = make(false, undefined, false, true);
    fire(click(EventSourceType.TOUCH_EVENT_FROM_GLASSES_R));
    expect(brainstorm.handleClick).toHaveBeenCalledTimes(1);
    expect(menu.handleClick).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
  });

  it('records a redacted Brainstorm action only after the controller accepts it', () => {
    const events: unknown[] = [];
    const { fire } = make(false, { record: (event) => events.push(event) }, false, true);

    fire(click(EventSourceType.TOUCH_EVENT_FROM_GLASSES_R));

    expect(events).toContainEqual({
      type: 'brainstorm_action', action: 'show_terminal', surface: 'brainstorm',
      source: 'right', gesture: 'tap', delivery: 'sys',
    });
    expect(JSON.stringify(events)).not.toMatch(/topic|prompt|payload|token|https?:/i);
  });

  it('maps a downward G2 swipe to the live Brainstorm cancellation owner', () => {
    const { fire, sent, menu, brainstorm } = make(false, undefined, false, true);
    fire({ textEvent: { containerID: 2, eventType: OsEventTypeList.SCROLL_BOTTOM_EVENT } });
    expect(brainstorm.handleCancel).toHaveBeenCalledTimes(1);
    expect(menu.handleScroll).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
  });

  it('maps G2 swipes to the active navigation choice owner before menu or relay', () => {
    const { fire, sent, menu, navigation } = make(false, undefined, true);
    fire({ textEvent: { containerID: 2, eventType: OsEventTypeList.SCROLL_BOTTOM_EVENT } });
    expect(navigation.handleScroll).toHaveBeenCalledWith('down');
    expect(menu.handleScroll).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
  });

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

  it('selects the highlighted custom-menu row when a host delivers tap as textEvent', () => {
    const { fire, sent, menu } = make(true);

    // Protobuf omits CLICK_EVENT (0), so this is the host's source-less tap
    // shape on the image HUD's transparent text capture layer.
    fire({ textEvent: { containerID: 2, containerName: 'visual-input' } });

    expect(menu.handleClick).toHaveBeenCalledTimes(1);
    expect(menu.handleScroll).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
  });

  it('right click selects the custom menu row instead of confirming or relaying', () => {
    const { fire, sent, menu, imu } = make(true);
    fire(click(EventSourceType.TOUCH_EVENT_FROM_GLASSES_R));
    expect(menu.handleClick).toHaveBeenCalledTimes(1);
    expect(imu.confirmByTap).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
  });

  it('selects the custom menu row when a G2 sys click has no physical source', () => {
    const { fire, sent, menu } = make(true);

    fire(click(EventSourceType.TOUCH_EVENT_FORM_DUMMY_NULL));

    expect(menu.handleClick).toHaveBeenCalledTimes(1);
    expect(sent).toEqual([]);
  });

  it('left click selects an open menu row instead of leaving Calendar or Tasks inert', () => {
    const { fire, sent, menu, imu } = make(true);
    fire(click(EventSourceType.TOUCH_EVENT_FROM_GLASSES_L));
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

  it('opens listening settings from contextual-menu item four', () => {
    const r = make();
    r.fire({ menuItemClickEvent: { itemID: 4 } });
    expect(r.menu.openSettings).toHaveBeenCalledTimes(1);
  });

  it('classifies the delivered gesture without forwarding source or content', () => {
    const events: unknown[] = [];
    const r = make(false, { record: (event) => events.push(event) });

    r.fire({ sysEvent: { eventType: LONG_PRESS, eventSource: 1 } });

    expect(events).toEqual([{
      type: 'gesture', gesture: 'long_press', delivery: 'sys',
    }]);
  });
});

describe('EventRouter — production Messages menu composition', () => {
  it('routes physical menu and list events into the bound Mac request, then walks back to Home', async () => {
    let dispatch: (event: EvenHubEvent) => void = () => {};
    const sent: UplinkMessage[] = [];
    const sidebars: string[][] = [];
    const hud = {
      showSidebar: vi.fn(async (items: string[]) => {
        sidebars.push(items);
        return true;
      }),
      showList: vi.fn(async () => true),
      showDefault: vi.fn(async () => true),
      setStatus: vi.fn(async () => true),
      setSelection: vi.fn(async () => true),
      moveSelection: vi.fn(async () => true),
      showProductivity: vi.fn(async () => true),
    } as unknown as HudPresenter;
    const link = {
      send: (message: UplinkMessage) => { sent.push(message); },
    } as unknown as MacLink;
    const menu = new CapabilityMenu(hud, new Settings(), link);
    const client = {
      track: (unsubscribe: () => void) => unsubscribe,
      require: () => ({
        onEvenHubEvent: (handler: (event: EvenHubEvent) => void) => {
          dispatch = handler;
          return () => {};
        },
        shutDownPageContainer: vi.fn(),
      }),
    } as unknown as BridgeClient;
    const audio = {
      isOpen: false,
      ingest: vi.fn(),
      start: vi.fn(),
      stop: vi.fn(),
    } as unknown as AudioStreamer;
    const imu = {
      isPending: vi.fn(() => false),
      ingest: vi.fn(),
      confirmByTap: vi.fn(),
    } as unknown as ImuMonitor;
    new EventRouter(client, link, audio, imu, menu).attach();

    dispatch({ sysEvent: {
      eventType: OsEventTypeList.DOUBLE_CLICK_EVENT,
      eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_L,
    } } as EvenHubEvent);
    await vi.waitFor(() => expect(sidebars).toHaveLength(1));
    expect(sidebars[0]?.[0]).toBe('Messages');

    dispatch(click(EventSourceType.TOUCH_EVENT_FROM_GLASSES_R) as EvenHubEvent);
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    const request = sent[0] as Extract<UplinkMessage, { kind: 'input' }>;
    expect(request.event).toMatchObject({
      containerName: 'capmenu', selectedName: 'Messages', selectedIndex: 0,
    });

    expect(await menu.presentList(
      'MESSAGES',
      ['[now] Dele · Are you coming?', '[2m] Adaeze · Call me'],
      request.event.requestId,
      true,
      'tap select · left x2 overview',
    )).toBe(true);
    dispatch({ listEvent: {
      eventType: OsEventTypeList.CLICK_EVENT,
      currentSelectItemIndex: 1,
    } } as EvenHubEvent);
    expect(sent[1]).toMatchObject({
      kind: 'input',
      event: {
        containerName: 'capdata',
        selectedName: 'MESSAGES',
        selectedIndex: 1,
        requestId: request.event.requestId,
      },
    });
    expect(await menu.presentList(
      'MESSAGES · CONVERSATION',
      ['Dele · Are you coming?', 'Say reply saying …'],
      request.event.requestId,
      false,
    )).toBe(true);

    dispatch({ sysEvent: {
      eventType: OsEventTypeList.DOUBLE_CLICK_EVENT,
      eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_L,
    } } as EvenHubEvent);
    await vi.waitFor(() => expect(hud.showList).toHaveBeenCalledTimes(4));
    dispatch({ sysEvent: {
      eventType: OsEventTypeList.DOUBLE_CLICK_EVENT,
      eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_L,
    } } as EvenHubEvent);
    await vi.waitFor(() => expect(sidebars).toHaveLength(2));
    dispatch({ sysEvent: {
      eventType: OsEventTypeList.DOUBLE_CLICK_EVENT,
      eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_L,
    } } as EvenHubEvent);
    await vi.waitFor(() => expect(hud.showDefault).toHaveBeenCalledTimes(1));
    expect(sent[2]).toEqual({
      kind: 'input',
      event: {
        containerName: 'capability-lifecycle',
        selectedName: 'messages-list',
      },
    });
    expect(sent[3]).toEqual({
      kind: 'input',
      event: {
        containerName: 'capability-lifecycle',
        selectedName: 'messages-home',
      },
    });
  });
});
