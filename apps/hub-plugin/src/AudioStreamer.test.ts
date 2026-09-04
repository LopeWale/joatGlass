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

  it('erases partial PCM and marks a discontinuity across mic stop and resume', async () => {
    const { audio, sent } = make();
    await audio.start();
    audio.ingest([0x02, 0x01], 'other');
    vi.advanceTimersByTime(200);
    // Both a complete unflushed sample and a half-sample are private to the
    // old capture. Neither may be joined to speech after mute/doff/reconnect.
    audio.ingest([0x03, 0x00, 0xaa], 'other');
    await audio.stop();
    await audio.stop(); // redundant cleanup does not invent another boundary
    audio.ingest([0xff, 0xff], 'other'); // closed ingress is inert
    await audio.start();
    audio.ingest([0x04, 0x00, 0x05, 0x00], 'self');
    vi.advanceTimersByTime(200);

    const messages = sent.filter((message) => message.kind === 'audio');
    expect(messages.map((message) => message.seq)).toEqual([0, 2]);
    expect(messages.map((message) => message.pcm)).toEqual([[258], [4, 5]]);
    expect(messages[1]?.speaker).toBe('self');
  });

  it('splits a delayed flush into sample-bounded messages', async () => {
    const { audio, sent } = make();
    await audio.start();
    // Four nominal 200 ms chunks plus a short tail arrive before the timer.
    const sourceSamples = Array.from(
      { length: 4 * 3_200 + 137 },
      (_, index) => (index % 32_768) - 16_384,
    );
    const sourceBytes = new Uint8Array(sourceSamples.length * 2);
    const sourceView = new DataView(sourceBytes.buffer);
    sourceSamples.forEach((sample, index) => sourceView.setInt16(index * 2, sample, true));
    audio.ingest(sourceBytes);

    vi.advanceTimersByTime(200);

    const messages = sent.filter((message) => message.kind === 'audio');
    expect(messages).toHaveLength(5);
    expect(messages.map((message) => message.pcm.length)).toEqual([
      3_200, 3_200, 3_200, 3_200, 137,
    ]);
    expect(messages.map((message) => message.seq)).toEqual([0, 1, 2, 3, 4]);
    expect(messages.flatMap((message) => message.pcm)).toEqual(sourceSamples);
  });

  it('marks a mixed-speaker batch unknown without changing its PCM sequence', async () => {
    const { audio, sent } = make();
    await audio.start();

    // Speaker attribution is per SDK audio event, while the wire batch may
    // contain several events.  It must never relabel the first samples with
    // the final event's role: the Mac rejects an utterance only when every
    // received frame is "other".
    audio.ingest(new Uint8Array([0x02, 0x01]), 'self');
    audio.ingest(new Uint8Array([0xff, 0xff]), 'other');
    vi.advanceTimersByTime(200);

    const message = sent[0];
    if (message?.kind !== 'audio') throw new Error('expected audio');
    expect(message.pcm).toEqual([258, -1]);
    expect(message.speaker).toBe('unknown');
  });

  it('does not promote an unlabeled event to other-speaker audio', async () => {
    const { audio, sent } = make();
    await audio.start();

    audio.ingest(new Uint8Array([0x02, 0x01]));
    audio.ingest(new Uint8Array([0xff, 0xff]), 'other');
    vi.advanceTimersByTime(200);

    const message = sent[0];
    if (message?.kind !== 'audio') throw new Error('expected audio');
    expect(message.pcm).toEqual([258, -1]);
    expect(message.speaker).toBe('unknown');
  });

  it('retains unanimous other-speaker attribution for the Mac admission gate', async () => {
    const { audio, sent } = make();
    await audio.start();

    audio.ingest(new Uint8Array([0x02, 0x01]), 'other');
    audio.ingest(new Uint8Array([0xff, 0xff]), 'other');
    vi.advanceTimersByTime(200);

    const message = sent[0];
    if (message?.kind !== 'audio') throw new Error('expected audio');
    expect(message.speaker).toBe('other');
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
