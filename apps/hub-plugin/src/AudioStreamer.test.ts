import { AudioInputSource } from '@evenrealities/even_hub_sdk';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AudioStreamer } from './AudioStreamer';
import type { BridgeClient } from './BridgeClient';
import type { MacLink } from './MacLink';
import type { UplinkMessage } from './types';

function make() {
  const sent: UplinkMessage[] = [];
  const client = {
    assertStarted: () => ({ audioControl: async () => true }),
    require: () => ({ audioControl: async () => true }),
  } as unknown as BridgeClient;
  const audio = new AudioStreamer(client, {
    send: (m: UplinkMessage) => sent.push(m),
  } as unknown as MacLink);
  return { audio, sent };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe('AudioStreamer byte decoding', () => {
  it('decodes s16le byte pairs into real samples', async () => {
    const { audio, sent } = make();
    await audio.start();
    // 0x0102=258, 0xFFFF=-1, 0x8000=-32768
    audio.ingest(new Uint8Array([0x02, 0x01, 0xff, 0xff, 0x00, 0x80]));
    vi.advanceTimersByTime(250);
    expect(sent).toHaveLength(1);
    const msg = sent[0];
    if (msg?.kind !== 'audio') throw new Error('expected audio');
    expect(msg.pcm).toEqual([258, -1, -32768]);
  });

  it('carries an odd trailing byte into the next event without misalignment', async () => {
    const { audio, sent } = make();
    await audio.start();
    audio.ingest(new Uint8Array([0x02, 0x01, 0xaa])); // sample 258 + half a sample
    audio.ingest(new Uint8Array([0x00]));             // completes 0x00AA = 170
    audio.ingest(new Uint8Array([0x03, 0x00]));       // sample 3, aligned again
    vi.advanceTimersByTime(250);
    const msg = sent[0];
    if (msg?.kind !== 'audio') throw new Error('expected audio');
    expect(msg.pcm).toEqual([258, 0x00aa, 3]);
  });

  it('accepts plain number[] (host JSON path) identically', async () => {
    const { audio, sent } = make();
    await audio.start();
    audio.ingest([0x02, 0x01]);
    vi.advanceTimersByTime(250);
    const msg = sent[0];
    if (msg?.kind !== 'audio') throw new Error('expected audio');
    expect(msg.pcm).toEqual([258]);
  });

  it('a new stop invalidates and compensates an older pending start', async () => {
    let releaseStart!: (started: boolean) => void;
    const pendingStart = new Promise<boolean>((resolve) => { releaseStart = resolve; });
    const controls: boolean[] = [];
    const client = {
      assertStarted: () => ({
        audioControl: async (open: boolean) => {
          controls.push(open);
          if (open) return pendingStart;
          return true;
        },
      }),
      require: () => ({
        audioControl: async (open: boolean) => {
          controls.push(open);
          return true;
        },
      }),
    } as unknown as BridgeClient;
    const audio = new AudioStreamer(client, { send: () => undefined } as unknown as MacLink);
    const oldGeneration = new AbortController();

    const starting = audio.start(AudioInputSource.Glasses, oldGeneration.signal);
    oldGeneration.abort();
    await audio.stop();
    releaseStart(true);

    await expect(starting).resolves.toBe(false);
    expect(audio.isOpen).toBe(false);
    expect(controls).toEqual([true, false, false]);
  });
});
