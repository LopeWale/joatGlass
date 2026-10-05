import { describe, expect, it, vi } from 'vitest';
import type { RebuildPageContainer, TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import { HudPageBuilder } from './HudPageBuilder';
import { HudPresenter } from './HudPresenter';
import type { AssistantConversationSnapshot } from './types';
import type { HudVisualFrame } from './HudVisualSurface';

function snapshot(revision: number, answer?: string): AssistantConversationSnapshot {
  return {
    conversationId: 'native-conversation-1',
    revision,
    state: answer ? 'ready' : 'thinking',
    turns: [
      { role: 'user', text: 'What is the capital of France?' },
      ...(answer ? [{ role: 'assistant' as const, text: answer }] : []),
    ],
    continueAfterMs: 30_000,
    earlierTurnsOmitted: false,
  };
}

function rig() {
  const operations: string[] = [];
  const rebuilds: RebuildPageContainer[] = [];
  const upgrades: TextContainerUpgrade[] = [];
  const rebuildPageContainer = vi.fn(async (page: RebuildPageContainer) => {
    operations.push(`rebuild:${page.imageObject?.length ?? 0}:${page.textObject?.length ?? 0}`);
    rebuilds.push(page);
    return true;
  });
  const textContainerUpgrade = vi.fn(async (update: TextContainerUpgrade) => {
    operations.push(`upgrade:${update.containerName ?? update.containerID}`);
    upgrades.push(update);
    return true;
  });
  const client = {
    require: () => ({ rebuildPageContainer, textContainerUpgrade }),
  } as unknown as BridgeClient;
  const frames: HudVisualFrame[] = [];
  const visual = {
    paint: vi.fn(async (frame: HudVisualFrame) => {
      operations.push(`visual:${frame.kind}`);
      frames.push(frame);
      return true;
    }),
    forgetPage: vi.fn(() => operations.push('forget')),
    whenIdle: vi.fn(async () => true),
    withPageTransition: vi.fn(async (replace: () => Promise<boolean>) => replace()),
  };
  return {
    hud: new HudPresenter(client, new HudPageBuilder(), visual),
    operations, rebuilds, upgrades, frames, visual,
  };
}

describe('HudPresenter native assistant conversation', () => {
  it('uses a native text page and in-place text upgrades even when the rest of JOAT is visual', async () => {
    const { hud, rebuilds, upgrades, frames } = rig();

    expect(await hud.showAssistantConversation(snapshot(1))).toBe(true);
    expect(rebuilds).toHaveLength(1);
    expect(rebuilds[0]?.imageObject ?? []).toHaveLength(0);
    expect(rebuilds[0]?.textObject?.map((item) => item.containerName)).toEqual([
      'status', 'conversation', 'wearer-turns', 'joat-turns', 'scroll-position', 'generation',
    ]);
    expect(frames).toHaveLength(0);

    expect(await hud.showAssistantConversation(snapshot(2, 'Paris.'))).toBe(true);
    await hud.whenAssistantConversationIdle();

    expect(rebuilds).toHaveLength(1);
    expect(upgrades.some((item) => (
      item.containerName === 'joat-turns' && item.content?.includes('Paris.')
    ))).toBe(true);
    expect(frames).toHaveLength(0);

    expect(await hud.showAssistantConversation(snapshot(3, 'Paris, France.'))).toBe(true);
    await hud.whenAssistantConversationIdle();
    expect(rebuilds).toHaveLength(1);
    expect(upgrades.some((item) => (
      item.containerName === 'joat-turns' && item.content?.includes('Paris, France.')
    ))).toBe(true);
    expect(frames).toHaveLength(0);
  });

  it('rebuilds and completely repaints the four-tile page only when leaving native conversation', async () => {
    const { hud, operations, rebuilds, frames, visual } = rig();
    expect(await hud.showAssistantConversation(snapshot(1, 'Paris.'))).toBe(true);

    expect(await hud.requestAssistantConversationExit()).toBe('prompted');
    expect(await hud.requestAssistantConversationExit()).toBe('exited');

    expect(rebuilds[rebuilds.length - 1]?.imageObject).toHaveLength(4);
    expect(visual.withPageTransition).toHaveBeenCalledTimes(2);
    expect(visual.forgetPage).toHaveBeenCalledTimes(2);
    expect(frames[frames.length - 1]?.kind).toBe('overview');
    expect(operations.slice(-3)).toEqual(['rebuild:4:1', 'forget', 'visual:overview']);
  });
});
