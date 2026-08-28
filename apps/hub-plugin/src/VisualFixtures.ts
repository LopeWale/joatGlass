import type { HudPresenter } from './HudPresenter';
import type { ImuMonitor } from './ImuMonitor';

/** Copy used only by explicit development screenshot states. */
export const VISUAL_CONFIRMATION_PROMPT =
  'Send to SIMULATOR TEST (NO SEND)? “Trace only. No message will be sent.”';

const VISUAL_CONFIRMATION_ID = 'visual-confirmation';

/**
 * Arm the explicit development confirmation fixture through the production
 * gate. The fixture is inert with respect to the Mac and external senders,
 * but its simulator Click/Up/Down gestures must behave like the real surface.
 */
export async function runVisualConfirmationFixture(
  hud: Pick<HudPresenter, 'showConfirmation' | 'dismissConfirmation'>,
  imu: Pick<ImuMonitor, 'prepare' | 'activate' | 'cancel'>,
): Promise<boolean> {
  const rendered = hud.showConfirmation(VISUAL_CONFIRMATION_PROMPT, VISUAL_CONFIRMATION_ID);
  imu.prepare(VISUAL_CONFIRMATION_ID, () => {
    void hud.dismissConfirmation(VISUAL_CONFIRMATION_ID);
  });

  let visible = false;
  try {
    visible = await rendered;
  } catch (error) {
    console.error('[joat] visual confirmation render failed:', error);
  }
  if (visible) imu.activate(VISUAL_CONFIRMATION_ID, 8000);
  else imu.cancel(VISUAL_CONFIRMATION_ID);
  return visible;
}
