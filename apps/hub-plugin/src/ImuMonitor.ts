import { ImuReportPace } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import type { MacLink } from './MacLink';
import type { IMU_Report_Data } from '@evenrealities/even_hub_sdk';

export type ConfirmationOutcome = 'accepted' | 'declined' | 'timed_out';

/**
 * Exclusive confirmation gate for consequential actions (RULES.md #6).
 *
 * IMU samples are available only for opt-in diagnostics. Uncalibrated motion
 * must never approve an action: the visible contract requires a deliberate
 * G2 touchpad press, while a swipe or timeout refuses.
 */
export class ImuMonitor {
  private pending?: {
    actionId: string;
    active: boolean;
    expiresAt: number;
    onResult?: (accepted: boolean) => void;
    onOutcome?: (outcome: ConfirmationOutcome) => void;
    abort?: { signal: AbortSignal; handler: () => void };
  };
  private expiryTimer?: ReturnType<typeof setTimeout>;
  private controlEpoch = 0;
  private desiredOpen = false;
  private trace = false;

  constructor(
    private readonly client: BridgeClient,
    private readonly link: MacLink,
  ) {}

  async start(
    pace: ImuReportPace = ImuReportPace.P100,
    signal?: AbortSignal,
  ): Promise<boolean> {
    const epoch = ++this.controlEpoch;
    this.desiredOpen = true;
    try {
      const ok = await this.client.assertStarted().imuControl(true, pace);
      if (signal?.aborted || epoch !== this.controlEpoch || !this.desiredOpen) {
        try {
          await this.client.require().imuControl(false);
        } catch {
          // Best-effort compensation; link-loss cleanup will retry stop.
        }
        return false;
      }
      return ok;
    } catch (err) {
      // Hosts without IMU reject the call outright — the desktop simulator's
      // bridge implements no `imuControl` variant at all. Degrade instead of
      // killing boot: diagnostic IMU is unavailable, tap confirmation still works.
      console.warn('[ImuMonitor] imuControl unavailable on this host:', err);
      return false;
    }
  }

  async stop(): Promise<void> {
    this.controlEpoch += 1;
    this.desiredOpen = false;
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
  await(
    actionId: string,
    timeoutMs = 8000,
    onResult?: (accepted: boolean) => void,
    onOutcome?: (outcome: ConfirmationOutcome) => void,
  ): void {
    this.prepare(actionId, onResult, undefined, onOutcome);
    this.activate(actionId, timeoutMs);
  }

  /** Reserve exclusive input ownership while its HUD prompt is rendering. */
  prepare(
    actionId: string,
    onResult?: (accepted: boolean) => void,
    signal?: AbortSignal,
    onOutcome?: (outcome: ConfirmationOutcome) => void,
  ): void {
    // One decision surface at a time. Replacing a pending request refuses it
    // before the new prompt is armed, so an old action can never be approved
    // by a gesture intended for the new one.
    if (this.pending) this.resolve(false, 'declined');
    const pending = {
      actionId,
      active: false,
      expiresAt: Number.POSITIVE_INFINITY,
      ...(onResult ? { onResult } : {}),
      ...(onOutcome ? { onOutcome } : {}),
      ...(signal ? {
        abort: {
          signal,
          handler: () => { this.cancel(actionId); },
        },
      } : {}),
    };
    this.pending = pending;
    if (pending.abort) {
      if (pending.abort.signal.aborted) pending.abort.handler();
      else pending.abort.signal.addEventListener('abort', pending.abort.handler, { once: true });
    }
    console.info(`[joat trace] trace=${actionId} stage=confirmation-received`);
  }

  /** Start the decision clock only after the complete prompt is visible. */
  activate(actionId: string, timeoutMs = 8000): boolean {
    if (this.pending?.actionId !== actionId) return false;
    this.pending.active = true;
    this.pending.expiresAt = Date.now() + timeoutMs;
    this.expiryTimer = setTimeout(() => this.resolve(false, 'timed_out'), Math.max(0, timeoutMs));
    console.info(`[joat trace] trace=${actionId} stage=confirmation-visible`);
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
    this.resolve(false, 'declined');
    return true;
  }

  /** Teardown path: clear timers without emitting into a link being closed. */
  dispose(): void {
    if (this.expiryTimer) clearTimeout(this.expiryTimer);
    this.expiryTimer = undefined;
    this.detachAbort(this.pending);
    this.pending = undefined;
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
      this.resolve(false, 'timed_out');
      return;
    }

    // Motion is intentionally observational only. It can expire a stale gate
    // above, but it cannot produce an approval verdict.
  }

  /**
   * Explicit accept path — a deliberate tap is unambiguous, unlike a nod.
   *
   * Fed by either built-in G2 temple click. Optional R1 input may route here
   * too, but is never required or advertised.
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
      this.resolve(false, 'timed_out');
      return true;
    }
    this.resolve(true, 'accepted');
    return true;
  }

  /** Explicit refusal routed from a G2 swipe while a gate is live. */
  declineByTap(): boolean {
    if (!this.pending) return false;
    this.resolve(false, 'declined');
    return true;
  }

  private resolve(accepted: boolean, outcome: ConfirmationOutcome): void {
    if (!this.pending) return;
    const pending = this.pending;
    const { actionId, onResult, onOutcome } = pending;
    this.pending = undefined;
    this.detachAbort(pending);
    if (this.expiryTimer) clearTimeout(this.expiryTimer);
    this.expiryTimer = undefined;
    console.info(
      `[joat trace] trace=${actionId} stage=confirmation-settled accepted=${accepted}`,
    );
    if (onResult) {
      onResult(accepted);
    } else {
      this.link.send({ kind: 'confirm', actionId, accepted });
    }
    onOutcome?.(outcome);
  }

  private detachAbort(pending: typeof this.pending): void {
    if (!pending?.abort) return;
    pending.abort.signal.removeEventListener('abort', pending.abort.handler);
  }
}
