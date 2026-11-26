import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { RebuildPageContainer, TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import { CapabilityMenu } from './CapabilityMenu';
import { HudPageBuilder } from './HudPageBuilder';
import { HudPresenter } from './HudPresenter';
import type { HudStatusModel } from './HudStatus';
import type { HudVisualFrame } from './HudVisualSurface';
import type { MacLink } from './MacLink';
import { navigationVisualFixture, type NavigationHudModel } from './NavigationHud';
import { Settings } from './Settings';
import type { AgentTerminalHudModel, AssistantConversationSnapshot } from './types';

/**
 * Even's own features hold the lens one at a time. JOAT's do not: a route
 * keeps running behind a conversation or a feature page, and every page's
 * header carries what else is running. Until 2026-10-05 each navigation
 * frame (once a second while walking) went through `showNavigation`, which
 * handed off whatever was on the glass, so a conversation or a Pomodoro
 * page lasted until the wearer's next step.
 */

const last = <T,>(values: readonly T[]): T | undefined => values[values.length - 1];

const LIVE: HudStatusModel = { clock: '9:41', link: 'connected', mic: true, battery: 80, charging: false };

function rig() {
  const rebuilds: RebuildPageContainer[] = [];
  const upgrades: TextContainerUpgrade[] = [];
  const client = {
    require: () => ({
      rebuildPageContainer: vi.fn(async (page: RebuildPageContainer) => { rebuilds.push(page); return true; }),
      textContainerUpgrade: vi.fn(async (update: TextContainerUpgrade) => { upgrades.push(update); return true; }),
    }),
  } as unknown as BridgeClient;
  const frames: HudVisualFrame[] = [];
  const visual = {
    paint: vi.fn(async (frame: HudVisualFrame) => { frames.push(frame); return true; }),
    forgetPage: vi.fn(),
    whenIdle: vi.fn(async () => true),
    withPageTransition: vi.fn(async (replace: () => Promise<boolean>) => replace()),
  };
  const hud = new HudPresenter(client, new HudPageBuilder(), visual);
  const statusWrites = () => upgrades.filter((update) => update.containerName === 'status').map((u) => u.content ?? '');
  const headerOf = (page: RebuildPageContainer | undefined) => (
    page?.textObject?.find((container) => container.containerName === 'status')?.content ?? ''
  );
  return { hud, rebuilds, upgrades, frames, statusWrites, headerOf };
}

function guidance(maneuverDistanceM: number, maneuver: NavigationHudModel['maneuver'] = 'turn_right'): NavigationHudModel {
  return { ...navigationVisualFixture('nav-guidance'), maneuver, maneuverDistanceM };
}

function conversation(revision = 1, conversationId = 'walk-and-talk'): AssistantConversationSnapshot {
  return {
    conversationId,
    revision,
    state: 'ready',
    turns: [
      { role: 'user', text: 'Remind me what Evan said about the gate.' },
      { role: 'assistant', text: 'Park on the left; the code is the same as last time.' },
    ],
    continueAfterMs: 30_000,
    earlierTurnsOmitted: false,
  };
}

const FOCUS_ENDS_IN_MS = 12 * 60_000 - 30_000;

async function startPomodoro(hud: HudPresenter) {
  await hud.acceptPanel({
    kind: 'hud.panel', tab: 'pomodoro', revision: 1, rows: ['Pause', 'Reset'],
    pomodoro: {
      phase: 'focus', running: true, remainingMs: FOCUS_ENDS_IN_MS, lengthMs: 1_500_000,
      completed: 1, endsAtUnixMs: Date.now() + FOCUS_ENDS_IN_MS,
    },
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-05T18:00:00Z'));
});
afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); });

describe('a route keeps running behind a conversation', () => {
  it('never takes the glass, carries the next turn in the header, and returns with the newest frame', async () => {
    const r = rig();
    await r.hud.setStatusModel(LIVE);
    await startPomodoro(r.hud);
    expect(await r.hud.showNavigation(guidance(240))).toBe(true);
    expect(await r.hud.showAssistantConversation(conversation())).toBe(true);
    const conversationPage = r.rebuilds[r.rebuilds.length - 1];
    // The conversation page has the header line now, with both features.
    expect(r.headerOf(conversationPage)).toBe('9:41 · JOAT · Focus 12m · → 250 m · ● · ▆');
    expect(r.hud.isNavigationVisible()).toBe(false);

    const rebuildsBefore = r.rebuilds.length;
    const framesBefore = r.frames.length;
    // Three seconds of walking: each frame is delivered, none takes the glass.
    for (const metres of [200, 140, 80]) expect(await r.hud.updateNavigation(guidance(metres))).toBe(true);
    await r.hud.whenAssistantConversationIdle();
    expect(r.rebuilds).toHaveLength(rebuildsBefore);
    expect(r.frames).toHaveLength(framesBefore);
    expect(r.hud.hasAssistantConversation()).toBe(true);
    expect(last(r.statusWrites())).toBe('9:41 · JOAT · Focus 12m · → 80 m · ● · ▆');

    // Leaving the conversation shows the route at the newest frame, not the
    // one that was on the glass when the conversation began.
    expect(await r.hud.requestAssistantConversationExit()).toBe('prompted');
    expect(await r.hud.requestAssistantConversationExit()).toBe('exited');
    const navPage = last(r.rebuilds);
    const navBody = navPage?.textObject?.find((container) => container.containerName === 'main')?.content ?? '';
    expect(navBody).toContain('→ 80 m');
    // On the route page the turn is in main; the spine carries the timer.
    expect(r.headerOf(navPage)).toBe('9:41 · NAVIGATION · Focus 12m · ● · ▆');
    expect(r.hud.isNavigationVisible()).toBe(true);
  });
});

describe('a route keeps running behind a feature page', () => {
  it('updates the list header in place and never repaints the map over it', async () => {
    const r = rig();
    await r.hud.setStatusModel(LIVE);
    expect(await r.hud.showNavigation(guidance(500))).toBe(true);
    expect(await r.hud.showList(['Rates hold · BBC', 'Rover lands · NPR'], {
      title: 'NEWS', railTab: 'news', selectable: true, nativeFirmwareList: true,
    })).toBe(true);
    expect(r.headerOf(r.rebuilds[r.rebuilds.length - 1])).toBe('9:41 · NEWS · → 500 m · ● · ▆');

    const rebuildsBefore = r.rebuilds.length;
    const framesBefore = r.frames.length;
    expect(await r.hud.updateNavigation(guidance(450))).toBe(true);
    expect(r.statusWrites()).toEqual(['9:41 · NEWS · → 450 m · ● · ▆']);
    // Same rounded distance: nothing to write.
    expect(await r.hud.updateNavigation(guidance(452))).toBe(true);
    expect(r.statusWrites()).toHaveLength(1);
    expect(r.rebuilds).toHaveLength(rebuildsBefore);
    expect(r.frames).toHaveLength(framesBefore);

    r.hud.navigationEnded();
    expect(last(r.statusWrites())).toBe('9:41 · NEWS · ● · ▆');
  });

  it('still owns Home: from the dashboard a frame repaints the route page', async () => {
    const r = rig();
    await r.hud.setStatusModel(LIVE);
    await r.hud.showDefault();
    expect(await r.hud.updateNavigation(guidance(120))).toBe(true);
    const navPage = last(r.rebuilds);
    const navBody = navPage?.textObject?.find((container) => container.containerName === 'main')?.content ?? '';
    expect(navBody).toContain('→ 100 m');
    expect(r.headerOf(navPage)).toBe('9:41 · NAVIGATION · ● · ▆');
  });
});

describe('the Pomodoro rides along on every page', () => {
  it('shows on other pages, hides on its own, and moves on the minute in place', async () => {
    const r = rig();
    await r.hud.setStatusModel(LIVE);
    await startPomodoro(r.hud);
    await r.hud.showList(['Pause', 'Reset'], {
      title: 'POMODORO', railTab: 'pomodoro', selectable: true, nativeFirmwareList: true,
    });
    expect(r.headerOf(r.rebuilds[r.rebuilds.length - 1])).toBe('9:41 · POMODORO · ● · ▆');

    await r.hud.showList(['Evan · On my way'], { title: 'MESSAGES', selectable: true, nativeFirmwareList: true });
    expect(r.headerOf(r.rebuilds[r.rebuilds.length - 1])).toBe('9:41 · MESSAGES · Focus 12m · ● · ▆');

    // 11:30 left: at the minute boundary the chip reads 11, written in place.
    await vi.advanceTimersByTimeAsync(31_000);
    expect(last(r.statusWrites())).toBe('9:41 · MESSAGES · Focus 11m · ● · ▆');
  });
});

function terminalModel(summary: string, state: AgentTerminalHudModel['state'] = 'ready'): AgentTerminalHudModel {
  return {
    openingRequestId: 'voice-term-1',
    openingSelectionId: 'voice-term-1',
    state,
    focus: 'Claude Code',
    lanes: ['Claude Code'],
    activity: summary,
    terminalReason: '',
    approvals: 0,
    conversationId: 'claude-session',
    messages: [
      { id: 'm1', role: 'user', text: 'Run unit suite' },
      { id: 'm2', role: 'assistant', text: summary },
    ],
    historyTruncated: false,
    inputRequired: false,
    taskRecovery: false,
  };
}

describe('JOAT modal floats and persists over every active feature', () => {
  it('mounts over active firmware features in a single rebuild (zero blank 4-tile visual flash)', async () => {
    const r = rig();
    await r.hud.setStatusModel(LIVE);
    expect(await r.hud.showNavigation(guidance(240))).toBe(true);

    const before = r.rebuilds.length;
    expect(await r.hud.showAssistantConversation(conversation(1))).toBe(true);
    // Must mount directly from navigation-native -> assistant-native in ONE rebuild,
    // never flashing an intermediate 4-image visualLayout rebuild in between.
    expect(r.rebuilds.length - before).toBe(1);
    const overlay = last(r.rebuilds);
    expect(overlay?.textObject?.some((t) => t.containerName === 'conversation')).toBe(true);
    expect(overlay?.textObject?.some((t) => t.containerName === 'main')).toBe(true);
  });

  it('floats over Agent Terminal with visible terminal context above the modal and survives background terminal polls', async () => {
    const r = rig();
    const link = { send: vi.fn(), sendTerminal: vi.fn(() => true) } as unknown as MacLink;
    const menu = new CapabilityMenu(r.hud, new Settings(), link);
    await r.hud.setStatusModel(LIVE);

    // Voice opens Agent Terminal session:
    expect(await menu.presentList('AGENT TERMINAL', ['Connecting…'], 'voice-term-1', false)).toBe(true);
    expect(await menu.presentTerminalOpening(
      terminalModel('Initial terminal output'),
      { requestId: 'voice-term-1', selectionId: 'voice-term-1' },
      () => true,
    )).toBe(true);

    const beforeModal = r.rebuilds.length;
    expect(await r.hud.showAssistantConversation(conversation(1))).toBe(true);
    expect(r.rebuilds.length - beforeModal).toBe(1);
    expect(r.hud.hasAssistantConversation()).toBe(true);
    const modalPage = last(r.rebuilds);
    expect(modalPage?.textObject?.some((t) => t.containerName === 'main')).toBe(true);

    // Background 1.5s terminal poll arrives while JOAT modal is up: modal MUST stay visible!
    expect(await menu.presentTerminalOpening(
      terminalModel('Updated terminal output from poll', 'running'),
      { requestId: 'voice-term-1', selectionId: 'voice-term-1' },
      () => true,
    )).toBe(true);
    expect(r.hud.hasAssistantConversation()).toBe(true);

    // Dismissing JOAT's modal restores the updated terminal session!
    expect(await r.hud.dismissAssistantConversation('walk-and-talk', 1)).toBe(true);
    expect(r.hud.hasAssistantConversation()).toBe(false);
    const restored = last(r.rebuilds);
    expect(restored?.textObject?.some((t) => t.containerName === 'terminal-bright'
      && (t.content ?? '').includes('Updated terminal output from poll'))).toBe(true);
  });

  it('survives background Mail/Messages refreshes and Pomodoro ticks without tearing down JOAT modal', async () => {
    const r = rig();
    const link = { send: vi.fn() } as unknown as MacLink;
    const menu = new CapabilityMenu(r.hud, new Settings(), link);
    await r.hud.setStatusModel(LIVE);

    // Open Mail via voice, then invoke JOAT modal:
    expect(await menu.presentList('MAIL', ['[2m] Old email'], 'voice-mail-1', true)).toBe(true);
    expect(await r.hud.showAssistantConversation(conversation(1))).toBe(true);
    expect(r.hud.hasAssistantConversation()).toBe(true);

    // Background IMAP sync pushes refreshed rows for the SAME requestId:
    expect(await menu.presentList('MAIL', ['[now] Brand new email', '[2m] Old email'], 'voice-mail-1', true)).toBe(true);
    expect(r.hud.hasAssistantConversation()).toBe(true);

    // Dismissing JOAT modal reveals the refreshed Mail list!
    expect(await r.hud.dismissAssistantConversation('walk-and-talk', 1)).toBe(true);
    const restoredList = last(r.rebuilds)?.listObject?.[0];
    expect(restoredList?.itemContainer?.itemName?.[0]).toContain('Brand new email');

    // Open Pomodoro tab, invoke JOAT modal, then push a background Pomodoro tick:
    await startPomodoro(r.hud);
    await menu.openPanelTab('pomodoro', 0, false);
    expect(await r.hud.showAssistantConversation(conversation(1, 'walk-and-talk-2'))).toBe(true);
    expect(r.hud.hasAssistantConversation()).toBe(true);
    expect(await menu.presentPanelContent('pomodoro')).toBe(true);
    expect(r.hud.hasAssistantConversation()).toBe(true);
  });

  it('floats over MESSAGES list and MESSAGES · CONVERSATION reader, survives live message refreshes, and hands off to reply confirmation', async () => {
    const r = rig();
    const link = { send: vi.fn() } as unknown as MacLink;
    const menu = new CapabilityMenu(r.hud, new Settings(), link);
    await r.hud.setStatusModel(LIVE);

    // 1. Open MESSAGES thread list, invoke JOAT modal ("summarize my messages"):
    expect(await menu.presentList(
      'MESSAGES',
      ['New message · voice recipient + text', '[2m] Alex Rivera • · Does 12:30 work for lunch?'],
      'voice-msg-1',
      true,
      'tap: select · left x2: back',
    )).toBe(true);
    expect(await r.hud.showAssistantConversation(conversation(1, 'msg-summary-1'))).toBe(true);
    expect(r.hud.hasAssistantConversation()).toBe(true);

    // Background MacMessages poll refreshes MESSAGES list while JOAT modal is up:
    expect(await menu.presentList(
      'MESSAGES',
      ['New message · voice recipient + text', '[now] Alex Rivera • · Or 1:00 PM?', '[2m] Sam Lee · See you next week.'],
      'voice-msg-1',
      true,
      'tap: select · left x2: back',
    )).toBe(true);
    expect(r.hud.hasAssistantConversation()).toBe(true);
    expect(await r.hud.dismissAssistantConversation('msg-summary-1', 1)).toBe(true);
    expect(last(r.rebuilds)?.listObject?.[0]?.itemContainer?.itemName?.[1]).toContain('Or 1:00 PM?');

    // 2. Open MESSAGES · CONVERSATION reader, invoke JOAT ("suggest replies"), receive background thread refresh:
    expect(await menu.presentList(
      'MESSAGES · CONVERSATION',
      ['Alex Rivera', 'Alex Rivera: Does 12:30 work for lunch?'],
      'voice-msg-1',
      true,
      'tap: reply · left x2: back',
      undefined,
      'Alex Rivera',
      undefined,
      undefined,
      undefined,
      { id: 'thread-alex-1', index: 0, count: 1 },
    )).toBe(true);
    expect(await r.hud.showAssistantConversation({
      conversationId: 'msg-suggest-1',
      revision: 1,
      state: 'ready',
      listening: true,
      continueAfterMs: 120_000,
      earlierTurnsOmitted: false,
      turns: [
        { role: 'user', text: 'suggest replies' },
        { role: 'assistant', text: '1) 12:30 works great! 2) Can we do 1:00 instead?' },
      ],
    })).toBe(true);
    expect(r.hud.hasAssistantConversation()).toBe(true);

    // Incoming text refreshes MESSAGES · CONVERSATION underneath without tearing down JOAT modal:
    expect(await menu.presentList(
      'MESSAGES · CONVERSATION',
      ['Alex Rivera', 'Alex Rivera: Or 1:00 PM?', 'Alex Rivera: Does 12:30 work for lunch?'],
      'voice-msg-1',
      true,
      'tap: reply · left x2: back',
      undefined,
      'Alex Rivera',
      undefined,
      undefined,
      undefined,
      { id: 'thread-alex-1', index: 0, count: 1 },
    )).toBe(true);
    expect(r.hud.hasAssistantConversation()).toBe(true);

    // Wearer picks option 2 -> ConfirmGate opens over MESSAGES · CONVERSATION, then dismisses back to updated thread!
    expect(await r.hud.showConfirmation(
      'Send to Alex Rivera: Can we do 1:00 instead?',
      'confirm-msg-1',
      'messages',
    )).toBe(true);
    expect(r.hud.hasAssistantConversation()).toBe(false);
    expect(await r.hud.dismissConfirmation('confirm-msg-1')).toBe(true);
    const restoredReader = last(r.rebuilds)?.textObject?.find((t) => t.containerName === 'main');
    expect(restoredReader?.content).toContain('Or 1:00 PM?');
  });
});
