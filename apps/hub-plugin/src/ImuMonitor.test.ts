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
  const imu = new ImuMonitor(client, link);
  return { imu, sent };
}

describe('ImuMonitor confirms', () => {
  it('traces prompt visibility and verdict without logging the prompt', () => {
    const { imu } = make();
    const info = vi.spyOn(console, 'info').mockImplementation(() => {});

    imu.prepare('message-reply:hud-12:a1');
    expect(imu.activate('message-reply:hud-12:a1', 1000)).toBe(true);
    imu.confirmByTap();

    expect(info.mock.calls.map(([line]) => line)).toEqual([
      '[joat trace] trace=message-reply:hud-12:a1 stage=confirmation-received',
      '[joat trace] trace=message-reply:hud-12:a1 stage=confirmation-visible',
      '[joat trace] trace=message-reply:hud-12:a1 stage=confirmation-settled accepted=true',
    ]);
    info.mockRestore();
  });

  it('motion never approves a decision that advertises TAP as its only accept gesture', () => {
    const { imu, sent } = make();
    imu.await('act-1');
    imu.ingest(sample(0.0));
    imu.ingest(sample(1.0));
    expect(sent).toEqual([]);
    expect(imu.isPending()).toBe(true);
    imu.dispose();
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

  it('explicit decline resolves false', () => {
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

  it('revoking a generation cancels its still-visible confirmation gate', () => {
    const { imu, sent } = make();
    const controller = new AbortController();
    const settled = vi.fn();
    imu.prepare('old-generation', settled, controller.signal);
    expect(imu.activate('old-generation', 8000)).toBe(true);

    controller.abort();

    expect(imu.isPending()).toBe(false);
    expect(settled).toHaveBeenCalledOnce();
    expect(settled).toHaveBeenCalledWith(false);
    expect(sent).toEqual([]);
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

  it('tracing does not turn observed motion into an approval', () => {
    const { imu, sent } = make();
    imu.setTrace(true);
    imu.await('act-2');
    imu.ingest(sample(0.0));
    imu.ingest(sample(0.4));
    expect(sent.filter((m) => m.kind === 'imu')).toHaveLength(2);
    expect(sent.filter((m) => m.kind === 'confirm')).toEqual([]);
    expect(imu.isPending()).toBe(true);
    imu.dispose();
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
  it('a new stop invalidates and compensates an older pending IMU start', async () => {
    let releaseStart!: (started: boolean) => void;
    const pendingStart = new Promise<boolean>((resolve) => { releaseStart = resolve; });
    const controls: boolean[] = [];
    const client = {
      assertStarted: () => ({
        imuControl: async (open: boolean) => {
          controls.push(open);
          if (open) return pendingStart;
          return true;
        },
      }),
      require: () => ({
        imuControl: async (open: boolean) => {
          controls.push(open);
          return true;
        },
      }),
    } as unknown as BridgeClient;
    const imu = new ImuMonitor(client, { send: () => undefined } as unknown as MacLink);
    const oldGeneration = new AbortController();

    const starting = imu.start(undefined, oldGeneration.signal);
    oldGeneration.abort();
    await imu.stop();
    releaseStart(true);

    await expect(starting).resolves.toBe(false);
    expect(controls).toEqual([true, false, false]);
  });

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

  it('reports a timeout outcome separately from an explicit cancellation', () => {
    vi.useFakeTimers();
    const { imu } = make();
    const outcomes = vi.fn();

    imu.prepare('message-timeout', undefined, undefined, outcomes);
    expect(imu.activate('message-timeout', 1000)).toBe(true);
    vi.advanceTimersByTime(1000);

    expect(outcomes).toHaveBeenCalledWith('timed_out');
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
