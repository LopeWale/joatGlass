import { afterEach, describe, expect, it, vi } from 'vitest';
import type { RebuildPageContainer } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import { HudPageBuilder } from './HudPageBuilder';
import { HudPresenter, setNativeOverviewForTest } from './HudPresenter';
import type { HudVisualFrame } from './HudVisualSurface';
import { NOTIFICATION_CARD_MS } from './NotificationCard';
import { ContainerId, type HudNotificationCard } from './types';

setNativeOverviewForTest(true);

/**
 * The notification card (NotificationCard.ts) as the wearer ruled it
 * (2026-10-07): it pops up over every page; a tap marks it read and shows
 * the next unread, or closes after the last; a double tap closes it unread;
 * a card that popped up on its own closes after a few seconds.
 */
function rig() {
  const rebuilds: RebuildPageContainer[] = [];
  const client = {
    require: () => ({
      rebuildPageContainer: vi.fn(async (page: RebuildPageContainer) => { rebuilds.push(page); return true; }),
      textContainerUpgrade: vi.fn(async () => true),
      updateImageRawData: vi.fn(async () => 'success'),
    }),
  } as unknown as BridgeClient;
  const visual = {
    paint: vi.fn(async (_frame: HudVisualFrame) => true),
    paintExact: vi.fn(async (_frame: HudVisualFrame, current: () => boolean) => current()),
    forgetPage: vi.fn(),
    whenIdle: vi.fn(async () => true),
    withPageTransition: vi.fn(async (replace: () => Promise<boolean>) => replace()),
  };
  const hud = new HudPresenter(client, new HudPageBuilder(), visual);
  return { hud, rebuilds };
}

const id = (n: number) => n.toString(16).padStart(32, '0');
const card = (n: number, body: string): HudNotificationCard => ({
  id: id(n), app: 'Messages', sender: `Sender ${n}`, body, at: Date.now(), urgent: false,
});
const words = (page: RebuildPageContainer | undefined, name: string) =>
  page?.textObject?.find((t) => t.containerName === name)?.content;
const last = <T>(items: readonly T[]) => items[items.length - 1];
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

afterEach(() => { vi.useRealTimers(); });

describe('notification card', () => {
  it('pops up over the page; a tap reads it and shows the next; the last tap closes', async () => {
    const r = rig();
    await r.hud.showDefault();
    r.hud.acceptNotifications([card(2, 'Second, the newest'), card(1, 'First, older')]);
    await r.hud.showText('Sender 2 · Messages\nSecond, the newest', ContainerId.Main, 3, 'notification', id(2));
    await settle();
    expect(r.hud.hasNotificationCard()).toBe(true);
    expect(words(last(r.rebuilds), 'note-body')).toContain('Second, the newest');
    expect(words(last(r.rebuilds), 'note-meta')).toContain('1/2');

    expect(r.hud.tapNotificationCard()).toBe(id(2));
    await settle();
    expect(r.hud.hasNotificationCard()).toBe(true);
    expect(words(last(r.rebuilds), 'note-body')).toContain('First, older');

    expect(r.hud.tapNotificationCard()).toBe(id(1));
    await settle();
    expect(r.hud.hasNotificationCard()).toBe(false);
    expect(last(r.rebuilds)?.textObject?.some((t) => t.containerName?.startsWith('note-'))).toBe(false);
    expect(r.hud.unreadNotifications()).toEqual([]);
    // Home came back without the card it had kept in its pane.
    expect(r.hud.hasVisibleNotification()).toBe(false);
  });

  it('a double tap closes the card and leaves it unread', async () => {
    const r = rig();
    await r.hud.showDefault();
    r.hud.acceptNotifications([card(1, 'Still unread')]);
    await r.hud.showText('Sender 1 · Messages\nStill unread', ContainerId.Main, 3, 'notification', id(1));
    await settle();
    expect(await r.hud.closeNotificationCard()).toBe(true);
    expect(r.hud.hasNotificationCard()).toBe(false);
    expect(r.hud.unreadNotifications().map((c) => c.id)).toEqual([id(1)]);
  });

  it('a card that popped up on its own closes after its display time', async () => {
    vi.useFakeTimers();
    const r = rig();
    await r.hud.showDefault();
    r.hud.acceptNotifications([card(1, 'Hello')]);
    await r.hud.showText('Sender 1 · Messages\nHello', ContainerId.Main, 3, 'notification', id(1));
    await vi.advanceTimersByTimeAsync(10);
    expect(r.hud.hasNotificationCard()).toBe(true);
    await vi.advanceTimersByTimeAsync(NOTIFICATION_CARD_MS);
    expect(r.hud.hasNotificationCard()).toBe(false);
  });

  it('opened from Home it stays until the wearer is done', async () => {
    vi.useFakeTimers();
    const r = rig();
    await r.hud.showDefault();
    r.hud.acceptNotifications([card(1, 'Hello')]);
    expect(await r.hud.browseNotifications()).toBe(true);
    await vi.advanceTimersByTimeAsync(NOTIFICATION_CARD_MS * 3);
    expect(r.hud.hasNotificationCard()).toBe(true);
  });

  it('pops up over a list too, its rows under the card left out', async () => {
    const r = rig();
    await r.hud.showList(Array.from({ length: 8 }, (_, i) => `Row ${i + 1}`), {
      title: 'NEWS', hint: 'tap: open', selectable: true, nativeFirmwareList: true,
    });
    r.hud.acceptNotifications([card(1, 'Hello')]);
    await r.hud.showText('Sender 1 · Messages\nHello', ContainerId.Main, 3, 'notification', id(1));
    await settle();
    const page = last(r.rebuilds)!;
    expect(words(page, 'note-body')).toContain('Hello');
    const rows = page.listObject?.[0]?.itemContainer?.itemName ?? [];
    expect(rows.length).toBeGreaterThan(0);
    expect(rows).not.toContain('Row 1');
    // The list beneath gives up the input to the card.
    expect(page.listObject?.[0]?.isEventCapture).toBe(0);
    // No selection box on a row the wearer cannot pick.
    expect(page.listObject?.[0]?.itemContainer?.isItemSelectBorderEn).toBe(0);
  });

  it('is drawn whole once the full list arrives after the Mac\'s short line', async () => {
    const r = rig();
    await r.hud.showDefault();
    await r.hud.showText('Sender 1 · Messages\nHello th…', ContainerId.Main, 3, 'notification', id(1));
    await settle();
    expect(words(last(r.rebuilds), 'note-body')).toContain('Hello th…');
    r.hud.acceptNotifications([card(1, 'Hello there, the whole message is here now.')]);
    await settle();
    expect(words(last(r.rebuilds), 'note-body')).toContain('the whole message is here now.');
  });
});
