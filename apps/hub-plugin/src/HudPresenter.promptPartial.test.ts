import { describe, expect, it, vi } from 'vitest';
import type { RebuildPageContainer, TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import { HudPageBuilder } from './HudPageBuilder';
import { HudPresenter, setNativeOverviewForTest } from './HudPresenter';
import type { HudVisualFrame } from './HudVisualSurface';

setNativeOverviewForTest(false);

/**
 * The wearer's words appear in the Messages reply card while they speak
 * (prompt.partial). They are display only, and a card that keeps its size
 * is upgraded in place rather than rebuilt.
 */
function rig() {
  const rebuilds: RebuildPageContainer[] = [];
  const upgrades: TextContainerUpgrade[] = [];
  const client = {
    require: () => ({
      rebuildPageContainer: vi.fn(async (page: RebuildPageContainer) => { rebuilds.push(page); return true; }),
      textContainerUpgrade: vi.fn(async (update: TextContainerUpgrade) => { upgrades.push(update); return true; }),
    }),
  } as unknown as BridgeClient;
  // A page replacement waits on `gate.current`, so a test can hold one.
  const gate = { current: Promise.resolve() };
  let replacing = false;
  const visual = {
    paint: vi.fn(async (_frame: HudVisualFrame) => true),
    paintExact: vi.fn(async (_frame: HudVisualFrame, current: () => boolean) => current()),
    forgetPage: vi.fn(),
    whenIdle: vi.fn(async () => true),
    withPageTransition: vi.fn(async (replace: () => Promise<boolean>) => {
      // The real guard: a second replacement while one runs is refused.
      if (replacing) return false;
      replacing = true;
      try { await gate.current; return await replace(); } finally { replacing = false; }
    }),
  };
  const hud = new HudPresenter(client, new HudPageBuilder(), visual);
  return { hud, rebuilds, upgrades, gate };
}

function last<T>(items: readonly T[]): T | undefined {
  return items[items.length - 1];
}

const QUESTION = 'What do you want to say back to Adele?';

function wearerWords(page: RebuildPageContainer | undefined): string | undefined {
  return page?.textObject?.find((text) => text.containerName === 'wearer-turns')?.content;
}

describe('HudPresenter reply card words so far', () => {
  it('shows the words as they are spoken, in place while the card keeps its size', async () => {
    const r = rig();
    expect(await r.hud.showPromptQuestion('p1', QUESTION, () => true, 'messages')).toBe(true);
    const opened = r.rebuilds.length;

    expect(await r.hud.showPromptPartial('p1', 'Running')).toBe(true);
    // The card grew a row for the wearer's words: one rebuild.
    expect(r.rebuilds).toHaveLength(opened + 1);
    expect(wearerWords(last(r.rebuilds))).toContain('Running');

    expect(await r.hud.showPromptPartial('p1', 'Running late')).toBe(true);
    // Same size: only the words changed, upgraded in place.
    expect(r.rebuilds).toHaveLength(opened + 1);
    expect(last(r.upgrades)?.containerName).toBe('wearer-turns');
    expect(last(r.upgrades)?.content).toContain('Running late');
  });

  it('a cancel during a growing partial waits for it, then restores the page beneath', async () => {
    // The page guard refuses a second replacement while one runs: a cancel
    // that did not wait for the partial's rebuild was refused and left the
    // cancelled card on the glass.
    const r = rig();
    await r.hud.showList(['1/1 · Adele', 'Adele: hi'], {
      title: 'MESSAGES · CONVERSATION', hint: 'tap: reply', selectable: true, nativeTextPage: true,
    });
    await r.hud.showPromptQuestion('p1', QUESTION, () => true, 'messages');
    let release: () => void = () => undefined;
    r.gate.current = new Promise<void>((resolve) => { release = resolve; });
    const partial = r.hud.showPromptPartial('p1', 'Running');  // grows the card: a rebuild, held
    await Promise.resolve();
    const dismissed = r.hud.dismissPromptQuestion('p1');
    let settled = false;
    void dismissed.then(() => { settled = true; });
    await new Promise((resolve) => setTimeout(resolve, 5));
    expect(settled).toBe(false);  // the cancel waits for the held rebuild
    release();
    expect(await partial).toBe(false);
    expect(await dismissed).toBe(true);
    const onGlass = last(r.rebuilds);
    expect(onGlass?.textObject?.some((text) => text.containerName === 'conversation')).toBe(false);
    expect(onGlass?.textObject?.some((text) => text.containerName === 'main')).toBe(true);
  });

  it('ignores words for another prompt or a non-Messages question', async () => {
    const r = rig();
    await r.hud.showPromptQuestion('p1', QUESTION, () => true, 'messages');
    expect(await r.hud.showPromptPartial('p2', 'Running')).toBe(false);

    const other = rig();
    await other.hud.showPromptQuestion('q1', 'What should the note say?', () => true, 'capability');
    expect(await other.hud.showPromptPartial('q1', 'Buy milk')).toBe(false);
  });

  it('paints only the newest words when several arrive during one paint', async () => {
    const r = rig();
    await r.hud.showPromptQuestion('p1', QUESTION, () => true, 'messages');
    const first = r.hud.showPromptPartial('p1', 'Run');
    void r.hud.showPromptPartial('p1', 'Running');
    void r.hud.showPromptPartial('p1', 'Running late, start');
    await first;
    await vi.waitFor(() => expect(
      wearerWords(last(r.rebuilds))?.includes('Running late, start')
        || last(r.upgrades)?.content?.includes('Running late, start'),
    ).toBe(true));
    const painted = [...r.rebuilds.map(wearerWords), ...r.upgrades.map((u) => u.content)];
    expect(painted.some((words) => words?.includes('Running') && !words.includes('late'))).toBe(false);
  });
});
