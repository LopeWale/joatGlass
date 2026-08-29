import { describe, expect, it, vi } from 'vitest';
import { OsEventTypeList } from '@evenrealities/even_hub_sdk';
import { CapabilityMenu, isActionLifecycleTitle } from './CapabilityMenu';
import { Settings } from './Settings';
import type { HudPresenter } from './HudPresenter';
import type { MacLink } from './MacLink';
import type { ProductivityHudModel } from './types';

/**
 * The wearer's report from hardware: double-tap opened "a menu modal, but
 * not the one we designed", and there was no back gesture. These tests pin
 * the designed control-center: capability rows first, settings one level
 * down, and double-tap ALWAYS means back once anything is open.
 */

type PaintResult = boolean | Promise<boolean>;

function rig(
  sidebarResult: boolean | boolean[] = true,
  listResult: PaintResult | PaintResult[] = true,
) {
  const sidebars: string[][] = [];
  const lists: string[][] = [];
  const listOptions: Array<{ title?: string; hint?: string } | undefined> = [];
  const statuses: string[] = [];
  const operations: string[] = [];
  const selections: number[] = [];
  const moves: number[] = [];
  const sent: Array<Record<string, unknown>> = [];
  let defaults = 0;
  let sidebarCall = 0;
  let listCall = 0;
  const hud = {
    showSidebar: vi.fn(async (items: string[]) => {
      operations.push('sidebar');
      sidebars.push(items);
      const result = Array.isArray(sidebarResult)
        ? (sidebarResult[sidebarCall] ?? true)
        : sidebarResult;
      sidebarCall += 1;
      return result;
    }),
    showList: vi.fn(async (
      items: string[], options?: { title?: string; hint?: string },
    ) => {
      operations.push('list');
      lists.push(items);
      listOptions.push(options);
      const result = Array.isArray(listResult)
        ? (listResult[listCall] ?? true)
        : listResult;
      listCall += 1;
      return result;
    }),
    setStatus: vi.fn(async (t: string) => {
      operations.push('status');
      statuses.push(t);
      return true;
    }),
    setSelection: vi.fn(async (index: number) => {
      selections.push(index);
      return true;
    }),
    moveSelection: vi.fn(async (delta: number) => {
      moves.push(delta);
      return true;
    }),
    showProductivity: vi.fn(async () => {
      operations.push('productivity');
      return true;
    }),
    showDefault: vi.fn(async () => { defaults += 1; return true; }),
  } as unknown as HudPresenter;
  const link = { send: (m: Record<string, unknown>) => sent.push(m) } as unknown as MacLink;
  const menu = new CapabilityMenu(hud, new Settings(), link);
  return {
    menu, sidebars, lists, listOptions, operations, selections, moves, statuses, sent,
    defaultsCount: () => defaults,
  };
}

const click = (index: number) =>
  ({ eventType: OsEventTypeList.CLICK_EVENT, currentSelectItemIndex: index }) as never;
const clickRow0 = () => ({ eventType: undefined, currentSelectItemIndex: undefined }) as never;

function latestRequestId(sent: Array<Record<string, unknown>>): string {
  const request = [...sent].reverse().find((message) => {
    const event = message.event as { containerName?: string } | undefined;
    return message.kind === 'input' && event?.containerName === 'capmenu';
  });
  return ((request?.event as { requestId?: string } | undefined)?.requestId) ?? '';
}

const rows = (count: number) => Array.from({ length: count }, (_, index) => `Row ${index + 1}`);

describe('CapabilityMenu', () => {
  it('recognizes only the explicit safe-action lifecycle namespace', () => {
    expect(isActionLifecycleTitle('ACTION · VERIFYING')).toBe(true);
    expect(isActionLifecycleTitle('ACTION · DONE')).toBe(true);
    expect(isActionLifecycleTitle('ACTIVITY')).toBe(false);
  });

  it('double-tap from home exposes one unified Planning entry in the wearable sidebar', async () => {
    const { menu, sidebars, lists, operations } = rig();
    await menu.toggle();
    expect(sidebars[0]).toEqual([
      'Messages', 'Planning', 'Navigation', 'Brainstorm',
      'Notifications', 'QuickNotes', 'Agent Terminal', 'Activity',
      'Repeat', 'Reconnect', 'Settings',
    ]);
    expect(lists).toEqual([]);
    expect(operations).toEqual(['sidebar']);
  });

  it('stays closed when the firmware rejects the sidebar rebuild', async () => {
    const { menu, sidebars } = rig(false);
    await menu.toggle();
    expect(sidebars).toHaveLength(1);
    expect(menu.isOpen()).toBe(false);
  });

  it('picking Messages removes the sidebar into a full-focus loading screen before asking the Mac', async () => {
    const { menu, lists, listOptions, operations, sent } = rig();
    await menu.toggle();
    expect(menu.handleList(clickRow0())).toBe(true); // protobuf zero-drop: row 0
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(lists[0]).toEqual(['Loading recent messages…']);
    expect(listOptions[0]).toEqual({ title: 'MESSAGES', hint: 'left x2 overview' });
    expect(operations).toEqual(['sidebar', 'list']);
    expect(sent[0]).toMatchObject({
      kind: 'input',
      event: { containerName: 'capmenu', eventType: 0, selectedIndex: 0, selectedName: 'Messages' },
    });
    expect(latestRequestId(sent)).toMatch(/^hud-request-/);
    // In the data view, list events are the Mac's business, not the menu's.
    expect(menu.handleList(click(1))).toBe(false);
  });

  it('moves the custom sidebar selection with text-capture scrolls and opens that row on click', async () => {
    const { menu, selections, lists, listOptions } = rig();
    await menu.toggle();
    expect(menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT)).toBe(true);
    expect(selections).toEqual([1]);
    expect(menu.handleClick()).toBe(true);
    await vi.waitFor(() => expect(lists).toHaveLength(1));
    expect(lists[0]).toEqual(['Loading recent planning…']);
    expect(listOptions[0]?.title).toBe('PLANNING');
  });

  it('keeps Navigation visible in the first-level wearable sidebar and opens its destination handoff', async () => {
    const { menu, sidebars, selections, lists, listOptions, sent } = rig();
    await menu.toggle();

    expect(sidebars[0]).toEqual([
      'Messages', 'Planning', 'Navigation', 'Brainstorm',
      'Notifications', 'QuickNotes', 'Agent Terminal', 'Activity',
      'Repeat', 'Reconnect', 'Settings',
    ]);
    for (let index = 0; index < 2; index += 1) {
      menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    }
    expect(selections).toEqual([1, 2]);
    expect(menu.handleClick()).toBe(true);

    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(lists[0]).toEqual(['Loading recent navigation…']);
    expect(listOptions[0]).toEqual({ title: 'NAVIGATION', hint: 'left x2 overview' });
    expect(sent[0]).toMatchObject({
      kind: 'input',
      event: {
        containerName: 'capmenu',
        selectedIndex: 2,
        selectedName: 'Navigation',
      },
    });
  });

  it('scrolls one continuous wearable list to Agent Terminal without a More submenu', async () => {
    const { menu, selections, lists, listOptions, sent } = rig();
    await menu.toggle();
    for (let index = 0; index < 6; index += 1) {
      menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    }
    expect(selections).toEqual([1, 2, 3, 4, 5, 6]);
    expect(menu.handleClick()).toBe(true);
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(lists[0]).toEqual(['Loading recent agent terminal…']);
    expect(listOptions[0]?.title).toBe('AGENT TERMINAL');
    expect(sent[0]).toMatchObject({
      kind: 'input',
      event: {
        containerName: 'capmenu', eventType: 0, selectedIndex: 6, selectedName: 'Agent Terminal',
      },
    });
  });

  it('keeps rows four and five reachable after the Mac replaces the loading view', async () => {
    const { menu, moves, sent } = rig();
    await menu.toggle();
    menu.handleList(clickRow0());
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    await menu.presentList('MESSAGES', rows(5), latestRequestId(sent), true);

    expect(menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT)).toBe(true);
    expect(moves).toEqual([1]);
  });

  it('clicking a selected Messages row emits one traced reply-selection event', async () => {
    const { menu, sent } = rig();
    const info = vi.spyOn(console, 'info').mockImplementation(() => {});
    await menu.toggle();
    menu.handleList(clickRow0());
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(await menu.presentList(
      'MESSAGES', rows(3), latestRequestId(sent), true,
    )).toBe(true);
    menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);

    expect(menu.handleClick()).toBe(true);
    expect(sent[1]).toMatchObject({
      kind: 'input',
      event: {
        containerName: 'capdata',
        selectedIndex: 1,
        selectedName: 'MESSAGES',
      },
    });
    const traceId = (sent[1]?.event as { traceId?: string }).traceId;
    expect((sent[1]?.event as { requestId?: string }).requestId).toBe(latestRequestId(sent));
    expect(traceId).toMatch(/^hud-/);
    expect(info).toHaveBeenCalledWith(
      `[joat trace] trace=${traceId} stage=message-selected index=1`,
    );
    info.mockRestore();
  });

  it('routes non-Messages data clicks as capability detail selections', async () => {
    const { menu, sent } = rig();
    await menu.toggle();
    menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    menu.handleClick(); // Planning capability
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(await menu.presentList(
      'PLANNING', rows(2), latestRequestId(sent), true,
    )).toBe(true);

    expect(menu.handleClick()).toBe(true);
    expect(sent[1]).toMatchObject({
      kind: 'input',
      event: {
        containerName: 'capdata',
        selectedIndex: 0,
        selectedName: 'PLANNING',
      },
    });
  });

  it('returns a Planning drill-down to the live planner instead of Home', async () => {
    const { menu, sent } = rig();
    await menu.toggle();
    menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    menu.handleClick();
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    await menu.presentList('PLANNING', rows(2), latestRequestId(sent), true);

    menu.handleClick();
    expect(sent[1]).toMatchObject({
      kind: 'input',
      event: { containerName: 'capdata', selectedName: 'PLANNING', selectedIndex: 0 },
    });

    await menu.toggle();
    await vi.waitFor(() => expect(sent).toHaveLength(3));
    expect(sent[2]).toMatchObject({
      kind: 'input',
      event: { containerName: 'capmenu', selectedName: 'Planning', selectedIndex: 1 },
    });
  });

  it('keeps setup and action lifecycle lists read-only', async () => {
    const { menu, sent } = rig();
    await menu.toggle();
    menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    menu.handleClick();
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(await menu.presentList(
      'PLANNING · PLAN · SETUP', rows(2), latestRequestId(sent), false,
    )).toBe(true);
    expect(menu.handleClick()).toBe(true);
    expect(sent).toHaveLength(1);

    await menu.toggle();
    expect(await menu.presentLifecycle('ACTION · VERIFYING', rows(1))).toBe(true);
    expect(menu.handleClick()).toBe(true);
    expect(sent).toHaveLength(1);
  });

  it.each([
    { domain: 'calendar', phase: 'verifying', title: 'Design review', secondary: 'Checking native read-back' },
    { domain: 'calendar', phase: 'saved', title: 'Design review', secondary: 'Calendar read-back matched' },
    { domain: 'calendar', phase: 'failed', title: 'Design review', secondary: 'No calendar item was changed.' },
    { domain: 'calendar', phase: 'check', title: 'Design review', secondary: 'It may have changed. Do not retry.' },
    { domain: 'calendar', phase: 'cancelled', title: 'Design review', secondary: 'No calendar item was changed.' },
    { domain: 'calendar', phase: 'not_saved', title: 'Design review', secondary: 'No calendar item was changed.' },
    { domain: 'reminders', phase: 'completed', title: 'Buy filters', secondary: 'Reminders read-back matched' },
    { domain: 'reminders', phase: 'due', title: 'Buy filters', secondary: 'This reminder is still incomplete.' },
  ] satisfies ReadonlyArray<{
    domain: ProductivityHudModel['domain'];
    phase: ProductivityHudModel['phase'];
    title: string;
    secondary: string;
  }>)('returns $domain $phase lifecycle state to unified Planning', async (outcome) => {
    const { menu, lists, sent, operations } = rig();
    expect(await menu.presentProductivity({
      domain: outcome.domain,
      phase: outcome.phase,
      title: outcome.title,
      primary: 'Status',
      secondary: outcome.secondary,
      hint: 'left x2 Planning',
      planningAnchorUnixMs: 1_788_274_200_000,
    })).toBe(true);
    expect(operations).toEqual(['productivity']);
    expect(menu.handleClick()).toBe(true); // lifecycle pages never write

    await menu.toggle();
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(lists[0]).toEqual(['Loading recent planning…']);
    expect(sent[0]).toMatchObject({
      kind: 'input',
      event: {
        containerName: 'capmenu', selectedIndex: 1, selectedName: 'Planning',
        planningAnchorUnixMs: 1_788_274_200_000,
      },
    });
  });

  it('falls back to generic Planning when a stale outcome has an invalid anchor', async () => {
    const { menu, sent } = rig();
    await menu.presentProductivity({
      domain: 'reminders',
      phase: 'due',
      title: 'Buy filters',
      primary: 'Due now',
      secondary: 'This reminder is still incomplete.',
      hint: 'left x2 Planning',
      planningAnchorUnixMs: Number.NaN,
    });

    await menu.toggle();
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    const event = sent[0]?.event as Record<string, unknown>;
    expect(event).toMatchObject({
      containerName: 'capmenu', selectedName: 'Planning',
    });
    expect(event).not.toHaveProperty('planningAnchorUnixMs');
  });

  it('the armed reply instructions cannot be reinterpreted as message rows', async () => {
    const { menu, sent } = rig();
    await menu.toggle();
    menu.handleList(clickRow0());
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    const requestId = latestRequestId(sent);
    expect(await menu.presentList('MESSAGES', rows(3), requestId, true)).toBe(true);
    menu.handleClick();
    expect(sent).toHaveLength(2);

    expect(await menu.presentList(
      'MESSAGES · REPLY', rows(2), requestId, false,
    )).toBe(true);
    expect(menu.handleClick()).toBe(true);
    expect(sent).toHaveLength(2); // no second/stale conversation selection
  });

  it('double-tap is BACK from data — buffered home restores without an obsolete repaint', async () => {
    const { menu, sent, defaultsCount } = rig();
    await menu.toggle();
    menu.handleList(clickRow0());
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    await menu.toggle();          // back
    expect(menu.isOpen()).toBe(false);
    expect(defaultsCount()).toBe(1);
    expect(sent).toHaveLength(1); // only the capability selection request
  });

  it('a live Brainstorm atomically releases its instruction page and menu input', async () => {
    const { menu, sent, defaultsCount } = rig();
    await menu.toggle();
    for (let index = 0; index < 3; index += 1) {
      menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    }
    expect(menu.handleClick()).toBe(true); // scrollable first-level Brainstorm
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(await menu.presentList(
      'BRAINSTORM',
      ['Say: “JOAT, brainstorm <question>”'],
      latestRequestId(sent),
      false,
    )).toBe(true);

    await menu.beginLiveSession('brainstorm');

    expect(menu.isOpen()).toBe(false);
    expect(defaultsCount()).toBe(1);
    expect(menu.handleClick()).toBe(false);
  });

  it('rejects a delayed capability response after the wearer returns home', async () => {
    const { menu, sent } = rig();
    await menu.toggle();
    menu.handleList(clickRow0());
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    const requestId = latestRequestId(sent);
    expect(await menu.presentList('MESSAGES', rows(1), requestId, true)).toBe(true);
    await menu.toggle();
    expect(await menu.presentList('MESSAGES', rows(1), requestId, true)).toBe(false);
  });

  it('accepts only the newest capability response, including its setup suffix', async () => {
    const { menu, sent } = rig();
    await menu.toggle();
    menu.handleList(clickRow0());
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    const oldRequest = latestRequestId(sent);
    await menu.toggle();
    await menu.toggle();
    menu.handleList(click(1));
    await vi.waitFor(() => expect(sent).toHaveLength(2));
    const newRequest = latestRequestId(sent);

    expect(newRequest).not.toBe(oldRequest);
    expect(await menu.presentList('MESSAGES', rows(1), oldRequest, true)).toBe(false);
    expect(await menu.presentList('PLANNING', rows(1), oldRequest, true)).toBe(false);
    expect(await menu.presentList('PLANNING', rows(1), newRequest, true)).toBe(true);
    expect(await menu.presentList(
      'PLANNING · PLAN · SETUP', rows(1), newRequest, false,
    )).toBe(true);
  });

  it('does not expose settings controls when their focus page fails to paint', async () => {
    const { menu, sent, defaultsCount } = rig(true, false);

    await menu.openSettings();

    expect(menu.isOpen()).toBe(false);
    expect(menu.handleList(clickRow0())).toBe(false);
    expect(sent).toEqual([]);
    expect(defaultsCount()).toBe(1);
  });

  it('closes instead of entering an invisible scrolled capability after a paint failure', async () => {
    const { menu, defaultsCount } = rig(true, false);
    await menu.toggle();
    for (let index = 0; index < 4; index += 1) {
      menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    }

    menu.handleClick();
    await vi.waitFor(() => expect(menu.isOpen()).toBe(false));

    expect(defaultsCount()).toBe(1);
    expect(menu.handleClick()).toBe(false);
  });

  it('does not request Mac data when the loading page fails to paint', async () => {
    const { menu, sent, defaultsCount } = rig(true, false);
    await menu.toggle();

    menu.handleList(clickRow0());
    await vi.waitFor(() => expect(menu.isOpen()).toBe(false));

    expect(sent).toEqual([]);
    expect(defaultsCount()).toBe(1);
  });

  it('does not activate response rows until their complete frame paints', async () => {
    let finishResponsePaint: ((shown: boolean) => void) | undefined;
    const responsePaint = new Promise<boolean>((resolve) => {
      finishResponsePaint = resolve;
    });
    const { menu, sent, lists, defaultsCount } = rig(true, [true, responsePaint]);
    await menu.toggle();
    menu.handleList(clickRow0());
    await vi.waitFor(() => expect(sent).toHaveLength(1));

    const presenting = menu.presentList(
      'MESSAGES', rows(3), latestRequestId(sent), true,
    );
    await vi.waitFor(() => expect(lists).toHaveLength(2));

    expect(menu.handleClick()).toBe(true);
    expect(sent).toHaveLength(1); // no capdata selection while pixels are pending

    finishResponsePaint?.(false);
    expect(await presenting).toBe(true);
    expect(menu.isOpen()).toBe(false);
    expect(menu.handleClick()).toBe(false);
    expect(defaultsCount()).toBe(1);
  });

  it('Settings lives one level down and still applies a policy', async () => {
    const { menu, lists, listOptions, sent } = rig();
    await menu.openSettings(); // contextual-menu item
    expect(lists[0]!.some((r) => r.includes('Resume mic'))).toBe(true);
    expect(listOptions[0]).toEqual({
      title: 'LISTENING RESUME',
      hint: 'tap apply · left x2 overview',
    });
    menu.handleList(clickRow0()); // pick 'instantly' (row 0, zero-dropped)
    expect(sent.some((m) => m.kind === 'policy' && (m as { resume?: string }).resume === 'auto')).toBe(true);
    expect(menu.isOpen()).toBe(false); // applied and closed home
  });

  it('double-tap from the capability list is back, not deeper', async () => {
    const { menu, defaultsCount } = rig();
    await menu.toggle();
    await menu.toggle();
    expect(menu.isOpen()).toBe(false);
    expect(defaultsCount()).toBe(1);
  });
});
