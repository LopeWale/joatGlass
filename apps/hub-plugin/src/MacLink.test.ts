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
});

describe('MacLink diagnostic delivery', () => {
  it('drops offline diagnostic bursts without evicting queued wearer input', () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    link.send({ kind: 'input', event: { eventType: 0 } });
    for (let frame = 1; frame <= 400; frame += 1) {
      link.sendDiagnostic({
        kind: 'g2.diagnostic',
        event: { type: 'tile_upload', frame, order: 0, containerId: 4, outcome: 'success' },
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
