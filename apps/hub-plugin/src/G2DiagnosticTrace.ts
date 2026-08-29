import type { MacLink } from './MacLink';
import type { UplinkMessage } from './types';

/**
 * The real-G2 diagnostic vocabulary. It deliberately has no text, URLs,
 * device identifiers, audio, token, or user-input payload fields. The Mac
 * independently validates this same vocabulary before it writes an export.
 */
export type G2TracePage = 'overview' | 'sidebar' | 'list' | 'confirmation' | 'navigation';

export type G2TraceEvent =
  | {
    type: 'page_transition';
    from: G2TracePage;
    to: G2TracePage;
    reason: 'show_sidebar' | 'show_list' | 'show_overview' | 'show_confirmation' | 'show_navigation' | 'dismiss_confirmation';
    outcome: 'success' | 'failed';
  }
  | {
    type: 'gesture';
    gesture: 'tap' | 'double_press' | 'long_press' | 'long_press_release' | 'scroll_up' | 'scroll_down' | 'menu_item' | 'foreground_enter' | 'foreground_exit' | 'system_exit' | 'abnormal_exit' | 'imu_report' | 'unknown';
    delivery: 'sys' | 'text' | 'list' | 'menu';
  }
  | {
    type: 'tile_upload';
    frame: number;
    order: number;
    containerId: 4 | 8 | 9 | 10;
    outcome: 'success' | 'failed' | 'error';
  }
  /**
   * A content-free first-light receipt. It identifies only whether the real
   * host accepted the modern page and its first image frame; it never carries
   * HUD text, image pixels, or wearer/device data.
   */
  | {
    type: 'hud_surface';
    stage: 'visual_rebuild' | 'first_frame' | 'selected';
    surface: 'visual' | 'text' | 'minimal';
    outcome: 'success' | 'failed';
  }
  | {
    type: 'self_heal';
    surface: G2TracePage;
    target: 'text' | 'visual';
    decision: 'attempt' | 'recovered' | 'abandoned' | 'skipped';
    containerId: number;
  }
  | {
    type: 'location_probe';
    sampleSequence: number;
    profile: 'high';
    lifecycle: 'foreground' | 'background';
    /** Bit 0 timestamp, bit 1 speed, bit 2 heading. Presence only. */
    fields: number;
    cadenceMs: number;
    accuracy: 'unknown' | 'lte5' | 'lte10' | 'lte25' | 'gt25';
    timestampAge: 'missing' | 'future' | 'fresh' | 'stale';
    displacement: 'first' | 'lt5' | '5_20' | '20_100' | 'gt100';
    headingDelta: 'unavailable' | 'insufficient_motion' | '0_30' | '31_90' | '91_180';
  }
  | {
    type: 'location_probe_state';
    requested: 'high' | 'ambient';
    outcome: 'success' | 'failed';
  };

/** Narrow observer seam so instrumentation cannot change HUD control flow. */
export interface G2DiagnosticTrace {
  record(event: G2TraceEvent): void;
}

/**
 * Sends diagnostics only after the trusted Mac explicitly enables the mode.
 * Calls are synchronous and best-effort: an unavailable gateway must never
 * delay rendering, gesture handling, or a recovery attempt.
 */
export class MacG2DiagnosticTrace implements G2DiagnosticTrace {
  private enabled = false;

  constructor(private readonly link: Pick<MacLink, 'sendDiagnostic'>) {}

  setEnabled(enabled: boolean): void {
    this.enabled = enabled;
  }

  record(event: G2TraceEvent): void {
    if (!this.enabled) return;
    try {
      this.link.sendDiagnostic({ kind: 'g2.diagnostic', event } satisfies UplinkMessage);
    } catch {
      // Diagnostics are strictly observational. A socket that closes between
      // MacLink's open-state check and send must never change HUD control flow.
    }
  }
}
