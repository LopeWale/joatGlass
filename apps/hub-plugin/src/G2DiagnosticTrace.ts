import type { MacLink } from './MacLink';
import type { PromptOwnerKind, UplinkMessage } from './types';

/**
 * The real-G2 diagnostic vocabulary. It deliberately has no text, URLs,
 * device identifiers, audio, token, or user-input payload fields. The Mac
 * independently validates this same vocabulary before it writes an export.
 */
export type G2TracePage = 'overview' | 'sidebar' | 'list' | 'confirmation' | 'navigation';
export type G2TraceCadence = 'first' | 'lte1s' | '1_5s' | '5_30s' | 'gt30s';
/** Fixed Messages surfaces only. The trace never receives list rows, sender
 * names, contact handles, draft text, or the confirmation prompt. */
export type G2TraceMessagesSurface =
  | 'picker' | 'conversation' | 'compose' | 'access' | 'setup' | 'retry'
  | 'empty' | 'read_only' | 'confirmation';
export type NavigationCue =
  | 'destination' | 'repair' | 'mode' | 'preview' | 'guidance'
  | 'uncertain' | 'wrong_way' | 'off_route' | 'rerouting' | 'arrived' | 'error'
  | 'stopped';

/** Common, already-bucketed phone location quality. No route position or
 * measured movement value leaves the glasses. */
type LocationQualityEvent = {
  sampleSequence: number;
  lifecycle: 'foreground' | 'background';
  /** Bit 0 timestamp, bit 1 speed, bit 2 heading. Presence only. */
  fields: number;
  cadence: G2TraceCadence;
  accuracy: 'unknown' | 'lte5' | 'lte10' | 'lte25' | 'gt25';
  timestampAge: 'missing' | 'future' | 'fresh' | 'stale';
  displacement: 'first' | 'lt5' | '5_20' | '20_100' | 'gt100';
  headingDelta: 'unavailable' | 'insufficient_motion' | '0_30' | '31_90' | '91_180';
};

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
    /** SDK call duration in whole ms. Excludes queueing, rendering, encoding
     * and pacing; success is not proof of visible delivery on the glasses. */
    durationMs: number;
    /** Encoded bytes handed to the bridge — a size, never a pixel. */
    bytes: number;
  }
  /**
   * Where one visual frame's time went. Durations and counts only: no text,
   * no pixels, no wearer or device identifier. `kind` names the screen so a
   * slow surface can be found without guessing which one it was.
   *
   * The production twin of the DEV-only `[hud-timing]` console line. BLE
   * transit exists only on real hardware, so the numbers that decide whether
   * a screen is worth moving off the image-tile path cannot be collected
   * from the dev server or the simulator — the two places that line prints.
   * Emitted only for frames that actually re-sent tiles: a clock tick
   * repaints without telling the wearer anything, and recording those floods
   * the bounded trace (see `completedVisualFrameReceipt` below).
   */
  | {
    type: 'frame_timing';
    frame: number;
    kind: 'brainstorm' | 'confirmation' | 'list' | 'messages'
      | 'navigation' | 'overview' | 'productivity' | 'prompt_question' | 'reader';
    /** Tiles actually re-sent. 0 means every region was already correct. */
    changedTiles: number;
    /** Waiting to start, behind an in-flight frame. */
    queueMs: number;
    /** Canvas draw. */
    renderMs: number;
    /** Greyscale/LZ4 preparation, parallel across tiles. */
    encodeMs: number;
    /** Slept in TILE_CALL_GAP_MS pacing — self-imposed, not the bridge. */
    pacingMs: number;
    /** Sum of the SDK upload calls. Never a pixel-visible receipt. */
    bridgeMs: number;
    /** Queue to last tile settled. */
    totalMs: number;
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
  | (LocationQualityEvent & {
    type: 'location_probe';
    profile: 'high';
  })
  /** Location quality of the actual navigation profile, rather than the
   * standalone Gate-0 probe. It exists only while Mac diagnostics are on. */
  | (LocationQualityEvent & { type: 'navigation_location' })
  | {
    type: 'location_probe_state';
    requested: 'high' | 'ambient';
    outcome: 'success' | 'failed';
  }
  /** HUD acknowledgement with no rendered copy, place, route, or pixels. The
   * Mac receipt supplies the timestamp used to synchronize lens video. */
  | {
    type: 'navigation_cue';
    cue: NavigationCue;
    outcome: 'success' | 'failed';
  }
  /** A physical-source receipt only after the Brainstorm controller accepted
   * its fixed, prompt-free action. */
  | {
    type: 'brainstorm_action';
    action: 'begin_topic_capture' | 'begin_terminal_input' | 'show_terminal' | 'show_brainstorm' | 'cancel' | 'retry_terminal';
    surface: 'brainstorm' | 'agent_terminal';
    source: 'left' | 'right' | 'ring';
    gesture: 'tap' | 'double_press';
    delivery: 'sys';
  }
  /** A completed four-tile Messages state, bound only to its fixed surface
   * name and raster digest. The digest is not reversible HUD copy. */
  | {
    type: 'visual_frame';
    kind: 'messages';
    surface: G2TraceMessagesSurface;
    sha256: string;
  }
  /** Outcome after the already-tagged Messages confirmation gate settles.
   * It distinguishes explicit decline from silence without preserving a
   * message body or recipient. */
  | {
    type: 'messages_confirmation';
    outcome: 'accepted' | 'declined' | 'timed_out';
  }
  /** Where a spoken answer's question stopped on the glasses (a Messages
   * reply, a Brainstorm topic, a terminal reply, a page row's question):
   * which owner, and the step reached. No question text, no words. */
  | {
    type: 'prompt_stage';
    owner: PromptOwnerKind;
    stage: PromptStage;
  };

/** The steps a spoken answer's question takes on the glasses. */
export type PromptStage =
  | 'presented' | 'painted' | 'paint_refused' | 'mic_ready' | 'mic_refused'
  | 'acknowledged' | 'ack_refused' | 'retired';

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
