import { TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import type { HudPageBuilder, SidebarBackdrop } from './HudPageBuilder';
import type { AssistantOverlayVisual, HudFramePainter, HudVisualFrame } from './HudVisualSurface';
import type { G2DiagnosticTrace, G2TracePage } from './G2DiagnosticTrace';
import type { NavigationHudModel } from './NavigationHud';
import {
  CONFIRM_PROMPT_MAX_UNITS,
  ContainerId,
  type BrainstormHudModel,
  type HudContentKind,
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
function normalizePlanningGrid(
  planningGrid: readonly PlanningGridCell[] | undefined,
): PlanningGridCell[] | undefined {
  if (!planningGrid || planningGrid.length !== 42) return undefined;
  const normalized = planningGrid.map((cell) => ({
    day: cell.day,
    eventCount: cell.eventCount,
    reminderCount: cell.reminderCount,
    inMonth: cell.inMonth,
  }));
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

export interface ListSurfaceOptions {
  title?: string;
  hint?: string;
  selectable?: boolean;
  planningGrid?: readonly PlanningGridCell[];
}

interface ResolvedListSurfaceOptions {
  title: string;
  hint: string;
  selectable: boolean;
  planningGrid?: readonly PlanningGridCell[];
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
  | { kind: 'sidebar'; items: string[]; selectedIndex: number }
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
  surface: HudSurface;
}

interface AssistantOverlayState extends AssistantOverlayVisual {
  dismissTimer?: ReturnType<typeof setTimeout>;
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
  private focusReturn?: HudContentState;
  private confirmation?: ConfirmationReturnState;
  private confirmationQueue: Promise<unknown> = Promise.resolve();
  private confirmationStartedAt?: number;
  private confirmationTimer?: ReturnType<typeof setInterval>;
  private assistantOverlay?: AssistantOverlayState;
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
  async showAssistantOverlay(text: string, append = false): Promise<boolean> {
    if (!this.visual || !text) return false;
    const current = this.assistantOverlay;
    if (current?.dismissTimer !== undefined) clearTimeout(current.dismissTimer);
    this.assistantOverlay = { text: append ? `${current?.text ?? ''}${text}` : text };
    if (this.confirmation) return true;
    return this.paintVisual();
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
    return this.paintVisual();
  }

  /** The transient reply owns ordinary input while it is readable, so a
   * wearer cannot accidentally activate the obscured feature underneath. */
  hasAssistantOverlay(): boolean {
    return this.assistantOverlay !== undefined;
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
    return this.renderList(surface);
  }

  async showSidebar(items: string[], selectedIndex = 0): Promise<boolean> {
    const surface: HudSurface = { kind: 'sidebar', items: [...items], selectedIndex };
    if (this.confirmation) {
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
    return this.renderSidebar(surface);
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
    return this.renderNavigation(surface, statusOverride, prior);
  }

  /** Dedicated four-tile production page for a live Brainstorm or Hermes. */
  async showBrainstorm(brainstorm: BrainstormHudModel, statusOverride?: string): Promise<boolean> {
    if (!this.visual || this.confirmation) return false;
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
  ): Promise<boolean> {
    if (!this.visual) return false;
    const surface: HudSurface = {
      kind: 'productivity',
      productivity: this.cloneProductivity(productivity),
    };
    if (this.confirmation) {
      // Native read-back can start before the async confirmation dismissal
      // finishes its last tile. Defer the page rather than claiming a screen
      // paint which the wearer never saw; dismiss restores it atomically.
      this.confirmation.surface = surface;
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

  async setSelection(index: number): Promise<boolean> {
    if (
      this.surface.kind === 'default'
      || this.surface.kind === 'navigation'
      || this.surface.kind === 'brainstorm'
      || this.surface.kind === 'productivity'
    ) return false;
    const maximum = Math.max(0, this.surface.items.length - 1);
    const selectedIndex = Math.max(0, Math.min(maximum, index));
    if (this.surface.kind === 'sidebar') {
      const surface: HudSurface = { ...this.surface, selectedIndex };
      return this.renderSidebar(surface);
    }
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

  private async renderSidebar(
    surface: Extract<HudSurface, { kind: 'sidebar' }>,
    content: HudContentState = this.focusReturn ?? {
      streams: this.streams,
      brightness: this.brightness,
      kinds: this.kinds,
    },
  ): Promise<boolean> {
    const from = this.tracePage();
    const liveStatus = this.captureLiveStatus();
    const backdrop = this.sidebarBackdrop(content);
    const ok = this.visual
      ? true
      : await this.client.require()
        .rebuildPageContainer(this.layouts.sidebarLayout(surface.items, backdrop));
    if (!ok) {
      this.recordPageTransition(from, 'sidebar', 'show_sidebar', 'failed');
      return false;
    }
    this.resetAll();
    this.surface = surface;
    this.streams.set(ContainerId.Main, backdrop.main ?? '');
    const mainKind = content.kinds.get(ContainerId.Main);
    if (mainKind) this.kinds.set(ContainerId.Main, mainKind);
    this.streams.set(ContainerId.Context, backdrop.context ?? '');
    this.streams.set(ContainerId.Tray, backdrop.tray ?? '');
    this.streams.set(ContainerId.Hint, backdrop.hint ?? 'left x2 overview');
    this.brightness.set(
      ContainerId.Main,
      content.brightness.get(ContainerId.Main) ?? DEFAULT_BRIGHTNESS[ContainerId.Main]!,
    );
    this.brightness.set(
      ContainerId.Context,
      content.brightness.get(ContainerId.Context) ?? DEFAULT_BRIGHTNESS[ContainerId.Context]!,
    );
    this.brightness.set(
      ContainerId.Tray,
      content.brightness.get(ContainerId.Tray) ?? DEFAULT_BRIGHTNESS[ContainerId.Tray]!,
    );
    this.brightness.set(ContainerId.Hint, DEFAULT_BRIGHTNESS[ContainerId.Hint]!);
    if (this.visual) {
      if (liveStatus) {
        this.streams.set(ContainerId.StatusBar, liveStatus.text);
        if (liveStatus.brightness !== undefined) {
          this.brightness.set(ContainerId.StatusBar, liveStatus.brightness);
        }
      }
      const painted = await this.paintVisual();
      this.recordPageTransition(from, 'sidebar', 'show_sidebar', painted ? 'success' : 'failed');
      return painted;
    }
    const restored = await this.restoreLiveStatus(liveStatus);
    this.recordPageTransition(from, 'sidebar', 'show_sidebar', restored ? 'success' : 'failed');
    return restored;
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
  showConfirmation(prompt: string, id = 'hud:confirmation'): Promise<boolean> {
    const priorPage = this.tracePage();
    const openedHere = this.confirmation === undefined;
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
    }

    if (this.visual) this.stopVisualConfirmationCountdown();

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
      if (painted && this.confirmation?.id === id) {
        // Match the IMU/tap gate: its eight seconds begin only after this
        // promise resolves, so the visible countdown begins after the first
        // complete four-tile decision frame as well.
        this.startVisualConfirmationCountdown(id);
      }
      if (!painted && openedHere && this.confirmation?.id === id) {
        // A failed first paint must not leave an invisible decision gate owning
        // every later content update. Restore the logical surface snapshot so
        // the next healthy frame can recover, while still returning false so
        // the IMU/tap gate never activates.
        this.confirmation = undefined;
        this.stopVisualConfirmationCountdown();
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
        this.stopVisualConfirmationCountdown();
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
    this.stopVisualConfirmationCountdown();

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

    if (prior.surface.kind === 'sidebar') {
      this.focusReturn = {
        streams: new Map(prior.streams),
        brightness: new Map(prior.brightness),
        kinds: new Map(prior.kinds),
      };
      const restored = await this.renderSidebar(prior.surface, prior);
      const result = await this.repaintReplacementConfirmation(restored);
      this.recordPageTransition('confirmation', 'sidebar', 'dismiss_confirmation', result ? 'success' : 'failed');
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
          : this.surface.kind === 'sidebar'
            ? this.layouts.sidebarLayout(this.surface.items, this.sidebarBackdrop(
              this.focusReturn ?? {
                streams: this.streams,
                brightness: this.brightness,
                kinds: this.kinds,
              },
            ))
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
    reason: 'show_sidebar' | 'show_list' | 'show_overview' | 'show_confirmation' | 'show_navigation' | 'dismiss_confirmation',
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
    return (await this.renderConfirmation(replacement.id)) && restored;
  }

  private resolveListOptions(options: ListSurfaceOptions): ResolvedListSurfaceOptions {
    const planningGrid = normalizePlanningGrid(options.planningGrid);
    return {
      title: (options.title?.trim() || DEFAULT_LIST_TITLE).slice(0, MAX_FOCUS_COPY_CHARS),
      hint: (options.hint?.trim() || DEFAULT_LIST_HINT).slice(0, MAX_FOCUS_COPY_CHARS),
      selectable: options.selectable ?? false,
      ...(planningGrid ? { planningGrid } : {}),
    };
  }

  private sidebarBackdrop(content: HudContentState): SidebarBackdrop {
    return {
      main: content.streams.get(ContainerId.Main),
      context: content.streams.get(ContainerId.Context),
      tray: content.streams.get(ContainerId.Tray),
      hint: 'left x2 overview',
    };
  }

  private visualFrame(): HudVisualFrame {
    const status = this.streams.get(ContainerId.StatusBar) ?? '';
    if (this.confirmation) {
      const elapsed = this.confirmationStartedAt === undefined
        ? 0
        : Math.floor((Date.now() - this.confirmationStartedAt) / 1000);
      return {
        kind: 'confirmation',
        status,
        micOn: this.micOn,
        prompt: this.confirmation.prompt,
        secondsRemaining: Math.max(0, 8 - elapsed),
        reviewDomain: this.productivityReviewDomain(this.confirmation.prompt),
      };
    }
    if (this.surface.kind === 'sidebar') {
      return this.withAssistantOverlay({
        kind: 'sidebar',
        status,
        micOn: this.micOn,
        mainBrightness: this.brightness.get(ContainerId.Main)
          ?? DEFAULT_BRIGHTNESS[ContainerId.Main]!,
        items: [...this.surface.items],
        selectedIndex: this.surface.selectedIndex,
        main: this.streams.get(ContainerId.Main) ?? '',
        mainKind: this.kinds.get(ContainerId.Main),
        context: this.streams.get(ContainerId.Context) ?? '',
        tray: this.streams.get(ContainerId.Tray) ?? '',
        hint: this.streams.get(ContainerId.Hint) ?? '',
      });
    }
    if (this.surface.kind === 'list') {
      return this.withAssistantOverlay({
        kind: 'list',
        status,
        micOn: this.micOn,
        title: this.surface.options.title,
        items: [...this.surface.items],
        selectedIndex: this.surface.selectedIndex,
        selectable: this.surface.options.selectable,
        hint: this.surface.options.hint,
        planningGrid: this.surface.options.planningGrid,
      });
    }
    if (this.surface.kind === 'navigation') {
      return this.withAssistantOverlay({
        kind: 'navigation',
        status,
        micOn: this.micOn,
        navigation: this.cloneNavigation(this.surface.navigation),
      });
    }
    if (this.surface.kind === 'brainstorm') {
      return this.withAssistantOverlay({
        kind: 'brainstorm',
        status,
        micOn: this.micOn,
        brainstorm: this.cloneBrainstorm(this.surface.brainstorm),
      });
    }
    if (this.surface.kind === 'productivity') {
      return this.withAssistantOverlay({
        kind: 'productivity',
        status,
        micOn: this.micOn,
        productivity: this.cloneProductivity(this.surface.productivity),
      });
    }
    return this.withAssistantOverlay({
      kind: 'overview',
      status,
      micOn: this.micOn,
      mainBrightness: this.brightness.get(ContainerId.Main)
        ?? DEFAULT_BRIGHTNESS[ContainerId.Main]!,
      main: this.streams.get(ContainerId.Main) ?? '',
      mainKind: this.kinds.get(ContainerId.Main),
      context: this.streams.get(ContainerId.Context) ?? '',
      tray: this.streams.get(ContainerId.Tray) ?? '',
      hint: this.streams.get(ContainerId.Hint) ?? '',
    });
  }

  private withAssistantOverlay<T extends HudVisualFrame>(frame: T): T {
    const overlay = this.assistantOverlay;
    if (!overlay) return frame;
    return { ...frame, assistantOverlay: { text: overlay.text } };
  }

  private async paintVisual(): Promise<boolean> {
    if (!this.visual) return false;
    const ok = await this.visual.paint(this.visualFrame());
    if (!ok && !this.healing) return this.healAndRetry(ContainerId.Main);
    return ok;
  }

  private startVisualConfirmationCountdown(id: string): void {
    this.stopVisualConfirmationCountdown();
    this.confirmationStartedAt = Date.now();
    this.confirmationTimer = setInterval(() => {
      if (this.confirmation?.id !== id) {
        this.stopVisualConfirmationCountdown();
        return;
      }
      void this.paintVisual();
    }, 1000);
  }

  private stopVisualConfirmationCountdown(): void {
    if (this.confirmationTimer !== undefined) clearInterval(this.confirmationTimer);
    this.confirmationTimer = undefined;
    this.confirmationStartedAt = undefined;
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
    if (surface.kind === 'sidebar') {
      return {
        kind: 'sidebar', items: [...surface.items], selectedIndex: surface.selectedIndex,
      };
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
    this.lastLength.clear();
    this.streams.clear();
    this.lastPaint.clear();
    this.brightness.clear();
    this.kinds.clear();
  }
}
