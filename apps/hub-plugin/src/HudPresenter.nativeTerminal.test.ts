import { describe, expect, it, vi } from 'vitest';
import type { RebuildPageContainer, TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import { HudPageBuilder } from './HudPageBuilder';
import { HudPresenter } from './HudPresenter';
import { ContainerId, type BrainstormHudModel } from './types';
import type { HudVisualFrame } from './HudVisualSurface';

function terminal(overrides: Partial<BrainstormHudModel> = {}): BrainstormHudModel {
  return {
    screen: 'agent_terminal',
    state: 'ready',
    topic: 'OpenClaw · ready',
    collaborators: ['OpenClaw', 'Hermes'],
    activity: 'Ready',
    conversationId: 'openclaw-conversation',
    messages: [],
    noToolsChecked: true,
    ...overrides,
  };
}

function longAnswer(): BrainstormHudModel {
  return terminal({
    state: 'settled',
    messages: [{
      id: 'answer-one',
      role: 'assistant',
      text: Array.from({ length: 32 }, (_, index) => `OpenClaw result line ${index + 1}`).join('\n'),
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

function body(page: RebuildPageContainer | undefined): string {
  return page?.textObject?.find((container) => container.containerName === 'terminal-body')?.content ?? '';
}

function last<T>(values: readonly T[]): T | undefined {
  return values[values.length - 1];
}

describe('HudPresenter native Agent Terminal journey', () => {
  it('uses firmware list selection, one native terminal body, truthful paging, and explicit Back transitions', async () => {
    const r = rig();
    expect(await r.hud.showText('9:41 JOAT listening bat 87%', ContainerId.StatusBar)).toBe(true);
    const framesBeforeList = r.frames.length; // the status paint, before any native page

    expect(await r.hud.showList(['OpenClaw', 'Hermes'], {
      title: 'AGENT TERMINAL',
      hint: 'double-click: back',
      selectable: true,
      nativeFirmwareList: true,
    })).toBe(true);
    expect(r.rebuilds).toHaveLength(1);
    expect(r.rebuilds[0]?.listObject).toHaveLength(1);
    expect(r.rebuilds[0]?.imageObject ?? []).toHaveLength(0);
    // The firmware owns this page, so the live top line travels in the
    // rebuild; a blank status container would hide the clock and battery.
    // It is also the page's only title line.
    expect(r.rebuilds[0]?.textObject?.find((c) => c.containerName === 'status')?.content)
      .toBe('AGENT TERMINAL · 9:41 JOAT listening bat 87%');
    expect(r.hud.usesCustomListSelection()).toBe(false);

    expect(await r.hud.showBrainstorm(terminal())).toBe(true);
    expect(r.rebuilds).toHaveLength(2);
    expect(r.rebuilds[1]?.textObject?.map((item) => item.containerName)).toEqual([
      'terminal-head', 'terminal-body',
    ]);
    expect(r.rebuilds[1]?.imageObject ?? []).toHaveLength(0);
    expect(r.frames).toHaveLength(framesBeforeList);
    expect(r.hud.scrollTranscript('up')).toBe(false);
    expect(r.hud.scrollTranscript('down')).toBe(false);

    expect(await r.hud.showPromptQuestion(
      'terminal_prompt', 'What should OpenClaw inspect?', () => true, 'terminal',
    )).toBe(true);
    expect(last(r.upgrades)?.containerName).toBe('terminal-body');
    expect(last(r.upgrades)?.content).toContain('QUESTION > What should OpenClaw inspect?');
    expect(await r.hud.dismissPromptQuestion('terminal_prompt')).toBe(true);
    expect(last(r.upgrades)?.content).not.toContain('QUESTION >');

    expect(await r.hud.showBrainstorm(longAnswer())).toBe(true);
    await r.hud.whenNativeTerminalIdle();
    const writesBeforeScroll = r.upgrades.length;
    expect(r.hud.scrollTranscript('up')).toBe(true);
    await r.hud.whenNativeTerminalIdle();
    expect(r.upgrades).toHaveLength(writesBeforeScroll + 1);
    expect(last(r.upgrades)?.containerName).toBe('terminal-body');
    expect(last(last(r.upgrades)?.content?.split('\n') ?? [])).toContain('down: newer');

    const rebuildsBeforeBack = r.rebuilds.length;
    expect(await r.hud.showList(['OpenClaw', 'Hermes'], {
      title: 'AGENT TERMINAL', hint: 'double-click: back', selectable: true,
      nativeFirmwareList: true,
    })).toBe(true);
    expect(r.rebuilds).toHaveLength(rebuildsBeforeBack + 1);
    expect(last(r.rebuilds)?.listObject).toHaveLength(1);
    expect(last(r.rebuilds)?.imageObject ?? []).toHaveLength(0);

    expect(await r.hud.showDefault()).toBe(true);
    expect(last(r.rebuilds)?.imageObject).toHaveLength(4);
    expect(last(r.frames)?.kind).toBe('overview');

    const activeTerminalOperations = r.operations.slice(1, rebuildsBeforeBack + writesBeforeScroll + 4);
    expect(activeTerminalOperations.some((entry) => entry.endsWith(':4'))).toBe(false);
  });

  it('restores the semantic reading anchor into a fresh WebView', async () => {
    const before = rig();
    expect(await before.hud.showBrainstorm(longAnswer())).toBe(true);
    expect(before.hud.scrollTranscript('up')).toBe(true);
    await before.hud.whenNativeTerminalIdle();
    const snapshot = JSON.parse(JSON.stringify(before.hud.exportBackgroundState())) as Record<string, unknown>;

    const after = rig();
    expect(after.hud.importBackgroundState(snapshot)).toBe(true);
    expect(await after.hud.repaintRestoredSurface()).toBe(true);
    const restored = body(last(after.rebuilds));
    expect(restored).toContain('down: newer');
    expect(restored).not.toContain('OpenClaw result line 32');
    expect(last(after.rebuilds)?.imageObject ?? []).toHaveLength(0);
    expect(after.frames).toHaveLength(0);
  });

  it('returns from a native assistant answer to the retained native terminal', async () => {
    const r = rig();
    expect(await r.hud.showBrainstorm(longAnswer())).toBe(true);
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
      'terminal-head', 'terminal-body',
    ]);
    expect(last(r.rebuilds)?.imageObject ?? []).toHaveLength(0);
    expect(body(last(r.rebuilds))).toContain('OPENCLAW > COMPLETE');
  });
});


describe('HudPresenter Brainstorm on the native page', () => {
  const session: BrainstormHudModel = {
    screen: 'brainstorm', state: 'running', topic: 'Should the glasses cache the picker?',
    collaborators: ['Codex', 'Claude'], activeSpeaker: 'Codex · round 1/3 · 2 calls',
    activity: 'YOU > Should the glasses cache the picker?\nCODEX > A cache saves a round trip.',
  };

  it('shows a Brainstorm as the two-text page under the transition barrier, with no image tiles', async () => {
    const r = rig();
    await r.hud.showText('12:34 JOAT listening bat 80%', ContainerId.StatusBar);
    await r.hud.showText('home card', ContainerId.Main);
    const framesBefore = r.frames.length;

    expect(await r.hud.showBrainstorm(session)).toBe(true);

    expect(r.operations.filter((op) => op === 'rebuild:0:2:0')).toHaveLength(1);
    expect(r.frames).toHaveLength(framesBefore);
    const page = last(r.rebuilds);
    // The head line is the page's status header: name, then the live line.
    expect(page?.textObject?.find((c) => c.containerName === 'terminal-head')?.content).toBe('BRAINSTORM · 12:34 JOAT listening bat 80%');
    expect(body(page)).toContain('CODEX > A cache saves a round trip.');
    expect(body(page).split('\n')[0]).toContain('WORKING · Codex · round 1/3 · 2 calls');

    // A streamed revision is one body upgrade, never another rebuild.
    const rebuilds = r.rebuilds.length;
    expect(await r.hud.showBrainstorm({ ...session, activity: `${session.activity} It also risks stale rows.` })).toBe(true);
    expect(r.rebuilds).toHaveLength(rebuilds);
    expect(last(r.upgrades)?.content).toContain('stale rows');
    expect(r.frames).toHaveLength(framesBefore);
  });

  it('pages the transcript natively on swipe and returns to the page after a question', async () => {
    const r = rig();
    const long = { ...session, activity: Array.from({ length: 30 }, (_, i) => `CODEX > point ${i + 1}`).join('\n') };
    expect(await r.hud.showBrainstorm(long)).toBe(true);
    const rebuilds = r.rebuilds.length;
    expect(r.hud.scrollTranscript('up')).toBe(true);
    await vi.waitFor(() => expect(last(r.upgrades)?.content).toContain('newer · swipe down · double-click: cancel'));
    expect(r.rebuilds).toHaveLength(rebuilds);

    expect(await r.hud.showPromptQuestion('q-1', 'What would you like to brainstorm?', () => true, 'brainstorm')).toBe(true);
    expect(last(r.rebuilds)?.textObject?.map((c) => c.containerName)).toEqual(['status', 'main', 'hint']);
    const before = r.operations.length;
    expect(await r.hud.dismissPromptQuestion('q-1')).toBe(true);
    const since = r.operations.slice(before);
    expect(since.filter((op) => op === 'rebuild:0:2:0')).toHaveLength(1);
    expect(since.filter((op) => op.startsWith('rebuild:0:1:4') || op.startsWith('visual:'))).toHaveLength(0);
  });

});
