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

/** Image updates must identify the same named visual container that the
 * layout declared. Keeping the mapping beside stable IDs prevents a bridge
 * acknowledgement from being mistaken for a visible tile when a host routes
 * raw pixels by name. */
export const VisualContainerName: Readonly<Record<number, string>> = {
  [ContainerId.Image]: 'visual-tl',
  [ContainerId.VisualTopRight]: 'visual-tr',
  [ContainerId.VisualBottomLeft]: 'visual-bl',
  [ContainerId.VisualBottomRight]: 'visual-br',
};

/** Canvas constants from the SDK. Never hardcode these elsewhere. */
export const CANVAS = { width: 576, height: 288, greyLevels: 16 } as const;

/** Request ids the Mac mints for voice-initiated surfaces. A list arriving
 * with this prefix is adopted as a fresh Mac-initiated generation instead of
 * being dropped as stale. Mac counterpart: VOICE_REQUEST_PREFIX in
 * apps/mac/joat/core/protocol.py. */
export const VOICE_REQUEST_PREFIX = 'voice-';
/** JavaScript UTF-16 code units. Longer previews must fail, never truncate. */
export const CONFIRM_PROMPT_MAX_UNITS = 120;

/**
 * How listening resumes after the wearer puts the glasses back on.
 * Doff always stops the mic locally (fail-safe); this only governs resume.
 * Wearer-configurable on the G2 (left-temple double-tap → settings list).
 */
export type ResumePolicy = 'auto' | 'mac' | 'confirm';
export type HudContentKind = 'assistant' | 'notification' | 'calendar' | 'system';
/** A transient JOAT response is never a Messages, Brainstorm, or Hermes page. */
export type AssistantOverlayOperation = 'replace' | 'append' | 'settle' | 'dismiss';
export type G2DiagnosticEvent = import('./G2DiagnosticTrace').G2TraceEvent;
export type NavigationHudModel = import('./NavigationHud').NavigationHudModel;
export type NavigationRoutePlan = import('./NavigationProgress').NavigationRoutePlan;
/** Safe projection of the live Brainstorm / Hermes terminal state. */
/** Which surface a diagnostic gesture came from. This is receipt metadata
 * ONLY — it is no longer a frame discriminator, because the two surfaces no
 * longer share a frame (`docs/SESSION_MODEL.md:74-75`). */
export type BrainstormHudScreen = 'brainstorm' | 'agent_terminal';

export type BrainstormHudState =
  | 'unavailable'
  | 'approval_required'
  | 'ready'
  | 'topic_capture'
  | 'acceptance_ready'
  | 'running'
  | 'settled'
  | 'cancelled'
  | 'failed'
  | 'blocked';
/** The Brainstorm conversation. Deliberately carries NO Agent Terminal,
 * Hermes, OpenClaw, endpoint, approval or tool-policy field —
 * `docs/SESSION_MODEL.md:74-75` states that as a requirement, and the two
 * surfaces shared one frame in violation of it until this split.
 *
 * That violation was not academic. It is why the Agent Terminal printed
 * "JOAT · BRAINSTORM" across its spine, why a healthy terminal spent four
 * rows on connection chrome, and why a tap on the terminal was gated on the
 * BRAINSTORM lane's readiness. One frame, two features, three bugs. */
export interface BrainstormHudModel {
  /** WHICH of the two surfaces this frame is. Brainstorm (Codex proposes,
   * Claude critiques) and the Agent Terminal (Hermes and OpenClaw work) are
   * two features that share a frame shape, not one screen with two moods.
   * The renderer reads this to title the spine; without it the spine fell
   * through to the link status and the Terminal was captured reading
   * "JOAT · BRAINSTORM", which is exactly the confusion they are kept
   * apart to prevent. This is a RENDER-side discriminator: the wire still
   * carries `brainstorm.view` and `agent_terminal.view` separately. */
  screen: 'brainstorm' | 'agent_terminal';
  state: BrainstormHudState;
  topic: string;
  collaborators: string[];
  activeSpeaker?: string;
  activity: string;
  /** Where the dedicated session lives, shown so the wearer can tell a
   * local session from a remote one without leaving the surface. */
  endpointHost?: string;
  /** Session readiness, which is not the same as `state`: the surface can be
   * running while the session behind it is blocked. */
  terminalState?: BrainstormHudState;
  terminalReason?: string;
  /** Whether the no-tools policy was verified for this session. */
  noToolsChecked?: boolean;
  /** Default-off Mac diagnostics arm. Only this explicit state may expose the
   * isolated G2 acceptance gesture; ordinary Brainstorm never does. */
  acceptanceArmed?: boolean;
}

/** The Agent Terminal: Hermes and OpenClaw work sessions. Its own frame,
 * with the connection and tool-policy facts that belong to it and to no
 * other surface. */
export interface AgentTerminalHudModel {
  state: BrainstormHudState;
  /** The focused lane, in the roster's "<lane> · <state>" shape. */
  focus: string;
  /** Lane chips: every concurrent session, so one glance covers the roster. */
  lanes: string[];
  activity: string;
  endpointHost?: string;
  terminalReason?: string;
  /** Local preflight only; this is never an atomic remote guarantee. */
  noToolsChecked: boolean;
}

/**
 * Bounded wearer intents while the live Brainstorm surface owns the HUD.
 * These are deliberately not free-form prompts: spoken language remains the
 * sole topic source, while gestures only control the local session lifecycle.
 */
export type BrainstormAction =
  | 'begin_acceptance'
  | 'begin_topic_capture'
  /** Arm ONE utterance as input to the focused Agent Terminal session.
   * Distinct from begin_topic_capture: that starts a Brainstorm, which is
   * a different feature with different collaborators. */
  | 'begin_terminal_input'
  | 'show_terminal'
  | 'show_brainstorm'
  | 'cancel'
  | 'retry_terminal'
  /** Answer the tool request the focused work session is blocked on.
   * Voice ("terminal approve") was the only way, so a wearer looking at a
   * blocked session had no gesture for the decision in front of them. */
  | 'approve_terminal'
  | 'deny_terminal';

/** A bounded lifecycle page for native Calendar and Reminders actions.
 * The interactive preview remains a separate `confirm.request` frame; this
 * state is only what the native bridge is doing or established afterward. */
export type ProductivityHudDomain = 'calendar' | 'reminders';

/** A redacted source-backed count for one fixed Planning month cell. Labels,
 * native identifiers, and times stay in the Week/Day drill-down payload. */
/** One entry inside a Planning day, for the dashboard's right pane. */
export interface PlanningGridEntry {
  /** Local clock label the Mac already formatted ("16:30", "ALL DAY"). */
  time: string;
  kind: 'event' | 'reminder';
  title: string;
}

export interface PlanningGridCell {
  day: string;
  eventCount: number;
  reminderCount: number;
  inMonth: boolean;
  /**
   * The day's entries, so hovering a date can repaint the right pane from
   * local state. Asking the Mac per hover would put a round trip on the
   * serialized BLE lane for every scroll tick; the month arrives once over
   * the websocket instead, and only the changed tiles are transferred.
   */
  entries?: PlanningGridEntry[];
}
export type ProductivityHudPhase =
  | 'verifying'
  | 'saved'
  /** A persisted JOAT-created reminder is awaiting its native EventKit alarm. */
  | 'monitoring'
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
  /** Content-free instant that returns an outcome to its native Planning day. */
  planningAnchorUnixMs?: number;
  /** Set only after a Reminder saved independently and JOAT persisted its
   * bounded monitor occurrence. The Hub uses this to present a truthful
   * monitoring page after the readable Saved receipt. */
  monitoringArmed?: boolean;
  /** Opaque local occurrence UUID. Present only on a due view that requests
   * a generation-bound plugin acceptance receipt; never Reminder content. */
  reminderDueId?: string;
  /** Fresh gateway-generated attempt UUID. It prevents a delayed receipt
   * from satisfying a later retry for the same occurrence. */
  reminderDueAttemptId?: string;
  /** Mac gateway connection generation paired with reminderDueId. */
  reminderDueGeneration?: number;
  /** Simulator-only presentation receipt binding. It is supplied only for a
   * verifying write surface and contains no Calendar or Reminder content. */
  presentationReceiptId?: string;
  /** Current gateway generation paired with presentationReceiptId. */
  presentationReceiptGeneration?: number;
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
  | {
    kind: 'location'; latitude: number; longitude: number; accuracy?: number;
    /** Optional phone fix timestamp; the Mac combines it with receive time for expiry. */
    timestamp?: number;
    /** Present only when replying to a one-shot location.refresh request. */
    refreshId?: string;
  }
  /** Confirms a freshly issued pairing token was PERSISTED (not merely received). */
  | { kind: 'pairing'; token: string }
  /** Opt-in and content-free; the Mac validates this allowlisted schema again. */
  | { kind: 'g2.diagnostic'; event: G2DiagnosticEvent }
  | {
    kind: 'brainstorm.action';
    action: Exclude<BrainstormAction, 'begin_acceptance'>;
  }
  | {
    kind: 'brainstorm.action';
    action: 'begin_acceptance';
    surface: 'brainstorm';
    source: 'right' | 'ring';
    gesture: 'double_press';
  }
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
  }
  /** The plugin accepted the typed due surface. This is not wearer attention. */
  | {
    kind: 'reminder_due.ack';
    reminderDueId: string;
    reminderDueAttemptId: string;
    generation: number;
    outcome: 'accepted';
  }
  /** Current-client acceptance of a typed verifying surface. This is not
   * native EventKit delivery, physical G2 pixels, or wearer attention. */
  | {
    kind: 'productivity.view.ack';
    presentationReceiptId: string;
    generation: number;
    outcome: 'accepted';
  };

/** Messages sent Mac → plugin. */
export type DownlinkMessage =
  /** The socket is usable only after this frame. */
  | { kind: 'auth.accepted' }
  | { kind: 'hud.text'; containerId: ContainerId; text: string; offset?: number; length?: number; append?: boolean; textColor?: number; contentKind?: HudContentKind; traceId?: string }
  | {
    kind: 'assistant.overlay';
    operation: AssistantOverlayOperation;
    text?: string;
    dismissAfterMs?: number;
  }
  | { kind: 'hud.rebuild'; layout: 'default' | 'list' | 'image'; reason?: 'brainstorm' }
  | {
    kind: 'hud.list';
    items: string[];
    title?: string;
    hint?: string;
    requestId?: string;
    selectable?: boolean;
    planningGrid?: PlanningGridCell[];
  }
  | { kind: 'brainstorm.view'; brainstorm: BrainstormHudModel }
  | { kind: 'agent_terminal.view'; terminal: AgentTerminalHudModel }
  | { kind: 'productivity.view'; productivity: ProductivityHudModel }
  | { kind: 'audio.control'; open: boolean; source?: 'glasses' | 'phone' }
  | { kind: 'imu.control'; open: boolean; paceMs?: number }
  /** Calibration only: relay every raw IMU sample uplink (~10/s at P100). */
  | { kind: 'imu.trace'; enabled: boolean }
  | { kind: 'confirm.request'; actionId: string; prompt: string; challengeId: string; sessionId: string; payloadDigest: string; previewDigest: string; expiresAtMs: number; diagnosticKind?: 'messages' }
  | { kind: 'policy.set'; resume: ResumePolicy }
  /** Explicit-window pairing: issued only after an auth frame with a null token. */
  | { kind: 'pairing.set'; token: string }
  /** The Mac shell enables this only for an explicit support capture. */
  | { kind: 'g2.diagnostic.control'; enabled: boolean }
  /** Gate-0 only: switch the one phone-location stream to/from high accuracy. */
  | { kind: 'location.probe.control'; enabled: boolean }
  /** Assistant-only one-shot phone fix; it does not alter the location stream profile. */
  | { kind: 'location.refresh'; requestId: string }
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
  | {
    kind: 'navigation.map';
    sessionId: string;
    latitude: number;
    longitude: number;
    zoom: number;
    width: number;
    height: number;
    /** base64 PNG rendered on the Mac; the access token never travels. */
    png: string;
  }
  | {
    kind: 'hud.ambient';
    /** e.g. "72f" — already formatted by the Mac, which owns the unit. */
    temperature: string;
    /** One short sky word: clear, rain, fog… Empty when unknown. */
    condition: string;
    unread: number;
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
  /** Content-free instant supplied only when returning from a productivity outcome. */
  planningAnchorUnixMs?: number;
}

export type Unsubscribe = () => void;

/**
 * The wearer's one menu.
 *
 * It lives here, not in CapabilityMenu, because the firmware contextual menu
 * (`menuObject`) and the plugin's own router must offer exactly the same rows.
 * Two menus with two lists is how the wearer met a menu they could not reach.
 *
 * Notifications is deliberately absent: the resting dashboard already shows
 * the cards themselves, so a row that opens a page to re-read the home screen
 * is a page, a request, and a round trip spent on nothing (the wearer's ruling).
 */
export const CAPABILITIES = [
  'Messages', 'Planning', 'Navigation', 'Brainstorm',
  'QuickNotes', 'Agent Terminal', 'Activity', 'Settings',
] as const;

export type CapabilityName = (typeof CAPABILITIES)[number];

/** Firmware menu ids for the rows above; item 0 is reserved by the protocol,
 * and 1-5 are the retired internal controls, so capabilities start at 10. */
export const CAPABILITY_MENU_ID_BASE = 10;
