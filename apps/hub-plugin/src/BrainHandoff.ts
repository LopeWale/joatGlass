import type { LinkState } from './MacLink';
import type { HandoffCapability } from './types';

/**
 * A Mac-pinned page (Messages, Agent Terminal, Activity), opened
 * while the phone brain (JOAT Brain) holds the glasses.
 *
 * Either the plugin intercepts the menu pick directly on the phone brain or
 * the phone brain answers with `brain.handoff`. This paints a single focus
 * list frame upfront, dials the package's other brains in order (the Mac that
 * last answered first; no Mac is named in code) without extra default-layout
 * BLE rebuilds, opens the page on the first that authenticates, and goes back
 * to the phone brain once the wearer is Home again, or when that Mac goes
 * away. A hand-off is never remembered as the preferred brain: a relaunch in
 * the middle of a session starts on the phone.
 */
export interface BrainHandoffDeps {
  /** The package's brains (MAC_WS_URLS). */
  urls: readonly string[];
  link: { readonly activeUrl: string; readonly linkState: LinkState; readonly wasReplaced: boolean };
  /** Retire the glasses' surfaces and dial `url` (main.ts switchBrain). */
  switchBrain: (url: string, options?: { skipDefaultPaint?: boolean }) => Promise<boolean>;
  menu: {
    prepareMacHandoffPage?: (capability: HandoffCapability) => Promise<boolean>;
    showLocalPage: (title: string, items: string[]) => Promise<boolean>;
    openOnMac: (capability: HandoffCapability) => Promise<void>;
  };
  lastMac: { get: () => string | null; set: (url: string) => void };
  isExiting: () => boolean;
  onChange?: () => void;
  log?: (line: string) => void;
  /** How long one Mac gets to answer (MacLink: 4 s to open, 4 s to authenticate). */
  dialMs?: number;
  /** How long a Mac that went away gets to come back. */
  lostMs?: number;
  pollMs?: number;
}

interface Visit {
  home: string;
  capability: HandoffCapability;
  /** 'switching' while the Macs are dialed; 'open' once one answered. */
  state: 'switching' | 'open';
  /** The wearer went Home while the Macs were still being dialed. */
  cancelled: boolean;
  lostTimer?: ReturnType<typeof setTimeout>;
}

/** The Mac that last took a hand-off: tried first next time (browser storage;
 * only ever an entry of the package's own list). */
export const HANDOFF_LAST_MAC_KEY = 'joat.handoff.last-mac.v1';

/** The package's other brains, the Mac that last answered first. */
export function handoffCandidates(urls: readonly string[], home: string, last: string | null): string[] {
  const others = urls.filter((url) => url !== home);
  return last !== null && others.includes(last)
    ? [last, ...others.filter((url) => url !== last)]
    : others;
}

export class BrainHandoff {
  private visit?: Visit;
  private readonly dialMs: number;
  private readonly lostMs: number;
  private readonly pollMs: number;

  constructor(private readonly deps: BrainHandoffDeps) {
    this.dialMs = deps.dialMs ?? 10_000;
    this.lostMs = deps.lostMs ?? 20_000;
    this.pollMs = deps.pollMs ?? 200;
  }

  /** A page is on (or being opened on) a Mac: not the preferred brain. */
  get active(): boolean {
    return this.visit !== undefined;
  }

  get capability(): HandoffCapability | undefined {
    return this.visit?.capability;
  }

  get homeUrl(): string | undefined {
    return this.visit?.home;
  }

  get lastMacUrl(): string | null {
    const last = this.deps.lastMac.get();
    return last !== null && this.deps.urls.includes(last) ? last : null;
  }

  /** The phone brain handed `capability` over. Resolves once it is open on
   * a Mac, or the glasses are back on the phone. */
  async start(capability: HandoffCapability): Promise<void> {
    const { deps } = this;
    if (deps.isExiting() || this.visit !== undefined) return;
    const home = deps.link.activeUrl;
    const macs = handoffCandidates(deps.urls, home, deps.lastMac.get());
    const title = capability.toUpperCase();
    if (macs.length === 0) {
      deps.log?.(`handoff: ${capability}: this package lists no Mac`);
      await deps.menu.showLocalPage(`${title} · ON A MAC`, [`${capability} runs on a Mac.`, 'This JOAT package lists no Mac.']);
      return;
    }
    const visit: Visit = { home, capability, state: 'switching', cancelled: false };
    this.visit = visit;
    deps.onChange?.();
    const current = () => this.visit === visit && !visit.cancelled && !deps.isExiting();
    deps.log?.(`handoff: ${capability}: trying ${macs.length} Mac(s)`);
    const hasPrepare = typeof deps.menu.prepareMacHandoffPage === 'function';
    if (hasPrepare) {
      await deps.menu.prepareMacHandoffPage!(capability);
    }
    for (const mac of macs) {
      if (!current() || !await deps.switchBrain(mac, { skipDefaultPaint: true }) || !current()) break;
      if (!hasPrepare) {
        await deps.menu.showLocalPage(title, [`Opening ${capability} on your Mac…`]);
      }
      const answered = await this.waitFor(() => !current() || deps.link.linkState === 'connected', this.dialMs);
      if (!current()) break;
      if (answered) {
        visit.state = 'open';
        deps.lastMac.set(mac);
        deps.onChange?.();
        deps.log?.(`handoff: ${capability}: open on ${hostOf(mac)}`);
        await deps.menu.openOnMac(capability);
        return;
      }
      deps.log?.(`handoff: ${capability}: ${hostOf(mac)} did not answer`);
    }
    // The wearer chose a brain on the phone page: that choice stands.
    if (this.visit !== visit) return;
    this.visit = undefined;
    deps.onChange?.();
    if (deps.isExiting()) return;
    await deps.switchBrain(home);
    deps.onChange?.();
    if (!visit.cancelled) {
      await deps.menu.showLocalPage(`${title} · NO MAC`, [
        `${capability} runs on your Mac.`,
        'No Mac answered. Is JOAT open',
        'there, with Tailscale on?',
      ]);
    }
  }

  /** The wearer is Home: the page handed to a Mac is over. */
  home(): void {
    const visit = this.visit;
    if (visit === undefined) return;
    if (visit.state === 'switching') {
      if (!visit.cancelled) {
        visit.cancelled = true;
        this.deps.onChange?.();
      }
      return;
    }
    void this.back(visit, 'home');
  }

  /** Every link state: a Mac that stays away hands the glasses back. */
  linkState(state: LinkState): void {
    const visit = this.visit;
    if (visit?.state !== 'open') return;
    if (state === 'connected') {
      if (visit.lostTimer !== undefined) clearTimeout(visit.lostTimer);
      visit.lostTimer = undefined;
    } else if (visit.lostTimer === undefined && !this.deps.link.wasReplaced) {
      visit.lostTimer = setTimeout(() => {
        visit.lostTimer = undefined;
        if (this.visit === visit && this.deps.link.linkState !== 'connected') void this.back(visit, 'lost');
      }, this.lostMs);
    }
  }

  /** The wearer picked a brain on the phone page: no way back to arrange. */
  cancel(): void {
    const visit = this.visit;
    if (visit === undefined) return;
    if (visit.lostTimer !== undefined) clearTimeout(visit.lostTimer);
    this.visit = undefined;
    this.deps.onChange?.();
  }

  private async back(visit: Visit, why: 'home' | 'lost'): Promise<void> {
    if (this.visit !== visit) return;
    if (visit.lostTimer !== undefined) clearTimeout(visit.lostTimer);
    this.visit = undefined;
    const { deps } = this;
    deps.onChange?.();
    deps.log?.(`handoff: ${visit.capability}: back to the phone (${why})`);
    if (deps.isExiting()) return;
    const switched = await deps.switchBrain(visit.home);
    deps.onChange?.();
    if (!switched || why !== 'lost') return;
    await deps.menu.showLocalPage(`${visit.capability.toUpperCase()} · MAC LOST`, [
      'Lost the Mac.',
      'JOAT is back on the phone.',
    ]);
  }

  private async waitFor(done: () => boolean, ms: number): Promise<boolean> {
    const until = Date.now() + ms;
    while (!done()) {
      if (Date.now() >= until) return false;
      await new Promise((resolve) => setTimeout(resolve, this.pollMs));
    }
    return true;
  }
}

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}
