import { describe, expect, it, vi } from 'vitest';

import {
  CONFIRMATION_DECISION_WINDOW_MS,
  CONFIRMATION_DISMISSAL_BUDGET_MS,
  confirmationDecisionWindowMs,
  deliverAfterConfirmationDismissal,
} from './ConfirmationCommit';

describe('deliverAfterConfirmationDismissal', () => {
  it('does not release a safe write until the confirmation has painted its dismissal', async () => {
    let resolveDismiss: ((value: boolean) => void) | undefined;
    const dismiss = vi.fn(() => new Promise<boolean>((resolve) => {
      resolveDismiss = resolve;
    }));
    const deliver = vi.fn();

    const settled = deliverAfterConfirmationDismissal({
      accepted: true,
      dismiss,
      isAborted: () => false,
      deliver,
    });

    expect(dismiss).toHaveBeenCalledOnce();
    expect(deliver).not.toHaveBeenCalled();
    resolveDismiss?.(true);

    await expect(settled).resolves.toBe(true);
    expect(deliver).toHaveBeenCalledOnce();
  });

  it('fails closed for a write when the exact confirmation cannot be dismissed', async () => {
    const deliver = vi.fn();

    await expect(deliverAfterConfirmationDismissal({
      accepted: true,
      dismiss: async () => false,
      isAborted: () => false,
      deliver,
    })).resolves.toBe(false);

    expect(deliver).not.toHaveBeenCalled();
  });

  it('delivers an explicit refusal even if dismissal fails, unless the generation is stale', async () => {
    const deliver = vi.fn();

    await expect(deliverAfterConfirmationDismissal({
      accepted: false,
      dismiss: async () => false,
      isAborted: () => false,
      deliver,
    })).resolves.toBe(true);
    await expect(deliverAfterConfirmationDismissal({
      accepted: false,
      dismiss: async () => true,
      isAborted: () => true,
      deliver,
    })).resolves.toBe(false);

    expect(deliver).toHaveBeenCalledOnce();
  });

  it('delivers a refusal after a dismissed-paint exception to retire the challenge', async () => {
    const deliver = vi.fn();

    await expect(deliverAfterConfirmationDismissal({
      accepted: false,
      dismiss: async () => { throw new Error('paint failed'); },
      isAborted: () => false,
      deliver,
    })).resolves.toBe(true);

    expect(deliver).toHaveBeenCalledOnce();
  });
});

describe('confirmationDecisionWindowMs', () => {
  const now = 1_700_000_000_000;

  it('keeps the full eight seconds while the Mac deadline comfortably covers them', () => {
    expect(confirmationDecisionWindowMs(now + 20_000, now)).toBe(CONFIRMATION_DECISION_WINDOW_MS);
  });

  it('shrinks the window so the tap and the dismissal still land before the Mac deadline', () => {
    // 9 s left on the Mac clock: 4 s is reserved for restoring the page
    // beneath the gate, so the wearer gets 5 s to decide.
    expect(confirmationDecisionWindowMs(now + 9_000, now)).toBe(9_000 - CONFIRMATION_DISMISSAL_BUDGET_MS);
  });

  it('never cancels a visible gate outright on a skewed phone clock', () => {
    expect(confirmationDecisionWindowMs(now - 60_000, now)).toBe(3_000);
    expect(confirmationDecisionWindowMs(now + 1_000, now)).toBe(3_000);
  });

  it('falls back to eight seconds without a usable deadline', () => {
    expect(confirmationDecisionWindowMs(undefined, now)).toBe(CONFIRMATION_DECISION_WINDOW_MS);
    expect(confirmationDecisionWindowMs(Number.NaN, now)).toBe(CONFIRMATION_DECISION_WINDOW_MS);
  });
});
