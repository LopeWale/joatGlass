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

function make(accepted = true) {
  const sent: UplinkMessage[] = [];
  const controller = new BrainstormController({
    sendBrainstorm: (message) => {
      if (accepted) sent.push(message);
      return accepted;
    },
  });
  return { controller, sent };
}

describe('BrainstormController', () => {
  it('starts the fixed acceptance only from a safe ready Brainstorm gesture', () => {
    const { controller, sent } = make();
    controller.present(model());

    expect(controller.handleAcceptance('right')).toBe(true);
    expect(sent).toEqual([{
      kind: 'brainstorm.action',
      action: 'begin_acceptance',
      surface: 'brainstorm',
      source: 'right',
      gesture: 'double_press',
    }]);
    expect(controller.consumeAcceptedAction()).toEqual({
      action: 'begin_acceptance', surface: 'brainstorm',
    });
  });

  it('consumes pending and disconnected test gestures without leaking into capture or exit', () => {
    const { controller, sent } = make();
    controller.present(model());
    expect(controller.handleAcceptance('ring')).toBe(true);
    expect(controller.handleAcceptance('ring')).toBe(true);
    expect(controller.handleClick()).toBe(true);
    expect(sent).toHaveLength(1);

    controller.present(model({
      state: 'acceptance_ready',
      activity: 'Tap to start the Hermes acceptance run',
    }));
    expect(controller.handleAcceptance('right')).toBe(true);
    expect(sent).toHaveLength(1);
    expect(controller.handleClick()).toBe(true);
    expect(sent[sent.length - 1]).toEqual({
      kind: 'brainstorm.action', action: 'begin_topic_capture',
    });
    expect(controller.handleClick()).toBe(true);
    expect(sent).toHaveLength(2);

    const offline = make(false);
    offline.controller.present(model());
    expect(offline.controller.handleAcceptance('right')).toBe(true);
    expect(offline.sent).toEqual([]);
  });

  it('rejects acceptance outside the safe ready no-tool Brainstorm surface', () => {
    const { controller, sent } = make();
    for (const unsafe of [
      model({ state: 'running' }),
      model({ terminalState: 'blocked' }),
      model({ noToolsChecked: false }),
      model({ screen: 'agent_terminal' }),
    ]) {
      controller.present(unsafe);
      expect(controller.handleAcceptance('right')).toBe(false);
    }
    expect(sent).toEqual([]);
  });

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

  it('keeps an armed topic capture single-use until the wearer cancels it', () => {
    const { controller, sent } = make();
    controller.present(model({
      state: 'topic_capture',
      topic: 'Speak your question',
      activity: 'Your next spoken phrase starts this Brainstorm.',
    }));

    expect(controller.handleClick()).toBe(true);
    expect(sent).toEqual([]);
    expect(controller.handleCancel()).toBe(true);
    expect(sent).toEqual([{ kind: 'brainstorm.action', action: 'cancel' }]);
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
