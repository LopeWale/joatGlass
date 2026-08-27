import { ImuReportPace } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import type { MacLink } from './MacLink';
import type { IMU_Report_Data } from '@evenrealities/even_hub_sdk';

/**
 * IMU-based nod detection — our Confirmer for consequential actions
 * (RULES.md #6: every computer-use action needs explicit confirmation).
 *
 * At P100 (10 Hz) a deliberate nod is a pitch swing beyond threshold and back
 * within the window. Thresholds need calibration against real hardware; the
 * defaults are a starting point, not tuned values.
 */
export class ImuMonitor {
  private samples: Array<{ y: number; at: number }> = [];
  private pending?: {
    actionId: string;
    active: boolean;
    expiresAt: number;
    onResult?: (accepted: boolean) => void;
  };
  private expiryTimer?: ReturnType<typeof setTimeout>;
  private open = false;
  private trace = false;

  constructor(
    private readonly client: BridgeClient,
    private readonly link: MacLink,
    private readonly threshold = 0.35,
    private readonly windowMs = 1200,
  ) {}

  async start(pace: ImuReportPace = ImuReportPace.P100): Promise<boolean> {
    try {
      const ok = await this.client.assertStarted().imuControl(true, pace);
      this.open = ok;
      return ok;
    } catch (err) {
      // Hosts without IMU reject the call outright — the desktop simulator's
      // bridge implements no `imuControl` variant at all. Degrade instead of
      // killing boot: nod-confirm is unavailable, tap-confirm still works.
      console.warn('[ImuMonitor] imuControl unavailable on this host:', err);
      this.open = false;
      return false;
    }
  }

  async stop(): Promise<void> {
    if (!this.open) return;
    this.open = false;
    try {
      await this.client.require().imuControl(false);
    } catch {
      // Same hosts reject the off-call too; there is nothing to turn off.
    }
  }

  /**
   * Arm a confirmation window.
   *
   * Without onResult (the Mac's confirm.request flow): the verdict is sent
   * uplink as a confirm message. With onResult (plugin-local decisions like
   * resume-on-don): the callback gets the verdict and NOTHING goes to the
   * Mac — it never asked, so it gets no answer.
   */
  await(actionId: string, timeoutMs = 8000, onResult?: (accepted: boolean) => void): void {
    this.prepare(actionId, onResult);
    this.activate(actionId, timeoutMs);
  }

  /** Reserve exclusive input ownership while its HUD prompt is rendering. */
  prepare(actionId: string, onResult?: (accepted: boolean) => void): void {
    // One decision surface at a time. Replacing a pending request refuses it
    // before the new prompt is armed, so an old action can never be approved
    // by a gesture intended for the new one.
    if (this.pending) this.resolve(false);
    this.pending = { actionId, active: false, expiresAt: Number.POSITIVE_INFINITY, onResult };
    this.samples = [];
  }

  /** Start the decision clock only after the complete prompt is visible. */
  activate(actionId: string, timeoutMs = 8000): boolean {
    if (this.pending?.actionId !== actionId) return false;
    this.pending.active = true;
    this.pending.expiresAt = Date.now() + timeoutMs;
    this.expiryTimer = setTimeout(() => this.resolve(false), Math.max(0, timeoutMs));
    return true;
  }

  /**
   * Calibration mode: relay every raw sample uplink so thresholds can be
   * derived from real labelled gestures instead of guessed. Off by default —
   * at P100 this is 10 messages/second of pure debug traffic.
   */
  setTrace(enabled: boolean): void {
    this.trace = enabled;
  }

  /** True only while an exclusive decision surface owns wearer gestures. */
  isPending(): boolean {
    return this.pending !== undefined;
  }

  /** Fail closed only if the named action still owns the decision surface. */
  cancel(actionId: string): boolean {
    if (this.pending?.actionId !== actionId) return false;
    this.resolve(false);
    return true;
  }

  /** Teardown path: clear timers without emitting into a link being closed. */
  dispose(): void {
    if (this.expiryTimer) clearTimeout(this.expiryTimer);
    this.expiryTimer = undefined;
    this.pending = undefined;
    this.samples = [];
  }

  /** Called by EventRouter for each sysEvent carrying imuData. */
  ingest(imu: IMU_Report_Data): void {
    const now = Date.now();

    // Trace runs before the pending guard: calibration needs the idle and
    // walking baselines too, not just samples inside a confirm window.
    if (this.trace) {
      this.link.send({ kind: 'imu', x: imu.x ?? 0, y: imu.y ?? 0, z: imu.z ?? 0, at: now });
    }

    if (!this.pending?.active) return;

    if (now > this.pending.expiresAt) {
      this.resolve(false);
      return;
    }

    this.samples.push({ y: imu.y ?? 0, at: now });
    this.samples = this.samples.filter((s) => now - s.at <= this.windowMs);

    const values = this.samples.map((s) => s.y);
    const swing = Math.max(...values) - Math.min(...values);
    if (swing >= this.threshold) this.resolve(true);
  }

  /**
   * Explicit accept path — a deliberate tap is unambiguous, unlike a nod.
   *
   * Fed by the R1 ring click and by a right-temple click. The temple is the
   * one confirm gesture available to a wearer with no ring, so it is not a
   * fallback: until the nod thresholds are calibrated against real traces it
   * is the only confirm surface we actually trust.
   */
  confirmByTap(): boolean {
    if (!this.pending) return false;
    // Rendering owns input but cannot approve. Consume the gesture and wait
    // for an explicit tap after activate() starts the visible decision.
    if (!this.pending.active) return true;
    // Expiry must hold HERE, not only in ingest(): ingest is driven by IMU
    // samples, and on a host where imuControl fails (the simulator rejects
    // it outright) no sample ever arrives — so an armed window lived
    // forever and a much-later unrelated tap approved an action the Mac
    // had long since treated as declined (observed live). An
    // expired window resolves REFUSED, clearing the pending state.
    if (Date.now() > this.pending.expiresAt) {
      this.resolve(false);
      return true;
    }
    this.resolve(true);
    return true;
  }

  /** Explicit refusal: the left temple is otherwise unused while a gate is live. */
  declineByTap(): boolean {
    if (!this.pending) return false;
    this.resolve(false);
    return true;
  }

  private resolve(accepted: boolean): void {
    if (!this.pending) return;
    const { actionId, onResult } = this.pending;
    this.pending = undefined;
    if (this.expiryTimer) clearTimeout(this.expiryTimer);
    this.expiryTimer = undefined;
    this.samples = [];
    if (onResult) {
      onResult(accepted);
    } else {
      this.link.send({ kind: 'confirm', actionId, accepted });
    }
  }
}
