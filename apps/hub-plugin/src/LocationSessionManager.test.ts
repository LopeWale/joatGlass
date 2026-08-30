import { AppLocationAccuracy, type AppLocation } from '@evenrealities/even_hub_sdk';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { LocationSessionManager } from './LocationSessionManager';

function harness(first: AppLocation | null = null) {
  let onLocation: ((location: AppLocation) => void) | undefined;
  const order: string[] = [];
  const bridge = {
    getAppLocation: vi.fn(async () => first),
    startAppLocationUpdates: vi.fn(async () => {
      order.push('start');
      return true;
    }),
    stopAppLocationUpdates: vi.fn(async () => true),
    onAppLocationChanged: vi.fn((callback: (location: AppLocation) => void) => {
      order.push('listen');
      onLocation = callback;
      return vi.fn();
    }),
  };
  const link = { send: vi.fn() };
  const diagnostics = { record: vi.fn() };
  const manager = new LocationSessionManager(
    { require: () => bridge } as never,
    link as never,
    diagnostics,
  );
  return {
    manager,
    bridge,
    link,
    diagnostics,
    order,
    emit: (location: AppLocation) => onLocation?.(location),
  };
}

afterEach(() => {
  vi.useRealTimers();
});

describe('LocationSessionManager', () => {
  it('owns one ambient host stream and preserves the coarse Mac uplink', async () => {
    const h = harness({ latitude: 35.2, longitude: -97.4, accuracy: 12 });

    expect(await h.manager.start()).toBe(true);

    expect(h.order).toEqual(['listen', 'start']);
    expect(h.bridge.getAppLocation).toHaveBeenCalledWith({
      accuracy: AppLocationAccuracy.Medium,
      timeoutMs: 5000,
    });
    expect(h.bridge.startAppLocationUpdates).toHaveBeenCalledWith({
      accuracy: AppLocationAccuracy.Medium,
      distanceFilter: 250,
      intervalMs: 60_000,
    });
    expect(h.link.send).toHaveBeenCalledWith({
      kind: 'location', latitude: 35.2, longitude: -97.4, accuracy: 12,
    });
  });

  it('switches the one host stream to probe accuracy and back without adding listeners', async () => {
    const h = harness();
    await h.manager.start();

    expect(await h.manager.setProbeEnabled(true)).toBe(true);
    expect(await h.manager.setProbeEnabled(false)).toBe(true);

    expect(h.bridge.onAppLocationChanged).toHaveBeenCalledTimes(1);
    expect(h.bridge.stopAppLocationUpdates).toHaveBeenCalledTimes(2);
    expect(h.bridge.startAppLocationUpdates.mock.calls).toEqual([
      [{ accuracy: AppLocationAccuracy.Medium, distanceFilter: 250, intervalMs: 60_000 }],
      [{ accuracy: AppLocationAccuracy.High, distanceFilter: 1, intervalMs: 1000 }],
      [{ accuracy: AppLocationAccuracy.Medium, distanceFilter: 250, intervalMs: 60_000 }],
    ]);
  });

  it('revocation stops a pending high-rate start and compensates its late success', async () => {
    const h = harness();
    await h.manager.start();
    let releaseProbe!: (started: boolean) => void;
    const pendingProbe = new Promise<boolean>((resolve) => { releaseProbe = resolve; });
    h.bridge.startAppLocationUpdates.mockImplementationOnce(() => pendingProbe);
    const generation = new AbortController();

    const probing = h.manager.setProbeEnabled(true, generation.signal);
    await vi.waitFor(() => {
      expect(h.bridge.startAppLocationUpdates).toHaveBeenCalledTimes(2);
    });
    generation.abort();
    await vi.waitFor(() => {
      expect(h.bridge.stopAppLocationUpdates.mock.calls.length).toBeGreaterThanOrEqual(2);
    });

    releaseProbe(true);
    await expect(probing).resolves.toBe(false);
    await vi.waitFor(() => {
      expect(h.bridge.startAppLocationUpdates).toHaveBeenLastCalledWith({
        accuracy: AppLocationAccuracy.Medium,
        distanceFilter: 250,
        intervalMs: 60_000,
      });
    });
    expect(h.bridge.stopAppLocationUpdates.mock.calls.length).toBeGreaterThanOrEqual(3);
  });

  it('runs a distinct high-accuracy navigation profile and keeps rapid fixes local', async () => {
    const h = harness();
    const fixes: Array<{ location: AppLocation; receivedAt: number }> = [];
    await h.manager.start();

    expect(await h.manager.setNavigationConsumer((location, receivedAt) => {
      fixes.push({ location, receivedAt });
    })).toBe(true);
    h.emit({
      latitude: 41.9,
      longitude: -87.65,
      accuracy: 6,
      heading: 90,
      speed: 1.4,
      timestamp: 123,
    });
    h.emit({ latitude: 41.90001, longitude: -87.64999, accuracy: 7 });

    expect(h.bridge.startAppLocationUpdates.mock.calls[
      h.bridge.startAppLocationUpdates.mock.calls.length - 1
    ]).toEqual([{
      accuracy: AppLocationAccuracy.High,
      distanceFilter: 1,
      intervalMs: 1000,
    }]);
    expect(fixes).toHaveLength(2);
    expect(fixes[0]?.location).toMatchObject({ heading: 90, speed: 1.4, timestamp: 123 });
    expect(h.link.send.mock.calls.filter(([message]) => message.kind === 'location')).toHaveLength(1);
    expect(h.diagnostics.record.mock.calls.flat()).not.toContainEqual(
      expect.objectContaining({ type: 'location_probe' }),
    );
    expect(h.diagnostics.record).toHaveBeenCalledWith({
      type: 'navigation_location',
      sampleSequence: 1,
      lifecycle: 'foreground',
      fields: 7,
      cadence: 'first',
      accuracy: 'lte10',
      timestampAge: 'stale',
      displacement: 'first',
      headingDelta: 'unavailable',
    });
  });

  it('re-arms the active high-accuracy navigation stream after foreground return', async () => {
    const h = harness();
    await h.manager.start();
    await h.manager.setNavigationConsumer(() => undefined);

    h.manager.setForeground(false);
    await h.manager.setForeground(true);

    expect(h.bridge.onAppLocationChanged).toHaveBeenCalledTimes(1);
    expect(h.bridge.stopAppLocationUpdates).toHaveBeenCalledTimes(2);
    expect(h.bridge.startAppLocationUpdates.mock.calls).toEqual([
      [{ accuracy: AppLocationAccuracy.Medium, distanceFilter: 250, intervalMs: 60_000 }],
      [{ accuracy: AppLocationAccuracy.High, distanceFilter: 1, intervalMs: 1000 }],
      [{ accuracy: AppLocationAccuracy.High, distanceFilter: 1, intervalMs: 1000 }],
    ]);
  });

  it('does not let a diagnostic probe preempt an active navigation session', async () => {
    const h = harness();
    await h.manager.start();
    await h.manager.setNavigationConsumer(() => undefined);

    expect(await h.manager.setProbeEnabled(true)).toBe(false);
    expect(h.bridge.startAppLocationUpdates).toHaveBeenCalledTimes(2);
  });

  it('requests a fresh high-accuracy route origin without restarting the stream', async () => {
    const origin = { latitude: 41.9, longitude: -87.65, accuracy: 6 };
    const h = harness(origin);
    await h.manager.start();
    h.bridge.getAppLocation.mockClear();

    await expect(h.manager.requestNavigationOrigin()).resolves.toEqual(origin);
    expect(h.bridge.getAppLocation).toHaveBeenCalledWith({
      accuracy: AppLocationAccuracy.High,
      timeoutMs: 5000,
    });
    expect(h.bridge.startAppLocationUpdates).toHaveBeenCalledTimes(1);
  });

  it('accepts a phone fix that settles at the SDK deadline before the local watchdog', async () => {
    vi.useFakeTimers();
    const h = harness();
    await h.manager.start();
    h.bridge.getAppLocation.mockClear();
    const origin = { latitude: 41.9, longitude: -87.65, accuracy: 6 };
    h.bridge.getAppLocation.mockImplementationOnce(
      () => new Promise<AppLocation | null>((resolve) => {
        setTimeout(() => resolve(origin), 5_000);
      }),
    );

    let result: AppLocation | null | undefined;
    void h.manager.requestNavigationOrigin().then((value) => { result = value; });
    await vi.advanceTimersByTimeAsync(5_000);

    expect(result).toEqual(origin);
    expect(h.bridge.getAppLocation).toHaveBeenCalledWith({
      accuracy: AppLocationAccuracy.High,
      timeoutMs: 5_000,
    });
  });

  it('answers an assistant refresh with a timestamped request-bound phone fix', async () => {
    const current = {
      latitude: 41.9,
      longitude: -87.65,
      accuracy: 6,
      timestamp: 1_725_000_000_123,
    };
    const h = harness(current);
    await h.manager.start();
    h.link.send.mockClear();
    h.bridge.getAppLocation.mockClear();

    await expect(h.manager.requestAssistantLocation('location-refresh-1')).resolves.toBe(true);

    expect(h.bridge.getAppLocation).toHaveBeenCalledWith({
      accuracy: AppLocationAccuracy.Medium,
      timeoutMs: 5000,
    });
    expect(h.link.send).toHaveBeenCalledWith({
      kind: 'location',
      latitude: 41.9,
      longitude: -87.65,
      accuracy: 6,
      timestamp: 1_725_000_000_123,
      refreshId: 'location-refresh-1',
    });
  });

  it('emits only bounded derived probe telemetry, never coordinates or raw motion values', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-28T12:00:00.000Z'));
    const h = harness();
    await h.manager.start();
    await h.manager.setProbeEnabled(true);
    h.manager.setForeground(false);

    h.emit({
      latitude: 0,
      longitude: 0,
      accuracy: 4,
      speed: 1.4,
      heading: 90,
      timestamp: Date.now() - 200,
    });
    vi.setSystemTime(new Date('2026-08-28T12:00:01.000Z'));
    h.emit({
      latitude: 0,
      longitude: 0.0001,
      accuracy: 7,
      speed: 1.6,
      heading: 88,
      timestamp: Date.now() - 300,
    });

    const samples = h.diagnostics.record.mock.calls
      .map(([event]) => event)
      .filter((event) => event.type === 'location_probe');
    expect(samples[0]).toEqual({
      type: 'location_probe',
      sampleSequence: 1,
      profile: 'high',
      lifecycle: 'background',
      fields: 7,
      cadence: 'first',
      accuracy: 'lte5',
      timestampAge: 'fresh',
      displacement: 'first',
      headingDelta: 'unavailable',
    });
    expect(samples[1]).toEqual({
      type: 'location_probe',
      sampleSequence: 2,
      profile: 'high',
      lifecycle: 'background',
      fields: 7,
      cadence: 'lte1s',
      accuracy: 'lte10',
      timestampAge: 'fresh',
      displacement: '5_20',
      headingDelta: '0_30',
    });
    const serialized = JSON.stringify(h.diagnostics.record.mock.calls);
    expect(serialized).not.toContain('latitude');
    expect(serialized).not.toContain('longitude');
    expect(serialized).not.toContain('heading":');
    expect(serialized).not.toContain('speed":');
    expect(serialized).not.toContain('timestamp":');
  });

  it('records whether the requested high-accuracy profile actually started', async () => {
    const h = harness();
    await h.manager.setProbeEnabled(true);
    h.bridge.startAppLocationUpdates.mockResolvedValueOnce(false);

    expect(await h.manager.start()).toBe(false);
    expect(h.diagnostics.record).toHaveBeenCalledWith({
      type: 'location_probe_state', requested: 'high', outcome: 'failed',
    });
  });

  it('still starts continuous updates when the one-shot fix is unavailable', async () => {
    const h = harness();
    h.bridge.getAppLocation.mockRejectedValueOnce(new Error('no one-shot fix'));

    expect(await h.manager.start()).toBe(true);
    expect(h.bridge.startAppLocationUpdates).toHaveBeenCalledTimes(1);
  });

  it('does not let a host that ignores the one-shot timeout block Gate 0', async () => {
    vi.useFakeTimers();
    const h = harness();
    h.bridge.getAppLocation.mockImplementationOnce(() => new Promise<AppLocation | null>(() => {}));
    await h.manager.setProbeEnabled(true);

    const started = h.manager.start();
    await vi.advanceTimersByTimeAsync(6_000);

    await expect(started).resolves.toBe(true);
    expect(h.bridge.startAppLocationUpdates).toHaveBeenLastCalledWith({
      accuracy: AppLocationAccuracy.High,
      distanceFilter: 1,
      intervalMs: 1000,
    });
    const profileStates = h.diagnostics.record.mock.calls
      .map(([event]) => event)
      .filter((event) => event.type === 'location_probe_state');
    expect(profileStates).toEqual([
      { type: 'location_probe_state', requested: 'high', outcome: 'success' },
    ]);
  });

  it('bounds an unresponsive high-accuracy stream start as a failed Gate 0 capability', async () => {
    vi.useFakeTimers();
    const h = harness();
    h.bridge.startAppLocationUpdates.mockImplementationOnce(
      () => new Promise<boolean>(() => {}),
    );
    await h.manager.setProbeEnabled(true);

    const started = h.manager.start();
    await vi.advanceTimersByTimeAsync(5_000);

    await expect(started).resolves.toBe(false);
    expect(h.diagnostics.record).toHaveBeenCalledWith({
      type: 'location_probe_state', requested: 'high', outcome: 'failed',
    });
    expect(h.bridge.stopAppLocationUpdates).toHaveBeenCalled();
  });

  it('degrades to unavailable when an older host cannot register the location listener', async () => {
    const h = harness();
    h.bridge.onAppLocationChanged.mockImplementationOnce(() => {
      throw new Error('listener unavailable');
    });

    await expect(h.manager.start()).resolves.toBe(false);
    expect(h.bridge.startAppLocationUpdates).not.toHaveBeenCalled();
  });

  it('stops the host stream and listener during shutdown', async () => {
    const h = harness();
    await h.manager.start();
    const unsubscribe = h.bridge.onAppLocationChanged.mock.results[0]?.value as ReturnType<typeof vi.fn>;

    await h.manager.shutdown();

    expect(unsubscribe).toHaveBeenCalledTimes(1);
    expect(h.bridge.stopAppLocationUpdates).toHaveBeenCalledTimes(1);
  });

  it('keeps duplicate probe controls idempotent without restarting sample order', async () => {
    const h = harness();
    await h.manager.start();
    await h.manager.setProbeEnabled(true);
    h.emit({ latitude: 1, longitude: 1 });

    await h.manager.setProbeEnabled(true);
    h.emit({ latitude: 1, longitude: 1.0001 });

    expect(h.bridge.stopAppLocationUpdates).toHaveBeenCalledTimes(1);
    expect(h.bridge.startAppLocationUpdates).toHaveBeenCalledTimes(2);
    const samples = h.diagnostics.record.mock.calls
      .map(([event]) => event)
      .filter((event) => event.type === 'location_probe');
    expect(samples[1]).toMatchObject({ sampleSequence: 2 });
  });

  it('immediately retries stop when a failed profile switch leaves stream state uncertain', async () => {
    const h = harness();
    await h.manager.start();
    h.bridge.stopAppLocationUpdates.mockResolvedValueOnce(false);
    h.bridge.startAppLocationUpdates.mockResolvedValueOnce(false);

    expect(await h.manager.setProbeEnabled(true)).toBe(false);
    expect(h.bridge.stopAppLocationUpdates).toHaveBeenCalledTimes(2);
    await h.manager.shutdown();

    expect(h.bridge.stopAppLocationUpdates).toHaveBeenCalledTimes(2);
  });
});
