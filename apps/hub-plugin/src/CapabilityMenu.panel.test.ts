import { describe, expect, it, vi } from 'vitest';
import { OsEventTypeList } from '@evenrealities/even_hub_sdk';
import { CapabilityMenu, pomodoroCountdownRow } from './CapabilityMenu';
import { PanelRail } from './PanelRail';
import { Settings } from './Settings';
import type { HudPresenter } from './HudPresenter';
import type { MacLink } from './MacLink';
import type { PanelTab } from './types';

/**
 * The overview's tab rail, through the menu: a resting swipe moves the
 * rail, a click expands the selected tab from the plugin's cache, a row
 * tap carries the cached revision, Back walks one level, and long press
 * refreshes only at the top of an expanded tab.
 */

function rig(activeTab: PanelTab = 'notifications') {
  const rail = new PanelRail();
  rail.setActive(activeTab);
  const lists: Array<{ items: string[]; options?: Record<string, unknown> }> = [];
  const steps: number[] = [];
  const sent: Array<Record<string, unknown>> = [];
  let defaults = 0;
  let listResult = true;
  /** Runs while a paint is in flight — how a push lands mid-transition. */
  let duringPaint: (() => void) | undefined;
  const hud = {
    rail,
    activeTab: () => rail.active(),
    stepTab: vi.fn(async (delta: number) => {
      steps.push(delta);
      return rail.step(delta);
    }),
    showList: vi.fn(async (items: string[], options?: Record<string, unknown>) => {
      lists.push({ items: [...items], options });
      const hook = duringPaint;
      duringPaint = undefined;
      hook?.();
      return listResult;
    }),
    setStatus: vi.fn(async () => true),
    showDefault: vi.fn(async () => { defaults += 1; return true; }),
    unread: [] as Array<{ id: string }>,
    unreadNotifications: () => hud.unread,
    browseNotifications: vi.fn(async () => true),
  } as unknown as HudPresenter & { unread: Array<{ id: string }>; browseNotifications: ReturnType<typeof vi.fn> };
  const link = { send: (m: Record<string, unknown>) => sent.push(m) } as unknown as MacLink;
  const menu = new CapabilityMenu(hud, new Settings(), link);
  return {
    menu, hud, rail, lists, steps, sent,
    // Surface announcements ("which page owns the glass") travel the same
    // uplink as content requests but mean the opposite thing: they RETIRE
    // ownership on the Mac. A test asking "did this gesture ask the Mac for
    // anything?" must count requests, not every frame of bookkeeping.
    requests: () => sent.filter(
      (message) => (message.event as { containerName?: string } | undefined)
        ?.containerName !== 'capability-lifecycle',
    ),
    defaults: () => defaults,
    failNextList: () => { listResult = false; },
    whilePainting: (hook: () => void) => { duringPaint = hook; },
  };
}

const newsPush = (revision: number, rows: string[]) => ({
  kind: 'hud.panel' as const, tab: 'news' as const, revision, rows,
});

const pomodoroPush = (revision: number) => ({
  kind: 'hud.panel' as const, tab: 'pomodoro' as const, revision,
  rows: ['Pause', 'Skip to break', 'Reset'],
  pomodoro: {
    phase: 'focus' as const, running: true, remainingMs: 877_000,
    lengthMs: 1_500_000, completed: 1, endsAtUnixMs: Date.now() + 877_000,
  },
});

describe('CapabilityMenu — the overview tab rail', () => {
  it('expands News as a firmware list and returns to it from an article on row zero, with no cursor restoration', async () => {
    const { menu, rail, lists, requests, defaults } = rig('news');
    rail.accept(newsPush(7, ['Story 01', 'Story 02']));
    await menu.openPanelTab('news');
    expect(lists[lists.length - 1]?.options).toMatchObject({ title: 'NEWS', nativeFirmwareList: true, railTab: 'news' });
    // The firmware reports the tapped row; the request carries the cached revision.
    menu.handleList({ eventType: OsEventTypeList.CLICK_EVENT, currentSelectItemIndex: 1 } as never);
    expect(requests()).toHaveLength(1);
    expect((requests()[0]?.event as { selectedIndex?: number; requestId?: string })).toMatchObject({ selectedIndex: 1, requestId: 'panel-news-7' });
    await menu.presentList('NEWS · ARTICLE', ['1/1 · Story 02', 'Complete article.'], 'panel-news-7', false);
    expect(lists[lists.length - 1]?.options).toMatchObject({ title: 'NEWS · ARTICLE', nativeTextPage: true });

    await menu.toggle();
    // The firmware has no cursor-setting API: the list returns on row zero.
    expect(lists[lists.length - 1]?.options).toMatchObject({ title: 'NEWS', nativeFirmwareList: true });
    expect(menu.isOpen()).toBe(true);
    expect(defaults()).toBe(0);
    await menu.toggle();
    expect(menu.isOpen()).toBe(false);
    expect(defaults()).toBe(1);
  });

  it('a resting swipe moves the rail and is consumed even at the ends', () => {
    const { menu, steps } = rig();
    expect(menu.handleOverviewSwipe(OsEventTypeList.SCROLL_TOP_EVENT)).toBe(true);
    expect(menu.handleOverviewSwipe(OsEventTypeList.SCROLL_BOTTOM_EVENT)).toBe(true);
    expect(menu.handleOverviewSwipe(OsEventTypeList.CLICK_EVENT)).toBe(false);
    expect(steps).toEqual([-1, 1]);
  });

  it('a click on Notifications with unread opens the newest card, not the list', async () => {
    const { menu, hud, lists, requests } = rig();
    hud.unread = [{ id: 'a'.repeat(32) }];
    await menu.openActiveTab();
    expect(hud.browseNotifications).toHaveBeenCalledTimes(1);
    expect(lists).toHaveLength(0);
    expect(requests()).toEqual([]);
  });

  it('a click on Notifications still asks the Mac; on News it expands the cache', async () => {
    const { menu, rail, lists, sent, requests } = rig();
    await menu.openActiveTab();
    expect(lists[0]?.options?.title).toBe('NOTIFICATIONS');
    expect(sent.some((m) => (m.event as { containerName?: string }).containerName === 'capmenu')).toBe(true);

    rail.accept(newsPush(3, ['[2m] Rates hold · BBC', '[5m] Rover lands · NPR']));
    rail.setActive('news');
    sent.length = 0;
    await menu.openActiveTab();
    const page = lists[lists.length - 1]!;
    expect(page.items).toEqual(['[2m] Rates hold · BBC', '[5m] Rover lands · NPR']);
    expect(page.options).toMatchObject({ title: 'NEWS', selectable: true, railTab: 'news' });
    expect(requests()).toEqual([]); // painted from memory, no round trip
    expect(menu.isOpen()).toBe(true);
    expect(menu.isPanelExpanded()).toBe(true);
    // The overview swipe is no longer a rail swipe: the page owns scrolls.
    expect(menu.handleOverviewSwipe(OsEventTypeList.SCROLL_BOTTOM_EVENT)).toBe(false);
  });

  it('an empty tab expands to an honest, unselectable line', async () => {
    const { menu, lists } = rig('search');
    await menu.openPanelTab('search');
    const page = lists[0]!;
    expect(page.items).toEqual(['Search not loaded — hold to retry.']);
    expect(page.options).toMatchObject({ title: 'SEARCH', selectable: false });
    expect(menu.handleClick()).toBe(true); // consumed, nothing to open
  });

  it('an empty pomodoro tab names the gesture that fills it, never a phrase to say', async () => {
    const { menu, lists } = rig('pomodoro');
    await menu.openPanelTab('pomodoro');
    const page = lists[0]!;
    // The countdown row falls back to a plain heading: nothing to count yet.
    expect(page.items).toEqual(['Focus timer', 'Timer not loaded — hold to retry.']);
    expect(page.options).toMatchObject({ title: 'POMODORO', selectable: false, nativeFirmwareList: true });
    expect(page.items.join(' ')).not.toMatch(/Say “JOAT/u);
    expect(menu.handleClick()).toBe(true);
  });

  it('the countdown row is a plain label without a snapshot and the configured focus length when idle', () => {
    const now = 1_700_000_000_000;
    expect(pomodoroCountdownRow(undefined, now)).toBe('Focus timer');
    expect(pomodoroCountdownRow({
      phase: 'idle', running: false, remainingMs: 1_800_000, lengthMs: 1_800_000, completed: 0,
    }, now)).toBe('Ready · 30 min focus');
    expect(pomodoroCountdownRow({
      phase: 'focus', running: true, remainingMs: 877_000, lengthMs: 1_500_000, completed: 1,
      endsAtUnixMs: now + 877_000,
    }, now)).toBe('Focus · block 2 of 4 · 15 min left');
  });

  it('a row tap carries the cached revision, so a stale tap is refusable', async () => {
    const { menu, rail, sent } = rig('news');
    rail.accept(newsPush(7, ['[2m] Rates hold · BBC', '[5m] Rover lands · NPR']));
    await menu.openPanelTab('news');
    expect(menu.handleList({ eventType: OsEventTypeList.CLICK_EVENT, currentSelectItemIndex: 1 } as never)).toBe(true);
    const tap = sent.find((m) => (m.event as { containerName?: string }).containerName === 'capdata');
    expect(tap?.event).toMatchObject({
      containerName: 'capdata', selectedName: 'NEWS', selectedIndex: 1, requestId: 'panel-news-7',
    });
  });

  it('the opened article is a NEWS page: Back returns to the cached list, then the overview', async () => {
    const { menu, rail, lists, sent } = rig('news');
    rail.accept(newsPush(7, ['[2m] Rates hold · BBC', '[5m] Rover lands · NPR']));
    await menu.openPanelTab('news');
    menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    menu.handleClick();
    const opened = await menu.presentList(
      'NEWS · ARTICLE',
      ['Rover lands · NPR · 5m', 'The rover touched down at dawn.'],
      'panel-news-7',
      false,
      'down more · 2x tap: back',
    );
    expect(opened).toBe(true);
    expect(menu.isPanelExpanded()).toBe(false); // inside the reader
    // A boundary swipe with a Mac-authored "down more" hint pages the reader.
    sent.length = 0;
    expect(menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT)).toBe(true);
    expect(sent[0]?.event).toMatchObject({
      containerName: 'cappage', selectedName: 'NEWS',
      eventType: OsEventTypeList.SCROLL_BOTTOM_EVENT, requestId: 'panel-news-7',
    });
    // "up previous" was not offered on page one, so an upward swipe is not a request.
    sent.length = 0;
    menu.handleScroll(OsEventTypeList.SCROLL_TOP_EVENT);
    expect(sent).toEqual([]);

    // The reader keeps the tab's rail lit…
    const reader = lists[lists.length - 1]!;
    expect(reader.options).toMatchObject({ title: 'NEWS · ARTICLE', railTab: 'news' });

    await menu.toggle();
    const back = lists[lists.length - 1]!;
    expect(back.options?.title).toBe('NEWS');
    expect(back.items).toHaveLength(2);
    // …and so does the list Back restores — it vanished here once.
    expect(back.options).toMatchObject({ railTab: 'news' });
    expect(menu.isPanelExpanded()).toBe(true);
    await menu.toggle();
    expect(menu.isOpen()).toBe(false);
  });

  it('a stale article for an older revision is dropped, not painted', async () => {
    const { menu, rail, lists } = rig('news');
    rail.accept(newsPush(7, ['[2m] Rates hold · BBC']));
    await menu.openPanelTab('news');
    const painted = lists.length;
    const shown = await menu.presentList('NEWS · ARTICLE', ['old', 'body'], 'panel-news-6', false);
    expect(shown).toBe(false);
    expect(lists).toHaveLength(painted);
  });

  it('long press at the top of a tab asks the Mac to refresh it; deeper it does nothing', async () => {
    const { menu, rail, sent } = rig('news');
    rail.accept(newsPush(2, ['[2m] Rates hold · BBC']));
    await menu.openPanelTab('news');
    sent.length = 0;
    menu.refreshPanel();
    expect(sent[0]?.event).toMatchObject({
      containerName: 'caprefresh', selectedName: 'NEWS', requestId: 'panel-news-2',
    });
    menu.handleClick();
    await menu.presentList('NEWS · ARTICLE', ['Rates hold', 'body'], 'panel-news-2', false);
    sent.length = 0;
    menu.refreshPanel();
    expect(sent).toEqual([]);
  });

  it('the pomodoro page is expanded but not refreshable — the hold stays the mic', async () => {
    const { menu, rail, sent } = rig('pomodoro');
    rail.accept(pomodoroPush(4));
    await menu.openPanelTab('pomodoro');
    sent.length = 0;
    // Structurally a panel top level…
    expect(menu.isPanelExpanded()).toBe(true);
    // …but there is nothing to re-fetch: the Mac pushes every transition
    // and the countdown ticks locally, so the gesture belongs to the mic.
    expect(menu.isPanelRefreshable()).toBe(false);
    menu.refreshPanel();
    expect(sent).toEqual([]);
  });

  it('a fresh push rebuilds the expanded firmware tab in place', async () => {
    const { menu, rail, lists, sent } = rig('news');
    rail.accept(newsPush(2, ['[2m] A · BBC', '[3m] B · BBC', '[4m] C · BBC']));
    await menu.openPanelTab('news');
    rail.accept(newsPush(3, ['[1m] New · NPR', '[3m] A · BBC']));
    expect(await menu.presentPanelContent('news')).toBe(true);
    const page = lists[lists.length - 1]!;
    expect(page.items).toEqual(['[1m] New · NPR', '[3m] A · BBC']);
    expect(page.options).toMatchObject({ nativeFirmwareList: true });
    // The next tap carries the revision that is on the glass now.
    sent.length = 0;
    menu.handleList({ eventType: OsEventTypeList.CLICK_EVENT, currentSelectItemIndex: 1 } as never);
    const tap = sent.find((m) => (m.event as { containerName?: string }).containerName === 'capdata');
    expect(tap?.event).toMatchObject({ requestId: 'panel-news-3', selectedIndex: 1 });
    // A push for a tab that is not on the glass is cache-only.
    expect(await menu.presentPanelContent('search')).toBe(false);
  });

  it('the pomodoro page leads with a whole-minute countdown row the tap mapping skips, rebuilt on the minute tick', async () => {
    const { menu, rail, lists, sent, hud } = rig('pomodoro');
    rail.accept(pomodoroPush(4));
    await menu.openPanelTab('pomodoro');
    const page = lists[lists.length - 1]!;
    expect(page.options).toMatchObject({ title: 'POMODORO', nativeFirmwareList: true });
    expect(page.items[0]).toMatch(/min left$|^Ready/u);
    expect(page.items.slice(1)).toEqual(['Pause', 'Skip to break', 'Reset']);
    sent.length = 0;
    // The countdown row is inert; the Mac's rows keep their own indexes.
    menu.handleList({ eventType: OsEventTypeList.CLICK_EVENT, currentSelectItemIndex: undefined } as never);
    expect(sent.filter((m) => (m.event as { containerName?: string }).containerName === 'capdata')).toEqual([]);
    menu.handleList({ eventType: OsEventTypeList.CLICK_EVENT, currentSelectItemIndex: 1 } as never);
    const tap = sent.find((m) => (m.event as { containerName?: string }).containerName === 'capdata');
    expect(tap?.event).toMatchObject({ selectedName: 'POMODORO', selectedIndex: 0, requestId: 'panel-pomodoro-4' });
    // The presenter's minute tick asks the menu for a rebuilt page.
    const before = lists.length;
    (hud as unknown as { onNativePanelTick?: () => void }).onNativePanelTick?.();
    await vi.waitFor(() => expect(lists.length).toBe(before + 1));
    expect(lists[lists.length - 1]?.options).toMatchObject({ title: 'POMODORO', nativeFirmwareList: true });
  });

  it('a push that lands while the page paints is painted, not paired with old rows', async () => {
    const { menu, rail, lists, sent, whilePainting } = rig('news');
    rail.accept(newsPush(7, ['[2m] Old · BBC']));
    whilePainting(() => rail.accept(newsPush(8, ['[0m] New · NPR', '[2m] Old · BBC'])));
    await menu.openPanelTab('news');
    const page = lists[lists.length - 1]!;
    expect(page.items).toEqual(['[0m] New · NPR', '[2m] Old · BBC']);
    menu.handleClick();
    const tap = sent.find((m) => (m.event as { containerName?: string }).containerName === 'capdata');
    expect(tap?.event).toMatchObject({ requestId: 'panel-news-8', selectedIndex: 0 });
  });

  it('Back from an article lands on the rows the Mac pushed meanwhile, keeping the row', async () => {
    const { menu, rail, lists, sent } = rig('news');
    rail.accept(newsPush(2, ['[2m] A · BBC', '[3m] B · BBC', '[4m] C · BBC']));
    await menu.openPanelTab('news');
    menu.handleList({ eventType: OsEventTypeList.CLICK_EVENT, currentSelectItemIndex: 2 } as never);
    await menu.presentList('NEWS · ARTICLE', ['C', 'body'], 'panel-news-2', false, '2x tap: back');
    // The poller pushes while the wearer reads: cached only, the reader stays.
    rail.accept(newsPush(3, ['[0m] Fresh · NPR', '[3m] A · BBC']));
    expect(await menu.presentPanelContent('news')).toBe(false);
    expect(lists[lists.length - 1]?.options?.title).toBe('NEWS · ARTICLE');

    await menu.toggle();
    const back = lists[lists.length - 1]!;
    expect(back.options).toMatchObject({ title: 'NEWS', railTab: 'news', nativeFirmwareList: true });
    expect(back.items).toEqual(['[0m] Fresh · NPR', '[3m] A · BBC']);
    sent.length = 0;
    menu.handleList({ eventType: OsEventTypeList.CLICK_EVENT, currentSelectItemIndex: undefined } as never);
    expect((sent[0]?.event as { requestId?: string; selectedIndex?: number })).toMatchObject({ requestId: 'panel-news-3', selectedIndex: 0 });
    await menu.toggle();
    expect(menu.isOpen()).toBe(false);
  });

  it('a failed paint closes back to the overview instead of trapping input', async () => {
    const { menu, rail, failNextList, defaults } = rig('news');
    rail.accept(newsPush(1, ['[2m] A · BBC']));
    failNextList();
    await menu.openPanelTab('news');
    expect(menu.isOpen()).toBe(false);
    expect(defaults()).toBe(1);
  });
});
