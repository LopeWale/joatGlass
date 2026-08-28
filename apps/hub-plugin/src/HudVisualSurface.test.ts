import { describe, expect, it, vi } from 'vitest';
import { ImageRawDataUpdateResult } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import {
  HudVisualSurface,
  splitGray4Tiles,
  type HudFrameRenderer,
  type HudVisualFrame,
} from './HudVisualSurface';
import { ContainerId } from './types';

describe('splitGray4Tiles', () => {
  it('maps the 576x288 canvas into four ordered 288x144 SDK images', () => {
    const pixels = new Uint8Array(576 * 288);
    pixels[0] = 1;
    pixels[287] = 2;
    pixels[288] = 3;
    pixels[576 * 144] = 4;
    pixels[576 * 144 + 288] = 5;

    const tiles = splitGray4Tiles(pixels);
    expect(tiles.map((tile) => tile.containerId)).toEqual([
      ContainerId.Image,
      ContainerId.VisualTopRight,
      ContainerId.VisualBottomLeft,
      ContainerId.VisualBottomRight,
    ]);
    expect(tiles.every((tile) => tile.pixels.length === 288 * 144)).toBe(true);
    expect(tiles[0]?.pixels[0]).toBe(1);
    expect(tiles[0]?.pixels[287]).toBe(2);
    expect(tiles[1]?.pixels[0]).toBe(3);
    expect(tiles[2]?.pixels[0]).toBe(4);
    expect(tiles[3]?.pixels[0]).toBe(5);
  });
});

describe('HudVisualSurface', () => {
  it('awaits all four image updates serially', async () => {
    let active = 0;
    let maxActive = 0;
    const calls: number[] = [];
    const client = {
      require: () => ({
        updateImageRawData: async (update: { containerID?: number }) => {
          active += 1;
          maxActive = Math.max(maxActive, active);
          calls.push(update.containerID ?? -1);
          await Promise.resolve();
          active -= 1;
          return ImageRawDataUpdateResult.success;
        },
      }),
    } as unknown as BridgeClient;
    const renderer: HudFrameRenderer = {
      render: async () => [
        { containerId: ContainerId.Image, pixels: new Uint8Array(4) },
        { containerId: ContainerId.VisualTopRight, pixels: new Uint8Array(4) },
        { containerId: ContainerId.VisualBottomLeft, pixels: new Uint8Array(4) },
        { containerId: ContainerId.VisualBottomRight, pixels: new Uint8Array(4) },
      ],
    };
    const frame: HudVisualFrame = {
      kind: 'overview', status: '', micOn: false, mainBrightness: 4,
      main: '', context: '', tray: '', hint: '',
    };

    const surface = new HudVisualSurface(client, renderer);
    const [first, second] = await Promise.all([surface.paint(frame), surface.paint(frame)]);
    expect(first).toBe(true);
    expect(second).toBe(true);
    expect(maxActive).toBe(1);
    expect(calls).toEqual([
      ContainerId.Image, 8, 9, 10,
      ContainerId.Image, 8, 9, 10,
    ]);
  });

  it('returns false instead of rejecting when rendering fails', async () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const client = { require: () => ({}) } as unknown as BridgeClient;
    const renderer: HudFrameRenderer = {
      render: async () => { throw new Error('missing icon'); },
    };
    const frame: HudVisualFrame = {
      kind: 'overview', status: '', micOn: false, mainBrightness: 4,
      main: '', context: '', tray: '', hint: '',
    };

    expect(await new HudVisualSurface(client, renderer).paint(frame)).toBe(false);
    expect(error).toHaveBeenCalledWith('[HudVisualSurface] frame paint failed');
    error.mockRestore();
  });

  it('coalesces not-yet-started frames to the newest complete state', async () => {
    const rendered: string[] = [];
    const client = {
      require: () => ({
        updateImageRawData: async () => ImageRawDataUpdateResult.success,
      }),
    } as unknown as BridgeClient;
    const renderer: HudFrameRenderer = {
      render: async (frame) => {
        rendered.push(frame.kind === 'overview' ? frame.main : frame.kind);
        await Promise.resolve();
        return [
          { containerId: ContainerId.Image, pixels: new Uint8Array(4) },
          { containerId: ContainerId.VisualTopRight, pixels: new Uint8Array(4) },
          { containerId: ContainerId.VisualBottomLeft, pixels: new Uint8Array(4) },
          { containerId: ContainerId.VisualBottomRight, pixels: new Uint8Array(4) },
        ];
      },
    };
    const frame = (main: string): HudVisualFrame => ({
      kind: 'overview', status: '', micOn: false, mainBrightness: 4,
      main, context: '', tray: '', hint: '',
    });
    const surface = new HudVisualSurface(client, renderer);

    await Promise.all([
      surface.paint(frame('first')),
      surface.paint(frame('stale middle')),
      surface.paint(frame('latest')),
    ]);

    expect(rendered).toEqual(['first', 'latest']);
  });
});
