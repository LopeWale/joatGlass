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
      model({ state: 'blocked' }),
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
      noToolsChecked: false,
    }));

    expect(controller.handleClick()).toBe(true);
    expect(sent).toEqual([{ kind: 'brainstorm.action', action: 'retry_terminal' }]);
  });

  it('keeps a busy terminal conversation visible on tap', () => {
    const { controller, sent } = make();
    controller.present(model({
      screen: 'agent_terminal',
      state: 'running',
    }));

    expect(controller.handleClick()).toBe(true);
    expect(sent).toEqual([]);
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
    // A pending TOOL request. A connection awaiting device approval in
    // OpenClaw reads the same state with zero here, and taps to retry.
    approvals: state === 'approval_required' ? 1 : 0,
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

describe('approval_required means two different things', () => {
  const base = {
    screen: 'agent_terminal' as const, topic: 'work', collaborators: [],
    activity: '>', noToolsChecked: true,
  };

  it('retries when the CONNECTION is waiting on device approval', () => {
    // No pending tool request: OpenClaw has not approved this device, and
    // the remedy is a retry. Dropping this case while adding the approve
    // gesture made the state do nothing at all.
    const { controller, sent } = make();
    controller.present({
      ...base, state: 'approval_required', approvals: 0,
    } as never);
    expect(controller.handleClick()).toBe(true);
    expect((sent[0] as { action: string }).action).toBe('retry_terminal');
  });

  it('approves when a LANE is waiting on a tool request', () => {
    const { controller, sent } = make();
    controller.present({
      ...base, state: 'approval_required', approvals: 2,
    } as never);
    expect(controller.handleClick()).toBe(true);
    expect((sent[0] as { action: string }).action).toBe('approve_terminal');
  });
});

describe('a Brainstorm frame carries no terminal fields at all', () => {
  // docs/SESSION_MODEL.md:74-75. The wire dropped them in the split, so on
  // a real Brainstorm frame terminalState/noToolsChecked/endpointHost are
  // UNDEFINED. Two things had gated on them and silently stopped working:
  // the ready hint stopped saying "tap: speak", and the diagnostic arm went
  // invisible. This pins that no Brainstorm behaviour depends on them.
  const bare = (state: string) => ({
    screen: 'brainstorm' as const, state,
    topic: 'cold-start latency', collaborators: ['Codex', 'Claude'],
    activity: 'CODEX > cache it',
    // deliberately: no terminalState, no noToolsChecked, no endpointHost.
  });

  it('a ready Brainstorm still arms topic capture', () => {
    const { controller, sent } = make();
    controller.present(bare('ready') as never);
    expect(controller.handleClick()).toBe(true);
    expect((sent[0] as { action: string }).action).toBe('begin_topic_capture');
  });

  it('a running Brainstorm still hands off to the terminal', () => {
    const { controller, sent } = make();
    controller.present(bare('running') as never);
    expect(controller.handleClick()).toBe(true);
    expect((sent[0] as { action: string }).action).toBe('show_terminal');
  });

  it('never reaches a terminal action from a Brainstorm frame', () => {
    const { controller, sent } = make();
    for (const state of ['ready', 'running', 'settled', 'topic_capture']) {
      controller.present(bare(state) as never);
      controller.handleClick();
    }
    const actions = sent.map((m) => (m as { action: string }).action);
    for (const terminalOnly of [
      'begin_terminal_input', 'retry_terminal',
      'approve_terminal', 'deny_terminal',
    ]) {
      expect(actions).not.toContain(terminalOnly);
    }
  });
});

describe('native contextual menu input ownership', () => {
  it('suspends gestures and resumes the existing visit when the overlay closes', () => {
    const { controller, sent } = make();
    controller.present(model({ state: 'running' }));
    controller.setSuspended(true);
    expect(controller.isActive()).toBe(false);
    expect(controller.handleClick()).toBe(false);
    expect(controller.handleCancel()).toBe(false);
    expect(controller.handleSwipe()).toBe(false);
    expect(controller.handleAcceptance('right')).toBe(false);
    expect(sent).toEqual([]);
    controller.setSuspended(false);
    expect(controller.isActive()).toBe(true);
    expect(controller.handleClick()).toBe(true);
    expect(sent).toEqual([{ kind: 'brainstorm.action', action: 'show_terminal' }]);
  });

  it('closing the overlay cannot restore a feature that was left while it was open', () => {
    const { controller, sent } = make();
    controller.present(model({ state: 'running' }));
    controller.setSuspended(true);
    controller.clear();
    controller.setSuspended(false);
    expect(controller.isActive()).toBe(false);
    expect(controller.handleClick()).toBe(false);
    expect(sent).toEqual([]);
  });
});
