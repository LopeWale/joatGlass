import {
  ImageRawDataUpdate,
  ImageRawDataUpdateResult,
} from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import type { G2DiagnosticTrace, TraceTileContainer } from './G2DiagnosticTrace';
import type { HudStatusModel } from './HudStatus';
import type { EvenReaderLine } from './EvenFont';
import type { HomeColumnView } from './HomeImages';
import { isNavigationSetupStatus, type NavigationHudModel } from './NavigationHud';
import type { PanelRailVisual } from './PanelRail';
import {
  CANVAS,
  ContainerId,
  VisualContainerName,
  type HudContentKind,
  type PlanningGridCell,
  type ProductivityHudModel,
} from './types';

const TILE_WIDTH = 288;
const TILE_HEIGHT = 144;

export interface HudImageTile {
  containerId: number;
  pixels: Uint8Array;
  /** A tile smaller than a quadrant (the header's 288x24 halves). */
  width?: number;
  height?: number;
}

/** Margin facts for the resting dashboard, resolved by the Mac. */
export interface HudAmbientModel {
  temperature: string;
  condition: string;
  unread: number;
}

export type AssistantOverlayPresentation = 'answer' | 'receipt';

export interface AssistantOverlayVisual {
  text: string;
  /** Answers own a readable card even when short; receipts may use the compact strip. */
  presentation: AssistantOverlayPresentation;
  /** Monotonic presenter identity so the same answer starts at page one when shown again. */
  revision?: number;
}

/** The pages that are pixels: the dashboard, the Planning month (a
 * calendar grid has no firmware template) and live guidance with its map.
 * Every other page is a firmware text page or list (HudPresenter). */
type HudVisualBaseFrame =
  | {
    kind: 'overview';
    status: string;
    micOn: boolean;
    ambient?: HudAmbientModel;
    mainBrightness: number;
    main: string;
    mainKind?: HudContentKind;
    context: string;
    tray: string;
    hint: string;
    /** The right pane's tab rail and the active tab's cached content.
     * Absent only in fixtures and tests that predate the rail. */
    rail?: PanelRailVisual;
  }
  | {
    kind: 'list';
    status: string;
    micOn: boolean;
    title: string;
    /** Display-only identity retained through reading and Back restoration. */
    contextLabel?: string;
    controlStartIndex?: number;
    planningEntries?: Array<{ time: string; kind: "event" | "reminder"; title: string } | null>;
    items: string[];
    selectedIndex: number;
    selectable: boolean;
    hint: string;
    planningGrid?: readonly PlanningGridCell[];
    planningPane?: { dateIndex: number; focused: boolean; itemIndex: number };
    /** Present when this list is an expanded panel tab: the rail stays on
     * the left so the wearer can see where they are. */
    rail?: PanelRailVisual;
  }
  | {
    kind: 'navigation';
    status: string;
    micOn: boolean;
    navigation: NavigationHudModel;
  }
  | {
    /** A reader set in Even's font (EvenFont.ts) across the whole canvas.
     * Opt-in only (VITE_EVEN_FONT_PAGES=1): readers are firmware text. */
    kind: 'reader';
    status: string;
    micOn: boolean;
    title: string;
    lines: EvenReaderLine[];
    /** The page's hint, its first clause the page count ("2/3"). */
    hint: string;
    /** JOAT's box covers the page from here down: nothing is drawn below
     * it (ConversationOverlay keeps these tiles under the box). */
    clipBottom?: number;
  };

/** Overlays composite into the same four G2 images, not a fifth container.
 * `actionOverlay`: transient outcome strip over the page the action came from;
 * truth states (failed/check/due) still arrive as a full productivity frame. */
export type HudVisualFrame = HudVisualBaseFrame & {
  /** The phone's status facts as data (HudStatus.ts). Present on every
   * production frame once Chrome has started; the renderer falls back to
   * parsing `status` only for frames built without it. */
  statusModel?: HudStatusModel;
  assistantOverlay?: AssistantOverlayVisual;
  actionOverlay?: { productivity: ProductivityHudModel };
};

export interface HudFrameRenderer {
  render(frame: HudVisualFrame): Promise<readonly HudImageTile[]>;
  /** The header band alone, as its two image halves. */
  renderHeader?(model: HudStatusModel, label: string): Promise<readonly HudImageTile[]>;
  /** Whether this renderer sets readers in Even's font (EvenFont.ts). */
  readonly drawsReaders?: boolean;
  /** Home's left column as its two images (HomeImages.ts). */
  renderHomeColumn?(view: HomeColumnView, model?: HudStatusModel): Promise<readonly HudImageTile[]>;
}

/** A header repaint queued on the same single image lane as frames. */
interface HeaderPaint {
  kind: 'header';
  model: HudStatusModel;
  label: string;
}
/** A part of a firmware page drawn as images (Home's column): its own
 * tiles, on the same lane, sent only where their pixels changed. */
interface StripPaint {
  kind: 'strip';
  render: () => Promise<readonly HudImageTile[]>;
}
type PaintWork = HudVisualFrame | HeaderPaint | StripPaint;

/** Header and strip work: page parts, never superseded by a full frame. */
function isPagePart(work: PaintWork): work is HeaderPaint | StripPaint {
  return work.kind === 'header' || work.kind === 'strip';
}

export interface HudFramePainter {
  paint(frame: HudVisualFrame): Promise<boolean>;
  /** Receipt for this frame only. Cancellation and a page rebuild invalidate
   * the result; accepted identical pixels on the same page may be reused. */
  paintExact?(frame: HudVisualFrame, isCurrent: () => boolean): Promise<boolean>;
  /** The page on the glass was rebuilt: nothing painted before is on it. */
  forgetPage?(): void;
  /** Paint the header halves of a firmware page; only a half whose pixels
   * changed is sent. */
  paintHeader?(model: HudStatusModel, label: string, isCurrent: () => boolean): Promise<boolean>;
  /** Whether this painter can draw the header (its renderer has one). */
  canPaintHeader?(): boolean;
  /** Paint Home's left column images; only a tile whose pixels changed is sent. */
  paintHomeColumn?(view: HomeColumnView, isCurrent: () => boolean): Promise<boolean>;
  canPaintHomeColumn?(): boolean;
  /** Whether this painter draws a reader page in Even's font. A
   * painter without it keeps readers as firmware text pages. */
  canPaintReader?(): boolean;
  /** Wait for queued paints without cancelling their delivery receipts. */
  whenIdle?(timeoutMs?: number): Promise<boolean>;
  /** Hold image submissions while a bounded native-idle check and page
   * replacement run. A wait timeout must never invoke replacePage. */
  withPageTransition?(replacePage: () => Promise<boolean>, isCurrent: () => boolean): Promise<boolean>;
  suspendDisplay?(): void;
  resumeDisplay?(): void;
  /** Set by the presenter: page parts were just switched off, so the page
   * on the glass should be redrawn without its header and column images. */
  onPagePartsDisabled?: () => void;
}

/** Converts one raw raster tile into an image format the native bridge and
 * the official simulator both decode. */
export type HudImageEncoder = (tile: HudImageTile) => Promise<Uint8Array>;

/**
 * Only an idle background overview burst waits for this window. Explicit
 * interaction frames wake the window immediately, and an already-busy lane
 * naturally batches background updates without adding another delay.
 */
export const PAINT_COALESCE_MS = 40;

/**
 * A tile write that outlives this has an uncertain native outcome. Every
 * later frame queues behind the one paint lane, so a
 * promise that never settles is a HUD that never changes again —
 * notifications stop arriving, the menu stops opening — with nothing
 * logged. Fail the frame instead, forget the delivered tiles, and let the
 * lane drain.
 */
export const TILE_UPLOAD_TIMEOUT_MS = 15_000;

/**
 * A page part (the header halves, Home's column tiles) is small: a send
 * takes well under 2 s. One that has not answered in this long is not
 * going to, and every page change waits for the lane to drain.
 *
 * On the glasses (2026-10-08/09) one such write never settled. Each page
 * change then waited the full 15 s, MacLink's 15 s downlink watchdog
 * dropped the Mac link on the same beat, and the plugin reconnected into
 * the same wait: about 2,350 drops in two days. Page parts now give up
 * sooner and switch themselves off (`pagePartsDisabled`).
 */
export const PAGE_PART_UPLOAD_TIMEOUT_MS = 6_000;

/**
 * How long a page change waits for image writes to drain before it is
 * refused. Kept well inside MacLink's 15 s downlink watchdog: a refused
 * page leaves the link up, a page change that outwaits the watchdog drops
 * it.
 */
export const PAGE_TRANSITION_IDLE_MS = 8_000;

/** Consecutive failed page-part sends (the host answered, but not with
 * success) that switch page parts off for this session. */
export const PAGE_PART_FAILURE_LIMIT = 3;

/** A bounded wait that ran out; the work it waited on may still finish. */
class SettleTimeout extends Error {}

/**
 * Minimum spacing between two updateImageRawData calls. Even's display docs:
 * The SDK documents a 100ms image-path pacing window. Retain a 10ms margin
 * while awaiting every write. This does not make multiple tiles atomic and
 * an SDK result does not establish when pixels became visible on hardware.
 */
export const TILE_CALL_GAP_MS = 110;

export interface HudVisualSurfaceOptions {
  /** Burst window in ms; 0 (the default) renders the moment a frame lands. */
  coalesceMs?: number;
  /** Per text/tile bridge write deadline in ms; 0 waits forever. */
  uploadTimeoutMs?: number;
  /** Minimum ms from one tile call to the next; 0 (the default) sends back to back. */
  tileGapMs?: number;
}

function now(): number {
  return globalThis.performance?.now?.() ?? Date.now();
}

function elapsedSince(startedAt: number): number {
  return Math.max(0, Math.round(now() - startedAt));
}

/** Resolve with `work`, or reject once `ms` have passed without it settling. */
function settleWithin<T>(work: Promise<T>, ms: number, what: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => reject(new SettleTimeout(`${what} did not settle within ${ms}ms`)), ms);
  });
  return Promise.race([work, deadline]).finally(() => clearTimeout(timer));
}

interface PendingPaint {
  frame: PaintWork;
  isCurrent?: () => boolean;
  displayCurrent: () => boolean;
  resolve: Array<(painted: boolean) => void>;
  queuedAt: number;
  immediate: boolean;
}

function pixelsEqual(left: Uint8Array | undefined, right: Uint8Array): boolean {
  if (left === undefined || left.length !== right.length) return false;
  for (let index = 0; index < right.length; index += 1) {
    if (left[index] !== right[index]) return false;
  }
  return true;
}

/**
 * Split one row-major raw Gray8 frame into the SDK's four maximum-size
 * images. A byte-per-pixel buffer must remain Gray8; it is neither packed
 * Gray4 nor the old invalid hybrid of one 4-bit value in every byte.
 */
export function splitGrayscaleTiles(pixels: Uint8Array): HudImageTile[] {
  if (pixels.length !== CANVAS.width * CANVAS.height) {
    throw new Error(`expected ${CANVAS.width * CANVAS.height} grayscale pixels, got ${pixels.length}`);
  }
  const definitions = [
    { containerId: ContainerId.Image, x: 0, y: 0 },
    { containerId: ContainerId.VisualTopRight, x: TILE_WIDTH, y: 0 },
    { containerId: ContainerId.VisualBottomLeft, x: 0, y: TILE_HEIGHT },
    { containerId: ContainerId.VisualBottomRight, x: TILE_WIDTH, y: TILE_HEIGHT },
  ] as const;

  return definitions.map(({ containerId, x, y }) => {
    const unpacked = new Uint8Array(TILE_WIDTH * TILE_HEIGHT);
    for (let row = 0; row < TILE_HEIGHT; row += 1) {
      const source = (y + row) * CANVAS.width + x;
      unpacked.set(pixels.subarray(source, source + TILE_WIDTH), row * TILE_WIDTH);
    }
    return { containerId, pixels: unpacked };
  });
}

/** The SDK accepts encoded image bytes and performs the device's Gray4
 * conversion itself. Encode the canvas tile rather than sending an invalid
 * byte-per-pixel Gray4 hybrid. */
export async function encodeGray8TilePng(tile: HudImageTile): Promise<Uint8Array> {
  const width = tile.width ?? TILE_WIDTH;
  const height = tile.height ?? TILE_HEIGHT;
  const expectedLength = width * height;
  if (tile.pixels.length !== expectedLength) {
    throw new Error(`expected ${expectedLength} grayscale tile pixels, got ${tile.pixels.length}`);
  }
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext('2d', { alpha: false });
  if (!context) throw new Error('2D canvas is unavailable for HUD tile encoding');
  const image = context.createImageData(width, height);
  for (let source = 0, target = 0; source < tile.pixels.length; source += 1, target += 4) {
    const gray = tile.pixels[source] ?? 0;
    image.data[target] = gray;
    image.data[target + 1] = gray;
    image.data[target + 2] = gray;
    image.data[target + 3] = 255;
  }
  context.putImageData(image, 0, 0);
  const blob = await new Promise<Blob>((resolve, reject) => {
    canvas.toBlob((value) => {
      if (value) resolve(value);
      else reject(new Error('HUD tile PNG encoding failed'));
    }, 'image/png');
  });
  return new Uint8Array(await blob.arrayBuffer());
}

export class HudVisualSurface implements HudFramePainter {
  private painting = false;
  private readonly queue: PendingPaint[] = [];
  private readonly deliveredTiles = new Map<number, Uint8Array>();
  private frameNumber = 0;
  private readonly coalesceMs: number;
  private readonly uploadTimeoutMs: number;
  private readonly tileGapMs: number;
  private lastTileCallAt = Number.NEGATIVE_INFINITY;
  private nativeUploadSettled: Promise<void> = Promise.resolve();
  private pageRevision = 0;
  private displayActive = true;
  private displayRevision = 0;
  private readonly idleWaiters: Array<() => void> = [];
  private wakeCoalescing?: () => void;
  private replacingPage = false;
  /** A full-tile write the host has not answered past its timeout. Until
   * it settles nothing else is written (one writer: a late old tile must
   * not land behind newer pixels), but nothing waits on it either: a frame
   * or page change is refused at once instead of after another full
   * timeout, so the Mac message behind it never trips MacLink's watchdog. */
  private hungWrite = false;
  /** Header and column images are off for this session: one of them hung
   * or kept failing on the glasses, and every page change waited on it.
   * The presenter draws both as firmware text instead. */
  private pagePartsDisabled = false;
  private pagePartFailures = 0;
  onPagePartsDisabled?: () => void;
  /** Two recent encodings per container support Back/reopen without growing
   * with the note archive. This is preparation only, never delivery evidence. */
  private readonly encodedTiles = new Map<number, Array<{ pixels: Uint8Array; bytes: Uint8Array }>>();

  constructor(
    private readonly client: BridgeClient,
    private readonly renderer: HudFrameRenderer,
    private readonly diagnostics?: G2DiagnosticTrace,
    private readonly imageEncoder: HudImageEncoder = async (tile) => tile.pixels,
    options: HudVisualSurfaceOptions = {},
  ) {
    this.coalesceMs = Math.max(0, options.coalesceMs ?? 0);
    this.uploadTimeoutMs = Math.max(0, options.uploadTimeoutMs ?? TILE_UPLOAD_TIMEOUT_MS);
    this.tileGapMs = Math.max(0, options.tileGapMs ?? 0);
  }

  /**
   * The page on the glass was rebuilt — replaced by a firmware list page and
   * later rebuilt back — so nothing delivered before it is on it any more:
   * the next frame writes the spine word and every tile again.
   */
  forgetPage(): void {
    if (import.meta.env.DEV) console.info('[blank-trace]', Date.now(), 'forget-page', this.pageRevision);
    this.pageRevision += 1;
    this.deliveredTiles.clear();
  }

  suspendDisplay(): void {
    if (!this.displayActive) return;
    this.displayActive = false;
    this.displayRevision += 1;
    this.forgetPage();
    this.encodedTiles.clear();
    for (const pending of this.queue.splice(0)) pending.resolve.forEach((resolve) => resolve(false));
    this.wakeCoalescing?.();
  }

  resumeDisplay(): void {
    this.displayActive = true;
  }

  private waitForPaintDrain(timeoutMs: number): Promise<boolean> {
    if (!this.painting) return Promise.resolve(true);
    return new Promise<boolean>((resolve) => {
      const done = () => {
        clearTimeout(timer);
        resolve(true);
      };
      const timer = setTimeout(() => {
        const index = this.idleWaiters.indexOf(done);
        if (index >= 0) this.idleWaiters.splice(index, 1);
        resolve(false);
      }, Math.max(0, timeoutMs));
      this.idleWaiters.push(done);
    });
  }

  async whenIdle(timeoutMs = 8000): Promise<boolean> {
    const deadline = now() + Math.max(0, timeoutMs);
    try {
      do {
        if (!await this.waitForPaintDrain(Math.max(0, deadline - now()))) return false;
        // Reporting a timeout does not settle the underlying SDK operation.
        // Include that operation even after the JavaScript drain has stopped.
        // A write already known to be hung is not waited on again.
        await settleWithin(
          this.nativeUploadSettled,
          this.hungWrite ? 0 : Math.max(0, deadline - now()),
          'native display idle',
        );
        if (!this.painting) return true;
      } while (now() < deadline);
    } catch { /* An unresolved native write keeps the page unsafe to replace. */ }
    return false;
  }

  async withPageTransition(replacePage: () => Promise<boolean>, isCurrent: () => boolean): Promise<boolean> {
    if (this.replacingPage || !this.displayActive || !isCurrent()) return false;
    this.replacingPage = true;
    this.suspendDisplay();
    const revision = this.displayRevision;
    try {
      // Bounded below MacLink's 15 s downlink watchdog (PAGE_TRANSITION_IDLE_MS).
      const idleMs = this.uploadTimeoutMs > 0
        ? Math.min(this.uploadTimeoutMs, PAGE_TRANSITION_IDLE_MS) : PAGE_TRANSITION_IDLE_MS;
      if (!await this.whenIdle(idleMs) || !isCurrent()) return false;
      // Display remains suspended across the native call, eliminating the
      // check-then-rebuild gap in which a new image upload could start.
      return await replacePage();
    } finally {
      this.replacingPage = false;
      if (this.displayRevision === revision && isCurrent()) this.resumeDisplay();
    }
  }

  paint(frame: HudVisualFrame): Promise<boolean> {
    return this.enqueue(frame);
  }

  paintExact(frame: HudVisualFrame, isCurrent: () => boolean): Promise<boolean> {
    const pageRevision = this.pageRevision;
    return this.enqueue(frame, () => pageRevision === this.pageRevision && isCurrent());
  }

  canPaintHeader(): boolean {
    return !this.pagePartsDisabled && this.renderer.renderHeader !== undefined;
  }

  canPaintReader(): boolean {
    return this.renderer.drawsReaders === true;
  }

  canPaintHomeColumn(): boolean {
    return !this.pagePartsDisabled && this.renderer.renderHomeColumn !== undefined;
  }

  paintHomeColumn(view: HomeColumnView, isCurrent: () => boolean): Promise<boolean> {
    const render = this.renderer.renderHomeColumn;
    if (!render || this.pagePartsDisabled) return Promise.resolve(false);
    const pageRevision = this.pageRevision;
    return this.enqueue(
      { kind: 'strip', render: () => render.call(this.renderer, view) },
      () => pageRevision === this.pageRevision && isCurrent(),
    );
  }

  paintHeader(model: HudStatusModel, label: string, isCurrent: () => boolean): Promise<boolean> {
    if (!this.renderer.renderHeader || this.pagePartsDisabled) return Promise.resolve(false);
    const pageRevision = this.pageRevision;
    return this.enqueue(
      { kind: 'header', model, label },
      () => pageRevision === this.pageRevision && isCurrent(),
    );
  }

  private enqueue(frame: PaintWork, isCurrent?: () => boolean): Promise<boolean> {
    const displayRevision = this.displayRevision;
    const bridgeCurrent = this.client.captureDisplayGuard?.() ?? (() => true);
    const displayCurrent = () => this.displayActive && !this.replacingPage
      && this.displayRevision === displayRevision && bridgeCurrent();
    if (!displayCurrent()) return Promise.resolve(false);
    const immediate = isPagePart(frame) || !!isCurrent || frame.kind !== 'overview'
      || !!frame.assistantOverlay || !!frame.actionOverlay;
    return new Promise((resolve) => {
      const pending = this.queue[this.queue.length - 1];
      if (pending && pending.displayCurrent() && !isCurrent && !pending.isCurrent
        && frame.kind !== 'navigation' && pending.frame.kind !== 'navigation'
        && !isPagePart(frame) && !isPagePart(pending.frame)) {
        // A full frame supersedes every older not-yet-started frame. Keeping
        // only the latest prevents streamed text/countdown ticks from building
        // a stale multi-second BLE backlog while preserving one command lane.
        // Navigation is excluded: its caller emits a semantic receipt only
        // after this exact four-tile frame finishes, so acknowledging a newer
        // generation on behalf of an older one would corrupt HUD/input truth.
        pending.frame = frame as HudVisualFrame;
        pending.immediate ||= immediate;
        pending.resolve.push(resolve);
      } else {
        this.queue.push({ frame, isCurrent, displayCurrent, resolve: [resolve], queuedAt: now(), immediate });
      }
      if (immediate) this.wakeCoalescing?.();
      if (!this.painting) {
        this.painting = true;
        void this.drain();
      }
    });
  }

  private async drain(): Promise<void> {
    // Coalesce only a new background burst. A gesture arriving during this
    // window wakes it; queued work behind a write has already had its window.
    if (this.coalesceMs > 0 && this.queue[0] && !this.queue[0].immediate) {
      await new Promise<void>((resolve) => {
        const finish = () => {
          clearTimeout(timer);
          this.wakeCoalescing = undefined;
          resolve();
        };
        const timer = setTimeout(finish, this.coalesceMs);
        this.wakeCoalescing = finish;
      });
    }
    for (;;) {
      const next = this.queue.shift();
      if (!next) break;
      const painted = await this.paintNow(
        next.frame, () => next.displayCurrent() && (next.isCurrent?.() ?? true), next.queuedAt,
      );
      next.resolve.forEach((resolve) => resolve(painted));
    }
    this.painting = false;
    for (const resolve of this.idleWaiters.splice(0)) resolve();
  }

  private async encodeTile(tile: HudImageTile, isCurrent: () => boolean): Promise<Uint8Array> {
    const cached = this.encodedTiles.get(tile.containerId)?.find((entry) => pixelsEqual(entry.pixels, tile.pixels));
    if (cached) return cached.bytes;
    const bytes = await this.imageEncoder(tile);
    if (isCurrent()) {
      const entries = this.encodedTiles.get(tile.containerId) ?? [];
      entries.unshift({ pixels: tile.pixels.slice(), bytes: bytes.slice() });
      this.encodedTiles.set(tile.containerId, entries.slice(0, 2));
    }
    return bytes;
  }

  /**
   * The host has not answered an image write within its timeout. A host
   * timeout does not cancel the SDK call, so the write may still land.
   *
   * A page part (header half, Home column tile) switches page parts off
   * for the session and releases the one-writer boundary for that write:
   * the presenter redraws the page without those containers, so a late
   * write has nothing of ours left to overwrite. Waiting on it instead is
   * what held every later page change and dropped the Mac link.
   *
   * A full tile keeps the boundary (nothing else is written until it
   * settles), but is marked hung so nothing waits on it again.
   */
  private writeTimedOut(settled: Promise<void>, pagePart: boolean): void {
    this.deliveredTiles.clear();
    if (pagePart) {
      this.disablePageParts();
      if (this.nativeUploadSettled === settled) this.nativeUploadSettled = Promise.resolve();
      return;
    }
    this.hungWrite = true;
    void settled.then(() => {
      if (this.nativeUploadSettled === settled) this.hungWrite = false;
    });
  }

  private disablePageParts(): void {
    if (this.pagePartsDisabled) return;
    this.pagePartsDisabled = true;
    console.warn('[HudVisualSurface] header and column images off for this session: '
      + 'one hung or kept failing on the glasses; drawing them as firmware text');
    // After the failing paint has returned, never inside it.
    setTimeout(() => { this.onPagePartsDisabled?.(); }, 0);
  }

  private async paintNow(frame: PaintWork, guard?: () => boolean, queuedAt = now()): Promise<boolean> {
    const frameNumber = ++this.frameNumber;
    const pageRevision = this.pageRevision;
    const isCurrent = (): boolean => pageRevision === this.pageRevision && (guard?.() ?? true);
    const startedAt = now();
    const timing = { frame: frameNumber, kind: frame.kind, queueMs: elapsedSince(queuedAt), renderMs: 0, encodeMs: 0, pacingMs: 0, bridgeMs: 0, changedTiles: 0, totalMs: 0, accepted: false };
    let current: { order: number; tile: HudImageTile; startedAt: number; bytes: number } | undefined;
    try {
      if (!isCurrent()) return false;
      // A write known to be hung is checked, not waited on: refused at once.
      await settleWithin(
        this.nativeUploadSettled,
        this.hungWrite ? 0 : this.uploadTimeoutMs || 8000,
        'previous display write',
      );
      if (!isCurrent()) return false;
      if (import.meta.env.DEV) console.info('[blank-trace]', Date.now(), 'render-start', frame.kind, pageRevision);
      const renderStartedAt = now();
      const rendered = frame.kind === 'header'
        ? await this.renderer.renderHeader!(frame.model, frame.label)
        : frame.kind === 'strip'
          ? await frame.render()
          : await this.renderer.render(frame);
      timing.renderMs = elapsedSince(renderStartedAt);
      // Retire prior choices/instructions outside the status region before
      // replacing its complete explanation. No native write is parallelized.
      const tiles = frame.kind === 'navigation' && isNavigationSetupStatus(frame.navigation)
        ? [...rendered].sort((a, b) => Number(a.containerId === ContainerId.Image)
          - Number(b.containerId === ContainerId.Image))
        : rendered;
      if (import.meta.env.DEV) console.info('[blank-trace]', Date.now(), 'render-result', frame.kind, tiles.length,
        tiles.map(t => t.pixels.reduce((n, v) => n + (v > 0 ? 1 : 0), 0)));
      if (!isCurrent()) return false;
      // Navigation acknowledges the complete desired pixel state: unchanged
      // regions already confirmed on this page remain valid. Page rebuild,
      // cancellation and failed uploads invalidate that cache below.
      const changed = tiles.map((tile, order) => ({ tile, order }))
        .filter(({ tile }) => !pixelsEqual(this.deliveredTiles.get(tile.containerId), tile.pixels));
      timing.changedTiles = changed.length;
      // Finish all preparation before exposing the first changed tile. Only
      // encoding is parallel: SDK writes below keep their single-owner lane.
      const encodeStartedAt = now();
      const encodingResults = await Promise.allSettled(changed.map(async ({ tile, order }) => ({
        tile, order, imageData: await this.encodeTile(tile, isCurrent),
      })));
      // A failed encoder cannot leave background preparation running into
      // the next frame and repopulating its cache after cancellation.
      const prepared = encodingResults.map((result) => {
        if (result.status === 'rejected') throw result.reason;
        return result.value;
      });
      timing.encodeMs = elapsedSince(encodeStartedAt);
      if (!isCurrent()) return false;
      for (const { tile, order, imageData } of prepared) {
        if (!isCurrent()) return false;
        current = { order, tile, startedAt: now(), bytes: 0 };
        const containerName = VisualContainerName[tile.containerId];
        if (!containerName) throw new Error(`unknown visual container ${tile.containerId}`);
        current.bytes = imageData.length;
        // A host timeout does not cancel the SDK call. Keep the native
        // single-writer boundary even after reporting failure, otherwise a
        // late old upload can overwrite newer pixels behind our cache.
        if (this.uploadTimeoutMs > 0) {
          await settleWithin(
            this.nativeUploadSettled,
            this.hungWrite ? 0 : this.uploadTimeoutMs,
            'previous native upload',
          );
        } else {
          await this.nativeUploadSettled;
        }
        if (!isCurrent()) return false;
        if (this.tileGapMs > 0) {
          const wait = this.tileGapMs - (now() - this.lastTileCallAt);
          if (wait > 0) {
            const pacingStartedAt = now();
            await new Promise<void>((resolve) => { setTimeout(resolve, wait); });
            timing.pacingMs += elapsedSince(pacingStartedAt);
          }
        }
        if (!isCurrent()) return false;
        // SDK call duration only; not a pixel-visible or BLE-delivery receipt.
        current.startedAt = now();
        this.lastTileCallAt = current.startedAt;
        if (import.meta.env.DEV) console.info('[blank-trace]', Date.now(), 'upload-start', tile.containerId);
        const upload = this.client.require().updateImageRawData(
          new ImageRawDataUpdate({
            containerID: tile.containerId,
            containerName,
            // The phone bridge is a Dart List<int> boundary.  Keep the
            // payload as a normal JS array instead of relying on a typed-array
            // structured-clone conversion that the host does not guarantee.
            imageData: Array.from(imageData),
          }),
        );
        const settled = upload.then(() => undefined, () => undefined);
        this.nativeUploadSettled = settled;
        const timeoutMs = isPagePart(frame) && this.uploadTimeoutMs > 0
          ? Math.min(this.uploadTimeoutMs, PAGE_PART_UPLOAD_TIMEOUT_MS)
          : this.uploadTimeoutMs;
        let result: ImageRawDataUpdateResult;
        try {
          result = timeoutMs > 0
            ? await settleWithin(upload, timeoutMs, `${containerName} upload`)
            : await upload;
        } catch (error) {
          if (error instanceof SettleTimeout) this.writeTimedOut(settled, isPagePart(frame));
          throw error;
        }
        timing.bridgeMs += elapsedSince(current.startedAt);
        if (import.meta.env.DEV) console.info('[blank-trace]', Date.now(), 'upload-result', tile.containerId, result);
        if (!isCurrent()) {
          // A late native write may have changed this tile even though its
          // owner was cancelled. Never reuse the previous pixel cache for it.
          this.deliveredTiles.delete(tile.containerId);
          return false;
        }
        const outcome = result === ImageRawDataUpdateResult.success ? 'success' : 'failed';
        // Page parts report only failures: a header half on every minute
        // would flood the bounded trace, a failed one is what support needs.
        if (!isPagePart(frame) || outcome !== 'success') this.diagnostics?.record({
          type: 'tile_upload',
          frame: frameNumber,
          order,
          containerId: tile.containerId as TraceTileContainer,
          outcome,
          durationMs: elapsedSince(current.startedAt),
          bytes: current.bytes,
        });
        if (isPagePart(frame)) {
          this.pagePartFailures = outcome === 'success' ? 0 : this.pagePartFailures + 1;
          if (this.pagePartFailures >= PAGE_PART_FAILURE_LIMIT) this.disablePageParts();
        }
        if (outcome === 'failed') {
          this.deliveredTiles.clear();
          return false;
        }
        this.deliveredTiles.set(tile.containerId, tile.pixels.slice());
      }
      if (!isCurrent()) return false;
      timing.accepted = true;
      return true;
    } catch (error) {
      this.deliveredTiles.clear();
      if (current) {
        this.diagnostics?.record({
          type: 'tile_upload',
          frame: frameNumber,
          order: current.order,
          containerId: current.tile.containerId as TraceTileContainer,
          outcome: 'error',
          durationMs: elapsedSince(current.startedAt),
          bytes: current.bytes,
        });
      }
      console.error(
        '[HudVisualSurface] frame paint failed:',
        error instanceof Error ? error.message : 'unknown error',
      );
      return false;
    } finally {
      timing.totalMs = elapsedSince(queuedAt);
      // Development performance evidence contains no text, identifiers or
      // raster payloads and does not change the Mac diagnostic wire schema.
      if (import.meta.env.DEV) console.info('[hud-timing]', JSON.stringify({ ...timing, workMs: elapsedSince(startedAt) }));
      // The same split, on the wire, so it survives QR sideload and a packed
      // .ehpk — the only places the BLE hop is real. An unchanged frame sent
      // nothing: recording it floods a trace bounded at MAX_RETAINED_EVENTS
      // and buries the frames that did work.
      if (timing.changedTiles > 0 && !isPagePart(frame)) {
        this.diagnostics?.record({
          type: 'frame_timing',
          frame: timing.frame,
          kind: frame.kind,
          changedTiles: timing.changedTiles,
          queueMs: timing.queueMs,
          renderMs: timing.renderMs,
          encodeMs: timing.encodeMs,
          pacingMs: timing.pacingMs,
          bridgeMs: timing.bridgeMs,
          totalMs: timing.totalMs,
        });
      }
    }
  }
}
