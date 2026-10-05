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

/** The wearer's decision clock on a Mac challenge, once the gate is visible. */
export const CONFIRMATION_DECISION_WINDOW_MS = 8_000;
/**
 * What the verdict still costs after the tap: an acceptance leaves the phone
 * only once the page beneath the gate is back (`deliverAfterConfirmationDismissal`),
 * and that restore is one firmware rebuild for a list or up to four image
 * tiles for a raster page.
 */
export const CONFIRMATION_DISMISSAL_BUDGET_MS = 4_000;
const CONFIRMATION_DECISION_FLOOR_MS = 3_000;

/**
 * Fit the decision window inside the Mac's own deadline. The Mac times its
 * challenge from the moment it sends `confirm.request`; the phone's queue and
 * the gate's paint eat into that before the clock here starts, and the
 * dismissal eats into it after the tap. A fixed eight seconds therefore let
 * the wearer tap into a window the Mac had already closed ("Reply
 * cancelled." after a visible tap). The floor keeps a skewed phone clock from
 * cancelling a gate the wearer can still see.
 */
export function confirmationDecisionWindowMs(expiresAtMs: number | undefined, nowMs = Date.now()): number {
  if (typeof expiresAtMs !== 'number' || !Number.isFinite(expiresAtMs)) return CONFIRMATION_DECISION_WINDOW_MS;
  const remaining = expiresAtMs - nowMs - CONFIRMATION_DISMISSAL_BUDGET_MS;
  return Math.max(CONFIRMATION_DECISION_FLOOR_MS, Math.min(CONFIRMATION_DECISION_WINDOW_MS, remaining));
}
