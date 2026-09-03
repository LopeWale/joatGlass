import { describe, expect, it, vi } from 'vitest';
import { ImageRawDataUpdateResult } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import {
  HudVisualSurface,
  splitGrayscaleTiles,
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
    ]);
  });

  it('encodes and uploads only tiles whose rendered pixels changed', async () => {
    const encoded: number[] = [];
    const uploaded: number[] = [];
    let topLeft = new Uint8Array([1]);
    let topRight = new Uint8Array([2]);
    const bottomLeft = new Uint8Array([3]);
    const bottomRight = new Uint8Array([4]);
    const client = {
      require: () => ({
        updateImageRawData: async (update: { containerID?: number }) => {
          uploaded.push(update.containerID ?? -1);
          return ImageRawDataUpdateResult.success;
        },
      }),
    } as unknown as BridgeClient;
    const renderer: HudFrameRenderer = {
      render: async () => [
        { containerId: ContainerId.Image, pixels: topLeft },
        { containerId: ContainerId.VisualTopRight, pixels: topRight },
        { containerId: ContainerId.VisualBottomLeft, pixels: bottomLeft },
        { containerId: ContainerId.VisualBottomRight, pixels: bottomRight },
      ],
    };
    const encode = vi.fn(async (tile: { containerId: number; pixels: Uint8Array }) => {
      encoded.push(tile.containerId);
      return tile.pixels;
    });
    const frame: HudVisualFrame = {
      kind: 'overview', status: '', micOn: false, mainBrightness: 4,
      main: '', context: '', tray: '', hint: '',
    };
    const surface = new HudVisualSurface(client, renderer, undefined, encode);

    expect(await surface.paint(frame)).toBe(true);
    topLeft = new Uint8Array([5]);
    topRight = new Uint8Array([6]);
    expect(await surface.paint(frame)).toBe(true);

    expect(encoded).toEqual([4, 8, 9, 10, 4, 8]);
    expect(uploaded).toEqual([4, 8, 9, 10, 4, 8]);
  });

  it('invalidates delivered-tile state after an upload failure', async () => {
    const uploaded: number[] = [];
    let failTopRight = true;
    const client = {
      require: () => ({
        updateImageRawData: async (update: { containerID?: number }) => {
          const containerId = update.containerID ?? -1;
          uploaded.push(containerId);
          if (containerId === ContainerId.VisualTopRight && failTopRight) {
            failTopRight = false;
            return ImageRawDataUpdateResult.sendFailed;
          }
          return ImageRawDataUpdateResult.success;
        },
      }),
    } as unknown as BridgeClient;
    const renderer: HudFrameRenderer = {
      render: async () => [
        { containerId: ContainerId.Image, pixels: new Uint8Array([1]) },
        { containerId: ContainerId.VisualTopRight, pixels: new Uint8Array([2]) },
        { containerId: ContainerId.VisualBottomLeft, pixels: new Uint8Array([3]) },
        { containerId: ContainerId.VisualBottomRight, pixels: new Uint8Array([4]) },
      ],
    };
    const frame: HudVisualFrame = {
      kind: 'overview', status: '', micOn: false, mainBrightness: 4,
      main: '', context: '', tray: '', hint: '',
    };
    const surface = new HudVisualSurface(client, renderer);

    expect(await surface.paint(frame)).toBe(false);
    expect(await surface.paint(frame)).toBe(true);

    expect(uploaded).toEqual([4, 8, 4, 8, 9, 10]);
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
    expect(error).toHaveBeenCalledWith('[HudVisualSurface] frame paint failed:', 'missing icon');
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

    // Each receipt carries the bridge round trip and the encoded size —
    // the two numbers that locate a slow HUD on the phone or on the BLE
    // hop — and never the pixels themselves.
    expect(events).toEqual([
      { type: 'tile_upload', frame: 1, order: 0, containerId: 4, outcome: 'success', durationMs: expect.any(Number), bytes: 3 },
      { type: 'tile_upload', frame: 1, order: 1, containerId: 8, outcome: 'success', durationMs: expect.any(Number), bytes: 1 },
      { type: 'tile_upload', frame: 1, order: 2, containerId: 9, outcome: 'success', durationMs: expect.any(Number), bytes: 1 },
      { type: 'tile_upload', frame: 1, order: 3, containerId: 10, outcome: 'success', durationMs: expect.any(Number), bytes: 1 },
    ]);
    for (const event of events as Array<{ durationMs: number }>) {
      expect(event.durationMs).toBeGreaterThanOrEqual(0);
    }
  });

  it('writes the spine word natively before any tile, and only when it changes', async () => {
    // "listening → hearing you…" is a few bytes of textContainerUpgrade
    // now, never a 0.5-2 s image tile — and it lands FIRST, so the wearer
    // sees the word while the tiles that changed follow at BLE pace.
    const calls: Array<{ kind: 'text' | 'tile'; content?: string; length?: number }> = [];
    const client = {
      require: () => ({
        textContainerUpgrade: async (update: { content?: string; contentLength?: number; containerID?: number; containerName?: string }) => {
          expect(update.containerID).toBe(ContainerId.SpineLabel);
          expect(update.containerName).toBe('spine-label');
          calls.push({ kind: 'text', content: update.content, length: update.contentLength });
          return true;
        },
        updateImageRawData: async () => {
          calls.push({ kind: 'tile' });
          return ImageRawDataUpdateResult.success;
        },
      }),
    } as unknown as BridgeClient;
    let pixel = 0;
    const renderer: HudFrameRenderer = {
      // Every frame differs in one tile, so a tile write follows each label.
      render: async () => [
        { containerId: ContainerId.Image, pixels: new Uint8Array([pixel += 1]) },
        { containerId: ContainerId.VisualTopRight, pixels: new Uint8Array(1) },
        { containerId: ContainerId.VisualBottomLeft, pixels: new Uint8Array(1) },
        { containerId: ContainerId.VisualBottomRight, pixels: new Uint8Array(1) },
      ],
    };
    const frame = (status: string): HudVisualFrame => ({
      kind: 'overview', status, micOn: false, mainBrightness: 4,
      main: '', context: '', tray: '', hint: '',
    });
    const surface = new HudVisualSurface(client, renderer, undefined, undefined, {
      spineLabel: (candidate) => candidate.status,
    });

    expect(await surface.paint(frame('JOAT · LISTENING'))).toBe(true);
    expect(calls[0]).toEqual({ kind: 'text', content: 'JOAT · LISTENING', length: 16 });
    expect(calls.filter((call) => call.kind === 'tile')).toHaveLength(4);

    calls.length = 0;
    expect(await surface.paint(frame('JOAT · LISTENING'))).toBe(true);
    expect(calls.filter((call) => call.kind === 'text')).toEqual([]);

    calls.length = 0;
    expect(await surface.paint(frame('JOAT · HEARING YOU…'))).toBe(true);
    expect(calls[0]).toEqual({ kind: 'text', content: 'JOAT · HEARING YOU…', length: 19 });

    // A shorter word still overwrites the whole longer one.
    calls.length = 0;
    expect(await surface.paint(frame('NEWS'))).toBe(true);
    expect(calls[0]).toEqual({ kind: 'text', content: 'NEWS', length: 19 });
  });

  it('truncates the spine word to the native box with the firmware font metrics', async () => {
    let written = '';
    const client = {
      require: () => ({
        textContainerUpgrade: async (update: { content?: string }) => {
          written = update.content ?? '';
          return true;
        },
        updateImageRawData: async () => ImageRawDataUpdateResult.success,
      }),
    } as unknown as BridgeClient;
    const renderer: HudFrameRenderer = {
      render: async () => [{ containerId: ContainerId.Image, pixels: new Uint8Array(1) }],
    };
    const long = 'JOAT · A STATE WORD FAR LONGER THAN THE SPINE BOX CAN HOLD';
    const frame: HudVisualFrame = {
      kind: 'overview', status: long, micOn: false, mainBrightness: 4,
      main: '', context: '', tray: '', hint: '',
    };
    await new HudVisualSurface(client, renderer, undefined, undefined, {
      spineLabel: (candidate) => candidate.status,
    }).paint(frame);
    expect(written.endsWith('...')).toBe(true);
    expect(written.length).toBeLessThan(long.length);
    expect(long.startsWith(written.slice(0, -3))).toBe(true);
  });

  it('fails the frame when the spine container is gone, and rewrites the word after the page heals', async () => {
    let textOk = false;
    const writes: string[] = [];
    let tiles = 0;
    const client = {
      require: () => ({
        textContainerUpgrade: async (update: { content?: string }) => {
          writes.push(update.content ?? '');
          return textOk;
        },
        updateImageRawData: async () => { tiles += 1; return ImageRawDataUpdateResult.success; },
      }),
    } as unknown as BridgeClient;
    const renderer: HudFrameRenderer = {
      render: async () => [{ containerId: ContainerId.Image, pixels: new Uint8Array([7]) }],
    };
    const frame: HudVisualFrame = {
      kind: 'overview', status: 'JOAT · READY', micOn: false, mainBrightness: 4,
      main: '', context: '', tray: '', hint: '',
    };
    const surface = new HudVisualSurface(client, renderer, undefined, undefined, {
      spineLabel: (candidate) => candidate.status,
    });

    // The page is missing: no tile is attempted, the presenter will heal.
    expect(await surface.paint(frame)).toBe(false);
    expect(tiles).toBe(0);

    textOk = true; // the presenter rebuilt the page
    expect(await surface.paint(frame)).toBe(true);
    expect(writes).toEqual(['JOAT · READY', 'JOAT · READY']);
    expect(tiles).toBe(1);
  });

  it('paces successive tile calls past the host\'s 100 ms hold window', async () => {
    // Even's display docs: a call that arrives inside 100 ms of the last
    // one "is held and flushed on the next one" — a frame's last tile would
    // wait for a later frame that may never come.
    const startedAt: number[] = [];
    const client = {
      require: () => ({
        updateImageRawData: async () => {
          startedAt.push(performance.now());
          return ImageRawDataUpdateResult.success;
        },
      }),
    } as unknown as BridgeClient;
    const renderer: HudFrameRenderer = {
      render: async () => [
        { containerId: ContainerId.Image, pixels: new Uint8Array([1]) },
        { containerId: ContainerId.VisualTopRight, pixels: new Uint8Array([2]) },
        { containerId: ContainerId.VisualBottomLeft, pixels: new Uint8Array([3]) },
        { containerId: ContainerId.VisualBottomRight, pixels: new Uint8Array([4]) },
      ],
    };
    const frame: HudVisualFrame = {
      kind: 'overview', status: '', micOn: false, mainBrightness: 4,
      main: '', context: '', tray: '', hint: '',
    };
    const surface = new HudVisualSurface(client, renderer, undefined, undefined, { tileGapMs: 30 });
    expect(await surface.paint(frame)).toBe(true);
    expect(startedAt).toHaveLength(4);
    for (let index = 1; index < startedAt.length; index += 1) {
      expect(startedAt[index]! - startedAt[index - 1]!).toBeGreaterThanOrEqual(25);
    }
  });

  it('renders a burst of frames as one frame inside the coalescing window', async () => {
    // A voice turn's chrome arrives as separate Mac messages a few ms
    // apart. Rendering the first the instant it lands would ship the
    // spine's tile twice, at 0.5-2 s a tile over BLE.
    const rendered: string[] = [];
    const client = {
      require: () => ({
        updateImageRawData: async () => ImageRawDataUpdateResult.success,
      }),
    } as unknown as BridgeClient;
    const renderer: HudFrameRenderer = {
      render: async (frame) => {
        rendered.push(frame.kind === 'overview' ? frame.main : frame.kind);
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
    const surface = new HudVisualSurface(client, renderer, undefined, undefined, { coalesceMs: 20 });

    const first = surface.paint(frame('mic on'));
    await new Promise((resolve) => { setTimeout(resolve, 5); });
    const second = surface.paint(frame('hearing you'));
    expect(await Promise.all([first, second])).toEqual([true, true]);
    expect(rendered).toEqual(['hearing you']);

    // The window is per burst, never extended: a frame after it is its own.
    expect(await surface.paint(frame('reply'))).toBe(true);
    expect(rendered).toEqual(['hearing you', 'reply']);
  });

  it('keeps navigation frames apart even inside the coalescing window', async () => {
    const rendered: string[] = [];
    const client = {
      require: () => ({
        updateImageRawData: async () => ImageRawDataUpdateResult.success,
      }),
    } as unknown as BridgeClient;
    const renderer: HudFrameRenderer = {
      render: async (candidate) => {
        if (candidate.kind === 'navigation') rendered.push(candidate.navigation.primary);
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
    const surface = new HudVisualSurface(client, renderer, undefined, undefined, { coalesceMs: 20 });

    expect(await Promise.all([
      surface.paint(frame('first')),
      surface.paint(frame('latest')),
    ])).toEqual([true, true]);
    expect(rendered).toEqual(['first', 'latest']);
  });

  it('fails a frame whose tile upload never settles, so the lane drains', async () => {
    // A dropped bridge promise used to wedge the single paint lane: every
    // later frame queued behind it forever and the HUD simply stopped.
    const events: Array<{ outcome: string; durationMs?: number }> = [];
    let calls = 0;
    const client = {
      require: () => ({
        updateImageRawData: () => {
          calls += 1;
          if (calls === 1) return new Promise<ImageRawDataUpdateResult>(() => undefined);
          return Promise.resolve(ImageRawDataUpdateResult.success);
        },
      }),
    } as unknown as BridgeClient;
    const renderer: HudFrameRenderer = {
      render: async () => [
        { containerId: ContainerId.Image, pixels: new Uint8Array([1]) },
        { containerId: ContainerId.VisualTopRight, pixels: new Uint8Array([2]) },
        { containerId: ContainerId.VisualBottomLeft, pixels: new Uint8Array([3]) },
        { containerId: ContainerId.VisualBottomRight, pixels: new Uint8Array([4]) },
      ],
    };
    const frame: HudVisualFrame = {
      kind: 'overview', status: '', micOn: false, mainBrightness: 4,
      main: '', context: '', tray: '', hint: '',
    };
    const surface = new HudVisualSurface(
      client,
      renderer,
      { record: (event) => { if (event.type === 'tile_upload') events.push(event); } },
      undefined,
      { uploadTimeoutMs: 15 },
    );

    expect(await surface.paint(frame)).toBe(false);
    expect(events).toEqual([expect.objectContaining({ outcome: 'error', order: 0 })]);
    expect(events[0]?.durationMs).toBeGreaterThanOrEqual(10);

    // The lane is free, and nothing is remembered as delivered: the next
    // frame ships all four tiles again.
    expect(await surface.paint(frame)).toBe(true);
    expect(calls).toBe(5);
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
        screen: 'agent_terminal', state: 'running', topic: 'not retained',
        collaborators: [], activity: 'not retained',
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

  it('records a completed Messages conversation raster without retaining its text', async () => {
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
      kind: 'list', status: '', micOn: false, title: 'MESSAGES · CONVERSATION',
      items: ['Private sender · private message body'], selectedIndex: 0,
      selectable: true, hint: 'say reply',
    };

    await new HudVisualSurface(client, renderer, {
      record: (event) => events.push(event),
    }).paint(frame);

    await vi.waitFor(() => expect(events.some(
      (event) => (event as { type?: string; kind?: string }).type === 'visual_frame'
        && (event as { kind?: string }).kind === 'messages',
    )).toBe(true));
    const receipt = events.find((event) => (
      (event as { type?: string; kind?: string }).type === 'visual_frame'
      && (event as { kind?: string }).kind === 'messages'
    ));
    expect(receipt).toEqual({
      type: 'visual_frame', kind: 'messages', surface: 'conversation',
      sha256: expect.stringMatching(/^[0-9a-f]{64}$/),
    });
    expect(JSON.stringify(receipt)).not.toMatch(/private|sender|body|pixels/i);
  });
});
