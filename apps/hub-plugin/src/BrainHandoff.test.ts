import { describe, expect, it, vi } from 'vitest';
import type { RebuildPageContainer } from '@evenrealities/even_hub_sdk';
import { BrainHandoff, handoffCandidates, type BrainHandoffDeps } from './BrainHandoff';
import type { BridgeClient } from './BridgeClient';
import { CapabilityMenu } from './CapabilityMenu';
import { Chrome } from './Chrome';
import { HudPageBuilder } from './HudPageBuilder';
import { HudPresenter } from './HudPresenter';
import type { LinkState, MacLink } from './MacLink';
import { isPhoneBrain } from './PhoneControlPanel';
import { Settings } from './Settings';
import { ContainerId, HANDOFF_CAPABILITIES } from './types';

const PHONE = 'ws://127.0.0.1:8787/glasses';
const MAC_A = 'wss://mac-a.example.ts.net/glasses';
const MAC_B = 'wss://mac-b.example.ts.net/glasses';

/** A link whose `answers` say which brains authenticate. */
function harness(
  answers: Record<string, boolean>,
  last: string | null = null,
  withPrepare = false,
) {
  const link = { activeUrl: PHONE, linkState: 'connected' as LinkState, wasReplaced: false };
  const dialed: string[] = [];
  const dialOptions: Array<{ url: string; skipDefaultPaint?: boolean }> = [];
  const pages: string[] = [];
  const prepared: string[] = [];
  const opened: string[] = [];
  let changes = 0;
  let lastMac = last;
  const deps: BrainHandoffDeps = {
    urls: [PHONE, MAC_A, MAC_B],
    link,
    switchBrain: vi.fn(async (url: string, options?: { skipDefaultPaint?: boolean }) => {
      dialed.push(url);
      dialOptions.push({ url, skipDefaultPaint: options?.skipDefaultPaint });
      link.activeUrl = url;
      link.linkState = answers[url] === true ? 'connected' : 'connecting';
      return true;
    }),
    menu: {
      ...(withPrepare ? {
        prepareMacHandoffPage: vi.fn(async (capability) => {
          prepared.push(capability);
          return true;
        }),
      } : {}),
      showLocalPage: vi.fn(async (title: string) => { pages.push(title); return true; }),
      openOnMac: vi.fn(async (capability) => { opened.push(capability); }),
    },
    lastMac: { get: () => lastMac, set: (url) => { lastMac = url; } },
    isExiting: () => false,
    onChange: () => { changes += 1; },
    dialMs: 30,
    lostMs: 30,
    pollMs: 5,
  };
  return {
    link,
    dialed,
    dialOptions,
    pages,
    prepared,
    opened,
    deps,
    changes: () => changes,
    lastMac: () => lastMac,
  };
}

describe('HANDOFF_CAPABILITIES', () => {
  it('pins Messages, Agent Terminal, and Activity to the Mac', () => {
    expect(HANDOFF_CAPABILITIES).toEqual(['Messages', 'Agent Terminal', 'Activity']);
  });
});

describe('handoffCandidates', () => {
  it('is every other brain, in the package order', () => {
    expect(handoffCandidates([PHONE, MAC_A, MAC_B], PHONE, null)).toEqual([MAC_A, MAC_B]);
  });

  it('tries the Mac that last answered first', () => {
    expect(handoffCandidates([PHONE, MAC_A, MAC_B], PHONE, MAC_B)).toEqual([MAC_B, MAC_A]);
  });

  it('ignores a remembered Mac the package no longer lists', () => {
    expect(handoffCandidates([PHONE, MAC_A], PHONE, 'wss://gone.example/glasses')).toEqual([MAC_A]);
  });
});

describe('BrainHandoff', () => {
  it('opens the page on the first Mac that answers, and remembers it', async () => {
    const h = harness({ [MAC_B]: true });
    const handoff = new BrainHandoff(h.deps);
    await handoff.start('Agent Terminal');
    expect(h.dialed).toEqual([MAC_A, MAC_B]);
    expect(h.dialOptions).toEqual([
      { url: MAC_A, skipDefaultPaint: true },
      { url: MAC_B, skipDefaultPaint: true },
    ]);
    expect(h.opened).toEqual(['Agent Terminal']);
    expect(handoff.active).toBe(true);
    expect(handoff.homeUrl).toBe(PHONE);
    expect(handoff.lastMacUrl).toBe(MAC_B);
    expect(h.lastMac()).toBe(MAC_B);
    expect(h.changes()).toBeGreaterThanOrEqual(2);
  });

  it('paints a single upfront handoff page via prepareMacHandoffPage and skips default paint on Mac dial', async () => {
    const order: string[] = [];
    const h = harness({ [MAC_B]: true }, null, true);
    h.deps.menu.prepareMacHandoffPage = vi.fn(async (capability) => {
      order.push(`prepare:${capability}`);
      return true;
    });
    h.deps.switchBrain = vi.fn(async (url: string, options?: { skipDefaultPaint?: boolean }) => {
      order.push(`switch:${url}:${String(options?.skipDefaultPaint)}`);
      h.link.activeUrl = url;
      h.link.linkState = url === MAC_B ? 'connected' : 'connecting';
      return true;
    });
    h.deps.menu.openOnMac = vi.fn(async (capability) => {
      order.push(`openOnMac:${capability}`);
    });

    const handoff = new BrainHandoff(h.deps);
    await handoff.start('Messages');

    expect(order).toEqual([
      'prepare:Messages',
      `switch:${MAC_A}:true`,
      `switch:${MAC_B}:true`,
      'openOnMac:Messages',
    ]);
    expect(h.pages).toEqual([]);
    expect(handoff.active).toBe(true);
    expect(handoff.capability).toBe('Messages');
    expect(handoff.homeUrl).toBe(PHONE);

    handoff.home();
    await vi.waitFor(() => expect(order).toContain(`switch:${PHONE}:undefined`));
    expect(handoff.active).toBe(false);
    expect(handoff.homeUrl).toBeUndefined();
  });

  it('goes back to the phone when the wearer is Home', async () => {
    const h = harness({ [MAC_A]: true });
    const handoff = new BrainHandoff(h.deps);
    await handoff.start('Agent Terminal');
    handoff.home();
    await vi.waitFor(() => expect(h.dialed).toEqual([MAC_A, PHONE]));
    expect(h.dialOptions).toEqual([
      { url: MAC_A, skipDefaultPaint: true },
      { url: PHONE, skipDefaultPaint: undefined },
    ]);
    expect(handoff.active).toBe(false);
    expect(handoff.homeUrl).toBeUndefined();
  });

  it('comes back to the phone and says so when no Mac answers', async () => {
    const h = harness({});
    const handoff = new BrainHandoff(h.deps);
    await handoff.start('Agent Terminal');
    expect(h.dialed).toEqual([MAC_A, MAC_B, PHONE]);
    expect(h.dialOptions).toEqual([
      { url: MAC_A, skipDefaultPaint: true },
      { url: MAC_B, skipDefaultPaint: true },
      { url: PHONE, skipDefaultPaint: undefined },
    ]);
    expect(h.opened).toEqual([]);
    expect(h.pages[h.pages.length - 1]).toBe('AGENT TERMINAL · NO MAC');
    expect(handoff.active).toBe(false);
  });

  it('stops dialing, quietly, when the wearer goes Home first', async () => {
    const h = harness({});
    const handoff = new BrainHandoff(h.deps);
    const started = handoff.start('Agent Terminal');
    await vi.waitFor(() => expect(h.dialed).toEqual([MAC_A]));
    handoff.home();
    await started;
    expect(h.dialed).toEqual([MAC_A, PHONE]);
    expect(h.pages).not.toContain('AGENT TERMINAL · NO MAC');
  });

  it('hands the glasses back when the Mac stays away', async () => {
    const h = harness({ [MAC_A]: true });
    const handoff = new BrainHandoff(h.deps);
    await handoff.start('Agent Terminal');
    h.link.linkState = 'reconnecting';
    handoff.linkState('reconnecting');
    await vi.waitFor(() => expect(h.dialed).toEqual([MAC_A, PHONE]));
    await vi.waitFor(() => expect(h.pages[h.pages.length - 1]).toBe('AGENT TERMINAL · MAC LOST'));
  });

  it('keeps the Mac when it comes back in time', async () => {
    const h = harness({ [MAC_A]: true });
    const handoff = new BrainHandoff({ ...h.deps, lostMs: 60 });
    await handoff.start('Agent Terminal');
    handoff.linkState('reconnecting');
    handoff.linkState('connected');
    await new Promise((resolve) => setTimeout(resolve, 90));
    expect(h.dialed).toEqual([MAC_A]);
    expect(handoff.active).toBe(true);
  });

  it('leaves a brain the wearer picked on the phone page alone', async () => {
    const h = harness({ [MAC_A]: true });
    const handoff = new BrainHandoff(h.deps);
    await handoff.start('Agent Terminal');
    const beforeChanges = h.changes();
    handoff.cancel();
    expect(h.changes()).toBe(beforeChanges + 1);
    handoff.home();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(h.dialed).toEqual([MAC_A]);
  });

  it('says so when the package lists no Mac', async () => {
    const h = harness({});
    const handoff = new BrainHandoff({ ...h.deps, urls: [PHONE] });
    await handoff.start('Agent Terminal');
    expect(h.dialed).toEqual([]);
    expect(h.pages).toEqual(['AGENT TERMINAL · ON A MAC']);
  });

  it('eliminates the 3-rebuild BLE storm end-to-end across HudPresenter + CapabilityMenu + switchBrain, and returns home on hud.rebuild', async () => {
    const rebuilds: RebuildPageContainer[] = [];
    const client = {
      require: () => ({
        rebuildPageContainer: vi.fn(async (page: RebuildPageContainer) => {
          rebuilds.push(page);
          return true;
        }),
        textContainerUpgrade: vi.fn(async () => true),
      }),
    } as unknown as BridgeClient;
    const visual = {
      paint: vi.fn(async () => true),
      paintExact: vi.fn(async () => true),
      withPageTransition: async (replace: () => Promise<boolean>) => replace(),
      forgetPage: vi.fn(),
      suspendDisplay: vi.fn(),
      resumeDisplay: vi.fn(),
    };
    const hud = new HudPresenter(client, new HudPageBuilder(), visual as never);
    await hud.showDefault();
    rebuilds.length = 0;

    const sentByOrigin: Array<{ url: string; msg: Record<string, unknown> }> = [];
    const linkState = {
      activeUrl: PHONE,
      linkState: 'connected' as LinkState,
      wasReplaced: false,
      send: (msg: Record<string, unknown>) => {
        sentByOrigin.push({ url: linkState.activeUrl, msg });
      },
    };
    const chrome = new Chrome(hud);
    chrome.start();
    await new Promise((resolve) => setTimeout(resolve, 0));
    rebuilds.length = 0;

    const menu = new CapabilityMenu(hud, new Settings(), linkState as unknown as MacLink);
    let lastMac: string | null = null;
    const switchBrain = async (url: string, options?: { skipDefaultPaint?: boolean }): Promise<boolean> => {
      const skipDefaultPaint = options?.skipDefaultPaint === true;
      if (!skipDefaultPaint) menu.releaseToLiveSession();
      chrome.stop();
      if (!skipDefaultPaint) hud.suspendDisplay();
      hud.resumeDisplay();
      if (!skipDefaultPaint) {
        const cleared = await hud.showDefault();
        if (!cleared) return false;
      }
      chrome.start();
      linkState.activeUrl = url;
      linkState.linkState = 'connected';
      return true;
    };

    const handoff = new BrainHandoff({
      urls: [PHONE, MAC_A],
      link: linkState,
      switchBrain,
      menu,
      lastMac: { get: () => lastMac, set: (url) => { lastMac = url; } },
      isExiting: () => false,
      dialMs: 30,
      lostMs: 30,
      pollMs: 5,
    });
    menu.handoffCapability = (capability) => {
      if (handoff.active || !isPhoneBrain(linkState.activeUrl)) return false;
      void handoff.start(capability);
      return true;
    };
    menu.onHome = () => handoff.home();

    // Pick Messages while connected to PHONE: intercepts locally without round-tripping to PHONE!
    await menu.openFromContextualMenu(0);
    await vi.waitFor(() => expect(handoff.active).toBe(true));
    await vi.waitFor(() => expect(
      sentByOrigin.some((entry) => entry.url === MAC_A && (entry.msg.event as { containerName?: string } | undefined)?.containerName === 'capmenu'),
    ).toBe(true));

    // Only ONE BLE rebuild happened for the entire transition (the upfront "Opening Messages on your Mac…" list)!
    expect(rebuilds).toHaveLength(1);
    expect(sentByOrigin.filter((entry) => entry.url === PHONE && (entry.msg.event as { containerName?: string } | undefined)?.containerName === 'capmenu')).toHaveLength(0);

    // Mac on_connect hud.text clears arrive while surface is 'list' -> 0 BLE rebuilds!
    await hud.showText('', ContainerId.Main);
    await hud.showText('', ContainerId.Context);
    await hud.showText('', ContainerId.Tray);
    expect(rebuilds).toHaveLength(1);

    // Mac responds with the real MESSAGES list -> 1 BLE rebuild for the destination list!
    const capmenuSend = sentByOrigin.find(
      (entry) => entry.url === MAC_A && (entry.msg.event as { containerName?: string } | undefined)?.containerName === 'capmenu',
    )!;
    const requestId = (capmenuSend.msg.event as { requestId: string }).requestId;
    expect(await menu.presentList('MESSAGES', ['Adele · On my way'], requestId, true, 'tap: open · 2x tap: back')).toBe(true);
    expect(rebuilds).toHaveLength(2);

    // Voice "JOAT, go home" sends `hud.rebuild` on the Mac -> releases menu, resets tab, shows Home, and hands back to PHONE!
    menu.releaseToLiveSession();
    hud.resetTab();
    await hud.showDefault();
    if (handoff.active) {
      setTimeout(() => { handoff.home(); }, 0);
    }
    await vi.waitFor(() => expect(linkState.activeUrl).toBe(PHONE));
    expect(handoff.active).toBe(false);
    expect(menu.isOpen()).toBe(false);
    chrome.stop();
  });

  it('drops trailing downlink frames queued behind brain.handoff on the departing phone generation before setTimeout fires', async () => {
    const h = harness({ [MAC_A]: true }, null, true);
    const handoff = new BrainHandoff(h.deps);
    let handoffDepartingGeneration = -1;
    const overlayPainted: string[] = [];

    const dispatch = async (
      msg: { kind: 'brain.handoff'; capability: 'Messages' } | { kind: 'assistant.overlay'; text: string },
      context: { generation: number; signal: { aborted: boolean } },
    ): Promise<void> => {
      const ensureCurrent = (): void => {
        if (context.signal.aborted || context.generation === handoffDepartingGeneration) {
          throw new Error(`stale downlink generation ${context.generation}`);
        }
      };
      ensureCurrent();
      if (msg.kind === 'brain.handoff') {
        if ((HANDOFF_CAPABILITIES as readonly string[]).includes(msg.capability) && !handoff.active) {
          handoffDepartingGeneration = context.generation;
          setTimeout(() => { void handoff.start(msg.capability); }, 0);
        }
        return;
      }
      overlayPainted.push(msg.text);
    };

    const phoneCtx = { generation: 1, signal: { aborted: false } };
    await dispatch({ kind: 'brain.handoff', capability: 'Messages' }, phoneCtx);
    await expect(
      dispatch({ kind: 'assistant.overlay', text: 'Opening Messages.' }, phoneCtx),
    ).rejects.toThrow('stale downlink generation 1');
    expect(overlayPainted).toEqual([]);

    await vi.waitFor(() => expect(handoff.active).toBe(true));
    expect(h.prepared).toEqual(['Messages']);
    expect(h.opened).toEqual(['Messages']);
  });
});
