import { describe, expect, it, vi } from 'vitest';
import {
  activateVisualHudPage,
  initializeHudPage,
  selectHudFirstLight,
} from './HudPageBootstrap';

type RebuildOutcome = boolean | Error;

function harness(
  rebuildResults: RebuildOutcome[],
  startup: 'created' | 'recovered' = 'recovered',
) {
  const pages = {
    minimal: { name: 'minimal-visible-rebuild' },
    dashboard: { name: 'text-dashboard-without-menu' },
    default: { name: 'text-dashboard-with-menu' },
    visual: { name: 'visual-page' },
  };
  const layouts = {
    visualStartup: vi.fn(() => pages.visual),
    compatibilityLayout: vi.fn(() => pages.minimal),
    dashboardCompatibilityLayout: vi.fn(() => pages.dashboard),
    defaultLayout: vi.fn(() => pages.default),
    visualLayout: vi.fn(() => pages.visual),
  };
  const rebuildPageContainer = vi.fn(async () => {
    const outcome = rebuildResults.shift() ?? false;
    if (outcome instanceof Error) throw outcome;
    return outcome;
  });
  const client = {
    start: vi.fn(async () => startup),
    require: vi.fn(() => ({ rebuildPageContainer })),
  };
  return { client, layouts, pages, rebuildPageContainer };
}

describe('initializeHudPage', () => {
  it('creates the four-tile visual page during the one-shot startup', async () => {
    const { client, layouts, pages, rebuildPageContainer } = harness([], 'created');

    await expect(initializeHudPage(client as never, layouts as never)).resolves.toEqual({
      surface: 'visual',
      contextualMenu: true,
    });

    expect(client.start).toHaveBeenCalledWith(pages.visual, pages.minimal);
    expect(rebuildPageContainer).not.toHaveBeenCalled();
  });

  it('continues from text recovery when the one-shot visual page is rejected', async () => {
    const { client, layouts, pages, rebuildPageContainer } = harness([true, true]);

    await expect(initializeHudPage(client as never, layouts as never)).resolves.toEqual({
      surface: 'text',
      contextualMenu: true,
    });

    expect(client.start).toHaveBeenCalledWith(pages.visual, pages.minimal);
    expect(rebuildPageContainer).toHaveBeenNthCalledWith(1, pages.dashboard);
    expect(rebuildPageContainer).toHaveBeenNthCalledWith(2, pages.default);
  });

  it.each([
    ['resolved false', false],
    ['rejected promise', new Error('native rejected')],
  ] as const)('restores the visible minimal page when the dashboard rebuild %s', async (_name, failure) => {
    const { client, layouts, pages, rebuildPageContainer } = harness([failure, true]);

    await expect(initializeHudPage(client as never, layouts as never))
      .resolves.toEqual({ surface: 'minimal', contextualMenu: false });

    expect(rebuildPageContainer).toHaveBeenNthCalledWith(1, pages.dashboard);
    expect(rebuildPageContainer).toHaveBeenNthCalledWith(2, pages.minimal);
  });

  it.each([
    ['resolved false', false],
    ['rejected promise', new Error('menu rejected')],
  ] as const)('makes menu-free compatibility durable when the menu rebuild %s', async (_name, failure) => {
    const { client, layouts, pages, rebuildPageContainer } = harness([true, failure, true]);

    await expect(initializeHudPage(client as never, layouts as never)).resolves.toEqual({
      surface: 'text',
      contextualMenu: false,
    });

    expect(rebuildPageContainer).toHaveBeenNthCalledWith(2, pages.default);
    expect(rebuildPageContainer).toHaveBeenNthCalledWith(3, pages.dashboard);
  });

  it('continues in minimal compatibility when menu recovery cannot restore the dashboard', async () => {
    const { client, layouts, pages, rebuildPageContainer } = harness([true, false, false, true]);

    await expect(initializeHudPage(client as never, layouts as never))
      .resolves.toEqual({ surface: 'minimal', contextualMenu: false });

    expect(rebuildPageContainer).toHaveBeenNthCalledWith(3, pages.dashboard);
    expect(rebuildPageContainer).toHaveBeenNthCalledWith(4, pages.minimal);
  });

  it('fails only when no known-visible compatibility page can be restored', async () => {
    const { client, layouts } = harness([false, false]);
    await expect(initializeHudPage(client as never, layouts as never))
      .rejects.toThrow('minimal page restoration also failed');
  });

  it('does not rebuild when the visual one-shot startup and its text fallback both fail', async () => {
    const { client, layouts, rebuildPageContainer } = harness([true]);
    client.start.mockRejectedValueOnce(new Error('visual startup failed'));

    await expect(initializeHudPage(client as never, layouts as never))
      .rejects.toThrow('visual startup failed');
    expect(rebuildPageContainer).not.toHaveBeenCalled();
  });
});

describe('activateVisualHudPage', () => {
  it('selects the visual surface only after its rebuild is acknowledged', async () => {
    const { client, layouts, pages, rebuildPageContainer } = harness([true]);

    await expect(activateVisualHudPage(client as never, layouts as never)).resolves.toBe('visual');
    expect(rebuildPageContainer).toHaveBeenCalledWith(pages.visual);
  });

  it('reports a visual rebuild result without letting observer errors affect first light', async () => {
    const { client, layouts } = harness([true]);
    const observer = vi.fn(() => { throw new Error('telemetry unavailable'); });

    await expect(activateVisualHudPage(
      client as never,
      layouts as never,
      'text',
      observer,
    )).resolves.toBe('visual');
    expect(observer).toHaveBeenCalledWith(true);
  });

  it.each([
    ['resolved false', false],
    ['rejected promise', new Error('visual rejected')],
  ] as const)('restores the text dashboard when visual activation %s', async (_name, failure) => {
    const { client, layouts, pages, rebuildPageContainer } = harness([failure, true]);

    await expect(activateVisualHudPage(client as never, layouts as never)).resolves.toBe('text');
    expect(rebuildPageContainer).toHaveBeenNthCalledWith(1, pages.visual);
    expect(rebuildPageContainer).toHaveBeenNthCalledWith(2, pages.default);
  });

  it('degrades to the minimal live surface when visual and text pages are rejected', async () => {
    const { client, layouts, pages, rebuildPageContainer } = harness([false, false, true]);
    await expect(activateVisualHudPage(client as never, layouts as never))
      .resolves.toBe('minimal');
    expect(rebuildPageContainer).toHaveBeenNthCalledWith(3, pages.minimal);
  });

  it('does not retry a text dashboard already rejected during boot', async () => {
    const { client, layouts, pages, rebuildPageContainer } = harness([false, true]);
    await expect(activateVisualHudPage(client as never, layouts as never, 'minimal'))
      .resolves.toBe('minimal');
    expect(rebuildPageContainer).toHaveBeenNthCalledWith(1, pages.visual);
    expect(rebuildPageContainer).toHaveBeenNthCalledWith(2, pages.minimal);
  });

  it('fails only when visual and every known-visible fallback are rejected', async () => {
    const { client, layouts } = harness([false, false, false]);
    await expect(activateVisualHudPage(client as never, layouts as never))
      .rejects.toThrow('every compatibility restoration failed');
  });
});

describe('selectHudFirstLight', () => {
  function hud(painted: boolean) {
    return { setStatus: vi.fn(async () => painted) };
  }

  it('selects the modern HUD only after its first frame is acknowledged', async () => {
    const visual = hud(true);
    const text = hud(true);
    const minimal = hud(true);
    const restoreFallback = vi.fn(async () => 'text' as const);
    const restoreMinimal = vi.fn(async () => true);

    await expect(selectHudFirstLight({
      activateSurface: async () => 'visual',
      restoreFallback,
      restoreMinimal,
      createVisual: () => visual,
      createText: () => text,
      createMinimal: () => minimal,
    })).resolves.toEqual({ hud: visual, surface: 'visual' });

    expect(visual.setStatus).toHaveBeenCalledWith('JOAT · connecting');
    expect(text.setStatus).not.toHaveBeenCalled();
    expect(restoreFallback).not.toHaveBeenCalled();
    expect(restoreMinimal).not.toHaveBeenCalled();
  });

  it('reports a rejected visual first frame before restoring text', async () => {
    const visual = hud(false);
    const text = hud(true);
    const firstFrame = vi.fn();

    await expect(selectHudFirstLight({
      activateSurface: async () => 'visual',
      restoreFallback: async () => 'text',
      restoreMinimal: async () => true,
      createVisual: () => visual,
      createText: () => text,
      createMinimal: () => hud(true),
      onVisualFirstFrame: firstFrame,
    })).resolves.toEqual({ hud: text, surface: 'text' });
    expect(firstFrame).toHaveBeenCalledWith(false);
  });

  it('restores and paints text when the visual containers acknowledge but the first frame fails', async () => {
    const visual = hud(false);
    const text = hud(true);
    const minimal = hud(true);
    const restoreFallback = vi.fn(async () => 'text' as const);
    const restoreMinimal = vi.fn(async () => true);

    await expect(selectHudFirstLight({
      activateSurface: async () => 'visual',
      restoreFallback,
      restoreMinimal,
      createVisual: () => visual,
      createText: () => text,
      createMinimal: () => minimal,
    })).resolves.toEqual({ hud: text, surface: 'text' });

    expect(restoreFallback).toHaveBeenCalledTimes(1);
    expect(text.setStatus).toHaveBeenCalledWith('JOAT · connecting');
  });

  it('paints text immediately when the native host rejects the modern HUD', async () => {
    const visual = hud(true);
    const text = hud(true);
    const minimal = hud(true);

    await expect(selectHudFirstLight({
      activateSurface: async () => 'text',
      restoreFallback: async () => 'text',
      restoreMinimal: async () => true,
      createVisual: () => visual,
      createText: () => text,
      createMinimal: () => minimal,
    })).resolves.toEqual({ hud: text, surface: 'text' });

    expect(visual.setStatus).not.toHaveBeenCalled();
    expect(text.setStatus).toHaveBeenCalledTimes(1);
  });

  it('continues with the minimal live presenter when that is the only accepted page', async () => {
    const minimal = hud(true);
    await expect(selectHudFirstLight({
      activateSurface: async () => 'minimal',
      restoreFallback: async () => 'minimal',
      restoreMinimal: async () => true,
      createVisual: () => hud(false),
      createText: () => hud(true),
      createMinimal: () => minimal,
    })).resolves.toEqual({ hud: minimal, surface: 'minimal' });
    expect(minimal.setStatus).toHaveBeenCalledWith('JOAT · connecting');
  });

  it.each([
    ['resolved false', false],
    ['rejected promise', new Error('text paint rejected')],
  ] as const)('switches to a live minimal presenter when text first paint %s', async (_name, failure) => {
    const restoreMinimal = vi.fn(async () => true);
    const minimal = hud(true);
    const text = {
      setStatus: vi.fn(async () => {
        if (failure instanceof Error) throw failure;
        return failure;
      }),
    };
    await expect(selectHudFirstLight({
      activateSurface: async () => 'text',
      restoreFallback: async () => 'text',
      restoreMinimal,
      createVisual: () => hud(true),
      createText: () => text,
      createMinimal: () => minimal,
    })).resolves.toEqual({ hud: minimal, surface: 'minimal' });
    expect(restoreMinimal).toHaveBeenCalledTimes(1);
  });

  it('fails only when the sole minimal container cannot be painted', async () => {
    const restoreMinimal = vi.fn(async () => true);
    await expect(selectHudFirstLight({
      activateSurface: async () => 'minimal',
      restoreFallback: async () => 'minimal',
      restoreMinimal,
      createVisual: () => hud(true),
      createText: () => hud(true),
      createMinimal: () => hud(false),
    })).rejects.toThrow('compatibility HUD first paint failed');
    expect(restoreMinimal).toHaveBeenCalledTimes(1);
  });
});
