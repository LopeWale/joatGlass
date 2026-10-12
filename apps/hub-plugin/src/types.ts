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
export const SPINE_LABEL_BOX = { x: 95, y: 1, width: 192, height: 22 } as const;

/** Image updates must identify the same named visual container that the
 * layout declared. Keeping the mapping beside stable IDs prevents a bridge
 * acknowledgement from being mistaken for a visible tile when a host routes
 * raw pixels by name. */
export const VisualContainerName: Readonly<Record<number, string>> = {
  [ContainerId.Image]: 'visual-tl',
  [ContainerId.VisualTopRight]: 'visual-tr',
  [ContainerId.VisualBottomLeft]: 'visual-bl',
  [ContainerId.VisualBottomRight]: 'visual-br',
  16: 'hdr-left',
  18: 'home-top',
  19: 'home-bot',
  17: 'hdr-right',
};

/** The one header every firmware page carries: the image spine's two
 * halves (HeaderImage.ts). Page-local ids; they never cross the wire. */
export const HeaderImageId = { Left: 16, Right: 17 } as const;
/** Home's left column as two images (HomeImages.ts). */
export const HomeColumnId = { Top: 18, Bottom: 19 } as const;
/** Home's pane label column (OverviewText.ts): the grid's left column,
 * beside the value column that is ContainerId.Main. Page-local. */
export const HomePaneLabelId = 20;

/** Canvas constants from the SDK. Never hardcode these elsewhere. */
export const CANVAS = { width: 576, height: 288, greyLevels: 16 } as const;

/** Request ids the Mac mints for voice-initiated surfaces. A list arriving
 * with this prefix is adopted as a fresh Mac-initiated generation instead of
 * being dropped as stale. Mac counterpart: VOICE_REQUEST_PREFIX in
 * apps/mac/joat/core/protocol.py. */
export const VOICE_REQUEST_PREFIX = 'voice-';
/** JavaScript UTF-16 code units. Longer previews must fail, never truncate. */
export const CONFIRM_PROMPT_MAX_UNITS = 120;
/** A Messages draft reviewed in JOAT's box (seven rows), not on the native
 * confirmation page the 120 above was sized for. Mac twin:
 * MESSAGE_REVIEW_PROMPT_MAX_UTF16_UNITS in core/protocol.py. */
export const MESSAGE_REVIEW_PROMPT_MAX_UNITS = 280;

/**
 * How listening resumes after the wearer puts the glasses back on.
 * Doff always stops the mic locally (fail-safe); this only governs resume.
 * Wearer-configurable on the G2 (left-temple double-tap → settings list).
 */
export type ResumePolicy = 'auto' | 'mac' | 'confirm';

/** A saved place on the wire (core/protocol.py `SavedPlace`): a name and an
 * address to search for, coordinates to route to, or both. */
export interface SavedPlaceWire {
  id: string;
  name: string;
  address?: string;
  latitude?: number;
  longitude?: number;
}
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
/** The `brainstorm.view` WIRE payload — exactly the five fields
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
  | 'begin_topic_capture'
  /** Arm ONE utterance as input to the focused Agent Terminal session.
   * Distinct from begin_topic_capture: that starts a Brainstorm, which is
   * a different feature with different collaborators. */
  | 'begin_terminal_input'
  | 'show_terminal'
  | 'show_brainstorm'
  | 'cancel'
  | 'retry_terminal'
  /** Long-press on a running work session cancels it (2026-10-06): the
   * gesture for "terminal cancel", which stays as the spoken form. */
  | 'cancel_terminal'
  /** Answer the tool request the focused work session is blocked on.
   * Voice ("terminal approve") was the only way, so a wearer looking at a
   * blocked session had no gesture for the decision in front of them. */
  | 'approve_terminal'
  | 'deny_terminal';

/** What a decision gate is for, when it is not a generic action: a spoken
 * terminal reply is CHECKED, so the page says "send" and its swipe asks for
 * the words again instead of cancelling (2026-10-06). */
export type ConfirmationPurpose = 'terminal_input';

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
export type PromptOwnerKind = 'messages' | 'brainstorm' | 'terminal' | 'capability';

export interface PromptPresentMessage {
  kind: 'prompt.present';
  promptId: string;
  question: string;
  presentationBinding: string;
  /** Opaque Mac audio-session identity, not the local MacLink generation. */
  connectionGeneration: string;
  source: 'glasses' | 'phone';
  expiresInMs: number;
  /** 'capability': a question a tapped page row asks for itself. */
  ownerKind: PromptOwnerKind;
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

/** The wearer's words so far while they answer this prompt (Mac partial
 * transcript). Display only; the final transcript alone answers. */
export interface PromptPartialMessage {
  kind: 'prompt.partial';
  promptId: string;
  connectionGeneration: string;
  text: string;
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

/**
 * The phone's JOAT control screen (the plugin page inside the Even app, not
 * the lenses): Brainstorm reset, saved subscription accounts, seat models.
 * Mirrors `ControlAction` / `ControlState` in core/protocol.py — the two
 * must change together (tests/unit/test_protocol.py pins the Mac side).
 */
export type ControlProviderName = 'claude' | 'codex';
export type ControlModelSlotKey = 'codex' | 'claude' | 'assistant';

export type ControlActionMessage =
  | { kind: 'control.action'; action: 'refresh' | 'brainstorm.reset' | 'account.cancel' | 'account.dismiss' }
  | { kind: 'control.action'; action: 'account.add'; provider: ControlProviderName; label: string }
  | {
    kind: 'control.action';
    action: 'account.activate' | 'account.signin' | 'account.signout' | 'account.remove';
    provider: ControlProviderName;
    profileId: string;
  }
  | { kind: 'control.action'; action: 'account.rename'; provider: ControlProviderName; profileId: string; label: string }
  /** A one-time code the Claude sign-in page showed. Never stored or logged. */
  | { kind: 'control.action'; action: 'account.code'; code: string }
  | { kind: 'control.action'; action: 'model.select'; slot: ControlModelSlotKey; option: string };

/** logged_in · logged_out · limited · missing · unknown */
export type ControlAccountState = string;

export interface ControlSeatView {
  name: string;
  title: string;
  installed: boolean;
  account: ControlAccountState;
  accountLabel: string;
  model: string;
}

export interface ControlProfileView {
  id: string;
  label: string;
  state: ControlAccountState;
  isDefault: boolean;
  active: boolean;
  email?: string;
  plan?: string;
}

export interface ControlProviderView {
  provider: ControlProviderName;
  title: string;
  installed: boolean;
  profiles: ControlProfileView[];
}

export interface ControlModelSlotView {
  key: string;
  title: string;
  current: string;
  options: string[];
}

export interface ControlLoginView {
  provider: ControlProviderName;
  profileId: string;
  /** starting · open_link · enter_code · completing · done · failed · cancelled */
  stage: string;
  message: string;
  needsCode: boolean;
  url?: string;
  code?: string;
}

export interface ControlStateView {
  revision: number;
  brainstorm: { state: string; phase: string; detail: string; seats: ControlSeatView[] };
  accounts: ControlProviderView[];
  models: ControlModelSlotView[];
  login: ControlLoginView | null;
  notice: string;
}

/** Messages sent plugin → Mac. */
export type UplinkMessage =
  | PromptAckMessage
  | PromptCancelMessage
  | ControlActionMessage
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
  /** The wearer's saved places, whole, whenever the phone page changes them
   * and on every connect (core/protocol.py `Places`). */
  | { kind: 'places'; places: SavedPlaceWire[] }
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
    action: BrainstormAction;
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
  /** The wearer tapped the due page: complete that occurrence. Bound like
   * the acknowledgement; the Mac still runs its confirmed completion flow. */
  | {
    kind: 'reminder_due.complete';
    reminderDueId: string;
    reminderDueAttemptId: string;
    generation: number;
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
/** One unread JOAT notification as the glasses' card shows it. */
export interface HudNotificationCard {
  /** The Mac's card id (32 hex): the dismiss uplink's `traceId`. */
  id: string;
  app: string;
  sender: string;
  /** The whole message, whitespace collapsed, at most 600 characters. */
  body: string;
  /** Unix ms. */
  at: number;
  urgent: boolean;
}

/**
 * Every unread JOAT notification, newest first (at most ten), republished
 * whenever the Mac's card log changes. The notification card reads its next
 * unread from here, so a tap moves on without a round trip.
 * Mac counterpart: HudNotifications in apps/mac/joat/core/protocol.py.
 */
export interface HudNotificationsMessage {
  kind: 'hud.notifications';
  cards: HudNotificationCard[];
}

export type DownlinkMessage =
  | PromptPresentMessage
  | MessageUIMessage
  | PromptCancelMessage
  | PromptPartialMessage
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
    /** Row indices of the "newer"/"older" paging rows a Messages conversation
     * carries. Present (even empty) means paging travels as rows, so the
     * thread may be a firmware list, which reports taps but no scroll
     * events; a tap on one of these rows is the page request. Mac
     * counterpart: HudList.page_controls in core/protocol.py. */
    pageControls?: { newerIndex?: number; olderIndex?: number };
  }
  | { kind: 'brainstorm.view'; brainstorm: BrainstormViewPayload }
  | { kind: 'agent_terminal.view'; terminal: AgentTerminalHudModel }
  | { kind: 'productivity.view'; productivity: ProductivityHudModel }
  | { kind: 'audio.control'; open: boolean; source?: 'glasses' | 'phone' }
  | { kind: 'imu.control'; open: boolean; paceMs?: number }
  /** Calibration only: relay every raw IMU sample uplink (~10/s at P100). */
  | { kind: 'imu.trace'; enabled: boolean }
  | { kind: 'confirm.request'; actionId: string; prompt: string; challengeId: string; sessionId: string; payloadDigest: string; previewDigest: string; expiresAtMs: number; diagnosticKind?: 'messages'; messageUIActionId?: string; purpose?: ConfirmationPurpose }
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
  | HudNotificationsMessage
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
  | ({ kind: 'control.state' } & ControlStateView)
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
