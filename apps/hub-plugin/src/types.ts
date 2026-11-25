/**
 * Shared types for the Hub plugin ⇄ Mac brain protocol.
 * Keep this file in sync with apps/mac/joat/core (the Python mirror).
 */
import type { ReminderCard } from './ReminderCard';

/** RFC 6455 private-use close code: another authenticated display took over. */
export const PLUGIN_REPLACED_CLOSE_CODE = 4409;
/** A standby dial (`?standby=1`) refused: another display holds the glasses.
 * Twin: apps/mac/joat/core/protocol.py. */
export const PLUGIN_SLOT_OCCUPIED_CLOSE_CODE = 4410;
/** The Mac heard nothing from this page for too long and closed the link.
 * Twin: apps/mac/joat/core/protocol.py. */
export const PLUGIN_HEARTBEAT_LOST_CLOSE_CODE = 4408;

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
  21: 'rem-card',
  22: 'nav-map',
};

/** The one header every firmware page carries: the image spine's two
 * halves (HeaderImage.ts). Page-local ids; they never cross the wire. */
export const HeaderImageId = { Left: 16, Right: 17 } as const;
/** Home's left column as two images (HomeImages.ts). */
export const HomeColumnId = { Top: 18, Bottom: 19 } as const;
/** Home's pane label column (OverviewText.ts): the grid's left column,
 * beside the value column that is ContainerId.Main. Page-local. */
export const HomePaneLabelId = 20;
/** The Reminders page's next-up card image (ReminderCard.ts). Page-local. */
export const ReminderCardId = 21;
/** Active Navigation's right-pane vector route schematic image (NavigationCard.ts). Page-local. */
export const NavigationMapId = 22;

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

/**
 * How JOAT uses the microphone on the glasses/phone:
 * - `glasses`: 24/7 continuous G2 BLE microphone (hands-free "JOAT" / "Hey JOAT").
 * - `phone`: 24/7 phone microphone via Even App (hands-free wake with zero G2 BLE audio traffic).
 * - `on_demand`: G2 BLE microphone stays OFF at rest (0 BLE pkt/s, matching native Even AI for
 *   zero R1 ring / AirPods / Tesla contention) and opens only during hold-to-talk or prompts.
 */
export type MicMode = 'glasses' | 'phone' | 'on_demand';

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
/** A transient JOAT response is never a Messages or Agent Terminal page. */
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
  /** True while the follow-up window is open (wearer can speak without saying "JOAT"). */
  listening?: boolean;
}
export type G2DiagnosticEvent = import('./G2DiagnosticTrace').G2TraceEvent;
export type NavigationHudModel = import('./NavigationHud').NavigationHudModel;
export type NavigationManeuver = import('./NavigationHud').NavigationManeuver;
export type NavigationRoutePlan = import('./NavigationProgress').NavigationRoutePlan;

export type TerminalHudState =
  | 'unavailable'
  | 'approval_required'
  | 'ready'
  | 'running'
  | 'settled'
  | 'cancelled'
  | 'failed'
  | 'blocked';

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

/** The Agent Terminal HUD model on the wire (`agent_terminal.view`). */
export interface AgentTerminalHudModel extends TerminalConversationFields {
  /** Opaque visit and row-selection identity; both are present together. */
  openingRequestId?: string;
  openingSelectionId?: string;
  state: TerminalHudState;
  /** The focused lane, in the roster's "<lane> · <state>" shape. */
  focus: string;
  /** Lane chips: every concurrent session, so one glance covers the roster. */
  lanes: string[];
  activity: string;
  terminalReason?: string;
  /** Tool requests the focused lane is blocked on.
   *
   * Distinct from `state === 'approval_required'`, which also describes a
   * CONNECTION whose device has not been approved — a different problem
   * whose remedy is retry, not approve. */
  approvals?: number;
}

/**
 * Bounded wearer intents while the live Agent Terminal surface owns the HUD.
 * Spoken language remains the prompt source, while gestures control the
 * focused terminal session lifecycle.
 */
export type TerminalAction =
  | 'begin_terminal_input'
  | 'retry_terminal'
  | 'cancel_terminal'
  | 'approve_terminal'
  | 'deny_terminal';

export interface TerminalActionMessage {
  kind: 'terminal.action';
  action: TerminalAction;
}

/** What a decision gate is for, when it is not a generic action: a spoken
 * terminal reply is CHECKED, so the page says "send" and its swipe asks for
 * the words again instead of cancelling (2026-10-06). */
export type ConfirmationPurpose = 'terminal_input';

/** A bounded lifecycle page for native Calendar and Reminders actions.
 * The interactive preview remains a separate `confirm.request` frame; this
 * state is only what the native bridge is doing or established afterward. */
export type ProductivityHudDomain = 'calendar' | 'reminders';

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
    /** This page answers link.ping (MacLink); the Mac may then hold it to it. */
    heartbeat?: true;
    /** This page follows `brain.handoff`: a phone brain may send a Mac-pinned
     * page (Messages, Agent Terminal, Activity) to a Mac instead of saying it is not there. */
    handoff?: true;
  }
  | { kind: 'link.pong'; seq: number }
  | { kind: 'audio'; source: 'glasses' | 'phone'; seq: number; pcm: number[]; speaker?: string; audioEpoch?: string }
  | { kind: 'imu'; x: number; y: number; z: number; at: number }
  | { kind: 'input'; event: InputEventPayload }
  | {
    kind: 'device';
    battery?: number;
    wearing?: boolean;
    charging?: boolean;
    inCase?: boolean;
    connect: string;
    mic?: boolean;
    micSource?: 'glasses' | 'phone';
    micPromptId?: string;
    deviceRole?: 'glasses' | 'other';
    snTail?: string;
    rearmReason?: string;
    ringTouches?: number;
  }
  | { kind: 'confirm'; actionId: string; accepted: boolean; challengeId?: string; sessionId?: string; payloadDigest?: string; previewDigest?: string }
  | { kind: 'policy'; resume: ResumePolicy; micMode?: MicMode }
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
  | TerminalActionMessage
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
    maneuver?: NavigationManeuver;
    maneuverDistanceM?: number;
    instruction?: string;
    stepId?: string;
    remainingDistanceM?: number;
    etaMinutes?: number;
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
 * Every unread JOAT notification, newest first (at most 25), republished
 * whenever the brain's card log changes. The notification card reads its next
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
  | { kind: 'link.ping'; seq: number }
  /** From the phone brain (JOAT Brain): the wearer opened a page only a Mac
   * has. The plugin opens it on one of the package's Macs and comes back to
   * the phone when the wearer goes Home (main.ts BrainHandoff). */
  | { kind: 'brain.handoff'; capability: HandoffCapability; requestId?: string }
  | { kind: 'hud.text'; containerId: ContainerId; text: string; offset?: number; length?: number; append?: boolean; textColor?: number; contentKind?: HudContentKind; traceId?: string }
  | {
    kind: 'assistant.overlay';
    operation: AssistantOverlayOperation;
    text?: string;
    dismissAfterMs?: number;
    /** Explicit visual meaning; older Mac brains omit it and default to an answer. */
    presentation?: 'answer' | 'receipt';
    /** This overlay ANSWERS something the wearer just asked on the surface
     * they are looking at, rather than queued background chatter. */
    answersWearer?: boolean;
  }
  | ({ kind: 'assistant.conversation'; operation: 'replace' } & AssistantConversationSnapshot)
  | {
    kind: 'assistant.conversation';
    operation: 'dismiss';
    conversationId: string;
    revision: number;
  }
  | { kind: 'hud.rebuild'; layout: 'default' | 'list' | 'image'; reason?: 'terminal' | 'brainstorm' }
  | {
    kind: 'hud.list';
    /** Opaque reader visit and source-page cursor, separate from reply ownership. */
    readerPage?: { id: string; index: number; count: number };
    /** Mac-owned archive page identity; note IDs never cross this boundary. */
    quickNotesPage?: { index: number; count: number };
    items: string[];
    /** A reader's whole text (a News article, a Messages conversation, a
     * saved QuickNote: the reader template), after any rows in `items`. Not
     * a list row, so not held to the firmware's 20 x 63-byte list caps: the
     * plugin lays it out as one document the glasses scroll. Mac
     * counterpart: HudList.body in core/protocol.py. */
    body?: string;
    /** Display context only, independent of routing and private action targets. */
    contextLabel?: string;
    /** First navigation/view control in the unchanged Mac-owned row array. */
    controlStartIndex?: number;
    planningEntries?: Array<{ time: string; kind: "event" | "reminder"; title: string } | null>;
    title?: string;
    hint?: string;
    requestId?: string;
    selectable?: boolean;
    /** The Reminders page's next-up card (ReminderCard.ts): the next
     * reminder not yet late and the open counts. JOAT Brain counterpart:
     * Reminders.listPage. */
    reminderCard?: ReminderCard;
    planningNavigation?: { canGoBack: boolean; selectedIndex: number };
    /** Row indices of the "newer"/"older" paging rows a Messages conversation
     * carries. Present (even empty) means paging travels as rows, so the
     * thread may be a firmware list, which reports taps but no scroll
     * events; a tap on one of these rows is the page request. Mac
     * counterpart: HudList.page_controls in core/protocol.py. */
    pageControls?: { newerIndex?: number; olderIndex?: number };
    /** Whether live QuickNotes AI cues are enabled in JOAT Brain settings. */
    cuesEnabled?: boolean;
  }
  | { kind: 'agent_terminal.view'; terminal: AgentTerminalHudModel }
  | { kind: 'productivity.view'; productivity: ProductivityHudModel }
  | { kind: 'audio.control'; open: boolean; source?: 'glasses' | 'phone' }
  | { kind: 'imu.control'; open: boolean; paceMs?: number }
  /** Calibration only: relay every raw IMU sample uplink (~10/s at P100). */
  | { kind: 'imu.trace'; enabled: boolean }
  | { kind: 'confirm.request'; actionId: string; prompt: string; challengeId: string; sessionId: string; payloadDigest: string; previewDigest: string; expiresAtMs: number; diagnosticKind?: 'messages'; messageUIActionId?: string; purpose?: ConfirmationPurpose }
  | { kind: 'confirm.cancel'; actionId: string }
  | { kind: 'policy.set'; resume: ResumePolicy; micMode?: MicMode }
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
    phoneFixes?: boolean;
  }
  | {
    kind: 'navigation.fix';
    sessionId: string;
    latitude: number;
    longitude: number;
    accuracy?: number;
    heading?: number;
    speed?: number;
    timestamp?: number;
  }
  | { kind: 'navigation.resume'; sessionId: string }
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
  | { kind: 'shutdown'; exitMode?: 0 | 1 };

export interface InputEventPayload {
  quickNotesPageIndex?: number;
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
 * the cards themselves, so a separate menu row to re-read the home screen
 * would spend an extra page, request, and round trip unnecessarily.
 */
export const CAPABILITIES = [
  // Reminders replaced Planning and its calendar; a Mac brain answers it
  // with its Planning pages (capabilities.py). Brainstorm is omitted from
  // the top-level menu in favour of the conversational JOAT assistant.
  'Messages', 'Mail', 'Reminders', 'Navigation',
  // QuickNotes lives on the overview's rail; Search opens from the menu.
  'Search', 'Agent Terminal', 'Activity', 'Settings',
] as const;

export type CapabilityName = (typeof CAPABILITIES)[number];

/** Pages a phone brain hands to a Mac (`brain.handoff`). */
export const HANDOFF_CAPABILITIES = ['Messages', 'Agent Terminal', 'Activity'] as const;
export type HandoffCapability = (typeof HANDOFF_CAPABILITIES)[number];

/** Firmware menu ids for the rows above; item 0 is reserved by the protocol,
 * and 1-5 are the retired internal controls, so capabilities start at 10. */
export const CAPABILITY_MENU_ID_BASE = 10;

/** An open QuickNote's page — Even's "New note" (FEAT-002). */
export const QUICKNOTES_NOTE_TITLE = 'QUICKNOTES · NEW NOTE';
/** "Undo last input" in the glasses' menu while that page is open (Even's
 * QuickNote menu). Id 6: 1-5 are retired controls, 10+ the capabilities. */
export const QUICKNOTES_UNDO_MENU_ID = 6;
export const QUICKNOTES_UNDO_MENU_LABEL = 'Undo last input';
/** "AI cues" and "Prep notes" after it on the same menu (Even's Conversate
 * cues and Prep Notes, 2026-10-30): the brain draws the page. Ids 8 and 9,
 * the last free ones below the capabilities. */
export const QUICKNOTES_CUES_MENU_ID = 8;
export const QUICKNOTES_CUES_MENU_LABEL = 'AI cues';
export const QUICKNOTES_PREP_MENU_ID = 9;
export const QUICKNOTES_PREP_MENU_LABEL = 'Prep notes';

/**
 * The overview's right panel is a vertical rail of tabs. Order is the rail
 * order, top to bottom; Notifications is first because it is what the
 * resting screen shows and what every reopen lands on; Reminders (planner)
 * is second. These are NOT sidebar capabilities: the rail is reached by
 * swiping on the overview. Mac counterpart: PANEL_TABS in
 * apps/mac/joat/core/protocol.py.
 */
export const PANEL_TABS = ['notifications', 'planner', 'news', 'pomodoro', 'quicknotes'] as const;
export type PanelTab = (typeof PANEL_TABS)[number];
/** The tabs whose content the Mac pushes as `hud.panel`: the rail's, and
 * Search, whose page the menu opens. QuickNotes sits directly on the
 * overview rail for quick capture. Mac counterpart: PANEL_TABS in core/protocol.py. */
export const PANEL_CONTENT_TABS = ['planner', 'news', 'pomodoro', 'quicknotes', 'search'] as const;
export type PanelContentTab = (typeof PANEL_CONTENT_TABS)[number];

/** `timer` is the plain one-shot countdown: no breaks, no sets. */
export type PomodoroPhase = 'idle' | 'focus' | 'break' | 'long_break' | 'timer';

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
  /** Focus blocks finished in the current set. */
  completed: number;
  /** Focus blocks before the long break; the wearer sets it. Absent = 4. */
  blocksPerSet?: number;
}

/** The request id a panel row tap echoes: the tab and the content revision
 * it was tapped on. Mac counterpart: panel_request_id / parse_panel_request_id. */
export function panelRequestId(tab: PanelContentTab, revision: number): string {
  return `panel-${tab}-${revision}`;
}
