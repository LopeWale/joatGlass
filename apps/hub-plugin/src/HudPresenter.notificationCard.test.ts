import { afterEach, describe, expect, it, vi } from 'vitest';
import type { RebuildPageContainer } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import { HudPageBuilder } from './HudPageBuilder';
import {
  HudPresenter,
  MAX_NOTIFICATION_STACK,
  MAX_READ_NOTIFICATION_IDS,
  NOTIFICATIONS_READ_STORAGE_KEY,
  setNativeOverviewForTest,
} from './HudPresenter';
import type { HudStatusModel } from './HudStatus';
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

  it('closeNotificationCard closes without marking read (auto-timeout), while dismissNotificationCard (2x tap) marks read, closes, and updates Home unread count', async () => {
    const r = rig();
    await r.hud.showDefault();
    await r.hud.setAmbient({ temperature: '72f', condition: 'clear', unread: 2 });
    r.hud.acceptNotifications([card(2, 'Newest unread'), card(1, 'Older unread')]);
    await r.hud.showText('Sender 2 · Messages\nNewest unread', ContainerId.Main, 3, 'notification', id(2));
    await settle();

    // Auto-timeout close leaves both unread.
    expect(await r.hud.closeNotificationCard()).toBe(true);
    expect(r.hud.hasNotificationCard()).toBe(false);
    expect(r.hud.unreadNotifications().map((c) => c.id)).toEqual([id(2), id(1)]);
    expect(words(last(r.rebuilds), 'info')).toContain('2 unread');

    // Reopen and double-tap dismiss: marks active + unread stack read, closes card, and decrements Home unread to 0 immediately!
    expect(await r.hud.browseNotifications()).toBe(true);
    await settle();
    expect(r.hud.dismissNotificationCard()).toEqual([id(2), id(1)]);
    await settle();
    expect(r.hud.hasNotificationCard()).toBe(false);
    expect(r.hud.unreadNotifications()).toEqual([]);
    expect(words(last(r.rebuilds), 'info') ?? '').not.toContain('unread');
    expect(words(last(r.rebuilds), 'pane')).not.toContain('Newest unread');
    expect(words(last(r.rebuilds), 'pane')).toContain('tap: open notifications');
  });

  it('a card that popped up on its own marks itself read and notifies the brain after completing its full display window', async () => {
    vi.useFakeTimers();
    const r = rig();
    const readIds: string[] = [];
    r.hud.onNotificationRead = (cardId) => readIds.push(cardId);
    await r.hud.showDefault();
    await r.hud.setAmbient({ temperature: '72f', condition: 'clear', unread: 2 });
    r.hud.acceptNotifications([card(2, 'Hello'), card(1, 'Earlier')]);
    await r.hud.showText('Sender 2 · Messages\nHello', ContainerId.Main, 3, 'notification', id(2));
    await vi.advanceTimersByTimeAsync(10);
    expect(r.hud.hasNotificationCard()).toBe(true);
    expect(readIds).toEqual([]);

    await vi.advanceTimersByTimeAsync(NOTIFICATION_CARD_MS);
    expect(r.hud.hasNotificationCard()).toBe(false);
    expect(readIds).toEqual([id(2)]);
    expect(r.hud.unreadNotifications().map((c) => c.id)).toEqual([id(1)]);
    expect(words(last(r.rebuilds), 'info')).toContain('1 unread');
  });

  it('marks a browsed notification card read when departing to another surface, and hydrates tombstones from Even host storage when localStorage was wiped', async () => {
    const r1 = rig();
    const readIds: string[] = [];
    let mirroredRaw = '';
    r1.hud.onNotificationRead = (cardId) => readIds.push(cardId);
    r1.hud.onPersistReadNotifications = (encoded) => { mirroredRaw = encoded; };

    await r1.hud.showDefault();
    r1.hud.acceptNotifications([card(2, 'Read on browse'), card(1, 'Still unread')]);
    expect(await r1.hud.browseNotifications()).toBe(true);
    await settle();
    expect(r1.hud.hasNotificationCard()).toBe(true);

    // Wearer opens Messages over the browsed notification without tapping the card again:
    await r1.hud.showList(['+ New message', '[Now] Sender 2 · Read on browse'], {
      title: 'MESSAGES', hint: 'tap: select', selectable: true, nativeFirmwareList: true,
    });
    await settle();
    expect(readIds).toEqual([id(2)]);
    expect(r1.hud.unreadNotifications().map((c) => c.id)).toEqual([id(1)]);
    expect(mirroredRaw).toContain(id(2));

    // Simulate plugin reopen where WKWebView wiped localStorage, hydrated from Even host storage:
    const r2 = rig();
    r2.hud.hydrateReadNotifications(mirroredRaw);
    const stale = r2.hud.acceptNotifications([card(2, 'Read on browse'), card(1, 'Still unread')]);
    expect(stale).toEqual([id(2)]);
    expect(r2.hud.unreadNotifications().map((c) => c.id)).toEqual([id(1)]);
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

  it('persists bounded 200-ID read tombstones in localStorage across omissions and reconnects, and returns stale read IDs for reconciliation', async () => {
    const store = new Map<string, string>();
    const prevStorage = (globalThis as { localStorage?: unknown }).localStorage;
    (globalThis as { localStorage?: unknown }).localStorage = {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => { store.set(k, v); },
    };
    try {
      const r1 = rig();
      await r1.hud.showDefault();
      // Accept 30 cards -> sliced to 25!
      const thirty = Array.from({ length: 30 }, (_, i) => card(i + 1, `Body ${i + 1}`));
      expect(r1.hud.acceptNotifications(thirty)).toEqual([]);
      expect(r1.hud.unreadNotifications()).toHaveLength(MAX_NOTIFICATION_STACK);

      // Dismiss 1 card via tap
      expect(await r1.hud.browseNotifications()).toBe(true);
      await settle();
      expect(r1.hud.tapNotificationCard()).toBe(id(1));
      await settle();

      // Even if a transient empty list arrives, id(1) stays in notificationsReadHere!
      expect(r1.hud.acceptNotifications([])).toEqual([]);

      // Fresh HudPresenter after WebView reload / reconnect loads tombstones from localStorage
      // and flags id(1) for brain reconciliation while never showing it again!
      const r2 = rig();
      await r2.hud.showDefault();
      const stale = r2.hud.acceptNotifications([card(1, 'Body 1'), card(2, 'Body 2')]);
      expect(stale).toEqual([id(1)]);
      expect(r2.hud.unreadNotifications().map((c) => c.id)).toEqual([id(2)]);

      // A replayed hud.text for already-read id(1) is ignored and never pops up!
      await r2.hud.showText('Sender 1 · Messages\nBody 1', ContainerId.Main, 3, 'notification', id(1));
      await settle();
      expect(r2.hud.hasNotificationCard()).toBe(false);

      // Verify 200-ID FIFO cap in localStorage
      const seedIds = Array.from({ length: 205 }, (_, i) => (i + 100).toString(16).padStart(32, '0'));
      store.set(NOTIFICATIONS_READ_STORAGE_KEY, JSON.stringify(seedIds));
      const r3 = rig();
      const staleCap = r3.hud.acceptNotifications([
        { id: seedIds[0]!, app: 'Mail', sender: 'Oldest', body: 'Evicted', at: Date.now(), urgent: false },
        { id: seedIds[5]!, app: 'Mail', sender: 'Kept', body: 'Still tombstoned', at: Date.now(), urgent: false },
      ]);
      expect(staleCap).toEqual([seedIds[5]!]);
      expect(MAX_READ_NOTIFICATION_IDS).toBe(200);
    } finally {
      if (prevStorage === undefined) delete (globalThis as { localStorage?: unknown }).localStorage;
      else (globalThis as { localStorage?: unknown }).localStorage = prevStorage;
    }
  });

  it('resetNotificationsForBrainSwitch clears stack and notificationsSynced so unread counts never bleed across brains', async () => {
    const r = rig();
    await r.hud.showDefault();
    await r.hud.setAmbient({ temperature: '72f', condition: 'clear', unread: 3 });
    r.hud.acceptNotifications([card(1, 'A'), card(2, 'B'), card(3, 'C')]);
    await settle();
    expect(r.hud.unreadNotifications()).toHaveLength(3);

    r.hud.resetNotificationsForBrainSwitch();
    expect(r.hud.unreadNotifications()).toEqual([]);
    await r.hud.showDefault();
    expect(words(last(r.rebuilds), 'info') ?? '').not.toContain('unread');
  });

  it('advances elapsed time on the popup notification card on minute status ticks without freezing at Now', async () => {
    vi.useFakeTimers();
    const baseTime = new Date('2026-11-22T16:00:00Z').getTime();
    vi.setSystemTime(baseTime);
    const r = rig();
    await r.hud.showDefault();
    r.hud.acceptNotifications([
      { id: id(1), app: 'Messages', sender: 'Casey', body: 'Running 5 minutes late', at: baseTime - 10_000, urgent: false },
    ]);
    expect(await r.hud.browseNotifications()).toBe(true);
    await vi.advanceTimersByTimeAsync(10);
    expect(words(last(r.rebuilds), 'note-meta')).toBe('Now');

    // Advance 5 minutes and tick Chrome's status model -> card updates to '5m ago'!
    vi.setSystemTime(baseTime + 5 * 60_000);
    const model: HudStatusModel = { clock: '4:05', link: 'connected', mic: true, battery: 90, charging: false };
    await r.hud.setStatusModel(model);
    await vi.advanceTimersByTimeAsync(10);
    expect(words(last(r.rebuilds), 'note-meta')).toBe('5m ago');
  });
});
