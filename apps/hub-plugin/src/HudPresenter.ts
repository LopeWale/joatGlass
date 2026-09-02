import { TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import type { HudPageBuilder } from './HudPageBuilder';
import type { AssistantOverlayVisual, HudFramePainter, HudVisualFrame } from './HudVisualSurface';
import type { G2DiagnosticTrace, G2TracePage } from './G2DiagnosticTrace';
import { usesActionPillLayout } from './HudCanvasRenderer';
import type { NavigationHudModel } from './NavigationHud';
import {
  PanelRail,
  pomodoroTickDelayMs,
  type HudPanelMessage,
  type PanelContent,
} from './PanelRail';
import {
  CONFIRM_PROMPT_MAX_UNITS,
  ContainerId,
  type BrainstormHudModel,
  type HudContentKind,
  type PanelContentTab,
  type PanelTab,
  type PlanningGridCell,
  type ProductivityHudDomain,
  type ProductivityHudModel,
} from './types';

const MAX_UPGRADE_CHARS = 2000;

/**
 * Repaint throttle for streamed text. Tokens arrive far faster than BLE can
 * move frames to the glasses; painting per token floods the link and, on
 * real hardware, rendered as flashing fragments ("cutting out, one line").
 * ~3 paints/second reads as live streaming while keeping BLE sane.
 */
const PAINT_INTERVAL_MS = 300;

const DEFAULT_LIST_TITLE = 'JOAT';
const DEFAULT_LIST_HINT = 'left x2 overview';
const MAX_FOCUS_COPY_CHARS = 46;

/** The downlink is a WebSocket boundary, so treat its optional visual
 * metadata as untrusted even though TypeScript says it is well shaped. A bad
 * or partial grid must fall back to the ordinary selectable list. */
/** Rebuild a container-keyed Map from a JSON record, dropping bad entries.
 * A background snapshot crosses a JSON boundary and a process boundary, so it
 * is untrusted input, not a value this page wrote. */
function numericMap<T>(
  raw: unknown,
  valid: (value: unknown) => value is T,
): Map<number, T> {
  const map = new Map<number, T>();
  if (typeof raw !== 'object' || raw === null) return map;
  for (const [key, value] of Object.entries(raw)) {
    const id = Number(key);
    if (Number.isInteger(id) && valid(value)) map.set(id, value);
  }
  return map;
}

function normalizePlanningEntries(
  entries: PlanningGridCell['entries'],
): PlanningGridCell['entries'] {
  if (!Array.isArray(entries)) return undefined;
  const kept = entries
    .filter((entry) => (
      typeof entry === 'object' && entry !== null
      && typeof entry.time === 'string' && entry.time.length <= 8
      && (entry.kind === 'event' || entry.kind === 'reminder')
      && typeof entry.title === 'string' && entry.title.length > 0
    ))
    .slice(0, 6)
    .map((entry) => ({
      time: entry.time,
      kind: entry.kind,
      title: entry.title.slice(0, 64),
    }));
  return kept.length > 0 ? kept : undefined;
}

function normalizePlanningGrid(
  planningGrid: readonly PlanningGridCell[] | undefined,
): PlanningGridCell[] | undefined {
  if (!planningGrid || planningGrid.length !== 42) return undefined;
  const normalized = planningGrid.map((cell) => {
    const entries = normalizePlanningEntries(cell.entries);
    return {
      day: cell.day,
      eventCount: cell.eventCount,
      reminderCount: cell.reminderCount,
      inMonth: cell.inMonth,
      ...(entries ? { entries } : {}),
    };
  });
  const isValid = normalized.every((cell) => (
    typeof cell.day === 'string'
    && /^\d{4}-\d{2}-\d{2}$/.test(cell.day)
    && Number.isInteger(cell.eventCount)
    && cell.eventCount >= 0
    && cell.eventCount <= 99
    && Number.isInteger(cell.reminderCount)
    && cell.reminderCount >= 0
    && cell.reminderCount <= 99
    && typeof cell.inMonth === 'boolean'
  ));
  return isValid ? normalized : undefined;
}

const DEFAULT_BRIGHTNESS: Readonly<Record<number, number>> = {
  [ContainerId.StatusBar]: 2,
  [ContainerId.Main]: 4,
  [ContainerId.Context]: 3,
  [ContainerId.Tray]: 2,
  [ContainerId.Hint]: 1,
};

/** Plugin-local dashboard focus: which date is hovered, whether the right
 * pane owns input, and which of its rows is hovered. Never crosses the wire —
 * a hover must not cost a Mac round trip on the serialized lane. */
export interface PlanningPaneState {
  dateIndex: number;
  focused: boolean;
  itemIndex: number;
}

export interface ListSurfaceOptions {
  title?: string;
  hint?: string;
  selectable?: boolean;
  planningGrid?: readonly PlanningGridCell[];
  planningPane?: PlanningPaneState;
  /** This list is an expanded panel tab: keep its rail lit on the left. */
  railTab?: PanelContentTab;
}

interface ResolvedListSurfaceOptions {
  title: string;
  hint: string;
  selectable: boolean;
  planningGrid?: readonly PlanningGridCell[];
  planningPane?: PlanningPaneState;
  railTab?: PanelContentTab;
}

interface HudContentState {
  streams: Map<number, string>;
  brightness: Map<number, number>;
  kinds: Map<number, HudContentKind>;
}

interface PresenterSnapshot extends HudContentState {
  surface: HudSurface;
  focusReturn?: HudContentState;
}

type HudSurface =
  | { kind: 'default' }
  | {
    kind: 'list';
    items: string[];
    options: ResolvedListSurfaceOptions;
    selectedIndex: number;
  }
  | { kind: 'navigation'; navigation: NavigationHudModel }
  | { kind: 'brainstorm'; brainstorm: BrainstormHudModel }
  | { kind: 'productivity'; productivity: ProductivityHudModel };

interface ConfirmationReturnState extends HudContentState {
  id: string;
  prompt: string;
  diagnosticKind?: 'messages';
  surface: HudSurface;
  onDeferredProductivityVisible?: () => void;
  /** Pill deferred behind an in-flight dismissal; fires once the restore paints. */
  onDeferredPillVisible?: () => void;
}

interface AssistantOverlayState extends AssistantOverlayVisual {
  dismissTimer?: ReturnType<typeof setTimeout>;
}

interface ActionOverlayState {
  productivity: ProductivityHudModel;
  dismissTimer?: ReturnType<typeof setTimeout>;
}

/** Failsafe only — CapabilityMenu owns the real auto-return; this stops a
 * lost frame from leaving the receipt on the glass forever. */
const ACTION_PILL_FAILSAFE_MS = 8_000;

function sameProductivity(a: ProductivityHudModel, b: ProductivityHudModel): boolean {
  return a.domain === b.domain
    && a.phase === b.phase
    && a.title === b.title
    && a.primary === b.primary
    && a.secondary === b.secondary
    && a.hint === b.hint
    && a.planningAnchorUnixMs === b.planningAnchorUnixMs
    && a.monitoringArmed === b.monitoringArmed
    && a.reminderDueId === b.reminderDueId
    && a.reminderDueAttemptId === b.reminderDueAttemptId
    && a.reminderDueGeneration === b.reminderDueGeneration
    && a.presentationReceiptId === b.presentationReceiptId
    && a.presentationReceiptGeneration === b.presentationReceiptGeneration;
}

/** A Home notification temporarily replaces Main, but must never destroy the
 * card it covered. Keep only the local visual tuple; acknowledging or
 * mutating a native notification remains a separate Mac-owned operation. */
interface NotificationReturnState {
  text?: string;
  brightness?: number;
  kind?: HudContentKind;
}

/**
 * Renders text to the HUD.
 *
 * Streaming is ACCUMULATE-AND-REPAINT, not splice-at-offset: every paint
 * carries the full accumulated text from offset 0, so each frame is
 * self-contained. Offset splicing looked fine in the simulator but real
 * firmware rendered only the latest chunk — and with per-token BLE writes a
 * dropped frame silently corrupted everything after it. Full frames make
 * dropped/reordered updates self-healing.
 */
export class HudPresenter {
  private lastLength = new Map<number, number>();
  private streams = new Map<number, string>();
  private lastPaint = new Map<number, number>();
  private pendingPaint = new Map<number, ReturnType<typeof setTimeout>>();
  private brightness = new Map<number, number>();
  private kinds = new Map<number, HudContentKind>();
  private surface: HudSurface = { kind: 'default' };
  /** Margin facts (weather, unread) resolved by the Mac for the dashboard. */
  private ambient?: { temperature: string; condition: string; unread: number };
  /** The right pane's tab rail: which tab is active, and what the Mac last
   * pushed for each. Local state — a swipe between tabs never round-trips. */
  readonly rail = new PanelRail();
  /** Repaints the visible pomodoro when its next digit moves. */
  private panelTicker?: ReturnType<typeof setTimeout>;
  private focusReturn?: HudContentState;
  private confirmation?: ConfirmationReturnState;
  private confirmationQueue: Promise<unknown> = Promise.resolve();
  private assistantOverlay?: AssistantOverlayState;
  private actionOverlay?: ActionOverlayState;
  private assistantOverlayPaint?: Promise<boolean>;
  private assistantOverlayPaintTimer?: ReturnType<typeof setTimeout>;
  private assistantOverlayPaintDirty = false;
  private assistantOverlayPaintStartedAt = 0;
  private notificationReturn?: NotificationReturnState;
  private micOn = false;

  constructor(
    private readonly client: BridgeClient,
    private readonly layouts: HudPageBuilder,
    private readonly visual?: HudFramePainter,
    private readonly diagnostics?: G2DiagnosticTrace,
  ) {}

  /** Replace a container's full text. Also resets any stream in progress.
   *
   * `textColor` (firmware brightness 0-4) sticks to the container until the
   * next showText sets or clears it — the Mac computes it per card (urgent
   * bright, routine dim) and the old handler silently dropped the field, so
   * the feature had never worked on glass (observed live). */
  async showText(
    text: string,
    containerId: ContainerId = ContainerId.Main,
    textColor?: number,
    contentKind?: HudContentKind,
  ): Promise<boolean> {
    if (this.confirmation && containerId !== ContainerId.StatusBar) {
      this.deferText(text, containerId, textColor, contentKind);
      return true;
    }
    // A focus page owns the entire non-status canvas. Background replies,
    // transcripts, tray updates, and Chrome's dashboard hint are buffered
    // until Back instead of overwriting its header or targeting containers
    // that intentionally do not exist in this layout.
    if (this.surface.kind !== 'default' && containerId !== ContainerId.StatusBar) {
      this.deferFocusText(text, containerId, textColor, contentKind);
      return true;
    }
    return this.replaceText(text, containerId, textColor, contentKind);
  }

  private async replaceText(
    text: string,
    containerId: ContainerId,
    textColor?: number,
    contentKind?: HudContentKind,
  ): Promise<boolean> {
    if (containerId === ContainerId.Main) {
      if (contentKind === 'notification') {
        if (!this.notificationReturn) {
          this.notificationReturn = {
            text: this.streams.get(ContainerId.Main),
            brightness: this.brightness.get(ContainerId.Main),
            kind: this.kinds.get(ContainerId.Main),
          };
        }
      } else {
        // A newer normal card is authoritative Home content; never resurrect
        // an older snapshot when the wearer later taps the screen.
        this.notificationReturn = undefined;
      }
    }
    this.cancelPending(containerId);
    this.streams.set(containerId, text);
    if (textColor !== undefined) {
      this.brightness.set(containerId, textColor);
    } else {
      this.brightness.delete(containerId);
    }
    if (containerId === ContainerId.Main) {
      this.kinds.set(containerId, contentKind ?? 'assistant');
    }
    return this.paint(containerId);
  }

  /** Stream a chunk: accumulate, repaint (throttled) with the full text. */
  async appendText(chunk: string, containerId: ContainerId = ContainerId.Main): Promise<boolean> {
    if (this.confirmation && containerId !== ContainerId.StatusBar) {
      const streams = this.confirmation.streams;
      streams.set(containerId, (streams.get(containerId) ?? '') + chunk);
      return true;
    }
    if (this.surface.kind !== 'default' && containerId !== ContainerId.StatusBar) {
      const deferred = this.ensureFocusReturn();
      deferred.streams.set(
        containerId,
        (deferred.streams.get(containerId) ?? '') + chunk,
      );
      return true;
    }
    this.streams.set(containerId, (this.streams.get(containerId) ?? '') + chunk);
    const now = Date.now();
    const elapsed = now - (this.lastPaint.get(containerId) ?? 0);
    if (elapsed >= PAINT_INTERVAL_MS) {
      return this.paint(containerId);
    }
    if (!this.pendingPaint.has(containerId)) {
      // Trailing paint so the tail of a burst always lands — without it the
      // final tokens of a reply would sit unpainted forever.
      this.pendingPaint.set(containerId, setTimeout(() => {
        this.pendingPaint.delete(containerId);
        void this.paint(containerId);
      }, PAINT_INTERVAL_MS - elapsed));
    }
    return true;
  }

  /**
   * JOAT is a transient top layer, not another content page. It intentionally
   * leaves the current Messages/Navigation/Brainstorm/Hermes surface intact;
   * only a consent confirmation remains visually exclusive.
   */
  async showAssistantOverlay(
    text: string, append = false, answersWearer = false,
  ): Promise<boolean> {
    // Not over a brainstorm/terminal page — UNLESS it answers something
    // the wearer just asked there. Seen live in the simulator: a queued
    // reply drained the instant a session settled and painted "REPLY —
    // Using the phone mic…" across the synthesis, default-layout text
    // stamped over a full-frame surface. The session's answer owns that
    // screen and unrelated chatter must wait.
    //
    // But the rule was written as "never", and that silenced the replies
    // TO the wearer: "make that shorter" produced its answer into nothing,
    // and the question asked before replacing a live session was never
    // seen — so the wearer was waiting on a prompt that did not exist.
    // An answer is not an interruption.
    if (this.surface.kind === 'brainstorm' && !answersWearer) return false;
    if (!this.visual || !text) return false;
    const current = this.assistantOverlay;
    if (current?.dismissTimer !== undefined) clearTimeout(current.dismissTimer);
    this.assistantOverlay = { text: append ? `${current?.text ?? ''}${text}` : text };
    if (this.confirmation) return true;
    if (append || this.assistantOverlayPaint) {
      this.scheduleAssistantOverlayPaint();
      return true;
    }
    return this.paintAssistantOverlayNow();
  }

  settleAssistantOverlay(dismissAfterMs: number): boolean {
    const overlay = this.assistantOverlay;
    if (!overlay || !Number.isInteger(dismissAfterMs) || dismissAfterMs < 1_000 || dismissAfterMs > 15_000) {
      return false;
    }
    if (overlay.dismissTimer !== undefined) clearTimeout(overlay.dismissTimer);
    overlay.dismissTimer = setTimeout(() => {
      if (this.assistantOverlay === overlay) void this.dismissAssistantOverlay();
    }, dismissAfterMs);
    return true;
  }

  async dismissAssistantOverlay(): Promise<boolean> {
    const overlay = this.assistantOverlay;
    if (!overlay) return true;
    if (overlay.dismissTimer !== undefined) clearTimeout(overlay.dismissTimer);
    this.assistantOverlay = undefined;
    if (this.confirmation) return true;
    this.cancelAssistantOverlayPaintTimer();
    const active = this.assistantOverlayPaint;
    if (active) await active;
    // The active paint's completion may have queued the newest reply frame.
    // Cancel it after the await so an old tail can never repaint over a local
    // wearer dismissal.
    this.assistantOverlayPaintDirty = false;
    this.cancelAssistantOverlayPaintTimer();
    return this.paintAssistantOverlayNow();
  }

  /** Full raster frames are slower than model chunks. Keep at most one frame
   * active and one newest tail so streaming can never outrun the four-tile
   * bridge, including when the underlying surface is Navigation. */
  private paintAssistantOverlayNow(): Promise<boolean> {
    if (!this.visual) return Promise.resolve(false);
    if (this.assistantOverlayPaint) {
      this.assistantOverlayPaintDirty = true;
      return Promise.resolve(true);
    }
    this.assistantOverlayPaintStartedAt = Date.now();
    const paint = this.paintVisual();
    this.assistantOverlayPaint = paint;
    const completed = (): void => {
      if (this.assistantOverlayPaint !== paint) return;
      this.assistantOverlayPaint = undefined;
      if (!this.assistantOverlayPaintDirty) return;
      this.assistantOverlayPaintDirty = false;
      this.scheduleAssistantOverlayPaint();
    };
    void paint.then(completed, completed);
    return paint;
  }

  private scheduleAssistantOverlayPaint(): void {
    if (!this.visual || this.confirmation) return;
    if (this.assistantOverlayPaint) {
      this.assistantOverlayPaintDirty = true;
      return;
    }
    if (this.assistantOverlayPaintTimer !== undefined) return;
    const elapsed = Date.now() - this.assistantOverlayPaintStartedAt;
    const delay = Math.max(0, PAINT_INTERVAL_MS - elapsed);
    this.assistantOverlayPaintTimer = setTimeout(() => {
      this.assistantOverlayPaintTimer = undefined;
      void this.paintAssistantOverlayNow();
    }, delay);
  }

  private cancelAssistantOverlayPaintTimer(): void {
    if (this.assistantOverlayPaintTimer !== undefined) {
      clearTimeout(this.assistantOverlayPaintTimer);
    }
    this.assistantOverlayPaintTimer = undefined;
  }

  /** The transient reply owns ordinary input while it is readable, so a
   * wearer cannot accidentally activate the obscured feature underneath. */
  hasAssistantOverlay(): boolean {
    return this.assistantOverlay !== undefined;
  }

  /** Only a fully visible Home card is tappable. Focus pages keep incoming
   * notifications deferred, assistant overlays keep their higher priority,
   * and confirmations remain exclusive. */
  hasVisibleNotification(): boolean {
    return this.surface.kind === 'default'
      && !this.confirmation
      && !this.assistantOverlay
      && this.kinds.get(ContainerId.Main) === 'notification'
      && (this.streams.get(ContainerId.Main)?.trim().length ?? 0) > 0;
  }

  /** Hide the visible card locally and restore the exact displaced Main
   * content. This deliberately does not mark a native notification read. */
  async dismissVisibleNotification(): Promise<boolean> {
    if (!this.hasVisibleNotification()) return false;
    const prior = this.notificationReturn;
    this.notificationReturn = undefined;
    this.cancelPending(ContainerId.Main);
    this.streams.delete(ContainerId.Main);
    this.brightness.delete(ContainerId.Main);
    this.kinds.delete(ContainerId.Main);
    if (prior?.text !== undefined) this.streams.set(ContainerId.Main, prior.text);
    if (prior?.brightness !== undefined) this.brightness.set(ContainerId.Main, prior.brightness);
    if (prior?.kind !== undefined) this.kinds.set(ContainerId.Main, prior.kind);
    return this.paint(ContainerId.Main);
  }

  /**
   * Announce a transient state ("listening", "settings"). This does NOT
   * write container 1 directly any more: the status bar has ONE owner
   * (Chrome), which composes clock + state + battery. Direct writes from
   * four call sites were clobbering each other — observed live
   * and the collision the dashboard design existed to prevent. Until
   * Chrome registers, announcements fall back to a direct paint so early
   * boot is never mute.
   */
  onStatus?: (text: string) => void;

  setStatus(text: string): Promise<boolean> {
    if (this.onStatus) {
      this.onStatus(text);
      return Promise.resolve(true);
    }
    return this.showText(text, ContainerId.StatusBar);
  }

  /** Privacy state is structured; it must never be inferred from a transient
   * status word such as "connecting" or "paused". */
  setMicState(open: boolean): void {
    this.micOn = open;
  }

  async showList(items: string[], options: ListSurfaceOptions = {}): Promise<boolean> {
    const resolved = this.resolveListOptions(options);
    const surface: HudSurface = {
      kind: 'list', items: [...items], options: resolved, selectedIndex: 0,
    };
    if (this.confirmation) {
      if (this.confirmation.surface.kind === 'default' && !this.focusReturn) {
        this.focusReturn = {
          streams: new Map(this.confirmation.streams),
          brightness: new Map(this.confirmation.brightness),
          kinds: new Map(this.confirmation.kinds),
        };
      }
      this.confirmation.surface = surface;
      return true;
    }
    if (this.surface.kind === 'default' && !this.focusReturn) {
      this.focusReturn = {
        streams: new Map(this.streams),
        brightness: new Map(this.brightness),
        kinds: new Map(this.kinds),
      };
    }
    this.clearActionOverlay();
    return this.renderList(surface);
  }

  /** Paint a complete navigation snapshot. The Mac/session owns the state;
   * this presenter only gives it exclusive focus and exact restoration. */
  async showNavigation(navigation: NavigationHudModel, statusOverride?: string): Promise<boolean> {
    if (!this.visual) return false;
    const prior = this.capturePresenterSnapshot();
    const surface: HudSurface = {
      kind: 'navigation',
      navigation: this.cloneNavigation(navigation),
    };
    if (this.confirmation) {
      // A deferred surface has not crossed the SDK bridge. Navigation relies
      // on an exact generation-bound bridge receipt, so reject instead of
      // reporting a paint that never happened or resurrecting it on dismiss.
      return false;
    }
    if (this.surface.kind === 'default' && !this.focusReturn) {
      this.focusReturn = {
        streams: new Map(this.streams),
        brightness: new Map(this.brightness),
        kinds: new Map(this.kinds),
      };
    }
    // Route progress updates the SAME page; only entering navigation
    // supersedes a receipt strip.
    if (this.surface.kind !== 'navigation') this.clearActionOverlay();
    return this.renderNavigation(surface, statusOverride, prior);
  }

  /** Dedicated four-tile production page for a live Brainstorm or Hermes. */
  private thinkingTicker?: ReturnType<typeof setTimeout>;

  /** While the transcript tail is a silent "Thinking…" marker, repaint
   * locally once a second so the elapsed counter moves.
   *
   * No frame arrives from the Mac until the collaborator produces a
   * token — the engine used to compute this counter and re-ship a whole
   * frame over BLE every three seconds, which the wearer spotted on
   * sight: "it's only changing every three seconds … we're doing it the
   * difficult way." Device-local animation is smooth and costs the link
   * nothing. */
  /** How long the counter has been ticking, so it can slow down. */
  private thinkingTicks = 0;

  /** Whole seconds of silence after which the counter stops needing to be
   * precise. A turn measured against the real CLIs runs 12-40s, so a
   * minute of silence means something is wrong, not slow — and past that
   * the wearer is deciding whether to cancel, not reading digits. */
  private static readonly THINKING_PRECISE_S = 60;

  private armThinkingTicker(): void {
    if (this.thinkingTicker !== undefined) return;
    if (this.surface.kind !== 'brainstorm') return;
    if (!(this.visual?.transcriptIsThinking?.() ?? false)) {
      this.thinkingTicks = 0;
      return;
    }
    // Measured, not assumed: this costs ONE bridge write per tick. The
    // engine's old version shipped a whole frame every three seconds, so
    // at 1Hz this is 3x the writes — cheaper per update, more often, and
    // unbounded for as long as a turn stays silent. A hung collaborator
    // could hold the link at 1Hz indefinitely. So: one second while the
    // number is worth reading, five after that.
    this.thinkingTicks += 1;
    const interval = this.thinkingTicks < HudPresenter.THINKING_PRECISE_S
      ? 1000
      : 5000;
    this.thinkingTicker = setTimeout(() => {
      this.thinkingTicker = undefined;
      if (this.surface.kind !== 'brainstorm') return;
      // localTick: only the counter's digits moved, so this repaint is
      // NOT recorded as a wearer-visible frame receipt.
      void this.paintVisual(true);
    }, interval);
  }

  /** Swipe on the brainstorm/terminal surface pages through history. */
  scrollTranscript(direction: 'up' | 'down'): boolean {
    if (this.surface.kind !== 'brainstorm' || !this.visual) return false;
    if (!(this.visual.scrollTranscript?.(direction) ?? false)) return true;
    void this.paintVisual();
    return true;
  }

  async showBrainstorm(brainstorm: BrainstormHudModel, statusOverride?: string): Promise<boolean> {
    if (!this.visual || this.confirmation) return false;
    this.clearActionOverlay();
    const prior = this.capturePresenterSnapshot();
    const surface: HudSurface = {
      kind: 'brainstorm',
      brainstorm: this.cloneBrainstorm(brainstorm),
    };
    if (this.surface.kind === 'default' && !this.focusReturn) {
      this.focusReturn = {
        streams: new Map(this.streams),
        brightness: new Map(this.brightness),
        kinds: new Map(this.kinds),
      };
    }
    const liveStatus = statusOverride === undefined
      ? this.captureLiveStatus()
      : { text: statusOverride, brightness: 2 };
    this.resetAll();
    this.surface = surface;
    if (liveStatus) {
      this.streams.set(ContainerId.StatusBar, liveStatus.text);
      if (liveStatus.brightness !== undefined) {
        this.brightness.set(ContainerId.StatusBar, liveStatus.brightness);
      }
    }
    const painted = await this.paintVisual();
    if (!painted) {
      this.restorePresenterSnapshot(prior);
      await this.visual.paint(this.visualFrame());
    }
    return painted;
  }

  /** Native Calendar/Reminders lifecycle page. The preceding confirmation is
   * the only write authority; this surface only renders bridge evidence. */
  async showProductivity(
    productivity: ProductivityHudModel,
    statusOverride?: string,
    onDeferredProductivityVisible?: () => void,
  ): Promise<boolean> {
    if (!this.visual) return false;
    if (usesActionPillLayout(productivity.phase)) {
      return this.showActionPill(productivity, onDeferredProductivityVisible);
    }
    this.clearActionOverlay();
    const surface: HudSurface = {
      kind: 'productivity',
      productivity: this.cloneProductivity(productivity),
    };
    if (this.confirmation) {
      // Native read-back can start before the async confirmation dismissal
      // finishes its last tile. Defer the page rather than claiming a screen
      // paint which the wearer never saw; dismiss restores it atomically.
      this.confirmation.surface = surface;
      this.confirmation.onDeferredProductivityVisible = onDeferredProductivityVisible;
      return true;
    }
    const prior = this.capturePresenterSnapshot();
    if (this.surface.kind === 'default' && !this.focusReturn) {
      this.focusReturn = {
        streams: new Map(this.streams),
        brightness: new Map(this.brightness),
        kinds: new Map(this.kinds),
      };
    }
    const liveStatus = statusOverride === undefined
      ? this.captureLiveStatus()
      : { text: statusOverride, brightness: 2 };
    this.resetAll();
    this.surface = surface;
    if (liveStatus) {
      this.streams.set(ContainerId.StatusBar, liveStatus.text);
      if (liveStatus.brightness !== undefined) {
        this.brightness.set(ContainerId.StatusBar, liveStatus.brightness);
      }
    }
    const painted = await this.paintVisual();
    if (!painted) {
      this.restorePresenterSnapshot(prior);
      await this.visual.paint(this.visualFrame());
    }
    return painted;
  }

  /** Receipt strip over whatever page the wearer is on; no surface swap. */
  private async showActionPill(
    productivity: ProductivityHudModel,
    onVisible?: () => void,
  ): Promise<boolean> {
    this.clearActionOverlay();
    if (this.confirmation) {
      // A retained truth page under the gate is superseded by this newer
      // outcome — never restore FAILED beneath a SAVED strip.
      if (this.confirmation.surface.kind === 'productivity') {
        this.confirmation.surface = { kind: 'default' };
      }
      this.armActionOverlay(productivity);
      this.confirmation.onDeferredPillVisible = onVisible;
      return true;
    }
    if (this.surface.kind === 'productivity') {
      await this.showDefault();
    }
    const overlay = this.armActionOverlay(productivity);
    const painted = await this.paintVisual();
    if (!painted && this.actionOverlay === overlay) this.clearActionOverlay();
    return painted;
  }

  private armActionOverlay(productivity: ProductivityHudModel): ActionOverlayState {
    const overlay: ActionOverlayState = {
      productivity: this.cloneProductivity(productivity),
    };
    overlay.dismissTimer = setTimeout(() => {
      if (this.actionOverlay !== overlay) return;
      this.actionOverlay = undefined;
      void this.paintVisual();
    }, ACTION_PILL_FAILSAFE_MS);
    this.actionOverlay = overlay;
    return overlay;
  }

  private clearActionOverlay(): void {
    if (this.actionOverlay?.dismissTimer !== undefined) {
      clearTimeout(this.actionOverlay.dismissTimer);
    }
    this.actionOverlay = undefined;
  }

  /** Whether this exact productivity surface is currently retained after a
   * successful paint. A deferred confirmation or a failed paint is not a
   * visible surface and must not be used as a delivery acknowledgement. */
  isProductivityVisible(productivity: ProductivityHudModel): boolean {
    if (!this.confirmation && this.actionOverlay) {
      if (sameProductivity(this.actionOverlay.productivity, productivity)) return true;
    }
    if (this.surface.kind !== 'productivity') return false;
    return sameProductivity(this.surface.productivity, productivity);
  }

  /** Repaint the Planning dashboard with a new hover/pane focus. Local-only
   * state: no Mac request, one visual paint, and the unchanged-tile skip
   * keeps a hover move to the tiles whose pixels actually differ. */
  async updatePlanningPane(pane: PlanningPaneState): Promise<boolean> {
    if (this.surface.kind !== 'list' || !this.surface.options.planningGrid) {
      return false;
    }
    this.surface = {
      ...this.surface,
      options: { ...this.surface.options, planningPane: { ...pane } },
    };
    return this.paintVisual();
  }

  async setSelection(index: number): Promise<boolean> {
    if (
      this.surface.kind === 'default'
      || this.surface.kind === 'navigation'
      || this.surface.kind === 'brainstorm'
      || this.surface.kind === 'productivity'
    ) return false;
    const maximum = Math.max(0, this.surface.items.length - 1);
    const selectedIndex = Math.max(0, Math.min(maximum, index));
    const surface: HudSurface = { ...this.surface, selectedIndex };
    return this.renderList(surface);
  }

  moveSelection(delta: number): Promise<boolean> {
    if (
      this.surface.kind === 'default'
      || this.surface.kind === 'navigation'
      || this.surface.kind === 'brainstorm'
      || this.surface.kind === 'productivity'
    ) {
      return Promise.resolve(false);
    }
    return this.setSelection(this.surface.selectedIndex + delta);
  }

  private async renderNavigation(
    surface: Extract<HudSurface, { kind: 'navigation' }>,
    statusOverride?: string,
    prior?: PresenterSnapshot,
  ): Promise<boolean> {
    const from = this.tracePage();
    const liveStatus = statusOverride === undefined
      ? this.captureLiveStatus()
      : { text: statusOverride, brightness: 2 };
    this.resetAll();
    this.surface = surface;
    if (liveStatus) {
      this.streams.set(ContainerId.StatusBar, liveStatus.text);
      if (liveStatus.brightness !== undefined) {
        this.brightness.set(ContainerId.StatusBar, liveStatus.brightness);
      }
    }
    const painted = await this.paintVisual();
    if (!painted && prior) {
      this.restorePresenterSnapshot(prior);
      // paintVisual already spent its one bounded self-heal on the failed new
      // frame. Make one direct rollback paint so pixels and input ownership
      // converge on the same prior surface, but keep the caller result false.
      await this.visual?.paint(this.visualFrame());
    }
    this.recordPageTransition(from, 'navigation', 'show_navigation', painted ? 'success' : 'failed');
    return painted;
  }

  private async renderList(surface: Extract<HudSurface, { kind: 'list' }>): Promise<boolean> {
    const from = this.tracePage();
    const liveStatus = this.captureLiveStatus();
    const ok = this.visual
      ? true
      : await this.client.require()
        .rebuildPageContainer(this.layouts.listLayout(surface.items, surface.options));
    if (!ok) {
      this.recordPageTransition(from, 'list', 'show_list', 'failed');
      return false;
    }
    this.resetAll();
    this.surface = surface;
    // Mirror payload content in presenter state so confirmation restoration
    // and a same-layout self-heal retain the named focus page.
    this.streams.set(ContainerId.Main, surface.options.title);
    this.brightness.set(ContainerId.Main, 4);
    this.streams.set(ContainerId.Hint, surface.options.hint);
    this.brightness.set(ContainerId.Hint, 1);
    if (this.visual) {
      if (liveStatus) {
        this.streams.set(ContainerId.StatusBar, liveStatus.text);
        if (liveStatus.brightness !== undefined) {
          this.brightness.set(ContainerId.StatusBar, liveStatus.brightness);
        }
      }
      const painted = await this.paintVisual();
      this.recordPageTransition(from, 'list', 'show_list', painted ? 'success' : 'failed');
      return painted;
    }
    const restored = await this.restoreLiveStatus(liveStatus);
    this.recordPageTransition(from, 'list', 'show_list', restored ? 'success' : 'failed');
    return restored;
  }

  /** Adopt the Mac's margin facts and repaint if the dashboard is showing. */
  async setAmbient(
    ambient: { temperature: string; condition: string; unread: number },
  ): Promise<boolean> {
    const changed = this.ambient?.temperature !== ambient.temperature
      || this.ambient?.condition !== ambient.condition
      || this.ambient?.unread !== ambient.unread;
    this.ambient = ambient;
    if (!changed) return true;
    // Only the resting screen wears these; repainting a feature page for a
    // temperature would cost BLE transfers the wearer never asked for.
    if (this.surface.kind !== 'default') return true;
    if (!this.visual) return true;
    return this.paintVisual();
  }

  /** Which tab the overview's right pane shows. Local: the Mac never learns it. */
  activeTab(): PanelTab {
    return this.rail.active();
  }

  async setActiveTab(tab: PanelTab): Promise<boolean> {
    if (!this.rail.setActive(tab)) return true;
    return this.repaintRestingPane();
  }

  /** A swipe on the resting overview: one tab along the rail. Returns the
   * tab now active, or undefined at either end of the rail. */
  async stepTab(delta: number): Promise<PanelTab | undefined> {
    const next = this.rail.step(delta);
    if (next !== undefined) await this.repaintRestingPane();
    return next;
  }

  /** Cache a Mac push. Repaints only when the pushed tab is on the glass;
   * an expanded page for that tab is CapabilityMenu's to refresh. */
  async acceptPanel(message: HudPanelMessage): Promise<PanelContent> {
    const content = this.rail.accept(message);
    if (this.rail.active() === message.tab) await this.repaintRestingPane();
    return content;
  }

  /** Back to the first tab — what a fresh brain connection lands on. */
  resetTab(): void {
    this.rail.reset();
  }

  private async repaintRestingPane(): Promise<boolean> {
    if (this.confirmation) return true;
    if (this.surface.kind !== 'default') return true;
    if (!this.visual) return true;
    return this.paintVisual();
  }

  /**
   * Repaint the visible pomodoro exactly when its next digit moves: every
   * second on the expanded tab, every minute on the resting overview. A
   * tick is a localTick — no new information from the Mac — so it is not a
   * wearer-visible frame receipt, and it stops the moment the timer is not
   * on the glass or not running.
   */
  private armPanelTicker(): void {
    if (this.panelTicker !== undefined) return;
    const delay = this.panelTickDelay();
    if (delay === undefined) return;
    this.panelTicker = setTimeout(() => {
      this.panelTicker = undefined;
      if (this.panelTickDelay() === undefined) return;
      void this.paintVisual(true);
    }, delay);
  }

  private panelTickDelay(): number | undefined {
    const pomodoro = this.rail.content('pomodoro')?.pomodoro;
    if (!pomodoro) return undefined;
    const now = Date.now();
    if (this.surface.kind === 'default' && this.rail.active() === 'pomodoro') {
      return pomodoroTickDelayMs(pomodoro, now, 60_000);
    }
    if (this.surface.kind === 'list' && this.surface.options.railTab === 'pomodoro') {
      return pomodoroTickDelayMs(pomodoro, now, 1000);
    }
    return undefined;
  }

  private clearPanelTicker(): void {
    if (this.panelTicker === undefined) return;
    clearTimeout(this.panelTicker);
    this.panelTicker = undefined;
  }

  async showDefault(): Promise<boolean> {
    if (this.confirmation) {
      this.confirmation.surface = { kind: 'default' };
      if (this.focusReturn) {
        this.confirmation.streams = new Map(this.focusReturn.streams);
        this.confirmation.brightness = new Map(this.focusReturn.brightness);
        this.confirmation.kinds = new Map(this.focusReturn.kinds);
        this.focusReturn = undefined;
      }
      return true;
    }
    this.clearActionOverlay();
    const from = this.tracePage();
    const deferred = this.focusReturn;
    const liveStatus = this.captureLiveStatus();
    if (this.visual) {
      const content = deferred ?? {
        streams: new Map(this.streams),
        brightness: new Map(this.brightness),
        kinds: new Map(this.kinds),
      };
      this.resetAll();
      this.surface = { kind: 'default' };
      this.focusReturn = undefined;
      this.streams = new Map(content.streams);
      this.brightness = new Map(content.brightness);
      this.kinds = new Map(content.kinds);
      if (liveStatus) {
        this.streams.set(ContainerId.StatusBar, liveStatus.text);
        if (liveStatus.brightness !== undefined) {
          this.brightness.set(ContainerId.StatusBar, liveStatus.brightness);
        }
      }
      const painted = await this.paintVisual();
      this.recordPageTransition(from, 'overview', 'show_overview', painted ? 'success' : 'failed');
      return painted;
    }
    const ok = await this.client.require().rebuildPageContainer(this.layouts.defaultLayout());
    if (!ok) {
      this.recordPageTransition(from, 'overview', 'show_overview', 'failed');
      return false;
    }
    this.resetAll();
    this.surface = { kind: 'default' };
    this.focusReturn = undefined;
    let restored = await this.restoreLiveStatus(liveStatus);
    if (!deferred) {
      this.recordPageTransition(from, 'overview', 'show_overview', restored ? 'success' : 'failed');
      return restored;
    }

    for (const [id, text] of deferred.streams) {
      if (id !== ContainerId.StatusBar) this.streams.set(id, text);
    }
    for (const [id, level] of deferred.brightness) {
      if (id !== ContainerId.StatusBar) this.brightness.set(id, level);
    }
    for (const [id, kind] of deferred.kinds) {
      if (id !== ContainerId.StatusBar) this.kinds.set(id, kind);
    }
    for (const id of [
      ContainerId.Main,
      ContainerId.Context,
      ContainerId.Tray,
      ContainerId.Hint,
    ] as const) {
      if (this.streams.has(id)) restored = (await this.paint(id)) && restored;
    }
    this.recordPageTransition(from, 'overview', 'show_overview', restored ? 'success' : 'failed');
    return restored;
  }

  /**
   * The one exclusive app-owned decision surface.
   *
   * It keeps plugin-owned status visible, removes competing navigation and
   * content, and defers ordinary paints until the gate settles. Dismissal
   * rebuilds the exact prior surface rather than dropping the wearer at Home.
   */
  showConfirmation(
    prompt: string,
    id = 'hud:confirmation',
    diagnosticKind?: 'messages',
  ): Promise<boolean> {
    const priorPage = this.tracePage();
    const openedHere = this.confirmation === undefined;
    // A new decision gate supersedes any lingering outcome strip.
    this.clearActionOverlay();
    const safePrompt = prompt.trim() || 'Confirm requested action.';
    if (safePrompt.length > CONFIRM_PROMPT_MAX_UNITS) {
      console.error('[HudPresenter] exact confirmation preview exceeds HUD capacity');
      return Promise.resolve(false);
    }
    if (!this.confirmation) {
      const content = this.surface.kind !== 'default' && this.focusReturn
        ? this.focusReturn
        : { streams: this.streams, brightness: this.brightness, kinds: this.kinds };
      this.confirmation = {
        id,
        prompt: safePrompt,
        ...(diagnosticKind ? { diagnosticKind } : {}),
        surface: this.cloneSurface(this.surface),
        streams: new Map(content.streams),
        brightness: new Map(content.brightness),
        kinds: new Map(content.kinds),
      };
    } else {
      // Claim ownership synchronously, before an awaited bridge call. A stale
      // result callback can now only enqueue a no-op dismissal for its old id.
      this.confirmation.id = id;
      this.confirmation.prompt = safePrompt;
      this.confirmation.diagnosticKind = diagnosticKind;
    }

    return this.enqueueConfirmation(() => this.renderConfirmation(id, openedHere, priorPage));
  }

  private async renderConfirmation(
    id: string,
    openedHere = false,
    from: G2TracePage = this.tracePage(),
  ): Promise<boolean> {
    const current = this.confirmation;
    if (!current || current.id !== id) return true;
    const liveStatus = this.captureLiveStatus();
    if (this.visual) {
      this.resetAll();
      this.streams.set(ContainerId.Main, `CONFIRM ACTION\n\n${current.prompt}`);
      this.streams.set(ContainerId.Context, 'TAP · confirm   SWIPE · cancel');
      this.streams.set(ContainerId.Tray, '');
      this.streams.set(ContainerId.Hint, '8 seconds · timeout cancels');
      if (liveStatus) {
        this.streams.set(ContainerId.StatusBar, liveStatus.text);
        if (liveStatus.brightness !== undefined) {
          this.brightness.set(ContainerId.StatusBar, liveStatus.brightness);
        }
      }
      const painted = await this.paintVisual();
      if (!painted && openedHere && this.confirmation?.id === id) {
        // A failed first paint must not leave an invisible decision gate owning
        // every later content update. Restore the logical surface snapshot so
        // the next healthy frame can recover, while still returning false so
        // the IMU/tap gate never activates.
        this.confirmation = undefined;
        this.surface = this.cloneSurface(current.surface);
        this.streams = new Map(current.streams);
        this.brightness = new Map(current.brightness);
        this.kinds = new Map(current.kinds);
        // One failed tile can leave a torn confirmation visible even though
        // the logical gate has rolled back. Actively repaint the complete
        // prior frame before returning failure to the input gate.
        if (liveStatus) {
          this.streams.set(ContainerId.StatusBar, liveStatus.text);
          if (liveStatus.brightness !== undefined) {
            this.brightness.set(ContainerId.StatusBar, liveStatus.brightness);
          }
        }
        await this.paintVisual();
      }
      this.recordPageTransition(from, 'confirmation', 'show_confirmation', painted ? 'success' : 'failed');
      return painted;
    }
    const ok = await this.client.require()
      .rebuildPageContainer(this.layouts.confirmationLayout(current.prompt));
    if (this.confirmation?.id !== id) return true;
    if (!ok) {
      if (openedHere) {
        this.confirmation = undefined;
      }
      this.recordPageTransition(from, 'confirmation', 'show_confirmation', 'failed');
      return false;
    }
    this.resetAll();
    this.streams.set(ContainerId.Main, `CONFIRM ACTION\n\n${current.prompt}`);
    this.streams.set(ContainerId.Context, 'TAP · confirm   SWIPE · cancel');
    this.streams.set(ContainerId.Tray, '');
    this.streams.set(ContainerId.Hint, '8 seconds · timeout cancels');
    this.brightness.set(ContainerId.Main, 4);
    this.brightness.set(ContainerId.Context, 3);
    this.brightness.set(ContainerId.Tray, 3);
    this.brightness.set(ContainerId.Hint, 1);
    const restored = await this.restoreLiveStatus(liveStatus);
    this.recordPageTransition(from, 'confirmation', 'show_confirmation', restored ? 'success' : 'failed');
    return restored;
  }

  dismissConfirmation(id?: string): Promise<boolean> {
    return this.enqueueConfirmation(() => this.performDismissConfirmation(id));
  }

  private async performDismissConfirmation(id?: string): Promise<boolean> {
    const prior = this.confirmation;
    // A bound dismissal is also the write-authorization barrier. It must
    // distinguish a real dismissal from an already-replaced or expired modal
    // so an old gesture cannot authorize a different action in the background.
    if (!prior) return id === undefined;
    if (id !== undefined && prior.id !== id) return false;

    // Clear this synchronously before any awaited paint. A replacement
    // confirmation may be queued by a callback in the same event turn.
    this.confirmation = undefined;

    const result = await this.restoreAfterConfirmation(prior);
    // A pill deferred behind this gate rode along in the restore repaint.
    if (result && this.actionOverlay) prior.onDeferredPillVisible?.();
    return result;
  }

  private async restoreAfterConfirmation(prior: ConfirmationReturnState): Promise<boolean> {
    if (prior.surface.kind === 'list') {
      this.focusReturn = {
        streams: new Map(prior.streams),
        brightness: new Map(prior.brightness),
        kinds: new Map(prior.kinds),
      };
      const restored = await this.renderList(prior.surface);
      const result = await this.repaintReplacementConfirmation(restored);
      this.recordPageTransition('confirmation', 'list', 'dismiss_confirmation', result ? 'success' : 'failed');
      return result;
    }

    if (prior.surface.kind === 'navigation') {
      this.focusReturn = {
        streams: new Map(prior.streams),
        brightness: new Map(prior.brightness),
        kinds: new Map(prior.kinds),
      };
      const restored = await this.renderNavigation(prior.surface);
      const result = await this.repaintReplacementConfirmation(restored);
      this.recordPageTransition('confirmation', 'navigation', 'dismiss_confirmation', result ? 'success' : 'failed');
      return result;
    }

    if (prior.surface.kind === 'brainstorm') {
      this.focusReturn = {
        streams: new Map(prior.streams),
        brightness: new Map(prior.brightness),
        kinds: new Map(prior.kinds),
      };
      const restored = await this.showBrainstorm(prior.surface.brainstorm);
      const result = await this.repaintReplacementConfirmation(restored);
      this.recordPageTransition('confirmation', 'list', 'dismiss_confirmation', result ? 'success' : 'failed');
      return result;
    }

    if (prior.surface.kind === 'productivity') {
      this.focusReturn = {
        streams: new Map(prior.streams),
        brightness: new Map(prior.brightness),
        kinds: new Map(prior.kinds),
      };
      const restored = await this.showProductivity(prior.surface.productivity);
      if (restored && this.isProductivityVisible(prior.surface.productivity)) {
        prior.onDeferredProductivityVisible?.();
      }
      const result = await this.repaintReplacementConfirmation(restored);
      this.recordPageTransition('confirmation', 'list', 'dismiss_confirmation', result ? 'success' : 'failed');
      return result;
    }

    const liveStatus = this.captureLiveStatus();
    if (this.visual) {
      this.resetAll();
      this.surface = { kind: 'default' };
      this.streams = new Map(prior.streams);
      this.brightness = new Map(prior.brightness);
      this.kinds = new Map(prior.kinds);
      if (liveStatus) {
        this.streams.set(ContainerId.StatusBar, liveStatus.text);
        if (liveStatus.brightness !== undefined) {
          this.brightness.set(ContainerId.StatusBar, liveStatus.brightness);
        }
      }
      const result = await this.repaintReplacementConfirmation(await this.paintVisual());
      this.recordPageTransition('confirmation', 'overview', 'dismiss_confirmation', result ? 'success' : 'failed');
      return result;
    }
    const rebuilt = await this.client.require().rebuildPageContainer(this.layouts.defaultLayout());
    if (!rebuilt) {
      this.recordPageTransition('confirmation', 'overview', 'dismiss_confirmation', 'failed');
      return false;
    }
    this.resetAll();
    this.surface = { kind: 'default' };
    for (const id of this.pendingPaint.keys()) this.cancelPending(id);

    // Status can keep changing while the gate is open. Preserve that live
    // plugin-owned line rather than restoring a stale snapshot.
    this.streams = new Map(prior.streams);
    this.brightness = new Map(prior.brightness);
    this.kinds = new Map(prior.kinds);
    if (liveStatus) {
      this.streams.set(ContainerId.StatusBar, liveStatus.text);
      if (liveStatus.brightness !== undefined) {
        this.brightness.set(ContainerId.StatusBar, liveStatus.brightness);
      }
    }

    let ok = true;
    for (const id of [
      ContainerId.Main,
      ContainerId.Context,
      ContainerId.Tray,
      ContainerId.Hint,
    ] as const) {
      this.streams.set(id, this.streams.get(id) ?? '');
      this.brightness.set(id, this.brightness.get(id) ?? DEFAULT_BRIGHTNESS[id]!);
      ok = (await this.paint(id)) && ok;
    }
    const result = await this.repaintReplacementConfirmation(ok);
    this.recordPageTransition('confirmation', 'overview', 'dismiss_confirmation', result ? 'success' : 'failed');
    return result;
  }

  /**
   * Rebuild the page and retry once when a paint reports failure.
   *
   * textContainerUpgrade returns false when the container does not exist —
   * which happens whenever the host already had a startup page and our
   * createStartUpPageContainer therefore returned `invalid` (it is one-shot
   * PER HOST SESSION, not per page load: a WebView reload, a relaunch, or a
   * second plugin instance all land here). BridgeClient's rebuild fallback
   * reports success without necessarily materialising the containers, so
   * every later paint failed SILENTLY and the HUD went dead for the rest of
   * the session — observed in the simulator as 382 consecutive
   * "container N not found" warnings behind a blank display.
   *
   * Every paint result is now checked, so any cause of a missing page
   * self-heals on the next write.
   */
  private healing = false;

  private async healAndRetry(containerId: number): Promise<boolean> {
    if (this.healing) {
      this.recordSelfHeal('skipped', containerId);
      return false; // one attempt; never recurse
    }
    this.healing = true;
    this.recordSelfHeal('attempt', containerId);
    try {
      const layout = this.visual
        ? this.layouts.visualLayout()
        : this.confirmation
        ? this.layouts.confirmationLayout(this.confirmation.prompt)
        : this.surface.kind === 'list'
          ? this.layouts.listLayout(this.surface.items, this.surface.options)
          : this.layouts.defaultLayout();
      const rebuilt = await this.client.require().rebuildPageContainer(layout);
      if (!rebuilt) {
        this.recordSelfHeal('abandoned', containerId);
        return false;
      }
      console.warn('[HudPresenter] page was missing — rebuilt and repainting');
      const recovered = this.visual ? await this.paintVisual() : await this.paint(containerId);
      this.recordSelfHeal(recovered ? 'recovered' : 'abandoned', containerId);
      return recovered;
    } catch {
      this.recordSelfHeal('abandoned', containerId);
      return false;
    } finally {
      this.healing = false;
    }
  }

  private async paint(containerId: number): Promise<boolean> {
    if (this.visual) return this.paintVisual();
    const full = this.streams.get(containerId) ?? '';
    // Overflow keeps the TAIL: mid-stream, the newest words are the ones
    // being read; the opening lines are already consumed.
    const content = full.length > MAX_UPGRADE_CHARS ? full.slice(-MAX_UPGRADE_CHARS) : full;
    const previous = this.lastLength.get(containerId) ?? 0;
    this.lastPaint.set(containerId, Date.now());
    const level = this.brightness.get(containerId);
    const ok = await this.client.require().textContainerUpgrade(
      new TextContainerUpgrade({
        containerID: containerId,
        contentOffset: 0,
        // Overwrite whatever was there before, otherwise stale tail text survives.
        contentLength: Math.max(previous, content.length),
        content,
        // Omitted = keep the container's current brightness (SDK contract).
        ...(level !== undefined ? { textColor: level } : {}),
      }),
    );
    this.lastLength.set(containerId, content.length);
    if (!ok && !this.healing) {
      // The container is gone. Rebuild the page and paint again, rather
      // than dropping this and every future write on the floor.
      return this.healAndRetry(containerId);
    }
    return ok;
  }

  private tracePage(): G2TracePage {
    if (this.confirmation) return 'confirmation';
    if (this.surface.kind === 'default') return 'overview';
    // The trace vocabulary predates this visual page; it remains content-free.
    return this.surface.kind === 'brainstorm' || this.surface.kind === 'productivity'
      ? 'list'
      : this.surface.kind;
  }

  private recordPageTransition(
    from: G2TracePage,
    to: G2TracePage,
    reason: 'show_list' | 'show_overview' | 'show_confirmation' | 'show_navigation' | 'dismiss_confirmation',
    outcome: 'success' | 'failed',
  ): void {
    if (from === to) return;
    this.diagnostics?.record({ type: 'page_transition', from, to, reason, outcome });
  }

  private recordSelfHeal(
    decision: 'attempt' | 'recovered' | 'abandoned' | 'skipped',
    containerId: number,
  ): void {
    this.diagnostics?.record({
      type: 'self_heal',
      surface: this.tracePage(),
      target: this.visual ? 'visual' : 'text',
      decision,
      containerId,
    });
  }

  private cancelPending(containerId: number): void {
    const timer = this.pendingPaint.get(containerId);
    if (timer !== undefined) {
      clearTimeout(timer);
      this.pendingPaint.delete(containerId);
    }
  }

  private deferText(
    text: string,
    containerId: ContainerId,
    textColor?: number,
    contentKind?: HudContentKind,
  ): void {
    const deferred = this.confirmation;
    if (!deferred) return;
    deferred.streams.set(containerId, text);
    if (textColor !== undefined) deferred.brightness.set(containerId, textColor);
    else deferred.brightness.delete(containerId);
    if (containerId === ContainerId.Main) {
      deferred.kinds.set(containerId, contentKind ?? 'assistant');
    }
  }

  private deferFocusText(
    text: string,
    containerId: ContainerId,
    textColor?: number,
    contentKind?: HudContentKind,
  ): void {
    const deferred = this.ensureFocusReturn();
    deferred.streams.set(containerId, text);
    if (textColor !== undefined) deferred.brightness.set(containerId, textColor);
    else deferred.brightness.delete(containerId);
    if (containerId === ContainerId.Main) {
      deferred.kinds.set(containerId, contentKind ?? 'assistant');
    }
  }

  private ensureFocusReturn(): HudContentState {
    this.focusReturn ??= { streams: new Map(), brightness: new Map(), kinds: new Map() };
    return this.focusReturn;
  }

  private captureLiveStatus(): { text: string; brightness?: number } | undefined {
    const text = this.streams.get(ContainerId.StatusBar);
    if (text === undefined) return undefined;
    return { text, brightness: this.brightness.get(ContainerId.StatusBar) };
  }

  private async restoreLiveStatus(
    status: { text: string; brightness?: number } | undefined,
  ): Promise<boolean> {
    if (!status) return true;
    this.streams.set(ContainerId.StatusBar, status.text);
    if (status.brightness !== undefined) {
      this.brightness.set(ContainerId.StatusBar, status.brightness);
    }
    return this.paint(ContainerId.StatusBar);
  }

  private enqueueConfirmation(operation: () => Promise<boolean>): Promise<boolean> {
    const next = this.confirmationQueue.then(operation, operation);
    this.confirmationQueue = next.then(() => undefined, () => undefined);
    return next;
  }

  private async repaintReplacementConfirmation(restored: boolean): Promise<boolean> {
    const replacement = this.confirmation;
    if (!replacement) return restored;
    // The queued dismissal belongs to the confirmation that was visible when
    // its gesture began. If another confirmation claimed the decision surface
    // while the prior page was restoring, render that replacement but report
    // the old dismissal as unsuccessful. Returning success here would let an
    // old tap authorize action A after the wearer is already reviewing B.
    await this.renderConfirmation(replacement.id);
    return false;
  }

  private resolveListOptions(options: ListSurfaceOptions): ResolvedListSurfaceOptions {
    const planningGrid = normalizePlanningGrid(options.planningGrid);
    return {
      title: (options.title?.trim() || DEFAULT_LIST_TITLE).slice(0, MAX_FOCUS_COPY_CHARS),
      hint: (options.hint?.trim() || DEFAULT_LIST_HINT).slice(0, MAX_FOCUS_COPY_CHARS),
      selectable: options.selectable ?? false,
      ...(options.railTab ? { railTab: options.railTab } : {}),
      ...(planningGrid ? { planningGrid } : {}),
      // The pane rides only with a valid grid: hover state without dates
      // would put the renderer on the dashboard path with nothing to draw.
      ...(planningGrid && options.planningPane
        ? { planningPane: { ...options.planningPane } }
        : {}),
    };
  }

  private visualFrame(): HudVisualFrame {
    const status = this.streams.get(ContainerId.StatusBar) ?? '';
    if (this.confirmation) {
      return {
        kind: 'confirmation',
        status,
        micOn: this.micOn,
        prompt: this.confirmation.prompt,
        reviewDomain: this.productivityReviewDomain(this.confirmation.prompt),
        ...(this.confirmation.diagnosticKind
          ? { diagnosticKind: this.confirmation.diagnosticKind }
          : {}),
      };
    }
    if (this.surface.kind === 'list') {
      const railTab = this.surface.options.railTab;
      return this.withOverlays({
        kind: 'list',
        status,
        micOn: this.micOn,
        title: this.surface.options.title,
        items: [...this.surface.items],
        selectedIndex: this.surface.selectedIndex,
        selectable: this.surface.options.selectable,
        hint: this.surface.options.hint,
        planningGrid: this.surface.options.planningGrid,
        planningPane: this.surface.options.planningPane,
        ...(railTab ? { rail: this.rail.visualFor(railTab) } : {}),
      });
    }
    if (this.surface.kind === 'navigation') {
      return this.withOverlays({
        kind: 'navigation',
        status,
        micOn: this.micOn,
        navigation: this.cloneNavigation(this.surface.navigation),
      });
    }
    if (this.surface.kind === 'brainstorm') {
      return this.withOverlays({
        kind: 'brainstorm',
        status,
        micOn: this.micOn,
        brainstorm: this.cloneBrainstorm(this.surface.brainstorm),
      });
    }
    if (this.surface.kind === 'productivity') {
      return this.withOverlays({
        kind: 'productivity',
        status,
        micOn: this.micOn,
        productivity: this.cloneProductivity(this.surface.productivity),
      });
    }
    return this.withOverlays({
      kind: 'overview',
      status,
      micOn: this.micOn,
      ...(this.ambient ? { ambient: this.ambient } : {}),
      mainBrightness: this.brightness.get(ContainerId.Main)
        ?? DEFAULT_BRIGHTNESS[ContainerId.Main]!,
      main: this.streams.get(ContainerId.Main) ?? '',
      mainKind: this.kinds.get(ContainerId.Main),
      context: this.streams.get(ContainerId.Context) ?? '',
      tray: this.streams.get(ContainerId.Tray) ?? '',
      hint: this.streams.get(ContainerId.Hint) ?? '',
      rail: this.rail.visual(),
    });
  }

  private withOverlays<T extends HudVisualFrame>(frame: T): T {
    let out = frame;
    if (this.actionOverlay) {
      out = {
        ...out,
        actionOverlay: {
          productivity: this.cloneProductivity(this.actionOverlay.productivity),
        },
      };
    }
    if (this.assistantOverlay) {
      out = { ...out, assistantOverlay: { text: this.assistantOverlay.text } };
    }
    return out;
  }

  /** Paint the surface adopted from a background snapshot. Exactly one paint:
   * the reloaded page already drew its startup frame, and the BLE lane is
   * serialized, so a second repaint would queue behind this one for nothing. */
  async repaintRestoredSurface(): Promise<boolean> {
    return this.paintVisual();
  }

  private async paintVisual(localTick = false): Promise<boolean> {
    if (!this.visual) return false;
    const ok = await this.visual.paint(this.visualFrame(), localTick);
    // Keep the local elapsed counter moving between Mac frames.
    this.armThinkingTicker();
    this.armPanelTicker();
    if (!ok && !this.healing) return this.healAndRetry(ContainerId.Main);
    return ok;
  }

  private cloneSurface(surface: HudSurface): HudSurface {
    if (surface.kind === 'default') return { kind: 'default' };
    if (surface.kind === 'navigation') {
      return { kind: 'navigation', navigation: this.cloneNavigation(surface.navigation) };
    }
    if (surface.kind === 'brainstorm') {
      return { kind: 'brainstorm', brainstorm: this.cloneBrainstorm(surface.brainstorm) };
    }
    if (surface.kind === 'productivity') {
      return { kind: 'productivity', productivity: this.cloneProductivity(surface.productivity) };
    }
    return {
      kind: 'list',
      items: [...surface.items],
      options: {
        ...surface.options,
        ...(surface.options.planningGrid
          ? { planningGrid: surface.options.planningGrid.map((cell) => ({ ...cell })) }
          : {}),
      },
      selectedIndex: surface.selectedIndex,
    };
  }

  private cloneContentState(content: HudContentState): HudContentState {
    return {
      streams: new Map(content.streams),
      brightness: new Map(content.brightness),
      kinds: new Map(content.kinds),
    };
  }

  /**
   * A JSON-safe projection of everything the wearer is currently looking at.
   *
   * The host's background migration reloads the plugin URL into a fresh
   * WebView, so anything held only in memory is gone on return. The internal
   * snapshot uses Maps, which do not survive a JSON round trip, so the
   * container keys become plain records here.
   */
  exportBackgroundState(): Record<string, unknown> {
    return {
      surface: this.cloneSurface(this.surface),
      streams: Object.fromEntries(this.streams),
      brightness: Object.fromEntries(this.brightness),
      kinds: Object.fromEntries(this.kinds),
      micOn: this.micOn,
      rail: this.rail.export(),
    };
  }

  /**
   * Adopt a snapshot from the page this one replaced, WITHOUT painting.
   *
   * The caller repaints once afterwards; painting here would race the startup
   * frame on the serialized BLE lane. Unknown or malformed fields are skipped
   * rather than trusted — a corrupt snapshot must not brick the surface.
   */
  importBackgroundState(saved: Record<string, unknown>): boolean {
    const surface = saved.surface;
    if (typeof surface !== 'object' || surface === null) return false;
    const kind = (surface as { kind?: unknown }).kind;
    if (typeof kind !== 'string') return false;
    this.surface = this.cloneSurface(surface as HudSurface);
    this.streams = numericMap(saved.streams, (v): v is string => typeof v === 'string');
    this.brightness = numericMap(saved.brightness, (v): v is number => typeof v === 'number');
    this.kinds = numericMap(
      saved.kinds,
      (v): v is HudContentKind => typeof v === 'string',
    );
    if (typeof saved.micOn === 'boolean') this.micOn = saved.micOn;
    if (typeof saved.rail === 'object' && saved.rail !== null) {
      this.rail.import(saved.rail as Record<string, unknown>);
    }
    return true;
  }

  private capturePresenterSnapshot(): PresenterSnapshot {
    return {
      surface: this.cloneSurface(this.surface),
      streams: new Map(this.streams),
      brightness: new Map(this.brightness),
      kinds: new Map(this.kinds),
      ...(this.focusReturn ? { focusReturn: this.cloneContentState(this.focusReturn) } : {}),
    };
  }

  private restorePresenterSnapshot(snapshot: PresenterSnapshot): void {
    this.surface = this.cloneSurface(snapshot.surface);
    this.streams = new Map(snapshot.streams);
    this.brightness = new Map(snapshot.brightness);
    this.kinds = new Map(snapshot.kinds);
    this.focusReturn = snapshot.focusReturn
      ? this.cloneContentState(snapshot.focusReturn)
      : undefined;
  }

  private cloneNavigation(navigation: NavigationHudModel): NavigationHudModel {
    return { ...navigation, options: [...navigation.options] };
  }

  private cloneBrainstorm(brainstorm: BrainstormHudModel): BrainstormHudModel {
    return { ...brainstorm, collaborators: [...brainstorm.collaborators] };
  }

  private cloneProductivity(productivity: ProductivityHudModel): ProductivityHudModel {
    return { ...productivity };
  }

  private productivityReviewDomain(prompt: string): ProductivityHudDomain | undefined {
    const normalized = prompt.trim().toLowerCase();
    if (normalized.startsWith('create event ')) return 'calendar';
    if (normalized.startsWith('create reminder ') || normalized.startsWith('complete reminder ')) {
      return 'reminders';
    }
    return undefined;
  }

  private resetAll(): void {
    for (const id of this.pendingPaint.keys()) this.cancelPending(id);
    this.clearPanelTicker();
    this.lastLength.clear();
    this.streams.clear();
    this.lastPaint.clear();
    this.brightness.clear();
    this.kinds.clear();
  }
}
