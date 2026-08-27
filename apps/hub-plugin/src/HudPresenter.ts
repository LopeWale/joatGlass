import { TextContainerUpgrade } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import type { HudPageBuilder } from './HudPageBuilder';
import { CONFIRM_HINT, ContainerId } from './types';

const MAX_UPGRADE_CHARS = 2000;

/**
 * Repaint throttle for streamed text. Tokens arrive far faster than BLE can
 * move frames to the glasses; painting per token floods the link and, on
 * real hardware, rendered as flashing fragments ("cutting out, one line").
 * ~3 paints/second reads as live streaming while keeping BLE sane.
 */
const PAINT_INTERVAL_MS = 300;

const DEFAULT_LIST_TITLE = 'JOAT';
const DEFAULT_LIST_HINT = 'left x2 back';
const MAX_FOCUS_COPY_CHARS = 46;
const MAX_CONFIRM_PROMPT_CHARS = 120;

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
}

interface ResolvedListSurfaceOptions {
  title: string;
  hint: string;
}

interface HudContentState {
  streams: Map<number, string>;
  brightness: Map<number, number>;
}

type HudSurface =
  | { kind: 'default' }
  | { kind: 'list'; items: string[]; options: ResolvedListSurfaceOptions };

interface ConfirmationReturnState extends HudContentState {
  id: string;
  prompt: string;
  surface: HudSurface;
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
  private surface: HudSurface = { kind: 'default' };
  private focusReturn?: HudContentState;
  private confirmation?: ConfirmationReturnState;
  private confirmationQueue: Promise<unknown> = Promise.resolve();

  constructor(
    private readonly client: BridgeClient,
    private readonly layouts: HudPageBuilder,
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
  ): Promise<boolean> {
    if (this.confirmation && containerId !== ContainerId.StatusBar) {
      this.deferText(text, containerId, textColor);
      return true;
    }
    // A focus page owns the entire non-status canvas. Background replies,
    // transcripts, tray updates, and Chrome's dashboard hint are buffered
    // until Back instead of overwriting its header or targeting containers
    // that intentionally do not exist in this layout.
    if (this.surface.kind === 'list' && containerId !== ContainerId.StatusBar) {
      this.deferFocusText(text, containerId, textColor);
      return true;
    }
    return this.replaceText(text, containerId, textColor);
  }

  private async replaceText(
    text: string,
    containerId: ContainerId,
    textColor?: number,
  ): Promise<boolean> {
    this.cancelPending(containerId);
    this.streams.set(containerId, text);
    if (textColor !== undefined) {
      this.brightness.set(containerId, textColor);
    } else {
      this.brightness.delete(containerId);
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
    if (this.surface.kind === 'list' && containerId !== ContainerId.StatusBar) {
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

  async showList(items: string[], options: ListSurfaceOptions = {}): Promise<boolean> {
    const resolved = this.resolveListOptions(options);
    const surface: HudSurface = { kind: 'list', items: [...items], options: resolved };
    if (this.confirmation) {
      if (this.confirmation.surface.kind === 'default' && !this.focusReturn) {
        this.focusReturn = {
          streams: new Map(this.confirmation.streams),
          brightness: new Map(this.confirmation.brightness),
        };
      }
      this.confirmation.surface = surface;
      return true;
    }
    if (this.surface.kind === 'default' && !this.focusReturn) {
      this.focusReturn = {
        streams: new Map(this.streams),
        brightness: new Map(this.brightness),
      };
    }
    return this.renderList(surface);
  }

  private async renderList(surface: Extract<HudSurface, { kind: 'list' }>): Promise<boolean> {
    const liveStatus = this.captureLiveStatus();
    const ok = await this.client.require()
      .rebuildPageContainer(this.layouts.listLayout(surface.items, surface.options));
    if (!ok) return false;
    this.resetAll();
    this.surface = surface;
    // Mirror payload content in presenter state so confirmation restoration
    // and a same-layout self-heal retain the named focus page.
    this.streams.set(ContainerId.Main, surface.options.title);
    this.brightness.set(ContainerId.Main, 3);
    return this.restoreLiveStatus(liveStatus);
  }

  async showDefault(): Promise<boolean> {
    if (this.confirmation) {
      this.confirmation.surface = { kind: 'default' };
      if (this.focusReturn) {
        this.confirmation.streams = new Map(this.focusReturn.streams);
        this.confirmation.brightness = new Map(this.focusReturn.brightness);
        this.focusReturn = undefined;
      }
      return true;
    }
    const deferred = this.focusReturn;
    const liveStatus = this.captureLiveStatus();
    const ok = await this.client.require().rebuildPageContainer(this.layouts.defaultLayout());
    if (!ok) return false;
    this.resetAll();
    this.surface = { kind: 'default' };
    this.focusReturn = undefined;
    let restored = await this.restoreLiveStatus(liveStatus);
    if (!deferred) return restored;

    for (const [id, text] of deferred.streams) {
      if (id !== ContainerId.StatusBar) this.streams.set(id, text);
    }
    for (const [id, level] of deferred.brightness) {
      if (id !== ContainerId.StatusBar) this.brightness.set(id, level);
    }
    for (const id of [
      ContainerId.Main,
      ContainerId.Context,
      ContainerId.Tray,
      ContainerId.Hint,
    ] as const) {
      if (this.streams.has(id)) restored = (await this.paint(id)) && restored;
    }
    return restored;
  }

  /**
   * The one exclusive app-owned HUD overlay.
   *
   * It keeps plugin-owned status visible, suppresses competing regions, and
   * defers ordinary content until the gate settles. If opened from a list,
   * the default five-region page is installed temporarily; dismissal rebuilds
   * the exact focus page rather than dropping the wearer at Home.
   */
  showConfirmation(prompt: string, id = 'hud:confirmation'): Promise<boolean> {
    const openedHere = this.confirmation === undefined;
    const safePrompt = (prompt.trim() || 'Confirm requested action.')
      .slice(0, MAX_CONFIRM_PROMPT_CHARS);
    if (!this.confirmation) {
      const content = this.surface.kind === 'list' && this.focusReturn
        ? this.focusReturn
        : { streams: this.streams, brightness: this.brightness };
      this.confirmation = {
        id,
        prompt: safePrompt,
        surface: this.cloneSurface(this.surface),
        streams: new Map(content.streams),
        brightness: new Map(content.brightness),
      };
    } else {
      // Claim ownership synchronously, before an awaited bridge call. A stale
      // result callback can now only enqueue a no-op dismissal for its old id.
      this.confirmation.id = id;
      this.confirmation.prompt = safePrompt;
    }

    return this.enqueueConfirmation(() => this.renderConfirmation(id, openedHere));
  }

  private async renderConfirmation(id: string, openedHere = false): Promise<boolean> {
    const current = this.confirmation;
    if (!current || current.id !== id) return true;
    if (this.surface.kind === 'list') {
      const liveStatus = this.captureLiveStatus();
      const ok = await this.client.require()
        .rebuildPageContainer(this.layouts.defaultLayout());
      if (this.confirmation?.id !== id) return true;
      if (!ok) {
        if (openedHere) this.confirmation = undefined;
        return false;
      }
      this.resetAll();
      this.surface = { kind: 'default' };
      if (!await this.restoreLiveStatus(liveStatus)) return false;
    }

    // The bridge is a single BLE command lane. Sequential paints avoid
    // interleaved upgrades and finish with the action hint, so the decision
    // never advertises controls before its prompt exists.
    let ok = await this.replaceText(
      `CONFIRM ACTION\n${current.prompt}`, ContainerId.Main, 4,
    );
    if (this.confirmation?.id !== id) return true;
    ok = (await this.replaceText('', ContainerId.Context, 0)) && ok;
    if (this.confirmation?.id !== id) return true;
    ok = (await this.replaceText('', ContainerId.Tray, 0)) && ok;
    if (this.confirmation?.id !== id) return true;
    ok = (await this.replaceText(CONFIRM_HINT, ContainerId.Hint, 4)) && ok;
    return ok;
  }

  dismissConfirmation(id?: string): Promise<boolean> {
    return this.enqueueConfirmation(() => this.performDismissConfirmation(id));
  }

  private async performDismissConfirmation(id?: string): Promise<boolean> {
    const prior = this.confirmation;
    if (!prior) return true;
    if (id !== undefined && prior.id !== id) return true;

    // Clear this synchronously before any awaited paint. A replacement
    // confirmation may be queued by a callback in the same event turn.
    this.confirmation = undefined;

    if (prior.surface.kind === 'list') {
      this.focusReturn = {
        streams: new Map(prior.streams),
        brightness: new Map(prior.brightness),
      };
      const restored = await this.renderList(prior.surface);
      return this.repaintReplacementConfirmation(restored);
    }

    this.surface = { kind: 'default' };
    for (const id of this.pendingPaint.keys()) this.cancelPending(id);

    // Status can keep changing while the gate is open. Preserve that live
    // plugin-owned line rather than restoring a stale snapshot.
    const liveStatus = this.streams.get(ContainerId.StatusBar);
    const liveStatusBrightness = this.brightness.get(ContainerId.StatusBar);
    this.streams = new Map(prior.streams);
    this.brightness = new Map(prior.brightness);
    if (liveStatus !== undefined) this.streams.set(ContainerId.StatusBar, liveStatus);
    if (liveStatusBrightness !== undefined) {
      this.brightness.set(ContainerId.StatusBar, liveStatusBrightness);
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
    return this.repaintReplacementConfirmation(ok);
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
    if (this.healing) return false; // one attempt; never recurse
    this.healing = true;
    try {
      const layout = this.surface.kind === 'list'
        ? this.layouts.listLayout(this.surface.items, this.surface.options)
        : this.layouts.defaultLayout();
      const rebuilt = await this.client.require().rebuildPageContainer(layout);
      if (!rebuilt) return false;
      console.warn('[HudPresenter] page was missing — rebuilt and repainting');
      return await this.paint(containerId);
    } catch {
      return false;
    } finally {
      this.healing = false;
    }
  }

  private async paint(containerId: number): Promise<boolean> {
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

  private cancelPending(containerId: number): void {
    const timer = this.pendingPaint.get(containerId);
    if (timer !== undefined) {
      clearTimeout(timer);
      this.pendingPaint.delete(containerId);
    }
  }

  private deferText(text: string, containerId: ContainerId, textColor?: number): void {
    const deferred = this.confirmation;
    if (!deferred) return;
    deferred.streams.set(containerId, text);
    if (textColor !== undefined) deferred.brightness.set(containerId, textColor);
    else deferred.brightness.delete(containerId);
  }

  private deferFocusText(text: string, containerId: ContainerId, textColor?: number): void {
    const deferred = this.ensureFocusReturn();
    deferred.streams.set(containerId, text);
    if (textColor !== undefined) deferred.brightness.set(containerId, textColor);
    else deferred.brightness.delete(containerId);
  }

  private ensureFocusReturn(): HudContentState {
    this.focusReturn ??= { streams: new Map(), brightness: new Map() };
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
    return {
      title: (options.title?.trim() || DEFAULT_LIST_TITLE).slice(0, MAX_FOCUS_COPY_CHARS),
      hint: (options.hint?.trim() || DEFAULT_LIST_HINT).slice(0, MAX_FOCUS_COPY_CHARS),
    };
  }

  private cloneSurface(surface: HudSurface): HudSurface {
    if (surface.kind === 'default') return { kind: 'default' };
    return {
      kind: 'list',
      items: [...surface.items],
      options: { ...surface.options },
    };
  }

  private resetAll(): void {
    for (const id of this.pendingPaint.keys()) this.cancelPending(id);
    this.lastLength.clear();
    this.streams.clear();
    this.lastPaint.clear();
    this.brightness.clear();
  }
}
