import { AudioInputSource } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import type { MacLink } from './MacLink';

const SAMPLE_RATE = 16_000;

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
  private buffer: number[] = [];
  private flushTimer?: ReturnType<typeof setInterval>;
  private open = false;
  private controlEpoch = 0;
  private desiredOpen = false;
  /** Which mic was last (or is currently) hot — the menu's unmute reuses it. */
  lastSource: AudioInputSource = AudioInputSource.Glasses;
  /** Fired on every real open/close so the Mac's mic pill reflects TRUTH. */
  onState?: (open: boolean) => void;
  /**
   * Speaker attribution is supplied per SDK event, but we send coalesced PCM
   * batches.  A batch is safe to label only when every included event agrees;
   * the Mac may discard an utterance labelled entirely "other".
   */
  private speaker?: string;
  private speakerMixed = false;
  private speakerMissing = false;
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

  /** Glasses mic requires a successful startup page; phone mic does not. */
  async start(
    source: AudioInputSource = AudioInputSource.Glasses,
    signal?: AbortSignal,
  ): Promise<boolean> {
    const epoch = ++this.controlEpoch;
    this.desiredOpen = true;
    // Idempotent: a second start while open must not stack another flush
    // timer (the overwritten handle would leak and flush forever).
    if (this.open) return true;
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
    this.flushTimer = setInterval(() => this.flush(source), this.flushMs);
    return true;
  }

  async stop(): Promise<void> {
    this.controlEpoch += 1;
    this.desiredOpen = false;
    this.open = false;
    this.onState?.(false);
    if (this.flushTimer) clearInterval(this.flushTimer);
    this.flushTimer = undefined;
    this.buffer = [];
    this.resetSpeakerAttribution();
    try {
      await this.client.require().audioControl(false);
    } catch {
      // Host without audio support: nothing was ever on.
    }
  }

  // A byte left over when an event ends mid-sample; prepended to the next
  // event. Without this, one odd-length event misaligns every later sample.
  private carry: number | null = null;

  /**
   * Called by EventRouter for each audioEvent.
   *
   * The SDK's audioPcm is a Uint8Array of s16le BYTES (PCM 字节码), not an
   * array of samples — treating bytes as samples yields near-silent noise at
   * half speed (peak 255 instead of 32767). Decode byte pairs here so the
   * wire carries real 16-bit samples, as protocol.py documents.
   */
  ingest(pcm: Uint8Array | number[], speakerRole?: string | null): void {
    if (!this.open) return;
    this.observeSpeaker(speakerRole);
    const bytes = pcm instanceof Uint8Array ? pcm : Uint8Array.from(pcm);
    let i = 0;
    if (this.carry !== null && bytes.length > 0) {
      this.buffer.push(toSigned16(this.carry, bytes[0] as number));
      this.carry = null;
      i = 1;
    }
    for (; i + 1 < bytes.length; i += 2) {
      this.buffer.push(toSigned16(bytes[i] as number, bytes[i + 1] as number));
    }
    if (i < bytes.length) this.carry = bytes[i] as number;
  }

  private flush(source: AudioInputSource): void {
    if (this.buffer.length === 0) return;
    const speaker = this.speakerMixed ? 'unknown' : this.speaker;
    while (this.buffer.length > 0) {
      this.link.send({
        kind: 'audio',
        source: source === AudioInputSource.Glasses ? 'glasses' : 'phone',
        seq: this.seq++,
        pcm: this.buffer.splice(0, this.samplesPerMessage),
        ...(speaker !== undefined ? { speaker } : {}),
      });
    }
    this.resetSpeakerAttribution();
  }

  private observeSpeaker(speakerRole: string | null | undefined): void {
    if (typeof speakerRole !== 'string' || speakerRole.length === 0) {
      this.speakerMissing = true;
      if (this.speaker !== undefined) this.speakerMixed = true;
      return;
    }
    // Unknown future SDK labels must never become a false "other" claim.
    const speaker = ['self', 'other', 'unknown'].includes(speakerRole)
      ? speakerRole
      : 'unknown';
    if (this.speaker === undefined) {
      this.speaker = speaker;
    } else if (this.speaker !== speaker) {
      this.speakerMixed = true;
    }
    if (this.speakerMissing) this.speakerMixed = true;
  }

  private resetSpeakerAttribution(): void {
    this.speaker = undefined;
    this.speakerMixed = false;
    this.speakerMissing = false;
  }
}

/** Two little-endian bytes → one signed 16-bit sample. */
function toSigned16(lo: number, hi: number): number {
  const v = lo | (hi << 8);
  return v >= 0x8000 ? v - 0x10000 : v;
}
