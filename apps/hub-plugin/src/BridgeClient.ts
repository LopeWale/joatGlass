import {
  waitForEvenAppBridge,
  StartUpPageCreateResult,
  ImageRawDataUpdateResult,
  utf8ByteLength,
  type EvenAppBridge,
  type RebuildPageContainer,
} from '@evenrealities/even_hub_sdk';
import { LIST_ITEM_MAX_BYTES, LIST_MAX_ITEMS, TEXT_CONTAINER_MAX_BYTES } from './FirmwareLimits';
import type { Unsubscribe } from './types';

/** How the host accepted the one-shot startup request. */
export type StartupPageResolution = 'created' | 'recovered';

/**
 * Owns the SDK bridge lifecycle and enforces the required call order:
 *   1. waitForEvenAppBridge()
 *   2. createStartUpPageContainer()  — exactly once, must return success
 *   3. everything else
 *
 * audioControl(Glasses) and imuControl fail if step 2 has not succeeded, so
 * every consumer goes through `assertStarted()` before touching those APIs.
 */
/** DEV-only: name every host display call. Each one costs real BLE time on a
 * G2 (~83ms textContainerUpgrade, ~165ms rebuildPageContainer, ~104ms+ per
 * image tile), so "how many calls, of what kind" is the rendering cost — the
 * simulator itself is too fast to show it. */
function hostCall(method: string, detail?: number): void {
  if (import.meta.env.DEV) console.info('[host-call]', method, detail ?? '');
}

/** A bounded wait that ran out; the work it waited on may still finish. */
export class SettleTimeout extends Error {}

/** Resolve with `work`, or reject once `ms` have passed without it settling. */
export function settleWithin<T>(work: Promise<T>, ms: number, what: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => reject(new SettleTimeout(`${what} did not settle within ${ms}ms`)), ms);
  });
  return Promise.race([work, deadline]).finally(() => clearTimeout(timer));
}

/**
 * How long a bridge call to the glasses may take before it counts as failed.
 * Even: "a single flaky hop can hang ~30s; wrap calls in Promise.race with a
 * few-second cap". A rebuild normally answers in about 165 ms and a text
 * update in about 83 ms. Together with a page change's image drain
 * (PAGE_TRANSITION_IDLE_MS, 8 s) this stays inside MacLink's 15 s downlink
 * watchdog, and a hung call no longer holds every later page change behind
 * it. Image writes keep their own limits (HudVisualSurface).
 */
export const BRIDGE_CALL_TIMEOUT_MS = 5_000;

/** A rejected rebuild leaves the glass on its previous page with no visible
 * error. Name the limit that was crossed (FirmwareLimits) so the failure is
 * never silent again — the News tab card and list both failed this way for
 * weeks, and every status push re-sent the same rejected page. */
function reportRejectedPage(page: RebuildPageContainer): void {
  const over: string[] = [];
  for (const t of page.textObject ?? []) {
    const n = utf8ByteLength(t.content ?? '');
    if (n > TEXT_CONTAINER_MAX_BYTES) over.push(`text '${t.containerName}' ${n}B > ${TEXT_CONTAINER_MAX_BYTES}B`);
  }
  for (const l of page.listObject ?? []) {
    const names = l.itemContainer?.itemName ?? [];
    if (names.length > LIST_MAX_ITEMS) over.push(`list '${l.containerName}' ${names.length} rows > ${LIST_MAX_ITEMS}`);
    names.forEach((name, i) => {
      const n = utf8ByteLength(name);
      if (n > LIST_ITEM_MAX_BYTES) over.push(`list '${l.containerName}' row ${i} ${n}B > ${LIST_ITEM_MAX_BYTES}B`);
    });
  }
  console.warn('[page-rejected] rebuildPageContainer', over.length ? over.join('; ') : 'no byte-limit violation (see simulator/host log)');
}

export class BridgeClient {
  private bridge?: EvenAppBridge;
  private displayBridge?: EvenAppBridge;
  private displayActive = true;
  private displayRevision = 0;
  private started = false;
  /** Bumped by every full-page write (startup page or rebuild) as it goes
   * to the host, so a caller can tell whether the page it drew is still the
   * page on the glass before it upgrades one of its containers. */
  private pageEpochValue = 0;
  private readonly unsubscribes: Unsubscribe[] = [];
  private launchSource?: string;
  /** What the host's storage holds, as far as this session has read or
   * written it. */
  private readonly storedValues = new Map<string, string>();
  private storageWrites: Promise<unknown> = Promise.resolve();

  /**
   * The Even host's storage, the one door for every key. A write shares the
   * glasses' link with the page writes, and Even warns that racing them can
   * drop the connection: so a value the host already holds is not written
   * again (the preferred Mac was rewritten on every reconnect), and writes go
   * one after another.
   */
  readonly storage = {
    get: async (key: string): Promise<string> => {
      const value = await this.require().getLocalStorage(key);
      this.storedValues.set(key, value);
      return value;
    },
    set: (key: string, value: string): Promise<boolean> => {
      if (this.storedValues.get(key) === value) return Promise.resolve(true);
      this.storedValues.set(key, value);
      const forget = (): false => {
        if (this.storedValues.get(key) === value) this.storedValues.delete(key);
        return false;
      };
      const write = this.storageWrites.then(() => this.require().setLocalStorage(key, value))
        .then((stored) => stored || forget(), forget);
      this.storageWrites = write;
      return write;
    },
  };

  /** Step 1. Also registers onLaunchSource, which fires exactly once.
   *
   * BOUNDED on purpose: waitForEvenAppBridge never rejects, so a host that
   * never announces the bridge leaves boot suspended forever — a dark HUD,
   * no error on the phone panel, and nothing in the Mac log. That is
   * indistinguishable from a network fault while being a completely
   * different problem, so it must fail loudly instead of hanging.
   */
  async init(timeoutMs = 8000): Promise<EvenAppBridge> {
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      this.bridge = await Promise.race([
        waitForEvenAppBridge(),
        new Promise<never>((_, reject) => {
          timer = setTimeout(
            () => reject(new Error(
              `Even App bridge never became ready after ${timeoutMs}ms — ` +
              'is this page open inside the Even Hub app?')),
            timeoutMs,
          );
        }),
      ]);
    } finally {
      if (timer !== undefined) clearTimeout(timer);
    }
    this.track(this.bridge.onLaunchSource((src) => { this.launchSource = src; }));
    return this.require();
  }

  /**
   * Step 2. createStartUpPageContainer is one-shot PER HOST SESSION, not per
   * page load: a reloaded WebView (vite HMR, `evenhub qr` hot reload) boots
   * again inside a host that already has the startup page, and the host
   * answers `invalid`. When that happens, repaint the existing page with
   * `rebuild` instead — a genuinely invalid layout fails the rebuild too and
   * still surfaces the original error.
   */
  async start(
    container: Parameters<EvenAppBridge['createStartUpPageContainer']>[0],
    rebuild?: Parameters<EvenAppBridge['rebuildPageContainer']>[0],
  ): Promise<StartupPageResolution> {
    if (this.started) throw new Error('createStartUpPageContainer already called (one-shot)');
    const current = this.captureDisplayGuard();
    const result = await this.require().createStartUpPageContainer(container);
    if (!current()) throw new Error('startup display ownership retired');
    if (result === StartUpPageCreateResult.success) {
      this.started = true;
      return 'created';
    }
    if (result === StartUpPageCreateResult.invalid && rebuild !== undefined) {
      if (await this.require().rebuildPageContainer(rebuild) && current()) {
        this.started = true;
        return 'recovered';
      }
    }
    throw new Error(`startup page failed: ${StartUpPageCreateResult[result] ?? result}`);
  }

  /** Throws unless the startup page succeeded — gates glasses mic + IMU. */
  assertStarted(): EvenAppBridge {
    if (!this.started) throw new Error('startup page not created; glasses mic and IMU unavailable');
    return this.require();
  }

  /** The number of full-page writes sent so far (see pageEpochValue). */
  pageEpoch(): number {
    return this.pageEpochValue;
  }

  require(): EvenAppBridge {
    if (!this.bridge) throw new Error('bridge not initialized; call init() first');
    if (!this.displayBridge) {
      const native = this.bridge;
      // Only display writes are gated. Keep the SDK singleton as the receiver
      // for every other method, including synchronous listener registration
      // and microphone/IMU cleanup. Never mutate the SDK singleton itself.
      const writes: Pick<EvenAppBridge,
        'createStartUpPageContainer' | 'rebuildPageContainer' | 'textContainerUpgrade' | 'updateImageRawData'
      > = {
        createStartUpPageContainer: (page) => this.writeDisplay(
          () => {
            this.pageEpochValue += 1;
            hostCall('createStartUpPageContainer');
            return native.createStartUpPageContainer(page);
          },
          StartUpPageCreateResult.invalid,
        ),
        rebuildPageContainer: (page) => this.writeDisplay(
          async () => {
            this.pageEpochValue += 1;
            hostCall('rebuildPageContainer', page.textObject?.length);
            const accepted = await native.rebuildPageContainer(page);
            if (!accepted) reportRejectedPage(page);
            return accepted;
          },
          false,
          'rebuildPageContainer',
        ),
        textContainerUpgrade: (text) => this.writeDisplay(
          () => { hostCall('textContainerUpgrade', text.containerID); return native.textContainerUpgrade(text); },
          false,
          'textContainerUpgrade',
        ),
        updateImageRawData: (image) => this.writeDisplay(
          () => { hostCall('updateImageRawData', image.containerID); return native.updateImageRawData(image); },
          ImageRawDataUpdateResult.sendFailed,
        ),
      };
      this.displayBridge = new Proxy(native, {
        get(target, property) {
          switch (property) {
            case 'createStartUpPageContainer': return writes.createStartUpPageContainer;
            case 'rebuildPageContainer': return writes.rebuildPageContainer;
            case 'textContainerUpgrade': return writes.textContainerUpgrade;
            case 'updateImageRawData': return writes.updateImageRawData;
            default: {
              const value: unknown = Reflect.get(target, property, target);
              return typeof value === 'function' ? value.bind(target) : value;
            }
          }
        },
      });
    }
    return this.displayBridge;
  }

  /** An evicted document yields its display until deliberate recovery. */
  suspendDisplay(): void {
    if (!this.displayActive) return;
    this.displayActive = false;
    this.displayRevision += 1;
  }

  resumeDisplay(): void {
    this.displayActive = true;
  }

  /** Work from an earlier ownership interval stays stale after resume. */
  captureDisplayGuard(): () => boolean {
    const revision = this.displayRevision;
    const active = this.displayActive;
    return () => active && this.displayActive && this.displayRevision === revision;
  }

  /** Every display write. `bounded` names a page or text write, which fails
   * after BRIDGE_CALL_TIMEOUT_MS instead of holding the glass forever. */
  private async writeDisplay<T>(write: () => Promise<T>, declined: T, bounded?: string): Promise<T> {
    const current = this.captureDisplayGuard();
    if (!current()) return declined;
    let result: T;
    try {
      result = bounded === undefined
        ? await write()
        : await settleWithin(write(), BRIDGE_CALL_TIMEOUT_MS, bounded);
    } catch (error) {
      if (!(error instanceof SettleTimeout)) throw error;
      // The host may still apply it late: nothing may trust the page on the
      // glass to be the one last drawn, so the next write rebuilds it whole.
      this.pageEpochValue += 1;
      console.warn('[bridge]', error.message);
      return declined;
    }
    // A call already handed to the host cannot be recalled. Its late result
    // cannot authorize another tile, fallback rebuild, or visible receipt.
    return current() ? result : declined;
  }

  getLaunchSource(): string | undefined {
    return this.launchSource;
  }

  /** Register an SDK unsubscribe so teardown is total. */
  track(unsub: Unsubscribe): Unsubscribe {
    this.unsubscribes.push(unsub);
    return unsub;
  }

  dispose(): void {
    for (const unsub of this.unsubscribes.splice(0)) {
      try { unsub(); } catch { /* teardown must not throw */ }
    }
  }
}
