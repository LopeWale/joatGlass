import { describe, expect, it, vi } from 'vitest';
import type { RebuildPageContainer, TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import { HudPageBuilder } from './HudPageBuilder';
import { HudPresenter, setNativeOverviewForTest } from './HudPresenter';
import { ContainerId, type AgentTerminalHudModel } from './types';
import type { HudVisualFrame } from './HudVisualSurface';

// These tests pin the IMAGE overview (VITE_NATIVE_OVERVIEW=0). The text
// overview, which is Home by default, has its own: HudPresenter.textOverview.test.ts.
setNativeOverviewForTest(false);

function terminal(overrides: Partial<AgentTerminalHudModel> = {}): AgentTerminalHudModel {
  return {
    state: 'ready',
    focus: 'Claude · ready',
    lanes: ['Claude · ready', 'Codex · idle', 'Antigravity · idle'],
    activity: 'Ready',
    conversationId: 'claude-conversation',
    messages: [],
    ...overrides,
  };
}

function longAnswer(): AgentTerminalHudModel {
  return terminal({
    state: 'settled',
    messages: [{
      id: 'answer-one',
      role: 'assistant',
      text: Array.from({ length: 32 }, (_, index) => `Claude result line ${index + 1}`).join('\n'),
    }],
  });
}

function rig() {
  const operations: string[] = [];
  const rebuilds: RebuildPageContainer[] = [];
  const upgrades: TextContainerUpgrade[] = [];
  const frames: HudVisualFrame[] = [];
  const rebuildPageContainer = vi.fn(async (page: RebuildPageContainer) => {
    rebuilds.push(page);
    operations.push(`rebuild:${page.listObject?.length ?? 0}:${page.textObject?.length ?? 0}:${page.imageObject?.length ?? 0}`);
    return true;
  });
  const textContainerUpgrade = vi.fn(async (upgrade: TextContainerUpgrade) => {
    upgrades.push(upgrade);
    operations.push(`upgrade:${upgrade.containerName ?? upgrade.containerID}`);
    return true;
  });
  const client = {
    require: () => ({ rebuildPageContainer, textContainerUpgrade }),
  } as unknown as BridgeClient;
  const visual = {
    paint: vi.fn(async (frame: HudVisualFrame) => {
      frames.push(frame);
      operations.push(`visual:${frame.kind}`);
      return true;
    }),
    paintExact: vi.fn(async (frame: HudVisualFrame, current: () => boolean) => {
      if (!current()) return false;
      frames.push(frame);
      operations.push(`visual:${frame.kind}`);
      return current();
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

function containerText(page: RebuildPageContainer | undefined, name: string): string {
  return page?.textObject?.find((container) => container.containerName === name)?.content ?? '';
}

function last<T>(values: readonly T[]): T | undefined {
  return values[values.length - 1];
}

describe('HudPresenter native Agent Terminal journey', () => {
  it('uses firmware list selection, the 5-container dual-brightness card, inline voice, truthful paging, and explicit Back transitions', async () => {
    const r = rig();
    expect(await r.hud.showText('9:41 JOAT listening bat 87%', ContainerId.StatusBar)).toBe(true);
    const framesBeforeList = r.frames.length;

    expect(await r.hud.showList(['Claude · 2 sessions', 'Codex · idle'], {
      title: 'AGENT TERMINAL',
      hint: 'tap: open · 2x: close',
      selectable: true,
      nativeFirmwareList: true,
    })).toBe(true);
    expect(r.rebuilds).toHaveLength(1);
    expect(r.rebuilds[0]?.listObject).toHaveLength(1);
    expect(r.rebuilds[0]?.imageObject ?? []).toHaveLength(0);
    expect(r.rebuilds[0]?.textObject?.find((c) => c.containerName === 'status')?.content)
      .toBe('AGENT TERMINAL · 9:41 JOAT listening bat 87%');

    expect(await r.hud.showTerminal(terminal())).toBe(true);
    expect(r.rebuilds).toHaveLength(2);
    expect(r.rebuilds[1]?.textObject?.map((item) => item.containerName)).toEqual([
      'terminal-head',
      'terminal-card',
      'terminal-dim',
      'terminal-bright',
      'terminal-foot',
    ]);
    expect(r.rebuilds[1]?.imageObject ?? []).toHaveLength(0);
    expect(r.frames).toHaveLength(framesBeforeList);
    expect(r.hud.scrollTranscript('up')).toBe(false);
    expect(r.hud.scrollTranscript('down')).toBe(false);

    expect(await r.hud.showPromptQuestion(
      'terminal_prompt', 'What should Claude inspect?', () => true, 'terminal',
    )).toBe(true);
    const brightUpgrades = () => r.upgrades.filter((u) => u.containerName === 'terminal-bright');
    const footUpgrades = () => r.upgrades.filter((u) => u.containerName === 'terminal-foot');
    expect(last(brightUpgrades())?.content).toContain('> Listening…');
    expect(last(brightUpgrades())?.content).not.toContain('QUESTION >');
    expect(last(footUpgrades())?.content).toBe('● Listening · tap: done · 2x: cancel');

    expect(await r.hud.showPromptPartial('terminal_prompt', 'Check tests and summarize.')).toBe(true);
    expect(last(brightUpgrades())?.content).toContain('> Check tests and summarize.');
    expect(last(brightUpgrades())?.content).not.toContain('YOU >');

    expect(await r.hud.showPromptTranscribing('terminal_prompt')).toBe(true);
    expect(last(footUpgrades())?.content).toBe('◐ Transcribing… · 2x: cancel');

    expect(await r.hud.dismissPromptQuestion('terminal_prompt')).toBe(true);
    expect(last(brightUpgrades())?.content).not.toContain('> Check tests and summarize.');
    expect(last(footUpgrades())?.content).toBe('tap: speak · 2x: sessions');

    expect(await r.hud.showTerminal(longAnswer())).toBe(true);
    await r.hud.whenNativeTerminalIdle();
    const writesBeforeScroll = r.upgrades.length;
    expect(r.hud.scrollTranscript('up')).toBe(true);
    await r.hud.whenNativeTerminalIdle();
    expect(r.upgrades.length).toBeGreaterThan(writesBeforeScroll);
    expect(last(footUpgrades())?.content).toContain('newer · tap: speak · swipe: scroll · 2x: sessions');

    const rebuildsBeforeBack = r.rebuilds.length;
    expect(await r.hud.showList(['+ New session', 'Auth refactor · settled'], {
      title: 'AGENT TERMINAL · CLAUDE',
      hint: 'tap: open · 2x: agents',
      selectable: true,
      nativeFirmwareList: true,
    })).toBe(true);
    expect(r.rebuilds).toHaveLength(rebuildsBeforeBack + 1);
    expect(last(r.rebuilds)?.listObject).toHaveLength(1);
    expect(last(r.rebuilds)?.imageObject ?? []).toHaveLength(0);

    const opsBeforeHome = r.operations.length;
    expect(await r.hud.showDefault()).toBe(true);
    expect(last(r.rebuilds)?.imageObject).toHaveLength(4);
    expect(last(r.frames)?.kind).toBe('overview');

    const activeTerminalOperations = r.operations.slice(1, opsBeforeHome);
    expect(activeTerminalOperations.some((entry) => entry.endsWith(':4'))).toBe(false);
  });

  it('restores the semantic reading anchor into a fresh WebView', async () => {
    const before = rig();
    expect(await before.hud.showTerminal(longAnswer())).toBe(true);
    expect(before.hud.scrollTranscript('up')).toBe(true);
    await before.hud.whenNativeTerminalIdle();
    const snapshot = JSON.parse(JSON.stringify(before.hud.exportBackgroundState())) as Record<string, unknown>;

    const after = rig();
    expect(after.hud.importBackgroundState(snapshot)).toBe(true);
    expect(await after.hud.repaintRestoredSurface()).toBe(true);
    const restoredPage = last(after.rebuilds);
    expect(containerText(restoredPage, 'terminal-foot')).toContain('newer');
    expect(containerText(restoredPage, 'terminal-bright')).not.toContain('Claude result line 32');
    expect(restoredPage?.imageObject ?? []).toHaveLength(0);
    expect(after.frames).toHaveLength(0);
  });

  it('returns from a native assistant answer to the retained 5-container terminal', async () => {
    const r = rig();
    expect(await r.hud.showTerminal(longAnswer())).toBe(true);
    expect(await r.hud.showAssistantConversation({
      conversationId: 'joat-answer',
      revision: 1,
      state: 'ready',
      turns: [
        { role: 'user', text: 'Summarize that result.' },
        { role: 'assistant', text: 'Here is the short version.' },
      ],
      continueAfterMs: 30_000,
      earlierTurnsOmitted: false,
    })).toBe(true);
    expect(await r.hud.requestAssistantConversationExit()).toBe('prompted');
    expect(await r.hud.requestAssistantConversationExit()).toBe('exited');
    expect(last(r.rebuilds)?.textObject?.map((item) => item.containerName)).toEqual([
      'terminal-head',
      'terminal-card',
      'terminal-dim',
      'terminal-bright',
      'terminal-foot',
    ]);
    expect(last(r.rebuilds)?.imageObject ?? []).toHaveLength(0);
    expect(containerText(last(r.rebuilds), 'terminal-bright')).toContain('Claude result line 32');
  });

  it('clears any lingering assistantOverlay when showTerminal paints over it', async () => {
    const r = rig();
    expect(await r.hud.showTerminal(longAnswer())).toBe(true);
    expect(await r.hud.showAssistantOverlay('Recorded: approve.', false, true)).toBe(true);
    expect(r.hud.hasAssistantOverlay()).toBe(true);
    expect(await r.hud.showTerminal(longAnswer())).toBe(true);
    expect(r.hud.hasAssistantOverlay()).toBe(false);
  });
});
