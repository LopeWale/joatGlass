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
  const visual = {
    paint: vi.fn(async (_frame: HudVisualFrame) => true),
    paintExact: vi.fn(async (_frame: HudVisualFrame, current: () => boolean) => current()),
    forgetPage: vi.fn(),
    whenIdle: vi.fn(async () => true),
    withPageTransition: vi.fn(async (replace: () => Promise<boolean>) => replace()),
  };
  const hud = new HudPresenter(client, new HudPageBuilder(), visual);
  return { hud, rebuilds, upgrades };
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
