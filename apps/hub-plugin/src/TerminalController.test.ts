import { describe, expect, it } from 'vitest';
import { TerminalController } from './TerminalController';
import type { AgentTerminalHudModel, UplinkMessage } from './types';

function model(overrides: Partial<AgentTerminalHudModel> = {}): AgentTerminalHudModel {
  return {
    state: 'ready',
    focus: 'Claude · idle',
    lanes: ['Claude · idle', 'Codex · idle', 'Antigravity · idle'],
    activity: 'Ready',
    conversationId: 'claude-1',
    messages: [],
    ...overrides,
  };
}

function make(accepted = true) {
  const sent: UplinkMessage[] = [];
  const controller = new TerminalController({
    sendTerminal: (message) => {
      if (accepted) sent.push(message);
      return accepted;
    },
  });
  return { controller, sent };
}

describe('TerminalController', () => {
  it.each(['ready', 'settled', 'cancelled', 'failed'] as const)(
    'arms spoken terminal input on tap when session state is %s',
    (state) => {
      const { controller, sent } = make();
      controller.present(model({ state }));
      expect(controller.handleClick()).toBe(true);
      expect(sent).toEqual([{ kind: 'terminal.action', action: 'begin_terminal_input' }]);
      expect(controller.consumeAcceptedAction()).toEqual({
        action: 'begin_terminal_input',
        surface: 'agent_terminal',
      });
      expect(controller.consumeAcceptedAction()).toBeUndefined();
    },
  );

  it.each(['blocked', 'unavailable', 'approval_required'] as const)(
    'sends retry_terminal on tap when state is %s with no pending tool approvals',
    (state) => {
      const { controller, sent } = make();
      controller.present(model({ state, approvals: 0 }));
      expect(controller.handleClick()).toBe(true);
      expect(sent).toEqual([{ kind: 'terminal.action', action: 'retry_terminal' }]);
    },
  );

  it('Prioritizes tool approvals (tap: approve, swipe: deny) over running or connection states', () => {
    const { controller, sent } = make();
    controller.present(model({ state: 'running', approvals: 1 }));
    expect(controller.handleClick()).toBe(true);
    expect(controller.handleSwipe()).toBe(true);
    expect(sent).toEqual([
      { kind: 'terminal.action', action: 'approve_terminal' },
      { kind: 'terminal.action', action: 'deny_terminal' },
    ]);
  });

  it('keeps a running session visible on tap and leaves swipe to scrollback when approvals === 0', () => {
    const { controller, sent } = make();
    controller.present(model({ state: 'running', approvals: 0 }));
    expect(controller.handleClick()).toBe(true);
    expect(controller.handleSwipe()).toBe(false);
    expect(controller.handleCancel()).toBe(false);
    expect(sent).toEqual([]);
  });

  it('cancels a running job on hold (long press), consuming the gesture even if disconnected', () => {
    const live = make(true);
    live.controller.present(model({ state: 'running' }));
    expect(live.controller.handleLongPress()).toBe(true);
    expect(live.sent).toEqual([{ kind: 'terminal.action', action: 'cancel_terminal' }]);

    const offline = make(false);
    offline.controller.present(model({ state: 'running' }));
    expect(offline.controller.handleLongPress()).toBe(true);
    expect(offline.sent).toEqual([]);

    live.controller.present(model({ state: 'ready' }));
    expect(live.controller.handleLongPress()).toBe(false);
  });

  it('suspends input during OS contextual menu overlay and clears cleanly on departure', () => {
    const { controller, sent } = make();
    controller.present(model({ state: 'ready' }));
    controller.setSuspended(true);
    expect(controller.isActive()).toBe(false);
    expect(controller.handleClick()).toBe(false);
    expect(controller.handleSwipe()).toBe(false);
    expect(controller.handleLongPress()).toBe(false);
    expect(sent).toEqual([]);

    controller.setSuspended(false);
    expect(controller.isActive()).toBe(true);
    expect(controller.handleClick()).toBe(true);
    expect(sent).toEqual([{ kind: 'terminal.action', action: 'begin_terminal_input' }]);

    controller.setSuspended(true);
    controller.clear();
    controller.setSuspended(false);
    expect(controller.isActive()).toBe(false);
    expect(controller.handleClick()).toBe(false);
  });
});
