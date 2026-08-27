import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Chrome } from './Chrome';
import { HudPresenter } from './HudPresenter';
import { HudPageBuilder } from './HudPageBuilder';
import type { BridgeClient } from './BridgeClient';
import type { TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import { ContainerId } from './types';

/**
 * This file did not exist while Chrome shipped three regressions, which is
 * precisely why they shipped green: nothing in the suite could observe
 * Chrome interacting with a real HudPresenter.
 *
 * The one that reached the wearer: Chrome paints the status bar AND the
 * dashboard hint on every tick, including while a focus page (menu, message
 * list) is up. Those writes must never reach a container the focus layout
 * does not declare, because a failed paint triggers the self-heal, which
 * rebuilds the page and destroys the menu — "the menu shows but immediately
 * disappears and the screen goes black".
 */

const paints: TextContainerUpgrade[] = [];
let rebuilds = 0;

function realPresenter(): HudPresenter {
  const client = {
    require: () => ({
      textContainerUpgrade: async (u: TextContainerUpgrade) => {
        paints.push(u);
        return true;
      },
      rebuildPageContainer: async () => {
        rebuilds += 1;
        return true;
      },
    }),
  } as unknown as BridgeClient;
  return new HudPresenter(client, new HudPageBuilder());
}

const wrote = (id: ContainerId) => paints.filter((p) => p.containerID === id);

beforeEach(() => {
  vi.useFakeTimers();
  paints.length = 0;
  rebuilds = 0;
});

afterEach(() => {
  vi.useRealTimers();
});

describe('Chrome', () => {
  it('paints clock, state and battery into the status bar on start', () => {
    const hud = realPresenter();
    const chrome = new Chrome(hud);
    chrome.setBattery(87, false);
    chrome.start();
    chrome.stop();
    const bar = wrote(ContainerId.StatusBar);
    expect(bar.length).toBeGreaterThan(0);
    expect(bar[bar.length - 1]!.content).toMatch(/\d{1,2}:\d{2}/);
    expect(bar[bar.length - 1]!.content).toContain('bat 87%');
  });

  it('keeps the status bar within the 46-character budget', () => {
    const hud = realPresenter();
    const chrome = new Chrome(hud);
    chrome.setBattery(100, true);
    chrome.setLink('reconnecting');
    chrome.start();
    chrome.stop();
    for (const p of wrote(ContainerId.StatusBar)) {
      expect(p.content!.length).toBeLessThanOrEqual(46);
    }
  });

  it('reports the LINK before the mic — a wearer whose brain is gone needs that first', () => {
    const hud = realPresenter();
    const chrome = new Chrome(hud);
    chrome.setMic(true);
    chrome.setLink('reconnecting');
    chrome.start();
    chrome.stop();
    expect(wrote(ContainerId.StatusBar).pop()!.content).toContain('reconnecting');
  });

  it('a 30s tick during a focus page NEVER rebuilds the page', async () => {
    // The reported bug, pinned at the Chrome end.
    const hud = realPresenter();
    await hud.showList(['Messages', 'Notifications']);
    const rebuildsAfterOpen = rebuilds;
    const chrome = new Chrome(hud);
    chrome.start();
    vi.advanceTimersByTime(90_000); // three ticks
    await Promise.resolve();
    chrome.stop();
    expect(rebuilds).toBe(rebuildsAfterOpen); // the list survives
    expect(wrote(ContainerId.Hint)).toEqual([]); // never written behind focus
  });

  it('setStatus from anywhere becomes the state word, not a second writer', async () => {
    const hud = realPresenter();
    const chrome = new Chrome(hud);
    chrome.setLink('connected');
    chrome.start();
    paints.length = 0;
    await hud.setStatus('JOAT · settings');
    const bar = wrote(ContainerId.StatusBar);
    chrome.stop();
    expect(bar.length).toBeGreaterThan(0);
    // Composed INTO the line — clock and battery survive the announcement.
    expect(bar[bar.length - 1]!.content).toContain('settings');
    expect(bar[bar.length - 1]!.content).toMatch(/\d{1,2}:\d{2}/);
  });

  it('stop() halts the interval so a torn-down Chrome cannot paint', () => {
    const hud = realPresenter();
    const chrome = new Chrome(hud);
    chrome.start();
    chrome.stop();
    paints.length = 0;
    vi.advanceTimersByTime(120_000);
    expect(paints).toEqual([]);
  });
});
