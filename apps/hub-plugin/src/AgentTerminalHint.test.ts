/**
 * The hint on the glass must describe what the gesture actually does.
 *
 * Nothing tested the agent_terminal branches of brainstormActionHint, and
 * two of them were false. handleClick() was corrected so a tap on the
 * terminal arms TERMINAL INPUT — its own comment records that it "used to
 * send begin_topic_capture — i.e. the Agent Terminal's primary gesture
 * started a Brainstorm, a different feature" — but the hint kept
 * advertising "tap: begin Brainstorm". And a lane waiting on an approval
 * read "tap: retry Hermes", the wrong action, while the only useful one
 * ("terminal approve") was never shown.
 *
 * So these tests drive the CONTROLLER and the HINT from the same model and
 * require them to agree. A hint is a promise about behaviour; the two
 * cannot be allowed to drift apart again.
 */
import { describe, expect, it } from 'vitest';

import { BrainstormController } from './BrainstormController';
import { brainstormActionHint } from './HudCanvasRenderer';
import type { BrainstormHudModel, BrainstormHudState, UplinkMessage } from './types';

function terminal(overrides: Partial<BrainstormHudModel> = {}): BrainstormHudModel {
  return {
    screen: 'agent_terminal',
    state: 'ready',
    topic: 'hermes · ready',
    collaborators: ['hermes·idle'],
    activity: 'HERMES > no output yet',
    noToolsChecked: true,
    ...overrides,
  };
}

/** What handleClick actually sends for this model. */
function tapAction(model: BrainstormHudModel): string | undefined {
  const sent: UplinkMessage[] = [];
  const controller = new BrainstormController({
    sendBrainstorm: (message) => { sent.push(message); return true; },
  });
  controller.present(model);
  controller.handleClick();
  const first = sent[0] as { action?: string } | undefined;
  return first?.action;
}

describe('Agent Terminal action hints', () => {
  it('tells the wearer how to answer a pending approval', () => {
    // On the wire, a pending TOOL request is the approvals count; a frame
    // in approval_required with zero is a CONNECTION waiting on device
    // approval in OpenClaw (retry). The old assertion here wanted a
    // voice-only wording from a branch keyed on the render model's
    // terminalState twin — which mirrored state on every real frame, so
    // that wording never showed once.
    const waiting = terminal({ state: 'approval_required', approvals: 1 });
    const hint = brainstormActionHint(waiting);
    expect(hint).toContain('tap: approve');
    expect(hint).toContain('swipe: deny');
    expect(hint).not.toContain('retry');
    expect(tapAction(waiting)).toBe('approve_terminal');
  });

  it('never claims a tap on the terminal begins a Brainstorm', () => {
    // Starting a Brainstorm from the Agent Terminal is a feature-boundary
    // violation, and this hint asserted it on the glass.
    for (const state of ['ready', 'approval_required', 'blocked', 'unavailable'] as const) {
      const hint = brainstormActionHint(terminal({ state }));
      expect(hint).not.toContain('begin Brainstorm');
    }
  });

  it('promises the tap that handleClick actually performs', () => {
    const cases: Array<{
      model: BrainstormHudModel;
      action: string;
      says: string;
    }> = [
      {
        model: terminal({ state: 'ready' }),
        action: 'begin_terminal_input',
        says: 'speak to the terminal',
      },
      {
        model: terminal({ state: 'running' }),
        action: 'show_brainstorm',
        says: 'scroll',
      },
      {
        model: terminal({ state: 'blocked' }),
        action: 'retry_terminal',
        says: 'retry Hermes',
      },
      {
        model: terminal({ state: 'unavailable' }),
        action: 'retry_terminal',
        says: 'retry Hermes',
      },
    ];
    for (const { model, action, says } of cases) {
      expect(tapAction(model)).toBe(action);
      expect(brainstormActionHint(model)).toContain(says);
    }
  });

  it('a device-approval wait retries; hint and tap agree', () => {
    // approval_required with NO pending tool request is the connection
    // waiting on device approval in OpenClaw — remedy: retry.
    const waiting = terminal({ state: 'approval_required', approvals: 0 });
    expect(brainstormActionHint(waiting)).toContain('approve in OpenClaw');
    expect(brainstormActionHint(waiting)).toContain('tap: retry');
    expect(tapAction(waiting)).toBe('retry_terminal');
  });

  it('a pending approval outranks a broken state, in hint AND tap', () => {
    // The controller answers the approval whenever approvals > 0 — that
    // is its branch order — so the hint must promise the same, even on a
    // blocked frame. Keyed on state alone it said 'retry Hermes' while
    // the tap sent approve_terminal: a hint promising one action and the
    // gesture performing another, on the exact frame where the action is
    // consequential.
    for (const state of ['blocked', 'unavailable', 'running'] as const) {
      const waiting = terminal({ state, approvals: 1 });
      expect(brainstormActionHint(waiting)).toContain('tap: approve');
      expect(tapAction(waiting)).toBe('approve_terminal');
    }
  });

  it('keeps every hint inside the row it is drawn in', () => {
    // Centred at 9px across 500px: 500 / (9 * 1266/2048) = 89 columns.
    const states: BrainstormHudState[] = [
      'ready', 'running', 'approval_required', 'blocked', 'unavailable',
      'settled', 'cancelled', 'failed',
    ];
    for (const state of states) {
      for (const approvals of [0, 1]) {
        const hint = brainstormActionHint(terminal({ state, approvals }));
        expect(hint.length).toBeLessThanOrEqual(89);
      }
    }
  });
});

describe('answering an approval by gesture', () => {
  it('names the gestures when the lane itself is what is waiting', () => {
    // The hint must always match the tap. A pending approval on a DEAD
    // connection still taps to retry_terminal, so only the case where the
    // lane can carry the answer may advertise approve/deny.
    const waiting = terminal({ state: 'approval_required', approvals: 1 });
    expect(brainstormActionHint(waiting)).toContain('tap: approve');
    expect(brainstormActionHint(waiting)).toContain('swipe: deny');
    expect(tapAction(waiting)).toBe('approve_terminal');
  });
});

describe('the ready Brainstorm tells you how to start one', () => {
  const ready = (extra: Record<string, unknown> = {}) => ({
    screen: 'brainstorm' as const, state: 'ready' as const,
    noToolsChecked: true, ...extra,
  });

  it('says tap to speak, with no terminal field in sight', () => {
    // This gated on terminalState === 'ready' && noToolsChecked, which a
    // Brainstorm frame no longer carries. It fell through to the generic
    // hint and stopped naming the one action the screen exists for.
    expect(brainstormActionHint(ready() as never)).toContain('tap: speak');
  });

  it('shows the diagnostic gesture only when the Mac armed it', () => {
    expect(brainstormActionHint(ready({ acceptanceArmed: true }) as never))
      .toContain('test');
    expect(brainstormActionHint(ready() as never)).not.toContain('test');
  });
});

describe('the switch gesture is only promised where it works', () => {
  // Caught on a real simulator render: the footer advertised "tap: next
  // session" under a strip reading "LANES > one session", where the tap
  // answers "the only session". A hint naming an effect the code does not
  // produce is the exact defect this file exists to prevent — and I had
  // just introduced a fresh one while fixing the others.
  const lanes = (...names: string[]) => terminal({
    state: 'running', collaborators: names,
  });

  it('names the tap when there is somewhere to cycle to', () => {
    expect(brainstormActionHint(lanes('work·run', 'hermes·idle')))
      .toContain('tap: next session');
  });

  it('says nothing about it on a one-lane Mac', () => {
    const hint = brainstormActionHint(lanes('work·run'));
    expect(hint).not.toContain('next session');
    expect(hint).toContain('swipe: scroll');
  });

  it('says nothing about it when the frame carries no lanes at all', () => {
    expect(brainstormActionHint(terminal({ state: 'running' })))
      .not.toContain('next session');
  });
});
