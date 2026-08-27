import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { TOKEN_KEY, handlePairingOffer } from './Pairing';
import type { MacLink } from './MacLink';

/**
 * The absorbing-state bug both audit passes confirmed: an offer of the very
 * token the phone already holds was DROPPED, so a lost ack (or an operator
 * deleting pairing.json) left the Mac's window open to the whole LAN
 * forever, with a manual storage wipe as the only exit. These tests pin
 * every branch of the handler.
 */

type Sent = { kind: string; token?: string };

function fakeLink(): { link: MacLink; sent: Sent[]; tokens: string[] } {
  const sent: Sent[] = [];
  const tokens: string[] = [];
  const link = {
    send: (m: Sent) => { sent.push(m); },
    setToken: (t: string) => { tokens.push(t); },
  } as unknown as MacLink;
  return { link, sent, tokens };
}

let store: Record<string, string>;

beforeEach(() => {
  store = {};
  (globalThis as { localStorage?: unknown }).localStorage = {
    getItem: (k: string) => (k in store ? store[k] : null),
    setItem: (k: string, v: string) => { store[k] = v; },
    removeItem: (k: string) => { delete store[k]; },
  };
});

afterEach(() => {
  delete (globalThis as { localStorage?: unknown }).localStorage;
});

describe('handlePairingOffer', () => {
  it('first offer: stores, mirrors to host, acks', () => {
    const { link, sent } = fakeLink();
    const hosted: string[] = [];
    expect(handlePairingOffer('T1', link, (t) => hosted.push(t))).toBe('stored');
    expect(store[TOKEN_KEY]).toBe('T1');
    expect(hosted).toEqual(['T1']);
    expect(sent).toEqual([{ kind: 'pairing', token: 'T1' }]);
  });

  it('re-offer of the SAME token re-acks — the lost-ack state is no longer absorbing', () => {
    store[TOKEN_KEY] = 'T1';
    const { link, sent } = fakeLink();
    const hosted: string[] = [];
    expect(handlePairingOffer('T1', link, (t) => hosted.push(t))).toBe('reacked');
    expect(sent).toEqual([{ kind: 'pairing', token: 'T1' }]); // ack closes the Mac's window
    expect(hosted).toEqual([]); // nothing rewritten
  });

  it('offer of a DIFFERENT token is ignored — the rogue-gateway guard holds', () => {
    store[TOKEN_KEY] = 'T1';
    const { link, sent, tokens } = fakeLink();
    expect(handlePairingOffer('EVIL', link, () => {})).toBe('ignored');
    expect(sent).toEqual([]);
    expect(tokens).toEqual([]);
    expect(store[TOKEN_KEY]).toBe('T1');
  });

  it('storage failure: session-only token, NO ack, so real pairing stays possible', () => {
    (globalThis as { localStorage?: unknown }).localStorage = {
      getItem: () => null,
      setItem: () => { throw new Error('private mode'); },
    };
    const { link, sent, tokens } = fakeLink();
    expect(handlePairingOffer('T1', link, () => {})).toBe('session-only');
    expect(tokens).toEqual(['T1']);
    expect(sent).toEqual([]); // an ack would pair the Mac to a token we cannot keep
  });
});
