import { describe, expect, it, vi } from 'vitest';
import { AudioInputSource, EventSourceType, OsEventTypeList, type EvenHubEvent } from '@evenrealities/even_hub_sdk';
import { EventRouter, type LifecycleHandler } from './EventRouter';
import type { BridgeClient } from './BridgeClient';
import type { MacLink } from './MacLink';
import type { AudioStreamer } from './AudioStreamer';
import type { ImuMonitor } from './ImuMonitor';
import { CapabilityMenu } from './CapabilityMenu';
import { Settings } from './Settings';
import type { HudPresenter } from './HudPresenter';
import { CAPABILITY_MENU_ID_BASE, type UplinkMessage } from './types';

function make(
  menuConsumesLists = false,
  diagnostics?: { record: (event: unknown) => void },
  navigationActive = false,
  brainstormActive = false,
  assistantOverlayVisible = false,
  notificationVisible = false,
  brainstormAcceptanceArmed = false,
  panelSwipeOwned = false,
  panelExpanded = false,
  // Refreshable implies expanded, never the reverse: the pomodoro page is
  // expanded but excluded from the carve-out, so it keeps the mic.
  panelRefreshable = panelExpanded,
  promptedInput?: { isActive(): boolean; cancel(reason?: string): void },
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
    // Production `CapabilityMenu.toggle()` is async; the router chains on it
    // to hold one Back at a time.
    toggle: vi.fn(async () => {}),
    handleList: vi.fn(() => menuConsumesLists),
    handleScroll: vi.fn(() => menuConsumesLists),
    handleClick: vi.fn(() => menuConsumesLists),
    openSettings: vi.fn(),
    openPlanning: vi.fn(),
    // `menuConsumesLists` stands in for "a menu view owns input", which is
    // exactly when the overview click-to-notifications drill-in must NOT fire.
    isOpen: vi.fn(() => menuConsumesLists),
    openNotifications: vi.fn(async () => {}),
    openActiveTab: vi.fn(async () => {}),
    // The overview's tab rail: a resting swipe is consumed only when the
    // menu is closed, and long-press-release refreshes only inside an
    // expanded tab. Both default to "not owning" so every other test keeps
    // its established relay and mic behaviour.
    handleOverviewSwipe: vi.fn(() => !menuConsumesLists && panelSwipeOwned),
    isPanelExpanded: vi.fn(() => panelExpanded),
    isPanelRefreshable: vi.fn(() => panelRefreshable),
    refreshPanel: vi.fn(),
    openFromContextualMenu: vi.fn(async () => {}),
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
    // False by default: only the topic-capture state claims a swipe as a
    // backout; every test that wants that behaviour flips it explicitly.
    handleSwipe: vi.fn(() => false),
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
    scrollAssistantOverlay: vi.fn(() => assistantOverlayVisible),
    dismissAssistantOverlay: vi.fn(async () => true),
    hasAssistantConversation: vi.fn(() => false),
    assistantConversationIdentity: vi.fn((): { conversationId: string; revision: number } | undefined => undefined),
    isAssistantConversationContinuePromptVisible: vi.fn(() => false),
    scrollAssistantConversation: vi.fn(() => false),
    continueAssistantConversation: vi.fn(async () => false),
    requestAssistantConversationExit: vi.fn(async (): Promise<
      'inactive' | 'prompted' | 'exited' | 'failed'
    > => 'inactive'),
    hasVisibleNotification: vi.fn(() => notificationVisible),
    dismissVisibleNotification: vi.fn(async () => true),
  };
  const shutDownPageContainer = vi.fn(async (_mode: number) => true);
  const lifecycle: LifecycleHandler = {
    onForegroundEnter: vi.fn(),
    onForegroundExit: vi.fn(),
    onContextualMenu: vi.fn(),
    onExit: vi.fn(),
  };
  const link = {
    send: (message: UplinkMessage) => sent.push(message),
    sendConversationAction: (message: UplinkMessage) => {
      sent.push(message);
      return true;
    },
    connect: vi.fn(),
    wasReplaced: false,
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
  const transcriptScroll = vi.fn(() => true);
  new EventRouter(
    client as unknown as BridgeClient,
    link as unknown as MacLink,
    audio as unknown as AudioStreamer,
    imu as unknown as ImuMonitor,
    menu as unknown as CapabilityMenu,
    lifecycle,
    diagnostics,
    navigation,
    brainstorm,
    assistantOverlay,
    transcriptScroll,
    promptedInput,
  ).attach();
  return {
    fire: (e: unknown) => dispatch(e as EvenHubEvent),
    sent,
    transcriptScroll,
    audio,
    imu,
    menu,
    navigation,
    brainstorm,
    assistantOverlay,
    lifecycle,
    link,
    shutDownPageContainer,
  };
}

it('a replaced document ignores gestures and PCM while retaining host lifecycle recovery', () => {
  const r = make();
  r.link.wasReplaced = true;
  r.fire({ sysEvent: { eventType: OsEventTypeList.LONG_PRESS_EVENT } });
  r.fire({ sysEvent: { eventType: OsEventTypeList.LONG_PRESS_RELEASE_EVENT } });
  r.fire({ menuItemClickEvent: { itemID: 2 } });
  r.fire({ audioEvent: { audioPcm: [1, 2] } });
  r.fire({ textEvent: { eventType: OsEventTypeList.SCROLL_TOP_EVENT } });
  expect(r.audio.start).not.toHaveBeenCalled();
  expect(r.audio.stop).not.toHaveBeenCalled();
  expect(r.audio.ingest).not.toHaveBeenCalled();
  expect(r.sent).toEqual([]);
  r.fire({ sysEvent: { eventType: OsEventTypeList.FOREGROUND_ENTER_EVENT } });
  expect(r.lifecycle.onForegroundEnter).toHaveBeenCalledOnce();
});

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
    expect(audio.ingest).toHaveBeenCalledWith([1, 2, 3], undefined, undefined);
  });

  it.each([AudioInputSource.Glasses, AudioInputSource.Phone])(
    'forwards the SDK actual %s audio source alongside speaker attribution', (source) => {
      const { fire, audio } = make();
      fire({ audioEvent: { audioPcm: [2, 1], speakerRole: 'other', source } });

      expect(audio.ingest).toHaveBeenCalledExactlyOnceWith([2, 1], 'other', source);
    },
  );

  it('does not replace an unknown SDK audio source with the requested microphone', () => {
    const { fire, audio } = make();
    fire({ audioEvent: { audioPcm: [2, 1], speakerRole: 'self', source: 'future-microphone' } });

    expect(audio.ingest).toHaveBeenCalledExactlyOnceWith([2, 1], 'self', 'future-microphone');
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

  it('a non-decision temple press opens the tab the overview rail has selected', () => {
    const { fire, imu, menu, sent } = make();
    fire(click(EventSourceType.TOUCH_EVENT_FROM_GLASSES_L));
    expect(imu.confirmByTap).not.toHaveBeenCalled();
    expect(imu.declineByTap).not.toHaveBeenCalled();
    // Consumed locally: this is the drill-in for what is already on the
    // glass. The old relay carried no containerName, so the Mac dropped it.
    expect(menu.openActiveTab).toHaveBeenCalledTimes(1);
    expect(sent).toEqual([]);
  });

  it('leaves a click alone while a menu view already owns input', () => {
    const { fire, menu } = make(true);
    fire(click(EventSourceType.TOUCH_EVENT_FROM_GLASSES_L));
    expect(menu.openActiveTab).not.toHaveBeenCalled();
    expect(menu.handleClick).toHaveBeenCalledTimes(1);
  });

  it('a swipe on the resting overview moves along the tab rail and never reaches the Mac', () => {
    const { fire, menu, sent } = make(false, undefined, false, false, false, false, false, true);
    fire({ textEvent: { eventType: OsEventTypeList.SCROLL_BOTTOM_EVENT } });
    fire({ textEvent: { eventType: OsEventTypeList.SCROLL_TOP_EVENT } });
    expect(menu.handleOverviewSwipe).toHaveBeenCalledTimes(2);
    expect(sent).toEqual([]);
  });

  it('a live navigation keeps its swipes even when the rail would take them', () => {
    const { fire, menu, navigation } = make(
      false, undefined, true, false, false, false, false, true,
    );
    fire({ textEvent: { eventType: OsEventTypeList.SCROLL_BOTTOM_EVENT } });
    expect(navigation.handleScroll).toHaveBeenCalledWith('down');
    expect(menu.handleOverviewSwipe).not.toHaveBeenCalled();
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

  it('left-temple double press backs out of an active confirmation', () => {
    const { fire, imu, sent } = make();
    imu.isPending.mockReturnValue(true);

    fire({
      sysEvent: {
        eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_L,
        eventType: OsEventTypeList.DOUBLE_CLICK_EVENT,
      },
    });

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

  it('right temple is the same local Back as the left, never the OS exit dialog', () => {
    // GesturePolicy owns double press independently of the hand (the
    // simulator's generic double-click is right-sourced, and it used to open
    // the exit confirmation). Exit is the host's own lifecycle, not a gesture.
    const { fire, menu, sent, shutDownPageContainer, lifecycle } = make();
    fire({
      sysEvent: {
        eventType: OsEventTypeList.DOUBLE_CLICK_EVENT,
        eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_R,
      },
    });
    expect(menu.toggle).toHaveBeenCalledTimes(1);
    expect(shutDownPageContainer).not.toHaveBeenCalled();
    expect(lifecycle.onExit).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
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
    const { fire, brainstorm, menu, sent, shutDownPageContainer } = make(
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
    // Unarmed, the right double press is the same Back as the left: it
    // cancels the live run and never reaches the OS exit dialog.
    expect(brainstorm.handleCancel).toHaveBeenCalledTimes(1);
    expect(menu.toggle).not.toHaveBeenCalled();
    expect(shutDownPageContainer).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
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
  it('keeps conversation gestures on the one persistent surface and asks before Back exits', async () => {
    const { fire, sent, menu, assistantOverlay } = make();
    assistantOverlay.hasAssistantConversation.mockReturnValue(true);
    assistantOverlay.assistantConversationIdentity.mockReturnValue({
      conversationId: 'voice-a', revision: 7,
    });
    assistantOverlay.scrollAssistantConversation.mockReturnValue(true);
    assistantOverlay.requestAssistantConversationExit
      .mockResolvedValueOnce('prompted')
      .mockResolvedValueOnce('exited');

    fire({ textEvent: { containerName: 'conversation', eventType: OsEventTypeList.SCROLL_TOP_EVENT } });
    fire(click(EventSourceType.TOUCH_EVENT_FROM_GLASSES_R));
    fire({ sysEvent: {
      eventType: OsEventTypeList.DOUBLE_CLICK_EVENT,
      eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_L,
    } });
    await Promise.resolve();
    fire({ sysEvent: {
      eventType: OsEventTypeList.DOUBLE_CLICK_EVENT,
      eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_L,
    } });
    await Promise.resolve();

    expect(assistantOverlay.scrollAssistantConversation).toHaveBeenCalledWith('up');
    expect(assistantOverlay.requestAssistantConversationExit).toHaveBeenCalledTimes(2);
    expect(menu.toggle).not.toHaveBeenCalled();
    expect(sent).toContainEqual({
      kind: 'input', event: {
        containerName: 'assistant-conversation', selectedName: 'exit',
        conversationId: 'voice-a', conversationRevision: 7,
      },
    });
  });

  it('continues from the visible inactivity question only after the footer repaint succeeds', async () => {
    const { fire, sent, assistantOverlay } = make();
    assistantOverlay.hasAssistantConversation.mockReturnValue(true);
    assistantOverlay.assistantConversationIdentity.mockReturnValue({
      conversationId: 'voice-a', revision: 7,
    });
    assistantOverlay.isAssistantConversationContinuePromptVisible.mockReturnValue(true);
    assistantOverlay.continueAssistantConversation.mockResolvedValue(true);

    fire(click(EventSourceType.TOUCH_EVENT_FROM_GLASSES_R));
    await Promise.resolve();

    expect(assistantOverlay.continueAssistantConversation).toHaveBeenCalledTimes(1);
    expect(assistantOverlay.requestAssistantConversationExit).not.toHaveBeenCalled();
    expect(sent).toContainEqual({
      kind: 'input', event: {
        containerName: 'assistant-conversation', selectedName: 'continue',
        conversationId: 'voice-a', conversationRevision: 7,
      },
    });
  });

  it('does not acknowledge Continue when its visible footer replacement fails', async () => {
    const { fire, sent, assistantOverlay } = make();
    assistantOverlay.hasAssistantConversation.mockReturnValue(true);
    assistantOverlay.assistantConversationIdentity.mockReturnValue({
      conversationId: 'voice-a', revision: 7,
    });
    assistantOverlay.isAssistantConversationContinuePromptVisible.mockReturnValue(true);
    assistantOverlay.continueAssistantConversation.mockResolvedValue(false);

    fire(click(EventSourceType.TOUCH_EVENT_FROM_GLASSES_R));
    await Promise.resolve();

    expect(sent).toEqual([]);
  });

  it('treats a protobuf-zero left tap as Continue on the visible conversation', async () => {
    const { fire, sent, assistantOverlay } = make();
    assistantOverlay.hasAssistantConversation.mockReturnValue(true);
    assistantOverlay.assistantConversationIdentity.mockReturnValue({
      conversationId: 'voice-left', revision: 4,
    });
    assistantOverlay.isAssistantConversationContinuePromptVisible.mockReturnValue(true);
    assistantOverlay.continueAssistantConversation.mockResolvedValue(true);

    fire({ sysEvent: { eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_L } });
    await Promise.resolve();

    expect(sent).toContainEqual({
      kind: 'input', event: {
        containerName: 'assistant-conversation', selectedName: 'continue',
        conversationId: 'voice-left', conversationRevision: 4,
      },
    });
  });

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

  it('pages a visible assistant reply before a hidden Brainstorm swipe action', () => {
    const { fire, sent, brainstorm, assistantOverlay } = make(
      false, undefined, false, true, true,
    );

    fire({ textEvent: { containerID: 2, eventType: OsEventTypeList.SCROLL_BOTTOM_EVENT } });

    expect(assistantOverlay.scrollAssistantOverlay).toHaveBeenCalledWith('down');
    expect(assistantOverlay.dismissAssistantOverlay).not.toHaveBeenCalled();
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

  it('maps G2 swipes on a live Brainstorm to the transcript, never to cancel', () => {
    // The wearer, watching this live: "can i scroll up and down to see the
    // replies?" A downward swipe used to CANCEL the session — the harshest
    // possible answer to someone trying to re-read a reply. Cancel is the
    // deliberate left double-press only.
    const { fire, sent, menu, brainstorm, transcriptScroll } = make(false, undefined, false, true);
    fire({ textEvent: { containerID: 2, eventType: OsEventTypeList.SCROLL_BOTTOM_EVENT } });
    fire({ textEvent: { containerID: 2, eventType: OsEventTypeList.SCROLL_TOP_EVENT } });
    expect(transcriptScroll).toHaveBeenNthCalledWith(1, 'down');
    expect(transcriptScroll).toHaveBeenNthCalledWith(2, 'up');
    expect(brainstorm.handleCancel).not.toHaveBeenCalled();
    expect(menu.handleScroll).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
  });

  it('a left single on a live Brainstorm is the footer action, not menu or cancel', () => {
    // Even's input reference gives BOTH temples the same gesture set (the
    // audit's O3): a left single is the same footer tap as a right single.
    // It must not cancel, must not open anything, and — once the surface
    // consumes it — must not also be relayed as a second, generic input.
    const { fire, menu, brainstorm, sent } = make(false, undefined, false, true);
    fire({
      sysEvent: {
        eventType: OsEventTypeList.CLICK_EVENT,
        eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_L,
      },
    });
    expect(menu.toggle).not.toHaveBeenCalled();
    expect(menu.openNotifications).not.toHaveBeenCalled();
    expect(brainstorm.handleCancel).not.toHaveBeenCalled();
    expect(brainstorm.handleClick).toHaveBeenCalledTimes(1);
    expect(sent).toEqual([]); // consumed by the surface, not double-delivered
  });

  it('a left single the live Brainstorm declines still reaches the Mac', () => {
    // The surface owning the page does not own every tap: when its footer
    // has nothing armed (handleClick() false), the input must keep its
    // pre-O3 path and be relayed for the Mac to decide.
    const { fire, menu, brainstorm, sent } = make(false, undefined, false, true);
    brainstorm.handleClick.mockReturnValue(false);
    fire({
      sysEvent: {
        eventType: OsEventTypeList.CLICK_EVENT,
        eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_L,
      },
    });
    expect(brainstorm.handleCancel).not.toHaveBeenCalled();
    expect(menu.toggle).not.toHaveBeenCalled();
    expect(sent).toHaveLength(1); // relayed, the Mac decides
  });

  it('keeps cancel on the left double press', () => {
    const { fire, brainstorm } = make(false, undefined, false, true);
    fire({
      sysEvent: {
        eventType: OsEventTypeList.DOUBLE_CLICK_EVENT,
        eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_L,
      },
    });
    expect(brainstorm.handleCancel).toHaveBeenCalledTimes(1);
  });

  it('lets a swipe back out of topic capture, where there is nothing to scroll', () => {
    // The one state that keeps the old swipe-cancel: no transcript exists
    // yet, the screen is a single promise, and the wearer who swipes is
    // saying never mind. The controller decides (it knows the state); the
    // router only offers it the swipe before falling back to scrolling.
    const { fire, brainstorm, transcriptScroll } = make(false, undefined, false, true);
    brainstorm.handleSwipe.mockReturnValueOnce(true);
    fire({ textEvent: { containerID: 2, eventType: OsEventTypeList.SCROLL_BOTTOM_EVENT } });
    expect(brainstorm.handleSwipe).toHaveBeenCalledTimes(1);
    expect(transcriptScroll).not.toHaveBeenCalled();
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

  it('dismisses a Home notification from the native list-event tap before it can relay', () => {
    const { fire, assistantOverlay, menu, sent } = make(
      false, undefined, false, false, false, true,
    );

    // Some G2 firmware surfaces the current Home tile as a List event rather
    // than a system/text event. A protobuf-zero click is omitted on the wire.
    fire({ listEvent: { containerID: 3 } });

    expect(assistantOverlay.dismissVisibleNotification).toHaveBeenCalledTimes(1);
    expect(menu.handleList).not.toHaveBeenCalled();
    expect(sent).toEqual([]);
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

  it('toggles the mic when a plain sysEvent long-press reaches release', () => {
    const r = make();
    r.fire({ sysEvent: { eventType: LONG_PRESS, eventSource: 1 } });
    r.fire({ sysEvent: { eventType: RELEASE, eventSource: 1 } });
    expect(r.audio.stop).toHaveBeenCalledTimes(1);
  });

  it('refreshes an expanded panel tab instead of muting — the one carve-out', () => {
    // Menu open (a data view owns input) AND that view is a panel tab's top
    // level: the wearer's ruling is that long press re-fetches it there.
    const r = make(true, undefined, false, false, false, false, false, false, true);
    r.fire({ textEvent: { eventType: LONG_PRESS, containerName: 'main' } });
    r.fire({ textEvent: { eventType: RELEASE, containerName: 'main' } });
    expect(r.menu.refreshPanel).toHaveBeenCalledTimes(1);
    expect(r.audio.stop).not.toHaveBeenCalled();
    expect(r.sent).toEqual([]);
  });

  it('keeps the mic on the pomodoro page — expanded, but nothing to re-fetch', () => {
    // The defect the simulator sweep surfaced: POMODORO satisfies
    // isPanelExpanded, so the hold used to fire a refresh the wearer could
    // not see and swallow the mic, with the hint naming neither.
    const r = make(true, undefined, false, false, false, false, false, false, true, false);
    r.fire({ textEvent: { eventType: LONG_PRESS, containerName: 'main' } });
    r.fire({ textEvent: { eventType: RELEASE, containerName: 'main' } });
    expect(r.menu.refreshPanel).not.toHaveBeenCalled();
    expect(r.audio.stop).toHaveBeenCalledTimes(1);
  });

  it('keeps the mic on long press inside an opened article, and on the overview', () => {
    const r = make(true);
    r.fire({ sysEvent: { eventType: LONG_PRESS, eventSource: 1 } });
    r.fire({ sysEvent: { eventType: RELEASE, eventSource: 1 } });
    expect(r.menu.refreshPanel).not.toHaveBeenCalled();
    expect(r.audio.stop).toHaveBeenCalledTimes(1);
  });

  it('toggles the mic from a textEvent — the relay must not swallow it', () => {
    const r = make();
    r.fire({ textEvent: { eventType: LONG_PRESS, containerName: 'main' } });
    r.fire({ textEvent: { eventType: RELEASE, containerName: 'main' } });
    expect(r.audio.stop).toHaveBeenCalledTimes(1);
    expect(r.sent).toEqual([]); // not relayed to the Mac as a scroll
  });

  it('toggles the mic from a listEvent', () => {
    const r = make();
    r.fire({ listEvent: { eventType: LONG_PRESS } });
    r.fire({ listEvent: { eventType: RELEASE } });
    expect(r.audio.stop).toHaveBeenCalledTimes(1);
    expect(r.sent).toEqual([]);
  });

  it('un-mutes when the mic is off, reusing lastSource', () => {
    const r = make();
    r.audio.isOpen = false;
    r.fire({ sysEvent: { eventType: LONG_PRESS, eventSource: 1 } });
    r.fire({ sysEvent: { eventType: RELEASE, eventSource: 1 } });
    expect(r.audio.start).toHaveBeenCalledWith(r.audio.lastSource);
  });

  it('release is consumed silently — one press, one toggle, no phantom input', () => {
    const r = make();
    r.fire({ sysEvent: { eventType: RELEASE, eventSource: 1 } });
    expect(r.audio.stop).not.toHaveBeenCalled();
    expect(r.audio.start).not.toHaveBeenCalled();
    expect(r.sent).toEqual([]);
  });

  it('routes a cached Refresh row through tap-then-long-press without toggling the mic', () => {
    // Connection settings moved to the phone (HudPageBuilder.systemMenu):
    // the retired Refresh id stays routed to the settings guidance because a
    // host may still show a cached menu, and it must not mute on the way.
    const r = make();

    r.fire({ sysEvent: { eventType: LONG_PRESS, eventSource: 1 } });
    r.fire({ sysEvent: { eventType: OsEventTypeList.FOREGROUND_ENTER_EVENT } });
    r.fire({ menuItemClickEvent: { itemID: 3 } });
    r.fire({ sysEvent: { eventType: RELEASE, eventSource: 1 } });
    r.fire({ sysEvent: { eventType: OsEventTypeList.FOREGROUND_EXIT_EVENT } });

    expect(r.menu.openSettings).toHaveBeenCalledTimes(1);
    expect(r.link.connect).not.toHaveBeenCalled();
    expect(r.audio.stop).not.toHaveBeenCalled();
    expect(r.audio.start).not.toHaveBeenCalled();
    expect(r.lifecycle.onForegroundEnter).not.toHaveBeenCalled();
    expect(r.lifecycle.onForegroundExit).not.toHaveBeenCalled();
    expect(r.lifecycle.onContextualMenu).toHaveBeenNthCalledWith(1, true);
    expect(r.lifecycle.onContextualMenu).toHaveBeenNthCalledWith(2, false);
  });

  it('does not leave the app backgrounded after an automation-style menu selection', () => {
    const r = make();

    r.fire({ menuItemClickEvent: { itemID: 5 } });
    r.fire({ sysEvent: { eventType: OsEventTypeList.FOREGROUND_EXIT_EVENT } });

    expect(r.menu.openPlanning).toHaveBeenCalledTimes(1);
    expect(r.lifecycle.onForegroundExit).not.toHaveBeenCalled();
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

  it('routes a cached Refresh tap to the phone-owned connection settings, not a local reconnect', () => {
    const r = make();

    r.fire({ menuItemClickEvent: { itemID: 3 } });

    expect(r.menu.openSettings).toHaveBeenCalledTimes(1);
    expect(r.link.connect).not.toHaveBeenCalled();
    expect(r.sent).toEqual([]);
  });

  it('opens unified Planning from contextual-menu item five', () => {
    const r = make();
    r.fire({ menuItemClickEvent: { itemID: 5 } });
    expect(r.menu.openPlanning).toHaveBeenCalledTimes(1);
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
    // Mirrors HudPresenter.usesCustomListSelection: a page drawn as the
    // firmware's own list (the Messages picker and thread since 2026-10-05)
    // lets the firmware cursor own the reported row index.
    type NativeListOptions = { nativeFirmwareList?: boolean; nativeTextPage?: boolean };
    let shownOptions: NativeListOptions | undefined;
    const ownsCustomCursor = (options: NativeListOptions | undefined) => options !== undefined
      && options.nativeFirmwareList !== true && options.nativeTextPage !== true;
    const hud = {
      usesCustomListSelection: (next?: NativeListOptions) => next
        ? ownsCustomCursor(next)
        : ownsCustomCursor(shownOptions),
      showList: vi.fn(async (_items: string[], options?: NativeListOptions) => {
        shownOptions = options;
        return true;
      }),
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

    // The wearer picks Messages in the firmware contextual menu. The OS then
    // closes its overlay and delivers a foreground-exit — without replaying
    // it, the router's overlay boundary rightly swallows every later input.
    dispatch({ menuItemClickEvent: {
      itemID: CAPABILITY_MENU_ID_BASE,
    } } as EvenHubEvent);
    dispatch({ sysEvent: {
      eventType: OsEventTypeList.FOREGROUND_EXIT_EVENT,
    } } as EvenHubEvent);
    await vi.waitFor(() => expect(sent).toHaveLength(2));
    // Opening a capability first tells the Mac which surface owns the glass
    // (navigation ownership), then asks for that capability's content.
    expect(sent[0]).toMatchObject({
      kind: 'input',
      event: { containerName: 'capability-lifecycle', selectedName: 'surface-feature' },
    });
    const request = sent[1] as Extract<UplinkMessage, { kind: 'input' }>;
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
    expect(sent[2]).toMatchObject({
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
    // The list call starts before its restored highlight has finished.
    // A second deliberate Back belongs after the visible picker is ready.
    const selection = sent[2] as Extract<UplinkMessage, { kind: 'input' }>;
    await vi.waitFor(() => expect(sent[3]).toEqual({
      kind: 'input',
      event: { containerName: 'capability-lifecycle', selectedName: 'messages-list', requestId: request.event.requestId, traceId: selection.event.traceId },
    }));
    // That lifecycle message is the return path's last act, but the Back
    // owner's promise settles a few microtasks later, and the router refuses
    // a second Back until it has (one Back at a time). Yield one macrotask so
    // the next press is the deliberate second Back, not a dropped repeat.
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    dispatch({ sysEvent: {
      eventType: OsEventTypeList.DOUBLE_CLICK_EVENT,
      eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_L,
    } } as EvenHubEvent);
    await vi.waitFor(() => expect(hud.showDefault).toHaveBeenCalledTimes(1));
    expect(sent[3]).toEqual({
      kind: 'input',
      event: {
        containerName: 'capability-lifecycle',
        selectedName: 'messages-list',
        requestId: request.event.requestId,
        traceId: selection.event.traceId,
      },
    });
    // Walking back to Home retires ownership twice, and both halves matter
    // to the Mac (services/capabilities.py): the Messages-scoped retirement
    // clears the bound message selection, and the surface-level one clears
    // the active capability so no later response can paint over Home.
    expect(sent[4]).toEqual({
      kind: 'input',
      event: { containerName: 'capability-lifecycle', selectedName: 'surface-home' },
    });
    expect(sent[5]).toEqual({
      kind: 'input',
      event: {
        containerName: 'capability-lifecycle',
        selectedName: 'messages-home',
        requestId: request.event.requestId,
        traceId: selection.event.traceId,
      },
    });
  });
});


describe('EventRouter prompt ownership', () => {
  function question() {
    const owner = { isActive: vi.fn(() => true), cancel: vi.fn() };
    return { owner, ...make(false, undefined, true, true, true, true, false, true, true, true, owner) };
  }

  it('Back cancels the question before touching its retained feature or overlay', () => {
    const f = question();
    f.fire({ sysEvent: {
      eventType: OsEventTypeList.DOUBLE_CLICK_EVENT,
      eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_L,
    } });
    expect(f.owner.cancel).toHaveBeenCalledOnce();
    expect(f.assistantOverlay.dismissAssistantOverlay).not.toHaveBeenCalled();
    expect(f.navigation.handleCancel).not.toHaveBeenCalled();
    expect(f.brainstorm.handleCancel).not.toHaveBeenCalled();
    expect(f.menu.toggle).not.toHaveBeenCalled();
    expect(f.sent).toEqual([]);
  });

  it.each([
    click(EventSourceType.TOUCH_EVENT_FROM_GLASSES_R),
    { textEvent: { eventType: OsEventTypeList.SCROLL_TOP_EVENT } },
    { textEvent: { eventType: OsEventTypeList.SCROLL_BOTTOM_EVENT } },
    { textEvent: {} },
    { listEvent: { currentSelectItemIndex: 1 } },
  ])('consumes ordinary input without operating a hidden feature: %j', (event) => {
    const f = question();
    f.fire(event);
    expect(f.owner.cancel).not.toHaveBeenCalled();
    expect(f.assistantOverlay.dismissAssistantOverlay).not.toHaveBeenCalled();
    expect(f.menu.handleList).not.toHaveBeenCalled();
    expect(f.navigation.handleClick).not.toHaveBeenCalled();
    expect(f.navigation.handleScroll).not.toHaveBeenCalled();
    expect(f.brainstorm.handleClick).not.toHaveBeenCalled();
    expect(f.sent).toEqual([]);
  });

  it('keeps hold-to-mute instead of refreshing the hidden News page', () => {
    const f = question();
    f.fire({ textEvent: { eventType: OsEventTypeList.LONG_PRESS_EVENT } });
    f.fire({ textEvent: { eventType: OsEventTypeList.LONG_PRESS_RELEASE_EVENT } });
    expect(f.audio.stop).toHaveBeenCalledOnce();
    expect(f.menu.refreshPanel).not.toHaveBeenCalled();
  });

  it('releases the question before a contextual-menu feature action', () => {
    const f = question();
    f.fire({ menuItemClickEvent: { itemID: CAPABILITY_MENU_ID_BASE } });
    expect(f.owner.cancel).toHaveBeenCalledOnce();
    expect(f.owner.cancel.mock.invocationCallOrder[0]).toBeLessThan(
      f.menu.openFromContextualMenu.mock.invocationCallOrder[0]!,
    );
  });

  it('preserves confirmation priority and lifecycle delivery', () => {
    const f = question();
    f.imu.isPending.mockReturnValue(true);
    f.fire(click(EventSourceType.TOUCH_EVENT_FROM_GLASSES_L));
    expect(f.imu.confirmByTap).toHaveBeenCalledOnce();
    expect(f.owner.cancel).not.toHaveBeenCalled();
    f.fire({ sysEvent: { eventType: OsEventTypeList.SYSTEM_EXIT_EVENT } });
    expect(f.lifecycle.onExit).toHaveBeenCalledWith('system');
  });
});
