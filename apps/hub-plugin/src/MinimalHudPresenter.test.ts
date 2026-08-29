import { describe, expect, it, vi } from 'vitest';
import type { TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import { HudPageBuilder } from './HudPageBuilder';
import { MinimalHudPresenter } from './MinimalHudPresenter';
import { ContainerId } from './types';

describe('MinimalHudPresenter recovery surface', () => {
  it('flattens chrome and reply content into the sole known-visible container', async () => {
    const paints: TextContainerUpgrade[] = [];
    const client = {
      require: () => ({
        textContainerUpgrade: vi.fn(async (paint: TextContainerUpgrade) => {
          paints.push(paint);
          return true;
        }),
        rebuildPageContainer: vi.fn(async () => false),
      }),
    } as unknown as BridgeClient;
    const hud = new MinimalHudPresenter(client, new HudPageBuilder(false));

    await expect(hud.setStatus('JOAT · connected')).resolves.toBe(true);
    await expect(hud.showText('Reply', ContainerId.Main)).resolves.toBe(true);
    await expect(hud.appendText(' continued', ContainerId.Main)).resolves.toBe(true);
    await expect(hud.showText('tap: retry later', ContainerId.Hint)).resolves.toBe(true);

    expect(paints).toHaveLength(4);
    expect(paints.every((paint) => paint.containerID === ContainerId.Main)).toBe(true);
    const last = paints[paints.length - 1];
    expect(last?.content).toContain('JOAT · connected');
    expect(last?.content).toContain('Reply continued');
    expect(last?.content).toContain('tap: retry later');
  });

  it('keeps Chrome as the sole status owner', async () => {
    const client = {
      require: () => ({
        textContainerUpgrade: vi.fn(async () => true),
        rebuildPageContainer: vi.fn(async () => false),
      }),
    } as unknown as BridgeClient;
    const hud = new MinimalHudPresenter(client, new HudPageBuilder(false));
    const owner = vi.fn();
    hud.onStatus = owner;

    await expect(hud.setStatus('JOAT · listening')).resolves.toBe(true);
    expect(owner).toHaveBeenCalledWith('JOAT · listening');
  });

  it('shows an assistant answer transiently, then restores the same recovery content', async () => {
    vi.useFakeTimers();
    try {
      const paints: TextContainerUpgrade[] = [];
      const client = {
        require: () => ({
          textContainerUpgrade: vi.fn(async (paint: TextContainerUpgrade) => {
            paints.push(paint);
            return true;
          }),
          rebuildPageContainer: vi.fn(async () => false),
        }),
      } as unknown as BridgeClient;
      const hud = new MinimalHudPresenter(client, new HudPageBuilder(false));
      await hud.showText('Calendar is still open.', ContainerId.Main);
      await hud.showAssistantOverlay('Good morning.');

      expect(hud.hasAssistantOverlay()).toBe(true);
      expect(paints[paints.length - 1]?.content).toContain('Good morning.');
      expect(paints[paints.length - 1]?.content).toContain('Calendar is still open.');

      expect(hud.settleAssistantOverlay(1_000)).toBe(true);
      await vi.advanceTimersByTimeAsync(1_000);

      expect(hud.hasAssistantOverlay()).toBe(false);
      expect(paints[paints.length - 1]?.content).not.toContain('Good morning.');
      expect(paints[paints.length - 1]?.content).toContain('Calendar is still open.');
    } finally {
      vi.useRealTimers();
    }
  });
});
