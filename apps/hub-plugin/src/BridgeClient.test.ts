import { describe, expect, it, vi } from 'vitest';
import { StartUpPageCreateResult } from '@evenrealities/even_hub_sdk';
import { BridgeClient } from './BridgeClient';

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
