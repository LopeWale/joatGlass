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
    terminalState: 'ready',
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
    const hint = brainstormActionHint(terminal({ state: 'approval_required' }));
    expect(hint).toContain('terminal approve');
    expect(hint).toContain('terminal deny');
    // The old hint sent them to retry a gateway that is working fine.
    expect(hint).not.toContain('retry');
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
        model: terminal({ terminalState: 'ready' }),
        action: 'begin_terminal_input',
        says: 'speak to the terminal',
      },
      {
        model: terminal({ state: 'running' }),
        action: 'show_brainstorm',
        says: 'scroll',
      },
      {
        model: terminal({ state: 'blocked', terminalState: 'blocked' }),
        action: 'retry_terminal',
        says: 'retry Hermes',
      },
      {
        model: terminal({ state: 'unavailable', terminalState: 'unavailable' }),
        action: 'retry_terminal',
        says: 'retry Hermes',
      },
    ];
    for (const { model, action, says } of cases) {
      expect(tapAction(model)).toBe(action);
      expect(brainstormActionHint(model)).toContain(says);
    }
  });

  it('does not offer an approval the gateway cannot carry', () => {
    // A lane can hold a pending approval while the CONNECTION is down —
    // model.state and model.terminalState are different fields. Saying
    // "terminal approve" into a dead link is advice that cannot be
    // followed, which is the defect this whole file exists to prevent.
    for (const terminalState of ['blocked', 'unavailable'] as const) {
      const hint = brainstormActionHint(
        terminal({ state: 'approval_required', terminalState }),
      );
      expect(hint).toContain('retry Hermes');
      expect(hint).toContain('approve');       // still names the goal
      expect(tapAction(terminal({ state: 'approval_required', terminalState })))
        .toBe('retry_terminal');               // and matches the tap
    }
    // Healthy gateway: the approval words are the useful ones.
    const healthy = brainstormActionHint(
      terminal({ state: 'approval_required', terminalState: 'ready' }),
    );
    expect(healthy).toContain('terminal approve');
    expect(healthy).not.toContain('retry');
  });

  it('keeps every hint inside the row it is drawn in', () => {
    // Centred at 9px across 500px: 500 / (9 * 1266/2048) = 89 columns.
    const states: BrainstormHudState[] = [
      'ready', 'running', 'approval_required', 'blocked', 'unavailable',
      'settled', 'cancelled', 'failed',
    ];
    for (const state of states) {
      for (const terminalState of ['ready', 'blocked', 'unavailable'] as const) {
        const hint = brainstormActionHint(terminal({ state, terminalState }));
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
    const waiting = terminal({
      state: 'approval_required', terminalState: 'approval_required',
      approvals: 1,
    });
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
