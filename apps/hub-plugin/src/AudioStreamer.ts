import { AudioInputSource } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import type { MacLink } from './MacLink';
import type { UplinkMessage } from './types';

const SAMPLE_RATE = 16_000;

export interface PromptAudioEpoch {
  readonly promptId: string;
  readonly source: AudioInputSource;
  /** Local MacLink dispatcher generation, not the Mac audio-session string. */
  readonly generation: number;
}

export interface PromptMicrophone {
  readonly ready: Promise<boolean>;
  release(): void;
}

interface EpochState extends PromptAudioEpoch {
  ready: boolean;
  invalidated: boolean;
}

interface AudioSegment {
  source: AudioInputSource;
  /** A legacy fallback remains usable for ordinary speech, never prompt proof. */
  sourceKnown: boolean;
  speaker?: string;
  epoch?: EpochState;
  samples: number[];
}

interface PartialSample {
  byte: number;
  source: AudioInputSource;
  sourceKnown: boolean;
  speaker?: string;
  epoch?: EpochState;
}

/**
 * Streams microphone PCM to the Mac.
 *
 * The SDK delivers audio already decoded — PCM 16 kHz, signed 16-bit LE, mono —
 * via audioEvent.audioPcm. No LC3 decode is needed on this path (that is only
 * for the Phase-5 direct-BLE transport).
 *
 * Frames are batched before sending: one WebSocket message per ~20 ms frame
 * would swamp the link, so we coalesce into ~200 ms chunks.
 */
export class AudioStreamer {
  private seq = 0;
  private buffer: AudioSegment[] = [];
  private flushTimer?: ReturnType<typeof setInterval>;
  private open = false;
  private controlEpoch = 0;
  private desiredOpen = false;
  private controlTail: Promise<unknown> = Promise.resolve();
  /** Which mic was last (or is currently) hot — the menu's unmute reuses it. */
  lastSource: AudioInputSource = AudioInputSource.Glasses;
  /** Fired on every real open/close so the Mac's mic pill reflects TRUTH. */
  onState?: (open: boolean, micPromptId?: string) => void;
  /** Synchronous owner revocation; called only after this epoch is inert. */
  onPromptInvalidated?: (promptId: string) => void;
  private promptEpoch?: EpochState;
  private promptRevision = 0;
  private readonly samplesPerMessage: number;

  constructor(
    private readonly client: BridgeClient,
    private readonly link: MacLink,
    private readonly flushMs = 200,
  ) {
    this.samplesPerMessage = Math.max(1, Math.round(SAMPLE_RATE * flushMs / 1_000));
  }

  /** True while the mic is streaming. DeviceMonitor keys resume logic off this. */
  get isOpen(): boolean {
    return this.open;
  }

  activatePromptEpoch(epoch: PromptAudioEpoch, acknowledge: () => boolean): boolean {
    if (
      !this.open || typeof epoch.promptId !== 'string'
      || !/^[A-Za-z0-9._:-]{1,128}$/.test(epoch.promptId)
      || (epoch.source !== AudioInputSource.Glasses && epoch.source !== AudioInputSource.Phone)
      || !Number.isSafeInteger(epoch.generation) || epoch.generation < 1
      || typeof acknowledge !== 'function'
    ) return false;
    const revision = ++this.promptRevision;
    // Segments already carry their original identity. They must reach the
    // link before the new acknowledgment; a half-sample cannot cross it.
    this.promptEpoch = undefined;
    try {
      this.flush();
    } catch {
      this.carry = null;
      return false;
    }
    if (revision !== this.promptRevision || !this.open) return false;
    this.carry = null;
    const next: EpochState = {
      promptId: epoch.promptId, source: epoch.source, generation: epoch.generation,
      ready: false, invalidated: false,
    };
    this.promptEpoch = next;
    let accepted = false;
    try {
      accepted = acknowledge() === true;
    } catch {
      // A failed receipt never grants an audio epoch.
    }
    if (!accepted || this.promptEpoch !== next || next.invalidated || !this.open) {
      if (this.promptEpoch === next) this.clearPromptEpoch(next.promptId);
      return false;
    }
    next.ready = true;
    return true;
  }

  clearPromptEpoch(promptId?: string): void {
    const current = this.promptEpoch;
    if (current === undefined || (promptId !== undefined && current.promptId !== promptId)) return;
    current.invalidated = true;
    this.promptEpoch = undefined;
    this.promptRevision += 1;
    this.buffer = this.buffer.filter((segment) => segment.epoch !== current);
    this.carry = null;
    this.seq += 1;
    try {
      this.onPromptInvalidated?.(current.promptId);
    } catch {
      console.warn('[AudioStreamer] prompt invalidation callback failed');
    }
  }

  /** Glasses mic requires a successful startup page; phone mic does not. */
  async start(
    source: AudioInputSource = AudioInputSource.Glasses,
    signal?: AbortSignal,
  ): Promise<boolean> {
    if (signal?.aborted || (this.open && this.lastSource !== source)) return false;
    const epoch = ++this.controlEpoch;
    this.desiredOpen = true;
    // An existing stream cannot attest to a different requested source.
    if (this.open) return this.lastSource === source;
    return this.control(async () => {
      if (signal?.aborted || epoch !== this.controlEpoch || !this.desiredOpen) return false;
      try {
        const bridge =
          source === AudioInputSource.Glasses ? this.client.assertStarted() : this.client.require();
        const ok = await bridge.audioControl(true, source);
        if (!ok) return false;
      } catch (err) {
        // A host without mic support must degrade the mic, not kill boot.
        console.warn('[AudioStreamer] audioControl unavailable on this host:', err);
        return false;
      }
      if (signal?.aborted || epoch !== this.controlEpoch || !this.desiredOpen) {
        // A stop/reconnect overtook this start while the native call was in
        // flight. Turn it off again after the late success; merely ignoring the
        // result would leave a hot microphone behind false local state.
        try {
          await this.client.require().audioControl(false);
        } catch {
          // Best-effort compensation; link-loss cleanup will retry stop.
        }
        return false;
      }
      this.open = true;
      this.lastSource = source;
      this.onState?.(true);
      this.flushTimer = setInterval(() => this.flush(), this.flushMs);
      return true;
    });
  }

  /** The question owns only the microphone it opened. External starts/stops
   * supersede that ownership, and release revokes local capture synchronously. */
  openPromptMicrophone(source: AudioInputSource, promptId: string): PromptMicrophone {
    if (this.open) return {
      ready: Promise.resolve(this.lastSource === source), release: () => undefined,
    };
    const ready = this.start(source);
    const epoch = this.controlEpoch;
    let released = false;
    return { ready, release: () => {
      if (released) return;
      released = true;
      if (epoch === this.controlEpoch) void this.stop(promptId);
    } };
  }

  // Serialize only native mic control, including late-start compensation.
  // Display and other SDK calls do not share this lane.
  private control<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.controlTail.then(operation, operation);
    this.controlTail = result.catch(() => undefined);
    return result;
  }

  async stop(micPromptId?: string): Promise<void> {
    this.controlEpoch += 1;
    this.desiredOpen = false;
    // A pause discards PCM, even when no full batch was sent. Reserve a
    // sequence position so resumed speech cannot join the pre-mute utterance
    // on an otherwise unchanged socket. Repeated cleanup remains idempotent.
    if (this.open && this.promptEpoch === undefined) this.seq += 1;
    this.open = false;
    this.clearPromptEpoch();
    this.onState?.(false, micPromptId);
    if (this.flushTimer) clearInterval(this.flushTimer);
    this.flushTimer = undefined;
    this.buffer = [];
    this.carry = null;
    await this.control(async () => {
      try {
        await this.client.require().audioControl(false);
      } catch {
        // Host without audio support: nothing was ever on.
      }
    });
  }

  // A byte left over when an event ends mid-sample; prepended to the next
  // event. Without this, one odd-length event misaligns every later sample.
  private carry: PartialSample | null = null;

  /**
   * Called by EventRouter for each audioEvent.
   *
   * The SDK's audioPcm is a Uint8Array of s16le BYTES (PCM 字节码), not an
   * array of samples — treating bytes as samples yields near-silent noise at
   * half speed (peak 255 instead of 32767). Decode byte pairs here so the
   * wire carries real 16-bit samples, as protocol.py documents.
   */
  ingest(
    pcm: Uint8Array | number[], speakerRole?: string | null, source?: AudioInputSource,
  ): void {
    if (!this.open) return;
    const sourceKnown = source === AudioInputSource.Glasses || source === AudioInputSource.Phone;
    const actualSource = sourceKnown ? source : this.lastSource;
    const current = this.promptEpoch;
    let epoch = current?.ready ? current : undefined;
    if (current !== undefined && (!sourceKnown || actualSource !== current.source)) {
      this.clearPromptEpoch(current.promptId);
      // This event preceded any publication made by the invalidation
      // callback, so it cannot acquire that newer question's identity.
      epoch = undefined;
      if (!this.open) return;
    }
    const speaker = normalizeSpeaker(speakerRole);
    const bytes = pcm instanceof Uint8Array ? pcm : Uint8Array.from(pcm);
    let i = 0;
    // The requested microphone is not evidence of the source of this event.
    // Do not complete a sample with a byte from another (or newly unknown)
    // source. A speaker change on the SAME source still preserves waveform.
    if (this.carry !== null && (
      this.carry.source !== actualSource || this.carry.sourceKnown !== sourceKnown
      || this.carry.epoch !== epoch
    )) this.carry = null;
    if (this.carry !== null && bytes.length > 0) {
      const sampleSpeaker = combinedSpeaker(this.carry.speaker, speaker);
      this.segment(actualSource, sourceKnown, sampleSpeaker, epoch).samples.push(
        toSigned16(this.carry.byte, bytes[0] as number),
      );
      this.carry = null;
      i = 1;
    }
    if (i + 1 < bytes.length) {
      const segment = this.segment(actualSource, sourceKnown, speaker, epoch);
      for (; i + 1 < bytes.length; i += 2) {
        segment.samples.push(toSigned16(bytes[i] as number, bytes[i + 1] as number));
      }
    }
    if (i < bytes.length) {
      this.carry = { byte: bytes[i] as number, source: actualSource, sourceKnown, speaker, epoch };
    }
  }

  private segment(
    source: AudioInputSource, sourceKnown: boolean, speaker?: string, epoch?: EpochState,
  ): AudioSegment {
    const last = this.buffer[this.buffer.length - 1];
    if (last && last.source === source && last.sourceKnown === sourceKnown
      && last.speaker === speaker && last.epoch === epoch) {
      return last;
    }
    const segment: AudioSegment = { source, sourceKnown, speaker, epoch, samples: [] };
    this.buffer.push(segment);
    return segment;
  }

  private flush(): void {
    const pending = this.buffer;
    this.buffer = [];
    for (const segment of pending) {
      const epoch = segment.epoch;
      while (segment.samples.length > 0) {
        if (epoch?.invalidated) break;
        const message: Extract<UplinkMessage, { kind: 'audio' }> = {
          kind: 'audio',
          source: segment.source,
          seq: this.seq++,
          pcm: segment.samples.splice(0, this.samplesPerMessage),
          ...(segment.speaker !== undefined ? { speaker: segment.speaker } : {}),
          ...(epoch !== undefined ? { audioEpoch: epoch.promptId } : {}),
        };
        if (epoch !== undefined) {
          if (!this.link.sendForGeneration(message, epoch.generation)) {
            epoch.invalidated = true;
            if (this.promptEpoch === epoch) this.clearPromptEpoch(epoch.promptId);
            break;
          }
        } else {
          this.link.send(message);
        }
      }
    }
  }
}

function normalizeSpeaker(role: string | null | undefined): string | undefined {
  if (typeof role !== 'string' || role.length === 0) return undefined;
  return ['self', 'other', 'unknown'].includes(role) ? role : 'unknown';
}

/** A sample spanning two SDK roles retains any explicit other-speaker evidence. */
function combinedSpeaker(first: string | undefined, second: string | undefined): string | undefined {
  if (first === 'other' || second === 'other') return 'other';
  return first === second ? first : 'unknown';
}

/** Two little-endian bytes → one signed 16-bit sample. */
function toSigned16(lo: number, hi: number): number {
  const v = lo | (hi << 8);
  return v >= 0x8000 ? v - 0x10000 : v;
}
