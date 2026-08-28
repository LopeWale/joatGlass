import {
  ImageRawDataUpdate,
  ImageRawDataUpdateResult,
} from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import { CANVAS, ContainerId, type HudContentKind } from './types';

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
    hint: string;
  }
  | {
    kind: 'confirmation';
    status: string;
    micOn: boolean;
    prompt: string;
    secondsRemaining: number;
  };

export interface HudFrameRenderer {
  render(frame: HudVisualFrame): Promise<readonly HudImageTile[]>;
}

interface PendingPaint {
  frame: HudVisualFrame;
  resolve: Array<(painted: boolean) => void>;
}

/** Split one row-major Gray4 frame into the SDK's four maximum-size images. */
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
    const tile = new Uint8Array(TILE_WIDTH * TILE_HEIGHT);
    for (let row = 0; row < TILE_HEIGHT; row += 1) {
      const source = (y + row) * CANVAS.width + x;
      tile.set(pixels.subarray(source, source + TILE_WIDTH), row * TILE_WIDTH);
    }
    return { containerId, pixels: tile };
  });
}

export class HudVisualSurface {
  private painting = false;
  private pending?: PendingPaint;

  constructor(
    private readonly client: BridgeClient,
    private readonly renderer: HudFrameRenderer,
  ) {}

  paint(frame: HudVisualFrame): Promise<boolean> {
    return new Promise((resolve) => {
      if (!this.painting) {
        this.painting = true;
        void this.drain({ frame, resolve: [resolve] });
        return;
      }
      if (this.pending) {
        // A full frame supersedes every older not-yet-started frame. Keeping
        // only the latest prevents streamed text/countdown ticks from building
        // a stale multi-second BLE backlog while preserving one command lane.
        this.pending.frame = frame;
        this.pending.resolve.push(resolve);
      } else {
        this.pending = { frame, resolve: [resolve] };
      }
    });
  }

  private async drain(current: PendingPaint): Promise<void> {
    let next: PendingPaint | undefined = current;
    while (next) {
      const painted = await this.paintNow(next.frame);
      next.resolve.forEach((resolve) => resolve(painted));
      next = this.pending;
      this.pending = undefined;
    }
    this.painting = false;
  }

  private async paintNow(frame: HudVisualFrame): Promise<boolean> {
    try {
      const tiles = await this.renderer.render(frame);
      for (const tile of tiles) {
        const result = await this.client.require().updateImageRawData(
          new ImageRawDataUpdate({
            containerID: tile.containerId,
            imageData: tile.pixels,
          }),
        );
        if (result !== ImageRawDataUpdateResult.success) return false;
      }
      return true;
    } catch {
      console.error('[HudVisualSurface] frame paint failed');
      return false;
    }
  }
}
