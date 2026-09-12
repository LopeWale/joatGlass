/**
 * Shared types for the Hub plugin ⇄ Mac brain protocol.
 * Keep this file in sync with apps/mac/joat/core (the Python mirror).
 */

/** RFC 6455 private-use close code: another authenticated display took over. */
export const PLUGIN_REPLACED_CLOSE_CODE = 4409;

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

/**
 * Where the spine's state word lives, DRAWN BY THE CANVAS into the top-left
 * tile (HudCanvasRenderer.drawShell) — keeping the changing label inside one
 * tile means an isolated status update dirties one tile, not both halves of
 * the HUD. The box sits east of the 12 px monospace clock (x=30, up to nine
 * glyphs ≈ 95 px) and ends exactly ON the tile seam.
 *
 * The width is measured, not guessed: @evenrealities/pretext puts the
 * longest words this box must hold at 185 px ("JOAT · HEARING YOU…") and
 * 188 px ("JOAT · RECONNECTING"), so 192 px is the smallest box that keeps
 * them on ONE line. At the old 180 px both wrapped and were truncated to
 * "…HEARING YO...". drawShell reads x and width from HERE — the 2026-09-05
 * audit found it hardcoding 100/184, under the measured minimum.
 *
 * A native-container twin of this path (container 11 + textContainerUpgrade)
 * was removed the same day: no layout ever declared the container and no
 * production caller supplied the label, so the write could never succeed —
 * and a second writer for the same word is a collision, not a fallback.
 */
export const SPINE_LABEL_BOX = { x: 95, y: 7, width: 192, height: 27 } as const;

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
export type AssistantConversationState = 'thinking' | 'answering' | 'ready';
export interface AssistantConversationTurn {
  role: 'user' | 'assistant';
  text: string;
}
export interface AssistantConversationSnapshot {
  conversationId: string;
  revision: number;
  state: AssistantConversationState;
  turns: AssistantConversationTurn[];
  continueAfterMs: number;
  earlierTurnsOmitted: boolean;
}
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
/** The `brainstorm.view` WIRE payload — exactly the six fields
 * `core/protocol.py`'s BrainstormHudView.to_wire() emits, and nothing
 * else. docs/SESSION_MODEL.md:84-88: the Brainstorm frame "contains no
 * Agent Terminal, Hermes, OpenClaw, endpoint, approval, or tool-policy
 * fields." The engine held that; THIS file still declared the payload as
 * the render-side union below, so the plugin's half of the duplicated
 * protocol claimed fields the wire forbids — and the two protocol files
 * disagreed about the frame they both describe. */
export interface BrainstormViewPayload {
  state: BrainstormHudState;
  topic: string;
  collaborators: string[];
  activeSpeaker?: string;
  activity: string;
  /** Default-off Mac diagnostics arm — a Brainstorm fact, not a terminal
   * one: it gates the isolated G2 acceptance gesture. */
  acceptanceArmed?: boolean;
}

export interface TerminalConversationMessage {
  id: string;
  role: 'user' | 'assistant' | 'status';
  text: string;
}

export interface TerminalConversationFields {
  conversationId?: string;
  messages?: TerminalConversationMessage[];
  historyTruncated?: boolean;
  inputRequired?: boolean;
  taskRecovery?: boolean;
}

/** RENDER model, not a wire frame: the union one renderer draws. Built in
 * main.ts from whichever of the two wire payloads arrived, plus the
 * `screen` discriminator that exists only on this side. */
export interface BrainstormHudModel extends TerminalConversationFields {
  /** Local exact prompt publication, never part of the terminal wire frame. */
  terminalPrompt?: { id: string; question: string };
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
  terminalReason?: string;
  /** Whether the no-tools policy was verified for this session. */
  noToolsChecked?: boolean;
  /** Tool requests the focused lane is blocked on — see
   * AgentTerminalHudModel. A connection awaiting device approval is a
   * different problem with the opposite remedy, so the tap keys on this. */
  approvals?: number;
  /** Default-off Mac diagnostics arm. Only this explicit state may expose the
   * isolated G2 acceptance gesture; ordinary Brainstorm never does. */
  acceptanceArmed?: boolean;
}

/** The Agent Terminal: Hermes and OpenClaw work sessions. Its own frame,
 * with the connection and tool-policy facts that belong to it and to no
 * other surface. */
export interface AgentTerminalHudModel extends TerminalConversationFields {
  /** Opaque visit and row-selection identity; both are present together. */
  openingRequestId?: string;
  openingSelectionId?: string;
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
  /** Tool requests the focused lane is blocked on.
   *
   * Distinct from `state === 'approval_required'`, which also describes a
   * CONNECTION whose device has not been approved in OpenClaw — a
   * different problem whose remedy is retry, not approve. A tap needs to
   * know which, and only this count says so. */
  approvals?: number;
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

/** Exact application question; distinct from an ordinary assistant reply. */
export interface PromptPresentMessage {
  kind: 'prompt.present';
  promptId: string;
  question: string;
  presentationBinding: string;
  /** Opaque Mac audio-session identity, not the local MacLink generation. */
  connectionGeneration: string;
  source: 'glasses' | 'phone';
  expiresInMs: number;
  ownerKind: 'messages' | 'brainstorm' | 'terminal';
  ownerId: string;
  field: string;
}

/** A Messages picker action, bound to the exact local visit and row tap. */
export interface MessageUIMessage {
  kind: 'message.ui';
  requestId: string;
  selectionId: string;
  actionId: string;
  state: 'preparing' | 'awaiting_input' | 'awaiting_confirmation' | 'sending'
    | 'cancelled' | 'sent' | 'failed' | 'uncertain';
  text: string;
}

/** Either side can revoke precisely this question on its original session. */
export interface PromptCancelMessage {
  kind: 'prompt.cancel';
  promptId: string;
  connectionGeneration: string;
}

export interface PromptAckMessage {
  kind: 'prompt.ack';
  promptId: string;
  presentationBinding: string;
  connectionGeneration: string;
  source: 'glasses' | 'phone';
  success: boolean;
  audioEpoch?: string | null;
}

/** Messages sent plugin → Mac. */
export type UplinkMessage =
  | PromptAckMessage
  | PromptCancelMessage
  /** First application frame on every socket. Credentials never belong in a URL. */
  | { kind: 'auth'; token: string | null }
  | {
    kind: 'hello'; sn: string; model: string; launchSource: string; userName?: string;
    /** The PHONE's IANA zone. The Mac formats every time the wearer reads,
     * and its own zone is only right by coincidence — see phoneTimeZone. */
    timeZone?: string;
  }
  | { kind: 'audio'; source: 'glasses' | 'phone'; seq: number; pcm: number[]; speaker?: string; audioEpoch?: string }
  | { kind: 'imu'; x: number; y: number; z: number; at: number }
  | { kind: 'input'; event: InputEventPayload }
  | { kind: 'device'; battery?: number; wearing?: boolean; charging?: boolean; connect: string; mic?: boolean; micSource?: 'glasses' | 'phone'; micPromptId?: string }
  | { kind: 'confirm'; actionId: string; accepted: boolean; challengeId?: string; sessionId?: string; payloadDigest?: string; previewDigest?: string }
  | { kind: 'policy'; resume: ResumePolicy }
  /** Wearer position via bridge.getAppLocation — the sideload-safe API. */
  | {
    kind: 'location'; latitude: number; longitude: number; accuracy?: number;
    /** Optional phone fix timestamp; the Mac combines it with receive time for expiry. */
    timestamp?: number;
    /** Present only when replying to a one-shot location.refresh request. */
    refreshId?: string;
    /** The phone's IANA zone, resent with every fix so LANDING somewhere
     * corrects the HUD without waiting for a reconnect. Same value as the
     * one on `hello`; this is the self-healing path, not a second source. */
    timeZone?: string;
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
  | PromptPresentMessage
  | MessageUIMessage
  | PromptCancelMessage
  /** The socket is usable only after this frame. */
  | { kind: 'auth.accepted' }
  | { kind: 'hud.text'; containerId: ContainerId; text: string; offset?: number; length?: number; append?: boolean; textColor?: number; contentKind?: HudContentKind; traceId?: string }
  | {
    kind: 'assistant.overlay';
    operation: AssistantOverlayOperation;
    text?: string;
    dismissAfterMs?: number;
    /** Explicit visual meaning; older Mac brains omit it and default to an answer. */
    presentation?: 'answer' | 'receipt';
    /** This overlay ANSWERS something the wearer just asked on the surface
     * they are looking at, so the Brainstorm refusal does not apply. That
     * refusal exists to stop unrelated queued chatter stamping itself over
     * a synthesis — not to silence the reply to an instruction. */
    answersWearer?: boolean;
  }
  | ({ kind: 'assistant.conversation'; operation: 'replace' } & AssistantConversationSnapshot)
  | {
    kind: 'assistant.conversation';
    operation: 'dismiss';
    conversationId: string;
    revision: number;
  }
  | { kind: 'hud.rebuild'; layout: 'default' | 'list' | 'image'; reason?: 'brainstorm' }
  | {
    kind: 'hud.list';
    /** Opaque reader visit and source-page cursor, separate from reply ownership. */
    readerPage?: { id: string; index: number; count: number };
    /** Mac-owned archive page identity; note IDs never cross this boundary. */
    quickNotesPage?: { index: number; count: number };
    items: string[];
    /** Display context only, independent of routing and private action targets. */
    contextLabel?: string;
    /** First navigation/view control in the unchanged Mac-owned row array. */
    controlStartIndex?: number;
    planningEntries?: Array<{ time: string; kind: "event" | "reminder"; title: string } | null>;
    title?: string;
    hint?: string;
    requestId?: string;
    selectable?: boolean;
    planningGrid?: PlanningGridCell[];
    planningNavigation?: { canGoBack: boolean; selectedIndex: number };
  }
  | { kind: 'brainstorm.view'; brainstorm: BrainstormViewPayload }
  | { kind: 'agent_terminal.view'; terminal: AgentTerminalHudModel }
  | { kind: 'productivity.view'; productivity: ProductivityHudModel }
  | { kind: 'audio.control'; open: boolean; source?: 'glasses' | 'phone' }
  | { kind: 'imu.control'; open: boolean; paceMs?: number }
  /** Calibration only: relay every raw IMU sample uplink (~10/s at P100). */
  | { kind: 'imu.trace'; enabled: boolean }
  | { kind: 'confirm.request'; actionId: string; prompt: string; challengeId: string; sessionId: string; payloadDigest: string; previewDigest: string; expiresAtMs: number; diagnosticKind?: 'messages'; messageUIActionId?: string }
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
  /**
   * One tab of the overview's right panel, pushed proactively like
   * `hud.ambient` so a swipe between tabs never costs a Mac round trip. The
   * plugin caches the newest push per tab; `revision` is echoed back in every
   * row tap (see `panelRequestId`) so a tap after a refresh cannot open the
   * wrong story. Notifications is not a panel push: that tab reads the
   * Main/Context/Tray streams the overview already carries.
   */
  | {
    kind: 'hud.panel';
    tab: PanelContentTab;
    revision: number;
    /** Dense-row grammar, "[age] primary · secondary"; ≤20 rows × ≤64 chars. */
    rows: string[];
    /** One quiet line above the rows: the search query, or feed freshness. */
    headline?: string;
    /** What the pane says when there are no rows yet. */
    empty?: string;
    pomodoro?: HudPanelPomodoro;
    /** A voice-initiated open ("show the news"): switch to and expand this tab. */
    activate?: boolean;
  }
  | { kind: 'navigation.stop'; sessionId: string }
  | { kind: 'shutdown'; exitMode?: 0 | 1 };

export interface InputEventPayload {
  quickNotesPageIndex?: number;
  /** Exact civil date from the Mac's current Planning grid. */
  planningDay?: string;
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
  /** Binds Continue/Exit to the exact assistant transcript the wearer saw. */
  conversationId?: string;
  conversationRevision?: number;
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

/**
 * The overview's right panel is a vertical rail of tabs. Order is the rail
 * order, top to bottom; Notifications is first because it is what the
 * resting screen shows, and the tab every reopen lands on. These are NOT
 * sidebar capabilities: the rail is reached by swiping on the overview.
 * Mac counterpart: PANEL_TABS in apps/mac/joat/core/protocol.py.
 */
export const PANEL_TABS = ['notifications', 'news', 'pomodoro', 'search'] as const;
export type PanelTab = (typeof PANEL_TABS)[number];
/** The tabs whose content the Mac pushes as `hud.panel`. */
export type PanelContentTab = Exclude<PanelTab, 'notifications'>;

export type PomodoroPhase = 'idle' | 'focus' | 'break' | 'long_break';

/** The timer as the Mac last knew it. The plugin counts down from
 * `endsAtUnixMs` on its own clock, so a tick costs no Mac traffic. */
export interface HudPanelPomodoro {
  phase: PomodoroPhase;
  running: boolean;
  /** Wall-clock end of the running phase; absent while paused or idle. */
  endsAtUnixMs?: number;
  /** Time left while paused, or the full phase while idle. */
  remainingMs: number;
  /** Length of the current phase, for a progress bar. */
  lengthMs: number;
  /** Focus blocks finished in the current set of four. */
  completed: number;
}

/** The request id a panel row tap echoes: the tab and the content revision
 * it was tapped on. Mac counterpart: panel_request_id / parse_panel_request_id. */
export function panelRequestId(tab: PanelContentTab, revision: number): string {
  return `panel-${tab}-${revision}`;
}

export function parsePanelRequestId(
  requestId: string | undefined,
): { tab: PanelContentTab; revision: number } | undefined {
  const match = requestId?.match(/^panel-(news|pomodoro|search)-(\d{1,9})$/);
  if (!match) return undefined;
  return { tab: match[1] as PanelContentTab, revision: Number(match[2]) };
}
