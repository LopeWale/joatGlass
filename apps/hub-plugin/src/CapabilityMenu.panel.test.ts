import { describe, expect, it, vi } from 'vitest';
import { OsEventTypeList } from '@evenrealities/even_hub_sdk';
import { CapabilityMenu } from './CapabilityMenu';
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

function rig(activeTab: PanelTab = 'notifications', restorationResult: boolean | Promise<boolean> = true) {
  const rail = new PanelRail();
  rail.setActive(activeTab);
  const lists: Array<{ items: string[]; options?: Record<string, unknown> }> = [];
  const steps: number[] = [];
  const selections: number[] = [];
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
    setSelection: vi.fn(async (index: number) => {
      selections.push(index);
      return true;
    }),
    restoreSelection: vi.fn(async (index: number) => {
      selections.push(index);
      return restorationResult;
    }),
    moveSelection: vi.fn(async () => true),
    setStatus: vi.fn(async () => true),
    showDefault: vi.fn(async () => { defaults += 1; return true; }),
    // The real presenter answers true whenever a visual surface exists and
    // the list is not a native firmware one. These panels drive the custom
    // cursor (setSelection/moveSelection/restoreSelection), so that is the
    // configuration under test. Two CapabilityMenu call sites invoke this
    // WITHOUT `?.`, so a double that omits it does not fall back — it
    // throws, and every test in the file dies on the first selection.
    usesCustomListSelection: () => true,
  } as unknown as HudPresenter;
  const link = { send: (m: Record<string, unknown>) => sent.push(m) } as unknown as MacLink;
  const menu = new CapabilityMenu(hud, new Settings(), link);
  return {
    menu, rail, lists, steps, selections, sent,
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
  it.each([true, false])('keeps Home after a pending News cursor restoration is cancelled (late result=%s)', async (lateResult) => {
    let finishSelection!: (painted: boolean) => void;
    const painting = new Promise<boolean>((resolve) => { finishSelection = resolve; });
    const { menu, rail, selections, requests, defaults } = rig('news', painting);
    rail.accept(newsPush(7, ['Story 01', 'Story 02']));
    await menu.openPanelTab('news');
    menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    menu.handleClick();
    await menu.presentList('NEWS · ARTICLE', ['Story 02', 'Complete article.'], 'panel-news-7', false);
    selections.length = 0;
    const returning = menu.toggle();
    try {
      await vi.waitFor(() => expect(selections).toContain(1));
      expect(menu.handleClick()).toBe(true);
      expect(requests()).toHaveLength(1);
      await menu.toggle();
      expect(menu.isOpen()).toBe(false);
      expect(defaults()).toBe(1);
      finishSelection(lateResult);
      await returning;
      expect(menu.isOpen()).toBe(false);
      expect(menu.handleClick()).toBe(false);
      expect(requests()).toHaveLength(1);
      expect(defaults()).toBe(1);
    } finally {
      finishSelection(lateResult);
      await returning;
      menu.releaseToLiveSession();
    }
  });

  it('closes instead of enabling News taps after a failed cursor restoration', async () => {
    const { menu, rail, requests, defaults } = rig('news', false);
    rail.accept(newsPush(7, ['Story 01', 'Story 02']));
    await menu.openPanelTab('news');
    menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    menu.handleClick();
    await menu.presentList('NEWS · ARTICLE', ['Story 02', 'Complete article.'], 'panel-news-7', false);

    await menu.toggle();

    expect(menu.isOpen()).toBe(false);
    expect(menu.handleClick()).toBe(false);
    expect(requests()).toHaveLength(1);
    expect(defaults()).toBe(1);
  });

  it('a resting swipe moves the rail and is consumed even at the ends', () => {
    const { menu, steps } = rig();
    expect(menu.handleOverviewSwipe(OsEventTypeList.SCROLL_TOP_EVENT)).toBe(true);
    expect(menu.handleOverviewSwipe(OsEventTypeList.SCROLL_BOTTOM_EVENT)).toBe(true);
    expect(menu.handleOverviewSwipe(OsEventTypeList.CLICK_EVENT)).toBe(false);
    expect(steps).toEqual([-1, 1]);
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
    expect(page.items).toEqual(['Say “JOAT, look up …” to search.']);
    expect(page.options).toMatchObject({ title: 'SEARCH', selectable: false });
    expect(menu.handleClick()).toBe(true); // consumed, nothing to open
  });

  it('a row tap carries the cached revision, so a stale tap is refusable', async () => {
    const { menu, rail, sent } = rig('news');
    rail.accept(newsPush(7, ['[2m] Rates hold · BBC', '[5m] Rover lands · NPR']));
    await menu.openPanelTab('news');
    menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    expect(menu.handleClick()).toBe(true);
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
      'down more · left x2: back',
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

  it('a fresh push repaints the expanded tab in place and keeps the row', async () => {
    const { menu, rail, lists, selections } = rig('news');
    rail.accept(newsPush(2, ['[2m] A · BBC', '[3m] B · BBC', '[4m] C · BBC']));
    await menu.openPanelTab('news');
    menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    rail.accept(newsPush(3, ['[1m] New · NPR', '[3m] A · BBC']));
    expect(await menu.presentPanelContent('news')).toBe(true);
    const page = lists[lists.length - 1]!;
    expect(page.items).toEqual(['[1m] New · NPR', '[3m] A · BBC']);
    expect(selections[selections.length - 1]).toBe(1); // clamped from row 2
    // A push for a tab that is not on the glass is cache-only.
    expect(await menu.presentPanelContent('search')).toBe(false);
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
    const { menu, rail, lists, sent, selections } = rig('news');
    rail.accept(newsPush(2, ['[2m] A · BBC', '[3m] B · BBC', '[4m] C · BBC']));
    await menu.openPanelTab('news');
    menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    menu.handleClick();
    await menu.presentList('NEWS · ARTICLE', ['C', 'body'], 'panel-news-2', false, 'left x2: back');
    // The poller pushes while the wearer reads: cached only, the reader stays.
    rail.accept(newsPush(3, ['[0m] Fresh · NPR', '[3m] A · BBC']));
    expect(await menu.presentPanelContent('news')).toBe(false);
    expect(lists[lists.length - 1]?.options?.title).toBe('NEWS · ARTICLE');

    await menu.toggle();
    const back = lists[lists.length - 1]!;
    expect(back.options).toMatchObject({ title: 'NEWS', railTab: 'news' });
    expect(back.items).toEqual(['[0m] Fresh · NPR', '[3m] A · BBC']);
    expect(selections[selections.length - 1]).toBe(1); // row 2 clamped to the new list
    sent.length = 0;
    menu.handleClick();
    expect((sent[0]?.event as { requestId?: string }).requestId).toBe('panel-news-3');
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
