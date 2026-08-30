import type { EvenAppBridge } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import type { HudPageBuilder } from './HudPageBuilder';

export type HudPageSurface = 'visual' | 'text' | 'minimal';
export type HudFallbackSurface = Exclude<HudPageSurface, 'visual'>;

export interface HudPageBootResult {
  surface: HudPageSurface;
  contextualMenu: boolean;
}

type HudPageBootClient = Pick<BridgeClient, 'start' | 'require'>;
type HudPageBootLayouts = Pick<
  HudPageBuilder,
  | 'visualStartup'
  | 'compatibilityLayout'
  | 'dashboardCompatibilityLayout'
  | 'defaultLayout'
  | 'visualLayout'
>;

async function rebuild(
  client: HudPageBootClient,
  page: Parameters<EvenAppBridge['rebuildPageContainer']>[0],
): Promise<boolean> {
  try {
    return await client.require().rebuildPageContainer(page);
  } catch {
    return false;
  }
}

function observe(observer: ((outcome: boolean) => void) | undefined, outcome: boolean): void {
  try {
    observer?.(outcome);
  } catch {
    // Observation must not decide whether the wearer gets a usable HUD.
  }
}

/**
 * Use the visual G2 surface as the one-shot startup page. If the native host
 * rejects it, BridgeClient rebuilds the known-visible text page and this
 * routine continues through the compatibility path.
 *
 * This is intentionally not a simulator-only path: the package and simulator
 * exercise the exact same native-page contract. A startup rejection is not a
 * black lens; it lands on the durable text page before optional enhancements.
 */
export async function initializeHudPage(
  client: HudPageBootClient,
  layouts: HudPageBootLayouts,
): Promise<HudPageBootResult> {
  const startup = await client.start(layouts.visualStartup(), layouts.compatibilityLayout());
  if (startup === 'created') {
    // `visualStartup()` declares the menu alongside the image containers. A
    // future visual rebuild must carry that declaration forward: omitting it
    // would clear the OS overlay while keeping the rest of the HUD visible.
    return { surface: 'visual', contextualMenu: true };
  }

  if (!await rebuild(client, layouts.dashboardCompatibilityLayout())) {
    const restored = await rebuild(client, layouts.compatibilityLayout());
    if (!restored) {
      throw new Error('startup text dashboard rejected; minimal page restoration also failed');
    }
    return { surface: 'minimal', contextualMenu: false };
  }

  if (await rebuild(client, layouts.defaultLayout())) {
    return { surface: 'text', contextualMenu: true };
  }

  // Reassert the known-good page after any failed or rejected enhancement;
  // do not assume a native `false` left the prior containers intact.
  if (!await rebuild(client, layouts.dashboardCompatibilityLayout())) {
    if (!await rebuild(client, layouts.compatibilityLayout())) {
      throw new Error('failed to restore any visible HUD after contextual menu rejection');
    }
    return { surface: 'minimal', contextualMenu: false };
  }
  return { surface: 'text', contextualMenu: false };
}

/** Attempt the modern visual page; restore the negotiated text page on failure. */
export async function activateVisualHudPage(
  client: HudPageBootClient,
  layouts: Pick<HudPageBuilder, 'visualLayout' | 'defaultLayout' | 'compatibilityLayout'>,
  fallback: HudFallbackSurface = 'text',
  onVisualRebuild?: (outcome: boolean) => void,
): Promise<HudPageSurface> {
  const visualRebuilt = await rebuild(client, layouts.visualLayout());
  observe(onVisualRebuild, visualRebuilt);
  if (visualRebuilt) return 'visual';
  if (fallback === 'text' && await rebuild(client, layouts.defaultLayout())) {
    return 'text';
  }
  if (await rebuild(client, layouts.compatibilityLayout())) return 'minimal';
  throw new Error('visual HUD rejected and every compatibility restoration failed');
}

export function restoreTextHudPage(
  client: HudPageBootClient,
  layouts: Pick<HudPageBuilder, 'defaultLayout'>,
): Promise<boolean> {
  return rebuild(client, layouts.defaultLayout());
}

export function restoreMinimalHudPage(
  client: HudPageBootClient,
  layouts: Pick<HudPageBuilder, 'compatibilityLayout'>,
): Promise<boolean> {
  return rebuild(client, layouts.compatibilityLayout());
}

export async function restoreCompatibleHudPage(
  client: HudPageBootClient,
  layouts: Pick<HudPageBuilder, 'defaultLayout' | 'compatibilityLayout'>,
  fallback: HudFallbackSurface,
): Promise<HudFallbackSurface> {
  if (fallback === 'text' && await rebuild(client, layouts.defaultLayout())) {
    return 'text';
  }
  if (await rebuild(client, layouts.compatibilityLayout())) return 'minimal';
  throw new Error('could not restore a compatible HUD surface');
}

interface FirstLightHud {
  setStatus(text: string): Promise<boolean>;
}

interface HudFirstLightOptions<T extends FirstLightHud> {
  activateSurface(): Promise<HudPageSurface>;
  restoreFallback(): Promise<HudFallbackSurface>;
  restoreMinimal(): Promise<boolean>;
  createVisual(): T;
  createText(): T;
  createMinimal(): T;
  onVisualFirstFrame?: (outcome: boolean) => void;
}

async function attempt(action: () => Promise<boolean>): Promise<boolean> {
  try {
    return await action();
  } catch {
    return false;
  }
}

/**
 * Bridge acknowledgement is not pixel proof, but a failed first frame is a
 * definitive reason to abandon the visual page. Select modern UI only after
 * both its container rebuild and first complete paint acknowledge success.
 */
export async function selectHudFirstLight<T extends FirstLightHud>(
  options: HudFirstLightOptions<T>,
): Promise<{ hud: T; surface: HudPageSurface }> {
  let surface = await options.activateSurface();
  if (surface === 'visual') {
    const visual = options.createVisual();
    const painted = await attempt(() => visual.setStatus('JOAT · connecting'));
    observe(options.onVisualFirstFrame, painted);
    if (painted) return { hud: visual, surface: 'visual' };
    try {
      surface = await options.restoreFallback();
    } catch {
      if (!await attempt(options.restoreMinimal)) {
        throw new Error('visual first paint failed and no visible fallback could be restored');
      }
      surface = 'minimal';
    }
  }

  const fallback = surface === 'text' ? options.createText() : options.createMinimal();
  if (await attempt(() => fallback.setStatus('JOAT · connecting'))) {
    return { hud: fallback, surface };
  }
  if (surface === 'text' && await attempt(options.restoreMinimal)) {
    const minimal = options.createMinimal();
    if (await attempt(() => minimal.setStatus('JOAT · connecting'))) {
      return { hud: minimal, surface: 'minimal' };
    }
  } else if (surface === 'minimal') {
    await attempt(options.restoreMinimal);
  }
  throw new Error('startup compatibility HUD first paint failed');
}
