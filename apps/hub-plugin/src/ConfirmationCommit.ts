/**
 * A safe write must not race a confirmation surface off the wearer display.
 * The caller supplies the concrete transport so this helper remains free of
 * credentials and transport policy.
 */
export interface ConfirmationCommitBarrier {
  accepted: boolean;
  dismiss: () => Promise<boolean>;
  isAborted: () => boolean;
  deliver: () => void;
}

/**
 * Dismiss and paint the confirmation result before releasing the confirmed
 * action to the Mac. A failed dismissal is fail-closed for an acceptance: the
 * wearer never receives a write whose transition away from review could not
 * be rendered. A refusal is still delivered because it cannot write and must
 * retire the exact Mac-side challenge.
 */
export async function deliverAfterConfirmationDismissal(
  barrier: ConfirmationCommitBarrier,
): Promise<boolean> {
  let dismissed = false;
  try {
    dismissed = await barrier.dismiss();
  } catch (error) {
    if (barrier.accepted || barrier.isAborted()) throw error;
  }
  if (barrier.isAborted() || (barrier.accepted && !dismissed)) return false;
  barrier.deliver();
  return true;
}
