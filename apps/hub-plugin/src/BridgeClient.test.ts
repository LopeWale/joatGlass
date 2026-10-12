import { describe, expect, it, vi } from 'vitest';
import { ImageRawDataUpdateResult, StartUpPageCreateResult } from '@evenrealities/even_hub_sdk';
import { BRIDGE_CALL_TIMEOUT_MS, BridgeClient } from './BridgeClient';
import { HudPresenter, setNativeOverviewForTest } from './HudPresenter';
import { HudPageBuilder } from './HudPageBuilder';
import { HudVisualSurface } from './HudVisualSurface';

// These tests pin the IMAGE overview (VITE_NATIVE_OVERVIEW=0). The text
// overview, which is Home by default, has its own: HudPresenter.textOverview.test.ts.
setNativeOverviewForTest(false);

vi.mock('@evenrealities/even_hub_sdk', async (importOriginal) => {
  const real = await importOriginal<typeof import('@evenrealities/even_hub_sdk')>();
  return { ...real, waitForEvenAppBridge: () => Promise.resolve(globalThis.__fakeBridge) };
});

declare global {
  // eslint-disable-next-line no-var
  var __fakeBridge: unknown;
}

function bridge(overrides: Record<string, unknown>) {
  globalThis.__fakeBridge = {
    onLaunchSource: () => () => {},
    ...overrides,
  };
}

describe('BridgeClient.start reload resilience', () => {
  it('fresh session: create succeeds, no rebuild attempted', async () => {
    const rebuild = vi.fn();
    bridge({
      createStartUpPageContainer: async () => StartUpPageCreateResult.success,
      rebuildPageContainer: rebuild,
    });
    const c = new BridgeClient();
    await c.init();
    await expect(c.start({} as never, {} as never)).resolves.toBe('created');
    expect(rebuild).not.toHaveBeenCalled();
    expect(() => c.assertStarted()).not.toThrow();
  });

  it('reloaded session: create says invalid, rebuild adopts the existing page', async () => {
    bridge({
      createStartUpPageContainer: async () => StartUpPageCreateResult.invalid,
      rebuildPageContainer: async () => true,
    });
    const c = new BridgeClient();
    await c.init();
    await expect(c.start({} as never, {} as never)).resolves.toBe('recovered');
    expect(() => c.assertStarted()).not.toThrow();
  });

  it('genuinely invalid layout: rebuild fails too, original error surfaces', async () => {
    bridge({
      createStartUpPageContainer: async () => StartUpPageCreateResult.invalid,
      rebuildPageContainer: async () => false,
    });
    const c = new BridgeClient();
    await c.init();
    await expect(c.start({} as never, {} as never)).rejects.toThrow('invalid');
  });

  it('oversize is never masked by the rebuild path', async () => {
    const rebuild = vi.fn(async () => true);
    bridge({
      createStartUpPageContainer: async () => StartUpPageCreateResult.oversize,
      rebuildPageContainer: rebuild,
    });
    const c = new BridgeClient();
    await c.init();
    await expect(c.start({} as never, {} as never)).rejects.toThrow('oversize');
    expect(rebuild).not.toHaveBeenCalled();
  });
});

describe('retired display ownership', () => {
  function suspend(owner: BridgeClient | HudPresenter): void {
    owner.suspendDisplay();
  }
  function resume(owner: BridgeClient | HudPresenter): void {
    owner.resumeDisplay();
  }

  it('blocks cached SDK display methods but leaves microphone cleanup and storage usable', async () => {
    const create = vi.fn(async () => StartUpPageCreateResult.success);
    const rebuild = vi.fn(async () => true);
    const text = vi.fn(async () => true);
    const image = vi.fn(async () => ImageRawDataUpdateResult.success);
    const mic = vi.fn(async () => true);
    const storage = vi.fn(async () => true);
    bridge({
      createStartUpPageContainer: create, rebuildPageContainer: rebuild,
      textContainerUpgrade: text, updateImageRawData: image,
      audioControl: mic, setLocalStorage: storage,
    });
    const client = new BridgeClient();
    const cached = await client.init();
    const cachedText = cached.textContainerUpgrade.bind(cached);
    suspend(client);

    const outcomes = await Promise.all([
      cached.createStartUpPageContainer({} as never),
      cached.rebuildPageContainer({} as never),
      cachedText({} as never),
      cached.updateImageRawData({} as never),
    ]);
    expect([create.mock.calls.length, rebuild.mock.calls.length, text.mock.calls.length, image.mock.calls.length])
      .toEqual([0, 0, 0, 0]);
    expect(outcomes).toEqual([StartUpPageCreateResult.invalid, false, false, ImageRawDataUpdateResult.sendFailed]);
    await cached.audioControl(false);
    await cached.setLocalStorage('synthetic-display-test', 'saved');
    expect(mic).toHaveBeenCalledWith(false);
    expect(storage).toHaveBeenCalledWith('synthetic-display-test', 'saved');

    resume(client);
    expect(await cachedText({} as never)).toBe(true);
    expect(text).toHaveBeenCalledOnce();
  });

  it.each(['render', 'upload', 'native text'] as const)(
    'does not resume stale %s work or self-heal over the new owner after manual recovery', async (stage) => {
      let release!: () => void;
      const held = new Promise<void>((resolve) => { release = resolve; });
      let reached = false;
      const rebuild = vi.fn(async () => true);
      const text = vi.fn(async () => {
        if (stage === 'native text' && !reached) { reached = true; await held; }
        return true;
      });
      const image = vi.fn(async () => {
        if (stage === 'upload' && !reached) { reached = true; await held; }
        return ImageRawDataUpdateResult.success;
      });
      bridge({
        rebuildPageContainer: rebuild, textContainerUpgrade: text, updateImageRawData: image,
      });
      const client = new BridgeClient();
      await client.init();
      const visual = stage === 'native text' ? undefined : new HudVisualSurface(client, {
        render: async () => {
          if (stage === 'render' && !reached) { reached = true; await held; }
          return [4, 8, 9, 10].map((containerId) => ({
            containerId, pixels: new Uint8Array([containerId]),
          }));
        },
      });
      const hud = new HudPresenter(client, new HudPageBuilder(), visual);
      const pending = hud.showText('Synthetic old owner');
      try {
        await vi.waitFor(() => expect(reached).toBe(true));
        suspend(client);
        suspend(hud);
        resume(client);
        resume(hud);
        release();

        expect(await pending).toBe(false);
        expect(rebuild).not.toHaveBeenCalled();
        expect(image).toHaveBeenCalledTimes(stage === 'upload' ? 1 : 0);
        expect(text).toHaveBeenCalledTimes(stage === 'native text' ? 1 : 0);

        expect(await hud.showText('Synthetic recovered owner')).toBe(true);
        expect(image).toHaveBeenCalledTimes(stage === 'native text' ? 0 : stage === 'upload' ? 5 : 4);
        expect(text).toHaveBeenCalledTimes(stage === 'native text' ? 2 : 0);
      } finally {
        release();
        await pending;
        suspend(hud);
      }
    },
  );
});

describe('bridge calls that never answer', () => {
  it('fails a rebuild or a text update that has not answered in time, and distrusts the page', async () => {
    vi.useFakeTimers();
    try {
      bridge({
        rebuildPageContainer: () => new Promise<boolean>(() => {}),
        textContainerUpgrade: () => new Promise<boolean>(() => {}),
      });
      const c = new BridgeClient();
      await c.init();
      const before = c.pageEpoch();
      const rebuild = c.require().rebuildPageContainer({} as never);
      const text = c.require().textContainerUpgrade({} as never);
      await vi.advanceTimersByTimeAsync(BRIDGE_CALL_TIMEOUT_MS);
      await expect(rebuild).resolves.toBe(false);
      await expect(text).resolves.toBe(false);
      // Either may still land late: no in-place update may trust the page.
      expect(c.pageEpoch()).toBeGreaterThan(before + 1);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('BridgeClient.storage', () => {
  it('writes a value the host does not hold yet, and never the same value twice', async () => {
    const set = vi.fn(async () => true);
    bridge({ getLocalStorage: async () => 'wss://mac-a/glasses', setLocalStorage: set });
    const c = new BridgeClient();
    await c.init();
    expect(await c.storage.get('joat.brain')).toBe('wss://mac-a/glasses');
    // Every reconnect wrote the preferred Mac again; the host holds it.
    expect(await c.storage.set('joat.brain', 'wss://mac-a/glasses')).toBe(true);
    expect(set).not.toHaveBeenCalled();
    expect(await c.storage.set('joat.brain', 'wss://mac-b/glasses')).toBe(true);
    expect(await c.storage.set('joat.brain', 'wss://mac-b/glasses')).toBe(true);
    expect(set).toHaveBeenCalledTimes(1);
  });

  it('writes one key after another, and writes a failed value again next time', async () => {
    const order: string[] = [];
    let release: (() => void) | undefined;
    let fail = true;
    bridge({
      setLocalStorage: (key: string) => new Promise<boolean>((resolve) => {
        order.push(`start ${key}`);
        const finish = () => { order.push(`end ${key}`); resolve(!(fail && key === 'b')); };
        if (key === 'a') release = finish; else finish();
      }),
    });
    const c = new BridgeClient();
    await c.init();
    const a = c.storage.set('a', '1');
    const b = c.storage.set('b', '2');
    await Promise.resolve();
    expect(order).toEqual(['start a']);
    release?.();
    expect(await a).toBe(true);
    expect(await b).toBe(false);
    expect(order).toEqual(['start a', 'end a', 'start b', 'end b']);
    fail = false;
    expect(await c.storage.set('b', '2')).toBe(true);
    expect(order).toHaveLength(6);
  });
});
