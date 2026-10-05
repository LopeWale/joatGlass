import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import { HudPresenter } from './HudPresenter';
import { HudPageBuilder } from './HudPageBuilder';
import type { BridgeClient } from './BridgeClient';
import type { HudVisualFrame, HudVisualSurface } from './HudVisualSurface';
import { ContainerId } from './types';

/** The text compatibility presenter (no image surface) shows a list as
 * Even's own list too. */
interface RebuiltPage {
  listObject?: unknown[];
  imageObject?: unknown[];
  textObject?: Array<{ containerID?: number; content?: string }>;
}

/** `withBarrier` adds the surface's page-transition barrier, which every
 * page rebuild (including the self-heal) requires; without it the
 * confirmation and question stay raster, as most of these cases assume. */
function harness(withVisual = true, withBarrier = false) {
  const rebuilds: RebuiltPage[] = [];
  const upgrades: TextContainerUpgrade[] = [];
  const frames: HudVisualFrame[] = [];
  const state = { paintAccepted: true, rebuildAccepted: true };
  const client = {
    require: () => ({
      textContainerUpgrade: async (update: TextContainerUpgrade) => { upgrades.push(update); return true; },
      rebuildPageContainer: async (page: RebuiltPage) => { rebuilds.push(page); return state.rebuildAccepted; },
    }),
  } as unknown as BridgeClient;
  const visual = {
    paint: async (frame: HudVisualFrame) => { frames.push(frame); return state.paintAccepted; },
    paintExact: async (frame: HudVisualFrame, current: () => boolean) => {
      if (!current()) return false;
      frames.push(frame);
      return state.paintAccepted && current();
    },
    forgetPage: () => undefined,
    ...(withBarrier ? { withPageTransition: async (replace: () => Promise<boolean>) => replace() } : {}),
  } as unknown as HudVisualSurface;
  const hud = new HudPresenter(client, new HudPageBuilder(), withVisual ? visual : undefined);
  return { hud, rebuilds, upgrades, frames, state };
}

const NEWS = ['Story 01 · 2h', 'Story 02 · 3h', 'Story 03 · 5h'];
const NEWS_OPTIONS = {
  title: 'NEWS', hint: 'tap: open · hold: refresh · left x2: back',
  selectable: true, railTab: 'news' as const,
};

async function openNews(h: ReturnType<typeof harness>) {
  await h.hud.showText('9:41  JOAT ready  bat 87%', ContainerId.StatusBar);
  return h.hud.showList(NEWS, NEWS_OPTIONS);
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); });

describe('lists without a custom renderer', () => {
  it('retains native list compatibility when no custom renderer exists', async () => {
    const h = harness(false);
    expect(await openNews(h)).toBe(true);
    expect(h.rebuilds).toHaveLength(1);
    expect(h.rebuilds[0]?.listObject).toHaveLength(1);
    expect(h.rebuilds[0]?.imageObject).toBeUndefined();
    expect(h.frames).toEqual([]);
    h.state.rebuildAccepted = false;
    expect(await h.hud.showList(['Result'], { title: 'SEARCH', railTab: 'search' })).toBe(false);
  });
});
