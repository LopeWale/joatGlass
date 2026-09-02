import { describe, expect, it } from 'vitest';
import { BrainstormController } from './BrainstormController';
import type { BrainstormHudModel, UplinkMessage } from './types';

function model(overrides: Partial<BrainstormHudModel> = {}): BrainstormHudModel {
  return {
    screen: 'brainstorm',
    state: 'ready',
    topic: 'How should JOAT plan today?',
    // The two CLI collaborators, and no one else: Hermes sat in this
    // fixture for as long as the engine wrongly seated it in production.
    collaborators: ['Codex', 'Claude'],
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
  it('does not start diagnostic acceptance from an ordinary ready Brainstorm', () => {
    const { controller, sent } = make();
    controller.present(model());

    expect(controller.handleAcceptance('right')).toBe(false);
    expect(sent).toEqual([]);
    expect(controller.consumeAcceptedAction()).toBeUndefined();
  });

  it('starts the fixed acceptance only from an explicitly armed ready Brainstorm', () => {
    const { controller, sent } = make();
    controller.present({ ...model(), acceptanceArmed: true });

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

  it('consumes pending and disconnected armed test gestures without leaking into capture or exit', () => {
    const { controller, sent } = make();
    controller.present({ ...model(), acceptanceArmed: true });
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
    offline.controller.present({ ...model(), acceptanceArmed: true });
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

describe('swipe ownership by state', () => {
  it('claims a swipe only during topic capture', () => {
    const { controller, sent } = make();
    controller.present(model({ state: 'topic_capture' }));
    expect(controller.handleSwipe()).toBe(true);
    expect(sent.map((m) => (m as { action: string }).action)).toEqual(['cancel']);

    // A RUNNING session must never lose itself to a swipe — the wearer
    // re-reading a reply is the whole reason swipes scroll now.
    controller.present(model({ state: 'running' }));
    expect(controller.handleSwipe()).toBe(false);
    controller.present(model({ state: 'ready' }));
    expect(controller.handleSwipe()).toBe(false);
    expect(sent).toHaveLength(1);
  });
});


describe('answering a blocked work session', () => {
  // Both fields carry the lane's state, as main.ts builds it from the
  // AgentTerminalHudView. `state: 'running'` would mean a BRAINSTORM is
  // live behind this frame, in which case a tap returns to the room —
  // a different, correct behaviour.
  const terminal = (state: string) => ({
    screen: 'agent_terminal', state, topic: 'work · ask1',
    collaborators: [], activity: '> run the audit',
    terminalState: state, noToolsChecked: true,
  });

  it('taps to approve and swipes to deny', () => {
    const { controller, sent } = make();
    controller.present(terminal('approval_required') as never);
    expect(controller.handleClick()).toBe(true);
    expect(controller.handleSwipe()).toBe(true);
    expect(sent.map((m) => (m as { action: string }).action))
      .toEqual(['approve_terminal', 'deny_terminal']);
  });

  it('still retries the daemon when the daemon is what is broken', () => {
    // approval_required and unavailable are different problems. A tap used
    // to send retry_terminal for BOTH, so the wearer retried a connection
    // when what was waiting was a tool request.
    const { controller, sent } = make();
    controller.present(terminal('unavailable') as never);
    expect(controller.handleClick()).toBe(true);
    expect((sent[0] as { action: string }).action).toBe('retry_terminal');
  });

  it('does not claim a swipe on an ordinary terminal', () => {
    const { controller } = make();
    controller.present(terminal('ready') as never);
    expect(controller.handleSwipe()).toBe(false);
  });
});
