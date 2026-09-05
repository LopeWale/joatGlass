import {
  ImageRawDataUpdate,
  ImageRawDataUpdateResult,
  TextContainerUpgrade,
} from '@evenrealities/even_hub_sdk';
import { pxTruncate } from '@evenrealities/pretext';
import type { BridgeClient } from './BridgeClient';
import type { G2DiagnosticTrace, G2TraceEvent } from './G2DiagnosticTrace';
import type { NavigationHudModel } from './NavigationHud';
import type { PanelRailVisual } from './PanelRail';
import {
  CANVAS,
  ContainerId,
  SPINE_LABEL_BOX,
  SPINE_LABEL_NAME,
  VisualContainerName,
  type BrainstormHudModel,
  type HudContentKind,
  type PlanningGridCell,
  type ProductivityHudDomain,
  type ProductivityHudModel,
} from './types';

const TILE_WIDTH = 288;
const TILE_HEIGHT = 144;

export interface HudImageTile {
  containerId: number;
  pixels: Uint8Array;
}

/** Margin facts for the resting dashboard, resolved by the Mac. */
export interface HudAmbientModel {
  temperature: string;
  condition: string;
  unread: number;
}

export interface AssistantOverlayVisual {
  text: string;
}

type HudVisualBaseFrame =
  | {
    kind: 'prompt_question';
    status: string;
    micOn: boolean;
    promptId: string;
    question: string;
  }
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
    kind: 'confirmation';
    status: string;
    micOn: boolean;
    prompt: string;
    reviewDomain?: ProductivityHudDomain;
    /** Explicit Mac-provided diagnostic category; never inferred from the
     * private confirmation prompt. */
    diagnosticKind?: 'messages';
  }
  | {
    kind: 'navigation';
    status: string;
    micOn: boolean;
    navigation: NavigationHudModel;
  }
  | {
    kind: 'brainstorm';
    status: string;
    micOn: boolean;
    brainstorm: BrainstormHudModel;
  }
  | {
    kind: 'productivity';
    status: string;
    micOn: boolean;
    productivity: ProductivityHudModel;
  };

/** Overlays composite into the same four G2 images, not a fifth container.
 * `actionOverlay`: transient outcome strip over the page the action came from;
 * truth states (failed/check/due) still arrive as a full productivity frame. */
export type HudVisualFrame = HudVisualBaseFrame & {
  assistantOverlay?: AssistantOverlayVisual;
  actionOverlay?: { productivity: ProductivityHudModel };
};

export interface HudFrameRenderer {
  scrollTranscript?(direction: 'up' | 'down'): boolean;
  transcriptIsThinking?(): boolean;
  render(frame: HudVisualFrame): Promise<readonly HudImageTile[]>;
}

export interface HudFramePainter {
  /** `localTick`: this repaint carries no new information from the Mac —
   * only a device-side clock digit moved. It draws normally but is NOT
   * recorded as a wearer-visible frame receipt. */
  paint(frame: HudVisualFrame, localTick?: boolean): Promise<boolean>;
  /** Receipt for this frame only. Cancellation and a page rebuild invalidate
   * the result; accepted identical pixels on the same page may be reused. */
  paintExact?(frame: HudVisualFrame, isCurrent: () => boolean): Promise<boolean>;
  /** Move the brainstorm/terminal transcript window; true if it moved. */
  scrollTranscript?(direction: 'up' | 'down'): boolean;
  /** The transcript tail is a silent "Thinking…" marker awaiting tokens. */
  transcriptIsThinking?(): boolean;
  /** The page on the glass was rebuilt: nothing painted before is on it. */
  forgetPage?(): void;
  suspendDisplay?(): void;
  resumeDisplay?(): void;
}

/** Converts one raw raster tile into an image format the native bridge and
 * the official simulator both decode. */
export type HudImageEncoder = (tile: HudImageTile) => Promise<Uint8Array>;

function messagesDiagnosticSurface(frame: HudVisualFrame): Extract<G2TraceEvent, {
  type: 'visual_frame'; kind: 'messages';
}>['surface'] | undefined {
  if (frame.kind === 'confirmation') {
    return frame.diagnosticKind === 'messages' ? 'confirmation' : undefined;
  }
  if (frame.kind !== 'list') return undefined;
  switch (frame.title.trim().toUpperCase()) {
    case 'MESSAGES': return 'picker';
    case 'MESSAGES · CONVERSATION': return 'conversation';
    case 'MESSAGES · COMPOSE': return 'compose';
    case 'MESSAGES · ACCESS': return 'access';
    case 'MESSAGES · SETUP': return 'setup';
    case 'MESSAGES · RETRY': return 'retry';
    case 'MESSAGES · EMPTY': return 'empty';
    case 'MESSAGES · READ ONLY': return 'read_only';
    default: return undefined;
  }
}

async function completedVisualFrameReceipt(
  frame: HudVisualFrame,
  tiles: readonly HudImageTile[],
): Promise<Extract<G2TraceEvent, { type: 'visual_frame' }> | undefined> {
  const messagesSurface = messagesDiagnosticSurface(frame);
  if ((frame.kind !== 'brainstorm' && messagesSurface === undefined)
    || tiles.length !== 4 || !globalThis.crypto?.subtle) {
    return undefined;
  }
  const rasterLength = tiles.reduce((total, tile) => total + tile.pixels.length, 0);
  const raster = new Uint8Array(rasterLength);
  let offset = 0;
  for (const tile of tiles) {
    raster.set(tile.pixels, offset);
    offset += tile.pixels.length;
  }
  const digest = new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256', raster));
  const sha256 = Array.from(digest, (byte) => byte.toString(16).padStart(2, '0')).join('');
  if (frame.kind === 'brainstorm') {
    return {
      type: 'visual_frame',
      kind: 'brainstorm',
      surface: frame.brainstorm.screen,
      state: frame.brainstorm.state,
      sha256,
    };
  }
  return {
    type: 'visual_frame',
    kind: 'messages',
    surface: messagesSurface!,
    sha256,
  };
}

/**
 * Frames that land within this window of the first queued one render as ONE
 * frame. A voice turn's chrome reaches the plugin as a burst of separate Mac
 * messages — the microphone state, then the state word — and rendering the
 * first the instant it arrives ships the spine's tile twice, at 0.5-2 s a
 * tile over BLE (Even's own figure). Against that hop the wait is invisible.
 * Opt-in through the constructor: the presenter's tests pin the synchronous
 * start, and production (main.ts) passes this value.
 */
export const PAINT_COALESCE_MS = 40;

/**
 * A tile write that outlives this has not merely been slow: the host has
 * dropped it. Every later frame queues behind the one paint lane, so a
 * promise that never settles is a HUD that never changes again —
 * notifications stop arriving, the menu stops opening — with nothing
 * logged. Fail the frame instead, forget the delivered tiles, and let the
 * lane drain.
 */
export const TILE_UPLOAD_TIMEOUT_MS = 15_000;

/**
 * Minimum spacing between two updateImageRawData calls. Even's display docs:
 * "Each updateImageRawData holds the image path for 100ms. A call that
 * arrives inside that window is held and flushed on the next one." A frame's
 * LAST tile arriving inside the window would therefore sit on the phone
 * until some later frame's first call — a torn page for as long as nothing
 * else changes. Pacing the calls ourselves costs at most 10 ms a tile and
 * never strands one. Opt-in like the burst window; main.ts passes it.
 */
export const TILE_CALL_GAP_MS = 110;

export interface HudVisualSurfaceOptions {
  /** Burst window in ms; 0 (the default) renders the moment a frame lands. */
  coalesceMs?: number;
  /** Per text/tile bridge write deadline in ms; 0 waits forever. */
  uploadTimeoutMs?: number;
  /** Minimum ms from one tile call to the next; 0 (the default) sends back to back. */
  tileGapMs?: number;
  /**
   * The spine's state word for this frame. When supplied, the word is
   * written to the native SpineLabel container instead of being painted
   * into a tile — Even's own guidance: "text updates are much faster than
   * image updates; use text for anything that needs to feel instant".
   * Omitted in fixtures that only exercise tiles.
   */
  spineLabel?: (frame: HudVisualFrame) => string;
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
    timer = setTimeout(() => reject(new Error(`${what} did not settle within ${ms}ms`)), ms);
  });
  return Promise.race([work, deadline]).finally(() => clearTimeout(timer));
}

interface PendingPaint {
  frame: HudVisualFrame;
  localTick: boolean;
  isCurrent?: () => boolean;
  displayCurrent: () => boolean;
  resolve: Array<(painted: boolean) => void>;
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
  const expectedLength = TILE_WIDTH * TILE_HEIGHT;
  if (tile.pixels.length !== expectedLength) {
    throw new Error(`expected ${expectedLength} grayscale tile pixels, got ${tile.pixels.length}`);
  }
  const canvas = document.createElement('canvas');
  canvas.width = TILE_WIDTH;
  canvas.height = TILE_HEIGHT;
  const context = canvas.getContext('2d', { alpha: false });
  if (!context) throw new Error('2D canvas is unavailable for HUD tile encoding');
  const image = context.createImageData(TILE_WIDTH, TILE_HEIGHT);
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
  private readonly spineLabel?: (frame: HudVisualFrame) => string;
  private readonly tileGapMs: number;
  private lastTileCallAt = Number.NEGATIVE_INFINITY;
  /** What the SpineLabel container is showing right now. Empty means the
   * page was (re)built and the container is back to its ' ' placeholder. */
  private writtenSpineLabel = '';
  private pageRevision = 0;
  private displayActive = true;
  private displayRevision = 0;

  constructor(
    private readonly client: BridgeClient,
    private readonly renderer: HudFrameRenderer,
    private readonly diagnostics?: G2DiagnosticTrace,
    private readonly imageEncoder: HudImageEncoder = async (tile) => tile.pixels,
    options: HudVisualSurfaceOptions = {},
  ) {
    this.coalesceMs = Math.max(0, options.coalesceMs ?? 0);
    this.uploadTimeoutMs = Math.max(0, options.uploadTimeoutMs ?? TILE_UPLOAD_TIMEOUT_MS);
    this.spineLabel = options.spineLabel;
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
    this.writtenSpineLabel = '';
  }

  suspendDisplay(): void {
    if (!this.displayActive) return;
    this.displayActive = false;
    this.displayRevision += 1;
    this.forgetPage();
    for (const pending of this.queue.splice(0)) pending.resolve.forEach((resolve) => resolve(false));
  }

  resumeDisplay(): void {
    this.displayActive = true;
  }

  /**
   * Put the state word on the glass before anything else in the frame.
   *
   * This is the whole point of the native container: "listening" ->
   * "hearing you..." -> a reply is a few bytes of textContainerUpgrade that
   * lands immediately, where painting the same word into the top-left tile
   * cost a 0.5-2 s image write per change (Even Realities' own figure) and
   * put every other update behind it in the one paint lane.
   *
   * `contentLength` covers the LONGER of the old and new words: the
   * firmware replaces a span, so a short word after a long one must erase
   * the tail it is replacing rather than leave it standing.
   *
   * A false return means the container is not on the page — the host
   * rebuilt it underneath us. Forget both the word and the delivered tiles
   * and fail the frame, so the presenter heals the page and the next paint
   * writes everything again.
   */
  private async writeSpineLabel(frame: HudVisualFrame, isCurrent: () => boolean): Promise<boolean> {
    if (!isCurrent()) return false;
    if (!this.spineLabel) return true;
    const label = pxTruncate(this.spineLabel(frame), SPINE_LABEL_BOX.width);
    if (label === this.writtenSpineLabel) return true;
    const contentLength = Math.max(this.writtenSpineLabel.length, label.length);
    const write = this.client.require().textContainerUpgrade(
      new TextContainerUpgrade({
        containerID: ContainerId.SpineLabel,
        containerName: SPINE_LABEL_NAME,
        contentOffset: 0,
        contentLength,
        content: label,
      }),
    );
    // First light precedes MacLink.connect(). A host that never settles
    // this native call must release the frame to the existing HUD fallback,
    // just as a stalled tile upload does, instead of blocking all networking.
    const ok = this.uploadTimeoutMs > 0
      ? await settleWithin(write, this.uploadTimeoutMs, 'spine label write')
      : await write;
    if (!ok || !isCurrent()) {
      this.writtenSpineLabel = '';
      this.deliveredTiles.clear();
      return false;
    }
    this.writtenSpineLabel = label;
    return true;
  }

  scrollTranscript(direction: 'up' | 'down'): boolean {
    return this.renderer.scrollTranscript?.(direction) ?? false;
  }

  transcriptIsThinking(): boolean {
    return this.renderer.transcriptIsThinking?.() ?? false;
  }

  paint(frame: HudVisualFrame, localTick = false): Promise<boolean> {
    return this.enqueue(frame, localTick);
  }

  paintExact(frame: HudVisualFrame, isCurrent: () => boolean): Promise<boolean> {
    const pageRevision = this.pageRevision;
    return this.enqueue(frame, false, () => pageRevision === this.pageRevision && isCurrent());
  }

  private enqueue(frame: HudVisualFrame, localTick: boolean, isCurrent?: () => boolean): Promise<boolean> {
    const displayRevision = this.displayRevision;
    const bridgeCurrent = this.client.captureDisplayGuard?.() ?? (() => true);
    const displayCurrent = () => this.displayActive && this.displayRevision === displayRevision && bridgeCurrent();
    if (!displayCurrent()) return Promise.resolve(false);
    return new Promise((resolve) => {
      const pending = this.queue[this.queue.length - 1];
      if (pending && pending.displayCurrent() && !isCurrent && !pending.isCurrent
        && frame.kind !== 'navigation' && pending.frame.kind !== 'navigation') {
        // A full frame supersedes every older not-yet-started frame. Keeping
        // only the latest prevents streamed text/countdown ticks from building
        // a stale multi-second BLE backlog while preserving one command lane.
        // Navigation is excluded: its caller emits a semantic receipt only
        // after this exact four-tile frame finishes, so acknowledging a newer
        // generation on behalf of an older one would corrupt HUD/input truth.
        pending.frame = frame;
        // A real frame superseding a queued tick is a real frame.
        pending.localTick = pending.localTick && localTick;
        pending.resolve.push(resolve);
      } else {
        this.queue.push({ frame, localTick, isCurrent, displayCurrent, resolve: [resolve] });
      }
      if (!this.painting) {
        this.painting = true;
        void this.drain();
      }
    });
  }

  private async drain(): Promise<void> {
    for (;;) {
      // The window stays open while the frame is still queued: whatever
      // lands during it supersedes that frame instead of following it.
      if (this.coalesceMs > 0) {
        await new Promise<void>((resolve) => { setTimeout(resolve, this.coalesceMs); });
      }
      const next = this.queue.shift();
      if (!next) break;
      const painted = await this.paintNow(
        next.frame, next.localTick, () => next.displayCurrent() && (next.isCurrent?.() ?? true),
      );
      next.resolve.forEach((resolve) => resolve(painted));
    }
    this.painting = false;
  }

  private async paintNow(frame: HudVisualFrame, localTick = false, guard?: () => boolean): Promise<boolean> {
    const frameNumber = ++this.frameNumber;
    const pageRevision = this.pageRevision;
    const isCurrent = (): boolean => pageRevision === this.pageRevision && (guard?.() ?? true);
    let current: { order: number; tile: HudImageTile; startedAt: number; bytes: number } | undefined;
    try {
      // The word first: it is the fastest thing on the page and the thing
      // the wearer is waiting for. Tiles follow at BLE pace.
      if (!(await this.writeSpineLabel(frame, isCurrent)) || !isCurrent()) return false;
      if (import.meta.env.DEV) console.info('[blank-trace]', Date.now(), 'render-start', frame.kind, pageRevision);
      const tiles = await this.renderer.render(frame);
      if (import.meta.env.DEV) console.info('[blank-trace]', Date.now(), 'render-result', frame.kind, tiles.length,
        tiles.map(t => t.pixels.reduce((n, v) => n + (v > 0 ? 1 : 0), 0)));
      if (!isCurrent()) return false;
      // Navigation acknowledges an exact route generation only after four
      // bridge writes. Confirmation is an exclusive decision surface and may
      // never inherit a quadrant from the page beneath it.
      const requiresCompleteTransfer = frame.kind === 'confirmation' || frame.kind === 'navigation';
      for (const [order, tile] of tiles.entries()) {
        if (!isCurrent()) return false;
        current = { order, tile, startedAt: now(), bytes: 0 };
        if (
          !requiresCompleteTransfer
          && pixelsEqual(this.deliveredTiles.get(tile.containerId), tile.pixels)
        ) {
          if (import.meta.env.DEV) console.info('[blank-trace]', Date.now(), 'cache-skip', tile.containerId);
          continue;
        }
        const containerName = VisualContainerName[tile.containerId];
        if (!containerName) throw new Error(`unknown visual container ${tile.containerId}`);
        const imageData = await this.imageEncoder(tile);
        if (!isCurrent()) return false;
        current.bytes = imageData.length;
        if (this.tileGapMs > 0) {
          const wait = this.tileGapMs - (now() - this.lastTileCallAt);
          if (wait > 0) await new Promise<void>((resolve) => { setTimeout(resolve, wait); });
        }
        if (!isCurrent()) return false;
        // Timed from the bridge call, not the encode: the phone's LZ4 and
        // the BLE hop are what the number has to expose.
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
        const result = this.uploadTimeoutMs > 0
          ? await settleWithin(upload, this.uploadTimeoutMs, `${containerName} upload`)
          : await upload;
        if (import.meta.env.DEV) console.info('[blank-trace]', Date.now(), 'upload-result', tile.containerId, result);
        if (!isCurrent()) {
          // A late native write may have changed this tile even though its
          // owner was cancelled. Never reuse the previous pixel cache for it.
          this.deliveredTiles.delete(tile.containerId);
          return false;
        }
        const outcome = result === ImageRawDataUpdateResult.success ? 'success' : 'failed';
        this.diagnostics?.record({
          type: 'tile_upload',
          frame: frameNumber,
          order,
          containerId: tile.containerId as 4 | 8 | 9 | 10,
          outcome,
          durationMs: elapsedSince(current.startedAt),
          bytes: current.bytes,
        });
        if (outcome === 'failed') {
          this.deliveredTiles.clear();
          this.writtenSpineLabel = '';
          return false;
        }
        this.deliveredTiles.set(tile.containerId, tile.pixels.slice());
      }
      // This opt-in receipt binds the accepted G2 action to the completed
      // four-tile output without retaining a frame, text, topic, or device
      // identifier. Hashing is detached: it must not delay a visual ack or a
      // generation-bound navigation action after those four uploads succeed.
      // A clock tick is not a new wearer-visible frame. Recording one
      // every second floods the acceptance trace — which is bounded at
      // MAX_RETAINED_EVENTS — and the whole receipt collapses to an empty
      // "fail" that names no cause. The counter still DRAWS; it just does
      // not claim the Mac told the lens anything new.
      if (!isCurrent()) return false;
      if (localTick) return true;
      void completedVisualFrameReceipt(frame, tiles)
        .then((receipt) => { if (receipt) this.diagnostics?.record(receipt); })
        .catch(() => undefined);
      return true;
    } catch (error) {
      this.deliveredTiles.clear();
      this.writtenSpineLabel = '';
      if (current) {
        this.diagnostics?.record({
          type: 'tile_upload',
          frame: frameNumber,
          order: current.order,
          containerId: current.tile.containerId as 4 | 8 | 9 | 10,
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
    }
  }
}
