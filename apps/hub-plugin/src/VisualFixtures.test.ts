import { describe, expect, it, vi } from 'vitest';

import {
  VISUAL_CONFIRMATION_PROMPT,
  runVisualConfirmationFixture,
} from './VisualFixtures';

describe('visual fixtures', () => {
  it('makes the confirmation fixture unmistakably test-only', () => {
    expect(VISUAL_CONFIRMATION_PROMPT).toContain('SIMULATOR TEST (NO SEND)');
    expect(VISUAL_CONFIRMATION_PROMPT).toContain('No message will be sent');
    expect(VISUAL_CONFIRMATION_PROMPT).not.toContain('Adele');
    expect(VISUAL_CONFIRMATION_PROMPT).not.toContain('mockups');
  });

  it('arms the fixture gate and dismisses it with the simulator verdict', async () => {
    let settle: ((accepted: boolean) => void) | undefined;
    const hud = {
      showConfirmation: vi.fn(async () => true),
      dismissConfirmation: vi.fn(async () => true),
    };
    const imu = {
      prepare: vi.fn((_actionId: string, onResult: (accepted: boolean) => void) => {
        settle = onResult;
      }),
      activate: vi.fn(() => true),
      cancel: vi.fn(() => true),
    };

    await expect(runVisualConfirmationFixture(hud, imu)).resolves.toBe(true);
    expect(imu.prepare).toHaveBeenCalledWith('visual-confirmation', expect.any(Function));
    expect(imu.activate).toHaveBeenCalledWith('visual-confirmation', 8000);

    settle?.(true);
    expect(hud.dismissConfirmation).toHaveBeenCalledWith('visual-confirmation');
  });
});
