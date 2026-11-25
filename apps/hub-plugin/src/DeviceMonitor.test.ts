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
  sn?: string;
  batteryLevel?: number;
  isWearing?: boolean;
  isCharging?: boolean;
  isInCase?: boolean;
  connectType: string;
}) => void;

function make(
  policy: 'auto' | 'mac' | 'confirm' = 'confirm',
  micOpen = false,
  glassesSn?: string,
) {
  let statusCb: StatusCb = () => {};
  const sent: UplinkMessage[] = [];
  const audio = {
    isOpen: micOpen,
    start: vi.fn(async (_source?: unknown) => true),
    stop: vi.fn(async () => {}),
  };
  const imu = {
    prepare: vi.fn((_actionId: string, _onResult?: (a: boolean) => void) => {}),
    activate: vi.fn((_actionId: string, _timeoutMs?: number) => true),
    cancel: vi.fn((_actionId: string) => true),
  };
  const hud = {
    setStatus: vi.fn(async (_text: string) => true),
    showText: vi.fn(async (_text: string, _containerId?: number) => true),
    showConfirmation: vi.fn(async (_prompt: string, _actionId?: string) => true),
    dismissConfirmation: vi.fn(async (_actionId?: string) => true),
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

  const monitor = new DeviceMonitor(
    client as unknown as BridgeClient,
    { send: (m: UplinkMessage) => sent.push(m) } as unknown as MacLink,
    audio as unknown as AudioStreamer,
    imu as unknown as ImuMonitor,
    hud as unknown as HudPresenter,
    settings,
  );
  if (glassesSn) monitor.setGlassesSn(glassesSn);
  monitor.attach();

  return { push: (s: Parameters<StatusCb>[0]) => statusCb(s), sent, audio, imu, hud, monitor, settings };
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
    expect(imu.prepare).not.toHaveBeenCalled();
    expect(audio.start).not.toHaveBeenCalled();
    expect(hud.showText).not.toHaveBeenCalled();
    expect(hud.showConfirmation).not.toHaveBeenCalled();
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
    expect(imu.prepare).not.toHaveBeenCalled();
  });

  it('confirm: activates only after rendering; accept starts the mic', async () => {
    const { push, audio, imu, hud } = make('confirm');
    push({ isWearing: true, connectType: 'connected' });
    expect(imu.prepare).toHaveBeenCalledTimes(1);
    expect(hud.showConfirmation).toHaveBeenCalledWith('Resume listening?', 'local:resume');
    await vi.waitFor(() => expect(imu.activate).toHaveBeenCalledWith('local:resume', 8000));
    const onResult = imu.prepare.mock.calls[0]?.[1];
    expect(onResult).toBeDefined();

    onResult?.(false);
    expect(audio.start).not.toHaveBeenCalled();
    expect(hud.dismissConfirmation).toHaveBeenCalledTimes(1);
    onResult?.(true);
    expect(audio.start).toHaveBeenCalledTimes(1);
  });

  it('confirm: a failed HUD render cancels the exact action fail-closed', async () => {
    const { push, imu, hud } = make('confirm');
    hud.showConfirmation.mockResolvedValue(false);

    push({ isWearing: true, connectType: 'connected' });

    await vi.waitFor(() => expect(imu.cancel).toHaveBeenCalledWith('local:resume'));
  });

  it('confirm: a rejected HUD render also cancels fail-closed', async () => {
    const { push, imu, hud } = make('confirm');
    hud.showConfirmation.mockRejectedValue(new Error('bridge unavailable'));

    push({ isWearing: true, connectType: 'connected' });

    await vi.waitFor(() => expect(imu.cancel).toHaveBeenCalledWith('local:resume'));
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

describe('DeviceMonitor R1 ring / non-G2 sn isolation & micMode', () => {
  it('ignores Even R1 ring disconnect/reconnect status (different sn) for mic, wear, and battery while relaying telemetry', () => {
    const { push, sent, audio, imu, hud, monitor } = make('confirm', true, 'G2_GLASSES_7788');
    const batteries: Array<[number | undefined, boolean]> = [];
    monitor.onBattery = (lvl, chg) => batteries.push([lvl, chg]);

    push({ sn: 'G2_GLASSES_7788', batteryLevel: 82, isWearing: true, isCharging: false, connectType: 'connected' });
    expect(batteries).toEqual([[82, false]]);

    // SDK DeviceStatus.fromJson defaults omitted isWearing -> false and batteryLevel -> 0 on R1 flap:
    push({ sn: 'EVEN_R1_4412', batteryLevel: 0, isWearing: false, isCharging: false, isInCase: false, connectType: 'disconnected' });
    push({ sn: 'EVEN_R1_4412', batteryLevel: 64, isWearing: false, isCharging: false, isInCase: false, connectType: 'connected' });

    expect(audio.stop).not.toHaveBeenCalled();
    expect(imu.prepare).not.toHaveBeenCalled();
    expect(hud.setStatus).not.toHaveBeenCalled();
    expect(batteries).toEqual([[82, false]]);

    expect(sent[1]).toMatchObject({
      kind: 'device',
      connect: 'disconnected',
      deviceRole: 'other',
      snTail: '4412',
    });
    expect(sent[2]).toMatchObject({
      kind: 'device',
      connect: 'connected',
      deviceRole: 'other',
      snTail: '4412',
    });
  });

  it('honours micMode = phone on auto resume and micMode = on_demand without opening continuous BLE mic', () => {
    const phoneRig = make('auto', false, 'G2_GLASSES_7788');
    phoneRig.settings.setMicMode('phone');
    phoneRig.push({ sn: 'G2_GLASSES_7788', isWearing: true, connectType: 'connected' });
    expect(phoneRig.audio.start).toHaveBeenCalledWith('phone');

    const pttRig = make('auto', false, 'G2_GLASSES_7788');
    pttRig.settings.setMicMode('on_demand');
    pttRig.push({ sn: 'G2_GLASSES_7788', isWearing: true, connectType: 'connected' });
    expect(pttRig.audio.start).not.toHaveBeenCalled();
    expect(pttRig.hud.setStatus).toHaveBeenCalledWith('JOAT · hold to talk');
  });
});
