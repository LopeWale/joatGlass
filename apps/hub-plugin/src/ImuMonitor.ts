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
  private pending?: { actionId: string; expiresAt: number };
  private open = false;

  constructor(
    private readonly client: BridgeClient,
    private readonly link: MacLink,
    private readonly threshold = 0.35,
    private readonly windowMs = 1200,
  ) {}

  async start(pace: ImuReportPace = ImuReportPace.P100): Promise<boolean> {
    const ok = await this.client.assertStarted().imuControl(true, pace);
    this.open = ok;
    return ok;
  }

  async stop(): Promise<void> {
    if (!this.open) return;
    await this.client.require().imuControl(false);
    this.open = false;
  }

  /** Arm a confirmation window. The Mac sends confirm.request; we answer confirm. */
  await(actionId: string, timeoutMs = 8000): void {
    this.pending = { actionId, expiresAt: Date.now() + timeoutMs };
    this.samples = [];
  }

  /** Called by EventRouter for each sysEvent carrying imuData. */
  ingest(imu: IMU_Report_Data): void {
    if (!this.pending) return;
    const now = Date.now();

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

  /** Explicit accept path — R1 ring tap is unambiguous, unlike a nod. */
  confirmByRing(): void {
    if (this.pending) this.resolve(true);
  }

  private resolve(accepted: boolean): void {
    if (!this.pending) return;
    this.link.send({ kind: 'confirm', actionId: this.pending.actionId, accepted });
    this.pending = undefined;
    this.samples = [];
  }
}
