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
export type NavigationHudModel = import('./NavigationHud').NavigationHudModel;
export type NavigationRoutePlan = import('./NavigationProgress').NavigationRoutePlan;
/** Safe projection of the live Brainstorm / Hermes terminal state. */
export type BrainstormHudScreen = 'brainstorm' | 'agent_terminal';
export type BrainstormHudState =
  | 'unavailable'
  | 'approval_required'
  | 'ready'
  | 'running'
  | 'settled'
  | 'blocked';
export interface BrainstormHudModel {
  screen: BrainstormHudScreen;
  state: BrainstormHudState;
  topic: string;
  collaborators: string[];
  activeSpeaker?: string;
  activity: string;
  endpointHost?: string;
  terminalState: BrainstormHudState;
  terminalReason?: string;
  /** Local preflight only; this is never an atomic remote guarantee. */
  noToolsChecked: boolean;
}

/** A bounded lifecycle page for native Calendar and Reminders actions.
 * The interactive preview remains a separate `confirm.request` frame; this
 * state is only what the native bridge is doing or established afterward. */
export type ProductivityHudDomain = 'calendar' | 'reminders';
export type ProductivityHudPhase =
  | 'verifying'
  | 'saved'
  | 'completed'
  | 'failed'
  | 'check'
  | 'cancelled'
  | 'not_saved'
  | 'due';

export interface ProductivityHudModel {
  domain: ProductivityHudDomain;
  phase: ProductivityHudPhase;
  title: string;
  primary: string;
  secondary: string;
  hint: string;
}
export type NavigationAction =
  | 'confirm_destination'
  | 'retry_destination'
  | 'select_destination'
  | 'select_mode'
  | 'start'
  | 'change_mode'
  | 'change_destination'
  | 'finish'
  | 'retry'
  | 'cancel';

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
  | { kind: 'g2.diagnostic'; event: G2DiagnosticEvent }
  | {
    kind: 'navigation.action';
    sessionId: string;
    requestId: string;
    generation: number;
    action: NavigationAction;
    choiceId?: string;
  }
  | {
    kind: 'navigation.route.ack';
    sessionId: string;
    routeId: string;
    revision: number;
    outcome: 'installed' | 'rejected';
  }
  | {
    kind: 'navigation.view.ack';
    sessionId: string;
    requestId: string;
    generation: number;
    outcome: 'bridge_acknowledged' | 'rejected';
  }
  | {
    kind: 'navigation.state';
    sessionId: string;
    routeId: string;
    revision: number;
    sequence: number;
    state: 'guidance' | 'uncertain' | 'wrong_way' | 'off_route' | 'arrived';
  }
  | {
    kind: 'navigation.reroute.request';
    sessionId: string;
    routeId: string;
    baseRevision: number;
    requestId: string;
    latitude: number;
    longitude: number;
  }
  | {
    kind: 'navigation.origin';
    sessionId: string;
    requestId: string;
    generation: number;
    latitude: number;
    longitude: number;
    accuracy?: number;
  }
  | {
    kind: 'navigation.origin.failed';
    sessionId: string;
    requestId: string;
    generation: number;
    reason: 'unavailable';
  };

/** Messages sent Mac → plugin. */
export type DownlinkMessage =
  /** The socket is usable only after this frame. */
  | { kind: 'auth.accepted' }
  | { kind: 'hud.text'; containerId: ContainerId; text: string; offset?: number; length?: number; append?: boolean; textColor?: number; contentKind?: HudContentKind; traceId?: string }
  | { kind: 'hud.rebuild'; layout: 'default' | 'list' | 'image'; reason?: 'brainstorm' }
  | { kind: 'hud.list'; items: string[]; title?: string; hint?: string; requestId?: string; selectable?: boolean }
  | { kind: 'brainstorm.view'; brainstorm: BrainstormHudModel }
  | { kind: 'productivity.view'; productivity: ProductivityHudModel }
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
  /** Gate-0 only: switch the one phone-location stream to/from high accuracy. */
  | { kind: 'location.probe.control'; enabled: boolean }
  | {
    kind: 'navigation.view';
    sessionId: string;
    requestId: string;
    generation: number;
    navigation: NavigationHudModel;
  }
  | {
    kind: 'navigation.origin.request';
    sessionId: string;
    requestId: string;
    generation: number;
  }
  | {
    kind: 'navigation.route';
    sessionId: string;
    requestId: string;
    generation: number;
    routeId: string;
    revision: number;
    destinationLabel: string;
    etaMinutes: number;
    plan: NavigationRoutePlan;
  }
  | { kind: 'navigation.stop'; sessionId: string }
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
  /** Binds a row selection to the exact list generation that was rendered. */
  requestId?: string;
}

export type Unsubscribe = () => void;
