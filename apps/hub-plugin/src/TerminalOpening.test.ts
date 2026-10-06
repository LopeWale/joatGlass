import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { OsEventTypeList } from '@evenrealities/even_hub_sdk';
import { CapabilityMenu } from './CapabilityMenu';
import type { HudPresenter } from './HudPresenter';
import type { MacLink } from './MacLink';
import { Settings } from './Settings';
import { CAPABILITIES, type BrainstormHudModel, type InputEventPayload } from './types';

interface OpeningBinding {
  requestId: string;
  selectionId: string;
}

function deferred() {
  let release!: () => void;
  const promise = new Promise<void>((resolve) => { release = resolve; });
  return { promise, release };
}

const terminal = (state: 'unavailable' | 'ready' = 'ready'): BrainstormHudModel => ({
  screen: 'agent_terminal', state, topic: 'work', collaborators: ['work'],
  activity: state === 'ready' ? 'Ready for your instruction.' : 'Connecting to work…',
  noToolsChecked: false,
});

function rig() {
  const sent: Array<{ kind: string; event?: InputEventPayload }> = [];
  const committed: BrainstormHudModel[] = [];
  const guards: Array<(() => boolean) | undefined> = [];
  let screen = 'home';
  let listHold: ReturnType<typeof deferred> | undefined;
  let paintHold: ReturnType<typeof deferred> | undefined;
  let paintAccepted = true;
  let onSend: ((event: InputEventPayload) => void) | undefined;
  const hud = {
    showList: vi.fn(async (_items: string[], options?: { title?: string }) => {
      const hold = listHold;
      if (hold) await hold.promise;
      screen = `list:${options?.title}`;
      return true;
    }),
    showDefault: vi.fn(async () => { screen = 'home'; return true; }),
    setStatus: vi.fn(async () => true),
    showBrainstorm: vi.fn(async (
      model: BrainstormHudModel, _status?: string, isCurrent?: () => boolean,
    ) => {
      // The renderer's guarded commit is a controlled dependency here.
      // These tests prove the menu supplies a live ownership predicate;
      // separate HudPresenter tests must prove the real bridge behavior.
      guards.push(isCurrent);
      if (isCurrent && !isCurrent()) return false;
      const hold = paintHold;
      if (hold) await hold.promise;
      if ((isCurrent && !isCurrent()) || !paintAccepted) return false;
      committed.push(model);
      screen = `terminal:${model.state}`;
      return true;
    }),
  };
  const link = {
    send: vi.fn((message: { kind: string; event?: InputEventPayload }) => {
      sent.push(message);
      if (message.event) onSend?.(message.event);
    }),
  };
  const menu = new CapabilityMenu(
    hud as unknown as HudPresenter, new Settings(), link as unknown as MacLink,
  );

  async function present(model: BrainstormHudModel, binding: OpeningBinding, isCurrent = () => true) {
    return menu.presentTerminalOpening(model, binding, isCurrent);
  }

  function latest(containerName: string): InputEventPayload {
    const event = [...sent].reverse().find((message) => message.event?.containerName === containerName)?.event;
    expect(event, `missing ${containerName} input`).toBeDefined();
    return event!;
  }

  async function openTerminal(): Promise<string> {
    await menu.openFromContextualMenu(CAPABILITIES.indexOf('Agent Terminal'));
    const requestId = latest('capmenu').requestId;
    expect(requestId).toMatch(/^hud-request-/);
    return requestId!;
  }

  async function picker(requestId: string) {
    expect(await menu.presentList(
      'AGENT TERMINAL', ['work · idle', 'calendar · idle'], requestId, true,
      'tap: open · left x2: back',
    )).toBe(true);
  }

  function select(index = 0): OpeningBinding {
    expect(menu.handleList({
      eventType: OsEventTypeList.CLICK_EVENT, currentSelectItemIndex: index,
    } as never)).toBe(true);
    const event = latest('capdata');
    expect(event.selectedName).toBe('AGENT TERMINAL');
    expect(event.traceId).toMatch(/^hud-/);
    return { requestId: event.requestId!, selectionId: event.traceId! };
  }

  async function choose(): Promise<OpeningBinding> {
    const requestId = await openTerminal();
    await picker(requestId);
    return select();
  }

  return {
    menu, hud, sent, committed, guards, present, openTerminal, picker, select, choose,
    screen: () => screen,
    retire: (notifyMac = true) => menu.retireTerminalOpening(notifyMac),
    setOnSend: (callback: (event: InputEventPayload) => void) => { onSend = callback; },
    rejectPaint: () => { paintAccepted = false; },
    acceptPaint: () => { paintAccepted = true; },
    holdPaint: () => {
      paintHold = deferred();
      const current = paintHold;
      return () => { paintHold = undefined; current.release(); };
    },
    holdList: () => {
      listHold = deferred();
      const current = listHold;
      return () => { listHold = undefined; current.release(); };
    },
  };
}

async function settle() {
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
}

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); });

describe('Terminal opening ownership', () => {
  it('retains the selected pair across connecting and ready without a Home repaint', async () => {
    const fixture = rig();
    const binding = await fixture.choose();
    expect(await fixture.present(terminal('unavailable'), binding)).toBe(true);
    expect(await fixture.present(terminal(), binding)).toBe(true);
    expect(fixture.committed.map((model) => model.state)).toEqual(['unavailable', 'ready']);
    expect(fixture.hud.showDefault).not.toHaveBeenCalled();
    expect(fixture.menu.isOpen()).toBe(false);
    expect(fixture.menu.isAwaitingResponse()).toBe(false);
    await vi.advanceTimersByTimeAsync(12_000);
    expect(fixture.screen()).toBe('terminal:ready');
  });

  it('accepts a busy direct opening bound to its capability request in both fields', async () => {
    const fixture = rig();
    const requestId = await fixture.openTerminal();
    expect(await fixture.present(terminal(), { requestId, selectionId: requestId })).toBe(true);
    expect(fixture.screen()).toBe('terminal:ready');
    expect(fixture.hud.showDefault).not.toHaveBeenCalled();
  });

  it('binds the row before its send can synchronously deliver a response', async () => {
    const fixture = rig();
    const requestId = await fixture.openTerminal();
    await fixture.picker(requestId);
    let response: Promise<boolean> | undefined;
    fixture.setOnSend((event) => {
      if (event.containerName === 'capdata') {
        response = fixture.present(terminal(), { requestId: event.requestId!, selectionId: event.traceId! });
      }
    });
    fixture.select();
    expect(response).toBeDefined();
    expect(await response).toBe(true);
    expect(fixture.screen()).toBe('terminal:ready');
  });

  it.each(['request', 'selection', 'missing-request', 'missing-selection'])(
    'rejects a mismatched or partial %s binding without painting', async (field) => {
      const fixture = rig();
      const binding = await fixture.choose();
      const wrong = { ...binding };
      if (field.includes('request')) wrong.requestId = field.startsWith('missing') ? '' : 'old-request';
      else wrong.selectionId = field.startsWith('missing') ? '' : 'old-selection';
      expect(await fixture.present(terminal(), wrong)).toBe(false);
      expect(fixture.committed).toEqual([]);
      expect(fixture.screen()).toBe('list:AGENT TERMINAL');
      expect(fixture.menu.isOpen()).toBe(true);
    },
  );

  it('rejects a ready frame after Back from the still-visible picker', async () => {
    const fixture = rig();
    const binding = await fixture.choose();
    await fixture.menu.toggle();
    expect(await fixture.present(terminal(), binding)).toBe(false);
    expect(fixture.screen()).toBe('home');
    expect(fixture.committed).toEqual([]);
  });

  it('rejects an old selection when the same request gets a second row selection', async () => {
    const fixture = rig();
    const old = await fixture.choose();
    // A current Mac picker refresh permits another choice without creating a
    // new capability request. Only the row trace distinguishes these opens.
    await fixture.picker(old.requestId);
    const current = fixture.select(1);
    expect(current.requestId).toBe(old.requestId);
    expect(current.selectionId).not.toBe(old.selectionId);
    expect(await fixture.present(terminal(), old)).toBe(false);
    expect(fixture.committed).toEqual([]);
    expect(await fixture.present({ ...terminal(), topic: 'calendar' }, current)).toBe(true);
    expect(fixture.committed.map((model) => model.topic)).toEqual(['calendar']);
  });

  it('rejects old opening frames after another capability or a new Terminal visit', async () => {
    const fixture = rig();
    const old = await fixture.choose();
    await fixture.menu.openFromContextualMenu(CAPABILITIES.indexOf('Messages'));
    expect(await fixture.present(terminal(), old)).toBe(false);
    expect(fixture.screen()).toBe('list:MESSAGES');
    const current = await fixture.choose();
    expect(current.requestId).not.toBe(old.requestId);
    expect(await fixture.present(terminal(), old)).toBe(false);
    expect(await fixture.present(terminal(), current)).toBe(true);
  });

  it.each(['home', 'reconnect'])('retires an accepted opening on explicit %s', async (reason) => {
    const fixture = rig();
    const binding = await fixture.choose();
    expect(await fixture.present(terminal('unavailable'), binding)).toBe(true);
    fixture.retire(reason !== 'reconnect');
    await fixture.hud.showDefault();
    expect(await fixture.present(terminal(), binding, () => true)).toBe(false);
    expect(fixture.screen()).toBe('home');
    expect(fixture.committed.map((model) => model.state)).toEqual(['unavailable']);
  });

  it('does not let an old opening replace a deliberate Brainstorm handoff', async () => {
    const fixture = rig();
    const binding = await fixture.choose();
    expect(await fixture.present(terminal('unavailable'), binding)).toBe(true);
    await fixture.menu.beginLiveSession('brainstorm');
    await fixture.hud.showBrainstorm({ ...terminal(), screen: 'brainstorm', topic: 'current topic' });
    expect(await fixture.present(terminal(), binding)).toBe(false);
    expect(fixture.committed[fixture.committed.length - 1]?.screen).toBe('brainstorm');
  });

  it('rejects an already-aborted transport before changing the picker', async () => {
    const fixture = rig();
    const binding = await fixture.choose();
    expect(await fixture.present(terminal(), binding, () => false)).toBe(false);
    expect(fixture.hud.showBrainstorm).not.toHaveBeenCalled();
    expect(fixture.screen()).toBe('list:AGENT TERMINAL');
  });

  it('keeps opening pending if paint fails and accepts a later current retry', async () => {
    const fixture = rig();
    const binding = await fixture.choose();
    fixture.rejectPaint();
    expect(await fixture.present(terminal(), binding)).toBe(false);
    expect(fixture.menu.isOpen()).toBe(true);
    expect(fixture.menu.isAwaitingResponse()).toBe(true);
    expect(fixture.screen()).toBe('list:AGENT TERMINAL');
    fixture.acceptPaint();
    expect(await fixture.present(terminal(), binding)).toBe(true);
  });

  it('notifies the Mac once with the exact retired pair, before a new capability paints', async () => {
    const fixture = rig();
    const binding = await fixture.choose();
    expect(await fixture.present(terminal('unavailable'), binding)).toBe(true);
    const before = fixture.sent.length;
    const releaseList = fixture.holdList();
    const replacement = fixture.menu.openFromContextualMenu(CAPABILITIES.indexOf('Messages'));
    // Opening a new capability announces the incoming surface too. What this
    // test pins is the RETIREMENT: exactly one, carrying the exact pair the
    // Mac bound, so a late Terminal frame cannot paint over the new page.
    const retirements = () => fixture.sent.slice(before).filter(
      (message) => ((message as { event?: { selectedName?: string } }).event
        ?.selectedName ?? '').startsWith('terminal-'),
    );
    try {
      expect(retirements()).toEqual([{
        kind: 'input', event: {
          containerName: 'capability-lifecycle', selectedName: 'terminal-home',
          requestId: binding.requestId, traceId: binding.selectionId,
        },
      }]);
      fixture.retire();
      expect(retirements()).toHaveLength(1);
      expect(await fixture.present(terminal(), binding)).toBe(false);
    } finally {
      releaseList();
      await replacement;
    }
  });

  it('retires on disconnect without queueing an event to replay after reconnect', async () => {
    const fixture = rig();
    const binding = await fixture.choose();
    const before = fixture.sent.length;
    fixture.retire(false);
    fixture.retire();
    expect(fixture.sent.slice(before)).toEqual([]);
    expect(await fixture.present(terminal(), binding)).toBe(false);
    expect(fixture.menu.mayPresentUnboundTerminal()).toBe(false);
  });

  it('allows an unbound Home handoff or initial legacy response, without an intermediate feature', async () => {
    const fixture = rig();
    expect(fixture.menu.mayPresentUnboundTerminal()).toBe(true);
    await fixture.openTerminal();
    expect(fixture.menu.mayPresentUnboundTerminal()).toBe(true);
    await fixture.menu.beginLiveSession('brainstorm');
    expect(fixture.menu.mayPresentUnboundTerminal()).toBe(true);
  });

  it('rejects unbound frames while a roster, selected row, or accepted bound opening owns Terminal', async () => {
    const fixture = rig();
    const requestId = await fixture.openTerminal();
    await fixture.picker(requestId);
    expect(fixture.menu.mayPresentUnboundTerminal()).toBe(false);
    const binding = fixture.select();
    expect(fixture.menu.mayPresentUnboundTerminal()).toBe(false);
    expect(await fixture.present(terminal('unavailable'), binding)).toBe(true);
    expect(fixture.menu.isOpen()).toBe(false);
    expect(fixture.menu.mayPresentUnboundTerminal()).toBe(false);
  });

  it.each(['Messages', 'Brainstorm'] as const)(
    'rejects unbound Terminal frames before and after a new %s feature paints', async (feature) => {
      const fixture = rig();
      const binding = await fixture.choose();
      expect(await fixture.present(terminal(), binding)).toBe(true);
      const releaseList = fixture.holdList();
      const replacement = fixture.menu.openFromContextualMenu(CAPABILITIES.indexOf(feature));
      try {
        expect(fixture.menu.mayPresentUnboundTerminal()).toBe(false);
      } finally {
        releaseList();
        await replacement;
      }
      expect(fixture.menu.mayPresentUnboundTerminal()).toBe(false);
    },
  );

  it('does not commit an older connecting paint after a newer ready paint starts for the same opening', async () => {
    const fixture = rig();
    const binding = await fixture.choose();
    const releasePaint = fixture.holdPaint();
    const connecting = fixture.present(terminal('unavailable'), binding);
    const ready = fixture.present(terminal(), binding);
    try {
      releasePaint();
      expect(await connecting).toBe(false);
      expect(await ready).toBe(true);
      expect(fixture.committed.map((model) => model.state)).toEqual(['ready']);
      expect(fixture.screen()).toBe('terminal:ready');
    } finally {
      releasePaint();
      await Promise.all([connecting, ready]);
    }
  });

  it.each(['transport', 'new-capability'])(
    'checks %s retirement at the held paint commit, before a newer list finishes', async (change) => {
      const fixture = rig();
      const binding = await fixture.choose();
      const releasePaint = fixture.holdPaint();
      let current = true;
      const pending = fixture.present(terminal(), binding, () => current);
      await settle();
      expect(fixture.hud.showBrainstorm).toHaveBeenCalledTimes(1);
      let releaseList: (() => void) | undefined;
      let replacement: Promise<void> | undefined;
      try {
        if (change === 'transport') current = false;
        else {
          releaseList = fixture.holdList();
          replacement = fixture.menu.openFromContextualMenu(CAPABILITIES.indexOf('Messages'));
        }
        // No await before releasing the old paint: retirement must occur
        // before the replacement's first async host operation completes.
        releasePaint();
        expect(await pending).toBe(false);
        expect(fixture.committed).toEqual([]);
        if (replacement) {
          releaseList?.();
          await replacement;
          expect(fixture.screen()).toBe('list:MESSAGES');
          expect(fixture.menu.isOpen()).toBe(true);
        }
      } finally {
        releasePaint();
        releaseList?.();
        await replacement;
      }
    },
  );
});
