import {
  ImageRawDataUpdate,
  ImageRawDataUpdateResult,
} from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import type { G2DiagnosticTrace } from './G2DiagnosticTrace';
import type { NavigationHudModel } from './NavigationHud';
import {
  CANVAS,
  ContainerId,
  type BrainstormHudModel,
  type HudContentKind,
  type ProductivityHudDomain,
  type ProductivityHudModel,
} from './types';

const TILE_WIDTH = 288;
const TILE_HEIGHT = 144;

export interface HudImageTile {
  containerId: number;
  pixels: Uint8Array;
}

export type HudVisualFrame =
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

export interface HudFrameRenderer {
  render(frame: HudVisualFrame): Promise<readonly HudImageTile[]>;
}

export interface HudFramePainter {
  paint(frame: HudVisualFrame): Promise<boolean>;
}

interface PendingPaint {
  frame: HudVisualFrame;
  resolve: Array<(painted: boolean) => void>;
}

/**
 * Split one row-major *unpacked* Gray4 frame into the SDK's four maximum-size
 * images, then pack two 4-bit pixels into each byte for the native transport.
 *
 * The host determines raw format from byte length: 288 × 144 bytes means
 * Gray8, while 288 × 144 ÷ 2 means packed Gray4. Sending one 0...15 value per
 * byte is neither representation and is acknowledged without a valid raster.
 */
export function splitGray4Tiles(pixels: Uint8Array): HudImageTile[] {
  if (pixels.length !== CANVAS.width * CANVAS.height) {
    throw new Error(`expected ${CANVAS.width * CANVAS.height} Gray4 pixels, got ${pixels.length}`);
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
    const packed = new Uint8Array(unpacked.length / 2);
    for (let source = 0, target = 0; source < unpacked.length; source += 2, target += 1) {
      packed[target] = ((unpacked[source] ?? 0) << 4) | (unpacked[source + 1] ?? 0);
    }
    return { containerId, pixels: packed };
  });
}

export class HudVisualSurface implements HudFramePainter {
  private painting = false;
  private readonly queue: PendingPaint[] = [];
  private frameNumber = 0;

  constructor(
    private readonly client: BridgeClient,
    private readonly renderer: HudFrameRenderer,
    private readonly diagnostics?: G2DiagnosticTrace,
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
        const result = await this.client.require().updateImageRawData(
          new ImageRawDataUpdate({
            containerID: tile.containerId,
            // The phone bridge is a Dart List<int> boundary.  Keep the
            // payload as a normal JS array instead of relying on a typed-array
            // structured-clone conversion that the host does not guarantee.
            imageData: Array.from(tile.pixels),
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
      if (import.meta.env.DEV) {
        console.info(`[HudVisualSurface] frame ${frameNumber} bridge-ack`);
      }
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
