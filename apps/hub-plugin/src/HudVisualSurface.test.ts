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
import { selectHudFirstLight } from './HudPageBootstrap';

describe('prompt exact paint', () => {
  const frame = (main: string): HudVisualFrame => ({
    kind: 'overview', status: '', micOn: true, mainBrightness: 4,
    main, context: '', tray: '', hint: '',
  });
  function exact(surface: HudVisualSurface, value: HudVisualFrame, isCurrent: () => boolean) {
    return surface.paintExact(value, isCurrent);
  }
  const tiles = () => [4, 8, 9, 10].map((containerId) => ({
    containerId, pixels: new Uint8Array([containerId]),
  }));

  it('delivers a queued question itself before resolving its receipt, not a coalesced successor', async () => {
    let release!: () => void;
    const held = new Promise<void>((resolve) => { release = resolve; });
    const rendered: string[] = [];
    let uploads = 0;
    const client = { require: () => ({ updateImageRawData: async () => {
      if (++uploads === 1) await held;
      return ImageRawDataUpdateResult.success;
    } }) } as unknown as BridgeClient;
    const renderer: HudFrameRenderer = { render: async (value) => {
      rendered.push(value.kind === 'overview' ? value.main : value.kind);
      return tiles();
    } };
    const surface = new HudVisualSurface(client, renderer);
    const first = surface.paint(frame('prior surface'));
    let question: Promise<boolean> | undefined;
    let successor: Promise<boolean> | undefined;
    try {
      await vi.waitFor(() => expect(uploads).toBe(1));
      question = exact(surface, frame('Who should receive the message?'), () => true);
      successor = surface.paint(frame('incidental update'));
      release();
      expect(await question).toBe(true);
      await successor;
      expect(rendered).toEqual(['prior surface', 'Who should receive the message?', 'incidental update']);
    } finally {
      release();
      await Promise.all([first, question, successor]);
    }
  });

  it.each(['before', 'render', 'encode', 'first upload', 'last upload'] as const)(
    'rejects cancellation %s and does not issue later writes', async (boundary) => {
      let current = boundary !== 'before';
      let release!: () => void;
      const held = new Promise<void>((resolve) => { release = resolve; });
      let reached = false;
      const uploads: number[] = [];
      const client = { require: () => ({
        updateImageRawData: async (update: { containerID?: number }) => {
          uploads.push(update.containerID ?? -1);
          if ((boundary === 'first upload' && uploads.length === 1)
            || (boundary === 'last upload' && uploads.length === 4)) {
            reached = true;
            await held;
          }
          return ImageRawDataUpdateResult.success;
        },
      }) } as unknown as BridgeClient;
      const renderer: HudFrameRenderer = { render: async () => {
        if (boundary === 'render') { reached = true; await held; }
        return tiles();
      } };
      const surface = new HudVisualSurface(client, renderer, undefined, async (tile) => {
        if (boundary === 'encode' && !reached) { reached = true; await held; }
        return tile.pixels;
      }, {});
      const painting = exact(surface, frame('Say the message.'), () => current);
      try {
        if (boundary !== 'before') {
          await vi.waitFor(() => expect(reached).toBe(true));
          current = false;
        }
        release();
        expect(await painting).toBe(false);
        expect(uploads).toHaveLength(boundary === 'last upload' ? 4 : boundary === 'first upload' ? 1 : 0);
      } finally {
        current = false;
        release();
        await painting;
      }
    },
  );

  it('reuses accepted identical pixels only while current, and writes changed or forgotten page pixels', async () => {
    const uploaded: number[] = [];
    let changed = false;
    const client = { require: () => ({ updateImageRawData: async (update: { containerID?: number }) => {
      uploaded.push(update.containerID ?? -1);
      return ImageRawDataUpdateResult.success;
    } }) } as unknown as BridgeClient;
    const surface = new HudVisualSurface(client, { render: async () => tiles().map((tile) => ({
      ...tile, pixels: changed && tile.containerId === 4 ? new Uint8Array([99]) : tile.pixels,
    })) });
    await surface.paint(frame('Say the message.'));
    expect(await exact(surface, frame('Say the message.'), () => true)).toBe(true);
    expect(uploaded).toEqual([4, 8, 9, 10]);
    expect(await exact(surface, frame('Say the message.'), () => false)).toBe(false);
    expect(uploaded).toEqual([4, 8, 9, 10]);
    changed = true;
    expect(await exact(surface, frame('Say the message.'), () => true)).toBe(true);
    expect(uploaded).toEqual([4, 8, 9, 10, 4]);
    surface.forgetPage();
    expect(await exact(surface, frame('Say the message.'), () => true)).toBe(true);
    expect(uploaded).toEqual([4, 8, 9, 10, 4, 4, 8, 9, 10]);
  });

  it('never acknowledges a question with a failed tile', async () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    let uploads = 0;
    const client = { require: () => ({ updateImageRawData: async () => {
      uploads += 1;
      return uploads === 2 ? -1 : ImageRawDataUpdateResult.success;
    } }) } as unknown as BridgeClient;
    try {
      const surface = new HudVisualSurface(client, { render: async () => tiles() });
      expect(await exact(surface, frame('Say the message.'), () => true)).toBe(false);
      expect(uploads).toBe(2);
    } finally { error.mockRestore(); }
  });
});

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

  it('releases first light to its fallback when the first tile upload never settles', async () => {
    vi.useFakeTimers();
    const error = vi.spyOn(console, 'error').mockImplementation(() => {});
    let finishTile!: (result: ImageRawDataUpdateResult) => void;
    const pendingTile = new Promise<ImageRawDataUpdateResult>((resolve) => { finishTile = resolve; });
    const updateImageRawData = vi.fn()
      .mockReturnValueOnce(pendingTile)
      .mockResolvedValue(ImageRawDataUpdateResult.success);
    const client = {
      require: () => ({ updateImageRawData }),
    } as unknown as BridgeClient;
    const renderer: HudFrameRenderer = {
      render: async () => [{ containerId: ContainerId.Image, pixels: new Uint8Array([7]) }],
    };
    const frame: HudVisualFrame = {
      kind: 'overview', status: 'JOAT · connecting', micOn: false, mainBrightness: 4,
      main: '', context: '', tray: '', hint: '',
    };
    const surface = new HudVisualSurface(client, renderer, undefined, undefined, {
      uploadTimeoutMs: 50,
    });
    const fallback = { setStatus: vi.fn(async () => true) };
    let selected: string | undefined;
    try {
      const boot = selectHudFirstLight({
        activateSurface: async () => 'visual',
        restoreFallback: async () => 'text',
        restoreMinimal: async () => true,
        createVisual: () => ({ setStatus: () => surface.paint(frame) }),
        createText: () => fallback,
        createMinimal: () => fallback,
      }).then((result) => { selected = result.surface; });
      await vi.advanceTimersByTimeAsync(51);
      expect(selected).toBe('text');
      await boot;
      expect(fallback.setStatus).toHaveBeenCalledWith('JOAT · connecting');

      // A late success cannot retroactively acknowledge the timed-out paint
      // or poison its cache. A new paint re-uploads the tile and completes.
      finishTile(ImageRawDataUpdateResult.success);
      await Promise.resolve();
      expect(await surface.paint(frame)).toBe(true);
      expect(updateImageRawData).toHaveBeenCalledTimes(2);
    } finally {
      finishTile(ImageRawDataUpdateResult.sendFailed);
      error.mockRestore();
      vi.useRealTimers();
    }
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
    let settleFirst!: () => void;
    const client = {
      require: () => ({
        updateImageRawData: () => {
          calls += 1;
          if (calls === 1) {
            return new Promise<ImageRawDataUpdateResult>((resolve) => {
              settleFirst = () => resolve(ImageRawDataUpdateResult.success);
            });
          }
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

    // The lane is free: the next frame is answered, not stranded behind the
    // hung write. A host timeout does not cancel the SDK call, though, so
    // the native single-writer boundary still refuses a new tile until that
    // write settles (a late old upload must not land behind newer pixels).
    expect(await surface.paint(frame)).toBe(false);
    expect(calls).toBe(1);

    // Once it settles, nothing is remembered as delivered: the next frame
    // ships all four tiles again.
    settleFirst();
    expect(await surface.paint(frame)).toBe(true);
    expect(calls).toBe(5);
  });


});
