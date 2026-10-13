import { AudioInputSource } from '@evenrealities/even_hub_sdk';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AudioStreamer, type PromptAudioEpoch } from './AudioStreamer';
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

  it('preserves explicit other-speaker evidence beside self speech without changing PCM', async () => {
    const { audio, sent } = make();
    await audio.start();

    // Keep homogeneous attribution segments. Collapsing both roles to
    // "unknown" erases evidence needed by the host's prompted-input gate.
    audio.ingest(new Uint8Array([0x02, 0x01]), 'self', AudioInputSource.Glasses);
    audio.ingest(new Uint8Array([0xff, 0xff]), 'other', AudioInputSource.Glasses);
    vi.advanceTimersByTime(200);

    const messages = sent.filter((message) => message.kind === 'audio');
    expect(messages.flatMap((message) => message.pcm)).toEqual([258, -1]);
    expect(messages.map((message) => [message.speaker, message.pcm])).toEqual([
      ['self', [258]], ['other', [-1]],
    ]);
  });

  it('preserves an unlabeled segment and the following explicit other-speaker segment', async () => {
    const { audio, sent } = make();
    await audio.start();

    audio.ingest(new Uint8Array([0x02, 0x01]), undefined, AudioInputSource.Glasses);
    audio.ingest(new Uint8Array([0xff, 0xff]), 'other', AudioInputSource.Glasses);
    vi.advanceTimersByTime(200);

    const messages = sent.filter((message) => message.kind === 'audio');
    expect(messages.flatMap((message) => message.pcm)).toEqual([258, -1]);
    expect(messages.map((message) => [message.speaker, message.pcm])).toEqual([
      [undefined, [258]], ['other', [-1]],
    ]);
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

  it('labels actual phone PCM as phone even when glasses were requested', async () => {
    const { audio, sent } = make();
    await audio.start(AudioInputSource.Glasses);

    audio.ingest([0x02, 0x01], 'self', AudioInputSource.Phone);
    vi.advanceTimersByTime(200);

    const messages = sent.filter((message) => message.kind === 'audio');
    expect(messages.map((message) => [message.source, message.pcm])).toEqual([
      ['phone', [258]],
    ]);
  });

  it('keeps actual source changes in order within one timed flush', async () => {
    const { audio, sent } = make();
    await audio.start(AudioInputSource.Glasses);

    audio.ingest([0x02, 0x01], 'self', AudioInputSource.Glasses);
    audio.ingest([0xff, 0xff], 'self', AudioInputSource.Phone);
    audio.ingest([0x03, 0x00], 'self', AudioInputSource.Glasses);
    vi.advanceTimersByTime(200);

    const messages = sent.filter((message) => message.kind === 'audio');
    expect(messages.map((message) => [message.source, message.pcm])).toEqual([
      ['glasses', [258]], ['phone', [-1]], ['glasses', [3]],
    ]);
    expect(messages.map((message) => message.seq)).toEqual([0, 1, 2]);
  });

  it.each([
    [AudioInputSource.Glasses, AudioInputSource.Phone],
    [AudioInputSource.Phone, AudioInputSource.Glasses],
  ])('does not join an odd byte across the %s to %s source edge', async (before, after) => {
    const { audio, sent } = make();
    await audio.start(before);

    audio.ingest([0x02, 0x01, 0xaa], 'self', before);
    audio.ingest([0x03, 0x00, 0x04, 0x00], 'self', after);
    vi.advanceTimersByTime(200);

    const messages = sent.filter((message) => message.kind === 'audio');
    // The old half-sample has no matching byte from its source. Complete
    // samples on both sides remain exact; 0x03 must not complete old 0xaa.
    expect(messages.map((message) => [message.source, message.pcm])).toEqual([
      [before, [258]], [after, [3, 4]],
    ]);
  });

  it('retains explicit other segments across a sample-bounded delayed flush', async () => {
    const { audio, sent } = make();
    await audio.start();
    const selfBytes = new Uint8Array(3_201 * 2);
    const otherBytes = new Uint8Array(3_202 * 2);
    for (let index = 0; index < selfBytes.length; index += 2) selfBytes[index] = 7;
    for (let index = 0; index < otherBytes.length; index += 2) otherBytes[index] = 9;

    audio.ingest(selfBytes, 'self', AudioInputSource.Glasses);
    audio.ingest(otherBytes, 'other', AudioInputSource.Glasses);
    vi.advanceTimersByTime(200);

    const messages = sent.filter((message) => message.kind === 'audio');
    expect(messages.every((message) => message.pcm.length <= 3_200)).toBe(true);
    expect(messages.flatMap((message) => message.pcm)).toEqual([
      ...Array<number>(3_201).fill(7), ...Array<number>(3_202).fill(9),
    ]);
    expect(messages.filter((message) => message.speaker === 'self')
      .flatMap((message) => message.pcm)).toEqual(Array<number>(3_201).fill(7));
    expect(messages.filter((message) => message.speaker === 'other')
      .flatMap((message) => message.pcm)).toEqual(Array<number>(3_202).fill(9));
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
    await Promise.resolve(); // Native start is now held in the microphone control lane.
    oldGeneration.abort();
    const stopping = audio.stop();
    releaseStart(true);

    await expect(starting).resolves.toBe(false);
    await stopping;
    expect(audio.isOpen).toBe(false);
    expect(controls).toEqual([true, false, false]);
  });
});

describe('AudioStreamer prompt microphone ownership', () => {
  it('preserves an existing ambient microphone and rejects a different source', async () => {
    const { audio } = make();
    await audio.start(AudioInputSource.Phone);
    const prompt = audio.openPromptMicrophone(AudioInputSource.Phone, 'question');
    expect(await prompt.ready).toBe(true);
    prompt.release();
    expect(audio.isOpen).toBe(true);
    const wrong = audio.openPromptMicrophone(AudioInputSource.Glasses, 'wrong');
    expect(await wrong.ready).toBe(false);
    wrong.release();
    expect(audio.isOpen).toBe(true);
    await audio.stop();
  });

  it('does not let old prompt cleanup stop a later explicit user start', async () => {
    const { audio } = make();
    const old = audio.openPromptMicrophone(AudioInputSource.Phone, 'old');
    expect(await old.ready).toBe(true);
    await audio.start(AudioInputSource.Phone);
    old.release();
    expect(audio.isOpen).toBe(true);
    await audio.stop();
  });

  it('serializes cancelled opening, compensation and a replacement microphone', async () => {
    let release!: (ok: boolean) => void;
    const held = new Promise<boolean>((resolve) => { release = resolve; });
    const controls: boolean[] = []; let active = 0; let maximum = 0;
    const bridge = { audioControl: async (open: boolean) => {
      active += 1; maximum = Math.max(maximum, active); controls.push(open);
      try { return controls.length === 1 ? await held : true; }
      finally { active -= 1; }
    } };
    const audio = new AudioStreamer({ require: () => bridge, assertStarted: () => bridge } as unknown as BridgeClient,
      { send: () => undefined } as unknown as MacLink);
    const old = audio.openPromptMicrophone(AudioInputSource.Phone, 'old');
    await Promise.resolve();
    old.release();
    const next = audio.openPromptMicrophone(AudioInputSource.Phone, 'next');
    release(true);
    expect(await old.ready).toBe(false);
    expect(await next.ready).toBe(true);
    old.release();
    expect(audio.isOpen).toBe(true);
    expect(maximum).toBe(1);
    expect(controls).toEqual([true, false, false, true]);
    next.release();
    expect(audio.isOpen).toBe(false);
    await audio.stop();
  });
});

describe('AudioStreamer prompt epochs', () => {
  type BoundMessage = Extract<UplinkMessage, { kind: 'audio' | 'prompt.ack' | 'prompt.cancel' }>;

  function activate(audio: AudioStreamer, epoch: PromptAudioEpoch, acknowledge: () => boolean): boolean {
    return audio.activatePromptEpoch(epoch, acknowledge);
  }
  function clear(audio: AudioStreamer, promptId?: string): void {
    audio.clearPromptEpoch(promptId);
  }
  function makePrompt() {
    const sent: UplinkMessage[] = [];
    const ordinary: UplinkMessage[] = [];
    const state = { generation: 7, authenticated: true };
    const sendForGeneration = vi.fn((message: BoundMessage, generation: number): boolean => {
      if (!state.authenticated || generation !== state.generation) return false;
      sent.push(message);
      return true;
    });
    const client = {
      assertStarted: () => ({ audioControl: async () => true }),
      require: () => ({ audioControl: async () => true }),
    } as unknown as BridgeClient;
    const audio = new AudioStreamer(client, {
      send: (message: UplinkMessage) => { ordinary.push(message); sent.push(message); },
      sendForGeneration,
    } as unknown as MacLink);
    function arm(promptId: string): boolean {
      const generation = state.generation;
      return activate(audio, { promptId, source: AudioInputSource.Glasses, generation }, () => (
        sendForGeneration({
          kind: 'prompt.ack', promptId, presentationBinding: `binding-${promptId}`,
          connectionGeneration: 'mac-audio-session', source: 'glasses',
          success: true, audioEpoch: promptId,
        }, generation)
      ));
    }
    return { audio, sent, ordinary, state, sendForGeneration, arm };
  }

  it('flushes old complete PCM before ack and drops its half-sample before tagging fresh audio', async () => {
    const rig = makePrompt();
    await rig.audio.start();
    rig.audio.ingest([1, 0, 0xaa], 'self', AudioInputSource.Glasses);

    expect(rig.arm('prompt-1')).toBe(true);
    rig.audio.ingest([2, 0], 'self', AudioInputSource.Glasses);
    vi.advanceTimersByTime(200);

    expect(rig.sent.map((message) => message.kind)).toEqual(['audio', 'prompt.ack', 'audio']);
    const audio = rig.sent.filter((message) => message.kind === 'audio');
    expect(audio.map((message) => [message.pcm, message.audioEpoch])).toEqual([
      [[1], undefined], [[2], 'prompt-1'],
    ]);
    expect(rig.ordinary.filter((message) => message.kind === 'audio').map((message) => message.pcm))
      .toEqual([[1]]);
    expect(rig.sendForGeneration).toHaveBeenLastCalledWith(audio[1], 7);
  });

  it('keeps the previous epoch on old buffered PCM when another prompt is acknowledged', async () => {
    const rig = makePrompt();
    await rig.audio.start();
    expect(rig.arm('prompt-1')).toBe(true);
    rig.audio.ingest([1, 0, 0xaa], 'self', AudioInputSource.Glasses);

    expect(rig.arm('prompt-2')).toBe(true);
    rig.audio.ingest([2, 0], 'self', AudioInputSource.Glasses);
    vi.advanceTimersByTime(200);

    expect(rig.sent.map((message) => message.kind)).toEqual([
      'prompt.ack', 'audio', 'prompt.ack', 'audio',
    ]);
    expect(rig.sent.filter((message) => message.kind === 'audio')
      .map((message) => [message.pcm, message.audioEpoch])).toEqual([
      [[1], 'prompt-1'], [[2], 'prompt-2'],
    ]);
    expect(rig.ordinary).toEqual([]);
  });

  it('never tags fresh PCM when the synchronous acknowledgment fails', async () => {
    const rig = makePrompt();
    await rig.audio.start();
    const acknowledge = vi.fn(() => false);
    expect(activate(rig.audio, {
      promptId: 'rejected', source: AudioInputSource.Glasses, generation: 7,
    }, acknowledge)).toBe(false);
    rig.audio.ingest([1, 0], 'self', AudioInputSource.Glasses);
    vi.advanceTimersByTime(200);

    expect(acknowledge).toHaveBeenCalledTimes(1);
    expect(rig.sent.filter((message) => message.kind === 'audio')
      .map((message) => [message.pcm, message.audioEpoch])).toEqual([[[1], undefined]]);
  });

  it('captures epoch at ingress rather than relabeling PCM buffered inside the acknowledgment', async () => {
    const rig = makePrompt();
    await rig.audio.start();
    expect(activate(rig.audio, {
      promptId: 'prompt-1', source: AudioInputSource.Glasses, generation: 7,
    }, () => {
      rig.audio.ingest([1, 0], 'self', AudioInputSource.Glasses);
      return true;
    })).toBe(true);
    rig.audio.ingest([2, 0], 'self', AudioInputSource.Glasses);
    vi.advanceTimersByTime(200);

    expect(rig.sent.filter((message) => message.kind === 'audio')
      .map((message) => [message.pcm, message.audioEpoch])).toEqual([
      [[1], undefined], [[2], 'prompt-1'],
    ]);
  });

  it.each([undefined, 'future-microphone' as AudioInputSource])(
    'does not tag PCM whose SDK source is %s', async (source) => {
      const rig = makePrompt();
      await rig.audio.start();
      expect(rig.arm('prompt-1')).toBe(true);
      rig.audio.ingest([1, 0], 'self', source);
      vi.advanceTimersByTime(200);

      expect(rig.sent.filter((message) => message.kind === 'audio')
        .map((message) => [message.pcm, message.audioEpoch])).toEqual([[[1], undefined]]);
    },
  );

  it('a source change invalidates the prompt and drops its buffered samples before normal audio resumes', async () => {
    const rig = makePrompt();
    await rig.audio.start();
    expect(rig.arm('prompt-1')).toBe(true);
    rig.audio.ingest([1, 0, 0xaa], 'self', AudioInputSource.Glasses);
    rig.audio.ingest([2, 0], 'self', AudioInputSource.Phone);
    rig.audio.ingest([3, 0], 'self', AudioInputSource.Glasses);
    vi.advanceTimersByTime(200);

    expect(rig.sent.filter((message) => message.kind === 'audio')
      .map((message) => [message.source, message.pcm, message.audioEpoch])).toEqual([
      ['phone', [2], undefined], ['glasses', [3], undefined],
    ]);
  });

  it('only matching clear drops the prompt buffer and carry and marks one discontinuity', async () => {
    const rig = makePrompt();
    await rig.audio.start();
    expect(rig.arm('prompt-1')).toBe(true);
    rig.audio.ingest([1, 0, 0xaa], 'self', AudioInputSource.Glasses);
    clear(rig.audio, 'some-other-prompt');
    rig.audio.ingest([0], 'self', AudioInputSource.Glasses);
    vi.advanceTimersByTime(200);
    const first = rig.sent.find((message) => message.kind === 'audio');
    expect(first?.pcm).toEqual([1, 170]);
    expect(first?.audioEpoch).toBe('prompt-1');

    rig.audio.ingest([2, 0, 0xbb], 'self', AudioInputSource.Glasses);
    clear(rig.audio, 'prompt-1');
    clear(rig.audio, 'prompt-1');
    rig.audio.ingest([3, 0], 'self', AudioInputSource.Glasses);
    vi.advanceTimersByTime(200);
    const messages = rig.sent.filter((message) => message.kind === 'audio');
    expect(messages.map((message) => [message.pcm, message.audioEpoch])).toEqual([
      [[1, 170], 'prompt-1'], [[3], undefined],
    ]);
    expect(messages[1]!.seq).toBe(messages[0]!.seq + 2);
  });

  it('drops stale-generation prompted audio without using the replayable send queue', async () => {
    const rig = makePrompt();
    await rig.audio.start();
    expect(rig.arm('prompt-1')).toBe(true);
    rig.audio.ingest([1, 0], 'self', AudioInputSource.Glasses);
    rig.state.generation = 8;
    vi.advanceTimersByTime(200);

    expect(rig.sent.map((message) => message.kind)).toEqual(['prompt.ack']);
    expect(rig.ordinary).toEqual([]);
    expect(rig.sendForGeneration).toHaveBeenLastCalledWith(expect.objectContaining({
      kind: 'audio', pcm: [1], audioEpoch: 'prompt-1',
    }), 7);
  });

  it('stop removes the epoch and pending bytes before microphone restart', async () => {
    const rig = makePrompt();
    await rig.audio.start();
    expect(rig.arm('prompt-1')).toBe(true);
    rig.audio.ingest([1, 0, 0xaa], 'self', AudioInputSource.Glasses);
    await rig.audio.stop();
    await rig.audio.start();
    rig.audio.ingest([2, 0], 'self', AudioInputSource.Glasses);
    vi.advanceTimersByTime(200);

    expect(rig.sent.filter((message) => message.kind === 'audio')
      .map((message) => [message.pcm, message.audioEpoch])).toEqual([[[2], undefined]]);
  });
});

describe('AudioStreamer stream liveness (Even Hub Background & Lifecycle)', () => {
  // Even's lifecycle page: `audioControl(true, ...)` capture "stops if the
  // WebView is suspended"; re-enable it on foreground with the same
  // AudioInputSource, and do not assume the stream is still live.
  function rig(results: boolean[] = []) {
    const calls: Array<[boolean, AudioInputSource | undefined]> = [];
    const audioControl = async (open: boolean, source?: AudioInputSource) => {
      calls.push([open, source]);
      return open ? (results.length ? results.shift()! : true) : true;
    };
    const client = {
      assertStarted: () => ({ audioControl }),
      require: () => ({ audioControl }),
    } as unknown as BridgeClient;
    const audio = new AudioStreamer(client, { send: () => undefined } as unknown as MacLink);
    return { audio, calls };
  }

  it('a question on a silent stream re-arms the host capture before it is ready', async () => {
    const { audio, calls } = rig();
    await audio.start(AudioInputSource.Glasses);
    audio.ingest(new Uint8Array([1, 0]));
    // The phone went to the background and came back: the host stopped
    // delivering PCM, and this page still believes the mic is open.
    vi.setSystemTime(Date.now() + 5_000);
    expect(audio.isOpen).toBe(true);
    expect(audio.isStale()).toBe(true);
    calls.length = 0;
    const mic = audio.openPromptMicrophone(AudioInputSource.Glasses, 'reply');
    await expect(mic.ready).resolves.toBe(true);
    expect(calls).toEqual([[true, AudioInputSource.Glasses]]);
    expect(audio.isStale()).toBe(false);
  });

  it('a question on a live stream does not touch the host', async () => {
    const { audio, calls } = rig();
    await audio.start(AudioInputSource.Glasses);
    audio.ingest(new Uint8Array([1, 0]));
    calls.length = 0;
    const mic = audio.openPromptMicrophone(AudioInputSource.Glasses, 'reply');
    await expect(mic.ready).resolves.toBe(true);
    expect(calls).toEqual([]);
  });

  it('a host that refuses the re-arm leaves the mic honestly off', async () => {
    const { audio, calls } = rig([true, false]);
    const states: boolean[] = [];
    audio.onState = (open) => states.push(open);
    await audio.start(AudioInputSource.Glasses);
    vi.setSystemTime(Date.now() + 5_000);
    const mic = audio.openPromptMicrophone(AudioInputSource.Glasses, 'reply');
    await expect(mic.ready).resolves.toBe(false);
    await vi.runOnlyPendingTimersAsync();
    expect(audio.isOpen).toBe(false);
    expect(states).toEqual([true, false]);
    expect(calls[calls.length - 1]).toEqual([false, undefined]);
  });

  it('a stream that goes silent re-arms itself, at most every ten seconds', async () => {
    const { audio, calls } = rig();
    await audio.start(AudioInputSource.Glasses);
    calls.length = 0;
    await vi.advanceTimersByTimeAsync(1_400);
    expect(calls).toEqual([]); // not stale yet
    await vi.advanceTimersByTimeAsync(400);
    expect(calls).toEqual([[true, AudioInputSource.Glasses]]);
    await vi.advanceTimersByTimeAsync(9_000);
    expect(calls).toHaveLength(1); // still silent, but rate-limited
    await vi.advanceTimersByTimeAsync(1_200);
    expect(calls).toHaveLength(2);
    // Frames flowing again: no more re-arms.
    for (let i = 0; i < 100; i += 1) {
      audio.ingest(new Uint8Array([1, 0]));
      await vi.advanceTimersByTimeAsync(200);
    }
    expect(calls).toHaveLength(2);
    await audio.stop();
  });

  it('re-arm keeps the same microphone (phone stays phone)', async () => {
    const { audio, calls } = rig();
    await audio.start(AudioInputSource.Phone);
    calls.length = 0;
    await expect(audio.rearm('foreground')).resolves.toBe(true);
    expect(calls).toEqual([[true, AudioInputSource.Phone]]);
  });

  it('re-arm is a no-op while the mic is off', async () => {
    const { audio, calls } = rig();
    await expect(audio.rearm('foreground')).resolves.toBe(false);
    expect(calls).toEqual([]);
  });
});

describe('AudioStreamer bounded mic calls (2026-10-13)', () => {
  // A host mic call that never answered held every later mic call behind it,
  // and the Mac's `audio.control` waited until MacLink's 15 s watchdog
  // dropped the link — 45 times in one day.
  function rig() {
    const calls: boolean[] = [];
    const pending: Array<(ok: boolean) => void> = [];
    let hang = true;
    const audioControl = (open: boolean) => {
      calls.push(open);
      if (open && hang) return new Promise<boolean>((resolve) => pending.push(resolve));
      return Promise.resolve(true);
    };
    const client = {
      assertStarted: () => ({ audioControl }),
      require: () => ({ audioControl }),
    } as unknown as BridgeClient;
    const audio = new AudioStreamer(client, { send: () => undefined } as unknown as MacLink);
    return { audio, calls, pending, answer: () => { hang = false; } };
  }

  it('a start the host never answers gives up within the bridge limit', async () => {
    const { audio } = rig();
    const started = audio.start(AudioInputSource.Glasses);
    await vi.advanceTimersByTimeAsync(5_000);
    await expect(started).resolves.toBe(false);
    expect(audio.isOpen).toBe(false);
  });

  it('the next mic call is not stuck behind a hung one', async () => {
    const { audio, answer } = rig();
    const first = audio.start(AudioInputSource.Glasses);
    await vi.advanceTimersByTimeAsync(5_000);
    await first;
    answer();
    await expect(audio.start(AudioInputSource.Glasses)).resolves.toBe(true);
    expect(audio.isOpen).toBe(true);
  });

  it('a start that succeeds late is adopted while the mic is still wanted', async () => {
    const { audio, pending } = rig();
    const states: boolean[] = [];
    audio.onState = (open) => states.push(open);
    const started = audio.start(AudioInputSource.Glasses);
    await vi.advanceTimersByTimeAsync(5_000);
    await expect(started).resolves.toBe(false);
    pending[0]!(true);
    await vi.advanceTimersByTimeAsync(0);
    expect(audio.isOpen).toBe(true);
    expect(states).toEqual([true]);
  });

  it('a start that succeeds after the mic was stopped is turned off again', async () => {
    const { audio, calls, pending } = rig();
    const started = audio.start(AudioInputSource.Glasses);
    await vi.advanceTimersByTimeAsync(5_000);
    await started;
    await audio.stop();
    calls.length = 0;
    pending[0]!(true);
    await vi.advanceTimersByTimeAsync(0);
    expect(audio.isOpen).toBe(false);
    expect(calls).toEqual([false]);
  });

  it('a stop the host never answers still finishes', async () => {
    let hangStop = false;
    const audioControl = (open: boolean) =>
      !open && hangStop ? new Promise<boolean>(() => undefined) : Promise.resolve(true);
    const client = {
      assertStarted: () => ({ audioControl }),
      require: () => ({ audioControl }),
    } as unknown as BridgeClient;
    const audio = new AudioStreamer(client, { send: () => undefined } as unknown as MacLink);
    await audio.start(AudioInputSource.Glasses);
    hangStop = true;
    const stopped = audio.stop();
    await vi.advanceTimersByTimeAsync(5_000);
    await expect(stopped).resolves.toBeUndefined();
    hangStop = false;
    await expect(audio.start(AudioInputSource.Glasses)).resolves.toBe(true);
  });
});
