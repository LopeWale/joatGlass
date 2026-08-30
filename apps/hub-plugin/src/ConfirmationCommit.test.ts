import { describe, expect, it, vi } from 'vitest';

import { deliverAfterConfirmationDismissal } from './ConfirmationCommit';

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
