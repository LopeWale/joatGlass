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
});
