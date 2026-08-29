import { afterAll, afterEach, describe, expect, it, vi } from 'vitest';
import {
  EventSourceType,
  OsEventTypeList,
  type EvenHubEvent,
} from '@evenrealities/even_hub_sdk';
import { CapabilityMenu } from './CapabilityMenu';
import { EventRouter } from './EventRouter';
import { Settings } from './Settings';
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
  const lists: Array<{ items: string[]; options?: { title?: string; hint?: string } }> = [];
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
      options?: { title?: string; hint?: string },
    ) => {
      lists.push({ items, options });
      return true;
    }),
    showDefault: vi.fn(async () => true),
    setStatus: vi.fn(async () => true),
    setSelection: vi.fn(async () => true),
    moveSelection: vi.fn(async () => true),
    showProductivity: vi.fn(async () => true),
  } as unknown as HudPresenter;
  const link = new MacLink({ urls: ['ws://joat.test:8787/glasses'] });
  const menu = new CapabilityMenu(hud, new Settings(), link);
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
  return { fire, hud, link, lists, menu, sidebars };
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

async function openAndScrollTo(
  harness: ReturnType<typeof rig>,
  label: 'Repeat' | 'Reconnect' | 'Settings',
): Promise<void> {
  harness.fire(leftDoublePress());
  await vi.waitFor(() => expect(harness.sidebars).toHaveLength(1));
  const index = harness.sidebars[0]?.indexOf(label) ?? -1;
  expect(index).toBeGreaterThanOrEqual(0);
  for (let current = 0; current < index; current += 1) harness.fire(scrollDown());
}

describe('first-level G2 sidebar actions', () => {
  it('routes Repeat through the existing Mac-held last-reply contract', async () => {
    const harness = rig();
    harness.link.connect();
    sockets[0]?.open();
    sockets[0]?.receive({ kind: 'auth.accepted' });

    await openAndScrollTo(harness, 'Repeat');
    harness.fire(rightPress());

    await vi.waitFor(() => expect(sockets[0]?.sent).toEqual([
      JSON.stringify({ kind: 'auth', token: null }),
      JSON.stringify({
        kind: 'input',
        event: { containerName: 'sysmenu', selectedIndex: 1 },
      }),
    ]));
    expect(harness.hud.showDefault).toHaveBeenCalledTimes(1);
  });

  it('routes Reconnect through the real link recovery entry point', async () => {
    const harness = rig();
    harness.link.connect();
    sockets[0]?.open();
    sockets[0]?.receive({ kind: 'auth.accepted' });

    await openAndScrollTo(harness, 'Reconnect');
    harness.fire(rightPress());

    await vi.waitFor(() => expect(sockets).toHaveLength(2));
    expect(sockets[0]?.closed).toBe(true);
    expect(harness.link.linkState).toBe('connecting');
    sockets[1]?.open();
    expect(harness.link.linkState).toBe('connecting');
    sockets[1]?.receive({ kind: 'auth.accepted' });
    expect(harness.link.linkState).toBe('connected');
    expect(harness.hud.showDefault).toHaveBeenCalledTimes(1);
  });

  it('opens the persisted listening Settings surface and returns Home on left-double', async () => {
    const harness = rig();

    await openAndScrollTo(harness, 'Settings');
    harness.fire(rightPress());
    await vi.waitFor(() => expect(harness.lists).toHaveLength(1));
    expect(harness.lists[0]?.items.map((item) => item.replace('*', ' '))).toEqual([
      '  Resume mic: instantly',
      '  Resume mic: Mac decides',
      '  Resume mic: ask me first',
    ]);
    expect(harness.lists[0]?.options).toEqual({
      title: 'LISTENING RESUME', hint: 'tap apply · left x2 overview',
    });

    harness.fire(leftDoublePress());
    await vi.waitFor(() => expect(harness.hud.showDefault).toHaveBeenCalledTimes(1));
    expect(sockets).toEqual([]);
  });
});
