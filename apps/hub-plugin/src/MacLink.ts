import type { DownlinkMessage, UplinkMessage } from './types';
import { PLUGIN_REPLACED_CLOSE_CODE } from './types';

/** Observable link lifecycle, for status surfaces (webview panel, HUD). */
export type LinkState = 'connecting' | 'connected' | 'reconnecting' | 'closed';

/**
 * Pairing credentials are scoped to a canonical WebSocket origin. Every
 * approved Mac brain maintains its own pairing store, so a credential issued
 * by one brain must never be offered to another.
 */
export type PairingTokensByOrigin = Readonly<Record<string, string>>;

/**
 * Lifetime of one socket dispatcher. Handlers must check the signal after
 * every awaited host/HUD operation before publishing follow-up state.
 */
export interface DownlinkContext {
  readonly generation: number;
  readonly signal: AbortSignal;
}

type DownlinkHandler = (
  msg: DownlinkMessage,
  context: DownlinkContext,
) => void | Promise<void>;

type NavigationUplinkMessage = Extract<UplinkMessage, {
  kind: 'navigation.action' | 'navigation.route.ack' | 'navigation.view.ack'
    | 'navigation.state' | 'navigation.reroute.request' | 'navigation.origin'
    | 'navigation.origin.failed';
}>;

type BrainstormUplinkMessage = Extract<UplinkMessage, { kind: 'brainstorm.action' }>;
type PhoneLocationUplink = Extract<UplinkMessage, { kind: 'location' }>;
type PromptBoundUplink = Extract<UplinkMessage, { kind: 'audio' | 'prompt.ack' | 'prompt.cancel' }>;

interface QueuedDownlink {
  message: DownlinkMessage;
  started: boolean;
}

interface DownlinkDispatcher {
  readonly socket: WebSocket;
  readonly generation: number;
  readonly controller: AbortController;
  tail: Promise<void>;
  deadline?: ReturnType<typeof setTimeout>;
  pendingAssistantAppend?: QueuedDownlink;
}

function canonicalOrigin(value: string): string | null {
  try {
    const parsed = new URL(value);
    return (parsed.protocol === 'ws:' || parsed.protocol === 'wss:')
      ? parsed.origin
      : null;
  } catch {
    return null;
  }
}

function isAssistantAppendWithText(
  message: DownlinkMessage,
): message is Extract<DownlinkMessage, { kind: 'assistant.overlay' }> & {
  operation: 'append';
  text: string;
} {
  return message.kind === 'assistant.overlay'
    && message.operation === 'append'
    && typeof message.text === 'string';
}

export interface MacLinkOptions {
  /**
   * Candidate brain URLs, tried in order with rotation on failure. Every
   * entry must be an exact-match domain listed in app.json — the Hub blocks
   * anything else. Multiple entries make the brain PORTABLE: run it on any
   * whitelisted Mac and the plugin finds it, no rebuild needed.
   */
  urls: readonly string[];
  /** Legacy one-brain pairing token, migrated to the first configured origin. */
  token?: string | null;
  /** Pairing tokens keyed by canonical origin for approved candidate brains. */
  tokens?: PairingTokensByOrigin;
  /** Exact approved candidate that most recently authenticated successfully. */
  preferredUrl?: string | null;
  maxQueue?: number;
  reconnectBaseMs?: number;
  reconnectMaxMs?: number;
  /** Give up on a dial that has not opened, and rotate. */
  dialTimeoutMs?: number;
  /** Abort and replace a connection whose downlink handler stops settling. */
  downlinkHandlerTimeoutMs?: number;
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
  /** Latest private phone fix, kept out of the audio backlog until auth. */
  private pendingLocation?: PhoneLocationUplink;
  private attempt = 0;
  private urlIndex = 0;
  private closed = false;
  private replaced = false;
  private handler?: DownlinkHandler;
  private ingressHandler?: (msg: DownlinkMessage, context: DownlinkContext) => void;
  private downlinkDispatcher?: DownlinkDispatcher;
  private nextDownlinkGeneration = 0;
  private stateHandler?: (state: LinkState) => void;
  private currentState: LinkState = 'connecting';
  private readonly tokensByOrigin = new Map<string, string>();
  private readonly failedTokensByOrigin = new Map<string, number>();
  private authenticated = false;

  private readonly maxQueue: number;
  private readonly baseMs: number;
  private readonly capMs: number;
  private readonly dialMs: number;
  private readonly downlinkMs: number;
  private dialTimer?: ReturnType<typeof setTimeout>;
  private reconnectTimer?: ReturnType<typeof setTimeout>;
  private staleTokenHandler?: (origin: string, tokens: PairingTokensByOrigin) => void;

  constructor(private readonly opts: MacLinkOptions) {
    if (opts.urls.length === 0) throw new Error('MacLink needs at least one URL');
    for (const [origin, token] of Object.entries(opts.tokens ?? {})) {
      if (canonicalOrigin(origin) === origin && token.length > 0) {
        this.tokensByOrigin.set(origin, token);
      }
    }
    // The old single-token representation could only have been used with
    // candidate zero. Retaining that interpretation is safe migration; it
    // never sprays a credential across the remaining approved brains.
    const initialOrigin = canonicalOrigin(opts.urls[0]!);
    if (opts.token && initialOrigin && !this.tokensByOrigin.has(initialOrigin)) {
      this.tokensByOrigin.set(initialOrigin, opts.token);
    }
    const preferredIndex = opts.preferredUrl === undefined || opts.preferredUrl === null
      ? -1
      : opts.urls.indexOf(opts.preferredUrl);
    if (preferredIndex >= 0) this.urlIndex = preferredIndex;
    this.maxQueue = opts.maxQueue ?? 200;
    this.baseMs = opts.reconnectBaseMs ?? 500;
    this.capMs = opts.reconnectMaxMs ?? 15_000;
    this.dialMs = opts.dialTimeoutMs ?? 4_000;
    this.downlinkMs = opts.downlinkHandlerTimeoutMs ?? 15_000;
  }

  /** The URL currently being dialed (for status surfaces). */
  get activeUrl(): string {
    return this.opts.urls[this.urlIndex % this.opts.urls.length] as string;
  }

  /** Explicit phone selection stays within the package's approved candidates.
   * Retire queued input before changing origins; credentials remain per-origin. */
  selectUrl(url: string): boolean {
    const index = this.opts.urls.indexOf(url);
    if (index < 0) return false;
    this.halt();
    this.urlIndex = index;
    this.attempt = 0;
    this.connect();
    return true;
  }

  /** A replaced display yields until its user explicitly reconnects it. */
  get wasReplaced(): boolean {
    return this.replaced;
  }

  /** The Even host can restore an earlier foreground WebView after migration. */
  resumeIfReplaced(): void {
    if (this.replaced) this.connect();
  }

  /** Canonical origin of the candidate currently being dialed. */
  get activeOrigin(): string {
    return canonicalOrigin(this.activeUrl) ?? '';
  }

  /** Credential for the current candidate only, never for another host. */
  get pairingToken(): string | null {
    return this.tokensByOrigin.get(this.activeOrigin) ?? null;
  }

  /** Snapshot suitable for durable storage; never include it in HUD/log text. */
  get pairingTokens(): PairingTokensByOrigin {
    return Object.fromEntries(this.tokensByOrigin);
  }

  /** Latest lifecycle truth, retained even when the HUD is still booting. */
  get linkState(): LinkState {
    return this.currentState;
  }

  /** Adopt a pairing token for the current approved origin only. */
  setToken(token: string): void {
    const origin = this.activeOrigin;
    if (!origin || !token) return;
    this.tokensByOrigin.set(origin, token);
    this.failedTokensByOrigin.delete(origin);
  }

  /** Drop only the active origin's unacknowledged pairing candidate. */
  clearToken(): void {
    const origin = this.activeOrigin;
    if (!origin) return;
    this.tokensByOrigin.delete(origin);
    this.failedTokensByOrigin.delete(origin);
  }

  /**
   * Fires when a held token has failed enough dials to be considered stale
   * (the Mac re-paired without us). The token is already dropped; the
   * handler's job is to forget it durably so a relaunch doesn't resurrect it.
   */
  onStaleToken(handler: (origin: string, tokens: PairingTokensByOrigin) => void): void {
    this.staleTokenHandler = handler;
  }

  onMessage(handler: DownlinkHandler): void {
    this.handler = handler;
  }

  /** Synchronous invalidation only; HUD work stays on the ordered handler. */
  onMessageIngress(handler: (msg: DownlinkMessage, context: DownlinkContext) => void): void {
    this.ingressHandler = handler;
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
    this.replaced = false;
    this.clearReconnectTimer();
    this.clearDialTimer();
    // Abandon any previous socket BEFORE dialing again. A socket we are
    // replacing must stop driving state: otherwise its close schedules
    // another dial that races this one, both open, the newest-wins server
    // drops the older, ITS close dials again — and the loop never settles.
    // Observed on real hardware as ~2 reconnects/second, indefinitely.
    this.abandon();
    this.authenticated = false;
    this.reportState(this.attempt === 0 ? 'connecting' : 'reconnecting');
    // Credentials never ride a request target: URL strings are copied into
    // browser history, proxy/access logs, diagnostics, and exception text.
    const socket = new WebSocket(this.activeUrl);
    const dispatcher: DownlinkDispatcher = {
      socket,
      generation: ++this.nextDownlinkGeneration,
      controller: new AbortController(),
      tail: Promise.resolve(),
    };
    this.downlinkDispatcher = dispatcher;
    this.socket = socket;

    // A host that DROPS packets (wrong subnet, asleep, AP isolation) never
    // errors — it hangs until the OS timeout, minutes later. With several
    // candidates that stalls the whole rotation behind one dead host, so
    // give up early and let the next candidate have its turn.
    this.clearDialTimer();
    this.dialTimer = setTimeout(() => {
      if (socket.readyState !== WebSocket.OPEN) this.expireSocket(socket);
    }, this.dialMs);

    socket.onopen = () => {
      if (this.socket !== socket) return; // superseded by a newer dial
      this.clearDialTimer();
      this.rawSend({ kind: 'auth', token: this.pairingToken });
      // TCP/WebSocket open is not application authentication. Bound the
      // second phase too, or a peer can leave the HUD connecting forever.
      this.dialTimer = setTimeout(() => {
        if (!this.authenticated) this.expireSocket(socket);
      }, this.dialMs);
    };
    socket.onmessage = (event) => {
      if (this.socket !== socket) return;
      try {
        const message = JSON.parse(String(event.data)) as DownlinkMessage;
        if (message.kind === 'auth.accepted') {
          if (!this.authenticated) {
            this.authenticated = true;
            this.clearDialTimer();
            this.attempt = 0;
            this.failedTokensByOrigin.delete(this.activeOrigin);
            this.reportState('connected');
            const location = this.pendingLocation;
            this.pendingLocation = undefined;
            if (location !== undefined) this.rawSend(location);
            for (const queued of this.queue.splice(0)) this.rawSend(queued);
          }
          return;
        }
        // A new client must store and ack pairing.set before the session is
        // authenticated. Every other pre-auth downlink is ignored.
        if (!this.authenticated && message.kind !== 'pairing.set') return;
        const context: DownlinkContext = {
          generation: dispatcher.generation,
          signal: dispatcher.controller.signal,
        };
        if (this.authenticated && !context.signal.aborted) {
          try {
            this.ingressHandler?.(message, context);
          } catch {
            // Invalidating one owner must not poison the ordered dispatcher.
          }
        }
        const handler = this.handler;
        if (handler !== undefined) {
          if (isAssistantAppendWithText(message)) {
            const pending = dispatcher.pendingAssistantAppend;
            if (
              pending !== undefined
              && !pending.started
              && isAssistantAppendWithText(pending.message)
            ) {
              pending.message = {
                ...pending.message,
                text: pending.message.text + message.text,
              };
              return;
            }
          }
          // Any non-coalesced frame is a wire-order boundary. In particular,
          // settle/dismiss must never overtake or absorb pending text.
          dispatcher.pendingAssistantAppend = undefined;
          const work: QueuedDownlink = { message, started: false };
          if (isAssistantAppendWithText(message)) {
            dispatcher.pendingAssistantAppend = work;
          }
          // WebSocket invokes callbacks synchronously, but HUD operations are
          // asynchronous. Preserve wire order so a rebuild cannot race the
          // status/text frames that follow it, and contain one rejected
          // render without poisoning later downlinks.
          dispatcher.tail = dispatcher.tail
            .then(async () => {
              work.started = true;
              if (dispatcher.pendingAssistantAppend === work) {
                dispatcher.pendingAssistantAppend = undefined;
              }
              if (
                this.socket !== socket
                || this.downlinkDispatcher !== dispatcher
                || context.signal.aborted
              ) return;
              dispatcher.deadline = setTimeout(() => {
                if (this.downlinkDispatcher !== dispatcher) return;
                this.expireSocket(socket);
              }, this.downlinkMs);
              try {
                await handler(work.message, context);
              } finally {
                if (dispatcher.deadline !== undefined) {
                  clearTimeout(dispatcher.deadline);
                  dispatcher.deadline = undefined;
                }
              }
            })
            .catch(() => undefined);
        }
      } catch {
        // Ignore malformed frames rather than killing the link.
      }
    };
    socket.onclose = (event) => {
      if (this.socket !== socket) return; // a superseded socket is not news
      this.clearDialTimer();
      this.revokeDownlinkDispatcher(socket);
      const wasAuthenticated = this.authenticated;
      this.authenticated = false;
      if (wasAuthenticated && event.code === PLUGIN_REPLACED_CLOSE_CODE) {
        this.replaced = true;
        this.pendingLocation = undefined;
        // halt() cancels all timers, revokes queued downlinks and notifies
        // the host to stop audio/input. Keep credentials for manual takeover.
        this.halt();
        return;
      }
      // Post-handshake auth gives an explicit close code. Never discard a
      // durable token for generic network failure, sleep, or a dead endpoint.
      const origin = this.activeOrigin;
      if (this.pairingToken !== null && !wasAuthenticated && event.code === 4401) {
        const failures = (this.failedTokensByOrigin.get(origin) ?? 0) + 1;
        this.failedTokensByOrigin.set(origin, failures);
        // This origin is reached once per complete candidate rotation, so two
        // explicit rejections are already two full passes. Count only its own
        // failures: a rejecting replacement brain must never erase a healthy
        // brain's credential.
        if (failures >= 2) {
          this.clearToken();
          this.staleTokenHandler?.(origin, this.pairingTokens);
        }
      }
      if (!this.closed) {
        this.scheduleReconnect();
        this.reportState('reconnecting');
      } else {
        this.reportState('closed');
      }
    };
    socket.onerror = () => socket.close();
  }

  send(msg: UplinkMessage): void {
    if (this.closed) return;
    if (
      this.socket?.readyState === WebSocket.OPEN
      && (this.authenticated || msg.kind === 'pairing')
    ) {
      this.rawSend(msg);
      return;
    }
    if (this.queue.length >= this.maxQueue) this.queue.shift();
    this.queue.push(msg);
  }

  /** Prompt receipts and audio belong to one authenticated dispatcher only. */
  sendForGeneration(msg: PromptBoundUplink, expectedGeneration: number): boolean {
    const socket = this.socket;
    const dispatcher = this.downlinkDispatcher;
    if (
      this.closed || !this.authenticated || socket?.readyState !== WebSocket.OPEN
      || dispatcher === undefined || dispatcher.socket !== socket
      || dispatcher.controller.signal.aborted
      || !Number.isSafeInteger(expectedGeneration) || expectedGeneration < 1
      || dispatcher.generation !== expectedGeneration
      || !['audio', 'prompt.ack', 'prompt.cancel'].includes(msg.kind)
    ) return false;
    try {
      const payload = JSON.stringify(msg);
      // Serialization can invoke user-supplied hooks. Never write to a
      // replacement connection if one ran while building this payload.
      if (
        this.socket !== socket || this.downlinkDispatcher !== dispatcher
        || dispatcher.controller.signal.aborted || !this.authenticated
        || socket.readyState !== WebSocket.OPEN
      ) return false;
      socket.send(payload);
      return true;
    } catch {
      // No replay queue, including when an open socket rejects a write.
      return false;
    }
  }

  /**
   * Preserve only the newest phone fix while a WebSocket authenticates.
   * Location is current context, not a replayable audio stream: placing it in
   * the bounded normal queue lets microphone frames evict the only startup
   * fix before the Mac can ask a nearby-place or navigation question.
   */
  sendLatestLocation(msg: PhoneLocationUplink): void {
    if (this.closed) return;
    if (this.socket?.readyState === WebSocket.OPEN && this.authenticated) {
      this.rawSend(msg);
      return;
    }
    this.pendingLocation = msg;
  }

  /**
   * Observational G2 support metadata is intentionally lossy. It must not
   * consume the normal queue while offline, where it could otherwise evict a
   * confirmation or wearer input before the next authenticated session.
   */
  sendDiagnostic(msg: Extract<UplinkMessage, { kind: 'g2.diagnostic' }>): void {
    if (this.socket?.readyState !== WebSocket.OPEN || !this.authenticated) return;
    try {
      this.rawSend(msg);
    } catch {
      // A socket can close after the readiness check. Trace loss is safe.
    }
  }

  /**
   * Navigation messages are valid only for the currently authenticated Mac
   * session. Never queue them across reconnects: origin/reroute can contain
   * coordinates, and even content-free receipts are generation-bound.
   */
  sendNavigation(msg: NavigationUplinkMessage): boolean {
    if (this.socket?.readyState !== WebSocket.OPEN || !this.authenticated) return false;
    try {
      this.rawSend(msg);
      return true;
    } catch {
      return false;
    }
  }

  /**
   * Brainstorm gestures are session-control intents, not durable commands.
   * Dropping one during a reconnect is safer than replaying a stale cancel or
   * retry against the replacement Mac session.
   */
  sendBrainstorm(msg: BrainstormUplinkMessage): boolean {
    if (this.socket?.readyState !== WebSocket.OPEN || !this.authenticated) return false;
    try {
      this.rawSend(msg);
      return true;
    } catch {
      return false;
    }
  }

  /** Conversation choices are bound to pixels from one authenticated Mac
   * session. Replaying Exit or Continue after reconnect could target a
   * successor conversation, so these actions are deliberately lossy. */
  sendConversationAction(msg: Extract<UplinkMessage, { kind: 'input' }>): boolean {
    const event = msg.event;
    if (
      event.containerName !== 'assistant-conversation'
      || (event.selectedName !== 'continue' && event.selectedName !== 'exit')
      || typeof event.conversationId !== 'string'
      || !/^[A-Za-z0-9_-]{1,64}$/.test(event.conversationId)
      || !Number.isSafeInteger(event.conversationRevision)
      || (event.conversationRevision ?? -1) < 0
      || this.socket?.readyState !== WebSocket.OPEN
      || !this.authenticated
    ) return false;
    try {
      this.rawSend(msg);
      return true;
    } catch {
      return false;
    }
  }

  close(): void {
    this.replaced = false;
    this.halt();
  }

  private halt(): void {
    this.closed = true;
    this.authenticated = false;
    this.clearReconnectTimer();
    this.clearDialTimer();
    this.abandon();
    this.queue = [];
    this.pendingLocation = undefined;
    this.reportState('closed');
  }

  /** Detach and close the current socket so it can no longer drive state. */
  private abandon(): void {
    const prev = this.socket;
    if (prev === undefined) return;
    this.revokeDownlinkDispatcher(prev);
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

  private revokeDownlinkDispatcher(socket: WebSocket): void {
    const dispatcher = this.downlinkDispatcher;
    if (dispatcher?.socket !== socket) return;
    this.downlinkDispatcher = undefined;
    if (dispatcher.deadline !== undefined) {
      clearTimeout(dispatcher.deadline);
      dispatcher.deadline = undefined;
    }
    dispatcher.controller.abort();
  }

  private clearDialTimer(): void {
    if (this.dialTimer !== undefined) {
      clearTimeout(this.dialTimer);
      this.dialTimer = undefined;
    }
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer !== undefined) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = undefined;
    }
  }

  /** A local deadline must not wait for the browser's asynchronous close. */
  private expireSocket(socket: WebSocket): void {
    if (this.socket !== socket || this.closed) return;
    this.clearDialTimer();
    this.authenticated = false;
    this.abandon();
    this.scheduleReconnect();
    this.reportState('reconnecting');
  }

  private rawSend(msg: UplinkMessage): void {
    this.socket?.send(JSON.stringify(msg));
  }

  private scheduleReconnect(): void {
    // At most ONE dial in flight. Without this, every stray close adds
    // another pending connect and they compound.
    if (this.reconnectTimer !== undefined) return;
    // Rotate to the next candidate brain before retrying, so a dead primary
    // never blocks a live secondary. Backoff grows per full rotation, not per
    // attempt — trying 3 dead hosts shouldn't take 3x as long to give up on.
    this.urlIndex = (this.urlIndex + 1) % this.opts.urls.length;
    const round = Math.floor(this.attempt++ / this.opts.urls.length);
    const delay = Math.min(this.baseMs * 2 ** round, this.capMs);
    const jitter = delay * 0.2 * Math.random();
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = undefined;
      if (!this.closed) this.connect();
    }, delay + jitter);
  }
}
