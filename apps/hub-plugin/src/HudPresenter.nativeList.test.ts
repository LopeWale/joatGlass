import { describe, expect, it } from 'vitest';
import type { TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import { HudPresenter, NATIVE_LIST_TITLES, usesNativeListPage } from './HudPresenter';
import { HudPageBuilder } from './HudPageBuilder';
import type { BridgeClient } from './BridgeClient';
import type { HudVisualFrame, HudVisualSurface } from './HudVisualSurface';
import { ContainerId } from './types';

/**
 * NEWS and SEARCH are firmware lists, not pixels. Even's display docs put
 * the reason plainly — "native scrollable lists, with scroll highlighting
 * handled in firmware" — so a swipe on them costs no BLE traffic at all,
 * where the four-tile page cost 2-4 image writes at 0.5-2 s each. These pin
 * every transition between the tiles and the list page.
 */

interface RebuiltPage {
  listObject?: unknown[];
  imageObject?: unknown[];
  textObject?: Array<{ containerID?: number; content?: string }>;
}

function harness() {
  const rebuilds: Array<{ list: number; images: number; title?: string }> = [];
  const upgrades: TextContainerUpgrade[] = [];
  const frames: HudVisualFrame[] = [];
  let forgotten = 0;
  const client = {
    require: () => ({
      textContainerUpgrade: async (update: TextContainerUpgrade) => {
        upgrades.push(update);
        return true;
      },
      rebuildPageContainer: async (page: RebuiltPage) => {
        rebuilds.push({
          list: page.listObject?.length ?? 0,
          images: page.imageObject?.length ?? 0,
          title: page.textObject?.find((text) => text.containerID === ContainerId.Main)?.content,
        });
        return true;
      },
    }),
  } as unknown as BridgeClient;
  const visual = {
    paint: async (frame: HudVisualFrame) => { frames.push(frame); return true; },
    forgetPage: () => { forgotten += 1; },
  } as unknown as HudVisualSurface;
  const hud = new HudPresenter(client, new HudPageBuilder(), visual);
  return { hud, rebuilds, upgrades, frames, forgotten: () => forgotten };
}

const NEWS = ['Rates hold · 2h', 'Launch slips · 3h', 'Quake felt · 5h'];

async function openNews(h: ReturnType<typeof harness>) {
  await h.hud.showText('9:41  JOAT ready  bat 87%', ContainerId.StatusBar);
  await h.hud.showList(NEWS, {
    title: 'NEWS', hint: 'tap: open · hold: refresh · left x2: back', selectable: true, railTab: 'news',
  });
}

describe('the firmware list page', () => {
  it('names exactly the two pages the wearer traded the rail for', () => {
    expect([...NATIVE_LIST_TITLES]).toEqual(['NEWS', 'SEARCH']);
    const list = (title: string, railTab?: 'news' | 'search') => ({
      kind: 'list' as const, items: ['x'], selectedIndex: 0,
      options: { title, hint: '', selectable: true, ...(railTab ? { railTab } : {}) },
    });
    expect(usesNativeListPage(list('NEWS', 'news'))).toBe(true);
    expect(usesNativeListPage(list('search', 'search'))).toBe(true);
    // The readers those pages open stay pixels, and so does a NEWS list
    // that is not the rail's own page.
    expect(usesNativeListPage(list('NEWS · ARTICLE', 'news'))).toBe(false);
    expect(usesNativeListPage(list('NEWS'))).toBe(false);
    expect(usesNativeListPage({ kind: 'default' })).toBe(false);
  });

  it('opens NEWS as one rebuild carrying the rows, and paints no frame', async () => {
    const h = harness();
    await openNews(h);
    const framesBefore = h.frames.length;
    expect(h.rebuilds).toEqual([{ list: 1, images: 0, title: 'NEWS' }]);
    expect(h.frames).toHaveLength(framesBefore);
    // The page changed under the tiles and the spine word: forget them.
    expect(h.forgotten()).toBe(1);
    // The status line moved with the wearer, as native text.
    const status = h.upgrades.filter((update) => update.containerID === ContainerId.StatusBar);
    expect(status[status.length - 1]?.content).toBe('9:41  JOAT ready  bat 87%');
  });

  it('writes a new state word on the list page as native text, never a frame', async () => {
    const h = harness();
    await openNews(h);
    const frames = h.frames.length;
    const rebuilds = h.rebuilds.length;
    await h.hud.showText('9:41  JOAT hearing you…  bat 87%', ContainerId.StatusBar);
    expect(h.frames).toHaveLength(frames);
    expect(h.rebuilds).toHaveLength(rebuilds);
    const last = h.upgrades[h.upgrades.length - 1];
    expect(last).toMatchObject({
      containerID: ContainerId.StatusBar, content: '9:41  JOAT hearing you…  bat 87%',
    });
  });

  it('moves the highlight without a rebuild: the firmware owns it', async () => {
    const h = harness();
    await openNews(h);
    const rebuilds = h.rebuilds.length;
    const frames = h.frames.length;
    expect(await h.hud.setSelection(2)).toBe(true);
    expect(await h.hud.moveSelection(-1)).toBe(true);
    expect(h.rebuilds).toHaveLength(rebuilds);
    expect(h.frames).toHaveLength(frames);
  });

  it('rebuilds once when the Mac pushes fresh rows', async () => {
    const h = harness();
    await openNews(h);
    const rebuilds = h.rebuilds.length;
    await h.hud.showList([...NEWS, 'Markets open · 1m'], {
      title: 'NEWS', hint: 'tap: open · hold: refresh · left x2: back', selectable: true, railTab: 'news',
    });
    expect(h.rebuilds).toHaveLength(rebuilds + 1);
    expect(h.rebuilds[h.rebuilds.length - 1]).toMatchObject({ list: 1, images: 0 });
  });

  it('brings the tiles back for the overview: one rebuild, then a frame', async () => {
    const h = harness();
    await openNews(h);
    const forgotten = h.forgotten();
    await h.hud.showDefault();
    const last = h.rebuilds[h.rebuilds.length - 1];
    expect(last).toMatchObject({ list: 0, images: 4 });
    expect(h.forgotten()).toBe(forgotten + 1);
    expect(h.frames[h.frames.length - 1]).toMatchObject({ kind: 'overview' });
  });

  it('opens a headline as pixels again', async () => {
    const h = harness();
    await openNews(h);
    const frames = h.frames.length;
    await h.hud.showList(['Rates hold · Reuters · 2h', 'The central bank held…'], {
      title: 'NEWS · ARTICLE', hint: 'left x2: back', railTab: 'news',
    });
    expect(h.rebuilds[h.rebuilds.length - 1]).toMatchObject({ list: 0, images: 4 });
    expect(h.frames).toHaveLength(frames + 1);
    expect(h.frames[h.frames.length - 1]).toMatchObject({ kind: 'list', title: 'NEWS · ARTICLE' });
  });

  it('treats SEARCH the same way and every other list as pixels', async () => {
    const h = harness();
    await h.hud.showList(['Result · site'], { title: 'SEARCH', railTab: 'search', selectable: true });
    expect(h.rebuilds[h.rebuilds.length - 1]).toMatchObject({ list: 1, images: 0, title: 'SEARCH' });
    const frames = h.frames.length;
    await h.hud.showList(['Loading…'], { title: 'MESSAGES' });
    expect(h.rebuilds[h.rebuilds.length - 1]).toMatchObject({ list: 0, images: 4 });
    expect(h.frames).toHaveLength(frames + 1);
  });

  it('paints a confirmation as pixels over the list, and rebuilds the list when it closes', async () => {
    const h = harness();
    await openNews(h);
    expect(await h.hud.showConfirmation('Send this message?', 'gate-1')).toBe(true);
    expect(h.rebuilds[h.rebuilds.length - 1]).toMatchObject({ list: 0, images: 4 });
    expect(h.frames[h.frames.length - 1]).toMatchObject({ kind: 'confirmation' });
    const frames = h.frames.length;
    await h.hud.dismissConfirmation('gate-1');
    expect(h.rebuilds[h.rebuilds.length - 1]).toMatchObject({ list: 1, images: 0, title: 'NEWS' });
    expect(h.frames).toHaveLength(frames);
  });

  it('heals a refused write by rebuilding the list page, not the tiles', async () => {
    const rebuilt: RebuiltPage[] = [];
    let refuse = false;
    const client = {
      require: () => ({
        textContainerUpgrade: async () => {
          if (refuse) { refuse = false; return false; }
          return true;
        },
        rebuildPageContainer: async (page: RebuiltPage) => { rebuilt.push(page); return true; },
      }),
    } as unknown as BridgeClient;
    const visual = {
      paint: async () => true,
      forgetPage: () => undefined,
    } as unknown as HudVisualSurface;
    const hud = new HudPresenter(client, new HudPageBuilder(), visual);
    await hud.showList(NEWS, { title: 'NEWS', selectable: true, railTab: 'news' });
    refuse = true;
    expect(await hud.showText('9:41  JOAT ready', ContainerId.StatusBar)).toBe(true);
    const healed = rebuilt[rebuilt.length - 1];
    expect(healed?.listObject).toHaveLength(1);
    expect(healed?.imageObject ?? []).toHaveLength(0);
  });
});
