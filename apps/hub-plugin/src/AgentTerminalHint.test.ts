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
        says: 'Brainstorm',
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
