import { describe, expect, it, vi } from 'vitest';
import type { IMU_Report_Data } from '@evenrealities/even_hub_sdk';
import { ImuMonitor } from './ImuMonitor';
import type { BridgeClient } from './BridgeClient';
import type { MacLink } from './MacLink';
import type { UplinkMessage } from './types';

/** The monitor only reads .y; the SDK class carries serialization baggage. */
const sample = (y: number): IMU_Report_Data => ({ y }) as IMU_Report_Data;

function make() {
  const sent: UplinkMessage[] = [];
  const link = { send: (m: UplinkMessage) => sent.push(m) } as unknown as MacLink;
  const client = {} as BridgeClient; // start()/stop() untouched in these tests
  const imu = new ImuMonitor(client, link, 0.35, 1200);
  return { imu, sent };
}

describe('ImuMonitor confirms', () => {
  it('nod swing past threshold resolves a Mac confirm uplink', () => {
    const { imu, sent } = make();
    imu.await('act-1');
    imu.ingest(sample(0.0));
    imu.ingest(sample(0.4)); // swing 0.4 >= 0.35
    expect(sent).toEqual([{ kind: 'confirm', actionId: 'act-1', accepted: true }]);
  });

  it('local onResult gets the verdict and NOTHING goes uplink', () => {
    const { imu, sent } = make();
    const cb = vi.fn();
    imu.await('local:resume', 8000, cb);
    imu.confirmByTap();
    expect(cb).toHaveBeenCalledWith(true);
    expect(sent).toEqual([]);
  });

  it('expired window resolves false on next sample', () => {
    const { imu, sent } = make();
    imu.await('late', -1); // already expired
    imu.ingest(sample(0.0));
    expect(sent).toEqual([{ kind: 'confirm', actionId: 'late', accepted: false }]);
  });

  it('tap with nothing pending is a no-op', () => {
    const { imu, sent } = make();
    imu.confirmByTap();
    expect(sent).toEqual([]);
  });

  it('left-temple decline resolves false', () => {
    const { imu, sent } = make();
    imu.await('cancel-me');
    expect(imu.isPending()).toBe(true);
    expect(imu.declineByTap()).toBe(true);
    expect(imu.isPending()).toBe(false);
    expect(sent).toEqual([{ kind: 'confirm', actionId: 'cancel-me', accepted: false }]);
  });

  it('pending is cleared before the callback runs, so a callback may re-arm', () => {
    const { imu, sent } = make();
    const second = vi.fn();
    imu.await('first', 8000, () => imu.await('second', 8000, second));
    imu.confirmByTap(); // resolves first → callback arms second
    imu.confirmByTap(); // must hit the re-armed window, not a stale one
    expect(second).toHaveBeenCalledWith(true);
    expect(sent).toEqual([]);
  });

  it('a stale render failure cannot cancel the replacement action', () => {
    const { imu, sent } = make();
    imu.await('action-a');
    imu.await('action-b');

    expect(imu.cancel('action-a')).toBe(false);
    expect(imu.isPending()).toBe(true);
    expect(imu.confirmByTap()).toBe(true);
    expect(sent).toEqual([
      { kind: 'confirm', actionId: 'action-a', accepted: false },
      { kind: 'confirm', actionId: 'action-b', accepted: true },
    ]);
  });

  it('prepared gate consumes accepts but cannot approve until the prompt is visible', () => {
    const { imu, sent } = make();
    imu.prepare('rendering');

    expect(imu.isPending()).toBe(true);
    expect(imu.confirmByTap()).toBe(true);
    expect(sent).toEqual([]);

    expect(imu.activate('rendering', 1000)).toBe(true);
    expect(imu.confirmByTap()).toBe(true);
    expect(sent).toEqual([
      { kind: 'confirm', actionId: 'rendering', accepted: true },
    ]);
  });
});

describe('ImuMonitor trace mode', () => {
  it('is off by default — no debug traffic on the wire', () => {
    const { imu, sent } = make();
    imu.ingest(sample(0.1));
    expect(sent).toEqual([]);
  });

  it('relays raw samples when enabled, even with no confirm pending', () => {
    const { imu, sent } = make();
    imu.setTrace(true);
    imu.ingest({ x: 1, y: 2, z: 3 } as IMU_Report_Data);
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({ kind: 'imu', x: 1, y: 2, z: 3 });
  });

  it('missing axes default to 0 rather than emitting undefined', () => {
    const { imu, sent } = make();
    imu.setTrace(true);
    imu.ingest(sample(0.5));
    expect(sent[0]).toMatchObject({ kind: 'imu', x: 0, y: 0.5, z: 0 });
  });

  it('tracing does not suppress the confirm it is observing', () => {
    const { imu, sent } = make();
    imu.setTrace(true);
    imu.await('act-2');
    imu.ingest(sample(0.0));
    imu.ingest(sample(0.4));
    expect(sent.filter((m) => m.kind === 'imu')).toHaveLength(2);
    expect(sent.filter((m) => m.kind === 'confirm')).toEqual([
      { kind: 'confirm', actionId: 'act-2', accepted: true },
    ]);
  });

  it('setTrace(false) stops the stream', () => {
    const { imu, sent } = make();
    imu.setTrace(true);
    imu.ingest(sample(0.1));
    imu.setTrace(false);
    imu.ingest(sample(0.2));
    expect(sent).toHaveLength(1);
  });
});

describe('ImuMonitor host degradation', () => {
  it('start() returns false instead of throwing when the host has no IMU', async () => {
    const sent: UplinkMessage[] = [];
    const client = {
      assertStarted: () => ({
        imuControl: () => Promise.reject(new Error('unknown variant `imuControl`')),
      }),
    } as unknown as BridgeClient;
    const imu = new ImuMonitor(client, { send: (m: UplinkMessage) => sent.push(m) } as unknown as MacLink);
    await expect(imu.start()).resolves.toBe(false);
  });

  it('stop() never throws even when the host rejects the off-call', async () => {
    const client = {
      assertStarted: () => ({ imuControl: () => Promise.resolve(true) }),
      require: () => ({ imuControl: () => Promise.reject(new Error('unknown variant')) }),
    } as unknown as BridgeClient;
    const imu = new ImuMonitor(client, { send: () => {} } as unknown as MacLink);
    await imu.start();
    await expect(imu.stop()).resolves.toBeUndefined();
  });
});

describe('confirm window expiry at the tap path', () => {
  /**
   * Expiry only lived in ingest(), which is IMU-sample-driven — on a host
   * where imuControl fails, no sample ever arrives, the window never
   * expired, and a much-later unrelated tap APPROVED an action the Mac had
   * long treated as declined (audit finding, verified in code).
   */
  it('a tap after the window expires resolves REFUSED, not accepted', () => {
    vi.useFakeTimers();
    const sent: Array<{ kind: string; accepted?: boolean }> = [];
    const link = { send: (m: { kind: string }) => sent.push(m) } as never;
    const imu = new ImuMonitor({} as never, link);
    imu.await('a1', 1000);
    vi.advanceTimersByTime(5000); // window long dead, no IMU samples flowed
    imu.confirmByTap();
    expect(sent).toEqual([{ kind: 'confirm', actionId: 'a1', accepted: false }]);
    vi.useRealTimers();
  });

  it('expires on its own without requiring an IMU sample or later tap', () => {
    vi.useFakeTimers();
    const sent: Array<{ kind: string; accepted?: boolean }> = [];
    const link = { send: (m: { kind: string }) => sent.push(m) } as never;
    const imu = new ImuMonitor({} as never, link);
    imu.await('a-timeout', 1000);
    vi.advanceTimersByTime(1000);
    expect(sent).toEqual([
      { kind: 'confirm', actionId: 'a-timeout', accepted: false },
    ]);
    imu.confirmByTap();
    expect(sent).toHaveLength(1);
    vi.useRealTimers();
  });

  it('dispose clears expiry without writing to a closed link', () => {
    vi.useFakeTimers();
    const sent: Array<{ kind: string }> = [];
    const link = { send: (m: { kind: string }) => sent.push(m) } as never;
    const imu = new ImuMonitor({} as never, link);
    imu.await('teardown', 1000);

    imu.dispose();
    vi.advanceTimersByTime(1000);

    expect(imu.isPending()).toBe(false);
    expect(sent).toEqual([]);
    vi.useRealTimers();
  });

  it('a tap inside the window still accepts', () => {
    vi.useFakeTimers();
    const sent: Array<{ kind: string; accepted?: boolean }> = [];
    const link = { send: (m: { kind: string }) => sent.push(m) } as never;
    const imu = new ImuMonitor({} as never, link);
    imu.await('a2', 8000);
    vi.advanceTimersByTime(2000);
    imu.confirmByTap();
    expect(sent).toEqual([{ kind: 'confirm', actionId: 'a2', accepted: true }]);
    vi.useRealTimers();
  });
});
