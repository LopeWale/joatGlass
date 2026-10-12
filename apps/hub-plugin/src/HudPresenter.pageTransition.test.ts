import { describe, expect, it } from 'vitest';
import { ImageRawDataUpdateResult, type RebuildPageContainer } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import { HudPageBuilder } from './HudPageBuilder';
import { HudPresenter, setNativeOverviewForTest } from './HudPresenter';
import { HudVisualSurface, type HudFrameRenderer } from './HudVisualSurface';
import { ContainerId } from './types';

// Home as firmware text, the default.
setNativeOverviewForTest(true);

/**
 * One page change at a time goes to the glass (HudVisualSurface's page
 * barrier). A second one that arrives while the first is in flight must
 * wait for it, not be turned away: a decision card turned away is declined
 * before the wearer ever sees it, and a Back turned away leaves the old
 * page on the glass. And the barrier must hand the glass back when a page
 * change ends, whatever the page that asked for it decided meanwhile.
 */
function rig() {
  const pages: RebuildPageContainer[] = [];
  let holdHome = false;
  let release: (() => void) | undefined;
  const client = {
    require: () => ({
      updateImageRawData: async () => ImageRawDataUpdateResult.success,
      textContainerUpgrade: async () => true,
      rebuildPageContainer: async (page: RebuildPageContainer) => {
        const home = page.textObject?.some((text) => text.containerName === 'tabs') ?? false;
        if (home && holdHome) {
          holdHome = false;
          await new Promise<void>((resolve) => { release = resolve; });
        }
        pages.push(page);
        return true;
      },
    }),
  } as unknown as BridgeClient;
  const renderer: HudFrameRenderer = {
    render: async () => [ContainerId.Image, 8, 9, 10].map((containerId) => ({
      containerId, pixels: new Uint8Array(4),
    })),
  };
  const hud = new HudPresenter(client, new HudPageBuilder(), new HudVisualSurface(client, renderer));
  const main = (page: RebuildPageContainer | undefined) => (
    page?.textObject?.find((text) => text.containerName === 'main')?.content ?? ''
  );
  return {
    hud,
    pages,
    lastMain: () => main(pages[pages.length - 1]),
    lastIsHome: () => pages[pages.length - 1]?.textObject?.some((text) => text.containerName === 'tabs') ?? false,
    /** The next rebuild of Home waits until `releaseHome`. */
    holdNextHome: () => { holdHome = true; },
    homeHeld: () => release !== undefined,
    releaseHome: () => { release?.(); release = undefined; },
  };
}

const waitFor = async (condition: () => boolean): Promise<void> => {
  for (let tries = 0; tries < 200 && !condition(); tries += 1) await new Promise((resolve) => setTimeout(resolve, 1));
  expect(condition()).toBe(true);
};

describe('HudPresenter page changes that overlap', () => {
  it('shows a card that lands while Home comes back from the last one, and the glass keeps painting', async () => {
    const r = rig();
    expect(await r.hud.showText('home', ContainerId.Main)).toBe(true);
    expect(await r.hud.showConfirmation('Add lunch at noon?', 'first')).toBe(true);

    r.holdNextHome();
    const back = r.hud.dismissConfirmation('first');
    await waitFor(r.homeHeld);
    const second = r.hud.showConfirmation('Send to Mom?', 'second');
    r.releaseHome();
    await back;

    expect(await second).toBe(true);
    expect(r.lastMain()).toContain('Send to Mom?');
    // The glass was handed back: leaving this card repaints Home.
    expect(await r.hud.dismissConfirmation('second')).toBe(true);
    expect(r.lastIsHome()).toBe(true);
  });

  it('shows a card that lands while Home is being redrawn, instead of declining it unseen', async () => {
    const r = rig();
    expect(await r.hud.showText('home', ContainerId.Main)).toBe(true);
    expect(await r.hud.showList(['Pause', 'Reset'], { title: 'POMODORO', hint: 'tap: select · 2x tap: back' })).toBe(true);

    r.holdNextHome();
    const home = r.hud.showDefault();
    await waitFor(r.homeHeld);
    const card = r.hud.showConfirmation('Send to Mom?', 'during-home');
    r.releaseHome();
    await home;

    expect(await card).toBe(true);
    expect(r.lastMain()).toContain('Send to Mom?');
  });

  it('takes Back to Home even when it lands while another page change is running', async () => {
    const r = rig();
    expect(await r.hud.showText('home', ContainerId.Main)).toBe(true);
    expect(await r.hud.showList(['Pause', 'Reset'], { title: 'POMODORO', hint: 'tap: select · 2x tap: back' })).toBe(true);

    r.holdNextHome();
    const first = r.hud.showDefault();
    await waitFor(r.homeHeld);
    const second = r.hud.showDefault();
    r.releaseHome();

    expect(await first).toBe(true);
    expect(await second).toBe(true);
    expect(r.lastIsHome()).toBe(true);
  });
});
