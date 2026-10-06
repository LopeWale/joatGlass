import { afterAll, afterEach, describe, expect, it, vi } from 'vitest';
import {
  EventSourceType,
  OsEventTypeList,
  type EvenHubEvent,
} from '@evenrealities/even_hub_sdk';
import { CapabilityMenu } from './CapabilityMenu';
import { EventRouter } from './EventRouter';
import { Settings } from './Settings';
import { CAPABILITIES, CAPABILITY_MENU_ID_BASE } from './types';
import type { AudioStreamer } from './AudioStreamer';
import type { BridgeClient } from './BridgeClient';
import type { HudPresenter } from './HudPresenter';
import type { ImuMonitor } from './ImuMonitor';
import { MacLink } from './MacLink';

type Dispatch = (event: EvenHubEvent) => void;

const sockets: FakeWebSocket[] = [];

class FakeWebSocket {
  static readonly OPEN = 1;
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  sent: string[] = [];

  constructor(readonly url: string) {
    sockets.push(this);
  }

  send(data: string): void { this.sent.push(data); }

  close(code = 1006): void {
    this.closed = true;
    this.readyState = 3;
    this.onclose?.({ code });
  }

  open(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }

  receive(message: unknown): void {
    this.onmessage?.({ data: JSON.stringify(message) });
  }
}

const previousWebSocket = (globalThis as { WebSocket?: unknown }).WebSocket;
(globalThis as { WebSocket?: unknown }).WebSocket = FakeWebSocket;

afterEach(() => {
  sockets.length = 0;
});

afterAll(() => {
  (globalThis as { WebSocket?: unknown }).WebSocket = previousWebSocket;
});

function rig() {
  const sidebars: string[][] = [];
  const lists: Array<{
    items: string[];
    options?: { title?: string; hint?: string; selectable?: boolean };
  }> = [];
  let dispatch: Dispatch | undefined;

  const client = {
    require: () => ({
      onEvenHubEvent: (handler: Dispatch) => {
        dispatch = handler;
        return () => {};
      },
      shutDownPageContainer: vi.fn(),
    }),
    track: (unsubscribe: () => void) => unsubscribe,
  } as unknown as BridgeClient;
  const hud = {
    showSidebar: vi.fn(async (items: string[]) => {
      sidebars.push(items);
      return true;
    }),
    showList: vi.fn(async (
      items: string[],
      options?: { title?: string; hint?: string; selectable?: boolean },
    ) => {
      lists.push({ items, options });
      return true;
    }),
    showDefault: vi.fn(async () => true),
    setStatus: vi.fn(async () => true),
    showProductivity: vi.fn(async () => true),
  } as unknown as HudPresenter;
  const link = new MacLink({ urls: ['ws://joat.test:8787/glasses'] });
  const settings = new Settings();
  const menu = new CapabilityMenu(hud, settings, link);
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

  const fire = (event: EvenHubEvent): void => {
    if (!dispatch) throw new Error('EventRouter did not attach');
    dispatch(event);
  };
  return { fire, hud, link, lists, menu, settings, sidebars };
}

function leftDoublePress() {
  return {
    sysEvent: {
      eventType: OsEventTypeList.DOUBLE_CLICK_EVENT,
      eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_L,
    },
  } as EvenHubEvent;
}

function rightPress() {
  return {
    sysEvent: {
      eventType: OsEventTypeList.CLICK_EVENT,
      eventSource: EventSourceType.TOUCH_EVENT_FROM_GLASSES_R,
    },
  } as EvenHubEvent;
}

function scrollDown() {
  return {
    textEvent: { eventType: OsEventTypeList.SCROLL_BOTTOM_EVENT },
  } as EvenHubEvent;
}

async function openSettingsFromFirmwareMenu(
  harness: ReturnType<typeof rig>,
): Promise<void> {
  // The wearer's menu is the firmware contextual menu; picking Settings
  // arrives as its OS -> APP action item click event, and the OS closing
  // its overlay then delivers a foreground-exit. Replaying both matters:
  // until the exit arrives, the router's overlay boundary swallows every
  // scroll and press, exactly as it must on hardware.
  harness.fire({ menuItemClickEvent: {
    itemID: CAPABILITY_MENU_ID_BASE + CAPABILITIES.indexOf('Settings'),
  } } as EvenHubEvent);
  harness.fire({ sysEvent: {
    eventType: OsEventTypeList.FOREGROUND_EXIT_EVENT,
  } } as EvenHubEvent);
  await vi.waitFor(() => expect(harness.lists).toHaveLength(1));
}

/**
 * 2026-09-11: listening policy, Repeat and Reconnect moved to the phone's
 * controls (Changelog, "phone controls"). The glasses' Settings slot is a
 * cached firmware menu link, so it shows directions and owns nothing but
 * Back. These tests drive the real EventRouter and pin that retirement: the
 * gestures that used to apply a policy, repeat a reply or recycle the link
 * reach neither the policy, the link nor the Mac — while the surface
 * announcements that retire capability ownership on the Mac still travel.
 */
describe('wearer G2 actions through Settings', () => {
  it('a press on the retired Repeat row reaches the Mac only as the surface announcements', async () => {
    const harness = rig();
    harness.link.connect();
    sockets[0]?.open();
    sockets[0]?.receive({ kind: 'auth.accepted' });

    await openSettingsFromFirmwareMenu(harness);
    for (let index = 0; index < 3; index += 1) harness.fire(scrollDown());
    harness.fire(rightPress()); // the row index that used to be "Repeat last reply"
    expect(harness.menu.isOpen()).toBe(true);
    expect(harness.hud.showDefault).not.toHaveBeenCalled();
    harness.fire(leftDoublePress());

    // Opening Settings announces the surface and closing announces Home.
    // Both are load-bearing: services/capabilities.py retires capability
    // ownership on the surface messages, so dropping them strands the
    // previous page. Nothing rides between them.
    await vi.waitFor(() => expect(sockets[0]?.sent).toEqual([
      JSON.stringify({ kind: 'auth', token: null }),
      JSON.stringify({
        kind: 'input',
        event: { containerName: 'capability-lifecycle', selectedName: 'surface-settings' },
      }),
      JSON.stringify({
        kind: 'input',
        event: { containerName: 'capability-lifecycle', selectedName: 'surface-home' },
      }),
    ]));
    expect(harness.hud.showDefault).toHaveBeenCalledTimes(1);
    expect(harness.menu.isOpen()).toBe(false);
  });

  it('a press on the retired Reconnect row cannot recycle the link', async () => {
    const harness = rig();
    harness.link.connect();
    sockets[0]?.open();
    sockets[0]?.receive({ kind: 'auth.accepted' });

    await openSettingsFromFirmwareMenu(harness);
    for (let index = 0; index < 4; index += 1) harness.fire(scrollDown());
    harness.fire(rightPress()); // the row index that used to be "Reconnect JOAT"

    expect(sockets).toHaveLength(1);
    expect(sockets[0]?.closed).toBe(false);
    expect(harness.link.linkState).toBe('connected');
    expect(sockets[0]?.sent).toEqual([
      JSON.stringify({ kind: 'auth', token: null }),
      JSON.stringify({
        kind: 'input',
        event: { containerName: 'capability-lifecycle', selectedName: 'surface-settings' },
      }),
    ]);
    expect(harness.menu.isOpen()).toBe(true);
  });

  it('opens the phone-controls Settings surface and returns Home on left-double', async () => {
    const harness = rig();

    await openSettingsFromFirmwareMenu(harness);
    expect(harness.lists[0]?.items).toEqual([
      'Manage listening and Mac connections',
      'in JOAT’s phone controls in Even Hub.',
    ]);
    expect(harness.lists[0]?.options).toEqual({
      title: 'PHONE CONTROLS', hint: 'left x2: back', selectable: false, nativeFirmwareList: true,
    });

    harness.fire(leftDoublePress());
    await vi.waitFor(() => expect(harness.hud.showDefault).toHaveBeenCalledTimes(1));
    expect(sockets).toEqual([]);
  });

  it('a scroll and press on Settings never changes the persisted policy through EventRouter', async () => {
    const harness = rig();

    await openSettingsFromFirmwareMenu(harness);
    expect(harness.settings.resumePolicy).toBe('confirm');

    harness.fire(scrollDown());
    harness.fire(rightPress()); // the row that used to apply "Mac decides"
    harness.fire(scrollDown());
    harness.fire(rightPress());

    // No cursor to move, no policy to apply, no page to leave.
    expect(harness.settings.resumePolicy).toBe('confirm');
    expect(harness.hud.showDefault).not.toHaveBeenCalled();
    expect(harness.menu.isOpen()).toBe(true);
    expect(sockets).toEqual([]);

    harness.fire(leftDoublePress());
    await vi.waitFor(() => expect(harness.hud.showDefault).toHaveBeenCalledTimes(1));
    expect(harness.settings.resumePolicy).toBe('confirm');
  });
});
