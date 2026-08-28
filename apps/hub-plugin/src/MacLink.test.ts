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
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(url: string) {
    dialed.push(url);
    sockets.push(this);
  }
  send(_data: string): void {}
  close(): void {
    this.closed = true;
    this.onclose?.();
  }
  /** Simulate the server accepting this dial. */
  open(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }
}

(globalThis as { WebSocket?: unknown }).WebSocket = FakeWebSocket;

afterEach(() => {
  dialed.length = 0;
  sockets.length = 0;
  vi.useRealTimers();
});

describe('MacLink pairing token', () => {
  it('dials bare when unpaired — the TOFU window depends on it', () => {
    new MacLink({ urls: ['ws://mac.local:8787/glasses'] }).connect();
    expect(dialed).toEqual(['ws://mac.local:8787/glasses']);
  });

  it('appends ?token= when constructed with one (reconnect after pairing)', () => {
    new MacLink({ urls: ['ws://mac.local:8787/glasses'], token: 'abc123' }).connect();
    expect(dialed).toEqual(['ws://mac.local:8787/glasses?token=abc123']);
  });

  it('uses & when the candidate already carries a query', () => {
    new MacLink({ urls: ['ws://mac.local:8787/glasses?x=1'], token: 't' }).connect();
    expect(dialed).toEqual(['ws://mac.local:8787/glasses?x=1&token=t']);
  });

  it('URL-encodes the token — it rides a URL component', () => {
    new MacLink({ urls: ['ws://m:1/g'], token: 'a/b+c' }).connect();
    expect(dialed[0]).toBe('ws://m:1/g?token=a%2Fb%2Bc');
  });

  it('setToken applies from the next dial on (pairing.set mid-session)', () => {
    const link = new MacLink({ urls: ['ws://m:1/g'] });
    link.connect();
    link.setToken('fresh');
    link.connect();
    expect(dialed).toEqual(['ws://m:1/g', 'ws://m:1/g?token=fresh']);
  });

  it('carries the token across URL rotation (failover keeps auth)', () => {
    const link = new MacLink({ urls: ['ws://a:1/g', 'ws://b:1/g'], token: 't' });
    link.connect();
    // scheduleReconnect rotates urlIndex; simulate by closing the socket.
    (link as unknown as { urlIndex: number }).urlIndex = 1;
    link.connect();
    expect(dialed).toEqual(['ws://a:1/g?token=t', 'ws://b:1/g?token=t']);
  });

  it('keeps activeUrl clean for status surfaces — no token leak to the panel', () => {
    const link = new MacLink({ urls: ['ws://m:1/g'], token: 'secret' });
    link.connect();
    expect(link.activeUrl).toBe('ws://m:1/g');
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
    stale.onclose?.(); // the server dropped the older socket, late
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
    live.onclose?.(); // spurious repeats must not compound
    live.onclose?.();
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
   * A rejected token is indistinguishable from an unreachable Mac inside a
   * WebView — the socket just never opens, no 401 is visible. Without
   * self-healing the wearer stays locked out of their own brain until
   * someone clears localStorage by hand (which happened, twice).
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
      sockets[sockets.length - 1]!.close();
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
    sockets[0]!.close();                   // one failure
    vi.advanceTimersByTime(RETRY_MS);
    sockets[sockets.length - 1]!.open();    // Mac came back — count resets
    sockets[sockets.length - 1]!.close();   // and dropped again later
    vi.advanceTimersByTime(RETRY_MS);
    expect(forgotten).toBe(0); // intermittency is not staleness
  });
});
