import {
  waitForEvenAppBridge,
  StartUpPageCreateResult,
  ImageRawDataUpdateResult,
  type EvenAppBridge,
} from '@evenrealities/even_hub_sdk';
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

export class BridgeClient {
  private bridge?: EvenAppBridge;
  private displayBridge?: EvenAppBridge;
  private displayActive = true;
  private displayRevision = 0;
  private started = false;
  private readonly unsubscribes: Unsubscribe[] = [];
  private launchSource?: string;

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
          () => { hostCall('createStartUpPageContainer'); return native.createStartUpPageContainer(page); },
          StartUpPageCreateResult.invalid,
        ),
        rebuildPageContainer: (page) => this.writeDisplay(
          () => { hostCall('rebuildPageContainer', page.textObject?.length); return native.rebuildPageContainer(page); },
          false,
        ),
        textContainerUpgrade: (text) => this.writeDisplay(
          () => { hostCall('textContainerUpgrade', text.containerID); return native.textContainerUpgrade(text); },
          false,
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

  private async writeDisplay<T>(write: () => Promise<T>, declined: T): Promise<T> {
    const current = this.captureDisplayGuard();
    if (!current()) return declined;
    const result = await write();
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
