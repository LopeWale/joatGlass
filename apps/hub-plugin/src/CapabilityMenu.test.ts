import {
  afterEach, beforeEach, describe, expect, it, vi,
} from 'vitest';
import { OsEventTypeList, type RebuildPageContainer } from '@evenrealities/even_hub_sdk';
import { CapabilityMenu, isActionLifecycleTitle } from './CapabilityMenu';
import { BrainstormController } from './BrainstormController';
import { Settings } from './Settings';
import { HudPresenter } from './HudPresenter';
import { HudPageBuilder } from './HudPageBuilder';
import type { BridgeClient } from './BridgeClient';
import type { HudVisualFrame } from './HudVisualSurface';
import { selectedMessagesConversationRow } from './HudCanvasRenderer';
import type { MacLink } from './MacLink';
import { CAPABILITIES, type ProductivityHudModel } from './types';

/**
 * The wearer's report from hardware: double-tap opened "a menu modal, but
 * not the one we designed", and there was no back gesture. These tests pin
 * the designed control-center: capability rows first, settings one level
 * down, and double-tap ALWAYS means back once anything is open.
 */

type PaintResult = boolean | Promise<boolean>;

function rig(
  listResult: PaintResult | PaintResult[] = true,
  productivityResult: PaintResult | PaintResult[] = true,
  productivityVisible = true,
  selectionResult: PaintResult = true,
) {
  const lists: string[][] = [];
  const listOptions: Array<{ title?: string; hint?: string; selectable?: boolean } | undefined> = [];
  const statuses: string[] = [];
  const operations: string[] = [];
  const productivities: ProductivityHudModel[] = [];
  const deferredProductivityVisibleCallbacks: Array<(() => void) | undefined> = [];
  const selections: number[] = [];
  const moves: number[] = [];
  const sent: Array<Record<string, unknown>> = [];
  const surfaceEvents: Array<Record<string, unknown>> = [];
  let defaults = 0;
  let listCall = 0;
  let visibleSelection = 0;
  let productivityCall = 0;
  // The list page on the glass, as HudPresenter tracks its surface: a
  // firmware list or text page owns its own cursor; a raster list or any
  // non-list surface leaves the cursor to the plugin.
  type PageOptions = {
    title?: string; hint?: string; selectable?: boolean;
    nativeFirmwareList?: boolean; nativeTextPage?: boolean; initialSelectedIndex?: number;
  };
  let currentList: PageOptions | undefined;
  const setSelection = vi.fn(async (index: number) => {
    visibleSelection = index;
    selections.push(index);
    return selectionResult;
  });
  const hud = {
    // As the real presenter answers (HudPresenter.usesCustomListSelection):
    // with `next`, for the page about to be shown by its opt-in; without it,
    // for the page on the glass.
    usesCustomListSelection: (next?: { nativeFirmwareList?: boolean; nativeTextPage?: boolean }) => (next
      ? next.nativeFirmwareList !== true && next.nativeTextPage !== true
      : !(currentList?.nativeFirmwareList || currentList?.nativeTextPage)),
    showList: vi.fn(async (items: string[], options?: PageOptions) => {
      operations.push('list');
      currentList = options;
      // HudPresenter.initialListSelection: a raster list opens on the
      // requested row; a firmware list always starts on row one (the
      // firmware has no cursor-setting API).
      visibleSelection = options?.nativeFirmwareList ? 0 : (options?.initialSelectedIndex ?? 0);
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
    setSelection,
    restoreSelection: vi.fn(async (index: number) => setSelection(index)),
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
      currentList = undefined;
      productivities.push(productivity);
      deferredProductivityVisibleCallbacks.push(onDeferredProductivityVisible);
      const result = Array.isArray(productivityResult)
        ? (productivityResult[productivityCall] ?? true)
        : productivityResult;
      productivityCall += 1;
      return result;
    }),
    isProductivityVisible: vi.fn(() => productivityVisible),
    showDefault: vi.fn(async () => { defaults += 1; currentList = undefined; return true; }),
  } as unknown as HudPresenter;
  const link = { send: (m: Record<string, unknown>) => {
    const event = m.event as { containerName?: string; selectedName?: string } | undefined;
    if (event?.containerName === 'capability-lifecycle' && event.selectedName?.startsWith('surface-')) {
      operations.push(event.selectedName);
      surfaceEvents.push(m);
    } else sent.push(m);
  } } as unknown as MacLink;
  const menu = new CapabilityMenu(hud, new Settings(), link);
  return {
    menu, hud, lists, listOptions, operations, productivities,
    deferredProductivityVisibleCallbacks, selections, moves, statuses, sent, surfaceEvents,
    defaultsCount: () => defaults,
    visibleSelection: () => visibleSelection,
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

describe('Brainstorm surface ownership', () => {
  it('a pending Messages request does not authorize a Brainstorm frame', async () => {
    const { menu } = rig();
    await menu.openFromContextualMenu(CAPABILITIES.indexOf('Messages'));
    expect(menu.isAwaitingResponse()).toBe(true);
    expect(menu.mayPresentBrainstorm(menu.captureBrainstormVisit())).toBe(false);
  });

  it('a final Brainstorm frame does not replace local Settings', async () => {
    const { menu } = rig();
    await menu.openSettings();
    expect(menu.mayPresentBrainstorm(menu.captureBrainstormVisit())).toBe(false);
  });

  it('retires queued and in-flight Brainstorm work before Settings finishes painting', async () => {
    let finish!: (value: boolean) => void;
    const { menu, surfaceEvents, operations } = rig(new Promise<boolean>((resolve) => { finish = resolve; }));
    const oldVisit = menu.captureBrainstormVisit();
    menu.claimBrainstormVisit(oldVisit);
    const controller = new BrainstormController({ sendBrainstorm: () => true });
    controller.present({ screen: 'brainstorm', state: 'running', topic: '', collaborators: [], activity: '' });
    menu.onSurfaceDeparture = () => controller.clear();
    const opening = menu.openSettings();
    expect(controller.isActive()).toBe(false);
    expect(oldVisit()).toBe(false);
    expect(menu.mayPresentBrainstorm(menu.captureBrainstormVisit())).toBe(false);
    expect(menu.claimBrainstormVisit(oldVisit)).toBe(false);
    expect(operations.slice(0, 2)).toEqual(['surface-settings', 'list']);
    expect(surfaceEvents).toEqual([{ kind: 'input', event: {
      containerName: 'capability-lifecycle', selectedName: 'surface-settings',
    } }]);
    finish(true);
    await opening;
  });

  it('notifies Home locally and permits only a subsequent explicit Brainstorm visit', async () => {
    const { menu, surfaceEvents } = rig();
    await menu.openFromContextualMenu(CAPABILITIES.indexOf('Brainstorm'));
    const previous = menu.captureBrainstormVisit();
    expect(menu.mayPresentBrainstorm(previous)).toBe(true);
    await menu.toggle();
    expect(menu.mayPresentBrainstorm(previous)).toBe(false);
    expect(menu.mayPresentBrainstorm(menu.captureBrainstormVisit())).toBe(false);
    expect(surfaceEvents[surfaceEvents.length - 1]?.event).toEqual({ containerName: 'capability-lifecycle', selectedName: 'surface-home' });
    await menu.openFromContextualMenu(CAPABILITIES.indexOf('Brainstorm'));
    expect(menu.mayPresentBrainstorm(previous)).toBe(false);
    expect(menu.mayPresentBrainstorm(menu.captureBrainstormVisit())).toBe(true);
  });

  it('suspends the native menu, then preserves the visit only if no feature was chosen', async () => {
    const { menu, surfaceEvents } = rig();
    menu.claimBrainstormVisit(menu.captureBrainstormVisit());
    const beforeOverlay = menu.captureBrainstormVisit();
    menu.setContextualMenuOpen(true);
    expect(beforeOverlay()).toBe(false);
    expect(menu.mayPresentBrainstorm(menu.captureBrainstormVisit())).toBe(false);
    menu.setContextualMenuOpen(false);
    expect(menu.mayPresentBrainstorm(menu.captureBrainstormVisit())).toBe(true);
    menu.setContextualMenuOpen(true);
    await menu.openFromContextualMenu(CAPABILITIES.indexOf('Messages'));
    menu.setContextualMenuOpen(false);
    expect(menu.mayPresentBrainstorm(menu.captureBrainstormVisit())).toBe(false);
    expect(surfaceEvents.map((message) => (message.event as { selectedName: string }).selectedName)).toEqual([
      'surface-context-menu-open', 'surface-context-menu-closed',
      'surface-context-menu-open', 'surface-feature', 'surface-context-menu-closed',
    ]);
  });

  it('retires an accepted Mac list without echoing, while an explicitly queued replay may reopen', async () => {
    const { menu, surfaceEvents } = rig();
    menu.claimBrainstormVisit(menu.captureBrainstormVisit());
    const explicitReplay = menu.captureBrainstormVisit();
    expect(await menu.presentList('MESSAGES', ['Conversation'], 'voice-request-new', true)).toBe(true);
    expect(menu.mayPresentBrainstorm(explicitReplay)).toBe(false);
    expect(surfaceEvents).toEqual([]);
    expect(menu.claimBrainstormVisit(explicitReplay)).toBe(true);
    expect(menu.mayPresentBrainstorm(explicitReplay)).toBe(true);
  });

  it('does not retire Brainstorm for a rejected stale list', async () => {
    const { menu } = rig();
    const visit = menu.captureBrainstormVisit();
    menu.claimBrainstormVisit(visit);
    expect(await menu.presentList('MESSAGES', ['Old conversation'], 'old-request', true)).toBe(false);
    expect(menu.mayPresentBrainstorm(visit)).toBe(true);
  });
});

function conversationSelectionRig(native = false) {
  let completed: HudVisualFrame | undefined;
  const frames: HudVisualFrame[] = [];
  const sent: Array<Record<string, unknown>> = [];
  const hooks: { paint?: (frame: HudVisualFrame) => Promise<void> } = {};
  const client = {
    require: () => ({
      textContainerUpgrade: async () => true,
      rebuildPageContainer: async () => true,
    }),
  } as unknown as BridgeClient;
  const hud = new HudPresenter(client, new HudPageBuilder(), native ? undefined : {
    paint: async (frame) => {
      await hooks.paint?.(frame);
      completed = frame;
      frames.push(frame);
      return true;
    },
    // The Messages picker is a firmware list; the conversation that follows
    // is a pixel page. Crossing that boundary needs the real surface's page
    // transition hooks, so the stub must answer them like production does.
    withPageTransition: async (replace: () => Promise<boolean>) => replace(),
    forgetPage: () => undefined,
    whenIdle: async () => true,
  });
  // Same split as the main rig above: surface announcements retire ownership
  // on the Mac and are not requests. Counting them here would make every
  // "how many times did we ask the Mac?" assertion drift with page routing.
  const surfaceEvents: Array<Record<string, unknown>> = [];
  const link = { send: (message: Record<string, unknown>) => {
    const event = message.event as { containerName?: string; selectedName?: string } | undefined;
    if (event?.containerName === 'capability-lifecycle' && event.selectedName?.startsWith('surface-')) {
      surfaceEvents.push(message);
    } else sent.push(message);
  } } as unknown as MacLink;
  const menu = new CapabilityMenu(hud, new Settings(), link);
  return { menu, hud, sent, surfaceEvents, frames, hooks, completed: () => completed };
}

async function openConversationSelection(r: ReturnType<typeof conversationSelectionRig>) {
  await r.menu.openFromContextualMenu(0);
  const requestId = latestRequestId(r.sent);
  await r.menu.presentList('MESSAGES', ['New message', 'Alex · Preview'], requestId, true);
  r.menu.handleList(click(1));
  return requestId;
}

const actionableConversation = [
  'Latest messages — down for older',
  'Alex · First received body',
  'You · Second message body',
  'Say “JOAT, reply saying …”',
  'Mic off? Long press to enable.',
];

describe('Conversation actionable selection', () => {
  it.each(['text', 'native-zero', 'omitted-zero'] as const)(
    'opens with the first message visibly selected and immediately readable (%s tap)', async (tap) => {
      const r = conversationSelectionRig();
      try {
        const requestId = await openConversationSelection(r);
        expect(await r.menu.presentList(
          'MESSAGES · CONVERSATION', actionableConversation, requestId, true,
          'down older · tap read · say reply · left x2: back',
        )).toBe(true);
        expect(r.completed()).toMatchObject({
          kind: 'list', title: 'MESSAGES · CONVERSATION', selectedIndex: 1,
        });
        expect(r.frames.filter((frame) => frame.kind === 'list'
          && frame.title === 'MESSAGES · CONVERSATION')).toHaveLength(1);
        const frame = r.completed();
        if (frame?.kind !== 'list') throw new Error('Expected completed conversation frame');
        expect(selectedMessagesConversationRow(frame.items, frame.selectedIndex)).toBe(0);
        if (tap === 'text') r.menu.handleClick();
        else r.menu.handleList(tap === 'native-zero' ? click(0) : clickRow0());
        expect(r.sent[r.sent.length - 1]?.event).toMatchObject({
          containerName: 'capmessage', selectedIndex: 1, requestId,
        });
      } finally { r.menu.releaseToLiveSession(); }
    },
  );

  it('awaits the first actionable highlight before admitting an immediate tap', async () => {
    const r = conversationSelectionRig();
    let release!: () => void;
    const held = new Promise<void>((resolve) => { release = resolve; });
    let selectionStarted = false;
    let opening: Promise<boolean> | undefined;
    try {
      const requestId = await openConversationSelection(r);
      r.hooks.paint = async (frame) => {
        if (frame.kind === 'list' && frame.title === 'MESSAGES · CONVERSATION'
          && frame.selectedIndex === 1) {
          selectionStarted = true;
          await held;
        }
      };
      let finished = false;
      opening = r.menu.presentList('MESSAGES · CONVERSATION', actionableConversation, requestId, true)
        .then((result) => { finished = true; return result; });
      await vi.waitFor(() => expect(selectionStarted).toBe(true));
      expect(finished).toBe(false);
      expect(r.menu.handleClick()).toBe(true);
      expect(r.sent).toHaveLength(2);
      release();
      await opening;
      expect(r.completed()).toMatchObject({ kind: 'list', selectedIndex: 1 });
      r.menu.handleClick();
      expect(r.sent[r.sent.length - 1]?.event).toMatchObject({ containerName: 'capmessage', selectedIndex: 1 });
    } finally {
      release();
      await opening;
      r.menu.releaseToLiveSession();
    }
  });

  it('pages at the last actual message instead of stepping into invisible reply instructions', async () => {
    const r = conversationSelectionRig();
    try {
      const requestId = await openConversationSelection(r);
      await r.menu.presentList('MESSAGES · CONVERSATION', actionableConversation, requestId, true, 'down older');
      r.menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
      await vi.waitFor(() => expect(r.completed()).toMatchObject({ kind: 'list', selectedIndex: 2 }));
      r.menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
      r.menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
      expect(r.sent).toHaveLength(3);
      expect(r.sent[2]?.event).toMatchObject({
        containerName: 'cappage', selectedName: 'MESSAGES',
        eventType: OsEventTypeList.SCROLL_BOTTOM_EVENT, requestId,
      });
      expect(r.completed()).toMatchObject({ kind: 'list', selectedIndex: 2 });
    } finally { r.menu.releaseToLiveSession(); }
  });

  it('pages upward from the first actual message without an invisible header stop', async () => {
    const r = conversationSelectionRig();
    try {
      const requestId = await openConversationSelection(r);
      await r.menu.presentList('MESSAGES · CONVERSATION', actionableConversation, requestId, true, 'up newer');
      r.menu.handleScroll(OsEventTypeList.SCROLL_TOP_EVENT);
      expect(r.sent[r.sent.length - 1]?.event).toMatchObject({
        containerName: 'cappage', eventType: OsEventTypeList.SCROLL_TOP_EVENT, requestId,
      });
      expect(r.completed()).toMatchObject({ kind: 'list', selectedIndex: 1 });
    } finally { r.menu.releaseToLiveSession(); }
  });

  // Ordered gestures (2026-09-11, usesOrderedSelection): on the raster
  // conversation a native List click wrapped around the custom surface taps
  // the row the wearer SEES. Its hidden index owns nothing here: neither an
  // instruction row nor another speaker can become the target.
  it('does not turn a reported instruction row into an invisible tap target', async () => {
    const r = conversationSelectionRig();
    try {
      const requestId = await openConversationSelection(r);
      await r.menu.presentList('MESSAGES · CONVERSATION', actionableConversation, requestId, true);
      r.menu.handleList(click(4));
      expect(r.sent).toHaveLength(3);
      expect(r.sent[2]?.event).toMatchObject({ containerName: 'capmessage', selectedIndex: 1, requestId });
      expect(r.completed()).toMatchObject({ kind: 'list', selectedIndex: 1 });
      // That read is in flight: a press neither repeats nor retargets it.
      r.menu.handleClick();
      expect(r.sent).toHaveLength(3);
      expect(r.sent[r.sent.length - 1]?.event).toMatchObject({ containerName: 'capmessage', selectedIndex: 1 });
    } finally { r.menu.releaseToLiveSession(); }
  });

  it('keeps the visible message authoritative when a hidden native cursor reports another speaker', async () => {
    const r = conversationSelectionRig();
    try {
      const requestId = await openConversationSelection(r);
      await r.menu.presentList('MESSAGES · CONVERSATION', actionableConversation, requestId, true);
      expect(r.completed()).toMatchObject({ kind: 'list', selectedIndex: 1 });
      expect(r.menu.handleList(click(2))).toBe(true);
      expect(r.sent).toHaveLength(3);
      expect(r.sent[2]?.event).toMatchObject({ containerName: 'capmessage', selectedIndex: 1, requestId });
      expect(r.completed()).toMatchObject({ kind: 'list', selectedIndex: 1 });
      r.menu.handleClick();
      expect(r.sent).toHaveLength(3);
      expect(r.sent[r.sent.length - 1]?.event).toMatchObject({
        containerName: 'capmessage', selectedIndex: 1, requestId,
      });
    } finally { r.menu.releaseToLiveSession(); }
  });

  it('preserves arbitrary legacy conversation row indices', async () => {
    const r = conversationSelectionRig();
    try {
      const requestId = await openConversationSelection(r);
      await r.menu.presentList('MESSAGES · CONVERSATION', ['Legacy row one', 'Legacy row two'], requestId, true);
      r.menu.handleClick();
      expect(r.sent[r.sent.length - 1]?.event).toMatchObject({ containerName: 'capmessage', selectedIndex: 0 });
    } finally { r.menu.releaseToLiveSession(); }
  });

  it('preserves the actual firmware row index in native fallback', async () => {
    const r = conversationSelectionRig(true);
    try {
      const requestId = await openConversationSelection(r);
      await r.menu.presentList('MESSAGES · CONVERSATION', actionableConversation, requestId, true);
      r.menu.handleList(click(2));
      expect(r.sent[r.sent.length - 1]?.event).toMatchObject({ containerName: 'capmessage', selectedIndex: 2 });
    } finally { r.menu.releaseToLiveSession(); }
  });
});

describe('CapabilityMenu', () => {
  it('recognizes only the explicit safe-action lifecycle namespace', () => {
    expect(isActionLifecycleTitle('ACTION · VERIFYING')).toBe(true);
    expect(isActionLifecycleTitle('ACTION · DONE')).toBe(true);
    expect(isActionLifecycleTitle('ACTIVITY')).toBe(false);
  });

  it('left double-tap at home is a no-op — the firmware menu is the only menu', async () => {
    const { menu, lists, operations, defaultsCount } = rig();
    await menu.toggle();
    expect(menu.isOpen()).toBe(false);
    expect(lists).toHaveLength(0);
    expect(operations).toEqual([]);
    expect(defaultsCount()).toBe(0);
  });

  it('picking Messages opens a full-focus loading screen before asking the Mac', async () => {
    const { menu, lists, listOptions, operations, sent } = rig();
    await menu.openFromContextualMenu(0); // Messages, via the firmware menu
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(lists[0]).toEqual(['Loading recent messages…']);
    expect(listOptions[0]).toEqual({ title: 'MESSAGES', hint: 'left x2: back', nativeFirmwareList: true });
    // Ownership is announced BEFORE the loading frame paints: the Mac must
    // know Home no longer owns the glass before any response can arrive.
    expect(operations).toEqual(['surface-feature', 'list']);
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

  it('replaces a missing Planning response with retry guidance, then accepts its late matching response', async () => {
    vi.useFakeTimers();
    try {
      const { menu, lists, listOptions, sent } = rig();
      await menu.openFromContextualMenu(1); // Planning
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
      await menu.openFromContextualMenu(0); // Messages
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
    await menu.openFromContextualMenu(0); // Messages
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
      await menu.openFromContextualMenu(0); // Messages
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
      const { menu, sent } = rig([true, retryPaint, true]);
      await menu.openFromContextualMenu(1); // Planning
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
      await menu.openFromContextualMenu(1); // Planning
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
      await menu.openFromContextualMenu(4); // via the firmware menu
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
      await menu.openFromContextualMenu(1); // Planning, via the firmware menu
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
      await menu.openFromContextualMenu(1); // Planning, via the firmware menu
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
    await menu.openFromContextualMenu(0); // Messages
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(await menu.presentList('MESSAGES', rows(2), latestRequestId(sent), true)).toBe(true);

    menu.releaseToLiveSession();
    expect(sent[sent.length - 1]).toMatchObject({
      kind: 'input',
      event: { containerName: 'capability-lifecycle', selectedName: 'messages-home' },
    });
  });

  it('Navigation opens from the firmware menu into its destination handoff', async () => {
    const { menu, lists, listOptions, sent } = rig();
    await menu.openFromContextualMenu(2); // Navigation
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(lists[0]).toEqual(['Loading recent navigation…']);
    expect(listOptions[0]?.title).toBe('NAVIGATION');
    expect(sent[0]).toMatchObject({
      kind: 'input',
      event: { containerName: 'capmenu', eventType: 0, selectedIndex: 2, selectedName: 'Navigation' },
    });
  });

  it('Agent Terminal opens from the firmware menu without a More submenu', async () => {
    const { menu, lists, listOptions, sent } = rig();
    await menu.openFromContextualMenu(5); // Agent Terminal
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(lists[0]).toEqual(['Loading recent agent terminal…']);
    expect(listOptions[0]?.title).toBe('AGENT TERMINAL');
    expect(sent[0]).toMatchObject({
      kind: 'input',
      event: {
        // Agent Terminal is index 5 in the 8-row menu since Notifications left.
        containerName: 'capmenu', eventType: 0, selectedIndex: 5, selectedName: 'Agent Terminal',
      },
    });
  });
  it('hands plain-row pages to the firmware list and keeps cursor-owning pages raster', async () => {
    const { menu, listOptions, sent } = rig();
    await menu.openFromContextualMenu(0); // Messages
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(listOptions[0]).toMatchObject({ title: 'MESSAGES', nativeFirmwareList: true });
    expect(await menu.presentList('MESSAGES', rows(3), latestRequestId(sent), true)).toBe(true);
    expect(listOptions[1]).toMatchObject({ title: 'MESSAGES', nativeFirmwareList: true });

    await menu.openFromContextualMenu(6); // Activity
    await vi.waitFor(() => expect(sent.filter((m) => (m.event as { containerName?: string }).containerName === 'capmenu')).toHaveLength(2));
    expect(listOptions[2]).toMatchObject({ title: 'ACTIVITY', nativeFirmwareList: true });
    expect(await menu.presentList('ACTIVITY', rows(2), latestRequestId(sent), true)).toBe(true);
    expect(listOptions[3]).toMatchObject({ title: 'ACTIVITY', nativeFirmwareList: true });

    await menu.openFromContextualMenu(1); // Planning
    await vi.waitFor(() => expect(sent.filter((m) => (m.event as { containerName?: string }).containerName === 'capmenu')).toHaveLength(3));
    // The loading page is a firmware list; only the month grid itself, a
    // page with no Even template, stays raster.
    expect(listOptions[4]).toMatchObject({ title: 'PLANNING', nativeFirmwareList: true });
    expect(await menu.presentList('PLANNING · PLAN · AUGUST 2026', rows(2), latestRequestId(sent), true)).toBe(true);
    expect(listOptions[5]).not.toHaveProperty('nativeFirmwareList');
  });

  it('draws QuickNotes, Notifications and the readers natively, paging the archive by its rows', async () => {
    const { menu, hud, listOptions, sent } = rig();
    hud.usesCustomListSelection = vi.fn((next?: { nativeFirmwareList?: boolean; nativeTextPage?: boolean }) => (
      next ? next.nativeFirmwareList !== true && next.nativeTextPage !== true : true
    ));
    await menu.openFromContextualMenu(CAPABILITIES.indexOf('QuickNotes'));
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    const requestId = latestRequestId(sent);
    const page = [...rows(20), '▼ Older notes'];
    expect(await menu.presentList(
      'QUICKNOTES', page, requestId, true, 'down older · tap: open · left x2: back',
      undefined, undefined, undefined, undefined, { index: 0, count: 2 }, undefined, undefined, { olderIndex: 20 },
    )).toBe(true);
    expect(listOptions[listOptions.length - 1]).toMatchObject({ title: 'QUICKNOTES', nativeFirmwareList: true });

    // The paging row is the archive page request the edge swipe used to send.
    sent.length = 0;
    expect(menu.handleList(click(20))).toBe(true);
    expect(sent[sent.length - 1]).toMatchObject({
      kind: 'input',
      event: { containerName: 'cappage', selectedName: 'QUICKNOTES · LIST', selectedIndex: 0, eventType: OsEventTypeList.SCROLL_BOTTOM_EVENT, requestId },
    });
    expect(await menu.presentList(
      'QUICKNOTES', ['Row 21', 'Row 22', '▲ Newer notes'], requestId, true, 'up newer · tap: open · left x2: back',
      undefined, undefined, undefined, undefined, { index: 1, count: 2 }, undefined, undefined, { newerIndex: 2 },
    )).toBe(true);
    sent.length = 0;
    expect(menu.handleList(click(2))).toBe(true);
    expect(sent[sent.length - 1]).toMatchObject({
      event: { containerName: 'cappage', selectedName: 'QUICKNOTES · LIST', selectedIndex: 1, eventType: OsEventTypeList.SCROLL_TOP_EVENT },
    });
    expect(await menu.presentList(
      'QUICKNOTES', page, requestId, true, 'down older · tap: open · left x2: back',
      undefined, undefined, undefined, undefined, { index: 0, count: 2 }, undefined, undefined, { olderIndex: 20 },
    )).toBe(true);

    // A note row opens its reader, which is a firmware text page.
    sent.length = 0;
    expect(menu.handleList(click(1))).toBe(true);
    expect(sent[sent.length - 1]).toMatchObject({ event: { containerName: 'capdata', selectedName: 'QUICKNOTES', selectedIndex: 1 } });
    expect(await menu.presentList(
      'QUICKNOTES · DETAIL', ['1/2 · Exact text', 'First page'], requestId, false, 'down more · left x2: back',
    )).toBe(true);
    expect(listOptions[listOptions.length - 1]).toMatchObject({ title: 'QUICKNOTES · DETAIL', nativeTextPage: true });
  });

  it('draws the Planning week and day pages as firmware lists and keeps Back with the Mac', async () => {
    const { menu, hud, listOptions, sent } = rig();
    // As the presenter answers: the page about to be shown by its opt-in,
    // the page on the glass by what was shown last.
    hud.usesCustomListSelection = vi.fn((next?: { nativeFirmwareList?: boolean; nativeTextPage?: boolean }) => (
      next ? next.nativeFirmwareList !== true && next.nativeTextPage !== true
        : !(listOptions[listOptions.length - 1] as { nativeFirmwareList?: boolean } | undefined)?.nativeFirmwareList
    ));
    await menu.openFromContextualMenu(CAPABILITIES.indexOf('Planning'));
    await vi.waitFor(() => expect(latestRequestId(sent)).not.toBe(''));
    const requestId = latestRequestId(sent);
    const week = [
      'MON OCT 5 · 2 events', 'TUE OCT 6 · free', 'WED OCT 7 · 1 reminder', 'THU OCT 8 · free',
      'FRI OCT 9 · free', 'SAT OCT 10 · free', 'SUN OCT 11 · free',
      '‹ SEP 28', 'OCT 12 ›', '○ VIEW · MONTH', '● VIEW · WEEK', '○ VIEW · DAY',
    ];
    expect(await menu.presentList(
      'PLANNING · PLAN · OCT 5–11', week, requestId, true, 'tap: open · left x2: back',
      undefined, 'Planning · OCT 5–11', 7, undefined, undefined, undefined, { canGoBack: true, selectedIndex: 0 },
    )).toBe(true);
    expect(listOptions[listOptions.length - 1]).toMatchObject({ title: 'PLANNING · PLAN · OCT 5–11', nativeFirmwareList: true });

    // The firmware reports the tapped row; the Mac resolves it by index.
    sent.length = 0;
    expect(menu.handleList(click(2))).toBe(true);
    expect(sent[sent.length - 1]).toMatchObject({
      kind: 'input', event: { containerName: 'capdata', selectedName: 'PLANNING', selectedIndex: 2, requestId },
    });
    const day = ['[09:00] Standup', '[16:30] □ Call the dentist', '‹ TUE OCT 6', 'THU OCT 8 ›', '○ VIEW · MONTH', '○ VIEW · WEEK', '● VIEW · DAY'];
    expect(await menu.presentList(
      'PLANNING · PLAN · WED OCT 7', day, requestId, true, 'tap: open · left x2: back',
      undefined, 'Planning · WED OCT 7', 2,
      [{ time: 'Today, 09:00', kind: 'event', title: 'Standup' }, { time: 'Today, 16:30', kind: 'reminder', title: 'Call the dentist' }, null, null, null, null, null],
      undefined, undefined, { canGoBack: true, selectedIndex: 2 },
    )).toBe(true);
    expect(listOptions[listOptions.length - 1]).toMatchObject({ title: 'PLANNING · PLAN · WED OCT 7', nativeFirmwareList: true });

    // Back is the Mac's: one planning-back lifecycle, no local repaint.
    sent.length = 0;
    await menu.toggle();
    expect(sent[sent.length - 1]).toMatchObject({
      kind: 'input', event: { containerName: 'capability-lifecycle', selectedName: 'planning-back', requestId },
    });
  });

  it('draws Notifications and a Planning entry natively', async () => {
    const { menu, hud, listOptions, sent } = rig();
    hud.usesCustomListSelection = vi.fn((next?: { nativeFirmwareList?: boolean; nativeTextPage?: boolean }) => (
      next ? next.nativeFirmwareList !== true && next.nativeTextPage !== true : true
    ));
    await menu.openNotifications();
    await vi.waitFor(() => expect(latestRequestId(sent)).not.toBe(''));
    expect(await menu.presentList(
      'NOTIFICATIONS', ['[2m] Dele · Call me', 'Clear JOAT notifications'], latestRequestId(sent), true,
      'tap: read · left x2: back', undefined, undefined, 1,
    )).toBe(true);
    expect(listOptions[listOptions.length - 1]).toMatchObject({ title: 'NOTIFICATIONS', nativeFirmwareList: true });
    expect(await menu.presentList(
      'NOTIFICATIONS · DETAIL', ['Dele · Messages', 'Call me when you arrive.'], latestRequestId(sent), false,
    )).toBe(true);
    expect(listOptions[listOptions.length - 1]).toMatchObject({ title: 'NOTIFICATIONS · DETAIL', nativeTextPage: true });

    await menu.toggle();
    sent.length = 0;
    await menu.openFromContextualMenu(CAPABILITIES.indexOf('Planning'));
    await vi.waitFor(() => expect(latestRequestId(sent)).not.toBe(''));
    const planning = latestRequestId(sent);
    expect(await menu.presentList('PLANNING', ['14:00 · Design review'], planning, true, 'tap: open')).toBe(true);
    // A read-only entry answers a row tap and is a firmware text page.
    expect(menu.handleList(click(0))).toBe(true);
    expect(await menu.presentList(
      'PLANNING · EVENT', ['Design review', '14:00 · Event'], planning, false, 'left x2 Planning',
    )).toBe(true);
    expect(listOptions[listOptions.length - 1]).toMatchObject({ title: 'PLANNING · EVENT', nativeTextPage: true });
  });

  it('draws the message reader as a firmware text page, pages it locally, then asks the Mac past its edge', async () => {
    const { menu, hud, listOptions, lists, sent } = rig();
    let localPages = 1;
    hud.scrollTextPage = vi.fn((direction: 'up' | 'down') => {
      if (direction === 'down' && localPages > 0) { localPages -= 1; return true; }
      return false;
    });
    hud.usesCustomListSelection = vi.fn((next?: { nativeFirmwareList?: boolean; nativeTextPage?: boolean }) => (
      next ? next.nativeFirmwareList !== true && next.nativeTextPage !== true : true
    ));
    await menu.openFromContextualMenu(0); // Messages
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(await menu.presentList('MESSAGES', rows(2), latestRequestId(sent), true)).toBe(true);
    expect(menu.handleList(click(1))).toBe(true);
    const requestId = latestRequestId(sent);
    expect(await menu.presentList(
      'MESSAGES · CONVERSATION', ['Latest messages', 'Dele · Opening words', 'Say reply'], requestId, true,
      'tap read · say reply · left x2: back',
    )).toBe(true);
    menu.handleList(click(1));
    const items = ['1/2 · From Dele', 'Opening words of a long message'];
    expect(await menu.presentList(
      'MESSAGES · MESSAGE', items, requestId, false, 'down more · say reply · left x2: back',
    )).toBe(true);
    // The rows go to the presenter untouched (no raster prose pager) with the opt-in.
    expect(lists[lists.length - 1]).toEqual(items);
    expect(listOptions[listOptions.length - 1]).toMatchObject({ title: 'MESSAGES · MESSAGE', nativeTextPage: true });

    const before = sent.length;
    // First swipe: the text page turns its own page; nothing goes to the Mac.
    expect(menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT)).toBe(true);
    expect(sent).toHaveLength(before);
    // Past the local edge the Mac's footer offers "down more", so the Mac is asked.
    expect(menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT)).toBe(true);
    expect(sent).toHaveLength(before + 1);
    expect(sent[sent.length - 1]).toMatchObject({
      kind: 'input',
      event: { containerName: 'cappage', selectedName: 'MESSAGES', eventType: OsEventTypeList.SCROLL_BOTTOM_EVENT, requestId },
    });
  });

  it('draws a conversation with paging rows as a firmware list and turns their taps into page requests', async () => {
    const { menu, listOptions, sent } = rig();
    await menu.openFromContextualMenu(0); // Messages
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(await menu.presentList('MESSAGES', rows(2), latestRequestId(sent), true)).toBe(true);
    expect(menu.handleList(click(1))).toBe(true);
    const requestId = latestRequestId(sent);
    const thread = [
      'Latest messages',
      'Dele · Unread · Attachment',
      'You · Read · On my way',
      'Say “JOAT, reply saying …”',
      'Mic off? Long press to enable.',
      '▼ Older messages',
    ];
    expect(await menu.presentList(
      'MESSAGES · CONVERSATION', thread, requestId, true, 'tap read · say reply · left x2: back',
      undefined, 'Dele', undefined, undefined, undefined, undefined, undefined, { olderIndex: 5 },
    )).toBe(true);
    expect(listOptions[listOptions.length - 1]).toMatchObject({
      title: 'MESSAGES · CONVERSATION', nativeFirmwareList: true,
    });

    // The firmware reports the tapped row. A heading or instruction row is inert.
    sent.length = 0;
    expect(menu.handleList(clickRow0())).toBe(true);
    expect(menu.handleList(click(3))).toBe(true);
    expect(sent).toEqual([]);

    // A message row reads it, with the raw list index the Mac owns.
    expect(menu.handleList(click(2))).toBe(true);
    expect(sent[sent.length - 1]?.event).toMatchObject({
      containerName: 'capmessage', selectedName: 'MESSAGES', selectedIndex: 2, requestId,
    });
    // The Mac answers that read with the message page; a fresh thread frame
    // restores the conversation before the paging row is used.
    expect(await menu.presentList(
      'MESSAGES · CONVERSATION', thread, requestId, true, 'tap read · say reply · left x2: back',
      undefined, 'Dele', undefined, undefined, undefined, undefined, undefined, { olderIndex: 5 },
    )).toBe(true);

    // The paging row is the page request the swipe used to send.
    sent.length = 0;
    expect(menu.handleList(click(5))).toBe(true);
    expect(sent[sent.length - 1]?.event).toMatchObject({
      containerName: 'cappage', selectedName: 'MESSAGES',
      eventType: OsEventTypeList.SCROLL_BOTTOM_EVENT, requestId,
    });
    // Until that page lands, further taps on the thread are ignored.
    sent.length = 0;
    expect(menu.handleList(click(1))).toBe(true);
    expect(sent).toEqual([]);

    const older = ['Older messages', 'Dele · Earlier', 'Say “JOAT, reply saying …”', '▲ Newer messages'];
    expect(await menu.presentList(
      'MESSAGES · CONVERSATION', older, requestId, true, 'tap read · say reply · left x2: back',
      undefined, 'Dele', undefined, undefined, undefined, undefined, undefined, { newerIndex: 3 },
    )).toBe(true);
    expect(menu.handleList(click(3))).toBe(true);
    expect(sent[sent.length - 1]?.event).toMatchObject({
      containerName: 'cappage', eventType: OsEventTypeList.SCROLL_TOP_EVENT, requestId,
    });
  });

  it('keeps a Messages conversation on the raster page even though the picker is a firmware list', async () => {
    const { menu, listOptions, sent } = rig();
    await menu.openFromContextualMenu(0); // Messages
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(await menu.presentList('MESSAGES', rows(2), latestRequestId(sent), true)).toBe(true);
    expect(menu.handleList(click(1))).toBe(true);
    expect(await menu.presentList(
      'MESSAGES · CONVERSATION', ['Ada · 2m · On my way', 'Say reply to answer'],
      latestRequestId(sent), true, 'say reply',
    )).toBe(true);
    const conversation = listOptions[listOptions.length - 1];
    expect(conversation?.title).toBe('MESSAGES · CONVERSATION');
    expect(conversation).not.toHaveProperty('nativeFirmwareList');
  });

  it('keeps rows four and five reachable after the Mac replaces the loading view', async () => {
    const { menu, moves, sent } = rig();
    await menu.openFromContextualMenu(0); // Messages
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    await menu.presentList('MESSAGES', rows(5), latestRequestId(sent), true);

    expect(menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT)).toBe(true);
    expect(moves).toEqual([1]);
  });

  it('clicking a selected Messages row emits one traced reply-selection event', async () => {
    const { menu, sent } = rig();
    const info = vi.spyOn(console, 'info').mockImplementation(() => {});
    await menu.openFromContextualMenu(0); // Messages
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
    await menu.openFromContextualMenu(0); // Messages
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
    await menu.openFromContextualMenu(0); // Messages
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
    await menu.openFromContextualMenu(0); // Messages
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
    await menu.openFromContextualMenu(0); // Messages
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
    const { menu, lists, sent, visibleSelection } = rig();
    await menu.openFromContextualMenu(0); // Messages
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    const requestId = latestRequestId(sent);
    const conversations = ['New message', 'Dele · Preview'];
    const transcript = ['Latest messages', 'Dele · Opening words', 'Say reply'];
    expect(await menu.presentList('MESSAGES', conversations, requestId, true)).toBe(true);
    menu.handleList(click(1));
    const selectionId = (sent[1]!.event as { traceId: string }).traceId;
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
    expect(visibleSelection()).toBe(1);
    expect(sent[3]).toEqual({
      kind: 'input',
      event: { containerName: 'capability-lifecycle', selectedName: 'messages-conversation', requestId, traceId: selectionId },
    });

    await menu.toggle();
    expect(lists[lists.length - 1]).toEqual(conversations);
    // The picker is a firmware list: Back lands on row one (the firmware
    // has no cursor-setting API), so the row the wearer left is not restored.
    expect(visibleSelection()).toBe(0);
    expect(sent[4]).toEqual({
      kind: 'input',
      event: { containerName: 'capability-lifecycle', selectedName: 'messages-list', requestId, traceId: selectionId },
    });
  });

  it('asks the Mac to cancel discard before enabling refreshed QuickNotes controls', async () => {
    const { menu, lists, sent } = rig();
    const requestId = 'voice-notes-back';
    const recording = ['Save and finish', 'Pause recording', 'Discard recording'];
    await menu.presentList('QUICKNOTES · RECORDING', recording, requestId, true);
    menu.handleList(click(2));
    await menu.presentList('QUICKNOTES · DISCARD?', ['Confirm discard', 'Keep recording'], requestId, true);
    const paints = lists.length;
    await menu.toggle();
    expect(lists).toHaveLength(paints); // No stale Save label while the Mac still maps row zero to discard.
    expect(sent[sent.length - 1]).toEqual({kind: 'input', event: {
      containerName: 'capability-lifecycle', selectedName: 'quicknotes-back', requestId,
    }});
    const requests = sent.length;
    menu.handleList(click(0));
    expect(sent).toHaveLength(requests);
    await menu.presentList('QUICKNOTES · RECORDING', recording, requestId, true);
    menu.handleList(click(0));
    expect(sent[sent.length - 1]).toMatchObject({event: {
      containerName: 'capdata', selectedName: 'QUICKNOTES', selectedIndex: 0, requestId,
    }});
    await menu.toggle();
    expect(menu.isOpen()).toBe(false); // The abandoned discard dialog is not in Back history.
  });

  it('leaves QuickNotes retry after the Mac keep response is lost', async () => {
    vi.useFakeTimers();
    try {
      const { menu, listOptions, sent } = rig();
      const requestId = 'voice-notes-lost';
      await menu.presentList('QUICKNOTES · DISCARD?', ['Confirm discard', 'Keep recording'], requestId, true);
      await menu.toggle();
      const pending = sent.length;
      await menu.toggle();
      expect(sent).toHaveLength(pending); // Do not duplicate an in-flight keep.
      await vi.advanceTimersByTimeAsync(12_000);
      expect(listOptions[listOptions.length - 1]?.title).toBe('QUICKNOTES · RETRY');
      await menu.toggle();
      expect(menu.isOpen()).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it.each(['text', 'native'] as const)('QuickNotes reader pages through %s input and restores the Back target', async (input) => {
    const { menu, hud, sent, lists, visibleSelection } = rig();
    // The reader is a firmware text page (2026-10-05): the presenter turns
    // its own local page first; past that edge the Mac's footer decides.
    // One local page here, consumed by the first swipe down.
    let localPages = 1;
    hud.scrollTextPage = vi.fn((direction: 'up' | 'down') => {
      if (direction === 'down' && localPages > 0) { localPages -= 1; return true; }
      return false;
    });
    await menu.openFromContextualMenu(CAPABILITIES.indexOf('QuickNotes'));
    const requestId = latestRequestId(sent);
    const picker = ['Other note', 'Long exact note'];
    await menu.presentList('QUICKNOTES', picker, requestId, true);
    menu.handleList(click(1));
    const selection = sent[sent.length - 1];
    if (!selection) throw new Error('No note selection was emitted');
    const traceId = (selection.event as { traceId: string }).traceId;
    await menu.presentList('QUICKNOTES · DETAIL', ['1/2 · Exact text', 'First page\n'], requestId, false, 'down more · left x2: back');
    const swipe = (event: number) => input === 'text'
      ? menu.handleScroll(event) : menu.handleList(scroll(event, 0));
    const before = sent.length;
    swipe(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    swipe(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    expect(sent).toHaveLength(before + 1);
    expect(sent[sent.length - 1]).toEqual({kind: 'input', event: {
      containerName: 'cappage', selectedName: 'QUICKNOTES', selectedIndex: 0,
      eventType: OsEventTypeList.SCROLL_BOTTOM_EVENT, requestId, traceId,
    }});
    expect(await menu.presentList('QUICKNOTES · DETAIL', ['2/2 · Exact text', 'Wrong request'], 'old-request', false)).toBe(false);
    await menu.presentList('QUICKNOTES · DETAIL', ['2/2 · Exact text', 'Final page'], requestId, false, 'up previous · left x2: back');
    const lastPage = sent.length;
    swipe(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    expect(sent).toHaveLength(lastPage);
    swipe(OsEventTypeList.SCROLL_TOP_EVENT);
    expect(sent[sent.length - 1]).toMatchObject({event: {selectedIndex: 1, traceId}});
    await menu.toggle();
    expect(lists[lists.length - 1]).toEqual(picker);
    // The archive is a firmware list: Back lands on row one (no
    // cursor-setting API), not on the note the wearer opened.
    expect(visibleSelection()).toBe(0);
    expect(sent[sent.length - 1]).toEqual({kind: 'input', event: {
      containerName: 'capability-lifecycle', selectedName: 'quicknotes-list', requestId,
    }});
    expect(await menu.presentList('QUICKNOTES · DETAIL', ['1/2 · Exact text', 'Late page'], requestId, false)).toBe(false);
    menu.handleList(click(0));
    expect(sent[sent.length - 1]).toMatchObject({event: {
      containerName: 'capdata', selectedName: 'QUICKNOTES', selectedIndex: 0, requestId,
    }});
    await menu.presentList('QUICKNOTES · NOTE', ['1/1 · Recording', 'Full transcript'], requestId, false, 'left x2: back');
    const complete = sent.length;
    swipe(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    expect(sent).toHaveLength(complete);
    await menu.toggle();
    expect(lists[lists.length - 1]).toEqual(picker);
    expect(visibleSelection()).toBe(0);
  });

  it('requests older and newer conversation pages only at scroll boundaries', async () => {
    const { menu, sent } = rig();
    await menu.openFromContextualMenu(0); // Messages
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
    await menu.openFromContextualMenu(0); // Messages
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
    await menu.openFromContextualMenu(1); // Planning // Planning capability
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
    await menu.openFromContextualMenu(1); // Planning
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
    await menu.openFromContextualMenu(1); // Planning
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
    { domain: 'calendar', phase: 'failed', title: 'Design review', secondary: 'No calendar item was changed.' },
    { domain: 'calendar', phase: 'check', title: 'Design review', secondary: 'It may have changed. Do not retry.' },
    { domain: 'calendar', phase: 'cancelled', title: 'Design review', secondary: 'No calendar item was changed.' },
    { domain: 'calendar', phase: 'not_saved', title: 'Design review', secondary: 'No calendar item was changed.' },
    { domain: 'reminders', phase: 'verifying', title: 'Buy filters', secondary: 'Checking native read-back' },
    { domain: 'reminders', phase: 'failed', title: 'Buy filters', secondary: 'No reminders item was changed.' },
    { domain: 'reminders', phase: 'check', title: 'Buy filters', secondary: 'It may have changed. Do not retry.' },
    { domain: 'reminders', phase: 'cancelled', title: 'Buy filters', secondary: 'No reminders item was changed.' },
    { domain: 'reminders', phase: 'not_saved', title: 'Buy filters', secondary: 'No reminders item was changed.' },
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

  it.each([
    { domain: 'calendar', phase: 'saved', title: 'Design review', secondary: 'Calendar read-back matched' },
    { domain: 'reminders', phase: 'saved', title: 'Buy filters', secondary: 'Native alarm set · JOAT monitoring armed.' },
    { domain: 'reminders', phase: 'monitoring', title: 'Buy filters', secondary: 'Native alarm is scheduled. JOAT will check at due time.' },
    { domain: 'reminders', phase: 'completed', title: 'Buy filters', secondary: 'Reminders read-back matched' },
  ] satisfies ReadonlyArray<{
    domain: ProductivityHudModel['domain'];
    phase: ProductivityHudModel['phase'];
    title: string;
    secondary: string;
  }>)('refreshes unified Planning at once after a verified $domain $phase outcome, keeping its receipt', async (outcome) => {
    // 2026-09-11: a saved native-model result refreshes the selected agenda
    // immediately while its receipt stays on the glass — no Loading page, no
    // wait for Back, and the anchor rides the request.
    const { menu, lists, listOptions, sent, operations } = rig();
    expect(await menu.presentProductivity({
      domain: outcome.domain,
      phase: outcome.phase,
      title: outcome.title,
      primary: 'Status',
      secondary: outcome.secondary,
      hint: 'left x2 Planning',
      planningAnchorUnixMs: 1_788_274_200_000,
    })).toBe(true);
    expect(operations).toEqual(['productivity', 'surface-feature']);
    expect(lists).toEqual([]);
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({
      kind: 'input',
      event: {
        containerName: 'capmenu', selectedIndex: 1, selectedName: 'Planning',
        planningAnchorUnixMs: 1_788_274_200_000,
      },
    });
    expect(menu.handleClick()).toBe(true); // the retained receipt never writes
    expect(sent).toHaveLength(1);

    // The refreshed agenda lands over the receipt it was requested from.
    expect(await menu.presentList(
      'PLANNING · PLAN · AUGUST 2026', rows(2), latestRequestId(sent), true,
    )).toBe(true);
    expect(listOptions[0]).toMatchObject({
      title: 'PLANNING · PLAN · AUGUST 2026', retainProductivityReceipt: true,
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
    expect(await rig(true, false).menu.presentProductivity(due)).toBe(false);

    let resolveFirst: (value: boolean) => void = () => undefined;
    const firstPaint = new Promise<boolean>((resolve) => { resolveFirst = resolve; });
    const { menu } = rig(true, [firstPaint, true]);
    const stale = menu.presentProductivity(due);
    await Promise.resolve();
    expect(await menu.presentProductivity({ ...due, reminderDueAttemptId: '9b38eafb-6fda-4d4c-bb6e-80b9365854b5' })).toBe(true);
    resolveFirst(true);
    expect(await stale).toBe(false);
  });

  it('hands a deferred due acknowledgement to the presenter without sending it early', async () => {
    const { menu, deferredProductivityVisibleCallbacks } = rig(true, true, false);
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
        true, true, false,
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
    { domain: 'reminders', phase: 'saved' },
    { domain: 'reminders', phase: 'monitoring' },
    { domain: 'reminders', phase: 'completed' },
  ] satisfies ReadonlyArray<{
    domain: ProductivityHudModel['domain'];
    phase: ProductivityHudModel['phase'];
  }>)('returns a verified $domain $phase to its anchored Planning day at once', async (outcome) => {
    // A verified outcome needs no reading time before the agenda: the
    // receipt stays while the anchored refresh is requested immediately,
    // and no second return fires behind it.
    vi.useFakeTimers();
    try {
      const { menu, sent } = rig();
      const planningAnchorUnixMs = 1_788_274_200_000;
      await menu.presentProductivity({
        ...outcome,
        title: 'Native-backed item',
        primary: 'Outcome recorded',
        secondary: 'Read-back complete',
        hint: 'left x2 Planning',
        planningAnchorUnixMs,
      });

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

      await vi.advanceTimersByTimeAsync(5_000);
      expect(sent).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it.each([
    { domain: 'calendar', phase: 'failed' },
    { domain: 'calendar', phase: 'check' },
    { domain: 'calendar', phase: 'cancelled' },
    { domain: 'calendar', phase: 'not_saved' },
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
    // The anchored outcome still on the glass is what the contextual escape
    // must carry. A verified save no longer waits here (it refreshes Planning
    // at once, anchor included — pinned above); a write in flight does.
    const { menu, lists, sent } = rig();
    expect(await menu.presentProductivity({
      domain: 'reminders',
      phase: 'verifying',
      title: 'Prepare report',
      primary: 'Verifying',
      secondary: 'Checking native read-back',
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

  it('keeps an independently verified reminder visible as monitoring over the reloaded agenda', async () => {
    // 2026-09-11: the bridge verified the Reminder and the Mac persisted its
    // monitor, so Planning is reloaded now (anchor included, receipt kept)
    // and the monitoring fact is shown over that agenda once it lands.
    vi.useFakeTimers();
    try {
      const { menu, productivities, lists, listOptions, sent } = rig();
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

      expect(productivities).toHaveLength(1);
      expect(lists).toEqual([]);
      expect(sent).toHaveLength(1);
      expect(sent[0]).toMatchObject({
        kind: 'input',
        event: { containerName: 'capmenu', selectedName: 'Planning', planningAnchorUnixMs },
      });

      // The monitoring receipt waits for the agenda, not for a clock.
      await vi.advanceTimersByTimeAsync(5_000);
      expect(productivities).toHaveLength(1);

      expect(await menu.presentList(
        'PLANNING · PLAN · AUGUST 2026', rows(2), latestRequestId(sent), true,
      )).toBe(true);
      expect(listOptions[0]).toMatchObject({ retainProductivityReceipt: true });
      await vi.advanceTimersByTimeAsync(4_999);
      expect(productivities).toHaveLength(1);

      await vi.advanceTimersByTimeAsync(1);
      expect(productivities).toHaveLength(2);
      expect(productivities[1]).toMatchObject({
        domain: 'reminders',
        phase: 'monitoring',
        title: 'Buy filters',
        primary: 'Monitoring armed',
        secondary: 'Native alarm is scheduled. JOAT will check at due time.',
        hint: 'left x2: back',
        planningAnchorUnixMs,
      });

      // The receipt rides over the agenda already shown: nothing is
      // requested or reloaded again.
      await vi.advanceTimersByTimeAsync(5_000);
      expect(sent).toHaveLength(1);
      expect(lists).toHaveLength(1);
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
      // The verified save asked for its anchored agenda at once.
      expect(sent).toHaveLength(1);
      const refresh = latestRequestId(sent);
      await menu.presentProductivity({
        domain: 'reminders',
        phase: 'due',
        title: 'Buy filters',
        primary: 'Due now',
        secondary: 'This reminder is still incomplete.',
        hint: 'left x2 Planning',
        planningAnchorUnixMs: 1_788_274_200_000,
      });

      // The due page stays: no return fires behind it, and the agenda the
      // save requested can no longer paint over it.
      await vi.advanceTimersByTimeAsync(5_000);
      expect(sent).toHaveLength(1);
      expect(await menu.presentList('PLANNING · PLAN · AUGUST 2026', rows(2), refresh, true)).toBe(false);
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
    await menu.openFromContextualMenu(0); // Messages
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

  it('walks Messages detail back to its cached conversation list, then Home', async () => {
    const { menu, lists, sent, defaultsCount } = rig();
    await menu.openFromContextualMenu(0); // Messages
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    const requestId = latestRequestId(sent);
    const conversationRows = ['New message · voice recipient + text', '[now] Adele · Good morning'];
    expect(await menu.presentList('MESSAGES', conversationRows, requestId, true)).toBe(true);
    menu.handleList(click(1));
    const selectionId = (sent[1]!.event as { traceId: string }).traceId;
    expect(sent).toHaveLength(2);
    expect(await menu.presentList(
      'MESSAGES · CONVERSATION', ['Adele · Good morning', 'Say reply saying …'], requestId, false,
    )).toBe(true);

    await menu.toggle();
    expect(lists[lists.length - 1]).toEqual(conversationRows);
    expect(defaultsCount()).toBe(0);
    expect(sent[2]).toEqual({
      kind: 'input',
      event: { containerName: 'capability-lifecycle', selectedName: 'messages-list', requestId, traceId: selectionId },
    });

    await menu.toggle();
    expect(menu.isOpen()).toBe(false);
    expect(defaultsCount()).toBe(1);
    expect(sent[3]).toEqual({
      kind: 'input',
      event: { containerName: 'capability-lifecycle', selectedName: 'messages-home', requestId, traceId: selectionId },
    });
  });

  it('News opens as a firmware list, reads an article on a text page, and returns to the list', async () => {
    const rebuilds: RebuildPageContainer[] = [];
    const sent: Array<Record<string, unknown>> = [];
    const client = {
      require: () => ({
        textContainerUpgrade: async () => true,
        rebuildPageContainer: async (page: RebuildPageContainer) => { rebuilds.push(page); return true; },
      }),
    } as unknown as BridgeClient;
    const hud = new HudPresenter(client, new HudPageBuilder(), {
      paint: async () => true,
      paintExact: async (_frame: HudVisualFrame, current: () => boolean) => current(),
      forgetPage: () => undefined,
      whenIdle: async () => true,
      withPageTransition: async (replace: () => Promise<boolean>) => replace(),
    } as never);
    const link = { send: (message: Record<string, unknown>) => {
      const event = message.event as { containerName?: string; selectedName?: string } | undefined;
      if (event?.containerName === 'capability-lifecycle'
        && event.selectedName?.startsWith('surface-')) return;
      sent.push(message);
    } } as unknown as MacLink;
    const menu = new CapabilityMenu(hud, new Settings(), link);
    hud.rail.accept({ kind: 'hud.panel', tab: 'news', revision: 7, rows: ['Story 01', 'Story 02'] });
    await menu.openPanelTab('news');
    expect(rebuilds[rebuilds.length - 1]?.listObject?.length ?? 0).toBe(1);
    menu.handleList(click(1));
    expect(sent).toHaveLength(1);
    expect(sent[0]?.event).toMatchObject({
      containerName: 'capdata', selectedName: 'NEWS', selectedIndex: 1, requestId: 'panel-news-7',
    });
    expect(await menu.presentList(
      'NEWS · ARTICLE', ['1/1 · Story 02', 'Complete synthetic article.'], 'panel-news-7', false,
    )).toBe(true);
    const article = rebuilds[rebuilds.length - 1];
    expect(article?.listObject?.length ?? 0).toBe(0);
    expect(article?.textObject?.map((c) => c.containerName)).toEqual(['status', 'main', 'hint']);

    await menu.toggle();
    expect(rebuilds[rebuilds.length - 1]?.listObject?.length ?? 0).toBe(1);
    expect(hud.exportBackgroundState().surface).toMatchObject({
      kind: 'list', options: { title: 'NEWS', nativeFirmwareList: true },
    });
    menu.handleList(clickRow0());
    expect(sent).toHaveLength(2);
    expect(sent[1]?.event).toMatchObject({
      containerName: 'capdata', selectedName: 'NEWS', selectedIndex: 0, requestId: 'panel-news-7',
    });
    menu.releaseToLiveSession();
  });

  it('double-tap from the Messages root returns Home and clears Mac-private Messages state', async () => {
    const { menu, sent, defaultsCount } = rig();
    await menu.openFromContextualMenu(0); // Messages
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    await menu.toggle();          // back
    expect(menu.isOpen()).toBe(false);
    expect(defaultsCount()).toBe(1);
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
    await menu.openFromContextualMenu(3); // via the firmware menu // scrollable first-level Brainstorm
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(await menu.presentList(
      'BRAINSTORM',
      ['Say: “JOAT, brainstorm <question>”'],
      latestRequestId(sent),
      false,
    )).toBe(true);

    await menu.beginLiveSession('brainstorm');

    expect(menu.isOpen()).toBe(false);
    expect(defaultsCount()).toBe(0);
    expect(menu.handleClick()).toBe(false);
  });

  it('rejects a delayed capability response after the wearer returns home', async () => {
    const { menu, sent } = rig();
    await menu.openFromContextualMenu(0); // Messages
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    const requestId = latestRequestId(sent);
    expect(await menu.presentList('MESSAGES', rows(1), requestId, true)).toBe(true);
    await menu.toggle();
    expect(await menu.presentList('MESSAGES', rows(1), requestId, true)).toBe(false);
  });

  it('accepts only the newest capability response, including its setup suffix', async () => {
    const { menu, sent } = rig();
    await menu.openFromContextualMenu(0); // Messages
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    const oldRequest = latestRequestId(sent);
    await menu.toggle(); // back: data -> Home (also retires reply consent)
    await menu.openFromContextualMenu(1); // Planning, a NEW generation
    await vi.waitFor(() => expect(sent).toHaveLength(3));
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
    const { menu, sent, defaultsCount } = rig(false);

    await menu.openSettings();

    expect(menu.isOpen()).toBe(false);
    expect(menu.handleList(clickRow0())).toBe(false);
    expect(sent).toEqual([]);
    expect(defaultsCount()).toBe(1);
  });

  it('closes instead of entering an invisible scrolled capability after a paint failure', async () => {
    const { menu, defaultsCount } = rig(false);
    await menu.openFromContextualMenu(4); // via the firmware menu
    await vi.waitFor(() => expect(menu.isOpen()).toBe(false));

    expect(defaultsCount()).toBe(1);
    expect(menu.handleClick()).toBe(false);
  });

  it('does not request Mac data when the loading page fails to paint', async () => {
    const { menu, sent, defaultsCount } = rig(false);
    await menu.openFromContextualMenu(0); // Messages
    await vi.waitFor(() => expect(menu.isOpen()).toBe(false));

    expect(sent).toEqual([]);
    expect(defaultsCount()).toBe(1);
  });

  it('does not activate response rows until their complete frame paints', async () => {
    let finishResponsePaint: ((shown: boolean) => void) | undefined;
    const responsePaint = new Promise<boolean>((resolve) => {
      finishResponsePaint = resolve;
    });
    const { menu, sent, lists, defaultsCount } = rig([true, responsePaint]);
    await menu.openFromContextualMenu(0); // Messages
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

  it('Settings is phone guidance on a firmware list: no local choice, no Mac action, only Back', async () => {
    // 2026-09-11: listening policy and reconnect belong to the phone's
    // controls. The glasses' Settings slot is a cached firmware menu link, so
    // a scroll or press on any old row index must apply nothing and send
    // nothing; the page is consumed locally until Back.
    const { menu, lists, listOptions, selections, moves, sent } = rig();
    await menu.openSettings(); // contextual-menu item
    expect(lists[0]).toEqual([
      'Manage listening and Mac connections',
      'in JOAT’s phone controls in Even Hub.',
    ]);
    expect(listOptions[0]).toEqual({
      title: 'PHONE CONTROLS',
      hint: 'left x2: back',
      selectable: false,
      nativeFirmwareList: true,
    });
    expect(menu.isOpen()).toBe(true);
    expect(menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT)).toBe(true);
    expect(selections).toEqual([]);
    expect(moves).toEqual([]);
    expect(menu.handleClick()).toBe(true); // the row that used to apply "Mac decides"
    expect(menu.handleList(click(1))).toBe(true);
    expect(sent).toEqual([]);
    expect(menu.isOpen()).toBe(true);
    await menu.toggle();
    expect(menu.isOpen()).toBe(false);
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

  it('binds a conversational Terminal visit and rejects its frames after Home', async () => {
    const { menu, hud } = rig();
    const shown = vi.fn(async () => true);
    hud.showBrainstorm = shown;
    await menu.presentList('MESSAGES', ['Choose a conversation'], 'voice-messages', true);
    expect(await menu.presentList('AGENT TERMINAL', ['Opening hermes…'], 'voice-terminal')).toBe(true);
    const model = { screen: 'agent_terminal' as const, state: 'ready' as const,
      topic: 'hermes', collaborators: ['hermes'], activity: 'Ready' };
    const binding = { requestId: 'voice-terminal', selectionId: 'voice-terminal' };
    expect(await menu.presentTerminalOpening(model, binding, () => true)).toBe(true);
    expect(shown).toHaveBeenCalledTimes(1);
    menu.retireTerminalOpening();
    expect(await menu.presentTerminalOpening(model, binding, () => true)).toBe(false);
    expect(shown).toHaveBeenCalledTimes(1);
  });

  it('returns a terminal conversation to agent choices and rejects late old frames', async () => {
    const { menu, hud, sent } = rig();
    hud.showBrainstorm = vi.fn(async () => true);
    await menu.presentList('AGENT TERMINAL', ['Opening Hermes…'], 'voice-terminal-back');
    const model = { screen: 'agent_terminal' as const, state: 'ready' as const,
      topic: 'Hermes', collaborators: ['Hermes'], activity: 'Reply' };
    const binding = { requestId: 'voice-terminal-back', selectionId: 'voice-terminal-back' };
    expect(await menu.presentTerminalOpening(model, binding, () => true)).toBe(true);
    await menu.toggle();
    expect(sent.some((message: any) => message.event?.containerName === 'capmenu'
      && message.event?.selectedName === 'Agent Terminal')).toBe(true);
    expect(await menu.presentTerminalOpening(model, binding, () => true)).toBe(false);
  });

  it('offers retry if a conversational Terminal frame never paints', async () => {
    vi.useFakeTimers();
    try {
      const { menu, hud, lists } = rig();
      hud.showBrainstorm = vi.fn(async () => false);
      await menu.presentList('AGENT TERMINAL', ['Opening hermes…'], 'voice-lost-terminal');
      await menu.presentTerminalOpening({ screen: 'agent_terminal', state: 'ready',
        topic: 'hermes', collaborators: ['hermes'], activity: 'Ready' },
      { requestId: 'voice-lost-terminal', selectionId: 'voice-lost-terminal' }, () => true);
      await vi.advanceTimersByTimeAsync(15_000);
      expect(lists[lists.length - 1]?.join(' ')).toContain('retry');
    } finally { vi.useRealTimers(); }
  });

  it('hands a live Brainstorm its view without painting Overview between updates', async () => {
    const { menu, defaultsCount } = rig();
    await menu.presentList('BRAINSTORM', ['New brainstorm'], 'voice-brainstorm', true);
    await menu.beginLiveSession('brainstorm');
    await menu.beginLiveSession('brainstorm');
    expect(menu.isOpen()).toBe(false);
    expect(defaultsCount()).toBe(0);
  });

  it('still drops an unsolicited list without the voice prefix', async () => {
    const { menu, lists } = rig();
    const painted = await menu.presentList('MESSAGES', ['row'], 'hud-request-999', true);
    expect(painted).toBe(false);
    expect(lists).toHaveLength(0);
  });

  it('double-tap from Settings is back to Home, not deeper', async () => {
    const { menu, defaultsCount } = rig();
    await menu.openFromContextualMenu(CAPABILITIES.indexOf('Settings'));
    expect(menu.isOpen()).toBe(true);
    await menu.toggle();
    expect(menu.isOpen()).toBe(false);
    expect(defaultsCount()).toBe(1);
  });
});

describe('Planning split dashboard', () => {
  // The hover start reads the WALL CLOCK ("today when the month shows it,
  // else the first in-month day"). Left unpinned, these tests silently
  // change meaning on 1 September: they passed for a year by exercising the
  // fallback, then broke the day the calendar walked into the fixture's own
  // month. Pin it, and cover both branches on purpose instead.
  //
  // A LOCAL date — the code compares against toLocaleDateString('en-CA'),
  // so a UTC midnight would resolve to the previous day west of Greenwich.
  const OUTSIDE_THE_GRID = new Date(2026, 5, 17, 12, 0, 0); // 17 Jun 2026

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(OUTSIDE_THE_GRID);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  function dashboardRig() {
    const panes: Array<{ dateIndex: number; focused: boolean; itemIndex: number }> = [];
    const sent: Array<Record<string, unknown>> = [];
    const hud = {
      usesCustomListSelection: () => true,
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
    await menu.openFromContextualMenu(1); // Planning, via the firmware menu
    await vi.waitFor(() => expect(sent.length).toBeGreaterThan(0));
    const requestId = latestRequestId(sent);
    expect(await menu.presentList(
      'PLANNING · PLAN · SEPTEMBER 2026', rows(11), requestId, true,
      undefined, monthGrid() as never,
    )).toBe(true);
    return { ...rigParts, requestId };
  }

  // Dashboard input is queued behind the previous pane paint
  // (queueDashboardInteraction: a gesture binds to the month it was made on
  // and follows visible paints), so each gesture lands a microtask later.
  const painted = () => vi.advanceTimersByTimeAsync(0);

  it('scroll hovers dates, tap hands focus to the pane, tap opens the entry', async () => {
    const { menu, panes, sent, requestId } = await openDashboard();

    // Hover moves locally — no Mac traffic, one repaint per move.
    const before = sent.length;
    menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    await painted();
    expect(sent.length).toBe(before);
    expect(panes).toHaveLength(1);
    expect(panes[panes.length - 1]).toMatchObject({ dateIndex: 2, focused: false });

    // Return to Sep 1. Today is pinned outside this month, so hover starts
    // on the first in-month day, index 1; the scroll above moved it to 2.
    menu.handleScroll(OsEventTypeList.SCROLL_TOP_EVENT);
    await painted();
    expect(panes[panes.length - 1]).toMatchObject({ dateIndex: 1, focused: false });

    // First tap: the pane takes input. Second tap: the entry opens via the
    // date+position wire, no identifiers.
    menu.handleClick();
    await painted();
    expect(panes[panes.length - 1]).toMatchObject({ focused: true, itemIndex: 0 });
    menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    await painted();
    expect(panes[panes.length - 1]).toMatchObject({ focused: true, itemIndex: 1 });
    menu.handleClick();
    await painted();
    const grid = sent[sent.length - 1] as { event: Record<string, unknown> };
    expect(grid.event.containerName).toBe('capgrid');
    expect(grid.event.selectedIndex).toBe(1);
    expect(grid.event.requestId).toBe(requestId);
    expect(grid.event.planningDay).toBe('2026-09-01');
    expect(grid.event).not.toHaveProperty('planningAnchorUnixMs');
  });

  it('hover starts on today when the shown month contains it', async () => {
    // The other branch of the same line of code. Sep 1 is index 1 in this
    // grid (Mon 31 Aug leads), and it is the only day carrying entries — so
    // landing there is observable through the pane, not just the index.
    vi.setSystemTime(new Date(2026, 8, 1, 9, 0, 0)); // 1 Sep 2026, local
    const { menu, panes } = await openDashboard();
    menu.handleClick(); // the pane takes focus only if today has entries
    await painted();
    expect(panes[panes.length - 1]).toMatchObject({ dateIndex: 1, focused: true });
  });

  it('Back walks pane to grid before leaving the dashboard', async () => {
    const { menu, panes } = await openDashboard();
    menu.handleClick(); // pane focus
    await painted();
    expect(panes[panes.length - 1]).toMatchObject({ focused: true });
    await menu.toggle(); // Back: pane -> grid, still on the dashboard
    expect(panes[panes.length - 1]).toMatchObject({ focused: false });
    expect(menu.isOpen()).toBe(true);
  });

  it('scrolling past the month edge asks the Mac for the neighbour month', async () => {
    const { menu, sent } = await openDashboard();
    // Walk hover to date 0, then one more up crosses the edge.
    for (let i = 0; i < 42; i += 1) menu.handleScroll(OsEventTypeList.SCROLL_TOP_EVENT);
    await painted();
    const monthFlip = sent[sent.length - 1] as { event: Record<string, unknown> };
    expect(monthFlip.event.containerName).toBe('capdata');
    expect(monthFlip.event.selectedIndex).toBe(6); // the month list's ‹ previous row
    // One request per edge crossing, however many swipes queued behind it.
    expect(sent.filter((m) => (m.event as { containerName?: string }).containerName === 'capdata')).toHaveLength(1);
  });

  it('a tap on a free day focuses the pane on its Open day row', async () => {
    // Empty dates also have Open day (dashboardTap), so the pane takes
    // focus on every date; the one row to land on is index 0. The old
    // version of this test asserted the opposite without awaiting the
    // queued paint, so it passed vacuously.
    const { menu, panes } = await openDashboard();
    menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT); // a free day
    await painted();
    const before = panes.length;
    menu.handleClick();
    await painted();
    expect(panes.length).toBe(before + 1);
    expect(panes[panes.length - 1]).toMatchObject({ focused: true, itemIndex: 0 });
  });
});

describe('queued conversational Terminal prelude ownership', () => {
  function queuedPrelude(menu: CapabilityMenu, requestId: string): () => Promise<boolean> {
    const current = menu.captureTerminalPreludeGuard();
    return async () => current()
      ? menu.presentList('AGENT TERMINAL', ['Opening hermes…'], requestId, false, 'left x2: back')
      : false;
  }

  const ready = {
    screen: 'agent_terminal' as const, state: 'ready' as const,
    topic: 'hermes', collaborators: ['hermes'], activity: 'Ready',
  };

  it('drops a prelude received before Home but dispatched afterward, without opening its queued ready frame', async () => {
    const { menu, hud, lists } = rig();
    hud.showBrainstorm = vi.fn(async () => true);
    await menu.presentList('MESSAGES', ['Choose a conversation'], 'voice-old-messages', true);
    const dispatch = queuedPrelude(menu, 'voice-queued-terminal');
    await menu.toggle();
    expect(menu.isOpen()).toBe(false);
    const before = lists.length;

    expect(await dispatch()).toBe(false);
    expect(lists).toHaveLength(before);
    expect(await menu.presentTerminalOpening(ready, {
      requestId: 'voice-queued-terminal', selectionId: 'voice-queued-terminal',
    }, () => true)).toBe(false);
    expect(hud.showBrainstorm).not.toHaveBeenCalled();
    expect(menu.isOpen()).toBe(false);
  });

  it('keeps a newly chosen capability when an older prelude leaves the downlink queue', async () => {
    const { menu, hud, lists, listOptions } = rig();
    hud.showBrainstorm = vi.fn(async () => true);
    const dispatch = queuedPrelude(menu, 'voice-before-quicknotes');
    await menu.openFromContextualMenu(CAPABILITIES.indexOf('QuickNotes'));
    const before = lists.length;

    expect(await dispatch()).toBe(false);
    expect(lists).toHaveLength(before);
    expect(listOptions[listOptions.length - 1]?.title).toBe('QUICKNOTES');
    expect(await menu.presentTerminalOpening(ready, {
      requestId: 'voice-before-quicknotes', selectionId: 'voice-before-quicknotes',
    }, () => true)).toBe(false);
    expect(hud.showBrainstorm).not.toHaveBeenCalled();
    await menu.toggle();
  });

  it('retires a queued prelude on disconnect even before any Terminal binding exists', async () => {
    const { menu, hud, lists, sent } = rig();
    hud.showBrainstorm = vi.fn(async () => true);
    const dispatch = queuedPrelude(menu, 'voice-before-disconnect');
    menu.retireTerminalOpening(false);

    expect(await dispatch()).toBe(false);
    expect(lists).toHaveLength(0);
    expect(sent).toHaveLength(0);
    expect(await menu.presentTerminalOpening(ready, {
      requestId: 'voice-before-disconnect', selectionId: 'voice-before-disconnect',
    }, () => true)).toBe(false);
    expect(hud.showBrainstorm).not.toHaveBeenCalled();
  });

  it('admits a current prelude once before adoption and accepts its normal bound ready frame', async () => {
    const { menu, hud, lists } = rig();
    hud.showBrainstorm = vi.fn(async () => true);
    await menu.presentList('MESSAGES', ['Choose a conversation'], 'voice-current-messages', true);
    const dispatch = queuedPrelude(menu, 'voice-current-terminal');

    expect(await dispatch()).toBe(true);
    expect(lists[lists.length - 1]).toEqual(['Opening hermes…']);
    expect(await menu.presentTerminalOpening(ready, {
      requestId: 'voice-current-terminal', selectionId: 'voice-current-terminal',
    }, () => true)).toBe(true);
    expect(hud.showBrainstorm).toHaveBeenCalledTimes(1);
    expect(menu.isOpen()).toBe(false);
  });

  it('accepts two legitimate queued preludes in order without adoption retiring the later arrival', async () => {
    const { menu, hud, lists } = rig();
    hud.showBrainstorm = vi.fn(async () => true);
    const first = queuedPrelude(menu, 'voice-queued-first');
    const second = queuedPrelude(menu, 'voice-queued-second');

    expect(await first()).toBe(true);
    expect(await menu.presentTerminalOpening(ready, {
      requestId: 'voice-queued-first', selectionId: 'voice-queued-first',
    }, () => true)).toBe(true);
    expect(await second()).toBe(true);
    expect(await menu.presentTerminalOpening(ready, {
      requestId: 'voice-queued-second', selectionId: 'voice-queued-second',
    }, () => true)).toBe(true);
    expect(lists).toEqual([['Opening hermes…'], ['Opening hermes…']]);
    expect(hud.showBrainstorm).toHaveBeenCalledTimes(2);
  });

  it('Home retires both queued preludes, including the one not yet dispatched', async () => {
    const { menu, hud, lists } = rig();
    hud.showBrainstorm = vi.fn(async () => true);
    await menu.presentList('MESSAGES', ['Choose a conversation'], 'voice-before-both', true);
    const first = queuedPrelude(menu, 'voice-retired-first');
    const second = queuedPrelude(menu, 'voice-retired-second');
    await menu.toggle();
    const before = lists.length;

    expect(await first()).toBe(false);
    expect(await second()).toBe(false);
    expect(lists).toHaveLength(before);
    for (const requestId of ['voice-retired-first', 'voice-retired-second']) {
      expect(await menu.presentTerminalOpening(ready, {
        requestId, selectionId: requestId,
      }, () => true)).toBe(false);
    }
    expect(hud.showBrainstorm).not.toHaveBeenCalled();
    expect(menu.isOpen()).toBe(false);
  });

  it('keeps a queued prelude current across an ordinary incoming live-frame handoff', async () => {
    const { menu, hud } = rig();
    hud.showBrainstorm = vi.fn(async () => true);
    const dispatch = queuedPrelude(menu, 'voice-after-incoming-frame');
    await menu.beginLiveSession('brainstorm');

    expect(await dispatch()).toBe(true);
    expect(await menu.presentTerminalOpening(ready, {
      requestId: 'voice-after-incoming-frame', selectionId: 'voice-after-incoming-frame',
    }, () => true)).toBe(true);
    expect(hud.showBrainstorm).toHaveBeenCalledTimes(1);
  });
});
