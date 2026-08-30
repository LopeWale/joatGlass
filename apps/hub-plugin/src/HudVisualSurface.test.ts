import { describe, expect, it, vi } from 'vitest';
import { ImageRawDataUpdateResult } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import {
  HudVisualSurface,
  splitGrayscaleTiles,
  visibleSurfaceReceipt,
  type HudFrameRenderer,
  type HudVisualFrame,
} from './HudVisualSurface';
import { ContainerId } from './types';

describe('splitGrayscaleTiles', () => {
  it('maps the raw Gray8 canvas into four ordered SDK images', () => {
    const pixels = new Uint8Array(576 * 288);
    pixels[0] = 1;
    pixels[287] = 2;
    pixels[288] = 3;
    pixels[576 * 144] = 4;
    pixels[576 * 144 + 288] = 5;

    const tiles = splitGrayscaleTiles(pixels);
    expect(tiles.map((tile) => tile.containerId)).toEqual([
      ContainerId.Image,
      ContainerId.VisualTopRight,
      ContainerId.VisualBottomLeft,
      ContainerId.VisualBottomRight,
    ]);
    expect(tiles.every((tile) => tile.pixels.length === 41_472)).toBe(true);
    expect(tiles[0]?.pixels[0]).toBe(1);
    expect(tiles[0]?.pixels[287]).toBe(2);
    expect(tiles[1]?.pixels[0]).toBe(3);
    expect(tiles[2]?.pixels[0]).toBe(4);
    expect(tiles[3]?.pixels[0]).toBe(5);
  });
});

describe('HudVisualSurface', () => {
  it('marks only a completed Reminders due frame with a content-free receipt key', () => {
    const due: HudVisualFrame = {
      kind: 'productivity', status: '', micOn: false,
      productivity: {
        domain: 'reminders', phase: 'due', title: 'private title',
        primary: 'Due now', secondary: 'private subtitle', hint: 'left x2 Planning',
      },
    };
    const saved: HudVisualFrame = {
      ...due,
      productivity: { ...due.productivity, phase: 'saved' },
    };

    expect(visibleSurfaceReceipt(due)).toBe('reminder_due');
    expect(visibleSurfaceReceipt(saved)).toBeUndefined();
    expect(JSON.stringify(visibleSurfaceReceipt(due))).not.toContain('private');
  });

  it('addresses every raw Gray8 upload by the exact visual container ID and name', async () => {
    const payloads: Array<{ imageData?: unknown; containerName?: unknown }> = [];
    const client = {
      require: () => ({
        updateImageRawData: async (update: { imageData?: unknown; containerName?: unknown }) => {
          payloads.push(update);
          return ImageRawDataUpdateResult.success;
        },
      }),
    } as unknown as BridgeClient;
    const pixels = new Uint8Array(576 * 288);
    pixels.fill(170);
    pixels[0] = 0;
    pixels[576 * 288 - 1] = 255;
    const renderer: HudFrameRenderer = { render: async () => splitGrayscaleTiles(pixels) };
    const frame: HudVisualFrame = {
      kind: 'overview', status: '', micOn: false, mainBrightness: 4,
      main: '', context: '', tray: '', hint: '',
    };

    await new HudVisualSurface(client, renderer).paint(frame);

    expect(payloads).toHaveLength(4);
    expect(payloads.map((payload) => payload.containerName)).toEqual([
      'visual-tl', 'visual-tr', 'visual-bl', 'visual-br',
    ]);
    expect(payloads.every((payload) => Array.isArray(payload.imageData) && payload.imageData.length === 41_472)).toBe(true);
    expect((payloads[0]?.imageData as number[])[0]).toBe(0);
    expect((payloads[3]?.imageData as number[])[41_471]).toBe(255);
    expect(payloads.flatMap((payload) => payload.imageData as number[]).every((value) => value <= 255)).toBe(true);
  });

  it('sends encoded image bytes when the production tile encoder is supplied', async () => {
    const payloads: unknown[] = [];
    const client = {
      require: () => ({
        updateImageRawData: async (update: { imageData?: unknown }) => {
          payloads.push(update.imageData);
          return ImageRawDataUpdateResult.success;
        },
      }),
    } as unknown as BridgeClient;
    const renderer: HudFrameRenderer = {
      render: async () => splitGrayscaleTiles(new Uint8Array(576 * 288).fill(170)),
    };
    const encoded = new Uint8Array([137, 80, 78, 71]);
    const encode = vi.fn(async () => encoded);
    const frame: HudVisualFrame = {
      kind: 'overview', status: '', micOn: false, mainBrightness: 4,
      main: '', context: '', tray: '', hint: '',
    };

    await new HudVisualSurface(client, renderer, undefined, encode).paint(frame);

    expect(encode).toHaveBeenCalledTimes(4);
    expect(payloads).toEqual(Array.from({ length: 4 }, () => [137, 80, 78, 71]));
  });

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

  it('never coalesces generation-bound navigation frames', async () => {
    const rendered: string[] = [];
    const client = {
      require: () => ({
        updateImageRawData: async () => ImageRawDataUpdateResult.success,
      }),
    } as unknown as BridgeClient;
    const renderer: HudFrameRenderer = {
      render: async (candidate) => {
        if (candidate.kind === 'navigation') rendered.push(candidate.navigation.primary);
        await Promise.resolve();
        return [
          { containerId: ContainerId.Image, pixels: new Uint8Array(4) },
          { containerId: ContainerId.VisualTopRight, pixels: new Uint8Array(4) },
          { containerId: ContainerId.VisualBottomLeft, pixels: new Uint8Array(4) },
          { containerId: ContainerId.VisualBottomRight, pixels: new Uint8Array(4) },
        ];
      },
    };
    const frame = (primary: string): HudVisualFrame => ({
      kind: 'navigation', status: '', micOn: false,
      navigation: {
        view: 'guidance', primary, secondary: 'onto Test Ave', hint: 'continue',
        interaction: 'none', options: [], selectedIndex: 0,
      },
    });
    const surface = new HudVisualSurface(client, renderer);

    expect(await Promise.all([
      surface.paint(frame('first')),
      surface.paint(frame('middle')),
      surface.paint(frame('latest')),
    ])).toEqual([true, true, true]);
    expect(rendered).toEqual(['first', 'middle', 'latest']);
  });

  it('records the serial tile order and result without retaining pixels', async () => {
    const events: unknown[] = [];
    const client = {
      require: () => ({
        updateImageRawData: async () => ImageRawDataUpdateResult.success,
      }),
    } as unknown as BridgeClient;
    const renderer: HudFrameRenderer = {
      render: async () => [
        { containerId: ContainerId.Image, pixels: new Uint8Array([9, 8, 7]) },
        { containerId: ContainerId.VisualTopRight, pixels: new Uint8Array([6]) },
        { containerId: ContainerId.VisualBottomLeft, pixels: new Uint8Array([5]) },
        { containerId: ContainerId.VisualBottomRight, pixels: new Uint8Array([4]) },
      ],
    };
    const frame: HudVisualFrame = {
      kind: 'overview', status: '', micOn: false, mainBrightness: 4,
      main: '', context: '', tray: '', hint: '',
    };

    await new HudVisualSurface(client, renderer, {
      record: (event) => events.push(event),
    }).paint(frame);

    expect(events).toEqual([
      { type: 'tile_upload', frame: 1, order: 0, containerId: 4, outcome: 'success' },
      { type: 'tile_upload', frame: 1, order: 1, containerId: 8, outcome: 'success' },
      { type: 'tile_upload', frame: 1, order: 2, containerId: 9, outcome: 'success' },
      { type: 'tile_upload', frame: 1, order: 3, containerId: 10, outcome: 'success' },
    ]);
  });

  it('records a completed Brainstorm raster only as a fixed-state SHA-256 receipt', async () => {
    const events: unknown[] = [];
    const client = {
      require: () => ({
        updateImageRawData: async () => ImageRawDataUpdateResult.success,
      }),
    } as unknown as BridgeClient;
    const renderer: HudFrameRenderer = {
      render: async () => [
        { containerId: ContainerId.Image, pixels: new Uint8Array([9, 8, 7]) },
        { containerId: ContainerId.VisualTopRight, pixels: new Uint8Array([6]) },
        { containerId: ContainerId.VisualBottomLeft, pixels: new Uint8Array([5]) },
        { containerId: ContainerId.VisualBottomRight, pixels: new Uint8Array([4]) },
      ],
    };
    const frame: HudVisualFrame = {
      kind: 'brainstorm', status: '', micOn: false,
      brainstorm: {
        screen: 'agent_terminal', state: 'running', topic: 'not retained', collaborators: [],
        activity: 'not retained', terminalState: 'ready', noToolsChecked: true,
      },
    };

    await new HudVisualSurface(client, renderer, {
      record: (event) => events.push(event),
    }).paint(frame);

    await vi.waitFor(() => expect(events.some(
      (event) => (event as { type?: string }).type === 'visual_frame',
    )).toBe(true));
    const receipt = events.find((event) => (event as { type?: string }).type === 'visual_frame');
    expect(receipt).toEqual({
      type: 'visual_frame', kind: 'brainstorm', surface: 'agent_terminal', state: 'running',
      sha256: expect.stringMatching(/^[0-9a-f]{64}$/),
    });
    expect(JSON.stringify(receipt)).not.toMatch(/retained|topic|activity|pixels/i);
  });
});
