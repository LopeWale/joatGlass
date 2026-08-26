import { afterEach, describe, expect, it } from 'vitest';
import { MacLink } from './MacLink';

/**
 * Stub WebSocket capturing every constructor URL — vitest runs in node.
 * Dial-time behaviour is all these tests need; no frames ever flow.
 */
const dialed: string[] = [];

class FakeWebSocket {
  static readonly OPEN = 1;
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(url: string) {
    dialed.push(url);
  }
  send(_data: string): void {}
  close(): void {}
}

(globalThis as { WebSocket?: unknown }).WebSocket = FakeWebSocket;

afterEach(() => {
  dialed.length = 0;
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

  it('keeps activeUrl clean for status surfaces — no token leak to the panel', () => {
    const link = new MacLink({ urls: ['ws://m:1/g'], token: 'secret' });
    link.connect();
    expect(link.activeUrl).toBe('ws://m:1/g');
  });
});
