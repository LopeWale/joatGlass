import { AudioInputSource } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import type { MacLink } from './MacLink';
import type { AudioStreamer } from './AudioStreamer';
import type { ImuMonitor } from './ImuMonitor';
import type { HudPresenter } from './HudPresenter';
import type { Settings } from './Settings';

/** Fields we track per onDeviceStatusChanged callback. All optional — older hosts omit them. */
interface Snapshot {
  sn?: string;
  battery?: number;
  wearing?: boolean;
  charging?: boolean;
  inCase?: boolean;
  connect: string;
}

/** Warn on the HUD when battery first drops to this level (percent), unless charging. */
const LOW_BATTERY_PCT = 15;

function extractSnTail(sn: string | undefined): string | undefined {
  if (typeof sn !== 'string') return undefined;
  const clean = sn.trim();
  return clean.length > 0 ? clean.slice(-4) : undefined;
}

function isLikelyRingSn(sn: string): boolean {
  const upper = sn.trim().toUpperCase();
  return upper.startsWith('R1') || upper.includes('RING') || upper.includes('_R1');
}

/**
 * Owns the onDeviceStatusChanged subscription: relays every snapshot to the
 * Mac/iPhone brain, and applies the local policy that must NOT wait for a
 * round-trip.
 *
 * SDK 0.0.16 dispatches `onDeviceStatusChanged` for BOTH G2 glasses AND the
 * Even R1 smart ring without filtering `status.sn`, while `DeviceStatus.fromJson`
 * defaults omitted `isWearing` to `false` and `batteryLevel` to `0`. Filtering
 * non-glasses serial numbers prevents R1 ring connect/disconnect flaps near
 * AirPods or car Bluetooth from shutting off the G2 microphone or flashing
 * bogus 0% battery / "paused (glasses off)" states.
 */
export class DeviceMonitor {
  /** Battery feed for the plugin-owned status bar. */
  onBattery?: (level: number | undefined, charging: boolean) => void;
  /** Fired when a non-G2 Even accessory (e.g. R1 ring) reports status. */
  onAccessoryStatus?: (count: number, connect: string, snTail?: string) => void;
  private prev?: Snapshot;
  private glassesSn?: string;
  private accessoryEventCount = 0;
  private warnedLowBattery = false;

  constructor(
    private readonly client: BridgeClient,
    private readonly link: MacLink,
    private readonly audio: AudioStreamer,
    private readonly imu: ImuMonitor,
    private readonly hud: HudPresenter,
    private readonly settings: Settings,
  ) {}

  setGlassesSn(sn: string | undefined): void {
    const clean = typeof sn === 'string' ? sn.trim() : '';
    if (clean.length > 0 && clean !== 'unknown') {
      this.glassesSn = clean;
    }
  }

  private isNonGlassesStatus(sn: string | undefined): boolean {
    if (typeof sn !== 'string' || sn.trim().length === 0) return false;
    const clean = sn.trim();
    if (this.glassesSn) return clean !== this.glassesSn;
    if (isLikelyRingSn(clean)) return true;
    if (this.prev?.sn && this.prev.wearing === true && clean !== this.prev.sn) return true;
    return false;
  }

  attach(): void {
    this.client.track(
      this.client.require().onDeviceStatusChanged((status) => {
        const rawSn = (status as { sn?: string }).sn;
        const rawInCase = (status as { isInCase?: boolean }).isInCase;
        const snTail = extractSnTail(rawSn);
        const nonGlasses = this.isNonGlassesStatus(rawSn);
        if (!nonGlasses && typeof rawSn === 'string' && rawSn.trim().length > 0 && status.isWearing === true && !this.glassesSn) {
          this.glassesSn = rawSn.trim();
        }
        const next: Snapshot = {
          ...(typeof rawSn === 'string' && rawSn.trim().length > 0 ? { sn: rawSn.trim() } : {}),
          battery: status.batteryLevel,
          wearing: status.isWearing,
          charging: status.isCharging,
          ...(rawInCase !== undefined ? { inCase: rawInCase } : {}),
          connect: status.connectType,
        };
        this.link.send({
          kind: 'device',
          ...(next.battery !== undefined ? { battery: next.battery } : {}),
          ...(next.wearing !== undefined ? { wearing: next.wearing } : {}),
          ...(next.charging !== undefined ? { charging: next.charging } : {}),
          ...(next.inCase !== undefined ? { inCase: next.inCase } : {}),
          connect: next.connect,
          ...(snTail !== undefined ? {
            deviceRole: nonGlasses ? 'other' : 'glasses',
            snTail,
          } : {}),
        });
        if (nonGlasses) {
          this.accessoryEventCount += 1;
          this.onAccessoryStatus?.(this.accessoryEventCount, next.connect, snTail);
          return;
        }
        this.onBattery?.(next.battery, next.charging === true);
        this.apply(this.prev, next);
        this.prev = next;
      }),
    );
  }

  private apply(prev: Snapshot | undefined, next: Snapshot): void {
    // Doff: kill the glasses mic immediately. The Mac also sees wearing=false
    // via the relay above and can adjust its session state; we don't wait.
    if (prev?.wearing !== false && next.wearing === false) {
      void this.audio.stop();
      void this.hud.setStatus('JOAT · paused (glasses off)');
    }

    if (prev?.wearing !== true && next.wearing === true) {
      this.onWorn();
    }

    // Battery: warn once per dip below the threshold; re-arm once recovered.
    const pct = next.battery;
    if (pct !== undefined) {
      if (pct <= LOW_BATTERY_PCT && next.charging !== true && !this.warnedLowBattery) {
        this.warnedLowBattery = true;
        void this.hud.setStatus(`JOAT · battery ${pct}%`);
      } else if ((pct > LOW_BATTERY_PCT || next.charging === true) && this.warnedLowBattery) {
        this.warnedLowBattery = false;
      }
    }
  }

  /**
   * Don (glasses back on). The mic is OFF here, because doff stopped it.
   * How it resumes is the wearer's choice (Settings, left-temple double-tap):
   *   auto    — start the glasses mic immediately;
   *   mac     — status line only; the Mac sees wearing=true from the relay
   *             and sends audio.control when its session logic wants ears;
   *   confirm — HUD prompt, resume only on nod / temple or ring tap (RULES.md
   *             consequential-action gate). Timeout or no answer = stay off.
   */
  private onWorn(): void {
    // Nothing to resume — e.g. the first status event after boot, where
    // main.ts already started the mic. Prompting to "resume" a live mic
    // would paint a lying status line on a privacy surface.
    if (this.audio.isOpen) return;
    if (this.settings.micMode === 'on_demand') {
      void this.hud.setStatus('JOAT · hold to talk');
      return;
    }
    const targetSource = this.settings.micMode === 'phone'
      ? AudioInputSource.Phone
      : AudioInputSource.Glasses;
    const activeLabel = targetSource === AudioInputSource.Phone
      ? 'JOAT · listening (phone mic)'
      : 'JOAT · listening';
    switch (this.settings.resumePolicy) {
      case 'auto':
        void this.audio.start(targetSource);
        void this.hud.setStatus(activeLabel);
        break;
      case 'mac':
        void this.hud.setStatus('JOAT · glasses on');
        break;
      case 'confirm': {
        const actionId = 'local:resume';
        const rendered = this.hud.showConfirmation('Resume listening?', actionId);
        this.imu.prepare(actionId, (accepted) => {
          void this.hud.dismissConfirmation(actionId);
          if (accepted) void this.audio.start(targetSource);
          void this.hud.setStatus(accepted ? activeLabel : 'JOAT · mic off');
        });
        void rendered.then(
          (ok) => {
            if (ok) this.imu.activate(actionId, 8000);
            else this.imu.cancel(actionId);
          },
          () => { this.imu.cancel(actionId); },
        );
        break;
      }
    }
  }
}
