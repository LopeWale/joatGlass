import { describe, expect, it, vi } from 'vitest';
import type { RebuildPageContainer, TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import { HudPageBuilder } from './HudPageBuilder';
import { HudPresenter } from './HudPresenter';
import { ContainerId, type AssistantConversationSnapshot } from './types';
import type { HudVisualFrame } from './HudVisualSurface';

function conversation(
  revision: number,
  state: AssistantConversationSnapshot['state'],
  turns: AssistantConversationSnapshot['turns'],
): AssistantConversationSnapshot {
  return {
    conversationId: 'voice-turns-1', revision, state, turns,
    continueAfterMs: 30_000, earlierTurnsOmitted: false,
  };
}

function rig() {
  const rebuilds: RebuildPageContainer[] = [];
  const upgrades: TextContainerUpgrade[] = [];
  const rebuildPageContainer = vi.fn(async (page: RebuildPageContainer) => {
    rebuilds.push(page);
    return true;
  });
  const textContainerUpgrade = vi.fn(async (update: TextContainerUpgrade) => {
    upgrades.push(update);
    return true;
  });
  const client = {
    require: () => ({ rebuildPageContainer, textContainerUpgrade }),
  } as unknown as BridgeClient;
  const frames: HudVisualFrame[] = [];
  const visual = {
    paint: vi.fn(async (frame: HudVisualFrame) => { frames.push(frame); return true; }),
    forgetPage: vi.fn(),
    preparePageTransition: vi.fn(async () => {}),
  };
  return {
    hud: new HudPresenter(client, new HudPageBuilder(), visual),
    rebuilds, upgrades, rebuildPageContainer, textContainerUpgrade, frames, visual,
  };
}

describe('HudPresenter retained assistant conversation integration', () => {
  it('composites the real prompt and answer into raster frames and blocks unrelated repaints', async () => {
    const { hud, rebuilds, upgrades, frames } = rig();
    await hud.showText('NEXT\nDesign review', ContainerId.Main);
    const visualFramesBeforeConversation = frames.length;

    expect(await hud.showAssistantConversation(conversation(1, 'thinking', [
      { role: 'user', text: 'What is the capital of France?' },
    ]))).toBe(true);
    expect(await hud.showAssistantConversation(conversation(2, 'ready', [
      { role: 'user', text: 'What is the capital of France?' },
      { role: 'assistant', text: 'Paris.' },
    ]))).toBe(true);
    await hud.whenAssistantConversationIdle();
    await hud.setStatus('JOAT · listening');

    const conversationFrames = frames.slice(visualFramesBeforeConversation);
    expect(conversationFrames[0]?.assistantConversation?.visibleLines)
      .toContainEqual({ role: 'user', text: 'What is the capital of France?' });
    expect(conversationFrames[conversationFrames.length - 1]?.assistantConversation?.visibleLines)
      .toContainEqual({ role: 'assistant', text: 'Paris.' });
    expect(rebuilds).toHaveLength(0);
    expect(upgrades).toHaveLength(0);
    // The status mutation is retained underneath, but does not overwrite the
    // conversation with an unrelated frame while the conversation owns it.
    expect(conversationFrames).toHaveLength(2);
    expect(hud.hasAssistantConversation()).toBe(true);
  });

  it('restores the exact underlying visual page only after the continuation question is declined', async () => {
    const { hud, rebuilds, frames, visual } = rig();
    await hud.showText('Retained dashboard card', ContainerId.Main);
    await hud.showAssistantConversation(conversation(1, 'ready', [
      { role: 'user', text: 'Tell me a joke.' },
      { role: 'assistant', text: 'A byte walked into a bar.' },
    ]));

    await expect(hud.requestAssistantConversationExit()).resolves.toBe('prompted');
    expect(hud.isAssistantConversationContinuePromptVisible()).toBe(true);
    await expect(hud.continueAssistantConversation()).resolves.toBe(true);
    await expect(hud.requestAssistantConversationExit()).resolves.toBe('prompted');
    await expect(hud.requestAssistantConversationExit()).resolves.toBe('exited');

    expect(rebuilds).toHaveLength(0);
    expect(visual.forgetPage).not.toHaveBeenCalled();
    expect(frames[frames.length - 1]).toMatchObject({
      kind: 'overview', main: 'Retained dashboard card',
    });
    expect(hud.hasAssistantConversation()).toBe(false);
  });

  it('restores the newest underlay state instead of the stale entry snapshot', async () => {
    const { hud, frames } = rig();
    await hud.showText('Old dashboard card', ContainerId.Main);
    await hud.showAssistantConversation(conversation(1, 'ready', [
      { role: 'user', text: 'What changed?' },
      { role: 'assistant', text: 'I am checking.' },
    ]));

    await hud.showText('Newest dashboard card', ContainerId.Main);
    await expect(hud.requestAssistantConversationExit()).resolves.toBe('prompted');
    await expect(hud.requestAssistantConversationExit()).resolves.toBe('exited');

    expect(frames[frames.length - 1]).toMatchObject({
      kind: 'overview', main: 'Newest dashboard card',
    });
  });

  it('makes a transactional receipt visible by completing a page handoff first', async () => {
    const { hud, frames } = rig();
    await hud.showAssistantConversation(conversation(1, 'ready', [
      { role: 'user', text: 'Save this note.' },
      { role: 'assistant', text: 'I can do that.' },
    ]));

    expect(await hud.showAssistantOverlay('Saved QuickNote.', false, true, 'receipt')).toBe(true);

    expect(hud.hasAssistantConversation()).toBe(false);
    expect(hud.hasAssistantOverlay()).toBe(true);
    expect(frames[frames.length - 1]).toMatchObject({
      assistantOverlay: { text: 'Saved QuickNote.', presentation: 'receipt' },
    });
  });

  it('rolls a failed feature handoff back to the conversation and can resume the same id', async () => {
    const { hud, visual } = rig();
    await hud.showAssistantConversation(conversation(1, 'thinking', [
      { role: 'user', text: 'Open News.' },
    ]));
    visual.paint.mockResolvedValueOnce(false);

    await expect(hud.showList(['Headline'], { title: 'NEWS' })).resolves.toBe(false);
    expect(hud.hasAssistantConversation()).toBe(true);

    await expect(hud.showAssistantConversation(conversation(2, 'ready', [
      { role: 'user', text: 'Open News.' },
      { role: 'assistant', text: 'News is unavailable.' },
    ]))).resolves.toBe(true);
    expect(hud.assistantConversationIdentity()).toEqual({
      conversationId: 'voice-turns-1', revision: 2,
    });
  });
});
