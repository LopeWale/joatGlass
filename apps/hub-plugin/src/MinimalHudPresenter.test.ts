import { describe, expect, it, vi } from 'vitest';
import type { TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import { HudPageBuilder } from './HudPageBuilder';
import { MinimalHudPresenter } from './MinimalHudPresenter';
import { ContainerId, type AssistantConversationSnapshot } from './types';

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

  it('flattens a persistent conversation into one container without attempting a rich rebuild', async () => {
    const paints: TextContainerUpgrade[] = [];
    const rebuildPageContainer = vi.fn(async () => false);
    const client = {
      require: () => ({
        textContainerUpgrade: vi.fn(async (paint: TextContainerUpgrade) => {
          paints.push(paint);
          return true;
        }),
        rebuildPageContainer,
      }),
    } as unknown as BridgeClient;
    const hud = new MinimalHudPresenter(client, new HudPageBuilder(false));
    const view: AssistantConversationSnapshot = {
      conversationId: 'compat-chat', revision: 2, state: 'ready',
      turns: [
        { role: 'user', text: 'What is the capital of France?' },
        { role: 'assistant', text: 'Paris.' },
      ],
      continueAfterMs: 30_000, earlierTurnsOmitted: false,
    };

    await expect(hud.showAssistantConversation(view)).resolves.toBe(true);

    expect(rebuildPageContainer).not.toHaveBeenCalled();
    expect(paints[paints.length - 1]?.containerID).toBe(ContainerId.Main);
    expect(paints[paints.length - 1]?.content).toContain('YOU · What is the capital of France?');
    expect(paints[paints.length - 1]?.content).toContain('JOAT · Paris.');
    expect(hud.assistantConversationIdentity()).toEqual({
      conversationId: 'compat-chat', revision: 2,
    });
  });

  it('retires its flattened conversation and timer when the display is suspended', async () => {
    vi.useFakeTimers();
    try {
      const textContainerUpgrade = vi.fn(async () => true);
      const client = {
        require: () => ({
          textContainerUpgrade,
          rebuildPageContainer: vi.fn(async () => false),
        }),
        suspendDisplay: vi.fn(),
      } as unknown as BridgeClient;
      const hud = new MinimalHudPresenter(client, new HudPageBuilder(false));
      await hud.showAssistantConversation({
        conversationId: 'compat-chat', revision: 2, state: 'ready',
        turns: [
          { role: 'user', text: 'Keep this open?' },
          { role: 'assistant', text: 'Yes.' },
        ],
        continueAfterMs: 1_000, earlierTurnsOmitted: false,
      });
      const paintsBeforeSuspend = textContainerUpgrade.mock.calls.length;

      hud.suspendDisplay();
      await vi.advanceTimersByTimeAsync(2_000);

      expect(hud.hasAssistantConversation()).toBe(false);
      expect(hud.assistantConversationIdentity()).toBeUndefined();
      expect(textContainerUpgrade).toHaveBeenCalledTimes(paintsBeforeSuspend);
      expect(client.suspendDisplay).toHaveBeenCalledOnce();
    } finally {
      vi.useRealTimers();
    }
  });
});
