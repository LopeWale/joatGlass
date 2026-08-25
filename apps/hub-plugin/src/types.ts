/**
 * Shared types for the Hub plugin ⇄ Mac brain protocol.
 * Keep this file in sync with apps/mac/jarvis/core (the Python mirror).
 */

/** Container IDs — stable across rebuilds so textContainerUpgrade can target them. */
export const ContainerId = {
  StatusBar: 1,
  Main: 2,
  List: 3,
  Image: 4,
} as const;
export type ContainerId = (typeof ContainerId)[keyof typeof ContainerId];

/** Canvas constants from the SDK. Never hardcode these elsewhere. */
export const CANVAS = { width: 576, height: 288, greyLevels: 16 } as const;

/**
 * Confirmation prompt hint. Lives here, not in main.ts, so DeviceMonitor can
 * use it without a circular import.
 *
 * Names the right temple rather than the ring: the ring is an optional
 * accessory, the temple is always present. Both resolve the same confirm, and
 * the nod is listed last because its thresholds are still uncalibrated.
 */
export const CONFIRM_HINT = 'Tap the right temple (or nod) to confirm.';

/**
 * How listening resumes after the wearer puts the glasses back on.
 * Doff always stops the mic locally (fail-safe); this only governs resume.
 * Wearer-configurable on the G2 (left-temple double-tap → settings list).
 */
export type ResumePolicy = 'auto' | 'mac' | 'confirm';

/** Messages sent plugin → Mac. */
export type UplinkMessage =
  | { kind: 'hello'; sn: string; model: string; launchSource: string }
  | { kind: 'audio'; source: 'glasses' | 'phone'; seq: number; pcm: number[] }
  | { kind: 'imu'; x: number; y: number; z: number; at: number }
  | { kind: 'input'; event: InputEventPayload }
  | { kind: 'device'; battery?: number; wearing?: boolean; charging?: boolean; connect: string }
  | { kind: 'confirm'; actionId: string; accepted: boolean }
  | { kind: 'policy'; resume: ResumePolicy };

/** Messages sent Mac → plugin. */
export type DownlinkMessage =
  | { kind: 'hud.text'; containerId: ContainerId; text: string; offset?: number; length?: number; append?: boolean }
  | { kind: 'hud.rebuild'; layout: 'default' | 'list' | 'image' }
  | { kind: 'hud.list'; items: string[] }
  | { kind: 'audio.control'; open: boolean; source?: 'glasses' | 'phone' }
  | { kind: 'imu.control'; open: boolean; paceMs?: number }
  /** Calibration only: relay every raw IMU sample uplink (~10/s at P100). */
  | { kind: 'imu.trace'; enabled: boolean }
  | { kind: 'confirm.request'; actionId: string; prompt: string }
  | { kind: 'policy.set'; resume: ResumePolicy }
  | { kind: 'shutdown'; exitMode?: 0 | 1 };

export interface InputEventPayload {
  containerId?: number;
  containerName?: string;
  eventType?: number;
  /** EventSourceType: 1 = right temple, 2 = R1 ring, 3 = left temple */
  eventSource?: number;
  selectedIndex?: number;
  selectedName?: string;
}

export type Unsubscribe = () => void;
