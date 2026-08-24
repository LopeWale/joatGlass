import { beforeEach, describe, expect, it, vi } from 'vitest';
import { DeviceMonitor } from './DeviceMonitor';
import { Settings } from './Settings';
import type { BridgeClient } from './BridgeClient';
import type { MacLink } from './MacLink';
import type { AudioStreamer } from './AudioStreamer';
import type { ImuMonitor } from './ImuMonitor';
import type { HudPresenter } from './HudPresenter';
import type { UplinkMessage } from './types';

type StatusCb = (status: {
  batteryLevel?: number;
  isWearing?: boolean;
  isCharging?: boolean;
  connectType: string;
}) => void;

function make(policy: 'auto' | 'mac' | 'confirm' = 'confirm', micOpen = false) {
  let statusCb: StatusCb = () => {};
  const sent: UplinkMessage[] = [];
  const audio = {
    isOpen: micOpen,
    start: vi.fn(async (_source?: unknown) => true),
    stop: vi.fn(async () => {}),
  };
  const imu = {
    await: vi.fn((_actionId: string, _timeoutMs?: number, _onResult?: (a: boolean) => void) => {}),
  };
  const hud = {
    setStatus: vi.fn(async (_text: string) => true),
    showText: vi.fn(async (_text: string, _containerId?: number) => true),
  };
  const client = {
    track: (u: () => void) => u,
    require: () => ({
      onDeviceStatusChanged: (cb: StatusCb) => {
        statusCb = cb;
        return () => {};
      },
    }),
  };
  const settings = new Settings();
  settings.setResumePolicy(policy);

  new DeviceMonitor(
    client as unknown as BridgeClient,
    { send: (m: UplinkMessage) => sent.push(m) } as unknown as MacLink,
    audio as unknown as AudioStreamer,
    imu as unknown as ImuMonitor,
    hud as unknown as HudPresenter,
    settings,
  ).attach();

  return { push: (s: Parameters<StatusCb>[0]) => statusCb(s), sent, audio, imu, hud };
}

beforeEach(() => {
  delete (globalThis as { localStorage?: unknown }).localStorage;
});

describe('DeviceMonitor relay', () => {
  it('relays every snapshot to the Mac', () => {
    const { push, sent } = make();
    push({ batteryLevel: 80, isWearing: true, isCharging: false, connectType: 'connected' });
    expect(sent[0]).toEqual({
      kind: 'device',
      battery: 80,
      wearing: true,
      charging: false,
      connect: 'connected',
    });
  });
});

describe('DeviceMonitor doff', () => {
  it('stops the mic on the FIRST event when the glasses are on the desk', () => {
    const { push, audio } = make('confirm', true);
    push({ isWearing: false, connectType: 'connected' });
    expect(audio.stop).toHaveBeenCalledTimes(1);
  });

  it('stops the mic on a wearing true→false transition, once, not on repeats', () => {
    const { push, audio } = make('confirm', true);
    push({ isWearing: true, connectType: 'connected' });
    push({ isWearing: false, connectType: 'connected' });
    push({ isWearing: false, connectType: 'connected' });
    expect(audio.stop).toHaveBeenCalledTimes(1);
  });

  it('ignores hosts that omit isWearing entirely', () => {
    const { push, audio } = make('confirm', true);
    push({ connectType: 'connected' });
    expect(audio.stop).not.toHaveBeenCalled();
  });
});

describe('DeviceMonitor don / resume policy', () => {
  it('does NOTHING when the mic is already open (boot don event — regression)', () => {
    const { push, audio, imu, hud } = make('confirm', true);
    push({ isWearing: true, connectType: 'connected' });
    expect(imu.await).not.toHaveBeenCalled();
    expect(audio.start).not.toHaveBeenCalled();
    expect(hud.showText).not.toHaveBeenCalled();
  });

  it('auto: starts the mic immediately', () => {
    const { push, audio } = make('auto');
    push({ isWearing: true, connectType: 'connected' });
    expect(audio.start).toHaveBeenCalledTimes(1);
  });

  it('mac: leaves the mic off — the Mac decides via audio.control', () => {
    const { push, audio, imu } = make('mac');
    push({ isWearing: true, connectType: 'connected' });
    expect(audio.start).not.toHaveBeenCalled();
    expect(imu.await).not.toHaveBeenCalled();
  });

  it('confirm: arms a local window; accept starts the mic, decline leaves it off', () => {
    const { push, audio, imu } = make('confirm');
    push({ isWearing: true, connectType: 'connected' });
    expect(imu.await).toHaveBeenCalledTimes(1);
    const onResult = imu.await.mock.calls[0]?.[2];
    expect(onResult).toBeDefined();

    onResult?.(false);
    expect(audio.start).not.toHaveBeenCalled();
    onResult?.(true);
    expect(audio.start).toHaveBeenCalledTimes(1);
  });
});

describe('DeviceMonitor battery', () => {
  it('warns once per dip below threshold and re-arms after recovery', () => {
    const { push, hud } = make('mac');
    const warns = () =>
      hud.setStatus.mock.calls.filter(([s]: [string]) => s.includes('battery')).length;

    push({ batteryLevel: 10, isCharging: false, connectType: 'connected' });
    push({ batteryLevel: 9, isCharging: false, connectType: 'connected' });
    expect(warns()).toBe(1);

    push({ batteryLevel: 50, isCharging: false, connectType: 'connected' });
    push({ batteryLevel: 8, isCharging: false, connectType: 'connected' });
    expect(warns()).toBe(2);
  });

  it('never warns while charging', () => {
    const { push, hud } = make('mac');
    push({ batteryLevel: 5, isCharging: true, connectType: 'connected' });
    expect(hud.setStatus.mock.calls.some(([s]: [string]) => s.includes('battery'))).toBe(false);
  });
});
