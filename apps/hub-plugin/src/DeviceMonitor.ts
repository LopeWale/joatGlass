import { AudioInputSource } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import type { MacLink } from './MacLink';
import type { AudioStreamer } from './AudioStreamer';
import type { ImuMonitor } from './ImuMonitor';
import type { HudPresenter } from './HudPresenter';
import type { Settings } from './Settings';
import { ContainerId, CONFIRM_HINT } from './types';

/** Fields we track per onDeviceStatusChanged callback. All optional — older hosts omit them. */
interface Snapshot {
  battery?: number;
  wearing?: boolean;
  charging?: boolean;
  connect: string;
}

/** Warn on the HUD when battery first drops to this level (percent), unless charging. */
const LOW_BATTERY_PCT = 15;

/**
 * Owns the onDeviceStatusChanged subscription: relays every snapshot to the
 * Mac, and applies the local policy that must NOT wait for a Mac round-trip.
 *
 * Doff (wearing → false) stops the glasses mic HERE: streaming audio from
 * glasses nobody is wearing is a privacy bug, and the fail-safe direction is
 * off. Resuming on don is deliberately not automatic — see onWorn().
 */
export class DeviceMonitor {
  private prev?: Snapshot;
  private warnedLowBattery = false;

  constructor(
    private readonly client: BridgeClient,
    private readonly link: MacLink,
    private readonly audio: AudioStreamer,
    private readonly imu: ImuMonitor,
    private readonly hud: HudPresenter,
    private readonly settings: Settings,
  ) {}

  attach(): void {
    this.client.track(
      this.client.require().onDeviceStatusChanged((status) => {
        const next: Snapshot = {
          battery: status.batteryLevel,
          wearing: status.isWearing,
          charging: status.isCharging,
          connect: status.connectType,
        };
        this.link.send({ kind: 'device', ...next });
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
    switch (this.settings.resumePolicy) {
      case 'auto':
        void this.audio.start(AudioInputSource.Glasses);
        void this.hud.setStatus('JOAT · listening');
        break;
      case 'mac':
        void this.hud.setStatus('JOAT · glasses on');
        break;
      case 'confirm':
        void this.hud.showText(`Resume listening?\n\n${CONFIRM_HINT}`, ContainerId.Main);
        this.imu.await('local:resume', 8000, (accepted) => {
          if (accepted) void this.audio.start(AudioInputSource.Glasses);
          void this.hud.setStatus(accepted ? 'JOAT · listening' : 'JOAT · mic off');
        });
        break;
    }
  }
}
