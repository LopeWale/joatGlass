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
  productivityResult: PaintResult | PaintResult[] = true,
  productivityVisible = true,
) {
  const sidebars: string[][] = [];
  const lists: string[][] = [];
  const listOptions: Array<{ title?: string; hint?: string; selectable?: boolean } | undefined> = [];
  const statuses: string[] = [];
  const operations: string[] = [];
  const productivities: ProductivityHudModel[] = [];
  const deferredProductivityVisibleCallbacks: Array<(() => void) | undefined> = [];
  const selections: number[] = [];
  const moves: number[] = [];
  const sent: Array<Record<string, unknown>> = [];
  let defaults = 0;
  let sidebarCall = 0;
  let listCall = 0;
  let productivityCall = 0;
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
      items: string[], options?: { title?: string; hint?: string; selectable?: boolean },
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
    showProductivity: vi.fn(async (
      productivity: ProductivityHudModel,
      _status?: string,
      onDeferredProductivityVisible?: () => void,
    ) => {
      operations.push('productivity');
      productivities.push(productivity);
      deferredProductivityVisibleCallbacks.push(onDeferredProductivityVisible);
      const result = Array.isArray(productivityResult)
        ? (productivityResult[productivityCall] ?? true)
        : productivityResult;
      productivityCall += 1;
      return result;
    }),
    isProductivityVisible: vi.fn(() => productivityVisible),
    showDefault: vi.fn(async () => { defaults += 1; return true; }),
  } as unknown as HudPresenter;
  const link = { send: (m: Record<string, unknown>) => sent.push(m) } as unknown as MacLink;
  const menu = new CapabilityMenu(hud, new Settings(), link);
  return {
    menu, sidebars, lists, listOptions, operations, productivities,
    deferredProductivityVisibleCallbacks, selections, moves, statuses, sent,
    defaultsCount: () => defaults,
  };
}

const click = (index: number) =>
  ({ eventType: OsEventTypeList.CLICK_EVENT, currentSelectItemIndex: index }) as never;
const clickRow0 = () => ({ eventType: undefined, currentSelectItemIndex: undefined }) as never;
const scroll = (eventType: number, index?: number) =>
  ({ eventType, currentSelectItemIndex: index }) as never;

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
      'Settings',
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
    expect(listOptions[0]).toEqual({ title: 'MESSAGES', hint: 'left x2: back' });
    expect(operations).toEqual(['sidebar', 'list']);
    expect(sent[0]).toMatchObject({
      kind: 'input',
      event: { containerName: 'capmenu', eventType: 0, selectedIndex: 0, selectedName: 'Messages' },
    });
    expect(latestRequestId(sent)).toMatch(/^hud-request-/);
    // The loading data view owns firmware list events, but no row can activate
    // until a matching Mac response explicitly marks the rows selectable.
    expect(menu.handleList(click(1))).toBe(true);
    expect(sent).toHaveLength(1);
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

  it('uses the mirrored visual sidebar row when a host reports a stale native list index', async () => {
    const { menu, lists, listOptions } = rig();
    await menu.toggle();
    menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT); // Planning

    // Some firmware emits a list click from the invisible event-capture
    // layer. Its native list index is still zero even though JOAT visibly
    // moved the custom sidebar highlight to Planning.
    expect(menu.handleList(clickRow0())).toBe(true);
    await vi.waitFor(() => expect(lists).toHaveLength(1));
    expect(listOptions[0]?.title).toBe('PLANNING');
  });

  it('replaces a missing Planning response with retry guidance, then accepts its late matching response', async () => {
    vi.useFakeTimers();
    try {
      const { menu, lists, listOptions, sent } = rig();
      await menu.toggle();
      menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
      menu.handleClick();
      await vi.advanceTimersByTimeAsync(0);
      const requestId = latestRequestId(sent);
      expect(requestId).toMatch(/^hud-request-/);

      await vi.advanceTimersByTimeAsync(12_000);
      expect(lists[1]).toEqual([
        'No response from the Mac yet.',
        'Return to Overview, then open Planning to retry.',
      ]);
      expect(listOptions[1]).toMatchObject({
        title: 'PLANNING · RETRY', hint: 'left x2: back',
      });

      expect(await menu.presentList(
        'PLANNING · PLAN · AUGUST 2026', rows(2), requestId, true,
      )).toBe(true);
      expect(lists[2]).toEqual(rows(2));
    } finally {
      vi.useRealTimers();
    }
  });

  it('replaces a missing Messages response with a working tap-to-retry action', async () => {
    vi.useFakeTimers();
    try {
      const { menu, lists, listOptions, sent } = rig();
      await menu.toggle();
      menu.handleList(clickRow0());
      await vi.advanceTimersByTimeAsync(0);
      const firstRequestId = latestRequestId(sent);

      await vi.advanceTimersByTimeAsync(12_000);
      expect(lists[1]).toEqual(['Mac did not answer. Tap to retry Messages.']);
      expect(listOptions[1]).toMatchObject({
        title: 'MESSAGES · RETRY', hint: 'tap: retry · left x2: back', selectable: true,
      });

      expect(menu.handleClick()).toBe(true);
      await vi.advanceTimersByTimeAsync(0);
      expect(sent).toHaveLength(2);
      expect(sent[1]).toMatchObject({
        kind: 'input',
        event: { containerName: 'capmenu', selectedIndex: 0, selectedName: 'Messages' },
      });
      expect(latestRequestId(sent)).not.toBe(firstRequestId);
    } finally {
      vi.useRealTimers();
    }
  });

  it('turns Messages access guidance into a real retry action after Mac recovery', async () => {
    const { menu, sent, listOptions, moves } = rig();
    await menu.toggle();
    menu.handleList(clickRow0());
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    const firstRequestId = latestRequestId(sent);

    expect(await menu.presentList(
      'MESSAGES · ACCESS',
      [
        'Messages access is blocked on this Mac.',
        'Allow Full Disk Access for JOAT, then tap retry.',
      ],
      firstRequestId,
      false,
      'left x2: back',
    )).toBe(true);
    expect(listOptions[listOptions.length - 1]).toMatchObject({
      selectable: true, hint: 'tap: retry · left x2: back',
    });
    expect(menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT)).toBe(true);
    expect(moves).toEqual([]);

    expect(menu.handleClick()).toBe(true);
    await vi.waitFor(() => expect(sent).toHaveLength(2));
    expect(sent[1]).toMatchObject({
      kind: 'input',
      event: { containerName: 'capmenu', selectedName: 'Messages', selectedIndex: 0 },
    });
    expect(latestRequestId(sent)).not.toBe(firstRequestId);
  });

  it.each([
    ['conversation', async (menu: CapabilityMenu, requestId: string) => {
      expect(menu.handleList(click(1))).toBe(true);
      return requestId;
    }],
    ['full message', async (menu: CapabilityMenu, requestId: string) => {
      expect(menu.handleList(click(1))).toBe(true);
      expect(await menu.presentList(
        'MESSAGES · CONVERSATION',
        ['Latest messages', 'Dele · Opening words', 'Say reply'],
        requestId,
        true,
        'tap read · say reply · left x2: back',
      )).toBe(true);
      expect(menu.handleList(click(1))).toBe(true);
      return requestId;
    }],
  ])('turns a dropped Messages %s selection response into a tap-to-retry frame', async (
    _kind,
    select,
  ) => {
    vi.useFakeTimers();
    try {
      const { menu, lists, listOptions, sent } = rig();
      await menu.toggle();
      menu.handleList(clickRow0());
      await vi.advanceTimersByTimeAsync(0);
      const firstRequestId = latestRequestId(sent);
      expect(await menu.presentList('MESSAGES', rows(2), firstRequestId, true)).toBe(true);

      await select(menu, firstRequestId);
      expect(sent[sent.length - 1]).toMatchObject({
        kind: 'input',
        event: { requestId: firstRequestId },
      });

      await vi.advanceTimersByTimeAsync(12_000);
      expect(lists[lists.length - 1]).toEqual(['Mac did not answer. Tap to retry Messages.']);
      expect(listOptions[listOptions.length - 1]).toMatchObject({
        title: 'MESSAGES · RETRY',
        hint: 'tap: retry · left x2: back',
        selectable: true,
      });

      expect(menu.handleClick()).toBe(true);
      await vi.advanceTimersByTimeAsync(0);
      expect(sent[sent.length - 1]).toMatchObject({
        kind: 'input',
        event: { containerName: 'capmenu', selectedName: 'Messages' },
      });
      expect(latestRequestId(sent)).not.toBe(firstRequestId);
    } finally {
      vi.useRealTimers();
    }
  });

  it('does not discard a matching Planning response while the retry frame is still painting', async () => {
    vi.useFakeTimers();
    try {
      let finishRetryPaint: ((shown: boolean) => void) | undefined;
      const retryPaint = new Promise<boolean>((resolve) => { finishRetryPaint = resolve; });
      const { menu, sent } = rig(true, [true, retryPaint, true]);
      await menu.toggle();
      menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
      menu.handleClick();
      await vi.advanceTimersByTimeAsync(0);
      const requestId = latestRequestId(sent);

      await vi.advanceTimersByTimeAsync(12_000);
      const response = menu.presentList(
        'PLANNING · PLAN · AUGUST 2026', rows(2), requestId, true,
      );
      finishRetryPaint?.(true);

      expect(await response).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it('cancels the retry timer once the current Planning frame arrives', async () => {
    vi.useFakeTimers();
    try {
      const { menu, lists, sent } = rig();
      await menu.toggle();
      menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
      menu.handleClick();
      await vi.advanceTimersByTimeAsync(0);
      expect(await menu.presentList(
        'PLANNING · PLAN · AUGUST 2026', rows(2), latestRequestId(sent), true,
      )).toBe(true);

      await vi.advanceTimersByTimeAsync(12_000);
      expect(lists).toEqual([
        ['Loading recent planning…'],
        rows(2),
      ]);
    } finally {
      vi.useRealTimers();
    }
  });

  it('replaces a missing response with retry guidance for every capability, not just Planning and Messages', async () => {
    vi.useFakeTimers();
    try {
      const { menu, lists, listOptions } = rig();
      await menu.toggle();
      for (let index = 0; index < 5; index += 1) {
        menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT); // QuickNotes
      }
      menu.handleClick();
      await vi.advanceTimersByTimeAsync(0);
      expect(listOptions[0]?.title).toBe('QUICKNOTES');

      await vi.advanceTimersByTimeAsync(12_000);
      // The guidance names the row exactly as the sidebar prints it —
      // 'QuickNotes', not a case-folded 'Quicknotes'.
      expect(lists[1]).toEqual([
        'No response from the Mac yet.',
        'Return to Overview, then open QuickNotes to retry.',
      ]);
      expect(listOptions[1]).toMatchObject({
        title: 'QUICKNOTES · RETRY', hint: 'left x2: back',
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it('recovers a dropped Planning drill-down tap instead of leaving the month tap-dead', async () => {
    vi.useFakeTimers();
    try {
      const { menu, lists, listOptions, sent } = rig();
      await menu.toggle();
      menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT); // Planning
      menu.handleClick();
      await vi.advanceTimersByTimeAsync(0);
      const requestId = latestRequestId(sent);
      expect(await menu.presentList(
        'PLANNING · PLAN · AUGUST 2026', rows(3), requestId, true,
      )).toBe(true);

      // Week-row tap: the selection is sent, then the Mac response is lost.
      expect(menu.handleList(click(1))).toBe(true);
      expect(sent[sent.length - 1]).toMatchObject({
        kind: 'input',
        event: { containerName: 'capdata', selectedName: 'PLANNING', requestId },
      });
      await vi.advanceTimersByTimeAsync(12_000);
      expect(listOptions[listOptions.length - 1]).toMatchObject({
        title: 'PLANNING · RETRY', hint: 'left x2: back',
      });

      // A late matching week frame still lands, and — because the timeout
      // cleared the pending-selection latch — its rows accept a new tap.
      expect(await menu.presentList(
        'PLANNING · PLAN · AUG 3–9', rows(2), requestId, true,
      )).toBe(true);
      expect(lists[lists.length - 1]).toEqual(rows(2));
      const before = sent.length;
      expect(menu.handleList(click(0))).toBe(true);
      expect(sent.length).toBe(before + 1);
      expect(sent[sent.length - 1]).toMatchObject({
        kind: 'input',
        event: { containerName: 'capdata', selectedName: 'PLANNING' },
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it('releaseToLiveSession clears the menu and its retry timer without painting over the live surface', async () => {
    vi.useFakeTimers();
    try {
      const { menu, lists, sent, defaultsCount } = rig();
      await menu.toggle();
      menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT); // Planning
      menu.handleClick();
      await vi.advanceTimersByTimeAsync(0);
      expect(lists).toHaveLength(1);
      expect(menu.isOpen()).toBe(true);
      const paintedBefore = defaultsCount();

      menu.releaseToLiveSession();
      expect(menu.isOpen()).toBe(false);
      // No Overview or retry frame may race the live session's own paint.
      await vi.advanceTimersByTimeAsync(12_000);
      expect(lists).toHaveLength(1);
      expect(defaultsCount()).toBe(paintedBefore);
      // A stale response for the released request can no longer paint either.
      expect(await menu.presentList(
        'PLANNING · PLAN · AUGUST 2026', rows(2), latestRequestId(sent), true,
      )).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it('releaseToLiveSession away from Messages retires the Mac-side reply consent', async () => {
    const { menu, sent } = rig();
    await menu.toggle();
    menu.handleList(clickRow0());
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(await menu.presentList('MESSAGES', rows(2), latestRequestId(sent), true)).toBe(true);

    menu.releaseToLiveSession();
    expect(sent[sent.length - 1]).toMatchObject({
      kind: 'input',
      event: { containerName: 'capability-lifecycle', selectedName: 'messages-home' },
    });
  });

  it('keeps Navigation visible in the first-level wearable sidebar and opens its destination handoff', async () => {
    const { menu, sidebars, selections, lists, listOptions, sent } = rig();
    await menu.toggle();

    expect(sidebars[0]).toEqual([
      'Messages', 'Planning', 'Navigation', 'Brainstorm',
      'Notifications', 'QuickNotes', 'Agent Terminal', 'Activity',
      'Settings',
    ]);
    for (let index = 0; index < 2; index += 1) {
      menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    }
    expect(selections).toEqual([1, 2]);
    expect(menu.handleClick()).toBe(true);

    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(lists[0]).toEqual(['Loading recent navigation…']);
    expect(listOptions[0]).toEqual({ title: 'NAVIGATION', hint: 'left x2: back' });
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

  it('routes a physical listEvent selection through the owned Messages request', async () => {
    const { menu, sent } = rig();
    const info = vi.spyOn(console, 'info').mockImplementation(() => {});
    await menu.toggle();
    menu.handleList(clickRow0());
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    const requestId = latestRequestId(sent);
    expect(await menu.presentList('MESSAGES', rows(3), requestId, true)).toBe(true);

    expect(menu.handleList(click(1))).toBe(true);
    expect(sent[1]).toMatchObject({
      kind: 'input',
      event: {
        containerName: 'capdata',
        selectedIndex: 1,
        selectedName: 'MESSAGES',
        requestId,
      },
    });
    const traceId = (sent[1]?.event as { traceId?: string }).traceId;
    expect(traceId).toMatch(/^hud-/);
    expect(info).toHaveBeenCalledWith(
      `[joat trace] trace=${traceId} stage=message-selected index=1`,
    );
    info.mockRestore();
  });

  it('keeps a Messages picker selection when a native scroll reports its stale zero index', async () => {
    const { menu, selections, sent } = rig();
    await menu.toggle();
    menu.handleList(clickRow0());
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    const requestId = latestRequestId(sent);
    expect(await menu.presentList('MESSAGES', rows(3), requestId, true)).toBe(true);

    // The official G2 simulator emits the same list index while reporting a
    // downward scroll. The scroll direction remains an ordinary G2 control,
    // so preserve the locally mirrored highlight until its following click.
    expect(menu.handleList(scroll(OsEventTypeList.SCROLL_BOTTOM_EVENT, 0))).toBe(true);
    expect(menu.handleList(scroll(OsEventTypeList.SCROLL_BOTTOM_EVENT, 0))).toBe(true);
    expect(menu.handleList(click(0))).toBe(true);

    expect(selections).toEqual([1, 2]);
    expect(sent[1]).toMatchObject({
      kind: 'input',
      event: {
        containerName: 'capdata',
        selectedIndex: 2,
        selectedName: 'MESSAGES',
        requestId,
      },
    });
  });

  it('suppresses repeated physical message selections until the Mac replaces the frame', async () => {
    const { menu, sent } = rig();
    const info = vi.spyOn(console, 'info').mockImplementation(() => {});
    await menu.toggle();
    menu.handleList(clickRow0());
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    const requestId = latestRequestId(sent);
    expect(await menu.presentList('MESSAGES', rows(3), requestId, true)).toBe(true);

    expect(menu.handleList(click(1))).toBe(true);
    expect(menu.handleList(click(1))).toBe(true);
    expect(sent).toHaveLength(2);

    expect(await menu.presentList(
      'MESSAGES · REPLY', ['Ada · latest', 'Say “JOAT, reply saying …”'], requestId, false,
    )).toBe(true);
    expect(sent).toHaveLength(2);
    info.mockRestore();
  });

  it('routes a selected transcript line to the Mac-owned full-message reader', async () => {
    const { menu, sent } = rig();
    const info = vi.spyOn(console, 'info').mockImplementation(() => {});
    await menu.toggle();
    menu.handleList(clickRow0());
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    const requestId = latestRequestId(sent);
    expect(await menu.presentList('MESSAGES', rows(2), requestId, true)).toBe(true);
    expect(menu.handleList(click(1))).toBe(true);
    expect(await menu.presentList(
      'MESSAGES · CONVERSATION',
      ['Latest messages', 'Dele · Opening words', 'You · Reply', 'Say reply'],
      requestId,
      true,
      'say reply · left x2: back',
    )).toBe(true);

    expect(menu.handleList(click(1))).toBe(true);
    expect(menu.handleList(click(1))).toBe(true);
    expect(sent).toHaveLength(3);
    expect(sent[2]).toMatchObject({
      kind: 'input',
      event: {
        containerName: 'capmessage',
        selectedName: 'MESSAGES',
        selectedIndex: 1,
        requestId,
      },
    });
    const traceId = (sent[2]?.event as { traceId?: string }).traceId;
    expect(traceId).toMatch(/^hud-/);
    expect(info).toHaveBeenCalledWith(
      `[joat trace] trace=${traceId} stage=message-read-requested index=1`,
    );

    expect(await menu.presentList(
      'MESSAGES · MESSAGE',
      ['From Dele · 1/2', 'Opening words'],
      requestId,
      false,
      'down more · say reply · left x2: back',
    )).toBe(true);
    expect(menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT)).toBe(true);
    expect(sent).toHaveLength(4);
    expect(sent[3]).toMatchObject({
      kind: 'input',
      event: {
        containerName: 'cappage',
        selectedName: 'MESSAGES',
        eventType: OsEventTypeList.SCROLL_BOTTOM_EVENT,
        requestId,
      },
    });
    info.mockRestore();
  });

  it('returns from full text to the live conversation before clearing its reply target', async () => {
    const { menu, lists, sent } = rig();
    await menu.toggle();
    menu.handleList(clickRow0());
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    const requestId = latestRequestId(sent);
    const conversations = ['New message', 'Dele · Preview'];
    const transcript = ['Latest messages', 'Dele · Opening words', 'Say reply'];
    expect(await menu.presentList('MESSAGES', conversations, requestId, true)).toBe(true);
    menu.handleList(click(1));
    expect(await menu.presentList(
      'MESSAGES · CONVERSATION', transcript, requestId, true,
      'tap read · say reply · left x2: back',
    )).toBe(true);
    menu.handleList(click(1));
    expect(await menu.presentList(
      'MESSAGES · MESSAGE', ['From Dele · 1/1', 'Opening words'], requestId, false,
      'say reply · left x2: back',
    )).toBe(true);

    await menu.toggle();
    expect(lists[lists.length - 1]).toEqual(transcript);
    expect(sent[3]).toEqual({
      kind: 'input',
      event: { containerName: 'capability-lifecycle', selectedName: 'messages-conversation' },
    });

    await menu.toggle();
    expect(lists[lists.length - 1]).toEqual(conversations);
    expect(sent[4]).toEqual({
      kind: 'input',
      event: { containerName: 'capability-lifecycle', selectedName: 'messages-list' },
    });
  });

  it('requests older and newer conversation pages only at scroll boundaries', async () => {
    const { menu, sent } = rig();
    await menu.toggle();
    menu.handleList(clickRow0());
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    const requestId = latestRequestId(sent);
    expect(await menu.presentList('MESSAGES', rows(2), requestId, true)).toBe(true);
    menu.handleList(click(1));
    expect(await menu.presentList(
      'MESSAGES · CONVERSATION', rows(4), requestId, false,
      'down older · say reply · left x2: back',
    )).toBe(true);

    for (let index = 0; index < 3; index += 1) {
      expect(menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT)).toBe(true);
    }
    expect(sent).toHaveLength(2);
    expect(menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT)).toBe(true);
    expect(menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT)).toBe(true);
    expect(sent).toHaveLength(3);
    expect(sent[2]).toMatchObject({
      kind: 'input',
      event: {
        containerName: 'cappage',
        selectedName: 'MESSAGES',
        eventType: OsEventTypeList.SCROLL_BOTTOM_EVENT,
        requestId,
      },
    });

    expect(await menu.presentList(
      'MESSAGES · CONVERSATION', rows(3), requestId, false,
      'up newer · say reply · left x2: back',
    )).toBe(true);
    expect(menu.handleScroll(OsEventTypeList.SCROLL_TOP_EVENT)).toBe(true);
    expect(sent).toHaveLength(4);
    expect(sent[3]).toMatchObject({
      kind: 'input',
      event: {
        containerName: 'cappage',
        selectedName: 'MESSAGES',
        eventType: OsEventTypeList.SCROLL_TOP_EVENT,
        requestId,
      },
    });
  });

  it('forwards native list scroll boundary events for a paged conversation', async () => {
    const { menu, sent } = rig();
    await menu.toggle();
    menu.handleList(clickRow0());
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    const requestId = latestRequestId(sent);
    expect(await menu.presentList('MESSAGES', rows(2), requestId, true)).toBe(true);
    menu.handleList(click(1));
    expect(await menu.presentList(
      'MESSAGES · CONVERSATION', rows(4), requestId, false,
      'down older · say reply · left x2: back',
    )).toBe(true);

    expect(menu.handleList(scroll(OsEventTypeList.SCROLL_BOTTOM_EVENT, 1))).toBe(true);
    expect(menu.handleList(scroll(OsEventTypeList.SCROLL_BOTTOM_EVENT, 2))).toBe(true);
    expect(menu.handleList(scroll(OsEventTypeList.SCROLL_BOTTOM_EVENT, 3))).toBe(true);
    expect(sent).toHaveLength(2);
    expect(menu.handleList(scroll(OsEventTypeList.SCROLL_BOTTOM_EVENT, 3))).toBe(true);
    expect(sent).toHaveLength(3);
    expect(sent[2]).toMatchObject({
      kind: 'input',
      event: {
        containerName: 'cappage',
        eventType: OsEventTypeList.SCROLL_BOTTOM_EVENT,
        requestId,
      },
    });
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
    { domain: 'reminders', phase: 'verifying', title: 'Buy filters', secondary: 'Checking native read-back' },
    { domain: 'reminders', phase: 'saved', title: 'Buy filters', secondary: 'Native alarm set · JOAT monitoring armed.' },
    { domain: 'reminders', phase: 'monitoring', title: 'Buy filters', secondary: 'Native alarm is scheduled. JOAT will check at due time.' },
    { domain: 'reminders', phase: 'failed', title: 'Buy filters', secondary: 'No reminders item was changed.' },
    { domain: 'reminders', phase: 'check', title: 'Buy filters', secondary: 'It may have changed. Do not retry.' },
    { domain: 'reminders', phase: 'cancelled', title: 'Buy filters', secondary: 'No reminders item was changed.' },
    { domain: 'reminders', phase: 'not_saved', title: 'Buy filters', secondary: 'No reminders item was changed.' },
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

  it('does not report a failed or superseded due paint as visible', async () => {
    const due: ProductivityHudModel = {
      domain: 'reminders', phase: 'due', title: 'Private reminder',
      primary: 'Due now', secondary: 'Still incomplete', hint: 'left x2 Planning',
      reminderDueId: '67ec97c5-a0fd-48ec-89cb-1b756ddaf87a',
      reminderDueAttemptId: 'a76b12f1-45e4-41fd-aed8-086f6b6b1954',
      reminderDueGeneration: 7,
    };
    expect(await rig(true, true, false).menu.presentProductivity(due)).toBe(false);

    let resolveFirst: (value: boolean) => void = () => undefined;
    const firstPaint = new Promise<boolean>((resolve) => { resolveFirst = resolve; });
    const { menu } = rig(true, true, [firstPaint, true]);
    const stale = menu.presentProductivity(due);
    await Promise.resolve();
    expect(await menu.presentProductivity({ ...due, reminderDueAttemptId: '9b38eafb-6fda-4d4c-bb6e-80b9365854b5' })).toBe(true);
    resolveFirst(true);
    expect(await stale).toBe(false);
  });

  it('hands a deferred due acknowledgement to the presenter without sending it early', async () => {
    const { menu, deferredProductivityVisibleCallbacks } = rig(true, true, true, false);
    const due: ProductivityHudModel = {
      domain: 'reminders', phase: 'due', title: 'Private reminder',
      primary: 'Due now', secondary: 'Still incomplete', hint: 'left x2 Planning',
      reminderDueId: '67ec97c5-a0fd-48ec-89cb-1b756ddaf87a',
      reminderDueAttemptId: 'a76b12f1-45e4-41fd-aed8-086f6b6b1954',
      reminderDueGeneration: 7,
    };
    let acknowledged = 0;
    expect(await menu.presentProductivity(due, () => { acknowledged += 1; })).toBe(false);
    expect(acknowledged).toBe(0);
    deferredProductivityVisibleCallbacks[0]?.();
    expect(acknowledged).toBe(1);
  });

  it('starts a deferred terminal return only after the outcome is actually visible', async () => {
    vi.useFakeTimers();
    try {
      const { menu, deferredProductivityVisibleCallbacks, sent } = rig(
        true, true, true, false,
      );
      expect(await menu.presentProductivity({
        domain: 'calendar',
        phase: 'not_saved',
        title: 'Unsaved Calendar probe',
        primary: 'Not saved',
        secondary: 'No calendar item was changed.',
        hint: 'Back to Planning: left x2 · auto in 5s',
        planningAnchorUnixMs: 1_788_274_200_000,
      })).toBe(false);

      await vi.advanceTimersByTimeAsync(10_000);
      expect(sent).toEqual([]);

      deferredProductivityVisibleCallbacks[0]?.();
      await vi.advanceTimersByTimeAsync(4_999);
      expect(sent).toEqual([]);
      await vi.advanceTimersByTimeAsync(1);
      expect(sent).toHaveLength(1);
      expect(sent[0]).toMatchObject({
        kind: 'input',
        event: {
          containerName: 'capmenu',
          selectedName: 'Planning',
          planningAnchorUnixMs: 1_788_274_200_000,
        },
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    { domain: 'calendar', phase: 'saved' },
    { domain: 'calendar', phase: 'failed' },
    { domain: 'calendar', phase: 'check' },
    { domain: 'calendar', phase: 'cancelled' },
    { domain: 'calendar', phase: 'not_saved' },
    { domain: 'reminders', phase: 'saved' },
    { domain: 'reminders', phase: 'monitoring' },
    { domain: 'reminders', phase: 'completed' },
    { domain: 'reminders', phase: 'failed' },
    { domain: 'reminders', phase: 'check' },
    { domain: 'reminders', phase: 'cancelled' },
    { domain: 'reminders', phase: 'not_saved' },
  ] satisfies ReadonlyArray<{
    domain: ProductivityHudModel['domain'];
    phase: ProductivityHudModel['phase'];
  }>)('returns terminal $domain $phase to its anchored Planning day after a readable outcome', async (outcome) => {
    vi.useFakeTimers();
    try {
      const { menu, sent } = rig();
      const planningAnchorUnixMs = 1_788_274_200_000;
      await menu.presentProductivity({
        ...outcome,
        title: 'Native-backed item',
        primary: 'Outcome recorded',
        secondary: 'Read-back complete',
        hint: 'Planning in 5s · left x2 now',
        planningAnchorUnixMs,
      });

      await vi.advanceTimersByTimeAsync(4_999);
      expect(sent).toEqual([]);

      await vi.advanceTimersByTimeAsync(1);
      expect(sent).toHaveLength(1);
      expect(sent[0]).toMatchObject({
        kind: 'input',
        event: {
          containerName: 'capmenu',
          selectedIndex: 1,
          selectedName: 'Planning',
          planningAnchorUnixMs,
        },
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it('contextual Planning preserves the current safe-write day anchor', async () => {
    const { menu, lists, sent } = rig();
    expect(await menu.presentProductivity({
      domain: 'reminders',
      phase: 'saved',
      title: 'Prepare report',
      primary: 'Saved and verified',
      secondary: 'Native alarm set',
      hint: 'Back: left x2 to Planning',
      planningAnchorUnixMs: 1_788_274_200_000,
    })).toBe(true);

    await menu.openPlanning();

    expect(lists).toEqual([['Loading recent planning…']]);
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({
      kind: 'input',
      event: {
        containerName: 'capmenu',
        selectedIndex: 1,
        selectedName: 'Planning',
        planningAnchorUnixMs: 1_788_274_200_000,
      },
    });
  });

  it('keeps an independently verified reminder visible as monitoring before returning to Planning', async () => {
    vi.useFakeTimers();
    try {
      const { menu, productivities, sent } = rig();
      const planningAnchorUnixMs = 1_788_274_200_000;
      await menu.presentProductivity({
        domain: 'reminders',
        phase: 'saved',
        title: 'Buy filters',
        primary: 'Saved and verified',
        secondary: 'Reminders read-back matched',
        hint: 'Monitoring next · left x2 Planning',
        planningAnchorUnixMs,
        monitoringArmed: true,
      });

      await vi.advanceTimersByTimeAsync(4_999);
      expect(productivities).toHaveLength(1);
      expect(sent).toEqual([]);

      await vi.advanceTimersByTimeAsync(1);
      expect(productivities).toHaveLength(2);
      expect(productivities[1]).toMatchObject({
        domain: 'reminders',
        phase: 'monitoring',
        title: 'Buy filters',
        primary: 'Monitoring armed',
        secondary: 'Native alarm is scheduled. JOAT will check at due time.',
        hint: 'Planning in 5s · left x2 now',
        planningAnchorUnixMs,
      });
      expect(sent).toEqual([]);

      await vi.advanceTimersByTimeAsync(5_000);
      expect(sent).toHaveLength(1);
      expect(sent[0]).toMatchObject({
        kind: 'input',
        event: { containerName: 'capmenu', selectedName: 'Planning', planningAnchorUnixMs },
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it('cancels the local monitoring transition when the wearer returns to Planning early', async () => {
    vi.useFakeTimers();
    try {
      const { menu, productivities, sent } = rig();
      await menu.presentProductivity({
        domain: 'reminders',
        phase: 'saved',
        title: 'Buy filters',
        primary: 'Saved and verified',
        secondary: 'Native alarm set · JOAT monitoring armed.',
        hint: 'Monitoring next · left x2 Planning',
        planningAnchorUnixMs: 1_788_274_200_000,
        monitoringArmed: true,
      });

      await menu.toggle();
      expect(sent).toHaveLength(1);
      await vi.advanceTimersByTimeAsync(5_000);

      expect(productivities).toHaveLength(1);
      expect(sent).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it('does not abandon a newer due reminder when replacing a terminal outcome', async () => {
    vi.useFakeTimers();
    try {
      const { menu, sent } = rig();
      await menu.presentProductivity({
        domain: 'reminders',
        phase: 'saved',
        title: 'Buy filters',
        primary: 'Saved and verified',
        secondary: 'Native alarm set · JOAT monitoring armed.',
        hint: 'Planning in 5s · left x2 now',
        planningAnchorUnixMs: 1_788_274_200_000,
        monitoringArmed: true,
      });
      await menu.presentProductivity({
        domain: 'reminders',
        phase: 'due',
        title: 'Buy filters',
        primary: 'Due now',
        secondary: 'This reminder is still incomplete.',
        hint: 'left x2 Planning',
        planningAnchorUnixMs: 1_788_274_200_000,
      });

      await vi.advanceTimersByTimeAsync(5_000);
      expect(sent).toEqual([]);
    } finally {
      vi.useRealTimers();
    }
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

  it('walks Messages detail back to its cached conversation list, then sidebar, then Home', async () => {
    const { menu, sidebars, lists, sent, defaultsCount } = rig();
    await menu.toggle();
    menu.handleList(clickRow0());
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    const requestId = latestRequestId(sent);
    const conversationRows = ['New message · voice recipient + text', '[now] Adele · Good morning'];
    expect(await menu.presentList('MESSAGES', conversationRows, requestId, true)).toBe(true);
    menu.handleList(click(1));
    expect(sent).toHaveLength(2);
    expect(await menu.presentList(
      'MESSAGES · CONVERSATION', ['Adele · Good morning', 'Say reply saying …'], requestId, false,
    )).toBe(true);

    await menu.toggle();
    expect(lists[lists.length - 1]).toEqual(conversationRows);
    expect(defaultsCount()).toBe(0);
    expect(sent[2]).toEqual({
      kind: 'input',
      event: { containerName: 'capability-lifecycle', selectedName: 'messages-list' },
    });

    await menu.toggle();
    expect(sidebars).toHaveLength(2);
    expect(menu.isOpen()).toBe(true);
    expect(sent[3]).toEqual({
      kind: 'input',
      event: { containerName: 'capability-lifecycle', selectedName: 'messages-home' },
    });

    await menu.toggle();
    expect(menu.isOpen()).toBe(false);
    expect(defaultsCount()).toBe(1);
  });

  it('double-tap from the Messages root returns to the sidebar and clears Mac-private Messages state', async () => {
    const { menu, sent, defaultsCount } = rig();
    await menu.toggle();
    menu.handleList(clickRow0());
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    await menu.toggle();          // back
    expect(menu.isOpen()).toBe(true);
    expect(defaultsCount()).toBe(0);
    expect(sent).toHaveLength(2);
    expect(sent[1]).toEqual({
      kind: 'input',
      event: {
        containerName: 'capability-lifecycle',
        selectedName: 'messages-home',
      },
    });
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

  it('Settings renders a selectable G2 policy list and applies its scrolled cursor', async () => {
    const { menu, lists, listOptions, selections, sent } = rig();
    await menu.openSettings(); // contextual-menu item
    expect(lists[0]?.map((row) => row.replace('*', ' '))).toEqual([
      '  Resume mic: instantly',
      '  Resume mic: Mac decides',
      '  Resume mic: ask me first',
      'Repeat last reply',
      'Reconnect JOAT',
    ]);
    expect(listOptions[0]).toEqual({
      title: 'LISTENING RESUME',
      hint: 'tap: apply · left x2: back',
      selectable: true,
    });
    expect(menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT)).toBe(true);
    expect(selections).toEqual([1]);
    expect(menu.handleClick()).toBe(true); // pick the visibly highlighted Mac policy
    expect(sent.some((m) => m.kind === 'policy' && (m as { resume?: string }).resume === 'mac')).toBe(true);
    expect(menu.isOpen()).toBe(false); // applied and closed home
  });

  it('adopts a Mac voice generation so speech paints without a menu tap', async () => {
    const { menu, lists, listOptions } = rig();
    // Menu fully closed — the Mac answers "JOAT, show my messages".
    const painted = await menu.presentList(
      'MESSAGES', ['[2m] \u25cf Adele \u00b7 Send the mockups?'], 'voice-abc123', true, 'tap: select',
    );
    expect(painted).toBe(true);
    expect(lists[lists.length - 1]).toEqual(['[2m] \u25cf Adele \u00b7 Send the mockups?']);
    expect(listOptions[listOptions.length - 1]?.title).toBe('MESSAGES');
    // The adopted generation answers follow-up pages too (same voice id).
    const detail = await menu.presentList(
      'MESSAGES \u00b7 CONVERSATION', ['Latest messages \u2014 down for older', 'Adele \u00b7 hi'],
      'voice-abc123', true,
    );
    expect(detail).toBe(true);
  });

  it('still drops an unsolicited list without the voice prefix', async () => {
    const { menu, lists } = rig();
    const painted = await menu.presentList('MESSAGES', ['row'], 'hud-request-999', true);
    expect(painted).toBe(false);
    expect(lists).toHaveLength(0);
  });

  it('double-tap from the capability list is back, not deeper', async () => {
    const { menu, defaultsCount } = rig();
    await menu.toggle();
    await menu.toggle();
    expect(menu.isOpen()).toBe(false);
    expect(defaultsCount()).toBe(1);
  });
});

describe('Planning split dashboard', () => {
  function dashboardRig() {
    const panes: Array<{ dateIndex: number; focused: boolean; itemIndex: number }> = [];
    const sent: Array<Record<string, unknown>> = [];
    const hud = {
      showSidebar: vi.fn(async () => true),
      showList: vi.fn(async () => true),
      setStatus: vi.fn(async () => true),
      setSelection: vi.fn(async () => true),
      moveSelection: vi.fn(async () => true),
      updatePlanningPane: vi.fn(async (pane: { dateIndex: number; focused: boolean; itemIndex: number }) => {
        panes.push({ ...pane });
        return true;
      }),
      showProductivity: vi.fn(async () => true),
      isProductivityVisible: vi.fn(() => true),
      showDefault: vi.fn(async () => true),
    } as unknown as HudPresenter;
    const link = { send: (m: Record<string, unknown>) => sent.push(m) } as unknown as MacLink;
    const menu = new CapabilityMenu(hud, new Settings(), link);
    return { menu, panes, sent };
  }

  function monthGrid() {
    // Sep 2026 grid: Monday Aug 31 leads, 42 cells. Sep 1 (index 1) carries
    // two entries; every other day is free.
    return Array.from({ length: 42 }, (_, index) => {
      const day = new Date(Date.UTC(2026, 7, 31 + index));
      const iso = day.toISOString().slice(0, 10);
      const busy = index === 1;
      return {
        day: iso,
        eventCount: busy ? 1 : 0,
        reminderCount: busy ? 1 : 0,
        inMonth: day.getUTCMonth() === 8,
        ...(busy ? {
          entries: [
            { time: '09:30', kind: 'event' as const, title: 'Design review' },
            { time: '18:00', kind: 'reminder' as const, title: 'Buy filters' },
          ],
        } : {}),
      };
    });
  }

  async function openDashboard() {
    const rigParts = dashboardRig();
    const { menu, sent } = rigParts;
    await menu.toggle();
    menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT); // Planning
    menu.handleClick();
    await vi.waitFor(() => expect(sent.length).toBeGreaterThan(0));
    const requestId = latestRequestId(sent);
    expect(await menu.presentList(
      'PLANNING · PLAN · SEPTEMBER 2026', rows(11), requestId, true,
      undefined, monthGrid() as never,
    )).toBe(true);
    return { ...rigParts, requestId };
  }

  it('scroll hovers dates, tap hands focus to the pane, tap opens the entry', async () => {
    const { menu, panes, sent, requestId } = await openDashboard();

    // Hover moves locally — no Mac traffic, one repaint per move.
    const before = sent.length;
    menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    expect(sent.length).toBe(before);
    expect(panes[panes.length - 1]).toMatchObject({ focused: false });

    // Return to Sep 1 (hover starts on the first in-month day, index 1,
    // when today is outside this month; the scroll above moved to 2).
    menu.handleScroll(OsEventTypeList.SCROLL_TOP_EVENT);
    expect(panes[panes.length - 1]).toMatchObject({ dateIndex: 1, focused: false });

    // First tap: the pane takes input. Second tap: the entry opens via the
    // date+position wire, no identifiers.
    menu.handleClick();
    expect(panes[panes.length - 1]).toMatchObject({ focused: true, itemIndex: 0 });
    menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    expect(panes[panes.length - 1]).toMatchObject({ focused: true, itemIndex: 1 });
    menu.handleClick();
    const grid = sent[sent.length - 1] as { event: Record<string, unknown> };
    expect(grid.event.containerName).toBe('capgrid');
    expect(grid.event.selectedIndex).toBe(1);
    expect(grid.event.requestId).toBe(requestId);
    expect(typeof grid.event.planningAnchorUnixMs).toBe('number');
  });

  it('Back walks pane to grid before leaving the dashboard', async () => {
    const { menu, panes } = await openDashboard();
    menu.handleClick(); // pane focus
    expect(panes[panes.length - 1]).toMatchObject({ focused: true });
    await menu.toggle(); // Back: pane -> grid, still on the dashboard
    expect(panes[panes.length - 1]).toMatchObject({ focused: false });
    expect(menu.isOpen()).toBe(true);
  });

  it('scrolling past the month edge asks the Mac for the neighbour month', async () => {
    const { menu, sent } = await openDashboard();
    // Walk hover to date 0, then one more up crosses the edge.
    for (let i = 0; i < 42; i += 1) menu.handleScroll(OsEventTypeList.SCROLL_TOP_EVENT);
    const monthFlip = sent[sent.length - 1] as { event: Record<string, unknown> };
    expect(monthFlip.event.containerName).toBe('capdata');
    expect(monthFlip.event.selectedIndex).toBe(6); // the month list's ‹ previous row
  });

  it('a tap on a free day does not steal focus into an empty pane', async () => {
    const { menu, panes } = await openDashboard();
    menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT); // a free day
    const before = panes.length;
    menu.handleClick();
    // No pane repaint claiming focus happened.
    expect(panes.slice(before).every((p) => !p.focused)).toBe(true);
  });
});
