import {
  waitForEvenAppBridge,
  StartUpPageCreateResult,
  type EvenAppBridge,
} from '@evenrealities/even_hub_sdk';
import type { Unsubscribe } from './types';

/**
 * Owns the SDK bridge lifecycle and enforces the required call order:
 *   1. waitForEvenAppBridge()
 *   2. createStartUpPageContainer()  — exactly once, must return success
 *   3. everything else
 *
 * audioControl(Glasses) and imuControl fail if step 2 has not succeeded, so
 * every consumer goes through `assertStarted()` before touching those APIs.
 */
export class BridgeClient {
  private bridge?: EvenAppBridge;
  private started = false;
  private readonly unsubscribes: Unsubscribe[] = [];
  private launchSource?: string;

  /** Step 1. Also registers onLaunchSource, which fires exactly once. */
  async init(): Promise<EvenAppBridge> {
    this.bridge = await waitForEvenAppBridge();
    this.track(this.bridge.onLaunchSource((src) => { this.launchSource = src; }));
    return this.bridge;
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
  ): Promise<void> {
    if (this.started) throw new Error('createStartUpPageContainer already called (one-shot)');
    const result = await this.require().createStartUpPageContainer(container);
    if (result === StartUpPageCreateResult.success) {
      this.started = true;
      return;
    }
    if (result === StartUpPageCreateResult.invalid && rebuild !== undefined) {
      if (await this.require().rebuildPageContainer(rebuild)) {
        this.started = true;
        return;
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
    return this.bridge;
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
