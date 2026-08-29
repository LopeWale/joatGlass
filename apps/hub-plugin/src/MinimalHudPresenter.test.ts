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
});
