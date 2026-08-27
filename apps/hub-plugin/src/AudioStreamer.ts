import { AudioInputSource } from '@evenrealities/even_hub_sdk';
import type { BridgeClient } from './BridgeClient';
import type { MacLink } from './MacLink';

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
  /** Which mic was last (or is currently) hot — the menu's unmute reuses it. */
  lastSource: AudioInputSource = AudioInputSource.Glasses;
  /** Fired on every real open/close so the Mac's mic pill reflects TRUTH. */
  onState?: (open: boolean) => void;
  /** Dominant speakerRole across the current batch (SDK 0.0.14). */
  private speaker?: string;

  constructor(
    private readonly client: BridgeClient,
    private readonly link: MacLink,
    private readonly flushMs = 200,
  ) {}

  /** True while the mic is streaming. DeviceMonitor keys resume logic off this. */
  get isOpen(): boolean {
    return this.open;
  }

  /** Glasses mic requires a successful startup page; phone mic does not. */
  async start(source: AudioInputSource = AudioInputSource.Glasses): Promise<boolean> {
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
    this.open = true;
    this.lastSource = source;
    this.onState?.(true);
    this.flushTimer = setInterval(() => this.flush(source), this.flushMs);
    return true;
  }

  async stop(): Promise<void> {
    if (!this.open) return;
    this.open = false;
    this.onState?.(false);
    if (this.flushTimer) clearInterval(this.flushTimer);
    this.flushTimer = undefined;
    this.buffer = [];
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
    // Attribution rides the batch (self/other/unknown). Passed through for
    // calibration on the Mac; a future gate keeps non-wearer speech out of
    // STT entirely — which STRENGTHENS the explicit-invocation rule.
    if (typeof speakerRole === 'string' && speakerRole.length > 0) {
      this.speaker = speakerRole;
    }
    if (!this.open) return;
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
    this.link.send({
      kind: 'audio',
      source: source === AudioInputSource.Glasses ? 'glasses' : 'phone',
      seq: this.seq++,
      pcm: this.buffer.splice(0),
      ...(this.speaker !== undefined ? { speaker: this.speaker } : {}),
    });
    this.speaker = undefined;
  }
}

/** Two little-endian bytes → one signed 16-bit sample. */
function toSigned16(lo: number, hi: number): number {
  const v = lo | (hi << 8);
  return v >= 0x8000 ? v - 0x10000 : v;
}
