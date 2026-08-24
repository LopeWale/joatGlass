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
    imu.confirmByRing();
    expect(cb).toHaveBeenCalledWith(true);
    expect(sent).toEqual([]);
  });

  it('expired window resolves false on next sample', () => {
    const { imu, sent } = make();
    imu.await('late', -1); // already expired
    imu.ingest(sample(0.0));
    expect(sent).toEqual([{ kind: 'confirm', actionId: 'late', accepted: false }]);
  });

  it('ring tap with nothing pending is a no-op', () => {
    const { imu, sent } = make();
    imu.confirmByRing();
    expect(sent).toEqual([]);
  });

  it('pending is cleared before the callback runs, so a callback may re-arm', () => {
    const { imu, sent } = make();
    const second = vi.fn();
    imu.await('first', 8000, () => imu.await('second', 8000, second));
    imu.confirmByRing(); // resolves first → callback arms second
    imu.confirmByRing(); // must hit the re-armed window, not a stale one
    expect(second).toHaveBeenCalledWith(true);
    expect(sent).toEqual([]);
  });
});
