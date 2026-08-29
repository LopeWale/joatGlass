import { describe, expect, it } from 'vitest';
import { BrainstormController } from './BrainstormController';
import type { BrainstormHudModel, UplinkMessage } from './types';

function model(overrides: Partial<BrainstormHudModel> = {}): BrainstormHudModel {
  return {
    screen: 'brainstorm',
    state: 'ready',
    topic: 'How should JOAT plan today?',
    collaborators: ['Codex', 'Claude', 'Hermes'],
    activity: 'Say “JOAT, brainstorm …” to begin.',
    terminalState: 'ready',
    noToolsChecked: true,
    ...overrides,
  };
}

function make() {
  const sent: UplinkMessage[] = [];
  const controller = new BrainstormController({
    sendBrainstorm: (message) => {
      sent.push(message);
      return true;
    },
  });
  return { controller, sent };
}

describe('BrainstormController', () => {
  it('arms spoken topic capture from the ready Brainstorm page', () => {
    const { controller, sent } = make();
    controller.present(model());

    expect(controller.handleClick()).toBe(true);
    expect(sent).toEqual([{ kind: 'brainstorm.action', action: 'begin_topic_capture' }]);
    expect(controller.consumeAcceptedAction()).toEqual({
      action: 'begin_topic_capture', surface: 'brainstorm',
    });
    expect(controller.consumeAcceptedAction()).toBeUndefined();
  });

  it('opens the terminal from an active round without sending prompt content', () => {
    const { controller, sent } = make();
    controller.present(model({ state: 'running' }));

    expect(controller.handleClick()).toBe(true);
    expect(sent).toEqual([{ kind: 'brainstorm.action', action: 'show_terminal' }]);
  });

  it('uses left-double-click only to cancel a live Brainstorm', () => {
    const { controller, sent } = make();
    controller.present(model({ state: 'running' }));

    expect(controller.handleCancel()).toBe(true);
    expect(sent).toEqual([{ kind: 'brainstorm.action', action: 'cancel' }]);
    expect(controller.consumeAcceptedAction()).toEqual({ action: 'cancel', surface: 'brainstorm' });
  });

  it('requests a terminal retry only from an explicitly blocked terminal page', () => {
    const { controller, sent } = make();
    controller.present(model({
      screen: 'agent_terminal',
      state: 'blocked',
      terminalState: 'blocked',
      noToolsChecked: false,
    }));

    expect(controller.handleClick()).toBe(true);
    expect(sent).toEqual([{ kind: 'brainstorm.action', action: 'retry_terminal' }]);
  });

  it('returns from a live terminal overlay to the same active Brainstorm', () => {
    const { controller, sent } = make();
    controller.present(model({
      screen: 'agent_terminal',
      state: 'running',
      terminalState: 'ready',
    }));

    expect(controller.handleClick()).toBe(true);
    expect(sent).toEqual([{ kind: 'brainstorm.action', action: 'show_brainstorm' }]);
  });

  it('drops ownership when another production surface replaces Brainstorm', () => {
    const { controller, sent } = make();
    controller.present(model({ state: 'running' }));
    controller.clear();

    expect(controller.handleClick()).toBe(false);
    expect(controller.handleCancel()).toBe(false);
    expect(sent).toEqual([]);
  });
});
