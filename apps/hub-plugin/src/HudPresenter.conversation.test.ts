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
    whenIdle: vi.fn(async () => true),
    withPageTransition: vi.fn(async (replace: () => Promise<boolean>) => replace()),
  };
  return {
    hud: new HudPresenter(client, new HudPageBuilder(), visual),
    rebuilds, upgrades, rebuildPageContainer, textContainerUpgrade, frames, visual,
  };
}

describe('HudPresenter retained assistant conversation integration', () => {
  it('renders prompt and answer through native text while blocking unrelated raster repaints', async () => {
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
    expect(rebuilds).toHaveLength(1);
    expect(rebuilds[0]?.imageObject ?? []).toHaveLength(0);
    expect(rebuilds[0]?.textObject?.find((item) => item.containerName === 'wearer-turns')?.content)
      .toContain('What is the capital of France?');
    expect(upgrades.find((item) => item.containerName === 'joat-turns')?.content)
      .toContain('Paris.');
    // The status mutation is retained underneath, but does not overwrite the
    // conversation with an unrelated frame while the conversation owns it.
    expect(conversationFrames).toHaveLength(0);
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

    expect(rebuilds[0]?.imageObject ?? []).toHaveLength(0);
    expect(rebuilds[rebuilds.length - 1]?.imageObject).toHaveLength(4);
    expect(visual.forgetPage).toHaveBeenCalled();
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
    const { hud, rebuilds } = rig();
    await hud.showAssistantConversation(conversation(1, 'ready', [
      { role: 'user', text: 'Save this note.' },
      { role: 'assistant', text: 'I can do that.' },
    ]));

    expect(await hud.showAssistantOverlay('Saved QuickNote.', false, true, 'receipt')).toBe(true);

    expect(hud.hasAssistantConversation()).toBe(false);
    expect(hud.hasAssistantOverlay()).toBe(true);
    // The receipt is text, so it lands on a firmware text container rather
    // than in a raster frame. Asserting the container proves the handoff
    // completed AND that the words reached the glass — the visual frame
    // only ever proved the canvas had been asked to draw them.
    const page = rebuilds[rebuilds.length - 1];
    const body = page?.textObject?.find((c) => c.containerName === 'main');
    expect(body?.content).toContain('Saved QuickNote.');
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
