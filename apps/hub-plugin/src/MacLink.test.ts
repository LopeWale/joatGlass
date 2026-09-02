import { afterEach, describe, expect, it, vi } from 'vitest';
import { MacLink } from './MacLink';

/**
 * Stub WebSocket capturing every constructor URL — vitest runs in node.
 * Dial-time behaviour is all these tests need; no frames ever flow.
 */
const dialed: string[] = [];

const sockets: FakeWebSocket[] = [];

class FakeWebSocket {
  static readonly OPEN = 1;
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  sent: string[] = [];
  constructor(url: string) {
    dialed.push(url);
    sockets.push(this);
  }
  send(data: string): void { this.sent.push(data); }
  close(code = 1006): void {
    this.closed = true;
    this.readyState = 3;
    this.onclose?.({ code } as never);
  }
  /** Simulate the server accepting this dial. */
  open(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }
  receive(message: unknown): void {
    this.onmessage?.({ data: JSON.stringify(message) });
  }
}

(globalThis as { WebSocket?: unknown }).WebSocket = FakeWebSocket;

afterEach(() => {
  dialed.length = 0;
  sockets.length = 0;
  vi.useRealTimers();
});

describe('MacLink pairing token', () => {
  it('dials bare when unpaired — auth belongs in the first frame', () => {
    new MacLink({ urls: ['ws://mac.local:8787/glasses'] }).connect();
    expect(dialed).toEqual(['ws://mac.local:8787/glasses']);
  });

  it('never puts a credential in the WebSocket URL', () => {
    new MacLink({ urls: ['ws://mac.local:8787/glasses'], token: 'abc123' }).connect();
    expect(dialed).toEqual(['ws://mac.local:8787/glasses']);
  });

  it('sends the held credential as the first application frame', () => {
    new MacLink({ urls: ['ws://m:1/g'], token: 'a/b+c' }).connect();
    sockets[0]!.open();
    expect(sockets[0]!.sent).toEqual([JSON.stringify({ kind: 'auth', token: 'a/b+c' })]);
  });

  it('sends an explicit null credential when unpaired', () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    link.connect();
    sockets[0]!.open();
    expect(sockets[0]!.sent).toEqual([JSON.stringify({ kind: 'auth', token: null })]);
  });

  it('does not report connected or flush uplinks before auth is accepted', () => {
    const link = new MacLink({ urls: ['ws://m:1/g'], token: 't' });
    link.send({ kind: 'policy', resume: 'auto' });
    link.connect();
    sockets[0]!.open();
    expect(link.linkState).toBe('connecting');
    expect(sockets[0]!.sent).toEqual([JSON.stringify({ kind: 'auth', token: 't' })]);
    sockets[0]!.receive({ kind: 'auth.accepted' });
    expect(link.linkState).toBe('connected');
    expect(sockets[0]!.sent).toEqual([
      JSON.stringify({ kind: 'auth', token: 't' }),
      JSON.stringify({ kind: 'policy', resume: 'auto' }),
    ]);
  });

  it('keeps activeUrl clean for status surfaces — no token leak to the panel', () => {
    const link = new MacLink({ urls: ['ws://m:1/g'], token: 'secret' });
    link.connect();
    expect(link.activeUrl).toBe('ws://m:1/g');
  });

  it('uses the pairing token belonging to each approved brain, never a global token', () => {
    vi.useFakeTimers();
    const link = new MacLink({
      urls: ['ws://a:1/g', 'ws://b:1/g'],
      tokens: { 'ws://a:1': 'token-a', 'ws://b:1': 'token-b' },
      reconnectBaseMs: 10,
      reconnectMaxMs: 10,
    });
    link.connect();
    sockets[0]!.open();
    expect(sockets[0]!.sent).toEqual([JSON.stringify({ kind: 'auth', token: 'token-a' })]);

    sockets[0]!.close();
    vi.advanceTimersByTime(20);
    sockets[1]!.open();
    expect(sockets[1]!.sent).toEqual([JSON.stringify({ kind: 'auth', token: 'token-b' })]);
  });

  it('starts at a stored healthy approved brain rather than always candidate zero', () => {
    const link = new MacLink({
      urls: ['wss://a:443/glasses', 'wss://b:443/glasses'],
      preferredUrl: 'wss://b:443/glasses',
      tokens: { 'wss://b': 'token-b' },
    });
    link.connect();

    expect(dialed).toEqual(['wss://b:443/glasses']);
    sockets[0]!.open();
    expect(sockets[0]!.sent).toEqual([JSON.stringify({ kind: 'auth', token: 'token-b' })]);
  });

  it('forgets only the Mac that explicitly rejects its own token', () => {
    vi.useFakeTimers();
    const link = new MacLink({
      urls: ['ws://a:1/g', 'ws://b:1/g'],
      tokens: { 'ws://a:1': 'token-a', 'ws://b:1': 'token-b' },
      reconnectBaseMs: 10,
      reconnectMaxMs: 10,
    });
    const stale: Array<{ origin: string; tokens: Record<string, string> }> = [];
    link.onStaleToken((origin, tokens) => stale.push({ origin, tokens: { ...tokens } }));

    link.connect();
    sockets[0]!.open();
    sockets[0]!.close(4401); // first explicit rejection at A
    vi.advanceTimersByTime(30);

    sockets[1]!.open();
    sockets[1]!.receive({ kind: 'auth.accepted' }); // B remains independently healthy
    sockets[1]!.close(1006);
    vi.advanceTimersByTime(30);

    sockets[2]!.open();
    sockets[2]!.close(4401); // second complete rotation reaches A again

    expect(link.pairingTokens).toEqual({ 'ws://b:1': 'token-b' });
    expect(stale).toEqual([{
      origin: 'ws://a:1',
      tokens: { 'ws://b:1': 'token-b' },
    }]);
  });
});

describe('MacLink diagnostic delivery', () => {
  it('drops offline diagnostic bursts without evicting queued wearer input', () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    link.send({ kind: 'input', event: { eventType: 0 } });
    for (let frame = 1; frame <= 400; frame += 1) {
      link.sendDiagnostic({
        kind: 'g2.diagnostic',
        event: {
          type: 'tile_upload', frame, order: 0, containerId: 4, outcome: 'success',
          durationMs: 900, bytes: 4096,
        },
      });
    }

    link.connect();
    sockets[0]!.open();
    sockets[0]!.receive({ kind: 'auth.accepted' });

    expect(sockets[0]!.sent).toEqual([
      JSON.stringify({ kind: 'auth', token: null }),
      JSON.stringify({ kind: 'input', event: { eventType: 0 } }),
    ]);
  });

  it('sends the latest pending phone fix ahead of offline audio after authentication', () => {
    const link = new MacLink({ urls: ['ws://m:1/g'], maxQueue: 2 });
    link.send({ kind: 'audio', source: 'glasses', seq: 1, pcm: [1] });
    link.sendLatestLocation({
      kind: 'location', latitude: 41.9, longitude: -87.65, timestamp: 1_725_000_000_123,
    });
    link.send({ kind: 'audio', source: 'glasses', seq: 2, pcm: [2] });
    link.send({ kind: 'audio', source: 'glasses', seq: 3, pcm: [3] });

    link.connect();
    sockets[0]!.open();
    sockets[0]!.receive({ kind: 'auth.accepted' });

    expect(sockets[0]!.sent).toEqual([
      JSON.stringify({ kind: 'auth', token: null }),
      JSON.stringify({
        kind: 'location', latitude: 41.9, longitude: -87.65, timestamp: 1_725_000_000_123,
      }),
      JSON.stringify({ kind: 'audio', source: 'glasses', seq: 2, pcm: [2] }),
      JSON.stringify({ kind: 'audio', source: 'glasses', seq: 3, pcm: [3] }),
    ]);
  });
});

describe('MacLink navigation delivery', () => {
  it('drops session-bound navigation uplinks instead of crossing reconnects', () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    link.sendNavigation({
      kind: 'navigation.origin', sessionId: 'nav-old', requestId: 'origin-old',
      generation: 1, latitude: 41.9, longitude: -87.65,
    });

    link.connect();
    sockets[0]!.open();
    sockets[0]!.receive({ kind: 'auth.accepted' });

    expect(sockets[0]!.sent).toEqual([
      JSON.stringify({ kind: 'auth', token: null }),
    ]);
  });

  it('sends session-bound navigation uplinks only on the authenticated socket', () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    link.connect();
    sockets[0]!.open();
    sockets[0]!.receive({ kind: 'auth.accepted' });
    link.sendNavigation({
      kind: 'navigation.view.ack', sessionId: 'nav-1', requestId: 'mode-1',
      generation: 1, outcome: 'bridge_acknowledged',
    });

    expect(sockets[0]!.sent).toEqual([
      JSON.stringify({ kind: 'auth', token: null }),
      JSON.stringify({
        kind: 'navigation.view.ack', sessionId: 'nav-1', requestId: 'mode-1',
        generation: 1, outcome: 'bridge_acknowledged',
      }),
    ]);
  });
});

describe('MacLink downlink ordering', () => {
  it('coalesces adjacent queued assistant appends before the HUD renders them', async () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    const handled: Array<{ operation: string; text?: string }> = [];
    let releaseReplace!: () => void;
    const replaceBlocked = new Promise<void>((resolve) => { releaseReplace = resolve; });
    link.onMessage(async (message) => {
      if (message.kind !== 'assistant.overlay') return;
      handled.push({ operation: message.operation, text: message.text });
      if (message.operation === 'replace') await replaceBlocked;
    });
    link.connect();
    sockets[0]!.open();
    sockets[0]!.receive({ kind: 'auth.accepted' });

    sockets[0]!.receive({ kind: 'assistant.overlay', operation: 'replace', text: 'Hello' });
    await Promise.resolve();
    expect(handled).toEqual([{ operation: 'replace', text: 'Hello' }]);

    sockets[0]!.receive({ kind: 'assistant.overlay', operation: 'append', text: ' one' });
    sockets[0]!.receive({ kind: 'assistant.overlay', operation: 'append', text: ' two' });
    sockets[0]!.receive({ kind: 'assistant.overlay', operation: 'append', text: ' three' });
    sockets[0]!.receive({
      kind: 'assistant.overlay', operation: 'settle', dismissAfterMs: 6_000,
    });

    releaseReplace();
    await replaceBlocked;
    await vi.waitFor(() => {
      expect(handled).toEqual([
        { operation: 'replace', text: 'Hello' },
        { operation: 'append', text: ' one two three' },
        { operation: 'settle', text: undefined },
      ]);
    });
  });

  it('awaits each async HUD handler before dispatching the next frame', async () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    const order: string[] = [];
    let releaseRebuild!: () => void;
    const rebuildBlocked = new Promise<void>((resolve) => { releaseRebuild = resolve; });
    link.onMessage(async (message) => {
      order.push(`start:${message.kind}`);
      if (message.kind === 'hud.rebuild') await rebuildBlocked;
      order.push(`end:${message.kind}`);
    });
    link.connect();
    sockets[0]!.open();
    sockets[0]!.receive({ kind: 'auth.accepted' });

    sockets[0]!.receive({ kind: 'hud.rebuild', reason: 'brainstorm' });
    sockets[0]!.receive({ kind: 'hud.text', containerId: 4, text: 'first turn' });
    await Promise.resolve();
    expect(order).toEqual(['start:hud.rebuild']);

    releaseRebuild();
    await rebuildBlocked;
    await vi.waitFor(() => {
      expect(order).toEqual([
        'start:hud.rebuild',
        'end:hud.rebuild',
        'start:hud.text',
        'end:hud.text',
      ]);
    });
  });

  it('contains a rejected handler and continues with later frames', async () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    const handled: string[] = [];
    link.onMessage(async (message) => {
      if (message.kind === 'hud.rebuild') throw new Error('render failed');
      handled.push(message.kind);
    });
    link.connect();
    sockets[0]!.open();
    sockets[0]!.receive({ kind: 'auth.accepted' });

    sockets[0]!.receive({ kind: 'hud.rebuild', reason: 'brainstorm' });
    sockets[0]!.receive({ kind: 'hud.text', containerId: 4, text: 'still handled' });
    await vi.waitFor(() => expect(handled).toEqual(['hud.text']));
  });

  it('a hung old connection does not wedge downlinks after reconnect', async () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    const handled: string[] = [];
    const neverFinishes = new Promise<void>(() => {});
    link.onMessage(async (message) => {
      if (message.kind !== 'hud.text') return;
      if (message.text === 'old') await neverFinishes;
      handled.push(message.text);
    });
    link.connect();
    sockets[0]!.open();
    sockets[0]!.receive({ kind: 'auth.accepted' });
    sockets[0]!.receive({ kind: 'hud.text', containerId: 4, text: 'old' });
    await Promise.resolve();

    link.connect();
    sockets[1]!.open();
    sockets[1]!.receive({ kind: 'auth.accepted' });
    sockets[1]!.receive({ kind: 'hud.text', containerId: 4, text: 'new' });

    await vi.waitFor(() => expect(handled).toEqual(['new']));
  });

  it('aborts an in-flight generation before it can commit after reconnect', async () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    const commits: string[] = [];
    const contexts: Array<{ generation: number; signal: AbortSignal }> = [];
    let releaseOld!: () => void;
    const oldBlocked = new Promise<void>((resolve) => { releaseOld = resolve; });
    link.onMessage(async (message, context) => {
      contexts.push(context);
      if (message.kind === 'hud.text' && message.text === 'old') await oldBlocked;
      context.signal.throwIfAborted();
      commits.push(message.kind === 'hud.text' ? message.text : message.kind);
    });
    link.connect();
    sockets[0]!.open();
    sockets[0]!.receive({ kind: 'auth.accepted' });
    sockets[0]!.receive({ kind: 'hud.text', containerId: 4, text: 'old' });
    await Promise.resolve();

    link.connect();
    sockets[1]!.open();
    sockets[1]!.receive({ kind: 'auth.accepted' });
    sockets[1]!.receive({ kind: 'hud.text', containerId: 4, text: 'new' });
    await vi.waitFor(() => expect(commits).toEqual(['new']));

    releaseOld();
    await oldBlocked;
    await Promise.resolve();
    expect(commits).toEqual(['new']);
    expect(contexts).toHaveLength(2);
    expect(contexts[0]!.signal.aborted).toBe(true);
    expect(contexts[1]!.signal.aborted).toBe(false);
    expect(contexts[1]!.generation).toBeGreaterThan(contexts[0]!.generation);
  });

  it('never starts queued work from an abandoned generation', async () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    const started: string[] = [];
    let releaseOld!: () => void;
    const oldBlocked = new Promise<void>((resolve) => { releaseOld = resolve; });
    link.onMessage(async (message) => {
      if (message.kind !== 'hud.text') return;
      started.push(message.text);
      if (message.text === 'old-running') await oldBlocked;
    });
    link.connect();
    sockets[0]!.open();
    sockets[0]!.receive({ kind: 'auth.accepted' });
    sockets[0]!.receive({ kind: 'hud.text', containerId: 4, text: 'old-running' });
    sockets[0]!.receive({ kind: 'hud.text', containerId: 4, text: 'old-queued' });
    await Promise.resolve();

    link.connect();
    sockets[1]!.open();
    sockets[1]!.receive({ kind: 'auth.accepted' });
    sockets[1]!.receive({ kind: 'hud.text', containerId: 4, text: 'new' });
    await vi.waitFor(() => expect(started).toEqual(['old-running', 'new']));

    releaseOld();
    await oldBlocked;
    await Promise.resolve();
    await Promise.resolve();
    expect(started).toEqual(['old-running', 'new']);
  });

  it('expires a hung handler by aborting its generation and reconnecting', async () => {
    vi.useFakeTimers();
    const link = new MacLink({
      urls: ['ws://m:1/g'],
      reconnectBaseMs: 10,
      reconnectMaxMs: 10,
      downlinkHandlerTimeoutMs: 50,
    });
    let signal: AbortSignal | undefined;
    link.onMessage(async (_message, context) => {
      signal = context.signal;
      await new Promise<void>(() => {});
    });
    link.connect();
    const first = sockets[0]!;
    first.open();
    first.receive({ kind: 'auth.accepted' });
    first.receive({ kind: 'hud.text', containerId: 4, text: 'wedged' });
    await Promise.resolve();

    vi.advanceTimersByTime(50);
    expect(signal?.aborted).toBe(true);
    expect(first.closed).toBe(true);

    vi.advanceTimersByTime(20);
    expect(sockets).toHaveLength(2);
  });
});


describe('MacLink reconnect storm', () => {
  /**
   * Observed on real G2 hardware: ~2 reconnects/second, indefinitely. A
   * superseded socket kept its handlers, so its close scheduled ANOTHER
   * dial that raced the current one; both opened, the newest-wins server
   * dropped the older, and its close dialed again. The loop compounds.
   *
   * Retrying forever while down is CORRECT, so these assert the invariant
   * that actually broke: one drop must produce exactly one dial.
   */
  const SETTLE_MS = 1_000; // base backoff 500ms + up to 20% jitter

  it('retains a fast connection state for a HUD observer created later', () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    link.connect();
    sockets[0]!.open();
    sockets[0]!.receive({ kind: 'auth.accepted' });
    expect(link.linkState).toBe('connected');
  });

  it('abandons the previous socket when dialing again', () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    link.connect();
    const first = sockets[0]!;
    link.connect();
    expect(first.closed).toBe(true);
    expect(first.onclose).toBeNull(); // detached: cannot schedule a dial
  });

  it('a superseded socket closing does NOT schedule a reconnect', () => {
    vi.useFakeTimers();
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    link.connect();
    const stale = sockets[0]!;
    link.connect();
    const before = dialed.length;
    stale.onclose?.({ code: 1006 }); // the server dropped the older socket, late
    vi.advanceTimersByTime(SETTLE_MS);
    expect(dialed.length).toBe(before); // no dial from a socket we replaced
  });

  it('coalesces repeated closes of one socket into a single dial', () => {
    vi.useFakeTimers();
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    link.connect();
    const before = dialed.length;
    const live = sockets[0]!;
    live.close();
    live.onclose?.({ code: 1006 }); // spurious repeats must not compound
    live.onclose?.({ code: 1006 });
    vi.advanceTimersByTime(SETTLE_MS);
    expect(dialed.length).toBe(before + 1);
  });

  it('an opened socket still reconnects exactly once when it drops', () => {
    vi.useFakeTimers();
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    link.connect();
    const before = dialed.length;
    sockets[0]!.open();
    sockets[0]!.close();
    vi.advanceTimersByTime(SETTLE_MS);
    expect(dialed.length).toBe(before + 1);
  });

  it('the dial watchdog retries a hung dial, but only one at a time', () => {
    vi.useFakeTimers();
    const link = new MacLink({ urls: ['ws://m:1/g'], dialTimeoutMs: 100 });
    link.connect();
    const before = dialed.length;
    // Socket never opens: watchdog closes it, which schedules one retry.
    vi.advanceTimersByTime(100 + SETTLE_MS);
    expect(dialed.length).toBe(before + 1);
  });
});

describe('MacLink stale pairing token', () => {
  /**
   * Application auth closes with 4401 after the WebSocket opens. Generic
   * network failures must never be mistaken for stale credentials.
   */
  // Long enough for one backoff (500ms + jitter), short enough that the 4s
  // dial watchdog does not fire and count a failure of its own.
  const RETRY_MS = 1_000;

  it('drops a token that keeps failing, and reports it once', () => {
    vi.useFakeTimers();
    const link = new MacLink({ urls: ['ws://a:1/g', 'ws://b:1/g'], token: 'stale' });
    let forgotten = 0;
    link.onStaleToken(() => { forgotten += 1; });
    link.connect();
    for (let i = 0; i < 5; i += 1) {
      sockets[sockets.length - 1]!.open();
      sockets[sockets.length - 1]!.close(4401);
      vi.advanceTimersByTime(RETRY_MS);
    }
    expect(forgotten).toBe(1);
    expect(dialed[dialed.length - 1]).not.toContain('token=');
  });

  it('a successful connect resets the failure count', () => {
    vi.useFakeTimers();
    const link = new MacLink({ urls: ['ws://a:1/g'], token: 't' });
    let forgotten = 0;
    link.onStaleToken(() => { forgotten += 1; });
    link.connect();
    sockets[0]!.open();
    sockets[0]!.close(4401);               // one explicit auth failure
    vi.advanceTimersByTime(RETRY_MS);
    sockets[sockets.length - 1]!.open();
    sockets[sockets.length - 1]!.receive({ kind: 'auth.accepted' }); // resets
    sockets[sockets.length - 1]!.close();   // and dropped again later
    vi.advanceTimersByTime(RETRY_MS);
    expect(forgotten).toBe(0); // intermittency is not staleness
  });
});
