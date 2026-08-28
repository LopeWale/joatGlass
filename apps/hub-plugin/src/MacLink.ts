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
  /** Pairing token (from a previous pairing.set), appended as ?token= on dial. */
  token?: string | null;
  maxQueue?: number;
  reconnectBaseMs?: number;
  reconnectMaxMs?: number;
  /** Give up on a dial that has not opened, and rotate. */
  dialTimeoutMs?: number;
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
  private currentState: LinkState = 'connecting';
  private token: string | null;

  private readonly maxQueue: number;
  private readonly baseMs: number;
  private readonly capMs: number;
  private readonly dialMs: number;
  private dialTimer?: ReturnType<typeof setTimeout>;
  private reconnectPending = false;
  private failedWithToken = 0;
  private staleTokenHandler?: () => void;

  constructor(private readonly opts: MacLinkOptions) {
    if (opts.urls.length === 0) throw new Error('MacLink needs at least one URL');
    this.token = opts.token ?? null;
    this.maxQueue = opts.maxQueue ?? 200;
    this.baseMs = opts.reconnectBaseMs ?? 500;
    this.capMs = opts.reconnectMaxMs ?? 15_000;
    this.dialMs = opts.dialTimeoutMs ?? 4_000;
  }

  /** The URL currently being dialed (for status surfaces). */
  get activeUrl(): string {
    return this.opts.urls[this.urlIndex % this.opts.urls.length] as string;
  }

  /** Latest lifecycle truth, retained even when the HUD is still booting. */
  get linkState(): LinkState {
    return this.currentState;
  }

  /** Adopt a pairing token mid-session (pairing.set); used from the next dial on. */
  setToken(token: string): void {
    this.token = token;
    this.failedWithToken = 0;
  }

  /**
   * Fires when a held token has failed enough dials to be considered stale
   * (the Mac re-paired without us). The token is already dropped; the
   * handler's job is to forget it durably so a relaunch doesn't resurrect it.
   */
  onStaleToken(handler: () => void): void {
    this.staleTokenHandler = handler;
  }

  onMessage(handler: (msg: DownlinkMessage) => void): void {
    this.handler = handler;
  }

  onState(handler: (state: LinkState) => void): void {
    this.stateHandler = handler;
  }

  private reportState(state: LinkState): void {
    this.currentState = state;
    this.stateHandler?.(state);
  }

  connect(): void {
    this.closed = false;
    // Abandon any previous socket BEFORE dialing again. A socket we are
    // replacing must stop driving state: otherwise its close schedules
    // another dial that races this one, both open, the newest-wins server
    // drops the older, ITS close dials again — and the loop never settles.
    // Observed on real hardware as ~2 reconnects/second, indefinitely.
    this.abandon();
    this.reportState(this.attempt === 0 ? 'connecting' : 'reconnecting');
    // Token rides the URL: the gateway authorizes BEFORE the WebSocket
    // handshake, and a WebView WebSocket cannot set headers.
    const url = this.token
      ? `${this.activeUrl}${this.activeUrl.includes('?') ? '&' : '?'}token=${encodeURIComponent(this.token)}`
      : this.activeUrl;
    const socket = new WebSocket(url);
    this.socket = socket;

    // A host that DROPS packets (wrong subnet, asleep, AP isolation) never
    // errors — it hangs until the OS timeout, minutes later. With several
    // candidates that stalls the whole rotation behind one dead host, so
    // give up early and let the next candidate have its turn.
    this.clearDialTimer();
    this.dialTimer = setTimeout(() => {
      if (socket.readyState !== WebSocket.OPEN) socket.close();
    }, this.dialMs);

    socket.onopen = () => {
      if (this.socket !== socket) return; // superseded by a newer dial
      this.clearDialTimer();
      this.attempt = 0;
      this.failedWithToken = 0;
      this.reportState('connected');
      for (const msg of this.queue.splice(0)) this.rawSend(msg);
    };
    socket.onmessage = (event) => {
      if (this.socket !== socket) return;
      try {
        this.handler?.(JSON.parse(String(event.data)) as DownlinkMessage);
      } catch {
        // Ignore malformed frames rather than killing the link.
      }
    };
    socket.onclose = () => {
      if (this.socket !== socket) return; // a superseded socket is not news
      this.clearDialTimer();
      // A rejected token looks EXACTLY like an unreachable Mac from here —
      // a WebView socket never sees the 401. So if a held token has failed
      // a full rotation and then some, assume the Mac re-paired without us
      // and dial bare: the Mac only ever offers a token while unpaired, so
      // this re-pairs instead of leaving the wearer locked out forever.
      if (this.token !== null && socket.readyState !== WebSocket.OPEN) {
        this.failedWithToken += 1;
        if (this.failedWithToken >= this.opts.urls.length * 2) {
          this.token = null;
          this.failedWithToken = 0;
          this.staleTokenHandler?.();
        }
      }
      if (!this.closed) {
        this.reportState('reconnecting');
        this.scheduleReconnect();
      } else {
        this.reportState('closed');
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
    this.clearDialTimer();
    this.abandon();
    this.queue = [];
    this.reportState('closed');
  }

  /** Detach and close the current socket so it can no longer drive state. */
  private abandon(): void {
    const prev = this.socket;
    if (prev === undefined) return;
    this.socket = undefined;
    prev.onopen = null;
    prev.onmessage = null;
    prev.onclose = null;
    prev.onerror = null;
    try {
      prev.close();
    } catch {
      // Already closing/closed — nothing to do.
    }
  }

  private clearDialTimer(): void {
    if (this.dialTimer !== undefined) {
      clearTimeout(this.dialTimer);
      this.dialTimer = undefined;
    }
  }

  private rawSend(msg: UplinkMessage): void {
    this.socket?.send(JSON.stringify(msg));
  }

  private scheduleReconnect(): void {
    // At most ONE dial in flight. Without this, every stray close adds
    // another pending connect and they compound.
    if (this.reconnectPending) return;
    this.reconnectPending = true;
    // Rotate to the next candidate brain before retrying, so a dead primary
    // never blocks a live secondary. Backoff grows per full rotation, not per
    // attempt — trying 3 dead hosts shouldn't take 3x as long to give up on.
    this.urlIndex = (this.urlIndex + 1) % this.opts.urls.length;
    const round = Math.floor(this.attempt++ / this.opts.urls.length);
    const delay = Math.min(this.baseMs * 2 ** round, this.capMs);
    const jitter = delay * 0.2 * Math.random();
    setTimeout(() => {
      this.reconnectPending = false;
      if (!this.closed) this.connect();
    }, delay + jitter);
  }
}
