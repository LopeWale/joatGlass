import type { DownlinkMessage, UplinkMessage } from './types';

/** Observable link lifecycle, for status surfaces (webview panel, HUD). */
export type LinkState = 'connecting' | 'connected' | 'reconnecting' | 'closed';

export interface MacLinkOptions {
  /**
   * Candidate brain URLs, tried in order with rotation on failure. Every
   * entry must be an exact-match domain listed in app.json — the Hub blocks
   * anything else. Multiple entries make the brain PORTABLE: run it on any
   * whitelisted Mac and the plugin finds it, no rebuild needed.
   */
  urls: readonly string[];
  maxQueue?: number;
  reconnectBaseMs?: number;
  reconnectMaxMs?: number;
}

/**
 * WebSocket client to the Mac brain.
 *
 * Handles reconnect with backoff and bounded queueing. Audio is a firehose,
 * so when the socket is down we drop the OLDEST frames — a stale buffer is
 * worse than a gap for live transcription.
 */
export class MacLink {
  private socket?: WebSocket;
  private queue: UplinkMessage[] = [];
  private attempt = 0;
  private urlIndex = 0;
  private closed = false;
  private handler?: (msg: DownlinkMessage) => void;
  private stateHandler?: (state: LinkState) => void;

  private readonly maxQueue: number;
  private readonly baseMs: number;
  private readonly capMs: number;

  constructor(private readonly opts: MacLinkOptions) {
    if (opts.urls.length === 0) throw new Error('MacLink needs at least one URL');
    this.maxQueue = opts.maxQueue ?? 200;
    this.baseMs = opts.reconnectBaseMs ?? 500;
    this.capMs = opts.reconnectMaxMs ?? 15_000;
  }

  /** The URL currently being dialed (for status surfaces). */
  get activeUrl(): string {
    return this.opts.urls[this.urlIndex % this.opts.urls.length] as string;
  }

  onMessage(handler: (msg: DownlinkMessage) => void): void {
    this.handler = handler;
  }

  onState(handler: (state: LinkState) => void): void {
    this.stateHandler = handler;
  }

  connect(): void {
    this.closed = false;
    this.stateHandler?.(this.attempt === 0 ? 'connecting' : 'reconnecting');
    const socket = new WebSocket(this.activeUrl);
    this.socket = socket;

    socket.onopen = () => {
      this.attempt = 0;
      this.stateHandler?.('connected');
      for (const msg of this.queue.splice(0)) this.rawSend(msg);
    };
    socket.onmessage = (event) => {
      try {
        this.handler?.(JSON.parse(String(event.data)) as DownlinkMessage);
      } catch {
        // Ignore malformed frames rather than killing the link.
      }
    };
    socket.onclose = () => {
      if (!this.closed) {
        this.stateHandler?.('reconnecting');
        this.scheduleReconnect();
      } else {
        this.stateHandler?.('closed');
      }
    };
    socket.onerror = () => socket.close();
  }

  send(msg: UplinkMessage): void {
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.rawSend(msg);
      return;
    }
    if (this.queue.length >= this.maxQueue) this.queue.shift();
    this.queue.push(msg);
  }

  close(): void {
    this.closed = true;
    this.socket?.close();
    this.queue = [];
  }

  private rawSend(msg: UplinkMessage): void {
    this.socket?.send(JSON.stringify(msg));
  }

  private scheduleReconnect(): void {
    // Rotate to the next candidate brain before retrying, so a dead primary
    // never blocks a live secondary. Backoff grows per full rotation, not per
    // attempt — trying 3 dead hosts shouldn't take 3x as long to give up on.
    this.urlIndex = (this.urlIndex + 1) % this.opts.urls.length;
    const round = Math.floor(this.attempt++ / this.opts.urls.length);
    const delay = Math.min(this.baseMs * 2 ** round, this.capMs);
    const jitter = delay * 0.2 * Math.random();
    setTimeout(() => { if (!this.closed) this.connect(); }, delay + jitter);
  }
}
