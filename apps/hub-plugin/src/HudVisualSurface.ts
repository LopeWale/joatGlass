import {
  ImageRawDataUpdate,
  ImageRawDataUpdateResult,
} from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import type { G2DiagnosticTrace, G2TraceEvent } from './G2DiagnosticTrace';
import type { NavigationHudModel } from './NavigationHud';
import {
  CANVAS,
  ContainerId,
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

export interface AssistantOverlayVisual {
  text: string;
}

type HudVisualBaseFrame =
  | {
    kind: 'overview';
    status: string;
    micOn: boolean;
    mainBrightness: number;
    main: string;
    mainKind?: HudContentKind;
    context: string;
    tray: string;
    hint: string;
  }
  | {
    kind: 'sidebar';
    status: string;
    micOn: boolean;
    mainBrightness: number;
    mainKind?: HudContentKind;
    items: string[];
    selectedIndex: number;
    main: string;
    context: string;
    tray: string;
    hint: string;
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
  }
  | {
    kind: 'confirmation';
    status: string;
    micOn: boolean;
    prompt: string;
    secondsRemaining: number;
    reviewDomain?: ProductivityHudDomain;
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

/** The assistant layer is composited into the same four G2 images, not a
 * fifth native image container. That preserves the current feature page. */
export type HudVisualFrame = HudVisualBaseFrame & {
  assistantOverlay?: AssistantOverlayVisual;
};

export interface HudFrameRenderer {
  render(frame: HudVisualFrame): Promise<readonly HudImageTile[]>;
}

export interface HudFramePainter {
  paint(frame: HudVisualFrame): Promise<boolean>;
}

/** Converts one raw raster tile into an image format the native bridge and
 * the official simulator both decode. */
export type HudImageEncoder = (tile: HudImageTile) => Promise<Uint8Array>;

async function completedBrainstormFrameReceipt(
  frame: HudVisualFrame,
  tiles: readonly HudImageTile[],
): Promise<Extract<G2TraceEvent, { type: 'visual_frame' }> | undefined> {
  if (frame.kind !== 'brainstorm' || tiles.length !== 4 || !globalThis.crypto?.subtle) {
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
  return {
    type: 'visual_frame',
    kind: 'brainstorm',
    surface: frame.brainstorm.screen,
    state: frame.brainstorm.state,
    sha256,
  };
}

interface PendingPaint {
  frame: HudVisualFrame;
  resolve: Array<(painted: boolean) => void>;
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
  private frameNumber = 0;

  constructor(
    private readonly client: BridgeClient,
    private readonly renderer: HudFrameRenderer,
    private readonly diagnostics?: G2DiagnosticTrace,
    private readonly imageEncoder: HudImageEncoder = async (tile) => tile.pixels,
  ) {}

  paint(frame: HudVisualFrame): Promise<boolean> {
    return new Promise((resolve) => {
      if (!this.painting) {
        this.painting = true;
        void this.drain({ frame, resolve: [resolve] });
        return;
      }
      const pending = this.queue[this.queue.length - 1];
      if (pending && frame.kind !== 'navigation' && pending.frame.kind !== 'navigation') {
        // A full frame supersedes every older not-yet-started frame. Keeping
        // only the latest prevents streamed text/countdown ticks from building
        // a stale multi-second BLE backlog while preserving one command lane.
        // Navigation is excluded: its caller emits a semantic receipt only
        // after this exact four-tile frame finishes, so acknowledging a newer
        // generation on behalf of an older one would corrupt HUD/input truth.
        pending.frame = frame;
        pending.resolve.push(resolve);
      } else {
        this.queue.push({ frame, resolve: [resolve] });
      }
    });
  }

  private async drain(current: PendingPaint): Promise<void> {
    let next: PendingPaint | undefined = current;
    while (next) {
      const painted = await this.paintNow(next.frame);
      next.resolve.forEach((resolve) => resolve(painted));
      next = this.queue.shift();
    }
    this.painting = false;
  }

  private async paintNow(frame: HudVisualFrame): Promise<boolean> {
    const frameNumber = ++this.frameNumber;
    let current: { order: number; tile: HudImageTile } | undefined;
    try {
      const tiles = await this.renderer.render(frame);
      for (const [order, tile] of tiles.entries()) {
        current = { order, tile };
        const containerName = VisualContainerName[tile.containerId];
        if (!containerName) throw new Error(`unknown visual container ${tile.containerId}`);
        const imageData = await this.imageEncoder(tile);
        const result = await this.client.require().updateImageRawData(
          new ImageRawDataUpdate({
            containerID: tile.containerId,
            containerName,
            // The phone bridge is a Dart List<int> boundary.  Keep the
            // payload as a normal JS array instead of relying on a typed-array
            // structured-clone conversion that the host does not guarantee.
            imageData: Array.from(imageData),
          }),
        );
        const outcome = result === ImageRawDataUpdateResult.success ? 'success' : 'failed';
        this.diagnostics?.record({
          type: 'tile_upload',
          frame: frameNumber,
          order,
          containerId: tile.containerId as 4 | 8 | 9 | 10,
          outcome,
        });
        if (outcome === 'failed') return false;
      }
      // This opt-in receipt binds the accepted G2 action to the completed
      // four-tile output without retaining a frame, text, topic, or device
      // identifier. Hashing is detached: it must not delay a visual ack or a
      // generation-bound navigation action after those four uploads succeed.
      void completedBrainstormFrameReceipt(frame, tiles)
        .then((receipt) => { if (receipt) this.diagnostics?.record(receipt); })
        .catch(() => undefined);
      return true;
    } catch {
      if (current) {
        this.diagnostics?.record({
          type: 'tile_upload',
          frame: frameNumber,
          order: current.order,
          containerId: current.tile.containerId as 4 | 8 | 9 | 10,
          outcome: 'error',
        });
      }
      console.error('[HudVisualSurface] frame paint failed');
      return false;
    }
  }
}
