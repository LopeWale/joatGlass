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
    const bridge =
      source === AudioInputSource.Glasses ? this.client.assertStarted() : this.client.require();
    const ok = await bridge.audioControl(true, source);
    if (!ok) return false;
    this.open = true;
    this.flushTimer = setInterval(() => this.flush(source), this.flushMs);
    return true;
  }

  async stop(): Promise<void> {
    if (!this.open) return;
    await this.client.require().audioControl(false);
    this.open = false;
    if (this.flushTimer) clearInterval(this.flushTimer);
    this.flushTimer = undefined;
    this.buffer = [];
  }

  /** Called by EventRouter for each audioEvent. */
  ingest(pcm: Uint8Array | number[]): void {
    if (!this.open) return;
    for (const sample of pcm) this.buffer.push(sample);
  }

  private flush(source: AudioInputSource): void {
    if (this.buffer.length === 0) return;
    this.link.send({
      kind: 'audio',
      source: source === AudioInputSource.Glasses ? 'glasses' : 'phone',
      seq: this.seq++,
      pcm: this.buffer.splice(0),
    });
  }
}
