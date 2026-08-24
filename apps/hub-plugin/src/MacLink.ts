import type { DownlinkMessage, UplinkMessage } from './types';

/** Observable link lifecycle, for status surfaces (webview panel, HUD). */
export type LinkState = 'connecting' | 'connected' | 'reconnecting' | 'closed';

export interface MacLinkOptions {
  /** Must be an exact-match domain listed in app.json — the Hub blocks anything else. */
  url: string;
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
  private closed = false;
  private handler?: (msg: DownlinkMessage) => void;
  private stateHandler?: (state: LinkState) => void;

  private readonly maxQueue: number;
  private readonly baseMs: number;
  private readonly capMs: number;

  constructor(private readonly opts: MacLinkOptions) {
    this.maxQueue = opts.maxQueue ?? 200;
    this.baseMs = opts.reconnectBaseMs ?? 500;
    this.capMs = opts.reconnectMaxMs ?? 15_000;
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
    const socket = new WebSocket(this.opts.url);
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
    const delay = Math.min(this.baseMs * 2 ** this.attempt++, this.capMs);
    const jitter = delay * 0.2 * Math.random();
    setTimeout(() => { if (!this.closed) this.connect(); }, delay + jitter);
  }
}
