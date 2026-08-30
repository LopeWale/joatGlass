import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  PAIRING_TOKENS_KEY,
  decodePairingTokens,
  handlePairingOffer,
} from './Pairing';
import type { MacLink } from './MacLink';

/**
 * The absorbing-state bug both audit passes confirmed: an offer of the very
 * token the phone already holds was DROPPED, so a lost ack (or an operator
 * deleting pairing.json) left the Mac's window open to the whole LAN
 * forever, with a manual storage wipe as the only exit. These tests pin
 * every branch of the handler.
 */

type Sent = { kind: string; token?: string };

function fakeLink(
  origin = 'wss://mac-a.example.test',
  initial: Record<string, string> = {},
): { link: MacLink; sent: Sent[]; tokens: Record<string, string> } {
  const sent: Sent[] = [];
  const tokens = { ...initial };
  const link = {
    send: (m: Sent) => { sent.push(m); },
    get pairingToken() { return tokens[origin] ?? null; },
    get pairingTokens() { return { ...tokens }; },
    setToken: (t: string) => { tokens[origin] = t; },
    clearToken: () => { delete tokens[origin]; },
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
  it('rejects an overlarge persisted pairing map instead of broadening trust', () => {
    const entries = Array.from({ length: 17 }, (_, index) => [
      `wss://mac-${index}.example.test`,
      `T${index}`,
    ]);
    expect(decodePairingTokens(JSON.stringify(Object.fromEntries(entries)))).toEqual({});
  });

  it('first offer: stores a current-origin map, mirrors it to host, and acks', () => {
    const { link, sent } = fakeLink();
    const hosted: string[] = [];
    expect(handlePairingOffer('T1', link, (t) => hosted.push(t))).toBe('stored');
    expect(decodePairingTokens(store[PAIRING_TOKENS_KEY])).toEqual({
      'wss://mac-a.example.test': 'T1',
    });
    expect(hosted.map(decodePairingTokens)).toEqual([{
      'wss://mac-a.example.test': 'T1',
    }]);
    expect(sent).toEqual([{ kind: 'pairing', token: 'T1' }]);
  });

  it('re-offer of the SAME token re-acks — the lost-ack state is no longer absorbing', () => {
    const { link, sent } = fakeLink('wss://mac-a.example.test', {
      'wss://mac-a.example.test': 'T1',
    });
    const hosted: string[] = [];
    expect(handlePairingOffer('T1', link, (t) => hosted.push(t))).toBe('reacked');
    expect(sent).toEqual([{ kind: 'pairing', token: 'T1' }]); // ack closes the Mac's window
    expect(hosted).toEqual([]); // nothing rewritten
  });

  it('offer of a DIFFERENT token is ignored — the rogue-gateway guard holds', () => {
    const { link, sent, tokens } = fakeLink('wss://mac-a.example.test', {
      'wss://mac-a.example.test': 'T1',
      'wss://mac-b.example.test': 'T2',
    });
    expect(handlePairingOffer('EVIL', link, () => {})).toBe('ignored');
    expect(sent).toEqual([]);
    expect(tokens).toEqual({
      'wss://mac-a.example.test': 'T1',
      'wss://mac-b.example.test': 'T2',
    });
  });

  it('pairs a second Mac without invalidating the first Mac credential', () => {
    const { link, sent, tokens } = fakeLink('wss://mac-b.example.test', {
      'wss://mac-a.example.test': 'T1',
    });
    const hosted: string[] = [];
    expect(handlePairingOffer('T2', link, (t) => hosted.push(t))).toBe('stored');
    expect(tokens).toEqual({
      'wss://mac-a.example.test': 'T1',
      'wss://mac-b.example.test': 'T2',
    });
    expect(decodePairingTokens(store[PAIRING_TOKENS_KEY])).toEqual(tokens);
    expect(hosted.map(decodePairingTokens)).toEqual([tokens]);
    expect(sent).toEqual([{ kind: 'pairing', token: 'T2' }]);
  });

  it('storage failure: retains no token and NO ack, so a repeat offer can retry', () => {
    (globalThis as { localStorage?: unknown }).localStorage = {
      getItem: () => null,
      setItem: () => { throw new Error('private mode'); },
    };
    const { link, sent, tokens } = fakeLink();
    expect(handlePairingOffer('T1', link, () => {})).toBe('storage-unavailable');
    expect(tokens).toEqual({});
    expect(sent).toEqual([]); // an ack would pair the Mac to a token we cannot keep
  });
});
