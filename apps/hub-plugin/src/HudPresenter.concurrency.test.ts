import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { RebuildPageContainer, TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import { HudPageBuilder } from './HudPageBuilder';
import { HudPresenter } from './HudPresenter';
import type { HudStatusModel } from './HudStatus';
import type { HudVisualFrame } from './HudVisualSurface';
import { navigationVisualFixture, type NavigationHudModel } from './NavigationHud';
import type { AssistantConversationSnapshot } from './types';

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

function conversation(revision = 1): AssistantConversationSnapshot {
  return {
    conversationId: 'walk-and-talk',
    revision,
    state: 'ready',
    turns: [
      { role: 'user', text: 'Remind me what Dele said about the gate.' },
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

    // Leaving the conversation shows the map at the newest frame, not the
    // one that was on the glass when the conversation began.
    expect(await r.hud.requestAssistantConversationExit()).toBe('prompted');
    expect(await r.hud.requestAssistantConversationExit()).toBe('exited');
    const map = r.frames[r.frames.length - 1];
    expect(map).toMatchObject({ kind: 'navigation', navigation: { maneuverDistanceM: 80 } });
    // On the map the turn is the page; the spine carries the timer.
    expect(map?.statusModel?.activities).toEqual(['Focus 12m']);
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

  it('still owns Home: from the dashboard a frame repaints the map', async () => {
    const r = rig();
    await r.hud.setStatusModel(LIVE);
    await r.hud.showDefault();
    expect(await r.hud.updateNavigation(guidance(120))).toBe(true);
    expect(r.frames[r.frames.length - 1]).toMatchObject({ kind: 'navigation', navigation: { maneuverDistanceM: 120 } });
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

    await r.hud.showList(['Dele · On my way'], { title: 'MESSAGES', selectable: true, nativeFirmwareList: true });
    expect(r.headerOf(r.rebuilds[r.rebuilds.length - 1])).toBe('9:41 · MESSAGES · Focus 12m · ● · ▆');

    // 11:30 left: at the minute boundary the chip reads 11, written in place.
    await vi.advanceTimersByTimeAsync(31_000);
    expect(last(r.statusWrites())).toBe('9:41 · MESSAGES · Focus 11m · ● · ▆');
  });
});
