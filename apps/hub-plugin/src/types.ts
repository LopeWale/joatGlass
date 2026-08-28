/**
 * Shared types for the Hub plugin ⇄ Mac brain protocol.
 * Keep this file in sync with apps/mac/joat/core (the Python mirror).
 */

/** Container IDs — stable across rebuilds so textContainerUpgrade can target them. */
export const ContainerId = {
  StatusBar: 1,
  Main: 2,
  List: 3,
  Image: 4,
  // Dashboard regions (docs/HUD_DASHBOARD.md) — mirrored in protocol.py.
  Context: 5,
  Tray: 6,
  Hint: 7,
  VisualTopRight: 8,
  VisualBottomLeft: 9,
  VisualBottomRight: 10,
} as const;
export type ContainerId = (typeof ContainerId)[keyof typeof ContainerId];

/** Canvas constants from the SDK. Never hardcode these elsewhere. */
export const CANVAS = { width: 576, height: 288, greyLevels: 16 } as const;
/** JavaScript UTF-16 code units. Longer previews must fail, never truncate. */
export const CONFIRM_PROMPT_MAX_UNITS = 120;

/**
 * How listening resumes after the wearer puts the glasses back on.
 * Doff always stops the mic locally (fail-safe); this only governs resume.
 * Wearer-configurable on the G2 (left-temple double-tap → settings list).
 */
export type ResumePolicy = 'auto' | 'mac' | 'confirm';
export type HudContentKind = 'assistant' | 'notification' | 'calendar' | 'system';
export type G2DiagnosticEvent = import('./G2DiagnosticTrace').G2TraceEvent;

/** Messages sent plugin → Mac. */
export type UplinkMessage =
  /** First application frame on every socket. Credentials never belong in a URL. */
  | { kind: 'auth'; token: string | null }
  | { kind: 'hello'; sn: string; model: string; launchSource: string; userName?: string }
  | { kind: 'audio'; source: 'glasses' | 'phone'; seq: number; pcm: number[]; speaker?: string }
  | { kind: 'imu'; x: number; y: number; z: number; at: number }
  | { kind: 'input'; event: InputEventPayload }
  | { kind: 'device'; battery?: number; wearing?: boolean; charging?: boolean; connect: string; mic?: boolean }
  | { kind: 'confirm'; actionId: string; accepted: boolean; challengeId?: string; sessionId?: string; payloadDigest?: string; previewDigest?: string }
  | { kind: 'policy'; resume: ResumePolicy }
  /** Wearer position via bridge.getAppLocation — the sideload-safe API. */
  | { kind: 'location'; latitude: number; longitude: number; accuracy?: number }
  /** Confirms a freshly issued pairing token was PERSISTED (not merely received). */
  | { kind: 'pairing'; token: string }
  /** Opt-in and content-free; the Mac validates this allowlisted schema again. */
  | { kind: 'g2.diagnostic'; event: G2DiagnosticEvent };

/** Messages sent Mac → plugin. */
export type DownlinkMessage =
  /** The socket is usable only after this frame. */
  | { kind: 'auth.accepted' }
  | { kind: 'hud.text'; containerId: ContainerId; text: string; offset?: number; length?: number; append?: boolean; textColor?: number; contentKind?: HudContentKind; traceId?: string }
  | { kind: 'hud.rebuild'; layout: 'default' | 'list' | 'image' }
  | { kind: 'hud.list'; items: string[]; title?: string; hint?: string }
  | { kind: 'audio.control'; open: boolean; source?: 'glasses' | 'phone' }
  | { kind: 'imu.control'; open: boolean; paceMs?: number }
  /** Calibration only: relay every raw IMU sample uplink (~10/s at P100). */
  | { kind: 'imu.trace'; enabled: boolean }
  | { kind: 'confirm.request'; actionId: string; prompt: string; challengeId: string; sessionId: string; payloadDigest: string; previewDigest: string; expiresAtMs: number }
  | { kind: 'policy.set'; resume: ResumePolicy }
  /** Explicit-window pairing: issued only after an auth frame with a null token. */
  | { kind: 'pairing.set'; token: string }
  /** The Mac shell enables this only for an explicit support capture. */
  | { kind: 'g2.diagnostic.control'; enabled: boolean }
  | { kind: 'shutdown'; exitMode?: 0 | 1 };

export interface InputEventPayload {
  containerId?: number;
  containerName?: string;
  eventType?: number;
  /** EventSourceType: 1 = right temple, 2 = R1 ring, 3 = left temple */
  eventSource?: number;
  selectedIndex?: number;
  selectedName?: string;
  /** Privacy-safe action correlation. Never derived from message content. */
  traceId?: string;
}

export type Unsubscribe = () => void;
