import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { OsEventTypeList } from '@evenrealities/even_hub_sdk';
import { CapabilityMenu } from './CapabilityMenu';
import type { HudPresenter } from './HudPresenter';
import type { MacLink } from './MacLink';
import { Settings } from './Settings';
import type { InputEventPayload, MessageUIMessage, PromptPresentMessage, UplinkMessage } from './types';

function observe(menu: CapabilityMenu, message: unknown, current = () => true): boolean {
  return menu.observeMessageUI(message, current);
}
function show(menu: CapabilityMenu, message: MessageUIMessage, current = () => true): Promise<boolean> {
  return menu.presentMessageUI(message, current);
}
function owns(menu: CapabilityMenu, message: PromptPresentMessage): boolean {
  return menu.isMessagePromptCurrent(message);
}
function cancel(menu: CapabilityMenu, actionId?: string): boolean {
  return menu.cancelMessageAction(actionId);
}
function retire(menu: CapabilityMenu, notifyMac = false): void {
  menu.retireMessageUI(notifyMac);
}

function actionCurrent(menu: CapabilityMenu, actionId: string): boolean {
  return menu.isMessageActionCurrent(actionId);
}

function deferred() {
  let release!: () => void;
  const promise = new Promise<void>((resolve) => { release = resolve; });
  return { promise, release };
}

function prompt(actionId = 'messages-ui-one'): PromptPresentMessage {
  return {
    kind: 'prompt.present', promptId: 'question_one', question: 'What should the message say?',
    presentationBinding: 'a'.repeat(64), connectionGeneration: 'session_one',
    source: 'glasses', expiresInMs: 45000, ownerKind: 'messages', ownerId: actionId, field: 'body',
  };
}

function rig() {
  const sent: UplinkMessage[] = [];
  const frames: Array<{ items: string[]; title?: string; selectable?: boolean; hint?: string }> = [];
  const guards: Array<(() => boolean) | undefined> = [];
  const hooks: {
    send?: (message: UplinkMessage) => void;
    paint?: (title: string | undefined) => Promise<void>;
  } = {};
  let visible = 'Overview';
  const hud = {
    showList: vi.fn(async (items: string[], options?: { title?: string; selectable?: boolean; hint?: string }, current?: () => boolean) => {
      guards.push(current);
      await hooks.paint?.(options?.title);
      if (current && !current()) return false;
      frames.push({ items: [...items], ...options });
      visible = options?.title ?? '';
      return true;
    }),
    showDefault: vi.fn(async () => { visible = 'Overview'; return true; }),
    setStatus: vi.fn(async () => true),
  } as unknown as HudPresenter;
  const link = { send: (message: UplinkMessage) => { sent.push(message); hooks.send?.(message); } } as unknown as MacLink;
  const menu = new CapabilityMenu(hud, new Settings(), link);
  return { menu, hud, sent, frames, guards, hooks, visible: () => visible };
}

function events(r: ReturnType<typeof rig>, containerName: string): InputEventPayload[] {
  return r.sent.flatMap((message) => message.kind === 'input' && message.event.containerName === containerName
    ? [message.event] : []);
}

async function picker(r: ReturnType<typeof rig>) {
  await r.menu.openFromContextualMenu(0);
  const requests = events(r, 'capmenu');
  const requestId = requests[requests.length - 1]!.requestId!;
  expect(await r.menu.presentList('MESSAGES', ['New message', 'Alex'], requestId, true)).toBe(true);
  return requestId;
}

function select(r: ReturnType<typeof rig>, index = 1) {
  r.menu.handleList({ eventType: OsEventTypeList.CLICK_EVENT, currentSelectItemIndex: index } as never);
  const selections = events(r, 'capdata');
  const selection = selections[selections.length - 1]!;
  expect(selection?.requestId).toBeTruthy();
  expect(selection?.traceId).toBeTruthy();
  return selection;
}

function ui(selection: InputEventPayload, overrides: Partial<MessageUIMessage> = {}): MessageUIMessage {
  return {
    kind: 'message.ui', requestId: selection.requestId!, selectionId: selection.traceId!,
    actionId: 'messages-ui-one', state: 'preparing', text: 'Preparing message.', ...overrides,
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  // The Messages reader wraps the Mac's rows against JOAT's canvas face
  // (quickNotesProsePages); node has no DOM, so stub the measurement
  // backend the way HudCanvasRenderer.test does. Rows stay lossless slices
  // of the source text, which is all these tests read back.
  const state: Record<string, unknown> = { font: '12px monospace' };
  const fontSize = () => Number(/([\d.]+)px/.exec(String(state.font))?.[1] ?? 12);
  const width = (value: string) => Array.from(value).length * fontSize() * 0.6;
  Object.assign(state, {
    measureText: (value: string) => ({ width: width(value) }),
    fillText: () => undefined,
    getImageData: () => ({ data: new Uint8ClampedArray(576 * 288 * 4) }),
  });
  const context = new Proxy(state, { get: (target, key) => target[String(key)] ?? (() => undefined) });
  vi.stubGlobal('document', { createElement: () => ({ width: 0, height: 0, getContext: () => context }) });
});
afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('Messages action ownership at wire ingress', () => {
  it('gates a confirmation journal request by its exact UI action, including local cancellation and Home', async () => {
    const r = rig();
    await picker(r);
    const selected = select(r);
    expect(observe(r.menu, ui(selected))).toBe(true);
    expect(observe(r.menu, ui(selected, { state: 'awaiting_confirmation' }))).toBe(true);
    expect(actionCurrent(r.menu, 'messages-ui-one')).toBe(true);
    expect(actionCurrent(r.menu, 'journal-uuid-is-not-the-action')).toBe(false);
    expect(cancel(r.menu, 'messages-ui-one')).toBe(true);
    expect(actionCurrent(r.menu, 'messages-ui-one')).toBe(false);
    expect(observe(r.menu, ui(selected, { actionId: 'messages-ui-two' }))).toBe(true);
    expect(actionCurrent(r.menu, 'messages-ui-one')).toBe(false);
    expect(actionCurrent(r.menu, 'messages-ui-two')).toBe(true);
    await r.menu.toggle();
    expect(actionCurrent(r.menu, 'messages-ui-two')).toBe(false);
  });

  it('binds before capdata send and does not rearm a timeout after synchronous preparing ingress', async () => {
    const r = rig();
    await picker(r);
    let accepted = false;
    r.hooks.send = (message) => {
      if (message.kind === 'input' && message.event.containerName === 'capdata') {
        accepted = observe(r.menu, ui(message.event));
      }
    };
    select(r);
    expect(accepted).toBe(true);
    expect(owns(r.menu, prompt())).toBe(true);
    await vi.advanceTimersByTimeAsync(13000);
    expect(r.frames.some((frame) => frame.title === 'MESSAGES · RETRY')).toBe(false);
  });

  it('does not grant a UI prompt before actual selection, while unreserved tool actions remain compatible', async () => {
    const r = rig();
    expect(owns(r.menu, prompt('model-action-one'))).toBe(true);
    expect(owns(r.menu, prompt())).toBe(false);
    const requestId = await picker(r);
    expect(observe(r.menu, ui({ requestId, traceId: 'invented-selection' }))).toBe(false);
    expect(owns(r.menu, prompt())).toBe(false);
  });

  it.each(['requestId', 'selectionId'] as const)('rejects a foreign %s without disturbing the actual pending action', async (field) => {
    const r = rig();
    await picker(r);
    const selected = select(r);
    expect(observe(r.menu, ui(selected))).toBe(true);
    expect(observe(r.menu, ui(selected, { [field]: 'foreign', actionId: 'messages-ui-other' }))).toBe(false);
    expect(owns(r.menu, prompt())).toBe(true);
    expect(owns(r.menu, prompt('messages-ui-other'))).toBe(false);
  });

  it('does not let a second preparing action replace an unfinished action or terminal IDs restart', async () => {
    const r = rig();
    await picker(r);
    const selected = select(r);
    expect(observe(r.menu, ui(selected))).toBe(true);
    expect(observe(r.menu, ui(selected, { actionId: 'messages-ui-two' }))).toBe(false);
    expect(observe(r.menu, ui(selected, { state: 'sent', text: 'Sent. Delivery is unverified.' }))).toBe(true);
    expect(owns(r.menu, prompt())).toBe(false);
    expect(observe(r.menu, ui(selected))).toBe(false);
    expect(observe(r.menu, ui(selected, { actionId: 'messages-ui-two' }))).toBe(true);
    expect(observe(r.menu, ui(selected, { state: 'failed' }))).toBe(false);
    expect(owns(r.menu, prompt('messages-ui-two'))).toBe(true);
  });

  it.each([
    { actionId: 'ordinary-model-id' }, { actionId: 'messages-ui-' + 'a'.repeat(96) },
    { state: 'complete' }, { text: 'x'.repeat(1001) }, { text: 42 }, { actionId: 'messages-ui-bad id' },
  ])('leaves the existing action untouched for malformed input: %j', async (invalid) => {
    const r = rig();
    await picker(r);
    const selected = select(r);
    expect(observe(r.menu, ui(selected))).toBe(true);
    expect(observe(r.menu, { ...ui(selected), ...invalid })).toBe(false);
    expect(owns(r.menu, prompt())).toBe(true);
  });

  it('retains the capdata owner while a full-message read and page request use their own traces', async () => {
    const r = rig();
    const requestId = await picker(r);
    const selected = select(r);
    expect(observe(r.menu, ui(selected))).toBe(true);
    await r.menu.presentList('MESSAGES · CONVERSATION', ['Alex', 'Alex · full message'], requestId, true, 'down older');
    r.menu.handleList({ eventType: OsEventTypeList.CLICK_EVENT, currentSelectItemIndex: 1 } as never);
    expect(events(r, 'capmessage')).toHaveLength(1);
    await r.menu.presentList('MESSAGES · MESSAGE', ['1/2 · From Alex', 'Exact message text.'], requestId, false, 'down more');
    r.menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
    expect(events(r, 'cappage')).toHaveLength(1);
    expect(owns(r.menu, prompt())).toBe(true);
    expect(observe(r.menu, ui(selected, { state: 'awaiting_input' }))).toBe(true);
  });

  it('freezes accepted ingress fields instead of trusting later mutation of the transport object', async () => {
    const r = rig();
    await picker(r);
    const selected = select(r);
    const original = ui(selected);
    expect(observe(r.menu, original)).toBe(true);
    original.actionId = 'messages-ui-mutated';
    expect(owns(r.menu, prompt())).toBe(true);
    expect(owns(r.menu, prompt(original.actionId))).toBe(false);
  });
});

describe('Messages terminal receipts and contextual Back', () => {
  it.each(['cancelled', 'sent', 'failed', 'uncertain'] as const)('shows full %s text read-only and Back restores the actual retained conversation', async (state) => {
    const r = rig();
    const requestId = await picker(r);
    const selected = select(r);
    const conversation = ['Alex', 'Alex · Existing received message.'];
    await r.menu.presentList('MESSAGES · CONVERSATION', conversation, requestId, true);
    expect(observe(r.menu, ui(selected))).toBe(true);
    const text = ('Long safe status; keep this part. '.repeat(20) + 'FINAL STATUS MARKER').slice(0, 1000);
    const message = ui(selected, { state, text });
    expect(observe(r.menu, message)).toBe(true);
    expect(await show(r.menu, message)).toBe(true);
    // The receipt is the full-width prose reader (2026-09-12): a counter row
    // names the state and the part, the rows beneath are lossless slices of
    // the text, and a long receipt pages on swipe instead of being cut off.
    const receipt = r.frames[r.frames.length - 1]!;
    expect(receipt.title).toBe(`MESSAGES · ${state.toUpperCase()}`);
    expect(receipt.items[0]).toMatch(/^1\/1 · [A-Za-z]+( · part 1\/\d+)?$/);
    expect(receipt.selectable).toBe(false);
    let read = receipt.items.slice(1).join('');
    while (!read.endsWith('FINAL STATUS MARKER')) {
      const shown = r.frames.length;
      r.menu.handleScroll(OsEventTypeList.SCROLL_BOTTOM_EVENT);
      await vi.waitFor(() => expect(r.frames.length).toBe(shown + 1));
      const part = r.frames[r.frames.length - 1]!;
      expect(part.title).toBe(receipt.title);
      expect(part.selectable).toBe(false);
      read += part.items.slice(1).join('');
    }
    expect(read).toBe(text);
    const sends = events(r, 'capdata').length;
    r.menu.handleClick();
    expect(events(r, 'capdata')).toHaveLength(sends);
    await r.menu.toggle();
    expect(r.frames[r.frames.length - 1]?.items).toEqual(conversation);
    expect(events(r, 'capability-lifecycle')).toContainEqual(expect.objectContaining({
      selectedName: 'messages-conversation', requestId, traceId: selected.traceId,
    }));
    await r.menu.toggle();
    expect(r.frames[r.frames.length - 1]?.title).toBe('MESSAGES');
    expect(events(r, 'capability-lifecycle')).toContainEqual(expect.objectContaining({
      selectedName: 'messages-list', requestId, traceId: selected.traceId,
    }));
  });

  it('cancels the exact action synchronously, keeps its feature beneath the question, and sends only once', async () => {
    const r = rig();
    const requestId = await picker(r);
    const selected = select(r);
    await r.menu.presentList('MESSAGES · CONVERSATION', ['Alex', 'Existing message'], requestId, true);
    expect(observe(r.menu, ui(selected))).toBe(true);
    let retiredAtSend = false;
    r.hooks.send = (message) => {
      if (message.kind === 'input' && message.event.containerName === 'message-cancel') {
        retiredAtSend = !owns(r.menu, prompt());
      }
    };
    expect(cancel(r.menu, 'messages-ui-stale')).toBe(false);
    expect(cancel(r.menu, 'messages-ui-one')).toBe(true);
    expect(retiredAtSend).toBe(true);
    expect(r.visible()).toBe('MESSAGES · CONVERSATION');
    expect(cancel(r.menu, 'messages-ui-one')).toBe(false);
    expect(events(r, 'message-cancel')).toEqual([expect.objectContaining({
      selectedName: 'messages-ui-one', requestId, traceId: selected.traceId,
    })]);
    await r.menu.toggle();
    expect(r.visible()).toBe('MESSAGES');
  });

  it('retires a live action before awaiting Back to the conversation picker', async () => {
    const r = rig();
    const requestId = await picker(r);
    const selected = select(r);
    await r.menu.presentList('MESSAGES · CONVERSATION', ['Alex', 'Existing message'], requestId, true);
    expect(observe(r.menu, ui(selected))).toBe(true);
    const held = deferred();
    r.hooks.paint = async (title) => { if (title === 'MESSAGES') await held.promise; };
    const back = r.menu.toggle();
    try {
      expect(owns(r.menu, prompt())).toBe(false);
      expect(observe(r.menu, ui(selected, { state: 'awaiting_input' }))).toBe(false);
      held.release();
      await back;
      expect(r.visible()).toBe('MESSAGES');
    } finally { held.release(); await back; }
  });

  it('does not let an old prompt cancellation retire the successor action', async () => {
    const r = rig();
    await picker(r);
    const selected = select(r);
    expect(observe(r.menu, ui(selected))).toBe(true);
    expect(cancel(r.menu, 'messages-ui-one')).toBe(true);
    expect(observe(r.menu, ui(selected, { actionId: 'messages-ui-two' }))).toBe(true);
    expect(cancel(r.menu, 'messages-ui-one')).toBe(false);
    expect(owns(r.menu, prompt('messages-ui-two'))).toBe(true);
    expect(events(r, 'message-cancel')).toHaveLength(1);
  });

  it.each(['home', 'feature', 'disconnect', 'context'] as const)('rejects a late receipt and prompt after %s retires its visit', async (exit) => {
    const r = rig();
    await picker(r);
    const selected = select(r);
    let current = true;
    expect(observe(r.menu, ui(selected), () => current)).toBe(true);
    if (exit === 'home') await r.menu.toggle();
    else if (exit === 'feature') await r.menu.openFromContextualMenu(1);
    else if (exit === 'disconnect') retire(r.menu);
    else current = false;
    expect(owns(r.menu, prompt())).toBe(false);
    const late = ui(selected, { state: 'sent', text: 'Late receipt' });
    expect(observe(r.menu, late, () => current)).toBe(false);
    expect(await show(r.menu, late, () => current)).toBe(false);
    if (exit === 'home' || exit === 'feature') {
      expect(events(r, 'capability-lifecycle')).toContainEqual(expect.objectContaining({
        selectedName: 'messages-home', requestId: selected.requestId, traceId: selected.traceId,
      }));
    }
    if (exit === 'disconnect') expect(events(r, 'message-cancel')).toEqual([]);
  });

  it('checks the bound renderer guard during a held receipt paint so Home remains visible', async () => {
    const r = rig();
    await picker(r);
    const selected = select(r);
    expect(observe(r.menu, ui(selected))).toBe(true);
    const receipt = ui(selected, { state: 'sent', text: 'Sent. Delivery is unverified.' });
    expect(observe(r.menu, receipt)).toBe(true);
    const held = deferred();
    let entered = false;
    r.hooks.paint = async (title) => {
      if (title === 'MESSAGES · SENT') { entered = true; await held.promise; }
    };
    const work = show(r.menu, receipt);
    try {
      await Promise.resolve();
      expect(entered).toBe(true);
      const guard = r.guards[r.guards.length - 1];
      expect(guard?.()).toBe(true);
      await r.menu.toggle();
      expect(guard?.()).toBe(false);
      held.release();
      expect(await work).toBe(false);
      expect(r.menu.isOpen()).toBe(false);
      expect(r.visible()).toBe('Overview');
    } finally { held.release(); await work; }
  });
});
